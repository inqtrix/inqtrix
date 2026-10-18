import { useState } from 'react'

import { Check, ChevronDown, MoreHorizontal } from '@/components/icons'
import { reasoningLevelOptions } from '@/lib/modelCard'
import { cn } from '@/lib/utils'

/** Inline segment budget before levels spill into the overflow dropdown. Keeps
 * the segmented control at 2–4 inline options so it stays narrow. */
const INLINE_MAX = 4

type ReasoningEffortControlProps = {
  /** The model's accepted effort tokens (`ModelCard.reasoning_levels`). */
  levels: string[]
  selectedEffort: string | null
  onEffortChange: (effort: string | null) => void
  /** Eyebrow label (e.g. "Reasoning"). */
  label: string
}

/**
 * Model-dependent reasoning-effort selector: every supported level is
 * selectable. The first {@link INLINE_MAX} levels render as a segmented control;
 * any remaining (highest) levels collapse into a compact `⋯` dropdown so the
 * picker stays narrow. The active level is marked with the brand accent
 * (the brand accent marks selection); the overflow trigger itself turns brand
 * when the active level lives inside it. Renders nothing when the model exposes
 * no effort control.
 */
export function ReasoningEffortControl({
  levels,
  selectedEffort,
  onEffortChange,
  label,
}: ReasoningEffortControlProps) {
  const options = reasoningLevelOptions(levels)
  const [overflowOpen, setOverflowOpen] = useState(false)
  if (options.length === 0) return null

  const inline = options.slice(0, INLINE_MAX)
  const overflow = options.slice(INLINE_MAX)
  const overflowActive = overflow.find((option) => option.token === selectedEffort) ?? null

  // Clicking the active level again clears it (back to the model/server default).
  const select = (token: string) => {
    onEffortChange(selectedEffort === token ? null : token)
    setOverflowOpen(false)
  }

  return (
    <div className="flex items-center gap-2 border-t border-border bg-surface/40 px-2.5 py-1.5">
      <span className="t-caption text-muted-foreground/65">{label}</span>
      <div className="relative ml-auto flex items-center gap-0.5 rounded-md bg-surface p-0.5">
        {inline.map((option) => (
          <button
            className={cn(
              'h-6 rounded px-2 text-xs font-medium transition-colors',
              selectedEffort === option.token
                ? 'bg-brand-subtle text-brand'
                : 'text-muted-foreground hover:text-foreground',
            )}
            key={option.token}
            onClick={() => select(option.token)}
            type="button"
          >
            {option.label}
          </button>
        ))}
        {overflow.length > 0 ? (
          <>
            <button
              aria-expanded={overflowOpen}
              aria-haspopup="menu"
              className={cn(
                'flex h-6 items-center gap-0.5 rounded px-1.5 text-xs font-medium transition-colors',
                overflowActive
                  ? 'bg-brand-subtle text-brand'
                  : 'text-muted-foreground hover:text-foreground',
              )}
              onClick={() => setOverflowOpen((open) => !open)}
              type="button"
            >
              {overflowActive ? overflowActive.label : <MoreHorizontal className="size-3.5" />}
              <ChevronDown className={cn('size-3 transition-transform', overflowOpen && 'rotate-180')} />
            </button>
            {overflowOpen ? (
              <div className="absolute bottom-full right-0 z-50 mb-1 min-w-[7rem] overflow-hidden rounded-lg border border-border bg-card p-1 shadow-lg">
                {overflow.map((option) => {
                  const active = selectedEffort === option.token
                  return (
                    <button
                      className={cn(
                        'flex w-full items-center gap-2 rounded px-2 py-1 text-left text-xs font-medium transition-colors',
                        active ? 'bg-brand-subtle text-brand' : 'text-foreground hover:bg-accent/60',
                      )}
                      key={option.token}
                      onClick={() => select(option.token)}
                      type="button"
                    >
                      <span className="flex-1">{option.label}</span>
                      {active ? <Check className="size-3.5 text-brand" /> : null}
                    </button>
                  )
                })}
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </div>
  )
}
