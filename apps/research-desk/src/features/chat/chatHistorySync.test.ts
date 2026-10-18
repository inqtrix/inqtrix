import { describe, expect, it } from 'vitest'

import type {
  ServerChatMessage,
  ServerChatThread,
  ServerChatThreadGroup,
} from '@/api/inqtrixClient'
import type { ChatMessageRecord, ChatThreadRecord } from '@/features/project/types'
import {
  fingerprintThread,
  groupRecordFromServer,
  messageIdsToDelete,
  messageRecordFromServer,
  serverGroupPayload,
  serverMessagePayload,
  serverThreadPayload,
  threadModelSelectionFromWire,
  pushThreadMessages,
  shouldFetchMessageBaselineBeforePush,
  shouldLoadServerChatMessages,
  syncChatThreads,
  threadNeedsSync,
  threadRecordFromServer,
} from './chatHistorySync'

describe('chatHistorySync converters', () => {
  it('round-trips a thread record and its group membership', () => {
    const server: ServerChatThread = {
      created_at: 1_700_000_000,
      group_id: 'ctg_1',
      id: 'ct_1',
      preview: 'last line',
      source: 'imported',
      title: 'A thread',
      updated_at: 1_700_000_500,
    }
    const { groupId, record } = threadRecordFromServer(server)
    expect(groupId).toBe('ctg_1')
    expect(record.messages).toEqual([]) // messages load lazily on open
    expect(record.source).toBe('imported')
    expect(record.createdAt).toBe(new Date(1_700_000_000 * 1000).toISOString())

    const payload = serverThreadPayload(record, groupId)
    expect(payload.created_at).toBe(server.created_at)
    expect(payload.updated_at).toBe(server.updated_at)
    expect(payload.group_id).toBe('ctg_1')
    expect(payload.source).toBe('imported')
  })

  it('normalizes an unknown thread source instead of trusting it blindly', () => {
    const { record } = threadRecordFromServer({
      created_at: 1,
      group_id: null,
      id: 'ct_x',
      preview: '',
      source: 'totally-bogus',
      title: 'T',
      updated_at: 1,
    })
    expect(record.source).toBe('api')
  })

  it('round-trips a message with attachments/chainTrace/modelResolution/requestContext verbatim', () => {
    const record: ChatMessageRecord = {
      attachments: [
        {
          attachedAt: '2026-01-01T00:00:00.000Z',
          contentMarkdown: '# Report',
          kind: 'research-report',
          runId: 'run_1',
          title: 'My report',
        },
      ],
      chainTrace: [{ label: 'step', output: 'ok', status: 'ok' }],
      contentMarkdown: 'hello',
      createdAt: '2026-06-01T12:00:00.000Z',
      id: 'cm_1',
      modelResolution: {
        effort: 'high',
        effortSource: 'override',
        model: 'claude',
        modelSource: 'default',
        requestedTier: 'deep',
        tier: 'deep',
      },
      requestContext: { knowledgeCollectionIds: ['kc_1', 'kc_2'] },
      role: 'assistant',
    }
    const payload = serverMessagePayload(record)
    expect(payload.metadata.attachments).toEqual(record.attachments)
    expect(payload.metadata.chainTrace).toEqual(record.chainTrace)
    expect(payload.metadata.modelResolution).toEqual(record.modelResolution)
    expect(payload.metadata.requestContext).toEqual(record.requestContext)

    const wire: ServerChatMessage = {
      content_markdown: payload.content_markdown,
      created_at: payload.created_at,
      id: payload.id,
      metadata: payload.metadata,
      role: payload.role,
      thread_id: 'ct_1',
    }
    expect(messageRecordFromServer(wire)).toEqual(record)
  })

  it('omits absent optional message fields rather than emitting empty keys', () => {
    const wire: ServerChatMessage = {
      content_markdown: 'plain',
      created_at: 10,
      id: 'cm_2',
      metadata: {},
      role: 'user',
      thread_id: 'ct_1',
    }
    const record = messageRecordFromServer(wire)
    expect('attachments' in record).toBe(false)
    expect('chainTrace' in record).toBe(false)
    expect('modelResolution' in record).toBe(false)
    expect('requestContext' in record).toBe(false)
    // And a bare message packs an empty metadata object (no junk keys).
    expect(serverMessagePayload(record).metadata).toEqual({})
  })

  it('ignores malformed request context metadata from the server', () => {
    const record = messageRecordFromServer({
      content_markdown: 'plain',
      created_at: 10,
      id: 'cm_malformed',
      metadata: {
        requestContext: { knowledgeCollectionIds: [123, '', 'kc_1'] },
      },
      role: 'assistant',
      thread_id: 'ct_1',
    })

    expect(record.requestContext).toEqual({ knowledgeCollectionIds: ['kc_1'] })
  })

  it('round-trips a group record', () => {
    const server: ServerChatThreadGroup = {
      created_at: 100,
      id: 'ctg_1',
      title: 'Group',
      updated_at: 200,
    }
    const record = groupRecordFromServer(server)
    const payload = serverGroupPayload(record)
    expect(payload.created_at).toBe(100)
    expect(payload.updated_at).toBe(200)
    expect(payload.title).toBe('Group')
  })
})

describe('lazy message hydration guard', () => {
  const emptyThread = { messages: [] }
  const populatedThread = {
    messages: [{
      contentMarkdown: 'Question',
      createdAt: '2026-01-01T00:00:00.000Z',
      id: 'cm_1',
      role: 'user' as const,
    }],
  }

  it('does not read a newly-created local empty thread before its first PUT', () => {
    expect(shouldLoadServerChatMessages(emptyThread, false, false)).toBe(false)
  })

  it('loads an empty hydrated server thread exactly until it resolves', () => {
    expect(shouldLoadServerChatMessages(emptyThread, true, false)).toBe(true)
    expect(shouldLoadServerChatMessages(emptyThread, true, true)).toBe(false)
  })

  it('does not fetch when local messages or no thread already own the state', () => {
    expect(shouldLoadServerChatMessages(populatedThread, true, false)).toBe(false)
    expect(shouldLoadServerChatMessages(undefined, true, false)).toBe(false)
  })
})

describe('thread fingerprint diff', () => {
  const base = {
    createdAt: '2026-01-01T00:00:00.000Z',
    id: 'ct_1',
    messages: [],
    preview: '',
    source: 'api' as const,
    title: 'T',
    updatedAt: '2026-01-01T00:00:00.000Z',
  }

  it('reports a thread as needing sync when never synced', () => {
    expect(threadNeedsSync(undefined, fingerprintThread(base, null))).toBe(true)
  })

  it('detects an updatedAt change (content/title/message edit)', () => {
    const previous = fingerprintThread(base, null)
    const next = fingerprintThread(
      { ...base, updatedAt: '2026-01-02T00:00:00.000Z' },
      null,
    )
    expect(threadNeedsSync(previous, next)).toBe(true)
  })

  it('detects a group membership change without an updatedAt bump', () => {
    const previous = fingerprintThread(base, null)
    const next = fingerprintThread(base, 'ctg_1')
    expect(threadNeedsSync(previous, next)).toBe(true)
  })

  it('is stable for an unchanged thread (no spurious re-push)', () => {
    const previous = fingerprintThread(base, 'ctg_1')
    const next = fingerprintThread(base, 'ctg_1')
    expect(threadNeedsSync(previous, next)).toBe(false)
  })

  it('flags a local-newer thread for push when seeded with the server fingerprint', () => {
    // Hydration seeds synced state with WHAT THE SERVER HOLDS (the server
    // record). If the local copy is newer, the diff must fire so the newer
    // local thread is pushed UP — seeding with the LOCAL value (the old bug)
    // made this return false and silently stranded the local edit.
    const serverSeed = fingerprintThread(
      { ...base, updatedAt: '2026-01-01T00:00:00.000Z' },
      null,
    )
    const localNewer = fingerprintThread(
      { ...base, updatedAt: '2026-05-01T00:00:00.000Z' },
      null,
    )
    expect(threadNeedsSync(serverSeed, localNewer)).toBe(true)
  })
})

describe('message delete diff', () => {
  const message = (id: string): ChatMessageRecord => ({
    contentMarkdown: id,
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    role: 'user',
  })

  it('names the messages the server still holds but the thread dropped', () => {
    const known = new Set(['cm_0', 'cm_1', 'cm_2'])
    // cm_1 was deleted locally; the other two remain.
    expect(messageIdsToDelete(known, [message('cm_0'), message('cm_2')])).toEqual([
      'cm_1',
    ])
  })

  it('returns nothing when an unknown baseline means the server set is unfetched', () => {
    // An un-opened thread: deleting against an unknown baseline could drop
    // messages the client simply never loaded, so the diff must be empty.
    expect(messageIdsToDelete(undefined, [message('cm_0')])).toEqual([])
  })

  it('returns nothing when no message left the thread (no spurious delete)', () => {
    const known = new Set(['cm_0', 'cm_1'])
    expect(messageIdsToDelete(known, [message('cm_0'), message('cm_1')])).toEqual(
      [],
    )
  })

  it('deletes every message when the conversation was cleared', () => {
    const known = new Set(['cm_0', 'cm_1'])
    expect(messageIdsToDelete(known, []).sort()).toEqual(['cm_0', 'cm_1'])
  })

  it('requires a server baseline fetch before pushing local messages with an unknown baseline', () => {
    expect(shouldFetchMessageBaselineBeforePush(undefined, [message('cm_retry')])).toBe(true)
    expect(shouldFetchMessageBaselineBeforePush(undefined, [])).toBe(false)
    expect(shouldFetchMessageBaselineBeforePush(new Set(['cm_old']), [message('cm_retry')])).toBe(false)
  })
})

describe('chat message push against the snapshot of its pass', () => {
  const message = (id: string): ChatMessageRecord => ({
    contentMarkdown: id,
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    role: 'user',
  })
  const thread = (id: string, messages: ChatMessageRecord[]): ChatThreadRecord => ({
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    messages,
    preview: '',
    source: 'api',
    title: id,
    updatedAt: '2026-01-02T00:00:00.000Z',
  })
  const pushOf = (knownAtSnapshot: ReadonlySet<string> | undefined, messages: ChatMessageRecord[]) => {
    const deleted: string[] = []
    const appended: string[] = []
    const fetched: string[] = []
    const result = pushThreadMessages({
      appendMessages: async (pushed) => { appended.push(...pushed.map((entry) => entry.id)) },
      deleteMessage: async (id) => { deleted.push(id) },
      fetchBaseline: async () => {
        fetched.push('fetch')
        return new Set(['cm_old_1', 'cm_old_2'])
      },
      knownAtSnapshot,
      messages,
    })
    return { appended, deleted, fetched, result }
  }

  it('leaves an unopened thread without local messages to the load that seeds its baseline', async () => {
    const push = pushOf(undefined, [])
    expect(await push.result).toBeUndefined()
    expect(push.deleted).toEqual([])
    expect(push.appended).toEqual([])
    expect(push.fetched).toEqual([])
  })

  it('deletes only what the server held when the pass started and the snapshot dropped', async () => {
    const push = pushOf(new Set(['cm_0', 'cm_old']), [message('cm_0'), message('cm_new')])
    expect([...(await push.result) ?? []]).toEqual(['cm_0', 'cm_new'])
    expect(push.deleted).toEqual(['cm_old'])
    expect(push.appended).toEqual(['cm_0', 'cm_new'])
    expect(push.fetched).toEqual([])
  })

  it('learns the server set first when local messages meet an unknown baseline', async () => {
    const push = pushOf(undefined, [message('cm_old_1'), message('cm_retry')])
    expect([...(await push.result) ?? []]).toEqual(['cm_old_1', 'cm_retry'])
    expect(push.fetched).toEqual(['fetch'])
    expect(push.deleted).toEqual(['cm_old_2'])
  })

  it('hands each thread the message baseline its pass started with, not one a load seeded meanwhile', async () => {
    const syncedMessages = new Map<string, ReadonlySet<string>>([['ct_open', new Set(['cm_a'])]])
    const handed = new Map<string, ReadonlySet<string> | undefined>()
    await syncChatThreads({
      deleteThread: async () => undefined,
      fingerprintOf: (record) => fingerprintThread(record, null),
      pushThread: async (record, knownMessageIds) => {
        handed.set(record.id, knownMessageIds)
        // A load-on-open of the unopened thread lands while this push waits.
        syncedMessages.set('ct_unopened', new Set(['cm_server_1', 'cm_server_2']))
        syncedMessages.set('ct_open', new Set(['cm_a', 'cm_b']))
      },
      syncedMessages,
      syncedThreads: new Map(),
      threads: {
        ct_open: thread('ct_open', [message('cm_a')]),
        ct_unopened: thread('ct_unopened', []),
      },
    })
    expect([...handed.keys()]).toEqual(['ct_open', 'ct_unopened'])
    expect([...handed.get('ct_open') ?? []]).toEqual(['cm_a'])
    expect(handed.get('ct_unopened')).toBeUndefined()
  })
})

describe('chat thread model stickiness (wire)', () => {
  const record: ChatThreadRecord = {
    createdAt: '2026-08-07T10:00:00.000Z',
    id: 'ct_1',
    messages: [],
    preview: '',
    source: 'api',
    title: 'T',
    updatedAt: '2026-08-07T10:00:00.000Z',
  }

  it('rides every save as a whole-row field', () => {
    // The endpoint knows no PATCH: an omitted field is reset to '' by the
    // next unrelated title/preview save.
    const withPick = serverThreadPayload(
      { ...record, modelSelection: { model: 'gpt-5.4-nano', tier: null, effort: null } },
      null,
    )
    expect(JSON.parse(withPick.model_selection)).toEqual({
      model: 'gpt-5.4-nano',
      tier: null,
      effort: null,
    })
    expect(serverThreadPayload({ ...record }, null).model_selection).toBe('')
  })

  it('round-trips through the server record', () => {
    const { record: parsed } = threadRecordFromServer({
      id: 'ct_1',
      title: 'T',
      preview: '',
      source: 'api',
      group_id: null,
      created_at: 1_700_000_000,
      updated_at: 1_700_000_000,
      model_selection: '{"model":null,"tier":"fast","effort":null}',
    })
    expect(parsed.modelSelection).toEqual({ model: null, tier: 'fast', effort: null })
  })

  it('treats absent, empty and garbage as no pick', () => {
    expect(threadModelSelectionFromWire(undefined)).toBeNull()
    expect(threadModelSelectionFromWire('')).toBeNull()
    expect(threadModelSelectionFromWire('{broken')).toBeNull()
    // An unknown tier from a future build must not be pinned.
    expect(threadModelSelectionFromWire('{"tier":"turbo"}')).toBeNull()
  })
})
