import type { ReportReference, ResearchSource } from '@/features/researchRuns/types'
import { asString } from '@/lib/coerce'

export function normalizeReportReferences(
  value: unknown,
  markdown: string,
  topSources: readonly ResearchSource[],
): ReportReference[] {
  if (Array.isArray(value)) {
    return value.flatMap((item) => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return []
      const record = item as Record<string, unknown>
      const url = asString(record.url)?.trim()
      if (!url) return []
      return [{
        // Absent on older payloads = cited; only an explicit false marks a
        // source the writer examined without citing it.
        ...(typeof record.cited === 'boolean' ? { cited: record.cited } : {}),
        citation_id: optionalString(record, 'citation_id'),
        citation_ids: optionalStringArray(record, 'citation_ids'),
        chunk_id: optionalString(record, 'chunk_id'),
        chunk_index: optionalNumber(record, 'chunk_index'),
        collection_id: optionalString(record, 'collection_id'),
        document_id: optionalString(record, 'document_id'),
        excerpt: optionalString(record, 'excerpt'),
        generation_id: optionalString(record, 'generation_id'),
        grounded_support: optionalString(record, 'grounded_support'),
        label: asString(record.label)?.trim() || 'Quelle',
        page_number: optionalNumber(record, 'page_number'),
        provenance_status: optionalString(record, 'provenance_status'),
        provider_snippet: optionalString(record, 'provider_snippet'),
        query_id: optionalString(record, 'query_id'),
        query_ids: optionalStringArray(record, 'query_ids'),
        reference_id: optionalString(record, 'reference_id'),
        revision_id: optionalString(record, 'revision_id'),
        source_id: optionalString(record, 'source_id'),
        source_run_id: optionalString(record, 'source_run_id'),
        source_run_ids: optionalStringArray(record, 'source_run_ids'),
        source_span: sourceSpan(record.source_span),
        source_text: optionalString(record, 'source_text'),
        tier: asString(record.tier)?.trim() || tierForUrl(url, topSources),
        tier_reason: optionalString(record, 'tier_reason'),
        role_reason: optionalString(record, 'role_reason'),
        title: optionalString(record, 'title'),
        url,
      }]
    })
  }

  return reportReferencesFromMarkdown(markdown, topSources)
}

/** The report's reference list split the way the Markdown appendix splits it:
 *  cited sources first, examined-but-uncited sources as their own list. */
export function partitionReportReferences(references: readonly ReportReference[]): {
  cited: ReportReference[]
  examined: ReportReference[]
} {
  const cited: ReportReference[] = []
  const examined: ReportReference[] = []
  for (const reference of references) {
    (reference.cited === false ? examined : cited).push(reference)
  }
  return { cited, examined }
}

/** The tier label with the judged or proven role behind it ("primary · official_primary"). */
export function sourceRoleLabel(reference: Pick<ReportReference, 'tier' | 'tier_reason'>): string {
  const reason = reference.tier_reason ?? ''
  const [kind, role] = reason.split(':', 2)
  if ((kind === 'model' || kind === 'structural') && role) return `${reference.tier} · ${role}`
  return reference.tier
}

export function reportReferencesFromMarkdown(
  markdown: string,
  topSources: readonly ResearchSource[],
): ReportReference[] {
  const references: ReportReference[] = []
  let inReferenceSection = false
  const seenUrls = new Set<string>()

  for (const line of markdown.split(/\r?\n/)) {
    if (/^##\s+(Referenzen|References)\s*$/i.test(line.trim())) {
      inReferenceSection = true
      continue
    }
    if (!inReferenceSection) continue
    if (/^---+\s*$/.test(line.trim()) || /^##\s+/.test(line.trim())) break

    const match = line.match(/^\s*[-*]\s+\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/)
    if (!match) continue
    const label = match[1].trim() || 'Quelle'
    const url = match[2].trim()
    if (!url || seenUrls.has(url)) continue
    seenUrls.add(url)
    references.push({ label, tier: tierForUrl(url, topSources), url })
  }

  return references
}

function tierForUrl(url: string, topSources: readonly ResearchSource[]) {
  return topSources.find((source) => source.url === url)?.tier ?? 'unknown'
}

function optionalString(
  record: Record<string, unknown>,
  key: string,
): string | null | undefined {
  if (!(key in record)) return undefined
  return asString(record[key])?.trim() || null
}

function optionalNumber(
  record: Record<string, unknown>,
  key: string,
): number | null | undefined {
  if (!(key in record)) return undefined
  const value = record[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function optionalStringArray(
  record: Record<string, unknown>,
  key: string,
): string[] | undefined {
  if (!(key in record)) return undefined
  const value = record[key]
  return Array.isArray(value)
    ? value.flatMap((item) => {
      const normalized = asString(item)?.trim()
      return normalized ? [normalized] : []
    })
    : []
}

function sourceSpan(value: unknown): ReportReference['source_span'] | undefined {
  if (value === undefined) return undefined
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const row = value as Record<string, unknown>
  if (
    typeof row.start !== 'number'
    || !Number.isFinite(row.start)
    || typeof row.end !== 'number'
    || !Number.isFinite(row.end)
    || typeof row.offset_unit !== 'string'
  ) {
    return null
  }
  return {
    document_content_hash: optionalString(row, 'document_content_hash'),
    end: row.end,
    offset_unit: row.offset_unit,
    start: row.start,
  }
}
