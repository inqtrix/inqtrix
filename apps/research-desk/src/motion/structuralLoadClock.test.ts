import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  createStructuralLoadClock,
  scheduleAfterStructuralPaint,
  structuralLoadTiming,
} from './structuralLoadClock'

describe('structural load clock', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  function setup() {
    vi.useFakeTimers()
    vi.setSystemTime(0)
    const onFallbackRequested = vi.fn()
    const onRelease = vi.fn()
    const clock = createStructuralLoadClock({
      now: () => Date.now(),
      onFallbackRequested,
      onRelease,
    })
    clock.arm()
    return { clock, onFallbackRequested, onRelease }
  }

  it.each([50, 250, 750, 799])(
    'releases at %d ms without ever requesting a fallback',
    (elapsed) => {
      const { clock, onFallbackRequested, onRelease } = setup()
      vi.advanceTimersByTime(elapsed)
      clock.release()
      vi.runAllTimers()

      expect(onFallbackRequested).not.toHaveBeenCalled()
      expect(onRelease).toHaveBeenCalledOnce()
      expect(onRelease).toHaveBeenCalledWith(false)
    },
  )

  it('requests the fallback at the shared 800 ms threshold', () => {
    const { clock, onFallbackRequested, onRelease } = setup()
    vi.advanceTimersByTime(structuralLoadTiming.fallbackDelayMs - 1)
    expect(onFallbackRequested).not.toHaveBeenCalled()

    vi.advanceTimersByTime(1)
    expect(onFallbackRequested).toHaveBeenCalledOnce()
    expect(onRelease).not.toHaveBeenCalled()

    // Readiness before the requested fallback commits cancels the visual
    // state instead of manufacturing a single-frame flash.
    clock.release()
    expect(onRelease).toHaveBeenCalledWith(false)
  })

  it('holds a committed fallback for at least 300 ms', () => {
    const { clock, onRelease } = setup()
    vi.advanceTimersByTime(structuralLoadTiming.fallbackDelayMs)
    clock.fallbackCommitted()
    vi.advanceTimersByTime(100)
    clock.release()

    vi.advanceTimersByTime(structuralLoadTiming.minimumFallbackMs - 101)
    expect(onRelease).not.toHaveBeenCalled()
    vi.advanceTimersByTime(1)
    expect(onRelease).toHaveBeenCalledOnce()
    expect(onRelease).toHaveBeenCalledWith(true)
  })

  it('releases immediately when a slow target outlives the minimum', () => {
    const { clock, onRelease } = setup()
    vi.advanceTimersByTime(structuralLoadTiming.fallbackDelayMs)
    clock.fallbackCommitted()
    vi.advanceTimersByTime(700)
    clock.release()

    expect(onRelease).toHaveBeenCalledOnce()
    expect(onRelease).toHaveBeenCalledWith(true)
  })

  it('cancels every outstanding timer during a rapid identity change', () => {
    const first = setup()
    vi.advanceTimersByTime(structuralLoadTiming.fallbackDelayMs)
    first.clock.fallbackCommitted()
    first.clock.release()
    first.clock.cancel()
    vi.runAllTimers()

    expect(first.onRelease).not.toHaveBeenCalled()
  })
})

describe('cold target paint barrier', () => {
  it('mounts work only after two frames and cancels either pending handle', () => {
    const pending = new Map<number, () => void>()
    let nextId = 1
    vi.stubGlobal('requestAnimationFrame', (callback: () => void) => {
      const id = nextId++
      pending.set(id, callback)
      return id
    })
    vi.stubGlobal('cancelAnimationFrame', (id: number) => {
      pending.delete(id)
    })
    const fireFrame = () => {
      const callbacks = [...pending.values()]
      pending.clear()
      for (const callback of callbacks) callback()
    }
    const mount = vi.fn()

    scheduleAfterStructuralPaint(mount)
    fireFrame()
    expect(mount).not.toHaveBeenCalled()
    fireFrame()
    expect(mount).toHaveBeenCalledOnce()

    const cancelled = vi.fn()
    const cancel = scheduleAfterStructuralPaint(cancelled)
    fireFrame()
    cancel()
    fireFrame()
    expect(cancelled).not.toHaveBeenCalled()
    expect(pending.size).toBe(0)
  })
})
