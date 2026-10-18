import { describe, expect, it } from 'vitest'

import { createEmptyProjectState } from '@/features/project/seedProject'
import type {
  ChatMessageRecord,
  ChatThreadRecord,
  ProjectState,
} from '@/features/project/types'
import { fingerprintThread } from '@/features/chat/chatHistorySync'
import { researchDeskReducer } from '@/features/researchDesk/state'

function thread(
  id: string,
  overrides: Partial<ChatThreadRecord> = {},
): ChatThreadRecord {
  return {
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    messages: [],
    preview: '',
    source: 'api',
    title: id,
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

function message(
  id: string,
  createdAt: string,
  overrides: Partial<ChatMessageRecord> = {},
): ChatMessageRecord {
  return { contentMarkdown: id, createdAt, id, role: 'user', ...overrides }
}

function withThread(local: ChatThreadRecord): ProjectState {
  const base = createEmptyProjectState()
  return {
    ...base,
    chatThreadOrder: [local.id],
    chatThreads: { [local.id]: local },
    dirty: false,
  }
}

describe('server chat hydration', () => {
  it('adds server threads with empty messages + membership, WITHOUT dirtying', () => {
    const next = researchDeskReducer(createEmptyProjectState(), {
      memberships: { ct_1: 'ctg_1' },
      threads: [thread('ct_1', { title: 'From server' })],
      type: 'upsertServerChatThreads',
    })
    expect(next.chatThreads.ct_1.title).toBe('From server')
    expect(next.chatThreads.ct_1.messages).toEqual([]) // loaded lazily on open
    expect(next.chatThreadGroupMemberships.ct_1).toBe('ctg_1')
    expect(next.chatThreadOrder).toContain('ct_1')
    expect(next.dirty).toBe(false) // server-pushed state never marks dirty
  })

  it('preserves the server keyset order within a hydrated page (no updatedAt re-sort)', () => {
    // Server pages by created_at desc; the batch arrives newest-first. updatedAt
    // is deliberately the REVERSE order -- if the reducer re-sorted by updatedAt
    // the list would flip, which is the cross-page inconsistency we forbid.
    const next = researchDeskReducer(createEmptyProjectState(), {
      memberships: { ct_new: null, ct_old: null },
      threads: [
        thread('ct_new', { createdAt: '2026-03-01T00:00:00.000Z', updatedAt: '2026-01-01T00:00:00.000Z' }),
        thread('ct_old', { createdAt: '2026-02-01T00:00:00.000Z', updatedAt: '2026-09-01T00:00:00.000Z' }),
      ],
      type: 'upsertServerChatThreads',
    })
    expect(next.chatThreadOrder).toEqual(['ct_new', 'ct_old'])
  })

  it('appends an older load-more page to the END, keeping the merged list newest-first', () => {
    const page1 = researchDeskReducer(createEmptyProjectState(), {
      memberships: { ct_a: null, ct_b: null },
      threads: [
        thread('ct_a', { createdAt: '2026-03-01T00:00:00.000Z' }),
        thread('ct_b', { createdAt: '2026-02-01T00:00:00.000Z' }),
      ],
      type: 'upsertServerChatThreads',
    })
    const page2 = researchDeskReducer(page1, {
      append: true,
      memberships: { ct_c: null },
      threads: [thread('ct_c', { createdAt: '2026-01-01T00:00:00.000Z' })],
      type: 'upsertServerChatThreads',
    })
    expect(page2.chatThreadOrder).toEqual(['ct_a', 'ct_b', 'ct_c'])
  })

  it('does NOT clobber a newer local thread with an older server version', () => {
    const local = thread('ct_1', {
      messages: [message('cm_local', '2026-02-01T00:00:00.000Z')],
      title: 'Local edit',
      updatedAt: '2026-02-01T00:00:00.000Z',
    })
    const next = researchDeskReducer(withThread(local), {
      memberships: { ct_1: null },
      threads: [thread('ct_1', { title: 'Stale server', updatedAt: '2026-01-01T00:00:00.000Z' })],
      type: 'upsertServerChatThreads',
    })
    expect(next.chatThreads.ct_1.title).toBe('Local edit') // local wins
    expect(next.chatThreads.ct_1.messages).toHaveLength(1) // messages kept
  })

  it('takes a strictly-newer server version for an existing thread', () => {
    const local = thread('ct_1', { title: 'Old local', updatedAt: '2026-01-01T00:00:00.000Z' })
    const next = researchDeskReducer(withThread(local), {
      memberships: { ct_1: null },
      threads: [thread('ct_1', { title: 'Newer server', updatedAt: '2026-03-01T00:00:00.000Z' })],
      type: 'upsertServerChatThreads',
    })
    expect(next.chatThreads.ct_1.title).toBe('Newer server')
  })

  it('fills a thread\'s messages on load-on-open WITHOUT dirtying', () => {
    const local = thread('ct_1')
    const next = researchDeskReducer(withThread(local), {
      messages: [
        message('cm_2', '2026-01-01T00:02:00.000Z'),
        message('cm_1', '2026-01-01T00:01:00.000Z'),
      ],
      threadId: 'ct_1',
      type: 'upsertServerChatMessages',
    })
    // Merged + ordered chronologically (oldest first).
    expect(next.chatThreads.ct_1.messages.map((m) => m.id)).toEqual(['cm_1', 'cm_2'])
    expect(next.dirty).toBe(false)
  })

  it('restores user-before-assistant order when persisted turn timestamps tie', () => {
    const local = thread('ct_1')
    const tiedAt = '2026-01-01T00:01:00.000Z'
    const next = researchDeskReducer(withThread(local), {
      messages: [
        message('cm_assistant', tiedAt, {
          contentMarkdown: 'Already answered',
          role: 'assistant',
        }),
        message('cm_user', tiedAt, { contentMarkdown: 'Question' }),
      ],
      threadId: 'ct_1',
      type: 'upsertServerChatMessages',
    })

    expect(next.chatThreads.ct_1.messages.map((item) => item.role)).toEqual([
      'user',
      'assistant',
    ])
    expect(next.dirty).toBe(false)
  })

  it('restores a tied turn split across server pages without duplicates', () => {
    const tiedAt = '2026-01-01T00:01:00.000Z'
    const assistantPage = researchDeskReducer(withThread(thread('ct_1')), {
      messages: [
        message('cm_assistant', tiedAt, {
          contentMarkdown: 'Already answered',
          role: 'assistant',
        }),
      ],
      threadId: 'ct_1',
      type: 'upsertServerChatMessages',
    })
    const merged = researchDeskReducer(assistantPage, {
      messages: [
        message('cm_user', tiedAt, { contentMarkdown: 'Question' }),
        message('cm_assistant', tiedAt, {
          contentMarkdown: 'Already answered',
          role: 'assistant',
        }),
      ],
      threadId: 'ct_1',
      type: 'upsertServerChatMessages',
    })

    expect(merged.chatThreads.ct_1.messages.map((item) => item.id)).toEqual([
      'cm_user',
      'cm_assistant',
    ])
    expect(merged.dirty).toBe(false)
  })

  it('merges server groups WITHOUT dirtying', () => {
    const next = researchDeskReducer(createEmptyProjectState(), {
      groups: [{ createdAt: '2026-01-01T00:00:00.000Z', id: 'ctg_1', title: 'G', updatedAt: '2026-01-01T00:00:00.000Z' }],
      type: 'upsertServerChatThreadGroups',
    })
    expect(next.chatThreadGroups.ctg_1.title).toBe('G')
    expect(next.chatThreadGroupOrder).toContain('ctg_1')
    expect(next.dirty).toBe(false)
  })

  it('setServerSyncEnabled flips the opt-in AND dirties (so it persists)', () => {
    const base = createEmptyProjectState()
    expect(base.serverSyncEnabled).toBe(false)
    const next = researchDeskReducer(base, { enabled: true, type: 'setServerSyncEnabled' })
    expect(next.serverSyncEnabled).toBe(true)
    expect(next.dirty).toBe(true)
  })
})

describe('chat message branching and retry', () => {
  it('replaces the selected assistant answer and removes later messages on retry', () => {
    const local = thread('ct_1', {
      messages: [
        message('cm_u1', '2026-01-01T00:01:00.000Z', { contentMarkdown: 'First prompt' }),
        message('cm_a1', '2026-01-01T00:02:00.000Z', {
          contentMarkdown: 'First answer',
          requestContext: { knowledgeCollectionIds: ['kc_1'] },
          role: 'assistant',
        }),
        message('cm_u2', '2026-01-01T00:03:00.000Z', { contentMarkdown: 'Follow-up' }),
        message('cm_a2', '2026-01-01T00:04:00.000Z', {
          contentMarkdown: 'Follow-up answer',
          role: 'assistant',
        }),
      ],
    })

    const next = researchDeskReducer(withThread(local), {
      assistantMessageId: 'cm_retry',
      createdAt: '2026-01-01T00:05:00.000Z',
      requestContext: { knowledgeCollectionIds: ['kc_1'] },
      replacedAssistantMessageId: 'cm_a1',
      threadId: 'ct_1',
      type: 'startChatAssistantRetry',
    })

    expect(next.chatThreads.ct_1.messages).toEqual([
      local.messages[0],
      {
        contentMarkdown: '',
        createdAt: '2026-01-01T00:05:00.000Z',
        id: 'cm_retry',
        requestContext: { knowledgeCollectionIds: ['kc_1'] },
        role: 'assistant',
      },
    ])
    expect(next.chatThreads.ct_1.updatedAt).toBe('2026-01-01T00:05:00.000Z')
    expect(next.dirty).toBe(true)
  })

  it('refuses retry for assistant greetings without a preceding user message', () => {
    const local = thread('ct_1', {
      messages: [
        message('cm_a0', '2026-01-01T00:01:00.000Z', {
          contentMarkdown: 'Hello',
          role: 'assistant',
        }),
      ],
    })
    const seeded = withThread(local)

    const next = researchDeskReducer(seeded, {
      assistantMessageId: 'cm_retry',
      createdAt: '2026-01-01T00:02:00.000Z',
      replacedAssistantMessageId: 'cm_a0',
      threadId: 'ct_1',
      type: 'startChatAssistantRetry',
    })

    expect(next).toBe(seeded)
  })

  it('keeps branch behavior scoped to messages through the selected turn', () => {
    const local = thread('ct_1', {
      messages: [
        message('cm_u1', '2026-01-01T00:01:00.000Z', { contentMarkdown: 'First prompt' }),
        message('cm_a1', '2026-01-01T00:02:00.000Z', {
          contentMarkdown: 'First answer',
          requestContext: { knowledgeCollectionIds: ['kc_branch'] },
          role: 'assistant',
        }),
        message('cm_u2', '2026-01-01T00:03:00.000Z', { contentMarkdown: 'Follow-up' }),
        message('cm_a2', '2026-01-01T00:04:00.000Z', {
          contentMarkdown: 'Follow-up answer',
          role: 'assistant',
        }),
      ],
    })

    const next = researchDeskReducer(withThread(local), {
      messageId: 'cm_a1',
      threadId: 'ct_1',
      type: 'branchChatThreadFromMessage',
    })
    const branchId = next.ui.selectedChatThreadId
    expect(branchId).not.toBe('ct_1')
    expect(branchId).not.toBeNull()
    const branch = next.chatThreads[branchId ?? '']

    expect(next.chatThreads.ct_1.messages).toHaveLength(4)
    expect(branch.messages.map((item) => item.contentMarkdown)).toEqual([
      'First prompt',
      'First answer',
    ])
    expect(branch.messages.map((item) => item.id)).not.toEqual(['cm_u1', 'cm_a1'])
    expect(branch.messages[1].requestContext).toEqual({ knowledgeCollectionIds: ['kc_branch'] })
  })
})

describe('confirmed thread deletion', () => {
  it('marks a thread as deleting without touching the autosave fingerprint', () => {
    const local = thread('ct_1')
    const next = researchDeskReducer(withThread(local), {
      deletion: { error: null, status: 'deleting' },
      threadId: 'ct_1',
      type: 'setChatThreadDeletion',
    })

    const marked = next.chatThreads.ct_1
    expect(marked.deletion).toEqual({ error: null, status: 'deleting' })
    // The row stays until the server confirms.
    expect(next.chatThreadOrder).toEqual(['ct_1'])
    // (groupId, updatedAt) IS the autosave fingerprint: a deletion marker
    // must never read as an edit that needs pushing.
    expect(fingerprintThread(marked, null)).toEqual(fingerprintThread(local, null))
  })

  it('clears the marker when a failed deletion is reset', () => {
    const marked = researchDeskReducer(withThread(thread('ct_1')), {
      deletion: { error: 'boom', status: 'delete_failed' },
      threadId: 'ct_1',
      type: 'setChatThreadDeletion',
    })
    const cleared = researchDeskReducer(marked, {
      deletion: null,
      threadId: 'ct_1',
      type: 'setChatThreadDeletion',
    })

    expect(cleared.chatThreads.ct_1.deletion).toBeUndefined()
    expect(Object.hasOwn(cleared.chatThreads.ct_1, 'deletion')).toBe(false)
  })

  it('refuses assistant content for a thread awaiting deletion', () => {
    const local = thread('ct_1', {
      messages: [message('cm_u1', '2026-01-01T00:00:00.000Z'), message('cm_a1', '2026-01-01T00:00:01.000Z', { role: 'assistant' })],
    })
    const marked = researchDeskReducer(withThread(local), {
      deletion: { error: null, status: 'deleting' },
      threadId: 'ct_1',
      type: 'setChatThreadDeletion',
    })

    // This is what the aborted generation dispatches on its way out; writing
    // it would bump updatedAt and make the next flush re-push a deleted row.
    const next = researchDeskReducer(marked, {
      assistantMessageId: 'cm_a1',
      contentMarkdown: 'Antwort abgebrochen.',
      threadId: 'ct_1',
      type: 'setChatAssistantMessageContent',
    })

    expect(next).toBe(marked)
    expect(next.chatThreads.ct_1.messages[1].contentMarkdown).toBe('cm_a1')
  })
})
