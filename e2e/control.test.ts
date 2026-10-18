import assert from 'node:assert/strict'
import { describe, test } from 'node:test'

import {
  CollaborationFixtureControlClient,
  controlRequestTimeoutMs,
  parseOperationState,
} from './control.ts'
import type { CollaborationControlFixture } from './config.ts'

const fixture: CollaborationControlFixture = {
  authorizationEnv: 'INQTRIX_E2E_CONTROL_TOKEN',
  baseURL: 'https://control.example.test',
  paths: {
    armGatewayOutage: '/gateway-outage:arm',
    armLostAck: '/lost-ack:arm',
    armOutage: '/outage:arm',
    operationStatus: '/operation:status',
    restart: '/restart',
    restore: '/restore',
  },
  runId: 'inqv-control-client-test-01',
}

describe('collaboration fixture controls', () => {
  test('allows recovery controls to wait for container health without widening fast controls', () => {
    assert.equal(controlRequestTimeoutMs('restore'), 45_000)
    assert.equal(controlRequestTimeoutMs('restart'), 45_000)
    assert.equal(controlRequestTimeoutMs('armLostAck'), 5_000)
    assert.equal(controlRequestTimeoutMs('operationStatus'), 5_000)
  })

  test('parses the observable fault state without dropping sequence fields', () => {
    assert.deepEqual(parseOperationState({
      close_code: 4503,
      durability_reconciled: true,
      durable_sequence: 41,
      operation_id: 'operation-1',
      outage_layer: 'collaboration_sidecar',
      pending_durability_count: 0,
      projection_sequence: 40,
      reconciliation_sequence: 41,
      state: 'outage',
    }), {
      closeCode: 4503,
      durabilityReconciled: true,
      durableSequence: 41,
      operationId: 'operation-1',
      outageLayer: 'collaboration_sidecar',
      pendingDurabilityCount: 0,
      projectionSequence: 40,
      reconciliationSequence: 41,
      state: 'outage',
    })
  })

  test('waits for explicit reconciliation and zero pending durability', async () => {
    const responses = [
      {
        durability_reconciled: false,
        durable_sequence: 41,
        operation_id: 'operation-1',
        pending_durability_count: 1,
        state: 'ready',
      },
      {
        durability_reconciled: true,
        durable_sequence: 41,
        operation_id: 'operation-1',
        pending_durability_count: 0,
        reconciliation_sequence: 41,
        state: 'ready',
      },
    ]
    const client = new CollaborationFixtureControlClient(
      fixture,
      { INQTRIX_E2E_CONTROL_TOKEN: 'test-control-token' },
      async () => new Response(JSON.stringify(responses.shift()), {
        headers: { 'Content-Type': 'application/json' },
        status: 200,
      }),
    )

    const state = await client.waitForDurabilityReconciliation('operation-1', 1_000)

    assert.equal(state.durabilityReconciled, true)
    assert.equal(state.pendingDurabilityCount, 0)
    assert.equal(state.reconciliationSequence, 41)
  })

  test('never exposes the bearer token when a control request fails', async () => {
    const sentinel = 'never-print-this-control-token'
    const failingFetch: typeof fetch = async () => {
      throw new Error(`transport accidentally included ${sentinel}`)
    }
    const client = new CollaborationFixtureControlClient(
      fixture,
      { INQTRIX_E2E_CONTROL_TOKEN: sentinel },
      failingFetch,
    )

    await assert.rejects(
      () => client.armLostAck('document-id', 'user-id'),
      (error: unknown) => {
        assert(error instanceof Error)
        assert.doesNotMatch(error.message, new RegExp(sentinel))
        assert.match(error.message, /failed before receiving a response/)
        return true
      },
    )
  })

  test('uses a distinct authenticated control path for the FastAPI gateway outage', async () => {
    let observedUrl = ''
    let observedBody: unknown = null
    let observedRunId = ''
    const client = new CollaborationFixtureControlClient(
      fixture,
      { INQTRIX_E2E_CONTROL_TOKEN: 'test-control-token' },
      async (input, init) => {
        observedUrl = String(input)
        observedBody = JSON.parse(String(init?.body))
        observedRunId = new Headers(init?.headers).get(
          'X-Inqtrix-Verification-Run-Id',
        ) ?? ''
        return new Response(JSON.stringify({
          operation_id: 'gateway-operation',
          outage_layer: 'fastapi_gateway',
          state: 'armed',
        }), {
          headers: { 'Content-Type': 'application/json' },
          status: 200,
        })
      },
    )

    const operation = await client.armGatewayOutage('document-id', 'user-id')

    assert.equal(observedUrl, 'https://control.example.test/gateway-outage:arm')
    assert.deepEqual(observedBody, { document_id: 'document-id', user_id: 'user-id' })
    assert.equal(observedRunId, fixture.runId)
    assert.equal(operation.outageLayer, 'fastapi_gateway')
  })

  test('rejects malformed operation states instead of treating them as ready', () => {
    assert.throws(
      () => parseOperationState({ operation_id: 'operation-1', state: 'done' }),
      /state is invalid/,
    )
  })

  test('rejects an unrecognized outage layer instead of conflating fault targets', () => {
    assert.throws(
      () => parseOperationState({
        operation_id: 'operation-1',
        outage_layer: 'generic_gateway',
        state: 'outage',
      }),
      /must identify collaboration_sidecar or fastapi_gateway/,
    )
  })
})
