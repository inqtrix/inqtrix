import { describe, expect, it } from 'vitest'

import { agentScrollKey, agentTranscriptVersion, transcriptSettled } from './agentScroll'
import type { AgentRunRecord } from './model'

function run(overrides: Partial<AgentRunRecord> = {}): AgentRunRecord {
  return {
    runId: 'run-1',
    kind: 'agent',
    question: 'Frage',
    status: 'running',
    phase: 'execution',
    station: 'intake',
    createdAt: '2026-08-30T10:00:00.000Z',
    lastSequence: 1,
    planStale: false,
    approvals: [],
    ...overrides,
  } as AgentRunRecord
}

describe('agentScrollKey', () => {
  it('namespaces like the other desks', () => {
    expect(agentScrollKey('sess-1')).toBe('agent:sess-1')
  })

  it('has no identity without a session', () => {
    // A null key disables restoration — the empty state has no scroll
    // target and must not inherit the previous session's position.
    expect(agentScrollKey(null)).toBeNull()
    expect(agentScrollKey('   ')).toBeNull()
  })
})

describe('agentTranscriptVersion', () => {
  it('changes when a run gains events', () => {
    const before = agentTranscriptVersion([run({ lastSequence: 7 })])
    const after = agentTranscriptVersion([run({ lastSequence: 8 })])
    expect(after).not.toBe(before)
  })

  it('changes when a run finishes', () => {
    // The recap and the file rows appear on completion — that is growth
    // too, even when no further event follows.
    const before = agentTranscriptVersion([run({ status: 'running' })])
    const after = agentTranscriptVersion([run({ status: 'completed' })])
    expect(after).not.toBe(before)
  })

  it('changes when a turn is added', () => {
    const before = agentTranscriptVersion([run()])
    const after = agentTranscriptVersion([run(), run({ runId: 'run-2' })])
    expect(after).not.toBe(before)
  })

  it('notices growth on an OLDER run, not just the last one', () => {
    // A late artifact update lands on an earlier turn; following must
    // not miss it just because the newest run is quiet.
    const before = agentTranscriptVersion([
      run({ lastSequence: 3 }),
      run({ runId: 'run-2', lastSequence: 9 }),
    ])
    const after = agentTranscriptVersion([
      run({ lastSequence: 4 }),
      run({ runId: 'run-2', lastSequence: 9 }),
    ])
    expect(after).not.toBe(before)
  })

  it('stays stable when nothing changed', () => {
    // A version that churns on every render would follow the user down
    // even when they scrolled up deliberately.
    expect(agentTranscriptVersion([run(), run({ runId: 'run-2' })])).toBe(
      agentTranscriptVersion([run(), run({ runId: 'run-2' })]),
    )
  })
})

describe('transcriptSettled', () => {
  it('is true exactly when an active run in the previous version has ended', () => {
    expect(transcriptSettled([run({ status: 'running' })], [run({ status: 'completed' })])).toBe(true)
    expect(transcriptSettled([run({ status: 'waiting_for_approval' })], [run({ status: 'cancelled' })])).toBe(true)
  })

  it('is false while the run is still active, for plain appends and without history', () => {
    expect(transcriptSettled([run({ status: 'running' })], [run({ status: 'running', lastSequence: 9 })])).toBe(false)
    expect(transcriptSettled([run({ status: 'completed' })], [run({ status: 'completed' }), run({ runId: 'run-2', status: 'running' })])).toBe(false)
    expect(transcriptSettled(null, [run({ status: 'completed' })])).toBe(false)
  })
})
