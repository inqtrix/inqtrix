import {
  applySuggestions,
  revertSuggestions,
  suggestChangesKey,
  transformToSuggestionTransaction,
} from '@handlewithcare/prosemirror-suggest-changes'
import type { JSONContent } from '@tiptap/core'
import type { Fragment, Mark, Node as ProseMirrorNode, Slice } from '@tiptap/pm/model'
import { EditorState, type Transaction } from '@tiptap/pm/state'
import { ReplaceStep, findWrapping, type StepMap } from '@tiptap/pm/transform'
import { ySyncPluginKey } from '@tiptap/y-tiptap'
import { isEditorTextAlignment } from './extensions.js'
import { createSecureUuid } from './ids.js'
import { addProposedBlockAnchorCleanup } from './proposedBlockAnchors.js'
import {
  SUGGESTION_MARK_NAMES,
  type SerializedSuggestionKind,
  type SuggestionKind,
  type SuggestionMarkKind,
} from './suggestionMarks.js'
import {
  INQTRIX_STRUCTURE_COMMAND_META,
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  isStructureSuggestionData,
  type StructureSuggestionAction,
  type StructureSuggestionCommand,
  type StructureSuggestionData,
} from './structureSuggestions.js'

/**
 * Why the suggest-mode transform refused a change. The code is the contract
 * with the editor, which shows the user a text for it; the error message
 * stays technical English.
 *
 * - `adjacent_own_suggestion_conflict`: the change sits directly at one of
 *   the actor's own pending suggestions and cannot join it (text typed right
 *   at a proposed deletion, a deletion right before a proposed insertion or
 *   right next to a proposed join of two blocks).
 * - `divider_unsupported`: a divider cannot be reviewed as a reversible
 *   structural suggestion.
 * - `foreign_suggestion_changed`: the change would alter, extend or remove a
 *   pending suggestion of another collaborator.
 * - `format_overlaps_own_suggestion`: formatting would absorb one of the
 *   actor's own pending suggestions (a proposal spanning original text and
 *   an own insertion) or remove it (a format that excludes the mark of an
 *   own suggestion).
 * - `node_marks_unsupported`: the change would need a suggestion on a whole
 *   block or atom, which the collaborative document cannot carry.
 * - `own_suggestion_edit_unsupported`: a direct edit would change the actor's
 *   own pending suggestion in a way that is not a suggestion itself.
 * - `proposed_block_unsupported`: a new block may only be a paragraph, a
 *   list item or an open task item; a heading, a code block or a checked
 *   task item cannot be proposed as a new block.
 * - `split_moves_content`: a new block would take existing content along; in
 *   suggest mode a new block may only start at the end of a block.
 * - `structure_suggestion_inapplicable`: the change would leave a pending
 *   structural suggestion that can no longer be applied, for example
 *   formatting inside a block proposed as a code block.
 * - `structure_target_unsupported`: the selected block cannot carry the
 *   requested structural suggestion.
 * - `suggestion_metadata_invalid`: the result would hold a suggestion
 *   without complete, consistent metadata.
 * - `suggestion_would_disappear`: the change would remove one of the actor's
 *   own pending suggestions completely instead of withdrawing it.
 * - `table_topology_unsupported`: rows, columns or cells of a table cannot be
 *   added, removed, merged or split as a suggestion.
 * - `unsupported_structure`: any other change that has no reversible
 *   suggestion representation.
 */
export const UNSUPPORTED_SUGGESTION_REASONS = Object.freeze([
  'adjacent_own_suggestion_conflict',
  'divider_unsupported',
  'foreign_suggestion_changed',
  'format_overlaps_own_suggestion',
  'node_marks_unsupported',
  'own_suggestion_edit_unsupported',
  'proposed_block_unsupported',
  'split_moves_content',
  'structure_suggestion_inapplicable',
  'structure_target_unsupported',
  'suggestion_metadata_invalid',
  'suggestion_would_disappear',
  'table_topology_unsupported',
  'unsupported_structure',
] as const)

export type UnsupportedSuggestionReason = typeof UNSUPPORTED_SUGGESTION_REASONS[number]

export class UnsupportedSuggestionStructureError extends Error {
  readonly code = 'unsupported_suggestion_structure'
  readonly reason: UnsupportedSuggestionReason

  constructor(message: string, reason: UnsupportedSuggestionReason) {
    super(message)
    this.name = 'UnsupportedSuggestionStructureError'
    this.reason = reason
  }
}

/** A suggestion whose marks or structure data do not form one consistent
 * suggestion. Names the suggestion when it is known. */
export class InvalidSuggestionMetadataError extends Error {
  readonly suggestionId: string | null

  constructor(message: string, suggestionId: string | null) {
    super(message)
    this.name = 'InvalidSuggestionMetadataError'
    this.suggestionId = suggestionId
  }
}

export type SuggestionMetadata = {
  authorId: string
  createdAt: number
  patchId: string
}

export type SuggestionDescriptor = SuggestionMetadata & {
  kind: SuggestionKind
  suggestionId: string
}

type SuggestionMarkDescriptor = SuggestionMetadata & {
  declaredKind: SerializedSuggestionKind
  markKind: SuggestionMarkKind
  mark: Mark
  suggestionId: string
}

type SuggestionDescriptorAccumulator = SuggestionMetadata & {
  declarations: Array<{
    declaredKind: SerializedSuggestionKind
    markKind: SuggestionMarkKind
  }>
  markKinds: Set<SuggestionMarkKind>
  marks: Mark[]
  occurrences: SuggestionMarkOccurrence[]
  suggestionId: string
}

type SuggestionMarkOccurrence = {
  from: number
  markKind: SuggestionMarkKind
  parent: ProseMirrorNode
  to: number
}

type MarkLocation = {
  from: number
  mark: Mark
  nodePosition: number | null
  to: number
}

type TrackedSlashSuggestion = SuggestionMetadata & {
  suggestionId: string
}

export function isRemoteYjsTransaction(transaction: Transaction): boolean {
  const meta = yjsTransactionMetadata(transaction)
  return meta?.isChangeOrigin === true
}

export function isYjsUndoRedoTransaction(transaction: Transaction): boolean {
  return yjsTransactionMetadata(transaction)?.isUndoRedoOperation === true
}

function yjsTransactionMetadata(transaction: Transaction): {
  isChangeOrigin?: boolean
  isUndoRedoOperation?: boolean
} | undefined {
  return transaction.getMeta(ySyncPluginKey) as {
    isChangeOrigin?: boolean
    isUndoRedoOperation?: boolean
  } | undefined
}

function hasSuggestionCommandBypass(transaction: Transaction): boolean {
  const metadata = transaction.getMeta(suggestChangesKey) as { skip?: unknown } | undefined
  return metadata !== undefined && Object.prototype.hasOwnProperty.call(metadata, 'skip')
}

export function shouldBypassSuggestionTransform(transaction: Transaction): boolean {
  return (
    !transaction.docChanged
    || isRemoteYjsTransaction(transaction)
    || isYjsUndoRedoTransaction(transaction)
    || hasSuggestionCommandBypass(transaction)
  )
}

export function transformToInqtrixSuggestionTransaction(
  transaction: Transaction,
  state: EditorState,
  metadata: SuggestionMetadata,
  generateSuggestionId: () => string = createSecureUuid,
): Transaction {
  if (shouldBypassSuggestionTransform(transaction)) return transaction
  const structureCommand = transaction.getMeta(
    INQTRIX_STRUCTURE_COMMAND_META,
  ) as StructureSuggestionCommand | undefined
  if (structureCommand) {
    return transformStructureCommand(
      transaction,
      state,
      metadata,
      structureCommand,
      generateSuggestionId,
    )
  }
  assertSuggestionTransactionSupported(transaction)

  const generatedIds = new Set<string>()
  let transformed: Transaction
  try {
    transformed = transformToSuggestionTransaction(transaction, state, () => {
      const id = generateSuggestionId()
      generatedIds.add(id)
      return id
    })
  } catch (error) {
    if (
      error instanceof Error
      && error.name === 'TransformError'
      && error.message.startsWith('Invalid content for node ')
    ) {
      throw new UnsupportedSuggestionStructureError(
        'The suggestion transform requires node marks that the collaborative schema does not support',
        'node_marks_unsupported',
      )
    }
    throw error
  }
  transformed = normalizeReusedSuggestionMarks(transformed, state.doc)
  assertProposedBlocksHoldOnlyOwnInsertions(transformed, metadata.authorId)
  assertYjsCompatibleSuggestionStructure(transformed.doc)
  const pureFormatting = isPureFormattingTransaction(transaction)
  transformed = enrichGeneratedSuggestionMarks(
    transformed,
    generatedIds,
    metadata,
    pureFormatting ? 'format' : null,
  )
  // The service's record rules run first, so the reason for a refusal never
  // depends on what else the actor has pending in the document. Its block
  // and deletion rules follow in the service's order.
  assertSuggestionMetadataMirrorsService(state.doc, transformed.doc, metadata.authorId)
  assertSuggestionRecordsMirrorService(
    state.doc,
    transformed.doc,
    metadata.authorId,
    pureFormatting,
  )
  assertAddedWholeBlocksHoldOnlyOwnInsertions(transformed, metadata.authorId)
  assertKeepsOwnSuggestionMarkers(transformed, metadata.authorId)
  assertSuggestionIsReversible(state.doc, transformed.doc)
  assertStructureSuggestionsStayApplicable(state.doc, transformed.doc)
  return transformed
}

/**
 * A suggested block boundary is only reversible when the new block holds
 * nothing but the actor's own inserted text: the collaborative document
 * cannot move existing content into a new block and back, so Enter in the
 * middle or at the start of a block, splitting a list item with a nested
 * list, or pasting several paragraphs into the middle of one is refused.
 * Enter at the end of a block, and inside the actor's own inserted text,
 * stays possible.
 */
function assertProposedBlocksHoldOnlyOwnInsertions(
  transaction: Transaction,
  authorId: string,
): void {
  for (let index = 0; index < transaction.steps.length; index += 1) {
    const step = transaction.steps[index]
    const document = transaction.docs[index]
    if (!(step instanceof ReplaceStep) || !document) continue
    if (!sliceOpensBlockBoundary(step.slice)) continue
    const $to = document.resolve(step.to)
    const depth = $to.depth - step.slice.openEnd + 1
    if (depth < 1 || depth > $to.depth) {
      throw new UnsupportedSuggestionStructureError(
        'This block boundary cannot be proposed as a reversible suggestion.',
        'split_moves_content',
      )
    }
    let movesContent = false
    document.nodesBetween(step.to, $to.end(depth), (node, position) => {
      if (movesContent) return false
      if (position < step.to && !node.isText) return true
      if (!node.isText || !isOwnInsertionText(node, authorId)) movesContent = true
      return false
    })
    if (movesContent) {
      throw new UnsupportedSuggestionStructureError(
        'In suggest mode a new block can only start at the end of a block.',
        'split_moves_content',
      )
    }
    if (!sliceAddsOnlyProposableBlocks(step.slice)) {
      throw new UnsupportedSuggestionStructureError(
        'In suggest mode a new block can only be a paragraph, a list item or an open task item.',
        'proposed_block_unsupported',
      )
    }
  }
}

/**
 * A block the suggestion transform adds as a whole (not the new half of a
 * split, which the boundary check above judges) must hold nothing but the
 * actor's own inserted text: the collaboration service admits a new block
 * only in that shape. The transform adds such blocks itself when it
 * represents a join it cannot express inside the existing blocks, for
 * example Backspace joining two lists, where it adds a list item holding a
 * deleted marker character; the service refuses that, so it is refused
 * here. Runs on the finished transaction, whose suggestion marks carry
 * their author.
 */
function assertAddedWholeBlocksHoldOnlyOwnInsertions(
  transaction: Transaction,
  authorId: string,
): void {
  const document = transaction.doc
  for (let index = 0; index < transaction.steps.length; index += 1) {
    const step = transaction.steps[index]
    if (!(step instanceof ReplaceStep)) continue
    const later = transaction.mapping.slice(index + 1)
    for (const added of wholeBlocksOfSlice(step.slice)) {
      const mapped = later.mapResult(step.from - step.slice.openStart + added.offset, 1)
      if (mapped.deleted) continue
      const node = document.nodeAt(mapped.pos)
      if (!node || node.type !== added.node.type) continue
      if (!holdsOnlyOwnInsertedText(node, authorId)) {
        throw new UnsupportedSuggestionStructureError(
          'This change adds a block that cannot be proposed as a reversible suggestion.',
          'unsupported_structure',
        )
      }
    }
  }
}

/**
 * The client's copy of the service's deletion rule for the actor's own
 * suggestions: a suggest-only update may remove characters of its own
 * pending insertions, but no other character of its own suggestions. The
 * suggestion transform removes such a character in one case: a deletion
 * right next to an own proposed join merges into the join and drops its
 * deleted marker character. The service refuses that, so it is refused
 * here, like typing at the same place.
 */
function assertKeepsOwnSuggestionMarkers(
  transaction: Transaction,
  authorId: string,
): void {
  for (let index = 0; index < transaction.steps.length; index += 1) {
    const step = transaction.steps[index]
    const document = transaction.docs[index]
    if (!(step instanceof ReplaceStep) || !document || step.from >= step.to) continue
    let removesOwnMarker = false
    document.nodesBetween(step.from, step.to, (node) => {
      if (removesOwnMarker) return false
      if (!node.isText || isOwnInsertionText(node, authorId)) return true
      removesOwnMarker = node.marks.some((mark) => (
        SUGGESTION_MARK_NAMES.has(mark.type.name as SuggestionMarkKind)
        && mark.attrs.authorId === authorId
      ))
      return false
    })
    if (removesOwnMarker) {
      throw new UnsupportedSuggestionStructureError(
        'This edit would remove a character of a pending own suggestion that is not inserted text',
        'adjacent_own_suggestion_conflict',
      )
    }
  }
}

/** The block nodes a slice holds completely, with their offset in the
 * slice's content. A node open at an edge of the slice is joined with an
 * existing block, so it is not added as a whole; its complete children are. */
function wholeBlocksOfSlice(slice: Slice): Array<{ node: ProseMirrorNode; offset: number }> {
  const blocks: Array<{ node: ProseMirrorNode; offset: number }> = []
  const visit = (fragment: Fragment, start: number, openStart: number, openEnd: number): void => {
    fragment.forEach((node, offset, index) => {
      if (!node.isBlock) return
      const openLeft = index === 0 && openStart > 0
      const openRight = index === fragment.childCount - 1 && openEnd > 0
      if (!openLeft && !openRight) {
        blocks.push({ node, offset: start + offset })
        return
      }
      visit(
        node.content,
        start + offset + 1,
        openLeft ? openStart - 1 : 0,
        openRight ? openEnd - 1 : 0,
      )
    })
  }
  visit(slice.content, 0, slice.openStart, slice.openEnd)
  return blocks
}

/** Whether every text block inside `node` holds text, and only text the
 * actor proposes to insert. */
function holdsOnlyOwnInsertedText(node: ProseMirrorNode, authorId: string): boolean {
  if (node.childCount === 0) return false
  let own = true
  node.forEach((child) => {
    if (!own) return
    own = node.isTextblock
      ? isOwnInsertionText(child, authorId)
      : child.isBlock && holdsOnlyOwnInsertedText(child, authorId)
  })
  return own
}

/**
 * The blocks a suggestion may add, with the attributes each may carry and the
 * values allowed for them. Nothing else can be taken back by rejecting the
 * suggestion: a structure suggestion in particular is refused on a new block,
 * which holds nothing but the text the suggestion proposes. The collaboration
 * service applies this table to every block an update adds; the suggest-mode
 * transform refuses the same blocks before anything is sent.
 */
const PROPOSED_BLOCK_ATTRIBUTES: ReadonlyMap<
  string,
  ReadonlyMap<string, (value: unknown) => boolean>
> = new Map([
  ['listItem', new Map()],
  ['paragraph', new Map([['textAlign', isEditorTextAlignment]])],
  ['taskItem', new Map([['checked', (value: unknown) => value === false]])],
])

/** Whether a new block of this type with these attributes may be proposed.
 * `attributes` lists the attributes the block actually carries. */
export function isProposableBlock(
  nodeName: string,
  attributes: Iterable<readonly [string, unknown]>,
): boolean {
  const allowed = PROPOSED_BLOCK_ATTRIBUTES.get(nodeName)
  if (!allowed) return false
  for (const [name, value] of attributes) {
    const valid = allowed.get(name)
    if (!valid || !valid(value)) return false
  }
  return true
}

/**
 * Whether every block the slice adds may be proposed. The open blocks on the
 * slice's left edge are not added: their content joins the block the slice
 * is inserted into. Every other block of the slice, at any depth, is new.
 * The collaborative document stores only attributes that are set, so a
 * block's attributes are the ones that are not null.
 */
function sliceAddsOnlyProposableBlocks(slice: Slice): boolean {
  const visit = (fragment: Fragment, openDepth: number): boolean => {
    let proposable = true
    fragment.forEach((node, _offset, index) => {
      if (!proposable || node.isInline) return
      const joins = index === 0 && openDepth > 0
      if (
        !joins
        && !isProposableBlock(
          node.type.name,
          Object.entries(node.attrs).filter(([, value]) => value !== null),
        )
      ) {
        proposable = false
        return
      }
      proposable = visit(node.content, joins ? openDepth - 1 : 0)
    })
    return proposable
  }
  return visit(slice.content, slice.openStart)
}

function sliceOpensBlockBoundary(slice: Slice): boolean {
  if (slice.openEnd === 0) return false
  if (slice.openStart !== slice.openEnd) return true
  let fragment = slice.content
  for (let depth = 0; depth < slice.openStart; depth += 1) {
    if (fragment.childCount !== 1 || !fragment.firstChild) return true
    fragment = fragment.firstChild.content
  }
  return false
}

function isOwnInsertionText(node: ProseMirrorNode, authorId: string): boolean {
  return node.isText && node.marks.some((mark) => (
    mark.type.name === 'insertion' && mark.attrs.authorId === authorId
  ))
}

/**
 * The client's copy of the metadata rules the collaboration service applies
 * to every suggestion update: every suggestion carries complete metadata,
 * one patch has one author and one creation time, and an existing
 * suggestion keeps its author, time, kind and patch.
 */
function assertSuggestionMetadataMirrorsService(
  before: ProseMirrorNode,
  after: ProseMirrorNode,
  authorId: string,
): void {
  let previous: SuggestionDescriptor[]
  let next: SuggestionDescriptor[]
  try {
    previous = suggestionDescriptors(before)
  } catch {
    throw new UnsupportedSuggestionStructureError(
      'The document already holds a suggestion without complete metadata',
      'suggestion_metadata_invalid',
    )
  }
  try {
    next = suggestionDescriptors(after)
  } catch {
    throw joinedSuggestionOfOtherKind(previous, after, authorId)
      ?? new UnsupportedSuggestionStructureError(
        'The suggestion transform produced a suggestion without complete metadata',
        'suggestion_metadata_invalid',
      )
  }
  const patches = new Map<string, SuggestionDescriptor>()
  for (const descriptor of next) {
    const reference = patches.get(descriptor.patchId)
    if (
      reference
      && (
        reference.authorId !== descriptor.authorId
        || reference.createdAt !== descriptor.createdAt
      )
    ) {
      throw new UnsupportedSuggestionStructureError(
        'The suggestion transform mixed authors or times inside one patch',
        'suggestion_metadata_invalid',
      )
    }
    patches.set(descriptor.patchId, descriptor)
  }
  const previousById = new Map(previous.map((descriptor) => [descriptor.suggestionId, descriptor]))
  const previousByPatch = new Map(previous.map((descriptor) => [descriptor.patchId, descriptor]))
  for (const descriptor of next) {
    const existing = previousById.get(descriptor.suggestionId)
    const patch = previousByPatch.get(descriptor.patchId)
    const changed = (
      existing !== undefined
      && (
        existing.authorId !== descriptor.authorId
        || existing.createdAt !== descriptor.createdAt
        || existing.kind !== descriptor.kind
        || existing.patchId !== descriptor.patchId
      )
    ) || (
      patch !== undefined
      && (patch.authorId !== descriptor.authorId || patch.createdAt !== descriptor.createdAt)
    )
    if (!changed) continue
    const foreign = (existing ?? patch)?.authorId !== authorId
    throw new UnsupportedSuggestionStructureError(
      foreign
        ? 'This edit changes a pending suggestion of another collaborator'
        : 'The suggestion transform changed the metadata of a pending suggestion',
      foreign ? 'foreign_suggestion_changed' : 'unsupported_structure',
    )
  }
}

/**
 * The upstream tracker joins a new change to a suggestion directly beside it
 * by reusing that suggestion's id, also across mark types: a deletion right
 * before a proposed insertion takes the insertion's id, and text typed right
 * at a proposed deletion takes the deletion's id. Such a mark carries only
 * the id, and joined, the suggestion would change its kind, which the
 * service refuses. Names the owner of the joined suggestion, or null when
 * the incomplete mark has another cause.
 */
function joinedSuggestionOfOtherKind(
  previous: readonly SuggestionDescriptor[],
  after: ProseMirrorNode,
  authorId: string,
): UnsupportedSuggestionStructureError | null {
  const existing = new Map(previous.map((descriptor) => [descriptor.suggestionId, descriptor]))
  let joined: SuggestionDescriptor | null = null
  after.descendants((node) => {
    if (joined) return false
    for (const mark of node.marks) {
      if (!SUGGESTION_MARK_NAMES.has(mark.type.name as SuggestionMarkKind)) continue
      if (typeof mark.attrs.authorId === 'string' && mark.attrs.authorId.length > 0) continue
      const target = typeof mark.attrs.id === 'string' ? existing.get(mark.attrs.id) : undefined
      if (target) {
        joined = target
        return false
      }
    }
    return true
  })
  const target = joined as SuggestionDescriptor | null
  if (!target) return null
  if (target.authorId !== authorId) return foreignSuggestionChanged()
  return new UnsupportedSuggestionStructureError(
    'This edit would join a pending own suggestion of another kind',
    'adjacent_own_suggestion_conflict',
  )
}

/**
 * The client's copy of the record rules the collaboration service applies:
 * no pending suggestion disappears, another collaborator's suggestion stays
 * exactly as it was, and no new suggestion is attributed to someone else.
 */
function assertSuggestionRecordsMirrorService(
  before: ProseMirrorNode,
  after: ProseMirrorNode,
  authorId: string,
  pureFormatting: boolean,
): void {
  const previous = suggestionRecordSummaries(before)
  const next = suggestionRecordSummaries(after)
  for (const [suggestionId, record] of previous) {
    const current = next.get(suggestionId)
    if (!current) {
      if (record.authorId !== authorId) throw foreignSuggestionChanged()
      throw pureFormatting
        ? new UnsupportedSuggestionStructureError(
            'Formatting across original text and a pending own insertion cannot be proposed',
            'format_overlaps_own_suggestion',
          )
        : new UnsupportedSuggestionStructureError(
            'This edit would remove a pending suggestion completely instead of withdrawing it',
            'suggestion_would_disappear',
          )
    }
    if (record.authorId !== authorId && record.content !== current.content) {
      throw foreignSuggestionChanged()
    }
  }
  for (const [suggestionId, record] of next) {
    if (!previous.has(suggestionId) && record.authorId !== authorId) {
      throw foreignSuggestionChanged()
    }
  }
}

function foreignSuggestionChanged(): UnsupportedSuggestionStructureError {
  return new UnsupportedSuggestionStructureError(
    'This edit changes a pending suggestion of another collaborator',
    'foreign_suggestion_changed',
  )
}

type SuggestionRecordSummary = {
  authorId: string
  /** Every occurrence of the suggestion in document order, in one string. */
  content: string
}

/**
 * The edit-mode counterpart of the suggest-mode record rules, the client's
 * copy of what the collaboration service accepts from an edit lease. Every
 * pending suggestion, another person's or the actor's own, follows one
 * rule:
 *
 * - its characters may be deleted: the suggestion keeps what is left, and
 *   it is removed once every character it proposed is gone;
 * - its characters may be formatted, and text around it may change;
 * - it never gains a character, moves, or loses its mark while the text it
 *   proposed stays, which would decide it outside the decision path.
 *
 * A suggestion of the actor's own that gained characters (typing inside
 * one's own insertion) is the actor's proposal, which the service judges as
 * a suggestion, so the change may not alter the original document then.
 * Changes the service would refuse are refused here with a reason, before
 * anything is sent.
 *
 * Returns the suggestions the change removed.
 */
export function assertDirectEditKeepsSuggestionRecords(
  transaction: Transaction,
  authorId: string,
): string[] {
  const before = transaction.before
  const after = transaction.doc
  const previous = suggestionRecordSummaries(before)
  const next = suggestionRecordSummaries(after)
  if (previous.size === 0 && next.size === 0) return []
  const ownerOf = (suggestionId: string | null): string | null => (
    suggestionId === null
      ? null
      : previous.get(suggestionId)?.authorId ?? next.get(suggestionId)?.authorId ?? null
  )
  try {
    suggestionDescriptors(after)
  } catch (error) {
    const owner = ownerOf(error instanceof InvalidSuggestionMetadataError ? error.suggestionId : null)
    throw owner !== null && owner !== authorId
      ? foreignSuggestionChanged()
      : ownSuggestionEditUnsupported()
  }
  let ownChanged = false
  const removed: string[] = []
  for (const [suggestionId, record] of previous) {
    const own = record.authorId === authorId
    if (!next.has(suggestionId)) {
      if (removedAsRejected(transaction, suggestionId)) {
        removed.push(suggestionId)
        continue
      }
      // Its mark went while the text it proposed stayed.
      if (!own) throw foreignSuggestionChanged()
      throw new UnsupportedSuggestionStructureError(
        'This edit would remove a pending suggestion without the text it proposes',
        'suggestion_would_disappear',
      )
    }
    if (keptOnItsCharacters(transaction, suggestionId)) continue
    if (!own) throw foreignSuggestionChanged()
    ownChanged = true
  }
  for (const [suggestionId, record] of next) {
    if (previous.has(suggestionId)) continue
    if (record.authorId !== authorId) throw foreignSuggestionChanged()
    ownChanged = true
  }
  if (ownChanged) {
    let originalUnchanged = false
    try {
      originalUnchanged = projectOriginalDocument(before).eq(projectOriginalDocument(after))
    } catch {
      originalUnchanged = false
    }
    if (!originalUnchanged) throw ownSuggestionEditUnsupported()
  }
  assertStructureSuggestionsStayApplicable(before, after)
  return removed.sort()
}

/**
 * The client's copy of the service's applicability rule: no change may leave
 * a structural suggestion that cannot be applied, for example formatting
 * inside a block proposed as a code block, because the final projection of
 * the document, and every change summary built from it, would fail for
 * everyone. A suggestion that already could not be applied before the change
 * does not block it.
 */
function assertStructureSuggestionsStayApplicable(
  before: ProseMirrorNode,
  after: ProseMirrorNode,
): void {
  const left = structureSuggestionsLeftInapplicable(before, after)
  if (left === null || left.length > 0) {
    throw new UnsupportedSuggestionStructureError(
      'This change would leave a pending structural suggestion that cannot be applied',
      'structure_suggestion_inapplicable',
    )
  }
}

/**
 * The structural suggestions a change leaves impossible to apply that could
 * still be applied before it, sorted: the ones the applicability rule
 * refuses. Null when the projection of the other suggestions fails after the
 * change and did not before, so no single suggestion can be named.
 */
export function structureSuggestionsLeftInapplicable(
  before: ProseMirrorNode,
  after: ProseMirrorNode,
): string[] | null {
  if (!holdsStructureSuggestion(after)) return []
  const afterFailures = cachedInapplicableStructureSuggestionIds(after)
  if (afterFailures?.size === 0) return []
  const beforeFailures = cachedInapplicableStructureSuggestionIds(before)
  if (afterFailures === null) return beforeFailures === null ? [] : null
  return [...afterFailures].filter((suggestionId) => !beforeFailures?.has(suggestionId)).sort()
}

const structurePresenceCache = new WeakMap<ProseMirrorNode, boolean>()

/** Whether any block of the document carries a structural suggestion. Only
 * text blocks carry one, so the walk does not enter them. */
function holdsStructureSuggestion(document: ProseMirrorNode): boolean {
  const cached = structurePresenceCache.get(document)
  if (cached !== undefined) return cached
  let found = false
  document.descendants((node) => {
    if (found) return false
    if (isStructureSuggestionData(node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR])) found = true
    return !found && !node.isTextblock
  })
  structurePresenceCache.set(document, found)
  return found
}

const inapplicableCache = new WeakMap<ProseMirrorNode, ReadonlySet<string> | null>()

/** `inapplicableStructureSuggestionIds`, or null when the projection of the
 * other suggestions fails. Documents are immutable, so the result is kept
 * per document: the document after one keystroke is the one before the
 * next. */
function cachedInapplicableStructureSuggestionIds(
  document: ProseMirrorNode,
): ReadonlySet<string> | null {
  if (inapplicableCache.has(document)) return inapplicableCache.get(document) ?? null
  let result: ReadonlySet<string> | null
  try {
    result = inapplicableStructureSuggestionIds(document)
  } catch {
    result = null
  }
  inapplicableCache.set(document, result)
  return result
}

/** What one character or block carries of a suggestion: the mark kind, or
 * `structure` for a block, and the mark with every attribute or the
 * structure data, as stable JSON. */
type SuggestionUnit = { kind: SuggestionMarkKind | 'structure'; value: string }

const suggestionUnitCache = new WeakMap<ProseMirrorNode, Map<string, Map<number, SuggestionUnit>>>()

/**
 * Every character that carries a suggestion mark and every block that
 * carries a structure suggestion, by suggestion id and position. Read once
 * per document; the document after one keystroke is the document before the
 * next.
 */
function suggestionUnits(document: ProseMirrorNode): Map<string, Map<number, SuggestionUnit>> {
  const cached = suggestionUnitCache.get(document)
  if (cached) return cached
  const units = new Map<string, Map<number, SuggestionUnit>>()
  const add = (suggestionId: string, position: number, unit: SuggestionUnit): void => {
    const entry = units.get(suggestionId)
    if (entry) entry.set(position, unit)
    else units.set(suggestionId, new Map([[position, unit]]))
  }
  document.descendants((node, position) => {
    const structure = node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR]
    if (isStructureSuggestionData(structure)) {
      add(structure.suggestionId, position, { kind: 'structure', value: stableSignatureJson(structure) })
    }
    if (!node.isInline) return true
    for (const mark of node.marks) {
      if (!SUGGESTION_MARK_NAMES.has(mark.type.name as SuggestionMarkKind)) continue
      const unit: SuggestionUnit = {
        kind: mark.type.name as SuggestionMarkKind,
        value: stableSignatureJson(mark.toJSON()),
      }
      const suggestionId = String(mark.attrs.suggestionId ?? mark.attrs.id)
      for (let offset = 0; offset < node.nodeSize; offset += 1) add(suggestionId, position + offset, unit)
    }
    return false
  })
  suggestionUnitCache.set(document, units)
  return units
}

/** Whether a transaction deleted a unit: the character at a position, or
 * the block that starts there. */
function unitDeleted(transaction: Transaction, position: number, unit: SuggestionUnit): boolean {
  if (unit.kind === 'structure') return transaction.mapping.mapResult(position, 1).deleted
  let character = position
  for (const map of transaction.mapping.maps) {
    let deleted = false
    map.forEach((oldStart, oldEnd) => {
      if (oldStart <= character && character + 1 <= oldEnd) deleted = true
    })
    if (deleted) return true
    character = map.map(character, 1)
  }
  return false
}

/** The client's copy of the service's removal rule: a suggestion the change
 * removed completely counts as removed when every character it proposed to
 * insert is deleted. Text it proposed to delete may stay or go, and so may
 * the block of a structure suggestion. Stripping the mark of a proposed
 * insertion and keeping its text would accept it instead. */
function removedAsRejected(transaction: Transaction, suggestionId: string): boolean {
  for (const [position, unit] of suggestionUnits(transaction.before).get(suggestionId) ?? []) {
    if (unit.kind !== 'insertion' && unit.kind !== 'modification') continue
    if (!unitDeleted(transaction, position, unit)) return false
  }
  return true
}

/** The client's copy of the service's rule for a suggestion a change keeps:
 * every character and block that carries it afterwards carried it before,
 * with the same mark and attributes, and every one that carried it before
 * still carries it or is deleted. */
function keptOnItsCharacters(transaction: Transaction, suggestionId: string): boolean {
  const before = suggestionUnits(transaction.before).get(suggestionId)
  const after = suggestionUnits(transaction.doc).get(suggestionId)
  if (!before || !after) return false
  const kept = new Set<number>()
  for (const [position, unit] of before) {
    if (unitDeleted(transaction, position, unit)) continue
    const mapped = transaction.mapping.map(position, 1)
    const current = after.get(mapped)
    if (current?.kind !== unit.kind || current.value !== unit.value) return false
    kept.add(mapped)
  }
  for (const position of after.keys()) {
    if (!kept.has(position)) return false
  }
  return true
}

function ownSuggestionEditUnsupported(): UnsupportedSuggestionStructureError {
  return new UnsupportedSuggestionStructureError(
    'This direct edit changes a pending own suggestion; decide the suggestion first or switch to suggest mode',
    'own_suggestion_edit_unsupported',
  )
}

const recordSummaryCache = new WeakMap<ProseMirrorNode, Map<string, SuggestionRecordSummary>>()

/**
 * The content of every pending suggestion, without positions: one entry per
 * marked text node (mark kind, text, other marks) and one per block that
 * carries a structural suggestion (its full signature). Documents are
 * immutable, so the result is kept per document: the document after one
 * keystroke is the document before the next.
 */
function suggestionRecordSummaries(
  document: ProseMirrorNode,
): Map<string, SuggestionRecordSummary> {
  const cached = recordSummaryCache.get(document)
  if (cached) return cached
  const summaries = collectSuggestionRecordSummaries(document)
  recordSummaryCache.set(document, summaries)
  return summaries
}

function collectSuggestionRecordSummaries(
  document: ProseMirrorNode,
): Map<string, SuggestionRecordSummary> {
  const entries = new Map<string, { authorId: string; entries: string[] }>()
  const add = (suggestionId: string, authorId: string, entry: string): void => {
    const record = entries.get(suggestionId)
    if (record) record.entries.push(entry)
    else entries.set(suggestionId, { authorId, entries: [entry] })
  }
  document.descendants((node) => {
    for (const mark of node.marks) {
      if (!SUGGESTION_MARK_NAMES.has(mark.type.name as SuggestionMarkKind)) continue
      add(
        String(mark.attrs.suggestionId ?? mark.attrs.id),
        String(mark.attrs.authorId),
        stableSignatureJson([
          mark.type.name,
          node.isText ? node.text ?? '' : node.type.name,
          node.marks
            .filter((other) => !SUGGESTION_MARK_NAMES.has(other.type.name as SuggestionMarkKind))
            .map((other) => other.toJSON()),
        ]),
      )
    }
    const structure = node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR]
    if (isStructureSuggestionData(structure)) {
      add(
        structure.suggestionId,
        structure.authorId,
        stableSignatureJson(['structure', structureSuggestionSignature(node)]),
      )
    }
    return true
  })
  return new Map([...entries].map(([suggestionId, record]) => [suggestionId, {
    authorId: record.authorId,
    content: JSON.stringify(record.entries),
  }]))
}

/**
 * What identifies the content a structural suggestion was made on: the
 * block type, its other attributes and marks, the complete suggestion data
 * (action, discarded slash command, metadata) and the inline sequence that
 * the data's offsets refer to. Text runs that only marks separate count as
 * one run, so formatting inside the block leaves the signature unchanged;
 * text and inline atoms such as formulas or hard breaks change it.
 */
export function structureSuggestionSignature(node: ProseMirrorNode): unknown {
  const attrs: Record<string, unknown> = { ...node.attrs }
  const data = attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR] ?? null
  delete attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR]
  const inline: unknown[] = []
  node.forEach((child) => {
    const last = inline.at(-1)
    if (child.isText && typeof last === 'string') {
      inline[inline.length - 1] = last + (child.text ?? '')
      return
    }
    inline.push(child.isText ? child.text ?? '' : { attrs: child.attrs, type: child.type.name })
  })
  return {
    attrs,
    data,
    inline,
    marks: node.marks
      .filter((mark) => !SUGGESTION_MARK_NAMES.has(mark.type.name as SuggestionMarkKind))
      .map((mark) => ({ attrs: mark.attrs, type: mark.type.name })),
    type: node.type.name,
  }
}

function stableSignatureJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableSignatureJson).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableSignatureJson(item)}`)
      .join(',')}}`
  }
  return JSON.stringify(value) ?? 'null'
}

/**
 * `prosemirror-suggest-changes` preserves only the upstream `id` attribute
 * when it extends an adjacent mark. Inqtrix adds authoritative actor, patch,
 * time and semantic attributes, so copy those attributes from the existing
 * mark before validating the transformed document.
 */
function normalizeReusedSuggestionMarks(
  transaction: Transaction,
  before: ProseMirrorNode,
): Transaction {
  const existing = new Map<string, Mark>()
  before.descendants((node) => {
    for (const mark of node.marks) {
      const id = mark.attrs.id
      if (
        SUGGESTION_MARK_NAMES.has(mark.type.name as SuggestionMarkKind)
        && typeof id === 'string'
        && id.length > 0
      ) {
        existing.set(`${mark.type.name}:${id}`, mark)
      }
    }
  })
  const locations: MarkLocation[] = []
  transaction.doc.descendants((node, position) => {
    for (const mark of node.marks) {
      const id = mark.attrs.id
      if (
        typeof id !== 'string'
        || !existing.has(`${mark.type.name}:${id}`)
      ) continue
      locations.push({
        from: position,
        mark,
        nodePosition: node.isText ? null : position,
        to: position + node.nodeSize,
      })
    }
  })
  for (const location of locations) {
    const id = String(location.mark.attrs.id)
    const authoritative = existing.get(`${location.mark.type.name}:${id}`)
    if (!authoritative || location.mark.eq(authoritative)) continue
    if (location.nodePosition === null) {
      transaction.removeMark(location.from, location.to, location.mark)
      transaction.addMark(location.from, location.to, authoritative)
      continue
    }
    transaction.removeNodeMark(location.nodePosition, location.mark)
    transaction.addNodeMark(location.nodePosition, authoritative)
  }
  return transaction
}

function isPureFormattingTransaction(transaction: Transaction): boolean {
  return (
    transaction.steps.length > 0
    && transaction.steps.every((step) => {
      const stepType = step.toJSON().stepType
      return stepType === 'addMark' || stepType === 'removeMark'
    })
  )
}

/**
 * The client's copy of the service's reversibility rule, which it applies to
 * every suggestion: rejecting every suggestion must restore the exact
 * original document. The metadata and record rules above have already
 * proved that no suggestion disappeared, no foreign suggestion changed and
 * no metadata moved. What still breaks the projection is a default
 * ProseMirror structural step without a durable suggestion representation,
 * and a formatting suggestion that takes over the invisible marker of an own
 * proposed join, as a selection to the end of the block does. The original
 * projection of the document before a change is the one after the previous
 * change, so it is kept per document.
 */
function assertSuggestionIsReversible(
  before: ProseMirrorNode,
  after: ProseMirrorNode,
): void {
  let reversible = false
  try {
    reversible = originalProjectionOf(before).eq(originalProjectionOf(after))
  } catch {
    reversible = false
  }
  if (!reversible) {
    throw new UnsupportedSuggestionStructureError(
      'Rejecting this suggestion would not restore the original document',
      'unsupported_structure',
    )
  }
}

const originalProjectionCache = new WeakMap<ProseMirrorNode, ProseMirrorNode>()

function originalProjectionOf(document: ProseMirrorNode): ProseMirrorNode {
  const cached = originalProjectionCache.get(document)
  if (cached) return cached
  const projected = projectOriginalDocument(document)
  originalProjectionCache.set(document, projected)
  return projected
}

function assertSuggestionTransactionSupported(transaction: Transaction): void {
  for (let index = 0; index < transaction.steps.length; index += 1) {
    const before = transaction.docs[index]
    const after = transaction.docs[index + 1] ?? transaction.doc
    const step = transaction.steps[index]
    if (!before || !step) continue
    for (const table of tableNodes(before)) {
      const mappedTable = mappedTableNode(after, table.position, step.getMap())
      if (mappedTable && !sameTableTopology(table.node, mappedTable)) {
        throw new UnsupportedSuggestionStructureError(
          'Suggestions cannot change rows, columns, merged cells, or split cells',
          'table_topology_unsupported',
        )
      }
    }
  }
}

function tableNodes(document: ProseMirrorNode): Array<{
  node: ProseMirrorNode
  position: number
}> {
  const tables: Array<{ node: ProseMirrorNode; position: number }> = []
  document.descendants((node, position) => {
    if (node.type.name === 'table') tables.push({ node, position })
    return node.type.name !== 'table'
  })
  return tables
}

function mappedTableNode(
  document: ProseMirrorNode,
  position: number,
  mapping: StepMap,
): ProseMirrorNode | null {
  for (const association of [1, -1]) {
    const mapped = mapping.mapResult(position, association)
    if (mapped.deletedAcross) continue
    const node = document.nodeAt(mapped.pos)
    if (node?.type.name === 'table') return node
  }
  return null
}

function sameTableTopology(left: ProseMirrorNode, right: ProseMirrorNode): boolean {
  if (left.childCount !== right.childCount) return false
  for (let rowIndex = 0; rowIndex < left.childCount; rowIndex += 1) {
    const leftRow = left.child(rowIndex)
    const rightRow = right.child(rowIndex)
    if (leftRow.type.name !== rightRow.type.name || leftRow.childCount !== rightRow.childCount) {
      return false
    }
    for (let cellIndex = 0; cellIndex < leftRow.childCount; cellIndex += 1) {
      const leftCell = leftRow.child(cellIndex)
      const rightCell = rightRow.child(cellIndex)
      if (
        leftCell.type.name !== rightCell.type.name
        || leftCell.attrs.colspan !== rightCell.attrs.colspan
        || leftCell.attrs.rowspan !== rightCell.attrs.rowspan
        || JSON.stringify(leftCell.attrs.colwidth) !== JSON.stringify(rightCell.attrs.colwidth)
      ) {
        return false
      }
    }
  }
  return true
}

function enrichGeneratedSuggestionMarks(
  transaction: Transaction,
  generatedIds: ReadonlySet<string>,
  metadata: SuggestionMetadata,
  semanticOverride: Extract<SuggestionKind, 'format'> | null,
): Transaction {
  const locations: MarkLocation[] = []
  const markKinds = new Map<string, Set<SuggestionMarkKind>>()
  transaction.doc.descendants((node, position) => {
    for (const mark of node.marks) {
      const id = mark.attrs.id
      if (typeof id !== 'string' || !generatedIds.has(id)) continue
      const kind = mark.type.name as SuggestionMarkKind
      const kinds = markKinds.get(id) ?? new Set<SuggestionMarkKind>()
      kinds.add(kind)
      markKinds.set(id, kinds)
      locations.push({
        from: position,
        mark,
        nodePosition: node.isText ? null : position,
        to: position + node.nodeSize,
      })
    }
  })

  for (const location of locations) {
    const id = String(location.mark.attrs.id)
    const kinds = markKinds.get(id)
    if (!kinds) throw new Error(`Suggestion ${id} has no generated mark composition`)
    const kind = semanticOverride ?? resolveSuggestionKind(
      id,
      kinds,
      locations
        .filter((candidate) => candidate.mark.attrs.id === id)
        .map((candidate) => candidate.mark),
    )
    const enriched = location.mark.type.create({
      ...location.mark.attrs,
      authorId: metadata.authorId,
      createdAt: metadata.createdAt,
      id,
      kind,
      patchId: metadata.patchId,
      suggestionId: id,
    })
    if (location.nodePosition === null) {
      transaction.removeMark(location.from, location.to, location.mark)
      transaction.addMark(location.from, location.to, enriched)
      continue
    }
    transaction.removeNodeMark(location.nodePosition, location.mark)
    transaction.addNodeMark(location.nodePosition, enriched)
  }
  return transaction
}

/** Below this a suggestion's creation time is Unix seconds; at or above it,
 * epoch milliseconds. The bound is the year 5138 in seconds and March 1973
 * in milliseconds, so no real time of either unit falls on the wrong side. */
const UNIX_SECONDS_UPPER_BOUND = 100_000_000_000

/** A suggestion's creation time in epoch milliseconds.
 *
 * A browser typing in suggest mode and the collaboration service publishing an
 * AI suggestion stamp epoch milliseconds; documents also hold AI suggestions
 * stamped in Unix seconds, written before the service moved to milliseconds.
 * Whatever shows or orders suggestions by time reads it through this, and the
 * stored value stays as written. */
export function suggestionCreatedAtMs(createdAt: number): number {
  return createdAt < UNIX_SECONDS_UPPER_BOUND ? createdAt * 1_000 : createdAt
}

/** Every suggestion of the document, oldest first. */
export function suggestionDescriptors(document: ProseMirrorNode): SuggestionDescriptor[] {
  const descriptors = new Map<string, SuggestionDescriptorAccumulator>()
  document.descendants((node, position, parent) => {
    for (const mark of node.marks) {
      if (!SUGGESTION_MARK_NAMES.has(mark.type.name as SuggestionMarkKind)) continue
      const descriptor = suggestionMarkDescriptor(mark)
      if (!parent) {
        throw new InvalidSuggestionMetadataError(
          'Suggestion mark has no structural parent',
          descriptor.suggestionId,
        )
      }
      const occurrence: SuggestionMarkOccurrence = {
        from: position,
        markKind: descriptor.markKind,
        parent,
        to: position + node.nodeSize,
      }
      const existing = descriptors.get(descriptor.suggestionId)
      if (existing && !sameSuggestionMetadata(existing, descriptor)) {
        throw new InvalidSuggestionMetadataError(
          `Suggestion ${descriptor.suggestionId} has inconsistent metadata`,
          descriptor.suggestionId,
        )
      }
      if (existing) {
        existing.declarations.push({
          declaredKind: descriptor.declaredKind,
          markKind: descriptor.markKind,
        })
        existing.markKinds.add(descriptor.markKind)
        existing.marks.push(descriptor.mark)
        existing.occurrences.push(occurrence)
        continue
      }
      descriptors.set(descriptor.suggestionId, {
        authorId: descriptor.authorId,
        createdAt: descriptor.createdAt,
        declarations: [{
          declaredKind: descriptor.declaredKind,
          markKind: descriptor.markKind,
        }],
        markKinds: new Set([descriptor.markKind]),
        marks: [descriptor.mark],
        occurrences: [occurrence],
        patchId: descriptor.patchId,
        suggestionId: descriptor.suggestionId,
      })
    }
  })
  const markDescriptors = [...descriptors.values()]
    .map((descriptor) => {
      const kind = resolveSuggestionKind(
        descriptor.suggestionId,
        descriptor.markKinds,
        descriptor.marks,
      )
      if (!hasValidKindDeclarations(kind, descriptor.declarations)) {
        throw new InvalidSuggestionMetadataError(
          `Suggestion ${descriptor.suggestionId} has an invalid mark composition`,
          descriptor.suggestionId,
        )
      }
      if (
        (kind === 'replacement' || kind === 'format')
        && descriptor.markKinds.has('deletion')
        && descriptor.markKinds.has('insertion')
      ) {
        assertAdjacentModificationPair(descriptor.suggestionId, descriptor.occurrences)
      }
      return {
        authorId: descriptor.authorId,
        createdAt: descriptor.createdAt,
        kind,
        patchId: descriptor.patchId,
        suggestionId: descriptor.suggestionId,
      }
    })
  const result = new Map(markDescriptors.map((descriptor) => [
    descriptor.suggestionId,
    descriptor,
  ]))
  document.descendants((node) => {
    const structure = node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR]
    if (structure === null || structure === undefined) return true
    if (!isStructureSuggestionData(structure)) {
      throw new InvalidSuggestionMetadataError(
        'Structural suggestion is missing authoritative metadata',
        null,
      )
    }
    if (result.has(structure.suggestionId)) {
      throw new InvalidSuggestionMetadataError(
        `Suggestion ${structure.suggestionId} has duplicate representations`,
        structure.suggestionId,
      )
    }
    result.set(structure.suggestionId, {
      authorId: structure.authorId,
      createdAt: structure.createdAt,
      kind: 'structure',
      patchId: structure.patchId,
      suggestionId: structure.suggestionId,
    })
    return true
  })
  return [...result.values()].sort((left, right) => (
      suggestionCreatedAtMs(left.createdAt) - suggestionCreatedAtMs(right.createdAt)
      || left.suggestionId.localeCompare(right.suggestionId)
    ))
}

function suggestionMarkDescriptor(mark: Mark): SuggestionMarkDescriptor {
  const markKind = mark.type.name as SuggestionMarkKind
  const { authorId, createdAt, id, kind, patchId, suggestionId } = mark.attrs
  if (
    !SUGGESTION_MARK_NAMES.has(markKind)
    || typeof id !== 'string'
    || id.length === 0
    || typeof suggestionId !== 'string'
    || suggestionId.length === 0
    || id !== suggestionId
    || !isSerializedSuggestionKind(kind)
    || typeof authorId !== 'string'
    || authorId.length === 0
    || typeof createdAt !== 'number'
    || !Number.isFinite(createdAt)
    || typeof patchId !== 'string'
    || patchId.length === 0
  ) {
    throw new InvalidSuggestionMetadataError(
      'Suggestion mark is missing authoritative metadata',
      typeof id === 'string' && id.length > 0 ? id : null,
    )
  }
  return {
    authorId,
    createdAt,
    declaredKind: kind as SerializedSuggestionKind,
    mark,
    markKind,
    patchId,
    suggestionId,
  }
}

function isSerializedSuggestionKind(value: unknown): value is SerializedSuggestionKind {
  return (
    value === 'deletion'
    || value === 'format'
    || value === 'insertion'
    || value === 'modification'
    || value === 'replacement'
    || value === 'structure'
  )
}

function hasValidKindDeclarations(
  resolvedKind: SuggestionKind,
  declarations: ReadonlyArray<{
    declaredKind: SerializedSuggestionKind
    markKind: SuggestionMarkKind
  }>,
): boolean {
  return declarations.every(({ declaredKind }) => (
    declaredKind === resolvedKind
    || (
      declaredKind === 'modification'
      && ['format', 'replacement', 'structure'].includes(resolvedKind)
    )
  ))
}

function assertAdjacentModificationPair(
  suggestionId: string,
  occurrences: readonly SuggestionMarkOccurrence[],
): void {
  const deletions = contiguousSuggestionRanges(occurrences, 'deletion')
  const insertions = contiguousSuggestionRanges(occurrences, 'insertion')
  const deletion = deletions[0]
  const insertion = insertions[0]
  if (
    deletions.length !== 1
    || insertions.length !== 1
    || !deletion
    || !insertion
    || deletion.parent !== insertion.parent
    || (deletion.to !== insertion.from && insertion.to !== deletion.from)
  ) {
    throw new InvalidSuggestionMetadataError(
      `Suggestion ${suggestionId} modification halves must be adjacent replacement ranges`,
      suggestionId,
    )
  }
}

function contiguousSuggestionRanges(
  occurrences: readonly SuggestionMarkOccurrence[],
  markKind: 'deletion' | 'insertion',
): Array<{ from: number; parent: ProseMirrorNode; to: number }> {
  const ranges: Array<{ from: number; parent: ProseMirrorNode; to: number }> = []
  const matching = occurrences
    .filter((occurrence) => occurrence.markKind === markKind)
    .sort((left, right) => left.from - right.from || left.to - right.to)
  for (const occurrence of matching) {
    const previous = ranges.at(-1)
    if (previous && previous.parent === occurrence.parent && previous.to === occurrence.from) {
      previous.to = occurrence.to
    } else {
      ranges.push({
        from: occurrence.from,
        parent: occurrence.parent,
        to: occurrence.to,
      })
    }
  }
  return ranges
}

function sameSuggestionMetadata(
  left: SuggestionMetadata & { suggestionId: string },
  right: SuggestionMetadata & { suggestionId: string },
): boolean {
  return (
    left.authorId === right.authorId
    && left.createdAt === right.createdAt
    && left.patchId === right.patchId
    && left.suggestionId === right.suggestionId
  )
}

function resolveSuggestionKind(
  suggestionId: string,
  markKinds: ReadonlySet<SuggestionMarkKind>,
  marks: readonly Mark[],
): SuggestionKind {
  const declaredKinds = new Set(
    marks
      .map((mark) => mark.attrs.kind)
      .filter((kind): kind is SuggestionKind => (
        kind === 'deletion'
        || kind === 'format'
        || kind === 'insertion'
        || kind === 'replacement'
        || kind === 'structure'
      )),
  )
  if (declaredKinds.size === 1) {
    const declared = [...declaredKinds][0]!
    if (
      (declared === 'format' || declared === 'replacement')
      && markKinds.size === 2
      && markKinds.has('deletion')
      && markKinds.has('insertion')
    ) return declared
    if (
      declared === 'structure'
      && markKinds.size === 1
      && markKinds.has('modification')
    ) return declared
  }
  if (markKinds.size === 1 && markKinds.has('insertion')) return 'insertion'
  if (markKinds.size === 1 && markKinds.has('deletion')) return 'deletion'
  if (markKinds.size === 1 && markKinds.has('modification')) {
    return marks.some((mark) => mark.attrs.type === 'nodeType')
      ? 'structure'
      : 'format'
  }
  if (
    markKinds.size === 2
    && markKinds.has('deletion')
    && markKinds.has('insertion')
  ) {
    return 'replacement'
  }
  throw new InvalidSuggestionMetadataError(
    `Suggestion ${suggestionId} has an unsupported mark composition`,
    suggestionId,
  )
}

function transformStructureCommand(
  proposed: Transaction,
  state: EditorState,
  metadata: SuggestionMetadata,
  command: StructureSuggestionCommand,
  generateSuggestionId: () => string,
): Transaction {
  if (command.action === 'table' || command.action === 'divider') {
    throw new UnsupportedSuggestionStructureError(
      command.action === 'table'
        ? 'Table topology cannot be proposed safely. Switch to Edit mode to insert a table.'
        : 'A divider cannot be reviewed as a reversible structural suggestion.',
      command.action === 'table' ? 'table_topology_unsupported' : 'divider_unsupported',
    )
  }
  const targetBefore = textblockPositionAt(
    state.doc,
    command.commandRange?.from ?? state.selection.from,
  )
  if (targetBefore === null) {
    throw new UnsupportedSuggestionStructureError(
      'The selected block cannot carry a structural suggestion.',
      'structure_target_unsupported',
    )
  }
  const sourceNode = state.doc.nodeAt(targetBefore)
  if (!sourceNode?.isTextblock) {
    throw new UnsupportedSuggestionStructureError(
      'The selected block cannot carry a structural suggestion.',
      'structure_target_unsupported',
    )
  }
  const transformed = state.tr
  let discardedCommand: StructureSuggestionData['discardedCommand']
  const trackedSlash = command.commandRange
    ? trackedSlashSuggestion(
        state.doc,
        command.commandRange,
        metadata.authorId,
      )
    : null
  if (command.commandRange) {
    const { from, to } = command.commandRange
    const contentFrom = targetBefore + 1
    const contentTo = targetBefore + sourceNode.nodeSize - 1
    if (
      !Number.isSafeInteger(from)
      || !Number.isSafeInteger(to)
      || from < contentFrom
      || to < from
      || to > contentTo
    ) {
      throw new UnsupportedSuggestionStructureError(
        'The slash command range is outside the selected block.',
        'structure_target_unsupported',
      )
    }
    const text = state.doc.textBetween(from, to, '', '')
    if (!text.startsWith('/') || text.length > 96) {
      throw new UnsupportedSuggestionStructureError(
        'The slash command is too large to preserve safely.',
        'structure_target_unsupported',
      )
    }
    if (!trackedSlash) {
      discardedCommand = {
        offset: from - contentFrom,
        text,
      }
    }
    transformed.delete(from, to)
  }
  const mappedTarget = transformed.mapping.map(targetBefore, 1)
  const expected = state.tr
  if (command.commandRange) {
    expected.delete(command.commandRange.from, command.commandRange.to)
  }
  const expectedTarget = expected.mapping.map(targetBefore, 1)
  applyStructureAction(expected, expectedTarget, command.action)
  if (!expected.doc.eq(proposed.doc)) {
    throw new UnsupportedSuggestionStructureError(
      'This block transformation cannot yet be represented as one reversible suggestion.',
      'unsupported_structure',
    )
  }
  const suggestionId = generateSuggestionId()
  if (suggestionId === trackedSlash?.suggestionId) {
    throw new UnsupportedSuggestionStructureError(
      'The structural suggestion identifier must differ from the slash insertion.',
      'suggestion_metadata_invalid',
    )
  }
  const structureMetadata = trackedSlash ?? metadata
  const structure: StructureSuggestionData = {
    action: command.action,
    authorId: structureMetadata.authorId,
    createdAt: structureMetadata.createdAt,
    ...(discardedCommand ? { discardedCommand } : {}),
    kind: 'structure',
    patchId: structureMetadata.patchId,
    suggestionId,
  }
  transformed.setNodeAttribute(
    mappedTarget,
    INQTRIX_STRUCTURE_SUGGESTION_ATTR,
    structure,
  )
  assertYjsCompatibleSuggestionStructure(transformed.doc)
  return transformed
}

/**
 * A real slash-menu interaction is two collaborative transactions: typing the
 * slash first creates an insertion suggestion, then selecting a command removes
 * that token and proposes a block transformation. Reuse the first suggestion's
 * patch metadata only when the command range consumes that complete, actor-owned
 * insertion. This keeps the durable patch non-empty and prevents a transient
 * command token from becoming a second review item.
 */
function trackedSlashSuggestion(
  document: ProseMirrorNode,
  range: { from: number; to: number },
  authorId: string,
): TrackedSlashSuggestion | null {
  const { from, to } = range
  const commandText = document.textBetween(from, to, '', '')
  if (
    !Number.isSafeInteger(from)
    || !Number.isSafeInteger(to)
    || from < 0
    || to <= from
    || to > document.content.size
    || !commandText.startsWith('/')
    || commandText.length > 96
  ) return null

  let candidate: TrackedSlashSuggestion | null = null
  let covered = 0
  let invalid = false
  document.nodesBetween(from, to, (node, position) => {
    if (!node.isText) return true
    const overlapFrom = Math.max(from, position)
    const overlapTo = Math.min(to, position + node.nodeSize)
    if (overlapTo <= overlapFrom) return false
    const suggestionMarks = node.marks.filter((mark) => (
      SUGGESTION_MARK_NAMES.has(mark.type.name)
    ))
    const insertion = suggestionMarks.length === 1
      && suggestionMarks[0]?.type.name === 'insertion'
      ? suggestionMarks[0]
      : null
    const attrs = insertion?.attrs
    if (
      !attrs
      || attrs.authorId !== authorId
      || attrs.kind !== 'insertion'
      || typeof attrs.patchId !== 'string'
      || attrs.patchId.length === 0
      || typeof attrs.suggestionId !== 'string'
      || attrs.suggestionId.length === 0
      || attrs.id !== attrs.suggestionId
      || !Number.isSafeInteger(attrs.createdAt)
      || Number(attrs.createdAt) < 0
    ) {
      invalid = true
      return false
    }
    const current: TrackedSlashSuggestion = {
      authorId,
      createdAt: Number(attrs.createdAt),
      patchId: attrs.patchId,
      suggestionId: attrs.suggestionId,
    }
    if (
      candidate
      && (
        candidate.createdAt !== current.createdAt
        || candidate.patchId !== current.patchId
        || candidate.suggestionId !== current.suggestionId
      )
    ) {
      invalid = true
      return false
    }
    candidate = current
    covered += overlapTo - overlapFrom
    return false
  })
  if (invalid || !candidate || covered !== to - from) return null

  let occurrenceSize = 0
  document.descendants((node, position) => {
    if (!node.isText) return true
    const matches = node.marks.some((mark) => (
      mark.type.name === 'insertion'
      && mark.attrs.suggestionId === candidate?.suggestionId
    ))
    if (!matches) return true
    if (position < from || position + node.nodeSize > to) invalid = true
    occurrenceSize += node.nodeSize
    return true
  })
  return !invalid && occurrenceSize === to - from ? candidate : null
}

function textblockPositionAt(document: ProseMirrorNode, position: number): number | null {
  const safePosition = Math.max(0, Math.min(position, document.content.size))
  const resolved = document.resolve(safePosition)
  for (let depth = resolved.depth; depth >= 1; depth -= 1) {
    if (resolved.node(depth).isTextblock) return resolved.before(depth)
  }
  const node = document.nodeAt(safePosition)
  return node?.isTextblock ? safePosition : null
}

function applyStructureAction(
  transaction: Transaction,
  position: number,
  action: StructureSuggestionAction,
): void {
  const node = transaction.doc.nodeAt(position)
  if (!node?.isTextblock) {
    throw new UnsupportedSuggestionStructureError(
      'The structural suggestion target no longer exists.',
      'structure_target_unsupported',
    )
  }
  const schema = transaction.doc.type.schema
  if (action === 'paragraph') {
    const paragraph = schema.nodes.paragraph
    if (!paragraph) {
      throw new UnsupportedSuggestionStructureError(
        'Paragraphs are unavailable.',
        'unsupported_structure',
      )
    }
    transaction.setNodeMarkup(position, paragraph, {
      textAlign: node.attrs.textAlign ?? null,
    })
    return
  }
  if (action === 'codeBlock') {
    const codeBlock = schema.nodes.codeBlock
    const paragraph = schema.nodes.paragraph
    if (!codeBlock || !paragraph) {
      throw new UnsupportedSuggestionStructureError(
        'Code blocks are unavailable.',
        'unsupported_structure',
      )
    }
    transaction.setNodeMarkup(
      position,
      node.type === codeBlock ? paragraph : codeBlock,
      node.type === codeBlock ? { textAlign: null } : null,
    )
    return
  }
  if (action.startsWith('heading')) {
    const heading = schema.nodes.heading
    const paragraph = schema.nodes.paragraph
    if (!heading || !paragraph) {
      throw new UnsupportedSuggestionStructureError(
        'Headings are unavailable.',
        'unsupported_structure',
      )
    }
    const level = Number(action.at(-1))
    const targetType = node.type === heading && node.attrs.level === level
      ? paragraph
      : heading
    transaction.setNodeMarkup(position, targetType, targetType === heading
      ? { level, textAlign: node.attrs.textAlign ?? null }
      : { textAlign: node.attrs.textAlign ?? null })
    return
  }
  const wrapperName = action === 'blockquote'
    ? 'blockquote'
    : action === 'bulletList'
      ? 'bulletList'
      : action === 'orderedList'
        ? 'orderedList'
        : 'taskList'
  const wrapper = schema.nodes[wrapperName]
  if (!wrapper) {
    throw new UnsupportedSuggestionStructureError(
      'The requested block structure is unavailable.',
      'unsupported_structure',
    )
  }
  const from = transaction.doc.resolve(position)
  const to = transaction.doc.resolve(position + node.nodeSize)
  const range = from.blockRange(to)
  const wrapping = range ? findWrapping(range, wrapper) : null
  if (!range || !wrapping) {
    throw new UnsupportedSuggestionStructureError(
      'Only an unwrapped text block can be proposed as this structure.',
      'structure_target_unsupported',
    )
  }
  transaction.wrap(range, wrapping)
}

export function resolveStructureSuggestion(
  state: EditorState,
  suggestionId: string,
  decision: 'accept' | 'reject',
): EditorState {
  const locations = structureSuggestionLocations(state.doc)
    .filter((location) => location.data.suggestionId === suggestionId)
  if (locations.length !== 1) {
    throw new Error(`Structural suggestion ${suggestionId} is missing or duplicated`)
  }
  const transaction = state.tr
  const location = locations[0]!
  transaction.setNodeAttribute(
    location.position,
    INQTRIX_STRUCTURE_SUGGESTION_ATTR,
    null,
  )
  if (decision === 'accept') {
    applyStructureAction(transaction, location.position, location.data.action)
  }
  return state.apply(transaction)
}

function projectStructureSuggestions(
  state: EditorState,
  projection: 'final' | 'original',
  onInapplicable: ((suggestionId: string) => void) | null = null,
): EditorState {
  const locations = structureSuggestionLocations(state.doc)
    .sort((left, right) => right.position - left.position)
  let current = state
  for (const location of locations) {
    const live = structureSuggestionLocations(current.doc)
      .find((candidate) => candidate.data.suggestionId === location.data.suggestionId)
    if (!live) throw new Error(`Structural suggestion ${location.data.suggestionId} disappeared`)
    const transaction = current.tr
    transaction.setNodeAttribute(
      live.position,
      INQTRIX_STRUCTURE_SUGGESTION_ATTR,
      null,
    )
    if (projection === 'final' && onInapplicable) {
      const attempt = current.tr.setNodeAttribute(
        live.position,
        INQTRIX_STRUCTURE_SUGGESTION_ATTR,
        null,
      )
      try {
        applyStructureAction(attempt, live.position, live.data.action)
        current = current.apply(attempt)
      } catch {
        onInapplicable(live.data.suggestionId)
        current = current.apply(transaction)
      }
      continue
    }
    if (projection === 'final') {
      applyStructureAction(transaction, live.position, live.data.action)
    } else if (live.data.discardedCommand) {
      // The block's text may have been edited directly since the command
      // was typed; the command goes back at its offset, or at the end of
      // the text that is left.
      const block = current.doc.nodeAt(live.position)
      const insertionAt = (
        live.position
        + 1
        + Math.min(live.data.discardedCommand.offset, block?.content.size ?? 0)
      )
      transaction.insertText(live.data.discardedCommand.text, insertionAt)
    }
    current = current.apply(transaction)
  }
  return current
}

function structureSuggestionLocations(document: ProseMirrorNode): Array<{
  data: StructureSuggestionData
  position: number
}> {
  const locations: Array<{ data: StructureSuggestionData; position: number }> = []
  document.descendants((node, position) => {
    const data = node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR]
    if (data === null || data === undefined) return true
    if (!node.isTextblock || !isStructureSuggestionData(data)) {
      throw new Error('Structural suggestion metadata is invalid')
    }
    locations.push({ data, position })
    return true
  })
  return locations
}

export function assertYjsCompatibleSuggestionStructure(
  document: ProseMirrorNode,
): void {
  let unsupportedNodeType: string | null = null
  const inspect = (node: ProseMirrorNode): boolean => {
    if (
      !node.isText
      && node.marks.some((mark) => (
        SUGGESTION_MARK_NAMES.has(mark.type.name as SuggestionMarkKind)
      ))
    ) {
      unsupportedNodeType = node.type.name
      return false
    }
    return true
  }
  inspect(document)
  document.descendants((node) => {
    if (unsupportedNodeType) return false
    return inspect(node)
  })
  if (unsupportedNodeType) {
    throw new UnsupportedSuggestionStructureError(
      `Node-level suggestions on ${unsupportedNodeType} cannot be represented by the Yjs editor codec`,
      'node_marks_unsupported',
    )
  }
  const structureIds = new Set<string>()
  document.descendants((node) => {
    const structure = node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR]
    if (structure === null || structure === undefined) return true
    if (
      !node.isTextblock
      || !isStructureSuggestionData(structure)
      || structureIds.has(structure.suggestionId)
    ) {
      throw new UnsupportedSuggestionStructureError(
        'Structural suggestion metadata is invalid or duplicated',
        'suggestion_metadata_invalid',
      )
    }
    structureIds.add(structure.suggestionId)
    return true
  })
}

export function projectFinalDocument(document: ProseMirrorNode): ProseMirrorNode {
  return runProjection(document, 'final')
}

/**
 * The structural suggestions whose action cannot be applied in the final
 * projection, for example a code block proposed for a heading that holds
 * formatted text, or a code block proposed for the paragraph a list item
 * starts with. Each is tried on its own and skipped when it fails, so one
 * such suggestion does not hide another. Throws when the projection of the
 * other suggestions fails.
 */
export function inapplicableStructureSuggestionIds(document: ProseMirrorNode): Set<string> {
  const inapplicable = new Set<string>()
  runProjection(document, 'final', (suggestionId) => inapplicable.add(suggestionId))
  return inapplicable
}

export function projectOriginalDocument(document: ProseMirrorNode): ProseMirrorNode {
  return runProjection(document, 'original')
}

function runProjection(
  document: ProseMirrorNode,
  projection: 'final' | 'original',
  onInapplicable: ((suggestionId: string) => void) | null = null,
): ProseMirrorNode {
  let state = EditorState.create({ schema: document.type.schema, doc: document })
  if (projection === 'final') {
    // Accepting a proposed block break keeps the break, not the zero-width
    // spaces that anchor it.
    const cleanup = addProposedBlockAnchorCleanup(state.tr)
    if (cleanup.docChanged) state = state.apply(cleanup)
  }
  const command = projection === 'final' ? applySuggestions : revertSuggestions
  const handled = command(state, (transaction) => {
    state = state.apply(transaction)
  })
  if (!handled) {
    throw new Error('Suggestion projection could not be applied')
  }
  return projectStructureSuggestions(state, projection, onInapplicable).doc
}

export function projectFinalJson(document: ProseMirrorNode): JSONContent {
  return projectFinalDocument(document).toJSON()
}

export function projectOriginalJson(document: ProseMirrorNode): JSONContent {
  return projectOriginalDocument(document).toJSON()
}
