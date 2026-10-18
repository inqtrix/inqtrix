import { describe, expect, it } from 'vitest'

import { pendingGate } from './ComposerGateTray'
import { applyAgentRunEvent } from './events'
import { agentRunFromSummary } from './model'
import type {
  AgentApprovalRecord,
  AgentClarificationRecord,
  AgentRunRecord,
} from './model'
import type {
  ResearchRunEvent,
  ResearchRunSummary,
} from '@/features/researchRuns/types'

function summary(overrides: Partial<ResearchRunSummary> = {}): ResearchRunSummary {
  return {
    access: { mode: 'owner' },
    run_id: 'run_parent_1',
    status: 'waiting_for_children',
    queue_position: null,
    question: 'Delegiere eine Tiefenrecherche.',
    stack: 'default',
    mode: 'agent_kernel',
    kind: 'agent',
    agent_overrides: { autonomy: 'balanced' },
    created_at: 1_700_000_000,
    started_at: 1_700_000_001,
    finished_at: null,
    elapsed_seconds: null,
    snapshot: { current_node: 'agent_kernel', last_message: '' },
    error: null,
    events_url: '/v1/runs/run_parent_1/events',
    result_url: '/v1/runs/run_parent_1/result',
    ...overrides,
  }
}

function event(
  sequence: number,
  type: string,
  data: Record<string, unknown> = {},
): ResearchRunEvent {
  return {
    type,
    run_id: 'run_parent_1',
    sequence,
    created_at: 1_700_000_100 + sequence,
    data,
  }
}

function childProgress(
  sequence: number,
  runStatus: string,
): ResearchRunEvent {
  return event(sequence, 'inqtrix.agent.child.progress', {
    child_run_id: 'run_child_1',
    run_status: runStatus,
    task_id: 'call_abc',
  })
}

function approval(
  overrides: Partial<AgentApprovalRecord> = {},
): AgentApprovalRecord {
  return {
    approvalId: 'apr_child_plan_0',
    kind: 'plan',
    status: 'pending',
    subjectType: 'plan',
    subjectId: 'plan_1',
    payload: {},
    decision: '',
    note: '',
    createdAt: 1_700_000_050,
    decidedAt: null,
    ...overrides,
  }
}

function clarification(
  overrides: Partial<AgentClarificationRecord> = {},
): AgentClarificationRecord {
  return {
    clarificationId: 'clr_child_1',
    question: 'Welchen Zeitraum soll die Recherche abdecken?',
    options: [],
    questions: [],
    answers: {},
    status: 'pending',
    answer: '',
    optionId: '',
    defaultAssumption: '',
    createdAt: 1_700_000_050,
    answeredAt: null,
    ...overrides,
  }
}

function parentWithParkedChild(
  gates: Partial<AgentRunRecord['childGates'][string]> = {},
): AgentRunRecord {
  const base = applyAgentRunEvent(
    agentRunFromSummary(summary()),
    childProgress(1, 'waiting_for_approval'),
  )
  return {
    ...base,
    childGates: {
      run_child_1: {
        approvals: [],
        clarifications: [],
        stale: false,
        ...gates,
      },
    },
  }
}

describe('child gate staleness (events)', () => {
  it('flags the child gates when a child parks on a human decision', () => {
    const next = applyAgentRunEvent(
      agentRunFromSummary(summary()),
      childProgress(1, 'waiting_for_approval'),
    )
    expect(next.childGates.run_child_1?.stale).toBe(true)
  })

  it('flags only the transition, not repeated progress while parked', () => {
    const parked = applyAgentRunEvent(
      agentRunFromSummary(summary()),
      childProgress(1, 'waiting_for_approval'),
    )
    const settled = {
      ...parked,
      childGates: {
        run_child_1: { approvals: [], clarifications: [], stale: false },
      },
    }
    const next = applyAgentRunEvent(
      settled,
      childProgress(2, 'waiting_for_approval'),
    )
    expect(next.childGates.run_child_1?.stale).toBe(false)
  })

  it('does not flag a working child', () => {
    const next = applyAgentRunEvent(
      agentRunFromSummary(summary()),
      childProgress(1, 'running'),
    )
    expect(next.childGates.run_child_1).toBeUndefined()
  })
})

describe('pendingGate over children', () => {
  it('surfaces a parked child approval with the child route', () => {
    const run = parentWithParkedChild({ approvals: [approval()] })
    const gate = pendingGate(run)
    expect(gate).toMatchObject({
      childRunId: 'run_child_1',
      kind: 'child_approval',
    })
  })

  it('labels the child gate with the FULL delegated question', () => {
    // Every other parent-side source (args preview, progress
    // message) is truncated — approving on a cut-off sentence is
    // approving blind, so the fetched run-row question wins.
    const fullQuestion = 'Erstelle ein vollständiges, belastbares Memo zum '
      + 'Stand der Batteriepass-Umsetzung entlang aller Dimensionen. '.repeat(8)
    const run = parentWithParkedChild({
      approvals: [approval()],
      question: fullQuestion,
    })
    const gate = pendingGate(run)
    expect(gate).toMatchObject({ kind: 'child_approval' })
    expect(
      gate && 'childLabel' in gate ? gate.childLabel : '',
    ).toBe(fullQuestion)
  })

  it('prefers a child clarification over a child approval', () => {
    const run = parentWithParkedChild({
      approvals: [approval()],
      clarifications: [clarification()],
    })
    expect(pendingGate(run)?.kind).toBe('child_clarification')
  })

  it('keeps the run own gate ahead of any child gate', () => {
    const run = {
      ...parentWithParkedChild({ approvals: [approval()] }),
      approvals: [approval({ approvalId: 'apr_root_tool', kind: 'tool' })],
      status: 'waiting_for_approval' as const,
    }
    expect(pendingGate(run)?.kind).toBe('tool')
  })

  it('offers nothing once the child resumed, despite stale pending rows', () => {
    // The child decided elsewhere resumes via child.progress; the local
    // pending row must not re-open a gate the child has already left.
    const run = applyAgentRunEvent(
      parentWithParkedChild({ approvals: [approval()] }),
      childProgress(2, 'running'),
    )
    expect(pendingGate(run)).toBeNull()
  })
})
