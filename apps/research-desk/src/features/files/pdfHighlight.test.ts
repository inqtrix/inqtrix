import { describe, expect, it } from 'vitest'
import { mapTargetsToItemRanges, renderItemHtml } from './pdfHighlight'

describe('mapTargetsToItemRanges', () => {
  it('maps a quote contained in a single text item', () => {
    const byItem = mapTargetsToItemRanges([{ str: 'a high risk AI system' }], ['high risk'])
    expect(byItem.get(0)).toEqual([{ start: 2, end: 11 }])
  })

  it('maps a quote that spans several text items (pdf.js line splitting)', () => {
    // pdf.js emits "high", "risk", "system" as separate items; the joined text
    // is "high risk system" so "high risk" must light up items 0 and 1 only.
    const byItem = mapTargetsToItemRanges(
      [{ str: 'high' }, { str: 'risk' }, { str: 'system' }],
      ['high risk'],
    )
    expect(byItem.get(0)).toEqual([{ start: 0, end: 4 }])
    expect(byItem.get(1)).toEqual([{ start: 0, end: 4 }])
    expect(byItem.has(2)).toBe(false)
  })

  it('falls back to the next target when the quote is not found', () => {
    const byItem = mapTargetsToItemRanges([{ str: 'the system is fine' }], ['nuclear', 'system'])
    expect(byItem.get(0)).toEqual([{ start: 4, end: 10 }])
  })

  it('returns an empty map when nothing matches (page-level cue stays the fallback)', () => {
    expect(mapTargetsToItemRanges([{ str: 'unrelated text' }], ['absent']).size).toBe(0)
  })

  it('degrades to sentence phrases when the full multi-sentence quote does not match', () => {
    // The page has the two sentences; the quote strings them in the opposite
    // order (MarkItDown vs pdf.js extraction divergence), so the full quote fails
    // contiguously — but each cited sentence still appears verbatim on the page.
    const items = [{ str: 'Biometric systems are prohibited' }, { str: 'They pose high risk' }]
    const byItem = mapTargetsToItemRanges(items, [
      'They pose high risk. Biometric systems are prohibited.',
    ])
    expect(byItem.get(0)).toEqual([{ start: 0, end: 32 }])
    expect(byItem.get(1)).toEqual([{ start: 0, end: 19 }])
  })

  it('keeps item indices aligned across marked-content markers without a str', () => {
    // The marker occupies index 0; the real text item is index 1, which must be
    // the key (customTextRenderer indexes the same array).
    const byItem = mapTargetsToItemRanges(
      [{ type: 'beginMarkedContent' }, { str: 'a dangerous clause' }],
      ['dangerous'],
    )
    expect(byItem.has(0)).toBe(false)
    expect(byItem.get(1)).toEqual([{ start: 2, end: 11 }])
  })
})

describe('renderItemHtml', () => {
  it('wraps matched slices in <mark> and escapes the rest', () => {
    const html = renderItemHtml('a high risk system', [{ start: 2, end: 11 }], false)
    expect(html).toBe('a <mark class="rounded-sm bg-brand/30 text-transparent">high risk</mark> system')
  })

  it('escapes HTML so page text or a quote cannot inject markup (XSS guard)', () => {
    const html = renderItemHtml('<script>alert(1)</script> dangerous', [{ start: 26, end: 35 }], false)
    expect(html).not.toContain('<script>')
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;')
    expect(html).toContain('>dangerous</mark>')
  })

  it('marks the first hit with the scroll anchor only when asked', () => {
    expect(renderItemHtml('hit', [{ start: 0, end: 3 }], true)).toContain('data-inqtrix-hit="1"')
    expect(renderItemHtml('hit', [{ start: 0, end: 3 }], false)).not.toContain('data-inqtrix-hit')
  })

  it('escapes an item with no ranges and adds no markup', () => {
    expect(renderItemHtml('plain & <b>text</b>', undefined, false)).toBe('plain &amp; &lt;b&gt;text&lt;/b&gt;')
  })
})
