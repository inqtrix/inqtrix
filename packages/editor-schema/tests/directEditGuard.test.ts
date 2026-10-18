import { getSchema } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { joinBackward, splitBlock } from '@tiptap/pm/commands'
import { EditorState, TextSelection, type Transaction } from '@tiptap/pm/state'
import { describe, expect, it } from 'vitest'
import {
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  UnsupportedSuggestionStructureError,
  assertDirectEditKeepsSuggestionRecords,
  createEditorSchemaExtensions,
} from '../src/index'

// In edit mode a change is written directly. Every pending suggestion,
// another person's or the writer's own, may lose characters and be formatted
// and stays pending with what is left; it is removed once every character it
// proposed is deleted. It never gains a character, moves, or loses its mark
// while its text stays. What the collaboration service would refuse, the
// editor refuses before it is sent, with a reason it can explain.

const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
const ACTOR = 'user-actor'
const OTHER = 'user-other'

function metadata(authorId: string, suggestionId: string, kind: string) {
  return {
    authorId,
    createdAt: 1_784_112_000,
    id: suggestionId,
    kind,
    patchId: `patch-${suggestionId}`,
    suggestionId,
  }
}

const DOCUMENT = {
  type: 'doc',
  content: [
    { type: 'paragraph', content: [{ type: 'text', text: 'Before ZONEBEFORE.' }] },
    {
      type: 'paragraph',
      attrs: {
        [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: {
          action: 'heading2',
          authorId: ACTOR,
          createdAt: 1_784_112_000,
          kind: 'structure',
          patchId: 'patch-structure',
          suggestionId: 'structure',
        },
      },
      content: [{ type: 'text', text: 'Structure ZONETARGET with text.' }],
    },
    { type: 'paragraph', content: [{ type: 'text', text: 'Following ZONEFOLLOW.' }] },
    {
      type: 'paragraph',
      content: [
        { type: 'text', text: 'With ' },
        { type: 'text', text: 'OWNINSERT', marks: [{ type: 'insertion', attrs: metadata(ACTOR, 'own-insertion', 'insertion') }] },
        { type: 'text', text: ' and ' },
        { type: 'text', text: 'OWNDELETE', marks: [{ type: 'deletion', attrs: metadata(ACTOR, 'own-deletion', 'deletion') }] },
        { type: 'text', text: ' end.' },
      ],
    },
    {
      type: 'paragraph',
      content: [
        { type: 'text', text: 'Foreign ' },
        { type: 'text', text: 'THEIRS', marks: [{ type: 'insertion', attrs: metadata(OTHER, 'foreign-insertion', 'insertion') }] },
        { type: 'text', text: ' tail.' },
      ],
    },
    { type: 'paragraph', content: [{ type: 'text', text: 'X', marks: [{ type: 'insertion', attrs: metadata(ACTOR, 'single', 'insertion') }] }] },
    {
      type: 'paragraph',
      content: [
        { type: 'text', text: 'Replace ' },
        { type: 'text', text: 'OLDWORD', marks: [{ type: 'deletion', attrs: metadata(OTHER, 'foreign-replacement', 'replacement') }] },
        { type: 'text', text: 'NEWWORD', marks: [{ type: 'insertion', attrs: metadata(OTHER, 'foreign-replacement', 'replacement') }] },
        { type: 'text', text: ' here.' },
      ],
    },
    {
      type: 'paragraph',
      attrs: {
        [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: {
          action: 'heading3',
          authorId: OTHER,
          createdAt: 1_784_112_000,
          kind: 'structure',
          patchId: 'patch-foreign-structure',
          suggestionId: 'foreign-structure',
        },
      },
      content: [{ type: 'text', text: 'Foreign structure ZONEFOREIGNSTRUCT.' }],
    },
  ],
}

function at(document: ProseMirrorNode, needle: string, where: 'start' | 'end' | 'in', offset = 0): number {
  let found = -1
  document.descendants((node, position) => {
    if (found >= 0) return false
    if (node.isTextblock && node.textContent.includes(needle)) {
      if (where === 'start') found = position + 1
      else if (where === 'end') found = position + 1 + node.content.size
      else found = position + 1 + node.textContent.indexOf(needle) + offset
      return false
    }
    return true
  })
  if (found < 0) throw new Error(`fixture has no text block containing ${needle}`)
  return found
}

type Command = (state: EditorState, dispatch: (transaction: Transaction) => void) => boolean

function edit(
  place: (document: ProseMirrorNode) => number,
  change: Command | ((state: EditorState) => Transaction),
): Transaction {
  const base = EditorState.create({ doc: schema.nodeFromJSON(DOCUMENT), schema })
  const state = base.apply(base.tr.setSelection(TextSelection.create(base.doc, place(base.doc))))
  if (change === splitBlock || change === joinBackward) {
    let transaction: Transaction | null = null
    ;(change as Command)(state, (next) => {
      transaction = next
    })
    if (!transaction) throw new Error('the command did not apply')
    return transaction
  }
  return (change as (state: EditorState) => Transaction)(state)
}

function verdict(transaction: Transaction): string {
  try {
    assertDirectEditKeepsSuggestionRecords(transaction, ACTOR)
    return 'allowed'
  } catch (error) {
    if (error instanceof UnsupportedSuggestionStructureError) return error.reason
    throw error
  }
}

const typeText = (value: string) => (state: EditorState) => state.tr.insertText(value)
const deleteNext = (length: number) => (state: EditorState) => (
  state.tr.delete(state.selection.from, state.selection.from + length)
)
const deleteBlockAround = (state: EditorState) => {
  const $from = state.selection.$from
  return state.tr.delete($from.before(1), $from.after(1))
}
const withMark = (name: string, length: number, attrs?: Record<string, unknown>) => (state: EditorState) => {
  const mark = schema.marks[name]
  if (!mark) throw new Error(`schema has no ${name} mark`)
  return state.tr.addMark(state.selection.from, state.selection.from + length, mark.create(attrs))
}
// Typed text without any mark, as the editor inserts it inside another
// person's suggestion in edit mode.
const typePlain = (value: string) => (state: EditorState) => (
  state.tr.insert(state.selection.from, schema.text(value))
)
const withoutMark = (name: string, length: number) => (state: EditorState) => {
  const mark = schema.marks[name]
  if (!mark) throw new Error(`schema has no ${name} mark`)
  return state.tr.removeMark(state.selection.from, state.selection.from + length, mark)
}

describe('the edit-mode guard for pending suggestions', () => {
  it.each([
    ['typing in a paragraph without suggestions', () => edit((d) => at(d, 'ZONEBEFORE', 'end'), typeText('x'))],
    ['Enter at the end of a paragraph without suggestions', () => edit((d) => at(d, 'ZONEBEFORE', 'end'), splitBlock)],
    ['Enter at the end of a block with an own structure suggestion', () => edit((d) => at(d, 'ZONETARGET', 'end'), splitBlock)],
    ['bold inside a block with an own structure suggestion', () => edit((d) => at(d, 'ZONETARGET', 'in'), (state: EditorState) => {
      const bold = schema.marks.bold
      if (!bold) throw new Error('schema has no bold mark')
      return state.tr.addMark(state.selection.from, state.selection.from + 4, bold.create())
    })],
    ['typing inside an own insertion', () => edit((d) => at(d, 'OWNINSERT', 'in', 3), typeText('x'))],
    ['Backspace inside an own insertion', () => edit((d) => at(d, 'OWNINSERT', 'in', 3), (state: EditorState) => (
      state.tr.delete(state.selection.from - 1, state.selection.from)
    ))],
    ['typing directly before an own insertion', () => edit((d) => at(d, 'OWNINSERT', 'in'), typeText('x'))],
    ['typing directly after a foreign insertion', () => edit((d) => at(d, 'THEIRS', 'in', 6), typeText('x'))],
    ['deleting a foreign insertion completely, which removes it', () => edit((d) => at(d, 'THEIRS', 'in'), deleteNext('THEIRS'.length))],
    ['deleting the paragraph that holds a foreign insertion, which removes it', () => edit((d) => at(d, 'THEIRS', 'in'), deleteBlockAround)],
    ['replacing a foreign insertion and the text around it, which removes it', () => edit((d) => at(d, 'Foreign THEIRS', 'in'), (state: EditorState) => (
      state.tr.insertText('New words', state.selection.from, state.selection.from + 'Foreign THEIRS tail'.length)
    ))],
    ['deleting both halves of a foreign replacement, which removes it', () => edit((d) => at(d, 'OLDWORD', 'in'), deleteNext('OLDWORDNEWWORD'.length))],
    ['deleting the block that carries a foreign structure suggestion, which removes it', () => edit((d) => at(d, 'ZONEFOREIGNSTRUCT', 'in'), deleteBlockAround)],
    ['Backspace at the start of a block with an own structure suggestion, which removes it', () => edit((d) => at(d, 'ZONETARGET', 'start'), joinBackward)],
    ['Backspace on the last character of an own insertion, which removes it', () => edit((d) => at(d, 'X', 'end'), (state: EditorState) => (
      state.tr.delete(state.selection.from - 1, state.selection.from)
    ))],
    ['code formatting over an own deletion, which removes it and keeps the text', () => edit((d) => at(d, 'OWNDELETE', 'in'), withMark('code', 'OWNDELETE'.length))],
    ['deleting part of a foreign insertion', () => edit((d) => at(d, 'THEIRS', 'in', 2), deleteNext(2))],
    ['deleting part of an own insertion together with the original text before it', () => edit((d) => at(d, 'With OWNINSERT', 'in'), deleteNext('With OWN'.length))],
    ['deleting part of the proposed deletion of a foreign replacement', () => edit((d) => at(d, 'OLDWORD', 'in', 1), deleteNext(2))],
    ['bold over a foreign insertion', () => edit((d) => at(d, 'THEIRS', 'in'), withMark('bold', 'THEIRS'.length))],
    ['a link over a foreign insertion and the text around it', () => edit((d) => at(d, 'Foreign THEIRS', 'in'), withMark('link', 'Foreign THEIRS tail'.length, { href: 'https://example.com' }))],
    ['bold over a foreign replacement', () => edit((d) => at(d, 'OLDWORD', 'in'), withMark('bold', 'OLDWORDNEWWORD'.length))],
    ['bold across an own insertion and the original text around it', () => edit((d) => at(d, 'With OWNINSERT', 'in', 2), withMark('bold', 'th OWNINSERT an'.length))],
    ['plain text typed inside a foreign insertion, which splits it', () => edit((d) => at(d, 'THEIRS', 'in', 3), typePlain('x'))],
    ['typing inside a block with an own structure suggestion', () => edit((d) => at(d, 'ZONETARGET', 'in', 2), typeText('x'))],
    ['typing inside a block with a foreign structure suggestion', () => edit((d) => at(d, 'ZONEFOREIGNSTRUCT', 'in', 2), typeText('x'))],
    ['Backspace at the start of the block after a block with an own structure suggestion', () => edit((d) => at(d, 'ZONEFOLLOW', 'start'), joinBackward)],
  ])('allows %s', (_name, build) => {
    expect(verdict(build())).toBe('allowed')
  })

  it.each([
    ['Enter in the middle of a block with an own structure suggestion', 'own_suggestion_edit_unsupported', () => edit((d) => at(d, 'ZONETARGET', 'in', 4), splitBlock)],
    ['Enter at the start of a block with an own structure suggestion', 'own_suggestion_edit_unsupported', () => edit((d) => at(d, 'ZONETARGET', 'start'), splitBlock)],
    ['typing inside an own deletion', 'own_suggestion_edit_unsupported', () => edit((d) => at(d, 'OWNDELETE', 'in', 3), typeText('x'))],
    ['formatting that removes an own insertion mark and keeps its text', 'suggestion_would_disappear', () => edit((d) => at(d, 'OWNINSERT', 'in'), withMark('code', 'OWNINSERT'.length))],
    ['removing the mark of an own insertion and keeping its text', 'suggestion_would_disappear', () => edit((d) => at(d, 'OWNINSERT', 'in'), withoutMark('insertion', 'OWNINSERT'.length))],
    ['code over a foreign insertion, which would keep its text without its mark', 'foreign_suggestion_changed', () => edit((d) => at(d, 'THEIRS', 'in'), withMark('code', 'THEIRS'.length))],
    ['removing the mark of part of a foreign insertion and keeping its text', 'foreign_suggestion_changed', () => edit((d) => at(d, 'THEIRS', 'in', 1), withoutMark('insertion', 2))],
    ['typing inside a foreign insertion with the text taking its mark', 'foreign_suggestion_changed', () => edit((d) => at(d, 'THEIRS', 'in', 3), typeText('x'))],
    ['changing the proposal of a foreign structure suggestion', 'foreign_suggestion_changed', () => edit((d) => at(d, 'ZONEFOREIGNSTRUCT', 'in'), (state: EditorState) => {
      const position = state.selection.$from.before(1)
      const data = state.doc.nodeAt(position)?.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR] as Record<string, unknown>
      return state.tr.setNodeAttribute(position, INQTRIX_STRUCTURE_SUGGESTION_ATTR, { ...data, action: 'codeBlock' })
    })],
    ['deleting only the proposed half of a foreign replacement', 'foreign_suggestion_changed', () => edit((d) => at(d, 'NEWWORD', 'in'), deleteNext('NEWWORD'.length))],
    ['moving a foreign insertion to another paragraph', 'foreign_suggestion_changed', () => edit((d) => at(d, 'THEIRS', 'in'), (state: EditorState) => {
      const from = state.selection.from
      const slice = state.doc.slice(from, from + 'THEIRS'.length)
      const transaction = state.tr.delete(from, from + 'THEIRS'.length)
      return transaction.replace(at(transaction.doc, 'ZONEBEFORE', 'end'), undefined, slice)
    })],
    ['typing between the halves of a foreign replacement', 'foreign_suggestion_changed', () => edit((d) => at(d, 'NEWWORD', 'in'), typeText('x'))],
    ['moving a block that carries a foreign structure suggestion', 'foreign_suggestion_changed', () => edit((d) => at(d, 'ZONEBEFORE', 'start'), (state: EditorState) => {
      let from = -1
      state.doc.forEach((node, offset) => {
        if (node.textContent.includes('ZONEFOREIGNSTRUCT')) from = offset
      })
      const node = state.doc.nodeAt(from)
      if (!node) throw new Error('fixture has no foreign structure block')
      return state.tr.delete(from, from + node.nodeSize).insert(0, node)
    })],
  ])('refuses %s', (_name, reason, build) => {
    expect(verdict(build())).toBe(reason)
  })

  it.each([
    ['deleting a foreign insertion completely', () => edit((d) => at(d, 'THEIRS', 'in'), deleteNext('THEIRS'.length)), ['foreign-insertion']],
    ['deleting both halves of a foreign replacement', () => edit((d) => at(d, 'OLDWORD', 'in'), deleteNext('OLDWORDNEWWORD'.length)), ['foreign-replacement']],
    ['Backspace on the last character of an own insertion', () => edit((d) => at(d, 'X', 'end'), (state: EditorState) => (
      state.tr.delete(state.selection.from - 1, state.selection.from)
    )), ['single']],
    ['typing directly after a foreign insertion', () => edit((d) => at(d, 'THEIRS', 'in', 6), typeText('x')), []],
    ['deleting part of a foreign insertion', () => edit((d) => at(d, 'THEIRS', 'in', 2), deleteNext(2)), []],
  ] as const)('names what %s removes', (_name, build, removed) => {
    expect(assertDirectEditKeepsSuggestionRecords(build(), ACTOR)).toEqual(removed)
  })
})

describe('the edit-mode guard for structure suggestions that must stay applicable', () => {
  // A code block holds plain text only. Formatting inside a block that is
  // proposed as a code block would leave a proposal that cannot be applied,
  // which the collaboration service refuses.
  const codeBlockProposal = (authorId: string, suggestionId: string) => ({
    [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: {
      action: 'codeBlock',
      authorId,
      createdAt: 1_784_112_000,
      kind: 'structure',
      patchId: `patch-${suggestionId}`,
      suggestionId,
    },
  })
  const PROPOSED = {
    type: 'doc',
    content: [
      { type: 'paragraph', attrs: codeBlockProposal(OTHER, 'foreign-code'), content: [{ type: 'text', text: 'Foreign code ZONEFOREIGNCODE' }] },
      { type: 'paragraph', attrs: codeBlockProposal(ACTOR, 'own-code'), content: [{ type: 'text', text: 'Own code ZONEOWNCODE' }] },
      { type: 'paragraph', content: [{ type: 'text', text: 'Plain ZONEPLAIN.' }] },
    ],
  }

  function boldIn(needle: string): Transaction {
    const state = EditorState.create({ doc: schema.nodeFromJSON(PROPOSED), schema })
    const bold = schema.marks.bold
    if (!bold) throw new Error('schema has no bold mark')
    const from = at(state.doc, needle, 'in')
    return state.tr.addMark(from, from + 4, bold.create())
  }

  it.each([
    ['a block another person proposed as a code block', 'ZONEFOREIGNCODE'],
    ['a block the actor proposed as a code block', 'ZONEOWNCODE'],
  ])('refuses bold inside %s', (_name, needle) => {
    expect(verdict(boldIn(needle))).toBe('structure_suggestion_inapplicable')
  })

  it('allows bold in a block without a proposal', () => {
    expect(verdict(boldIn('ZONEPLAIN'))).toBe('allowed')
  })

  it('refuses bold inside a quoted block proposed as a code block, the only proposal', () => {
    const quoted = EditorState.create({
      doc: schema.nodeFromJSON({
        type: 'doc',
        content: [
          {
            type: 'blockquote',
            content: [{ type: 'paragraph', attrs: codeBlockProposal(OTHER, 'quoted-code'), content: [{ type: 'text', text: 'Quoted code ZONEQUOTED' }] }],
          },
          { type: 'paragraph', content: [{ type: 'text', text: 'Plain ZONEPLAIN.' }] },
        ],
      }),
      schema,
    })
    const bold = schema.marks.bold
    if (!bold) throw new Error('schema has no bold mark')
    const from = at(quoted.doc, 'ZONEQUOTED', 'in')
    expect(verdict(quoted.tr.addMark(from, from + 4, bold.create()))).toBe('structure_suggestion_inapplicable')
  })

  it('allows a change elsewhere while a proposal already cannot be applied', () => {
    const stored = EditorState.create({ doc: schema.nodeFromJSON(PROPOSED), schema })
    const bold = schema.marks.bold
    if (!bold) throw new Error('schema has no bold mark')
    const from = at(stored.doc, 'ZONEOWNCODE', 'in')
    const inapplicable = stored.apply(stored.tr.addMark(from, from + 4, bold.create()))
    expect(verdict(inapplicable.tr.insertText('!', at(inapplicable.doc, 'ZONEPLAIN', 'end'))))
      .toBe('allowed')
  })
})
