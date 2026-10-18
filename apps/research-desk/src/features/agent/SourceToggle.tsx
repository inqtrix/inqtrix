import type { ComponentType } from 'react'

import { cn } from '@/lib/utils'

export type SourceToggleState = 'on' | 'off' | 'unavailable'

/**
 * ONE glyph for both composer sources (web, project knowledge): on = brand
 * chip, off = struck-through glyph, unavailable = greyed with the server's
 * reason. A locked composer (run in flight, surface transition) keeps the
 * state visible and only stops reacting. An unavailable source can still be
 * switched OFF — the user must be able to leave a state the server refuses.
 */
export function SourceToggle({
  hint,
  icon: Icon,
  label,
  locked,
  onToggle,
  revealLabel,
  source,
  state,
  switchedOn,
}: {
  /** Full-sentence state description (tooltip + aria-description). */
  hint: string
  icon: ComponentType<{ className?: string }>
  /** The accessible name of the control (the source). */
  label: string
  locked: boolean
  onToggle: () => void
  /** Short label revealed beside the glyph on hover. */
  revealLabel: string
  source: 'web' | 'knowledge'
  state: SourceToggleState
  /** The persisted switch behind the state — an unavailable source that is
   * still switched on may be switched off. */
  switchedOn: boolean
}) {
  const isOn = state === 'on'
  const interactive = !locked && (state !== 'unavailable' || switchedOn)
  return (
    <button
      aria-description={hint}
      aria-disabled={!interactive}
      aria-label={label}
      aria-pressed={switchedOn}
      className={cn(
        'agent-source-toggle relative inline-flex h-6 min-w-6 items-center justify-center gap-1 overflow-hidden rounded px-1.5 text-xs font-medium',
        isOn
          ? 'agent-source-toggle-on bg-brand-subtle text-brand shadow-[0_1px_2px_var(--shadow-hairline)]'
          : 'text-muted-foreground/60',
        state === 'unavailable' && 'opacity-50',
        !interactive && 'cursor-not-allowed',
      )}
      data-source={source}
      data-source-state={state}
      onClick={() => {
        if (!interactive) return
        onToggle()
      }}
      title={hint}
      type="button"
    >
      <span
        aria-hidden="true"
        className={cn(
          'agent-source-glyph relative grid size-4 shrink-0 place-items-center',
          `agent-source-glyph-${source}`,
          state === 'off' && 'agent-source-glyph-off',
        )}
      >
        <Icon className="icon-sm" />
        <span className="agent-source-glyph-tracer" />
      </span>
      <span className="agent-source-toggle-label grid">
        <span className="overflow-hidden whitespace-nowrap">{revealLabel}</span>
      </span>
    </button>
  )
}
