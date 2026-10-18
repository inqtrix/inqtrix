export type CollectionMentionState = {
  /** Char index of the `@` in the textarea value. */
  start: number
  query: string
}

/** Detect the active `@`-collection mention at the caret — shared by the
 * Knowledge composer, the Agent composer and the plan-gate task editor. */
export function detectCollectionMention(
  value: string,
  caret: number,
): CollectionMentionState | null {
  const safeCaret = Math.max(0, Math.min(caret, value.length))
  const beforeCaret = value.slice(0, safeCaret)
  const match = /(?:^|\s)@([^\s@]*)$/i.exec(beforeCaret)
  if (!match) return null
  return {
    query: match[1],
    start: safeCaret - match[1].length - 1,
  }
}
