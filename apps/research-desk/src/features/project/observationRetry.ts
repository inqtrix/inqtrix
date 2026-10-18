import { useRef, useState } from 'react'

/**
 * The one rule for status polls that keep failing: the wait doubles from the
 * poll's own base to a cap, and after a bounded number of failed reads in a
 * row the poll stops until the browser is online again or the tab is visible
 * again. An unreachable server never gets an endless stream of requests.
 */

/** Failed reads in a row after which a status poll stops. */
export const OBSERVATION_MAX_FAILURES = 10

/** The longest wait between two failed reads. */
export const OBSERVATION_MAX_DELAY_MS = 30_000

/** The wait after `failures` failed reads in a row (from 1), or null once the poll stops. */
export function observationRetryDelay(failures: number, baseMs: number): number | null {
  if (failures >= OBSERVATION_MAX_FAILURES) return null
  return Math.min(OBSERVATION_MAX_DELAY_MS, baseMs * 2 ** Math.max(0, failures - 1))
}

type RestartCueTargets = {
  isVisible: () => boolean
  online: EventTarget
  visibility: EventTarget
}

function browserTargets(): RestartCueTargets {
  return {
    isVisible: () => document.visibilityState === 'visible',
    online: window,
    visibility: document,
  }
}

export type StoppedObservations = {
  /** Forget every stopped poll; none of them restarts. */
  clear: () => void
  /** Forget one stopped poll without restarting it: it restarted another way or ended. */
  drop: (key: string) => void
  /** Note a poll that stopped and how it starts again. */
  stop: (key: string, restart: () => void) => void
}

/**
 * The stopped polls of one hook. While any poll is stopped it listens for
 * the browser to be online again or the tab to be visible again, then starts
 * each stopped poll once more; `onChange` hears whether any poll is stopped.
 */
export function createStoppedObservations(
  onChange: (stopped: boolean) => void,
  targets: RestartCueTargets = browserTargets(),
): StoppedObservations {
  const restarts = new Map<string, () => void>()
  let listening = false

  const listen = (on: boolean) => {
    if (on === listening) return
    listening = on
    if (on) {
      targets.online.addEventListener('online', restartAll)
      targets.visibility.addEventListener('visibilitychange', onVisibility)
    } else {
      targets.online.removeEventListener('online', restartAll)
      targets.visibility.removeEventListener('visibilitychange', onVisibility)
    }
  }
  const update = () => {
    listen(restarts.size > 0)
    onChange(restarts.size > 0)
  }
  const restartAll = () => {
    const pending = [...restarts.values()]
    restarts.clear()
    update()
    pending.forEach((restart) => restart())
  }
  const onVisibility = () => {
    if (targets.isVisible()) restartAll()
  }

  return {
    clear: () => {
      if (restarts.size === 0) return
      restarts.clear()
      update()
    },
    drop: (key) => {
      if (restarts.delete(key)) update()
    },
    stop: (key, restart) => {
      restarts.set(key, restart)
      update()
    },
  }
}

/** A hook's stopped polls, and whether any of them is stopped. */
export function useStoppedObservations(): {
  observationStopped: boolean
  stoppedObservations: StoppedObservations
} {
  const [observationStopped, setObservationStopped] = useState(false)
  const stoppedRef = useRef<StoppedObservations | null>(null)
  if (stoppedRef.current === null) {
    stoppedRef.current = createStoppedObservations(setObservationStopped)
  }
  return { observationStopped, stoppedObservations: stoppedRef.current }
}
