import { getSchema, type JSONContent } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import {
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  createEditorSchemaExtensions,
  isStructureSuggestionData,
  projectFinalDocument,
  projectOriginalDocument,
  type CollaborationChangeKind,
  type SuggestionDescriptor,
  type SuggestionKind,
} from '@inqtrix/editor-schema'

export type BoundedChangeSummaryEdit = {
  after: string
  before: string
  kind: SuggestionKind | 'direct'
  position: number
}

export type BoundedChangeSummary = {
  /** Whether the change altered the document a reader sees: its text,
   * marks, attributes or structure. A change that did not, such as a
   * formatting cleanup that only removes redundant items, is stored like any
   * other; the activity history does not list it. */
  changesDocument: boolean
  edits: BoundedChangeSummaryEdit[]
  omittedEditCount: number
}

const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

export function summarizeEditorChange(input: {
  after: JSONContent
  before: JSONContent
  changeKind: CollaborationChangeKind
  decision: 'accept' | 'reject' | null
  suggestions: readonly SuggestionDescriptor[]
}): BoundedChangeSummary {
  const beforeDocument = schema.nodeFromJSON(input.before)
  const afterDocument = schema.nodeFromJSON(input.after)
  if (beforeDocument.eq(afterDocument)) return { changesDocument: false, edits: [], omittedEditCount: 0 }
  const structural = structureSummaryEdits(
    input.changeKind === 'decision' ? beforeDocument : afterDocument,
    input.suggestions,
  )
  const edits: BoundedChangeSummaryEdit[] = [...structural]
  const nonStructural = input.suggestions.filter((item) => item.kind !== 'structure')
  if (edits.length < 3 && (nonStructural.length > 0 || input.changeKind === 'direct')) {
    const comparison = comparisonDocuments(
      beforeDocument,
      afterDocument,
      input.changeKind,
      input.decision,
    )
    const textEdit = boundedTextDifference(
      comparison.before.textBetween(0, comparison.before.content.size, '\n', '\n'),
      comparison.after.textBetween(0, comparison.after.content.size, '\n', '\n'),
      nonStructural[0]?.kind ?? 'direct',
    )
    if (textEdit) edits.push(textEdit)
  }
  if (
    edits.length === 0
    && nonStructural.some((item) => item.kind === 'format')
  ) {
    edits.push({
      after: 'Updated formatting',
      before: 'Original formatting',
      kind: 'format',
      position: 0,
    })
  }
  const total = structural.length + (
    nonStructural.length > 0
      ? nonStructural.length
      : input.changeKind === 'direct'
        ? 1
        : 0
  )
  const visibleEdits = edits.slice(0, 3)
  return {
    changesDocument: true,
    edits: visibleEdits,
    omittedEditCount: Math.max(0, total - visibleEdits.length),
  }
}

function comparisonDocuments(
  before: ProseMirrorNode,
  after: ProseMirrorNode,
  changeKind: CollaborationChangeKind,
  decision: 'accept' | 'reject' | null,
): { after: ProseMirrorNode; before: ProseMirrorNode } {
  if (changeKind === 'suggestion') {
    return {
      after: projectFinalDocument(after),
      before: projectOriginalDocument(after),
    }
  }
  if (changeKind === 'decision') {
    return decision === 'accept'
      ? { after, before: projectOriginalDocument(before) }
      : { after: projectOriginalDocument(before), before: projectFinalDocument(before) }
  }
  return { after, before }
}

function structureSummaryEdits(
  document: ProseMirrorNode,
  suggestions: readonly SuggestionDescriptor[],
): BoundedChangeSummaryEdit[] {
  const selected = new Set(
    suggestions
      .filter((item) => item.kind === 'structure')
      .map((item) => item.suggestionId),
  )
  const edits: BoundedChangeSummaryEdit[] = []
  document.descendants((node, position) => {
    const data = node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR]
    if (!isStructureSuggestionData(data) || !selected.has(data.suggestionId)) return true
    edits.push({
      after: structureActionLabel(data.action),
      before: structureNodeLabel(node),
      kind: 'structure',
      position,
    })
    return true
  })
  return edits
}

function structureNodeLabel(node: ProseMirrorNode): string {
  if (node.type.name === 'heading') return `Heading ${String(node.attrs.level ?? '')}`.trim()
  if (node.type.name === 'codeBlock') return 'Code block'
  return 'Paragraph'
}

function structureActionLabel(action: string): string {
  const labels: Record<string, string> = {
    blockquote: 'Block quote',
    bulletList: 'Bulleted list',
    codeBlock: 'Code block',
    heading1: 'Heading 1',
    heading2: 'Heading 2',
    heading3: 'Heading 3',
    orderedList: 'Numbered list',
    paragraph: 'Paragraph',
    taskList: 'Task list',
  }
  return labels[action] ?? action
}

function boundedTextDifference(
  before: string,
  after: string,
  kind: SuggestionKind | 'direct',
): BoundedChangeSummaryEdit | null {
  if (before === after) return null
  let prefix = 0
  const maximumPrefix = Math.min(before.length, after.length)
  while (prefix < maximumPrefix && before[prefix] === after[prefix]) prefix += 1
  let beforeSuffix = before.length
  let afterSuffix = after.length
  while (
    beforeSuffix > prefix
    && afterSuffix > prefix
    && before[beforeSuffix - 1] === after[afterSuffix - 1]
  ) {
    beforeSuffix -= 1
    afterSuffix -= 1
  }
  return {
    after: boundedExcerpt(after.slice(prefix, afterSuffix)),
    before: boundedExcerpt(before.slice(prefix, beforeSuffix)),
    kind,
    position: prefix,
  }
}

/** A short, verbatim excerpt of the user's text for the change display.
 *
 * Verbatim is the promise here: `value` comes from `textBetween` and therefore
 * contains only text, never markup. A tag strip (`/<[^>]*>/g`) would remove
 * no markup but real content: `a<b>c` would become `ac`, `Map<K,V>` would
 * become `Map`, and the display would claim a different change than the one
 * the user made.
 *
 * The cut happens at codepoint boundaries. `slice` cuts at UTF-16 units and
 * can tear a surrogate pair; the lone surrogate that results is not valid
 * JSON, and Postgres rejects it on the jsonb write ("Unicode low surrogate
 * must follow a high surrogate"). An emoji at the 160-character boundary
 * would thus break the write, and with it the document. */
function boundedExcerpt(value: string): string {
  const clean = value
    .replace(/\s+/g, ' ')
    .trim()
  const codepoints = Array.from(clean)
  if (codepoints.length <= 160) return clean
  return `${codepoints.slice(0, 159).join('')}…`
}
