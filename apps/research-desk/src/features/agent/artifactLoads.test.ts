import { describe, expect, it } from 'vitest'

import { shareArtifactLoad } from './useAgentControlApi'

const deferred = <T,>() => {
  let resolve: (value: T) => void = () => undefined
  let reject: (reason: unknown) => void = () => undefined
  const promise = new Promise<T>((settle, fail) => {
    resolve = settle
    reject = fail
  })
  return { promise, reject, resolve }
}

describe('artifact bodies on their way', () => {
  it('asks once for the latest body while it is on its way, and again afterwards', async () => {
    // An open canvas document asked for its body on every render while it
    // was missing: ten requests for one document after a reload.
    const loads = new Map<string, Promise<string>>()
    let started = 0
    const pending = deferred<string>()
    const start = () => { started += 1; return pending.promise }
    const first = shareArtifactLoad(loads, 'run', 'art', undefined, start)
    const second = shareArtifactLoad(loads, 'run', 'art', undefined, start)
    expect(started).toBe(1)
    pending.resolve('body')
    await expect(first).resolves.toBe('body')
    await expect(second).resolves.toBe('body')
    await Promise.resolve()
    await shareArtifactLoad(loads, 'run', 'art', undefined, async () => { started += 1; return 'newer' })
    expect(started).toBe(2)
  })

  it('keeps the body of a pinned revision, which never changes', async () => {
    // A revision diff fetched both revisions again on every event of a
    // running run.
    const loads = new Map<string, Promise<string>>()
    let started = 0
    const start = async () => { started += 1; return 'revision 2' }
    await shareArtifactLoad(loads, 'run', 'art', 2, start)
    await shareArtifactLoad(loads, 'run', 'art', 2, start)
    expect(started).toBe(1)
    await shareArtifactLoad(loads, 'run', 'art', 3, start)
    expect(started).toBe(2)
  })

  it('asks again for a pinned revision whose request failed', async () => {
    const loads = new Map<string, Promise<string>>()
    const failing = deferred<string>()
    const first = shareArtifactLoad(loads, 'run', 'art', 2, () => failing.promise)
    failing.reject(new Error('offline'))
    await expect(first).rejects.toThrow('offline')
    await Promise.resolve()
    let started = 0
    await shareArtifactLoad(loads, 'run', 'art', 2, async () => { started += 1; return 'revision 2' })
    expect(started).toBe(1)
  })
})
