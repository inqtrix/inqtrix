/**
 * Per-conversation scroll-position memory.
 *
 * A module-level singleton `Map`, deliberately NOT React state and NOT persisted:
 * it survives desk/view switches (the workspaces unmount on switch) so returning
 * to a thread restores where the user was, but it resets on a full page reload —
 * which is exactly the requested "fresh page load lands at the bottom" behaviour.
 *
 * We store distance-from-bottom (not an absolute `scrollTop`) plus a
 * `pinnedToBottom` flag: distance-from-bottom is invariant while content grows
 * downward (async markdown/code-highlight settle), so restoring it keeps the same
 * visual anchor even before the thread has finished laying out.
 *
 * Keys must be namespaced by surface (`chat:<id>` / `knowledge:<id>` /
 * `chat:incognito` / `knowledge:incognito`) because thread and session ids are
 * server-assigned and share no guaranteed namespace.
 */

export type ScrollMemoryEntry = {
  distanceFromBottom: number
  pinnedToBottom: boolean
}

const store = new Map<string, ScrollMemoryEntry>()

export function readScrollMemory(key: string): ScrollMemoryEntry | undefined {
  return store.get(key)
}

export function writeScrollMemory(key: string, entry: ScrollMemoryEntry): void {
  store.set(key, entry)
}

/** Drop a remembered position — call when a thread/session is deleted, cleared,
 * or an incognito session is reset, so a stale mid-scroll position can never
 * leak onto an emptied or recreated conversation. */
export function clearScrollMemory(key: string): void {
  store.delete(key)
}
