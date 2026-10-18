import { describe, expect, it } from 'vitest'
import { translations } from '@/i18n/translations'
import type { KnowledgeRunStepRecord } from '@/features/project/types'
import { knowledgeStepLine } from './stepLines'

const t = translations.de.knowledge

function line(step: KnowledgeRunStepRecord, collectionCount = 1) {
  return knowledgeStepLine(step, { collectionCount, t })
}

describe('knowledgeStepLine', () => {
  it('renders follow-up contextualization outcomes', () => {
    expect(line({
      facts: { contextMarker: '_knowledge_query_context_applied', rewritten: true },
      id: 'context',
      kind: 'context',
      status: 'done',
    })).toMatchObject({
      primary: 'Nachfrage mit Verlauf geklärt',
    })
    expect(line({
      facts: { contextMarker: '_knowledge_query_context_fallback', rewritten: false },
      id: 'context',
      kind: 'context',
      status: 'done',
    })).toMatchObject({
      primary: 'Verlauf geprüft; Frage bleibt eigenständig',
      secondary: 'Originalfrage genutzt',
    })
  })

  it('renders the deep RAG live ledger lines used by the demo twin', () => {
    const steps: KnowledgeRunStepRecord[] = [
      {
        facts: { autoSelected: false, degradedStages: ['rerank'], profile: 'tief' },
        id: 'profile',
        kind: 'profile',
        status: 'done',
      },
      { facts: {}, id: 'vocabulary', kind: 'vocabulary', status: 'done' },
      {
        facts: { candidateCount: 8, topK: 8 },
        id: 'retrieval',
        kind: 'retrieval',
        status: 'done',
      },
      {
        facts: { subQueryCount: 0 },
        id: 'decompose',
        kind: 'decompose',
        status: 'done',
      },
      {
        facts: { rewritten: true, round: 4, roundsTotal: 4, sufficient: false },
        id: 'gate-3',
        kind: 'gate',
        status: 'done',
      },
      { facts: {}, id: 'answer', kind: 'answer', status: 'running' },
    ]

    expect(line(steps[0])).toMatchObject({
      primary: 'Profil: Tief',
      secondary: 'Reduzierte Stufen: rerank',
    })
    expect(line(steps[1]).primary).toBe('Formuliere fachsprachlich um')
    expect(line(steps[2]).primary).toBe('Durchsuche 1 Sammlung… (8 Treffer)')
    expect(line(steps[3]).primary).toBe('Zerlege Frage in 0 Teilfragen')
    expect(line(steps[4])).toMatchObject({
      primary: 'Bewerte Evidenz (Runde 4/4)',
      secondary: 'nicht ausreichend · Suchanfrage umformuliert',
    })
    expect(line(steps[5]).primary).toBe('Formuliere Antwort…')
  })

  it('surfaces the searched document count when known (coverage signal)', () => {
    const retrieval: KnowledgeRunStepRecord = {
      facts: { candidateCount: 16, collectionDocumentCount: 11, topK: 8 },
      id: 'retrieval',
      kind: 'retrieval',
      status: 'done',
    }
    expect(line(retrieval).primary).toBe('Durchsuche 11 Dokumente… (16 Treffer)')
  })

  it('falls back to the collection line when the document count is unknown', () => {
    const retrieval: KnowledgeRunStepRecord = {
      facts: { candidateCount: 16, topK: 8 },
      id: 'retrieval',
      kind: 'retrieval',
      status: 'done',
    }
    expect(line(retrieval).primary).toBe('Durchsuche 1 Sammlung… (16 Treffer)')
  })

  it('surfaces top_k and final_k as a secondary line, flagging an override', () => {
    const base: KnowledgeRunStepRecord = {
      facts: { candidateCount: 16, topK: 8, finalK: 16 },
      id: 'retrieval',
      kind: 'retrieval',
      status: 'done',
    }
    expect(line(base).secondary).toBe('top_k 8 · final_k 16')
    expect(line({ ...base, facts: { ...base.facts, finalKOverridden: true } }).secondary)
      .toBe('top_k 8 · final_k 16 · überschrieben')
  })

  it.each([
    {
      candidateCap: 64,
      finalComplete: true,
      finalTopK: 3,
      reason: 'vector_overfetch_cap',
      returnedHits: 3,
      suffix: 'Die Prüfung endete bei 64 Kandidaten.',
    },
    {
      candidateCap: 64,
      finalComplete: false,
      finalTopK: 8,
      reason: 'vector_overfetch_cap',
      returnedHits: 3,
      suffix: 'Die Prüfung endete bei 64 Kandidaten.',
    },
    {
      candidateCap: null,
      finalComplete: true,
      finalTopK: 3,
      reason: 'vector_candidate_stalled',
      returnedHits: 3,
      suffix: 'Die nächste Kandidatenstufe lieferte keine neuen aktiven Kandidaten und wurde beendet.',
    },
    {
      candidateCap: null,
      finalComplete: false,
      finalTopK: 8,
      reason: 'vector_candidate_stalled',
      returnedHits: 3,
      suffix: 'Die nächste Kandidatenstufe lieferte keine neuen aktiven Kandidaten und wurde beendet.',
    },
  ])('renders $reason with final completeness $finalComplete truthfully', ({
    candidateCap,
    finalComplete,
    finalTopK,
    reason,
    returnedHits,
    suffix,
  }) => {
    const retrieval: KnowledgeRunStepRecord = {
      facts: {
        candidateCount: 3,
        retrievalDegradations: [{
          candidate_cap: candidateCap,
          final_evidence_complete: finalComplete,
          final_top_k: finalTopK,
          reason,
          requested_candidate_pool: 12,
          requested_top_k: finalTopK,
          retrieval_mode: 'hybrid',
          returned_candidate_pool: 6,
          returned_hits: returnedHits,
          stage: 'vector_candidate_pool',
        }],
      },
      id: 'retrieval',
      kind: 'retrieval',
      status: 'done',
    }

    expect(line(retrieval).warning).toBe([
      'Der Vektor-Kandidatenpool erreichte 6 von 12 geplanten Kandidaten.',
      finalComplete
        ? `Die ${finalTopK} final angeforderten verifizierten Belege sind dennoch vollständig verfügbar.`
        : `Final sind nur ${returnedHits} von ${finalTopK} angeforderten verifizierten Belegen verfügbar.`,
      suffix,
    ].join(' '))
  })

  it('renders source-integrity exclusions through the shared warning copy', () => {
    const retrieval: KnowledgeRunStepRecord = {
      facts: {
        candidateCount: 2,
        retrievalWarnings: [{
          code: 'chunks_require_reindex',
          count: 3,
          message: 'server fallback',
          reason: 'source_unverified',
          recommended_action: 'reindex',
          stage: 'canonical_hydration',
        }],
      },
      id: 'retrieval',
      kind: 'retrieval',
      status: 'done',
    }

    expect(line(retrieval).warning).toBe(
      '3 ältere Treffer wurden ausgeschlossen, weil ihr Originaltext nicht '
      + 'verifiziert werden konnte. Diese Inhalte müssen neu indiziert werden.',
    )
  })

  it('keeps duplicate consolidation informational in the retained retrieval step', () => {
    const retrieval: KnowledgeRunStepRecord = {
      facts: {
        candidateCount: 2,
        retrievalWarnings: [{
          code: 'duplicate_documents_collapsed',
          count: 1,
          message: '',
          reason: 'duplicate_document',
          recommended_action: null,
          stage: 'canonical_hydration',
        }],
      },
      id: 'retrieval',
      kind: 'retrieval',
      status: 'done',
    }

    expect(line(retrieval)).toMatchObject({
      information: '1 Treffer wurde übergangen, weil ein inhaltsgleiches '
        + 'Dokument schon unter den gefundenen Quellen war.',
      warning: undefined,
    })
  })

  it('names an unparseable grounding response instead of claiming zero checked quotes', () => {
    expect(line({
      facts: {
        groundingMarker: '_knowledge_grounding_fallback',
        quotesTotal: 0,
        quotesVerified: 0,
      },
      id: 'grounding',
      kind: 'grounding',
      status: 'done',
    }).primary).toBe('Zitatprüfung konnte nicht ausgewertet werden')
  })

  it('surfaces an unparseable evidence gate instead of presenting fallback as a normal verdict', () => {
    expect(line({
      facts: {
        gateMarker: '_knowledge_gate_fallback',
        rewritten: false,
        round: 1,
        roundsTotal: 3,
        sufficient: true,
      },
      id: 'gate-0',
      kind: 'gate',
      status: 'done',
    }).secondary).toBe(
      'ausreichend · Evidenzbewertung nicht auswertbar — Antwortpfad sichtbar degradiert',
    )
  })

  it('renders the visible answer regeneration step', () => {
    expect(line({
      facts: { quotesTotal: 10, quotesUnverified: 1 },
      id: 'answer-retry',
      kind: 'answer-retry',
      status: 'running',
    }).primary).toBe('Formuliere Antwort neu (1 Zitat nicht belegt)…')
    expect(line({
      facts: { quotesTotal: 10, quotesUnverified: 1 },
      id: 'answer-retry',
      kind: 'answer-retry',
      status: 'done',
    }).primary).toBe('Antwort neu formuliert (1 Zitat war nicht belegt)')
    expect(line({
      facts: { quotesTotal: 10, quotesUnverified: 3 },
      id: 'answer-retry',
      kind: 'answer-retry',
      status: 'done',
    }).primary).toBe('Antwort neu formuliert (3 Zitate waren nicht belegt)')
  })

  it('renders the queued step position-less in both states', () => {
    expect(line({ facts: {}, id: 'queued', kind: 'queued', status: 'running' }).primary)
      .toBe('In Warteschlange – wartet auf einen freien Slot…')
    expect(line({ facts: {}, id: 'queued', kind: 'queued', status: 'done' }).primary)
      .toBe('Warteschlange durchlaufen')
  })
})
