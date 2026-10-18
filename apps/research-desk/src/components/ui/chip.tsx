import * as React from 'react'

import { cn } from '@/lib/utils'

/**
 * Filter / toggle pill — the single canonical chip used across the app (job
 * filters, editor comment-kind filters, report event pills, …). Fixed at the
 * design-system control size so chips look identical everywhere: `h-6`,
 * `rounded-full`, control label `text-[11px]`/`font-medium`. The active state is
 * the shared brand-subtle treatment; convey a per-tone meaning with the optional
 * `dot`. Owning the size here keeps raw `text-[11px]` out of
 * feature code (enforced by the design-lint guard).
 */
export interface ChipProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Pressed/selected state — paints the brand-subtle active treatment. */
  active?: boolean
  /** Optional leading status dot; pass a tone fill class (e.g. `bg-brand`). */
  dot?: string
  /** Optional trailing count (rendered tabular and one step down). */
  count?: number
}

export function Chip({ active = false, dot, count, className, children, ...props }: ChipProps) {
  return (
    <button
      aria-pressed={active}
      className={cn(
        'inline-flex h-6 shrink-0 items-center gap-1.5 rounded-full border px-2.5 text-[11px] font-medium transition-colors',
        active
          ? 'border-brand/40 bg-brand-subtle text-brand'
          : 'border-border bg-background text-muted-foreground hover:text-foreground',
        className,
      )}
      type="button"
      {...props}
    >
      {dot ? <span aria-hidden="true" className={cn('size-1.5 shrink-0 rounded-full', dot)} /> : null}
      {children}
      {count != null ? (
        <span className={cn('t-hint tabular-nums', active ? 'text-brand/80' : 'text-muted-foreground/80')}>
          {count}
        </span>
      ) : null}
    </button>
  )
}
