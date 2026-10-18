import { describe, expect, it, vi } from 'vitest'

import {
  acknowledgeAgentTaskCancellation,
  applyAgentRunEvent,
  nextFailedChildTasks,
  nextOpenChildTasks,
} from './events'
import { agentRunFromSummary, type AgentPlanTaskRecord } from './model'
import { agentTaskGroups } from './plan/taskPresentation'
import type { ResearchRunEvent, ResearchRunSummary } from '@/features/researchRuns/types'

function summary(overrides: Partial<ResearchRunSummary> = {}): ResearchRunSummary {
  return {
    access: { mode: 'owner' },
    run_id: 'run_agent_1',
    status: 'running',
    queue_position: null,
    question: 'Erstelle eine Marktanalyse.',
    stack: 'default',
    mode: 'workspace_agent',
    kind: 'agent',
    agent_overrides: { autonomy: 'balanced' },
    created_at: 1_700_000_000,
    started_at: 1_700_000_001,
    finished_at: null,
    elapsed_seconds: null,
    snapshot: { current_node: 'agent', last_message: '' },
    error: null,
    events_url: '/v1/runs/run_agent_1/events',
    result_url: '/v1/runs/run_agent_1/result',
    ...overrides,
  }
}

function event(
  sequence: number,
  type: string,
  data: Record<string, unknown> = {},
): ResearchRunEvent {
  return { type, run_id: 'run_agent_1', sequence, created_at: 1_700_000_100 + sequence, data }
}

describe('applyAgentRunEvent', () => {
  it('retains the same typed model failure through SSE and stored run loading', () => {
    const error = { type: 'model_tool_arguments', message: 'Invalid provider arguments.' }
    const streamed = applyAgentRunEvent(
      agentRunFromSummary(summary({ mode: 'agent_kernel' })),
      event(1, 'inqtrix.run.failed', { error }),
    )
    const stored = agentRunFromSummary(summary({ mode: 'agent_kernel', status: 'failed', error }))
    expect(streamed.errorCode).toBe('model_tool_arguments')
    expect(stored.errorCode).toBe('model_tool_arguments')
    expect(streamed.error).toBe('Invalid provider arguments.')
    expect(stored.error).toBe('Invalid provider arguments.')
  })

  it('applies an HTTP task-cancel acknowledgement without waiting for SSE', () => {
    const record = {
      ...agentRunFromSummary(summary()),
      taskStates: {
        t1: { attempt: 1, status: 'running' as const },
      },
    }

    const acknowledged = acknowledgeAgentTaskCancellation(
      record,
      't1',
      'cancel_requested',
      null,
    )

    expect(acknowledged.taskStates.t1?.status).toBe('cancel_requested')
    expect(acknowledged.planStale).toBe(true)

    const completed = {
      ...record,
      taskStates: {
        t1: { attempt: 1, status: 'completed' as const },
      },
    }
    expect(acknowledgeAgentTaskCancellation(
      completed,
      't1',
      'cancel_requested',
      null,
    ).taskStates.t1?.status).toBe('completed')
    expect(applyAgentRunEvent(
      completed,
      event(1, 'inqtrix.agent.task.cancel_requested', {
        status: 'cancel_requested',
        task_id: 't1',
      }),
    ).taskStates.t1?.status).toBe('completed')
  })

  it('keeps an HTTP cancel acknowledgement monotone across delayed events', () => {
    const running = {
      ...agentRunFromSummary(summary()),
      taskStates: {
        t1: { attempt: 1, status: 'running' as const },
      },
    }
    let record = acknowledgeAgentTaskCancellation(
      running,
      't1',
      'cancel_requested',
      null,
    )

    record = applyAgentRunEvent(
      record,
      event(1, 'inqtrix.agent.task.started', {
        attempt: 1,
        task_id: 't1',
      }),
    )
    expect(record.taskStates.t1?.status).toBe('cancel_requested')

    record = applyAgentRunEvent(
      record,
      event(2, 'inqtrix.agent.child.progress', {
        child_run_id: 'run_child_1',
        run_status: 'running',
        task_id: 't1',
      }),
    )
    expect(record.taskStates.t1?.status).toBe('cancel_requested')

    record = applyAgentRunEvent(
      record,
      event(3, 'inqtrix.agent.child.progress', {
        child_run_id: 'run_child_1',
        run_status: 'completed',
        task_id: 't1',
      }),
    )
    expect(record.taskStates.t1?.status).toBe('cancel_requested')

    record = applyAgentRunEvent(
      record,
      event(4, 'inqtrix.agent.task.finished', {
        status: 'cancelled',
        task_id: 't1',
      }),
    )
    expect(record.taskStates.t1?.status).toBe('cancelled')
  })

  it('advances phase and station, never backwards on gate phases', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.phase.changed', { phase: 'discovery' }))
    expect(record.station).toBe('discovery')
    // clarification is a gate, not a station: track keeps discovery
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.phase.changed', { phase: 'clarification' }))
    expect(record.phase).toBe('clarification')
    expect(record.station).toBe('discovery')
    record = applyAgentRunEvent(record, event(3, 'inqtrix.agent.phase.changed', { phase: 'execution' }))
    expect(record.station).toBe('execution')
  })

  it('ignores replayed frames (sequence gate)', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(5, 'inqtrix.agent.phase.changed', { phase: 'planning' }))
    const replay = applyAgentRunEvent(record, event(5, 'inqtrix.agent.phase.changed', { phase: 'intake' }))
    expect(replay).toBe(record)
    expect(replay.station).toBe('planning')
  })

  it('flips stale flags on control-row signals instead of copying payloads', () => {
    let record = agentRunFromSummary(summary())
    record = { ...record, planStale: false, approvalsStale: false, clarificationsStale: false }
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.plan.proposed', { plan_id: 'p1', version: 1 }))
    expect(record.planStale).toBe(true)
    expect(record.plan).toBeUndefined()
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.approval.requested', { approval_id: 'a1', kind: 'plan' }))
    expect(record.approvalsStale).toBe(true)
    record = applyAgentRunEvent(record, event(3, 'inqtrix.agent.clarification.requested', { clarification_id: 'c1' }))
    expect(record.clarificationsStale).toBe(true)
  })

  it('records auto-approved replan versions for the timeline note', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.plan.revised', { version: 2, auto_approved: true }))
    expect(record.lastAutoApprovedVersion).toBe(2)
    expect(record.planStale).toBe(true)
  })

  it('marks the plan stale when a loaded plan approval is decided', () => {
    let record = agentRunFromSummary(summary())
    record = {
      ...record,
      planStale: false,
      approvals: [{
        approvalId: 'apr-plan',
        kind: 'plan',
        status: 'pending',
        subjectType: 'plan',
        subjectId: 'p1',
        payload: {},
        decision: '',
        note: '',
        createdAt: 1,
        decidedAt: null,
      }],
    }
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.approval.decided', {
      approval_id: 'apr-plan',
      status: 'approved',
    }))
    expect(record.planStale).toBe(true)
    expect(record.approvalsStale).toBe(true)
  })

  it('tracks task lifecycle with child attribution and new map references', () => {
    let record = agentRunFromSummary(summary())
    const before = record.taskStates
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.task.started', { task_id: 't1', ordinal: 0, tool_kind: 'web_research', attempt: 1 }))
    expect(record.taskStates.t1.status).toBe('running')
    expect(record.taskStates).not.toBe(before)
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.child.progress', {
      task_id: 't1',
      child_run_id: 'run_child_1',
      snapshot: { current_node: 'search' },
    }))
    expect(record.children.run_child_1.snapshot?.current_node).toBe('search')
    expect(record.taskStates.t1.childRunId).toBe('run_child_1')
    record = applyAgentRunEvent(record, event(3, 'inqtrix.agent.task.finished', {
      task_id: 't1',
      status: 'completed',
      child_run_id: 'run_child_1',
    }))
    expect(record.taskStates.t1.status).toBe('completed')
    expect(record.planStale).toBe(true)
  })

  it('marks failed tasks with their error and outcome', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.task.failed', {
      task_id: 't2',
      status: 'failed',
      error: 'Deadline erreicht',
    }))
    expect(record.taskStates.t2).toMatchObject({ status: 'failed', error: 'Deadline erreicht' })
  })

  it('keeps cancellation requested distinct until the task settles', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.task.started', {
      task_id: 't-cancel',
      attempt: 1,
    }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.task.cancel_requested', {
      task_id: 't-cancel',
      status: 'cancel_requested',
    }))

    expect(record.taskStates['t-cancel'].status).toBe('cancel_requested')
    expect(record.planStale).toBe(true)

    record = applyAgentRunEvent(record, event(3, 'inqtrix.agent.task.finished', {
      task_id: 't-cancel',
      status: 'cancelled',
    }))
    expect(record.taskStates['t-cancel']).toMatchObject({
      status: 'cancelled',
      outcome: 'cancelled',
    })
  })

  it('keeps insufficient evidence distinct from failure and completion', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.task.finished', {
      task_id: 't3',
      status: 'insufficient_evidence',
      error: 'Only one source met the evidence contract.',
    }))
    expect(record.taskStates.t3).toMatchObject({
      status: 'insufficient_evidence',
      outcome: 'insufficient_evidence',
    })
  })

  it('keeps identity-only child progress in preparing state and consumes its projection', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.child.progress', {
      task_id: 't1',
      child_run_id: 'run_child_1',
      snapshot: {},
      run_status: 'running',
      current_node: 'search',
      message: 'Searching primary sources',
      metrics: { sources: 4, queries: 2 },
      attempt: 2,
      updated_at: 1_700_000_101,
      error: { type: 'provider_error', message: 'Child provider unavailable' },
    }))
    expect(record.children.run_child_1.snapshot).toBeUndefined()
    expect(record.children.run_child_1).toMatchObject({
      currentNode: 'search',
      message: 'Searching primary sources',
      metrics: { sources: 4, queries: 2 },
      attempt: 2,
      error: 'Child provider unavailable',
      errorCode: 'provider_error',
    })
    expect(record.taskStates.t1).toMatchObject({
      status: 'running',
      attempt: 2,
      childRunId: 'run_child_1',
      error: 'Child provider unavailable',
      errorCode: 'provider_error',
    })
  })

  it('artifact events only flag staleness — cached rows stay untouched', () => {
    let record = agentRunFromSummary(summary())
    const cached = {
      artifactId: 'art1', kind: 'memo' as const, title: 'Memo',
      status: 'writing' as const, revision: 1, updatedBy: 'agent' as const,
      refsCount: 0, createdAt: 1, updatedAt: 1, contentMarkdown: 'rev-1 body',
    }
    record = {
      ...record,
      artifactOrder: ['art1'],
      artifacts: { art1: cached },
      artifactsStale: false,
    }
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.artifact.updated', {
      artifact_id: 'art1',
      revision: 3,
      updated_by: 'agent',
    }))
    // Adopting the bumped revision over the OLD body would defeat the
    // list-refetch staleness check and let the cached body silently
    // overwrite the newer server revision.
    expect(record.artifacts.art1).toBe(cached)
    expect(record.artifacts.art1.revision).toBe(1)
    expect(record.artifactsStale).toBe(true)
  })

  it('publishes answer deltas into one Markdown artifact and detects gaps', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.answer.started', {
      artifact_id: 'art-answer',
      publication_id: 'pub-answer',
      status: 'writing',
    }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.output_text.delta', {
      artifact_id: 'art-answer',
      publication_id: 'pub-answer',
      offset: 0,
      delta: '**Preis:** ',
    }))
    const firstBytes = new TextEncoder().encode('**Preis:** ').byteLength
    record = applyAgentRunEvent(record, event(3, 'inqtrix.output_text.delta', {
      artifact_id: 'art-answer',
      publication_id: 'pub-answer',
      offset: firstBytes,
      delta: '5 €',
    }))

    expect(record.artifacts['art-answer']).toMatchObject({
      contentMarkdown: '**Preis:** 5 €',
      publicationId: 'pub-answer',
      status: 'writing',
    })

    const gapped = applyAgentRunEvent(record, event(4, 'inqtrix.output_text.delta', {
      artifact_id: 'art-answer',
      publication_id: 'pub-answer',
      offset: 999,
      delta: 'untrusted gap',
    }))
    expect(gapped.artifacts['art-answer'].contentMarkdown).toBe('**Preis:** 5 €')
    expect(gapped.artifactsStale).toBe(true)
    // The gap is remembered on the artifact so the list refetch at settle
    // never keeps the truncated stream as the finished answer.
    expect(gapped.artifacts['art-answer'].publicationGap).toBe(true)

    const ready = applyAgentRunEvent(gapped, event(5, 'inqtrix.answer.ready', {
      artifact_id: 'art-answer',
      publication_id: 'pub-answer',
      status: 'ready',
    }))
    expect(ready.artifacts['art-answer']).toMatchObject({
      contentMarkdown: '**Preis:** 5 €',
      publicationNeedsReconcile: true,
      status: 'ready',
    })
    expect(ready.artifactsStale).toBe(true)
  })

  it('does not fetch a completed answer ahead of its streaming publication', () => {
    const record = {
      ...agentRunFromSummary(summary()),
      artifactsStale: false,
    }
    const signalled = applyAgentRunEvent(record, event(
      1,
      'inqtrix.agent.artifact.created',
      { artifact_id: 'art-answer', kind: 'answer', revision: 1 },
    ))
    expect(signalled.artifactsStale).toBe(false)

    const failed = applyAgentRunEvent(signalled, event(
      2,
      'inqtrix.run.failed',
      { error: { message: 'publication failed', type: 'server_error' } },
    ))
    expect(failed.artifactsStale).toBe(true)
  })

  it('maps waiting and terminal run events onto the status', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.run.waiting', { status: 'waiting_for_approval' }))
    expect(record.status).toBe('waiting_for_approval')
    record = applyAgentRunEvent(record, event(2, 'inqtrix.run.waiting', { status: 'waiting_for_input' }))
    expect(record.status).toBe('waiting_for_input')
    record = applyAgentRunEvent(record, event(3, 'inqtrix.run.completed', { status: 'completed' }))
    expect(record.status).toBe('completed')
    expect(record.artifactsStale).toBe(true)
    expect(record.finishedAt).toBe('2023-11-14T22:15:03.000Z')
    expect(record.elapsedSeconds).toBe(102)
  })

  it('starts the run stopwatch from the live boundary when the queued summary had no start', () => {
    let record = agentRunFromSummary(summary({ started_at: null }))
    record = applyAgentRunEvent(record, event(1, 'inqtrix.run.started'))
    record = applyAgentRunEvent(record, event(9, 'inqtrix.run.completed'))

    expect(record.startedAt).toBe('2023-11-14T22:15:01.000Z')
    expect(record.elapsedSeconds).toBe(8)
  })

  it('marks a resumed segment running without replacing the first start', () => {
    let record = agentRunFromSummary(summary())
    const firstStartedAt = record.startedAt
    record = applyAgentRunEvent(record, event(1, 'inqtrix.run.queued', {
      resumed: true,
      segment_id: 'seg_agent_1_2',
    }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.run.resumed', {
      status: 'running',
      segment_id: 'seg_agent_1_2',
      segment_ordinal: 2,
    }))

    expect(record.status).toBe('running')
    expect(record.startedAt).toBe(firstStartedAt)
  })

  it('preserves authoritative terminal timing while replaying persisted events', () => {
    let record = agentRunFromSummary(summary({
      elapsed_seconds: 77,
      finished_at: 1_700_000_078,
      status: 'completed',
    }))
    record = applyAgentRunEvent(record, event(9, 'inqtrix.run.completed'))

    expect(record.finishedAt).toBe('2023-11-14T22:14:38.000Z')
    expect(record.elapsedSeconds).toBe(77)
  })

  it('maps kernel tool events onto the ONE activity-step protocol', () => {
    let record = agentRunFromSummary(summary({ mode: 'agent_kernel' }))
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.tool.started', {
      tool: 'web_instant',
      tool_call_id: 'call_1',
      args_preview: 'WM 2026 Spielplan',
    }))

    // Live line: operation + literal query, running.
    expect(record.activity).toMatchObject({
      detail: 'WM 2026 Spielplan',
      operation: 'web_instant',
      status: 'running',
    })
    const running = record.stepLog.filter((item) => item.kind === 'activity')
    expect(running).toHaveLength(1)
    expect(running[0]).toMatchObject({
      activityOperation: 'web_instant',
      detail: 'WM 2026 Spielplan',
      status: 'running',
    })

    // finished SETTLES the same row (no second line) and keeps the query.
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.tool.finished', {
      tool: 'web_instant',
      tool_call_id: 'call_1',
      status: 'success',
    }))
    const settled = record.stepLog.filter((item) => item.kind === 'activity')
    expect(settled).toHaveLength(1)
    expect(settled[0]).toMatchObject({
      detail: 'WM 2026 Spielplan',
      status: 'completed',
    })
  })

  it('keeps one live row per read document even when two share a title', () => {
    let record = agentRunFromSummary(summary())
    const reading = (sequence: number, documentId: string, status: string, current: number, total: number) =>
      event(sequence, 'inqtrix.agent.activity', {
        kind: 'reading', operation: 'document_analysis', phase: 'execution', task_id: 't1',
        activity_id: `t1:document-analysis:${documentId}`, detail: 'Vertrag',
        document_id: documentId, status, current, total,
      })
    record = applyAgentRunEvent(record, reading(1, 'kd_1', 'started', 1, 4))
    record = applyAgentRunEvent(record, reading(2, 'kd_2', 'started', 1, 8))
    record = applyAgentRunEvent(record, reading(3, 'kd_1', 'completed', 4, 4))
    const rows = record.stepLog.filter((item) => item.kind === 'activity')
    expect(rows).toHaveLength(2)
    expect(rows.map((row) => [row.status, row.current, row.total])).toEqual([
      ['completed', 4, 4],
      ['started', 1, 8],
    ])
  })

  it('settles kernel tool rows of tools outside the operation vocabulary', () => {
    let record = agentRunFromSummary(summary({ mode: 'agent_kernel' }))
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.tool.started', {
      tool: 'read_canvas',
      tool_call_id: 'call_2',
      args_preview: '',
      artifact_id: 'art_1',
    }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.tool.finished', {
      tool: 'read_canvas',
      tool_call_id: 'call_2',
      status: 'error',
    }))
    const rows = record.stepLog.filter((item) => item.kind === 'activity')
    expect(rows).toHaveLength(1)
    // The settled row still knows which document it read.
    expect(rows[0]).toMatchObject({ artifactId: 'art_1', label: 'read_canvas', status: 'failed' })
  })

  it('shows no preview for a legacy JSON args_preview without a query, never its id', () => {
    let record = agentRunFromSummary(summary({ mode: 'agent_kernel' }))
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.tool.started', {
      tool: 'read_research_report',
      tool_call_id: 'call_r',
      args_preview: '{"report_id": "run_8198"}',
    }))
    expect(record.activity?.detail ?? '').toBe('')
  })

  it('unwraps a legacy JSON args_preview to the bare query', () => {
    let record = agentRunFromSummary(summary({ mode: 'agent_kernel' }))
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.tool.started', {
      tool: 'search_project_knowledge',
      tool_call_id: 'call_3',
      args_preview: '{"query": "Compliance-Last Anbieter"}',
    }))
    expect(record.activity).toMatchObject({
      detail: 'Compliance-Last Anbieter',
      operation: 'knowledge_search',
    })
  })

  it('settles a tool row across a phase flip and by bare call id', () => {
    let record = agentRunFromSummary(summary({ mode: 'agent_kernel' }))
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.tool.started', {
      tool: 'web_instant',
      tool_call_id: 'call_9',
      args_preview: 'Marktlage',
    }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.phase.changed', {
      phase: 'done', previous_phase: 'execution',
    }))
    // finished after the phase flip AND with a lost tool name: the call
    // id alone must settle the SAME row.
    record = applyAgentRunEvent(record, event(3, 'inqtrix.agent.tool.finished', {
      tool: '',
      tool_call_id: 'call_9',
      status: 'success',
    }))
    const rows = record.stepLog.filter((item) => item.kind === 'activity')
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      detail: 'Marktlage',
      status: 'completed',
      activityOperation: 'web_instant',
      activityOperationCode: 'web_instant',
    })
    expect(record.activity).toMatchObject({
      operation: 'web_instant',
      operationCode: 'web_instant',
      status: 'completed',
    })
  })

  it('settles still-running tool rows at a terminal run event', () => {
    let record = agentRunFromSummary(summary({ mode: 'agent_kernel' }))
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.tool.started', {
      tool: 'web_instant',
      tool_call_id: 'call_10',
      args_preview: 'Abbruch mitten im Call',
    }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.run.failed', {
      error: { code: 'iteration_limit', message: 'Limit erreicht.' },
    }))
    const rows = record.stepLog.filter((item) => item.kind === 'activity')
    expect(rows).toHaveLength(1)
    expect(rows[0]?.status).toBe('failed')
  })

  it.each(['write_todos', 'ask_user'])('keeps %s completions off the activity log even without a reply name', (tool) => {
    let record = agentRunFromSummary(summary({ mode: 'agent_kernel' }))
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.tool.started', {
      tool,
      tool_call_id: 'call_4',
      args_preview: '{"todos": []}',
    }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.tool.finished', {
      tool: '', tool_call_id: 'call_4', status: 'error',
    }))
    expect(record.stepLog).toHaveLength(0)
    expect(record.activity).toBeUndefined()
  })

  it('does not invent a search from an anonymous orphan completion', () => {
    let record = agentRunFromSummary(summary({ mode: 'agent_kernel' }))
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.tool.finished', {
      tool: '', tool_call_id: 'unseen_call', status: 'error',
    }))
    expect(record.stepLog).toHaveLength(0)
    expect(record.activity).toBeUndefined()
  })

  it('reads structured run.failed errors with the legacy message fallback', () => {
    let record = agentRunFromSummary(summary({ mode: 'agent_kernel' }))
    record = applyAgentRunEvent(record, event(1, 'inqtrix.run.failed', {
      error: { code: 'iteration_limit', message: 'Der Lauf hat sein Schritt-Limit erreicht.' },
    }))
    expect(record.status).toBe('failed')
    expect(record.error).toBe('Der Lauf hat sein Schritt-Limit erreicht.')

    let legacy = agentRunFromSummary(summary({ mode: 'agent_kernel' }))
    legacy = applyAgentRunEvent(legacy, event(1, 'inqtrix.run.failed', {
      message: 'Serverfehler.',
    }))
    expect(legacy.error).toBe('Serverfehler.')
  })

  it('captures the live activity line', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.activity', { kind: 'searching', probe: 'Marktlage 2026' }))
    expect(record.activity).toMatchObject({ kind: 'searching', detail: 'Marktlage 2026' })
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.activity', {
      detail: 'Das Memo braucht weitere Evidenz zu: Alle Anbieter mit Quelle belegt.',
      kind: 'critic_research',
    }))
    expect(record.activity).toMatchObject({
      detail: 'Das Memo braucht weitere Evidenz zu: Alle Anbieter mit Quelle belegt.',
      kind: 'critic_research',
    })
  })

  it('carries the flagged passages of a review into the step log', () => {
    // Measured gap: the server sent the findings and nothing on this side
    // read them, so a review's substance never reached a reader — only
    // the prose that says how many there were.
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.activity', {
      kind: 'deep_review_findings',
      label: 'Verifikation: 2 Befunde',
      findings: [
        { quote: 'Der Markt wuchs um 12 %.', finding: 'Ohne Beleg.', target: 'chat' },
        { quote: 'Die Quelle ist eindeutig.', finding: 'Widerspruch zu [K2].', target: 'art_1' },
        { quote: '', finding: 'Ohne Zitat.', target: 'chat' },
        'kein Objekt',
      ],
    }))
    const step = record.stepLog.at(-1)
    // Half a finding renders as a quotation mark around nothing, so the
    // reducer drops the incomplete rows instead of passing them on.
    expect(step?.findings).toEqual([
      { quote: 'Der Markt wuchs um 12 %.', finding: 'Ohne Beleg.', target: 'chat' },
      { quote: 'Die Quelle ist eindeutig.', finding: 'Widerspruch zu [K2].', target: 'art_1' },
    ])
  })

  it('leaves an activity without findings unchanged', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.activity', {
      kind: 'searching', probe: 'Marktlage 2026',
    }))
    expect(record.stepLog.at(-1)?.findings).toBeUndefined()
  })

  it('aggregates repeated semantic operations and projects local task progress', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.activity', {
      kind: 'searching',
      probe: 'knowledge.search',
      task_id: 'k1',
      current: 1,
      total: 4,
    }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.activity', {
      kind: 'searching',
      operation: 'knowledge_search',
      task_id: 'k1',
      current: 2,
      total: 4,
    }))
    expect(record.stepLog.filter((entry) => entry.kind === 'activity')).toHaveLength(1)
    expect(record.stepLog.find((entry) => entry.kind === 'activity')).toMatchObject({
      activityCount: 2,
      activityOperation: 'knowledge_search',
      current: 2,
      total: 4,
    })
    expect(record.taskStates.k1.activity).toMatchObject({
      operation: 'knowledge_search',
      current: 2,
      total: 4,
    })
  })

  it('gives every query of a multi-query task its own protocol row', () => {
    // Regression (Verlauf = Portal): query 2..N of one task used to
    // coalesce into row 1's counter and vanish entirely once the task
    // settled — "search 6 of 6" was never visible as a step.
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.activity', {
      kind: 'searching',
      operation: 'knowledge_search',
      task_id: 'k1',
      status: 'started',
      query: 'EU AI Act Fristen',
      current: 1,
      total: 2,
    }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.activity', {
      kind: 'searching',
      operation: 'knowledge_search',
      task_id: 'k1',
      status: 'completed',
      query: 'EU AI Act Fristen',
      current: 1,
      total: 2,
    }))
    record = applyAgentRunEvent(record, event(3, 'inqtrix.agent.activity', {
      kind: 'searching',
      operation: 'knowledge_search',
      task_id: 'k1',
      status: 'started',
      query: 'EU AI Act Sanktionen',
      current: 2,
      total: 2,
    }))
    const rows = record.stepLog.filter((entry) => entry.kind === 'activity')
    expect(rows).toHaveLength(2)
    expect(rows[0]).toMatchObject({
      detail: 'EU AI Act Fristen',
      status: 'completed',
    })
    expect(rows[1]).toMatchObject({
      detail: 'EU AI Act Sanktionen',
      status: 'started',
    })
    // The settled row KEEPS its place — a later task_finished event
    // must not remove protocol lines (append-only contract).
    record = applyAgentRunEvent(record, event(4, 'inqtrix.agent.task.finished', {
      task_id: 'k1',
      status: 'completed',
    }))
    expect(
      record.stepLog.filter((entry) => entry.kind === 'activity'),
    ).toHaveLength(2)
  })

  it('keeps provider retry notices on their invocation row and settles at task end', () => {
    // Regression: retry frames carry a changed detail text ("… Versuch
    // 2/3 …"); keyed by detail they opened ghost rows that never
    // settled. The bare `query` is the stable invocation identity, and
    // the task's terminal event closes any row still open.
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.activity', {
      kind: 'searching',
      operation: 'knowledge_search',
      task_id: 'k1',
      status: 'started',
      query: 'EU AI Act Fristen',
    }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.activity', {
      kind: 'searching',
      operation: 'knowledge_search',
      task_id: 'k1',
      status: 'started',
      query: 'EU AI Act Fristen',
      detail: 'EU AI Act Fristen · Provider-Versuch 2/3 fehlgeschlagen',
      attempt: 2,
    }))
    let rows = record.stepLog.filter((entry) => entry.kind === 'activity')
    expect(rows).toHaveLength(1)
    expect(rows[0].detail).toContain('Provider-Versuch 2/3')
    // Hard-terminal task: the still-open row must not stay "running".
    record = applyAgentRunEvent(record, event(3, 'inqtrix.agent.task.finished', {
      task_id: 'k1',
      status: 'completed',
    }))
    rows = record.stepLog.filter((entry) => entry.kind === 'activity')
    expect(rows).toHaveLength(1)
    expect(rows[0].status).toBe('completed')
  })

  it('keeps six parallel tasks as six distinct settled rows', () => {
    let record = agentRunFromSummary(summary())
    for (let index = 1; index <= 6; index += 1) {
      record = applyAgentRunEvent(record, event(index, 'inqtrix.agent.activity', {
        kind: 'searching',
        operation: 'knowledge_search',
        task_id: `t${index}`,
        status: 'started',
        query: `Frage ${index}`,
      }))
    }
    for (let index = 1; index <= 6; index += 1) {
      record = applyAgentRunEvent(
        record,
        event(6 + index, 'inqtrix.agent.task.finished', {
          task_id: `t${index}`,
          status: 'completed',
        }),
      )
    }
    const rows = record.stepLog.filter((entry) => entry.kind === 'activity')
    expect(rows).toHaveLength(6)
    expect(rows.every((row) => row.status === 'completed')).toBe(true)
  })

  it('does not double-count started and completed frames for one invocation', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.activity', {
      activity_id: 'op-1',
      operation: 'web_instant',
      task_id: 'w1',
      status: 'started',
      query: 'EU AI Act latest changes',
    }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.activity', {
      activity_id: 'op-1',
      operation: 'web_instant',
      task_id: 'w1',
      status: 'completed',
      purpose: 'Check the current regulatory state',
      metrics: { result_count: 6 },
      attempt: 1,
    }))
    const activity = record.stepLog.find((entry) => entry.kind === 'activity')
    expect(activity?.activityCount).toBe(1)
    expect(record.taskStates.w1.activityHistory).toHaveLength(1)
    expect(record.taskStates.w1.activityHistory?.[0].status).toBe('completed')
    expect(record.taskStates.w1.activityHistory?.[0].detail).toBe(
      'EU AI Act latest changes',
    )
    expect(record.taskStates.w1.activityHistory?.[0]).toMatchObject({
      attempt: 1,
      metrics: { result_count: 6 },
      purpose: 'Check the current regulatory state',
    })
  })

  it('uses nested activity errors instead of mislabelling the query as failure', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.activity', {
      activity_id: 'op-2',
      operation: 'web_instant',
      task_id: 'w2',
      status: 'failed',
      query: 'AI market size',
      error: { code: 'search_timeout', message: 'Search provider timed out' },
    }))
    expect(record.taskStates.w2.error).toBe('Search provider timed out')
    expect(record.taskStates.w2.errorCode).toBe('search_timeout')
    expect(record.taskStates.w2.error).not.toBe('AI market size')
  })

  it('retains terminal task metrics for local work units', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.task.finished', {
      task_id: 'k1',
      status: 'completed',
      metrics: { sources: 7, queries: 3, claims: 4 },
      result_summary: 'Seven relevant sources support the finding.',
    }))
    expect(record.taskStates.k1.metrics).toEqual({
      sources: 7,
      queries: 3,
      claims: 4,
    })
    expect(record.taskStates.k1.resultSummary).toBe(
      'Seven relevant sources support the finding.',
    )
  })

  it('preserves the earlier activity finish when a parallel wave folds later', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.task.started', {
      task_id: 'w1',
    }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.activity', {
      activity_id: 'web-1',
      operation: 'web_instant',
      status: 'started',
      task_id: 'w1',
    }))
    record = applyAgentRunEvent(record, event(19, 'inqtrix.agent.activity', {
      activity_id: 'web-1',
      operation: 'web_instant',
      status: 'completed',
      task_id: 'w1',
    }))
    record = applyAgentRunEvent(record, event(47, 'inqtrix.agent.task.finished', {
      status: 'completed',
      task_id: 'w1',
    }))
    expect(record.taskStates.w1).toMatchObject({
      finishedAt: 1_700_000_119,
      startedAt: 1_700_000_101,
    })
  })

  it('marks a visible fallback on its task state', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.activity', {
      operation: 'task.legacy_budget_ignored',
      detail: 'Legacy task budget ignored',
      task_id: 'w1',
      status: 'completed',
      fallback: true,
    }))
    expect(record.taskStates.w1.fallback).toBe(true)
    expect(record.taskStates.w1.activity?.detail).toBe('Legacy task budget ignored')
  })

  it('keeps a completed task with an evidence limit in the completed group', () => {
    const planned = (taskId: string, ordinal: number): AgentPlanTaskRecord => ({
      taskId, ordinal, title: taskId, toolKind: 'web_research', objective: '',
      queries: [], gapIds: [], dependsOn: [], budget: {}, params: {},
      expectedOutput: '', status: 'running', childRunId: null, resultSummary: '',
    })
    const withLimit = planned('w1', 0)
    const withFallback = planned('w2', 1)
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.task.started', {
      task_id: 'w1',
    }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.activity', {
      activity_id: 'evidence-review:w1',
      operation: 'task.evidence_partial',
      detail: 'Teilaufgabe „Marktlage“: 3 Quellen nicht ausgewertet',
      task_id: 'w1',
      status: 'completed',
      evidence_limit: true,
    }))
    record = applyAgentRunEvent(record, event(3, 'inqtrix.agent.task.finished', {
      status: 'completed',
      task_id: 'w1',
    }))
    // The counter-case: a real fallback path on an equally completed task.
    record = applyAgentRunEvent(record, event(4, 'inqtrix.agent.activity', {
      operation: 'task.legacy_budget_ignored',
      detail: 'Legacy task budget ignored',
      task_id: 'w2',
      status: 'completed',
      fallback: true,
    }))
    record = applyAgentRunEvent(record, event(5, 'inqtrix.agent.task.finished', {
      status: 'completed',
      task_id: 'w2',
    }))
    const live = record.taskStates.w1
    expect(live.evidenceLimit).toBe(true)
    expect(live.fallback).toBeUndefined()
    // The run view's own grouping, fed the reducer's task states.
    expect(agentTaskGroups([withLimit, withFallback], record.taskStates)).toEqual([
      { kind: 'attention', tasks: [withFallback] },
      { kind: 'completed', tasks: [withLimit] },
    ])
    const row = record.stepLog.find((entry) => entry.activityOperationCode === 'task.evidence_partial')
    expect(row?.evidenceLimit).toBe(true)
    expect(row?.fallback).toBe(false)
  })

  it('reads the fallback flag of a stored evidence-limit event as its evidence limit', () => {
    // Events stored by earlier versions carry ``fallback: true`` on both
    // evidence-limit codes; a reopened run keeps those completed tasks in
    // the completed group instead of under attention.
    const planned = (taskId: string, ordinal: number): AgentPlanTaskRecord => ({
      taskId, ordinal, title: taskId, toolKind: 'web_research', objective: '',
      queries: [], gapIds: [], dependsOn: [], budget: {}, params: {},
      expectedOutput: '', status: 'running', childRunId: null, resultSummary: '',
    })
    const stored = [
      { taskId: 'w1', operation: 'task.evidence_partial', detail: 'Teilaufgabe „Marktlage“: 3 Quellen nicht ausgewertet' },
      { taskId: 'w2', operation: 'task.evidence_needs_review', detail: 'Teilaufgabe „Preise“: nur einfach belegt' },
    ]
    let record = agentRunFromSummary(summary())
    let sequence = 0
    for (const { taskId, operation, detail } of stored) {
      record = applyAgentRunEvent(record, event(++sequence, 'inqtrix.agent.task.started', {
        task_id: taskId,
      }))
      record = applyAgentRunEvent(record, event(++sequence, 'inqtrix.agent.activity', {
        activity_id: `evidence-review:${taskId}`,
        operation,
        detail,
        task_id: taskId,
        status: 'completed',
        fallback: true,
      }))
      record = applyAgentRunEvent(record, event(++sequence, 'inqtrix.agent.task.finished', {
        status: 'completed',
        task_id: taskId,
      }))
    }
    for (const { taskId, operation } of stored) {
      expect(record.taskStates[taskId].evidenceLimit).toBe(true)
      expect(record.taskStates[taskId].fallback).toBeUndefined()
      const row = record.stepLog.find((entry) => entry.activityOperationCode === operation)
      expect(row?.evidenceLimit).toBe(true)
      expect(row?.fallback).toBe(false)
    }
    const tasks = [planned('w1', 0), planned('w2', 1)]
    expect(agentTaskGroups(tasks, record.taskStates)).toEqual([
      { kind: 'completed', tasks },
    ])
  })

  it('returns a failed child task to running on a queued retry projection', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.child.progress', {
      task_id: 't1',
      child_run_id: 'child-1',
      run_status: 'failed',
      attempt: 1,
      error: { code: 'timeout', message: 'First attempt timed out' },
    }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.child.progress', {
      task_id: 't1',
      child_run_id: 'child-1',
      run_status: 'queued',
      attempt: 2,
    }))
    expect(record.taskStates.t1).toMatchObject({
      status: 'running',
      attempt: 2,
      error: 'First attempt timed out',
      errorCode: 'timeout',
    })
    expect(record.taskStates.t1.finishedAt).toBeUndefined()
  })

  it('preserves an unknown stable operation code for technical detail', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.activity', {
      operation: 'vendor.custom.lookup',
      task_id: 'x1',
      status: 'started',
    }))
    expect(record.taskStates.x1.activity).toMatchObject({
      operation: undefined,
      operationCode: 'vendor.custom.lookup',
    })
  })
})

describe('stepLog (transcript lines)', () => {
  it('appends ordered entries for phases, activity, plan and tasks', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(
      record,
      event(1, 'inqtrix.agent.phase.changed', { phase: 'discovery' }),
    )
    record = applyAgentRunEvent(
      record,
      event(2, 'inqtrix.agent.activity', {
        kind: 'searching',
        probe: 'EU AI Act Anforderungen',
      }),
    )
    record = applyAgentRunEvent(
      record,
      event(3, 'inqtrix.agent.plan.proposed', {
        plan_id: 'plan_1',
        version: 1,
      }),
    )
    record = applyAgentRunEvent(
      record,
      event(4, 'inqtrix.agent.task.started', { task_id: 't1', attempt: 1 }),
    )
    record = applyAgentRunEvent(
      record,
      event(5, 'inqtrix.agent.task.failed', {
        task_id: 't1',
        status: 'failed',
        error: 'Sammlung nicht sichtbar oder unbekannt: kc_x',
      }),
    )
    expect(record.stepLog.map((entry) => entry.kind)).toEqual([
      'phase',
      'activity',
      'plan',
      'task_started',
      'task_failed',
    ])
    expect(record.stepLog[1].detail).toBe('EU AI Act Anforderungen')
    expect(record.stepLog[2].version).toBe(1)
    expect(record.stepLog[4].error).toContain('kc_x')
    // Entries carry the event sequence for stable keys/ordering.
    expect(record.stepLog.map((entry) => entry.seq)).toEqual([1, 2, 3, 4, 5])
  })

  it('appends decision markers that join with the control rows', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(
      record,
      event(1, 'inqtrix.agent.clarification.answered', {
        clarification_id: 'cl_1',
      }),
    )
    record = applyAgentRunEvent(
      record,
      event(2, 'inqtrix.agent.approval.decided', {
        approval_id: 'apr_1',
        status: 'approved',
      }),
    )
    expect(record.stepLog).toEqual([
      expect.objectContaining({
        clarificationId: 'cl_1',
        kind: 'clarification_answered',
      }),
      expect.objectContaining({
        approvalId: 'apr_1',
        detail: 'approved',
        kind: 'approval_decided',
      }),
    ])
  })

  it('captures narration prose from the narration event', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(
      record,
      event(1, 'inqtrix.agent.narration', {
        kind: 'discovery',
        narration_id: 'n-discovery',
        phase: 'discovery',
        text: 'Ich habe eine Sammlung mit 12 Dokumenten gefunden.',
      }),
    )
    expect(record.stepLog[0]).toEqual(
      expect.objectContaining({
        kind: 'narration',
        narrationId: 'n-discovery',
        phase: 'discovery',
        text: 'Ich habe eine Sammlung mit 12 Dokumenten gefunden.',
      }),
    )
  })

  it('preserves catch-up versus live provenance on transcript entries', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(
      record,
      event(1, 'inqtrix.agent.narration', {
        narration_id: 'n-discovery',
        text: 'Persistierte Zwischenmeldung.',
      }),
      { arrivedLive: false },
    )
    expect(record.stepLog[0].arrivedLive).toBe(false)

    record = applyAgentRunEvent(
      record,
      event(2, 'inqtrix.agent.narration', {
        narration_id: 'n-discovery',
        text: 'Neue Live-Fortsetzung.',
      }),
      { arrivedLive: true },
    )
    expect(record.stepLog[0]).toMatchObject({
      arrivedLive: true,
      seq: 1,
      text: 'Neue Live-Fortsetzung.',
    })
  })

  it('upserts a re-emitted narration id in place instead of duplicating', () => {
    // The critic replan loop re-runs synthesis and re-emits the stable
    // narration_id 'n-synthesis' with a FRESH higher sequence — the
    // sequence guard does not suppress it, so without id-dedup the line
    // would multiply in the transcript.
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(
      record,
      event(1, 'inqtrix.agent.narration', {
        narration_id: 'n-synthesis',
        text: 'Ich schreibe das Memo mit 3 Abschnitten.',
      }),
    )
    record = applyAgentRunEvent(
      record,
      event(2, 'inqtrix.agent.activity', { kind: 'searching', probe: 'x' }),
    )
    record = applyAgentRunEvent(
      record,
      event(9, 'inqtrix.agent.narration', {
        narration_id: 'n-synthesis',
        text: 'Ich schreibe das Memo mit 4 Abschnitten.',
      }),
    )
    const narrations = record.stepLog.filter(
      (entry) => entry.kind === 'narration',
    )
    expect(narrations).toHaveLength(1)
    // Text updated in place; seq stays at the original for key stability.
    expect(narrations[0].text).toBe('Ich schreibe das Memo mit 4 Abschnitten.')
    expect(narrations[0].seq).toBe(1)
    // The intervening activity line kept its position (no reordering).
    expect(record.stepLog.map((entry) => entry.kind)).toEqual([
      'narration',
      'activity',
    ])
  })

  it('replays are no-ops for the log too', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(
      record,
      event(3, 'inqtrix.agent.phase.changed', { phase: 'planning' }),
    )
    const replay = applyAgentRunEvent(
      record,
      event(3, 'inqtrix.agent.phase.changed', { phase: 'planning' }),
    )
    expect(replay).toBe(record)
    expect(record.stepLog).toHaveLength(1)
  })
})

describe('model_resolution events', () => {
  it('keeps only ANSWER-node resolutions on the record', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(
      record,
      event(1, 'inqtrix.node.model_resolution', {
        node: 'agent_intake',
        model: 'fast-model',
        effort: '',
        tier: 'fast',
        model_source: 'tier:fast',
      }),
    )
    // Assembly-line provenance never drives the chip.
    expect(record.modelResolution).toBeUndefined()
    record = applyAgentRunEvent(
      record,
      event(2, 'inqtrix.node.model_resolution', {
        node: 'agent_synthesis',
        model: 'opus-x',
        effort: 'xhigh',
        tier: 'high',
        model_source: 'explicit_request',
      }),
    )
    expect(record.modelResolution).toEqual({
      model: 'opus-x',
      effort: 'xhigh',
      tier: 'high',
      modelSource: 'explicit_request',
    })
    // A later answer-node resolution wins (the kernel/answer call is
    // the one the chip should describe).
    record = applyAgentRunEvent(
      record,
      event(3, 'inqtrix.node.model_resolution', {
        node: 'agent_answer',
        model: 'opus-x',
        effort: 'high',
        tier: 'high',
        model_source: 'explicit_request',
      }),
    )
    expect(record.modelResolution?.effort).toBe('high')
  })
})

/**
 * File chips: artifact.created/updated events (kinds memo|deliverable)
 * collect into the turn's touchedArtifacts — deduped by artifact, first
 * touch keeps its position and its `created` provenance, an update
 * refreshes revision/title in place. Everything else never chips.
 */
describe('touchedArtifacts (file chips)', () => {
  it('collects a created memo and dedupes its later update in place', () => {
    const base = agentRunFromSummary(summary())
    const created = applyAgentRunEvent(
      base,
      event(1, 'inqtrix.agent.artifact.created', {
        artifact_id: 'art_memo',
        kind: 'memo',
        revision: 1,
        updated_by: 'agent',
      }),
      { arrivedLive: true },
    )
    expect(created.touchedArtifacts).toHaveLength(1)
    expect(created.touchedArtifacts[0]).toMatchObject({
      arrivedLive: true,
      artifactId: 'art_memo',
      created: true,
      kind: 'memo',
      revision: 1,
    })

    const withDeliverable = applyAgentRunEvent(
      created,
      event(2, 'inqtrix.agent.artifact.created', {
        artifact_id: 'art_report',
        kind: 'deliverable',
        revision: 1,
        title: 'Marktbericht',
      }),
      { arrivedLive: true },
    )
    const updated = applyAgentRunEvent(
      withDeliverable,
      event(3, 'inqtrix.agent.artifact.updated', {
        artifact_id: 'art_memo',
        kind: 'memo',
        revision: 2,
        updated_by: 'agent',
      }),
      { arrivedLive: true },
    )
    // Still two chips; the memo kept its FIRST position and its created
    // provenance while the revision advanced.
    expect(updated.touchedArtifacts.map((item) => item.artifactId)).toEqual([
      'art_memo',
      'art_report',
    ])
    expect(updated.touchedArtifacts[0].revision).toBe(2)
    expect(updated.touchedArtifacts[0].created).toBe(true)
    expect(updated.touchedArtifacts[1].title).toBe('Marktbericht')
  })

  it('never chips other kinds, kindless user PUTs, or conflict markers', () => {
    const base = agentRunFromSummary(summary())
    let record = applyAgentRunEvent(
      base,
      event(1, 'inqtrix.agent.artifact.created', {
        artifact_id: 'art_evidence',
        kind: 'evidence_bundle',
        revision: 1,
      }),
      { arrivedLive: true },
    )
    // A user PUT emits artifact.updated WITHOUT a kind field.
    record = applyAgentRunEvent(
      record,
      event(2, 'inqtrix.agent.artifact.updated', {
        artifact_id: 'art_memo',
        revision: 5,
        updated_by: 'user',
      }),
      { arrivedLive: true },
    )
    record = applyAgentRunEvent(
      record,
      event(3, 'inqtrix.agent.artifact.edit_conflict', {
        artifact_id: 'art_memo',
        kind: 'memo',
      }),
      { arrivedLive: true },
    )
    expect(record.touchedArtifacts).toEqual([])
    // The invalidation signal itself still fired: the list refetch that
    // reloads the rows is unaffected by the chip decision.
    expect(record.artifactsStale).toBe(true)
  })

  it('marks replayed chips as not live (no entrance motion)', () => {
    const base = agentRunFromSummary(summary())
    const replayed = applyAgentRunEvent(
      base,
      event(1, 'inqtrix.agent.artifact.created', {
        artifact_id: 'art_memo',
        kind: 'memo',
        revision: 1,
      }),
      { arrivedLive: false },
    )
    expect(replayed.touchedArtifacts[0].arrivedLive).toBe(false)
  })

  it('accumulates the server line delta and keeps from_revision sticky', () => {
    const base = agentRunFromSummary(summary())
    const first = applyAgentRunEvent(
      base,
      event(1, 'inqtrix.agent.artifact.updated', {
        artifact_id: 'art_doc',
        from_revision: 3,
        kind: 'deliverable',
        lines_added: 4,
        lines_removed: 1,
        revision: 4,
        title: 'Bericht',
      }),
      { arrivedLive: true },
    )
    expect(first.touchedArtifacts[0]).toMatchObject({
      created: false,
      fromRevision: 3,
      linesAdded: 4,
      linesRemoved: 1,
      revision: 4,
    })
    const second = applyAgentRunEvent(
      first,
      event(2, 'inqtrix.agent.artifact.updated', {
        artifact_id: 'art_doc',
        from_revision: 4,
        kind: 'deliverable',
        lines_added: 2,
        lines_removed: 5,
        revision: 5,
        title: 'Bericht',
      }),
      { arrivedLive: true },
    )
    // The turn diff spans FIRST touch -> latest revision; numbers sum.
    expect(second.touchedArtifacts[0]).toMatchObject({
      fromRevision: 3,
      linesAdded: 6,
      linesRemoved: 6,
      revision: 5,
    })
  })

  it('drops the sum honestly when one contributor carries no numbers', () => {
    const base = agentRunFromSummary(summary())
    const counted = applyAgentRunEvent(
      base,
      event(1, 'inqtrix.agent.artifact.updated', {
        artifact_id: 'art_doc',
        from_revision: 1,
        kind: 'deliverable',
        lines_added: 4,
        lines_removed: 1,
        revision: 2,
      }),
      { arrivedLive: true },
    )
    const degraded = applyAgentRunEvent(
      counted,
      // A row from a server that sends no line counts, or the size
      // guard: the revision advances without any numbers.
      event(2, 'inqtrix.agent.artifact.updated', {
        artifact_id: 'art_doc',
        kind: 'deliverable',
        revision: 3,
      }),
      { arrivedLive: true },
    )
    expect(degraded.touchedArtifacts[0].revision).toBe(3)
    expect(degraded.touchedArtifacts[0].fromRevision).toBe(1)
    expect(degraded.touchedArtifacts[0].linesAdded).toBeUndefined()
    expect(degraded.touchedArtifacts[0].linesRemoved).toBeUndefined()

    // A HALF payload (only one side present) is just as unsummable —
    // never a NaN, never a partial number.
    const half = applyAgentRunEvent(
      base,
      event(1, 'inqtrix.agent.artifact.updated', {
        artifact_id: 'art_half',
        kind: 'deliverable',
        lines_removed: 2,
        revision: 2,
      }),
      { arrivedLive: true },
    )
    expect(half.touchedArtifacts[0].linesAdded).toBeUndefined()
    expect(half.touchedArtifacts[0].linesRemoved).toBeUndefined()
  })

  it('derives from_revision when the event carries none (revision - 1)', () => {
    const base = agentRunFromSummary(summary())
    const legacy = applyAgentRunEvent(
      base,
      event(1, 'inqtrix.agent.artifact.updated', {
        artifact_id: 'art_doc',
        kind: 'memo',
        revision: 4,
      }),
      { arrivedLive: true },
    )
    expect(legacy.touchedArtifacts[0].fromRevision).toBe(3)
  })

  it('rebuilds the sent-comments record from the attached event', () => {
    const base = agentRunFromSummary(summary())
    const attached = applyAgentRunEvent(
      base,
      event(1, 'inqtrix.agent.canvas_context.attached', {
        artifact_id: 'art_doc',
        comments: [
          { comment: 'Bitte kuerzen.', quote_preview: 'Erster Satz\u2026' },
          { comment: 'Zahl ergaenzen.', quote_preview: 'Umsatz stieg' },
        ],
        revision: 3,
      }),
      { arrivedLive: false },
    )
    expect(attached.canvasContextMeta).toEqual({
      artifactId: 'art_doc',
      comments: [
        { comment: 'Bitte kuerzen.', quotePreview: 'Erster Satz\u2026' },
        { comment: 'Zahl ergaenzen.', quotePreview: 'Umsatz stieg' },
      ],
      revision: 3,
    })
  })

  it('records the rules a run works under, with their frozen revision', () => {
    // The revision is the one thing the typed question does not show.
    const base = agentRunFromSummary(summary())
    const attached = applyAgentRunEvent(
      base,
      event(1, 'inqtrix.agent.requirement.attached', {
        free_text_chars: 0,
        review: 'assignment',
        rules: [
          { label: 'Executive Briefing', revision: 3, template_id: 'pt_brief' },
          { label: 'Lektor', revision: 1, template_id: 'pt_lektor' },
        ],
      }),
      { arrivedLive: false },
    )
    expect(attached.requirementMeta).toEqual({
      freeTextChars: 0,
      review: 'assignment',
      rules: [
        { label: 'Executive Briefing', revision: 3, templateId: 'pt_brief' },
        { label: 'Lektor', revision: 1, templateId: 'pt_lektor' },
      ],
    })
  })

  it('keeps a role child\'s last settled step and the server\'s count', () => {
    const base = agentRunFromSummary(summary())
    const stepped = applyAgentRunEvent(
      base,
      event(1, 'inqtrix.agent.child.progress', {
        child_run_id: 'run_role', event_type: 'inqtrix.agent.tool.finished',
        run_status: 'running', task_id: 'call_review', tool: 'read_evidence', tool_calls: 5,
      }),
    )
    expect(stepped.children.run_role).toMatchObject({ lastTool: 'read_evidence', toolCalls: 5 })
    // A later lifecycle projection carries no step and keeps the last one.
    const later = applyAgentRunEvent(
      stepped,
      event(2, 'inqtrix.agent.child.progress', {
        child_run_id: 'run_role', event_type: 'inqtrix.agent.phase.changed',
        run_status: 'running', snapshot: { phase: 'execution' }, task_id: 'call_review',
      }),
    )
    expect(later.children.run_role).toMatchObject({ lastTool: 'read_evidence', toolCalls: 5 })
  })

  it('keeps the title a rule was attached under, and invents none', () => {
    // The title is what the pill showed when the rule was attached; a part
    // stored before titles were kept has none, and the reader falls back.
    const attached = applyAgentRunEvent(
      agentRunFromSummary(summary()),
      event(1, 'inqtrix.agent.requirement.attached', {
        free_text_chars: 0,
        review: 'assignment',
        rules: [
          { label: 'briefing', revision: 3, template_id: 'pt_brief', title: 'Executive Briefing' },
          { label: 'lektor', revision: 1, template_id: 'pt_lektor' },
        ],
      }),
      { arrivedLive: false },
    )
    expect(attached.requirementMeta?.rules).toEqual([
      { label: 'briefing', revision: 3, templateId: 'pt_brief', title: 'Executive Briefing' },
      { label: 'lektor', revision: 1, templateId: 'pt_lektor' },
    ])
  })

  it('keeps an unknown typed length and an unknown review value as they came', () => {
    const base = agentRunFromSummary(summary())
    const attached = applyAgentRunEvent(
      base,
      event(1, 'inqtrix.agent.requirement.attached', {
        free_text_chars: null,
        review: 'per_section',
        rules: [{ label: 'Lektor', revision: 2, template_id: 'pt_lektor' }],
      }),
      { arrivedLive: true },
    )
    expect(attached.requirementMeta).toMatchObject({ freeTextChars: null, review: 'per_section' })
  })

  it('says so, once, when it drops a rule part that is not whole', () => {
    // Half a rule name is no record — but dropping it silently would
    // hide a server that sends broken parts.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      const base = agentRunFromSummary(summary())
      const attached = applyAgentRunEvent(
        base,
        event(1, 'inqtrix.agent.requirement.attached', {
          free_text_chars: 0,
          review: 'assignment',
          rules: [
            { label: 'Lektor', revision: 2, template_id: 'pt_lektor' },
            { label: '', revision: 1, template_id: 'pt_x' },
            { label: 'Kaputt', template_id: 'pt_y' },
          ],
        }),
        { arrivedLive: true },
      )
      expect(attached.requirementMeta?.rules).toEqual([
        { label: 'Lektor', revision: 2, templateId: 'pt_lektor' },
      ])
      expect(warn).toHaveBeenCalledTimes(1)
      expect(String(warn.mock.calls[0][0])).toContain(base.runId)
    } finally {
      warn.mockRestore()
    }
  })

  it('stays quiet when every rule part is whole', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    try {
      applyAgentRunEvent(
        agentRunFromSummary(summary()),
        event(1, 'inqtrix.agent.requirement.attached', {
          free_text_chars: 0,
          review: 'assignment',
          rules: [{ label: 'Lektor', revision: 2, template_id: 'pt_lektor' }],
        }),
        { arrivedLive: true },
      )
      expect(warn).not.toHaveBeenCalled()
    } finally {
      warn.mockRestore()
    }
  })

  it('never chips a kind-less rename signal despite its title', () => {
    const base = agentRunFromSummary(summary())
    const renamed = applyAgentRunEvent(
      base,
      event(1, 'inqtrix.agent.artifact.updated', {
        artifact_id: 'art_doc',
        renamed: true,
        revision: 2,
        title: 'Neuer Titel',
        updated_by: 'user',
      }),
      { arrivedLive: true },
    )
    expect(renamed.touchedArtifacts).toEqual([])
    // The staleness signal still refreshes names everywhere.
    expect(renamed.artifactsStale).toBe(true)
  })
})

describe('nextOpenChildTasks', () => {
  it('collects a mission’s whole parallel wave', () => {
    let open = nextOpenChildTasks(undefined, 'inqtrix.agent.task.started', 0)
    for (const ordinal of [1, 2, 4, 3]) {
      open = nextOpenChildTasks(open, 'inqtrix.agent.task.started', ordinal)
    }
    expect(open).toEqual([0, 1, 2, 3, 4])
  })

  it('closes exactly the task that finished, keeping the straggler', () => {
    let open: number[] | undefined = [0, 1, 2, 3, 4]
    for (const ordinal of [1, 2, 4, 0]) {
      open = nextOpenChildTasks(open, 'inqtrix.agent.task.finished', ordinal)
    }
    expect(open).toEqual([3])
  })

  it('leaves the set alone for an unrelated event', () => {
    expect(
      nextOpenChildTasks([2], 'inqtrix.agent.phase.changed', undefined),
    ).toEqual([2])
  })

  it('has nothing to say before the first task', () => {
    expect(
      nextOpenChildTasks(undefined, 'inqtrix.agent.phase.changed', undefined),
    ).toBeUndefined()
  })
})

describe('answer.started announces the citation labels', () => {
  it('keeps them so the STREAMED text can render its citations', () => {
    // Without them the body streams with plain `[W1]` and is rewritten
    // wholesale the moment the answer settles — which reads as the
    // message being re-inserted.
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.answer.started', {
      artifact_id: 'art-answer',
      publication_id: 'pub-answer',
      reference_labels: ['W1', 'W2', 'W3'],
      status: 'writing',
    }))
    expect(record.artifacts['art-answer']?.publicationRefLabels).toEqual([
      'W1',
      'W2',
      'W3',
    ])
  })

  it('stays undefined when an older server announces none', () => {
    // Degrade to unlinked citations, never invent links.
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.answer.started', {
      artifact_id: 'art-answer',
      publication_id: 'pub-answer',
      status: 'writing',
    }))
    expect(
      record.artifacts['art-answer']?.publicationRefLabels,
    ).toBeUndefined()
  })
})

describe('critic revision discard activity', () => {
  it('lands as one step row that names what was discarded', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.activity', {
      kind: 'critic_revision_discarded',
      label: 'Kritik-Revision verworfen',
      detail: 'Die Ueberarbeitung hatte keine Abschnittsstruktur; das urspruengliche Memo bleibt bestehen.',
    }))
    expect(record.activity?.kind).toBe('critic_revision_discarded')
    const rows = record.stepLog.filter((entry) => entry.kind === 'activity')
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      detail: 'Die Ueberarbeitung hatte keine Abschnittsstruktur; das urspruengliche Memo bleibt bestehen.',
    })
  })
})

describe('failed child tasks settle and are counted', () => {
  it('a task.failed leaves the open set and joins the failed set', () => {
    let open = nextOpenChildTasks(undefined, 'inqtrix.agent.task.started', 5)
    open = nextOpenChildTasks(open, 'inqtrix.agent.task.started', 6)
    expect(open).toEqual([5, 6])
    open = nextOpenChildTasks(open, 'inqtrix.agent.task.failed', 5)
    expect(open).toEqual([6])
    let failed = nextFailedChildTasks(undefined, 'inqtrix.agent.task.failed', 5)
    failed = nextFailedChildTasks(failed, 'inqtrix.agent.task.finished', 6)
    expect(failed).toEqual([5])
    failed = nextFailedChildTasks(failed, 'inqtrix.agent.task.failed', 6)
    expect(failed).toEqual([5, 6])
  })

  it('remembers a started task title by ordinal for the child line', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.child.progress', {
      child_run_id: 'run_child',
      task_id: 'call_1',
      run_status: 'running',
      event_type: 'inqtrix.agent.task.started',
      ordinal: 4,
      tool_kind: 'web_research',
      title: 'Prüfe strittige Wochenmeldungen',
    }))
    expect(record.children.run_child.taskTitles).toEqual({ 4: 'Prüfe strittige Wochenmeldungen' })
    expect(record.children.run_child.openTasks).toEqual([4])
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.child.progress', {
      child_run_id: 'run_child',
      task_id: 'call_1',
      run_status: 'running',
      event_type: 'inqtrix.agent.task.failed',
      ordinal: 4,
      status: 'failed',
    }))
    expect(record.children.run_child.openTasks).toEqual([])
    expect(record.children.run_child.failedTasks).toEqual([4])
    expect(record.children.run_child.taskTitles).toEqual({ 4: 'Prüfe strittige Wochenmeldungen' })
  })
})

describe('a mission child\'s searches and synthesis lines reach its record', () => {
  it('keeps the latest started query, counts completed searches, ignores run scope', () => {
    let record = agentRunFromSummary(summary())
    const progress = (sequence: number, data: Record<string, unknown>) =>
      event(sequence, 'inqtrix.agent.child.progress', {
        child_run_id: 'run_child', task_id: 'call_1', run_status: 'running', ...data,
      })
    record = applyAgentRunEvent(record, progress(1, {
      event_type: 'inqtrix.agent.activity', operation: 'web.search.instant', status: 'started', query: 'Erste Suche',
    }))
    expect(record.children.run_child.lastQuery).toBe('Erste Suche')
    expect(record.children.run_child.searchesDone).toBeUndefined()
    record = applyAgentRunEvent(record, progress(2, {
      event_type: 'inqtrix.agent.activity', operation: 'web.search.instant', status: 'completed', query: 'Erste Suche',
    }))
    expect(record.children.run_child.searchesDone).toBe(1)
    record = applyAgentRunEvent(record, progress(3, {
      event_type: 'inqtrix.agent.activity', operation: 'knowledge.search', status: 'started', query: 'Zweite Suche',
    }))
    expect(record.children.run_child.lastQuery).toBe('Zweite Suche')
    // A non-search activity changes nothing.
    record = applyAgentRunEvent(record, progress(4, {
      event_type: 'inqtrix.agent.activity', operation: 'agent.model.turn', status: 'started',
    }))
    expect(record.children.run_child.lastQuery).toBe('Zweite Suche')
    expect(record.children.run_child.searchesDone).toBe(1)
  })

  it('stores only synthesis narration as the section line', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.child.progress', {
      child_run_id: 'run_child', task_id: 'call_1', run_status: 'running',
      event_type: 'inqtrix.agent.narration', kind: 'task', text: 'Modellprosa',
    }))
    expect(record.children.run_child.section).toBeUndefined()
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.child.progress', {
      child_run_id: 'run_child', task_id: 'call_1', run_status: 'running',
      event_type: 'inqtrix.agent.narration', narration_id: 'n-section-0', kind: 'synthesis', text: "Abschnitt 'Lage' geschrieben.",
    }))
    expect(record.children.run_child.section).toBe("Abschnitt 'Lage' geschrieben.")
  })
})

describe('phase headings fold when a segment re-enters the same phase', () => {
  const phases = (record: ReturnType<typeof agentRunFromSummary>) =>
    record.stepLog.filter((item) => item.kind === 'phase').map((item) => item.phase)

  it('appends one heading for execution, execution', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.phase.changed', { phase: 'execution' }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.phase.changed', { phase: 'execution' }))
    expect(phases(record)).toEqual(['execution'])
    expect(record.phase).toBe('execution')
  })

  it('keeps a genuine return to an earlier phase', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.phase.changed', { phase: 'execution' }))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.phase.changed', { phase: 'synthesis' }))
    record = applyAgentRunEvent(record, event(3, 'inqtrix.agent.phase.changed', { phase: 'execution' }))
    expect(phases(record)).toEqual(['execution', 'synthesis', 'execution'])
  })
})

describe('tool rows remember the todo they served and when they started', () => {
  it('stamps todoIndex and startedAt, and keeps startedAt across the settle', () => {
    let record = agentRunFromSummary(summary())
    record = applyAgentRunEvent(record, event(1, 'inqtrix.agent.tool.started', {
      tool: 'run_deep_mission', invocation_id: 'inv_1', args_preview: 'Memo', todo_index: 1,
    }))
    const row = record.stepLog.find((item) => item.activityKey === 'tool:inv_1')
    expect(row?.todoIndex).toBe(1)
    expect(row?.startedAt).toBe(1_700_000_101)
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.tool.finished', {
      tool: 'run_deep_mission', invocation_id: 'inv_1', status: 'success',
    }))
    const settled = record.stepLog.find((item) => item.activityKey === 'tool:inv_1')
    expect(settled?.status).toBe('completed')
    expect(settled?.startedAt).toBe(1_700_000_101)
    expect(settled?.at).toBe(1_700_000_102)
    expect(settled?.todoIndex).toBe(1)
  })

  it('counts written sections and remembers the first child projection', () => {
    let record = agentRunFromSummary(summary())
    const progress = (sequence: number, data: Record<string, unknown>) =>
      event(sequence, 'inqtrix.agent.child.progress', {
        child_run_id: 'run_child', task_id: 'call_1', run_status: 'running', ...data,
      })
    record = applyAgentRunEvent(record, progress(1, { event_type: 'inqtrix.agent.phase.changed' }))
    expect(record.children.run_child.startedAt).toBe(1_700_000_101)
    record = applyAgentRunEvent(record, progress(2, {
      event_type: 'inqtrix.agent.narration', narration_id: 'n-synthesis', kind: 'synthesis', text: 'Memo mit 3 Abschnitten.',
    }))
    record = applyAgentRunEvent(record, progress(3, {
      event_type: 'inqtrix.agent.narration', narration_id: 'n-section-0', kind: 'synthesis', text: 'Abschnitt A geschrieben.',
    }))
    record = applyAgentRunEvent(record, progress(4, {
      event_type: 'inqtrix.agent.narration', narration_id: 'n-section-1', kind: 'synthesis', text: 'Abschnitt B geschrieben.',
    }))
    expect(record.children.run_child.sectionsDone).toBe(2)
    expect(record.children.run_child.startedAt).toBe(1_700_000_101)
  })
})

describe('child mission projections for the card', () => {
  it('joins a task\'s searches and result line to its row and keeps the latest decision', () => {
    let record = agentRunFromSummary(summary())
    const child = (sequence: number, data: Record<string, unknown>) =>
      event(sequence, 'inqtrix.agent.child.progress', {
        child_run_id: 'run_child_1', task_id: 'call_1', run_status: 'running', ...data,
      })
    record = applyAgentRunEvent(record, child(1, { event_type: 'inqtrix.agent.task.started', ordinal: 0, title: 'Regulierung erfassen', child_task_id: 't1' }))
    record = applyAgentRunEvent(record, child(2, { event_type: 'inqtrix.agent.activity', operation: 'web.search.instant', status: 'started', query: 'EU AI Act Leitlinien', child_task_id: 't1' }))
    record = applyAgentRunEvent(record, child(3, { event_type: 'inqtrix.agent.activity', operation: 'web.search.instant', status: 'completed', query: 'EU AI Act Leitlinien', child_task_id: 't1' }))
    record = applyAgentRunEvent(record, child(4, { event_type: 'inqtrix.agent.narration', narration_id: 'n-task-t1', kind: 'task', text: 'Regulierung erfassen: EU-Leitlinien vom 02.09. belegt.' }))
    record = applyAgentRunEvent(record, child(5, { event_type: 'inqtrix.agent.task.finished', ordinal: 0, status: 'completed', child_task_id: 't1' }))
    record = applyAgentRunEvent(record, child(6, { event_type: 'inqtrix.agent.narration', narration_id: 'n-plan', kind: 'plan', text: 'Mein Plan (2 Aufgaben): Regulierung und Markt.' }))
    record = applyAgentRunEvent(record, child(7, { event_type: 'inqtrix.agent.activity', kind: 'critic_research', label: 'Critic fordert Recherche', detail: 'Das Memo braucht weitere Evidenz zu: Gegenposition belegt.' }))
    const projected = record.children.run_child_1
    expect(projected?.taskIds).toEqual({ t1: 0 })
    expect(projected?.taskStartedAt).toEqual({ 0: 1_700_000_101 })
    expect(projected?.taskSettledAt).toEqual({ 0: 1_700_000_105 })
    expect(projected?.taskSearches).toEqual({ 0: 1 })
    expect(projected?.lastQueryTask).toBe(0)
    expect(projected?.lastQuery).toBe('EU AI Act Leitlinien')
    expect(projected?.taskResults).toEqual({ 0: 'EU-Leitlinien vom 02.09. belegt.' })
    expect(projected?.planLine).toBe('Mein Plan (2 Aufgaben): Regulierung und Markt.')
    expect(projected?.decision).toEqual({
      kind: 'critic_research',
      label: 'Critic fordert Recherche',
      detail: 'Das Memo braucht weitere Evidenz zu: Gegenposition belegt.',
      at: 1_700_000_107,
    })
    expect(projected?.researchRounds).toBe(1)
    // A result line for an unknown task id and a non-decision kind change nothing.
    record = applyAgentRunEvent(record, child(8, { event_type: 'inqtrix.agent.narration', narration_id: 'n-task-t9', kind: 'task', text: 'Fremd: x' }))
    record = applyAgentRunEvent(record, child(9, { event_type: 'inqtrix.agent.activity', kind: 'memory', label: 'Memory' }))
    expect(record.children.run_child_1?.taskResults).toEqual({ 0: 'EU-Leitlinien vom 02.09. belegt.' })
    expect(record.children.run_child_1?.decision?.kind).toBe('critic_research')
    // The tool kind per task lets the card leave the search count off a
    // task that delegates to a grandchild (its searches never cross).
    record = applyAgentRunEvent(record, child(10, { event_type: 'inqtrix.agent.task.started', ordinal: 1, title: 'Markt', child_task_id: 't2', tool_kind: 'web_research' }))
    expect(record.children.run_child_1?.taskKinds).toEqual({ 1: 'web_research' })
    expect(record.children.run_child_1?.taskIds).toEqual({ t1: 0, t2: 1 })
    record = applyAgentRunEvent(record, child(11, { event_type: 'inqtrix.agent.activity', kind: 'critic_unavailable', label: 'Kontrolle nicht auswertbar', detail: 'Kein gültiges Prüfurteil.' }))
    expect(record.children.run_child_1?.decision?.kind).toBe('critic_unavailable')
    expect(record.children.run_child_1?.decision?.label).toBe('Kontrolle nicht auswertbar')
    expect(record.children.run_child_1?.researchRounds).toBe(1)
  })
})

describe('the outcome record', () => {
  const record = (sequence: number, data: Record<string, unknown>) => event(sequence, 'inqtrix.agent.outcome', {
    outcome_id: 'run_agent_1:outcome', final: 'completed', review: 'completed', round: 1,
    revision: 'none', accounting: 'complete', applied: 0, codes: [], limits: [],
    open: { by_code: {}, by_kind: {} }, lines: [], summary: 'Geprüft · ohne Befund', ...data,
  })

  it('keeps the latest outcome record per run', () => {
    // A redelivered segment repeats its record; the later one is the one
    // the run ended with.
    let run = agentRunFromSummary(summary())
    run = applyAgentRunEvent(run, record(1, { summary: 'Vor der Prüfung beendet', review: 'not_run' }))
    run = applyAgentRunEvent(run, record(2, { summary: 'Geprüft · 1 Punkt nicht geprüft' }))
    expect(run.outcome?.summary).toBe('Geprüft · 1 Punkt nicht geprüft')
  })

  it('keeps the outcome out of the step log', () => {
    // The step log is bounded; the record is run state the recap must
    // never lose to that bound.
    let run = agentRunFromSummary(summary())
    run = applyAgentRunEvent(run, event(1, 'inqtrix.agent.phase.changed', { phase: 'critic' }))
    const steps = run.stepLog.length
    run = applyAgentRunEvent(run, record(2, {}))
    expect([run.stepLog.length, run.outcome?.summary]).toEqual([steps, 'Geprüft · ohne Befund'])
  })

  it('does not read a record without its summary', () => {
    let run = agentRunFromSummary(summary())
    run = applyAgentRunEvent(run, record(1, { summary: undefined }))
    expect(run.outcome).toBeUndefined()
  })

  it('reads the lines as whole lines only', () => {
    let run = agentRunFromSummary(summary())
    run = applyAgentRunEvent(run, record(1, {
      lines: [
        { kind: 'not_checked', text: 'Quelle A nicht gelesen.', reasons: [] },
        { kind: 'limit', text: 'Lesegrenze erreicht.', reasons: ['extraction_limit', 7] },
        { kind: 'finding', text: '' , reasons: [] },
        'kein Objekt',
      ],
    }))
    expect(run.outcome?.lines).toEqual([
      { kind: 'not_checked', text: 'Quelle A nicht gelesen.', reasons: [] },
      { kind: 'limit', text: 'Lesegrenze erreicht.', reasons: ['extraction_limit'] },
    ])
  })

  it('reads the details and every whole mark', () => {
    let run = agentRunFromSummary(summary())
    run = applyAgentRunEvent(run, record(1, {
      summary: 'Geprüft · 2 Aussagen: 1 vom Anbieter zugeschrieben, 1 nicht ausreichend belegt',
      details: 'ohne Befund · 0 von 2 zitierten Belegen nachgelesen',
      marks: [
        { output_id: 'chat', quote: 'Der Bonus beträgt 16 Prozent [W1].', state: 'insufficient',
          reason: 'finding', note: 'Nicht ausreichend belegt: W1 nennt keinen Prozentsatz.' },
        { output_id: 'chat', quote: '', state: 'insufficient', reason: 'uncited', note: 'x' },
        { output_id: 'chat', quote: 'Ohne Grund.', state: 'insufficient', reason: 'uncited' },
        'kein Objekt',
      ],
    }))
    expect([run.outcome?.details, run.outcome?.marks]).toEqual([
      'ohne Befund · 0 von 2 zitierten Belegen nachgelesen',
      [{
        outputId: 'chat', quote: 'Der Bonus beträgt 16 Prozent [W1].', state: 'insufficient',
        note: 'Nicht ausreichend belegt: W1 nennt keinen Prozentsatz.',
      }],
    ])
  })

  it('reads a record without details or marks as having none', () => {
    let run = agentRunFromSummary(summary())
    run = applyAgentRunEvent(run, record(1, {}))
    expect([run.outcome?.details, run.outcome?.marks]).toEqual([undefined, []])
  })

  it("keeps a delegated mission's projected record on its child", () => {
    let run = agentRunFromSummary(summary())
    const child = (sequence: number, data: Record<string, unknown>) =>
      event(sequence, 'inqtrix.agent.child.progress', {
        child_run_id: 'run_child_1', task_id: 'call_1', run_status: 'running', ...data,
      })
    run = applyAgentRunEvent(run, child(1, { event_type: 'inqtrix.agent.outcome', final: 'completed', review: 'completed', applied: 2, summary: 'Geprüft · 2 Befunde überarbeitet, nicht nachgeprüft' }))
    run = applyAgentRunEvent(run, child(2, { event_type: 'inqtrix.agent.phase.changed', snapshot: { phase: 'done' } }))
    expect(run.children.run_child_1?.outcome).toEqual({
      final: 'completed', review: 'completed', applied: 2,
      summary: 'Geprüft · 2 Befunde überarbeitet, nicht nachgeprüft',
    })
  })
})

describe('run clock inputs', () => {
  it('records the server time of the newest event, a child\'s included', () => {
    let record = agentRunFromSummary(summary({ mode: 'agent_kernel' }))
    record = applyAgentRunEvent(record, event(1, 'inqtrix.run.started'))
    record = applyAgentRunEvent(record, event(2, 'inqtrix.agent.child.progress', {
      child_run_id: 'run_child', task_id: 'call_a', run_status: 'running',
    }))
    expect(record.lastProgressAt).toBe(1_700_000_102)
    // A replayed frame is a no-op and moves nothing back.
    expect(applyAgentRunEvent(record, event(1, 'inqtrix.run.started')).lastProgressAt)
      .toBe(1_700_000_102)
  })

  it('measures the clock offset from live arrivals only, keeping the smallest', () => {
    let record = agentRunFromSummary(summary({ mode: 'agent_kernel' }))
    // History replay carries no arrival time: no offset from it.
    record = applyAgentRunEvent(record, event(1, 'inqtrix.run.started'), { arrivedLive: false })
    expect(record.clockOffsetSeconds).toBeUndefined()
    // A client clock 30 s ahead; the second frame arrived faster.
    record = applyAgentRunEvent(record, event(2, 'inqtrix.run.resumed'), {
      arrivedLive: true, receivedAt: 1_700_000_102 + 31.5,
    })
    record = applyAgentRunEvent(record, event(3, 'inqtrix.run.resumed'), {
      arrivedLive: true, receivedAt: 1_700_000_103 + 30.2,
    })
    expect(record.clockOffsetSeconds).toBeCloseTo(30.2)
  })
})

describe('the answer draft', () => {
  const draft = (markdown = 'Erster Stand [W1].') => event(1, 'inqtrix.answer.draft', {
    artifact_id: 'art-answer',
    draft_id: 'drf-answer',
    markdown,
    reference_labels: ['W1'],
  })

  it('keeps the draft on the run, not as an answer artifact', () => {
    // The artifact list reconcile replaces artifact rows from the server;
    // a draft held there would be discarded before the review ends.
    const record = applyAgentRunEvent(agentRunFromSummary(summary()), draft())
    expect([record.answerDraft, record.artifactOrder]).toEqual([
      {
        artifactId: 'art-answer',
        createdAt: 1_700_000_101,
        draftId: 'drf-answer',
        markdown: 'Erster Stand [W1].',
        referenceLabels: ['W1'],
      },
      [],
    ])
  })

  it('keeps the draft while the reviewed answer starts, streams and is ready', () => {
    let record = applyAgentRunEvent(agentRunFromSummary(summary()), draft())
    record = applyAgentRunEvent(record, event(2, 'inqtrix.answer.started', {
      artifact_id: 'art-answer', publication_id: 'pub-answer', status: 'writing',
    }))
    record = applyAgentRunEvent(record, event(3, 'inqtrix.output_text.delta', {
      artifact_id: 'art-answer', publication_id: 'pub-answer', offset: 0,
      delta: '## Lage\n\nErster Stand [W1].',
    }))
    record = applyAgentRunEvent(record, event(4, 'inqtrix.answer.ready', {
      artifact_id: 'art-answer', publication_id: 'pub-answer', bytes: 28, status: 'ready',
    }))
    expect([
      record.answerDraft?.markdown,
      record.artifacts['art-answer']?.contentMarkdown,
    ]).toEqual(['Erster Stand [W1].', '## Lage\n\nErster Stand [W1].'])
  })

  it('takes a repeated draft of the run as the newer one', () => {
    let record = applyAgentRunEvent(agentRunFromSummary(summary()), draft())
    record = applyAgentRunEvent(record, { ...draft('Neuer Stand [W1].'), sequence: 2 })
    expect(record.answerDraft?.markdown).toBe('Neuer Stand [W1].')
  })

  it('ignores a draft without its text', () => {
    const record = applyAgentRunEvent(agentRunFromSummary(summary()), draft(''))
    expect(record.answerDraft).toBeUndefined()
  })
})
