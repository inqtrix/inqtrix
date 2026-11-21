// What a finished turn's outcome record must say about its review, judged
// apart from the browser so the rule is testable without a stack.
//
// A turn's published verify level owes one review state: `completed`,
// `waived` or `not_run`. A review that was owed and left no usable verdict
// (`unavailable`) is a failure, with one bounded exception: the two ways a
// review can end without a verdict although nothing is broken. A model
// verdict may negate a flag and name no reason for it (`inconsistent`), and a
// review role may reach its tool budget before it judges
// (`budget_exhausted`). Those are a warning, never silently a pass: the
// record must carry the reason, the reader text must name it, and more of
// them in one conversation than the ceiling allows is a failure again.

/** What a completed review may have been checked against. */
export const REVIEW_BASES = Object.freeze(['evidence', 'knowledge', 'provider_context'])

/** The reasons of an unavailable review that stay inside the warning band. */
export const REVIEW_BAND_REASONS = Object.freeze(['inconsistent', 'budget_exhausted'])

/** The verdict flags an inconsistent review can leave unexplained, each with
 * the words the reader text names it by (German and English: the reader text
 * follows the language of the question). The server's own words; a test pins
 * the two tables together. */
export const REVIEW_FLAG_WORDS = Object.freeze({
  complete: ['Vollständigkeit', 'completeness'],
  grounded: ['Belegtreue', 'grounding'],
  contradictions_named: ['Gegenpositionen', 'opposing views'],
})

/** How many reviews of one conversation may end inside the band: one turn
 * whose review role reads a whole conversation can reach its tool budget, and
 * one model verdict can contradict itself. A third is no accident. */
export const REVIEW_BAND_CEILING = 2

const text = (value) => (typeof value === 'string' ? value.trim() : '')

/**
 * Judge one outcome record against the review state its level owes.
 *
 * Returns `{ status: 'passed' }`, `{ status: 'warning', reason, unexplained }`
 * for an unavailable review inside the band whose record and reader text
 * both name the reason, or `{ status: 'failed', problem }`.
 */
export function judgeReviewOutcome(record, expected) {
  const failed = (problem) => ({ problem, status: 'failed' })
  if (!record || typeof record !== 'object') return failed('sent no outcome record')
  const summary = text(record.summary)
  if (record.review === expected) {
    if (expected !== 'completed') return { status: 'passed' }
    if (!REVIEW_BASES.includes(record.basis)) {
      return failed(`completed its review on the unknown basis ${JSON.stringify(record.basis ?? null)}`)
    }
    if (!summary) return failed('completed its review without a reader text')
    return { status: 'passed' }
  }
  if (expected !== 'completed' || record.review !== 'unavailable') {
    return failed(`owes a review that is ${expected} and records it as ${String(record.review)}`)
  }
  const reason = text(record.unavailable_reason)
  if (!reason) return failed('ended its review without a usable verdict and names no reason')
  if (!REVIEW_BAND_REASONS.includes(reason)) {
    return failed(`ended its review without a usable verdict (${reason})`)
  }
  if (reason === 'budget_exhausted') {
    if (!/budget/i.test(summary)) {
      return failed('stopped its review at the budget, and the reader text does not say so')
    }
    return { reason, status: 'warning', unexplained: [] }
  }
  const flags = record.unexplained
  if (!Array.isArray(flags) || flags.length === 0
    || flags.some((flag) => !Object.hasOwn(REVIEW_FLAG_WORDS, flag))) {
    return failed(`has an inconsistent verdict and names no known flag for it: ${JSON.stringify(flags ?? null)}`)
  }
  const unnamed = flags.filter((flag) => !REVIEW_FLAG_WORDS[flag].some((word) => summary.includes(word)))
  if (unnamed.length > 0) {
    return failed(`has an inconsistent verdict, and the reader text does not name ${unnamed.join(', ')}`)
  }
  return { reason, status: 'warning', unexplained: [...flags] }
}

/**
 * Judge the band of one conversation: `warnings` are its turns inside the
 * band (`{ marker, reason }`), `reviewed` the number of turns judged.
 *
 * Inside the ceiling the band passes and `note` states target and measured
 * value for the report; above it the reviews are a defect again.
 */
export function judgeReviewBand(warnings, reviewed, ceiling = REVIEW_BAND_CEILING) {
  if (warnings.length === 0) return { status: 'passed' }
  const measured = `${warnings.length} of ${reviewed} reviews without a usable verdict (ceiling ${ceiling}): `
    + warnings.map(({ marker, reason }) => `${marker} ${reason}`).join(', ')
  if (warnings.length > ceiling) return { problem: measured, status: 'failed' }
  return { note: `warning band: ${measured}`, status: 'passed' }
}
