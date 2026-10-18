import { getSchema } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { splitListItem } from '@tiptap/pm/schema-list'
import { EditorState, TextSelection, type Transaction } from '@tiptap/pm/state'
import {
  EDITOR_YJS_FRAGMENT,
  INQTRIX_STRUCTURE_COMMAND_META,
  createEditorSchemaExtensions,
  editorJsonToYDoc,
  editorYDocToJson,
  parseEditorMarkdown,
  projectFinalJson,
  serializeEditorJson,
  suggestionDescriptors,
  transformToInqtrixSuggestionTransaction,
  validateEditorYDoc,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import { resolvePatchDecision } from '../src/decisionResolver'

const PATCH_ID = '22222222-2222-4222-8222-222222222222'
const SUGGESTION_ID = '33333333-3333-4333-8333-333333333333'
const SECOND_PATCH_ID = '44444444-4444-4444-8444-444444444444'
const SECOND_SUGGESTION_ID = '55555555-5555-4555-8555-555555555555'
const USER_ID = '11111111-1111-4111-8111-111111111111'
const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

describe('patch decision resolver', () => {
  it.each([
    ['text replacement', 'accept', 'World'],
    ['text replacement', 'reject', 'Hello'],
    ['formatting replacement', 'accept', '**Hello**'],
    ['formatting replacement', 'reject', 'Hello'],
  ] as const)('resolves a %s %s atomically', (change, decision, expected) => {
    const tracked = modification(change)
    const document = editorJsonToYDoc(tracked.toJSON())

    const result = resolvePatchDecision(document, {
      decision,
      patchIds: [PATCH_ID],
    })
    const resolved = schema.nodeFromJSON(editorYDocToJson(document))

    expect(serializeEditorJson(resolved.toJSON(), 'final').trim()).toBe(expected)
    expect(suggestionDescriptors(resolved)).toEqual([])
    expect(result).toMatchObject({
      patchIds: [PATCH_ID],
      suggestionIds: [SUGGESTION_ID],
      suggestions: [{
        kind: change === 'text replacement' ? 'replacement' : 'format',
        suggestionId: SUGGESTION_ID,
      }],
    })
    document.destroy()
  })

  it.each([
    ['accept', ''],
    ['reject', 'Hello'],
  ] as const)('resolves an inline deletion at the document end with %s', (
    decision,
    expected,
  ) => {
    const tracked = inlineDeletionAtDocumentEnd()
    const document = editorJsonToYDoc(tracked.toJSON())

    const result = resolvePatchDecision(document, {
      decision,
      patchIds: [PATCH_ID],
    })
    const resolved = schema.nodeFromJSON(editorYDocToJson(document))

    expect(serializeEditorJson(resolved.toJSON(), 'final').trim()).toBe(expected)
    expect(suggestionDescriptors(resolved)).toEqual([])
    expect(result).toMatchObject({
      patchIds: [PATCH_ID],
      suggestionIds: [SUGGESTION_ID],
      suggestions: [{ kind: 'deletion', suggestionId: SUGGESTION_ID }],
    })
    document.destroy()
  })

  // Accepting a proposed block type writes the block anew. An empty line
  // proposed as a heading becomes a heading without content, which holds its
  // one empty text node in the decision's own write, as every new textblock
  // does; a rejection keeps the line and the text node it had.
  it.each(['accept', 'reject'] as const)('leaves an empty line proposed as a heading with one empty text node on %s', (decision) => {
    const plain = schema.nodeFromJSON(parseEditorMarkdown('Oben.\n\n\n\nUnten.'))
    expect(plain.child(1).content.size).toBe(0)
    // The editor's slash command in suggest mode: the typed `/` is replaced by
    // the proposal of the block type.
    const author = { authorId: USER_ID, createdAt: 1_784_112_000, patchId: PATCH_ID }
    const line = plain.child(0).nodeSize + 1
    const empty = EditorState.create({ schema, doc: plain })
    const slashed = empty.apply(transformToInqtrixSuggestionTransaction(
      empty.tr.insertText('/', line),
      empty,
      author,
      () => SECOND_SUGGESTION_ID,
    ))
    const tracked = transformToInqtrixSuggestionTransaction(
      slashed.tr
        .delete(line, line + 1)
        .setNodeMarkup(line - 1, schema.nodes.heading, { level: 2 })
        .setMeta(INQTRIX_STRUCTURE_COMMAND_META, {
          action: 'heading2',
          commandRange: { from: line, to: line + 1 },
        }),
      slashed,
      author,
      () => SUGGESTION_ID,
    ).doc
    expect(suggestionDescriptors(tracked).map((item) => item.kind)).toEqual(['structure'])
    const document = editorJsonToYDoc(tracked.toJSON())

    resolvePatchDecision(document, { decision, patchIds: [PATCH_ID] })

    const resolved = document.getXmlFragment(EDITOR_YJS_FRAGMENT).get(1) as Y.XmlElement
    expect({
      children: resolved.toArray().map((child) => (
        child instanceof Y.XmlText
          ? `text:${Object.keys(child.getAttributes()).length}:${child.length}`
          : 'element'
      )),
      name: resolved.nodeName,
    }).toEqual({
      children: ['text:0:0'],
      name: decision === 'accept' ? 'heading' : 'paragraph',
    })
    expect(suggestionDescriptors(schema.nodeFromJSON(validateEditorYDoc(document)))).toEqual([])
    document.destroy()
  })

  it('rejects a partial modification pair without mutating the Y.Doc', () => {
    const tracked = modification('text replacement')
    const partialState = EditorState.create({ schema, doc: tracked })
    let insertionFrom: number | null = null
    let insertionTo: number | null = null
    tracked.descendants((node, position) => {
      if (node.marks.some((mark) => mark.type.name === 'insertion')) {
        insertionFrom = position
        insertionTo = position + node.nodeSize
      }
    })
    if (insertionFrom === null || insertionTo === null) {
      throw new Error('Modification fixture has no insertion half')
    }
    const partial = partialState.apply(partialState.tr.removeMark(
      insertionFrom,
      insertionTo,
      schema.marks.insertion,
    )).doc
    const document = editorJsonToYDoc(partial.toJSON())
    const before = Y.encodeStateAsUpdate(document)

    expect(() => resolvePatchDecision(document, {
      decision: 'accept',
      patchIds: [PATCH_ID],
    })).toThrowError('decision_conflict')
    expect(Y.encodeStateAsUpdate(document)).toEqual(before)
    document.destroy()
  })

  it('rejects a malformed modification pair without mutating the Y.Doc', () => {
    const tracked = modification('formatting replacement')
    const malformedState = EditorState.create({ schema, doc: tracked })
    const insertions: Array<{
      from: number
      mark: NonNullable<ProseMirrorNode['marks'][number]>
      to: number
    }> = []
    tracked.descendants((node, position) => {
      const mark = node.marks.find((candidate) => candidate.type.name === 'insertion')
      if (mark) insertions.push({ from: position, mark, to: position + node.nodeSize })
    })
    const insertion = insertions[0]
    if (!insertion) throw new Error('Modification fixture has no insertion half')
    const malformedMark = insertion.mark.type.create({
      ...insertion.mark.attrs,
      kind: 'insertion',
    })
    const malformed = malformedState.apply(
      malformedState.tr
        .removeMark(insertion.from, insertion.to, insertion.mark)
        .addMark(insertion.from, insertion.to, malformedMark),
    ).doc
    const document = editorJsonToYDoc(malformed.toJSON())
    const before = Y.encodeStateAsUpdate(document)

    expect(() => resolvePatchDecision(document, {
      decision: 'reject',
      patchIds: [PATCH_ID],
    })).toThrowError('decision_conflict')
    expect(Y.encodeStateAsUpdate(document)).toEqual(before)
    document.destroy()
  })

  it('rejects distant modification halves without mutating the Y.Doc', () => {
    const document = editorJsonToYDoc(distantModification().toJSON())
    const before = Y.encodeStateAsUpdate(document)

    expect(() => resolvePatchDecision(document, {
      decision: 'accept',
      patchIds: [PATCH_ID],
    })).toThrowError('decision_conflict')
    expect(Y.encodeStateAsUpdate(document)).toEqual(before)
    document.destroy()
  })

  it.each([
    [PATCH_ID, SECOND_SUGGESTION_ID],
    [SECOND_PATCH_ID, SUGGESTION_ID],
  ])('decides half-open adjacent patch %s without touching its neighbor', (
    selectedPatchId,
    remainingSuggestionId,
  ) => {
    const document = editorJsonToYDoc(adjacentInsertions().toJSON())

    resolvePatchDecision(document, {
      decision: 'accept',
      patchIds: [selectedPatchId],
    })

    expect(suggestionDescriptors(schema.nodeFromJSON(editorYDocToJson(document)))).toEqual([
      expect.objectContaining({ suggestionId: remainingSuggestionId }),
    ])
    document.destroy()
  })
})

describe('accepting a proposed block break', () => {
  // The zero-width spaces that anchor a proposed break are no content anyone
  // typed. Accepting keeps the break and drops them, also when later typing
  // left an anchor in the middle of the text; a zero-width space typed on
  // purpose stays, and rejecting restores the original document exactly.
  const MARKDOWN = 'First paragraph.\n\nSecond paragraph.'
  const cases: Array<[string, string, (state: EditorState) => EditorState, number]> = [
    ['Enter at the end of a paragraph', MARKDOWN, (state) => (
      proposeBreak(state, state.tr.split(blockEnd(state, 'First')))
    ), 0],
    ['Enter at the end of a paragraph, then typing', MARKDOWN, (state) => {
      const split = proposeBreak(state, state.tr.split(blockEnd(state, 'First')))
      return proposeBreak(split, split.tr.insertText('New', blockEnd(split, 'First') + 3))
    }, 0],
    ['Enter twice at the end of a paragraph', MARKDOWN, (state) => {
      const split = proposeBreak(state, state.tr.split(blockEnd(state, 'First')))
      return proposeBreak(split, split.tr.split(blockEnd(split, 'First') + 3))
    }, 0],
    ['Enter inside own inserted text', MARKDOWN, (state) => {
      const typed = proposeBreak(state, state.tr.insertText(' Added', blockEnd(state, 'First')))
      return proposeBreak(typed, typed.tr.split(blockEnd(typed, 'First') - 2))
    }, 0],
    ['Enter at the end of a list item, then typing', '- Item one\n- Item two\n\nAfter.', (state) => {
      const atEnd = state.apply(state.tr.setSelection(
        TextSelection.create(state.doc, blockEnd(state, 'Item two')),
      ))
      const listItem = schema.nodes.listItem
      if (!listItem) throw new Error('Editor schema has no list item')
      let split: Transaction | null = null
      splitListItem(listItem)(atEnd, (transaction) => {
        split = transaction
      })
      if (!split) throw new Error('The list item could not be split')
      const proposed = proposeBreak(atEnd, split)
      return proposeBreak(proposed, proposed.tr.insertText('Item', blockEnd(proposed, 'Item two') + 5))
    }, 0],
    ['Enter, then typing at the end of the original paragraph', MARKDOWN, (state) => {
      const split = proposeBreak(state, state.tr.split(blockEnd(state, 'First')))
      return proposeBreak(split, split.tr.insertText('abc', blockEnd(split, 'First')))
    }, 0],
    ['Enter, then typing at the start of the new paragraph before its anchor', MARKDOWN, (state) => {
      const split = proposeBreak(state, state.tr.split(blockEnd(state, 'First')))
      return proposeBreak(split, split.tr.insertText('xy', blockEnd(split, 'First') + 2))
    }, 0],
    ['a zero-width space typed on purpose inside the new paragraph', MARKDOWN, (state) => {
      const split = proposeBreak(state, state.tr.split(blockEnd(state, 'First')))
      return proposeBreak(split, split.tr.insertText('Ne\u200Bw', blockEnd(split, 'First') + 3))
    }, 1],
  ]

  it.each(cases)('keeps no anchor: %s', (_name, markdown, build, typedOnPurpose) => {
    const original = EditorState.create({
      doc: schema.nodeFromJSON(parseEditorMarkdown(markdown)),
      schema,
    })
    const proposed = build(original)
    const patchIds = [...new Set(suggestionDescriptors(proposed.doc).map((item) => item.patchId))]

    const accepted = editorJsonToYDoc(proposed.doc.toJSON())
    resolvePatchDecision(accepted, { decision: 'accept', patchIds })
    const acceptedJson = editorYDocToJson(accepted)
    expect(zeroWidthSpaces(JSON.stringify(acceptedJson))).toBe(typedOnPurpose)
    expect(acceptedJson).toEqual(schema.nodeFromJSON(projectFinalJson(proposed.doc)).toJSON())
    accepted.destroy()

    const rejected = editorJsonToYDoc(proposed.doc.toJSON())
    resolvePatchDecision(rejected, { decision: 'reject', patchIds })
    expect(editorYDocToJson(rejected)).toEqual(original.doc.toJSON())
    rejected.destroy()
  })

  it('keeps the anchors of another pending break when accepting one', () => {
    const original = EditorState.create({
      doc: schema.nodeFromJSON(parseEditorMarkdown(MARKDOWN)),
      schema,
    })
    const first = proposeBreak(original, original.tr.split(blockEnd(original, 'First')))
    const both = proposeBreak(first, first.tr.split(blockEnd(first, 'Second')), SECOND_PATCH_ID)
    const document = editorJsonToYDoc(both.doc.toJSON())

    resolvePatchDecision(document, { decision: 'accept', patchIds: [PATCH_ID] })
    const json = editorYDocToJson(document)
    expect(zeroWidthSpaces(JSON.stringify(json))).toBe(2)
    expect(suggestionDescriptors(schema.nodeFromJSON(json))).toEqual([
      expect.objectContaining({ patchId: SECOND_PATCH_ID }),
    ])
    document.destroy()
  })
})

let proposedSuggestion = 0

function proposeBreak(
  state: EditorState,
  transaction: Transaction,
  patchId = PATCH_ID,
): EditorState {
  return state.apply(transformToInqtrixSuggestionTransaction(
    transaction,
    state,
    { authorId: USER_ID, createdAt: 1_784_112_020, patchId },
    () => {
      proposedSuggestion += 1
      return `77777777-7777-4777-8777-${String(proposedSuggestion).padStart(12, '0')}`
    },
  ))
}

function blockEnd(state: EditorState, needle: string): number {
  let found = -1
  state.doc.descendants((node, position) => {
    if (found >= 0) return false
    if (node.isTextblock && node.textContent.includes(needle)) {
      found = position + 1 + node.content.size
      return false
    }
    return true
  })
  if (found < 0) throw new Error(`Fixture has no text block containing ${needle}`)
  return found
}

function zeroWidthSpaces(text: string): number {
  return text.split('\u200B').length - 1
}

function modification(
  change: 'formatting replacement' | 'text replacement',
): ProseMirrorNode {
  const document = schema.nodeFromJSON(parseEditorMarkdown('Hello'))
  const state = EditorState.create({ schema, doc: document })
  const bold = schema.marks.bold
  if (!bold) throw new Error('Editor schema has no bold mark')
  const transaction = change === 'text replacement'
    ? state.tr.insertText('World', 1, 6)
    : state.tr.addMark(1, 6, bold.create())
  return state.apply(transformToInqtrixSuggestionTransaction(
    transaction,
    state,
    { authorId: USER_ID, createdAt: 1_784_112_000, patchId: PATCH_ID },
    () => SUGGESTION_ID,
  )).doc
}

function inlineDeletionAtDocumentEnd(): ProseMirrorNode {
  const document = schema.nodeFromJSON(parseEditorMarkdown('Hello'))
  const state = EditorState.create({ schema, doc: document })
  return state.apply(transformToInqtrixSuggestionTransaction(
    state.tr.delete(1, 6),
    state,
    { authorId: USER_ID, createdAt: 1_784_112_002, patchId: PATCH_ID },
    () => SUGGESTION_ID,
  )).doc
}

function distantModification(): ProseMirrorNode {
  const deletion = schema.marks.deletion
  const insertion = schema.marks.insertion
  if (!deletion || !insertion) throw new Error('Editor schema has no suggestion marks')
  const metadata = {
    authorId: USER_ID,
    createdAt: 1_784_112_001,
    id: SUGGESTION_ID,
    kind: 'modification',
    patchId: PATCH_ID,
    suggestionId: SUGGESTION_ID,
  }
  return schema.node('doc', null, [schema.node('paragraph', null, [
    schema.text('Before', [deletion.create(metadata)]),
    schema.text(' unrelated '),
    schema.text('After', [insertion.create(metadata)]),
  ])])
}

function adjacentInsertions(): ProseMirrorNode {
  const insertion = schema.marks.insertion
  if (!insertion) throw new Error('Editor schema has no insertion mark')
  const mark = (suggestionId: string, patchId: string, createdAt: number) => insertion.create({
    authorId: USER_ID,
    createdAt,
    id: suggestionId,
    kind: 'insertion',
    patchId,
    suggestionId,
  })
  return schema.node('doc', null, [schema.node('paragraph', null, [
    schema.text('A', [mark(SUGGESTION_ID, PATCH_ID, 1_784_112_010)]),
    schema.text('B', [mark(SECOND_SUGGESTION_ID, SECOND_PATCH_ID, 1_784_112_011)]),
  ])])
}
