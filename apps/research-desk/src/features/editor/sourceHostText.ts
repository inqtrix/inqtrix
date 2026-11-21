import { Transaction, type EditorState, type TransactionSpec } from '@codemirror/state'

export type HostTextChange = { from: number; insert: string; to: number }

/**
 * The smallest change that turns `current` into `next`: their common start
 * and common end stay, so a selection there keeps its place. `null` when both
 * texts are the same.
 */
export function hostTextChange(current: string, next: string): HostTextChange | null {
  if (current === next) return null
  const shorter = Math.min(current.length, next.length)
  let start = 0
  while (start < shorter && current.charCodeAt(start) === next.charCodeAt(start)) start += 1
  let currentEnd = current.length
  let nextEnd = next.length
  // The ends never move past the start, so a repeated stretch cannot make
  // them cross.
  while (
    currentEnd > start
    && nextEnd > start
    && current.charCodeAt(currentEnd - 1) === next.charCodeAt(nextEnd - 1)
  ) {
    currentEnd -= 1
    nextEnd -= 1
  }
  return { from: start, insert: next.slice(start, nextEnd), to: currentEnd }
}

/**
 * The transaction that shows `next`, a text the record holds and the source
 * view does not show yet (another writer's version, a merge, an accepted agent
 * proposal, another document): only the range that differs is replaced, the
 * selection is mapped through it, and the change stays out of the undo
 * history, so undo takes back the user's own edits only. `null` when the view
 * shows `next` already.
 */
export function hostTextTransaction(state: EditorState, next: string): TransactionSpec | null {
  const change = hostTextChange(state.doc.toString(), next)
  if (!change) return null
  return { annotations: Transaction.addToHistory.of(false), changes: change }
}
