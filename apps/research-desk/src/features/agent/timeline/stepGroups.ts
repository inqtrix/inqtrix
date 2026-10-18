import type { AgentRunRecord, AgentStepEntry } from '../model'
import {
  agentTaskResultPreview,
  effectiveAgentTaskStatus,
} from '../plan/taskPresentation'

type TaskRowRun = Pick<AgentRunRecord, 'plan' | 'stepLog' | 'taskStates'>

/**
 * The result line a task row shows under its title: the plain-text head of
 * the task's result, and only once the row is terminal, carries no error
 * and did not end without sufficient evidence (those rows show the error or
 * the status instead). Empty means the row shows no result. The row and the
 * fold of the task's narration read THIS function, so the narration is
 * hidden exactly when the row states the same result.
 */
export function taskRowResultPreview(entry: AgentStepEntry, run: TaskRowRun): string {
  if (entry.kind !== 'task_finished' && entry.kind !== 'task_failed') return ''
  if (entry.error) return ''
  const task = run.plan?.tasks.find((candidate) => candidate.taskId === entry.taskId)
  const live = entry.taskId ? run.taskStates[entry.taskId] : undefined
  if (task && effectiveAgentTaskStatus(task, live) === 'insufficient_evidence') return ''
  const result = task?.resultSummary || live?.resultSummary || ''
  const title = task?.title ?? entry.taskId ?? ''
  return result ? agentTaskResultPreview(title, result) : ''
}

const TASK_NARRATION_ID = /^n-task-(.+)$/

/**
 * Whether a finished task's narration (`n-task-<id>`, kind `task`) says
 * nothing the task's own row does not already show. The server keeps
 * emitting that line — the child mission card joins it by id — so the
 * transcript folds it here, and only while a finished row of the same task
 * renders a non-empty result from {@link taskRowResultPreview}.
 */
export function taskResultShownByRow(entry: AgentStepEntry, run: TaskRowRun): boolean {
  if (entry.kind !== 'narration' || entry.detail !== 'task') return false
  const taskId = TASK_NARRATION_ID.exec(entry.narrationId ?? '')?.[1]
  if (!taskId) return false
  // Without its plan task the row can only show the raw id as its title;
  // the narration is then the one line naming the task in words.
  if (!run.plan?.tasks.some((task) => task.taskId === taskId)) return false
  return run.stepLog.some(
    (row) =>
      row.kind === 'task_finished'
      && row.taskId === taskId
      && taskRowResultPreview(row, run) !== '',
  )
}

/**
 * The step feed's disclosure groups: consecutive per-section narration
 * lines of one memo pass ("Abschnitt 'X' geschrieben.") fold into one
 * row with a chevron — the reader sees "N Abschnitte geschrieben" (or
 * the newest line while the pass is running) and opens the list on
 * demand. Every other entry stays a row of its own.
 */
export type StepFeedItem =
  | { kind: 'entry'; entry: AgentStepEntry }
  | { kind: 'sections'; entries: AgentStepEntry[]; seq: number }
  | { kind: 'evidence'; entries: AgentStepEntry[]; seq: number }

const SECTION_ID = /^n-section-\d+$/

export function isSectionLine(entry: AgentStepEntry): boolean {
  return entry.kind === 'narration' && SECTION_ID.test(entry.narrationId ?? '')
}

export function isEvidenceReadEntry(entry: AgentStepEntry): boolean {
  return entry.kind === 'activity' && (
    entry.activityOperation === 'evidence_read'
    || entry.activityOperationCode === 'read_evidence'
    || entry.label === 'read_evidence'
  )
}

export function groupStepEntries(stepLog: readonly AgentStepEntry[]): StepFeedItem[] {
  const items: StepFeedItem[] = []
  for (const entry of stepLog) {
    if (isEvidenceReadEntry(entry)) {
      const last = items[items.length - 1]
      if (last?.kind === 'evidence') last.entries.push(entry)
      else items.push({ kind: 'evidence', entries: [entry], seq: entry.seq })
      continue
    }
    if (isSectionLine(entry)) {
      const last = items[items.length - 1]
      if (last && last.kind === 'sections') {
        last.entries.push(entry)
        continue
      }
      items.push({ kind: 'sections', entries: [entry], seq: entry.seq })
      continue
    }
    items.push({ kind: 'entry', entry })
  }
  return items
}
