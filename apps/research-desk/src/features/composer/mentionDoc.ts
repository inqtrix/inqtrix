import type { JSONContent } from '@tiptap/core'
import { Fragment, Slice, type Node as ProseMirrorNode } from '@tiptap/pm/model'
import type { EditorView } from '@tiptap/pm/view'
import type { ChatContextReferenceRecord } from '@/features/project/types'
import { MENTION_PILL_NAME, type MentionPillKind } from './MentionPill'

/** Mention prefix per positional kind (rules are global and never pills). */
export const PILL_KIND_PREFIX: Record<MentionPillKind, string> = {
  'file-asset': '@files:',
  'file-group': '@filegroups:',
  'research-report': '@research:',
}

export function pillKindToRef(kind: MentionPillKind, id: string): ChatContextReferenceRecord {
  switch (kind) {
    case 'file-asset':
      return { fileId: id, kind: 'file-asset' }
    case 'file-group':
      return { groupId: id, kind: 'file-group' }
    case 'research-report':
      return { kind: 'research-report', runId: id }
  }
}

/** Whether a reference kind is positional (rendered as a `[N]` pill). */
export function isPillKind(kind: ChatContextReferenceRecord['kind']): kind is MentionPillKind {
  return kind === 'file-asset' || kind === 'file-group' || kind === 'research-report'
}

/**
 * Serialize a TipTap document (JSON) to plain text. Each mention pill is replaced
 * via `pillToText`, which receives the pill's kind/id/label and its 0-based
 * reading-order index. Paragraphs are joined with newlines.
 */
export function serializeMentionDoc(
  doc: JSONContent,
  pillToText: (kind: MentionPillKind, id: string, label: string, pillIndex: number) => string,
): string {
  let pillIndex = 0
  const paragraphs = (doc.content ?? []).map((block) => (
    (block.content ?? []).map((node) => {
      if (node.type === MENTION_PILL_NAME) {
        const kind = (node.attrs?.refKind ?? 'file-asset') as MentionPillKind
        const text = pillToText(kind, String(node.attrs?.refId ?? ''), String(node.attrs?.refLabel ?? ''), pillIndex)
        pillIndex += 1
        return text
      }
      if (node.type === 'hardBreak') return '\n'
      return node.text ?? ''
    }).join('')
  ))
  return paragraphs.join('\n').trim()
}

/** The instruction text sent to the model: pills become `[N]` (1-based reading order). */
export function instructionTextFromDoc(doc: JSONContent): string {
  return serializeMentionDoc(doc, (_kind, _id, _label, pillIndex) => `[${pillIndex + 1}]`)
}

/** A round-trippable text with pills as `@kind:label` (used for text improvement). */
export function mentionTextFromDoc(doc: JSONContent): string {
  return serializeMentionDoc(doc, (kind, _id, label) => `${PILL_KIND_PREFIX[kind]}${label}`)
}

export type LabelResolver = (kind: MentionPillKind, label: string) => { id: string; label: string } | null

const TOKEN_KIND: Record<string, MentionPillKind> = {
  files: 'file-asset',
  filegroups: 'file-group',
  research: 'research-report',
}

/** The `@kind:label` token `mentionDocFromText` turns into a pill. */
const MENTION_TOKEN_PATTERN = '@(files|filegroups|research):([a-z0-9-]+)'

/** U+2060 WORD JOINER: invisible and without a line break, and not part of
 * the token, so `@` followed by it never starts one. */
const TOKEN_BREAK = '\u2060'

/** One token kept plain: the word joiner after its `@`. */
function plainToken(token: string): string {
  return `@${TOKEN_BREAK}${token.slice(1)}`
}

/**
 * A slice pasted or dropped into the composer, with its text kept plain.
 *
 * Pasted text is text the user did not write into the composer: a passage of
 * the shared document, another participant's comment, a page from elsewhere.
 * A token resolves against the library of whoever converts it, and labels
 * are slugs of titles that anyone can guess, so its tokens must never turn
 * into references to the user's own files, groups or reports — not when the
 * text arrives, and not when a later conversion re-reads the whole composer
 * (a restored draft). A word joiner after the `@` keeps the text reading the
 * same while no conversion matches it any more. A pill is not text: a
 * reference copied inside the app pastes as the reference it was. A drag
 * inside the composer moves text that is already there; its drop hands back
 * the very slice the drag started with, which stays as it is.
 */
export function plainPastedSlice(slice: Slice, view: Pick<EditorView, 'dragging'>): Slice {
  if (slice === view.dragging?.slice) return slice
  return new Slice(plainFragment(slice.content), slice.openStart, slice.openEnd)
}

function plainFragment(fragment: Fragment): Fragment {
  if (fragment.firstChild?.isInline) return plainInline(fragment)
  const children: ProseMirrorNode[] = []
  fragment.forEach((node) => {
    children.push(node.isLeaf ? node : node.copy(plainFragment(node.content)))
  })
  return Fragment.from(children)
}

/**
 * Characters a token search skips in pasted text: everything but letters,
 * digits, whitespace, the token's own `@`, `:` and `-`, and the word joiner.
 * After the paste, the composer turns Markdown (`**…**`, `` `…` ``) into
 * formatting and drops its markers, so `**@**files:label` reads as a token
 * once the paste is in.
 */
const SKIPPED_IN_TOKEN_SEARCH = /[^\p{L}\p{N}\s@:\u2060-]/u

/**
 * Inline content with a word joiner after the `@` of every token. A
 * conversion reads a block's text without its marks, so a token can run
 * across text nodes (`@files` in bold, `:label` not); it is found in that
 * text, read without the punctuation the composer's formatting may still
 * drop. Every other inline node ends a token, as it does in that text.
 */
function plainInline(fragment: Fragment): Fragment {
  let text = ''
  fragment.forEach((node) => {
    text += node.isText ? node.text ?? '' : '\n'
  })
  // The searched text, and for each of its code units the offset in `text`.
  let searched = ''
  const offsets: number[] = []
  let position = 0
  for (const character of text) {
    if (!SKIPPED_IN_TOKEN_SEARCH.test(character)) {
      searched += character
      for (let unit = 0; unit < character.length; unit += 1) offsets.push(position + unit)
    }
    position += character.length
  }
  const tokenStarts = new Set(
    Array.from(searched.matchAll(new RegExp(MENTION_TOKEN_PATTERN, 'gi')), (match) => offsets[match.index]),
  )
  if (tokenStarts.size === 0) return fragment
  const children: ProseMirrorNode[] = []
  let offset = 0
  fragment.forEach((node) => {
    if (!node.isText) {
      children.push(node)
      offset += 1
      return
    }
    const value = node.text ?? ''
    let next = ''
    for (let index = 0; index < value.length; index += 1) {
      next += value[index]
      if (tokenStarts.has(offset + index)) next += TOKEN_BREAK
    }
    children.push(node.type.schema.text(next, node.marks))
    offset += value.length
  })
  return Fragment.from(children)
}

/** A token's kind and label as a conversion reads them. */
function mentionTokenKey(kind: string, label: string): string {
  return `${kind.toLowerCase()}:${label.toLowerCase()}`
}

/**
 * An improved composer text in which a token converts only if it converted in
 * the text that was improved.
 *
 * The improvement comes from a model. It can drop the invisible word joiner
 * that keeps a pasted token plain (`plainPastedSlice`), or write a token of
 * its own; neither may turn into a reference to the user's library when the
 * user accepts the improvement. Tokens the original text held keep
 * converting.
 */
export function improvedMentionText(improved: string, original: string): string {
  const held = new Set(
    Array.from(
      original.matchAll(new RegExp(MENTION_TOKEN_PATTERN, 'gi')),
      (match) => mentionTokenKey(match[1], match[2]),
    ),
  )
  return improved.replace(
    new RegExp(MENTION_TOKEN_PATTERN, 'gi'),
    (token, kind: string, label: string) => (
      held.has(mentionTokenKey(kind, label)) ? token : plainToken(token)
    ),
  )
}

/**
 * Parse plain text with `@files:`/`@filegroups:`/`@research:` tokens into a
 * TipTap document, turning every resolvable token into a pill. Unknown tokens
 * and tokens kept plain by a word joiner stay as plain text. Used for a
 * restored draft and an accepted text improvement. Pasted text is not
 * converted, and its tokens stay plain in every later conversion
 * (`plainPastedSlice`).
 */
export function mentionDocFromText(text: string, resolve: LabelResolver): JSONContent {
  const tokenRe = new RegExp(MENTION_TOKEN_PATTERN, 'gi')
  const content = text.split('\n').map((line) => {
    const inline: JSONContent[] = []
    let lastIndex = 0
    tokenRe.lastIndex = 0
    let match = tokenRe.exec(line)
    while (match) {
      const kind = TOKEN_KIND[match[1].toLowerCase()]
      const resolved = kind ? resolve(kind, match[2].toLowerCase()) : null
      if (resolved) {
        if (match.index > lastIndex) inline.push({ type: 'text', text: line.slice(lastIndex, match.index) })
        inline.push({ attrs: { refId: resolved.id, refKind: kind, refLabel: resolved.label }, type: MENTION_PILL_NAME })
        lastIndex = match.index + match[0].length
      }
      match = tokenRe.exec(line)
    }
    if (lastIndex < line.length) inline.push({ text: line.slice(lastIndex), type: 'text' })
    return inline.length > 0 ? { content: inline, type: 'paragraph' } : { type: 'paragraph' }
  })
  return { content, type: 'doc' }
}

/** Positional pill references in reading order. */
export function pillRefsFromDoc(doc: JSONContent): ChatContextReferenceRecord[] {
  const refs: ChatContextReferenceRecord[] = []
  for (const block of doc.content ?? []) {
    for (const node of block.content ?? []) {
      if (node.type === MENTION_PILL_NAME) {
        refs.push(pillKindToRef((node.attrs?.refKind ?? 'file-asset') as MentionPillKind, String(node.attrs?.refId ?? '')))
      }
    }
  }
  return refs
}
