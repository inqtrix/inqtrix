/**
 * The run-load engine: many signed-in users against a running stack whose models
 * are the provider stand-in, driven the way the surface drives the API.
 *
 * One Node process, plain fetch, no browser. The stack's lifecycle belongs to the
 * caller; this engine starts nothing. It proves first that the target is wired to
 * the stand-in, because a mistaken target would send the whole load to paid
 * providers. Started by the verification orchestrator as profile `run-load`.
 *
 * Logs and the report carry counts, durations and route names. A question, an
 * answer, an address or a request text never leaves this process.
 */

import { spawn } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { setMaxListeners } from 'node:events'
import { chmod, readFile, readdir, rename, writeFile } from 'node:fs/promises'
import { networkInterfaces } from 'node:os'
import { resolve } from 'node:path'
import { monitorEventLoopDelay, performance } from 'node:perf_hooks'
import process from 'node:process'
import { createInterface } from 'node:readline'
import { setTimeout as delay } from 'node:timers/promises'
import { fileURLToPath } from 'node:url'

import { eraseTemporaryUser } from '../../verification/fixtures/accounts.mjs'
import { fetchActorJson } from '../../verification/fixtures/api.mjs'
import { attemptCleanup } from '../../verification/fixtures/cleanup-step.mjs'
import { runContainerControlCommand } from '../../verification/fixtures/container-control.mjs'
import { VerificationLifecycleClient } from '../../verification/fixtures/lifecycle-client.mjs'
import { normalizeLoadSmokeBaseURL } from '../../verification/fixtures/load-smoke.mjs'
import { assertVerificationRunId } from '../../verification/fixtures/run-scope.mjs'
import { writeScenarioSidecar } from '../../verification/fixtures/scenario-sidecar.mjs'
import {
  NOT_MEASURED,
  POSTGRES_CHECKPOINT_BLOBS_SQL,
  POSTGRES_CHECKPOINT_TABLES_SQL,
  POSTGRES_CHECKPOINT_WRITES_SQL,
  POSTGRES_RUN_EVENTS_SQL,
  POSTGRES_SAMPLE_SQL,
  RESIDUE_TABLES,
  SCENARIO,
  STAGE_NAMES,
  STREAM_LIVE_MARKER,
  reloadWaveWidth,
  accountBelongsToPrefix,
  accountEmail,
  applyRunEvent,
  buildDirectChatRequest,
  buildMixTable,
  buildReport,
  buildRunRequest,
  cancelSpread,
  clarificationAnswerBody,
  createCountGauge,
  createPhaseGauges,
  createPhasedMetrics,
  createRng,
  createRunState,
  createSerialQueue,
  createSseParser,
  deliveryAccount,
  describeFailure,
  desiredActiveRoots,
  findForbiddenContent,
  formatSummaryLine,
  isLocalHostname,
  judgeBaseline,
  judgeLoadApplied,
  judgeWorkerStop,
  judgeResidue,
  judgeConnectionBudget,
  judgeScenarios,
  loopReadings,
  mapLimit,
  networkAddresses,
  nextArrivalSeconds,
  normalizeBaseline,
  normalizeEndpoint,
  numberedQuestion,
  numericSettingsScript,
  parseCgroupStats,
  parseNumericSettings,
  parseSetCookie,
  phaseOverruns,
  parseValkeyCommands,
  planNextRun,
  portOfUrl,
  projectContainers,
  refreshPaths,
  residueSql,
  runDataSql,
  sessionIdFor,
  standInCoverage,
  statementsPerService,
  thinkSeconds,
  unknownDegradedMessages,
  validateConfig,
  WORKER_STOP,
  wiringCheckScript,
} from './run-load-lib.mjs'

const PHASES = ['provision', 'rest', 'idle', 'ramp', 'plateau', 'decay', 'cancel_probe', 'cleanup']
const REPOSITORY_ROOT = fileURLToPath(new URL('../../..', import.meta.url))
const SCENARIOS_PATH = fileURLToPath(new URL('./scenarios.json', import.meta.url))
const STAND_IN_GET = "import os,sys,urllib.request;print(urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('INQTRIX_MOCK_PORT','9300')+sys.argv[1],timeout=5).read().decode())"
const PROBE_GET = "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:9400/probes',timeout=3).read().decode())"
const CGROUP_READ = "grep -E '^anon ' /sys/fs/cgroup/memory.stat; cat /sys/fs/cgroup/pids.current; grep usage_usec /sys/fs/cgroup/cpu.stat"
const VALKEY_READ = 'REDISCLI_AUTH="$INQTRIX_VALKEY_PASSWORD" "$(command -v valkey-cli || command -v redis-cli)" INFO stats'
const PSQL_START = 'psql -X -q -At -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
const PSQL_OPTIONS = 'PGOPTIONS=-c default_transaction_read_only=on -c statement_timeout=120000 -c application_name=run-load-sampler'
const EXEC_TIMEOUT_MS = 15_000
const PSQL_TIMEOUT_MS = 150_000
const INDEXING_POLL_MS = 1_000
const DELETION_TIMEOUT_MS = 120_000
const DELETION_POLL_MS = 500
const LOOP_RESOLUTION_MS = 20

/** A reason to stop that is safe to write down, because this module spelled it. */
class RunLoadStop extends Error {
  constructor(reason) {
    super(reason)
    this.name = 'RunLoadStop'
    this.reason = reason
  }
}

function requiredEnvironment(name) {
  const value = process.env[name]?.trim()
  if (!value) throw new Error(`${name} is required.`)
  return value
}

function progress(message) {
  process.stderr.write(`[run-load] ${message}\n`)
}

function failureKind(error) {
  return typeof error?.cause?.code === 'string'
    ? error.cause.code
    : typeof error?.name === 'string'
      ? error.name
      : 'Error'
}

function parseJson(text) {
  try {
    return JSON.parse(text)
  } catch {
    return null
  }
}

// ------------------------------------------------------------------ HTTP session

/** One browser's cookies and identity, and the headers its page would send. */
class HttpSession {
  #cookies = new Map()

  constructor({ baseURL, label, metrics, requestTimeoutMs }) {
    this.baseURL = baseURL
    this.csrf = null
    this.hostname = new URL(baseURL).hostname
    this.label = label
    this.metrics = metrics
    this.requestTimeoutMs = requestTimeoutMs
    this.user = null
    this.workspaceId = null
  }

  #remember(response) {
    for (const header of response.headers.getSetCookie()) {
      const cookie = parseSetCookie(header, this.hostname)
      if (!cookie) continue
      if (cookie.deleted) this.#cookies.delete(cookie.name)
      else this.#cookies.set(cookie.name, cookie)
    }
  }

  pageHeaders(method, options = {}) {
    const headers = { accept: options.accept ?? 'application/json' }
    if (this.user?.id) headers['X-Inqtrix-Expected-User-Id'] = this.user.id
    const workspace = options.workspaceHeader ?? this.workspaceId
    if (workspace) headers['X-Inqtrix-Workspace-Id'] = workspace
    if (!['GET', 'HEAD'].includes(method) && this.csrf) headers['X-CSRF-Token'] = this.csrf
    if (options.lastEventId) headers['Last-Event-ID'] = options.lastEventId
    return headers
  }

  async rawFetch(method, path, { body, headers, signal }) {
    const sent = { ...headers }
    if (this.#cookies.size > 0) {
      sent.cookie = [...this.#cookies.values()]
        .map((cookie) => `${cookie.name}=${cookie.value}`)
        .join('; ')
    }
    if (body !== undefined) sent['content-type'] = 'application/json'
    const response = await fetch(`${this.baseURL}${path}`, {
      body: body === undefined ? undefined : JSON.stringify(body),
      headers: sent,
      method,
      redirect: 'manual',
      signal,
    })
    this.#remember(response)
    return response
  }

  /** A request measured until its whole body has arrived. */
  async request(method, path, options = {}) {
    const key = normalizeEndpoint(method, path)
    const started = performance.now()
    const signals = [AbortSignal.timeout(this.requestTimeoutMs)]
    if (options.signal) signals.push(options.signal)
    try {
      const response = await this.rawFetch(method, path, {
        body: options.body,
        headers: this.pageHeaders(method, options),
        signal: AbortSignal.any(signals),
      })
      const text = await response.text()
      const elapsed = performance.now() - started
      this.metrics.recordRequest(key, elapsed, response.status)
      if (options.group) this.metrics.recordGroup(options.group, elapsed)
      return { json: () => parseJson(text), ok: response.ok, status: response.status }
    } catch (error) {
      if (!options.signal?.aborted) this.metrics.recordRequestError(key, failureKind(error))
      throw error
    }
  }

  /** A stream measured up to its headers; the caller reads the body. */
  async openStream(method, path, options = {}) {
    const key = normalizeEndpoint(method, path)
    const started = performance.now()
    try {
      const response = await this.rawFetch(method, path, {
        body: options.body,
        headers: this.pageHeaders(method, { ...options, accept: 'text/event-stream' }),
        signal: options.signal,
      })
      const elapsed = performance.now() - started
      this.metrics.recordRequest(key, elapsed, response.status)
      if (options.group) this.metrics.recordGroup(options.group, elapsed)
      return response
    } catch (error) {
      if (!options.signal?.aborted) this.metrics.recordRequestError(key, failureKind(error))
      throw error
    }
  }

  /** The stored-session form the orchestrator signs out with after an interruption. */
  async storageState({ path }) {
    await writeFile(path, `${JSON.stringify({
      cookies: [...this.#cookies.values()].map(({ deleted: _deleted, ...cookie }) => cookie),
      origins: [],
    })}\n`, { encoding: 'utf8', mode: 0o600 })
  }

  /** Sign in as the surface does; a new account adopts a workspace on its first read. */
  async signIn({ email, password, runId }) {
    const login = await this.request('POST', '/api/auth/login/local', { body: { email, password } })
    if (login.status !== 200) throw new Error(`${this.label} login returned HTTP ${login.status}.`)
    let answer = await this.request('GET', '/api/auth/session')
    if (answer.status !== 200) {
      throw new Error(`${this.label} session lookup returned HTTP ${answer.status}.`)
    }
    let session = answer.json()
    if (!session?.project_namespace) {
      const candidate = `e2e-${runId}-${this.label}`
        .toLowerCase()
        .replaceAll(/[^a-z0-9_-]+/g, '-')
        .slice(0, 80)
      answer = await this.request('GET', '/api/auth/session', { workspaceHeader: candidate })
      if (answer.status !== 200) {
        throw new Error(`${this.label} workspace adoption returned HTTP ${answer.status}.`)
      }
      session = answer.json()
    }
    if (
      typeof session?.csrf_token !== 'string'
      || !session.csrf_token
      || typeof session?.user?.id !== 'string'
      || typeof session?.project_namespace !== 'string'
      || !session.project_namespace
    ) throw new Error(`${this.label} session is incomplete.`)
    this.csrf = session.csrf_token
    this.user = { id: session.user.id }
    this.workspaceId = session.project_namespace
  }
}

/** The actor the shared fixtures expect, over this session and its measurements. */
function actorOf(session, email) {
  return {
    context: {
      request: {
        fetch: async (path, options) => {
          const key = normalizeEndpoint(options.method, path)
          const started = performance.now()
          try {
            const response = await session.rawFetch(options.method, path, {
              body: options.data,
              headers: options.headers,
              signal: AbortSignal.timeout(session.requestTimeoutMs),
            })
            const text = await response.text()
            session.metrics.recordRequest(key, performance.now() - started, response.status)
            return { status: () => response.status, text: async () => text }
          } catch (error) {
            session.metrics.recordRequestError(key, failureKind(error))
            throw error
          }
        },
      },
    },
    csrf: session.csrf,
    email,
    label: session.label,
    user: session.user,
    workspaceId: session.workspaceId,
  }
}

/**
 * The orchestrator's ledger rewrites one file per call and takes one call at a
 * time, while the accounts and collections are provisioned side by side.
 */
function oneLifecycleCallAtATime(lifecycle) {
  const inTurn = createSerialQueue()
  return {
    close: () => lifecycle.close(),
    complete: (handle) => inTurn(() => lifecycle.complete(handle)),
    completeSession: (session) => inTurn(() => lifecycle.completeSession(session)),
    register: (resource) => inTurn(() => lifecycle.register(resource)),
    registerSession: (context, label) => inTurn(() => lifecycle.registerSession(context, label)),
  }
}

// ------------------------------------------------------------------ database session

/** One long-lived read-only psql session: a sampler opens no connection per reading. */
class PsqlSession {
  #child = null
  #onLine = null
  #sequence = 0
  #tail = Promise.resolve()

  constructor({ command, container }) {
    this.command = command
    this.container = container
    this.restarts = 0
    this.started = false
  }

  #start() {
    if (this.started) this.restarts += 1
    this.started = true
    const child = spawn(this.command, [
      'exec',
      '--interactive',
      '--env',
      PSQL_OPTIONS,
      this.container.id,
      'sh',
      '-c',
      PSQL_START,
    ], {
      cwd: REPOSITORY_ROOT,
      env: process.env,
      shell: false,
      stdio: ['pipe', 'pipe', 'ignore'],
    })
    child.stdin.on('error', () => undefined)
    child.once('error', () => undefined)
    child.once('close', () => {
      if (this.#child === child) this.#child = null
      this.#onLine?.(null)
    })
    createInterface({ input: child.stdout, crlfDelay: Number.POSITIVE_INFINITY })
      .on('line', (line) => this.#onLine?.(line))
    this.#child = child
  }

  /** The lines of one statement, or null when the session could not answer. */
  query(sql) {
    const run = this.#tail.then(() => this.#run(sql))
    this.#tail = run.catch(() => undefined)
    return run
  }

  #run(sql) {
    if (!this.#child) this.#start()
    this.#sequence += 1
    const sentinel = `__run_load_${this.#sequence}__`
    const lines = []
    return new Promise((resolvePromise) => {
      const settle = (value) => {
        clearTimeout(timer)
        this.#onLine = null
        resolvePromise(value)
      }
      const timer = setTimeout(() => {
        this.#child?.kill('SIGKILL')
        settle(null)
      }, PSQL_TIMEOUT_MS)
      this.#onLine = (line) => {
        if (line === null) settle(null)
        else if (line === sentinel) settle(lines)
        else lines.push(line)
      }
      this.#child.stdin.write(`${sql};\n\\echo ${sentinel}\n`)
    })
  }

  async queryJson(sql) {
    const lines = await this.query(sql)
    return lines?.length ? parseJson(lines.join('\n')) : null
  }

  async queryNumber(sql) {
    const lines = await this.query(sql)
    if (!lines?.length) return null
    const value = Number(lines[0])
    return Number.isFinite(value) ? value : null
  }

  close() {
    this.#child?.stdin.end()
    this.#child = null
  }
}

// ------------------------------------------------------------------ samplers

/**
 * Counters from outside the application: Postgres, the container cgroups, the
 * broker, the stand-in and the optional probes. A value that cannot be read stays
 * `not_measured`; it never reads as zero.
 */
class Sampling {
  #errors = new Map()
  #owners = new Map()
  #stopped = false
  #timers = new Map()

  constructor({ command, config, onStandIn, project }) {
    this.command = command
    this.config = config
    this.containers = []
    this.gauges = new Map(PHASES.map((phase) => [phase, createPhaseGauges()]))
    this.onStandIn = onStandIn
    this.phase = PHASES[0]
    this.project = project
    this.psql = null
    this.replacedContainers = 0
    this.rssMax = 0
    this.seenContainers = new Map()
    this.stoppedContainers = new Set()
  }

  #countError(name) {
    this.#errors.set(name, (this.#errors.get(name) ?? 0) + 1)
  }

  #exec(container, args, timeoutMs = EXEC_TIMEOUT_MS) {
    return runContainerControlCommand(
      this.command,
      ['exec', container.id, ...args],
      REPOSITORY_ROOT,
      null,
      timeoutMs,
    )
  }

  service(name) {
    return this.containers.filter((container) => container.service === name && container.running)
  }

  async refreshContainers() {
    const inventory = await runContainerControlCommand(
      this.command,
      ['ps', '--all', '--format', 'json'],
      REPOSITORY_ROOT,
      null,
      EXEC_TIMEOUT_MS,
    ).catch(() => null)
    if (inventory?.exitCode !== 0) throw new RunLoadStop('container_inventory_unavailable')
    const rows = parseJson(inventory.stdout)
    if (!Array.isArray(rows)) throw new RunLoadStop('container_inventory_invalid')
    this.containers = projectContainers(rows, this.project)
    const fresh = []
    for (const container of this.containers) {
      const known = this.seenContainers.get(container.name)
      if (known && known !== container.id) this.replacedContainers += 1
      if (known !== container.id) fresh.push(container)
      this.seenContainers.set(container.name, container.id)
      if (!container.running) this.stoppedContainers.add(container.name)
    }
    await mapLimit(fresh, this.config.sampling.exec_concurrency, async (container) => {
      // Only the network section: the rest of an inspect holds the environment.
      const inspected = await runContainerControlCommand(
        this.command,
        ['inspect', '--format', '{{json .NetworkSettings.Networks}}', container.id],
        REPOSITORY_ROOT,
        null,
        EXEC_TIMEOUT_MS,
      ).catch(() => null)
      if (inspected?.exitCode !== 0) return
      for (const address of networkAddresses(parseJson(inspected.stdout))) {
        this.#owners.set(address, { name: container.name, service: container.service })
      }
    })
  }

  ownerOfAddress = (address) => {
    if (address === 'local') return { name: 'local', service: 'local' }
    return this.#owners.get(address) ?? { name: 'unknown', service: 'unknown' }
  }

  async wiredToStandIn(container) {
    const result = await this.#exec(
      container,
      ['sh', '-c', wiringCheckScript(this.config.stand_in)],
    ).catch(() => null)
    return result?.exitCode === 0 && result.stdout.trim() === '1'
  }

  /** The limits this stack runs under, read from the first replica of each service. */
  async numericSettings(settings) {
    const result = {}
    for (const [service, variables] of Object.entries(settings ?? {})) {
      const [container] = this.service(service)
      const read = container
        ? await this.#exec(container, ['sh', '-c', numericSettingsScript(variables)]).catch(() => null)
        : null
      result[service] = parseNumericSettings(read?.exitCode === 0 ? read.stdout : '', variables)
    }
    return result
  }

  async standIn(path) {
    const [container] = this.service(this.config.stand_in.service)
    if (!container) return null
    const result = await this.#exec(container, ['python', '-c', STAND_IN_GET, path]).catch(() => null)
    return result?.exitCode === 0 ? parseJson(result.stdout) : null
  }

  async valkeyCommands() {
    const [container] = this.service('valkey')
    if (!container) return null
    const result = await this.#exec(container, ['sh', '-c', VALKEY_READ]).catch(() => null)
    return result?.exitCode === 0 ? parseValkeyCommands(result.stdout) : null
  }

  async containerStats(container) {
    const result = await this.#exec(container, ['sh', '-c', CGROUP_READ]).catch(() => null)
    return result?.exitCode === 0 ? parseCgroupStats(result.stdout) : null
  }

  /** The probes of the API and the workers; a process without them is `not_measured`. */
  async readProbes() {
    const targets = [...this.service('api'), ...this.service('worker')]
    const readings = await mapLimit(targets, this.config.sampling.exec_concurrency, async (container) => {
      const result = await this.#exec(container, ['python', '-c', PROBE_GET]).catch(() => null)
      const values = result?.exitCode === 0 ? parseJson(result.stdout) : null
      return [container.name, { service: container.service, values: values ?? null }]
    })
    return Object.fromEntries(readings)
  }

  async startPostgres() {
    const [container] = this.service('postgres')
    if (!container) throw new RunLoadStop('postgres_not_running')
    this.psql = new PsqlSession({ command: this.command, container })
    return await this.psql.queryJson(POSTGRES_SAMPLE_SQL)
  }

  /** The counters at a phase boundary, read now instead of at the last interval. */
  async snapshot() {
    const at = Date.now() / 1000
    const [pg, runEvents, valkey, standIn, probes] = await Promise.all([
      this.psql?.queryJson(POSTGRES_SAMPLE_SQL) ?? null,
      this.psql?.queryNumber(POSTGRES_RUN_EVENTS_SQL) ?? null,
      this.valkeyCommands(),
      this.standIn('/admin/stats'),
      this.readProbes(),
    ])
    return {
      at,
      loop: loopReadings(probes),
      pg,
      run_events: runEvents,
      stand_in: standIn,
      statements: statementsPerService(probes),
      valkey,
    }
  }

  setPhase(name) {
    this.phase = name
  }

  #gauges() {
    return this.gauges.get(this.phase)
  }

  #every(name, seconds, task) {
    const tick = async () => {
      if (this.#stopped) return
      const started = performance.now()
      try {
        await task()
      } catch {
        this.#countError(name)
      }
      if (this.#stopped) return
      const wait = Math.max(0, seconds * 1000 - (performance.now() - started))
      this.#timers.set(name, setTimeout(tick, wait))
    }
    this.#timers.set(name, setTimeout(tick, seconds * 1000))
  }

  start() {
    const { sampling } = this.config
    this.#every('postgres', sampling.postgres_seconds, async () => {
      const sample = await this.psql?.queryJson(POSTGRES_SAMPLE_SQL)
      if (!sample) throw new Error('no reading')
      this.#gauges().recordPostgres(sample, this.ownerOfAddress)
    })
    this.#every('postgres_tables', sampling.postgres_table_seconds, async () => {
      if (await this.psql?.queryNumber(POSTGRES_CHECKPOINT_TABLES_SQL) !== 2) return
      const [blobs, writes] = await Promise.all([
        this.psql?.queryNumber(POSTGRES_CHECKPOINT_BLOBS_SQL),
        this.psql?.queryNumber(POSTGRES_CHECKPOINT_WRITES_SQL),
      ])
      if (Number.isFinite(blobs) || Number.isFinite(writes)) this.#gauges().recordCheckpoint(blobs, writes)
    })
    this.#every('containers', sampling.containers_seconds, async () => {
      await this.refreshContainers()
      await mapLimit(
        this.containers.filter((container) => container.running),
        sampling.exec_concurrency,
        async (container) => {
          const stats = await this.containerStats(container)
          this.#gauges().recordContainer(container, stats, Date.now() / 1000)
        },
      )
    })
    this.#every('stand_in', sampling.stand_in_seconds, async () => {
      this.rssMax = Math.max(this.rssMax, process.memoryUsage().rss)
      const stats = await this.standIn('/admin/stats')
      if (!stats) throw new Error('no reading')
      this.#gauges().recordStandIn(stats)
      this.onStandIn?.(stats)
    })
  }

  /** Stop reading; the psql session stays open for the final counts. */
  stop() {
    this.#stopped = true
    for (const timer of this.#timers.values()) clearTimeout(timer)
  }

  facts() {
    return {
      errors: Object.fromEntries(this.#errors),
      postgres_restarts: this.psql?.restarts ?? 0,
      replaced_containers: this.replacedContainers,
      stopped_containers: [...this.stoppedContainers].sort(),
    }
  }
}

// ------------------------------------------------------------------ one tab

/** One signed-in page: its first load, the user stream and the background reads. */
class Tab {
  constructor(engine, account) {
    this.account = account
    this.busy = false
    this.collection = null
    this.collectionId = null
    this.controller = new AbortController()
    setMaxListeners(0, this.controller.signal)
    this.engine = engine
    this.lastEventId = undefined
    this.open = false
    this.pendingWake = { all: false, scopes: new Set() }
    this.ready = false
    this.reconnects = 0
    this.session = new HttpSession({
      baseURL: engine.baseURL,
      label: `rl-${account.number}`,
      metrics: engine.metrics,
      requestTimeoutMs: engine.config.transport.request_timeout_seconds * 1000,
    })
    this.wakeTimer = null
  }

  get signal() {
    return this.controller.signal
  }

  async signIn() {
    await this.session.signIn({
      email: this.account.email,
      password: this.engine.userPassword,
      runId: this.engine.runId,
    })
  }

  /** A failed background read is counted where it happened and changes nothing here. */
  async #read(method, path) {
    const isList = this.engine.config.tab.desk_switch.includes(path)
    try {
      await this.session.request(method, path, {
        group: isList ? 'lists' : undefined,
        signal: this.signal,
      })
    } catch {
      if (!this.signal.aborted) this.engine.metrics.count('tab_read_failures')
    }
  }

  async #loadPage(group) {
    const { config, metrics } = this.engine
    const started = performance.now()
    await mapLimit(config.page_load, config.tab.page_load_parallel, async (entry) => {
      const [method, path] = entry.split(' ')
      await this.#read(method, path)
    })
    metrics.recordGroup(group, performance.now() - started)
  }

  /** The same requests again; the stream and the timers of the open tab go on. */
  reloadPage() {
    return this.#loadPage('page_reload')
  }

  async openPage() {
    await this.#loadPage('page_load')
    this.open = true
    this.#runBackground()
  }

  #runBackground() {
    const { config, jitterRng } = this.engine
    void this.#userStream()
    const quotaMs = config.tab.quota_poll_seconds * 1000
    void this.#every(jitterRng.between(0, quotaMs), () => quotaMs, async () => {
      await this.#read('GET', '/v1/quota/usage')
    })
    const switchMs = config.tab.desk_switch_seconds * 1000
    const jitter = config.tab.desk_switch_jitter_share
    void this.#every(
      jitterRng.between(0, switchMs),
      () => jitterRng.between(switchMs * (1 - jitter), switchMs * (1 + jitter)),
      async () => {
        await Promise.all(config.tab.desk_switch.map((path) => this.#read('GET', path)))
        this.engine.metrics.count('desk_switches')
      },
    )
  }

  async #every(firstMs, nextMs, task) {
    try {
      await delay(firstMs, undefined, { signal: this.signal })
      while (!this.signal.aborted) {
        await task()
        await delay(nextMs(), undefined, { signal: this.signal })
      }
    } catch (error) {
      if (!this.signal.aborted) {
        this.engine.metrics.count('tab_background_failures')
        progress(`A tab background task stopped: ${describeFailure(error)}`)
      }
    }
  }

  async #userStream() {
    const { config, metrics } = this.engine
    while (!this.signal.aborted) {
      try {
        const response = await this.session.openStream('GET', '/v1/user/events', {
          lastEventId: this.lastEventId,
          signal: this.signal,
        })
        if (!response.ok) {
          await response.body?.cancel()
          metrics.streamDrop('user_events', `http_${response.status}`)
          if (response.status === 401 || response.status === 403) {
            metrics.count('tabs_signed_out')
            return
          }
        } else {
          const parser = createSseParser(
            (frame) => this.#onUserFrame(frame),
            { onOverflow: () => metrics.count('sse_frames_oversized') },
          )
          for await (const chunk of response.body) parser.push(chunk)
          parser.end()
          if (!this.signal.aborted) metrics.streamDrop('user_events', 'ended')
        }
      } catch (error) {
        if (this.signal.aborted) return
        metrics.streamDrop('user_events', failureKind(error))
      }
      const waitMs = Math.min(
        config.user_events.reconnect_max_seconds * 1000,
        1_000 * 2 ** Math.min(this.reconnects, 5),
      )
      this.reconnects += 1
      try {
        await delay(waitMs, undefined, { signal: this.signal })
      } catch {
        return
      }
    }
  }

  #onUserFrame(frame) {
    const { config, metrics } = this.engine
    const { user_events: userEvents } = config
    if (!frame.event) return
    metrics.count(`user_events_${frame.event}`)
    if (frame.event === 'ready') {
      this.ready = true
      this.reconnects = 0
      this.lastEventId = frame.id ?? parseJson(frame.data)?.cursor
      if (userEvents.enabled) this.#noteWake(null)
    } else if (frame.event === 'invalidate') {
      this.lastEventId = frame.id ?? this.lastEventId
      const scope = parseJson(frame.data)?.scope
      if (!userEvents.enabled || userEvents.consumed_scopes.includes(scope)) return
      // The page confirms who it is before any list is read again.
      void this.#read('GET', userEvents.identity_probe).then(() => this.#noteWake(scope ?? null))
    } else if (frame.event === 'reset') {
      this.lastEventId = undefined
      if (!userEvents.enabled) return
      void this.#read('GET', userEvents.identity_probe).then(() => this.#noteWake(null))
    }
  }

  /** Events close together are folded into one refresh, as the page folds them. */
  #noteWake(scope) {
    if (this.signal.aborted) return
    if (scope === null) this.pendingWake.all = true
    else this.pendingWake.scopes.add(scope)
    clearTimeout(this.wakeTimer)
    this.wakeTimer = setTimeout(() => {
      const wake = this.pendingWake
      this.pendingWake = { all: false, scopes: new Set() }
      const paths = refreshPaths(wake, this.engine.config.user_events)
      void Promise.all(paths.map((path) => this.#read('GET', path)))
    }, this.engine.config.user_events.wake_debounce_ms)
  }

  close() {
    this.open = false
    clearTimeout(this.wakeTimer)
    this.controller.abort()
  }
}

// ------------------------------------------------------------------ one root run

/** One root run from its submit to its result, answered and followed like the surface. */
class RootRun {
  constructor(engine, tab, entry, number) {
    this.answered = new Set()
    this.childTasks = new Set()
    this.controller = new AbortController()
    this.engine = engine
    this.entry = entry
    this.firstEventSeen = false
    this.number = number
    this.started = false
    this.state = null
    this.stopped = false
    this.submittedAt = 0
    this.tab = tab
    this.unfinished = false
  }

  get signal() {
    return this.controller.signal
  }

  async #sleep(ms) {
    try {
      await delay(ms, undefined, { signal: this.signal })
    } catch {
      // Stopped: the caller checks the state it is waiting on.
    }
  }

  async drive() {
    const { engine, entry, tab } = this
    const { config, metrics, mix } = engine
    try {
      const keys = entry.questions
      const text = config.questions[keys[engine.questionRng.int(keys.length)]]
      const body = buildRunRequest({
        collectionId: tab.collectionId,
        question: numberedQuestion(text, this.number),
        scenario: entry,
        sessionId: sessionIdFor(entry.mode, randomUUID()),
        workspaceId: tab.session.workspaceId,
      })
      this.submittedAt = performance.now()
      engine.noteSubmit()
      const answer = await tab.session.request('POST', '/v1/runs', {
        body,
        group: 'submit',
        signal: this.signal,
      })
      if (answer.status !== 202) {
        metrics.count('runs_rejected')
        mix.rejected[entry.id] = (mix.rejected[entry.id] ?? 0) + 1
        progress(`Run refused, ${entry.id}: ${answer.status} ${answer.json()?.error?.type ?? 'without a reason'}.`)
        return
      }
      const runId = answer.json()?.run_id
      if (typeof runId !== 'string' || !runId) {
        metrics.count('runs_rejected')
        return
      }
      this.state = createRunState(runId)
      this.started = true
      metrics.count('runs_started')
      mix.started[entry.id] = (mix.started[entry.id] ?? 0) + 1
      await this.#follow()
    } catch (error) {
      if (!this.signal.aborted) {
        metrics.count('run_driver_failures')
        progress(`A run driver stopped: ${describeFailure(error)}`)
      }
    } finally {
      if (this.started) await this.#finish()
      this.controller.abort()
      await Promise.allSettled([...this.childTasks])
      tab.busy = false
      engine.rootEnded(this)
    }
  }

  async #follow() {
    const { state } = this
    const { metrics } = this.engine
    while (!state.terminal && !this.stopped) {
      const outcome = await this.#stream()
      if (state.terminal || this.stopped || this.signal.aborted) return
      if (outcome === 'parked') {
        if (state.parked) await this.#parked()
        continue
      }
      metrics.streamDrop('run_events', outcome)
      await this.#outagePolls()
    }
  }

  /** The surface's watchdog times: a stream that stays silent is opened again. */
  async #stream() {
    const { state } = this
    const { config, metrics } = this.engine
    const { first_activity_seconds: firstSeconds, inactivity_seconds: quietSeconds } = config.transport
    const attempt = new AbortController()
    let reason = null
    let live = false
    let watchdog = null
    const arm = (seconds) => {
      clearTimeout(watchdog)
      watchdog = setTimeout(() => {
        reason = 'inactive'
        attempt.abort()
      }, seconds * 1000)
    }
    const close = (why) => {
      reason ??= why
      attempt.abort()
    }
    arm(firstSeconds)
    try {
      const query = state.lastSequence === null ? '' : `?after=${state.lastSequence}`
      metrics.count('run_streams_opened')
      const response = await this.tab.session.openStream('GET', `/v1/runs/${state.runId}/events${query}`, {
        signal: AbortSignal.any([this.signal, attempt.signal]),
      })
      if (response.status === 401 || response.status === 404) {
        await response.body?.cancel()
        state.terminal = 'unavailable'
        return 'terminal'
      }
      if (!response.ok) {
        await response.body?.cancel()
        return `http_${response.status}`
      }
      const parser = createSseParser((frame) => {
        arm(quietSeconds)
        if (!frame.data || reason) return
        const event = parseJson(frame.data)
        if (!event) {
          metrics.count('sse_frames_unparseable')
          return
        }
        if (event.type === STREAM_LIVE_MARKER) {
          live = true
          if (state.parked) close('parked')
          return
        }
        this.#onEvent(event, live ? 'live' : 'replayed')
        if (state.terminal) close('terminal')
        else if (live && state.parked) close('parked')
      }, { onOverflow: () => metrics.count('sse_frames_oversized') })
      for await (const chunk of response.body) {
        arm(quietSeconds)
        parser.push(chunk)
      }
      parser.end()
      return reason ?? (state.terminal ? 'terminal' : 'ended')
    } catch (error) {
      return reason ?? (this.signal.aborted ? 'stopped' : failureKind(error))
    } finally {
      clearTimeout(watchdog)
    }
  }

  #onEvent(event, delivery) {
    const { metrics } = this.engine
    if (typeof event.sequence === 'number' && !this.firstEventSeen) {
      this.firstEventSeen = true
      metrics.recordGroup('first_event', performance.now() - this.submittedAt)
    }
    metrics.count(`events_${delivery}`)
    if (delivery === 'live' && Number.isFinite(event.created_at)) {
      const lagMs = Date.now() - event.created_at * 1000
      if (lagMs < 0) metrics.count('sse_lag_negative')
      metrics.recordLag(lagMs)
    }
    for (const gate of applyRunEvent(this.state, event)) this.#openChildGate(gate)
  }

  /** While a transport is down the surface polls the same events, then tries again. */
  async #outagePolls() {
    const { outage_poll_seconds: seconds, outage_polls_per_retry: polls } = this.engine.config.transport
    for (let poll = 0; poll < polls; poll += 1) {
      await this.#pollEvents()
      if (this.state.terminal || this.state.parked || this.stopped || this.signal.aborted) return
      await this.#sleep(seconds * 1000)
    }
  }

  async #pollEvents() {
    const { state, tab } = this
    const after = state.lastSequence === null ? '' : `&after=${state.lastSequence}`
    try {
      const answer = await tab.session.request(
        'GET',
        `/v1/runs/${state.runId}/events?format=json${after}`,
        { signal: this.signal },
      )
      if (answer.status === 401 || answer.status === 404) {
        state.terminal = 'unavailable'
        return
      }
      if (answer.status !== 200) return
      for (const event of answer.json()?.data ?? []) this.#onEvent(event, 'polled')
    } catch {
      this.engine.metrics.count('run_poll_failures')
    }
  }

  /**
   * A parked run sends nothing, so the surface closes its stream and polls until
   * the person has decided; it reads the gate at once and the person takes their
   * time. Then the stream opens again.
   */
  async #parked() {
    const { engine, state } = this
    const { config, metrics, thinkRng } = engine
    const status = state.parked
    const seconds = thinkSeconds(thinkRng, config.think)
    const decideAt = performance.now() + seconds * 1000
    metrics.count(status === 'waiting_for_approval' ? 'gates_approval' : 'gates_input')
    if (seconds >= config.think.late_seconds) metrics.count('gates_left_parked')
    await this.#readGate(state.runId, status)
    const pollMs = config.transport.parked_poll_seconds * 1000
    for (;;) {
      await this.#pollEvents()
      if (state.terminal || !state.parked || this.stopped || this.signal.aborted) return
      const remaining = decideAt - performance.now()
      if (remaining <= 0) break
      await this.#sleep(Math.min(remaining, pollMs))
      if (this.stopped || this.signal.aborted) return
    }
    const decided = await this.#decide(state.runId, status)
    if (decided) {
      state.parked = null
      return
    }
    metrics.count('gates_without_row')
    await this.#sleep(pollMs)
    await this.#pollEvents()
  }

  async #readGate(runId, status) {
    const path = status === 'waiting_for_approval' ? 'approvals' : 'clarifications'
    try {
      return await this.tab.session.request('GET', `/v1/runs/${runId}/${path}`, { signal: this.signal })
    } catch {
      this.engine.metrics.count('run_poll_failures')
      return null
    }
  }

  /** Answer every open row of this gate; true when one was answered. */
  async #decide(runId, status) {
    const { config, metrics } = this.engine
    const { session } = this.tab
    const approval = status === 'waiting_for_approval'
    const rows = await this.#readGate(runId, status)
    if (!rows || rows.status !== 200) return false
    let decided = false
    for (const row of rows.json()?.data ?? []) {
      if (row?.status !== 'pending') continue
      const id = approval ? row.approval_id : row.clarification_id
      const key = `${runId}:${id}`
      if (this.answered.has(key)) continue
      try {
        const answer = await session.request(
          'POST',
          `/v1/runs/${runId}/${approval ? 'approvals' : 'clarifications'}/${id}`,
          {
            body: approval
              ? { decision: 'approve' }
              : clarificationAnswerBody(row, config.clarification_answer),
            signal: this.signal,
          },
        )
        // A refusal stays a refusal; only a request that got nowhere is tried again.
        this.answered.add(key)
        if (answer.ok) decided = true
        else metrics.count('gate_answers_refused')
      } catch {
        metrics.count('gate_answers_failed')
      }
    }
    return decided
  }

  /** A child's gate blocks the delegation; the parent's stream stays open meanwhile. */
  #openChildGate({ childRunId, status }) {
    const { config, metrics, thinkRng } = this.engine
    const task = (async () => {
      metrics.count('gates_child')
      const seconds = thinkSeconds(thinkRng, config.think)
      if (seconds >= config.think.late_seconds) metrics.count('gates_left_parked')
      await Promise.all([
        this.#readGate(childRunId, 'waiting_for_approval'),
        this.#readGate(childRunId, 'waiting_for_input'),
      ])
      await this.#sleep(seconds * 1000)
      if (this.signal.aborted || this.stopped) return
      const decided = await this.#decide(childRunId, status)
      if (!decided) metrics.count('gates_without_row')
    })().catch(() => metrics.count('gate_answers_failed'))
    this.childTasks.add(task)
    void task.finally(() => this.childTasks.delete(task))
  }

  async #finish() {
    const { metrics } = this.engine
    const { state } = this
    const { duplicated, lost } = deliveryAccount(state)
    metrics.count('events_duplicated', duplicated)
    if (lost !== null) {
      metrics.count('events_lost', lost)
      metrics.count('runs_with_full_account')
    }
    if (state.terminal === 'inqtrix.run.completed') {
      metrics.count('runs_completed')
      try {
        const answer = await this.tab.session.request('GET', `/v1/runs/${state.runId}/result`, { group: 'result' })
        if (answer.status !== 200) metrics.count('results_unreadable')
      } catch {
        metrics.count('results_unreadable')
      }
    } else if (state.terminal === 'inqtrix.run.failed') {
      metrics.count('runs_failed')
    } else if (state.terminal === 'inqtrix.run.cancelled' && !this.unfinished) {
      metrics.count('runs_cancelled')
    } else if (state.terminal === 'unavailable') {
      metrics.count('runs_unavailable')
      metrics.count('runs_failed')
    } else {
      metrics.count('runs_unfinished')
    }
  }

  /** End of the decay: stop the run and count it as not finished. */
  async cancelAsUnfinished() {
    this.unfinished = true
    if (!this.state) return
    try {
      await this.tab.session.request('POST', `/v1/runs/${this.state.runId}/cancel`, { body: {} })
    } catch {
      this.engine.metrics.count('run_cancel_failures')
    }
  }

  abandon() {
    this.stopped = true
    this.controller.abort()
  }
}

// ------------------------------------------------------------------ the engine

class Engine {
  constructor({ adminEmail, adminPassword, baseURL, baselinePath, command, config, fault, reloadWave, configDigest, lifecycle, project, prefix, reportDirectory, runId, stageName, userPassword }) {
    this.fault = fault
    this.faultRecord = null
    this.reloadWave = reloadWave
    this.reloadWaveRecord = null
    this.accepting = false
    this.configDigest = configDigest
    this.accountPrefix = prefix
    this.accounts = []
    this.activeRootGauge = createCountGauge()
    this.activeRoots = new Set()
    this.adminActor = null
    this.adminEmail = adminEmail
    this.adminPassword = adminPassword
    this.adminSession = null
    this.adminLifecycleSession = null
    this.baseURL = baseURL
    this.baselinePath = baselinePath
    this.cancelProbeResult = null
    this.config = config
    this.directInFlight = new Set()
    this.firstSubmitAt = null
    this.lifecycle = lifecycle
    this.metrics = createPhasedMetrics(PHASES)
    this.pendingStarts = 0
    this.phases = {}
    this.probes = {}
    this.provisioning = {
      accounts_created: 0,
      accounts_wanted: config.stages[stageName].tabs,
      collections_ready: 0,
      collections_wanted: config.stages[stageName].collection_users,
      failures: [],
      owner_created: false,
      preflight_passed: false,
      samplers_running: false,
      tabs_open: 0,
      tabs_signed_in: 0,
    }
    this.questionNumber = 0
    this.reportDirectory = reportDirectory
    this.rootPromises = new Set()
    this.runData = null
    this.runId = runId
    this.sampling = new Sampling({
      command,
      config,
      onStandIn: (stats) => this.#watchStandIn(stats),
      project,
    })
    this.startTimers = new Set()
    this.jitterRng = createRng(config.seed + 1)
    this.mixRng = createRng(config.seed)
    this.questionRng = createRng(config.seed + 2)
    this.startRng = createRng(config.seed + 3)
    this.thinkRng = createRng(config.seed + 5)
    this.directRng = createRng(config.seed + 6)
    this.silentReadings = 0
    this.stack = { project, stand_in: null }
    this.stage = config.stages[stageName]
    this.stageName = stageName
    this.standInBefore = null
    this.standInAfter = null
    this.standInSeen = false
    this.watchFrom = null
    this.stopController = new AbortController()
    setMaxListeners(0, this.stopController.signal)
    this.stopReason = null
    this.table = buildMixTable(config)
    this.tabs = []
    this.userPassword = userPassword
    this.workloadRan = false
    this.mix = {
      planned: Object.fromEntries(
        this.table.map((entry) => [entry.id, Math.round(entry.planned_share * 10_000) / 10_000]),
      ),
      redraws: 0,
      rejected: {},
      seed: config.seed,
      skipped: 0,
      started: {},
    }
  }

  get stopped() {
    return this.stopController.signal.aborted
  }

  requestStop(reason) {
    if (this.stopped) return
    this.stopReason = reason
    progress(`Stopping: ${reason}`)
    for (const timer of this.startTimers) clearTimeout(timer)
    this.startTimers.clear()
    this.stopController.abort()
  }

  nextQuestionNumber() {
    this.questionNumber += 1
    return this.questionNumber
  }

  async sleep(ms) {
    try {
      await delay(ms, undefined, { signal: this.stopController.signal })
    } catch {
      // Stopped: the caller checks.
    }
  }

  enterPhase(name) {
    this.metrics.setPhase(name)
    this.sampling.setPhase(name)
    progress(`Phase ${name}.`)
  }

  noteSubmit() {
    this.firstSubmitAt ??= performance.now()
  }

  /**
   * The stand-in must see the work. A run that finishes while it stays silent was
   * answered by something else, so the load stops before that gets expensive.
   */
  #watchStandIn(stats) {
    if (this.standInSeen || this.firstSubmitAt === null || !this.watchFrom) return
    if ((stats.total_calls ?? 0) > (this.watchFrom.total_calls ?? 0)) {
      this.standInSeen = true
      return
    }
    const finished = this.metrics.counter('runs_completed') + this.metrics.counter('direct_completed')
    const silentForMs = performance.now() - this.firstSubmitAt
    if (finished > 0 || silentForMs > this.config.stand_in.silent_after_seconds * 1000) {
      this.silentReadings += 1
      if (this.silentReadings >= 2) this.requestStop('stand_in_saw_no_call')
    }
  }

  // ---------------------------------------------------------------- step 1

  async proveStack() {
    const { config, provisioning, sampling } = this
    const failures = provisioning.failures
    await sampling.refreshContainers()
    if (sampling.containers.length === 0) {
      failures.push('no_container_of_project')
      throw new RunLoadStop('preflight_failed')
    }
    const { hostname } = new URL(this.baseURL)
    const own = Object.values(networkInterfaces()).flat().map((row) => row?.address).filter(Boolean)
    if (!isLocalHostname(hostname, own)) failures.push('base_url_is_not_this_machine')
    const port = portOfUrl(this.baseURL)
    if (!sampling.containers.some((container) => container.running && container.ports.includes(port))) {
      failures.push('base_url_port_is_not_published_by_the_project')
    }
    for (const service of config.stand_in.required_wired_services) {
      if (sampling.service(service).length === 0) failures.push(`service_not_running:${service}`)
    }
    if (sampling.service(config.stand_in.service).length === 0) {
      failures.push('stand_in_not_running')
      throw new RunLoadStop('preflight_failed')
    }
    const wired = config.stand_in.wired_services.flatMap((service) => sampling.service(service))
    const wiring = await mapLimit(wired, config.sampling.exec_concurrency, async (container) => (
      await sampling.wiredToStandIn(container) ? null : `not_wired_to_stand_in:${container.service}`
    ))
    failures.push(...new Set(wiring.filter(Boolean)))
    const health = await sampling.standIn('/healthz')
    if (!health) {
      failures.push('stand_in_health_unreadable')
    } else {
      const unknown = unknownDegradedMessages(health, config.stand_in.known_degraded)
      if (unknown.length > 0) failures.push(`stand_in_degraded:${unknown.length}`)
      this.stack.stand_in = {
        conversations: health.conversations,
        degraded_known: (health.degraded?.length ?? 0) - unknown.length,
        degraded_unknown: unknown.map((message) => message.slice(0, 160)),
        embedding_dim: health.embedding_dim,
        latency_scale: health.latency_scale,
        operations: health.operations,
        responses: health.responses,
        selection: typeof health.selection === 'string'
          ? health.selection
          : Object.keys(health.selection ?? {}).sort(),
        signatures: health.signatures,
        status: health.status,
      }
      if (health.latency_scale !== 1) {
        progress('The stand-in runs at a scaled latency: a rehearsal, no capacity result.')
      }
    }
    this.standInBefore = await sampling.standIn('/admin/stats')
    if (!this.standInBefore) failures.push('stand_in_stats_unreadable')
    // A sampler that does not read can only report silence, so it stops the run here.
    const firstSample = await sampling.startPostgres().catch(() => null)
    if (!firstSample) failures.push('postgres_sampler_not_reading')
    const statsRead = await sampling.containerStats(sampling.service('postgres')[0] ?? sampling.containers[0])
    if (!statsRead) failures.push('container_sampler_not_reading')
    if (sampling.service('valkey').length > 0 && (await sampling.valkeyCommands()) === null) {
      failures.push('valkey_sampler_not_reading')
    }
    if (this.fault && sampling.service('worker').length < 2) failures.push('worker_stop_needs_two_workers')
    if (failures.length > 0) throw new RunLoadStop('preflight_failed')
    this.stack.settings = await sampling.numericSettings(config.setup_settings)
    this.stack.containers_by_service = {}
    for (const container of sampling.containers) {
      if (!container.running) continue
      this.stack.containers_by_service[container.service] = (
        this.stack.containers_by_service[container.service] ?? 0
      ) + 1
    }
    provisioning.preflight_passed = true
    provisioning.samplers_running = true
    sampling.start()
    progress('The stack is proven to answer from the stand-in; samplers are reading.')
  }

  /** Only after the proof: a first owner on an empty stack costs no provider call. */
  async ensureOwner() {
    const session = this.#newAdminSession()
    const status = await session.request('GET', '/api/setup/status').catch(() => null)
    if (status?.status !== 200 || status.json()?.needs_owner !== true) return
    const created = await session.request('POST', '/api/setup/owner', {
      body: {
        display_name: 'Run load owner',
        email: this.adminEmail,
        password: this.adminPassword,
      },
    })
    if (created.status !== 201) throw new RunLoadStop('owner_setup_failed')
    this.provisioning.owner_created = true
    progress('Created the first owner of the empty stack.')
  }

  #newAdminSession() {
    return new HttpSession({
      baseURL: this.baseURL,
      label: 'rl-owner',
      metrics: this.metrics,
      requestTimeoutMs: this.config.transport.request_timeout_seconds * 1000,
    })
  }

  // ---------------------------------------------------------------- step 2 to 4

  async signInAdmin() {
    const session = this.#newAdminSession()
    await session.signIn({
      email: this.adminEmail,
      password: this.adminPassword,
      runId: this.runId,
    })
    this.adminSession = session
    this.adminLifecycleSession = await this.lifecycle.registerSession(session, 'run-load owner')
    this.adminActor = actorOf(session, this.adminEmail)
    const capabilities = await session.request('GET', '/v1/capabilities')
    if (capabilities.json()?.features?.agent_kernel !== true) {
      this.provisioning.failures.push('agent_kernel_not_available')
      throw new RunLoadStop('agent_kernel_not_available')
    }
  }

  async provisionAccounts() {
    const { config, provisioning, stage } = this
    progress(`Creating and signing in ${stage.tabs} temporary accounts.`)
    const numbers = Array.from({ length: stage.tabs }, (_, index) => index + 1)
    await mapLimit(numbers, config.provisioning.concurrency, async (number) => {
      if (this.stopped) return
      const email = accountEmail(this.accountPrefix, number)
      try {
        const created = await fetchActorJson(this.adminActor, 'POST', '/v1/admin/users', {
          data: {
            display_name: `Run load ${number}`,
            email,
            instance_role: 'user',
            password: this.userPassword,
          },
          expected: [201],
        })
        const account = { cleanupHandle: null, email, id: created.id, number }
        this.accounts.push(account)
        provisioning.accounts_created += 1
        const tab = new Tab(this, account)
        await tab.signIn()
        provisioning.tabs_signed_in += 1
        this.tabs.push(tab)
      } catch (error) {
        this.metrics.count('provisioning_failures')
        progress(`A temporary account was not provisioned: ${describeFailure(error)}`)
      }
    })
    if (this.tabs.length === 0) throw new RunLoadStop('no_account_provisioned')
  }

  async openTabs() {
    const { config, provisioning } = this
    progress(`Opening ${this.tabs.length} tabs.`)
    await mapLimit(this.tabs, config.provisioning.concurrency, async (tab) => {
      if (this.stopped) return
      try {
        await tab.openPage()
        provisioning.tabs_open += 1
      } catch (error) {
        this.metrics.count('tab_open_failures')
        progress(`A tab did not open: ${describeFailure(error)}`)
      }
    })
    if (provisioning.tabs_open === 0) throw new RunLoadStop('no_tab_open')
  }

  async provisionCollections() {
    const { config, provisioning, stage } = this
    const directory = resolve(REPOSITORY_ROOT, config.provisioning.collection_corpus)
    const names = (await readdir(directory)).filter((name) => name.endsWith('.md')).sort()
    const files = await Promise.all(names.map(async (name) => ({
      name,
      text: await readFile(resolve(directory, name), 'utf8'),
    })))
    const owners = this.tabs.slice(0, stage.collection_users)
    progress(`Creating ${owners.length} collections of ${files.length} documents.`)
    const provision = async (tab, index) => {
      if (this.stopped) return
      try {
        await this.#createCollection(tab, index + 1, files)
        provisioning.collections_ready += 1
      } catch (error) {
        this.metrics.count('collection_failures')
        progress(`A collection was not provisioned: ${describeFailure(error)}`)
      }
    }
    await mapLimit(owners, config.provisioning.concurrency, provision)
  }

  async #createCollection(tab, number, files) {
    const { session } = tab
    const name = `${this.runId} Richtlinien ${number}`
    const created = await session.request('POST', '/v1/knowledge/collections', { body: { name } })
    if (created.status !== 201) throw new Error(`Collection creation returned HTTP ${created.status}.`)
    const id = created.json()?.id
    if (typeof id !== 'string' || !id) throw new Error('Collection creation returned no identifier.')
    const handle = await this.lifecycle.register({
      credential: 'user',
      id,
      kind: 'knowledge_collection',
      name,
      ownerEmail: tab.account.email,
    })
    tab.collection = { handle, id }
    for (const file of files) {
      const job = await session.request('POST', `/v1/knowledge/collections/${id}/document-revisions`, {
        body: {
          metadata: { run_id: this.runId, source: 'verification' },
          text: file.text,
          title: `${this.runId} ${file.name}`,
          workspace_id: session.workspaceId,
        },
      })
      if (job.status !== 202) throw new Error(`Document submission returned HTTP ${job.status}.`)
      await this.#waitForIndexing(session, job.json()?.job_id)
    }
    tab.collectionId = id
  }

  async #waitForIndexing(session, jobId) {
    if (typeof jobId !== 'string' || !jobId) throw new Error('Document submission returned no job.')
    const deadline = performance.now() + this.config.provisioning.indexing_timeout_seconds * 1000
    while (performance.now() < deadline && !this.stopped) {
      const answer = await session.request('GET', `/v1/knowledge/indexing-jobs/${jobId}`)
      const status = answer.json()?.status
      if (status === 'completed') return
      if (status === 'failed' || status === 'cancelled') throw new Error('Indexing did not complete.')
      await this.sleep(INDEXING_POLL_MS)
    }
    throw new Error('Indexing did not finish in time.')
  }

  // ---------------------------------------------------------------- steps 5 to 7

  /** Accounts signed in, no page open: what the idle phase is compared with per tab. */
  async runRest() {
    const start = await this.sampling.snapshot()
    this.enterPhase('rest')
    await this.sleep(this.stage.rest_seconds * 1000)
    this.phases.rest = { end: await this.sampling.snapshot(), start }
    this.enterPhase('provision')
  }

  async runIdle() {
    const start = await this.sampling.snapshot()
    this.enterPhase('idle')
    await this.sleep(this.stage.idle_seconds * 1000)
    const end = await this.sampling.snapshot()
    this.probes.idle_end = await this.sampling.readProbes()
    this.phases.idle = { end, start }
    return end
  }

  #startRoot() {
    this.pendingStarts -= 1
    if (!this.accepting || this.stopped) return
    const plan = planNextRun({ rng: this.mixRng, table: this.table, tabs: this.tabs })
    if (!plan) {
      this.mix.skipped += 1
      return
    }
    this.mix.redraws += plan.redraws
    plan.tab.busy = true
    const run = new RootRun(this, plan.tab, plan.entry, this.nextQuestionNumber())
    this.activeRoots.add(run)
    const driven = run.drive()
    this.rootPromises.add(driven)
    void driven.finally(() => this.rootPromises.delete(driven))
  }

  rootEnded(run) {
    this.activeRoots.delete(run)
  }

  async #directChat() {
    const { config, metrics } = this
    const open = this.tabs.filter((tab) => tab.open)
    if (open.length === 0) return
    const tab = open[this.directRng.int(open.length)]
    const keys = config.direct.questions
    const question = numberedQuestion(
      config.questions[keys[this.questionRng.int(keys.length)]],
      this.nextQuestionNumber(),
    )
    const started = performance.now()
    this.noteSubmit()
    metrics.count('direct_started')
    try {
      const response = await tab.session.openStream('POST', '/v1/chat/completions', {
        body: buildDirectChatRequest(question),
        signal: tab.signal,
      })
      if (!response.ok) {
        await response.body?.cancel()
        metrics.count('direct_rejected')
        return
      }
      let first = true
      let done = false
      const parser = createSseParser((frame) => {
        if (!frame.data) return
        if (first) {
          first = false
          metrics.recordGroup('chat_first_chunk', performance.now() - started)
        }
        if (frame.data === '[DONE]') done = true
      })
      for await (const chunk of response.body) parser.push(chunk)
      parser.end()
      if (done) {
        metrics.count('direct_completed')
      } else {
        metrics.streamDrop('direct_chat', 'ended')
        metrics.count('direct_incomplete')
      }
    } catch (error) {
      if (tab.signal.aborted) return
      metrics.streamDrop('direct_chat', failureKind(error))
      metrics.count('direct_incomplete')
    }
  }

  /** The open stream of direct chats: the rate follows the ramp and counts no root. */
  async #directChatLoop(startedAt) {
    const { config, stage } = this
    while (this.accepting && !this.stopped) {
      const elapsed = (performance.now() - startedAt) / 1000
      const fraction = Math.min(1, elapsed / stage.ramp_seconds)
      const open = this.tabs.filter((tab) => tab.open).length
      const wait = nextArrivalSeconds(this.directRng, config.direct.rate_per_user_hour, open * fraction)
      // Arrivals have no memory, so a long draw can be drawn again at the new rate.
      if (wait === null || wait > 5) {
        await this.sleep(5_000)
        continue
      }
      await this.sleep(wait * 1000)
      if (!this.accepting || this.stopped) return
      const chat = this.#directChat()
      this.directInFlight.add(chat)
      void chat.finally(() => this.directInFlight.delete(chat))
    }
  }

  async runRampAndPlateau(rampStart) {
    const { config, stage } = this
    this.enterPhase('ramp')
    // Indexing the collections reached the stand-in too; only runs count from here.
    this.watchFrom = rampStart.stand_in ?? this.standInBefore
    this.workloadRan = true
    this.accepting = true
    const startedAt = performance.now()
    const stopAt = startedAt + (stage.ramp_seconds + stage.plateau_seconds) * 1000
    const directLoop = this.#directChatLoop(startedAt)
    let rampEnd = null
    let lastReport = startedAt
    let workerStop = null
    let reloadWave = null
    while (!this.stopped && performance.now() < stopAt) {
      const elapsed = (performance.now() - startedAt) / 1000
      if (rampEnd === null && elapsed >= stage.ramp_seconds) {
        this.enterPhase('plateau')
        rampEnd = this.sampling.snapshot()
      }
      if (rampEnd !== null) this.activeRootGauge.record(this.activeRoots.size)
      if (this.fault && !workerStop && elapsed >= stage.ramp_seconds + stage.plateau_seconds / 2) {
        workerStop = this.#stopOneWorker()
      }
      if (this.reloadWave && !reloadWave && elapsed >= stage.ramp_seconds + stage.plateau_seconds / 2) {
        reloadWave = this.#reloadOpenTabs()
      }
      const desired = desiredActiveRoots(elapsed, stage.ramp_seconds, stage.roots)
      while (this.activeRoots.size + this.pendingStarts < desired) {
        this.pendingStarts += 1
        const waitMs = this.startRng.between(
          config.between_runs_seconds.min,
          config.between_runs_seconds.max,
        ) * 1000
        const timer = setTimeout(() => {
          this.startTimers.delete(timer)
          this.#startRoot()
        }, waitMs)
        this.startTimers.add(timer)
      }
      if (performance.now() - lastReport >= 60_000) {
        lastReport = performance.now()
        progress(
          `active=${this.activeRoots.size} started=${this.metrics.counter('runs_started')} `
          + `completed=${this.metrics.counter('runs_completed')} failed=${this.metrics.counter('runs_failed')}`,
        )
      }
      await this.sleep(500)
    }
    this.accepting = false
    this.enterPhase('decay')
    const plateauEnd = this.sampling.snapshot()
    const plateauProbes = this.sampling.readProbes()
    await directLoop
    await workerStop
    await reloadWave
    const rampEndValue = rampEnd === null ? await plateauEnd : await rampEnd
    this.phases.ramp = { end: rampEndValue, start: rampStart }
    if (rampEnd !== null) this.phases.plateau = { end: await plateauEnd, start: rampEndValue }
    this.probes.plateau_end = await plateauProbes
    return await plateauEnd
  }

  async runDecay(decayStart) {
    const { config, stage } = this
    const deadline = performance.now() + stage.decay_max_seconds * 1000
    while (this.activeRoots.size > 0 && performance.now() < deadline && !this.stopped) {
      await this.sleep(1_000)
    }
    const remaining = [...this.activeRoots]
    if (remaining.length > 0) {
      progress(`Cancelling ${remaining.length} runs still active after the decay.`)
      await mapLimit(remaining, config.provisioning.concurrency, (run) => run.cancelAsUnfinished())
      const settleUntil = performance.now() + config.decay_cancel_settle_seconds * 1000
      while (this.activeRoots.size > 0 && performance.now() < settleUntil) {
        await delay(500)
      }
      for (const run of this.activeRoots) run.abandon()
    }
    // A chat that hangs ends with its tab; it must not hold up the measurement.
    await Promise.race([
      Promise.allSettled([...this.rootPromises, ...this.directInFlight]),
      delay(config.decay_cancel_settle_seconds * 1000),
    ])
    this.phases.decay = { end: await this.sampling.snapshot(), start: decayStart }
  }

  // ---------------------------------------------------------------- step 8

  /**
   * How long cancelled runs hold their provider calls, with nothing else running.
   * Several runs at once give a spread instead of one number: what counts is how
   * soon the calls in flight at the moment of the cancel are gone. It waits for the
   * searches, because those are what a run holds longest.
   */
  async runCancelProbe() {
    const { cancel_probe: probe } = this.config
    const { sampling } = this
    const tabs = this.tabs.filter((candidate) => candidate.open).slice(0, probe.runs)
    if (tabs.length === 0) return { reason: 'no_open_tab', status: NOT_MEASURED }
    const quietUntil = performance.now() + probe.quiet_wait_seconds * 1000
    for (;;) {
      const stats = await sampling.standIn('/admin/stats')
      if (stats?.in_flight === 0) break
      if (performance.now() >= quietUntil || this.stopped) return { reason: 'stand_in_never_quiet', status: NOT_MEASURED }
      await this.sleep(probe.poll_ms)
    }
    const entry = this.table.find((row) => row.id === probe.scenario)
    const submitted = await Promise.all(tabs.map(async (tab) => {
      const answer = await tab.session.request('POST', '/v1/runs', {
        body: buildRunRequest({
          collectionId: tab.collectionId,
          question: numberedQuestion(this.config.questions[entry.questions[0]], this.nextQuestionNumber()),
          scenario: entry,
          sessionId: sessionIdFor(entry.mode, randomUUID()),
          workspaceId: tab.session.workspaceId,
        }),
      })
      const runId = answer.json()?.run_id
      return answer.status === 202 && typeof runId === 'string' ? { runId, tab } : null
    }))
    const runs = submitted.filter(Boolean)
    if (runs.length === 0) return { reason: 'no_run_accepted', status: NOT_MEASURED }
    const wanted = runs.length * probe.min_searches_per_run
    const callUntil = performance.now() + probe.provider_call_wait_seconds * 1000
    let before = null
    while (performance.now() < callUntil && !this.stopped) {
      before = await sampling.standIn('/admin/stats')
      if ((before?.in_flight_per_operation?.[probe.operation] ?? 0) >= wanted) break
      await this.sleep(probe.poll_ms)
    }
    const cancelledAt = performance.now()
    await Promise.all(runs.map(({ runId, tab }) => (
      tab.session.request('POST', `/v1/runs/${runId}/cancel`, { body: {} })
    )))
    if (!(before?.in_flight > 0)) return { reason: 'no_provider_call_seen', status: NOT_MEASURED }
    const drainUntil = cancelledAt + probe.drain_wait_seconds * 1000
    const readings = []
    let after = null
    while (performance.now() < drainUntil) {
      after = await sampling.standIn('/admin/stats')
      if (Number.isFinite(after?.in_flight)) {
        readings.push({ in_flight: after.in_flight, seconds: (performance.now() - cancelledAt) / 1000 })
        if (after.in_flight === 0) break
      }
      await delay(probe.poll_ms)
    }
    // The accounts cannot be erased cleanly while these runs still wind down.
    const statuses = {}
    await Promise.all(runs.map(async ({ runId, tab }) => {
      let status = 'unknown'
      while (performance.now() < drainUntil) {
        const summary = await tab.session.request('GET', `/v1/runs/${runId}`)
        status = summary.json()?.status ?? status
        if (['cancelled', 'completed', 'failed'].includes(status)) break
        await delay(probe.poll_ms)
      }
      statuses[status] = (statuses[status] ?? 0) + 1
    }))
    return {
      disconnected: (after?.disconnected ?? 0) - (before.disconnected ?? 0),
      in_flight_at_cancel: before.in_flight,
      run_statuses: statuses,
      runs: runs.length,
      scenario: probe.scenario,
      searches_at_cancel: before.in_flight_per_operation?.[probe.operation] ?? 0,
      ...cancelSpread(readings, before.in_flight),
    }
  }

  // ---------------------------------------------------------------- step 9

  async harvestRunData() {
    if (this.accounts.length === 0 || !this.sampling.psql) return
    try {
      const ids = this.accounts.map((account) => account.id).filter(Boolean)
      this.runData = await this.sampling.psql.queryJson(runDataSql(ids))
    } catch (error) {
      progress(`The run data could not be read: ${describeFailure(error)}`)
    }
  }

  /** Close every tab, delete what was created, erase the accounts, count what remains. */
  async cleanUp() {
    this.enterPhase('cleanup')
    this.sampling.stop()
    for (const tab of this.tabs) tab.close()
    let failures = 0
    const { concurrency } = this.config.provisioning
    const withCollections = this.tabs.filter((tab) => tab.collection)
    await mapLimit(withCollections, concurrency, async (tab) => {
      const cleaned = await attemptCleanup('collection', () => this.#deleteCollection(tab))
      if (!cleaned) failures += 1
    })
    if (this.adminActor) {
      await mapLimit(this.accounts, concurrency, async (account) => {
        const erased = await attemptCleanup(
          'temporary account',
          () => eraseTemporaryUser(this.adminActor, account, this.lifecycle),
        )
        if (!erased) failures += 1
      })
    }
    // Before the owner signed in nothing could be created, and nothing can be counted.
    const residue = this.adminSession ? await this.#countResidue(failures) : null
    if (this.adminSession) {
      await attemptCleanup('admin logout', async () => {
        const answer = await this.adminSession.request('POST', '/api/auth/logout')
        if (![200, 401].includes(answer.status)) {
          throw new Error(`Admin logout returned HTTP ${answer.status}.`)
        }
        await this.lifecycle.completeSession(this.adminLifecycleSession)
      })
    }
    return residue
  }

  async #deleteCollection(tab) {
    const { session } = tab
    const { handle, id } = tab.collection
    const answer = await session.request('DELETE', `/v1/knowledge/collections/${id}`)
    if (![202, 204, 404].includes(answer.status)) {
      throw new Error(`Collection deletion returned HTTP ${answer.status}.`)
    }
    if (answer.status === 202) {
      const operationId = answer.json()?.operation_id
      if (typeof operationId !== 'string') throw new Error('Accepted deletion carries no operation.')
      const deadline = performance.now() + DELETION_TIMEOUT_MS
      for (;;) {
        const current = await session.request('GET', `/v1/deletion-operations/${operationId}`)
        const status = current.json()?.status
        if (current.status === 404 || status === 'deleted') break
        if (status === 'delete_failed') throw new Error('Collection deletion failed.')
        if (performance.now() >= deadline) throw new Error('Collection deletion did not finish in time.')
        await delay(DELETION_POLL_MS)
      }
    }
    await this.lifecycle.complete(handle)
    tab.collection = null
  }

  async #countResidue(cleanupFailures) {
    let accounts = null
    try {
      const listed = await this.adminSession.request('GET', '/v1/admin/users')
      const users = listed.status === 200 ? listed.json()?.users : null
      if (Array.isArray(users)) {
        accounts = users.filter((user) => accountBelongsToPrefix(user?.email, this.accountPrefix)).length
      }
    } catch (error) {
      progress(`The remaining accounts could not be counted: ${describeFailure(error)}`)
    }
    const ids = this.accounts.map((account) => account.id).filter(Boolean)
    const tables = {}
    if (ids.length > 0 && this.sampling.psql) {
      try {
        for (const [table, column] of RESIDUE_TABLES) {
          tables[table] = await this.sampling.psql.queryNumber(residueSql(table, column, ids))
        }
      } catch (error) {
        progress(`The remaining rows could not be counted: ${describeFailure(error)}`)
      }
    }
    return judgeResidue({ accounts, cleanupFailures, tables })
  }

  /**
   * Half way through the plateau the open tabs load their page again in a wave, as
   * after a deployment or at the start of a working day, while the runs go on.
   */
  async #reloadOpenTabs() {
    const tabs = this.tabs.filter((tab) => tab.open)
    const failedBefore = this.metrics.counter('tab_read_failures')
    const startedAt = performance.now()
    progress(`Reloading ${tabs.length} tabs, ${this.reloadWave} at a time.`)
    await mapLimit(tabs, this.reloadWave, (tab) => tab.reloadPage())
    this.reloadWaveRecord = {
      failed_reads: this.metrics.counter('tab_read_failures') - failedBefore,
      seconds: Math.round((performance.now() - startedAt) / 100) / 10,
      tabs: tabs.length,
      width: this.reloadWave,
    }
    progress(`Reload wave done in ${this.reloadWaveRecord.seconds} s.`)
  }

  /**
   * Half way through the plateau one worker gets SIGTERM, is killed after the grace
   * time and comes back. Its runs have to reach another worker through heartbeat
   * silence; what that takes is read when the decay has ended.
   */
  async #stopOneWorker() {
    const [worker] = this.sampling.service('worker')
    const record = {
      active_roots_at_stop: this.activeRoots.size,
      kind: WORKER_STOP.name,
      restarted: false,
      runs_failed_at_stop: this.metrics.counter('runs_failed'),
      since: new Date().toISOString(),
      stopped: false,
      worker: worker?.name ?? null,
    }
    this.faultRecord = record
    if (!worker) return
    progress(`Stopping ${worker.name} with ${record.active_roots_at_stop} active root runs.`)
    const control = async (args, timeoutSeconds) => {
      const result = await runContainerControlCommand(
        this.sampling.command,
        args,
        REPOSITORY_ROOT,
        null,
        timeoutSeconds * 1000,
      ).catch(() => null)
      return result?.exitCode === 0
    }
    record.stopped = await control(
      ['stop', '--time', String(WORKER_STOP.graceSeconds), worker.id],
      WORKER_STOP.graceSeconds + 30,
    )
    await delay(WORKER_STOP.downSeconds * 1000)
    record.restarted = await control(['start', worker.id], 60)
    progress(`${worker.name} stopped: ${record.stopped}, back: ${record.restarted}.`)
  }

  /** Lines of one container's log since a moment that hold a text, read as a stream. */
  #countLogLines(container, since, text) {
    return new Promise((resolvePromise) => {
      const child = spawn(this.sampling.command, ['logs', '--since', since, container.id], {
        cwd: REPOSITORY_ROOT,
        env: process.env,
        shell: false,
        stdio: ['ignore', 'pipe', 'pipe'],
      })
      let count = 0
      const countLines = (stream) => createInterface({ input: stream }).on('line', (line) => {
        if (line.includes(text)) count += 1
      })
      countLines(child.stdout)
      countLines(child.stderr)
      child.once('error', () => resolvePromise(null))
      child.once('close', (exitCode) => resolvePromise(exitCode === 0 ? count : null))
    })
  }

  /** The takeovers after the stop, from the workers' own lines. Counts only. */
  async #judgeFault() {
    if (!this.faultRecord) return null
    const record = this.faultRecord
    const workers = this.sampling.containers.filter((row) => row.service === 'worker')
    const counts = await Promise.all(workers.map((worker) => (
      this.#countLogLines(worker, record.since, 'is taking over job')
    )))
    const takeovers = counts.includes(null)
      ? NOT_MEASURED
      : counts.reduce((sum, count) => sum + count, 0)
    const unfinished = this.metrics.counter('runs_unfinished')
    const failedAfterStop = this.metrics.counter('runs_failed') - record.runs_failed_at_stop
    return {
      ...record,
      failed_after_stop: failedAfterStop,
      takeovers,
      unfinished,
      ...judgeWorkerStop({
        activeRootsAtStop: record.active_roots_at_stop,
        failedAfterStop,
        restarted: record.restarted,
        stopped: record.stopped,
        takeovers,
        unfinished,
      }),
    }
  }

  async #judgeBaseline(report) {
    if (!this.baselinePath) return null
    try {
      const document = JSON.parse(await readFile(resolve(REPOSITORY_ROOT, this.baselinePath), 'utf8'))
      return judgeBaseline(report.comparison, normalizeBaseline(document))
    } catch {
      return { rules: [{ id: 'baseline_readable', status: 'failed' }], status: 'failed' }
    }
  }

  async finishReport(residue, clientLoop, startedAt) {
    const { provisioning } = this
    const complete = provisioning.preflight_passed
      && provisioning.samplers_running
      && provisioning.accounts_created === provisioning.accounts_wanted
      && provisioning.tabs_open === provisioning.accounts_wanted
      && provisioning.collections_ready === provisioning.collections_wanted
    // From the first run on: what indexing the collections cost says nothing about runs.
    const coverage = this.workloadRan ? standInCoverage(this.watchFrom, this.standInAfter) : null
    const input = {
      cancelProbe: this.cancelProbeResult,
      clientLoop,
      clientRssMax: this.sampling.rssMax,
      coverage,
      finishedAt: new Date().toISOString(),
      metrics: this.metrics,
      mix: this.mix,
      phases: Object.fromEntries(['rest', 'idle', 'ramp', 'plateau', 'decay'].map((name) => [name, {
        ...this.phases[name],
        gauges: this.sampling.gauges.get(name).summary(),
      }])),
      activeRoots: this.activeRootGauge.n > 0
        ? { max: this.activeRootGauge.max, median: this.activeRootGauge.median(), target: this.stage.roots }
        : null,
      limits: this.config.limits,
      loadMatrix: this.config.load_matrix,
      setup: {
        config_digest: this.configDigest,
        containers_by_service: this.stack.containers_by_service,
        latency_scale: this.stack.stand_in?.latency_scale,
        selection: this.stack.stand_in?.selection,
        settings: this.stack.settings,
        ...(this.fault ? { fault: WORKER_STOP.name } : {}),
        ...(this.reloadWave ? { reload_wave: this.reloadWave } : {}),
      },
      probes: this.probes,
      provisioning: { ...provisioning, complete },
      residue,
      runData: this.runData,
      runId: this.runId,
      samplers: this.sampling.facts(),
      stack: this.stack,
      stage: this.stageName,
      startedAt,
      stopReason: this.stopReason,
    }
    const report = buildReport(input)
    const baseline = await this.#judgeBaseline(report)
    if (baseline) report.baseline = baseline
    if (this.reloadWaveRecord) report.reload_wave = this.reloadWaveRecord
    const fault = await this.#judgeFault()
    if (fault) report.fault = fault
    const ratio = report.runs.success_ratio
    const counter = (name) => this.metrics.counter(name)
    const dropsOf = (stream) => Object.entries(report.client.total.stream_drops)
      .filter(([key]) => key.startsWith(`${stream}:`))
      .reduce((sum, [, count]) => sum + count, 0)
    report.load_applied = judgeLoadApplied({
      activeRootsMedian: report.active_roots?.median,
      directFailures: counter('direct_rejected') + counter('direct_incomplete'),
      directStarted: counter('direct_started'),
      httpFailures: report.http.failures,
      phaseOverruns: phaseOverruns(report, this.stage),
      rejected: report.runs.rejected,
      requests: report.http.requests,
      resultsUnreadable: counter('results_unreadable'),
      runStreamDrops: dropsOf('run_events'),
      runStreams: counter('run_streams_opened'),
      started: report.runs.started,
      stopReason: this.stopReason,
      strayFailures: counter('engine_stray_failures'),
      tabsOpen: provisioning.tabs_open,
      tabsSignedOut: counter('tabs_signed_out'),
      tabsWithStream: this.tabs.filter((tab) => tab.ready).length,
      targetRoots: this.stage.roots,
      userStreamDrops: dropsOf('user_events'),
    }, this.config.load_applied, this.stage.min_active_root_share)
    for (const check of report.load_applied.checks) {
      if (!check.passed) progress(`Load not applied: ${check.id} ${check.value}, limit ${check.limit}.`)
    }
    for (const limit of report.limits) {
      if (limit.exceeded === true) progress(`Above a starting limit: ${limit.id} ${limit.value}, limit ${limit.limit}.`)
    }
    for (const rule of baseline?.warnings ?? []) progress(`Slower than the base, not red: ${rule}.`)
    const connectionBudget = this.workloadRan ? judgeConnectionBudget(report) : null
    report.connection_budget = connectionBudget ?? { status: 'not_run' }
    for (const rule of connectionBudget?.rules ?? []) {
      if (rule.status !== 'passed') progress(`Connection budget, ${rule.status}: ${rule.id} ${rule.current ?? rule.reason}, limit ${rule.limit}.`)
    }
    const scenarios = judgeScenarios({
      baseline,
      connectionBudget,
      coverage,
      events: report.events_delivery,
      fault,
      loadApplied: report.load_applied,
      provisioned: complete,
      residue,
      successRatioValue: ratio,
      workloadRan: this.workloadRan,
    })
    const written = { id: SCENARIO.report, status: 'passed' }
    report.scenarios = [...scenarios, written]
    try {
      const text = `${JSON.stringify(report, null, 2)}\n`
      const needles = {
        'administrator password': this.adminPassword,
        'clarification answer': this.config.clarification_answer,
        'temporary password': this.userPassword,
      }
      for (const [key, question] of Object.entries(this.config.questions)) {
        needles[`question ${key}`] = question
      }
      const forbidden = findForbiddenContent(text, needles)
      if (forbidden.length > 0) {
        throw new RunLoadStop(`The report holds forbidden content: ${forbidden.join(', ')}.`)
      }
      const path = resolve(this.reportDirectory, 'run-load-report.json')
      const temporary = `${path}.tmp`
      await writeFile(temporary, text, { encoding: 'utf8', mode: 0o600 })
      await rename(temporary, path)
      await chmod(path, 0o600)
      if (parseJson(await readFile(path, 'utf8'))?.run_id !== this.runId) {
        throw new RunLoadStop('The report did not read back.')
      }
    } catch (error) {
      written.status = 'failed'
      progress(`The report was not written: ${error instanceof RunLoadStop ? error.reason : describeFailure(error)}`)
    }
    await writeScenarioSidecar(report.scenarios, process.env)
    for (const scenario of report.scenarios) progress(`${scenario.id}: ${scenario.status}`)
    process.stdout.write(`${formatSummaryLine(report)}\n`)
    return report.scenarios.every((scenario) => scenario.status === 'passed')
  }
}

// ------------------------------------------------------------------ entry point

async function main() {
  const stageName = requiredEnvironment('INQTRIX_RUN_LOAD_STAGE')
  if (!STAGE_NAMES.includes(stageName)) throw new Error('INQTRIX_RUN_LOAD_STAGE is unknown.')
  const runId = assertVerificationRunId(requiredEnvironment('INQTRIX_VERIFICATION_RUN_ID'))
  const prefix = requiredEnvironment('INQTRIX_RUN_LOAD_ACCOUNT_PREFIX')
  if (!/^inqv-rl-[a-z0-9-]+-[0-9a-f]{16}-u$/.test(prefix)) {
    throw new Error('INQTRIX_RUN_LOAD_ACCOUNT_PREFIX is not bound to a run.')
  }
  const configText = await readFile(SCENARIOS_PATH, 'utf8')
  const config = validateConfig(JSON.parse(configText))
  const reportDirectory = requiredEnvironment('INQTRIX_VERIFICATION_REPORT_DIR')
  const lifecycle = oneLifecycleCallAtATime(
    new VerificationLifecycleClient({ reportDirectory, runId }),
  )
  const engine = new Engine({
    adminEmail: requiredEnvironment('INQTRIX_E2E_ADMIN_EMAIL'),
    adminPassword: requiredEnvironment('INQTRIX_E2E_ADMIN_PASSWORD'),
    baseURL: normalizeLoadSmokeBaseURL(process.env.INQTRIX_E2E_BASE_URL),
    baselinePath: process.env.INQTRIX_RUN_LOAD_BASELINE?.trim() || null,
    fault: process.env.INQTRIX_RUN_LOAD_FAULT?.trim() === WORKER_STOP.name,
    reloadWave: reloadWaveWidth(process.env.INQTRIX_RUN_LOAD_RELOAD_WAVE),
    command: process.env.INQTRIX_RUN_LOAD_ENGINE?.trim() || 'podman',
    config,
    configDigest: createHash('sha256').update(configText).digest('hex').slice(0, 16),
    lifecycle,
    prefix,
    project: process.env.INQTRIX_RUN_LOAD_PROJECT?.trim() || 'inqtrix-loadtest',
    reportDirectory,
    runId,
    stageName,
    userPassword: requiredEnvironment('INQTRIX_E2E_USER_PASSWORD'),
  })
  const startedAt = new Date().toISOString()
  const loop = monitorEventLoopDelay({ resolution: LOOP_RESOLUTION_MS })
  loop.enable()
  const interrupt = () => engine.requestStop('interrupted')
  process.once('SIGINT', interrupt)
  process.once('SIGTERM', interrupt)
  // One tab's stray failure is a count in the report, not the end of the run.
  const survive = (error) => {
    engine.metrics.count('engine_stray_failures')
    progress(`A stray failure: ${describeFailure(error)}`)
  }
  process.on('uncaughtException', survive)
  process.on('unhandledRejection', survive)

  let residue = null
  let passed = false
  try {
    try {
      await engine.proveStack()
      await engine.ensureOwner()
      await engine.signInAdmin()
      await engine.provisionAccounts()
      await engine.provisionCollections()
      await engine.runRest()
      await engine.openTabs()
      const idleEnd = await engine.runIdle()
      if (!engine.stopped) {
        const plateauEnd = await engine.runRampAndPlateau(idleEnd)
        await engine.runDecay(plateauEnd)
      }
      if (!engine.stopped) {
        engine.enterPhase('cancel_probe')
        engine.cancelProbeResult = await engine.runCancelProbe().catch((error) => ({
          reason: describeFailure(error),
          status: NOT_MEASURED,
        }))
      }
    } catch (error) {
      const reason = error instanceof RunLoadStop ? error.reason : describeFailure(error)
      engine.stopReason ??= reason
      progress(`Stopped early: ${reason}`)
    }
    engine.probes.final = await engine.sampling.readProbes()
    engine.standInAfter = await engine.sampling.standIn('/admin/stats')
    await engine.harvestRunData()
    residue = await engine.cleanUp()
  } finally {
    loop.disable()
    // The monitor's own timer interval is in every value it records.
    const stall = (nanoseconds) => Math.max(0, Math.round(nanoseconds / 1e5 - LOOP_RESOLUTION_MS * 10) / 10)
    const clientLoop = {
      max: stall(loop.max),
      p50: stall(loop.percentile(50)),
      p99: stall(loop.percentile(99)),
    }
    try {
      passed = await engine.finishReport(residue, clientLoop, startedAt)
    } finally {
      engine.sampling.psql?.close()
      lifecycle.close()
    }
  }
  process.exitCode = passed ? 0 : 1
  // A socket or timer left behind must not keep the orchestrator waiting.
  setTimeout(() => process.exit(process.exitCode), 5_000).unref()
}

main().catch((error) => {
  process.stderr.write(`[run-load] The engine failed: ${describeFailure(error)}\n`)
  process.exitCode = 1
  setTimeout(() => process.exit(1), 1_000).unref()
})

