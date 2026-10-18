import { describe, expect, it } from 'vitest'
import { excerptHighlightRanges, previewWindow } from './CitationExcerpt'

describe('excerptHighlightRanges', () => {
  it('locates the cited span within the excerpt (the two-level highlight target)', () => {
    const excerpt = 'Vortext. Die Haftung ist begrenzt. Nachtext.'
    const ranges = excerptHighlightRanges(excerpt, ['Die Haftung ist begrenzt.'])
    expect(ranges).toHaveLength(1)
    expect(excerpt.slice(ranges[0].start, ranges[0].end)).toBe('Die Haftung ist begrenzt.')
  })

  it('returns no ranges when the span is not present verbatim (honest "no span")', () => {
    expect(excerptHighlightRanges('irgendein Text', ['fehlt komplett'])).toEqual([])
  })
})

describe('previewWindow', () => {
  it('centers on the first match and re-bases the ranges onto the trimmed text', () => {
    const excerpt = `${'a'.repeat(400)} ZIEL ${'b'.repeat(400)}`
    const ranges = excerptHighlightRanges(excerpt, ['ZIEL'])
    const windowed = previewWindow(excerpt, ranges, 20)

    expect(windowed.text.startsWith('…')).toBe(true)
    expect(windowed.text.endsWith('…')).toBe(true)
    expect(windowed.text.length).toBeLessThan(excerpt.length)
    expect(windowed.ranges).toHaveLength(1)
    expect(windowed.text.slice(windowed.ranges[0].start, windowed.ranges[0].end)).toBe('ZIEL')
  })

  it('returns the leading window without ranges when there is no match', () => {
    const windowed = previewWindow('x'.repeat(100), [], 10)
    expect(windowed.ranges).toEqual([])
    expect(windowed.text.endsWith('…')).toBe(true)
  })
})
