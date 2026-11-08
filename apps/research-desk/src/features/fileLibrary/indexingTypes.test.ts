import { describe, expect, it } from 'vitest'
import type { VectorIndexRecord } from '@/features/project/types'
import {
  currentIndexingJobs,
  documentJobsView,
  indexesWithListablePendingMembers,
  indexingJobDisposition,
  listedMembersView,
  newestIndexingJobs,
  newestResumableIndexingJobs,
  splitJobsByKind,
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

describe('documentJobsView', () => {
  const documentJob = (
    jobId: string,
    status: IndexingJobSummary['status'],
    documentId: string,
  ): IndexingJobSummary => ({
    ...summary('kc', jobId, status),
    document_id: documentId,
    operation_kind: 'document_revision',
  })
  const members = [
    { fileId: 'a', serverDocumentId: 'kd_a', state: 'pending' as const },
    { fileId: 'b', serverDocumentId: 'kd_b', state: 'pending' as const },
    { fileId: 'c', state: 'pending' as const },
  ]

  it('reads what is still being worked on, paused and finished', () => {
    const view = documentJobsView(
      [documentJob('j1', 'running', 'kd_a'), documentJob('j2', 'paused_dependency', 'kd_b')],
      members,
    )
    expect(view.active.map((job) => [job.fileId, job.summary.job_id])).toEqual([['a', 'j1']])
    expect(view.paused.map((job) => [job.fileId, job.status])).toEqual([['b', 'paused_dependency']])
    expect(view.embeddedFileIds).toEqual([])
    expect(view.failed).toBeNull()
  })

  it('takes the newest attempt of a document: a retry settles what the last one ended with', () => {
    const view = documentJobsView(
      [documentJob('retry', 'completed', 'kd_a'), documentJob('first', 'failed', 'kd_a')],
      members,
    )
    expect(view.embeddedFileIds).toEqual(['a'])
    expect(view.failed).toBeNull()
  })

  it('names a failure only while the document still owes its vectors', () => {
    const failed = documentJob('j1', 'failed', 'kd_a')
    expect(documentJobsView([failed], members).failed?.job_id).toBe('j1')
    expect(
      documentJobsView([failed], [{ fileId: 'a', serverDocumentId: 'kd_a', state: 'embedded' }]).failed,
    ).toBeNull()
  })

  it('ignores jobs of documents the index does not hold, and collection jobs', () => {
    const view = documentJobsView(
      [documentJob('j1', 'running', 'kd_other'), summary('kc', 'j2', 'running')],
      members,
    )
    expect(view).toEqual({ active: [], embeddedFileIds: [], failed: null, paused: [] })
  })
})

describe('listedMembersView', () => {
  // The collection's document list outlives the job list, which keeps a
  // finished document job an hour and at most ten per collection.
  const listed = {
    byFileId: new Map([['a', 'kd_a'], ['c', 'kd_c'], ['d', 'kd_d_other']]),
    ids: new Set(['kd_a', 'kd_c', 'kd_d_other']),
  }

  it('reads a pending member whose document the collection holds as embedded', () => {
    expect(listedMembersView(
      [{ fileId: 'a', serverDocumentId: 'kd_a', state: 'pending' }],
      listed,
    )).toEqual({ embeddedFileIds: ['a'], serverDocumentIds: {} })
  })

  it('adopts the document of a member that never learned its id', () => {
    expect(listedMembersView([{ fileId: 'c', state: 'pending' }], listed)).toEqual({
      embeddedFileIds: ['c'],
      serverDocumentIds: { c: 'kd_c' },
    })
  })

  it('leaves a member pending whose own document is not listed', () => {
    expect(listedMembersView(
      [
        { fileId: 'b', serverDocumentId: 'kd_b', state: 'pending' },
        // Another document of the same file is a duplicate, not this member.
        { fileId: 'd', serverDocumentId: 'kd_d', state: 'pending' },
      ],
      listed,
    )).toEqual({ embeddedFileIds: [], serverDocumentIds: {} })
  })

  it('touches only pending members', () => {
    expect(listedMembersView(
      [
        { fileId: 'a', serverDocumentId: 'kd_a', state: 'embedded' },
        { fileId: 'c', state: 'skipped' },
      ],
      listed,
    )).toEqual({ embeddedFileIds: [], serverDocumentIds: {} })
  })
})

describe('indexesWithListablePendingMembers', () => {
  const index = (
    id: string,
    patch: Partial<VectorIndexRecord> = {},
  ): VectorIndexRecord => ({
    createdAt: '2026-11-05T00:00:00.000Z',
    dims: 1536,
    history: [],
    id,
    lastError: null,
    members: [{ fileId: 'a', serverDocumentId: 'kd_a', state: 'pending' }],
    model: 'embed',
    serverCollectionId: `kc_${id}`,
    status: 'stale',
    title: id,
    updatedAt: '2026-11-05T00:00:00.000Z',
    ...patch,
  }) as VectorIndexRecord

  it('lists each server-backed index with pending members once', () => {
    const indexes = {
      done: index('done'),
      local: index('local', { serverCollectionId: undefined }),
      ready: index('ready', { members: [{ fileId: 'a', serverDocumentId: 'kd_a', state: 'embedded' }], status: 'ready' }),
      stale: index('stale'),
    }

    expect(
      indexesWithListablePendingMembers(indexes, new Set(['done'])).map((item) => item.id),
    ).toEqual(['stale'])
  })

  it('leaves an index to the run or deletion that owns it', () => {
    const indexes = {
      deleting: index('deleting', { status: 'deleting' }),
      failedDelete: index('failedDelete', { status: 'delete_failed' }),
      indexing: index('indexing', { status: 'indexing' }),
    }

    expect(indexesWithListablePendingMembers(indexes, new Set())).toEqual([])
  })
})

describe('splitJobsByKind', () => {
  it('keeps the document jobs of a collection together, newest first', () => {
    const newer = { ...summary('kc', 'j1', 'running'), operation_kind: 'document_revision' as const }
    const older = { ...summary('kc', 'j2', 'failed'), operation_kind: 'document_revision' as const }
    const generation = summary('kc', 'j3', 'running')

    const { documentJobs, generations } = splitJobsByKind([newer, generation, older])

    expect(documentJobs.get('kc')).toEqual([newer, older])
    expect(generations).toEqual([generation])
  })
})
