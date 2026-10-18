import { describe, expect, it } from 'vitest'
import type {
  ResearchRunEvent,
  ResearchRunResult,
  ResearchRunSummary,
} from '@/features/researchRuns/types'
import { DEFAULT_PANEL_LAYOUT } from '@/features/project/panelLayout'
import { createEmptyProjectState } from '@/features/project/seedProject'
import type {
  ChatRuleRecord,
  ChatThreadRecord,
  EditorDocumentRecord,
  EditorFolderRecord,
  FileAssetRecord,
  KnowledgeSessionGroupRecord,
  KnowledgeSessionRecord,
  KnowledgeThreadItemRecord,
  ProjectState,
  ResearchRunRecord,
} from '@/features/project/types'
import { applyRunEvent, fromRunSummary, mergeRunSummary } from '@/features/project/types'
import {
  agentSessionFingerprint,
  persistableAgentSessionsInOrder,
  pushAgentSessions,
  serverAgentSessionFingerprint,
  serverAgentSessionPayload,
} from '@/features/agent/agentSessionSync'
import { DERIVED_AGENT_SESSION_UPDATED_AT } from '@/features/agent/model'
import type { AutoResumePlan } from '@/features/fileLibrary/indexingTypes'
import { researchDeskReducer, visibleResearchJobs } from './state'
import { projectResearchJobs } from '@/features/project/selectors'

function makeRunEvent(overrides: Partial<ResearchRunEvent> = {}): ResearchRunEvent {
  return {
    created_at: Date.parse('2026-01-01T00:00:05.000Z') / 1000,
    data: {},
    run_id: 'run-1',
    sequence: 1,
    type: 'inqtrix.run.started',
    ...overrides,
  }
}

describe('applyRunEvent startedAt', () => {
  it('derives startedAt from the running-transition event when the summary had none', () => {
    // A fresh run is created `queued` with started_at null; the running event
    // carries no started_at, so without deriving it the live timer sticks at
    // 00:00:00 (status flips to running while startedAt stays undefined).
    const queued = fromRunSummary(makeRunSummary(), 'test-stack')
    expect(queued.startedAt).toBeUndefined()

    const next = applyRunEvent(queued, makeRunEvent())
    expect(next.status).toBe('running')
    expect(next.startedAt).toBe('2026-01-01T00:00:05.000Z')
  })

  it('does not overwrite an already-set startedAt on later running events', () => {
    const running = applyRunEvent(fromRunSummary(makeRunSummary(), 'test-stack'), makeRunEvent())
    const later = applyRunEvent(
      running,
      makeRunEvent({
        created_at: Date.parse('2026-01-01T00:00:42.000Z') / 1000,
        data: { status: 'running' },
        sequence: 2,
        type: 'inqtrix.progress',
      }),
    )
    expect(later.startedAt).toBe('2026-01-01T00:00:05.000Z')
  })

  it('preserves an authoritative summary startedAt from a hydrated running run', () => {
    // listResearchRuns already carries started_at for in-flight runs — an event
    // must never clobber that with a (later) event timestamp.
    const hydrated = fromRunSummary(
      makeRunSummary({ started_at: Date.parse('2026-01-01T00:00:01.000Z') / 1000, status: 'running' }),
      'test-stack',
    )
    expect(hydrated.startedAt).toBe('2026-01-01T00:00:01.000Z')
    const next = applyRunEvent(hydrated, makeRunEvent({ created_at: Date.parse('2026-01-01T00:00:09.000Z') / 1000 }))
    expect(next.startedAt).toBe('2026-01-01T00:00:01.000Z')
  })
})

describe('applyRunEvent lifecycle status', () => {
  it('does not treat a completed research query as a completed run', () => {
    const running = applyRunEvent(
      fromRunSummary(makeRunSummary(), 'test-stack'),
      makeRunEvent(),
    )

    const next = applyRunEvent(
      running,
      makeRunEvent({
        created_at: Date.parse('2026-01-01T00:00:12.000Z') / 1000,
        data: { query: 'Primärquellen', status: 'completed' },
        sequence: 2,
        type: 'inqtrix.research.query.finished',
      }),
    )

    expect(next.status).toBe('running')
    expect(next.finishedAt).toBeUndefined()
  })

  it('accepts status from the authoritative run snapshot event', () => {
    const running = applyRunEvent(
      fromRunSummary(makeRunSummary(), 'test-stack'),
      makeRunEvent(),
    )

    const next = applyRunEvent(
      running,
      makeRunEvent({
        created_at: Date.parse('2026-01-01T00:00:20.000Z') / 1000,
        data: { snapshot: { done: true }, status: 'completed' },
        sequence: 3,
        type: 'inqtrix.run.snapshot',
      }),
    )

    expect(next.status).toBe('completed')
    expect(next.finishedAt).toBe('2026-01-01T00:00:20.000Z')
  })
})

function makeAsset(id: string, label: string, overrides: Partial<FileAssetRecord> = {}): FileAssetRecord {
  return {
    createdAt: '2026-01-01T00:00:00.000Z',
    extractedText: `${label} content`,
    fileName: `${label}.txt`,
    groupId: null,
    id,
    label,
    mimeType: 'text/plain',
    origin: 'library',
    pageCount: null,
    parseStatus: 'parsed',
    parseWarning: null,
    sectionId: 'file-section-temp',
    sizeBytes: 12,
    textTruncated: false,
    title: `${label}.txt`,
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

function makeRule(id: string, label: string, overrides: Partial<ChatRuleRecord> = {}): ChatRuleRecord {
  return {
    contentMarkdown: `${label} prompt`,
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    label,
    title: `${label} prompt`,
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

function makeKnowledgeSession(
  id: string,
  title: string,
  overrides: Partial<KnowledgeSessionRecord> = {},
): KnowledgeSessionRecord {
  return {
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    title,
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

function makeKnowledgeGroup(
  id: string,
  title: string,
  overrides: Partial<KnowledgeSessionGroupRecord> = {},
): KnowledgeSessionGroupRecord {
  return {
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    title,
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

function makeKnowledgeItem(
  id: string,
  sessionId: string,
  overrides: Partial<KnowledgeThreadItemRecord> = {},
): KnowledgeThreadItemRecord {
  return {
    collectionTitles: ['EU Recht'],
    createdAt: '2026-01-02T00:00:00.000Z',
    id,
    progress: { steps: [] },
    question: 'Welche Pflichten gelten?',
    requestedProfile: null,
    runId: `run-${id}`,
    sessionId,
    status: 'running',
    ...overrides,
  }
}

const storedAnswer = {
  answerMarkdown: 'Antwort.',
  degradedStages: [],
  quotes: [],
  references: [],
  refusal: false,
  retrievalDegradations: [],
}

const storedResult = {
  answer: 'Antwort.',
  run_id: 'run-ki-1',
  status: 'completed',
  top_claims: [],
  top_sources: [],
} as unknown as ResearchRunResult

function makeRunSummary(overrides: Partial<ResearchRunSummary> = {}): ResearchRunSummary {
  return {
    access: { mode: 'owner' },
    agent_overrides: {},
    created_at: Date.parse('2026-01-01T00:00:00.000Z') / 1000,
    elapsed_seconds: null,
    error: null,
    events_url: '/v1/runs/run-1/events',
    finished_at: null,
    mode: 'research',
    question: 'Research question',
    queue_position: null,
    result_url: '/v1/runs/run-1/result',
    run_id: 'run-1',
    snapshot: {},
    stack: 'test-stack',
    started_at: null,
    status: 'queued',
    ...overrides,
  }
}

describe('authoritative run replacement', () => {
  it('adopts the server-generated id for an imported run and its selection', () => {
    const imported = {
      ...fromRunSummary(makeRunSummary({ run_id: 'external-run' }), 'test-stack'),
      source: 'imported' as const,
    }
    const state = {
      ...createEmptyProjectState(),
      researchRunOrder: [imported.runId],
      researchRuns: { [imported.runId]: imported },
      ui: {
        ...createEmptyProjectState().ui,
        expandedJobId: imported.runId,
        pendingChatReportRunId: imported.runId,
        selectedJobId: imported.runId,
      },
    }

    const adopted = researchDeskReducer(state, {
      sourceRunId: imported.runId,
      summary: makeRunSummary({ run_id: 'run_server_allocated' }),
      type: 'adoptImportedApiRun',
    })

    expect(adopted.researchRuns['external-run']).toBeUndefined()
    expect(adopted.researchRuns.run_server_allocated).toMatchObject({
      runId: 'run_server_allocated',
      source: 'api',
    })
    expect(adopted.researchRunOrder).toEqual(['run_server_allocated'])
    expect(adopted.ui).toMatchObject({
      expandedJobId: 'run_server_allocated',
      pendingChatReportRunId: 'run_server_allocated',
      selectedJobId: 'run_server_allocated',
    })
  })

  it('prunes missing API runs while preserving imported rows and server order', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      summary: makeRunSummary({ run_id: 'run-revoked' }),
      type: 'upsertApiRunSummary',
    })
    const imported: ResearchRunRecord = {
      ...fromRunSummary(makeRunSummary({ run_id: 'run-imported' }), 'test-stack'),
      source: 'imported',
    }
    state = {
      ...state,
      researchRunOrder: ['run-revoked', imported.runId],
      researchRuns: { ...state.researchRuns, [imported.runId]: imported },
    }

    const replaced = researchDeskReducer(state, {
      summaries: [
        makeRunSummary({ run_id: 'run-newest' }),
        makeRunSummary({ run_id: 'run-older' }),
      ],
      type: 'replaceApiRunSummaries',
    })

    expect(replaced.researchRuns['run-revoked']).toBeUndefined()
    expect(replaced.researchRuns['run-imported']).toBe(imported)
    expect(replaced.researchRunOrder).toEqual([
      'run-newest',
      'run-older',
      'run-imported',
    ])
    expect(replaced.ui.selectedJobId).toBe('run-newest')
  })

  it('keeps live event state for present runs and removes vanished agent shells', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      summary: makeRunSummary({ run_id: 'run-live' }),
      type: 'upsertApiRunSummary',
    })
    state = {
      ...state,
      researchRuns: {
        ...state.researchRuns,
        'run-live': {
          ...state.researchRuns['run-live'],
          events: [{
            createdAt: '2026-01-01T00:00:05.000Z',
            id: 'event-live',
            kind: 'system',
            severity: 'info',
            title: 'Live detail',
          }],
        },
      },
    }
    state = researchDeskReducer(state, {
      summary: makeRunSummary({
        kind: 'agent',
        mode: 'workspace_agent',
        run_id: 'run-agent-revoked',
      }),
      type: 'upsertApiRunSummary',
    })

    const replaced = researchDeskReducer(state, {
      summaries: [makeRunSummary({ run_id: 'run-live', status: 'running' })],
      type: 'replaceApiRunSummaries',
    })

    expect(replaced.researchRuns['run-live'].events).toHaveLength(1)
    expect(replaced.agentRuns['run-agent-revoked']).toBeUndefined()
    expect(replaced.agentSessions['run-agent-revoked']).toBeUndefined()
  })

  it('retains loaded agent children for a visible root and prunes the full removed tree', () => {
    const root = makeRunSummary({
      kind: 'agent',
      mode: 'workspace_agent',
      run_id: 'run-agent-root',
      session_id: 'session-agent',
    })
    const child = makeRunSummary({
      kind: 'agent_child',
      mode: 'workspace_agent',
      parent_run_id: root.run_id,
      root_run_id: root.run_id,
      run_id: 'run-agent-child',
      session_id: 'session-agent',
    })
    let state = researchDeskReducer(createEmptyProjectState(), {
      summary: root,
      type: 'upsertAgentRunSummary',
    })
    state = researchDeskReducer(state, {
      summary: child,
      type: 'upsertAgentRunSummary',
    })

    const retained = researchDeskReducer(state, {
      summaries: [root],
      type: 'replaceApiRunSummaries',
    })
    expect(retained.agentRuns[root.run_id]).toBeDefined()
    expect(retained.agentRuns[child.run_id]).toMatchObject({
      parentRunId: root.run_id,
      rootRunId: root.run_id,
    })

    const pruned = researchDeskReducer(retained, {
      summaries: [],
      type: 'replaceApiRunSummaries',
    })
    expect(pruned.agentRuns[root.run_id]).toBeUndefined()
    expect(pruned.agentRuns[child.run_id]).toBeUndefined()
  })

  it('keeps shared agent runs in a non-persistable view session and prunes it on revoke', () => {
    const shared = makeRunSummary({
      access: { mode: 'shared', permission: 'view' },
      kind: 'agent',
      mode: 'workspace_agent',
      run_id: 'run-agent-shared',
      session_id: 'owner-session-secret',
    })
    let state = researchDeskReducer(createEmptyProjectState(), {
      summary: shared,
      type: 'upsertApiRunSummary',
    })
    const derivedId = state.agentRuns[shared.run_id].sessionId as string

    expect(derivedId).toBe('shared-agent-view:run-agent-shared')
    expect(derivedId).not.toContain(shared.session_id as string)
    expect(state.agentSessions[derivedId]).toMatchObject({
      persistable: false,
      runIds: [shared.run_id],
    })
    expect(persistableAgentSessionsInOrder(
      state.agentSessions,
      state.agentSessionOrder,
    )).toEqual([])

    state = researchDeskReducer(state, {
      summaries: [shared],
      type: 'replaceApiRunSummaries',
    })
    expect(state.agentSessionOrder.filter((id) => id === derivedId)).toEqual([
      derivedId,
    ])
    expect(persistableAgentSessionsInOrder(
      state.agentSessions,
      state.agentSessionOrder,
    )).toEqual([])

    state = researchDeskReducer(state, {
      summaries: [],
      type: 'replaceApiRunSummaries',
    })
    expect(state.agentRuns[shared.run_id]).toBeUndefined()
    expect(state.agentSessions[derivedId]).toBeUndefined()
    expect(state.agentSessionOrder).not.toContain(derivedId)
  })

  it('never asks the server for a session it made up from a run id', () => {
    // A run with no session of its own still needs a row in the rail, so
    // the reducer fabricates a shell keyed by the RUN id. That shell is
    // local grouping and has no server row: marking it persistable made
    // the desk fetch `GET /v1/agent-sessions/run_…`, take the 404, and
    // paint a red error over a perfectly healthy run.
    const sessionless = makeRunSummary({
      kind: 'agent',
      mode: 'workspace_agent',
      run_id: 'run-agent-legacy',
      session_id: undefined,
    })
    const state = researchDeskReducer(createEmptyProjectState(), {
      summary: sessionless,
      type: 'upsertApiRunSummary',
    })

    // The shell is keyed by the run id — that is what makes it unfetchable.
    expect(state.agentSessions['run-agent-legacy']).toMatchObject({
      persistable: false,
      runIds: ['run-agent-legacy'],
    })
    expect(persistableAgentSessionsInOrder(
      state.agentSessions,
      state.agentSessionOrder,
    )).toEqual([])
  })

  it('keeps owned agent runs on their existing persistable session path', () => {
    const owned = makeRunSummary({
      kind: 'agent',
      mode: 'workspace_agent',
      run_id: 'run-agent-owned',
      session_id: 'session-owned',
    })
    const state = researchDeskReducer(createEmptyProjectState(), {
      summary: owned,
      type: 'upsertApiRunSummary',
    })

    expect(state.agentRuns[owned.run_id].sessionId).toBe('session-owned')
    // The contrast to shared views: an owned run's session is a REAL,
    // persistable-capable record (never the persistable:false shell)...
    expect(
      state.agentSessions['session-owned'].persistable,
    ).not.toBe(false)
    // ...but as an UNTOUCHED fabrication (epoch updatedAt) it is not
    // yet admitted for push — the server row from claim_session is the
    // truth, and pushing the fabrication would resurrect a derived
    // title over the stored one. A user mutation or the
    // server listing lifts the marker.
    expect(persistableAgentSessionsInOrder(
      state.agentSessions,
      state.agentSessionOrder,
    )).toEqual([])
  })
})

function makeChatThread(id: string, title: string, overrides: Partial<ChatThreadRecord> = {}): ChatThreadRecord {
  return {
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    messages: [],
    preview: 'Preview',
    source: 'mock',
    title,
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

function makeEditorFolder(id: string, title: string, overrides: Partial<EditorFolderRecord> = {}): EditorFolderRecord {
  return {
    createdAt: '2026-01-01T00:00:00.000Z',
    id,
    title,
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

function makeEditorDocument(
  id: string,
  title: string,
  overrides: Partial<EditorDocumentRecord> = {},
): EditorDocumentRecord {
  return {
    contentMarkdown: `# ${title}`,
    createdAt: '2026-01-01T00:00:00.000Z',
    folderId: null,
    id,
    revision: 1,
    source: 'blank',
    title,
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

describe('setResearchRunAutocomplete', () => {
  it('toggles a run\'s @-mention availability and marks the project dirty', () => {
    const base = createEmptyProjectState()
    base.researchRuns = {
      'run-1': { events: [], runId: 'run-1', status: 'completed', summary: { title: 'r' } } as unknown as ResearchRunRecord,
    }
    base.researchRunOrder = ['run-1']

    const hidden = researchDeskReducer(base, {
      includeInAutocomplete: false,
      runId: 'run-1',
      type: 'setResearchRunAutocomplete',
    })
    expect(hidden.researchRuns['run-1'].includeInAutocomplete).toBe(false)
    expect(hidden.dirty).toBe(true)

    // An unknown run id is a no-op and returns the same state reference.
    const noop = researchDeskReducer(hidden, {
      includeInAutocomplete: true,
      runId: 'missing',
      type: 'setResearchRunAutocomplete',
    })
    expect(noop).toBe(hidden)
  })
})

describe('mergeRunSummary', () => {
  it('preserves the local @-mention availability across an API summary refresh', () => {
    const prior = {
      events: [],
      includeInAutocomplete: false,
      runId: 'run-1',
      status: 'completed',
      summary: { title: 'r' },
    } as unknown as ResearchRunRecord

    const merged = mergeRunSummary(prior, makeRunSummary({ run_id: 'run-1', status: 'completed' }), 'fallback-stack')

    // A run-list refresh must not silently re-enable a report the user hid.
    expect(merged.includeInAutocomplete).toBe(false)
  })
})

describe('ui visibility reducer actions', () => {
  it('hides and shows the chat history panel', () => {
    const hidden = researchDeskReducer(createEmptyProjectState(), {
      isVisible: false,
      type: 'setChatHistoryVisible',
    })
    expect(hidden.ui.isChatHistoryVisible).toBe(false)
    const shown = researchDeskReducer(hidden, {
      isVisible: true,
      type: 'setChatHistoryVisible',
    })
    expect(shown.ui.isChatHistoryVisible).toBe(true)
  })

  it('marks the project dirty for every collapsible-panel toggle (durable persistence)', () => {
    // Each collapse flag is a persisted ui/editorUi field; a pure toggle must
    // dirty the project so it is saved/pushed and survives reload. A revert that
    // drops dirty:true from any of these would otherwise pass silently.
    const collapseActions = [
      'setChatHistoryVisible',
      'setKnowledgeHistoryVisible',
      'setReportVisible',
      'setEditorTreeVisible',
      'setEditorCommentPanelVisible',
    ] as const
    for (const type of collapseActions) {
      const next = researchDeskReducer(createEmptyProjectState(), { isVisible: false, type })
      expect(next.dirty, `${type} must dirty the project`).toBe(true)
    }
  })

  it('hides and shows the knowledge history panel', () => {
    const hidden = researchDeskReducer(createEmptyProjectState(), {
      isVisible: false,
      type: 'setKnowledgeHistoryVisible',
    })
    expect(hidden.ui.isKnowledgeHistoryVisible).toBe(false)
    const shown = researchDeskReducer(hidden, {
      isVisible: true,
      type: 'setKnowledgeHistoryVisible',
    })
    expect(shown.ui.isKnowledgeHistoryVisible).toBe(true)
  })

  it('persists clamped resizable panel sizes', () => {
    const base = createEmptyProjectState()
    expect(base.ui.panelLayout).toEqual(DEFAULT_PANEL_LAYOUT)

    const resized = researchDeskReducer(base, {
      key: 'chatHistory',
      size: 37,
      type: 'setPanelLayoutSize',
    })
    expect(resized.dirty).toBe(true)
    expect(resized.ui.panelLayout.chatHistory).toBe(37)

    const tooNarrow = researchDeskReducer(resized, {
      key: 'chatHistory',
      size: 0,
      type: 'setPanelLayoutSize',
    })
    expect(tooNarrow.ui.panelLayout.chatHistory).toBe(18)

    const tooWide = researchDeskReducer(base, {
      key: 'researchReport',
      size: 90,
      type: 'setPanelLayoutSize',
    })
    expect(tooWide.ui.panelLayout.researchReport).toBe(58)

    const editorResized = researchDeskReducer(base, {
      key: 'editorComments',
      size: 30,
      type: 'setPanelLayoutSize',
    })
    expect(editorResized.dirty).toBe(true)
    expect(editorResized.ui.panelLayout.editorComments).toBe(30)

    const editorTooNarrow = researchDeskReducer(base, {
      key: 'editorComments',
      size: 5,
      type: 'setPanelLayoutSize',
    })
    expect(editorTooNarrow.ui.panelLayout.editorComments).toBe(20)
  })
})

describe('api run summary reducer actions', () => {
  it('keeps knowledge runs out of the global research run store', () => {
    const base = createEmptyProjectState()

    const reduced = researchDeskReducer(base, {
      summary: makeRunSummary({ mode: 'knowledge', run_id: 'run-knowledge' }),
      type: 'upsertApiRunSummary',
    })

    expect(reduced).toBe(base)
    expect(reduced.researchRuns['run-knowledge']).toBeUndefined()
    expect(reduced.researchRunOrder).not.toContain('run-knowledge')
  })
})

describe('pinned explorer reducer actions', () => {
  it('toggles pinned chat threads, knowledge sessions and editor documents', () => {
    const base = createEmptyProjectState()
    const sessionId = base.selectedKnowledgeSessionId as string
    const seeded = {
      ...base,
      chatThreadOrder: ['ct-1'],
      chatThreads: { 'ct-1': makeChatThread('ct-1', 'Chat') },
      editorDocumentOrder: ['doc-1'],
      editorDocuments: { 'doc-1': makeEditorDocument('doc-1', 'Doc') },
    }

    const withChatPin = researchDeskReducer(seeded, {
      threadId: 'ct-1',
      type: 'togglePinnedChatThread',
    })
    const withKnowledgePin = researchDeskReducer(withChatPin, {
      sessionId,
      type: 'togglePinnedKnowledgeSession',
    })
    const withDocumentPin = researchDeskReducer(withKnowledgePin, {
      documentId: 'doc-1',
      type: 'togglePinnedEditorDocument',
    })

    expect(withDocumentPin.ui.pinnedExplorer.chatThreadIds).toEqual(['ct-1'])
    expect(withDocumentPin.ui.pinnedExplorer.knowledgeSessionIds).toEqual([sessionId])
    expect(withDocumentPin.ui.pinnedExplorer.editorDocumentIds).toEqual(['doc-1'])
    expect(withDocumentPin.dirty).toBe(true)

    const unpinned = researchDeskReducer(withDocumentPin, {
      threadId: 'ct-1',
      type: 'togglePinnedChatThread',
    })
    expect(unpinned.ui.pinnedExplorer.chatThreadIds).toEqual([])
  })

  it('appends new pins so the first pin stays on top (FIFO pin order)', () => {
    const base = createEmptyProjectState()
    const seeded = {
      ...base,
      chatThreadOrder: ['ct-1', 'ct-2'],
      chatThreads: {
        'ct-1': makeChatThread('ct-1', 'First'),
        'ct-2': makeChatThread('ct-2', 'Second'),
      },
    }
    const one = researchDeskReducer(seeded, { threadId: 'ct-1', type: 'togglePinnedChatThread' })
    const two = researchDeskReducer(one, { threadId: 'ct-2', type: 'togglePinnedChatThread' })
    expect(two.ui.pinnedExplorer.chatThreadIds).toEqual(['ct-1', 'ct-2'])
  })

  it('setExplorerSortMode switches one desk and marks the project dirty', () => {
    const base = createEmptyProjectState()
    const next = researchDeskReducer(base, { desk: 'chat', mode: 'manual', type: 'setExplorerSortMode' })
    expect(next.ui.explorerSort).toEqual({ ...base.ui.explorerSort, chat: 'manual' })
    expect(next.dirty).toBe(true)
    expect(researchDeskReducer(next, { desk: 'chat', mode: 'manual', type: 'setExplorerSortMode' })).toBe(next)
  })

  it('adoptExplorerOrder takes the visible order, appends uncovered ids and switches to manual', () => {
    const base = createEmptyProjectState()
    const seeded = {
      ...base,
      chatThreadGroupOrder: ['cg-1', 'cg-2'],
      chatThreadOrder: ['ct-1', 'ct-2', 'ct-3'],
    }
    const next = researchDeskReducer(seeded, {
      desk: 'chat',
      folderIds: ['cg-2', 'cg-1'],
      itemIds: ['ct-3', 'ct-1'],
      type: 'adoptExplorerOrder',
    })
    expect(next.chatThreadOrder).toEqual(['ct-3', 'ct-1', 'ct-2'])
    expect(next.chatThreadGroupOrder).toEqual(['cg-2', 'cg-1'])
    expect(next.ui.explorerSort.chat).toBe('manual')
    expect(next.dirty).toBe(true)
  })

  it('drop index anchors against unpinned rows only (pinned rows render lifted)', () => {
    const base = createEmptyProjectState()
    const seeded = {
      ...base,
      chatThreadOrder: ['ct-p', 'ct-x', 'ct-y', 'ct-z', 'ct-w'],
      chatThreads: {
        'ct-p': makeChatThread('ct-p', 'Pinned'),
        'ct-w': makeChatThread('ct-w', 'Dragged'),
        'ct-x': makeChatThread('ct-x', 'X'),
        'ct-y': makeChatThread('ct-y', 'Y'),
        'ct-z': makeChatThread('ct-z', 'Z'),
      },
      ui: {
        ...base.ui,
        pinnedExplorer: { ...base.ui.pinnedExplorer, chatThreadIds: ['ct-p'] },
      },
    }
    // Visible unpinned section is x, y, z — dropping at index 1 must land
    // BEFORE y (the pinned row p is lifted out and never counted).
    const next = researchDeskReducer(seeded, {
      groupId: null,
      targetIndex: 1,
      threadId: 'ct-w',
      type: 'moveChatThreadToGroup',
    })
    expect(next.chatThreadOrder).toEqual(['ct-p', 'ct-x', 'ct-w', 'ct-y', 'ct-z'])
  })

  it('agent drag: positional move into a folder respects pinned exclusion and adopt covers agent', () => {
    const base = createEmptyProjectState()
    const agentSession = (id: string, title: string, groupId: string | null = null) => ({
      createdAt: '2026-08-01T00:00:00.000Z',
      groupId,
      id,
      runIds: [],
      sourcePolicy: { knowledge: 'available' as const, web: 'available' as const },
      title,
      updatedAt: '2026-08-01T00:00:00.000Z',
    })
    const seeded = {
      ...base,
      agentSessionGroupOrder: ['ag-1'],
      agentSessionGroups: {
        'ag-1': { createdAt: '', id: 'ag-1', title: 'Ordner', updatedAt: '' },
      },
      agentSessionOrder: ['as-p', 'as-a', 'as-b', 'as-c'],
      agentSessions: {
        'as-a': agentSession('as-a', 'In folder A', 'ag-1'),
        'as-b': agentSession('as-b', 'In folder B', 'ag-1'),
        'as-c': agentSession('as-c', 'Loose C'),
        'as-p': agentSession('as-p', 'Pinned in folder', 'ag-1'),
      },
      ui: {
        ...base.ui,
        pinnedExplorer: { ...base.ui.pinnedExplorer, agentSessionIds: ['as-p'] },
      },
    }
    // Visible folder rows are a, b (p is lifted into the pinned block);
    // dropping c at index 1 must land between a and b.
    const moved = researchDeskReducer(seeded, {
      groupId: 'ag-1',
      sessionId: 'as-c',
      targetIndex: 1,
      type: 'moveAgentSessionToGroup',
    })
    expect(moved.agentSessionOrder).toEqual(['as-p', 'as-a', 'as-c', 'as-b'])
    expect(moved.agentSessions['as-c']?.groupId).toBe('ag-1')

    const adopted = researchDeskReducer(seeded, {
      desk: 'agent',
      folderIds: ['ag-1'],
      itemIds: ['as-b', 'as-a'],
      type: 'adoptExplorerOrder',
    })
    expect(adopted.agentSessionOrder).toEqual(['as-b', 'as-a', 'as-p', 'as-c'])
    expect(adopted.ui.explorerSort.agent).toBe('manual')
  })

  it('agent drag: folders reorder positionally', () => {
    const base = createEmptyProjectState()
    const group = (id: string, title: string) => ({ createdAt: '', id, title, updatedAt: '' })
    const seeded = {
      ...base,
      agentSessionGroupOrder: ['ag-1', 'ag-2', 'ag-3'],
      agentSessionGroups: {
        'ag-1': group('ag-1', 'Eins'),
        'ag-2': group('ag-2', 'Zwei'),
        'ag-3': group('ag-3', 'Drei'),
      },
    }
    const next = researchDeskReducer(seeded, {
      groupId: 'ag-3',
      targetIndex: 0,
      type: 'moveAgentSessionGroup',
    })
    expect(next.agentSessionGroupOrder).toEqual(['ag-3', 'ag-1', 'ag-2'])
  })

  it('agent drag: an in-place drop is a strict no-op (no dirty flag)', () => {
    const base = createEmptyProjectState()
    const agentSession = (id: string, title: string) => ({
      createdAt: '2026-08-01T00:00:00.000Z',
      groupId: null,
      id,
      runIds: [],
      sourcePolicy: { knowledge: 'available' as const, web: 'available' as const },
      title,
      updatedAt: '2026-08-01T00:00:00.000Z',
    })
    const seeded = {
      ...base,
      agentSessionOrder: ['as-a', 'as-b'],
      agentSessions: {
        'as-a': agentSession('as-a', 'A'),
        'as-b': agentSession('as-b', 'B'),
      },
    }
    const next = researchDeskReducer(seeded, {
      groupId: null,
      sessionId: 'as-a',
      targetIndex: 0,
      type: 'moveAgentSessionToGroup',
    })
    expect(next).toBe(seeded)
  })

  it('removes stale pins when pinned entries are deleted', () => {
    const base = createEmptyProjectState()
    const sessionId = base.selectedKnowledgeSessionId as string
    const seeded = {
      ...base,
      chatThreadOrder: ['ct-1'],
      chatThreads: { 'ct-1': makeChatThread('ct-1', 'Chat') },
      editorDocumentOrder: ['doc-1'],
      editorDocuments: { 'doc-1': makeEditorDocument('doc-1', 'Doc') },
      editorUi: { ...base.editorUi, activeDocumentId: 'doc-1', openDocumentIds: ['doc-1'] },
      ui: {
        ...base.ui,
        pinnedExplorer: {
          chatThreadIds: ['ct-1'],
          editorDocumentIds: ['doc-1'],
          knowledgeSessionIds: [sessionId],
          agentSessionIds: [],
        },
        selectedChatThreadId: 'ct-1',
      },
    }

    const withoutChat = researchDeskReducer(seeded, {
      threadId: 'ct-1',
      type: 'deleteChatThread',
    })
    const withoutKnowledge = researchDeskReducer(withoutChat, {
      sessionId,
      type: 'deleteKnowledgeSession',
    })
    const withoutDocument = researchDeskReducer(withoutKnowledge, {
      documentId: 'doc-1',
      type: 'deleteEditorDocument',
    })

    expect(withoutDocument.ui.pinnedExplorer).toEqual({
      chatThreadIds: [],
      editorDocumentIds: [],
      knowledgeSessionIds: [],
      agentSessionIds: [],
    })
  })
})

describe('knowledge session reducer actions', () => {
  it('creates, selects, and renames knowledge sessions', () => {
    const base = createEmptyProjectState()
    const created = researchDeskReducer(base, {
      session: makeKnowledgeSession('ks-2', 'Client review'),
      type: 'createKnowledgeSession',
    })
    expect(created.selectedKnowledgeSessionId).toBe('ks-2')
    expect(created.knowledgeSessionOrder[0]).toBe('ks-2')

    const renamed = researchDeskReducer(created, {
      sessionId: 'ks-2',
      title: 'Board memo',
      type: 'renameKnowledgeSession',
    })
    expect(renamed.knowledgeSessions['ks-2'].title).toBe('Board memo')

    const selected = researchDeskReducer(renamed, {
      sessionId: base.selectedKnowledgeSessionId as string,
      type: 'selectKnowledgeSession',
    })
    expect(selected.selectedKnowledgeSessionId).toBe(base.selectedKnowledgeSessionId)
  })

  it('keeps questions scoped to their knowledge session and removes them on delete', () => {
    const base = createEmptyProjectState()
    const defaultSessionId = base.selectedKnowledgeSessionId as string
    const withSecondSession = researchDeskReducer(base, {
      session: makeKnowledgeSession('ks-2', 'New session'),
      type: 'createKnowledgeSession',
    })
    const withDefaultItem = researchDeskReducer(withSecondSession, {
      item: makeKnowledgeItem('ki-1', defaultSessionId),
      type: 'startKnowledgeAsk',
    })
    const withSecondItem = researchDeskReducer(withDefaultItem, {
      item: makeKnowledgeItem('ki-2', 'ks-2', { question: 'Wie ist Artikel 6 belegt?' }),
      type: 'startKnowledgeAsk',
    })

    expect(withSecondItem.knowledgeItems['ki-1'].sessionId).toBe(defaultSessionId)
    expect(withSecondItem.knowledgeItems['ki-2'].sessionId).toBe('ks-2')
    expect(withSecondItem.knowledgeSessions['ks-2'].title).toBe('Wie ist Artikel 6 belegt?')

    const deleted = researchDeskReducer(withSecondItem, {
      sessionId: 'ks-2',
      type: 'deleteKnowledgeSession',
    })
    expect(deleted.knowledgeSessions['ks-2']).toBeUndefined()
    expect(deleted.knowledgeItems['ki-2']).toBeUndefined()
    expect(deleted.knowledgeItems['ki-1']).toBeDefined()
    expect(deleted.selectedKnowledgeSessionId).toBe(defaultSessionId)
  })

  it('deletes selected knowledge Q&A entries and touches their sessions', () => {
    const base = createEmptyProjectState()
    const defaultSessionId = base.selectedKnowledgeSessionId as string
    const seeded = {
      ...base,
      knowledgeItemOrder: ['ki-1', 'ki-2'],
      knowledgeItems: {
        'ki-1': makeKnowledgeItem('ki-1', defaultSessionId),
        'ki-2': makeKnowledgeItem('ki-2', defaultSessionId, { question: 'Second question' }),
      },
      knowledgeSessions: {
        ...base.knowledgeSessions,
        [defaultSessionId]: { ...base.knowledgeSessions[defaultSessionId], updatedAt: '2026-01-01T00:00:00.000Z' },
      },
    }

    const deleted = researchDeskReducer(seeded, {
      itemIds: ['ki-1'],
      type: 'deleteKnowledgeItems',
    })

    expect(deleted.knowledgeItemOrder).toEqual(['ki-2'])
    expect(deleted.knowledgeItems['ki-1']).toBeUndefined()
    expect(deleted.knowledgeItems['ki-2']).toBeDefined()
    expect(deleted.knowledgeSessions[defaultSessionId].updatedAt).not.toBe('2026-01-01T00:00:00.000Z')
    expect(deleted.dirty).toBe(true)
  })

  it('clears one knowledge session without removing sibling session entries', () => {
    const base = createEmptyProjectState()
    const defaultSessionId = base.selectedKnowledgeSessionId as string
    const seeded = {
      ...base,
      knowledgeItemOrder: ['ki-1', 'ki-2'],
      knowledgeItems: {
        'ki-1': makeKnowledgeItem('ki-1', defaultSessionId),
        'ki-2': makeKnowledgeItem('ki-2', 'ks-2'),
      },
      knowledgeSessionOrder: [defaultSessionId, 'ks-2'],
      knowledgeSessions: {
        ...base.knowledgeSessions,
        [defaultSessionId]: { ...base.knowledgeSessions[defaultSessionId], updatedAt: '2026-01-01T00:00:00.000Z' },
        'ks-2': makeKnowledgeSession('ks-2', 'Second'),
      },
    }

    const cleared = researchDeskReducer(seeded, {
      sessionId: defaultSessionId,
      type: 'clearKnowledgeSession',
    })

    expect(cleared.knowledgeItemOrder).toEqual(['ki-2'])
    expect(cleared.knowledgeItems['ki-1']).toBeUndefined()
    expect(cleared.knowledgeItems['ki-2']).toBeDefined()
    expect(cleared.knowledgeSessions[defaultSessionId]).toBeDefined()
    expect(cleared.knowledgeSessions[defaultSessionId].updatedAt).not.toBe('2026-01-01T00:00:00.000Z')
    expect(cleared.selectedKnowledgeSessionId).toBe(defaultSessionId)
    expect(cleared.dirty).toBe(true)
  })

  it('restarts a knowledge ask in place and clears the previous answer payload', () => {
    const base = createEmptyProjectState()
    const defaultSessionId = base.selectedKnowledgeSessionId as string
    const seeded = {
      ...base,
      knowledgeItemOrder: ['ki-1'],
      knowledgeItems: {
        'ki-1': makeKnowledgeItem('ki-1', defaultSessionId, {
          answer: {
            answerMarkdown: 'Old answer',
            degradedStages: [],
            retrievalDegradations: [],
            references: [],
            refusal: false,
            quotes: [],
          },
          collectionIds: ['collection-old'],
          completedAt: '2026-01-03T00:00:00.000Z',
          error: 'old error',
          status: 'completed',
          topK: 5,
        }),
      },
    }

    const restarted = researchDeskReducer(seeded, {
      item: makeKnowledgeItem('ki-new', defaultSessionId, {
        collectionIds: ['collection-new'],
        collectionTitles: ['EU Recht', 'AI Act'],
        createdAt: '2026-01-04T00:00:00.000Z',
        question: 'Updated question',
        requestedProfile: 'tief',
        runId: 'run-new',
        topK: null,
      }),
      replacedItemId: 'ki-1',
      type: 'restartKnowledgeAsk',
    })

    expect(restarted.knowledgeItemOrder).toEqual(['ki-1'])
    expect(restarted.knowledgeItems['ki-1']).toMatchObject({
      collectionIds: ['collection-new'],
      collectionTitles: ['EU Recht', 'AI Act'],
      createdAt: '2026-01-04T00:00:00.000Z',
      id: 'ki-1',
      progress: { steps: [] },
      question: 'Updated question',
      requestedProfile: 'tief',
      runId: 'run-new',
      sessionId: defaultSessionId,
      status: 'running',
      topK: null,
    })
    expect(restarted.knowledgeItems['ki-1'].answer).toBeUndefined()
    expect(restarted.knowledgeItems['ki-1'].completedAt).toBeUndefined()
    expect(restarted.knowledgeItems['ki-1'].error).toBeUndefined()
    expect(restarted.knowledgeSessions[defaultSessionId].updatedAt).toBe('2026-01-04T00:00:00.000Z')
    expect(restarted.dirty).toBe(true)
  })

  it('sets completedAt when a knowledge item completes', () => {
    const base = createEmptyProjectState()
    const defaultSessionId = base.selectedKnowledgeSessionId as string
    const seeded = {
      ...base,
      knowledgeItemOrder: ['ki-1'],
      knowledgeItems: {
        'ki-1': makeKnowledgeItem('ki-1', defaultSessionId, {
          progress: {
            steps: [{ facts: {}, id: 'answer', kind: 'answer', status: 'running' }],
          },
        }),
      },
    }

    const completed = researchDeskReducer(seeded, {
      answer: {
        answerMarkdown: 'Fresh answer',
        degradedStages: [],
        retrievalDegradations: [],
        references: [],
        refusal: false,
        quotes: [],
      },
      runId: 'run-ki-1',
      type: 'completeKnowledgeItem',
    })

    expect(completed.knowledgeItems['ki-1'].status).toBe('completed')
    expect(completed.knowledgeItems['ki-1'].completedAt).toMatch(/^\d{4}-\d{2}-\d{2}T/)
    expect(completed.knowledgeItems['ki-1'].progress.steps[0].status).toBe('done')
    expect(completed.knowledgeSessions[defaultSessionId].updatedAt).toBe(completed.knowledgeItems['ki-1'].completedAt)
    expect(completed.dirty).toBe(true)
  })

  it('leaves a completed ask and its session as they are when the same result arrives again', () => {
    // Every refresh pulse reloads finished results. Attaching one again must
    // neither redate the answer nor move its session; earlier clients stamped
    // both with the time of the reload and saved the whole session again.
    const base = createEmptyProjectState()
    const sessionId = base.selectedKnowledgeSessionId as string
    const seeded = {
      ...base,
      dirty: false,
      knowledgeItemOrder: ['ki-1'],
      knowledgeItems: {
        'ki-1': makeKnowledgeItem('ki-1', sessionId, {
          answer: storedAnswer,
          completedAt: '2026-01-02T00:03:00.000Z',
          status: 'completed',
        }),
      },
      knowledgeSessions: {
        ...base.knowledgeSessions,
        [sessionId]: { ...base.knowledgeSessions[sessionId], updatedAt: '2026-01-02T00:03:00.000Z' },
      },
    }
    expect(researchDeskReducer(seeded, {
      finishedAt: '2026-01-02T00:03:00.000Z',
      result: storedResult,
      type: 'attachApiRunResult',
    })).toBe(seeded)
    expect(researchDeskReducer(seeded, { result: storedResult, type: 'attachApiRunResult' })).toBe(seeded)
  })

  it('stamps the first completion of an ask with the run finish', () => {
    const base = createEmptyProjectState()
    const sessionId = base.selectedKnowledgeSessionId as string
    const seeded = {
      ...base,
      knowledgeItemOrder: ['ki-1'],
      knowledgeItems: { 'ki-1': makeKnowledgeItem('ki-1', sessionId) },
      knowledgeSessions: {
        ...base.knowledgeSessions,
        [sessionId]: { ...base.knowledgeSessions[sessionId], updatedAt: '2026-01-02T00:00:00.000Z' },
      },
    }
    const completed = researchDeskReducer(seeded, {
      finishedAt: '2026-01-02T00:03:00.000Z',
      result: storedResult,
      type: 'attachApiRunResult',
    })
    expect(completed.knowledgeItems['ki-1'].status).toBe('completed')
    expect(completed.knowledgeItems['ki-1'].completedAt).toBe('2026-01-02T00:03:00.000Z')
    expect(completed.knowledgeSessions[sessionId].updatedAt).toBe('2026-01-02T00:03:00.000Z')
    expect(completed.dirty).toBe(true)
  })

  it('corrects a redated answer and the session time that redating set, once', () => {
    // Earlier clients stamped answer and session with the same reload time.
    // The run finish is the truth; the session takes its latest real activity.
    const base = createEmptyProjectState()
    const sessionId = base.selectedKnowledgeSessionId as string
    const redated = '2026-10-04T08:44:12.430Z'
    const seeded = {
      ...base,
      dirty: false,
      knowledgeItemOrder: ['ki-1', 'ki-2'],
      knowledgeItems: {
        'ki-1': makeKnowledgeItem('ki-1', sessionId, {
          answer: storedAnswer,
          completedAt: redated,
          status: 'completed',
        }),
        'ki-2': makeKnowledgeItem('ki-2', sessionId, {
          answer: storedAnswer,
          completedAt: '2026-01-02T00:05:00.000Z',
          status: 'completed',
        }),
      },
      knowledgeSessions: {
        ...base.knowledgeSessions,
        [sessionId]: { ...base.knowledgeSessions[sessionId], updatedAt: redated },
      },
    }
    const repaired = researchDeskReducer(seeded, {
      finishedAt: '2026-01-02T00:03:00.000Z',
      result: storedResult,
      type: 'attachApiRunResult',
    })
    expect(repaired.knowledgeItems['ki-1'].completedAt).toBe('2026-01-02T00:03:00.000Z')
    expect(repaired.knowledgeItems['ki-1'].answer).toBe(storedAnswer)
    expect(repaired.knowledgeSessions[sessionId].updatedAt).toBe('2026-01-02T00:05:00.000Z')
    expect(repaired.dirty).toBe(true)
    expect(researchDeskReducer(repaired, {
      finishedAt: '2026-01-02T00:03:00.000Z',
      result: storedResult,
      type: 'attachApiRunResult',
    })).toBe(repaired)
  })

  it('keeps a session time that a later edit set while correcting the answer', () => {
    const base = createEmptyProjectState()
    const sessionId = base.selectedKnowledgeSessionId as string
    const seeded = {
      ...base,
      knowledgeItemOrder: ['ki-1'],
      knowledgeItems: {
        'ki-1': makeKnowledgeItem('ki-1', sessionId, {
          answer: storedAnswer,
          completedAt: '2026-10-04T08:44:12.430Z',
          status: 'completed',
        }),
      },
      knowledgeSessions: {
        ...base.knowledgeSessions,
        [sessionId]: { ...base.knowledgeSessions[sessionId], updatedAt: '2026-10-05T10:00:00.000Z' },
      },
    }
    const repaired = researchDeskReducer(seeded, {
      finishedAt: '2026-01-02T00:03:00.000Z',
      result: storedResult,
      type: 'attachApiRunResult',
    })
    expect(repaired.knowledgeItems['ki-1'].completedAt).toBe('2026-01-02T00:03:00.000Z')
    expect(repaired.knowledgeSessions[sessionId].updatedAt).toBe('2026-10-05T10:00:00.000Z')
  })

  it('keeps an event-only retrieval degradation on the final answer', () => {
    const base = createEmptyProjectState()
    const defaultSessionId = base.selectedKnowledgeSessionId as string
    const seeded = {
      ...base,
      knowledgeItemOrder: ['ki-1'],
      knowledgeItems: {
        'ki-1': makeKnowledgeItem('ki-1', defaultSessionId),
      },
    }
    const degradation = {
      candidate_cap: 64,
      final_evidence_complete: false,
      final_top_k: 8,
      reason: 'vector_overfetch_cap',
      requested_candidate_pool: 40,
      requested_top_k: 8,
      retrieval_mode: 'hybrid',
      returned_candidate_pool: 6,
      returned_hits: 3,
      stage: 'vector_candidate_pool',
    }
    const withEvent = researchDeskReducer(seeded, {
      event: makeRunEvent({
        data: degradation,
        run_id: 'run-ki-1',
        type: 'inqtrix.knowledge.retrieval.degraded',
      }),
      type: 'appendApiRunEvent',
    })
    const completed = researchDeskReducer(withEvent, {
      result: {
        answer: 'Antwort.',
        run_id: 'run-ki-1',
        status: 'completed',
        top_claims: [],
        top_sources: [],
      } as unknown as ResearchRunResult,
      type: 'attachApiRunResult',
    })

    expect(completed.knowledgeItems['ki-1'].answer?.retrievalDegradations).toEqual([
      degradation,
    ])
  })

  it('marks a running knowledge item as cancelled when its run is cancelled', () => {
    const base = createEmptyProjectState()
    const defaultSessionId = base.selectedKnowledgeSessionId as string
    const seeded = {
      ...base,
      knowledgeItemOrder: ['ki-1'],
      knowledgeItems: {
        'ki-1': makeKnowledgeItem('ki-1', defaultSessionId, {
          progress: {
            steps: [{ facts: {}, id: 'answer', kind: 'answer', status: 'running' }],
          },
        }),
      },
      knowledgeSessions: {
        ...base.knowledgeSessions,
        [defaultSessionId]: {
          ...base.knowledgeSessions[defaultSessionId],
          updatedAt: '2026-01-01T00:00:00.000Z',
        },
      },
    }
    const event: ResearchRunEvent = {
      created_at: Date.parse('2026-01-02T00:05:00.000Z') / 1000,
      data: { message: 'Request cancelled', status: 'cancelled' },
      run_id: 'run-ki-1',
      sequence: 5,
      type: 'inqtrix.run.cancelled',
    }

    const cancelled = researchDeskReducer(seeded, {
      event,
      type: 'appendApiRunEvent',
    })

    expect(cancelled.knowledgeItems['ki-1'].status).toBe('cancelled')
    expect(cancelled.knowledgeItems['ki-1'].progress.steps[0].status).toBe('done')
    expect(cancelled.knowledgeItems['ki-1'].error).toBeUndefined()
    expect(cancelled.knowledgeSessions[defaultSessionId].updatedAt).not.toBe('2026-01-01T00:00:00.000Z')
  })

  it('deletes the last remaining session, leaving an empty state', () => {
    const base = createEmptyProjectState()
    const onlyId = base.selectedKnowledgeSessionId as string
    expect(base.knowledgeSessionOrder).toEqual([onlyId])

    const deleted = researchDeskReducer(base, {
      sessionId: onlyId,
      type: 'deleteKnowledgeSession',
    })

    // The last session is deletable — no minimum-count guard — and the next
    // ask creates a fresh one.
    expect(deleted.knowledgeSessions[onlyId]).toBeUndefined()
    expect(deleted.knowledgeSessionOrder).toEqual([])
    expect(deleted.selectedKnowledgeSessionId).toBeNull()
  })

  it('prunes the pristine bootstrap default on an empty-server hydrate', () => {
    const base = createEmptyProjectState()
    const defaultId = base.selectedKnowledgeSessionId as string

    const pruned = researchDeskReducer(base, {
      serverIds: [],
      type: 'pruneLocalPlaceholderKnowledgeSessions',
    })

    // No phantom "Neue Sitzung" lingers (and so none gets re-synced as a ghost).
    expect(pruned.knowledgeSessions[defaultId]).toBeUndefined()
    expect(pruned.knowledgeSessionOrder).toEqual([])
    expect(pruned.selectedKnowledgeSessionId).toBeNull()
  })

  it('keeps a renamed empty bootstrap session on an empty-server hydrate', () => {
    const base = createEmptyProjectState()
    const defaultId = base.selectedKnowledgeSessionId as string
    const renamed = researchDeskReducer(base, {
      sessionId: defaultId,
      title: 'Test',
      type: 'renameKnowledgeSession',
    })

    const pruned = researchDeskReducer(renamed, {
      serverIds: [],
      type: 'pruneLocalPlaceholderKnowledgeSessions',
    })

    expect(pruned.knowledgeSessions[defaultId]?.title).toBe('Test')
    expect(pruned.knowledgeSessionOrder).toEqual([defaultId])
  })

  it('keeps a user-created empty session on an empty-server hydrate', () => {
    const base = createEmptyProjectState()
    const defaultId = base.selectedKnowledgeSessionId as string
    const created = researchDeskReducer(base, {
      session: makeKnowledgeSession('ks-2', 'Test'),
      type: 'createKnowledgeSession',
    })

    const pruned = researchDeskReducer(created, {
      serverIds: [],
      type: 'pruneLocalPlaceholderKnowledgeSessions',
    })

    expect(pruned.knowledgeSessions[defaultId]).toBeUndefined()
    expect(pruned.knowledgeSessions['ks-2']?.title).toBe('Test')
    expect(pruned.knowledgeSessionOrder).toEqual(['ks-2'])
    expect(pruned.selectedKnowledgeSessionId).toBe('ks-2')
  })

  it('keeps a placeholder session that has local items pending sync', () => {
    const base = createEmptyProjectState()
    const defaultId = base.selectedKnowledgeSessionId as string
    const withItem = researchDeskReducer(base, {
      item: makeKnowledgeItem('ki-1', defaultId),
      type: 'startKnowledgeAsk',
    })

    const pruned = researchDeskReducer(withItem, {
      serverIds: [],
      type: 'pruneLocalPlaceholderKnowledgeSessions',
    })

    expect(pruned.knowledgeSessions[defaultId]).toBeDefined()
  })

  it('hydrates server knowledge groups and session memberships', () => {
    const base = createEmptyProjectState()
    const withGroup = researchDeskReducer(base, {
      groups: [makeKnowledgeGroup('kg-1', 'Folder')],
      type: 'upsertServerKnowledgeSessionGroups',
    })
    const withSession = researchDeskReducer(withGroup, {
      memberships: { 'ks-server': 'kg-1' },
      sessions: [
        makeKnowledgeSession('ks-server', 'Server session', {
          updatedAt: '2026-01-03T00:00:00.000Z',
        }),
      ],
      type: 'upsertServerKnowledgeSessions',
    })

    expect(withSession.dirty).toBe(base.dirty)
    expect(withSession.knowledgeSessionGroupOrder[0]).toBe('kg-1')
    expect(withSession.knowledgeSessionGroups['kg-1']?.title).toBe('Folder')
    expect(withSession.knowledgeSessions['ks-server']?.title).toBe('Server session')
    expect(withSession.knowledgeSessionGroupMemberships['ks-server']).toBe('kg-1')
  })

  it('keeps a server-known session even when its items are not loaded yet', () => {
    const base = createEmptyProjectState()
    const defaultId = base.selectedKnowledgeSessionId as string

    const pruned = researchDeskReducer(base, {
      serverIds: [defaultId],
      type: 'pruneLocalPlaceholderKnowledgeSessions',
    })

    expect(pruned.knowledgeSessions[defaultId]).toBeDefined()
    expect(pruned.knowledgeSessionOrder).toEqual([defaultId])
  })

  it('does not overwrite a custom empty-session title on first ask', () => {
    const withSession = researchDeskReducer(createEmptyProjectState(), {
      session: makeKnowledgeSession('ks-2', 'Client A'),
      type: 'createKnowledgeSession',
    })
    const next = researchDeskReducer(withSession, {
      item: makeKnowledgeItem('ki-1', 'ks-2', { question: 'Welche Risiken gibt es?' }),
      type: 'startKnowledgeAsk',
    })
    expect(next.knowledgeSessions['ks-2'].title).toBe('Client A')
  })

  it('moves knowledge sessions into folders and clears memberships when a folder is deleted', () => {
    const base = createEmptyProjectState()
    const defaultSessionId = base.selectedKnowledgeSessionId as string
    const withFolder = researchDeskReducer(base, {
      title: 'Client files',
      type: 'createKnowledgeSessionGroup',
    })
    const groupId = withFolder.knowledgeSessionGroupOrder[0]
    const withSecondSession = researchDeskReducer(withFolder, {
      session: makeKnowledgeSession('ks-2', 'Evidence review'),
      type: 'createKnowledgeSession',
    })
    const movedSecond = researchDeskReducer(withSecondSession, {
      groupId,
      sessionId: 'ks-2',
      targetIndex: 0,
      type: 'moveKnowledgeSessionToGroup',
    })
    const movedDefault = researchDeskReducer(movedSecond, {
      groupId,
      sessionId: defaultSessionId,
      targetIndex: 0,
      type: 'moveKnowledgeSessionToGroup',
    })

    expect(movedDefault.knowledgeSessionGroupMemberships['ks-2']).toBe(groupId)
    expect(movedDefault.knowledgeSessionGroupMemberships[defaultSessionId]).toBe(groupId)
    expect(movedDefault.knowledgeSessionOrder.slice(0, 2)).toEqual([defaultSessionId, 'ks-2'])

    const deletedFolder = researchDeskReducer(movedDefault, {
      groupId,
      type: 'deleteKnowledgeSessionGroup',
    })

    expect(deletedFolder.knowledgeSessionGroups[groupId]).toBeUndefined()
    expect(deletedFolder.knowledgeSessionGroupMemberships['ks-2']).toBeUndefined()
    expect(deletedFolder.knowledgeSessions['ks-2']).toBeDefined()
    expect(deletedFolder.knowledgeSessions[defaultSessionId]).toBeDefined()
  })

  it('reorders knowledge session folders', () => {
    const withFirstFolder = researchDeskReducer(createEmptyProjectState(), {
      title: 'First',
      type: 'createKnowledgeSessionGroup',
    })
    const firstGroupId = withFirstFolder.knowledgeSessionGroupOrder[0]
    const withSecondFolder = researchDeskReducer(withFirstFolder, {
      title: 'Second',
      type: 'createKnowledgeSessionGroup',
    })
    const secondGroupId = withSecondFolder.knowledgeSessionGroupOrder[0]

    const reordered = researchDeskReducer(withSecondFolder, {
      groupId: firstGroupId,
      targetIndex: 0,
      type: 'moveKnowledgeSessionGroup',
    })

    expect(withSecondFolder.knowledgeSessionGroupOrder).toEqual([secondGroupId, firstGroupId])
    expect(reordered.knowledgeSessionGroupOrder).toEqual([firstGroupId, secondGroupId])
  })
})

describe('chat folder reducer actions', () => {
  it('creates a localized empty chat without synthetic conversation history', () => {
    const next = researchDeskReducer(createEmptyProjectState(), {
      preview: 'Bereit für eine freie Frage.',
      title: 'Neuer Chat',
      type: 'createChatThread',
    })
    const threadId = next.ui.selectedChatThreadId
    const thread = threadId ? next.chatThreads[threadId] : undefined

    expect(thread).toMatchObject({
      messages: [],
      preview: 'Bereit für eine freie Frage.',
      title: 'Neuer Chat',
    })
  })

  it('starts a new chat on the account preference, not on the previous pick', () => {
    let state = createEmptyProjectState()
    // The user switched this chat to a strong model...
    state = researchDeskReducer(state, {
      model: 'claude-opus-4-8',
      type: 'setSelectedChatModel',
    })
    expect(state.ui.selectedChatModel).toBe('claude-opus-4-8')

    // ...opening a NEW chat must not carry that over; it starts on the
    // preference the user configured in Settings.
    state = researchDeskReducer(state, {
      modelTier: 'fast',
      preview: 'Bereit.',
      title: 'Neuer Chat',
      type: 'createChatThread',
    })
    expect(state.ui.selectedChatModelTier).toBe('fast')
    // Exclusivity: a tier clears the explicit model and its effort.
    expect(state.ui.selectedChatModel).toBeNull()
    expect(state.ui.selectedChatEffort).toBeNull()
  })

  it('a new chat without a preference falls back to the server default', () => {
    const state = researchDeskReducer(createEmptyProjectState(), {
      preview: 'Bereit.',
      title: 'Neuer Chat',
      type: 'createChatThread',
    })
    // null is what the picker shows as its server-default entry — never an
    // invented tier.
    expect(state.ui.selectedChatModelTier).toBeNull()
  })

  it('a new chat never touches the agent selection', () => {
    // An agent run fans out over several thinking nodes while a chat answer is
    // a single call. Opening a chat must not move agent spend.
    let state = researchDeskReducer(createEmptyProjectState(), {
      tier: 'high',
      type: 'setSelectedAgentModelTier',
    })
    state = researchDeskReducer(state, {
      modelTier: 'fast',
      preview: 'Bereit.',
      title: 'Neuer Chat',
      type: 'createChatThread',
    })
    expect(state.ui.selectedChatModelTier).toBe('fast')
    expect(state.ui.selectedAgentModelTier).toBe('high')
  })

  it('uses the active UI language when a conversation is cleared', () => {
    const base = createEmptyProjectState()
    const thread = makeChatThread('ct-1', 'Frage', {
      messages: [{
        contentMarkdown: 'Was ist neu?',
        createdAt: '2026-01-01T00:00:01.000Z',
        id: 'cm-1',
        role: 'user',
      }],
      preview: 'Was ist neu?',
    })
    const cleared = researchDeskReducer({
      ...base,
      chatThreadOrder: [thread.id],
      chatThreads: { [thread.id]: thread },
    }, {
      emptyPreview: 'Bereit für eine freie Frage.',
      threadId: thread.id,
      type: 'clearChatThread',
    })

    expect(cleared.chatThreads[thread.id]).toMatchObject({
      messages: [],
      preview: 'Bereit für eine freie Frage.',
      title: 'Frage',
    })
  })

  it('uses the active UI language when every selected message is deleted', () => {
    const base = createEmptyProjectState()
    const thread = makeChatThread('ct-1', 'Question', {
      messages: [{
        contentMarkdown: 'What changed?',
        createdAt: '2026-01-01T00:00:01.000Z',
        id: 'cm-1',
        role: 'user',
      }],
      preview: 'What changed?',
    })
    const cleared = researchDeskReducer({
      ...base,
      chatThreadOrder: [thread.id],
      chatThreads: { [thread.id]: thread },
    }, {
      emptyPreview: 'Ready for an open question.',
      messageIds: ['cm-1'],
      threadId: thread.id,
      type: 'deleteChatMessages',
    })

    expect(cleared.chatThreads[thread.id]).toMatchObject({
      messages: [],
      preview: 'Ready for an open question.',
      title: 'Question',
    })
  })

  it('creates a chat thread inside the requested folder', () => {
    const withFolder = researchDeskReducer(createEmptyProjectState(), {
      title: 'Folder',
      type: 'createChatThreadGroup',
    })
    const groupId = withFolder.chatThreadGroupOrder[0]

    const next = researchDeskReducer(withFolder, {
      groupId,
      preview: 'Ready for an open question.',
      title: 'New chat',
      type: 'createChatThread',
    })
    const threadId = next.ui.selectedChatThreadId

    expect(threadId).toBeTruthy()
    expect(next.chatThreadGroupMemberships[threadId as string]).toBe(groupId)
    expect(next.chatThreadOrder[0]).toBe(threadId)
    expect(next.dirty).toBe(true)
  })

  it('dissolves a deleted chat folder while keeping its threads', () => {
    const base = createEmptyProjectState()
    const seeded = {
      ...base,
      chatThreadGroupMemberships: { 'ct-1': 'cg-1' },
      chatThreadGroupOrder: ['cg-1'],
      chatThreadGroups: {
        'cg-1': {
          createdAt: '2026-01-01T00:00:00.000Z',
          id: 'cg-1',
          title: 'Folder',
          updatedAt: '2026-01-01T00:00:00.000Z',
        },
      },
      chatThreadOrder: ['ct-1'],
      chatThreads: { 'ct-1': makeChatThread('ct-1', 'Chat') },
    }

    const next = researchDeskReducer(seeded, {
      groupId: 'cg-1',
      type: 'deleteChatThreadGroup',
    })

    expect(next.chatThreadGroups['cg-1']).toBeUndefined()
    expect(next.chatThreadGroupMemberships['ct-1']).toBeUndefined()
    expect(next.chatThreads['ct-1']).toBeDefined()
  })
})

describe('editor folder reducer actions', () => {
  it('dissolves a deleted editor folder while keeping its documents', () => {
    const base = createEmptyProjectState()
    const seeded = {
      ...base,
      editorDocumentOrder: ['doc-1'],
      editorDocuments: { 'doc-1': makeEditorDocument('doc-1', 'Doc', { folderId: 'folder-1' }) },
      editorFolderOrder: ['folder-1'],
      editorFolders: { 'folder-1': makeEditorFolder('folder-1', 'Folder') },
    }

    const next = researchDeskReducer(seeded, {
      folderId: 'folder-1',
      type: 'deleteEditorFolder',
    })

    expect(next.editorFolders['folder-1']).toBeUndefined()
    expect(next.editorDocuments['doc-1']).toMatchObject({ folderId: null })
  })
})

describe('file-asset reducer actions', () => {
  it('replaces unreferenced local bootstrap sections with the server scope', () => {
    const base = createEmptyProjectState()
    const localIds = [...base.fileLibrarySectionOrder]
    const createdAt = '2026-01-01T00:00:00.000Z'
    const serverSections: ProjectState['fileLibrarySections'] = {
      'server-library': {
        createdAt,
        id: 'server-library',
        kind: 'custom',
        title: 'Bibliothek',
        updatedAt: createdAt,
      },
      'server-temp': {
        createdAt,
        id: 'server-temp',
        kind: 'temporary',
        title: 'Temporäre Dateien',
        updatedAt: createdAt,
      },
    }
    const hydrated = researchDeskReducer(base, {
      sections: Object.values(serverSections),
      type: 'upsertServerAssetSections',
    })
    const next = researchDeskReducer(hydrated, {
      hiddenServerIds: [],
      serverHasTemporarySection: true,
      serverIds: Object.keys(serverSections),
      type: 'pruneLocalBootstrapFileSections',
    })

    expect(next.fileLibrarySectionOrder).toEqual([
      'server-library',
      'server-temp',
    ])
    expect(localIds.every((id) => next.fileLibrarySections[id] === undefined))
      .toBe(true)
    expect(next.dirty).toBe(false)
  })

  it('keeps a referenced bootstrap section and repairs a missing server temporary section', () => {
    const base = createEmptyProjectState()
    const customId = base.fileLibrarySectionOrder.find(
      (id) => base.fileLibrarySections[id]?.kind === 'custom',
    )!
    const temporaryId = base.fileLibrarySectionOrder.find(
      (id) => base.fileLibrarySections[id]?.kind === 'temporary',
    )!
    const withAsset = researchDeskReducer(base, {
      assets: [makeAsset('f1', 'alpha', { sectionId: customId })],
      type: 'ingestFileAssets',
    })

    const next = researchDeskReducer(withAsset, {
      hiddenServerIds: [],
      serverHasTemporarySection: false,
      serverIds: ['unrelated-server-section'],
      type: 'pruneLocalBootstrapFileSections',
    })

    expect(next.fileLibrarySectionOrder).toContain(customId)
    expect(next.fileLibrarySectionOrder).toContain(temporaryId)
  })

  it('turns a renamed prepared section into durable user data', () => {
    const base = createEmptyProjectState()
    const sectionId = base.fileLibrarySectionOrder.find(
      (id) => base.fileLibrarySections[id]?.kind === 'custom',
    )!

    const renamed = researchDeskReducer(base, {
      sectionId,
      title: 'Mein eigener Bestand',
      type: 'renameFileLibrarySection',
    })
    const next = researchDeskReducer(renamed, {
      hiddenServerIds: [sectionId],
      serverHasTemporarySection: true,
      serverIds: ['server-temp', sectionId],
      type: 'pruneLocalBootstrapFileSections',
    })

    expect(next.fileLibrarySections[sectionId]).toMatchObject({
      isBootstrapPlaceholder: false,
      semanticRole: 'custom',
      title: 'Mein eigener Bestand',
    })
  })

  it('drops projected historical duplicates from local state without deleting server data', () => {
    const base = createEmptyProjectState()
    const createdAt = '2026-01-01T00:00:00.000Z'
    const duplicate = {
      createdAt,
      id: 'historical-duplicate',
      kind: 'custom' as const,
      title: 'Bibliothek',
      updatedAt: createdAt,
    }
    const hydrated = researchDeskReducer(base, {
      sections: [duplicate],
      type: 'upsertServerAssetSections',
    })

    const next = researchDeskReducer(hydrated, {
      hiddenServerIds: [duplicate.id],
      serverHasTemporarySection: true,
      serverIds: [duplicate.id, 'server-canonical'],
      type: 'pruneLocalBootstrapFileSections',
    })

    expect(next.fileLibrarySections[duplicate.id]).toBeUndefined()
    expect(next.fileLibrarySectionOrder).not.toContain(duplicate.id)
  })

  it('never hides a projected server duplicate that local data references', () => {
    const base = createEmptyProjectState()
    const createdAt = '2026-01-01T00:00:00.000Z'
    const duplicate = {
      createdAt,
      id: 'historical-in-use',
      kind: 'custom' as const,
      title: 'Bibliothek',
      updatedAt: createdAt,
    }
    const hydrated = researchDeskReducer(base, {
      sections: [duplicate],
      type: 'upsertServerAssetSections',
    })
    const withLocalAsset = researchDeskReducer(hydrated, {
      assets: [makeAsset('local-file', 'local', { sectionId: duplicate.id })],
      type: 'ingestFileAssets',
    })

    const next = researchDeskReducer(withLocalAsset, {
      hiddenServerIds: [duplicate.id],
      serverHasTemporarySection: true,
      serverIds: [duplicate.id, 'server-canonical'],
      type: 'pruneLocalBootstrapFileSections',
    })

    expect(next.fileLibrarySections[duplicate.id]).toEqual(duplicate)
  })

  it('ingests assets into the store and order', () => {
    const next = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha')],
      type: 'ingestFileAssets',
    })
    expect(next.fileAssetOrder).toContain('f1')
    expect(next.fileAssets.f1.label).toBe('alpha')
    expect(next.dirty).toBe(true)
  })

  it('converges server-owned upload state even when local metadata is newer', () => {
    const local = makeAsset('f1', 'local-newer', {
      extractedText: 'keep local body',
      updatedAt: '2026-02-01T00:00:00.000Z',
      uploadError: null,
      uploadPending: true,
    })
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [local],
      type: 'ingestFileAssets',
    })
    const incoming = makeAsset('f1', 'stale-server-label', {
      extractedText: '',
      updatedAt: '2026-01-01T00:00:00.000Z',
      uploadError: 'Der Upload wurde nicht abgeschlossen.',
      uploadPending: false,
    })

    const next = researchDeskReducer(seeded, {
      assets: [incoming],
      type: 'upsertServerAssetMetadata',
    })

    expect(next.fileAssets.f1.label).toBe('local-newer')
    expect(next.fileAssets.f1.extractedText).toBe('keep local body')
    expect(next.fileAssets.f1.uploadPending).toBe(false)
    expect(next.fileAssets.f1.uploadError).toBe('Der Upload wurde nicht abgeschlossen.')
  })

  it('keeps canonical prepared content separate from the editable body', () => {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha', {
        extractedText: 'client body',
        preparedContentHash: 'sha256:old',
        preparedText: 'old canonical body',
      })],
      type: 'ingestFileAssets',
    })
    const loaded = researchDeskReducer(seeded, {
      assetId: 'f1',
      extractedText: 'editable server body',
      preparedAt: '2026-02-01T00:00:00.000Z',
      preparedContentHash: 'sha256:new',
      preparedParserId: 'markitdown',
      preparedText: 'new canonical body',
      type: 'setServerAssetBody',
    })

    expect(loaded.fileAssets.f1.extractedText).toBe('editable server body')
    expect(loaded.fileAssets.f1.preparedText).toBe('new canonical body')
    expect(loaded.fileAssets.f1.preparedContentHash).toBe('sha256:new')
    expect(loaded.dirty).toBe(seeded.dirty)
  })

  it('renames an asset label', () => {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha')],
      type: 'ingestFileAssets',
    })
    const next = researchDeskReducer(seeded, { fileId: 'f1', label: 'renamed', type: 'renameFileAsset' })
    expect(next.fileAssets.f1.label).toBe('renamed')
  })

  it('toggles the transient parse-pending flag without marking the project dirty', () => {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha')],
      type: 'ingestFileAssets',
    })
    const clean = { ...seeded, dirty: false }
    const pending = researchDeskReducer(clean, { assetId: 'f1', pending: true, type: 'setFileAssetParsePending' })
    expect(pending.fileAssets.f1.parsePending).toBe(true)
    expect(pending.dirty).toBe(false) // transient, never synced
    const cleared = researchDeskReducer(pending, { assetId: 'f1', pending: false, type: 'setFileAssetParsePending' })
    expect(cleared.fileAssets.f1.parsePending).toBe(false)
  })

  it('toggles the transient upload-pending flag without marking the project dirty', () => {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha', { uploadPending: true })],
      type: 'ingestFileAssets',
    })
    const clean = { ...seeded, dirty: false }
    const cleared = researchDeskReducer(clean, { assetId: 'f1', pending: false, type: 'setFileAssetUploadPending' })
    expect(cleared.fileAssets.f1.uploadPending).toBe(false)
    expect(cleared.dirty).toBe(false) // transient, never synced
  })

  it('re-entering upload-pending clears a previous upload error (retry)', () => {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha', { uploadError: 'kaputt', uploadPending: false })],
      type: 'ingestFileAssets',
    })
    const clean = { ...seeded, dirty: false }
    const retrying = researchDeskReducer(clean, { assetId: 'f1', pending: true, type: 'setFileAssetUploadPending' })
    expect(retrying.fileAssets.f1.uploadPending).toBe(true)
    expect(retrying.fileAssets.f1.uploadError).toBeNull()
    expect(retrying.dirty).toBe(false)
  })

  it('adopts a durable queued upload without inventing a completed file', () => {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha', {
        uploadPending: true,
        uploadStatus: 'awaiting_upload',
      })],
      type: 'ingestFileAssets',
    })
    const next = researchDeskReducer({ ...seeded, dirty: false }, {
      assetId: 'f1',
      error: 'storage unavailable',
      operationId: 'up_1',
      serverFileId: null,
      status: 'retrying',
      type: 'adoptFileAssetUploadLifecycle',
    })

    expect(next.fileAssets.f1).toMatchObject({
      uploadOperationId: 'up_1',
      uploadPending: true,
      uploadStatus: 'retrying',
    })
    expect(next.fileAssets.f1.serverFileId ?? null).toBeNull()
    expect(next.dirty).toBe(false)
  })

  it('publishes the server file only when the durable lifecycle reaches ready', () => {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha', {
        uploadOperationId: 'up_1',
        uploadPending: true,
        uploadStatus: 'finalizing',
      })],
      type: 'ingestFileAssets',
    })
    const next = researchDeskReducer({ ...seeded, dirty: false }, {
      assetId: 'f1',
      error: null,
      operationId: 'up_1',
      serverFileId: 'fl_1',
      status: 'ready',
      type: 'adoptFileAssetUploadLifecycle',
    })

    expect(next.fileAssets.f1).toMatchObject({
      serverFileId: 'fl_1',
      uploadPending: false,
      uploadStatus: 'ready',
    })
    expect(next.dirty).toBe(true)
  })

  it('completing an upload persists serverFileId and settles the transient state', () => {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha', { uploadPending: true })],
      type: 'ingestFileAssets',
    })
    const clean = { ...seeded, dirty: false }
    const next = researchDeskReducer(clean, {
      assetId: 'f1',
      serverFileId: 'fl_new',
      type: 'completeFileAssetUpload',
    })
    expect(next.fileAssets.f1.serverFileId).toBe('fl_new')
    expect(next.fileAssets.f1.uploadPending).toBe(false)
    expect(next.fileAssets.f1.uploadError).toBeNull()
    expect(next.dirty).toBe(true) // serverFileId is persisted data
    expect(next.fileAssets.f1.updatedAt).not.toBe(seeded.fileAssets.f1.updatedAt)
  })

  it('a failed upload keeps a transient error badge and a persisted warning', () => {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha', { uploadPending: true })],
      type: 'ingestFileAssets',
    })
    const clean = { ...seeded, dirty: false }
    const next = researchDeskReducer(clean, {
      assetId: 'f1',
      message: 'Server-Upload fehlgeschlagen (503) — Datei bleibt lokal.',
      type: 'failFileAssetUpload',
    })
    expect(next.fileAssets.f1.uploadPending).toBe(false)
    expect(next.fileAssets.f1.uploadError).toContain('fehlgeschlagen')
    expect(next.fileAssets.f1.parseWarning).toContain('fehlgeschlagen')
    expect(next.dirty).toBe(true) // the warning is persisted
    // Repeating the failure never stacks the same warning twice.
    const again = researchDeskReducer(next, {
      assetId: 'f1',
      message: 'Server-Upload fehlgeschlagen (503) — Datei bleibt lokal.',
      type: 'failFileAssetUpload',
    })
    expect(again.fileAssets.f1.parseWarning).toBe(next.fileAssets.f1.parseWarning)
  })

  it('a successful retry retracts the persisted upload-failure warning', () => {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha', { parseWarning: 'Nur ein Teil wurde verarbeitet.', uploadPending: true })],
      type: 'ingestFileAssets',
    })
    const failed = researchDeskReducer(seeded, {
      assetId: 'f1',
      message: 'Server-Upload fehlgeschlagen (503) — Datei bleibt lokal.',
      type: 'failFileAssetUpload',
    })
    expect(failed.fileAssets.f1.parseWarning).toContain('fehlgeschlagen')
    const retried = researchDeskReducer(failed, {
      assetId: 'f1',
      serverFileId: 'fl_retry',
      type: 'completeFileAssetUpload',
    })
    // The "Datei bleibt lokal" claim is retracted; unrelated warnings stay.
    expect(retried.fileAssets.f1.parseWarning).toBe('Nur ein Teil wurde verarbeitet.')
    expect(retried.fileAssets.f1.serverFileId).toBe('fl_retry')
  })

  it('settles a deferred client parse onto a placeholder row', () => {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha', { extractedText: '', parsePending: true })],
      type: 'ingestFileAssets',
    })
    const next = researchDeskReducer(seeded, {
      assetId: 'f1',
      clearParsePending: true,
      extractedText: 'client text',
      pageCount: 3,
      parseStatus: 'parsed',
      parseWarning: null,
      textTruncated: false,
      type: 'applyFileAssetClientParse',
    })
    expect(next.fileAssets.f1.extractedText).toBe('client text')
    expect(next.fileAssets.f1.pageCount).toBe(3)
    expect(next.fileAssets.f1.parserId).toBe('client')
    expect(next.fileAssets.f1.parsePending).toBe(false)
    expect(next.dirty).toBe(true)
  })

  it('keeps the parsing badge when the server parse still owns the settle', () => {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha', { extractedText: '', parsePending: true })],
      type: 'ingestFileAssets',
    })
    const next = researchDeskReducer(seeded, {
      assetId: 'f1',
      clearParsePending: false,
      extractedText: 'client text',
      pageCount: null,
      parseStatus: 'parsed',
      parseWarning: null,
      textTruncated: false,
      type: 'applyFileAssetClientParse',
    })
    expect(next.fileAssets.f1.parsePending).toBe(true)
  })

  it('a settled client parse never downgrades a landed server parse', () => {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha', {
        extractedText: 'markitdown text',
        pageCount: null,
        parserId: 'markitdown',
      })],
      type: 'ingestFileAssets',
    })
    const next = researchDeskReducer(seeded, {
      assetId: 'f1',
      clearParsePending: true,
      extractedText: 'stale client text',
      pageCount: 7,
      parseStatus: 'partial',
      parseWarning: 'client warn',
      textTruncated: true,
      type: 'applyFileAssetClientParse',
    })
    // Only the page count backfills; text/status/provenance stay server-owned.
    expect(next.fileAssets.f1.extractedText).toBe('markitdown text')
    expect(next.fileAssets.f1.parserId).toBe('markitdown')
    expect(next.fileAssets.f1.parseStatus).toBe('parsed')
    expect(next.fileAssets.f1.pageCount).toBe(7)
  })

  it('deletes a batch of assets in one state update, cleaning refs and index members', () => {
    const base = createEmptyProjectState()
    const seeded = researchDeskReducer(base, {
      assets: [makeAsset('f1', 'alpha'), makeAsset('f2', 'beta'), makeAsset('f3', 'gamma')],
      type: 'ingestFileAssets',
    })
    const withIndex = researchDeskReducer(seeded, {
      fileIds: ['f1', 'f2'],
      model: 'text-embedding-3-small',
      title: 'Idx',
      type: 'createVectorIndex',
    })
    const withRef = {
      ...withIndex,
      ui: {
        ...withIndex.ui,
        pendingChatAttachmentRefs: [
          { fileId: 'f1', kind: 'file-asset' as const },
          { fileId: 'f3', kind: 'file-asset' as const },
        ],
      },
    }
    const next = researchDeskReducer(withRef, { fileIds: ['f1', 'f2', 'missing'], type: 'deleteFileAssets' })
    expect(next.fileAssets.f1).toBeUndefined()
    expect(next.fileAssets.f2).toBeUndefined()
    expect(next.fileAssets.f3).toBeDefined()
    expect(next.fileAssetOrder).toEqual(['f3'])
    expect(next.ui.pendingChatAttachmentRefs).toEqual([{ fileId: 'f3', kind: 'file-asset' }])
    const index = Object.values(next.vectorIndexes)[0]
    expect(index.members).toEqual([])
    // Batch of zero existing ids is a no-op with identical state.
    expect(researchDeskReducer(next, { fileIds: ['missing'], type: 'deleteFileAssets' })).toBe(next)
  })

  it('moves a batch of assets in one state update, skipping already-placed files', () => {
    const base = createEmptyProjectState()
    const librarySectionId = base.fileLibrarySectionOrder.find(
      (id) => base.fileLibrarySections[id]?.kind === 'custom',
    )!
    const seeded = researchDeskReducer(base, {
      assets: [makeAsset('f1', 'alpha'), makeAsset('f2', 'beta')],
      type: 'ingestFileAssets',
    })
    const next = researchDeskReducer(seeded, {
      fileIds: ['f1', 'f2'],
      groupId: null,
      sectionId: librarySectionId,
      type: 'moveFileAssets',
    })
    expect(next.fileAssets.f1.sectionId).toBe(librarySectionId)
    expect(next.fileAssets.f2.sectionId).toBe(librarySectionId)
    // Moving them again to the same place changes nothing.
    expect(researchDeskReducer(next, {
      fileIds: ['f1', 'f2'],
      groupId: null,
      sectionId: librarySectionId,
      type: 'moveFileAssets',
    })).toBe(next)
  })

  it('moves an asset into an existing section group', () => {
    const base = createEmptyProjectState()
    const librarySectionId = base.fileLibrarySectionOrder.find(
      (id) => base.fileLibrarySections[id]?.kind === 'custom',
    )!
    const temporarySectionId = base.fileLibrarySectionOrder.find(
      (id) => base.fileLibrarySections[id]?.kind === 'temporary',
    )!
    const created = researchDeskReducer(base, {
      sectionId: librarySectionId,
      title: 'Group',
      type: 'createFileGroup',
    })
    const groupId = created.fileGroupOrder[0]
    const seeded = researchDeskReducer(created, {
      assets: [makeAsset('f1', 'alpha', { sectionId: temporarySectionId })],
      type: 'ingestFileAssets',
    })
    const next = researchDeskReducer(seeded, {
      fileId: 'f1',
      groupId,
      sectionId: librarySectionId,
      type: 'moveFileAsset',
    })
    expect(next.fileAssets.f1.sectionId).toBe(librarySectionId)
    expect(next.fileAssets.f1.groupId).toBe(groupId)
  })

  it('drops a move into a group that does not belong to the target section', () => {
    const base = createEmptyProjectState()
    const librarySectionId = base.fileLibrarySectionOrder.find(
      (id) => base.fileLibrarySections[id]?.kind === 'custom',
    )!
    const temporarySectionId = base.fileLibrarySectionOrder.find(
      (id) => base.fileLibrarySections[id]?.kind === 'temporary',
    )!
    const seeded = researchDeskReducer(base, {
      assets: [makeAsset('f1', 'alpha', { sectionId: temporarySectionId })],
      type: 'ingestFileAssets',
    })
    const next = researchDeskReducer(seeded, {
      fileId: 'f1',
      groupId: 'does-not-exist',
      sectionId: librarySectionId,
      type: 'moveFileAsset',
    })
    expect(next.fileAssets.f1.sectionId).toBe(librarySectionId)
    expect(next.fileAssets.f1.groupId).toBeNull()
  })

  it('deletes an asset and strips its pending chat reference', () => {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: [makeAsset('f1', 'alpha')],
      type: 'ingestFileAssets',
    })
    const withRef = {
      ...seeded,
      ui: { ...seeded.ui, pendingChatAttachmentRefs: [{ fileId: 'f1', kind: 'file-asset' as const }] },
    }
    const next = researchDeskReducer(withRef, { fileId: 'f1', type: 'deleteFileAsset' })
    expect(next.fileAssets.f1).toBeUndefined()
    expect(next.fileAssetOrder).not.toContain('f1')
    expect(next.ui.pendingChatAttachmentRefs).toEqual([])
  })
})

describe('file-group reducer actions', () => {
  it('creates a group under a section', () => {
    const base = createEmptyProjectState()
    const librarySectionId = base.fileLibrarySectionOrder.find(
      (id) => base.fileLibrarySections[id]?.kind === 'custom',
    )!
    const next = researchDeskReducer(base, {
      sectionId: librarySectionId,
      title: 'New Group',
      type: 'createFileGroup',
    })
    expect(next.fileGroupOrder).toHaveLength(1)
    const groupId = next.fileGroupOrder[0]
    expect(next.fileGroups[groupId]).toMatchObject({ sectionId: librarySectionId, title: 'New Group' })
  })

  it('reassigns members to no group when their group is deleted', () => {
    const base = createEmptyProjectState()
    const librarySectionId = base.fileLibrarySectionOrder.find(
      (id) => base.fileLibrarySections[id]?.kind === 'custom',
    )!
    const created = researchDeskReducer(base, {
      sectionId: librarySectionId,
      title: 'Group',
      type: 'createFileGroup',
    })
    const groupId = created.fileGroupOrder[0]
    const withAsset = researchDeskReducer(created, {
      assets: [makeAsset('f1', 'alpha', { groupId, sectionId: librarySectionId })],
      type: 'ingestFileAssets',
    })
    const next = researchDeskReducer(withAsset, { groupId, type: 'deleteFileGroup' })
    expect(next.fileGroups[groupId]).toBeUndefined()
    expect(next.fileAssets.f1.groupId).toBeNull()
  })

  it('keeps a server group and its children visible until its exact deletion operation completes', () => {
    const base = createEmptyProjectState()
    const librarySectionId = base.fileLibrarySectionOrder.find(
      (id) => base.fileLibrarySections[id]?.kind === 'custom',
    )!
    const created = researchDeskReducer(base, {
      sectionId: librarySectionId,
      title: 'Group',
      type: 'createFileGroup',
    })
    const groupId = created.fileGroupOrder[0]
    const synced = researchDeskReducer(created, {
      groupId,
      type: 'markFileGroupServerSynced',
    })
    const withAsset = researchDeskReducer(synced, {
      assets: [makeAsset('f1', 'alpha', { groupId, sectionId: librarySectionId })],
      type: 'ingestFileAssets',
    })

    const deleting = researchDeskReducer(withAsset, {
      error: null,
      groupId,
      operationId: 'del_group',
      stage: 'metadata_removed',
      status: 'running',
      type: 'setFileGroupDeletionState',
    })
    expect(deleting.fileGroups[groupId]).toMatchObject({
      deletionOperationId: 'del_group',
      lifecycleStatus: 'deleting',
    })
    expect(deleting.fileAssets.f1.groupId).toBe(groupId)

    const failed = researchDeskReducer(deleting, {
      error: 'database unavailable',
      groupId,
      operationId: 'del_group',
      stage: 'delete_failed',
      status: 'delete_failed',
      type: 'setFileGroupDeletionState',
    })
    expect(failed.fileGroups[groupId]).toMatchObject({
      deletionError: 'database unavailable',
      lifecycleStatus: 'delete_failed',
    })
    expect(researchDeskReducer(failed, {
      groupId,
      operationId: 'del_other',
      type: 'completeFileGroupDeletion',
    })).toBe(failed)

    const completed = researchDeskReducer(failed, {
      groupId,
      operationId: 'del_group',
      type: 'completeFileGroupDeletion',
    })
    expect(completed.fileGroups[groupId]).toBeUndefined()
    expect(completed.fileAssets.f1.groupId).toBeNull()
  })
})

describe('legacy resource identity migration', () => {
  it('rekeys a file section and all local parent references atomically', () => {
    const base = createEmptyProjectState()
    const originalId = base.fileLibrarySectionOrder.find(
      (id) => base.fileLibrarySections[id]?.kind === 'temporary',
    )!
    const legacyId = 'file-section-temp'
    const section = base.fileLibrarySections[originalId]
    const fileLibrarySections: ProjectState['fileLibrarySections'] = {
      ...base.fileLibrarySections,
      [legacyId]: { ...section, id: legacyId },
    }
    delete fileLibrarySections[originalId]
    const state = {
      ...base,
      fileAssetOrder: ['fa-1'],
      fileAssets: {
        'fa-1': makeAsset('fa-1', 'source', { sectionId: legacyId }),
      },
      fileGroupOrder: ['fg-1'],
      fileGroups: {
        'fg-1': {
          createdAt: section.createdAt,
          id: 'fg-1',
          sectionId: legacyId,
          title: 'Sources',
          updatedAt: section.updatedAt,
        },
      },
      fileLibrarySectionOrder: base.fileLibrarySectionOrder.map(
        (id) => id === originalId ? legacyId : id,
      ),
      fileLibrarySections,
    }

    const migrated = researchDeskReducer(state, {
      replacements: { [legacyId]: 'file-section-new' },
      type: 'rekeyFileLibrarySectionIds',
    })

    expect(migrated.fileLibrarySections[legacyId]).toBeUndefined()
    expect(migrated.fileLibrarySections['file-section-new']?.id).toBe('file-section-new')
    expect(migrated.fileGroups['fg-1'].sectionId).toBe('file-section-new')
    expect(migrated.fileAssets['fa-1'].sectionId).toBe('file-section-new')
    expect(migrated.fileLibrarySectionOrder).toContain('file-section-new')
  })

  it('rekeys bootstrap children without overwriting an observed canonical row', () => {
    const base = createEmptyProjectState()
    const bootstrapId = base.fileLibrarySectionOrder.find(
      (id) => base.fileLibrarySections[id]?.semanticRole === 'library',
    )!
    const canonicalId = 'server-canonical-library'
    const canonical = {
      createdAt: '2026-01-01T00:00:00.000Z',
      id: canonicalId,
      kind: 'custom' as const,
      semanticRole: 'library' as const,
      serverSynced: true,
      title: 'Server canonical',
      updatedAt: '2026-01-01T00:00:00.000Z',
    }
    const seeded = {
      ...base,
      fileAssetOrder: ['fa-bootstrap'],
      fileAssets: {
        'fa-bootstrap': makeAsset('fa-bootstrap', 'source', {
          sectionId: bootstrapId,
        }),
      },
      fileLibrarySectionOrder: [canonicalId, ...base.fileLibrarySectionOrder],
      fileLibrarySections: {
        ...base.fileLibrarySections,
        [canonicalId]: canonical,
      },
    }

    const migrated = researchDeskReducer(seeded, {
      replacements: { [bootstrapId]: canonicalId },
      type: 'rekeyFileLibrarySectionIds',
    })

    expect(migrated.fileLibrarySections[canonicalId]).toEqual(canonical)
    expect(migrated.fileAssets['fa-bootstrap'].sectionId).toBe(canonicalId)
    expect(
      migrated.fileLibrarySectionOrder.filter((id) => id === canonicalId),
    ).toHaveLength(1)
  })

  it('rekeys a knowledge session and every item, membership, selection, and pin', () => {
    const base = createEmptyProjectState()
    const originalId = base.selectedKnowledgeSessionId!
    const legacyId = 'knowledge-session-default'
    const session = base.knowledgeSessions[originalId]
    const state = {
      ...base,
      knowledgeItemOrder: ['ki-1'],
      knowledgeItems: { 'ki-1': makeKnowledgeItem('ki-1', legacyId) },
      knowledgeSessionGroupMemberships: { [legacyId]: 'kg-1' },
      knowledgeSessionOrder: [legacyId],
      knowledgeSessions: { [legacyId]: { ...session, id: legacyId } },
      selectedKnowledgeSessionId: legacyId,
      ui: {
        ...base.ui,
        pinnedExplorer: {
          ...base.ui.pinnedExplorer,
          knowledgeSessionIds: [legacyId],
        },
      },
    }

    const migrated = researchDeskReducer(state, {
      replacements: { [legacyId]: 'ks-new' },
      type: 'rekeyKnowledgeSessionIds',
    })

    expect(migrated.knowledgeSessions[legacyId]).toBeUndefined()
    expect(migrated.knowledgeSessions['ks-new']?.id).toBe('ks-new')
    expect(migrated.knowledgeItems['ki-1'].sessionId).toBe('ks-new')
    expect(migrated.knowledgeSessionGroupMemberships).toEqual({ 'ks-new': 'kg-1' })
    expect(migrated.selectedKnowledgeSessionId).toBe('ks-new')
    expect(migrated.ui.pinnedExplorer.knowledgeSessionIds).toEqual(['ks-new'])
    expect(migrated.dirty).toBe(true)
  })
})

describe('chat exchange ordering', () => {
  it('places the assistant after the user without relying on message ids', () => {
    const next = researchDeskReducer(createEmptyProjectState(), {
      assistantMessageId: 'a-before-u-lexically',
      contentMarkdown: 'Question',
      createdAt: '2026-01-03T00:00:00.000Z',
      threadId: 'thread-order',
      type: 'startChatExchange',
      userMessageId: 'z-user',
    })

    const [userMessage, assistantMessage] =
      next.chatThreads['thread-order'].messages
    expect(userMessage.role).toBe('user')
    expect(userMessage.createdAt).toBe('2026-01-03T00:00:00.000Z')
    expect(assistantMessage.role).toBe('assistant')
    expect(assistantMessage.createdAt > userMessage.createdAt).toBe(true)
  })
})

describe('chat rule reducer actions', () => {
  it('upserts legacy rules with prompt-library defaults', () => {
    const next = researchDeskReducer(createEmptyProjectState(), {
      rule: makeRule('r1', 'legacy'),
      type: 'upsertChatRule',
    })

    expect(next.chatRules.r1).toMatchObject({
      category: 'instruction',
      includeInAutocomplete: true,
      linkedContextRefs: [],
      visibility: { agent: false, chat: true, editor: true },
    })
    expect(next.chatRuleOrder).toEqual(['r1'])
  })

  it('keeps only database references on context-pack rules', () => {
    const next = researchDeskReducer(createEmptyProjectState(), {
      rule: makeRule('r1', 'profile', {
        category: 'context',
        linkedContextRefs: [
          { fileId: 'f1', kind: 'file-asset' },
          { kind: 'chat-rule', ruleId: 'nested-rule' },
          { groupId: 'g1', kind: 'file-group' },
        ],
      }),
      type: 'upsertChatRule',
    })

    expect(next.chatRules.r1.linkedContextRefs).toEqual([
      { fileId: 'f1', kind: 'file-asset' },
      { groupId: 'g1', kind: 'file-group' },
    ])
  })

  it('stores rendered context-pack content on chat attachments', () => {
    const base = createEmptyProjectState()
    const seeded = {
      ...base,
      chatRuleOrder: ['r1'],
      chatRules: {
        r1: makeRule('r1', 'profile', {
          category: 'context',
          contentMarkdown: 'Apply the profile.\n{{context}}',
          linkedContextRefs: [{ fileId: 'f1', kind: 'file-asset' }],
        }),
      },
      fileAssetOrder: ['f1'],
      fileAssets: {
        f1: makeAsset('f1', 'alpha', { extractedText: 'Original profile content.' }),
      },
    }

    const next = researchDeskReducer(seeded, {
      assistantMessageId: 'a1',
      attachmentRefs: [{ kind: 'chat-rule', ruleId: 'r1' }],
      contentMarkdown: 'Use @rules:profile',
      createdAt: '2026-01-03T00:00:00.000Z',
      threadId: 'thread-1',
      type: 'startChatExchange',
      userMessageId: 'u1',
    })

    const attachment = next.chatThreads['thread-1'].messages[0].attachments?.[0]
    expect(attachment).toMatchObject({ kind: 'chat-rule', label: 'profile' })
    expect(attachment?.contentMarkdown).toContain('Apply the profile.')
    expect(attachment?.contentMarkdown).toContain('Original profile content.')
    expect(attachment?.contentMarkdown).not.toContain('{{context}}')
  })
})

describe('reorderChatContextInDraft', () => {
  function withPending(ruleIds: string[]) {
    const base = createEmptyProjectState()
    return {
      ...base,
      ui: {
        ...base.ui,
        pendingChatAttachmentRefs: ruleIds.map((ruleId) => ({ kind: 'chat-rule' as const, ruleId })),
      },
    }
  }

  it('moves a pending ref to a new index', () => {
    const next = researchDeskReducer(withPending(['a', 'b', 'c']), {
      fromIndex: 0,
      toIndex: 2,
      type: 'reorderChatContextInDraft',
    })
    expect(next.ui.pendingChatAttachmentRefs.map((ref) => (ref as { ruleId: string }).ruleId)).toEqual(['b', 'c', 'a'])
    expect(next.dirty).toBe(true)
  })

  it('ignores no-op and out-of-bounds reorders', () => {
    const seeded = withPending(['a', 'b'])
    expect(researchDeskReducer(seeded, { fromIndex: 0, toIndex: 0, type: 'reorderChatContextInDraft' })).toBe(seeded)
    expect(researchDeskReducer(seeded, { fromIndex: 0, toIndex: 5, type: 'reorderChatContextInDraft' })).toBe(seeded)
  })
})

describe('vector index reducer actions', () => {
  function withAssets(...labels: string[]) {
    return researchDeskReducer(createEmptyProjectState(), {
      assets: labels.map((label) => makeAsset(label, label, { pageCount: 10 })),
      type: 'ingestFileAssets',
    })
  }

  it('creates an index with members as stale and an empty index as ready', () => {
    const seeded = withAssets('a', 'b')
    const withIndex = researchDeskReducer(seeded, { fileIds: ['a', 'b'], title: 'EU Recht', type: 'createVectorIndex' })
    const index = withIndex.vectorIndexes[withIndex.vectorIndexOrder[0]]
    expect(index.title).toBe('EU Recht')
    expect(index.handle).toBe('eu-recht')
    expect(index.dims).toBe(3072)
    expect(index.status).toBe('stale')
    expect(index.members.map((member) => member.fileId)).toEqual(['a', 'b'])
    expect(index.members.every((member) => member.state === 'pending')).toBe(true)

    const empty = researchDeskReducer(seeded, { fileIds: [], title: 'Empty', type: 'createVectorIndex' })
    expect(empty.vectorIndexes[empty.vectorIndexOrder[0]].status).toBe('ready')
  })

  it('assigns unique handles for duplicate titles', () => {
    let state = withAssets('a')
    state = researchDeskReducer(state, { fileIds: [], title: 'EU', type: 'createVectorIndex' })
    state = researchDeskReducer(state, { fileIds: [], title: 'EU', type: 'createVectorIndex' })
    const handles = state.vectorIndexOrder.map((id) => state.vectorIndexes[id].handle).sort()
    expect(handles).toEqual(['eu', 'eu-2'])
  })

  it('re-slugs the handle on rename', () => {
    let state = researchDeskReducer(withAssets('a'), { fileIds: ['a'], title: 'Old', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, { indexId: id, title: 'Neuer Titel', type: 'renameVectorIndex' })
    expect(state.vectorIndexes[id].title).toBe('Neuer Titel')
    expect(state.vectorIndexes[id].handle).toBe('neuer-titel')
  })

  it('adds documents as pending and marks the index stale', () => {
    let state = researchDeskReducer(withAssets('a', 'b'), { fileIds: ['a'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'demo', totalDocuments: 1, type: 'startVectorIndexReindex' })
    state = researchDeskReducer(state, { indexId: id, type: 'completeVectorIndexReindex' })
    expect(state.vectorIndexes[id].status).toBe('ready')
    state = researchDeskReducer(state, { fileIds: ['b'], indexId: id, type: 'addDocsToVectorIndex' })
    expect(state.vectorIndexes[id].status).toBe('stale')
    expect(state.vectorIndexes[id].members.find((member) => member.fileId === 'b')?.state).toBe('pending')
  })

  it('changing the model updates dims, marks stale, and resets members to pending', () => {
    let state = researchDeskReducer(withAssets('a'), { fileIds: ['a'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'demo', totalDocuments: 1, type: 'startVectorIndexReindex' })
    state = researchDeskReducer(state, { indexId: id, type: 'completeVectorIndexReindex' })
    expect(state.vectorIndexes[id].members[0].state).toBe('embedded')
    state = researchDeskReducer(state, { indexId: id, model: 'text-embedding-3-small', type: 'setVectorIndexModel' })
    expect(state.vectorIndexes[id].dims).toBe(1536)
    expect(state.vectorIndexes[id].status).toBe('stale')
    expect(state.vectorIndexes[id].members[0].state).toBe('pending')
  })

  it('reindex marks indexing then ready with embedded members; complete is a no-op otherwise', () => {
    let state = researchDeskReducer(withAssets('a', 'b'), { fileIds: ['a', 'b'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    const indexing = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'demo', totalDocuments: 2, type: 'startVectorIndexReindex' })
    expect(indexing.vectorIndexes[id].status).toBe('indexing')
    state = researchDeskReducer(indexing, { indexId: id, type: 'completeVectorIndexReindex' })
    expect(state.vectorIndexes[id].status).toBe('ready')
    expect(state.vectorIndexes[id].members.every((member) => member.state === 'embedded')).toBe(true)
    expect(researchDeskReducer(state, { indexId: id, type: 'completeVectorIndexReindex' })).toBe(state)
  })

  it('adopts a cancelled run\'s partial result so the next run resumes', () => {
    let state = researchDeskReducer(withAssets('a', 'b', 'c'), { fileIds: ['a', 'b', 'c'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'build', totalDocuments: 3, type: 'startVectorIndexReindex' })
    state = researchDeskReducer(state, {
      embeddedFileIds: ['a'],
      indexId: id,
      result: 'cancelled',
      serverCollectionId: 'kc_partial',
      serverCollectionModel: 'text-embedding-3-large',
      serverDocumentIds: { a: 'kd_a' },
      type: 'completeVectorIndexReindex',
    })
    const index = state.vectorIndexes[id]
    // What embedded is kept AND the collection is adopted — without the id the
    // next run would build a second collection instead of resuming.
    expect(index.serverCollectionId).toBe('kc_partial')
    expect(index.members.find((member) => member.fileId === 'a')?.state).toBe('embedded')
    expect(index.members.find((member) => member.fileId === 'a')?.serverDocumentId).toBe('kd_a')
    expect(index.members.filter((member) => member.state === 'pending')).toHaveLength(2)
    expect(index.status).toBe('stale')
    expect(index.history?.[0]).toMatchObject({ documents: 1, result: 'cancelled' })
    expect(state.indexingJobs[id]).toBeUndefined()
  })

  it('adopts confirmed first-build work without terminating a paused run', () => {
    let state = researchDeskReducer(
      withAssets('a', 'b', 'c'),
      { fileIds: ['a', 'b', 'c'], title: 'X', type: 'createVectorIndex' },
    )
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, {
      indexId: id,
      jobId: 'build-1',
      source: 'build',
      totalDocuments: 3,
      type: 'startVectorIndexReindex',
    })
    state = researchDeskReducer(state, {
      embeddedFileIds: ['a'],
      indexId: id,
      serverCollectionId: 'kc-partial',
      serverCollectionModel: 'text-embedding-3-small',
      serverDocumentIds: { a: 'kd-a', b: 'kd-b' },
      skippedFileIds: [],
      type: 'adoptVectorIndexPartialResult',
    })

    expect(state.vectorIndexes[id]).toMatchObject({
      serverCollectionId: 'kc-partial',
      status: 'indexing',
    })
    expect(state.vectorIndexes[id].members).toEqual([
      { fileId: 'a', serverDocumentId: 'kd-a', state: 'embedded' },
      { fileId: 'b', serverDocumentId: 'kd-b', state: 'pending' },
      { fileId: 'c', state: 'pending' },
    ])
    expect(state.vectorIndexes[id].history ?? []).toHaveLength(0)
    expect(state.indexingJobs[id].jobId).toBe('build-1')
  })

  it('only members actually in flight read as running', () => {
    let state = researchDeskReducer(withAssets('a', 'b', 'c'), { fileIds: ['a', 'b', 'c'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, { indexId: id, jobId: 'j1', runningFileIds: [], source: 'build', totalDocuments: 3, type: 'startVectorIndexReindex' })
    expect(state.indexingJobs[id].runningFileIds).toEqual([])
    // The pool picked up two members; the third is still queued.
    state = researchDeskReducer(state, {
      completedDocuments: 0,
      indexId: id,
      runningFileIds: ['a', 'b'],
      totalDocuments: 3,
      type: 'markVectorIndexProgress',
    })
    expect(state.indexingJobs[id].runningFileIds).toEqual(['a', 'b'])
    // 'a' finished, so it stops reading as running.
    state = researchDeskReducer(state, {
      completedDocuments: 1,
      embedded: true,
      fileId: 'a',
      indexId: id,
      runningFileIds: ['b'],
      totalDocuments: 3,
      type: 'markVectorIndexProgress',
    })
    expect(state.indexingJobs[id].runningFileIds).toEqual(['b'])
  })

  it('tracks queued and running server phases independently for each member', () => {
    let state = researchDeskReducer(withAssets('a', 'b', 'c'), { fileIds: ['a', 'b', 'c'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, {
      indexId: id,
      jobId: 'j1',
      queuedFileIds: ['a', 'b', 'c'],
      runningFileIds: [],
      source: 'build',
      totalDocuments: 3,
      type: 'startVectorIndexReindex',
    })
    expect(state.indexingJobs[id].memberProgress).toEqual({
      a: { status: 'queued' },
      b: { status: 'queued' },
      c: { status: 'queued' },
    })

    state = researchDeskReducer(state, {
      currentBatch: 7,
      fileId: 'a',
      indexId: id,
      phase: 'contextualization',
      status: 'running',
      totalBatches: 18,
      type: 'markVectorIndexMemberProgress',
    })
    expect(state.indexingJobs[id].memberProgress?.a).toEqual({
      currentBatch: 7,
      phase: 'contextualization',
      queuePosition: undefined,
      status: 'running',
      totalBatches: 18,
    })
    expect(state.indexingJobs[id].memberProgress?.b).toEqual({ status: 'queued' })
  })

  it('keeps every paused document with its job when a client run pauses', () => {
    // The run ends with two documents waiting and one it never reached: both
    // paused rows keep their job for "Alle fortsetzen", the unreached row
    // leaves the queue (nothing runs it until the user continues).
    let state = researchDeskReducer(withAssets('a', 'b', 'c'), { fileIds: ['a', 'b', 'c'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, {
      indexId: id,
      jobId: 'build-1',
      queuedFileIds: ['a', 'b', 'c'],
      runningFileIds: [],
      source: 'build',
      totalDocuments: 3,
      type: 'startVectorIndexReindex',
    })
    const plan: AutoResumePlan = { attempts: 0, max: 5, next_at: 2_000_000_000, stopped: null }
    for (const [fileId, status, errorType, autoResume] of [
      ['a', 'paused_validation', 'contextualization_validation_error', null],
      ['b', 'paused_dependency', 'embedding_provider_timeout', plan],
    ] as const) {
      state = researchDeskReducer(state, {
        autoResume,
        errorType,
        fileId,
        indexId: id,
        jobId: `ix_${fileId}`,
        status,
        type: 'markVectorIndexMemberProgress',
      })
    }

    state = researchDeskReducer(state, {
      indexId: id,
      status: 'paused_dependency',
      type: 'markVectorIndexRunPaused',
    })

    // A dependency-paused run names what its dependency pause waits for, not
    // the validation pause that happens to come first.
    expect(state.indexingJobs[id]).toMatchObject({
      autoResume: plan,
      pauseErrorType: 'embedding_provider_timeout',
      source: 'build',
      status: 'paused_dependency',
    })
    expect(state.indexingJobs[id].memberProgress?.a).toMatchObject({
      jobId: 'ix_a',
      status: 'paused_validation',
    })
    expect(state.indexingJobs[id].memberProgress?.b).toMatchObject({
      jobId: 'ix_b',
      status: 'paused_dependency',
    })
    expect(state.indexingJobs[id].memberProgress?.c).toBeUndefined()
    expect(state.vectorIndexes[id].status).toBe('indexing')
  })

  it('keeps the current member projection when the same durable job reattaches', () => {
    let state = researchDeskReducer(withAssets('a', 'b'), { fileIds: ['a', 'b'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, {
      indexId: id,
      jobId: 'j1',
      queuedFileIds: ['a', 'b'],
      source: 'server',
      totalDocuments: 2,
      type: 'startVectorIndexReindex',
    })
    state = researchDeskReducer(state, {
      currentBatch: 8,
      fileId: 'a',
      indexId: id,
      phase: 'contextualization',
      status: 'running',
      totalBatches: 43,
      type: 'markVectorIndexMemberProgress',
    })
    const startedAt = state.indexingJobs[id].startedAt

    state = researchDeskReducer(state, {
      indexId: id,
      jobId: 'j1',
      queuedFileIds: ['a', 'b'],
      source: 'server',
      status: 'running',
      totalDocuments: 2,
      type: 'startVectorIndexReindex',
    })

    expect(state.indexingJobs[id].startedAt).toBe(startedAt)
    expect(state.indexingJobs[id].memberProgress?.a).toEqual({
      currentBatch: 8,
      phase: 'contextualization',
      queuePosition: undefined,
      status: 'running',
      totalBatches: 43,
    })
    expect(state.indexingJobs[id].memberProgress?.b).toEqual({ status: 'queued' })
  })

  it('a progress event without an outcome never sorts a member anywhere', () => {
    let state = researchDeskReducer(withAssets('a', 'b'), { fileIds: ['a', 'b'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'build', totalDocuments: 2, type: 'startVectorIndexReindex' })
    // "Document 1 of 2 started" — a title, not a result.
    state = researchDeskReducer(state, {
      completedDocuments: 0,
      currentDocumentTitle: 'Vertrag.pdf',
      indexId: id,
      totalDocuments: 2,
      type: 'markVectorIndexProgress',
    })
    const live = state.indexingJobs[id]
    expect(live.currentDocumentTitle).toBe('Vertrag.pdf')
    expect(live.embeddedFileIds ?? []).toEqual([])
    expect(live.skippedFileIds ?? []).toEqual([])
  })

  it('marks no-text members terminal skipped and reads ready (not perpetually stale)', () => {
    let state = researchDeskReducer(withAssets('a', 'b'), { fileIds: ['a', 'b'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'build', totalDocuments: 2, type: 'startVectorIndexReindex' })
    // 'a' embedded; 'b' carried no extractable text -> terminal skipped, never embeds.
    state = researchDeskReducer(state, {
      embeddedFileIds: ['a'],
      skippedFileIds: ['b'],
      indexId: id,
      serverCollectionId: 'kc_live',
      serverCollectionModel: 'text-embedding-3-large',
      type: 'completeVectorIndexReindex',
    })
    const members = state.vectorIndexes[id].members
    expect(members.find((member) => member.fileId === 'a')?.state).toBe('embedded')
    expect(members.find((member) => member.fileId === 'b')?.state).toBe('skipped')
    // Skipped is terminal (a no-text doc can never embed), so nothing is pending
    // and the index reads ready — not a perpetual stale that prompts a futile re-index.
    expect(state.vectorIndexes[id].status).toBe('ready')
    expect(state.vectorIndexes[id].serverCollectionModel).toBe('text-embedding-3-large')
  })

  it('stays stale while a member is genuinely pending (not yet ingested, not skipped)', () => {
    let state = researchDeskReducer(withAssets('a', 'b'), { fileIds: ['a', 'b'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'build', totalDocuments: 2, type: 'startVectorIndexReindex' })
    // Only 'a' ingested this run; 'b' was neither embedded nor skipped -> still pending.
    state = researchDeskReducer(state, {
      embeddedFileIds: ['a'],
      indexId: id,
      serverCollectionId: 'kc_live',
      type: 'completeVectorIndexReindex',
    })
    const members = state.vectorIndexes[id].members
    expect(members.find((member) => member.fileId === 'b')?.state).toBe('pending')
    expect(state.vectorIndexes[id].status).toBe('stale')
  })

  it('keeps a previously-skipped member skipped across a later run that does not touch it', () => {
    let state = researchDeskReducer(withAssets('a', 'b'), { fileIds: ['a', 'b'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'build', totalDocuments: 2, type: 'startVectorIndexReindex' })
    state = researchDeskReducer(state, { embeddedFileIds: ['a'], skippedFileIds: ['b'], indexId: id, serverCollectionId: 'kc', type: 'completeVectorIndexReindex' })
    // A later incremental run touches only 'a' (b is neither embedded nor skipped
    // THIS run). 'b' must NOT silently revert to pending — skipped is terminal.
    state = researchDeskReducer(state, { indexId: id, jobId: 'j2', source: 'build', totalDocuments: 1, type: 'startVectorIndexReindex' })
    state = researchDeskReducer(state, { embeddedFileIds: ['a'], indexId: id, serverCollectionId: 'kc', type: 'completeVectorIndexReindex' })
    expect(state.vectorIndexes[id].members.find((member) => member.fileId === 'b')?.state).toBe('skipped')
    expect(state.vectorIndexes[id].status).toBe('ready')
  })

  it('marks the complete embedded set ready when every member ingested', () => {
    let state = researchDeskReducer(withAssets('a', 'b'), { fileIds: ['a', 'b'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'build', totalDocuments: 2, type: 'startVectorIndexReindex' })
    state = researchDeskReducer(state, {
      embeddedFileIds: ['a', 'b'],
      indexId: id,
      serverCollectionId: 'kc_live',
      serverCollectionModel: 'text-embedding-3-large',
      type: 'completeVectorIndexReindex',
    })
    expect(state.vectorIndexes[id].members.every((member) => member.state === 'embedded')).toBe(true)
    expect(state.vectorIndexes[id].status).toBe('ready')
  })

  it('markVectorIndexProgress advances the bar and records per-file outcomes live', () => {
    let state = researchDeskReducer(withAssets('a', 'b'), { fileIds: ['a', 'b'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'build', totalDocuments: 2, type: 'startVectorIndexReindex' })
    state = researchDeskReducer(state, {
      completedDocuments: 1, embedded: true, fileId: 'a', indexId: id,
      totalDocuments: 2, type: 'markVectorIndexProgress',
    })
    state = researchDeskReducer(state, {
      completedDocuments: 2, embedded: false, fileId: 'b', indexId: id,
      totalDocuments: 2, type: 'markVectorIndexProgress',
    })
    const live = state.indexingJobs[id]
    expect(live.completedDocuments).toBe(2)
    expect(live.percent).toBe(100)
    expect(live.embeddedFileIds).toEqual(['a'])
    expect(live.skippedFileIds).toEqual(['b'])
  })

  it('persists each member server-document id on completion (for later removal)', () => {
    let state = researchDeskReducer(withAssets('a', 'b'), { fileIds: ['a', 'b'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'build', totalDocuments: 2, type: 'startVectorIndexReindex' })
    state = researchDeskReducer(state, {
      embeddedFileIds: ['a', 'b'],
      indexId: id,
      serverCollectionId: 'kc_live',
      serverCollectionModel: 'text-embedding-3-large',
      serverDocumentIds: { a: 'kd_a', b: 'kd_b' },
      type: 'completeVectorIndexReindex',
    })
    const members = state.vectorIndexes[id].members
    expect(members.find((member) => member.fileId === 'a')?.serverDocumentId).toBe('kd_a')
    expect(members.find((member) => member.fileId === 'b')?.serverDocumentId).toBe('kd_b')
  })

  it('persists a source-reconciled legacy member id without removing the member', () => {
    let state = researchDeskReducer(
      withAssets('a'),
      { fileIds: ['a'], title: 'X', type: 'createVectorIndex' },
    )
    const id = state.vectorIndexOrder[0]

    state = researchDeskReducer(state, {
      fileId: 'a',
      indexId: id,
      serverDocumentId: 'kd_reconciled',
      type: 'reconcileVectorIndexMemberDocument',
    })

    expect(state.vectorIndexes[id].members).toEqual([{
      fileId: 'a',
      serverDocumentId: 'kd_reconciled',
      state: 'pending',
    }])
  })

  it('removing the last member returns the index to ready', () => {
    let state = researchDeskReducer(withAssets('a'), { fileIds: ['a'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    expect(state.vectorIndexes[id].status).toBe('stale')
    state = researchDeskReducer(state, { fileId: 'a', indexId: id, type: 'removeDocFromVectorIndex' })
    expect(state.vectorIndexes[id].members).toHaveLength(0)
    expect(state.vectorIndexes[id].status).toBe('ready')
  })

  it('accepts a server-catalog model and explicit dims at creation and model change', () => {
    let state = researchDeskReducer(withAssets('a'), {
      dims: 1024,
      fileIds: ['a'],
      model: 'BAAI/bge-m3',
      title: 'Server',
      type: 'createVectorIndex',
    })
    const id = state.vectorIndexOrder[0]
    expect(state.vectorIndexes[id].model).toBe('BAAI/bge-m3')
    expect(state.vectorIndexes[id].dims).toBe(1024)

    state = researchDeskReducer(state, {
      dims: 3072,
      indexId: id,
      model: 'custom/server-model',
      type: 'setVectorIndexModel',
    })
    expect(state.vectorIndexes[id].model).toBe('custom/server-model')
    expect(state.vectorIndexes[id].dims).toBe(3072)
  })

  it('stores the server collection id on completion and clears errors on retry', () => {
    let state = researchDeskReducer(withAssets('a'), { fileIds: ['a'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]

    state = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'demo', totalDocuments: 1, type: 'startVectorIndexReindex' })
    state = researchDeskReducer(state, {
      indexId: id,
      serverCollectionId: 'kc_live',
      type: 'completeVectorIndexReindex',
    })
    expect(state.vectorIndexes[id].serverCollectionId).toBe('kc_live')
    expect(state.vectorIndexes[id].status).toBe('ready')

    // A terminal error only lands while a run is actually in flight.
    state = researchDeskReducer(state, { indexId: id, jobId: 'j2', source: 'server', totalDocuments: 1, type: 'startVectorIndexReindex' })
    state = researchDeskReducer(state, {
      indexId: id,
      message: 'embedding backend down',
      serverCollectionId: 'kc_cleanup_pending',
      serverCollectionModel: 'text-embedding-3-small',
      type: 'markVectorIndexError',
    })
    expect(state.vectorIndexes[id].status).toBe('error')
    expect(state.vectorIndexes[id].lastError).toBe('embedding backend down')
    expect(state.vectorIndexes[id].serverCollectionId).toBe('kc_cleanup_pending')
    expect(state.vectorIndexes[id].serverCollectionModel).toBe('text-embedding-3-small')

    state = researchDeskReducer(state, { indexId: id, jobId: 'j3', source: 'server', totalDocuments: 1, type: 'startVectorIndexReindex' })
    expect(state.vectorIndexes[id].status).toBe('indexing')
    expect(state.vectorIndexes[id].lastError).toBeNull()
    state = researchDeskReducer(state, { indexId: id, type: 'completeVectorIndexReindex' })
    expect(state.vectorIndexes[id].serverCollectionId).toBe('kc_cleanup_pending')
  })

  it('deleting a file drops it from every index membership', () => {
    let state = researchDeskReducer(withAssets('a', 'b'), { fileIds: ['a', 'b'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, { fileId: 'a', type: 'deleteFileAsset' })
    expect(state.fileAssets.a).toBeUndefined()
    expect(state.vectorIndexes[id].members.map((member) => member.fileId)).toEqual(['b'])
  })

  it('applies terminal aggregate deletion only to the exact owned lifecycle', () => {
    const seeded = withAssets('a')
    seeded.fileAssets.a = { ...seeded.fileAssets.a, serverSynced: true }

    const unownedTerminal = researchDeskReducer(seeded, {
      fileIds: ['a'],
      operationId: 'del-old',
      type: 'completeFileAssetDeletion',
    })
    expect(unownedTerminal).toBe(seeded)

    const deleting = researchDeskReducer(seeded, {
      error: null,
      fileIds: ['a'],
      operationId: 'del-current',
      stage: 'search_detached',
      status: 'running',
      type: 'setFileAssetDeletionState',
    })
    expect(deleting.fileAssets.a).toMatchObject({
      deletionOperationId: 'del-current',
      lifecycleStatus: 'deleting',
    })
    expect(researchDeskReducer(deleting, {
      fileIds: ['a'],
      operationId: 'del-old',
      type: 'completeFileAssetDeletion',
    })).toBe(deleting)

    const completed = researchDeskReducer(deleting, {
      fileIds: ['a'],
      operationId: 'del-current',
      type: 'completeFileAssetDeletion',
    })
    expect(completed.fileAssets.a).toBeUndefined()
  })

  it('does not let an old deletion receipt capture a local-only replacement', () => {
    const replacement = withAssets('a')
    replacement.fileAssets.a = {
      ...replacement.fileAssets.a,
      createdAt: '2026-01-03T00:00:00.000Z',
      serverSynced: false,
    }

    const projected = researchDeskReducer(replacement, {
      error: null,
      fileIds: ['a'],
      operationId: 'del-old',
      stage: 'deleted',
      status: 'running',
      type: 'setFileAssetDeletionState',
    })
    expect(projected).toBe(replacement)
    expect(researchDeskReducer(projected, {
      fileIds: ['a'],
      operationId: 'del-old',
      type: 'completeFileAssetDeletion',
    })).toBe(replacement)
  })

  it('deleting a custom section removes its assets and their index memberships', () => {
    let state = researchDeskReducer(createEmptyProjectState(), { sectionId: '', title: 'Custom', type: 'createFileLibrarySection' })
    const sectionId = state.fileLibrarySectionOrder[state.fileLibrarySectionOrder.length - 1]
    state = researchDeskReducer(state, { assets: [makeAsset('a', 'a', { sectionId })], type: 'ingestFileAssets' })
    state = researchDeskReducer(state, { fileIds: ['a'], title: 'X', type: 'createVectorIndex' })
    const id = state.vectorIndexOrder[0]
    state = researchDeskReducer(state, { sectionId, type: 'deleteFileLibrarySection' })
    expect(state.fileLibrarySections[sectionId]).toBeUndefined()
    expect(state.fileAssets.a).toBeUndefined()
    expect(state.vectorIndexes[id].members).toHaveLength(0)
  })

  it('keeps an empty section deletion addressable until its exact operation completes', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      sectionId: '',
      title: 'Custom',
      type: 'createFileLibrarySection',
    })
    const sectionId = state.fileLibrarySectionOrder[state.fileLibrarySectionOrder.length - 1]
    state.fileLibrarySections[sectionId] = {
      ...state.fileLibrarySections[sectionId],
      serverSynced: true,
    }
    state = researchDeskReducer(state, {
      error: null,
      operationId: 'del-section',
      sectionId,
      stage: 'queued',
      status: 'queued',
      type: 'setFileLibrarySectionDeletionState',
    })
    expect(state.fileLibrarySections[sectionId]).toMatchObject({
      deletionOperationId: 'del-section',
      lifecycleStatus: 'deleting',
    })
    expect(researchDeskReducer(state, {
      operationId: 'del-stale',
      sectionId,
      type: 'completeFileLibrarySectionDeletion',
    })).toBe(state)

    state = researchDeskReducer(state, {
      operationId: 'del-section',
      sectionId,
      type: 'completeFileLibrarySectionDeletion',
    })
    expect(state.fileLibrarySections[sectionId]).toBeUndefined()
  })

  it('refuses to delete the temporary section', () => {
    const state = createEmptyProjectState()
    const temporarySectionId = state.fileLibrarySectionOrder.find(
      (id) => state.fileLibrarySections[id]?.kind === 'temporary',
    )
    expect(temporarySectionId).toBeDefined()
    expect(researchDeskReducer(state, {
      sectionId: temporarySectionId!,
      type: 'deleteFileLibrarySection',
    })).toBe(state)
  })
})

describe('live indexing-job lifecycle', () => {
  function withIndex(...fileIds: string[]) {
    const seeded = researchDeskReducer(createEmptyProjectState(), {
      assets: fileIds.map((id) => makeAsset(id, id, { pageCount: 10 })),
      type: 'ingestFileAssets',
    })
    const state = researchDeskReducer(seeded, { fileIds, title: 'X', type: 'createVectorIndex' })
    return { id: state.vectorIndexOrder[0], state }
  }

  it('starting a reindex opens a live job and flips the index to indexing', () => {
    const { id, state } = withIndex('a', 'b')
    const started = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'server', totalDocuments: 2, type: 'startVectorIndexReindex' })
    expect(started.vectorIndexes[id].status).toBe('indexing')
    expect(started.indexingJobs[id]).toMatchObject({ completedDocuments: 0, jobId: 'j1', percent: 0, source: 'server', totalDocuments: 2 })
  })

  it('defaults runningFileIds to every member when the action omits it (server re-embed)', () => {
    // The durable server re-embed does not pass a working set — it re-vectorizes
    // the whole collection, so every member must be in the run.
    const { id, state } = withIndex('a', 'b')
    const started = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'server', totalDocuments: 2, type: 'startVectorIndexReindex' })
    expect([...started.indexingJobs[id].runningFileIds].sort()).toEqual(['a', 'b'])
  })

  it('uses the explicit runningFileIds subset (incremental add) so only those files run', () => {
    // Indexing one new document must NOT pull the already-embedded members into
    // the run — only the passed subset is the working set.
    const { id, state } = withIndex('a', 'b')
    const started = researchDeskReducer(state, { indexId: id, jobId: 'j1', runningFileIds: ['b'], source: 'build', totalDocuments: 1, type: 'startVectorIndexReindex' })
    expect(started.indexingJobs[id].runningFileIds).toEqual(['b'])
  })

  it('markVectorIndexDocumentEmbedded maps a server document id to its file and flips it live', () => {
    // The durable server re-embed confirms documents by backend id; the reducer
    // resolves it to the local file via the member's serverDocumentId.
    const { id, state } = withIndex('a', 'b')
    const model = state.vectorIndexes[id].model
    let next = researchDeskReducer(state, { indexId: id, jobId: 'j0', source: 'build', totalDocuments: 2, type: 'startVectorIndexReindex' })
    // A completed build assigns each member its backend document id.
    next = researchDeskReducer(next, {
      embeddedFileIds: ['a', 'b'],
      indexId: id,
      serverCollectionId: 'col-1',
      serverCollectionModel: model,
      serverDocumentIds: { a: 'kd_a', b: 'kd_b' },
      type: 'completeVectorIndexReindex',
    })
    next = researchDeskReducer(next, { indexId: id, jobId: 'j1', source: 'server', totalDocuments: 2, type: 'startVectorIndexReindex' })
    const clean = { ...next, dirty: false }
    const flipped = researchDeskReducer(clean, { indexId: id, serverDocumentId: 'kd_a', type: 'markVectorIndexDocumentEmbedded' })
    // Only file 'a' flips; the project is not dirtied (ephemeral live state).
    expect(flipped.dirty).toBe(false)
    expect(flipped.indexingJobs[id].embeddedFileIds).toContain('a')
    expect(flipped.indexingJobs[id].embeddedFileIds ?? []).not.toContain('b')
  })

  it('markVectorIndexDocumentEmbedded ignores an unknown server document id (older index)', () => {
    // A member without a tracked serverDocumentId cannot be mapped → no-op; the
    // row flips at completion instead. State is returned unchanged.
    const { id, state } = withIndex('a')
    const started = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'server', totalDocuments: 1, type: 'startVectorIndexReindex' })
    const same = researchDeskReducer(started, { indexId: id, serverDocumentId: 'kd_unknown', type: 'markVectorIndexDocumentEmbedded' })
    expect(same).toBe(started)
  })

  it('progress updates the live job without dirtying the project', () => {
    const { id, state } = withIndex('a', 'b')
    const started = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'server', totalDocuments: 2, type: 'startVectorIndexReindex' })
    // Streaming progress is the hot path: it must never mark the project dirty.
    const clean = { ...started, dirty: false }
    const progressed = researchDeskReducer(clean, {
      completedDocuments: 1,
      currentDocumentTitle: 'doc-a',
      indexId: id,
      totalDocuments: 2,
      type: 'markVectorIndexProgress',
    })
    expect(progressed.dirty).toBe(false)
    expect(progressed.indexingJobs[id].percent).toBe(50)
    expect(progressed.indexingJobs[id].currentDocumentTitle).toBe('doc-a')
  })

  it('queued sets the FIFO position (dirty-free) and progress clears it', () => {
    const { id, state } = withIndex('a', 'b')
    const started = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'server', totalDocuments: 2, type: 'startVectorIndexReindex' })
    const clean = { ...started, dirty: false }
    const queued = researchDeskReducer(clean, { indexId: id, queuePosition: 3, type: 'markVectorIndexQueued' })
    expect(queued.dirty).toBe(false)
    expect(queued.indexingJobs[id].queuePosition).toBe(3)
    // A progress tick means a slot freed up → leave the waiting state.
    const running = researchDeskReducer(queued, { completedDocuments: 1, indexId: id, totalDocuments: 2, type: 'markVectorIndexProgress' })
    expect(running.indexingJobs[id].queuePosition).toBeNull()
  })

  it('keeps a paused server job checkpointed until explicit resume or supersession', () => {
    const { id, state } = withIndex('a', 'b')
    const started = researchDeskReducer(state, {
      indexId: id,
      jobId: 'j1',
      source: 'server',
      totalDocuments: 2,
      type: 'startVectorIndexReindex',
    })
    const clean = { ...started, dirty: false }
    const plan: AutoResumePlan = { attempts: 0, max: 5, next_at: 2_000_000_000, stopped: null }
    const paused = researchDeskReducer(clean, {
      autoResume: plan,
      completedDocuments: 1,
      currentBatch: 7,
      errorType: 'contextualization_provider_timeout',
      indexId: id,
      phase: 'contextualize',
      status: 'paused_dependency',
      totalBatches: 18,
      totalDocuments: 2,
      type: 'markVectorIndexPaused',
    })

    expect(paused.dirty).toBe(false)
    expect(paused.vectorIndexes[id].status).toBe('indexing')
    expect(paused.vectorIndexes[id].history ?? []).toHaveLength(0)
    expect(paused.indexingJobs[id]).toMatchObject({
      autoResume: plan,
      completedDocuments: 1,
      currentBatch: 7,
      pauseErrorType: 'contextualization_provider_timeout',
      phase: 'contextualize',
      status: 'paused_dependency',
      totalBatches: 18,
    })
    expect(paused.indexingJobs[id].memberProgress?.a).toMatchObject({
      currentBatch: 7,
      phase: 'contextualize',
      status: 'paused_dependency',
      totalBatches: 18,
    })

    const resumed = researchDeskReducer(paused, {
      indexId: id,
      totalDocuments: 2,
      type: 'markVectorIndexResumed',
    })
    expect(resumed.indexingJobs[id]).toMatchObject({ status: 'running' })
    expect(resumed.indexingJobs[id].pauseErrorType).toBeUndefined()
    expect(resumed.indexingJobs[id].autoResume).toBeUndefined()
    expect(resumed.indexingJobs[id].currentBatch).toBeUndefined()
    expect(resumed.indexingJobs[id].memberProgress?.a).toMatchObject({
      status: 'running',
    })

    const superseded = researchDeskReducer(resumed, {
      indexId: id,
      type: 'markVectorIndexSuperseded',
    })
    expect(superseded.indexingJobs[id]).toBeUndefined()
    expect(superseded.vectorIndexes[id].status).toBe('stale')
    expect(superseded.vectorIndexes[id].history ?? []).toHaveLength(0)
  })

  it('pauses every row a server rebuild works on, even without a working set', () => {
    // A rebuild of the whole index starts with no working set; its rows run
    // from document progress and must not read "running" while it waits.
    const { id, state } = withIndex('a', 'b')
    const started = researchDeskReducer(state, {
      indexId: id,
      jobId: 'j1',
      runningFileIds: [],
      source: 'server',
      totalDocuments: 2,
      type: 'startVectorIndexReindex',
    })
    const embedding = researchDeskReducer(started, {
      currentBatch: 1,
      fileId: 'a',
      indexId: id,
      phase: 'embedding',
      status: 'running',
      totalBatches: 1,
      type: 'markVectorIndexMemberProgress',
    })
    const paused = researchDeskReducer(embedding, {
      autoResume: null,
      completedDocuments: 0,
      currentBatch: 1,
      errorType: 'vector_store_unavailable',
      indexId: id,
      phase: 'embedding',
      status: 'paused_dependency',
      totalBatches: 1,
      totalDocuments: 2,
      type: 'markVectorIndexPaused',
    })

    expect(paused.indexingJobs[id].memberProgress?.a?.status).toBe('paused_dependency')
  })

  it('a paused run of several documents names the soonest automatic resume', () => {
    const { id, state } = withIndex('a', 'b')
    const started = researchDeskReducer(state, {
      indexId: id,
      jobId: 'paused-c1',
      queuedFileIds: [],
      runningFileIds: [],
      source: 'build',
      status: 'paused_dependency',
      totalDocuments: 2,
      type: 'startVectorIndexReindex',
    })
    const later: AutoResumePlan = { attempts: 0, max: 5, next_at: 2_000_000_240, stopped: null }
    const sooner: AutoResumePlan = { attempts: 2, max: 5, next_at: 2_000_000_030, stopped: null }
    const withRows = [['a', later], ['b', sooner]].reduce(
      (current, [fileId, plan]) => researchDeskReducer(current, {
        autoResume: plan as AutoResumePlan,
        errorType: 'embedding_provider_timeout',
        fileId: fileId as string,
        indexId: id,
        jobId: `ix-${fileId as string}`,
        status: 'paused_dependency',
        type: 'markVectorIndexMemberProgress',
      }),
      started,
    )
    const paused = researchDeskReducer(withRows, {
      indexId: id,
      status: 'paused_dependency',
      type: 'markVectorIndexRunPaused',
    })

    expect(paused.indexingJobs[id]).toMatchObject({
      autoResume: sooner,
      pauseErrorType: 'embedding_provider_timeout',
      status: 'paused_dependency',
    })
  })

  it('completion records an ok history entry and clears the live job', () => {
    const { id, state } = withIndex('a', 'b')
    let next = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'server', totalDocuments: 2, type: 'startVectorIndexReindex' })
    next = researchDeskReducer(next, { indexId: id, type: 'completeVectorIndexReindex' })
    expect(next.indexingJobs[id]).toBeUndefined()
    const history = next.vectorIndexes[id].history ?? []
    expect(history).toHaveLength(1)
    expect(history[0]).toMatchObject({ documents: 2, result: 'ok' })
  })

  it('an error records an error entry with the message and clears the live job', () => {
    const { id, state } = withIndex('a', 'b')
    let next = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'server', totalDocuments: 2, type: 'startVectorIndexReindex' })
    next = researchDeskReducer(next, { completedDocuments: 1, indexId: id, totalDocuments: 2, type: 'markVectorIndexProgress' })
    next = researchDeskReducer(next, { indexId: id, message: 'embedding backend down', type: 'markVectorIndexError' })
    expect(next.indexingJobs[id]).toBeUndefined()
    expect(next.vectorIndexes[id].status).toBe('error')
    const entry = (next.vectorIndexes[id].history ?? [])[0]
    expect(entry).toMatchObject({ documents: 1, error: 'embedding backend down', result: 'error' })
  })

  it('cancellation records a cancelled entry, clears the job, and restores stale when members are still pending', () => {
    const { id, state } = withIndex('a', 'b')
    let next = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'demo', totalDocuments: 2, type: 'startVectorIndexReindex' })
    next = researchDeskReducer(next, { indexId: id, type: 'markVectorIndexCancelled' })
    expect(next.indexingJobs[id]).toBeUndefined()
    expect(next.vectorIndexes[id].status).toBe('stale')
    expect((next.vectorIndexes[id].history ?? [])[0]).toMatchObject({ result: 'cancelled' })
  })

  it('cancellation restores ready when every member is already embedded', () => {
    const { id, state } = withIndex('a')
    let next = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'demo', totalDocuments: 1, type: 'startVectorIndexReindex' })
    next = researchDeskReducer(next, { indexId: id, type: 'completeVectorIndexReindex' })
    next = researchDeskReducer(next, { indexId: id, jobId: 'j2', source: 'demo', totalDocuments: 1, type: 'startVectorIndexReindex' })
    next = researchDeskReducer(next, { indexId: id, type: 'markVectorIndexCancelled' })
    expect(next.vectorIndexes[id].status).toBe('ready')
  })

  it('a late terminal callback after a finished run is a no-op (no resurrected run, no garbage history)', () => {
    const { id, state } = withIndex('a', 'b')
    let next = researchDeskReducer(state, { indexId: id, jobId: 'j1', source: 'server', totalDocuments: 2, type: 'startVectorIndexReindex' })
    next = researchDeskReducer(next, { indexId: id, type: 'completeVectorIndexReindex' })
    const settled = next
    // A duplicate error/cancel arriving after completion (resume race) must
    // not flip the finished index nor append a second history entry.
    next = researchDeskReducer(next, { indexId: id, message: 'late failure', type: 'markVectorIndexError' })
    next = researchDeskReducer(next, { indexId: id, type: 'markVectorIndexCancelled' })
    expect(next).toBe(settled)
    expect(next.vectorIndexes[id].status).toBe('ready')
    expect(next.vectorIndexes[id].history ?? []).toHaveLength(1)
  })
})

describe('editor document revision = server base (CAS contract)', () => {
  it('does NOT bump revision on rename/move; only updatedAt moves', () => {
    const base = {
      ...createEmptyProjectState(),
      editorDocumentOrder: ['doc-1'],
      editorDocuments: { 'doc-1': makeEditorDocument('doc-1', 'Doc') },
    }
    // revision is the last-synced server base and stays put across local
    // edits; only updatedAt moves so the debounced autosave fires. The save
    // sends base+1 and the store CAS is against the base — a per-edit bump
    // would be the old, race-prone counter.
    const renamed = researchDeskReducer(base, {
      documentId: 'doc-1',
      title: 'Neuer Titel',
      type: 'renameEditorDocument',
    })
    expect(renamed.editorDocuments['doc-1'].revision).toBe(1)
    expect(renamed.editorDocuments['doc-1'].updatedAt).not.toBe(
      base.editorDocuments['doc-1'].updatedAt,
    )
    expect(renamed.dirty).toBe(true)

    const moved = researchDeskReducer(renamed, {
      documentId: 'doc-1',
      folderId: 'edf_x',
      targetIndex: 0,
      type: 'moveEditorDocumentToFolder',
    })
    expect(moved.editorDocuments['doc-1'].revision).toBe(1)
  })

  it('leaves revision put on documents orphaned by a folder delete', () => {
    const base = {
      ...createEmptyProjectState(),
      editorDocumentOrder: ['doc-1'],
      editorDocuments: {
        'doc-1': makeEditorDocument('doc-1', 'Doc', { folderId: 'edf_1' }),
      },
      editorFolders: {
        edf_1: {
          createdAt: '2026-01-01T00:00:00.000Z',
          id: 'edf_1',
          title: 'Ordner',
          updatedAt: '2026-01-01T00:00:00.000Z',
        },
      },
      editorFolderOrder: ['edf_1'],
    }
    const next = researchDeskReducer(base, {
      folderId: 'edf_1',
      type: 'deleteEditorFolder',
    })
    expect(next.editorDocuments['doc-1'].folderId).toBeNull()
    expect(next.editorDocuments['doc-1'].revision).toBe(1)
  })

  it('adopts the server revision after a successful save (base tracks server)', () => {
    const base = {
      ...createEmptyProjectState(),
      editorDocumentOrder: ['doc-1'],
      editorDocuments: { 'doc-1': makeEditorDocument('doc-1', 'Doc') },
    }
    // A save based on base 1 advanced the server to 2; adopt it, revision-only.
    const adopted = researchDeskReducer(base, {
      documentId: 'doc-1',
      revision: 2,
      type: 'adoptEditorDocumentRevision',
    })
    expect(adopted.editorDocuments['doc-1'].revision).toBe(2)
    expect(adopted.editorDocuments['doc-1'].updatedAt).toBe(
      base.editorDocuments['doc-1'].updatedAt,
    )
    expect(adopted.dirty).toBe(base.dirty)
    // A late/duplicate adopt must never rewind a fresher base.
    const noop = researchDeskReducer(adopted, {
      documentId: 'doc-1',
      revision: 1,
      type: 'adoptEditorDocumentRevision',
    })
    expect(noop).toBe(adopted)
  })

  it('rebase (no live edit) adopts the server base, keeps updatedAt', () => {
    const base = {
      ...createEmptyProjectState(),
      editorDocumentOrder: ['doc-1'],
      editorDocuments: { 'doc-1': makeEditorDocument('doc-1', 'Doc') },
    }
    const rebased = researchDeskReducer(base, {
      contentMarkdown: 'server body',
      documentId: 'doc-1',
      pushedContentMarkdown: base.editorDocuments['doc-1'].contentMarkdown,
      revision: 7,
      type: 'rebaseServerEditorDocument',
    })
    expect(rebased.editorDocuments['doc-1'].revision).toBe(7)
    expect(rebased.editorDocuments['doc-1'].contentMarkdown).toBe('server body')
    expect(rebased.editorDocuments['doc-1'].updatedAt).toBe(
      base.editorDocuments['doc-1'].updatedAt,
    )
  })

  it('rebase keeps a live keystroke and adopts the server base (no silent loss)', () => {
    const base = {
      ...createEmptyProjectState(),
      editorDocumentOrder: ['doc-1'],
      editorDocuments: { 'doc-1': makeEditorDocument('doc-1', 'Doc') },
    }
    // The flush PUT "# Doc"; during the PUT->GET window the user typed.
    const typed = researchDeskReducer(base, {
      documentId: 'doc-1',
      contentMarkdown: '# Doc + live edit',
      type: 'updateEditorDocumentMarkdown',
    })
    const rebased = researchDeskReducer(typed, {
      contentMarkdown: 'server body from agent patch',
      documentId: 'doc-1',
      pushedContentMarkdown: '# Doc', // what the failed PUT carried
      revision: 7,
      type: 'rebaseServerEditorDocument',
    })
    // The live keystroke is NOT overwritten by the server body…
    expect(rebased.editorDocuments['doc-1'].contentMarkdown).toBe(
      '# Doc + live edit',
    )
    // …and revision rebases onto the server BASE (7); the next flush re-pushes
    // the live edit as base+1 (8), staying dirty. No silent loss.
    expect(rebased.editorDocuments['doc-1'].revision).toBe(7)
    expect(rebased.dirty).toBe(true)
  })
})

describe('agent canvas is reconciled on session switch', () => {
  it('clears canvas tabs when a different session is selected', () => {
    const base = createEmptyProjectState()
    const withSessions = {
      ...base,
      agentSessions: {
        's1': {
          id: 's1', title: 'A', groupId: null, createdAt: '', updatedAt: '', runIds: [],
          sourcePolicy: { web: 'available' as const, knowledge: 'available' as const },
        },
        's2': {
          id: 's2', title: 'B', groupId: null, createdAt: '', updatedAt: '', runIds: [],
          sourcePolicy: { web: 'available' as const, knowledge: 'available' as const },
        },
      },
      selectedAgentSessionId: 's1',
    }
    // Open a pinned plan tab in session s1.
    const withTab = researchDeskReducer(withSessions, {
      descriptor: { runId: 'r1', view: 'plan' },
      source: 'user',
      type: 'openAgentCanvasView',
    })
    expect(withTab.agentCanvas.tabs).toHaveLength(1)
    expect(withTab.agentCanvas.open).toBe(true)

    // Switching to s2 must clear the canvas so s1's tab does not leak.
    const switched = researchDeskReducer(withTab, {
      sessionId: 's2',
      type: 'selectAgentSession',
    })
    expect(switched.selectedAgentSessionId).toBe('s2')
    expect(switched.agentCanvas.tabs).toEqual([])
    expect(switched.agentCanvas.open).toBe(false)
    expect(switched.agentCanvas.activeTabId).toBeNull()

    // Re-selecting the SAME session is a no-op (canvas preserved).
    const withTabAgain = researchDeskReducer(switched, {
      descriptor: { runId: 'r2', view: 'run' },
      source: 'user',
      type: 'openAgentCanvasView',
    })
    const same = researchDeskReducer(withTabAgain, {
      sessionId: 's2',
      type: 'selectAgentSession',
    })
    expect(same).toBe(withTabAgain)
  })

  it('clears canvas when creating (auto-selecting) a new session', () => {
    const base = { ...createEmptyProjectState(), selectedAgentSessionId: 's1' }
    const withTab = researchDeskReducer(base, {
      descriptor: { runId: 'r1', view: 'plan' },
      source: 'user',
      type: 'openAgentCanvasView',
    })
    expect(withTab.agentCanvas.tabs).toHaveLength(1)
    const created = researchDeskReducer(withTab, {
      session: {
        id: 's2',
        title: 'New',
        groupId: null,
        createdAt: '',
        updatedAt: '',
        runIds: [],
        sourcePolicy: { web: 'available', knowledge: 'available' },
      },
      type: 'createAgentSession',
    })
    expect(created.selectedAgentSessionId).toBe('s2')
    expect(created.agentCanvas.tabs).toEqual([])
  })

  it('clears canvas when a select=true run summary switches sessions', () => {
    const base = { ...createEmptyProjectState(), selectedAgentSessionId: 's1' }
    const withTab = researchDeskReducer(base, {
      descriptor: { runId: 'r1', view: 'plan' },
      source: 'user',
      type: 'openAgentCanvasView',
    })
    const summary: ResearchRunSummary = {
      access: { mode: 'owner' },
      run_id: 'r-new',
      status: 'running',
      queue_position: null,
      question: 'Neue Analyse',
      stack: 'default',
      mode: 'workspace_agent',
      kind: 'agent',
      session_id: 's-other',
      agent_overrides: {},
      created_at: 1_700_000_000,
      started_at: 1_700_000_000,
      finished_at: null,
      elapsed_seconds: null,
      snapshot: {},
      error: null,
      events_url: '/v1/runs/r-new/events',
      result_url: '/v1/runs/r-new/result',
    }
    const upserted = researchDeskReducer(withTab, {
      select: true,
      summary,
      type: 'upsertAgentRunSummary',
    })
    expect(upserted.selectedAgentSessionId).toBe('s-other')
    expect(upserted.agentCanvas.tabs).toEqual([])
  })
})

describe('agent plan stale re-flag (plan tab on-open fetch)', () => {
  const summary: ResearchRunSummary = {
    access: { mode: 'owner' },
    run_id: 'r-plan',
    status: 'waiting_for_approval',
    queue_position: null,
    question: 'Planlauf',
    stack: 'default',
    mode: 'workspace_agent',
    kind: 'agent',
    session_id: 's-plan',
    agent_overrides: {},
    created_at: 1_700_000_000,
    started_at: 1_700_000_000,
    finished_at: null,
    elapsed_seconds: null,
    snapshot: {},
    error: null,
    events_url: '/v1/runs/r-plan/events',
    result_url: '/v1/runs/r-plan/result',
  }

  it('re-flags a cleared plan as stale without dirtying the project', () => {
    const base = researchDeskReducer(createEmptyProjectState(), {
      summary,
      type: 'upsertAgentRunSummary',
    })
    // The 404 path clears the flag with no content (pre-planning).
    const cleared = researchDeskReducer(base, {
      plan: null,
      runId: 'r-plan',
      type: 'setAgentRunPlan',
    })
    expect(cleared.agentRuns['r-plan'].planStale).toBe(false)
    expect(cleared.agentRuns['r-plan'].plan).toBeUndefined()
    const reflagged = researchDeskReducer(cleared, {
      runId: 'r-plan',
      type: 'markAgentRunPlanStale',
    })
    expect(reflagged.agentRuns['r-plan'].planStale).toBe(true)
    expect(reflagged.dirty).toBe(false)
  })

  it('is an identity no-op while the flag is already set', () => {
    const base = researchDeskReducer(createEmptyProjectState(), {
      summary,
      type: 'upsertAgentRunSummary',
    })
    expect(base.agentRuns['r-plan'].planStale).toBe(true)
    const again = researchDeskReducer(base, {
      runId: 'r-plan',
      type: 'markAgentRunPlanStale',
    })
    expect(again).toBe(base)
  })
})

describe('gate-tray approvals reconcile with exclusive membership', () => {
  const agentSummary = (
    overrides: Partial<ResearchRunSummary> = {},
  ): ResearchRunSummary => ({
    access: { mode: 'owner' },
    run_id: 'r-gate',
    status: 'waiting_for_approval',
    queue_position: null,
    question: 'Gate-Lauf',
    stack: 'default',
    mode: 'workspace_agent',
    kind: 'agent',
    session_id: 's-gate',
    agent_overrides: {},
    created_at: 1_700_000_000,
    started_at: 1_700_000_000,
    finished_at: null,
    elapsed_seconds: null,
    snapshot: {},
    error: null,
    events_url: '/v1/runs/r-gate/events',
    result_url: '/v1/runs/r-gate/result',
    ...overrides,
  })
  const approvalWire = (status: 'pending' | 'approved') => ({
    approval_id: 'ap-1',
    run_id: 'r-gate',
    kind: 'plan' as const,
    status,
    subject_type: 'plan',
    subject_id: 'p1',
    payload: {},
    decision: status === 'approved' ? 'approve' : '',
    note: '',
    decided_by_user_id: null,
    created_at: 1_700_000_000,
    decided_at: status === 'approved' ? 1_700_000_100 : null,
  })

  it('never regresses a decided approval back to pending (stale refetch race)', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      summary: agentSummary(),
      type: 'upsertAgentRunSummary',
    })
    state = researchDeskReducer(state, {
      approvals: [approvalWire('approved')],
      runId: 'r-gate',
      type: 'setAgentRunApprovals',
    })
    // The approval.decided event triggers a full-list refetch that can
    // observe the row still pending — it must not re-open the gate.
    state = researchDeskReducer(state, {
      approvals: [approvalWire('pending')],
      runId: 'r-gate',
      type: 'setAgentRunApprovals',
    })
    expect(state.agentRuns['r-gate'].approvals).toHaveLength(1)
    expect(state.agentRuns['r-gate'].approvals[0].status).toBe('approved')
  })

  it('never regresses an answered clarification back to pending', () => {
    const clarificationWire = (status: 'pending' | 'answered') => ({
      clarification_id: 'cl-1',
      run_id: 'r-gate',
      question: 'Welcher Markt?',
      options: [],
      default_assumption: '',
      status,
      answer: status === 'answered' ? 'DACH' : '',
      option_id: '',
      answered_by_user_id: null,
      created_at: 1_700_000_000,
      answered_at: status === 'answered' ? 1_700_000_050 : null,
    })
    let state = researchDeskReducer(createEmptyProjectState(), {
      summary: agentSummary(),
      type: 'upsertAgentRunSummary',
    })
    state = researchDeskReducer(state, {
      clarifications: [clarificationWire('answered')],
      runId: 'r-gate',
      type: 'setAgentRunClarifications',
    })
    // The clarification.answered event refetch can race the commit and
    // observe the round still pending — the gate must stay closed.
    state = researchDeskReducer(state, {
      clarifications: [clarificationWire('pending')],
      runId: 'r-gate',
      type: 'setAgentRunClarifications',
    })
    expect(state.agentRuns['r-gate'].clarifications).toHaveLength(1)
    expect(state.agentRuns['r-gate'].clarifications[0].status).toBe('answered')
  })

  it('titles a placeholder session from its first run question', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      session: {
        id: 's-gate',
        title: 'Neue Sitzung',
        groupId: null,
        createdAt: '2026-08-27T06:00:00.000Z',
        updatedAt: '2026-08-27T06:00:00.000Z',
        runIds: [],
        sourcePolicy: { knowledge: 'available', web: 'available' },
      },
      type: 'createAgentSession',
    })
    state = researchDeskReducer(state, {
      summary: agentSummary(),
      type: 'upsertAgentRunSummary',
    })
    expect(state.agentSessions['s-gate'].title).toBe('Gate-Lauf')
    // The retitle must SYNC exactly once: it stamps updatedAt with the
    // run's createdAt (the autosave fingerprint), so a reload sees a
    // real title instead of re-deriving one from hydration order.
    expect(state.agentSessions['s-gate'].updatedAt).not.toBe(
      '2026-08-27T06:00:00.000Z',
    )

    // …but HYDRATION must never hand out titles: it replays newest-first, so
    // deriving one there drifts a placeholder to the LATEST question.
    let hydrated = researchDeskReducer(createEmptyProjectState(), {
      session: {
        id: 's-gate',
        title: 'Neue Sitzung',
        groupId: null,
        createdAt: '2026-08-27T06:00:00.000Z',
        updatedAt: '2026-08-27T06:00:00.000Z',
        runIds: [],
        sourcePolicy: { knowledge: 'available', web: 'available' },
      },
      type: 'createAgentSession',
    })
    hydrated = researchDeskReducer(hydrated, {
      hydration: true,
      summary: agentSummary(),
      type: 'upsertApiRunSummary',
    })
    expect(hydrated.agentSessions['s-gate'].title).toBe('Neue Sitzung')
    expect(hydrated.agentSessions['s-gate'].runIds).toEqual(['r-gate'])

    // A deliberately named session keeps its name…
    let named = researchDeskReducer(createEmptyProjectState(), {
      session: {
        id: 's-gate',
        title: 'Mein Batterie-Projekt',
        groupId: null,
        createdAt: '2026-08-27T06:00:00.000Z',
        updatedAt: '2026-08-27T06:00:00.000Z',
        runIds: [],
        sourcePolicy: { knowledge: 'available', web: 'available' },
      },
      type: 'createAgentSession',
    })
    named = researchDeskReducer(named, {
      summary: agentSummary(),
      type: 'upsertAgentRunSummary',
    })
    expect(named.agentSessions['s-gate'].title).toBe('Mein Batterie-Projekt')

    // …and a later second run never retitles an established session.
    state = researchDeskReducer(state, {
      summary: agentSummary({
        question: 'Zweite Frage',
        run_id: 'r-gate-2',
      }),
      type: 'upsertAgentRunSummary',
    })
    expect(state.agentSessions['s-gate'].title).toBe('Gate-Lauf')
  })

  it('stores child gate rows on the parent and never regresses a decided row', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      summary: agentSummary({ status: 'waiting_for_children' }),
      type: 'upsertAgentRunSummary',
    })
    state = {
      ...state,
      agentRuns: {
        ...state.agentRuns,
        'r-gate': {
          ...state.agentRuns['r-gate'],
          childGates: {
            'r-child': { approvals: [], clarifications: [], stale: true },
          },
        },
      },
    }
    // The decide seeds the fresh decided row and clears the stale flag …
    state = researchDeskReducer(state, {
      approvals: [{ ...approvalWire('approved'), run_id: 'r-child' }],
      childRunId: 'r-child',
      runId: 'r-gate',
      type: 'setAgentChildGates',
    })
    const decided = state.agentRuns['r-gate'].childGates['r-child']
    expect(decided.stale).toBe(false)
    expect(decided.approvals[0].status).toBe('approved')
    // … and the racing full-list fetch must not regress it to pending.
    state = researchDeskReducer(state, {
      approvals: [{ ...approvalWire('pending'), run_id: 'r-child' }],
      childRunId: 'r-child',
      clarifications: [],
      runId: 'r-gate',
      type: 'setAgentChildGates',
    })
    const raced = state.agentRuns['r-gate'].childGates['r-child']
    expect(raced.approvals).toHaveLength(1)
    expect(raced.approvals[0].status).toBe('approved')
  })

  it('retains an in-flight answer publication across a stale artifact-list response', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      summary: agentSummary({ status: 'running' }),
      type: 'upsertAgentRunSummary',
    })
    const run = state.agentRuns['r-gate']
    state = {
      ...state,
      agentRuns: {
        ...state.agentRuns,
        'r-gate': {
          ...run,
          artifactOrder: ['answer-r-gate'],
          artifacts: {
            ...run.artifacts,
            'answer-r-gate': {
              artifactId: 'answer-r-gate',
              kind: 'answer',
              title: 'Antwort',
              status: 'writing',
              revision: 0,
              updatedBy: 'agent',
              refsCount: 0,
              createdAt: 1_700_000_000,
              updatedAt: 1_700_000_000,
              contentMarkdown: '**Zwischenstand**',
              publicationId: 'publication-r-gate',
              publicationOffset: 18,
            },
          },
        },
      },
    }

    state = researchDeskReducer(state, {
      artifacts: [],
      runId: 'r-gate',
      type: 'setAgentRunArtifacts',
    })

    expect(state.agentRuns['r-gate'].artifactOrder).toEqual(['answer-r-gate'])
    expect(state.agentRuns['r-gate'].artifacts['answer-r-gate']).toMatchObject({
      contentMarkdown: '**Zwischenstand**',
      publicationId: 'publication-r-gate',
      status: 'writing',
    })
  })

  it('keeps the streamed answer body across the post-ready list refetch', () => {
    // answer.ready sets publicationNeedsReconcile and re-lists the
    // artifacts; the list row carries the finalized (higher) revision but
    // no body. The streamed text IS the finalized text, so it stays on
    // screen while the detail fetch — still owed — brings the references.
    let state = researchDeskReducer(createEmptyProjectState(), {
      summary: agentSummary({ status: 'completed' }),
      type: 'upsertAgentRunSummary',
    })
    const run = state.agentRuns['r-gate']
    const streamed = {
      artifactId: 'answer-r-gate',
      kind: 'answer' as const,
      title: 'Antwort',
      status: 'ready' as const,
      revision: 1,
      updatedBy: 'agent' as const,
      refsCount: 0,
      createdAt: 1_700_000_000,
      updatedAt: 1_700_000_000,
      contentMarkdown: '# Fertig\n\nDer gestreamte Text.',
      publicationNeedsReconcile: true,
      publicationRefLabels: ['W1'],
    }
    state = {
      ...state,
      agentRuns: {
        ...state.agentRuns,
        'r-gate': { ...run, artifactOrder: ['answer-r-gate'], artifacts: { ...run.artifacts, 'answer-r-gate': streamed } },
      },
    }
    const row = {
      artifact_id: 'answer-r-gate',
      created_at: 1_700_000_000,
      kind: 'answer',
      refs_count: 3,
      revision: 2,
      status: 'ready',
      title: 'Antwort',
      updated_at: 1_700_000_001,
      updated_by: 'agent',
    }

    state = researchDeskReducer(state, {
      artifacts: [row as never],
      runId: 'r-gate',
      type: 'setAgentRunArtifacts',
    })
    const kept = state.agentRuns['r-gate'].artifacts['answer-r-gate']
    expect(kept.contentMarkdown).toBe('# Fertig\n\nDer gestreamte Text.')
    expect(kept.revision).toBe(2)
    expect(kept.publicationNeedsReconcile).toBe(true)
    // The announced labels bridge the citations until the refs arrive.
    expect(kept.publicationRefLabels).toEqual(['W1'])

    // A gapped stream is never kept, reconcile flag or not.
    state = {
      ...state,
      agentRuns: {
        ...state.agentRuns,
        'r-gate': {
          ...state.agentRuns['r-gate'],
          artifacts: { 'answer-r-gate': { ...streamed, revision: 1, publicationGap: true } },
        },
      },
    }
    state = researchDeskReducer(state, {
      artifacts: [row as never],
      runId: 'r-gate',
      type: 'setAgentRunArtifacts',
    })
    expect(state.agentRuns['r-gate'].artifacts['answer-r-gate'].contentMarkdown).toBeUndefined()

    // Without the reconcile flag the existing guard still drops an older body.
    state = {
      ...state,
      agentRuns: {
        ...state.agentRuns,
        'r-gate': {
          ...state.agentRuns['r-gate'],
          artifacts: { 'answer-r-gate': { ...streamed, revision: 1, publicationNeedsReconcile: false } },
        },
      },
    }
    state = researchDeskReducer(state, {
      artifacts: [row as never],
      runId: 'r-gate',
      type: 'setAgentRunArtifacts',
    })
    expect(state.agentRuns['r-gate'].artifacts['answer-r-gate'].contentMarkdown).toBeUndefined()
  })

  it('retains a fetched evidence payload across a metadata-list refresh', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      summary: agentSummary({ status: 'completed' }),
      type: 'upsertAgentRunSummary',
    })
    const meta = {
      artifact_id: 'artifact-evidence',
      created_at: 1_700_000_000,
      kind: 'evidence_bundle' as const,
      refs_count: 1,
      revision: 2,
      run_id: 'r-gate',
      session_id: null,
      status: 'ready' as const,
      title: 'Evidence',
      updated_at: 1_700_000_100,
      updated_by: 'agent' as const,
    }
    state = researchDeskReducer(state, {
      artifact: {
        ...meta,
        content_markdown: '',
        payload: {
          schema_version: 1,
          web_search_ledger: {
            kind: 'web_search_ledger',
            schema_version: 1,
            searches: {
              'query-1': { provider_answer: 'Grounded answer', query_id: 'query-1' },
            },
          },
        },
        refs: [{ label: 'W1', query_id: 'query-1' }],
        revisions: [],
      },
      runId: 'r-gate',
      type: 'setAgentRunArtifactDetail',
    })
    state = researchDeskReducer(state, {
      artifacts: [meta],
      runId: 'r-gate',
      type: 'setAgentRunArtifacts',
    })

    expect(state.agentRuns['r-gate'].artifacts['artifact-evidence'].payload)
      .toEqual({
        schema_version: 1,
        web_search_ledger: {
          kind: 'web_search_ledger',
          schema_version: 1,
          searches: {
            'query-1': { provider_answer: 'Grounded answer', query_id: 'query-1' },
          },
        },
      })
  })

  it('sweeps a run out of its phantom session once the real session hydrates', () => {
    // First sighting without session_id: phantom session keyed by runId.
    let state = researchDeskReducer(createEmptyProjectState(), {
      select: true,
      summary: agentSummary({ session_id: undefined }),
      type: 'upsertAgentRunSummary',
    })
    expect(state.agentSessions['r-gate'].runIds).toEqual(['r-gate'])
    expect(state.selectedAgentSessionId).toBe('r-gate')
    // Later summary carries the real session: exclusive membership, the
    // emptied phantom shell disappears and selection follows the run.
    state = researchDeskReducer(state, {
      summary: agentSummary(),
      type: 'upsertAgentRunSummary',
    })
    expect(state.agentSessions['r-gate']).toBeUndefined()
    expect(state.agentSessionOrder).not.toContain('r-gate')
    expect(state.agentSessions['s-gate'].runIds).toEqual(['r-gate'])
    expect(state.selectedAgentSessionId).toBe('s-gate')
    expect(state.ui.selectedAgentSessionId).toBe('s-gate')
  })

  it('keeps an empty-string session_id from stomping the known session', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      summary: agentSummary(),
      type: 'upsertAgentRunSummary',
    })
    state = researchDeskReducer(state, {
      summary: agentSummary({ session_id: '', status: 'running' }),
      type: 'upsertAgentRunSummary',
    })
    expect(state.agentRuns['r-gate'].sessionId).toBe('s-gate')
    expect(state.agentSessions['s-gate'].runIds).toEqual(['r-gate'])
    expect(state.agentSessions['r-gate']).toBeUndefined()
  })
})

describe('agent session selection mirrors into persisted ui intent', () => {
  it('select, create, run-summary select and delete all write ui.selectedAgentSessionId', () => {
    const base = {
      ...createEmptyProjectState(),
      agentSessionOrder: ['s1'],
      agentSessions: {
        s1: {
          id: 's1', title: 'A', groupId: null, createdAt: '', updatedAt: '', runIds: [],
          sourcePolicy: { web: 'available' as const, knowledge: 'available' as const },
        },
      },
    }
    const selected = researchDeskReducer(base, {
      sessionId: 's1',
      type: 'selectAgentSession',
    })
    expect(selected.ui.selectedAgentSessionId).toBe('s1')

    const created = researchDeskReducer(selected, {
      session: {
        id: 's2', title: 'B', groupId: null, createdAt: '', updatedAt: '', runIds: [],
        sourcePolicy: { web: 'available' as const, knowledge: 'available' as const },
      },
      type: 'createAgentSession',
    })
    expect(created.ui.selectedAgentSessionId).toBe('s2')

    const deleted = researchDeskReducer(created, {
      sessionId: 's2',
      type: 'deleteAgentSession',
    })
    expect(deleted.selectedAgentSessionId).toBe(deleted.ui.selectedAgentSessionId)
  })
})

describe('agent session source-policy hydration', () => {
  it('atomically selects a newly confirmed server session', () => {
    const base = createEmptyProjectState()
    const next = researchDeskReducer(base, {
      groups: [],
      selectSessionId: 's-confirmed',
      sessions: [{
        id: 's-confirmed',
        title: 'Confirmed',
        group_id: null,
        created_at: Date.parse('2026-07-10T10:00:00.000Z') / 1000,
        updated_at: Date.parse('2026-07-10T10:00:00.000Z') / 1000,
      }],
      type: 'upsertServerAgentSessions',
    })

    expect(next.selectedAgentSessionId).toBe('s-confirmed')
    expect(next.ui.selectedAgentSessionId).toBe('s-confirmed')
  })

  it('takes a newer pick from the list row and saves nothing back after a remount', async () => {
    // A view switch remounts the desk: the store keeps its sessions and the
    // new mount seeds each baseline from the listing. Another device picked a
    // different model meanwhile. A list row that carries the settings body
    // hands the newer pick to the store and the baseline alike, so nothing is
    // saved; a list row without it reads default settings and saved the stale
    // local pick back over the newer one.
    const base = createEmptyProjectState()
    const local = researchDeskReducer(base, {
      session: {
        id: 's-pick',
        title: 'Pick',
        groupId: null,
        createdAt: '2026-07-10T10:00:00.000Z',
        updatedAt: '2026-07-10T10:00:00.000Z',
        runIds: [],
        sourcePolicy: { web: 'disabled', knowledge: 'available' },
        modelSelection: { model: null, tier: 'fast', effort: null },
      },
      type: 'createAgentSession',
    })
    const elsewhere = {
      ...local.agentSessions['s-pick'],
      modelSelection: { model: null, tier: 'high' as const, effort: null },
      updatedAt: '2026-07-10T10:05:00.000Z',
    }
    const row = {
      id: 's-pick',
      title: 'Pick',
      group_id: null,
      created_at: Date.parse('2026-07-10T10:00:00.000Z') / 1000,
      updated_at: Date.parse('2026-07-10T10:05:00.000Z') / 1000,
      items_json: serverAgentSessionPayload(elsewhere).items_json,
    }
    const remounted = researchDeskReducer(local, {
      groups: [],
      sessions: [row],
      type: 'upsertServerAgentSessions',
    })
    expect(remounted.agentSessions['s-pick'].modelSelection?.tier).toBe('high')
    const synced = new Map([[row.id, serverAgentSessionFingerprint(row)]])
    expect(agentSessionFingerprint(remounted.agentSessions['s-pick'])).toBe(synced.get(row.id))
    const saved: string[] = []
    await pushAgentSessions({
      order: remounted.agentSessionOrder,
      save: async (session) => { saved.push(session.id) },
      sessions: remounted.agentSessions,
      synced,
    })
    expect(saved).toEqual([])
  })

  it('does not reset local policy when a metadata-only list row omits items_json', () => {
    const base = createEmptyProjectState()
    const withSession = researchDeskReducer(base, {
      session: {
        id: 's-source',
        title: 'Sources',
        groupId: null,
        createdAt: '2026-07-10T10:00:00.000Z',
        updatedAt: '2026-07-10T10:00:00.000Z',
        runIds: [],
        sourcePolicy: { web: 'disabled', knowledge: 'available' },
      },
      type: 'createAgentSession',
    })
    const metadataOnly = researchDeskReducer(withSession, {
      groups: [],
      sessions: [{
        id: 's-source',
        title: 'Sources',
        group_id: null,
        created_at: Date.parse('2026-07-10T10:00:00.000Z') / 1000,
        updated_at: Date.parse('2026-07-10T10:01:00.000Z') / 1000,
      }],
      type: 'upsertServerAgentSessions',
    })
    expect(metadataOnly.agentSessions['s-source'].sourcePolicy).toEqual({
      web: 'disabled',
      knowledge: 'available',
    })
  })

  it('switches, keeps and hydrates the session knowledge switch', () => {
    const base = createEmptyProjectState()
    const withSession = researchDeskReducer(base, {
      session: {
        id: 's-know',
        title: 'Knowledge',
        groupId: null,
        createdAt: '2026-07-10T10:00:00.000Z',
        updatedAt: '2026-07-10T10:00:00.000Z',
        runIds: [],
        sourcePolicy: { web: 'available', knowledge: 'available' },
      },
      type: 'createAgentSession',
    })
    expect(withSession.agentSessions['s-know'].knowledgeSwitch).toBeUndefined()
    const on = researchDeskReducer(withSession, {
      knowledgeSwitch: 'all',
      sessionId: 's-know',
      type: 'setAgentSessionKnowledgeSwitch',
    })
    expect(on.agentSessions['s-know'].knowledgeSwitch).toBe('all')
    expect(on.dirty).toBe(true)
    // Same value again is a no-op (no autosave churn).
    expect(researchDeskReducer(on, {
      knowledgeSwitch: 'all',
      sessionId: 's-know',
      type: 'setAgentSessionKnowledgeSwitch',
    })).toBe(on)
    // Local-newer-wins: a stale server row (older than the click) must not
    // undo the switch, whether it carries metadata or not.
    const stale = researchDeskReducer(on, {
      groups: [],
      sessions: [{
        id: 's-know',
        title: 'Knowledge',
        group_id: null,
        created_at: Date.parse('2026-07-10T10:00:00.000Z') / 1000,
        updated_at: Date.parse('2026-07-10T10:01:00.000Z') / 1000,
        items_json: JSON.stringify({
          source_policy: { web: 'available', knowledge: 'available' },
          knowledge_switch: 'off',
        }),
      }],
      type: 'upsertServerAgentSessions',
    })
    expect(stale.agentSessions['s-know'].knowledgeSwitch).toBe('all')
    // A newer metadata-only list row (no items_json) keeps the local
    // switch, while a newer full row hydrates whatever the server stored,
    // including "off" (the struck-through glyph survives a reload).
    const later = Math.floor(Date.now() / 1000) + 60
    const metadataOnly = researchDeskReducer(stale, {
      groups: [],
      sessions: [{
        id: 's-know',
        title: 'Knowledge',
        group_id: null,
        created_at: Date.parse('2026-07-10T10:00:00.000Z') / 1000,
        updated_at: later,
      }],
      type: 'upsertServerAgentSessions',
    })
    expect(metadataOnly.agentSessions['s-know'].knowledgeSwitch).toBe('all')
    const hydrated = researchDeskReducer(metadataOnly, {
      groups: [],
      sessions: [{
        id: 's-know',
        title: 'Knowledge',
        group_id: null,
        created_at: Date.parse('2026-07-10T10:00:00.000Z') / 1000,
        updated_at: later + 60,
        items_json: JSON.stringify({
          source_policy: { web: 'available', knowledge: 'available' },
          knowledge_switch: 'off',
        }),
      }],
      type: 'upsertServerAgentSessions',
    })
    expect(hydrated.agentSessions['s-know'].knowledgeSwitch).toBe('off')
  })
})

describe('durable session deletion projection', () => {
  it('keeps an Agent session visible and moves selection while deletion runs', () => {
    let state = createEmptyProjectState()
    state = researchDeskReducer(state, {
      session: {
        id: 'as-delete',
        title: 'Delete me',
        groupId: null,
        createdAt: '2026-07-10T10:00:00.000Z',
        updatedAt: '2026-07-10T10:00:00.000Z',
        runIds: [],
        sourcePolicy: { web: 'available', knowledge: 'available' },
      },
      type: 'createAgentSession',
    })
    const deleting = researchDeskReducer(state, {
      deletion: {
        error: null,
        operationId: 'del_agent',
        stage: 'queued',
        status: 'deleting',
      },
      sessionId: 'as-delete',
      type: 'setAgentSessionDeletionState',
    })

    expect(deleting.agentSessions['as-delete'].deletion?.operationId).toBe('del_agent')
    expect(deleting.selectedAgentSessionId).not.toBe('as-delete')
    expect(researchDeskReducer(deleting, {
      sessionId: 'as-delete',
      type: 'selectAgentSession',
    })).toBe(deleting)
  })

  it('lets a server tombstone override a newer local Knowledge session', () => {
    const base = createEmptyProjectState()
    const local = makeKnowledgeSession('ks-delete', 'Local', {
      updatedAt: '2026-07-10T11:00:00.000Z',
    })
    const withLocal = researchDeskReducer(base, {
      session: local,
      type: 'createKnowledgeSession',
    })
    const projected = researchDeskReducer(withLocal, {
      memberships: { 'ks-delete': null },
      sessions: [{
        ...local,
        deletion: {
          error: 'dependency unavailable',
          operationId: 'del_knowledge',
          stage: 'delete_failed',
          status: 'delete_failed',
        },
        updatedAt: '2026-07-10T10:00:00.000Z',
      }],
      type: 'upsertServerKnowledgeSessions',
    })

    expect(projected.knowledgeSessions['ks-delete'].deletion).toEqual({
      error: 'dependency unavailable',
      operationId: 'del_knowledge',
      stage: 'delete_failed',
      status: 'delete_failed',
    })
    expect(projected.selectedKnowledgeSessionId).not.toBe('ks-delete')
  })

  it('rejects late Agent run hydration after terminal aggregate deletion', () => {
    const root = makeRunSummary({
      kind: 'agent',
      mode: 'workspace_agent',
      run_id: 'r-gate',
      session_id: 's-gate',
    })
    const child = makeRunSummary({
      kind: 'agent_child',
      mode: 'workspace_agent',
      parent_run_id: root.run_id,
      root_run_id: root.run_id,
      run_id: 'r-gate-child',
      session_id: undefined,
    })
    let state = researchDeskReducer(createEmptyProjectState(), {
      summary: root,
      type: 'upsertAgentRunSummary',
    })
    state = researchDeskReducer(state, {
      summary: child,
      type: 'upsertAgentRunSummary',
    })
    expect(state.agentSessions['r-gate-child']).toBeDefined()

    state = researchDeskReducer(state, {
      operationId: 'del_agent_terminal',
      sessionId: 's-gate',
      type: 'deleteAgentSession',
    })
    expect(state.agentRuns['r-gate']).toBeUndefined()
    expect(state.agentRuns['r-gate-child']).toBeUndefined()
    expect(state.agentSessions['r-gate-child']).toBeUndefined()

    const afterRootReplay = researchDeskReducer(state, {
      summary: root,
      type: 'upsertAgentRunSummary',
    })
    const afterChildReplay = researchDeskReducer(afterRootReplay, {
      summary: child,
      type: 'upsertAgentRunSummary',
    })
    expect(afterChildReplay).toBe(afterRootReplay)
    expect(afterChildReplay.agentSessions['s-gate']).toBeUndefined()
  })

  it('rejects stale Knowledge session and item hydration after completion', () => {
    const session = makeKnowledgeSession('ks-terminal', 'Deleted')
    let state = researchDeskReducer(createEmptyProjectState(), {
      session,
      type: 'createKnowledgeSession',
    })
    state = researchDeskReducer(state, {
      operationId: 'del_knowledge_terminal',
      sessionId: session.id,
      type: 'deleteKnowledgeSession',
    })
    const replayed = researchDeskReducer(state, {
      memberships: { [session.id]: null },
      sessions: [session],
      type: 'upsertServerKnowledgeSessions',
    })
    expect(replayed.knowledgeSessions[session.id]).toBeUndefined()
    expect(researchDeskReducer(replayed, {
      items: [],
      sessionId: session.id,
      type: 'setServerKnowledgeSessionItems',
    })).toBe(replayed)
  })
})

describe('agent model selection actions', () => {
  it('keeps the chat-picker exclusivity contract on the agent fields', () => {
    let state = createEmptyProjectState()
    state = researchDeskReducer(state, {
      model: 'claude-opus-4-8',
      type: 'setSelectedAgentModel',
    })
    state = researchDeskReducer(state, {
      effort: 'high',
      type: 'setSelectedAgentEffort',
    })
    expect(state.ui.selectedAgentModel).toBe('claude-opus-4-8')
    expect(state.ui.selectedAgentEffort).toBe('high')
    expect(state.ui.selectedAgentModelTier).toBeNull()
    // Tier pick clears model AND effort (effort is model-dependent).
    state = researchDeskReducer(state, {
      tier: 'mid',
      type: 'setSelectedAgentModelTier',
    })
    expect(state.ui.selectedAgentModelTier).toBe('mid')
    expect(state.ui.selectedAgentModel).toBeNull()
    expect(state.ui.selectedAgentEffort).toBeNull()
    // Model pick clears the tier again.
    state = researchDeskReducer(state, {
      model: 'claude-haiku-4-5',
      type: 'setSelectedAgentModel',
    })
    expect(state.ui.selectedAgentModelTier).toBeNull()
    // The AGENT selection never touches the chat fields.
    expect(state.ui.selectedChatModel).toBeNull()
    expect(state.ui.selectedChatModelTier).toBeNull()
  })
})

describe('agent model stickiness (session-scoped)', () => {
  const makeSession = (id: string) => ({
    id,
    title: id,
    groupId: null,
    createdAt: '2026-08-07T10:00:00.000Z',
    updatedAt: '2026-08-07T10:00:00.000Z',
    runIds: [] as string[],
    sourcePolicy: { web: 'available' as const, knowledge: 'available' as const },
  })

  function withSession() {
    return researchDeskReducer(createEmptyProjectState(), {
      session: makeSession('sess-a'),
      type: 'createAgentSession',
    })
  }

  it('writes a user pick onto the active session', () => {
    let state = withSession()
    state = researchDeskReducer(state, { tier: 'fast', type: 'setSelectedAgentModelTier' })
    expect(state.agentSessions['sess-a'].modelSelection).toEqual({
      effort: null,
      model: null,
      tier: 'fast',
    })
    expect(state.ui.selectedAgentModelTier).toBe('fast')
  })

  it('restores the session pick when switching back', () => {
    let state = withSession()
    state = researchDeskReducer(state, { tier: 'fast', type: 'setSelectedAgentModelTier' })
    state = researchDeskReducer(state, {
      session: makeSession('sess-b'),
      type: 'createAgentSession',
    })
    // A fresh session carries no pick, so the preference can seed it.
    expect(state.ui.selectedAgentModelTier).toBeNull()

    state = researchDeskReducer(state, { sessionId: 'sess-a', type: 'selectAgentSession' })
    expect(state.ui.selectedAgentModelTier).toBe('fast')
  })

  it('does not bump updatedAt when the pick is unchanged', () => {
    let state = withSession()
    state = researchDeskReducer(state, { tier: 'fast', type: 'setSelectedAgentModelTier' })
    const stamp = state.agentSessions['sess-a'].updatedAt
    state = researchDeskReducer(state, { tier: 'fast', type: 'setSelectedAgentModelTier' })
    expect(state.agentSessions['sess-a'].updatedAt).toBe(stamp)
  })

  it('clearing everything removes the stored pick instead of pinning nulls', () => {
    // The picker's Auto row means "follow my default" — with a pinned
    // null-triple the preference could never seed again.
    let state = withSession()
    state = researchDeskReducer(state, { tier: 'fast', type: 'setSelectedAgentModelTier' })
    state = researchDeskReducer(state, { tier: null, type: 'setSelectedAgentModelTier' })
    expect(state.agentSessions['sess-a'].modelSelection).toBeUndefined()
  })

  it('never lets an agent pick reach the chat fields', () => {
    let state = withSession()
    state = researchDeskReducer(state, { tier: 'fast', type: 'setSelectedAgentModelTier' })
    expect(state.ui.selectedChatModelTier).toBeNull()
    expect(state.ui.selectedChatModel).toBeNull()
  })

  it('keeps working before any session exists', () => {
    let state = createEmptyProjectState()
    state = researchDeskReducer(state, { tier: 'high', type: 'setSelectedAgentModelTier' })
    expect(state.ui.selectedAgentModelTier).toBe('high')
  })
})

describe('agent preference seeding never masquerades as a pick', () => {
  it('touches only the working value, never the session', () => {
    // Writing the seed into the session bumps updatedAt, makes the local copy
    // look newer than the server row, and blocks the stored pick from ever
    // loading.
    let state = researchDeskReducer(createEmptyProjectState(), {
      session: {
        id: 'sess-seed',
        title: 'S',
        groupId: null,
        createdAt: '2026-08-07T10:00:00.000Z',
        updatedAt: '2026-08-07T10:00:00.000Z',
        runIds: [],
        sourcePolicy: { web: 'available', knowledge: 'available' },
      },
      type: 'createAgentSession',
    })
    const stamp = state.agentSessions['sess-seed'].updatedAt
    const dirtyBefore = state.dirty
    state = researchDeskReducer(state, {
      tier: 'high',
      type: 'seedAgentModelTierFromPreference',
    })
    expect(state.ui.selectedAgentModelTier).toBe('high')
    expect(state.agentSessions['sess-seed'].modelSelection).toBeUndefined()
    expect(state.agentSessions['sess-seed'].updatedAt).toBe(stamp)
    expect(state.dirty).toBe(dirtyBefore)
  })

  it('yields to a pick the user already made', () => {
    let state = createEmptyProjectState()
    state = researchDeskReducer(state, { tier: 'fast', type: 'setSelectedAgentModelTier' })
    state = researchDeskReducer(state, {
      tier: 'high',
      type: 'seedAgentModelTierFromPreference',
    })
    expect(state.ui.selectedAgentModelTier).toBe('fast')
  })
})

describe('agent model stickiness survives the server hydrate', () => {
  const wire = (id: string, itemsJson?: string) => ({
    id,
    title: 'Server',
    group_id: null,
    created_at: 1_700_000_000,
    updated_at: 1_800_000_500,
    ...(itemsJson === undefined ? {} : { items_json: itemsJson }),
  })
  const withPick = JSON.stringify({
    source_policy: { web: 'available', knowledge: 'available' },
    model_selection: { model: 'gpt-5.4-nano', tier: null, effort: null },
  })

  it('restores the stored pick and marks the detail as hydrated', () => {
    const state = researchDeskReducer(createEmptyProjectState(), {
      groups: [],
      sessions: [wire('sess-srv', withPick)],
      type: 'upsertServerAgentSessions',
    })
    expect(state.agentSessions['sess-srv'].modelSelection).toEqual({
      effort: null,
      model: 'gpt-5.4-nano',
      tier: null,
    })
    expect(state.agentSessions['sess-srv'].metadataHydrated).toBe(true)
  })

  it('a metadata-only list row neither hydrates nor clears the local pick', () => {
    // The list deliberately omits items_json. Treating that as "no pick"
    // would wipe the local value on every background refresh.
    let state = researchDeskReducer(createEmptyProjectState(), {
      groups: [],
      sessions: [wire('sess-srv', withPick)],
      type: 'upsertServerAgentSessions',
    })
    state = researchDeskReducer(state, {
      groups: [],
      sessions: [{ ...wire('sess-srv'), updated_at: 1_800_000_900 }],
      type: 'upsertServerAgentSessions',
    })
    expect(state.agentSessions['sess-srv'].modelSelection).toEqual({
      effort: null,
      model: 'gpt-5.4-nano',
      tier: null,
    })
  })

  it('fills an untouched working value for the selected session', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      groups: [],
      selectSessionId: 'sess-srv',
      sessions: [wire('sess-srv', withPick)],
      type: 'upsertServerAgentSessions',
    })
    expect(state.ui.selectedAgentModel).toBe('gpt-5.4-nano')

    // And the late DETAIL of an already-selected session fills it too.
    state = researchDeskReducer(createEmptyProjectState(), {
      groups: [],
      selectSessionId: 'sess-srv',
      sessions: [wire('sess-srv')],
      type: 'upsertServerAgentSessions',
    })
    expect(state.ui.selectedAgentModel).toBeNull()
    state = researchDeskReducer(state, {
      groups: [],
      sessions: [wire('sess-srv', withPick)],
      type: 'upsertServerAgentSessions',
    })
    expect(state.ui.selectedAgentModel).toBe('gpt-5.4-nano')
  })

  it('never overwrites a pick made while the detail was in flight', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      groups: [],
      selectSessionId: 'sess-srv',
      sessions: [wire('sess-srv')],
      type: 'upsertServerAgentSessions',
    })
    state = researchDeskReducer(state, { tier: 'mid', type: 'setSelectedAgentModelTier' })
    state = researchDeskReducer(state, {
      groups: [],
      sessions: [{ ...wire('sess-srv', withPick), updated_at: 1_900_000_000 }],
      type: 'upsertServerAgentSessions',
    })
    expect(state.ui.selectedAgentModelTier).toBe('mid')
  })
})

describe('chat thread model stickiness (thread-scoped)', () => {
  function withThread() {
    let state = researchDeskReducer(createEmptyProjectState(), {
      preview: 'Bereit.',
      title: 'A',
      type: 'createChatThread',
    })
    // createChatThread selects the new thread; activeView starts as 'chat'?
    // The empty state's activeView is whatever seedProject sets — force chat
    // so the write-through gate is open like in the real surface.
    state = { ...state, ui: { ...state.ui, activeView: 'chat' as const } }
    return state
  }

  it('writes a user pick onto the active thread and bumps updatedAt', () => {
    let state = withThread()
    const threadId = state.ui.selectedChatThreadId as string
    const stamp = state.chatThreads[threadId].updatedAt
    state = researchDeskReducer(state, { model: 'gpt-5.4-nano', type: 'setSelectedChatModel' })
    expect(state.chatThreads[threadId].modelSelection).toEqual({
      effort: null,
      model: 'gpt-5.4-nano',
      tier: null,
    })
    expect(state.chatThreads[threadId].updatedAt >= stamp).toBe(true)
    // Unchanged pick must not bump again (autosave would loop).
    const bumped = state.chatThreads[threadId].updatedAt
    state = researchDeskReducer(state, { model: 'gpt-5.4-nano', type: 'setSelectedChatModel' })
    expect(state.chatThreads[threadId].updatedAt).toBe(bumped)
  })

  it('an editor-view pick never touches the thread', () => {
    let state = withThread()
    const threadId = state.ui.selectedChatThreadId as string
    state = { ...state, ui: { ...state.ui, activeView: 'editor' as const } }
    state = researchDeskReducer(state, { model: 'claude-opus-4-8', type: 'setSelectedChatModel' })
    expect(state.chatThreads[threadId].modelSelection).toBeUndefined()
    // The editor view still writes the shared chat pick in `ui`; this
    // coupling is temporary until the editor view keeps its own pick.
    expect(state.ui.selectedChatModel).toBe('claude-opus-4-8')
  })

  it('restores the thread pick when switching back', () => {
    let state = withThread()
    const threadA = state.ui.selectedChatThreadId as string
    state = researchDeskReducer(state, { tier: 'fast', type: 'setSelectedChatModelTier' })
    state = researchDeskReducer(state, {
      preview: 'Bereit.',
      title: 'B',
      type: 'createChatThread',
    })
    expect(state.ui.selectedChatModelTier).toBeNull()
    state = researchDeskReducer(state, { threadId: threadA, type: 'selectChatThread' })
    expect(state.ui.selectedChatModelTier).toBe('fast')
  })

  it('seeding stays ui-only and yields to a pick', () => {
    let state = withThread()
    const threadId = state.ui.selectedChatThreadId as string
    const stamp = state.chatThreads[threadId].updatedAt
    state = researchDeskReducer(state, { tier: 'high', type: 'seedChatModelTierFromPreference' })
    expect(state.ui.selectedChatModelTier).toBe('high')
    expect(state.chatThreads[threadId].modelSelection).toBeUndefined()
    expect(state.chatThreads[threadId].updatedAt).toBe(stamp)
    state = researchDeskReducer(state, { tier: 'fast', type: 'setSelectedChatModelTier' })
    state = researchDeskReducer(state, { tier: 'high', type: 'seedChatModelTierFromPreference' })
    expect(state.ui.selectedChatModelTier).toBe('fast')
  })

  it('hydrate restores the stored pick and fills an untouched ui', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      append: false,
      memberships: {},
      threads: [{
        createdAt: '2026-08-07T10:00:00.000Z',
        id: 'ct_srv',
        messages: [],
        modelSelection: { model: null, tier: 'fast', effort: null },
        preview: '',
        source: 'api' as const,
        title: 'Srv',
        updatedAt: '2026-08-07T10:00:00.000Z',
      }],
      type: 'upsertServerChatThreads',
    })
    expect(state.chatThreads['ct_srv'].modelSelection).toEqual({
      model: null, tier: 'fast', effort: null,
    })
    // A NEWER server row replaces the stored pick — the merge must copy the
    // field, not keep the stale local one.
    state = researchDeskReducer(state, {
      append: false,
      memberships: {},
      threads: [{
        createdAt: '2026-08-07T10:00:00.000Z',
        id: 'ct_srv',
        messages: [],
        modelSelection: { model: null, tier: 'high', effort: null },
        preview: '',
        source: 'api' as const,
        title: 'Srv',
        updatedAt: '2026-08-07T10:00:01.000Z',
      }],
      type: 'upsertServerChatThreads',
    })
    expect(state.chatThreads['ct_srv'].modelSelection).toEqual({
      model: null, tier: 'high', effort: null,
    })
    // Untouched ui of the selected thread fills from the arriving row.
    state = { ...state, ui: { ...state.ui, selectedChatThreadId: 'ct_srv' } }
    state = researchDeskReducer(state, {
      append: false,
      memberships: {},
      threads: [{
        createdAt: '2026-08-07T10:00:00.000Z',
        id: 'ct_srv',
        messages: [],
        modelSelection: { model: null, tier: 'high', effort: null },
        preview: '',
        source: 'api' as const,
        title: 'Srv',
        updatedAt: '2026-08-07T10:00:02.000Z',
      }],
      type: 'upsertServerChatThreads',
    })
    expect(state.ui.selectedChatModelTier).toBe('high')
  })
})

describe('appendApiRunEvents (history batch)', () => {
  function seededWithRun() {
    const run = fromRunSummary(makeRunSummary({ run_id: 'run-batch' }), 'test-stack')
    const base = createEmptyProjectState()
    return {
      ...base,
      researchRunOrder: [run.runId],
      researchRuns: { [run.runId]: run },
    }
  }
  const batchEvents = [1, 2, 3].map((sequence) =>
    makeRunEvent({
      created_at: Date.parse('2026-01-01T00:00:05.000Z') / 1000 + sequence,
      data: { message: `Schritt ${sequence}`, status: 'running' },
      run_id: 'run-batch',
      sequence,
      type: 'inqtrix.progress.message',
    }))

  it('is equivalent to N single appends except for arrivedLive', () => {
    // The batch path exists so a replay lands in ONE commit without any row
    // counting as live — it must never drift from the singular path in
    // anything else (status transitions, dedup, knowledge/agent routing).
    const viaBatch = researchDeskReducer(seededWithRun(), {
      events: batchEvents,
      type: 'appendApiRunEvents',
    })
    const viaSingles = batchEvents.reduce(
      (state, event) => researchDeskReducer(state, { event, type: 'appendApiRunEvent' }),
      seededWithRun(),
    )

    const stripLive = (state: typeof viaBatch) =>
      state.researchRuns['run-batch'].events.map((event) => {
        const { arrivedLive, ...rest } = event
        void arrivedLive
        return rest
      })
    expect(stripLive(viaBatch)).toEqual(stripLive(viaSingles))
    expect(viaBatch.researchRuns['run-batch'].status)
      .toBe(viaSingles.researchRuns['run-batch'].status)

    // The flag is the ONLY divergence: history renders in place, live rises.
    expect(viaBatch.researchRuns['run-batch'].events.map((event) => event.arrivedLive))
      .toEqual([undefined, undefined, undefined])
    expect(viaSingles.researchRuns['run-batch'].events.map((event) => event.arrivedLive))
      .toEqual([true, true, true])
  })
})

describe('markApiRunUnavailable', () => {
  function seeded() {
    const run = fromRunSummary(
      makeRunSummary({ run_id: 'run-gone', status: 'running' }),
      'test-stack',
    )
    const base = createEmptyProjectState()
    return {
      ...base,
      researchRunOrder: [run.runId],
      researchRuns: { [run.runId]: run },
    }
  }

  it('sets the calm lock without claiming a status', () => {
    // The server's 404 is deliberately non-disclosing (revoked, deleted,
    // expired and foreign all identical), so the record keeps its last
    // known status and only the flag moves — the card renders a lock,
    // never "access revoked" and never a red error.
    const next = researchDeskReducer(seeded(), {
      runId: 'run-gone',
      type: 'markApiRunUnavailable',
    })
    const run = next.researchRuns['run-gone']
    expect(run.unavailable).toBe(true)
    expect(run.status).toBe('running')
    expect(run.error).toBeUndefined()
  })

  it('is idempotent and ignores unknown runs', () => {
    const once = researchDeskReducer(seeded(), {
      runId: 'run-gone',
      type: 'markApiRunUnavailable',
    })
    expect(researchDeskReducer(once, {
      runId: 'run-gone',
      type: 'markApiRunUnavailable',
    })).toBe(once)
    const state = seeded()
    expect(researchDeskReducer(state, {
      runId: 'run-unknown',
      type: 'markApiRunUnavailable',
    })).toBe(state)
  })
})

describe('markApiRunUnavailable for knowledge asks', () => {
  it('settles the thread item terminally instead of spinning forever', () => {
    // Knowledge runs never live in researchRuns: without this branch the
    // dispatch was a no-op, the item claimed "running" forever, the ask
    // gate stayed blocked, and cancel also 404s -- no path out.
    const base = createEmptyProjectState()
    const withItem = researchDeskReducer(base, {
      item: makeKnowledgeItem('ki-gone', base.selectedKnowledgeSessionId as string),
      type: 'startKnowledgeAsk',
    })
    const next = researchDeskReducer(withItem, {
      runId: 'run-ki-gone',
      type: 'markApiRunUnavailable',
    })
    const item = next.knowledgeItems['ki-gone']
    expect(item.status).toBe('failed')
    expect(item.error).toBe('Dieser Lauf ist nicht mehr verfügbar.')
  })

  it('never demotes a completed item', () => {
    const base = createEmptyProjectState()
    const withItem = researchDeskReducer(base, {
      item: makeKnowledgeItem('ki-done', base.selectedKnowledgeSessionId as string, {
        status: 'completed',
      }),
      type: 'startKnowledgeAsk',
    })
    const next = researchDeskReducer(withItem, {
      runId: 'run-ki-done',
      type: 'markApiRunUnavailable',
    })
    expect(next.knowledgeItems['ki-done'].status).toBe('completed')
  })
})

describe('unavailable runs and the status buckets', () => {
  it('drops out of the running bucket but stays under all', () => {
    // The card visibly retracts its last status (calm lock): the COUNT
    // and the filtered list must not keep claiming "running".
    const run = {
      ...fromRunSummary(
        makeRunSummary({ run_id: 'run-lock', status: 'running' }),
        'test-stack',
      ),
      unavailable: true,
    }
    const jobs = projectResearchJobs({
      ...createEmptyProjectState(),
      researchRunOrder: [run.runId],
      researchRuns: { [run.runId]: run },
    })
    expect(jobs).toHaveLength(1)
    expect(jobs[0].unavailable).toBe(true)
    expect(visibleResearchJobs(jobs, 'all')).toHaveLength(1)
    expect(visibleResearchJobs(jobs, 'running')).toHaveLength(0)
  })
})

/**
 * Session artifact index: the anchor-independent listing per session.
 * Seeded stale on the session's first sighting, filled by the session
 * listing fetch, re-flagged when a run's artifact surface invalidates
 * (an update may have re-anchored a session artifact), and NEVER left
 * stale after a failed fetch (no tight loop — the error stays visible).
 */
describe('agent session artifact index', () => {
  const summary: ResearchRunSummary = {
    access: { mode: 'owner' },
    run_id: 'r-idx',
    status: 'running',
    queue_position: null,
    question: 'Indexlauf',
    stack: 'default',
    mode: 'workspace_agent',
    kind: 'agent',
    session_id: 's-idx',
    agent_overrides: {},
    created_at: 1_700_000_000,
    started_at: 1_700_000_000,
    finished_at: null,
    elapsed_seconds: null,
    snapshot: {},
    error: null,
    events_url: '/v1/runs/r-idx/events',
    result_url: '/v1/runs/r-idx/result',
  }
  const metaWire = {
    artifact_id: 'a-memo',
    created_at: 1_700_000_010,
    kind: 'memo' as const,
    refs_count: 0,
    revision: 2,
    run_id: 'r-anchor',
    session_id: 's-idx',
    status: 'ready' as const,
    title: 'Memo',
    updated_at: 1_700_000_020,
    updated_by: 'agent' as const,
  }

  it('seeds a stale empty index on first session sighting', () => {
    const base = researchDeskReducer(createEmptyProjectState(), {
      summary,
      type: 'upsertAgentRunSummary',
    })
    expect(base.agentSessionArtifacts['s-idx']).toEqual({
      byId: {},
      order: [],
      stale: true,
    })
  })

  it('stores listing rows with their CURRENT anchor and clears stale', () => {
    const base = researchDeskReducer(createEmptyProjectState(), {
      summary,
      type: 'upsertAgentRunSummary',
    })
    const filled = researchDeskReducer(base, {
      artifacts: [metaWire],
      sessionId: 's-idx',
      type: 'setAgentSessionArtifacts',
    })
    const index = filled.agentSessionArtifacts['s-idx']
    expect(index.stale).toBe(false)
    expect(index.order).toEqual(['a-memo'])
    expect(index.byId['a-memo'].runId).toBe('r-anchor')
  })

  const detailWire = {
    ...metaWire,
    content_markdown: 'Inhalt',
    refs: [],
    revisions: [],
  }

  it('lands a body fetched under a non-desk anchor on the session index row', () => {
    // A delegated child is never a desk record; its memo body has no run
    // record to live on, so the index row holds it for the resolver.
    const withIndex = researchDeskReducer(createEmptyProjectState(), {
      artifacts: [metaWire],
      sessionId: 's-idx',
      type: 'setAgentSessionArtifacts',
    })
    const loaded = researchDeskReducer(withIndex, {
      artifact: detailWire,
      runId: 'r-anchor',
      type: 'setAgentRunArtifactDetail',
    })
    expect(loaded.agentRuns['r-anchor']).toBeUndefined()
    expect(
      loaded.agentSessionArtifacts['s-idx'].byId['a-memo'].record?.contentMarkdown,
    ).toBe('Inhalt')
  })

  it('drops a non-desk body when no index row knows the artifact', () => {
    const empty = createEmptyProjectState()
    const same = researchDeskReducer(empty, {
      artifact: detailWire,
      runId: 'r-anchor',
      type: 'setAgentRunArtifactDetail',
    })
    expect(same).toBe(empty)
  })

  it('keeps an index-held body that is not older than the refreshed row', () => {
    const withIndex = researchDeskReducer(createEmptyProjectState(), {
      artifacts: [metaWire],
      sessionId: 's-idx',
      type: 'setAgentSessionArtifacts',
    })
    const loaded = researchDeskReducer(withIndex, {
      artifact: detailWire,
      runId: 'r-anchor',
      type: 'setAgentRunArtifactDetail',
    })
    const sameRevision = researchDeskReducer(loaded, {
      artifacts: [metaWire],
      sessionId: 's-idx',
      type: 'setAgentSessionArtifacts',
    })
    expect(
      sameRevision.agentSessionArtifacts['s-idx'].byId['a-memo'].record?.contentMarkdown,
    ).toBe('Inhalt')
    const newerRow = researchDeskReducer(loaded, {
      artifacts: [{ ...metaWire, revision: 3 }],
      sessionId: 's-idx',
      type: 'setAgentSessionArtifacts',
    })
    expect(newerRow.agentSessionArtifacts['s-idx'].byId['a-memo'].record).toBeUndefined()
  })

  it('re-flags the index when a run event invalidates its artifacts', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      summary,
      type: 'upsertAgentRunSummary',
    })
    state = researchDeskReducer(state, {
      artifacts: [metaWire],
      sessionId: 's-idx',
      type: 'setAgentSessionArtifacts',
    })
    // Clear the run's own stale artifact surface first (transition pin).
    state = researchDeskReducer(state, {
      artifacts: [],
      runId: 'r-idx',
      type: 'setAgentRunArtifacts',
    })
    expect(state.agentRuns['r-idx'].artifactsStale).toBe(false)
    const completed = researchDeskReducer(state, {
      event: {
        type: 'inqtrix.run.completed',
        run_id: 'r-idx',
        sequence: 10,
        created_at: 1_700_000_100,
        data: {},
      },
      type: 'appendApiRunEvent',
    })
    const index = completed.agentSessionArtifacts['s-idx']
    expect(index.stale).toBe(true)
    // Rows survive the re-flag — the menu keeps rendering while refetching.
    expect(index.order).toEqual(['a-memo'])
  })

  it('records a fetch failure visibly and clears stale (no tight loop)', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      summary,
      type: 'upsertAgentRunSummary',
    })
    state = researchDeskReducer(state, {
      artifacts: [metaWire],
      sessionId: 's-idx',
      type: 'setAgentSessionArtifacts',
    })
    const failed = researchDeskReducer(state, {
      message: 'Listing fehlgeschlagen',
      sessionId: 's-idx',
      type: 'markAgentSessionArtifactsError',
    })
    const index = failed.agentSessionArtifacts['s-idx']
    expect(index.stale).toBe(false)
    expect(index.error).toBe('Listing fehlgeschlagen')
    expect(index.order).toEqual(['a-memo'])
  })
})

/**
 * Run summaries hydrate NEWEST-FIRST and may beat the session listing. The
 * fabricated session record must (a) never carry merge authority (the real
 * server row wins), (b) converge its display title on the OLDEST run seen,
 * and (c) never be pushed by the autosave (agentSessionSync fence, pinned
 * there). Without all three, a reload retitles the session to the LATEST
 * question and syncs that derived title back durably over the stored one
 * (DB updated_at ends up equal to the newest run's created_at).
 */
describe('derived session titles never gain authority', () => {
  const derivedTitleSummary = (
    runId: string,
    createdAt: number,
    question: string,
  ): ResearchRunSummary => ({
    access: { mode: 'owner' },
    run_id: runId,
    status: 'completed',
    queue_position: null,
    question,
    stack: 'default',
    mode: 'agent_kernel',
    kind: 'agent',
    session_id: 's-derived-title',
    agent_overrides: {},
    created_at: createdAt,
    started_at: createdAt,
    finished_at: createdAt + 10,
    elapsed_seconds: 10,
    snapshot: {},
    error: null,
    events_url: `/v1/runs/${runId}/events`,
    result_url: `/v1/runs/${runId}/result`,
  })

  it('hydration order cannot retitle: server row beats the fabrication', () => {
    let state = createEmptyProjectState()
    // Newest run arrives FIRST (hydration replays newest-first).
    state = researchDeskReducer(state, {
      hydration: true,
      summary: derivedTitleSummary('r-new', 2_000, 'Aktualisiere das Canvas-Dokument.'),
      type: 'upsertApiRunSummary',
    })
    const fabricated = state.agentSessions['s-derived-title']
    expect(fabricated.title).toBe('Aktualisiere das Canvas-Dokument.')
    expect(fabricated.updatedAt).toBe(DERIVED_AGENT_SESSION_UPDATED_AT)

    // The OLDER run converges the derived title on the first question.
    state = researchDeskReducer(state, {
      hydration: true,
      summary: derivedTitleSummary('r-old', 1_000, 'Erstelle im Canvas ein Dokument.'),
      type: 'upsertApiRunSummary',
    })
    expect(state.agentSessions['s-derived-title'].title).toBe(
      'Erstelle im Canvas ein Dokument.',
    )
    expect(state.agentSessions['s-derived-title'].updatedAt).toBe(
      DERIVED_AGENT_SESSION_UPDATED_AT,
    )

    // The REAL server row (stored title, real timestamp) always wins the
    // merge against the epoch-stamped fabrication.
    state = researchDeskReducer(state, {
      groups: [],
      sessions: [
        {
          id: 's-derived-title',
          title: 'Erstelle im Canvas (gespeichert)',
          group_id: null,
          created_at: 1_000,
          updated_at: 1_000,
        },
      ],
      type: 'upsertServerAgentSessions',
    })
    const settled = state.agentSessions['s-derived-title']
    expect(settled.title).toBe('Erstelle im Canvas (gespeichert)')
    expect(settled.updatedAt).toBe(new Date(1_000_000).toISOString())
    // Both runs stayed attached through the merge.
    expect(settled.runIds).toEqual(['r-new', 'r-old'])
  })

  it('a user rename lifts the epoch marker (record becomes pushable)', () => {
    let state = researchDeskReducer(createEmptyProjectState(), {
      hydration: true,
      summary: derivedTitleSummary('r-new', 2_000, 'Aktualisiere das Canvas-Dokument.'),
      type: 'upsertApiRunSummary',
    })
    state = researchDeskReducer(state, {
      sessionId: 's-derived-title',
      title: 'Mein Batterie-Dossier',
      type: 'renameAgentSession',
    })
    const renamed = state.agentSessions['s-derived-title']
    expect(renamed.title).toBe('Mein Batterie-Dossier')
    expect(renamed.updatedAt).not.toBe(DERIVED_AGENT_SESSION_UPDATED_AT)
    // And a LATER older-run summary must not undo the rename.
    state = researchDeskReducer(state, {
      hydration: true,
      summary: derivedTitleSummary('r-old', 1_000, 'Erstelle im Canvas ein Dokument.'),
      type: 'upsertApiRunSummary',
    })
    expect(state.agentSessions['s-derived-title'].title).toBe('Mein Batterie-Dossier')
  })
})

describe('a finished result that cannot be loaded', () => {
  const completedRun = () => researchDeskReducer(createEmptyProjectState(), {
    summary: makeRunSummary({
      finished_at: Date.parse('2026-01-01T00:05:00.000Z') / 1000,
      run_id: 'run-done',
      status: 'completed',
    }),
    type: 'upsertApiRunSummary',
  })

  it('keeps a completed run completed and records why its report is missing', () => {
    // A timed-out or failed result request says nothing about the run: it
    // completed on the server. Earlier clients rewrote it to "failed".
    const failedLoad = researchDeskReducer(completedRun(), {
      message: 'Zeitüberschreitung',
      runId: 'run-done',
      type: 'markApiRunResultError',
    })
    expect(failedLoad.researchRuns['run-done'].status).toBe('completed')
    expect(failedLoad.researchRuns['run-done'].error).toBeUndefined()
    expect(failedLoad.researchRuns['run-done'].resultError).toBe('Zeitüberschreitung')
  })

  it('clears the load error once the result arrives', () => {
    const failedLoad = researchDeskReducer(completedRun(), {
      message: 'Zeitüberschreitung',
      runId: 'run-done',
      type: 'markApiRunResultError',
    })
    const loaded = researchDeskReducer(failedLoad, {
      result: {
        ...storedResult,
        metrics: {
          aspect_coverage: 0, answer_bound_claims_count: 0, claims: { quality_score: 0, status_counts: {} },
          completion_tokens: 0, confidence: 8, elapsed_seconds: 2, evidence_consistency: 0,
          evidence_contract_status: 'unknown', evidence_sufficiency: 0, prompt_tokens: 0, rounds: 1,
          sources: { quality_score: 0, tier_counts: {} }, total_citations: 0, total_queries: 1,
          unbound_answer_citations_count: 0, verified_claims_used_count: 0,
        },
        references: [],
        run_id: 'run-done',
      } as unknown as ResearchRunResult,
      type: 'attachApiRunResult',
    })
    expect(loaded.researchRuns['run-done'].resultError).toBeUndefined()
    expect(loaded.researchRuns['run-done'].result).toBeDefined()
  })

  it('leaves a completed ask and its session time as they are', () => {
    const base = createEmptyProjectState()
    const sessionId = base.selectedKnowledgeSessionId as string
    const seeded = {
      ...base,
      dirty: false,
      knowledgeItemOrder: ['ki-1'],
      knowledgeItems: {
        'ki-1': makeKnowledgeItem('ki-1', sessionId, {
          answer: storedAnswer,
          completedAt: '2026-01-02T00:03:00.000Z',
          status: 'completed',
        }),
      },
      knowledgeSessions: {
        ...base.knowledgeSessions,
        [sessionId]: { ...base.knowledgeSessions[sessionId], updatedAt: '2026-01-02T00:03:00.000Z' },
      },
    }
    expect(researchDeskReducer(seeded, {
      message: 'Zeitüberschreitung',
      runId: 'run-ki-1',
      type: 'markApiRunResultError',
    })).toBe(seeded)
  })

  it('still settles an ask that never received its answer as failed', () => {
    const base = createEmptyProjectState()
    const sessionId = base.selectedKnowledgeSessionId as string
    const seeded = {
      ...base,
      knowledgeItemOrder: ['ki-1'],
      knowledgeItems: { 'ki-1': makeKnowledgeItem('ki-1', sessionId) },
    }
    const failed = researchDeskReducer(seeded, {
      message: 'Zeitüberschreitung',
      runId: 'run-ki-1',
      type: 'markApiRunResultError',
    })
    expect(failed.knowledgeItems['ki-1'].status).toBe('failed')
    expect(failed.knowledgeItems['ki-1'].error).toBe('Zeitüberschreitung')
  })
})
