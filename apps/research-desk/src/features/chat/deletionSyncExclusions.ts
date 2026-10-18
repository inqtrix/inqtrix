import type { ChatThreadRecord } from '../project/types'

/**
 * Which thread ids the autosave diff must leave alone, and which deletion
 * tombstones have served their purpose.
 *
 * A confirmed deletion owns its thread exclusively, and that ownership must
 * survive one awkward window: the DELETE has succeeded and the synced
 * baseline is pruned, but React has not committed the local removal yet, so
 * the flush queued behind the deletion still sees the thread in its state
 * snapshot. Without an exclusion the diff reads that stale record as a NEW
 * thread (no baseline) and re-pushes it — the deleted conversation comes
 * back server-side, and the following pass then deletes it again with a
 * request nobody asked for.
 *
 * Two signals cover the window: the record's own `deletion` marker (set on
 * click, so it travels inside the stale snapshot) and the tombstone set of
 * just-deleted ids (armed at DELETE success, for the sliver where even the
 * marker's commit could still be pending). A tombstone is `settled` — safe
 * for the caller to drop — once the removal has committed and the id has
 * left the collection, because an absent id with a pruned baseline is
 * invisible to the diff on its own.
 */
export function deletionSyncExclusions(
  threads: Record<string, ChatThreadRecord>,
  recentlyDeleted: ReadonlySet<string>,
): { exclude: Set<string>; settled: string[] } {
  const exclude = new Set<string>()
  const settled: string[] = []
  for (const id of recentlyDeleted) {
    if (id in threads) {
      exclude.add(id)
    } else {
      settled.push(id)
    }
  }
  for (const thread of Object.values(threads)) {
    if (thread.deletion) exclude.add(thread.id)
  }
  return { exclude, settled }
}
