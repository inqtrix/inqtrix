/**
 * Move one item to a new index, returning a fresh array.
 *
 * Used wherever the composer's ordered references are reordered by drag: the
 * chat draft reducer (`pendingChatAttachmentRefs`), the editor's local extra
 * refs, and the TipTap pill attribute permutation in `MentionComposer`. A
 * no-op move or an out-of-range index returns a copy unchanged, so callers never
 * have to guard the bounds themselves.
 */
export function moveItem<T>(items: readonly T[], fromIndex: number, toIndex: number): T[] {
  if (fromIndex === toIndex) return [...items]
  if (!items[fromIndex] || toIndex < 0 || toIndex >= items.length) return [...items]
  const next = [...items]
  const [item] = next.splice(fromIndex, 1)
  next.splice(toIndex, 0, item)
  return next
}
