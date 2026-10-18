import { createHash } from 'node:crypto'

import { getSchema, type JSONContent } from '@tiptap/core'
import { Fragment, type Node as ProseMirrorNode } from '@tiptap/pm/model'
import { yXmlFragmentToProsemirrorJSON } from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  InvalidSuggestionMetadataError,
  SUGGESTION_MARK_NAMES,
  completeTextblocksCreatedSince,
  createEditorSchemaExtensions,
  isStructureSuggestionData,
  suggestionDescriptors,
  type SuggestionDescriptor,
  type SuggestionMarkKind,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import { hashBytes, validateDocument } from './documentState'
import { CollaborationError } from './errors'
import { collectSuggestionRecords, reviewEditOfSuggestions, type YjsDeleteSet } from './suggestPolicy'

/**
 * Normalization of what an edit-lease update leaves behind when it meets
 * concurrent changes in the room.
 *
 * A writer's update U is computed against the writer's own view. Merged
 * into the room R, it can leave a state the checks refuse although the
 * writer did nothing they were not allowed to: typed characters inherit
 * another person's suggestion mark, a mark the writer set meets a mark it
 * excludes, a list loses its last item to two deletions, or a copy of a
 * suggestion comes back from a stale view after it was decided. Refusing
 * such an update ends the writer's session although edit rights cover a
 * resolution.
 *
 * So, and only after a check refused R + U, the service plans a deterministic
 * normalization N on R + U and persists U and N together as ONE row in the
 * writer's own name, under the writer's own lease. The room applies the row
 * as one update, so no connection ever receives R + U. Every rule only takes
 * back what U contributed to a conflict, completes structure the room held,
 * applies the recorded outcome of a decided suggestion, or, as the last
 * resort, removes a suggestion, which edit rights allow. Nothing is ever
 * written in another person's name, and a suggest-only lease is never
 * normalized.
 *
 * One round runs these rules on a copy of the current state, each on the
 * texts the update touched:
 *
 * - `inherited_mark`: a character U created carries another person's
 *   suggestion mark it inherited from formatting U did not write. The mark is
 *   cleared on it: edit-mode text never proposes in another person's name.
 * - `excluded_mark`: two marks that exclude each other meet on one character
 *   and exactly one of them, a formatting mark, was set by U. U's mark
 *   yields. A suggestion U proposes is never trimmed this way.
 * - `resurrected_suggestion`: a suggestion the room no longer holds comes
 *   back through U (a copy re-created from a view older than its decision,
 *   or written back by an undo), whoever proposed it. The copy takes the
 *   suggestion's recorded outcome: an accepted insertion keeps its text
 *   without the mark, an accepted deletion loses its text, a rejected,
 *   removed or superseded suggestion is removed the way a rejection removes
 *   it. Only a suggestion this room saw leave with the same author, patch,
 *   time and kind qualifies. Any other record in another person's name,
 *   including one decided before the room was loaded, refuses the update;
 *   the actor's own record new to the room is the actor's proposal.
 * - `empty_container`: a list, list item or quote the room held and the merge
 *   left empty gets the schema's minimal content instead of being deleted, so
 *   a third writer's concurrent item keeps its place.
 * - `redundant_format`: formatting items that change nothing and only split
 *   a run are removed, except a value U wrote that no reader sees.
 *
 * Then the round judges the settled state the way the suggestion policy does.
 * Editing may take characters away from another person's suggestion and
 * format it; what is left of it stays pending. Two cases remain that no rule
 * above resolves, and there the suggestion is removed as a whole, the way a
 * rejection removes it (its proposed text deleted, its deletion marks and
 * structure data removed), and the policy names it as removed in the
 * writer's row:
 *
 * - `invalid_composition`: the merge left a suggestion, whoever proposed
 *   it, in a form the schema refuses: a replacement or format suggestion
 *   whose halves new text separated or that lost one half (a pair is only
 *   valid as two adjacent halves), or a structure suggestion a split copied
 *   onto a second block.
 * - `altered_suggestion`: the update left another person's suggestion
 *   neither on a subset of its own characters nor written again in place
 *   (it gained characters, moved or changed its attributes), or removed it
 *   without deleting the text it proposed.
 *
 * Rejecting a format suggestion would mean reverting formatting, and
 * accepting a structure suggestion would mean restructuring; neither is done
 * here, so such an update stays refused. So does one that writes in another
 * person's name.
 *
 * After each round R + (U + N) is rebuilt exactly as the room and every
 * replay of the store apply it, including the formatting cleanup Yjs runs
 * after it, which is folded into the row. At most three rounds; the checks
 * then judge the result and a refusal stands.
 *
 * The bytes are deterministic for the same room state and update: N is
 * written by a Yjs client derived from both, so a second preparation of the
 * same update on the same state produces the same row and the same hash,
 * which the store deduplicates.
 */

const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

export const MAX_NORMALIZATION_ROUNDS = 3

/**
 * Items and characters one normalization may read, over all its rounds.
 * Beyond it the update is refused as it would have been without
 * normalization. The rules read only the texts the update touched, so an
 * ordinary conflict reads a few thousand units even in a long document; an
 * edit across a whole document of about 80 pages that rejects every
 * suggestion in it reads about 1.6 million. The budget admits that about
 * twice over and bounds what the rules read. Each round also copies,
 * rebuilds and checks the whole document a fixed number of times; the round
 * limit bounds that work, not the budget.
 */
export const NORMALIZATION_UNIT_BUDGET = 3_000_000

const NORMALIZATION_ORIGIN = 'inqtrix-edit-merge-normalization'

export type SuggestionOutcome = 'accepted' | 'rejected' | 'superseded'

/** A suggestion this room saw leave the document, with how it left. */
export type KnownSuggestion = SuggestionDescriptor & { outcome: SuggestionOutcome }

export type NormalizationRule =
  | 'altered_suggestion'
  | 'empty_container'
  | 'excluded_mark'
  | 'inherited_mark'
  | 'invalid_composition'
  | 'redundant_format'
  | 'resurrected_suggestion'

export type NormalizationDeclineReason =
  /** The unit budget was spent. */
  | 'budget'
  /** A record in another person's name the room does not know, or changed
   * metadata of an existing one. */
  | 'forged_record'
  /** No rule applies to what the checks refused. */
  | 'no_rule'
  /** Resolving a suggestion needs more than removing marks, text or
   * structure data: a format suggestion whose formatting would have to be
   * reverted, an accepted structure suggestion, proposed characters whose
   * identity the writer's binding exchanged with typed ones. */
  | 'unsupported_suggestion'
  /** The bytes did not settle into one transaction within the rounds. */
  | 'unsettled'

export type EditMergeNormalizationInput = {
  actorUserId: string
  /** The canonical JSON of the room's state before the update. */
  before: JSONContent
  documentLimitBytes: number
  /** Updates of the follow-up transactions Yjs ran after applying `update`
   * to the room's state (its formatting cleanup). */
  followUps: readonly Uint8Array[]
  known: (suggestionId: string) => KnownSuggestion | undefined
  /** The room's state with `update` applied, as Yjs applied it. */
  merged: Y.Doc
  room: Y.Doc
  /** The room's encoded state and its hash, as validated. */
  roomState: Uint8Array
  roomStateHash: string
  unitBudget?: number
  update: Uint8Array
}

export type EditMergeNormalization = {
  /** U and N as one update: what the row persists and the room applies. */
  bytes: Uint8Array
  /** Exactly the items applying `bytes` to the room deleted. */
  deletedByUpdate: YjsDeleteSet
  rounds: number
  rules: Partial<Record<NormalizationRule, number>>
  /** The room's state with `bytes` applied; the caller owns it. */
  state: Y.Doc
  unitsRead: number
}

export type EditMergeNormalizationResult =
  | { declined: NormalizationDeclineReason; suggestionId: string | null; unitsRead: number }
  | { declined: null; normalization: EditMergeNormalization }

class NormalizationDeclined extends Error {
  constructor(
    readonly reason: NormalizationDeclineReason,
    readonly suggestionId?: string,
  ) {
    super(reason)
    this.name = 'NormalizationDeclined'
  }
}

class UnitBudget {
  used = 0

  constructor(private readonly limit: number) {}

  spend(units: number): void {
    this.used += units
    if (this.used > this.limit) throw new NormalizationDeclined('budget')
  }
}

type Attribute = { setter: Y.Item | null; value: unknown }

type Context = {
  actorUserId: string
  before: JSONContent
  budget: UnitBudget
  clientId: number
  documentLimitBytes: number
  known: (suggestionId: string) => KnownSuggestion | undefined
  room: Y.Doc
  roomVector: Map<number, number>
  /** Ids of every suggestion record the room holds. */
  roomRecordIds: () => ReadonlySet<string>
  /** The room's suggestion units: per unit key, the suggestions whose marks
   * it carries, with the mark kind. */
  roomUnits: () => ReadonlyMap<string, ReadonlyArray<{ kind: SuggestionMarkKind; suggestionId: string }>>
  rules: Partial<Record<NormalizationRule, number>>
}

export function normalizeEditMerge(input: EditMergeNormalizationInput): EditMergeNormalizationResult {
  const budget = new UnitBudget(input.unitBudget ?? NORMALIZATION_UNIT_BUDGET)
  const roomVector = Y.decodeStateVector(Y.encodeStateVector(input.room))
  const taken = new Set<number>([
    ...Y.decodeStateVector(Y.encodeStateVector(input.merged)).keys(),
    input.room.clientID,
    input.merged.clientID,
  ])
  let roomRecordIds: ReadonlySet<string> | null = null
  let roomUnits: Map<string, Array<{ kind: SuggestionMarkKind; suggestionId: string }>> | null = null
  const context: Context = {
    actorUserId: input.actorUserId,
    before: input.before,
    budget,
    clientId: normalizationClientId(`${input.roomStateHash}:${hashBytes(input.update)}`, taken),
    documentLimitBytes: input.documentLimitBytes,
    known: input.known,
    room: input.room,
    roomRecordIds: () => {
      roomRecordIds ??= new Set(collectSuggestionRecords(input.before).keys())
      return roomRecordIds
    },
    roomUnits: () => {
      roomUnits ??= suggestionUnitsOf(input.room, budget)
      return roomUnits
    },
    roomVector,
    rules: {},
  }
  const parts: Uint8Array[] = [input.update, ...input.followUps]
  let state: Y.Doc = input.merged
  let settled: { deletedByUpdate: YjsDeleteSet; document: Y.Doc } | null = null
  let rounds = 0
  try {
    while (rounds < MAX_NORMALIZATION_ROUNDS) {
      const planned = planRound(state, context)
      if (planned.length === 0) break
      rounds += 1
      parts.push(...planned)
      const next = settle(input.roomState, parts)
      if (state !== input.merged) state.destroy()
      state = next.document
      settled = next
    }
  } catch (error) {
    if (state !== input.merged) state.destroy()
    if (error instanceof NormalizationDeclined) {
      return { declined: error.reason, suggestionId: error.suggestionId ?? null, unitsRead: budget.used }
    }
    throw error
  }
  if (!settled) return { declined: 'no_rule', suggestionId: null, unitsRead: budget.used }
  return {
    declined: null,
    normalization: {
      bytes: Y.mergeUpdates(parts),
      deletedByUpdate: settled.deletedByUpdate,
      rounds,
      rules: context.rules,
      state: settled.document,
      unitsRead: budget.used,
    },
  }
}

/** A Yjs client id for the normalization's own items, derived from the room
 * state and the update: the same inputs give the same bytes. It never names
 * a client the state already holds. */
export function normalizationClientId(seed: string, taken: ReadonlySet<number>): number {
  for (let attempt = 0; ; attempt += 1) {
    const id = createHash('sha256').update(`${seed}:${attempt}`).digest().readUInt32BE(0)
    if (id !== 0 && !taken.has(id)) return id
  }
}

/**
 * Applies the room's state and then the parts as ONE update, exactly as the
 * room and every replay apply them. The formatting cleanup Yjs runs after
 * such an update is a transaction of its own; it is appended to the parts
 * and the rebuild repeated, so the bytes finally apply as one transaction.
 */
function settle(
  roomState: Uint8Array,
  parts: Uint8Array[],
): { deletedByUpdate: YjsDeleteSet; document: Y.Doc } {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    const document = new Y.Doc()
    Y.applyUpdate(document, roomState)
    const followUps: Uint8Array[] = []
    const deletions: YjsDeleteSet[] = []
    let first = true
    const capture = (update: Uint8Array): void => {
      if (first) {
        first = false
        return
      }
      followUps.push(update)
    }
    const observe = (transaction: Y.Transaction): void => {
      deletions.push(transaction.deleteSet)
    }
    document.on('update', capture)
    document.on('afterTransaction', observe)
    try {
      Y.applyUpdate(document, Y.mergeUpdates(parts))
    } finally {
      document.off('update', capture)
      document.off('afterTransaction', observe)
    }
    if (followUps.length === 0) {
      return { deletedByUpdate: Y.mergeDeleteSets(deletions), document }
    }
    document.destroy()
    parts.push(...followUps)
  }
  throw new NormalizationDeclined('unsettled')
}

/** One round: every rule on a copy of `state`, written by the
 * normalization's own client. Returns the updates it wrote. */
function planRound(state: Y.Doc, context: Context): Uint8Array[] {
  const work = new Y.Doc()
  work.clientID = context.clientId
  Y.applyUpdate(work, Y.encodeStateAsUpdate(state))
  const written: Uint8Array[] = []
  const capture = (update: Uint8Array): void => {
    written.push(update)
  }
  work.on('update', capture)
  try {
    work.transact((transaction) => {
      // What the update changed is confined to the texts it wrote into or
      // deleted from; the room held no side effect anywhere else.
      const touched = touchedTexts(work, context)
      stripInheritedMarks(touched, context)
      yieldExcludedMarks(touched, context)
      resolveResurrectedSuggestions(work, touched, context)
      fillEmptyContainers(work, context)
      removeRedundantFormatItems(touched, transaction, context)
    }, NORMALIZATION_ORIGIN)
    // The suggestion rules judge a settled, valid state; formatting that
    // only the cleanup above would remove makes a state non-canonical.
    const validated = validOrNull(work, context.documentLimitBytes)
    let review: ReturnType<typeof reviewEditOfSuggestions> | null = null
    if (validated) {
      try {
        review = reviewEditOfSuggestions(context.before, validated, context.actorUserId, {
          afterDocument: work,
          beforeDocument: context.room,
        })
      } catch (error) {
        if (!(error instanceof CollaborationError)) throw error
        // An invalid suggestion composition is read as an invalid schema;
        // anything else the review refuses no rule resolves.
        if (error.reason !== 'invalid_schema') throw new NormalizationDeclined('unsupported_suggestion')
      }
    }
    if (!review) {
      work.transact(() => {
        rejectInvalidCompositions(work, context)
      }, NORMALIZATION_ORIGIN)
    } else {
      if (review.writtenInOthersName !== null) {
        throw new NormalizationDeclined('forged_record', review.writtenInOthersName)
      }
      const altered = review.altered
      if (altered.length > 0) {
        work.transact(() => {
          resolveInPlace(work, new Map(altered.map((id) => [id, 'reject'])), context)
        }, NORMALIZATION_ORIGIN)
        count(context, 'altered_suggestion', altered.length)
      }
    }
  } finally {
    work.off('update', capture)
    work.destroy()
  }
  return written
}

function validOrNull(document: Y.Doc, documentLimitBytes: number): JSONContent | null {
  try {
    return validateDocument(document, documentLimitBytes).canonicalJson
  } catch (error) {
    if (error instanceof CollaborationError && error.reason === 'invalid_schema') return null
    throw new NormalizationDeclined('unsupported_suggestion')
  }
}

function count(context: Context, rule: NormalizationRule, amount = 1): void {
  context.rules[rule] = (context.rules[rule] ?? 0) + amount
}

/** Whether a unit (a character or format item, by id) was written after the
 * room's state: by the update, or by the normalization itself. */
function writtenAfterRoom(context: Context, client: number, clock: number): boolean {
  return clock >= (context.roomVector.get(client) ?? 0)
}

/** Whether an item was written by the update itself, not by the room and
 * not by the normalization. */
function writtenByUpdate(context: Context, item: Y.Item, offset = 0): boolean {
  return item.id.client !== context.clientId
    && writtenAfterRoom(context, item.id.client, item.id.clock + offset)
}

/**
 * The texts the update, the normalization or the cleanup after them changed
 * since the room's state: a text holding an item written after it, or an
 * item the room held alive that is deleted now. Read item by item, not
 * character by character. Yjs squashes a client's consecutive characters
 * into one item, so an item that starts in the room's state can end in the
 * update: its last clock decides.
 */
function touchedTexts(work: Y.Doc, context: Context): Y.XmlText[] {
  return xmlTexts(work).filter((text) => {
    let read = 0
    try {
      for (let item = text._start; item !== null; item = item.right) {
        read += 1
        if (writtenAfterRoom(context, item.id.client, item.id.clock + item.length - 1)) return true
        if (item.deleted && aliveInRoom(context, item)) return true
      }
      return false
    } finally {
      context.budget.spend(read)
    }
  })
}

/** Whether any clock of an item was alive in the room's state. */
function aliveInRoom(context: Context, item: Y.Item): boolean {
  const end = Math.min(item.id.clock + item.length, context.roomVector.get(item.id.client) ?? 0)
  let clock = item.id.clock
  while (clock < end) {
    const struct = Y.getItem(context.room.store, Y.createID(item.id.client, clock))
    if (!(struct instanceof Y.Item)) return false
    if (!struct.deleted) return true
    clock = struct.id.clock + struct.length
  }
  return false
}

/** Whether a text holds a live suggestion format item, read item by item. */
function carriesSuggestionFormat(text: Y.XmlText, budget: UnitBudget): boolean {
  let read = 0
  try {
    for (let item = text._start; item !== null; item = item.right) {
      read += 1
      if (
        !item.deleted
        && item.content instanceof Y.ContentFormat
        && suggestionMarkKind(item.content.key)
      ) return true
    }
    return false
  } finally {
    budget.spend(read)
  }
}

function xmlTexts(document: Y.Doc): Y.XmlText[] {
  const texts: Y.XmlText[] = []
  const visit = (parent: Y.XmlFragment | Y.XmlElement): void => {
    for (const child of parent.toArray()) {
      if (child instanceof Y.XmlText) texts.push(child)
      else if (child instanceof Y.XmlElement) visit(child)
    }
  }
  visit(document.getXmlFragment(EDITOR_YJS_FRAGMENT))
  return texts
}

function xmlElements(document: Y.Doc): Y.XmlElement[] {
  const elements: Y.XmlElement[] = []
  const visit = (parent: Y.XmlFragment | Y.XmlElement): void => {
    for (const child of parent.toArray()) {
      if (!(child instanceof Y.XmlElement)) continue
      elements.push(child)
      visit(child)
    }
  }
  visit(document.getXmlFragment(EDITOR_YJS_FRAGMENT))
  return elements
}

/** Every character of a text with its index and the formatting in force at
 * it, each value with the format item that set it: the closest live format
 * item of its key to the left, exactly as Yjs reads it. */
function walkUnits(
  text: Y.XmlText,
  budget: UnitBudget,
  visit: (item: Y.Item, offset: number, index: number, attributes: ReadonlyMap<string, Attribute>) => void,
): void {
  const attributes = new Map<string, Attribute>()
  let index = 0
  let read = 0
  for (let item = text._start; item !== null; item = item.right) {
    read += 1
    if (item.deleted) continue
    if (item.content instanceof Y.ContentFormat) {
      const { key, value } = item.content
      if (value === null) attributes.delete(key)
      else attributes.set(key, { setter: item, value })
      continue
    }
    if (!item.countable) continue
    for (let offset = 0; offset < item.length; offset += 1) {
      visit(item, offset, index + offset, attributes)
    }
    read += item.length
    index += item.length
  }
  budget.spend(read)
}

/** The suggestion mark kind a Yjs format key stands for; the binding adds a
 * hash to the key of a mark that may overlap itself. */
function suggestionMarkKind(key: string): SuggestionMarkKind | null {
  const name = /(.*)(--[a-zA-Z0-9+/=]{8})$/.exec(key)?.[1] ?? key
  return SUGGESTION_MARK_NAMES.has(name) ? name as SuggestionMarkKind : null
}

function markName(key: string): string {
  return /(.*)(--[a-zA-Z0-9+/=]{8})$/.exec(key)?.[1] ?? key
}

function suggestionIdOf(value: unknown): string | null {
  if (!value || typeof value !== 'object') return null
  const id = Reflect.get(value, 'suggestionId') ?? Reflect.get(value, 'id')
  return typeof id === 'string' ? id : null
}

function authorOf(value: unknown): string | null {
  if (!value || typeof value !== 'object') return null
  const author = Reflect.get(value, 'authorId')
  return typeof author === 'string' ? author : null
}

function unitKey(client: number, clock: number): string {
  return `${client}:${clock}`
}

function suggestionUnitsOf(
  document: Y.Doc,
  budget: UnitBudget,
): Map<string, Array<{ kind: SuggestionMarkKind; suggestionId: string }>> {
  const units = new Map<string, Array<{ kind: SuggestionMarkKind; suggestionId: string }>>()
  for (const text of xmlTexts(document)) {
    if (!carriesSuggestionFormat(text, budget)) continue
    walkUnits(text, budget, (item, offset, _index, attributes) => {
      for (const [key, attribute] of attributes) {
        const kind = suggestionMarkKind(key)
        const suggestionId = kind ? suggestionIdOf(attribute.value) : null
        if (!kind || !suggestionId) continue
        const unit = unitKey(item.id.client, item.id.clock + offset)
        const entries = units.get(unit)
        if (entries) entries.push({ kind, suggestionId })
        else units.set(unit, [{ kind, suggestionId }])
      }
    })
  }
  return units
}

/** Pending edits to one text: characters to delete and keys to clear, by
 * character index. Applied clears first (they move no index), then the
 * deletions from the right. */
class TextEdits {
  private readonly clears = new Map<string, Set<number>>()
  private readonly deletions = new Set<number>()

  clear(key: string, index: number): void {
    const indexes = this.clears.get(key) ?? new Set<number>()
    indexes.add(index)
    this.clears.set(key, indexes)
  }

  delete(index: number): void {
    this.deletions.add(index)
  }

  get size(): number {
    let size = this.deletions.size
    for (const indexes of this.clears.values()) size += indexes.size
    return size
  }

  apply(text: Y.XmlText): number {
    let runs = 0
    for (const [key, indexes] of [...this.clears].sort(([left], [right]) => left.localeCompare(right))) {
      const kept = [...indexes].filter((index) => !this.deletions.has(index))
      for (const [start, length] of ranges(kept).reverse()) {
        text.format(start, length, { [key]: null })
        runs += 1
      }
    }
    for (const [start, length] of ranges([...this.deletions]).reverse()) {
      text.delete(start, length)
      runs += 1
    }
    return runs
  }
}

function ranges(indexes: number[]): Array<[number, number]> {
  const sorted = [...new Set(indexes)].sort((left, right) => left - right)
  const result: Array<[number, number]> = []
  for (const index of sorted) {
    const last = result.at(-1)
    if (last && last[0] + last[1] === index) last[1] += 1
    else result.push([index, 1])
  }
  return result
}

/** `inherited_mark`: a character the update created never carries another
 * person's suggestion mark that formatting the update did not write gave it. */
function stripInheritedMarks(texts: readonly Y.XmlText[], context: Context): void {
  let runs = 0
  for (const text of texts) {
    const edits = new TextEdits()
    walkUnits(text, context.budget, (item, offset, index, attributes) => {
      if (!writtenByUpdate(context, item, offset)) return
      for (const [key, attribute] of attributes) {
        if (!suggestionMarkKind(key)) continue
        if (attribute.setter && writtenByUpdate(context, attribute.setter)) continue
        if (authorOf(attribute.value) === context.actorUserId) continue
        edits.clear(key, index)
      }
    })
    if (edits.size > 0) runs += edits.apply(text)
  }
  if (runs > 0) count(context, 'inherited_mark', runs)
}

/** `excluded_mark`: where two marks that exclude each other meet on one
 * character and exactly one was set by the update, the update's mark yields. */
function yieldExcludedMarks(texts: readonly Y.XmlText[], context: Context): void {
  let runs = 0
  for (const text of texts) {
    const edits = new TextEdits()
    walkUnits(text, context.budget, (_item, _offset, index, attributes) => {
      if (attributes.size < 2) return
      const keys = [...attributes.keys()].sort()
      for (const left of keys) {
        for (const right of keys) {
          if (left >= right) continue
          const leftType = schema.marks[markName(left)]
          const rightType = schema.marks[markName(right)]
          if (!leftType || !rightType || leftType === rightType) continue
          if (!leftType.excludes(rightType) && !rightType.excludes(leftType)) continue
          const leftSetter = attributes.get(left)?.setter ?? null
          const rightSetter = attributes.get(right)?.setter ?? null
          const leftByUpdate = leftSetter !== null && writtenByUpdate(context, leftSetter)
          const rightByUpdate = rightSetter !== null && writtenByUpdate(context, rightSetter)
          if (leftByUpdate === rightByUpdate) continue
          // Only formatting yields. A suggestion the update proposes is
          // judged as proposed, never trimmed to fit.
          const yielding = leftByUpdate ? left : right
          if (suggestionMarkKind(yielding)) continue
          edits.clear(yielding, index)
        }
      }
    })
    if (edits.size > 0) runs += edits.apply(text)
  }
  if (runs > 0) count(context, 'excluded_mark', runs)
}

type Resolution = 'accept' | 'reject'

/**
 * Removes suggestions from the document in place, the way a decision does
 * as far as marks, text and structure data go:
 *
 * - reject: proposed text is deleted, deletion marks and structure data are
 *   removed. Every character the room held under the suggestion's insertion
 *   goes, also where the update dropped its mark and kept the text; the
 *   rejection would otherwise keep what it rejects.
 * - accept: proposed text keeps its characters without the mark, text
 *   proposed for deletion is deleted.
 *
 * Only characters the suggestion stood on in the room, or that were written
 * after the room's state (its copies), are deleted. Where the mark spread to
 * other text the room held, only the mark is removed from it. A format
 * suggestion, whose resolution would revert formatting, and an accepted
 * structure suggestion are left to a decision: the update is refused.
 */
function resolveInPlace(
  work: Y.Doc,
  resolutions: ReadonlyMap<string, Resolution>,
  context: Context,
): number {
  const roomUnits = context.roomUnits()
  // Characters the room held under a rejected insertion that no longer carry
  // it. Deleted only where nothing written after the room carries the
  // suggestion: a copy of it elsewhere means the writer's binding may have
  // exchanged the identity of proposed and typed characters, and deleting
  // the room's character would delete typed text.
  const copied = new Set<string>()
  const unmarked: Array<{ index: number; text: Y.XmlText; suggestionId: string }> = []
  const plans: Array<{ edits: TextEdits; text: Y.XmlText }> = []
  for (const text of textsHolding(work, resolutions, roomUnits, context)) {
    const edits = new TextEdits()
    walkUnits(text, context.budget, (item, offset, index, attributes) => {
      const unit = unitKey(item.id.client, item.id.clock + offset)
      const createdAfterRoom = writtenAfterRoom(context, item.id.client, item.id.clock + offset)
      const roomEntries = roomUnits.get(unit) ?? []
      let remove = false
      const carried = new Set<string>()
      const clears: string[] = []
      for (const [key, attribute] of attributes) {
        const kind = suggestionMarkKind(key)
        const suggestionId = kind ? suggestionIdOf(attribute.value) : null
        const resolution = suggestionId ? resolutions.get(suggestionId) : undefined
        if (!kind || !suggestionId || !resolution) continue
        if (kind === 'modification') throw new NormalizationDeclined('unsupported_suggestion', suggestionId)
        carried.add(suggestionId)
        if (createdAfterRoom) copied.add(suggestionId)
        const ownUnit = createdAfterRoom || roomEntries.some((entry) => entry.suggestionId === suggestionId)
        const removesText = (kind === 'insertion') === (resolution === 'reject')
        if (ownUnit && removesText) remove = true
        else clears.push(key)
      }
      for (const entry of roomEntries) {
        const resolution = resolutions.get(entry.suggestionId)
        if (!resolution || carried.has(entry.suggestionId)) continue
        if (entry.kind === 'modification') {
          throw new NormalizationDeclined('unsupported_suggestion', entry.suggestionId)
        }
        if (resolution === 'reject' && entry.kind === 'insertion') {
          unmarked.push({ index, suggestionId: entry.suggestionId, text })
        }
      }
      if (remove) edits.delete(index)
      else for (const key of clears) edits.clear(key, index)
    })
    plans.push({ edits, text })
  }
  for (const entry of unmarked) {
    if (copied.has(entry.suggestionId)) {
      throw new NormalizationDeclined('unsupported_suggestion', entry.suggestionId)
    }
    plans.find((plan) => plan.text === entry.text)?.edits.delete(entry.index)
  }
  let runs = 0
  for (const plan of plans) {
    if (plan.edits.size > 0) runs += plan.edits.apply(plan.text)
  }
  for (const element of xmlElements(work)) {
    const data = element.getAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR) as unknown
    if (!isStructureSuggestionData(data)) continue
    const resolution = resolutions.get(data.suggestionId)
    if (!resolution) continue
    if (resolution === 'accept') throw new NormalizationDeclined('unsupported_suggestion', data.suggestionId)
    element.removeAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR)
    runs += 1
  }
  context.budget.spend(runs)
  return runs
}

/**
 * The texts a resolution has to read: those with a live format item of one
 * of the suggestions, and those holding characters the room had under one
 * of them. Found item by item and through the room's units, so the texts
 * without any of them are never walked character by character.
 */
function textsHolding(
  work: Y.Doc,
  resolutions: ReadonlyMap<string, Resolution>,
  roomUnits: ReadonlyMap<string, ReadonlyArray<{ kind: SuggestionMarkKind; suggestionId: string }>>,
  context: Context,
): Y.XmlText[] {
  const selected = new Set<Y.XmlText>()
  for (const [unit, entries] of roomUnits) {
    if (!entries.some((entry) => resolutions.has(entry.suggestionId))) continue
    const [client, clock] = unit.split(':').map(Number)
    const item = Y.getItem(work.store, Y.createID(client!, clock!))
    if (item instanceof Y.Item && item.parent instanceof Y.XmlText) selected.add(item.parent)
  }
  context.budget.spend(roomUnits.size)
  return xmlTexts(work).filter((text) => {
    if (selected.has(text)) return true
    let read = 0
    try {
      for (let item = text._start; item !== null; item = item.right) {
        read += 1
        if (
          !item.deleted
          && item.content instanceof Y.ContentFormat
          && suggestionMarkKind(item.content.key)
          && resolutions.has(suggestionIdOf(item.content.value) ?? '')
        ) return true
      }
      return false
    } finally {
      context.budget.spend(read)
    }
  })
}

type SeenRecord = {
  authorId: string | null
  createdAt: unknown
  kind: unknown
  patchId: unknown
}

/** `resurrected_suggestion`: a suggestion the room no longer holds, brought
 * back by the update, takes its recorded outcome, whoever proposed it. */
function resolveResurrectedSuggestions(
  work: Y.Doc,
  touched: readonly Y.XmlText[],
  context: Context,
): void {
  const roomIds = context.roomRecordIds()
  const seen = new Map<string, SeenRecord[]>()
  const note = (suggestionId: string, value: unknown): void => {
    if (roomIds.has(suggestionId) || !value || typeof value !== 'object') return
    const record: SeenRecord = {
      authorId: authorOf(value),
      createdAt: Reflect.get(value, 'createdAt'),
      kind: Reflect.get(value, 'kind'),
      patchId: Reflect.get(value, 'patchId'),
    }
    const list = seen.get(suggestionId)
    if (list) list.push(record)
    else seen.set(suggestionId, [record])
  }
  // A suggestion the room does not hold can only be in force where the
  // update wrote: in a text it touched, or on a block.
  for (const text of touched) {
    walkUnits(text, context.budget, (_item, _offset, _index, attributes) => {
      for (const [key, attribute] of attributes) {
        const suggestionId = suggestionMarkKind(key) ? suggestionIdOf(attribute.value) : null
        if (suggestionId) note(suggestionId, attribute.value)
      }
    })
  }
  const elements = xmlElements(work)
  context.budget.spend(elements.length)
  for (const element of elements) {
    const data = element.getAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR) as unknown
    if (isStructureSuggestionData(data)) note(data.suggestionId, data)
  }
  const resolutions = new Map<string, Resolution>()
  for (const [suggestionId, records] of [...seen].sort(([left], [right]) => left.localeCompare(right))) {
    const known = context.known(suggestionId)
    if (!known) {
      // A suggestion new to the room is a new proposal: the actor's own is
      // the policy's to judge, one in another person's name is forged.
      if (records.every((record) => record.authorId === context.actorUserId)) continue
      throw new NormalizationDeclined('forged_record', suggestionId)
    }
    // A suggestion the room saw leave keeps its outcome, whoever wrote it:
    // an undo or a stale view never proposes it again.
    const matches = records.every((record) => (
      record.authorId === known.authorId
      && record.createdAt === known.createdAt
      && record.patchId === known.patchId
      && record.kind === known.kind
    ))
    if (!matches) throw new NormalizationDeclined('forged_record', suggestionId)
    resolutions.set(suggestionId, known.outcome === 'accepted' ? 'accept' : 'reject')
  }
  if (resolutions.size === 0) return
  resolveInPlace(work, resolutions, context)
  count(context, 'resurrected_suggestion', resolutions.size)
}

/** `invalid_composition`: a suggestion the merge left in a form the schema
 * refuses is removed as a whole, whoever proposed it. The room held only
 * valid suggestions, so the update caused it. */
function rejectInvalidCompositions(work: Y.Doc, context: Context): void {
  const rejected = new Set<string>()
  for (let attempt = 0; attempt < 8; attempt += 1) {
    let suggestionId: string | null = null
    try {
      const json = yXmlFragmentToProsemirrorJSON(work.getXmlFragment(EDITOR_YJS_FRAGMENT))
      const document = schema.nodeFromJSON(json)
      context.budget.spend(document.content.size)
      suggestionDescriptors(document)
    } catch (error) {
      if (error instanceof NormalizationDeclined) throw error
      suggestionId = error instanceof InvalidSuggestionMetadataError ? error.suggestionId : null
    }
    if (!suggestionId || rejected.has(suggestionId)) break
    rejected.add(suggestionId)
    resolveInPlace(work, new Map([[suggestionId, 'reject']]), context)
  }
  if (rejected.size > 0) count(context, 'invalid_composition', rejected.size)
}

/** `empty_container`: a block the room held that must have content and that
 * the merge left empty gets the schema's minimal content. A textblock of that
 * content holds its empty text node, as every writer of a new textblock
 * writes it. */
function fillEmptyContainers(work: Y.Doc, context: Context): void {
  let filled = 0
  const since = Y.getState(work.store, work.clientID)
  const elements = xmlElements(work)
  context.budget.spend(elements.length)
  for (const element of elements) {
    const type = schema.nodes[element.nodeName]
    if (!type || type.isTextblock || type.isLeaf || element.length > 0) continue
    if (type.validContent(Fragment.empty)) continue
    const item = element._item
    if (!item || writtenAfterRoom(context, item.id.client, item.id.clock)) continue
    const content = type.createAndFill()
    if (!content) continue
    const children: Y.XmlElement[] = []
    content.forEach((child) => {
      children.push(yElementFor(child))
    })
    element.insert(0, children)
    filled += 1
  }
  completeTextblocksCreatedSince(work, since)
  if (filled > 0) count(context, 'empty_container', filled)
}

/** The Yjs element the canonical transformer writes for an empty node, before
 * its textblocks are completed (see fillEmptyContainers). */
function yElementFor(node: ProseMirrorNode): Y.XmlElement {
  const element = new Y.XmlElement(node.type.name)
  for (const [key, value] of Object.entries(node.attrs).sort(([left], [right]) => left.localeCompare(right))) {
    if (value !== null && value !== undefined) element.setAttribute(key, value as string)
  }
  const children: Y.XmlElement[] = []
  node.forEach((child) => {
    children.push(yElementFor(child))
  })
  if (children.length > 0) element.insert(0, children)
  return element
}

/**
 * `redundant_format`: in every gap between two characters, a format item a
 * later item of the same key overwrites, or one that repeats the value in
 * force, changes nothing and only splits a run. It is deleted, the per-gap
 * cleanup Yjs itself means to run. An item the update wrote is deleted only
 * when its value is null, shown in the text, the mark's default, known in
 * the room, or a well-formed suggestion value: a value the update wrote that
 * no reader sees is never removed silently, and the update stays refused.
 */
function removeRedundantFormatItems(
  texts: readonly Y.XmlText[],
  transaction: Y.Transaction,
  context: Context,
): void {
  let known: Map<string, Set<string>> | null = null
  const roomValues = (): Map<string, Set<string>> => {
    known ??= visibleValues(xmlTexts(context.room), context.budget)
    return known
  }
  let removed = 0
  for (const text of texts) {
    const visible = visibleValues([text], context.budget)
    const inForce = new Map<string, string>()
    let gap: Y.Item[] = []
    const settleGap = (followedByContent: boolean): void => {
      if (followedByContent) {
        const byKey = new Map<string, Y.Item[]>()
        for (const item of gap) {
          const key = (item.content as Y.ContentFormat).key
          byKey.set(key, [...(byKey.get(key) ?? []), item])
        }
        for (const [key, items] of byKey) {
          const last = items.at(-1)!
          const lastValue = (last.content as Y.ContentFormat).value
          const noop = stableJson(lastValue ?? null) === (inForce.get(key) ?? 'null')
          const removable = noop ? items : items.slice(0, -1)
          const guarded = removable.some((item) => {
            const value = (item.content as Y.ContentFormat).value
            return writtenByUpdate(context, item)
              && value !== null
              && !visible.get(key)?.has(stableJson(value))
              && stableJson(value) !== defaultMarkValue(key)
              && !roomValues().get(key)?.has(stableJson(value))
              && !(suggestionMarkKind(key) && isWellFormedSuggestionValue(value))
          })
          if (guarded) continue
          for (const item of removable) {
            item.delete(transaction)
            removed += 1
          }
        }
      }
      for (const item of gap) {
        if (item.deleted) continue
        const { key, value } = item.content as Y.ContentFormat
        if (value === null) inForce.delete(key)
        else inForce.set(key, stableJson(value))
      }
      gap = []
    }
    let read = 0
    for (let item = text._start; item !== null; item = item.right) {
      read += 1
      if (item.deleted) continue
      if (item.content instanceof Y.ContentFormat) {
        gap.push(item)
        continue
      }
      if (!item.countable) continue
      settleGap(true)
    }
    settleGap(false)
    context.budget.spend(read)
  }
  if (removed > 0) count(context, 'redundant_format', removed)
}

function visibleValues(texts: readonly Y.XmlText[], budget: UnitBudget): Map<string, Set<string>> {
  const values = new Map<string, Set<string>>()
  for (const text of texts) {
    const delta = text.toDelta() as Array<{ attributes?: Record<string, unknown> }>
    budget.spend(delta.length)
    for (const run of delta) {
      for (const [key, value] of Object.entries(run.attributes ?? {})) {
        const set = values.get(key) ?? new Set<string>()
        set.add(stableJson(value))
        values.set(key, set)
      }
    }
  }
  return values
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
const SUGGESTION_VALUE_KEYS = ['authorId', 'createdAt', 'id', 'kind', 'patchId', 'suggestionId']
const SUGGESTION_VALUE_KINDS = new Set(['deletion', 'format', 'insertion', 'replacement'])

/** A suggestion mark value of exactly the schema's shape: ids, author, time
 * and kind, nothing free-form. Removing a redundant item with such a value
 * hides nothing a reader could miss. */
function isWellFormedSuggestionValue(value: unknown): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const keys = Object.keys(value).sort()
  if (
    keys.length !== SUGGESTION_VALUE_KEYS.length
    || keys.some((key, index) => key !== SUGGESTION_VALUE_KEYS[index])
  ) return false
  const record = value as Record<string, unknown>
  return typeof record.id === 'string' && UUID.test(record.id)
    && record.suggestionId === record.id
    && typeof record.patchId === 'string' && UUID.test(record.patchId)
    && typeof record.authorId === 'string' && UUID.test(record.authorId)
    && Number.isSafeInteger(record.createdAt) && Number(record.createdAt) >= 0
    && typeof record.kind === 'string' && SUGGESTION_VALUE_KINDS.has(record.kind)
}

/** The value a mark without chosen attributes is written with (`{}` for bold,
 * italic, ...): it carries nothing a reader could miss. */
function defaultMarkValue(key: string): string | null {
  const type = schema.marks[key]
  if (!type) return null
  try {
    return stableJson(type.create().attrs)
  } catch {
    return null
  }
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`
  if (value && typeof value === 'object') {
    return `{${Object.entries(value)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`)
      .join(',')}}`
  }
  return JSON.stringify(value)
}
