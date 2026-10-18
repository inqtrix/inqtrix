import { beforeEach, describe, expect, it, vi } from 'vitest'
import { fetchResearchRunSummary } from '@/api/inqtrixClient'
import { subscribeRunEvents } from '@/features/researchRuns/runEventChannel'
import type { ResearchRunSummary } from '@/features/researchRuns/types'
import { createEmptyProjectState } from '@/features/project/seedProject'
import { researchDeskReducer } from '@/features/researchDesk/state'
import { agentRunFromSummary } from './model'
import { inspectAgentChild } from './useAgentControlApi'

vi.mock('@/api/inqtrixClient', async (original) => ({ ...await original<typeof import('@/api/inqtrixClient')>(), fetchResearchRunSummary: vi.fn() }))
vi.mock('@/features/researchRuns/runEventChannel', async (original) => ({ ...await original<typeof import('@/features/researchRuns/runEventChannel')>(), subscribeRunEvents: vi.fn() }))

function childSummary(): ResearchRunSummary {
  return {
    run_id: 'child', root_run_id: 'root', parent_run_id: 'root', kind: 'agent_child', mode: 'workspace_agent',
    question: 'Inspect sources', access: { mode: 'owner' }, stack: 'default', status: 'waiting_for_approval',
    agent_overrides: {}, created_at: 1, started_at: 1, finished_at: null, elapsed_seconds: null,
    queue_position: null, snapshot: {}, error: null, events_url: '/v1/runs/child/events', result_url: '/v1/runs/child/result',
  }
}

beforeEach(() => vi.resetAllMocks())

describe('inspected delegated runs', () => {
  it('loads the selected child under caller scope and keeps replay distinct from new events', async () => {
    const summary = childSummary()
    vi.mocked(fetchResearchRunSummary).mockResolvedValue(summary)
    const options = { workspaceId: 'workspace-owner', signal: new AbortController().signal }
    const dispatch = vi.fn()
    vi.mocked(subscribeRunEvents).mockImplementation(async (channel) => {
      expect(channel.options).toBe(options)
      expect(channel.signal).toBe(options.signal)
      expect(channel.eventsUrl).toBe('/v1/runs/child/events')
      expect(channel.preferPolling?.()).toBe(true)
      channel.onHistory?.([{ type: 'inqtrix.run.resumed', run_id: 'child', sequence: 1, created_at: 2, data: {} }])
      expect(channel.preferPolling?.()).toBe(false)
      channel.onEvent({ type: 'inqtrix.run.waiting', run_id: 'child', sequence: 2, created_at: 3, data: { status: 'waiting_for_input' } })
      expect(channel.preferPolling?.()).toBe(true)
    })
    await inspectAgentChild('child', options, dispatch, vi.fn())
    expect(fetchResearchRunSummary).toHaveBeenCalledWith('child', options)
    expect(dispatch.mock.calls.map(([action]) => action.type)).toEqual(['upsertInspectedAgentChild', 'appendApiRunEvents', 'appendApiRunEvent'])
  })

  it('does not subscribe after closing the detail while its summary is loading', async () => {
    const controller = new AbortController()
    vi.mocked(fetchResearchRunSummary).mockImplementation(async () => { controller.abort(); return childSummary() })
    const dispatch = vi.fn()
    await inspectAgentChild('child', { signal: controller.signal }, dispatch, vi.fn())
    expect(dispatch).not.toHaveBeenCalled()
    expect(subscribeRunEvents).not.toHaveBeenCalled()
  })

  it('keeps unavailable children as an error rather than a finished history', async () => {
    vi.mocked(fetchResearchRunSummary).mockResolvedValue(childSummary())
    vi.mocked(subscribeRunEvents).mockImplementation(async (channel) => { channel.onUnavailable?.(404) })
    await expect(inspectAgentChild('child', { signal: new AbortController().signal }, vi.fn(), vi.fn())).rejects.toThrow('no longer available')
  })

  it('keeps child hydration out of sessions and ignores late results after their root disappears', () => {
    const summary = childSummary()
    const initial = createEmptyProjectState()
    const base = { ...initial, agentRuns: { root: agentRunFromSummary({ ...summary, kind: 'agent', run_id: 'root' }) } }
    const next = researchDeskReducer(base, { type: 'upsertInspectedAgentChild', summary })
    expect(next.agentRuns.child?.parentRunId).toBe('root')
    expect(next.agentSessions).toBe(base.agentSessions)
    expect(next.agentSessionOrder).toBe(base.agentSessionOrder)
    expect(next.dirty).toBe(base.dirty)
    expect(researchDeskReducer(initial, { type: 'upsertInspectedAgentChild', summary })).toBe(initial)
  })
})
