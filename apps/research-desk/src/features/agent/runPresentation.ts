import type { TranslationDictionary } from '@/i18n/translations'
import { countLabel } from '@/i18n/plural'
import { formatDuration } from '@/lib/time'
import { effortLevelLabel } from '@/lib/modelCard'

import type {
  AgentArtifactRecord,
  AgentClaimMark,
  AgentRunOutcome,
  AgentRunRecord,
  AgentStepEntry,
} from './model'
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
    ).replace('{duration}', () => formatDuration(age))
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
  >,
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
  }
}

/**
 * What a run calls its Stufe, in the header of its question and in its
 * settings line: the recorded Stufe first. A run without one that ran with
 * the legacy deep depth says "Deep" — that depth had no budgets of a Stufe,
 * so "Tief" would claim budgets the run never had. Otherwise nothing.
 * `deep` marks the deep end of the ladder for the header's accent.
 */
export function runTierBadge(
  run: Pick<AgentRunRecord, 'agentTier' | 'depth'>,
  t: TranslationDictionary,
): { deep: boolean; label: string } | null {
  const tier = agentTierIdOf(run.agentTier)
  if (tier) return { deep: tier === 'tief', label: tierLabel(tier, t) }
  return run.depth === 'deep' ? { deep: true, label: t.agent.composer.deep } : null
}

/**
 * The conditions a finished turn ran under, as one readable line: engine ·
 * permission · Stufe · model · effort · web · knowledge. Every part is a
 * recorded fact — the accepted run's execution snapshot or the request the
 * server admitted — never a guess: without a snapshot only permission and
 * Stufe remain, without a model there is no model part, and a source that
 * was available but unused reads as "0 searches" rather than vanishing.
 * The engine part names the kernel only: a stored run of the retired
 * mission engine says nothing about an engine the desk no longer offers.
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
  if (execution?.effectiveMode === 'agent_kernel') parts.push(engineModeLabel(t))
  if (run.autonomy) parts.push(autonomyModeLabel(run.autonomy, t))
  const tier = runTierBadge(run, t)
  if (tier) parts.push(tier.label)
  const model = execution?.model ?? run.modelResolution?.model
  if (model) parts.push(model)
  const effort = execution?.reasoningEffort ?? run.modelResolution?.effort
  if (effort) {
    parts.push(
      t.agent.timeline.recapEffort.replace('{level}', () => effortLevelLabel(effort)),
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
      knowledgeSearchTotal(execution).searches,
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
 * The web searches of a run as its surfaces count them: the research the
 * run delegated and the searches run for it. A delegation is ONE web call
 * of the parent and no search; its searches are the ones the child's
 * provider executed, from the child's typed snapshot. A child that records
 * no searches (a mission, a role, a research that has not searched yet)
 * adds nothing — never a guess. The settings line and the status menu both
 * count here, so they cannot disagree.
 */
export function webSearchTotals(
  execution: Pick<AgentExecutionSnapshot, 'toolUseCounts' | 'webSearches'>,
  children: AgentRunRecord['children'] | undefined,
): { research: number; searches: number } {
  const kids = Object.values(children ?? {})
  if (execution.webSearches === null) {
    // A snapshot from before the count: its counter of source uses and
    // the children's planned queries are all it recorded.
    return {
      research: 0,
      searches: execution.toolUseCounts.web + kids.reduce(
        (total, child) => total + Math.max(0, child.snapshot?.total_queries ?? 0),
        0,
      ),
    }
  }
  const research = kids.filter(
    (child) => typeof child.snapshot?.executed_queries === 'number',
  )
  return {
    research: research.length,
    searches: execution.webSearches + research.reduce(
      (total, child) => total + Math.max(0, child.snapshot?.executed_queries ?? 0),
      0,
    ),
  }
}

/**
 * The searches of the knowledge base a run ran. The counter of source uses
 * takes a document read like a search, so it is no count of them; the run
 * reports its searches beside it. `recorded` is false for a snapshot that
 * carries no such count (a mission, a run from before the count): its
 * counter of source uses is then all there is.
 */
export function knowledgeSearchTotal(
  execution: Pick<AgentExecutionSnapshot, 'knowledgeSearches' | 'toolUseCounts'>,
): { recorded: boolean; searches: number } {
  return execution.knowledgeSearches === null
    ? { recorded: false, searches: execution.toolUseCounts.knowledge }
    : { recorded: true, searches: execution.knowledgeSearches }
}

/** The web part of the settings line (:func:`webSearchTotals` in its words). */
function webSearchesLabel(
  execution: Pick<AgentExecutionSnapshot, 'toolUseCounts' | 'webSearches'>,
  children: AgentRunRecord['children'] | undefined,
  t: TranslationDictionary,
): string {
  const { research, searches } = webSearchTotals(execution, children)
  if (research === 0) {
    return countLabel(
      searches,
      t.agent.timeline.recapWebSearchOne,
      t.agent.timeline.recapWebSearches,
    )
  }
  return t.agent.timeline.recapWebResearch
    .replace('{research}', () => countLabel(
      research,
      t.agent.timeline.recapResearchOne,
      t.agent.timeline.recapResearches,
    ))
    .replace('{searches}', () => countLabel(
      searches,
      t.agent.timeline.recapSearchOne,
      t.agent.timeline.recapSearches,
    ))
}

/**
 * One "used" row of the run overview: the value of its pill and, where the
 * pill cannot hold it, one line under the row.
 */
export type SourceUseRow = { hint?: string; value: string }

/**
 * The overview's "web used" row, or `null` where the run did not use the
 * web. It counts where the settings line counts (:func:`webSearchTotals`),
 * so a delegated research never reads as one search: the pill holds the
 * searches and the research stands on the line under it. The pill is
 * narrow; both counts in it would be cut at two-digit numbers, and in
 * English already at one research.
 */
export function webUseRow(
  execution: Pick<AgentExecutionSnapshot, 'toolUseCounts' | 'webSearches'>,
  children: AgentRunRecord['children'] | undefined,
  t: TranslationDictionary,
): SourceUseRow | null {
  const { research, searches } = webSearchTotals(execution, children)
  if (research === 0 && searches === 0 && execution.toolUseCounts.web === 0) {
    return null
  }
  const o = t.agent.overview
  const value = countLabel(searches, o.toolUseCountOne, o.toolUseCount)
  if (research === 0) return { value }
  return {
    hint: o.toolUseResearchHint.replace('{research}', () => countLabel(
      research,
      t.agent.timeline.recapResearchOne,
      t.agent.timeline.recapResearches,
    )),
    value,
  }
}

/**
 * The overview's "knowledge used" row, or `null` where the run did not use
 * the knowledge base (:func:`knowledgeSearchTotal`). A run that used it and
 * searched nothing itself says exactly that: it read documents, or its
 * subtasks asked the project knowledge, and a row of "0 searches" under
 * "used" would read as a contradiction.
 */
export function knowledgeUseRow(
  execution: Pick<AgentExecutionSnapshot, 'knowledgeSearches' | 'toolUseCounts'>,
  t: TranslationDictionary,
): SourceUseRow | null {
  const { recorded, searches } = knowledgeSearchTotal(execution)
  const o = t.agent.overview
  if (searches > 0) {
    return { value: countLabel(searches, o.toolUseCountOne, o.toolUseCount) }
  }
  return recorded && execution.toolUseCounts.knowledge > 0
    ? { value: o.toolKnowledgeNoOwnSearch }
    : null
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
