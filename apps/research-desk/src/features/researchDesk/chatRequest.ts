import type { ChatCompletionMessage, ChatCompletionRequest } from '@/api/inqtrixClient'

/** The local-only chat thread of an incognito session. */
export const INCOGNITO_THREAD_ID = 'chat-incognito-session'

/**
 * The request every chat send starts from. A turn sent from the incognito
 * thread asks the server to keep its content out of traces; the flag follows
 * the thread the message was sent from, not the switch's state later on.
 * Collections to search make the ask a knowledge-mode ask, which carries
 * the interface language for a question that leaves its own open.
 */
export function chatCompletionBase({
  agentOverrides,
  knowledgeCollectionIds = [],
  messages,
  stack,
  threadId,
  uiLanguage,
}: {
  agentOverrides?: ChatCompletionRequest['agentOverrides']
  knowledgeCollectionIds?: string[]
  messages: ChatCompletionMessage[]
  stack?: string
  threadId: string
  uiLanguage?: ChatCompletionRequest['uiLanguage']
}): ChatCompletionRequest {
  const knowledgeMode = knowledgeCollectionIds.length > 0
  return {
    agentOverrides,
    ...(threadId === INCOGNITO_THREAD_ID ? { incognito: true } : {}),
    includeProgress: false,
    ...(knowledgeMode
      ? { knowledgeFilters: { collectionIds: knowledgeCollectionIds }, uiLanguage }
      : {}),
    messages,
    mode: knowledgeMode ? 'knowledge' : 'direct_llm',
    stack,
  }
}
