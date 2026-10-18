import { describe, expect, it } from 'vitest'

import { translations } from '@/i18n/translations'

import {
  knowledgeRetrievalDegradations,
  knowledgeRetrievalWarnings,
  knowledgeSearchWarningText,
  mergeKnowledgeRetrievalDegradations,
  mergeKnowledgeRetrievalWarnings,
} from './retrievalDegradation'

const cases = [
  ['vector_overfetch_cap', true, 4, 4],
  ['vector_overfetch_cap', false, 8, 3],
  ['vector_candidate_stalled', true, 4, 4],
  ['vector_candidate_stalled', false, 8, 3],
] as const

describe('knowledge retrieval degradation parser', () => {
  it.each(cases)(
    'preserves %s with final completeness %s',
    (reason, finalEvidenceComplete, finalTopK, returnedHits) => {
      const raw = {
        candidate_cap: reason === 'vector_overfetch_cap' ? 64 : null,
        final_evidence_complete: finalEvidenceComplete,
        final_top_k: finalTopK,
        reason,
        requested_candidate_pool: 40,
        requested_top_k: finalTopK,
        retrieval_mode: 'hybrid',
        returned_candidate_pool: 7,
        returned_hits: returnedHits,
        stage: 'vector_candidate_pool',
      }

      expect(knowledgeRetrievalDegradations([raw])).toEqual([raw])
    },
  )

  it('normalizes legacy final counters without losing old persisted warnings', () => {
    expect(knowledgeRetrievalDegradations([{
      candidate_cap: 64,
      reason: 'vector_overfetch_cap',
      requested_top_k: 8,
      retrieval_mode: 'hybrid',
      returned_hits: 3,
    }])).toEqual([{
      candidate_cap: 64,
      final_evidence_complete: false,
      final_top_k: 8,
      reason: 'vector_overfetch_cap',
      requested_candidate_pool: 8,
      requested_top_k: 8,
      retrieval_mode: 'hybrid',
      returned_candidate_pool: 3,
      returned_hits: 3,
      stage: 'vector_candidate_pool',
    }])
  })

  it('deduplicates exact event/completion copies but retains distinct pool outcomes', () => {
    const complete = knowledgeRetrievalDegradations([{
      candidate_cap: 64,
      final_evidence_complete: true,
      final_top_k: 4,
      reason: 'vector_overfetch_cap',
      requested_candidate_pool: 40,
      requested_top_k: 4,
      retrieval_mode: 'hybrid',
      returned_candidate_pool: 8,
      returned_hits: 4,
      stage: 'vector_candidate_pool',
    }])[0]
    const shallowerPool = {
      ...complete,
      returned_candidate_pool: 7,
    }

    expect(mergeKnowledgeRetrievalDegradations(
      [complete],
      [complete, shallowerPool],
    )).toEqual([complete, shallowerPool])
  })
})

describe('knowledge retrieval warning parser', () => {
  const warning = {
    code: 'chunks_require_reindex',
    count: 2,
    message: 'server fallback',
    reason: 'source_unverified',
    recommended_action: 'reindex',
    stage: 'canonical_hydration',
  }

  it('preserves the bounded source-integrity warning without evidence text', () => {
    expect(knowledgeRetrievalWarnings([{
      ...warning,
      source_text: 'must not survive',
    }])).toEqual([warning])
  })

  it('merges cumulative event/result snapshots by typed identity', () => {
    expect(mergeKnowledgeRetrievalWarnings(
      knowledgeRetrievalWarnings([{ ...warning, count: 1 }]),
      knowledgeRetrievalWarnings([warning]),
    )).toEqual([warning])
  })
})

describe('knowledge search warning text', () => {
  const de = translations.de.knowledge
  const en = translations.en.knowledge

  it.each([
    ['chunks_require_reindex', 1, '1 älterer Treffer wurde ausgeschlossen, weil sein Originaltext nicht verifiziert werden konnte.'],
    ['chunks_require_reindex', 3, '3 ältere Treffer wurden ausgeschlossen, weil ihr Originaltext nicht verifiziert werden konnte.'],
    ['chunks_pending_reconciliation', 1, '1 Vektortreffer wurde ausgeschlossen, weil der zugehörige kanonische Datensatz'],
    ['chunks_pending_reconciliation', 2, '2 Vektortreffer wurden ausgeschlossen, weil der zugehörige kanonische Datensatz'],
    ['duplicate_documents_collapsed', 1, '1 Treffer wurde übergangen, weil ein inhaltsgleiches Dokument'],
    ['duplicate_documents_collapsed', 4, '4 Treffer wurden übergangen, weil inhaltsgleiche Dokumente'],
  ] as const)('counts %s in hits with the number the count takes (%i)', (code, count, text) => {
    // A reword of these dictionary lines must be caught here: the singular
    // is its own entry, never the plural with "1" filled in.
    expect(knowledgeSearchWarningText({ code, count, message: 'server message' }, de)).toContain(text)
  })

  it('reads an unknown code as one general localized line, never the server text or the code', () => {
    const warning = { code: 'future_code', count: 2, message: 'Results were excluded by a future check.' }
    expect(knowledgeSearchWarningText(warning, de)).toBe(de.retrievalDegradedUnknown)
    expect(knowledgeSearchWarningText(warning, en)).toBe(en.retrievalDegradedUnknown)
    expect(de.retrievalDegradedUnknown).not.toContain('future_code')
  })
})
