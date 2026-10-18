import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { FileAssetRecord, VectorIndexRecord } from '@/features/project/types'
import {
  createVectorIndexCollectionOnServer,
  ingestNewVectorIndexMembers,
  KnowledgeReindexPartialError,
} from './knowledgeSync'

const mocks = vi.hoisted(() => ({
  addKnowledgeDocument: vi.fn(),
  cancelIndexingJob: vi.fn(),
  createKnowledgeCollection: vi.fn(),
  deleteKnowledgeCollection: vi.fn(),
  fetchKnowledgeDocumentText: vi.fn(),
  getAssetDeletionOperation: vi.fn(),
  getIndexingJob: vi.fn(),
  ingestKnowledgeFile: vi.fn(),
  listKnowledgeDocuments: vi.fn(),
  jobs: new Map<string, Record<string, unknown>>(),
  resumeIndexingJob: vi.fn(),
  resumeIndexingJobWithoutContext: vi.fn(),
  startDocumentRevisionJob: vi.fn(),
  streamIndexingJobEvents: vi.fn(),
}))

vi.mock('@/api/inqtrixClient', () => ({
  addKnowledgeDocument: mocks.addKnowledgeDocument,
  cancelIndexingJob: mocks.cancelIndexingJob,
  createKnowledgeCollection: mocks.createKnowledgeCollection,
  deleteKnowledgeCollection: mocks.deleteKnowledgeCollection,
  fetchKnowledgeDocumentText: mocks.fetchKnowledgeDocumentText,
  getAssetDeletionOperation: mocks.getAssetDeletionOperation,
  getIndexingJob: mocks.getIndexingJob,
  ingestKnowledgeFile: mocks.ingestKnowledgeFile,
  listKnowledgeDocuments: mocks.listKnowledgeDocuments,
  resumeIndexingJob: mocks.resumeIndexingJob,
  resumeIndexingJobWithoutContext: mocks.resumeIndexingJobWithoutContext,
  startDocumentRevisionJob: mocks.startDocumentRevisionJob,
  streamIndexingJobEvents: mocks.streamIndexingJobEvents,
  hasHttpStatus: (error: unknown, status: number) =>
    error instanceof Error && (error as Error & { status?: number }).status === status,
}))

function makeIndex(overrides: Partial<VectorIndexRecord> = {}): VectorIndexRecord {
  return {
    createdAt: '2026-06-10T00:00:00.000Z',
    dims: 1536,
    handle: 'vertraege',
    id: 'vector-index-1',
    members: [{ fileId: 'file-1', state: 'pending' }],
    model: 'text-embedding-3-small',
    status: 'stale',
    title: 'Verträge',
    updatedAt: '2026-06-10T00:00:00.000Z',
    ...overrides,
  }
}

function makeAsset(overrides: Partial<FileAssetRecord> = {}): FileAssetRecord {
  return {
    createdAt: '2026-06-10T00:00:00.000Z',
    extractedText: 'Die Haftung ist begrenzt.',
    fileName: 'vertrag.pdf',
    groupId: null,
    id: 'file-1',
    label: 'rahmenvertrag',
    mimeType: 'application/pdf',
    origin: 'library',
    pageCount: 3,
    parseStatus: 'parsed',
    parseWarning: null,
    sectionId: 'section-1',
    sizeBytes: 1000,
    serverFileId: 'srv-file-1',
    textTruncated: false,
    title: 'Rahmenvertrag.pdf',
    updatedAt: '2026-06-10T00:00:00.000Z',
    ...overrides,
  }
}

/** One document-revision job summary as the server returns it. */
function jobSummary(
  collectionId: string,
  jobId: string,
  documentId: string,
  status: string,
  overrides: Record<string, unknown> = {},
) {
  return {
    // Every summary carries a checkpoint, as on the wire.
    checkpoint: {},
    collection_id: collectionId,
    completed_documents: status === 'completed' ? 1 : 0,
    current_batch: 4,
    document_id: documentId,
    error: null,
    events_url: `/events/${jobId}`,
    job_id: jobId,
    operation_kind: 'document_revision',
    phase: 'contextualization',
    revision_id: `rev_${jobId}`,
    status,
    total_batches: 11,
    total_documents: 1,
    ...overrides,
  }
}

beforeEach(() => {
  mocks.addKnowledgeDocument.mockReset()
  mocks.cancelIndexingJob.mockReset()
  mocks.createKnowledgeCollection.mockReset()
  mocks.deleteKnowledgeCollection.mockReset()
  mocks.fetchKnowledgeDocumentText.mockReset()
  mocks.getAssetDeletionOperation.mockReset()
  mocks.getIndexingJob.mockReset()
  mocks.ingestKnowledgeFile.mockReset()
  mocks.listKnowledgeDocuments.mockReset()
  mocks.resumeIndexingJob.mockReset()
  mocks.resumeIndexingJobWithoutContext.mockReset()
  mocks.startDocumentRevisionJob.mockReset()
  mocks.streamIndexingJobEvents.mockReset()
  mocks.jobs.clear()
  mocks.listKnowledgeDocuments.mockResolvedValue({ data: [], next_cursor: null })
  mocks.createKnowledgeCollection.mockResolvedValue({ id: 'kc_fresh' })
  mocks.addKnowledgeDocument.mockResolvedValue({ id: 'kd_1' })
  mocks.ingestKnowledgeFile.mockResolvedValue({ id: 'kd_file' })
  mocks.fetchKnowledgeDocumentText.mockResolvedValue({ id: 'kd_file', text: 'MARKITDOWN BODY' })
  mocks.startDocumentRevisionJob.mockImplementation(
    async (collectionId: string, payload: { metadata?: Record<string, unknown>; text: string; title: string }, options: unknown) => {
      const document = await mocks.addKnowledgeDocument(collectionId, payload, options)
      const jobId = `ix_${mocks.jobs.size + 1}`
      const summary = {
        collection_id: collectionId,
        document_id: document.id,
        error: null,
        events_url: `/events/${jobId}`,
        job_id: jobId,
        operation_kind: 'document_revision',
        revision_id: `rev_${mocks.jobs.size + 1}`,
        status: 'completed',
      }
      mocks.jobs.set(jobId, summary)
      return summary
    },
  )
  mocks.getIndexingJob.mockImplementation(async (jobId: string) => mocks.jobs.get(jobId))
  mocks.cancelIndexingJob.mockImplementation(async (jobId: string) => mocks.jobs.get(jobId))
  mocks.streamIndexingJobEvents.mockResolvedValue(undefined)
  mocks.deleteKnowledgeCollection.mockImplementation(async (_id: string, options?: { signal?: AbortSignal }) => {
    // Mirror fetch: a request carrying an already-aborted signal never leaves.
    if (options?.signal?.aborted) {
      throw new DOMException('The operation was aborted.', 'AbortError')
    }
    return {
      asset_ids: [],
      attempt: 1,
      completed_items: 1,
      created_at: 1,
      error: null,
      finished_at: 2,
      operation_id: 'del_collection',
      retryable: false,
      stage: 'deleted',
      started_at: 1,
      status: 'deleted',
      target_id: 'kc_fresh',
      target_kind: 'knowledge_collection',
      total_items: 1,
    }
  })
  mocks.getAssetDeletionOperation.mockImplementation(async () => (
    mocks.deleteKnowledgeCollection.mock.results.at(-1)?.value
  ))
})

afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('createVectorIndexCollectionOnServer', () => {
  it('uses SSE first and reconnects with backoff after transport loss', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const queued = {
      collection_id: 'kc_fresh',
      completed_documents: 0,
      current_batch: 0,
      document_id: 'kd_async',
      error: null,
      events_url: '/events/ix_async',
      job_id: 'ix_async',
      operation_kind: 'document_revision',
      phase: 'queued',
      revision_id: 'rev_async',
      status: 'queued',
    }
    mocks.startDocumentRevisionJob.mockResolvedValueOnce(queued)
    mocks.streamIndexingJobEvents
      .mockRejectedValueOnce(new Error('stream disconnected'))
      .mockResolvedValueOnce(undefined)
    mocks.getIndexingJob
      .mockResolvedValueOnce({ ...queued, phase: 'embedding', status: 'running' })
      .mockResolvedValueOnce({ ...queued, phase: 'completed', status: 'completed' })

    const pending = createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset()],
      {},
    )
    await vi.advanceTimersByTimeAsync(0)
    expect(mocks.streamIndexingJobEvents).toHaveBeenCalledTimes(1)
    expect(mocks.getIndexingJob).toHaveBeenCalledTimes(1)

    // Minimum jittered reconnect is 187.5 ms; there is no 75 ms poll loop.
    await vi.advanceTimersByTimeAsync(150)
    expect(mocks.getIndexingJob).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(50)
    await expect(pending).resolves.toMatchObject({ uploadedDocuments: 1 })
    expect(mocks.streamIndexingJobEvents).toHaveBeenCalledTimes(2)
    expect(mocks.getIndexingJob).toHaveBeenCalledTimes(2)
  })

  it('observes server cancellation with bounded polling after the caller aborts', async () => {
    vi.useFakeTimers()
    vi.spyOn(Math, 'random').mockReturnValue(0)
    const controller = new AbortController()
    const queued = {
      collection_id: 'kc_fresh',
      completed_documents: 0,
      current_batch: 0,
      document_id: 'kd_async',
      error: null,
      events_url: '/events/ix_async',
      job_id: 'ix_async',
      operation_kind: 'document_revision',
      phase: 'embedding',
      revision_id: 'rev_async',
      status: 'queued',
    }
    mocks.startDocumentRevisionJob.mockResolvedValueOnce(queued)
    mocks.streamIndexingJobEvents.mockImplementationOnce(
      async (_url: string, options: { signal?: AbortSignal }) => new Promise<void>(
        (_resolve, reject) => options.signal?.addEventListener(
          'abort',
          () => reject(new DOMException('aborted', 'AbortError')),
          { once: true },
        ),
      ),
    )
    mocks.cancelIndexingJob.mockResolvedValueOnce({
      ...queued,
      status: 'cancelling',
    })
    mocks.getIndexingJob.mockResolvedValueOnce({
      ...queued,
      phase: 'cancelled',
      status: 'cancelled',
    })

    const pending = createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset()],
      { signal: controller.signal },
    )
    await vi.advanceTimersByTimeAsync(0)
    expect(mocks.streamIndexingJobEvents).toHaveBeenCalledTimes(1)
    controller.abort()
    await vi.advanceTimersByTimeAsync(0)
    expect(mocks.cancelIndexingJob).toHaveBeenCalledTimes(1)
    expect(mocks.getIndexingJob).not.toHaveBeenCalled()

    await vi.advanceTimersByTimeAsync(150)
    expect(mocks.getIndexingJob).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(50)
    await expect(pending).resolves.toMatchObject({ cancelled: true })
    expect(mocks.getIndexingJob).toHaveBeenCalledTimes(1)
  })

  it('creates a collection with the index model and uploads member texts', async () => {
    const result = await createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset()],
      { apiKey: 'key', workspaceId: 'ws_test_123' },
    )

    expect(mocks.createKnowledgeCollection).toHaveBeenCalledWith(
      { embeddingModel: 'text-embedding-3-small', name: 'Verträge' },
      { apiKey: 'key', workspaceId: 'ws_test_123' },
    )
    expect(mocks.startDocumentRevisionJob).toHaveBeenCalledWith(
      'kc_fresh',
      {
        assetId: 'file-1',
        metadata: { fileId: 'file-1', fileName: 'vertrag.pdf', file_id: 'srv-file-1' },
        title: 'Rahmenvertrag.pdf',
      },
      { apiKey: 'key', signal: undefined, workspaceId: 'ws_test_123' },
    )
    expect(result).toEqual({
      cancelled: false,
      collectionId: 'kc_fresh',
      serverCollectionModel: 'text-embedding-3-small',
      embeddedFileIds: ['file-1'],
      skippedFileIds: [],
      skippedFiles: [],
      uploadedDocuments: 1,
      serverDocumentIds: { 'file-1': 'kd_1' },
    })
  })

  it('refuses to replace an existing collection', async () => {
    await expect(createVectorIndexCollectionOnServer(
      makeIndex({ serverCollectionId: 'kc_existing' }),
      [makeAsset()],
      {},
    )).rejects.toThrow('requires an index without a server collection')

    expect(mocks.deleteKnowledgeCollection).not.toHaveBeenCalled()
    expect(mocks.createKnowledgeCollection).not.toHaveBeenCalled()
  })

  it('reports members without extracted text instead of dropping them silently', async () => {
    const result = await createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset(), makeAsset({ extractedText: '  ', id: 'file-2', label: 'Scan ohne Text', serverFileId: null })],
      {},
    )
    expect(result.skippedFiles).toEqual(['Scan ohne Text'])
    expect(result.uploadedDocuments).toBe(1)
  })

  it('creates an empty collection and reports all-textless members as skipped', async () => {
    const result = await createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset({ extractedText: '', label: 'Scan ohne Text', serverFileId: null })],
      {},
    )

    expect(mocks.deleteKnowledgeCollection).not.toHaveBeenCalled()
    expect(mocks.createKnowledgeCollection).toHaveBeenCalledWith(
      { embeddingModel: 'text-embedding-3-small', name: 'Verträge' },
      {},
    )
    expect(mocks.startDocumentRevisionJob).not.toHaveBeenCalled()
    expect(result).toEqual({
      cancelled: false,
      collectionId: 'kc_fresh',
      serverCollectionModel: 'text-embedding-3-small',
      embeddedFileIds: [],
      skippedFileIds: ['file-1'],
      skippedFiles: ['Scan ohne Text'],
      uploadedDocuments: 0,
      serverDocumentIds: {},
    })
  })

  it('ingests an uploaded file through its stable asset identity', async () => {
    const result = await createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset({ serverFileId: 'srv-file-9' })],
      { useFileIngestion: true, workspaceId: 'ws_test_123' },
    )

    expect(mocks.startDocumentRevisionJob).toHaveBeenCalledWith(
      'kc_fresh',
      expect.objectContaining({ assetId: 'file-1' }),
      expect.objectContaining({ signal: undefined }),
    )
    expect(result.uploadedDocuments).toBe(1)
  })

  it('does not bypass asset authority when the display text is already server-parsed', async () => {
    const result = await createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset({ serverFileId: 'srv-file-9', parserId: 'markitdown' })],
      { useFileIngestion: true, workspaceId: 'ws_test_123' },
    )
    // The upload already produced the canonical server text. The revision
    // names the asset; it carries no text of its own for the server to trust.
    expect(mocks.startDocumentRevisionJob).toHaveBeenCalledWith(
      'kc_fresh',
      {
        assetId: 'file-1',
        metadata: { fileId: 'file-1', fileName: 'vertrag.pdf', file_id: 'srv-file-9' },
        title: 'Rahmenvertrag.pdf',
      },
      { signal: undefined, useFileIngestion: true, workspaceId: 'ws_test_123' },
    )
    expect(result.uploadedDocuments).toBe(1)
  })

  it('does not promote client-parsed display text to source authority', async () => {
    await createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset({ serverFileId: 'srv-file-9', parserId: 'client' })],
      { useFileIngestion: true },
    )
    const payload = mocks.startDocumentRevisionJob.mock.calls[0]?.[1]
    expect(payload).toMatchObject({ assetId: 'file-1' })
    expect(payload).not.toHaveProperty('text')
  })

  it('fails visibly when canonical server preparation is unavailable', async () => {
    mocks.startDocumentRevisionJob.mockRejectedValueOnce(
      new Error('canonical source preparation unavailable'),
    )
    await expect(createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset({ serverFileId: 'srv-file-9', parserId: 'client' })],
      { useFileIngestion: true },
    )).rejects.toThrow('canonical source preparation unavailable')
  })

  it('counts a text-less asset as uploadable when file ingestion covers it', async () => {
    const result = await createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset({ extractedText: '', serverFileId: 'srv-file-9' })],
      { useFileIngestion: true },
    )
    expect(result.skippedFiles).toEqual([])
    expect(mocks.startDocumentRevisionJob).toHaveBeenCalledTimes(1)
  })

  it('uses asset authority even when the legacy capability hint is absent', async () => {
    await createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset({ serverFileId: 'srv-file-9' })],
      {},
    )
    expect(mocks.startDocumentRevisionJob).toHaveBeenCalledTimes(1)
    const payload = mocks.startDocumentRevisionJob.mock.calls[0]?.[1]
    expect(payload).toMatchObject({ assetId: 'file-1' })
    expect(payload).not.toHaveProperty('text')
  })

  it('keeps an asset without a server source pending instead of indexing browser text', async () => {
    const result = await createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset({ serverFileId: null })],
      { useFileIngestion: true },
    )
    expect(mocks.startDocumentRevisionJob).not.toHaveBeenCalled()
    expect(result.skippedFileIds).toEqual(['file-1'])
  })

  it('preserves the collection identity when a first-build member fails', async () => {
    mocks.addKnowledgeDocument.mockRejectedValueOnce(new Error('embedding backend down'))

    const error = await createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset()],
      {},
    ).catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(KnowledgeReindexPartialError)
    expect(error).toMatchObject({
      message: 'embedding backend down',
      result: {
        collectionId: 'kc_fresh',
        embeddedFileIds: [],
      },
    })
    expect(mocks.deleteKnowledgeCollection).not.toHaveBeenCalled()
  })

  it('returns the resumable document job together with confirmed siblings', async () => {
    mocks.startDocumentRevisionJob.mockImplementation(
      async (collectionId: string, payload: { title: string }) => {
        if (payload.title === 'paused.pdf') {
          return {
            checkpoint: {},
            collection_id: collectionId,
            completed_documents: 0,
            current_batch: 24,
            document_id: 'kd_paused',
            error: { message: 'Provider rate limit', type: 'provider_rate_limited' },
            events_url: '/events/ix_paused',
            job_id: 'ix_paused',
            operation_kind: 'document_revision',
            phase: 'embedding',
            revision_id: 'rev_paused',
            status: 'paused_dependency',
            total_batches: 24,
            total_documents: 1,
          }
        }
        return {
          collection_id: collectionId,
          completed_documents: 1,
          current_batch: 0,
          document_id: 'kd_complete',
          error: null,
          events_url: '/events/ix_complete',
          job_id: 'ix_complete',
          operation_kind: 'document_revision',
          phase: 'completed',
          revision_id: 'rev_complete',
          status: 'completed',
          total_batches: 0,
          total_documents: 1,
        }
      },
    )

    const error = await createVectorIndexCollectionOnServer(
      makeIndex(),
      [
        makeAsset({ id: 'file-complete', title: 'complete.pdf' }),
        makeAsset({ id: 'file-paused', title: 'paused.pdf' }),
      ],
      {},
    ).catch((caught: unknown) => caught)

    expect(error).toBeInstanceOf(KnowledgeReindexPartialError)
    expect(error).toMatchObject({
      pausedJobs: [{
        fileId: 'file-paused',
        status: 'paused_dependency',
      }],
      result: {
        collectionId: 'kc_fresh',
        embeddedFileIds: ['file-complete'],
        serverDocumentIds: {
          'file-complete': 'kd_complete',
          'file-paused': 'kd_paused',
        },
      },
    })
    expect(mocks.deleteKnowledgeCollection).not.toHaveBeenCalled()
  })
})

describe('ingestNewVectorIndexMembers', () => {
  it('ingests only the new members into the existing collection (no rebuild)', async () => {
    const result = await ingestNewVectorIndexMembers(
      makeIndex({
        serverCollectionId: 'kc_live',
        serverCollectionModel: 'text-embedding-3-small',
      }),
      [makeAsset({ id: 'file-2', fileName: 'neu.pdf', title: 'Neu.pdf' })],
      { apiKey: 'key', workspaceId: 'ws_test_123' },
    )

    // The existing collection is reused — never deleted or recreated.
    expect(mocks.deleteKnowledgeCollection).not.toHaveBeenCalled()
    expect(mocks.createKnowledgeCollection).not.toHaveBeenCalled()
    expect(mocks.addKnowledgeDocument).toHaveBeenCalledWith(
      'kc_live',
      expect.objectContaining({
        assetId: 'file-2',
        metadata: expect.objectContaining({ fileId: 'file-2', fileName: 'neu.pdf' }),
      }),
      { apiKey: 'key', signal: undefined, workspaceId: 'ws_test_123' },
    )
    expect(result.collectionId).toBe('kc_live')
    expect(result.embeddedFileIds).toEqual(['file-2'])
    expect(result.skippedFileIds).toEqual([])
    // The backend document id is returned per member so removal can delete it.
    expect(result.serverDocumentIds).toEqual({ 'file-2': 'kd_1' })
  })

  it('reports text-less members as skipped instead of dropping them', async () => {
    const result = await ingestNewVectorIndexMembers(
      makeIndex({
        serverCollectionId: 'kc_live',
        serverCollectionModel: 'text-embedding-3-small',
      }),
      [makeAsset({ id: 'file-3', extractedText: '   ', label: 'Scan ohne Text', serverFileId: null })],
      {},
    )
    expect(mocks.addKnowledgeDocument).not.toHaveBeenCalled()
    expect(result.embeddedFileIds).toEqual([])
    expect(result.skippedFileIds).toEqual(['file-3'])
    expect(result.skippedFiles).toEqual(['Scan ohne Text'])
  })

  it('keeps a cancelled run\'s embedded documents and its collection', async () => {
    const controller = new AbortController()
    mocks.addKnowledgeDocument.mockImplementation(async () => {
      // The first document lands, then the user cancels mid-run.
      controller.abort()
      return { id: 'kd_first' }
    })
    const result = await createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset({ id: 'file-a' }), makeAsset({ id: 'file-b' }), makeAsset({ id: 'file-c' })],
      { signal: controller.signal },
    )
    expect(result.cancelled).toBe(true)
    expect(result.collectionId).toBe('kc_fresh')
    expect(result.embeddedFileIds.length).toBeGreaterThan(0)
    // What embedded is real — the collection must survive so the index can
    // adopt it and a later run resumes instead of starting over.
    expect(mocks.deleteKnowledgeCollection).not.toHaveBeenCalled()
  })

  it('removes the empty collection when a build is cancelled before anything lands', async () => {
    const controller = new AbortController()
    controller.abort()
    const result = await createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset({ id: 'file-a' })],
      { signal: controller.signal },
    )
    expect(result.cancelled).toBe(true)
    expect(result.collectionId).toBeNull()
    expect(result.embeddedFileIds).toEqual([])
    expect(mocks.addKnowledgeDocument).not.toHaveBeenCalled()
    // No phantom collection is left behind for the user to wonder about. The
    // cleanup must NOT ride the run's own (already aborted) signal, or the
    // request would never be sent and the collection would survive.
    expect(mocks.deleteKnowledgeCollection).toHaveBeenCalledWith(
      'kc_fresh',
      expect.objectContaining({ signal: undefined }),
    )
    expect(mocks.deleteKnowledgeCollection).toHaveResolved()
  })

  it('retains the collection identity when cancelled-build cleanup is unresolved', async () => {
    const controller = new AbortController()
    controller.abort()
    mocks.deleteKnowledgeCollection.mockResolvedValueOnce({
      asset_ids: [],
      attempt: 1,
      completed_items: 0,
      created_at: 1,
      error: { message: 'object store unavailable', type: 'dependency_error' },
      finished_at: 2,
      operation_id: 'del_failed',
      retryable: true,
      stage: 'blob_delete',
      started_at: 1,
      status: 'failed',
      target_id: 'kc_fresh',
      target_kind: 'knowledge_collection',
      total_items: 1,
    })

    const result = await createVectorIndexCollectionOnServer(
      makeIndex(),
      [makeAsset({ id: 'file-a' })],
      { signal: controller.signal },
    )

    expect(result.cancelled).toBe(true)
    expect(result.collectionId).toBe('kc_fresh')
    expect(result.embeddedFileIds).toEqual([])
  })

  it('reports a failed member so it stops counting as work in progress', async () => {
    const failed: string[] = []
    mocks.addKnowledgeDocument.mockImplementation(async (_id: string, payload: { title: string }) => {
      if (payload.title === 'boom.pdf') throw new Error('500')
      return { id: 'kd_ok' }
    })
    await expect(
      createVectorIndexCollectionOnServer(
        makeIndex(),
        [makeAsset({ id: 'file-a', title: 'boom.pdf' }), makeAsset({ id: 'file-b' })],
        {},
        { onMemberFailed: (event) => failed.push(event.fileId) },
      ),
    ).rejects.toThrow('500')
    expect(failed).toEqual(['file-a'])
  })

  it('fails loudly without an existing server collection', async () => {
    await expect(
      ingestNewVectorIndexMembers(makeIndex({ serverCollectionId: null }), [makeAsset()], {}),
    ).rejects.toThrow('requires an existing server collection')
  })

  it('reports server-confirmed per-member progress via onMemberDone', async () => {
    const events: Array<{ fileId: string; done: number; total: number; embedded: boolean }> = []
    await ingestNewVectorIndexMembers(
      makeIndex({
        serverCollectionId: 'kc_live',
        serverCollectionModel: 'text-embedding-3-small',
      }),
      [
        makeAsset({ id: 'file-a' }),
        makeAsset({ id: 'file-b', extractedText: '   ', label: 'Scan', serverFileId: null }),
        makeAsset({ id: 'file-c' }),
      ],
      {},
      { onMemberDone: (event) => events.push(event) },
    )
    // One callback per member with the REAL outcome (the text-less 'file-b' is
    // reported embedded:false, not silently dropped). Members ingest with
    // bounded parallelism, so completion ORDER is not part of the contract —
    // the counter is: every member reports once, counting 1..total.
    expect(events).toHaveLength(3)
    expect(events.every((event) => event.total === 3)).toBe(true)
    expect([...events.map((event) => event.done)].sort()).toEqual([1, 2, 3])
    expect(
      Object.fromEntries(events.map((event) => [event.fileId, event.embedded])),
    ).toEqual({ 'file-a': true, 'file-b': false, 'file-c': true })
  })

  it('announces each member before its ingest so the UI can name it', async () => {
    const started: Array<{ done: number; fileId: string; total: number }> = []
    await ingestNewVectorIndexMembers(
      makeIndex({ serverCollectionId: 'kc_live', serverCollectionModel: 'text-embedding-3-small' }),
      [makeAsset({ id: 'file-a' }), makeAsset({ id: 'file-b' })],
      {},
      { onMemberStart: (event) => started.push(event) },
    )
    expect(started.map((event) => event.fileId).sort()).toEqual(['file-a', 'file-b'])
    expect(started.every((event) => event.total === 2)).toBe(true)
    // A start reports CONFIRMED completions, never the queue position — with
    // members in flight at once a position would claim progress that has not
    // happened and then jump backwards when the first one lands.
    expect(started.every((event) => event.done <= 1)).toBe(true)
  })

  it('projects durable queue and contextualization progress for each member', async () => {
    const queued = {
      collection_id: 'kc_live',
      completed_documents: 0,
      current_batch: 0,
      document_id: 'kd_progress',
      error: null,
      events_url: '/events/ix_progress',
      job_id: 'ix_progress',
      operation_kind: 'document_revision',
      phase: 'queued',
      queue_position: 2,
      revision_id: 'rev_progress',
      status: 'queued',
      total_batches: 0,
    }
    mocks.startDocumentRevisionJob.mockResolvedValueOnce(queued)
    mocks.streamIndexingJobEvents.mockImplementationOnce(
      async (_url: string, options: { onEvent: (event: unknown) => void }) => {
        options.onEvent({
          data: {
            snapshot: {
              current_batch: 1,
              phase: 'contextualization',
              total_batches: 4,
            },
          },
          job_id: 'ix_progress',
          sequence: 2,
          type: 'inqtrix.index.progress',
        })
        options.onEvent({
          data: {
            snapshot: {
              current_batch: 2,
              phase: 'contextualization',
              total_batches: 4,
            },
          },
          job_id: 'ix_progress',
          sequence: 3,
          type: 'inqtrix.index.progress',
        })
        // Durable replay from an older cursor must not move the row backwards.
        options.onEvent({
          data: {
            snapshot: {
              current_batch: 1,
              phase: 'contextualization',
              total_batches: 4,
            },
          },
          job_id: 'ix_progress',
          sequence: 2,
          type: 'inqtrix.index.progress',
        })
      },
    )
    mocks.getIndexingJob.mockResolvedValueOnce({
      ...queued,
      current_batch: 0,
      phase: 'embedding',
      queue_position: null,
      status: 'completed',
    })
    const progress: Array<Record<string, unknown>> = []

    await ingestNewVectorIndexMembers(
      makeIndex({
        serverCollectionId: 'kc_live',
        serverCollectionModel: 'text-embedding-3-small',
      }),
      [makeAsset({ id: 'file-progress' })],
      {},
      { onMemberJobProgress: (event) => progress.push(event) },
    )

    expect(progress[0]).toMatchObject({
      fileId: 'file-progress',
      queuePosition: 2,
      status: 'queued',
    })
    expect(progress.filter((event) => event.currentBatch === 1)).toHaveLength(1)
    expect(progress.at(-1)).toMatchObject({
      currentBatch: 2,
      fileId: 'file-progress',
      phase: 'contextualization',
      totalBatches: 4,
    })
  })

  it('stops every worker at the first genuine failure', async () => {
    const started: string[] = []
    mocks.addKnowledgeDocument.mockImplementation(async (_collectionId: string, payload: { title: string }) => {
      started.push(payload.title)
      if (payload.title === 'fail.pdf') throw new Error('413 too large')
      return { id: `kd_${payload.title}` }
    })
    const members = [
      makeAsset({ id: 'file-a', title: 'a.pdf' }),
      makeAsset({ id: 'file-b', title: 'fail.pdf' }),
      ...Array.from({ length: 9 }, (_, n) => makeAsset({ id: `file-${n}`, title: `later-${n}.pdf` })),
    ]
    await expect(
      createVectorIndexCollectionOnServer(makeIndex(), members, {}),
    ).rejects.toThrow('413 too large')
    // The queue must NOT drain after the failure: no new provider work starts,
    // while already-confirmed siblings and the collection remain reusable.
    expect(started.length).toBeLessThan(members.length)
    expect(mocks.deleteKnowledgeCollection).not.toHaveBeenCalled()
  })

  it('lets the remaining documents run while one waits for review', async () => {
    // A document whose context needs review concerns that document only: the
    // queue keeps going instead of stopping every later document with it.
    const started: string[] = []
    mocks.startDocumentRevisionJob.mockImplementation(
      async (collectionId: string, payload: { title: string }) => {
        started.push(payload.title)
        return payload.title === 'review.pdf'
          ? jobSummary(collectionId, 'ix_review', 'kd_review', 'paused_validation')
          : jobSummary(collectionId, `ix_${payload.title}`, `kd_${payload.title}`, 'completed')
      },
    )
    const members = [
      makeAsset({ id: 'file-a', title: 'a.pdf' }),
      makeAsset({ id: 'file-review', title: 'review.pdf' }),
      ...Array.from({ length: 5 }, (_, n) => makeAsset({ id: `file-${n}`, title: `later-${n}.pdf` })),
    ]

    const error = await createVectorIndexCollectionOnServer(makeIndex(), members, {})
      .catch((caught: unknown) => caught)

    expect(started).toHaveLength(members.length)
    expect(error).toBeInstanceOf(KnowledgeReindexPartialError)
    const partial = error as KnowledgeReindexPartialError
    expect(partial.pausedJobs.map((job) => job.fileId)).toEqual(['file-review'])
    expect(partial.result.embeddedFileIds).toHaveLength(members.length - 1)
  })

  it('starts no further document once the provider is unavailable', async () => {
    // A provider outage would pause every later document as well: no fresh
    // provider work starts after it, the confirmed siblings stay usable.
    const started: string[] = []
    mocks.startDocumentRevisionJob.mockImplementation(
      async (collectionId: string, payload: { title: string }) => {
        started.push(payload.title)
        return payload.title === 'outage.pdf'
          ? jobSummary(collectionId, 'ix_outage', 'kd_outage', 'paused_dependency')
          : jobSummary(collectionId, `ix_${payload.title}`, `kd_${payload.title}`, 'completed')
      },
    )
    const members = [
      makeAsset({ id: 'file-a', title: 'a.pdf' }),
      makeAsset({ id: 'file-outage', title: 'outage.pdf' }),
      ...Array.from({ length: 5 }, (_, n) => makeAsset({ id: `file-${n}`, title: `later-${n}.pdf` })),
    ]

    const error = await createVectorIndexCollectionOnServer(makeIndex(), members, {})
      .catch((caught: unknown) => caught)

    expect(started.length).toBeLessThan(members.length)
    expect((error as KnowledgeReindexPartialError).pausedJobs.map((job) => job.fileId))
      .toEqual(['file-outage'])
  })

  it('reports a pause to its row as soon as the server reports it', async () => {
    // The row must read "paused" when the server pauses, not minutes later
    // when the last sibling finishes; the job id lets the user resume it.
    mocks.startDocumentRevisionJob.mockResolvedValueOnce(
      jobSummary('kc_live', 'ix_wait', 'kd_wait', 'running'),
    )
    mocks.getIndexingJob.mockResolvedValueOnce(
      jobSummary('kc_live', 'ix_wait', 'kd_wait', 'paused_validation', {
        error: { message: 'Kontext prüfen', type: 'contextualization_validation_error' },
      }),
    )
    const progress: Array<Record<string, unknown>> = []

    await ingestNewVectorIndexMembers(
      makeIndex({
        serverCollectionId: 'kc_live',
        serverCollectionModel: 'text-embedding-3-small',
      }),
      [makeAsset({ id: 'file-wait' })],
      {},
      { onMemberJobProgress: (event) => progress.push(event) },
    ).catch(() => undefined)

    expect(progress.at(-1)).toMatchObject({
      autoResume: null,
      errorType: 'contextualization_validation_error',
      fileId: 'file-wait',
      jobId: 'ix_wait',
      status: 'paused_validation',
    })
  })

  it('carries the automatic-resume plan of a dependency pause to its row', async () => {
    const plan = { attempts: 1, max: 5, next_at: 2_000_000_000, stopped: null }
    mocks.startDocumentRevisionJob.mockResolvedValueOnce(
      jobSummary('kc_live', 'ix_out', 'kd_out', 'running'),
    )
    mocks.getIndexingJob.mockResolvedValueOnce(
      jobSummary('kc_live', 'ix_out', 'kd_out', 'paused_dependency', {
        checkpoint: { auto_resume: plan },
        error: { message: 'vector store down', type: 'vector_store_unavailable' },
      }),
    )
    const progress: Array<Record<string, unknown>> = []

    await ingestNewVectorIndexMembers(
      makeIndex({
        serverCollectionId: 'kc_live',
        serverCollectionModel: 'text-embedding-3-small',
      }),
      [makeAsset({ id: 'file-out' })],
      {},
      { onMemberJobProgress: (event) => progress.push(event) },
    ).catch(() => undefined)

    expect(progress.at(-1)).toMatchObject({
      autoResume: plan,
      errorType: 'vector_store_unavailable',
      fileId: 'file-out',
      jobId: 'ix_out',
      status: 'paused_dependency',
    })
  })

  it('resumes each paused document and starts only the ones never begun', async () => {
    // "Alle fortsetzen": a paused job keeps its checkpoint (resumed, never
    // restarted, even when the collection already lists its document), a raw
    // choice rebuilds that revision without context, and a document the
    // stopped run never reached starts fresh.
    mocks.listKnowledgeDocuments.mockResolvedValue({
      data: [{ id: 'kd_a', metadata: { fileId: 'file-a' } }],
      next_cursor: null,
    })
    mocks.resumeIndexingJob.mockImplementation(
      async (jobId: string) => jobSummary('kc_live', jobId, 'kd_a', 'completed'),
    )
    mocks.resumeIndexingJobWithoutContext.mockImplementation(
      async (jobId: string) => jobSummary('kc_live', jobId, 'kd_b', 'ready_raw_by_user_choice'),
    )
    mocks.startDocumentRevisionJob.mockImplementation(
      async (collectionId: string) => jobSummary(collectionId, 'ix_c', 'kd_c', 'completed'),
    )

    const result = await ingestNewVectorIndexMembers(
      makeIndex({
        serverCollectionId: 'kc_live',
        serverCollectionModel: 'text-embedding-3-small',
      }),
      [
        makeAsset({ id: 'file-a', title: 'a.pdf' }),
        makeAsset({ id: 'file-b', title: 'b.pdf' }),
        makeAsset({ id: 'file-c', title: 'c.pdf' }),
      ],
      {},
      undefined,
      new Map([
        ['file-a', { jobId: 'ix_a', mode: 'resume' as const }],
        ['file-b', { jobId: 'ix_b', mode: 'raw' as const }],
      ]),
    )

    expect(mocks.resumeIndexingJob.mock.calls.map(([jobId]) => jobId)).toEqual(['ix_a'])
    expect(mocks.resumeIndexingJobWithoutContext.mock.calls.map(([jobId]) => jobId))
      .toEqual(['ix_b'])
    expect(mocks.startDocumentRevisionJob.mock.calls.map(([, payload]) => payload.title))
      .toEqual(['c.pdf'])
    expect(result.embeddedFileIds).toEqual(
      expect.arrayContaining(['file-a', 'file-b', 'file-c']),
    )
    expect(result.serverDocumentIds).toMatchObject({
      'file-a': 'kd_a',
      'file-b': 'kd_b',
      'file-c': 'kd_c',
    })
  })

  it('fails the resume instead of silently duplicating when the listing breaks', async () => {
    mocks.listKnowledgeDocuments.mockRejectedValue(new Error('500'))
    await expect(
      ingestNewVectorIndexMembers(
        makeIndex({ serverCollectionId: 'kc_live', serverCollectionModel: 'text-embedding-3-small' }),
        [makeAsset({ id: 'file-a' })],
        {},
      ),
    ).rejects.toThrow('500')
    // Without the adoption map a resume would re-POST documents the
    // collection already holds — a permanent, unremovable duplicate.
    expect(mocks.addKnowledgeDocument).not.toHaveBeenCalled()
  })

  it('adopts documents the collection already holds instead of embedding twice', async () => {
    mocks.listKnowledgeDocuments.mockResolvedValue({
      data: [{ id: 'kd_existing', metadata: { fileId: 'file-a' } }],
      next_cursor: null,
    })
    const result = await ingestNewVectorIndexMembers(
      makeIndex({ serverCollectionId: 'kc_live', serverCollectionModel: 'text-embedding-3-small' }),
      [makeAsset({ id: 'file-a' }), makeAsset({ id: 'file-b' })],
      {},
    )
    // A document left behind by a cancelled run is counted, never re-embedded.
    expect(mocks.addKnowledgeDocument).toHaveBeenCalledTimes(1)
    expect(result.embeddedFileIds.sort()).toEqual(['file-a', 'file-b'])
    expect(result.serverDocumentIds['file-a']).toBe('kd_existing')
  })
})
