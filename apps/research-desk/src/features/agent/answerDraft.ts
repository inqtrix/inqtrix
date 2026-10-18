/**
 * What the review changed in an answer the reader already read as its draft.
 *
 * The server shows a reviewed kernel run's answer before the review starts
 * (`inqtrix.answer.draft`) and publishes the reviewed answer afterwards. The
 * reviewed answer replaces the draft in place; the passages the review
 * changed are marked by their source lines, which the Markdown renderer maps
 * onto the blocks it draws (`components/markdown/reviewMarks.ts`).
 */

import { normalizeLatexDelimiters } from '@/components/markdown/latexDelimiters'
import { markdownDiffSegments } from '@/features/editor/suggestionDiff'

/**
 * The 1-based lines of the reviewed answer that the draft did not hold, as
 * the renderer reads them: both texts pass the renderer's own delimiter
 * normalization first, which can turn one source line into three. A
 * trailing line end is no change.
 */
export function reviewChangedLines(draft: string, reviewed: string): ReadonlySet<number> {
  const changed = new Set<number>()
  let line = 1
  for (const segment of markdownDiffSegments(
    normalizeLatexDelimiters(draft.trimEnd()),
    normalizeLatexDelimiters(reviewed.trimEnd()),
  )) {
    if (segment.type === 'delete') continue
    const lines = lineCount(segment.text)
    if (segment.type === 'insert') {
      for (let offset = 0; offset < lines; offset += 1) changed.add(line + offset)
    }
    line += lines
  }
  return changed
}

/** Whether the review published the draft's own text. */
export function reviewKeptDraft(draft: string, reviewed: string): boolean {
  return draft.trimEnd() === reviewed.trimEnd()
}

function lineCount(text: string): number {
  const parts = text.split('\n')
  return parts[parts.length - 1] === '' ? parts.length - 1 : parts.length
}
