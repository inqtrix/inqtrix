import { timingSafeEqual } from 'node:crypto'
import { createServer } from 'node:http'

import {
  CAPACITY_LEASE_TTL_SECONDS,
  LOAD_NETWORK_CONTROL_CONTRACT,
  SESSION_REISSUE_CONTRACT,
  SOAK_NETWORK_PHASES,
} from '../../load/collaboration-load-lib.mjs'
import { assertVerificationRunId } from './run-scope.mjs'

const MAX_BODY_BYTES = 128 * 1024
const MAX_REISSUE_BATCH = 100
const PHASE_IDS = new Set(SOAK_NETWORK_PHASES.map((phase) => phase.id))
const PURPOSES = new Set([
  'connected_rotation',
  'fresh_observer',
  'post_restart_observer',
  'scheduled_rotation',
])
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i

export const LOAD_CONTROL_PATHS = Object.freeze({
  networkPhase: '/control/network-phase',
  sessionReissue: '/control/session-reissue',
})

export function createRegisteredSessionReissuer({ issueSession }) {
  if (typeof issueSession !== 'function') {
    throw new Error('Registered session reissuer requires an issue callback.')
  }
  const registered = new Map()
  return {
    register(reissueId, actor, currentSession) {
      if (registered.has(reissueId)) {
        throw new Error(`Duplicate registered session reissue ID: ${reissueId}`)
      }
      registered.set(reissueId, { actor, currentSession })
    },
    reissueSession: async ({ purpose, reissueId, rotationCommandId }) => {
      const state = registered.get(reissueId)
      if (!state) return null
      const replacement = await issueSession({
        actor: state.actor,
        currentSession: state.currentSession,
        purpose,
        rotationCommandId,
      })
      state.currentSession = replacement
      return replacement
    },
  }
}

export async function startLoadControlServer({
  beforeNetworkPhase = async () => undefined,
  networkDriver,
  onNetworkPhase = () => undefined,
  reissueSession,
  runId,
  token,
}) {
  assertVerificationRunId(runId)
  if (typeof token !== 'string' || Buffer.byteLength(token, 'utf8') < 32) {
    throw new Error('Load-control authorization must contain at least 32 UTF-8 bytes.')
  }
  if (!networkDriver || typeof networkDriver.apply !== 'function') {
    throw new Error('Load-control requires a network shaping driver.')
  }
  if (typeof reissueSession !== 'function') {
    throw new Error('Load-control requires an authenticated session reissue callback.')
  }
  if (typeof beforeNetworkPhase !== 'function' || typeof onNetworkPhase !== 'function') {
    throw new Error('Load-control phase callbacks must be functions.')
  }

  const server = createServer(async (request, response) => {
    const fail = (status, reason) => json(response, status, { error: { reason } })
    try {
      if (!isLoopback(request.socket.remoteAddress)) return fail(403, 'loopback_required')
      if (request.method !== 'POST') return fail(405, 'method_not_allowed')
      if (!authorized(request.headers.authorization, token)) return fail(401, 'unauthorized')
      if (request.headers['x-inqtrix-verification-run-id'] !== runId) {
        return fail(409, 'run_scope_mismatch')
      }
      if (request.headers['content-type']?.split(';', 1)[0]?.trim() !== 'application/json') {
        return fail(415, 'content_type_required')
      }
      const body = await readJson(request)
      const path = new URL(request.url ?? '/', 'http://127.0.0.1').pathname
      if (path === LOAD_CONTROL_PATHS.networkPhase) {
        const phaseId = requireNetworkPhase(body)
        await beforeNetworkPhase(phaseId)
        await networkDriver.apply(phaseId)
        onNetworkPhase(phaseId)
        return json(response, 200, {
          contract: LOAD_NETWORK_CONTROL_CONTRACT,
          phase_id: phaseId,
          state: 'applied',
        })
      }
      if (path === LOAD_CONTROL_PATHS.sessionReissue) {
        const command = requireReissueCommand(body)
        const sessions = []
        for (const requested of command.sessions) {
          const session = await reissueSession({
            purpose: command.purpose,
            reissueId: requested.reissue_id,
            rotationCommandId: requested.rotation_command_id,
          })
          if (!session) return fail(404, 'session_not_found')
          sessions.push({ ...requested, session })
        }
        return json(response, 200, {
          contract: SESSION_REISSUE_CONTRACT,
          lease_ttl_seconds: CAPACITY_LEASE_TTL_SECONDS,
          sessions,
          source: 'fastapi_collaboration_session',
        })
      }
      return fail(404, 'not_found')
    } catch (error) {
      const status = error instanceof LoadControlRequestError ? error.status : 500
      return fail(status, status === 500 ? 'control_failed' : error.reason)
    }
  })

  await new Promise((resolvePromise, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolvePromise)
  })
  const address = server.address()
  if (!address || typeof address === 'string') {
    server.close()
    throw new Error('Load-control did not bind a loopback TCP address.')
  }
  return {
    baseURL: `http://127.0.0.1:${address.port}`,
    paths: LOAD_CONTROL_PATHS,
    async close() {
      await new Promise((resolvePromise, reject) => {
        server.close((error) => error ? reject(error) : resolvePromise())
      })
      await networkDriver.close()
    },
  }
}

function requireNetworkPhase(value) {
  if (
    !isRecord(value)
    || Object.keys(value).sort().join(',') !== 'contract,phase_id'
    || value.contract !== LOAD_NETWORK_CONTROL_CONTRACT
    || typeof value.phase_id !== 'string'
    || !PHASE_IDS.has(value.phase_id)
  ) throw new LoadControlRequestError(400, 'invalid_network_phase')
  return value.phase_id
}

function requireReissueCommand(value) {
  if (
    !isRecord(value)
    || Object.keys(value).sort().join(',') !== 'contract,lease_ttl_seconds,purpose,sessions'
    || value.contract !== SESSION_REISSUE_CONTRACT
    || value.lease_ttl_seconds !== CAPACITY_LEASE_TTL_SECONDS
    || !PURPOSES.has(value.purpose)
    || !Array.isArray(value.sessions)
    || value.sessions.length < 1
    || value.sessions.length > MAX_REISSUE_BATCH
  ) throw new LoadControlRequestError(400, 'invalid_reissue_request')
  const seen = new Set()
  for (const session of value.sessions) {
    if (
      !isRecord(session)
      || Object.keys(session).sort().join(',') !== 'reissue_id,rotation_command_id'
      || typeof session.reissue_id !== 'string'
      || session.reissue_id.length < 8
      || session.reissue_id.length > 120
      || !UUID_PATTERN.test(session.rotation_command_id)
      || seen.has(session.reissue_id)
    ) throw new LoadControlRequestError(400, 'invalid_reissue_request')
    seen.add(session.reissue_id)
  }
  return value
}

async function readJson(request) {
  const chunks = []
  let size = 0
  for await (const chunk of request) {
    size += chunk.length
    if (size > MAX_BODY_BYTES) throw new LoadControlRequestError(413, 'body_too_large')
    chunks.push(chunk)
  }
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'))
  } catch {
    throw new LoadControlRequestError(400, 'invalid_json')
  }
}

function authorized(header, expected) {
  const actual = typeof header === 'string' && header.startsWith('Bearer ')
    ? header.slice('Bearer '.length)
    : ''
  const actualBytes = Buffer.from(actual)
  const expectedBytes = Buffer.from(expected)
  return actualBytes.length === expectedBytes.length
    && timingSafeEqual(actualBytes, expectedBytes)
}

function isLoopback(value) {
  return value === '127.0.0.1' || value === '::1' || value === '::ffff:127.0.0.1'
}

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function json(response, status, payload) {
  response.writeHead(status, {
    'Cache-Control': 'no-store',
    'Content-Type': 'application/json; charset=utf-8',
  })
  response.end(`${JSON.stringify(payload)}\n`)
}

class LoadControlRequestError extends Error {
  constructor(status, reason) {
    super(reason)
    this.status = status
    this.reason = reason
  }
}
