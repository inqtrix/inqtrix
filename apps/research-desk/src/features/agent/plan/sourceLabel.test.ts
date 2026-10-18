import { describe, expect, it } from 'vitest'

import {
  planTaskSourceLabel,
  vectorBackendDisplay,
  type PlanSourceInfo,
} from './sourceLabel'

const LABELS = {
  allCollections: 'alle ausgewaehlten Sammlungen',
  knowledgeIndex: 'Wissens-Index',
  recency: 'Aktualitaet',
  web: 'Web',
}

const INFO: PlanSourceInfo = {
  collections: [
    { id: 'kc_18d4', title: 'EU-AI-Act' },
    { id: 'kc_beta', title: 'Marktdaten' },
  ],
  vectorBackendLabel: 'Qdrant',
}

describe('planTaskSourceLabel', () => {
  it('maps rag collection ids to titles with the backend label', () => {
    expect(
      planTaskSourceLabel(
        {
          toolKind: 'rag_query',
          params: { collection_ids: ['kc_18d4', 'kc_beta'] },
        },
        INFO,
        LABELS,
      ),
    ).toBe(
      'Wissens-Index (Qdrant) → EU-AI-Act (kc_18d4), Marktdaten (kc_beta)',
    )
  })

  it('keeps an unknown id visible instead of prettifying it', () => {
    expect(
      planTaskSourceLabel(
        { toolKind: 'rag_query', params: { collection_ids: ['kc_ghost'] } },
        INFO,
        LABELS,
      ),
    ).toBe('Wissens-Index (Qdrant) → kc_ghost')
  })

  it('states the inherited run scope when no ids narrow the task', () => {
    expect(
      planTaskSourceLabel({ toolKind: 'rag_query', params: {} }, INFO, LABELS),
    ).toBe('Wissens-Index (Qdrant) → alle ausgewaehlten Sammlungen')
  })

  it('omits the backend parenthesis without a configured backend', () => {
    expect(
      planTaskSourceLabel(
        { toolKind: 'file_analysis', params: {} },
        { ...INFO, vectorBackendLabel: null },
        LABELS,
      ),
    ).toBe('Wissens-Index → alle ausgewaehlten Sammlungen')
  })

  it('labels web tasks with their recency hint', () => {
    expect(
      planTaskSourceLabel(
        { toolKind: 'web_research', params: { recency: '365d' } },
        INFO,
        LABELS,
      ),
    ).toBe('Web · Aktualitaet: 365d')
    expect(
      planTaskSourceLabel({ toolKind: 'web_instant', params: {} }, INFO, LABELS),
    ).toBe('Web')
  })

  it('returns null for synthesis (no retrieval source)', () => {
    expect(
      planTaskSourceLabel({ toolKind: 'synthesis', params: {} }, INFO, LABELS),
    ).toBeNull()
  })
})

describe('vectorBackendDisplay', () => {
  it('capitalizes the configured backend id and hides blanks', () => {
    expect(vectorBackendDisplay('qdrant')).toBe('Qdrant')
    expect(vectorBackendDisplay('  ')).toBeNull()
    expect(vectorBackendDisplay(undefined)).toBeNull()
  })
})
