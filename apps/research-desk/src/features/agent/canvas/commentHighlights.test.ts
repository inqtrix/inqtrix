/**
 * Pure matching core of the pending-comment highlights. The DOM
 * glue (text index, ranges, CSS Highlight painting) is deliberately
 * thin and covered by the visual acceptance in a real browser — this
 * suite stays node-only like the rest of the unit tier.
 */
import { describe, expect, it } from 'vitest'

import {
  applyCanvasCommentHighlights,
  findMatchOffsets,
} from './commentHighlights'

const TEXT = 'Ein fetter Satz. Erster  Teil\n zweiter Teil. Alpha Beta Gamma'

describe('findMatchOffsets (pending-comment highlight matching)', () => {
  it('finds an exact rendered text', () => {
    const [offset] = findMatchOffsets(TEXT, ['Ein fetter Satz.'])
    expect(TEXT.slice(offset.start, offset.end)).toBe('Ein fetter Satz.')
  })

  it('tolerates whitespace differences between draft and document', () => {
    const [offset] = findMatchOffsets(TEXT, ['Erster Teil zweiter Teil.'])
    expect(offset).toBeDefined()
    expect(TEXT.slice(offset.start, offset.end)).toContain('zweiter Teil.')
  })

  it('yields nothing for text the document no longer contains', () => {
    expect(findMatchOffsets(TEXT, ['alter Satz'])).toHaveLength(0)
  })

  it('maps one offset per draft, honestly skipping missing ones', () => {
    const offsets = findMatchOffsets(TEXT, ['Alpha', 'fehlt', 'Gamma'])
    expect(
      offsets.map((offset) => TEXT.slice(offset.start, offset.end)),
    ).toEqual(['Alpha', 'Gamma'])
  })

  it('ignores empty and whitespace-only drafts', () => {
    expect(findMatchOffsets(TEXT, ['', '   '])).toHaveLength(0)
  })

  it('escapes regex metacharacters in the draft text', () => {
    const text = 'Preis (netto) steigt um 3.5 Prozent.'
    const [offset] = findMatchOffsets(text, ['Preis (netto) steigt'])
    expect(text.slice(offset.start, offset.end)).toBe('Preis (netto) steigt')
  })
})

describe('applyCanvasCommentHighlights', () => {
  it('reports the missing Highlight API honestly (node)', () => {
    // The node tier ships no CSS.highlights — the painter must say so
    // instead of throwing; highlights are pure visual enhancement.
    expect(applyCanvasCommentHighlights(null, ['x'])).toBe(false)
  })
})
