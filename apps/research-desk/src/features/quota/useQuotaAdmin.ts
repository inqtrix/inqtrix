import { useCallback, useEffect, useRef, useState } from 'react'
import {
  clearQuotaLimit,
  fetchQuotaAdmin,
  resetQuota,
  setQuotaLimit,
} from '@/api/inqtrixClient'
import {
  quotaAdminAvailable,
  seedQuotaAdminSnapshot,
  type QuotaAdminSnapshot,
} from './admin'
import { useQuotaMeterGate } from './QuotaMeterContext'

type AdminStatus = 'idle' | 'unavailable' | 'loading' | 'ready' | 'error'

export type QuotaAdminState = {
  /** True when the caller may administer quotas (an instance admin with the
   * quota capability on, or demo). */
  available: boolean
  demo: boolean
  snapshot: QuotaAdminSnapshot | null
  status: AdminStatus
  error: string | null
  /** Last failed mutation message; surfaced as a banner (No Silent Fallbacks). */
  mutationError: string | null
}

/** Instance-admin quota administration for the Settings panel.
 *
 * Quota administration is tenant-wide platform administration, so it is gated
 * on the INSTANCE ROLE (``instance_role == "admin"``), never on workspace
 * ownership — the server enforces the same axis on ``/v1/admin/quota*``. The
 * caller passes ``instanceAdmin`` (already resolved from the session role);
 * availability is then ``enabled && (demo || instanceAdmin)``, where the
 * shared meter gate contributes ``enabled`` (oidc + ``capabilities.quota`` +
 * session). A non-admin is ``available: false`` so Settings hides the section.
 */
export function useQuotaAdmin({ instanceAdmin }: { instanceAdmin: boolean }) {
  const { enabled, demo } = useQuotaMeterGate()
  const available = quotaAdminAvailable({ demo, enabled }, instanceAdmin)
  const [state, setState] = useState<QuotaAdminState>({
    available: false,
    demo,
    error: null,
    mutationError: null,
    snapshot: null,
    status: 'idle',
  })
  const generationRef = useRef(0)
  const now = useRef(Math.floor(Date.now() / 1000)).current

  const reload = useCallback(async () => {
    if (!available) return
    if (demo) {
      setState({
        available: true,
        demo,
        error: null,
        mutationError: null,
        snapshot: seedQuotaAdminSnapshot(now),
        status: 'ready',
      })
      return
    }
    const generation = ++generationRef.current
    setState((current) => ({ ...current, available: true, status: 'loading' }))
    try {
      const snapshot = await fetchQuotaAdmin()
      if (generationRef.current !== generation) return
      setState({
        available: true,
        demo,
        error: null,
        mutationError: null,
        snapshot,
        status: 'ready',
      })
    } catch (error) {
      if (generationRef.current !== generation) return
      setState((current) => ({
        ...current,
        available: true,
        error: error instanceof Error ? error.message : String(error),
        snapshot: null,
        status: 'error',
      }))
    }
  }, [available, demo, now])

  // Availability tracks the instance role: load the snapshot when it turns on,
  // and hide the section (no stale snapshot) when it turns off.
  useEffect(() => {
    if (!available) {
      generationRef.current += 1
      setState({
        available: false,
        demo,
        error: null,
        mutationError: null,
        snapshot: null,
        status: 'unavailable',
      })
      return
    }
    void reload()
  }, [available, demo, reload])

  // One mutation runner: every admin write clears the prior error, runs, and
  // re-reads the snapshot so the table reflects server truth (No Silent
  // Fallbacks). A batch (applyOverrides/resetUsage) can partially commit
  // before rejecting, so we reload on FAILURE too — then re-assert the error,
  // because a successful reload clears mutationError. Otherwise the table
  // would keep showing pre-write values while some writes already landed.
  const runMutation = useCallback(
    async (action: () => Promise<void>) => {
      if (!available || demo) return
      setState((current) => ({ ...current, mutationError: null }))
      try {
        await action()
        await reload()
      } catch (error) {
        const message = error instanceof Error ? error.message : String(error)
        await reload()
        setState((current) => ({ ...current, mutationError: message }))
      }
    },
    [available, demo, reload],
  )

  const setLimit = useCallback(
    (userId: string, dimension: string, value: number) =>
      runMutation(() =>
        setQuotaLimit({ dimension, user_id: userId, value }),
      ),
    [runMutation],
  )

  const clearLimit = useCallback(
    (userId: string, dimension: string) =>
      runMutation(() => clearQuotaLimit(userId, dimension)),
    [runMutation],
  )

  /** Commit a batch of per-user overrides as ONE unit of work: the inline
   * editor collects every changed dimension and saves on demand, so the
   * snapshot reloads once (not per field). ``value: null`` clears a row. */
  const applyOverrides = useCallback(
    (
      userId: string,
      changes: ReadonlyArray<{ dimension: string; value: number | null }>,
    ) =>
      runMutation(async () => {
        for (const change of changes) {
          if (change.value == null) {
            await clearQuotaLimit(userId, change.dimension)
          } else {
            await setQuotaLimit({
              dimension: change.dimension,
              user_id: userId,
              value: change.value,
            })
          }
        }
      }),
    [runMutation],
  )

  /** Reset every flow window for one user (the row-level "reset usage"). */
  const resetUsage = useCallback(
    (userId: string, dimensions: ReadonlyArray<string>) =>
      runMutation(async () => {
        for (const dimension of dimensions) {
          await resetQuota({ dimension, user_id: userId })
        }
      }),
    [runMutation],
  )

  return {
    applyOverrides,
    clearLimit,
    reload,
    resetUsage,
    setLimit,
    state,
  }
}
