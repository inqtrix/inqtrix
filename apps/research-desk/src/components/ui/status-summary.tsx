import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

/**
 * Shared building blocks for a read-only "what is configured" summary inside a
 * dropdown — a labelled group of rows, each a label on the left and a tone-coded
 * value pill on the right. Used by the Research Desk run-settings overview and the
 * Knowledge Desk profile/retrieval overview so both read as one design.
 */

/** A labelled section of status rows (uppercase eyebrow + the rows). */
export function SummaryGroup({ children, label }: { children: ReactNode; label: string }) {
  return (
    <div>
      <div className="px-2.5 pb-0.5 pt-1.5 t-caption text-muted-foreground/60">{label}</div>
      <div className="grid gap-0.5">{children}</div>
    </div>
  )
}

export type StatusRowTone = 'default' | 'muted' | 'success' | 'warning'

/** One status line: a muted label and a tone-coded value pill. */
export function StatusRow({
  label,
  tone = 'default',
  value,
}: {
  label: string
  tone?: StatusRowTone
  value: string
}) {
  return (
    <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-3 px-2.5 py-1">
      <span className="truncate t-meta-sm text-muted-foreground">{label}</span>
      <span
        className={cn(
          'max-w-36 truncate rounded-md px-1.5 py-0.5 text-right t-meta-sm font-medium',
          tone === 'success' && 'bg-success-subtle text-success',
          tone === 'warning' && 'bg-warning-subtle text-warning',
          tone === 'muted' && 'bg-surface text-muted-foreground',
          tone === 'default' && 'bg-background text-foreground',
        )}
      >
        {value}
      </span>
    </div>
  )
}
