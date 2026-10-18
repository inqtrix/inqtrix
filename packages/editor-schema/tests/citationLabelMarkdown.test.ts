import type { JSONContent } from '@tiptap/core'
import { getSchema } from '@tiptap/core'
import { MarkdownManager } from '@tiptap/markdown'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { createEditorSchemaExtensions } from '../src/extensions.js'
import {
  parseEditorMarkdown,
  serializeEditorJson,
  serializeWithRawCitationLabels,
  setCitationLabelsLeftEscapedReporter,
} from '../src/markdown.js'
import { projectFinalDocument } from '../src/suggestions.js'

const extensions = createEditorSchemaExtensions({ enableUndoRedo: false })
const schema = getSchema(extensions)
// The serializer exactly as it runs without the label protection: the
// "old" side of every comparison below.
const escapingManager = new MarkdownManager({ extensions, markedOptions: { gfm: true } })

function serializeEscaping(content: JSONContent): string {
  const projected = projectFinalDocument(schema.nodeFromJSON(content))
  return escapingManager.serialize(projected.toJSON())
}

const suggestionMetadata = {
  authorId: 'user-1',
  createdAt: 10,
  id: 'suggestion-1',
  patchId: 'patch-1',
  suggestionId: 'suggestion-1',
}

function text(value: string, ...marks: string[]): JSONContent {
  if (marks.length === 0) return { type: 'text', text: value }
  return {
    type: 'text',
    text: value,
    marks: marks.map((type) => (
      type === 'link'
        ? { type, attrs: { href: 'https://example.org' } }
        : type === 'insertion'
          ? { type, attrs: { ...suggestionMetadata, kind: 'insertion' } }
          : { type }
    )),
  }
}

function paragraph(...content: JSONContent[]): JSONContent {
  return { type: 'paragraph', content }
}

function doc(...content: JSONContent[]): JSONContent {
  return { type: 'doc', content }
}

function markdownDoc(markdown: string): JSONContent {
  return parseEditorMarkdown(markdown)
}

function listOf(type: 'bulletList' | 'orderedList', ...items: JSONContent[][]): JSONContent {
  return {
    type,
    ...(type === 'orderedList' ? { attrs: { start: 1 } } : {}),
    content: items.map((content) => ({ type: 'listItem', content })),
  }
}

/** A table whose body row holds one cell of several paragraphs; with
 * *header*, the header row holds it instead. */
function tableWithCellParagraphs(paragraphs: string[], header = false): JSONContent {
  const cell = (type: string, ...content: JSONContent[]): JSONContent => ({ type, content })
  const paragraphsOf = (values: string[]) => values.map((value) => paragraph(text(value)))
  return doc({
    type: 'table',
    content: [
      cell(
        'tableRow',
        cell('tableHeader', ...paragraphsOf(header ? paragraphs : ['H'])),
        cell('tableHeader', paragraph(text('I'))),
      ),
      cell(
        'tableRow',
        cell('tableCell', ...paragraphsOf(header ? ['x'] : paragraphs)),
        cell('tableCell', paragraph(text('y'))),
      ),
    ],
  })
}

afterEach(() => {
  vi.restoreAllMocks()
  setCitationLabelsLeftEscapedReporter(null)
})

describe('citation labels at the Markdown serializer exit', () => {
  it('writes labels raw in prose, lists, tables, headings, quotes and bold text', () => {
    const cases: Array<[string, string]> = [
      ['Satz [K1].', 'Satz [K1].'],
      ['Satz [K2, W7].', 'Satz [K2, W7].'],
      ['Satz [K2; W7].', 'Satz [K2; W7].'],
      ['Angrenzend [K2][W7] bleibt.', 'Angrenzend [K2][W7] bleibt.'],
      ['## Titel [K1]', '## Titel [K1]'],
      ['# [K1]: Titel', '# [K1]: Titel'],
      [
        '- Punkt [K1]\n\n1. Punkt [W2]\n\n- [ ] Aufgabe [K3]',
        '- Punkt [K1]\n\n1. Punkt [W2]\n\n- [ ] Aufgabe [K3]',
      ],
      ['> Zitat [W7]', '> Zitat [W7]'],
      ['Satz **fett [K1]** Ende.', 'Satz **fett [K1]** Ende.'],
      ['Quelle [K1]: siehe oben', 'Quelle [K1]: siehe oben'],
      [
        '| A | B |\n| --- | --- |\n| x [K1] | [W7]: y |',
        '\n| A      | B       |\n| ------ | ------- |\n| x [K1] | [W7]: y |\n',
      ],
    ]
    for (const [markdown, expected] of cases) {
      expect(serializeEditorJson(markdownDoc(markdown)), markdown).toBe(expected)
    }
    expect(serializeEditorJson(doc(paragraph(text('Satz '), text('[K1]', 'bold')))))
      .toBe('Satz **[K1]**')
  })

  it('keeps the escape where a raw bracket would open an inline link', () => {
    expect(serializeEditorJson(doc(paragraph(text('Studie [K1](2020) Ende')))))
      .toBe('Studie \\[K1\\](2020) Ende')
    // A suggestion mark writes no delimiter of its own: its `(` lands right
    // after the label in the output even though it is another text node.
    const insertion = doc(paragraph(text('Satz [K1]'), text('(x)', 'insertion')))
    expect(serializeWithRawCitationLabels(
      insertion,
      schema,
      (content) => escapingManager.serialize(content),
    )).toBe('Satz \\[K1\\](x)')
    // An emphasis delimiter in between opens no link: the label stays raw.
    expect(serializeEditorJson(doc(paragraph(text('Satz [K1]'), text('(x)', 'italic')))))
      .toBe('Satz [K1]*(x)*')
    // Two paragraphs of one table cell are written with a line break
    // between them: the `(` of the second one does not follow the label, so
    // the label stays raw.
    expect(serializeEditorJson(tableWithCellParagraphs(['a [K1]', '(2020)'])))
      .toContain('| a [K1]<br>(2020) |')
  })

  it('keeps the escape where a raw bracket would open a link reference definition', () => {
    expect(serializeEditorJson(doc(
      paragraph(text('[K1]: foo')),
      paragraph(text('Weiter [K1] hier')),
    ))).toBe('\\[K1\\]: foo\n\nWeiter [K1] hier')
    expect(serializeEditorJson(doc({
      type: 'blockquote',
      content: [paragraph(text('[K1]: foo'))],
    }))).toBe('> \\[K1\\]: foo')
    expect(serializeEditorJson(doc(listOf('bulletList', [paragraph(text('[K1]: foo'))]))))
      .toBe('- \\[K1\\]: foo')
    expect(serializeEditorJson(doc(listOf('orderedList', [paragraph(text('[K2, W7]: foo'))]))))
      .toBe('1. \\[K2, W7\\]: foo')
    expect(serializeEditorJson(markdownDoc('- [ ] [K1]: Aufgabe'))).toBe('- [ ] \\[K1\\]: Aufgabe')
    expect(serializeEditorJson(doc(paragraph(text('Zeile'), { type: 'hardBreak' }, text('[K1]: foo')))))
      .toBe('Zeile  \n\\[K1\\]: foo')
  })

  it('leaves link text, code, formulas and non-label brackets as the serializer writes them', () => {
    const cases: JSONContent[] = [
      doc(paragraph(text('siehe [K1]', 'link'))),
      doc(paragraph(text('[K1](x)', 'code'))),
      markdownDoc('Code `[K1](x)` und\n\n```\n[K1](x) \\[K2\\]\n```'),
      markdownDoc('Formel $x_[1]$ und\n\n$$\n[K1] + a_b\n$$'),
      markdownDoc('Ein Platzhalter [Marke] im Satz.'),
      markdownDoc('x [k1] und [K3–K5] und [K2 und W7] und [unbelegt: K2]'),
      doc(paragraph(text('['), text('K1', 'bold'), text(']'))),
    ]
    for (const content of cases) {
      expect(serializeEditorJson(content)).toBe(serializeEscaping(content))
    }
    expect(serializeEditorJson(doc(paragraph(text('siehe [K1]', 'link')))))
      .toBe('[siehe \\[K1\\]](https://example.org)')
  })

  it('leaves a label escaped whose bracket follows a backslash in the text', () => {
    const content = doc(paragraph(text('literal \\[K1\\] und \\[K1] und \\\\[K1]')))
    expect(serializeEditorJson(content)).toBe(serializeEscaping(content))
    expect(serializeEditorJson(content)).toBe(
      'literal \\\\\\[K1\\\\\\] und \\\\\\[K1\\] und \\\\\\\\\\[K1\\]',
    )
  })

  it('turns a stored escaped label into a citation on the next serialization', () => {
    // A known limit: parsing drops the escape, so the document cannot tell
    // a deliberately written `\[K1\]` from a citation, and the next save
    // writes it raw.
    const stored = 'Bewusst \\[K1\\] geschrieben.'
    expect(parseEditorMarkdown(stored)).toEqual(parseEditorMarkdown('Bewusst [K1] geschrieben.'))
    expect(serializeEditorJson(parseEditorMarkdown(stored))).toBe('Bewusst [K1] geschrieben.')
  })

  it('tells an installed reporter instead of the console when labels stay escaped', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const report = vi.fn()
    setCitationLabelsLeftEscapedReporter(report)

    const markdown = serializeEditorJson(doc(paragraph(text('Satz [K1] und \uFDD0.'))))

    expect(markdown).toBe('Satz \\[K1\\] und \uFDD0.')
    expect(report).toHaveBeenCalledTimes(1)
    expect(warn).not.toHaveBeenCalled()
    expect(serializeEditorJson(doc(paragraph(text('Satz [K1].'))))).toBe('Satz [K1].')
    expect(report).toHaveBeenCalledTimes(1)
  })

  it('restores labels in time linear in the text', () => {
    // A label's line start is looked up only before a `:` and never further
    // back than its line. Looking it up for every label (here: a long line
    // of labels) or back to the start of the text (here: many short lines
    // of labels before a colon) made restoring quadratic, several seconds
    // for this text; linear it takes milliseconds.
    const longLine = Array.from(
      { length: 20_000 },
      (_, index) => `Satz ${index} mit Beleg \uFDD0K${index}\uFDD1 und mehr Text.`,
    ).join(' ')
    const shortLines = Array.from(
      { length: 20_000 },
      (_, index) => `\uFDD0K${index}\uFDD1: Eintrag`,
    ).join('\n')
    const started = performance.now()
    const restored = serializeWithRawCitationLabels(doc(), schema, () => `${longLine}\n${shortLines}`)
    const elapsed = performance.now() - started

    expect(restored).toContain('Beleg [K19999] und mehr Text.\n\\[K0\\]: Eintrag')
    expect(elapsed).toBeLessThan(1_000)
  })

  it('serializes unprotected and warns when the document already holds a placeholder', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const content = doc(paragraph(text('Satz [K1] und \uFDD0fremd\uFDD1.')))

    const markdown = serializeEditorJson(content)

    expect(markdown).toBe('Satz \\[K1\\] und \uFDD0fremd\uFDD1.')
    expect(warn).toHaveBeenCalledTimes(1)
    // The prose the helper emits: a reword must be caught here.
    expect(String(warn.mock.calls[0]?.[0])).toContain('Citation labels stay escaped')
  })

  it('relies on the serializer escaping brackets and passing the placeholders through', () => {
    // The protection rests on two details of `@tiptap/markdown` that are not
    // public API: it escapes `[` and `]` in plain text, and its escaping and
    // entity encoding leave U+FDD0 and U+FDD1 alone. A version that changes
    // either must fail here, not silently in stored documents.
    expect(escapingManager.serialize(doc(paragraph(text('Satz [K1] \uFDD0K1\uFDD1 & <x>')))))
      .toBe('Satz \\[K1\\] \uFDD0K1\uFDD1 &amp; &lt;x&gt;')
  })
})

const DIFFERENTIAL_CORPUS: Array<[string, JSONContent]> = [
  ['single label', markdownDoc('Satz [K1].')],
  ['grouped label', markdownDoc('Satz [K2, W7].')],
  ['group with tabs', doc(paragraph(text('Satz [\tK1 ;W7\t] Ende')))],
  ['adjacent labels', markdownDoc('Angrenzend [K2][W7] bleibt.')],
  ['label before a link', doc(paragraph(text('Satz [K1]'), text('W7', 'link')))],
  ['label as link text', markdownDoc('Link [W7](https://example.org) hier.')],
  ['label before a year in parentheses', doc(paragraph(text('Studie [K1](2020) Ende')))],
  ['label before a suggestion-marked parenthesis', doc(paragraph(text('Satz [K1]'), text('(x)', 'insertion')))],
  ['label before an emphasized parenthesis', doc(paragraph(text('Satz [K1]'), text('(x)', 'italic')))],
  ['label at paragraph start before a colon', doc(paragraph(text('[K1]: foo')), paragraph(text('Weiter [K1] hier')))],
  ['label at quote start before a colon', doc({ type: 'blockquote', content: [paragraph(text('[K1]: foo'))] })],
  ['label at list start before a colon', doc(listOf('bulletList', [paragraph(text('[K1]: foo'))]))],
  ['group at ordered list start before a colon', doc(listOf('orderedList', [paragraph(text('[K2, W7]: foo'))]))],
  ['label after a task checkbox before a colon', markdownDoc('- [ ] [K1]: Aufgabe')],
  ['label after a hard break before a colon', doc(paragraph(text('Zeile'), { type: 'hardBreak' }, text('[K1]: foo')))],
  ['label mid-line before a colon', doc(paragraph(text('Quelle [K1]: siehe oben')))],
  ['label in a heading before a colon', markdownDoc('# [K1]: Titel')],
  ['labels in lists, a heading and a quote', markdownDoc('## Titel [K1]\n\n- Punkt [K1]\n\n1. Punkt [W2]\n\n- [ ] Aufgabe [K3]\n\n> Zitat [W7]')],
  ['labels in a table', markdownDoc('| A | B |\n| --- | --- |\n| x [K1] | [W7]: y |')],
  ['label in bold text', markdownDoc('Satz **fett [K1]** Ende.')],
  ['bold label', doc(paragraph(text('Satz '), text('[K1]', 'bold')))],
  ['label split across marks', doc(paragraph(text('['), text('K1', 'bold'), text(']')))],
  ['label in link text', doc(paragraph(text('siehe [K1]', 'link')))],
  ['label in a code mark', doc(paragraph(text('[K1](x)', 'code')))],
  ['labels in a code span and a fence', markdownDoc('Code `[K1](x)` und\n\n```\n[K1](x) \\[K2\\]\n```')],
  ['labels in formulas', markdownDoc('Formel $x_[1]$ und\n\n$$\n[K1] + a_b\n$$')],
  ['labels after backslashes', doc(paragraph(text('literal \\[K1\\] und \\[K1] und [K1\\] und \\\\[K1]')))],
  ['stored escaped label', markdownDoc('Escaped \\[K1\\] literal.')],
  ['image-like label', doc(paragraph(text('Bild ![K1] und ![K2](x) Ende')))],
  ['label before an empty bracket', doc(paragraph(text('Satz [K1][] Ende')))],
  ['non-label brackets', markdownDoc('Ein Platzhalter [Marke] im Satz, x [k1], [K3–K5], [A1] und [unbelegt: K2].')],
  ['a document without brackets', markdownDoc('# Titel\n\nText mit *Betonung*, `code` und einer [Verknuepfung](https://example.org).\n\n---\n\n1. eins\n2. zwei')],
]

// Two paragraphs of one table cell: written, they are joined into one line
// with a `<br>` between them, which the reader takes as a hard break, so
// they read back as one paragraph with a line break.
const CELL_PARAGRAPH_CORPUS: Array<[string, JSONContent]> = [
  ['label before a parenthesis in the next paragraph', tableWithCellParagraphs(['a [K1]', '(2020)'])],
  ['label before a colon in the next paragraph', tableWithCellParagraphs(['a [K1]', ': x'])],
  ['label before a bracket in the next paragraph', tableWithCellParagraphs(['a [K1]', '[W2]'])],
  ['label before a parenthesis in the next paragraph of a header cell', tableWithCellParagraphs(['[W2]', '(c)'], true)],
  ['image-like label before a parenthesis in the next paragraph', tableWithCellParagraphs(['a ![K1]', '(x)'])],
]

describe('the label-preserving serializer against the escaping one', () => {
  it.each(DIFFERENTIAL_CORPUS)('%s reads back as the same document and is a fixpoint', (_name, content) => {
    const written = serializeEditorJson(content)
    const escaped = serializeEscaping(content)

    const readBack = schema.nodeFromJSON(parseEditorMarkdown(written))
    const readBackEscaped = schema.nodeFromJSON(parseEditorMarkdown(escaped))
    expect(readBack.eq(readBackEscaped)).toBe(true)
    expect(serializeEditorJson(parseEditorMarkdown(written))).toBe(written)
  })

  it.each(CELL_PARAGRAPH_CORPUS)('%s of a table cell reads back as the same document', (_name, content) => {
    const written = serializeEditorJson(content)
    const escaped = serializeEscaping(content)

    const readBack = schema.nodeFromJSON(parseEditorMarkdown(written))
    const readBackEscaped = schema.nodeFromJSON(parseEditorMarkdown(escaped))
    expect(readBack.eq(readBackEscaped)).toBe(true)
    // The document read back holds one paragraph and writes the same
    // Markdown again.
    expect(serializeEditorJson(readBack.toJSON())).toBe(written)
  })

  it('changes the output only where the text holds a citation label bracket', () => {
    const withoutLabels = DIFFERENTIAL_CORPUS.filter(([name]) => [
      'label in link text',
      'label in a code mark',
      'labels in a code span and a fence',
      'labels in formulas',
      'label split across marks',
      'labels after backslashes',
      'a document without brackets',
    ].includes(name))
    expect(withoutLabels).toHaveLength(7)
    for (const [name, content] of withoutLabels) {
      expect(serializeEditorJson(content), name).toBe(serializeEscaping(content))
    }
  })
})
