/**
 * Pure logic of the run-load engine: no network, no containers, no clock of its
 * own. Everything that decides a measurement or a verdict lives here so it can
 * be tested without a stack.
 */

export const STAGE_NAMES = Object.freeze(['smoke', 'l05', 'l1', 'l2'])
export const NOT_MEASURED = 'not_measured'

/** How many tabs a reload wave opens at once; anything but a positive whole number asks for none. */
export function reloadWaveWidth(value) {
  const width = Number(value)
  return Number.isInteger(width) && width > 0 ? width : 0
}

export const SCENARIO = Object.freeze({
  baseline: 'run-load.baseline-holds',
  connectionBudget: 'run-load.connection-budget',
  events: 'run-load.events-complete',
  loadApplied: 'run-load.load-applied',
  provisioned: 'run-load.provisioned',
  residue: 'run-load.no-residue',
  report: 'run-load.report-written',
  standIn: 'run-load.stand-in-covered',
  workerStop: 'run-load.worker-stop',
  workload: 'run-load.workload-completed',
})

/**
 * The one disturbance of the plateau so far: one worker gets SIGTERM half way through,
 * is killed after the grace time and comes back a little later. No capacity assumption,
 * so these numbers stay out of the scenario file and a run's setup digest.
 */
export const WORKER_STOP = Object.freeze({
  downSeconds: 30,
  graceSeconds: 10,
  name: 'worker-stop',
})

/** Plausibility of the workload, not an acceptance limit: below it the run
 * mostly measured failures, so its other numbers do not describe real work. */
export const MIN_SUCCESS_RATIO = 0.9

/**
 * The share of provider calls the stand-in may answer without a recording.
 *
 * Not zero: a text that was edited on the way can carry evidence markers from
 * elsewhere, and that sends a run down repair paths no recording contains. The
 * stand-in answers those neutrally and counts them, which keeps the run going
 * and is honest about it. A small share therefore belongs to normal operation.
 * Above it the workload has left what the recordings cover, and the measured
 * fan-out and timing no longer describe the real system.
 */
export const MAX_UNMATCHED_SHARE = 0.02

/** More transactions per provider call than this share above the base is a regression. */
export const BASELINE_TRANSACTION_TOLERANCE = 0.1
/** A thread that grows with the load is a leak in waiting, not a matter of scatter. */
export const BASELINE_THREAD_TOLERANCE = 0.1
// The processes whose thread pools this code decides. Node's and Postgres's counts
// move on their own: the collaboration service stood at 13 and at 20 in equal runs.
const THREAD_RULE_SERVICES = ['api', 'worker']
/** Two reports can land one histogram bucket apart without anything having changed. */
export const MIN_TIME_TOLERANCE_SHARE = 0.04
/** Four runs of the same code put the median of the delivery delay within two buckets. */
export const MIN_MEDIAN_TOLERANCE_SHARE = 0.08
/** The capacity model's limit for the delivery delay. */
export const SSE_LAG_P95_LIMIT_MS = 1000
/** How much longer than planned a phase may take before it no longer counts as measured. */
export const MAX_PHASE_OVERRUN_SHARE = 0.05
/** An event-loop stall this long is felt by every request of the process. */
export const LOOP_LAG_P99_LIMIT_MS = 50

export const TERMINAL_EVENT_TYPES = new Set([
  'inqtrix.run.cancelled',
  'inqtrix.run.completed',
  'inqtrix.run.failed',
])
export const STREAM_LIVE_MARKER = 'inqtrix.stream.live'
export const CHILD_PROGRESS_EVENT = 'inqtrix.agent.child.progress'
/** The statuses in which a run waits for a person and sends nothing. */
export const PARKED_STATUSES = new Set(['waiting_for_approval', 'waiting_for_input'])
/** The limit gate's stable option for keeping what the run has so far. */
export const LIMIT_PARTIAL_OPTION = 'partial'

const COMPOSE_PROJECT = 'com.docker.compose.project'
const COMPOSE_SERVICE = 'com.docker.compose.service'
const ACCOUNT_DOMAIN = 'example.invalid'
const ACCOUNT_SUFFIX = /^\d{1,4}@example\.invalid$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

// ---------------------------------------------------------------- configuration

export function validateConfig(config) {
  const errors = []
  const positive = (path, value) => {
    if (!(Number.isFinite(value) && value > 0)) errors.push(`${path} must be a positive number`)
  }
  for (const name of STAGE_NAMES) {
    const stage = config?.stages?.[name]
    if (!stage) {
      errors.push(`stages.${name} is missing`)
      continue
    }
    for (const field of [
      'tabs',
      'roots',
      'rest_seconds',
      'idle_seconds',
      'ramp_seconds',
      'plateau_seconds',
      'decay_max_seconds',
    ]) positive(`stages.${name}.${field}`, stage[field])
    if (stage.roots > stage.tabs) errors.push(`stages.${name}.roots exceeds its tabs`)
    if (
      !Number.isInteger(stage.collection_users)
      || stage.collection_users < 1
      || stage.collection_users > stage.tabs
    ) errors.push(`stages.${name}.collection_users must lie between 1 and its tabs`)
  }
  for (const [name, family] of Object.entries(config?.families ?? {})) {
    positive(`families.${name}.rate_per_user_hour`, family?.rate_per_user_hour)
  }
  const shares = new Map()
  for (const [id, scenario] of Object.entries(config?.scenarios ?? {})) {
    if (!config?.families?.[scenario.family]) errors.push(`scenarios.${id}.family is unknown`)
    positive(`scenarios.${id}.share`, scenario.share)
    shares.set(scenario.family, (shares.get(scenario.family) ?? 0) + scenario.share)
    if (!['agent_kernel', 'knowledge', 'research'].includes(scenario.mode)) {
      errors.push(`scenarios.${id}.mode is unknown`)
    }
    questionsOf(`scenarios.${id}`, scenario.questions, config, errors)
  }
  if (shares.size === 0) errors.push('scenarios is empty')
  for (const [family, share] of shares) {
    if (Math.abs(share - 1) > 1e-9) errors.push(`the shares of family ${family} do not sum to 1`)
  }
  positive('direct.rate_per_user_hour', config?.direct?.rate_per_user_hour)
  questionsOf('direct', config?.direct?.questions, config, errors)
  if (typeof config?.clarification_answer !== 'string' || !config.clarification_answer.trim()) {
    errors.push('clarification_answer is missing')
  }
  positive('think.min_seconds', config?.think?.min_seconds)
  positive('think.late_seconds', config?.think?.late_seconds)
  if (!(config?.think?.max_seconds >= config?.think?.min_seconds)) {
    errors.push('think.max_seconds is below its minimum')
  }
  if (!(config?.think?.late_share >= 0 && config?.think?.late_share <= 1)) {
    errors.push('think.late_share must lie between 0 and 1')
  }
  if (!(config?.provisioning?.concurrency >= 1 && config?.provisioning?.concurrency <= 8)) {
    errors.push('provisioning.concurrency must lie between 1 and 8')
  }
  for (const entry of config?.page_load ?? []) {
    if (!/^(?:GET|PUT) \/\S*$/.test(entry)) errors.push(`page_load entry is invalid: ${entry}`)
  }
  if (!config?.scenarios?.[config?.cancel_probe?.scenario]) {
    errors.push('cancel_probe.scenario is unknown')
  }
  const standIn = config?.stand_in
  for (const [service, variables] of Object.entries(config?.setup_settings ?? {})) {
    for (const variable of variables) {
      if (!/^[A-Z][A-Z0-9_]*$/.test(variable)) errors.push(`setup_settings.${service} holds an invalid name`)
    }
  }
  for (const [path, list] of [
    ['stand_in.wired_services', standIn?.wired_services],
    ['stand_in.endpoint_variables', standIn?.endpoint_variables],
  ]) {
    if (!Array.isArray(list) || list.length === 0) errors.push(`${path} is empty`)
  }
  if (Object.keys(standIn?.provider_variables ?? {}).length === 0) {
    errors.push('stand_in.provider_variables is empty')
  }
  const loadApplied = config?.load_applied
  for (const field of [
    'min_active_root_share',
    'max_rejected_share',
    'max_http_failure_share',
    'max_direct_failure_share',
    'max_stream_drop_share',
  ]) {
    if (!(loadApplied?.[field] >= 0 && loadApplied?.[field] <= 1)) {
      errors.push(`load_applied.${field} must lie between 0 and 1`)
    }
  }
  for (const variable of [
    ...(standIn?.endpoint_variables ?? []),
    ...Object.keys(standIn?.provider_variables ?? {}),
  ]) {
    if (!/^[A-Z][A-Z0-9_]*$/.test(variable)) errors.push(`stand_in variable is invalid: ${variable}`)
  }
  if (!/^[a-z]+:\/\/[a-z0-9.-]+:$/.test(standIn?.endpoint_prefix ?? '')) {
    errors.push('stand_in.endpoint_prefix is invalid')
  }
  for (const value of Object.values(standIn?.provider_variables ?? {})) {
    if (!/^[a-z0-9_-]+$/.test(String(value))) errors.push('stand_in.provider_variables holds an invalid value')
  }
  if (errors.length > 0) throw new Error(`The run-load scenarios are invalid: ${errors.join('; ')}.`)
  return config
}

function questionsOf(path, questions, config, errors) {
  if (!Array.isArray(questions) || questions.length === 0) {
    errors.push(`${path}.questions is empty`)
    return
  }
  for (const key of questions) {
    if (typeof config?.questions?.[key] !== 'string' || !config.questions[key].trim()) {
      errors.push(`${path} names an unknown question: ${key}`)
    }
  }
}

// ------------------------------------------------------------------ seeded choice

/** A small seeded generator, so that one seed reproduces one workload. */
export function createRng(seed) {
  let state = seed >>> 0
  const next = () => {
    state = (state + 0x6d2b79f5) >>> 0
    let mixed = state
    mixed = Math.imul(mixed ^ (mixed >>> 15), mixed | 1)
    mixed ^= mixed + Math.imul(mixed ^ (mixed >>> 7), mixed | 61)
    return ((mixed ^ (mixed >>> 14)) >>> 0) / 4294967296
  }
  return {
    between: (low, high) => low + next() * (high - low),
    int: (limit) => Math.floor(next() * limit),
    next,
  }
}

/** Each scenario's weight is its family's rate times its share of that family. */
export function buildMixTable(config) {
  const entries = Object.entries(config.scenarios).map(([id, scenario]) => ({
    ...scenario,
    id,
    weight: config.families[scenario.family].rate_per_user_hour * scenario.share,
  }))
  const total = entries.reduce((sum, entry) => sum + entry.weight, 0)
  return entries.map((entry) => ({ ...entry, planned_share: entry.weight / total }))
}

export function pickWeighted(rng, table) {
  const total = table.reduce((sum, entry) => sum + entry.weight, 0)
  let point = rng.next() * total
  for (const entry of table) {
    point -= entry.weight
    if (point < 0) return entry
  }
  return table.at(-1)
}

/**
 * The next root run: a scenario by the mix, then a random open tab without an
 * active run that can serve it. A scenario nobody can serve right now is drawn
 * again and the redraw counted, so a skew in the mix stays visible. A pick takes
 * the same draws however many tabs are free: who happens to be free depends on
 * timing, and that must not change which scenarios one seed produces.
 */
export function planNextRun({ maxRedraws = 16, rng, table, tabs }) {
  for (let redraws = 0; redraws <= maxRedraws; redraws += 1) {
    const entry = pickWeighted(rng, table)
    const free = tabs.filter((tab) => (
      tab.open && !tab.busy && (!entry.needs_collection || Boolean(tab.collectionId))
    ))
    if (free.length > 0) return { entry, redraws, tab: free[rng.int(free.length)] }
  }
  return null
}

/** Calls of the returned function run one after another; a failure stays with its caller. */
export function createSerialQueue() {
  let tail = Promise.resolve()
  return (call) => {
    const turn = tail.then(call)
    tail = turn.catch(() => undefined)
    return turn
  }
}

/** Run *task* over *items* with at most *limit* at a time; results keep their order. */
export async function mapLimit(items, limit, task) {
  const results = new Array(items.length)
  let next = 0
  const worker = async () => {
    while (next < items.length) {
      const index = next
      next += 1
      results[index] = await task(items[index], index)
    }
  }
  await Promise.all(Array.from({ length: Math.min(limit, items.length) }, worker))
  return results
}

/** The active root runs wanted so far: linear over the ramp, then the target. */
export function desiredActiveRoots(elapsedSeconds, rampSeconds, target) {
  if (!(rampSeconds > 0) || elapsedSeconds >= rampSeconds) return target
  return Math.ceil((Math.max(0, elapsedSeconds) / rampSeconds) * target)
}

export function thinkSeconds(rng, think) {
  return rng.next() < think.late_share
    ? think.late_seconds
    : rng.between(think.min_seconds, think.max_seconds)
}

/** The wait to the next arrival of an open stream, or null at rate zero. */
export function nextArrivalSeconds(rng, ratePerUserHour, activeTabs) {
  const perSecond = (ratePerUserHour * activeTabs) / 3600
  if (!(perSecond > 0)) return null
  return -Math.log(1 - rng.next()) / perSecond
}

export function numberedQuestion(text, number) {
  return `${text} (Nr. ${number})`
}

export function sessionIdFor(mode, uuid) {
  if (mode === 'agent_kernel') return `agent-session-${uuid}`
  if (mode === 'knowledge') return `ks-${uuid}`
  return null
}

/** The body the surface sends for this scenario, field for field. */
export function buildRunRequest({ collectionId, question, scenario, sessionId, workspaceId }) {
  if (scenario.mode === 'research') {
    return {
      agent_overrides: { report_profile: scenario.report_profile },
      mode: 'research',
      question,
      workspace_id: workspaceId,
    }
  }
  if (scenario.mode === 'knowledge') {
    return {
      knowledge_filters: { collection_ids: [collectionId], profile: scenario.profile },
      mode: 'knowledge',
      question,
      session_id: sessionId,
      ui_language: 'de',
      workspace_id: workspaceId,
    }
  }
  const withCollection = scenario.needs_collection === true
  return {
    agent_overrides: { agent_tier: scenario.tier },
    autonomy: 'balanced',
    ...(withCollection ? { knowledge_filters: { collection_ids: [collectionId] } } : {}),
    mode: 'agent_kernel',
    question,
    response_form: 'auto',
    session_id: sessionId,
    source_policy: {
      knowledge: withCollection ? 'available' : 'disabled',
      web: scenario.web ? 'available' : 'disabled',
    },
    workspace_id: workspaceId,
  }
}

export function buildDirectChatRequest(question) {
  return {
    include_progress: false,
    messages: [{ content: question, role: 'user' }],
    mode: 'direct_llm',
    model: 'research-agent',
    stream: true,
  }
}

/** A limit gate is answered by keeping the result so far, anything else by text. */
export function clarificationAnswerBody(row, answerText) {
  const options = Array.isArray(row?.options) ? row.options : []
  return options.some((option) => option?.id === LIMIT_PARTIAL_OPTION)
    ? { option_id: LIMIT_PARTIAL_OPTION }
    : { answer: answerText }
}

/** Which lists a tab reads again for what the user stream told it. */
export function refreshPaths({ all, scopes }, userEvents) {
  if (all) return [...userEvents.all]
  const paths = new Set()
  for (const scope of scopes) {
    const listed = userEvents.scopes[scope]
    // A scope nobody knows must not leave a list stale.
    if (!listed) return [...userEvents.all]
    for (const path of listed) paths.add(path)
  }
  return [...paths]
}

// ------------------------------------------------------------------ run state

export function createRunState(runId) {
  return {
    children: new Map(),
    duplicates: 0,
    lastSequence: null,
    parked: null,
    received: new Set(),
    runId,
    terminal: null,
    terminalSequence: null,
  }
}

/**
 * What the stream of a finished run left out or repeated. A run's sequence has no
 * holes and ends with its terminal event, so that event's number is the total. A
 * run that was left before its terminal event has no total to hold it against.
 */
export function deliveryAccount(state) {
  return {
    duplicated: state.duplicates,
    lost: state.terminalSequence === null
      ? null
      : Math.max(0, state.terminalSequence - state.received.size),
  }
}

/**
 * Fold one event into the state of a followed root run. Returns the child gates
 * this event opened: a child parking on a person blocks the delegation, and only
 * the transition asks again, so further progress of a parked child does not.
 */
export function applyRunEvent(state, event) {
  const childGates = []
  if (typeof event?.sequence === 'number') {
    state.lastSequence = state.lastSequence === null
      ? event.sequence
      : Math.max(state.lastSequence, event.sequence)
    if (state.received.has(event.sequence)) state.duplicates += 1
    else state.received.add(event.sequence)
  }
  if (event?.type === CHILD_PROGRESS_EVENT) {
    const childRunId = event.data?.child_run_id
    const status = event.data?.run_status
    if (typeof childRunId === 'string' && typeof status === 'string') {
      if (state.children.get(childRunId) !== status) {
        state.children.set(childRunId, status)
        if (PARKED_STATUSES.has(status)) childGates.push({ childRunId, status })
      }
    }
    return childGates
  }
  if (event?.run_id !== state.runId) return childGates
  if (TERMINAL_EVENT_TYPES.has(event.type)) {
    state.terminal = event.type
    state.terminalSequence = event.sequence ?? null
    state.parked = null
  } else if (event.type === 'inqtrix.run.waiting') {
    // A wait for children is not parked: they stream through this run.
    state.parked = PARKED_STATUSES.has(event.data?.status) ? event.data.status : null
  } else if (event.type === 'inqtrix.run.resumed' || event.type === 'inqtrix.run.started') {
    state.parked = null
  }
  return childGates
}

// ------------------------------------------------------------------ percentiles

/** Nearest rank over a list small enough to keep. */
export function percentile(values, rank) {
  if (values.length === 0) return null
  const sorted = [...values].sort((first, second) => first - second)
  return sorted[Math.max(0, Math.ceil((rank / 100) * sorted.length) - 1)]
}

const HISTOGRAM_GROWTH = 1.04
const HISTOGRAM_MIN = 0.1
export const HISTOGRAM_BUCKETS = 512

/**
 * Durations without an unbounded list: fixed buckets a factor of 1.04 apart, so
 * a percentile is at most 4 % off and memory stays constant over any run.
 */
export function createHistogram() {
  const counts = new Uint32Array(HISTOGRAM_BUCKETS)
  let n = 0
  let min = Number.POSITIVE_INFINITY
  let max = Number.NEGATIVE_INFINITY
  const percentileOf = (rank) => {
    if (n === 0) return null
    const target = Math.max(1, Math.ceil((rank / 100) * n))
    let seen = 0
    for (let bucket = 0; bucket < HISTOGRAM_BUCKETS; bucket += 1) {
      seen += counts[bucket]
      if (seen >= target) {
        const upper = HISTOGRAM_MIN * HISTOGRAM_GROWTH ** bucket
        return Math.min(max, Math.max(min, upper))
      }
    }
    return max
  }
  return {
    get n() {
      return n
    },
    percentile: percentileOf,
    record(value) {
      if (!Number.isFinite(value)) return
      const sample = Math.max(0, value)
      const bucket = sample <= HISTOGRAM_MIN
        ? 0
        : Math.min(
            HISTOGRAM_BUCKETS - 1,
            Math.ceil(Math.log(sample / HISTOGRAM_MIN) / Math.log(HISTOGRAM_GROWTH)),
          )
      counts[bucket] += 1
      n += 1
      min = Math.min(min, sample)
      max = Math.max(max, sample)
    },
    summary() {
      if (n === 0) return { n: 0 }
      return {
        max_ms: round(max),
        n,
        p50_ms: round(percentileOf(50)),
        p95_ms: round(percentileOf(95)),
        p99_ms: round(percentileOf(99)),
      }
    },
  }
}

/** A small count over time: exact maximum and median, memory bound by its range. */
export function createCountGauge() {
  const counts = new Map()
  let n = 0
  let max = null
  return {
    get max() {
      return max
    },
    median() {
      if (n === 0) return null
      const target = Math.ceil(n / 2)
      let seen = 0
      for (const value of [...counts.keys()].sort((first, second) => first - second)) {
        seen += counts.get(value)
        if (seen >= target) return value
      }
      return max
    },
    get n() {
      return n
    },
    record(value) {
      if (!Number.isFinite(value)) return
      const key = Math.round(value)
      counts.set(key, (counts.get(key) ?? 0) + 1)
      n += 1
      max = max === null ? key : Math.max(max, key)
    },
  }
}

function round(value, digits = 1) {
  if (!Number.isFinite(value)) return value
  const factor = 10 ** digits
  return Math.round(value * factor) / factor
}

function measured(value, digits) {
  if (!Number.isFinite(value)) return NOT_MEASURED
  return digits === undefined ? value : round(value, digits)
}

// ------------------------------------------------------------------ server-sent events

/**
 * Frames from a byte stream. Chunk boundaries may fall anywhere, inside a frame
 * and inside a multi-byte character. An unterminated last frame is dropped, as
 * the event-stream format says, and an oversized frame is dropped and reported
 * instead of growing without bound.
 */
export function createSseParser(onFrame, { maxBufferedChars = 16 * 1024 * 1024, onOverflow } = {}) {
  const decoder = new TextDecoder()
  const boundary = /\r?\n\r?\n/g
  let buffer = ''
  let scanned = 0
  let skipping = false
  const drain = () => {
    let start = 0
    // A boundary can straddle the end of what was already searched.
    boundary.lastIndex = Math.max(0, scanned - 3)
    for (let match = boundary.exec(buffer); match; match = boundary.exec(buffer)) {
      const raw = buffer.slice(start, match.index)
      start = match.index + match[0].length
      boundary.lastIndex = start
      if (skipping) skipping = false
      else if (raw) onFrame(parseSseFrame(raw))
    }
    if (start > 0) buffer = buffer.slice(start)
    scanned = buffer.length
  }
  return {
    end() {
      buffer += decoder.decode()
      drain()
      const discarded = buffer.length > 0
      buffer = ''
      scanned = 0
      return { discarded }
    },
    push(chunk) {
      buffer += typeof chunk === 'string' ? chunk : decoder.decode(chunk, { stream: true })
      drain()
      if (buffer.length > maxBufferedChars) {
        buffer = ''
        scanned = 0
        skipping = true
        onOverflow?.()
      }
    },
  }
}

function parseSseFrame(raw) {
  const frame = { comment: false, data: null, event: null, id: null }
  const data = []
  for (const line of raw.split(/\r?\n/)) {
    if (!line) continue
    if (line.startsWith(':')) {
      frame.comment = true
      continue
    }
    const colon = line.indexOf(':')
    const field = colon < 0 ? line : line.slice(0, colon)
    let value = colon < 0 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'data') data.push(value)
    else if (field === 'event') frame.event = value
    else if (field === 'id') frame.id = value
  }
  if (data.length > 0) frame.data = data.join('\n')
  return frame
}

// ------------------------------------------------------------------ endpoints and metrics

const ID_FOLLOWS = new Set([
  'agent-sessions',
  'approvals',
  'artifacts',
  'clarifications',
  'collections',
  'deletion-operations',
  'documents',
  'indexing-jobs',
  'knowledge-sessions',
  'runs',
  'tasks',
  'threads',
  'uploads',
  'users',
])
const ID_LIKE = [UUID, /^[0-9a-f]{12,}$/i, /^\d+$/]

/** One name per route: identifiers and the query string are not part of it. */
export function normalizeEndpoint(method, path) {
  const segments = String(path).split(/[?#]/, 1)[0].split('/')
  const normalized = segments.map((segment, index) => {
    if (!segment || segment === 'import') return segment
    if (ID_FOLLOWS.has(segments[index - 1])) return '<id>'
    return ID_LIKE.some((pattern) => pattern.test(segment)) ? '<id>' : segment
  })
  return `${method} ${normalized.join('/')}`
}

const MAX_ENDPOINTS = 200
const OTHER_ENDPOINT = 'other'

export function createMetrics() {
  const counters = new Map()
  const endpoints = new Map()
  const groups = new Map()
  const lag = createHistogram()
  const streamDrops = new Map()
  const endpoint = (key) => {
    const known = endpoints.get(key)
    if (known) return known
    const name = endpoints.size >= MAX_ENDPOINTS ? OTHER_ENDPOINT : key
    const row = endpoints.get(name) ?? {
      errors: new Map(),
      histogram: createHistogram(),
      statuses: new Map(),
    }
    endpoints.set(name, row)
    return row
  }
  return {
    count(name, by = 1) {
      counters.set(name, (counters.get(name) ?? 0) + by)
    },
    counter(name) {
      return counters.get(name) ?? 0
    },
    recordGroup(name, ms) {
      if (!groups.has(name)) groups.set(name, createHistogram())
      groups.get(name).record(ms)
    },
    recordLag(ms) {
      lag.record(ms)
    },
    recordRequest(key, ms, status) {
      const row = endpoint(key)
      row.histogram.record(ms)
      row.statuses.set(status, (row.statuses.get(status) ?? 0) + 1)
    },
    recordRequestError(key, kind) {
      const row = endpoint(key)
      row.errors.set(kind, (row.errors.get(kind) ?? 0) + 1)
    },
    streamDrop(stream, reason) {
      const key = `${stream}:${reason}`
      streamDrops.set(key, (streamDrops.get(key) ?? 0) + 1)
    },
    summary() {
      return {
        counters: Object.fromEntries([...counters].sort(byKey)),
        endpoints: Object.fromEntries(
          [...endpoints].sort(byKey).map(([key, row]) => [key, endpointSummary(row)]),
        ),
        groups: Object.fromEntries(
          [...groups].sort(byKey).map(([name, histogram]) => [name, histogram.summary()]),
        ),
        sse_lag: lag.summary(),
        stream_drops: Object.fromEntries([...streamDrops].sort(byKey)),
      }
    },
  }
}

function byKey([first], [second]) {
  return first < second ? -1 : first > second ? 1 : 0
}

function endpointSummary({ errors, histogram, statuses }) {
  const inRange = (low, high) => Object.fromEntries(
    [...statuses].filter(([status]) => status >= low && status < high).sort(byKey),
  )
  const { max_ms: maxMs, n, p50_ms: p50, p95_ms: p95 } = histogram.summary()
  return {
    errors: Object.fromEntries([...errors].sort(byKey)),
    max_ms: maxMs,
    n,
    p50_ms: p50,
    p95_ms: p95,
    status_4xx: inRange(400, 500),
    status_5xx: inRange(500, 600),
  }
}

/** Everything recorded goes to the phase it happened in and to the whole run. */
export function createPhasedMetrics(names) {
  const phases = new Map(names.map((name) => [name, createMetrics()]))
  const total = createMetrics()
  let current = names[0]
  const both = (write) => {
    write(phases.get(current))
    write(total)
  }
  return {
    count: (name, by) => both((metrics) => metrics.count(name, by)),
    counter: (name) => total.counter(name),
    get phase() {
      return current
    },
    phaseCounter: (phase, name) => phases.get(phase)?.counter(name) ?? 0,
    phaseSummary: (phase) => phases.get(phase).summary(),
    recordGroup: (name, ms) => both((metrics) => metrics.recordGroup(name, ms)),
    recordLag: (ms) => both((metrics) => metrics.recordLag(ms)),
    recordRequest: (key, ms, status) => both((metrics) => metrics.recordRequest(key, ms, status)),
    recordRequestError: (key, kind) => both((metrics) => metrics.recordRequestError(key, kind)),
    setPhase(name) {
      if (!phases.has(name)) throw new Error(`Unknown run-load phase: ${name}`)
      current = name
    },
    streamDrop: (stream, reason) => both((metrics) => metrics.streamDrop(stream, reason)),
    totalSummary: () => total.summary(),
  }
}

/** What went wrong in words that carry no request and no content: type, status, code. */
export function describeFailure(error) {
  const status = /\bHTTP (\d{3})\b/.exec(String(error?.message ?? ''))
  const name = typeof error?.name === 'string' ? error.name : 'Error'
  const code = typeof error?.cause?.code === 'string' ? error.cause.code : null
  return [name, status ? `HTTP ${status[1]}` : null, code].filter(Boolean).join(', ')
}

// ------------------------------------------------------------------ accounts and sessions

export function accountEmail(prefix, number) {
  return `${prefix}${number}@${ACCOUNT_DOMAIN}`
}

/** Exactly this run's addresses: its prefix, a number, the reserved domain. */
export function accountBelongsToPrefix(email, prefix) {
  return typeof email === 'string'
    && email.startsWith(prefix)
    && ACCOUNT_SUFFIX.test(email.slice(prefix.length))
}

/** One Set-Cookie header as a stored cookie, or null when it deletes or says nothing. */
export function parseSetCookie(header, hostname) {
  const [pair, ...attributes] = String(header).split(';')
  const separator = pair.indexOf('=')
  if (separator <= 0) return null
  const cookie = {
    domain: hostname,
    expires: -1,
    httpOnly: false,
    name: pair.slice(0, separator).trim(),
    path: '/',
    sameSite: 'Lax',
    secure: false,
    value: pair.slice(separator + 1).trim(),
  }
  for (const attribute of attributes) {
    const at = attribute.indexOf('=')
    const name = (at < 0 ? attribute : attribute.slice(0, at)).trim().toLowerCase()
    const value = at < 0 ? '' : attribute.slice(at + 1).trim()
    if (name === 'path' && value) cookie.path = value
    else if (name === 'httponly') cookie.httpOnly = true
    else if (name === 'secure') cookie.secure = true
    else if (name === 'samesite' && value) {
      cookie.sameSite = value[0].toUpperCase() + value.slice(1).toLowerCase()
    } else if (name === 'max-age') {
      const seconds = Number(value)
      if (Number.isFinite(seconds) && seconds <= 0) cookie.deleted = true
    } else if (name === 'expires') {
      const expiresAt = Date.parse(value)
      if (Number.isFinite(expiresAt)) cookie.expires = expiresAt / 1000
    }
  }
  return cookie
}

// ------------------------------------------------------------------ containers

function containerLabels(row) {
  if (row?.Config?.Labels && typeof row.Config.Labels === 'object') return row.Config.Labels
  if (row?.Labels && typeof row.Labels === 'object' && !Array.isArray(row.Labels)) return row.Labels
  const labels = {}
  for (const pair of String(row?.Labels ?? '').split(',')) {
    const separator = pair.indexOf('=')
    if (separator > 0) labels[pair.slice(0, separator)] = pair.slice(separator + 1)
  }
  return labels
}

function containerName(row) {
  const names = Array.isArray(row?.Names) ? row.Names[0] : row?.Names ?? row?.Name
  return String(names ?? row?.Id ?? row?.ID ?? 'unknown').replace(/^\//, '')
}

/** The containers of exactly this Compose project, replicas included. */
export function projectContainers(rows, project) {
  const containers = []
  for (const row of rows) {
    const labels = containerLabels(row)
    if (labels[COMPOSE_PROJECT] !== project) continue
    const id = row.Id ?? row.ID
    if (typeof id !== 'string' || !/^[a-f0-9]{12,64}$/i.test(id)) continue
    containers.push({
      id,
      name: containerName(row),
      ports: [...publishedHostPorts(row)],
      running: row.State?.Running === true
        || String(row.State ?? '').toLowerCase() === 'running'
        || String(row.Status ?? '').toLowerCase().startsWith('up '),
      service: labels[COMPOSE_SERVICE] ?? 'unknown',
    })
  }
  return containers
}

export function publishedHostPorts(row) {
  const ports = new Set()
  if (Array.isArray(row?.Ports)) {
    for (const entry of row.Ports) {
      const host = Number(entry?.host_port ?? entry?.hostPort ?? entry?.PublicPort)
      const range = Number(entry?.range) || 1
      if (host > 0) for (let offset = 0; offset < range; offset += 1) ports.add(host + offset)
    }
  } else if (typeof row?.Ports === 'string') {
    for (const match of row.Ports.matchAll(/:(\d+)->/g)) ports.add(Number(match[1]))
  }
  return ports
}

/** The port a URL really connects to. */
export function portOfUrl(url) {
  const parsed = new URL(url)
  if (parsed.port) return Number(parsed.port)
  return parsed.protocol === 'https:' ? 443 : 80
}

export function isLocalHostname(hostname, ownAddresses = []) {
  return ['localhost', '127.0.0.1', '::1', '[::1]'].includes(hostname)
    || hostname.endsWith('.localhost')
    || ownAddresses.includes(hostname)
}

export function networkAddresses(networks) {
  const addresses = []
  for (const network of Object.values(networks ?? {})) {
    for (const address of [network?.IPAddress, network?.GlobalIPv6Address]) {
      if (typeof address === 'string' && address) addresses.push(address)
    }
  }
  return addresses
}

/**
 * A shell check that says only whether a container is wired to the stand-in. It
 * compares the provider endpoints and prints 1 or 0, never a value. Without this
 * proof a mistaken target would send the whole load to paid providers.
 */
export function wiringCheckScript(standIn) {
  const lines = ['ok=1']
  for (const variable of standIn.endpoint_variables) {
    lines.push(`case "$${variable}" in ${standIn.endpoint_prefix}*) ;; *) ok=0 ;; esac`)
  }
  for (const [variable, expected] of Object.entries(standIn.provider_variables)) {
    lines.push(`[ "$${variable}" = "${expected}" ] || ok=0`)
  }
  lines.push('echo "$ok"')
  return lines.join('\n')
}

/**
 * A shell check that prints limits the operator's files can set, one per line. Only
 * digits leave the container: whatever else a variable holds reads as `unset`.
 */
export function numericSettingsScript(variables) {
  return variables
    .map((variable) => `case "$${variable}" in ''|*[!0-9]*) echo unset ;; *) echo "$${variable}" ;; esac`)
    .join('\n')
}

export function parseNumericSettings(text, variables) {
  const lines = String(text ?? '').trim().split('\n')
  return Object.fromEntries(variables.map((variable, index) => {
    const value = lines[index]
    return [variable, /^(?:\d+|unset)$/.test(value ?? '') ? value : NOT_MEASURED]
  }))
}

/** The degraded messages of the stand-in nobody has accepted. */
export function unknownDegradedMessages(health, knownPatterns) {
  const patterns = knownPatterns.map((pattern) => new RegExp(pattern))
  return (Array.isArray(health?.degraded) ? health.degraded : [])
    .map((message) => String(message))
    .filter((message) => !patterns.some((pattern) => pattern.test(message)))
}

export function parseCgroupStats(text) {
  const anon = /^anon (\d+)$/m.exec(text)
  const usage = /^usage_usec (\d+)$/m.exec(text)
  const threads = /^(\d+)$/m.exec(text)
  if (!anon || !usage || !threads) return null
  return {
    anon_bytes: Number(anon[1]),
    cpu_usec: Number(usage[1]),
    threads: Number(threads[1]),
  }
}

/** Only the one counter leaves the output of the broker's INFO command. */
export function parseValkeyCommands(text) {
  const match = /^total_commands_processed:(\d+)$/m.exec(String(text))
  return match ? Number(match[1]) : null
}

// ------------------------------------------------------------------ database reads

/** One row of counters, no row content: activity, totals, waiting locks, run states. */
export const POSTGRES_SAMPLE_SQL = [
  "select json_build_object(",
  "'locks_waiting', count(*) filter (where wait_event_type = 'Lock'),",
  "'idle_in_tx', count(*) filter (where state like 'idle in transaction%'),",
  "'active', count(*) filter (where state = 'active'),",
  "'total', count(*),",
  "'commits', (select xact_commit from pg_stat_database where datname = current_database()),",
  "'rollbacks', (select xact_rollback from pg_stat_database where datname = current_database()),",
  "'sessions', (select sessions from pg_stat_database where datname = current_database()),",
  "'wal', pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::bigint,",
  "'by_client', (select coalesce(json_object_agg(addr, n), '{}') from (select coalesce(host(client_addr), 'local') addr, count(*) n from pg_stat_activity where datname = current_database() and pid <> pg_backend_pid() group by 1) by_addr),",
  "'lock_waits', (select coalesce(json_agg(json_build_object('locktype', locktype, 'mode', mode, 'n', n)), '[]') from (select locktype, mode, count(*) n from pg_locks where not granted group by 1, 2) waiting),",
  "'runs', (select coalesce(json_object_agg(status, n), '{}') from (select status, count(*) n from runs where status in ('queued', 'running', 'waiting_for_approval', 'waiting_for_input', 'waiting_for_children') group by 1) active)",
  ") from pg_stat_activity where datname = current_database() and pid <> pg_backend_pid()",
].join(' ')

export const POSTGRES_RUN_EVENTS_SQL = 'select count(*) from run_events'
/** The saver creates its tables on the first checkpoint; asking earlier only fills the log with errors. */
export const POSTGRES_CHECKPOINT_TABLES_SQL = "select count(*) from pg_class where relkind = 'r' and relname in ('checkpoint_blobs', 'checkpoint_writes')"
export const POSTGRES_CHECKPOINT_BLOBS_SQL = 'select coalesce(sum(octet_length(blob)), 0) from checkpoint_blobs'
export const POSTGRES_CHECKPOINT_WRITES_SQL = 'select coalesce(sum(octet_length(blob)), 0) from checkpoint_writes'

function uuidList(userIds) {
  for (const id of userIds) {
    if (!UUID.test(id)) throw new Error('A run-load account has an invalid identifier.')
  }
  return `'{${userIds.join(',')}}'::uuid[]`
}

/** What the runs of these accounts and their children did, in counts and bytes. */
export function runDataSql(userIds) {
  const ids = uuidList(userIds)
  return [
    `with roots as (select run_id from runs where created_by_user_id = any(${ids})),`,
    `scoped as (select run_id, status, kind, mode, parent_run_id, queued_seconds, active_seconds, waiting_seconds from runs where created_by_user_id = any(${ids}) or root_run_id in (select run_id from roots) or parent_run_id in (select run_id from roots))`,
    'select json_build_object(',
    "'runs', (select count(*) from scoped),",
    "'roots', (select count(*) from scoped where parent_run_id is null),",
    "'children', (select count(*) from scoped where parent_run_id is not null),",
    "'by_status', (select coalesce(json_object_agg(status, n), '{}') from (select status, count(*) n from scoped group by 1) statuses),",
    "'by_mode', (select coalesce(json_object_agg(mode, n), '{}') from (select mode, count(*) n from scoped group by 1) modes),",
    "'queued_p50', (select percentile_cont(0.5) within group (order by queued_seconds) from scoped),",
    "'queued_p95', (select percentile_cont(0.95) within group (order by queued_seconds) from scoped),",
    "'active_p50', (select percentile_cont(0.5) within group (order by active_seconds) from scoped),",
    "'active_p95', (select percentile_cont(0.95) within group (order by active_seconds) from scoped),",
    "'waiting_p95', (select percentile_cont(0.95) within group (order by waiting_seconds) from scoped),",
    "'queued_by_lane', (select coalesce(json_object_agg(lane, json_build_object('runs', n, 'p50', p50, 'p95', p95)), '{}') from (select concat(mode, '/', kind) lane, count(*) n, percentile_cont(0.5) within group (order by queued_seconds) p50, percentile_cont(0.95) within group (order by queued_seconds) p95 from scoped group by 1) lanes),",
    "'events', (select coalesce(json_agg(json_build_object('type', type, 'n', n, 'bytes', bytes) order by bytes desc), '[]') from (select e.type, count(*) n, sum(octet_length(e.data::text)) bytes from run_events e join scoped s using (run_id) group by e.type) by_type)",
    ')',
  ].join(' ')
}

export const RESIDUE_TABLES = Object.freeze([
  ['users', 'id'],
  ['auth_sessions', 'user_id'],
  ['runs', 'created_by_user_id'],
  ['agent_sessions', 'created_by_user_id'],
  ['knowledge_sessions', 'created_by_user_id'],
  ['knowledge_collections', 'created_by_user_id'],
])

export function residueSql(table, column, userIds) {
  return `select count(*) from ${table} where ${column} = any(${uuidList(userIds)})`
}

// ------------------------------------------------------------------ phase values

/** Counters over the samples of one phase; a missing sample stays missing. */
export function createPhaseGauges() {
  const postgres = {
    active: createCountGauge(),
    connections: createCountGauge(),
    idle_in_transaction: createCountGauge(),
    locks_waiting: createCountGauge(),
  }
  const statuses = new Map()
  const lockSamples = new Map()
  const connections = new Map()
  const containers = new Map()
  const checkpoint = { blobs: createCountGauge(), writes: createCountGauge() }
  const inFlight = createCountGauge()
  const status = (name) => {
    if (!statuses.has(name)) statuses.set(name, createCountGauge())
    return statuses.get(name)
  }
  return {
    recordCheckpoint(blobs, writes) {
      checkpoint.blobs.record(blobs)
      checkpoint.writes.record(writes)
    },
    recordContainer(container, stats, at) {
      const row = containers.get(container.name) ?? {
        at_first: null,
        at_last: null,
        cpu_first: null,
        cpu_last: null,
        memory: createCountGauge(),
        memory_first: null,
        memory_last: null,
        memory_readings: [],
        service: container.service,
        threads: createCountGauge(),
      }
      containers.set(container.name, row)
      if (!stats) return
      row.memory.record(stats.anon_bytes)
      row.memory_readings.push(stats.anon_bytes)
      row.threads.record(stats.threads)
      if (row.cpu_first === null) {
        row.at_first = at
        row.cpu_first = stats.cpu_usec
        row.memory_first = stats.anon_bytes
      }
      row.at_last = at
      row.cpu_last = stats.cpu_usec
      row.memory_last = stats.anon_bytes
    },
    recordPostgres(sample, containerOfAddress) {
      postgres.active.record(sample.active)
      postgres.connections.record(sample.total)
      postgres.idle_in_transaction.record(sample.idle_in_tx)
      postgres.locks_waiting.record(sample.locks_waiting)
      for (const name of [
        'queued',
        'running',
        'waiting_for_approval',
        'waiting_for_children',
        'waiting_for_input',
      ]) status(name).record(sample.runs?.[name] ?? 0)
      for (const row of sample.lock_waits ?? []) {
        const key = `${row.locktype}/${row.mode}`
        const seen = lockSamples.get(key) ?? { maximum: 0, samples: 0 }
        seen.samples += 1
        seen.maximum = Math.max(seen.maximum, row.n)
        lockSamples.set(key, seen)
      }
      const perContainer = new Map()
      for (const [address, count] of Object.entries(sample.by_client ?? {})) {
        const owner = containerOfAddress(address)
        const row = perContainer.get(owner.name) ?? { count: 0, service: owner.service }
        row.count += count
        perContainer.set(owner.name, row)
      }
      for (const [name, row] of perContainer) {
        const seen = connections.get(name) ?? { gauge: createCountGauge(), service: row.service }
        seen.gauge.record(row.count)
        connections.set(name, seen)
      }
    },
    recordStandIn(stats) {
      inFlight.record(stats?.in_flight)
    },
    summary() {
      const running = status('running')
      return {
        checkpoint_bytes_max: {
          blobs: measured(checkpoint.blobs.max),
          writes: measured(checkpoint.writes.max),
        },
        connections_per_container: Object.fromEntries(
          [...connections].sort(byKey).map(([name, row]) => [
            name,
            { max: row.gauge.max, service: row.service },
          ]),
        ),
        containers: Object.fromEntries(
          [...containers].sort(byKey).map(([name, row]) => [name, {
            anon_bytes_growth: row.memory_first === null
              ? NOT_MEASURED
              : row.memory_last - row.memory_first,
            // Start-up settles early and a leak keeps going: only the two halves tell them apart.
            anon_bytes_growth_second_half: row.memory_readings.length < 2
              ? NOT_MEASURED
              : row.memory_last - row.memory_readings[Math.floor(row.memory_readings.length / 2)],
            anon_bytes_max: measured(row.memory.max),
            cpu_cores: row.at_last > row.at_first && row.cpu_last >= row.cpu_first
              ? round((row.cpu_last - row.cpu_first) / 1_000_000 / (row.at_last - row.at_first), 3)
              : NOT_MEASURED,
            cpu_seconds: row.cpu_first === null || row.cpu_last < row.cpu_first
              ? NOT_MEASURED
              : round((row.cpu_last - row.cpu_first) / 1_000_000, 2),
            service: row.service,
            threads_max: measured(row.threads.max),
          }]),
        ),
        lock_samples: [...lockSamples].sort(byKey).map(([key, seen]) => {
          const [locktype, mode] = key.split('/')
          return { locktype, max_waiting: seen.maximum, mode, samples: seen.samples }
        }),
        postgres: {
          active_max: measured(postgres.active.max),
          connections_max: measured(postgres.connections.max),
          idle_in_transaction_max: measured(postgres.idle_in_transaction.max),
          locks_waiting_max: measured(postgres.locks_waiting.max),
          samples: postgres.connections.n,
        },
        slots: {
          queued_max: measured(status('queued').max),
          running_max: measured(running.max),
          running_median: measured(running.median()),
          waiting_for_approval_max: measured(status('waiting_for_approval').max),
          waiting_for_children_max: measured(status('waiting_for_children').max),
          waiting_for_input_max: measured(status('waiting_for_input').max),
        },
        stand_in_in_flight_max: measured(inFlight.max),
      }
    },
  }
}

/** Change per second between two samples, or null when either is missing. */
export function ratePerSecond(first, last, seconds) {
  if (!Number.isFinite(first) || !Number.isFinite(last) || !(seconds > 0)) return null
  // A counter that fell belongs to a process that started again inside the window.
  if (last < first) return null
  return (last - first) / seconds
}

/**
 * Transactions a unit of work caused on top of what the idle stack does anyway:
 * what the phase did, minus the idle rate over its length, per unit it finished.
 */
export function netPerUnit({ delta, idleRatePerSecond, seconds, units }) {
  if (
    !Number.isFinite(delta)
    || !Number.isFinite(idleRatePerSecond)
    || !Number.isFinite(seconds)
    || !(units > 0)
  ) return null
  return (delta - idleRatePerSecond * seconds) / units
}

export function successRatio({ completed, started }) {
  return started > 0 ? completed / started : null
}

function sumValues(counters) {
  return Object.values(counters ?? {}).reduce(
    (sum, value) => sum + (Number.isFinite(value) ? value : 0),
    0,
  )
}

function deltaOf(before, after) {
  const delta = {}
  for (const [key, value] of Object.entries(after ?? {})) {
    const change = value - (before?.[key] ?? 0)
    if (change !== 0) delta[key] = change
  }
  return delta
}

/**
 * What the stand-in saw during this run. Its counters start with its process, so
 * only the change counts. `unmatched_instructions` and the `unmatched` operation
 * count the same calls by two keys, which is why the larger one is taken instead
 * of their sum.
 */
export function standInCoverage(before, after) {
  // A stand-in that restarted counted from zero again, so no difference means anything.
  const restarted = after?.uptime_seconds < before?.uptime_seconds
  if (!after || !before || restarted) {
    return {
      passed: false,
      reason: restarted ? 'stand_in_restarted' : 'no_reading',
      status: NOT_MEASURED,
      total_calls: NOT_MEASURED,
      unmatched_share: NOT_MEASURED,
    }
  }
  const total = (after.total_calls ?? 0) - (before.total_calls ?? 0)
  const instructions = deltaOf(before.unmatched_instructions, after.unmatched_instructions)
  const operations = deltaOf(before.calls_per_operation, after.calls_per_operation)
  const synthesised = deltaOf(before.synthesised_replies, after.synthesised_replies)
  const dropped = deltaOf(before.tool_calls_dropped, after.tool_calls_dropped)
  // A reply built for a known operation had no recording either; its `unmatched`
  // entry is the same calls as the two counters above.
  const built = sumValues(synthesised) - (synthesised.unmatched ?? 0)
  const unmatched = Math.max(sumValues(instructions), operations.unmatched ?? 0) + built
  const share = total > 0 ? unmatched / total : null
  return {
    calls_per_operation: operations,
    evidence_errors: (after.evidence_errors ?? 0) - (before.evidence_errors ?? 0),
    max_unmatched_share: MAX_UNMATCHED_SHARE,
    passed: share !== null && share <= MAX_UNMATCHED_SHARE,
    synthesised_replies: synthesised,
    synthesised_replies_total: sumValues(synthesised),
    tool_calls_dropped: dropped,
    tool_calls_dropped_total: sumValues(dropped),
    total_calls: total,
    unmatched_calls: unmatched,
    unmatched_instructions: instructions,
    unmatched_share: share === null ? NOT_MEASURED : round(share, 4),
  }
}

// ------------------------------------------------------------------ verdicts

/**
 * The earlier reports a run is held against: one, or several that show the spread.
 * A report that was stopped early or has a red scenario is no base: its numbers
 * describe something else. How it stood against an older base is no such scenario:
 * that judges the older base as much as this report.
 */
export function normalizeBaseline(document) {
  const reports = Array.isArray(document)
    ? document
    : Array.isArray(document?.reports)
      ? document.reports
      : [document]
  if (reports.length < 1) {
    throw new Error('A run-load baseline holds at least one report.')
  }
  return reports.map((report) => {
    const comparison = report?.comparison
    if (!comparison || typeof comparison !== 'object') {
      throw new Error('A run-load baseline report has no comparison values.')
    }
    if (report.stop_reason) throw new Error('A run-load baseline report was stopped early.')
    const red = (report.scenarios ?? []).some(
      (scenario) => scenario.id !== SCENARIO.baseline && scenario.status !== 'passed',
    )
    if (red) throw new Error('A run-load baseline report has a red scenario.')
    return comparison
  })
}

function numbersOf(values) {
  return values.filter((value) => Number.isFinite(value))
}

/** The connections of these are not the application's: autovacuum, a psql session. */
const CONNECTION_SERVICES_NOT_COMPARED = new Set(['local', 'unknown'])
/** The same value whatever order its keys arrived in, so that it can be compared. */
function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]))
  }
  return value
}

const TIME_RULES = Object.freeze([
  ['first_event_p95', 'first_event_p95_ms'],
  ['lists_p95', 'lists_p95_ms'],
  ['result_p95', 'result_p95_ms'],
  ['submit_p95', 'submit_p95_ms'],
])

/** Above the base but within how far it scatters by itself: visible, not red. */
function timeLimit(bases, minShare = MIN_TIME_TOLERANCE_SHARE) {
  // Several reports of the same state show how far it scatters; a report alone cannot.
  const highest = Math.max(...bases)
  const tolerance = round(Math.max(highest - Math.min(...bases), highest * minShare), 1)
  return { highest, limit: round(highest + tolerance, 1), tolerance }
}

/**
 * Hold a run against its base. A value that was not measured cannot show that a
 * rule holds, so it reads as failed, with the reason. Counters are hard. The median
 * delivery delay is red above base and tolerance and a warning inside the tolerance.
 * The tail is not held against the base: between four runs of the same code it moved
 * by half while the median moved by two buckets, so it warns and only the capacity
 * model's limit makes it red. The other times only warn, because the load gate does
 * not decide on them.
 */
export function judgeBaseline(current, baselines) {
  const rules = []
  const unmeasured = (id) => rules.push({ id, status: 'failed', reason: NOT_MEASURED })

  const stages = new Set(baselines.map((base) => base.stage))
  rules.push({
    id: 'same_stage',
    status: stages.size === 1 && stages.has(current.stage) ? 'passed' : 'failed',
  })
  const setup = JSON.stringify(canonical(current.setup ?? null))
  rules.push({
    id: 'same_setup',
    status: current.setup && baselines.every((base) => JSON.stringify(canonical(base.setup ?? null)) === setup)
      ? 'passed'
      : 'failed',
  })

  // Per provider call and not per run: the mix of short and long runs and the runs
  // cut off by the window move a count of runs far more than the code does.
  // Net of the idle rate a count can only be negative if the time was not work time.
  const baseTransactions = numbersOf(baselines.map((base) => base.tx_per_provider_call_net))
  if (!(current.tx_per_provider_call_net > 0) || baseTransactions.length === 0) {
    unmeasured('transactions_per_provider_call')
  } else {
    const limit = Math.max(...baseTransactions) * (1 + BASELINE_TRANSACTION_TOLERANCE)
    rules.push({
      current: round(current.tx_per_provider_call_net, 2),
      id: 'transactions_per_provider_call',
      limit: round(limit, 2),
      status: current.tx_per_provider_call_net > limit ? 'failed' : 'passed',
    })
  }

  const services = new Set(
    [
      ...Object.keys(current.connections_per_container_max ?? {}),
      ...baselines.flatMap((base) => Object.keys(base.connections_per_container_max ?? {})),
    ].filter((service) => !CONNECTION_SERVICES_NOT_COMPARED.has(service)),
  )
  if (services.size === 0) unmeasured('connections_per_container')
  for (const service of [...services].sort()) {
    const value = current.connections_per_container_max?.[service]
    if (!Number.isFinite(value)) {
      unmeasured(`connections_per_container:${service}`)
      continue
    }
    // A peak of short-lived connections scatters by itself; a fixed pool has no
    // spread between the bases, and then the rule is exact.
    const seen = baselines.map((base) => base.connections_per_container_max?.[service] ?? 0)
    const limit = 2 * Math.max(...seen) - Math.min(...seen)
    rules.push({
      current: value,
      id: `connections_per_container:${service}`,
      limit,
      status: value > limit ? 'failed' : 'passed',
    })
  }

  for (const service of THREAD_RULE_SERVICES) {
    const seen = numbersOf(baselines.map((base) => base.threads_per_container_max?.[service]))
    // A base from before this measure cannot say anything about it.
    if (seen.length === 0) continue
    const value = current.threads_per_container_max?.[service]
    if (!Number.isFinite(value)) {
      unmeasured(`threads_per_container:${service}`)
      continue
    }
    const limit = round(Math.max(...seen) * (1 + BASELINE_THREAD_TOLERANCE), 1)
    rules.push({
      current: value,
      id: `threads_per_container:${service}`,
      limit,
      status: value > limit ? 'failed' : 'passed',
    })
  }

  const baseMedian = numbersOf(baselines.map((base) => base.sse_lag_p50_ms))
  if (!Number.isFinite(current.sse_lag_p50_ms) || baseMedian.length === 0) {
    unmeasured('sse_lag_p50')
  } else {
    const { highest, limit, tolerance } = timeLimit(baseMedian, MIN_MEDIAN_TOLERANCE_SHARE)
    const value = current.sse_lag_p50_ms
    rules.push({
      current: value,
      id: 'sse_lag_p50',
      limit,
      status: value > limit ? 'failed' : value > highest ? 'warning' : 'passed',
      tolerance,
    })
  }

  if (!Number.isFinite(current.sse_lag_p95_ms)) {
    unmeasured('sse_lag_p95')
  } else {
    const value = current.sse_lag_p95_ms
    const highest = Math.max(...numbersOf(baselines.map((base) => base.sse_lag_p95_ms)))
    rules.push({
      current: value,
      id: 'sse_lag_p95',
      limit: SSE_LAG_P95_LIMIT_MS,
      status: value > SSE_LAG_P95_LIMIT_MS ? 'failed' : value > highest ? 'warning' : 'passed',
    })
  }

  for (const [id, field] of TIME_RULES) {
    const bases = numbersOf(baselines.map((base) => base[field]))
    if (!Number.isFinite(current[field]) || bases.length === 0) continue
    const { limit, tolerance } = timeLimit(bases)
    rules.push({
      current: current[field],
      id,
      limit,
      status: current[field] > limit ? 'warning' : 'passed',
      tolerance,
    })
  }

  if (!Number.isFinite(current.loop_lag_p99_ms)) {
    unmeasured('loop_lag_p99')
  } else {
    rules.push({
      current: current.loop_lag_p99_ms,
      id: 'loop_lag_p99',
      limit: LOOP_LAG_P99_LIMIT_MS,
      status: current.loop_lag_p99_ms > LOOP_LAG_P99_LIMIT_MS ? 'failed' : 'passed',
    })
  }
  return {
    rules,
    status: rules.some((rule) => rule.status === 'failed') ? 'failed' : 'passed',
    warnings: rules.filter((rule) => rule.status === 'warning').map((rule) => rule.id),
  }
}

/**
 * The starting limits of the capacity model against what this run measured. They
 * are shown and not judged: they become hard when the operator has confirmed them.
 */
export function compareWithLimits(comparison, limits) {
  return Object.entries(limits ?? {})
    .filter(([id]) => id !== 'source')
    .map(([id, limit]) => {
      const value = comparison?.[id]
      return {
        exceeded: Number.isFinite(value) ? value > limit : NOT_MEASURED,
        id,
        limit,
        value: Number.isFinite(value) ? value : NOT_MEASURED,
      }
    })
}

/**
 * The phases the comparison reads that took longer than the stage planned. Every rate
 * divides by a phase's length, so a machine that stood still during one makes its
 * numbers meaningless, and a sleeping host can do that without any request failing.
 */
export function phaseOverruns(report, stage) {
  const planned = { idle: stage.idle_seconds, plateau: stage.plateau_seconds, rest: stage.rest_seconds }
  return Object.entries(planned)
    .filter(([phase, seconds]) => report[phase]?.seconds > seconds * (1 + MAX_PHASE_OVERRUN_SHARE) + 5)
    .map(([phase]) => phase)
    .sort()
}

/**
 * Whether the stage's load reached the stack. A defect that breaks a path lowers
 * the load and so improves every other number, which the other scenarios cannot see.
 */
export function judgeLoadApplied(facts, limits, minActiveRootShare = limits.min_active_root_share) {
  const share = (part, whole) => (whole > 0 ? part / whole : 0)
  const check = (id, passed, value, limit) => ({ id, limit, passed, value })
  const checks = [
    check('no_stop_reason', !facts.stopReason, facts.stopReason ?? null, null),
    check('no_stray_failure', facts.strayFailures === 0, facts.strayFailures, 0),
    check(
      'active_roots',
      Number.isFinite(facts.activeRootsMedian)
        && facts.activeRootsMedian >= minActiveRootShare * facts.targetRoots,
      facts.activeRootsMedian ?? NOT_MEASURED,
      minActiveRootShare * facts.targetRoots,
    ),
    check('every_tab_has_its_stream', facts.tabsOpen > 0 && facts.tabsWithStream === facts.tabsOpen, facts.tabsWithStream, facts.tabsOpen),
    check('no_tab_signed_out', facts.tabsSignedOut === 0, facts.tabsSignedOut, 0),
    check('rejected_share', share(facts.rejected, facts.rejected + facts.started) <= limits.max_rejected_share, round(share(facts.rejected, facts.rejected + facts.started), 4), limits.max_rejected_share),
    check('http_failure_share', share(facts.httpFailures, facts.requests) <= limits.max_http_failure_share, round(share(facts.httpFailures, facts.requests), 4), limits.max_http_failure_share),
    check('direct_failure_share', share(facts.directFailures, facts.directStarted) <= limits.max_direct_failure_share, round(share(facts.directFailures, facts.directStarted), 4), limits.max_direct_failure_share),
    check('run_stream_drop_share', share(facts.runStreamDrops, facts.runStreams) <= limits.max_stream_drop_share, round(share(facts.runStreamDrops, facts.runStreams), 4), limits.max_stream_drop_share),
    check('user_stream_drop_share', share(facts.userStreamDrops, facts.tabsOpen) <= limits.max_stream_drop_share, round(share(facts.userStreamDrops, facts.tabsOpen), 4), limits.max_stream_drop_share),
    check('results_readable', facts.resultsUnreadable === 0, facts.resultsUnreadable, 0),
    check('phases_as_planned', facts.phaseOverruns.length === 0, facts.phaseOverruns.join(', ') || null, null),
  ]
  return { checks, passed: checks.every((row) => row.passed) }
}

/** What the run's requests answered, from the route table of the metrics. */
export function requestTotals(endpoints) {
  let requests = 0
  let failures = 0
  for (const row of Object.values(endpoints ?? {})) {
    const errors = sumValues(row.errors)
    const failed = sumValues(row.status_5xx)
    requests += (row.n ?? 0) + errors
    failures += failed + errors
  }
  return { failures, requests }
}

/** A remaining account or row is residue; an account count nobody got is no zero. */
export function judgeResidue({ accounts, cleanupFailures = 0, tables = {} }) {
  const rows = Object.values(tables)
  // A table nobody could read may hold anything, so it is no zero either.
  const total = Number.isFinite(accounts) && rows.every((value) => Number.isFinite(value))
    ? accounts + rows.reduce((sum, value) => sum + value, 0)
    : NOT_MEASURED
  return {
    accounts: measured(accounts),
    cleanup_failures: cleanupFailures,
    passed: total === 0 && cleanupFailures === 0,
    tables: Object.fromEntries(
      Object.entries(tables).map(([table, value]) => [table, measured(value)]),
    ),
    total,
  }
}

/**
 * What one stopped worker has to leave behind: its runs handed over to another worker
 * through heartbeat silence, and none of them lost. Without a takeover the stop hit no
 * run in flight, and the run says nothing about redelivery.
 */
export function judgeWorkerStop({ activeRootsAtStop, failedAfterStop, restarted, stopped, takeovers, unfinished }) {
  const check = (id, passed, value) => ({ id, passed, value })
  const checks = [
    check('worker_stopped', stopped === true, stopped === true),
    check('worker_restarted', restarted === true, restarted === true),
    check('runs_in_flight_at_the_stop', activeRootsAtStop > 0, activeRootsAtStop),
    check('runs_handed_over', takeovers > 0, takeovers),
    check('no_run_lost', unfinished === 0, unfinished),
    check('no_run_failed_after_the_stop', failedAfterStop === 0, failedAfterStop),
  ]
  return { checks, passed: checks.every((row) => row.passed) }
}

/** New connections per minute a run may open beyond what the samplers themselves open. */
export const NEW_CONNECTIONS_ABOVE_REST_PER_MINUTE = 5
/** The connection of a contract probe or the startup check, beside the pools. */
const ONE_OFF_CONNECTIONS = 1

/**
 * What a process may hold, read from its own probes. One engine per process, no caller
 * that ran out of connections, no process above what it says it may hold, and a pool
 * that keeps its connections instead of opening new ones under load. These hold whatever
 * the base says: a base recorded with two engines must not make a second one look fine.
 */
export function judgeConnectionBudget(report) {
  const rules = []
  const final = report.probes?.final ?? {}
  const unmeasured = (id) => rules.push({ id, reason: NOT_MEASURED, status: 'failed' })
  const names = Object.keys(final).sort()
  let poolCapacity = 0
  if (names.length === 0) unmeasured('probes')
  for (const name of names) {
    const probe = final[name]
    if (!Array.isArray(probe?.pools)) {
      unmeasured(`one_pool:${name}`)
      continue
    }
    rules.push({
      current: probe.pools.length,
      id: `one_pool:${name}`,
      limit: 1,
      status: probe.pools.length === 1 ? 'passed' : 'failed',
    })
    const timeouts = probe.pools.reduce((sum, pool) => sum + pool.timeouts, 0)
    if (Number.isFinite(timeouts)) {
      rules.push({
        current: timeouts,
        id: `no_pool_timeout:${name}`,
        limit: 0,
        status: timeouts === 0 ? 'passed' : 'failed',
      })
    } else {
      unmeasured(`no_pool_timeout:${name}`)
    }
    // A caller cancelled while waiting did not get a connection either; a health
    // check or a closed tab does that now and then, so it shows without deciding.
    const abandoned = probe.pools.reduce((sum, pool) => sum + (pool.abandoned ?? 0), 0)
    rules.push({
      current: abandoned,
      id: `waits_abandoned:${name}`,
      limit: 0,
      status: abandoned === 0 ? 'passed' : 'warning',
    })
    poolCapacity += probe.pools.reduce((sum, pool) => sum + pool.size + pool.max_overflow, 0)
    const checkpointer = Math.max(0, ...(probe.checkpointer_pools ?? []).map((pool) => pool.pool_max ?? 0))
    const limit = probe.pools.reduce((sum, pool) => sum + pool.size + pool.max_overflow, 0)
      + checkpointer + ONE_OFF_CONNECTIONS
    const held = report.connections_per_container_max?.[name]?.max
    if (Number.isFinite(held)) {
      rules.push({
        current: held,
        id: `within_budget:${name}`,
        limit,
        status: held <= limit ? 'passed' : 'failed',
      })
    } else {
      unmeasured(`within_budget:${name}`)
    }
    const before = report.probes?.idle_end?.[name]?.null_pool_connects
    const after = report.probes?.plateau_end?.[name]?.null_pool_connects
    const minutes = ((report.ramp?.seconds ?? 0) + (report.plateau?.seconds ?? 0)) / 60
    if (Number.isFinite(before) && Number.isFinite(after)) {
      // A worker proves its contract once a minute on a connection of its own.
      // A process that restarted in between counts from its new start.
      const counted = after >= before ? after - before : after
      const allowed = Math.ceil(minutes) + 2
      rules.push({
        current: counted,
        id: `one_off_connections:${name}`,
        limit: allowed,
        status: counted <= allowed ? 'passed' : 'failed',
      })
    } else {
      unmeasured(`one_off_connections:${name}`)
    }
  }
  const plateau = report.plateau?.connections_new_per_minute
  const rest = report.rest?.connections_new_per_minute
  if (Number.isFinite(plateau) && Number.isFinite(rest)) {
    // The pools may still fill during the plateau, which counts for little in the
    // long stages and for a lot in a plateau of one minute.
    const plateauMinutes = (report.plateau?.seconds ?? 0) / 60
    const filling = plateauMinutes > 0 ? poolCapacity / plateauMinutes : 0
    const limit = round(rest + NEW_CONNECTIONS_ABOVE_REST_PER_MINUTE + filling, 1)
    rules.push({
      current: plateau,
      id: 'new_connections_per_minute',
      limit,
      status: plateau <= limit ? 'passed' : 'failed',
    })
  } else {
    unmeasured('new_connections_per_minute')
  }
  return {
    rules,
    status: rules.some((rule) => rule.status === 'failed') ? 'failed' : 'passed',
    warnings: rules.filter((rule) => rule.status === 'warning').map((rule) => rule.id),
  }
}

export function judgeScenarios({ baseline, connectionBudget, coverage, events, fault, loadApplied, provisioned, residue, successRatioValue, workloadRan }) {
  const verdict = (id, passed) => ({ id, status: passed ? 'passed' : 'failed' })
  const scenarios = [verdict(SCENARIO.provisioned, provisioned)]
  if (workloadRan) {
    scenarios.push(verdict(
      SCENARIO.workload,
      Number.isFinite(successRatioValue) && successRatioValue >= MIN_SUCCESS_RATIO,
    ))
    scenarios.push(verdict(SCENARIO.standIn, coverage?.passed === true))
    scenarios.push(verdict(
      SCENARIO.events,
      events?.runs_checked > 0 && events.live > 0 && events.lost === 0 && events.duplicated === 0,
    ))
    scenarios.push(verdict(SCENARIO.loadApplied, loadApplied?.passed === true))
  }
  if (residue) scenarios.push(verdict(SCENARIO.residue, residue.passed === true))
  if (connectionBudget) {
    scenarios.push(verdict(SCENARIO.connectionBudget, connectionBudget.status === 'passed'))
  }
  if (baseline) scenarios.push(verdict(SCENARIO.baseline, baseline.status === 'passed'))
  if (fault) scenarios.push(verdict(SCENARIO.workerStop, fault.passed === true))
  return scenarios
}

// ------------------------------------------------------------------ report

function phaseSection(phase, metrics) {
  const { end, gauges, start } = phase
  const seconds = Number.isFinite(end?.at) && Number.isFinite(start?.at) ? end.at - start.at : null
  const summary = metrics.phaseSummary(phase.name)
  const delivered = (summary.counters.events_live ?? 0) + (summary.counters.events_replayed ?? 0)
  const rate = (read, scale = 1) => {
    const value = ratePerSecond(read(start), read(end), seconds)
    return value === null ? NOT_MEASURED : round(value * scale, 3)
  }
  const finished = ['runs_cancelled', 'runs_completed', 'runs_failed', 'runs_unfinished']
    .reduce((sum, name) => sum + (summary.counters[name] ?? 0), 0)
  return {
    checkpoint_bytes_max: gauges?.checkpoint_bytes_max ?? NOT_MEASURED,
    client: summary,
    connections_new_per_minute: rate((row) => row?.pg?.sessions, 60),
    connections_per_container: gauges?.connections_per_container ?? {},
    containers: gauges?.containers ?? {},
    events_delivered_per_second: seconds > 0 ? round(delivered / seconds, 3) : NOT_MEASURED,
    events_written_per_second: rate((row) => row?.run_events),
    lock_samples: gauges?.lock_samples ?? [],
    postgres: gauges?.postgres ?? {},
    loop_lag: loopLagBetween(start?.loop, end?.loop),
    rollbacks_per_second: rate((row) => row?.pg?.rollbacks),
    runs_finished: finished,
    seconds: measured(seconds, 1),
    slots: gauges?.slots ?? {},
    stand_in_calls_per_second: rate((row) => row?.stand_in?.total_calls),
    stand_in_in_flight_max: gauges?.stand_in_in_flight_max ?? NOT_MEASURED,
    statements_per_second: Object.fromEntries(
      Object.keys(end?.statements ?? {}).sort().map((service) => [
        service,
        rate((row) => row?.statements?.[service]),
      ]),
    ),
    transactions_per_second: rate((row) => row?.pg?.commits),
    valkey_commands_per_second: rate((row) => row?.valkey),
    wal_bytes_per_second: rate((row) => row?.pg?.wal),
  }
}

/** One field of a phase, added up over the running containers of each service. */
function perService(section, field) {
  const totals = {}
  for (const row of Object.values(section?.containers ?? {})) {
    if (Number.isFinite(row[field])) totals[row.service] = (totals[row.service] ?? 0) + row[field]
  }
  return totals
}

/** Per unit of *count*, what each service has in *after* above what it has in *before*. */
function increasePerUnit(after, before, count, digits = 2) {
  return Object.fromEntries(
    Object.keys(after).sort().map((service) => [
      service,
      count > 0 && Number.isFinite(after[service]) && Number.isFinite(before[service])
        ? round((after[service] - before[service]) / count, digits)
        : NOT_MEASURED,
    ]),
  )
}

/**
 * What one open page costs and what one running root costs: the same counter in two
 * phases that differ by exactly that. A level that scatters more than the unit moves
 * it can read below zero, so these say something from dozens of pages on.
 */
function unitCosts(sections, provisioning, activeRoots) {
  const tabs = provisioning?.tabs_open ?? 0
  const running = sections.plateau.slots?.running_median
  const roots = activeRoots?.median
  const delta = (name) => {
    const later = sections.idle[name]
    const earlier = sections.rest[name]
    return Number.isFinite(later) && Number.isFinite(earlier) && tabs > 0
      ? round((later - earlier) / tabs, 4)
      : NOT_MEASURED
  }
  const held = (later, earlier, field, count) => increasePerUnit(
    perService(later, field),
    perService(earlier, field),
    count,
  )
  const perUnit = (count) => ({
    anon_bytes: held(sections.plateau, sections.idle, 'anon_bytes_max', count),
    count: measured(count),
    statements_per_second: increasePerUnit(
      sections.plateau.statements_per_second,
      sections.idle.statements_per_second,
      count,
      3,
    ),
    threads: held(sections.plateau, sections.idle, 'threads_max', count),
    transactions_per_second: Number.isFinite(sections.plateau.transactions_per_second)
      && Number.isFinite(sections.idle.transactions_per_second) && count > 0
      ? round((sections.plateau.transactions_per_second - sections.idle.transactions_per_second) / count, 3)
      : NOT_MEASURED,
  })
  return {
    // A root may be parked or waiting for its children; a slot is a run in progress.
    per_active_root: perUnit(roots),
    per_running_slot: perUnit(running),
    per_tab: {
      anon_bytes: held(sections.idle, sections.rest, 'anon_bytes_max', tabs),
      statements_per_second: increasePerUnit(
        sections.idle.statements_per_second,
        sections.rest.statements_per_second,
        tabs,
        3,
      ),
      tabs,
      threads: held(sections.idle, sections.rest, 'threads_max', tabs),
      transactions_per_second: delta('transactions_per_second'),
      valkey_commands_per_second: delta('valkey_commands_per_second'),
    },
  }
}

function maximaOfContainers(sections) {
  const connections = {}
  const memory = {}
  const threads = {}
  const byService = {}
  const threadsByService = {}
  for (const section of sections) {
    for (const [name, row] of Object.entries(section.connections_per_container)) {
      connections[name] = { max: Math.max(connections[name]?.max ?? 0, row.max), service: row.service }
      byService[row.service] = Math.max(byService[row.service] ?? 0, row.max)
    }
    for (const [name, row] of Object.entries(section.containers)) {
      if (Number.isFinite(row.anon_bytes_max)) {
        memory[name] = { max: Math.max(memory[name]?.max ?? 0, row.anon_bytes_max), service: row.service }
      }
      if (Number.isFinite(row.threads_max)) {
        threads[name] = { max: Math.max(threads[name]?.max ?? 0, row.threads_max), service: row.service }
        threadsByService[row.service] = Math.max(threadsByService[row.service] ?? 0, row.threads_max)
      }
    }
  }
  return { byService, connections, memory, threads, threadsByService }
}

/**
 * How soon the provider calls of cancelled runs are gone: the time after which at
 * most half, at most a twentieth and none of those in flight at the cancel are left.
 * Taken from the last reading above each mark, so a call that started after the
 * cancel counts as held. Calls that outlive the wait leave a mark not measured.
 */
export function cancelSpread(readings, inFlightAtCancel) {
  const secondsUntilAtMost = (mark) => {
    const last = readings.at(-1)
    if (!last || last.in_flight > mark) return NOT_MEASURED
    const above = readings.findLastIndex((reading) => reading.in_flight > mark)
    return round(readings[above + 1].seconds, 2)
  }
  return {
    seconds_to_half: secondsUntilAtMost(Math.floor(inFlightAtCancel / 2)),
    seconds_to_p95: secondsUntilAtMost(Math.floor(inFlightAtCancel * 0.05)),
    seconds_to_quiet: secondsUntilAtMost(0),
    status: readings.at(-1)?.in_flight === 0 ? 'measured' : NOT_MEASURED,
  }
}

/** What each service has sent so far, over its replicas; no probe, no entry. */
export function statementsPerService(probes) {
  const totals = {}
  for (const probe of Object.values(probes ?? {})) {
    const statements = probe?.values?.statements
    if (Number.isFinite(statements)) totals[probe.service] = (totals[probe.service] ?? 0) + statements
  }
  return totals
}

/** What each serving process's histogram reads at this moment, by container. */
export function loopReadings(probes) {
  const readings = {}
  for (const [name, probe] of Object.entries(probes ?? {})) {
    const lag = probe?.values?.loop_lag
    if (Array.isArray(lag?.counts) && Array.isArray(lag?.limits_ms)) {
      readings[name] = { counts: lag.counts, limits_ms: lag.limits_ms }
    }
  }
  return readings
}

/**
 * The loop lag between two readings, per container: a window of the run instead of
 * everything since the process started. A value above the last bucket reads as that
 * bucket's limit, so it can only be understated, and the share over the limit says
 * how often it happened.
 */
export function loopLagBetween(first, last) {
  const result = {}
  for (const [name, end] of Object.entries(last ?? {})) {
    const start = first?.[name]
    if (!start) continue
    const counts = end.counts.map((count, index) => count - (start.counts[index] ?? 0))
    // A process that restarted inside the window began counting again.
    if (counts.some((count) => count < 0)) continue
    const n = counts.reduce((sum, count) => sum + count, 0)
    if (!(n > 0)) continue
    const at = (rank) => {
      const target = Math.ceil((rank / 100) * n)
      let seen = 0
      for (let index = 0; index < counts.length; index += 1) {
        seen += counts[index]
        if (seen >= target) return end.limits_ms[Math.min(index, end.limits_ms.length - 1)]
      }
      return end.limits_ms.at(-1)
    }
    const over = counts.reduce(
      (sum, count, index) => sum + ((end.limits_ms[index] ?? Number.POSITIVE_INFINITY) > LOOP_LAG_P99_LIMIT_MS ? count : 0),
      0,
    )
    result[name] = { n, p50_ms: at(50), p95_ms: at(95), p99_ms: at(99), share_over_limit: round(over / n, 4) }
  }
  return result
}

/** The worst lag of any serving process in a window, or null without a reading. */
export function worstLoopLag(lagPerContainer) {
  const values = Object.values(lagPerContainer ?? {}).map((row) => row.p99_ms)
  return values.length > 0 ? Math.max(...values) : null
}

function sumOver(section, service, field) {
  const value = perService(section, field)[service]
  return Number.isFinite(value) ? round(value, 3) : null
}

function probesSection(probes) {
  return Object.fromEntries(
    Object.entries(probes ?? {}).map(([name, probe]) => [
      name,
      probe?.values
        ? { service: probe.service, ...probe.values }
        : { service: probe?.service, status: NOT_MEASURED },
    ]),
  )
}

export function buildReport(input) {
  const { metrics, phases } = input
  const sections = {}
  for (const name of ['rest', 'idle', 'ramp', 'plateau', 'decay']) {
    sections[name] = phaseSection({ ...phases[name], name }, metrics)
  }
  const measuredPhases = Object.values(sections)
  const idleRate = ratePerSecond(
    phases.idle?.start?.pg?.commits,
    phases.idle?.end?.pg?.commits,
    phases.idle?.end?.at - phases.idle?.start?.at,
  )
  // From the first run to the last: at the end of the plateau alone, a long run's
  // transactions fall into another window than its end, and the count of runs scatters.
  const workStart = phases.ramp?.start
  const workEnd = phases.decay?.end
  const workSeconds = workEnd?.at - workStart?.at
  const workDelta = workEnd?.pg?.commits - workStart?.pg?.commits
  const workRuns = sections.ramp.runs_finished + sections.plateau.runs_finished + sections.decay.runs_finished
  const net = netPerUnit({
    delta: workDelta,
    idleRatePerSecond: idleRate,
    seconds: workSeconds,
    units: workRuns,
  })
  // Read paths end in a rollback, so the commits alone miss half of the database's
  // work. The stand-in's calls are the work itself, whatever run they belong to.
  const allTransactions = (row) => (
    Number.isFinite(row?.pg?.commits) && Number.isFinite(row?.pg?.rollbacks)
      ? row.pg.commits + row.pg.rollbacks
      : null
  )
  const change = (first, last) => (
    Number.isFinite(first) && Number.isFinite(last) ? last - first : null
  )
  const plateauSeconds = phases.plateau?.end?.at - phases.plateau?.start?.at
  const plateauTransactions = change(allTransactions(phases.plateau?.start), allTransactions(phases.plateau?.end))
  const plateauCalls = change(phases.plateau?.start?.stand_in?.total_calls, phases.plateau?.end?.stand_in?.total_calls)
  const idleAllRate = ratePerSecond(
    allTransactions(phases.idle?.start),
    allTransactions(phases.idle?.end),
    phases.idle?.end?.at - phases.idle?.start?.at,
  )
  const netPerCall = netPerUnit({
    delta: plateauTransactions,
    idleRatePerSecond: idleAllRate,
    seconds: plateauSeconds,
    units: plateauCalls,
  })
  const maxima = maximaOfContainers(measuredPhases)
  const started = metrics.counter('runs_started')
  const completed = metrics.counter('runs_completed')
  const ratio = successRatio({ completed, started })
  const lagP50 = sections.plateau.client.sse_lag.p50_ms
  const lagP95 = sections.plateau.client.sse_lag.p95_ms
  const connectionsNew = sections.plateau.connections_new_per_minute
  const groupP95 = (name) => {
    const value = sections.plateau.client.groups?.[name]?.p95_ms
    return Number.isFinite(value) ? value : null
  }
  const comparison = {
    connections_per_container_max: maxima.byService,
    threads_per_container_max: maxima.threadsByService,
    first_event_p95_ms: groupP95('first_event'),
    lists_p95_ms: groupP95('lists'),
    loop_lag_p99_ms: worstLoopLag(sections.plateau.loop_lag),
    postgres_cpu_cores: sumOver(sections.plateau, 'postgres', 'cpu_cores'),
    result_p95_ms: groupP95('result'),
    setup: input.setup ?? null,
    sse_lag_p50_ms: Number.isFinite(lagP50) ? lagP50 : null,
    sse_lag_p95_ms: Number.isFinite(lagP95) ? lagP95 : null,
    stage: input.stage,
    submit_p95_ms: groupP95('submit'),
    tx_per_provider_call_net: netPerCall,
    tx_per_run_net: net,
  }
  const totals = metrics.totalSummary()
  const { failures: httpFailures, requests } = requestTotals(totals.endpoints)
  return {
    active_roots: input.activeRoots ?? NOT_MEASURED,
    baseline: input.baseline ?? { status: 'not_run' },
    cancel_probe: input.cancelProbe ?? { status: NOT_MEASURED },
    client: {
      event_loop_delay_ms: input.clientLoop ?? NOT_MEASURED,
      rss_max_bytes: input.clientRssMax ?? NOT_MEASURED,
      total: totals,
    },
    comparison,
    connections_new_per_minute: {
      idle: sections.idle.connections_new_per_minute,
      plateau: connectionsNew,
      ramp: sections.ramp.connections_new_per_minute,
    },
    connections_per_container_max: maxima.connections,
    decay: sections.decay,
    events_delivery: {
      duplicated: metrics.counter('events_duplicated'),
      live: metrics.counter('events_live'),
      lost: metrics.counter('events_lost'),
      runs_checked: metrics.counter('runs_with_full_account'),
    },
    events_per_second: {
      delivered: {
        idle: sections.idle.events_delivered_per_second,
        plateau: sections.plateau.events_delivered_per_second,
        ramp: sections.ramp.events_delivered_per_second,
      },
      written: {
        idle: sections.idle.events_written_per_second,
        plateau: sections.plateau.events_written_per_second,
        ramp: sections.ramp.events_written_per_second,
      },
    },
    finished_at: input.finishedAt,
    http: { failures: httpFailures, requests },
    idle: sections.idle,
    limits: compareWithLimits(comparison, input.limits),
    load_matrix: input.loadMatrix ?? NOT_MEASURED,
    lock_samples: {
      plateau: sections.plateau.lock_samples,
      ramp: sections.ramp.lock_samples,
    },
    mix: input.mix,
    notes: {
      probes: 'Each probe summarises its process from its start, so the values include the idle phase. The loop lag of a phase is the difference of two readings, and a stall counts the wake-ups it swallowed.',
      own_load: 'The samplers are part of the system they measure: every reading is a container exec or a query.',
      sse_lag: 'Commit time of an event against its receipt on the same host, for frames after the live marker only.',
    },
    plateau: sections.plateau,
    probes: {
      final: probesSection(input.probes?.final),
      idle_end: probesSection(input.probes?.idle_end),
      plateau_end: probesSection(input.probes?.plateau_end),
    },
    provisioning: input.provisioning,
    ramp: sections.ramp,
    residue: input.residue ?? { passed: false, total: NOT_MEASURED },
    rest: sections.rest,
    run_id: input.runId,
    runs: {
      cancelled: metrics.counter('runs_cancelled'),
      completed,
      database: input.runData ?? NOT_MEASURED,
      failed: metrics.counter('runs_failed'),
      rejected: metrics.counter('runs_rejected'),
      started,
      success_ratio: ratio === null ? NOT_MEASURED : round(ratio, 3),
      unfinished: metrics.counter('runs_unfinished'),
    },
    samplers: input.samplers ?? {},
    schema_version: 1,
    slot_wait_seconds: {
      by_lane: input.runData?.queued_by_lane ?? NOT_MEASURED,
      p50: measured(input.runData?.queued_p50, 2),
      p95: measured(input.runData?.queued_p95, 2),
      runs: measured(input.runData?.runs),
    },
    slots: sections.plateau.slots,
    stack: input.stack,
    stage: input.stage,
    stand_in: input.coverage ?? { passed: false, status: NOT_MEASURED },
    started_at: input.startedAt,
    stop_reason: input.stopReason ?? null,
    threads_per_container_max: maxima.threads,
    unit_costs: unitCosts(sections, input.provisioning, input.activeRoots),
    anon_bytes_per_container_max: maxima.memory,
    transactions_per_provider_call_net: {
      idle_rate_per_second: measured(idleAllRate, 3),
      provider_calls: measured(plateauCalls),
      seconds: measured(plateauSeconds, 1),
      transactions: measured(plateauTransactions),
      value: measured(netPerCall, 2),
    },
    transactions_per_run_net: {
      idle_rate_per_second: measured(idleRate, 3),
      runs_finished: workRuns,
      value: measured(net, 2),
      work_delta: measured(workDelta),
      work_seconds: measured(workSeconds, 1),
    },
    valkey_commands_per_second: {
      idle: sections.idle.valkey_commands_per_second,
      plateau: sections.plateau.valkey_commands_per_second,
      ramp: sections.ramp.valkey_commands_per_second,
    },
  }
}

/** The counted line a reader takes the verdict from. */
export function formatSummaryLine(report) {
  const value = (raw) => (raw === null || raw === undefined ? NOT_MEASURED : raw)
  const fields = [
    ['stage', report.stage],
    ['roots_started', report.runs?.started],
    ['completed', report.runs?.completed],
    ['failed', report.runs?.failed],
    ['success_ratio', report.runs?.success_ratio],
    ['tx_per_run_net', report.transactions_per_run_net?.value],
    ['tx_per_call_net', report.transactions_per_provider_call_net?.value],
    ['conn_new_per_min', report.connections_new_per_minute?.plateau],
    ['sse_lag_p50_ms', report.comparison?.sse_lag_p50_ms],
    ['sse_lag_p95_ms', report.comparison?.sse_lag_p95_ms],
    ['residue', report.residue?.total],
    ['unmatched_share', report.stand_in?.unmatched_share],
    ['synthesised_replies', report.stand_in?.synthesised_replies_total],
    ['tool_calls_dropped', report.stand_in?.tool_calls_dropped_total],
    ['cancelled', report.runs?.cancelled],
    ['unfinished', report.runs?.unfinished],
    ['events_lost', report.events_delivery?.lost],
    ['events_duplicated', report.events_delivery?.duplicated],
    ['rejected', report.runs?.rejected],
    ['http_failures', report.http?.failures],
    ['active_roots_median', report.active_roots?.median],
    ['loop_lag_p99_ms', report.comparison?.loop_lag_p99_ms],
    ['latency_scale', report.stack?.stand_in?.latency_scale],
  ]
  return `RUN-LOAD SUMMARY ${fields.map(([name, raw]) => `${name}=${value(raw)}`).join(' ')}`
}

const EMAIL_ADDRESS = /[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+/i

/**
 * What must not stand in a report: an address, or any of the given texts. A
 * report is shared more widely than a run's own files, so this is checked on the
 * finished text and not assumed from how it was built.
 */
export function findForbiddenContent(text, needles) {
  const found = []
  if (EMAIL_ADDRESS.test(text)) found.push('email address')
  for (const [label, needle] of Object.entries(needles)) {
    if (needle && text.includes(needle)) found.push(label)
  }
  return found
}
