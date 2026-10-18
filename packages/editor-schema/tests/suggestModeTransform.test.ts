import { Editor, getSchema } from '@tiptap/core'
import { joinBackward } from '@tiptap/pm/commands'
import { Fragment, Slice } from '@tiptap/pm/model'
import { splitListItem } from '@tiptap/pm/schema-list'
import { EditorState, TextSelection, type Transaction } from '@tiptap/pm/state'
import { describe, expect, it } from 'vitest'
import {
  INQTRIX_STRUCTURE_COMMAND_META,
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  UNSUPPORTED_SUGGESTION_REASONS,
  UnsupportedSuggestionStructureError,
  createEditorSchemaExtensions,
  parseEditorMarkdown,
  projectOriginalJson,
  suggestionDescriptors,
  transformToInqtrixSuggestionTransaction,
} from '../src/index'

// The suggest-mode transform refuses, before anything reaches the
// collaboration service, every change the service would reject. Each refusal
// carries a reason code the editor translates for the user.

const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
const ACTOR = 'user-actor'
const OTHER = 'user-other'

let nextSuggestion = 0
function suggestionId(): string {
  nextSuggestion += 1
  return `suggestion-${nextSuggestion}`
}

function stateFor(markdown: string): EditorState {
  return EditorState.create({ schema, doc: schema.nodeFromJSON(parseEditorMarkdown(markdown)) })
}

function suggest(state: EditorState, transaction: Transaction, authorId = ACTOR): EditorState {
  nextSuggestion += 1
  return state.apply(transformToInqtrixSuggestionTransaction(
    transaction,
    state,
    { authorId, createdAt: 1_784_112_000 + nextSuggestion, patchId: `patch-${nextSuggestion}` },
    suggestionId,
  ))
}

function textblockEnd(state: EditorState, needle: string): number {
  let found = -1
  state.doc.descendants((node, position) => {
    if (found >= 0) return false
    if (node.isTextblock && node.textContent.includes(needle)) {
      found = position + 1 + node.content.size
      return false
    }
    return true
  })
  if (found < 0) throw new Error(`fixture has no text block containing ${needle}`)
  return found
}

function textblockStart(state: EditorState, needle: string): number {
  let found = -1
  state.doc.descendants((node, position) => {
    if (found >= 0) return false
    if (node.isTextblock && node.textContent.includes(needle)) {
      found = position + 1
      return false
    }
    return true
  })
  if (found < 0) throw new Error(`fixture has no text block containing ${needle}`)
  return found
}

type Outcome =
  | { delivered: true }
  | { delivered: false; name: string; reason: unknown }

function outcome(run: () => unknown): Outcome {
  try {
    run()
    return { delivered: true }
  } catch (error) {
    return {
      delivered: false,
      name: error instanceof Error ? error.name : 'unknown',
      reason: error instanceof UnsupportedSuggestionStructureError ? error.reason : null,
    }
  }
}

function refusedWith(reason: string): Outcome {
  return { delivered: false, name: 'UnsupportedSuggestionStructureError', reason }
}

// Every case runs twice: once in a document where the actor has no other
// pending suggestion, once where the actor has one elsewhere. The reason must
// not depend on the rest of the document.
const OWN_SUGGESTION_ELSEWHERE = [false, true] as const

function withOptionalOwnSuggestion(state: EditorState, ownElsewhere: boolean): EditorState {
  if (!ownElsewhere) return state
  return suggest(state, state.tr.insertText('Y', textblockEnd(state, 'Elsewhere')))
}

describe('suggest-mode block boundaries', () => {
  const MARKDOWN = 'First paragraph with text.\n\nElsewhere.'

  it.each(OWN_SUGGESTION_ELSEWHERE)(
    'refuses Enter in the middle or at the start of a paragraph as moving content (own suggestion elsewhere: %s)',
    (ownElsewhere) => {
      const state = withOptionalOwnSuggestion(stateFor(MARKDOWN), ownElsewhere)
      const middle = textblockEnd(state, 'First') - 5
      const start = textblockStart(state, 'First')

      expect(outcome(() => suggest(state, state.tr.split(middle))))
        .toEqual(refusedWith('split_moves_content'))
      expect(outcome(() => suggest(state, state.tr.split(start))))
        .toEqual(refusedWith('split_moves_content'))
    },
  )

  it.each(OWN_SUGGESTION_ELSEWHERE)(
    'refuses splitting a list item whose nested list would move (own suggestion elsewhere: %s)',
    (ownElsewhere) => {
      const base = withOptionalOwnSuggestion(
        stateFor('- Item one\n  - Nested item\n- Item two\n\nElsewhere.'),
        ownElsewhere,
      )
      const state = base.apply(base.tr.setSelection(
        TextSelection.create(base.doc, textblockEnd(base, 'Item one')),
      ))
      let split: Transaction | null = null
      const listItem = schema.nodes.listItem
      if (!listItem) throw new Error('schema has no list item')
      splitListItem(listItem)(state, (transaction) => {
        split = transaction
      })
      if (!split) throw new Error('the list item could not be split')

      expect(outcome(() => suggest(state, split!))).toEqual(refusedWith('split_moves_content'))
    },
  )

  it.each(OWN_SUGGESTION_ELSEWHERE)(
    'refuses pasting two paragraphs into the middle of a paragraph (own suggestion elsewhere: %s)',
    (ownElsewhere) => {
      const state = withOptionalOwnSuggestion(stateFor(MARKDOWN), ownElsewhere)
      const paragraph = schema.nodes.paragraph
      if (!paragraph) throw new Error('schema has no paragraph')
      const middle = textblockEnd(state, 'First') - 5
      const paste = state.tr.replace(middle, middle, new Slice(Fragment.from([
        paragraph.create(null, schema.text('One')),
        paragraph.create(null, schema.text('Two')),
      ]), 1, 1))

      expect(outcome(() => suggest(state, paste))).toEqual(refusedWith('split_moves_content'))
    },
  )

  // A new block may only be a paragraph, a list item or an open task item:
  // the collaboration service refuses every other block an update adds.
  it.each(OWN_SUGGESTION_ELSEWHERE)(
    'refuses a new block of another kind at the end of a paragraph (own suggestion elsewhere: %s)',
    (ownElsewhere) => {
      const state = withOptionalOwnSuggestion(stateFor(MARKDOWN), ownElsewhere)
      const { codeBlock, heading, paragraph } = schema.nodes
      if (!codeBlock || !heading || !paragraph) throw new Error('schema has no heading or code block')
      const end = textblockEnd(state, 'First')
      const headings = state.tr.replace(end, end, new Slice(Fragment.from([
        heading.create({ level: 2 }, schema.text('One')),
        heading.create({ level: 2 }, schema.text('Two')),
      ]), 1, 1))
      const paragraphThenHeading = state.tr.replace(end, end, new Slice(Fragment.from([
        paragraph.create(null, schema.text('One')),
        heading.create({ level: 2 }, schema.text('Two')),
      ]), 1, 1))

      expect(outcome(() => suggest(state, headings)))
        .toEqual(refusedWith('proposed_block_unsupported'))
      expect(outcome(() => suggest(state, paragraphThenHeading)))
        .toEqual(refusedWith('proposed_block_unsupported'))
      expect(outcome(() => suggest(state, state.tr.split(end, 1, [{ type: codeBlock }]))))
        .toEqual(refusedWith('proposed_block_unsupported'))
    },
  )

  it('refuses a checked task item as a new block', () => {
    const state = stateFor('- [ ] Task one\n\nElsewhere.')
    const taskItem = schema.nodes.taskItem
    if (!taskItem) throw new Error('schema has no task item')
    const end = textblockEnd(state, 'Task one')
    expect(outcome(() => suggest(state, state.tr.split(end, 2, [
      { type: taskItem, attrs: { checked: true } },
      null,
    ])))).toEqual(refusedWith('proposed_block_unsupported'))
    expect(outcome(() => suggest(state, state.tr.split(end, 2, [
      { type: taskItem, attrs: { checked: false } },
      null,
    ])))).toEqual({ delivered: true })
  })

  it('delivers Enter at the end of a paragraph, at the end of a heading and inside own inserted text', () => {
    const state = stateFor('# Heading\n\nFirst paragraph.')
    const atParagraphEnd = suggest(state, state.tr.split(textblockEnd(state, 'First')))
    expect(atParagraphEnd.doc.childCount).toBe(state.doc.childCount + 1)
    expect(projectOriginalJson(atParagraphEnd.doc)).toEqual(state.doc.toJSON())

    // Enter at the end of a heading starts a paragraph, as the editor's
    // split does.
    const paragraph = schema.nodes.paragraph
    if (!paragraph) throw new Error('schema has no paragraph')
    const atHeadingEnd = suggest(state, state.tr.split(textblockEnd(state, 'Heading'), 1, [{ type: paragraph }]))
    expect(atHeadingEnd.doc.childCount).toBe(state.doc.childCount + 1)
    expect(atHeadingEnd.doc.child(1).type).toBe(paragraph)

    const typed = suggest(state, state.tr.insertText(' Added text', textblockEnd(state, 'First')))
    const insideOwn = suggest(typed, typed.tr.split(textblockEnd(typed, 'First') - 3))
    expect(insideOwn.doc.childCount).toBe(state.doc.childCount + 1)
    expect(projectOriginalJson(insideOwn.doc)).toEqual(state.doc.toJSON())
  })

  it('delivers Enter at the end of a list item and pasting two paragraphs at the end of a paragraph', () => {
    const list = stateFor('- Item one\n- Item two')
    const atItemEnd = list.apply(list.tr.setSelection(
      TextSelection.create(list.doc, textblockEnd(list, 'Item two')),
    ))
    let split: Transaction | null = null
    const listItem = schema.nodes.listItem
    if (!listItem) throw new Error('schema has no list item')
    splitListItem(listItem)(atItemEnd, (transaction) => {
      split = transaction
    })
    if (!split) throw new Error('the list item could not be split')
    expect(outcome(() => suggest(atItemEnd, split!))).toEqual({ delivered: true })

    const state = stateFor(MARKDOWN)
    const paragraph = schema.nodes.paragraph
    if (!paragraph) throw new Error('schema has no paragraph')
    const end = textblockEnd(state, 'First')
    const paste = state.tr.replace(end, end, new Slice(Fragment.from([
      paragraph.create(null, schema.text('One')),
      paragraph.create(null, schema.text('Two')),
    ]), 1, 1))
    expect(outcome(() => suggest(state, paste))).toEqual({ delivered: true })
  })
})

describe('suggest-mode block joins', () => {
  const JOINS = [
    '# Heading',
    '',
    'First paragraph.',
    '',
    'Second paragraph.',
    '',
    '- Bullet one',
    '- Bullet two',
    '',
    '1. Ordered one',
    '2. Ordered two',
  ].join('\n')

  // Backspace at the start of a text block, as the editor's keymap runs it.
  function backspaceAtStart(state: EditorState, needle: string): { placed: EditorState; transaction: Transaction } {
    const placed = state.apply(state.tr.setSelection(
      TextSelection.create(state.doc, textblockStart(state, needle)),
    ))
    let joined: Transaction | null = null
    joinBackward(placed, (transaction) => {
      joined = transaction
    })
    if (!joined) throw new Error(`Backspace at the start of ${needle} did nothing`)
    return { placed, transaction: joined }
  }

  it('delivers Backspace at the start of a paragraph after a paragraph and after a heading', () => {
    for (const needle of ['Second paragraph', 'First paragraph']) {
      const { placed, transaction } = backspaceAtStart(stateFor(JOINS), needle)
      expect(outcome(() => suggest(placed, transaction))).toEqual({ delivered: true })
    }
  })

  it('refuses Backspace that joins an ordered list to the bullet list before it', () => {
    // The suggestion transform represents this join with a whole new list
    // item that holds only a deleted marker character. The service admits a
    // new block only when it holds the actor's inserted text and continues
    // an insertion from the block before it, so the change is refused here.
    const { placed, transaction } = backspaceAtStart(stateFor(JOINS), 'Ordered one')
    expect(outcome(() => suggest(placed, transaction))).toEqual(refusedWith('unsupported_structure'))
  })

  it('refuses deleting text directly after or before an own proposed join', () => {
    // A proposed join keeps a deleted marker character at the end of the
    // first block and at the start of the second. Deleting text next to one
    // would merge the new deletion into the join and remove that marker,
    // which a suggestion may not remove; typing there is refused already.
    const { placed, transaction } = backspaceAtStart(stateFor(JOINS), 'Second paragraph')
    const joined = suggest(placed, transaction)
    const secondStart = textblockStart(joined, 'Second paragraph') + 1
    expect(outcome(() => suggest(joined, joined.tr.delete(secondStart, secondStart + 3))))
      .toEqual(refusedWith('adjacent_own_suggestion_conflict'))
    const firstEnd = textblockEnd(joined, 'First paragraph') - 1
    expect(outcome(() => suggest(joined, joined.tr.delete(firstEnd - 3, firstEnd))))
      .toEqual(refusedWith('adjacent_own_suggestion_conflict'))
    // A deletion that does not touch the markers is still a suggestion.
    const middle = textblockStart(joined, 'Second paragraph') + 8
    expect(outcome(() => suggest(joined, joined.tr.delete(middle, middle + 3)))).toEqual({ delivered: true })
  })

  it('refuses formatting a range that holds the marker of an own proposed join', () => {
    // Selecting to the end of the first block takes in the join's invisible
    // marker. The formatting suggestion would take the marker over from the
    // join, and rejecting everything would then no longer restore the
    // original document.
    const bold = schema.marks.bold
    if (!bold) throw new Error('schema has no bold mark')
    const { placed, transaction } = backspaceAtStart(stateFor(JOINS), 'Second paragraph')
    const joined = suggest(placed, transaction)
    const firstEnd = textblockEnd(joined, 'First paragraph')
    expect(outcome(() => suggest(joined, joined.tr.addMark(firstEnd - 10, firstEnd, bold.create()))))
      .toEqual(refusedWith('unsupported_structure'))
    expect(outcome(() => suggest(joined, joined.tr.addMark(firstEnd - 10, firstEnd - 4, bold.create()))))
      .toEqual({ delivered: true })
  })
})

describe('suggest-mode record rules', () => {
  const MARKDOWN = 'First paragraph.\n\nElsewhere.'

  it.each(OWN_SUGGESTION_ELSEWHERE)(
    'refuses deleting the last character of an own suggestion (own suggestion elsewhere: %s)',
    (ownElsewhere) => {
      const base = withOptionalOwnSuggestion(stateFor(MARKDOWN), ownElsewhere)
      const typed = suggest(base, base.tr.insertText('X', textblockEnd(base, 'First')))
      const end = textblockEnd(typed, 'First')

      expect(outcome(() => suggest(typed, typed.tr.delete(end - 1, end))))
        .toEqual(refusedWith('suggestion_would_disappear'))
    },
  )

  it.each(OWN_SUGGESTION_ELSEWHERE)(
    'refuses typing directly behind a foreign insertion (own suggestion elsewhere: %s)',
    (ownElsewhere) => {
      const base = stateFor(MARKDOWN)
      const foreign = withOptionalOwnSuggestion(
        suggest(base, base.tr.insertText(' foreign', textblockEnd(base, 'First')), OTHER),
        ownElsewhere,
      )
      const end = textblockEnd(foreign, 'First')

      expect(outcome(() => suggest(foreign, foreign.tr.insertText('w', end))))
        .toEqual(refusedWith('foreign_suggestion_changed'))
      expect(outcome(() => suggest(foreign, foreign.tr.split(end))))
        .toEqual(refusedWith('foreign_suggestion_changed'))
    },
  )

  it.each(OWN_SUGGESTION_ELSEWHERE)(
    'refuses formatting that spans original text and an own insertion (own suggestion elsewhere: %s)',
    (ownElsewhere) => {
      const base = withOptionalOwnSuggestion(stateFor(MARKDOWN), ownElsewhere)
      const typed = suggest(base, base.tr.insertText('XY', textblockEnd(base, 'First')))
      const end = textblockEnd(typed, 'First')
      const italic = schema.marks.italic
      if (!italic) throw new Error('schema has no italic mark')

      expect(outcome(() => suggest(typed, typed.tr.addMark(end - 6, end, italic.create()))))
        .toEqual(refusedWith('format_overlaps_own_suggestion'))
    },
  )

  // The upstream tracker gives a new deletion the id of an insertion right
  // after it, and new text the id of a deletion right after it. Joined that
  // way, the suggestion would change its kind, which the service refuses.
  it.each(OWN_SUGGESTION_ELSEWHERE)(
    'refuses deleting or typing directly at an own suggestion of another kind (own suggestion elsewhere: %s)',
    (ownElsewhere) => {
      const base = withOptionalOwnSuggestion(stateFor(MARKDOWN), ownElsewhere)
      const inserted = suggest(base, base.tr.insertText('XY', textblockEnd(base, 'First')))
      const beforeInsertion = textblockEnd(inserted, 'First') - 2
      expect(outcome(() => suggest(
        inserted,
        inserted.tr.delete(beforeInsertion - 1, beforeInsertion),
      ))).toEqual(refusedWith('adjacent_own_suggestion_conflict'))

      const end = textblockEnd(base, 'First')
      const deleted = suggest(base, base.tr.delete(end - 1, end))
      expect(outcome(() => suggest(deleted, deleted.tr.insertText('D', end - 1))))
        .toEqual(refusedWith('adjacent_own_suggestion_conflict'))
    },
  )

  it('refuses typing directly at a foreign deletion as a foreign change', () => {
    const base = stateFor(MARKDOWN)
    const end = textblockEnd(base, 'First')
    const deleted = suggest(base, base.tr.delete(end - 1, end), OTHER)

    expect(outcome(() => suggest(deleted, deleted.tr.insertText('D', end - 1))))
      .toEqual(refusedWith('foreign_suggestion_changed'))
  })

  it('still delivers typing and deleting inside an own insertion', () => {
    const base = stateFor(MARKDOWN)
    const typed = suggest(base, base.tr.insertText(' added', textblockEnd(base, 'First')))
    const end = textblockEnd(typed, 'First')
    const extended = suggest(typed, typed.tr.insertText('!', end))
    const corrected = suggest(extended, extended.tr.delete(end - 2, end - 1))

    expect(suggestionDescriptors(corrected.doc)).toHaveLength(1)
    expect(projectOriginalJson(corrected.doc)).toEqual(base.doc.toJSON())
  })
})

describe('suggest-mode structure suggestions stay applicable', () => {
  // A code block holds plain text only: formatting proposed inside a block
  // the actor proposed as a code block would leave that proposal impossible
  // to apply, which the collaboration service refuses.
  const proposedAsCode = (): EditorState => EditorState.create({
    schema,
    doc: schema.nodeFromJSON({
      type: 'doc',
      content: [
        {
          type: 'paragraph',
          attrs: {
            [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: {
              action: 'codeBlock',
              authorId: ACTOR,
              createdAt: 1_784_112_000,
              kind: 'structure',
              patchId: 'patch-code',
              suggestionId: 'suggestion-code',
            },
          },
          content: [{ type: 'text', text: 'Proposed as code.' }],
        },
        { type: 'paragraph', content: [{ type: 'text', text: 'Elsewhere.' }] },
      ],
    }),
  })

  it('refuses formatting inside a block the actor proposed as a code block', () => {
    const state = proposedAsCode()
    const bold = schema.marks.bold
    if (!bold) throw new Error('schema has no bold mark')

    expect(outcome(() => suggest(state, state.tr.addMark(2, 6, bold.create()))))
      .toEqual(refusedWith('structure_suggestion_inapplicable'))
    expect(outcome(() => suggest(state, state.tr.addMark(
      textblockEnd(state, 'Elsewhere') - 4,
      textblockEnd(state, 'Elsewhere'),
      bold.create(),
    )))).toEqual({ delivered: true })
    expect(outcome(() => suggest(state, state.tr.insertText('xy', 4)))).toEqual({ delivered: true })
  })

  it('delivers a change elsewhere while a proposal already cannot be applied', () => {
    const state = proposedAsCode()
    const bold = schema.marks.bold
    if (!bold) throw new Error('schema has no bold mark')
    const inapplicable = state.apply(state.tr.addMark(2, 6, bold.create()))

    expect(outcome(() => suggest(
      inapplicable,
      inapplicable.tr.insertText('!', textblockEnd(inapplicable, 'Elsewhere')),
    ))).toEqual({ delivered: true })
  })
})

describe('suggest-mode refusal reasons', () => {
  it('publishes the complete list of reason codes', () => {
    expect([...UNSUPPORTED_SUGGESTION_REASONS].sort()).toEqual([
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
    ])
  })

  it('names the reason of every structure command it refuses', () => {
    // A structure command is only transformed when it changes the document,
    // as the editor's own commands do.
    const state = stateFor('First paragraph.')
    const command = (action: string) => state.tr
      .insertText('x', 1)
      .setMeta(INQTRIX_STRUCTURE_COMMAND_META, { action })
    const inList = stateFor('- Item one')

    expect(outcome(() => suggest(state, command('table'))))
      .toEqual(refusedWith('table_topology_unsupported'))
    expect(outcome(() => suggest(state, command('divider'))))
      .toEqual(refusedWith('divider_unsupported'))
    expect(outcome(() => suggest(inList, inList.tr
      .setSelection(TextSelection.create(inList.doc, 3))
      .insertText('x', 3)
      .setMeta(INQTRIX_STRUCTURE_COMMAND_META, { action: 'bulletList' }))))
      .toEqual(refusedWith('structure_target_unsupported'))
  })

  it('names the reason of refused table topology changes and block insertions', () => {
    const table = stateFor('| A | B |\n| --- | --- |\n| 1 | 2 |')
    const row = table.doc.firstChild?.child(1)
    if (!row) throw new Error('fixture has no table row')
    const tableRow = schema.nodes.tableRow
    if (!tableRow) throw new Error('schema has no table row')
    const rowStart = 1 + (table.doc.firstChild?.child(0).nodeSize ?? 0)
    const rowCopy = tableRow.create(row.attrs, row.content)
    expect(outcome(() => suggest(table, table.tr.insert(rowStart, rowCopy))))
      .toEqual(refusedWith('table_topology_unsupported'))

    const state = stateFor('First paragraph.')
    const paragraph = schema.nodes.paragraph
    if (!paragraph) throw new Error('schema has no paragraph')
    expect(outcome(() => suggest(
      state,
      state.tr.insert(state.doc.content.size, paragraph.create(null, schema.text('Second'))),
    ))).toEqual(refusedWith('node_marks_unsupported'))
  })
})

describe('structure suggestion attribute', () => {
  it('stays on the original block when Enter splits a block that carries it', () => {
    const data = {
      action: 'heading2',
      authorId: ACTOR,
      createdAt: 1_784_112_000,
      kind: 'structure',
      patchId: 'patch-structure',
      suggestionId: 'suggestion-structure',
    }
    const editor = new Editor({
      content: {
        type: 'doc',
        content: [{
          attrs: { [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: data, textAlign: 'center' },
          content: [{ text: 'Proposed heading', type: 'text' }],
          type: 'paragraph',
        }],
      },
      element: null,
      extensions: createEditorSchemaExtensions({ enableUndoRedo: false }),
    })
    try {
      editor.commands.setTextSelection(editor.state.doc.content.size - 1)
      expect(editor.commands.splitBlock()).toBe(true)
      const blocks = editor.getJSON().content ?? []

      expect(blocks).toHaveLength(2)
      expect(blocks[0]?.attrs?.[INQTRIX_STRUCTURE_SUGGESTION_ATTR]).toEqual(data)
      expect(blocks[1]?.attrs?.[INQTRIX_STRUCTURE_SUGGESTION_ATTR]).toBeNull()
      // Plain block attributes still follow the split.
      expect(blocks[1]?.attrs?.textAlign).toBe('center')
    } finally {
      editor.destroy()
    }
  })
})
