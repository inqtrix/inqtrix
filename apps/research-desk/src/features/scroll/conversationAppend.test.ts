import { describe, expect, it } from 'vitest'

import {
  decideConversationAppend,
  type ConversationContentSnapshot,
} from './conversationAppend'

function snapshot(
  key: string | null,
  contentVersion: string,
  contentReady = true,
): ConversationContentSnapshot {
  return { contentReady, contentVersion, key }
}

describe('decideConversationAppend', () => {
  it('does not append on the first ready render for a conversation', () => {
    const decision = decideConversationAppend(null, snapshot('knowledge:a', 'v1'))

    expect(decision.shouldAppend).toBe(false)
    expect(decision.next).toEqual(snapshot('knowledge:a', 'v1'))
  })

  it('appends when the same ready conversation changes content', () => {
    const decision = decideConversationAppend(
      snapshot('knowledge:a', 'v1'),
      snapshot('knowledge:a', 'v2'),
    )

    expect(decision.shouldAppend).toBe(true)
    expect(decision.next).toEqual(snapshot('knowledge:a', 'v2'))
  })

  it('does not advance the ready snapshot while content is loading', () => {
    const previous = snapshot('knowledge:a', 'v1')
    const decision = decideConversationAppend(
      previous,
      snapshot('knowledge:b', 'loading', false),
    )

    expect(decision.shouldAppend).toBe(false)
    expect(decision.next).toBe(previous)
  })

  it('treats loading-to-ready after a key switch as restore, not append', () => {
    const loading = decideConversationAppend(
      snapshot('knowledge:a', 'v1'),
      snapshot('knowledge:b', 'loading', false),
    )
    const ready = decideConversationAppend(
      loading.next,
      snapshot('knowledge:b', 'v1'),
    )

    expect(ready.shouldAppend).toBe(false)
    expect(ready.next).toEqual(snapshot('knowledge:b', 'v1'))
  })

  it('clears the snapshot when no conversation key is active', () => {
    const decision = decideConversationAppend(
      snapshot('knowledge:a', 'v1'),
      snapshot(null, 'hidden'),
    )

    expect(decision.shouldAppend).toBe(false)
    expect(decision.next).toBeNull()
  })
})
