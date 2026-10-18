import { createContext, useContext, useMemo, type ReactNode } from 'react'
import { seedQuotaUsage } from './demo'
import type { QuotaDimensionUsage } from './model'
import { useQuotaUsage } from './useQuotaUsage'

type QuotaMeterGate = {
  /** Whether the quota meter applies (oidc + capability + session, or demo). */
  enabled: boolean
  /** Demo mode renders seeded figures without a backend. */
  demo: boolean
}

/** Demo-resolved usage shared by every meter/footer (see {@link useQuotaUsageData}). */
type QuotaUsageView = {
  /** Either the live server rows or the demo seed, already resolved. */
  rows: QuotaDimensionUsage[]
  /** A failed load, surfaced so consumers never masquerade as empty/unlimited. */
  loadFailed: boolean
}

const QuotaGateContext = createContext<QuotaMeterGate>({
  demo: false,
  enabled: false,
})

const QuotaUsageContext = createContext<QuotaUsageView>({
  loadFailed: false,
  rows: [],
})

/** Set once at the app root so every meter/footer shares ONE gate decision (no
 * prop-drilling of capability/session/demo through the chat/editor/research
 * trees) AND ONE usage poll.
 *
 * Owning the single {@link useQuotaUsage} here — above the workspace switch,
 * which unmounts/remounts each workspace — is what keeps the footer values
 * present on a mode switch instead of refetching from empty (the "pop-in").
 * The gate and the usage live in SEPARATE contexts on purpose: the gate is
 * stable (flips only when enabled/demo change) while usage ticks on the 30s
 * poll, so gate-only consumers ({@link useQuotaMeterGate}, e.g. the admin
 * panel) do not re-render every poll.
 */
export function QuotaMeterProvider({
  enabled,
  demo,
  children,
}: QuotaMeterGate & { children: ReactNode }) {
  const { state } = useQuotaUsage(enabled && !demo)
  const now = useMemo(() => Math.floor(Date.now() / 1000), [])
  // Memoised so the demo branch keeps a stable array identity (every footer
  // then shares one snapshot, and dependent memos do not recompute per render).
  const demoRows = useMemo(() => seedQuotaUsage(now), [now])

  const gate = useMemo<QuotaMeterGate>(() => ({ demo, enabled }), [demo, enabled])
  const usage = useMemo<QuotaUsageView>(() => {
    const rows = demo ? demoRows : state.rows
    // A failed load must not read identically to a genuinely empty/unlimited
    // account (all-zero rows look the same); surface it instead of zero bars.
    const loadFailed = !demo && state.status === 'error'
    return { loadFailed, rows }
  }, [demo, demoRows, state.rows, state.status])

  return (
    <QuotaGateContext.Provider value={gate}>
      <QuotaUsageContext.Provider value={usage}>
        {children}
      </QuotaUsageContext.Provider>
    </QuotaGateContext.Provider>
  )
}

export function useQuotaMeterGate(): QuotaMeterGate {
  return useContext(QuotaGateContext)
}

/** Demo-resolved quota usage shared by every meter/footer. Reads from the
 * single poll owned by {@link QuotaMeterProvider}, so the values are present
 * immediately on a workspace switch (no per-mount refetch). */
export function useQuotaUsageData(): QuotaUsageView {
  return useContext(QuotaUsageContext)
}
