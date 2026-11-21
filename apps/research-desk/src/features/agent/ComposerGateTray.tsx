import { useState } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { Search } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Chip } from '@/components/ui/chip'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { useLocale } from '@/i18n/LocaleProvider'
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
import type { AgentTimelineActions } from './timeline/AgentTimeline'
import { kernelToolLabel } from './activityPresentation'
import {
  ANSWER_FORM_ARG,
  DEFAULT_SEARCH_ANSWER_FORM,
  SEARCH_ANSWER_FORMS,
  canGrantForRun,
  choosesSearchAnswerForm,
  editableTextArgs,
  editedToolArgs,
  fullDelegationText,
  gateActionRow,
  gateKnowledgeScope,
  initialEditDraft,
  searchAnswerFormLabel,
  toolGateExplanation,
  toolGateHeadline,
} from './gateSummary'

/**
 * The ONE gate surface: every pending human decision of the active run —
 * a clarification, a tool approval, and the same two of a delegated child —
 * grows out of the composer as a tray, is answered at the input locus, and
 * afterwards appears in the transcript as a user-side decision entry (the
 * events append the markers). At most one gate is pending at a time (runs
 * are sequentially gated).
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
 * left. Only a tool approval has a tray: an approval of another kind (a
 * parked run of the retired mission engine waiting for its plan) offers
 * nothing this desk could decide. */
export function pendingGate(run: AgentRunRecord): PendingGate | null {
  if (!isActiveAgentRun(run.status)) return null
  const clarification = run.clarifications.find(
    (item) => item.status === 'pending',
  )
  if (clarification) return { clarification, kind: 'clarification' }
  const approval = run.approvals.find(
    (item) => item.status === 'pending' && item.kind === 'tool',
  )
  if (approval) return { approval, kind: 'tool' }
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
      (item) => item.status === 'pending' && item.kind === 'tool',
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

/** What the parked child is working ON, from the best source the parent
 * holds: the child's full question, else the delegation's tool row in the
 * transcript (its args preview carries the delegated question), else the
 * child's last progress message. */
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
      {t.agent.tray.childGateContext.replace('{label}', () => label)}
    </p>
  )
}

/**
 * Approve/reject for a tool gate raised INSIDE a delegated child run. Kept
 * deliberately compact: the delegation objective plus the proposed actions
 * are the decision context. Argument editing stays a root-gate affordance.
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
        {t.agent.tray.childToolHint}
      </p>
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
                  {row.note && (
                    <span className="mt-0.5 block text-muted-foreground">
                      {row.note}
                    </span>
                  )}
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

/** The kernel's per-tool consent gate (Standard keeps web contact
 * behind an approval). */
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
  // that matters — and passes non-string args through unchanged. The form
  // of a web search answer is a choice among its names, never free text,
  // and is offered also where the call names none.
  const single = proposed.length === 1 ? proposed[0] : null
  const editableKeys = editableTextArgs(single)
  const choosesForm = choosesSearchAnswerForm(single)
  const canEditArgs =
    Boolean(single) && canEdit && (editableKeys.length > 0 || choosesForm)
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
    void actions
      .decideApproval(run.runId, approval.approvalId, {
        decision: 'edit',
        actions: [
          { tool: String(single.tool ?? ''), args: editedToolArgs(single, draft) },
        ],
      })
      .finally(() => setSubmitting(false))
  }
  const startEditing = () => {
    setDraft(initialEditDraft(single))
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
        <ToolEditFields
          action={single}
          draft={draft}
          onChange={(key, value) =>
            setDraft((current) => ({ ...current, [key]: value }))}
          t={t}
        />
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
                  {row.note && (
                    <span className="mt-0.5 block text-muted-foreground">
                      {row.note}
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


/**
 * The fields of an edited tool call: every text argument as a text field,
 * and for a web search the form of its answer as a choice among the names
 * the search takes, never as free text.
 */
export function ToolEditFields({
  action,
  draft,
  onChange,
  t,
}: {
  action: Record<string, unknown>
  draft: Record<string, string>
  onChange: (key: string, value: string) => void
  t: TranslationDictionary
}) {
  return (
    <div className="mt-1.5 space-y-1.5">
      <p className="t-meta font-medium text-foreground/85">
        {kernelToolLabel(String(action.tool ?? ''), t)}
      </p>
      {editableTextArgs(action).map((key) => (
        <label className="block" key={key}>
          <span className="t-hint text-muted-foreground">{key}</span>
          <Input
            className="mt-0.5 h-7 text-xs"
            onChange={(event) => onChange(key, event.target.value)}
            value={draft[key] ?? ''}
          />
        </label>
      ))}
      {choosesSearchAnswerForm(action) && (
        <label className="block">
          <span className="t-hint text-muted-foreground">
            {t.agent.timeline.gateSearchFormLabel}
          </span>
          <Select
            onValueChange={(value) => onChange(ANSWER_FORM_ARG, value)}
            value={draft[ANSWER_FORM_ARG] ?? DEFAULT_SEARCH_ANSWER_FORM}
          >
            <SelectTrigger
              aria-label={t.agent.timeline.gateSearchFormLabel}
              className="mt-0.5"
              density="table"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {SEARCH_ANSWER_FORMS.map((form) => (
                <SelectItem key={form} value={form}>
                  {searchAnswerFormLabel(form, t)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </label>
      )}
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
