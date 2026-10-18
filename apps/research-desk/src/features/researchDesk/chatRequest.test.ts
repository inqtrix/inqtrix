import { describe, expect, it } from 'vitest'

import { chatCompletionBase, INCOGNITO_THREAD_ID } from './chatRequest'

const messages = [{ content: 'Hallo', role: 'user' as const }]

describe('chatCompletionBase', () => {
  it('marks a turn sent from the incognito thread, and only that one', () => {
    expect(chatCompletionBase({ messages, threadId: INCOGNITO_THREAD_ID })).toMatchObject({
      incognito: true,
    })
    expect(chatCompletionBase({ messages, threadId: 'chat-1' })).not.toHaveProperty('incognito')
  })

  it('asks in knowledge mode only with collections to search', () => {
    expect(
      chatCompletionBase({ knowledgeCollectionIds: ['kc_1'], messages, threadId: 'chat-1' }),
    ).toMatchObject({ knowledgeFilters: { collectionIds: ['kc_1'] }, mode: 'knowledge' })
    const direct = chatCompletionBase({ messages, threadId: 'chat-1' })
    expect(direct).toMatchObject({ includeProgress: false, mode: 'direct_llm' })
    expect(direct).not.toHaveProperty('knowledgeFilters')
  })

  it('carries the interface language with a knowledge ask only', () => {
    expect(
      chatCompletionBase({
        knowledgeCollectionIds: ['kc_1'], messages, threadId: 'chat-1', uiLanguage: 'de',
      }),
    ).toMatchObject({ mode: 'knowledge', uiLanguage: 'de' })
    expect(
      chatCompletionBase({ messages, threadId: 'chat-1', uiLanguage: 'de' }),
    ).not.toHaveProperty('uiLanguage')
  })
})
