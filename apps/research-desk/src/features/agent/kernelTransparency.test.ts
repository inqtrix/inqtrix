import { describe, expect, it } from 'vitest'

import { activityDisplayText, kernelToolLabel } from './activityPresentation'
import { applyAgentRunEvent } from './events'
import { agentRunFromSummary } from './model'
import { noticeDisplayText, noticeTone } from './timeline/AgentTimeline'
import { translations } from '@/i18n/translations'
import type {
  ResearchRunEvent,
  ResearchRunSummary,
} from '@/features/researchRuns/types'

function summary(): ResearchRunSummary {
  return {
    access: { mode: 'owner' },
    run_id: 'run_k1',
    status: 'running',
    queue_position: null,
    question: 'Recherchiere bitte.',
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
    events_url: '/v1/runs/run_k1/events',
    result_url: '/v1/runs/run_k1/result',
  }
}

function event(
  sequence: number,
  type: string,
  data: Record<string, unknown> = {},
): ResearchRunEvent {
  return {
    type,
    run_id: 'run_k1',
    sequence,
    created_at: 1_700_000_100 + sequence,
    data,
  }
}

describe('kernel transparency events', () => {
  it('replaces the task list wholesale from todo.updated', () => {
    const first = applyAgentRunEvent(
      agentRunFromSummary(summary()),
      event(1, 'inqtrix.agent.todo.updated', {
        todos: [
          { content: 'Quellen sammeln', status: 'in_progress' },
          { content: 'Antwort schreiben', status: 'pending' },
          { content: '', status: 'pending' },
        ],
      }),
    )
    expect(first.todos).toEqual([
      { content: 'Quellen sammeln', status: 'in_progress' },
      { content: 'Antwort schreiben', status: 'pending' },
    ])
    const second = applyAgentRunEvent(
      first,
      event(2, 'inqtrix.agent.todo.updated', {
        todos: [{ content: 'Quellen sammeln', status: 'completed' }],
      }),
    )
    expect(second.todos).toEqual([
      { content: 'Quellen sammeln', status: 'completed' },
    ])
    // The checklist is the surface — no transcript row is appended.
    expect(second.stepLog).toHaveLength(0)
  })

  it('turns the silent tool-limit stop into a visible notice', () => {
    const next = applyAgentRunEvent(
      agentRunFromSummary(summary()),
      event(1, 'inqtrix.agent.tool_limit.reached', {
        attempted: 31,
        limit: 30,
        batch_size: 2,
      }),
    )
    const notice = next.stepLog.find((entry) => entry.kind === 'notice')
    expect(notice).toMatchObject({
      current: 31,
      noticeCode: 'tool_limit',
      total: 30,
    })
    expect(noticeDisplayText(notice!, translations.de)).toBe(
      'Werkzeuglimit erreicht (31/30)',
    )
  })

  it('reports only the NEGATIVE sufficiency verdict', () => {
    const covered = applyAgentRunEvent(
      agentRunFromSummary(summary()),
      event(1, 'inqtrix.agent.sufficiency.judged', {
        coverage: 'covered',
        nudge: true,
        tool_uses: 3,
      }),
    )
    expect(covered.stepLog).toHaveLength(0)
    const gap = applyAgentRunEvent(
      agentRunFromSummary(summary()),
      event(1, 'inqtrix.agent.sufficiency.judged', {
        coverage: 'partial',
        missing: ["The document's end was not fully read [20000, 109873)"],
        nudge: true,
        open_continuations: 2,
        tool_uses: 3,
      }),
    )
    const notice = gap.stepLog.find((entry) => entry.kind === 'notice')
    // The judge's gaps steer the model's next searches: its words, not a
    // reader's. The row carries the server's fact only.
    expect(notice).toMatchObject({ current: 2, noticeCode: 'sufficiency_gap' })
    expect(notice?.detail).toBeUndefined()
  })

  it('folds every negative sufficiency verdict of a run into one row', () => {
    let run = applyAgentRunEvent(
      agentRunFromSummary(summary()),
      event(1, 'inqtrix.agent.sufficiency.judged', {
        coverage: 'partial', missing: ['A'], nudge: true, open_continuations: 2, tool_uses: 3,
      }),
    )
    // A server from before the count sends none: nothing is open then.
    run = applyAgentRunEvent(
      run,
      event(2, 'inqtrix.agent.sufficiency.judged', {
        coverage: 'partial', missing: ['B'], nudge: true, tool_uses: 5,
      }),
    )
    const notices = run.stepLog.filter((entry) => entry.kind === 'notice')
    expect(notices).toHaveLength(1)
    expect(notices[0]).toMatchObject({ current: 0, noticeCode: 'sufficiency_gap' })
  })

  it('says calmly that the evidence is not yet sufficient, never the judge\'s words', () => {
    expect(noticeDisplayText({ current: 2, detail: 'unread [20000, 109873)', noticeCode: 'sufficiency_gap' }, translations.de))
      .toBe('Beleglage reicht noch nicht aus · 2 Quellen noch nicht zu Ende gelesen')
    expect(noticeDisplayText({ current: 0, noticeCode: 'sufficiency_gap' }, translations.de))
      .toBe('Beleglage reicht noch nicht aus')
    expect(noticeDisplayText({ current: 1, noticeCode: 'sufficiency_gap' }, translations.en))
      .toBe('Evidence not yet sufficient · 1 source not read to the end')
    // A status, not an alarm; the citation notices keep their warning.
    expect(noticeTone({ noticeCode: 'sufficiency_gap' })).toBe('calm')
    expect(noticeTone({ noticeCode: 'citation_validation' })).toBe('warning')
  })

  it('marks degraded citation validation with its labels', () => {
    const next = applyAgentRunEvent(
      agentRunFromSummary(summary()),
      event(1, 'inqtrix.agent.citation.validation', {
        resolution: 'marked_unsubstantiated',
        status: 'degraded',
        unknown_labels: ['W7', 'K3'],
      }),
    )
    const notice = next.stepLog.find((entry) => entry.kind === 'notice')
    expect(notice).toMatchObject({
      detail: 'W7, K3',
      noticeCode: 'citation_validation',
      status: 'degraded',
    })
    expect(noticeDisplayText(notice!, translations.de)).toBe(
      'Nicht belegbare Zitate markiert: W7, K3',
    )
  })

  it('names an unreadable citation bracket among the marked citations', () => {
    const next = applyAgentRunEvent(
      agentRunFromSummary(summary()),
      event(1, 'inqtrix.agent.citation.validation', {
        malformed_brackets: ['[K3–K5]'],
        resolution: 'marked_unsubstantiated',
        status: 'degraded',
        unknown_labels: [],
      }),
    )
    const notice = next.stepLog.find((entry) => entry.kind === 'notice')
    expect(noticeDisplayText(notice!, translations.de)).toBe(
      'Nicht belegbare Zitate markiert: [K3–K5]',
    )
  })

  it('settles a decided-elsewhere approval row without the refetch hop', () => {
    const withPending = {
      ...agentRunFromSummary(summary()),
      approvals: [{
        approvalId: 'apr_x',
        kind: 'tool' as const,
        status: 'pending' as const,
        subjectType: 'tool',
        subjectId: 't1',
        payload: {},
        decision: '',
        note: '',
        createdAt: 1_700_000_050,
        decidedAt: null,
      }],
    }
    const next = applyAgentRunEvent(
      withPending,
      event(1, 'inqtrix.agent.approval.decided', {
        approval_id: 'apr_x',
        status: 'approved',
      }),
    )
    expect(next.approvals[0].status).toBe('approved')
    expect(next.approvalsStale).toBe(true)
    // An unexpected status value must not corrupt the row.
    const odd = applyAgentRunEvent(
      withPending,
      event(1, 'inqtrix.agent.approval.decided', {
        approval_id: 'apr_x',
        status: 'weird',
      }),
    )
    expect(odd.approvals[0].status).toBe('pending')
  })

  it('seeds no plan fetch for the plan-less kernel engine', () => {
    expect(agentRunFromSummary(summary()).planStale).toBe(false)
    expect(
      agentRunFromSummary({ ...summary(), mode: 'workspace_agent' }).planStale,
    ).toBe(true)
  })

  it('labels kernel wire tool ids and keeps unknown ids verbatim', () => {
    expect(kernelToolLabel('web_instant', translations.de)).toBe(
      'Instant-Websuche',
    )
    expect(kernelToolLabel('delegate_batch', translations.de)).toBe(
      'Delegation mehrerer Unteraufträge',
    )
    expect(kernelToolLabel('some_future_tool', translations.de)).toBe(
      'some_future_tool',
    )
    // The transcript's tool rows go through the same map: a delegation
    // row must not read `run_web_research` while its gate card says
    // "Web-Recherche (Unterauftrag)".
    expect(activityDisplayText({
      detail: '{"question": "EU-Batterieverordnung"}',
      kind: 'searching',
      label: 'run_web_research',
    }, translations.de).startsWith('Web-Recherche (Unterauftrag)')).toBe(true)
  })

  it('remembers what a gate asked, exactly once per subject', () => {
    const requested = applyAgentRunEvent(
      agentRunFromSummary(summary()),
      event(1, 'inqtrix.agent.approval.requested', {
        approval_id: 'apr_1',
        kind: 'tool',
      }),
    )
    const markers = requested.stepLog.filter(
      (entry) => entry.kind === 'gate_requested',
    )
    expect(markers).toMatchObject([{ approvalId: 'apr_1', detail: 'tool' }])
    // Requests can re-emit across resume segments — the marker must not.
    const reEmitted = applyAgentRunEvent(
      requested,
      event(2, 'inqtrix.agent.approval.requested', {
        approval_id: 'apr_1',
        kind: 'tool',
      }),
    )
    expect(
      reEmitted.stepLog.filter((entry) => entry.kind === 'gate_requested'),
    ).toHaveLength(1)
    const clarified = applyAgentRunEvent(
      reEmitted,
      event(3, 'inqtrix.agent.clarification.requested', {
        clarification_id: 'clr_1',
      }),
    )
    expect(
      clarified.stepLog.filter((entry) => entry.kind === 'gate_requested'),
    ).toHaveLength(2)
  })
})
