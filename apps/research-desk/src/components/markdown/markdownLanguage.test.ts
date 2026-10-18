import { describe, expect, it } from 'vitest'

import { plainCodeLanguageFromClassName } from './markdownLanguage'

describe('markdown language helpers', () => {
  it('reads language-* classes from markdown fallback code blocks', () => {
    expect(plainCodeLanguageFromClassName('language-python')).toBe('python')
    expect(plainCodeLanguageFromClassName(['rounded', 'language-py'])).toBe('python')
    expect(plainCodeLanguageFromClassName('language-unknown')).toBeNull()
  })

})
