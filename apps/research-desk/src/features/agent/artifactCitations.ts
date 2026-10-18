import {
  citationLabelFromHref,
  linkifyCitationLabels,
} from '@/components/markdown/citationLinks'
import type { KnowledgeReferenceRecord } from '@/features/project/types'

export type AgentArtifactReference = {
  citationId: string | null
  citationIds: string[]
  chunkIndex: number | null
  chunkId: string | null
  collectionId: string | null
  documentId: string | null
  excerpt: string | null
  generationId: string | null
  /** Provider-grounded statement used to support the synthesized claim. It is
   * not presented as a verbatim source excerpt. */
  groundedSupport: string | null
  label: string
  pageNumber: number | null
  provenanceStatus: string | null
  providerSnippet: string | null
  queryId: string | null
  queryIds: string[]
  referenceId: string | null
  revisionId: string | null
  sourceId: string | null
  sourceRunId: string | null
  sourceRunIds: string[]
  sourceSpan: {
    start: number
    end: number
    offsetUnit: string
    documentContentHash: string | null
  } | null
  title: string
  url: string | null
}

/**
 * A reference is knowledge-backed exactly when it carries a document id —
 * the same discriminator the evidence canvas resolves on. Never test the
 * `url`: a knowledge reference carries the internal source endpoint there,
 * so a URL check reads it as a web hit and renders the wrong row.
 */
export function isKnowledgeReference(
  reference: Pick<AgentArtifactReference, 'documentId'>,
): boolean {
  return Boolean(reference.documentId)
}

/** Same scheme guard as `webUrl()` (copyAnswer): tool-provided URLs are
 * trusted data, but only http(s) ever reaches `window.open`/`href`. */
export function isWebHref(url: string): boolean {
  return /^https?:\/\//i.test(url)
}

/**
 * Whether a citation is a WEB hit and must render as an external source
 * row. Knowledge wins first — see `isKnowledgeReference` — so an index
 * citation never renders as a web result just because its internal
 * source endpoint happens to be an http URL.
 */
export function isWebEvidenceReference(
  reference: Pick<AgentArtifactReference, 'documentId' | 'queryId' | 'url'>,
): boolean {
  if (isKnowledgeReference(reference)) return false
  return Boolean(reference.queryId)
    || Boolean(reference.url && isWebHref(reference.url))
}

/**
 * Reader target for ONE knowledge citation — the same shape
 * the Knowledge Desk builds for its citations, so the shared reader
 * highlights identically in both desks.
 *
 * Highlight priority is deliberate: the server-fetched chunk text is
 * canonical document text, while the stored reference excerpt may carry
 * contextual-retrieval scaffolding that never appears in the document
 * and would therefore fail to match. Returns null for web references —
 * they have no document to open.
 */
export function agentReferenceViewerTarget(
  reference: AgentArtifactReference,
  chunkExcerpt: string | null,
  collectionLabel: string | undefined,
): {
  chunkIndex: number | null
  collectionLabel?: string
  documentId: string
  excerpt: string | null
  highlightTargets: string[]
  pageNumber: number | null
  title: string
  verified: boolean
} | null {
  if (!reference.documentId) return null
  return {
    chunkIndex: reference.chunkIndex,
    collectionLabel,
    documentId: reference.documentId,
    excerpt: chunkExcerpt ?? reference.excerpt,
    highlightTargets: [chunkExcerpt, reference.excerpt].filter(
      (value): value is string => Boolean(value && value.trim()),
    ),
    pageNumber: reference.pageNumber,
    title: reference.title,
    verified: reference.provenanceStatus === 'verified_span',
  }
}

/** Labels an agent answer may cite.
 *
 * K and W are the kernel's own knowledge and web citations. E belongs to the
 * evidence chain of a DELEGATED research run: when the kernel hands work to
 * research, that run's labels travel with its findings unchanged, and a
 * delegated finding is a real agent citation. Dropping E left the evidence
 * panel empty on exactly the answers that had done the most work.
 */
export const isAgentCitationLabel = (label: string): boolean => /^[KWE]\d+$/.test(label)

/** Normalize trusted artifact refs into one UI shape. Older rows without an
 * excerpt remain useful: title, URL and exact RAG identity still survive. */
export function agentArtifactReferences(
  refs: readonly Record<string, unknown>[] | undefined,
): AgentArtifactReference[] {
  if (!refs) return []
  return refs.flatMap((ref) => {
    const label = stringField(ref, 'label')
    if (!label || !isAgentCitationLabel(label)) return []
    const documentId = stringField(ref, 'document_id') || null
    const url = stringField(ref, 'url') || null
    const queryIds = stringArrayField(ref, 'query_ids')
    const queryId = stringField(ref, 'query_id') || queryIds[0] || null
    if (!documentId && !url && !queryId) return []
    const excerpt = firstString(ref, ['excerpt', 'source_text', 'snippet'])
    const groundedSupport = stringField(ref, 'grounded_support') || null
    const title = stringField(ref, 'title') || url || documentId || label
    return [{
      citationId: stringField(ref, 'citation_id') || null,
      citationIds: stringArrayField(ref, 'citation_ids'),
      chunkIndex: numberField(ref, 'chunk_index'),
      chunkId: stringField(ref, 'chunk_id') || null,
      collectionId: stringField(ref, 'collection_id') || null,
      documentId,
      excerpt: excerpt || null,
      generationId: stringField(ref, 'generation_id') || null,
      groundedSupport,
      label,
      pageNumber: numberField(ref, 'page_number'),
      provenanceStatus: stringField(ref, 'provenance_status') || null,
      providerSnippet: stringField(ref, 'provider_snippet') || null,
      queryId,
      queryIds,
      referenceId: stringField(ref, 'reference_id') || null,
      revisionId: stringField(ref, 'revision_id') || null,
      sourceId: stringField(ref, 'source_id') || null,
      sourceRunId: stringField(ref, 'source_run_id') || null,
      sourceRunIds: stringArrayField(ref, 'source_run_ids'),
      sourceSpan: sourceSpanField(ref, 'source_span'),
      title,
      url,
    }]
  })
}

/**
 * Which citation labels an answer renders RIGHT NOW.
 *
 * While the answer streams its real references do not exist yet, but
 * the server announces the labels the finished answer will cite
 * (`answer.started`) — and the linkifier needs only labels. Using them
 * means the markdown handed to the renderer is identical before and
 * after the answer settles; without them the whole body was rewritten
 * in one step, which reads as the message being re-inserted.
 */
export function answerCitationLabels(
  writing: boolean,
  announced: readonly string[] | undefined,
  references: readonly AgentArtifactReference[],
): AgentArtifactReference[] {
  // The real references arrive with the detail fetch AFTER the answer
  // settled; until then the announced labels keep the citations linked,
  // so the markdown is identical while streaming, at ready, and with refs.
  if (!writing && references.length > 0) return [...references]
  return (announced ?? []).map(
    (label) => ({ label }) as AgentArtifactReference,
  )
}

export function linkifyAgentArtifactCitations(
  markdown: string,
  references: readonly AgentArtifactReference[],
): string {
  return linkifyCitationLabels(
    markdown,
    isAgentCitationLabel,
    new Set(references.map((reference) => reference.label)),
    { redirectKnownExternalLinks: true, requireKnownBracketed: true },
  )
}

export function agentCitationLabelFromHref(
  href: string | null | undefined,
): string | null {
  return citationLabelFromHref(href, isAgentCitationLabel)
}

/** Adapter for the established Knowledge source-row presentation. Web refs use
 * their real URL; internal refs keep a synthetic URL only as stable identity. */
export function agentReferenceAsKnowledge(
  reference: AgentArtifactReference,
): KnowledgeReferenceRecord {
  return {
    chunkIndex: reference.chunkIndex,
    documentId: reference.documentId,
    excerpt: reference.excerpt,
    label: reference.label,
    pageNumber: reference.pageNumber,
    sourceText: reference.excerpt,
    tier: 'unknown',
    title: reference.title,
    url: reference.url
      ?? `inqtrix://documents/${reference.documentId ?? reference.label}`,
  }
}

function firstString(
  record: Record<string, unknown>,
  keys: readonly string[],
): string {
  for (const key of keys) {
    const value = stringField(record, key)
    if (value) return value
  }
  return ''
}

function stringField(record: Record<string, unknown>, key: string): string {
  const value = record[key]
  return typeof value === 'string' ? value.trim() : ''
}

function stringArrayField(
  record: Record<string, unknown>,
  key: string,
): string[] {
  const value = record[key]
  return Array.isArray(value)
    ? value.filter((item): item is string => (
      typeof item === 'string' && Boolean(item.trim())
    )).map((item) => item.trim())
    : []
}

function numberField(
  record: Record<string, unknown>,
  key: string,
): number | null {
  const value = record[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function sourceSpanField(
  record: Record<string, unknown>,
  key: string,
): AgentArtifactReference['sourceSpan'] {
  const value = record[key]
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const span = value as Record<string, unknown>
  const start = numberField(span, 'start')
  const end = numberField(span, 'end')
  const offsetUnit = stringField(span, 'offset_unit')
  if (start === null || end === null || !offsetUnit) return null
  return {
    documentContentHash:
      stringField(span, 'document_content_hash') || null,
    end,
    offsetUnit,
    start,
  }
}
