import { describe, expect, it } from 'vitest'

import type { KnowledgeThreadItemRecord } from '@/features/project/types'
import { knowledgeComposerContextForSession } from './composerSessionContext'

function item(
  id: string,
  sessionId: string,
  overrides: Partial<KnowledgeThreadItemRecord> = {},
): KnowledgeThreadItemRecord {
  return {
    collectionIds: ['index-default'],
    collectionTitles: ['Default'],
    createdAt: '2026-08-05T12:00:00.000Z',
    id,
    progress: { steps: [] },
    question: 'Was ist belegt?',
    requestedProfile: 'standard',
    runId: `run-${id}`,
    sessionId,
    status: 'completed',
    ...overrides,
  }
}

describe('knowledgeComposerContextForSession', () => {
  it('restores the latest confirmed collection and requested Auto profile', () => {
    const context = knowledgeComposerContextForSession({
      availableCollectionIds: ['index-a', 'index-b'],
      availableProfileIds: ['auto', 'schnell', 'standard', 'tief'],
      evidenceKMax: 15,
      itemOrder: ['item-old', 'item-latest'],
      items: {
        'item-old': item('item-old', 'session-a', {
          collectionIds: ['index-a'],
          requestedProfile: 'schnell',
        }),
        'item-latest': item('item-latest', 'session-a', {
          collectionIds: ['index-b'],
          finalK: 7,
          requestedProfile: 'auto',
          topK: 19,
        }),
      },
      sessionId: 'session-a',
    })

    expect(context).toEqual({
      collectionIds: ['index-b'],
      finalK: 7,
      profileId: 'auto',
      sourceItemId: 'item-latest',
      topK: 19,
    })
  })

  it('never restores context from another session', () => {
    const context = knowledgeComposerContextForSession({
      availableCollectionIds: ['index-a', 'index-b'],
      availableProfileIds: ['auto', 'standard'],
      evidenceKMax: 15,
      itemOrder: ['item-a', 'item-b'],
      items: {
        'item-a': item('item-a', 'session-a', {
          collectionIds: ['index-a'],
          requestedProfile: 'auto',
        }),
        'item-b': item('item-b', 'session-b', {
          collectionIds: ['index-b'],
          requestedProfile: 'standard',
        }),
      },
      sessionId: 'session-a',
    })

    expect(context?.collectionIds).toEqual(['index-a'])
    expect(context?.profileId).toBe('auto')
    expect(context?.sourceItemId).toBe('item-a')
  })

  it('drops revoked collections and unavailable profiles without weakening the gate', () => {
    const context = knowledgeComposerContextForSession({
      availableCollectionIds: ['index-allowed'],
      availableProfileIds: ['auto', 'standard'],
      evidenceKMax: 10,
      itemOrder: ['item-latest'],
      items: {
        'item-latest': item('item-latest', 'session-a', {
          collectionIds: ['index-allowed', 'index-revoked'],
          finalK: 50,
          requestedProfile: 'retired-profile',
          topK: 80,
        }),
      },
      sessionId: 'session-a',
    })

    expect(context).toEqual({
      collectionIds: ['index-allowed'],
      finalK: 10,
      profileId: null,
      sourceItemId: 'item-latest',
      topK: 50,
    })
  })

  it('returns null for a new empty session', () => {
    expect(knowledgeComposerContextForSession({
      availableCollectionIds: ['index-a'],
      availableProfileIds: ['auto'],
      evidenceKMax: 15,
      itemOrder: [],
      items: {},
      sessionId: 'session-new',
    })).toBeNull()
  })
})
