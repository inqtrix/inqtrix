import { describe, expect, it } from 'vitest'

import { createMutationLane } from './mutationLane'

function deferred<T = void>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, reject, resolve }
}

describe('createMutationLane', () => {
  it('holds a queued task until the running one settles', async () => {
    const lane = createMutationLane()
    const first = deferred()
    const events: string[] = []

    const running = lane.run(async () => {
      events.push('save:start')
      await first.promise
      events.push('save:end')
    })
    const queued = lane.run(async () => {
      events.push('delete:start')
      events.push('delete:end')
    })

    // The autosave is in flight; the deletion must not have touched the
    // server yet — that overlap is what resurrects a deleted conversation.
    await Promise.resolve()
    expect(events).toEqual(['save:start'])

    first.resolve()
    await running
    await queued
    expect(events).toEqual(['save:start', 'save:end', 'delete:start', 'delete:end'])
  })

  it('runs a task queued behind a failing one and reports the failure to its own caller', async () => {
    const lane = createMutationLane()
    const failing = deferred()
    const order: string[] = []

    const rejected = lane.run(async () => {
      order.push('save')
      await failing.promise
    })
    const after = lane.run(async () => {
      order.push('delete')
      return 'deleted'
    })

    failing.reject(new Error('server refused'))
    await expect(rejected).rejects.toThrow('server refused')
    await expect(after).resolves.toBe('deleted')
    expect(order).toEqual(['save', 'delete'])
  })

  it('keeps submission order across many tasks', async () => {
    const lane = createMutationLane()
    const seen: number[] = []
    await Promise.all([1, 2, 3, 4, 5].map((n) => lane.run(async () => {
      await Promise.resolve()
      seen.push(n)
    })))
    expect(seen).toEqual([1, 2, 3, 4, 5])
  })
})
