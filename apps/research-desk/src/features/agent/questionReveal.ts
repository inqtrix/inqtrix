/**
 * Reading back a question the run row could not hold in full.
 *
 * A run row stores at most `QUESTION_COLUMN_LIMIT` characters so list
 * responses stay small, and the durable payload keeps what was actually
 * asked. The cut was always visible; what was missing is the way back to
 * the text. These are the two decisions that make that way safe, kept
 * out of the component so they can be tested without a DOM.
 */

/** Whether the transcript should offer to fetch the full question. */
export function offersFullQuestion(run: {
  questionClipped?: boolean
}): boolean {
  return run.questionClipped === true
}

/**
 * What the bubble shows once the single-run read answers.
 *
 * The clipped text is the floor, never an empty bubble: a row written
 * before the durable payload carried the question answers with nothing,
 * and blanking the user's own words would be worse than showing the
 * prefix they already had.
 */
export function revealedQuestion(
  clipped: string,
  fetched: string | null | undefined,
): string {
  const full = (fetched ?? '').trim()
  if (!full) return clipped
  return full.length >= clipped.length ? full : clipped
}
