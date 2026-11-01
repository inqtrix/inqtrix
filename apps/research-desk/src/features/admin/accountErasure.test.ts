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

/** An observer over scripted reads of the operation and of the account;
 * every wait is instant. */
function harness(
  reads: Array<ServerDeletionOperation | Error>,
  accountReads: Array<ServerDeletionOperation | Error> = [],
) {
  const script = [...reads]
  const accountScript = [...accountReads]
  const delays: number[] = []
  const events: Array<[string, unknown]> = []
  const restarts: Array<() => void> = []
  const next = (queue: Array<ServerDeletionOperation | Error>) => {
    const value = queue.shift()
    if (value === undefined) throw new Error('the script has no further read')
    if (value instanceof Error) throw value
    return value
  }
  const read = vi.fn(async () => next(script))
  const readByUser = vi.fn(async () => next(accountScript))
  const reopen = vi.fn(async () => operation({ status: 'queued' }))
  const restartByUser = vi.fn(async () => operation({ status: 'queued' }))
  const observer = createAccountErasureObserver({
    failureText: (error) => (error ? `reader text for ${error.type}` : null),
    onErased: (userId) => events.push(['erased', userId]),
    onProgress: (_userId, progress) => events.push(['progress', progress]),
    onResumed: () => undefined,
    onStopped: (_operationId, restart) => restarts.push(restart),
    read,
    readByUser,
    reopen,
    restartByUser,
    sleep: async (delayMs) => {
      delays.push(delayMs)
    },
  })
  return { delays, events, observer, read, readByUser, reopen, restartByUser, restarts }
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
      readByUser: async () => operation({ status: 'running' }),
      reopen: async () => operation({}),
      restartByUser: async () => operation({}),
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

describe('an erasure provisioning started', () => {
  it('is read through the account when its operation is not readable here', async () => {
    const h = harness(
      [notReadableHere()],
      [
        operation({ completed_items: 1, stage: 'aggregates_removed', status: 'running' }),
        // The erasure ends with the account itself: the account path never
        // answers `deleted`, it answers 404.
        notReadableHere(),
      ],
    )

    h.observer.observe('user-1', 'del_provisioning')

    await vi.waitFor(() => expect(h.events).toHaveLength(2))
    expect(h.events).toEqual([
      ['progress', expect.objectContaining({ phase: 'aggregates_removed', state: 'running' })],
      ['erased', 'user-1'],
    ])
    expect(h.read).toHaveBeenCalledTimes(1)
    expect(h.readByUser).toHaveBeenCalledTimes(2)
    expect(h.readByUser).toHaveBeenCalledWith('user-1', expect.anything())
  })

  it('is tried again by erasing the account again, not by reopening the operation', async () => {
    const h = harness(
      [notReadableHere()],
      [
        operation({
          error: { message: 'object store unreachable', type: 'server_error' },
          stage: 'delete_failed',
          status: 'delete_failed',
        }),
        notReadableHere(),
      ],
    )

    h.observer.observe('user-1', 'del_provisioning')
    await vi.waitFor(() => expect(h.events).toHaveLength(1))
    expect(h.events[0]).toEqual([
      'progress',
      expect.objectContaining({ error: 'reader text for server_error', state: 'failed' }),
    ])

    await h.observer.retry('user-1')

    await vi.waitFor(() => expect(h.events.at(-1)).toEqual(['erased', 'user-1']))
    expect(h.restartByUser).toHaveBeenCalledWith('user-1')
    expect(h.reopen).not.toHaveBeenCalled()
    // The repeated erasure is read through the account as well.
    expect(h.read).toHaveBeenCalledTimes(1)
  })

  it('reloads the list when the account names no erasure either', async () => {
    // Gone, never started, or another administrator's: only the list knows
    // which, and a list that drops the account ends the row.
    const h = harness([notReadableHere()], [notReadableHere()])

    h.observer.observe('user-1', 'del_of_another_admin')
    await vi.waitFor(() => expect(h.events).toHaveLength(1))

    expect(h.events).toEqual([['erased', 'user-1']])
    expect(h.readByUser).toHaveBeenCalledTimes(1)
  })

  it('offers to erase the account again while the reloaded list still names the erasure', async () => {
    const h = harness(
      [notReadableHere()],
      [notReadableHere(), operation({ stage: 'aggregates_removed', status: 'running' }), notReadableHere()],
    )

    // Erasing the account again resumes the erasure under the operation id
    // the list names (`del_1`, as the scripted DELETE answers).
    h.observer.observe('user-1', 'del_1')
    await vi.waitFor(() => expect(h.events).toEqual([['erased', 'user-1']]))

    // The reloaded list still names the erasure.
    h.observer.observe('user-1', 'del_1')

    expect(h.events.at(-1)).toEqual([
      'progress',
      expect.objectContaining({ operationId: 'del_1', state: 'unknown' }),
    ])
    // Following it again would reload the list without end.
    await new Promise((resolve) => setTimeout(resolve, 20))
    expect(h.readByUser).toHaveBeenCalledTimes(1)

    await h.observer.retry('user-1')

    expect(h.restartByUser).toHaveBeenCalledWith('user-1')
    expect(h.reopen).not.toHaveBeenCalled()
    // The resumed erasure is followed through the account until it ends.
    await vi.waitFor(() => expect(h.events.at(-1)).toEqual(['erased', 'user-1']))
    expect(h.events).toContainEqual([
      'progress',
      expect.objectContaining({ phase: 'aggregates_removed', state: 'running' }),
    ])
    expect(h.read).toHaveBeenCalledTimes(1)
  })
})
