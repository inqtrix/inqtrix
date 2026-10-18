/**
 * Pending-comment highlights in the rendered canvas.
 *
 * Queued selection comments keep their anchor visibly highlighted until
 * they travel with the next submission, so the reader sees which
 * passages already carry a pending comment. The search runs on RENDERED text (the drafts carry
 * the selection's `plainText` twin — the `quote` is markdown source
 * and never occurs verbatim in the DOM), whitespace-tolerant like the
 * knowledge highlight matcher, and paints via the CSS Custom Highlight
 * API — no DOM mutation, so the markdown renderer stays untouched.
 * Browsers without the API simply show no highlight (visual
 * enhancement only; documented, nothing functional depends on it).
 */

export const CANVAS_COMMENT_HIGHLIGHT_NAME = 'inqtrix-canvas-comment'
export const CANVAS_COMMENT_ACTIVE_HIGHLIGHT_NAME =
  'inqtrix-canvas-comment-active'

type TextIndex = {
  /** Concatenated text content of every text node under the root. */
  text: string
  /** One entry per character of `text`: owning node + offset inside. */
  map: { node: Text; offset: number }[]
}

export function buildTextIndex(root: Node): TextIndex {
  const map: TextIndex['map'] = []
  let text = ''
  const walker = (root.ownerDocument ?? document).createTreeWalker(
    root,
    NodeFilter.SHOW_TEXT,
  )
  for (
    let node = walker.nextNode();
    node !== null;
    node = walker.nextNode()
  ) {
    const value = (node as Text).data
    for (let offset = 0; offset < value.length; offset += 1) {
      map.push({ node: node as Text, offset })
    }
    text += value
  }
  return { text, map }
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Whitespace-tolerant pattern (the knowledge matcher's convention):
 * NBSP and newlines in the DOM must not break the match. */
function tolerantPattern(plainText: string): RegExp | null {
  const words = plainText.replace(/\s+/g, ' ').trim().split(' ')
  if (words.length === 0 || words[0] === '') return null
  return new RegExp(words.map(escapeRegExp).join('[\\s\\u00a0]+'), 'g')
}

/**
 * PURE core (node-testable): first occurrence of each rendered text
 * inside the concatenated document text, whitespace-tolerant. Missing
 * texts (document changed since the selection) simply yield no match —
 * the row in the stack still shows the comment, nothing silently dies.
 */
export function findMatchOffsets(
  text: string,
  plainTexts: readonly string[],
): { start: number; end: number }[] {
  if (text.length === 0) return []
  const offsets: { start: number; end: number }[] = []
  for (const plainText of plainTexts) {
    const pattern = tolerantPattern(plainText)
    if (!pattern) continue
    const match = pattern.exec(text)
    if (!match || match[0].length === 0) continue
    offsets.push({ start: match.index, end: match.index + match[0].length })
  }
  return offsets
}

/** DOM glue: offsets from the pure core, ranges via the text index. */
export function findQuoteRanges(
  root: Node,
  plainTexts: readonly string[],
): Range[] {
  if (plainTexts.length === 0) return []
  const index = buildTextIndex(root)
  const ranges: Range[] = []
  for (const offset of findMatchOffsets(index.text, plainTexts)) {
    const start = index.map[offset.start]
    const last = index.map[offset.end - 1]
    if (!start || !last) continue
    const range = (root.ownerDocument ?? document).createRange()
    range.setStart(start.node, start.offset)
    range.setEnd(last.node, last.offset + 1)
    ranges.push(range)
  }
  return ranges
}

/** Feature-detected painter; returns whether the API is available.
 * `activePlainText` paints a SECOND, stronger layer on the entry the
 * user is currently editing. */
export function applyCanvasCommentHighlights(
  root: Node | null,
  plainTexts: readonly string[],
  activePlainText: string | null = null,
): boolean {
  const registry = (
    globalThis as { CSS?: { highlights?: Map<string, unknown> } }
  ).CSS?.highlights
  const HighlightCtor = (
    globalThis as { Highlight?: new (...ranges: Range[]) => unknown }
  ).Highlight
  if (!registry || !HighlightCtor) return false
  const paint = (name: string, texts: readonly string[]) => {
    const ranges = root ? findQuoteRanges(root, texts) : []
    if (ranges.length === 0) registry.delete(name)
    else registry.set(name, new HighlightCtor(...ranges))
  }
  paint(CANVAS_COMMENT_HIGHLIGHT_NAME, root ? plainTexts : [])
  paint(
    CANVAS_COMMENT_ACTIVE_HIGHLIGHT_NAME,
    root && activePlainText ? [activePlainText] : [],
  )
  return true
}
