import type { TranslationDictionary } from '@/i18n/translations'
import { countLabel } from '@/i18n/plural'
import { formatDuration } from '@/lib/time'
import { effortLevelLabel } from '@/lib/modelCard'

import type {
  AgentArtifactRecord,
  AgentClaimMark,
  AgentPlanTaskRecord,
  AgentRunOutcome,
  AgentRunRecord,
  AgentStepEntry,
} from './model'
import { effectiveAgentTaskStatus } from './plan/taskPresentation'
import {
  normalizeAgentExecutionSnapshot,
  type AgentExecutionSnapshot,
} from './executionPolicy'
import {
  agentTierIdOf,
  autonomyModeLabel,
  engineModeLabel,
  tierLabel,
} from './runSetupLabels'

/**
 * The review states whose record a reader is shown: a review that ran, and
 * one that was owed and gave no usable verdict. A Stufe that waives the
 * review and a run that ended without one owe the reader nothing, so
 * their records stay quiet on every surface.
 */
const SHOWN_REVIEWS: ReadonlySet<string> = new Set(['completed', 'unavailable'])

/** *outcome* when a surface shows it, else undefined. */
export function shownOutcome<T extends { review: string }>(
  outcome: T | undefined,
): T | undefined {
  return outcome && SHOWN_REVIEWS.has(outcome.review) ? outcome : undefined
}

/** The outcome shown under a text the run delivered — the chat answer or
 * a document canvas: the run's record, unless the user edited that text
 * since — the record describes the text the run delivered, not the
 * user's edit. */
export function documentOutcome<T extends { review: string }>(
  outcome: T | undefined,
  artifact: Pick<AgentArtifactRecord, 'updatedBy'>,
): T | undefined {
  return artifact.updatedBy === 'user' ? undefined : shownOutcome(outcome)
}

/** The marks of a shown outcome (`documentOutcome`) that belong to
 * *artifact*'s text: the chat answer's on the answer, a canvas's on that
 * canvas. A record names the answer `chat`, never by its artifact id. */
export function artifactClaimMarks(
  outcome: Pick<AgentRunOutcome, 'marks'> | undefined,
  artifact: Pick<AgentArtifactRecord, 'artifactId' | 'kind'>,
): AgentClaimMark[] {
  if (!outcome) return []
  const outputId = artifact.kind === 'answer' ? 'chat' : artifact.artifactId
  return outcome.marks.filter((mark) => mark.outputId === outputId)
}

/** Seconds of silence before the run line names the age of the newest
 * update: a busy run stays calm, a quiet one says how quiet. */
export const RUN_CLOCK_QUIET_SECONDS = 20
/** Without any new update for this long the line says so plainly — a
 * neutral state, never a cancel. */
export const RUN_CLOCK_STALE_SECONDS = 60

/**
 * What the run line's clock shows at *now* (server seconds): the elapsed
 * time since the server's run start and, after a quiet stretch, the age of
 * the newest update — its own events and its children's. While the run
 * waits for the user, the age is not counted against it. `null` when no
 * start is known yet.
 */
export function runClockParts(
  {
    lastProgressAt,
    now,
    startedAt,
    waiting,
  }: {
    lastProgressAt: number | undefined
    now: number
    startedAt: string | undefined
    waiting: boolean
  },
  t: TranslationDictionary,
): { elapsed: string; note: string } | null {
  if (!startedAt) return null
  const elapsed = Math.floor(now - Date.parse(startedAt) / 1000)
  if (!Number.isFinite(elapsed)) return null
  const age = lastProgressAt === undefined
    ? undefined
    : Math.max(0, Math.floor(now - lastProgressAt))
  const note = waiting || age === undefined || age < RUN_CLOCK_QUIET_SECONDS
    ? ''
    : (age >= RUN_CLOCK_STALE_SECONDS
      ? t.agent.timeline.noNewProgress
      : t.agent.timeline.lastProgressAgo
    ).replace('{duration}', formatDuration(age))
  return { elapsed: formatDuration(Math.max(0, elapsed)), note }
}

/** The narration slot of the kernel's review pass: one deterministic id per
 * emission site on the server (`agents/kernel/algorithm.py`). Open while the
 * review or its revision runs; its closing text is the outcome's summary. */
export const KERNEL_REVIEW_NARRATION_ID = 'kernel_deep_review'

/** Whether the answer block states the run's outcome itself — under a ready
 * answer the run delivered (`RunOutcomeLine`). Then that is the one place
 * the reader finds the review's sentence; the recap and the review's
 * closing narration stay quiet instead of saying it again. */
export function answerStatesOutcome(
  run: Pick<AgentRunRecord, 'artifactOrder' | 'artifacts' | 'outcome'>,
): boolean {
  const answerId = run.artifactOrder.find(
    (artifactId) => run.artifacts[artifactId]?.kind === 'answer',
  )
  const answer = answerId ? run.artifacts[answerId] : undefined
  return Boolean(
    answer && answer.status === 'ready' && documentOutcome(run.outcome, answer),
  )
}

/**
 * What the recap says about the run's review: the record's own summary;
 * `missing` for a completed agent run whose read stream holds no record
 * (it reads "Ohne Ausgang"); `none` when there is nothing to say — no
 * review was owed, or the run's stream has not been read yet.
 */
export type AgentRecapOutcome = { summary: string } | 'missing' | 'none'

export type AgentRunCompletionRecap = {
  elapsedSeconds: number | undefined
  referenceCount: number
  outcome: AgentRecapOutcome
  synthesized: boolean
  taskCount: number
  tasksCompleted: number
}

/** Add only runs observed before the authoritative listing settled to the
 * history baseline. Runs first observed afterwards are genuinely new turns. */
export function retainHydratedAgentRunIds(
  current: ReadonlySet<string>,
  observedRunIds: Iterable<string>,
  runsHydrated: boolean,
): ReadonlySet<string> {
  if (runsHydrated) return current
  const next = new Set(current)
  for (const runId of observedRunIds) next.add(runId)
  return next
}

/** A narration types only when its exact event arrived live. Legacy/demo
 * entries may animate on a new run, but never when remounted as history. */
export function shouldAnimateAgentNarration({
  entry,
  historicalRun,
  isLatest,
}: {
  entry: Pick<AgentStepEntry, 'arrivedLive'>
  historicalRun: boolean
  isLatest: boolean
}): boolean {
  if (!isLatest || entry.arrivedLive === false) return false
  return entry.arrivedLive === true || !historicalRun
}

/** Evidence-backed facts for a completed run's compact return-state. */
export function agentRunCompletionRecap(
  run: Pick<
    AgentRunRecord,
    | 'elapsedSeconds'
    | 'kind'
    | 'lastSequence'
    | 'outcome'
    | 'status'
    | 'stepLog'
    | 'taskStates'
  >,
  tasks: readonly AgentPlanTaskRecord[],
  memo: AgentArtifactRecord | undefined,
): AgentRunCompletionRecap {
  // The words are the record's: both engines send one, and a phase or an
  // activity says nothing about how the review ended. A run hydrated
  // from its summary alone has read none of its events yet, and its
  // record may still be on the way.
  const shown = shownOutcome(run.outcome)
  const outcome: AgentRecapOutcome = shown
    ? { summary: shown.summary }
    : run.outcome === undefined
      && run.kind === 'agent'
      && run.status === 'completed'
      && run.lastSequence > 0
      ? 'missing'
      : 'none'
  return {
    elapsedSeconds: run.elapsedSeconds,
    referenceCount: memo?.refsCount ?? 0,
    outcome,
    synthesized: Boolean(memo) || run.stepLog.some(
      (entry) => entry.kind === 'phase' && entry.phase === 'synthesis',
    ),
    taskCount: tasks.length,
    tasksCompleted: tasks.filter(
      (task) => effectiveAgentTaskStatus(
        task,
        run.taskStates[task.taskId],
      ) === 'completed',
    ).length,
  }
}

/**
 * The conditions a finished turn ran under, as one readable line: engine ·
 * permission · Stufe · model · effort · web · knowledge. Every part is a
 * recorded fact — the accepted run's execution snapshot or the request the
 * server admitted — never a guess: without a snapshot only permission and
 * Stufe remain, without a model there is no model part, and a source that
 * was available but unused reads as "0 searches" rather than vanishing.
 */
export function runSettingsSummary(
  run: Pick<
    AgentRunRecord,
    'agentTier' | 'autonomy' | 'depth' | 'modelResolution' | 'snapshot'
  > & Partial<Pick<AgentRunRecord, 'children'>>,
  t: TranslationDictionary,
): string[] {
  const execution = normalizeAgentExecutionSnapshot(run.snapshot)
  const parts: string[] = []
  const mode = execution?.effectiveMode
  if (mode === 'agent_kernel' || mode === 'workspace_agent') {
    parts.push(engineModeLabel(mode, t))
  }
  if (run.autonomy) parts.push(autonomyModeLabel(run.autonomy, t))
  const tier = agentTierIdOf(run.agentTier)
  if (tier) parts.push(tierLabel(tier, t))
  else if (run.depth === 'deep') parts.push(t.agent.composer.deep)
  const model = execution?.model ?? run.modelResolution?.model
  if (model) parts.push(model)
  const effort = execution?.reasoningEffort ?? run.modelResolution?.effort
  if (effort) {
    parts.push(
      t.agent.timeline.recapEffort.replace('{level}', effortLevelLabel(effort)),
    )
  }
  if (!execution) return parts
  parts.push(
    execution.sourcePolicy.web === 'available'
      ? webSearchesLabel(execution, run.children, t)
      : t.agent.timeline.recapWebOff,
  )
  if (execution.sourcePolicy.knowledge === 'available') {
    const searches = countLabel(
      execution.toolUseCounts.knowledge,
      t.agent.timeline.recapKnowledgeSearchOne,
      t.agent.timeline.recapKnowledgeSearches,
    )
    const scope = knowledgeScopeLabel(execution, t)
    parts.push(scope ? `${searches} (${scope})` : searches)
  } else {
    parts.push(t.agent.timeline.recapKnowledgeOff)
  }
  return parts
}

/**
 * The web part of the line: the searches the provider ran, beside the
 * research the run delegated. A delegation is ONE web call of the parent
 * and no search; its searches are the ones the child's provider executed,
 * from the child's typed snapshot. A child that records no searches (a
 * mission, a role, a research that has not searched yet) adds nothing —
 * never a guess.
 */
function webSearchesLabel(
  execution: Pick<AgentExecutionSnapshot, 'toolUseCounts' | 'webSearches'>,
  children: AgentRunRecord['children'] | undefined,
  t: TranslationDictionary,
): string {
  const kids = Object.values(children ?? {})
  if (execution.webSearches === null) {
    // A snapshot from before the count: its budget counter and the
    // children's planned queries are all it recorded.
    return countLabel(
      execution.toolUseCounts.web + kids.reduce(
        (total, child) => total + Math.max(0, child.snapshot?.total_queries ?? 0),
        0,
      ),
      t.agent.timeline.recapWebSearchOne,
      t.agent.timeline.recapWebSearches,
    )
  }
  const research = kids.filter(
    (child) => typeof child.snapshot?.executed_queries === 'number',
  )
  const searches = execution.webSearches + research.reduce(
    (total, child) => total + Math.max(0, child.snapshot?.executed_queries ?? 0),
    0,
  )
  if (research.length === 0) {
    return countLabel(
      searches,
      t.agent.timeline.recapWebSearchOne,
      t.agent.timeline.recapWebSearches,
    )
  }
  return t.agent.timeline.recapWebResearch
    .replace('{research}', countLabel(
      research.length,
      t.agent.timeline.recapResearchOne,
      t.agent.timeline.recapResearches,
    ))
    .replace('{searches}', countLabel(
      searches,
      t.agent.timeline.recapSearchOne,
      t.agent.timeline.recapSearches,
    ))
}

/** The boundary a knowledge search ran against — only when the server
 * reported it: the session switch (every visible collection) or the
 * attached chips with their count. An inherited or unreported scope adds
 * nothing rather than a guess. */
function knowledgeScopeLabel(
  execution: Pick<
    AgentExecutionSnapshot,
    'knowledgeCollectionCount' | 'knowledgeScope'
  >,
  t: TranslationDictionary,
): string {
  if (execution.knowledgeScope === 'all') {
    return t.agent.timeline.recapKnowledgeScopeAll
  }
  if (execution.knowledgeScope === 'pinned' && execution.knowledgeCollectionCount > 0) {
    return countLabel(
      execution.knowledgeCollectionCount,
      t.agent.timeline.recapKnowledgeScopePinnedOne,
      t.agent.timeline.recapKnowledgeScopePinned,
    )
  }
  return ''
}

/**
 * How long ago the task list was reported, when the run has moved on
 * since — else `null`.
 *
 * The list only changes when the model calls `write_todos`. A delegation
 * is ONE tool call that can own the run for tens of minutes, so a list
 * written before it kept naming a finished step as the current one: in
 * the run that motivated this, "structuring the assignment" stood as
 * in-progress for fifty minutes while a whole mission ran beneath it.
 *
 * The surface does not guess whether the list is still true. It states
 * the fact — when it was last reported — and lets the reader judge.
 * Nothing newer than the list means nothing to add.
 */
export function agentTodoReportAge(
  todosAt: number | undefined,
  stepLog: readonly Pick<AgentStepEntry, 'at'>[],
  now: number,
  childActivityAt = 0,
): number | null {
  if (!todosAt) return null
  const newestStep = stepLog.reduce(
    (latest, entry) => (entry.at > latest ? entry.at : latest),
    0,
  )
  // A delegation adds no step rows for minutes while its child works;
  // the child's projections are that work, so the list is stale then too.
  if (Math.max(newestStep, childActivityAt) <= todosAt) return null
  return Math.max(0, Math.round(now - todosAt))
}

/** Newest `child.progress` arrival across a run's delegated children. */
export function latestChildActivity(
  children: Readonly<Record<string, { updatedAt?: number }>>,
): number {
  return Object.values(children).reduce(
    (latest, child) => Math.max(latest, child.updatedAt ?? 0),
    0,
  )
}
