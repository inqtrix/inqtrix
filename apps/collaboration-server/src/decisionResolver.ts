import {
  applySuggestion,
  revertSuggestion,
} from '@handlewithcare/prosemirror-suggest-changes'
import { getSchema } from '@tiptap/core'
import type { Mark, Node as ProseMirrorNode } from '@tiptap/pm/model'
import { EditorState, type Transaction } from '@tiptap/pm/state'
import { initProseMirrorDoc, updateYFragment } from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  completeTextblocksCreatedSince,
  createEditorSchemaExtensions,
  isStructureSuggestionData,
  proposedBlockAnchorPositions,
  resolveStructureSuggestion,
  suggestionDescriptors,
  type SuggestionDescriptor,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import { CloseCodes, CollaborationError } from './errors'

export type DecisionInput = {
  decision: 'accept' | 'reject'
  patchIds: string[]
}

export type DecisionResult = {
  patchIds: string[]
  suggestions: SuggestionDescriptor[]
  suggestionIds: string[]
}

type SuggestionRange = {
  from: number
  to: number
}

const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

export function resolvePatchDecision(
  document: Y.Doc,
  input: DecisionInput,
): DecisionResult {
  const patchIds = new Set(input.patchIds)
  if (patchIds.size === 0 || patchIds.size !== input.patchIds.length) throw decisionConflict()

  const fragment = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, schema)
  let state = EditorState.create({ doc: initialized.doc, schema })
  const before = descriptorsOrConflict(state.doc)
  const selected = before.filter((item) => patchIds.has(item.patchId))
  if (selected.length === 0) throw decisionConflict()
  for (const patchId of patchIds) {
    if (!selected.some((item) => item.patchId === patchId)) throw decisionConflict()
  }

  const selectedIds = new Set(selected.map((item) => item.suggestionId))
  const unselectedBefore = descriptorFingerprints(before, selectedIds)
  const orderedIds = [...selected]
    .sort((left, right) => (
      Number(right.kind === 'structure') - Number(left.kind === 'structure')
      || Number(right.kind === 'format') - Number(left.kind === 'format')
      || Number(right.kind === 'replacement') - Number(left.kind === 'replacement')
      || left.suggestionId.localeCompare(right.suggestionId)
    ))
    .map((item) => item.suggestionId)
  for (const suggestionId of orderedIds) {
    const current = descriptorsOrConflict(state.doc)
      .find((item) => item.suggestionId === suggestionId)
    if (!current) continue
    const range = suggestionRange(state, suggestionId)
    if (!range || overlapsUnselectedSuggestion(state, range, selectedIds)) {
      throw decisionConflict()
    }
    if (current.kind === 'structure') {
      try {
        state = resolveStructureSuggestion(state, suggestionId, input.decision)
      } catch {
        throw decisionConflict()
      }
      continue
    }
    const markKinds = suggestionMarkKinds(state.doc, suggestionId)
    if (
      current.kind === 'format'
      && markKinds.size === 1
      && markKinds.has('modification')
    ) {
      state = resolveModification(state, suggestionId, input.decision)
      continue
    }
    if (
      current.kind === 'replacement'
      && !(
        markKinds.size === 2
        && markKinds.has('deletion')
        && markKinds.has('insertion')
      )
    ) {
      throw decisionConflict()
    }
    const command = input.decision === 'accept'
      ? applySuggestion(suggestionId, range.from, range.to)
      : revertSuggestion(suggestionId, range.from, range.to)
    // An accepted block break keeps the break, not its zero-width anchors.
    // They are located before the upstream command removes the marks that
    // identify them; removing them first would leave a suggestion that
    // consists of anchors only without a range to accept.
    const anchors = input.decision === 'accept'
      ? proposedBlockAnchorPositions(state.doc, (id) => id === suggestionId)
      : []
    // Assigned inside the command's dispatch callback.
    let applied = null as Transaction | null
    const handled = command(state, (transaction: Transaction) => {
      state = state.apply(transaction)
      applied = transaction
    })
    if (!handled || !applied) throw decisionConflict()
    if (anchors.length > 0) state = withoutAnchors(state, anchors, applied)
  }

  const remaining = descriptorsOrConflict(state.doc)
  if (remaining.some((item) => selectedIds.has(item.suggestionId))) throw decisionConflict()
  if (descriptorFingerprints(remaining, selectedIds) !== unselectedBefore) throw decisionConflict()

  // A block the decision writes anew (an accepted change of block type) holds
  // its empty text node when it has no content, in the same row.
  const since = Y.getState(document.store, document.clientID)
  updateYFragment(document, fragment, state.doc, initialized.meta)
  completeTextblocksCreatedSince(document, since)
  return {
    patchIds: [...patchIds].sort(),
    suggestions: selected
      .map((item) => ({ ...item }))
      .sort((left, right) => left.suggestionId.localeCompare(right.suggestionId)),
    suggestionIds: [...selectedIds].sort(),
  }
}

const ZERO_WIDTH_SPACE = '\u200B'

function withoutAnchors(
  state: EditorState,
  anchors: readonly number[],
  applied: Transaction,
): EditorState {
  const cleanup = state.tr
  const positions = anchors
    .map((position) => applied.mapping.mapResult(position, 1))
    .filter((result) => !result.deleted)
    .map((result) => result.pos)
    .sort((left, right) => right - left)
  for (const position of positions) {
    if (state.doc.textBetween(position, position + 1, '', '') === ZERO_WIDTH_SPACE) {
      cleanup.delete(position, position + 1)
    }
  }
  return cleanup.docChanged ? state.apply(cleanup) : state
}

function descriptorsOrConflict(document: ProseMirrorNode): SuggestionDescriptor[] {
  try {
    return suggestionDescriptors(document)
  } catch {
    throw decisionConflict()
  }
}

function suggestionMarkKinds(
  document: ProseMirrorNode,
  suggestionId: string,
): Set<string> {
  const kinds = new Set<string>()
  document.descendants((node) => {
    for (const mark of node.marks) {
      if ((mark.attrs.suggestionId ?? mark.attrs.id) === suggestionId) {
        kinds.add(mark.type.name)
      }
    }
  })
  return kinds
}

function resolveModification(
  state: EditorState,
  suggestionId: string,
  decision: 'accept' | 'reject',
): EditorState {
  const locations: Array<{ mark: Mark; node: ProseMirrorNode; position: number }> = []
  state.doc.descendants((node, position) => {
    for (const mark of node.marks) {
      const id = mark.attrs.suggestionId ?? mark.attrs.id
      if (mark.type.name === 'modification' && id === suggestionId) {
        locations.push({ mark, node, position })
      }
    }
  })
  if (locations.length === 0) throw decisionConflict()

  const transaction = state.tr
  for (const location of locations.sort((left, right) => right.position - left.position)) {
    const { mark, node, position } = location
    if (node.isInline) {
      transaction.removeMark(position, position + node.nodeSize, mark)
    } else {
      transaction.removeNodeMark(position, mark)
    }
    if (decision === 'accept') continue
    const type = mark.attrs.type
    if (type === 'attr' && typeof mark.attrs.attrName === 'string') {
      transaction.setNodeAttribute(position, mark.attrs.attrName, mark.attrs.previousValue)
      continue
    }
    if (type === 'nodeType' && typeof mark.attrs.previousValue === 'string') {
      const previousType = state.schema.nodes[mark.attrs.previousValue]
      if (!previousType) throw decisionConflict()
      transaction.setNodeMarkup(position, previousType)
      continue
    }
    if (type === 'mark') {
      if (mark.attrs.newValue && typeof mark.attrs.newValue === 'object') {
        const newMark = state.schema.markFromJSON(mark.attrs.newValue)
        if (node.isInline) {
          transaction.removeMark(position, position + node.nodeSize, newMark)
        } else {
          transaction.removeNodeMark(position, newMark)
        }
      }
      if (mark.attrs.previousValue && typeof mark.attrs.previousValue === 'object') {
        const previousMark = state.schema.markFromJSON(mark.attrs.previousValue)
        if (node.isInline) {
          transaction.addMark(position, position + node.nodeSize, previousMark)
        } else {
          transaction.addNodeMark(position, previousMark)
        }
      }
      continue
    }
    throw decisionConflict()
  }
  if (!transaction.steps.length) throw decisionConflict()
  return state.apply(transaction)
}

function suggestionRange(state: EditorState, suggestionId: string): SuggestionRange | null {
  let from = Number.POSITIVE_INFINITY
  let to = Number.NEGATIVE_INFINITY
  state.doc.descendants((node, position) => {
    const structure = node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR]
    const hasStructure = (
      isStructureSuggestionData(structure)
      && structure.suggestionId === suggestionId
    )
    if (!hasStructure && !node.marks.some((mark) => (
      mark.attrs.suggestionId === suggestionId || mark.attrs.id === suggestionId
    ))) return true
    from = Math.min(from, position)
    to = Math.max(to, position + node.nodeSize)
    return true
  })
  return Number.isFinite(from) && Number.isFinite(to) ? { from, to } : null
}

function overlapsUnselectedSuggestion(
  state: EditorState,
  selectedRange: SuggestionRange,
  selectedIds: ReadonlySet<string>,
): boolean {
  let overlaps = false
  state.doc.descendants((node, position) => {
    if (overlaps) return false
    const end = position + node.nodeSize
    if (end <= selectedRange.from || position >= selectedRange.to) return true
    for (const mark of node.marks) {
      const id = typeof mark.attrs.suggestionId === 'string'
        ? mark.attrs.suggestionId
        : mark.attrs.id
      if (typeof id === 'string' && !selectedIds.has(id)) {
        overlaps = true
        return false
      }
    }
    const structure = node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR]
    if (
      isStructureSuggestionData(structure)
      && !selectedIds.has(structure.suggestionId)
    ) {
      overlaps = true
      return false
    }
    return true
  })
  return overlaps
}

function descriptorFingerprints(
  descriptors: ReturnType<typeof suggestionDescriptors>,
  excluded: ReadonlySet<string>,
): string {
  return JSON.stringify(descriptors
    .filter((item) => !excluded.has(item.suggestionId))
    .sort((left, right) => left.suggestionId.localeCompare(right.suggestionId)))
}

function decisionConflict(): CollaborationError {
  return new CollaborationError('decision_conflict', {
    closeCode: CloseCodes.incompatible,
    httpStatus: 409,
  })
}
