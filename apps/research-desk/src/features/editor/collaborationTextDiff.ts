import { SUGGESTION_MARK_NAMES } from '@inqtrix/editor-schema'
import { Extension } from '@tiptap/core'
import type { Mark, Node as ProseMirrorNode } from '@tiptap/pm/model'
import { Plugin, PluginKey } from '@tiptap/pm/state'
import { ySyncPluginKey, type ProsemirrorBinding } from '@tiptap/y-tiptap'
import * as Y from 'yjs'

/*
 * The Yjs binding writes a local ProseMirror change into each text by
 * diffing the text's plain characters: a common prefix, a common suffix, and
 * the middle deleted and inserted. When typed text starts with the character
 * that follows it, or a deletion ends with the character before it, that
 * diff is ambiguous, and the binding keeps the reading with the longest
 * prefix. That reading can pair a character of another writer's pending
 * suggestion with a typed character that carries no suggestion: the typed
 * character then takes the suggestion's place, and the suggestion's own
 * character loses its mark. The service sees another writer's suggestion
 * rewritten.
 *
 * The text update here changes the same characters the binding would, but
 * among the readings that do so it reuses an existing character only for a
 * character with the same suggestion marks. A character never changes the
 * insertion it belongs to: where every reading would keep a proposed
 * character for text that does not propose it (text typed over a
 * suggestion, the same text typed again), or give an existing character to
 * a proposal, that character is written anew. The service counts a
 * suggestion as removed by an edit only when the characters it proposed
 * are gone; kept without their mark they would read as accepted.
 * It writes the characters the way the binding does, and the binding then
 * formats the text as before. (Writing new characters with their final
 * formatting instead would let the binding's formatting pass delete format
 * items written in the same change, which the service refuses from a
 * suggest-only writer as transient history.)
 */

const markAwareBindings = new WeakSet<object>()

/** The attribute key the binding writes for a mark that may overlap
 * itself carries a hash after this separator. */
const HASHED_ATTRIBUTE = /(.*)(--[a-zA-Z0-9+/=]{8})$/

/**
 * Makes `binding` write local changes with the mark-aware text update. The
 * binding pairs every changed text with the ProseMirror text nodes it holds
 * and records that pair in its mapping before it diffs the text; the update
 * runs at that moment, on the binding's own pairing.
 */
export function installMarkAwareTextDiff(binding: ProsemirrorBinding): void {
  if (markAwareBindings.has(binding)) return
  markAwareBindings.add(binding)
  const writeLocalChange = binding._prosemirrorChanged.bind(binding)
  binding._prosemirrorChanged = (document: ProseMirrorNode) => {
    const mapping = binding.mapping
    binding.mapping = pairingMapping(mapping)
    try {
      writeLocalChange(document)
    } finally {
      binding.mapping = mapping
    }
  }
}

/** The Tiptap extension that installs the mark-aware text update on the
 * editor's Yjs binding. */
export const MarkAwareCollaborationTextDiff = Extension.create({
  name: 'markAwareCollaborationTextDiff',
  addProseMirrorPlugins() {
    return [
      new Plugin({
        key: new PluginKey('markAwareCollaborationTextDiff'),
        view: (view) => {
          const binding = ySyncPluginKey.getState(view.state)?.binding as
            ProsemirrorBinding | undefined
          if (binding) installMarkAwareTextDiff(binding)
          return {}
        },
      }),
    ]
  },
})

type BindingMapping = ProsemirrorBinding['mapping']

/** The binding's mapping, updating each text of the Yjs document it pairs
 * with ProseMirror text nodes before the pair is recorded. */
function pairingMapping(mapping: BindingMapping): BindingMapping {
  const pair = (key: unknown, value: unknown): BindingMapping => {
    if (
      key instanceof Y.XmlText
      && key.doc !== null
      && Array.isArray(value)
    ) {
      writeTextIdentityAware(key, value as ProseMirrorNode[])
    }
    mapping.set(key as never, value as never)
    return proxy
  }
  const proxy: BindingMapping = new Proxy(mapping, {
    get(target, property) {
      if (property === 'set') return pair
      const value: unknown = Reflect.get(target, property, target)
      return typeof value === 'function' ? value.bind(target) : value
    },
  })
  return proxy
}

type Character = {
  /** The suggestion whose insertion the character belongs to, or ''. */
  insertion: string
  signature: string
}

/** Writes the characters of `nodes` into `text`, changing as few characters
 * as the binding's own diff and reusing a character only for one with the
 * same suggestion marks. Leaves a text alone whose characters are equal
 * already, or that holds anything but characters. */
export function writeTextIdentityAware(
  text: Y.XmlText,
  nodes: readonly ProseMirrorNode[],
): void {
  const delta = text.toDelta() as Array<{ attributes?: Record<string, unknown>; insert?: unknown }>
  let before = ''
  const beforeCharacters: Character[] = []
  for (const operation of delta) {
    if (typeof operation.insert !== 'string') return
    const attributes = operation.attributes ?? {}
    const character = {
      insertion: attributeInsertion(attributes),
      signature: attributeSignature(attributes),
    }
    before += operation.insert
    for (let index = 0; index < operation.insert.length; index += 1) {
      beforeCharacters.push(character)
    }
  }
  let after = ''
  const afterCharacters: Character[] = []
  for (const node of nodes) {
    const value = node.text ?? ''
    const character = {
      insertion: markInsertion(node.marks),
      signature: markSignature(node.marks),
    }
    after += value
    for (let index = 0; index < value.length; index += 1) afterCharacters.push(character)
  }
  const { index, remove, insert } = before === after
    ? { index: 0, insert: 0, remove: 0 }
    : simpleStringDiff(before, after)
  const shift = before === after
    ? 0
    : reuseShift(before, after, beforeCharacters, afterCharacters, index, remove, insert)
  const window = keepInsertions(beforeCharacters, afterCharacters, index - shift, remove, insert)
  // The binding's own reading: it writes exactly this itself.
  if (window.start === index && window.remove === remove) return
  if (window.remove > 0) text.delete(window.start, window.remove)
  if (window.insert > 0) text.insert(window.start, after.slice(window.start, window.start + window.insert))
}

/**
 * Widens a changed window until no character kept outside it changes the
 * insertion it belongs to: from the first such character in front of the
 * window to the last one behind it. Both halves of a surrogate pair carry
 * the same marks, so the widened window never splits one.
 */
function keepInsertions(
  beforeCharacters: readonly Character[],
  afterCharacters: readonly Character[],
  start: number,
  remove: number,
  insert: number,
): { insert: number; remove: number; start: number } {
  if (remove === 0 && insert === 0) {
    // The characters are equal and only their formatting changes.
    let first = -1
    let last = -1
    for (let index = 0; index < beforeCharacters.length; index += 1) {
      if (beforeCharacters[index]!.insertion === afterCharacters[index]!.insertion) continue
      if (first < 0) first = index
      last = index
    }
    return first < 0
      ? { insert: 0, remove: 0, start }
      : { insert: last + 1 - first, remove: last + 1 - first, start: first }
  }
  const offset = insert - remove
  let first = start
  for (let index = 0; index < start; index += 1) {
    if (beforeCharacters[index]!.insertion !== afterCharacters[index]!.insertion) {
      first = index
      break
    }
  }
  let beforeEnd = start + remove
  for (let index = beforeCharacters.length - 1; index >= beforeEnd; index -= 1) {
    if (beforeCharacters[index]!.insertion !== afterCharacters[index + offset]!.insertion) {
      beforeEnd = index + 1
      break
    }
  }
  return { insert: beforeEnd + offset - first, remove: beforeEnd - first, start: first }
}

/** The binding's own diff: the longest common prefix, then the longest
 * common suffix of the rest, never splitting a surrogate pair. */
function simpleStringDiff(before: string, after: string): {
  index: number
  insert: number
  remove: number
} {
  let left = 0
  while (left < before.length && left < after.length && before[left] === after[left]) left += 1
  if (left > 0 && isHighSurrogate(before.charCodeAt(left - 1))) left -= 1
  let right = 0
  while (
    right + left < before.length
    && right + left < after.length
    && before[before.length - right - 1] === after[after.length - right - 1]
  ) right += 1
  if (right > 0 && isLowSurrogate(before.charCodeAt(before.length - right))) right -= 1
  return {
    index: left,
    insert: after.length - left - right,
    remove: before.length - left - right,
  }
}

/**
 * How far to move the changed window of the binding's diff to the left.
 * Moving it by one keeps the number of changed characters when the last
 * character of the old window equals the last of the new one: that pair
 * joins the common suffix and the prefix gives up its last pair. Among the
 * windows reachable that way, the one whose reused pairs disagree in
 * suggestion marks least wins; on a tie the binding's own window stays.
 */
function reuseShift(
  before: string,
  after: string,
  beforeCharacters: readonly Character[],
  afterCharacters: readonly Character[],
  index: number,
  remove: number,
  insert: number,
): number {
  const beforeEnd = index + remove
  const afterEnd = index + insert
  const disagree = (left: number, right: number): number => (
    beforeCharacters[left]!.signature === afterCharacters[right]!.signature ? 0 : 1
  )
  let cost = 0
  let best = 0
  let bestShift = 0
  for (let shift = 0; index - shift - 1 >= 0; shift += 1) {
    const joining = beforeEnd - shift - 1
    const joiningAfter = afterEnd - shift - 1
    if (before[joining] !== after[joiningAfter]) break
    const leaving = index - shift - 1
    cost += disagree(joining, joiningAfter) - disagree(leaving, leaving)
    const start = index - shift - 1
    const splitsPair = (start > 0 && isHighSurrogate(before.charCodeAt(start - 1)))
      || isLowSurrogate(before.charCodeAt(beforeEnd - shift - 1))
    if (cost < best && !splitsPair) {
      best = cost
      bestShift = shift + 1
    }
  }
  return bestShift
}

function isHighSurrogate(code: number): boolean {
  return code >= 0xd800 && code <= 0xdbff
}

function isLowSurrogate(code: number): boolean {
  return code >= 0xdc00 && code <= 0xdfff
}

function attributeMarkName(key: string): string {
  return HASHED_ATTRIBUTE.exec(key)?.[1] ?? key
}

/** The suggestion whose insertion a character of the Yjs text belongs to. */
function attributeInsertion(attributes: Readonly<Record<string, unknown>>): string {
  for (const [key, value] of Object.entries(attributes)) {
    if (attributeMarkName(key) !== 'insertion' || !value || typeof value !== 'object') continue
    const suggestionId = Reflect.get(value, 'suggestionId')
    return typeof suggestionId === 'string' ? suggestionId : ''
  }
  return ''
}

/** The suggestion whose insertion a ProseMirror character belongs to. */
function markInsertion(marks: readonly Mark[]): string {
  const mark = marks.find((candidate) => candidate.type.name === 'insertion')
  const suggestionId = mark?.attrs.suggestionId
  return typeof suggestionId === 'string' ? suggestionId : ''
}

/** The suggestion marks of a character written in the Yjs text. */
function attributeSignature(attributes: Readonly<Record<string, unknown>>): string {
  return Object.entries(attributes)
    .filter(([key, value]) => value !== null && SUGGESTION_MARK_NAMES.has(attributeMarkName(key)))
    .map(([key, value]) => `${attributeMarkName(key)}\u0000${stableJson(value)}`)
    .sort()
    .join('\u0001')
}

/** The suggestion marks of a ProseMirror character. */
function markSignature(marks: readonly Mark[]): string {
  return marks
    .filter((mark) => SUGGESTION_MARK_NAMES.has(mark.type.name))
    .map((mark) => `${mark.type.name}\u0000${stableJson(mark.attrs)}`)
    .sort()
    .join('\u0001')
}

function stableJson(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value) ?? 'undefined'
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`
  return `{${Object.keys(value as Record<string, unknown>)
    .sort()
    .map((key) => `${JSON.stringify(key)}:${stableJson((value as Record<string, unknown>)[key])}`)
    .join(',')}}`
}
