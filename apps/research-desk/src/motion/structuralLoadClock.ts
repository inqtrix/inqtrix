export const structuralLoadTiming = {
  fallbackDelayMs: 800,
  minimumFallbackMs: 300,
} as const

type TimerHandle = ReturnType<typeof setTimeout>

type StructuralLoadClockOptions = {
  onFallbackRequested: () => void
  onRelease: (fallbackWasShown: boolean) => void
  now?: () => number
  setTimer?: (callback: () => void, delayMs: number) => TimerHandle
  clearTimer?: (handle: TimerHandle) => void
}

export type StructuralLoadClock = {
  arm: () => void
  cancel: () => void
  fallbackCommitted: () => void
  release: () => void
}

/** Runs work only after the current shell has had an opportunity to paint.
 * Pending cold targets use this barrier so their first expensive render cannot
 * delay the shell. Ready cache hits intentionally bypass it. */
export function scheduleAfterStructuralPaint(callback: () => void): () => void {
  let secondFrame = 0
  const firstFrame = requestAnimationFrame(() => {
    secondFrame = requestAnimationFrame(callback)
  })
  return () => {
    cancelAnimationFrame(firstFrame)
    cancelAnimationFrame(secondFrame)
  }
}

/** Owns the two timing rules for a structural loading cycle.
 *
 * Requesting a fallback and committing it are deliberately separate. The
 * minimum duration starts when React commits the fallback, not when the
 * delayed timer merely asks React to render it. A target that becomes ready
 * in that small window can therefore still complete without flashing a
 * fallback that never reached a paint.
 */
export function createStructuralLoadClock({
  onFallbackRequested,
  onRelease,
  now = () => performance.now(),
  setTimer = setTimeout,
  clearTimer = clearTimeout,
}: StructuralLoadClockOptions): StructuralLoadClock {
  type ClockState = 'idle' | 'waiting' | 'requested' | 'visible' | 'releasing' | 'released' | 'cancelled'

  let state: ClockState = 'idle'
  let delayTimer: TimerHandle | null = null
  let minimumTimer: TimerHandle | null = null
  let fallbackCommittedAt: number | null = null

  const clearDelayTimer = () => {
    if (delayTimer === null) return
    clearTimer(delayTimer)
    delayTimer = null
  }

  const clearMinimumTimer = () => {
    if (minimumTimer === null) return
    clearTimer(minimumTimer)
    minimumTimer = null
  }

  const finish = (fallbackWasShown: boolean) => {
    if (state === 'released' || state === 'cancelled') return
    clearDelayTimer()
    clearMinimumTimer()
    state = 'released'
    onRelease(fallbackWasShown)
  }

  return {
    arm() {
      if (state !== 'idle') return
      state = 'waiting'
      delayTimer = setTimer(() => {
        delayTimer = null
        if (state !== 'waiting') return
        state = 'requested'
        onFallbackRequested()
      }, structuralLoadTiming.fallbackDelayMs)
    },

    fallbackCommitted() {
      if (state !== 'requested') return
      state = 'visible'
      fallbackCommittedAt = now()
    },

    release() {
      if (state === 'released' || state === 'releasing' || state === 'cancelled') return

      // A requested fallback that has not committed can still be cancelled
      // without producing a one-frame flash.
      if (state === 'idle' || state === 'waiting' || state === 'requested') {
        finish(false)
        return
      }

      const elapsed = fallbackCommittedAt === null ? 0 : now() - fallbackCommittedAt
      const remaining = Math.max(0, structuralLoadTiming.minimumFallbackMs - elapsed)
      if (remaining === 0) {
        finish(true)
        return
      }

      state = 'releasing'
      minimumTimer = setTimer(() => {
        minimumTimer = null
        finish(true)
      }, remaining)
    },

    cancel() {
      if (state === 'cancelled') return
      clearDelayTimer()
      clearMinimumTimer()
      state = 'cancelled'
    },
  }
}
