import * as Y from 'yjs'

import type { CollaborationAccess } from './protocol.js'
import {
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  isStructureSuggestionData,
} from './structureSuggestions.js'
import { SUGGESTION_MARK_NAMES } from './suggestionMarks.js'
import { isTextblockCompletion } from './textblockPlaceholder.js'

// A browser that keeps one update in flight merges what its editor produces
// meanwhile into the next update. The collaboration service judges an update
// as a whole: a merged update must be accepted whenever each of its members
// would have been accepted alone, sent after its predecessors. The rules below
// seal a group before a member that must not join it. The service's contract
// test forms its groups with these same functions and sends them through the
// real acceptance path, so the groups it checks are the groups a browser that
// uses these functions forms.

/** Write mode under which the editor produced a local update. */
export type CollaborationWriteStamp = 'edit' | 'suggest'

/**
 * How a local update may be grouped with others.
 *
 * - `suggestion`: produced under suggest access or in suggest mode. The editor
 *   transforms every such change into a suggestion change.
 * - `isolated`: an edit-mode change the browser cannot classify for certain.
 *   It carries suggestion metadata, changes a text node that carries a
 *   suggestion mark or lies in a block with a structure suggestion, or deletes
 *   a block. The service may classify it as a suggestion change, so it is
 *   always sent alone, exactly as the editor produced it.
 * - `direct`: every other edit-mode change.
 */
export type LocalUpdateClass = 'direct' | 'isolated' | 'suggestion'

/**
 * Facts of one local editor transaction that are only visible before Yjs
 * garbage-collects its deleted content, see describeLocalEditorTransaction.
 */
export type LocalEditorTransactionFacts = Readonly<{
  /**
   * The transaction deleted an item holding a Yjs type: a block, or a text or
   * inline node inside one.
   */
  blockDelete: boolean
  /**
   * The transaction wrote the empty text nodes of the textblocks the
   * transaction before it created, see completeCreatedTextblocks.
   */
  completion: boolean
  /** The transaction deleted anything, of any client. */
  deletes: boolean
  /**
   * The transaction has the shape of a Yjs formatting cleanup, see
   * isFormattingCleanupTransaction.
   */
  formattingCleanup: boolean
  /** The local client's clock advanced: the transaction inserted structs. */
  inserts: boolean
  /**
   * The transaction changed a text node that carries a suggestion mark (live,
   * or deleted by this transaction), a text node inside a block with a
   * structure suggestion, or a block whose structure suggestion is set or was
   * removed by this transaction.
   */
  suggestionAdjacent: boolean
}>

/** What the grouping rules need to know about a batch of local updates. */
export type LocalBatchFacts = Readonly<{
  /** Some member carries suggestion metadata (a mark or a structure suggestion). */
  boundary: boolean
  /** Total size of the members' updates. */
  bytes: number
  class: LocalUpdateClass
  /** Some member deletes. */
  deletes: boolean
  /** Some member inserts. */
  inserts: boolean
}>

/** Facts of one local editor update; a batch of one. */
export type LocalEditorUpdateFacts = LocalBatchFacts & Readonly<{
  /**
   * The update completes the textblocks the update before it created (see
   * LocalEditorTransactionFacts.completion).
   */
  completion: boolean
  /** The update carries a structure suggestion. */
  structure: boolean
}>

/** Why a batch is sealed before the next local update. */
export type LocalBatchSealReason =
  | 'byte_cap'
  | 'first_suggestion_boundary'
  | 'isolated_update'
  | 'structure_suggestion'
  | 'suggestion_direction'
  | 'write_mode'

// A merged update never exceeds this size, whatever the frame limit is.
const MERGED_UPDATE_MAX_BYTES = 256 * 1024
// A merged update stays within this fraction of the frame limit, so a merge
// never produces a frame the service refuses for its size while every member
// alone would have fit.
const MERGED_UPDATE_FRAME_LIMIT_DIVISOR = 8

/**
 * The byte cap for merging local updates under the frame limit the session
 * publishes: an eighth of the frame limit, at most 256 KiB.
 */
export function collaborationMergeByteCap(frameLimitBytes: number): number {
  if (!Number.isSafeInteger(frameLimitBytes) || frameLimitBytes < 1) {
    throw new RangeError('The collaboration frame limit must be a positive integer.')
  }
  return Math.min(
    MERGED_UPDATE_MAX_BYTES,
    Math.floor(frameLimitBytes / MERGED_UPDATE_FRAME_LIMIT_DIVISOR),
  )
}

/**
 * Facts of a local editor transaction.
 *
 * Call it from the editor document's `afterTransaction` event. Yjs emits that
 * event before it garbage-collects the transaction's deleted content and emits
 * the `update` event afterwards, so a suggestion mark or a block deleted by the
 * transaction is only visible here.
 */
export function describeLocalEditorTransaction(
  transaction: Y.Transaction,
): LocalEditorTransactionFacts {
  const localClient = transaction.doc.clientID
  const inserts = (transaction.afterState.get(localClient) ?? 0)
    !== (transaction.beforeState.get(localClient) ?? 0)
  const deleted = deletedStructFacts(transaction)
  return {
    blockDelete: deleted.blockDelete,
    completion: isTextblockCompletion(transaction),
    deletes: deleted.count > 0,
    formattingCleanup: isFormattingCleanup(transaction, deleted),
    inserts,
    suggestionAdjacent: changesSuggestionContext(transaction),
  }
}

/**
 * Facts of a local editor update, from the update bytes and the facts its
 * transaction left in `afterTransaction`.
 *
 * `transaction` is null when those facts are missing; the update is then
 * `isolated` and travels alone, which the service accepts whenever it accepts
 * the update at all.
 */
export function describeLocalEditorUpdate(input: {
  access: CollaborationAccess
  stamp: CollaborationWriteStamp
  transaction: LocalEditorTransactionFacts | null
  update: Uint8Array
}): LocalEditorUpdateFacts {
  const metadata = suggestionMetadataOf(input.update)
  const facts = input.transaction
  return {
    boundary: metadata?.boundary ?? false,
    bytes: input.update.byteLength,
    class: localUpdateClass(input.access, input.stamp, facts, metadata),
    completion: facts?.completion ?? false,
    deletes: facts?.deletes ?? true,
    inserts: facts?.inserts ?? true,
    structure: metadata?.structure ?? false,
  }
}

/**
 * The facts of `batch` after `next` joined it; `batch` null starts a batch.
 */
export function mergeLocalBatchFacts(
  batch: LocalBatchFacts | null,
  next: LocalEditorUpdateFacts,
): LocalBatchFacts {
  if (batch === null) {
    return {
      boundary: next.boundary,
      bytes: next.bytes,
      class: next.class,
      deletes: next.deletes,
      inserts: next.inserts,
    }
  }
  return {
    boundary: batch.boundary || next.boundary,
    bytes: batch.bytes + next.bytes,
    class: strictestClass(batch.class, next.class),
    deletes: batch.deletes || next.deletes,
    inserts: batch.inserts || next.inserts,
  }
}

/**
 * Why the open batch must be sealed before `next` joins it, or null when
 * `next` may join. Without an open batch (`open` null) nothing is sealed.
 *
 * Sealing more often than these rules ask is always acceptable: every part of
 * a group formed under these rules again satisfies them.
 */
export function localBatchSealReason(
  open: LocalBatchFacts | null,
  next: LocalEditorUpdateFacts,
  mergeByteCap: number,
): LocalBatchSealReason | null {
  if (open === null) return null
  // A completion joins the update it completes, whatever that update's class:
  // the textblocks it completes are never sent without their text node. The
  // completed update already carries the text nodes (Yjs encodes an update
  // from every struct written since its transaction began), so the
  // completion adds nothing to the batch, and alone it would be a batch the
  // transport already holds.
  if (next.completion) return null
  // The service refuses an update that mixes direct and suggestion changes. A
  // change of write mode (or of access) is where suggestion changes start or
  // end.
  if (isSuggestionClass(next) !== isSuggestionClass(open)) return 'write_mode'
  // The service must see a structure suggestion's short-lived `/query`
  // insertion before the update that replaces it.
  if (next.structure) return 'structure_suggestion'
  // Keeps editor normalization and direct edits out of the first proposal.
  if (next.boundary && !open.boundary) return 'first_suggestion_boundary'
  // A suggestion group is insertion-only or deletion-only, and an update that
  // does both travels alone. Merged, an insertion and a later deletion of the
  // same text leave a deleted struct the service's suggestion check refuses,
  // and a deletion and a re-insertion of the same text inside the writer's
  // own suggestion change no suggestion, which the service refuses too.
  if (
    open.class === 'suggestion'
    && ((next.deletes && open.inserts) || (next.inserts && open.deletes))
  ) return 'suggestion_direction'
  if (next.class === 'isolated' || open.class === 'isolated') return 'isolated_update'
  if (open.bytes + next.bytes > mergeByteCap) return 'byte_cap'
  return null
}

/**
 * Whether a transaction has the shape of the formatting cleanup Yjs runs after
 * a remote change: local, inserting nothing, and deleting only format items.
 *
 * Yjs runs such a cleanup in a transaction of its own after it applied a remote
 * change to a text node with formatting, and removes the format items the
 * change made redundant. A transaction that changed nothing is not a cleanup.
 * The check is about shape only: a writer who removes a whole format run
 * produces the same shape, so callers apply it only to the transactions that
 * follow a remote change.
 *
 * Call it from the document's `afterTransaction` event: afterwards Yjs has
 * replaced the deleted items' content, and the check can no longer see what
 * was deleted.
 */
export function isFormattingCleanupTransaction(transaction: Y.Transaction): boolean {
  return isFormattingCleanup(transaction, deletedStructFacts(transaction))
}

/** Whether an update carries a structure suggestion. */
export function containsStructureSuggestionAttribute(update: Uint8Array): boolean {
  return suggestionMetadataOf(update)?.structure ?? false
}

/** Whether an update carries suggestion metadata (a mark or a structure suggestion). */
export function containsSuggestionBoundary(update: Uint8Array): boolean {
  return suggestionMetadataOf(update)?.boundary ?? false
}

type DeleteSet = Y.Transaction['deleteSet']

type DeletedStructFacts = {
  blockDelete: boolean
  count: number
  onlyFormatItems: boolean
}

function deletedStructFacts(transaction: Y.Transaction): DeletedStructFacts {
  const facts: DeletedStructFacts = { blockDelete: false, count: 0, onlyFormatItems: true }
  Y.iterateDeletedStructs(transaction, transaction.deleteSet, (struct) => {
    facts.count += 1
    if (!(struct instanceof Y.Item)) {
      facts.onlyFormatItems = false
      return
    }
    if (struct.content instanceof Y.ContentType) facts.blockDelete = true
    if (!(struct.content instanceof Y.ContentFormat)) facts.onlyFormatItems = false
  })
  return facts
}

function isFormattingCleanup(
  transaction: Y.Transaction,
  deleted: DeletedStructFacts,
): boolean {
  if (!transaction.local) return false
  for (const [client, clock] of transaction.afterState) {
    if ((transaction.beforeState.get(client) ?? 0) !== clock) return false
  }
  return deleted.count > 0 && deleted.onlyFormatItems
}

function changesSuggestionContext(transaction: Y.Transaction): boolean {
  for (const type of transaction.changed.keys()) {
    if (type instanceof Y.XmlText) {
      if (carriesSuggestionMark(type, transaction.deleteSet)) return true
      const parent = type.parent
      if (
        parent instanceof Y.XmlElement
        && carriesStructureSuggestion(parent, transaction.deleteSet)
      ) return true
    } else if (
      type instanceof Y.XmlElement
      && carriesStructureSuggestion(type, transaction.deleteSet)
    ) {
      return true
    }
  }
  return false
}

function carriesSuggestionMark(text: Y.XmlText, deleteSet: DeleteSet): boolean {
  for (let item = text._start; item !== null; item = item.right) {
    if (
      item.content instanceof Y.ContentFormat
      && SUGGESTION_MARK_NAMES.has(item.content.key)
      && (!item.deleted || Y.isDeleted(deleteSet, item.id))
    ) return true
  }
  return false
}

function carriesStructureSuggestion(
  element: Y.XmlElement,
  deleteSet: DeleteSet,
): boolean {
  const attribute = element._map.get(INQTRIX_STRUCTURE_SUGGESTION_ATTR)
  if (!attribute) return false
  if (attribute.deleted) return Y.isDeleted(deleteSet, attribute.id)
  return element.getAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR) != null
}

function localUpdateClass(
  access: CollaborationAccess,
  stamp: CollaborationWriteStamp,
  facts: LocalEditorTransactionFacts | null,
  metadata: SuggestionMetadata | null,
): LocalUpdateClass {
  if (facts === null || metadata === null) return 'isolated'
  if (access === 'suggest' || stamp === 'suggest') return 'suggestion'
  if (
    metadata.boundary
    || metadata.structure
    || facts.suggestionAdjacent
    || facts.blockDelete
  ) return 'isolated'
  return 'direct'
}

function isSuggestionClass(facts: LocalBatchFacts): boolean {
  return facts.class === 'suggestion'
}

function strictestClass(
  left: LocalUpdateClass,
  right: LocalUpdateClass,
): LocalUpdateClass {
  if (left === 'isolated' || right === 'isolated') return 'isolated'
  if (left === 'suggestion' || right === 'suggestion') return 'suggestion'
  return 'direct'
}

type SuggestionMetadata = { boundary: boolean; structure: boolean }

// Structure suggestions and suggestion marks in the structs an update inserts;
// null when the update cannot be decoded.
function suggestionMetadataOf(update: Uint8Array): SuggestionMetadata | null {
  let structs: ReturnType<typeof Y.decodeUpdate>['structs']
  try {
    structs = Y.decodeUpdate(update).structs
  } catch {
    return null
  }
  let structure = false
  let boundary = false
  for (const struct of structs) {
    if (!(struct instanceof Y.Item)) continue
    if (
      struct.parentSub === INQTRIX_STRUCTURE_SUGGESTION_ATTR
      || (
        struct.content instanceof Y.ContentAny
        && struct.content.getContent().length === 1
        && isStructureSuggestionData(struct.content.getContent()[0])
      )
    ) {
      structure = true
      boundary = true
    }
    if (
      struct.content instanceof Y.ContentFormat
      && SUGGESTION_MARK_NAMES.has(struct.content.key)
    ) {
      boundary = true
    }
  }
  return { boundary, structure }
}
