import { describe, expect, it } from 'vitest'
import {
  currentIndexingJobs,
  indexingJobDisposition,
  newestIndexingJobs,
  newestResumableIndexingJobs,
  type IndexingJobSummary,
  type IndexingJobStatus,
} from './indexingTypes'

function summary(
  collectionId: string,
  jobId: string,
  status: IndexingJobStatus,
): IndexingJobSummary {
  return {
    checkpoint: {},
    collection_id: collectionId,
    collection_name: collectionId,
    completed_documents: 0,
    created_at: 0,
    current_batch: 0,
    elapsed_seconds: null,
    embedding_model: 'embed',
    operation_kind: 'collection_generation',
    document_id: null,
    revision_id: null,
    error: null,
    events_url: `/events/${jobId}`,
    fence_token: null,
    finished_at: null,
    generation_id: null,
    index_id: null,
    job_id: jobId,
    percent: 0,
    phase: status,
    queue_position: null,
    snapshot: {},
    started_at: null,
    status,
    total_batches: 0,
    total_documents: 0,
    workspace_id: null,
  }
}

describe('indexingJobDisposition', () => {
  it('keeps pauses distinct from active execution', () => {
    expect(indexingJobDisposition('paused_dependency')).toEqual({
      kind: 'paused',
      status: 'paused_dependency',
    })
    expect(indexingJobDisposition('paused_validation')).toEqual({
      kind: 'paused',
      status: 'paused_validation',
    })
  })

  it('does not collapse superseded or explicit raw completion into running', () => {
    expect(indexingJobDisposition('superseded')).toEqual({ kind: 'superseded' })
    expect(indexingJobDisposition('ready_raw_by_user_choice')).toEqual({
      kind: 'ready_raw',
    })
  })

  it('fails visibly for an unknown runtime wire value', () => {
    expect(() => indexingJobDisposition('future_status' as IndexingJobStatus))
      .toThrow('Unknown indexing job status')
  })

  it('hydrates only the newest resumable job and never replays terminal history', () => {
    const current = summary('collection-a', 'job-current', 'paused_dependency')
    const oldFailure = summary('collection-a', 'job-old', 'failed')
    const latestTerminal = summary('collection-b', 'job-done', 'completed')
    const impossibleOlderActive = summary('collection-b', 'job-stale', 'running')

    expect(newestResumableIndexingJobs([
      current,
      oldFailure,
      latestTerminal,
      impossibleOlderActive,
    ])).toEqual([current, impossibleOlderActive])
  })

  it('does not let a completed sibling hide a paused document revision', () => {
    const completed = summary('collection-a', 'job-completed', 'completed')
    const paused = {
      ...summary('collection-a', 'job-paused', 'paused_dependency'),
      operation_kind: 'document_revision' as const,
      document_id: 'kd-paused',
    }

    expect(currentIndexingJobs([completed, paused])).toEqual([paused])
  })

  it('keeps the newest terminal projection for reconnect reconciliation', () => {
    const current = summary('collection-a', 'job-current', 'running')
    const olderFailure = summary('collection-a', 'job-old', 'failed')
    const latestTerminal = summary('collection-b', 'job-done', 'completed')
    const impossibleOlderActive = summary('collection-b', 'job-stale', 'running')

    expect(newestIndexingJobs([
      current,
      olderFailure,
      latestTerminal,
      impossibleOlderActive,
    ])).toEqual([current, latestTerminal])
  })
})
