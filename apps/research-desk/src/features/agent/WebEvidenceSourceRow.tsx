import { Globe2 } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { StatusBadge } from '@/features/settings/parts'
import { useLocale } from '@/i18n/LocaleProvider'
import { safeEvidenceHttpUrl } from './evidenceProvenance'
import type { TaskResultReference } from './taskResultReferences'

export function WebEvidenceSourceRow({
  onInspect,
  reference,
}: {
  onInspect?: () => void
  reference: TaskResultReference
}) {
  const { t } = useLocale()
  const safeUrl = safeEvidenceHttpUrl(reference.url)
  return (
    <div className="flex min-w-0 items-start gap-2" data-web-evidence-source="true">
      <Globe2 className="mt-0.5 icon-sm shrink-0 text-muted-foreground/70" />
      {reference.label && (
        <span className="mt-0.5 shrink-0 t-mono text-muted-foreground">
          {reference.label}
        </span>
      )}
      <span className="min-w-0 flex-1">
        {safeUrl ? (
          <a
            className="block break-words t-list text-foreground transition-colors hover:text-brand"
            href={safeUrl}
            rel="noreferrer noopener"
            target="_blank"
          >
            {reference.title}
          </a>
        ) : (
          <span className="block break-words t-list text-foreground">
            {reference.title}
          </span>
        )}
        {reference.domain && (
          <span className="block truncate t-meta-sm text-muted-foreground">
            {reference.domain}
          </span>
        )}
        <span className="mt-1 flex flex-wrap items-center gap-1.5">
          <StatusBadge
            density="table"
            label={t.agent.canvas.providerGroundedResult}
            tone="brand"
          />
          {onInspect && (
            <Button
              aria-label={`${t.agent.canvas.inspectEvidence}: ${reference.title}`}
              className="h-5 px-1.5 t-hint text-muted-foreground hover:text-foreground"
              onClick={onInspect}
              size="sm"
              type="button"
              variant="ghost"
            >
              {t.agent.canvas.inspectEvidence}
            </Button>
          )}
        </span>
      </span>
    </div>
  )
}
