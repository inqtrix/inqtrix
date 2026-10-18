import { Gauge } from '@/components/icons'
import { useMemo } from 'react'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import {
  buildQuotaMeterModel,
  formatQuotaAmount,
  quotaBarFractionClass,
  quotaBarWidth,
  quotaFooter,
  type QuotaDimensionKey,
  type QuotaMeterDimension,
  type QuotaMeterThreshold,
} from './model'
import { useQuotaMeterGate, useQuotaUsageData } from './QuotaMeterContext'

type QuotaMeterProps = {
  disabled?: boolean
}

/** Threshold -> number/glyph colour: calm while healthy, status hue when tight. */
function thresholdTextClass(threshold: QuotaMeterThreshold): string {
  if (threshold === 'critical') return 'text-destructive'
  if (threshold === 'warning') return 'text-warning'
  return 'text-muted-foreground'
}

export function QuotaMeter({ disabled = false }: QuotaMeterProps) {
  const { t, locale } = useLocale()
  const { enabled } = useQuotaMeterGate()
  const { rows, loadFailed } = useQuotaUsageData()
  const model = useMemo(() => buildQuotaMeterModel(rows), [rows])

  if (!enabled) return null

  const labels: Record<QuotaDimensionKey, string> = {
    runs: t.quota.dimRuns,
    llm_tokens: t.quota.dimLlmTokens,
    embedding_tokens: t.quota.dimEmbeddingTokens,
    stored_bytes: t.quota.dimStoredBytes,
  }
  const colour = thresholdTextClass(model.threshold)
  const pct =
    loadFailed || model.worstFraction == null
      ? null
      : Math.round(Math.min(1, model.worstFraction) * 100)
  const footer = quotaFooter(model)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={t.quota.meterTitle}
          className={cn(
            'h-7 min-w-0 shrink-0 gap-1 rounded-md px-1.5 text-xs font-semibold hover:bg-accent/70 hover:text-foreground focus-visible:ring-1',
            'data-[state=open]:bg-accent data-[state=open]:text-foreground',
            // The shared Button forces svg to size-4 (16px); the footer
            // idiom and the neighbouring context-token ring are 14px, so
            // pin the glyph to size-3.5 here (same arbitrary-variant key
            // -> tailwind-merge drops the base size-4).
            '[&_svg]:size-3.5',
            colour,
          )}
          disabled={disabled}
          type="button"
          variant="ghost"
        >
          <Gauge aria-hidden="true" />
          {pct != null ? (
            <span className="t-mono tabular-nums">{pct}%</span>
          ) : null}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent
        align="end"
        className="w-72 max-w-[calc(100vw-2rem)] rounded-xl p-0 shadow-lg"
        side="top"
        sideOffset={8}
      >
        <div className="flex items-center gap-1.5 border-b border-border px-2.5 py-1.5">
          <span className="t-meta-sm font-medium text-muted-foreground">
            {t.quota.meterTitle}
          </span>
          {pct != null ? (
            <span className={cn('ml-auto t-mono tabular-nums', colour)}>
              {pct}%
            </span>
          ) : null}
        </div>

        {loadFailed ? (
          <p className="px-2.5 py-3 t-meta-sm text-muted-foreground">
            {t.quota.loadFailed}
          </p>
        ) : (
          <>
            <ul className="grid gap-2 px-2.5 py-2">
              {model.dimensions.map((dimension) => (
                <QuotaDimensionRow
                  dimension={dimension}
                  key={dimension.key}
                  label={labels[dimension.key]}
                  unlimitedLabel={t.quota.unlimited}
                />
              ))}
            </ul>

            {footer.kind === 'exceeded' ? (
              <p className="border-t border-border px-2.5 pb-2 pt-1.5 t-hint font-medium text-destructive">
                {t.quota.exceeded}
              </p>
            ) : (
              <p className="border-t border-border px-2.5 pb-2 pt-1.5 t-hint text-muted-foreground/70">
                {footer.kind === 'reset'
                  ? `${t.quota.resetsAt} ${new Date(
                      footer.resetAt * 1000,
                    ).toLocaleDateString(locale, {
                      day: 'numeric',
                      month: 'short',
                      timeZone: 'UTC',
                    })}`
                  : t.quota.stockHint}
              </p>
            )}
          </>
        )}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function QuotaDimensionRow({
  dimension,
  label,
  unlimitedLabel,
}: {
  dimension: QuotaMeterDimension
  label: string
  unlimitedLabel: string
}) {
  const usedLabel = formatQuotaAmount(dimension.key, dimension.used)
  const value =
    dimension.limit == null
      ? `${usedLabel} · ${unlimitedLabel}`
      : `${usedLabel} / ${formatQuotaAmount(dimension.key, dimension.limit)}`
  const width = quotaBarWidth(dimension.fraction)
  return (
    <li className="grid gap-0.5">
      <div className="flex items-baseline gap-2">
        <span className="t-meta-sm text-muted-foreground">{label}</span>
        <span className="ml-auto t-mono tabular-nums text-foreground">{value}</span>
      </div>
      <span className="block h-1 w-full overflow-hidden rounded-full bg-muted">
        <span
          className={cn('block h-full rounded-full', quotaBarFractionClass(dimension.fraction))}
          style={{ width: `${width}%` }}
        />
      </span>
    </li>
  )
}
