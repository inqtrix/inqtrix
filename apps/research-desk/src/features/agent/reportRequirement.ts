import type { InqtrixCapabilities } from '@/features/researchRuns/types'

/** The published `capabilities.agent.report_requirement` — the one place
 * these limits come from (the server's manifest, or the demo's mirror of
 * it). */
export type ReportRequirementCapability = NonNullable<
  NonNullable<InqtrixCapabilities['agent']>['report_requirement']
>

/*
 * The result requirement a user attaches to a run: prompt-library rules,
 * attached in the Agent Desk composer as inline pills (sent as
 * `report_rule_ids` on `POST /v1/runs`). Its limits come from the
 * capability manifest (published == enforced).
 */

/**
 * The server's cap on distinct rules, or `null`.
 *
 * Deliberately without a fallback number, like the composed ceiling: a
 * server that publishes no cap is not pre-checked, and the surfaces say
 * the server checks the count on sending.
 */
export function reportRuleIdsMax(
  requirement: ReportRequirementCapability | null | undefined,
): number | null {
  const published = requirement?.max_rules
  return typeof published === 'number' && published > 0 ? published : null
}

/**
 * The server's ceiling for the COMPOSED requirement, or `null`.
 *
 * Deliberately without a fallback number: a server that publishes no
 * ceiling is not pre-checked, and the surface says so — a client-side
 * copy of the constant would be a second definition that drifts the day
 * the server changes it.
 */
export function reportComposedCharsMax(
  requirement: ReportRequirementCapability | null | undefined,
): number | null {
  const published = requirement?.max_composed_chars
  return typeof published === 'number' && published > 0 ? published : null
}

/**
 * What the server puts between two composed blocks: one blank line.
 *
 * The block sizes themselves come from the server (`composed_chars` per
 * template); only the joint is added here, as the runs API documents it.
 */
export const RULE_BLOCK_SEPARATOR_CHARS = 2

/** Composed size of the attached rules against the server's ceiling, or
 * `unchecked` when that cannot be known before sending (no published
 * ceiling, or a rule whose size the server has not reported). */
export type RuleRequirementBudget =
  | { used: number; max: number }
  | { unchecked: true }

/**
 * Sum the published block sizes of the attached rules plus one separator
 * per joint. A rule with an empty body composes into nothing and adds no
 * joint (the server drops empty blocks before it joins them).
 */
export function ruleRequirementBudget(
  sizes: readonly (number | null)[],
  max: number | null,
): RuleRequirementBudget {
  if (max === null || sizes.some((size) => size === null)) return { unchecked: true }
  const blocks = (sizes as number[]).filter((size) => size > 0)
  const joints = Math.max(0, blocks.length - 1)
  return {
    max,
    used: blocks.reduce((sum, size) => sum + size, 0) + joints * RULE_BLOCK_SEPARATOR_CHARS,
  }
}

/** The composed total once a rule of `size` joins `budget`. */
export function ruleRequirementTotalWith(
  budget: { used: number },
  size: number,
): number {
  if (size <= 0) return budget.used
  return budget.used + size + (budget.used > 0 ? RULE_BLOCK_SEPARATOR_CHARS : 0)
}
