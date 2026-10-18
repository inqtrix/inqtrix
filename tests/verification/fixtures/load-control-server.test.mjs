import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { afterEach, test } from 'node:test'

import {
  CAPACITY_LEASE_TTL_SECONDS,
  LOAD_NETWORK_CONTROL_CONTRACT,
  SESSION_REISSUE_CONTRACT,
} from '../../load/collaboration-load-lib.mjs'
import {
  createRegisteredSessionReissuer,
  LOAD_CONTROL_PATHS,
  startLoadControlServer,
} from './load-control-server.mjs'

const RUN_ID = 'inqv-load-control-test-01'
const TOKEN = 't'.repeat(43)
const servers = []

afterEach(async () => {
  while (servers.length > 0) await servers.pop().close()
})

test('load control requires bearer authorization and exact run scope', async () => {
  const server = await startServer()
  const body = {
    contract: LOAD_NETWORK_CONTROL_CONTRACT,
    phase_id: 'normal',
  }
  assert.equal((await post(server, LOAD_CONTROL_PATHS.networkPhase, body, {
    token: '',
  })).status, 401)
  assert.equal((await post(server, LOAD_CONTROL_PATHS.networkPhase, body, {
    runId: 'inqv-load-control-other-01',
  })).status, 409)
  assert.equal((await fetch(`${server.baseURL}${LOAD_CONTROL_PATHS.networkPhase}`, {
    headers: authorizationHeaders(),
    method: 'GET',
  })).status, 405)
})

test('load control applies only allowlisted network phases', async () => {
  const phases = []
  const callbacks = []
  const server = await startServer({
    beforeNetworkPhase: async (phaseId) => callbacks.push(`before:${phaseId}`),
    onNetworkPhase: (phaseId) => callbacks.push(`after:${phaseId}`),
    phases,
  })
  const valid = await post(server, LOAD_CONTROL_PATHS.networkPhase, {
    contract: LOAD_NETWORK_CONTROL_CONTRACT,
    phase_id: 'latency-100ms',
  })
  assert.equal(valid.status, 200)
  assert.deepEqual(phases, ['latency-100ms'])
  assert.deepEqual(callbacks, [
    'before:latency-100ms',
    'after:latency-100ms',
  ])
  assert.equal(valid.headers.get('cache-control'), 'no-store')

  const invalid = await post(server, LOAD_CONTROL_PATHS.networkPhase, {
    contract: LOAD_NETWORK_CONTROL_CONTRACT,
    phase_id: 'loss-50pct',
  })
  assert.equal(invalid.status, 400)
  assert.deepEqual(phases, ['latency-100ms'])
})

test('load control reissues only registered opaque sessions through its callback', async () => {
  const calls = []
  const server = await startServer({
    async reissueSession({ purpose, reissueId, rotationCommandId }) {
      calls.push({ purpose, reissueId, rotationCommandId })
      if (reissueId !== 'registered-session-01') return null
      return productSession(`lease-${calls.length}`)
    },
  })
  const rotation = randomUUID()
  const valid = await post(server, LOAD_CONTROL_PATHS.sessionReissue, {
    contract: SESSION_REISSUE_CONTRACT,
    lease_ttl_seconds: CAPACITY_LEASE_TTL_SECONDS,
    purpose: 'scheduled_rotation',
    sessions: [{
      reissue_id: 'registered-session-01',
      rotation_command_id: rotation,
    }],
  })
  assert.equal(valid.status, 200)
  const payload = await valid.json()
  assert.equal(payload.source, 'fastapi_collaboration_session')
  assert.equal(payload.sessions[0].rotation_command_id, rotation)
  assert.equal(payload.sessions[0].session.lease_token, 'lease-1')
  assert.deepEqual(calls, [{
    purpose: 'scheduled_rotation',
    reissueId: 'registered-session-01',
    rotationCommandId: rotation,
  }])

  const missing = await post(server, LOAD_CONTROL_PATHS.sessionReissue, {
    contract: SESSION_REISSUE_CONTRACT,
    lease_ttl_seconds: CAPACITY_LEASE_TTL_SECONDS,
    purpose: 'fresh_observer',
    sessions: [{
      reissue_id: 'unknown-session-02',
      rotation_command_id: randomUUID(),
    }],
  })
  assert.equal(missing.status, 404)
})

test('registered session reissue rotates from the current lease and advances only after success', async () => {
  const calls = []
  let rejectNext = true
  const registry = createRegisteredSessionReissuer({
    async issueSession(command) {
      calls.push(command)
      if (rejectNext) {
        rejectNext = false
        throw new Error('synthetic rotation failure')
      }
      return productSession(`replacement-${calls.length}`)
    },
  })
  const actor = { id: 'actor-1' }
  registry.register(
    'registered-session-01',
    actor,
    productSession('current-lease'),
  )
  const firstCommand = {
    purpose: 'connected_rotation',
    reissueId: 'registered-session-01',
    rotationCommandId: randomUUID(),
  }
  await assert.rejects(
    () => registry.reissueSession(firstCommand),
    /synthetic rotation failure/,
  )
  const replacement = await registry.reissueSession({
    ...firstCommand,
    rotationCommandId: randomUUID(),
  })
  assert.equal(replacement.lease_token, 'replacement-2')
  await registry.reissueSession({
    ...firstCommand,
    purpose: 'scheduled_rotation',
    rotationCommandId: randomUUID(),
  })
  assert.equal(calls[0].actor, actor)
  assert.equal(calls[0].currentSession.lease_token, 'current-lease')
  assert.equal(calls[1].currentSession.lease_token, 'current-lease')
  assert.equal(calls[2].currentSession.lease_token, 'replacement-2')
  assert.equal(calls[0].rotationCommandId, firstCommand.rotationCommandId)
  assert.equal(calls[2].purpose, 'scheduled_rotation')
  assert.equal(
    await registry.reissueSession({
      ...firstCommand,
      reissueId: 'unknown-session-02',
    }),
    null,
  )
  assert.throws(
    () => registry.register(
      'registered-session-01',
      actor,
      productSession('duplicate'),
    ),
    /Duplicate registered session reissue ID/,
  )
})

test('load control always normalizes the network driver on close', async () => {
  let closes = 0
  const server = await startServer({
    networkDriver: {
      async apply() {},
      async close() { closes += 1 },
    },
  })
  await server.close()
  servers.pop()
  assert.equal(closes, 1)
})

async function startServer({
  beforeNetworkPhase,
  networkDriver,
  onNetworkPhase,
  phases = [],
  reissueSession = async () => productSession('replacement'),
} = {}) {
  const server = await startLoadControlServer({
    beforeNetworkPhase,
    networkDriver: networkDriver ?? {
      async apply(phaseId) { phases.push(phaseId) },
      async close() {},
    },
    onNetworkPhase,
    reissueSession,
    runId: RUN_ID,
    token: TOKEN,
  })
  servers.push(server)
  return server
}

function post(server, path, body, {
  runId = RUN_ID,
  token = TOKEN,
} = {}) {
  return fetch(`${server.baseURL}${path}`, {
    body: JSON.stringify(body),
    headers: authorizationHeaders(runId, token),
    method: 'POST',
  })
}

function authorizationHeaders(runId = RUN_ID, token = TOKEN) {
  return {
    Authorization: `Bearer ${token}`,
    'Content-Type': 'application/json',
    'X-Inqtrix-Verification-Run-Id': runId,
  }
}

function productSession(leaseToken) {
  const now = Date.now() / 1_000
  return {
    access: 'edit',
    expires_at: now + 60,
    initial_write_mode: 'edit',
    lease_token: leaseToken,
    protocol_version: 1,
    refresh_after: now + 30,
    room: 'inqtrix-editor-v1:test:g1',
    schema_version: 1,
    user: { id: 'user-1' },
    websocket_path: '/collaboration',
  }
}
