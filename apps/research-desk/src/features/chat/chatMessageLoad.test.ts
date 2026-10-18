import { describe, expect, it, vi } from 'vitest'

import {
  chatThreadHistoryPending,
  selectedChatMessageLoadState,
  shareChatMessageLoad,
  type SharedChatMessageLoad,
  updateChatMessageLoadError,
} from './chatMessageLoad'

function deferred(): {
  promise: Promise<void>
  resolve: () => void
} {
  let resolve: () => void = () => undefined
  const promise = new Promise<void>((settle) => {
    resolve = settle
  })
  return { promise, resolve }
}

describe('shareChatMessageLoad', () => {
  it('shares a prefetch failure and promotes selected-thread error handling', async () => {
    const gate = deferred()
    const loads = new Map<string, SharedChatMessageLoad>()
    const surfaceIntentAtSettlement: boolean[] = []
    const start = vi.fn(async (load: SharedChatMessageLoad) => {
      await gate.promise
      surfaceIntentAtSettlement.push(load.surfaceErrors)
      throw new Error('offline')
    })

    const prefetched = shareChatMessageLoad(loads, 'thread-a', false, start)
    const selected = shareChatMessageLoad(loads, 'thread-a', true, start)

    expect(selected).toBe(prefetched)
    expect(loads.get('thread-a')?.surfaceErrors).toBe(true)
    gate.resolve()
    await expect(selected).rejects.toThrow('offline')

    expect(start).toHaveBeenCalledOnce()
    expect(surfaceIntentAtSettlement).toEqual([true])
    expect(loads.has('thread-a')).toBe(false)
  })

  it('removes a failed flight so the same thread can be retried', async () => {
    const loads = new Map<string, SharedChatMessageLoad>()
    const start = vi.fn()
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(undefined)

    await expect(
      shareChatMessageLoad(loads, 'thread-a', true, start),
    ).rejects.toThrow('offline')
    expect(loads.has('thread-a')).toBe(false)

    await expect(
      shareChatMessageLoad(loads, 'thread-a', true, start),
    ).resolves.toBeUndefined()
    expect(start).toHaveBeenCalledTimes(2)
  })

  it('does not let an older completion clear a replacement flight', async () => {
    const firstGate = deferred()
    const secondGate = deferred()
    const loads = new Map<string, SharedChatMessageLoad>()
    const first = shareChatMessageLoad(
      loads,
      'thread-a',
      false,
      async () => firstGate.promise,
    )

    loads.clear()
    const second = shareChatMessageLoad(
      loads,
      'thread-a',
      false,
      async () => secondGate.promise,
    )
    firstGate.resolve()
    await first

    expect(loads.get('thread-a')?.promise).toBe(second)
    secondGate.resolve()
    await second
    expect(loads.has('thread-a')).toBe(false)
  })
})

describe('selected chat message load state', () => {
  it('keeps failure terminal without treating it as resolved empty content', () => {
    const failed = updateChatMessageLoadError(new Map(), 'thread-a', 'offline')

    expect(selectedChatMessageLoadState({
      errorByThreadId: failed,
      expectsServerMessages: true,
      resolvedThreadIds: new Set(),
      selectedThreadId: 'thread-a',
    })).toEqual({ error: 'offline', loading: false })
  })

  it('re-arms loading when retry clears the failure', () => {
    const failed = updateChatMessageLoadError(new Map(), 'thread-a', 'offline')
    const retrying = updateChatMessageLoadError(failed, 'thread-a', null)

    expect(selectedChatMessageLoadState({
      errorByThreadId: retrying,
      expectsServerMessages: true,
      resolvedThreadIds: new Set(),
      selectedThreadId: 'thread-a',
    })).toEqual({ error: null, loading: true })
  })

  it('stops loading only after a successful payload resolves', () => {
    expect(selectedChatMessageLoadState({
      errorByThreadId: new Map(),
      expectsServerMessages: true,
      resolvedThreadIds: new Set(['thread-a']),
      selectedThreadId: 'thread-a',
    })).toEqual({ error: null, loading: false })
  })
})

describe('continuing a thread waits for its history', () => {
  it('waits only for a listed server thread whose messages have not loaded', () => {
    const pending = { failed: false, localMessageCount: 0, resolved: false, serverThreadKnown: true }
    expect(chatThreadHistoryPending(pending)).toBe(true)
    expect(chatThreadHistoryPending({ ...pending, serverThreadKnown: false })).toBe(false)
    expect(chatThreadHistoryPending({ ...pending, localMessageCount: 2 })).toBe(false)
    expect(chatThreadHistoryPending({ ...pending, resolved: true })).toBe(false)
    expect(chatThreadHistoryPending({ ...pending, failed: true })).toBe(false)
  })
})
