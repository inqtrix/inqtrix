import type { AgentRuleSource } from '../contextCategories'
import type { AgentApprovalRecord } from '../model'
import type { ReportRuleOption } from './PlanReviewBody'

/**
 * The result requirement that is actually in force for a run.
 *
 * It lives in the plan gate's decision payload, not in the plan, so a
 * finished plan cannot show it from the plan alone. Reading it back is
 * the difference between a requirement the user can verify and one they
 * have to remember: the value shaped every section of the report.
 *
 * Presence decides, in decision order — an approval that carried no
 * requirement key leaves the earlier one standing (the server's resume
 * does the same), while an explicitly emptied one clears it. Only plan
 * gates can carry it; the server rejects it anywhere else.
 */
export function decidedReportGuidance(
  approvals: readonly AgentApprovalRecord[],
): string {
  let effective = ''
  for (const approval of approvals) {
    if (approval.kind !== 'plan' && approval.kind !== 'replan') continue
    if (approval.status === 'pending') continue
    const payload = approval.decisionPayload
    // The parts, when the decision kept them: `report_guidance` holds
    // the COMPOSED text with its `[Rule: …]` / `[Free requirement]`
    // markers, which is what the model reads — showing that back would
    // be showing the user the prompt. Older decisions have only the
    // composed value and fall back to it.
    if (payload && 'report_requirement' in payload) {
      const requirement = payload.report_requirement
      const freeText =
        requirement && typeof requirement === 'object'
          ? (requirement as { free_text?: unknown }).free_text
          : undefined
      effective = typeof freeText === 'string' ? freeText.trim() : ''
      continue
    }
    if (!payload || !('report_guidance' in payload)) continue
    const value = payload.report_guidance
    effective = typeof value === 'string' ? value.trim() : ''
  }
  return effective
}

/**
 * Labels of the library rules the decided gate ran under.
 *
 * The composed requirement in the prompt carries `[Rule: …]` markers,
 * but showing those to the user would be showing them the prompt. The
 * decision keeps the parts for exactly this: naming what was attached.
 */
export function decidedReportRuleLabels(
  approvals: readonly AgentApprovalRecord[],
): string[] {
  let labels: string[] = []
  for (const approval of approvals) {
    if (approval.kind !== 'plan' && approval.kind !== 'replan') continue
    if (approval.status === 'pending') continue
    const payload = approval.decisionPayload
    if (!payload || !('report_requirement' in payload)) continue
    const requirement = payload.report_requirement
    if (!requirement || typeof requirement !== 'object') {
      labels = []
      continue
    }
    const rules = (requirement as { rules?: unknown }).rules
    labels = Array.isArray(rules)
      ? rules
        .map((rule) =>
          rule && typeof rule === 'object'
            ? String((rule as { label?: unknown }).label ?? '')
            : '',
        )
        .filter((label) => label.length > 0)
      : []
  }
  return labels
}

/** One library rule as the run is frozen under it. */
export type GateRuleInForce = { templateId: string; label: string; revision: number }

/** The requirement in force when a plan or replan gate opened. */
export type GateRequirementInForce = { freeText: string; rules: GateRuleInForce[] }

/**
 * The requirement a pending gate opens with, read from its payload parts
 * — or `null` when the gate carries none (nothing in force, a gate from an
 * older server, a requirement the server could not split into its parts).
 *
 * Without it the gate draft started empty, and the first touch of the
 * typed text sent an empty rule list: the server reads that as "remove
 * the rules", so the rules the run was submitted with vanished without
 * anybody choosing that. A payload whose parts are not all whole is
 * refused as a whole (with a warning), never prefilled in part: a dropped
 * rule would be removed by exactly that edit.
 */
export function gateRequirementInForce(
  approval: AgentApprovalRecord | null | undefined,
): GateRequirementInForce | null {
  const raw = approval?.payload?.report_requirement
  if (raw === undefined || raw === null) return null
  const requirement = typeof raw === 'object' ? raw as Record<string, unknown> : null
  const freeText = requirement?.free_text
  const rawRules = requirement?.rules
  const rules = Array.isArray(rawRules)
    ? rawRules.map((item): GateRuleInForce | null => {
      if (typeof item !== 'object' || item === null) return null
      const part = item as Record<string, unknown>
      return typeof part.template_id === 'string' && part.template_id
        && typeof part.label === 'string'
        && Number.isInteger(part.revision) && Number(part.revision) > 0
        ? { label: part.label, revision: Number(part.revision), templateId: part.template_id }
        : null
    })
    : null
  if (typeof freeText !== 'string' || !rules || rules.some((rule) => rule === null)) {
    console.warn(
      'Plan gate payload carries a malformed report_requirement; the gate opens without a prefill.',
      raw,
    )
    return null
  }
  return { freeText, rules: rules as GateRuleInForce[] }
}

/**
 * The catalog a gate may ADD rules from — the agent rules the composer
 * lets the user pick, and no others: a context pack with linked files
 * would travel as a literal `{{context}}` placeholder (the files stay in
 * the browser), and a rule the server does not know yet is refused.
 */
export function gateRuleCatalog(rules: readonly AgentRuleSource[]): ReportRuleOption[] {
  return rules
    .filter((rule) => !rule.notSynced && !rule.linkedFiles)
    .map((rule) => ({ label: rule.label, ruleId: rule.id, title: rule.title }))
}

/**
 * The rules a gate offers: every rule in force first — under the frozen
 * version the run works under, also when the library has since edited,
 * unshared or deleted it — then the rest of the caller's catalog.
 *
 * The server keeps a listed rule that is in force at its frozen revision
 * and text; only a rule the selection ADDS is read from the catalog. So a
 * rule in force must stay visible and keepable by id, never be filtered
 * out because the catalog no longer lists it. Its title comes from the
 * catalog while it lists the rule, from its label once it does not.
 */
export function gateRuleOptions(
  catalog: readonly ReportRuleOption[],
  inForce: GateRequirementInForce | null,
): ReportRuleOption[] {
  if (!inForce || inForce.rules.length === 0) return [...catalog]
  const titles = new Map(catalog.map((option) => [option.ruleId, option.title]))
  const kept = inForce.rules.map((rule) => ({
    frozenRevision: rule.revision,
    label: rule.label,
    ruleId: rule.templateId,
    title: titles.get(rule.templateId) ?? rule.label,
  }))
  const keptIds = new Set(kept.map((rule) => rule.ruleId))
  return [...kept, ...catalog.filter((option) => !keptIds.has(option.ruleId))]
}

/**
 * The tooltip of a gate rule chip. A rule in force names the version the
 * run works under SEPARATELY from what a click does — attached, a click
 * detaches it; detached, a click attaches it again — so the note never
 * announces the opposite of the click.
 */
export function gateRuleChipTitle(
  option: ReportRuleOption,
  attached: boolean,
  labels: { frozen: (revision: number) => string; attach: string; detach: string },
): string {
  // The chip shows the title; its tooltip carries the handle the rule is
  // typed by, as the "@" menu shows it.
  const handle = `@rules:${option.label}`
  if (option.frozenRevision === undefined) return handle
  return [
    handle,
    labels.frozen(option.frozenRevision),
    attached ? labels.detach : labels.attach,
  ].join(' — ')
}

