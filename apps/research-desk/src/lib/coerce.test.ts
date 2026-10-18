import { describe, expect, it } from 'vitest'

import {
  asFiniteNumber,
  asNonEmptyString,
  asString,
  asStringArray,
} from './coerce'

describe('asString', () => {
  it('returns strings verbatim, including empty and whitespace', () => {
    expect(asString('x')).toBe('x')
    expect(asString('')).toBe('')
    expect(asString('  ')).toBe('  ')
  })

  it('returns undefined for non-strings', () => {
    expect(asString(1)).toBeUndefined()
    expect(asString(null)).toBeUndefined()
    expect(asString(undefined)).toBeUndefined()
    expect(asString({})).toBeUndefined()
  })
})

describe('asNonEmptyString', () => {
  it('returns the original (untrimmed) value when non-blank', () => {
    expect(asNonEmptyString('x')).toBe('x')
    expect(asNonEmptyString('  x  ')).toBe('  x  ')
  })

  it('treats blank strings as absent', () => {
    expect(asNonEmptyString('')).toBeUndefined()
    expect(asNonEmptyString('   ')).toBeUndefined()
  })

  it('returns undefined for non-strings', () => {
    expect(asNonEmptyString(0)).toBeUndefined()
    expect(asNonEmptyString(null)).toBeUndefined()
  })
})

describe('asFiniteNumber', () => {
  it('returns finite numbers, including zero and negatives', () => {
    expect(asFiniteNumber(0)).toBe(0)
    expect(asFiniteNumber(-3.5)).toBe(-3.5)
  })

  it('rejects NaN and Infinity', () => {
    expect(asFiniteNumber(NaN)).toBeUndefined()
    expect(asFiniteNumber(Infinity)).toBeUndefined()
    expect(asFiniteNumber(-Infinity)).toBeUndefined()
  })

  it('rejects non-numbers (no coercion of numeric strings)', () => {
    expect(asFiniteNumber('5')).toBeUndefined()
    expect(asFiniteNumber(null)).toBeUndefined()
    expect(asFiniteNumber(undefined)).toBeUndefined()
  })
})

describe('asStringArray', () => {
  it('keeps only string members, dropping the rest', () => {
    expect(asStringArray(['a', 1, 'b', null, {}])).toEqual(['a', 'b'])
  })

  it('returns an empty array for non-arrays or all-non-string', () => {
    expect(asStringArray('a')).toEqual([])
    expect(asStringArray(null)).toEqual([])
    expect(asStringArray([1, 2, 3])).toEqual([])
  })
})
