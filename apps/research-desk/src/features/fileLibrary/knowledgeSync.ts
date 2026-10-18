import {
  cancelIndexingJob,
  createKnowledgeCollection,
  deleteKnowledgeCollection,
  getAssetDeletionOperation,
  getIndexingJob,
  listKnowledgeDocuments,
  resumeIndexingJob,
  resumeIndexingJobWithoutContext,
  startDocumentRevisionJob,
  streamIndexingJobEvents,
} from '@/api/inqtrixClient'
import type {
  AutoResumePlan,
  IndexingJobEvent,
  IndexingJobSnapshot,
  IndexingJobSummary,
} from './indexingTypes'
import type { ServerDeletionOperation } from '@/api/inqtrixClient'
import type { FileAssetRecord, VectorIndexRecord } from '@/features/project/types'
import { carriedErrorText, readerTexts } from '@/api/requestErrorText'

/** How many immutable revisions may be reserved concurrently. The POSTs are
 * short: provider work runs in the existing server job queue and this client
 * only subscribes to durable progress. */
const INGEST_CONCURRENCY = 3
const EVENT_RECONNECT_BASE_MS = 250
const EVENT_RECONNECT_MAX_MS = 5_000

const ACTIVE_JOB_STATUSES = new Set([
  'queued',
  'running',
  'cancelling',
])

const PAUSED_JOB_STATUSES = new Set([
  'paused_dependency',
  'paused_validation',
])

class DocumentRevisionJobError extends Error {
  readonly jobId: string
  readonly summary: IndexingJobSummary

  constructor(job: IndexingJobSummary, message: string) {
    super(message)
    this.name = 'DocumentRevisionJobError'
    this.jobId = job.job_id
    this.summary = job
  }
}

/** A revision paused because its own content needs review, as opposed to a
 * dependency (provider) pause that would hold every other document too. */
function isDocumentReviewPause(error: unknown): boolean {
  return error instanceof DocumentRevisionJobError
    && error.summary.status === 'paused_validation'
}

/** A paused revision job to continue instead of starting a new revision:
 * `resume` keeps its checkpoint, `raw` rebuilds it without context by the
 * user's explicit choice. */
export type PausedMemberJob = { jobId: string; mode: 'raw' | 'resume' }

type KnowledgeMemberFailure = {
  error: unknown
  fileId: string
  position: number
}

class KnowledgeAssetIngestError extends Error {
  readonly failures: KnowledgeMemberFailure[]
  readonly partial: KnowledgeAssetIngestResult

  constructor(
    partial: KnowledgeAssetIngestResult,
    failures: KnowledgeMemberFailure[],
  ) {
    const ordered = [...failures].sort((left, right) => left.position - right.position)
    const primary = ordered[0]?.error
    super(primary instanceof Error ? primary.message : String(primary))
    this.name = 'KnowledgeAssetIngestError'
    this.failures = ordered
    this.partial = partial
  }
}

export type PausedDocumentRevision = {
  fileId: string
  summary: IndexingJobSummary
  status: 'paused_dependency' | 'paused_validation'
}

/** A first build or incremental add stopped after some durable work had
 * already succeeded. The collection and confirmed document ids are preserved;
 * callers adopt this partial truth before showing the failure or pause. */
export class KnowledgeReindexPartialError extends Error {
  readonly failures: ReadonlyArray<{ fileId: string; message: string }>
  readonly pausedJobs: PausedDocumentRevision[]
  readonly result: KnowledgeReindexResult

  constructor(
    result: KnowledgeReindexResult,
    failures: KnowledgeMemberFailure[],
  ) {
    const ordered = [...failures].sort((left, right) => left.position - right.position)
    const primary = ordered[0]?.error
    super(primary instanceof Error ? primary.message : String(primary))
    this.name = 'KnowledgeReindexPartialError'
    this.result = result
    this.failures = ordered.map(({ error, fileId }) => ({
      fileId,
      message: error instanceof Error ? error.message : String(error),
    }))
    this.pausedJobs = ordered.flatMap(({ error, fileId }) => (
      error instanceof DocumentRevisionJobError
      && (
        error.summary.status === 'paused_dependency'
        || error.summary.status === 'paused_validation'
      )
        ? [{
            fileId,
            summary: error.summary,
            status: error.summary.status,
          }]
        : []
    ))
  }
}

function eventReconnectDelay(attempt: number): number {
  const exponential = Math.min(
    EVENT_RECONNECT_BASE_MS * (2 ** Math.min(attempt, 8)),
    EVENT_RECONNECT_MAX_MS,
  )
  // Bounded jitter prevents many document workers from reconnecting in lockstep.
  return Math.round(exponential * (0.75 + Math.random() * 0.5))
}

async function waitForReconnect(delayMs: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return
  await new Promise<void>((resolve) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, delayMs)
    const onAbort = () => {
      clearTimeout(timer)
      resolve()
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

async function waitForDocumentRevision(
  started: IndexingJobSummary,
  options: KnowledgeSyncOptions,
  onProgress?: (progress: MemberJobProgressEvent) => void,
): Promise<{ cancelled: boolean; summary: IndexingJobSummary }> {
  const streamController = new AbortController()
  const cancelOptions = {
    apiKey: options.apiKey,
    workspaceId: options.workspaceId,
  }
  let parentAborted = options.signal?.aborted ?? false
  let cancelSent = false
  let reconnectAttempt = 0
  let warnedAboutDisconnect = false
  let progressFingerprint = ''
  let lastEventSequence = 0
  // A pause is reported like any live state: the row reads "paused" the moment
  // the server pauses, and carries the job id the user resumes it with.
  const emitSummary = (summary: IndexingJobSummary) => {
    const paused = PAUSED_JOB_STATUSES.has(summary.status)
    if (!ACTIVE_JOB_STATUSES.has(summary.status) && !paused) return
    onProgress?.({
      currentBatch: summary.current_batch,
      jobId: summary.job_id,
      ...(paused
        ? {
            autoResume: summary.checkpoint.auto_resume ?? null,
            errorType: summary.error?.type ?? null,
          }
        : {}),
      phase: summary.phase,
      queuePosition: summary.queue_position,
      status: summary.status as MemberJobProgressEvent['status'],
      totalBatches: summary.total_batches,
    })
  }
  const emitEvent = (event: IndexingJobEvent) => {
    // A reconnect may replay the durable history. Never move a row backwards
    // from a newer batch to an older one.
    if (event.sequence <= lastEventSequence) return
    lastEventSequence = event.sequence
    const snapshot = (event.data?.snapshot ?? {}) as IndexingJobSnapshot
    if (event.type === 'inqtrix.index.queued') {
      const position = event.data?.queue_position
      onProgress?.({
        currentBatch: snapshot.current_batch,
        phase: snapshot.phase,
        queuePosition: typeof position === 'number' ? position : null,
        status: 'queued',
        totalBatches: snapshot.total_batches,
      })
    } else if (
      event.type === 'inqtrix.index.started'
      || event.type === 'inqtrix.index.progress'
      || event.type === 'inqtrix.index.resumed'
    ) {
      onProgress?.({
        currentBatch: snapshot.current_batch,
        phase: snapshot.phase,
        queuePosition: null,
        status: 'running',
        totalBatches: snapshot.total_batches,
      })
    } else if (event.type === 'inqtrix.index.cancelling') {
      onProgress?.({
        currentBatch: snapshot.current_batch,
        phase: snapshot.phase,
        queuePosition: null,
        status: 'cancelling',
        totalBatches: snapshot.total_batches,
      })
    }
  }
  const onAbort = () => {
    parentAborted = true
    streamController.abort()
  }
  options.signal?.addEventListener('abort', onAbort, { once: true })
  try {
    let summary = started
    emitSummary(summary)
    while (ACTIVE_JOB_STATUSES.has(summary.status)) {
      if (options.signal?.aborted) {
        parentAborted = true
      }
      if (parentAborted && !cancelSent) {
        summary = await cancelIndexingJob(started.job_id, cancelOptions)
        cancelSent = true
      } else if (!parentAborted) {
        try {
          // SSE is the primary wait primitive. A normal stream remains open
          // until a terminal event; no summary polling runs beside it.
          await streamIndexingJobEvents(started.events_url, {
            ...cancelOptions,
            signal: streamController.signal,
            onEvent: emitEvent,
          })
        } catch (error) {
          // Transport loss cannot decide the durable job outcome. Reconnect
          // below with bounded backoff; warn once instead of flooding logs.
          if (!streamController.signal.aborted && !warnedAboutDisconnect) {
            warnedAboutDisconnect = true
            console.warn('Document indexing event stream disconnected.', error)
          }
        }
        summary = options.signal?.aborted
          ? await cancelIndexingJob(started.job_id, cancelOptions)
          : await getIndexingJob(started.job_id, cancelOptions)
        emitSummary(summary)
        if (options.signal?.aborted) {
          parentAborted = true
          cancelSent = true
        }
      } else {
        summary = await getIndexingJob(started.job_id, cancelOptions)
        emitSummary(summary)
      }

      if (!ACTIVE_JOB_STATUSES.has(summary.status)) break
      const nextFingerprint = [
        summary.status,
        summary.phase,
        summary.current_batch,
        summary.completed_documents,
      ].join(':')
      reconnectAttempt = nextFingerprint === progressFingerprint
        ? reconnectAttempt + 1
        : 0
      progressFingerprint = nextFingerprint
      // Once the caller aborts, the original signal is permanently aborted.
      // Cancellation still needs bounded status observation until the server
      // confirms a terminal state; reusing that signal would resolve every
      // delay immediately and create a tight GET loop.
      await waitForReconnect(
        eventReconnectDelay(reconnectAttempt),
        parentAborted ? undefined : options.signal,
      )
    }
    if (summary.status === 'completed' || summary.status === 'ready_raw_by_user_choice') {
      return { cancelled: false, summary }
    }
    if (summary.status === 'cancelled') {
      return { cancelled: true, summary }
    }
    const message = carriedErrorText(summary.error, readerTexts().fileLibrary.indexingFailed)
      ?? (summary.status === 'superseded'
        ? readerTexts().fileLibrary.indexingSuperseded
        : readerTexts().fileLibrary.indexingFailed)
    throw new DocumentRevisionJobError(summary, message)
  } finally {
    options.signal?.removeEventListener('abort', onAbort)
    streamController.abort()
  }
}

/** Connection facts needed to talk to the knowledge backend. */
export type KnowledgeSyncOptions = {
  apiKey?: string
  /** Aborts the run: in-flight requests reject and no further document is
   * started. The partial result is returned with `cancelled` set, so the
   * caller can keep what was actually embedded. */
  signal?: AbortSignal
  /**
   * Compatibility capability hint retained for callers on mixed server
   * versions. Current servers require every uploaded file asset to reference
   * its operation-fenced canonical preparation; browser extraction is never
   * promoted to knowledge-source authority.
   */
  useFileIngestion?: boolean
  workspaceId?: string
}

/** Whether a rejection is the run's own abort rather than a real failure. */
export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

/** Options for the cleanup that runs AFTER a run was aborted. It must not
 * carry the run's signal: an already-aborted signal makes fetch reject
 * before the request is sent, so the collection we mean to remove would
 * silently survive. */
function cleanupOptions(options: KnowledgeSyncOptions): KnowledgeSyncOptions {
  return { ...options, signal: undefined }
}

async function waitForCollectionDeletion(
  started: ServerDeletionOperation,
  options: KnowledgeSyncOptions,
): Promise<void> {
  let current = started
  let unchangedAttempts = 0
  let fingerprint = ''
  while (current.status === 'queued' || current.status === 'running') {
    const nextFingerprint = [
      current.status,
      current.stage,
      current.completed_items,
    ].join(':')
    unchangedAttempts = nextFingerprint === fingerprint
      ? unchangedAttempts + 1
      : 0
    fingerprint = nextFingerprint
    await waitForReconnect(eventReconnectDelay(unchangedAttempts), options.signal)
    current = await getAssetDeletionOperation(current.operation_id, options)
  }
  if (current.status !== 'deleted') {
    throw new Error(
      carriedErrorText(current.error, readerTexts().fileLibrary.collectionDeletionFailed)
      ?? readerTexts().fileLibrary.collectionDeletionFailed,
    )
  }
}

async function deleteCollectionAndWait(
  collectionId: string,
  options: KnowledgeSyncOptions,
): Promise<void> {
  const safeOptions = cleanupOptions(options)
  const operation = await deleteKnowledgeCollection(collectionId, safeOptions)
  await waitForCollectionDeletion(operation, safeOptions)
}

export type KnowledgeReindexResult = {
  /** `null` only when a cancelled first build embedded nothing and its empty
   * collection was removed again — there is then no collection to adopt. */
  collectionId: string | null
  /** The run stopped early because the caller aborted it. Everything reported
   * here still landed on the server and must be kept. */
  cancelled: boolean
  /** Embedding model the collection was built/extended with — persisted on the
   * index so a later reindex can tell "model changed" from "docs added". */
  serverCollectionModel: string
  /** Asset ids actually ingested + embedded this run (the COMPLETE truth for a
   * rebuild; the newly-added subset for an incremental ingest). */
  embeddedFileIds: string[]
  /** Asset ids skipped because no extracted text was available (never silently
   * dropped — the caller leaves them pending so the index reads honestly). */
  skippedFileIds: string[]
  /** Labels of the skipped members (back-compat surface for messaging). */
  skippedFiles: string[]
  uploadedDocuments: number
  /** fileId -> backend knowledge-document id for each member ingested this run
   * (persisted on the member so a later removal can delete the exact doc). */
  serverDocumentIds: Record<string, string>
}

/** Uploaded assets are indexed only through their stable asset identity. The
 * revision endpoint validates the canonical extract against the original file
 * digest before reserving any work. */
function hasServerSource(asset: FileAssetRecord): boolean {
  return Boolean(asset.serverFileId)
}

export type KnowledgeAssetIngestResult = {
  /** The run was aborted before every member was processed. */
  cancelled: boolean
  embeddedFileIds: string[]
  skippedFileIds: string[]
  skippedFiles: string[]
  /** fileId -> backend knowledge-document id for each ingested member, so the
   * caller can persist it on the member (enables single-doc removal later). */
  serverDocumentIds: Record<string, string>
}

/** Per-member progress callback fired AFTER each member's server-confirmed
 * ingest (or skip), so the caller can advance the progress bar + flip that
 * file row to its real outcome — genuine server feedback, not cosmetic. */
export type MemberProgress = (event: {
  fileId: string
  done: number
  total: number
  embedded: boolean
}) => void

/** Fired when a member's ingest STARTS, before its request goes out. The only
 * signal available during the long synchronous server-side embed, so the UI
 * can name the documents it is working on instead of sitting at zero.
 * `done` is the CONFIRMED completion count, never the queue position —
 * several members are in flight at once, so a position would overstate
 * progress and then jump backwards on the next completion. */
export type MemberStart = (event: {
  fileId: string
  done: number
  total: number
}) => void

/** Fired when a member's ingest failed. Not an outcome the index records —
 * the run fails as a whole — but the caller must stop presenting it as work
 * in progress. */
export type MemberFailed = (event: { fileId: string }) => void

/** Authoritative live state of one durable document-revision job. Batch
 * counters describe contextualization batches, not guessed document chunks. */
export type MemberJobProgressEvent = {
  currentBatch?: number
  /** The member's durable revision job, so a pause can be resumed. */
  jobId?: string
  /** While the job is paused: what it waits for and the server's plan to
   * resume it on its own. */
  errorType?: string | null
  autoResume?: AutoResumePlan | null
  phase?: string
  queuePosition?: number | null
  status:
    | 'queued'
    | 'running'
    | 'cancelling'
    | 'paused_dependency'
    | 'paused_validation'
  totalBatches?: number
}

export type MemberJobProgress = (
  event: MemberJobProgressEvent & { fileId: string },
) => void

export type IngestProgress = {
  onMemberDone?: MemberProgress
  onMemberFailed?: MemberFailed
  onMemberJobProgress?: MemberJobProgress
  onMemberStart?: MemberStart
}

/** Ingest a list of member assets into an EXISTING collection id. Defined once
 * and shared by the rebuild-from-scratch path and the
 * incremental add path. Members with neither usable text nor file ingestion are
 * reported back (never silently dropped), including a rebuild where every
 * member is terminally skipped and the fresh server collection stays empty. */
async function ingestMembersIntoCollection(
  collectionId: string,
  memberAssets: FileAssetRecord[],
  options: KnowledgeSyncOptions,
  progress?: IngestProgress,
  /** fileId -> existing knowledge-document id. Members listed here are already
   * on the server and are adopted instead of re-ingested (resume after a
   * cancelled run; the server finishes an aborted request's document). */
  existingDocumentIds?: ReadonlyMap<string, string>,
  resumeJobs?: ReadonlyMap<string, PausedMemberJob>,
): Promise<KnowledgeAssetIngestResult> {
  const embeddedFileIds: string[] = []
  const skippedFileIds: string[] = []
  const skippedFiles: string[] = []
  const serverDocumentIds: Record<string, string> = {}
  const total = memberAssets.length
  let done = 0
  let cancelled = false

  const settle = (asset: FileAssetRecord, embedded: boolean) => {
    done += 1
    progress?.onMemberDone?.({ fileId: asset.id, done, total, embedded })
  }

  const ingestOne = async (asset: FileAssetRecord) => {
    progress?.onMemberStart?.({ fileId: asset.id, done, total })
    const paused = resumeJobs?.get(asset.id)
    // A paused job continues from its checkpoint before any adoption check:
    // the collection may already list its document while the paused revision
    // is still owed.
    const adopted = paused ? undefined : existingDocumentIds?.get(asset.id)
    if (adopted) {
      // Already on the server from an earlier (cancelled) run — count it,
      // never pay for a second embedding of the same document.
      embeddedFileIds.push(asset.id)
      serverDocumentIds[asset.id] = adopted
      settle(asset, true)
      return
    }
    const useServerSource = hasServerSource(asset)
    if (!useServerSource) {
      skippedFileIds.push(asset.id)
      skippedFiles.push(asset.label)
      settle(asset, false)
      return
    }
    // `fileId` = the local asset id (member mapping); `file_id` = the SERVER
    // file id (when uploaded) so the knowledge citation viewer can load the
    // original PDF for the page-jump. Omitted when there is no server file
    // (text-only docs) → the viewer shows no source PDF.
    const metadata = {
      fileId: asset.id,
      fileName: asset.fileName,
      ...(asset.serverFileId ? { file_id: asset.serverFileId } : {}),
    }
    const jobOptions = { apiKey: options.apiKey, workspaceId: options.workspaceId }
    const started = !paused
      ? await startDocumentRevisionJob(
          collectionId,
          { assetId: asset.id, metadata, title: asset.title },
          { ...options, signal: undefined },
        )
      : paused.mode === 'raw'
        ? await resumeIndexingJobWithoutContext(paused.jobId, jobOptions)
        : await resumeIndexingJob(paused.jobId, jobOptions)
    const outcome = await waitForDocumentRevision(
      started,
      options,
      (jobProgress) => progress?.onMemberJobProgress?.({
        ...jobProgress,
        fileId: asset.id,
      }),
    )
    if (outcome.cancelled) {
      cancelled = true
      return
    }
    const documentId = outcome.summary.document_id
    if (!documentId) {
      throw new DocumentRevisionJobError(
        outcome.summary,
        'Completed document indexing job did not return a document id.',
      )
    }
    embeddedFileIds.push(asset.id)
    serverDocumentIds[asset.id] = documentId
    settle(asset, true)
  }

  // Bounded worker pool: several documents embed at once, but the queue is
  // shared so the endpoint never sees more than INGEST_CONCURRENCY at a time.
  // Every worker re-checks both stop conditions before taking more work, so a
  // cancel OR a sibling's failure ends the run within one in-flight request
  // instead of walking the rest of the queue.
  let cursor = 0
  const failures: KnowledgeMemberFailure[] = []
  let stopTakingWork = false
  const worker = async () => {
    while (cursor < memberAssets.length) {
      if (stopTakingWork) return
      if (options.signal?.aborted) {
        cancelled = true
        return
      }
      const position = cursor
      cursor += 1
      try {
        await ingestOne(memberAssets[position])
      } catch (error) {
        if (isAbortError(error)) {
          cancelled = true
          return
        }
        progress?.onMemberFailed?.({ fileId: memberAssets[position].id })
        failures.push({
          error,
          fileId: memberAssets[position].id,
          position,
        })
        // A document waiting for review concerns that document only, so the
        // queue keeps going. Any other failure stops every worker: keeping the
        // others running would start fresh provider work after the dependency
        // has already failed. Requests already in flight are allowed to settle
        // so their confirmed work can be adopted instead of being deleted.
        if (isDocumentReviewPause(error)) continue
        stopTakingWork = true
        return
      }
    }
  }
  const workers = Array.from(
    { length: Math.max(1, Math.min(INGEST_CONCURRENCY, memberAssets.length)) },
    worker,
  )
  // Wait for EVERY already-started worker to settle. Their successful
  // documents are durable truth and must be returned even when a sibling
  // pauses or fails.
  await Promise.all(workers)
  const partial = {
    cancelled,
    embeddedFileIds,
    skippedFileIds,
    skippedFiles,
    serverDocumentIds,
  }
  if (failures.length > 0) {
    for (const failure of failures) {
      if (
        failure.error instanceof DocumentRevisionJobError
        && failure.error.summary.document_id
      ) {
        serverDocumentIds[failure.fileId] = failure.error.summary.document_id
      }
    }
    throw new KnowledgeAssetIngestError(partial, failures)
  }

  return partial
}

/** Map the collection's already-ingested documents by their local asset id.
 * A failed listing FAILS the run: without this map a resumed run re-ingests
 * documents the collection already holds, and the duplicate carries the same
 * `metadata.fileId` while only the newer id is tracked — the older copy then
 * stays searchable forever and cannot be removed from the UI. */
async function existingDocumentIdsByFileId(
  collectionId: string,
  options: KnowledgeSyncOptions,
): Promise<Map<string, string>> {
  const byFileId = new Map<string, string>()
  let cursor: string | undefined
  do {
    const page = await listKnowledgeDocuments(collectionId, { ...options, cursor, limit: 100 })
    for (const document of page.data) {
      const fileId = document.metadata?.fileId
      if (typeof fileId === 'string' && !byFileId.has(fileId)) byFileId.set(fileId, document.id)
    }
    cursor = page.next_cursor ?? undefined
  } while (cursor)
  return byFileId
}

/** Create the server collection for a not-yet-built local vector index.

Once a collection exists its identity and embedding model are immutable from
this local setup surface. Refreshes run in place through the indexing-job API;
this function must therefore never delete or replace an existing collection
(which would revoke shares and invalidate every server reference). */
export async function createVectorIndexCollectionOnServer(
  index: VectorIndexRecord,
  memberAssets: FileAssetRecord[],
  options: KnowledgeSyncOptions,
  progress?: IngestProgress,
): Promise<KnowledgeReindexResult> {
  if (index.serverCollectionId) {
    throw new Error(
      'createVectorIndexCollectionOnServer requires an index without a server collection.',
    )
  }

  const collection = await createKnowledgeCollection(
    { embeddingModel: index.model, name: index.title },
    options,
  )

  let ingest: KnowledgeAssetIngestResult
  try {
    ingest = await ingestMembersIntoCollection(
      collection.id, memberAssets, options, progress,
    )
  } catch (error) {
    if (error instanceof KnowledgeAssetIngestError) {
      throw new KnowledgeReindexPartialError({
        collectionId: collection.id,
        cancelled: error.partial.cancelled,
        serverCollectionModel: index.model,
        embeddedFileIds: error.partial.embeddedFileIds,
        skippedFileIds: error.partial.skippedFileIds,
        skippedFiles: error.partial.skippedFiles,
        uploadedDocuments: error.partial.embeddedFileIds.length,
        serverDocumentIds: error.partial.serverDocumentIds,
      }, error.failures)
    }
    // The collection exists and is the only stable address for diagnosing or
    // retrying this first build. Never destroy it as compensation for a child
    // failure; an explicit user deletion remains the sole destructive path.
    throw error
  }

  if (ingest.cancelled && ingest.embeddedFileIds.length === 0) {
    // Cancelled before anything landed: the empty collection would surface as
    // a phantom the user never asked for. Report "no collection" only after
    // the durable deletion operation proves that teardown is complete.
    let collectionRemoved = false
    try {
      await deleteCollectionAndWait(collection.id, options)
      collectionRemoved = true
    } catch {
      // Preserve the server identity when deletion is unresolved. Returning
      // null here would turn a failed async teardown into a false terminal
      // success and leave the collection impossible to address from the UI.
    }
    return {
      collectionId: collectionRemoved ? null : collection.id,
      cancelled: true,
      serverCollectionModel: index.model,
      embeddedFileIds: [],
      skippedFileIds: ingest.skippedFileIds,
      skippedFiles: ingest.skippedFiles,
      uploadedDocuments: 0,
      serverDocumentIds: {},
    }
  }

  // A cancelled run that DID embed documents keeps its collection: those
  // embeddings are real and the index adopts them (a later run resumes).
  return {
    collectionId: collection.id,
    cancelled: ingest.cancelled,
    serverCollectionModel: index.model,
    embeddedFileIds: ingest.embeddedFileIds,
    skippedFileIds: ingest.skippedFileIds,
    skippedFiles: ingest.skippedFiles,
    uploadedDocuments: ingest.embeddedFileIds.length,
    serverDocumentIds: ingest.serverDocumentIds,
  }
}

/** Add local assets to an existing server collection. Used by both an owner's
 * canonical collection view and an accepted editor share; no local VectorIndex
 * record is created for the recipient. */
export async function ingestAssetsIntoKnowledgeCollection(
  collectionId: string,
  assets: FileAssetRecord[],
  options: KnowledgeSyncOptions,
  progress?: IngestProgress,
): Promise<KnowledgeAssetIngestResult> {
  return ingestMembersIntoCollection(collectionId, assets, options, progress)
}

/** Incrementally ingest the index's newly-added (pending) members into its
EXISTING server collection — no delete/recreate, no re-embedding of documents
already present. Used when documents are added to an already-built index and the
embedding model is unchanged: only the new members are uploaded, so a small add
does not trigger a full rebuild, and every added document is ingested. Members
without text are reported, not dropped; partial
success is fine (the caller marks only the embedded ones). */
export async function ingestNewVectorIndexMembers(
  index: VectorIndexRecord,
  pendingMemberAssets: FileAssetRecord[],
  options: KnowledgeSyncOptions,
  progress?: IngestProgress,
  /** fileId -> paused revision job of an earlier run ("Alle fortsetzen"):
   * continued instead of started, so its checkpoint is not paid twice. */
  resumeJobs?: ReadonlyMap<string, PausedMemberJob>,
): Promise<KnowledgeReindexResult> {
  if (!index.serverCollectionId) {
    throw new Error(
      'ingestNewVectorIndexMembers requires an existing server collection.',
    )
  }
  // Adopt what the collection already holds instead of embedding it twice.
  // A cancelled run can leave a document the server finished after the client
  // stopped listening, and a resumed run must not pay for it again.
  const existing = await existingDocumentIdsByFileId(index.serverCollectionId, options)
  let ingest: KnowledgeAssetIngestResult
  try {
    ingest = await ingestMembersIntoCollection(
      index.serverCollectionId,
      pendingMemberAssets,
      options,
      progress,
      existing,
      resumeJobs,
    )
  } catch (error) {
    if (error instanceof KnowledgeAssetIngestError) {
      throw new KnowledgeReindexPartialError({
        collectionId: index.serverCollectionId,
        cancelled: error.partial.cancelled,
        serverCollectionModel: index.serverCollectionModel ?? index.model,
        embeddedFileIds: error.partial.embeddedFileIds,
        skippedFileIds: error.partial.skippedFileIds,
        skippedFiles: error.partial.skippedFiles,
        uploadedDocuments: error.partial.embeddedFileIds.length,
        serverDocumentIds: error.partial.serverDocumentIds,
      }, error.failures)
    }
    throw error
  }
  return {
    collectionId: index.serverCollectionId,
    cancelled: ingest.cancelled,
    serverCollectionModel: index.serverCollectionModel ?? index.model,
    embeddedFileIds: ingest.embeddedFileIds,
    skippedFileIds: ingest.skippedFileIds,
    skippedFiles: ingest.skippedFiles,
    uploadedDocuments: ingest.embeddedFileIds.length,
    serverDocumentIds: ingest.serverDocumentIds,
  }
}
