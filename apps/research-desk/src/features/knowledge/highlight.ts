/**
 * Whitespace-normalized text matching for the document viewer and the
 * Finden snippet rows.
 *
 * The grounding stage verifies quotes against whitespace-collapsed
 * document text, so the client must match the same way: any run of
 * whitespace in the target matches any run of whitespace (including
 * line breaks) in the document. Matching is case-insensitive — quotes
 * are verified case-sensitively server-side, but the viewer's job is
 * to FIND the passage, and extracted text may differ in casing around
 * headings.
 *
 * Kept deliberately renderer-agnostic (offsets into the original
 * string, no DOM): a future layout parser will swap the text layer for
 * coordinates while this matching layer stays unchanged.
 */

export type HighlightRange = {
  start: number
  end: number
}

export type HighlightSegment = {
  text: string
  /** Index into the matched ranges; null for plain text between hits. */
  rangeIndex: number | null
}

const MAX_MATCHES = 200

/** Find every whitespace-normalized occurrence of `target` in `text`. */
export function findNormalizedMatches(text: string, target: string): HighlightRange[] {
  const pattern = normalizedPattern(target)
  if (!pattern) return []

  const ranges: HighlightRange[] = []
  let match: RegExpExecArray | null
  while ((match = pattern.exec(text)) !== null && ranges.length < MAX_MATCHES) {
    ranges.push({ end: match.index + match[0].length, start: match.index })
    // Zero-length safety: never loop in place.
    if (match[0].length === 0) pattern.lastIndex += 1
  }
  return ranges
}

/** First target (in order) that produces at least one match wins; used
 * when a precise quote may fail and search terms act as fallback. */
export function findFirstMatchingTarget(
  text: string,
  targets: readonly string[],
): HighlightRange[] {
  for (const target of targets) {
    const ranges = findNormalizedMatches(text, target)
    if (ranges.length > 0) return ranges
  }
  return []
}

/** Matches for several independent terms (Finden snippets), merged and
 * sorted; overlapping ranges are collapsed so segments never nest. */
export function findTermMatches(text: string, terms: readonly string[]): HighlightRange[] {
  const all = terms.flatMap((term) => findNormalizedMatches(text, term))
  all.sort((a, b) => a.start - b.start || a.end - b.end)
  const merged: HighlightRange[] = []
  for (const range of all) {
    const last = merged[merged.length - 1]
    if (last && range.start <= last.end) {
      last.end = Math.max(last.end, range.end)
    } else {
      merged.push({ ...range })
    }
  }
  return merged
}

/** Split `text` into render segments for the given (sorted, disjoint)
 * ranges. Out-of-order or overlapping input is tolerated by skipping
 * ranges that rewind. */
export function splitByRanges(text: string, ranges: readonly HighlightRange[]): HighlightSegment[] {
  const segments: HighlightSegment[] = []
  let cursor = 0
  ranges.forEach((range, index) => {
    if (range.start < cursor || range.start >= range.end || range.end > text.length) return
    if (range.start > cursor) {
      segments.push({ rangeIndex: null, text: text.slice(cursor, range.start) })
    }
    segments.push({ rangeIndex: index, text: text.slice(range.start, range.end) })
    cursor = range.end
  })
  if (cursor < text.length) {
    segments.push({ rangeIndex: null, text: text.slice(cursor) })
  }
  return segments
}

/** Tokenize a Finden query into highlightable terms (>= 2 chars). */
export function searchTermsFromQuery(query: string): string[] {
  return query
    .split(/\s+/)
    .map((term) => term.trim())
    .filter((term) => term.length >= 2)
}

function normalizedPattern(target: string): RegExp | null {
  const collapsed = target.replace(/\s+/g, ' ').trim()
  if (!collapsed) return null
  const escaped = collapsed
    .split(' ')
    .map(escapeRegExp)
    .join('\\s+')
  return new RegExp(escaped, 'gi')
}

function escapeRegExp(value: string) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}
