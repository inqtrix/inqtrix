import { Coins, Layers, type LucideIcon } from '@/components/icons'
import { useLocale } from '@/i18n/LocaleProvider'
import type { TranslationDictionary } from '@/i18n/translations'
import { buildFooterSections } from './model'
import { QuotaMeterSection } from './QuotaMeterSection'
import { useQuotaMeterGate, useQuotaUsageData } from './QuotaMeterContext'

/** The quota dimensions the rail footer can surface. Only the two token
 * budgets relevant to a workspace glance (embedding for indexing, llm for
 * conversations/documents); runs/stored-bytes belong to the Database footer
 * and the full QuotaMeter dropdown, not here. */
export type FooterDimensionKey = 'embedding_tokens' | 'llm_tokens'

type FooterDimensionMeta = { icon: LucideIcon; label: string; unitLabel: string }

function footerDimensionMeta(t: TranslationDictionary): Record<FooterDimensionKey, FooterDimensionMeta> {
  return {
    embedding_tokens: {
      icon: Layers,
      label: t.vectorIndex.embeddingQuota,
      unitLabel: t.vectorIndex.tokensUnit,
    },
    llm_tokens: {
      icon: Coins,
      label: t.quota.dimLlmTokens,
      unitLabel: t.vectorIndex.tokensUnit,
    },
  }
}

type QuotaUsageFooterProps = {
  /** Dimensions to show, in order (Knowledge: embedding + llm; Chat/Editor: llm). */
  dimensions: readonly FooterDimensionKey[]
}

/** Persistent quota glance pinned to the bottom of a workspace's left rail,
 * mirroring the Database footer (Rail.tsx) for Knowledge/Chat/Editor.
 *
 * Autonomous and gated like {@link useQuotaMeterGate}'s other consumers:
 * pulls its own data, needs no props beyond the dimension list, and renders
 * NOTHING when quotas do not apply ((``return null`` keeps none/apikey
 * deployments byte-identical, no empty bordered box). A failed load is made
 * visible rather than masquerading as an empty/unlimited
 * account. Desktop-only by default (``hidden lg:block``) because the Chat/
 * Knowledge history panels are height-clamped top strips on mobile, where an
 * appended footer would crush the list — same as the Database rail itself
 * being ``hidden lg:flex``.
 */
export function QuotaUsageFooter({ dimensions }: QuotaUsageFooterProps) {
  const { enabled } = useQuotaMeterGate()
  const { rows, loadFailed } = useQuotaUsageData()
  const { locale, t } = useLocale()

  if (!enabled) return null

  const sections = buildFooterSections(rows, dimensions)
  const meta = footerDimensionMeta(t)

  return (
    <div className="hidden border-t border-border p-3 lg:block">
      {loadFailed ? (
        <p className="px-1 t-meta-sm text-muted-foreground">{t.quota.loadFailed}</p>
      ) : (
        <div className="space-y-3 px-1 py-0.5">
          {sections.map((section, index) => {
            const dimensionMeta = meta[section.dimension]
            const periodLabel =
              section.periodStart > 0
                ? new Date(section.periodStart * 1000).toLocaleDateString(locale, {
                    month: 'long',
                    timeZone: 'UTC',
                  })
                : undefined
            return (
              <QuotaMeterSection
                className={index > 0 ? 'border-t border-border/70 pt-3' : undefined}
                dimension={section.dimension}
                icon={dimensionMeta.icon}
                key={section.dimension}
                label={dimensionMeta.label}
                limit={section.limit}
                periodLabel={periodLabel}
                unitLabel={dimensionMeta.unitLabel}
                unlimitedLabel={t.quota.unlimited}
                used={section.used}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}
