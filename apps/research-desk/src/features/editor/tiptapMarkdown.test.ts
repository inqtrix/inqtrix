import { createEditorSchemaExtensions } from '@inqtrix/editor-schema'
import { Editor as HeadlessEditor, getSchema, type JSONContent } from '@tiptap/core'
import type { Editor } from '@tiptap/react'
import { describe, expect, it } from 'vitest'
import {
  normalizeEditorMarkdownForTiptap,
  serializeEditorFinalProjectionMarkdown,
  serializeEditorMarkdown,
} from './tiptap'

/** Unit Separator (U+001F) that older versions of `@tiptap/markdown` left
 * inside populated table cells. Documents stored with it still carry it, and
 * it breaks GFM table re-parsing. */
const SEPARATOR = String.fromCharCode(0x1f)

const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

/** An editor whose Markdown serializer returns *markdown* verbatim. */
function editorReturning(markdown: string): Editor {
  return {
    getJSON: () => ({ type: 'doc', content: [{ type: 'paragraph' }] }),
    markdown: { serialize: () => markdown },
    schema,
  } as unknown as Editor
}

/** A table whose header row reads Quelle | Wert and whose body row holds
 * the two given cells. */
function tableDocument(source: JSONContent[], value: JSONContent[]): JSONContent {
  const paragraph = (text: string): JSONContent => ({ type: 'paragraph', content: [{ type: 'text', text }] })
  return {
    type: 'doc',
    content: [{
      type: 'table',
      content: [
        { type: 'tableRow', content: [
          { type: 'tableHeader', content: [paragraph('Quelle')] },
          { type: 'tableHeader', content: [paragraph('Wert')] },
        ] },
        { type: 'tableRow', content: [
          { type: 'tableCell', content: source },
          { type: 'tableCell', content: value },
        ] },
      ],
    }],
  }
}

function openEditor(content: JSONContent | string): HeadlessEditor {
  return new HeadlessEditor({
    content,
    ...(typeof content === 'string' ? { contentType: 'markdown' as const } : {}),
    element: null,
    extensions: createEditorSchemaExtensions({ enableUndoRedo: false }),
    injectCSS: false,
  })
}

describe('serializeEditorMarkdown', () => {
  it('saves a populated table as plain GFM, each line break of a cell as <br>, and reads it back', () => {
    const text = (value: string): JSONContent => ({ type: 'text', text: value })
    const editor = openEditor(tableDocument(
      [{ type: 'paragraph', content: [text('Bericht'), { type: 'hardBreak' }, text('[K1]')] }],
      [{ type: 'paragraph', content: [text('12')] }, { type: 'paragraph', content: [text('Stand 2026')] }],
    ))
    try {
      const saved = serializeEditorMarkdown(editor as unknown as Editor)
      expect(saved).not.toContain(SEPARATOR)
      expect(saved).toBe([
        '',
        '| Quelle          | Wert             |',
        '| --------------- | ---------------- |',
        '| Bericht<br>[K1] | 12<br>Stand 2026 |',
        '',
      ].join('\n'))
      const reopened = openEditor(saved)
      try {
        // A cell is one line: its hard break comes back as it was, and its
        // second paragraph as a line of the first one.
        const expected = openEditor(tableDocument(
          [{ type: 'paragraph', content: [text('Bericht'), { type: 'hardBreak' }, text('[K1]')] }],
          [{ type: 'paragraph', content: [text('12'), { type: 'hardBreak' }, text('Stand 2026')] }],
        ))
        try {
          expect(reopened.getJSON()).toEqual(expected.getJSON())
        } finally {
          expected.destroy()
        }
        expect(serializeEditorMarkdown(reopened as unknown as Editor)).toBe(saved)
      } finally {
        reopened.destroy()
      }
    } finally {
      editor.destroy()
    }
  })

  it('leaves artifact-free markdown unchanged', () => {
    const markdown = '# Title\n\nBody text with no tables.'
    expect(serializeEditorMarkdown(editorReturning(markdown))).toBe(markdown)
  })

  it('projects a collaboration replacement to its canonical final markdown', () => {
    const metadata = {
      authorId: 'user-1',
      createdAt: 10,
      patchId: 'patch-1',
      suggestionId: 'suggestion-1',
    }
    const editor = new HeadlessEditor({
      content: {
        content: [{
          content: [
            {
              marks: [{
                attrs: { ...metadata, id: metadata.suggestionId, kind: 'deletion' },
                type: 'deletion',
              }],
              text: 'old',
              type: 'text',
            },
            {
              marks: [{
                attrs: { ...metadata, id: metadata.suggestionId, kind: 'insertion' },
                type: 'insertion',
              }],
              text: 'new',
              type: 'text',
            },
          ],
          type: 'paragraph',
        }],
        type: 'doc',
      },
      element: null,
      extensions: createEditorSchemaExtensions({ enableUndoRedo: false }),
      injectCSS: false,
    })

    try {
      expect(serializeEditorFinalProjectionMarkdown(editor)).toBe('new')
    } finally {
      editor.destroy()
    }
  })
})

describe('normalizeEditorMarkdownForTiptap', () => {
  it('heals legacy documents that still carry the table-cell separator on load', () => {
    expect(normalizeEditorMarkdownForTiptap(`| A${SEPARATOR} | B${SEPARATOR} |`)).toBe('| A | B |')
  })

  it('still normalizes block and inline math delimiters', () => {
    expect(normalizeEditorMarkdownForTiptap('\\[ a^2 \\]')).toBe('\n\n$$\na^2\n$$\n\n')
    expect(normalizeEditorMarkdownForTiptap('\\( b \\)')).toBe('$b$')
  })
})
