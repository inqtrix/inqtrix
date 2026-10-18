import { joinNumberWords } from '@inqtrix/editor-schema'
import { Diff, diffLines, wordsWithSpaceDiff } from 'diff'

export type SuggestionDiffSegment = {
  text: string
  type: 'delete' | 'equal' | 'insert'
}

export type SuggestionDiffDisplay = 'block' | 'inline'
export type SuggestionReviewSurface = 'editor' | 'panel'

export type SuggestionDiffPlan = {
  display: SuggestionDiffDisplay
  reviewSurface: SuggestionReviewSurface
  segments: SuggestionDiffSegment[]
}

export type DocumentDiffBlock =
  | { kind: 'delete'; markdown: string }
  | { kind: 'equal'; markdown: string }
  | { kind: 'insert'; markdown: string }
  | {
    afterMarkdown: string
    beforeMarkdown: string
    inlineSegments: SuggestionDiffSegment[] | null
    kind: 'replace'
  }

export const INLINE_DIFF_MAX_CHARS = 720
export const INLINE_DIFF_MAX_CHANGED_SEGMENTS = 14
export const DOCUMENT_INLINE_DIFF_MAX_CHARS = 2_400
export const DOCUMENT_INLINE_DIFF_MAX_CHANGED_SEGMENTS = 48

const PUNCTUATION_ONLY = /^[^\s\p{L}\p{N}]+$/u

/**
 * The library's word diff, with a number and its separators as one word
 * (`joinNumberWords`, the rule the collaboration server's publication
 * shares). The library splits 18.000 into 18 . 000, so a changed amount
 * read as fragments ("1821.000500").
 */
class SuggestionWordDiff extends Diff<string, string> {
  tokenize(value: string): string[] {
    return joinNumberWords([...wordsWithSpaceDiff.tokenize(value)])
  }
}

const suggestionWordDiff = new SuggestionWordDiff()

/**
 * Word-level diff between the anchored original text and a proposed rewrite.
 * Whitespace is preserved so the rendered diff reads like the source passage;
 * changes that only punctuation separates read as one replacement ("A-B"
 * becomes "C-D", not "AC-BD").
 */
export function suggestionDiffSegments(original: string, proposed: string): SuggestionDiffSegment[] {
  return mergePunctuationSeparatedChanges(
    suggestionWordDiff.diff(original, proposed).map((part) => ({
      text: part.value,
      type: part.added ? 'insert' : part.removed ? 'delete' : 'equal',
    })),
  )
}

type ChangeRun = { deleted: string; inserted: string }

function mergePunctuationSeparatedChanges(segments: SuggestionDiffSegment[]): SuggestionDiffSegment[] {
  const items: Array<SuggestionDiffSegment | ChangeRun> = []
  for (const segment of segments) {
    const last = items.at(-1)
    if (segment.type === 'equal') {
      items.push(segment)
      continue
    }
    const run = last && !('type' in last) ? last : { deleted: '', inserted: '' }
    if (run !== last) items.push(run)
    if (segment.type === 'delete') run.deleted += segment.text
    else run.inserted += segment.text
    const separator = items.at(-2)
    const before = items.at(-3)
    if (
      separator && 'type' in separator && PUNCTUATION_ONLY.test(separator.text)
      && before && !('type' in before)
    ) {
      before.deleted += separator.text + run.deleted
      before.inserted += separator.text + run.inserted
      items.splice(-2, 2)
    }
  }
  return items.flatMap((item) => {
    if ('type' in item) return [item]
    return [
      ...(item.deleted ? [{ text: item.deleted, type: 'delete' as const }] : []),
      ...(item.inserted ? [{ text: item.inserted, type: 'insert' as const }] : []),
    ]
  })
}

export function markdownDiffSegments(originalMarkdown: string, currentMarkdown: string): SuggestionDiffSegment[] {
  return diffLines(originalMarkdown, currentMarkdown)
    .map((part) => ({
      text: part.value,
      type: (part.added ? 'insert' : part.removed ? 'delete' : 'equal') as SuggestionDiffSegment['type'],
    }))
    .filter((segment) => segment.text.length > 0)
}

export function documentDiffPlan(originalMarkdown: string, currentMarkdown: string): DocumentDiffBlock[] {
  const segments = markdownDiffSegments(originalMarkdown, currentMarkdown)
  const blocks: DocumentDiffBlock[] = []

  for (let index = 0; index < segments.length; index += 1) {
    const segment = segments[index]
    const next = segments[index + 1]

    if (segment.type === 'delete' && next?.type === 'insert') {
      blocks.push(documentReplacementBlock(segment.text, next.text))
      index += 1
      continue
    }

    if (segment.type === 'insert' && next?.type === 'delete') {
      blocks.push(documentReplacementBlock(next.text, segment.text))
      index += 1
      continue
    }

    blocks.push({ kind: segment.type, markdown: segment.text })
  }

  return blocks
}

export function suggestionDiffPlan(original: string, proposed: string): SuggestionDiffPlan {
  const segments = suggestionDiffSegments(original, proposed)
  const display = shouldUseBlockDiff(original, proposed, segments) ? 'block' : 'inline'
  return {
    display,
    reviewSurface: display === 'block' ? 'editor' : 'panel',
    segments,
  }
}

export function hasVisibleDiff(segments: SuggestionDiffSegment[]): boolean {
  return segments.some((segment) => segment.type !== 'equal' && segment.text.trim().length > 0)
}

function shouldUseBlockDiff(
  original: string,
  proposed: string,
  segments: SuggestionDiffSegment[],
) {
  const originalLength = original.trim().length
  const proposedLength = proposed.trim().length
  if (original.includes('\n') || proposed.includes('\n')) return true
  if (originalLength > INLINE_DIFF_MAX_CHARS || proposedLength > INLINE_DIFF_MAX_CHARS) return true
  const changedSegments = segments.filter((segment) => segment.type !== 'equal' && segment.text.trim()).length
  return changedSegments > INLINE_DIFF_MAX_CHANGED_SEGMENTS
}

function documentReplacementBlock(beforeMarkdown: string, afterMarkdown: string): DocumentDiffBlock {
  const inlineSegments = documentInlineDiffSegments(beforeMarkdown, afterMarkdown)
  return {
    afterMarkdown,
    beforeMarkdown,
    inlineSegments,
    kind: 'replace',
  }
}

function documentInlineDiffSegments(beforeMarkdown: string, afterMarkdown: string): SuggestionDiffSegment[] | null {
  const before = beforeMarkdown.trim()
  const after = afterMarkdown.trim()
  if (!shouldUseInlineDocumentDiff(before, after)) return null

  const segments = suggestionDiffSegments(before, after)
  const changedSegments = segments.filter((segment) => segment.type !== 'equal' && segment.text.trim())
  if (changedSegments.length === 0) return null
  if (changedSegments.length > DOCUMENT_INLINE_DIFF_MAX_CHANGED_SEGMENTS) return null
  if (changedSegments.some((segment) => changedMarkdownSyntax(segment.text))) return null
  if (diffTouchesInlineMarkdownSyntax(before, after, segments)) return null

  return segments
}

function shouldUseInlineDocumentDiff(before: string, after: string): boolean {
  if (!before || !after) return false
  if (before.length > DOCUMENT_INLINE_DIFF_MAX_CHARS || after.length > DOCUMENT_INLINE_DIFF_MAX_CHARS) return false
  if (hasMultipleMarkdownBlocks(before) || hasMultipleMarkdownBlocks(after)) return false
  if (hasBlockMarkdownShape(before) || hasBlockMarkdownShape(after)) return false
  return true
}

function hasMultipleMarkdownBlocks(value: string): boolean {
  return /\n\s*\n/.test(value) || value.split(/\r?\n/).filter((line) => line.trim()).length > 1
}

function hasBlockMarkdownShape(value: string): boolean {
  return /(^|\n)\s*(#{1,6}\s|[-+*]\s+|\d+\.\s+|>\s+|```|~~~|\|)/.test(value)
}

function changedMarkdownSyntax(value: string): boolean {
  return /[[\]()`*_]/.test(value)
}

function diffTouchesInlineMarkdownSyntax(
  before: string,
  after: string,
  segments: SuggestionDiffSegment[],
): boolean {
  const beforeRanges = inlineMarkdownSyntaxRanges(before)
  const afterRanges = inlineMarkdownSyntaxRanges(after)
  let beforeOffset = 0
  let afterOffset = 0

  for (const segment of segments) {
    if (segment.type === 'equal') {
      beforeOffset += segment.text.length
      afterOffset += segment.text.length
      continue
    }

    if (segment.type === 'delete') {
      const start = beforeOffset
      beforeOffset += segment.text.length
      if (rangeTouchesAny(start, beforeOffset, beforeRanges)) return true
      continue
    }

    const start = afterOffset
    afterOffset += segment.text.length
    if (rangeTouchesAny(start, afterOffset, afterRanges)) return true
  }

  return false
}

function inlineMarkdownSyntaxRanges(value: string): Array<{ end: number; start: number }> {
  const ranges: Array<{ end: number; start: number }> = []
  const tokenPattern = /\[[^\]\n]+\]\([^) \n]+(?:\s+"[^"\n]*")?\)|`[^`\n]+`|\*\*[^*\n][^*\n]*\*\*|\*[^*\n][^*\n]*\*/g
  let match: RegExpExecArray | null
  while ((match = tokenPattern.exec(value))) {
    ranges.push({ end: match.index + match[0].length, start: match.index })
  }
  return ranges
}

function rangeTouchesAny(start: number, end: number, ranges: Array<{ end: number; start: number }>): boolean {
  return ranges.some((range) => start < range.end && end > range.start)
}
