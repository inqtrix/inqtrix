import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  getAgentRunArtifact,
  getAgentRunPlan,
  listAgentRunApprovals,
  listAgentRunArtifacts,
  listAgentRunClarifications,
  listAgentSessionArtifacts,
  renameAgentRunArtifact,
} from '@/api/inqtrixClient'
import type { ResearchRunSummary } from '@/features/researchRuns/types'
import { agentRunFromSummary, type AgentRunRecord, type AgentSessionArtifactIndex } from './model'
import { agentControlScope, useAgentControlApi } from './useAgentControlApi'

// Execute the hook's fetch effect without a DOM test runtime (as in
// childControlRows.test.ts); the visible behaviour is checked live.
vi.mock('react', async (original) => ({
  ...await original<typeof import('react')>(),
  useRef: <T,>(value: T) => ({ current: value }),
  useCallback: <T,>(callback: T) => callback,
  useMemo: <T,>(factory: () => T) => factory(),
  useEffect: (effect: () => unknown) => { effect() },
}))
vi.mock('@/api/inqtrixClient', async (original) => ({
  ...await original<typeof import('@/api/inqtrixClient')>(),
  getAgentRunArtifact: vi.fn(),
  getAgentRunPlan: vi.fn(),
  listAgentRunApprovals: vi.fn(),
  listAgentRunArtifacts: vi.fn(),
  listAgentRunClarifications: vi.fn(),
  listAgentSessionArtifacts: vi.fn(),
  renameAgentRunArtifact: vi.fn(),
}))

const summary = (overrides: Partial<ResearchRunSummary>): ResearchRunSummary => ({
  access: { mode: 'owner' }, agent_overrides: {}, created_at: 1, elapsed_seconds: 1, error: null,
  events_url: '/events', finished_at: 2, kind: 'agent', mode: 'agent_kernel', question: 'Q',
  queue_position: null, result_url: '/result', run_id: 'run', snapshot: {}, stack: 'default',
  started_at: 1, status: 'completed', ...overrides,
} as ResearchRunSummary)

const staleRun = (overrides: Partial<ResearchRunSummary>): AgentRunRecord => {
  const run = agentRunFromSummary(summary(overrides))
  run.approvalsStale = true
  run.clarificationsStale = true
  run.artifactsStale = true
  run.planStale = false
  return run
}

const staleIndex = { stale: true } as unknown as AgentSessionArtifactIndex

beforeEach(() => {
  vi.mocked(getAgentRunArtifact).mockReset().mockResolvedValue({ artifact_id: 'artifact-1' } as never)
  vi.mocked(getAgentRunPlan).mockReset().mockResolvedValue(null as never)
  vi.mocked(listAgentRunApprovals).mockReset().mockResolvedValue([] as never)
  vi.mocked(listAgentRunArtifacts).mockReset().mockResolvedValue([] as never)
  vi.mocked(listAgentRunClarifications).mockReset().mockResolvedValue([] as never)
  vi.mocked(listAgentSessionArtifacts).mockReset().mockResolvedValue({ data: [] } as never)
  vi.mocked(renameAgentRunArtifact).mockReset().mockResolvedValue({} as never)
})

describe('control rows of the open session only', () => {
  it('names the runs of the open session and every inspected child mission', () => {
    const runs = {
      'run-a': staleRun({ run_id: 'run-a', session_id: 'session-a' }),
      'run-b': staleRun({ run_id: 'run-b', session_id: 'session-b' }),
      child: staleRun({ kind: 'agent_child', parent_run_id: 'run-b', root_run_id: 'run-b', run_id: 'child' }),
    }
    expect([...agentControlScope(runs, 'session-a')].sort()).toEqual(['child', 'run-a'])
    expect([...agentControlScope(runs, null)]).toEqual(['child'])
  })

  it('does not load the rows of runs in other sessions', async () => {
    // Opening the Agent Desk fetched approvals, clarifications and artifacts
    // for every agent run of every session at once (58 runs, 586 requests
    // after one reload), and the open session waited behind them.
    const dispatch = vi.fn()
    useAgentControlApi({
      apiKey: undefined,
      dispatch,
      enabled: true,
      onEditorDocumentsChanged: vi.fn(),
      runs: {
        'run-a': staleRun({ run_id: 'run-a', session_id: 'session-a' }),
        'run-b': staleRun({ run_id: 'run-b', session_id: 'session-b' }),
      },
      selectedSessionId: 'session-a',
      workspaceId: 'workspace-owner',
    })
    await vi.waitFor(() => expect(dispatch).toHaveBeenCalledTimes(3))
    expect(vi.mocked(listAgentRunApprovals).mock.calls.map((call) => call[0])).toEqual(['run-a'])
    expect(vi.mocked(listAgentRunClarifications).mock.calls.map((call) => call[0])).toEqual(['run-a'])
    expect(vi.mocked(listAgentRunArtifacts).mock.calls.map((call) => call[0])).toEqual(['run-a'])
  })

  it('loads the artifact index of the open session only', async () => {
    const dispatch = vi.fn()
    useAgentControlApi({
      apiKey: undefined,
      dispatch,
      enabled: true,
      onEditorDocumentsChanged: vi.fn(),
      runs: {},
      selectedSessionId: 'session-a',
      sessionArtifacts: { 'session-a': staleIndex, 'session-b': staleIndex },
      workspaceId: 'workspace-owner',
    })
    await vi.waitFor(() => expect(dispatch).toHaveBeenCalledTimes(1))
    expect(vi.mocked(listAgentSessionArtifacts).mock.calls.map((call) => call[0])).toEqual(['session-a'])
  })
})

describe('a session the server does not hold', () => {
  const viewId = 'shared-agent-view:run-shared'
  const sharedRun = () => staleRun({
    access: { mode: 'shared', permission: 'edit' },
    run_id: 'run-shared',
  })
  const control = (
    sessions: Record<string, { persistable?: boolean }>,
    dispatch = vi.fn(),
    sessionId = viewId,
    sessionArtifacts: Record<string, AgentSessionArtifactIndex> = { [sessionId]: staleIndex },
  ) => useAgentControlApi({
    apiKey: undefined,
    dispatch,
    enabled: true,
    onEditorDocumentsChanged: vi.fn(),
    runs: { 'run-shared': sharedRun() },
    selectedSessionId: sessionId,
    sessionArtifacts,
    sessions,
    workspaceId: 'workspace-owner',
  })
  const settled = () => new Promise((resolve) => setTimeout(resolve, 0))

  it('asks for no artifact list of a shared-run view', async () => {
    // The view is a local shell: `GET .../shared-agent-view:<id>/artifacts`
    // answered 404 on every run event and put an error on the session index.
    const dispatch = vi.fn()
    control({ [viewId]: { persistable: false } }, dispatch)
    await settled()
    expect(listAgentSessionArtifacts).not.toHaveBeenCalled()
    expect(dispatch).not.toHaveBeenCalledWith(
      expect.objectContaining({ type: 'markAgentSessionArtifactsError' }),
    )
  })

  it('still asks for the list of a real session', async () => {
    control({ 'session-a': {} }, vi.fn(), 'session-a')
    await settled()
    expect(vi.mocked(listAgentSessionArtifacts).mock.calls.map((call) => call[0]))
      .toEqual(['session-a'])
  })

  it('reports a rename as done without asking for the list of the view', async () => {
    // The rename succeeded on the server; a 404 from the list behind it turned
    // that into "rename failed" for a recipient with edit rights.
    vi.mocked(listAgentSessionArtifacts).mockRejectedValue(
      Object.assign(new Error('not found'), { status: 404 }),
    )
    const result = await control({ [viewId]: { persistable: false } })
      .renameArtifact('run-shared', 'artifact-1', 'Neuer Titel', viewId)
    expect(result).toEqual({ kind: 'renamed' })
    expect(renameAgentRunArtifact).toHaveBeenCalledTimes(1)
    expect(listAgentSessionArtifacts).not.toHaveBeenCalled()
  })

  it('refreshes the list after renaming in a real session', async () => {
    // No stale index here, so the request can only come from the rename.
    const dispatch = vi.fn()
    const result = await control({ 'session-a': {} }, dispatch, 'session-a', {})
      .renameArtifact('run-shared', 'artifact-1', 'Neuer Titel', 'session-a')
    expect(result).toEqual({ kind: 'renamed' })
    expect(vi.mocked(listAgentSessionArtifacts).mock.calls.map((call) => call[0]))
      .toEqual(['session-a'])
    expect(dispatch).toHaveBeenCalledWith(
      expect.objectContaining({ sessionId: 'session-a', type: 'setAgentSessionArtifacts' }),
    )
  })
})
