import { describe, expect, it } from 'vitest'
import { detectSkillSlash } from './skillSlash'

describe('detectSkillSlash', () => {
  it('detects a slash token at start and after whitespace', () => {
    expect(detectSkillSlash('/', 1)).toEqual({ query: '', start: 0 })
    expect(detectSkillSlash('/spre', 5)).toEqual({ query: 'spre', start: 0 })
    expect(detectSkillSlash('Bitte /email', 12)).toEqual({
      query: 'email',
      start: 6,
    })
  })

  it('ignores slashes inside paths and after the caret', () => {
    expect(detectSkillSlash('docs/readme', 11)).toBeNull()
    expect(detectSkillSlash('https://example.com', 19)).toBeNull()
    // Caret before the token: no active slash at the caret.
    expect(detectSkillSlash('text /spre', 4)).toBeNull()
  })

  it('normalizes the query to lowercase and stops at spaces', () => {
    expect(detectSkillSlash('/SPRE', 5)).toEqual({ query: 'spre', start: 0 })
    expect(detectSkillSlash('/done und weiter', 16)).toBeNull()
  })
})
