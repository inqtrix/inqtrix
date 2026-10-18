import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  getAgentRunPlan,
  listAgentRunApprovals,
  listAgentRunArtifacts,
  listAgentRunClarifications,
  listAgentSessionArtifacts,
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
  getAgentRunPlan: vi.fn(),
  listAgentRunApprovals: vi.fn(),
  listAgentRunArtifacts: vi.fn(),
  listAgentRunClarifications: vi.fn(),
  listAgentSessionArtifacts: vi.fn(),
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
  vi.mocked(getAgentRunPlan).mockReset().mockResolvedValue(null as never)
  vi.mocked(listAgentRunApprovals).mockReset().mockResolvedValue([] as never)
  vi.mocked(listAgentRunArtifacts).mockReset().mockResolvedValue([] as never)
  vi.mocked(listAgentRunClarifications).mockReset().mockResolvedValue([] as never)
  vi.mocked(listAgentSessionArtifacts).mockReset().mockResolvedValue({ data: [] } as never)
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
