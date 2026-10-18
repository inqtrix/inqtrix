import { useCallback, useLayoutEffect, useMemo, useRef } from 'react'

import { createRestoreAnchorLock, isScrollIntentKey } from './restoreAnchorLock'
import {
  distanceFromBottom,
  isNearBottom,
  readScrollMetrics,
  scrollFollowModeForUpdate,
  SCROLL_AUTO_FOLLOW_THRESHOLD_PX,
} from './scrollMetrics'
import { readScrollMemory, writeScrollMemory, type ScrollMemoryEntry } from './scrollMemory'

/** Debounce for persisting the user's scroll position — long enough to coalesce
 * a scroll gesture, short enough to survive an immediate thread switch. */
const MEMORY_WRITE_DEBOUNCE_MS = 120
/** After content stops growing for this long, the restore lock releases. The
 * ResizeObserver fires before paint, so held frames are never shown mid-move. */
const LOCK_QUIET_MS = 90
/** Hard cap so a pathologically never-quiescent view can't hold the lock (and
 * suppress the user's own scrolls) forever. Never drives visible motion — the
 * observer already holds the anchor every frame. */
const LOCK_SAFETY_MS = 1000

type ScrollRestorationOptions = {
  /** Namespaced per-conversation identity (`chat:<id>` / `knowledge:<id>` / …).
   * `null` disables restoration (e.g. no active thread, non-ask mode). */
  memoryKey: string | null
  /** Resolve the scrollable Radix viewport from the surface's container. */
  getViewport: () => HTMLElement | null
  /** False while the visible surface is a lazy-load placeholder rather than the
   * actual conversation. Restore attaches only once real content can be measured. */
  contentReady?: boolean
  /** True while a message/answer is actively streaming — keeps auto-follow at
   * the bottom without smoothing every token. */
  isStreaming: boolean
  reduceMotion: boolean | null
  /** Near-bottom threshold override; defaults to the shared 96px. */
  nearBottomThresholdPx?: number
}

type ScrollRestorationHandle = {
  /** Run the follow decision when a new message/answer lands. `instant`
   * skips the smooth scroll for the one update that only settles content
   * the user already watched stream (a run finishing): smoothing there
   * reads as the answer moving after it was done. */
  onContentAppended: (options?: { instant?: boolean }) => void
  /** Force an instant jump to the bottom (composer send). */
  scrollToBottom: () => void
}

/**
 * Restore and remember the scroll position of a chat/knowledge message view.
 *
 * Root mechanism (why there is no rAF/timeout ladder): the position is held
 * from inside a `ResizeObserver` callback, which runs after layout and before
 * paint. Correcting there means the grown-and-corrected state is the only state
 * ever painted, so async markdown/code-highlight growth does not produce a
 * visible downward crawl on the first (cold) view. The lock releases once the
 * content has been quiet for one short window — not after a fixed frame budget.
 * Two hold rules live in restoreAnchorLock.ts: pure width reflows (window
 * resize) never re-pin or extend the hold, and an explicit user gesture
 * (wheel/touchmove/scroll key) releases it immediately. Dragging the scrollbar
 * thumb is intentionally not an intent event — worst case the hold lasts the
 * quiet window, hard-capped at the safety timeout.
 *
 * Position memory is distance-from-bottom + a pinned flag, kept in a module
 * singleton (see scrollMemory.ts): restores are instant (`auto`, never smooth),
 * survive in-session desk switches, and reset on a full reload.
 */
export function useScrollRestoration({
  memoryKey,
  getViewport,
  contentReady = true,
  isStreaming,
  reduceMotion,
  nearBottomThresholdPx = SCROLL_AUTO_FOLLOW_THRESHOLD_PX,
}: ScrollRestorationOptions): ScrollRestorationHandle {
  const getViewportRef = useRef(getViewport)
  const memoryKeyRef = useRef(memoryKey)
  const contentReadyRef = useRef(contentReady)
  const isStreamingRef = useRef(isStreaming)
  const reduceMotionRef = useRef(reduceMotion)
  const thresholdRef = useRef(nearBottomThresholdPx)
  const activeKeyRef = useRef<string | null>(null)
  // Whether the user was following at the bottom BEFORE the latest content grew.
  // Updated only by real user scrolls (and the restore target); content growth
  // never touches it, so onContentAppended can decide "follow?" on the
  // pre-growth state instead of measuring the already-grown DOM.
  const followingRef = useRef(true)
  const pendingScrollToBottomKeyRef = useRef<string | null>(null)

  getViewportRef.current = getViewport
  memoryKeyRef.current = memoryKey
  contentReadyRef.current = contentReady
  isStreamingRef.current = isStreaming
  reduceMotionRef.current = reduceMotion
  thresholdRef.current = nearBottomThresholdPx

  const scrollToBottom = useCallback(() => {
    const viewport = getViewportRef.current()
    const key = memoryKeyRef.current
    if (!contentReadyRef.current || !viewport) {
      pendingScrollToBottomKeyRef.current = key
      return
    }
    pendingScrollToBottomKeyRef.current = null
    viewport.scrollTop = viewport.scrollHeight
    followingRef.current = true
    if (key) writeScrollMemory(key, { distanceFromBottom: 0, pinnedToBottom: true })
  }, [])

  const onContentAppended = useCallback((options?: { instant?: boolean }) => {
    if (!contentReadyRef.current) return
    const viewport = getViewportRef.current()
    if (!viewport) return
    const key = activeKeyRef.current
    const mode = scrollFollowModeForUpdate({
      hasActiveContent: isStreamingRef.current || options?.instant === true,
      keyChanged: false,
      // Decide on the PRE-growth follow state, not the just-grown DOM: a large
      // chunk that adds >threshold px must not read as "user scrolled away".
      nearBottom: followingRef.current,
      reduceMotion: reduceMotionRef.current,
    })
    if (mode === 'none') {
      // The user has scrolled up — keep their position, just refresh the memo.
      if (key) {
        writeScrollMemory(key, {
          distanceFromBottom: Math.max(0, distanceFromBottom(readScrollMetrics(viewport))),
          pinnedToBottom: false,
        })
      }
      return
    }
    if (mode === 'smooth') {
      viewport.scrollTo({ behavior: 'smooth', top: viewport.scrollHeight })
    } else {
      viewport.scrollTop = viewport.scrollHeight
    }
    followingRef.current = true
    if (key) writeScrollMemory(key, { distanceFromBottom: 0, pinnedToBottom: true })
  }, [])

  useLayoutEffect(() => {
    const key = memoryKey
    if (!contentReady) {
      activeKeyRef.current = null
      followingRef.current = true
      return undefined
    }
    activeKeyRef.current = key
    const viewport = getViewportRef.current()
    if (!key || !viewport) return undefined

    const content = viewport.firstElementChild as HTMLElement | null
    const forceBottom = pendingScrollToBottomKeyRef.current === key
    const memo = readScrollMemory(key)
    // `undefined`/pinned -> hold the bottom; otherwise hold the remembered
    // distance-from-bottom so async growth above doesn't shift the anchor.
    const target: 'bottom' | number = forceBottom || !memo || memo.pinnedToBottom
      ? 'bottom'
      : memo.distanceFromBottom
    followingRef.current = target === 'bottom'

    const applyTarget = () => {
      if (target === 'bottom') {
        viewport.scrollTop = viewport.scrollHeight
        return
      }
      viewport.scrollTop = Math.max(0, viewport.scrollHeight - viewport.clientHeight - target)
    }

    // Synchronous first application, before this commit paints.
    applyTarget()
    if (forceBottom) {
      pendingScrollToBottomKeyRef.current = null
      writeScrollMemory(key, { distanceFromBottom: 0, pinnedToBottom: true })
    }

    let observer: ResizeObserver | null = null
    const lock = createRestoreAnchorLock({
      applyTarget,
      clearTimeout: (handle) => window.clearTimeout(handle),
      onRelease: () => {
        observer?.disconnect()
        observer = null
      },
      quietMs: LOCK_QUIET_MS,
      safetyMs: LOCK_SAFETY_MS,
      setTimeout: (handler, timeout) => window.setTimeout(handler, timeout),
    })
    lock.start()

    if (content && typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver((entries) => {
        lock.onContentResize(observedBlockSize(entries, content))
      })
      observer.observe(content)
    } else {
      // No observer available: apply once more next frame, then stop holding.
      window.requestAnimationFrame(() => {
        applyTarget()
        lock.release()
      })
    }

    // An active user gesture ends the hold immediately — programmatic scrollTop
    // writes never fire these events, so they are unambiguous user intent.
    const onUserScrollIntent = () => {
      if (lock.isActive()) lock.onUserScrollIntent()
    }
    const onScrollIntentKey = (event: KeyboardEvent) => {
      if (!isScrollIntentKey(event.key)) return
      if (isEditableEventTarget(event.target)) return
      onUserScrollIntent()
    }
    viewport.addEventListener('wheel', onUserScrollIntent, { passive: true })
    viewport.addEventListener('touchmove', onUserScrollIntent, { passive: true })
    viewport.addEventListener('keydown', onScrollIntentKey)

    let writeTimer: number | null = null
    let pendingEntry: ScrollMemoryEntry | null = null
    const onScroll = () => {
      // Ignore the scroll events our own lock produces; only remember the user's.
      if (lock.isActive()) return
      const metrics = readScrollMetrics(viewport)
      const atBottom = isNearBottom(metrics, thresholdRef.current)
      // Track the live follow state from real user scrolls only (this handler is
      // skipped while the lock drives programmatic scrolls), so onContentAppended
      // reads the user's intent, not a transient position.
      followingRef.current = atBottom
      // Capture synchronously while THIS conversation is on screen. The flush on
      // cleanup uses this snapshot rather than re-reading the viewport, because
      // the ScrollArea element is reused and already shows the next conversation
      // by the time cleanup runs (which would persist a wrong position here).
      pendingEntry = {
        distanceFromBottom: Math.max(0, distanceFromBottom(metrics)),
        pinnedToBottom: atBottom,
      }
      if (writeTimer != null) window.clearTimeout(writeTimer)
      writeTimer = window.setTimeout(() => {
        writeTimer = null
        if (pendingEntry) writeScrollMemory(key, pendingEntry)
      }, MEMORY_WRITE_DEBOUNCE_MS)
    }
    viewport.addEventListener('scroll', onScroll, { passive: true })

    return () => {
      viewport.removeEventListener('scroll', onScroll)
      viewport.removeEventListener('wheel', onUserScrollIntent)
      viewport.removeEventListener('touchmove', onUserScrollIntent)
      viewport.removeEventListener('keydown', onScrollIntentKey)
      if (writeTimer != null) window.clearTimeout(writeTimer)
      lock.release()
      // Flush the last position captured while this conversation was shown.
      if (pendingEntry) writeScrollMemory(key, pendingEntry)
    }
  }, [contentReady, memoryKey])

  // Stable handle so consumers can list it in effect deps without re-running
  // onContentAppended on every render (both callbacks are already stable).
  return useMemo(() => ({ onContentAppended, scrollToBottom }), [onContentAppended, scrollToBottom])
}

function observedBlockSize(entries: ResizeObserverEntry[], fallback: HTMLElement): number {
  const entry = entries[entries.length - 1]
  const boxSize = entry?.borderBoxSize?.[0]
  if (boxSize) return boxSize.blockSize
  if (entry) return entry.contentRect.height
  return fallback.offsetHeight
}

/** Typing a space (or arrows) in an inline edit field is not a scroll gesture. */
function isEditableEventTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false
  return target.isContentEditable
    || target instanceof HTMLInputElement
    || target instanceof HTMLTextAreaElement
}
