import { describe, expect, it } from 'vitest'
import type { KnowledgeThreadItemRecord } from '@/features/project/types'
import { buildKnowledgeAskMessages } from './conversationContext'

function item(
  id: string,
  question: string,
  answerMarkdown: string | null,
  status: KnowledgeThreadItemRecord['status'] = 'completed',
): KnowledgeThreadItemRecord {
  return {
    answer: answerMarkdown
      ? {
        answerMarkdown,
        degradedStages: [],
        retrievalDegradations: [],
        quotes: [],
        references: [],
        refusal: false,
      }
      : undefined,
    collectionTitles: ['EU-Recht'],
    createdAt: `2026-01-01T00:00:0${id}.000Z`,
    id,
    progress: { steps: [] },
    question,
    requestedProfile: 'tief',
    runId: `run-${id}`,
    sessionId: 'ks-1',
    status,
  }
}

describe('buildKnowledgeAskMessages', () => {
  it('returns undefined for a first ask without completed history', () => {
    expect(buildKnowledgeAskMessages([], 'Was gilt?')).toBeUndefined()
    expect(buildKnowledgeAskMessages([
      item('1', 'Alt?', 'Antwort.', 'failed'),
      item('2', 'Neu?', null, 'running'),
    ], 'Was gilt?')).toBeUndefined()
  })

  it('builds recent Q&A history and strips old citation labels', () => {
    const messages = buildKnowledgeAskMessages([
      item('1', 'Was ist Art. 5?', 'Art. 5 verbietet X. [K1][K2]'),
    ], 'Und Art. 6?')

    expect(messages).toEqual([
      { content: 'Was ist Art. 5?', role: 'user' },
      { content: 'Art. 5 verbietet X.', role: 'assistant' },
      { content: 'Und Art. 6?', role: 'user' },
    ])
  })

  it('excludes the replaced item and later turns during in-place rerun', () => {
    const messages = buildKnowledgeAskMessages([
      item('1', 'Erste Frage?', 'Erste Antwort.'),
      item('2', 'Zu ersetzende Frage?', 'Alte Antwort.'),
      item('3', 'Spaetere Frage?', 'Spaetere Antwort.'),
    ], 'Neue Fassung?', { replaceItemId: '2' })

    expect(messages).toEqual([
      { content: 'Erste Frage?', role: 'user' },
      { content: 'Erste Antwort.', role: 'assistant' },
      { content: 'Neue Fassung?', role: 'user' },
    ])
  })

  it('caps context to the last six completed turns', () => {
    const messages = buildKnowledgeAskMessages(
      Array.from({ length: 8 }, (_, index) =>
        item(String(index + 1), `Frage ${index + 1}?`, `Antwort ${index + 1}.`)),
      'Nachfrage?',
    )

    expect(messages?.[0]).toEqual({ content: 'Frage 3?', role: 'user' })
    expect(messages).toHaveLength(13)
    expect(messages?.at(-1)).toEqual({ content: 'Nachfrage?', role: 'user' })
  })
})
