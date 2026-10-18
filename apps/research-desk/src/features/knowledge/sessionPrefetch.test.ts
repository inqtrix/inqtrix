import { describe, expect, it } from 'vitest'

import type { KnowledgeSessionRecord } from '@/features/project/types'
import { recentKnowledgeSessionsForPrefetch } from './sessionPrefetch'

function session(id: string, updatedAt: string): KnowledgeSessionRecord {
  return {
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    title: id,
    updatedAt,
  }
}

describe('recentKnowledgeSessionsForPrefetch', () => {
  it('selects the five most recently updated server-known sessions', () => {
    const sessions = {
      'ks-1': session('ks-1', '2026-01-01T00:00:00.000Z'),
      'ks-2': session('ks-2', '2026-01-02T00:00:00.000Z'),
      'ks-3': session('ks-3', '2026-01-03T00:00:00.000Z'),
      'ks-4': session('ks-4', '2026-01-04T00:00:00.000Z'),
      'ks-5': session('ks-5', '2026-01-05T00:00:00.000Z'),
      'ks-6': session('ks-6', '2026-01-06T00:00:00.000Z'),
      'local-only': session('local-only', '2026-01-07T00:00:00.000Z'),
    }

    expect(
      recentKnowledgeSessionsForPrefetch(
        sessions,
        new Set(['ks-1', 'ks-2', 'ks-3', 'ks-4', 'ks-5', 'ks-6']),
      ).map((record) => record.id),
    ).toEqual(['ks-6', 'ks-5', 'ks-4', 'ks-3', 'ks-2'])
  })

  it('keeps the selection bounded by the explicit limit', () => {
    const sessions = {
      'ks-1': session('ks-1', '2026-01-01T00:00:00.000Z'),
      'ks-2': session('ks-2', '2026-01-02T00:00:00.000Z'),
      'ks-3': session('ks-3', '2026-01-03T00:00:00.000Z'),
    }

    expect(
      recentKnowledgeSessionsForPrefetch(
        sessions,
        new Set(['ks-1', 'ks-2', 'ks-3']),
        2,
      ).map((record) => record.id),
    ).toEqual(['ks-3', 'ks-2'])
  })
})
