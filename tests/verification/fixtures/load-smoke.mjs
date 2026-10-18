import { assertVerificationRunId } from './run-scope.mjs'
import { writePrivateJsonFixture } from './private-json.mjs'
import {
  buildLargeCollaborationDocumentSeed,
} from './collaboration-document-state.mjs'
import {
  CAPACITY_LEASE_TTL_SECONDS,
  LOAD_NETWORK_CONTROL_CONTRACT,
  SESSION_REISSUE_CONTRACT,
} from '../../load/collaboration-load-lib.mjs'

export const LOAD_SMOKE_CONNECTIONS = 20
export const LOAD_SMOKE_IDENTITIES = 4
export const LOAD_SMOKE_SESSIONS_PER_IDENTITY = 5
export const LOAD_SMOKE_WRITERS = 5
export const LOAD_SOAK_CONNECTIONS = 25
export const LOAD_SOAK_WRITERS = 5
export const LOAD_SOAK_COMMENTERS = 5
export const LOAD_SOAK_READERS = 10
export const LOAD_SOAK_FEATURE_ACTORS = 5

export function buildLoadDocumentSeed({
  loadProfile,
  requestedProfile,
  runId,
}) {
  assertVerificationRunId(runId)
  if (!['load-smoke', 'load-soak', 'load-ramp'].includes(loadProfile)) {
    throw new Error(
      'Generated load document seed supports load-smoke, load-soak, or load-ramp only.',
    )
  }
  if (
    requestedProfile !== undefined
    && requestedProfile !== null
    && typeof requestedProfile !== 'string'
  ) {
    throw new Error('INQTRIX_LOAD_SMOKE_DOCUMENT_PROFILE must be a string.')
  }
  const requested = requestedProfile?.trim() ?? ''
  if (loadProfile !== 'load-smoke' && requested) {
    throw new Error(
      'INQTRIX_LOAD_SMOKE_DOCUMENT_PROFILE is supported by load-smoke only.',
    )
  }
  const profile = requested || 'standard'
  if (!['standard', 'large-state'].includes(profile)) {
    throw new Error(
      'INQTRIX_LOAD_SMOKE_DOCUMENT_PROFILE must be standard or large-state.',
    )
  }

  if (profile === 'large-state') {
    return {
      ...buildLargeCollaborationDocumentSeed({ runId }),
      profile,
    }
  }
  const paragraphs = [`Run ${runId}. Synthetische Lastdaten.`]
  const markdown = `# System\n\n${paragraphs[0]}`
  return {
    characterCount: markdown.length,
    markdown,
    paragraphCount: paragraphs.length,
    profile,
  }
}

export function normalizeLoadSmokeBaseURL(value) {
  let parsed
  try {
    parsed = new URL(value ?? 'http://127.0.0.1:8080')
  } catch {
    throw new Error('INQTRIX_E2E_BASE_URL must be a valid HTTP(S) URL.')
  }
  if (
    !['http:', 'https:'].includes(parsed.protocol)
    || parsed.username
    || parsed.password
    || parsed.search
    || parsed.hash
  ) {
    throw new Error(
      'INQTRIX_E2E_BASE_URL must be a credential-free HTTP(S) URL.',
    )
  }
  return parsed.origin
}

/** Fixture for the LOCAL capacity ramp.
 *
 * Unlike the smoke fixture this one is variable-sized: the ramp reuses the
 * same room and the same capped identity pool across every rung, so the
 * only thing that grows is the socket count. The identity ceiling is
 * asserted here rather than assumed, because it is exactly what keeps the
 * ramp an honest fan-out proof instead of a capacity release.
 */
export function buildLoadRampFixture({
  baseURL,
  identityCeiling,
  runId,
  sessions,
  writers,
}) {
  assertVerificationRunId(runId)
  if (!Array.isArray(sessions) || sessions.length === 0) {
    throw new Error('Load-ramp provisioning produced no sessions.')
  }
  const normalized = sessions.map((session, index) => {
    const row = requireSession(session, index)
    return minimizeSession(row, runId, index)
  })
  if (new Set(normalized.map((session) => session.room)).size !== 1) {
    throw new Error('Every generated load-ramp session must use one room.')
  }
  const identities = new Set(normalized.map((session) => session.user.id))
  if (identities.size > identityCeiling) {
    throw new Error(
      `Load-ramp exceeded its identity ceiling of ${identityCeiling}.`,
    )
  }
  if (normalized.slice(0, writers).some((session) => session.access !== 'edit')) {
    throw new Error(
      `The first ${writers} load-ramp sessions must be edit-capable.`,
    )
  }
  return {
    api_probe: {
      contract: 'inqtrix-health-v1',
      url: '/health',
    },
    base_url: normalizeLoadSmokeBaseURL(baseURL),
    sessions: normalized,
    version: 2,
  }
}

export function buildLoadSmokeFixture({
  baseURL,
  runId,
  sessions,
}) {
  assertVerificationRunId(runId)
  if (
    !Array.isArray(sessions)
    || sessions.length !== LOAD_SMOKE_CONNECTIONS
  ) {
    throw new Error(
      `Load-smoke provisioning must produce exactly ${LOAD_SMOKE_CONNECTIONS} sessions.`,
    )
  }
  const normalized = sessions.map((session, index) => {
    const row = requireSession(session, index)
    return minimizeSession(row, runId, index)
  })
  const rooms = new Set(normalized.map((session) => session.room))
  if (rooms.size !== 1) {
    throw new Error('Every generated load-smoke session must use one room.')
  }
  const perIdentity = new Map()
  for (const session of normalized) {
    perIdentity.set(
      session.user.id,
      (perIdentity.get(session.user.id) ?? 0) + 1,
    )
  }
  if (
    perIdentity.size !== LOAD_SMOKE_IDENTITIES
    || [...perIdentity.values()].some(
      (count) => count !== LOAD_SMOKE_SESSIONS_PER_IDENTITY,
    )
  ) {
    throw new Error(
      `Load-smoke requires ${LOAD_SMOKE_IDENTITIES} identities with `
      + `${LOAD_SMOKE_SESSIONS_PER_IDENTITY} sessions each.`,
    )
  }
  if (
    normalized.slice(0, LOAD_SMOKE_WRITERS)
      .some((session) => session.access !== 'edit')
  ) {
    throw new Error(
      `The first ${LOAD_SMOKE_WRITERS} load-smoke sessions must be edit-capable.`,
    )
  }
  return {
    api_probe: {
      contract: 'inqtrix-health-v1',
      url: '/health',
    },
    base_url: normalizeLoadSmokeBaseURL(baseURL),
    sessions: normalized,
    version: 2,
  }
}

export function buildLoadSoakFixture({
  baseURL,
  controls,
  runId,
  sessions,
}) {
  assertVerificationRunId(runId)
  if (!Array.isArray(sessions) || sessions.length !== LOAD_SOAK_CONNECTIONS) {
    throw new Error(
      `Load-soak provisioning must produce exactly ${LOAD_SOAK_CONNECTIONS} sessions.`,
    )
  }
  const normalized = sessions.map((session, index) => {
    const row = requireSession(session, index, ['edit', 'suggest', 'view'])
    return minimizeSession(row, runId, index, 'soak')
  })
  if (new Set(normalized.map((session) => session.room)).size !== 1) {
    throw new Error('Every generated load-soak session must use one room.')
  }
  if (new Set(normalized.map((session) => session.user.id)).size !== LOAD_SOAK_CONNECTIONS) {
    throw new Error('Load-soak requires exactly 25 distinct session identities.')
  }
  const expectedAccess = [
    ...Array.from({ length: LOAD_SOAK_WRITERS }, () => 'edit'),
    ...Array.from({ length: LOAD_SOAK_COMMENTERS }, () => 'suggest'),
    ...Array.from({ length: LOAD_SOAK_READERS + LOAD_SOAK_FEATURE_ACTORS }, () => 'view'),
  ]
  if (normalized.some((session, index) => session.access !== expectedAccess[index])) {
    throw new Error(
      'Load-soak sessions must be ordered as 5 edit, 5 suggest, and 15 view identities.',
    )
  }
  const normalizedControls = normalizeSoakControls(controls)
  return {
    api_probe: {
      contract: 'inqtrix-health-v1',
      url: '/health',
    },
    base_url: normalizeLoadSmokeBaseURL(baseURL),
    network_control: {
      authorization_env: normalizedControls.authorizationEnv,
      contract: LOAD_NETWORK_CONTROL_CONTRACT,
      run_id: runId,
      url: new URL(normalizedControls.networkPath, normalizedControls.baseURL).toString(),
    },
    sessions: normalized,
    session_reissue: {
      authorization_env: normalizedControls.authorizationEnv,
      contract: SESSION_REISSUE_CONTRACT,
      lease_ttl_seconds: CAPACITY_LEASE_TTL_SECONDS,
      run_id: runId,
      url: new URL(normalizedControls.reissuePath, normalizedControls.baseURL).toString(),
    },
    version: 2,
  }
}

export async function writePrivateLoadSmokeFixture(path, fixture) {
  await writePrivateJsonFixture(path, fixture)
}

function requireSession(value, index, allowedAccess = ['edit']) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`Generated load-smoke session ${index + 1} is invalid.`)
  }
  const requiredStrings = [
    'access',
    'initial_write_mode',
    'lease_token',
    'room',
    'websocket_path',
  ]
  for (const field of requiredStrings) {
    if (typeof value[field] !== 'string' || value[field].length === 0) {
      throw new Error(
        `Generated load-smoke session ${index + 1} has no ${field}.`,
      )
    }
  }
  if (!allowedAccess.includes(value.access) || value.initial_write_mode !== value.access) {
    throw new Error(
      `Generated load session ${index + 1} has an invalid access mode.`,
    )
  }
  for (const field of ['expires_at', 'refresh_after']) {
    if (!Number.isFinite(Number(value[field]))) {
      throw new Error(
        `Generated load-smoke session ${index + 1} has invalid ${field}.`,
      )
    }
  }
  for (const field of ['protocol_version', 'schema_version']) {
    if (!Number.isSafeInteger(value[field]) || value[field] < 1) {
      throw new Error(
        `Generated load-smoke session ${index + 1} has invalid ${field}.`,
      )
    }
  }
  if (
    !value.user
    || typeof value.user !== 'object'
    || typeof value.user.id !== 'string'
    || value.user.id.length === 0
  ) {
    throw new Error(
      `Generated load-smoke session ${index + 1} has no user identity.`,
    )
  }
  return value
}

function minimizeSession(row, runId, index, profile = 'load') {
  return {
    access: row.access,
    expires_at: row.expires_at,
    initial_write_mode: row.initial_write_mode,
    lease_token: row.lease_token,
    protocol_version: row.protocol_version,
    refresh_after: row.refresh_after,
    reissue_id: `${runId}-${profile}-${String(index + 1).padStart(2, '0')}`,
    room: row.room,
    schema_version: row.schema_version,
    user: { id: row.user.id },
    websocket_path: row.websocket_path,
  }
}

function normalizeSoakControls(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('Load-soak controls are required.')
  }
  const authorizationEnv = value.authorizationEnv
  if (typeof authorizationEnv !== 'string' || !/^[A-Z][A-Z0-9_]*$/.test(authorizationEnv)) {
    throw new Error('Load-soak control authorizationEnv is invalid.')
  }
  let baseURL
  try {
    baseURL = new URL(value.baseURL)
  } catch {
    throw new Error('Load-soak control baseURL is invalid.')
  }
  if (
    baseURL.protocol !== 'http:'
    || baseURL.hostname !== '127.0.0.1'
    || baseURL.username
    || baseURL.password
    || baseURL.search
    || baseURL.hash
    || baseURL.pathname !== '/'
  ) throw new Error('Load-soak control baseURL must be an uncredentialed 127.0.0.1 HTTP origin.')
  const path = (field) => {
    const candidate = value[field]
    if (
      typeof candidate !== 'string'
      || !candidate.startsWith('/')
      || candidate.startsWith('//')
      || candidate.includes('?')
      || candidate.includes('#')
    ) throw new Error(`Load-soak control ${field} is invalid.`)
    return candidate
  }
  return {
    authorizationEnv,
    baseURL,
    networkPath: path('networkPath'),
    reissuePath: path('reissuePath'),
  }
}
