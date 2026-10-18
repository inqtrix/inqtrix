import { memo, type DragEvent, type MouseEvent as ReactMouseEvent } from 'react'
import { Check, Eye, Folder, Info, Link, RotateCcw, Sparkles, X } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { cn } from '@/lib/utils'
import type {
  FileAssetRecord,
  IndexingMemberLive,
  VectorIndexMemberState,
} from '@/features/project/types'
import { StatusBadge } from '@/features/settings/parts'
import {
  ConfirmDelete,
  ContextOmittedLabel,
  InlineText,
  MoveMenu,
  ParserBadge,
  StatusMark,
  TypeBadge,
  TypeTile,
  type MoveTarget,
} from './controls'
import { chunkEstimate, formatAddedAt, formatAddedAtFull, formatBytes, ingestBadgeState, memberCellState, memberHasNoEmbeddableText, typeMeta } from './helpers'
import { FILE_DRAG_TYPE } from './constants'

export const EXPLORER_GRID = 'grid grid-cols-[minmax(16rem,1fr)_4.5rem_10rem_5rem_7rem_minmax(6rem,9rem)_6.25rem] items-center gap-3'

export type FileItemProps = {
  asset: FileAssetRecord
  mode: 'library' | 'index'
  breadcrumb?: string | null
  referenceCount?: number
  moveTargets?: MoveTarget[]
  memberState?: VectorIndexMemberState
  indexRemoval?: {
    error?: string
    status: 'blocked' | 'reconciling' | 'deleting' | 'delete_failed'
  }
  /** Whether the member's index is CURRENTLY running ANY embed job — gates the
   * per-row actions (no "remove" / no per-file "index this file" while a run is
   * in flight, "one job per index"). NOT the running-label gate — see `inRun`. */
  indexing?: boolean
  /** Whether THIS specific file is part of the current run's working set
   * (`runningFileIds`) — drives whether the row reads "läuft". */
  inRun?: boolean
  /** This file's server-confirmed live outcome during an active run. */
  liveProgress?: 'embedded' | 'skipped'
  /** Queue/phase facts from this file's durable document job. */
  jobProgress?: IndexingMemberLive
  /** Chunks of this member's server document that carry no context (the
   * provider's content filter blocked their batch); named beside "indexed". */
  contextOmittedChunks?: number
  source?: string | null
  onRename?: (fileId: string, label: string) => void
  onMove?: (fileId: string, sectionId: string, groupId: string | null) => void
  onDelete?: (fileId: string) => void
  onRemoveFromIndex?: (fileId: string) => void
  onRetryIndexRemoval?: () => void
  /** Index just THIS file (per-row action), shown for a not-yet-indexed
   * member when no run is in flight. */
  onIndexMember?: (fileId: string) => void
  onDragStart?: (fileId: string) => void
  onDragEnd?: () => void
  onPreview?: (fileId: string) => void
  /** Re-run a failed server upload from the retained File object. Shown only
   * while `canRetryUpload` confirms the bytes are still held (same session). */
  onRetryUpload?: (fileId: string) => void
  /** Resume the same durable deletion manifest after a terminal failure. */
  onRetryDeletion?: (operationId: string) => void
  canRetryUpload?: (fileId: string) => boolean
  /** Selection mode is active (>=1 selected or explicitly armed): checkboxes
   * are persistently visible, row click TOGGLES instead of previewing, and
   * per-row hover actions are suppressed. Without it the checkbox is
   * hover/focus-revealed as the mode's entry point. */
  selectionActive?: boolean
  selected?: boolean
  /** Toggle this file's selection. `range` = shift (anchor..target),
   * `additive` = ctrl/cmd toggle. Presence of the callback enables the
   * selection affordances (library mode only). */
  onToggleSelect?: (fileId: string, options: { additive?: boolean; range?: boolean }) => void
}

/** Circle checkbox at a row/card's leading edge:
 * hover/focus-revealed until selection mode is active, then persistent. Kept
 * in the accessibility tree at all times — the reveal is purely visual. */
function SelectCheck({
  label,
  onToggle,
  revealed,
  selected,
}: {
  label: string
  onToggle: (event: ReactMouseEvent) => void
  revealed: boolean
  selected: boolean
}) {
  return (
    <button
      aria-label={label}
      aria-pressed={selected}
      className={cn(
        'grid size-5 shrink-0 place-items-center rounded-full border transition-all',
        selected
          ? 'border-brand bg-brand text-brand-foreground'
          : 'border-border bg-surface text-transparent hover:border-brand/60',
        revealed ? 'opacity-100' : 'opacity-0 focus-visible:opacity-100 group-hover:opacity-100',
      )}
      onClick={(event) => {
        event.stopPropagation()
        onToggle(event)
      }}
      type="button"
    >
      <Check className="size-3" />
    </button>
  )
}

function ChunkCell({
  asset,
  contextOmittedChunks = 0,
  indexRemoval,
  jobProgress,
  state,
  inRun = false,
  liveProgress,
  stacked = false,
}: {
  asset: FileAssetRecord
  contextOmittedChunks?: number
  /** The card's narrow footer puts the omission count under "indexed"
   * instead of beside it. */
  stacked?: boolean
  indexRemoval?: FileItemProps['indexRemoval']
  jobProgress?: IndexingMemberLive
  state: VectorIndexMemberState
  /** Whether THIS file is part of the running job's working set
   * (`runningFileIds`). "läuft" is shown ONLY for files the active run actually
   * processes — a file outside the run keeps its real state, so indexing one new
   * document never makes the already-embedded rows read "läuft" (the prior bug). */
  inRun?: boolean
  /** During an active run, this file's server-CONFIRMED outcome so far:
   * `embedded` (done), `skipped` (no text), or undefined (not yet processed →
   * still running). Drives the live, per-file feedback. */
  liveProgress?: 'embedded' | 'skipped'
}) {
  const { locale, t } = useLocale()
  if (indexRemoval?.status === 'reconciling') {
    return (
      <span className="inline-flex h-5 items-center gap-1 rounded-md border border-brand/25 bg-brand-subtle px-1.5 t-hint font-medium text-brand" role="status">
        <span className="inqtrix-running-dot size-1.5 rounded-full bg-brand" />
        {t.vectorIndex.removalReconciling}
      </span>
    )
  }
  if (indexRemoval?.status === 'deleting') {
    return (
      <span className="inline-flex h-5 items-center gap-1 rounded-md border border-warning/25 bg-warning-subtle px-1.5 t-hint font-medium text-warning" role="status">
        <span className="size-1.5 rounded-full bg-warning motion-safe:animate-pulse" />
        {t.fileLibrary.deletionSearchDetached}
      </span>
    )
  }
  if (indexRemoval?.status === 'delete_failed') {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex h-5 cursor-help items-center rounded-md border border-warning/25 bg-warning-subtle px-1.5 t-hint font-medium text-warning">
            {t.fileLibrary.deletionFailed}
          </span>
        </TooltipTrigger>
        <TooltipContent side="top">{indexRemoval.error ?? t.fileLibrary.deletionFailed}</TooltipContent>
      </Tooltip>
    )
  }
  if (indexRemoval?.status === 'blocked') {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex h-5 cursor-help items-center rounded-md border border-warning/25 bg-warning-subtle px-1.5 t-hint font-medium text-warning">
            {t.vectorIndex.removalBlocked}
          </span>
        </TooltipTrigger>
        <TooltipContent side="top">{indexRemoval.error ?? t.vectorIndex.removalBlockedHint}</TooltipContent>
      </Tooltip>
    )
  }
  // A terminal per-file outcome always wins. Until then, show the durable
  // document job's exact queue/phase state instead of a generic spinner.
  if (!liveProgress && jobProgress?.status === 'queued') {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex h-5 cursor-help items-center rounded-md border border-border bg-muted/45 px-1.5 t-hint font-medium text-muted-foreground">
            {t.vectorIndex.memberQueued}
          </span>
        </TooltipTrigger>
        <TooltipContent side="top">
          {jobProgress.queuePosition != null
            ? t.vectorIndex.memberQueuedPosition(jobProgress.queuePosition)
            : t.vectorIndex.memberQueuedTooltip}
        </TooltipContent>
      </Tooltip>
    )
  }
  if (
    !liveProgress
    && (
      jobProgress?.status === 'paused_dependency'
      || jobProgress?.status === 'paused_validation'
    )
  ) {
    const dependency = jobProgress.status === 'paused_dependency'
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex h-5 cursor-help items-center rounded-md border border-warning/25 bg-warning-subtle px-1.5 t-hint font-medium text-warning">
            {dependency
              ? t.vectorIndex.memberPausedDependency
              : t.vectorIndex.memberPausedValidation}
          </span>
        </TooltipTrigger>
        <TooltipContent side="top">
          {dependency
            ? t.vectorIndex.pausedDependencyTitle
            : t.vectorIndex.pausedValidationTitle}
        </TooltipContent>
      </Tooltip>
    )
  }
  if (!liveProgress && jobProgress) {
    const contextualizing = jobProgress.phase === 'contextualization'
    const embeddingWaiting = jobProgress.phase === 'embedding_wait'
    const currentBatch = jobProgress.currentBatch ?? 0
    const totalBatches = jobProgress.totalBatches ?? 0
    const batchNumbersKnown = currentBatch > 0 && totalBatches > 0
    const hasBatchProgress = contextualizing && batchNumbersKnown
    const hasEmbeddingBatch =
      jobProgress.phase === 'embedding' && batchNumbersKnown
    const detail = jobProgress.status === 'cancelling'
      ? t.vectorIndex.memberCancelling
      : hasBatchProgress
        ? t.vectorIndex.memberContextBatch(currentBatch, totalBatches)
        : contextualizing
          ? t.vectorIndex.memberContext
          : embeddingWaiting
            ? t.vectorIndex.memberEmbeddingWait
            : hasEmbeddingBatch
              ? t.vectorIndex.memberEmbeddingBatch(currentBatch, totalBatches)
              : jobProgress.phase === 'embedding'
                ? t.vectorIndex.memberEmbedding
                : jobProgress.phase === 'validation'
                  ? t.vectorIndex.memberValidating
                  : jobProgress.phase === 'publication'
                    ? t.vectorIndex.memberPublishing
                    : t.vectorIndex.memberPreparing
    const tooltip = jobProgress.status === 'cancelling'
      ? t.vectorIndex.memberCancellingTooltip
      : hasBatchProgress
        ? t.vectorIndex.memberContextBatchTooltip(currentBatch, totalBatches)
      : contextualizing
        ? t.vectorIndex.memberContextTooltip
        : embeddingWaiting
          ? t.vectorIndex.memberEmbeddingWaitTooltip
          : hasEmbeddingBatch
            ? t.vectorIndex.memberEmbeddingBatchTooltip(
              currentBatch,
              totalBatches,
            )
            : jobProgress.phase === 'embedding'
              ? t.vectorIndex.memberEmbeddingTooltip
              : jobProgress.phase === 'validation'
                ? t.vectorIndex.memberValidatingTooltip
                : jobProgress.phase === 'publication'
                  ? t.vectorIndex.memberPublishingTooltip
                  : t.vectorIndex.memberPreparingTooltip
    return (
      <span className="flex min-w-0 items-center gap-1.5">
        <Tooltip>
          <TooltipTrigger asChild>
            <span className="inline-flex h-5 shrink-0 cursor-help items-center gap-1 whitespace-nowrap rounded-md border border-brand/25 bg-brand-subtle px-1.5 t-hint font-medium text-brand">
              <span className="inqtrix-running-dot size-1.5 rounded-full bg-brand" />
              {t.vectorIndex.embeddingRunning}
            </span>
          </TooltipTrigger>
          <TooltipContent side="top">{tooltip}</TooltipContent>
        </Tooltip>
        <span
          className="min-w-0 whitespace-nowrap t-hint tabular-nums text-muted-foreground"
          title={detail}
        >
          {detail}
        </span>
      </span>
    )
  }
  // The server-confirmed live outcome wins (each row flips as it lands); a file
  // in the run but not yet confirmed shows the pulsing "läuft"; everything else
  // keeps its persisted state (No-Silent-Fallbacks: only an in-run file pulses,
  // never forever and never a row outside the run).
  const effective = memberCellState(state, inRun, liveProgress)
  if (effective === 'running') {
    // A brand status pill with the pulsing dot — same table density as the other
    // states and the same tone as the index-level "Indexiere…" badge.
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex h-5 cursor-help items-center gap-1 rounded-md border border-brand/25 bg-brand-subtle px-1.5 t-hint font-medium text-brand">
            <span className="inqtrix-running-dot size-1.5 rounded-full bg-brand" />
            {t.vectorIndex.embeddingRunning}
          </span>
        </TooltipTrigger>
        <TooltipContent side="top">{t.vectorIndex.embeddingRunningTooltip}</TooltipContent>
      </Tooltip>
    )
  }
  if (effective === 'embedded') {
    // Explicit "Indexiert" status (best practice) instead of a bare number; the
    // chunk count moves into the tooltip as the secondary detail.
    const chunks = chunkEstimate(asset)
    const indexed = (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex shrink-0 cursor-help">
            <StatusBadge density="table" label={t.vectorIndex.statusReady} tone="success" />
          </span>
        </TooltipTrigger>
        <TooltipContent side="top">{countLabel(chunks, t.vectorIndex.chunksOne, t.vectorIndex.chunks, chunks.toLocaleString(locale))}</TooltipContent>
      </Tooltip>
    )
    if (contextOmittedChunks <= 0) return indexed
    // Indexed and searchable by their own text, but without context: the
    // provider's content filter blocked their batch. Named, never silent.
    return (
      <span
        className={cn(
          'flex min-w-0',
          stacked ? 'flex-col items-end gap-0.5' : 'items-center gap-1.5',
        )}
      >
        {indexed}
        <ContextOmittedLabel count={contextOmittedChunks} />
      </span>
    )
  }
  // 'skipped' (live, no text) or 'pending' (queued / unembeddable, not
  // running). A mid-upload/mid-parse placeholder reads "queued", never
  // "no text" — its text simply has not arrived yet.
  const noText = effective === 'skipped' || memberHasNoEmbeddableText(asset)
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex cursor-help">
          <StatusBadge
            density="table"
            label={noText ? t.vectorIndex.embeddingNoText : t.vectorIndex.embeddingPending}
            tone="neutral"
          />
        </span>
      </TooltipTrigger>
      <TooltipContent side="top">
        {noText
          ? t.vectorIndex.embeddingNoTextTooltip
          : t.vectorIndex.embeddingPendingTooltip}
      </TooltipContent>
    </Tooltip>
  )
}

function UsedCell({ count }: { count: number }) {
  const { t } = useLocale()
  if (count <= 0) return <span className="text-muted-foreground/40">{t.fileLibrary.referencedNone}</span>
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex cursor-help items-center gap-1 text-muted-foreground">
          <Link className="size-3" />
          <span className="tabular-nums">{count}×</span>
        </span>
      </TooltipTrigger>
      <TooltipContent side="top">{t.fileLibrary.referencedTooltip.replace('{count}', String(count))}</TooltipContent>
    </Tooltip>
  )
}

/** Date + time the file was added to the library (`createdAt`). The compact
 * cell omits the year; the hover tooltip carries the full stamp. */
function AddedCell({ asset }: { asset: FileAssetRecord }) {
  const { locale, t } = useLocale()
  return (
    <div className="text-right t-meta-sm tabular-nums text-muted-foreground">
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="cursor-help whitespace-nowrap">{formatAddedAt(asset.createdAt, locale)}</span>
        </TooltipTrigger>
        <TooltipContent side="top">
          {t.fileLibrary.addedTooltip.replace('{date}', formatAddedAtFull(asset.createdAt, locale))}
        </TooltipContent>
      </Tooltip>
    </div>
  )
}

function parserLabel(asset: FileAssetRecord, t: ReturnType<typeof useLocale>['t']): string {
  if (asset.lifecycleStatus === 'deleting') return t.fileLibrary.deletionRunning
  if (asset.lifecycleStatus === 'delete_failed') return t.fileLibrary.deletionFailed
  if (asset.uploadStatus === 'awaiting_upload') return t.fileLibrary.uploadAwaiting
  if (asset.uploadStatus === 'retrying') return t.fileLibrary.uploadRetrying
  if (asset.uploadStatus === 'parsing') return t.fileLibrary.parserRunning
  if (asset.uploadStatus === 'finalizing') return t.fileLibrary.uploadFinalizing
  if (asset.uploadPending) return t.fileLibrary.uploadRunning
  if (asset.parsePending) return t.fileLibrary.parserRunning
  if (asset.parserId === 'markitdown') return t.fileLibrary.parserMarkitdown
  if (asset.parserId === 'client') return t.fileLibrary.parserClient
  return t.fileLibrary.referencedNone
}

function deletionLabel(asset: FileAssetRecord, t: ReturnType<typeof useLocale>['t']): string {
  if (asset.lifecycleStatus === 'delete_failed') return t.fileLibrary.deletionFailed
  if (asset.deletionStage === 'search_detached') return t.fileLibrary.deletionSearchDetached
  if (
    asset.deletionStage === 'blobs_removed'
    || asset.deletionStage === 'metadata_removed'
    || asset.deletionStage === 'residuals_verified'
  ) return t.fileLibrary.deletionFreeingStorage
  return t.fileLibrary.deletionRunning
}

function DeletionBadge({ asset }: { asset: FileAssetRecord }) {
  const { t } = useLocale()
  if (asset.lifecycleStatus !== 'deleting' && asset.lifecycleStatus !== 'delete_failed') return null
  const failed = asset.lifecycleStatus === 'delete_failed'
  return (
    <span
      className={cn(
        'inline-flex h-5 shrink-0 items-center gap-1 rounded-md border px-1.5 t-hint font-medium',
        failed
          ? 'border-warning/30 bg-warning-subtle text-warning'
          : 'border-brand/25 bg-brand-subtle text-brand',
      )}
      role="status"
    >
      {failed ? null : <span className="inqtrix-running-dot size-1.5 rounded-full bg-brand motion-reduce:animate-none" />}
      {deletionLabel(asset, t)}
    </span>
  )
}

function fileHandle(asset: FileAssetRecord): string {
  return `@files:${asset.label}`
}

function FileHandle({ asset }: { asset: FileAssetRecord }) {
  const { t } = useLocale()
  const handle = fileHandle(asset)
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="shrink-0 cursor-help t-mono text-muted-foreground hover:text-foreground">@files:</span>
      </TooltipTrigger>
      <TooltipContent
        className="max-w-[320px] border border-border bg-popover p-2.5 text-popover-foreground shadow-lg"
        side="top"
      >
        {/* The tooltip IS the full-string affordance — values wrap, never
            truncate. [overflow-wrap:anywhere] breaks unbroken tokens (file
            names, handles) only when a line cannot fit, without inflating
            the intrinsic width. */}
        <p className="font-mono t-label leading-5 text-foreground [overflow-wrap:anywhere]">{handle}</p>
        <p className="mt-1 t-meta-sm leading-5 text-muted-foreground [overflow-wrap:anywhere]">
          {asset.fileName}
        </p>
        <p className="mt-1.5 t-hint text-muted-foreground">{t.fileLibrary.handleTooltip}</p>
      </TooltipContent>
    </Tooltip>
  )
}

function FileDetails({
  asset,
  location,
  mode,
}: {
  asset: FileAssetRecord
  location?: string | null
  mode: FileItemProps['mode']
}) {
  const { locale, t } = useLocale()
  const meta = typeMeta(asset)
  const detailRows: [string, string][] = [
    [t.fileLibrary.detailHandle, fileHandle(asset)],
    [t.fileLibrary.detailFileName, asset.fileName],
    [t.fileLibrary.detailLocation, location || t.fileLibrary.referencedNone],
    [t.fileLibrary.detailParser, parserLabel(asset, t)],
    [
      mode === 'index' ? t.fileLibrary.detailChunks : t.fileLibrary.columnPages,
      mode === 'index'
        ? chunkEstimate(asset).toLocaleString(locale)
        : meta.paged && asset.pageCount != null
          ? asset.pageCount.toLocaleString(locale)
          : t.fileLibrary.noPages,
    ],
    [t.fileLibrary.columnSize, formatBytes(asset.sizeBytes, locale)],
  ]
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex shrink-0 cursor-help items-center text-muted-foreground/70 hover:text-foreground">
          <Info className="size-3.5" />
        </span>
      </TooltipTrigger>
      <TooltipContent
        className="max-w-[320px] border border-border bg-popover p-2.5 text-popover-foreground shadow-lg"
        side="top"
      >
        {/* Details popover = the full-string affordance: values WRAP instead
            of truncating ([overflow-wrap:anywhere] breaks unbroken file
            names/handles only when a line cannot fit, without blowing out
            the minmax(0,1fr) track). Labels top-align against wrapped
            values; no title attributes (mouse-only, unreliable). */}
        <p className="t-label leading-5 text-foreground [overflow-wrap:anywhere]">{asset.label}</p>
        <div className="mt-2 grid gap-1.5">
          {detailRows.map(([label, value]) => (
            <div className="grid grid-cols-[5.5rem_minmax(0,1fr)] items-start gap-2 t-meta-sm" key={label}>
              <span className="text-muted-foreground">{label}</span>
              <span className="min-w-0 leading-5 text-foreground [overflow-wrap:anywhere]">{value}</span>
            </div>
          ))}
          {asset.parseWarning ? (
            <div className="border-t border-border/70 pt-1.5 t-meta-sm text-warning [overflow-wrap:anywhere]">
              {asset.parseWarning}
            </div>
          ) : null}
        </div>
      </TooltipContent>
    </Tooltip>
  )
}

function NameCell({
  asset,
  breadcrumb,
  mode,
  onRename,
  source,
}: {
  asset: FileAssetRecord
  breadcrumb?: string | null
  mode: FileItemProps['mode']
  onRename?: (fileId: string, label: string) => void
  source?: string | null
}) {
  const { t } = useLocale()
  const titleNode = onRename ? (
    <InlineText
      ariaLabel={t.fileLibrary.rename}
      className="min-w-0 max-w-full t-list text-foreground"
      onCommit={(label) => onRename(asset.id, label)}
      value={asset.label}
    />
  ) : (
    <span className="min-w-0 truncate t-list text-foreground">{asset.label}</span>
  )
  const location = breadcrumb || source || null

  return (
    <div className="flex min-w-0 items-center gap-2">
      <TypeTile asset={asset} size="sm" />
      <div className="grid min-w-0 flex-1 grid-cols-[minmax(0,1fr)_1.25rem] items-center gap-1.5">
        <div className="flex min-w-0 items-center gap-1.5">
          <FileHandle asset={asset} />
          {titleNode}
          <StatusMark asset={asset} />
          {ingestBadgeState(asset) ? <ParserBadge asset={asset} /> : null}
          <DeletionBadge asset={asset} />
        </div>
        <span className="flex justify-center">
          <FileDetails asset={asset} location={location} mode={mode} />
        </span>
      </div>
    </div>
  )
}

function RowActions(props: FileItemProps) {
  const { t } = useLocale()
  const { asset, indexing, memberState, mode, moveTargets, onDelete, onIndexMember, onMove, onRemoveFromIndex } = props
  if (asset.lifecycleStatus === 'deleting') return null
  if (asset.lifecycleStatus === 'delete_failed') {
    return asset.deletionOperationId && props.onRetryDeletion ? (
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            aria-label={t.fileLibrary.deletionRetry}
            className="size-7 text-warning hover:text-warning"
            onClick={() => props.onRetryDeletion?.(asset.deletionOperationId as string)}
            size="icon"
            type="button"
            variant="ghost"
          >
            <RotateCcw className="size-3.5" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top">{asset.deletionError || t.fileLibrary.deletionRetry}</TooltipContent>
      </Tooltip>
    ) : null
  }
  if (mode === 'index') {
    if (
      props.indexRemoval?.status === 'deleting'
      || props.indexRemoval?.status === 'reconciling'
    ) return null
    if (
      props.indexRemoval?.status === 'delete_failed'
      || props.indexRemoval?.status === 'blocked'
    ) {
      return props.onRetryIndexRemoval ? (
        <Button
          aria-label={t.fileLibrary.deletionRetry}
          className="size-7 text-warning"
          onClick={() => props.onRetryIndexRemoval?.()}
          size="icon"
          type="button"
          variant="ghost"
        >
          <RotateCcw className="size-3.5" />
        </Button>
      ) : null
    }
    // A not-yet-indexed member gets its own "index this file" action (no run in
    // flight) — indexing just it, no full rebuild.
    const canIndex = memberState === 'pending' && !indexing && Boolean(onIndexMember)
    return (
      <>
        {canIndex ? (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                aria-label={t.vectorIndex.indexMember}
                className="size-7 text-brand hover:text-brand"
                onClick={() => onIndexMember?.(asset.id)}
                size="icon"
                type="button"
                variant="ghost"
              >
                <Sparkles className="size-3.5" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top">{t.vectorIndex.indexMember}</TooltipContent>
          </Tooltip>
        ) : null}
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              aria-label={t.vectorIndex.removeDoc}
              className="size-7 text-muted-foreground hover:text-foreground"
              // Disabled mid-run: removing a still-building member would strand
              // its server document (its id isn't tracked until the run lands).
              disabled={indexing}
              onClick={() => onRemoveFromIndex?.(asset.id)}
              size="icon"
              type="button"
              variant="ghost"
            >
              <X className="size-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top">{t.vectorIndex.removeDoc}</TooltipContent>
        </Tooltip>
      </>
    )
  }
  const canRetry =
    (asset.uploadStatus === 'failed'
      || asset.uploadStatus === 'cancelled'
      || asset.uploadStatus === 'awaiting_upload')
    && Boolean(props.onRetryUpload)
    && (props.canRetryUpload?.(asset.id) ?? false)
  return (
    <>
      {canRetry ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              aria-label={t.fileLibrary.uploadRetry}
              className="size-7 text-warning hover:text-warning"
              onClick={() => props.onRetryUpload?.(asset.id)}
              size="icon"
              type="button"
              variant="ghost"
            >
              <RotateCcw className="size-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top">{t.fileLibrary.uploadRetry}</TooltipContent>
        </Tooltip>
      ) : null}
      {props.onPreview ? (
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              aria-label={t.filePreview.open}
              className="size-7 text-muted-foreground hover:text-foreground"
              onClick={() => props.onPreview?.(asset.id)}
              size="icon"
              type="button"
              variant="ghost"
            >
              <Eye className="size-3.5" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="top">{t.filePreview.open}</TooltipContent>
        </Tooltip>
      ) : null}
      {moveTargets && onMove ? <MoveMenu asset={asset} onMove={onMove} targets={moveTargets} /> : null}
      {onDelete ? (
        <ConfirmDelete
          ariaLabel={t.fileLibrary.remove}
          hint={t.fileLibrary.removeFileHint.replace('{label}', asset.label)}
          onConfirm={() => onDelete(asset.id)}
        />
      ) : null}
    </>
  )
}

function dragHandlers(props: FileItemProps) {
  const { asset, mode, onDragEnd, onDragStart } = props
  if (mode === 'index' || (asset.lifecycleStatus ?? 'active') !== 'active') return {}
  return {
    draggable: true,
    onDragEnd,
    onDragStart: (event: DragEvent) => {
      event.dataTransfer.setData(FILE_DRAG_TYPE, asset.id)
      event.dataTransfer.effectAllowed = 'move'
      onDragStart?.(asset.id)
    },
  }
}

/** Row/card click while selection affordances exist: modifiers always
 * select (ctrl/cmd toggle, shift range), a plain
 * click toggles only while the mode is active, else falls through (preview). */
function handleSelectableClick(
  props: FileItemProps,
  event: ReactMouseEvent,
  fallthrough: (() => void) | undefined,
): void {
  if (props.onToggleSelect && props.mode === 'library') {
    if (event.shiftKey) {
      props.onToggleSelect(props.asset.id, { range: true })
      return
    }
    if (event.metaKey || event.ctrlKey) {
      props.onToggleSelect(props.asset.id, { additive: true })
      return
    }
    if (props.selectionActive) {
      props.onToggleSelect(props.asset.id, {})
      return
    }
  }
  fallthrough?.()
}

/** Memoised: during an upload batch the pipeline dispatches per-file settle
 * actions; with stable row callbacks (the workspace useCallbacks them) only
 * the row whose asset record changed re-renders. */
export const FileRow = memo(function FileRow(props: FileItemProps) {
  const { locale, t } = useLocale()
  const { asset, breadcrumb, inRun, liveProgress, memberState, mode, referenceCount, source } = props
  const isIndex = mode === 'index'
  const lifecycleActive = (asset.lifecycleStatus ?? 'active') === 'active'
  const meta = typeMeta(asset)
  const canPreview = lifecycleActive && mode === 'library' && Boolean(props.onPreview)
  const canSelect = lifecycleActive && mode === 'library' && Boolean(props.onToggleSelect)
  return (
    <div
      // Selection state for AT lives on the checkbox (aria-pressed) — a
      // role-less div may not carry aria-selected.
      data-selected={canSelect && props.selected ? '' : undefined}
      className={cn(
        EXPLORER_GRID,
        'group min-h-9 rounded-md border-b border-border/45 px-2 py-1.5 transition-colors hover:bg-surface/55',
        !isIndex && 'cursor-grab active:cursor-grabbing',
        props.selected && 'bg-brand-subtle/50 hover:bg-brand-subtle/60',
        !lifecycleActive && 'cursor-default bg-surface/35 opacity-75 hover:bg-surface/35',
      )}
      onClick={(event) => handleSelectableClick(props, event, canPreview ? () => props.onPreview?.(asset.id) : undefined)}
      // Shift-click selects a range — never native text selection.
      onMouseDown={canSelect ? (event) => { if (event.shiftKey) event.preventDefault() } : undefined}
      {...dragHandlers(props)}
    >
      <div className="flex min-w-0 items-center gap-2">
        {canSelect ? (
          <SelectCheck
            label={t.fileLibrary.selectFile.replace('{label}', asset.label)}
            onToggle={(event) => props.onToggleSelect?.(asset.id, {
              additive: event.metaKey || event.ctrlKey,
              range: event.shiftKey,
            })}
            revealed={Boolean(props.selectionActive || props.selected)}
            selected={Boolean(props.selected)}
          />
        ) : null}
        <div className="min-w-0 flex-1">
          {/* Rename pauses with the other per-row actions during selection
              mode: the name is the row's largest click target and must
              toggle, not open the inline editor. */}
          <NameCell asset={asset} breadcrumb={breadcrumb} mode={mode} onRename={!lifecycleActive || props.selectionActive ? undefined : props.onRename} source={source} />
        </div>
      </div>
      <div className="min-w-0"><TypeBadge asset={asset} /></div>
      <div className={cn('t-meta tabular-nums text-muted-foreground', isIndex ? 'flex justify-start' : 'text-right')}>
        {isIndex ? (
          <ChunkCell asset={asset} contextOmittedChunks={props.contextOmittedChunks} inRun={inRun} indexRemoval={props.indexRemoval} jobProgress={props.jobProgress} liveProgress={liveProgress} state={memberState ?? 'pending'} />
        ) : meta.paged && asset.pageCount != null ? (
          asset.pageCount
        ) : (
          <span className="text-muted-foreground/40">{t.fileLibrary.referencedNone}</span>
        )}
      </div>
      <div className="text-right t-meta tabular-nums text-muted-foreground">{formatBytes(asset.sizeBytes, locale)}</div>
      <AddedCell asset={asset} />
      {isIndex ? (
        <div className="flex min-w-0 items-center gap-1 t-meta-sm text-muted-foreground">
          <Folder className="size-3 shrink-0" />
          <span className="truncate">{source ?? t.fileLibrary.referencedNone}</span>
        </div>
      ) : (
        <div className="flex justify-end t-meta-sm"><UsedCell count={referenceCount ?? 0} /></div>
      )}
      <div
        className="flex items-center justify-end gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100"
        onClick={(event) => event.stopPropagation()}
      >
        {/* Per-row actions pause during selection mode — the contextual bar
            owns actions then, so one action surface applies to the whole
            selection. */}
        {props.selectionActive ? null : <RowActions {...props} />}
      </div>
    </div>
  )
})

export const FileCard = memo(function FileCard(props: FileItemProps) {
  const { locale, t } = useLocale()
  const { asset, breadcrumb, inRun, liveProgress, memberState, mode, referenceCount } = props
  const isIndex = mode === 'index'
  const lifecycleActive = (asset.lifecycleStatus ?? 'active') === 'active'
  const meta = typeMeta(asset)
  const canPreview = lifecycleActive && mode === 'library' && Boolean(props.onPreview)
  const canSelect = lifecycleActive && mode === 'library' && Boolean(props.onToggleSelect)
  return (
    <div
      // Selection state for AT lives on the checkbox (aria-pressed) — a
      // role-less div may not carry aria-selected.
      data-selected={canSelect && props.selected ? '' : undefined}
      className={cn(
        'group flex flex-col rounded-lg border border-border bg-card p-3 shadow-[0_1px_2px_var(--shadow-hairline)] transition-colors hover:bg-accent/30',
        !isIndex && 'cursor-grab active:cursor-grabbing',
        props.selected && 'border-brand/50 bg-brand-subtle/40 hover:bg-brand-subtle/50',
        !lifecycleActive && 'cursor-default bg-surface/35 opacity-75 hover:bg-surface/35',
      )}
      onClick={(event) => handleSelectableClick(props, event, canPreview ? () => props.onPreview?.(asset.id) : undefined)}
      onMouseDown={canSelect ? (event) => { if (event.shiftKey) event.preventDefault() } : undefined}
      {...dragHandlers(props)}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex min-w-0 items-center gap-2">
          {canSelect ? (
            <SelectCheck
              label={t.fileLibrary.selectFile.replace('{label}', asset.label)}
              onToggle={(event) => props.onToggleSelect?.(asset.id, {
                additive: event.metaKey || event.ctrlKey,
                range: event.shiftKey,
              })}
              revealed={Boolean(props.selectionActive || props.selected)}
              selected={Boolean(props.selected)}
            />
          ) : null}
          <TypeTile asset={asset} size="md" />
          <TypeBadge asset={asset} />
        </div>
        <div
          className="flex items-center gap-1 opacity-0 transition-opacity focus-within:opacity-100 group-hover:opacity-100"
          onClick={(event) => event.stopPropagation()}
        >
          {props.selectionActive ? null : <RowActions {...props} />}
        </div>
      </div>
      <div className="mt-2.5 min-w-0">
        <div className="flex min-w-0 items-center gap-1.5">
          <span className="shrink-0 t-mono text-muted-foreground">@files:</span>
          {lifecycleActive && props.onRename && !props.selectionActive ? (
            <InlineText
              ariaLabel={t.fileLibrary.rename}
              className="min-w-0 max-w-full t-list text-foreground"
              onCommit={(label) => props.onRename?.(asset.id, label)}
              value={asset.label}
            />
          ) : (
            <span className="min-w-0 truncate t-list text-foreground">{asset.label}</span>
          )}
          <StatusMark asset={asset} />
          <ParserBadge asset={asset} />
          <DeletionBadge asset={asset} />
        </div>
        <p className="mt-0.5 truncate t-meta-sm text-muted-foreground" title={asset.fileName}>
          {asset.fileName}
          {breadcrumb ? ` · ${breadcrumb}` : ''}
        </p>
        <p className="mt-1 t-hint tabular-nums text-muted-foreground">{formatAddedAt(asset.createdAt, locale)}</p>
      </div>
      <div className="mt-3 flex items-center justify-between gap-2 border-t border-border/60 pt-2 t-meta-sm text-muted-foreground">
        <span className="tabular-nums">
          {formatBytes(asset.sizeBytes, locale)}
          {meta.paged && asset.pageCount != null ? ` · ${asset.pageCount} ${t.fileLibrary.pagesUnit}` : ''}
        </span>
        {isIndex ? (
          <ChunkCell asset={asset} contextOmittedChunks={props.contextOmittedChunks} inRun={inRun} indexRemoval={props.indexRemoval} jobProgress={props.jobProgress} liveProgress={liveProgress} stacked state={memberState ?? 'pending'} />
        ) : (
          <UsedCell count={referenceCount ?? 0} />
        )}
      </div>
    </div>
  )
})
