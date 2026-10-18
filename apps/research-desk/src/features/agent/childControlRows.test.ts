import { describe, expect, it, vi } from 'vitest'
import { getAgentRunPlan } from '@/api/inqtrixClient'
import { agentRunFromSummary } from './model'
import { useAgentControlApi } from './useAgentControlApi'

// Execute the hook's initial fetch effect without introducing a DOM test runtime.
// Stream lifetime and navigation are verified in the visible browser.
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
}))

describe('inspected mission control rows', () => {
  it('fetches the durable plan for an inspected child through the real control effect', async () => {
    const run = agentRunFromSummary({
      run_id: 'child', parent_run_id: 'root', root_run_id: 'root', kind: 'agent_child', mode: 'workspace_agent',
      question: 'Read existing sources', access: { mode: 'owner' }, stack: 'default', status: 'completed',
      agent_overrides: {}, created_at: 1, started_at: 1, finished_at: 2, elapsed_seconds: 1,
      queue_position: null, snapshot: {}, error: null, events_url: '/events', result_url: '/result',
    })
    run.approvalsStale = false
    run.clarificationsStale = false
    run.artifactsStale = false
    const plan = {
      plan_id: 'plan', run_id: 'child', version: 1, status: 'approved', created_by: 'agent' as const,
      reason: '', created_at: 1, summary_markdown: 'Read sources', assumptions: [], success_criteria: [], tasks: [], versions: [],
    }
    vi.mocked(getAgentRunPlan).mockResolvedValue(plan)
    const dispatch = vi.fn()
    useAgentControlApi({ apiKey: undefined, workspaceId: 'workspace-owner', enabled: true, runs: { child: run }, selectedSessionId: null, dispatch, onEditorDocumentsChanged: vi.fn() })
    await vi.waitFor(() => expect(dispatch).toHaveBeenCalledWith({ type: 'setAgentRunPlan', runId: 'child', plan }))
    // The request carries the signal its deadline cancels it through.
    expect(getAgentRunPlan).toHaveBeenCalledWith('child', undefined, {
      apiKey: undefined,
      signal: expect.any(AbortSignal),
      workspaceId: 'workspace-owner',
    })
  })
})
