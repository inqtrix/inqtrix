import { formatDuration } from '@/lib/time'
import type { TranslationDictionary } from '../../i18n/translations'
import { countLabel } from '@/i18n/plural'
import type { AgentStepEntry } from './model'

/**
 * What the tool rows say about ONE todo item after the fact: how many
 * steps served it and how long they spanned. Derived from the step log's
 * `todoIndex` (the kernel stamps every tool row with the todo it served),
 * never from the model's own report — the list stays a report, the
 * numbers stay measurements.
 */
export type TodoStepStats = { steps: number; seconds: number; running: boolean }

export function todoStepStats(
  stepLog: readonly Pick<AgentStepEntry, 'todoIndex' | 'at' | 'startedAt' | 'status'>[],
  index: number,
): TodoStepStats {
  const rows = stepLog.filter((item) => item.todoIndex === index)
  if (rows.length === 0) return { steps: 0, seconds: 0, running: false }
  const first = Math.min(...rows.map((item) => item.startedAt ?? item.at))
  const last = Math.max(...rows.map((item) => item.at))
  // A row that has not settled has no end yet: its span is unknown, and
  // "00:00:00" would read as a step that took no time (seen on a
  // delegation that had been running for nine minutes).
  const running = rows.some((item) => item.status === 'running')
  return { steps: rows.length, seconds: Math.max(0, last - first), running }
}

/** "3 Schritte · 18:03", "1 Schritt · läuft" while a row is still open —
 * empty when no row served the todo. */
export function todoStepText(
  stats: TodoStepStats,
  t: TranslationDictionary,
): string {
  if (stats.steps === 0) return ''
  const duration = stats.running
    ? t.agent.timeline.todoStepRunning
    : formatDuration(stats.seconds)
  return countLabel(
    stats.steps,
    t.agent.timeline.todoStepsOne,
    t.agent.timeline.todoSteps,
  ).replace('{duration}', duration)
}
