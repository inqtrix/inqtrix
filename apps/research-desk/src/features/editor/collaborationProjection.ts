import {
  decideEditorCollaborationPatches,
  flushEditorCollaborationProjection,
  type ClientOptions,
  type EditorCollaborationDecisionResponse,
  type EditorCollaborationProjection,
} from '@/api/inqtrixClient'
import type { EditorDocumentRecord } from '@/features/project/types'
import {
  beginCollaborationAuthorityGuard,
  type CollaborationAuthorityGuard,
  type CollaborationAuthoritySource,
} from './collaborationAuthority'
import type { CollaborationDocumentHandle } from './useCollaborationDocument'

export type CollaborationProjectionBarrierCode =
  | 'authoritative_sequence_invalid'
  | 'command_response_invalid'
  | 'local_barrier_unavailable'
  | 'local_sequence_invalid'
  | 'projection_behind_local'
  | 'projection_generation_mismatch'
  | 'projection_not_authoritative'
  | 'projection_invalid'

export class CollaborationProjectionBarrierError extends Error {
  readonly code: CollaborationProjectionBarrierCode

  constructor(code: CollaborationProjectionBarrierCode, message: string) {
    super(message)
    this.code = code
    this.name = 'CollaborationProjectionBarrierError'
  }
}

export type CollaborationProjectionController = CollaborationAuthoritySource & {
  flushAndAwaitDurable: () => Promise<number>
  setAuthoritativeSequence: (sequence: number) => void
}

export type ConfirmedCollaborationProjection = {
  confirmedAt: string
  markdown: string
  sequence: number
}

export type CollaborationProjectionFallback = {
  confirmedAt: string
  markdown: string
}

type ProjectionBarrierOptions = {
  authorityGuard?: CollaborationAuthorityGuard | null
  clientOptions: ClientOptions
  controller: CollaborationProjectionController | null
  documentId: string
  generation: number | null | undefined
  flushProjection?: (
    documentId: string,
    options: ClientOptions,
  ) => Promise<EditorCollaborationProjection>
  now?: () => Date
  requireLocal?: boolean
}

type CollaborationDecisionOptions = {
  authorityGuard?: CollaborationAuthorityGuard | null
  clientOptions: ClientOptions
  controller: CollaborationProjectionController | null
  decide?: typeof decideEditorCollaborationPatches
  decision: 'accept' | 'reject'
  decisionId: string
  documentId: string
  generation: number | null | undefined
  flushProjection?: ProjectionBarrierOptions['flushProjection']
  patchIds: readonly string[]
}

/** Adapt the collaboration lifecycle's public durability methods without
 * making projection callers depend on the controller's implementation. */
export function collaborationProjectionController(
  handle: CollaborationDocumentHandle,
  documentId = handle.documentId,
  generation = handle.generation,
): CollaborationProjectionController | null {
  if (
    handle.lifecycleStatus === 'inactive'
    || handle.documentId === null
    || handle.generation === null
    || handle.documentId !== documentId
    || handle.generation !== generation
  ) return null
  return {
    flushAndAwaitDurable: handle.flushAndAwaitDurable,
    readAuthority: handle.readAuthority,
    setAuthoritativeSequence: handle.setAuthoritativeSequence,
  }
}

/** Drain the browser's 50 ms Yjs batch and durable acknowledgements before
 * asking the server to publish the canonical markdown projection. */
export async function flushCollaborationProjectionBarrier({
  authorityGuard,
  clientOptions,
  controller,
  documentId,
  generation,
  flushProjection = flushEditorCollaborationProjection,
  now = () => new Date(),
  requireLocal = true,
}: ProjectionBarrierOptions): Promise<ConfirmedCollaborationProjection> {
  if (!isAuthoritativeSequence(generation)) {
    throw new CollaborationProjectionBarrierError(
      'local_barrier_unavailable',
      'The collaboration document generation is not available.',
    )
  }
  if (requireLocal && !controller) {
    throw new CollaborationProjectionBarrierError(
      'local_barrier_unavailable',
      'The collaboration durability barrier is not available.',
    )
  }

  const guard = authorityGuard ?? beginControllerAuthorityGuard(
    controller,
    documentId,
    generation,
    'write',
  )
  if (
    guard
    && (
      guard.identity.documentId !== documentId
      || guard.identity.generation !== generation
    )
  ) {
    throw new CollaborationProjectionBarrierError(
      'local_barrier_unavailable',
      'The collaboration authority does not match the requested document generation.',
    )
  }
  guard?.assertCurrent()

  const localSequence = controller
    ? await controller.flushAndAwaitDurable()
    : null
  guard?.assertCurrent()
  if (localSequence !== null && !isAuthoritativeSequence(localSequence)) {
    throw new CollaborationProjectionBarrierError(
      'local_sequence_invalid',
      'The collaboration durability barrier returned an invalid sequence.',
    )
  }

  guard?.assertCurrent()
  const projection = await flushProjection(documentId, clientOptions)
  guard?.assertCurrent()
  if (projection.generation !== generation) {
    throw new CollaborationProjectionBarrierError(
      'projection_generation_mismatch',
      'The server projection belongs to a different collaboration document generation.',
    )
  }
  if (
    typeof projection.content_markdown !== 'string'
    || !isAuthoritativeSequence(projection.sequence)
  ) {
    throw new CollaborationProjectionBarrierError(
      'projection_invalid',
      'The server returned an invalid collaboration projection.',
    )
  }
  const authoritativeSequence = projection.authoritative_sequence
  if (
    !isAuthoritativeSequence(authoritativeSequence)
    || authoritativeSequence < projection.sequence
  ) {
    throw new CollaborationProjectionBarrierError(
      'authoritative_sequence_invalid',
      'The server returned an invalid authoritative collaboration sequence.',
    )
  }
  if (projection.sequence !== authoritativeSequence) {
    throw new CollaborationProjectionBarrierError(
      'projection_not_authoritative',
      'The server projection has not reached the authoritative collaboration sequence.',
    )
  }
  if (localSequence !== null && authoritativeSequence < localSequence) {
    throw new CollaborationProjectionBarrierError(
      'projection_behind_local',
      'The authoritative server sequence is behind the durable browser sequence.',
    )
  }

  guard?.assertCurrent()
  controller?.setAuthoritativeSequence(authoritativeSequence)
  return {
    confirmedAt: now().toISOString(),
    markdown: projection.content_markdown,
    sequence: authoritativeSequence,
  }
}

/** Feed command responses back into the same local activity watermark used by
 * the next barrier, including peer and assistant advancement. */
export function setAuthoritativeCollaborationSequence(
  controller: CollaborationProjectionController | null,
  sequence: number,
): void {
  if (!isAuthoritativeSequence(sequence)) {
    throw new CollaborationProjectionBarrierError(
      'authoritative_sequence_invalid',
      'The collaboration command returned an invalid sequence.',
    )
  }
  controller?.setAuthoritativeSequence(sequence)
}

/** Every decision gets a fresh browser+server barrier; its command result is
 * immediately adopted so the next decision cannot reuse the prior sequence. */
export async function decideCollaborationPatchesAfterBarrier({
  authorityGuard,
  clientOptions,
  controller,
  decide = decideEditorCollaborationPatches,
  decision,
  decisionId,
  documentId,
  generation,
  flushProjection,
  patchIds,
}: CollaborationDecisionOptions): Promise<EditorCollaborationDecisionResponse> {
  const guard = authorityGuard ?? beginControllerAuthorityGuard(
    controller,
    documentId,
    generation,
    'decision',
  )
  const projection = await flushCollaborationProjectionBarrier({
    authorityGuard: guard,
    clientOptions,
    controller,
    documentId,
    generation,
    flushProjection,
  })
  guard?.assertCurrent()
  const response = await decide(
    documentId,
    {
      decision,
      decision_id: decisionId,
      expected_sequence: projection.sequence,
      patch_ids: [...new Set(patchIds)],
    },
    clientOptions,
  )
  guard?.assertCurrent()
  if (
    response.decision_id !== decisionId
    || !Number.isSafeInteger(response.sequence)
    || response.sequence <= projection.sequence
    || !Array.isArray(response.suggestion_ids)
    || response.suggestion_ids.some((id) => typeof id !== 'string' || !id)
  ) {
    throw new CollaborationProjectionBarrierError(
      'command_response_invalid',
      'The collaboration decision was not confirmed durably.',
    )
  }
  guard?.assertCurrent()
  setAuthoritativeCollaborationSequence(controller, response.sequence)
  return response
}

/** A stale export is allowed only when its exact server projection carries an
 * explicit confirmation timestamp. AI and decisions never call this helper. */
export function confirmedProjectionFallback(
  document: EditorDocumentRecord,
): CollaborationProjectionFallback | null {
  const confirmedAt = document.collaboration?.projectionUpdatedAt
  if (document.contentMode !== 'collaboration' || !confirmedAt) return null
  const parsed = new Date(confirmedAt)
  if (Number.isNaN(parsed.getTime())) return null
  // A document hydrated from metadata alone carries an empty body until it is
  // opened. Exporting that as a confirmed state writes an empty file under a
  // plausible timestamp, which is worse for a backup than refusing outright.
  if (document.contentMarkdown.length === 0) return null
  return { confirmedAt: parsed.toISOString(), markdown: document.contentMarkdown }
}

function isAuthoritativeSequence(sequence: unknown): sequence is number {
  return typeof sequence === 'number'
    && Number.isSafeInteger(sequence)
    && sequence >= 0
}

function beginControllerAuthorityGuard(
  controller: CollaborationProjectionController | null,
  documentId: string,
  generation: number | null | undefined,
  requirement: 'decision' | 'write',
): CollaborationAuthorityGuard | null {
  if (!controller) return null
  if (!isAuthoritativeSequence(generation)) {
    throw new CollaborationProjectionBarrierError(
      'local_barrier_unavailable',
      'The collaboration authority lifecycle is not available.',
    )
  }
  return beginCollaborationAuthorityGuard(
    controller,
    { documentId, generation },
    requirement,
  )
}
