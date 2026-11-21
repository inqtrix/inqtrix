import { describe, expect, it } from 'vitest'

import { agentRunFromSummary } from './model'
import type { AgentArtifactRecord } from './model'
import type { ResearchRunSummary } from '@/features/researchRuns/types'
import {
  agentTranscriptHydrated,
  currentAgentControlFetchFailures,
} from './useAgentControlApi'

function agentRun() {
  return agentRunFromSummary({
    access: { mode: 'owner' },
    agent_overrides: {},
    created_at: 1,
    elapsed_seconds: null,
    error: null,
    events_url: '/events',
    finished_at: null,
    kind: 'agent',
    mode: 'agent_kernel',
    question: 'Research',
    queue_position: null,
    run_id: 'run-1',
    snapshot: {},
    stack: 'default',
    started_at: null,
    status: 'completed',
  } satisfies ResearchRunSummary)
}

describe('agentTranscriptHydrated', () => {
  it('waits for initial control rows and the terminal answer body', () => {
    const initial = agentRun()
    expect(agentTranscriptHydrated(initial)).toBe(false)

    const answer = {
      artifactId: 'answer-1',
      kind: 'answer',
      revision: 2,
      status: 'ready',
    } as AgentArtifactRecord
    const rowsLoaded = {
      ...initial,
      approvalsStale: false,
      artifactOrder: [answer.artifactId],
      artifacts: { [answer.artifactId]: answer },
      artifactsStale: false,
      clarificationsStale: false,
    }
    expect(agentTranscriptHydrated(rowsLoaded)).toBe(false)
    expect(agentTranscriptHydrated(
      rowsLoaded,
      new Set(['run-1:answer:answer-1:2']),
    )).toBe(true)
    expect(agentTranscriptHydrated({
      ...rowsLoaded,
      artifacts: {
        [answer.artifactId]: { ...answer, contentMarkdown: 'Fertig.' },
      },
    })).toBe(true)
  })
})

describe('currentAgentControlFetchFailures', () => {
  it('suppresses the active failed cycle but re-arms a later invalidation', () => {
    const stale = agentRun()
    const failed = new Set(['run-1:approvals'])

    const sameCycle = currentAgentControlFetchFailures(
      { [stale.runId]: stale },
      failed,
    )
    expect(sameCycle).toEqual(failed)

    const settled = { ...stale, approvalsStale: false }
    const afterSettlement = currentAgentControlFetchFailures(
      { [settled.runId]: settled },
      sameCycle,
    )
    expect(afterSettlement).toEqual(new Set())

    const invalidatedAgain = { ...settled, approvalsStale: true }
    expect(currentAgentControlFetchFailures(
      { [invalidatedAgain.runId]: invalidatedAgain },
      afterSettlement,
    )).toEqual(new Set())
  })

  it('tracks a child-gate fetch failure by its stale cycle', () => {
    const parked = {
      ...agentRun(),
      childGates: {
        'run-child': { approvals: [], clarifications: [], stale: true },
      },
    }
    const failed = new Set(['run-1:child_gates:run-child'])
    expect(currentAgentControlFetchFailures(
      { [parked.runId]: parked },
      failed,
    )).toEqual(failed)

    const settled = {
      ...parked,
      childGates: {
        'run-child': { approvals: [], clarifications: [], stale: false },
      },
    }
    expect(currentAgentControlFetchFailures(
      { [settled.runId]: settled },
      failed,
    )).toEqual(new Set())
  })

  it('re-arms answer loading when its row revision changes', () => {
    const answer = {
      artifactId: 'answer-1',
      kind: 'answer',
      revision: 2,
      status: 'ready',
    } as AgentArtifactRecord
    const run = {
      ...agentRun(),
      approvalsStale: false,
      artifactOrder: [answer.artifactId],
      artifacts: { [answer.artifactId]: answer },
      artifactsStale: false,
      clarificationsStale: false,
    }

    const sameRevision = currentAgentControlFetchFailures(
      { [run.runId]: run },
      new Set(['run-1:answer:answer-1:2']),
    )
    expect(sameRevision).toEqual(new Set(['run-1:answer:answer-1:2']))

    const newer = {
      ...run,
      artifacts: {
        [answer.artifactId]: { ...answer, revision: 3 },
      },
    }
    expect(currentAgentControlFetchFailures(
      { [newer.runId]: newer },
      sameRevision,
    )).toEqual(new Set())
  })
})
