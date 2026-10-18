import { describe, expect, it } from 'vitest'
import { fromRunSummary } from '@/features/project/types'
import type { ResearchRunSummary } from './types'
import { importPayloadFromRun } from './useResearchRunImport'

function summary(): ResearchRunSummary {
  return {
    access: { mode: 'owner' },
    agent_overrides: {},
    created_at: 1_767_225_600,
    elapsed_seconds: 1,
    error: null,
    events_url: '/v1/runs/external-run/events',
    finished_at: 1_767_225_601,
    mode: 'research',
    question: 'Imported question',
    queue_position: null,
    result_url: '/v1/runs/external-run/result',
    run_id: 'external-run',
    snapshot: {},
    stack: 'web',
    started_at: 1_767_225_600,
    status: 'completed',
  }
}

describe('importPayloadFromRun', () => {
  it('sends the project id only as source_run_id', () => {
    const payload = importPayloadFromRun(fromRunSummary(summary(), 'web'))

    expect(payload.source_run_id).toBe('external-run')
    expect(payload).not.toHaveProperty('run_id')
  })
})
