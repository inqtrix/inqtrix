import type { JSONContent } from '@tiptap/core'
import { describe, expect, it } from 'vitest'

import {
  canonicalizeEditorJson,
  parseEditorMarkdown,
  serializeEditorJson,
} from '../src/markdown.js'
import { editorJsonToYDoc, editorYDocToJson } from '../src/transformer.js'

// A table cell is one Markdown line, so the serializer writes a line break
// inside a cell (a hard break, or the break between two paragraphs of one
// cell) as `<br>`. These tests run without a DOM, the way the collaboration
// service reads Markdown when it activates a document or publishes a
// suggestion.

const text = (value: string, ...marks: string[]): JSONContent => (
  marks.length === 0
    ? { type: 'text', text: value }
    : { type: 'text', text: value, marks: marks.map((type) => (
        type === 'link' ? { type, attrs: { href: 'https://example.org' } } : { type }
      )) }
)
const hardBreak: JSONContent = { type: 'hardBreak' }
const paragraph = (...content: JSONContent[]): JSONContent => ({ type: 'paragraph', content })

/** A table with one header cell over one body cell holding *cell*. */
function tableDocument(...cell: JSONContent[]): JSONContent {
  return {
    type: 'doc',
    content: [{
      type: 'table',
      content: [
        { type: 'tableRow', content: [{ type: 'tableHeader', content: [paragraph(text('Head'))] }] },
        { type: 'tableRow', content: [{ type: 'tableCell', content: cell }] },
      ],
    }],
  }
}

/** The inline content of the body cell of a table read from *markdown*. */
function bodyCell(markdown: string): JSONContent[] {
  const table = parseEditorMarkdown(markdown).content?.[0]
  expect(table?.type).toBe('table')
  const cell = table?.content?.[1]?.content?.[0]
  expect(cell?.content).toHaveLength(1)
  return cell?.content?.[0]?.content ?? []
}

/** Inline content by node type, text and mark types, without mark attributes. */
function shape(content: readonly JSONContent[]): unknown[] {
  return content.map((node) => (
    node.type === 'text'
      ? [node.text, ...(node.marks ?? []).map((mark) => mark.type)]
      : node.type
  ))
}

/** Every node of *document* whose type is *type*. */
function nodesOf(document: JSONContent, type: string): JSONContent[] {
  const found: JSONContent[] = []
  const visit = (node: JSONContent): void => {
    if (node.type === type) found.push(node)
    for (const child of node.content ?? []) visit(child)
  }
  visit(document)
  return found
}

describe('a line break inside a table cell', () => {
  it('reads as the hard break the serializer wrote, not as the text <br>', () => {
    expect(bodyCell('| Head |\n| --- |\n| a<br>b |')).toEqual([text('a'), hardBreak, text('b')])
  })

  it.each([
    ['<br>', '| Head |\n| --- |\n| a<br>b |', [text('a'), hardBreak, text('b')]],
    ['<br/>', '| Head |\n| --- |\n| a<br/>b |', [text('a'), hardBreak, text('b')]],
    ['<br />', '| Head |\n| --- |\n| a<br />b |', [text('a'), hardBreak, text('b')]],
    ['two breaks and a break at either end', '| Head |\n| --- |\n| <br>a<br><br>b<br> |', [hardBreak, text('a'), hardBreak, hardBreak, text('b'), hardBreak]],
    ['a break inside bold text', '| Head |\n| --- |\n| **a<br>b** |', [text('a', 'bold'), hardBreak, text('b', 'bold')]],
    ['a break inside link text', '| Head |\n| --- |\n| [a<br>b](https://example.org) |', [text('a', 'link'), hardBreak, text('b', 'link')]],
  ] as const)('reads %s as a hard break', (_name, markdown, expected) => {
    expect(shape(bodyCell(markdown))).toEqual(shape(expected))
  })

  it('reads as a hard break in a header cell as well', () => {
    const table = parseEditorMarkdown('| a<br>b |\n| --- |\n| c |').content?.[0]
    expect(table?.content?.[0]?.content?.[0]?.content?.[0]?.content).toEqual([text('a'), hardBreak, text('b')])
  })

  it('reads a cell the editor wrote back as the same document, and the collaboration document holds it', () => {
    const written = tableDocument(paragraph(hardBreak, text('first'), hardBreak, hardBreak, text('second [K1]'), hardBreak))
    const markdown = serializeEditorJson(written)
    expect(markdown).toContain('| <br>first<br><br>second [K1]<br> |')

    const read = parseEditorMarkdown(markdown)
    expect(read).toEqual(canonicalizeEditorJson(written))
    // What activating the Markdown gives every browser of the document.
    expect(editorYDocToJson(editorJsonToYDoc(read))).toEqual(canonicalizeEditorJson(written))
    expect(serializeEditorJson(read)).toBe(markdown)
  })

  it('keeps two paragraphs of one cell apart by a hard break', () => {
    // A cell is one line, so the second paragraph comes back as a line of
    // the first one: the text stays apart instead of running together.
    const markdown = serializeEditorJson(tableDocument(paragraph(text('first')), paragraph(text('second'))))
    expect(markdown).toContain('| first<br>second |')
    expect(bodyCell(markdown)).toEqual([text('first'), hardBreak, text('second')])
  })
})

describe('the table-cell line break rule reads nothing else', () => {
  it.each([
    ['an upper-case tag', '<BR>'],
    ['a tag with a space before its end', '<br >'],
    ['a tag with an attribute', '<br class="x">'],
    ['a tag with an event handler', '<br onclick="alert(1)">'],
    ['a closing tag', '</br>'],
    ['a span', '<span>u</span>'],
    ['an image with an event handler', '<img src="x" onerror="alert(1)">'],
  ])('keeps %s inside a table cell as text', (_name, html) => {
    const read = parseEditorMarkdown(`| Head |\n| --- |\n| a${html}b |`)
    expect(nodesOf(read, 'hardBreak')).toEqual([])
    expect(nodesOf(read, 'image')).toEqual([])
    expect(nodesOf(read, 'text').map((node) => node.text).join('')).toContain(html)
  })

  it('keeps a break inside a code span of a table cell as code text', () => {
    expect(bodyCell('| Head |\n| --- |\n| `a<br>b` |')).toEqual([text('a<br>b', 'code')])
  })

  it.each([
    ['a paragraph', 'a<br>b'],
    ['a heading', '# a<br>b'],
    ['a list item', '- a<br>b'],
    ['a quote', '> a<br>b'],
  ])('keeps a break in %s outside any table as text', (_name, markdown) => {
    const read = parseEditorMarkdown(markdown)
    expect(nodesOf(read, 'hardBreak')).toEqual([])
    expect(nodesOf(read, 'text').map((node) => node.text).join('')).toBe('a<br>b')
  })

  it('reads text that spells a break, written by the editor, back as that text', () => {
    // The serializer escapes the angle brackets of typed text, so a user's
    // literal `<br>` never reaches the reader as HTML.
    const written = tableDocument(paragraph(text('x <br> y')))
    const markdown = serializeEditorJson(written)
    expect(markdown).toContain('| x &lt;br&gt; y |')
    expect(parseEditorMarkdown(markdown)).toEqual(canonicalizeEditorJson(written))
  })
})
