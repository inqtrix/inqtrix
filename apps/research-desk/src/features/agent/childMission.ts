import { formatDurationMsShort } from '@/lib/time'
import type { TranslationDictionary } from '../../i18n/translations'
import { countLabel } from '@/i18n/plural'
import type { AgentChildProgressRecord } from './model'
import { shownOutcome } from './runPresentation'

/**
 * Run-scoped activity kinds a mission's DECISIONS cross under — the
 * mirror of the server's `_CHILD_DECISION_KINDS` (`runs/shared.py`).
 * The reducer keeps the latest one; the card shows it as the line that
 * says why the mission goes on or why it stops researching, until the
 * mission's outcome record says how its review ended.
 */
export const CHILD_DECISION_KINDS = new Set([
  'critic_unavailable',
  'critic_revise',
  'critic_research',
  'critic_research_exhausted',
  'replan_invalid',
])

/** The four stages a reader thinks in, in order. */
export type ChildStage = 'plan' | 'research' | 'memo' | 'check'
export const CHILD_STAGES: readonly ChildStage[] = ['plan', 'research', 'memo', 'check']

/**
 * Mission phases folded to the four stages. A research child's phases
 * (`classify`, `search`, `evaluate`, `answer`) are NOT in this map — that
 * is how a research child (a line, no card) is told apart from a mission.
 */
const STAGE_BY_PHASE: Record<string, ChildStage> = {
  intake: 'plan',
  discovery: 'plan',
  clarification: 'plan',
  planning: 'plan',
  plan_approval: 'plan',
  execution: 'research',
  evidence: 'research',
  synthesis: 'memo',
  critic: 'check',
  patch: 'check',
  done: 'check',
}

export function childStage(phase: string | undefined): ChildStage | undefined {
  return phase ? STAGE_BY_PHASE[phase] : undefined
}

export type ChildTaskRowState = 'done' | 'running' | 'failed'

export type ChildTaskRow = {
  ordinal: number
  /** The plan task's title; empty when an older projection carried none. */
  title: string
  state: ChildTaskRowState
  /** Seconds the task took (settled) or has been running (open). */
  elapsedSeconds?: number
  /** Completed searches of this task, or undefined when the task
   * delegates to a grandchild (`web_research`) whose searches never
   * cross — "0" would read as "searched nothing". */
  searches?: number
  /** Head of the task's result (its title prefix stripped). */
  result?: string
  /** The search this task started most recently, while it runs. */
  query?: string
}

export type ChildStageView = { key: ChildStage; state: 'done' | 'now' | 'todo' }

export type ChildDecisionView = { kind: string; label: string; detail?: string }

export type ChildMissionView = {
  runStatus: string
  /** completed, failed or cancelled — the card folds and stops ticking. */
  terminal: boolean
  brokenOff?: 'childFailed' | 'childCancelled'
  error?: string
  stages: ChildStageView[]
  rows: ChildTaskRow[]
  tasks: number
  done: number
  failed: number
  running: number
  searches: number
  sections: number
  researchRounds: number
  elapsedSeconds: number
  decision?: ChildDecisionView
  /** The review's ending in the server's words: the record's summary when
   * its review ran or was owed, `null` when the record says none was owed
   * (nothing to say), absent while no record arrived. */
  outcome?: string | null
  planLine?: string
  /** The latest search when it could not be joined to a task row. */
  lastQuery?: string
  /** The memo being written: the derived synthesis line (outline
   * announced, section written) while the mission is at the memo or
   * check stage. */
  memoLine?: string
}

/** The child fields the card speaks from. */
export type ChildMissionSource = Pick<
  AgentChildProgressRecord,
  | 'runStatus'
  | 'snapshot'
  | 'error'
  | 'openTasks'
  | 'failedTasks'
  | 'taskTitles'
  | 'taskIds'
  | 'taskStartedAt'
  | 'taskSettledAt'
  | 'taskSearches'
  | 'taskKinds'
  | 'taskResults'
  | 'lastQuery'
  | 'lastQueryTask'
  | 'searchesDone'
  | 'section'
  | 'sectionsDone'
  | 'researchRounds'
  | 'decision'
  | 'outcome'
  | 'planLine'
  | 'startedAt'
  | 'updatedAt'
>

const BROKEN_OFF: Record<string, 'childFailed' | 'childCancelled'> = {
  cancelled: 'childCancelled',
  failed: 'childFailed',
}

/** A task's result narration reads "<title>: <head>"; the row already
 * names the title, so the card shows the head alone — as plain text: the
 * head is the first sentences of a Markdown summary, and "## Kurzfazit
 * **31.08.**" in a one-line row is noise, not structure. */
export function stripTaskTitle(text: string, title: string | undefined): string {
  const trimmed = text.trim()
  const prefix = title?.trim()
  const head = prefix && trimmed.startsWith(`${prefix}: `)
    ? trimmed.slice(prefix.length + 2)
    : trimmed
  return plainHead(head)
}

/** Markdown syntax off a one-line excerpt: heading marks, emphasis,
 * inline code, list bullets — the words stay exactly as written. */
export function plainHead(text: string): string {
  return text
    .replace(/(^|\s)#{1,6}\s+/g, '$1')
    .replace(/\*\*|__/g, '')
    .replace(/(^|[^\w])[*_](?=\S)/g, '$1')
    .replace(/(?<=\S)[*_](?=[^\w]|$)/g, '')
    .replace(/`/g, '')
    .replace(/(^|\s)[-*+]\s+(?=\S)/g, '$1')
    .replace(/\s+/g, ' ')
    .trim()
}

/**
 * A delegated MISSION — task projections or a mission phase arrived. A
 * research child reports neither and keeps the one-line presenter.
 */
export function isMissionChild(child: Partial<ChildMissionSource>): boolean {
  if (Object.keys(child.taskTitles ?? {}).length > 0) return true
  if ((child.openTasks ?? []).length > 0) return true
  return childStage(child.snapshot?.phase) !== undefined
}

/**
 * What the card can honestly show about a delegated mission RIGHT NOW,
 * built only from what the projection carried — no invented rows, no
 * estimated remaining time. `now` is epoch seconds and drives the
 * running clocks; a terminal child uses its last update instead.
 */
export function childMissionView(
  child: ChildMissionSource,
  now: number,
): ChildMissionView | null {
  if (!isMissionChild(child)) return null
  const runStatus = child.runStatus ?? ''
  const terminal = runStatus === 'completed' || runStatus === 'failed' || runStatus === 'cancelled'
  const open = new Set(child.openTasks ?? [])
  const failed = new Set(child.failedTasks ?? [])
  const titles = child.taskTitles ?? {}
  const ordinals = [...new Set([
    ...Object.keys(titles).map(Number),
    ...open,
    ...failed,
  ])].filter((value) => Number.isFinite(value)).sort((left, right) => left - right)
  const rows: ChildTaskRow[] = ordinals.map((ordinal) => {
    const state: ChildTaskRowState = failed.has(ordinal)
      ? 'failed'
      : open.has(ordinal)
        ? 'running'
        : 'done'
    const startedAt = child.taskStartedAt?.[ordinal]
    const settledAt = child.taskSettledAt?.[ordinal]
    const end = state === 'running' ? now : settledAt
    const elapsedSeconds = startedAt !== undefined && end !== undefined
      ? Math.max(0, end - startedAt)
      : undefined
    const query = state === 'running' && child.lastQueryTask === ordinal
      ? child.lastQuery?.trim() || undefined
      : undefined
    const result = child.taskResults?.[ordinal]?.trim() || undefined
    // Only a task that searches ITSELF has a search count the parent can
    // see: a `web_research` task delegates to a grandchild, a synthesis
    // task searches nothing — for both, "0 Suchen" would mislead.
    const kind = child.taskKinds?.[ordinal]
    const searchesItself = kind === 'web_instant' || kind === 'rag_query'
    const searches = searchesItself ? (child.taskSearches?.[ordinal] ?? 0) : undefined
    return {
      ordinal,
      title: titles[ordinal]?.trim() ?? '',
      state,
      ...(elapsedSeconds !== undefined ? { elapsedSeconds } : {}),
      ...(searches !== undefined ? { searches } : {}),
      ...(result ? { result } : {}),
      ...(query ? { query } : {}),
    }
  })
  const current = childStage(child.snapshot?.phase)
  const currentIndex = current ? CHILD_STAGES.indexOf(current) : -1
  const stages: ChildStageView[] = CHILD_STAGES.map((key, index) => ({
    key,
    state: runStatus === 'completed'
      ? 'done'
      : currentIndex < 0
        ? 'todo'
        : index < currentIndex
          ? 'done'
          : index === currentIndex
            ? 'now'
            : 'todo',
  }))
  const end = terminal ? (child.updatedAt ?? now) : now
  const elapsedSeconds = child.startedAt !== undefined ? Math.max(0, end - child.startedAt) : 0
  const joinedQuery = rows.some((row) => row.query !== undefined)
  // A search line belongs to the research stage: once the memo is being
  // written, the last query is history and would make the sum line read
  // as if the writer were still searching.
  const searching = current === undefined || current === 'plan' || current === 'research'
  const lastQuery = !terminal && searching && !joinedQuery
    ? child.lastQuery?.trim() || undefined
    : undefined
  const memoLine = !terminal && (current === 'memo' || current === 'check')
    ? child.section?.trim() || undefined
    : undefined
  // Once the record says how the review ended, its summary is the line;
  // the round decisions before it were steps on the way there.
  const outcome = child.outcome
    ? shownOutcome(child.outcome)?.summary ?? null
    : undefined
  const decision = outcome
    ? { kind: 'outcome', label: outcome }
    : child.decision
      ? {
        kind: child.decision.kind,
        label: child.decision.label,
        ...(child.decision.detail ? { detail: child.decision.detail } : {}),
      }
      : undefined
  return {
    runStatus,
    terminal,
    ...(BROKEN_OFF[runStatus] ? { brokenOff: BROKEN_OFF[runStatus] } : {}),
    ...(child.error?.trim() ? { error: child.error.trim() } : {}),
    stages,
    rows,
    tasks: rows.length,
    done: rows.filter((row) => row.state === 'done').length,
    failed: rows.filter((row) => row.state === 'failed').length,
    running: rows.filter((row) => row.state === 'running').length,
    searches: child.searchesDone ?? 0,
    sections: child.sectionsDone ?? 0,
    researchRounds: child.researchRounds ?? 0,
    elapsedSeconds,
    ...(decision ? { decision } : {}),
    ...(outcome !== undefined ? { outcome } : {}),
    ...(child.planLine?.trim() ? { planLine: child.planLine.trim() } : {}),
    ...(lastQuery ? { lastQuery } : {}),
    ...(memoLine ? { memoLine } : {}),
  }
}

/** The check's part of the folded line: the record's summary once it
 * arrived, nothing when the record says no review was owed. A mission
 * that ended without a record claims no review: a completed one reads
 * "Ohne Ausgang", as the recap does, one that broke off says nothing
 * here. Only while it still runs does the line count the research rounds
 * its review opened. */
export function childCheckLabel(
  view: Pick<ChildMissionView, 'outcome' | 'researchRounds' | 'runStatus' | 'terminal'>,
  t: TranslationDictionary,
): string {
  if (view.outcome !== undefined) return view.outcome ?? ''
  if (view.terminal) {
    return view.runStatus === 'completed' ? t.agent.timeline.recapNoOutcome : ''
  }
  if (view.researchRounds <= 0) return t.agent.timeline.childCheckClean
  return countLabel(
    view.researchRounds,
    t.agent.timeline.childCheckRoundOne,
    t.agent.timeline.childCheckRounds,
  )
}

/** The one-line retrospective a folded card shows: counts and the
 * duration — what the delegation cost, readable later — then the check's
 * outcome. The outcome is the longest part and comes last, so a line cut
 * to its column still shows the cost. */
export function childMissionFoldedLine(view: ChildMissionView, t: TranslationDictionary): string {
  const line = [
    countLabel(view.tasks, t.agent.timeline.taskCountOne, t.agent.timeline.taskCount),
    view.searches > 0
      ? countLabel(
        view.searches,
        t.agent.timeline.childSearchCountOne,
        t.agent.timeline.childSearchCount,
      )
      : '',
    countLabel(
      view.sections,
      t.agent.timeline.sectionCountOne,
      t.agent.timeline.sectionCount,
    ),
    formatDurationMsShort(view.elapsedSeconds * 1000),
    childCheckLabel(view, t),
  ].filter(Boolean).join(' · ')
  if (!view.brokenOff) return line
  const label = t.agent.timeline[view.brokenOff]
  return `${view.error ? `${label}: ${view.error}` : label} · ${line}`
}
