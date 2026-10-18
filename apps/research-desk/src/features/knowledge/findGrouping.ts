import type { KnowledgeSearchHit } from '@/features/researchRuns/types'

/** All hits of one document, folded for the Finden result list. */
export type DocumentHitGroup = {
  documentId: string
  collectionId: string
  title: string
  /** Total hits in this document (may exceed the rendered snippets). */
  hitCount: number
  /** Best (= first, the API returns score-sorted hits) score. */
  topScore: number
  /** At most `maxSnippets` hits, in API (score) order. */
  snippets: KnowledgeSearchHit[]
}

export const FIND_MAX_SNIPPETS_PER_DOCUMENT = 3

/**
 * Group score-sorted search hits by document. Group order follows each
 * document's best hit (first appearance in the score-sorted input), so
 * the strongest document stays on top without re-sorting.
 */
export function groupHitsByDocument(
  hits: readonly KnowledgeSearchHit[],
  maxSnippets: number = FIND_MAX_SNIPPETS_PER_DOCUMENT,
): DocumentHitGroup[] {
  const groups = new Map<string, DocumentHitGroup>()
  for (const hit of hits) {
    const existing = groups.get(hit.document_id)
    if (!existing) {
      groups.set(hit.document_id, {
        collectionId: hit.collection_id,
        documentId: hit.document_id,
        hitCount: 1,
        snippets: [hit],
        title: hit.document_title,
        topScore: hit.score,
      })
      continue
    }
    existing.hitCount += 1
    if (existing.snippets.length < maxSnippets) {
      existing.snippets.push(hit)
    }
  }
  return [...groups.values()]
}
