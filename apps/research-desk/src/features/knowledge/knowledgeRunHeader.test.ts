import { describe, expect, it } from 'vitest'
import { translations } from '@/i18n/translations'
import type {
  KnowledgeRunStepRecord,
  KnowledgeThreadItemRecord,
} from '@/features/project/types'
import {
  KNOWLEDGE_RUN_FACT_PLACEHOLDER,
  knowledgeRunFacts,
  knowledgeRunHeaderStatus,
} from './knowledgeRunHeader'

const t = translations.de.knowledge

function item(
  steps: KnowledgeRunStepRecord[],
  requestedProfile: string | null = 'tief',
): KnowledgeThreadItemRecord {
  return {
    collectionTitles: ['EU-Recht'],
    createdAt: '2026-06-20T00:00:00.000Z',
    id: 'knowledge-item-1',
    progress: { steps },
    question: 'Wann gilt ein KI-System als Hochrisiko-System?',
    requestedProfile,
    runId: 'demo-run-1',
    sessionId: 'knowledge-session-1',
    status: 'running',
  }
}

describe('knowledgeRunFacts', () => {
  it('keeps the same four fact slots before retrieval and gate facts exist', () => {
    expect(knowledgeRunFacts({
      collectionCount: 1,
      item: item([
        {
          facts: { profile: 'tief' },
          id: 'profile',
          kind: 'profile',
          status: 'running',
        },
      ]),
      t,
    })).toEqual([
      { id: 'profile', label: 'Profil', pending: false, value: 'Tief' },
      { id: 'collections', label: 'Sammlungen', pending: false, value: '1' },
      { id: 'hits', label: 'Treffer', pending: true, value: KNOWLEDGE_RUN_FACT_PLACEHOLDER },
      { id: 'round', label: 'Runde', pending: true, value: KNOWLEDGE_RUN_FACT_PLACEHOLDER },
    ])
  })

  it('updates fact slot values without changing their order', () => {
    const facts = knowledgeRunFacts({
      collectionCount: 1,
      item: item([
        {
          facts: { candidateCount: 8 },
          id: 'retrieval',
          kind: 'retrieval',
          status: 'done',
        },
        {
          facts: { rewritten: true, round: 3, roundsTotal: 4, sufficient: false },
          id: 'gate-2',
          kind: 'gate',
          status: 'running',
        },
      ]),
      t,
    })

    expect(facts.map((fact) => fact.id)).toEqual(['profile', 'collections', 'hits', 'round'])
    expect(facts.map((fact) => fact.value)).toEqual(['Tief', '1', '8', '3/4'])
    expect(facts.map((fact) => fact.pending)).toEqual([false, false, false, false])
  })
})

describe('knowledgeRunHeaderStatus', () => {
  it('uses only the primary step line as visible header status', () => {
    const status = knowledgeRunHeaderStatus({
      collectionCount: 1,
      fallback: t.runPreparing,
      step: {
        facts: { rewritten: true, round: 3, roundsTotal: 4, sufficient: false },
        id: 'gate-2',
        kind: 'gate',
        status: 'running',
      },
      t,
    })

    expect(status.value).toBe('Bewerte Evidenz (Runde 3/4)')
    expect(status.value).not.toContain('nicht ausreichend')
    expect(status.title).toBe('Bewerte Evidenz (Runde 3/4) · nicht ausreichend · Suchanfrage umformuliert')
  })

  it('lets a running queued step displace the preparing fallback', () => {
    const status = knowledgeRunHeaderStatus({
      collectionCount: 1,
      fallback: 'Initialisiere Retrieval',
      step: { facts: {}, id: 'queued', kind: 'queued', status: 'running' },
      t,
    })
    expect(status.value).toBe('In Warteschlange – wartet auf einen freien Slot…')
    expect(status.value).not.toBe('Initialisiere Retrieval')
  })
})
