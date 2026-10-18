/**
 * Line-hunk view of an artifact revision diff: the inline chip
 * expansion shows ONLY the changed regions — unified-diff style hunks
 * with a few context lines and visible "N unchanged lines" gaps —
 * never the whole document (that is the canvas full-diff view's job).
 */

import { markdownDiffSegments } from '@/features/editor/suggestionDiff'

export type DiffHunkLine = {
  type: 'context' | 'delete' | 'insert'
  text: string
}

export type DiffHunk = {
  /** Unchanged lines skipped since the previous hunk (0 for the
   * first hunk when the change starts at the top). */
  skippedBefore: number
  lines: DiffHunkLine[]
}

export type DiffHunkPlan = {
  hunks: DiffHunk[]
  /** Unchanged lines after the last hunk (a visible tail gap). */
  skippedAfter: number
}

/** Git's conventional context width. */
export const DIFF_HUNK_CONTEXT_LINES = 3

type FlatLine = DiffHunkLine

function flatDiffLines(from: string, to: string): FlatLine[] {
  const lines: FlatLine[] = []
  for (const segment of markdownDiffSegments(from, to)) {
    const type = segment.type === 'equal' ? 'context' : segment.type
    // A trailing newline closes the last line instead of opening an
    // empty extra one.
    const parts = segment.text.split('\n')
    if (parts[parts.length - 1] === '') parts.pop()
    for (const text of parts) lines.push({ type, text })
  }
  return lines
}

export function diffHunkPlan(
  from: string,
  to: string,
  context: number = DIFF_HUNK_CONTEXT_LINES,
): DiffHunkPlan {
  const lines = flatDiffLines(from, to)
  const changed = lines
    .map((line, index) => (line.type === 'context' ? -1 : index))
    .filter((index) => index !== -1)
  if (changed.length === 0) return { hunks: [], skippedAfter: 0 }

  const hunks: DiffHunk[] = []
  let cursor = 0
  let index = 0
  while (index < changed.length) {
    const start = Math.max(changed[index] - context, cursor)
    // Merge changes whose context windows touch into ONE hunk.
    let end = changed[index] + context
    while (
      index + 1 < changed.length
      && changed[index + 1] - context <= end + 1
    ) {
      index += 1
      end = changed[index] + context
    }
    end = Math.min(end, lines.length - 1)
    hunks.push({
      skippedBefore: start - cursor,
      lines: lines.slice(start, end + 1),
    })
    cursor = end + 1
    index += 1
  }
  return { hunks, skippedAfter: lines.length - cursor }
}
