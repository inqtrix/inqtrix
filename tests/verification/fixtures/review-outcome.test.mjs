import assert from 'node:assert/strict'
import test from 'node:test'

import {
  REVIEW_BAND_CEILING,
  judgeReviewBand,
  judgeReviewOutcome,
} from './review-outcome.mjs'

const completed = (overrides = {}) => ({
  basis: 'evidence',
  review: 'completed',
  summary: 'Geprüft · ohne Befund',
  ...overrides,
})

const inconsistent = (overrides = {}) => ({
  review: 'unavailable',
  summary: 'Prüfurteil widersprüchlich: Vollständigkeit ohne Begründung',
  unavailable_reason: 'inconsistent',
  unexplained: ['complete'],
  ...overrides,
})

test('a review that ended as its level owes passes, on every known basis', () => {
  for (const basis of ['evidence', 'knowledge', 'provider_context']) {
    assert.deepEqual(judgeReviewOutcome(completed({ basis }), 'completed'), { status: 'passed' })
  }
  assert.deepEqual(judgeReviewOutcome({ review: 'waived' }, 'waived'), { status: 'passed' })
  assert.deepEqual(judgeReviewOutcome({ review: 'not_run' }, 'not_run'), { status: 'passed' })
})

test('a completed review must name a known basis and carry its reader text', () => {
  const unknown = judgeReviewOutcome(completed({ basis: 'web' }), 'completed')
  assert.equal(unknown.status, 'failed')
  assert.match(unknown.problem, /basis/)
  const missing = judgeReviewOutcome(completed({ basis: undefined }), 'completed')
  assert.equal(missing.status, 'failed')
  const silent = judgeReviewOutcome(completed({ summary: ' ' }), 'completed')
  assert.equal(silent.status, 'failed')
  assert.match(silent.problem, /reader text/)
})

test('a missing record and a review state the level does not owe are failures', () => {
  assert.equal(judgeReviewOutcome(undefined, 'completed').status, 'failed')
  const waived = judgeReviewOutcome({ review: 'waived' }, 'completed')
  assert.equal(waived.status, 'failed')
  assert.match(waived.problem, /waived/)
  assert.equal(judgeReviewOutcome(completed(), 'waived').status, 'failed')
  // An unavailable review is in the band only where a review was owed.
  assert.equal(judgeReviewOutcome(inconsistent(), 'not_run').status, 'failed')
})

test('an inconsistent verdict that names its flag in record and reader text is a warning', () => {
  assert.deepEqual(judgeReviewOutcome(inconsistent(), 'completed'), {
    reason: 'inconsistent', status: 'warning', unexplained: ['complete'],
  })
  const english = inconsistent({
    summary: 'Review verdict contradictory: completeness, grounding without a reason',
    unexplained: ['complete', 'grounded'],
  })
  assert.equal(judgeReviewOutcome(english, 'completed').status, 'warning')
})

test('an inconsistent verdict whose reader text does not name the flag fails', () => {
  // The shared sentence without the flag is exactly what the reader must not get.
  const generic = inconsistent({ summary: 'Prüfung nicht auswertbar, Ergebnis ungeprüft' })
  const verdict = judgeReviewOutcome(generic, 'completed')
  assert.equal(verdict.status, 'failed')
  assert.match(verdict.problem, /complete/)
  // Every unexplained flag is named, not just one of them.
  const half = inconsistent({ unexplained: ['complete', 'contradictions_named'] })
  assert.match(judgeReviewOutcome(half, 'completed').problem, /contradictions_named/)
})

test('an inconsistent verdict without its flags in the record fails', () => {
  for (const unexplained of [undefined, [], ['complete', 'tone'], 'complete']) {
    const verdict = judgeReviewOutcome(inconsistent({ unexplained }), 'completed')
    assert.equal(verdict.status, 'failed', JSON.stringify(unexplained))
    assert.match(verdict.problem, /flag/)
  }
})

test('a review stopped at its budget is a warning only when the reader text says so', () => {
  const stopped = {
    review: 'unavailable',
    summary: 'Prüfung am Budget beendet, Ergebnis ungeprüft',
    unavailable_reason: 'budget_exhausted',
  }
  assert.deepEqual(judgeReviewOutcome(stopped, 'completed'), {
    reason: 'budget_exhausted', status: 'warning', unexplained: [],
  })
  const silent = { ...stopped, summary: 'Prüfung nicht auswertbar, Ergebnis ungeprüft' }
  assert.equal(judgeReviewOutcome(silent, 'completed').status, 'failed')
})

test('an unavailable review outside the band, or without a reason, fails', () => {
  for (const reason of ['failed', 'context', 'result_missing', 'schema_invalid', 'unavailable']) {
    const verdict = judgeReviewOutcome(
      { review: 'unavailable', summary: 'Prüfung gescheitert, Ergebnis ungeprüft', unavailable_reason: reason },
      'completed',
    )
    assert.equal(verdict.status, 'failed', reason)
    assert.match(verdict.problem, new RegExp(reason))
  }
  for (const reason of [undefined, '', 7]) {
    const verdict = judgeReviewOutcome({ review: 'unavailable', summary: 'x', unavailable_reason: reason }, 'completed')
    assert.equal(verdict.status, 'failed')
    assert.match(verdict.problem, /names no reason/)
  }
})

test('the band is bounded: above its ceiling the reviews are a defect again', () => {
  const turn = (marker, reason = 'inconsistent') => ({ marker, reason })
  assert.deepEqual(judgeReviewBand([], 9), { status: 'passed' })
  const inside = judgeReviewBand([turn('R3'), turn('R5', 'budget_exhausted')], 9)
  assert.equal(inside.status, 'passed')
  assert.equal(
    inside.note,
    `warning band: 2 of 9 reviews without a usable verdict (ceiling ${REVIEW_BAND_CEILING}): `
      + 'R3 inconsistent, R5 budget_exhausted',
  )
  const above = judgeReviewBand([turn('R1'), turn('R2'), turn('R3')], 9)
  assert.equal(above.status, 'failed')
  assert.match(above.problem, /3 of 9 reviews/)
  assert.match(above.problem, new RegExp(`ceiling ${REVIEW_BAND_CEILING}`))
  assert.equal(REVIEW_BAND_CEILING, 2)
})
