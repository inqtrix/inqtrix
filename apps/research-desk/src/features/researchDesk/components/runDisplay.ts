import {
  CheckCircle2,
  Circle,
  CirclePause,
  Clock3,
  FileText,
  OctagonAlert,
  LoaderCircle,
  XCircle,
  Search,
  SlidersHorizontal,
  type LucideIcon,
} from '@/components/icons'
import type { TranslationDictionary } from '@/i18n/translations'
import type { JobPhase, JobStatus } from '../types'

export const statusIcon = {
  cancelled: XCircle,
  completed: CheckCircle2,
  expired: Clock3,
  failed: OctagonAlert,
  queued: Clock3,
  running: LoaderCircle,
  waiting_for_approval: CirclePause,
  waiting_for_input: CirclePause,
  // The children park is system progress, not a human wait: child
  // research runs are executing, so it reads as "running".
  waiting_for_children: LoaderCircle,
} satisfies Record<JobStatus, LucideIcon>

export const statusBadgeClassName = {
  cancelled: 'border-destructive/20 bg-destructive/10 text-destructive/85 hover:bg-destructive/10',
  completed: 'border-success/20 bg-success-subtle text-success hover:bg-success-subtle',
  expired: 'border-muted-foreground/20 bg-muted text-muted-foreground hover:bg-muted',
  failed: 'border-destructive/25 bg-destructive/10 text-destructive hover:bg-destructive/10',
  queued: 'border-border bg-muted text-muted-foreground hover:bg-muted',
  running: 'border-brand/20 bg-brand-subtle text-brand hover:bg-brand-subtle',
  waiting_for_approval:
    'border-warning/25 bg-warning-subtle text-warning hover:bg-warning-subtle',
  waiting_for_input:
    'border-warning/25 bg-warning-subtle text-warning hover:bg-warning-subtle',
  waiting_for_children: 'border-brand/20 bg-brand-subtle text-brand hover:bg-brand-subtle',
} satisfies Record<JobStatus, string>

export const phaseIcon = {
  analysis: CheckCircle2,
  planning: FileText,
  search: Search,
  evaluation: SlidersHorizontal,
  answer: FileText,
} satisfies Record<JobPhase, LucideIcon>

export const queuedPhaseIcon = Circle

export function phaseLabel(phase: JobPhase, t: TranslationDictionary) {
  if (phase === 'analysis') return t.runCard.analysis
  if (phase === 'planning') return t.runCard.planning
  if (phase === 'search') return t.runCard.search
  if (phase === 'evaluation') return t.runCard.evaluation
  return t.runCard.answer
}

/**
 * Compact a run id for the card metadata so it does not eat a whole line.
 * Keeps a recognizable prefix and suffix joined by a middle ellipsis
 * (e.g. `run_2c4f53d6…fe3426`); the full id stays available via the span's
 * tooltip. Ids short enough to fit are returned unchanged.
 */
export function shortRunId(id: string): string {
  const head = 12
  const tail = 6
  if (id.length <= head + tail + 1) return id
  return `${id.slice(0, head)}…${id.slice(-tail)}`
}
