import { useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { ChevronDown, ExternalLink, PenLine, Search } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Chip } from '@/components/ui/chip'
import { Input } from '@/components/ui/input'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { cn } from '@/lib/utils'
import type { TranslationDictionary } from '@/i18n/translations'
import {
  answersRequestFromDraft,
  isRoundComplete,
  setFreeText,
  toggleOption,
  type RoundAnswerDraft,
} from './clarificationAnswers'
import type { AgentClarificationAnswerRequest } from './types'
import {
  canEditAgentRun,
  isActiveAgentRun,
  isGateAgentRun,
  type AgentApprovalRecord,
  type AgentClarificationRecord,
  type AgentRunRecord,
} from './model'
import type { ResearchRunStatus } from '@/features/researchRuns/types'
import { usePatchReview } from './patch/usePatchReview'
import { usePlanApproval } from './plan/usePlanApproval'
import type { AgentTimelineActions } from './timeline/AgentTimeline'
import { discoveryProbeDisplay, kernelToolLabel } from './activityPresentation'
import { canGrantForRun, fullDelegationText, gateActionRow, gateKnowledgeScope, toolGateExplanation, toolGateHeadline, replanGateSummary } from './gateSummary'

/**
 * The ONE gate surface: every pending human decision of the
 * active run — clarification, plan/replan approval, discovery approval,
 * patch review — grows out of the composer as a tray, is answered at
 * the input locus, and afterwards appears in the transcript as a
 * user-side decision entry (the events append the markers). At most one
 * gate is pending at a time (runs are sequentially gated).
 */
export function ComposerGateTray({
  actions,
  run,
}: {
  actions: AgentTimelineActions
  run: AgentRunRecord | undefined
}) {
  const { t } = useLocale()
  const reduceMotion = Boolean(useReducedMotion())
  const gate = run ? pendingGate(run) : null
  const canEdit = canEditAgentRun(run)
  return (
    <AnimatePresence initial={false}>
      {run && gate && (
        <motion.div
          animate={{ height: 'auto', opacity: 1 }}
          className="overflow-hidden"
          exit={reduceMotion ? undefined : { height: 0, opacity: 0 }}
          initial={reduceMotion ? false : { height: 0, opacity: 0 }}
          transition={appMotionSafe(reduceMotion)}
        >
          <div
            className={cn(
              'mx-auto mb-1.5 max-w-5xl rounded-xl border px-3 py-2.5 shadow-[0_1px_2px_var(--shadow-hairline)]',
              gate.kind === 'clarification'
                ? 'border-warning/30 bg-warning-subtle/40'
                : 'border-brand/25 bg-brand-subtle/40',
            )}
          >
            {gate.kind === 'clarification' && (
              <ClarificationGate
                canEdit={canEdit}
                clarification={gate.clarification}
                onAnswer={(answer) =>
                  actions.answerClarification(
                    run.runId,
                    gate.clarification.clarificationId,
                    answer,
                  )}
                t={t}
              />
            )}
            {gate.kind === 'plan' && (
              <PlanGate actions={actions} canEdit={canEdit} run={run} t={t} />
            )}
            {gate.kind === 'discovery' && (
              <DiscoveryGate
                approval={gate.approval}
                actions={actions}
                canEdit={canEdit}
                run={run}
                t={t}
              />
            )}
            {gate.kind === 'patch' && (
              <PatchGate actions={actions} canEdit={canEdit} run={run} t={t} />
            )}
            {gate.kind === 'tool' && (
              <ToolGate
                approval={gate.approval}
                actions={actions}
                canEdit={canEdit}
                run={run}
                t={t}
              />
            )}
            {gate.kind === 'child_clarification' && (
              <div>
                <ChildGateContext label={gate.childLabel} t={t} />
                <ClarificationGate
                  canEdit={canEdit}
                  clarification={gate.clarification}
                  onAnswer={(answer) =>
                    actions.answerChildClarification(
                      run.runId,
                      gate.childRunId,
                      gate.clarification.clarificationId,
                      answer,
                    )}
                  t={t}
                />
              </div>
            )}
            {gate.kind === 'child_approval' && (
              <ChildApprovalGate
                approval={gate.approval}
                canEdit={canEdit}
                childLabel={gate.childLabel}
                onDecide={(decision) =>
                  actions.decideChildApproval(
                    run.runId,
                    gate.childRunId,
                    gate.approval.approvalId,
                    { decision },
                  )}
                t={t}
              />
            )}
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  )
}

type PendingGate =
  | { kind: 'clarification'; clarification: AgentClarificationRecord }
  | { kind: 'plan'; approval: AgentApprovalRecord }
  | { kind: 'discovery'; approval: AgentApprovalRecord }
  | { kind: 'patch'; approval: AgentApprovalRecord }
  | { kind: 'tool'; approval: AgentApprovalRecord }
  | {
    kind: 'child_clarification'
    clarification: AgentClarificationRecord
    childRunId: string
    childLabel: string
  }
  | {
    kind: 'child_approval'
    approval: AgentApprovalRecord
    childRunId: string
    childLabel: string
  }

/** The active run's single pending gate; decisions on settled runs 409.
 * The run's own rows come first; a delegated child parked on a human
 * decision surfaces after them — gated on the child's CURRENT status,
 * so a stale pending row can never re-open a gate the child has already
 * left. */
export function pendingGate(run: AgentRunRecord): PendingGate | null {
  if (!isActiveAgentRun(run.status)) return null
  const clarification = run.clarifications.find(
    (item) => item.status === 'pending',
  )
  if (clarification) return { clarification, kind: 'clarification' }
  const approval = run.approvals.find((item) => item.status === 'pending')
  if (approval) {
    if (approval.kind === 'discovery') return { approval, kind: 'discovery' }
    if (approval.kind === 'patch') return { approval, kind: 'patch' }
    if (approval.kind === 'tool') return { approval, kind: 'tool' }
    return { approval, kind: 'plan' }
  }
  for (const child of Object.values(run.children)) {
    if (!child.runStatus || !isGateAgentRun(child.runStatus as ResearchRunStatus)) continue
    const gates = run.childGates[child.childRunId]
    if (!gates) continue
    const childLabel = childGateLabel(run, child)
    const childClarification = gates.clarifications.find(
      (item) => item.status === 'pending',
    )
    if (childClarification) {
      return {
        childLabel,
        childRunId: child.childRunId,
        clarification: childClarification,
        kind: 'child_clarification',
      }
    }
    const childApproval = gates.approvals.find(
      (item) => item.status === 'pending',
    )
    if (childApproval) {
      return {
        approval: childApproval,
        childLabel,
        childRunId: child.childRunId,
        kind: 'child_approval',
      }
    }
  }
  return null
}

/** What the parked child is working ON, from the best source the parent
 * holds: the plan task it fulfils, else the delegation's tool row in the
 * transcript (its args preview carries the delegated question), else the
 * child's last progress message. */
/**
 * Whether the edit control should appear DISABLED with its reason.
 *
 * A gate proposing several actions cannot be edited: the HITL resume
 * contract carries one action per decision, and swapping a tool would
 * grant something the gate never showed the user
 * (``_validated_tool_edit``). An absent control would read as an
 * oversight rather than a rule.
 *
 * Only this reason earns a disabled control. Missing permission or
 * nothing string-valued to edit are different situations and keep
 * hiding it — a disabled button there would promise a capability the
 * reader does not have at all.
 */
export function toolEditIsBlockedByMultiAction(
  canEdit: boolean,
  proposedActions: number,
): boolean {
  return canEdit && proposedActions > 1
}

function childGateLabel(
  run: AgentRunRecord,
  child: AgentRunRecord['children'][string],
): string {
  // The fetched run-row question is the primary source: the
  // transcript detail is a truncated args preview, and approving on a
  // cut-off sentence is approving blind. The question COLUMN itself
  // bounds at 500 — the delegation approval's verbatim args recover
  // the full assignment behind a visible clip.
  const question = run.childGates[child.childRunId]?.question
  if (question) {
    return fullDelegationText(run.approvals, question) ?? question
  }
  const planTask = run.plan?.tasks.find(
    (task) => task.childRunId === child.childRunId,
  )
  if (planTask) return planTask.title || planTask.objective
  const toolCallId = child.taskId.split(':')[0]
  const toolRow = toolCallId
    ? run.stepLog.find((entry) => entry.activityKey === `tool:${toolCallId}`)
    : undefined
  if (toolRow?.detail) return toolRow.detail
  return child.message || child.childRunId
}

/** The assumption a run continues with when its question stays open — one
 * line for every clarification form, the model's text shown as written. */
function DefaultAssumptionLine({ assumption, t }: { assumption: string; t: TranslationDictionary }) {
  if (!assumption) return null
  return (
    <p className="mt-1.5 t-hint text-muted-foreground">
      {/* A function replacer: model text may hold `$&`, which a string
          replacement would read as a pattern. */}
      {t.agent.timeline.defaultAssumption.replace('{assumption}', () => assumption)}
    </p>
  )
}

function ClarificationGate({
  canEdit,
  clarification,
  onAnswer,
  t,
}: {
  canEdit: boolean
  clarification: AgentClarificationRecord
  onAnswer: (answer: AgentClarificationAnswerRequest) => Promise<unknown>
  t: TranslationDictionary
}) {
  const [submitting, setSubmitting] = useState(false)
  const choose = (optionId: string) => {
    if (!canEdit || submitting) return
    setSubmitting(true)
    void onAnswer({ option_id: optionId }).finally(() => setSubmitting(false))
  }
  if (clarification.questions.length > 0) {
    return (
      <StructuredClarificationForm
        canEdit={canEdit}
        clarification={clarification}
        onAnswer={onAnswer}
        t={t}
      />
    )
  }
  return (
    <div>
      <GateTitle pulse title={t.agent.timeline.waitingInputTitle} tone="warning" />
      <p className="mt-1 break-words t-body text-foreground">
        {clarification.question}
      </p>
      {clarification.options.length > 0 && (
        <div className="mt-2 flex flex-wrap items-center gap-1.5">
          {clarification.options.map((option) => (
            <Button
              className="h-6 rounded-full border-border bg-background px-2.5 text-xs"
              disabled={!canEdit || submitting}
              key={option.id}
              onClick={() => choose(option.id)}
              size="sm"
              type="button"
              variant="outline"
            >
              {option.label}
            </Button>
          ))}
          {canEdit && (
            <span className="t-hint text-muted-foreground">
              {t.agent.tray.freeTextHint}
            </span>
          )}
        </div>
      )}
      {canEdit && clarification.options.length === 0 && (
        <p className="mt-1 t-hint text-muted-foreground">
          {t.agent.tray.freeTextHint}
        </p>
      )}
      <DefaultAssumptionLine assumption={clarification.defaultAssumption} t={t} />
    </div>
  )
}

/** One quiet context line above a child gate: which delegation is asking. */
function ChildGateContext({
  label,
  t,
}: {
  label: string
  t: TranslationDictionary
}) {
  return (
    <p className="mb-1 break-words t-hint text-muted-foreground">
      {t.agent.tray.childGateContext.replace('{label}', label)}
    </p>
  )
}

/**
 * Approve/reject for a gate raised INSIDE a delegated child run. Kept
 * deliberately compact: the child's own plan is not fetched here — the
 * delegation objective plus the proposed actions are the decision
 * context. Argument editing stays a root-gate affordance.
 */
function ChildApprovalGate({
  approval,
  canEdit,
  childLabel,
  onDecide,
  t,
}: {
  approval: AgentApprovalRecord
  canEdit: boolean
  childLabel: string
  onDecide: (decision: 'approve' | 'reject') => Promise<unknown>
  t: TranslationDictionary
}) {
  const [submitting, setSubmitting] = useState(false)
  const proposed = Array.isArray(approval.payload.actions)
    ? (approval.payload.actions as Record<string, unknown>[])
    : []
  const probes = Array.isArray(approval.payload.probes)
    ? (approval.payload.probes as Record<string, unknown>[])
    : []
  // A replan says why the plan grew and what it adds.
  const replan = replanGateSummary(approval.payload, t)
  const decide = (decision: 'approve' | 'reject') => {
    if (!canEdit || submitting) return
    setSubmitting(true)
    void onDecide(decision).finally(() => setSubmitting(false))
  }
  return (
    <div>
      <GateTitle pulse title={t.agent.tray.childApprovalTitle} tone="warning" />
      <ChildGateContext label={childLabel} t={t} />
      <p className="t-meta text-muted-foreground">
        {approval.kind === 'tool'
          ? t.agent.tray.childToolHint
          : approval.kind === 'discovery'
            ? t.agent.tray.childDiscoveryHint
            : approval.kind === 'replan'
              ? replan.hint
              : t.agent.tray.childPlanHint}
      </p>
      {approval.kind === 'replan' && replan.rows.length > 0 && (
        <div className="mt-1.5">
          <p className="t-meta text-muted-foreground">{replan.heading}</p>
          <ul className="mt-0.5 space-y-0.5">
            {replan.rows.map((row, index) => (
              <li
                className="flex items-start gap-1.5 t-meta text-foreground/85"
                key={index}
              >
                <Search className="mt-0.5 icon-xs shrink-0 text-muted-foreground" />
                <span className="min-w-0 break-words">{row}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {proposed.length > 0 && (
        <ul className="mt-1.5 space-y-1">
          {proposed.map((action, index) => {
            const row = gateActionRow(action, t)
            return (
              <li
                className="flex items-start gap-1.5 t-meta text-foreground/85"
                key={index}
              >
                <Search className="mt-0.5 icon-xs shrink-0 text-muted-foreground" />
                <span className="min-w-0 break-words">
                  <span className="font-medium">{row.label}</span>
                  {row.text && (
                    <span className="mt-0.5 block whitespace-pre-wrap">
                      {row.text}
                    </span>
                  )}
                </span>
              </li>
            )
          })}
        </ul>
      )}
      {probes.length > 0 && (
        <ul className="mt-1.5 space-y-0.5">
          {probes.map((probe, index) => {
            const display = discoveryProbeDisplay(probe, t)
            return (
              <li
                className="flex items-start gap-1.5 t-meta text-foreground/85"
                key={index}
              >
                <Search className="mt-0.5 icon-xs shrink-0 text-muted-foreground" />
                <span className="min-w-0 break-words">
                  <span className="font-medium">{display.title}</span>
                  {display.detail ? ` · ${display.detail}` : ''}
                </span>
              </li>
            )
          })}
        </ul>
      )}
      <div className="mt-2 flex items-center gap-1.5">
        <Button
          className="h-7 bg-brand px-2.5 text-xs text-brand-foreground hover:bg-brand/90"
          disabled={!canEdit || submitting}
          onClick={() => decide('approve')}
          size="sm"
          type="button"
        >
          {t.agent.timeline.approve}
        </Button>
        <Button
          className="h-7 px-2.5 text-xs"
          disabled={!canEdit || submitting}
          onClick={() => decide('reject')}
          size="sm"
          type="button"
          variant="outline"
        >
          {t.agent.timeline.reject}
        </Button>
      </div>
    </div>
  )
}

/** The structured round form: one chips row per question, per-question
 * "Sonstiges" free text, ONE submit for the whole round (the server
 * rejects partial answers — the round parks the run exactly once). */
export function StructuredClarificationForm({
  canEdit,
  clarification,
  onAnswer,
  t,
}: {
  canEdit: boolean
  clarification: AgentClarificationRecord
  onAnswer: (answer: AgentClarificationAnswerRequest) => Promise<unknown>
  t: TranslationDictionary
}) {
  const [submitting, setSubmitting] = useState(false)
  const [draft, setDraft] = useState<RoundAnswerDraft>({})
  const [otherOpen, setOtherOpen] = useState<Record<string, boolean>>({})
  const questions = clarification.questions
  const complete = isRoundComplete(questions, draft)
  const submit = () => {
    if (!canEdit || submitting || !complete) return
    setSubmitting(true)
    void onAnswer({
      answers: answersRequestFromDraft(questions, draft),
    }).finally(() => setSubmitting(false))
  }
  return (
    <div>
      <GateTitle pulse title={t.agent.timeline.waitingInputTitle} tone="warning" />
      <div className="mt-1.5 space-y-2.5">
        {questions.map((question) => {
          const entry = draft[question.id]
          const showOther
            = Boolean(otherOpen[question.id]) || question.options.length === 0
          return (
            <div key={question.id}>
              <p className="break-words t-body text-foreground">
                {question.prompt}
                {question.multiSelect && (
                  <span className="ml-1.5 t-hint text-muted-foreground">
                    {t.agent.tray.clarifyMultiHint}
                  </span>
                )}
              </p>
              <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                {question.options.map((option) => (
                  // The chosen option's wording is the user's answer, so a
                  // long option wraps and stays readable in full where it
                  // is picked (no hover on touch devices).
                  <Chip
                    active={Boolean(entry?.optionIds.includes(option.id))}
                    className="h-auto min-h-6 max-w-full whitespace-normal break-words rounded-xl py-0.5 text-left"
                    disabled={!canEdit || submitting}
                    key={option.id}
                    onClick={() =>
                      setDraft((prev) => toggleOption(prev, question, option.id))}
                    title={option.description || undefined}
                  >
                    {option.label}
                  </Chip>
                ))}
                {question.options.length > 0 && (
                  <Chip
                    active={showOther}
                    disabled={!canEdit || submitting}
                    onClick={() =>
                      setOtherOpen((prev) => ({
                        ...prev,
                        [question.id]: !prev[question.id],
                      }))}
                  >
                    {t.agent.tray.clarifyOther}
                  </Chip>
                )}
              </div>
              {showOther && (
                <Input
                  className="mt-1.5 h-8 max-w-md"
                  disabled={!canEdit || submitting}
                  onChange={(event) =>
                    setDraft((prev) =>
                      setFreeText(prev, question.id, event.target.value))}
                  onKeyDown={(event) => {
                    if (event.key === 'Enter') {
                      event.preventDefault()
                      submit()
                    }
                  }}
                  placeholder={t.agent.tray.clarifyOtherPlaceholder}
                  value={entry?.text ?? ''}
                />
              )}
            </div>
          )
        })}
      </div>
      <div className="mt-2.5 flex items-center gap-2">
        <Button
          disabled={!canEdit || submitting || !complete}
          onClick={submit}
          size="sm"
          type="button"
        >
          {t.agent.tray.clarifySubmit}
        </Button>
        {canEdit && (
          <span className="t-hint text-muted-foreground">
            {t.agent.tray.freeTextHint}
          </span>
        )}
      </div>
      <DefaultAssumptionLine assumption={clarification.defaultAssumption} t={t} />
    </div>
  )
}

function PlanGate({
  actions,
  canEdit,
  run,
  t,
}: {
  actions: AgentTimelineActions
  canEdit: boolean
  run: AgentRunRecord
  t: TranslationDictionary
}) {
  const [queriesExpanded, setQueriesExpanded] = useState(false)
  // THE one decision lane (shared with the canvas plan tab): approving
  // with a dirty draft becomes `decision:'edit'` carrying the edits —
  // a raw decideApproval here would silently DROP changes made in the
  // plan tab before the user pressed Freigeben in the tray.
  const planApproval = usePlanApproval({
    decideApproval: actions.decideApproval,
    draft: actions.planDrafts[run.runId] ?? null,
    onDraftChange: (draft) => actions.setPlanDraft(run.runId, draft),
    run,
  })
  const decide = (decision: 'approve' | 'reject') => {
    if (!canEdit) return
    void planApproval.decide(decision)
  }
  const submitting = planApproval.submitting
  const rejectPending = planApproval.draft?.rejectPending ?? false
  const tasks = run.plan?.tasks ?? []
  const counts = {
    internal: tasks.filter(
      (task) =>
        task.toolKind === 'rag_query' || task.toolKind === 'file_analysis',
    ).length,
    synthesis: tasks.filter((task) => task.toolKind === 'synthesis').length,
    web: tasks.filter(
      (task) =>
        task.toolKind === 'web_research' || task.toolKind === 'web_instant',
    ).length,
  }
  // Concrete numbers against rubber-stamping: the user sees WHAT they
  // approve before the primary action even exists.
  const summary = t.agent.tray.planSummary
    .replace('{version}', String(run.plan?.version ?? 1))
    .replace('{tasks}', countLabel(tasks.length, t.agent.timeline.taskCountOne, t.agent.timeline.taskCount))
    .replace('{internal}', String(counts.internal))
    .replace('{web}', String(counts.web))
    .replace('{synthesis}', String(counts.synthesis))
  // Informed web consent: the approved plan
  // is the ONLY surface where Standard mode consents to web searches —
  // the concrete queries therefore show INLINE, not just behind "Plan
  // ansehen".
  const webQueries = tasks
    .filter(
      (task) =>
        task.toolKind === 'web_research' || task.toolKind === 'web_instant',
    )
    .flatMap((task) => task.queries)
  const shownQueries = queriesExpanded ? webQueries : webQueries.slice(0, 1)
  return (
    <div>
      <GateTitle pulse title={t.agent.tray.planTitle} tone="brand" />
      <p className="mt-1 t-meta tabular-nums text-foreground/90">{summary}</p>
      {shownQueries.length > 0 && (
        <div className="mt-1.5 space-y-0.5">
          {shownQueries.map((query) => (
            <p
              className="flex min-w-0 items-center gap-1.5 t-meta text-foreground/85"
              key={query}
            >
              <Search className="icon-xs shrink-0 text-muted-foreground" />
              <span className={cn('min-w-0 break-words', !queriesExpanded && 'line-clamp-2')}>
                {query}
              </span>
            </p>
          ))}
          {webQueries.length > 0 && (
            <button
              aria-expanded={queriesExpanded}
              className="flex items-center gap-1 t-hint font-medium text-muted-foreground transition-colors hover:text-foreground"
              onClick={() => setQueriesExpanded((current) => !current)}
              type="button"
            >
              {queriesExpanded
                ? t.agent.tray.fewerQueries
                : countLabel(
                  webQueries.length,
                  t.agent.tray.expandQueriesOne,
                  t.agent.tray.expandQueries,
                )}
              <ChevronDown
                className={cn(
                  'icon-xs transition-transform',
                  queriesExpanded && 'rotate-180',
                )}
              />
            </button>
          )}
        </div>
      )}
      {planApproval.error && (
        <p className="mt-1 break-words t-meta-sm text-destructive">
          {planApproval.error}
        </p>
      )}
      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        <Button
          className="h-7 gap-1 bg-brand px-2.5 text-xs text-brand-foreground hover:bg-brand/90"
          onClick={() =>
            actions.onOpenCanvas({ runId: run.runId, view: 'plan' })}
          size="sm"
          type="button"
        >
          <ExternalLink className="size-3" />
          {t.agent.tray.viewPlan}
        </Button>
        {/* One decision at a time: while a rejection note is being
            written in the plan tab, this bar must not offer a plain
            approve — the click would land, the note would be gone, and
            nothing would say so. The intent lives in the shared draft,
            so both surfaces see it. */}
        {!rejectPending && (
          <>
            <Button
              className="h-7 px-2.5 text-xs"
              disabled={!canEdit || submitting}
              onClick={() => decide('approve')}
              size="sm"
              type="button"
              variant="outline"
            >
              {t.agent.timeline.approve}
            </Button>
            <Button
              className="h-7 px-2.5 text-xs text-muted-foreground hover:text-destructive"
              disabled={!canEdit || submitting}
              onClick={() => decide('reject')}
              size="sm"
              type="button"
              variant="ghost"
            >
              {t.agent.timeline.reject}
            </Button>
          </>
        )}
        {canEdit && (
          <span className="t-hint text-muted-foreground">
            {rejectPending
              ? t.agent.tray.rejectPendingHint
              : t.agent.tray.editHint}
          </span>
        )}
      </div>
    </div>
  )
}

function DiscoveryGate({
  actions,
  approval,
  canEdit,
  run,
  t,
}: {
  actions: AgentTimelineActions
  approval: AgentApprovalRecord
  canEdit: boolean
  run: AgentRunRecord
  t: TranslationDictionary
}) {
  const [submitting, setSubmitting] = useState(false)
  const probes = Array.isArray(approval.payload.probes)
    ? (approval.payload.probes as Record<string, unknown>[])
    : []
  const decide = (decision: 'approve' | 'reject') => {
    if (!canEdit || submitting) return
    setSubmitting(true)
    void actions
      .decideApproval(run.runId, approval.approvalId, { decision })
      .finally(() => setSubmitting(false))
  }
  return (
    <div>
      <GateTitle pulse title={t.agent.timeline.waitingApprovalTitle} tone="warning" />
      <p className="mt-1 t-meta text-muted-foreground">
        {t.agent.timeline.discoveryApprovalHint}
      </p>
      <ul className="mt-1.5 space-y-0.5">
        {probes.map((probe, index) => (
          <DiscoveryProbeRow key={index} probe={probe} t={t} />
        ))}
      </ul>
      <div className="mt-2 flex items-center gap-1.5">
        <Button
          className="h-7 bg-brand px-2.5 text-xs text-brand-foreground hover:bg-brand/90"
          disabled={!canEdit || submitting}
          onClick={() => decide('approve')}
          size="sm"
          type="button"
        >
          {t.agent.timeline.approve}
        </Button>
        <Button
          className="h-7 px-2.5 text-xs"
          disabled={!canEdit || submitting}
          onClick={() => decide('reject')}
          size="sm"
          type="button"
          variant="outline"
        >
          {t.agent.timeline.reject}
        </Button>
      </div>
    </div>
  )
}

/** The kernel's per-tool consent gate (Standard keeps web contact
 * behind an approval). Rendered as a plan gate before, whose approve
 * button silently did nothing — usePlanApproval only decides
 * plan/replan approvals. */
function ToolGate({
  actions,
  approval,
  canEdit,
  run,
  t,
}: {
  actions: AgentTimelineActions
  approval: AgentApprovalRecord
  canEdit: boolean
  run: AgentRunRecord
  t: TranslationDictionary
}) {
  const [submitting, setSubmitting] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState<Record<string, string>>({})
  const proposed = Array.isArray(approval.payload.actions)
    ? (approval.payload.actions as Record<string, unknown>[])
    : []
  const knowledgeScope = gateKnowledgeScope(approval.payload)
  // Editable only when the gate proposes exactly ONE action (backend
  // constraint: _validated_tool_edit rejects multi-action edits and cannot
  // swap the tool); v1 edits string-valued args — the web query is the case
  // that matters — and passes non-string args through unchanged.
  const single = proposed.length === 1 ? proposed[0] : null
  const singleArgs =
    (single?.args as Record<string, unknown> | undefined) ?? {}
  const editableKeys = Object.keys(singleArgs).filter(
    (key) => typeof singleArgs[key] === 'string',
  )
  const canEditArgs = Boolean(single) && canEdit && editableKeys.length > 0
  // A gate proposing SEVERAL actions cannot be edited — the HITL resume
  // contract carries one action per decision, and swapping a tool would
  // grant something the gate never showed. An absent button would read
  // as an oversight rather than a rule, so it appears disabled and says
  // why; the other reasons for hiding it
  // (no permission, nothing string-valued to edit) are different
  // situations and keep hiding it.
  const canGrantRun = canGrantForRun(run.autonomy, proposed)

  const decide = (decision: 'approve' | 'reject', scope?: 'run') => {
    if (!canEdit || submitting) return
    setSubmitting(true)
    void actions
      .decideApproval(
        run.runId,
        approval.approvalId,
        scope ? { decision, approval_scope: scope } : { decision },
      )
      .finally(() => setSubmitting(false))
  }
  const submitEdit = () => {
    if (!single || !canEdit || submitting) return
    setSubmitting(true)
    const args: Record<string, unknown> = { ...singleArgs }
    for (const key of editableKeys) {
      if (key in draft) args[key] = draft[key]
    }
    void actions
      .decideApproval(run.runId, approval.approvalId, {
        decision: 'edit',
        actions: [{ tool: String(single.tool ?? ''), args }],
      })
      .finally(() => setSubmitting(false))
  }
  const startEditing = () => {
    setDraft(
      Object.fromEntries(
        editableKeys.map((key) => [key, String(singleArgs[key] ?? '')]),
      ),
    )
    setEditing(true)
  }
  return (
    <div>
      <GateTitle pulse title={toolGateHeadline(proposed, t)} tone="warning" />
      <p className="mt-1 t-meta text-muted-foreground">
        {toolGateExplanation(proposed, t)}
      </p>
      {knowledgeScope.length > 0 && (
        <p className="mt-1 t-meta text-muted-foreground">
          <span className="font-medium text-foreground/85">
            {t.agent.timeline.gateScopeLabel}
          </span>
          {' '}
          {knowledgeScope.join(' \u00b7 ')}
        </p>
      )}
      {editing && single ? (
        <div className="mt-1.5 space-y-1.5">
          <p className="t-meta font-medium text-foreground/85">
            {kernelToolLabel(String(single.tool ?? ''), t)}
          </p>
          {editableKeys.map((key) => (
            <label className="block" key={key}>
              <span className="t-hint text-muted-foreground">{key}</span>
              <Input
                className="mt-0.5 h-7 text-xs"
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    [key]: event.target.value,
                  }))}
                value={draft[key] ?? ''}
              />
            </label>
          ))}
        </div>
      ) : (
        <ul className="mt-1.5 space-y-1">
          {proposed.map((action, index) => {
            // The row IS the approval content: the payload args are the
            // verbatim full text, and nothing here may truncate (approving a
            // cut-off sentence is approving blind).
            const row = gateActionRow(action, t)
            return (
              <li
                className="flex items-start gap-1.5 t-meta text-foreground/85"
                key={index}
              >
                <Search className="mt-0.5 icon-xs shrink-0 text-muted-foreground" />
                <span className="min-w-0 break-words">
                  <span className="font-medium">{row.label}</span>
                  {row.text && (
                    <span className="mt-0.5 block whitespace-pre-wrap">
                      {row.text}
                    </span>
                  )}
                  {row.items.length > 0 && (
                    <span className="mt-0.5 block space-y-0.5">
                      {row.items.map((item, itemIndex) => (
                        <span
                          className="block whitespace-pre-wrap text-muted-foreground"
                          key={itemIndex}
                        >
                          {itemIndex + 1}. {item}
                        </span>
                      ))}
                    </span>
                  )}
                </span>
              </li>
            )
          })}
        </ul>
      )}
      <div className="mt-2 flex items-center gap-1.5">
        {editing ? (
          <>
            <Button
              className="h-7 bg-brand px-2.5 text-xs text-brand-foreground hover:bg-brand/90"
              disabled={!canEdit || submitting}
              onClick={submitEdit}
              size="sm"
              type="button"
            >
              {t.agent.tray.toolEditSubmit}
            </Button>
            <Button
              className="h-7 px-2.5 text-xs"
              disabled={submitting}
              onClick={() => setEditing(false)}
              size="sm"
              type="button"
              variant="ghost"
            >
              {t.agent.tray.toolEditCancel}
            </Button>
          </>
        ) : (
          <>
            <Button
              className="h-7 bg-brand px-2.5 text-xs text-brand-foreground hover:bg-brand/90"
              disabled={!canEdit || submitting}
              onClick={() => decide('approve')}
              size="sm"
              type="button"
            >
              {t.agent.timeline.approve}
            </Button>
            {canGrantRun ? (
              <Button
                className="h-7 px-2.5 text-xs"
                disabled={!canEdit || submitting}
                onClick={() => decide('approve', 'run')}
                size="sm"
                title={t.agent.tray.toolApproveForRunHint}
                type="button"
                variant="outline"
              >
                {t.agent.tray.toolApproveForRun}
              </Button>
            ) : null}
            <Button
              className="h-7 px-2.5 text-xs"
              disabled={!canEdit || submitting}
              onClick={() => decide('reject')}
              size="sm"
              type="button"
              variant="outline"
            >
              {t.agent.timeline.reject}
            </Button>
            {canEditArgs ? (
              <Button
                className="h-7 px-2.5 text-xs"
                disabled={submitting}
                onClick={startEditing}
                size="sm"
                type="button"
                variant="ghost"
              >
                {t.agent.tray.toolEdit}
              </Button>
            ) : toolEditIsBlockedByMultiAction(canEdit, proposed.length) ? (
              <span
                className="inline-flex"
                title={t.agent.tray.toolEditMultiActionHint}
              >
                <Button
                  className="h-7 px-2.5 text-xs"
                  disabled
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  {t.agent.tray.toolEdit}
                </Button>
              </span>
            ) : null}
          </>
        )}
      </div>
    </div>
  )
}


function DiscoveryProbeRow({
  probe,
  t,
}: {
  probe: Record<string, unknown>
  t: TranslationDictionary
}) {
  const display = discoveryProbeDisplay(probe, t)
  return (
    <li className="flex items-start gap-1.5 t-meta text-foreground/85">
      <Search className="mt-0.5 icon-xs shrink-0 text-muted-foreground" />
      <span className="min-w-0 break-words">
        <span className="font-medium">{display.title}</span>
        {display.detail ? ` · ${display.detail}` : ''}
      </span>
    </li>
  )
}

function PatchGate({
  actions,
  canEdit,
  run,
  t,
}: {
  actions: AgentTimelineActions
  canEdit: boolean
  run: AgentRunRecord
  t: TranslationDictionary
}) {
  const review = usePatchReview({
    actions: {
      applyPatch: actions.applyPatch,
      decideApproval: actions.decideApproval,
      rejectPatch: actions.rejectPatch,
    },
    run,
  })
  const patch = run.patch
  if (!patch || !run.patchId) return null
  return (
    <div>
      <GateTitle pulse title={t.agent.patch.title} tone="brand" />
      <p className="mt-1 t-meta text-foreground/90">
        {countLabel(
          patch.edits.length,
          t.agent.patch.editCountOne,
          t.agent.patch.editCount,
        )}
        {patch.summary ? ` · ${patch.summary}` : ''}
      </p>
      {review.notice && (
        <p className="mt-1 t-meta-sm text-warning">{review.notice}</p>
      )}
      <div className="mt-2 flex items-center gap-1.5">
        <Button
          className="h-7 gap-1 bg-brand px-2.5 text-xs text-brand-foreground hover:bg-brand/90"
          onClick={() =>
            run.patchId
            && actions.onOpenCanvas({
              patchId: run.patchId,
              runId: run.runId,
              view: 'patch',
            })}
          size="sm"
          type="button"
        >
          <PenLine className="size-3" />
          {t.agent.tray.view}
        </Button>
        <Button
          className="h-7 px-2.5 text-xs"
          disabled={!canEdit || review.submitting}
          onClick={() => void review.approveAndApply()}
          size="sm"
          type="button"
          variant="outline"
        >
          {t.agent.patch.applyAndApprove}
        </Button>
        <Button
          className="h-7 px-2.5 text-xs text-muted-foreground hover:text-destructive"
          disabled={!canEdit || review.submitting}
          onClick={() => void review.reject()}
          size="sm"
          type="button"
          variant="ghost"
        >
          {t.agent.timeline.reject}
        </Button>
      </div>
    </div>
  )
}

function GateTitle({
  pulse,
  title,
  tone,
}: {
  pulse: boolean
  title: string
  tone: 'brand' | 'warning'
}) {
  const reduceMotion = Boolean(useReducedMotion())
  return (
    <div className="flex items-center gap-1.5">
      <span
        aria-hidden="true"
        className={cn(
          'size-1.5 rounded-full',
          tone === 'warning' ? 'bg-warning' : 'bg-brand',
          pulse && !reduceMotion && 'inqtrix-running-dot',
        )}
      />
      <span className="t-card text-foreground">{title}</span>
    </div>
  )
}

function appMotionSafe(reduceMotion: boolean) {
  return reduceMotion
    ? { duration: 0 }
    : { damping: 30, mass: 0.9, stiffness: 380, type: 'spring' as const }
}
