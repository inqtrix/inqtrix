/**
 * Pure helpers for highlighting a cited passage on a PDF page's pdf.js text
 * layer (used by `PdfViewer` via react-pdf's `customTextRenderer`).
 *
 * The matching itself is the same whitespace-tolerant matcher the in-document
 * reader uses (`features/knowledge/highlight`) — kept as the single source so
 * the PDF and the extracted-text views highlight the same way. These helpers add
 * the text-layer specifics: pdf.js splits a line into many text items, so a
 * quote routinely spans several, and `customTextRenderer` returns raw HTML per
 * item. So we match against the joined item text, map the hit offsets back onto
 * each item, and emit escaped HTML with the matched slices wrapped in <mark>.
 */

import {
  findFirstMatchingTarget,
  findTermMatches,
  splitByRanges,
  type HighlightRange,
} from '@/features/knowledge/highlight'

/** Brand highlight for the text layer — background ONLY, transparent text. The
 * pdf.js text layer paints transparent glyphs over the canvas (the real glyphs
 * come from the canvas below), so the <mark> keeps text transparent (a color
 * would double the canvas glyphs — the browser's UA `mark{color}` otherwise
 * wins) and adds no padding (would misalign the box). Slightly stronger brand
 * than the in-document reader's /20 since it sits over the canvas; the brand
 * color keeps it "aus einem Guss" with that reader's highlight. */
const MARK_CLASS = 'rounded-sm bg-brand/30 text-transparent'

/** Locate the cited passage in the joined page text, degrading gracefully:
 *
 * 1. The full quote-first targets, contiguously (the precise hit).
 * 2. If that fails, the quote's sentence-level phrases (>= 3 words). The
 *    targets come from MarkItDown's text extraction while the PDF text layer is
 *    pdf.js's extraction of the same file; the two diverge enough that a long
 *    multi-sentence quote rarely matches contiguously, but an individual cited
 *    sentence usually still appears verbatim on the page.
 *
 * Returns the first tier that yields a match (empty when neither does — the
 * caller then keeps the page-level cue rather than guessing a box).
 */
function locatePassageRanges(joined: string, targets: readonly string[]): HighlightRange[] {
  const full = findFirstMatchingTarget(joined, targets)
  if (full.length > 0) return full
  const phrases = splitIntoPhrases(targets[0] ?? '').filter(
    (phrase) => phrase.split(/\s+/).length >= 3,
  )
  return findTermMatches(joined, phrases)
}

/** Split text into sentence/clause phrases on terminal punctuation and line
 * breaks; trimmed, empties dropped. */
function splitIntoPhrases(text: string): string[] {
  return text
    .split(/[.;:!?\n\r]+/)
    .map((phrase) => phrase.trim())
    .filter((phrase) => phrase.length > 0)
}

const HTML_ESCAPES: Record<string, string> = {
  '"': '&quot;',
  '&': '&amp;',
  "'": '&#39;',
  '<': '&lt;',
  '>': '&gt;',
}

/** Escape text before it enters the renderer's returned HTML string — both the
 * matched quote and the raw page text are untrusted from an injection
 * standpoint; only the fixed <mark> wrapper is literal markup. */
export function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => HTML_ESCAPES[char] ?? char)
}

/**
 * Map quote-first highlight targets onto per-text-item character ranges for one
 * page. Returns a map keyed by the item's index in `items` (which equals
 * react-pdf's `itemIndex`), each value being the ranges to wrap WITHIN that
 * item's string. Empty when no target is located (the caller keeps the
 * page-level cue rather than guessing).
 */
export function mapTargetsToItemRanges(
  items: ReadonlyArray<{ str?: string } | { type: string }>,
  targets: readonly string[],
): Map<number, HighlightRange[]> {
  const byItem = new Map<number, HighlightRange[]>()
  if (targets.length === 0) return byItem

  const starts: number[] = []
  const lengths: number[] = []
  let joined = ''
  items.forEach((item, index) => {
    const str = 'str' in item && typeof item.str === 'string' ? item.str : ''
    starts[index] = joined.length
    lengths[index] = str.length
    joined += str
    joined += ' ' // single separator, absorbed by the matcher's \s+ joins
  })

  const ranges = locatePassageRanges(joined, targets)
  if (ranges.length === 0) return byItem

  for (let index = 0; index < starts.length; index += 1) {
    const itemStart = starts[index]
    const itemEnd = itemStart + lengths[index]
    const local: HighlightRange[] = []
    for (const range of ranges) {
      const overlapStart = Math.max(range.start, itemStart)
      const overlapEnd = Math.min(range.end, itemEnd)
      if (overlapStart < overlapEnd) {
        local.push({ end: overlapEnd - itemStart, start: overlapStart - itemStart })
      }
    }
    if (local.length > 0) byItem.set(index, local)
  }
  return byItem
}

/**
 * HTML for one text item's span: matched slices wrapped in <mark>, everything
 * else escaped. When `anchor` is set, the item's first <mark> carries the
 * scroll-anchor attribute so the viewer can center the passage.
 */
export function renderItemHtml(
  str: string,
  ranges: readonly HighlightRange[] | undefined,
  anchor: boolean,
): string {
  if (!ranges || ranges.length === 0) return escapeHtml(str)
  let anchored = false
  return splitByRanges(str, ranges)
    .map((segment) => {
      if (segment.rangeIndex === null) return escapeHtml(segment.text)
      const anchorAttr = anchor && !anchored ? ' data-inqtrix-hit="1"' : ''
      anchored = true
      return `<mark class="${MARK_CLASS}"${anchorAttr}>${escapeHtml(segment.text)}</mark>`
    })
    .join('')
}
