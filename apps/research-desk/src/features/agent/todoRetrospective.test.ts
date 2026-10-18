import { describe, expect, it } from 'vitest'

import { translations } from '../../i18n/translations'
import { todoStepStats, todoStepText } from './todoRetrospective'

const t = translations.de

describe('todoStepStats / todoStepText', () => {
  const log = [
    { todoIndex: 0, startedAt: 100, at: 105 },
    { todoIndex: 1, startedAt: 110, at: 1193 },
    { todoIndex: 1, startedAt: 1194, at: 1200 },
    { at: 1300 },
    { todoIndex: 2, startedAt: 2000, at: 2000, status: 'running' },
  ]

  it('counts the rows that served a todo and spans start to settle', () => {
    expect(todoStepStats(log, 1)).toEqual({ steps: 2, seconds: 1090, running: false })
    expect(todoStepStats(log, 0)).toEqual({ steps: 1, seconds: 5, running: false })
    expect(todoStepStats(log, 7)).toEqual({ steps: 0, seconds: 0, running: false })
  })

  it('says "k Schritte · Dauer", singular for one, nothing for none', () => {
    expect(todoStepText(todoStepStats(log, 1), t)).toBe('2 Schritte · 00:18:10')
    expect(todoStepText(todoStepStats(log, 0), t)).toBe('1 Schritt · 00:00:05')
    expect(todoStepText(todoStepStats(log, 7), t)).toBe('')
  })

  it('says "läuft" while a row that served the todo is still open', () => {
    // The regression: a delegation running for nine minutes read
    // "1 Schritt · 00:00:00" because its row had no end yet.
    expect(todoStepStats(log, 2)).toEqual({ steps: 1, seconds: 0, running: true })
    expect(todoStepText(todoStepStats(log, 2), t)).toBe('1 Schritt · läuft')
  })
})
