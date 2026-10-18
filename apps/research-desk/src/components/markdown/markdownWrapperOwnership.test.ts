import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { MARKDOWN_VARIANT_CLASS } from './MarkdownRenderer'

/**
 * The reading-scale classes `chat-markdown` / `report-markdown` are set
 * by the renderer itself. A consumer that adds them again is harmless
 * today and a fork tomorrow — the evidence panel and the diff view once
 * rendered without any class and looked like a different product. Only
 * the Tiptap editor surface, which renders its own document, may carry
 * the class.
 */
const SOURCE_ROOT = path.resolve(__dirname, '..', '..')
const OWNERS = new Set([
  'components/markdown/MarkdownRenderer.tsx',
  'features/editor/core/MarkdownEditorSurface.tsx',
])
const CLASS_LITERAL = /\b(chat-markdown|report-markdown)\b/

function sourceFiles(directory: string): string[] {
  return readdirSync(directory, { recursive: true, withFileTypes: true })
    .filter((entry) => entry.isFile() && /\.(ts|tsx)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name))
    .map((entry) => path.join(entry.parentPath, entry.name))
}

describe('markdown wrapper ownership', () => {
  it('names both classes once, in the renderer', () => {
    expect(Object.values(MARKDOWN_VARIANT_CLASS).sort()).toEqual(['chat-markdown', 'report-markdown'])
  })

  it('lets no consumer set the reading-scale class itself', () => {
    const offenders = sourceFiles(SOURCE_ROOT)
      .filter((file) => CLASS_LITERAL.test(readFileSync(file, 'utf8')))
      .map((file) => path.relative(SOURCE_ROOT, file))
      .filter((file) => !OWNERS.has(file))
    expect(offenders).toEqual([])
  })
})
