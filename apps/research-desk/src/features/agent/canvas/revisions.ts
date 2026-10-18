/**
 * The versions a reader may pick from a document's revision history.
 *
 * Every write is a revision snapshot — including the streaming flushes
 * of a memo growing section by section (status `writing`). Those stay
 * in the history for diffs and recovery, but they are not versions: a
 * fresh memo would list "Revision 22" with fragments at 1 and 3. The
 * picker lists the finished writes and user edits, always including the
 * current revision, and only when there is more than one to choose.
 */
export type RevisionEntry = { revision: number; status: string }

export function selectableRevisions<T extends RevisionEntry>(
  revisions: readonly T[] | undefined,
  current: number,
): T[] {
  const listed = (revisions ?? []).filter(
    (entry) => entry.status !== 'writing' || entry.revision === current,
  )
  return listed.length > 1 ? listed : []
}
