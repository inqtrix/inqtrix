/**
 * Pure draft logic of a structured clarification round.
 *
 * The gate parks the run exactly once per round, so the tray submits only
 * when EVERY question is resolved — either by picked option(s) or by
 * per-question free text ("Sonstiges"). Keeping this logic pure (no React,
 * no I/O) makes the submit-enablement contract unit-testable in the
 * node-only vitest setup.
 */

import type {
  AgentClarificationQuestion,
  AgentClarificationRecord,
} from './model'

export type RoundAnswerDraft = Record<
  string,
  { optionIds: string[]; text: string }
>

const EMPTY_ENTRY = { optionIds: [] as string[], text: '' }

function entryOf(draft: RoundAnswerDraft, questionId: string) {
  return draft[questionId] ?? EMPTY_ENTRY
}

/** Toggle one option: single-select replaces, multi-select toggles. */
export function toggleOption(
  draft: RoundAnswerDraft,
  question: AgentClarificationQuestion,
  optionId: string,
): RoundAnswerDraft {
  const entry = entryOf(draft, question.id)
  let optionIds: string[]
  if (question.multiSelect) {
    optionIds = entry.optionIds.includes(optionId)
      ? entry.optionIds.filter((id) => id !== optionId)
      : [...entry.optionIds, optionId]
  } else {
    optionIds = entry.optionIds.includes(optionId) ? [] : [optionId]
  }
  return { ...draft, [question.id]: { ...entry, optionIds } }
}

/** Set the per-question free text (empty string clears it). */
export function setFreeText(
  draft: RoundAnswerDraft,
  questionId: string,
  text: string,
): RoundAnswerDraft {
  const entry = entryOf(draft, questionId)
  return { ...draft, [questionId]: { ...entry, text } }
}

/** Whether one question is resolved (>=1 pick or non-empty text). */
export function isQuestionResolved(
  draft: RoundAnswerDraft,
  question: AgentClarificationQuestion,
): boolean {
  const entry = entryOf(draft, question.id)
  return entry.optionIds.length > 0 || entry.text.trim().length > 0
}

/** Whether the whole round can be submitted (server rejects partials). */
export function isRoundComplete(
  questions: AgentClarificationQuestion[],
  draft: RoundAnswerDraft,
): boolean {
  return (
    questions.length > 0
    && questions.every((question) => isQuestionResolved(draft, question))
  )
}

/** Human-readable Q/A lines of an ANSWERED round for the transcript.

 * Structured rounds compose per question (picked labels, then free
 * text); legacy rounds fall back to the whole-round answer or the
 * picked option label. Empty when nothing was answered yet. */
export function clarificationAnswerSummary(
  clarification: AgentClarificationRecord,
): { prompt: string; answer: string }[] {
  const entries = Object.entries(clarification.answers)
  if (entries.length > 0 && clarification.questions.length > 0) {
    const lines: { prompt: string; answer: string }[] = []
    for (const question of clarification.questions) {
      const entry = clarification.answers[question.id]
      if (!entry) continue
      const labels = new Map(
        question.options.map((option) => [option.id, option.label]),
      )
      const picked = entry.optionIds
        .map((id) => labels.get(id))
        .filter((label): label is string => Boolean(label))
      const parts = [picked.join('; '), entry.text.trim()].filter(Boolean)
      if (parts.length > 0) {
        lines.push({ prompt: question.prompt, answer: parts.join(' — ') })
      }
    }
    return lines
  }
  const legacy =
    clarification.answer
    || clarification.options.find(
      (option) => option.id === clarification.optionId,
    )?.label
    || ''
  return legacy ? [{ prompt: clarification.question, answer: legacy }] : []
}

/** The POST body shape (`answers`), trimmed and wire-cased. */
export function answersRequestFromDraft(
  questions: AgentClarificationQuestion[],
  draft: RoundAnswerDraft,
): Record<string, { option_ids: string[]; text: string }> {
  const answers: Record<string, { option_ids: string[]; text: string }> = {}
  for (const question of questions) {
    const entry = entryOf(draft, question.id)
    answers[question.id] = {
      option_ids: entry.optionIds,
      text: entry.text.trim(),
    }
  }
  return answers
}
