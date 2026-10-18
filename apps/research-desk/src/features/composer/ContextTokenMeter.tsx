import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import type { ContextCategoryKey, ContextTokenModel } from '@/features/files/contextTokens'
import { useLocale } from '@/i18n/LocaleProvider'
import { formatTokens } from '@/lib/modelCard'
import { toneBar } from '@/lib/tone'
import { cn } from '@/lib/utils'

type ContextTokenMeterProps = {
  model: ContextTokenModel
  /** Label for the `conversation` category (chat history vs editor document). */
  conversationLabel: string
  /** Authoritative prompt tokens from the last completed send, if any. */
  actualPromptTokens?: number | null
  disabled?: boolean
}

/** Threshold → number/header colour. Stays calm (neutral) while usage is healthy
 * and only takes a status hue once the budget gets tight. */
function thresholdTextClass(threshold: ContextTokenModel['threshold']): string {
  if (threshold === 'critical') return 'text-destructive'
  if (threshold === 'warning') return 'text-warning'
  return 'text-muted-foreground'
}

/** Threshold → ring colour. The gauge ring always carries a status hue — green
 * when healthy, amber when tight, red when critical — so the circle reads at a
 * glance even at low usage (the number stays quiet, see {@link thresholdTextClass}). */
function thresholdRingClass(threshold: ContextTokenModel['threshold']): string {
  if (threshold === 'critical') return 'text-destructive'
  if (threshold === 'warning') return 'text-warning'
  if (threshold === 'unknown') return 'text-muted-foreground'
  return 'text-success'
}

export function ContextTokenMeter({
  model,
  conversationLabel,
  actualPromptTokens = null,
  disabled = false,
}: ContextTokenMeterProps) {
  const { t } = useLocale()
  const labels: Record<ContextCategoryKey, string> = {
    documents: t.chat.contextCatDocuments,
    reports: t.chat.contextCatReports,
    composer: t.chat.contextCatComposer,
    conversation: conversationLabel,
    rules: t.chat.contextCatRules,
  }
  const colour = thresholdTextClass(model.threshold)
  const ringColour = thresholdRingClass(model.threshold)
  const pctDeg =
    model.usedFraction == null ? 0 : Math.min(100, model.usedFraction * 100) * 3.6
  // Scale segments by the full window (usable + reserved) when known, else by
  // the total so the proportions still read.
  const denominator =
    model.capacityTokens == null
      ? Math.max(1, model.totalTokens)
      : model.capacityTokens + model.reservedOutputTokens
  // Per-category breakdown bars are normalised to the largest category (widest =
  // 100%) for a relative comparison, independent of the window-scaled total bar.
  const maxCategoryTokens = model.categories.reduce((max, c) => Math.max(max, c.tokens), 0)

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={t.chat.contextMeterTitle}
          className={cn(
            'h-7 min-w-0 shrink-0 gap-1 rounded-md px-1.5 text-xs font-semibold hover:bg-accent/70 hover:text-foreground focus-visible:ring-1',
            'data-[state=open]:bg-accent data-[state=open]:text-foreground',
            colour,
          )}
          disabled={disabled}
          type="button"
          variant="ghost"
        >
          {model.usedFraction == null ? (
            <span className="size-3 rounded-full border border-current opacity-70" aria-hidden="true" />
          ) : (
            <span className="size-3.5 rounded-full bg-muted" aria-hidden="true">
              <span
                className={cn('block size-full rounded-full', ringColour)}
                style={{ background: `conic-gradient(currentColor ${pctDeg}deg, transparent 0)` }}
              />
            </span>
          )}
          <span className="t-mono tabular-nums">{formatTokens(model.totalTokens)}</span>
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
            {t.chat.contextMeterTitle}
          </span>
          <span className={cn('ml-auto t-mono tabular-nums', colour)}>
            {model.capacityTokens == null
              ? `≈ ${formatTokens(model.totalTokens)}`
              : `≈ ${formatTokens(model.totalTokens)} / ${formatTokens(model.capacityTokens)}`}
          </span>
        </div>

        <div className="px-2.5 py-2">
          <div className="flex h-1.5 w-full overflow-hidden rounded-full bg-muted">
            {model.categories.map((category) => (
              <span
                className={cn('h-full', toneBar[category.tone])}
                key={category.key}
                style={{ width: `${Math.min(100, (category.tokens / denominator) * 100)}%` }}
              />
            ))}
            {model.capacityTokens != null && model.reservedOutputTokens > 0 ? (
              <span
                className="h-full bg-muted-foreground/30"
                style={{ width: `${(model.reservedOutputTokens / denominator) * 100}%` }}
                title={t.chat.contextReserved}
              />
            ) : null}
          </div>

          <ul className="mt-2 grid gap-1.5">
            {model.categories.map((category) => {
              // Normalised to the largest category (widest = 100%); min width
              // keeps a tiny category visible.
              const width = maxCategoryTokens > 0
                ? Math.max(3, (category.tokens / maxCategoryTokens) * 100)
                : 0
              return (
                <li className="grid gap-0.5" key={category.key}>
                  <div className="flex items-baseline gap-2">
                    <span className="t-meta-sm text-muted-foreground">{labels[category.key]}</span>
                    <span className="ml-auto t-mono tabular-nums text-foreground">
                      {formatTokens(category.tokens)}
                    </span>
                  </div>
                  <span className="block h-1 w-full overflow-hidden rounded-full bg-muted">
                    <span
                      className={cn('block h-full rounded-full', toneBar[category.tone])}
                      style={{ width: `${width}%` }}
                    />
                  </span>
                </li>
              )
            })}
            {model.capacityTokens != null && model.reservedOutputTokens > 0 ? (
              <li className="mt-0.5 flex items-center gap-1.5">
                <span className="size-1.5 shrink-0 rounded-full bg-muted-foreground/30" />
                <span className="t-meta-sm text-muted-foreground/80">{t.chat.contextReserved}</span>
                <span className="ml-auto t-mono tabular-nums text-muted-foreground/80">
                  {formatTokens(model.reservedOutputTokens)}
                </span>
              </li>
            ) : null}
          </ul>

          {model.usedFraction != null && model.usedFraction > 1 ? (
            <p className="mt-2 border-t border-border pt-1.5 t-hint font-medium text-destructive">
              +{formatTokens(model.totalTokens - (model.capacityTokens ?? 0))} {t.chat.contextOverflow}
            </p>
          ) : (
            <p className="mt-2 border-t border-border pt-1.5 t-hint text-muted-foreground/70">
              {model.capacityTokens == null
                ? t.chat.contextCapacityUnknown
                : t.chat.contextEstimateHint}
            </p>
          )}
          {actualPromptTokens != null ? (
            <p className="t-hint text-muted-foreground/70">
              {t.chat.contextActual}: <span className="t-mono tabular-nums">{formatTokens(actualPromptTokens)}</span>
            </p>
          ) : null}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
