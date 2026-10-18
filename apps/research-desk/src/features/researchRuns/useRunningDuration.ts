import { useEffect, useState } from 'react'

import { formatDuration } from '@/lib/time'

/**
 * Live "HH:MM:SS" elapsed time for a running job, ticking once per second.
 *
 * Returns a static "00:00:00" for non-running states or a missing start time.
 * Shared by the research-desk job cards and the report panel's live view so the
 * counter behaves identically in both places.
 *
 * Args:
 *   status: The run status; the timer ticks only while it is ``'running'``.
 *   startedAtIso: ISO start timestamp; the elapsed time is measured from it.
 */
export function useRunningDuration(status: string, startedAtIso?: string): string {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    // Only tick once there is a start time to measure from — and re-arm when it
    // arrives (it lands one render after `status` flips to running for a fresh
    // run, so `startedAtIso` must be a dependency).
    if (status !== 'running' || !startedAtIso) return undefined
    const intervalId = window.setInterval(() => setNow(Date.now()), 1000)
    return () => window.clearInterval(intervalId)
  }, [status, startedAtIso])

  if (status !== 'running' || !startedAtIso) return '00:00:00'
  return formatDuration((now - new Date(startedAtIso).getTime()) / 1000)
}
