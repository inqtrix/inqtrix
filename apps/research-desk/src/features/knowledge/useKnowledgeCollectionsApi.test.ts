import { describe, expect, it } from 'vitest'
import type { KnowledgeCollectionInfo } from '@/features/researchRuns/types'
import { knowledgeCollectionOptions } from './useKnowledgeCollectionsApi'

const localIndexes = [
  {
    id: 'index-local-key',
    serverCollectionId: 'collection-owned',
    status: 'ready' as const,
    title: 'Local title',
  },
  {
    id: 'index-local-only',
    serverCollectionId: null,
    status: 'ready' as const,
    title: 'Local only',
  },
]

function collection(
  id: string,
  name: string,
  access: KnowledgeCollectionInfo['access'] = { mode: 'owner' },
): KnowledgeCollectionInfo {
  return {
    access,
    created_at: 1,
    document_count: 2,
    embedding_dim: 3,
    embedding_model: 'embed-test',
    id,
    name,
  }
}

describe('knowledgeCollectionOptions', () => {
  it('keeps stable local selection ids until the first server list settles', () => {
    expect(knowledgeCollectionOptions({
      localIndexes,
      serverCollections: [],
      serverLoaded: false,
    })).toEqual([{
      collectionId: 'collection-owned',
      id: 'index-local-key',
      title: 'Local title',
    }])
  })

  it('uses the successful server list as truth and includes shared collections', () => {
    expect(knowledgeCollectionOptions({
      localIndexes,
      serverCollections: [
        collection('collection-owned', 'Current server title'),
        collection('collection-shared', 'Shared collection', {
          mode: 'shared',
          permission: 'view',
        }),
      ],
      serverLoaded: true,
    })).toEqual([
      {
        collectionId: 'collection-owned',
        id: 'index-local-key',
        title: 'Current server title',
      },
      {
        collectionId: 'collection-shared',
        id: 'collection-shared',
        title: 'Shared collection',
      },
    ])
  })

  it('accepts an authoritative empty list', () => {
    expect(knowledgeCollectionOptions({
      localIndexes,
      serverCollections: [],
      serverLoaded: true,
    })).toEqual([])
  })
})
