import { useMemo } from 'react'
import { FileText, SearchCheck } from '@/components/icons'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { cn } from '@/lib/utils'
import type { KnowledgeSearchHit, KnowledgeSearchWarning } from '@/features/researchRuns/types'
import type { DocumentHitGroup } from './findGrouping'
import { findTermMatches, searchTermsFromQuery, splitByRanges } from './highlight'
import { RetrievalDegradationNotice } from './RetrievalDegradationNotice'
import { knowledgeSearchWarningNotice } from './retrievalDegradation'

export type FindResultsState = 'idle' | 'short' | 'searching' | 'ready' | 'error'

/**
 * Finden result list: one block per document (title, collection, hit
 * count) with up to three snippet rows; query terms are emphasized via
 * the brand-subtle mark treatment. Clicking a snippet opens the
 * document reader at that passage.
 */
export function DocumentFindResults({
  collectionTitleFor,
  error,
  groups,
  onOpenSnippet,
  query,
  state,
  warnings,
}: {
  collectionTitleFor: (collectionId: string) => string | null
  error: string | null
  groups: DocumentHitGroup[]
  onOpenSnippet: (hit: KnowledgeSearchHit) => void
  query: string
  state: FindResultsState
  warnings: KnowledgeSearchWarning[]
}) {
  const { t } = useLocale()
  const terms = useMemo(() => searchTermsFromQuery(query), [query])
  const warningNotices = warnings.map(
    (warning) => knowledgeSearchWarningNotice(warning, t.knowledge),
  )
  const warningMessages = warningNotices
    .filter((notice) => notice.tone === 'warning')
    .map((notice) => notice.message)
  const informationalMessages = warningNotices
    .filter((notice) => notice.tone === 'informational')
    .map((notice) => notice.message)

  if (state === 'idle') {
    return (
      <FindEmptyState
        hint={t.knowledge.findStartHint}
        title={t.knowledge.findStartTitle}
      />
    )
  }
  if (state === 'short') {
    return (
      <FindEmptyState
        hint={t.knowledge.findMinChars}
        title={t.knowledge.findStartTitle}
      />
    )
  }
  if (state === 'error') {
    return (
      <p className="px-1 py-6 text-center t-meta text-destructive">
        {error ?? t.knowledge.findError}
      </p>
    )
  }
  if (state === 'searching' && groups.length === 0) {
    return (
      <p className="px-1 py-6 text-center t-hint text-muted-foreground">
        {t.knowledge.findSearching}
      </p>
    )
  }
  if (groups.length === 0) {
    return (
      <div className="space-y-3">
        <RetrievalDegradationNotice announce messages={informationalMessages} tone="informational" />
        <RetrievalDegradationNotice announce messages={warningMessages} />
        <FindEmptyState
          hint={t.knowledge.findEmptyHint}
          title={t.knowledge.findEmptyTitle}
        />
      </div>
    )
  }

  return (
    <div className={cn('space-y-4', state === 'searching' && 'opacity-60')}>
      <RetrievalDegradationNotice announce messages={informationalMessages} tone="informational" />
      <RetrievalDegradationNotice announce messages={warningMessages} />
      {groups.map((group) => {
        const collectionTitle = collectionTitleFor(group.collectionId)
        const meta = [
          collectionTitle,
          countLabel(group.hitCount, t.knowledge.hitCountOne, t.knowledge.hitCount),
        ].filter(Boolean).join(' · ')
        return (
          <section key={group.documentId}>
            <div className="flex min-w-0 items-center gap-2 px-1">
              <FileText className="icon-sm shrink-0 text-muted-foreground/70" />
              <h3 className="min-w-0 truncate t-card text-foreground">{group.title}</h3>
              <span className="shrink-0 t-meta text-muted-foreground">{meta}</span>
            </div>
            <ul className="mt-1.5 space-y-1.5">
              {group.snippets.map((hit) => (
                <li key={`${hit.document_id}-${hit.chunk_index}`}>
                  <button
                    className="w-full rounded-md border border-border/70 bg-surface/50 px-3 py-2 text-left transition-colors hover:border-brand/40 hover:bg-accent/40"
                    onClick={() => onOpenSnippet(hit)}
                    type="button"
                  >
                    <SnippetText terms={terms} text={hit.excerpt} />
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )
      })}
    </div>
  )
}

function SnippetText({ terms, text }: { terms: string[]; text: string }) {
  const compact = text.replace(/\s+/g, ' ').trim()
  const display = compact.length > 360 ? `${compact.slice(0, 360)}…` : compact
  const segments = splitByRanges(display, findTermMatches(display, terms))
  return (
    <span className="line-clamp-3 t-meta text-muted-foreground">
      {segments.map((segment, index) => (
        segment.rangeIndex === null ? (
          <span key={index}>{segment.text}</span>
        ) : (
          <mark className="rounded-sm bg-brand-subtle px-0.5 font-medium text-brand" key={index}>
            {segment.text}
          </mark>
        )
      ))}
    </span>
  )
}

function FindEmptyState({ hint, title }: { hint: string; title: string }) {
  return (
    <div className="flex flex-col items-center px-4 py-10 text-center">
      <span className="flex size-10 items-center justify-center rounded-full border border-border bg-surface text-muted-foreground">
        <SearchCheck className="size-5" />
      </span>
      <p className="mt-3 t-section text-foreground">{title}</p>
      <p className="mt-1 max-w-sm t-meta text-muted-foreground">{hint}</p>
    </div>
  )
}
