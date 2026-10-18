import { describe, expect, it } from 'vitest'

import { applyAgentRunEvent } from './events'
import { agentRunFromSummary, mergeAgentRunSummary } from './model'
import type { ResearchRunEvent, ResearchRunSummary } from '@/features/researchRuns/types'

function summary(overrides: Partial<ResearchRunSummary> = {}): ResearchRunSummary {
  return {
    access: { mode: 'owner' },
    run_id: 'run_agent_1',
    status: 'waiting_for_approval',
    queue_position: null,
    question: 'Erstelle eine Marktanalyse.',
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

describe('a summary never regresses a run the stream has moved past', () => {
  // The decision endpoints answer with a summary read INSIDE the
  // transaction that re-queues the run, so it says `queued` while the
  // worker has already picked the run up. The resumed row follows within
  // milliseconds, while the next lifecycle event can be minutes away — so
  // the regression is not a flicker, it is the whole wait.
  function resumedRun() {
    let run = agentRunFromSummary(summary())
    run = applyAgentRunEvent(run, event(15, 'inqtrix.run.queued', {
      queue_position: 1,
      resumed: true,
      status: 'queued',
    }))
    return applyAgentRunEvent(run, event(17, 'inqtrix.run.resumed', { status: 'running' }))
  }

  it('keeps the streamed status when the summary is behind', () => {
    const run = resumedRun()
    expect(run.status).toBe('running')

    const merged = mergeAgentRunSummary(run, summary({
      last_event_sequence: 15,
      queue_position: 1,
      status: 'queued',
    }))

    expect(merged.status).toBe('running')
    expect(merged.queuePosition).toBeNull()
  })

  it('takes the summary when it is AHEAD of the stream', () => {
    // The polling fallback and a reload both deliver the truth this way;
    // the guard must not turn a live record deaf to it.
    const merged = mergeAgentRunSummary(resumedRun(), summary({
      finished_at: 1_700_000_400,
      last_event_sequence: 42,
      status: 'completed',
    }))

    expect(merged.status).toBe('completed')
  })

  it('takes a summary from a server that sends no position', () => {
    // Older servers omit the key; without a position there is nothing to
    // compare, and the summary is the only source.
    const merged = mergeAgentRunSummary(resumedRun(), summary({ status: 'completed' }))

    expect(merged.status).toBe('completed')
  })

  it('still merges what the event stream does not carry', () => {
    const merged = mergeAgentRunSummary(resumedRun(), summary({
      last_event_sequence: 15,
      session_id: 'ses_1',
      status: 'queued',
    }))

    expect(merged.sessionId).toBe('ses_1')
    expect(merged.status).toBe('running')
  })
})

describe('the checklist stops claiming a step is running when the run ends', () => {
  // `write_todos` costs the model a turn, and the last entry is the one
  // it finishes BY answering — there is no turn left after that to tick
  // it. Otherwise a completed run's box reads "2/3 erledigt" with a live
  // dot beside a step that is over.
  function runWithOpenTodo() {
    return applyAgentRunEvent(
      agentRunFromSummary(summary({ status: 'running' })),
      event(80, 'inqtrix.agent.todo.updated', {
        todos: [
          { content: 'Evidenz auswerten', status: 'completed' },
          { content: 'Ergebnisse strukturieren', status: 'completed' },
          { content: 'Einschaetzung geben', status: 'in_progress' },
        ],
      }),
    )
  }

  it('ticks the entry the model was on when the run completed', () => {
    const done = applyAgentRunEvent(
      runWithOpenTodo(),
      event(90, 'inqtrix.run.completed', {}),
    )

    expect(done.todos?.map((todo) => todo.status))
      .toEqual(['completed', 'completed', 'completed'])
  })

  it('leaves a cancelled run saying where it stopped', () => {
    const stopped = applyAgentRunEvent(
      runWithOpenTodo(),
      event(90, 'inqtrix.run.cancelled', {}),
    )

    expect(stopped.todos?.[2]?.status).toBe('in_progress')
  })

  it('never ticks a step the model had not started', () => {
    const run = applyAgentRunEvent(
      agentRunFromSummary(summary({ status: 'running' })),
      event(80, 'inqtrix.agent.todo.updated', {
        todos: [
          { content: 'Evidenz auswerten', status: 'in_progress' },
          { content: 'Einschaetzung geben', status: 'pending' },
        ],
      }),
    )

    const done = applyAgentRunEvent(run, event(90, 'inqtrix.run.completed', {}))

    expect(done.todos?.map((todo) => todo.status))
      .toEqual(['completed', 'pending'])
  })
})
