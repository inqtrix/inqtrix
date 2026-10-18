import { AgentDisclosure } from '../AgentDisclosure'
import { useEffect, useMemo, useRef, useState } from 'react'
import type { MouseEvent } from 'react'
import { motion, useReducedMotion } from 'motion/react'
import {
  AlertTriangle,
  BookOpen,
  BookSearch,
  Check,
  ChevronRight,
  Copy,
  ExternalLink,
  FileText,
  Globe2,
  Info,
  ListChecks,
  MessageSquareText,
  PenLine,
  Search,
  X,
} from '@/components/icons'
import { Button } from '@/components/ui/button'
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer'
import { MarkdownSelectionCopyMenu } from '@/components/markdown/MarkdownSelectionCopyMenu'
import { effortLevelLabel } from '@/lib/modelCard'
import { useLocale } from '@/i18n/LocaleProvider'
import { formatClockTime, formatDuration, formatMessageTimestamp } from '@/lib/time'
import { withAiDisclosure } from '@/lib/aiDisclosure'
import { cn } from '@/lib/utils'
import { fetchResearchRun } from '@/api/inqtrixClient'
import { offersFullQuestion, revealedQuestion } from '../questionReveal'

/**
 * The user's own words, in full.
 *
 * A run row stores at most `QUESTION_COLUMN_LIMIT` characters so list
 * responses stay small; the durable payload keeps what was actually
 * asked. Without this control the cut was visible but the text was
 * unreachable — the reader could see that something was missing and had
 * no way to read it. The server sets `question_clipped`, so nothing here
 * guesses from a trailing ellipsis.
 */
function ClippedQuestion({ run }: { run: AgentRunRecord }) {
  const { t } = useLocale()
  const [full, setFull] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [failed, setFailed] = useState(false)

  if (!offersFullQuestion(run)) {
    return <p className="whitespace-pre-wrap break-words">{run.question}</p>
  }

  const reveal = async () => {
    setLoading(true)
    setFailed(false)
    try {
      const summary = await fetchResearchRun(run.runId)
      setFull(revealedQuestion(run.question, summary.question_full))
    } catch {
      setFailed(true)
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <p className="whitespace-pre-wrap break-words">{full ?? run.question}</p>
      {full === null && (
        <button
          type="button"
          onClick={() => void reveal()}
          disabled={loading}
          className="mt-1 text-xs underline underline-offset-2 opacity-80 hover:opacity-100 disabled:opacity-50"
        >
          {failed
            ? t.agent.timeline.questionFullFailed
            : loading
              ? t.agent.timeline.questionFullLoading
              : t.agent.timeline.questionFullShow}
        </button>
      )}
    </>
  )
}

import { appMotion } from '@/motion/transitions'
import type { CanvasViewDescriptor } from '@/features/canvas/types'
import type { Locale, TranslationDictionary } from '@/i18n/translations'
import { countLabel } from '@/i18n/plural'
import { autonomyModeLabel } from '../runSetupLabels'
import { AgentActivityLine } from '../AgentPulseTrack'
import { isMissionChild } from '../childMission'
import { childProgressLine } from '../childProgress'
import { renderInlineMarkdown } from './inlineMarkdown'
import {
  groupStepEntries,
  isEvidenceReadEntry,
  taskResultShownByRow,
  taskRowResultPreview,
} from './stepGroups'
import { todoStepStats, todoStepText } from '../todoRetrospective'
import { diffHunkPlan, type DiffHunkPlan } from './artifactDiffHunks'
import { reviewChangedLines, reviewKeptDraft } from '../answerDraft'
import { ChildMissionCard } from './ChildMissionCard'
import {
  canEditAgentRun,
  isActiveAgentRun,
  isGateAgentRun,
  type AgentAnswerDraft,
  type AgentArtifactRecord,
  type AgentChildProgressRecord,
  type AgentRunOutcome,
  type AgentRunRecord,
  type AgentStepEntry,
} from '../model'
import type {
  AgentApprovalDecisionRequest,
  AgentClarificationAnswerRequest,
} from '../types'
import type { PlanSourceInfo } from '../plan/sourceLabel'
import type { AgentPlanDraft } from '../plan/usePlanApproval'
import { clarificationAnswerSummary } from '../clarificationAnswers'
import {
  agentArtifactReferences,
  agentCitationLabelFromHref,
  agentReferenceAsKnowledge,
  isWebEvidenceReference,
  answerCitationLabels,
  linkifyAgentArtifactCitations,
  type AgentArtifactReference,
} from '../artifactCitations'
import {
  agentActivityIconKind,
  activityDisplayText,
  activityStepDisplayText,
  evidenceReadSummary,
  isRoleOperation,
} from '../activityPresentation'
import { effectiveAgentTaskStatus } from '../plan/taskPresentation'
import {
  agentRunCompletionRecap,
  answerStatesOutcome,
  artifactClaimMarks,
  runClockParts,
  runSettingsSummary,
  agentTodoReportAge,
  documentOutcome,
  KERNEL_REVIEW_NARRATION_ID,
  latestChildActivity,
  shouldAnimateAgentNarration,
} from '../runPresentation'
import { ClaimNote, useClaimNote } from '../claimNote'
import { CitationGroupList } from '@/features/knowledge/CitationRow'
import {
  citationViews,
  groupCitationsByDocument,
} from '@/features/knowledge/citations'
import { agentRunFailureText } from '../runFailure'
import { runHeadline } from '../runHeadline'
import { decidedReportGuidance } from '../plan/reportGuidance'
import { WebEvidenceSourceRow } from '../WebEvidenceSourceRow'
import { copyTextToClipboard } from '@/lib/clipboard'

export type AgentTimelineActions = {
  answerClarification: (
    runId: string,
    clarificationId: string,
    answer: AgentClarificationAnswerRequest,
  ) => Promise<unknown>
  decideApproval: (
    runId: string,
    approvalId: string,
    decision: AgentApprovalDecisionRequest,
  ) => Promise<unknown>
  /** Child-gate twins: the decision targets the CHILD run id, the local
   * echo lands on the parent record. */
  answerChildClarification: (
    parentRunId: string,
    childRunId: string,
    clarificationId: string,
    answer: AgentClarificationAnswerRequest,
  ) => Promise<unknown>
  decideChildApproval: (
    parentRunId: string,
    childRunId: string,
    approvalId: string,
    decision: AgentApprovalDecisionRequest,
  ) => Promise<unknown>
  onCancelRun: (runId: string) => void
  onOpenCanvas: (descriptor: CanvasViewDescriptor) => void
  /** Revision-body fetch for the inline chip diff. Resolves the
   * artifact's CURRENT anchor internally; view-only, never store-bound. */
  loadArtifactRevision: (
    runId: string,
    artifactId: string,
    revision: number,
  ) => Promise<{ content_markdown: string }>
  applyPatch: (
    runId: string,
    patchId: string,
    expectedRevision: number,
  ) => Promise<
    | { kind: 'applied'; revision: number; appliedEditIds: string[] }
    | { kind: 'conflict'; currentRevision: number | null }
  >
  rejectPatch: (
    runId: string,
    patchId: string,
    note: string,
  ) => Promise<unknown>
  /** Shared per-run plan drafts (reducer-owned — one draft, two surfaces). */
  planDrafts: Record<string, AgentPlanDraft>
  setPlanDraft: (runId: string, draft: AgentPlanDraft | null) => void
  /** Collection titles + vector-backend label for the plan's per-task
   * source line (same data plane as the canvas plan view). */
  planSource: PlanSourceInfo
}

/**
 * The rules line under a question: "Rule: <title> (rev. n) · review pass
 * against the task". Each rule is named by the title it was attached
 * under (stored with its part), else by its current catalog title, and by
 * the event's label once neither is known. The review note names the
 * pass the run OWES (the event is emitted when the run starts, before any
 * verdict) and appears only for a value this desk knows; an unknown one
 * says nothing rather than something untrue.
 */
function requirementLine(
  meta: NonNullable<AgentRunRecord['requirementMeta']>,
  ruleTitles: Readonly<Record<string, string>>,
  t: ReturnType<typeof useLocale>['t'],
): string {
  // Function replacers: a title is user text, and a string replacement
  // would read `$&` in it as a pattern.
  const rules = meta.rules
    .map((rule) => t.agent.timeline.requirementRuleEntry
      .replace('{revision}', String(rule.revision))
      .replace('{label}', () => rule.title ?? ruleTitles[rule.templateId] ?? rule.label))
    .join(' · ')
  const head = countLabel(
    meta.rules.length,
    t.agent.timeline.requirementRulesOne,
    t.agent.timeline.requirementRules,
  ).replace('{rules}', () => rules)
  const review = meta.review === 'none'
    ? t.agent.timeline.requirementReviewNone
    : meta.review === 'assignment'
      ? t.agent.timeline.requirementReviewAssignment
      : meta.review === 'per_rule'
        ? t.agent.timeline.requirementReviewPerRule
        : null
  return review ? `${head} · ${review}` : head
}

/**
 * One session turn as a chat TRANSCRIPT: the user's question as
 * a bubble, the agent's steps as a running feed of one-liners (stepLog
 * joined with the control rows), decisions as user-side entries, and a
 * permanently mounted live status line while the run is active. Gates
 * are NOT rendered here — they live in the composer gate tray; the rich
 * surfaces (plan, Verlauf, memo) live in the canvas tabs.
 */
export function AgentRunTurn({
  actions,
  artifactNames,
  historical = false,
  ruleTitles = {},
  run,
  transportDegraded = false,
}: {
  actions: AgentTimelineActions
  /** Session file names (artifactId -> `name.md`), derived from the
   * anchor-independent session index; absent while it has not loaded —
   * the chips then fall back to plain titles instead of faking names. */
  artifactNames?: Record<string, string>
  /** Persisted/hydrated turns render in place. Exact events carrying the
   * transport's live provenance may still animate as the run continues. */
  historical?: boolean
  /** Rule titles by template id, from the agent rule catalog — the name
   * the composer pill showed for the same rule. */
  ruleTitles?: Readonly<Record<string, string>>
  run: AgentRunRecord
  /** Live updates are on the polling fallback — shown as
   * a visible hint, never a silent behavior change. */
  transportDegraded?: boolean
}) {
  const { locale, t } = useLocale()
  const reduceMotion = Boolean(useReducedMotion())
  const isActive = isActiveAgentRun(run.status)
  // Settled turns collapse their step feed to one summary line
  // ("✓ n Schritte") so the session history stays scannable; the ACTIVE
  // run always streams in full.
  const [historyExpanded, setHistoryExpanded] = useState(false)
  const [workPlanExpanded, setWorkPlanExpanded] = useState(false)
  const showSteps = isActive || historyExpanded
  // Inline chip diffs: expansion state lives on the TURN so the
  // file rows keep their open diff when they move between the running
  // fallback slot and the answer block.
  const [expandedDiffs, setExpandedDiffs] = useState<
    Record<string, boolean>
  >({})
  // The sent-comments line above the question bubble (collapsed by
  // default — quiet, expandable on demand).
  const [contextExpanded, setContextExpanded] = useState(false)
  const memoId = run.artifactOrder.find(
    (artifactId) => run.artifacts[artifactId]?.kind === 'memo',
  )
  const answerId = run.artifactOrder.find(
    (artifactId) => run.artifacts[artifactId]?.kind === 'answer',
  )
  const answer = answerId ? run.artifacts[answerId] : undefined
  const draft = run.answerDraft
  const tasks = (run.plan?.tasks ?? []).filter(
    (task) => task.toolKind !== 'synthesis',
  )
  const done = tasks.filter(
    (task) => effectiveAgentTaskStatus(task, run.taskStates[task.taskId]) === 'completed',
  ).length
  const failed = tasks.filter((task) => {
    const status = effectiveAgentTaskStatus(task, run.taskStates[task.taskId])
    return status === 'failed' || status === 'insufficient_evidence'
  })
  const lastTaskError = [...failed]
    .reverse()
    .map((task) => run.taskStates[task.taskId]?.error)
    .find(Boolean)
  const recapFacts = agentRunCompletionRecap(
    run,
    tasks,
    memoId ? run.artifacts[memoId] : undefined,
  )
  // The review's sentence stands once: under the answer when it carries it.
  const completionRecap = answerStatesOutcome(run)
    ? { ...recapFacts, outcome: 'none' as const }
    : recapFacts
  const runSettings = runSettingsSummary(run, t)
  // File rows are OUTPUT: they render inside the answer block —
  // above its copy bar — once an answer exists, and fall back to the
  // turn's tail while the run is still producing one.
  const artifactRows = run.touchedArtifacts.length > 0 ? (
    <ArtifactFileRows
      actions={actions}
      artifactNames={artifactNames}
      expandedDiffs={expandedDiffs}
      historical={historical}
      onToggleDiff={(artifactId) =>
        setExpandedDiffs((current) => ({
          ...current,
          [artifactId]: !current[artifactId],
        }))}
      reduceMotion={reduceMotion}
      run={run}
      t={t}
    />
  ) : null
  // The disclosure belongs to the turn, so loading an answer cannot reset
  // the work plan when it moves into the answer's supporting details.
  const workPlan = (run.todos?.length ?? 0) > 0 ? (
    <AgentTodoList
      folded={!isActive}
      onToggle={() => setWorkPlanExpanded((current) => !current)}
      open={workPlanExpanded}
      reportAge={isActive
        ? {
          childActivityAt: latestChildActivity(run.children),
          clockOffset: run.clockOffsetSeconds,
          stepLog: run.stepLog,
          todosAt: run.todosAt,
        }
        : null}
      stepLog={run.stepLog}
      t={t}
      todos={run.todos ?? []}
    />
  ) : null

  return (
    <motion.div
      animate={{ opacity: 1, y: 0 }}
      className="space-y-2"
      initial={reduceMotion || historical ? false : { opacity: 0, y: 6 }}
      transition={appMotion.card}
    >
      {/* The question follows the chat mode's user-message pattern to
          the letter (meta line above, tone-aware bubble below) so Agent
          Desk and Chat read as ONE design language. */}
      {/* Meta line, sent-comments box and bubble are SIBLINGS in a
          right-aligned column: each sizes to its own content under
          the same cap, so expanding the box never re-widens the bubble
          — the box may simply be wider than the bubble. */}
      <div className="flex min-w-0 flex-col items-end">
        <div className="mb-1 flex max-w-[min(72%,44rem)] flex-wrap items-center justify-end gap-x-2 gap-y-0.5 t-meta-sm font-semibold text-muted-foreground">
            <span>{t.chat.you}</span>
            <span className="whitespace-nowrap tabular-nums">
              {formatMessageTimestamp(run.createdAt, locale)}
            </span>
            {run.autonomy && (
              <span className="rounded-full border border-border bg-background px-1.5 py-px t-hint font-semibold">
                {autonomyModeLabel(run.autonomy, t)}
              </span>
            )}
            {run.depth === 'deep' && (
              <span className="rounded-full border border-brand/30 bg-brand-subtle px-1.5 py-px t-hint font-semibold text-brand">
                {t.agent.composer.deep}
              </span>
            )}
          </div>
          {run.canvasContextMeta
            && run.canvasContextMeta.comments.length > 0 && (
            <div className="mb-1.5 max-w-[min(72%,44rem)] rounded-md border border-border/70 bg-surface/60 text-left">
              {/* The chat chain-trace design language — full-width
                  header button, bordered rows — records which comments
                  rode this submission (replay-durable). The box sizes to
                  its own content and may exceed the bubble's width when
                  expanded, because it is a SIBLING of the bubble rather
                  than its container. */}
              <button
                aria-expanded={contextExpanded}
                className="flex w-full items-center gap-1.5 px-2.5 py-1.5 text-left t-meta-sm font-semibold text-muted-foreground transition hover:text-foreground"
                onClick={() => setContextExpanded((current) => !current)}
                type="button"
              >
                <MessageSquareText className="size-3.5 shrink-0" />
                <span className="min-w-0 flex-1 truncate">
                  {countLabel(
                    run.canvasContextMeta.comments.length,
                    t.agent.timeline.sentCanvasCommentsOne,
                    t.agent.timeline.sentCanvasComments,
                  )
                    .replace(
                      '{name}',
                      artifactNames?.[run.canvasContextMeta.artifactId]
                        || t.agent.canvas.views.document,
                    )}
                </span>
                <ChevronRight
                  className={cn(
                    'size-3.5 shrink-0 transition-transform',
                    contextExpanded && 'rotate-90',
                  )}
                />
              </button>
              {contextExpanded && (
                <div className="border-t border-border/70 px-2.5 py-1">
                  {run.canvasContextMeta.comments.map((comment, index) => (
                    <div
                      className="flex min-w-0 gap-1.5 border-b border-border/40 py-1 last:border-0"
                      key={index}
                    >
                      <span className="grid size-4 shrink-0 place-items-center rounded-[4px] bg-brand-subtle t-hint font-semibold tabular-nums text-brand">
                        {index + 1}
                      </span>
                      <span className="min-w-0 t-meta-sm">
                        <span className="text-muted-foreground">
                          „{comment.quotePreview}“
                        </span>{' '}
                        <span className="text-foreground">
                          {comment.comment}
                        </span>
                      </span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          <div className="inqtrix-user-bubble min-w-0 max-w-[min(72%,44rem)] rounded-lg px-3 py-2.5 text-sm leading-6 shadow-[0_1px_2px_var(--shadow-hairline)]">
            <ClippedQuestion run={run} />
          </div>
          {/* The rules this turn ran under, each with the revision it is
              frozen at — the one thing the question text cannot show. */}
          {run.requirementMeta && run.requirementMeta.rules.length > 0 && (
            <p className="mt-1 flex max-w-[min(72%,44rem)] items-center gap-1 t-hint text-muted-foreground">
              <BookOpen aria-hidden="true" className="size-3 shrink-0" />
              <span className="min-w-0 break-words" data-testid="agent-run-requirement">
                {requirementLine(run.requirementMeta, ruleTitles, t)}
              </span>
            </p>
          )}
      </div>

      {/* ONE run-status slot: while the run is active it holds the live
          line (activity, elapsed, stop); on completion the same slot holds
          the recap. Keeping both in one place means the answer below never
          shifts when the run settles — the status changes state, it does
          not move (a live row under the answer would vanish while the
          recap appeared above it). */}
      {isActive && (
        <div
          className="mx-1 flex items-center gap-2 border-l-2 border-border/60 py-1 pl-3"
          data-testid="agent-run-status"
        >
          {/* The headline names the KIND of work running now; the step
              rows below carry its detail, so the two never repeat a line. */}
          <AgentActivityLine
            className="min-w-0 flex-1"
            gate={isGateAgentRun(run.status)}
            text={runHeadline(run, t)}
          />
          <RunClock
            clockOffset={run.clockOffsetSeconds}
            lastProgressAt={run.lastProgressAt}
            locale={locale}
            startedAt={run.startedAt}
            t={t}
            waiting={isGateAgentRun(run.status)}
          />
          {canEditAgentRun(run) && (
            <Button
              aria-label={t.agent.composer.stop}
              className="size-5 shrink-0 text-muted-foreground hover:text-destructive"
              onClick={() => actions.onCancelRun(run.runId)}
              size="icon"
              type="button"
              variant="ghost"
            >
              <X className="icon-xs" />
            </Button>
          )}
        </div>
      )}
      {run.status === 'completed' && (
        <RunCompletionRecap
          modelSource={run.modelResolution?.modelSource}
          onToggleSteps={() =>
            setHistoryExpanded((current) => !current)}
          recap={completionRecap}
          settings={runSettings}
          stepCount={run.stepLog.length}
          stepsExpanded={historyExpanded}
          t={t}
        />
      )}

      {/* Completed runs fold the step toggle into the recap's fact line
          — the standalone row remains for failed/cancelled runs. */}
      {!isActive && run.status !== 'completed' && run.stepLog.length > 0 && (
        <button
          aria-expanded={historyExpanded}
          className="flex items-center gap-1.5 px-1 t-meta text-muted-foreground transition-colors hover:text-foreground"
          onClick={() => setHistoryExpanded((current) => !current)}
          type="button"
        >
          <X className="icon-xs shrink-0 text-muted-foreground" />
          {countLabel(
            run.stepLog.length,
            t.agent.timeline.stepsSummaryOne,
            t.agent.timeline.stepsSummary,
          )}
          <ChevronRight
            className={cn(
              'size-3 shrink-0 transition-transform',
              historyExpanded && 'rotate-90',
            )}
          />
        </button>
      )}
      {/* A failed or cancelled turn states its conditions too — the reader
          judges a broken-off run by what it was allowed to do. */}
      {!isActive && run.status !== 'completed' && runSettings.length > 0 && (
        <RunSettingsLine
          className="px-1"
          modelSource={run.modelResolution?.modelSource}
          settings={runSettings}
          t={t}
        />
      )}
      {run.stepLog.length > 0 && (
        <motion.div
          animate={showSteps ? { height: 'auto', opacity: 1 } : { height: 0, opacity: 0 }}
          aria-hidden={!showSteps}
          className="space-y-1 overflow-hidden px-1"
          inert={!showSteps ? true : undefined}
          initial={false}
          transition={reduceMotion ? { duration: 0 } : appMotion.list}
        >
          {groupStepEntries(run.stepLog).map((item) => item.kind === 'sections' ? (
            <SectionLines
              active={isActive && item.entries[item.entries.length - 1] === run.stepLog[run.stepLog.length - 1]}
              entries={item.entries}
              key={`sections-${item.seq}`}
              t={t}
            />
          ) : item.kind === 'evidence' ? (
            <EvidenceReadGroup
              entries={item.entries}
              historical={historical}
              key={`evidence-${item.entries[0].activityKey ?? item.seq}`}
              runStatus={run.status}
              t={t}
            />
          ) : (
            <StreamEntry
              actions={actions}
              artifactNames={artifactNames}
              entry={item.entry}
              historicalRun={historical}
              isLatest={isActive && item.entry === run.stepLog[run.stepLog.length - 1]}
              key={item.entry.seq}
              locale={locale}
              run={run}
              t={t}
            />
          ))}
        </motion.div>
      )}

      {tasks.length > 0
        && (run.phase === 'execution' || run.phase === 'evidence') && (
        <button
          className="flex items-center gap-1.5 px-1 t-meta tabular-nums text-muted-foreground transition-colors hover:text-foreground"
          onClick={() =>
            actions.onOpenCanvas({ runId: run.runId, view: 'plan' })}
          type="button"
        >
          <ListChecks className="icon-xs" />
          {countLabel(
            tasks.length,
            t.agent.timeline.stepsProgressOne,
            t.agent.timeline.stepsProgress,
          ).replace('{done}', String(done))}
        </button>
      )}

      {answer || draft ? (
        <AgentAnswerBlock
          actions={actions}
          answer={answer}
          artifactRows={artifactRows}
          draft={draft}
          run={run}
          t={t}
          workPlan={workPlan}
        />
      ) : (
        artifactRows && <div className="px-1">{artifactRows}</div>
      )}

      {(run.status === 'failed' || run.status === 'cancelled') && (
        <div className="flex items-start gap-2 px-1">
          <AlertTriangle className="mt-0.5 icon-sm shrink-0 text-destructive" />
          <div className="min-w-0">
            <p className="break-words t-meta text-destructive/90">
              {run.status === 'cancelled'
                ? t.agent.timeline.cancelled
                : (run.error || run.errorCode)
                  ? t.agent.timeline.taskFailed.replace(
                    '{error}',
                    agentRunFailureText(run.error ?? '', t, run.errorCode),
                  )
                  : t.agent.timeline.failure}
            </p>
            {tasks.length > 0 && (
              <p className="mt-0.5 t-hint tabular-nums text-muted-foreground">
                {t.agent.timeline.terminalTaskSummary
                  .replace('{done}', String(done))
                  .replace('{total}', String(tasks.length))
                  .replace('{failed}', countLabel(
                    failed.length,
                    t.agent.timeline.attentionCountOne,
                    t.agent.timeline.attentionCount,
                  ))}
              </p>
            )}
            {!run.error && !run.errorCode && lastTaskError && (
              <p className="mt-0.5 break-words t-meta-sm text-destructive/90">
                {lastTaskError}
              </p>
            )}
          </div>
        </div>
      )}

      {(run.error || run.errorCode)
        && run.status !== 'failed'
        && run.status !== 'cancelled' && (
        <p
          className="flex items-start gap-1.5 px-1 t-meta text-destructive/90"
          data-testid="agent-run-surface-error"
          role="status"
        >
          <AlertTriangle className="mt-0.5 icon-xs shrink-0" />
          <span className="min-w-0 break-words">
            {agentRunFailureText(run.error ?? '', t, run.errorCode)}
          </span>
        </p>
      )}

      {isActive && transportDegraded && (
        <p className="flex items-center gap-1.5 px-1 t-hint text-muted-foreground">
          <AlertTriangle className="icon-xs shrink-0 text-warning" />
          {t.agent.timeline.transportDegraded}
        </p>
      )}
      {/* The answer block carries the plan as soon as it stands, as a
          draft too. */}
      {!answer && !draft && workPlan && (
        <div className="px-1">{workPlan}</div>
      )}
    </motion.div>
  )
}

/** The model-reported work plan; execution activity is shown separately. */
function AgentTodoList({
  folded,
  onToggle,
  open,
  reportAge,
  stepLog,
  t,
  todos,
}: {
  /** After the run ended the list stays as a retrospective, folded to
   * its headline ("3/3 erledigt") until the reader opens it. */
  folded: boolean
  onToggle: () => void
  open: boolean
  /** What dates the model's last report of this list, while the run is
   * active — the list is a report, not a live readout. Its age ticks in
   * its own span (`TodoReportAge`); it stood still between events. */
  reportAge: TodoReportAgeInput | null
  stepLog: AgentStepEntry[]
  t: TranslationDictionary
  todos: { content: string; status: string }[]
}) {
  const doneCount = todos.filter((todo) => todo.status === 'completed').length
  const showItems = !folded || open
  return (
    <div
      className="rounded-lg border border-border/60 bg-muted/30 px-2.5 py-1.5"
      data-testid="agent-todo-list"
    >
      <p className="t-caption uppercase tracking-wide text-muted-foreground/70">
        {folded ? (
          <button
            aria-expanded={open}
            className="inline-flex items-center gap-1 uppercase tracking-wide hover:text-foreground"
            data-testid="agent-todo-toggle"
            onClick={onToggle}
            type="button"
          >
            <span aria-hidden="true">{open ? '▾' : '▸'}</span>
            {t.agent.timeline.todoTitle}
            {' · '}
            {t.agent.timeline.todoDone
              .replace('{done}', String(doneCount))
              .replace('{total}', String(todos.length))}
          </button>
        ) : (
          t.agent.timeline.todoTitle
        )}
        {reportAge !== null && <TodoReportAge input={reportAge} t={t} />}
      </p>
      {showItems && (
      <ul className="mt-1 space-y-0.5">
        {todos.map((todo, index) => {
          const done = todo.status === 'completed'
          const active = todo.status === 'in_progress'
          const steps = todoStepText(todoStepStats(stepLog, index), t)
          return (
            <li
              className={cn(
                'flex items-start gap-1.5 t-meta',
                done
                  ? 'text-muted-foreground/70 line-through'
                  : active
                    ? 'text-foreground/90'
                    : 'text-muted-foreground',
              )}
              key={`${index}:${todo.content}`}
            >
              {done ? (
                <Check className="mt-0.5 icon-xs shrink-0 text-success/80" />
              ) : active ? (
                <span
                  aria-hidden="true"
                  className="mt-1.5 size-1.5 shrink-0 rounded-full bg-brand"
                />
              ) : (
                <span
                  aria-hidden="true"
                  className="mt-1.5 size-1.5 shrink-0 rounded-full border border-muted-foreground/50"
                />
              )}
              <span className="min-w-0 break-words">
                {todo.content}
                {steps && (
                  <span className="ml-1.5 t-hint tabular-nums text-muted-foreground/70">
                    {steps}
                  </span>
                )}
              </span>
            </li>
          )
        })}
      </ul>
      )}
    </div>
  )
}

/** One isolated 1 s tick for a span that shows time: only the span repaints. */
function useSecondTick(): void {
  const [, setTick] = useState(0)
  useEffect(() => {
    // A plain interval: rAF pauses in background tabs.
    const id = window.setInterval(() => setTick((n) => n + 1), 1000)
    return () => window.clearInterval(id)
  }, [])
}

/** Server time now (s): the client clock minus the measured offset. */
function serverNow(clockOffset: number | undefined): number {
  return Date.now() / 1000 - (clockOffset ?? 0)
}

/**
 * The run line's clock: elapsed time since the server's run start and,
 * after a quiet stretch, the age of the newest update — its own events and
 * its children's. Server time, so a skewed client clock neither invents nor
 * hides elapsed time. While the run waits for the user, the age is not
 * counted against it.
 */
function RunClock({
  clockOffset,
  lastProgressAt,
  locale,
  startedAt,
  t,
  waiting,
}: {
  clockOffset: number | undefined
  lastProgressAt: number | undefined
  locale: Locale
  startedAt: string | undefined
  t: TranslationDictionary
  waiting: boolean
}) {
  useSecondTick()
  const parts = runClockParts(
    { lastProgressAt, now: serverNow(clockOffset), startedAt, waiting },
    t,
  )
  if (!parts) return null
  const { elapsed, note } = parts
  return (
    <span
      className="flex min-w-0 shrink items-center gap-1 t-hint tabular-nums text-muted-foreground"
      data-testid="agent-run-clock"
      title={lastProgressAt === undefined ? undefined : formatClockTime(lastProgressAt, locale)}
    >
      <span className="shrink-0">{elapsed}</span>
      {note && <span className="min-w-0 truncate">{`\u00b7 ${note}`}</span>}
    </span>
  )
}

type TodoReportAgeInput = {
  childActivityAt: number
  clockOffset: number | undefined
  stepLog: readonly AgentStepEntry[]
  todosAt: number | undefined
}

/** "last reported N ago" for the model's list, ticking in its own span. */
function TodoReportAge({
  input,
  t,
}: {
  input: TodoReportAgeInput
  t: TranslationDictionary
}) {
  useSecondTick()
  const seconds = agentTodoReportAge(
    input.todosAt,
    input.stepLog,
    serverNow(input.clockOffset),
    input.childActivityAt,
  )
  if (seconds === null) return null
  return (
    <span data-testid="agent-todo-reported-at">
      {' \u00b7 '}
      {t.agent.timeline.todoReportedAgo.replace('{duration}', formatDuration(seconds))}
    </span>
  )
}

/** The run's conditions as one quiet line (see `runSettingsSummary`). */
function RunSettingsLine({
  className,
  modelSource,
  settings,
  t,
}: {
  className?: string
  /** Where the model came from (live resolution only) — a tooltip, not
   * a claim: after a reload it is honestly absent. */
  modelSource?: string
  settings: readonly string[]
  t: TranslationDictionary
}) {
  return (
    <p
      aria-label={t.agent.timeline.recapSettingsLabel}
      className={cn('t-meta-sm text-muted-foreground', className)}
      data-testid="agent-run-settings"
      title={modelSource}
    >
      {settings.join(' \u00b7 ')}
    </p>
  )
}

function RunCompletionRecap({
  modelSource,
  onToggleSteps,
  recap,
  settings,
  stepCount = 0,
  stepsExpanded = false,
  t,
}: {
  modelSource?: string
  /** Step-feed disclosure folded into the fact line. */
  onToggleSteps?: () => void
  recap: ReturnType<typeof agentRunCompletionRecap>
  /** `runSettingsSummary` parts; empty hides the line. */
  settings: readonly string[]
  stepCount?: number
  stepsExpanded?: boolean
  t: TranslationDictionary
}) {
  const facts = [
    recap.taskCount > 0
      ? countLabel(
        recap.taskCount,
        t.agent.timeline.recapTasksOne,
        t.agent.timeline.recapTasks,
      ).replace('{done}', String(recap.tasksCompleted))
      : '',
    recap.referenceCount > 0
      ? countLabel(
        recap.referenceCount,
        t.agent.timeline.recapReferencesOne,
        t.agent.timeline.recapReferences,
      )
      : '',
    recap.elapsedSeconds !== undefined
      ? t.agent.timeline.recapElapsed.replace(
        '{duration}',
        formatDuration(recap.elapsedSeconds),
      )
      : '',
  ].filter(Boolean)
  const process = [
    recap.synthesized ? t.agent.timeline.recapSynthesized : '',
    recap.outcome === 'missing'
      ? t.agent.timeline.recapNoOutcome
      : recap.outcome === 'none'
        ? ''
        : recap.outcome.summary,
  ].filter(Boolean)
  return (
    <section className="mx-1 border-l-2 border-success/55 py-1 pl-3">
      {/* Title and facts share ONE flex-wrap row: a single compact
          line on normal widths that folds back to two when narrow. */}
      <div className="flex min-w-0 flex-wrap items-center gap-x-2.5 gap-y-1">
        <span className="flex shrink-0 items-center gap-2">
          <Check className="icon-sm shrink-0 text-success" />
          <p className="t-list text-foreground/90">
            {t.agent.timeline.recapTitle}
          </p>
        </span>
        {(facts.length > 0 || stepCount > 0) && (
          <div className="flex min-w-0 flex-wrap items-center gap-x-1.5 gap-y-1 t-meta tabular-nums text-muted-foreground">
            {facts.length > 0 && <span>{facts.join(' · ')}</span>}
            {stepCount > 0 && onToggleSteps && (
              <>
                {facts.length > 0 && <span aria-hidden="true">·</span>}
                <button
                  aria-expanded={stepsExpanded}
                  className="flex items-center gap-1 transition-colors hover:text-foreground"
                  onClick={onToggleSteps}
                  type="button"
                >
                  {countLabel(
                    stepCount,
                    t.agent.timeline.stepsSummaryOne,
                    t.agent.timeline.stepsSummary,
                  )}
                  <ChevronRight
                    className={cn(
                      'size-3 shrink-0 transition-transform',
                      stepsExpanded && 'rotate-90',
                    )}
                  />
                </button>
              </>
            )}
          </div>
        )}
      </div>
      {process.length > 0 && (
        <p className="mt-0.5 t-meta-sm text-foreground/75">
          {process.join(' · ')}
        </p>
      )}
      {settings.length > 0 && (
        <RunSettingsLine
          className="mt-0.5"
          modelSource={modelSource}
          settings={settings}
          t={t}
        />
      )}
    </section>
  )
}

/**
 * The turn's file rows: one row per touched document, the ± badge
 * doubling as a disclosure (chevron right -> down) that expands an
 * inline hunk diff below the rows — the canvas full diff stays
 * reachable from inside the expansion.
 */
function ArtifactFileRows({
  actions,
  artifactNames,
  expandedDiffs,
  historical,
  onToggleDiff,
  reduceMotion,
  run,
  t,
}: {
  actions: AgentTimelineActions
  artifactNames?: Record<string, string>
  expandedDiffs: Record<string, boolean>
  historical: boolean
  onToggleDiff: (artifactId: string) => void
  reduceMotion: boolean
  run: AgentRunRecord
  t: TranslationDictionary
}) {
  return (
    <div className="space-y-1.5">
      <div className="flex flex-wrap items-center gap-1.5">
        {run.touchedArtifacts.map((touched) => {
          // File-row identity: the derived session file name leads;
          // title stays the honest fallback while the session index
          // has not loaded (never a fake unsuffixed name).
          const label = artifactNames?.[touched.artifactId]
            || touched.title
            || run.artifacts[touched.artifactId]?.title
            || t.agent.canvas.views.document
          // The ± badge needs a diffable base revision AND a complete
          // server-counted sum — otherwise it stays honestly absent.
          const showDelta = touched.fromRevision >= 1
            && touched.linesAdded !== undefined
            && touched.linesRemoved !== undefined
          const expanded = Boolean(expandedDiffs[touched.artifactId])
          return (
            <motion.span
              animate={{ opacity: 1, y: 0 }}
              className="inline-flex h-6 min-w-0 max-w-80 items-stretch overflow-hidden rounded-full border border-border bg-surface t-hint font-semibold text-muted-foreground"
              // Same live-boundary rule as the step rows: only a chip
              // that arrived on the live side of its stream rises.
              initial={
                reduceMotion || historical || !touched.arrivedLive
                  ? false
                  : { opacity: 0, y: 4 }
              }
              key={touched.artifactId}
              transition={appMotion.list}
            >
              <button
                className="inline-flex min-w-0 items-center gap-1.5 pl-2.5 pr-2 transition-colors hover:text-foreground"
                onClick={() => actions.onOpenCanvas({
                  artifactId: touched.artifactId,
                  runId: run.runId,
                  view: 'document',
                })}
                // Full name stays reachable at the visual cut.
                title={label}
                type="button"
              >
                <FileText className="icon-xs shrink-0" />
                <span className="truncate">{label}</span>
                <span className="shrink-0 rounded-full border border-border bg-background px-1.5 py-px t-hint font-semibold">
                  {touched.created
                    ? t.agent.timeline.artifactCreated
                    : t.agent.timeline.artifactUpdated}
                </span>
              </button>
              {showDelta && (
                <button
                  aria-expanded={expanded}
                  aria-label={t.agent.timeline.artifactDiff
                    .replace('{from}', String(touched.fromRevision))
                    .replace('{to}', String(touched.revision))}
                  className="inline-flex shrink-0 items-center gap-1 border-l border-border px-2 tabular-nums transition-colors hover:bg-background"
                  onClick={() => onToggleDiff(touched.artifactId)}
                  title={t.agent.timeline.artifactDiff
                    .replace('{from}', String(touched.fromRevision))
                    .replace('{to}', String(touched.revision))}
                  type="button"
                >
                  <span className="text-success">
                    +{touched.linesAdded}
                  </span>
                  <span className="text-destructive">
                    −{touched.linesRemoved}
                  </span>
                  <ChevronRight
                    className={cn(
                      'size-3 shrink-0 transition-transform',
                      expanded && 'rotate-90',
                    )}
                  />
                </button>
              )}
            </motion.span>
          )
        })}
      </div>
      {run.touchedArtifacts
        .filter((touched) => expandedDiffs[touched.artifactId])
        .map((touched) => (
          <ArtifactInlineDiff
            actions={actions}
            artifactId={touched.artifactId}
            fromRevision={touched.fromRevision}
            key={touched.artifactId}
            runId={run.runId}
            t={t}
            toRevision={touched.revision}
          />
        ))}
    </div>
  )
}

/**
 * Inline hunk diff of one turn's revision span: changed regions
 * only — git-style context, visible "unchanged lines" gaps, an inner
 * scroll bound — with the canvas full-document diff one click away.
 */
function ArtifactInlineDiff({
  actions,
  artifactId,
  fromRevision,
  runId,
  t,
  toRevision,
}: {
  actions: AgentTimelineActions
  artifactId: string
  fromRevision: number
  runId: string
  t: TranslationDictionary
  toRevision: number
}) {
  const [plan, setPlan] = useState<DiffHunkPlan | null>(null)
  const [error, setError] = useState<string | null>(null)
  // The actions change with every event of a running run; the revisions do
  // not, so the diff loads once per revision pair through the latest loader.
  const loadRevisionRef = useRef(actions.loadArtifactRevision)
  loadRevisionRef.current = actions.loadArtifactRevision

  useEffect(() => {
    let cancelled = false
    setPlan(null)
    setError(null)
    void Promise.all([
      loadRevisionRef.current(runId, artifactId, fromRevision),
      loadRevisionRef.current(runId, artifactId, toRevision),
    ])
      .then(([from, to]) => {
        if (cancelled) return
        setPlan(diffHunkPlan(from.content_markdown, to.content_markdown))
      })
      .catch(() => {
        if (!cancelled) setError(t.agent.timeline.diffLoadFailed)
      })
    return () => {
      cancelled = true
    }
  }, [artifactId, fromRevision, runId, t, toRevision])

  const gap = (count: number, key: string) => (
    <p
      className="px-2 py-0.5 t-hint text-muted-foreground/80"
      key={key}
    >
      {countLabel(count, t.agent.timeline.diffSkippedLinesOne, t.agent.timeline.diffSkippedLines)}
    </p>
  )

  return (
    <div className="min-w-0 max-w-4xl rounded-md border border-border bg-surface">
      {error ? (
        <p className="px-2 py-1.5 t-hint text-destructive/90" role="status">
          {error}
        </p>
      ) : plan === null ? (
        <p className="px-2 py-1.5 t-hint text-muted-foreground">…</p>
      ) : (
        <div className="max-h-72 overflow-y-auto py-1 font-mono t-hint leading-5">
          {plan.hunks.map((hunk, hunkIndex) => (
            <div key={hunkIndex}>
              {hunk.skippedBefore > 0
                && gap(hunk.skippedBefore, `gap-${hunkIndex}`)}
              {hunk.lines.map((line, lineIndex) => (
                <p
                  className={cn(
                    'flex min-w-0 gap-2 whitespace-pre-wrap break-words px-2',
                    line.type === 'insert'
                      && 'bg-success/10 text-success',
                    line.type === 'delete'
                      && 'bg-destructive/10 text-destructive',
                    line.type === 'context' && 'text-muted-foreground',
                  )}
                  key={lineIndex}
                >
                  <span aria-hidden="true" className="w-3 shrink-0 select-none">
                    {line.type === 'insert'
                      ? '+'
                      : line.type === 'delete'
                        ? '−'
                        : ' '}
                  </span>
                  <span className="min-w-0 flex-1">{line.text || ' '}</span>
                </p>
              ))}
            </div>
          ))}
          {plan.skippedAfter > 0 && gap(plan.skippedAfter, 'gap-tail')}
        </div>
      )}
      <div className="border-t border-border/70 px-2 py-1">
        <button
          className="t-hint text-muted-foreground transition-colors hover:text-foreground"
          onClick={() => actions.onOpenCanvas({
            artifactId,
            fromRevision,
            runId,
            toRevision,
            view: 'diff',
          })}
          type="button"
        >
          {t.agent.timeline.openFullDiff}
        </button>
      </div>
    </div>
  )
}

/** Quiet right-aligned receipt for an answer/approval already captured by the
 * gate. It is intentionally not a second user-message bubble. */
function DecisionReceipt({ children }: { children: React.ReactNode }) {
  return (
    <div className="my-2 flex min-w-0 justify-end px-1">
      <div className="w-fit max-w-[min(78%,48rem)] space-y-1 border-r border-border/80 py-0.5 pr-2 text-right text-foreground/75">
        {children}
      </div>
    </div>
  )
}

/**
 * The chat-form deliverable, rendered FLAT like the chat mode's
 * assistant answers (ChatWorkspace pattern — meta header line, body
 * directly on the surface, NO bubble): only the user question keeps a
 * bubble, so Agent Desk and Chat read as one design language.
 *
 * A reviewed kernel run shows its draft here first (`run.answerDraft`), in
 * the same block, so the reviewed answer replaces it in place. The draft
 * stands until the reviewed answer is ready, also while that one streams
 * in; a run that ends without it keeps the draft as unreviewed. The
 * reviewed answer then marks what the review changed, and the draft stays
 * one disclosure away. A draft has no copy and no export.
 */
function AgentAnswerBlock({
  actions,
  answer,
  artifactRows,
  draft,
  run,
  t,
  workPlan,
}: {
  actions: AgentTimelineActions
  answer: AgentArtifactRecord | undefined
  /** The turn's file rows — output belongs to the answer, above
   * its copy bar, not above the run status. */
  artifactRows?: React.ReactNode
  draft: AgentAnswerDraft | undefined
  run: AgentRunRecord
  t: TranslationDictionary
  /** Model-reported work, between the answer and its sources/output controls. */
  workPlan?: React.ReactNode
}) {
  const { locale } = useLocale()
  const reduceMotion = Boolean(useReducedMotion())
  const [copied, setCopied] = useState(false)
  // A streaming reviewed answer would replace the whole text twice within
  // a second; the draft stands until the reviewed answer is ready.
  const showsDraft = draft !== undefined && answer?.status !== 'ready'
  const body = showsDraft ? draft.markdown : answer?.contentMarkdown
  const writing = !showsDraft && answer?.status === 'writing'
  const ready = !showsDraft && answer?.status === 'ready'
  const answerState = showsDraft
    ? (isActiveAgentRun(run.status) ? 'draft-reviewing' : 'draft-unreviewed')
    : answer?.status
  // The record describes the answer the run delivered; once the user
  // edited it, the line would describe text that is gone.
  const outcome = answer ? documentOutcome(run.outcome, answer) : undefined
  // Its marks belong to the published answer, never to the draft.
  const claimMarks = useMemo(
    () => (ready && answer ? artifactClaimMarks(outcome, answer) : []),
    [answer, outcome, ready],
  )
  const claimNote = useClaimNote()
  // The single citation path: the chat answer reuses the
  // canvas machinery \u2014 same refs, same linkify, same label semantics.
  // While writing, labels stay plain text (no chips, no sources yet).
  const references = useMemo(
    () => (showsDraft || writing || !answer ? [] : agentArtifactReferences(answer.refs)),
    [answer, showsDraft, writing],
  )
  // Citations render from the FIRST delta. While writing the real refs
  // do not exist yet, but the server sends the LABELS the finished
  // answer will cite (`answer.started`) — and the linkifier needs only
  // those. Without this the body would be rewritten wholesale the moment
  // the answer settles: every `[W1]` would become a link in one step,
  // which reads as the message being re-inserted. Same labels before and
  // after, so the markdown handed to the renderer does not change at all.
  // A draft names the labels it cites in its own event.
  const citationLabels = useMemo(
    () => answerCitationLabels(
      showsDraft || writing,
      showsDraft ? draft.referenceLabels : answer?.publicationRefLabels,
      references,
    ),
    [answer?.publicationRefLabels, draft, references, showsDraft, writing],
  )
  const linkedBody = useMemo(
    () =>
      body !== undefined && citationLabels.length > 0
        ? linkifyAgentArtifactCitations(body, citationLabels)
        : body,
    [body, citationLabels],
  )
  // What the review changed against the draft the reader read; a review
  // that published the draft's own text changes only the state.
  const revisedDraft = ready && draft !== undefined && body !== undefined
    && !reviewKeptDraft(draft.markdown, body)
    ? draft
    : undefined
  const changedLines = useMemo(
    () => (revisedDraft && body !== undefined
      ? reviewChangedLines(revisedDraft.markdown, body)
      : undefined),
    [body, revisedDraft],
  )
  const onCitationClick = (event: MouseEvent<HTMLDivElement>) => {
    const anchor = (event.target as HTMLElement | null)?.closest('a')
    const label = agentCitationLabelFromHref(anchor?.getAttribute('href'))
    if (!label || !answer) return
    const reference = references.find((item) => item.label === label)
    if (!reference) return
    event.preventDefault()
    event.stopPropagation()
    actions.onOpenCanvas({
      artifactId: answer.artifactId,
      label,
      runId: run.runId,
      view: 'evidence',
    })
  }
  const copy = () => {
    if (body === undefined) return
    void copyTextToClipboard(
      withAiDisclosure(body, t.aiTransparency.exportNotice),
    ).then((copied) => {
      if (!copied) return
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    })
  }
  return (
    <div
      className="px-1"
      aria-live={writing || answerState === 'draft-reviewing' ? 'polite' : undefined}
      data-answer-state={answerState}
      data-testid="agent-answer"
    >
      <div className="mb-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 t-meta-sm font-semibold text-muted-foreground">
        <span>{t.chat.assistant}</span>
        <span className="font-normal">{t.agent.timeline.answerLabel}</span>
        {showsDraft && (
          <span className="rounded-full border border-border bg-background px-1.5 py-px t-hint font-medium">
            {answerState === 'draft-reviewing'
              ? t.agent.timeline.answerDraftReviewing
              : t.agent.timeline.answerDraftUnreviewed}
          </span>
        )}
        {run.finishedAt && (
          <span className="whitespace-nowrap tabular-nums">
            {formatMessageTimestamp(run.finishedAt, locale)}
          </span>
        )}
        {run.modelResolution?.model && (
          <span
            className="rounded-full border border-border bg-background px-1.5 py-px t-hint font-medium"
            title={run.modelResolution.modelSource}
          >
            {run.modelResolution.model}
            {run.modelResolution.effort
              ? ` \u00b7 ${effortLevelLabel(run.modelResolution.effort)}`
              : ''}
          </span>
        )}
      </div>
      {body !== undefined && showsDraft ? (
        // No selection copy either: nothing of a draft leaves the block.
        <div
          className="max-w-4xl text-sm leading-snug text-foreground"
          onClickCapture={onCitationClick}
        >
          <MarkdownRenderer markdown={linkedBody ?? body} variant="chat" />
        </div>
      ) : body !== undefined ? (
        <MarkdownSelectionCopyMenu
          aiGenerated
          className="max-w-4xl text-sm leading-snug text-foreground"
          markdown={body}
          onBlurCapture={claimNote.onBlurCapture}
          onClickCapture={(event: MouseEvent<HTMLDivElement>) => {
            claimNote.onClickCapture(event)
            onCitationClick(event)
          }}
          onFocusCapture={claimNote.onFocusCapture}
        >
          <MarkdownRenderer
            changedLines={changedLines}
            claimMarks={claimMarks}
            isStreaming={writing}
            markdown={linkedBody ?? body}
            variant="chat"
          />
          {writing && (
            <span
              aria-hidden="true"
              className={cn(
                'ml-1 inline-block size-2 rounded-full bg-brand align-middle',
                !reduceMotion && 'animate-pulse',
              )}
            />
          )}
        </MarkdownSelectionCopyMenu>
      ) : (
        <span
          aria-hidden="true"
          className={cn(
            'inline-block size-2 rounded-full bg-brand align-middle',
            !reduceMotion && 'animate-pulse',
          )}
        />
      )}
      {claimMarks.length > 0 && (
        <ClaimNote
          anchor={claimNote.anchor}
          line={claimNote.line}
          note={claimNote.note}
          onClose={claimNote.close}
        />
      )}
      {!showsDraft && answer?.status === 'interrupted' && (
        <p className="mt-1 t-meta text-destructive" role="status">
          {t.agent.timeline.answerInterrupted}
        </p>
      )}
      {revisedDraft && (
        <div className="mt-2 max-w-4xl" data-testid="agent-answer-revision">
          <p className="t-meta-sm text-muted-foreground">
            {t.agent.timeline.answerRevisedMarks}
          </p>
          <AgentDisclosure
            summary={t.agent.timeline.answerShowDraft}
            testId="agent-answer-draft"
          >
            <div className="border-l border-border/60 py-1 pl-3 text-sm leading-snug text-muted-foreground">
              <MarkdownRenderer
                markdown={linkifyAgentArtifactCitations(
                  revisedDraft.markdown,
                  answerCitationLabels(true, revisedDraft.referenceLabels, []),
                )}
                variant="chat"
              />
            </div>
          </AgentDisclosure>
        </div>
      )}
      {workPlan && <div className="mt-3 max-w-4xl">{workPlan}</div>}
      {ready && answer && references.length > 0 && (
        <AgentAnswerSources
          onOpenEvidence={(label) =>
            actions.onOpenCanvas({
              artifactId: answer.artifactId,
              label,
              runId: run.runId,
              view: 'evidence',
            })}
          references={references}
        />
      )}
      {artifactRows && <div className="mt-2">{artifactRows}</div>}
      {ready && outcome && (
        <RunOutcomeLine className="mt-2 max-w-4xl" outcome={outcome} t={t} />
      )}
      {body !== undefined && ready && answer && (
        <div className="mt-1 flex items-center gap-1">
          <Button
            aria-label={t.agent.timeline.answerCopy}
            className="size-6 text-muted-foreground hover:text-foreground"
            onClick={copy}
            size="icon"
            type="button"
            variant="ghost"
          >
            {copied ? (
              <Check className="icon-xs text-success" />
            ) : (
              <Copy className="icon-xs" />
            )}
          </Button>
          <Button
            aria-label={t.agent.timeline.answerOpenCanvas}
            className="size-6 text-muted-foreground hover:text-foreground"
            onClick={() =>
              actions.onOpenCanvas({
                artifactId: answer.artifactId,
                runId: run.runId,
                view: 'document',
              })}
            size="icon"
            type="button"
            variant="ghost"
          >
            <ExternalLink className="icon-xs" />
          </Button>
        </div>
      )}
    </div>
  )
}

/**
 * How the run's review ended, under the output it describes: the
 * record's summary as one line, what the review did besides it and what
 * it left open each behind a disclosure. Every word is the server's;
 * nothing here is counted.
 */
export function RunOutcomeLine({
  className,
  outcome,
  t,
}: {
  className?: string
  outcome: AgentRunOutcome
  t: TranslationDictionary
}) {
  return (
    <div className={className} data-testid="agent-run-outcome">
      <p className="t-meta-sm text-muted-foreground">{outcome.summary}</p>
      {outcome.details && (
        <AgentDisclosure
          summary={t.agent.timeline.outcomeDetailsToggle}
          testId="agent-run-outcome-details"
        >
          <p className="ml-1.5 break-words border-l border-border/60 py-1 pl-3 t-meta text-muted-foreground">
            {outcome.details}
          </p>
        </AgentDisclosure>
      )}
      {outcome.lines.length > 0 && (
        <AgentDisclosure
          summary={t.agent.timeline.outcomeLinesToggle}
          testId="agent-run-outcome-lines"
        >
          <ul className="ml-1.5 space-y-1 border-l border-border/60 py-1 pl-3">
            {outcome.lines.map((line, index) => (
              <li
                className="break-words t-meta text-muted-foreground"
                data-line-kind={line.kind}
                key={index}
              >
                {line.text}
              </li>
            ))}
          </ul>
        </AgentDisclosure>
      )}
    </div>
  )
}

/**
 * Source list under the chat answer, folded behind a disclosure:
 * a long reference list (13 rows in the survey session measured 862px)
 * would otherwise dominate the transcript, so the default is collapsed
 * with a visible count — same chevron convention as every other
 * expander. Web references retain their external URL and expose the
 * same evidence-trail action/status as the Canvas; Knowledge references
 * open the evidence view directly.
 */
function AgentAnswerSources({
  onOpenEvidence,
  references,
}: {
  onOpenEvidence: (label: string) => void
  references: AgentArtifactReference[]
}) {
  const { t } = useLocale()
  const [expanded, setExpanded] = useState(false)
  const webReferences = references.filter(isWebEvidenceReference)
  const knowledgeGroups = useMemo(
    () =>
      groupCitationsByDocument(
        citationViews(
          references
            .filter((reference) => !isWebEvidenceReference(reference))
            .map(agentReferenceAsKnowledge),
          [],
          t.knowledge.viewerSection,
        ).map((view) => ({ ...view, canOpen: true })),
      ),
    [references, t.knowledge.viewerSection],
  )
  return (
    <section
      className="mt-3 max-w-4xl border-t border-border/70 pt-2"
      data-testid="agent-sources"
    >
      <button
        aria-expanded={expanded}
        className="flex items-center gap-1 t-meta-sm font-semibold text-muted-foreground transition-colors hover:text-foreground"
        onClick={() => setExpanded((current) => !current)}
        type="button"
      >
        {countLabel(
          references.length,
          t.agent.timeline.sourcesSummaryOne,
          t.agent.timeline.sourcesSummary,
        )}
        <ChevronRight
          className={cn(
            'size-3 shrink-0 transition-transform',
            expanded && 'rotate-90',
          )}
        />
      </button>
      {expanded && (
        <div className="mt-1.5 space-y-2">
          {/* Knowledge citations render EXACTLY like the Knowledge Desk's:
              the cited passage leads, the document name and its section
              sit in the quiet meta line, and several passages of one PDF
              collapse into one group. A flat list repeated the same file
              name for every citation and never said WHERE in it. */}
          {knowledgeGroups.length > 0 && (
            <CitationGroupList
              activeKey={null}
              groups={knowledgeGroups}
              onOpen={(view) => onOpenEvidence(view.label)}
              onOpenDocument={(group) => {
                const first = group.citations[0]
                if (first) onOpenEvidence(first.label)
              }}
            />
          )}
          {webReferences.length > 0 && (
            <ul className="space-y-1.5">
              {webReferences.map((reference) => (
                <li key={reference.label}>
                  <WebEvidenceSourceRow
                    onInspect={() => onOpenEvidence(reference.label)}
                    reference={{
                      ...reference,
                      domain: hostFromUrl(reference.url ?? ''),
                      key: reference.referenceId ?? reference.label,
                    }}
                  />
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </section>
  )
}

// Deliberately NO third-party favicon fetch here: loading
// icons.duckduckgo.com/ip3/<domain>.ico would leak every cited source
// hostname to an external service on view — against the product's
// data-minimization line (log pseudonymization, webUrl()). Globe2 it is.

function hostFromUrl(url: string): string | null {
  try {
    return new URL(url).hostname
  } catch {
    return null
  }
}

/** One transcript line, dispatched by step kind. */
function StreamEntry({
  actions,
  artifactNames,
  entry,
  historicalRun = false,
  isLatest = false,
  locale,
  run,
  t,
}: {
  actions: AgentTimelineActions
  /** Session file names by artifact id: tool rows name the document. */
  artifactNames?: Record<string, string>
  entry: AgentStepEntry
  historicalRun?: boolean
  /** Newest line of an ACTIVE run — the only one that types itself. */
  isLatest?: boolean
  locale: Locale
  run: AgentRunRecord
  t: TranslationDictionary
}) {
  switch (entry.kind) {
    case 'phase': {
      const station = entry.phase
        ? (stationLabel(entry.phase, t) ?? null)
        : null
      if (!station) return null
      return (
        <p className="pt-1.5 t-caption uppercase tracking-wide text-muted-foreground/70">
          {station}
        </p>
      )
    }
    case 'activity': {
      // A review row is one short line: its label carries the count,
      // and the passages it objected to fold under it.
      const text = entry.findings?.length && entry.label
        ? entry.label
        : activityStepDisplayText(
          entry,
          t,
          (artifactId) => artifactNames?.[artifactId] ?? run.artifacts[artifactId]?.title,
        )
      if (!text) return null
      // Append-only protocol: a finished invocation KEEPS its row (the
      // observed bug was searches vanishing once their task settled) —
      // the row's own status drives the glyph, never the task status.
      const failed = entry.status === 'failed'
      const fallback = entry.fallback === true
      const settled = entry.status === 'completed'
      const displayText = failed && entry.error
        ? `${text} · ${entry.error}`
        : text
      const row = (
        <p
          className={cn(
            'flex items-start gap-1.5 t-meta',
            entry.taskId && 'pl-5',
            failed
              ? 'text-destructive/90'
              : fallback
                ? 'text-warning'
                : 'text-muted-foreground',
          )}
          data-testid="agent-activity-item"
        >
          {failed || fallback ? (
            <AlertTriangle className="mt-0.5 icon-xs shrink-0" />
          ) : entry.evidenceLimit ? (
            // A note about the evidence, neither an alarm nor a plain
            // success: the row keeps the muted tone and names the limit.
            <Info className="mt-0.5 icon-xs shrink-0" />
          ) : settled ? (
            <Check className="mt-0.5 icon-xs shrink-0 text-success/80" />
          ) : (
            <ActivityGlyph operation={entry.activityOperation} />
          )}
          <ClampedText text={displayText} />
        </p>
      )
      if (entry.findings?.length) {
        // The review's own line carries the count; the substance is the
        // passage it objected to. Without this the reader gets a number
        // and has to trust it — the text under the finding is the only
        // part they can check themselves. It is one click away, so the
        // row stays one line in the transcript.
        return (
          <>
            {row}
            <AgentDisclosure
              summary={t.agent.timeline.reviewFindingsToggle}
              testId="agent-review-findings"
            >
              <ul className="ml-1.5 space-y-1.5 border-l border-border/60 py-1 pl-3">
                {entry.findings.map((finding, index) => (
                  <li className="min-w-0 t-meta" key={`${entry.seq}-${index}`}>
                    <q className="block break-words text-foreground/80">
                      {finding.quote}
                    </q>
                    <span className="block break-words text-muted-foreground">
                      {finding.finding}
                    </span>
                  </li>
                ))}
              </ul>
            </AgentDisclosure>
          </>
        )
      }
      const child = delegatedChild(run, entry)
      if (child && !isRoleOperation(entry.activityOperationCode) && isMissionChild(child)) {
        // A delegated MISSION gets the card: stages, one row per plan
        // task with its result head, the latest decision, the sum. Once
        // the tool row settled it stays as the folded retrospective.
        return (
          <>
            {row}
            <ChildMissionCard
              child={child}
              folded={settled || failed}
              onOpenDetails={() => actions.onOpenCanvas({ runId: child.childRunId, view: 'run' })}
              t={t}
            />
          </>
        )
      }
      const childLine = settled || failed || !child
        ? null
        : childProgressLine(child, t, locale)
      if (!childLine) return row
      // A delegation is ONE tool row that can own the run for tens of
      // minutes. Its child reports phase and task all along; this is
      // where that reaches the reader.
      return (
        <>
          {row}
          <p
            className="flex items-start gap-1.5 pl-5 t-meta text-muted-foreground/70"
            data-testid="agent-child-progress"
          >
            <span className="min-w-0 break-words">{childLine}</span>
          </p>
        </>
      )
    }
    case 'plan': {
      const label = (
        entry.autoApproved
          ? t.agent.timeline.planRevisedAuto
          : entry.version !== undefined && entry.version > 1
            ? t.agent.timeline.planRevised
            : t.agent.timeline.planProposed
      ).replace('{version}', String(entry.version ?? 1))
      return (
        <p className="flex items-center gap-1.5 t-meta text-foreground/85">
          <ListChecks className="icon-xs shrink-0 text-muted-foreground" />
          <span className="min-w-0 break-words">{label}</span>
          <button
            className="inline-flex shrink-0 items-center gap-1 text-brand transition-colors hover:text-brand/80"
            onClick={() =>
              actions.onOpenCanvas({ runId: run.runId, view: 'plan' })}
            type="button"
          >
            <ExternalLink className="icon-xs" />
            {t.agent.timeline.openInCanvas}
          </button>
        </p>
      )
    }
    case 'task_started':
    case 'task_finished':
    case 'task_failed': {
      const title =
        run.plan?.tasks.find((task) => task.taskId === entry.taskId)?.title
        ?? entry.taskId
        ?? ''
      const running =
        entry.kind === 'task_started'
        && run.taskStates[entry.taskId ?? '']?.status === 'running'
      // Started lines collapse into their outcome line once the task is
      // over — the transcript stays one line per finished step.
      if (entry.kind === 'task_started' && !running) return null
      const task = run.plan?.tasks.find((candidate) => candidate.taskId === entry.taskId)
      const effectiveStatus = task
        ? effectiveAgentTaskStatus(task, run.taskStates[task.taskId])
        : entry.kind === 'task_failed'
          ? 'failed'
          : entry.kind === 'task_finished'
            ? 'completed'
            : 'running'
      const insufficient = effectiveStatus === 'insufficient_evidence'
      // The one source of the result line: the fold of the task's
      // narration reads the same function, so the result stands once.
      const preview = taskRowResultPreview(entry, run)
      const terminal = entry.kind !== 'task_started'
      const glyph =
        insufficient ? (
          <AlertTriangle className="icon-xs shrink-0 text-warning" />
        ) : entry.kind === 'task_failed' ? (
          <X className="icon-xs shrink-0 text-destructive" />
        ) : entry.kind === 'task_finished' ? (
          <Check className="icon-xs shrink-0 text-success" />
        ) : (
          <span
            aria-hidden="true"
            className="size-1.5 shrink-0 rounded-full bg-brand inqtrix-running-dot"
          />
        )
      return (
        <button
          className="group relative flex w-full items-center gap-1.5 rounded py-1 pl-0.5 pr-6 text-left transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          onClick={() =>
            entry.taskId
            && actions.onOpenCanvas({
              runId: run.runId,
              taskId: entry.taskId,
              view: 'run',
            })}
          type="button"
        >
          {glyph}
          <span
            className={cn(
              'min-w-0 flex-1 break-words t-meta',
              insufficient
                ? 'text-warning'
                : entry.kind === 'task_failed'
                ? 'text-destructive/90'
                : 'text-foreground/85',
            )}
          >
            <span className="block font-medium">{title}</span>
            {terminal && (preview || entry.error || insufficient) && (
              <span className="mt-0.5 block line-clamp-2 pr-1 font-normal text-muted-foreground">
                {entry.error
                  ?? (insufficient
                    ? t.agent.task.statusInsufficientEvidence
                    : preview)}
              </span>
            )}
          </span>
          <ChevronRight
            aria-hidden="true"
            className="absolute right-1.5 top-1/2 icon-xs -translate-y-1/2 text-muted-foreground/55 transition-[color,transform] group-hover:translate-x-0.5 group-hover:text-foreground"
          />
        </button>
      )
    }
    case 'clarification_answered': {
      const clarification = run.clarifications.find(
        (item) => item.clarificationId === entry.clarificationId,
      )
      if (!clarification) return null
      const lines = clarificationAnswerSummary(clarification)
      if (lines.length === 0) return null
      return (
        <DecisionReceipt>
          {lines.map((line) => (
            <p
              className="break-words t-meta text-foreground/90"
              key={line.prompt}
            >
              <span className="text-muted-foreground">{line.prompt}</span>
              {` — ${line.answer}`}
            </p>
          ))}
        </DecisionReceipt>
      )
    }
    case 'approval_decided': {
      const approval = run.approvals.find(
        (item) => item.approvalId === entry.approvalId,
      )
      const label = decisionLabel(
        approval?.kind ?? 'plan',
        entry.detail ?? approval?.status ?? '',
        t,
      )
      if (!label) return null
      return (
        <DecisionReceipt>
          <p className="flex items-center justify-end gap-1.5 t-meta text-foreground/80">
            <PenLine className="icon-xs shrink-0 text-muted-foreground" />
            {label}
          </p>
        </DecisionReceipt>
      )
    }
    case 'narration':
      // A finished task's narration repeats its row's result line.
      if (!entry.text || taskResultShownByRow(entry, run)) return null
      // The review's closing line is the outcome's summary, which the
      // answer states under itself: one sentence, one place.
      if (
        entry.narrationId === KERNEL_REVIEW_NARRATION_ID
        && !entry.open
        && answerStatesOutcome(run)
      ) {
        return null
      }
      return (
        <NarrationText
          animate={shouldAnimateAgentNarration({
            entry,
            historicalRun,
            isLatest,
          })}
          text={entry.text}
        />
      )
    case 'notice': {
      const label = noticeDisplayText(entry, t)
      if (!label) return null
      const calm = noticeTone(entry) === 'calm'
      const Icon = calm ? Info : AlertTriangle
      return (
        <p
          className={cn('flex items-start gap-1.5 t-meta', calm ? 'text-muted-foreground' : 'text-warning')}
          data-testid="agent-notice-item"
        >
          <Icon className="mt-0.5 icon-xs shrink-0" />
          <span className="min-w-0 break-words">{label}</span>
        </p>
      )
    }
    case 'gate_requested': {
      // The request marker stays visible even when its row is gone —
      // unlike the decision receipts, history must never lose WHAT was
      // asked (the rows only enrich the line).
      const clarification = entry.clarificationId
        ? run.clarifications.find(
          (item) => item.clarificationId === entry.clarificationId,
        )
        : undefined
      const approval = entry.approvalId
        ? run.approvals.find(
          (item) => item.approvalId === entry.approvalId,
        )
        : undefined
      const label = entry.clarificationId
        ? t.agent.timeline.gateRequestedClarification
        : gateRequestedLabel(entry.detail ?? approval?.kind ?? '', t)
      const detail = clarification?.question ?? ''
      // Scope badge: a decision that granted the tool run-wide
      // stays readable in the history.
      const grantedForRun =
        approval?.decisionPayload?.approval_scope === 'run'
      // The result requirement shaped every section of the report. It
      // lived only in the decision payload, so after the gate closed
      // the user had no way to see what was still in force.
      const guidance = approval ? decidedReportGuidance([approval]) : ''
      return (
        <div className="space-y-0.5">
          <p className="flex items-start gap-1.5 t-meta text-muted-foreground">
            <PenLine className="mt-0.5 icon-xs shrink-0" />
            <span className="min-w-0 break-words">
              {label}
              {detail ? ` · ${detail}` : ''}
              {grantedForRun
                ? ` · ${t.agent.timeline.gateGrantedForRun}`
                : ''}
            </span>
          </p>
          {guidance && <GateGuidanceLine guidance={guidance} />}
        </div>
      )
    }
    default:
      return null
  }
}

/** The result requirement of a decided plan gate, collapsed by default.
 *
 * Collapsed because it can be 2000 characters and the transcript is a
 * conversation, not a form; present because a requirement the user
 * cannot re-read is one they have to remember. */
function GateGuidanceLine({ guidance }: { guidance: string }) {
  const { t } = useLocale()
  const [expanded, setExpanded] = useState(false)
  return (
    <div className="pl-[1.125rem]">
      <button
        aria-expanded={expanded}
        className="flex items-center gap-1 t-hint text-muted-foreground/80 transition-colors hover:text-foreground"
        onClick={() => setExpanded((current) => !current)}
        type="button"
      >
        <ChevronRight
          className={cn(
            'icon-xs transition-transform',
            expanded && 'rotate-90',
          )}
        />
        {t.agent.plan.reportGuidanceEffective}
      </button>
      {expanded && (
        <p className="mt-0.5 whitespace-pre-line break-words t-meta text-foreground/85">
          {guidance}
        </p>
      )}
    </div>
  )
}


/** How a runtime notice row reads: `calm` for a status the run is still
 * working through (the evidence is not yet sufficient), `warning` for
 * something that went wrong or was cut. Exported for unit tests. */
export function noticeTone(entry: Pick<AgentStepEntry, 'noticeCode'>): 'calm' | 'warning' {
  return entry.noticeCode === 'sufficiency_gap' ? 'calm' : 'warning'
}

/** i18n label of one runtime notice row (the row carries only its code).
 * Exported for unit tests (node env cannot render the component). */
export function noticeDisplayText(
  entry: Pick<AgentStepEntry, 'current' | 'detail' | 'noticeCode' | 'status' | 'total'>,
  t: TranslationDictionary,
): string {
  switch (entry.noticeCode) {
    case 'tool_limit':
      return t.agent.timeline.noticeToolLimit
        .replace('{used}', String(entry.current ?? '—'))
        .replace('{limit}', String(entry.total ?? '—'))
    case 'citation_validation':
      return entry.status === 'degraded'
        ? t.agent.timeline.noticeCitationsDegraded.replace(
          '{labels}',
          entry.detail ?? '',
        )
        : t.agent.timeline.noticeCitationsRepaired
    case 'sufficiency_gap': {
      // Server facts only: the judge's gaps are steering text for the model.
      const open = entry.current ?? 0
      return open > 0
        ? `${t.agent.timeline.noticeSufficiencyGap} · ${countLabel(
          open,
          t.agent.timeline.noticeSufficiencyOpenOne,
          t.agent.timeline.noticeSufficiencyOpen,
        )}`
        : t.agent.timeline.noticeSufficiencyGap
    }
    default:
      return ''
  }
}

function gateRequestedLabel(kind: string, t: TranslationDictionary): string {
  switch (kind) {
    case 'plan':
    case 'replan':
      return t.agent.timeline.gateRequestedPlan
    case 'discovery':
      return t.agent.timeline.gateRequestedDiscovery
    case 'patch':
      return t.agent.timeline.gateRequestedPatch
    case 'tool':
      return t.agent.timeline.gateRequestedTool
    default:
      return t.agent.timeline.gateRequested
  }
}

/** Consecutive retained reads share one status line; details keep every invocation. */
export function EvidenceReadGroup({
  entries, historical, runStatus, t,
}: {
  entries: AgentStepEntry[]
  historical: boolean
  runStatus: AgentRunRecord['status']
  t: TranslationDictionary
}) {
  const { done, failed, settled, summary } = evidenceReadSummary(entries, runStatus, t)
  return (
    <AgentDisclosure testId="agent-evidence-reads" summary={summary} warning={failed > 0}
      animateEntrance={!historical && Boolean(entries[0].arrivedLive)}
      icon={failed ? <AlertTriangle className="icon-xs shrink-0" />
        : settled ? <Check className="icon-xs shrink-0 text-success/80" />
          : <BookSearch className="icon-xs shrink-0" />}
      suffix={!settled && done > 0 && isActiveAgentRun(runStatus) ? (
        <span className="shrink-0 t-hint tabular-nums">
          {t.agent.timeline.evidenceReadProgress.replace('{done}', String(done)).replace('{count}', String(entries.length))}
        </span>
      ) : undefined}>
      <ul className="ml-1.5 space-y-1 border-l border-border/60 py-1 pl-3">
        {entries.map((entry) => (
          <li className="flex items-start gap-1.5 t-meta text-muted-foreground" key={entry.activityKey ?? entry.seq}>
            {entry.status === 'failed'
              ? <AlertTriangle className="mt-0.5 icon-xs shrink-0 text-warning" />
              : entry.status === 'completed'
                ? <Check className="mt-0.5 icon-xs shrink-0 text-success/80" />
                : <BookSearch className="mt-0.5 icon-xs shrink-0" />}
            <span className="min-w-0 break-words">
              {entry.detail?.trim() || t.agent.activityOperations.evidenceIndex}
              {entry.error && <span className="block text-warning">{entry.error}</span>}
            </span>
          </li>
        ))}
      </ul>
    </AgentDisclosure>
  )
}

function SectionLines({
  active,
  entries,
  t,
}: {
  active: boolean
  entries: AgentStepEntry[]
  t: TranslationDictionary
}) {
  const [open, setOpen] = useState(false)
  const newest = entries[entries.length - 1]
  const summary = active && newest?.text
    ? newest.text
    : countLabel(entries.length, t.agent.timeline.sectionsWrittenOne, t.agent.timeline.sectionsWritten)
  return (
    <div>
      <button
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 py-0.5 text-left text-sm leading-snug text-foreground/90 transition-colors hover:text-foreground"
        onClick={() => setOpen((current) => !current)}
        type="button"
      >
        <ChevronRight
          className={cn('size-3 shrink-0 text-muted-foreground transition-transform', open && 'rotate-90')}
        />
        <span className="min-w-0 truncate">{renderInlineMarkdown(summary)}</span>
      </button>
      {open && (
        <div className="ml-4 border-l border-border/60 pl-3">
          {entries.map((entry) => (
            <p className="py-0.5 t-meta text-muted-foreground" key={entry.seq}>
              {renderInlineMarkdown(entry.text ?? '')}
            </p>
          ))}
        </div>
      )}
    </div>
  )
}

/**
 * Narration prose with the "writes itself" feel: the NEWEST
 * line of an active run types in; history and replays render instantly
 * (the events are persisted paragraphs, the animation is presentation).
 */
function NarrationText({ animate, text }: { animate: boolean; text: string }) {
  const reduceMotion = Boolean(useReducedMotion())
  const typing = animate && !reduceMotion
  const [visible, setVisible] = useState(typing ? 0 : text.length)
  useEffect(() => {
    if (!typing) {
      setVisible(text.length)
      return undefined
    }
    if (visible >= text.length) return undefined
    const id = window.setTimeout(
      () => setVisible((count) => Math.min(count + 3, text.length)),
      18,
    )
    return () => window.clearTimeout(id)
  }, [text, typing, visible])
  return (
    <p className="whitespace-pre-wrap break-words py-0.5 text-sm leading-snug text-foreground/90">
      {renderInlineMarkdown(text.slice(0, visible))}
      {typing && visible < text.length && (
        <span aria-hidden="true" className="text-brand">
          ▍
        </span>
      )}
    </p>
  )
}

/** German decision line for the transcript (rows carry kind + status). */
function decisionLabel(
  kind: string,
  status: string,
  t: TranslationDictionary,
): string | null {
  const map = t.agent.timeline.decisions as Record<
    string,
    Record<string, string> | undefined
  >
  const byKind = map[kind] ?? map.plan
  if (!byKind) return null
  return byKind[status] ?? byKind.approved ?? null
}

function ActivityGlyph({
  operation,
}: {
  operation: AgentStepEntry['activityOperation']
}) {
  const kind = agentActivityIconKind(operation)
  if (kind === 'web') {
    return <Globe2 className="mt-0.5 icon-xs shrink-0" />
  }
  if (kind === 'knowledge') {
    return <BookSearch className="mt-0.5 icon-xs shrink-0" />
  }
  return <Search className="mt-0.5 icon-xs shrink-0" />
}

/** The live line of the child a delegation row started, if it has one.
 *
 * The link already exists: a child's `taskId` IS the delegating tool
 * call's id, which is how the gate tray finds the child's assignment. */
function delegatedChild(
  run: AgentRunRecord,
  entry: AgentStepEntry,
): AgentChildProgressRecord | null {
  const key = entry.activityKey
  if (!key?.startsWith('tool:')) return null
  const toolCallId = key.slice('tool:'.length)
  return Object.values(run.children).find(
    (candidate) => candidate.taskId.split(':')[0] === toolCallId,
  ) ?? null
}

/** A tool row reads as ONE line — "Tiefen-Recherche (Unterauftrag) ·
 * <Auftrag>" — and opens to its full text on click; the assignment of a
 * delegation ran to three lines and drowned the rows around it. Nothing
 * is cut: the whole text stays in the row and in the tooltip. */
function ClampedText({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <button
      aria-expanded={expanded}
      className={cn(
        'min-w-0 break-words text-left',
        !expanded && 'line-clamp-1',
      )}
      data-testid="agent-activity-text"
      onClick={() => setExpanded((value) => !value)}
      title={expanded ? undefined : text}
      type="button"
    >
      {text}
    </button>
  )
}

function stationLabel(
  phase: string,
  t: TranslationDictionary,
): string | undefined {
  const stations = t.agent.stations as Record<string, string | undefined>
  return stations[phase]
}

/** The live activity readout: explicit activity event > phase default. */
export function activityText(
  run: AgentRunRecord,
  t: TranslationDictionary,
  locale: Locale,
): string {
  // A freshly submitted run without a real queue position reads as
  // "starting", not as waiting in line (honest either way: an actual
  // position > 0 keeps the queue label).
  if (run.status === 'queued') {
    return run.queuePosition && run.queuePosition > 0
      ? t.agent.activity.queued
      : t.agent.activity.starting
  }
  if (run.status === 'waiting_for_approval') {
    return t.agent.activity.waitingApproval
  }
  if (run.status === 'waiting_for_input') return t.agent.activity.waitingInput
  // Waiting for children is NOT idling: the delegated run is doing the
  // work, and its progress arrives here as `child.progress`. Without
  // this branch the line fell through to the parent's own last tool
  // row, which froze the moment it delegated — one unchanging sentence
  // for twenty minutes, on BOTH surfaces that render this text (the
  // transcript's running line and the follow-execution panel). The run
  // looked hung while it was verifying its twelfth finding.
  if (run.status === 'waiting_for_children') {
    for (const child of Object.values(run.children)) {
      const line = childProgressLine(child, t, locale)
      if (line) return line
    }
  }
  // Parallel work is the point of the wave scheduler — the live line
  // says so instead of pretending one operation runs at a time.
  const runningTasks = run.plan
    ? run.plan.tasks.filter((task) => {
      const status = run.taskStates[task.taskId]?.status
      return status === 'running' || status === 'cancel_requested'
    }).length
    : 0
  const parallelPrefix = runningTasks > 1
    ? t.agent.activity.parallelTasks.replace('{count}', String(runningTasks))
    : ''
  if (run.activity?.operation === 'evidence_read' || run.activity?.label === 'read_evidence') {
    const currentReads: AgentStepEntry[] = []
    for (let index = run.stepLog.length - 1; index >= 0; index -= 1) {
      const entry = run.stepLog[index]
      if (!isEvidenceReadEntry(entry)) break
      currentReads.push(entry)
    }
    if (currentReads.length > 0) {
      const { summary, done, settled } = evidenceReadSummary(currentReads, run.status, t)
      return !settled && done > 0
        ? `${summary} · ${t.agent.timeline.evidenceReadProgress.replace('{done}', String(done)).replace('{count}', String(currentReads.length))}`
        : summary
    }
  }
  if (run.activity?.operation) {
    const operationText = activityDisplayText(run.activity, t)
    return parallelPrefix
      ? `${parallelPrefix} · ${operationText}`
      : operationText
  }
  if (run.activity?.kind === 'searching') {
    // A kernel tool row carries the tool id as its label (no normalized
    // operation): say the tool and its subject — "Tiefen-Recherche
    // (Unterauftrag) · <Auftrag>" — instead of the generic "Durchsucht
    // Quellen", which stood for eighteen minutes during a delegation.
    if (run.activity.label) return activityDisplayText(run.activity, t)
    return run.activity.detail
      ? `${t.agent.activity.searching} · ${run.activity.detail}`
      : t.agent.activity.searching
  }
  // The server label follows the run's language, so no event kind is
  // re-worded here. A local re-wording would contradict the transcript for
  // the same moment ("Recherche-Limit erreicht" against "Recherche-Cap
  // erreicht"), and an override for `critic_research_exhausted` would
  // flatten THREE distinct server reasons (no classified gap / research
  // cap / mission time budget) into one sentence, so the reader could not
  // tell which one applied. The server label is the single source.
  if (run.activity?.label) return run.activity.label
  if (run.phase === 'execution' && run.plan) {
    const tasks = run.plan.tasks.filter((task) => task.toolKind !== 'synthesis')
    const done = tasks.filter(
      (task) => run.taskStates[task.taskId]?.status === 'completed',
    ).length
    return t.agent.activity.taskProgress
      .replace('{done}', String(done))
      .replace('{total}', String(tasks.length))
  }
  const key = run.phase as keyof TranslationDictionary['agent']['activity']
  const label = t.agent.activity[key]
  return typeof label === 'string' ? label : t.agent.activity.execution
}
