/**
 * The citation grammar of inline evidence labels, the frontend's copy of
 * ``src/inqtrix/citation_labels.py``.
 *
 * One label per bracket is the form every reader parses (``[K2]``, also as
 * link text ``[W7](https://…)``). A grouped bracket (``[K2, W7]``,
 * ``[K2; W7]``) is the same citation written differently; the backend
 * writes it singly before storing agent text, and this copy does the same at
 * render time for text that does not pass that boundary: knowledge and
 * research answers, user-edited canvases, and artifacts stored without it.
 * A bracket no reader can parse (a range ``[K3–K5]``, a slash, a prose join,
 * a label with a note, a group used as link text) and every platform marker
 * (a word and a colon first, ``[unbelegt: K2]``) is opaque: nothing inside
 * it becomes a citation chip. A backslash-escaped bracket is literal text.
 *
 * Fenced and inline code are never read; URLs are read like prose, since a
 * link destination ends at ``)`` and a label may follow it directly. Lines
 * end where Python's ``str.splitlines`` ends them, and digits and separators
 * are ASCII, as in the backend; a shared fixture
 * (``tests/fixtures/citation_label_parity.json``) holds both copies in
 * parity. Formulas are not separated here: the backend recognizes them with a
 * heuristic that has no frontend copy, and the fixture holds no formula case.
 */

export type CitationLabelPredicate = (label: string) => boolean

export type ProsePiece = { prose: boolean; text: string }

const LINE_BREAKS = '\\n\\r\\v\\f\\x1c\\x1d\\x1e\\x85\\u2028\\u2029'
const LINES = new RegExp(`[^${LINE_BREAKS}]*(?:\\r\\n|[${LINE_BREAKS}])|[^${LINE_BREAKS}]+$`, 'gu')
const FENCE_OPEN = /^[ \t]{0,3}(`{3,}|~{3,})/
const ANY_BRACKET = /(?<!\\)\[([^[\]\n]{1,200})\](?!\()/gu
const BRACKET_BEFORE_LINK = /(?<!\\)\[([^[\]\n]{1,200})\](?=\()/gu
const MARKER = /^[ \t]*\p{L}+[ \t]*:/u
const LABEL = '[A-Z][0-9]+'
const SEPARATOR = '[ \\t]*[,;][ \\t]*'
const GROUP = new RegExp(`(?<!\\\\)\\[[ \\t]*(${LABEL}(?:${SEPARATOR}${LABEL})+)[ \\t]*\\](?!\\()`, 'g')
const GROUP_AS_LINK = new RegExp(`(?<!\\\\)\\[[ \\t]*(${LABEL}(?:${SEPARATOR}${LABEL})+)[ \\t]*\\](?=\\()`, 'g')
const SPLIT_SEPARATOR = new RegExp(SEPARATOR)
const CITATION = /^\[([A-Z][0-9]+)\]$/
const LABEL_TOKEN = /(?<![A-Za-z0-9])([A-Z][0-9]+)(?![A-Za-z0-9])/g
const MEMBER_LABEL = new RegExp(LABEL, 'g')

/** Build the predicate of a label family string such as ``'KW'``. */
export function familyPredicate(families: string): CitationLabelPredicate {
  const pattern = new RegExp(`^[${families}][0-9]+$`)
  return (label) => pattern.test(label)
}

/** *markdown* split into prose and code pieces whose concatenation is the input. */
export function prosePieces(markdown: string): ProsePiece[] {
  const pieces: ProsePiece[] = []
  let fenceCharacter = ''
  let fenceWidth = 0
  for (const line of markdown.match(LINES) ?? []) {
    if (fenceCharacter) {
      pieces.push({ prose: false, text: line })
      if (isFenceClose(line, fenceCharacter, fenceWidth)) {
        fenceCharacter = ''
        fenceWidth = 0
      }
      continue
    }
    const opener = FENCE_OPEN.exec(line)
    if (opener) {
      fenceCharacter = opener[1][0]
      fenceWidth = opener[1].length
      pieces.push({ prose: false, text: line })
      continue
    }
    splitLine(line, pieces)
  }
  return pieces
}

function isFenceClose(line: string, character: string, width: number): boolean {
  const stripped = line.replace(/^[ \t]+/, '')
  if (line.length - stripped.length > 3) return false
  let markerWidth = 0
  while (markerWidth < stripped.length && stripped[markerWidth] === character) markerWidth += 1
  return markerWidth >= width && stripped.slice(markerWidth).trim() === ''
}

function splitLine(line: string, pieces: ProsePiece[]): void {
  let prose = ''
  let index = 0
  while (index < line.length) {
    if (line[index] !== '`') {
      prose += line[index]
      index += 1
      continue
    }
    let width = 0
    while (line[index + width] === '`') width += 1
    const delimiter = '`'.repeat(width)
    const close = line.indexOf(delimiter, index + width)
    if (close < 0) {
      prose += delimiter
      index += width
      continue
    }
    if (prose) pieces.push({ prose: true, text: prose })
    prose = ''
    pieces.push({ prose: false, text: line.slice(index, close + width) })
    index = close + width
  }
  if (prose) pieces.push({ prose: true, text: prose })
}

/** Grouped citation brackets of *isLabel* labels written singly, in prose only. */
export function canonicalizeCitationGroups(
  markdown: string,
  isLabel: CitationLabelPredicate,
): string {
  if (!markdown.includes('[')) return markdown
  return prosePieces(markdown)
    .map((piece) => (piece.prose ? canonicalizeProse(piece.text, isLabel) : piece.text))
    .join('')
}

function canonicalizeProse(text: string, isLabel: CitationLabelPredicate): string {
  let out = ''
  let cursor = 0
  for (const group of citationGroupSpans(text, isLabel)) {
    out += text.slice(cursor, group.start) + group.labels.map((member) => `[${member.label}]`).join(' ')
    cursor = group.end
  }
  return out + text.slice(cursor)
}

export type CitationGroupSpan = {
  start: number
  end: number
  /** The members `canonicalizeCitationGroups` writes singly, each once. */
  labels: { start: number; label: string }[]
}

/** The grouped citation brackets of one prose *text* that
 * `canonicalizeCitationGroups` writes singly, with the position of each
 * member label, in text order. */
export function citationGroupSpans(text: string, isLabel: CitationLabelPredicate): CitationGroupSpan[] {
  const groups: CitationGroupSpan[] = []
  for (const match of text.matchAll(GROUP)) {
    const membersStart = match.index + match[0].indexOf(match[1])
    const labels: CitationGroupSpan['labels'] = []
    let parsed = true
    for (const token of match[1].matchAll(MEMBER_LABEL)) {
      if (!isLabel(token[0])) {
        parsed = false
        break
      }
      if (!labels.some((member) => member.label === token[0])) {
        labels.push({ start: membersStart + token.index, label: token[0] })
      }
    }
    if (parsed) groups.push({ start: match.index, end: match.index + match[0].length, labels })
  }
  return groups
}

/** Label-bearing brackets of *isLabel* labels that no reader can parse, verbatim, in text order. */
export function malformedCitationBrackets(
  markdown: string,
  isLabel: CitationLabelPredicate,
): string[] {
  const found: string[] = []
  for (const piece of prosePieces(canonicalizeCitationGroups(markdown, isLabel))) {
    if (!piece.prose) continue
    for (const span of opaqueSpans(piece.text, isLabel)) {
      if (!span.marker) found.push(span.text)
    }
  }
  return found
}

type OpaqueSpan = { start: number; end: number; text: string; marker: boolean }

/**
 * Brackets in one prose *text* whose inside must not become chips: platform
 * markers (also as link text) and brackets no reader can parse. Sorted by
 * position.
 */
export function opaqueSpans(text: string, isLabel: CitationLabelPredicate): OpaqueSpan[] {
  const spans: OpaqueSpan[] = []
  for (const match of text.matchAll(GROUP_AS_LINK)) {
    if (!match[1].split(SPLIT_SEPARATOR).every(isLabel)) continue
    spans.push({ start: match.index, end: match.index + match[0].length, text: match[0], marker: false })
  }
  for (const match of text.matchAll(BRACKET_BEFORE_LINK)) {
    if (!MARKER.test(match[1])) continue
    spans.push({ start: match.index, end: match.index + match[0].length, text: match[0], marker: true })
  }
  for (const match of text.matchAll(ANY_BRACKET)) {
    const inner = match[1]
    if (CITATION.test(match[0]) && isLabel(match[0].slice(1, -1))) continue
    const marker = MARKER.test(inner)
    if (!marker && inner.endsWith('\\')) continue
    const hasLabel = [...inner.matchAll(LABEL_TOKEN)].some((token) => isLabel(token[1]))
    if (!hasLabel) continue
    spans.push({ start: match.index, end: match.index + match[0].length, text: match[0], marker })
  }
  return spans.sort((left, right) => left.start - right.start)
}
