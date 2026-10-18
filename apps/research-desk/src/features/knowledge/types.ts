import type {
  KnowledgeDocumentText,
  KnowledgeSearchResponse,
} from '@/features/researchRuns/types'

/** One selectable knowledge collection (a ready vector index). */
export type KnowledgeCollectionOption = {
  /** Local vector-index id (selection key, stable across reindexes). */
  id: string
  /** Backend collection id sent in `knowledge_filters.collection_ids`;
   * in demo mode this is the local index id (resolved locally). */
  collectionId: string
  title: string
}

/** What the document viewer should open and highlight. */
export type DocumentViewerTarget = {
  documentId: string
  /** Known title (reference/search hit) shown while the text loads. */
  title?: string
  collectionLabel?: string
  /** Highlight candidates in priority order — the first one that
   * matches the document text wins (quote first, search terms after). */
  highlightTargets: string[]
  /** The exact original-source excerpt for citations and Find-mode targets. */
  excerpt?: string | null
  /** 0-based chunk index, for the "Abschnitt N" label on the Beleg view. */
  chunkIndex?: number | null
  /** Whether the cited quote was grounding-verified (drives the "belegt"
   * badge). Undefined when grounding was off / no quote for this citation. */
  verified?: boolean
  /** Best-effort 1-based source page of the cited chunk (PDF sources); the
   * "Quelle" tab opens the PDF at this page with a soft highlight. Null when
   * unmapped / non-PDF. */
  pageNumber?: number | null
}

/**
 * Data access used by the knowledge workspace, injected by the shell:
 * the live implementation calls the Inqtrix API, the demo
 * implementation answers from the seeded corpus. Components never
 * branch on demo mode themselves.
 */
export type KnowledgeDataSource = {
  search: (query: string, collectionIds: string[], topK: number) => Promise<KnowledgeSearchResponse>
  loadDocumentText: (documentId: string) => Promise<KnowledgeDocumentText>
  /** Metadata-only authorization probe. The Original affordance stays disabled
   * until this confirms access for the current principal. */
  canLoadFileContent:
    | ((fileId: string) => Promise<boolean>)
    | null
  /** Original binary for the viewer's Original tab; null = no original
   * available in this deployment (demo, or files feature off). */
  loadFileContent:
    | ((fileId: string) => Promise<{ blob: Blob; contentType: string }>)
    | null
}
