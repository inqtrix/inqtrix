import { describe, expect, it } from 'vitest'
import type { KnowledgeThreadItemRecord } from '@/features/project/types'
import {
  knowledgeCompletionHandoffId,
  knowledgeItemStatusSnapshot,
} from './completionHandoff'

function item(
  id: string,
  status: KnowledgeThreadItemRecord['status'],
  withAnswer = status === 'completed',
): KnowledgeThreadItemRecord {
  return {
    answer: withAnswer
      ? {
        answerMarkdown: 'Fertig.',
        degradedStages: [],
        retrievalDegradations: [],
        quotes: [],
        references: [],
        refusal: false,
      }
      : undefined,
    collectionTitles: ['EU-Recht'],
    createdAt: '2026-06-20T08:00:00.000Z',
    id,
    progress: { steps: [] },
    question: 'Frage?',
    requestedProfile: null,
    runId: `run-${id}`,
    sessionId: 'session-1',
    status,
  }
}

describe('knowledgeCompletionHandoffId', () => {
  it('detects only a fresh running-to-completed answer transition', () => {
    const previous = knowledgeItemStatusSnapshot([
      item('old-answer', 'completed'),
      item('fresh-answer', 'running', false),
    ])

    expect(knowledgeCompletionHandoffId({
      items: [
        item('old-answer', 'completed'),
        item('fresh-answer', 'completed'),
      ],
      previousStatuses: previous,
    })).toBe('fresh-answer')
  })

  it('does not animate already completed answers during initial hydration', () => {
    expect(knowledgeCompletionHandoffId({
      items: [item('old-answer', 'completed')],
      previousStatuses: new Map(),
    })).toBeNull()
  })

  it('ignores completed items until the answer payload is attached', () => {
    const previous = knowledgeItemStatusSnapshot([item('pending-answer', 'running', false)])

    expect(knowledgeCompletionHandoffId({
      items: [item('pending-answer', 'completed', false)],
      previousStatuses: previous,
    })).toBeNull()
  })
})
