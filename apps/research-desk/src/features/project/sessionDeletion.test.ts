import { describe, expect, it } from 'vitest'

import type { ServerDeletionOperation } from '@/api/inqtrixClient'
import {
  assertSessionDeletionOperation,
  nextDeletionPollDelayMs,
  SessionDeletionContractError,
  sessionDeletionFromWire,
} from './sessionDeletion'

function receipt(
  overrides: Partial<ServerDeletionOperation> = {},
): ServerDeletionOperation {
  return {
    attempt: 1,
    asset_ids: [],
    completed_items: 0,
    created_at: 1,
    error: null,
    finished_at: null,
    operation_id: 'del_1',
    retryable: false,
    stage: 'queued',
    started_at: null,
    status: 'queued',
    target_id: 'as_1',
    target_kind: 'agent_session',
    total_items: 2,
    ...overrides,
  }
}

describe('session deletion projection', () => {
  it('accepts only a receipt for the exact requested session and desk', () => {
    expect(() => assertSessionDeletionOperation(
      receipt(),
      'agent_session',
      'as_1',
    )).not.toThrow()

    expect(() => assertSessionDeletionOperation(
      receipt({ target_id: 'as_other' }),
      'agent_session',
      'as_1',
    )).toThrow(SessionDeletionContractError)
    expect(() => assertSessionDeletionOperation(
      receipt({ target_kind: 'knowledge_session' }),
      'agent_session',
      'as_1',
    )).toThrow(SessionDeletionContractError)
  })

  it('restores a durable failed tombstone from session metadata', () => {
    expect(sessionDeletionFromWire({
      deletion_error: 'dependency unavailable',
      deletion_operation_id: 'del_1',
      deletion_stage: 'delete_failed',
      lifecycle_status: 'delete_failed',
    })).toEqual({
      error: 'dependency unavailable',
      operationId: 'del_1',
      stage: 'delete_failed',
      status: 'delete_failed',
    })
  })
})

describe('deletion receipt poll cadence', () => {
  it('keeps the first seconds fast so a normal deletion stays snappy', () => {
    // Perceived deletion time is the server's work PLUS this wait, so the
    // window that covers an ordinary deletion must not back off at all.
    const fast = Array.from({ length: 10 }, (_unused, index) =>
      nextDeletionPollDelayMs(index))
    expect(fast).toEqual(Array.from({ length: 10 }, () => 300))

    const elapsedMs = fast.reduce((total, delay) => total + delay, 0)
    expect(elapsedMs).toBeGreaterThanOrEqual(3_000)
  })

  it('calms down only once an operation misses that window, and caps', () => {
    expect(nextDeletionPollDelayMs(10)).toBe(600)
    expect(nextDeletionPollDelayMs(11)).toBe(1_200)
    expect(nextDeletionPollDelayMs(12)).toBe(2_400)
    expect(nextDeletionPollDelayMs(13)).toBe(4_800)
    expect(nextDeletionPollDelayMs(14)).toBe(5_000)
    expect(nextDeletionPollDelayMs(500)).toBe(5_000)
  })

  it('stays well under a hundred polls across a full dispatch timeout', () => {
    let elapsedMs = 0
    let polls = 0
    while (elapsedMs < 240_000) {
      elapsedMs += nextDeletionPollDelayMs(polls)
      polls += 1
    }
    expect(polls).toBeLessThan(100)
  })
})
