import {
  AlertTriangle,
  CheckCircle2,
  Copy,
  Clock3,
  Download,
  FileText,
  Info,
  LoaderCircle,
  MessageSquarePlus,
  PanelRightClose,
} from '@/components/icons'
import {
  memo,
  useDeferredValue,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type UIEvent,
} from 'react'
import { motion, useReducedMotion } from 'motion/react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Switch } from '@/components/ui/switch'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { withFrontmatter } from '@/features/project/markdown'
import { partitionReportReferences, sourceRoleLabel } from '@/features/project/reportReferences'
import type { ResearchRunRecord } from '@/features/project/types'
import type { ReportReference } from '@/features/researchRuns/types'
import { useRunningDuration } from '@/features/researchRuns/useRunningDuration'
import { useLocale } from '@/i18n/LocaleProvider'
import type { TranslationDictionary } from '@/i18n/translations'
import { aiDisclosureFrontmatter, withAiDisclosure } from '@/lib/aiDisclosure'
import { cn } from '@/lib/utils'
import { StructuralLoadBoundary } from '@/motion/StructuralLoadBoundary'
import { Skeleton } from '@/components/ui/skeleton'
import {
  phaseLabel,
  statusBadgeClassName,
} from '../researchDesk/components/runDisplay'
import { MarkdownReport } from './MarkdownReport'
import {
  agentStepScrollDecision,
  agentStepScrollTop,
  isAgentStepScrollKey,
} from './agentStepScroll'
import { copyTextToClipboard } from '@/lib/clipboard'
import { formatDuration } from '@/lib/time'
import { AgentDisclosure } from '@/features/agent/AgentDisclosure'
import { agentArtifactReferences } from '@/features/agent/artifactCitations'
import { evidenceLineageFromArtifactPayload } from '@/features/agent/evidenceProvenance'
import { EvidenceProvenancePanel } from '@/features/agent/EvidenceProvenancePanel'
import { readerError } from '@/api/requestErrorText'

type ReportPanelProps = {
  onHide: () => void
  onSetReportAutocomplete?: (runId: string, includeInAutocomplete: boolean) => void
  onUseReportInChat?: (runId: string) => void
  /** Load the report again after its result failed to load. */
  onRetryResult?: (runId: string) => void
  selectedRun: ResearchRunRecord | null
}

type ReportPanelMode =
  | 'completed-loading'
  | 'completed-result-failed'
  | 'completed-with-report'
  | 'completed-without-report'
  | 'empty'
  | 'queued'
  | 'running'
  | 'terminal'

type ReportPanelState = {
  markdown: string | null
  mode: ReportPanelMode
}

export function reportStructuralIdentity(
  runId: string | null,
  mode: ReportPanelMode,
): string {
  return runId ? `report:${runId}:${mode}` : 'report:empty'
}

export const ReportPanel = memo(function ReportPanel({
  onHide,
  onRetryResult,
  onSetReportAutocomplete,
  onUseReportInChat,
  selectedRun,
}: ReportPanelProps) {
  const { t } = useLocale()
  // The navigation card acknowledges the click urgently in the parent. The
  // report tree itself enters React's transition lane so parsing a long cached
  // document cannot delay that feedback; no fixed timer is involved.
  const targetRun = useDeferredValue(selectedRun)
  const panelState = useMemo(() => resolveReportPanelState(targetRun), [targetRun])
  const isRunningRun = panelState.mode === 'running'
  const isCompletedRun = panelState.mode === 'completed-with-report'
    || panelState.mode === 'completed-without-report'
    || panelState.mode === 'completed-result-failed'
  const visibleEventCount = useMemo(
    () => (targetRun ? targetRun.events.filter(isDisplayableAgentEvent).length : 0),
    [targetRun],
  )
  const panelTitle = isRunningRun
    ? t.report.agentStepsTitle
    : panelState.mode === 'queued'
      ? t.report.queueTitle
      : t.report.title
  const canUseReportInChat = Boolean(
    targetRun && panelState.mode === 'completed-with-report' && onUseReportInChat,
  )

  return (
    // Header and body form one identity-owned surface. The boundary therefore
    // never exposes a newly selected title above the previous report body.
    <aside
      className="h-full min-h-0 w-full min-w-0 max-w-full overflow-hidden border-t border-border bg-background lg:border-t-0"
    >
      <StructuralLoadBoundary
        className="h-full"
        fallback={<ReportSurfaceSkeleton />}
        identity={reportStructuralIdentity(targetRun?.runId ?? null, panelState.mode)}
        phase={targetRun
          ? isRunningRun
            ? 'refreshing'
            : panelState.mode === 'completed-loading' ? 'pending' : 'ready'
          : 'empty'}
      >
      <Tabs
        className="flex h-full min-h-0 flex-col"
        data-report-surface-run-id={targetRun?.runId ?? ''}
        defaultValue="preview"
      >
        <div>
          <div className="flex inqtrix-panel-header items-center justify-between gap-3 border-b border-border px-3">
            <div className="min-w-0">
              <h2 className="t-section truncate text-foreground">
                {panelTitle}
              </h2>
            </div>
            <div className="flex shrink-0 items-center gap-1.5">
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    aria-label={t.report.useInChat}
                    className="size-7"
                    disabled={!canUseReportInChat}
                    onClick={() => {
                      if (!targetRun || !canUseReportInChat) return
                      onUseReportInChat?.(targetRun.runId)
                    }}
                    size="icon"
                    type="button"
                    variant="ghost"
                  >
                    <MessageSquarePlus className="size-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{t.report.useInChat}</TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    aria-label={t.report.hide}
                    className="size-7"
                    onClick={onHide}
                    size="icon"
                    type="button"
                    variant="ghost"
                  >
                    <PanelRightClose className="size-4" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{t.report.hide}</TooltipContent>
              </Tooltip>
            </div>
          </div>
          {targetRun && (
            <div className="border-b border-border px-4 py-2.5">
              <div className="min-w-0">
                <p className="t-meta line-clamp-3 break-words text-muted-foreground">
                  {targetRun.summary.title}
                </p>
                {isRunningRun ? (
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <span className="inline-flex h-6 items-center gap-1.5 rounded-md border border-success/20 bg-success-subtle px-2 text-xs font-semibold text-success">
                      <span className="size-1.5 rounded-full bg-success" />
                      {t.report.live}
                    </span>
                    <span className="inline-flex h-6 items-center rounded-md border border-border bg-surface px-2 text-xs font-semibold text-muted-foreground">
                      {visibleEventCount} {t.report.events}
                    </span>
                  </div>
                ) : (
                  panelState.mode === 'completed-with-report' && targetRun.source === 'mock' && (
                    <Badge
                      className="mt-1.5 shrink-0 border-border bg-muted text-muted-foreground hover:bg-muted"
                      variant="outline"
                    >
                      {t.report.mockReport}
                    </Badge>
                  )
                )}
              </div>
              {isCompletedRun && (
                <div className="mt-3 flex min-w-0 items-center">
                  <TabsList className="h-9 bg-surface">
                    <TabsTrigger className="h-7 px-2 text-xs" value="preview">
                      {t.report.tabs.preview}
                    </TabsTrigger>
                    <TabsTrigger className="h-7 px-2 text-xs" value="evidence">
                      {t.report.tabs.evidence}
                    </TabsTrigger>
                    <TabsTrigger className="h-7 px-2 text-xs" value="agentSteps">
                      {t.report.tabs.agentSteps}
                    </TabsTrigger>
                    <TabsTrigger className="h-7 px-2 text-xs" value="export">
                      {t.report.tabs.export}
                    </TabsTrigger>
                  </TabsList>
                </div>
              )}
            </div>
          )}
        </div>

        {!targetRun ? (
          <div className="flex min-h-0 flex-1" key="empty">
            <EmptyReportPanel />
          </div>
        ) : isCompletedRun ? (
          <div className="min-h-0 flex-1 overflow-hidden" key="report">
            <ScrollArea className="h-full min-h-0" key={targetRun.runId}>
              <TabsContent className="m-0 w-full min-w-0 max-w-full overflow-hidden p-4" value="preview">
                <ReportPreview
                  markdown={panelState.markdown}
                  onRetryResult={onRetryResult}
                  run={targetRun}
                />
              </TabsContent>
              <TabsContent className="m-0 w-full min-w-0 max-w-full overflow-hidden p-4" value="evidence">
                <ReportEvidence run={targetRun} />
              </TabsContent>
              <TabsContent className="m-0 w-full min-w-0 max-w-full overflow-hidden p-4" value="agentSteps">
                <ReportAgentSteps run={targetRun} />
              </TabsContent>
              <TabsContent className="m-0 w-full min-w-0 max-w-full overflow-hidden p-4" value="export">
                <ReportExport markdown={panelState.markdown} onSetAutocomplete={onSetReportAutocomplete} run={targetRun} />
              </TabsContent>
            </ScrollArea>
          </div>
        ) : (
          <div className="min-h-0 flex-1 overflow-hidden" key="status">
            {isRunningRun ? (
              <RunStatusPanel key={targetRun.runId} run={targetRun} />
            ) : (
              <ScrollArea className="h-full min-h-0" key={targetRun.runId}>
                <RunStatusPanel run={targetRun} />
              </ScrollArea>
            )}
          </div>
        )}
      </Tabs>
      </StructuralLoadBoundary>
    </aside>
  )
})

/** Complete target silhouette used only after the shared cold-wait delay. */
function ReportSurfaceSkeleton() {
  return (
    <div aria-hidden className="flex h-full min-h-0 flex-col bg-background">
      <div className="flex inqtrix-panel-header shrink-0 items-center gap-3 border-b border-border px-3">
        <Skeleton className="h-4 w-28" />
        <Skeleton className="ml-auto size-7 rounded-md" />
      </div>
      <div className="shrink-0 border-b border-border px-4 py-2.5">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="mt-2 h-8 w-56" />
      </div>
      <ReportBodySkeleton />
    </div>
  )
}

/** Document-shaped placeholder for the report body's staged mount, at the
 * body's own padding (TabsContent p-4). A FULL page of heading + paragraph
 * silhouette: the report fills the panel, so a top strip of bars made the
 * swap read as "empty becomes text" below — the crossfade can only blend
 * what overlaps. Overflow is cropped like real text at the panel edge. */
function ReportBodySkeleton() {
  const paragraph = (key: number, widths: readonly string[]) => (
    <div className="flex flex-col gap-2" key={key}>
      {widths.map((width, index) => (
        <Skeleton className={cn('h-4', width)} key={index} />
      ))}
    </div>
  )
  return (
    <div aria-hidden className="flex min-h-0 flex-1 flex-col gap-4 overflow-hidden p-4">
      <Skeleton className="h-6 w-2/5" />
      {paragraph(0, ['w-[92%]', 'w-[86%]', 'w-[89%]', 'w-[64%]'])}
      {paragraph(1, ['w-[88%]', 'w-[93%]', 'w-[71%]'])}
      <Skeleton className="h-5 w-1/4" />
      {paragraph(2, ['w-[90%]', 'w-[85%]', 'w-[94%]', 'w-[58%]'])}
      {paragraph(3, ['w-[87%]', 'w-[92%]', 'w-[66%]'])}
      <Skeleton className="h-5 w-1/3" />
      {paragraph(4, ['w-[91%]', 'w-[88%]', 'w-[79%]'])}
    </div>
  )
}

export function resolveReportPanelState(selectedRun: ResearchRunRecord | null): ReportPanelState {
  if (!selectedRun) {
    return { markdown: null, mode: 'empty' }
  }

  if (selectedRun.status === 'queued') {
    return { markdown: null, mode: 'queued' }
  }

  if (selectedRun.status === 'running') {
    return { markdown: null, mode: 'running' }
  }

  if (selectedRun.status !== 'completed') {
    return { markdown: null, mode: 'terminal' }
  }

  const markdown = selectedRun.result?.markdown ?? null
  if (markdown) return { markdown, mode: 'completed-with-report' }
  // A server run's result loads after the listing: until it arrives the
  // report is loading, not missing; a failed load is shown as such.
  if (selectedRun.source === 'api' && !selectedRun.result) {
    return {
      markdown: null,
      mode: selectedRun.resultError ? 'completed-result-failed' : 'completed-loading',
    }
  }
  return { markdown: null, mode: 'completed-without-report' }
}

function ReportPreview({
  markdown,
  onRetryResult,
  run,
}: {
  markdown: string | null
  onRetryResult?: (runId: string) => void
  run: ResearchRunRecord
}) {
  const { t } = useLocale()
  const [copyFeedback, setCopyFeedback] = useState<'copied' | 'idle'>('idle')
  const copyResetRef = useRef<number | null>(null)

  useEffect(() => {
    return () => {
      if (copyResetRef.current) {
        window.clearTimeout(copyResetRef.current)
      }
    }
  }, [])

  async function handleCopyMarkdown() {
    if (!markdown) return

    if (copyResetRef.current) {
      window.clearTimeout(copyResetRef.current)
    }

    try {
      // Whole-answer copy carries the disclosure; selections and code/table
      // copies stay byte-exact (see lib/aiDisclosure).
      const copied = await copyTextToClipboard(
        withAiDisclosure(markdown, t.aiTransparency.exportNotice),
      )
      if (!copied) {
        throw readerError('copyFailed')
      }
      setCopyFeedback('copied')
      copyResetRef.current = window.setTimeout(() => {
        setCopyFeedback('idle')
        copyResetRef.current = null
      }, 1600)
    } catch {
      setCopyFeedback('idle')
    }
  }

  if (markdown) {
    const copyLabel = copyFeedback === 'copied'
      ? t.report.copiedMarkdown
      : t.report.copyMarkdown

    return (
      <article className="group relative w-full min-w-0 max-w-full overflow-hidden [overflow-wrap:anywhere]">
        <div className="sticky top-2 z-20 flex h-0 justify-end pr-1">
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                aria-label={copyLabel}
                className={cn(
                  'pointer-events-auto size-8 border border-border/70 bg-card/85 text-muted-foreground opacity-0 shadow-[0_8px_24px_var(--shadow-soft)] backdrop-blur transition hover:bg-surface hover:text-foreground focus-visible:opacity-100 group-hover:opacity-100',
                  copyFeedback === 'copied' && '!opacity-100 border-success/30 bg-success-subtle text-success hover:bg-success-subtle hover:text-success',
                )}
                onClick={() => void handleCopyMarkdown()}
                size="icon"
                type="button"
                variant="ghost"
              >
                {copyFeedback === 'copied' ? (
                  <CheckCircle2 className="size-4" />
                ) : (
                  <Copy className="size-4" />
                )}
              </Button>
            </TooltipTrigger>
            <TooltipContent>{copyLabel}</TooltipContent>
          </Tooltip>
        </div>
        <span aria-live="polite" className="sr-only">
          {copyFeedback === 'copied' ? t.report.copiedMarkdown : ''}
        </span>
        <MarkdownReport markdown={markdown} />
      </article>
    )
  }

  return (
    <div className="flex min-h-[340px] items-center justify-center p-4 text-center">
      <div className="max-w-md">
        <div className="mx-auto flex size-14 items-center justify-center rounded-full bg-muted text-muted-foreground">
          <FileText className="size-7" />
        </div>
        <h3 className="mt-5 text-base font-semibold text-foreground">
          {run.summary.title}
        </h3>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">
          {reportFallbackDescription(run, t.report)}
        </p>
        {run.resultError && onRetryResult ? (
          <Button
            className="mt-4"
            onClick={() => onRetryResult(run.runId)}
            size="sm"
            type="button"
            variant="outline"
          >
            {t.report.retryResult}
          </Button>
        ) : null}
      </div>
    </div>
  )
}

/** Why a completed run shows no report: its result failed to load, the server
 * run delivered none, or a local run never had one. */
export function reportFallbackDescription(
  run: Pick<ResearchRunRecord, 'resultError' | 'source'>,
  texts: Pick<
    TranslationDictionary['report'],
    'noReportDescription' | 'resultLoadFailedDescription' | 'unavailableDescription'
  >,
): string {
  if (run.resultError) return texts.resultLoadFailedDescription
  return run.source === 'api' ? texts.noReportDescription : texts.unavailableDescription
}

function RunStatusPanel({ run }: { run: ResearchRunRecord }) {
  if (run.status === 'queued') {
    return <QueuedRunPanel run={run} />
  }
  if (run.status !== 'running') {
    return <TerminalRunPanel run={run} />
  }

  return <LiveRunPanel run={run} />
}

function LiveRunPanel({ run }: { run: ResearchRunRecord }) {
  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden">
      <LiveRunOverview run={run} />
      <div className="min-h-0 flex-1 overflow-hidden">
        <AgentEventList run={run} />
      </div>
    </div>
  )
}

export function LiveRunOverview({ run }: { run: ResearchRunRecord }) {
  const { t } = useLocale()
  const snapshot = run.snapshot
  const runningDuration = useRunningDuration(run.status, run.startedAt)
  const duration = run.status === 'running'
    ? runningDuration
    : run.durationSeconds !== undefined && Number.isFinite(run.durationSeconds)
      ? formatDuration(run.durationSeconds)
      : '—'

  return (
    <section className="border-b border-border px-4 py-3">
      <div className="grid gap-2 sm:grid-cols-2">
        <LiveRunMetric label={t.report.currentPhase} value={phaseLabel(run.phaseState.activePhase, t)} />
        <LiveRunMetric label={t.runCard.duration} value={duration} />
        <LiveRunMetric label={t.runCard.rounds} value={run.metrics.rounds} />
        <LiveRunMetric label={t.runCard.sources} value={run.metrics.sources} />
        <LiveRunMetric label={t.runCard.queries} value={run.metrics.queries} />
        {snapshot?.confidence ? (
          <LiveRunMetric label={t.common.confidence} value={`${snapshot.confidence} / 10`} />
        ) : null}
        {snapshot?.claim_quality_score !== undefined ? (
          <LiveRunMetric label={t.report.claimQuality} value={formatScore(snapshot.claim_quality_score)} />
        ) : null}
      </div>
    </section>
  )
}

function LiveRunMetric({
  label,
  value,
}: {
  label: string
  value: number | string
}) {
  return (
    <div className="rounded-md border border-border bg-surface px-3 py-2">
      <p className="t-caption text-muted-foreground">
        {label}
      </p>
      <p className="mt-1 truncate text-sm font-semibold text-foreground">{value}</p>
    </div>
  )
}

function QueuedRunPanel({ run }: { run: ResearchRunRecord }) {
  const { t } = useLocale()

  return (
    <div className="p-4">
      <section className="rounded-lg border border-dashed border-border bg-background p-6">
        <div className="flex flex-col items-center text-center">
          <div className="flex size-14 items-center justify-center rounded-full bg-muted text-muted-foreground">
            <Clock3 className="size-7" />
          </div>
          <Badge className={cn('mt-4 gap-1.5', statusBadgeClassName[run.status])} variant="outline">
            <Clock3 className="size-3.5" />
            {t.status[run.status]}
          </Badge>
          <h3 className="mt-4 text-base font-semibold leading-7 text-foreground [overflow-wrap:anywhere]">
            {t.report.waitingTitle}
          </h3>
          <p className="mt-2 max-w-md text-sm leading-6 text-muted-foreground">
            {run.summary.queueNote ?? t.report.queueDescription}
          </p>
        </div>
      </section>
    </div>
  )
}

function AgentEventList({ run }: { run: ResearchRunRecord }) {
  return <AgentStepTimeline mode="live" run={run} />
}

function TerminalRunPanel({ run }: { run: ResearchRunRecord }) {
  return (
    <div>
      <LiveRunOverview run={run} />
      <section className="px-4 py-3">
        <AgentStepTimeline mode="archive" run={run} />
      </section>
    </div>
  )
}

function ReportAgentSteps({ run }: { run: ResearchRunRecord }) {
  const { t } = useLocale()

  return (
    <div className="space-y-3">
      <section className="rounded-md border border-border bg-background p-3">
        <h3 className="t-section text-foreground">
          {t.report.agentStepsTitle}
        </h3>
        <p className="mt-1 text-sm leading-6 text-muted-foreground">
          {t.report.agentStepsArchiveDescription}
        </p>
      </section>
      <AgentStepTimeline mode="archive" run={run} />
    </div>
  )
}

function AgentStepTimeline({
  mode,
  run,
}: {
  mode: 'archive' | 'live'
  run: ResearchRunRecord
}) {
  const { t } = useLocale()
  const reduceMotion = Boolean(useReducedMotion())
  const isLive = mode === 'live'
  const events = run.events.filter(isDisplayableAgentEvent)
  const activeEventIndex = isLive ? events.findIndex((event) => event.active) : -1
  const derivedActiveEventIndex = isLive
    ? activeEventIndex >= 0
      ? activeEventIndex
      : events.length - 1
    : -1
  const activeStepRef = useRef<HTMLLIElement | null>(null)
  const logScrollRef = useRef<HTMLDivElement | null>(null)
  const positionedRunIdRef = useRef<string | null>(null)
  const userNavigatedLogRef = useRef(false)
  const [autoFollow, setAutoFollow] = useState(true)
  const [showJumpButton, setShowJumpButton] = useState(false)

  useLayoutEffect(() => {
    const activeStep = activeStepRef.current
    const scrollContainer = logScrollRef.current

    if (!isLive || !activeStep || !scrollContainer) return

    const decision = agentStepScrollDecision({
      autoFollow,
      geometry: {
        containerHeight: scrollContainer.clientHeight,
        stepHeight: activeStep.offsetHeight,
        stepTop: activeStep.offsetTop,
      },
      positionedRunId: positionedRunIdRef.current,
      reducedMotion: reduceMotion,
      runId: run.runId,
    })
    if (!decision) return

    if (decision.initializesRun) {
      positionedRunIdRef.current = run.runId
      userNavigatedLogRef.current = false
      setAutoFollow(true)
      setShowJumpButton(false)
      scrollContainer.scrollTop = decision.top
      return
    }
    scrollContainer.scrollTo({
      behavior: decision.behavior,
      top: decision.top,
    })
  }, [autoFollow, derivedActiveEventIndex, events.length, isLive, reduceMotion, run.runId])

  function handleEventScroll(event: UIEvent<HTMLDivElement>) {
    if (!isLive || !userNavigatedLogRef.current) return

    const target = event.currentTarget
    const distanceFromBottom = target.scrollHeight - target.scrollTop - target.clientHeight
    const isNearBottom = distanceFromBottom < 56

    setAutoFollow(isNearBottom)
    setShowJumpButton(!isNearBottom)
  }

  function markManualLogNavigation() {
    if (!isLive) return
    userNavigatedLogRef.current = true
  }

  function handleLogKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (isAgentStepScrollKey(event.key)) markManualLogNavigation()
  }

  function jumpToCurrentStep() {
    const activeStep = activeStepRef.current
    const scrollContainer = logScrollRef.current

    userNavigatedLogRef.current = false
    setAutoFollow(true)
    setShowJumpButton(false)

    if (!activeStep || !scrollContainer) return

    scrollContainer.scrollTo({
      behavior: reduceMotion ? 'auto' : 'smooth',
      top: agentStepScrollTop({
        containerHeight: scrollContainer.clientHeight,
        stepHeight: activeStep.offsetHeight,
        stepTop: activeStep.offsetTop,
      }),
    })
  }

  return (
    <section
      className={cn(
        'relative',
        isLive && 'flex h-full min-h-0 flex-col overflow-hidden px-4 py-3',
      )}
      onPointerDown={markManualLogNavigation}
      onTouchMove={markManualLogNavigation}
      onWheel={markManualLogNavigation}
    >
      {events.length > 0 ? (
        <div className={cn('relative', isLive && 'min-h-0 flex-1 overflow-hidden')}>
          <div
            aria-label={isLive ? t.report.agentStepsDescription : undefined}
            className={cn(
              isLive
                ? [
                  'h-full min-h-0 overflow-y-auto overscroll-contain pr-2 [scrollbar-gutter:stable] [scrollbar-width:thin]',
                  '[scrollbar-color:color-mix(in_oklch,var(--muted-foreground)_20%,transparent)_transparent]',
                  '[&::-webkit-scrollbar]:w-1.5 [&::-webkit-scrollbar-track]:bg-transparent',
                  '[&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border/50',
                  'hover:[&::-webkit-scrollbar-thumb]:bg-muted-foreground/30',
                ]
                : 'overflow-visible',
            )}
            onKeyDown={handleLogKeyDown}
            onScroll={handleEventScroll}
            ref={logScrollRef}
            role={isLive ? 'region' : undefined}
            tabIndex={isLive ? 0 : undefined}
          >
            <ol className="space-y-1">
              {events.map((event, index) => {
                const isActive = isLive && index === derivedActiveEventIndex
                const isDone = isLive && index < derivedActiveEventIndex
                const isWarning = event.severity === 'warning'
                const isError = event.severity === 'error'
                const showAlert = isWarning || isError

                return (
                  <li
                    className="grid grid-cols-[28px_minmax(0,1fr)] gap-3"
                    key={event.id}
                    ref={isActive ? activeStepRef : undefined}
                  >
                    <div className="relative flex justify-center pt-0.5">
                      {index < events.length - 1 && (
                        <span
                          className={cn(
                            'absolute bottom-[-10px] top-7 w-px bg-border',
                            isActive && 'bg-brand/25',
                          )}
                        />
                      )}
                      <span
                        className={cn(
                          'relative z-10 flex size-5 items-center justify-center rounded-full border bg-card text-muted-foreground transition-colors',
                          (isDone || !isLive) && !showAlert && 'border-transparent bg-transparent',
                          isActive && !showAlert && 'border-brand/35 bg-brand-subtle text-brand shadow-[0_0_0_3px_var(--brand-subtle)]',
                          isWarning && 'border-warning/30 bg-warning-subtle text-warning',
                          isError && 'border-destructive/30 bg-destructive/10 text-destructive',
                          !isDone && !isActive && !showAlert && 'border-border',
                        )}
                      >
                        {isActive && !showAlert && !reduceMotion && (
                          <motion.span
                            aria-hidden="true"
                            animate={{ opacity: [0.45, 0, 0.45], scale: [1, 1.55, 1] }}
                            className="absolute inset-0 rounded-full border border-brand/35"
                            transition={{ duration: 1.8, ease: 'easeInOut', repeat: Infinity }}
                          />
                        )}
                        {showAlert ? (
                          <AlertTriangle className="relative size-3.5" />
                        ) : isActive ? (
                          <LoaderCircle
                            className={cn(
                              'relative size-3.5',
                              !reduceMotion && 'animate-spin [animation-duration:1.5s]',
                            )}
                          />
                        ) : (
                          <span
                            className={cn(
                              'relative size-1.5 rounded-full bg-muted-foreground/40',
                              isDone && 'bg-muted-foreground/35',
                            )}
                          />
                        )}
                      </span>
                    </div>
                    <div
                      className={cn(
                        'min-w-0 pb-4 text-sm',
                        isActive && 'rounded-md bg-brand-subtle/35 px-2.5 py-2 shadow-[inset_2px_0_0_var(--brand)]',
                        isWarning && 'rounded-md bg-warning-subtle/45 px-2.5 py-2 shadow-[inset_2px_0_0_var(--warning)]',
                        isError && 'rounded-md bg-destructive/10 px-2.5 py-2 shadow-[inset_2px_0_0_var(--destructive)]',
                      )}
                    >
                      <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-3">
                        <p
                          className={cn(
                            'leading-6 text-foreground [overflow-wrap:anywhere]',
                            isLive && !isActive && !isDone && 'text-muted-foreground',
                          )}
                        >
                          {displayAgentEventTitle(event.title, t)}
                        </p>
                        <span className="t-hint flex items-center gap-1 pt-1 text-muted-foreground">
                          {event.severity === 'warning' ? (
                            <AlertTriangle className="size-3 text-warning" />
                          ) : event.severity === 'error' ? (
                            <AlertTriangle className="size-3 text-destructive" />
                          ) : (
                            <Info className="size-3 text-muted-foreground/70" />
                          )}
                          <time>{formatTime(event.createdAt)}</time>
                        </span>
                      </div>
                      {isActive && isLive && (
                        <span className="mt-1.5 inline-flex rounded-full bg-brand/10 px-2 py-0.5 t-meta-sm font-semibold text-brand">
                          {t.report.activeEvent}
                        </span>
                      )}
                    </div>
                  </li>
                )
              })}
            </ol>
          </div>
          {showJumpButton && (
            <Button
              className="absolute bottom-3 right-4 gap-2 shadow-[0_8px_24px_var(--shadow-soft)]"
              onClick={jumpToCurrentStep}
              size="sm"
              type="button"
              variant="secondary"
            >
              <LoaderCircle className="size-3.5" />
              <span>{t.report.jumpToCurrent}</span>
            </Button>
          )}
        </div>
      ) : (
        <p className="mt-3 text-sm leading-6 text-muted-foreground">
          {isLive ? t.report.noEvents : t.report.agentStepsEmpty}
        </p>
      )}
    </section>
  )
}

function ReferenceList({ heading, references, ledger }: {
  heading: string
  references: readonly ReportReference[]
  ledger?: Record<string, unknown>
}) {
  const { t } = useLocale()
  return (
    <section>
      <h3 className="t-section text-foreground">{heading}</h3>
      <ol className="mt-2 space-y-2">
        {references.map((reference, index) => (
          <li
            className="rounded-md border border-border bg-background p-3 text-sm"
            key={`${reference.url}-${index}`}
          >
            <div className="flex min-w-0 items-start gap-2">
              <span className="mt-0.5 inline-flex h-5 shrink-0 items-center rounded-sm border border-border bg-card px-1.5 t-hint font-semibold text-muted-foreground">
                {reference.label}
              </span>
              <a
                className="min-w-0 break-words font-medium text-foreground hover:text-brand"
                href={reference.url}
                rel="noreferrer"
                target="_blank"
              >
                {reference.url}
              </a>
            </div>
            <p
              className="t-label mt-1 text-muted-foreground"
              title={reference.role_reason || reference.tier_reason || undefined}
            >
              {sourceRoleLabel(reference)}
            </p>
            <ResearchSourceEvidence reference={reference} ledger={ledger} label={t.report.sourceDetails} />
          </li>
        ))}
      </ol>
    </section>
  )
}

function ResearchSourceEvidence({ reference, ledger, label }: {
  reference: ReportReference
  ledger?: Record<string, unknown>
  label: string
}) {
  const [bound] = agentArtifactReferences([reference])
  const lineage = bound
    ? evidenceLineageFromArtifactPayload({ web_search_ledger: ledger ?? { searches: {} } }, bound)
    : null
  const statements = reference.statements ?? []
  if (!lineage && statements.length === 0) return null
  return <AgentDisclosure summary={label} testId="research-source-evidence">
    {statements.length > 0 && <ReferenceStatements statements={statements} />}
    {bound && lineage && <EvidenceProvenancePanel reference={bound} lineage={lineage} />}
  </AgentDisclosure>
}

/** What the research ties to one source: each statement with whether this
 * source's own text was checked, else the claim's status. */
export function ReferenceStatements({ statements }: {
  statements: NonNullable<ReportReference['statements']>
}) {
  const { t } = useLocale()
  return (
    <section className="mb-3" data-testid="research-source-statements">
      <h4 className="t-label text-muted-foreground">{t.report.sourceStatements}</h4>
      <ul className="mt-1 space-y-1">
        {statements.map((statement, index) => (
          <li className="text-sm leading-6" key={`${index}-${statement.text}`}>
            <span className="text-foreground">{statement.text}</span>{' '}
            <span className="t-label text-muted-foreground">
              ({statement.checked ? t.report.statementChecked : claimStatusLabel(statement.status, t)})
            </span>
          </li>
        ))}
      </ul>
    </section>
  )
}

export function ReportEvidence({ run }: { run: ResearchRunRecord }) {
  const { t } = useLocale()
  const metrics = run.result?.metrics
  const references = run.result?.references ?? []
  const { cited: citedReferences, examined: examinedReferences } = partitionReportReferences(references)
  const topSources = run.result?.topSources ?? []
  const topClaims = run.result?.topClaims ?? []
  const fallbackSources = references.length === 0 ? topSources : []

  if (metrics || references.length > 0 || fallbackSources.length > 0 || topClaims.length > 0) {
    return (
      <div className="space-y-4">
        {metrics && (
          <section className="grid gap-2 sm:grid-cols-2">
            <EvidenceMetric label={t.common.confidence} value={`${metrics.confidence ?? 0} / 10`} />
            <EvidenceMetric label={t.runCard.sources} value={metrics.total_citations ?? 0} />
            <EvidenceMetric label={t.report.sourceQuality} value={formatScore(metrics.sources?.quality_score)} />
            <EvidenceMetric label={t.report.claimQuality} value={formatScore(metrics.claims?.quality_score)} />
            <EvidenceMetric label={t.report.evidenceContract} value={metrics.evidence_contract_status ?? 'unknown'} />
            <EvidenceMetric label={t.report.aspectCoverage} value={formatPercent(metrics.aspect_coverage)} />
          </section>
        )}
        {citedReferences.length > 0 && (
          <ReferenceList heading={t.report.reportReferences} references={citedReferences} ledger={run.result?.webSearchLedger} />
        )}
        {examinedReferences.length > 0 && (
          <ReferenceList heading={t.report.examinedSources} references={examinedReferences} ledger={run.result?.webSearchLedger} />
        )}
        {fallbackSources.length > 0 && (
          <section>
            <h3 className="t-section text-foreground">{t.runCard.sources}</h3>
            <ol className="mt-2 space-y-2">
              {fallbackSources.map((source) => (
                <li
                  className="rounded-md border border-border bg-background p-3 text-sm"
                  key={source.url}
                >
                  <a
                    className="break-words font-medium text-foreground hover:text-brand"
                    href={source.url}
                    rel="noreferrer"
                    target="_blank"
                  >
                    {source.url}
                  </a>
                  <p className="t-label mt-1 text-muted-foreground">{source.tier}</p>
                </li>
              ))}
            </ol>
          </section>
        )}
        {topClaims.length > 0 && (
          <section>
            <h3 className="t-section text-foreground">{t.report.claims}</h3>
            <ol className="mt-2 space-y-2">
              {topClaims.slice(0, 10).map((claim, index) => (
                <li
                  className="rounded-md border border-border bg-background p-3 text-sm leading-6"
                  key={`${claim.status}-${index}-${claim.text}`}
                >
                  <p className="text-foreground">{claim.text}</p>
                  <p className="t-label mt-1 text-muted-foreground">
                    {claimStatusLabel(claim.status, t)} · {t.report.claimSupport
                      .replace('{support}', String(claim.support_count))
                      .replace('{contradict}', String(claim.contradict_count))}
                  </p>
                </li>
              ))}
            </ol>
          </section>
        )}
      </div>
    )
  }

  return (
    <div className="rounded-lg border border-dashed border-border bg-background p-6 text-sm leading-6 text-muted-foreground">
      {t.report.evidencePending}
    </div>
  )
}

/** A claim status in the reader's language; a status this version does not
 * know (a newer server) stays as written. */
export function claimStatusLabel(
  status: string,
  t: ReturnType<typeof useLocale>['t'],
): string {
  if (status === 'verified') return t.report.claimStatusVerified
  if (status === 'attributed') return t.report.claimStatusAttributed
  if (status === 'contested') return t.report.claimStatusContested
  if (status === 'unverified') return t.report.claimStatusUnverified
  return status
}

function displayAgentEventTitle(
  title: string,
  t: ReturnType<typeof useLocale>['t'],
) {
  if (title === 'Run completed') return t.report.runCompletedEvent
  if (title === 'Run cancelled') return t.report.runCancelledEvent
  if (title === 'Cancellation requested') return t.report.cancellationRequestedEvent
  if (title === 'Run failed') return t.report.runFailedEvent
  return title
}

function isDisplayableAgentEvent(event: ResearchRunRecord['events'][number]) {
  const title = event.title.trim()
  return !(
    /^run snapshot$/i.test(title)
    || /^queued$/i.test(title)
    || /^run started$/i.test(title)
    || /^started\s+\w+/i.test(title)
    || /^finished\s+\w+/i.test(title)
  )
}

function EvidenceMetric({
  label,
  value,
}: {
  label: string
  value: number | string
}) {
  return (
    <div className="rounded-md border border-border bg-background p-3">
      <p className="t-label text-muted-foreground">{label}</p>
      <p className="mt-1 text-sm font-semibold text-foreground">{value}</p>
    </div>
  )
}

function formatScore(value: number | undefined) {
  if (value === undefined) return 'n/a'
  return value.toFixed(2)
}

function formatPercent(value: number | undefined) {
  if (value === undefined) return 'n/a'
  return `${Math.round(value * 100)}%`
}

function formatTime(iso: string) {
  return new Intl.DateTimeFormat('de-DE', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(iso))
}

function ReportExport({
  markdown,
  onSetAutocomplete,
  run,
}: {
  markdown: string | null
  onSetAutocomplete?: (runId: string, includeInAutocomplete: boolean) => void
  run: ResearchRunRecord
}) {
  const { t } = useLocale()
  const hasReport = Boolean(markdown)
  const [isExportingWord, setIsExportingWord] = useState(false)
  const [wordFailed, setWordFailed] = useState(false)
  const mentionAvailable = run.includeInAutocomplete !== false

  function handleExportMarkdown() {
    if (!markdown) return

    // The file leaves the app, so it carries both channels: front matter for
    // machines, a trailing blockquote for whoever opens it.
    downloadMarkdownFile(
      withFrontmatter(
        aiDisclosureFrontmatter(t.aiTransparency.exportNotice),
        withAiDisclosure(markdown, t.aiTransparency.exportNotice),
      ),
      reportMarkdownFileName(run),
    )
  }

  async function handleExportWord() {
    if (!markdown || isExportingWord) return
    setWordFailed(false)
    setIsExportingWord(true)
    try {
      // Reuse the editor's markdown->docx export (code-split, browser-only).
      const { exportMarkdownToDocx } = await import('@/features/editor/export/docxExport')
      await exportMarkdownToDocx(markdown.trimEnd(), run.summary.title, {
        notice: t.aiTransparency.exportNotice,
      })
    } catch (error) {
      setWordFailed(true)
      console.error('Inqtrix report Word export failed.', error)
    } finally {
      setIsExportingWord(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="rounded-lg border border-border bg-surface p-4">
        <h3 className="t-section text-foreground">{t.report.exportTitle}</h3>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">
          {hasReport ? t.report.exportReadyDescription : t.report.exportDescription}
        </p>
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <Button
            className="gap-2"
            disabled={!hasReport}
            onClick={handleExportMarkdown}
            type="button"
            variant="outline"
          >
            <Download className="size-4" />
            <span>{t.report.exportMarkdown}</span>
          </Button>
          <Button
            className="gap-2"
            disabled={!hasReport || isExportingWord}
            onClick={() => void handleExportWord()}
            type="button"
            variant="outline"
          >
            {isExportingWord ? (
              <LoaderCircle className="size-4 motion-safe:animate-spin" />
            ) : (
              <FileText className="size-4" />
            )}
            <span>{t.report.exportWord}</span>
          </Button>
        </div>
        {wordFailed ? (
          <p className="mt-2 text-sm text-destructive">{t.report.exportWordFailed}</p>
        ) : null}
      </div>

      {onSetAutocomplete ? (
        <div className="rounded-lg border border-border bg-surface p-4">
          <h3 className="t-section text-foreground">{t.report.mentionAvailableTitle}</h3>
          <div className="mt-3 flex items-start justify-between gap-4">
            <div className="min-w-0">
              <p className="t-list text-foreground">{t.report.mentionAvailable}</p>
              <p className="mt-1 t-meta text-muted-foreground">{t.report.mentionAvailableHint}</p>
            </div>
            <Switch
              aria-label={t.report.mentionAvailable}
              checked={mentionAvailable}
              onCheckedChange={(checked) => onSetAutocomplete(run.runId, checked)}
            />
          </div>
        </div>
      ) : null}
    </div>
  )
}

function downloadMarkdownFile(markdown: string, fileName: string) {
  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = fileName
  link.style.display = 'none'

  document.body.appendChild(link)
  link.click()
  document.body.removeChild(link)
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

function reportMarkdownFileName(run: ResearchRunRecord) {
  const date = run.submittedAt.slice(0, 10).replace(/-/g, '')
  const title = sanitizeFileNameSegment(run.summary.title)
  const runId = sanitizeFileNameSegment(run.runId)
  return `${date}_${runId}_${title}.md`
}

function sanitizeFileNameSegment(value: string) {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 72) || 'report'
}

function EmptyReportPanel() {
  const { t } = useLocale()

  return (
    <div className="relative flex flex-1">
      <div className="absolute inset-x-6 bottom-12 top-4 flex items-center justify-center text-center lg:bottom-40 lg:top-0">
        <div className="flex max-w-64 flex-col items-center">
          <span className="grid size-10 place-items-center rounded-xl border border-border bg-surface text-muted-foreground shadow-[0_1px_2px_var(--shadow-hairline)]">
            <FileText className="icon-md" />
          </span>
          <h3 className="mt-3 t-section text-foreground">
            {t.report.emptyTitle}
          </h3>
          <p className="mt-1 max-w-56 t-meta text-muted-foreground">
            {t.report.emptyDescription}
          </p>
        </div>
      </div>
    </div>
  )
}
