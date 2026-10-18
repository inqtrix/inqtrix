import type { TranslationDictionary } from '@/i18n/translations'
import { countLabel } from '@/i18n/plural'
import { kernelToolLabel } from './activityPresentation'
import { agentTaskTypeLabel } from './plan/taskPresentation'
import type { AgentPlanTaskRecord } from './model'

/**
 * What a tool approval actually asks for, said plainly.
 *
 * A gate card must communicate the CATEGORY of the request (a single web
 * search is not the same decision as a fan-out of parallel sub-runs) and
 * the COMPLETE text being approved — the payload rows carry the full
 * args verbatim, so nothing here may truncate. Unknown tools stay
 * visible under their raw id (never guessed).
 */

export type GateActionRow = {
  /** Human tool label (raw id when unknown). */
  label: string
  /** The full primary text being approved (query/question/assignment). */
  text: string
  /** Fan-out items (delegate_batch): one full objective per entry. */
  items: string[]
}

const PRIMARY_ARG_KEYS = ['query', 'question', 'assignment', 'skill_id'] as const

function primaryText(args: Record<string, unknown> | undefined): string {
  if (!args) return ''
  for (const key of PRIMARY_ARG_KEYS) {
    const value = args[key]
    if (typeof value === 'string' && value.trim()) return value
  }
  return ''
}

export function gateActionRow(
  action: Record<string, unknown>,
  t: TranslationDictionary,
): GateActionRow {
  const tool = String(action.tool ?? '')
  const args = (action.args ?? undefined) as Record<string, unknown> | undefined
  const assignments = Array.isArray(args?.assignments)
    ? (args.assignments as Record<string, unknown>[])
    : []
  return {
    label: kernelToolLabel(tool, t),
    text: primaryText(args),
    items: assignments.map((assignment) => {
      const objective = String(assignment.objective ?? '')
      const mode = assignment.mode ? ` (${String(assignment.mode)})` : ''
      return `${objective}${mode}`
    }),
  }
}

/**
 * The gate headline: one specific sentence naming the request category.
 * Multi-action batches and unknown tools stay honest (count / raw id).
 */
export function toolGateHeadline(
  actions: Record<string, unknown>[],
  t: TranslationDictionary,
): string {
  if (actions.length !== 1) {
    return t.agent.timeline.gateActionsTitle.replace(
      '{count}',
      String(actions.length),
    )
  }
  const action = actions[0]
  const tool = String(action.tool ?? '')
  switch (tool) {
    case 'web_instant':
      return t.agent.timeline.gateWebInstantTitle
    case 'search_project_knowledge':
      return t.agent.timeline.gateKnowledgeTitle
    case 'run_web_research':
      return t.agent.timeline.gateWebResearchTitle
    case 'run_deep_mission':
      return t.agent.timeline.gateDeepMissionTitle
    case 'delegate_batch': {
      const args = (action.args ?? {}) as Record<string, unknown>
      const count = Array.isArray(args.assignments)
        ? args.assignments.length
        : 0
      return countLabel(
        count,
        t.agent.timeline.gateFanoutTitleOne,
        t.agent.timeline.gateFanoutTitle,
      )
    }
    case 'load_skill':
      return t.agent.timeline.gateSkillTitle
    case 'propose_editor_patch':
      return t.agent.timeline.gatePatchTitle
    case 'read_project_document':
    case 'read_canvas':
    case 'write_canvas':
      return t.agent.timeline.gateGenericTitle.replace(
        '{tool}',
        kernelToolLabel(tool, t),
      )
    default:
      return t.agent.timeline.gateGenericTitle.replace('{tool}', tool)
  }
}

/** One plain sentence on what approving actually starts; '' = no extra. */
export function toolGateExplanation(
  actions: Record<string, unknown>[],
  t: TranslationDictionary,
): string {
  if (actions.length !== 1) return t.agent.timeline.gateActionsHint
  switch (String(actions[0].tool ?? '')) {
    case 'web_instant':
      return t.agent.timeline.gateWebInstantHint
    case 'search_project_knowledge':
      return t.agent.timeline.gateKnowledgeHint
    case 'run_web_research':
      return t.agent.timeline.gateWebResearchHint
    case 'run_deep_mission':
      return t.agent.timeline.gateDeepMissionHint
    case 'delegate_batch':
      return t.agent.timeline.gateFanoutHint
    case 'load_skill':
      return t.agent.timeline.gateSkillHint
    default:
      return t.agent.timeline.toolApprovalHint
  }
}

/**
 * Recover the FULL delegated text behind a clipped run-row question.
 *
 * The question COLUMN bounds at 500 chars (visible "…"), but the
 * delegation approval's payload carries the args verbatim — when one of
 * its texts starts with the clipped prefix, that is the complete
 * assignment the user originally approved.
 */
export function fullDelegationText(
  approvals: readonly { payload: Record<string, unknown> }[],
  clippedQuestion: string,
): string | null {
  const prefix = clippedQuestion.endsWith('…')
    ? clippedQuestion.slice(0, -1)
    : clippedQuestion
  if (!prefix) return null
  for (const approval of approvals) {
    const actions = Array.isArray(approval.payload.actions)
      ? (approval.payload.actions as Record<string, unknown>[])
      : []
    for (const action of actions) {
      const args = (action.args ?? {}) as Record<string, unknown>
      const candidates: string[] = []
      for (const key of ['question', 'assignment'] as const) {
        if (typeof args[key] === 'string') candidates.push(args[key] as string)
      }
      if (Array.isArray(args.assignments)) {
        for (const assignment of args.assignments as Record<string, unknown>[]) {
          if (typeof assignment.objective === 'string') {
            candidates.push(assignment.objective)
          }
        }
      }
      for (const candidate of candidates) {
        if (candidate.length > prefix.length && candidate.startsWith(prefix)) {
          return candidate
        }
      }
    }
  }
  return null
}

/**
 * Whether a tool gate may offer the run-wide grant: only the
 * balanced mode honors grants (strict stays per-call by design, the
 * server ignores grants there), and the always-gated patch tool is
 * never grantable — the server would refuse with 400.
 */
export function canGrantForRun(
  autonomy: string | undefined,
  actions: Record<string, unknown>[],
): boolean {
  if ((autonomy ?? 'balanced') !== 'balanced') return false
  if (actions.length === 0) return false
  return actions.every(
    (action) => String(action.tool ?? '') !== 'propose_editor_patch',
  )
}

/**
 * Collection names the gated run may reach. The boundary is
 * pinned at submission and enforced server-side; the card names it so
 * the decision is not blind. An absent key renders nothing — a gate
 * from an older run must not claim an empty scope.
 */
export function gateKnowledgeScope(
  payload: Record<string, unknown>,
): string[] {
  const raw = payload.knowledge_scope
  if (!Array.isArray(raw)) return []
  return raw.filter(
    (entry): entry is string => typeof entry === 'string' && entry.length > 0,
  )
}


/**
 * What a delegated mission's REPLAN gate asks for: the reason
 * the plan grew and the tasks the new version adds — said from the
 * approval payload alone, without fetching the child's plan. An unknown
 * reason code stays visible verbatim; a task kind the labels do not know
 * is shown by its raw id.
 */
export type ReplanGateSummary = {
  /** "The delegated run wants to extend the plan (v2): <reason>" */
  hint: string
  /** "Open tasks in plan v2:" — only when there are rows. */
  heading: string
  /** One row per not yet executed task: "<type> · <title>". */
  rows: string[]
}

const REPLAN_REASON: Record<string, (t: TranslationDictionary) => string> = {
  critic_research: (t) => t.agent.tray.replanReasonCritic,
  replan: (t) => t.agent.tray.replanReasonGap,
}

export function replanGateSummary(
  payload: Record<string, unknown>,
  t: TranslationDictionary,
): ReplanGateSummary {
  const version = String(payload.plan_version ?? '')
  const reasonCode = String(payload.reason ?? '')
  const reason = REPLAN_REASON[reasonCode]?.(t) ?? reasonCode
  const tasks = Array.isArray(payload.new_tasks)
    ? (payload.new_tasks as Record<string, unknown>[])
    : []
  const rows = tasks.map((task) => {
    const toolKind = String(task.tool_kind ?? '')
    const typeLabel = agentTaskTypeLabel(toolKind as AgentPlanTaskRecord['toolKind'], t) || toolKind
    const title = String(task.title ?? '').trim()
    return title ? `${typeLabel} · ${title}` : typeLabel
  })
  return {
    hint: t.agent.tray.childReplanHint
      .replace('{version}', version)
      .replace('{reason}', reason),
    heading: t.agent.tray.childReplanTasks.replace('{version}', version),
    rows,
  }
}
