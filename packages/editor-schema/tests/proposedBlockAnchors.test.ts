import { getSchema } from '@tiptap/core'
import { splitListItem } from '@tiptap/pm/schema-list'
import { EditorState, TextSelection, type Transaction } from '@tiptap/pm/state'
import { describe, expect, it } from 'vitest'
import {
  createEditorSchemaExtensions,
  parseEditorMarkdown,
  projectFinalJson,
  projectOriginalJson,
  proposedBlockAnchorPositions,
  serializeEditorJson,
  transformToInqtrixSuggestionTransaction,
} from '../src/index'

// A proposed paragraph break carries a zero-width space on each side of the
// boundary, the only content the upstream tracker can mark for an otherwise
// empty insertion. Once the break is taken over, those anchors are no
// content anyone typed: the final projection, and with it every export and
// change summary, must not contain them. A zero-width space a person typed
// on purpose stays.

const ZERO_WIDTH_SPACE = '\u200B'
const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
const AUTHOR = 'user-anchor'

let nextSuggestion = 0

function stateFor(markdown: string): EditorState {
  return EditorState.create({ schema, doc: schema.nodeFromJSON(parseEditorMarkdown(markdown)) })
}

function suggest(state: EditorState, transaction: Transaction, authorId = AUTHOR): EditorState {
  nextSuggestion += 1
  const patch = nextSuggestion
  return state.apply(transformToInqtrixSuggestionTransaction(
    transaction,
    state,
    { authorId, createdAt: 1_784_112_000 + patch, patchId: `patch-${patch}` },
    () => {
      nextSuggestion += 1
      return `suggestion-${nextSuggestion}`
    },
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

function anchorCount(text: string): number {
  return text.split(ZERO_WIDTH_SPACE).length - 1
}

function finalText(state: EditorState): string {
  return JSON.stringify(projectFinalJson(state.doc))
}

const MARKDOWN = 'First paragraph.\n\nSecond paragraph.'

// Each case: the markdown, the suggestion to build, and how many zero-width
// spaces a person typed on purpose.
const cases: Array<[string, string, (state: EditorState) => EditorState, number]> = [
  ['Enter at the end of a paragraph', MARKDOWN, (state) => (
    suggest(state, state.tr.split(textblockEnd(state, 'First')))
  ), 0],
  ['Enter at the end of a paragraph, then typing', MARKDOWN, (state) => {
    const split = suggest(state, state.tr.split(textblockEnd(state, 'First')))
    return suggest(split, split.tr.insertText('New', textblockEnd(split, 'First') + 3))
  }, 0],
  ['Enter twice at the end of a paragraph', MARKDOWN, (state) => {
    const split = suggest(state, state.tr.split(textblockEnd(state, 'First')))
    return suggest(split, split.tr.split(textblockEnd(split, 'First') + 3))
  }, 0],
  ['Enter inside own inserted text', MARKDOWN, (state) => {
    const typed = suggest(state, state.tr.insertText(' Added', textblockEnd(state, 'First')))
    return suggest(typed, typed.tr.split(textblockEnd(typed, 'First') - 2))
  }, 0],
  ['Enter at the end of a list item, then typing', '- Item one\n- Item two\n\nAfter.', (state) => {
    const atEnd = state.apply(state.tr.setSelection(
      TextSelection.create(state.doc, textblockEnd(state, 'Item two')),
    ))
    const listItem = schema.nodes.listItem
    if (!listItem) throw new Error('schema has no list item')
    let split: Transaction | null = null
    splitListItem(listItem)(atEnd, (transaction) => {
      split = transaction
    })
    if (!split) throw new Error('the list item could not be split')
    const proposed = suggest(atEnd, split)
    return suggest(proposed, proposed.tr.insertText('Item', textblockEnd(proposed, 'Item two') + 5))
  }, 0],
  ['Enter, then typing at the end of the original paragraph (anchor now mid-text)', MARKDOWN, (state) => {
    const split = suggest(state, state.tr.split(textblockEnd(state, 'First')))
    return suggest(split, split.tr.insertText('abc', textblockEnd(split, 'First')))
  }, 0],
  ['Enter, typing in the new paragraph, then at the end of the original one', MARKDOWN, (state) => {
    const split = suggest(state, state.tr.split(textblockEnd(state, 'First')))
    const typed = suggest(split, split.tr.insertText('New', textblockEnd(split, 'First') + 3))
    return suggest(typed, typed.tr.insertText('abc', textblockEnd(typed, 'First')))
  }, 0],
  ['Enter, then typing at the start of the new paragraph before its anchor', MARKDOWN, (state) => {
    const split = suggest(state, state.tr.split(textblockEnd(state, 'First')))
    return suggest(split, split.tr.insertText('xy', textblockEnd(split, 'First') + 2))
  }, 0],
  ['a zero-width space typed on purpose inside the new paragraph', MARKDOWN, (state) => {
    const split = suggest(state, state.tr.split(textblockEnd(state, 'First')))
    return suggest(split, split.tr.insertText(`Ne${ZERO_WIDTH_SPACE}w`, textblockEnd(split, 'First') + 3))
  }, 1],
  ['a zero-width space typed on purpose at the end of an insertion without a break', MARKDOWN, (state) => (
    suggest(state, state.tr.insertText(` new${ZERO_WIDTH_SPACE}`, textblockEnd(state, 'First')))
  ), 1],
]

describe('proposed block anchors', () => {
  it.each(cases)('are absent from the final projection: %s', (_name, markdown, build, typedOnPurpose) => {
    const state = stateFor(markdown)
    const proposed = build(state)

    expect(anchorCount(JSON.stringify(proposed.doc.toJSON()))).toBeGreaterThanOrEqual(typedOnPurpose)
    expect(anchorCount(finalText(proposed))).toBe(typedOnPurpose)
    // The character kept is the one typed on purpose, not an anchor.
    if (typedOnPurpose > 0) expect(finalText(proposed)).toMatch(/Ne\u200Bw|new\u200B/)
    expect(anchorCount(serializeEditorJson(proposed.doc.toJSON(), 'final'))).toBe(typedOnPurpose)
    // Rejecting everything still restores the original document exactly.
    expect(projectOriginalJson(proposed.doc)).toEqual(state.doc.toJSON())
  })

  it('locate only the anchors of the selected suggestion', () => {
    const state = stateFor(MARKDOWN)
    const first = suggest(state, state.tr.split(textblockEnd(state, 'First')))
    const both = suggest(first, first.tr.split(textblockEnd(first, 'Second')), 'user-other')
    const ids = new Set<string>()
    both.doc.descendants((node) => {
      for (const mark of node.marks) {
        if (mark.type.name === 'insertion') ids.add(String(mark.attrs.suggestionId))
      }
    })
    expect(ids.size).toBe(2)
    const [firstId] = [...ids]

    const all = proposedBlockAnchorPositions(both.doc)
    const selected = proposedBlockAnchorPositions(both.doc, (id) => id === firstId)
    expect(all).toHaveLength(4)
    expect(selected).toHaveLength(2)
    for (const position of all) {
      expect(both.doc.textBetween(position, position + 1)).toBe(ZERO_WIDTH_SPACE)
    }
    expect(selected.every((position) => all.includes(position))).toBe(true)
  })
})
