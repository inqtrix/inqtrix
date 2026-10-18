import { describe, expect, it } from 'vitest'

import type { KnowledgeDocumentInfo } from '@/features/researchRuns/types'
import { contextOmittedByDocumentId, contextOmittedChunks } from './contextOmitted'

function document(id: string, metadata: Record<string, unknown>): KnowledgeDocumentInfo {
  return { chunk_count: 12, collection_id: 'kc_1', created_at: 0, id, metadata, title: id }
}

describe('chunks a document holds without context', () => {
  it('reads the count the server records with the revision', () => {
    expect(
      contextOmittedChunks({ _chunk_context_omitted: { chunks: 5, reason: 'content_filter' } }),
    ).toBe(5)
  })

  it('reads nothing from a document without omissions or with a malformed record', () => {
    for (const metadata of [
      {},
      { _chunk_context_omitted: null },
      { _chunk_context_omitted: 4 },
      { _chunk_context_omitted: { chunks: 0 } },
      { _chunk_context_omitted: { chunks: -2 } },
      { _chunk_context_omitted: { chunks: 1.5 } },
      { _chunk_context_omitted: { chunks: '3' } },
    ]) {
      expect(contextOmittedChunks(metadata)).toBe(0)
    }
  })

  it('maps every document with omissions by its id and leaves the rest out', () => {
    const counts = contextOmittedByDocumentId([
      document('kd_a', { _chunk_context_omitted: { chunks: 2, reason: 'content_filter' } }),
      document('kd_b', { fileId: 'file-b' }),
      document('kd_c', { _chunk_context_omitted: { chunks: 7, reason: 'content_filter' } }),
    ])

    expect([...counts]).toEqual([['kd_a', 2], ['kd_c', 7]])
  })
})
