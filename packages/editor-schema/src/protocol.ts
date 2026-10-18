export type CollaborationAccess = 'edit' | 'suggest' | 'view'
export type CollaborationActorKind = 'assistant' | 'agent' | 'guest' | 'human' | 'system'
export type CollaborationChangeKind = 'decision' | 'direct' | 'suggestion' | 'system'

export type CollaborationDurableAck = {
  hash: string
  sequence: number
  type: 'durable_ack'
}

export type CollaborationDurableRejection = {
  code: 'forbidden' | 'generation_mismatch' | 'invalid_schema' | 'rate_limited' | 'too_large'
  hash: string
  type: 'durable_rejection'
}

export type CollaborationServerMessage =
  | CollaborationDurableAck
  | CollaborationDurableRejection

/**
 * Close sent when a client update depends on Yjs state the room does not hold.
 *
 * The update is not written and the room stays as it was. The client resends
 * its unconfirmed updates in the order it produced them; a schema violation
 * keeps its own, permanent reason.
 */
export const COLLABORATION_CAUSAL_GAP_CLOSE = Object.freeze({
  code: 4412,
  reason: 'causal_gap',
} as const)

/**
 * Close sent when an open connection with edit or suggest access sends more
 * frames or bytes than the collaboration service holds for it while earlier
 * frames still wait.
 *
 * The frames the service held back are discarded unprocessed; frames it had
 * already handed on for processing are processed as usual. The client
 * reconnects and resends its unconfirmed updates in the order it produced
 * them.
 */
export const COLLABORATION_INGRESS_OVERFLOW_CLOSE = Object.freeze({
  code: 4429,
  reason: 'ingress_overflow',
} as const)

/**
 * Most frames the collaboration service queues on one socket for document
 * connections that have not opened yet (authentication and the room load).
 * One more frame resets the socket; the queued bytes are bounded by the frame
 * limit in the same way. A client that sends frames right after
 * authentication keeps them, together with its own control frames, within
 * this bound.
 */
export const COLLABORATION_PRE_OPEN_QUEUE_FRAMES = 64

export type CollaborationUpdateEnvelope = {
  actorKind: CollaborationActorKind
  changeKind: CollaborationChangeKind
  commandId?: string
  generation: number
  hash: string
  suggestionIds: string[]
  update: Uint8Array
}

export type CollaborationDecisionCommand = {
  commandId: string
  decision: 'accept' | 'reject'
  expectedSequence: number
  patchIds: string[]
}

export type CollaborationStateVector = {
  coveredSequence: number
  generation: number
  stateVector: Uint8Array
}

export type CollaborationSnapshot = CollaborationStateVector & {
  createdAt: number
  projectionHash: string
  schemaHash: string
  schemaVersion: number
  stateHash: string
  stateUpdate: Uint8Array
}

export type CollaborationProjection = {
  contentMarkdown: string
  coveredSequence: number
  generation: number
  projectionHash: string
  updatedAt: number
}

export function isCollaborationDurableAck(value: unknown): value is CollaborationDurableAck {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Partial<CollaborationDurableAck>
  return (
    candidate.type === 'durable_ack'
    && typeof candidate.hash === 'string'
    && Number.isSafeInteger(candidate.sequence)
    && (candidate.sequence ?? 0) > 0
  )
}
