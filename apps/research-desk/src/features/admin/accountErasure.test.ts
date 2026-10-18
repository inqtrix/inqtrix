import { describe, expect, it, vi } from 'vitest'

import type { ServerDeletionOperation } from '@/api/inqtrixClient'
import { OBSERVATION_MAX_FAILURES } from '@/features/project/observationRetry'
import { nextDeletionPollDelayMs } from '@/features/project/sessionDeletion'
import { createAccountErasureObserver } from './accountErasure'

function operation(partial: Partial<ServerDeletionOperation>): ServerDeletionOperation {
  return {
    asset_ids: [],
    attempt: 1,
    completed_items: 0,
    created_at: 1,
    error: null,
    finished_at: null,
    operation_id: 'del_1',
    retryable: false,
    stage: 'queued',
    started_at: null,
    status: 'queued',
    target_id: 'user-1',
    target_kind: 'user',
    total_items: 4,
    ...partial,
  }
}

function notReadableHere(): Error {
  return Object.assign(new Error('not found'), { status: 404 })
}

/** An observer over a scripted sequence of reads; every wait is instant. */
function harness(reads: Array<ServerDeletionOperation | Error>) {
  const script = [...reads]
  const delays: number[] = []
  const events: Array<[string, unknown]> = []
  const restarts: Array<() => void> = []
  const read = vi.fn(async () => {
    const next = script.shift()
    if (next === undefined) throw new Error('the script has no further read')
    if (next instanceof Error) throw next
    return next
  })
  const reopen = vi.fn(async () => operation({ status: 'queued' }))
  const observer = createAccountErasureObserver({
    failureText: (error) => (error ? `reader text for ${error.type}` : null),
    onErased: (userId) => events.push(['erased', userId]),
    onProgress: (_userId, progress) => events.push(['progress', progress]),
    onResumed: () => undefined,
    onStopped: (_operationId, restart) => restarts.push(restart),
    read,
    reopen,
    sleep: async (delayMs) => {
      delays.push(delayMs)
    },
  })
  return { delays, events, observer, read, reopen, restarts }
}

describe('following an account erasure', () => {
  it('reports every checkpoint until the account is gone', async () => {
    const h = harness([
      operation({ status: 'queued' }),
      operation({ completed_items: 1, stage: 'aggregates_removed', status: 'running' }),
      operation({ completed_items: 4, stage: 'deleted', status: 'deleted' }),
    ])

    h.observer.observe('user-1', 'del_1')

    await vi.waitFor(() => expect(h.events.at(-1)).toEqual(['erased', 'user-1']))
    expect(h.events.slice(0, 2)).toEqual([
      ['progress', expect.objectContaining({ completed: 0, phase: 'queued', state: 'running', total: 4 })],
      ['progress', expect.objectContaining({ completed: 1, phase: 'aggregates_removed', state: 'running', total: 4 })],
    ])
    // The cadence every deletion receipt is read with.
    expect(h.delays).toEqual([0, 1, 2].map(nextDeletionPollDelayMs))
  })

  it('stops at a failed erasure and follows it again after a retry', async () => {
    const h = harness([
      operation({
        error: { message: 'object store unreachable', type: 'server_error' },
        stage: 'delete_failed',
        status: 'delete_failed',
      }),
      operation({ stage: 'deleted', status: 'deleted' }),
    ])

    h.observer.observe('user-1', 'del_1')
    await vi.waitFor(() => expect(h.events).toHaveLength(1))

    expect(h.events[0]).toEqual([
      'progress',
      expect.objectContaining({ error: 'reader text for server_error', phase: null, state: 'failed' }),
    ])
    expect(h.read).toHaveBeenCalledTimes(1)

    await h.observer.retry('user-1')

    await vi.waitFor(() => expect(h.events.at(-1)).toEqual(['erased', 'user-1']))
    expect(h.reopen).toHaveBeenCalledWith('del_1')
  })

  it('shows an erasure it cannot read as being erased and stops asking', async () => {
    const h = harness([notReadableHere()])

    h.observer.observe('user-1', 'del_of_another_admin')
    await vi.waitFor(() => expect(h.events).toHaveLength(1))

    expect(h.events[0]).toEqual([
      'progress',
      expect.objectContaining({ operationId: 'del_of_another_admin', state: 'unknown' }),
    ])
    expect(h.read).toHaveBeenCalledTimes(1)
  })

  it('stops after repeated failed reads and starts again on the restart cue', async () => {
    const failures = Array.from({ length: OBSERVATION_MAX_FAILURES }, () => new Error('offline'))
    const h = harness([...failures, operation({ stage: 'deleted', status: 'deleted' })])

    h.observer.observe('user-1', 'del_1')
    await vi.waitFor(() => expect(h.restarts).toHaveLength(1))
    expect(h.read).toHaveBeenCalledTimes(OBSERVATION_MAX_FAILURES)

    h.restarts[0]!()

    await vi.waitFor(() => expect(h.events.at(-1)).toEqual(['erased', 'user-1']))
  })

  it('stops following an account that left the list', async () => {
    const read = vi.fn(async () => operation({ status: 'running' }))
    const observer = createAccountErasureObserver({
      failureText: () => null,
      onErased: () => undefined,
      onProgress: () => undefined,
      onResumed: () => undefined,
      onStopped: () => undefined,
      read,
      reopen: async () => operation({}),
      sleep: (_delayMs, signal) =>
        new Promise((resolve, reject) => {
          const timer = setTimeout(resolve, 1)
          signal.addEventListener(
            'abort',
            () => {
              clearTimeout(timer)
              reject(signal.reason)
            },
            { once: true },
          )
        }),
    })

    observer.observe('user-1', 'del_1')
    await vi.waitFor(() => expect(read).toHaveBeenCalled())
    observer.retain(new Set())
    const readsWhenLeft = read.mock.calls.length
    await new Promise((resolve) => setTimeout(resolve, 30))

    expect(read).toHaveBeenCalledTimes(readsWhenLeft)
  })
})
