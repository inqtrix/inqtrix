import { useRef, useState, type PointerEvent as ReactPointerEvent } from 'react'
import { GripVertical, LoaderCircle, RefreshCw, X } from '@/components/icons'
import { attachmentChipVisual } from '@/features/files/attachmentChips'
import { chatContextRefKey, type ChatAttachmentChipModel } from '@/features/project/selectors'
import type { ChatContextReferenceRecord } from '@/features/project/types'
import { cn } from '@/lib/utils'

/**
 * Which ordered list a chip belongs to. Pills are the positional `[N]`
 * references inside the composer text; pending chips are the non-positional
 * (rule / dropped-file) references. The two are reordered independently — a pill
 * never drops into the pending run and vice versa — because their order is
 * backed by different sources (the TipTap document vs. a plain ref array).
 */
type ChipScope = 'pill' | 'pending'

type ContextChipLegendProps = {
  chips: ChatAttachmentChipModel[]
  /** Locale strings kept out of the shared component so it stays surface-agnostic. */
  labels: {
    attachmentFailed: string
    attachmentPending: string
    attachmentRetry: string
    removeContext: string
    reorderHint: string
  }
  onRemove: (ref: ChatContextReferenceRecord) => void
  /** Uses the existing durable UploadOperation/byte registry coordinator. */
  onRetry?: (chip: ChatAttachmentChipModel) => void
  /** Reorder within the pending (non-positional) run by pending index. */
  onReorderPending: (fromIndex: number, toIndex: number) => void
  /** Reorder within the pill (`[N]`) run by reading-order index. */
  onReorderPill: (fromIndex: number, toIndex: number) => void
  /** Ordered keys of the pending refs — the pending drag-rank space. */
  pendingKeys: string[]
  /** Ordered keys of the positional `[N]` pills — the pill drag-rank space. */
  pillKeys: string[]
}

/**
 * Draggable legend of attached context chips, shared by the chat and editor
 * composers. Each chip carries its `[N]` (or queue) number; dragging reorders it
 * within its own scope. Reordering a pill permutes which source each `[N]` points
 * to (via the composer's `reorderPill`), leaving the prose untouched; reordering a
 * pending chip permutes the surface's pending ref array. A key present in both
 * `pillKeys` and `pendingKeys` resolves to the pill scope (the deduped chip list
 * already renders it once, in the leading pill segment).
 */
export function ContextChipLegend({
  chips,
  labels,
  onRemove,
  onReorderPending,
  onReorderPill,
  onRetry,
  pendingKeys,
  pillKeys,
}: ContextChipLegendProps) {
  const [draggedKey, setDraggedKey] = useState<string | null>(null)
  const [draggedScope, setDraggedScope] = useState<ChipScope | null>(null)
  // Insertion index (0..scopeCount) inside the dragged chip's scope.
  const [dropIndicatorIndex, setDropIndicatorIndex] = useState<number | null>(null)
  const chipListRef = useRef<HTMLDivElement | null>(null)
  if (chips.length === 0) return null

  function readDropInsertionIndex(clientX: number, clientY: number, scope: ChipScope) {
    const container = chipListRef.current
    if (!container) return null
    const chipRects = Array.from(
      container.querySelectorAll<HTMLElement>(`[data-chip-scope="${scope}"]`),
    )
      .map((element) => {
        const index = Number(element.dataset.chipScopeIndex)
        const rect = element.getBoundingClientRect()
        return { centerY: rect.top + rect.height / 2, index, rect }
      })
      .filter((chip) => Number.isInteger(chip.index) && chip.rect.width > 0 && chip.rect.height > 0)
    if (chipRects.length === 0) return null
    const nearestRowChip = chipRects.reduce((nearest, chip) => (
      Math.abs(chip.centerY - clientY) < Math.abs(nearest.centerY - clientY) ? chip : nearest
    ))
    const rowBand = Math.max(18, nearestRowChip.rect.height)
    const rowChips = chipRects
      .filter((chip) => Math.abs(chip.centerY - nearestRowChip.centerY) <= rowBand)
      .sort((a, b) => a.rect.left - b.rect.left)
    if (rowChips.length === 0) return null
    for (const chip of rowChips) {
      if (clientX < chip.rect.left + chip.rect.width / 2) return chip.index
    }
    return rowChips[rowChips.length - 1].index + 1
  }

  function destinationIndexFromInsertionIndex(
    fromIndex: number,
    insertionIndex: number,
    scopeCount: number,
  ) {
    const bounded = Math.max(0, Math.min(scopeCount, insertionIndex))
    const destination = bounded > fromIndex ? bounded - 1 : bounded
    return Math.max(0, Math.min(scopeCount - 1, destination))
  }

  function reorderInScope(scope: ChipScope, fromIndex: number, toIndex: number) {
    if (scope === 'pill') onReorderPill(fromIndex, toIndex)
    else onReorderPending(fromIndex, toIndex)
  }

  function beginPointerReorder(
    event: ReactPointerEvent<HTMLSpanElement>,
    scope: ChipScope,
    fromIndex: number,
    scopeCount: number,
    key: string,
  ) {
    if (scopeCount <= 1 || event.button !== 0) return
    if ((event.target as HTMLElement).closest('button')) return
    event.preventDefault()
    setDraggedKey(key)
    setDraggedScope(scope)
    setDropIndicatorIndex(fromIndex)

    function handlePointerMove(moveEvent: PointerEvent) {
      const next = readDropInsertionIndex(moveEvent.clientX, moveEvent.clientY, scope)
      if (next !== null) setDropIndicatorIndex(next)
    }
    function finishPointerReorder(upEvent: PointerEvent) {
      const next = readDropInsertionIndex(upEvent.clientX, upEvent.clientY, scope)
      cleanupPointerReorder()
      if (next !== null) {
        const destination = destinationIndexFromInsertionIndex(fromIndex, next, scopeCount)
        if (destination !== fromIndex) reorderInScope(scope, fromIndex, destination)
      }
    }
    function cleanupPointerReorder() {
      document.removeEventListener('pointermove', handlePointerMove)
      document.removeEventListener('pointerup', finishPointerReorder)
      document.removeEventListener('pointercancel', cleanupPointerReorder)
      setDraggedKey(null)
      setDraggedScope(null)
      setDropIndicatorIndex(null)
    }
    document.addEventListener('pointermove', handlePointerMove)
    document.addEventListener('pointerup', finishPointerReorder)
    document.addEventListener('pointercancel', cleanupPointerReorder)
  }

  return (
    <div className="mb-1.5 flex min-w-0 flex-wrap items-center gap-1.5 border-b border-border/70 pb-1.5" ref={chipListRef}>
      {chips.map((chip, index) => {
        const key = chatContextRefKey(chip.ref)
        const { chipClassName, icon: Icon } = attachmentChipVisual(chip.kind)
        const pillIndex = pillKeys.indexOf(key)
        const scope: ChipScope | null = pillIndex >= 0
          ? 'pill'
          : pendingKeys.includes(key) ? 'pending' : null
        const scopeIndex = scope === 'pill' ? pillIndex : pendingKeys.indexOf(key)
        const scopeCount = scope === 'pill' ? pillKeys.length : pendingKeys.length
        const canReorder = scope !== null && scopeCount > 1
        const sameScopeDrag = canReorder && draggedKey !== null && draggedScope === scope
        const showBeforeIndicator = sameScopeDrag && dropIndicatorIndex === scopeIndex
        const showAfterIndicator = sameScopeDrag
          && dropIndicatorIndex === scopeCount && scopeIndex === scopeCount - 1
        return (
          <span
            className={cn(
              'group relative inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-md border px-2 py-1 text-xs font-semibold transition',
              chipClassName,
              chip.readiness === 'pending' && 'border-warning/50 bg-warning/10 text-warning',
              chip.readiness === 'failed' && 'border-destructive/50 bg-destructive/10 text-destructive',
              canReorder && 'cursor-grab active:cursor-grabbing',
              draggedKey === key && 'scale-[0.98] cursor-grabbing opacity-80 shadow-[0_8px_20px_var(--shadow-soft)] ring-1 ring-ring/50',
            )}
            data-chip-scope={canReorder && scope ? scope : undefined}
            data-chip-scope-index={canReorder ? scopeIndex : undefined}
            key={key}
            onKeyDown={canReorder && scope ? (event) => {
              if (!event.altKey) return
              if (event.key === 'ArrowLeft') {
                event.preventDefault()
                reorderInScope(scope, scopeIndex, Math.max(0, scopeIndex - 1))
              }
              if (event.key === 'ArrowRight') {
                event.preventDefault()
                reorderInScope(scope, scopeIndex, Math.min(scopeCount - 1, scopeIndex + 1))
              }
            } : undefined}
            onPointerDown={canReorder && scope
              ? (event) => beginPointerReorder(event, scope, scopeIndex, scopeCount, key)
              : undefined}
            tabIndex={canReorder ? 0 : undefined}
            title={[
              chip.title,
              chip.readiness === 'pending' ? labels.attachmentPending : null,
              chip.readiness === 'failed' ? chip.error ?? labels.attachmentFailed : null,
              canReorder ? labels.reorderHint : null,
            ].filter(Boolean).join(' · ')}
          >
            {showBeforeIndicator && (
              <span className="pointer-events-none absolute -left-1 top-1 bottom-1 w-0.5 rounded-full bg-ring shadow-[0_0_0_1px_var(--background)]" />
            )}
            {showAfterIndicator && (
              <span className="pointer-events-none absolute -right-1 top-1 bottom-1 w-0.5 rounded-full bg-ring shadow-[0_0_0_1px_var(--background)]" />
            )}
            {/* Displayed number = position in the combined list (matches the text);
                drag rank = scope position. */}
            <span className="relative grid h-4 min-w-4 shrink-0 place-items-center px-0.5 t-meta-sm font-bold leading-none tabular-nums text-current">
              <span className={cn(
                'opacity-80 transition-opacity',
                canReorder && 'group-hover:opacity-0 group-focus-within:opacity-0',
                draggedKey === key && 'opacity-0',
              )}>
                {index + 1}
              </span>
              {canReorder && (
                <GripVertical className={cn(
                  'absolute size-3.5 cursor-grab opacity-0 transition-opacity group-hover:opacity-70 group-focus-within:opacity-70',
                  draggedKey === key && 'cursor-grabbing opacity-90',
                )} />
              )}
            </span>
            <Icon className="size-3.5 shrink-0" />
            <span className="min-w-0 flex-1 truncate">{chip.label}</span>
            {chip.fileCount !== null && (
              <span className="shrink-0 t-hint font-bold tabular-nums opacity-75">{chip.fileCount}</span>
            )}
            {chip.readiness === 'pending' && (
              <span className="inline-flex max-w-36 shrink items-center gap-1 truncate t-hint font-medium">
                <LoaderCircle className="size-3 shrink-0 animate-spin motion-reduce:animate-none" />
                <span className="truncate">{labels.attachmentPending}</span>
              </span>
            )}
            {chip.readiness === 'failed' && (
              <span className="max-w-48 shrink truncate t-hint font-medium">
                {chip.error ?? labels.attachmentFailed}
              </span>
            )}
            {chip.readiness === 'failed' && chip.retryAssetIds.length > 0 && onRetry && (
              <button
                aria-label={labels.attachmentRetry}
                className="shrink-0 rounded-sm p-0.5 transition hover:bg-background/60 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                onClick={() => onRetry(chip)}
                title={labels.attachmentRetry}
                type="button"
              >
                <RefreshCw className="size-3" />
              </button>
            )}
            <button
              aria-label={labels.removeContext}
              className="ml-0.5 shrink-0 rounded-sm p-0.5 opacity-80 transition hover:bg-background/60 hover:opacity-100 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onClick={() => onRemove(chip.ref)}
              type="button"
            >
              <X className="size-3" />
            </button>
          </span>
        )
      })}
    </div>
  )
}
