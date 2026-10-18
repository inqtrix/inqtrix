import * as React from 'react'
import { ChevronDown, ChevronRight, LoaderCircle, Search, X } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

/** How many items a FOLDER section shows before "show more".
 *
 * Only folders cap their list: a folder can hold hundreds of entries and
 * would push the rest of the rail out of view. An ungrouped section is the
 * rail's own list — it already has a scrollbar, and capping it there hid
 * entries the user could simply have scrolled to.
 */
export const EXPLORER_SECTION_REVEAL_STEP = 8
export const EXPLORER_DRAG_THRESHOLD_PX = 4

export function isExplorerActionTarget(target: EventTarget | null) {
  return target instanceof HTMLElement && Boolean(target.closest('[data-explorer-action]'))
}

export function isPastExplorerDragThreshold(
  startX: number,
  startY: number,
  event: PointerEvent,
) {
  return Math.hypot(event.clientX - startX, event.clientY - startY) >= EXPLORER_DRAG_THRESHOLD_PX
}

export function ExplorerSectionLabel({
  children,
  className,
}: {
  children: React.ReactNode
  className?: string
}) {
  return (
    <p className={cn('px-1.5 pb-1 pt-3 t-label text-muted-foreground', className)}>
      {children}
    </p>
  )
}

export function ExplorerRunningIndicator({
  label,
  className,
}: {
  label: string
  className?: string
}) {
  return (
    <LoaderCircle
      aria-label={label}
      className={cn('icon-sm animate-spin text-brand', className)}
    />
  )
}

export function ExplorerSearchField({
  clearLabel,
  label,
  onChange,
  onClear,
  placeholder,
  value,
}: {
  clearLabel: string
  label: string
  onChange: (value: string) => void
  onClear: () => void
  placeholder: string
  value: string
}) {
  return (
    <div className="border-b border-border px-2 py-1.5">
      <div className="flex items-center gap-1.5 rounded-md bg-background/80 px-2 py-1">
        <Search className="icon-sm shrink-0 text-muted-foreground" />
        <input
          aria-label={label}
          className="t-label min-w-0 flex-1 border-0 bg-transparent text-foreground outline-none placeholder:text-muted-foreground"
          autoComplete="off"
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Escape' && value) {
              event.preventDefault()
              onClear()
            }
          }}
          placeholder={placeholder}
          role="searchbox"
          type="text"
          value={value}
        />
        {value ? (
          <button
            aria-label={clearLabel}
            className="grid size-5 shrink-0 place-items-center rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            onClick={onClear}
            type="button"
          >
            <X className="icon-sm" />
          </button>
        ) : null}
      </div>
    </div>
  )
}

export function ExplorerRevealControls({
  onShowLess,
  onShowMore,
  showLessLabel,
  showMoreLabel,
  step = EXPLORER_SECTION_REVEAL_STEP,
  total,
  visibleCount,
}: {
  onShowLess: () => void
  onShowMore: () => void
  showLessLabel: string
  showMoreLabel: string
  step?: number
  total: number
  visibleCount: number
}) {
  if (total <= step) return null
  const canShowMore = visibleCount < total
  const canShowLess = visibleCount > step

  return (
    <div className="inqtrix-explorer-reveal flex min-h-7 items-center gap-4 px-1.5 pl-6">
      {canShowMore && (
        <button
          className="text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          onMouseDown={(event) => event.preventDefault()}
          onClick={onShowMore}
          type="button"
        >
          {showMoreLabel}
        </button>
      )}
      {canShowLess && (
        <button
          className="text-xs text-muted-foreground transition-colors hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          onMouseDown={(event) => event.preventDefault()}
          onClick={onShowLess}
          type="button"
        >
          {showLessLabel}
        </button>
      )}
    </div>
  )
}

export function ExplorerFolderRow({
  actions,
  children,
  className,
  dragHandle,
  onPointerDown,
}: {
  actions?: React.ReactNode
  children: React.ReactNode
  className?: string
  dragHandle?: React.ReactNode
  onPointerDown?: React.PointerEventHandler<HTMLDivElement>
}) {
  return (
    <div
      className={cn(
        'group/explorer-folder flex min-h-8 items-center gap-1 rounded-md px-1.5 transition-colors',
        'hover:bg-surface focus-within:bg-surface',
        onPointerDown && 'cursor-grab active:cursor-grabbing',
        className,
      )}
      onPointerDown={onPointerDown}
    >
      {dragHandle}
      <div className="min-w-0 flex-1">{children}</div>
      {actions ? (
        <div
          className="flex items-center gap-0.5 opacity-0 transition-opacity group-hover/explorer-folder:opacity-100 group-focus-within/explorer-folder:opacity-100"
          data-explorer-action
        >
          {actions}
        </div>
      ) : null}
    </div>
  )
}

export function ExplorerFolderToggle({
  count,
  expanded,
  icon,
  label,
  onDoubleClick,
  onToggle,
  title,
}: {
  count?: React.ReactNode
  expanded: boolean
  icon: React.ReactNode
  label: string
  onDoubleClick?: React.MouseEventHandler<HTMLButtonElement>
  onToggle: () => void
  title: React.ReactNode
}) {
  const Chevron = expanded ? ChevronDown : ChevronRight
  return (
    <button
      aria-expanded={expanded}
      aria-label={label}
      className="flex min-h-8 w-full min-w-0 items-center gap-1.5 rounded-md text-left focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      onDoubleClick={onDoubleClick}
      onClick={onToggle}
      type="button"
    >
      <span className="shrink-0 text-muted-foreground">{icon}</span>
      <span className="min-w-0 truncate t-list text-foreground">{title}</span>
      <Chevron
        aria-hidden="true"
        className="icon-xs shrink-0 text-muted-foreground opacity-0 transition-opacity group-hover/explorer-folder:opacity-100 group-focus-within/explorer-folder:opacity-100"
      />
      {count !== undefined ? (
        <span className="ml-auto shrink-0 t-hint font-semibold tabular-nums text-muted-foreground">
          {count}
        </span>
      ) : null}
    </button>
  )
}

export function ExplorerItemRow({
  active = false,
  children,
  className,
  dragging = false,
  nested = false,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & {
  active?: boolean
  dragging?: boolean
  nested?: boolean
}) {
  return (
    <div
      className={cn(
        'group/explorer-item relative flex min-h-8 min-w-0 items-center rounded-md border border-transparent',
        'px-1.5 transition-colors',
        nested && 'pl-6',
        props.onPointerDown && 'cursor-grab active:cursor-grabbing',
        active
          ? 'border-brand/20 bg-brand-subtle text-brand'
          : 'text-foreground hover:bg-surface focus-within:bg-surface',
        dragging && 'border-brand/40 bg-brand-subtle/80 opacity-70',
        className,
      )}
      {...props}
    >
      {children}
    </div>
  )
}

export type ExplorerHistoryAction = {
  /** Overrides `label` as the accessible name, e.g. `"Löschen: <title>"`. */
  ariaLabel?: string
  destructive?: boolean
  disabled?: boolean
  icon: React.ReactNode
  /** Tooltip text and default accessible name. */
  label: string
  onSelect: () => void
}

/** Trailing hover actions, laid out from the right edge inward: the last
 * action sits at `right-1` and each earlier one steps 1.5rem further left.
 * A row may carry at most as many actions as there are offsets — an action
 * beyond them would lose its offset and fall to the row's leading edge. */
const EXPLORER_HISTORY_ACTION_OFFSETS = ['right-1', 'right-7', 'right-13']

/** Backdrop strip width by action count: the icon span plus a fade-out
 * runway so the truncated title never collides with revealed icons. */
const EXPLORER_HISTORY_ACTION_BACKDROP_WIDTHS = ['w-12', 'w-[4.75rem]', 'w-24']

function ExplorerHistoryActionButton({
  action,
  offset,
}: {
  action: ExplorerHistoryAction
  offset: string | undefined
}) {
  const [tooltipEnabled, setTooltipEnabled] = React.useState(true)

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {/* pointer-events follow the visibility: while hidden, the
            actions sit UNDER the time label, and an invisible destructive
            button must not catch a touch tap aimed at the timestamp. */}
        <Button
          aria-label={action.ariaLabel ?? action.label}
          className={cn(
            'pointer-events-none absolute top-1/2 size-6 -translate-y-1/2 text-foreground/55 opacity-0 transition',
            'focus-visible:pointer-events-auto focus-visible:opacity-100',
            'group-hover/explorer-item:pointer-events-auto group-hover/explorer-item:opacity-100',
            offset,
            action.destructive ? 'hover:text-destructive' : 'hover:text-foreground',
          )}
          data-explorer-action
          disabled={action.disabled}
          onClick={(event) => {
            event.stopPropagation()
            event.currentTarget.focus({ preventScroll: true })
            // The selected action may hand interaction to a portalled modal.
            // Removing the content in the same render prevents its exit
            // animation or a pending provider delay from crossing that
            // ownership boundary. A later, deliberate interaction restores
            // the normal tooltip contract.
            setTooltipEnabled(false)
            action.onSelect()
          }}
          onFocus={() => setTooltipEnabled(true)}
          onPointerEnter={() => setTooltipEnabled(true)}
          size="icon"
          type="button"
          variant="ghost"
        >
          {action.icon}
        </Button>
      </TooltipTrigger>
      {tooltipEnabled ? <TooltipContent>{action.label}</TooltipContent> : null}
    </Tooltip>
  )
}

/**
 * History/session explorer row: two-column grid whose right-aligned relative
 * age yields to the hover actions. The actions are absolutely positioned so
 * the title never shifts when they reveal.
 */
export function ExplorerHistoryRow({
  actions = [],
  active = false,
  dragging = false,
  disabled = false,
  indicator,
  nested = false,
  onPointerDown,
  onSelect,
  onStartRename,
  renameEditor,
  renameLabel,
  timeLabel,
  title,
}: {
  actions?: readonly ExplorerHistoryAction[]
  active?: boolean
  dragging?: boolean
  disabled?: boolean
  /** Status slot between title and age — running spinner / gate dot. */
  indicator?: React.ReactNode
  nested?: boolean
  onPointerDown?: React.PointerEventHandler<HTMLDivElement>
  onSelect: () => void
  onStartRename?: () => void
  /** When set, the row is in rename mode and renders this in place of the title. */
  renameEditor?: React.ReactNode
  renameLabel?: string
  timeLabel: string
  title: React.ReactNode
}) {
  const cells = (
    <>
      <span className="flex min-w-0 items-center gap-2">
        {renameEditor ?? (
          <span className="block min-w-0 flex-1 truncate t-list-regular text-foreground">
            {title}
          </span>
        )}
        {indicator}
      </span>
      <span className="shrink-0 t-hint tabular-nums text-muted-foreground group-hover/explorer-item:opacity-0 group-focus-within/explorer-item:opacity-0">
        {timeLabel}
      </span>
    </>
  )

  return (
    <ExplorerItemRow
      active={active}
      dragging={dragging}
      nested={nested}
      onPointerDown={onPointerDown}
      className={disabled ? 'opacity-70' : undefined}
    >
      {renameEditor ? (
        <div
          className="grid w-full min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 text-left"
          data-explorer-action
        >
          {cells}
        </div>
      ) : (
        <button
          aria-pressed={active}
          disabled={disabled}
          className="grid w-full min-w-0 grid-cols-[minmax(0,1fr)_auto] items-center gap-2 text-left focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          onClick={onSelect}
          onDoubleClick={onStartRename}
          title={renameLabel}
          type="button"
        >
          {cells}
        </button>
      )}
      {/* One area backdrop behind the revealed actions (not per icon):
          the buttons overlay the truncated title, so without it the
          icons and text collide. The gradient fades left and matches
          the row's own hover/active background for a seamless strip. */}
      {actions.length > 0 && (
        <span
          aria-hidden="true"
          className={cn(
            'pointer-events-none absolute inset-y-0 right-0 rounded-r-md opacity-0 transition-opacity',
            'bg-gradient-to-l via-65% to-transparent',
            active
              ? 'from-brand-subtle via-brand-subtle'
              : 'from-surface via-surface',
            'group-hover/explorer-item:opacity-100 group-focus-within/explorer-item:opacity-100',
            EXPLORER_HISTORY_ACTION_BACKDROP_WIDTHS[actions.length - 1],
          )}
        />
      )}
      {actions.map((action, index) => (
        <ExplorerHistoryActionButton
          action={action}
          key={index}
          offset={EXPLORER_HISTORY_ACTION_OFFSETS[actions.length - 1 - index]}
        />
      ))}
    </ExplorerItemRow>
  )
}

/** Inline rename control for `ExplorerHistoryRow`: commit on Enter/blur, cancel on Escape. */
export function ExplorerHistoryTitleInput({
  autoFocus = false,
  inputRef,
  label,
  onCancel,
  onChange,
  onCommit,
  value,
}: {
  autoFocus?: boolean
  inputRef?: React.Ref<HTMLInputElement>
  label: string
  onCancel: () => void
  onChange: (value: string) => void
  onCommit: () => void
  value: string
}) {
  return (
    <input
      aria-label={label}
      autoFocus={autoFocus}
      className="min-w-0 flex-1 rounded-sm border-0 bg-background/85 px-1.5 py-0.5 t-list-regular text-foreground outline-none focus-visible:ring-1 focus-visible:ring-ring"
      onBlur={onCommit}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === 'Enter') {
          event.preventDefault()
          onCommit()
        }
        if (event.key === 'Escape') {
          event.preventDefault()
          onCancel()
        }
      }}
      ref={inputRef}
      value={value}
    />
  )
}
