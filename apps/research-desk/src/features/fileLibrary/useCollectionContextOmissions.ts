import { useEffect, useState } from 'react'

import { contextOmittedByDocumentId } from './contextOmitted'
import type { KnowledgeSyncOptions } from './knowledgeSync'
import { fetchServerCollectionDocuments } from './ServerCollectionPanel'

const NONE: ReadonlyMap<string, number> = new Map()

/** Chunks without context per document of an index's server collection, read
 * from the document metadata the server writes with each revision. The
 * collection is listed when the view opens and again when a run ends (`idle`
 * turns true: every run ends by leaving "indexing"), never while a run is in
 * flight. */
export function useCollectionContextOmissions({
  collectionId,
  idle,
  knowledgeSync,
  scopeKey,
}: {
  collectionId: string | null
  idle: boolean
  knowledgeSync: KnowledgeSyncOptions | null
  scopeKey: string
}): ReadonlyMap<string, number> {
  // Keyed by document id, unique across collections: counts of a collection
  // the view left can never land on another collection's rows.
  const [counts, setCounts] = useState<ReadonlyMap<string, number>>(NONE)

  useEffect(() => {
    if (!collectionId || !knowledgeSync || !idle) return
    let current = true
    fetchServerCollectionDocuments(collectionId, knowledgeSync, scopeKey).then(
      (documents) => {
        if (current) setCounts(contextOmittedByDocumentId(documents))
      },
      (error: unknown) => {
        // The rows stay plain without the counts; the failure must not.
        if (current) console.warn('Context omission counts could not be loaded.', error)
      },
    )
    return () => {
      current = false
    }
  }, [collectionId, idle, knowledgeSync, scopeKey])

  return counts
}
