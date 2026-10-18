import { useState } from 'react'
import { useReducedMotion } from 'motion/react'
import { AlertTriangle, ChevronDown, Clock3, Info, Layers, Link, RotateCcw, Sparkles, Users, XCircle } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'
import { OptionMenuHeader, OptionMenuItem, optionMenuContentClassName } from '@/components/ui/option-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import type { Locale, TranslationDictionary } from '@/i18n/translations'
import { cn } from '@/lib/utils'
import { formatDurationMsShort } from '@/lib/time'
import type {
  EmbedModelDescriptor,
  EmbedModelId,
  IndexingJobLive,
  VectorIndexRecord,
  VectorIndexRunHistoryEntry,
  VectorIndexRunResult,
  VectorIndexStatus,
} from '@/features/project/types'
import type { VectorIndexMemberResolved } from '@/features/project/selectors'
import type { EmbeddingQuota } from '@/features/quota/useEmbeddingQuota'
import { ConfirmDelete } from './controls'
import { IndexPauseNotice } from './IndexPauseNotice'
import { indexPhaseText } from './indexPhaseText'
import { allowsRawRebuild } from './pauseNotice'
import { formatAddedAt, indexVectorCount } from './helpers'

const STATUS_STYLES: Record<VectorIndexStatus, { badge: string; dot: string; pulse: boolean }> = {
  ready: { badge: 'border-success/25 bg-success-subtle text-success', dot: 'bg-success', pulse: false },
  indexing: { badge: 'border-brand/25 bg-brand-subtle text-brand', dot: 'bg-brand', pulse: true },
  stale: { badge: 'border-warning/25 bg-warning-subtle text-warning', dot: 'bg-warning', pulse: false },
  error: { badge: 'border-destructive/25 bg-destructive-subtle text-destructive', dot: 'bg-destructive', pulse: false },
  deleting: { badge: 'border-warning/25 bg-warning-subtle text-warning', dot: 'bg-warning', pulse: true },
  delete_failed: { badge: 'border-destructive/25 bg-destructive-subtle text-destructive', dot: 'bg-destructive', pulse: false },
}

const HISTORY_RESULT_DOT: Record<VectorIndexRunResult, string> = {
  ok: 'bg-success',
  error: 'bg-destructive',
  cancelled: 'bg-warning',
}

function formatUpdated(iso: string, locale: Locale): string {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return iso
  return date.toLocaleDateString(locale, { day: '2-digit', month: 'short' })
}

/** Short name of the server's reason why no index can be built, for the
 * index mode and the rail's disabled "new index". */
export function knowledgeUnavailableLabel(reason: string, t: TranslationDictionary): string {
  return reason === 'store_unreachable'
    ? t.vectorIndex.modeStoreUnreachable
    : t.vectorIndex.modeKnowledgeOff
}

export function IndexBar({
  embedModels,
  deleting = false,
  embeddingQuota = null,
  index,
  live = null,
  members,
  onCancel,
  onDelete,
  onModel,
  onReindex,
  onRetryDelete,
  onResume,
  onResumeRaw,
  onShare,
  onOpenServerCollection,
  actionError = null,
  recoveryPending = null,
  serverBacked,
  unavailableReason = null,
  contextualRetrievalEnabled = null,
  serverFeatureLabels = null,
}: {
  embedModels: readonly EmbedModelDescriptor[]
  /** The aggregate server deletion is running; the index remains visible
   * until both its backing collection and durable index record are gone. */
  deleting?: boolean
  /** The caller's embedding-token usage; ``null`` when quotas don't apply. */
  embeddingQuota?: EmbeddingQuota | null
  index: VectorIndexRecord
  /** Live reindex progress for this index, or ``null`` when idle. */
  live?: IndexingJobLive | null
  members: VectorIndexMemberResolved[]
  onCancel: (indexId: string) => void
  onDelete: (indexId: string) => void
  onModel: (indexId: string, model: EmbedModelId) => void
  onReindex: (indexId: string) => void
  onRetryDelete?: () => void
  onResume: (indexId: string) => void
  onResumeRaw: (indexId: string) => void
  /** Share the index's backing knowledge collection. Absent when the index has
   * no server collection yet or the caller does not own it. */
  onShare?: () => void
  /** Open the index's backing collection on the server. The collection is the
   * index's storage and is not listed separately, so this is the only way to
   * reach documents that live in it without a local member. */
  onOpenServerCollection?: () => void
  /** Why the last index action failed, e.g. the server refused to delete the
   * backing collection. Rendered so a refusal is never silent. */
  actionError?: string | null
  /** A resume mutation is awaiting server acknowledgement. Keeping the exact
   * mode visible prevents double-submit and an ambiguous quality downgrade. */
  recoveryPending?: 'raw' | 'resume' | null
  serverBacked: boolean
  /** The server's reason (``operator_disabled``, ``store_unreachable``)
   * when this deployment has no knowledge base to build into: the index is
   * then read-only. ``null`` in demo mode and whenever knowledge works. */
  unavailableReason?: string | null
  /** Capability truth from the server. ``null`` means discovery has not
   * completed, so the UI must not claim either enrichment mode. */
  contextualRetrievalEnabled?: boolean | null
  serverFeatureLabels?: string[] | null
}) {
  const { locale, t } = useLocale()
  const reduceMotion = useReducedMotion() ?? false
  const [historyOpen, setHistoryOpen] = useState(false)
  const indexing = index.status === 'indexing'
  const paused = live?.status === 'paused_dependency' || live?.status === 'paused_validation'
  const pausedPhase = live
    ? indexPhaseText(live.phase, live.currentBatch ?? 0, live.totalBatches ?? 0, t)
    : null
  // A paused client run can wait on several documents; its one action
  // continues all of them.
  const pausedDocuments = live?.source === 'build'
    ? Object.values(live.memberProgress ?? {}).filter((progress) => (
        progress.status === 'paused_dependency' || progress.status === 'paused_validation'
      )).length
    : 0
  const recovering = recoveryPending !== null
  const readOnly = unavailableReason !== null
  const storeUnreachable = unavailableReason === 'store_unreachable'
  const busy = indexing || deleting || index.status === 'deleting'
  // Not-yet-indexed members: when present, the top action indexes just those new
  // documents (incremental, per-file progress); with none it refreshes the whole
  // collection. So the label/icon adapts to what the click will actually do.
  const pendingCount = members.filter((member) => member.member.state === 'pending').length
  // Reindex re-embeds every chunk, so a reached embedding budget blocks
  // it on the server path (the local simulation never gates).
  const quotaBlocked = serverBacked && (embeddingQuota?.exhausted ?? false)
  const stale = index.status === 'stale'
  const style = paused
    ? { badge: 'border-warning/25 bg-warning-subtle text-warning', dot: 'bg-warning', pulse: false }
    : STATUS_STYLES[index.status]
  const history = index.history ?? []
  const statusLabel =
    paused
      ? live?.status === 'paused_validation'
        ? t.vectorIndex.statusPausedValidation
        : t.vectorIndex.statusPausedDependency
      : index.status === 'ready'
      ? t.vectorIndex.statusReady
      : index.status === 'indexing'
        ? t.vectorIndex.statusIndexing
        : index.status === 'error'
          ? t.vectorIndex.statusError
          : index.status === 'deleting'
            ? t.vectorIndex.statusDeleting
            : index.status === 'delete_failed'
              ? t.vectorIndex.statusDeleteFailed
          : t.vectorIndex.statusStale
  const stats: { hint?: string; label: string; value: string }[] = [
    { label: t.vectorIndex.dimensions, value: index.dims.toLocaleString(locale) },
    {
      hint: t.vectorIndex.vectorsEstimateHint,
      label: t.vectorIndex.vectors,
      value: indexVectorCount(members).toLocaleString(locale),
    },
    { label: t.vectorIndex.documents, value: members.length.toLocaleString(locale) },
    { label: t.vectorIndex.updated, value: formatUpdated(index.updatedAt, locale) },
  ]
  const currentModel: EmbedModelDescriptor =
    embedModels.find((model) => model.id === index.model)
      ?? { dims: index.dims, id: index.model, label: index.model, provider: '' }
  const modeLabel = serverBacked
    ? t.vectorIndex.modeServer
    : unavailableReason !== null
      ? knowledgeUnavailableLabel(unavailableReason, t)
      : t.vectorIndex.modeDemo

  const quotaResetLabel =
    embeddingQuota && embeddingQuota.resetAt > 0
      ? new Date(embeddingQuota.resetAt * 1000).toLocaleDateString(locale, {
          day: 'numeric',
          month: 'long',
          timeZone: 'UTC',
        })
      : ''

  return (
    <div className="rounded-lg border border-border bg-card p-3.5 shadow-[0_1px_2px_var(--shadow-hairline)]">
      <div className="flex flex-wrap items-center gap-3">
        <span className="grid size-10 shrink-0 place-items-center rounded-lg border border-file/25 bg-file-subtle text-file">
          <Layers className="size-5" />
        </span>
        <div className="min-w-0 flex-1">
          <h3 className="t-card text-foreground">{t.vectorIndex.title}</h3>
          <div className="mt-1 flex flex-wrap items-center gap-2">
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="inline-flex shrink-0 cursor-help items-center gap-1 whitespace-nowrap rounded border border-border bg-surface px-1.5 py-0.5 font-mono t-meta-sm text-muted-foreground">
                  <Link className="size-3" />@index:{index.handle}
                </span>
              </TooltipTrigger>
              <TooltipContent side="top">{t.vectorIndex.indexHandleTooltip}</TooltipContent>
            </Tooltip>
            <span className={cn('inline-flex items-center gap-1.5 rounded-md border px-2 py-0.5 t-meta-sm font-semibold', style.badge)}>
              <span className={cn('size-1.5 rounded-full', style.dot, style.pulse && !reduceMotion && 'inqtrix-running-dot')} />
              {statusLabel}
            </span>
            <span
              className={cn(
                'inline-flex h-5 items-center rounded-md border px-1.5 t-hint font-semibold',
                serverBacked
                  ? 'border-success/20 bg-success-subtle text-success'
                  : 'border-border bg-surface text-muted-foreground',
              )}
            >
              {modeLabel}
            </span>
            <Tooltip>
              <TooltipTrigger asChild>
                <span className="inline-flex cursor-help items-center text-muted-foreground/70 hover:text-foreground">
                  <Info className="size-3.5" />
                </span>
              </TooltipTrigger>
              <TooltipContent
                className="max-w-[320px] border border-border bg-popover p-2.5 text-popover-foreground shadow-lg"
                side="top"
              >
                <p className="t-label text-foreground">{modeLabel}</p>
                <p className="mt-1 t-meta-sm text-muted-foreground">{t.vectorIndex.referenceHint}</p>
                <p className="mt-1.5 t-meta-sm text-muted-foreground">
                  {serverBacked ? t.vectorIndex.serverNote : t.vectorIndex.simulationNote}
                </p>
                {serverBacked && serverFeatureLabels && serverFeatureLabels.length > 0 ? (
                  <p className="mt-1.5 t-hint text-muted-foreground">
                    {t.vectorIndex.serverFeaturesLabel}: {serverFeatureLabels.join(' · ')}
                  </p>
                ) : null}
              </TooltipContent>
            </Tooltip>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2 self-center">
          {/* When quotaBlocked, the reason is the always-visible exhausted
              banner below (keyboard/SR reachable) — no disabled-button tooltip. */}
          <Button
            className="gap-1.5"
            disabled={readOnly || recovering || (busy && !paused) || (quotaBlocked && !paused)}
            onClick={() => paused ? onResume(index.id) : onReindex(index.id)}
            size="sm"
            type="button"
            variant="outline"
          >
            {!indexing && pendingCount > 0 ? (
              <Sparkles className="size-4" />
            ) : (
              <RotateCcw className={cn(
                'size-4',
                (indexing && !paused) || recoveryPending === 'resume'
                  ? 'motion-safe:animate-spin'
                  : null,
              )} />
            )}
            {recoveryPending === 'resume'
              ? t.vectorIndex.resumeRequesting
              : paused && pausedDocuments > 1
              ? t.vectorIndex.resumeAllIndexing.replace('{count}', String(pausedDocuments))
              : paused
              ? t.vectorIndex.resumeIndexing
              : indexing
              ? t.vectorIndex.reindexing
              : pendingCount > 0
                ? t.vectorIndex.reindexPending.replace('{count}', String(pendingCount))
                : t.vectorIndex.reindex}
          </Button>
          {indexing ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  aria-label={t.vectorIndex.cancelIndexing}
                  className="size-8 px-0 text-muted-foreground hover:text-destructive"
                  disabled={recovering}
                  onClick={() => onCancel(index.id)}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  <XCircle className="size-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top">{t.vectorIndex.cancelIndexing}</TooltipContent>
            </Tooltip>
          ) : null}
          {onOpenServerCollection && !busy ? (
            <Tooltip>
              <TooltipTrigger asChild>
                <Button
                  aria-label={t.vectorIndex.openServerCollection}
                  className="size-8 px-0 text-muted-foreground hover:text-foreground"
                  onClick={onOpenServerCollection}
                  size="sm"
                  type="button"
                  variant="ghost"
                >
                  <Layers className="size-4" />
                </Button>
              </TooltipTrigger>
              <TooltipContent side="top">{t.vectorIndex.openServerCollection}</TooltipContent>
            </Tooltip>
          ) : null}
          {onShare && !busy ? (
            <Button className="gap-1.5" onClick={onShare} size="sm" type="button" variant="outline">
              <Users className="size-4" />
              {t.sharing.share}
            </Button>
          ) : null}
          {deleting ? (
            <span
              aria-live="polite"
              className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-surface px-3 text-sm font-medium text-muted-foreground"
              role="status"
            >
              <Clock3 className={cn('size-4', !reduceMotion && 'animate-pulse')} />
              {t.vectorIndex.deleting}
            </span>
          ) : (
            <ConfirmDelete
              ariaLabel={t.vectorIndex.remove}
              disabled={busy}
              hint={t.vectorIndex.removeHint}
              label={t.vectorIndex.remove}
              onConfirm={() => onDelete(index.id)}
            />
          )}
        </div>
      </div>

      {/* One stats row — the running progress lives on the RIGHT of it (no
          separate row), matching the rest of the design language. */}
      <div className={cn(
        'mt-3 flex flex-wrap items-end gap-x-6 gap-y-2 border-t border-border/70 pt-3',
        indexing && 'md:flex-nowrap',
      )}>
        <div className="min-w-0">
          <span className="block t-caption font-semibold text-muted-foreground/80">{t.vectorIndex.embeddingModel}</span>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                aria-label={t.vectorIndex.embeddingModel}
                className="mt-1 h-7 gap-1.5 px-2 font-mono text-xs font-semibold"
                disabled={busy || Boolean(index.serverCollectionId)}
                size="sm"
                type="button"
                variant="outline"
              >
                <span className="truncate">{currentModel.label}</span>
                <ChevronDown className="text-muted-foreground" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className={optionMenuContentClassName} sideOffset={6}>
              <OptionMenuHeader count={embedModels.length} title={t.vectorIndex.embeddingModel} value={currentModel.label} />
              <div className="py-1">
                {embedModels.map((model) => (
                  <OptionMenuItem
                    active={model.id === index.model}
                    description={`${model.provider} · ${model.dims.toLocaleString(locale)}`}
                    icon={Layers}
                    key={model.id}
                    label={model.label}
                    onSelect={() => onModel(index.id, model.id)}
                  />
                ))}
              </div>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        {stats.map(({ hint, label, value }) => (
          <div className="min-w-0" key={label}>
            {hint ? (
              <Tooltip>
                <TooltipTrigger asChild>
                  <span
                    aria-label={hint}
                    className="inline-flex cursor-help items-center gap-1 t-caption font-semibold text-muted-foreground/80 outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                    tabIndex={0}
                  >
                    {label}
                    <Info aria-hidden className="size-3" />
                  </span>
                </TooltipTrigger>
                <TooltipContent className="max-w-[280px]" side="top">{hint}</TooltipContent>
              </Tooltip>
            ) : (
              <p className="t-caption font-semibold text-muted-foreground/80">{label}</p>
            )}
            <p className="mt-0.5 truncate t-list font-semibold tabular-nums text-foreground">{value}</p>
          </div>
        ))}
        {!indexing && history.length > 0 ? (
          <button
            aria-expanded={historyOpen}
            className="ml-auto inline-flex h-7 items-center gap-1.5 rounded-md border border-border bg-surface px-2 t-hint font-semibold text-muted-foreground transition-colors hover:text-foreground"
            onClick={() => setHistoryOpen((open) => !open)}
            type="button"
          >
            <ChevronDown className={cn('size-3.5 transition-transform', !reduceMotion && 'duration-200', !historyOpen && '-rotate-90')} />
            <Clock3 className="size-3.5" />
            <span>{t.vectorIndex.historyHeading}</span>
            <span className="text-muted-foreground/70">({history.length})</span>
          </button>
        ) : null}
        {indexing ? (
          <div className="ml-auto w-full min-w-0 md:w-48 md:shrink-0">
            <RunningIndexProgress live={live} reduceMotion={reduceMotion} t={t} />
          </div>
        ) : null}
      </div>

      {actionError ? (
        <div className="mt-2.5 flex items-center justify-between gap-2 rounded-md border border-destructive/25 bg-destructive-subtle px-2.5 py-1.5">
          <span className="inline-flex min-w-0 items-center gap-1.5 t-meta-sm font-medium text-destructive">
            <AlertTriangle className="size-3.5 shrink-0" />
            <span className="min-w-0 [overflow-wrap:anywhere]">{actionError}</span>
          </span>
          {onRetryDelete ? (
            <Button onClick={onRetryDelete} size="sm" type="button" variant="outline">
              {t.vectorIndex.errorRetry}
            </Button>
          ) : null}
        </div>
      ) : null}

      {paused && live ? (
        <div
          className="mt-2.5 rounded-md border border-warning/30 bg-warning-subtle px-3 py-2 text-warning"
          role="status"
        >
          <p className="t-meta font-semibold">
            {live.status === 'paused_validation'
              ? t.vectorIndex.pausedValidationTitle
              : t.vectorIndex.pausedDependencyTitle}
          </p>
          <IndexPauseNotice
            autoResume={live.autoResume}
            errorType={live.pauseErrorType}
            pausedDocuments={pausedDocuments}
            status={live.status}
          />
          <p className="mt-1 t-hint text-warning/90">
            {/* One job's checkpoint; a paused run of several documents shows
                each document's state in its row instead. */}
            {pausedPhase ? `${pausedPhase} · ` : null}
            {t.vectorIndex.activeGenerationUnchanged}
          </p>
          {allowsRawRebuild(live.status, live.pauseErrorType) ? (
            <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-warning/20 pt-2">
              <p className="max-w-3xl t-hint text-warning/90">
                {t.vectorIndex.resumeWithoutContextHint}
              </p>
              <Button
                className="shrink-0 border-warning/35 text-warning hover:bg-warning/10 hover:text-warning"
                disabled={recovering}
                onClick={() => onResumeRaw(index.id)}
                size="sm"
                type="button"
                variant="outline"
              >
                {recoveryPending === 'raw'
                  ? t.vectorIndex.resumeWithoutContextRequesting
                  : t.vectorIndex.resumeWithoutContext}
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}

      {indexing && !paused && live?.source === 'build' ? (
        // Honest expectation-setting: the client-driven build embeds document
        // by document on the server, so minutes without a visible jump are
        // normal — say so instead of letting it read as a hang.
        <p className="mt-2.5 t-meta-sm text-muted-foreground">
          {contextualRetrievalEnabled === true
            ? t.vectorIndex.buildDurationHintContextual
            : contextualRetrievalEnabled === false
              ? t.vectorIndex.buildDurationHintRaw
              : t.vectorIndex.buildDurationHint}
        </p>
      ) : null}

      {quotaBlocked ? (
        <div className="mt-2.5 flex items-center gap-2 rounded-md border border-destructive/25 bg-destructive-subtle px-2.5 py-1.5">
          <span className="inline-flex min-w-0 items-center gap-1.5 t-meta-sm font-medium text-destructive">
            <AlertTriangle className="size-3.5 shrink-0" />
            <span>{t.vectorIndex.embeddingQuotaBanner(quotaResetLabel)}</span>
          </span>
        </div>
      ) : null}

      {/* Like the quota banner: the reason for the disabled actions stays
          visible instead of hiding in a disabled-button tooltip. */}
      {readOnly ? (
        <div className="mt-2.5 flex items-center gap-2 rounded-md border border-warning/25 bg-warning-subtle px-2.5 py-1.5">
          <span className="inline-flex min-w-0 items-center gap-1.5 t-meta-sm font-medium text-warning">
            <AlertTriangle className="size-3.5 shrink-0" />
            <span>
              {storeUnreachable
                ? t.vectorIndex.readOnlyStoreUnreachable
                : t.vectorIndex.readOnlyKnowledgeOff}
            </span>
          </span>
        </div>
      ) : null}

      {index.status === 'error' ? (
        <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2 rounded-md border border-destructive/25 bg-destructive-subtle px-2.5 py-1.5">
          <span className="inline-flex min-w-0 items-center gap-1.5 t-meta-sm font-medium text-destructive">
            <AlertTriangle className="size-3.5 shrink-0" />
            <span className="truncate">{index.lastError || t.vectorIndex.errorNotice}</span>
          </span>
          <button
            className="shrink-0 rounded-md border border-destructive/30 bg-card px-2 py-1 t-meta-sm font-semibold text-destructive hover:bg-destructive-subtle disabled:opacity-50"
            disabled={readOnly}
            onClick={() => onReindex(index.id)}
            type="button"
          >
            {t.vectorIndex.errorRetry}
          </button>
        </div>
      ) : null}

      {stale && !indexing ? (
        <div className="mt-2.5 flex flex-wrap items-center justify-between gap-2 rounded-md border border-warning/25 bg-warning-subtle px-2.5 py-1.5">
          <span className="inline-flex items-center gap-1.5 t-meta-sm font-medium text-warning">
            <AlertTriangle className="size-3.5" />
            {t.vectorIndex.staleWarning}
          </span>
          <button
            className="shrink-0 rounded-md border border-warning/30 bg-card px-2 py-1 t-meta-sm font-semibold text-warning hover:bg-warning-subtle disabled:opacity-50"
            disabled={readOnly}
            onClick={() => onReindex(index.id)}
            type="button"
          >
            {t.vectorIndex.staleAction}
          </button>
        </div>
      ) : null}

      {!indexing && history.length > 0 && historyOpen ? (
        <IndexHistory
          entries={history}
          locale={locale}
          open={historyOpen}
          reduceMotion={reduceMotion}
          t={t}
        />
      ) : null}
    </div>
  )
}

/** Compact running indicator: percent + documents + a determinate bar.
 * Falls back to an indeterminate breathing bar when no live entry exists
 * (e.g. an ``indexing`` status restored from a manifest without a job). */
function RunningIndexProgress({
  live,
  reduceMotion,
  t,
}: {
  live: IndexingJobLive | null
  reduceMotion: boolean
  t: TranslationDictionary
}) {
  if (!live) {
    return (
      <div className="w-full" aria-atomic="true" aria-live="polite">
        <span className="block text-right t-meta-sm font-medium text-brand">
          {t.vectorIndex.progressLabel}
        </span>
        <span className="mt-1 block h-1.5 w-full overflow-hidden rounded-full bg-brand/15" aria-hidden>
          <span className={cn('block h-full rounded-full bg-brand', !reduceMotion && 'inqtrix-segment-breathe')} />
        </span>
      </div>
    )
  }
  if (live.queuePosition != null) {
    // Waiting for a free slot — show the FIFO position, not a stalled 0 %
    // (matches the research-run queue display). Indeterminate breathing bar.
    return (
      <div className="w-full" aria-atomic="true" aria-live="polite">
        <span className="block text-right t-meta-sm font-medium text-muted-foreground">
          {t.vectorIndex.queuedPosition(live.queuePosition)}
        </span>
        <span className="mt-1 block h-1.5 w-full overflow-hidden rounded-full bg-brand/15" aria-hidden>
          <span className={cn('block h-full rounded-full bg-brand/60', !reduceMotion && 'inqtrix-segment-breathe')} />
        </span>
      </div>
    )
  }
  const percent = Math.min(100, Math.max(0, live.percent))
  const paused = live.status === 'paused_dependency' || live.status === 'paused_validation'
  return (
    <div className="w-full" aria-atomic="true" aria-live="polite">
      <div className={cn(
        'flex items-center justify-end gap-1.5 t-meta-sm font-semibold',
        paused ? 'text-warning' : 'text-brand',
      )}>
        <span className={cn(
          'size-1.5 rounded-full',
          paused ? 'bg-warning' : 'bg-brand',
          !paused && !reduceMotion && 'inqtrix-running-dot',
        )} aria-hidden />
        <span
          className={cn('tabular-nums', !reduceMotion && 'inqtrix-metric-flash')}
          key={`${percent}-${live.completedDocuments}`}
        >
          {countLabel(live.totalDocuments, t.vectorIndex.progressPercentDocsOne, t.vectorIndex.progressPercentDocs)
            .replace('{percent}', String(percent))
            .replace('{done}', String(live.completedDocuments))}
        </span>
      </div>
      <span className="mt-1 block h-1.5 w-full overflow-hidden rounded-full bg-brand/15" aria-hidden>
        <span
          className="block h-full rounded-full bg-brand transition-[width] duration-300 ease-out motion-reduce:transition-none"
          style={{ width: `${Math.max(3, percent)}%` }}
        />
      </span>
      {/* Each document embeds synchronously on the server, so the percentage
          can stand still for minutes. Naming the document being worked on is
          the difference between "running" and "looks dead". */}
      {live.currentDocumentTitle ? (
        <span className="mt-1 block truncate text-right t-meta-sm text-muted-foreground" title={live.currentDocumentTitle}>
          {live.currentDocumentTitle}
        </span>
      ) : null}
    </div>
  )
}

/** Collapsible "last N runs" list (start, duration, outcome). */
function IndexHistory({
  entries,
  locale,
  open,
  reduceMotion,
  t,
}: {
  entries: VectorIndexRunHistoryEntry[]
  locale: Locale
  open: boolean
  reduceMotion: boolean
  t: TranslationDictionary
}) {
  const resultLabel = (result: VectorIndexRunResult): string =>
    result === 'error'
      ? t.vectorIndex.historyResultError
      : result === 'cancelled'
        ? t.vectorIndex.historyResultCancelled
        : t.vectorIndex.historyResultOk
  return (
    <div className="mt-2.5 border-t border-border/70 pt-2.5">
      <div
        className={cn(
          'grid transition-[grid-template-rows]',
          !reduceMotion && 'duration-200 ease-out',
          open ? 'grid-rows-[1fr]' : 'grid-rows-[0fr]',
        )}
      >
        <div className="overflow-hidden">
          <ul className="mt-2 space-y-1.5">
            {entries.map((entry, position) => (
              <li
                className="flex flex-wrap items-center gap-x-2 gap-y-0.5 t-meta-sm text-muted-foreground"
                key={`${entry.finishedAt}-${position}`}
              >
                <span className={cn('size-1.5 shrink-0 rounded-full', HISTORY_RESULT_DOT[entry.result])} aria-hidden />
                <span className="tabular-nums">{t.vectorIndex.historyStarted(formatAddedAt(entry.startedAt, locale))}</span>
                <span className="text-muted-foreground/60">·</span>
                <span className="tabular-nums">{t.vectorIndex.historyRan(formatDurationMsShort(entry.durationMs))}</span>
                <span className="text-muted-foreground/60">·</span>
                <span className="font-medium text-foreground/80">{resultLabel(entry.result)}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  )
}
