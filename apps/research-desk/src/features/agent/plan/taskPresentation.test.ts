import { describe, expect, it } from 'vitest'

import type { AgentPlanTaskRecord } from '../model'
import { translations } from '@/i18n/translations'
import {
  agentTaskExecutionLabel,
  agentTaskElapsedSeconds,
  agentTaskExecutionSemantics,
  agentTaskGroup,
  agentTaskMetrics,
  agentTaskResultPreview,
  agentTaskResultContent,
  agentPlanExecutionWaves,
  effectiveAgentTaskStatus,
} from './taskPresentation'

function task(
  patch: Partial<AgentPlanTaskRecord>,
): AgentPlanTaskRecord {
  return {
    budget: {},
    childRunId: null,
    dependsOn: [],
    expectedOutput: '',
    gapIds: [],
    objective: '',
    ordinal: 0,
    params: {},
    queries: [],
    resultSummary: '',
    status: 'pending',
    taskId: 't1',
    title: 'Task',
    toolKind: 'web_instant',
    ...patch,
  }
}

describe('agent task presentation', () => {
  it('distinguishes literal instant requests from one research child', () => {
    expect(agentTaskExecutionSemantics(task({
      queries: ['q1', 'q2'],
      toolKind: 'web_instant',
    }))).toMatchObject({ kind: 'instant', queryCount: 2, requestCount: 2 })
    expect(agentTaskExecutionSemantics(task({
      params: { profile: 'deep' },
      queries: ['guide 1', 'guide 2', 'guide 3'],
      toolKind: 'web_research',
    }))).toEqual({
      kind: 'research',
      profile: 'deep',
      queryCount: 3,
      requestCount: 1,
    })
  })

  it('labels instant work and real research children truthfully', () => {
    expect(agentTaskExecutionLabel(task({ queries: ['one question'] }), translations.de))
      .toBe('Instant-Suche · 1 Anfrage')
    expect(agentTaskExecutionLabel(task({
      params: { profile: 'deep' },
      queries: ['guide 1', 'guide 2'],
      toolKind: 'web_research',
    }), translations.de)).toBe('Recherche-Agent · Tief · 2 Leitfragen')
  })

  it('labels project-knowledge request counts without raw tool names', () => {
    expect(agentTaskExecutionLabel(task({
      queries: ['one question'],
      toolKind: 'rag_query',
    }), translations.de)).toBe('Projektwissen · 1 Abfrage')
    expect(agentTaskExecutionLabel(task({
      queries: ['q1', 'q2'],
      toolKind: 'rag_query',
    }), translations.de)).toBe('Projektwissen · 2 Abfragen')
  })

  it('uses durable task status when no live event state exists', () => {
    expect(effectiveAgentTaskStatus(task({ status: 'completed' }), undefined)).toBe(
      'completed',
    )
    expect(effectiveAgentTaskStatus(
      task({ status: 'completed' }),
      { attempt: 1, status: 'failed' },
    )).toBe('failed')
    expect(effectiveAgentTaskStatus(
      task({ status: 'insufficient_evidence' }),
      undefined,
    )).toBe('insufficient_evidence')
  })

  it('groups evidence warnings as attention rather than failure or completion', () => {
    expect(agentTaskGroup('running')).toBe('active')
    expect(agentTaskGroup('pending')).toBe('active')
    expect(agentTaskGroup('insufficient_evidence')).toBe('attention')
    expect(agentTaskGroup('failed')).toBe('attention')
    expect(agentTaskGroup('completed')).toBe('completed')
    expect(agentTaskGroup('cancel_requested')).toBe('active')
    expect(agentTaskGroup('cancelled')).toBe('attention')
    expect(agentTaskGroup('completed', true)).toBe('attention')
  })

  it('uses task operation boundaries for elapsed time', () => {
    expect(agentTaskElapsedSeconds(
      { finishedAt: 119, startedAt: 101 },
      147,
    )).toBe(18)
    expect(agentTaskElapsedSeconds({ startedAt: 101 }, 147)).toBe(46)
  })

  it('omits unavailable zero claims for local work but retains real counts', () => {
    const snapshot = {
      consolidated_claim_count: 0,
      total_queries: 1,
      total_sources: 8,
    }
    expect(agentTaskMetrics(snapshot, false)).toEqual([
      { kind: 'sources', value: 8 },
      { kind: 'queries', value: 1 },
    ])
    expect(agentTaskMetrics(snapshot, true)).toContainEqual({
      kind: 'claims',
      value: 0,
    })
    expect(agentTaskMetrics({ consolidated_claim_count: 3 }, false)).toEqual([
      { kind: 'claims', value: 3 },
    ])
  })

  it('builds a quiet plain preview without repeating the task title', () => {
    expect(agentTaskResultPreview(
      'Marktgegenstand eingrenzen',
      'Marktgegenstand eingrenzen: Für ein **belastbares Memo** [gilt](https://example.com) …',
    )).toBe('Für ein belastbares Memo gilt …')
  })

  it('never presents a fallback preview as a complete lazy result', () => {
    expect(agentTaskResultContent(null, 'Short preview', 'request failed')).toEqual({
      markdown: 'Short preview',
      previewOnly: true,
    })
    expect(agentTaskResultContent({
      answer_markdown: '',
      child_run_id: null,
      claims: [],
      error: null,
      legacy_summary_only: true,
      metrics: {
        claim_count: 0,
        completion_tokens: 0,
        prompt_tokens: 0,
        reference_count: 0,
      },
      references: [],
      result_summary: 'Legacy preview',
      status: 'completed',
      task_id: 't1',
    }, '', null)).toMatchObject({ previewOnly: true })
    expect(agentTaskResultContent({
      answer_markdown: 'Complete result',
      child_run_id: null,
      claims: [],
      error: null,
      legacy_summary_only: false,
      metrics: {
        claim_count: 1,
        completion_tokens: 2,
        prompt_tokens: 3,
        reference_count: 1,
      },
      references: [],
      result_summary: 'Preview',
      status: 'completed',
      task_id: 't1',
    }, 'Preview', null)).toEqual({
      markdown: 'Complete result',
      previewOnly: false,
    })
  })

  it('summarizes only dependency-backed parallel execution waves', () => {
    expect(agentPlanExecutionWaves([
      { taskId: 'w1', ordinal: 0, toolKind: 'web_instant', dependsOn: [] },
      { taskId: 'w2', ordinal: 1, toolKind: 'web_instant', dependsOn: [] },
      { taskId: 'k1', ordinal: 2, toolKind: 'rag_query', dependsOn: [] },
      {
        taskId: 's1',
        ordinal: 3,
        toolKind: 'synthesis',
        dependsOn: ['w1', 'w2', 'k1'],
      },
    ])).toEqual([
      {
        taskCount: 3,
        toolCounts: { rag_query: 1, web_instant: 2 },
      },
      { taskCount: 1, toolCounts: { synthesis: 1 } },
    ])
  })
})
