import { getSchema } from '@tiptap/core'
import { EditorState } from '@tiptap/pm/state'
import { describe, expect, it } from 'vitest'
import {
  createEditorSchemaExtensions,
  suggestionCreatedAtMs,
  suggestionDescriptors,
  transformToInqtrixSuggestionTransaction,
} from '../src/index'

// One moment in Unix seconds; the stamps below lie 10, 20 and 30 seconds
// after it, written in the unit their writer uses.
const BASE_SECONDS = 1_790_942_600

describe('the creation time of a suggestion', () => {
  it('reads Unix seconds and epoch milliseconds as one unit', () => {
    expect(suggestionCreatedAtMs(BASE_SECONDS)).toBe(1_790_942_600_000)
    expect(suggestionCreatedAtMs(1_790_942_610_250)).toBe(1_790_942_610_250)
  })

  it('orders suggestions by when they were made, whichever unit stamped them', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const paragraphs = ['P1', 'P2', 'P3', 'P4']
    let state = EditorState.create({
      doc: schema.node('doc', null, paragraphs.map((text) => (
        schema.node('paragraph', null, schema.text(text))
      ))),
      schema,
    })
    // Paragraph by paragraph, from the last to the first so the positions of
    // the earlier ones stay put. The suggestion ids sort in the opposite
    // order of the times, so an order by id cannot pass for an order by time.
    const stamps = [
      { createdAt: BASE_SECONDS, suggestionId: 's-d' },
      { createdAt: (BASE_SECONDS + 10) * 1_000, suggestionId: 's-c' },
      { createdAt: BASE_SECONDS + 20, suggestionId: 's-b' },
      { createdAt: (BASE_SECONDS + 30) * 1_000, suggestionId: 's-a' },
    ]
    for (const [index, { createdAt, suggestionId }] of [...stamps.entries()].reverse()) {
      const endOfParagraph = index * 4 + 3
      state = state.apply(transformToInqtrixSuggestionTransaction(
        state.tr.insertText('!', endOfParagraph),
        state,
        { authorId: 'user-1', createdAt, patchId: `patch-${suggestionId}` },
        () => suggestionId,
      ))
    }

    const descriptors = suggestionDescriptors(state.doc)

    expect(descriptors.map((descriptor) => descriptor.suggestionId))
      .toEqual(['s-d', 's-c', 's-b', 's-a'])
    // Each suggestion keeps the time as it was written.
    expect(descriptors.map((descriptor) => descriptor.createdAt)).toEqual([
      1_790_942_600,
      1_790_942_610_000,
      1_790_942_620,
      1_790_942_630_000,
    ])
  })
})
