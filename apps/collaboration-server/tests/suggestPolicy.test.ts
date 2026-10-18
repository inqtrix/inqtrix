import { getSchema, type JSONContent } from '@tiptap/core'
import { Fragment, Slice, type Node as ProseMirrorNode } from '@tiptap/pm/model'
import { EditorState, TextSelection, type Transaction } from '@tiptap/pm/state'
import { findWrapping } from '@tiptap/pm/transform'
import { initProseMirrorDoc, updateYFragment } from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  INQTRIX_STRUCTURE_COMMAND_META,
  createEditorSchemaExtensions,
  editorJsonToYDoc,
  parseEditorMarkdown,
  projectOriginalDocument,
  transformToInqtrixSuggestionTransaction,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import { CollaborationError } from '../src/errors'
import {
  collectSuggestionRecords,
  createYjsSuggestionPositionMapping,
  needsPositionMapper,
  sameSuggestionRecord,
  validateSuggestionUpdate,
} from '../src/suggestPolicy'
import { USER_ID } from './helpers'

const PATCH_ID = '22222222-2222-4222-8222-222222222222'
const SUGGESTION_ID = '33333333-3333-4333-8333-333333333333'
const OTHER_USER_ID = '44444444-4444-4444-8444-444444444444'
const STRUCTURE_SUGGESTION_ID = '55555555-5555-4555-8555-555555555555'
const CREATED_AT = 1_784_112_000
const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

describe('position mapper necessity', () => {
  // The mapper costs two initProseMirrorDoc conversions and two full
  // document parses. On a large document that is the single most
  // expensive part of policy validation — and on a document without a
  // single suggestion it answers a question nobody asked.
  it('is unnecessary when neither side carries a suggestion', () => {
    const plain = parseEditorMarkdown('Hello')

    expect(needsPositionMapper(plain, plain)).toBe(false)
  })

  it('is required as soon as either side carries a suggestion', () => {
    const before = schema.nodeFromJSON(parseEditorMarkdown('Hello'))
    const state = EditorState.create({ schema, doc: before })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.setSelection(TextSelection.create(state.doc, 6)).insertText('!'),
      state,
      { authorId: USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc

    // Both directions: adding one and removing one.
    expect(needsPositionMapper(before.toJSON(), tracked.toJSON())).toBe(true)
    expect(needsPositionMapper(tracked.toJSON(), before.toJSON())).toBe(true)
  })
})

describe('suggestion policy', () => {
  it('emits authoritative active patch state for a new suggestion', () => {
    const before = schema.nodeFromJSON(parseEditorMarkdown('Hello'))
    const state = EditorState.create({ schema, doc: before })
    const direct = state.tr
      .setSelection(TextSelection.create(state.doc, 6))
      .insertText('!')
    const tracked = transformToInqtrixSuggestionTransaction(
      direct,
      state,
      { authorId: USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc

    const result = validateMappedSuggestionUpdate(
      before.toJSON(),
      tracked.toJSON(),
      'suggest',
      USER_ID,
    )

    expect(result.changeKind).toBe('suggestion')
    expect(result.suggestionIds).toEqual([SUGGESTION_ID])
    expect(result.patches).toEqual([{
      activeSuggestionIds: [SUGGESTION_ID],
      authorId: USER_ID,
      createdAt: CREATED_AT,
      kinds: ['insertion'],
      patchId: PATCH_ID,
      supersededSuggestionIds: [],
    }])
  })

  it('allows a marked text suggestion to create the Yjs text type in an empty paragraph', () => {
    const before = schema.node('doc', null, [schema.node('paragraph')])
    const state = EditorState.create({ schema, doc: before })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('A', 1),
      state,
      { authorId: USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc

    const result = validateMappedSuggestionUpdate(
      before.toJSON(),
      tracked.toJSON(),
      'suggest',
      USER_ID,
    )

    expect(result.changeKind).toBe('suggestion')
    expect(result.suggestionIds).toEqual([SUGGESTION_ID])
  })

  it('accepts an actor-owned slash insertion superseded by one structure suggestion', () => {
    const before = schema.node('doc', null, [
      schema.node('heading', { level: 1, textAlign: null }, schema.text('Title')),
    ])
    const initial = EditorState.create({ schema, doc: before })
    const trackedSlash = initial.apply(transformToInqtrixSuggestionTransaction(
      initial.tr.insertText('/', 1),
      initial,
      { authorId: USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    )).doc
    const slashState = EditorState.create({ schema, doc: trackedSlash })
    const direct = slashState.tr
      .delete(1, 2)
      .setNodeMarkup(0, schema.nodes.heading, { level: 2, textAlign: null })
      .setMeta(INQTRIX_STRUCTURE_COMMAND_META, {
        action: 'heading2',
        commandRange: { from: 1, to: 2 },
      })
    const trackedStructure = slashState.apply(
      transformToInqtrixSuggestionTransaction(
        direct,
        slashState,
        {
          authorId: USER_ID,
          createdAt: CREATED_AT + 1,
          patchId: '66666666-6666-4666-8666-666666666666',
        },
        () => STRUCTURE_SUGGESTION_ID,
      ),
    ).doc

    const result = validateMappedSuggestionUpdate(
      trackedSlash.toJSON(),
      trackedStructure.toJSON(),
      'suggest',
      USER_ID,
    )

    expect(result.suggestionIds).toEqual([
      SUGGESTION_ID,
      STRUCTURE_SUGGESTION_ID,
    ])
    expect(result.patches).toEqual([{
      activeSuggestionIds: [STRUCTURE_SUGGESTION_ID],
      authorId: USER_ID,
      createdAt: CREATED_AT,
      kinds: ['structure'],
      patchId: PATCH_ID,
      supersededSuggestionIds: [SUGGESTION_ID],
    }])
  })

  it('rejects removal of the last active suggestion outside an explicit decision', () => {
    const plain = schema.nodeFromJSON(parseEditorMarkdown('Hello'))
    const state = EditorState.create({ schema, doc: plain })
    const direct = state.tr
      .setSelection(TextSelection.create(state.doc, 6))
      .insertText('!')
    const tracked = transformToInqtrixSuggestionTransaction(
      direct,
      state,
      { authorId: USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc

    expect(() => validateSuggestionUpdate(
      tracked.toJSON(),
      plain.toJSON(),
      'suggest',
      USER_ID,
    )).toThrowError('suggestion_policy_violation')
  })

  it('recognizes a replacement as one replacement descriptor', () => {
    const before = schema.nodeFromJSON(parseEditorMarkdown('Hello'))
    const state = EditorState.create({ schema, doc: before })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('World', 1, 6),
      state,
      { authorId: USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc

    const result = validateMappedSuggestionUpdate(
      before.toJSON(),
      tracked.toJSON(),
      'suggest',
      USER_ID,
    )

    expect(result.suggestions).toEqual([expect.objectContaining({
      kind: 'replacement',
      suggestionId: SUGGESTION_ID,
    })])
    expect(result.patches[0]?.kinds).toEqual(['replacement'])
  })

  it('rejects a newly injected suggestion attributed to another user', () => {
    const before = schema.nodeFromJSON(parseEditorMarkdown('Hello'))
    const state = EditorState.create({ schema, doc: before })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', 6),
      state,
      { authorId: OTHER_USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc

    expect(() => validateSuggestionUpdate(
      before.toJSON(),
      tracked.toJSON(),
      'suggest',
      USER_ID,
    )).toThrowError('suggestion_policy_violation')
  })

  it('allows a direct edit before an unchanged foreign suggestion', () => {
    const before = schema.nodeFromJSON(parseEditorMarkdown('First\n\nSecond'))
    const state = EditorState.create({ schema, doc: before })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', state.doc.content.size - 1),
      state,
      { authorId: OTHER_USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc
    const trackedState = EditorState.create({ schema, doc: tracked })
    const after = trackedState.apply(trackedState.tr.insertText('Prefix ', 1)).doc

    const result = validateMappedSuggestionUpdate(
      tracked.toJSON(),
      after.toJSON(),
      'edit',
      USER_ID,
    )

    expect(result.changeKind).toBe('direct')
    expect(result.suggestionIds).toEqual([])
  })

  it('allows a same-paragraph direct prefix before an unchanged foreign suggestion', () => {
    const before = schema.nodeFromJSON(parseEditorMarkdown('Hello'))
    const state = EditorState.create({ schema, doc: before })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', state.doc.content.size - 1),
      state,
      { authorId: OTHER_USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc
    const trackedState = EditorState.create({ schema, doc: tracked })
    const after = trackedState.apply(trackedState.tr.insertText('Prefix ', 1)).doc

    const result = validateMappedSuggestionUpdate(
      tracked.toJSON(),
      after.toJSON(),
      'edit',
      USER_ID,
    )

    expect(result.changeKind).toBe('direct')
    expect(result.suggestionIds).toEqual([])
  })

  it('fails closed when a foreign occurrence has no shared-document position mapping', () => {
    const tracked = trackedInsertion(parseEditorMarkdown('abcabc'), OTHER_USER_ID, 3)

    expect(() => validateSuggestionUpdate(
      tracked.toJSON(),
      tracked.toJSON(),
      'edit',
      USER_ID,
    )).toThrowError('suggestion_policy_violation')
  })

  it('allows an unrelated structural insertion before a foreign suggestion', () => {
    const before = schema.nodeFromJSON(parseEditorMarkdown('First\n\nSecond'))
    const state = EditorState.create({ schema, doc: before })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', state.doc.content.size - 1),
      state,
      { authorId: OTHER_USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc.toJSON()
    const after = structuredClone(tracked)
    const inserted = parseEditorMarkdown('Inserted').content?.[0]
    if (!after.content || !inserted) throw new Error('structural fixture is invalid')
    after.content.unshift(inserted)

    const result = validateMappedSuggestionUpdate(tracked, after, 'edit', USER_ID)

    expect(result.changeKind).toBe('direct')
    expect(result.suggestionIds).toEqual([])
  })

  it('rejects relocation of a foreign insertion to another structural parent', () => {
    const before = schema.nodeFromJSON(parseEditorMarkdown('First\n\nSecond'))
    const state = EditorState.create({ schema, doc: before })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', state.doc.content.size - 1),
      state,
      { authorId: OTHER_USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc.toJSON()
    const after = structuredClone(tracked)
    const first = after.content?.[0]
    const second = after.content?.[1]
    const foreignIndex = second?.content?.findIndex((node: JSONContent) => (
      node.marks?.some((mark: NonNullable<JSONContent['marks']>[number]) => (
        mark.attrs?.suggestionId === SUGGESTION_ID
      ))
    )) ?? -1
    if (!first || !second?.content || foreignIndex < 0) {
      throw new Error('foreign suggestion fixture is invalid')
    }
    const [foreign] = second.content.splice(foreignIndex, 1)
    if (!foreign) throw new Error('foreign suggestion fixture is invalid')
    first.content = [...(first.content ?? []), foreign]

    expect(() => validateMappedSuggestionUpdate(
      tracked,
      after,
      'edit',
      USER_ID,
    )).toThrowError('suggestion_policy_violation')
  })

  it('rejects relocation between structurally identical sibling blocks', () => {
    const before = schema.nodeFromJSON(parseEditorMarkdown('Same\n\nSame'))
    const state = EditorState.create({ schema, doc: before })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', state.doc.child(0).nodeSize - 1),
      state,
      { authorId: OTHER_USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc.toJSON()
    const after = structuredClone(tracked)
    const first = after.content?.[0]
    const second = after.content?.[1]
    const foreignIndex = first?.content?.findIndex((node: JSONContent) => (
      node.marks?.some((mark: NonNullable<JSONContent['marks']>[number]) => (
        mark.attrs?.suggestionId === SUGGESTION_ID
      ))
    )) ?? -1
    if (!first?.content || !second || foreignIndex < 0) {
      throw new Error('identical sibling fixture is invalid')
    }
    const [foreign] = first.content.splice(foreignIndex, 1)
    if (!foreign) throw new Error('identical sibling fixture is invalid')
    second.content = [...(second.content ?? []), foreign]

    expect(() => validateMappedSuggestionUpdate(
      tracked,
      after,
      'edit',
      USER_ID,
    )).toThrowError('suggestion_policy_violation')
  })

  it('rejects relocation of a foreign insertion within its structural parent', () => {
    const before = schema.nodeFromJSON(parseEditorMarkdown('abc'))
    const state = EditorState.create({ schema, doc: before })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('X', 2),
      state,
      { authorId: OTHER_USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc.toJSON()
    const after = structuredClone(tracked)
    const paragraph = after.content?.[0]
    const foreign = paragraph?.content?.find((node: JSONContent) => (
      node.marks?.some((mark: NonNullable<JSONContent['marks']>[number]) => (
        mark.attrs?.suggestionId === SUGGESTION_ID
      ))
    ))
    if (!paragraph || !foreign) throw new Error('foreign suggestion fixture is invalid')
    paragraph.content = [
      { text: 'ab', type: 'text' },
      foreign,
      { text: 'c', type: 'text' },
    ]

    expect(() => validateMappedSuggestionUpdate(
      tracked,
      after,
      'edit',
      USER_ID,
    )).toThrowError('suggestion_policy_violation')
  })

  it('rejects relocation from abc[X]abc to abcabc[X]', () => {
    const tracked = trackedInsertion(parseEditorMarkdown('abcabc'), OTHER_USER_ID, 3)
    const after = relocateSuggestionToParentEnd(tracked.toJSON())

    expect(() => validateMappedSuggestionUpdate(
      tracked.toJSON(),
      after,
      'edit',
      USER_ID,
    )).toThrowError('suggestion_policy_violation')
  })

  it('rejects repeated-text relocation when multiple textual anchors look identical', () => {
    const tracked = trackedInsertion(parseEditorMarkdown('abcabcabc'), OTHER_USER_ID, 3)
    const after = relocateSuggestionToParentEnd(tracked.toJSON())

    expect(() => validateMappedSuggestionUpdate(
      tracked.toJSON(),
      after,
      'edit',
      USER_ID,
    )).toThrowError('suggestion_policy_violation')
  })

  it('rejects relocation inside nested block content', () => {
    const tracked = trackedInsertion(parseEditorMarkdown('> abcabc'), OTHER_USER_ID, 3)
    const after = relocateSuggestionToParentEnd(tracked.toJSON())

    expect(() => validateMappedSuggestionUpdate(
      tracked.toJSON(),
      after,
      'edit',
      USER_ID,
    )).toThrowError('suggestion_policy_violation')
  })

  it('rejects relocation inside a table cell', () => {
    const tracked = trackedInsertion(
      parseEditorMarkdown('| Value |\n| --- |\n| abcabc |'),
      OTHER_USER_ID,
      3,
    )
    const after = relocateSuggestionToParentEnd(tracked.toJSON())

    expect(() => validateMappedSuggestionUpdate(
      tracked.toJSON(),
      after,
      'edit',
      USER_ID,
    )).toThrowError('suggestion_policy_violation')
  })

  it('allows an author to edit the content of their own active suggestion', () => {
    const tracked = trackedInsertion(parseEditorMarkdown('abcabc'), USER_ID, 3)
    const after = structuredClone(tracked.toJSON())
    const occurrence = findSuggestionNode(after)
    if (!occurrence) throw new Error('own suggestion fixture is invalid')
    occurrence.text = `${occurrence.text ?? ''}Y`

    const result = validateMappedSuggestionUpdate(
      tracked.toJSON(),
      after,
      'suggest',
      USER_ID,
    )

    expect(result.changeKind).toBe('suggestion')
    expect(result.suggestionIds).toEqual([SUGGESTION_ID])
  })

  it('rejects a mixed direct and suggestion update from an edit user', () => {
    const before = schema.nodeFromJSON(parseEditorMarkdown('First\n\nSecond'))
    const initial = EditorState.create({ schema, doc: before })
    const directState = initial.apply(initial.tr.insertText('Prefix ', 1))
    const mixed = transformToInqtrixSuggestionTransaction(
      directState.tr.insertText('!', directState.doc.content.size - 1),
      directState,
      { authorId: USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc

    expect(() => validateSuggestionUpdate(
      before.toJSON(),
      mixed.toJSON(),
      'edit',
      USER_ID,
    )).toThrowError('suggestion_policy_violation')
  })

  it.each([
    ['table topology', (document: JSONContent) => {
      const table = document.content?.[0]
      const firstRow = table?.content?.[0]
      if (!table?.content || !firstRow) throw new Error('table fixture is invalid')
      table.content.push(structuredClone(firstRow))
    }],
    ['table cell attributes', (document: JSONContent) => {
      const cell = document.content?.[0]?.content?.[0]?.content?.[0]
      if (!cell) throw new Error('table fixture is invalid')
      cell.attrs = { ...cell.attrs, colspan: 2, colwidth: [160, 160] }
    }],
    ['paragraph attributes', (document: JSONContent) => {
      const paragraph = document.content?.[0]
      if (!paragraph) throw new Error('paragraph fixture is invalid')
      paragraph.attrs = { ...paragraph.attrs, textAlign: 'right' }
    }],
  ])('rejects non-reversible %s changes hidden from suggestion marks', (_label, mutate) => {
    const source = _label.startsWith('table')
      ? '| A | B |\n| --- | --- |\n| one | two |'
      : 'Unchanged text'
    const before = parseEditorMarkdown(source)
    const after = structuredClone(before)
    mutate(after)

    expect(() => validateSuggestionUpdate(
      before,
      after,
      'suggest',
      USER_ID,
    )).toThrowError('suggestion_policy_violation')
  })

  it('rejects a tracked math atom replacement even when its original projection is intact', () => {
    const inlineMath = schema.nodes.inlineMath
    const deletion = schema.marks.deletion
    const insertion = schema.marks.insertion
    if (!inlineMath || !deletion || !insertion) throw new Error('math fixture is invalid')
    const metadata = {
      authorId: USER_ID,
      createdAt: CREATED_AT,
      id: SUGGESTION_ID,
      patchId: PATCH_ID,
      suggestionId: SUGGESTION_ID,
    }
    const before = schema.node('doc', null, [schema.node('paragraph', null, [
      schema.text('Equation: '),
      inlineMath.create({ latex: 'x + 1' }),
      schema.text('.'),
    ])])
    const after = schema.node('doc', null, [schema.node('paragraph', null, [
      schema.text('Equation: '),
      inlineMath.create(
        { latex: 'x + 1' },
        null,
        [deletion.create({ ...metadata, kind: 'modification' })],
      ),
      inlineMath.create(
        { latex: 'x + 2' },
        null,
        [insertion.create({ ...metadata, kind: 'modification' })],
      ),
      schema.text('.'),
    ])])

    expect(projectOriginalDocument(after).eq(before)).toBe(true)

    expect(() => validateSuggestionUpdate(
      before.toJSON(),
      after.toJSON(),
      'suggest',
      USER_ID,
    )).toThrowError('suggestion_policy_violation')
  })
})

describe('the deletions of a suggest-only update', () => {
  // The rule reads what the update deleted. Without that, a suggest-only
  // update cannot be judged and is refused, never waved through.
  it('are refused when the caller does not say what the update deleted', () => {
    const before = schema.nodeFromJSON(parseEditorMarkdown('Hello'))
    const state = EditorState.create({ schema, doc: before })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', 6),
      state,
      { authorId: USER_ID, createdAt: CREATED_AT, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc
    const beforeDocument = editorJsonToYDoc(before.toJSON())
    const afterDocument = new Y.Doc()
    Y.applyUpdate(afterDocument, Y.encodeStateAsUpdate(beforeDocument))
    const fragment = afterDocument.getXmlFragment(EDITOR_YJS_FRAGMENT)
    updateYFragment(afterDocument, fragment, tracked, initProseMirrorDoc(fragment, schema).meta)

    expect(() => validateSuggestionUpdate(before.toJSON(), tracked.toJSON(), 'suggest', USER_ID, {
      afterDocument,
      beforeDocument,
    })).toThrowError('suggestion_policy_violation')
    expect(validateSuggestionUpdate(before.toJSON(), tracked.toJSON(), 'edit', USER_ID, {
      afterDocument,
      beforeDocument,
    }).changeKind).toBe('suggestion')
    afterDocument.destroy()
    beforeDocument.destroy()
  })
})

describe('an edit lease that writes text again in place', () => {
  // y-tiptap writes a split, a join, a block type change, a list wrap or a
  // paste as the deletion of the moved text and the insertion of a copy. A
  // pending suggestion inside the moved text then consists of new Yjs items.
  // It is unchanged when it comes back with the same text, marks and
  // metadata, in the same order among the suggestions around it, and in the
  // place its surviving neighbours put it.
  const FOREIGN = { patchId: 'bbbbbbbb-0000-4000-8000-0000000000f1', suggestionId: 'bbbbbbbb-0000-4000-8000-000000000001' }
  const SECOND_FOREIGN = { patchId: 'bbbbbbbb-0000-4000-8000-0000000000f2', suggestionId: 'bbbbbbbb-0000-4000-8000-000000000002' }
  const OWN = { patchId: 'aaaaaaaa-0000-4000-8000-0000000000f1', suggestionId: 'aaaaaaaa-0000-4000-8000-000000000001' }
  const THIRD_USER_ID = '66666666-6666-4666-8666-666666666666'

  const insertion = (authorId: string, ids: { patchId: string; suggestionId: string }) => ({
    type: 'insertion',
    attrs: { authorId, createdAt: CREATED_AT, id: ids.suggestionId, kind: 'insertion', ...ids },
  })
  const text = (value: string, marks?: JSONContent['marks']): JSONContent => (
    marks ? { type: 'text', text: value, marks } : { type: 'text', text: value }
  )
  const paragraph = (...content: JSONContent[]): JSONContent => ({ type: 'paragraph', content })
  const documentOf = (...content: JSONContent[]) => schema.nodeFromJSON({ type: 'doc', content })

  const withSuggestion = (authorId: string) => documentOf(
    paragraph(text('Intro block')),
    paragraph(text('Alpha paragraph'), text(' inserted', [insertion(authorId, authorId === USER_ID ? OWN : FOREIGN)]), text(' tail text')),
    paragraph(text('Second block')),
  )
  const FOREIGN_DOCUMENT = withSuggestion(OTHER_USER_ID)
  const OWN_DOCUMENT = withSuggestion(USER_ID)
  const TWO_FOREIGN_DOCUMENT = documentOf(
    paragraph(
      text('Alpha paragraph'),
      text(' one', [insertion(OTHER_USER_ID, FOREIGN)]),
      text(' middle'),
      text(' two', [insertion(OTHER_USER_ID, SECOND_FOREIGN)]),
      text(' end'),
    ),
  )

  /** Position of the first character of `needle` in the document. */
  function at(document: ProseMirrorNode, needle: string): number {
    let found: number | null = null
    document.descendants((node, position) => {
      if (found !== null) return false
      if (node.isText && node.text?.includes(needle)) found = position + node.text.indexOf(needle)
      return true
    })
    if (found === null) throw new Error(`fixture has no text ${needle}`)
    return found
  }

  /** Start of the block whose text contains `needle`. */
  function blockStart(document: ProseMirrorNode, needle: string): number {
    const $position = document.resolve(at(document, needle))
    return $position.before($position.depth)
  }

  function markedNode(document: ProseMirrorNode, needle: string): ProseMirrorNode {
    const node = document.nodeAt(at(document, needle))
    if (!node) throw new Error(`fixture has no node at ${needle}`)
    return node
  }

  type Build = (state: EditorState) => Transaction

  const splitAt = (needle: string): Build => (state) => state.tr.split(at(state.doc, needle))

  /** Moves the marked text that starts with `needle` to `target(document)`. */
  const moveMarked = (needle: string, target: (tr: Transaction) => number): Build => (state) => {
    const node = markedNode(state.doc, needle)
    const from = at(state.doc, needle)
    const tr = state.tr.delete(from, from + node.nodeSize)
    return tr.insert(target(tr), node)
  }

  /** Splits before the suggestion, then changes what was moved. */
  const splitThen = (change: (tr: Transaction, from: number, node: ProseMirrorNode) => void): Build => (state) => {
    const node = markedNode(state.doc, ' inserted')
    const tr = state.tr.split(at(state.doc, 'paragraph'))
    change(tr, tr.mapping.map(at(state.doc, ' inserted')), node)
    return tr
  }

  function verdictOf(
    document: ProseMirrorNode,
    build: Build,
    access: 'edit' | 'suggest' = 'edit',
  ): string {
    const after = build(EditorState.create({ schema, doc: document })).doc
    try {
      return validateMappedSuggestionUpdate(document.toJSON(), after.toJSON(), access, USER_ID).changeKind
    } catch (error) {
      if (error instanceof CollaborationError) return `refused ${error.reason}`
      throw error
    }
  }

  const ACCEPTED = 'direct'
  const REFUSED = 'refused suggestion_policy_violation'

  it.each<[string, ProseMirrorNode, Build, string]>([
    ['a split before another person\'s insertion', FOREIGN_DOCUMENT, splitAt('paragraph'), ACCEPTED],
    ['joining the block that holds it to the one before', FOREIGN_DOCUMENT, (state) => state.tr.join(blockStart(state.doc, 'Alpha')), ACCEPTED],
    ['joining the next block to the one that holds it', FOREIGN_DOCUMENT, (state) => state.tr.join(blockStart(state.doc, 'Second')), ACCEPTED],
    ['turning the block into a heading', FOREIGN_DOCUMENT, (state) => {
      const start = blockStart(state.doc, 'Alpha')
      return state.tr.setBlockType(start + 1, start + 1, schema.nodes.heading!, { level: 2 })
    }, ACCEPTED],
    ['wrapping the block in a list', FOREIGN_DOCUMENT, (state) => {
      const $position = state.doc.resolve(at(state.doc, 'Alpha'))
      const range = $position.blockRange()
      const wrapping = range && findWrapping(range, schema.nodes.bulletList!)
      if (!range || !wrapping) throw new Error('the block cannot be wrapped')
      return state.tr.wrap(range, wrapping)
    }, ACCEPTED],
    ['a split after the suggestion', FOREIGN_DOCUMENT, splitAt('tail'), ACCEPTED],
    ['typing elsewhere in the block', FOREIGN_DOCUMENT, (state) => state.tr.insertText('x', at(state.doc, 'Alpha')), ACCEPTED],
    ['Enter over a selection before the suggestion', FOREIGN_DOCUMENT, (state) => {
      const from = at(state.doc, 'paragraph')
      return state.tr.delete(from, from + 'paragraph'.length).split(from)
    }, ACCEPTED],
    ['pasting two paragraphs before the suggestion', FOREIGN_DOCUMENT, (state) => {
      const pasted = new Slice(Fragment.from([
        schema.node('paragraph', null, schema.text('Pasted one')),
        schema.node('paragraph', null, schema.text('Pasted two')),
      ]), 1, 1)
      const from = at(state.doc, 'paragraph')
      return state.tr.replace(from, from, pasted)
    }, ACCEPTED],
    ['a split that also changes the moved original text', FOREIGN_DOCUMENT, (state) => {
      const tr = state.tr.split(at(state.doc, 'paragraph'))
      return tr.insertText('s', tr.mapping.map(at(state.doc, 'tail text') + 'tail text'.length))
    }, ACCEPTED],
    ['a split before the writer\'s own pending insertion', OWN_DOCUMENT, splitAt('paragraph'), ACCEPTED],
    ['turning a block with the writer\'s own pending insertion into a heading', OWN_DOCUMENT, (state) => {
      const start = blockStart(state.doc, 'Alpha')
      return state.tr.setBlockType(start + 1, start + 1, schema.nodes.heading!, { level: 2 })
    }, ACCEPTED],
    ['writing the suggestion again unchanged in place', FOREIGN_DOCUMENT, (state) => {
      const from = at(state.doc, ' inserted')
      const node = markedNode(state.doc, ' inserted')
      return state.tr.replaceWith(from, from + node.nodeSize, node)
    }, ACCEPTED],
    ['a split inside the suggestion', FOREIGN_DOCUMENT, (state) => state.tr.split(at(state.doc, 'inserted') + 3), REFUSED],
    ['moving the suggestion into another block', FOREIGN_DOCUMENT, moveMarked(' inserted', (tr) => tr.doc.content.size - 1), REFUSED],
    ['moving the suggestion past original text of its block', FOREIGN_DOCUMENT, moveMarked(' inserted', (tr) => at(tr.doc, ' tail text') + ' tail text'.length), REFUSED],
    ['moving the whole paragraph after the next one', FOREIGN_DOCUMENT, (state) => {
      const start = blockStart(state.doc, 'Alpha')
      const node = state.doc.nodeAt(start)
      if (!node) throw new Error('fixture has no paragraph')
      const tr = state.tr.delete(start, start + node.nodeSize)
      return tr.insert(tr.doc.content.size, node)
    }, REFUSED],
    ['a split that changes the suggestion text', FOREIGN_DOCUMENT, splitThen((tr, from, node) => {
      tr.replaceWith(from, from + node.nodeSize, schema.text(' insertex', node.marks))
    }), REFUSED],
    ['a split that formats the suggestion', FOREIGN_DOCUMENT, splitThen((tr, from, node) => {
      tr.addMark(from, from + node.nodeSize, schema.marks.bold!.create())
    }), REFUSED],
    ['a split that gives the suggestion another author', FOREIGN_DOCUMENT, splitThen((tr, from, node) => {
      const [mark] = node.marks
      if (!mark) throw new Error('fixture has no mark')
      tr.replaceWith(from, from + node.nodeSize, schema.text(node.text ?? '', [
        mark.type.create({ ...mark.attrs, authorId: THIRD_USER_ID }),
      ]))
    }), REFUSED],
    ['a split that duplicates the suggestion', FOREIGN_DOCUMENT, splitThen((tr, from, node) => {
      tr.insert(from + node.nodeSize, node)
    }), REFUSED],
    ['a split that swaps two suggestions', TWO_FOREIGN_DOCUMENT, (state) => {
      const one = markedNode(state.doc, ' one')
      const two = markedNode(state.doc, ' two')
      const oneFrom = at(state.doc, ' one')
      const twoFrom = at(state.doc, ' two')
      const tr = state.tr.split(at(state.doc, 'paragraph'))
      tr.replaceWith(tr.mapping.map(twoFrom), tr.mapping.map(twoFrom + two.nodeSize), one)
      tr.replaceWith(tr.mapping.map(oneFrom), tr.mapping.map(oneFrom + one.nodeSize), two)
      return tr
    }, REFUSED],
    ['a split that changes original text between two suggestions', TWO_FOREIGN_DOCUMENT, (state) => {
      const from = at(state.doc, 'middle')
      const tr = state.tr.split(at(state.doc, 'paragraph'))
      return tr.insertText('u', tr.mapping.map(from + 1), tr.mapping.map(from + 2))
    }, REFUSED],
    // Deleting the suggestion is a rejection only when its characters are
    // gone. " inserted" and " tail" start with the same space, and the plain
    // binding writes this deletion by keeping the suggestion's space for the
    // one behind it: the suggestion's first character would survive as plain
    // text. The editor writes it through the mark-aware text update, which
    // deletes the suggestion's own characters.
    ['deleting the suggestion where the plain binding keeps its first character', FOREIGN_DOCUMENT, (state) => {
      const from = at(state.doc, ' inserted')
      return state.tr.delete(from, from + ' inserted'.length)
    }, REFUSED],
    ['deleting the suggestion together with the space behind it', FOREIGN_DOCUMENT, (state) => {
      const from = at(state.doc, ' inserted')
      return state.tr.delete(from, from + ' inserted '.length)
    }, ACCEPTED],
  ])('%s', (_label, document, build, expected) => {
    expect(verdictOf(document, build)).toBe(expected)
  })

  it('stays refused for a split before another person\'s suggestion under a suggest-only lease', () => {
    expect(verdictOf(FOREIGN_DOCUMENT, splitAt('paragraph'), 'suggest')).toBe(REFUSED)
  })

  it('is offered to an edit lease only', () => {
    const before = FOREIGN_DOCUMENT
    const beforeDocument = editorJsonToYDoc(before.toJSON())
    const afterDocument = new Y.Doc()
    Y.applyUpdate(afterDocument, Y.encodeStateAsUpdate(beforeDocument))
    const documents = { afterDocument, beforeDocument }
    try {
      expect(createYjsSuggestionPositionMapping(before.toJSON(), before.toJSON(), documents, 'edit').recreated)
        .not.toBeNull()
      expect(createYjsSuggestionPositionMapping(before.toJSON(), before.toJSON(), documents, 'suggest').recreated)
        .toBeNull()
    } finally {
      afterDocument.destroy()
      beforeDocument.destroy()
    }
  })
})

describe('the cost of following text written again', () => {
  // The re-creation rule reads both documents whole. It may only do so for
  // a text occurrence that could not be followed by its own characters, and
  // then once per document, however many occurrences need it.
  const FIRST = { patchId: 'cccccccc-0000-4000-8000-0000000000f1', suggestionId: 'cccccccc-0000-4000-8000-000000000001' }
  const SECOND = { patchId: 'cccccccc-0000-4000-8000-0000000000f2', suggestionId: 'cccccccc-0000-4000-8000-000000000002' }
  const SENTENCE = 'Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor. '
  const insertion = (ids: { patchId: string; suggestionId: string }) => ({
    type: 'insertion',
    attrs: { authorId: OTHER_USER_ID, createdAt: CREATED_AT, id: ids.suggestionId, kind: 'insertion', ...ids },
  })

  /** About 80 pages of text, with two pending suggestions of another
   * person near the end. */
  function largeDocument(): ProseMirrorNode {
    const content: JSONContent[] = []
    for (let index = 0; index < 1_600; index += 1) {
      content.push({ type: 'paragraph', content: [{ type: 'text', text: `Block ${index}. ${SENTENCE.repeat(5)}` }] })
    }
    content[1_500] = {
      type: 'paragraph',
      content: [
        { type: 'text', text: 'Alpha paragraph' },
        { type: 'text', text: ' first', marks: [insertion(FIRST)] },
        { type: 'text', text: ' middle' },
        { type: 'text', text: ' second', marks: [insertion(SECOND)] },
        { type: 'text', text: ' tail' },
      ],
    }
    return schema.nodeFromJSON({ type: 'doc', content })
  }

  function inlineUnitCount(document: ProseMirrorNode): number {
    let count = 0
    document.descendants((node) => {
      if (node.isText) count += node.text?.length ?? 0
      else if (node.isLeaf) count += 1
    })
    return count
  }

  function judge(before: ProseMirrorNode, build: (state: EditorState) => Transaction): {
    followed: boolean
    milliseconds: number
    unitsRead: number
    unitsTotal: number
    verdict: string
  } {
    const beforeDocument = editorJsonToYDoc(before.toJSON())
    const afterDocument = new Y.Doc()
    Y.applyUpdate(afterDocument, Y.encodeStateAsUpdate(beforeDocument))
    const fragment = afterDocument.getXmlFragment(EDITOR_YJS_FRAGMENT)
    const initialized = initProseMirrorDoc(fragment, schema)
    const after = build(EditorState.create({ schema, doc: initialized.doc })).doc
    const deletions: Array<Y.Transaction['deleteSet']> = []
    afterDocument.on('afterTransaction', (transaction: Y.Transaction) => {
      deletions.push(transaction.deleteSet)
    })
    updateYFragment(afterDocument, fragment, after, initialized.meta)
    const documents = {
      afterDocument,
      beforeDocument,
      deletedByUpdate: Y.mergeDeleteSets(deletions),
    }
    try {
      const started = performance.now()
      let verdict: string
      try {
        verdict = validateSuggestionUpdate(before.toJSON(), after.toJSON(), 'edit', USER_ID, documents)
          .changeKind
      } catch (error) {
        if (!(error instanceof CollaborationError)) throw error
        verdict = `refused ${error.reason}`
      }
      const milliseconds = performance.now() - started
      const mapping = createYjsSuggestionPositionMapping(before.toJSON(), after.toJSON(), documents, 'edit')
      const beforeRecords = collectSuggestionRecords(before.toJSON())
      const afterRecords = collectSuggestionRecords(after.toJSON())
      const followed = [...beforeRecords].every(([id, record]) => {
        const next = afterRecords.get(id)
        return next !== undefined && sameSuggestionRecord(record, next, mapping.map, mapping.recreated)
      })
      return {
        followed,
        milliseconds,
        unitsRead: mapping.unitsRead(),
        unitsTotal: inlineUnitCount(before) + inlineUnitCount(after),
        verdict,
      }
    } finally {
      afterDocument.destroy()
      beforeDocument.destroy()
    }
  }

  const at = (document: ProseMirrorNode, needle: string): number => {
    let found = -1
    document.descendants((node, position) => {
      if (found >= 0) return false
      if (node.isText && node.text?.includes(needle)) found = position + node.text.indexOf(needle)
      return true
    })
    if (found < 0) throw new Error(`fixture has no text ${needle}`)
    return found
  }

  it('reads nothing for ordinary typing, and each document once for a split before two suggestions', () => {
    const before = largeDocument()
    expect(inlineUnitCount(before)).toBeGreaterThan(600_000)

    for (const needle of ['Block 10.', 'Alpha', 'tail']) {
      const typing = judge(before, (state) => state.tr.insertText('x', at(state.doc, needle)))
      expect(typing).toMatchObject({ followed: true, unitsRead: 0, verdict: 'direct' })
    }
    // A suggestion whose own characters survive is never looked for in the
    // rewritten text, also when the update formatted it, which keeps it
    // pending on those characters.
    const formatted = judge(before, (state) => {
      const from = at(state.doc, ' first')
      return state.tr.addMark(from, from + ' first'.length, schema.marks.bold!.create())
    })
    expect(formatted).toMatchObject({
      followed: false,
      unitsRead: 0,
      verdict: 'direct',
    })

    const split = judge(before, (state) => state.tr.split(at(state.doc, 'paragraph')))
    expect(split).toMatchObject({ followed: true, verdict: 'direct' })
    expect(split.unitsRead).toBe(split.unitsTotal)
    // One linear pass over both documents takes tens of milliseconds here;
    // reading the document again for every unit takes seconds.
    expect(split.milliseconds).toBeLessThan(1_500)
  })
})

function validateMappedSuggestionUpdate(
  before: JSONContent,
  after: JSONContent,
  access: 'edit' | 'suggest' | 'view',
  actorUserId: string,
): ReturnType<typeof validateSuggestionUpdate> {
  const beforeDocument = editorJsonToYDoc(before)
  const afterDocument = new Y.Doc()
  Y.applyUpdate(afterDocument, Y.encodeStateAsUpdate(beforeDocument))
  const fragment = afterDocument.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, schema)
  const deletions: Array<Y.Transaction['deleteSet']> = []
  afterDocument.on('afterTransaction', (transaction: Y.Transaction) => {
    deletions.push(transaction.deleteSet)
  })
  updateYFragment(
    afterDocument,
    fragment,
    schema.nodeFromJSON(after),
    initialized.meta,
  )
  try {
    return validateSuggestionUpdate(before, after, access, actorUserId, {
      afterDocument,
      beforeDocument,
      deletedByUpdate: Y.mergeDeleteSets(deletions),
    })
  } finally {
    afterDocument.destroy()
    beforeDocument.destroy()
  }
}

function trackedInsertion(
  json: JSONContent,
  authorId: string,
  textOffset: number,
): ReturnType<typeof schema.nodeFromJSON> {
  const document = schema.nodeFromJSON(json)
  let textPosition: number | null = null
  document.descendants((node, position) => {
    if (textPosition === null && node.isText && node.text?.includes('abcabc')) {
      textPosition = position
    }
  })
  if (textPosition === null) throw new Error('suggestion text fixture is invalid')
  const state = EditorState.create({ schema, doc: document })
  return transformToInqtrixSuggestionTransaction(
    state.tr.insertText('X', textPosition + textOffset),
    state,
    { authorId, createdAt: CREATED_AT, patchId: PATCH_ID },
    () => SUGGESTION_ID,
  ).doc
}

function relocateSuggestionToParentEnd(document: JSONContent): JSONContent {
  const after = structuredClone(document)
  const parent = findSuggestionParent(after)
  if (!parent?.content) throw new Error('suggestion relocation fixture is invalid')
  const index = parent.content.findIndex((node) => hasSuggestion(node))
  if (index < 0) throw new Error('suggestion relocation fixture is invalid')
  const [occurrence] = parent.content.splice(index, 1)
  if (!occurrence) throw new Error('suggestion relocation fixture is invalid')
  parent.content.push(occurrence)
  return after
}

function findSuggestionParent(node: JSONContent): JSONContent | null {
  if (node.content?.some((child) => hasSuggestion(child))) return node
  for (const child of node.content ?? []) {
    const found = findSuggestionParent(child)
    if (found) return found
  }
  return null
}

function findSuggestionNode(node: JSONContent): JSONContent | null {
  if (hasSuggestion(node)) return node
  for (const child of node.content ?? []) {
    const found = findSuggestionNode(child)
    if (found) return found
  }
  return null
}

function hasSuggestion(node: JSONContent): boolean {
  return Boolean(node.marks?.some((mark) => mark.attrs?.suggestionId === SUGGESTION_ID))
}
