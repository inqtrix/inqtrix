import { describe, expect, it } from 'vitest'

import {
  buildContextTokenModel,
  estimateTokensFromText,
  type ContextCategoryInput,
} from './contextTokens'

const cats: ContextCategoryInput[] = [
  { key: 'documents', tone: 'file', tokens: 100 },
  { key: 'composer', tone: 'brand', tokens: 0 },
  { key: 'conversation', tone: 'warning', tokens: 50 },
]

describe('estimateTokensFromText', () => {
  it('returns 0 for empty/blank input', () => {
    expect(estimateTokensFromText('')).toBe(0)
    expect(estimateTokensFromText('   ')).toBe(0)
  })

  it('returns a positive estimate for real text', () => {
    expect(estimateTokensFromText('hello world '.repeat(10))).toBeGreaterThan(0)
  })
})

describe('buildContextTokenModel', () => {
  it('sums tokens and drops empty categories', () => {
    const model = buildContextTokenModel(cats, {
      contextWindowTokens: 1000,
      reservedOutputTokens: 0,
      safetyTokens: 0,
    })
    expect(model.totalTokens).toBe(150)
    expect(model.categories.map((c) => c.key)).toEqual(['documents', 'conversation'])
  })

  it('reserves output budget + safety from the window', () => {
    const model = buildContextTokenModel([{ key: 'documents', tone: 'file', tokens: 100 }], {
      contextWindowTokens: 1000,
      reservedOutputTokens: 200,
      safetyTokens: 50,
    })
    expect(model.capacityTokens).toBe(750)
    expect(model.usedFraction).toBeCloseTo(100 / 750)
    expect(model.threshold).toBe('ok')
  })

  it('flags warning >=75% and critical >=90%', () => {
    const opts = { contextWindowTokens: 100, reservedOutputTokens: 0, safetyTokens: 0 }
    expect(buildContextTokenModel([{ key: 'documents', tone: 'file', tokens: 80 }], opts).threshold).toBe('warning')
    expect(buildContextTokenModel([{ key: 'documents', tone: 'file', tokens: 95 }], opts).threshold).toBe('critical')
    expect(buildContextTokenModel([{ key: 'documents', tone: 'file', tokens: 10 }], opts).threshold).toBe('ok')
  })

  it('reports unknown capacity when the context window is null', () => {
    const model = buildContextTokenModel(cats, {
      contextWindowTokens: null,
      reservedOutputTokens: 128_000,
    })
    expect(model.capacityTokens).toBeNull()
    expect(model.usedFraction).toBeNull()
    expect(model.threshold).toBe('unknown')
  })
})
