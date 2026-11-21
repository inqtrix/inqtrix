import { describe, expect, it } from 'vitest'

import {
  reportComposedCharsMax,
  reportRuleIdsMax,
} from './reportRequirement'

describe('report requirement limits', () => {
  it('renders the server’s own numbers', () => {
    // Published == enforced: a surface that accepts what the server
    // refuses teaches the user a limit that is not real.
    expect(reportRuleIdsMax({ max_rules: 1 })).toBe(1)
  })

  it('ignores a nonsensical published limit', () => {
    expect(reportRuleIdsMax({ max_rules: -3 })).toBeNull()
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
    const caps = { max_composed_chars: 12_345, max_rules: 3 }
    expect(reportComposedCharsMax(caps)).toBe(12_345)
  })

  it('has no fallback number: a server that publishes none is not pre-checked', () => {
    // Inventing a ceiling here would be a second copy of a server
    // constant that drifts the day the server changes it.
    expect(reportComposedCharsMax({ max_rules: 3 })).toBeNull()
    expect(reportComposedCharsMax(null)).toBeNull()
    expect(reportComposedCharsMax({ max_composed_chars: 0, max_rules: 3 }))
      .toBeNull()
  })
})
