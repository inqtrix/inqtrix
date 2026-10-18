import type { KnowledgeDocumentInfo } from '@/features/researchRuns/types'

/** Chunks a document holds without generated retrieval context because the
 * provider's content filter blocked their batch. The server records the count
 * in the document metadata with each revision; anything else reads as 0. */
export function contextOmittedChunks(metadata: Readonly<Record<string, unknown>>): number {
  const omitted = metadata._chunk_context_omitted
  if (typeof omitted !== 'object' || omitted === null) return 0
  const chunks = (omitted as { chunks?: unknown }).chunks
  return typeof chunks === 'number' && Number.isInteger(chunks) && chunks > 0 ? chunks : 0
}

/** Document id -> chunks without context, for the documents that have any. */
export function contextOmittedByDocumentId(
  documents: readonly KnowledgeDocumentInfo[],
): ReadonlyMap<string, number> {
  const counts = new Map<string, number>()
  for (const document of documents) {
    const chunks = contextOmittedChunks(document.metadata)
    if (chunks > 0) counts.set(document.id, chunks)
  }
  return counts
}
