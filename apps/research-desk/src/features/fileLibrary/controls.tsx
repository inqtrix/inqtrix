import { useState, type KeyboardEvent } from 'react'
import {
  AlertTriangle,
  ArrowUpDown,
  Check,
  ChevronDown,
  FolderOpen,
  Info,
  LayoutGrid,
  List,
  Monitor,
  Server,
  Trash2,
  XCircle,
  type LucideIcon,
} from '@/components/icons'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { OptionMenuHeader, OptionMenuItem, optionMenuContentClassName } from '@/components/ui/option-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import type { FileAssetRecord } from '@/features/project/types'
import { fileStatus, ingestBadgeState, typeMeta } from './helpers'
import type { SortMode, ViewMode } from './constants'

export function InlineText({
  ariaLabel,
  className,
  onCommit,
  value,
}: {
  ariaLabel: string
  className?: string
  onCommit: (next: string) => void
  value: string
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(value)

  if (!editing) {
    return (
      <button
        aria-label={ariaLabel}
        className={cn(
          'truncate rounded-sm px-1 text-left hover:bg-accent/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
          className,
        )}
        onClick={(event) => {
          event.stopPropagation()
          setDraft(value)
          setEditing(true)
        }}
        type="button"
      >
        {value}
      </button>
    )
  }

  const commit = () => {
    setEditing(false)
    const next = draft.trim()
    if (next && next !== value) onCommit(next)
  }

  return (
    <input
      aria-label={ariaLabel}
      autoFocus
      className={cn(
        'min-w-0 rounded-sm border border-input bg-background px-1 text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        className,
      )}
      onBlur={commit}
      onChange={(event) => setDraft(event.target.value)}
      onClick={(event) => event.stopPropagation()}
      onKeyDown={(event: KeyboardEvent<HTMLInputElement>) => {
        if (event.key === 'Enter') {
          event.preventDefault()
          commit()
        }
        if (event.key === 'Escape') {
          event.preventDefault()
          setEditing(false)
        }
      }}
      value={draft}
    />
  )
}

export function TypeTile({ asset, size = 'md' }: { asset: FileAssetRecord; size?: 'sm' | 'md' }) {
  const meta = typeMeta(asset)
  const Icon = meta.Icon
  return (
    <span
      className={cn(
        'grid shrink-0 place-items-center rounded-md border border-file/25 bg-file-subtle text-file',
        size === 'sm' ? 'size-7' : 'size-9',
      )}
    >
      <Icon className={size === 'sm' ? 'size-3.5' : 'size-4'} />
    </span>
  )
}

export function TypeBadge({ asset }: { asset: FileAssetRecord }) {
  return (
    <span className="inline-flex h-5 items-center rounded border border-border bg-surface px-1.5 font-mono t-caption font-semibold text-muted-foreground">
      {typeMeta(asset).label}
    </span>
  )
}

/** Colored status marker — the single color signal on a file. Renders nothing
 * for cleanly parsed documents. */
export function StatusMark({ asset }: { asset: FileAssetRecord }) {
  const { t } = useLocale()
  // While the upload or a background server parse runs, suppress any
  // client-parse error marker (e.g. pdf.js failing on Safari): it is about
  // to be superseded, so a red alarm here would be misleading. The
  // ParserBadge shows the in-flight (or upload-error) state instead; a real
  // error only surfaces once the transient states settle. A failed upload's
  // pill already carries the message — no duplicate info marker next to it.
  if (asset.parsePending || asset.uploadPending || asset.uploadError != null) return null
  const status = fileStatus(asset)
  if (status === 'ok') {
    // A clean parse can still carry a note — e.g. the server parser declined
    // and the browser parsed instead. Surface it as neutral info (not the
    // failed/truncated alarm color) so the fallback is never silent;
    // a genuinely clean parse has no note and no marker.
    if (!asset.parseWarning) return null
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex shrink-0 cursor-help items-center text-muted-foreground">
            <Info className="size-3.5" />
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-[260px]" side="top">{asset.parseWarning}</TooltipContent>
      </Tooltip>
    )
  }
  const isFailed = status === 'failed'
  const Icon = isFailed ? XCircle : AlertTriangle
  const label = isFailed ? t.fileLibrary.statusFailedLabel : t.fileLibrary.statusTruncatedLabel
  const note = asset.parseWarning ?? (isFailed ? t.fileLibrary.statusFailedNote : t.fileLibrary.statusTruncatedNote)
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className={cn('inline-flex shrink-0 cursor-help items-center', isFailed ? 'text-destructive' : 'text-warning')}>
          <Icon className="size-3.5" />
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-[260px]" side="top">
        <span className="font-semibold">{label}.</span> {note}
      </TooltipContent>
    </Tooltip>
  )
}

/** "N without context" beside an indexed document: chunks the provider's
 * content filter left without context, explained on hover. Callers render it
 * only for a count above 0. */
export function ContextOmittedLabel({ count }: { count: number }) {
  const { locale, t } = useLocale()
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="min-w-0 cursor-help truncate whitespace-nowrap t-hint font-medium text-warning">
          {t.vectorIndex.contextOmitted.replace('{count}', count.toLocaleString(locale))}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-[260px]" side="top">
        {t.vectorIndex.contextOmittedTooltip}
      </TooltipContent>
    </Tooltip>
  )
}

/** Provenance badge: which parser produced the extracted text — the
 * server-side ladder (MarkItDown) or the in-browser parser. Renders nothing
 * when unknown (legacy rows / a local `.md` save that omits the field), the
 * same graceful absence as a missing `serverFileId`. */
export function ParserBadge({ asset }: { asset: FileAssetRecord }) {
  const { t } = useLocale()
  const ingestState = ingestBadgeState(asset)
  if (ingestState === 'uploading') {
    // The original bytes are queued for or in flight to the server — the
    // row appeared instantly, this pill is the honest "not stored yet".
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex shrink-0 cursor-help items-center gap-1 rounded border border-brand/30 bg-brand-subtle px-1 t-hint font-medium text-brand">
            <span className="inqtrix-running-dot size-1.5 rounded-full bg-brand" />
            {asset.uploadStatus === 'awaiting_upload'
              ? t.fileLibrary.uploadAwaiting
              : asset.uploadStatus === 'retrying'
                ? t.fileLibrary.uploadRetrying
                : asset.uploadStatus === 'parsing'
                  ? t.fileLibrary.parserRunning
                  : asset.uploadStatus === 'finalizing'
                    ? t.fileLibrary.uploadFinalizing
                    : t.fileLibrary.uploadRunning}
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-[260px]" side="top">
          {asset.uploadStatus === 'awaiting_upload'
            ? t.fileLibrary.uploadAwaitingNote
            : asset.uploadStatus === 'retrying'
              ? asset.uploadError ?? t.fileLibrary.uploadRetryingNote
              : asset.uploadStatus === 'parsing'
                ? t.fileLibrary.parserRunningNote
                : asset.uploadStatus === 'finalizing'
                  ? t.fileLibrary.uploadFinalizingNote
                  : t.fileLibrary.uploadRunningNote}
        </TooltipContent>
      </Tooltip>
    )
  }
  if (ingestState === 'upload-error') {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex shrink-0 cursor-help items-center gap-1 rounded border border-warning/40 bg-warning/10 px-1 t-hint font-medium text-warning">
            <AlertTriangle className="size-3" />
            {t.fileLibrary.uploadFailedLabel}
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-[260px]" side="top">
          {asset.uploadError ?? t.fileLibrary.uploadFailedNote}
        </TooltipContent>
      </Tooltip>
    )
  }
  if (ingestState === 'parsing') {
    // A background server (MarkItDown) parse is in flight — make it visible
    // (pulsing dot) so the upgrade is not silent magic; it resolves to the
    // MarkItDown badge (or back to the client state) when it lands.
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <span className="inline-flex shrink-0 cursor-help items-center gap-1 rounded border border-brand/30 bg-brand-subtle px-1 t-hint font-medium text-brand">
            <span className="inqtrix-running-dot size-1.5 rounded-full bg-brand" />
            {t.fileLibrary.parserRunning}
          </span>
        </TooltipTrigger>
        <TooltipContent className="max-w-[260px]" side="top">{t.fileLibrary.parserRunningNote}</TooltipContent>
      </Tooltip>
    )
  }
  if (asset.parserId !== 'markitdown' && asset.parserId !== 'client') return null
  const isServer = asset.parserId === 'markitdown'
  const Icon = isServer ? Server : Monitor
  const label = isServer ? t.fileLibrary.parserMarkitdown : t.fileLibrary.parserClient
  const note = isServer ? t.fileLibrary.parserMarkitdownNote : t.fileLibrary.parserClientNote
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex shrink-0 cursor-help items-center gap-1 rounded border border-border bg-surface px-1 t-hint font-medium text-muted-foreground">
          <Icon className="size-3" />
          {label}
        </span>
      </TooltipTrigger>
      <TooltipContent className="max-w-[260px]" side="top">{note}</TooltipContent>
    </Tooltip>
  )
}

/** Two-click destructive confirm via a dropdown. Renders an icon-only ghost
 * trigger, or a labeled outline button when `label` is provided. */
export function ConfirmDelete({
  ariaLabel,
  disabled = false,
  hint,
  label,
  onConfirm,
}: {
  ariaLabel: string
  disabled?: boolean
  hint: string
  label?: string
  onConfirm: () => void
}) {
  const { t } = useLocale()
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        {label ? (
          <Button
            aria-label={ariaLabel}
            className="gap-1.5 text-muted-foreground hover:text-destructive"
            disabled={disabled}
            size="sm"
            type="button"
            variant="outline"
          >
            <Trash2 className="size-4" />
            {label}
          </Button>
        ) : (
          <Button
            aria-label={ariaLabel}
            className="size-7 text-muted-foreground hover:text-destructive"
            disabled={disabled}
            size="icon"
            type="button"
            variant="ghost"
          >
            <Trash2 className="size-3.5" />
          </Button>
        )}
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <DropdownMenuLabel className="whitespace-normal text-xs font-normal leading-5 text-muted-foreground">
          {hint}
        </DropdownMenuLabel>
        <DropdownMenuSeparator />
        <DropdownMenuItem
          className="text-destructive focus:bg-destructive/10 focus:text-destructive"
          disabled={disabled}
          onClick={() => onConfirm()}
        >
          <Trash2 className="size-4" />
          {t.fileLibrary.confirmDelete}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export type MoveTarget = {
  groupId: string | null
  key: string
  label: string
  sectionId: string
}

/** The move-target list shared by the per-row menu and the bulk-selection
 * menu — one rendering of "pick a collection/group destination". */
function MoveTargetItems({
  isCurrent,
  onSelect,
  targets,
}: {
  isCurrent?: (target: MoveTarget) => boolean
  onSelect: (target: MoveTarget) => void
  targets: MoveTarget[]
}) {
  return (
    <>
      {targets.map((target) => {
        const current = isCurrent?.(target) ?? false
        return (
          <DropdownMenuItem
            disabled={current}
            key={target.key}
            onClick={() => onSelect(target)}
          >
            <FolderOpen className="size-3.5 text-muted-foreground" />
            <span className="min-w-0 flex-1 truncate">{target.label}</span>
            {current ? <Check className="size-3.5 text-brand" /> : null}
          </DropdownMenuItem>
        )
      })}
    </>
  )
}

export function MoveMenu({
  asset,
  onMove,
  targets,
}: {
  asset: FileAssetRecord
  onMove: (fileId: string, sectionId: string, groupId: string | null) => void
  targets: MoveTarget[]
}) {
  const { t } = useLocale()
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={t.fileLibrary.move}
          className="size-7 text-muted-foreground hover:text-foreground"
          size="icon"
          type="button"
          variant="ghost"
        >
          <FolderOpen className="size-3.5" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="max-h-72 w-60 overflow-y-auto">
        <DropdownMenuLabel>{t.fileLibrary.move}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <MoveTargetItems
          isCurrent={(target) => target.sectionId === asset.sectionId && target.groupId === asset.groupId}
          onSelect={(target) => onMove(asset.id, target.sectionId, target.groupId)}
          targets={targets}
        />
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

/** Bulk variant for the selection action bar: same destination list, applied
 * to every selected file. A labeled outline trigger (the bar has room and the
 * action needs a name), no "current" marker — a mixed selection has none. */
export function BulkMoveMenu({
  disabled,
  label,
  onMoveAll,
  targets,
}: {
  disabled?: boolean
  label: string
  onMoveAll: (sectionId: string, groupId: string | null) => void
  targets: MoveTarget[]
}) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button className="gap-1.5" disabled={disabled} size="sm" type="button" variant="outline">
          <FolderOpen className="size-4" />
          {label}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="max-h-72 w-60 overflow-y-auto">
        <DropdownMenuLabel>{label}</DropdownMenuLabel>
        <DropdownMenuSeparator />
        <MoveTargetItems
          onSelect={(target) => onMoveAll(target.sectionId, target.groupId)}
          targets={targets}
        />
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function SortSelect({ onChange, value }: { onChange: (value: SortMode) => void; value: SortMode }) {
  const { t } = useLocale()
  const options: { label: string; value: SortMode }[] = [
    { label: t.fileLibrary.sortRecent, value: 'recent' },
    { label: t.fileLibrary.sortName, value: 'name' },
    { label: t.fileLibrary.sortSize, value: 'size' },
    { label: t.fileLibrary.sortPages, value: 'pages' },
  ]
  const current = options.find((option) => option.value === value) ?? options[0]
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button aria-label={t.fileLibrary.sortBy} className="h-8 gap-1.5 px-2.5" size="sm" type="button" variant="outline">
          <ArrowUpDown className="text-muted-foreground" />
          <span>{current.label}</span>
          <ChevronDown className="text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className={optionMenuContentClassName} sideOffset={6}>
        <OptionMenuHeader count={options.length} title={t.fileLibrary.sortBy} value={current.label} />
        <div className="py-1">
          {options.map((option) => (
            <OptionMenuItem
              active={option.value === value}
              icon={ArrowUpDown}
              key={option.value}
              label={option.label}
              onSelect={() => onChange(option.value)}
            />
          ))}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function ViewToggle({ onChange, value }: { onChange: (value: ViewMode) => void; value: ViewMode }) {
  const { t } = useLocale()
  const options: { icon: LucideIcon; label: string; value: ViewMode }[] = [
    { icon: List, label: t.fileLibrary.viewList, value: 'list' },
    { icon: LayoutGrid, label: t.fileLibrary.viewGrid, value: 'grid' },
  ]
  return (
    <div className="inline-flex h-8 items-center rounded-md border border-border bg-surface p-0.5">
      {options.map((option) => {
        const Icon = option.icon
        const active = option.value === value
        return (
          <Tooltip key={option.value}>
            <TooltipTrigger asChild>
              <button
                aria-label={option.label}
                aria-pressed={active}
                className={cn(
                  'grid size-7 place-items-center rounded-[6px] transition-colors',
                  active
                    ? 'bg-background text-foreground shadow-[0_1px_2px_var(--shadow-hairline)]'
                    : 'text-muted-foreground hover:text-foreground',
                )}
                onClick={() => onChange(option.value)}
                type="button"
              >
                <Icon className="size-4" />
              </button>
            </TooltipTrigger>
            <TooltipContent side="top">{option.label}</TooltipContent>
          </Tooltip>
        )
      })}
    </div>
  )
}
