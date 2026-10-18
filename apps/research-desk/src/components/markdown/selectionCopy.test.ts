import { describe, expect, it } from 'vitest'
import { markdownForVisibleSelection } from './selectionCopy'

describe('markdownForVisibleSelection', () => {
  it('returns the formatted markdown for a complete bold selection', () => {
    expect(markdownForVisibleSelection('Alpha **Beta** gamma', 'Beta')).toBe('**Beta**')
  })

  it('returns the list markdown for a multi-item visible selection', () => {
    expect(markdownForVisibleSelection('- First\n- Second', 'First\nSecond')).toBe('- First\n- Second')
  })

  it('returns inline code markup for selected code text', () => {
    expect(markdownForVisibleSelection('Use `const x = 1` here.', 'const x = 1')).toBe('`const x = 1`')
  })

  it('returns null when the visible selection is ambiguous', () => {
    expect(markdownForVisibleSelection('Alpha\n\nAlpha', 'Alpha')).toBeNull()
  })

  it('returns null when the visible selection cannot be mapped', () => {
    expect(markdownForVisibleSelection('Alpha **Beta** gamma', 'Missing')).toBeNull()
  })
})
