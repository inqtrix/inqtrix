import { getSchema } from '@tiptap/core'
import { EditorState } from '@tiptap/pm/state'
import {
  createEditorSchemaExtensions,
  parseEditorMarkdown,
  suggestionDescriptors,
  transformToInqtrixSuggestionTransaction,
  type SuggestionDescriptor,
} from '@inqtrix/editor-schema'

import { summarizeEditorChange } from '../src/changeSummary'

const AUTHOR_ID = '11111111-1111-4111-8111-111111111111'
const PATCH_ID = '22222222-2222-4222-8222-222222222222'

describe('bounded collaboration change summaries', () => {
  it('shows a proposed paragraph without the anchors of its block break', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const before = schema.nodeFromJSON(parseEditorMarkdown('First paragraph.\n\nSecond.'))
    const initial = EditorState.create({ doc: before, schema })
    const end = before.child(0).nodeSize - 1
    let next = 0
    const ids = () => `33333333-3333-4333-8333-${String(++next).padStart(12, '0')}`
    const metadata = { authorId: AUTHOR_ID, createdAt: 1_784_112_000, patchId: PATCH_ID }
    const split = initial.apply(transformToInqtrixSuggestionTransaction(
      initial.tr.split(end),
      initial,
      metadata,
      ids,
    ))
    const typed = split.apply(transformToInqtrixSuggestionTransaction(
      split.tr.insertText('Added', end + 3),
      split,
      metadata,
      ids,
    ))

    const summary = summarizeEditorChange({
      after: typed.doc.toJSON(),
      before: before.toJSON(),
      changeKind: 'suggestion',
      decision: null,
      suggestions: suggestionDescriptors(typed.doc),
    })

    expect(summary.edits).toHaveLength(1)
    expect(summary.edits[0]?.after).toBe('Added')
    expect(summary.edits[0]?.after).not.toContain('\u200B')
  })

  // The string comes from textBetween and is plain text. An excerpt that
  // removes what looks like a tag therefore removes user content, and the
  // change display would show something other than the document. The
  // summary reproduces; it does not sanitize.
  it('reproduces typed text literally, also when it looks like markup', () => {
    const summary = summarizeEditorChange({
      before: parseEditorMarkdown('Hello'),
      after: parseEditorMarkdown('Hello <script>unsafe</script> world'),
      changeKind: 'direct',
      decision: null,
      suggestions: [],
    })

    expect(summary).toEqual({
      changesDocument: true,
      edits: [{
        after: '<script>unsafe</script> world',
        before: '',
        kind: 'direct',
        position: 5,
      }],
      omittedEditCount: 0,
    })
  })

  // A single angle bracket, as typing code or a comparison produces it. A
  // summary that refused it would refuse the write and lock the document for
  // everyone in the room.
  it('passes a single angle bracket on unchanged', () => {
    const summary = summarizeEditorChange({
      before: parseEditorMarkdown('if (a'),
      after: parseEditorMarkdown('if (a < b) return x > y'),
      changeKind: 'direct',
      decision: null,
      suggestions: [],
    })

    expect(summary.edits[0]?.after).toBe('< b) return x > y')
  })

  // An emoji exactly on the truncation boundary: an excerpt cut at a UTF-16
  // unit leaves a lone surrogate. That is not valid JSON, and Postgres
  // refuses the jsonb write ("Unicode low surrogate must follow a high
  // surrogate"), which refuses the whole update.
  it('truncates at code point boundaries and never splits a surrogate pair', () => {
    const summary = summarizeEditorChange({
      before: parseEditorMarkdown('x'),
      after: parseEditorMarkdown(`x${'a'.repeat(158)}\u{1F600}bbb`),
      changeKind: 'direct',
      decision: null,
      suggestions: [],
    })

    const excerpt = summary.edits[0]?.after ?? ''
    const split = Array.from(excerpt).some((character) => {
      const codePoint = character.codePointAt(0) ?? 0
      return codePoint >= 0xd800 && codePoint <= 0xdfff
    })
    expect(split).toBe(false)
    expect(JSON.parse(JSON.stringify({ excerpt })).excerpt).toBe(excerpt)
  })

  it('limits excerpts to 160 characters', () => {
    const summary = summarizeEditorChange({
      before: parseEditorMarkdown('Before'),
      after: parseEditorMarkdown('x'.repeat(240)),
      changeKind: 'direct',
      decision: null,
      suggestions: [],
    })

    expect(summary.edits).toHaveLength(1)
    expect(summary.edits[0]?.after).toHaveLength(160)
    expect(summary.edits[0]?.after.endsWith('…')).toBe(true)
  })

  // A formatting cleanup a writer sends after a reconnect, or a second
  // writer's bold over text another writer bolded a moment earlier, is stored
  // like any other change, but the reader sees the same document before and
  // after. The summary says so, and names no edit.
  it.each(['direct', 'suggestion'] as const)('says a %s change that leaves the document as it was changes nothing, and names no edit', (changeKind) => {
    const summary = summarizeEditorChange({
      after: parseEditorMarkdown('Hello **world**'),
      before: parseEditorMarkdown('Hello **world**'),
      changeKind,
      decision: null,
      suggestions: [],
    })

    expect(summary).toEqual({ changesDocument: false, edits: [], omittedEditCount: 0 })
  })

  // Formatting leaves the text as it was; it still changes what the reader
  // sees, and the history keeps listing it.
  it('says a formatting-only edit changes the document, and counts it as one edit', () => {
    const summary = summarizeEditorChange({
      after: parseEditorMarkdown('**Hello** world'),
      before: parseEditorMarkdown('Hello world'),
      changeKind: 'direct',
      decision: null,
      suggestions: [],
    })

    expect(summary).toEqual({ changesDocument: true, edits: [], omittedEditCount: 1 })
  })

  it('counts non-visible suggestions while emitting at most three edits', () => {
    const suggestions = Array.from({ length: 5 }, (_, index) => descriptor(index))
    const summary = summarizeEditorChange({
      before: parseEditorMarkdown('Before'),
      after: parseEditorMarkdown('After'),
      changeKind: 'suggestion',
      decision: null,
      suggestions,
    })

    expect(summary.edits.length).toBeLessThanOrEqual(3)
    expect(summary.omittedEditCount).toBe(4)
  })
})

function descriptor(index: number): SuggestionDescriptor {
  return {
    authorId: AUTHOR_ID,
    createdAt: 1_784_112_000 + index,
    kind: index % 2 === 0 ? 'replacement' : 'format',
    patchId: PATCH_ID,
    suggestionId: `33333333-3333-4333-8333-${String(index).padStart(12, '0')}`,
  }
}
