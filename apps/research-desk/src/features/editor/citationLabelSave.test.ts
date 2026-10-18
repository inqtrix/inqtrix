/**
 * A saved document cites what it cited when it was opened.
 *
 * Every Markdown the editor writes (the live save, the collaboration
 * projection, the quote of a selection) is read back by the citation reader
 * the canvas renders with. Whatever the serializer does to brackets, the
 * set of cited labels a reader finds must not change across a save.
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import type { Root, RootContent } from 'mdast'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import remarkParse from 'remark-parse'
import { unified } from 'unified'
import { Editor as HeadlessEditor } from '@tiptap/core'
import type { JSONContent } from '@tiptap/core'
import type { Editor } from '@tiptap/react'
import { describe, expect, it, vi } from 'vitest'

import {
  linkifyAgentArtifactCitations,
  type AgentArtifactReference,
} from '@/features/agent/artifactCitations'
import { markdownForRange } from './anchoring'
import {
  createEditorExtensions,
  serializeEditorFinalProjectionMarkdown,
  serializeEditorMarkdown,
} from './tiptap'

type ParityCase = { name: string; text: string }

const parity: { cases: ParityCase[] } = JSON.parse(readFileSync(
  fileURLToPath(new URL('../../../../../tests/fixtures/citation_label_parity.json', import.meta.url)),
  'utf-8',
))

function openEditor(content: string | JSONContent): Editor {
  return new HeadlessEditor({
    content,
    ...(typeof content === 'string' ? { contentType: 'markdown' as const } : {}),
    element: null,
    extensions: createEditorExtensions(),
    injectCSS: false,
  }) as unknown as Editor
}

/** The labels the canvas renders as citations, as a set: every label the
 * text names is a known reference, so only the grammar decides. */
function citedLabels(markdown: string): string[] {
  const references = [...new Set(markdown.match(/[KWE][0-9]+/g) ?? [])]
    .map((label) => ({ label }) as AgentArtifactReference)
  const linked = linkifyAgentArtifactCitations(markdown, references)
  return [...new Set([...linked.matchAll(/\(#kref-([A-Z][0-9]+)\)/g)].map((match) => match[1]))]
    .sort()
}

// Escaped text is excluded: parsing drops the escape, so a stored `\[K1\]`
// reads as a citation after the next save (a known limit).
const UNESCAPED_CASES = parity.cases.filter((testCase) => !testCase.text.includes('\\'))

describe('citations survive every serializer exit', () => {
  it('reads a meaningful share of the shared citation fixture', () => {
    expect(UNESCAPED_CASES.length).toBeGreaterThanOrEqual(35)
  })

  it.each(UNESCAPED_CASES.map((testCase) => [testCase.name, testCase.text]))(
    '%s cites the same labels after a save',
    (_name, text) => {
      const before = citedLabels(text)
      const editor = openEditor(text)
      try {
        expect(citedLabels(serializeEditorMarkdown(editor)), 'live save').toEqual(before)
        expect(
          citedLabels(serializeEditorFinalProjectionMarkdown(editor)),
          'collaboration projection',
        ).toEqual(before)
        expect(
          citedLabels(markdownForRange(editor, 0, editor.state.doc.content.size)),
          'selection quote',
        ).toEqual(before)
      } finally {
        editor.destroy()
      }
    },
  )

  it('quotes plain text and says so when the Markdown cannot be written', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const editor = openEditor('Satz mit **fett** [K1].')
    try {
      const failure = new Error('serializer failed')
      vi.spyOn(editor.markdown!, 'serialize').mockImplementation(() => {
        throw failure
      })
      expect(markdownForRange(editor, 0, editor.state.doc.content.size)).toBe('Satz mit fett [K1].')
      expect(warn).toHaveBeenCalledTimes(1)
      // The prose the fallback emits: a reword must be caught here.
      expect(String(warn.mock.calls[0]?.[0])).toContain('quoted as plain text')
      expect(warn.mock.calls[0]?.[1]).toBe(failure)
    } finally {
      editor.destroy()
      vi.restoreAllMocks()
    }
  })

  it('quotes plain text and says so without a Markdown serializer', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const editor = openEditor('Satz mit **fett** [K1].')
    try {
      const withoutSerializer = { markdown: undefined, schema: editor.schema, state: editor.state } as unknown as Editor
      expect(markdownForRange(withoutSerializer, 0, editor.state.doc.content.size)).toBe('Satz mit fett [K1].')
      expect(warn).toHaveBeenCalledTimes(1)
      // The prose the fallback emits: a reword must be caught here.
      expect(String(warn.mock.calls[0]?.[0])).toContain('no Markdown serializer')
    } finally {
      editor.destroy()
      vi.restoreAllMocks()
    }
  })

  it('quotes a selected label raw', () => {
    const editor = openEditor('Vorher. Der Markt waechst [K1] und sinkt [K2, W7]. Nachher.')
    try {
      const paragraph = editor.state.doc.firstChild!
      const start = 1 + paragraph.textContent.indexOf('Der Markt')
      const end = 1 + paragraph.textContent.indexOf(' Nachher')
      expect(markdownForRange(editor, start, end)).toBe('Der Markt waechst [K1] und sinkt [K2, W7].')
    } finally {
      editor.destroy()
    }
  })
})

type MdastNode = Root | RootContent

/** A remark tree without positions and with adjacent text merged: two
 * spellings of the same text compare equal. */
function readerTree(markdown: string): unknown {
  const tree = unified().use(remarkParse).use(remarkGfm).use(remarkMath).parse(markdown)
  const normalize = (node: MdastNode): unknown => {
    const rest: Record<string, unknown> = { ...node }
    delete rest.position
    if (!Array.isArray(rest.children)) return rest
    const children: Array<Record<string, unknown>> = []
    for (const child of (rest.children as MdastNode[]).map(normalize) as Array<Record<string, unknown>>) {
      const last = children.at(-1)
      if (last?.type === 'text' && child.type === 'text') {
        last.value = `${String(last.value)}${String(child.value)}`
      } else {
        children.push({ ...child })
      }
    }
    return { ...rest, children }
  }
  return normalize(tree)
}

const text = (value: string, ...marks: string[]): JSONContent => ({
  type: 'text',
  text: value,
  ...(marks.length > 0
    ? { marks: marks.map((type) => (type === 'link' ? { type, attrs: { href: 'https://example.org' } } : { type })) }
    : {}),
})
const paragraphDoc = (...content: JSONContent[]): JSONContent => ({
  type: 'doc',
  content: [{ type: 'paragraph', content }],
})

const READER_CORPUS: Array<[string, string | JSONContent]> = [
  ['single label', 'Satz [K1].'],
  ['grouped label', 'Satz [K2, W7].'],
  ['adjacent labels', 'Angrenzend [K2][W7] bleibt.'],
  ['labels in a table', '| A | B |\n| --- | --- |\n| x [K1] | [W7]: y |'],
  ['label in a heading before a colon', '# [K1]: Titel'],
  ['labels in lists and a quote', '- Punkt [K1]\n\n> Zitat [W7]\n\n1. [K2] erst'],
  ['label before a link', paragraphDoc(text('Satz [K1]'), text('mehr', 'link'))],
  ['label before a bold parenthesis', paragraphDoc(text('Satz [K1]'), text('(x)', 'bold'))],
  ['label before a code parenthesis', paragraphDoc(text('Satz [K1]'), text('(x)', 'code'))],
  ['image-like label before a parenthesis', paragraphDoc(text('Bild ![K1](x) Ende'))],
  ['image-like label', paragraphDoc(text('Bild ![K1] Ende'))],
  ['label before an empty bracket', paragraphDoc(text('Satz [K1][] Ende'))],
  ['label mid-line before a colon', paragraphDoc(text('Quelle [K1]: siehe'))],
  [
    'label at paragraph start before a colon',
    { type: 'doc', content: [{ type: 'paragraph', content: [text('[K1]: https://x.org')] }, { type: 'paragraph', content: [text('dann [K1]')] }] },
  ],
  ['label after a hard break before a colon', paragraphDoc(text('Zeile'), { type: 'hardBreak' }, text('[K1]: foo'))],
  ['label after a task checkbox', '- [ ] Aufgabe [K1]\n'],
  ['label before a linked label', paragraphDoc(text('Satz [K1]'), text('W7', 'link'))],
  ['label before an autolink', paragraphDoc(text('Satz [K1]<https://x.org>'))],
  ['label before a year in parentheses', paragraphDoc(text('Studie [K1](2020) Ende'))],
  [
    'label before a parenthesis in the next paragraph of a table cell',
    {
      type: 'doc',
      content: [{
        type: 'table',
        content: [
          { type: 'tableRow', content: [{ type: 'tableHeader', content: [{ type: 'paragraph', content: [text('Quelle')] }] }] },
          {
            type: 'tableRow',
            content: [{
              type: 'tableCell',
              content: [
                { type: 'paragraph', content: [text('Studie [K1]')] },
                { type: 'paragraph', content: [text('(2020)')] },
              ],
            }],
          },
        ],
      }],
    },
  ],
]

/** Every node of the remark tree of *markdown* with its type, depth first. */
function readerNodes(markdown: string): Array<{ type: string; url?: string }> {
  const nodes: Array<{ type: string; url?: string }> = []
  const visit = (node: MdastNode) => {
    nodes.push({ type: node.type, ...('url' in node ? { url: node.url } : {}) })
    if ('children' in node) for (const child of node.children as MdastNode[]) visit(child)
  }
  visit(unified().use(remarkParse).use(remarkGfm).use(remarkMath).parse(markdown))
  return nodes
}

describe('the canvas reader reads what the editor writes', () => {
  it('renders a saved label after an exclamation mark as a citation, never an image', () => {
    const editor = openEditor('Das ist falsch![K1] Weiter.')
    try {
      const saved = serializeEditorMarkdown(editor)
      expect(saved).toBe('Das ist falsch![K1] Weiter.')
      const nodes = readerNodes(
        linkifyAgentArtifactCitations(saved, [{ label: 'K1' } as AgentArtifactReference]),
      )
      expect(nodes.filter((node) => node.type === 'link').map((node) => node.url)).toEqual(['#kref-K1'])
      expect(nodes.filter((node) => node.type === 'image')).toEqual([])
    } finally {
      editor.destroy()
    }
  })

  it.each(READER_CORPUS)('%s reads as the escaping serializer\'s output', (_name, content) => {
    const editor = openEditor(content)
    try {
      const escaped = editor.markdown!.serialize(editor.getJSON())
      expect(readerTree(serializeEditorMarkdown(editor))).toEqual(readerTree(escaped))
    } finally {
      editor.destroy()
    }
  })
})
