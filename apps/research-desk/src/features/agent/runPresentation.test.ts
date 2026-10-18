import { describe, expect, it } from 'vitest'

import type { AgentArtifactRecord, AgentPlanTaskRecord, AgentRunOutcome } from './model'
import type { ResearchRunSnapshot } from '@/features/researchRuns/types'
import { translations } from '@/i18n/translations'
import {
  agentRunCompletionRecap,
  artifactClaimMarks,
  agentTodoReportAge,
  latestChildActivity,
  retainHydratedAgentRunIds,
  runClockParts,
  runSettingsSummary,
  shouldAnimateAgentNarration,
} from './runPresentation'

const task = (taskId: string, status: string): AgentPlanTaskRecord => ({
  budget: {},
  childRunId: null,
  dependsOn: [],
  expectedOutput: '',
  gapIds: [],
  objective: '',
  ordinal: 0,
  params: {},
  queries: ['q'],
  resultSummary: '',
  status,
  taskId,
  title: taskId,
  toolKind: 'web_instant',
})

describe('agent run completion recap', () => {
  const outcome = (review: string, summary: string): AgentRunOutcome => ({
    outcomeId: 'run_recap:outcome', final: 'completed', review, summary, applied: 0, lines: [], marks: [],
  })
  const completed = {
    elapsedSeconds: 38, kind: 'agent' as const, lastSequence: 12,
    status: 'completed' as const, stepLog: [], taskStates: {},
  }

  it('the recap takes its words from the record', () => {
    // A phase or an activity says only that a review step ran; how it
    // ended is the record's own sentence, word for word.
    const recap = agentRunCompletionRecap({
      ...completed,
      outcome: outcome('completed', 'Geprüft · 2 Punkte nicht geprüft'),
      stepLog: [
        { kind: 'phase', phase: 'critic', seq: 1, at: 1 },
        { kind: 'activity', activityKind: 'critic_revise', seq: 2, at: 2 },
      ],
    }, [], undefined)
    expect(recap.outcome).toEqual({ summary: 'Geprüft · 2 Punkte nicht geprüft' })
  })

  it.each([
    ['completed', { summary: 'Geprüft · ohne Befund' }],
    ['unavailable', { summary: 'Geprüft · ohne Befund' }],
    ['waived', 'none'],
    ['not_run', 'none'],
  ] as const)('shows a %s review only when one ran or was owed', (review, expected) => {
    // A Stufe that waives the review and a run that ended without one
    // owe the reader nothing; their records stay quiet.
    const recap = agentRunCompletionRecap({
      ...completed, outcome: outcome(review, 'Geprüft · ohne Befund'),
    }, [], undefined)
    expect(recap.outcome).toEqual(expected)
  })

  it('a completed agent run without a record reads ohne Ausgang', () => {
    expect(agentRunCompletionRecap(completed, [], undefined).outcome).toBe('missing')
  })

  it.each([
    ['a run whose stream was not read yet', { lastSequence: 0 }],
    ['a delegated child', { kind: 'agent_child' as const }],
    ['a run that did not complete', { status: 'failed' as const }],
  ])('says nothing about %s without a record', (_case, overrides) => {
    // A run hydrated from its summary alone has read none of its events:
    // its record may still be on the way, so it claims no absence.
    expect(agentRunCompletionRecap({ ...completed, ...overrides }, [], undefined).outcome)
      .toBe('none')
  })

  it('uses durable task rows and artifact metadata for a substantive return state', () => {
    const memo = { refsCount: 38 } as AgentArtifactRecord
    expect(agentRunCompletionRecap({
      ...completed,
      elapsedSeconds: 863,
      outcome: outcome('completed', 'Geprüft · ohne Befund'),
    }, [task('t1', 'completed'), task('t2', 'completed')], memo)).toEqual({
      elapsedSeconds: 863,
      referenceCount: 38,
      outcome: { summary: 'Geprüft · ohne Befund' },
      synthesized: true,
      taskCount: 2,
      tasksCompleted: 2,
    })
  })
})

describe('run settings line', () => {
  const t = translations.de
  const execution = (overrides: Record<string, unknown> = {}): ResearchRunSnapshot => ({
    execution: {
      effective_mode: 'agent_kernel',
      response_form: 'chat',
      depth: 'normal',
      model: 'gpt-5.4',
      reasoning_effort: 'medium',
      source_policy: { web: 'disabled', knowledge: 'available' },
      knowledge_scope: 'all',
      knowledge_collection_count: 3,
      consent_reason: 'permission_policy',
      tool_use_counts: { web: 0, knowledge: 7 },
      ...overrides,
    } as ResearchRunSnapshot['execution'],
  })

  it('names every recorded condition of a kernel turn, in reading order', () => {
    expect(runSettingsSummary({
      agentTier: 'gruendlich', autonomy: 'balanced', snapshot: execution(),
    }, t)).toEqual([
      'Automatisch', 'Standard', 'Gründlich', 'gpt-5.4', 'Aufwand Med',
      'Web aus', 'Wissen 7 Suchen (alle Sammlungen)',
    ])
  })

  it('counts the web searches its subtasks ran in a snapshot from before the search count', () => {
    // The regression: "Web 1 Suche" while the research subtask ran
    // six searches — the line counted the kernel's own web calls only. A
    // snapshot without `web_searches` keeps this reading: its budget
    // counter and the child's planned queries are all it recorded.
    const parts = runSettingsSummary({
      agentTier: 'gruendlich',
      autonomy: 'balanced',
      children: {
        child_a: { childRunId: 'child_a', taskId: 'call_a', snapshot: { total_queries: 6 } },
        child_b: { childRunId: 'child_b', taskId: 'call_b' },
      },
      snapshot: execution({
        source_policy: { web: 'available', knowledge: 'disabled' },
        tool_use_counts: { web: 1, knowledge: 0 },
      }),
    }, t)
    expect(parts).toContain('Web 7 Suchen')
    expect(parts).not.toContain('Web 1 Suche')
  })

  it('counts the searches the provider ran and names the research it delegated', () => {
    // The regression: "Web 7 Suchen" for one delegated research whose provider ran
    // six searches. The budget counter takes the delegation as a web use,
    // and the child's planned queries are no searches.
    const parts = runSettingsSummary({
      agentTier: 'gruendlich',
      autonomy: 'balanced',
      children: {
        child_a: {
          childRunId: 'child_a', taskId: 'call_a',
          snapshot: { total_queries: 8, executed_queries: 6 },
        },
        role: { childRunId: 'role', taskId: 'role-review', snapshot: { current_node: 'agent_kernel' } },
      },
      snapshot: execution({
        source_policy: { web: 'available', knowledge: 'disabled' },
        tool_use_counts: { web: 1, knowledge: 0 },
        web_searches: 0,
      }),
    }, t)
    expect(parts).toContain('Web 1 Recherche · 6 Suchen')
  })

  it("adds the run's own instant searches to those of its research", () => {
    const parts = runSettingsSummary({
      autonomy: 'balanced',
      children: {
        child_a: { childRunId: 'child_a', taskId: 'call_a', snapshot: { total_queries: 4, executed_queries: 4 } },
        child_b: { childRunId: 'child_b', taskId: 'call_b', snapshot: { total_queries: 3, executed_queries: 1 } },
      },
      snapshot: execution({
        source_policy: { web: 'available', knowledge: 'disabled' },
        tool_use_counts: { web: 4, knowledge: 0 },
        web_searches: 2,
      }),
    }, t)
    expect(parts).toContain('Web 2 Recherchen · 7 Suchen')
  })

  it('names only the searches of a run that delegated no research', () => {
    const parts = runSettingsSummary({
      autonomy: 'balanced',
      snapshot: execution({
        source_policy: { web: 'available', knowledge: 'disabled' },
        tool_use_counts: { web: 1, knowledge: 0 },
        web_searches: 1,
      }),
    }, t)
    expect(parts).toContain('Web 1 Suche')
  })

  it('names the mission machine and the pinned chips with their count', () => {
    const parts = runSettingsSummary({
      agentTier: 'tief', autonomy: 'autonomous',
      snapshot: execution({
        effective_mode: 'workspace_agent',
        knowledge_scope: 'pinned',
        knowledge_collection_count: 2,
        tool_use_counts: { web: 3, knowledge: 1 },
        source_policy: { web: 'available', knowledge: 'available' },
      }),
    }, t)
    expect(parts[0]).toBe(t.agent.overview.brainMission)
    expect(parts).toContain('Web 3 Suchen')
    expect(parts).toContain('Wissen 1 Suche (2 Sammlungen)')
  })

  it('never guesses: no snapshot leaves permission and Stufe, an inherited scope adds nothing', () => {
    expect(runSettingsSummary({ agentTier: 'schnell', autonomy: 'strict' }, t))
      .toEqual(['Strikt', 'Schnell'])
    // Available but unused reads as zero, not as absent.
    expect(runSettingsSummary({
      autonomy: 'balanced',
      snapshot: execution({
        model: '', reasoning_effort: '',
        source_policy: { web: 'available', knowledge: 'available' },
        knowledge_scope: 'inherited', knowledge_collection_count: 0,
        tool_use_counts: { web: 0, knowledge: 0 },
      }),
    }, t)).toEqual(['Automatisch', 'Standard', 'Web 0 Suchen', 'Wissen 0 Suchen'])
    // A knowledge-off run says so instead of dropping the source.
    expect(runSettingsSummary({
      autonomy: 'balanced',
      snapshot: execution({ source_policy: { web: 'available', knowledge: 'disabled' } }),
    }, t)).toContain('Wissen aus')
  })

  it('falls back to the live model resolution when the snapshot carries no model', () => {
    const parts = runSettingsSummary({
      autonomy: 'balanced',
      modelResolution: { model: 'claude-opus-4-8', effort: 'high', tier: 'high', modelSource: 'tier:high' },
      snapshot: execution({ model: '', reasoning_effort: '' }),
    }, t)
    expect(parts).toContain('claude-opus-4-8')
    expect(parts).toContain('Aufwand High')
  })
})

describe('agent transcript presentation provenance', () => {
  it('adds runs observed during hydration but not later live turns', () => {
    const initial = new Set(['cached'])
    const hydrating = retainHydratedAgentRunIds(
      initial,
      ['cached', 'persisted-page'],
      false,
    )
    expect([...hydrating]).toEqual(['cached', 'persisted-page'])

    const settled = retainHydratedAgentRunIds(
      hydrating,
      ['cached', 'persisted-page', 'new-live-turn'],
      true,
    )
    expect(settled).toBe(hydrating)
    expect(settled.has('new-live-turn')).toBe(false)
  })

  it('animates only the newest genuinely live narration', () => {
    expect(shouldAnimateAgentNarration({
      entry: { arrivedLive: false },
      historicalRun: false,
      isLatest: true,
    })).toBe(false)
    expect(shouldAnimateAgentNarration({
      entry: { arrivedLive: undefined },
      historicalRun: true,
      isLatest: true,
    })).toBe(false)
    expect(shouldAnimateAgentNarration({
      entry: { arrivedLive: true },
      historicalRun: true,
      isLatest: true,
    })).toBe(true)
    expect(shouldAnimateAgentNarration({
      entry: { arrivedLive: true },
      historicalRun: false,
      isLatest: false,
    })).toBe(false)
  })
})

describe('agentTodoReportAge', () => {
  it('says how old the list is once the run worked past it', () => {
    // The regression: a todo written before a delegation kept naming a
    // finished step as current for fifty minutes.
    expect(
      agentTodoReportAge(1000, [{ at: 1200 }, { at: 4020 }], 4000),
    ).toBe(3000)
  })

  it('adds nothing while the list is the newest thing that happened', () => {
    expect(agentTodoReportAge(1000, [{ at: 900 }], 4000)).toBeNull()
  })

  it('counts a delegated child\'s progress as work past the list', () => {
    // A list written in the same second as the delegation would show no
    // age while the child works — no step row follows, but the child
    // projects progress all along.
    expect(agentTodoReportAge(1000, [{ at: 1000 }], 4000, 3500)).toBe(3000)
    expect(agentTodoReportAge(1000, [{ at: 1000 }], 4000, 1000)).toBeNull()
    expect(latestChildActivity({ a: { updatedAt: 3500 }, b: {}, c: { updatedAt: 2000 } })).toBe(3500)
  })

  it('adds nothing when no list was ever reported', () => {
    expect(agentTodoReportAge(undefined, [{ at: 900 }], 4000)).toBeNull()
  })

  it('never reports a negative age', () => {
    expect(agentTodoReportAge(5000, [{ at: 6000 }], 4000)).toBe(0)
  })
})

describe('run clock', () => {
  const t = translations.de
  const startedAt = new Date(1_700_000_000 * 1000).toISOString()
  const at = (seconds: number) => 1_700_000_000 + seconds

  it('ticks from the server start and stays quiet while updates arrive', () => {
    expect(runClockParts({ lastProgressAt: at(70), now: at(72), startedAt, waiting: false }, t))
      .toEqual({ elapsed: '00:01:12', note: '' })
  })

  it('names the age of the newest update after a quiet stretch', () => {
    // The regression: "zuletzt gemeldet vor 00:00:06" stood still
    // for thirty seconds — computed once per render, never ticking.
    expect(runClockParts({ lastProgressAt: at(40), now: at(65), startedAt, waiting: false }, t)?.note)
      .toBe('letzte Zwischenmeldung vor 00:00:25')
    expect(runClockParts({ lastProgressAt: at(40), now: at(66), startedAt, waiting: false }, t)?.note)
      .toBe('letzte Zwischenmeldung vor 00:00:26')
  })

  it('says plainly when nothing new arrived for a minute', () => {
    expect(runClockParts({ lastProgressAt: at(10), now: at(75), startedAt, waiting: false }, t)?.note)
      .toBe('keine neuen Zwischenstände seit 00:01:05')
  })

  it('does not count a wait for the user against the run', () => {
    expect(runClockParts({ lastProgressAt: at(10), now: at(300), startedAt, waiting: true }, t)?.note)
      .toBe('')
  })

  it('shows nothing before the run has a start', () => {
    expect(runClockParts({ lastProgressAt: undefined, now: at(5), startedAt: undefined, waiting: false }, t))
      .toBeNull()
  })
})

describe('the claim marks of a delivered text', () => {
  const mark = (outputId: string, quote: string) => ({
    outputId, quote, state: 'insufficient', note: 'Nicht ausreichend belegt.',
  })
  const outcome = { marks: [mark('chat', 'Antwortsatz.'), mark('art_doc', 'Dokumentsatz.')] }

  it('gives the answer the marks of the chat output and a canvas its own', () => {
    expect([
      artifactClaimMarks(outcome, { artifactId: 'art_run_answer', kind: 'answer' }).map((item) => item.quote),
      artifactClaimMarks(outcome, { artifactId: 'art_doc', kind: 'deliverable' }).map((item) => item.quote),
      artifactClaimMarks(outcome, { artifactId: 'art_other', kind: 'deliverable' }),
    ]).toEqual([['Antwortsatz.'], ['Dokumentsatz.'], []])
  })

  it('gives no marks without a shown outcome', () => {
    expect(artifactClaimMarks(undefined, { artifactId: 'art_doc', kind: 'deliverable' })).toEqual([])
  })
})
