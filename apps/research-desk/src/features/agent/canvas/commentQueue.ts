/**
 * Canvas comment queue: selection comments collected in the UI and
 * bundled into the run request's dedicated `canvas_context` field —
 * NEVER serialized into the question text (the question column is
 * clipped at persistence and reaches share-inbox titles).
 */

/** Mirror of the server's `CANVAS_CONTEXT_MAX_COMMENTS` bound. The UI
 * refuses to queue past it with a visible hint — the server would
 * reject the whole submission anyway (visibly, never truncating). */
export const AGENT_CANVAS_COMMENT_LIMIT = 20

/** One queued selection comment (camelCase twin of the wire shape,
 * plus two UI-ONLY fields the wire mapper never serializes: `id` keys
 * the stacked rows and the edit round-trip, `plainText` is the
 * RENDERED selection text the canvas highlight search uses — the
 * `quote` is markdown SOURCE and does not occur verbatim in the DOM). */
export type AgentCanvasCommentDraft = {
  /** UI-only stable identity — never sent. */
  id: string
  artifactId: string
  revision: number
  quote: string
  quoteBefore: string
  quoteAfter: string
  comment: string
  /** UI-only rendered-text twin of `quote` — never sent. */
  plainText: string
}

/** The submit-ready attachment: open document + queued comments. */
export type AgentCanvasSubmitContext = {
  artifactId: string
  revision: number
  comments: AgentCanvasCommentDraft[]
}

/**
 * Anchor a quote inside the artifact's markdown SOURCE with the editor
 * convention's 80-character context windows (anchoring.ts). A quote the
 * source does not contain verbatim (markdown mapping failed, or the
 * document changed since selection) keeps empty contexts — the quote
 * itself still travels, and the server passes anchors through verbatim.
 */
export function anchorFromMarkdownQuote(
  markdown: string,
  quote: string,
): { quote: string; quoteBefore: string; quoteAfter: string } {
  const index = markdown.indexOf(quote)
  if (index === -1) return { quote, quoteAfter: '', quoteBefore: '' }
  return {
    quote,
    quoteAfter: markdown.slice(index + quote.length, index + quote.length + 80),
    quoteBefore: markdown.slice(Math.max(0, index - 80), index),
  }
}

/** Bundle the queue for submission; an empty queue attaches nothing.
 * The open document is the first queued comment's target. */
export function canvasContextFromQueue(
  queue: AgentCanvasCommentDraft[],
): AgentCanvasSubmitContext | undefined {
  if (queue.length === 0) return undefined
  return {
    artifactId: queue[0].artifactId,
    comments: queue,
    revision: queue[0].revision,
  }
}

/** A canvas document pinned via @-mention — no comments. */
export type AgentCanvasDocumentPin = {
  artifactId: string
  revision: number
}

/**
 * THE precedence rule of the single-document channel: queued
 * comments BIND the attachment to their document — the wire carries
 * exactly one artifact, so a mention pin only travels while the queue
 * is empty. The UI enforces the same rule at the source (pinning is
 * cleared when a comment is queued; mention candidates hide while the
 * queue is non-empty), so this fold never silently drops a conflicting
 * pin — it cannot receive one.
 */
export function canvasContextFromSelection(
  queue: AgentCanvasCommentDraft[],
  pin: AgentCanvasDocumentPin | null,
): AgentCanvasSubmitContext | undefined {
  const fromQueue = canvasContextFromQueue(queue)
  if (fromQueue) return fromQueue
  if (!pin) return undefined
  return { artifactId: pin.artifactId, comments: [], revision: pin.revision }
}

/**
 * THE queue-consumption policy (single call site in the submit path):
 * the queue empties ONLY when the server accepted the run. A rejected
 * or failed submission keeps every queued comment so nothing the user
 * wrote is lost to a retry.
 */
export function settleCanvasQueueAfterSubmit(
  queue: AgentCanvasCommentDraft[],
  accepted: boolean,
): AgentCanvasCommentDraft[] {
  return accepted ? [] : queue
}

/**
 * The pin a newly queued comment leaves. Comments bind the single-document
 * channel to THEIR document: a pin on that same document is no conflict and
 * stays, a pin on another one ends — its capsule stays in the sentence as
 * the plain word the user typed, so nothing they wrote disappears.
 */
export function pinAfterComment(pinned: string | null, commentArtifactId: string): string | null {
  return pinned === commentArtifactId ? pinned : null
}
