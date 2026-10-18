/**
 * Pending canvas saves, keyed by artifact.
 *
 * The previous single-slot ref had a deregistration race: during a tab
 * transition AnimatePresence unmounts the OLD document view after the
 * new one mounted, so the old cleanup nulled the slot the new view had
 * just claimed — and the submit flush silently protected nothing. A Map
 * keyed by artifactId with own-entry-only deregistration closes that
 * race and, as a side effect, keeps every transiently co-mounted editor
 * flushable.
 */
export type CanvasSaveRegistry = {
  /**
   * Register one artifact's flush. Returns the deregister function; it
   * removes ONLY its own registration — a newer flush for the same
   * artifact survives an older mount's late cleanup.
   */
  register: (artifactId: string, flush: () => Promise<void>) => () => void
  /** Await every registered flush (each flush handles its own errors). */
  flushAll: () => Promise<void>
  size: () => number
}

export function createCanvasSaveRegistry(): CanvasSaveRegistry {
  const entries = new Map<string, () => Promise<void>>()
  return {
    register(artifactId, flush) {
      entries.set(artifactId, flush)
      return () => {
        if (entries.get(artifactId) === flush) {
          entries.delete(artifactId)
        }
      }
    },
    async flushAll() {
      for (const flush of [...entries.values()]) {
        await flush()
      }
    },
    size: () => entries.size,
  }
}
