import { useEffect, useRef } from 'react'
import type { CanvasState, CanvasViewDescriptor } from './types'
import { activeCanvasView, sameCanvasView } from './types'

/**
 * Follow-the-agent timing discipline — the calm rules:
 *
 * - 4s minimum dwell per view; incoming targets COALESCE (only the latest
 *   fires when the dwell expires).
 * - Pointer/scroll/selection within the last 3s defers a switch; 10s of
 *   continuous deferral auto-PINS (the user is clearly working here).
 * - 8s hysteresis against switching BACK to a view just left — except the
 *   document during synthesis (urgency `synthesis`), which always wins.
 * - Never opens a closed canvas except urgency `auto-open` (the reducer
 *   additionally enforces the once-only document rule).
 *
 * The hook only ever calls `openView(descriptor)` — pin/stack/auto-open
 * invariants stay in the reducer, so a rogue timer cannot violate them.
 */

const DWELL_MS = 4_000
const INTERACTION_QUIET_MS = 3_000
const DEFER_AUTO_PIN_MS = 10_000
const HYSTERESIS_MS = 8_000

export type CanvasFollowTarget = {
  descriptor: CanvasViewDescriptor
  urgency: 'auto-open' | 'open-only' | 'synthesis'
}

export function useCanvasFollow({
  canvas,
  enabled,
  onAutoPin,
  openView,
  target,
}: {
  canvas: CanvasState
  /** Follow default on; mobile passes false. */
  enabled: boolean
  /** 10s of user-blocked switching -> auto-pin (reducer action). */
  onAutoPin: () => void
  /** Dispatches `openAgentCanvasView` with source 'agent'. */
  openView: (descriptor: CanvasViewDescriptor) => void
  target: CanvasFollowTarget | null
}) {
  const lastInteractionAtRef = useRef(0)
  const lastSwitchAtRef = useRef(0)
  const deferringSinceRef = useRef<number | null>(null)
  const leftViewsRef = useRef(new Map<string, number>())
  const timerRef = useRef<number | null>(null)
  const stateRef = useRef({ canvas, onAutoPin, openView, target })
  stateRef.current = { canvas, onAutoPin, openView, target }

  // Interaction listeners live on window: any pointer/scroll/selection
  // activity postpones agent-driven switches (the user is reading).
  useEffect(() => {
    if (!enabled) return undefined
    const bump = () => {
      lastInteractionAtRef.current = Date.now()
    }
    window.addEventListener('pointerdown', bump, { passive: true })
    window.addEventListener('wheel', bump, { passive: true })
    document.addEventListener('selectionchange', bump)
    return () => {
      window.removeEventListener('pointerdown', bump)
      window.removeEventListener('wheel', bump)
      document.removeEventListener('selectionchange', bump)
    }
  }, [enabled])

  useEffect(() => {
    if (!enabled) return undefined

    const evaluate = () => {
      timerRef.current = null
      const { canvas: current, onAutoPin: autoPin, openView: open, target: desired } =
        stateRef.current
      if (!desired || current.pinned) {
        deferringSinceRef.current = null
        return
      }
      const active = activeCanvasView(current)
      if (sameCanvasView(active, desired.descriptor)) {
        deferringSinceRef.current = null
        return
      }
      // A closed canvas may open for the memo moments (`auto-open` at
      // completion, `synthesis` while the memo streams in — the plan's
      // "first artifact.created" auto-open); `open-only` targets never
      // open it. The reducer still enforces the once-only document rule.
      if (!current.open && desired.urgency === 'open-only') return

      const now = Date.now()
      const synthesis = desired.urgency === 'synthesis'

      // Hysteresis: do not bounce back to a view we just navigated away
      // from (synthesis excepted).
      const leftAt = leftViewsRef.current.get(viewKey(desired.descriptor))
      if (!synthesis && leftAt && now - leftAt < HYSTERESIS_MS) {
        schedule(HYSTERESIS_MS - (now - leftAt))
        return
      }
      // Minimum dwell on the current view.
      const sinceSwitch = now - lastSwitchAtRef.current
      if (!synthesis && sinceSwitch < DWELL_MS) {
        schedule(DWELL_MS - sinceSwitch)
        return
      }
      // Recent user interaction defers; sustained deferral auto-pins.
      const sinceInteraction = now - lastInteractionAtRef.current
      if (!synthesis && sinceInteraction < INTERACTION_QUIET_MS) {
        if (deferringSinceRef.current === null) {
          deferringSinceRef.current = now
        } else if (now - deferringSinceRef.current >= DEFER_AUTO_PIN_MS) {
          deferringSinceRef.current = null
          autoPin()
          return
        }
        schedule(INTERACTION_QUIET_MS - sinceInteraction)
        return
      }

      deferringSinceRef.current = null
      if (active) leftViewsRef.current.set(viewKey(active), now)
      lastSwitchAtRef.current = now
      open(desired.descriptor)
    }

    const schedule = (delayMs: number) => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current)
      timerRef.current = window.setTimeout(evaluate, Math.max(delayMs, 50))
    }

    evaluate()
    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current)
        timerRef.current = null
      }
    }
    // Re-evaluate whenever the desired target or canvas state changes;
    // coalescing happens naturally (only the LATEST target is in the ref).
  }, [enabled, target, canvas])
}

function viewKey(descriptor: CanvasViewDescriptor): string {
  return JSON.stringify(descriptor)
}
