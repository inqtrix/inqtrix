import { useLayoutEffect, useRef, type RefObject } from 'react'
import { useReducedMotion } from 'motion/react'

/**
 * Scroll policy for "bring the active match/citation into view" surfaces
 * (knowledge evidence reader, fullscreen document viewer).
 *
 * The rule mirrors the codified convention of `scrollMetrics.ts` ("a
 * switch is never animated"): the FIRST landing on a freshly shown
 * content surface positions instantly — the reader has no reference
 * point yet, so animating the travel from scrollTop 0 only showcases
 * intermediate content. Only later, user-driven match stepping inside
 * the same surface scrolls smoothly, and even that falls back to
 * instant under `prefers-reduced-motion`.
 */
export function activeMatchScrollDecision(options: {
  /** Identity of the shown content surface (document + view). */
  contentKey: string
  /** The surface the previous scroll landed on; null = nothing landed yet. */
  landedKey: string | null
  reducedMotion: boolean
}): { behavior: ScrollBehavior; initial: boolean } {
  const initial = options.landedKey !== options.contentKey
  return {
    behavior: initial || options.reducedMotion ? 'auto' : 'smooth',
    initial,
  }
}

/**
 * Scroll the active match element into view: instantly (pre-paint) on the
 * first landing per `contentKey`, smoothly on subsequent `activeIndex`
 * steps within the same surface. Leaving the surface (`enabled` false —
 * tab switch, document unloads) resets the landing, so every re-entry is
 * again an instant landing (the remount starts at scrollTop 0).
 */
export function useActiveMatchScroll({
  activeIndex,
  contentKey,
  enabled,
  targetRef,
}: {
  activeIndex: number
  contentKey: string
  enabled: boolean
  targetRef: RefObject<HTMLElement | null>
}) {
  const reducedMotion = Boolean(useReducedMotion())
  const landedKeyRef = useRef<string | null>(null)
  useLayoutEffect(() => {
    if (!enabled) {
      landedKeyRef.current = null
      return
    }
    const node = targetRef.current
    if (!node) return
    const decision = activeMatchScrollDecision({
      contentKey,
      landedKey: landedKeyRef.current,
      reducedMotion,
    })
    landedKeyRef.current = contentKey
    node.scrollIntoView({ behavior: decision.behavior, block: 'center' })
  }, [activeIndex, contentKey, enabled, reducedMotion, targetRef])
}
