export type TaskResultReference = {
  citationId?: string | null
  key: string
  label: string | null
  title: string
  url: string | null
  domain: string | null
  documentId: string | null
  chunkIndex: number | null
  pageNumber: number | null
  providerSnippet?: string | null
  queryId?: string | null
  excerpt: string | null
  sourceId: string | null
}

export type TaskResultReferenceGroup =
  | { kind: 'web'; reference: TaskResultReference }
  | { kind: 'document'; title: string; references: TaskResultReference[] }

/** Build the compact task-detail source projection.
 *
 * Web entries deduplicate by canonical URL and remain one flat row. Internal
 * chunks group only when several hits belong to the same document; a single
 * hit is still represented by one document group with one row so rendering can
 * avoid a redundant title/header pair.
 */
export function taskResultReferenceGroups(
  rawReferences: readonly Record<string, unknown>[],
): TaskResultReferenceGroup[] {
  const web = new Map<string, TaskResultReference>()
  const documents = new Map<string, TaskResultReference[]>()
  const order: Array<{ kind: 'web'; key: string } | { kind: 'document'; key: string }> = []

  for (const raw of rawReferences) {
    const documentId = stringField(raw, 'document_id') || null
    const url = stringField(raw, 'url') || null
    const queryId = stringField(raw, 'query_id') || null
    const chunkIndex = numberField(raw, 'chunk_index')
    const pageNumber = numberField(raw, 'page_number')
    const excerpt = firstString(raw, ['excerpt', 'source_text', 'grounded_support']) || null
    if (documentId) {
      const key = `${documentId}:${chunkIndex ?? ''}`
      let group = documents.get(documentId)
      if (!group) {
        group = []
        documents.set(documentId, group)
        order.push({ kind: 'document', key: documentId })
      }
      if (!group.some((reference) => reference.key === key)) {
        group.push({
          citationId: stringField(raw, 'citation_id') || null,
          chunkIndex,
          documentId,
          domain: null,
          excerpt,
          key,
          label: stringField(raw, 'label') || null,
          pageNumber,
          providerSnippet: stringField(raw, 'provider_snippet') || null,
          queryId,
          sourceId: stringField(raw, 'source_id') || null,
          title: stringField(raw, 'title') || documentId,
          url,
        })
      }
      continue
    }
    if (!url && !queryId) continue
    const key = url ? canonicalUrl(url) : `query:${queryId}`
    const candidate: TaskResultReference = {
      citationId: stringField(raw, 'citation_id') || null,
      chunkIndex,
      documentId: null,
      domain: url ? urlDomain(url) : null,
      excerpt,
      key,
      label: stringField(raw, 'label') || null,
      pageNumber,
      providerSnippet: stringField(raw, 'provider_snippet') || null,
      queryId,
      sourceId: stringField(raw, 'source_id') || null,
      title: stringField(raw, 'title')
        || (url ? urlDomain(url) || url : queryId ?? ''),
      url,
    }
    const existing = web.get(key)
    if (existing) {
      web.set(key, mergeWebProvenance(existing, candidate))
      continue
    }
    web.set(key, candidate)
    order.push({ kind: 'web', key })
  }

  return order.flatMap<TaskResultReferenceGroup>((entry) => {
    if (entry.kind === 'web') {
      const reference = web.get(entry.key)
      return reference ? [{ kind: 'web' as const, reference }] : []
    }
    const references = documents.get(entry.key) ?? []
    return references.length > 0
      ? [{
        kind: 'document' as const,
        references,
        title: references[0]?.title ?? entry.key,
      }]
      : []
  })
}

function mergeWebProvenance(
  existing: TaskResultReference,
  candidate: TaskResultReference,
): TaskResultReference {
  return {
    ...existing,
    citationId: existing.citationId || candidate.citationId,
    providerSnippet: existing.providerSnippet || candidate.providerSnippet,
    queryId: existing.queryId || candidate.queryId,
    sourceId: existing.sourceId || candidate.sourceId,
  }
}

function canonicalUrl(value: string): string {
  try {
    const url = new URL(value)
    url.hash = ''
    url.hostname = url.hostname.toLowerCase()
    for (const key of [...url.searchParams.keys()]) {
      if (/^(?:utm_[a-z]+|ref|source|fbclid|gclid)$/.test(key)) {
        url.searchParams.delete(key)
      }
    }
    url.pathname = url.pathname.replace(/\/$/, '') || '/'
    return url.toString()
  } catch {
    return value.trim()
  }
}

function urlDomain(value: string): string | null {
  try {
    return new URL(value).hostname.replace(/^www\./, '')
  } catch {
    return null
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

function numberField(
  record: Record<string, unknown>,
  key: string,
): number | null {
  const value = record[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}
