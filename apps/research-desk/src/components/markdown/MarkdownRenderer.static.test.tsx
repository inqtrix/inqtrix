import { readFileSync } from 'node:fs'
import path from 'node:path'

import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { translations } from '@/i18n/translations'
import { parseChatThread } from '@/features/project/markdown'
import { MarkdownRenderer, type MarkdownRendererVariant } from './MarkdownRenderer'

vi.mock('@/i18n/LocaleProvider', () => ({
  useLocale: () => ({ locale: 'de', t: translations.de }),
}))
vi.mock('@/theme/ThemeProvider', () => ({
  useTheme: () => ({ contrastMode: 'standard', preset: 'standard', resolvedTheme: 'light' }),
}))

const SAMPLE = path.resolve(__dirname, '../../features/project/demoContent/chat-history/chat-rendering-standard.md')

function render(markdown: string, variant: MarkdownRendererVariant): string {
  return renderToStaticMarkup(
    <TooltipProvider>
      <MarkdownRenderer markdown={markdown} variant={variant} />
    </TooltipProvider>,
  )
}

/** Block structure of the rendered HTML: the sequence of block-level tags,
 * with class attributes removed so only the scale may differ. */
function blockSkeleton(html: string): string[] {
  return [...html.matchAll(/<(h[1-6]|p|table|pre|figure|blockquote|ul|ol|hr)\b/g)].map((match) => match[1])
}

describe('MarkdownRenderer static contract', () => {
  const sample = parseChatThread(readFileSync(SAMPLE, 'utf8'))
  const body = sample.messages.find((message) => message.role === 'assistant')?.contentMarkdown ?? ''

  it('owns the reading-scale wrapper per variant on a box-less root', () => {
    const chat = render('Hallo **Welt**', 'chat')
    expect(chat).toContain('class="contents chat-markdown" data-markdown-variant="chat"')
    const report = render('Hallo **Welt**', 'report')
    expect(report).toContain('class="contents report-markdown" data-markdown-variant="report"')
  })

  it('renders the sample set with the same block structure in both variants', () => {
    const chat = render(body, 'chat')
    const report = render(body, 'report')
    const skeleton = blockSkeleton(chat)
    expect(skeleton).toEqual(blockSkeleton(report))
    expect(skeleton.filter((tag) => tag === 'table')).toHaveLength(3)
    expect(skeleton.filter((tag) => tag === 'figure')).toHaveLength(10)
    // The two adjacent plots share one row; the lone plot does not.
    expect(chat.match(/data-figure-row/g)).toHaveLength(1)
    expect(chat.match(/data-layout="half"/g)).toHaveLength(2)
    expect(chat.match(/data-layout="single"/g)).toHaveLength(8)
    expect(skeleton.filter((tag) => tag === 'blockquote')).toHaveLength(2)
    // Display and inline math went through KaTeX; nothing stayed raw.
    expect(chat.match(/class="katex-display"/g)?.length).toBeGreaterThanOrEqual(2)
    expect(chat).not.toContain('$$')
    expect(chat).toContain('USD 5')
    // Diagram figures carry their grammar for the browser probe.
    for (const type of ['flowchart', 'sequenceDiagram', 'xychart-beta']) {
      expect(chat).toContain(`data-diagram-type="${type}"`)
    }
  })

  it('typesets bracket delimiters on their own lines and leaves escaped brackets in prose', () => {
    const html = render('Formel:\n\\[\nE = mc^2\n\\]\n\n- [W1] Bericht \\[2024\\] der Agentur\n\nInline \\( x^2 \\) hier.', 'chat')
    expect(html.match(/class="katex-display"/g)).toHaveLength(1)
    expect(html).toContain('Bericht [2024] der Agentur')
    expect(html.match(/class="katex"/g)?.length).toBeGreaterThanOrEqual(2)
  })
})
