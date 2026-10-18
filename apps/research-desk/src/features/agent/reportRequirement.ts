import type { InqtrixCapabilities } from '@/features/researchRuns/types'

/** The published `capabilities.agent.report_requirement` — the one place
 * these limits come from (the server's manifest, or the demo's mirror of
 * it). */
export type ReportRequirementCapability = NonNullable<
  NonNullable<InqtrixCapabilities['agent']>['report_requirement']
>

/**
 * The result requirement a user attaches to a run: free text plus
 * prompt-library rules.
 *
 * It has three entry points: the Agent Desk composer attaches rules as
 * inline pills (sent as `report_rule_ids` on `POST /v1/runs`), the plan
 * gate edits the rules and sets the typed text, and API clients send
 * either directly. Every entry point is bounded by the SAME server limits.
 * The numbers come from the capability manifest (published == enforced).
 * The typed-text fallback below applies only to a server too old to
 * publish it and matches that server's own hardcoded value; the rule cap
 * and the composed ceiling have none — without a published number the
 * surfaces skip the pre-check and say so.
 */
export const REPORT_GUIDANCE_MAX_CHARS_FALLBACK = 2000

export function reportGuidanceMaxChars(
  requirement: ReportRequirementCapability | null | undefined,
): number {
  const published = requirement?.max_chars
  return typeof published === 'number' && published > 0
    ? published
    : REPORT_GUIDANCE_MAX_CHARS_FALLBACK
}

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

/**
 * Toggle one rule, honoring the server's cap.
 *
 * At the cap, selecting a further rule is a NO-OP rather than a silent
 * eviction of an earlier one: dropping a rule the user attached — to
 * make room for one they just picked — is exactly the kind of quiet
 * substitution that makes a requirement untrustworthy. The surface
 * disables the remaining entries so the cap is visible before the click.
 * Without a published cap (`max` null) nothing is pre-checked here; the
 * server checks the count on sending.
 */
export function toggleReportRule(
  ruleIds: readonly string[],
  ruleId: string,
  max: number | null,
): string[] {
  if (ruleIds.includes(ruleId)) {
    return ruleIds.filter((id) => id !== ruleId)
  }
  if (max !== null && ruleIds.length >= max) return [...ruleIds]
  return [...ruleIds, ruleId]
}
