import type { AgentRunRecord, AgentStepEntry } from '../model'

type TaskRowRun = Pick<AgentRunRecord, 'stepLog'>

/** Compact plain-text task evidence for a transcript row: Markdown marks,
 * links and a leading "<title>:" are dropped, the words stay as written. */
export function agentTaskResultPreview(title: string, markdown: string): string {
  let text = markdown
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/[`*_~>#|]/g, '')
    .replace(/\s+/g, ' ')
    .trim()
  const normalizedTitle = title.trim()
  if (
    normalizedTitle
    && text.toLocaleLowerCase().startsWith(
      `${normalizedTitle.toLocaleLowerCase()}:`,
    )
  ) {
    text = text.slice(normalizedTitle.length + 1).trimStart()
  }
  return text
}

const TASK_NARRATION_ID = /^n-task-(.+)$/

/** The result line a finished task wrote (`n-task-<id>`, kind `task`):
 * "<title>: <result head>". */
function taskNarration(
  stepLog: readonly AgentStepEntry[],
  taskId: string,
): AgentStepEntry | undefined {
  return stepLog.find(
    (entry) =>
      entry.kind === 'narration'
      && entry.detail === 'task'
      && entry.narrationId === `n-task-${taskId}`,
  )
}

/**
 * What a task row of a stored mission run says. The title is the one its
 * event carried; a run stored before task events carried titles names the
 * task by its result line instead — whole, since it opens with the title
 * and where that title ends cannot be read back — and else by its id.
 * `result` is the head of a concluded task's result, empty where the title
 * line already carries it or nothing was reported.
 */
export function retiredTaskRow(
  entry: AgentStepEntry,
  stepLog: readonly AgentStepEntry[],
): { title: string; result: string } {
  const concluded = entry.kind === 'task_finished' || entry.kind === 'task_failed'
  const taskId = entry.taskId ?? ''
  if (entry.title) {
    return {
      title: entry.title,
      result: concluded ? agentTaskResultPreview(entry.title, entry.result ?? '') : '',
    }
  }
  const narration = concluded ? taskNarration(stepLog, taskId) : undefined
  if (narration?.text) {
    return { title: agentTaskResultPreview('', narration.text), result: '' }
  }
  return {
    title: taskId,
    result: concluded ? agentTaskResultPreview(taskId, entry.result ?? '') : '',
  }
}

/** Whether a task's start line has been answered by its outcome line: the
 * transcript then shows the outcome alone, one line per concluded task. */
export function taskStartConcluded(
  entry: AgentStepEntry,
  stepLog: readonly AgentStepEntry[],
): boolean {
  return stepLog.some(
    (row) =>
      (row.kind === 'task_finished' || row.kind === 'task_failed')
      && row.taskId === entry.taskId
      && row.seq > entry.seq,
  )
}

/**
 * Whether a finished task's narration (`n-task-<id>`, kind `task`) says
 * nothing the task's own row does not already show: the row names the
 * task by that narration, or shows the result head under its title. The
 * transcript then folds the narration, so the result stands once.
 */
export function taskResultShownByRow(entry: AgentStepEntry, run: TaskRowRun): boolean {
  if (entry.kind !== 'narration' || entry.detail !== 'task') return false
  const taskId = TASK_NARRATION_ID.exec(entry.narrationId ?? '')?.[1]
  if (!taskId) return false
  return run.stepLog.some((row) => {
    if (row.kind !== 'task_finished' || row.taskId !== taskId || row.error) return false
    if (row.status !== undefined && row.status !== 'completed') return false
    return !row.title || retiredTaskRow(row, run.stepLog).result !== ''
  })
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
