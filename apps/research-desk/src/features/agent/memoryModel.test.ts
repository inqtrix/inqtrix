import { describe, expect, it } from 'vitest'

import {
  agentMemoryErrorMessage,
  agentMemoryModeLabel,
  mergeAgentMemoryState,
  pendingAgentMemoryCandidates,
  unavailableAgentMemoryState,
  visibleAgentFeedback,
} from './memoryModel'
import type {
  AgentFeedbackWire,
  AgentMemoryCandidateWire,
  AgentMemoryStatus,
  AgentMemoryWire,
} from '@/api/inqtrixClient'

describe('agent memory model', () => {
  it('shows effective candidate-only mode when auto-safe is degraded', () => {
    const status: AgentMemoryStatus = {
      available: true,
      degraded_reason: 'auto_safe_not_implemented',
      durable: true,
      effective_mode: 'candidate_only',
      mode: 'auto_safe',
      principal_eligible: true,
      provider: 'mem0',
    }

    expect(agentMemoryModeLabel(status)).toBe('mem0 · auto_safe -> candidate_only')
  })

  it('keeps only pending candidates for the inbox', () => {
    const candidates = [
      candidate('a', 'pending'),
      candidate('b', 'accepted'),
      candidate('c', 'rejected'),
    ]

    expect(pendingAgentMemoryCandidates(candidates).map((item) => item.id)).toEqual(['a'])
  })

  it('bounds feedback history without reordering server rows', () => {
    const rows = [
      feedback('f1', 'positive'),
      feedback('f2', 'negative'),
      feedback('f3', 'neutral'),
    ]

    expect(visibleAgentFeedback(rows, 2).map((item) => item.id)).toEqual(['f1', 'f2'])
  })

  it('merges the three list responses and prefers the memory-list status', () => {
    const status: AgentMemoryStatus = {
      available: true,
      durable: true,
      effective_mode: 'candidate_only',
      mode: 'candidate_only',
      principal_eligible: true,
      provider: 'mem0',
    }
    const merged = mergeAgentMemoryState(
      {
        candidates: { data: [candidate('a', 'pending')], status: null },
        feedback: { data: [feedback('f1', 'positive')] },
        memories: { data: [memory('m1')], status },
      },
      'plans',
    )

    expect(merged.status).toBe(status)
    expect(merged.memories.map((item) => item.id)).toEqual(['m1'])
    expect(merged.candidates.map((item) => item.id)).toEqual(['a'])
    expect(merged.feedback.map((item) => item.id)).toEqual(['f1'])
    expect(merged.searchQuery).toBe('plans')
    expect(merged.error).toBeNull()
  })

  it('synthesizes an unavailable status on the 404 fallback but keeps feedback', () => {
    const resolved = unavailableAgentMemoryState(
      [feedback('f1', 'neutral')],
      'plans',
    )

    expect(resolved.status).toMatchObject({
      available: false,
      mode: 'off',
      principal_eligible: false,
      provider: 'none',
    })
    expect(resolved.memories).toEqual([])
    expect(resolved.candidates).toEqual([])
    expect(resolved.feedback.map((item) => item.id)).toEqual(['f1'])
    expect(resolved.error).toBeNull()
  })

  it('maps thrown values to a user-facing message', () => {
    expect(agentMemoryErrorMessage(new Error('boom'))).toBe('boom')
    expect(agentMemoryErrorMessage('nope')).toBe('Memory unavailable')
  })
})

function candidate(
  id: string,
  status: AgentMemoryCandidateWire['status'],
): AgentMemoryCandidateWire {
  return {
    category: 'strategy',
    confidence: 0.8,
    content: `content ${id}`,
    created_at: 1,
    id,
    memory_id: '',
    reason: 'reason',
    scope: 'user',
    source_run_id: 'run_1',
    status,
    updated_at: 1,
  }
}

function memory(id: string): AgentMemoryWire {
  return {
    category: 'preference',
    confidence: 0.8,
    content: `content ${id}`,
    created_at: '2026-07-03T00:00:00Z',
    id,
    metadata: {},
    scope: 'user',
    source_run_id: 'run_1',
    updated_at: '2026-07-03T00:00:00Z',
  }
}

function feedback(
  id: string,
  value: AgentFeedbackWire['feedback'],
): AgentFeedbackWire {
  return {
    created_at: 1,
    feedback: value,
    id,
    memory_id: '',
    reason: '',
    run_id: `run_${id}`,
  }
}
