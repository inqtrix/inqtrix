import type { ReactNode } from 'react'

import { ExternalLink } from '@/components/icons'
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer'
import { StatusBadge, type StatusTone } from '@/features/settings/parts'
import { useLocale } from '@/i18n/LocaleProvider'
import type { AgentArtifactReference } from './artifactCitations'
import {
  safeEvidenceHttpUrl,
  type AgentEvidenceLineage,
  type WebSearchLineage,
} from './evidenceProvenance'

/**
 * Provenance for one Agent citation.
 *
 * Knowledge references show the canonical document span. Web references show
 * the provider search that produced them: exact query, coherent provider
 * answer, citation metadata and the honest mapping precision. Linked pages
 * are not fetched or represented as independently read excerpts.
 */
export function EvidenceProvenancePanel({
  lineage,
  lineageLoadFailed = false,
  reference,
}: {
  lineage: AgentEvidenceLineage | null
  lineageLoadFailed?: boolean
  reference: AgentArtifactReference
}) {
  const { t } = useLocale()
  const headingId = `evidence-provenance-${reference.label}`
  if (reference.documentId) {
    const verified = reference.provenanceStatus === 'verified_span'
    return (
      <section
        aria-labelledby={headingId}
        className="space-y-3 border-t border-border/70 pt-4"
        data-evidence-provenance="knowledge"
      >
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 className="t-section text-foreground" id={headingId}>
            {t.agent.canvas.knowledgeProvenance}
          </h2>
          <StatusBadge
            density="table"
            label={verified
              ? t.agent.canvas.knowledgeSpanVerified
              : t.agent.canvas.knowledgeSpanLegacy}
            tone={verified ? 'success' : 'warning'}
          />
        </div>
        <EvidenceMetadata>
          <MetadataRow label={t.agent.canvas.referenceId} value={reference.referenceId} />
          <MetadataRow label={t.agent.canvas.documentId} value={reference.documentId} />
          <MetadataRow label={t.agent.canvas.collectionId} value={reference.collectionId} />
          <MetadataRow label={t.agent.canvas.chunkId} value={reference.chunkId} />
          <MetadataRow label={t.agent.canvas.revisionId} value={reference.revisionId} />
          <MetadataRow label={t.agent.canvas.generationId} value={reference.generationId} />
          <MetadataRow
            label={t.agent.canvas.sourceSpan}
            value={reference.sourceSpan
              ? `${reference.sourceSpan.start}–${reference.sourceSpan.end} ${reference.sourceSpan.offsetUnit}`
              : null}
          />
          <MetadataRow
            label={t.agent.canvas.documentContentHash}
            value={reference.sourceSpan?.documentContentHash ?? null}
          />
          <MetadataRow
            label={t.agent.canvas.provenanceStatus}
            value={reference.provenanceStatus}
          />
        </EvidenceMetadata>
      </section>
    )
  }

  return (
    <section
      aria-labelledby={headingId}
      className="space-y-4 border-t border-border/70 pt-4"
      data-evidence-provenance="web"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="t-section text-foreground" id={headingId}>
            {t.agent.canvas.webSearchLineage}
          </h2>
          <p className="mt-0.5 t-meta text-muted-foreground">
            {t.agent.canvas.webSearchLineageDescription}
          </p>
        </div>
        <StatusBadge
          density="table"
          label={t.agent.canvas.providerGroundedResult}
          tone="brand"
        />
      </div>

      {!lineage && (
        <p className="rounded-md border border-border/70 bg-surface/50 px-3 py-2 t-meta text-muted-foreground">
          {lineageLoadFailed
            ? t.agent.canvas.lineageLoadFailed
            : t.agent.canvas.lineageUnavailable}
        </p>
      )}

      {lineage?.searches.map((search, index) => (
        <WebSearchRecord
          index={index}
          key={`${search.queryId}:${search.citation?.citationId ?? reference.label}`}
          search={search}
        />
      ))}

      <EvidenceMetadata>
        <MetadataRow label={t.agent.canvas.referenceId} value={reference.referenceId} />
        <MetadataRow label={t.agent.canvas.sourceId} value={reference.sourceId} />
        <MetadataRow label={t.agent.canvas.queryId} value={reference.queryId} />
        <MetadataRow label={t.agent.canvas.citationId} value={reference.citationId} />
      </EvidenceMetadata>
    </section>
  )
}

function WebSearchRecord({
  index,
  search,
}: {
  index: number
  search: WebSearchLineage
}) {
  const { t } = useLocale()
  const citation = search.citation
  const safeUrl = safeEvidenceHttpUrl(citation?.url ?? null)
  const mappingCopy = citation
    ? citation.mappingStatus === 'provider_answer_context'
      ? t.agent.canvas.providerAnswerContextMapping
      : citation.mappingStatus === 'provider_citation_marker'
        ? t.agent.canvas.providerCitationMarkerMapping
      : citation.mappingStatus === 'provider_snippet'
        ? t.agent.canvas.providerSnippetMapping
        : t.agent.canvas.sourceOnlyMapping
    : t.agent.canvas.sourceOnlyMapping

  return (
    <article className="overflow-hidden rounded-xl border border-border/80 bg-card/70 shadow-sm">
      <header className="flex flex-wrap items-start justify-between gap-2 border-b border-border/60 bg-surface/40 px-3 py-2.5">
        <div className="min-w-0">
          <p className="t-caption uppercase tracking-wide text-muted-foreground">
            {t.agent.canvas.webSearchRecord.replace(
              '{number}',
              String(index + 1),
            )}
          </p>
          <p className="mt-0.5 break-words t-body font-medium text-foreground">
            {search.query || t.agent.canvas.searchQueryUnavailable}
          </p>
        </div>
        <StatusBadge
          density="table"
          label={search.status === 'completed' ? t.agent.canvas.webSearchCompleted : search.status || t.agent.canvas.searchStatusUnknown}
          tone={searchStatusTone(search.status)}
        />
      </header>

      <div className="space-y-4 px-3 py-3">
        <EvidenceMetadata>
          <MetadataRow label={t.agent.canvas.webSearchProvider} value={search.provider} />
          <MetadataRow label={t.agent.canvas.searchInvocationId} value={search.invocationId} />
          <MetadataRow label={t.agent.canvas.sourceRunId} value={search.sourceRunId} />
          <MetadataRow
            label={t.agent.canvas.searchDuration}
            value={search.durationMs === null
              ? null
              : `${search.durationMs.toLocaleString()} ms`}
          />
        </EvidenceMetadata>

        {citation?.groundedSupport && (
          <section className="rounded-lg border border-brand/20 bg-brand-subtle/30 px-3 py-2.5">
            <h3 className="t-caption uppercase tracking-wide text-muted-foreground">
              {citation.mappingStatus === 'provider_citation_marker'
                ? t.agent.canvas.providerCitationContext
                : t.agent.canvas.providerGroundedSupport}
            </h3>
            <div className="mt-1 break-words text-foreground/90">
              <MarkdownRenderer
                markdown={citation.groundedSupport}
                variant="report"
              />
            </div>
          </section>
        )}

        {citation?.providerSnippet && (
          <section>
            <h3 className="t-caption uppercase tracking-wide text-muted-foreground">
              {t.agent.canvas.providerSnippet}
            </h3>
            <blockquote className="mt-1 rounded-md border-l-2 border-border bg-surface/60 px-3 py-2 whitespace-pre-wrap break-words t-body text-foreground/90">
              {citation.providerSnippet}
            </blockquote>
          </section>
        )}

        <p className="rounded-md bg-surface/60 px-3 py-2 t-meta text-muted-foreground">
          {mappingCopy}
        </p>

        {search.providerAnswer && (
          <section>
            <h3 className="t-caption uppercase tracking-wide text-muted-foreground">
              {t.agent.canvas.providerSearchAnswer}
            </h3>
            <div className="mt-1 rounded-lg border border-border/70 bg-background/65 px-3 py-2">
              <MarkdownRenderer
                markdown={search.providerAnswer}
                variant="report"
              />
            </div>
          </section>
        )}

        {safeUrl && (
          <a
            className="inline-flex max-w-full items-center gap-1.5 t-meta text-brand hover:underline"
            href={safeUrl}
            rel="noreferrer noopener"
            target="_blank"
          >
            <ExternalLink className="icon-sm shrink-0" />
            <span className="truncate">
              {citation?.title || citation?.url || safeUrl}
            </span>
          </a>
        )}
      </div>
    </article>
  )
}

function searchStatusTone(status: string | null): StatusTone {
  const normalized = (status || '').toLowerCase()
  if (/^(?:completed|complete|ready|success)$/.test(normalized)) return 'success'
  if (/(?:fail|error|cancel|timeout)/.test(normalized)) return 'destructive'
  if (/(?:running|pending|queued)/.test(normalized)) return 'brand'
  return 'neutral'
}

function EvidenceMetadata({ children }: { children: ReactNode }) {
  return (
    <dl className="grid min-w-0 grid-cols-[minmax(7rem,auto)_minmax(0,1fr)] gap-x-3 gap-y-1 t-meta">
      {children}
    </dl>
  )
}

function MetadataRow({
  label,
  value,
}: {
  label: string
  value: string | null | undefined
}) {
  if (!value) return null
  return (
    <>
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-all t-mono text-foreground/85">{value}</dd>
    </>
  )
}
