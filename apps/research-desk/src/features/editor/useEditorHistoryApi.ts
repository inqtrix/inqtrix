/**
 * Editor server sync (project-persistence tier).
 *
 * The editor counterpart of useChatHistoryApi, sharing the same shape:
 * hydrate document + folder METADATA on mount, load a document's heavy body
 * AND its comments lazily on open, and a debounced serialized autosave that
 * diffs three collections (documents, folders, comments) via the shared
 * syncCollection helper. Persistence only — it never calls a model; the
 * editor's AI/suggestion paths are untouched.
 *
 * It does NOT own the import button: the explicit opt-in push is the
 * project-level useProjectServerImport (which pushes chat AND editor in one
 * flow). This hook only hydrates + autosaves once the project is opted in
 * (syncActive), seeding its synced fingerprint to WHAT THE SERVER HOLDS so a
 * local-newer entity is pushed up rather than stranded.
 *
 * A Markdown document can have a second writer (another tab, an accepted
 * agent patch). A version saved elsewhere is merged into the local text with
 * the base both build on (markdownMerge, as Git merges); changes that
 * overlap leave the local version in a conflict copy saved at once. Nothing
 * pauses and nothing is deleted on that way.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { Dispatch } from 'react'

import {
  deleteEditorComment,
  deleteEditorDocument,
  deleteEditorFolder,
  getEditorDocument,
  hasHttpStatus,
  listEditorComments,
  listEditorDocuments,
  listEditorFolders,
  patchEditorDocumentMetadata,
  saveEditorComments,
  saveEditorDocument,
  saveEditorFolder,
} from '@/api/inqtrixClient'
import {
  commentRecordFromServer,
  documentRecordFromServer,
  folderRecordFromServer,
  isCollaborationDocument,
  serverCommentPayload,
  serverDocumentPayload,
  serverFolderPayload,
} from '@/features/editor/editorSync'
import type {
  EditorCommentOutboxEntry,
  EditorCommentThreadRecord,
  EditorDocumentRecord,
  EditorFolderRecord,
} from '@/features/project/types'
import {
  deleteTolerant404,
  syncCollection,
} from '@/features/project/syncCollection'
import { createProjectEntityId } from '@/features/project/entityId'
import {
  useProjectSyncLifecycle,
  useSyncScope,
  type SyncLifecycleToken,
} from '@/features/project/useProjectSyncLifecycle'
import {
  keepsLocalEditorText,
  type EditorDocumentRecoveryCapture,
  type EditorObservedServerMetadata,
  type ResearchDeskAction,
} from '@/features/researchDesk/state'
import type { InqtrixRequestError } from '@/api/inqtrixClient'
import { messageFromError } from '@/api/requestErrorText'
import { editorConflictCopyTitle } from './editorCopy'

export const AUTOSAVE_DEBOUNCE_MS = 1_500
const PAGE_LIMIT = 200

type CommentFingerprint = { documentId: string; updatedAt: string }

export type EditorCommentReconciliationPlan = {
  pendingDeletedCommentIds: Set<string>
  preserveCommentIds: Set<string>
  serverComments: EditorCommentThreadRecord[]
}

export type EditorOpenHydrationPlan = {
  loadComments: boolean
  loadDocumentDetail: boolean
}

export type EditorDocumentBodyHydration = {
  documentId: string | null
  error: string | null
  phase: 'empty' | 'error' | 'pending' | 'ready'
}

export type EditorServerDocumentProvenance = 'exact_detail' | 'metadata'

export type EditorDocumentAutosavePlan =
  | { kind: 'legacy_body' }
  | {
      kind: 'collaboration_metadata'
      payload: {
        expected_metadata_revision: number
        folder_id: string | null
        title: string
      }
    }
  | { kind: 'none' }

/** What merging a server version into a document's record left: `merged`
 * when the record holds the server's version with any own changes that did
 * not overlap it (or the record had moved past that version already),
 * `conflict` when the own version overlapped and now lives in the conflict
 * copy, saved at once while project sync runs. */
export type EditorDocumentMergeOutcome =
  | { kind: 'merged' }
  | { conflictCopyId: string; kind: 'conflict' }

/** Result of pushing one document: `saved` carries the server's adopted
 * revisions and the body the server now holds (`null` when only the
 * metadata of a collaboration document was saved). `merged` and `conflict`
 * mean a concurrent writer won the revision guard and its version was merged
 * into the record (see {@link EditorDocumentMergeOutcome}); the record then
 * sits on the server's revision and a save of it goes through. `converted`
 * means the document was converted to collaboration elsewhere: own changes
 * went to a conflict copy, and the record took the converted document.
 * `skipped` means the autosave plan had nothing to push for this record. */
export type EditorDocumentPushOutcome =
  | {
      contentMarkdown: string | null
      kind: 'saved'
      metadataRevision: number | undefined
      revision: number
    }
  | EditorDocumentMergeOutcome
  | { conflictCopyId: string | null; kind: 'converted' }
  | { kind: 'skipped' }

/** A copy that keeps this tab's version of a document after it overlapped a
 * version saved elsewhere. Known to this session only: after a reload the
 * copy's title says what it is. */
export type EditorConflictCopy = {
  copyDocumentId: string
  originalDocumentId: string
  /** `concurrent_change`: another writer saved an overlapping change.
   * `content_mode_changed`: the document was converted to collaboration
   * elsewhere while this tab had changes the server never got. */
  reason: 'concurrent_change' | 'content_mode_changed'
  /** Whether the server confirmed the copy's first save. Until then the
   * copy exists only in this tab. */
  saved: boolean
}

/** Keep the legacy body contract and collaboration metadata contract disjoint. */
export function planEditorDocumentAutosave(
  document: EditorDocumentRecord,
  expectedMetadataRevision = document.metadataRevision ?? 1,
  serverDocument?: EditorDocumentRecord,
): EditorDocumentAutosavePlan {
  const collaborationDocument = serverDocument
    && isCollaborationDocument(serverDocument)
    ? serverDocument
    : isCollaborationDocument(document)
      ? document
      : null
  if (!collaborationDocument) return { kind: 'legacy_body' }
  if (collaborationDocument.access?.mode !== 'owner') return { kind: 'none' }
  return {
    kind: 'collaboration_metadata',
    payload: {
      expected_metadata_revision: expectedMetadataRevision,
      folder_id: document.folderId,
      title: document.title,
    },
  }
}

export function shouldLoadLegacyEditorBody(
  document: EditorDocumentRecord | undefined,
): boolean {
  return document === undefined || !isCollaborationDocument(document)
}

/** Document detail and private comments have independent lazy-load lifecycles. */
export function planEditorOpenHydration({
  collaborationDocument,
  hasCommentSnapshot,
  hasExactDocumentDetail,
  hasLocalDocumentBody,
}: {
  collaborationDocument: boolean
  hasCommentSnapshot: boolean
  hasExactDocumentDetail: boolean
  hasLocalDocumentBody: boolean
}): EditorOpenHydrationPlan {
  return {
    loadComments: !hasCommentSnapshot,
    loadDocumentDetail: collaborationDocument
      ? !hasExactDocumentDetail
      : !hasLocalDocumentBody,
  }
}

/** Resolve only the document body's authoritative readiness. Comment loading
 * and background metadata refreshes deliberately do not hide an otherwise
 * usable editor body. Collaboration documents additionally require exact
 * detail provenance for their current projection lifecycle. */
export function resolveEditorDocumentBodyHydration({
  documentId,
  error,
  hasExactDocumentDetail,
  hasLoadedDocumentBody,
  hasServerDocument,
  requiresExactDocumentDetail,
}: {
  documentId: string | null
  error: string | null
  hasExactDocumentDetail: boolean
  hasLoadedDocumentBody: boolean
  hasServerDocument: boolean
  requiresExactDocumentDetail: boolean
}): EditorDocumentBodyHydration {
  if (!documentId) return { documentId: null, error: null, phase: 'empty' }
  if (!hasServerDocument) {
    return { documentId, error: null, phase: 'ready' }
  }
  const ready = requiresExactDocumentDetail
    ? hasExactDocumentDetail
    : hasLoadedDocumentBody
  if (ready) return { documentId, error: null, phase: 'ready' }
  if (error) return { documentId, error, phase: 'error' }
  return { documentId, error: null, phase: 'pending' }
}

/** A first project hydration may trust bodies restored from project markdown.
 * Later metadata refreshes may trust only bodies this lifecycle actually
 * loaded; by then metadata-only server rows are also present in project state. */
export function editorLocallyAuthoritativeDocumentIds({
  alreadyHydrated,
  loadedDocumentIds,
  projectDocumentIds,
}: {
  alreadyHydrated: boolean
  loadedDocumentIds: Iterable<string>
  projectDocumentIds: Iterable<string>
}): Set<string> {
  return new Set(alreadyHydrated ? loadedDocumentIds : projectDocumentIds)
}

/** Exact bodies are valid only for the collaboration projection lifecycle
 * that produced them. Local presence and metadata-only list records never
 * establish this provenance. */
export function editorDocumentDetailProvenanceKey(
  document: EditorDocumentRecord | undefined,
): string | null {
  if (!document) return null
  if (!isCollaborationDocument(document)) return `${document.id}:markdown`
  const generation = document.collaboration?.generation
  const projectionSequence = document.collaboration?.projectionSequence
  return `${document.id}:collaboration:g${generation ?? 'unknown'}:p${projectionSequence ?? 'unknown'}`
}

export function editorServerDocumentObservation(
  document: EditorDocumentRecord,
  provenance: EditorServerDocumentProvenance,
): {
  exactDetailProvenanceKey: string | null
  metadataRevision: number
  syncedFingerprint: string | null
} {
  return {
    exactDetailProvenanceKey: provenance === 'exact_detail'
      ? editorDocumentDetailProvenanceKey(document)
      : null,
    metadataRevision: document.metadataRevision ?? 1,
    syncedFingerprint: document.access?.mode === 'shared' ? null : document.updatedAt,
  }
}

/** Shared-in documents are live server views, never local autosave inputs. */
export function editorDocumentsForAutosave(
  documents: Record<string, EditorDocumentRecord>,
  serverDocuments?: ReadonlyMap<string, EditorDocumentRecord>,
  retiredDocumentIds: ReadonlySet<string> = new Set(),
): Record<string, EditorDocumentRecord> {
  return Object.fromEntries(Object.entries(documents).filter(([documentId, document]) => {
    const authoritativeDocument = serverDocuments?.get(documentId) ?? document
    return (
      authoritativeDocument.access?.mode !== 'shared'
      && document.recovery === undefined
      && !retiredDocumentIds.has(documentId)
    )
  }))
}

/** Private comments may persist on shared collaboration documents when the
 * caller has suggestion access; legacy shared documents remain read-only. */
export function canPersistEditorCommentsForDocument(
  document: EditorDocumentRecord | undefined,
): boolean {
  if (!document || document.access?.mode !== 'shared') return true
  return isCollaborationDocument(document) && document.access.permission !== 'view'
}

export function editorCommentsForAutosave(
  comments: Record<string, EditorCommentThreadRecord>,
  documents: Record<string, EditorDocumentRecord>,
  serverDocuments?: ReadonlyMap<string, EditorDocumentRecord>,
  retiredDocumentIds: ReadonlySet<string> = new Set(),
): Record<string, EditorCommentThreadRecord> {
  return Object.fromEntries(Object.entries(comments).filter(([, comment]) => {
    const authoritativeDocument = serverDocuments?.get(comment.documentId)
      ?? documents[comment.documentId]
    return (
      !retiredDocumentIds.has(comment.documentId)
      && authoritativeDocument?.recovery === undefined
      && canPersistEditorCommentsForDocument(authoritativeDocument)
    )
  }))
}

/** Reconcile one exact server list while preserving only explicit local
 * outbox work. Pending deletes stay absent when a racing GET still returns
 * their older server record. */
export function planEditorCommentReconciliation(
  documentId: string,
  localComments: Readonly<Record<string, EditorCommentThreadRecord>>,
  serverComments: readonly EditorCommentThreadRecord[],
  commentOutbox: Readonly<Record<string, EditorCommentOutboxEntry>>,
): EditorCommentReconciliationPlan {
  const preserveCommentIds = new Set(
    Object.entries(commentOutbox)
      .filter(([commentId, entry]) => (
        entry.documentId === documentId
        && entry.operation === 'upsert'
        && localComments[commentId] !== undefined
      ))
      .map(([commentId]) => commentId),
  )
  const pendingDeletedCommentIds = new Set(
    Object.entries(commentOutbox)
      .filter(([, entry]) => entry.documentId === documentId && entry.operation === 'delete')
      .map(([commentId]) => commentId),
  )
  return {
    pendingDeletedCommentIds,
    preserveCommentIds,
    serverComments: serverComments.filter(
      (comment) => !pendingDeletedCommentIds.has(comment.id),
    ),
  }
}

type UseEditorHistoryApiOptions = {
  apiKey: string | undefined
  consumeCollaborationRecovery?: (
    documentId: string,
  ) => { contentMarkdown: string } | null
  dispatch: Dispatch<ResearchDeskAction>
  editorCommentOutbox?: Record<string, EditorCommentOutboxEntry>
  editorComments: Record<string, EditorCommentThreadRecord>
  editorDocuments: Record<string, EditorDocumentRecord>
  editorFolders: Record<string, EditorFolderRecord>
  /** Language of the titles of conflict copies. */
  locale: 'de' | 'en'
  /** In-session load counter (bumped on every wholesale project replace). Part
   * of the lifecycle identity so a switch to another synced project re-hydrates
   * from its own server state instead of inheriting this one's synced map. */
  projectEpoch: number
  /** Bumped by the user-scoped invalidation channel (sharing) and local
   * share actions. A bump re-runs the authoritative document/folder
   * hydration WITHOUT a reset, so a document shared with this user appears
   * without a page reload (the run list follows the same pattern). */
  refreshToken?: number
  selectedDocumentId: string | null
  /** ``serverSyncEnabled`` AND the durable capability AND not demo. */
  syncActive: boolean
  workspaceId: string
}

export type EditorHistoryApiHandle = {
  /** The conflict copies of this session, oldest first, with the document
   * whose server version each one's text overlapped and whether its first
   * save is confirmed. A copy deleted since is left out. */
  conflictCopies: readonly EditorConflictCopy[]
  error: string | null
  /** Authoritative readiness for the currently requested body. Metadata and
   * comments are intentionally excluded: they may refresh behind visible
   * content without replacing the editor with a structural loader. */
  selectedDocumentBodyHydration: EditorDocumentBodyHydration
  /** Warm a document body from pointer/focus intent. The selected-document
   * loader awaits this exact same in-flight request. */
  prefetchDocumentBody: (
    documentId: string,
    options?: { retry?: boolean },
  ) => Promise<void>
  /** Push one document's current body to the server NOW (serialized against
   * the autosave loop). `saved` returns the fresh server revisions and the
   * body the server now holds. When a concurrent writer won the revision
   * guard, its version is merged into the record and the record is saved
   * once more, read after the render that applied the merge: the caller
   * gets `saved` and goes on. Only when that second save loses too does it
   * get `merged` or `conflict`. `converted` means the document was converted
   * to collaboration elsewhere; `skipped` means nothing was pushed (sync
   * inactive, not hydrated yet, no such document, or nothing to push for
   * it); a failed push throws. Two flows build on the saved base: the
   * share/collaboration-enable flow, so converting a document never depends
   * on the global project dirty flag, and accepting an agent patch on the
   * server, which applies the patch on the returned revision so that text
   * typed before is part of what the patch is applied to. */
  flushDocumentForShare: (
    documentId: string,
  ) => Promise<EditorDocumentPushOutcome>
  /** Merge a version of a Markdown document the server holds now (after an
   * accepted agent patch) into its record, by the same rule as a save that
   * lost against another writer, serialized against the autosave. A
   * conflict copy is saved at once while project sync runs. */
  mergeServerDocument: (
    documentId: string,
    server: EditorDocumentRecord,
  ) => Promise<EditorDocumentMergeOutcome>
  registerOpenedServerDocument: (
    document: EditorDocumentRecord,
    provenance: EditorServerDocumentProvenance,
  ) => void
}

export function useEditorHistoryApi({
  apiKey,
  consumeCollaborationRecovery,
  dispatch,
  editorCommentOutbox,
  editorComments,
  editorDocuments,
  editorFolders,
  locale,
  projectEpoch,
  refreshToken,
  selectedDocumentId,
  syncActive,
  workspaceId,
}: UseEditorHistoryApiOptions): EditorHistoryApiHandle {
  const [error, setError] = useState<string | null>(null)
  const [hydrated, setHydrated] = useState(false)

  const documentsRef = useRef(editorDocuments)
  documentsRef.current = editorDocuments
  const localeRef = useRef(locale)
  localeRef.current = locale
  const foldersRef = useRef(editorFolders)
  foldersRef.current = editorFolders
  const commentsRef = useRef(editorComments)
  commentsRef.current = editorComments
  const selectedDocumentIdRef = useRef(selectedDocumentId)
  selectedDocumentIdRef.current = selectedDocumentId
  const commentOutboxRef = useRef(editorCommentOutbox ?? {})
  commentOutboxRef.current = editorCommentOutbox ?? {}
  const optionsRef = useRef({ apiKey, workspaceId })
  optionsRef.current = { apiKey, workspaceId }
  const consumeCollaborationRecoveryRef = useRef(consumeCollaborationRecovery)
  consumeCollaborationRecoveryRef.current = consumeCollaborationRecovery

  const syncedDocsRef = useRef(new Map<string, string>())
  const syncedFoldersRef = useRef(new Map<string, string>())
  const syncedCommentsRef = useRef(new Map<string, CommentFingerprint>())
  const metadataRevisionsRef = useRef(new Map<string, number>())
  const serverDocumentsRef = useRef(new Map<string, EditorDocumentRecord>())
  const retiredServerDocumentIdsRef = useRef(new Set<string>())
  const loadedDocsRef = useRef(new Set<string>())
  const documentBodyRequestsRef = useRef(
    new Map<string, Promise<EditorDocumentRecord | null>>(),
  )
  const documentBodyErrorsRef = useRef(new Map<string, string>())
  const documentBodyRequestGenerationRef = useRef(0)
  const exactDetailProvenanceRef = useRef(new Map<string, string>())
  const loadedCommentsRef = useRef(new Set<string>())
  const hydratedRef = useRef(false)
  const flushingRef = useRef(false)
  const flushPendingRef = useRef(false)
  const syncActiveRef = useRef(syncActive)
  syncActiveRef.current = syncActive
  const syncIdentity = `${workspaceId}:${projectEpoch}`
  const bindSyncPass = useSyncScope(syncActive, syncIdentity)
  const [serverObservationEpoch, setServerObservationEpoch] = useState(0)
  const [documentBodyObservationEpoch, setDocumentBodyObservationEpoch] = useState(0)

  // Conflict copies of this session: which copy keeps the version of which
  // document, and whether the server confirmed its first save.
  const conflictCopiesRef = useRef(new Map<string, EditorConflictCopy>())
  const [conflictCopyList, setConflictCopyList] = useState<readonly EditorConflictCopy[]>([])
  const publishConflictCopies = useCallback(() => {
    setConflictCopyList([...conflictCopiesRef.current.values()])
  }, [])

  // A render barrier. The reducer decides a merge with the record as it is
  // when the action runs; the caller learns the outcome from the state that
  // render produced, never from a second computation that could see another
  // text. Each wait bumps a generation of its own; the effect of the render
  // that carries it resolves the wait. The dispatch before the bump is part
  // of that render, so the state read afterwards holds the action's result.
  const [renderedGeneration, setRenderedGeneration] = useState(0)
  const requestedGenerationRef = useRef(0)
  const renderWaitsRef = useRef<Array<{ generation: number; resolve: () => void }>>([])
  useEffect(() => {
    const due = renderWaitsRef.current.filter((wait) => wait.generation <= renderedGeneration)
    if (due.length === 0) return
    renderWaitsRef.current = renderWaitsRef.current.filter(
      (wait) => wait.generation > renderedGeneration,
    )
    for (const wait of due) wait.resolve()
  }, [renderedGeneration])
  const afterNextRender = useCallback(() => new Promise<void>((resolve) => {
    requestedGenerationRef.current += 1
    const generation = requestedGenerationRef.current
    renderWaitsRef.current.push({ generation, resolve })
    setRenderedGeneration(generation)
  }), [])

  const registerOpenedServerDocument = useCallback((
    document: EditorDocumentRecord,
    provenance: EditorServerDocumentProvenance,
  ) => {
    const observation = editorServerDocumentObservation(document, provenance)
    retiredServerDocumentIdsRef.current.delete(document.id)
    serverDocumentsRef.current.set(document.id, document)
    metadataRevisionsRef.current.set(document.id, observation.metadataRevision)
    if (observation.exactDetailProvenanceKey) {
      exactDetailProvenanceRef.current.set(
        document.id,
        observation.exactDetailProvenanceKey,
      )
      loadedDocsRef.current.add(document.id)
      documentBodyErrorsRef.current.delete(document.id)
      setDocumentBodyObservationEpoch((current) => current + 1)
    }
    if (observation.syncedFingerprint) {
      syncedDocsRef.current.set(document.id, observation.syncedFingerprint)
    }
    setServerObservationEpoch((current) => current + 1)
  }, [])

  const loadDocumentBody = useCallback((
    documentId: string,
  ): Promise<EditorDocumentRecord | null> => {
    const serverDocument = serverDocumentsRef.current.get(documentId)
    if (!serverDocument) {
      return Promise.resolve(documentsRef.current[documentId] ?? null)
    }
    const hasExactDocumentDetail = (
      exactDetailProvenanceRef.current.get(documentId)
      === editorDocumentDetailProvenanceKey(serverDocument)
    )
    const bodyReady = isCollaborationDocument(serverDocument)
      ? hasExactDocumentDetail
      : loadedDocsRef.current.has(documentId)
    if (bodyReady) return Promise.resolve(serverDocument)

    const inFlight = documentBodyRequestsRef.current.get(documentId)
    if (inFlight) return inFlight

    const requestGeneration = documentBodyRequestGenerationRef.current
    documentBodyErrorsRef.current.delete(documentId)
    setDocumentBodyObservationEpoch((current) => current + 1)
    const request = (async (): Promise<EditorDocumentRecord | null> => {
      try {
        const detail = await getEditorDocument(documentId, optionsRef.current)
        if (
          requestGeneration !== documentBodyRequestGenerationRef.current
          || retiredServerDocumentIdsRef.current.has(documentId)
          || !serverDocumentsRef.current.has(documentId)
        ) return null
        const detailRecord = documentRecordFromServer(detail)
        serverDocumentsRef.current.set(documentId, detailRecord)
        const provenanceKey = editorDocumentDetailProvenanceKey(detailRecord)
        if (provenanceKey) {
          exactDetailProvenanceRef.current.set(documentId, provenanceKey)
        }
        loadedDocsRef.current.add(documentId)
        documentBodyErrorsRef.current.delete(documentId)
        metadataRevisionsRef.current.set(
          documentId,
          detailRecord.metadataRevision ?? 1,
        )
        if (isCollaborationDocument(detailRecord)) {
          dispatch({ document: detailRecord, type: 'setServerEditorDocumentDetail' })
        } else if (shouldLoadLegacyEditorBody(detailRecord)) {
          // Text and revision of the detail together: someone may have saved
          // since the list this record's revision came from.
          dispatch({
            contentMarkdown: detailRecord.contentMarkdown,
            documentId,
            revision: detailRecord.revision,
            type: 'setServerEditorDocumentBody',
          })
        }
        setDocumentBodyObservationEpoch((current) => current + 1)
        return detailRecord
      } catch (caught) {
        if (requestGeneration !== documentBodyRequestGenerationRef.current) {
          return null
        }
        const message = messageFromError(caught)
        documentBodyErrorsRef.current.set(documentId, message)
        setDocumentBodyObservationEpoch((current) => current + 1)
        throw caught
      }
    })()
    documentBodyRequestsRef.current.set(documentId, request)
    const releaseRequest = () => {
      if (documentBodyRequestsRef.current.get(documentId) === request) {
        documentBodyRequestsRef.current.delete(documentId)
      }
    }
    void request.then(releaseRequest, releaseRequest)
    return request
  }, [dispatch])

  const prefetchDocumentBody = useCallback(async (
    documentId: string,
    options?: { retry?: boolean },
  ) => {
    const hadError = documentBodyErrorsRef.current.has(documentId)
    if (hadError && options?.retry !== true) return
    try {
      const document = await loadDocumentBody(documentId)
      if (
        document
        && selectedDocumentIdRef.current === documentId
        && hadError
      ) {
        setError(null)
        // Re-run the selected-document hydration effect so a successful retry
        // also resumes independent comments loading.
        setServerObservationEpoch((current) => current + 1)
      }
    } catch {
      // Readiness carries the scoped failure. Intent prefetch must not create
      // an unhandled rejection or replace an unrelated visible document.
    }
  }, [loadDocumentBody])

  // -- a version another writer saved ------------------------------------- #

  // pushDocument, defined below, saves conflict copies too; the helpers in
  // this section reach it through this ref.
  const pushDocumentRef = useRef<
    (document: EditorDocumentRecord) => Promise<EditorDocumentPushOutcome>
  >(async () => ({ kind: 'skipped' }))

  /** Records an exact server detail as this tab's latest observation of the
   * document, whose text is now loaded. */
  const noteServerDetail = useCallback((detail: EditorDocumentRecord) => {
    retiredServerDocumentIdsRef.current.delete(detail.id)
    serverDocumentsRef.current.set(detail.id, detail)
    metadataRevisionsRef.current.set(detail.id, detail.metadataRevision ?? 1)
    loadedDocsRef.current.add(detail.id)
    documentBodyErrorsRef.current.delete(detail.id)
    setDocumentBodyObservationEpoch((current) => current + 1)
  }, [])

  const markConflictCopySaved = useCallback((documentId: string) => {
    const copy = conflictCopiesRef.current.get(documentId)
    if (!copy || copy.saved) return
    conflictCopiesRef.current.set(documentId, { ...copy, saved: true })
    publishConflictCopies()
  }, [publishConflictCopies])

  /** Registers a conflict copy the reducer created and saves it at once,
   * without the autosave delay: until then it exists in this tab only. Runs
   * under the flush lock. A failed save stays visible as the sync error,
   * and the autosave saves the copy later like any unsaved document. Without
   * project sync the copy is a document of the project like any other. */
  const keepConflictCopy = useCallback(async (
    copy: EditorDocumentRecord,
    originalDocumentId: string,
    reason: EditorConflictCopy['reason'],
  ) => {
    conflictCopiesRef.current.set(copy.id, {
      copyDocumentId: copy.id,
      originalDocumentId,
      reason,
      saved: false,
    })
    publishConflictCopies()
    if (!syncActiveRef.current || !hydratedRef.current) return
    try {
      const outcome = await pushDocumentRef.current(copy)
      if (outcome.kind === 'saved') syncedDocsRef.current.set(copy.id, copy.updatedAt)
    } catch (caught) {
      setError(messageFromError(caught))
    }
  }, [publishConflictCopies])

  /** Merges a version of a Markdown document another writer saved into its
   * record (`mergeServerEditorDocument`, which merges with the record's own
   * base) and saves a conflict copy at once. Runs under the flush lock, so
   * no save of the record runs meanwhile. The outcome is read from the state
   * the merge produced: a conflict copy exists under the id given to it. */
  const mergeServerStateLocked = useCallback(async (
    documentId: string,
    server: EditorDocumentRecord,
  ): Promise<EditorDocumentMergeOutcome> => {
    const local = documentsRef.current[documentId]
    const seen = serverDocumentsRef.current.get(documentId)
    const at = new Date()
    const conflictCopyId = createProjectEntityId('editor-doc')
    dispatch({
      at: at.toISOString(),
      conflictCopy: {
        id: conflictCopyId,
        title: editorConflictCopyTitle(local?.title ?? server.title, at, localeRef.current),
      },
      documentId,
      observed: seen ? observedServerMetadata(seen) : null,
      server: {
        contentMarkdown: server.contentMarkdown,
        folderId: server.folderId,
        revision: server.revision,
        title: server.title,
      },
      type: 'mergeServerEditorDocument',
    })
    noteServerDetail(server)
    await afterNextRender()
    const record = documentsRef.current[documentId]
    if (
      record
      && syncedDocsRef.current.has(documentId)
      && record.revision === server.revision
      && record.contentMarkdown === server.contentMarkdown
      && record.title === server.title
      && record.folderId === server.folderId
    ) {
      // The record holds exactly the server's version: nothing to save.
      syncedDocsRef.current.set(documentId, record.updatedAt)
    }
    const copy = documentsRef.current[conflictCopyId]
    if (!copy) return { kind: 'merged' }
    await keepConflictCopy(copy, documentId, 'concurrent_change')
    return { conflictCopyId, kind: 'conflict' }
  }, [afterNextRender, dispatch, keepConflictCopy, noteServerDetail])

  /** The document was converted to collaboration elsewhere. Own changes go
   * to a conflict copy first (`copyEditorDocumentBeforeConversion`); then
   * the record takes the converted document the way an opened one does, and
   * the editor switches to collaboration. Runs under the flush lock. */
  const adoptConvertedDocumentLocked = useCallback(async (
    documentId: string,
    detail: EditorDocumentRecord,
  ): Promise<EditorDocumentPushOutcome> => {
    const local = documentsRef.current[documentId]
    const at = new Date()
    const conflictCopyId = createProjectEntityId('editor-doc')
    dispatch({
      at: at.toISOString(),
      conflictCopy: {
        id: conflictCopyId,
        title: editorConflictCopyTitle(local?.title ?? detail.title, at, localeRef.current),
      },
      documentId,
      projectionMarkdown: detail.contentMarkdown,
      type: 'copyEditorDocumentBeforeConversion',
    })
    dispatch({ document: detail, type: 'setServerEditorDocumentDetail' })
    registerOpenedServerDocument(detail, 'exact_detail')
    await afterNextRender()
    const copy = documentsRef.current[conflictCopyId]
    if (!copy) return { conflictCopyId: null, kind: 'converted' }
    await keepConflictCopy(copy, documentId, 'content_mode_changed')
    return { conflictCopyId, kind: 'converted' }
  }, [afterNextRender, dispatch, keepConflictCopy, registerOpenedServerDocument])

  // -- pushing one entity ----------------------------------------------- #

  /** Saves one document. The save carries the revision the record builds
   * on, and the server's revision guard takes it only on top of that
   * revision. When another writer saved since, nothing of either version is
   * dropped: its version is merged into the record with the record's base
   * (`merged`); changes that overlap leave the own version in a conflict
   * copy saved at once and the record on the server's version
   * (`conflict`); a conversion to collaboration elsewhere keeps own changes
   * in such a copy (`converted`). Nothing pauses, and no path here deletes. */
  const pushDocument = useCallback(async (
    document: EditorDocumentRecord,
  ): Promise<EditorDocumentPushOutcome> => {
    if (
      document.recovery !== undefined
      || retiredServerDocumentIdsRef.current.has(document.id)
    ) return { kind: 'skipped' }
    const plan = planEditorDocumentAutosave(
      document,
      metadataRevisionsRef.current.get(document.id),
      serverDocumentsRef.current.get(document.id),
    )
    if (plan.kind === 'none') return { kind: 'skipped' }
    if (plan.kind === 'collaboration_metadata') {
      const saved = await patchEditorDocumentMetadata(
        document.id,
        plan.payload,
        optionsRef.current,
      )
      const metadataRevision = requireEditorMetadataRevision(saved.metadata_revision)
      metadataRevisionsRef.current.set(document.id, metadataRevision)
      dispatch({
        documentId: document.id,
        metadataRevision,
        type: 'adoptEditorDocumentMetadataRevision',
      })
      const savedRecord = documentRecordFromServer(saved)
      retiredServerDocumentIdsRef.current.delete(document.id)
      serverDocumentsRef.current.set(document.id, savedRecord)
      return {
        contentMarkdown: null,
        kind: 'saved',
        metadataRevision,
        revision: saved.revision,
      }
    }

    let record = document
    if (
      syncedDocsRef.current.has(document.id) &&
      !loadedDocsRef.current.has(document.id)
    ) {
      // A tree-level metadata edit (rename / drag-to-folder / folder delete)
      // bumped this server-held document's updatedAt while its body was never
      // loaded (it is still ""). The PUT is a full-record upsert, so sending
      // body="" would ERASE the server's real body. Fetch the current server
      // body first and push the merged record (new metadata + kept body),
      // on the revision that body belongs to: the list this record's
      // revision came from may be older, and its revision would lose the
      // revision guard although nobody writes concurrently.
      const detail = documentRecordFromServer(
        await getEditorDocument(document.id, optionsRef.current),
      )
      dispatch({
        contentMarkdown: detail.contentMarkdown,
        documentId: document.id,
        revision: detail.revision,
        type: 'setServerEditorDocumentBody',
      })
      record = { ...document, contentMarkdown: detail.contentMarkdown, revision: detail.revision }
    }
    try {
      const saved = await saveEditorDocument(
        record.id,
        serverDocumentPayload(record),
        optionsRef.current,
      )
      // Adopt the server's new revision as our base (revision now tracks the
      // SERVER, not local edits), with the text this save sent. Never
      // touching content/updatedAt/dirty: the flush fingerprint is
      // updatedAt, so this cannot wedge a re-flush, and a live keystroke
      // during the save keeps its newer body and stays out of the base.
      // Without this, the next save would re-send the same base+1 and 409
      // against the just-advanced server on every flush.
      dispatch({
        contentMarkdown: record.contentMarkdown,
        documentId: record.id,
        revision: saved.revision,
        type: 'adoptEditorDocumentRevision',
      })
      // The upsert response also carries the metadata revision. Adopting it
      // makes a document created THIS session shareable immediately: without
      // it, `metadataRevision` stayed undefined until a reload re-hydrated
      // the record, and the share/collaboration entry points treat that as
      // "not on the server yet" (disabled Share button).
      const savedMetadataRevision = saved.metadata_revision
      const adoptedMetadataRevision = (
        savedMetadataRevision !== undefined
        && Number.isSafeInteger(savedMetadataRevision)
        && savedMetadataRevision >= 1
      )
        ? savedMetadataRevision
        : undefined
      if (adoptedMetadataRevision !== undefined) {
        metadataRevisionsRef.current.set(record.id, adoptedMetadataRevision)
        dispatch({
          documentId: record.id,
          metadataRevision: adoptedMetadataRevision,
          type: 'adoptEditorDocumentMetadataRevision',
        })
      }
      retiredServerDocumentIdsRef.current.delete(record.id)
      serverDocumentsRef.current.set(record.id, documentRecordFromServer(saved))
      // We hold this document's body now, so re-opening it must not trigger
      // a redundant body fetch.
      loadedDocsRef.current.add(record.id)
      documentBodyErrorsRef.current.delete(record.id)
      setDocumentBodyObservationEpoch((current) => current + 1)
      markConflictCopySaved(record.id)
      return {
        contentMarkdown: record.contentMarkdown,
        kind: 'saved',
        metadataRevision: adoptedMetadataRevision,
        revision: saved.revision,
      }
    } catch (cause) {
      if (!hasHttpStatus(cause, 409)) throw cause
      // The server's revision guard refused this save: another writer saved
      // past the revision this record builds on, or converted the document
      // to collaboration. Its version is fetched and kept together with the
      // local one.
      const detail = documentRecordFromServer(
        await getEditorDocument(record.id, optionsRef.current),
      )
      if (convertedElsewhere(cause, detail)) {
        const converted = await adoptConvertedDocumentLocked(record.id, detail)
        console.warn(
          `[inqtrix] Editor document ${record.id}: the save was refused (409) `
          + 'because the document was converted to collaboration elsewhere; '
          + (converted.kind === 'converted' && converted.conflictCopyId
            ? `the local changes are kept in the copy ${converted.conflictCopyId}.`
            : 'there were no local changes to keep.'),
        )
        return converted
      }
      const merged = await mergeServerStateLocked(record.id, detail)
      console.warn(
        `[inqtrix] Editor document ${record.id}: the save lost the revision `
        + `guard (409) against revision ${detail.revision}; `
        + (merged.kind === 'conflict'
          ? 'the changes overlap, so the document now holds that version and '
            + `the local version is kept in the copy ${merged.conflictCopyId}.`
          : 'that version was merged with the local text; own changes save on top of it.'),
      )
      return merged
    }
  }, [adoptConvertedDocumentLocked, dispatch, markConflictCopySaved, mergeServerStateLocked])
  pushDocumentRef.current = pushDocument

  const pushFolder = useCallback(async (folder: EditorFolderRecord) => {
    await saveEditorFolder(folder.id, serverFolderPayload(folder), optionsRef.current)
  }, [])

  const pushComment = useCallback(async (comment: EditorCommentThreadRecord) => {
    await saveEditorComments(
      comment.documentId,
      [serverCommentPayload(comment)],
      optionsRef.current,
    )
  }, [])

  // -- autosave flush (debounced, serialized) --------------------------- #

  const flush = useCallback(async () => {
    if (!syncActiveRef.current || !hydratedRef.current) return
    if (flushingRef.current) {
      flushPendingRef.current = true
      return
    }
    flushingRef.current = true
    const inScope = bindSyncPass()
    try {
      // Documents first: a document delete cascades its comments server-side.
      await syncCollection<EditorDocumentRecord, string>({
        inScope,
        current: editorDocumentsForAutosave(
          documentsRef.current,
          serverDocumentsRef.current,
          retiredServerDocumentIdsRef.current,
        ),
        synced: syncedDocsRef.current,
        fingerprintOf: (document) => document.updatedAt,
        changed: (previous, current) => previous !== current,
        pushOne: async (document) => {
          await pushDocument(document)
        },
        deleteOne: async (id) => {
          await deleteTolerant404(
            () => deleteEditorDocument(id, optionsRef.current),
          )
          loadedDocsRef.current.delete(id)
          documentBodyErrorsRef.current.delete(id)
          documentBodyRequestsRef.current.delete(id)
          exactDetailProvenanceRef.current.delete(id)
          loadedCommentsRef.current.delete(id)
          serverDocumentsRef.current.delete(id)
          // The server cascade-deletes this document's comments. Mirror that
          // in the client bookkeeping so the comment sync below does not try a
          // redundant delete that 404s (parent gone) and wedges the autosave
          // in a retry loop.
          for (const [commentId, fingerprint] of [
            ...syncedCommentsRef.current.entries(),
          ]) {
            if (fingerprint.documentId === id) {
              syncedCommentsRef.current.delete(commentId)
            }
          }
        },
      })
      await syncCollection<EditorFolderRecord, string>({
        inScope,
        current: foldersRef.current,
        synced: syncedFoldersRef.current,
        fingerprintOf: (folder) => folder.updatedAt,
        changed: (previous, current) => previous !== current,
        pushOne: pushFolder,
        deleteOne: (id) => deleteTolerant404(
          () => deleteEditorFolder(id, optionsRef.current),
        ),
      })
      const acknowledgements: Array<{
        commentId: string
        operation: 'delete' | 'upsert'
        updatedAt?: string
      }> = []
      for (const [commentId, pending] of Object.entries(commentOutboxRef.current)) {
        if (!inScope()) return
        const authoritativeDocument = serverDocumentsRef.current.get(pending.documentId)
          ?? documentsRef.current[pending.documentId]
        if (
          retiredServerDocumentIdsRef.current.has(pending.documentId)
          || authoritativeDocument?.recovery !== undefined
        ) continue
        if (!canPersistEditorCommentsForDocument(authoritativeDocument)) continue
        if (pending.operation === 'upsert') {
          const comment = commentsRef.current[commentId]
          if (!comment || comment.updatedAt !== pending.updatedAt) continue
          await pushComment(comment)
          if (!inScope()) return
          syncedCommentsRef.current.set(commentId, {
            documentId: comment.documentId,
            updatedAt: comment.updatedAt,
          })
        } else {
          await deleteTolerant404(
            () => deleteEditorComment(
              pending.documentId,
              commentId,
              optionsRef.current,
            ),
          )
          if (!inScope()) return
          syncedCommentsRef.current.delete(commentId)
        }
        acknowledgements.push({
          commentId,
          operation: pending.operation,
          ...(pending.updatedAt ? { updatedAt: pending.updatedAt } : {}),
        })
      }
      if (acknowledgements.length > 0) {
        const nextOutbox = { ...commentOutboxRef.current }
        for (const acknowledgement of acknowledgements) {
          const current = nextOutbox[acknowledgement.commentId]
          if (
            current?.operation === acknowledgement.operation
            && current.updatedAt === acknowledgement.updatedAt
          ) {
            delete nextOutbox[acknowledgement.commentId]
          }
        }
        commentOutboxRef.current = nextOutbox
        dispatch({ acknowledgements, type: 'acknowledgeEditorCommentOutbox' })
      }
      setError(null)
    } catch (caught) {
      setError(messageFromError(caught))
    } finally {
      flushingRef.current = false
      if (flushPendingRef.current) {
        flushPendingRef.current = false
        void flush()
      }
    }
  }, [bindSyncPass, pushComment, pushDocument, pushFolder])

  /** Runs `work` while holding the flush lock: waits for an in-flight
   * autosave pass or immediate save, holds the lock while `work` runs, and
   * starts an autosave pass that was asked for meanwhile. */
  const withFlushLock = useCallback(async <T>(work: () => Promise<T>): Promise<T> => {
    while (flushingRef.current) {
      await new Promise<void>((resolve) => {
        window.setTimeout(resolve, 50)
      })
    }
    flushingRef.current = true
    try {
      return await work()
    } finally {
      flushingRef.current = false
      if (flushPendingRef.current) {
        flushPendingRef.current = false
        void flush()
      }
    }
  }, [flush])

  const flushDocumentForShare = useCallback(async (
    documentId: string,
  ): Promise<EditorDocumentPushOutcome> => {
    if (!syncActiveRef.current || !hydratedRef.current) return { kind: 'skipped' }
    // Serialized against the debounced autosave loop, so the share flow can
    // never race a concurrent PUT of the same record.
    return withFlushLock(async () => {
      // The record is read only here, inside the lock. A record read before
      // the wait is stale by the time it is sent: in exactly that window the
      // running autosave adopts the next revision, and the flush would send
      // the previous one. The server refuses that with 409, and a caller
      // that sent the same stale record again could never succeed.
      const document = documentsRef.current[documentId]
      if (!document) return { kind: 'skipped' }
      const outcome = await pushDocument(document)
      if (outcome.kind === 'saved') {
        // Mark this fingerprint as synced so the next autosave cycle does not
        // re-push the identical body.
        syncedDocsRef.current.set(document.id, document.updatedAt)
        return outcome
      }
      if (outcome.kind !== 'merged' && outcome.kind !== 'conflict') return outcome
      // Another writer won the revision guard, and its version is merged
      // into the record now, on its revision (after a conflict: its version,
      // with the own one in the saved copy). pushDocument returned after the
      // render that applied the merge, so this read sees the merged record.
      // One more save gives the caller a saved revision to go on from.
      const merged = documentsRef.current[documentId]
      if (!merged) return outcome
      const second = await pushDocument(merged)
      if (second.kind === 'saved') syncedDocsRef.current.set(merged.id, merged.updatedAt)
      return second
    })
  }, [pushDocument, withFlushLock])

  const mergeServerDocument = useCallback((
    documentId: string,
    server: EditorDocumentRecord,
  ): Promise<EditorDocumentMergeOutcome> => withFlushLock(
    () => mergeServerStateLocked(documentId, server),
  ), [mergeServerStateLocked, withFlushLock])

  /** After a refresh of the lists: merges the newer server version of each
   * document whose text this tab holds, so a change saved elsewhere shows
   * without a reload instead of being overwritten by the next save here.
   * Serialized against the autosave; a save that lost the revision guard
   * meanwhile has merged the same version already, which the reducer then
   * ignores. */
  const mergeNewerServerVersions = useCallback((
    documentIds: readonly string[],
    token: SyncLifecycleToken,
  ) => withFlushLock(async () => {
    for (const documentId of documentIds) {
      if (token.cancelled) return
      const local = documentsRef.current[documentId]
      const listed = serverDocumentsRef.current.get(documentId)
      if (
        !local
        || !listed
        || isCollaborationDocument(local)
        || listed.revision <= local.revision
      ) continue
      const detail = documentRecordFromServer(
        await getEditorDocument(documentId, optionsRef.current),
      )
      if (token.cancelled) return
      if (isCollaborationDocument(detail)) continue
      await mergeServerStateLocked(documentId, detail)
    }
  }), [mergeServerStateLocked, withFlushLock])
  const mergeNewerServerVersionsRef = useRef(mergeNewerServerVersions)
  mergeNewerServerVersionsRef.current = mergeNewerServerVersions

  /** A conversion to collaboration elsewhere that a refresh of the lists
   * shows before a save of this tab meets it. Own text of each document
   * (`copyEditorDocumentBeforeConversion`) goes to a conflict copy, then
   * `reconcile` takes the lists, which switches the document to
   * collaboration, and the copies are saved at once. Under the flush lock,
   * so no save of such a document runs in between. `null` when the
   * lifecycle ended meanwhile; nothing is copied then. */
  const copyBeforeConversions = useCallback(<T,>(
    documentIds: readonly string[],
    token: SyncLifecycleToken,
    reconcile: () => T,
  ): Promise<T | null> => withFlushLock(async () => {
    const projections: EditorDocumentRecord[] = []
    for (const documentId of documentIds) {
      const local = documentsRef.current[documentId]
      // A save refused for the conversion may have taken it meanwhile.
      if (!local || isCollaborationDocument(local)) continue
      const detail = documentRecordFromServer(
        await getEditorDocument(documentId, optionsRef.current),
      )
      if (token.cancelled) return null
      if (isCollaborationDocument(detail)) projections.push(detail)
    }
    const copies: Array<{ conflictCopyId: string; documentId: string }> = []
    for (const detail of projections) {
      const local = documentsRef.current[detail.id]
      if (!local || isCollaborationDocument(local)) continue
      const at = new Date()
      const conflictCopyId = createProjectEntityId('editor-doc')
      dispatch({
        at: at.toISOString(),
        conflictCopy: {
          id: conflictCopyId,
          title: editorConflictCopyTitle(local.title, at, localeRef.current),
        },
        documentId: detail.id,
        projectionMarkdown: detail.contentMarkdown,
        type: 'copyEditorDocumentBeforeConversion',
      })
      copies.push({ conflictCopyId, documentId: detail.id })
    }
    const result = reconcile()
    await afterNextRender()
    for (const { conflictCopyId, documentId } of copies) {
      const copy = documentsRef.current[conflictCopyId]
      if (copy) await keepConflictCopy(copy, documentId, 'content_mode_changed')
    }
    return result
  }), [afterNextRender, dispatch, keepConflictCopy, withFlushLock])
  const copyBeforeConversionsRef = useRef(copyBeforeConversions)
  copyBeforeConversionsRef.current = copyBeforeConversions

  // An editor text that overlapped a version which reached the record
  // before the editor showed it waits on the record
  // (`pendingConflictMarkdown`, see the reducer's updateEditorDocumentMarkdown).
  // It becomes a conflict copy at once: the copy action runs right here,
  // so no later action can meet the waiting text, and the copy is saved
  // under the flush lock like every conflict copy.
  const pendingCopiesRef = useRef(new Map<string, string>())
  useEffect(() => {
    for (const document of Object.values(editorDocuments)) {
      const pending = document.pendingConflictMarkdown
      if (pending === undefined) {
        pendingCopiesRef.current.delete(document.id)
        continue
      }
      if (pendingCopiesRef.current.get(document.id) === pending) continue
      pendingCopiesRef.current.set(document.id, pending)
      const at = new Date()
      const conflictCopyId = createProjectEntityId('editor-doc')
      dispatch({
        at: at.toISOString(),
        conflictCopy: {
          id: conflictCopyId,
          title: editorConflictCopyTitle(document.title, at, localeRef.current),
        },
        documentId: document.id,
        type: 'copyPendingEditorConflictVersion',
      })
      void withFlushLock(async () => {
        await afterNextRender()
        const copy = documentsRef.current[conflictCopyId]
        if (copy) await keepConflictCopy(copy, document.id, 'concurrent_change')
      })
    }
  }, [afterNextRender, dispatch, editorDocuments, keepConflictCopy, withFlushLock])

  // -- reset + hydrate lifecycle (re-armed on project identity) ---------- #

  const reset = useCallback(() => {
    documentBodyRequestGenerationRef.current += 1
    hydratedRef.current = false
    setHydrated(false)
    syncedDocsRef.current.clear()
    syncedFoldersRef.current.clear()
    syncedCommentsRef.current.clear()
    metadataRevisionsRef.current.clear()
    serverDocumentsRef.current.clear()
    retiredServerDocumentIdsRef.current.clear()
    loadedDocsRef.current.clear()
    documentBodyRequestsRef.current.clear()
    documentBodyErrorsRef.current.clear()
    exactDetailProvenanceRef.current.clear()
    loadedCommentsRef.current.clear()
    if (conflictCopiesRef.current.size > 0) {
      conflictCopiesRef.current.clear()
      publishConflictCopies()
    }
    setDocumentBodyObservationEpoch((current) => current + 1)
  }, [publishConflictCopies])

  const hydrate = useCallback((token: SyncLifecycleToken) => {
    documentBodyRequestGenerationRef.current += 1
    documentBodyRequestsRef.current.clear()
    void (async () => {
      try {
        const options = optionsRef.current
        const serverFolders = await listEditorFolders(options)
        const folderRecords = serverFolders.map(folderRecordFromServer)
        const documentRecords: EditorDocumentRecord[] = []
        let cursor: string | undefined
        do {
          const page = await listEditorDocuments({
            ...options,
            cursor,
            limit: PAGE_LIMIT,
            scope: 'all',
          })
          for (const serverDocument of page.data) {
            documentRecords.push(documentRecordFromServer(serverDocument))
          }
          cursor = page.next_cursor ?? undefined
        } while (cursor)
        if (token.cancelled) return
        // Takes the lists: dispatches the reconcile, seeds the save
        // fingerprints and the server observation, and names the documents
        // whose text this tab holds and whose server version is newer.
        const reconcile = (): string[] => {
          // On the first hydration, documents already in the local project carry
          // an authoritative body loaded from project markdown. On a background
          // refresh, server-only metadata rows are already present locally too;
          // only the explicit loaded set may preserve their body readiness.
          const locallyPresentIds = editorLocallyAuthoritativeDocumentIds({
            alreadyHydrated: hydratedRef.current,
            loadedDocumentIds: loadedDocsRef.current,
            projectDocumentIds: Object.keys(documentsRef.current),
          })
          if (folderRecords.length > 0) {
            dispatch({ folders: folderRecords, type: 'upsertServerEditorFolders' })
          }
          const authoritativeDocumentIds = new Set(
            documentRecords.map((document) => document.id),
          )
          const retiredDocumentIds = new Set<string>()
          for (const documentId of authoritativeDocumentIds) {
            retiredServerDocumentIdsRef.current.delete(documentId)
          }
          for (const [documentId, document] of Object.entries(documentsRef.current)) {
            if (
              (document.serverSynced === true || document.access?.mode === 'shared')
              && !authoritativeDocumentIds.has(documentId)
            ) {
              retiredDocumentIds.add(documentId)
            }
          }
          for (const documentId of serverDocumentsRef.current.keys()) {
            if (!authoritativeDocumentIds.has(documentId)) {
              retiredDocumentIds.add(documentId)
            }
          }
          const recoveryCaptures: EditorDocumentRecoveryCapture[] = []
          const capturedAt = new Date().toISOString()
          for (const documentId of retiredDocumentIds) {
            const localDocument = documentsRef.current[documentId]
            if (!localDocument) continue
            const collaborationRecovery =
              consumeCollaborationRecoveryRef.current?.(documentId) ?? null
            const hasChangedLocalDocument = (
              syncedDocsRef.current.has(documentId)
              && syncedDocsRef.current.get(documentId) !== localDocument.updatedAt
            )
            const hasPendingPrivateComment = Object.values(
              commentOutboxRef.current,
            ).some((entry) => (
              entry.documentId === documentId
              && entry.operation === 'upsert'
            ))
            if (
              !collaborationRecovery
              && !hasChangedLocalDocument
              && !hasPendingPrivateComment
            ) continue
            recoveryCaptures.push({
              capturedAt,
              contentMarkdown: collaborationRecovery?.contentMarkdown
                ?? localDocument.contentMarkdown,
              documentId,
            })
          }
          // Establish the no-resurrection boundary before dispatching. A
          // debounced flush can run between this continuation and React's next
          // render; it must already exclude every remotely retired id.
          for (const documentId of retiredDocumentIds) {
            retiredServerDocumentIdsRef.current.add(documentId)
            serverDocumentsRef.current.delete(documentId)
            syncedDocsRef.current.delete(documentId)
            metadataRevisionsRef.current.delete(documentId)
            loadedDocsRef.current.delete(documentId)
            documentBodyErrorsRef.current.delete(documentId)
            documentBodyRequestsRef.current.delete(documentId)
            exactDetailProvenanceRef.current.delete(documentId)
            loadedCommentsRef.current.delete(documentId)
            for (const [commentId, fingerprint] of syncedCommentsRef.current) {
              if (fingerprint.documentId === documentId) {
                syncedCommentsRef.current.delete(commentId)
              }
            }
          }
          // A list row carries no text: for the documents whose text this tab
          // holds, the reconcile takes only metadata, and the newer text is
          // merged below. A local title or folder still equal to what this tab
          // last saw on the server takes the server's; a different one is an
          // open local rename or move.
          const observedMetadata: Record<string, EditorObservedServerMetadata> = {}
          for (const documentId of locallyPresentIds) {
            const seen = serverDocumentsRef.current.get(documentId)
            if (seen) observedMetadata[documentId] = observedServerMetadata(seen)
          }
          dispatch({
            documents: documentRecords,
            localTextDocumentIds: [...locallyPresentIds],
            observedMetadata,
            recoveryCaptures,
            retiredDocumentIds: [...retiredDocumentIds],
            type: 'reconcileServerEditorDocuments',
          })
          // Seed each fingerprint to WHAT THE SERVER HOLDS (its updated_at);
          // a local-newer entity then differs and the first autosave pushes it.
          // A document whose text this tab holds keeps its own updatedAt in
          // the reconcile; on a refresh its fingerprint is still that of its
          // last save here. Replaced by the server's, it would differ although
          // nothing changed here, and the next pass would write the server's
          // text back as a new revision.
          const newerServerDocumentIds: string[] = []
          for (const record of documentRecords) {
            const keepsText = keepsLocalEditorText(record, locallyPresentIds)
            if (
              keepsText
              && record.revision > (documentsRef.current[record.id]?.revision ?? Infinity)
            ) {
              newerServerDocumentIds.push(record.id)
            }
            serverDocumentsRef.current.set(record.id, record)
            if (record.access?.mode === 'shared') continue
            if (!keepsText || !syncedDocsRef.current.has(record.id)) {
              syncedDocsRef.current.set(record.id, record.updatedAt)
            }
            metadataRevisionsRef.current.set(record.id, record.metadataRevision ?? 1)
            if (locallyPresentIds.has(record.id)) {
              // Its body is authoritative locally: never overwrite it on open
              // (a local-newer body must survive + be pushed up), and it may be
              // pushed directly without first fetching the server body.
              loadedDocsRef.current.add(record.id)
            }
          }
          for (const record of folderRecords) {
            syncedFoldersRef.current.set(record.id, record.updatedAt)
          }
          hydratedRef.current = true
          setHydrated(true)
          setServerObservationEpoch((current) => current + 1)
          setDocumentBodyObservationEpoch((current) => current + 1)
          setError(null)
          return newerServerDocumentIds
        }
        // A conversion elsewhere that the lists show before a save meets it:
        // own text of such a document goes to a conflict copy first, as on
        // a save refused for the conversion; the reconcile then switches the
        // document to collaboration.
        const textIds = editorLocallyAuthoritativeDocumentIds({
          alreadyHydrated: hydratedRef.current,
          loadedDocumentIds: loadedDocsRef.current,
          projectDocumentIds: Object.keys(documentsRef.current),
        })
        const convertedIds = documentRecords
          .filter((record) => {
            const local = documentsRef.current[record.id]
            return textIds.has(record.id)
              && isCollaborationDocument(record)
              && local !== undefined
              && !isCollaborationDocument(local)
          })
          .map((record) => record.id)
        const newerServerDocumentIds = convertedIds.length > 0
          ? await copyBeforeConversionsRef.current(convertedIds, token, reconcile)
          : reconcile()
        if (newerServerDocumentIds && newerServerDocumentIds.length > 0) {
          await mergeNewerServerVersionsRef.current(newerServerDocumentIds, token)
        }
      } catch (caught) {
        if (!token.cancelled) setError(messageFromError(caught))
      }
    })()
  }, [dispatch])

  useProjectSyncLifecycle({
    active: syncActive,
    identity: syncIdentity,
    reset,
    hydrate,
  })

  // Authoritative re-hydration on user invalidations (sharing), focus,
  // coming back online and a reconnect: a document shared with this user
  // must appear WITHOUT a page reload. No reset — the hydrate upserts are
  // updatedAt-guarded and re-seed fingerprints to the server state, so
  // local-newer edits survive and re-push; a document whose text this tab
  // holds keeps it, and a newer server version of it is merged in.
  const lastRefreshTokenRef = useRef(refreshToken)
  useEffect(() => {
    if (
      refreshToken === undefined
      || refreshToken === lastRefreshTokenRef.current
    ) return
    lastRefreshTokenRef.current = refreshToken
    if (!syncActiveRef.current || !hydratedRef.current) return
    const token: SyncLifecycleToken = { cancelled: false }
    hydrate(token)
    return () => {
      token.cancelled = true
    }
  }, [hydrate, refreshToken])

  // -- load a document's body + comments on open ------------------------ #

  useEffect(() => {
    if (!syncActive || !hydrated || !selectedDocumentId) return
    // Only documents the server holds (hydrated) need a body/comment fetch;
    // a locally-authored document keeps its in-memory body authoritative.
    if (!serverDocumentsRef.current.has(selectedDocumentId)) return
    const documentId = selectedDocumentId
    const cachedDocument = serverDocumentsRef.current.get(documentId)
      ?? documentsRef.current[documentId]
    const hadLocalDocumentBody = loadedDocsRef.current.has(documentId)
    const hydrationPlan = planEditorOpenHydration({
      collaborationDocument: isCollaborationDocument(cachedDocument),
      hasCommentSnapshot: loadedCommentsRef.current.has(documentId),
      hasExactDocumentDetail: exactDetailProvenanceRef.current.get(documentId)
        === editorDocumentDetailProvenanceKey(cachedDocument),
      hasLocalDocumentBody: hadLocalDocumentBody,
    })
    if (!hydrationPlan.loadComments && !hydrationPlan.loadDocumentDetail) return
    if (hydrationPlan.loadComments) loadedCommentsRef.current.add(documentId)
    let cancelled = false
    let commentsApplied = !hydrationPlan.loadComments
    void (async () => {
      try {
        let document = serverDocumentsRef.current.get(documentId)
          ?? documentsRef.current[documentId]
        if (hydrationPlan.loadDocumentDetail) {
          const loadedDocument = await loadDocumentBody(documentId)
          if (cancelled) return
          if (!loadedDocument) {
            if (hydrationPlan.loadComments) {
              loadedCommentsRef.current.delete(documentId)
            }
            return
          }
          document = loadedDocument
        }
        if (hydrationPlan.loadComments) {
          const comments: EditorCommentThreadRecord[] = []
          let cursor: string | undefined
          do {
            const page = await listEditorComments(documentId, {
              ...optionsRef.current,
              cursor,
              limit: PAGE_LIMIT,
            })
            for (const serverComment of page.data) {
              comments.push(commentRecordFromServer(serverComment))
            }
            cursor = page.next_cursor ?? undefined
          } while (cursor)
          if (cancelled) return
          const reconciliation = planEditorCommentReconciliation(
            documentId,
            commentsRef.current,
            comments,
            commentOutboxRef.current,
          )
          dispatch({
            comments: reconciliation.serverComments,
            documentId,
            preserveCommentIds: [...reconciliation.preserveCommentIds],
            type: 'reconcileServerEditorComments',
          })
          if (canPersistEditorCommentsForDocument(document)) {
            const serverCommentIds = new Set(comments.map((comment) => comment.id))
            for (const [commentId, fingerprint] of [...syncedCommentsRef.current]) {
              if (fingerprint.documentId !== documentId) continue
              if (
                reconciliation.pendingDeletedCommentIds.has(commentId)
                && serverCommentIds.has(commentId)
              ) continue
              syncedCommentsRef.current.delete(commentId)
            }
            for (const comment of reconciliation.serverComments) {
              syncedCommentsRef.current.set(comment.id, {
                documentId: comment.documentId,
                updatedAt: comment.updatedAt,
              })
            }
          }
          commentsApplied = true
        }
        setError(null)
      } catch (caught) {
        if (!cancelled) {
          if (hydrationPlan.loadComments) loadedCommentsRef.current.delete(documentId)
          setError(messageFromError(caught))
        }
      }
    })()
    return () => {
      cancelled = true
      // Body requests intentionally survive a selection change and become a
      // warm cache hit. Comments remain selection-owned and are re-armed when
      // an interrupted document is opened again.
      if (!commentsApplied && hydrationPlan.loadComments) {
        loadedCommentsRef.current.delete(documentId)
      }
    }
  }, [
    dispatch,
    hydrated,
    loadDocumentBody,
    selectedDocumentId,
    serverObservationEpoch,
    syncActive,
  ])

  // -- debounced autosave trigger --------------------------------------- #

  useEffect(() => {
    if (!syncActive || !hydrated) return
    const timer = setTimeout(() => {
      void flush()
    }, AUTOSAVE_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [
    editorCommentOutbox,
    editorComments,
    editorDocuments,
    editorFolders,
    flush,
    hydrated,
    syncActive,
  ])

  const selectedDocumentBodyHydration = useMemo(() => {
    const documentId = selectedDocumentId
    const serverDocument = documentId
      ? serverDocumentsRef.current.get(documentId)
      : undefined
    const document = serverDocument
      ?? (documentId ? editorDocuments[documentId] : undefined)
    return resolveEditorDocumentBodyHydration({
      documentId,
      error: documentId
        ? documentBodyErrorsRef.current.get(documentId) ?? null
        : null,
      hasExactDocumentDetail: Boolean(
        documentId
        && serverDocument
        && exactDetailProvenanceRef.current.get(documentId)
          === editorDocumentDetailProvenanceKey(serverDocument),
      ),
      hasLoadedDocumentBody: Boolean(
        documentId && loadedDocsRef.current.has(documentId),
      ),
      hasServerDocument: serverDocument !== undefined,
      requiresExactDocumentDetail: Boolean(
        document && isCollaborationDocument(document),
      ),
    })
  }, [
    documentBodyObservationEpoch,
    editorDocuments,
    hydrated,
    selectedDocumentId,
    serverObservationEpoch,
    syncActive,
  ])

  const conflictCopies = useMemo(
    () => conflictCopyList.filter((copy) => editorDocuments[copy.copyDocumentId] !== undefined),
    [conflictCopyList, editorDocuments],
  )

  // Until the server confirms a conflict copy's first save, or while an
  // editor text waits to become one, the own version exists in this tab
  // only: leaving the page asks first. Without project sync nothing goes
  // to the server, and the project keeps the copy like any other document.
  const leavingAsks = syncActive && (
    conflictCopies.some((copy) => !copy.saved)
    || Object.values(editorDocuments).some((document) => document.pendingConflictMarkdown !== undefined)
  )
  useEffect(() => {
    if (!leavingAsks) return
    const ask = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      // Engines that still need the legacy form of the question.
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', ask)
    return () => window.removeEventListener('beforeunload', ask)
  }, [leavingAsks])

  return {
    conflictCopies,
    error,
    flushDocumentForShare,
    mergeServerDocument,
    prefetchDocumentBody,
    registerOpenedServerDocument,
    selectedDocumentBodyHydration,
  }
}

function requireEditorMetadataRevision(revision: number | undefined): number {
  if (!Number.isSafeInteger(revision) || revision === undefined || revision < 1) {
    throw new Error('The server returned an invalid editor metadata revision.')
  }
  return revision
}

function observedServerMetadata(document: EditorDocumentRecord): EditorObservedServerMetadata {
  return { folderId: document.folderId, title: document.title }
}

/** Whether a refused save met a document converted to collaboration
 * elsewhere: the server names that reason, and the detail fetched after the
 * refusal shows the mode in any case. */
function convertedElsewhere(cause: unknown, detail: EditorDocumentRecord): boolean {
  return (cause as InqtrixRequestError).detail?.reason === 'content_mode_changed'
    || isCollaborationDocument(detail)
}
