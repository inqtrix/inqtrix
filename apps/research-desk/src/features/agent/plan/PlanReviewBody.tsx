import { useCallback, useId, useLayoutEffect, useRef, useState } from 'react'
import {
  ArrowDown,
  ArrowUp,
  BookOpen,
  Database,
  FileText,
  Globe2,
  PenLine,
  Plus,
  Trash2,
  Zap,
  type LucideIcon,
} from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Chip } from '@/components/ui/chip'
import {
  MentionMenu,
  type MentionMenuOption,
} from '@/components/ui/mention-menu'
import { StatusBadge } from '@/features/settings/parts'
import {
  REPORT_GUIDANCE_MAX_CHARS_FALLBACK,
  toggleReportRule,
} from '@/features/agent/reportRequirement'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import type { TranslationDictionary } from '@/i18n/translations'
import { cn } from '@/lib/utils'
import {
  detectCollectionMention,
  type CollectionMentionState,
} from '@/features/composer/collectionMention'
import { resizeTextareaToRows } from '@/features/composer/textareaAutosize'
import type { AgentPlanTaskRecord, AgentRunRecord } from '../model'
import { gateRuleChipTitle } from './reportGuidance'
import { planTaskSourceLabel, type PlanSourceInfo } from './sourceLabel'
import {
  buildUserPlanTask,
  webProfileOptionsForTier,
  withUserPlanTask,
} from './addTask'
import type { AgentPlanDraft, AgentPlanTaskDraft } from './usePlanApproval'
import {
  agentTaskExecutionLabel,
  agentTaskQueryLabel,
  agentTaskTypeLabel,
  agentPlanExecutionWaves,
  effectiveAgentTaskStatus,
  type AgentPlanExecutionWave,
  type AgentTaskEffectiveStatus,
} from './taskPresentation'

const TOOL_ICON: Record<AgentPlanTaskRecord['toolKind'], LucideIcon> = {
  web_research: Globe2,
  web_instant: Zap,
  rag_query: Database,
  file_analysis: FileText,
  synthesis: PenLine,
}

const RAG_PROFILE_OPTIONS: readonly string[] = [
  'schnell',
  'standard',
  'gruendlich',
  'tief',
]

/** Suchtiefe options per tool kind. web_research is capped to the run's
 * tier ceiling — the gate never OFFERS what the validator rejects
 * (mirror of the backend TIER_POLICIES; enforcement stays server-side). */
function profileOptionsFor(
  toolKind: AgentPlanTaskRecord['toolKind'],
  tier: string | undefined,
  depth: string | undefined,
): readonly string[] | undefined {
  if (toolKind === 'rag_query') return RAG_PROFILE_OPTIONS
  if (toolKind === 'web_research') {
    const options = webProfileOptionsForTier(tier, depth)
    // A single option is no choice — hide the select (legacy exact pin).
    return options.length > 1 ? options : undefined
  }
  return undefined
}

/** Mirror of the backend's ``MAX_TASK_QUERIES`` (plan_validation.py). */
const MAX_TASK_QUERIES = 8

/**
 * THE plan review — one implementation rendered in two densities:
 * the timeline card mounts it `density="compact"`, the canvas plan
 * view `density="full"`. Editable while a plan/replan approval is pending;
 * read-only afterwards. All edits go through the shared draft from
 * `usePlanApproval`, so approving with changes becomes `decision: 'edit'`
 * against the same backend validator the agent's planner uses.
 */
export function PlanReviewBody({
  decidedGuidance = '',
  decidedRuleLabels = [],
  reportRuleOptions = [],
  reportGuidanceMaxChars = REPORT_GUIDANCE_MAX_CHARS_FALLBACK,
  reportRuleIdsMax,
  density,
  draft,
  editable,
  planSource,
  run,
  updateDraft,
}: {
  /** The result requirement already in force (from the decided gate).
   * The plan itself cannot carry it, so a finished plan can only show
   * it when the caller hands it in. */
  decidedGuidance?: string
  /** Prompt-library rules the user may attach as a requirement — those
   * they marked visible for the agent. */
  reportRuleOptions?: ReportRuleOption[]
  /** Server limits for the requirement (published == enforced). The rule
   * cap has no default: null = not published, so the picker does not
   * pre-check the count and says the server checks it. */
  reportGuidanceMaxChars?: number
  reportRuleIdsMax: number | null
  /** Labels of the rules the decided gate ran under. */
  decidedRuleLabels?: string[]
  density: 'compact' | 'full'
  draft: AgentPlanDraft | null
  editable: boolean
  /** Collection titles + vector-backend label for the per-task source
   * line — the "wo" of the approval transparency. */
  planSource: PlanSourceInfo
  run: AgentRunRecord
  updateDraft: (update: (draft: AgentPlanDraft) => AgentPlanDraft) => void
}) {
  const { t } = useLocale()
  const plan = run.plan
  if (!plan || !draft) {
    return (
      <p className="t-meta text-muted-foreground">{t.agent.plan.noPlan}</p>
    )
  }
  const full = density === 'full'
  const executionWaves = agentPlanExecutionWaves(
    draft.tasks.map((task, ordinal) => ({ ...task, ordinal })),
  )

  return (
    <div className="min-w-0 space-y-3">
      {plan.summaryMarkdown && (
        <p className="t-body break-words text-foreground/90">
          {plan.summaryMarkdown}
        </p>
      )}

      {executionWaves.length > 0 && (
        <div>
          <p className="t-caption uppercase tracking-wide text-muted-foreground">
            {t.agent.plan.executionFlow}
          </p>
          <ol className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1">
            {executionWaves.map((wave, index) => (
              <li className="t-meta text-foreground/85" key={index}>
                <span className="font-medium">
                  {wavePrefix(wave, index, t)}
                </span>{' '}
                {formatExecutionWave(wave, t)}
              </li>
            ))}
          </ol>
        </div>
      )}

      <ul className="space-y-1">
        {draft.tasks.map((task, index) => (
          <PlanTaskRow
            agentTier={run.agentTier}
            depth={run.depth}
            editable={editable}
            full={full}
            index={index}
            key={task.taskId}
            liveState={run.taskStates[task.taskId]}
            planSource={planSource}
            status={effectiveAgentTaskStatus(
              plan.tasks.find((item) => item.taskId === task.taskId)
                ?? { status: 'pending' },
              run.taskStates[task.taskId],
            )}
            reportGuidance={draft.reportGuidance}
            reportGuidanceMaxChars={reportGuidanceMaxChars}
            reportRuleIdsMax={reportRuleIdsMax}
            reportRuleIds={draft.reportRuleIds}
            reportRuleOptions={reportRuleOptions}
            decidedGuidance={decidedGuidance}
            decidedRuleLabels={decidedRuleLabels}
            task={task}
            taskCount={draft.tasks.length}
            updateDraft={updateDraft}
          />
        ))}
      </ul>

      {editable && (
        <AddTaskRow
          agentTier={run.agentTier}
          depth={run.depth}
          planSource={planSource}
          updateDraft={updateDraft}
        />
      )}

      {(full || draft.assumptions.length > 0) && draft.assumptions.length > 0 && (
        <div>
          <p className="t-caption text-muted-foreground">
            {t.agent.plan.assumptions}
          </p>
          <ul className="mt-1 space-y-0.5">
            {draft.assumptions.map((assumption, index) => (
              <li className="t-meta text-muted-foreground" key={index}>
                {assumption}
              </li>
            ))}
          </ul>
        </div>
      )}

      {full && draft.successCriteria.length > 0 && (
        <div>
          <p className="t-caption text-muted-foreground">
            {t.agent.plan.successCriteria}
          </p>
          <ul className="mt-1 space-y-0.5">
            {draft.successCriteria.map((criterion, index) => (
              <li className="t-meta text-foreground/85" key={index}>
                {criterion}
              </li>
            ))}
          </ul>
        </div>
      )}

      {editable && (
        <p className="t-hint text-muted-foreground">{t.agent.plan.editHint}</p>
      )}
    </div>
  )
}

function formatExecutionWave(
  wave: AgentPlanExecutionWave,
  t: ReturnType<typeof useLocale>['t'],
): string {
  const labels: Array<[AgentPlanTaskRecord['toolKind'], string]> = [
    ['web_instant', t.agent.plan.waveInstant],
    ['web_research', t.agent.plan.waveResearch],
    ['rag_query', t.agent.plan.waveKnowledge],
    ['file_analysis', t.agent.plan.waveFile],
    ['synthesis', t.agent.plan.waveSynthesis],
  ]
  return labels
    .flatMap(([kind, label]) => {
      const count = wave.toolCounts[kind]
      return count ? [`${count}× ${label}`] : []
    })
    .join(' · ')
}

function wavePrefix(
  wave: AgentPlanExecutionWave,
  index: number,
  t: ReturnType<typeof useLocale>['t'],
): string {
  if (index === 0) {
    return wave.taskCount > 1 ? t.agent.plan.waveParallel : t.agent.plan.waveStart
  }
  return wave.taskCount > 1
    ? t.agent.plan.waveThenParallel
    : t.agent.plan.waveThen
}

function PlanTaskRow({
  agentTier,
  decidedGuidance,
  depth,
  editable,
  full,
  index,
  liveState,
  decidedRuleLabels,
  planSource,
  reportGuidance,
  reportGuidanceMaxChars,
  reportRuleIds,
  reportRuleIdsMax,
  reportRuleOptions,
  status,
  task,
  taskCount,
  updateDraft,
}: {
  agentTier: string | undefined
  depth: string | undefined
  editable: boolean
  full: boolean
  index: number
  liveState?: { status: string; error?: string }
  planSource: PlanSourceInfo
  status: AgentTaskEffectiveStatus
  /** Draft value of the result requirement — the synthesis row owns it. */
  reportGuidance: string
  reportRuleIds: string[]
  reportRuleOptions: ReportRuleOption[]
  reportGuidanceMaxChars: number
  reportRuleIdsMax: number | null
  /** The requirement already in force, for a plan that is decided. */
  decidedGuidance: string
  decidedRuleLabels: string[]
  task: AgentPlanTaskDraft
  taskCount: number
  updateDraft: (update: (draft: AgentPlanDraft) => AgentPlanDraft) => void
}) {
  const { t } = useLocale()
  const [queriesExpanded, setQueriesExpanded] = useState(false)
  const ToolIcon = TOOL_ICON[task.toolKind]
  const profileOptions = profileOptionsFor(task.toolKind, agentTier, depth)
  const profile = typeof task.params.profile === 'string' ? task.params.profile : ''
  const isSynthesis = task.toolKind === 'synthesis'
  const readQueries = task.queries.filter((query) => query.trim())
  const typeLabel = agentTaskTypeLabel(task.toolKind, t)
  const sourceLabel = planTaskSourceLabel(task, planSource, {
    allCollections: t.agent.plan.allCollections,
    knowledgeIndex: t.agent.plan.knowledgeIndex,
    recency: t.agent.plan.recency,
    web: t.agent.plan.webSource,
  })
  const executionLabel = agentTaskExecutionLabel(task, t)
  const queryLabel = agentTaskQueryLabel(task, t)
  const statusTone =
    status === 'completed'
      ? 'bg-success'
      : status === 'failed'
        ? 'bg-destructive'
        : status === 'insufficient_evidence'
          ? 'bg-warning'
        : status === 'running'
          ? 'bg-brand inqtrix-running-dot'
          : 'bg-muted-foreground/30'

  const patchTask = (patch: Partial<AgentPlanTaskDraft>) => {
    updateDraft((draft) => ({
      ...draft,
      tasks: draft.tasks.map((item) =>
        item.taskId === task.taskId ? { ...item, ...patch } : item,
      ),
    }))
  }

  return (
    <li className="group rounded-md border border-border bg-surface/50 px-2.5 py-1.5">
      <div className="flex items-start gap-2">
        <span aria-hidden="true" className={cn('size-1.5 shrink-0 rounded-full', statusTone)} />
        <span className="flex shrink-0" title={typeLabel}>
          <ToolIcon aria-label={typeLabel} className="icon-sm text-muted-foreground" />
        </span>
        {editable && !isSynthesis ? (
          <AutosizePlanTextarea
            ariaLabel={t.agent.plan.task}
            className="min-w-0 flex-1 rounded-sm border-none bg-transparent p-0 t-list text-foreground outline-none focus-visible:ring-1 focus-visible:ring-brand/40"
            maxRows={full ? undefined : 2}
            onChange={(value) => patchTask({ title: value })}
            value={task.title}
          />
        ) : (
          <span className="min-w-0 flex-1 break-words t-list text-foreground">
            {task.title}
          </span>
        )}
        {editable && !isSynthesis && (
          <span className="flex shrink-0 items-center opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100">
            <Button
              aria-label={t.agent.plan.moveUp}
              className="size-7 text-muted-foreground hover:text-foreground"
              disabled={index === 0}
              onClick={() => moveTask(updateDraft, task.taskId, -1)}
              size="icon"
              type="button"
              variant="ghost"
            >
              <ArrowUp className="icon-xs" />
            </Button>
            <Button
              aria-label={t.agent.plan.moveDown}
              className="size-7 text-muted-foreground hover:text-foreground"
              disabled={index >= taskCount - 1}
              onClick={() => moveTask(updateDraft, task.taskId, 1)}
              size="icon"
              type="button"
              variant="ghost"
            >
              <ArrowDown className="icon-xs" />
            </Button>
            <Button
              aria-label={t.agent.plan.deleteTask}
              className="size-7 text-muted-foreground hover:text-destructive"
              onClick={() =>
                updateDraft((draft) => ({
                  ...draft,
                  tasks: draft.tasks
                    .filter((item) => item.taskId !== task.taskId)
                    .map((item) => ({
                      ...item,
                      dependsOn: item.dependsOn.filter((id) => id !== task.taskId),
                    })),
                }))}
              size="icon"
              type="button"
              variant="ghost"
            >
              <Trash2 className="icon-xs" />
            </Button>
          </span>
        )}
      </div>
      <div className="mt-1 flex min-w-0 flex-wrap items-center gap-1.5 pl-6">
        <StatusBadge density="table" label={executionLabel} tone="neutral" />
        {profileOptions && editable && (
          <label className="flex shrink-0 items-center gap-1 t-hint text-muted-foreground">
            {t.agent.plan.searchDepth}
            <select
              aria-label={t.agent.plan.searchDepth}
              className="h-5 shrink-0 rounded border border-border bg-card px-1 t-hint text-muted-foreground"
              onChange={(event) =>
                patchTask({ params: { ...task.params, profile: event.target.value } })}
              value={profile || profileOptions[0]}
            >
              {profileOptions.map((option) => (
                <option key={option} value={option}>
                  {profileOptionLabel(task.toolKind, option, t)}
                </option>
              ))}
            </select>
          </label>
        )}
      </div>
      {task.objective && (
        <p className="mt-1 break-words pl-6 t-meta text-muted-foreground">
          {task.objective}
        </p>
      )}
      {editable && !isSynthesis ? (
        <QueryEditor full={full} patchTask={patchTask} t={t} task={task} />
      ) : (
        readQueries.length > 0 && (
          <div className="mt-1.5 pl-6">
            <p className="t-hint text-muted-foreground/80">
              {queryLabel}
            </p>
            <ul className="mt-0.5 space-y-0.5 border-l-2 border-brand/25 pl-2">
              {(full || queriesExpanded ? readQueries : readQueries.slice(0, 1)).map((query, queryIndex) => (
                <li
                  className={cn(
                    'break-words t-meta text-foreground/85',
                    !full && !queriesExpanded && 'line-clamp-2',
                  )}
                  key={queryIndex}
                >
                  {query}
                </li>
              ))}
            </ul>
            {!full && readQueries.length > 1 && (
              <button
                aria-expanded={queriesExpanded}
                className="mt-1 t-hint font-medium text-muted-foreground transition-colors hover:text-foreground"
                onClick={() => setQueriesExpanded((current) => !current)}
                type="button"
              >
                {queriesExpanded
                  ? t.agent.tray.fewerQueries
                  : countLabel(
                    readQueries.length,
                    t.agent.tray.expandQueriesOne,
                    t.agent.tray.expandQueries,
                  )}
              </button>
            )}
          </div>
        )
      )}
      {sourceLabel && (
        <p className="mt-1 break-words pl-6 t-hint text-muted-foreground">
          {t.agent.plan.source}: {sourceLabel}
        </p>
      )}
      {full && task.dependsOn.length > 0 && (
        <p className="mt-0.5 pl-6 t-hint text-muted-foreground/70">
          {t.agent.plan.dependsOn}: {task.dependsOn.join(', ')}
        </p>
      )}
      {full && !editable && task.expectedOutput && (
        <p className="mt-0.5 break-words pl-6 t-hint text-muted-foreground/70">
          {t.agent.plan.expectedOutput}: {task.expectedOutput}
        </p>
      )}
      {/* The result requirement belongs to the row that produces the
          result. At the bottom of the panel, below the task list and
          three add-buttons, the one control that shapes the output would
          be the least likely thing to be found. */}
      {isSynthesis && full && editable && (
        <div className="mt-1.5 pl-6">
          <label
            className="t-hint text-muted-foreground/80"
            htmlFor="agent-plan-report-guidance"
          >
            {t.agent.plan.reportGuidance}
          </label>
          <ReportRulePicker
            attached={reportRuleIds}
            max={reportRuleIdsMax}
            onToggle={(ruleId) =>
              updateDraft((current) => ({
                ...current,
                // The SERVER's cap, honored here: otherwise a fourth rule
                // would be accepted by the gate and then refused at the
                // decision, with the whole decision lost.
                reportRuleIds: toggleReportRule(
                  current.reportRuleIds,
                  ruleId,
                  reportRuleIdsMax,
                ),
                reportRequirementTouched: true,
              }))}
            options={reportRuleOptions}
          />
          <textarea
            className="mt-1 w-full resize-none rounded-md border border-border bg-card px-2.5 py-1.5 t-meta text-foreground placeholder:text-muted-foreground/70 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            id="agent-plan-report-guidance"
            maxLength={reportGuidanceMaxChars}
            onChange={(event) =>
              updateDraft((current) => ({
                ...current,
                reportGuidance: event.target.value,
                // Touching it here is what makes the field travel —
                // including an emptied one, which is how a requirement
                // is deliberately cleared at the gate.
                reportRequirementTouched: true,
              }))}
            placeholder={t.agent.plan.reportGuidancePlaceholder}
            rows={2}
            value={reportGuidance}
          />
        </div>
      )}
      {isSynthesis
        && full
        && !editable
        && (decidedGuidance || decidedRuleLabels.length > 0) && (
        <div className="mt-1.5 pl-6">
          <p className="t-hint text-muted-foreground/80">
            {t.agent.plan.reportGuidanceEffective}
          </p>
          {decidedRuleLabels.length > 0 && (
            <p className="mt-0.5 t-meta text-muted-foreground">
              {decidedRuleLabels.map((label) => `@rules:${label}`).join(' · ')}
            </p>
          )}
          <p className="mt-0.5 whitespace-pre-line break-words t-meta text-foreground/85">
            {decidedGuidance}
          </p>
        </div>
      )}
      {liveState?.status === 'failed' && liveState.error && (
        <p className="mt-0.5 pl-6 t-meta-sm text-destructive">
          {t.agent.timeline.taskFailed.replace('{error}', liveState.error)}
        </p>
      )}
      {status === 'insufficient_evidence' && (
        <p className="mt-0.5 pl-6 t-meta-sm text-warning">
          {t.agent.task.statusInsufficientEvidence}
        </p>
      )}
    </li>
  )
}

export type ReportRuleOption = {
  ruleId: string
  label: string
  title: string
  /** A rule in force: the revision the run is frozen at. */
  frozenRevision?: number
}

/**
 * Attaching a saved requirement instead of retyping it.
 *
 * Ids only leave the client: the server resolves label, revision and
 * text from the deciding caller's own library, so an attachment can
 * never smuggle text into the writing prompts. Rules appear here only
 * when their owner marked them visible for the agent — a rule written
 * for chat should not start steering reports by itself.
 */
function ReportRulePicker({
  attached,
  max,
  onToggle,
  options,
}: {
  attached: string[]
  /** The server's cap; null = not published, so not pre-checked. */
  max: number | null
  onToggle: (ruleId: string) => void
  options: ReportRuleOption[]
}) {
  const { t } = useLocale()
  const chipTitleLabels = {
    attach: t.agent.plan.reportRuleAttach,
    detach: t.agent.plan.reportRuleDetach,
    frozen: (revision: number) =>
      t.agent.plan.reportRuleFrozen.replace('{revision}', String(revision)),
  }
  // Render the ATTACHMENTS first, then the offers — an attached rule
  // that has left the catalog (its agent visibility switched off, or the
  // rule deleted) must stay visible and detachable. Rendering `options`
  // alone hid it while it kept shaping the report, and — once the draft
  // survived a reload — left a stored id that no control could remove,
  // so every approve failed with a 400 naming an id the UI never showed.
  const attachedRows = attached.map((ruleId) => ({
    option: options.find((item) => item.ruleId === ruleId)
      ?? { ruleId, label: ruleId, title: t.agent.plan.reportRuleUnknown },
    known: options.some((item) => item.ruleId === ruleId),
  }))
  const offered = options.filter((item) => !attached.includes(item.ruleId))
  if (attachedRows.length === 0 && offered.length === 0) return null
  // At the cap the remaining rules are visibly out of reach, so the
  // limit is legible BEFORE the click — instead of a click that quietly
  // does nothing. Without a published cap the server checks the count.
  const atCap = max !== null && attached.length >= max
  const capText = max === null ? '' : t.agent.plan.reportRulesCap.replace('{max}', String(max))
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1">
      {attachedRows.map(({ known, option }) => (
        <button
          aria-pressed
          className={cn(
            'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 t-hint transition-colors',
            known
              ? 'border-brand/50 bg-brand/10 text-brand'
              : 'border-warning/50 bg-warning/10 text-warning',
          )}
          key={option.ruleId}
          onClick={() => onToggle(option.ruleId)}
          title={known
            ? gateRuleChipTitle(option, true, chipTitleLabels)
            : option.label}
          type="button"
        >
          <BookOpen className="icon-xs" />
          {option.title}
        </button>
      ))}
      {offered.map((option) => {
        const active = false
        const blocked = !active && atCap
        return (
          <button
            aria-pressed={active}
            className={cn(
              'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 t-hint transition-colors',
              active
                ? 'border-brand/50 bg-brand/10 text-brand'
                : 'border-border text-muted-foreground hover:text-foreground',
              blocked && 'cursor-not-allowed opacity-40 hover:text-muted-foreground',
            )}
            disabled={blocked}
            key={option.ruleId}
            onClick={() => onToggle(option.ruleId)}
            title={
              blocked
                ? capText
                : gateRuleChipTitle(option, false, chipTitleLabels)
            }
            type="button"
          >
            <BookOpen className="icon-xs" />
            {option.title}
          </button>
        )
      })}
      <span className="t-hint text-muted-foreground/70">
        {atCap
          ? capText
          : max === null
            ? `${t.agent.plan.reportRulesHint} · ${t.agent.plan.reportRulesCapUnchecked}`
            : t.agent.plan.reportRulesHint}
      </span>
    </div>
  )
}

/**
 * Editable query rows: one input per literal query with remove, plus an
 * add row up to the backend's per-task cap. Replaces the earlier
 * single-field editor that silently collapsed ``queries`` to its first
 * entry.
 */
function QueryEditor({
  full,
  patchTask,
  t,
  task,
}: {
  full: boolean
  patchTask: (patch: Partial<AgentPlanTaskDraft>) => void
  t: ReturnType<typeof useLocale>['t']
  task: AgentPlanTaskDraft
}) {
  const queries = task.queries.length > 0 ? task.queries : ['']
  return (
    <div className="mt-1.5 space-y-1 pl-6">
      {queries.map((query, queryIndex) => (
        <div className="flex items-center gap-1" key={queryIndex}>
          <AutosizePlanTextarea
            ariaLabel={`${t.agent.plan.queries} ${queryIndex + 1}`}
            className="min-w-0 flex-1 rounded-sm border-none bg-transparent p-0 t-meta text-foreground outline-none focus-visible:ring-1 focus-visible:ring-brand/40"
            maxRows={full ? undefined : 2}
            onChange={(value) =>
              patchTask({
                queries: queries.map((item, itemIndex) =>
                  itemIndex === queryIndex ? value : item,
                ),
              })}
            value={query}
          />
          {queries.length > 1 && (
            <Button
              aria-label={t.agent.plan.removeQuery}
              className="size-7 shrink-0 text-muted-foreground hover:text-destructive"
              onClick={() =>
                patchTask({
                  queries: queries.filter(
                    (_item, itemIndex) => itemIndex !== queryIndex,
                  ),
                })}
              size="icon"
              type="button"
              variant="ghost"
            >
              <Trash2 className="icon-xs" />
            </Button>
          )}
        </div>
      ))}
      {task.toolKind !== 'web_instant' && queries.length < MAX_TASK_QUERIES && (
        <Button
          className="gap-1 px-2 text-muted-foreground hover:text-foreground"
          onClick={() => patchTask({ queries: [...queries, ''] })}
          size="sm"
          type="button"
          variant="ghost"
        >
          <Plus className="icon-xs" />
          {t.agent.plan.addQuery}
        </Button>
      )}
    </div>
  )
}

function moveTask(
  updateDraft: (update: (draft: AgentPlanDraft) => AgentPlanDraft) => void,
  taskId: string,
  offset: number,
) {
  updateDraft((draft) => {
    const index = draft.tasks.findIndex((task) => task.taskId === taskId)
    const target = index + offset
    if (index === -1 || target < 0 || target >= draft.tasks.length) return draft
    const tasks = [...draft.tasks]
    const [moved] = tasks.splice(index, 1)
    tasks.splice(target, 0, moved)
    return { ...draft, tasks }
  })
}

/**
 * Add one truthful execution unit: instant web, delegated research, or RAG.
 * Knowledge tasks reuse the shared `@`-collection picker so the scope is
 * chosen from the real catalog (chips above the field) instead of typed
 * blind; the chosen ids land in `params.collection_ids` — the same field
 * the backend validator already checks against visible collections.
 */
function AddTaskRow({
  agentTier,
  depth,
  planSource,
  updateDraft,
}: {
  agentTier: string | undefined
  depth: string | undefined
  planSource: PlanSourceInfo
  updateDraft: (update: (draft: AgentPlanDraft) => AgentPlanDraft) => void
}) {
  const { t } = useLocale()
  const researchHintId = useId()
  const inputRef = useRef<HTMLInputElement | null>(null)
  const [adding, setAdding] = useState<
    null | 'rag_query' | 'web_instant' | 'web_research'
  >(null)
  const [query, setQuery] = useState('')
  const [collectionIds, setCollectionIds] = useState<string[]>([])
  const [mention, setMention] = useState<CollectionMentionState | null>(null)
  const [mentionIndex, setMentionIndex] = useState(0)

  const selectedCollections = planSource.collections.filter((collection) =>
    collectionIds.includes(collection.id))
  const mentionCandidates = mention
    ? planSource.collections.filter(
      (collection) =>
        !collectionIds.includes(collection.id)
        && collection.title.toLowerCase().includes(mention.query.toLowerCase()),
    )
    : []
  const mentionOptions: MentionMenuOption[] = mentionCandidates.map(
    (collection) => ({
      group: t.knowledge.collectionGroup,
      icon: Database,
      isCategory: false,
      primary: collection.title,
      secondary: t.knowledge.collectionMenuHandle,
      tone: 'brand',
    }),
  )

  const reset = () => {
    setAdding(null)
    setQuery('')
    setCollectionIds([])
    setMention(null)
  }

  const commit = () => {
    const trimmed = query.trim()
    if (!adding || !trimmed) {
      reset()
      return
    }
    const kind = adding
    const task = buildUserPlanTask({
      collectionIds: kind === 'rag_query' ? collectionIds : [],
      depth,
      kind,
      taskId: `t_user_${Date.now().toString(36)}`,
      text: trimmed,
      tier: agentTier,
    })
    updateDraft((draft) => withUserPlanTask(draft, task))
    reset()
  }

  const updateMentionFromInput = (input: HTMLInputElement) => {
    if (adding !== 'rag_query') return
    setMention(
      detectCollectionMention(
        input.value,
        input.selectionStart ?? input.value.length,
      ),
    )
    setMentionIndex(0)
  }

  const selectMentionOption = (index: number) => {
    const collection = mentionCandidates[index]
    if (!collection || !mention) return
    const end = mention.start + 1 + mention.query.length
    setCollectionIds((current) => [...current, collection.id])
    setQuery((current) => `${current.slice(0, mention.start)}${current.slice(end)}`)
    setMention(null)
    window.requestAnimationFrame(() => {
      const input = inputRef.current
      if (!input) return
      input.focus()
      input.setSelectionRange(mention.start, mention.start)
    })
  }

  if (!adding) {
    return (
      <div className="space-y-1.5">
        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            className="gap-1 text-muted-foreground hover:text-foreground"
            onClick={() => setAdding('web_instant')}
            size="sm"
            type="button"
            variant="outline"
          >
            <Plus />
            <Zap />
            {t.agent.task.addInstant}
          </Button>
          <Button
            aria-describedby={researchHintId}
            className="gap-1 text-muted-foreground hover:text-foreground"
            onClick={() => setAdding('web_research')}
            size="sm"
            type="button"
            variant="ghost"
          >
            <Plus />
            <Globe2 />
            {t.agent.task.addResearch}
          </Button>
          <Button
            className="gap-1 text-muted-foreground hover:text-foreground"
            onClick={() => setAdding('rag_query')}
            size="sm"
            type="button"
            variant="ghost"
          >
            <Plus />
            <BookOpen />
            {t.agent.task.addKnowledge}
          </Button>
        </div>
        <p className="t-hint text-muted-foreground" id={researchHintId}>
          {t.agent.task.addResearchHint}
        </p>
      </div>
    )
  }
  return (
    <div
      className="relative space-y-1.5"
      onBlur={(event) => {
        // Commit only when focus truly LEAVES the add-row: chip removal and
        // mention-menu clicks move focus within it (or prevent the blur).
        if (event.currentTarget.contains(event.relatedTarget as Node | null)) {
          return
        }
        commit()
      }}
    >
      {mention && (
        <MentionMenu
          activeIndex={mentionIndex}
          labels={{
            backHint: t.chat.mentionBackHint,
            closeHint: t.chat.mentionCloseHint,
            filterPlaceholder: t.chat.mentionFilterPlaceholder,
            navHint: t.chat.mentionNavHint,
            rootTitle: t.knowledge.collectionPickerTitle,
            selectHint: t.chat.mentionSelectHint,
          }}
          onHover={setMentionIndex}
          onSelect={selectMentionOption}
          options={mentionOptions.length > 0
            ? mentionOptions
            : [{
              // A state without an action: picking it selects nothing.
              disabled: true,
              group: undefined,
              icon: Database,
              isCategory: false,
              state: true,
              primary: t.knowledge.noCollectionMatches,
              secondary: t.knowledge.collectionPickerHint,
              tone: 'brand',
            }]}
          scope={{
            icon: Database,
            kind: t.knowledge.collections,
            query: mention.query,
            tone: 'brand',
          }}
        />
      )}
      {selectedCollections.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5">
          {selectedCollections.map((collection) => (
            <Chip
              active
              aria-label={`${t.knowledge.removeCollection}: ${collection.title}`}
              dot="bg-brand"
              key={collection.id}
              onClick={() => {
                setCollectionIds((current) =>
                  current.filter((id) => id !== collection.id))
                inputRef.current?.focus()
              }}
              title={t.knowledge.removeCollection}
            >
              {collection.title}
            </Chip>
          ))}
        </div>
      )}
      <input
        autoFocus
        className="w-full rounded-md border border-border bg-card px-2 py-1 t-meta text-foreground outline-none focus-visible:ring-1 focus-visible:ring-brand/40"
        onChange={(event) => {
          setQuery(event.target.value)
          updateMentionFromInput(event.currentTarget)
        }}
        onKeyDown={(event) => {
          if (mention && mentionOptions.length > 0 && mentionCandidates.length > 0) {
            if (event.key === 'ArrowDown') {
              event.preventDefault()
              setMentionIndex((current) => (current + 1) % mentionOptions.length)
              return
            }
            if (event.key === 'ArrowUp') {
              event.preventDefault()
              setMentionIndex(
                (current) =>
                  (current - 1 + mentionOptions.length) % mentionOptions.length,
              )
              return
            }
            if (event.key === 'Enter' || event.key === 'Tab') {
              event.preventDefault()
              selectMentionOption(mentionIndex)
              return
            }
          }
          if (
            mention
            && mentionCandidates.length === 0
            && event.key === 'Enter'
          ) {
            // "No matches" open: a stray Enter must not commit the
            // unresolved @text — close the menu; the next Enter commits.
            event.preventDefault()
            setMention(null)
            return
          }
          if (mention && event.key === 'Escape') {
            event.preventDefault()
            setMention(null)
            return
          }
          if (event.key === 'Enter') commit()
          if (event.key === 'Escape') reset()
        }}
        onSelect={(event) => updateMentionFromInput(event.currentTarget)}
        placeholder={
          adding === 'rag_query'
            ? t.agent.task.addKnowledgePlaceholder
            : adding === 'web_research'
              ? t.agent.task.addResearchPlaceholder
              : t.agent.task.addInstantPlaceholder
        }
        ref={inputRef}
        value={query}
      />
    </div>
  )
}

function AutosizePlanTextarea({
  ariaLabel,
  className,
  maxRows,
  onChange,
  value,
}: {
  ariaLabel: string
  className: string
  maxRows?: number
  onChange: (value: string) => void
  value: string
}) {
  const ref = useRef<HTMLTextAreaElement | null>(null)
  const resize = useCallback(() => {
    resizeTextareaToRows(ref.current, maxRows)
  }, [maxRows])
  useLayoutEffect(() => {
    resize()
  }, [resize, value])
  // Height depends on width: the first measurement can run while the
  // canvas is still animating in (width ~0 — the text wraps per character
  // and a huge scrollHeight gets stamped as fixed height). Re-measure on
  // every WIDTH change only, so the height write cannot loop the observer.
  useLayoutEffect(() => {
    const element = ref.current
    if (!element || typeof ResizeObserver === 'undefined') return
    let lastWidth = element.clientWidth
    const observer = new ResizeObserver(() => {
      if (element.clientWidth === lastWidth) return
      lastWidth = element.clientWidth
      resize()
    })
    observer.observe(element)
    return () => observer.disconnect()
  }, [resize])
  return (
    <textarea
      aria-label={ariaLabel}
      className={cn('resize-none overflow-hidden', className)}
      onChange={(event) => onChange(event.target.value)}
      onInput={resize}
      ref={ref}
      rows={1}
      value={value}
    />
  )
}

/** Human Suchtiefe labels: the option states its BUDGET, never a bare
 * token — "compact" alone reads like jargon at the approval gate. */
function profileOptionLabel(
  toolKind: AgentPlanTaskRecord['toolKind'],
  option: string,
  t: TranslationDictionary,
): string {
  if (toolKind === 'web_research') {
    const labels = t.agent.plan.webProfileLabels as Record<string, string>
    return labels[option] ?? option
  }
  const labels = t.agent.plan.ragProfileLabels as Record<string, string>
  return labels[option] ?? option
}

