/**
 * Marks the statements of a rendered Markdown text that the review found
 * insufficiently supported (the outcome record's `marks`, built by the
 * server's `claim_states`).
 *
 * A mark names its statement by its passage as the text holds it. The
 * passage is found again by its words, not by its characters: the text
 * reaches the renderer with its citations as links and its emphasis as
 * elements, so the words are what both sides share. Citation links stay
 * outside the mark, and so does every label-like word (`W1`, `K12`), on
 * both sides alike. Each run of marked text becomes one `span` carrying
 * `data-claim-state` and `data-claim-note`, styled in `globals.css`; the
 * first span of a mark takes the focus, so a keyboard reaches its note.
 */

import { isCitationHref } from './citationLinks'

/** One mark as the renderer needs it. */
export type ClaimMarkView = {
  /** The statement as the delivered Markdown holds it, citations included. */
  quote: string
  state: string
  /** Why it is marked, in the reader's language. */
  note: string
}

type ClaimMarkHastNode = {
  children?: ClaimMarkHastNode[]
  properties?: Record<string, unknown>
  tagName?: string
  type: string
  value?: string
}

type Word = { key: string; node: ClaimMarkHastNode; start: number; end: number }

const WORD = /[\p{L}\p{N}]+/gu
const LABEL_LIKE = /^[kwe]\d{1,3}$/
const LINK_DESTINATION = /\]\([^)\s]*\)/g

/** Rehype plugin; the marks are found in text order. */
export function rehypeClaimMarks(options: { marks: readonly ClaimMarkView[] }) {
  return (tree: ClaimMarkHastNode) => {
    markClaims(tree, options.marks)
  }
}

function markClaims(tree: ClaimMarkHastNode, marks: readonly ClaimMarkView[]) {
  const parents = new Map<ClaimMarkHastNode, ClaimMarkHastNode>()
  const words: Word[] = []
  collectWords(tree, parents, words)
  const cuts = new Map<ClaimMarkHastNode, { start: number; end: number; mark: ClaimMarkView; first: boolean }[]>()
  let cursor = 0
  for (const mark of marks) {
    const wanted = markWords(mark.quote)
    if (!wanted.length) continue
    const at = findWords(words, wanted, cursor) ?? findWords(words, wanted, 0)
    if (at === undefined) {
      console.warn('A claim mark was not found in the rendered text; it is not shown.')
      continue
    }
    cursor = at + wanted.length
    const first = words[at]
    const last = words[cursor - 1]
    let placed = false
    for (const node of textNodesBetween(tree, first.node, last.node)) {
      const value = node.value ?? ''
      const start = node === first.node ? first.start : 0
      const end = node === last.node ? markEnd(words, cursor - 1) : value.length
      // Only the mark's own end sheds its trailing space: inside the mark a
      // space before an emphasis keeps the underline whole.
      const trimmed = node === last.node ? start + value.slice(start, end).trimEnd().length : end
      if (trimmed <= start) continue
      const list = cuts.get(node) ?? []
      list.push({ start, end: trimmed, mark, first: !placed })
      cuts.set(node, list)
      placed = true
    }
  }
  for (const [node, list] of cuts) {
    const parent = parents.get(node)
    if (!parent?.children) continue
    const position = parent.children.indexOf(node)
    if (position < 0) continue
    parent.children.splice(position, 1, ...split(node.value ?? '', list))
  }
}

/** Every word of the text in document order; a citation link's text is a
 * label, which no mark's words hold. */
function collectWords(
  node: ClaimMarkHastNode,
  parents: Map<ClaimMarkHastNode, ClaimMarkHastNode>,
  words: Word[],
) {
  for (const child of node.children ?? []) {
    parents.set(child, node)
    if (child.type === 'text') {
      for (const match of (child.value ?? '').matchAll(WORD)) {
        const key = match[0].normalize('NFC').toLowerCase()
        if (LABEL_LIKE.test(key)) continue
        words.push({ key, node: child, start: match.index, end: match.index + match[0].length })
      }
      continue
    }
    collectWords(child, parents, words)
  }
}

/** The words of a mark's passage, read like the rendered text. */
function markWords(quote: string): string[] {
  return [...quote.replace(LINK_DESTINATION, ']').matchAll(WORD)]
    .map((match) => match[0].normalize('NFC').toLowerCase())
    .filter((key) => !LABEL_LIKE.test(key))
}

function findWords(words: readonly Word[], wanted: readonly string[], from: number): number | undefined {
  for (let at = from; at + wanted.length <= words.length; at += 1) {
    if (wanted.every((key, offset) => words[at + offset].key === key)) return at
  }
  return undefined
}

/** Where a mark ending on `words[index]` ends in its text node: up to the
 * next word of that node, so its closing punctuation belongs to it. */
function markEnd(words: readonly Word[], index: number): number {
  const word = words[index]
  const next = words[index + 1]
  return next && next.node === word.node ? next.start : (word.node.value ?? '').length
}

/** The text nodes from *first* to *last* in document order, citation links
 * and whitespace-only nodes left out. */
function textNodesBetween(
  tree: ClaimMarkHastNode,
  first: ClaimMarkHastNode,
  last: ClaimMarkHastNode,
): ClaimMarkHastNode[] {
  const found: ClaimMarkHastNode[] = []
  let inside = false
  const walk = (node: ClaimMarkHastNode): boolean => {
    for (const child of node.children ?? []) {
      if (child.type === 'element' && child.tagName === 'a' && isCitationHref(child.properties?.href)) {
        continue
      }
      if (child.type === 'text') {
        if (child === first) inside = true
        if (inside && (child === first || child === last || (child.value ?? '').trim())) found.push(child)
        if (child === last) return true
        continue
      }
      if (walk(child)) return true
    }
    return false
  }
  walk(tree)
  return found
}

/** *value* cut into plain text and one marked span per cut, in order. */
function split(
  value: string,
  cuts: { start: number; end: number; mark: ClaimMarkView; first: boolean }[],
): ClaimMarkHastNode[] {
  const pieces: ClaimMarkHastNode[] = []
  let position = 0
  for (const cut of [...cuts].sort((left, right) => left.start - right.start)) {
    if (cut.start < position) continue
    if (cut.start > position) pieces.push({ type: 'text', value: value.slice(position, cut.start) })
    pieces.push({
      type: 'element',
      tagName: 'span',
      properties: {
        className: ['claim-mark'],
        dataClaimState: cut.mark.state,
        dataClaimNote: cut.mark.note,
        title: cut.mark.note,
        ...(cut.first ? { dataClaimFirst: '', tabIndex: 0 } : {}),
      },
      children: [{ type: 'text', value: value.slice(cut.start, cut.end) }],
    })
    position = cut.end
  }
  if (position < value.length) pieces.push({ type: 'text', value: value.slice(position) })
  return pieces
}
