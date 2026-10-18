import { useCallback, useEffect, useRef, useState } from 'react'
import { fetchQuotaUsage } from '@/api/inqtrixClient'
import { quotaRefreshDue, type QuotaDimensionUsage } from './model'

export type QuotaUsageState = {
  error: string | null
  rows: QuotaDimensionUsage[]
  status: 'error' | 'idle' | 'loading' | 'ready'
}

/** Server-truth quota usage for the signed-in caller (the composer meter).

 * Loads once when *enabled* and refreshes on a slow interval so the meter
 * tracks consumption without per-keystroke chatter. Every automatic
 * refresh (the interval and a tab becoming visible again) goes through
 * one rule, {@link quotaRefreshDue}: at most one request in flight and at
 * least REFRESH_MS between attempts, so a tab that switches visibility
 * every few seconds still costs at most two requests a minute. The whole
 * meter is gated upstream (oidc + ``capabilities.quota`` + authenticated),
 * so a disabled hook stays idle with no request (this is what keeps
 * none/apikey/demo deployments byte-identical). ``reload`` is exposed for
 * a caller that wants to force an immediate refresh.
 *
 * The hook's effectful behaviour (race guard, interval cleanup,
 * disabled = no fetch) is intentionally NOT unit-tested: this repo's
 * vitest runs in the node environment (pure logic only, see
 * vitest.config.ts); the pure parts live in model.ts and ARE covered.
 */
const REFRESH_MS = 30_000
/** How often a visible tab asks whether a refresh is due; asking is free. */
const DUE_CHECK_MS = 5_000

export function useQuotaUsage(enabled: boolean) {
  const [state, setState] = useState<QuotaUsageState>({
    error: null,
    rows: [],
    status: 'idle',
  })
  const generationRef = useRef(0)
  const inFlightRef = useRef(false)
  const lastAttemptAtRef = useRef<number | null>(null)

  const reload = useCallback(async () => {
    if (!enabled) return
    const generation = ++generationRef.current
    inFlightRef.current = true
    // Monotonic: a wall clock set backwards must not freeze the meter.
    lastAttemptAtRef.current = performance.now()
    setState((current) => ({ ...current, status: 'loading' }))
    try {
      const rows = await fetchQuotaUsage()
      if (generationRef.current !== generation) return
      setState({ error: null, rows, status: 'ready' })
    } catch (error) {
      if (generationRef.current !== generation) return
      setState({
        error: error instanceof Error ? error.message : String(error),
        rows: [],
        status: 'error',
      })
    } finally {
      // A superseded generation leaves the flag to the request that
      // replaced it.
      if (generationRef.current === generation) inFlightRef.current = false
    }
  }, [enabled])

  useEffect(() => {
    if (!enabled) {
      generationRef.current += 1
      inFlightRef.current = false
      lastAttemptAtRef.current = null
      setState({ error: null, rows: [], status: 'idle' })
      return
    }
    void reload()
    // A hidden tab has nobody looking at the meter, so it never fetches;
    // a tab that was hidden for an hour is current again the moment it
    // returns. Returning within REFRESH_MS of the last attempt fetches
    // nothing: an unconditional refetch on every `visibilitychange` once
    // turned ordinary tab switching into a request (plus two database
    // connections) every few seconds.
    const visible = (): boolean => (
      typeof document === 'undefined' || document.visibilityState === 'visible'
    )
    const refreshIfDue = (): void => {
      if (!visible()) return
      if (!quotaRefreshDue({
        inFlight: inFlightRef.current,
        lastAttemptAt: lastAttemptAtRef.current,
        now: performance.now(),
        refreshMs: REFRESH_MS,
      })) return
      void reload()
    }
    const timer = setInterval(refreshIfDue, DUE_CHECK_MS)
    document.addEventListener('visibilitychange', refreshIfDue)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', refreshIfDue)
    }
  }, [enabled, reload])

  return { reload, state }
}
