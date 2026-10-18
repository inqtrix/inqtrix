export type ConversationContentSnapshot = {
  contentReady: boolean
  contentVersion: string
  key: string | null
}

export type ConversationAppendDecision = {
  next: ConversationContentSnapshot | null
  shouldAppend: boolean
}

/**
 * Decide whether a conversation content change should run same-thread
 * auto-follow. Conversation switches and lazy-load reveals are restored by
 * `useScrollRestoration`; treating them as appends can paint the reused
 * ScrollArea at a stale position before the real content settles.
 */
export function decideConversationAppend(
  previous: ConversationContentSnapshot | null,
  current: ConversationContentSnapshot,
): ConversationAppendDecision {
  if (!current.key) return { next: null, shouldAppend: false }
  if (!current.contentReady) return { next: previous, shouldAppend: false }

  const shouldAppend = Boolean(
    previous?.contentReady
      && previous.key === current.key
      && previous.contentVersion !== current.contentVersion,
  )
  return { next: current, shouldAppend }
}
