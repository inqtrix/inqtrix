import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { translations } from '@/i18n/translations'
import { MarkdownRenderer } from './MarkdownRenderer'

vi.mock('@/i18n/LocaleProvider', () => ({
  useLocale: () => ({ locale: 'de', t: translations.de }),
}))
vi.mock('@/theme/ThemeProvider', () => ({
  useTheme: () => ({ contrastMode: 'standard', preset: 'standard', resolvedTheme: 'light' }),
}))

function render(markdown: string, changedLines?: ReadonlySet<number>): string {
  return renderToStaticMarkup(
    <TooltipProvider>
      <MarkdownRenderer changedLines={changedLines} markdown={markdown} variant="chat" />
    </TooltipProvider>,
  )
}

const MARK = 'data-review-changed'

function markedTags(html: string): string[] {
  return [...html.matchAll(/<(\w+)\b[^>]*data-review-changed/g)].map((match) => match[1])
}

function count(html: string, token: string): number {
  return html.split(token).length - 1
}

describe('the review marks in a rendered answer', () => {
  it('marks the changed item and keeps the list whole', () => {
    const html = render('1. Eins\n2. Zwei, belegt\n3. Drei', new Set([2]))
    expect([count(html, '<ol'), markedTags(html)]).toEqual([1, ['li']])
  })

  it('marks the changed row and keeps the table whole', () => {
    const html = render('| A | B |\n| --- | --- |\n| 1 | alt |\n| 2 | neu |', new Set([4]))
    expect([count(html, '<table'), markedTags(html)]).toEqual([1, ['tr']])
  })

  it('marks the innermost block: the paragraph of a loose item, not the item', () => {
    const html = render('- Eins\n\n- Zwei, belegt\n\n- Drei', new Set([3]))
    expect(markedTags(html)).toEqual(['p'])
  })

  it('marks a changed heading and leaves the paragraph after it', () => {
    expect(markedTags(render('## Lage\n\nErste Antwort.', new Set([1, 2])))).toEqual(['h2'])
  })

  it('marks a changed formula on a block around it, which the formula renderer keeps', () => {
    const html = render('Text.\n\n$$\nx^2\n$$\n\nEnde.', new Set([3, 4, 5]))
    expect([markedTags(html), html.includes('katex')]).toEqual([['div'], true])
  })

  it('marks a changed diagram on a block around it, which the diagram renderer keeps', () => {
    const html = render('Text.\n\n```mermaid\nflowchart LR\n  A --> B\n```\n\nEnde.', new Set([3, 4, 5, 6]))
    expect(markedTags(html)).toEqual(['div'])
  })

  it('marks a changed code block on the block itself', () => {
    expect(markedTags(render('Text.\n\n```js\nconst x = 1\n```', new Set([3, 4, 5])))).toEqual(['pre'])
  })

  it('marks nothing without changed lines', () => {
    expect(count(render('## Lage\n\nErste Antwort.'), MARK)).toBe(0)
  })
})
