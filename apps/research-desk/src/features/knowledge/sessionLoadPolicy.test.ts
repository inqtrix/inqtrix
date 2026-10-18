import { describe, expect, it } from 'vitest'

import type { KnowledgeSessionRecord } from '@/features/project/types'
import {
  decideKnowledgeSessionItemsLoadMerge,
  shouldSurfaceKnowledgeSessionItemsLoadResult,
} from './sessionLoadPolicy'

function session(id: string, updatedAt: string): KnowledgeSessionRecord {
  return {
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    title: id,
    updatedAt,
  }
}

describe('shouldSurfaceKnowledgeSessionItemsLoadResult', () => {
  it('does not surface prefetch results even when the session is currently selected', () => {
    expect(
      shouldSurfaceKnowledgeSessionItemsLoadResult({
        selectedSessionId: 'ks-selected',
        sessionId: 'ks-selected',
        surfaceErrors: false,
      }),
    ).toBe(false)
  })

  it('does not surface selected-load results after the user switches sessions', () => {
    expect(
      shouldSurfaceKnowledgeSessionItemsLoadResult({
        selectedSessionId: 'ks-current',
        sessionId: 'ks-old',
        surfaceErrors: true,
      }),
    ).toBe(false)
  })

  it('surfaces the selected session load result', () => {
    expect(
      shouldSurfaceKnowledgeSessionItemsLoadResult({
        selectedSessionId: 'ks-selected',
        sessionId: 'ks-selected',
        surfaceErrors: true,
      }),
    ).toBe(true)
  })
})

describe('decideKnowledgeSessionItemsLoadMerge', () => {
  it('skips applying server results when the local session was deleted', () => {
    expect(
      decideKnowledgeSessionItemsLoadMerge({
        localItemCount: 0,
        localSession: undefined,
        serverItemCount: 1,
        serverSession: session('ks-deleted', '2026-01-02T00:00:00.000Z'),
      }),
    ).toEqual({
      applyServerState: false,
      markItemsLoadResolved: false,
      markItemsPayloadLoaded: false,
    })
  })

  it('applies and marks loaded when the server version is at least as fresh as local state', () => {
    expect(
      decideKnowledgeSessionItemsLoadMerge({
        localItemCount: 0,
        localSession: session('ks-selected', '2026-01-01T00:00:00.000Z'),
        serverItemCount: 1,
        serverSession: session('ks-selected', '2026-01-02T00:00:00.000Z'),
      }),
    ).toEqual({
      applyServerState: true,
      markItemsLoadResolved: true,
      markItemsPayloadLoaded: true,
    })
  })

  it('keeps an older server item payload unloaded when local state has no items', () => {
    expect(
      decideKnowledgeSessionItemsLoadMerge({
        localItemCount: 0,
        localSession: session('ks-selected', '2026-01-03T00:00:00.000Z'),
        serverItemCount: 1,
        serverSession: session('ks-selected', '2026-01-02T00:00:00.000Z'),
      }),
    ).toEqual({
      applyServerState: false,
      markItemsLoadResolved: true,
      markItemsPayloadLoaded: false,
    })
  })

  it('treats local non-empty items as a complete payload for an older server version', () => {
    expect(
      decideKnowledgeSessionItemsLoadMerge({
        localItemCount: 2,
        localSession: session('ks-selected', '2026-01-03T00:00:00.000Z'),
        serverItemCount: 1,
        serverSession: session('ks-selected', '2026-01-02T00:00:00.000Z'),
      }),
    ).toEqual({
      applyServerState: false,
      markItemsLoadResolved: true,
      markItemsPayloadLoaded: true,
    })
  })

  it('marks an empty older server payload loaded when local state is also empty', () => {
    expect(
      decideKnowledgeSessionItemsLoadMerge({
        localItemCount: 0,
        localSession: session('ks-selected', '2026-01-03T00:00:00.000Z'),
        serverItemCount: 0,
        serverSession: session('ks-selected', '2026-01-02T00:00:00.000Z'),
      }),
    ).toEqual({
      applyServerState: false,
      markItemsLoadResolved: true,
      markItemsPayloadLoaded: true,
    })
  })
})
