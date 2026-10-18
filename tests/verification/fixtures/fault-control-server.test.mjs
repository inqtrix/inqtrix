import assert from 'node:assert/strict'
import { randomBytes } from 'node:crypto'
import { describe, test } from 'node:test'

import {
  FAULT_CONTROL_PATHS,
  parseContainerRuntimeState,
  startFaultControlServer,
} from './fault-control-server.mjs'

const RUN_ID = 'inqv-fault-control-test-01'
const DOCUMENT_ID = 'ed_fault_control_test'
const USER_ID = '11111111-1111-4111-8111-111111111111'

describe('loopback collaboration fault controller', () => {
  test('parses only the container runtime and health state', () => {
    assert.deepEqual(parseContainerRuntimeState(JSON.stringify({
      Health: { Status: 'healthy' },
      Status: 'running',
    })), { health: 'healthy', status: 'running' })
    assert.throws(
      () => parseContainerRuntimeState('{"Status":"running"}'),
      /does not expose a health state/,
    )
  })

  test('requires both bearer authorization and exact run scope', async () => {
    const token = randomBytes(32).toString('hex')
    const driver = fakeDriver()
    const server = await startFaultControlServer({
      allowedDocuments: { [DOCUMENT_ID]: [USER_ID] },
      driver,
      runId: RUN_ID,
      token,
    })
    try {
      const unauthorized = await fetch(new URL(
        FAULT_CONTROL_PATHS.armLostAck,
        server.baseURL,
      ), request({ token: 'wrong', runId: RUN_ID }))
      assert.equal(unauthorized.status, 401)
      const wrongRun = await fetch(new URL(
        FAULT_CONTROL_PATHS.armLostAck,
        server.baseURL,
      ), request({ token, runId: 'inqv-wrong-control-run-01' }))
      assert.equal(wrongRun.status, 409)
      assert.equal(driver.records.length, 0)
    } finally {
      await server.close()
    }
  })

  test('arms, reports, restores, and cleans one scoped lost ACK', async () => {
    const token = randomBytes(32).toString('hex')
    const driver = fakeDriver()
    const server = await startFaultControlServer({
      allowedDocuments: { [DOCUMENT_ID]: [USER_ID] },
      driver,
      runId: RUN_ID,
      token,
    })
    try {
      const armed = await invoke(server, FAULT_CONTROL_PATHS.armLostAck, {
        document_id: DOCUMENT_ID,
        user_id: USER_ID,
      }, token)
      assert.equal(armed.state, 'armed')
      assert.equal(armed.outage_layer, null)
      const status = await invoke(server, FAULT_CONTROL_PATHS.operationStatus, {
        operation_id: armed.operation_id,
      }, token)
      assert.equal(status.operation_id, armed.operation_id)
      const restored = await invoke(server, FAULT_CONTROL_PATHS.restore, {
        operation_id: armed.operation_id,
      }, token)
      assert.equal(restored.state, 'ready')
    } finally {
      await server.close()
    }
    assert.equal(driver.cleaned, true)
  })

  test('returns armed before independently stopping and restoring the gateway', async () => {
    const token = randomBytes(32).toString('hex')
    const driver = fakeDriver()
    const server = await startFaultControlServer({
      allowedDocuments: { [DOCUMENT_ID]: [USER_ID] },
      driver,
      runId: RUN_ID,
      token,
    })
    try {
      const armed = await invoke(server, FAULT_CONTROL_PATHS.armGatewayOutage, {
        document_id: DOCUMENT_ID,
        user_id: USER_ID,
      }, token)
      assert.equal(armed.state, 'armed')
      await new Promise((resolve) => setImmediate(resolve))
      const outage = await invoke(server, FAULT_CONTROL_PATHS.operationStatus, {
        operation_id: armed.operation_id,
      }, token)
      assert.equal(outage.state, 'outage')
      assert.equal(outage.outage_layer, 'fastapi_gateway')
      const restored = await invoke(server, FAULT_CONTROL_PATHS.restore, {
        operation_id: armed.operation_id,
      }, token)
      assert.equal(restored.state, 'ready')
      assert.equal(driver.gatewayStarted, 1)
      assert.equal(driver.gatewayStopped, 1)
    } finally {
      await server.close()
    }
  })
})

function fakeDriver() {
  return {
    cleaned: false,
    current: null,
    gatewayStarted: 0,
    gatewayStopped: 0,
    records: [],
    async arm(record) {
      this.current = { ...record, loaded: true }
      this.records.push(this.current)
      return this.current
    },
    async cleanup() { this.cleaned = true },
    async read(operationId) {
      assert.equal(this.current.operation_id, operationId)
      return this.current
    },
    async restartSidecar() {},
    async restore(operationId) {
      assert.equal(this.current.operation_id, operationId)
      this.current = { ...this.current, state: 'ready' }
      return this.current
    },
    async startGateway() { this.gatewayStarted += 1 },
    async stopGateway() { this.gatewayStopped += 1 },
  }
}

function request({ token, runId }) {
  return {
    body: JSON.stringify({ document_id: DOCUMENT_ID, user_id: USER_ID }),
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      'X-Inqtrix-Verification-Run-Id': runId,
    },
    method: 'POST',
  }
}

async function invoke(server, path, body, token) {
  const response = await fetch(new URL(path, server.baseURL), {
    body: JSON.stringify(body),
    headers: {
      Authorization: `Bearer ${token}`,
      'Content-Type': 'application/json',
      'X-Inqtrix-Verification-Run-Id': RUN_ID,
    },
    method: 'POST',
  })
  assert.equal(response.status, 200)
  return response.json()
}
