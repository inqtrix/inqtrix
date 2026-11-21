import type { TranslationDictionary } from '@/i18n/translations'
import { countLabel } from '@/i18n/plural'
import { kernelToolLabel } from './activityPresentation'

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
  /** A condition of the request the approver has to see beside its text:
   * the period a web research prefers for its sources, the form a web
   * search is asked to answer in, or that what the call names is not
   * applied. It says what the call asks for; whether the search provider
   * of the run takes a form, the card does not know. '' = the call names
   * none. */
  note: string
  /** Fan-out items (delegate_batch): one full objective per entry. */
  items: string[]
}

/** The argument that says what a call is for, in the order the server's
 * tool rows read them (`objective`: what a document analysis is for). */
const PRIMARY_ARG_KEYS = ['query', 'question', 'assignment', 'objective', 'skill_id'] as const

function primaryText(args: Record<string, unknown> | undefined): string {
  if (!args) return ''
  for (const key of PRIMARY_ARG_KEYS) {
    const value = args[key]
    if (typeof value === 'string' && value.trim()) return value
  }
  return ''
}

/** The periods a research takes, by the value a call names them with. */
const RECENCY_WINDOWS = new Map<string, (t: TranslationDictionary) => string>([
  ['day', (t) => t.agent.timeline.gateRecencyDay],
  ['week', (t) => t.agent.timeline.gateRecencyWeek],
  ['month', (t) => t.agent.timeline.gateRecencyMonth],
  ['year', (t) => t.agent.timeline.gateRecencyYear],
])

/**
 * What the card says about the period a call names for its sources.
 *
 * The tool reads one of its four periods whatever its case and surrounding
 * blanks, on a research only, and takes a missing, empty or null value for
 * none. A call that names anything else, or names a period for a subtask
 * that is no research, is refused: the card then says that the period is
 * not applied, with the value as it was sent, instead of announcing one.
 */
function recencyNote(
  value: unknown,
  research: boolean,
  t: TranslationDictionary,
): string {
  if (value === undefined || value === null) return ''
  const named = typeof value === 'string' ? value.trim() : JSON.stringify(value)
  if (!named) return ''
  const label = research ? RECENCY_WINDOWS.get(named.toLowerCase()) : undefined
  return label
    ? t.agent.timeline.gateRecencyNote.replace('{window}', () => label(t))
    : t.agent.timeline.gateRecencyRefused.replace('{window}', () => named)
}

/**
 * The forms a web search can be asked to answer in, by the names the
 * search tool takes (a test of the server holds both lists together).
 */
export const SEARCH_ANSWER_FORMS = ['overview', 'table', 'timeline'] as const
export type SearchAnswerForm = (typeof SEARCH_ANSWER_FORMS)[number]

/** The form of a search nobody asked a form of: choosing it asks for nothing. */
export const DEFAULT_SEARCH_ANSWER_FORM: SearchAnswerForm = SEARCH_ANSWER_FORMS[0]

/** The argument of the search tool that carries the form. */
export const ANSWER_FORM_ARG = 'answer_form'

/** The label of a form of the search answer, in the approver's words. */
export function searchAnswerFormLabel(
  form: SearchAnswerForm,
  t: TranslationDictionary,
): string {
  if (form === 'table') return t.agent.timeline.gateSearchFormTable
  if (form === 'timeline') return t.agent.timeline.gateSearchFormTimeline
  return t.agent.timeline.gateSearchFormOverview
}

/**
 * The form a web search names, as the tool reads it: exactly one of the
 * three names, and the default where the call names none (a missing or
 * null value). Any other value is no form (the tool refuses such a call).
 */
function namedSearchAnswerForm(value: unknown): SearchAnswerForm | null {
  if (value === undefined || value === null) return DEFAULT_SEARCH_ANSWER_FORM
  return SEARCH_ANSWER_FORMS.find((form) => form === value) ?? null
}

/**
 * What the card says about the form a web search is asked to answer in:
 * nothing for the default form, the requested form for a table or a
 * timeline, and for any other value that the tool refuses the call, with
 * the value as it was sent.
 */
function searchFormNote(value: unknown, t: TranslationDictionary): string {
  const form = namedSearchAnswerForm(value)
  if (form === DEFAULT_SEARCH_ANSWER_FORM) return ''
  if (form) {
    return t.agent.timeline.gateSearchFormNote.replace('{form}', () =>
      searchAnswerFormLabel(form, t),
    )
  }
  const named = typeof value === 'string' ? value : JSON.stringify(value)
  return t.agent.timeline.gateSearchFormRefused.replace('{form}', () => named)
}

/** Whether editing this action offers the form of the search answer as a choice. */
export function choosesSearchAnswerForm(
  action: Record<string, unknown> | null,
): boolean {
  return String(action?.tool ?? '') === 'web_instant'
}

function actionArgs(
  action: Record<string, unknown> | null,
): Record<string, unknown> {
  return (action?.args as Record<string, unknown> | undefined) ?? {}
}

/**
 * The arguments an edit offers as text fields: the ones that are text,
 * without the form of a web search, which is a choice.
 */
export function editableTextArgs(
  action: Record<string, unknown> | null,
): string[] {
  const args = actionArgs(action)
  return Object.keys(args).filter(
    (key) =>
      typeof args[key] === 'string'
      && !(choosesSearchAnswerForm(action) && key === ANSWER_FORM_ARG),
  )
}

/**
 * What the edit form starts from: every text argument as it was sent, and
 * for a web search the form it names (the default where it names none or
 * names no form).
 */
export function initialEditDraft(
  action: Record<string, unknown> | null,
): Record<string, string> {
  const args = actionArgs(action)
  const draft = Object.fromEntries(
    editableTextArgs(action).map((key) => [key, String(args[key] ?? '')]),
  )
  if (choosesSearchAnswerForm(action)) {
    draft[ANSWER_FORM_ARG] =
      namedSearchAnswerForm(args[ANSWER_FORM_ARG]) ?? DEFAULT_SEARCH_ANSWER_FORM
  }
  return draft
}

/**
 * The arguments an edit sends: the text fields as typed, everything else
 * as it was. The form of a web search goes as chosen; the default form is
 * left out, so the edited call is the one a search without a form makes.
 */
export function editedToolArgs(
  action: Record<string, unknown> | null,
  draft: Record<string, string>,
): Record<string, unknown> {
  const args: Record<string, unknown> = { ...actionArgs(action) }
  for (const key of editableTextArgs(action)) {
    if (key in draft) args[key] = draft[key]
  }
  if (choosesSearchAnswerForm(action) && ANSWER_FORM_ARG in draft) {
    if (draft[ANSWER_FORM_ARG] === DEFAULT_SEARCH_ANSWER_FORM) delete args[ANSWER_FORM_ARG]
    else args[ANSWER_FORM_ARG] = draft[ANSWER_FORM_ARG]
  }
  return args
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
    // Each key is an argument of its one tool; on another tool it is
    // none, and the card says nothing about it.
    note:
      tool === 'run_web_research'
        ? recencyNote(args?.recency, true, t)
        : tool === 'web_instant'
          ? searchFormNote(args?.[ANSWER_FORM_ARG], t)
          : '',
    items: assignments.map((assignment) => {
      const objective = String(assignment.objective ?? '')
      const mode = assignment.mode ? ` (${String(assignment.mode)})` : ''
      const window = recencyNote(
        assignment.recency,
        String(assignment.mode ?? '').trim() === 'research',
        t,
      )
      return `${objective}${mode}${window ? ` \u00b7 ${window}` : ''}`
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
    default:
      // A tool with a name of its own is named by it (a stored run's
      // retired delegation tool, too); an unknown one by its raw id.
      return t.agent.timeline.gateGenericTitle.replace(
        '{tool}',
        () => kernelToolLabel(tool, t),
      )
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

