import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { renderInlineMarkdown, stripHeadingMarkers } from './inlineMarkdown'

const html = (text: string) => renderToStaticMarkup(<>{renderInlineMarkdown(text)}</>)

describe('renderInlineMarkdown', () => {
  it('renders the emphasis a narration line carries instead of showing its markers', () => {
    // Otherwise "Am **05. September 2026** gilt ..." shows its stars.
    const out = html('Am **05. September 2026** gilt ein Satz von **2,25 %** p. a.')
    expect(out).toContain('<strong class="font-semibold text-foreground">05. September 2026</strong>')
    expect(out).not.toContain('**')
    expect(html('Siehe `read_canvas` und *vorläufig*')).toContain('<code')
    expect(html('Siehe `read_canvas` und *vorläufig*')).toContain('<em>vorläufig</em>')
    expect(html('Quelle [EZB](https://www.ecb.europa.eu/x)')).toContain('href="https://www.ecb.europa.eu/x"')
  })

  it('drops heading markers and keeps unbalanced markers literal while a line types in', () => {
    expect(stripHeadingMarkers('# EZB-Einlagensatz\n\n## Kurzfazit')).toBe('EZB-Einlagensatz\n\nKurzfazit')
    expect(html('Am **05. Sep')).toBe('Am **05. Sep')
    expect(html('3*4 und snake_case bleiben')).toBe('3*4 und snake_case bleiben')
  })
})
