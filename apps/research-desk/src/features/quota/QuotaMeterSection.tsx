import type { LucideIcon } from '@/components/icons'
import { cn } from '@/lib/utils'
import {
  formatQuotaAmount,
  quotaBarFractionClass,
  quotaBarWidth,
  type QuotaDimensionKey,
} from './model'

type QuotaMeterSectionProps = {
  /** Leading tone glyph (e.g. Layers for embedding, Coins for LLM tokens). */
  icon: LucideIcon
  /** Section header label (already localised). */
  label: string
  /** Drives the natural unit of the value via {@link formatQuotaAmount}. */
  dimension: QuotaDimensionKey
  used: number
  /** Effective limit, or ``null`` for unlimited (no bar, "used . unlimited"). */
  limit: number | null
  /** Already-formatted period label (e.g. month), shown right of the header. */
  periodLabel?: string
  /** Unit suffix for the limited value (e.g. "tokens"); omitted -> none. */
  unitLabel?: string
  /** Localised "unlimited" word for the unlimited value. */
  unlimitedLabel: string
  /** Extra classes on the root (e.g. a top hairline for a follow-on section). */
  className?: string
}

/** One quota-dimension meter row in the Database-footer visual language:
 * header (tone icon + label + optional period) over a value line over a
 * coloured utilisation bar.
 *
 * Single owner of the threshold/banding inputs: it derives
 * ``fraction``/``exhausted`` itself from ``used``/``limit`` and never accepts
 * pre-computed values, so the green/amber/red rule lives in exactly one place
 * (here, over the shared {@link quotaBarFractionClass}/{@link quotaBarWidth}).
 * Both the Database footer and the rail {@link QuotaUsageFooter} render through
 * this, so they stay pixel-identical.
 */
export function QuotaMeterSection({
  icon: Icon,
  label,
  dimension,
  used,
  limit,
  periodLabel,
  unitLabel,
  unlimitedLabel,
  className,
}: QuotaMeterSectionProps) {
  const limited = limit != null && limit > 0 ? limit : null
  const fraction = limited != null ? used / limited : null
  const exhausted = limited != null && used >= limited
  const value =
    limited != null
      ? `${formatQuotaAmount(dimension, used)} / ${formatQuotaAmount(dimension, limited)}${unitLabel ? ` ${unitLabel}` : ''}`
      : `${formatQuotaAmount(dimension, used)} · ${unlimitedLabel}`

  return (
    <section className={className}>
      <div className="flex flex-wrap items-center justify-between gap-x-2 gap-y-0.5">
        <span className="inline-flex items-center gap-1.5 t-label text-foreground">
          <Icon className="size-3.5 shrink-0 text-muted-foreground" />
          <span>{label}</span>
        </span>
        {periodLabel ? (
          <span className="shrink-0 t-meta-sm text-muted-foreground">{periodLabel}</span>
        ) : null}
      </div>
      <p
        className={cn(
          'mt-1 t-meta-sm tabular-nums',
          exhausted ? 'text-destructive' : 'text-muted-foreground',
        )}
      >
        {value}
      </p>
      {limited != null ? (
        <span className="mt-2 block h-1.5 overflow-hidden rounded-full bg-muted">
          <span
            className={cn('block h-full rounded-full', quotaBarFractionClass(fraction))}
            style={{ width: `${quotaBarWidth(fraction)}%` }}
          />
        </span>
      ) : null}
    </section>
  )
}
