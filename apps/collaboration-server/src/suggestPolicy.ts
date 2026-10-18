import { getSchema, type JSONContent } from '@tiptap/core'
import type { Mark, Node as ProseMirrorNode } from '@tiptap/pm/model'
import {
  absolutePositionToRelativePosition,
  initProseMirrorDoc,
  relativePositionToAbsolutePosition,
} from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  SUGGESTION_MARK_NAMES,
  createEditorSchemaExtensions,
  inapplicableStructureSuggestionIds,
  isProposableBlock,
  isStructureSuggestionData,
  projectOriginalDocument,
  structureSuggestionSignature,
  suggestionDescriptors,
  type CollaborationAccess,
  type CollaborationChangeKind,
  type SuggestionDescriptor,
  type SuggestionKind,
  type SuggestionMarkKind,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import type { SuggestionPatchState } from './contracts'
import { CloseCodes, CollaborationError } from './errors'

export type SuggestionOccurrence = {
  markKind: SuggestionMarkKind | 'structure'
  node: unknown
  position: SuggestionPosition
}

export type SuggestionPosition = {
  from: number
  parentPath: StructuralPathEntry[]
  to: number
}

type StructuralPathEntry = {
  attrs: unknown
  index: number
  type: string
}

/** Maps an occurrence of a suggestion from the document before an update to
 * the document after it. A structure occurrence is a whole block and follows
 * that block's identity; a mark occurrence follows its own characters. */
export type SuggestionPositionMapper = (
  position: SuggestionPosition,
  occurrence?: 'block' | 'text',
) => SuggestionPosition | null

/** A Yjs delete set, the shape a transaction reports its deletions in. */
export type YjsDeleteSet = ReturnType<typeof Y.createDeleteSet>

export type SuggestionDocumentPair = {
  afterDocument: Y.Doc
  beforeDocument: Y.Doc
  /** Exactly the items the update deleted: the delete sets of the
   * transactions that applied it to a copy of `beforeDocument`. Required to
   * judge a suggest-only update; without it such an update is refused. */
  deletedByUpdate?: YjsDeleteSet
}

type SuggestionXmlParent = Y.XmlElement | Y.XmlFragment
type SuggestionXmlType = Y.XmlElement | Y.XmlText

export type SuggestionRecord = {
  authorId: string
  createdAt: number
  kind: SuggestionKind
  occurrences: SuggestionOccurrence[]
  patchId: string
  suggestionId: string
}

export type SuggestionPolicyResult = {
  changeKind: CollaborationChangeKind
  patches: SuggestionPatchState[]
  /** The suggestions an edit-lease update removed completely, another
   * person's or the actor's own: every character each one proposed is
   * deleted. The store records each as removed by the update's actor, and
   * its patch in `patches` has lost it. Empty for every other access. */
  rejectedSuggestionIds: string[]
  suggestions: SuggestionDescriptor[]
  suggestionIds: string[]
}

/** Whether a suggestion position mapper has to be built for this pair.
 *
 * Exported so the decision is pinned by a contract instead of being an
 * implicit side effect of the validator's control flow. */
export function needsPositionMapper(before: JSONContent, after: JSONContent): boolean {
  return collectSuggestionRecords(before).size > 0
    || collectSuggestionRecords(after).size > 0
}

export function validateSuggestionUpdate(
  before: JSONContent,
  after: JSONContent,
  access: CollaborationAccess,
  actorUserId: string,
  documents?: SuggestionDocumentPair,
): SuggestionPolicyResult {
  if (access === 'view') throw policyViolation('access_revoked', 403)

  const beforeRecords = collectSuggestionRecords(before)
  const afterRecords = collectSuggestionRecords(after)
  // Only build the mapping when a suggestion can actually be compared.
  // Both consumers are no-ops without records: `changedSuggestionIds`
  // iterates the union of both key sets, and the foreign-record loop
  // below iterates `beforeRecords`. Getting this wrong fails LOUD — the
  // loop rejects the update on a missing mapping rather than accepting
  // something unverified.
  const positionMapping = documents && (beforeRecords.size > 0 || afterRecords.size > 0)
    ? createYjsSuggestionPositionMapping(before, after, documents, access)
    : null
  validatePatchGroups(beforeRecords)
  validatePatchGroups(afterRecords)
  validatePatchMetadataTransitions(beforeRecords, afterRecords)
  const semanticChangedIds = changedSuggestionContentIds(beforeRecords, afterRecords)

  for (const [id, record] of afterRecords) {
    const previous = beforeRecords.get(id)
    if (!previous && record.authorId !== actorUserId) throw policyViolation()
    if (previous && !sameSuggestionMetadata(previous, record)) throw policyViolation()
  }
  const supersededSuggestionIds = new Set<string>()
  const rejectedSuggestionIds = new Set<string>()
  // For an edit lease: the suggestions the update left on their own
  // characters, fewer of them or formatted otherwise. They are no change of
  // the actor's, whoever proposed them.
  const keptSuggestionIds = new Set<string>()
  for (const [id, record] of beforeRecords) {
    const next = afterRecords.get(id)
    if (!next) {
      if (isSupersededSlashInsertion(record, beforeRecords, afterRecords, actorUserId)) {
        supersededSuggestionIds.add(id)
        continue
      }
      // A writer with edit rights, a member or a guest of an edit link, may
      // delete the text of any pending suggestion; a suggestion whose every
      // proposed character is deleted is removed. The store records the
      // removal with the update's actor, in the update's transaction.
      if (access !== 'edit' || !positionMapping?.removedAsRejected(id)) {
        throw policyViolation()
      }
      rejectedSuggestionIds.add(id)
      continue
    }
    if (access === 'edit') {
      // Editing a pending suggestion's text directly may take characters
      // away from it and format it; it never gives it characters or moves
      // it. A suggestion kept that way stays pending in its author's name.
      if (positionMapping && keptOnItsCharacters(id, record, next, positionMapping)) {
        keptSuggestionIds.add(id)
        continue
      }
      // The actor's own suggestion that grew is the actor's proposal.
      if (record.authorId === actorUserId) continue
      throw policyViolation()
    }
    if (record.authorId === actorUserId) continue
    // Under a suggest-only lease another person's suggestion stays exactly
    // as it was: the same content in the same place, on the very characters
    // and block it stood on before.
    if (!keptUnchanged(id, record, next, positionMapping)) throw policyViolation()
  }
  const changedIds = access === 'edit'
    ? [...new Set([...beforeRecords.keys(), ...afterRecords.keys()])]
        .filter((id) => !keptSuggestionIds.has(id))
        .sort()
    : changedSuggestionIds(beforeRecords, afterRecords, positionMapping)
  const ownChangedIds = changedIds.filter((id) => !rejectedSuggestionIds.has(id))
  const changeKind = access === 'suggest' || ownChangedIds.length > 0 ? 'suggestion' : 'direct'

  if (changeKind === 'suggestion') {
    if (!documents) throw policyViolation()
    assertSuggestionYjsTopology(documents, new Set(semanticChangedIds))
    // A suggest-only lease never deletes original content, not even to
    // write it again. An edit lease may change content directly anyway.
    if (access === 'suggest') {
      assertSuggestionDeleteSet(documents, beforeRecords, actorUserId)
    }
    // A suggestion that lands in a block another person deleted or wrote
    // anew at the same time is dropped as the room merges it: the update
    // changes nothing visible, which is all a suggest-only lease may do
    // besides proposing. It is judged after the deletion rule above, so an
    // update that swaps original content for an identical copy stays refused.
    if (
      ownChangedIds.length === 0
      && !(access === 'suggest' && stableJson(before) === stableJson(after))
    ) {
      throw policyViolation()
    }
    if (!isReversibleSuggestionStructure(before, after)) throw policyViolation()
  }
  assertStructureSuggestionsStayApplicable(before, after, afterRecords)

  const patches = patchStatesForChanges(
    beforeRecords,
    afterRecords,
    changedIds,
    supersededSuggestionIds,
  )
  // A patch may lose its last suggestion only by removal: every suggestion
  // it held was removed by this edit-lease update.
  if (patches.some((patch) => (
    patch.activeSuggestionIds.length === 0
    && !everySuggestionOfPatch(beforeRecords, patch.patchId, rejectedSuggestionIds)
  ))) {
    throw policyViolation()
  }

  return {
    changeKind,
    patches,
    rejectedSuggestionIds: [...rejectedSuggestionIds].sort(),
    suggestions: ownChangedIds.map((id) => descriptor(afterRecords.get(id) ?? beforeRecords.get(id))),
    suggestionIds: ownChangedIds,
  }
}

/** Whether a suggestion an update kept stands exactly as it did: the same
 * content in the same place, on the very characters and block it stood on. */
function keptUnchanged(
  suggestionId: string,
  record: SuggestionRecord,
  next: SuggestionRecord,
  positionMapping: SuggestionPositionMapping | null,
): boolean {
  return Boolean(
    positionMapping
    && sameSuggestionRecord(record, next, positionMapping.map, positionMapping.recreated)
    && positionMapping.unitsUnchanged(suggestionId),
  )
}

/**
 * Whether an edit lease's update left a suggestion on its own characters:
 * every character and block that carries it afterwards carried it before,
 * with the same mark and the same attributes, and every one that carried it
 * before still does or is deleted. It may have lost characters, other marks
 * may have come or gone, and text around it may have changed; it gained
 * nothing and moved nowhere, since characters written elsewhere are new Yjs
 * items. A suggestion the binding wrote again in place with its block (a
 * block type change, a list around it, a split in front of it) counts as
 * kept when every character is new, every old one deleted, and the copy
 * stands where the original stood with the same text and marks.
 */
function keptOnItsCharacters(
  suggestionId: string,
  record: SuggestionRecord,
  next: SuggestionRecord,
  positionMapping: SuggestionPositionMapping,
): boolean {
  return positionMapping.unitsKept(suggestionId)
    || (
      positionMapping.unitsWrittenAgain(suggestionId)
      && sameSuggestionRecord(record, next, positionMapping.map, positionMapping.recreated)
    )
}

export type EditSuggestionReview = {
  /** Other people's suggestions the update altered in a way editing may
   * not: kept with characters it gained, moved or changed in its attributes,
   * or removed without the removal editing makes (proposed text left
   * behind). Sorted. */
  altered: string[]
  /** A suggestion the update writes in another person's name, when it does:
   * a new record, or a record or patch whose metadata changed. No rejection
   * can make such an update acceptable. */
  writtenInOthersName: string | null
}

/**
 * Reviews an edit-lease update against the suggestions of other people, by
 * the same rules `validateSuggestionUpdate` applies. An altered suggestion
 * can still be removed under the writer's edit rights, which is what the
 * service does in the writer's own row when nothing else resolves it.
 */
export function reviewEditOfSuggestions(
  before: JSONContent,
  after: JSONContent,
  actorUserId: string,
  documents: SuggestionDocumentPair,
): EditSuggestionReview {
  const beforeRecords = collectSuggestionRecords(before)
  const afterRecords = collectSuggestionRecords(after)
  validatePatchGroups(beforeRecords)
  for (const [id, record] of afterRecords) {
    const previous = beforeRecords.get(id)
    if (
      (!previous && record.authorId !== actorUserId)
      || (previous && !sameSuggestionMetadata(previous, record))
    ) {
      return { altered: [], writtenInOthersName: id }
    }
  }
  // A record the writer added to a patch whose author or time differs from
  // its own writes into another person's patch.
  const beforePatches = groupByPatch(beforeRecords)
  const afterPatches = groupByPatch(afterRecords)
  for (const record of afterRecords.values()) {
    const reference = beforePatches.get(record.patchId)?.[0] ?? afterPatches.get(record.patchId)?.[0]
    if (
      reference
      && (reference.authorId !== record.authorId || reference.createdAt !== record.createdAt)
    ) {
      return { altered: [], writtenInOthersName: record.suggestionId }
    }
  }
  const foreign = [...beforeRecords].filter(([, record]) => record.authorId !== actorUserId)
  if (foreign.length === 0) return { altered: [], writtenInOthersName: null }
  const positionMapping = createYjsSuggestionPositionMapping(before, after, documents, 'edit')
  const altered: string[] = []
  for (const [id, record] of foreign) {
    const next = afterRecords.get(id)
    if (next ? !keptOnItsCharacters(id, record, next, positionMapping) : !positionMapping.removedAsRejected(id)) {
      altered.push(id)
    }
  }
  return { altered: altered.sort(), writtenInOthersName: null }
}

function everySuggestionOfPatch(
  records: ReadonlyMap<string, SuggestionRecord>,
  patchId: string,
  suggestionIds: ReadonlySet<string>,
): boolean {
  const members = groupByPatch(records).get(patchId) ?? []
  return members.length > 0 && members.every((record) => suggestionIds.has(record.suggestionId))
}

/**
 * Selecting a slash-menu structure command legitimately replaces the actor's
 * complete `/query` insertion with one structure suggestion in the same patch.
 * Keep this exception deliberately narrow; every other disappearance still
 * requires an explicit durable accept/reject decision.
 */
function isSupersededSlashInsertion(
  record: SuggestionRecord,
  before: ReadonlyMap<string, SuggestionRecord>,
  after: ReadonlyMap<string, SuggestionRecord>,
  actorUserId: string,
): boolean {
  if (
    record.authorId !== actorUserId
    || record.kind !== 'insertion'
    || record.occurrences.length === 0
  ) return false
  const beforeGroup = groupByPatch(before).get(record.patchId) ?? []
  const afterGroup = groupByPatch(after).get(record.patchId) ?? []
  if (
    beforeGroup.length !== 1
    || beforeGroup[0]?.suggestionId !== record.suggestionId
    || afterGroup.length !== 1
  ) return false
  const replacement = afterGroup[0]
  if (
    !replacement
    || replacement.kind !== 'structure'
    || replacement.authorId !== actorUserId
    || replacement.createdAt !== record.createdAt
  ) return false

  const ordered = [...record.occurrences].sort((
    left,
    right,
  ) => left.position.from - right.position.from)
  let previousTo: number | null = null
  let command = ''
  for (const occurrence of ordered) {
    if (
      occurrence.markKind !== 'insertion'
      || (
        previousTo !== null
        && occurrence.position.from !== previousTo
      )
      || !occurrence.node
      || typeof occurrence.node !== 'object'
    ) return false
    const text = Reflect.get(occurrence.node, 'text')
    if (
      typeof text !== 'string'
      || text.length === 0
      || occurrence.position.to - occurrence.position.from !== text.length
    ) return false
    command += text
    previousTo = occurrence.position.to
  }
  return command.startsWith('/') && command.length <= 96
}

export function deriveSuggestionPatchStates(
  before: JSONContent,
  after: JSONContent,
  affectedSuggestionIds: readonly string[],
): SuggestionPatchState[] {
  const beforeRecords = collectSuggestionRecords(before)
  const afterRecords = collectSuggestionRecords(after)
  validatePatchGroups(beforeRecords)
  validatePatchGroups(afterRecords)
  validatePatchMetadataTransitions(beforeRecords, afterRecords)
  return patchStatesForChanges(beforeRecords, afterRecords, affectedSuggestionIds)
}

const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

export function collectSuggestionRecords(document: JSONContent): Map<string, SuggestionRecord> {
  try {
    return collectProseMirrorSuggestionRecords(schema.nodeFromJSON(document))
  } catch (error) {
    if (error instanceof CollaborationError) throw error
    throw policyViolation('invalid_schema', 409)
  }
}

function collectProseMirrorSuggestionRecords(
  document: ProseMirrorNode,
): Map<string, SuggestionRecord> {
  const records = new Map<string, SuggestionRecord>()
  for (const descriptor of suggestionDescriptors(document)) {
    if (
      !isUuid(descriptor.suggestionId)
      || !isUuid(descriptor.patchId)
      || !isUuid(descriptor.authorId)
      || !Number.isSafeInteger(descriptor.createdAt)
      || descriptor.createdAt < 0
    ) {
      throw policyViolation('invalid_schema', 409)
    }
    records.set(descriptor.suggestionId, {
      ...descriptor,
      occurrences: [],
    })
  }

  document.descendants((node, position) => {
    const suggestionMarks = node.marks.filter((mark) => (
      SUGGESTION_MARK_NAMES.has(mark.type.name)
    ))
    for (const mark of suggestionMarks) {
      const attrs = mark.attrs
      const id = typeof attrs.suggestionId === 'string' ? attrs.suggestionId : null
      const kind = mark.type.name as SuggestionMarkKind
      const record = id ? records.get(id) : null
      if (!record) throw policyViolation('invalid_schema', 409)
      const positionIdentity = suggestionPositionAt(
        document,
        position,
        position + node.nodeSize,
      )
      if (!positionIdentity) throw policyViolation('invalid_schema', 409)
      const occurrence: SuggestionOccurrence = {
        markKind: kind,
        node: suggestionNodeSignature(node, mark),
        position: positionIdentity,
      }
      record.occurrences.push(occurrence)
    }
    const structure = node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR]
    if (structure !== null && structure !== undefined) {
      if (!isStructureSuggestionData(structure)) {
        throw policyViolation('invalid_schema', 409)
      }
      const record = records.get(structure.suggestionId)
      if (!record || record.kind !== 'structure') {
        throw policyViolation('invalid_schema', 409)
      }
      const positionIdentity = suggestionPositionAt(
        document,
        position,
        position + node.nodeSize,
      )
      if (!positionIdentity) throw policyViolation('invalid_schema', 409)
      record.occurrences.push({
        markKind: 'structure',
        node: structureNodeSignature(node),
        position: positionIdentity,
      })
    }
  })
  if ([...records.values()].some((record) => record.occurrences.length === 0)) {
    throw policyViolation('invalid_schema', 409)
  }
  return records
}

export function isReversibleSuggestionStructure(
  before: JSONContent,
  after: JSONContent,
): boolean {
  try {
    const beforeDocument = schema.nodeFromJSON(before)
    const afterDocument = schema.nodeFromJSON(after)
    return (
      projectOriginalDocument(beforeDocument).eq(projectOriginalDocument(afterDocument))
      && sameNonTextAtoms(beforeDocument, afterDocument)
    )
  } catch {
    return false
  }
}

export function hasSameNonTextAtoms(before: JSONContent, after: JSONContent): boolean {
  try {
    return sameNonTextAtoms(
      schema.nodeFromJSON(before),
      schema.nodeFromJSON(after),
    )
  } catch {
    return false
  }
}

function sameNonTextAtoms(before: ProseMirrorNode, after: ProseMirrorNode): boolean {
  return stableJson(nonTextAtoms(before)) === stableJson(nonTextAtoms(after))
}

function nonTextAtoms(document: ProseMirrorNode): unknown[] {
  const atoms: unknown[] = []
  document.descendants((node) => {
    if (!node.isAtom || node.isText) return
    atoms.push({
      attrs: node.attrs,
      marks: node.marks
        .filter((mark) => !SUGGESTION_MARK_NAMES.has(mark.type.name))
        .map((mark) => ({ attrs: mark.attrs, type: mark.type.name })),
      type: node.type.name,
    })
  })
  return atoms
}

function patchStatesForChanges(
  before: ReadonlyMap<string, SuggestionRecord>,
  after: ReadonlyMap<string, SuggestionRecord>,
  affectedSuggestionIds: readonly string[],
  supersededSuggestionIds: ReadonlySet<string> = new Set(),
): SuggestionPatchState[] {
  const beforeGroups = groupByPatch(before)
  const afterGroups = groupByPatch(after)
  const patchIds = new Set<string>()
  for (const suggestionId of affectedSuggestionIds) {
    const previous = before.get(suggestionId)
    const next = after.get(suggestionId)
    if (!previous && !next) throw policyViolation('invalid_schema', 409)
    if (previous) patchIds.add(previous.patchId)
    if (next) patchIds.add(next.patchId)
  }
  return [...patchIds].sort().map((patchId) => {
    const active = afterGroups.get(patchId) ?? []
    const reference = active[0] ?? beforeGroups.get(patchId)?.[0]
    if (!reference) throw policyViolation('invalid_schema', 409)
    return {
      activeSuggestionIds: active.map((record) => record.suggestionId).sort(),
      authorId: reference.authorId,
      createdAt: reference.createdAt,
      kinds: [...new Set(active.map((record) => record.kind))].sort(suggestionKindOrder),
      patchId,
      supersededSuggestionIds: [...supersededSuggestionIds]
        .filter((suggestionId) => before.get(suggestionId)?.patchId === patchId)
        .sort(),
    }
  })
}

function validatePatchGroups(records: ReadonlyMap<string, SuggestionRecord>): void {
  for (const group of groupByPatch(records).values()) {
    const reference = group[0]
    if (!reference) continue
    for (const record of group) {
      if (
        record.authorId !== reference.authorId
        || record.createdAt !== reference.createdAt
      ) {
        throw policyViolation('invalid_schema', 409)
      }
    }
  }
}

function validatePatchMetadataTransitions(
  before: ReadonlyMap<string, SuggestionRecord>,
  after: ReadonlyMap<string, SuggestionRecord>,
): void {
  const beforeGroups = groupByPatch(before)
  const afterGroups = groupByPatch(after)
  for (const [patchId, beforeGroup] of beforeGroups) {
    const afterGroup = afterGroups.get(patchId)
    if (!afterGroup || !beforeGroup[0] || !afterGroup[0]) continue
    if (
      beforeGroup[0].authorId !== afterGroup[0].authorId
      || beforeGroup[0].createdAt !== afterGroup[0].createdAt
    ) {
      throw policyViolation()
    }
  }
}

function groupByPatch(
  records: ReadonlyMap<string, SuggestionRecord>,
): Map<string, SuggestionRecord[]> {
  const groups = new Map<string, SuggestionRecord[]>()
  for (const record of records.values()) {
    const existing = groups.get(record.patchId)
    if (existing) existing.push(record)
    else groups.set(record.patchId, [record])
  }
  for (const group of groups.values()) {
    group.sort((left, right) => left.suggestionId.localeCompare(right.suggestionId))
  }
  return groups
}

function descriptor(record: SuggestionRecord | undefined): SuggestionDescriptor {
  if (!record) throw policyViolation('invalid_schema', 409)
  return {
    authorId: record.authorId,
    createdAt: record.createdAt,
    kind: record.kind,
    patchId: record.patchId,
    suggestionId: record.suggestionId,
  }
}

function isUuid(value: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)
}

function changedSuggestionIds(
  before: ReadonlyMap<string, SuggestionRecord>,
  after: ReadonlyMap<string, SuggestionRecord>,
  positionMapping: SuggestionPositionMapping | null = null,
): string[] {
  const ids = new Set([...before.keys(), ...after.keys()])
  return [...ids]
    .filter((id) => {
      const previous = before.get(id)
      const next = after.get(id)
      return !previous || !next || !sameSuggestionRecord(
        previous,
        next,
        positionMapping?.map,
        positionMapping?.recreated,
      )
    })
    .sort()
}

function changedSuggestionContentIds(
  before: ReadonlyMap<string, SuggestionRecord>,
  after: ReadonlyMap<string, SuggestionRecord>,
): string[] {
  const ids = new Set([...before.keys(), ...after.keys()])
  return [...ids]
    .filter((id) => {
      const previous = before.get(id)
      const next = after.get(id)
      return !previous || !next || !sameSuggestionContent(previous, next)
    })
    .sort()
}

function sameSuggestionContent(left: SuggestionRecord, right: SuggestionRecord): boolean {
  if (
    !sameSuggestionMetadata(left, right)
    || left.occurrences.length !== right.occurrences.length
  ) return false
  const signatures = (record: SuggestionRecord): string[] => record.occurrences
    .map((occurrence) => stableJson({
      markKind: occurrence.markKind,
      node: occurrence.node,
    }))
    .sort()
  return stableJson(signatures(left)) === stableJson(signatures(right))
}

/** Whether a suggestion is unchanged: the same metadata, and every
 * occurrence found again, with the same content, where `positionMapper`
 * puts it. `recreatedMapper` is asked only for a text occurrence the first
 * mapper could not follow. */
export function sameSuggestionRecord(
  left: SuggestionRecord,
  right: SuggestionRecord,
  positionMapper: SuggestionPositionMapper = (position) => position,
  recreatedMapper: SuggestionPositionMapper | null = null,
): boolean {
  if (
    !sameSuggestionMetadata(left, right)
    || left.occurrences.length !== right.occurrences.length
  ) return false

  const unmatched = new Set(right.occurrences.map((_occurrence, index) => index))
  const matchesAt = (
    occurrence: SuggestionOccurrence,
    mappedPosition: SuggestionPosition | null,
  ): number[] => (mappedPosition
    ? [...unmatched].filter((index) => {
        const next = right.occurrences[index]
        return Boolean(
          next
          && occurrence.markKind === next.markKind
          && stableJson(occurrence.node) === stableJson(next.node)
          && sameSuggestionPosition(mappedPosition, next.position)
        )
      })
    : [])
  for (const occurrence of left.occurrences) {
    const kind = occurrence.markKind === 'structure' ? 'block' : 'text'
    let matches = matchesAt(occurrence, positionMapper(occurrence.position, kind))
    if (matches.length !== 1 && recreatedMapper && kind === 'text') {
      matches = matchesAt(occurrence, recreatedMapper(occurrence.position, kind))
    }
    if (matches.length !== 1) return false
    unmatched.delete(matches[0]!)
  }
  return unmatched.size === 0
}

function sameSuggestionPosition(left: SuggestionPosition, right: SuggestionPosition): boolean {
  return (
    left.from === right.from
    && left.to === right.to
    && stableJson(left.parentPath) === stableJson(right.parentPath)
  )
}

function sameSuggestionMetadata(left: SuggestionRecord, right: SuggestionRecord): boolean {
  return (
    left.authorId === right.authorId
    && left.createdAt === right.createdAt
    && left.kind === right.kind
    && left.patchId === right.patchId
    && left.suggestionId === right.suggestionId
  )
}

function suggestionKindOrder(left: SuggestionKind, right: SuggestionKind): number {
  const order: Record<SuggestionKind, number> = {
    insertion: 0,
    deletion: 1,
    replacement: 2,
    format: 3,
    structure: 4,
  }
  return order[left] - order[right]
}

function suggestionNodeSignature(
  node: ProseMirrorNode,
  targetMark: Mark,
): unknown {
  return {
    attrs: node.attrs,
    marks: node.marks
      .filter((mark) => (
        mark !== targetMark
        && !SUGGESTION_MARK_NAMES.has(mark.type.name)
      ))
      .map((mark) => ({ attrs: mark.attrs, type: mark.type.name })),
    text: node.text,
    type: node.type.name,
  }
}

/** The complete suggestion data and the inline sequence its offsets refer
 * to: another person's structure suggestion may not change in either. */
function structureNodeSignature(node: ProseMirrorNode): unknown {
  return structureSuggestionSignature(node)
}

export function suggestionPositionAt(
  document: ProseMirrorNode,
  from: number,
  to: number,
): SuggestionPosition | null {
  if (
    !Number.isSafeInteger(from)
    || !Number.isSafeInteger(to)
    || from < 0
    || to < from
    || to > document.content.size
  ) return null
  try {
    const resolved = document.resolve(from)
    const parentPath: StructuralPathEntry[] = []
    for (let depth = 0; depth <= resolved.depth; depth += 1) {
      const node = resolved.node(depth)
      parentPath.push({
        attrs: node.attrs,
        index: depth === 0 ? 0 : resolved.index(depth - 1),
        type: node.type.name,
      })
    }
    return { from, parentPath, to }
  } catch {
    return null
  }
}

/** How the occurrences of the suggestions in the document before an update
 * are found in the document after it. */
export type SuggestionPositionMapping = {
  map: SuggestionPositionMapper
  /** For an edit lease only: follows a text occurrence whose characters the
   * update wrote again in place; null for any other access. */
  recreated: SuggestionPositionMapper | null
  /** Whether a suggestion no longer in the document was removed the way a
   * rejection removes it: every character it proposed to insert is
   * deleted. Text it proposed to delete may stay or go; a structure
   * suggestion's block may stay or go. Stripping the mark of a proposed
   * insertion and keeping its text would accept it instead. Who may remove
   * a suggestion this way is the policy's decision, not this reading's. */
  removedAsRejected: (suggestionId: string) => boolean
  /** Whether a suggestion stands on a subset of its Yjs units: every
   * character or block that carries it after the update carried it before
   * with the same mark and attributes, and every one that carried it before
   * still carries it or is deleted. */
  unitsKept: (suggestionId: string) => boolean
  /** Whether a suggestion stands on exactly the Yjs units it stood on
   * before, with the same mark and attributes. */
  unitsUnchanged: (suggestionId: string) => boolean
  /** For an edit lease only: whether the update wrote a suggestion again,
   * every unit carrying it new and every unit that carried it before
   * deleted; `recreated` then proves its content and place. False for any
   * other access. */
  unitsWrittenAgain: (suggestionId: string) => boolean
  /** Inline units the re-creation rule has read so far: none unless a text
   * occurrence could not be followed by its own characters. */
  unitsRead: () => number
}

export function createYjsSuggestionPositionMapping(
  before: JSONContent,
  after: JSONContent,
  documents: SuggestionDocumentPair,
  access: CollaborationAccess,
): SuggestionPositionMapping {
  try {
    const beforeFragment = documents.beforeDocument.getXmlFragment(EDITOR_YJS_FRAGMENT)
    const afterFragment = documents.afterDocument.getXmlFragment(EDITOR_YJS_FRAGMENT)
    const beforeInitialized = initProseMirrorDoc(beforeFragment, schema)
    const afterInitialized = initProseMirrorDoc(afterFragment, schema)
    if (
      !beforeInitialized.doc.eq(schema.nodeFromJSON(before))
      || !afterInitialized.doc.eq(schema.nodeFromJSON(after))
    ) {
      throw policyViolation()
    }
    const mapTextPosition = (position: number): number | null => relativePositionToAbsolutePosition(
      documents.afterDocument,
      afterFragment,
      absolutePositionToRelativePosition(
        position,
        beforeFragment,
        beforeInitialized.mapping,
      ) as Y.RelativePosition,
      afterInitialized.mapping,
    )
    const map: SuggestionPositionMapper = (position, occurrence = 'text') => {
      try {
        if (occurrence === 'block') {
          // A block keeps its identity through the update: the same Yjs
          // element, wherever it is now. A copy of it elsewhere is not it.
          const element = xmlElementAt(beforeFragment, beforeInitialized.doc, position.from)
          const item = element?._item
            ? Y.getItem(documents.afterDocument.store, element._item.id)
            : null
          if (
            !element
            || !(item instanceof Y.Item)
            || item.deleted
            || !(item.content instanceof Y.ContentType)
            || !(item.content.type instanceof Y.XmlElement)
            || item.content.type.nodeName !== element.nodeName
          ) return null
          const from = proseMirrorPositionOf(afterFragment, afterInitialized.doc, item.content.type)
          const node = from === null ? null : afterInitialized.doc.nodeAt(from)
          if (from === null || !node || node.type.name !== element.nodeName) return null
          return suggestionPositionAt(afterInitialized.doc, from, from + node.nodeSize)
        }
        // A text position is bound to the character on its left. The end of
        // an occurrence is therefore its own last character already; its
        // start is bound to its own first character, so text written
        // directly in front of it does not move it.
        const ownStart = position.to > position.from
        const start = mapTextPosition(ownStart ? position.from + 1 : position.from)
        const to = mapTextPosition(position.to)
        if (start === null || to === null) return null
        return suggestionPositionAt(afterInitialized.doc, ownStart ? start - 1 : start, to)
      } catch {
        return null
      }
    }
    let units: { after: SuggestionUnits; before: SuggestionUnits } | null = null
    const unitTables = () => {
      units ??= {
        after: suggestionUnits(afterInitialized.mapping),
        before: suggestionUnits(beforeInitialized.mapping),
      }
      return units
    }
    const deletedAfter = (unit: string): boolean => unitDeletedIn(documents.afterDocument, unit)
    // Both rules answer false on any doubt, which refuses the update.
    const removedAsRejected = (suggestionId: string): boolean => {
      try {
        const before = unitTables().before.get(suggestionId)
        if (!before || before.size === 0) return false
        for (const [unit, { kind }] of before) {
          if (kind === 'insertion' || kind === 'modification') {
            if (!deletedAfter(unit)) return false
          }
        }
        return true
      } catch {
        return false
      }
    }
    /** The units of a suggestion before and after the update, when it has
     * units on both sides. */
    const unitsOf = (suggestionId: string) => {
      const tables = unitTables()
      const before = tables.before.get(suggestionId)
      const after = tables.after.get(suggestionId)
      return before && after && before.size > 0 && after.size > 0 ? { after, before } : null
    }
    const carriedBefore = (before: UnitCarriers, [unit, carrier]: [string, UnitCarrier]): boolean => {
      const previous = before.get(unit)
      return previous !== undefined && previous.kind === carrier.kind && previous.value === carrier.value
    }
    const unitsKept = (suggestionId: string): boolean => {
      try {
        const units = unitsOf(suggestionId)
        if (!units) return false
        return [...units.after].every((entry) => carriedBefore(units.before, entry))
          && [...units.before.keys()].every((unit) => units.after.has(unit) || deletedAfter(unit))
      } catch {
        return false
      }
    }
    const unitsUnchanged = (suggestionId: string): boolean => {
      try {
        const units = unitsOf(suggestionId)
        return units !== null
          && units.before.size === units.after.size
          && [...units.after].every((entry) => carriedBefore(units.before, entry))
      } catch {
        return false
      }
    }
    if (access !== 'edit') {
      return {
        map,
        recreated: null,
        removedAsRejected,
        unitsKept,
        unitsRead: () => 0,
        unitsUnchanged,
        unitsWrittenAgain: () => false,
      }
    }
    const unitsWrittenAgain = (suggestionId: string): boolean => {
      try {
        const units = unitsOf(suggestionId)
        return units !== null
          && [...units.after.keys()].every((unit) => createdByUpdate(documents.beforeDocument, unit))
          && [...units.before.keys()].every(deletedAfter)
      } catch {
        return false
      }
    }
    const recreation = createRecreationMapper(documents, {
      after: afterInitialized.doc,
      before: beforeInitialized.doc,
      beforeFragment,
      beforeMapping: beforeInitialized.mapping,
    })
    return {
      map,
      recreated: recreation.map,
      removedAsRejected,
      unitsKept,
      unitsRead: recreation.unitsRead,
      unitsUnchanged,
      unitsWrittenAgain,
    }
  } catch (error) {
    if (error instanceof CollaborationError) throw error
    throw policyViolation()
  }
}

/** What a unit carries of one suggestion: the mark kind, or `structure` for
 * a block, and the mark's attributes or the structure data, as stable JSON. */
type UnitCarrier = { kind: SuggestionMarkKind | 'structure'; value: string }

type UnitCarriers = Map<string, UnitCarrier>

/**
 * The Yjs units every suggestion stands on, by suggestion id: each
 * character a suggestion mark covers (by its item id and offset, with the
 * mark's kind and attributes) and each block that carries a structure
 * suggestion (with its data). Read from the Yjs formatting itself, only in
 * the texts whose ProseMirror nodes carry a suggestion mark, so the cost
 * follows the suggestions, not the document.
 */
type SuggestionUnits = Map<string, UnitCarriers>

type ProseMirrorBindingMapping = ReturnType<typeof initProseMirrorDoc>['mapping']

function suggestionUnits(mapping: ProseMirrorBindingMapping): SuggestionUnits {
  const units: SuggestionUnits = new Map()
  const add = (suggestionId: string, unit: string, carrier: UnitCarrier) => {
    const entry = units.get(suggestionId)
    if (entry) entry.set(unit, carrier)
    else units.set(suggestionId, new Map([[unit, carrier]]))
  }
  // The binding maps each block element to its node; the texts inside a
  // block are read only when one of the block's own inline nodes carries a
  // suggestion mark.
  for (const [type, projected] of mapping) {
    if (!(type instanceof Y.XmlElement) || Array.isArray(projected)) continue
    const data = type.getAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR)
    if (type._item && isStructureSuggestionData(data)) {
      add(
        data.suggestionId,
        unitKey(type._item.id.client, type._item.id.clock),
        { kind: 'structure', value: stableJson(data) },
      )
    }
    let marked = false
    ;(projected as ProseMirrorNode).forEach((child: ProseMirrorNode) => {
      if (child.isInline && child.marks.some((mark: Mark) => SUGGESTION_MARK_NAMES.has(mark.type.name))) {
        marked = true
      }
    })
    if (!marked) continue
    for (const child of type.toArray()) {
      if (child instanceof Y.XmlText) addTextUnits(child, add)
    }
  }
  return units
}

/** The formatting in force at a character is set by the closest live format
 * item of each key to its left, exactly as Yjs reads it. */
function addTextUnits(
  text: Y.XmlText,
  add: (suggestionId: string, unit: string, carrier: UnitCarrier) => void,
): void {
  const attributes = new Map<string, unknown>()
  for (let item = text._start; item !== null; item = item.right) {
    if (item.deleted) continue
    if (item.content instanceof Y.ContentFormat) {
      if (item.content.value === null) attributes.delete(item.content.key)
      else attributes.set(item.content.key, item.content.value)
      continue
    }
    if (!(item.content instanceof Y.ContentString)) continue
    for (const [key, value] of attributes) {
      const kind = suggestionMarkKindOfAttribute(key)
      const suggestionId = value && typeof value === 'object'
        ? Reflect.get(value, 'suggestionId')
        : null
      if (!kind || typeof suggestionId !== 'string') continue
      const carrier: UnitCarrier = { kind, value: stableJson(value) }
      for (let offset = 0; offset < item.length; offset += 1) {
        add(suggestionId, unitKey(item.id.client, item.id.clock + offset), carrier)
      }
    }
  }
}

/** The suggestion mark kind a Yjs format key stands for; the binding adds a
 * hash to the key of a mark that may overlap itself. */
function suggestionMarkKindOfAttribute(key: string): SuggestionMarkKind | null {
  const name = /(.*)(--[a-zA-Z0-9+/=]{8})$/.exec(key)?.[1] ?? key
  return SUGGESTION_MARK_NAMES.has(name) ? name as SuggestionMarkKind : null
}

function unitKey(client: number, clock: number): string {
  return `${client}:${clock}`
}

function parseUnitKey(unit: string): { client: number; clock: number } | null {
  const [client, clock] = unit.split(':').map(Number)
  return Number.isSafeInteger(client) && Number.isSafeInteger(clock)
    ? { client: client!, clock: clock! }
    : null
}

/** Whether a unit of the document before the update is deleted after it. A
 * unit that cannot be found counts as still there, so a doubt refuses. */
function unitDeletedIn(afterDocument: Y.Doc, unit: string): boolean {
  const id = parseUnitKey(unit)
  if (!id) return false
  try {
    const struct = Y.getItem(afterDocument.store, Y.createID(id.client, id.clock))
    return !(struct instanceof Y.Item) || struct.deleted
  } catch {
    return false
  }
}

/** Whether the update wrote a unit: its clock lies beyond what the document
 * before the update held of its client. */
function createdByUpdate(beforeDocument: Y.Doc, unit: string): boolean {
  const id = parseUnitKey(unit)
  return id !== null && id.clock >= Y.getState(beforeDocument.store, id.client)
}

/** The characters and inline leaves of a document in reading order, block
 * boundaries left out: for each, its Yjs item and its ProseMirror position,
 * content and formatting. */
type InlineUnits = {
  clients: number[]
  clocks: number[]
  /** A character's UTF-16 code unit, or -1 for an inline leaf. */
  codes: number[]
  /** A leaf's node type; empty for a character. */
  leaves: string[]
  /** Every mark with its attributes, and a leaf's own attributes. */
  marks: string[]
  /** Whether the unit carries a suggestion mark. */
  marked: boolean[]
  positions: number[]
}

/**
 * y-tiptap writes a split, a join, a block type change, a list wrap or a
 * paste as the deletion of the moved text and the insertion of a copy, so a
 * pending suggestion inside the moved text consists of new Yjs items and its
 * own characters no longer lead to it. For an edit lease, which may restructure
 * original text directly anyway, such an occurrence is the same one when it
 * comes back in place. Between the nearest characters on either side that
 * the update kept (block boundaries ignored), the stretch from the first to
 * the last suggestion-marked unit must come back unit for unit, with the same
 * text, marks and metadata, and the original text may differ on one side of
 * it only: Enter over a selection or a paste in front of a suggestion
 * changes what precedes it, a change behind it what follows. Text that
 * changes on both sides has moved the suggestion to another place, which an
 * edit lease may not do to a pending suggestion.
 *
 * Asked only for a text occurrence whose own characters could not be
 * followed, and only after checking that the update deleted every one of
 * them. The two documents are then read once, in one linear pass each.
 */
function createRecreationMapper(
  documents: SuggestionDocumentPair,
  projection: {
    after: ProseMirrorNode
    before: ProseMirrorNode
    beforeFragment: Y.XmlFragment
    beforeMapping: Parameters<typeof absolutePositionToRelativePosition>[2]
  },
): { map: SuggestionPositionMapper; unitsRead: () => number } {
  let tables: {
    after: InlineUnits
    afterIndex: (client: number, clock: number) => number
    before: InlineUnits
  } | null | undefined
  let unitsRead = 0
  const runs = new Map<string, { first: number; last: number; shift: number } | null>()
  const prepare = () => {
    if (tables !== undefined) return tables
    const before = inlineUnits(projection.before, documents.beforeDocument.getXmlFragment(EDITOR_YJS_FRAGMENT))
    const after = inlineUnits(projection.after, documents.afterDocument.getXmlFragment(EDITOR_YJS_FRAGMENT))
    unitsRead += (before?.units.positions.length ?? 0) + (after?.units.positions.length ?? 0)
    tables = before && after
      ? { after: after.units, afterIndex: after.indexOf, before: before.units }
      : null
    return tables
  }
  const map: SuggestionPositionMapper = (position, occurrence = 'text') => {
    try {
      if (occurrence !== 'text' || position.to <= position.from) return null
      if (!projection.before.nodeAt(position.from)?.isText) return null
      if (!occurrenceWasWrittenAgain(documents, projection, position)) return null
      const prepared = prepare()
      if (!prepared) return null
      const { after, afterIndex, before } = prepared
      const first = indexAtPosition(before.positions, position.from)
      const last = first + (position.to - position.from) - 1
      if (first < 0 || before.positions[last] !== position.to - 1) return null
      const alive = (index: number) => afterIndex(before.clients[index]!, before.clocks[index]!) >= 0
      let left = first - 1
      while (left >= 0 && !alive(left)) left -= 1
      let right = last + 1
      while (right < before.positions.length && !alive(right)) right += 1
      const leftAfter = left >= 0 ? afterIndex(before.clients[left]!, before.clocks[left]!) : -1
      const rightAfter = right < before.positions.length
        ? afterIndex(before.clients[right]!, before.clocks[right]!)
        : after.positions.length
      if (leftAfter >= rightAfter) return null
      const runKey = `${left}:${right}`
      if (!runs.has(runKey)) {
        runs.set(runKey, recreatedStretch(before, left + 1, right, after, leftAfter + 1, rightAfter))
      }
      const stretch = runs.get(runKey)
      if (!stretch || first < stretch.first || last > stretch.last) return null
      const from = after.positions[first + stretch.shift]
      const to = after.positions[last + stretch.shift]
      if (from === undefined || to === undefined) return null
      return suggestionPositionAt(projection.after, from, to + 1)
    } catch {
      return null
    }
  }
  return { map, unitsRead: () => unitsRead }
}

/** Whether the update deleted every character of a text occurrence: read
 * from the occurrence's own Yjs items, before any document is walked. */
function occurrenceWasWrittenAgain(
  documents: SuggestionDocumentPair,
  projection: {
    beforeFragment: Y.XmlFragment
    beforeMapping: Parameters<typeof absolutePositionToRelativePosition>[2]
  },
  position: SuggestionPosition,
): boolean {
  // A text position names the character on its left: one past the start
  // names the occurrence's first character.
  const relative = absolutePositionToRelativePosition(
    position.from + 1,
    projection.beforeFragment,
    projection.beforeMapping,
  ) as Y.RelativePosition
  if (!relative.item) return false
  const length = position.to - position.from
  let item: Y.Item | null = null
  const start = Y.getItem(documents.beforeDocument.store, relative.item)
  if (start instanceof Y.Item) item = start
  let offset = relative.item.clock - (item?.id.clock ?? 0)
  let seen = 0
  while (item !== null && seen < length) {
    if (!item.deleted) {
      if (item.content instanceof Y.ContentString) {
        for (let index = offset; index < item.length && seen < length; index += 1) {
          const now = Y.getItem(
            documents.afterDocument.store,
            Y.createID(item.id.client, item.id.clock + index),
          )
          if (now instanceof Y.Item && !now.deleted) return false
          seen += 1
        }
      } else if (!(item.content instanceof Y.ContentFormat)) {
        return false
      }
    }
    offset = 0
    item = item.right
  }
  return seen === length
}

/** Reads a document's inline units: the ProseMirror projection first, then
 * its Yjs items in the same order, each checked against the unit it stands
 * for. Null when the two disagree. */
function inlineUnits(
  document: ProseMirrorNode,
  fragment: Y.XmlFragment,
): { indexOf: (client: number, clock: number) => number; units: InlineUnits } | null {
  const units: InlineUnits = {
    clients: [],
    clocks: [],
    codes: [],
    leaves: [],
    marks: [],
    marked: [],
    positions: [],
  }
  const markKey = (node: ProseMirrorNode): string => stableJson(
    node.marks.map((mark) => ({ attrs: mark.attrs, type: mark.type.name })),
  )
  const isMarked = (node: ProseMirrorNode): boolean => node.marks.some((mark) => (
    SUGGESTION_MARK_NAMES.has(mark.type.name)
  ))
  document.descendants((node, position) => {
    if (node.isText) {
      const text = node.text ?? ''
      const marks = markKey(node)
      const marked = isMarked(node)
      for (let offset = 0; offset < text.length; offset += 1) {
        units.codes.push(text.charCodeAt(offset))
        units.leaves.push('')
        units.marks.push(marks)
        units.marked.push(marked)
        units.positions.push(position + offset)
      }
      return false
    }
    if (node.isLeaf) {
      units.codes.push(-1)
      units.leaves.push(node.type.name)
      units.marks.push(stableJson({ attrs: node.attrs, marks: markKey(node) }))
      units.marked.push(isMarked(node))
      units.positions.push(position)
    }
    return true
  })
  // Runs of consecutive clocks of one client, for finding a unit by its item.
  const runs = new Map<number, Array<{ clock: number; first: number; length: number }>>()
  const addRun = (client: number, clock: number, length: number) => {
    const first = units.clients.length
    for (let offset = 0; offset < length; offset += 1) {
      units.clients.push(client)
      units.clocks.push(clock + offset)
    }
    const list = runs.get(client) ?? []
    list.push({ clock, first, length })
    runs.set(client, list)
  }
  const walk = (parent: Y.XmlFragment | Y.XmlElement): boolean => {
    for (let item = parent._start; item !== null; item = item.right) {
      if (item.deleted || !(item.content instanceof Y.ContentType)) continue
      const type = item.content.type
      if (type instanceof Y.XmlText) {
        for (let character = type._start; character !== null; character = character.right) {
          if (character.deleted || character.content instanceof Y.ContentFormat) continue
          if (!(character.content instanceof Y.ContentString)) return false
          const text = character.content.str
          const first = units.clients.length
          for (let offset = 0; offset < text.length; offset += 1) {
            if (units.codes[first + offset] !== text.charCodeAt(offset)) return false
          }
          addRun(character.id.client, character.id.clock, text.length)
        }
      } else if (type instanceof Y.XmlElement) {
        const nodeType = schema.nodes[type.nodeName]
        if (!nodeType) return false
        if (nodeType.isLeaf) {
          if (units.leaves[units.clients.length] !== type.nodeName) return false
          addRun(item.id.client, item.id.clock, 1)
        } else if (!walk(type)) {
          return false
        }
      } else {
        return false
      }
    }
    return true
  }
  if (!walk(fragment) || units.clients.length !== units.positions.length) return null
  for (const list of runs.values()) list.sort((left, right) => left.clock - right.clock)
  return {
    indexOf: (client, clock) => {
      const list = runs.get(client)
      if (!list) return -1
      let low = 0
      let high = list.length - 1
      while (low <= high) {
        const middle = (low + high) >> 1
        const run = list[middle]!
        if (clock < run.clock) high = middle - 1
        else if (clock >= run.clock + run.length) low = middle + 1
        else return run.first + clock - run.clock
      }
      return -1
    },
    units,
  }
}

/**
 * Compares a run of the document before the update with the run that
 * replaced it. The stretch from the first to the last suggestion-marked unit
 * must be equal unit for unit, and so must the units in front of it or the
 * units behind it. Returns the stretch in the run before and how far its
 * units moved, or null.
 */
function recreatedStretch(
  before: InlineUnits,
  beforeStart: number,
  beforeEnd: number,
  after: InlineUnits,
  afterStart: number,
  afterEnd: number,
): { first: number; last: number; shift: number } | null {
  const markedBounds = (units: InlineUnits, start: number, end: number) => {
    let first = -1
    let last = -1
    for (let index = start; index < end; index += 1) {
      if (!units.marked[index]) continue
      if (first < 0) first = index
      last = index
    }
    return first < 0 ? null : { first, last }
  }
  const beforeStretch = markedBounds(before, beforeStart, beforeEnd)
  const afterStretch = markedBounds(after, afterStart, afterEnd)
  if (!beforeStretch || !afterStretch) return null
  const equalUnits = (beforeFrom: number, afterFrom: number, length: number): boolean => {
    for (let offset = 0; offset < length; offset += 1) {
      const beforeIndex = beforeFrom + offset
      const afterIndex = afterFrom + offset
      if (
        before.codes[beforeIndex] !== after.codes[afterIndex]
        || before.leaves[beforeIndex] !== after.leaves[afterIndex]
        || before.marks[beforeIndex] !== after.marks[afterIndex]
      ) return false
    }
    return true
  }
  const stretchLength = beforeStretch.last - beforeStretch.first + 1
  if (
    afterStretch.last - afterStretch.first + 1 !== stretchLength
    || !equalUnits(beforeStretch.first, afterStretch.first, stretchLength)
  ) return null
  const leadingLength = beforeStretch.first - beforeStart
  const trailingLength = beforeEnd - beforeStretch.last - 1
  const sameLeading = afterStretch.first - afterStart === leadingLength
    && equalUnits(beforeStart, afterStart, leadingLength)
  const sameTrailing = afterEnd - afterStretch.last - 1 === trailingLength
    && equalUnits(beforeStretch.last + 1, afterStretch.last + 1, trailingLength)
  if (!sameLeading && !sameTrailing) return null
  return { ...beforeStretch, shift: afterStretch.first - beforeStretch.first }
}

/** The index of the unit at a ProseMirror position, or -1. */
function indexAtPosition(positions: readonly number[], position: number): number {
  let low = 0
  let high = positions.length - 1
  while (low <= high) {
    const middle = (low + high) >> 1
    const value = positions[middle]!
    if (value === position) return middle
    if (value < position) low = middle + 1
    else high = middle - 1
  }
  return -1
}

function assertSuggestionYjsTopology(
  documents: SuggestionDocumentPair,
  changedSuggestionIds: ReadonlySet<string>,
): void {
  const beforeRoot = documents.beforeDocument.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const afterRoot = documents.afterDocument.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const beforeTypes = collectSuggestionXmlTypes(beforeRoot)
  const afterTypes = collectSuggestionXmlTypes(afterRoot)
  const mappedTypes = new Map<SuggestionXmlType, SuggestionXmlType>()
  const mappedAfterTypes = new Set<SuggestionXmlType>()

  for (const beforeType of beforeTypes) {
    const afterType = mapSuggestionXmlType(beforeType, documents.afterDocument)
    if (!afterType) {
      if (
        beforeType instanceof Y.XmlText
        && isChangedInsertionText(beforeType, changedSuggestionIds)
      ) continue
      throw policyViolation()
    }
    if (
      (beforeType instanceof Y.XmlElement) !== (afterType instanceof Y.XmlElement)
      || mappedAfterTypes.has(afterType)
    ) {
      throw policyViolation()
    }
    mappedTypes.set(beforeType, afterType)
    mappedAfterTypes.add(afterType)
  }

  for (const [beforeType, afterType] of mappedTypes) {
    const expectedParent = beforeType.parent === beforeRoot
      ? afterRoot
      : isSuggestionXmlType(beforeType.parent)
        ? mappedTypes.get(beforeType.parent)
        : null
    if (!expectedParent || afterType.parent !== expectedParent) throw policyViolation()
  }

  assertMappedChildOrder(beforeRoot, afterRoot, mappedTypes, mappedAfterTypes)
  for (const [beforeType, afterType] of mappedTypes) {
    if (beforeType instanceof Y.XmlElement && afterType instanceof Y.XmlElement) {
      assertMappedChildOrder(beforeType, afterType, mappedTypes, mappedAfterTypes)
    }
  }

  // Besides new text in a known element, an update may add a proposed block:
  // Enter at the end of a block. The types are visited parent first, and an
  // admitted block admits its whole subtree, which it has checked.
  const admittedTypes = new Set<SuggestionXmlType>()
  for (const afterType of afterTypes) {
    if (mappedAfterTypes.has(afterType) || admittedTypes.has(afterType)) continue
    const parent = afterType.parent
    if (
      parent !== afterRoot
      && (!isSuggestionXmlType(parent) || !mappedAfterTypes.has(parent))
    ) {
      throw policyViolation()
    }
    if (afterType instanceof Y.XmlText) {
      if (!isChangedInsertionText(afterType, changedSuggestionIds)) throw policyViolation()
      continue
    }
    if (
      !isProposedBlock(afterType, changedSuggestionIds)
      || !continuesProposedBreak(afterType, mappedAfterTypes, admittedTypes)
    ) {
      throw policyViolation()
    }
    admittedTypes.add(afterType)
    for (const descendant of collectSuggestionXmlTypes(afterType)) admittedTypes.add(descendant)
  }
}

/** A block whose whole subtree holds only blocks that may be proposed, with
 * the attributes allowed for them (`isProposableBlock`), and only text this
 * update proposes. */
function isProposedBlock(
  element: Y.XmlElement,
  changedSuggestionIds: ReadonlySet<string>,
): boolean {
  if (!isProposableBlock(element.nodeName, liveAttributes(element))) return false
  const children = element.toArray()
  return children.length > 0 && children.every((child) => (
    child instanceof Y.XmlText
      ? liveAttributes(child).length === 0
        && isChangedInsertionText(child, changedSuggestionIds)
      : child instanceof Y.XmlElement && isProposedBlock(child, changedSuggestionIds)
  ))
}

/** Every attribute a type holds, read from its attribute map itself: a
 * plain object built from the attributes cannot hold a key such as
 * `__proto__`, so a check over one would never see it. */
function liveAttributes(type: Y.XmlElement | Y.XmlText): Array<[string, unknown]> {
  const attributes: Array<[string, unknown]> = []
  for (const [name, item] of type._map) {
    if (item.deleted) continue
    attributes.push([name, item.content.getContent()[item.length - 1]])
  }
  return attributes
}

/** The shape Enter at the end of a block produces: the block before the new
 * one is known (existing or itself proposed by this update) and ends with
 * the same insertion the new block starts with. */
function continuesProposedBreak(
  element: Y.XmlElement,
  mappedAfterTypes: ReadonlySet<SuggestionXmlType>,
  admittedTypes: ReadonlySet<SuggestionXmlType>,
): boolean {
  const left = element.prevSibling
  if (
    !(left instanceof Y.XmlElement)
    || !(mappedAfterTypes.has(left) || admittedTypes.has(left))
  ) return false
  const leftId = insertionIdAtEdge(left, 'last')
  return leftId !== null && leftId === insertionIdAtEdge(element, 'first')
}

function insertionIdAtEdge(type: SuggestionXmlType, edge: 'first' | 'last'): string | null {
  if (type instanceof Y.XmlText) {
    const delta = type.toDelta() as Array<{ attributes?: Record<string, unknown> }>
    const insertion = (edge === 'first' ? delta[0] : delta.at(-1))?.attributes?.insertion
    if (!insertion || typeof insertion !== 'object') return null
    const suggestionId = Reflect.get(insertion, 'suggestionId')
    return typeof suggestionId === 'string' ? suggestionId : null
  }
  const children = type.toArray().filter(isSuggestionXmlType)
  const child = edge === 'first' ? children[0] : children.at(-1)
  return child ? insertionIdAtEdge(child, edge) : null
}

function collectSuggestionXmlTypes(parent: SuggestionXmlParent): SuggestionXmlType[] {
  const types: SuggestionXmlType[] = []
  for (const child of parent.toArray()) {
    if (!isSuggestionXmlType(child)) throw policyViolation()
    types.push(child)
    if (child instanceof Y.XmlElement) {
      types.push(...collectSuggestionXmlTypes(child))
    }
  }
  return types
}

function mapSuggestionXmlType(
  source: SuggestionXmlType,
  targetDocument: Y.Doc,
): SuggestionXmlType | null {
  const absolute = Y.createAbsolutePositionFromRelativePosition(
    Y.createRelativePositionFromTypeIndex(source, 0),
    targetDocument,
  )
  return isSuggestionXmlType(absolute?.type) ? absolute.type : null
}

function assertMappedChildOrder(
  beforeParent: SuggestionXmlParent,
  afterParent: SuggestionXmlParent,
  mappedTypes: ReadonlyMap<SuggestionXmlType, SuggestionXmlType>,
  mappedAfterTypes: ReadonlySet<SuggestionXmlType>,
): void {
  const expected = beforeParent.toArray()
    .filter(isSuggestionXmlType)
    .flatMap((child) => {
      const mapped = mappedTypes.get(child)
      return mapped ? [mapped] : []
    })
  const actual = afterParent.toArray()
    .filter(isSuggestionXmlType)
    .filter((child) => mappedAfterTypes.has(child))
  if (
    expected.length !== actual.length
    || expected.some((child, index) => child !== actual[index])
  ) {
    throw policyViolation()
  }
}

function isChangedInsertionText(
  text: Y.XmlText,
  changedSuggestionIds: ReadonlySet<string>,
): boolean {
  const delta = text.toDelta() as Array<{
    attributes?: Record<string, unknown>
    insert: unknown
  }>
  return delta.length > 0 && delta.every((part) => {
    if (typeof part.insert !== 'string' || part.insert.length === 0) return false
    const insertion = part.attributes?.insertion
    if (!insertion || typeof insertion !== 'object') return false
    const suggestionId = Reflect.get(insertion, 'suggestionId')
    return typeof suggestionId === 'string' && changedSuggestionIds.has(suggestionId)
  })
}

function isSuggestionXmlType(value: unknown): value is SuggestionXmlType {
  return value instanceof Y.XmlElement || value instanceof Y.XmlText
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

function policyViolation(
  reason: 'access_revoked' | 'invalid_schema' | 'suggestion_policy_violation' = 'suggestion_policy_violation',
  httpStatus = 403,
): CollaborationError {
  return new CollaborationError(reason, {
    closeCode: reason === 'invalid_schema'
      ? CloseCodes.incompatible
      : CloseCodes.accessRevoked,
    httpStatus,
  })
}

/**
 * A suggest-only update may delete exactly three kinds of items: characters
 * of the actor's own pending insertions (other than structure suggestions),
 * formatting items, and attribute values it replaces. Original text is never
 * deleted, not even to be written again unmarked, which would keep the text
 * but destroy the identity comments and other pending changes refer to.
 * Types (blocks, text nodes, inline atoms) are judged by the topology rules.
 *
 * The rule judges only the items the update deleted, found in the document
 * before it. To find the formatting in effect at them it reads the text they
 * come from once, not once per deleted character, so its cost follows the
 * update and the text it touches, never the deletion history of the rest of
 * the document. Returns how many deleted items it judged and how many items
 * it read.
 */
export function assertSuggestionDeleteSet(
  documents: SuggestionDocumentPair,
  beforeRecords: ReadonlyMap<string, SuggestionRecord>,
  actorUserId: string,
): { judged: number; read: number } {
  // Without the update's own deletions the rule cannot judge anything.
  const deleted = documents.deletedByUpdate
  if (!deleted) throw policyViolation()
  const beforeStore = documents.beforeDocument.store
  const insertionFormat = insertionFormatReader()
  let judged = 0
  try {
    for (const [client, ranges] of deleted.clients) {
      const beforeState = Y.getState(beforeStore, client)
      for (const range of ranges) {
        const end = range.clock + range.len
        let clock = range.clock
        while (clock < end) {
          if (clock >= beforeState) {
            // Created while the update was applied and deleted again: only
            // the formatting cleanup that can follow an update does that.
            const created = Y.getItem(documents.afterDocument.store, Y.createID(client, clock))
            if (!(created instanceof Y.Item) || !(created.content instanceof Y.ContentFormat)) {
              throw policyViolation()
            }
            judged += 1
            clock = created.id.clock + created.length
            continue
          }
          const item = Y.getItem(beforeStore, Y.createID(client, clock))
          if (!(item instanceof Y.Item) || item.deleted) throw policyViolation()
          if (!isDeletableBySuggestion(item, beforeRecords, actorUserId, insertionFormat.at)) {
            throw policyViolation()
          }
          judged += 1
          clock = item.id.clock + item.length
        }
      }
    }
  } catch (error) {
    if (error instanceof CollaborationError) throw error
    throw policyViolation()
  }
  return { judged, read: insertionFormat.read() }
}

function isDeletableBySuggestion(
  item: Y.Item,
  beforeRecords: ReadonlyMap<string, SuggestionRecord>,
  actorUserId: string,
  insertionAttributeAt: (item: Y.Item) => unknown,
): boolean {
  if (item.content instanceof Y.ContentFormat) return true
  if (item.parentSub !== null) return true
  if (item.content instanceof Y.ContentType) return true
  if (!(item.content instanceof Y.ContentString)) return false
  const insertion = insertionAttributeAt(item)
  const suggestionId = insertion && typeof insertion === 'object'
    ? Reflect.get(insertion, 'suggestionId')
    : null
  const record = typeof suggestionId === 'string' ? beforeRecords.get(suggestionId) : undefined
  return record !== undefined && record.authorId === actorUserId && record.kind !== 'structure'
}

/** Reads the value of the `insertion` format at items: the closest live
 * `insertion` format item to the left of an item sets it (an end marker
 * clears it). A walk to the left stops at the first item whose value is
 * already known, and every item it passed takes that value over, so an
 * update reads each item of a text once, plus the one item each walk stops
 * at, however many of its characters it deletes. */
function insertionFormatReader(): { at: (item: Y.Item) => unknown; read: () => number } {
  const known = new Map<Y.Item, unknown>()
  let read = 0
  return {
    at: (item) => {
      const passed: Y.Item[] = [item]
      let value: unknown = null
      for (let cursor = item.left; cursor !== null; cursor = cursor.left) {
        read += 1
        if (
          !cursor.deleted
          && cursor.content instanceof Y.ContentFormat
          && cursor.content.key === 'insertion'
        ) {
          value = cursor.content.value
          break
        }
        // No `insertion` format item lies between this item and the one
        // the walk started from, so both see the same value.
        if (known.has(cursor)) {
          value = known.get(cursor)
          break
        }
        passed.push(cursor)
      }
      for (const entry of passed) known.set(entry, value)
      return value
    },
    read: () => read,
  }
}

/**
 * No update, direct or suggested, may leave a structure suggestion that
 * cannot be applied: the final projection of the document, and with it every
 * later change summary and export, would fail for everyone in the room. A
 * suggestion that already could not be applied before the update does not
 * block it; such a stored suggestion is cleared by rejecting its patch.
 */
function assertStructureSuggestionsStayApplicable(
  before: JSONContent,
  after: JSONContent,
  afterRecords: ReadonlyMap<string, SuggestionRecord>,
): void {
  if (![...afterRecords.values()].some((record) => record.kind === 'structure')) return
  const inapplicable = (document: JSONContent): ReadonlySet<string> | null => {
    try {
      return inapplicableStructureSuggestionIds(schema.nodeFromJSON(document))
    } catch {
      return null
    }
  }
  const afterFailures = inapplicable(after)
  if (afterFailures?.size === 0) return
  const beforeFailures = inapplicable(before)
  // The projection of the other suggestions already failed before this
  // update; it cannot be judged here, and the update did not cause it.
  if (afterFailures === null) {
    if (beforeFailures === null) return
    throw policyViolation()
  }
  for (const suggestionId of afterFailures) {
    if (!beforeFailures?.has(suggestionId)) throw policyViolation()
  }
}

/** The Yjs element of the block that starts at a ProseMirror position. */
function xmlElementAt(
  fragment: Y.XmlFragment,
  document: ProseMirrorNode,
  position: number,
): Y.XmlElement | null {
  const node = document.nodeAt(position)
  if (!node) return null
  const resolved = document.resolve(position)
  let current: Y.XmlElement | Y.XmlFragment = fragment
  for (let depth = 0; depth <= resolved.depth; depth += 1) {
    const child: unknown = current.get(resolved.index(depth))
    if (!(child instanceof Y.XmlElement)) return null
    current = child
  }
  return current instanceof Y.XmlElement && current.nodeName === node.type.name ? current : null
}

/** The ProseMirror position at which a Yjs block element starts. */
function proseMirrorPositionOf(
  fragment: Y.XmlFragment,
  document: ProseMirrorNode,
  element: Y.XmlElement,
): number | null {
  const path: number[] = []
  let current: Y.XmlElement | Y.XmlFragment = element
  while (current !== fragment) {
    const parent: unknown = current.parent
    if (!(parent instanceof Y.XmlElement) && !(parent instanceof Y.XmlFragment)) return null
    const index = parent.toArray().indexOf(current as Y.XmlElement)
    if (index < 0) return null
    path.unshift(index)
    current = parent
  }
  let position = 0
  let node = document
  for (const [depth, index] of path.entries()) {
    if (index >= node.childCount) return null
    for (let child = 0; child < index; child += 1) position += node.child(child).nodeSize
    node = node.child(index)
    if (depth < path.length - 1) position += 1
  }
  return position
}
