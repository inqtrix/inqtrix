import { useLocale } from '@/i18n/LocaleProvider'
import type { Locale, TranslationDictionary } from '@/i18n/translations'
import type { AutoResumePlan } from './indexingTypes'
import { autoResumeNotice, pauseCause, type AutoResumeNotice, type PauseCause } from './pauseNotice'

/** What a paused index waits for, and when the server tries again on its own. */
export function IndexPauseNotice({
  autoResume,
  errorType,
  pausedDocuments = 0,
  status,
}: {
  autoResume: AutoResumePlan | null | undefined
  errorType: string | null | undefined
  /** Paused documents of a client run; more than one is named. */
  pausedDocuments?: number
  status: string | undefined
}) {
  const { locale, t } = useLocale()
  const notice = autoResumeNotice(status, autoResume, Date.now())
  return (
    <>
      <p className="mt-0.5 t-meta-sm [overflow-wrap:anywhere]">
        {causeText(pauseCause(errorType), t)}
        {pausedDocuments > 1
          ? ` ${t.vectorIndex.pausedDocuments.replace('{count}', String(pausedDocuments))}`
          : null}
      </p>
      {notice ? (
        <p className="mt-0.5 t-meta-sm font-medium [overflow-wrap:anywhere]">
          {noticeText(notice, t, locale)}
        </p>
      ) : null}
    </>
  )
}

function causeText(cause: PauseCause, t: TranslationDictionary): string {
  switch (cause) {
    case 'contextualization':
      return t.vectorIndex.pauseCauseContextualization
    case 'circuitOpen':
      return t.vectorIndex.pauseCauseCircuitOpen
    case 'embedding':
      return t.vectorIndex.pauseCauseEmbedding
    case 'vectorStore':
      return t.vectorIndex.pauseCauseVectorStore
    case 'quota':
      return t.vectorIndex.pauseCauseQuota
    case 'unknown':
      return t.vectorIndex.pausedFallback
  }
}

function noticeText(notice: AutoResumeNotice, t: TranslationDictionary, locale: Locale): string {
  if (notice.kind === 'planned' || notice.kind === 'soon') {
    const template = notice.kind === 'planned'
      ? t.vectorIndex.autoResumePlanned.replace(
          '{time}',
          notice.at.toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' }),
        )
      : t.vectorIndex.autoResumeSoon
    return template
      .replace('{attempt}', String(notice.attempt))
      .replace('{max}', String(notice.max))
  }
  const reason = notice.reason === 'exhausted'
    ? t.vectorIndex.autoResumeStoppedExhausted
        .replace('{attempts}', String(notice.attempts))
        .replace('{max}', String(notice.max))
    : notice.reason === 'access'
      ? t.vectorIndex.autoResumeStoppedAccess
      : t.vectorIndex.autoResumeStoppedUnavailable
  return `${reason} ${t.vectorIndex.autoResumeManual}`
}
