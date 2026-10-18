import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AlertTriangle, FileText, Plus, RotateCcw, Users, XCircle } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { listKnowledgeDocuments, type ServerDeletionOperation } from '@/api/inqtrixClient'
import type {
  FileAssetRecord,
  FileGroupRecord,
  FileLibrarySectionRecord,
} from '@/features/project/types'
import type {
  KnowledgeCollectionInfo,
  KnowledgeDocumentInfo,
} from '@/features/researchRuns/types'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { StructuralLoadBoundary, type StructuralLoadPhase } from '@/motion/StructuralLoadBoundary'
import { AddDocsPanel } from './AddDocsPanel'
import { contextOmittedChunks } from './contextOmitted'
import { ConfirmDelete, ContextOmittedLabel } from './controls'
import type { AutoResumePlan } from './indexingTypes'
import { IndexPauseNotice } from './IndexPauseNotice'
import { indexPhaseText } from './indexPhaseText'
import { allowsRawRebuild } from './pauseNotice'
import {
  ingestAssetsIntoKnowledgeCollection,
  type KnowledgeSyncOptions,
} from './knowledgeSync'
import { carriedErrorText } from '@/api/requestErrorText'

export type ServerCollectionJobState = {
  completedDocuments: number
  currentBatch?: number
  currentDocumentTitle?: string
  error?: string
  jobId: string
  /** While paused: what the job waits for and its automatic-resume plan. */
  pauseErrorType?: string | null
  autoResume?: AutoResumePlan | null
  phase?: string
  status:
    | 'cancelling'
    | 'error'
    | 'paused_dependency'
    | 'paused_validation'
    | 'queued'
    | 'running'
  totalBatches?: number
  totalDocuments: number
}

type ServerCollectionPanelProps = {
  assets: FileAssetRecord[]
  /** Project/principal/backend lifecycle fence. Cached documents from another
   * authenticated identity must never become a provisional warm snapshot. */
  cacheScopeKey: string
  collection: KnowledgeCollectionInfo
  deletionOperations: Readonly<Record<string, ServerDeletionOperation>>
  ensureAssetBodiesLoaded?: (assetIds: readonly string[]) => Promise<Map<string, string>>
  groups: FileGroupRecord[]
  job: ServerCollectionJobState | null
  knowledgeSync: KnowledgeSyncOptions
  onCancelReindex: (jobId: string) => Promise<void>
  onCollectionDeleted: () => void
  onCollectionMutated: () => void
  onDeleteCollection: (collectionId: string) => Promise<void>
  onDeleteDocument: (documentId: string) => Promise<void>
  onRetryDeletion: (operationId: string) => Promise<void>
  onResumeReindex: (jobId: string) => Promise<void>
  onResumeRawReindex: (jobId: string) => Promise<void>
  onShare?: (collection: KnowledgeCollectionInfo) => void
  onStartReindex: (collectionId: string) => Promise<void>
  query?: string
  recoveryPending?: 'raw' | 'resume' | null
  refreshToken?: number
  sections: FileLibrarySectionRecord[]
}

type ServerCollectionDocumentCacheEntry = {
  documents?: KnowledgeDocumentInfo[]
  inFlight?: Promise<KnowledgeDocumentInfo[]>
}

// The weak connection object bounds lifetime; the nested lifecycle key fences
// project, authenticated principal and backend. Cookie-account switches can
// reuse transport options, so object identity alone is not an authorization
// boundary.
const serverCollectionDocumentCaches = new WeakMap<
  KnowledgeSyncOptions,
  Map<string, Map<string, ServerCollectionDocumentCacheEntry>>
>()

function serverCollectionCache(
  options: KnowledgeSyncOptions,
  scopeKey: string,
): Map<string, ServerCollectionDocumentCacheEntry> {
  const scopes = serverCollectionDocumentCaches.get(options)
    ?? new Map<string, Map<string, ServerCollectionDocumentCacheEntry>>()
  if (!serverCollectionDocumentCaches.has(options)) {
    serverCollectionDocumentCaches.set(options, scopes)
  }
  const current = scopes.get(scopeKey)
  if (current) return current
  const created = new Map<string, ServerCollectionDocumentCacheEntry>()
  scopes.set(scopeKey, created)
  return created
}

function cachedServerCollectionDocuments(
  collectionId: string,
  options: KnowledgeSyncOptions,
  scopeKey: string,
): KnowledgeDocumentInfo[] | undefined {
  return serverCollectionCache(options, scopeKey).get(collectionId)?.documents
}

export async function fetchServerCollectionDocuments(
  collectionId: string,
  options: KnowledgeSyncOptions,
  scopeKey: string,
): Promise<KnowledgeDocumentInfo[]> {
  const cache = serverCollectionCache(options, scopeKey)
  const entry = cache.get(collectionId) ?? {}
  if (entry.inFlight) return entry.inFlight

  const request = (async () => {
    const incoming: KnowledgeDocumentInfo[] = []
    let cursor: string | undefined
    do {
      const page = await listKnowledgeDocuments(collectionId, {
        ...options,
        cursor,
        limit: 100,
      })
      incoming.push(...page.data)
      cursor = page.next_cursor ?? undefined
    } while (cursor)
    entry.documents = incoming
    return incoming
  })()
  entry.inFlight = request
  cache.set(collectionId, entry)
  const release = () => {
    if (entry.inFlight === request) delete entry.inFlight
  }
  void request.then(release, release)
  return request
}

/** Pointer/focus intent path shared with the selected collection's loader. */
export async function prefetchServerCollectionDocuments(
  collectionId: string,
  options: KnowledgeSyncOptions,
  scopeKey: string,
): Promise<void> {
  await fetchServerCollectionDocuments(collectionId, options, scopeKey)
}

/** Canonical server collection view. Accepted shares stay server objects: the
 * recipient never gets a synthetic local VectorIndex that could drift from
 * access, documents, or maintenance state. */
export function ServerCollectionPanel({
  assets,
  cacheScopeKey,
  collection,
  deletionOperations,
  ensureAssetBodiesLoaded,
  groups,
  job,
  knowledgeSync,
  onCancelReindex,
  onCollectionDeleted,
  onCollectionMutated,
  onDeleteCollection,
  onDeleteDocument,
  onRetryDeletion,
  onResumeReindex,
  onResumeRawReindex,
  onShare,
  onStartReindex,
  query = '',
  recoveryPending = null,
  refreshToken = 0,
  sections,
}: ServerCollectionPanelProps) {
  const { t } = useLocale()
  const initialDocuments = cachedServerCollectionDocuments(
    collection.id,
    knowledgeSync,
    cacheScopeKey,
  )
  const [documents, setDocuments] = useState<KnowledgeDocumentInfo[]>(initialDocuments ?? [])
  const [documentsIdentity, setDocumentsIdentity] = useState<string | null>(
    initialDocuments ? collection.id : null,
  )
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [errorIdentity, setErrorIdentity] = useState<string | null>(null)
  const [adding, setAdding] = useState(false)
  const [mutating, setMutating] = useState(false)
  const generationRef = useRef(0)
  const completedDeletionIdsRef = useRef(new Set<string>())
  const deletionList = useMemo(
    () => Object.values(deletionOperations).sort(
      (left, right) => right.created_at - left.created_at,
    ),
    [deletionOperations],
  )
  const collectionDeletion = deletionList.find(
    (operation) => operation.target_kind === 'knowledge_collection'
      && operation.target_id === collection.id,
  )
  const documentDeletions = useMemo(() => {
    const byDocument = new Map<string, ServerDeletionOperation>()
    for (const operation of deletionList) {
      if (
        operation.target_kind === 'knowledge_document'
        && !byDocument.has(operation.target_id)
      ) {
        byDocument.set(operation.target_id, operation)
      }
    }
    return byDocument
  }, [deletionList])
  const collectionDeleting = collectionDeletion?.status === 'queued'
    || collectionDeletion?.status === 'running'
  const collectionDeleteFailed = collectionDeletion?.status === 'delete_failed'

  const loadDocuments = useCallback(async () => {
    if (collectionDeleting) return
    const generation = ++generationRef.current
    setLoading(true)
    setError(null)
    setErrorIdentity(null)
    try {
      const incoming = await fetchServerCollectionDocuments(
        collection.id,
        knowledgeSync,
        cacheScopeKey,
      )
      if (generation !== generationRef.current) return
      setDocuments(incoming)
      setDocumentsIdentity(collection.id)
      setError(null)
      setErrorIdentity(null)
    } catch (cause) {
      if (generation !== generationRef.current) return
      setError(cause instanceof Error ? cause.message : String(cause))
      setErrorIdentity(collection.id)
    } finally {
      if (generation === generationRef.current) setLoading(false)
    }
  }, [cacheScopeKey, collection.id, collectionDeleting, knowledgeSync])

  useEffect(() => {
    void loadDocuments()
    return () => {
      generationRef.current += 1
    }
  }, [loadDocuments, refreshToken])

  useEffect(() => {
    if (documentsIdentity !== collection.id) return
    const cache = serverCollectionCache(knowledgeSync, cacheScopeKey)
    const entry = cache.get(collection.id) ?? {}
    entry.documents = documents
    cache.set(collection.id, entry)
  }, [cacheScopeKey, collection.id, documents, documentsIdentity, knowledgeSync])

  useEffect(() => {
    if (
      collectionDeletion?.status === 'deleted'
      && !completedDeletionIdsRef.current.has(collectionDeletion.operation_id)
    ) {
      completedDeletionIdsRef.current.add(collectionDeletion.operation_id)
      onCollectionDeleted()
      onCollectionMutated()
    }
    const deletedDocumentIds: string[] = []
    for (const [documentId, operation] of documentDeletions) {
      if (
        operation.status === 'deleted'
        && !completedDeletionIdsRef.current.has(operation.operation_id)
      ) {
        completedDeletionIdsRef.current.add(operation.operation_id)
        deletedDocumentIds.push(documentId)
      }
    }
    if (deletedDocumentIds.length > 0) {
      const deleted = new Set(deletedDocumentIds)
      setDocuments((current) => current.filter((item) => !deleted.has(item.id)))
      onCollectionMutated()
    }
  }, [collectionDeletion, documentDeletions, onCollectionDeleted, onCollectionMutated])

  const cachedCurrentDocuments = cachedServerCollectionDocuments(
    collection.id,
    knowledgeSync,
    cacheScopeKey,
  )
  const hasCurrentSnapshot = documentsIdentity === collection.id
    || cachedCurrentDocuments !== undefined
  const currentDocuments = documentsIdentity === collection.id
    ? documents
    : cachedCurrentDocuments ?? []
  const currentError = errorIdentity === null || errorIdentity === collection.id ? error : null
  const structuralPhase: StructuralLoadPhase = hasCurrentSnapshot
    ? loading ? 'refreshing' : currentDocuments.length === 0 ? 'empty' : 'ready'
    : currentError ? 'error' : 'pending'
  const owner = collection.access.mode === 'owner'
  const editable = owner
    || (collection.access.mode === 'shared' && collection.access.permission === 'edit')
  const memberIds = useMemo(() => new Set(
    currentDocuments
      .map((document) => document.metadata.fileId)
      .filter((value): value is string => typeof value === 'string'),
  ), [currentDocuments])
  const activeJob = job?.status === 'error' ? null : job
  const paused = activeJob?.status === 'paused_dependency'
    || activeJob?.status === 'paused_validation'
  const normalizedQuery = query.trim().toLocaleLowerCase()
  const visibleDocuments = useMemo(
    () => normalizedQuery.length === 0
      ? currentDocuments
      : currentDocuments.filter((document) => document.title.toLocaleLowerCase().includes(normalizedQuery)),
    [currentDocuments, normalizedQuery],
  )

  const startReindex = async () => {
    setError(null)
    try {
      await onStartReindex(collection.id)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  const cancelReindex = async () => {
    if (!activeJob) return
    try {
      await onCancelReindex(activeJob.jobId)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  const resumeReindex = async () => {
    if (!activeJob || !paused) return
    setError(null)
    try {
      await onResumeReindex(activeJob.jobId)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  const resumeRawReindex = async () => {
    if (!activeJob || !paused) return
    setError(null)
    try {
      await onResumeRawReindex(activeJob.jobId)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  const addAssets = async (assetIds: string[]) => {
    if (!editable || mutating) return
    setMutating(true)
    setError(null)
    let ingestStarted = false
    try {
      const selected = assetIds
        .map((assetId) => assets.find((asset) => asset.id === assetId))
        .filter((asset): asset is FileAssetRecord => asset !== undefined)
      const bodies = ensureAssetBodiesLoaded
        ? await ensureAssetBodiesLoaded(selected.map((asset) => asset.id))
        : null
      const resolved = bodies
        ? selected.map((asset) => ({
            ...asset,
            extractedText: bodies.get(asset.id) ?? asset.extractedText,
          }))
        : selected
      ingestStarted = true
      const result = await ingestAssetsIntoKnowledgeCollection(
        collection.id,
        resolved,
        knowledgeSync,
      )
      setAdding(false)
      await loadDocuments()
      onCollectionMutated()
      if (result.skippedFiles.length > 0) {
        setError(t.vectorIndex.serverSkippedDocuments.replace(
          '{names}',
          result.skippedFiles.join(', '),
        ))
      }
    } catch (cause) {
      // Ingest is intentionally per document. If a later document fails, the
      // earlier writes are already authoritative; refresh instead of leaving
      // the panel looking as if the whole batch rolled back.
      if (ingestStarted) {
        await loadDocuments()
        onCollectionMutated()
      }
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setMutating(false)
    }
  }

  const removeDocument = async (documentId: string) => {
    if (!editable || mutating || collectionDeleting) return
    setMutating(true)
    try {
      await onDeleteDocument(documentId)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setMutating(false)
    }
  }

  const removeCollection = async () => {
    if (!owner || mutating || collectionDeleting) return
    setMutating(true)
    try {
      await onDeleteCollection(collection.id)
      setError(null)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    } finally {
      setMutating(false)
    }
  }

  const retryDeletion = async (operationId: string) => {
    setError(null)
    try {
      await onRetryDeletion(operationId)
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause))
    }
  }

  return (
    <StructuralLoadBoundary
      className="min-h-0 flex-1"
      fallback={<ServerCollectionSkeleton />}
      identity={`library:server-collection:${collection.id}`}
      phase={structuralPhase}
    >
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 px-4 pt-0.5 md:px-6">
        <div className="rounded-lg border border-border bg-card p-3.5 shadow-[0_1px_2px_var(--shadow-hairline)]">
          <div className="flex flex-wrap items-center gap-3">
            <span className="grid size-10 shrink-0 place-items-center rounded-lg border border-file/25 bg-file-subtle text-file">
              <FileText className="size-5" />
            </span>
            <div className="min-w-0 flex-1">
              <h2 className="truncate t-card text-foreground">{collection.name}</h2>
              <div className="mt-1 flex flex-wrap items-center gap-2 t-meta text-muted-foreground">
                <span>{collection.embedding_model}</span>
                <span>·</span>
                <span>{countLabel(currentDocuments.length, t.vectorIndex.serverDocumentCountOne, t.vectorIndex.serverDocumentCount)}</span>
                {collection.access.mode === 'shared' ? (
                  <span className="rounded-md border border-brand/25 bg-brand-subtle px-1.5 py-0.5 t-meta-sm font-medium text-brand">
                    {collection.access.permission === 'edit'
                      ? t.sharing.sharedCanEdit
                      : t.sharing.sharedViewOnly}
                  </span>
                ) : null}
              </div>
            </div>
            <div className="flex shrink-0 flex-wrap items-center gap-2">
              {owner && onShare ? (
                <Button disabled={collectionDeleting} onClick={() => onShare(collection)} size="sm" type="button" variant="outline">
                  <Users className="size-4" />
                  {t.sharing.share}
                </Button>
              ) : null}
              {editable ? (
                <Button
                  disabled={collectionDeleting || recoveryPending !== null || (!paused && activeJob !== null) || mutating}
                  onClick={() => void (paused ? resumeReindex() : startReindex())}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  <RotateCcw className={recoveryPending === 'resume'
                    ? 'size-4 motion-safe:animate-spin'
                    : 'size-4'} />
                  {recoveryPending === 'resume'
                    ? t.vectorIndex.resumeRequesting
                    : paused
                      ? t.vectorIndex.resumeIndexing
                      : t.vectorIndex.reindex}
                </Button>
              ) : null}
              {activeJob ? (
                <Button
                  aria-label={t.vectorIndex.cancelIndexing}
                  disabled={activeJob.status === 'cancelling' || recoveryPending !== null}
                  onClick={() => void cancelReindex()}
                  size="icon"
                  type="button"
                  variant="ghost"
                >
                  <XCircle className="size-4" />
                </Button>
              ) : null}
              {owner && !collectionDeleting && !collectionDeleteFailed ? (
                <ConfirmDelete
                  ariaLabel={t.fileLibrary.removeCollection}
                  hint={t.fileLibrary.removeCollectionHint}
                  label={t.fileLibrary.removeCollection}
                  onConfirm={() => void removeCollection()}
                />
              ) : null}
              {owner && collectionDeleteFailed && collectionDeletion ? (
                <Button
                  aria-label={t.fileLibrary.collectionDeletionRetry}
                  className="text-warning"
                  onClick={() => void retryDeletion(collectionDeletion.operation_id)}
                  size="sm"
                  type="button"
                  variant="outline"
                >
                  <RotateCcw className="size-4" />
                  {t.fileLibrary.collectionDeletionRetry}
                </Button>
              ) : null}
            </div>
          </div>
          {collectionDeleting ? (
            <p className="mt-3 flex items-center gap-2 border-t border-border/70 pt-3 t-meta text-muted-foreground" role="status">
              <span className="size-2 rounded-full bg-warning motion-safe:animate-pulse" />
              {collectionDeletion && collectionDeletion.completed_items > 0
                ? t.fileLibrary.deletionSearchDetached
                : t.fileLibrary.collectionDeletionRunning}
            </p>
          ) : null}
          {collectionDeleteFailed ? (
            <p className="mt-3 border-t border-warning/25 pt-3 t-meta text-warning" role="alert">
              {carriedErrorText(collectionDeletion?.error, t.fileLibrary.collectionDeletionFailed)
                ?? t.fileLibrary.collectionDeletionFailed}
            </p>
          ) : null}
          {activeJob && !paused ? (
            <p className="mt-3 border-t border-border/70 pt-3 t-meta text-muted-foreground">
              {activeJob.status === 'cancelling'
                ? t.vectorIndex.cancelling
                : countLabel(
                  activeJob.totalDocuments,
                  t.vectorIndex.progressPercentDocsOne,
                  t.vectorIndex.progressPercentDocs,
                )
                  .replace('{percent}', String(activeJob.totalDocuments > 0
                    ? Math.round((activeJob.completedDocuments / activeJob.totalDocuments) * 100)
                    : 0))
                  .replace('{done}', String(activeJob.completedDocuments))}
              {activeJob.currentDocumentTitle ? ` · ${activeJob.currentDocumentTitle}` : ''}
            </p>
          ) : null}
          {activeJob && paused ? (
            <div
              className="mt-3 rounded-md border border-warning/30 bg-warning-subtle px-3 py-2 text-warning"
              role="status"
            >
              <p className="flex items-center gap-1.5 t-meta font-semibold">
                <AlertTriangle className="size-3.5 shrink-0" />
                {activeJob.status === 'paused_validation'
                  ? t.vectorIndex.pausedValidationTitle
                  : t.vectorIndex.pausedDependencyTitle}
              </p>
              <IndexPauseNotice
                autoResume={activeJob.autoResume}
                errorType={activeJob.pauseErrorType}
                status={activeJob.status}
              />
              <p className="mt-1 t-hint text-warning/90">
                {[
                  indexPhaseText(
                    activeJob.phase,
                    activeJob.currentBatch ?? 0,
                    activeJob.totalBatches ?? 0,
                    t,
                  ),
                  t.vectorIndex.activeGenerationUnchanged,
                ].filter(Boolean).join(' · ')}
              </p>
              {allowsRawRebuild(activeJob.status, activeJob.pauseErrorType) ? (
                <div className="mt-2 flex flex-wrap items-center justify-between gap-2 border-t border-warning/20 pt-2">
                  <p className="max-w-3xl t-hint text-warning/90">
                    {t.vectorIndex.resumeWithoutContextHint}
                  </p>
                  <Button
                    className="shrink-0 border-warning/35 text-warning hover:bg-warning/10 hover:text-warning"
                    disabled={recoveryPending !== null}
                    onClick={() => void resumeRawReindex()}
                    size="sm"
                    type="button"
                    variant="outline"
                  >
                    {recoveryPending === 'raw'
                      ? t.vectorIndex.resumeWithoutContextRequesting
                      : t.vectorIndex.resumeWithoutContext}
                  </Button>
                </div>
              ) : null}
            </div>
          ) : null}
          {job?.error ? <p className="mt-2 t-meta text-destructive">{job.error}</p> : null}
          {currentError ? <p className="mt-2 t-meta text-destructive">{currentError}</p> : null}
        </div>
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="flex flex-col gap-4 px-4 pb-4 pt-4 md:px-6 md:pb-6">
          {editable ? (
            <div>
              <Button disabled={collectionDeleting || mutating || activeJob !== null} onClick={() => setAdding(true)} size="sm" type="button">
                <Plus className="size-4" />
                {t.vectorIndex.addDocuments}
              </Button>
            </div>
          ) : null}
          {adding ? (
            <div className="space-y-2">
              <p className="rounded-md border border-warning/25 bg-warning-subtle px-3 py-2 t-meta text-warning">
                {t.vectorIndex.sharedUploadOwnershipNotice}
              </p>
              <AddDocsPanel
                docs={assets}
                groups={groups}
                memberIds={memberIds}
                onAdd={(assetIds) => void addAssets(assetIds)}
                onClose={() => setAdding(false)}
                sections={sections}
              />
            </div>
          ) : null}
          {!hasCurrentSnapshot && loading ? null : visibleDocuments.length === 0 ? (
            <div className="rounded-lg border border-dashed border-border px-6 py-10 text-center">
              <p className="t-label text-foreground">
                {normalizedQuery.length > 0
                  ? t.fileLibrary.emptySearchTitle
                  : t.vectorIndex.indexEmptyTitle}
              </p>
              <p className="mt-1 t-meta text-muted-foreground">
                {normalizedQuery.length > 0
                  ? t.fileLibrary.emptySearchHint
                  : t.vectorIndex.indexEmptyHint}
              </p>
            </div>
          ) : (
            <div className="overflow-hidden rounded-lg border border-border bg-card">
              {visibleDocuments.map((document) => {
                const deletion = documentDeletions.get(document.id)
                const deleting = deletion?.status === 'queued' || deletion?.status === 'running'
                const deleteFailed = deletion?.status === 'delete_failed'
                const omitted = contextOmittedChunks(document.metadata)
                return (
                <div
                  aria-disabled={deleting || collectionDeleting}
                  className="flex items-center gap-3 border-b border-border/70 px-3 py-2.5 last:border-b-0"
                  key={document.id}
                >
                  <FileText className="size-4 shrink-0 text-file" />
                  <div className="min-w-0 flex-1">
                    <p className="truncate t-list text-foreground">{document.title}</p>
                    {deleting ? (
                      <p className="flex items-center gap-1.5 t-meta-sm text-muted-foreground" role="status">
                        <span className="size-1.5 rounded-full bg-warning motion-safe:animate-pulse" />
                        {deletion && deletion.completed_items > 0
                          ? t.fileLibrary.deletionSearchDetached
                          : t.fileLibrary.deletionRunning}
                      </p>
                    ) : deleteFailed ? (
                      <p className="t-meta-sm text-warning" role="alert">
                        {carriedErrorText(deletion?.error, t.fileLibrary.deletionFailed)
                          ?? t.fileLibrary.deletionFailed}
                      </p>
                    ) : (
                      <p className="flex min-w-0 items-center gap-1.5 t-meta-sm text-muted-foreground">
                        <span className="shrink-0">
                          {countLabel(document.chunk_count, t.vectorIndex.chunksOne, t.vectorIndex.chunks)}
                        </span>
                        {omitted > 0 ? (
                          <>
                            <span aria-hidden="true">·</span>
                            <ContextOmittedLabel count={omitted} />
                          </>
                        ) : null}
                      </p>
                    )}
                  </div>
                  {editable && deleteFailed && deletion ? (
                    <Button
                      aria-label={t.fileLibrary.deletionRetry}
                      className="size-7 text-warning"
                      onClick={() => void retryDeletion(deletion.operation_id)}
                      size="icon"
                      type="button"
                      variant="ghost"
                    >
                      <RotateCcw className="size-3.5" />
                    </Button>
                  ) : editable && !deleting && !collectionDeleting ? (
                    <ConfirmDelete
                      ariaLabel={t.vectorIndex.removeDocument}
                      hint={t.vectorIndex.removeDocumentHint}
                      onConfirm={() => void removeDocument(document.id)}
                    />
                  ) : null}
                </div>
                )
              })}
            </div>
          )}
        </div>
      </ScrollArea>
    </div>
    </StructuralLoadBoundary>
  )
}

function ServerCollectionSkeleton() {
  return (
    <div aria-hidden className="flex h-full min-h-0 flex-col bg-background">
      <div className="shrink-0 px-4 pt-0.5 md:px-6">
        <div className="rounded-lg border border-border bg-card p-3.5">
          <div className="flex items-center gap-3">
            <Skeleton className="size-10 rounded-lg" />
            <div className="flex min-w-0 flex-1 flex-col gap-2">
              <Skeleton className="h-4 w-48" />
              <Skeleton className="h-3.5 w-64 max-w-full" />
            </div>
          </div>
        </div>
      </div>
      <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-hidden px-4 pb-4 pt-4 md:px-6 md:pb-6">
        {Array.from({ length: 12 }, (_, index) => (
          <div className="flex items-center gap-3 rounded-md border border-border px-3 py-2.5" key={index}>
            <Skeleton className="size-4 shrink-0" />
            <Skeleton className="h-4 w-[42%]" />
            <Skeleton className="ml-auto h-3.5 w-20" />
          </div>
        ))}
      </div>
    </div>
  )
}
