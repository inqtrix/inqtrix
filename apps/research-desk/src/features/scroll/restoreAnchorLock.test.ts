import { describe, expect, it } from 'vitest'

import { createRestoreAnchorLock, isScrollIntentKey } from './restoreAnchorLock'

type TimerHarness = {
  clearTimeout: (handle: number) => void
  fire: (handle: number) => void
  pending: () => number[]
  setTimeout: (handler: () => void, timeout: number) => number
  timeoutOf: (handle: number) => number
}

function createTimerHarness(): TimerHarness {
  let nextHandle = 1
  const timers = new Map<number, { handler: () => void, timeout: number }>()
  return {
    clearTimeout: (handle) => {
      timers.delete(handle)
    },
    fire: (handle) => {
      const timer = timers.get(handle)
      if (!timer) throw new Error(`timer ${handle} is not pending`)
      timers.delete(handle)
      timer.handler()
    },
    pending: () => [...timers.keys()],
    setTimeout: (handler, timeout) => {
      const handle = nextHandle++
      timers.set(handle, { handler, timeout })
      return handle
    },
    timeoutOf: (handle) => {
      const timer = timers.get(handle)
      if (!timer) throw new Error(`timer ${handle} is not pending`)
      return timer.timeout
    },
  }
}

function createLockHarness() {
  const timers = createTimerHarness()
  let applyCount = 0
  let releaseCount = 0
  const lock = createRestoreAnchorLock({
    applyTarget: () => {
      applyCount += 1
    },
    clearTimeout: timers.clearTimeout,
    onRelease: () => {
      releaseCount += 1
    },
    quietMs: 90,
    safetyMs: 1000,
    setTimeout: timers.setTimeout,
  })
  return {
    applyCount: () => applyCount,
    lock,
    releaseCount: () => releaseCount,
    timers,
    fireQuiet: () => {
      const quiet = timers.pending().find((handle) => timers.timeoutOf(handle) === 90)
      if (quiet == null) throw new Error('no quiet timer pending')
      timers.fire(quiet)
    },
    fireSafety: () => {
      const safety = timers.pending().find((handle) => timers.timeoutOf(handle) === 1000)
      if (safety == null) throw new Error('no safety timer pending')
      timers.fire(safety)
    },
  }
}

describe('createRestoreAnchorLock', () => {
  it('pins on the first content size, then settles once the quiet window elapses', () => {
    const h = createLockHarness()
    h.lock.start()
    h.lock.onContentResize(600)
    expect(h.applyCount()).toBe(1)

    h.fireQuiet()
    // Settle applies the anchor one final time, then releases exactly once.
    expect(h.applyCount()).toBe(2)
    expect(h.lock.isActive()).toBe(false)
    expect(h.releaseCount()).toBe(1)
  })

  it('re-pins and re-arms the quiet window on every height growth (streaming)', () => {
    const h = createLockHarness()
    h.lock.start()
    h.lock.onContentResize(600)
    h.lock.onContentResize(900)
    h.lock.onContentResize(1200)
    expect(h.applyCount()).toBe(3)
    expect(h.lock.isActive()).toBe(true)
    // Exactly one quiet timer pending (each growth replaced the previous one).
    expect(h.timers.pending().filter((handle) => h.timers.timeoutOf(handle) === 90)).toHaveLength(1)
  })

  it('ignores same-height callbacks (pure width reflow neither pins nor extends)', () => {
    const h = createLockHarness()
    h.lock.start()
    h.lock.onContentResize(600)
    const pinsAfterGrowth = h.applyCount()
    const quietHandle = h.timers.pending().find((handle) => h.timers.timeoutOf(handle) === 90)

    h.lock.onContentResize(600)
    expect(h.applyCount()).toBe(pinsAfterGrowth)
    // The pre-armed quiet timer is untouched, so the hold still ends on schedule.
    expect(h.timers.pending().find((handle) => h.timers.timeoutOf(handle) === 90)).toBe(quietHandle)
  })

  it('releases immediately on user scroll intent without yanking back to the anchor', () => {
    const h = createLockHarness()
    h.lock.start()
    h.lock.onContentResize(600)
    const pinsBeforeIntent = h.applyCount()

    h.lock.onUserScrollIntent()
    expect(h.lock.isActive()).toBe(false)
    expect(h.releaseCount()).toBe(1)
    // No settle applyTarget: the user's gesture wins.
    expect(h.applyCount()).toBe(pinsBeforeIntent)
    // Late observer callbacks after release are no-ops.
    h.lock.onContentResize(900)
    expect(h.applyCount()).toBe(pinsBeforeIntent)
  })

  it('the safety cap settles and releases even while growth keeps re-arming', () => {
    const h = createLockHarness()
    h.lock.start()
    h.lock.onContentResize(600)
    h.lock.onContentResize(900)
    const pinsBeforeSafety = h.applyCount()

    h.fireSafety()
    expect(h.applyCount()).toBe(pinsBeforeSafety + 1)
    expect(h.lock.isActive()).toBe(false)
    expect(h.releaseCount()).toBe(1)
  })

  it('release is idempotent and clears all pending timers', () => {
    const h = createLockHarness()
    h.lock.start()
    h.lock.onContentResize(600)
    h.lock.release()
    h.lock.release()
    expect(h.releaseCount()).toBe(1)
    expect(h.timers.pending()).toHaveLength(0)
  })
})

describe('isScrollIntentKey', () => {
  it('matches scroll keys and rejects typing keys', () => {
    expect(isScrollIntentKey('PageDown')).toBe(true)
    expect(isScrollIntentKey(' ')).toBe(true)
    expect(isScrollIntentKey('a')).toBe(false)
    expect(isScrollIntentKey('Enter')).toBe(false)
  })
})
