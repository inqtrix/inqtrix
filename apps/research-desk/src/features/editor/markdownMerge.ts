/**
 * Line-wise three-way merge of two versions of a Markdown document, as Git
 * and diff3 merge files.
 *
 * Both versions are compared with the base they were written on. Changes to
 * separate ranges of the base both go into the result, the same change made
 * on both sides goes in once, and changes whose ranges overlap or touch —
 * insertions at the same place included, and an insertion right next to a
 * range the other side changed — are a conflict. Nothing is placed by
 * guessing: no fuzz, no search for another matching place.
 *
 * In Markdown as the editor writes it a paragraph is one line and a blank
 * line separates paragraphs, so changes to neighbouring paragraphs merge,
 * while changes to neighbouring list items or table rows conflict.
 *
 * Pure and deterministic: the reducer runs it, and it neither reads a clock
 * nor depends on the time it takes.
 */

import { diffLines } from 'diff'

/** Past this many lines in any of the three texts a merge ends as a
 * conflict instead of being computed. */
export const MARKDOWN_MERGE_MAX_LINES = 20_000

/** Past this many removed plus inserted lines on one side a merge ends as a
 * conflict. The line diff takes time in proportion to the document length
 * times this count, so the bound keeps a merge of a rewritten document short
 * without asking a clock. */
export const MARKDOWN_MERGE_MAX_CHANGED_LINES = 1_000

export type MarkdownMergeConflictReason =
  /** The two sides changed ranges that overlap or touch. */
  | 'overlap'
  /** A text has more than {@link MARKDOWN_MERGE_MAX_LINES} lines, or a side
   * changed more than {@link MARKDOWN_MERGE_MAX_CHANGED_LINES} lines. */
  | 'too_large'
  /** The check after a clean merge found text lost or repeated. */
  | 'unsafe'

export type MarkdownMergeResult =
  | { contentMarkdown: string; kind: 'merged' }
  | { kind: 'conflict'; reason: MarkdownMergeConflictReason }

/** Merges the texts of two writers who both started from `base`. The texts
 * are compared with their line endings unified to `\n`; a merged result
 * uses `\n`. */
export function mergeMarkdownRevisions(
  base: string,
  ours: string,
  theirs: string,
): MarkdownMergeResult {
  return mergeMarkdownRevisionsWith(mergeChangedLines, base, ours, theirs)
}

/** Computes a merged text from three texts with `\n` line endings, or the
 * reason it found none. */
export type MarkdownLineMerger = (
  base: string,
  ours: string,
  theirs: string,
) => string | { conflict: 'overlap' | 'too_large' }

/** The merge with its line merger given. Every clean result of the merger
 * passes {@link mergeKeepsEveryChange}, so a fault in the merger ends as a
 * conflict, never as lost text. */
export function mergeMarkdownRevisionsWith(
  merger: MarkdownLineMerger,
  base: string,
  ours: string,
  theirs: string,
): MarkdownMergeResult {
  if (ours === theirs || ours === base) return { contentMarkdown: theirs, kind: 'merged' }
  if (theirs === base) return { contentMarkdown: ours, kind: 'merged' }
  const unifiedBase = unifyLineEndings(base)
  const unifiedOurs = unifyLineEndings(ours)
  const unifiedTheirs = unifyLineEndings(theirs)
  if (unifiedOurs === unifiedTheirs || unifiedOurs === unifiedBase) {
    return { contentMarkdown: theirs, kind: 'merged' }
  }
  if (unifiedTheirs === unifiedBase) return { contentMarkdown: ours, kind: 'merged' }
  if (
    Math.max(
      lineTokens(unifiedBase).length,
      lineTokens(unifiedOurs).length,
      lineTokens(unifiedTheirs).length,
    ) > MARKDOWN_MERGE_MAX_LINES
  ) {
    return { kind: 'conflict', reason: 'too_large' }
  }
  const merged = merger(unifiedBase, unifiedOurs, unifiedTheirs)
  if (typeof merged !== 'string') return { kind: 'conflict', reason: merged.conflict }
  if (!mergeKeepsEveryChange(unifiedBase, unifiedOurs, unifiedTheirs, merged)) {
    return { kind: 'conflict', reason: 'unsafe' }
  }
  return { contentMarkdown: merged, kind: 'merged' }
}

/** Whether two texts are the same apart from their line endings. */
export function sameMarkdownText(left: string, right: string): boolean {
  return left === right || unifyLineEndings(left) === unifyLineEndings(right)
}

/**
 * The check after a clean merge, on texts with `\n` line endings:
 *
 * - every line one side inserted stands in the result, in that side's order;
 * - every line of the base that both sides kept stands in the result, in its
 *   order;
 * - no line stands in the result more often than in the base and the
 *   insertions of both sides together.
 *
 * Its own pass over the diffs, so it does not share a fault of the merger.
 */
export function mergeKeepsEveryChange(
  base: string,
  ours: string,
  theirs: string,
  merged: string,
): boolean {
  const baseLines = lineTokens(base)
  const mergedLines = lineTokens(merged)
  const keptByBoth = new Array<boolean>(baseLines.length).fill(true)
  const insertedLines: string[] = []
  for (const side of [ours, theirs]) {
    const changes = diffLines(base, side, { maxEditLength: MARKDOWN_MERGE_MAX_CHANGED_LINES })
    if (!changes) return false
    const inserted: string[] = []
    let basePosition = 0
    for (const change of changes) {
      if (change.added) {
        inserted.push(...lineTokens(change.value))
        continue
      }
      if (change.removed) keptByBoth.fill(false, basePosition, basePosition + change.count)
      basePosition += change.count
    }
    if (!isSubsequence(inserted, mergedLines)) return false
    insertedLines.push(...inserted)
  }
  if (!isSubsequence(baseLines.filter((_, index) => keptByBoth[index]), mergedLines)) return false
  const allowed = lineCounts([...baseLines, ...insertedLines])
  for (const [line, count] of lineCounts(mergedLines)) {
    if (count > (allowed.get(line) ?? 0)) return false
  }
  return true
}

/** One side's change against the base: base lines `[start, end)` replaced by
 * `lines`; `start === end` is a pure insertion before base line `start`. */
type Hunk = { end: number; lines: string[]; start: number }

/** The default line merger: the hunks of both sides against the base, taken
 * apart where they are separate and compared where they overlap or touch. */
function mergeChangedLines(
  base: string,
  ours: string,
  theirs: string,
): string | { conflict: 'overlap' | 'too_large' } {
  const baseLines = lineTokens(base)
  const ourHunks = hunksAgainstBase(base, ours, baseLines.length)
  if (typeof ourHunks === 'string') return { conflict: ourHunks }
  const theirHunks = hunksAgainstBase(base, theirs, baseLines.length)
  if (typeof theirHunks === 'string') return { conflict: theirHunks }
  const merged: string[] = []
  let position = 0
  let ourIndex = 0
  let theirIndex = 0
  while (ourIndex < ourHunks.length || theirIndex < theirHunks.length) {
    const ourNext = ourHunks[ourIndex]
    const theirNext = theirHunks[theirIndex]
    const start = Math.min(ourNext?.start ?? Infinity, theirNext?.start ?? Infinity)
    let end = start
    const ourRegion: Hunk[] = []
    const theirRegion: Hunk[] = []
    // A hunk joins the region while it starts no later than the region ends:
    // ranges that overlap or touch are one region.
    for (;;) {
      if (ourIndex < ourHunks.length && ourHunks[ourIndex].start <= end) {
        end = Math.max(end, ourHunks[ourIndex].end)
        ourRegion.push(ourHunks[ourIndex])
        ourIndex += 1
        continue
      }
      if (theirIndex < theirHunks.length && theirHunks[theirIndex].start <= end) {
        end = Math.max(end, theirHunks[theirIndex].end)
        theirRegion.push(theirHunks[theirIndex])
        theirIndex += 1
        continue
      }
      break
    }
    merged.push(...baseLines.slice(position, start))
    const ourVersion = applyHunks(baseLines, start, end, ourRegion)
    if (theirRegion.length === 0) {
      merged.push(...ourVersion)
    } else {
      const theirVersion = applyHunks(baseLines, start, end, theirRegion)
      if (ourRegion.length > 0 && !sameLines(ourVersion, theirVersion)) return { conflict: 'overlap' }
      // Only one side changed the region, or both changed it the same way:
      // the change goes in once.
      merged.push(...theirVersion)
    }
    position = end
  }
  merged.push(...baseLines.slice(position))
  return merged.join('')
}

/** The changes of `side` against `base` as hunks in base order; `too_large`
 * past {@link MARKDOWN_MERGE_MAX_CHANGED_LINES}, `overlap` when the diff
 * does not add up to both texts. */
function hunksAgainstBase(
  base: string,
  side: string,
  baseLineCount: number,
): Hunk[] | 'overlap' | 'too_large' {
  const changes = diffLines(base, side, { maxEditLength: MARKDOWN_MERGE_MAX_CHANGED_LINES })
  if (!changes) return 'too_large'
  const sideLines = lineTokens(side)
  const hunks: Hunk[] = []
  let basePosition = 0
  let sidePosition = 0
  let open: Hunk | null = null
  for (const change of changes) {
    if (!change.added && !change.removed) {
      if (open) hunks.push(open)
      open = null
      basePosition += change.count
      sidePosition += change.count
      continue
    }
    open ??= { end: basePosition, lines: [], start: basePosition }
    if (change.removed) {
      basePosition += change.count
      open.end = basePosition
    } else {
      open.lines.push(...sideLines.slice(sidePosition, sidePosition + change.count))
      sidePosition += change.count
    }
  }
  if (open) hunks.push(open)
  if (basePosition !== baseLineCount || sidePosition !== sideLines.length) return 'overlap'
  return hunks
}

/** The base lines `[start, end)` with the given hunks of one side applied. */
function applyHunks(baseLines: readonly string[], start: number, end: number, hunks: readonly Hunk[]): string[] {
  const version: string[] = []
  let position = start
  for (const hunk of hunks) {
    version.push(...baseLines.slice(position, hunk.start), ...hunk.lines)
    position = hunk.end
  }
  version.push(...baseLines.slice(position, end))
  return version
}

/** A text as its lines, each with its own `\n`; a last line without one
 * stays without one. The same lines `diffLines` compares. */
function lineTokens(text: string): string[] {
  return text.match(/[^\n]*\n|[^\n]+$/g) ?? []
}

function unifyLineEndings(text: string): string {
  return text.replace(/\r\n?/g, '\n')
}

function sameLines(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((line, index) => line === right[index])
}

function isSubsequence(needle: readonly string[], haystack: readonly string[]): boolean {
  let position = 0
  for (const line of haystack) {
    if (position < needle.length && needle[position] === line) position += 1
  }
  return position === needle.length
}

function lineCounts(lines: readonly string[]): Map<string, number> {
  const counts = new Map<string, number>()
  for (const line of lines) counts.set(line, (counts.get(line) ?? 0) + 1)
  return counts
}
