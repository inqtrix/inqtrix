import type { KnowledgeQuoteRecord, KnowledgeReferenceRecord } from '@/features/project/types'

/**
 * View model for one citation row, shared by the answer's source list and the
 * panel's Belege list. The PRIMARY text is the supporting passage (the verbatim
 * grounding quote, else a trimmed chunk excerpt) — what the citation actually
 * supports — so two chunks of the same document are distinguishable. Document
 * identity (title, section, verified) is SECONDARY. No LLM call: every field is
 * already present on the reference + grounding quote.
 */
export type CitationView = {
  label: string
  reference: KnowledgeReferenceRecord
  documentId: string | null
  title: string
  /** "Abschnitt N" (1-based) when the chunk index is known. */
  sectionLabel: string | null
  /** Best-effort 1-based source page (PDF sources); null when unmapped. */
  pageNumber: number | null
  /** The supporting passage (quote → excerpt), clamped; null when neither
   * exists (older payloads) so the row falls back to the title. */
  snippet: string | null
  verified: boolean
  canOpen: boolean
}

const SNIPPET_MAX = 220

/** Collapse internal whitespace runs to single spaces and trim the ends. Shared
 * by the clamped row snippet and the unclamped copy-to-clipboard excerpt. */
export function collapseWhitespace(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
}

/** Collapse whitespace and clamp to a 2-line-ish snippet with an ellipsis. */
function clampSnippet(text: string): string {
  const collapsed = collapseWhitespace(text)
  return collapsed.length > SNIPPET_MAX
    ? `${collapsed.slice(0, SNIPPET_MAX).trimEnd()}…`
    : collapsed
}

/** Stable identity for one cited passage: (document, chunk). Used to mark the
 * active row in the panel without a label round-trip. */
export function citationKey(documentId: string | null, chunkIndex: number | null | undefined): string {
  return `${documentId ?? ''}:${typeof chunkIndex === 'number' ? chunkIndex : ''}`
}

export function citationView(
  reference: KnowledgeReferenceRecord,
  quote: KnowledgeQuoteRecord | undefined,
  sectionLabelTemplate: string,
): CitationView {
  const quoteText = quote?.text?.trim()
  const excerpt = reference.excerpt?.trim()
  const snippetSource = quoteText || excerpt || ''
  const sectionLabel =
    typeof reference.chunkIndex === 'number'
      ? sectionLabelTemplate.replace('{n}', String(reference.chunkIndex + 1))
      : null
  return {
    canOpen: Boolean(reference.documentId),
    documentId: reference.documentId ?? null,
    label: reference.label,
    pageNumber: reference.pageNumber ?? null,
    reference,
    sectionLabel,
    snippet: snippetSource ? clampSnippet(snippetSource) : null,
    title: reference.title ?? reference.url,
    verified: quote?.verified ?? false,
  }
}

/** Build citation view models for an answer's references, joining each to its
 * grounding quote by label. */
export function citationViews(
  references: KnowledgeReferenceRecord[],
  quotes: KnowledgeQuoteRecord[],
  sectionLabelTemplate: string,
): CitationView[] {
  const quoteByLabel = new Map(quotes.map((quote) => [quote.label, quote]))
  return references.map((reference) =>
    citationView(reference, quoteByLabel.get(reference.label), sectionLabelTemplate),
  )
}

export type CitationDocumentGroup = {
  documentId: string | null
  title: string
  citations: CitationView[]
}

/**
 * Collapse citations that point at the SAME document into one group (the doc
 * title shown once, its cited passages nested) — the enterprise pattern that
 * fixes a flat list of repeated identical filenames. Group order follows first
 * appearance, so the K-order is preserved across groups.
 */
export function groupCitationsByDocument(views: CitationView[]): CitationDocumentGroup[] {
  const groups: CitationDocumentGroup[] = []
  const byKey = new Map<string, CitationDocumentGroup>()
  for (const view of views) {
    const key = view.documentId ?? `title:${view.title}`
    let group = byKey.get(key)
    if (!group) {
      group = { citations: [], documentId: view.documentId, title: view.title }
      byKey.set(key, group)
      groups.push(group)
    }
    group.citations.push(view)
  }
  return groups
}

/**
 * The document group matching the currently open source, or null. Used to scope
 * the panel's Belege list to the active document (document-centric reader): when
 * a source is open, only its document + passages are shown. A null `documentId`
 * (no open source) yields null so the caller falls back to the full list.
 */
export function activeCitationGroup(
  groups: CitationDocumentGroup[],
  documentId: string | null | undefined,
): CitationDocumentGroup | null {
  if (!documentId) return null
  return groups.find((group) => group.documentId === documentId) ?? null
}

/** First openable citation of a document group (clicking a document header opens
 * the document at its first cited passage), or null when none can be opened. */
export function firstOpenableCitation(group: CitationDocumentGroup): CitationView | null {
  return group.citations.find((view) => view.canOpen) ?? null
}
