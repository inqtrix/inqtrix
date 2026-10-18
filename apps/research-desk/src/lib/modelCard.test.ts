import { describe, expect, it } from 'vitest'

import { costTier, effortLevelLabel, formatTokens, reasoningLevelOptions, speedLabel } from './modelCard'

describe('reasoningLevelOptions', () => {
  it('hides the selector when a model has no effort control (Haiku)', () => {
    expect(reasoningLevelOptions([])).toEqual([])
  })

  it('keeps every supported level selectable in the model order', () => {
    const options = reasoningLevelOptions(['none', 'low', 'medium', 'high', 'xhigh', 'max'])
    expect(options.map((o) => o.token)).toEqual(['none', 'low', 'medium', 'high', 'xhigh', 'max'])
    expect(options.map((o) => o.label)).toEqual(['Off', 'Low', 'Med', 'High', 'XHigh', 'Max'])
  })

  it('preserves a Pro model order without inventing an off level', () => {
    const options = reasoningLevelOptions(['medium', 'high', 'xhigh'])
    expect(options.map((o) => o.token)).toEqual(['medium', 'high', 'xhigh'])
    expect(options.some((o) => o.token === 'none')).toBe(false)
  })
})

describe('effortLevelLabel', () => {
  it('maps known tokens to compact labels and falls back capitalised', () => {
    expect(effortLevelLabel('none')).toBe('Off')
    expect(effortLevelLabel('minimal')).toBe('Min')
    expect(effortLevelLabel('medium')).toBe('Med')
    expect(effortLevelLabel('xhigh')).toBe('XHigh')
    expect(effortLevelLabel('ultra')).toBe('Ultra')
  })
})

describe('costTier', () => {
  it('maps output price to coarse $ tiers with the exact value', () => {
    expect(costTier({ input_per_mtok: 1, output_per_mtok: 5 }).signs).toBe('$')
    expect(costTier({ input_per_mtok: 3, output_per_mtok: 15 }).signs).toBe('$$')
    expect(costTier({ input_per_mtok: 5, output_per_mtok: 25 }).signs).toBe('$$$')
    expect(costTier({ input_per_mtok: 30, output_per_mtok: 180 }).signs).toBe('$$$$')
    expect(costTier({ input_per_mtok: 5, output_per_mtok: 25 }).value).toBe('$25/1M')
  })
})

describe('formatTokens', () => {
  it('formats context windows compactly', () => {
    expect(formatTokens(1_000_000)).toBe('1M')
    expect(formatTokens(1_050_000)).toBe('1.05M')
    expect(formatTokens(200_000)).toBe('200k')
  })

  it('shows exact counts below 1k and thousands above', () => {
    expect(formatTokens(0)).toBe('0')
    expect(formatTokens(20)).toBe('20')
    expect(formatTokens(850)).toBe('850')
    expect(formatTokens(1200)).toBe('1.2k')
  })
})

describe('speedLabel', () => {
  it('capitalises the TEMPO label', () => {
    expect(speedLabel('mittel')).toBe('Mittel')
  })
})
