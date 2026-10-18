import { createElement } from 'react'
import { renderToString } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import {
  ChatMessagesLoadError,
  chatStructuralPhaseForState,
  shouldClearAcceptedChatDraft,
} from './ChatWorkspace'

describe('chat draft submission ownership', () => {
  it('preserves text and pills until preflight accepts the exact snapshot', () => {
    expect(shouldClearAcceptedChatDraft(
      false,
      'Question',
      'Question',
      ['file:f1'],
      ['file:f1'],
    )).toBe(false)
    expect(shouldClearAcceptedChatDraft(
      true,
      'Question with follow-up',
      'Question',
      ['file:f1'],
      ['file:f1'],
    )).toBe(false)
    expect(shouldClearAcceptedChatDraft(
      true,
      'Question',
      'Question',
      ['file:f1', 'file:f2'],
      ['file:f1'],
    )).toBe(false)
    expect(shouldClearAcceptedChatDraft(
      true,
      'Question',
      'Question',
      ['file:f1'],
      ['file:f1'],
    )).toBe(true)
  })
})

describe('chat structural load phase', () => {
  it('publishes a terminal load error instead of pending or empty content', () => {
    expect(chatStructuralPhaseForState({
      hasThreadMessages: false,
      isMessagesLoading: false,
      messagesLoadError: 'offline',
      selectedThread: true,
    })).toBe('error')
  })

  it('renders the selected-thread failure with its retry action in-region', () => {
    const html = renderToString(createElement(ChatMessagesLoadError, {
      error: 'network unavailable',
      failedLabel: 'Conversation unavailable',
      loadingLabel: 'Loading conversation',
      onRetry: () => undefined,
      retryLabel: 'Try again',
    }))

    expect(html).toContain('data-chat-messages-load-error=""')
    expect(html).toContain('role="alert"')
    expect(html).toContain('Conversation unavailable')
    expect(html).toContain('network unavailable')
    expect(html).toContain('Try again')
  })

  it('returns to pending after retry clears the failure', () => {
    expect(chatStructuralPhaseForState({
      hasThreadMessages: false,
      isMessagesLoading: true,
      messagesLoadError: null,
      selectedThread: true,
    })).toBe('pending')
  })

  it('only treats a successfully resolved message-less thread as empty', () => {
    expect(chatStructuralPhaseForState({
      hasThreadMessages: false,
      isMessagesLoading: false,
      messagesLoadError: null,
      selectedThread: true,
    })).toBe('empty')
  })
})
