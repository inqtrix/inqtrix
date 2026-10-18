/** Keys that scroll a focused scroll container; typing keys never release the lock. */
const SCROLL_INTENT_KEYS = new Set(['ArrowDown', 'ArrowUp', 'End', 'Home', 'PageDown', 'PageUp', ' '])

export function isScrollIntentKey(key: string): boolean {
  return SCROLL_INTENT_KEYS.has(key)
}

type RestoreAnchorLockOptions = {
  /** Re-apply the anchor (bottom, or remembered distance-from-bottom). */
  applyTarget: () => void
  clearTimeout: (handle: number) => void
  /** Tear down DOM wiring (observer disconnect) exactly once, on any release path. */
  onRelease: () => void
  quietMs: number
  safetyMs: number
  setTimeout: (handler: () => void, timeout: number) => number
}

export type RestoreAnchorLock = {
  isActive: () => boolean
  onContentResize: (blockSize: number) => void
  onUserScrollIntent: () => void
  release: () => void
  start: () => void
}

/**
 * State machine for the post-restore anchor hold of `useScrollRestoration`.
 *
 * While active it re-applies the scroll anchor from ResizeObserver callbacks
 * (before paint) so async content growth never paints an un-corrected frame,
 * and releases after the content has been quiet for `quietMs` (hard-capped at
 * `safetyMs`). Two rules distinguish it from a naive hold:
 *
 * - Only a HEIGHT change re-pins and re-arms the quiet window. A pure width
 *   reflow (window resize) cannot move a distance-from-bottom anchor, so it
 *   must neither yank `scrollTop` nor extend the hold.
 * - An explicit user scroll gesture releases immediately WITHOUT a final
 *   `applyTarget`, so the user's own scrolling always wins over the restore.
 *
 * Timers are injected so the machine is testable in the node environment.
 */
export function createRestoreAnchorLock(options: RestoreAnchorLockOptions): RestoreAnchorLock {
  let active = false
  let quietTimer: number | null = null
  let safetyTimer: number | null = null
  let lastBlockSize: number | null = null

  const release = () => {
    if (!active) return
    active = false
    if (quietTimer != null) options.clearTimeout(quietTimer)
    if (safetyTimer != null) options.clearTimeout(safetyTimer)
    quietTimer = null
    safetyTimer = null
    options.onRelease()
  }

  // Final applyTarget as the lock ends so a burst of growth landing right at
  // the quiet edge still settles exactly on target rather than a frame short.
  const settleRelease = () => {
    if (!active) return
    options.applyTarget()
    release()
  }

  const scheduleQuietRelease = () => {
    if (quietTimer != null) options.clearTimeout(quietTimer)
    quietTimer = options.setTimeout(settleRelease, options.quietMs)
  }

  return {
    isActive: () => active,
    onContentResize: (blockSize) => {
      if (!active) return
      if (lastBlockSize !== null && blockSize === lastBlockSize) return
      lastBlockSize = blockSize
      options.applyTarget()
      scheduleQuietRelease()
    },
    onUserScrollIntent: release,
    release,
    start: () => {
      active = true
      scheduleQuietRelease()
      safetyTimer = options.setTimeout(settleRelease, options.safetyMs)
    },
  }
}
