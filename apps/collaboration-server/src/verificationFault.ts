import {
  lstatSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from 'node:fs'

import type { ConnectionContext } from './contracts'

export const VERIFICATION_FAULT_FILE =
  '/tmp/inqtrix-collaboration-verification-fault.json'
export const VERIFICATION_FAULT_ENV =
  'INQTRIX_COLLABORATION_VERIFICATION_FAULTS'

const CONTRACT = 'inqtrix-collaboration-verification-fault-v1'
const RUN_ID = /^inqv-[a-z0-9][a-z0-9-]{7,75}$/
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const DOCUMENT_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,191}$/

export type VerificationFaultKind = 'lost_ack' | 'sidecar_outage'
export type VerificationFaultState = 'armed' | 'failed' | 'outage' | 'ready' | 'triggered'

export type VerificationFaultRecord = {
  close_code: number | null
  contract: typeof CONTRACT
  document_id: string
  durability_reconciled: boolean | null
  durable_sequence: number | null
  kind: VerificationFaultKind
  loaded: boolean
  operation_id: string
  pending_durability_count: number | null
  projection_sequence: number | null
  reconciliation_sequence: number | null
  run_id: string
  state: VerificationFaultState
  update_hash: string | null
  user_id: string
}

export type PendingClientUpdate = {
  hash: string
  persistedSequence: number
  projectionSequence: number
  sequence: number
}

export function verificationFaultGateFromEnv(
  env: Readonly<Record<string, string | undefined>>,
  path = VERIFICATION_FAULT_FILE,
): VerificationFaultGate | null {
  const configured = env[VERIFICATION_FAULT_ENV]?.trim()
  if (configured === undefined || configured === '' || configured === '0') {
    return null
  }
  if (configured !== '1') {
    throw new Error(`${VERIFICATION_FAULT_ENV} must be 0 or 1`)
  }
  return new VerificationFaultGate(path)
}

/**
 * A deliberately container-local verification seam for deterministic transport
 * faults. It has no listener and no product route: the external verifier must
 * already have container-exec authority, write a private record, and signal the
 * process before this class can affect a connection.
 */
export class VerificationFaultGate {
  private record: VerificationFaultRecord | null = null

  constructor(private readonly path = VERIFICATION_FAULT_FILE) {}

  reset(): void {
    this.record = null
    try {
      unlinkSync(this.path)
    } catch (error) {
      if (!isMissingFile(error)) throw error
    }
  }

  reload(): VerificationFaultRecord {
    const metadata = lstatSync(this.path)
    if (!metadata.isFile() || (metadata.mode & 0o077) !== 0) {
      throw new Error('The collaboration verification fault record must be a private regular file.')
    }
    const record = parseVerificationFaultRecord(
      JSON.parse(readFileSync(this.path, 'utf8')),
    )
    this.record = { ...record, loaded: true }
    this.persist()
    return { ...this.record }
  }

  current(): VerificationFaultRecord | null {
    return this.record ? { ...this.record } : null
  }

  sidecarOutageActive(): boolean {
    return this.record?.kind === 'sidecar_outage'
      && this.record.state === 'outage'
  }

  blocksConnection(context: ConnectionContext): boolean {
    const current = this.record
    if (!current) return false
    if (current.kind === 'sidecar_outage' && current.state === 'outage') return true
    return current.kind === 'lost_ack'
      && current.state === 'triggered'
      && matchesActor(current, context)
  }

  triggerSidecarOutage(
    context: ConnectionContext,
    pending: PendingClientUpdate | null,
  ): boolean {
    const current = this.record
    if (
      !current
      || current.kind !== 'sidecar_outage'
      || current.state !== 'armed'
      || !matchesActor(current, context)
      || !pending
    ) return false
    this.record = {
      ...current,
      close_code: 4503,
      durable_sequence: pending.persistedSequence,
      projection_sequence: pending.projectionSequence,
      state: 'outage',
      update_hash: pending.hash,
    }
    this.persist()
    return true
  }

  triggerLostAcknowledgement(
    context: ConnectionContext,
    pending: PendingClientUpdate | null,
  ): boolean {
    const current = this.record
    if (
      !current
      || current.kind !== 'lost_ack'
      || current.state !== 'armed'
      || !matchesActor(current, context)
      || !pending
    ) return false
    this.record = {
      ...current,
      close_code: 1012,
      durability_reconciled: false,
      durable_sequence: pending.persistedSequence,
      pending_durability_count: 1,
      projection_sequence: pending.persistedSequence,
      state: 'triggered',
      update_hash: pending.hash,
    }
    this.persist()
    return true
  }

  /** Records that the actor received, on a later connection, the durable
   * acknowledgement this gate suppressed: the client resent the update and
   * the service acknowledged it as a duplicate. */
  recordAcknowledgementDelivery(
    context: ConnectionContext,
    acknowledgement: { hash: string; sequence: number },
  ): void {
    const current = this.record
    if (
      !current
      || current.kind !== 'lost_ack'
      || current.state !== 'ready'
      || !matchesActor(current, context)
      || !current.update_hash
      || acknowledgement.hash !== current.update_hash
    ) return
    this.record = {
      ...current,
      durability_reconciled: true,
      pending_durability_count: 0,
      reconciliation_sequence: acknowledgement.sequence,
    }
    this.persist()
  }

  private persist(): void {
    if (!this.record) return
    const temporary = `${this.path}.${process.pid}.tmp`
    writeFileSync(temporary, `${JSON.stringify(this.record)}\n`, {
      encoding: 'utf8',
      mode: 0o600,
    })
    renameSync(temporary, this.path)
  }
}

export function parseVerificationFaultRecord(value: unknown): VerificationFaultRecord {
  if (!isRecord(value)) throw new Error('The collaboration verification fault record is invalid.')
  if (value.contract !== CONTRACT) throw new Error('The collaboration verification fault contract is invalid.')
  const runId = requiredPattern(value.run_id, RUN_ID, 'run_id')
  const operationId = requiredPattern(value.operation_id, UUID, 'operation_id')
  const documentId = requiredPattern(value.document_id, DOCUMENT_ID, 'document_id')
  const userId = requiredPattern(value.user_id, UUID, 'user_id')
  if (value.kind !== 'lost_ack' && value.kind !== 'sidecar_outage') {
    throw new Error('The collaboration verification fault kind is invalid.')
  }
  if (!['armed', 'failed', 'outage', 'ready', 'triggered'].includes(String(value.state))) {
    throw new Error('The collaboration verification fault state is invalid.')
  }
  if (typeof value.loaded !== 'boolean') {
    throw new Error('The collaboration verification fault loaded flag is invalid.')
  }
  return {
    close_code: optionalInteger(value.close_code, 'close_code'),
    contract: CONTRACT,
    document_id: documentId,
    durability_reconciled: optionalBoolean(
      value.durability_reconciled,
      'durability_reconciled',
    ),
    durable_sequence: optionalInteger(value.durable_sequence, 'durable_sequence'),
    kind: value.kind,
    loaded: value.loaded,
    operation_id: operationId,
    pending_durability_count: optionalInteger(
      value.pending_durability_count,
      'pending_durability_count',
    ),
    projection_sequence: optionalInteger(value.projection_sequence, 'projection_sequence'),
    reconciliation_sequence: optionalInteger(
      value.reconciliation_sequence,
      'reconciliation_sequence',
    ),
    run_id: runId,
    state: value.state as VerificationFaultState,
    update_hash: optionalHash(value.update_hash),
    user_id: userId,
  }
}

function matchesActor(record: VerificationFaultRecord, context: ConnectionContext): boolean {
  return record.document_id === context.documentId && record.user_id === context.user.id
}

function optionalBoolean(value: unknown, field: string): boolean | null {
  if (value === null) return null
  if (typeof value !== 'boolean') throw new Error(`The collaboration verification fault ${field} is invalid.`)
  return value
}

function optionalInteger(value: unknown, field: string): number | null {
  if (value === null) return null
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new Error(`The collaboration verification fault ${field} is invalid.`)
  }
  return Number(value)
}

function optionalHash(value: unknown): string | null {
  if (value === null) return null
  if (typeof value !== 'string' || !/^[a-f0-9]{64}$/.test(value)) {
    throw new Error('The collaboration verification fault update_hash is invalid.')
  }
  return value
}

function requiredPattern(
  value: unknown,
  pattern: RegExp,
  field: string,
): string {
  if (typeof value !== 'string' || !pattern.test(value)) {
    throw new Error(`The collaboration verification fault ${field} is invalid.`)
  }
  return value
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function isMissingFile(error: unknown): boolean {
  return Boolean(
    error
    && typeof error === 'object'
    && 'code' in error
    && error.code === 'ENOENT',
  )
}
