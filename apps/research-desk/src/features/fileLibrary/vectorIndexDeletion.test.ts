import { describe, expect, it, vi } from 'vitest'

import type { ServerDeletionOperation } from '@/api/inqtrixClient'
import {
  deleteVectorIndexAggregate,
  resolveVectorIndexDeletionRoute,
} from './vectorIndexDeletion'

const operation: ServerDeletionOperation = {
  asset_ids: [],
  attempt: 0,
  completed_items: 0,
  created_at: 1,
  error: null,
  finished_at: null,
  operation_id: 'del_1',
  retryable: false,
  stage: 'queued',
  started_at: null,
  status: 'queued',
  target_id: 'vi_1',
  target_kind: 'vector_index',
  total_items: 4,
}

describe('resolveVectorIndexDeletionRoute', () => {
  it('uses the complete aggregate when project persistence owns the index', () => {
    expect(resolveVectorIndexDeletionRoute({
      knowledgeAvailable: true,
      projectPersistenceActive: true,
      serverCollectionId: 'kc_1',
    })).toBe('vector_index')
  })

  it('uses the Knowledge lifecycle for a local index with a server collection', () => {
    expect(resolveVectorIndexDeletionRoute({
      knowledgeAvailable: true,
      projectPersistenceActive: false,
      serverCollectionId: 'kc_1',
    })).toBe('knowledge_collection')
  })

  it('keeps an unbound local index deletion local', () => {
    expect(resolveVectorIndexDeletionRoute({
      knowledgeAvailable: true,
      projectPersistenceActive: false,
      serverCollectionId: null,
    })).toBe('local')
  })
})

describe('deleteVectorIndexAggregate', () => {
  it('starts exactly one server-owned aggregate operation', async () => {
    const deleteIndex = vi.fn(async () => operation)
    await expect(deleteVectorIndexAggregate({
      collectionId: 'kc_1',
      dependencies: { deleteIndex },
      indexId: 'vi_1',
      options: {},
    })).resolves.toEqual(operation)

    expect(deleteIndex).toHaveBeenCalledOnce()
    expect(deleteIndex).toHaveBeenCalledWith('vi_1', {}, 'kc_1')
  })

  it('does not hide a failed operation start behind a local fallback', async () => {
    const failure = Object.assign(new Error('HTTP 503'), { status: 503 })
    await expect(deleteVectorIndexAggregate({
      dependencies: { deleteIndex: vi.fn(async () => { throw failure }) },
      indexId: 'vi_1',
      options: {},
    })).rejects.toBe(failure)
  })
})
