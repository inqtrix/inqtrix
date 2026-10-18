import { describe, expect, it } from 'vitest'

import { prefetchableTaskResultIds } from './taskResultPrefetch'
import type { AgentPlanTaskRecord, AgentRunRecord } from './model'

function makeTask(
  taskId: string,
  status: string,
  toolKind = 'web_instant',
): AgentPlanTaskRecord {
  return {
    taskId,
    status,
    toolKind,
  } as unknown as AgentPlanTaskRecord
}

const NO_LIVE_STATE: AgentRunRecord['taskStates'] = {}

describe('prefetchableTaskResultIds', () => {
  it('returns only settled tasks, excluding synthesis', () => {
    const tasks = [
      makeTask('t1', 'completed'),
      makeTask('t2', 'running'),
      makeTask('t3', 'failed'),
      makeTask('t4', 'completed', 'synthesis'),
      makeTask('t5', 'pending'),
      makeTask('t6', 'cancelled'),
    ]
    expect(prefetchableTaskResultIds(tasks, NO_LIVE_STATE, 10)).toEqual([
      't1',
      't3',
      't6',
    ])
  })

  it('caps the candidate list', () => {
    const tasks = Array.from({ length: 9 }, (_, index) =>
      makeTask(`t${index}`, 'completed'))
    expect(prefetchableTaskResultIds(tasks, NO_LIVE_STATE, 6)).toHaveLength(6)
  })

  it('prefers the LIVE task state over the plan row status', () => {
    const tasks = [makeTask('t1', 'completed')]
    const live = {
      t1: { status: 'running' },
    } as unknown as AgentRunRecord['taskStates']
    expect(prefetchableTaskResultIds(tasks, live, 6)).toEqual([])
  })
})
