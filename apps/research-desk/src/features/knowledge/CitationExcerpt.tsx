import type { ReactNode } from 'react'
import { findFirstMatchingTarget, type HighlightRange } from './highlight'

/**
 * Render a cited excerpt with the quoted span(s) strongly highlighted inside the
 * surrounding chunk context — the two-level "passage-in-context" highlight. The
 * SAME markup/colour is reused by the source panel's "Beleg" tab and the answer
 * card's hover preview, so a citation looks identical wherever it appears.
 */
export function HighlightedExcerpt({ ranges, text }: { ranges: HighlightRange[]; text: string }) {
  if (ranges.length === 0) return <>{text}</>
  const sorted = [...ranges].sort((a, b) => a.start - b.start)
  const parts: ReactNode[] = []
  let cursor = 0
  sorted.forEach((range, index) => {
    if (range.start > cursor) {
      parts.push(<span key={`c${index}`}>{text.slice(cursor, range.start)}</span>)
    }
    parts.push(
      <mark
        key={`m${index}`}
        className="rounded bg-brand/20 px-0.5 font-medium text-foreground ring-1 ring-brand/30"
      >
        {text.slice(range.start, range.end)}
      </mark>,
    )
    cursor = range.end
  })
  if (cursor < text.length) parts.push(<span key="tail">{text.slice(cursor)}</span>)
  return <>{parts}</>
}

/** The highlight ranges for the cited span within an excerpt (the grounding
 * quote first, then any other targets). Empty when nothing matches verbatim. */
export function excerptHighlightRanges(
  excerpt: string,
  highlightTargets: string[],
): HighlightRange[] {
  if (!excerpt) return []
  return findFirstMatchingTarget(excerpt, highlightTargets)
}

/**
 * Trim a long excerpt down to a compact window CENTERED on the first cited span
 * (with `…` ellipses) for the hover preview, and re-base the highlight ranges
 * onto the trimmed string. Without a match it returns the leading window. Keeps
 * the popover small while still showing the span in its immediate context.
 */
export function previewWindow(
  text: string,
  ranges: HighlightRange[],
  radius = 240,
): { text: string; ranges: HighlightRange[] } {
  if (ranges.length === 0) {
    const head = text.slice(0, radius * 2)
    return { text: head.length < text.length ? `${head}…` : head, ranges: [] }
  }
  const first = [...ranges].sort((a, b) => a.start - b.start)[0]
  const start = Math.max(0, first.start - radius)
  const end = Math.min(text.length, first.end + radius)
  const prefix = start > 0 ? '…' : ''
  const suffix = end < text.length ? '…' : ''
  const padLeft = prefix.length
  const shifted = ranges
    .filter((range) => range.start >= start && range.end <= end)
    .map((range) => ({ start: range.start - start + padLeft, end: range.end - start + padLeft }))
  return { text: `${prefix}${text.slice(start, end)}${suffix}`, ranges: shifted }
}
