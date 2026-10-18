/**
 * Length guardrail for attached documents. Overly long documents exceed the
 * model context window and would make the request unusable, so this module
 * centralises the (conservative, deliberately visible) budget math used at
 * ingest time and in the composer indicator. The backend re-clamps against the
 * real model context window as the authoritative layer.
 */

/** Conservative per-document soft cap, in estimated tokens. A document above
 * this is truncated at ingest with a visible warning. */
export const MAX_DOC_TOKENS_SOFT = 24_000

/** Conservative shared ceiling for all attached documents of one request when
 * the selected model's real context window is unknown. */
export const DEFAULT_ATTACHMENT_BUDGET_TOKENS = 96_000

/** Rough chars-to-tokens estimate (~4 chars per token), aligned with the
 * backend's chars-approximately-tokens budgeting heuristic. */
export function estimateTokens(chars: number): number {
  return Math.ceil(chars / 4)
}

/** Per-document character cap derived from the soft token cap. */
export const MAX_DOC_CHARS_SOFT = MAX_DOC_TOKENS_SOFT * 4

export type ReferenceBudgetInput = {
  content: string
  label: string
}

export type BudgetEvaluation = {
  estTokens: number
  limitTokens: number
  offenders: string[]
  overBy: number
  withinBudget: boolean
}

/**
 * Aggregate token estimate across attached documents against a budget. Callers
 * may pass the selected model's context window via `limitTokens` when known;
 * otherwise a conservative shared ceiling is used. `offenders` lists labels of
 * individual documents above the per-document soft cap.
 */
export function evaluateBudget(
  docs: readonly ReferenceBudgetInput[],
  options?: { limitTokens?: number },
): BudgetEvaluation {
  const limitTokens = options?.limitTokens ?? DEFAULT_ATTACHMENT_BUDGET_TOKENS
  const estTokens = docs.reduce((sum, doc) => sum + estimateTokens(doc.content.length), 0)
  const offenders = docs
    .filter((doc) => estimateTokens(doc.content.length) > MAX_DOC_TOKENS_SOFT)
    .map((doc) => doc.label)
  return {
    estTokens,
    limitTokens,
    offenders,
    overBy: Math.max(0, estTokens - limitTokens),
    withinBudget: estTokens <= limitTokens,
  }
}

/** Show only a soft warning once attachments exceed the shared request budget. */
export function shouldShowAttachmentBudgetNotice(evaluation: BudgetEvaluation): boolean {
  return !evaluation.withinBudget
}
