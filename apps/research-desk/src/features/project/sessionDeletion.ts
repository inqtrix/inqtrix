import type { ServerDeletionOperation } from '@/api/inqtrixClient'

export type SessionDeletionState = {
  error: string | null
  operationId: string
  stage: string
  status: 'deleting' | 'delete_failed'
}

export class SessionDeletionContractError extends Error {}

const FAST_POLL_DELAY_MS = 300
const FAST_POLL_COUNT = 10
const MAX_POLL_DELAY_MS = 5_000

/** Delay before the next receipt poll, given how many polls already came
 * back non-terminal.
 *
 * The first {@link FAST_POLL_COUNT} polls keep a constant fast cadence,
 * because perceived deletion time is the server's work PLUS the wait for
 * the poll that notices it finished — backing off immediately would add
 * seconds of spinner to deletions that are already done. Only an
 * operation that has proven it will not finish in that window gets a
 * calmer cadence, and that is exactly the operation which may run until
 * the server's dispatch timeout expires it. Shared by the session and
 * asset receipts so the two cannot drift apart. */
export function nextDeletionPollDelayMs(completedPolls: number): number {
  if (completedPolls < FAST_POLL_COUNT) return FAST_POLL_DELAY_MS
  const grown = FAST_POLL_DELAY_MS * 2 ** (completedPolls - FAST_POLL_COUNT + 1)
  return Math.min(MAX_POLL_DELAY_MS, grown)
}

/** The wait after a first failed receipt read; the observation rule doubles
 * it for every further failure in a row. Shared by the session and asset
 * receipts, like the cadence above. */
export const DELETION_RETRY_BASE_MS = 750

export function assertSessionDeletionOperation(
  operation: ServerDeletionOperation,
  expectedKind: 'agent_session' | 'knowledge_session',
  expectedSessionId: string,
): void {
  if (
    operation.target_kind !== expectedKind
    || operation.target_id !== expectedSessionId
  ) {
    throw new SessionDeletionContractError(
      'Deletion receipt does not match the requested session',
    )
  }
}

export function sessionDeletionFromWire(value: {
  deletion_error?: string | null
  deletion_operation_id?: string | null
  deletion_stage?: string | null
  lifecycle_status?: 'active' | 'deleting' | 'delete_failed'
}): SessionDeletionState | undefined {
  const operationId = value.deletion_operation_id
  if (!operationId || value.lifecycle_status === 'active' || !value.lifecycle_status) {
    return undefined
  }
  return {
    error: value.deletion_error ?? null,
    operationId,
    stage: value.deletion_stage ?? value.lifecycle_status,
    status: value.lifecycle_status,
  }
}
