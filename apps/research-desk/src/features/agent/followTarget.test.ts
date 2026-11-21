import { describe, expect, it } from 'vitest'

import { fallbackCanvasDescriptor, routeAgentRunToView } from './followTarget'
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
    approvals: [],
    approvalsStale: false,
    clarifications: [],
    clarificationsStale: false,
    artifactOrder: [],
    artifacts: {},
    artifactsStale: false,
    children: {},
    childGates: {},
    stepLog: [],
    touchedArtifacts: [],
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
  it('routes nothing during intake (no file target without ids)', () => {
    expect(routeAgentRunToView(run())).toBeNull()
  })

  it('routes nothing for a waiting run off the kernel line', () => {
    // A parked run of the retired mission engine had a plan view to show;
    // no such view exists any more.
    expect(routeAgentRunToView(run({ status: 'waiting_for_approval', phase: 'planning' }))).toBeNull()
  })

  it('keeps execution on the control-room overview, also while a gate waits', () => {
    for (const status of ['running', 'waiting_for_approval'] as const) {
      expect(routeAgentRunToView(run({ phase: 'execution', station: 'execution', status }))).toEqual({
        descriptor: { view: 'run', runId: 'run_agent_1' },
        urgency: 'open-only',
      })
    }
  })

  it('the streaming memo wins against everything with synthesis urgency', () => {
    const record = run({
      phase: 'execution',
      station: 'execution',
      artifactOrder: ['memo1'],
      artifacts: { memo1: memo('writing') },
    })
    expect(routeAgentRunToView(record)).toEqual({
      descriptor: { view: 'document', runId: 'run_agent_1', artifactId: 'memo1' },
      urgency: 'synthesis',
    })
  })

  it('a completed run targets the memo with auto-open urgency', () => {
    const record = run({
      status: 'completed',
      phase: 'done',
      station: 'execution',
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
      station: 'execution',
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
      station: 'execution',
      artifactOrder: ['deliverable1', 'memo1'],
      artifacts: { deliverable1: deliverable('ready'), memo1: memo('ready') },
    })
    expect(routeAgentRunToView(record)?.descriptor).toEqual({
      view: 'document', runId: 'run_agent_1', artifactId: 'memo1',
    })
  })
})

describe('the view the canvas opens on without a tab', () => {
  it('leaves a session without a run to the empty canvas', () => {
    expect(fallbackCanvasDescriptor(undefined)).toBeNull()
  })

  it('opens a settled run without a document on its run view', () => {
    // No view of the retired plan stands behind a settled run any more.
    expect(fallbackCanvasDescriptor(run({ phase: 'done', status: 'completed' })))
      .toEqual({ runId: 'run_agent_1', view: 'run' })
  })

  it('opens the document of a run, the memo of a stored mission run first', () => {
    expect(fallbackCanvasDescriptor(run({
      artifactOrder: ['deliverable1'],
      artifacts: { deliverable1: deliverable('ready') },
      status: 'completed',
    }))).toEqual({ artifactId: 'deliverable1', runId: 'run_agent_1', view: 'document' })
    expect(fallbackCanvasDescriptor(run({
      artifactOrder: ['deliverable1', 'memo1'],
      artifacts: { deliverable1: deliverable('ready'), memo1: memo('ready') },
      status: 'completed',
    }))).toEqual({ artifactId: 'memo1', runId: 'run_agent_1', view: 'document' })
  })
})
