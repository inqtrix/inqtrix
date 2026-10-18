import { randomUUID } from 'node:crypto'

import { applySuggestions } from '@handlewithcare/prosemirror-suggest-changes'
import { getSchema } from '@tiptap/core'
import type { Node as ProseMirrorNode, Slice } from '@tiptap/pm/model'
import { EditorState, type Transaction } from '@tiptap/pm/state'
import { Mapping, type Step } from '@tiptap/pm/transform'
import { initProseMirrorDoc, updateYFragment } from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  SUGGESTION_MARK_NAMES,
  UnsupportedSuggestionStructureError,
  addProposedBlockAnchorCleanup,
  completeTextblocksCreatedSince,
  createEditorSchemaExtensions,
  parseEditorMarkdown,
  serializeEditorJson,
  suggestionDescriptors,
  transformToInqtrixSuggestionTransaction,
  wordSpans,
  type SuggestionDescriptor,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import { CloseCodes, CollaborationError } from './errors'
import { rebasePublication, type PublicationPlacement } from './publicationRebase'
import {
  collectSuggestionRecords,
  hasSameNonTextAtoms,
  isReversibleSuggestionStructure,
  sameSuggestionRecord,
  suggestionPositionAt,
} from './suggestPolicy'

export type PublishSuggestionInput = {
  actorUserId: string
  /** The projection the target was built on (`parsePublicationBase`). With
   * it, the AI section is placed onto the document as it is now; without
   * it, the target is compared with the document as it is. */
  base: ProseMirrorNode | null
  patchId: string
  /** The whole document as the publication proposes it
   * (`parseRoundTrippableTarget`). */
  target: ProseMirrorNode
}

export type PublishSuggestionResult = {
  patchIds: string[]
  /** How the target was placed when a base came with it, else null. */
  placement: PublicationPlacement['outcome'] | null
  suggestions: SuggestionDescriptor[]
  suggestionIds: string[]
}

export type SuggestionPublisherDependencies = {
  createSuggestionId?: () => string
  /** The publication time in epoch milliseconds, the unit a browser stamps
   * a typed suggestion with. */
  nowMilliseconds?: () => number
}

/**
 * The most new blocks one publication may propose. Each new block is a
 * break step of its own, and turning the steps into suggestions costs time
 * that grows with the square of their number, on the single thread every
 * room of this service shares; a rewrite adds a handful. A target with more
 * is refused before any step is built.
 */
export const MAX_PROPOSED_BLOCK_BREAKS = 50

/** Blocks whose end can carry the break that starts a new block after them:
 * a paragraph (also inside a list item, task item or quote) and a heading.
 * Enter at the end of a code block adds a line, not a block, so a block
 * after a code block has no break to stand for it. */
const BREAK_HOSTS: ReadonlySet<string> = new Set(['heading', 'paragraph'])

/** The containers a new block may arrive in: a new list or task item
 * around its paragraph. */
const NEW_BLOCK_CONTAINERS: ReadonlySet<string> = new Set(['listItem', 'taskItem'])

const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

export function publishTargetSuggestion(
  document: Y.Doc,
  input: PublishSuggestionInput,
  dependencies: SuggestionPublisherDependencies = {},
): PublishSuggestionResult {
  const fragment = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, schema)
  const state = EditorState.create({ doc: initialized.doc, schema })
  const beforeDescriptors = suggestionDescriptors(state.doc)
  if (beforeDescriptors.some((descriptor) => descriptor.patchId === input.patchId)) {
    throw suggestionConflict('patch_already_published')
  }

  const finalProjection = projectFinal(state)
  const placement = input.base
    ? rebasePublication(input.base, input.target, finalProjection.state.doc)
    : null
  const target = placement?.target ?? input.target
  if (!hasSameNonTextAtoms(finalProjection.state.doc.toJSON(), target.toJSON())) {
    throw unsupportedStructure('non_text_atoms_changed')
  }
  const canonicalTarget = serializeEditorJson(target.toJSON(), 'final')
  const currentFinal = serializeEditorJson(finalProjection.state.doc.toJSON(), 'final')
  if (canonicalTarget === currentFinal) throw suggestionConflict('target_unchanged')

  let direct: Transaction
  const toDocument = finalProjection.mapping.invert()
  try {
    const projected = createDiffSteps(
      finalProjection.state,
      target,
      (from, to) => holdsSuggestion(state.doc, toDocument.map(from, -1), toDocument.map(to, 1)),
    )
    const projectedStep = projected.first.steps[0]
    if (!projectedStep || projected.first.steps.length !== 1) {
      throw unsupportedStructure('diff_not_one_step')
    }
    const mappedStep = projectedStep.map(toDocument)
    if (!mappedStep) throw suggestionConflict('diff_not_mappable')
    direct = state.tr.step(mappedStep)
    // Each further break lies entirely inside a block this transaction
    // adds, so it needs no mapping: it goes to the end of the block the step
    // before it created, as Enter at the end of what was just typed does.
    for (const slice of projected.followingBreaks) {
      const at = insertedEnd(direct.steps[direct.steps.length - 1]!)
      direct.replace(at, at, slice)
    }
  } catch (error) {
    if (error instanceof CollaborationError) throw error
    throw unsupportedStructure('diff_failed')
  }

  const createdAt = (dependencies.nowMilliseconds ?? Date.now)()
  if (!Number.isSafeInteger(createdAt) || createdAt < 0) {
    throw new CollaborationError('internal_consistency', {
      closeCode: CloseCodes.internalConsistency,
    })
  }

  let transformed: Transaction
  try {
    transformed = transformToInqtrixSuggestionTransaction(
      direct,
      state,
      {
        authorId: input.actorUserId,
        createdAt,
        patchId: input.patchId,
      },
      dependencies.createSuggestionId ?? randomUUID,
    )
  } catch (error) {
    if (error instanceof UnsupportedSuggestionStructureError) {
      throw unsupportedStructure(error.reason)
    }
    throw suggestionConflict('suggestion_transform_failed')
  }
  if (!transformed.docChanged) throw suggestionConflict('suggestion_without_change')

  const next = state.apply(transformed)
  const afterDescriptors = suggestionDescriptors(next.doc)
  const created = afterDescriptors
    .filter((descriptor) => descriptor.patchId === input.patchId)
    .sort((left, right) => left.suggestionId.localeCompare(right.suggestionId))
  if (
    created.length === 0
    || created.some((descriptor) => (
      descriptor.authorId !== input.actorUserId
      || descriptor.createdAt !== createdAt
    ))
  ) {
    throw suggestionConflict('suggestion_metadata_mismatch')
  }

  assertExistingSuggestionsUnchanged(
    state.doc,
    next.doc,
    input.patchId,
    transformed,
  )
  if (!isReversibleSuggestionStructure(state.doc.toJSON(), next.doc.toJSON())) {
    throw suggestionConflict('suggestion_not_reversible')
  }
  if (serializeEditorJson(next.doc.toJSON(), 'final') !== canonicalTarget) {
    throw suggestionConflict('final_differs_from_target')
  }

  // A block the publication writes anew holds its empty text node when it has
  // no content, in the same row.
  const since = Y.getState(document.store, document.clientID)
  updateYFragment(document, fragment, next.doc, initialized.meta)
  completeTextblocksCreatedSince(document, since)
  return {
    patchIds: [input.patchId],
    placement: placement?.outcome ?? null,
    suggestions: created,
    suggestionIds: created.map((descriptor) => descriptor.suggestionId),
  }
}

/**
 * The final projection the target is compared with, and the mapping that
 * leads back from it to the document. It drops the anchors of proposed block
 * breaks, as every final projection does, so a pending break never shows up
 * as a difference the target would have to remove.
 */
function projectFinal(state: EditorState): {
  mapping: Mapping
  state: EditorState
} {
  const cleanup = addProposedBlockAnchorCleanup(state.tr)
  const cleaned = cleanup.docChanged ? state.apply(cleanup) : state
  let projection: Transaction | null = null
  const handled = applySuggestions(cleaned, (transaction) => {
    projection = transaction
  })
  if (!handled || !projection) {
    throw new CollaborationError('internal_consistency', {
      closeCode: CloseCodes.internalConsistency,
    })
  }
  const transaction = projection as Transaction
  return {
    mapping: new Mapping([...cleanup.mapping.maps, ...transaction.mapping.maps]),
    state: cleaned.apply(transaction),
  }
}

/** A publication's target, read from its Markdown. It must read back as
 * itself after the canonical serialization, or the publication would store
 * something other than what was proposed. Callers parse before they enter a
 * room's queue: parsing a large document takes long, and the queue is shared
 * by every writer of the room. */
export function parseRoundTrippableTarget(markdown: string): ProseMirrorNode {
  try {
    const target = schema.nodeFromJSON(parseEditorMarkdown(markdown))
    const canonical = serializeEditorJson(target.toJSON(), 'final')
    const reparsed = schema.nodeFromJSON(parseEditorMarkdown(canonical))
    if (!target.eq(reparsed)) throw invalidSchema('target_not_round_trippable')
    return target
  } catch (error) {
    if (error instanceof CollaborationError) throw error
    throw invalidSchema('target_unparsable')
  }
}

/** The projection a publication was built on, read from its Markdown. It is
 * not required to read back as itself: it only locates the AI section and
 * the changes since, and a base that differs from the canonical form can at
 * most widen what counts as changed. */
export function parsePublicationBase(markdown: string): ProseMirrorNode {
  try {
    return schema.nodeFromJSON(parseEditorMarkdown(markdown))
  } catch {
    throw invalidSchema('base_unparsable')
  }
}

type DiffSteps = {
  /** The one step the projection's mapping leads back into the document. */
  first: Transaction
  /** Breaks for further new blocks, each at the end of the block the step
   * before it creates. Empty unless several blocks are new. */
  followingBreaks: Slice[]
}

function createDiffSteps(
  state: EditorState,
  target: ProseMirrorNode,
  keepsMinimal: (from: number, to: number) => boolean,
): DiffSteps {
  const start = state.doc.content.findDiffStart(target.content)
  const ends = state.doc.content.findDiffEnd(target.content)
  if (start === null || ends === null) throw suggestionConflict('target_unchanged')
  let from = start
  let endCurrent = ends.a
  let endTarget = ends.b
  const overlap = from - Math.min(endCurrent, endTarget)
  if (overlap > 0) {
    endCurrent += overlap
    endTarget += overlap
  }
  const words = widenToWords(state.doc, target, { endCurrent, endTarget, from }, keepsMinimal)
  from = words.from
  endCurrent = words.endCurrent
  endTarget = words.endTarget
  // New blocks that begin like the paragraph after them ("Die ..." before
  // "Die ...") are found inside it, after the shared beginning, where they
  // would split it and move its text. Inserted at its start they give the
  // same result, and only there do the shapes below recognise them. An
  // insertion within one textblock, or at a textblock's end, stays as is.
  if (from === endCurrent) {
    const inserted = target.resolve(from)
    const offset = inserted.parentOffset
    if (
      inserted.parent.isTextblock
      && offset > 0
      && offset < state.doc.resolve(from).parent.content.size
      && inserted.sharedDepth(endTarget) < inserted.depth
      && state.tr.replace(
        from - offset,
        from - offset,
        target.slice(from - offset, endTarget - offset),
      ).doc.eq(target)
    ) {
      from -= offset
      endCurrent -= offset
      endTarget -= offset
    }
  }
  // New blocks before a paragraph are found at the START of that paragraph.
  // At the boundary before it they are the same insertion, proposed one
  // break per new block as anywhere else; inserted whole, every block after
  // the first could only be marked as a node.
  if (from === endCurrent && startsTextblock(state.doc, from)) {
    const boundary = blockBreakSteps(state, target, from - 1, from - 1, endTarget - 1)
    if (boundary) return boundary
  }
  // A new block between two blocks is found at the START of the following
  // block, which would move that block's text into a new block. Suggest mode
  // proposes a new block only at the END of a block, so the same insertion
  // moves left across the shared block boundary while the result stays the
  // target.
  while (
    from === endCurrent
    && from >= 2
    && startsTextblock(state.doc, from)
    && startsTextblock(target, from)
    && state.doc.resolve(from - 2).parent.isTextblock
  ) {
    const shifted = state.tr.replace(
      from - 2,
      endCurrent - 2,
      target.slice(from - 2, endTarget - 2),
    )
    if (!shifted.doc.eq(target)) break
    from -= 2
    endCurrent -= 2
    endTarget -= 2
  }
  const breaks = blockBreakSteps(state, target, from, endCurrent, endTarget)
  if (breaks) return breaks
  const transaction = state.tr.replace(
    from,
    endCurrent,
    target.slice(from, endTarget),
  )
  if (!transaction.doc.eq(target)) throw unsupportedStructure('diff_not_reproducible')
  return { first: transaction, followingBreaks: [] }
}

type ChangeRange = { endCurrent: number; endTarget: number; from: number }

/** An inline leaf (a hard break, a formula) ends a word; one character per
 * leaf keeps text offsets equal to document positions. */
const LEAF_BOUNDARY = ' '

/**
 * The change widened to whole words (`wordSpans`, a number with its
 * separators being one) where one of its ends splits a word, so its
 * suggestion reads "Kosten" to "Kostet" and "18.000" to "21.500", not "n" to
 * "t" and "18.0" to "21.5". Only a change inside one textblock widens, and
 * never onto text `keepsMinimal` names: a word that holds another writer's
 * pending suggestion keeps the minimal change, so a publication still never
 * touches another suggestion. The added text is the same before and after
 * the change, so the step still reproduces the target.
 */
function widenToWords(
  current: ProseMirrorNode,
  target: ProseMirrorNode,
  change: ChangeRange,
  keepsMinimal: (from: number, to: number) => boolean,
): ChangeRange {
  const $from = current.resolve(change.from)
  const $targetFrom = target.resolve(change.from)
  if (
    !$from.parent.isTextblock
    || !$targetFrom.parent.isTextblock
    || $from.start() !== $targetFrom.start()
    || !$from.sameParent(current.resolve(change.endCurrent))
    || !$targetFrom.sameParent(target.resolve(change.endTarget))
  ) return change
  const blockStart = $from.start()
  const currentWords = wordSpans(current.textBetween(blockStart, $from.end(), '', LEAF_BOUNDARY))
  const targetWords = wordSpans(target.textBetween(blockStart, $targetFrom.end(), '', LEAF_BOUNDARY))
  const from = change.from - blockStart
  const endCurrent = change.endCurrent - blockStart
  const endTarget = change.endTarget - blockStart
  const wordStart = Math.min(
    from,
    ...[...currentWords, ...targetWords]
      .filter(([start, end]) => start < from && from < end)
      .map(([start]) => start),
  )
  const wordRest = Math.max(
    0,
    ...currentWords
      .filter(([start, end]) => start < endCurrent && endCurrent < end)
      .map(([, end]) => end - endCurrent),
    ...targetWords
      .filter(([start, end]) => start < endTarget && endTarget < end)
      .map(([, end]) => end - endTarget),
  )
  const widenStart = wordStart < from
    && !keepsMinimal(blockStart + wordStart, change.from)
  const widenEnd = wordRest > 0
    && !keepsMinimal(change.endCurrent, change.endCurrent + wordRest)
  return {
    endCurrent: change.endCurrent + (widenEnd ? wordRest : 0),
    endTarget: change.endTarget + (widenEnd ? wordRest : 0),
    from: widenStart ? blockStart + wordStart : change.from,
  }
}

/** Whether any text between `from` and `to` carries a suggestion mark. */
function holdsSuggestion(document: ProseMirrorNode, from: number, to: number): boolean {
  let found = false
  document.nodesBetween(from, to, (node) => {
    if (node.marks.some((mark) => SUGGESTION_MARK_NAMES.has(mark.type.name))) found = true
    return !found
  })
  return found
}

/**
 * New blocks inserted at a block boundary, proposed the way a writer in
 * suggest mode makes them and Word, Google Docs and CKEditor record them:
 * a break at the end of the textblock before, then the text, one break
 * per new block. Inserted whole, a block can only be marked as a node,
 * which the shared document cannot hold.
 *
 * Applies when the target adds only textblocks (alone or in new list or
 * task items) right after the end of a paragraph or heading. Returns null
 * for any other shape; the single replacement then refuses what has no
 * suggestion form, as before.
 */
function blockBreakSteps(
  state: EditorState,
  target: ProseMirrorNode,
  from: number,
  endCurrent: number,
  endTarget: number,
): DiffSteps | null {
  if (from !== endCurrent || state.doc.resolve(from).parent.isTextblock) return null
  const ends = newTextblockEnds(target, from, endTarget)
  if (!ends) return null
  // The textblock the first break starts from ends one level (a paragraph
  // or heading) or two levels (the paragraph of a list or task item) before
  // the boundary.
  for (let depth = 1; depth <= 3 && from - depth >= 0; depth += 1) {
    const host = state.doc.resolve(from - depth)
    if (!host.parent.isTextblock) continue
    if (
      !BREAK_HOSTS.has(host.parent.type.name)
      || host.parentOffset !== host.parent.content.size
      || ends[ends.length - 1] !== endTarget - depth
    ) return null
    const cuts = [from - depth, ...ends]
    const slices: Slice[] = []
    const built = state.tr
    for (let index = 0; index + 1 < cuts.length; index += 1) {
      const slice = target.slice(cuts[index]!, cuts[index + 1]!)
      built.replace(cuts[index]!, cuts[index]!, slice)
      slices.push(slice)
    }
    if (!built.doc.eq(target)) return null
    return {
      first: state.tr.replace(cuts[0]!, cuts[0]!, slices[0]!),
      followingBreaks: slices.slice(1),
    }
  }
  return null
}

/**
 * The content end of each textblock an insertion adds, or null when it adds
 * anything else. Counted before any step is built: more than
 * MAX_PROPOSED_BLOCK_BREAKS is refused here.
 */
function newTextblockEnds(
  target: ProseMirrorNode,
  from: number,
  to: number,
): number[] | null {
  const inserted = target.slice(from, to)
  if (inserted.openStart !== 0 || inserted.openEnd !== 0) return null
  const ends: number[] = []
  let otherContent = false
  target.nodesBetween(from, to, (node, position) => {
    if (otherContent) return false
    // The blocks around the insertion: look inside them.
    if (position < from) return true
    if (node.isTextblock) {
      if (ends.length === MAX_PROPOSED_BLOCK_BREAKS) {
        throw unsupportedStructure('too_many_new_blocks')
      }
      ends.push(position + node.nodeSize - 1)
      return false
    }
    if (NEW_BLOCK_CONTAINERS.has(node.type.name)) return true
    otherContent = true
    return false
  })
  return otherContent || ends.length === 0 ? null : ends
}

/** Where the content a step inserted ends, in the document after it. */
function insertedEnd(step: Step): number {
  let end: number | null = null
  step.getMap().forEach((_oldStart, _oldEnd, _newStart, newEnd) => {
    end = newEnd
  })
  if (end === null) throw suggestionConflict('diff_not_mappable')
  return end
}

function startsTextblock(document: ProseMirrorNode, position: number): boolean {
  const resolved = document.resolve(position)
  return resolved.parent.isTextblock && resolved.parentOffset === 0
}

function assertExistingSuggestionsUnchanged(
  before: ProseMirrorNode,
  after: ProseMirrorNode,
  newPatchId: string,
  transaction: Transaction,
): void {
  const beforeRecords = collectSuggestionRecords(before.toJSON())
  const afterRecords = collectSuggestionRecords(after.toJSON())
  for (const [id, record] of beforeRecords) {
    if (record.patchId === newPatchId) continue
    const next = afterRecords.get(id)
    if (!next || !sameSuggestionRecord(record, next, (position) => (
      suggestionPositionAt(
        after,
        transaction.mapping.map(position.from, 1),
        transaction.mapping.map(position.to, -1),
      )
    ))) throw suggestionConflict('existing_suggestion_changed')
  }
  for (const [id, record] of afterRecords) {
    if (record.patchId === newPatchId) continue
    if (!beforeRecords.has(id)) throw suggestionConflict('existing_suggestion_changed')
  }
}

// Every refusal names the rule behind it (`suggestionReason`), so the log
// and the answer say why; `reason` stays the fixed vocabulary clients use.

function invalidSchema(suggestionReason: string): CollaborationError {
  return new CollaborationError('invalid_schema', {
    closeCode: CloseCodes.incompatible,
    httpStatus: 409,
    suggestionReason,
  })
}

function suggestionConflict(suggestionReason: string): CollaborationError {
  return new CollaborationError('suggestion_conflict', {
    closeCode: CloseCodes.incompatible,
    httpStatus: 409,
    suggestionReason,
  })
}

function unsupportedStructure(suggestionReason: string): CollaborationError {
  return new CollaborationError('unsupported_suggestion_structure', {
    closeCode: CloseCodes.incompatible,
    httpStatus: 409,
    suggestionReason,
  })
}
