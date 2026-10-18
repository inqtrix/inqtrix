import { describe, expect, it } from 'vitest'

import { shouldReloadPatTokensOnSectionChange } from './SettingsWorkspace'

describe('settings access-token refresh', () => {
  it('reloads exactly once for each entry into the access-token section', () => {
    const sections = [
      'preferences',
      'access-tokens',
      'access-tokens',
      'security',
      'access-tokens',
    ] as const

    const reloads = sections.slice(1).filter((section, index) =>
      shouldReloadPatTokensOnSectionChange(sections[index], section),
    )

    expect(reloads).toEqual(['access-tokens', 'access-tokens'])
  })

  it('does not reload when the reload callback changes without a section entry', () => {
    expect(
      shouldReloadPatTokensOnSectionChange('access-tokens', 'access-tokens'),
    ).toBe(false)
  })
})
