import { describe, expect, it } from 'vitest'
import { offersFullQuestion, revealedQuestion } from './questionReveal'

describe('offersFullQuestion', () => {
  it('offers only when the server said the row was clipped', () => {
    expect(offersFullQuestion({ questionClipped: true })).toBe(true)
  })

  it('stays silent on a question that fits', () => {
    expect(offersFullQuestion({})).toBe(false)
    expect(offersFullQuestion({ questionClipped: false })).toBe(false)
  })
})

describe('revealedQuestion', () => {
  it('shows what was actually asked', () => {
    expect(revealedQuestion('Was ist…', 'Was ist die Lage?')).toBe(
      'Was ist die Lage?',
    )
  })

  it('never blanks the bubble when the read answers with nothing', () => {
    // A row written before the payload carried the question.
    expect(revealedQuestion('Was ist…', null)).toBe('Was ist…')
    expect(revealedQuestion('Was ist…', undefined)).toBe('Was ist…')
    expect(revealedQuestion('Was ist…', '   ')).toBe('Was ist…')
  })

  it('never replaces the prefix with something shorter', () => {
    expect(revealedQuestion('Der lange Anfang…', 'kurz')).toBe(
      'Der lange Anfang…',
    )
  })
})
