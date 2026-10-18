import {
  createEditorSchemaExtensions,
  parseEditorMarkdown,
} from '@inqtrix/editor-schema'
import { Editor as HeadlessEditor } from '@tiptap/core'
import { describe, expect, it } from 'vitest'

import type { MarkdownHighlightedLine } from '@/components/markdown/codeHighlight'
import {
  buildCodeBlockHighlightDecorations,
  codeBlockHighlightLanguage,
  type CodeBlockHighlightJob,
} from './codeBlockHighlight'
import { serializeEditorMarkdown } from './tiptap'

const CODE = 'x = 1\nprint(x)'

function docWith(markdown: string) {
  const editor = new HeadlessEditor({
    content: parseEditorMarkdown(markdown),
    element: null,
    extensions: createEditorSchemaExtensions({ enableUndoRedo: false }),
    injectCSS: false,
  })
  return editor
}

describe('codeBlockHighlightLanguage', () => {
  it('folds aliases and falls back to plaintext for foreign tags', () => {
    expect(codeBlockHighlightLanguage('python')).toBe('python')
    expect(codeBlockHighlightLanguage('js')).toBe('javascript')
    // The DISPLAY falls back; the block's own attribute is untouched —
    // foreign languages must never be silently overwritten.
    expect(codeBlockHighlightLanguage('rust')).toBe('plaintext')
    expect(codeBlockHighlightLanguage(null)).toBe('plaintext')
  })
})

describe('buildCodeBlockHighlightDecorations', () => {
  it('maps token lines to inline decorations with the newline offset', () => {
    const editor = docWith(`\`\`\`python\n${CODE}\n\`\`\``)
    const lines: MarkdownHighlightedLine[] = [
      [{ color: '#111111', content: 'x = 1' }],
      [{ color: '#222222', content: 'print' }, { content: '(x)' }],
    ]
    const set = buildCodeBlockHighlightDecorations(
      editor.state.doc,
      'github-light',
      () => lines,
      () => {
        throw new Error('cache hit must not schedule')
      },
    )
    const found = set.find()
    // '(x)' carries no style -> no decoration for it.
    expect(found).toHaveLength(2)
    // Content starts at pos+1; the second line begins one position after
    // the first line's text (the newline occupies exactly one position).
    expect(found[0].from).toBe(1)
    expect(found[0].to).toBe(6)
    expect(found[1].from).toBe(7)
    expect(found[1].to).toBe(12)
  })

  it('schedules a miss once and decorates nothing yet', () => {
    const editor = docWith(`\`\`\`python\n${CODE}\n\`\`\``)
    const jobs: Array<[string, CodeBlockHighlightJob]> = []
    const set = buildCodeBlockHighlightDecorations(
      editor.state.doc,
      'github-dark',
      () => undefined,
      (cacheKey, job) => jobs.push([cacheKey, job]),
    )
    expect(set.find()).toHaveLength(0)
    expect(jobs).toHaveLength(1)
    expect(jobs[0][1]).toEqual({
      code: CODE,
      language: 'python',
      theme: 'github-dark',
    })
  })

  it('highlights a foreign language as plaintext without touching it', () => {
    const editor = docWith(`\`\`\`rust\nfn main() {}\n\`\`\``)
    let scheduled: CodeBlockHighlightJob | undefined
    buildCodeBlockHighlightDecorations(
      editor.state.doc,
      'github-light',
      () => undefined,
      (_key, job) => {
        scheduled = job
      },
    )
    expect(scheduled?.language).toBe('plaintext')
    let language: unknown
    editor.state.doc.descendants((node) => {
      if (node.type.name === 'codeBlock') language = node.attrs.language
      return language === undefined
    })
    expect(language).toBe('rust')
  })
})

describe('fence language round-trip invariant', () => {
  it('keeps known AND foreign fence languages through parse/serialize', () => {
    for (const fence of ['python', 'rust']) {
      const editor = docWith(`\`\`\`${fence}\nprint(1)\n\`\`\``)
      const markdown = serializeEditorMarkdown(editor as never)
      expect(markdown).toContain(`\`\`\`${fence}`)
    }
  })
})
