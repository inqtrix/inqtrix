import { describe, expect, it } from 'vitest'

import { bindSyncScope, deleteTolerant404, syncCollection } from './syncCollection'

type Item = { id: string; v: string }

function harness(current: Record<string, Item>, synced: Map<string, string>) {
  const pushed: string[] = []
  const deleted: string[] = []
  const run = () =>
    syncCollection<Item, string>({
      current,
      synced,
      fingerprintOf: (item) => item.v,
      changed: (previous, value) => previous !== value,
      pushOne: async (item) => {
        pushed.push(item.id)
      },
      deleteOne: async (id) => {
        deleted.push(id)
      },
    })
  return { deleted, pushed, run }
}

describe('syncCollection', () => {
  it('pushes new entities and advances the synced fingerprint', async () => {
    const synced = new Map<string, string>()
    const { pushed, run } = harness({ a: { id: 'a', v: '1' } }, synced)
    await run()
    expect(pushed).toEqual(['a'])
    expect(synced.get('a')).toBe('1')
  })

  it('pushes only changed entities, skips unchanged', async () => {
    const synced = new Map([['a', '1'], ['b', '1']])
    const { pushed, run } = harness(
      { a: { id: 'a', v: '1' }, b: { id: 'b', v: '2' } },
      synced,
    )
    await run()
    expect(pushed).toEqual(['b']) // a unchanged, b changed
    expect(synced.get('b')).toBe('2')
  })

  it('deletes entities the local collection no longer has', async () => {
    const synced = new Map([['a', '1'], ['gone', '1']])
    const { deleted, run } = harness({ a: { id: 'a', v: '1' } }, synced)
    await run()
    expect(deleted).toEqual(['gone'])
    expect(synced.has('gone')).toBe(false)
    expect(synced.has('a')).toBe(true)
  })

  it('leaves an excluded id alone in both directions', async () => {
    // The resurrection case: a confirmed DELETE prunes the synced baseline
    // while the stale state snapshot still holds the thread. Without the
    // exclusion the diff reads that as NEW -> re-push (the deleted row
    // comes back), and the re-seeded fingerprint makes the next pass issue
    // an uncommanded second DELETE. Excluded-but-synced covers the mirror
    // case: a failed deletion waiting for its manual retry must not be
    // auto-deleted by the flush.
    const synced = new Map([['failed', '1']])
    const pushed: string[] = []
    const deleted: string[] = []
    await syncCollection<Item, string>({
      current: { stale: { id: 'stale', v: '9' } },
      exclude: new Set(['stale', 'failed']),
      synced,
      fingerprintOf: (item) => item.v,
      changed: (previous, value) => previous !== value,
      pushOne: async (item) => {
        pushed.push(item.id)
      },
      deleteOne: async (id) => {
        deleted.push(id)
      },
    })
    expect(pushed).toEqual([])
    expect(deleted).toEqual([])
    expect(synced.has('stale')).toBe(false) // never re-seeded
    expect(synced.get('failed')).toBe('1') // baseline kept for the retry
  })

  it('does not advance the fingerprint when the push throws (retry-safe)', async () => {
    const synced = new Map<string, string>()
    let attempts = 0
    await expect(
      syncCollection<Item, string>({
        current: { a: { id: 'a', v: '1' } },
        synced,
        fingerprintOf: (item) => item.v,
        changed: (previous, value) => previous !== value,
        pushOne: async () => {
          attempts += 1
          throw new Error('network')
        },
        deleteOne: async () => {},
      }),
    ).rejects.toThrow('network')
    expect(attempts).toBe(1)
    expect(synced.has('a')).toBe(false) // not advanced -> retried next pass
  })
})

describe('syncCollection while other writers touch the baseline', () => {
  // A pass reads the local collection when it starts and awaits each push.
  // Meanwhile other writers add to the baseline: an older page loaded into the
  // list, a confirmed creation, a newly hydrated project. Ids added after the
  // snapshot are not part of this pass's difference.
  const run = (
    current: Record<string, Item>,
    synced: Map<string, string>,
    during: () => void,
    inScope?: () => boolean,
  ) => {
    const pushed: string[] = []
    const deleted: string[] = []
    let first = true
    return syncCollection<Item, string>({
      current,
      synced,
      fingerprintOf: (item) => item.v,
      changed: (previous, value) => previous !== value,
      pushOne: async (item) => {
        pushed.push(item.id)
        if (first) {
          first = false
          during()
        }
      },
      deleteOne: async (id) => {
        deleted.push(id)
      },
      inScope,
    }).then(() => ({ deleted, pushed }))
  }

  it('never deletes an id that entered the baseline after the snapshot', async () => {
    const synced = new Map([['a', '0']])
    const { deleted } = await run({ a: { id: 'a', v: '1' } }, synced, () => {
      synced.set('older-page', '1')
    })
    expect(deleted).toEqual([])
    expect(synced.get('older-page')).toBe('1')
  })

  it('still deletes what was gone locally when the pass started, unless it already left the baseline', async () => {
    const synced = new Map([['a', '0'], ['removed', '1'], ['deleted-meanwhile', '1']])
    const { deleted } = await run({ a: { id: 'a', v: '1' } }, synced, () => {
      synced.delete('deleted-meanwhile')
    })
    expect(deleted).toEqual(['removed'])
  })

  it('stops without a further request or baseline write once its project is gone', async () => {
    // A project switch resets the baseline and hydrates the next project
    // while this pass still awaits a push of the previous one.
    const synced = new Map([['a', '0'], ['b', '0'], ['removed', '1']])
    let live = true
    const { deleted, pushed } = await run(
      { a: { id: 'a', v: '1' }, b: { id: 'b', v: '1' } },
      synced,
      () => {
        live = false
        synced.clear()
        synced.set('next-project', '1')
      },
      () => live,
    )
    expect(pushed).toEqual(['a'])
    expect(deleted).toEqual([])
    expect([...synced]).toEqual([['next-project', '1']])
  })
})

describe('syncCollection between deletes', () => {
  it('sends nothing when its project went away before this collection started', async () => {
    // A flush runs several collections in a row (documents, then folders);
    // the switch can happen while an earlier one awaits its last request.
    const synced = new Map([['kept', '0'], ['removed', '1']])
    const pushed: string[] = []
    const deleted: string[] = []
    await syncCollection<Item, string>({
      current: { kept: { id: 'kept', v: '1' } },
      synced,
      fingerprintOf: (item) => item.v,
      changed: (previous, value) => previous !== value,
      pushOne: async (item) => { pushed.push(item.id) },
      deleteOne: async (id) => { deleted.push(id) },
      inScope: () => false,
    })
    expect(pushed).toEqual([])
    expect(deleted).toEqual([])
    expect([...synced]).toEqual([['kept', '0'], ['removed', '1']])
  })

  it('sends no delete when its project went away before this collection started', async () => {
    const synced = new Map([['kept', '1'], ['removed', '1']])
    const deleted: string[] = []
    await syncCollection<Item, string>({
      current: { kept: { id: 'kept', v: '1' } },
      synced,
      fingerprintOf: (item) => item.v,
      changed: (previous, value) => previous !== value,
      pushOne: async () => {},
      deleteOne: async (id) => { deleted.push(id) },
      inScope: () => false,
    })
    expect(deleted).toEqual([])
    expect(synced.has('removed')).toBe(true)
  })

  it('stops before the next delete once its project is gone', async () => {
    const synced = new Map([['x', '1'], ['y', '1']])
    const deleted: string[] = []
    let live = true
    await syncCollection<Item, string>({
      current: {},
      synced,
      fingerprintOf: (item) => item.v,
      changed: (previous, value) => previous !== value,
      pushOne: async () => {},
      deleteOne: async (id) => {
        deleted.push(id)
        live = false
      },
      inScope: () => live,
    })
    expect(deleted).toEqual(['x'])
    expect(synced.has('x')).toBe(true)
    expect(synced.has('y')).toBe(true)
  })
})

describe('bindSyncScope', () => {
  it('holds while the project and the sync switch it was bound to hold', () => {
    let identity = 'ws-1:1'
    let active = true
    const inScope = bindSyncScope(() => (active ? identity : null))
    expect(inScope()).toBe(true)
    identity = 'ws-2:1'
    expect(inScope()).toBe(false)
    identity = 'ws-1:1'
    active = false
    expect(inScope()).toBe(false)
  })
})

describe('deleteTolerant404', () => {
  it('treats an already-absent entity as the successful delete state', async () => {
    const notFound = Object.assign(new Error('not found'), { status: 404 })

    await expect(
      deleteTolerant404(async () => { throw notFound }),
    ).resolves.toBeUndefined()
  })

  it('keeps non-404 failures visible to the synchronization badge', async () => {
    const unavailable = Object.assign(new Error('unavailable'), { status: 503 })

    await expect(
      deleteTolerant404(async () => { throw unavailable }),
    ).rejects.toBe(unavailable)
  })
})
