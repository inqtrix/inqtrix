import { describe, expect, it } from 'vitest'

import type { ChatThreadRecord } from '../project/types'
import { deletionSyncExclusions } from './deletionSyncExclusions'

function thread(id: string, extra: Partial<ChatThreadRecord> = {}): ChatThreadRecord {
  return {
    createdAt: '2026-08-27T06:00:00.000Z',
    id,
    messages: [],
    preview: '',
    source: 'api',
    title: id,
    updatedAt: '2026-08-27T06:00:00.000Z',
    ...extra,
  }
}

describe('deletionSyncExclusions', () => {
  it('excludes a thread carrying a deletion marker', () => {
    const { exclude } = deletionSyncExclusions(
      {
        alive: thread('alive'),
        failing: thread('failing', {
          deletion: { error: 'server refused', status: 'delete_failed' },
        }),
        going: thread('going', { deletion: { error: null, status: 'deleting' } }),
      },
      new Set(),
    )
    expect(exclude).toEqual(new Set(['going', 'failing']))
  })

  it('excludes a just-deleted id whose stale record still sits in the snapshot', () => {
    // The resurrection window: DELETE confirmed, baseline pruned, but the
    // flush queued behind the deletion still sees the thread in its state
    // snapshot because React has not committed the removal yet.
    const { exclude, settled } = deletionSyncExclusions(
      { stale: thread('stale') },
      new Set(['stale']),
    )
    expect(exclude.has('stale')).toBe(true)
    expect(settled).toEqual([])
  })

  it('settles a tombstone once the removal has committed', () => {
    const { exclude, settled } = deletionSyncExclusions(
      { alive: thread('alive') },
      new Set(['committed-away']),
    )
    expect(settled).toEqual(['committed-away'])
    expect(exclude.has('committed-away')).toBe(false)
  })
})
