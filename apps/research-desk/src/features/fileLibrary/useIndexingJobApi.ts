import { useCallback, useEffect, useRef } from 'react'
import {
  cancelIndexingJob,
  hasHttpStatus,
  listIndexingJobs,
  resumeIndexingJob,
  resumeIndexingJobWithoutContext,
  startIndexingJob,
  streamIndexingJobEvents,
} from '@/api/inqtrixClient'
import {
  currentIndexingJobs,
  indexingJobDisposition,
  isTerminalIndexingStatus,
  type ActiveIndexingJobStatus,
  type AutoResumePlan,
  type IndexingJobEvent,
  type IndexingJobSnapshot,
  type IndexingJobSummary,
  type PausedIndexingJobStatus,
} from './indexingTypes'
import { carriedErrorText, messageFromError, readerError, readerTexts } from '@/api/requestErrorText'

export type IndexingPauseView = {
  /** The server's plan to resume this dependency pause on its own. */
  autoResume: AutoResumePlan | null
  completedDocuments: number
  currentBatch: number
  /** What the job waits for: the server's error type. */
  errorType: string | null
  generationId: string | null
  jobId: string
  phase: string
  status: 'paused_dependency' | 'paused_validation'
  totalBatches: number
  totalDocuments: number
}

export type IndexingMemberProgressView = {
  currentBatch?: number
  phase?: string
  status: 'queued' | 'running' | 'cancelling'
  totalBatches?: number
}

/**
 * Drives the server-backed reindex lifecycle, mirroring
 * {@link useResearchRunApi}: start a job, stream its progress via SSE,
 * cancel it, and re-attach to in-flight jobs on mount (so a reindex
 * survives closing and reopening the app). Events are keyed by job id and
 * resolved through the authoritative parent `collection_id`; callers may map
 * that collection onto local presentation state. Progress is throttled so a
 * large re-embed never floods the store.
 */
type IndexingCallbacks = {
  onCancelled: (collectionId: string) => void
  onComplete: (
    collectionId: string,
    summary: IndexingJobSummary,
  ) => void
  /** A single document finished embedding (server document id) — flips just
   * that file's row live, so a re-embed does not flip all files together. */
  onDocumentCompleted: (collectionId: string, documentId: string) => void
  /** Stable document identity precedes all phase events for that document. */
  onDocumentStarted: (collectionId: string, documentId: string) => void
  onDocumentProgress: (
    collectionId: string,
    documentId: string,
    progress: IndexingMemberProgressView,
  ) => void
  onError: (collectionId: string, message: string) => void
  onProgress: (
    collectionId: string,
    completedDocuments: number,
    totalDocuments: number,
    currentDocumentTitle?: string,
  ) => void
  onQueued: (collectionId: string, queuePosition: number | null) => void
  onPaused?: (collectionId: string, pause: IndexingPauseView) => void
  /** Every paused document revision of one collection found on load: they
   * come back as ONE paused run, never one job at a time. */
  onPausedDocuments: (collectionId: string, jobs: IndexingJobSummary[]) => void
  onReadyRaw?: (collectionId: string, jobId: string) => void
  onResumed?: (
    collectionId: string,
    jobId: string,
    totalDocuments: number,
  ) => void
  onSuperseded?: (collectionId: string, jobId: string) => void
  onStart: (
    collectionId: string,
    jobId: string,
    totalDocuments: number,
    status: ActiveIndexingJobStatus | PausedIndexingJobStatus,
    summary: IndexingJobSummary,
  ) => void
}

type UseIndexingJobApiOptions = IndexingCallbacks & {
  apiKey?: string
  enabled: boolean
  /** Cookie-session auth flip re-runs resume hydration (no remount). */
  sessionAuthed?: boolean
  /** User invalidation generation: re-list jobs started by another authorized
   * editor without adding a second polling or per-resource stream. */
  refreshToken?: number
  workspaceId: string
}

const PROGRESS_THROTTLE_MS = 150

export function useIndexingJobApi({
  apiKey,
  enabled,
  onCancelled,
  onComplete,
  onDocumentCompleted,
  onDocumentProgress,
  onDocumentStarted,
  onError,
  onProgress,
  onQueued,
  onPaused,
  onPausedDocuments,
  onReadyRaw,
  onResumed,
  onStart,
  onSuperseded,
  refreshToken = 0,
  sessionAuthed,
  workspaceId,
}: UseIndexingJobApiOptions) {
  const streamsRef = useRef(new Map<string, AbortController>())
  const jobCollectionRef = useRef(new Map<string, string>())
  const jobSummaryRef = useRef(new Map<string, IndexingJobSummary>())
  const lastProgressRef = useRef(new Map<string, number>())
  const callbacksRef = useRef<IndexingCallbacks>({
    onCancelled,
    onComplete,
    onDocumentCompleted,
    onDocumentProgress,
    onDocumentStarted,
    onError,
    onProgress,
    onQueued,
    onPaused,
    onPausedDocuments,
    onReadyRaw,
    onResumed,
    onStart,
    onSuperseded,
  })

  useEffect(() => {
    callbacksRef.current = {
      onCancelled,
      onComplete,
      onDocumentCompleted,
      onDocumentProgress,
      onDocumentStarted,
      onError,
      onProgress,
      onQueued,
      onPaused,
      onPausedDocuments,
      onReadyRaw,
      onResumed,
      onStart,
      onSuperseded,
    }
  }, [
    onCancelled,
    onComplete,
    onDocumentCompleted,
    onDocumentProgress,
    onDocumentStarted,
    onError,
    onPaused,
    onPausedDocuments,
    onProgress,
    onQueued,
    onReadyRaw,
    onResumed,
    onStart,
    onSuperseded,
  ])

  const handleEvent = useCallback((event: IndexingJobEvent) => {
    const collectionId = jobCollectionRef.current.get(event.job_id)
    const summary = jobSummaryRef.current.get(event.job_id)
    if (!collectionId || !summary) return
    const snapshot = (event.data?.snapshot ?? {}) as IndexingJobSnapshot
    const emitProgress = () =>
      callbacksRef.current.onProgress(
        collectionId,
        snapshot.completed_documents ?? 0,
        snapshot.total_documents ?? 0,
        snapshot.current_document_title,
      )
    if (
      event.type === 'inqtrix.index.progress'
      || event.type === 'inqtrix.index.started'
    ) {
      const now = Date.now()
      const last = lastProgressRef.current.get(event.job_id) ?? 0
      if (now - last < PROGRESS_THROTTLE_MS) return
      lastProgressRef.current.set(event.job_id, now)
      emitProgress()
    } else if (event.type === 'inqtrix.index.completed') {
      // Flush the terminal snapshot un-throttled first: the final progress
      // tick may have been dropped by the throttle, and the history entry
      // reads the live counts when it is recorded.
      emitProgress()
      callbacksRef.current.onComplete(collectionId, summary)
    } else if (event.type === 'inqtrix.index.failed') {
      emitProgress()
      const error = (event.data?.error ?? {}) as { message?: string }
      callbacksRef.current.onError(collectionId, error.message ?? 'Indizierung fehlgeschlagen.')
    } else if (event.type === 'inqtrix.index.cancelled') {
      emitProgress()
      callbacksRef.current.onCancelled(collectionId)
    } else if (event.type === 'inqtrix.index.queued') {
      const position = event.data?.queue_position
      callbacksRef.current.onQueued(
        collectionId,
        typeof position === 'number' ? position : null,
      )
    } else if (
      event.type === 'inqtrix.index.paused_dependency'
      || event.type === 'inqtrix.index.paused_validation'
    ) {
      emitProgress()
      const error = (event.data?.error ?? {}) as { type?: string }
      callbacksRef.current.onPaused?.(collectionId, {
        // The pause ends the event stream: its plan travels with it.
        autoResume: (event.data?.auto_resume ?? null) as AutoResumePlan | null,
        completedDocuments: snapshot.completed_documents ?? 0,
        currentBatch: snapshot.current_batch ?? 0,
        errorType: error.type ?? null,
        generationId: null,
        jobId: event.job_id,
        phase: snapshot.phase ?? 'paused',
        status: event.type.endsWith('paused_dependency')
          ? 'paused_dependency'
          : 'paused_validation',
        totalBatches: snapshot.total_batches ?? 0,
        totalDocuments: snapshot.total_documents ?? 0,
      })
    } else if (event.type === 'inqtrix.index.resumed') {
      emitProgress()
      callbacksRef.current.onResumed?.(
        collectionId,
        event.job_id,
        snapshot.total_documents ?? 0,
      )
    } else if (event.type === 'inqtrix.index.superseded') {
      callbacksRef.current.onSuperseded?.(collectionId, event.job_id)
    } else if (event.type === 'inqtrix.index.ready_raw_by_user_choice') {
      callbacksRef.current.onReadyRaw?.(collectionId, event.job_id)
    } else if (event.type === 'inqtrix.index.document_completed') {
      // Per-document flip — NOT throttled (each file should land) and it carries
      // no counts, so it never touches the progress bar.
      const documentId = event.data?.document_id
      if (typeof documentId === 'string') {
        callbacksRef.current.onDocumentCompleted(collectionId, documentId)
      }
    } else if (event.type === 'inqtrix.index.document_started') {
      const documentId = event.data?.document_id
      if (typeof documentId === 'string') {
        callbacksRef.current.onDocumentStarted(collectionId, documentId)
      }
    } else if (event.type === 'inqtrix.index.document_progress') {
      const documentId = event.data?.document_id
      const phase = event.data?.phase
      if (typeof documentId === 'string' && typeof phase === 'string') {
        callbacksRef.current.onDocumentProgress(collectionId, documentId, {
          currentBatch: typeof event.data?.current_batch === 'number'
            ? event.data.current_batch
            : 0,
          phase,
          status: 'running',
          totalBatches: typeof event.data?.total_batches === 'number'
            ? event.data.total_batches
            : 0,
        })
      }
    }
  }, [])

  const startStream = useCallback((summary: IndexingJobSummary) => {
    jobCollectionRef.current.set(summary.job_id, summary.collection_id)
    jobSummaryRef.current.set(summary.job_id, summary)
    if (streamsRef.current.has(summary.job_id)) return
    // Hydrate the one current projection before attaching after its cursor.
    // Historical SSE frames are audit history, not new UI transitions.
    callbacksRef.current.onProgress(
      summary.collection_id,
      summary.completed_documents,
      summary.total_documents,
      summary.snapshot.current_document_title,
    )
    for (const documentId of summary.checkpoint.completed_document_ids ?? []) {
      callbacksRef.current.onDocumentCompleted(
        summary.collection_id,
        documentId,
      )
    }
    for (const [documentId, progress] of Object.entries(
      summary.checkpoint.document_progress ?? {},
    )) {
      callbacksRef.current.onDocumentProgress(
        summary.collection_id,
        documentId,
        {
          currentBatch: progress.current_batch ?? 0,
          phase: progress.phase ?? 'preparing',
          status: 'running',
          totalBatches: progress.total_batches ?? 0,
        },
      )
    }
    const disposition = indexingJobDisposition(summary.status)
    if (disposition.kind === 'paused') {
      callbacksRef.current.onPaused?.(summary.collection_id, {
        autoResume: summary.checkpoint.auto_resume ?? null,
        completedDocuments: summary.completed_documents,
        currentBatch: summary.current_batch,
        errorType: summary.error?.type ?? null,
        generationId: summary.generation_id,
        jobId: summary.job_id,
        phase: summary.phase,
        status: disposition.status,
        totalBatches: summary.total_batches,
        totalDocuments: summary.total_documents,
      })
      return
    }
    if (isTerminalIndexingStatus(summary.status)) {
      // Terminal already (resume of a finished job): reflect the exact outcome.
      if (disposition.kind === 'completed') {
        callbacksRef.current.onComplete(summary.collection_id, summary)
      } else if (disposition.kind === 'failed') {
        callbacksRef.current.onError(
          summary.collection_id,
          carriedErrorText(summary.error, readerTexts().fileLibrary.indexingFailed)
            ?? readerTexts().fileLibrary.indexingFailed,
        )
      } else if (disposition.kind === 'cancelled') {
        callbacksRef.current.onCancelled(summary.collection_id)
      } else if (disposition.kind === 'superseded') {
        callbacksRef.current.onSuperseded?.(
          summary.collection_id,
          summary.job_id,
        )
      } else if (disposition.kind === 'ready_raw') {
        callbacksRef.current.onReadyRaw?.(
          summary.collection_id,
          summary.job_id,
        )
      }
      return
    }
    const controller = new AbortController()
    streamsRef.current.set(summary.job_id, controller)
    const collectionId = summary.collection_id
    void streamIndexingJobEvents(summary.events_url, {
      apiKey,
      lastEventId: summary.last_event_sequence == null
        ? undefined
        : String(summary.last_event_sequence),
      signal: controller.signal,
      workspaceId,
      onEvent: handleEvent,
    })
      .catch((error) => {
        if (controller.signal.aborted) return
        callbacksRef.current.onError(collectionId, messageFromError(error))
      })
      .finally(() => {
        streamsRef.current.delete(summary.job_id)
        lastProgressRef.current.delete(summary.job_id)
      })
  }, [apiKey, handleEvent, workspaceId])

  const startReindex = useCallback(async (collectionId: string) => {
    const summary = await startIndexingJob(
      collectionId,
      {},
      { apiKey, workspaceId },
    )
    callbacksRef.current.onStart(
      collectionId,
      summary.job_id,
      summary.total_documents,
      requireActiveStatus(summary.status),
      summary,
    )
    startStream(summary)
    return summary
  }, [apiKey, startStream, workspaceId])

  const cancelReindex = useCallback(async (jobId: string) => {
    const summary = await cancelIndexingJob(jobId, { apiKey, workspaceId })
    // Re-attach to observe the terminal cancelled event (or the queued
    // job's immediate cancellation).
    startStream(summary)
  }, [apiKey, startStream, workspaceId])

  const resumeReindex = useCallback(async (jobId: string) => {
    const summary = await resumeIndexingJob(jobId, { apiKey, workspaceId })
    const disposition = indexingJobDisposition(summary.status)
    if (disposition.kind === 'active') {
      callbacksRef.current.onResumed?.(
        summary.collection_id,
        summary.job_id,
        summary.total_documents,
      )
    }
    startStream(summary)
    return summary
  }, [apiKey, startStream, workspaceId])

  const resumeRawReindex = useCallback(async (jobId: string) => {
    const summary = await resumeIndexingJobWithoutContext(jobId, {
      apiKey,
      workspaceId,
    })
    const disposition = indexingJobDisposition(summary.status)
    if (disposition.kind === 'active') {
      callbacksRef.current.onResumed?.(
        summary.collection_id,
        summary.job_id,
        summary.total_documents,
      )
    }
    startStream(summary)
    return summary
  }, [apiKey, startStream, workspaceId])

  useEffect(() => {
    // Authentication and workspace changes invalidate the authority of every
    // open stream. A user-invalidation refresh does not: aborting here for
    // every refresh can race the terminal frame that triggered the refresh.
    for (const controller of streamsRef.current.values()) controller.abort()
    streamsRef.current.clear()
    jobCollectionRef.current.clear()
    jobSummaryRef.current.clear()
    lastProgressRef.current.clear()
  }, [apiKey, enabled, sessionAuthed, workspaceId])

  useEffect(() => {
    if (!enabled) return undefined
    let ignore = false
    async function hydrate() {
      try {
        const jobs = await listIndexingJobs({ apiKey, workspaceId })
        if (ignore) return
        // Several documents of one collection can wait at once (a provider
        // outage pauses every document in flight). Everything else keeps the
        // one current job per collection.
        const pausedDocuments = new Map<string, IndexingJobSummary[]>()
        for (const summary of jobs) {
          if (
            summary.operation_kind !== 'document_revision'
            || indexingJobDisposition(summary.status).kind !== 'paused'
          ) continue
          pausedDocuments.set(summary.collection_id, [
            ...(pausedDocuments.get(summary.collection_id) ?? []),
            summary,
          ])
        }
        for (const [collectionId, paused] of pausedDocuments) {
          callbacksRef.current.onPausedDocuments(collectionId, paused)
        }
        const others = jobs.filter((summary) => (
          !pausedDocuments.get(summary.collection_id)?.includes(summary)
        ))
        for (const summary of currentIndexingJobs(others)) {
          const disposition = indexingJobDisposition(summary.status)
          if (disposition.kind === 'active' || disposition.kind === 'paused') {
            callbacksRef.current.onStart(
              summary.collection_id,
              summary.job_id,
              summary.total_documents,
              disposition.status,
              summary,
            )
          }
          // Terminal summaries are intentionally projected too. They repair a
          // terminal event lost to navigation, reconnect, or an invalidation
          // arriving at the same instant; reducers make duplicate terminals
          // idempotent.
          startStream(summary)
        }
      } catch (error) {
        // 404 = no indexing surface (knowledge disabled / older backend):
        // expected, leave reindex affordances idle. Any other failure is a
        // real fault and must be visible (No-Silent-Fallbacks).
        if (!ignore && !hasHttpStatus(error, 404)) {
          console.warn('Inqtrix indexing-job resume failed.', error)
        }
      }
    }

    void hydrate()
    return () => {
      ignore = true
    }
  }, [apiKey, enabled, refreshToken, sessionAuthed, startStream, workspaceId])

  useEffect(() => {
    return () => {
      for (const controller of streamsRef.current.values()) controller.abort()
      streamsRef.current.clear()
    }
  }, [])

  return { cancelReindex, resumeRawReindex, resumeReindex, startReindex }
}

function requireActiveStatus(
  status: IndexingJobSummary['status'],
): ActiveIndexingJobStatus {
  const disposition = indexingJobDisposition(status)
  if (disposition.kind !== 'active') {
    throw readerError('indexingNotStarted')
  }
  return disposition.status
}
