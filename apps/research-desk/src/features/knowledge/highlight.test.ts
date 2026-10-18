import { describe, expect, it } from 'vitest'
import {
  findFirstMatchingTarget,
  findNormalizedMatches,
  findTermMatches,
  searchTermsFromQuery,
  splitByRanges,
} from './highlight'

describe('findNormalizedMatches', () => {
  it('matches across collapsed whitespace and line breaks', () => {
    const text = 'Ein KI-System gilt als\n  Hochrisiko-KI-System, wenn es eingesetzt wird.'
    const target = 'gilt als Hochrisiko-KI-System,   wenn'

    const ranges = findNormalizedMatches(text, target)

    expect(ranges).toHaveLength(1)
    expect(text.slice(ranges[0].start, ranges[0].end)).toBe(
      'gilt als\n  Hochrisiko-KI-System, wenn',
    )
  })

  it('matches case-insensitively and finds every occurrence', () => {
    const ranges = findNormalizedMatches('Anhang III und anhang iii.', 'Anhang III')
    expect(ranges).toHaveLength(2)
  })

  it('escapes regex metacharacters in the target', () => {
    const ranges = findNormalizedMatches('Artikel 6 (Absatz 3) gilt.', '(Absatz 3)')
    expect(ranges).toHaveLength(1)
  })

  it('returns no ranges for empty or whitespace-only targets', () => {
    expect(findNormalizedMatches('text', '')).toEqual([])
    expect(findNormalizedMatches('text', '   \n ')).toEqual([])
  })
})

describe('findFirstMatchingTarget', () => {
  it('falls back to the next target when the first one does not occur', () => {
    const text = 'Der Katalog beschreibt Pruefkriterien fuer Robustheit.'
    const ranges = findFirstMatchingTarget(text, ['nicht vorhanden', 'Pruefkriterien'])

    expect(ranges).toHaveLength(1)
    expect(text.slice(ranges[0].start, ranges[0].end)).toBe('Pruefkriterien')
  })

  it('returns empty when no target matches', () => {
    expect(findFirstMatchingTarget('abc', ['x', 'y'])).toEqual([])
  })
})

describe('findTermMatches', () => {
  it('merges overlapping term matches into disjoint ranges', () => {
    const ranges = findTermMatches('Hochrisiko-KI-System', ['Hochrisiko-KI', 'KI-System'])
    expect(ranges).toEqual([{ end: 20, start: 0 }])
  })
})

describe('splitByRanges', () => {
  it('produces alternating plain and highlighted segments', () => {
    const text = 'foo bar baz'
    const segments = splitByRanges(text, [{ end: 7, start: 4 }])

    expect(segments).toEqual([
      { rangeIndex: null, text: 'foo ' },
      { rangeIndex: 0, text: 'bar' },
      { rangeIndex: null, text: ' baz' },
    ])
  })

  it('returns the whole text as one segment without ranges', () => {
    expect(splitByRanges('foo', [])).toEqual([{ rangeIndex: null, text: 'foo' }])
  })
})

describe('searchTermsFromQuery', () => {
  it('drops one-character noise terms', () => {
    expect(searchTermsFromQuery('a KI Systeme  ')).toEqual(['KI', 'Systeme'])
  })
})
