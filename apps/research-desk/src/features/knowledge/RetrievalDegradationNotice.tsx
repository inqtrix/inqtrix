import { AlertTriangle, Info } from '@/components/icons'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'

export function RetrievalDegradationNotice({
  announce = false,
  messages,
  tone = 'warning',
}: {
  announce?: boolean
  messages: readonly string[]
  tone?: 'informational' | 'warning'
}) {
  const { t } = useLocale()
  const uniqueMessages = [...new Set(messages.map((message) => message.trim()).filter(Boolean))]
  if (uniqueMessages.length === 0) return null
  const informational = tone === 'informational'
  const title = informational
    ? t.knowledge.retrievalInformationalTitle
    : t.knowledge.retrievalDegradedTitle
  const Icon = informational ? Info : AlertTriangle

  return (
    <aside
      aria-label={title}
      aria-live={announce ? 'polite' : undefined}
      className={cn(
        'rounded-md border px-3 py-2.5',
        informational
          ? 'border-brand/25 bg-brand-subtle/30'
          : 'border-warning/40 bg-warning-subtle/30',
      )}
      data-knowledge-retrieval-degraded={informational ? undefined : 'true'}
      data-knowledge-retrieval-notice={tone}
    >
      <div className="flex items-start gap-2">
        <Icon className={cn(
          'mt-0.5 size-3.5 shrink-0',
          informational ? 'text-brand' : 'text-warning',
        )} />
        <div className="min-w-0">
          <p className={cn('t-caption', informational ? 'text-brand' : 'text-warning')}>
            {title}
          </p>
          <ul className={cn(
            'mt-1 space-y-0.5 t-meta',
            informational ? 'text-foreground/80' : 'text-warning/90',
          )}>
            {uniqueMessages.map((message) => <li key={message}>{message}</li>)}
          </ul>
        </div>
      </div>
    </aside>
  )
}
