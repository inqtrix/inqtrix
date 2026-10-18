import {
  AlertTriangle,
  ChevronDown,
  FileSearch,
  Globe2,
  Info,
  Search,
  Trash2,
  Users,
  XCircle,
  type LucideIcon,
} from '@/components/icons'
import {
  forwardRef,
  type MouseEvent,
} from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { SharedBadge } from '@/features/sharing/SharedBadge'
import { canCancelWithAccess } from '@/features/sharing/shareModel'
import { useLocale } from '@/i18n/LocaleProvider'
import { PhaseSegments as SharedPhaseSegments } from '@/components/ui/phase-segments'
import { cn } from '@/lib/utils'
import { useRunningDuration } from '@/features/researchRuns/useRunningDuration'
import { appMotion } from '@/motion/transitions'
import { localizedText, phaseOrder, type JobPhase, type ResearchJob } from '../types'
import {
  phaseLabel,
  queuedPhaseIcon,
  shortRunId,
  statusBadgeClassName,
  statusIcon,
} from './runDisplay'

type ResearchJobCardProps = {
  cancelError?: string
  isExpanded: boolean
  isCancelSubmitting?: boolean
  isSelected: boolean
  job: ResearchJob
  onCancel: () => void
  onDelete: () => void
  onSelect: () => void
  onShare?: () => void
  onToggleExpanded: () => void
  shareCount?: number
}

export const ResearchJobCard = forwardRef<HTMLElement, ResearchJobCardProps>(
  function ResearchJobCard({
    cancelError,
    isExpanded,
    isCancelSubmitting = false,
    isSelected,
    job,
    onCancel,
    onDelete,
    onSelect,
    onShare,
    onToggleExpanded,
    shareCount,
  }, ref) {
    const { locale, t } = useLocale()
    const StatusIcon = job.unavailable
      ? statusIcon.expired
      : statusIcon[job.status]
    const reduceMotion = useReducedMotion()
    // A locked card must not keep a 1 Hz interval alive: feed the hook a
    // non-running status so it neither ticks nor re-renders.
    const runningDuration = useRunningDuration(
      job.unavailable ? 'expired' : job.status,
      job.startedAtIso,
    )
    const isSharedIn = job.access?.mode === 'shared'
    // An active run is cancellable, not deletable: the server delete is
    // terminal-only (409 while active), so the trash button is hidden for
    // running/queued runs and cancel is the action instead.
    const isActive = !job.unavailable
      && (job.status === 'running' || job.status === 'queued')
    // Mirrors the server rule: cancelling a shared-in run needs at
    // least an edit grant — a view grantee would only earn a 404.
    const canCancel = isActive && canCancelWithAccess(job.access)
    const metadata: { text: string; title?: string }[] = [
      { text: `${t.runCard.jobId}: ${shortRunId(job.id)}`, title: `${t.runCard.jobId}: ${job.id}` },
      {
        text: job.status === 'completed' || job.status === 'failed' || job.status === 'cancelled' || job.status === 'expired'
          ? `${t.runCard.submitted}: ${job.submittedAt}`
          : `${t.runCard.started}: ${job.startedAt ?? job.submittedAt}`,
      },
    ]

    if (job.duration) {
      metadata.push({ text: `${t.runCard.duration}: ${job.duration}` })
    }
    if (job.status === 'queued' && job.queueNote) {
      metadata.push({ text: localizedText(job.queueNote, locale) })
    }
    if (job.status === 'running' && !job.unavailable) {
      metadata.push({ text: `${t.runCard.runtime}: ${runningDuration}` })
    }
    if (isCancelSubmitting) {
      metadata.push({ text: t.runCard.cancelSubmitted })
    } else if (canCancel && job.cancelRequested) {
      metadata.push({ text: t.runCard.cancelRequested })
    } else if (cancelError) {
      metadata.push({ text: `${t.runCard.cancelFailed}: ${cancelError}` })
    }
    if (job.error) {
      metadata.push({ text: job.error })
    }
    if (job.unavailable) {
      metadata.push({ text: t.runCard.unavailableHint })
    }

    return (
      <motion.article
        ref={ref}
        layout="position"
        initial={reduceMotion ? false : { opacity: 0, y: 6 }}
        animate={{ opacity: 1, y: 0 }}
        exit={reduceMotion ? { opacity: 0 } : { opacity: 0, scale: 0.98, y: -6 }}
        onClick={onSelect}
        transition={appMotion.card}
        className={cn(
          'relative cursor-default rounded-lg border bg-card p-4 shadow-[0_1px_2px_var(--shadow-hairline)] transition-shadow',
          isSelected
            ? 'border-brand shadow-[0_12px_30px_var(--brand-shadow)]'
            : 'border-border hover:shadow-[0_8px_24px_var(--shadow-soft)]',
          isSelected && 'mb-3',
        )}
      >
        <div className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-3 sm:grid-cols-[auto_minmax(0,1fr)_auto]">
          <StatusIcon
            className={cn(
              'mt-1 size-4 shrink-0',
              job.status === 'completed' && 'text-success',
              job.status === 'queued' && 'text-muted-foreground',
              job.status === 'running' && 'text-brand',
              job.status === 'failed' && 'text-destructive',
              job.status === 'cancelled' && 'text-destructive/80',
              job.status === 'expired' && 'text-muted-foreground',
              job.unavailable && 'text-muted-foreground',
              job.status === 'running' && !job.unavailable && !reduceMotion
                && 'animate-spin [animation-duration:1.6s]',
            )}
          />
          <div className="min-w-0">
            <h2 className="line-clamp-2 t-card text-foreground">
              {localizedText(job.title, locale)}
            </h2>
            <div className="mt-0.5 flex flex-wrap items-center gap-x-2 gap-y-1 t-meta text-muted-foreground">
              {metadata.map((item) => (
                <span className="min-w-0 truncate" key={item.text} title={item.title}>
                  {item.text}
                </span>
              ))}
            </div>
            {job.status === 'running' && !job.unavailable && !isExpanded && (
              <RunningCompactStatus job={job} />
            )}
          </div>
          <div className="col-span-2 ml-8 flex items-center gap-1 sm:col-span-1 sm:ml-0">
            <SharedBadge
              count={isSharedIn ? undefined : shareCount}
              isSharedWithMe={isSharedIn}
              onClick={isSharedIn ? undefined : onShare}
            />
            {job.unavailable ? (
              <Badge
                className={statusBadgeClassName.expired}
                variant="outline"
              >
                {t.runCard.unavailableBadge}
              </Badge>
            ) : (
              <Badge className={statusBadgeClassName[job.status]} variant="outline">
                {t.status[job.status]}
              </Badge>
            )}
            {canCancel && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    aria-label={isCancelSubmitting ? t.runCard.cancelSubmitted : t.runCard.cancel}
                    className="text-muted-foreground hover:text-destructive disabled:text-muted-foreground"
                    disabled={isCancelSubmitting || job.cancelRequested}
                    onClick={(event) => runCardAction(event, onCancel)}
                    size="icon"
                    type="button"
                    variant="ghost"
                  >
                    <XCircle className="size-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>
                  {isCancelSubmitting
                    ? t.runCard.cancelSubmitted
                    : job.cancelRequested
                      ? t.runCard.cancelRequested
                      : t.runCard.cancel}
                </TooltipContent>
              </Tooltip>
            )}
            {!isSharedIn && onShare && (
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    aria-label={t.sharing.share}
                    className="text-muted-foreground hover:text-foreground"
                    onClick={(event) => runCardAction(event, onShare)}
                    size="icon"
                    type="button"
                    variant="ghost"
                  >
                    <Users className="size-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{t.sharing.share}</TooltipContent>
              </Tooltip>
            )}
            {!isSharedIn && !isActive && (
              <Button
                aria-label={t.runCard.delete}
                className="text-muted-foreground hover:text-destructive"
                onClick={(event) => runCardAction(event, onDelete)}
                size="icon"
                type="button"
                variant="ghost"
              >
                <Trash2 className="size-4" />
              </Button>
            )}
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  aria-label={isSelected && isExpanded ? t.runCard.collapse : t.runCard.expand}
                  className="text-muted-foreground hover:text-foreground"
                  onClick={(event) => runCardAction(event, onToggleExpanded)}
                  size="icon"
                  type="button"
                  variant="ghost"
                >
                  <ChevronDown
                    className={cn(
                      'size-4 transition-transform duration-300',
                      isSelected && isExpanded ? '' : '-rotate-90',
                    )}
                  />
                </Button>
              </TooltipTrigger>
              <TooltipContent>
                {isSelected && isExpanded ? t.runCard.collapse : t.runCard.expand}
              </TooltipContent>
            </Tooltip>
          </div>
        </div>

        {isSelected && (
          <div
            className={cn(
              'grid overflow-hidden transition-[grid-template-rows] duration-200 ease-out motion-reduce:transition-none',
              isExpanded ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]',
            )}
          >
            <motion.div
              animate={isExpanded ? { opacity: 1, y: 0 } : { opacity: 0, y: -4 }}
              className="min-h-0 overflow-hidden"
              initial={false}
              transition={appMotion.card}
            >
              {job.status === 'running' && !job.unavailable ? (
                <RunningJobDetails job={job} />
              ) : (
                <CompactJobDetails job={job} />
              )}
            </motion.div>
          </div>
        )}

      </motion.article>
    )
  },
)

function runCardAction(
  event: MouseEvent<HTMLButtonElement>,
  action: () => void,
) {
  event.stopPropagation()
  action()
}

/** Research binding of the shared five-segment phase bar. */
function PhaseSegments({
  activePhase,
  completedPhases,
  thin = false,
  withLabels = false,
}: {
  activePhase: JobPhase
  completedPhases: readonly JobPhase[]
  thin?: boolean
  withLabels?: boolean
}) {
  const { t } = useLocale()
  return (
    <SharedPhaseSegments
      activePhase={activePhase}
      completedPhases={completedPhases}
      labelFor={(phase) => phaseLabel(phase, t)}
      phases={phaseOrder}
      thin={thin}
      withLabels={withLabels}
    />
  )
}

function RunningCompactStatus({ job }: { job: ResearchJob }) {
  const { locale, t } = useLocale()
  const reduceMotion = Boolean(useReducedMotion())
  const roundInfo = parseRoundMetric(job.metrics.rounds)
  const latestEvent = job.events.at(-1)
  const MessageIcon = latestEvent?.severity === 'warning' || latestEvent?.severity === 'error'
    ? AlertTriangle
    : Info
  const hasMetrics = job.metrics.sources > 0 || job.metrics.queries > 0

  return (
    <div className="mt-2.5 min-w-0 rounded-md border border-border bg-surface/70 px-2.5 py-2">
      <div className="flex items-center gap-3">
        <span className="inline-flex min-w-0 shrink-0 items-center gap-1.5 text-xs font-semibold text-brand">
          <span
            aria-hidden="true"
            className={cn(
              'size-1.5 shrink-0 rounded-full bg-brand',
              !reduceMotion && 'inqtrix-running-dot',
            )}
          />
          <AnimatePresence initial={false} mode="wait">
            <motion.span
              animate={{ opacity: 1, y: 0 }}
              className="truncate"
              exit={reduceMotion ? undefined : { opacity: 0, y: -5 }}
              initial={reduceMotion ? false : { opacity: 0, y: 5 }}
              key={job.activePhase}
              transition={appMotion.list}
            >
              {phaseLabel(job.activePhase, t)}
            </motion.span>
          </AnimatePresence>
        </span>
        <PhaseSegments activePhase={job.activePhase} completedPhases={job.completedPhases} thin />
        <span className="shrink-0 t-meta-sm font-medium tabular-nums text-muted-foreground">
          {t.runCard.currentRound}&nbsp;{roundInfo.current}{roundInfo.max ? `/${roundInfo.max}` : ''}
        </span>
      </div>

      {(latestEvent || hasMetrics) && (
        <div className="mt-2 flex items-center gap-3 t-meta-sm text-muted-foreground">
          {latestEvent && (
            <span className="flex min-w-0 flex-1 items-center gap-1.5">
              <MessageIcon
                className={cn(
                  'size-3 shrink-0',
                  latestEvent.severity === 'warning' && 'text-warning',
                  latestEvent.severity === 'error' && 'text-destructive',
                  latestEvent.severity !== 'warning' && latestEvent.severity !== 'error' && 'text-muted-foreground/70',
                )}
              />
              <span className="truncate">{localizedText(latestEvent.title, locale)}</span>
            </span>
          )}
          <span className="ml-auto flex shrink-0 items-center gap-3 tabular-nums">
            {job.metrics.sources > 0 && (
              <span className="inline-flex items-center gap-1">
                <Globe2 className="size-3" />
                <strong className="font-semibold text-foreground">{job.metrics.sources}</strong> {t.runCard.sources}
              </span>
            )}
            {job.metrics.queries > 0 && (
              <span className="inline-flex items-center gap-1">
                <Search className="size-3" />
                <strong className="font-semibold text-foreground">{job.metrics.queries}</strong> {t.runCard.queries}
              </span>
            )}
          </span>
        </div>
      )}
    </div>
  )
}

function RunningJobDetails({ job }: { job: ResearchJob }) {
  const { locale, t } = useLocale()
  const reduceMotion = Boolean(useReducedMotion())
  const roundInfo = parseRoundMetric(job.metrics.rounds)
  const activeIndex = phaseOrder.indexOf(job.activePhase)
  const visibleEvents = job.events.slice(-4)

  return (
    <div className="mt-3 space-y-3">
      <div className="rounded-lg border border-border bg-surface px-3 py-3">
        <div className="flex items-center justify-between gap-3">
          <span className="flex min-w-0 items-center gap-2">
            <span
              aria-hidden="true"
              className={cn(
                'size-2 shrink-0 rounded-full bg-brand',
                !reduceMotion && 'inqtrix-running-dot',
              )}
            />
            <span className="truncate text-xs font-semibold text-brand">
              {phaseLabel(job.activePhase, t)}
            </span>
            <span className="shrink-0 t-meta-sm font-medium tabular-nums text-muted-foreground">
              {t.runCard.phase} {activeIndex + 1}/{phaseOrder.length}
            </span>
          </span>
          <span className="shrink-0 t-meta-sm font-medium tabular-nums text-muted-foreground">
            {t.runCard.currentRound}&nbsp;
            <strong className="font-semibold text-foreground">{roundInfo.current}</strong>
            {roundInfo.max ? `/${roundInfo.max}` : ''}
          </span>
        </div>
        <div className="mt-2.5">
          <PhaseSegments activePhase={job.activePhase} completedPhases={job.completedPhases} withLabels />
        </div>
      </div>

      <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_270px]">
        <div className="rounded-lg border border-border bg-surface p-3">
          <div className="flex items-center justify-between gap-3">
            <h3 className="text-xs font-semibold text-foreground">{t.runCard.liveStatus}</h3>
            <span className="inline-flex items-center gap-1.5 text-xs font-semibold text-success">
              <span
                aria-hidden="true"
                className={cn('size-1.5 rounded-full bg-success', !reduceMotion && 'inqtrix-running-dot')}
              />
              {t.runCard.live}
            </span>
          </div>
          {/* One row rises when one event ARRIVES — never the whole block.
              `initial={false}` covers the rows already present when this list
              first renders (a card mounting with history: a desk switch, or a
              reload where the server replays a run's events). Rows added after
              that are genuinely new and animate. Keying on the event's stable
              id is what makes the distinction hold: the sliding window then
              drops the oldest row and adds one, instead of rebuilding all four
              whenever the formatted minute changes. */}
          <ol className="mt-2 space-y-1.5">
            <AnimatePresence initial={false}>
            {visibleEvents.map((event) => {
              const EventIcon = event.severity === 'warning' || event.severity === 'error'
                ? AlertTriangle
                : Info

              return (
                <motion.li
                  animate={{ opacity: 1, y: 0 }}
                  className="grid min-w-0 grid-cols-[50px_14px_minmax(0,1fr)_auto] items-center gap-2 text-xs"
                  // Data-borne history/live distinction: replayed events
                  // (reload into a running run, reconnect catch-up) carry no
                  // arrivedLive and render in place even though they enter
                  // this AnimatePresence after its first render. Only an
                  // event that arrived on the live side of its stream rises.
                  initial={reduceMotion || !event.arrivedLive ? false : { opacity: 0, y: 4 }}
                  key={event.id}
                  transition={appMotion.list}
                >
                  <span className="tabular-nums text-muted-foreground">{event.time}</span>
                  <EventIcon
                    className={cn(
                      'size-3.5',
                      event.severity === 'warning' && 'text-warning',
                      event.severity === 'error' && 'text-destructive',
                      event.severity !== 'warning'
                        && event.severity !== 'error'
                        && 'text-muted-foreground/75',
                    )}
                  />
                  <span
                    className={cn(
                      'truncate text-muted-foreground',
                      event.active && 'font-semibold text-foreground',
                    )}
                  >
                    {localizedText(event.title, locale)}
                  </span>
                  {event.active && (
                    <span className="flex gap-1 text-brand" aria-hidden="true">
                      <span className="size-1 rounded-full bg-brand animate-pulse" />
                      <span className="size-1 rounded-full bg-brand animate-pulse [animation-delay:120ms]" />
                      <span className="size-1 rounded-full bg-brand animate-pulse [animation-delay:240ms]" />
                    </span>
                  )}
                </motion.li>
              )
            })}
            </AnimatePresence>
          </ol>
        </div>

        <div className="rounded-lg border border-border bg-card p-3">
          <h3 className="mb-2 text-xs font-semibold text-foreground">{t.runCard.metrics}</h3>
          <MetricRow flash icon={Globe2} label={t.runCard.sources} value={job.metrics.sources} />
          <MetricRow flash icon={Search} label={t.runCard.queries} value={job.metrics.queries} />
          <MetricRow flash icon={FileSearch} label={t.runCard.claims} value={job.metrics.claims} />
        </div>
      </div>
    </div>
  )
}

function CompactJobDetails({ job }: { job: ResearchJob }) {
  const { t } = useLocale()
  const QueuedIcon = queuedPhaseIcon

  if (job.status === 'queued') {
    return (
      <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted-foreground">
        {phaseOrder.map((phase) => (
          <span className="inline-flex items-center gap-1" key={phase}>
            <QueuedIcon className="size-3" />
            {phaseLabel(phase, t)}
          </span>
        ))}
      </div>
    )
  }

  return (
    <div className="mt-3 flex flex-wrap items-center gap-3 text-xs text-muted-foreground">
      <span>
        {t.runCard.sources} <strong className="text-foreground">{job.metrics.sources}</strong>
      </span>
      {job.score && (
        <span>
          {t.runCard.score}{' '}
          <strong className="font-semibold text-success">{job.score}</strong>
        </span>
      )}
    </div>
  )
}

function parseRoundMetric(rounds: number | string) {
  const value = String(rounds)
  const match = value.match(/^\s*(\d+)\s*\/\s*(\d+)\s*$/)

  if (!match) {
    return { current: value, max: null }
  }

  return { current: match[1], max: match[2] }
}

function MetricRow({
  flash = false,
  icon: Icon,
  label,
  value,
}: {
  flash?: boolean
  icon: LucideIcon
  label: string
  value: number | string
}) {
  const reduceMotion = Boolean(useReducedMotion())
  return (
    <div className="grid grid-cols-[20px_minmax(0,1fr)_auto] items-center gap-1.5 py-1 text-xs">
      <Icon className="size-4 text-muted-foreground" />
      <span className="truncate text-muted-foreground">{label}</span>
      <strong className="font-semibold tabular-nums text-foreground">
        {/* key={value} re-mounts on each change, replaying the flash animation. */}
        <span className={cn(flash && !reduceMotion && 'inqtrix-metric-flash')} key={String(value)}>
          {value}
        </span>
      </strong>
    </div>
  )
}
