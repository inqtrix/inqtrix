import {
  mkdtempSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import type { ConnectionContext } from '../src/contracts'
import {
  parseVerificationFaultRecord,
  VerificationFaultGate,
  verificationFaultGateFromEnv,
} from '../src/verificationFault'

const RUN_ID = 'inqv-verification-fault-0001'
const OPERATION_ID = 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
const USER_ID = '11111111-1111-4111-8111-111111111111'
const HASH = 'a'.repeat(64)

describe('container-local verification fault gate', () => {
  it('is absent unless the process explicitly enables verification faults', () => {
    expect(verificationFaultGateFromEnv({})).toBeNull()
    expect(verificationFaultGateFromEnv({
      INQTRIX_COLLABORATION_VERIFICATION_FAULTS: '0',
    })).toBeNull()
    expect(verificationFaultGateFromEnv({
      INQTRIX_COLLABORATION_VERIFICATION_FAULTS: '1',
    })).toBeInstanceOf(VerificationFaultGate)
    expect(() => verificationFaultGateFromEnv({
      INQTRIX_COLLABORATION_VERIFICATION_FAULTS: 'true',
    })).toThrow(/must be 0 or 1/)
  })

  it('triggers a sidecar outage only after the selected actor update is durable', () => {
    withGate((gate, path) => {
      writeRecord(path, record({ kind: 'sidecar_outage' }))
      expect(gate.reload()).toMatchObject({ loaded: true, state: 'armed' })
      expect(statSync(path).mode & 0o077).toBe(0)
      expect(gate.triggerSidecarOutage(otherContext(), pending())).toBe(false)
      expect(gate.triggerSidecarOutage(context(), pending())).toBe(true)
      expect(gate.sidecarOutageActive()).toBe(true)
      expect(gate.current()).toMatchObject({
        close_code: 4503,
        durable_sequence: 12,
        projection_sequence: 11,
        state: 'outage',
      })
    })
  })

  it('suppresses one durable acknowledgement and records its later delivery as a duplicate', () => {
    withGate((gate, path) => {
      writeRecord(path, record({ kind: 'lost_ack' }))
      gate.reload()
      expect(gate.triggerLostAcknowledgement(context(), pending())).toBe(true)
      expect(gate.blocksConnection(context())).toBe(true)
      expect(gate.current()).toMatchObject({
        close_code: 1012,
        durability_reconciled: false,
        pending_durability_count: 1,
        state: 'triggered',
      })
      // Before the verifier releases the actor, a delivery is not recorded.
      gate.recordAcknowledgementDelivery(context(), { hash: HASH, sequence: 12 })
      expect(gate.current()).toMatchObject({
        durability_reconciled: false,
        reconciliation_sequence: null,
      })

      writeRecord(path, {
        ...gate.current()!,
        loaded: false,
        state: 'ready',
      })
      gate.reload()
      expect(gate.blocksConnection(context())).toBe(false)
      // Another actor's acknowledgement and another update's acknowledgement
      // leave the record open.
      gate.recordAcknowledgementDelivery(otherContext(), { hash: HASH, sequence: 12 })
      gate.recordAcknowledgementDelivery(context(), { hash: 'b'.repeat(64), sequence: 13 })
      expect(gate.current()).toMatchObject({
        durability_reconciled: false,
        pending_durability_count: 1,
        reconciliation_sequence: null,
      })
      gate.recordAcknowledgementDelivery(context(), { hash: HASH, sequence: 12 })
      expect(gate.current()).toMatchObject({
        durability_reconciled: true,
        pending_durability_count: 0,
        reconciliation_sequence: 12,
      })
    })
  })

  it('rejects a readable-by-group control record and clears stale state', () => {
    withGate((gate, path) => {
      writeFileSync(path, JSON.stringify(record({ kind: 'lost_ack' })), {
        mode: 0o640,
      })
      expect(() => gate.reload()).toThrow(/private regular file/)
      gate.reset()
      expect(gate.current()).toBeNull()
      expect(() => statSync(path)).toThrow()
    })
  })

  it('rejects malformed run scope and sequence fields', () => {
    expect(() => parseVerificationFaultRecord({
      ...record({ kind: 'lost_ack' }),
      run_id: '../wrong-run',
    })).toThrow(/run_id/)
    expect(() => parseVerificationFaultRecord({
      ...record({ kind: 'lost_ack' }),
      durable_sequence: -1,
    })).toThrow(/durable_sequence/)
  })
})

function withGate(run: (gate: VerificationFaultGate, path: string) => void): void {
  const directory = mkdtempSync(join(tmpdir(), 'inqtrix-verification-fault-'))
  const path = join(directory, 'fault.json')
  try {
    run(new VerificationFaultGate(path), path)
  } finally {
    rmSync(directory, { force: true, recursive: true })
  }
}

function writeRecord(path: string, value: unknown): void {
  writeFileSync(path, `${JSON.stringify(value)}\n`, { mode: 0o600 })
}

function record({ kind }: { kind: 'lost_ack' | 'sidecar_outage' }) {
  return {
    close_code: null,
    contract: 'inqtrix-collaboration-verification-fault-v1',
    document_id: 'ed_fault_document',
    durability_reconciled: null,
    durable_sequence: null,
    kind,
    loaded: false,
    operation_id: OPERATION_ID,
    pending_durability_count: null,
    projection_sequence: null,
    reconciliation_sequence: null,
    run_id: RUN_ID,
    state: 'armed',
    update_hash: null,
    user_id: USER_ID,
  }
}

function pending() {
  return {
    hash: HASH,
    persistedSequence: 12,
    projectionSequence: 11,
    sequence: 12,
  }
}

function context(): ConnectionContext {
  return {
    access: 'edit',
    documentId: 'ed_fault_document',
    expiresAt: Date.now() / 1_000 + 60,
    generation: 1,
    leaseId: 'lease-1',
    policyCursor: 0,
    protocolVersion: 1,
    schemaHash: 'schema-hash',
    schemaVersion: 2,
    sessionId: 'session-1',
    tenantId: 'tenant-1',
    user: { color: '#000000', id: USER_ID, name: 'Fault User' },
  }
}

function otherContext(): ConnectionContext {
  return {
    ...context(),
    user: {
      ...context().user,
      id: '22222222-2222-4222-8222-222222222222',
    },
  }
}
