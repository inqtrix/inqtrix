/**
 * The autosave diff loop shared by every project-entity sync.
 *
 * Each entity hook (chat threads/groups, editor documents/folders/comments)
 * keeps an in-memory ``synced`` map of "what the server holds" per id; this
 * helper diffs the current local collection against it and issues the
 * minimal writes: push entities whose fingerprint changed (or are new),
 * delete entities the server has but the local collection no longer does.
 * Defined once so the diff cannot drift between entities.
 *
 * Generic over the fingerprint type ``F`` and its ``changed`` comparator, so
 * an entity may use a simple string fingerprint OR a structured one (chat
 * threads compare ``{updatedAt, groupId}``) without this helper caring.
 * ``synced`` is mutated in place: an id's fingerprint advances only after
 * its push succeeds, so a failed/aborted push is retried on the next pass
 * (idempotent server upserts make that safe).
 *
 * The local collection and the baseline ids are read at the same moment,
 * when the pass starts. Other writers add to the baseline while the pass
 * awaits its pushes (an older page loaded into a list, a confirmed creation,
 * the next project's hydrate); such an id is missing from this pass's
 * snapshot and, read later, would look deleted locally.
 */

export type SyncCollectionArgs<T, F> = {
  /** The current local entities, keyed by id. */
  current: Record<string, T>
  /** Last successfully-synced fingerprint per id (mutated in place). */
  synced: Map<string, F>
  /** The fingerprint of an entity (the fields whose change needs a push). */
  fingerprintOf: (item: T) => F
  /** Whether ``current`` differs from the last-synced ``previous``. */
  changed: (previous: F | undefined, current: F) => boolean
  /** Push one new/changed entity to the server. */
  pushOne: (item: T) => Promise<void>
  /** Delete one entity (id) the local collection no longer has. */
  deleteOne: (id: string) => Promise<void>
  /**
   * Ids a concurrent exclusive writer owns right now — the diff must not
   * touch them in EITHER direction. The owning case is a user-confirmed
   * deletion: while it is in flight, just confirmed (the local removal has
   * not committed yet, so ``current`` still holds a stale record), or failed
   * and waiting for a manual retry, an autosave pass must neither re-push
   * the entity (that resurrects the deleted row server-side) nor delete it
   * (that would be an uncommanded retry of a failed deletion).
   */
  exclude?: ReadonlySet<string>
  /**
   * Whether the project this pass belongs to is still the open one
   * ({@link bindSyncScope}). Once it is not, the pass stops before its next
   * request and writes nothing more into ``synced``, which by then may
   * already hold the next project.
   */
  inScope?: () => boolean
}

export async function syncCollection<T, F>({
  current,
  synced,
  fingerprintOf,
  changed,
  pushOne,
  deleteOne,
  exclude,
  inScope = () => true,
}: SyncCollectionArgs<T, F>): Promise<void> {
  const known = [...synced.keys()]
  for (const item of Object.values(current)) {
    const fingerprint = fingerprintOf(item)
    const id = idOf(item)
    if (exclude?.has(id)) continue
    if (changed(synced.get(id), fingerprint)) {
      if (!inScope()) return
      await pushOne(item)
      if (!inScope()) return
      synced.set(id, fingerprint)
    }
  }
  for (const id of deletedSinceSnapshot(known, synced, current, exclude)) {
    if (!inScope()) return
    await deleteOne(id)
    if (!inScope()) return
    synced.delete(id)
  }
}

/**
 * The ids a pass deletes: in the baseline when the pass read ``current``
 * (``known``), still in it, and missing from ``current``. An id that entered
 * the baseline later belongs to a later snapshot; one that left it meanwhile
 * was already deleted.
 */
export function deletedSinceSnapshot<F>(
  known: readonly string[],
  synced: ReadonlyMap<string, F>,
  current: Readonly<Record<string, unknown>>,
  exclude?: ReadonlySet<string>,
): string[] {
  return known.filter((id) => !exclude?.has(id) && synced.has(id) && !(id in current))
}

/**
 * A predicate that holds while the sync scope read now still holds: ``read``
 * returns the open project's identity, or ``null`` while sync is off. Bound
 * when a pass starts, it tells the pass whether its project is still open;
 * the read happens during render, where the request options change too.
 */
export function bindSyncScope(read: () => string | null): () => boolean {
  const bound = read()
  return () => bound !== null && read() === bound
}

/** Every project entity record carries a string ``id`` — the map key. */
function idOf(item: unknown): string {
  return (item as { id: string }).id
}

/**
 * Run a delete, swallowing a 404. The desired post-state of a delete is
 * "gone", so a target the server no longer knows is already that state. This
 * also makes a parent-cascade safe: deleting a file-library section cascades
 * its groups + assets server-side, so the autosave's subsequent explicit
 * child deletes would otherwise 404 and wedge in a retry loop. Shared by the
 * asset + vector-index sync hooks.
 */
export async function deleteTolerant404(
  run: () => Promise<void>,
  isNotFound: (error: unknown) => boolean = (error) => (
    error instanceof Error
    && 'status' in error
    && error.status === 404
  ),
): Promise<void> {
  try {
    await run()
  } catch (error) {
    if (!isNotFound(error)) throw error
  }
}
