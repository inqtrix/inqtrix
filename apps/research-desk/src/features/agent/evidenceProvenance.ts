import type { AgentArtifactReference } from './artifactCitations'

export type WebSearchCitationLineage = {
  citationId: string | null
  groundedSupport: string | null
  mappingStatus:
    | 'provider_answer_context'
    | 'provider_citation_marker'
    | 'provider_snippet'
    | 'source_only'
  origin: string | null
  providerSnippet: string | null
  rank: number | null
  sourceId: string | null
  title: string | null
  url: string | null
}

export type WebSearchLineage = {
  citation: WebSearchCitationLineage | null
  durationMs: number | null
  finishedAt: string | null
  invocationId: string
  notice: string | null
  provider: string | null
  providerAnswer: string | null
  query: string | null
  queryId: string
  sourceRunId: string | null
  startedAt: string | null
  status: string | null
}

export type AgentEvidenceLineage = {
  searches: WebSearchLineage[]
}

/**
 * Resolve a web citation against the persisted provider-search ledger.
 *
 * The ledger contains exactly what the configured web-search provider
 * returned: query metadata, its coherent grounded answer and citation rows.
 * This projection never fetches the linked pages and never invents a
 * one-to-one passage mapping where the provider supplied only a source list.
 */
export function evidenceLineageFromArtifactPayload(
  payload: Record<string, unknown> | undefined,
  reference: AgentArtifactReference,
): AgentEvidenceLineage | null {
  if (!payload || reference.documentId) return null
  const ledger = objectField(payload, 'web_search_ledger')
  const searches = ledger && objectField(ledger, 'searches')
  if (!searches) return null

  const requestedQueryIds = uniqueStrings([
    ...reference.queryIds,
    reference.queryId,
  ])
  const citationIds = new Set(uniqueStrings([
    ...reference.citationIds,
    reference.citationId,
  ]))
  const sourceRunIds = new Set(uniqueStrings([
    ...reference.sourceRunIds,
    reference.sourceRunId,
  ]))
  const candidates = Object.entries(searches)
    .filter(([queryId, search]) => (
      isRecord(search)
      && (
        requestedQueryIds.length === 0
        || requestedQueryIds.includes(queryId)
        || requestedQueryIds.includes(stringField(search, 'query_id'))
      )
    ))
    .flatMap<WebSearchLineage>(([queryId, rawSearch]) => {
      if (!isRecord(rawSearch)) return []
      if (
        sourceRunIds.size > 0
        && stringField(rawSearch, 'source_run_id')
        && !sourceRunIds.has(stringField(rawSearch, 'source_run_id'))
      ) return []
      const citations = recordArray(rawSearch.citations)
      const citation = selectCitation(
        citations,
        reference,
        citationIds,
      )
      if (
        requestedQueryIds.length === 0
        && !citation
      ) return []
      return [{
        citation: citation ? citationLineage(citation, reference) : null,
        durationMs: nullableNumber(rawSearch.duration_ms),
        finishedAt: stringField(rawSearch, 'finished_at') || null,
        invocationId: stringField(rawSearch, 'invocation_id')
          || stringField(rawSearch, 'query_id')
          || queryId,
        notice: stringField(rawSearch, 'notice') || null,
        provider: stringField(rawSearch, 'provider') || null,
        providerAnswer: stringField(rawSearch, 'provider_answer') || null,
        query: stringField(rawSearch, 'query') || null,
        queryId: stringField(rawSearch, 'query_id') || queryId,
        sourceRunId: stringField(rawSearch, 'source_run_id') || null,
        startedAt: stringField(rawSearch, 'started_at') || null,
        status: stringField(rawSearch, 'status') || null,
      }]
    })

  return candidates.length > 0 ? { searches: candidates } : null
}

export function safeEvidenceHttpUrl(value: string | null): string | null {
  if (!value) return null
  try {
    const url = new URL(value)
    return url.protocol === 'http:' || url.protocol === 'https:'
      ? url.toString()
      : null
  } catch {
    return null
  }
}

function selectCitation(
  citations: Record<string, unknown>[],
  reference: AgentArtifactReference,
  citationIds: ReadonlySet<string>,
): Record<string, unknown> | null {
  return citations.find((citation) => (
    citationIds.size > 0
    && citationIds.has(stringField(citation, 'citation_id'))
  ))
    ?? citations.find((citation) => (
      Boolean(reference.sourceId)
      && stringField(citation, 'source_id') === reference.sourceId
    ))
    ?? citations.find((citation) => sameUrl(
      stringField(citation, 'url'),
      reference.url,
    ))
    ?? null
}

function citationLineage(
  citation: Record<string, unknown>,
  reference: AgentArtifactReference,
): WebSearchCitationLineage {
  const mappingStatus = stringField(citation, 'mapping_status')
  return {
    citationId: stringField(citation, 'citation_id')
      || reference.citationId,
    groundedSupport: stringField(citation, 'grounded_support')
      || reference.groundedSupport,
    mappingStatus: (
      mappingStatus === 'provider_answer_context'
      || mappingStatus === 'provider_citation_marker'
      || mappingStatus === 'provider_snippet'
      || mappingStatus === 'source_only'
    ) ? mappingStatus : (
      stringField(citation, 'grounded_support')
        ? 'provider_answer_context'
        : stringField(citation, 'snippet')
          ? 'provider_snippet'
          : 'source_only'
    ),
    origin: stringField(citation, 'origin') || null,
    providerSnippet: stringField(citation, 'snippet')
      || reference.providerSnippet,
    rank: nullableNumber(citation.rank),
    sourceId: stringField(citation, 'source_id') || reference.sourceId,
    title: stringField(citation, 'title') || reference.title || null,
    url: stringField(citation, 'url') || reference.url,
  }
}

function objectField(
  record: Record<string, unknown>,
  key: string,
): Record<string, unknown> | null {
  const value = record[key]
  return isRecord(value) ? value : null
}

function recordArray(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.filter(isRecord) : []
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function stringField(
  record: Record<string, unknown> | null,
  key: string,
): string {
  const value = record?.[key]
  return typeof value === 'string' ? value.trim() : ''
}

function nullableNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function uniqueStrings(values: Array<string | null>): string[] {
  return [...new Set(values.filter((value): value is string => Boolean(value)))]
}

function sameUrl(left: string, right: string | null): boolean {
  if (!left || !right) return false
  try {
    const a = new URL(left)
    const b = new URL(right)
    a.hash = ''
    b.hash = ''
    return a.toString() === b.toString()
  } catch {
    return left === right
  }
}
