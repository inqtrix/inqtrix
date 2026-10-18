import { createHash } from 'node:crypto'

import {
  getEditorSchemaFingerprint,
  validateEditorYDoc,
} from '@inqtrix/editor-schema'
import type { JSONContent } from '@tiptap/core'
import * as Y from 'yjs'

import type {
  LoadedDocumentCandidate,
  LoadedDocumentState,
  LoadedDocumentUpdate,
} from './contracts'
import {
  collaborationError,
  CloseCodes,
  CollaborationError,
  type CollaborationErrorReason,
} from './errors'

export type ValidatedDocument = {
  canonicalJson: JSONContent
  document: Y.Doc
  encodedState: Uint8Array
  stateHash: string
}

export type ReconstructionOptions = {
  onCandidateRejected?: (event: {
    candidateIndex: number
    reason: CollaborationErrorReason
  }) => void
  onCandidateSelected?: (event: {
    candidateIndex: number
    updates: readonly LoadedDocumentUpdate[]
  }) => void
}

export async function reconstructDocument(
  state: LoadedDocumentState,
  expected: {
    documentId: string
    generation: number
    schemaVersion: number
  },
  documentLimitBytes: number,
  options: ReconstructionOptions = {},
): Promise<Y.Doc> {
  return (await reconstructValidatedDocument(
    state,
    expected,
    documentLimitBytes,
    options,
  )).document
}

export async function reconstructValidatedDocument(
  state: LoadedDocumentState,
  expected: {
    documentId: string
    generation: number
    schemaVersion: number
  },
  documentLimitBytes: number,
  options: ReconstructionOptions = {},
): Promise<ValidatedDocument> {
  const schemaHash = await getEditorSchemaFingerprint()
  if (
    state.documentId !== expected.documentId
    || state.generation !== expected.generation
    || state.schemaVersion !== expected.schemaVersion
    || state.schemaHash !== schemaHash
  ) {
    throw new CollaborationError('invalid_schema', {
      closeCode: CloseCodes.incompatible,
      httpStatus: 409,
    })
  }

  const candidates = reconstructionCandidates(state)
  for (let candidateIndex = 0; candidateIndex < candidates.length; candidateIndex += 1) {
    const candidate = candidates[candidateIndex]!
    const document = new Y.Doc()
    try {
      reconstructCandidate(document, candidate, state.persistedSequence)
      const validated = validateDocument(document, documentLimitBytes)
      options.onCandidateSelected?.({ candidateIndex, updates: candidate.updates })
      return validated
    } catch (error) {
      document.destroy()
      const mapped = collaborationError(error)
      if (candidateIndex === candidates.length - 1) throw mapped
      options.onCandidateRejected?.({ candidateIndex, reason: mapped.reason })
    }
  }
  throw new CollaborationError('internal_consistency', {
    closeCode: CloseCodes.internalConsistency,
  })
}

type ReconstructionCandidate = Omit<LoadedDocumentCandidate, 'snapshot'> & {
  snapshot: LoadedDocumentCandidate['snapshot'] | null
}

function reconstructionCandidates(state: LoadedDocumentState): ReconstructionCandidate[] {
  if (state.snapshotCandidates !== undefined) {
    if (state.snapshotCandidates.length < 1 || state.snapshotCandidates.length > 2) {
      throw new CollaborationError('internal_consistency', {
        closeCode: CloseCodes.internalConsistency,
      })
    }
    return state.snapshotCandidates
  }
  return [{ snapshot: state.snapshot, updates: state.updates }]
}

function reconstructCandidate(
  document: Y.Doc,
  candidate: ReconstructionCandidate,
  persistedSequence: number,
): void {
  let coveredSequence = 0
  if (candidate.snapshot) {
    Y.applyUpdate(document, candidate.snapshot.stateUpdate)
    coveredSequence = candidate.snapshot.coveredSequence
    if (
      hashBytes(Y.encodeStateAsUpdate(document)) !== candidate.snapshot.stateHash
      || !sameBytes(Y.encodeStateVector(document), candidate.snapshot.stateVector)
    ) {
      throw new CollaborationError('internal_consistency', {
        closeCode: CloseCodes.internalConsistency,
      })
    }
  }
  for (const update of candidate.updates) {
    if (hashBytes(update.update) !== update.hash) {
      throw new CollaborationError('internal_consistency', {
        closeCode: CloseCodes.internalConsistency,
      })
    }
    if (update.sequence !== coveredSequence + 1) {
      throw new CollaborationError('internal_consistency', {
        closeCode: CloseCodes.internalConsistency,
      })
    }
    Y.applyUpdate(document, update.update)
    coveredSequence = update.sequence
  }
  if (coveredSequence !== persistedSequence) {
    throw new CollaborationError('internal_consistency', {
      closeCode: CloseCodes.internalConsistency,
    })
  }
}

export function sameBytes(left: Uint8Array, right: Uint8Array): boolean {
  // `Buffer.compare` is a memcmp and honours each view's own offset and
  // length, so this stays a content comparison — not a comparison of the
  // pooled ArrayBuffer that Yjs may hand out views into. The previous
  // `every` callback ran one JS invocation per byte, which on a 126 kB
  // encoded state is six figures of call overhead for an answer memcmp
  // gives in one pass. Semantics are unchanged; only the cost is.
  return left.byteLength === right.byteLength && Buffer.compare(left, right) === 0
}

export function cloneDocument(document: Y.Doc, encodedState?: Uint8Array): Y.Doc {
  const clone = new Y.Doc()
  Y.applyUpdate(clone, encodedState ?? Y.encodeStateAsUpdate(document))
  return clone
}

export function validateDocument(
  document: Y.Doc,
  documentLimitBytes: number,
): ValidatedDocument {
  let canonicalJson: JSONContent
  try {
    canonicalJson = validateEditorYDoc(document)
  } catch {
    throw new CollaborationError('invalid_schema', {
      closeCode: CloseCodes.incompatible,
      httpStatus: 409,
    })
  }
  const encodedState = Y.encodeStateAsUpdate(document)
  if (encodedState.byteLength > documentLimitBytes) {
    throw new CollaborationError('document_too_large', {
      closeCode: CloseCodes.messageTooLarge,
      httpStatus: 413,
    })
  }
  return {
    canonicalJson: freezeJson(canonicalJson),
    document,
    encodedState,
    stateHash: hashBytes(encodedState),
  }
}

function freezeJson<T>(value: T): T {
  if (value === null || typeof value !== 'object' || Object.isFrozen(value)) return value
  for (const child of Object.values(value)) freezeJson(child)
  return Object.freeze(value)
}

export function hashBytes(value: Uint8Array): string {
  return createHash('sha256').update(value).digest('hex')
}

export function hashString(value: string): string {
  return createHash('sha256').update(value, 'utf8').digest('hex')
}
