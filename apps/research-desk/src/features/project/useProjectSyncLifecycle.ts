import { useCallback, useEffect, useRef } from 'react'

import { bindSyncScope } from './syncCollection'

/** A cancellation token handed to a hydrate body; ``cancelled`` flips to true
 * when the project identity changes or the hook unmounts, so an in-flight
 * hydrate can bail before it writes into a torn-down or superseded session. */
export type SyncLifecycleToken = { cancelled: boolean }

type ProjectSyncLifecycleOptions = {
  /** The hook's ``syncActive`` gate: hydrate only runs while this is true. */
  active: boolean
  /** Stable string identity of the CURRENT project, e.g.
   * ``${workspaceId}:${projectEpoch}``. A change re-arms the lifecycle. */
  identity: string
  /** Clears the hook's synced/loaded/cursor state and the hydrated flag. Runs
   * before every (re)hydrate AND on leaving sync. Must be referentially stable
   * (wrap in useCallback) or the effect re-runs every render. */
  reset: () => void
  /** Loads the first page(s) from the server and marks the hook hydrated. Runs
   * only while active, after reset. Receives a token whose ``cancelled`` flips
   * true when the identity changes or the hook unmounts. Must be stable. */
  hydrate: (token: SyncLifecycleToken) => void
}

/**
 * Owns the reset+hydrate lifecycle of a PROJECT-SCOPED server-sync hook, keyed
 * on the project identity rather than on the ``syncActive`` boolean alone.
 *
 * The bug this exists to prevent: the project-scoped persistence hooks (chat,
 * editor, assets, vector-index) seed a ``synced`` fingerprint map from the
 * server on hydrate and, on autosave, DELETE whatever is in ``synced`` but no
 * longer present locally. If the hook only re-armed on ``syncActive`` going
 * false, switching to a DIFFERENT project that is also server-synced (so
 * ``syncActive`` stays true) would leave the PRIOR project's fingerprints in
 * ``synced`` -- and the next autosave would delete that project's rows on the
 * server. Re-keying the lifecycle on a project-identity change forces each
 * project to re-hydrate from its OWN server state, turning a silent
 * cross-project deletion into a safe merge.
 *
 * One effect (not two) so the ordering is explicit and cannot be broken by
 * reordering: ``reset`` always runs before ``hydrate``, and the previous
 * identity's in-flight hydrate is cancelled (via cleanup) before the next one
 * begins. Account-scoped hooks (preferences) and server-authoritative syncs
 * (templates) deliberately do NOT use this -- they are not project-scoped and
 * have no local-wins delete path.
 */
export function useProjectSyncLifecycle({
  active,
  identity,
  reset,
  hydrate,
}: ProjectSyncLifecycleOptions): void {
  useEffect(() => {
    reset()
    if (!active) return
    const token: SyncLifecycleToken = { cancelled: false }
    hydrate(token)
    return () => {
      token.cancelled = true
    }
  }, [active, identity, reset, hydrate])
}

/**
 * Binds autosave passes to the project open when a pass starts: the returned
 * binder hands each pass a predicate (`syncCollection`'s `inScope`) that holds
 * while the same project is open and sync is on. The scope is read during
 * render, where the hooks' request options change as well, so a pass of the
 * previous project sees a switch before it could send that project's rows to
 * the next one. Takes the same `active` and `identity` as
 * {@link useProjectSyncLifecycle}.
 */
export function useSyncScope(active: boolean, identity: string): () => () => boolean {
  const scopeRef = useRef<string | null>(active ? identity : null)
  scopeRef.current = active ? identity : null
  return useCallback(() => bindSyncScope(() => scopeRef.current), [])
}
