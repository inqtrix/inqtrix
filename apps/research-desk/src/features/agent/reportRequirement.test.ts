import { describe, expect, it } from 'vitest'

import {
  REPORT_GUIDANCE_MAX_CHARS_FALLBACK,
  reportComposedCharsMax,
  reportGuidanceMaxChars,
  reportRuleIdsMax,
  toggleReportRule,
} from './reportRequirement'

describe('report requirement limits', () => {
  it('renders the server’s own numbers', () => {
    // Published == enforced: a surface that accepts what the server
    // refuses teaches the user a limit that is not real.
    const caps = { max_chars: 500, max_rules: 1 }
    expect(reportGuidanceMaxChars(caps)).toBe(500)
    expect(reportRuleIdsMax(caps)).toBe(1)
  })

  it('falls back only when the server publishes nothing', () => {
    expect(reportGuidanceMaxChars(undefined)).toBe(
      REPORT_GUIDANCE_MAX_CHARS_FALLBACK,
    )
  })

  it('ignores a nonsensical published limit', () => {
    const caps = { max_chars: 0, max_rules: -3 }
    expect(reportGuidanceMaxChars(caps)).toBe(
      REPORT_GUIDANCE_MAX_CHARS_FALLBACK,
    )
    expect(reportRuleIdsMax(caps)).toBeNull()
  })

  it('has no fallback rule cap: a server that publishes none is not pre-checked', () => {
    // A client-side 3 was a second copy of the server's cap; without a
    // published one the surfaces let the server decide and say so.
    expect(reportRuleIdsMax(null)).toBeNull()
    expect(reportRuleIdsMax(undefined)).toBeNull()
  })
})

describe('the composed ceiling', () => {
  it('renders the server’s own ceiling', () => {
    const caps = { max_chars: 2000, max_composed_chars: 12_345, max_rules: 3 }
    expect(reportComposedCharsMax(caps)).toBe(12_345)
  })

  it('has no fallback number: a server that publishes none is not pre-checked', () => {
    // Inventing a ceiling here would be a second copy of a server
    // constant that drifts the day the server changes it.
    expect(reportComposedCharsMax({ max_chars: 2000, max_rules: 3 })).toBeNull()
    expect(reportComposedCharsMax(null)).toBeNull()
    expect(reportComposedCharsMax({ max_chars: 2000, max_composed_chars: 0, max_rules: 3 }))
      .toBeNull()
  })
})

describe('attaching rules', () => {
  it('adds and removes in attachment order', () => {
    expect(toggleReportRule([], 'a', 3)).toEqual(['a'])
    expect(toggleReportRule(['a'], 'b', 3)).toEqual(['a', 'b'])
    expect(toggleReportRule(['a', 'b'], 'a', 3)).toEqual(['b'])
  })

  it('never evicts an attached rule to make room', () => {
    // Silently dropping the first rule when the third is picked would
    // run under requirements the user believes are in force.
    expect(toggleReportRule(['a', 'b', 'c'], 'd', 3)).toEqual(['a', 'b', 'c'])
  })

  it('still lets the user detach at the cap', () => {
    expect(toggleReportRule(['a', 'b', 'c'], 'b', 3)).toEqual(['a', 'c'])
  })

  it('caps nothing the server did not publish', () => {
    expect(toggleReportRule(['a', 'b', 'c'], 'd', null)).toEqual(['a', 'b', 'c', 'd'])
  })
})
