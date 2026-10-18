import { describe, expect, it } from 'vitest'

import {
  agentArtifactFromWire,
  agentCenterScreen,
  agentRunFromSummary,
  agentSessionHistoryTimeIso,
  canEditAgentRun,
  isActiveAgentRun,
  isGateAgentRun,
  resolveAgentArtifact,
  restoredAgentSessionId,
  sessionArtifactMetaFromWire,
} from './model'
import type { ResearchRunStatus, ResearchRunSummary } from '@/features/researchRuns/types'

describe('agent artifact detail projection', () => {
  it('retains the evidence payload only after the detail row is fetched', () => {
    const artifact = agentArtifactFromWire({
      artifact_id: 'artifact-evidence',
      content_markdown: '',
      created_at: 1,
      kind: 'evidence_bundle',
      payload: {
        schema_version: 1,
        web_search_ledger: {
          kind: 'web_search_ledger',
          schema_version: 1,
          searches: {
            'query-1': { provider_answer: 'Grounded answer', query_id: 'query-1' },
          },
        },
      },
      refs: [],
      refs_count: 0,
      revision: 1,
      revisions: [],
      run_id: 'run-1',
      session_id: null,
      status: 'ready',
      title: 'Evidence',
      updated_at: 2,
      updated_by: 'agent',
    })

    expect(artifact.payload).toEqual({
      schema_version: 1,
      web_search_ledger: {
        kind: 'web_search_ledger',
        schema_version: 1,
        searches: {
          'query-1': { provider_answer: 'Grounded answer', query_id: 'query-1' },
        },
      },
    })
  })
})

/**
 * THE one active/gate vocabulary for every agent surface (composer,
 * pills, rail, pulse track, canvas, transcript). The regression this
 * locks: surfaces hand-rolled their own status lists and drifted —
 * `waiting_for_children` (the NORMAL parked-on-children mid-execution
 * state) was treated as idle by half of them.
 */
describe('agent run status predicates', () => {
  const expectations: Record<
    ResearchRunStatus,
    { active: boolean; gate: boolean }
  > = {
    queued: { active: true, gate: false },
    running: { active: true, gate: false },
    waiting_for_approval: { active: true, gate: true },
    waiting_for_input: { active: true, gate: true },
    waiting_for_children: { active: true, gate: false },
    completed: { active: false, gate: false },
    failed: { active: false, gate: false },
    cancelled: { active: false, gate: false },
    expired: { active: false, gate: false },
  }

  it('classifies every status exactly once', () => {
    for (const [status, expected] of Object.entries(expectations)) {
      expect(
        isActiveAgentRun(status as ResearchRunStatus),
        `${status} active`,
      ).toBe(expected.active)
      expect(
        isGateAgentRun(status as ResearchRunStatus),
        `${status} gate`,
      ).toBe(expected.gate)
    }
  })

  it('a gate is always active (working = active && !gate)', () => {
    for (const [status, expected] of Object.entries(expectations)) {
      if (expected.gate) {
        expect(isActiveAgentRun(status as ResearchRunStatus)).toBe(true)
      }
    }
  })
})

describe('agent run permission gate', () => {
  const runWithAccess = (
    access: import('@/features/researchRuns/types').ResearchRunAccess,
  ) => ({ access }) as import('./model').AgentRunRecord

  it('blocks every mutation for view shares only', () => {
    expect(canEditAgentRun(undefined)).toBe(false)
    expect(canEditAgentRun(runWithAccess({ mode: 'owner' }))).toBe(true)
    expect(canEditAgentRun(runWithAccess({ mode: 'unscoped' }))).toBe(true)
    expect(canEditAgentRun(runWithAccess({
      mode: 'shared',
      permission: 'edit',
    }))).toBe(true)
    expect(canEditAgentRun(runWithAccess({
      mode: 'shared',
      permission: 'view',
    }))).toBe(false)
  })
})

function makeSession(
  id: string,
  updatedAt: string,
  runIds: string[] = [],
) {
  return {
    id,
    title: id,
    groupId: null,
    createdAt: '2026-07-01T00:00:00.000Z',
    updatedAt,
    runIds,
    sourcePolicy: { web: 'available' as const, knowledge: 'available' as const },
  }
}

describe('restoredAgentSessionId', () => {
  const sessions = {
    old: makeSession('old', '2026-07-01T10:00:00.000Z'),
    recent: makeSession('recent', '2026-07-10T10:00:00.000Z'),
  }

  it('prefers the persisted intent when the session still exists', () => {
    expect(
      restoredAgentSessionId('old', ['recent', 'old'], sessions, {}),
    ).toBe('old')
  })

  it('falls back to the most recently updated session for a stale intent', () => {
    expect(
      restoredAgentSessionId('gone', ['old', 'recent'], sessions, {}),
    ).toBe('recent')
  })

  it('falls back by recency with no intent, and to null with no sessions', () => {
    expect(
      restoredAgentSessionId(null, ['old', 'recent'], sessions, {}),
    ).toBe('recent')
    expect(restoredAgentSessionId(null, [], {}, {})).toBeNull()
  })

  it('conversation recency (latest run) beats a newer rename', () => {
    const withRuns = {
      old: makeSession('old', '2026-07-01T10:00:00.000Z', ['r-old']),
      recent: makeSession('recent', '2026-07-10T10:00:00.000Z'),
    }
    const runs = {
      'r-old': { createdAt: '2026-07-11T09:00:00.000Z' },
    } as unknown as Record<string, import('./model').AgentRunRecord>
    expect(
      restoredAgentSessionId(null, ['old', 'recent'], withRuns, runs),
    ).toBe('old')
  })

  it('never restores a session whose durable deletion is still visible', () => {
    const deleting = {
      ...sessions,
      recent: {
        ...sessions.recent,
        deletion: {
          error: null,
          operationId: 'del_1',
          stage: 'queued',
          status: 'deleting' as const,
        },
      },
    }
    expect(restoredAgentSessionId('recent', ['recent', 'old'], deleting, {})).toBe('old')
  })
})

describe('agentSessionHistoryTimeIso', () => {
  it('uses the latest run createdAt over the session updatedAt', () => {
    const session = makeSession('s', '2026-07-01T10:00:00.000Z', ['r1', 'r2'])
    const runs = {
      r1: { createdAt: '2026-07-02T10:00:00.000Z' },
      r2: { createdAt: '2026-07-09T10:00:00.000Z' },
    } as unknown as Record<string, import('./model').AgentRunRecord>
    expect(agentSessionHistoryTimeIso(session, runs)).toBe(
      '2026-07-09T10:00:00.000Z',
    )
  })

  it('skips unresolvable run ids and falls back to updatedAt', () => {
    const session = makeSession('s', '2026-07-01T10:00:00.000Z', ['missing'])
    expect(agentSessionHistoryTimeIso(session, {})).toBe(
      '2026-07-01T10:00:00.000Z',
    )
  })
})

describe('agentCenterScreen', () => {
  const base = {
    hasRuns: false,
    hasSelectedSession: false,
    runsHydrated: true,
    serverEnabled: true,
    sessionsKnown: true,
  }

  it('never shows the skeleton once the sessions are known (the view-switch flash)', () => {
    // The regression this pins: a view switch remounted the desk, reset a
    // component-local settled flag and re-armed the skeleton over data the
    // store still held. Known + empty is a real welcome, not a loading state.
    expect(agentCenterScreen({ ...base })).toBe('welcome')
  })

  it('shows the skeleton for a genuinely unknown first listing', () => {
    expect(agentCenterScreen({ ...base, sessionsKnown: false })).toBe('skeleton')
  })

  it('keeps the skeleton while a selected session pages its runs in', () => {
    expect(agentCenterScreen({
      ...base,
      hasSelectedSession: true,
      runsHydrated: false,
    })).toBe('skeleton')
  })

  it('renders the transcript as soon as runs exist, even mid-listing', () => {
    expect(agentCenterScreen({
      ...base,
      hasRuns: true,
      sessionsKnown: false,
    })).toBe('transcript')
  })

  it('never shows the skeleton without a server (demo mode)', () => {
    expect(agentCenterScreen({
      ...base,
      serverEnabled: false,
      sessionsKnown: false,
    })).toBe('welcome')
  })
})

/**
 * Anchor independence: an artifact update RE-ANCHORS the server row
 * to the updating run, so a descriptor's runId can go stale while the
 * artifact itself lives on. The resolver walks hint → same-session
 * runs → session index and returns the runId every artifact API call
 * must use.
 */
describe('resolveAgentArtifact (anchor independence)', () => {
  const artifactWire = (artifactId: string, runId: string) => ({
    artifact_id: artifactId,
    content_markdown: 'Inhalt',
    created_at: 1,
    kind: 'memo' as const,
    refs: [],
    refs_count: 0,
    revision: 3,
    revisions: [],
    run_id: runId,
    session_id: 's1',
    status: 'ready' as const,
    title: 'Memo',
    updated_at: 2,
    updated_by: 'agent' as const,
  })
  const run = (runId: string, sessionId: string, artifactIds: string[] = []) => {
    const record = agentRunFromSummary({
      access: { mode: 'owner' },
      run_id: runId,
      status: 'completed',
      queue_position: null,
      question: 'Frage',
      stack: 'default',
      mode: 'agent_kernel',
      kind: 'agent',
      agent_overrides: { autonomy: 'balanced' },
      session_id: sessionId,
      created_at: 1,
      started_at: 1,
      finished_at: 2,
      elapsed_seconds: 1,
      snapshot: { current_node: 'agent_kernel', last_message: '' },
      error: null,
      events_url: `/v1/runs/${runId}/events`,
      result_url: `/v1/runs/${runId}/result`,
    } as ResearchRunSummary)
    const artifacts: typeof record.artifacts = {}
    for (const artifactId of artifactIds) {
      artifacts[artifactId] = agentArtifactFromWire(artifactWire(artifactId, runId))
    }
    return { ...record, artifactOrder: artifactIds, artifacts }
  }
  const index = (
    byId: Record<string, { artifactId: string; runId: string }>,
  ) => ({
    byId: Object.fromEntries(
      Object.entries(byId).map(([key, meta]) => [key, {
        artifactId: meta.artifactId,
        kind: 'memo' as const,
        revision: 3,
        runId: meta.runId,
        status: 'ready' as const,
        title: 'Memo',
        updatedAt: 2,
      }]),
    ),
    order: Object.keys(byId),
    stale: false,
  })

  it('returns the hinted run directly when it still holds the artifact', () => {
    const runs = { r1: run('r1', 's1', ['a1']) }
    const resolved = resolveAgentArtifact(runs, {}, { artifactId: 'a1', runId: 'r1' })
    expect(resolved.runId).toBe('r1')
    expect(resolved.artifact?.artifactId).toBe('a1')
  })

  it('follows a stale hint to the same-session run now holding the artifact', () => {
    // Run r2 updated the memo — the server moved the anchor off r1.
    const runs = { r1: run('r1', 's1'), r2: run('r2', 's1', ['a1']) }
    const resolved = resolveAgentArtifact(runs, {}, { artifactId: 'a1', runId: 'r1' })
    expect(resolved.runId).toBe('r2')
    expect(resolved.artifact?.artifactId).toBe('a1')
  })

  it('never crosses the session fence to a foreign run', () => {
    // Same artifactId on ANOTHER session's run must not resolve.
    const runs = { r1: run('r1', 's1'), rx: run('rx', 's2', ['a1']) }
    const resolved = resolveAgentArtifact(runs, {}, { artifactId: 'a1', runId: 'r1' })
    expect(resolved.runId).toBe('r1')
    expect(resolved.artifact).toBeUndefined()
  })

  it('never serves a cached copy OLDER than the index anchor', () => {
    // Live scenario: run r2 updated the document to revision 3 and the
    // server re-anchored it; run r1 still holds its frozen revision-1
    // copy from its own live events. Turn 1's chip resolves via r1 —
    // the resolver must follow the index to the CURRENT copy on r2.
    const r1 = run('r1', 's1', ['a1'])
    r1.artifacts.a1 = { ...r1.artifacts.a1, revision: 1 }
    const runs = { r1, r2: run('r2', 's1', ['a1']) }
    const sessionArtifacts = { s1: index({ a1: { artifactId: 'a1', runId: 'r2' } }) }
    const resolved = resolveAgentArtifact(runs, sessionArtifacts, {
      artifactId: 'a1',
      runId: 'r1',
    })
    expect(resolved.runId).toBe('r2')
    expect(resolved.artifact?.revision).toBe(3)
  })

  it('falls back to the session index anchor when no loaded run has it', () => {
    const runs = { r1: run('r1', 's1') }
    const sessionArtifacts = { s1: index({ a1: { artifactId: 'a1', runId: 'r9' } }) }
    const resolved = resolveAgentArtifact(runs, sessionArtifacts, {
      artifactId: 'a1',
      runId: 'r1',
    })
    expect(resolved.runId).toBe('r9')
    expect(resolved.artifact).toBeUndefined()
  })

  it('serves the body held on the index row when its anchor run is not loaded', () => {
    // A delegated child is never a desk record: its memo body can only
    // live on the session index row, and the resolver must serve it.
    const runs = { r1: run('r1', 's1') }
    const row = index({ a1: { artifactId: 'a1', runId: 'r9' } })
    const sessionArtifacts = {
      s1: {
        ...row,
        byId: {
          a1: {
            ...row.byId.a1,
            record: agentArtifactFromWire(artifactWire('a1', 'r9')),
          },
        },
      },
    }
    const resolved = resolveAgentArtifact(runs, sessionArtifacts, {
      artifactId: 'a1',
      runId: 'r1',
    })
    expect(resolved.runId).toBe('r9')
    expect(resolved.artifact?.contentMarkdown).toBe('Inhalt')
  })

  it('keeps the hint as the honest last resort', () => {
    const runs = { r1: run('r1', 's1') }
    const resolved = resolveAgentArtifact(runs, { s1: index({}) }, {
      artifactId: 'a1',
      runId: 'r1',
    })
    expect(resolved).toEqual({ artifact: undefined, runId: 'r1' })
  })
})

describe('sessionArtifactMetaFromWire', () => {
  it('carries the CURRENT run anchor and the listing fields', () => {
    expect(sessionArtifactMetaFromWire({
      artifact_id: 'a1',
      created_at: 1,
      kind: 'deliverable',
      refs_count: 2,
      revision: 4,
      run_id: 'r7',
      session_id: 's1',
      status: 'ready',
      title: 'Bericht',
      updated_at: 9,
      updated_by: 'agent',
    })).toEqual({
      artifactId: 'a1',
      kind: 'deliverable',
      revision: 4,
      runId: 'r7',
      status: 'ready',
      title: 'Bericht',
      updatedAt: 9,
    })
  })
})
