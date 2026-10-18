import { describe, expect, it } from 'vitest'

import { createEmptyProjectState } from '@/features/project/seedProject'
import { researchDeskReducer } from '@/features/researchDesk/state'
import { initialSessionTitle, reconcileTitle, sessionTitleHint, shouldRequestAgentTitle, titleWire, type TitleKind } from './model'

function prepared(kind: TitleKind) {
  let state = createEmptyProjectState()
  const common = { id: 'conversation', title: 'Initial question', titleRevision: 0,
    titleSource: 'initial' as const, titleServerReady: true,
    createdAt: '2026-01-01T00:00:00.000Z', updatedAt: '2026-01-01T00:00:00.000Z' }
  if (kind === 'chat') state = { ...state, chatThreads: { conversation: { ...common, source: 'api', preview: '', messages: [] } } }
  else state = { ...state, agentSessions: { conversation: { ...common, runIds: [], groupId: null,
    sourcePolicy: { web: 'available', knowledge: 'available' } } } }
  return researchDeskReducer(state, { type: 'requestSessionTitle', kind, id: 'conversation', question: 'Initial question' })
}

function row(state: ReturnType<typeof prepared>, kind: TitleKind) {
  return kind === 'chat' ? state.chatThreads.conversation : state.agentSessions.conversation
}

describe.each<TitleKind>(['chat', 'agent'])('conversation title contract: %s', (kind) => {
  it('makes a late local title saveable without changing activity or dirtying a skipped title', () => {
    const before = { ...prepared(kind), dirty: false }
    const requestId = row(before, kind).titleRequest!.id
    const completed = researchDeskReducer(before, { type: 'finishSessionTitle', kind, id: 'conversation',
      requestId, projectEpoch: before.projectEpoch, title: 'Short topic' })
    expect(completed.dirty).toBe(true)
    expect(row(completed, kind).updatedAt).toBe(row(before, kind).updatedAt)
    const skipped = researchDeskReducer(before, { type: 'finishSessionTitle', kind, id: 'conversation',
      requestId, projectEpoch: before.projectEpoch, title: null, reason: 'timeout' })
    expect(skipped.dirty).toBe(false)
  })

  it('keeps a provisional title immediately and changes only the label on completion', () => {
    const before = prepared(kind)
    const pending = row(before, kind)
    expect(pending.title).toBe('Initial question')
    expect(pending.titleRequest?.question).toBe('Initial question')
    const after = researchDeskReducer(before, { type: 'finishSessionTitle', kind, id: 'conversation',
      requestId: pending.titleRequest!.id, projectEpoch: before.projectEpoch, title: 'Short topic',
      current: { title: 'Short topic', title_revision: 1, title_source: 'generated' } })
    expect(row(after, kind).title).toBe('Short topic')
    expect(row(after, kind).updatedAt).toBe(pending.updatedAt)
    expect(row(after, kind).titleRequest).toBeUndefined()
  })

  it('rejects a model completion after a manual rename, including the identical provisional text', () => {
    for (const title of ['My project', 'Initial question']) {
      let state = prepared(kind)
      const requestId = row(state, kind).titleRequest!.id
      state = researchDeskReducer(state, kind === 'chat'
        ? { type: 'renameChatThread', threadId: 'conversation', title }
        : { type: 'renameAgentSession', sessionId: 'conversation', title })
      const after = researchDeskReducer(state, { type: 'finishSessionTitle', kind, id: 'conversation',
        requestId, projectEpoch: state.projectEpoch, title: 'Model topic',
        current: { title: 'Model topic', title_revision: 1, title_source: 'generated' } })
      expect(row(after, kind).title).toBe(title)
      expect(row(after, kind).titleSource).toBe('manual')
    }
  })

  it('does not apply a prior project response or recreate a removed conversation', () => {
    const state = prepared(kind)
    const action = { type: 'finishSessionTitle' as const, kind, id: 'conversation',
      requestId: row(state, kind).titleRequest!.id, projectEpoch: state.projectEpoch, title: 'Old result' }
    expect(row(researchDeskReducer({ ...state, projectEpoch: state.projectEpoch + 1 }, action), kind).title)
      .toBe(row(state, kind).title)
    const empty = createEmptyProjectState()
    expect(researchDeskReducer(empty, action)).toBe(empty)
  })

  it('preserves the initial title and records an unavailable generation without a retry intent', () => {
    const state = prepared(kind)
    const after = researchDeskReducer(state, { type: 'finishSessionTitle', kind, id: 'conversation',
      requestId: row(state, kind).titleRequest!.id, projectEpoch: state.projectEpoch,
      title: null, reason: 'no_fast_model' })
    expect(row(after, kind).title).toBe('Initial question')
    expect(row(after, kind).titleGenerationReason).toBe('no_fast_model')
    expect(row(after, kind).titleRequest).toBeUndefined()
  })
})

it('does not acknowledge a newer local rename with an older in-flight save receipt', () => {
  const local = { title: 'Newest edit', titleRevision: 0, titleSource: 'manual' as const, titlePending: true, titleEditId: 'new' }
  const old = { ...local, title: 'Earlier edit', titleEditId: 'old' }
  const merged = reconcileTitle(local, { title: 'Earlier edit', title_revision: 1, title_source: 'manual' }, old)
  expect(merged.title).toBe('Newest edit')
  expect(merged.titlePending).toBe(true)
})

it('accepts the matching save receipt and keeps a conflicting attempted name available', () => {
  const local = { title: 'My pending choice', titleRevision: 0, titleSource: 'manual' as const, titlePending: true, titleEditId: 'edit' }
  const accepted = reconcileTitle(local, { title: local.title, title_revision: 2, title_source: 'manual' }, local)
  expect(accepted).toMatchObject({ title: local.title, titleRevision: 2, titlePending: false })
  const conflict = reconcileTitle(local, { title: 'Other manual choice', title_revision: 2, title_source: 'manual' }, local)
  expect(conflict).toMatchObject({ title: 'Other manual choice', titleConflict: local.title, titlePending: false })
  expect(reconcileTitle(conflict, titleWire(conflict), conflict).titleConflict).toBe(local.title)
})

it('uses a newer title revision even when the server activity timestamp did not change', () => {
  let state = prepared('chat')
  const current = state.chatThreads.conversation
  state = researchDeskReducer(state, { type: 'upsertServerChatThreads', memberships: {},
    threads: [{ ...current, title: 'Remote manual title', titleRevision: 2, titleSource: 'manual' }] })
  expect(state.chatThreads.conversation.title).toBe('Remote manual title')
  expect(state.chatThreads.conversation.updatedAt).toBe(current.updatedAt)
})

it('retains the established provisional limits for both desks', () => {
  expect(initialSessionTitle('chat', 'a'.repeat(100))).toHaveLength(72)
  expect(initialSessionTitle('agent', 'a'.repeat(100))).toHaveLength(80)
  expect(initialSessionTitle('chat', '  First\nquestion  ')).toBe('First question')
})

it('requests an agent title only for a new, empty or recipient-derived session', () => {
  expect(shouldRequestAgentTitle(undefined)).toBe(true)
  expect(shouldRequestAgentTitle({ runIds: [] })).toBe(true)
  expect(shouldRequestAgentTitle({ runIds: ['shared'], persistable: false })).toBe(true)
  expect(shouldRequestAgentTitle({ runIds: ['owned'], persistable: true })).toBe(false)
})

describe('agent session title guards', () => {
  // A clarification answer or follow-up must not replace a generated,
  // manual or legacy title, nor a first title request that is still
  // pending; a completion lands only on the request it answers. Whether a
  // later question may replace a provisional 'initial' title is
  // deliberately not covered here.
  function withAgentSessions(
    sessions: Record<string, { title: string; titleSource?: 'initial' | 'generated' | 'manual' }>,
  ): ReturnType<typeof createEmptyProjectState> {
    const state = createEmptyProjectState()
    const agentSessions = Object.fromEntries(Object.entries(sessions).map(([id, fields]) => [id, {
      id, ...fields, titleRevision: 1, titleServerReady: true, runIds: [], groupId: null,
      sourcePolicy: { web: 'available' as const, knowledge: 'available' as const },
      createdAt: '2026-01-01T00:00:00.000Z', updatedAt: '2026-01-01T00:00:00.000Z',
    }]))
    return { ...state, agentSessions }
  }

  it.each(['generated', 'manual', undefined] as const)('a later submit never retitles a session whose title source is %s', (titleSource) => {
    // runIds [] lets the request past the submit's own check (an owned
    // session reaches the title request only that way, e.g. while its runs
    // are not loaded yet), so only the reducer's source guard is under test.
    const before = withAgentSessions({ s: { title: 'Battery storage twin', titleSource } })
    const after = researchDeskReducer(before, { type: 'requestSessionTitle', kind: 'agent', id: 's', question: 'Yes, only Germany' })
    expect(after).toBe(before)
  })

  it('a later submit keeps the first title request and title while that request is pending', () => {
    const before = prepared('agent')
    const pending = row(before, 'agent')
    const after = researchDeskReducer(before, { type: 'requestSessionTitle', kind: 'agent', id: 'conversation', question: 'Clarification answer' })
    expect(row(after, 'agent').title).toBe('Initial question')
    expect(row(after, 'agent').titleRequest).toEqual(pending.titleRequest)
  })

  it('ignores a late completion whose request is no longer pending', () => {
    let state = prepared('agent')
    const requestId = row(state, 'agent').titleRequest!.id
    state = researchDeskReducer(state, { type: 'finishSessionTitle', kind: 'agent', id: 'conversation',
      requestId, projectEpoch: state.projectEpoch, title: null, reason: 'timeout' })
    const after = researchDeskReducer(state, { type: 'finishSessionTitle', kind: 'agent', id: 'conversation',
      requestId, projectEpoch: state.projectEpoch, title: 'Late topic' })
    expect(after).toBe(state)
    expect(row(after, 'agent').title).toBe('Initial question')
  })

  it("never applies one session's completion to another session", () => {
    let state = withAgentSessions({
      a: { title: 'Question A', titleSource: 'initial' },
      b: { title: 'Question B', titleSource: 'initial' },
      c: { title: 'Question C', titleSource: 'initial' },
    })
    state = researchDeskReducer(state, { type: 'requestSessionTitle', kind: 'agent', id: 'a', question: 'Question A' })
    state = researchDeskReducer(state, { type: 'requestSessionTitle', kind: 'agent', id: 'b', question: 'Question B' })
    const requestOfA = state.agentSessions.a.titleRequest!.id
    // b has a pending request of its own, c has none.
    for (const id of ['b', 'c']) {
      const after = researchDeskReducer(state, { type: 'finishSessionTitle', kind: 'agent', id,
        requestId: requestOfA, projectEpoch: state.projectEpoch, title: 'Wrong topic' })
      expect(after).toBe(state)
    }
  })
})

it('keeps a chat title when a follow-up arrives after the title was settled', () => {
  // A follow-up in a chat must not replace the title, whether the model
  // generated one or the provisional title stayed because none was made.
  for (const completion of [{ title: 'Short topic' }, { title: null, reason: 'no_fast_model' }] as const) {
    let state = createEmptyProjectState()
    const first = { type: 'startChatExchange' as const, threadId: 'chat-one', userMessageId: 'user-one',
      assistantMessageId: 'assistant-one', contentMarkdown: 'How does a battery work?', createdAt: '2026-01-01T00:00:00.000Z' }
    state = researchDeskReducer(state, first)
    state = researchDeskReducer(state, { type: 'finishSessionTitle', kind: 'chat', id: 'chat-one',
      requestId: state.chatThreads['chat-one'].titleRequest!.id, projectEpoch: state.projectEpoch, ...completion })
    const settled = state.chatThreads['chat-one'].title
    state = researchDeskReducer(state, { ...first, userMessageId: 'user-two', assistantMessageId: 'assistant-two',
      contentMarkdown: 'Ja, nur Deutschland', createdAt: '2026-01-01T00:00:01.000Z' })
    expect(state.chatThreads['chat-one'].title).toBe(settled)
    expect(state.chatThreads['chat-one'].titleRequest).toBeUndefined()
  }
})

it('explains an unavailable automatic title without labeling a manual name as failed', () => {
  const copy = { pending: 'Pending title', unavailable: 'Provisional title retained', conflict: (value: string) => `Not saved: ${value}` }
  expect(sessionTitleHint({ title: 'Initial', titleSource: 'initial', titleGenerationReason: 'no_fast_model' }, copy, 'Rename'))
    .toContain('Provisional title retained')
  expect(sessionTitleHint({ title: 'My name', titleSource: 'manual', titleGenerationReason: 'no_fast_model' }, copy, 'Rename'))
    .not.toContain('Provisional title retained')
})

it('creates one title intent from the first saved chat message and captures its context', () => {
  let state = createEmptyProjectState()
  const submit = { type: 'startChatExchange' as const, threadId: 'chat-one', userMessageId: 'user-one',
    assistantMessageId: 'assistant-one', contentMarkdown: 'How does a battery work?',
    createdAt: '2026-01-01T00:00:00.000Z', titleLanguage: 'en', titleStack: 'original-stack' }
  state = researchDeskReducer(state, submit)
  const request = state.chatThreads['chat-one'].titleRequest
  expect(request).toMatchObject({ question: submit.contentMarkdown, language: 'en', stack: 'original-stack' })
  state = researchDeskReducer(state, { ...submit, userMessageId: 'user-two', assistantMessageId: 'assistant-two', contentMarkdown: 'A follow-up' })
  expect(state.chatThreads['chat-one'].titleRequest).toEqual(request)
  expect(state.chatThreads['chat-one'].title).toBe('How does a battery work?')
})

it('preserves a manually named empty chat when its first question arrives', () => {
  let state = prepared('chat')
  state = researchDeskReducer(state, { type: 'renameChatThread', threadId: 'conversation', title: 'My project' })
  state = researchDeskReducer(state, { type: 'startChatExchange', threadId: 'conversation', userMessageId: 'u', assistantMessageId: 'a',
    contentMarkdown: 'First real question', createdAt: '2026-01-01T00:00:01.000Z' })
  expect(state.chatThreads.conversation.title).toBe('My project')
  expect(state.chatThreads.conversation.titleRequest).toBeUndefined()
})

it('restores an unresolved rename as a visible conflict without submitting or changing the server title', () => {
  const state = prepared('chat')
  const after = researchDeskReducer(state, { type: 'restoreSessionTitleDraft', kind: 'chat', id: 'conversation',
    title: 'My unsaved choice', projectEpoch: state.projectEpoch })
  expect(after.chatThreads.conversation.title).toBe('Initial question')
  expect(after.chatThreads.conversation.titleConflict).toBe('My unsaved choice')
  expect(after.chatThreads.conversation.titlePending).not.toBe(true)
})
