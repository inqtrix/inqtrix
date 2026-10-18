import { describe, expect, it } from 'vitest'
import type { KnowledgeSearchHit } from '@/features/researchRuns/types'
import { groupHitsByDocument } from './findGrouping'

function hit(documentId: string, chunkIndex: number, score: number): KnowledgeSearchHit {
  return {
    chunk_id: `chunk-${documentId}-${chunkIndex}`,
    chunk_index: chunkIndex,
    collection_id: `col-${documentId}`,
    document_id: documentId,
    document_title: `Titel ${documentId}`,
    excerpt: `Auszug ${documentId}-${chunkIndex}`,
    generation_id: 'generation-1',
    page_number: null,
    provenance_status: 'verified_span',
    rank: 1,
    reference_id: 'K1',
    revision_id: 'revision-1',
    score,
    source_span: {
      document_content_hash: 'sha256',
      end: 20,
      offset_unit: 'utf8_byte',
      start: 0,
    },
  }
}

describe('groupHitsByDocument', () => {
  it('groups score-sorted hits by document, ordered by best hit', () => {
    const groups = groupHitsByDocument([
      hit('b', 0, 0.9),
      hit('a', 2, 0.8),
      hit('b', 3, 0.7),
      hit('a', 1, 0.6),
    ])

    expect(groups.map((group) => group.documentId)).toEqual(['b', 'a'])
    expect(groups[0]).toMatchObject({
      collectionId: 'col-b',
      hitCount: 2,
      title: 'Titel b',
      topScore: 0.9,
    })
  })

  it('caps snippets per document but keeps the full hit count', () => {
    const groups = groupHitsByDocument([
      hit('a', 0, 0.9),
      hit('a', 1, 0.8),
      hit('a', 2, 0.7),
      hit('a', 3, 0.6),
      hit('a', 4, 0.5),
    ])

    expect(groups).toHaveLength(1)
    expect(groups[0].hitCount).toBe(5)
    expect(groups[0].snippets.map((snippet) => snippet.chunk_index)).toEqual([0, 1, 2])
  })

  it('returns an empty list for no hits', () => {
    expect(groupHitsByDocument([])).toEqual([])
  })
})
