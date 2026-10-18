import { describe, expect, it } from 'vitest'

import { routeAgentRunToView } from './followTarget'
import type { AgentArtifactRecord, AgentRunRecord } from './model'

function run(overrides: Partial<AgentRunRecord> = {}): AgentRunRecord {
  return {
    runId: 'run_agent_1',
    kind: 'agent',
    question: 'Marktanalyse',
    status: 'running',
    phase: 'intake',
    station: 'intake',
    createdAt: '2026-07-02T10:00:00.000Z',
    lastSequence: 0,
    planStale: false,
    approvals: [],
    approvalsStale: false,
    clarifications: [],
    clarificationsStale: false,
    artifactOrder: [],
    artifacts: {},
    artifactsStale: false,
    taskStates: {},
    children: {},
    childGates: {},
    stepLog: [],
    touchedArtifacts: [],
    patchStale: false,
    ...overrides,
  }
}

function memo(status: 'writing' | 'ready'): AgentArtifactRecord {
  return {
    artifactId: 'memo1',
    kind: 'memo',
    title: 'Memo',
    status,
    revision: 2,
    updatedBy: 'agent',
    refsCount: 3,
    createdAt: 1,
    updatedAt: 2,
  }
}

function deliverable(status: 'writing' | 'ready'): AgentArtifactRecord {
  return {
    artifactId: 'deliverable1',
    kind: 'deliverable',
    title: 'Kurz-Memo',
    status,
    revision: 1,
    updatedBy: 'agent',
    refsCount: 2,
    createdAt: 1,
    updatedAt: 2,
  }
}

describe('routeAgentRunToView', () => {
  it('routes nothing during intake/discovery (no file target without ids)', () => {
    expect(routeAgentRunToView(run())).toBeNull()
    expect(routeAgentRunToView(run({ phase: 'discovery', station: 'discovery' }))).toBeNull()
  })

  it('targets the plan while planning or waiting for approval, open-only', () => {
    const planning = routeAgentRunToView(run({ phase: 'planning', station: 'planning' }))
    expect(planning).toEqual({
      descriptor: { view: 'plan', runId: 'run_agent_1' },
      urgency: 'open-only',
    })
    const waiting = routeAgentRunToView(run({ status: 'waiting_for_approval', phase: 'planning', station: 'planning' }))
    expect(waiting?.urgency).toBe('open-only')
  })

  it('keeps execution on the control-room overview', () => {
    const record = run({
      phase: 'execution',
      station: 'execution',
      taskStates: {
        t2: { status: 'running', attempt: 1 },
        t1: { status: 'running', attempt: 1 },
      },
      plan: {
        planId: 'p1', version: 1, status: 'approved', createdBy: 'agent',
        reason: '', createdAt: 1, summaryMarkdown: '', assumptions: [],
        successCriteria: [], versions: [],
        tasks: [
          { taskId: 't1', ordinal: 0, title: 'A', toolKind: 'web_research', objective: '', queries: [], gapIds: [], dependsOn: [], budget: {}, params: {}, expectedOutput: '', status: '', childRunId: null, resultSummary: '' },
          { taskId: 't2', ordinal: 1, title: 'B', toolKind: 'rag_query', objective: '', queries: [], gapIds: [], dependsOn: [], budget: {}, params: {}, expectedOutput: '', status: '', childRunId: null, resultSummary: '' },
        ],
      },
    })
    expect(routeAgentRunToView(record)).toEqual({
      descriptor: { view: 'run', runId: 'run_agent_1' },
      urgency: 'open-only',
    })
  })

  it('the streaming memo wins against everything with synthesis urgency', () => {
    const record = run({
      phase: 'execution',
      station: 'execution',
      taskStates: { t1: { status: 'running', attempt: 1 } },
      artifactOrder: ['memo1'],
      artifacts: { memo1: memo('writing') },
    })
    expect(routeAgentRunToView(record)).toEqual({
      descriptor: { view: 'document', runId: 'run_agent_1', artifactId: 'memo1' },
      urgency: 'synthesis',
    })
  })

  it('a pending PATCH gate targets the patch view, not the plan', () => {
    const record = run({
      status: 'waiting_for_approval',
      phase: 'patch',
      station: 'critic',
      patchId: 'pch_1',
      approvals: [{
        approvalId: 'apr_1',
        kind: 'patch',
        status: 'pending',
        subjectType: 'editor_patch',
        subjectId: 'pch_1',
        payload: {},
        decision: '',
        note: '',
        createdAt: 1,
        decidedAt: null,
      }],
    })
    expect(routeAgentRunToView(record)).toEqual({
      descriptor: { view: 'patch', runId: 'run_agent_1', patchId: 'pch_1' },
      urgency: 'open-only',
    })
  })

  it('a completed run targets the memo with auto-open urgency', () => {
    const record = run({
      status: 'completed',
      phase: 'done',
      station: 'critic',
      artifactOrder: ['memo1'],
      artifacts: { memo1: memo('ready') },
    })
    expect(routeAgentRunToView(record)).toEqual({
      descriptor: { view: 'document', runId: 'run_agent_1', artifactId: 'memo1' },
      urgency: 'auto-open',
    })
  })

  it('a completed kernel run targets its deliverable document (no memo)', () => {
    const record = run({
      status: 'completed',
      phase: 'done',
      station: 'critic',
      artifactOrder: ['deliverable1'],
      artifacts: { deliverable1: deliverable('ready') },
    })
    expect(routeAgentRunToView(record)).toEqual({
      descriptor: { view: 'document', runId: 'run_agent_1', artifactId: 'deliverable1' },
      urgency: 'auto-open',
    })
  })

  it('memo precedence: a run with both memo and deliverable targets the memo', () => {
    const record = run({
      status: 'completed',
      phase: 'done',
      station: 'critic',
      artifactOrder: ['deliverable1', 'memo1'],
      artifacts: { deliverable1: deliverable('ready'), memo1: memo('ready') },
    })
    expect(routeAgentRunToView(record)?.descriptor).toEqual({
      view: 'document', runId: 'run_agent_1', artifactId: 'memo1',
    })
  })
})
