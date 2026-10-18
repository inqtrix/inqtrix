/**
 * Wire types for the background reindex-job API
 * (``/v1/knowledge/...reindex`` + ``/v1/knowledge/indexing-jobs``).
 *
 * Mirrors the research-run envelope (summary + SSE event) so the
 * indexing hook can reuse the same streaming/resume machinery. The
 * shapes match the server's ``build_indexing_job_summary`` and the
 * ``inqtrix.index.*`` event payloads byte-for-byte.
 */

export type IndexingJobStatus =
  | 'queued'
  | 'running'
  | 'cancelling'
  | 'paused_dependency'
  | 'paused_validation'
  | 'superseded'
  | 'ready_raw_by_user_choice'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'expired'

/** Live progress carried on every event and the summary. */
export type IndexingJobSnapshot = {
  /** Number of document pipelines currently active inside one collection job. */
  active_documents?: number
  completed_documents?: number
  current_document_title?: string
  /** 0..1, mirrors the research-run snapshot's progress reader. */
  progress_estimate?: number
  phase?: string
  current_batch?: number
  total_batches?: number
  total_documents?: number
}

/** The server's bounded plan to resume a dependency pause on its own. */
export type AutoResumePlan = {
  /** Automatic resumes the current streak has used. */
  attempts: number
  max: number
  /** Epoch seconds of the next automatic resume; null once it ran or stopped. */
  next_at: number | null
  stopped: 'exhausted' | 'access' | 'unavailable' | null
}

export type IndexingJobSummary = {
  collection_id: string
  collection_name: string
  completed_documents: number
  created_at: number
  elapsed_seconds: number | null
  embedding_model: string
  operation_kind: 'collection_generation' | 'document_revision'
  document_id: string | null
  revision_id: string | null
  error: { message: string; type: string } | null
  phase: string
  current_batch: number
  total_batches: number
  checkpoint: {
    auto_resume?: AutoResumePlan
    completed_document_ids?: string[]
    contextualization?: {
      active_documents?: number
      document_id?: string | null
      completed_batches?: number
      total_batches?: number
    }
    document_progress?: Record<string, {
      current_batch?: number
      phase?: string
      total_batches?: number
    }>
  }
  generation_id: string | null
  fence_token: string | null
  events_url: string
  finished_at: number | null
  /** Optional caller correlation only. Authorization and UI ownership are
   * always derived from `collection_id`. */
  index_id: string | null
  job_id: string
  /** Durable cursor represented by this summary. Reattachment resumes after
   * it, so historical row transitions never replay as fresh animation. */
  last_event_sequence?: number
  percent: number
  /** 1-based FIFO slot while still queued; null once running/terminal. */
  queue_position: number | null
  snapshot: IndexingJobSnapshot
  started_at: number | null
  status: IndexingJobStatus
  total_documents: number
  workspace_id: string | null
}

/** One SSE frame; ``type`` is ``inqtrix.index.{started,progress,...}``. */
export type IndexingJobEvent = {
  created_at: number
  data: Record<string, unknown>
  job_id: string
  sequence: number
  type: string
}

export const TERMINAL_INDEXING_STATUSES: ReadonlySet<IndexingJobStatus> =
  new Set([
    'completed',
    'failed',
    'cancelled',
    'expired',
    'superseded',
    'ready_raw_by_user_choice',
  ])

export type ActiveIndexingJobStatus = 'cancelling' | 'queued' | 'running'
export type PausedIndexingJobStatus = 'paused_dependency' | 'paused_validation'
export type IndexingJobDisposition =
  | { kind: 'active'; status: ActiveIndexingJobStatus }
  | { kind: 'paused'; status: PausedIndexingJobStatus }
  | { kind: 'completed' }
  | { kind: 'failed' }
  | { kind: 'cancelled' }
  | { kind: 'superseded' }
  | { kind: 'ready_raw' }

/** Exhaustive wire-state projection. No unknown/non-active status becomes running. */
export function indexingJobDisposition(status: IndexingJobStatus): IndexingJobDisposition {
  switch (status) {
    case 'queued':
    case 'running':
    case 'cancelling':
      return { kind: 'active', status }
    case 'paused_dependency':
    case 'paused_validation':
      return { kind: 'paused', status }
    case 'completed':
      return { kind: 'completed' }
    case 'failed':
    case 'expired':
      return { kind: 'failed' }
    case 'cancelled':
      return { kind: 'cancelled' }
    case 'superseded':
      return { kind: 'superseded' }
    case 'ready_raw_by_user_choice':
      return { kind: 'ready_raw' }
    default: {
      const exhaustive: never = status
      throw new Error(`Unknown indexing job status: ${String(exhaustive)}`)
    }
  }
}

export function isTerminalIndexingStatus(status: IndexingJobStatus): boolean {
  return TERMINAL_INDEXING_STATUSES.has(status)
}

/** Select the newest job per collection from the API's newest-first
 * active-and-history feed. The newest terminal row is part of the current
 * projection: it lets a remounted or briefly disconnected client reconcile a
 * terminal transition without replaying the job's historical events. */
export function newestIndexingJobs(
  jobs: readonly IndexingJobSummary[],
): IndexingJobSummary[] {
  const seenCollections = new Set<string>()
  const newest: IndexingJobSummary[] = []
  for (const job of jobs) {
    if (seenCollections.has(job.collection_id)) continue
    seenCollections.add(job.collection_id)
    newest.push(job)
  }
  return newest
}

/** Select at most the newest resumable job per collection. */
export function newestResumableIndexingJobs(
  jobs: readonly IndexingJobSummary[],
): IndexingJobSummary[] {
  return newestIndexingJobs(jobs.filter((job) => {
    const disposition = indexingJobDisposition(job.status)
    return disposition.kind === 'active' || disposition.kind === 'paused'
  }))
}

/** Current recoverable projection per collection. A newer terminal document
 * job must not hide an older sibling that is still paused: document revisions
 * may legitimately overlap inside one first-build collection. */
export function currentIndexingJobs(
  jobs: readonly IndexingJobSummary[],
): IndexingJobSummary[] {
  const chosen = new Map<string, IndexingJobSummary>()
  for (const job of jobs) {
    const existing = chosen.get(job.collection_id)
    if (!existing) {
      chosen.set(job.collection_id, job)
      continue
    }
    const existingDisposition = indexingJobDisposition(existing.status)
    const disposition = indexingJobDisposition(job.status)
    if (
      existingDisposition.kind !== 'active'
      && existingDisposition.kind !== 'paused'
      && (disposition.kind === 'active' || disposition.kind === 'paused')
    ) {
      chosen.set(job.collection_id, job)
    }
  }
  const selectedIds = new Set([...chosen.values()].map((job) => job.job_id))
  return jobs.filter((job) => selectedIds.has(job.job_id))
}
