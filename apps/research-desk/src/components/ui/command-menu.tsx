import { useEffect, useRef } from 'react'
import { type LucideIcon } from '@/components/icons'
import { Kbd } from '@/components/ui/kbd'
import { cn } from '@/lib/utils'

export type CommandMenuItem = {
  /** Stable id (also the React key). */
  id: string
  label: string
  description?: string
  disabled?: boolean
  icon: LucideIcon
  /** Group header label; consecutive equal values render under one header. */
  group: string
}

/**
 * Presentation-only command popover for the editor's `/` slash menu. Mirrors the
 * `@`-mention menu's dense 13/11/10 language (grouped rows, left brand accent on
 * the active row, `<Kbd>` footer) so every popover in the app reads as one. The
 * caller owns positioning (a portal anchored at the caret), the active index and
 * keyboard handling; this component only renders and emits select/hover intents.
 */
export function CommandMenu({
  title,
  items,
  activeIndex,
  emptyLabel,
  navHint,
  selectHint,
  closeHint,
  onSelect,
  onHover,
}: {
  title: string
  items: CommandMenuItem[]
  activeIndex: number
  emptyLabel: string
  navHint: string
  selectHint: string
  closeHint: string
  onSelect: (index: number) => void
  onHover: (index: number) => void
}) {
  const groups: { label: string; rows: { index: number; item: CommandMenuItem }[] }[] = []
  items.forEach((item, index) => {
    const last = groups[groups.length - 1]
    if (last && last.label === item.group) last.rows.push({ index, item })
    else groups.push({ label: item.group, rows: [{ index, item }] })
  })

  // Keyboard navigation only moves `activeIndex`; without this the highlighted
  // row can scroll out of the (`max-h-72 overflow-y-auto`) viewport. `'nearest'`
  // scrolls only when the row is off-screen; no smooth behaviour so fast key
  // repeat stays in sync.
  const activeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex])

  return (
    <div
      className="w-max min-w-[15rem] max-w-sm overflow-hidden rounded-xl border border-border bg-popover shadow-lg animate-in fade-in zoom-in-95 motion-reduce:animate-none"
      data-editor-command-menu
    >
      <div className="flex items-center gap-1.5 border-b border-border px-2.5 py-1.5">
        <span className="text-[11px] font-medium text-muted-foreground">{title}</span>
        <span className="ml-auto shrink-0 text-[10px] tabular-nums text-muted-foreground/50">{items.length}</span>
      </div>

      {items.length === 0 ? (
        <div className="px-2.5 py-3 text-[11px] text-muted-foreground/70">{emptyLabel}</div>
      ) : (
        <div className="max-h-72 overflow-y-auto py-1">
          {groups.map((group, groupIndex) => (
            <div key={`${group.label}-${groupIndex}`}>
              <div className="px-2.5 pb-0.5 pt-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground/60">
                {group.label}
              </div>
              {group.rows.map(({ index, item }) => {
                const Icon = item.icon
                const active = index === activeIndex
                return (
                  <button
                    aria-disabled={item.disabled || undefined}
                    className={cn(
                      'relative flex w-full min-w-0 items-center gap-2.5 px-2.5 py-1.5 text-left transition-colors',
                      item.disabled
                        ? 'cursor-not-allowed text-muted-foreground/55'
                        : active
                          ? 'bg-accent'
                          : 'hover:bg-accent/50',
                    )}
                    disabled={item.disabled}
                    key={item.id}
                    onMouseDown={(event) => {
                      event.preventDefault()
                      if (!item.disabled) onSelect(index)
                    }}
                    onMouseEnter={() => {
                      if (!item.disabled) onHover(index)
                    }}
                    ref={active ? activeRef : undefined}
                    type="button"
                  >
                    {active && !item.disabled ? <span className="absolute inset-y-1 left-0 w-0.5 rounded-full bg-brand" /> : null}
                    <Icon className={cn(
                      'size-4 shrink-0',
                      item.disabled
                        ? 'text-muted-foreground/35'
                        : active
                          ? 'text-brand'
                          : 'text-muted-foreground/70',
                    )} />
                    <span className="min-w-0 flex-1">
                      <span className={cn(
                        'block truncate text-[13px] font-semibold',
                        item.disabled ? 'text-muted-foreground/55' : 'text-foreground',
                      )}>{item.label}</span>
                      {item.description ? (
                        <span className="block truncate text-[11px] text-muted-foreground">{item.description}</span>
                      ) : null}
                    </span>
                    {active && !item.disabled ? <Kbd>↵</Kbd> : null}
                  </button>
                )
              })}
            </div>
          ))}
        </div>
      )}

      <div className="flex items-center gap-3 border-t border-border bg-surface/40 px-2.5 py-1.5 text-[10px] text-muted-foreground/70">
        <span className="inline-flex items-center gap-1">
          <Kbd>↑</Kbd>
          <Kbd>↓</Kbd>
          {navHint}
        </span>
        <span className="inline-flex items-center gap-1">
          <Kbd>↵</Kbd>
          {selectHint}
        </span>
        <span className="ml-auto inline-flex items-center gap-1">
          <Kbd>Esc</Kbd>
          {closeHint}
        </span>
      </div>
    </div>
  )
}
