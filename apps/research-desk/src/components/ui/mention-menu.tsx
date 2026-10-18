import { useEffect, useRef } from 'react'
import { ChevronLeft, ChevronRight, type LucideIcon } from '@/components/icons'
import { Kbd } from '@/components/ui/kbd'
import { cn } from '@/lib/utils'
import { toneBar, toneChip, toneText, type MentionTone } from '@/lib/tone'

export type { MentionTone }

export type MentionMenuOption = {
  /** Human-readable primary line (category: description, item: title). */
  primary: string
  /** Secondary line (category: prefix, item: `@kind:handle` in mono). */
  secondary: string
  icon: LucideIcon
  tone: MentionTone
  /** Category rows show a drill-in chevron; item rows show the enter hint. */
  isCategory: boolean
  /** Optional group label; consecutive equal values render under one header. */
  group?: string
  /** Listed but not pickable; `secondary` says why. A click selects
   * nothing — the reason is legible before it, never after, so its text
   * wraps instead of being cut. */
  disabled?: boolean
  /** A sentence about the situation (nothing to list, or why nothing can
   * be picked) rather than an entry. Its text wraps instead of being cut
   * and it is never dimmed; with `disabled` it has no action, without it
   * picking it runs one (opening the prompt library, for example). */
  state?: boolean
}

export type MentionMenuScope = {
  /** null = root level (no breadcrumb, no back affordance). */
  kind: string | null
  query: string
  icon?: LucideIcon
  tone?: MentionTone
  /** What the header says on its right in place of the row count (a
   * scope's own budget, for example). */
  value?: string
}

export type MentionMenuLabels = {
  rootTitle: string
  filterPlaceholder: string
  navHint: string
  selectHint: string
  closeHint: string
  backHint: string
}

/**
 * Presentation-only mention popover shared by every `@`-autocomplete surface
 * (chat composer and editor composer). It owns no trigger, keyboard, or
 * selection logic: the caller supplies the resolved scope, options, active
 * index, and i18n labels, and receives select/hover/back intents back. Options
 * arrive in display order, so `index` is the flat index used for both hover and
 * keyboard navigation; grouping only inserts headers between consecutive runs
 * of the same `group` value and never reorders.
 */
export function MentionMenu({
  scope,
  options,
  activeIndex,
  labels,
  onSelect,
  onHover,
  onBack,
}: {
  scope: MentionMenuScope
  options: MentionMenuOption[]
  activeIndex: number
  labels: MentionMenuLabels
  onSelect: (index: number) => void
  onHover: (index: number) => void
  onBack?: () => void
}) {
  const isRoot = scope.kind == null
  const canGoBack = !isRoot && Boolean(onBack)
  const ScopeIcon = scope.icon

  const groups: { label: string | null; items: { index: number; option: MentionMenuOption }[] }[] = []
  options.forEach((option, index) => {
    const label = option.group ?? null
    const last = groups[groups.length - 1]
    if (last && last.label === label) last.items.push({ index, option })
    else groups.push({ items: [{ index, option }], label })
  })

  // Keep the keyboard-highlighted row in the (`max-h-64 overflow-y-auto`)
  // viewport — `activeIndex` moves on arrow keys but never scrolled on its own.
  const activeRef = useRef<HTMLButtonElement>(null)
  useEffect(() => {
    activeRef.current?.scrollIntoView({ block: 'nearest' })
  }, [activeIndex])

  return (
    <div className="absolute bottom-full left-0 z-30 mb-2 w-max min-w-[17rem] max-w-md overflow-hidden rounded-xl border border-border bg-popover shadow-lg animate-in fade-in slide-in-from-bottom-1 motion-reduce:animate-none">
      <div className="flex items-center gap-1.5 border-b border-border px-2.5 py-1.5">
        {isRoot ? (
          <span className="text-[11px] font-medium text-muted-foreground">{labels.rootTitle}</span>
        ) : (
          <span className="flex min-w-0 items-center gap-1.5 text-[11px]">
            {canGoBack ? (
              <button
                aria-label={labels.backHint}
                className="-ml-1 flex shrink-0 items-center rounded p-0.5 text-muted-foreground/70 transition-colors hover:bg-accent hover:text-foreground"
                onMouseDown={(event) => {
                  event.preventDefault()
                  onBack?.()
                }}
                title={labels.backHint}
                type="button"
              >
                <ChevronLeft className="size-3.5" />
              </button>
            ) : null}
            <span className={cn('inline-flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 font-medium', scope.tone ? toneChip[scope.tone] : '')}>
              {ScopeIcon ? <ScopeIcon className="size-3" /> : null}
              {scope.kind}
            </span>
            <ChevronRight className="size-3 shrink-0 text-muted-foreground/40" />
            <span className="truncate text-muted-foreground">
              {scope.query || labels.filterPlaceholder}
            </span>
          </span>
        )}
        {scope.value === undefined ? (
          <span className="ml-auto shrink-0 text-[10px] tabular-nums text-muted-foreground/50">{options.length}</span>
        ) : (
          // A sentence, not a count: it takes only the space the scope label
          // leaves (never drawn under the label), is cut there with its full
          // text on hover, and reads at full muted contrast.
          <span
            className="min-w-16 flex-1 truncate text-right text-[10px] tabular-nums text-muted-foreground"
            title={scope.value}
          >
            {scope.value}
          </span>
        )}
      </div>

      <div className="max-h-64 overflow-y-auto py-1">
        {groups.map((group, groupIndex) => (
          <div key={group.label ?? `group-${groupIndex}`}>
            {group.label ? (
              <div className="px-2.5 pb-0.5 pt-1.5 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground/60">
                {group.label}
              </div>
            ) : null}
            {group.items.map(({ index, option }) => {
              const Icon = option.icon
              const active = index === activeIndex
              // A reason, a state or a category's note (which can be the
              // reason the category takes no pick) is read in full: it
              // wraps where a pickable entry stays on one line.
              const text = option.disabled || option.state || option.isCategory
                ? 'block whitespace-normal break-words'
                : 'block truncate'
              return (
                <button
                  aria-disabled={option.disabled || undefined}
                  className={cn(
                    'relative flex w-full min-w-0 items-center gap-2.5 px-2.5 py-1.5 text-left transition-colors',
                    active ? 'bg-accent' : 'hover:bg-accent/50',
                    option.disabled && (option.state ? 'cursor-default' : 'cursor-not-allowed opacity-50'),
                  )}
                  key={`${option.secondary}-${index}`}
                  onMouseDown={(event) => {
                    event.preventDefault()
                    if (!option.disabled) onSelect(index)
                  }}
                  onMouseEnter={() => onHover(index)}
                  ref={active ? activeRef : undefined}
                  type="button"
                >
                  {active ? (
                    <span className={cn('absolute inset-y-1 left-0 w-0.5 rounded-full', toneBar[option.tone])} />
                  ) : null}
                  <Icon className={cn('size-4 shrink-0', active ? toneText[option.tone] : 'text-muted-foreground/70')} />
                  <span className="min-w-0 flex-1">
                    <span className={cn(text, 'text-[13px] font-semibold text-foreground')}>{option.primary}</span>
                    <span className={cn(text, 'text-[11px] text-muted-foreground')}>{option.secondary}</span>
                  </span>
                  {option.isCategory ? (
                    <ChevronRight className="size-3.5 shrink-0 text-muted-foreground/40" />
                  ) : active && !option.disabled ? (
                    <Kbd>↵</Kbd>
                  ) : null}
                </button>
              )
            })}
          </div>
        ))}
      </div>

      <div className="flex items-center gap-3 border-t border-border bg-surface/40 px-2.5 py-1.5 text-[10px] text-muted-foreground/70">
        <span className="inline-flex items-center gap-1">
          <Kbd>↑</Kbd>
          <Kbd>↓</Kbd>
          {labels.navHint}
        </span>
        <span className="inline-flex items-center gap-1">
          <Kbd>↵</Kbd>
          {labels.selectHint}
        </span>
        {canGoBack ? (
          <span className="inline-flex items-center gap-1">
            <Kbd>⌫</Kbd>
            {labels.backHint}
          </span>
        ) : null}
        <span className="ml-auto inline-flex items-center gap-1">
          <Kbd>Esc</Kbd>
          {labels.closeHint}
        </span>
      </div>
    </div>
  )
}
