import { describe, expect, it } from 'vitest'
import { activeMatchScrollDecision } from './useActiveMatchScroll'

describe('activeMatchScrollDecision', () => {
  it('lands instantly on a surface that was never scrolled', () => {
    expect(activeMatchScrollDecision({
      contentKey: 'doc-1:0',
      landedKey: null,
      reducedMotion: false,
    })).toEqual({ behavior: 'auto', initial: true })
  })

  it('steps smoothly within the same surface', () => {
    expect(activeMatchScrollDecision({
      contentKey: 'doc-1:0',
      landedKey: 'doc-1:0',
      reducedMotion: false,
    })).toEqual({ behavior: 'smooth', initial: false })
  })

  it('keeps stepping instant under reduced motion', () => {
    expect(activeMatchScrollDecision({
      contentKey: 'doc-1:0',
      landedKey: 'doc-1:0',
      reducedMotion: true,
    })).toEqual({ behavior: 'auto', initial: false })
  })

  it('treats a content change as a fresh instant landing', () => {
    expect(activeMatchScrollDecision({
      contentKey: 'doc-2:3',
      landedKey: 'doc-1:0',
      reducedMotion: false,
    })).toEqual({ behavior: 'auto', initial: true })
  })
})
