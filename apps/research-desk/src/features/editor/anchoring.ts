import { createSecurePrefixedId, serializeWithRawCitationLabels } from '@inqtrix/editor-schema'
import type { JSONContent } from '@tiptap/core'
import type { Editor } from '@tiptap/react'
import type { EditorCommentAnchorRecord, EditorCommentKind, EditorCommentThreadRecord } from '@/features/project/types'

export type AnchorLocator = {
  hint: number
  quoteAfter?: string
  quoteBefore?: string
  text: string
  /** Ambiguity policy. `'nearest'` (default, the legacy
   * behavior) scores quotes softly and falls back to hint distance —
   * right for re-anchoring records that carry a REAL position hint.
   * `'strict'` mirrors the server resolver (`_resolve_anchor`): a set
   * quote must match or the candidate is disqualified, summed distance
   * decides, and a tie or quoteless ambiguity ABSTAINS (null) instead
   * of guessing — right for model-authored edits, where the server
   * would silently skip whatever the client guessed. */
  mode?: 'strict' | 'nearest'
}

export type EditorTextRange = {
  from: number
  to: number
}

export function createCommentFromSelection(
  editor: Editor,
  documentId: string,
  commentMarkdown: string,
  kind: EditorCommentKind = 'collect',
): EditorCommentThreadRecord | null {
  const { from, to } = editor.state.selection
  if (from >= to) return null
  const anchor = materializeAnchorForRange(editor, { from, to })
  const now = new Date().toISOString()
  return {
    anchor,
    commentMarkdown,
    createdAt: now,
    documentId,
    id: createLocalId('editor-comment'),
    kind,
    status: 'open',
    updatedAt: now,
  }
}

export function clampAnchor(anchor: EditorCommentAnchorRecord, editor: Editor): EditorTextRange {
  const docSize = editor.state.doc.content.size
  const from = Math.max(1, Math.min(anchor.from, docSize))
  const to = Math.max(from, Math.min(anchor.to, docSize))
  return { from, to }
}

export function materializeAnchorForRange(
  editor: Editor,
  range: EditorTextRange,
  previous?: EditorCommentAnchorRecord,
): EditorCommentAnchorRecord {
  const docSize = editor.state.doc.content.size
  const from = Math.max(1, Math.min(range.from, docSize))
  const to = Math.max(from, Math.min(range.to, docSize))
  return {
    ...previous,
    from,
    quoteAfter: editor.state.doc.textBetween(to, Math.min(docSize, to + 80), ' '),
    quoteBefore: editor.state.doc.textBetween(Math.max(0, from - 80), from, ' '),
    selectedMarkdown: markdownForRange(editor, from, to),
    selectedText: editor.state.doc.textBetween(from, to, ' '),
    to,
  }
}

export function resolveMaterializedAnchor(
  editor: Editor,
  anchor: EditorCommentAnchorRecord,
): { anchor: EditorCommentAnchorRecord; range: EditorTextRange } | null {
  const range = resolveAnchorRange(editor, {
    hint: clampAnchor(anchor, editor).from,
    quoteAfter: anchor.quoteAfter,
    quoteBefore: anchor.quoteBefore,
    text: anchor.selectedText,
  })
  if (!range) return null
  return {
    anchor: materializeAnchorForRange(editor, range, anchor),
    range,
  }
}

export function materializeCommentThread(
  editor: Editor,
  comment: EditorCommentThreadRecord,
): EditorCommentThreadRecord | null {
  const resolved = resolveMaterializedAnchor(editor, comment.anchor)
  if (!resolved) return null
  return { ...comment, anchor: resolved.anchor }
}

/**
 * Resolve a content locator (anchored text plus surrounding context) to a live
 * document range. ProseMirror owns positions; suggestions and the model only
 * speak content, so this helper is the single boundary that turns "what text"
 * into "where in the document".
 */
export function resolveAnchorRange(editor: Editor, locator: AnchorLocator): EditorTextRange | null {
  const index = buildDocumentTextIndex(editor)
  for (const needle of searchNeedlesForText(locator.text)) {
    const candidates: IndexedCandidate[] = []
    let matchIndex = index.text.indexOf(needle)
    while (matchIndex >= 0) {
      const range = rangeFromIndexedMatch(index.positions, matchIndex, needle.length)
      if (range) candidates.push({ length: needle.length, range, start: matchIndex })
      matchIndex = index.text.indexOf(needle, matchIndex + 1)
    }
    if (candidates.length <= 1) {
      if (candidates[0]) return candidates[0].range
      continue
    }
    if (locator.mode === 'strict') {
      // The first needle form that matches at all DECIDES: an ambiguous
      // outcome abstains instead of retrying a shorter (even more
      // ambiguous) form — skipping beats guessing, like the server.
      return pickStrictCandidate(index.text, candidates, locator)
    }
    const picked = pickAnchorCandidate(editor, candidates.map((c) => c.range), locator)
    if (picked) return picked
  }
  return null
}

export function blockWidgetPositionForRange(editor: Editor, range: EditorTextRange): number {
  let widgetAt = range.to
  editor.state.doc.descendants((node, pos) => {
    if (!node.isTextblock) return true
    const nodeStart = pos
    const nodeEnd = pos + node.nodeSize
    if (range.to >= nodeStart && range.to <= nodeEnd) {
      widgetAt = Math.min(editor.state.doc.content.size, nodeEnd)
      return false
    }
    return true
  })
  return widgetAt
}

export function blockInsertionPositionForRange(
  editor: Editor,
  range: EditorTextRange,
  side: 'before' | 'after',
): number {
  let insertionAt = side === 'before' ? range.from : range.to
  editor.state.doc.descendants((node, pos) => {
    if (!node.isTextblock) return true
    const nodeStart = pos
    const nodeEnd = pos + node.nodeSize
    if (range.from >= nodeStart && range.to <= nodeEnd) {
      insertionAt = side === 'before'
        ? nodeStart
        : Math.min(editor.state.doc.content.size, nodeEnd)
      return false
    }
    return true
  })
  return Math.max(0, Math.min(insertionAt, editor.state.doc.content.size))
}

/** The Markdown of the range *from*–*to*, the quote a comment anchor and an
 * AI block input carry. Without a serializer, or when it fails, the quote is
 * the range's plain text, and the console says so. */
export function markdownForRange(editor: Editor, from: number, to: number): string {
  const slice = editor.state.doc.slice(from, to)
  const json = slice.content.toJSON()
  const content = Array.isArray(json) ? json : [json]
  if (content.length === 0) return editor.state.doc.textBetween(from, to, ' ')
  const markdown = editor.markdown
  if (!markdown) {
    console.warn('The selection is quoted as plain text: the editor has no Markdown serializer.')
    return editor.state.doc.textBetween(from, to, ' ')
  }
  try {
    return serializeWithRawCitationLabels(
      { type: 'doc', content } as JSONContent,
      editor.schema,
      (json) => markdown.serialize(json),
    ).trim()
  } catch (error) {
    console.warn('The selection is quoted as plain text: its Markdown could not be written.', error)
    return editor.state.doc.textBetween(from, to, ' ')
  }
}

export function shouldParsePastedMarkdown(value: string): boolean {
  const trimmed = value.trim()
  if (!trimmed) return false
  return /(^|\n)(#{1,6}\s|[-*+]\s+|\d+\.\s+|>\s|```|\|.+\|)/.test(trimmed)
    || /\[[^\]]+\]\([^)]+\)/.test(trimmed)
    || /(^|[^*])\*\*[^*\n]+\*\*([^*]|$)/.test(trimmed)
    || /(^|[^_])__[^_\n]+__([^_]|$)/.test(trimmed)
    || /(^|[^`])`[^`\n]+`([^`]|$)/.test(trimmed)
    || /\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([^)]+\\\)|(^|[^$])\$[^$\n]+\$([^$]|$)/.test(trimmed)
}

type DocumentTextIndex = {
  positions: Array<number | null>
  text: string
}

function buildDocumentTextIndex(editor: Editor): DocumentTextIndex {
  const positions: Array<number | null> = []
  let text = ''
  const appendChar = (rawChar: string, pos: number | null) => {
    const char = /\s/.test(rawChar) ? ' ' : rawChar
    if (char === ' ' && (!text || text.endsWith(' '))) return
    text += char
    positions.push(pos)
  }

  editor.state.doc.descendants((node, blockPos) => {
    if (!node.isTextblock) return true
    let blockHasText = false
    node.forEach((child, childOffset) => {
      if (!child.isText || !child.text) return
      if (!blockHasText && text) appendChar(' ', null)
      blockHasText = true
      const base = blockPos + 1 + childOffset
      for (let offset = 0; offset < child.text.length; offset += 1) {
        appendChar(child.text[offset], base + offset)
      }
    })
    return false
  })

  return { positions, text: text.trim() }
}

function rangeFromIndexedMatch(
  positions: Array<number | null>,
  start: number,
  length: number,
): EditorTextRange | null {
  const matchPositions = positions.slice(start, start + length)
  const from = matchPositions.find((pos): pos is number => typeof pos === 'number')
  const last = [...matchPositions].reverse().find((pos): pos is number => typeof pos === 'number')
  return from != null && last != null ? { from, to: last + 1 } : null
}

function normalizeSearchText(value: string): string {
  return value.replace(/\s+/g, ' ').trim()
}

/** Die Zeichen zurueckgewinnen, die der Serialisierer maskiert hat.
 *
 * Der Serialisierer macht aus `Der Wert snake_case und <FIX> sowie
 * [Marke] & Co.` den Text `Der Wert snake\_case und &lt;FIX&gt; sowie \[Marke\]
 * &amp; Co.` -- er maskiert also auf ZWEI Wegen, mit Entitaeten UND mit
 * Backslash. Wer nur einen davon aufloest, laesst die Haelfte der Faelle
 * weiterhin am Anker scheitern.
 *
 * `&amp;` wird ZULETZT aufgeloest: sonst wuerde aus dem maskierten Text
 * `&amp;lt;` (also woertlich "&lt;") faelschlich `<`. */
function decodeMarkdownEntities(value: string): string {
  return value
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&#0*39;/g, "'")
    .replace(/&amp;/g, '&')
}

/** Backslash-Maskierung aufloesen -- NACH den Markdown-Blockregeln.
 *
 * Die Reihenfolge ist tragend: ein maskiertes `\>` ist Text, kein Zitat. Loeste
 * man es vor der Blockregel auf, fraesse `^\s{0,3}>\s?` es als Zitatpraefix
 * weg und der Suchtext verlore sein erstes Zeichen. Dasselbe gilt fuer `\-`
 * und `\#` am Zeilenanfang. */
function decodeMarkdownEscapes(value: string): string {
  return value.replace(/\\([\\`*_{}[\]()#+\-.!>|~])/g, '$1')
}

export function markdownToPlainTextForEditor(value: string): string {
  return normalizeSearchText(value
    .replace(/```[\s\S]*?```/g, (block) => block.replace(/```[^\n]*\n?|```/g, ' '))
    .replace(/^\s{0,3}#{1,6}\s+/gm, '')
    .replace(/^\s{0,3}>\s?/gm, '')
    .replace(/^\s*[-*+]\s+/gm, '')
    .replace(/^\s*\d+[.)]\s+/gm, '')
    .replace(/^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/gm, ' ')
    .replace(/!\[([^\]]*)\]\([^)]+\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/(\*\*|__)(.*?)\1/g, '$2')
    .replace(/([*_~])/g, '')
    .replace(/\|/g, ' '))
}

/** Der Text, wie der Nutzer ihn im Editor SIEHT.
 *
 * Die Suchtexte eines KI-Laufs sind Markdown: das Modell bekommt die
 * Markdown-Projektion des Dokuments und antwortet in derselben Sprache. Der
 * Anker wird aber im Editor-Text gesucht, wo das Zeichen bereits aufgeloest
 * ist. Der Serialisierer maskiert dabei auf ZWEI Wegen: aus `<FIX>` wird
 * `&lt;FIX&gt;`, aus `[Marke]` wird `\[Marke\]`. Beides muss
 * zurueckgenommen werden, sonst findet der Lauf seinen eigenen Anker nicht --
 * und weil der Wurf in der Edit-Schleife steht, risse ein einziger nicht
 * auffindbarer Edit den GANZEN Lauf ab.
 *
 * Bewusst OHNE die Markdown-Blockregeln: die stecken in
 * :func:`markdownToPlainTextForEditor` und bleiben unveraendert. Ein
 * maskiertes `\>` ist Text und darf nicht als Zitatpraefix weggefressen
 * werden -- was genau passierte, haette man die Aufloesung in jene Kette
 * gelegt. */
export function literalTextFromMarkdown(value: string): string {
  return normalizeSearchText(decodeMarkdownEscapes(decodeMarkdownEntities(value)))
}

function searchNeedlesForText(value: string): string[] {
  const candidates = [
    normalizeSearchText(value),
    markdownToPlainTextForEditor(value),
    literalTextFromMarkdown(value),
  ]
  return [...new Set(candidates)]
    .filter(Boolean)
    .sort((a, b) => b.length - a.length)
}

export type IndexedCandidate = {
  /** Start offset of the match in the normalized document text. */
  start: number
  /** Needle length in normalized characters. */
  length: number
  range: EditorTextRange
}

/**
 * Server-faithful ambiguity policy, the structural twin of
 * `_resolve_anchor` in editor_patch_service.py applied to the
 * normalized editor text: a set quote that does not appear on its side
 * of an occurrence DISQUALIFIES that occurrence; the summed distance
 * between occurrence and its nearest matching quotes decides; a tie or
 * a quoteless multi-match returns null (abstention). No hint, no
 * partial-quote points, no context window.
 */
export function pickStrictCandidate(
  text: string,
  candidates: IndexedCandidate[],
  locator: AnchorLocator,
): EditorTextRange | null {
  const before = markdownToPlainTextForEditor(locator.quoteBefore ?? '')
  const after = markdownToPlainTextForEditor(locator.quoteAfter ?? '')
  if (!before && !after) return null
  let best: EditorTextRange | null = null
  let bestScore: number | null = null
  let tied = false
  for (const candidate of candidates) {
    let score = 0
    if (before) {
      // Python: content.rfind(quote_before, 0, index) — the quote must
      // END at or before the occurrence start.
      const pos = text.slice(0, candidate.start).lastIndexOf(before)
      if (pos < 0) continue
      score += candidate.start - (pos + before.length)
    }
    if (after) {
      const pos = text.indexOf(after, candidate.start + candidate.length)
      if (pos < 0) continue
      score += pos - (candidate.start + candidate.length)
    }
    if (bestScore === null || score < bestScore) {
      best = candidate.range
      bestScore = score
      tied = false
    } else if (score === bestScore) {
      tied = true
    }
  }
  return tied ? null : best
}

/**
 * Byte-literal port of the server anchor resolver (`_resolve_anchor`,
 * editor_patch_service.py) over the ORIGINAL markdown — no
 * normalization, hard quote disqualification, summed distance,
 * tie/quoteless-ambiguity abstention. The cross-language parity
 * fixture pins this function and the Python original against the same
 * cases; it also answers "would the server apply this edit?" before a
 * proposal is rendered.
 */
export function resolveAnchorInMarkdown(
  content: string,
  edit: { find: string; quoteBefore?: string; quoteAfter?: string },
): number | null {
  const find = edit.find
  if (!find) return null
  const quoteBefore = edit.quoteBefore ?? ''
  const quoteAfter = edit.quoteAfter ?? ''
  const occurrences: number[] = []
  let start = 0
  while (true) {
    const index = content.indexOf(find, start)
    if (index < 0) break
    occurrences.push(index)
    start = index + 1
  }
  if (occurrences.length === 0) return null
  if (occurrences.length === 1) return occurrences[0]
  if (!quoteBefore && !quoteAfter) return null
  let best: number | null = null
  let bestScore: number | null = null
  let tied = false
  for (const index of occurrences) {
    let score = 0
    if (quoteBefore) {
      const beforePos = content.slice(0, index).lastIndexOf(quoteBefore)
      if (beforePos < 0) continue
      score += index - (beforePos + quoteBefore.length)
    }
    if (quoteAfter) {
      const afterPos = content.indexOf(quoteAfter, index + find.length)
      if (afterPos < 0) continue
      score += afterPos - (index + find.length)
    }
    if (bestScore === null || score < bestScore) {
      best = index
      bestScore = score
      tied = false
    } else if (score === bestScore) {
      tied = true
    }
  }
  return tied ? null : best
}

function pickAnchorCandidate(
  editor: Editor,
  candidates: EditorTextRange[],
  locator: AnchorLocator,
): EditorTextRange {
  const { doc } = editor.state
  const before = markdownToPlainTextForEditor(locator.quoteBefore ?? '')
  const after = markdownToPlainTextForEditor(locator.quoteAfter ?? '')
  const scored = candidates.map((candidate) => {
    let score = 0
    if (before) {
      const context = normalizeSearchText(doc.textBetween(Math.max(0, candidate.from - before.length - 20), candidate.from, ' '))
      if (context.endsWith(before)) score += 2
      else if (before.length >= 12 && context.endsWith(before.slice(-12))) score += 1
    }
    if (after) {
      const context = normalizeSearchText(doc.textBetween(candidate.to, Math.min(doc.content.size, candidate.to + after.length + 20), ' '))
      if (context.startsWith(after)) score += 2
      else if (after.length >= 12 && context.startsWith(after.slice(0, 12))) score += 1
    }
    return { candidate, distance: Math.abs(candidate.from - locator.hint), score }
  })
  scored.sort((a, b) => b.score - a.score || a.distance - b.distance)
  return scored[0].candidate
}

function createLocalId(prefix: string): string {
  return createSecurePrefixedId(prefix)
}
