import { createHash, randomUUID } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import { performance } from 'node:perf_hooks'

import WebSocket from 'ws'
import * as Y from 'yjs'

export const MESSAGE_SYNC = 0
export const MESSAGE_AWARENESS = 1
export const MESSAGE_AUTH = 2
export const MESSAGE_QUERY_AWARENESS = 3
export const MESSAGE_SYNC_REPLY = 4
export const MESSAGE_STATELESS = 5
export const MESSAGE_BROADCAST_STATELESS = 6
export const MESSAGE_CLOSE = 7
export const MESSAGE_SYNC_STATUS = 8
export const MESSAGE_PING = 9
export const MESSAGE_PONG = 10
export const AUTH_TOKEN = 0
export const AUTH_DENIED = 1
export const AUTHENTICATED = 2
export const SYNC_STEP_ONE = 0
export const SYNC_STEP_TWO = 1
export const SYNC_UPDATE = 2
export const PROVIDER_VERSION = '4.3.0'
const COLLABORATION_ROOM_PATTERN = /^inqtrix-editor-v1:([A-Za-z0-9_-]{1,160}):g[1-9][0-9]*$/
export const CAPACITY_CONNECTIONS = 1_000
export const CAPACITY_WRITERS = 100
export const CAPACITY_VISIBLE_P95_MS = 250
export const CAPACITY_DURABLE_P95_MS = 500
export const CAPACITY_OBSERVER_COHORT = 20
export const CAPACITY_MIN_DURATION_MS = 30_000
export const CAPACITY_MIN_ACK_ROUNDS_PER_WRITER = 10
export const SMOKE_VISIBLE_WARNING_P95_MS = 300
// Sustained-load profiles (soak, ramp) carry the EXPLICITLY ACCEPTED
// large-state latency risk: the visible p95 is known to sit above the
// 250ms target under long or wide load. Reporting an accepted risk as a
// hard failure adds no information and would keep every run permanently
// red.
//
// The band is deliberately NOT unbounded. Above this ceiling an update
// takes seconds to become visible, which is a functional defect rather
// than a latency nuance — and it stays a hard failure. The production
// `capacity` gate never enters this band; its budget stays absolute.
export const ACCEPTED_LATENCY_CEILING_P95_MS = 2_000
export const API_DEGRADATION_LIMIT_PERCENT = 20
export const API_P95_LIMIT_MS = 50
export const API_PROBE_SAMPLES = 20
export const API_RELATIVE_GATE_MIN_BASELINE_MS = 10
export const API_PROBE_CONTRACT = 'inqtrix-health-v1'
export const INSTANCE_PROBE_CONTRACT = 'inqtrix-collaboration-instance-v1'
export const INSTANCE_PROBE_PATH = '/collaboration/instance'
export const SESSION_REISSUE_CONTRACT = 'inqtrix-collaboration-session-reissue-v1'
export const LOAD_NETWORK_CONTROL_CONTRACT = 'inqtrix-load-network-control-v1'
export const CAPACITY_LEASE_TTL_SECONDS = 60
// -- local capacity ramp ------------------------------------------------ #
//
// DELIBERATELY NOT the `capacity` gate. Capacity is the production-like
// proof: it demands an operator-prepared fixture with its own identities
// plus an HTTPS control plane (restart + session reissue). This ramp is
// the LOCAL, self-provisioning fan-out proof — every socket is issued to
// one of at most RAMP_MAX_IDENTITIES real accounts, so it measures socket
// and room fan-out, NOT tenant or identity scale. A completed ramp
// therefore never discharges the production capacity gate; the summary
// keeps `productionProofOutstanding` true by construction.
export const RAMP_STAGES = Object.freeze([20, 100, 250, 500, 1_000])
export const RAMP_MAX_IDENTITIES = 24
// The product caps concurrent collaboration sessions per user and
// document (INQTRIX_COLLABORATION_MAX_SESSIONS_PER_USER_DOCUMENT,
// default 5). That is an abuse guard, so the ramp OBEYS it instead of
// asking for it to be widened — a rung that would need a weakened
// security control is not a rung this harness may claim.
export const RAMP_SESSIONS_PER_IDENTITY = 5
export const RAMP_WRITER_SHARE = 0.25
export const RAMP_MAX_WRITERS = 100
export const RAMP_OBSERVER_SHARE = 0.05
export const RAMP_MAX_OBSERVERS = 20

/** The socket ceiling this host may legitimately reach: identities times
 * the product's own per-user session cap. Nothing above it is attemptable
 * without weakening a security control, so nothing above it is planned. */
export function rampSocketCeiling(
  identities = RAMP_MAX_IDENTITIES,
  sessionsPerIdentity = RAMP_SESSIONS_PER_IDENTITY,
) {
  return identities * sessionsPerIdentity
}

/** Split the ladder into the rungs the product contract permits locally
 * and the rungs it does not. The second list is not a failure — it is the
 * visible, unproven remainder that only a production-scale environment
 * with many real identities can discharge. */
export function partitionRampStages(
  stages = RAMP_STAGES,
  ceiling = rampSocketCeiling(),
) {
  return {
    ceiling,
    reachable: stages.filter((connections) => connections <= ceiling),
    unreachable: stages.filter((connections) => connections > ceiling),
  }
}

/** The stage ladder with its per-stage workload shape. Pure arithmetic:
 * the engine executes it, the reporter prints it, and the tests pin it. */
export function planRampStages(stages = RAMP_STAGES) {
  let previousWriters = 0
  return stages.map((connections) => {
    const writers = Math.min(
      RAMP_MAX_WRITERS,
      Math.max(previousWriters, Math.ceil(connections * RAMP_WRITER_SHARE)),
    )
    previousWriters = writers
    return {
      connections,
      observers: Math.min(
        RAMP_MAX_OBSERVERS,
        Math.max(1, Math.ceil(connections * RAMP_OBSERVER_SHARE)),
      ),
      sessionsPerIdentity: Math.ceil(connections / RAMP_MAX_IDENTITIES),
      writers,
    }
  })
}

/** Collapse executed stages into one verdict.
 *
 * The two stop kinds are deliberately NOT equivalent:
 *   - `integrity`  -> red. A lost or duplicated mutation, an authorization
 *                     break, or an OOM/restart is a product failure at any
 *                     scale.
 *   - `budget`     -> a controlled stop, not a failure. The host ran out of
 *                     headroom or latency budget; the highest passed rung
 *                     is the honest local result and the remaining rungs
 *                     stay visibly unproven.
 * Silence is never a pass: an empty ramp is `failed`.
 */
export function summarizeRamp(results, stages = RAMP_STAGES) {
  const executed = Array.isArray(results) ? results : []
  const passed = []
  let stopKind = null
  let reason = null
  for (const result of executed) {
    if (result.integrityPassed === false) {
      stopKind = 'integrity'
      reason = result.reason ?? `integrity failed at ${result.connections} sockets`
      break
    }
    if (result.latencyPassed === false) {
      stopKind = 'budget'
      reason = result.reason ?? `budget exceeded at ${result.connections} sockets`
      break
    }
    passed.push(result.connections)
  }
  const highestPassedConnections = passed.length > 0 ? Math.max(...passed) : 0
  const unreachedConnections = stages.filter(
    (connections) => !passed.includes(connections),
  )
  if (executed.length === 0) {
    return {
      highestPassedConnections: 0,
      identityCeiling: RAMP_MAX_IDENTITIES,
      productionProofOutstanding: true,
      reason: 'The ramp executed no stage.',
      status: 'failed',
      stopKind: 'integrity',
      unreachedConnections: [...stages],
    }
  }
  return {
    highestPassedConnections,
    identityCeiling: RAMP_MAX_IDENTITIES,
    // Fan-out from a capped identity pool never replaces the external
    // production-scale proof, not even on a complete ladder.
    productionProofOutstanding: true,
    reason,
    status: stopKind === 'integrity'
      ? 'failed'
      : stopKind === 'budget'
        ? 'stopped'
        : 'passed',
    stopKind,
    unreachedConnections,
  }
}

export const SOAK_CONNECTIONS = 25
export const SOAK_WRITERS = 5
export const SOAK_OBSERVER_COHORT = 10
export const SOAK_PHASE_DURATION_MS = 5 * 60 * 1_000
export const SOAK_MIN_DURATION_MS = 6 * SOAK_PHASE_DURATION_MS
export const SOAK_MIN_ACK_ROUNDS_PER_WRITER = 60
export const SOAK_WRITER_INTERVAL_MS = 5_000
export const SOAK_NETWORK_PHASES = Object.freeze([
  Object.freeze({
    durableAckP95Ms: 500,
    durationMs: SOAK_PHASE_DURATION_MS,
    id: 'normal',
    network: Object.freeze({ kind: 'normal' }),
    visibleUpdateP95Ms: 250,
  }),
  Object.freeze({
    durableAckP95Ms: 750,
    durationMs: SOAK_PHASE_DURATION_MS,
    id: 'latency-100ms',
    network: Object.freeze({ delayMs: 100, kind: 'delay' }),
    visibleUpdateP95Ms: 500,
  }),
  Object.freeze({
    durableAckP95Ms: 1_200,
    durationMs: SOAK_PHASE_DURATION_MS,
    id: 'latency-300ms',
    network: Object.freeze({ delayMs: 300, kind: 'delay' }),
    visibleUpdateP95Ms: 900,
  }),
  Object.freeze({
    durableAckP95Ms: 1_000,
    durationMs: SOAK_PHASE_DURATION_MS,
    id: 'bandwidth-2mbit',
    network: Object.freeze({ kind: 'rate', rate: '2mbit' }),
    visibleUpdateP95Ms: 750,
  }),
  Object.freeze({
    durableAckP95Ms: 1_500,
    durationMs: SOAK_PHASE_DURATION_MS,
    id: 'packet-loss-1pct',
    network: Object.freeze({ kind: 'loss', percent: 1 }),
    visibleUpdateP95Ms: 1_000,
  }),
  Object.freeze({
    durableAckP95Ms: 500,
    durationMs: SOAK_PHASE_DURATION_MS,
    id: 'normalized',
    network: Object.freeze({ kind: 'normal' }),
    visibleUpdateP95Ms: 250,
  }),
])

const API_PROBE_TIMEOUT_MS = 5_000
const SESSION_REISSUE_BATCH_SIZE = 100
const SESSION_REISSUE_TIMEOUT_MS = 30_000
const REISSUED_LEASE_MIN_REMAINING_SECONDS = 45
const REISSUED_LEASE_CLOCK_SKEW_SECONDS = 5
const LEASE_REFRESH_SAFETY_MS = 1_000
const VERIFICATION_RUN_ID_PATTERN = /^inqv-[a-z0-9][a-z0-9-]{7,75}$/
const LOCAL_ORIGIN = Object.freeze({ kind: 'load-test-local-update' })
const REMOTE_ORIGIN = Object.freeze({ kind: 'load-test-remote-update' })
const FIXED_WORKLOAD_FLAGS = new Set([
  '--connections',
  '--writers',
  '--visible-p95-ms',
  '--durable-p95-ms',
  '--min-ack-rounds',
  '--min-duration-ms',
  '--observers',
  '--post-sample-quiet-ms',
  '--writer-interval-ms',
])

export function parseArguments(args, environment = process.env) {
  const mode = requestedMode(args)
  const options = {
    allowInsecureTls: false,
    connectConcurrency: mode === 'capacity' ? 100 : 20,
    connections: mode === 'capacity'
      ? CAPACITY_CONNECTIONS
      : mode === 'soak'
        ? SOAK_CONNECTIONS
        : 20,
    connectTimeoutMs: 30_000,
    durableAckP95Ms: CAPACITY_DURABLE_P95_MS,
    fixturePath: environment.INQTRIX_LOAD_SESSION_FIXTURE ?? null,
    help: false,
    json: false,
    minAckRoundsPerWriter: mode === 'capacity'
      ? CAPACITY_MIN_ACK_ROUNDS_PER_WRITER
      : mode === 'soak'
        ? SOAK_MIN_ACK_ROUNDS_PER_WRITER
        : 2,
    minDurationMs: mode === 'capacity'
      ? CAPACITY_MIN_DURATION_MS
      : mode === 'soak'
        ? SOAK_MIN_DURATION_MS
        : 1_000,
    mode,
    observers: mode === 'capacity'
      ? CAPACITY_OBSERVER_COHORT
      : mode === 'soak'
        ? SOAK_OBSERVER_COHORT
        : 3,
    postSampleQuietMs: mode === 'capacity' ? 1_000 : 250,
    sampleTimeoutMs: 30_000,
    skipApiProbe: false,
    suppliedFlags: new Set(),
    visibleUpdateP95Ms: CAPACITY_VISIBLE_P95_MS,
    writerIntervalMs: mode === 'soak' ? SOAK_WRITER_INTERVAL_MS : 0,
    writers: mode === 'capacity'
      ? CAPACITY_WRITERS
      : mode === 'soak'
        ? SOAK_WRITERS
        : 5,
  }

  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index]
    if (argument === '--') continue
    if (argument === '--help' || argument === '-h') options.help = true
    else if (argument === '--json') options.json = true
    else if (argument === '--allow-insecure-tls') options.allowInsecureTls = true
    else if (argument === '--skip-api-probe') options.skipApiProbe = true
    else if (argument === '--mode') index += 1
    else if (argument === '--fixture') {
      options.fixturePath = nextArgument(args, ++index, argument)
    } else if (argument === '--connections') {
      options.suppliedFlags.add(argument)
      options.connections = positiveInteger(args, ++index, argument)
    } else if (argument === '--writers') {
      options.suppliedFlags.add(argument)
      options.writers = positiveInteger(args, ++index, argument)
    } else if (argument === '--observers') {
      options.suppliedFlags.add(argument)
      options.observers = positiveInteger(args, ++index, argument)
    } else if (argument === '--min-duration-ms') {
      options.suppliedFlags.add(argument)
      options.minDurationMs = positiveInteger(args, ++index, argument)
    } else if (argument === '--min-ack-rounds') {
      options.suppliedFlags.add(argument)
      options.minAckRoundsPerWriter = positiveInteger(args, ++index, argument)
    } else if (argument === '--connect-concurrency') {
      options.connectConcurrency = positiveInteger(args, ++index, argument)
    } else if (argument === '--connect-timeout-ms') {
      options.connectTimeoutMs = positiveInteger(args, ++index, argument)
    } else if (argument === '--sample-timeout-ms') {
      options.sampleTimeoutMs = positiveInteger(args, ++index, argument)
    } else if (argument === '--post-sample-quiet-ms') {
      options.suppliedFlags.add(argument)
      options.postSampleQuietMs = positiveInteger(args, ++index, argument)
    } else if (argument === '--visible-p95-ms') {
      options.suppliedFlags.add(argument)
      options.visibleUpdateP95Ms = positiveInteger(args, ++index, argument)
    } else if (argument === '--durable-p95-ms') {
      options.suppliedFlags.add(argument)
      options.durableAckP95Ms = positiveInteger(args, ++index, argument)
    } else if (argument === '--writer-interval-ms') {
      options.suppliedFlags.add(argument)
      options.writerIntervalMs = nonNegativeInteger(args, ++index, argument)
    } else {
      throw new Error(`Unknown argument: ${argument}`)
    }
  }

  if (options.mode === 'capacity' && options.help) {
    throw new Error(
      'Capacity mode forbids --help; use the load-smoke profile for discovery.',
    )
  }
  if (options.writers + options.observers > options.connections) {
    throw new Error('--writers plus --observers must not exceed --connections.')
  }
  enforceCapacityOptions(options)
  enforceSoakOptions(options)
  return options
}

export function enforceCapacityOptions(options) {
  if (options.mode !== 'capacity') return
  if (options.allowInsecureTls) {
    throw new Error('--allow-insecure-tls is forbidden in capacity mode.')
  }
  if (options.skipApiProbe) {
    throw new Error('--skip-api-probe is forbidden in capacity mode.')
  }
  const overridden = [...options.suppliedFlags].filter((flag) => FIXED_WORKLOAD_FLAGS.has(flag))
  if (overridden.length > 0) {
    throw new Error(
      `Capacity mode fixes capacity, latency gates, and the post-sample window; remove ${overridden.join(', ')}.`,
    )
  }
  if (
    options.connections !== CAPACITY_CONNECTIONS
    || options.writers !== CAPACITY_WRITERS
    || options.observers !== CAPACITY_OBSERVER_COHORT
    || options.minDurationMs !== CAPACITY_MIN_DURATION_MS
    || options.minAckRoundsPerWriter !== CAPACITY_MIN_ACK_ROUNDS_PER_WRITER
    || options.visibleUpdateP95Ms !== CAPACITY_VISIBLE_P95_MS
    || options.durableAckP95Ms !== CAPACITY_DURABLE_P95_MS
  ) {
    throw new Error('Capacity load options do not match the architecture constants.')
  }
}

export function enforceSoakOptions(options) {
  if (options.mode !== 'soak') return
  if (options.skipApiProbe) {
    throw new Error('--skip-api-probe is forbidden in soak mode.')
  }
  const overridden = [...options.suppliedFlags].filter((flag) => FIXED_WORKLOAD_FLAGS.has(flag))
  if (overridden.length > 0) {
    throw new Error(
      `Soak mode fixes identity count, duration, pacing, observers, and latency gates; remove ${overridden.join(', ')}.`,
    )
  }
  if (
    options.connections !== SOAK_CONNECTIONS
    || options.writers !== SOAK_WRITERS
    || options.observers !== SOAK_OBSERVER_COHORT
    || options.minDurationMs !== SOAK_MIN_DURATION_MS
    || options.minAckRoundsPerWriter !== SOAK_MIN_ACK_ROUNDS_PER_WRITER
    || options.writerIntervalMs !== SOAK_WRITER_INTERVAL_MS
  ) {
    throw new Error('Soak load options do not match the fixed local release contract.')
  }
}

export function loadFixture(path) {
  if (!existsSync(path)) throw new Error('Lease/session fixture does not exist.')
  let fixture
  try {
    fixture = JSON.parse(readFileSync(path, 'utf8'))
  } catch {
    throw new Error('Lease/session fixture is not valid JSON.')
  }
  if (!isRecord(fixture) || fixture.version !== 2 || !Array.isArray(fixture.sessions)) {
    throw new Error('Lease/session fixture must contain version=2 and a sessions array.')
  }
  return fixture
}

export function prepareSessions(fixture, options, nowSeconds = Date.now() / 1_000) {
  if (fixture.sessions.length < options.connections) {
    throw new Error(
      `Lease/session fixture contains ${fixture.sessions.length} sessions; ${options.connections} are required.`,
    )
  }
  const sessions = fixture.sessions.slice(0, options.connections).map((raw, index) => {
    if (!isRecord(raw)) throw new Error(`sessions[${index}] must be an object.`)
    const leaseToken = requiredString(raw.lease_token, `sessions[${index}].lease_token`)
    const room = requiredString(raw.room, `sessions[${index}].room`)
    collaborationDocumentId(room, `sessions[${index}].room`)
    const access = requiredString(raw.access, `sessions[${index}].access`)
    if (!['edit', 'suggest', 'view'].includes(access)) {
      throw new Error(`sessions[${index}].access must be edit, suggest, or view.`)
    }
    const expiresAt = Number(raw.expires_at)
    if (!Number.isFinite(expiresAt) || expiresAt <= nowSeconds) {
      throw new Error(`sessions[${index}].expires_at must be in the future.`)
    }
    const refreshAfter = Number(raw.refresh_after)
    if (!Number.isFinite(refreshAfter) || refreshAfter <= 0 || refreshAfter >= expiresAt) {
      throw new Error(`sessions[${index}].refresh_after must be positive and precede expires_at.`)
    }
    const initialWriteMode = requiredString(
      raw.initial_write_mode,
      `sessions[${index}].initial_write_mode`,
    )
    if (initialWriteMode !== access) {
      throw new Error(`sessions[${index}].initial_write_mode must match access.`)
    }
    const protocolVersion = positiveSafeInteger(
      raw.protocol_version,
      `sessions[${index}].protocol_version`,
    )
    const reissueId = requiredString(raw.reissue_id, `sessions[${index}].reissue_id`)
    const schemaVersion = positiveSafeInteger(
      raw.schema_version,
      `sessions[${index}].schema_version`,
    )
    const userId = sessionUserId(raw.user, `sessions[${index}].user`)
    const websocketUrl = resolveWebSocketUrl(fixture, raw, index)
    return {
      access,
      expiresAt,
      leaseToken,
      origin: resolveOrigin(fixture, raw, websocketUrl, index),
      protocolVersion,
      refreshAfter,
      reissueId,
      room,
      schemaVersion,
      userId,
      websocketUrl,
    }
  })
  if (new Set(sessions.map((session) => session.room)).size !== 1) {
    throw new Error('All selected sessions must target the same collaboration room.')
  }
  if (new Set(sessions.map((session) => new URL(session.websocketUrl).host)).size !== 1) {
    throw new Error('All selected sessions must target one WebSocket host.')
  }
  if (new Set(sessions.map((session) => session.reissueId)).size !== sessions.length) {
    throw new Error('Every selected session must have a unique reissue_id.')
  }
  return sessions
}

export function resolveApiProbe(fixture, options) {
  if (options.skipApiProbe) return null
  const raw = fixture.api_probe
  if (!isRecord(raw)) {
    throw new Error(
      options.mode === 'capacity'
        ? 'Capacity mode requires fixture.api_probe.'
        : 'Smoke mode requires fixture.api_probe or explicit --skip-api-probe.',
    )
  }
  const configured = requiredString(raw.url, 'fixture.api_probe.url')
  const contract = requiredString(raw.contract, 'fixture.api_probe.contract')
  if (contract !== API_PROBE_CONTRACT) {
    throw new Error(`fixture.api_probe.contract must equal ${API_PROBE_CONTRACT}.`)
  }
  let url
  try {
    const baseUrl = optionalString(fixture.base_url)
    url = baseUrl ? new URL(configured, baseUrl) : new URL(configured)
  } catch {
    throw new Error('fixture.api_probe.url must be absolute or resolve against fixture.base_url.')
  }
  assertPublicEndpoint(url, 'fixture.api_probe.url', ['http:', 'https:'])
  return { contract, url }
}

export function resolveInstanceProbe(fixture, options) {
  const raw = fixture.instance_probe
  if (!isRecord(raw)) {
    if (options.mode === 'capacity') {
      throw new Error('Capacity mode requires fixture.instance_probe.')
    }
    return null
  }
  const configured = requiredString(raw.url, 'fixture.instance_probe.url')
  const contract = requiredString(raw.contract, 'fixture.instance_probe.contract')
  if (contract !== INSTANCE_PROBE_CONTRACT) {
    throw new Error(`fixture.instance_probe.contract must equal ${INSTANCE_PROBE_CONTRACT}.`)
  }
  let url
  try {
    const baseUrl = optionalString(fixture.base_url)
    url = baseUrl ? new URL(configured, baseUrl) : new URL(configured)
  } catch {
    throw new Error('fixture.instance_probe.url must be absolute or resolve against fixture.base_url.')
  }
  assertPublicEndpoint(url, 'fixture.instance_probe.url', ['http:', 'https:'])
  return { contract, url }
}

export function resolveRestartControl(fixture, options, environment = process.env) {
  const raw = fixture.restart_control
  if (raw === undefined || raw === null) return null
  if (!isRecord(raw)) throw new Error('fixture.restart_control must be an object.')
  const baseUrl = requiredString(raw.base_url, 'fixture.restart_control.base_url')
  const authorizationEnv = requiredString(
    raw.authorization_env,
    'fixture.restart_control.authorization_env',
  )
  const restartPath = requiredString(raw.restart_path, 'fixture.restart_control.restart_path')
  if (!/^[A-Z][A-Z0-9_]*$/.test(authorizationEnv)) {
    throw new Error('fixture.restart_control.authorization_env must name an uppercase environment variable.')
  }
  if (
    !restartPath.startsWith('/')
    || restartPath.startsWith('//')
    || restartPath.includes('?')
    || restartPath.includes('#')
  ) {
    throw new Error('fixture.restart_control.restart_path must be an absolute path without query or fragment.')
  }
  let url
  try {
    const base = new URL(baseUrl)
    assertPublicEndpoint(base, 'fixture.restart_control.base_url', ['http:', 'https:'])
    url = new URL(restartPath, base)
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('fixture.restart_control')) throw error
    throw new Error('fixture.restart_control.base_url must be an HTTP(S) URL.')
  }
  const token = environment[authorizationEnv]?.trim()
  if (!token) {
    throw new Error(`${authorizationEnv} is required for restart control authorization.`)
  }
  return { authorization: `Bearer ${token}`, url }
}

export function resolveSessionReissueControl(fixture, options, environment = process.env) {
  const raw = fixture.session_reissue
  if (raw === undefined || raw === null) return null
  if (!isRecord(raw)) throw new Error('fixture.session_reissue must be an object.')
  const authorizationEnv = requiredString(
    raw.authorization_env,
    'fixture.session_reissue.authorization_env',
  )
  if (!/^[A-Z][A-Z0-9_]*$/.test(authorizationEnv)) {
    throw new Error(
      'fixture.session_reissue.authorization_env must name an uppercase environment variable.',
    )
  }
  const contract = requiredString(raw.contract, 'fixture.session_reissue.contract')
  if (contract !== SESSION_REISSUE_CONTRACT) {
    throw new Error(`fixture.session_reissue.contract must equal ${SESSION_REISSUE_CONTRACT}.`)
  }
  const leaseTtlSeconds = positiveSafeInteger(
    raw.lease_ttl_seconds,
    'fixture.session_reissue.lease_ttl_seconds',
  )
  let url
  try {
    url = new URL(requiredString(raw.url, 'fixture.session_reissue.url'))
    assertPublicEndpoint(url, 'fixture.session_reissue.url', ['http:', 'https:'])
  } catch (error) {
    if (error instanceof Error && error.message.startsWith('fixture.session_reissue')) throw error
    throw new Error('fixture.session_reissue.url must be an HTTP(S) URL.')
  }
  const token = environment[authorizationEnv]?.trim()
  if (!token) {
    throw new Error(`${authorizationEnv} is required for session reissue authorization.`)
  }
  return {
    authorization: `Bearer ${token}`,
    contract,
    leaseTtlSeconds,
    runId: optionalRunId(raw.run_id, 'fixture.session_reissue.run_id'),
    url,
  }
}

export function resolveNetworkControl(fixture, options, environment = process.env) {
  const raw = fixture.network_control
  if (raw === undefined || raw === null) return null
  if (!isRecord(raw)) throw new Error('fixture.network_control must be an object.')
  const authorizationEnv = requiredString(
    raw.authorization_env,
    'fixture.network_control.authorization_env',
  )
  if (!/^[A-Z][A-Z0-9_]*$/.test(authorizationEnv)) {
    throw new Error('fixture.network_control.authorization_env must name an uppercase environment variable.')
  }
  const contract = requiredString(raw.contract, 'fixture.network_control.contract')
  if (contract !== LOAD_NETWORK_CONTROL_CONTRACT) {
    throw new Error(`fixture.network_control.contract must equal ${LOAD_NETWORK_CONTROL_CONTRACT}.`)
  }
  const runId = optionalRunId(raw.run_id, 'fixture.network_control.run_id')
  if (!runId) throw new Error('fixture.network_control.run_id is required.')
  let url
  try {
    url = new URL(requiredString(raw.url, 'fixture.network_control.url'))
  } catch {
    throw new Error('fixture.network_control.url must be an HTTP(S) loopback URL.')
  }
  if (
    !['http:', 'https:'].includes(url.protocol)
    || !['127.0.0.1', '[::1]', '::1'].includes(url.hostname)
    || url.username
    || url.password
    || url.search
    || url.hash
  ) {
    throw new Error('fixture.network_control.url must be an uncredentialed loopback URL.')
  }
  const token = environment[authorizationEnv]?.trim()
  if (!token) throw new Error(`${authorizationEnv} is required for network control authorization.`)
  return {
    authorization: `Bearer ${token}`,
    contract,
    runId,
    url,
  }
}

export function assertCapacityPreflight(
  options,
  sessions,
  apiProbe,
  restartControl,
  instanceProbe,
  sessionReissueControl,
) {
  if (options.mode !== 'capacity') return
  enforceCapacityOptions(options)
  if (!apiProbe) throw new Error('Capacity mode requires the API latency probe.')
  if (!restartControl) {
    throw new Error('Capacity mode requires fixture.restart_control for reconstruction after restart.')
  }
  if (!instanceProbe) {
    throw new Error('Capacity mode requires fixture.instance_probe for independent restart identity.')
  }
  if (!sessionReissueControl) {
    throw new Error('Capacity mode requires fixture.session_reissue for 60-second lease rotation.')
  }
  if (restartControl.url.protocol !== 'https:') {
    throw new Error('Capacity restart control must use HTTPS.')
  }
  if (
    sessionReissueControl.url.protocol !== 'https:'
    || sessionReissueControl.contract !== SESSION_REISSUE_CONTRACT
    || sessionReissueControl.leaseTtlSeconds !== CAPACITY_LEASE_TTL_SECONDS
  ) {
    throw new Error(
      `Capacity session reissue must use HTTPS, contract ${SESSION_REISSUE_CONTRACT}, and ${CAPACITY_LEASE_TTL_SECONDS}-second leases.`,
    )
  }
  if (apiProbe.contract !== API_PROBE_CONTRACT) {
    throw new Error(`Capacity API probe contract must equal ${API_PROBE_CONTRACT}.`)
  }
  if (apiProbe.url.protocol !== 'https:' || apiProbe.url.pathname !== '/health') {
    throw new Error('Capacity API probe must use HTTPS and the exact FastAPI /health path.')
  }
  const apiOrigin = apiProbe.url.origin
  if (
    instanceProbe.contract !== INSTANCE_PROBE_CONTRACT
    || instanceProbe.url.protocol !== 'https:'
    || instanceProbe.url.origin !== apiOrigin
    || instanceProbe.url.pathname !== INSTANCE_PROBE_PATH
  ) {
    throw new Error(
      `Capacity instance probe must use HTTPS at ${INSTANCE_PROBE_PATH} on the public API/WebSocket origin.`,
    )
  }
  for (const [index, session] of sessions.entries()) {
    const websocketUrl = new URL(session.websocketUrl)
    if (websocketUrl.protocol !== 'wss:' || websocketUrl.pathname !== '/collaboration') {
      throw new Error(
        `Capacity sessions[${index}] must use WSS and the exact public /collaboration path.`,
      )
    }
    if (webSocketHttpOrigin(websocketUrl) !== apiOrigin) {
      throw new Error(
        `Capacity sessions[${index}] WebSocket and API probe must use the same origin including effective port.`,
      )
    }
    if (session.origin !== apiOrigin) {
      throw new Error(
        `Capacity sessions[${index}] Origin header must exactly match the public API/WebSocket origin.`,
      )
    }
  }
}

export function assertSoakPreflight(
  options,
  sessions,
  apiProbe,
  networkControl,
  sessionReissueControl,
) {
  if (options.mode !== 'soak') return
  enforceSoakOptions(options)
  if (!apiProbe) throw new Error('Soak mode requires the API latency probe.')
  if (!networkControl) throw new Error('Soak mode requires fixture.network_control.')
  if (!sessionReissueControl) {
    throw new Error('Soak mode requires fixture.session_reissue for the 30-minute lease lifecycle.')
  }
  if (
    networkControl.runId === null
    || sessionReissueControl.runId !== networkControl.runId
  ) {
    throw new Error('Soak controls must share one explicit verification Run ID.')
  }
  if (
    sessions.length !== SOAK_CONNECTIONS
    || new Set(sessions.map((session) => session.userId)).size !== SOAK_CONNECTIONS
  ) {
    throw new Error('Soak mode requires exactly 25 distinct collaboration identities.')
  }
  const expectedAccess = [
    ...Array.from({ length: SOAK_WRITERS }, () => 'edit'),
    ...Array.from({ length: 5 }, () => 'suggest'),
    ...Array.from({ length: 15 }, () => 'view'),
  ]
  if (sessions.some((session, index) => session.access !== expectedAccess[index])) {
    throw new Error('Soak sessions must be ordered as 5 edit, 5 suggest, and 15 view identities.')
  }
}

export async function measureApiProbe(
  probe,
  fetchImplementation = fetch,
  sampleSpanMs = 0,
) {
  if (!probe || probe.contract !== API_PROBE_CONTRACT || !(probe.url instanceof URL)) {
    throw new Error('API probe configuration is invalid.')
  }
  if (!Number.isFinite(sampleSpanMs) || sampleSpanMs < 0) {
    throw new Error('API probe sample span must be a finite non-negative number.')
  }
  const latencies = []
  const sampleStartedAt = []
  let samplingStartedAt = null
  for (let index = 0; index < API_PROBE_SAMPLES; index += 1) {
    const targetOffset = API_PROBE_SAMPLES === 1
      ? 0
      : (sampleSpanMs * index) / (API_PROBE_SAMPLES - 1)
    while (samplingStartedAt !== null && performance.now() - samplingStartedAt < targetOffset) {
      await delay(Math.ceil(targetOffset - (performance.now() - samplingStartedAt)))
    }
    const startedAt = performance.now()
    if (samplingStartedAt === null) samplingStartedAt = startedAt
    sampleStartedAt.push(startedAt)
    let response
    try {
      response = await fetchImplementation(probe.url, {
        cache: 'no-store',
        headers: { Accept: 'application/json' },
        method: 'GET',
        redirect: 'error',
        signal: AbortSignal.timeout(API_PROBE_TIMEOUT_MS),
      })
    } catch {
      throw new Error(
        `API probe request ${index + 1}/${API_PROBE_SAMPLES} failed before receiving a response.`,
      )
    }
    if (!response.ok) {
      await response.body?.cancel().catch(() => {})
      throw new Error(
        `API probe request ${index + 1}/${API_PROBE_SAMPLES} returned HTTP ${response.status}.`,
      )
    }
    const contentType = response.headers.get('content-type')?.toLowerCase() ?? ''
    if (!contentType.includes('application/json')) {
      await response.body?.cancel().catch(() => {})
      throw new Error(
        `API probe request ${index + 1}/${API_PROBE_SAMPLES} did not return application/json.`,
      )
    }
    let payload
    try {
      payload = await response.json()
    } catch {
      throw new Error(
        `API probe request ${index + 1}/${API_PROBE_SAMPLES} returned invalid JSON.`,
      )
    }
    assertInqtrixHealthPayload(payload, index)
    latencies.push(performance.now() - startedAt)
  }
  return {
    latencies,
    sampleSpanMs: sampleStartedAt.length < 2
      ? 0
      : sampleStartedAt.at(-1) - sampleStartedAt[0],
  }
}

export async function observeCollaborationInstance(probe, fetchImplementation = fetch) {
  if (
    !probe
    || probe.contract !== INSTANCE_PROBE_CONTRACT
    || !(probe.url instanceof URL)
  ) {
    throw new Error('Collaboration instance probe configuration is invalid.')
  }
  let response
  try {
    response = await fetchImplementation(probe.url, {
      cache: 'no-store',
      headers: { Accept: 'application/json' },
      method: 'GET',
      redirect: 'error',
      signal: AbortSignal.timeout(API_PROBE_TIMEOUT_MS),
    })
  } catch {
    throw new Error('Collaboration instance probe failed before receiving a response.')
  }
  if (!response.ok) {
    await response.body?.cancel().catch(() => {})
    throw new Error(`Collaboration instance probe returned HTTP ${response.status}.`)
  }
  const contentType = response.headers.get('content-type')?.toLowerCase() ?? ''
  if (!contentType.includes('application/json')) {
    await response.body?.cancel().catch(() => {})
    throw new Error('Collaboration instance probe did not return application/json.')
  }
  const cacheDirectives = (response.headers.get('cache-control') ?? '')
    .toLowerCase()
    .split(',')
    .map((directive) => directive.trim())
  if (!cacheDirectives.includes('no-store')) {
    await response.body?.cancel().catch(() => {})
    throw new Error('Collaboration instance probe must return Cache-Control: no-store.')
  }
  let payload
  try {
    payload = await response.json()
  } catch {
    throw new Error('Collaboration instance probe returned invalid JSON.')
  }
  return parseInstanceProbePayload(payload)
}

export async function restartFixture(control, room, fetchImplementation = fetch) {
  let response
  try {
    response = await fetchImplementation(control.url, {
      body: JSON.stringify({ room }),
      headers: {
        Accept: 'application/json',
        Authorization: control.authorization,
        'Content-Type': 'application/json',
      },
      method: 'POST',
      redirect: 'error',
      signal: AbortSignal.timeout(30_000),
    })
  } catch {
    throw new Error('Collaboration restart control failed before receiving a response.')
  }
  if (!response.ok) {
    await response.body?.cancel().catch(() => {})
    throw new Error(`Collaboration restart control returned HTTP ${response.status}.`)
  }
  let payload
  try {
    payload = await response.json()
  } catch {
    throw new Error('Collaboration restart control returned invalid JSON.')
  }
  return parseRestartAcknowledgement(payload)
}

export async function reissueSessions(
  control,
  sessions,
  purpose,
  fetchImplementation = fetch,
  now = () => Date.now() / 1_000,
) {
  if (!control || control.contract !== SESSION_REISSUE_CONTRACT) {
    throw new Error('Collaboration session reissue control is invalid.')
  }
  if (![
    'connected_rotation',
    'fresh_observer',
    'post_restart_observer',
    'scheduled_rotation',
  ].includes(purpose)) {
    throw new Error('Collaboration session reissue purpose is invalid.')
  }
  const replacements = []
  for (let start = 0; start < sessions.length; start += SESSION_REISSUE_BATCH_SIZE) {
    const batch = sessions.slice(start, start + SESSION_REISSUE_BATCH_SIZE)
    const requested = batch.map((session) => ({
      reissue_id: session.reissueId,
      rotation_command_id: randomUUID(),
    }))
    let response
    try {
      response = await fetchImplementation(control.url, {
        body: JSON.stringify({
          contract: SESSION_REISSUE_CONTRACT,
          lease_ttl_seconds: control.leaseTtlSeconds,
          purpose,
          sessions: requested,
        }),
        headers: {
          Accept: 'application/json',
          Authorization: control.authorization,
          'Content-Type': 'application/json',
          ...(control.runId
            ? { 'X-Inqtrix-Verification-Run-Id': control.runId }
            : {}),
        },
        method: 'POST',
        redirect: 'error',
        signal: AbortSignal.timeout(SESSION_REISSUE_TIMEOUT_MS),
      })
    } catch {
      throw new Error('Collaboration session reissue failed before receiving a response.')
    }
    if (!response.ok) {
      await response.body?.cancel().catch(() => {})
      throw new Error(`Collaboration session reissue returned HTTP ${response.status}.`)
    }
    const contentType = response.headers.get('content-type')?.toLowerCase() ?? ''
    if (!contentType.includes('application/json')) {
      await response.body?.cancel().catch(() => {})
      throw new Error('Collaboration session reissue did not return application/json.')
    }
    const cacheDirectives = (response.headers.get('cache-control') ?? '')
      .toLowerCase()
      .split(',')
      .map((directive) => directive.trim())
    if (!cacheDirectives.includes('no-store')) {
      await response.body?.cancel().catch(() => {})
      throw new Error('Collaboration session reissue must return Cache-Control: no-store.')
    }
    let payload
    try {
      payload = await response.json()
    } catch {
      throw new Error('Collaboration session reissue returned invalid JSON.')
    }
    if (
      !isRecord(payload)
      || payload.contract !== SESSION_REISSUE_CONTRACT
      || payload.source !== 'fastapi_collaboration_session'
      || payload.lease_ttl_seconds !== control.leaseTtlSeconds
      || !Array.isArray(payload.sessions)
      || payload.sessions.length !== batch.length
    ) {
      throw new Error(
        'Collaboration session reissue response did not match the authenticated FastAPI contract.',
      )
    }
    const responseReceivedAtMs = now() * 1_000
    for (const expected of batch) {
      assertCurrentLeaseUnexpired(
        expected,
        responseReceivedAtMs,
        'after the session reissue response',
      )
    }
    const byReissueId = new Map()
    for (const [index, item] of payload.sessions.entries()) {
      if (!isRecord(item)) {
        throw new Error(`Collaboration session reissue sessions[${index}] must be an object.`)
      }
      const reissueId = requiredString(
        item.reissue_id,
        `Collaboration session reissue sessions[${index}].reissue_id`,
      )
      if (byReissueId.has(reissueId)) {
        throw new Error('Collaboration session reissue returned a duplicate reissue_id.')
      }
      byReissueId.set(reissueId, item)
    }
    for (const [index, expected] of batch.entries()) {
      const item = byReissueId.get(expected.reissueId)
      if (!item) throw new Error('Collaboration session reissue omitted a requested session.')
      if (item.rotation_command_id !== requested[index].rotation_command_id) {
        throw new Error('Collaboration session reissue returned a mismatched rotation command.')
      }
      replacements.push(parseReissuedSession(
        item.session,
        expected,
        control,
        responseReceivedAtMs / 1_000,
        'Collaboration session reissue response',
      ))
    }
  }
  return replacements
}

export async function applyNetworkPhase(
  control,
  phaseId,
  fetchImplementation = fetch,
) {
  if (!control || control.contract !== LOAD_NETWORK_CONTROL_CONTRACT) {
    throw new Error('Load network control is invalid.')
  }
  if (!SOAK_NETWORK_PHASES.some((phase) => phase.id === phaseId)) {
    throw new Error('Load network phase is not allowlisted.')
  }
  let response
  try {
    response = await fetchImplementation(control.url, {
      body: JSON.stringify({
        contract: LOAD_NETWORK_CONTROL_CONTRACT,
        phase_id: phaseId,
      }),
      headers: {
        Accept: 'application/json',
        Authorization: control.authorization,
        'Content-Type': 'application/json',
        'X-Inqtrix-Verification-Run-Id': control.runId,
      },
      method: 'POST',
      redirect: 'error',
      signal: AbortSignal.timeout(30_000),
    })
  } catch {
    throw new Error('Load network control failed before receiving a response.')
  }
  if (!response.ok) {
    await response.body?.cancel().catch(() => {})
    throw new Error(`Load network control returned HTTP ${response.status}.`)
  }
  let payload
  try {
    payload = await response.json()
  } catch {
    throw new Error('Load network control returned invalid JSON.')
  }
  if (
    !isRecord(payload)
    || payload.contract !== LOAD_NETWORK_CONTROL_CONTRACT
    || payload.phase_id !== phaseId
    || payload.state !== 'applied'
  ) {
    throw new Error('Load network control acknowledgement is invalid.')
  }
  return { phaseId, state: 'applied' }
}

export function parseRestartAcknowledgement(payload) {
  if (!isRecord(payload) || payload.state !== 'ready') {
    throw new Error('Collaboration restart control did not report state="ready".')
  }
  if (payload.restart_kind !== 'ungraceful_process') {
    throw new Error('Collaboration restart control did not prove an ungraceful process restart.')
  }
  return {
    restartKind: 'ungraceful_process',
    state: 'ready',
  }
}

export function parseInstanceProbePayload(payload) {
  if (
    !isRecord(payload)
    || payload.contract !== INSTANCE_PROBE_CONTRACT
    || payload.service !== 'inqtrix-collaboration'
    || payload.status !== 'ready'
  ) {
    throw new Error(
      'Collaboration instance probe did not match the production data-plane contract.',
    )
  }
  return parseInstanceIdentity(payload, 'probe')
}

export class FatalSocketState {
  constructor() {
    this.error = null
  }

  record(error) {
    if (this.error === null) {
      this.error = error instanceof Error ? error : new Error('Unknown WebSocket failure.')
    }
  }

  throwIfSet() {
    if (this.error) throw this.error
  }
}

class CollaborationCloseFrameError extends Error {
  constructor(index, reason) {
    super(
      `Connection ${index} received a collaboration close frame: ${
        reason || 'no reason provided'
      }.`,
    )
    this.name = 'CollaborationCloseFrameError'
  }
}

export class RawCollaborationClient {
  constructor({ allowInsecureTls = false, index, onFatal, session }) {
    this.allowInsecureTls = allowInsecureTls
    this.authenticated = false
    this.authenticatedScope = null
    this.authenticationDeniedReason = null
    this.closeFrameReason = null
    this.closedByTest = false
    this.commentEventCount = 0
    this.document = new Y.Doc()
    this.documentId = collaborationDocumentId(session.room, 'collaboration session room')
    this.index = index
    this.lastSyncSaved = null
    this.onCommentEvent = () => {}
    this.onDurableAck = () => {}
    this.onFatal = onFatal
    this.onVisibleUpdate = () => {}
    this.restartExpectation = null
    this.rotationExpectation = null
    this.session = session
    this.socket = null
    this.syncStepTwoReceived = false
  }

  async connect(timeoutMs) {
    try {
      assertCurrentLeaseUnexpired(
        this.session,
        Date.now(),
        `immediately before connection ${this.index} authentication`,
      )
    } catch (error) {
      this.fail(error)
      throw error
    }
    await new Promise((resolve, reject) => {
      let settled = false
      const timer = setTimeout(() => {
        finish(new Error(`Connection ${this.index} did not authenticate and sync within ${timeoutMs}ms.`))
      }, timeoutMs)
      const finish = (error) => {
        if (settled) return
        settled = true
        clearTimeout(timer)
        if (error) reject(error)
        else resolve()
      }
      this.finishConnect = finish
      this.socket = new WebSocket(this.session.websocketUrl, {
        origin: this.session.origin,
        rejectUnauthorized: !this.allowInsecureTls,
      })
      this.socket.binaryType = 'arraybuffer'
      this.socket.on('open', () => {
        try {
          this.sendAuthentication()
          this.sendSyncStepOne()
        } catch (error) {
          this.fail(error instanceof Error
            ? error
            : new Error(`Connection ${this.index} could not authenticate.`))
        }
      })
      this.socket.on('message', (data, isBinary) => this.handleSocketMessage(data, isBinary))
      this.socket.on('error', () => {
        if (this.restartExpectation) {
          this.restartExpectation.transportError = true
          return
        }
        if (!this.closedByTest) {
          this.fail(new Error(`Connection ${this.index} encountered a WebSocket transport error.`))
        }
      })
      this.socket.on('close', (code) => {
        if (this.restartExpectation) {
          this.restartExpectation.closeCode = code
          this.restartExpectation.resolve()
          return
        }
        if (!this.closedByTest) {
          this.fail(new Error(`Connection ${this.index} closed unexpectedly with code ${code}.`))
        }
      })
    })
  }

  handleSocketMessage(data, isBinary) {
    if (!isBinary) {
      this.fail(new Error(`Connection ${this.index} received a non-binary collaboration frame.`))
      return
    }
    try {
      this.handleMessage(toUint8Array(data))
    } catch (error) {
      if (error instanceof CollaborationCloseFrameError) {
        this.fail(error)
        return
      }
      const reason = error instanceof Error
        ? error.message
        : 'Collaboration frame parsing failed for an unknown reason.'
      this.fail(new Error(
        `Connection ${this.index} received an invalid collaboration frame: ${reason}`,
      ))
    }
  }

  handleMessage(bytes) {
    if (bytes.length === 1 && bytes[0] === MESSAGE_PING) {
      this.socket?.send(Uint8Array.of(MESSAGE_PONG))
      return
    }
    if (bytes.length === 1 && bytes[0] === MESSAGE_PONG) return

    const decoder = new ByteDecoder(bytes)
    const room = decoder.readString()
    if (room !== this.session.room) {
      throw new Error('Collaboration frame routing key does not match the leased room.')
    }
    const type = decoder.readVarUint()
    if (type === MESSAGE_AUTH) this.handleAuthentication(decoder)
    else if (type === MESSAGE_SYNC || type === MESSAGE_SYNC_REPLY) this.handleSync(decoder)
    else if (type === MESSAGE_STATELESS) this.handleStateless(decoder)
    else if (type === MESSAGE_AWARENESS) decoder.readBytes()
    else if (type === MESSAGE_QUERY_AWARENESS) {
      // No payload.
    } else if (type === MESSAGE_BROADCAST_STATELESS) {
      decoder.readString()
    } else if (type === MESSAGE_SYNC_STATUS) {
      const saved = decoder.readVarUint()
      if (saved !== 0 && saved !== 1) throw new Error('Sync status must be zero or one.')
      this.lastSyncSaved = saved === 1
    } else if (type === MESSAGE_CLOSE) {
      this.closeFrameReason = decoder.remaining > 0 ? decoder.readString() : ''
      decoder.assertDone()
      throw new CollaborationCloseFrameError(this.index, this.closeFrameReason)
    } else {
      throw new Error(`Connection ${this.index} received unsupported message type ${type}.`)
    }
    decoder.assertDone()
  }

  handleAuthentication(decoder) {
    const authType = decoder.readVarUint()
    if (authType === AUTH_TOKEN) {
      decoder.assertDone()
      this.sendAuthentication()
      return
    }
    if (authType === AUTH_DENIED) {
      this.authenticationDeniedReason = decoder.readString()
      decoder.assertDone()
      const error = new Error(`Connection ${this.index} was denied by collaboration authentication.`)
      this.rejectRotation(error)
      throw error
    }
    if (authType !== AUTHENTICATED) {
      throw new Error(`Connection ${this.index} received an unknown authentication message.`)
    }
    const scope = decoder.readString()
    decoder.assertDone()
    const expected = expectedAuthenticationScope(this.session.access)
    if (scope !== expected) {
      throw new Error(`Connection ${this.index} received an authentication scope mismatch.`)
    }
    this.authenticatedScope = scope
    this.authenticated = true
    this.maybeReady()
  }

  handleSync(decoder) {
    const syncType = decoder.readVarUint()
    if (syncType === SYNC_STEP_ONE) {
      const stateVector = decoder.readBytes()
      decoder.assertDone()
      this.sendFrame(
        MESSAGE_SYNC,
        encodeVarUint(SYNC_STEP_TWO),
        encodeBytes(Y.encodeStateAsUpdate(this.document, stateVector)),
      )
      return
    }
    if (syncType !== SYNC_STEP_TWO && syncType !== SYNC_UPDATE) {
      throw new Error(`Connection ${this.index} received an unknown Yjs sync message.`)
    }
    const update = decoder.readBytes()
    decoder.assertDone()
    Y.applyUpdate(this.document, update, REMOTE_ORIGIN)
    if (syncType === SYNC_STEP_TWO) {
      this.syncStepTwoReceived = true
      this.maybeReady()
      this.resolveRotation()
    } else {
      this.onVisibleUpdate(sha256(update))
    }
  }

  handleStateless(decoder) {
    const payload = decoder.readString()
    let message
    try {
      message = JSON.parse(payload)
    } catch {
      throw new Error('Collaboration stateless payload is not valid JSON.')
    }
    if (!isRecord(message)) {
      throw new Error('Collaboration stateless payload must be an object.')
    }
    if (message.type === 'durable_ack') {
      if (
        typeof message.hash !== 'string'
        || !/^[a-f0-9]{64}$/.test(message.hash)
        || !Number.isSafeInteger(message.sequence)
        || message.sequence < 1
      ) {
        throw new Error('Collaboration stateless payload is not a durable acknowledgement.')
      }
      this.onDurableAck({
        hash: message.hash,
        sequence: message.sequence,
        type: message.type,
      })
      return
    }
    if (
      message.type === 'collaboration_comment_changed'
      || message.type === 'collaboration_comment_mentioned'
    ) {
      const keys = Object.keys(message)
      if (
        keys.length !== 2
        || !Object.hasOwn(message, 'document_id')
        || !Object.hasOwn(message, 'type')
      ) {
        throw new Error('Collaboration comment event has an invalid payload shape.')
      }
      if (message.document_id !== this.documentId) {
        throw new Error('Collaboration comment event targets another document.')
      }
      const event = {
        documentId: message.document_id,
        type: message.type,
      }
      this.commentEventCount += 1
      this.onCommentEvent(event)
      return
    }
    if (message.type === 'durable_rejection') {
      throw new Error('Collaboration durability rejection is fatal during load verification.')
    }
    throw new Error('Collaboration stateless payload type is unsupported.')
  }

  createParagraphUpdate(text) {
    let captured = null
    const listener = (update, origin) => {
      if (origin === LOCAL_ORIGIN) captured = update
    }
    this.document.on('update', listener)
    try {
      this.document.transact(() => {
        const paragraph = new Y.XmlElement('paragraph')
        const content = new Y.XmlText()
        content.insert(0, text)
        paragraph.insert(0, [content])
        const fragment = this.document.getXmlFragment('content')
        fragment.insert(fragment.length, [paragraph])
      }, LOCAL_ORIGIN)
    } finally {
      this.document.off('update', listener)
    }
    if (!captured) throw new Error(`Writer connection ${this.index} produced no Yjs update.`)
    return captured
  }

  sendUpdate(update) {
    this.sendFrame(MESSAGE_SYNC, encodeVarUint(SYNC_UPDATE), encodeBytes(update))
  }

  sendAuthentication(nowMilliseconds = Date.now()) {
    assertCurrentLeaseUnexpired(
      this.session,
      nowMilliseconds,
      `immediately before connection ${this.index} authentication`,
    )
    this.sendFrame(
      MESSAGE_AUTH,
      encodeVarUint(AUTH_TOKEN),
      encodeString(this.session.leaseToken),
      encodeString(PROVIDER_VERSION),
    )
  }

  async rotateSession(session, timeoutMs, nowMilliseconds = Date.now()) {
    assertCurrentLeaseUnexpired(
      this.session,
      nowMilliseconds,
      `immediately before connection ${this.index} reauthentication`,
    )
    assertReplacementSession(this.session, session, this.index, nowMilliseconds)
    if (this.rotationExpectation) {
      throw new Error(`Connection ${this.index} already has a lease rotation in progress.`)
    }
    let resolve
    let reject
    const confirmed = new Promise((done, failed) => {
      resolve = done
      reject = failed
    })
    const expectation = {
      reject,
      resolve,
      timer: null,
    }
    expectation.timer = setTimeout(() => {
      if (this.rotationExpectation !== expectation) return
      this.rotationExpectation = null
      reject(new Error(
        `Connection ${this.index} did not confirm its rotated lease through server sync within ${timeoutMs}ms.`,
      ))
    }, timeoutMs)
    this.rotationExpectation = expectation
    this.session = session
    try {
      this.sendAuthentication(nowMilliseconds)
      this.sendSyncStepOne()
      await confirmed
    } catch (error) {
      if (this.rotationExpectation === expectation) {
        clearTimeout(expectation.timer)
        this.rotationExpectation = null
      }
      throw error
    }
  }

  sendSyncStepOne() {
    this.sendFrame(
      MESSAGE_SYNC,
      encodeVarUint(SYNC_STEP_ONE),
      encodeBytes(Y.encodeStateVector(this.document)),
    )
  }

  sendFrame(type, ...payloads) {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new Error(`Connection ${this.index} is not open.`)
    }
    this.socket.send(encodeRoutedFrame(this.session.room, type, ...payloads))
  }

  documentText() {
    return this.document.getXmlFragment('content').toString()
  }

  maybeReady() {
    if (this.authenticated && this.syncStepTwoReceived && this.authenticatedScope) {
      this.finishConnect?.(null)
    }
  }

  fail(error) {
    this.finishConnect?.(error)
    this.rejectRotation(error)
    this.onFatal(error)
  }

  resolveRotation() {
    const expectation = this.rotationExpectation
    if (!expectation) return
    clearTimeout(expectation.timer)
    this.rotationExpectation = null
    expectation.resolve()
  }

  rejectRotation(error) {
    const expectation = this.rotationExpectation
    if (!expectation) return
    clearTimeout(expectation.timer)
    this.rotationExpectation = null
    expectation.reject(error)
  }

  expectUngracefulRestart() {
    if (!this.socket || this.socket.readyState !== WebSocket.OPEN) {
      throw new Error(`Connection ${this.index} is not open before controlled restart.`)
    }
    if (this.restartExpectation) {
      throw new Error(`Connection ${this.index} already expects a controlled restart.`)
    }
    let resolve
    const closed = new Promise((done) => { resolve = done })
    this.restartExpectation = {
      closeCode: null,
      closed,
      resolve,
      transportError: false,
    }
  }

  cancelUngracefulRestartExpectation() {
    const expectation = this.restartExpectation
    this.restartExpectation = null
    if (expectation && (expectation.transportError || expectation.closeCode !== null)) {
      this.fail(new Error(`Connection ${this.index} failed while restart control was unsuccessful.`))
    }
  }

  async waitForUngracefulRestartClose(timeoutMs) {
    const expectation = this.restartExpectation
    if (!expectation) {
      throw new Error(`Connection ${this.index} was not armed for controlled restart.`)
    }
    let timer
    try {
      await Promise.race([
        expectation.closed,
        new Promise((_, reject) => {
          timer = setTimeout(() => reject(new Error(
            `Connection ${this.index} stayed open across the ungraceful restart.`,
          )), timeoutMs)
        }),
      ])
    } finally {
      if (timer) clearTimeout(timer)
    }
    this.restartExpectation = null
    if (expectation.closeCode !== 1006) {
      throw new Error(
        `Connection ${this.index} received close code ${expectation.closeCode}; ungraceful restart requires abnormal transport loss code 1006 without a close frame.`,
      )
    }
    return expectation.closeCode
  }

  async close() {
    this.closedByTest = true
    if (this.socket && this.socket.readyState !== WebSocket.CLOSED) {
      await new Promise((resolve) => {
        const timer = setTimeout(resolve, 1_000)
        this.socket.once('close', () => {
          clearTimeout(timer)
          resolve()
        })
        this.socket.close(1000, 'load_test_complete')
      })
    }
    this.document.destroy()
  }
}

export class SessionRotationSupervisor {
  constructor({
    clients,
    concurrency,
    control,
    fatal,
    now = () => Date.now(),
    reissue = reissueSessions,
    timeoutMs,
    wait = delay,
  }) {
    this.clients = clients
    this.concurrency = concurrency
    this.control = control
    this.error = null
    this.fatal = fatal
    this.now = now
    this.reissue = reissue
    this.rotations = {
      connected: 0,
      scheduled: 0,
    }
    this.started = false
    this.stopped = false
    this.task = null
    this.timeoutMs = timeoutMs
    this.wait = wait
  }

  async rotateNow(purpose, clients = this.clients) {
    try {
      for (let start = 0; start < clients.length; start += this.concurrency) {
        const batch = clients.slice(start, start + this.concurrency)
        await Promise.all(batch.map(async (client) => {
          const replacements = await this.reissue(
            this.control,
            [client.session],
            purpose,
          )
          if (replacements.length !== 1) {
            throw new Error(
              'Collaboration session reissue must return exactly one replacement per client.',
            )
          }
          const checkedAfterResponseAt = this.now()
          assertCurrentLeaseUnexpired(
            client.session,
            checkedAfterResponseAt,
            'after the session reissue response',
          )
          const checkedBeforeReauthenticationAt = this.now()
          assertCurrentLeaseUnexpired(
            client.session,
            checkedBeforeReauthenticationAt,
            'immediately before socket reauthentication',
          )
          await client.rotateSession(
            replacements[0],
            this.timeoutMs,
            checkedBeforeReauthenticationAt,
          )
        }))
        this.fatal.throwIfSet()
      }
      if (purpose === 'connected_rotation') this.rotations.connected += clients.length
      else if (purpose === 'scheduled_rotation') this.rotations.scheduled += clients.length
      return clients.length
    } catch (error) {
      const failure = error instanceof Error
        ? error
        : new Error('Unknown collaboration session rotation failure.')
      this.fatal.record(failure)
      throw failure
    }
  }

  start() {
    if (this.started) throw new Error('Collaboration session rotation supervisor already started.')
    this.started = true
    this.task = this.run().catch((error) => {
      this.error = error instanceof Error
        ? error
        : new Error('Unknown collaboration session rotation supervisor failure.')
      this.fatal.record(this.error)
    })
  }

  async run() {
    while (!this.stopped) {
      const now = this.now()
      const refreshAt = Math.min(...this.clients.map(
        (client) => client.session.refreshAfter * 1_000 - LEASE_REFRESH_SAFETY_MS,
      ))
      if (refreshAt > now) {
        await this.wait(Math.min(refreshAt - now, 250))
        continue
      }
      const due = this.clients.filter(
        (client) => client.session.refreshAfter * 1_000 - LEASE_REFRESH_SAFETY_MS <= this.now(),
      )
      for (const client of due) {
        assertCurrentLeaseUnexpired(
          client.session,
          this.now(),
          'before scheduled rotation',
        )
      }
      if (due.length === 0) {
        await this.wait(25)
        continue
      }
      await this.rotateNow('scheduled_rotation', due)
    }
  }

  async stop() {
    this.stopped = true
    await this.task
    if (this.error) throw this.error
    this.fatal.throwIfSet()
    return { ...this.rotations }
  }
}

export async function performUngracefulRestart(
  control,
  room,
  clients,
  timeoutMs,
  instanceProbe,
  fetchImplementation = fetch,
) {
  const before = await observeCollaborationInstance(instanceProbe, fetchImplementation)
  for (const client of clients) client.expectUngracefulRestart()
  let acknowledgement
  try {
    acknowledgement = await restartFixture(control, room, fetchImplementation)
  } catch (error) {
    for (const client of clients) client.cancelUngracefulRestartExpectation()
    throw error
  }
  await Promise.all(clients.map((client) => client.waitForUngracefulRestartClose(timeoutMs)))
  const after = await observeCollaborationInstance(instanceProbe, fetchImplementation)
  if (before.instanceId === after.instanceId) {
    throw new Error('Production instance probe did not change instance_id after restart.')
  }
  if (after.epoch <= before.epoch) {
    throw new Error('Production instance probe did not advance epoch after restart.')
  }
  return {
    closedSockets: clients.length,
    transition: {
      after,
      before,
      restartKind: acknowledgement.restartKind,
      state: acknowledgement.state,
    },
  }
}

export async function connectInBatches(clients, concurrency, timeoutMs, fatal, onProgress) {
  for (let start = 0; start < clients.length; start += concurrency) {
    const batch = clients.slice(start, start + concurrency)
    await Promise.all(batch.map((client) => client.connect(timeoutMs)))
    fatal.throwIfSet()
    onProgress?.(Math.min(start + batch.length, clients.length), clients.length)
  }
}

export async function runSustainedWriterLoad({
  apiProbe,
  fatal,
  minAckRoundsPerWriter,
  minDurationMs,
  markerTag = null,
  observers,
  runId = randomUUID(),
  sampleTimeoutMs,
  writerIntervalMs = 0,
  writers,
}) {
  const records = new Map()
  const startedAt = performance.now()
  let stopWriting = false

  for (const observer of observers) {
    observer.onVisibleUpdate = (hash) => {
      const record = records.get(hash)
      if (record && !record.visibleByObserver.has(observer.index)) {
        record.visibleByObserver.set(observer.index, performance.now())
      }
    }
  }
  for (const writer of writers) {
    writer.onDurableAck = (ack) => {
      const record = records.get(ack.hash)
      if (record && record.ackAt === null) {
        record.ackAt = performance.now()
        record.sequence = ack.sequence
      }
    }
  }

  let rejectLoadFailure
  const loadFailure = new Promise((_, reject) => { rejectLoadFailure = reject })
  const roundsPerWriter = Array.from({ length: writers.length }, () => 0)
  const writerTasks = writers.map(async (writer, writerIndex) => {
    let round = 0
    do {
      fatal.throwIfSet()
      const marker = `inqtrix-load-${runId}-${
        markerTag ? `${markerTag}-` : ''
      }${writerIndex}-${round}`
      const update = writer.createParagraphUpdate(marker)
      const hash = sha256(update)
      const record = {
        ackAt: null,
        hash,
        marker,
        sentAt: performance.now(),
        sequence: null,
        visibleByObserver: new Map(),
      }
      records.set(hash, record)
      writer.sendUpdate(update)
      await waitForRecord(record, sampleTimeoutMs, fatal, observers.length)
      round += 1
      roundsPerWriter[writerIndex] = round
      if (writerIntervalMs > 0 && !stopWriting) {
        const elapsedMs = performance.now() - record.sentAt
        if (elapsedMs < writerIntervalMs) await delay(writerIntervalMs - elapsedMs)
      }
    } while (!stopWriting || round < minAckRoundsPerWriter)
  })
  let writerError = null
  const writerCompletion = Promise.all(writerTasks).catch((error) => {
    writerError = error
    stopWriting = true
    rejectLoadFailure(error)
  })

  let loadedApiMeasurement = null
  let probeError = null
  const probeCompletion = apiProbe
    ? measureApiProbe(apiProbe, fetch, minDurationMs).then((measurement) => {
        loadedApiMeasurement = measurement
      })
      .catch((error) => {
        probeError = error
        rejectLoadFailure(error)
      })
    : Promise.resolve()
  let durationTimer
  const durationCompletion = new Promise((resolve) => {
    durationTimer = setTimeout(resolve, minDurationMs)
  })
  let controlError = null
  try {
    await Promise.race([
      Promise.all([probeCompletion, durationCompletion]),
      loadFailure,
    ])
  } catch (error) {
    controlError = error
  } finally {
    if (durationTimer) clearTimeout(durationTimer)
    stopWriting = true
  }
  await writerCompletion
  fatal.throwIfSet()
  if (writerError) throw writerError
  if (probeError) throw probeError
  if (controlError) throw controlError

  const visibleLatencies = []
  const durableLatencies = []
  for (const record of records.values()) {
    if (!Number.isSafeInteger(record.sequence) || record.sequence < 1) {
      throw new Error('Writer sample is missing a positive durable sequence.')
    }
    for (const visibleAt of record.visibleByObserver.values()) {
      visibleLatencies.push(visibleAt - record.sentAt)
    }
    durableLatencies.push(record.ackAt - record.sentAt)
  }
  const durationMs = performance.now() - startedAt
  return {
    durationMs,
    durableLatencies,
    loadedApiLatencies: loadedApiMeasurement?.latencies ?? null,
    loadedApiSampleSpanMs: loadedApiMeasurement?.sampleSpanMs ?? null,
    markers: [...records.values()].map((record) => record.marker),
    observerCount: observers.length,
    roundsPerWriter,
    runId,
    visibleLatencies,
  }
}

export async function runSoakPhases({
  apiProbe,
  applyPhase = applyNetworkPhase,
  baselineApiMeasurement,
  fatal,
  networkControl,
  observers,
  phases = SOAK_NETWORK_PHASES,
  runId = randomUUID(),
  runWriterLoad = runSustainedWriterLoad,
  sampleTimeoutMs,
  writerIntervalMs = SOAK_WRITER_INTERVAL_MS,
  writers,
}) {
  if (!Array.isArray(phases) || phases.length === 0) {
    throw new Error('Soak requires at least one explicit network phase.')
  }
  const phaseResults = []
  const markers = []
  const durableLatencies = []
  const visibleLatencies = []
  const roundsPerWriter = Array.from({ length: writers.length }, () => 0)
  const minimumRoundsPerPhase = Math.ceil(
    SOAK_MIN_ACK_ROUNDS_PER_WRITER / SOAK_NETWORK_PHASES.length,
  )
  let durationMs = 0
  let loadedApiSampleSpanMs = 0
  for (const phase of phases) {
    await applyPhase(networkControl, phase.id)
    const load = await runWriterLoad({
      apiProbe,
      fatal,
      markerTag: phase.id,
      minAckRoundsPerWriter: minimumRoundsPerPhase,
      minDurationMs: phase.durationMs,
      observers,
      runId,
      sampleTimeoutMs,
      writerIntervalMs,
      writers,
    })
    const apiSummary = summarizeApiProbe(
      baselineApiMeasurement?.latencies ?? null,
      load.loadedApiLatencies,
    )
    const gates = evaluateGates(
      load.visibleLatencies,
      load.durableLatencies,
      apiSummary,
      {
        durableAckP95Ms: phase.durableAckP95Ms,
        minAckRoundsPerWriter: minimumRoundsPerPhase,
        minDurationMs: phase.durationMs,
        observers: observers.length,
        visibleUpdateP95Ms: phase.visibleUpdateP95Ms,
        writers: writers.length,
      },
      load,
    )
    const passed = phaseGatesPassed(gates)
    phaseResults.push({
      apiProbe: apiSummary,
      durableAckMs: summarize(load.durableLatencies),
      gates,
      id: phase.id,
      passed,
      visibleUpdateMs: summarize(load.visibleLatencies),
      writeSamples: load.markers.length,
    })
    markers.push(...load.markers)
    durableLatencies.push(...load.durableLatencies)
    visibleLatencies.push(...load.visibleLatencies)
    durationMs += load.durationMs
    loadedApiSampleSpanMs += load.loadedApiSampleSpanMs ?? 0
    for (const [index, rounds] of load.roundsPerWriter.entries()) {
      roundsPerWriter[index] += rounds
    }
  }
  const passed = phaseResults.every((phase) => phase.passed)
  const apiLatencyStatus = phaseResults.some((phase) => phase.apiProbe.status === 'failed')
    ? 'failed'
    : phaseResults.some((phase) => phase.apiProbe.status === 'warning')
      ? 'warning'
      : phaseResults.every((phase) => phase.apiProbe.status === 'skipped')
        ? 'skipped'
        : 'passed'
  return {
    durationMs,
    durableLatencies,
    gates: {
      apiLatencyAbsoluteLimitMs: API_P95_LIMIT_MS,
      apiLatencyDegradationLimitPercent: API_DEGRADATION_LIMIT_PERCENT,
      apiLatencyPassed: apiLatencyStatus !== 'failed',
      apiLatencyRelativeGateApplied: phaseResults.every(
        (phase) => phase.apiProbe.relativeGateApplied === true,
      ),
      apiLatencyRelativeGateMinBaselineMs: API_RELATIVE_GATE_MIN_BASELINE_MS,
      apiLatencyStatus,
      apiSampleSpanPassed: phaseResults.every(
        (phase) => phase.gates.apiSampleSpanPassed === true,
      ),
      durableAckPassed: phaseResults.every((phase) => phase.gates.durableAckPassed),
      minimumAckRoundsPassed: roundsPerWriter.every(
        (rounds) => rounds >= SOAK_MIN_ACK_ROUNDS_PER_WRITER,
      ),
      minimumDurationPassed: durationMs >= phases.reduce(
        (total, phase) => total + phase.durationMs,
        0,
      ),
      observerCohortPassed: phaseResults.every(
        (phase) => phase.gates.observerCohortPassed,
      ),
      phaseResultsPassed: passed,
      visibleUpdatePassed: phaseResults.every((phase) => phase.gates.visibleUpdatePassed),
    },
    loadedApiLatencies: phaseResults.flatMap((phase) => phase.apiProbe.loadedP95Ms === null
      ? []
      : [phase.apiProbe.loadedP95Ms]),
    loadedApiSampleSpanMs,
    markers,
    observerCount: observers.length,
    phases: phaseResults,
    roundsPerWriter,
    runId,
    visibleLatencies,
  }
}

export function verifyReconstructedMarkers(client, markers, runId) {
  const expected = new Set(markers)
  const pattern = new RegExp(`inqtrix-load-${escapeRegExp(runId)}-[a-z0-9-]+`, 'g')
  const observed = client.documentText().match(pattern) ?? []
  const counts = new Map()
  for (const marker of observed) counts.set(marker, (counts.get(marker) ?? 0) + 1)
  const missing = markers.filter((marker) => !counts.has(marker)).length
  const duplicates = [...counts.entries()]
    .filter(([marker, count]) => expected.has(marker) && count !== 1)
    .length
  const unexpected = [...counts.keys()].filter((marker) => !expected.has(marker)).length
  return {
    duplicates,
    expected: markers.length,
    missing,
    observed: observed.length,
    passed: missing === 0 && duplicates === 0 && unexpected === 0,
    unexpected,
  }
}

function phaseGatesPassed(gates) {
  return (
    gates.visibleUpdatePassed
    && gates.durableAckPassed
    && gates.apiLatencyStatus !== 'failed'
    && gates.apiSampleSpanPassed !== false
    && gates.minimumAckRoundsPassed
    && gates.minimumDurationPassed
    && gates.observerCohortPassed
  )
}

export function verifyObserverCohort(clients, markers, runId) {
  const results = clients.map((client) => verifyReconstructedMarkers(client, markers, runId))
  return {
    duplicates: results.reduce((total, result) => total + result.duplicates, 0),
    expectedPerObserver: markers.length,
    failedObservers: results.filter((result) => !result.passed).length,
    missing: results.reduce((total, result) => total + result.missing, 0),
    observed: results.reduce((total, result) => total + result.observed, 0),
    observerCount: clients.length,
    passed: results.length > 0 && results.every((result) => result.passed),
    unexpected: results.reduce((total, result) => total + result.unexpected, 0),
  }
}

export function summarizeApiProbe(baseline, loaded) {
  if (baseline === null && loaded === null) {
    return {
      absoluteLimitMs: API_P95_LIMIT_MS,
      absolutePassed: null,
      baselineP95Ms: null,
      degradationPercent: null,
      loadedP95Ms: null,
      reason: 'explicit_load_smoke_api_probe_opt_out',
      relativeGateApplied: null,
      relativeGateMinBaselineMs: API_RELATIVE_GATE_MIN_BASELINE_MS,
      relativeLimitPercent: API_DEGRADATION_LIMIT_PERCENT,
      relativePassed: null,
      samplesPerPhase: 0,
      status: 'skipped',
    }
  }
  if (!baseline || !loaded) throw new Error('API probe samples are incomplete.')
  const baselineP95Ms = percentile(baseline, 0.95)
  const loadedP95Ms = percentile(loaded, 0.95)
  if (baselineP95Ms <= 0) throw new Error('API probe baseline p95 must be greater than zero.')
  const degradationPercent = ((loadedP95Ms / baselineP95Ms) - 1) * 100
  const absolutePassed = loadedP95Ms <= API_P95_LIMIT_MS
  const relativeGateApplied = baselineP95Ms >= API_RELATIVE_GATE_MIN_BASELINE_MS
  const relativePassed = degradationPercent <= API_DEGRADATION_LIMIT_PERCENT
  let reason = null
  let status = 'passed'
  if (!absolutePassed) {
    reason = 'absolute_p95_above_limit'
    status = 'failed'
  } else if (relativeGateApplied && !relativePassed) {
    reason = 'relative_degradation_above_limit'
    status = 'failed'
  } else if (!relativeGateApplied && !relativePassed) {
    reason = 'relative_degradation_above_limit_below_stable_baseline'
    status = 'warning'
  }
  return {
    absoluteLimitMs: API_P95_LIMIT_MS,
    absolutePassed,
    baselineP95Ms,
    degradationPercent,
    loadedP95Ms,
    reason,
    relativeGateApplied,
    relativeGateMinBaselineMs: API_RELATIVE_GATE_MIN_BASELINE_MS,
    relativeLimitPercent: API_DEGRADATION_LIMIT_PERCENT,
    relativePassed,
    samplesPerPhase: API_PROBE_SAMPLES,
    status,
  }
}

export function evaluateGates(visibleLatencies, durableLatencies, apiProbe, options, load) {
  const visibleP95 = percentile(visibleLatencies, 0.95)
  const durableP95 = percentile(durableLatencies, 0.95)
  const minimumRounds = Math.min(...load.roundsPerWriter)
  const visibleUpdateTargetP95Ms = options.visibleUpdateP95Ms
  const smokeWarningEnabled = (
    options.mode === 'smoke'
    && visibleUpdateTargetP95Ms === CAPACITY_VISIBLE_P95_MS
  )
  // Sustained-load profiles carry the accepted large-state latency risk;
  // the production `capacity` gate deliberately does not.
  const acceptedLatencyBandEnabled = (
    options.mode === 'soak' || options.mode === 'ramp'
  )
  const visibleUpdateWarningEnabled = smokeWarningEnabled || acceptedLatencyBandEnabled
  const visibleUpdateHardLimitP95Ms = smokeWarningEnabled
    ? SMOKE_VISIBLE_WARNING_P95_MS
    : acceptedLatencyBandEnabled
      ? ACCEPTED_LATENCY_CEILING_P95_MS
      : visibleUpdateTargetP95Ms
  let visibleUpdateReason = null
  let visibleUpdateStatus = 'passed'
  if (visibleP95 >= visibleUpdateTargetP95Ms) {
    if (visibleUpdateWarningEnabled && visibleP95 <= visibleUpdateHardLimitP95Ms) {
      visibleUpdateReason = smokeWarningEnabled
        ? 'visible_update_above_target_within_smoke_limit'
        : 'visible_update_above_accepted_target'
      visibleUpdateStatus = 'warning'
    } else {
      visibleUpdateReason = 'visible_update_above_limit'
      visibleUpdateStatus = 'failed'
    }
  }
  return {
    apiLatencyAbsoluteLimitMs: API_P95_LIMIT_MS,
    apiLatencyDegradationLimitPercent: API_DEGRADATION_LIMIT_PERCENT,
    apiLatencyPassed: apiProbe.status === 'skipped' ? null : apiProbe.status !== 'failed',
    apiLatencyRelativeGateApplied: apiProbe.relativeGateApplied,
    apiLatencyRelativeGateMinBaselineMs: API_RELATIVE_GATE_MIN_BASELINE_MS,
    apiLatencyStatus: apiProbe.status,
    apiSampleSpanPassed: apiProbe.status === 'skipped'
      ? null
      : load.loadedApiSampleSpanMs >= options.minDurationMs,
    durableAckP95Ms: options.durableAckP95Ms,
    durableAckPassed: durableP95 < options.durableAckP95Ms,
    minimumAckRounds: options.minAckRoundsPerWriter,
    minimumAckRoundsPassed: (
      load.roundsPerWriter.length === options.writers
      && minimumRounds >= options.minAckRoundsPerWriter
    ),
    minimumDurationMs: options.minDurationMs,
    minimumDurationPassed: load.durationMs >= options.minDurationMs,
    observerCohort: options.observers,
    observerCohortPassed: load.observerCount >= options.observers,
    visibleUpdateHardLimitP95Ms,
    visibleUpdateP95Ms: visibleUpdateTargetP95Ms,
    visibleUpdatePassed: visibleUpdateStatus !== 'failed',
    visibleUpdateReason,
    visibleUpdateStatus,
    visibleUpdateTargetP95Ms,
    visibleUpdateWarningEnabled,
  }
}

export function allLoadGatesPassed(gates, reconstruction, sessionRotation) {
  return (
    gates.visibleUpdatePassed
    && gates.durableAckPassed
    && gates.apiLatencyStatus !== 'failed'
    && gates.apiSampleSpanPassed !== false
    && gates.minimumAckRoundsPassed
    && gates.minimumDurationPassed
    && gates.observerCohortPassed
    && reconstruction.passed
    && sessionRotation.passed
  )
}

export function summarize(values) {
  return { p50: percentile(values, 0.5), p95: percentile(values, 0.95) }
}

export function percentile(values, ratio) {
  if (!Array.isArray(values) || values.length === 0) {
    throw new Error('Percentile input must contain at least one sample.')
  }
  if (!(ratio > 0 && ratio <= 1)) throw new Error('Percentile ratio must be in (0, 1].')
  if (values.some((value) => !Number.isFinite(value) || value < 0)) {
    throw new Error('Percentile samples must be finite non-negative numbers.')
  }
  const sorted = [...values].sort((left, right) => left - right)
  return sorted[Math.max(0, Math.ceil(sorted.length * ratio) - 1)]
}

export function expectedAuthenticationScope(access) {
  if (access === 'view') return 'readonly'
  if (access === 'edit' || access === 'suggest') return 'read-write'
  throw new Error('Unknown collaboration access scope.')
}

export function encodeRoutedFrame(room, type, ...payloads) {
  return concatBytes(encodeString(room), encodeVarUint(type), ...payloads)
}

export function encodeVarUint(value) {
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error('Variable-length integer input must be a non-negative safe integer.')
  }
  const bytes = []
  let remaining = value
  while (remaining > 127) {
    bytes.push((remaining % 128) + 128)
    remaining = Math.floor(remaining / 128)
  }
  bytes.push(remaining)
  return Uint8Array.from(bytes)
}

export function encodeString(value) {
  return encodeBytes(new TextEncoder().encode(value))
}

export function encodeBytes(value) {
  return concatBytes(encodeVarUint(value.length), value)
}

export function concatBytes(...values) {
  const output = new Uint8Array(values.reduce((total, value) => total + value.length, 0))
  let offset = 0
  for (const value of values) {
    output.set(value, offset)
    offset += value.length
  }
  return output
}

export class ByteDecoder {
  constructor(bytes) {
    this.bytes = bytes
    this.offset = 0
  }

  get remaining() {
    return this.bytes.length - this.offset
  }

  readVarUint() {
    let value = 0
    let multiplier = 1
    while (this.offset < this.bytes.length) {
      const byte = this.bytes[this.offset++]
      value += (byte & 127) * multiplier
      if (byte < 128) return value
      multiplier *= 128
      if (!Number.isSafeInteger(value) || multiplier > Number.MAX_SAFE_INTEGER) break
    }
    throw new Error('Invalid variable-length integer.')
  }

  readBytes() {
    const length = this.readVarUint()
    const end = this.offset + length
    if (end > this.bytes.length) throw new Error('Truncated byte array.')
    const value = this.bytes.subarray(this.offset, end)
    this.offset = end
    return value
  }

  readString() {
    return new TextDecoder('utf-8', { fatal: true }).decode(this.readBytes())
  }

  assertDone() {
    if (this.remaining !== 0) throw new Error('Protocol frame contains trailing bytes.')
  }
}

export function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds))
}

function requestedMode(args) {
  const values = []
  for (let index = 0; index < args.length; index += 1) {
    if (args[index] === '--mode') values.push(nextArgument(args, index + 1, '--mode'))
  }
  if (values.length > 1) throw new Error('--mode may be supplied only once.')
  const mode = values[0] ?? 'smoke'
  if (mode !== 'smoke' && mode !== 'soak' && mode !== 'capacity' && mode !== 'ramp') {
    throw new Error('--mode must be smoke, soak, capacity, or ramp.')
  }
  return mode
}

function resolveWebSocketUrl(fixture, session, index) {
  const explicit = optionalString(session.websocket_url) ?? optionalString(fixture.websocket_url)
  let url
  try {
    if (explicit) url = new URL(explicit)
    else {
      const baseUrl = requiredString(fixture.base_url, 'fixture.base_url')
      const path = requiredString(session.websocket_path, `sessions[${index}].websocket_path`)
      url = new URL(path, baseUrl)
      url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:'
    }
  } catch {
    throw new Error(`sessions[${index}] WebSocket URL is invalid.`)
  }
  assertPublicEndpoint(url, `sessions[${index}] WebSocket URL`, ['ws:', 'wss:'])
  return url.toString()
}

function resolveOrigin(fixture, session, websocketUrl, index) {
  const configured = optionalString(session.origin) ?? optionalString(fixture.origin)
  let url
  try {
    url = configured
      ? new URL(configured)
      : optionalString(fixture.base_url)
        ? new URL(fixture.base_url)
        : new URL(websocketUrl.replace(/^ws/, 'http'))
  } catch {
    throw new Error(`sessions[${index}] origin is invalid.`)
  }
  assertPublicEndpoint(url, `sessions[${index}] origin`, ['http:', 'https:'])
  return url.origin
}

function assertPublicEndpoint(url, field, protocols) {
  if (!protocols.includes(url.protocol)) {
    throw new Error(`${field} uses an unsupported protocol.`)
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error(`${field} must not contain credentials, query, or fragment data.`)
  }
}

function webSocketHttpOrigin(websocketUrl) {
  const publicUrl = new URL(websocketUrl)
  publicUrl.protocol = publicUrl.protocol === 'wss:' ? 'https:' : 'http:'
  return publicUrl.origin
}

function assertInqtrixHealthPayload(payload, sampleIndex) {
  const validProvider = (value) => (
    isRecord(value)
    && typeof value.provider === 'string'
    && value.provider.trim().length > 0
    && typeof value.status === 'string'
    && value.status.trim().length > 0
  )
  if (
    !isRecord(payload)
    || payload.status !== 'ok'
    || !validProvider(payload.llm)
    || !validProvider(payload.search)
    || typeof payload.auth_mode !== 'string'
    || !isRecord(payload.legal)
  ) {
    throw new Error(
      `API probe request ${sampleIndex + 1}/${API_PROBE_SAMPLES} did not match the Inqtrix FastAPI /health schema.`,
    )
  }
}

async function waitForRecord(record, timeoutMs, fatal, observerCount) {
  const deadline = performance.now() + timeoutMs
  while (performance.now() < deadline) {
    fatal.throwIfSet()
    if (record.visibleByObserver.size === observerCount && record.ackAt !== null) return
    await delay(5)
  }
  throw new Error(
    'Timed out waiting for observer-cohort visibility and one durable acknowledgement.',
  )
}

function toUint8Array(data) {
  if (data instanceof ArrayBuffer) return new Uint8Array(data)
  if (ArrayBuffer.isView(data)) return new Uint8Array(data.buffer, data.byteOffset, data.byteLength)
  if (Array.isArray(data)) return concatBytes(...data.map(toUint8Array))
  throw new Error('Unsupported WebSocket frame representation.')
}

function sha256(value) {
  return createHash('sha256').update(value).digest('hex')
}

function nextArgument(args, index, name) {
  const value = args[index]
  if (!value) throw new Error(`${name} requires a value.`)
  return value
}

function positiveInteger(args, index, name) {
  const value = Number(nextArgument(args, index, name))
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new Error(`${name} requires a positive integer.`)
  }
  return value
}

function nonNegativeInteger(args, index, name) {
  const value = Number(nextArgument(args, index, name))
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new Error(`${name} requires a non-negative integer.`)
  }
  return value
}

function positiveSafeInteger(value, field) {
  const parsed = Number(value)
  if (!Number.isSafeInteger(parsed) || parsed < 1) {
    throw new Error(`${field} must be a positive integer.`)
  }
  return parsed
}

function sessionUserId(value, field) {
  if (!isRecord(value)) throw new Error(`${field} must be an object.`)
  return requiredString(value.id, `${field}.id`)
}

function collaborationDocumentId(room, field) {
  const match = COLLABORATION_ROOM_PATTERN.exec(room)
  if (!match) {
    throw new Error(`${field} must match the editor collaboration room contract.`)
  }
  return match[1]
}

function parseReissuedSession(raw, expected, control, nowSeconds, field) {
  if (!isRecord(raw)) throw new Error(`${field}.session must be an object.`)
  const access = requiredString(raw.access, `${field}.session.access`)
  const initialWriteMode = requiredString(
    raw.initial_write_mode,
    `${field}.session.initial_write_mode`,
  )
  const leaseToken = requiredString(raw.lease_token, `${field}.session.lease_token`)
  const room = requiredString(raw.room, `${field}.session.room`)
  const websocketPath = requiredString(
    raw.websocket_path,
    `${field}.session.websocket_path`,
  )
  const protocolVersion = positiveSafeInteger(
    raw.protocol_version,
    `${field}.session.protocol_version`,
  )
  const schemaVersion = positiveSafeInteger(
    raw.schema_version,
    `${field}.session.schema_version`,
  )
  const userId = sessionUserId(raw.user, `${field}.session.user`)
  const expiresAt = Number(raw.expires_at)
  const refreshAfter = Number(raw.refresh_after)
  const remainingSeconds = expiresAt - nowSeconds
  const minimumRemainingSeconds = Math.max(
    1,
    control.leaseTtlSeconds * (REISSUED_LEASE_MIN_REMAINING_SECONDS / CAPACITY_LEASE_TTL_SECONDS),
  )
  if (
    !Number.isFinite(expiresAt)
    || remainingSeconds < minimumRemainingSeconds
    || remainingSeconds > control.leaseTtlSeconds + REISSUED_LEASE_CLOCK_SKEW_SECONDS
  ) {
    throw new Error(`${field}.session.expires_at did not prove a freshly issued lease.`)
  }
  if (!Number.isFinite(refreshAfter) || refreshAfter <= nowSeconds || refreshAfter >= expiresAt) {
    throw new Error(`${field}.session.refresh_after must be in the future and precede expires_at.`)
  }
  if (
    access !== expected.access
    || initialWriteMode !== access
    || room !== expected.room
    || protocolVersion !== expected.protocolVersion
    || schemaVersion !== expected.schemaVersion
    || userId !== expected.userId
    || websocketPath !== new URL(expected.websocketUrl).pathname
  ) {
    throw new Error(`${field}.session changed immutable collaboration identity or protocol fields.`)
  }
  if (leaseToken === expected.leaseToken) {
    throw new Error(`${field}.session did not rotate the lease token.`)
  }
  return {
    access,
    expiresAt,
    leaseToken,
    origin: expected.origin,
    protocolVersion,
    refreshAfter,
    reissueId: expected.reissueId,
    room,
    schemaVersion,
    userId,
    websocketUrl: expected.websocketUrl,
  }
}

function assertReplacementSession(current, replacement, index, nowMilliseconds) {
  const nowSeconds = nowMilliseconds / 1_000
  if (
    !replacement
    || current.access !== replacement.access
    || current.origin !== replacement.origin
    || current.protocolVersion !== replacement.protocolVersion
    || current.reissueId !== replacement.reissueId
    || current.room !== replacement.room
    || current.schemaVersion !== replacement.schemaVersion
    || current.userId !== replacement.userId
    || current.websocketUrl !== replacement.websocketUrl
  ) {
    throw new Error(`Connection ${index} received a replacement lease for another session.`)
  }
  if (current.leaseToken === replacement.leaseToken) {
    throw new Error(`Connection ${index} did not receive a new lease token.`)
  }
  if (
    !Number.isFinite(replacement.expiresAt)
    || !Number.isFinite(replacement.refreshAfter)
    || replacement.refreshAfter <= nowSeconds
    || replacement.refreshAfter >= replacement.expiresAt
  ) {
    throw new Error(`Connection ${index} received invalid replacement lease timing.`)
  }
}

function assertCurrentLeaseUnexpired(session, nowMilliseconds, phase) {
  if (
    !session
    || !Number.isFinite(session.expiresAt)
    || !Number.isFinite(nowMilliseconds)
    || session.expiresAt * 1_000 <= nowMilliseconds
  ) {
    throw new Error(`A collaboration lease expired ${phase}.`)
  }
}

function requiredString(value, field) {
  const parsed = optionalString(value)
  if (!parsed) throw new Error(`${field} must be a non-empty string.`)
  return parsed
}

function optionalRunId(value, field) {
  if (value === undefined || value === null) return null
  const parsed = requiredString(value, field)
  if (!VERIFICATION_RUN_ID_PATTERN.test(parsed)) {
    throw new Error(`${field} must be a verification Run ID.`)
  }
  return parsed
}

function parseInstanceIdentity(value, field) {
  if (!isRecord(value)) {
    throw new Error(`Collaboration ${field} instance identity is missing.`)
  }
  const instanceId = requiredString(
    value.instance_id,
    `Collaboration ${field}.instance_id`,
  )
  const epoch = Number(value.epoch)
  if (!Number.isSafeInteger(epoch) || epoch < 1) {
    throw new Error(`Collaboration ${field}.epoch must be a positive integer.`)
  }
  return { epoch, instanceId }
}

function optionalString(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function isRecord(value) {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}
