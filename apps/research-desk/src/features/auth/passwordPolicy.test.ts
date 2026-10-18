import { describe, expect, it } from 'vitest'

import {
  MIN_PASSWORD_LENGTH,
  isPasswordAcceptable,
  passwordChecks,
  passwordStrength,
  passwordsMatch,
} from './passwordPolicy'

describe('isPasswordAcceptable', () => {
  it('gates ONLY on the minimum length (server-aligned)', () => {
    expect(isPasswordAcceptable('a'.repeat(MIN_PASSWORD_LENGTH - 1))).toBe(false)
    expect(isPasswordAcceptable('a'.repeat(MIN_PASSWORD_LENGTH))).toBe(true)
    // A long all-lowercase password is accepted: classes are advisory only.
    expect(isPasswordAcceptable('abcdefghijklmnop')).toBe(true)
  })
})

describe('passwordChecks', () => {
  it('marks length required and the character classes advisory', () => {
    const checks = passwordChecks('Abcdefghijkl1')
    expect(checks.find((c) => c.id === 'length')).toMatchObject({
      required: true,
      met: true,
    })
    expect(checks.filter((c) => !c.required).map((c) => c.id)).toEqual([
      'lower',
      'upper',
      'digit',
    ])
    expect(checks.find((c) => c.id === 'digit')?.met).toBe(true)
    expect(checks.find((c) => c.id === 'upper')?.met).toBe(true)
  })
})

describe('passwordStrength', () => {
  it('reports empty / weak / fair / strong by length gate + advisory classes', () => {
    expect(passwordStrength('')).toBe('empty')
    expect(passwordStrength('short')).toBe('weak') // below the length gate
    expect(passwordStrength('abcdefghijklmnop')).toBe('fair') // long, 1 class
    expect(passwordStrength('Abcdefghijkl1')).toBe('strong') // long, 3 classes
  })
})

describe('passwordsMatch', () => {
  it('requires a non-empty exact match', () => {
    expect(passwordsMatch('', '')).toBe(false)
    expect(passwordsMatch('correct-horse', 'correct-horse')).toBe(true)
    expect(passwordsMatch('correct-horse', 'battery')).toBe(false)
  })
})
