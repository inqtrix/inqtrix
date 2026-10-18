import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { LocaleProvider } from '@/i18n/LocaleProvider'
import { TooltipProvider } from '@/components/ui/tooltip'
import type { KnowledgeRetrievalDegradation } from '@/features/researchRuns/types'
import { AnswerCard } from './AnswerCard'

const degradationCases = [
  {
    degradation: {
      candidate_cap: 64,
      final_evidence_complete: true,
      final_top_k: 3,
      reason: 'vector_overfetch_cap',
      requested_candidate_pool: 12,
      requested_top_k: 3,
      retrieval_mode: 'hybrid',
      returned_candidate_pool: 6,
      returned_hits: 3,
      stage: 'vector_candidate_pool',
    },
    outcome: 'Die 3 final angeforderten verifizierten Belege sind dennoch vollständig verfügbar.',
    reasonDetail: 'Die Prüfung endete bei 64 Kandidaten.',
  },
  {
    degradation: {
      candidate_cap: 64,
      final_evidence_complete: false,
      final_top_k: 8,
      reason: 'vector_overfetch_cap',
      requested_candidate_pool: 12,
      requested_top_k: 8,
      retrieval_mode: 'hybrid',
      returned_candidate_pool: 6,
      returned_hits: 3,
      stage: 'vector_candidate_pool',
    },
    outcome: 'Final sind nur 3 von 8 angeforderten verifizierten Belegen verfügbar.',
    reasonDetail: 'Die Prüfung endete bei 64 Kandidaten.',
  },
  {
    degradation: {
      candidate_cap: null,
      final_evidence_complete: true,
      final_top_k: 3,
      reason: 'vector_candidate_stalled',
      requested_candidate_pool: 12,
      requested_top_k: 3,
      retrieval_mode: 'dense',
      returned_candidate_pool: 6,
      returned_hits: 3,
      stage: 'vector_candidate_pool',
    },
    outcome: 'Die 3 final angeforderten verifizierten Belege sind dennoch vollständig verfügbar.',
    reasonDetail: 'Die nächste Kandidatenstufe lieferte keine neuen aktiven Kandidaten und wurde beendet.',
  },
  {
    degradation: {
      candidate_cap: null,
      final_evidence_complete: false,
      final_top_k: 8,
      reason: 'vector_candidate_stalled',
      requested_candidate_pool: 12,
      requested_top_k: 8,
      retrieval_mode: 'dense',
      returned_candidate_pool: 6,
      returned_hits: 3,
      stage: 'vector_candidate_pool',
    },
    outcome: 'Final sind nur 3 von 8 angeforderten verifizierten Belegen verfügbar.',
    reasonDetail: 'Die nächste Kandidatenstufe lieferte keine neuen aktiven Kandidaten und wurde beendet.',
  },
] satisfies Array<{
  degradation: KnowledgeRetrievalDegradation
  outcome: string
  reasonDetail: string
}>

describe('AnswerCard retrieval degradation', () => {
  it.each(degradationCases)(
    'renders $degradation.reason with final completeness $degradation.final_evidence_complete truthfully',
    ({ degradation, outcome, reasonDetail }) => {
      const markup = renderToStaticMarkup(
        <LocaleProvider initialLocale="de">
          <TooltipProvider>
            <AnswerCard
              answer={{
                answerMarkdown: 'Belegte Teilantwort.',
                degradedStages: [],
                quotes: [],
                references: [],
                refusal: false,
                retrievalDegradations: [degradation],
              }}
              collectionCount={1}
              onOpenReference={vi.fn()}
            />
          </TooltipProvider>
        </LocaleProvider>,
      )

      expect(markup).toContain('data-knowledge-retrieval-degraded="true"')
      expect(markup).toContain('Retrieval technisch eingeschränkt')
      expect(markup).toContain('Der Vektor-Kandidatenpool erreichte 6 von 12 geplanten Kandidaten.')
      expect(markup).toContain(outcome)
      expect(markup).toContain(reasonDetail)
      expect(markup).toContain('Belegte Teilantwort.')
    },
  )

  it('renders native source-integrity warnings in the same warning surface', () => {
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <TooltipProvider>
          <AnswerCard
            answer={{
              answerMarkdown: 'Belegte Teilantwort.',
              degradedStages: [],
              quotes: [],
              references: [],
              refusal: false,
              retrievalDegradations: [],
              retrievalWarnings: [{
                code: 'chunks_pending_reconciliation',
                count: 2,
                message: 'server fallback',
                reason: 'canonical_chunk_unavailable',
                recommended_action: 'reconcile',
                stage: 'canonical_hydration',
              }],
            }}
            collectionCount={1}
            onOpenReference={vi.fn()}
          />
        </TooltipProvider>
      </LocaleProvider>,
    )

    expect(markup).toContain('data-knowledge-retrieval-degraded="true"')
    expect(markup).toContain(
      '2 Vektortreffer wurden ausgeschlossen, weil der zugehörige '
      + 'kanonische Datensatz noch nicht abgeglichen war.',
    )
  })

  it.each([
    {
      locale: 'de',
      message: '1 Treffer wurde übergangen, weil ein inhaltsgleiches Dokument',
      title: 'Inhaltsgleiche Dokumente zusammengeführt',
    },
    {
      locale: 'en',
      message: '1 hit was skipped because a content-identical document',
      title: 'Content-identical documents consolidated',
    },
  ] as const)('renders duplicate consolidation as an informational notice in $locale', ({ locale, message, title }) => {
    vi.stubGlobal('localStorage', {
      getItem: () => locale,
      setItem: vi.fn(),
    })
    try {
      const markup = renderToStaticMarkup(
        <LocaleProvider initialLocale={locale}>
          <TooltipProvider>
            <AnswerCard
              answer={{
                answerMarkdown: 'Belegte Teilantwort.',
                degradedStages: [],
                quotes: [],
                references: [],
                refusal: false,
                retrievalDegradations: [],
                retrievalWarnings: [{
                  code: 'duplicate_documents_collapsed',
                  count: 1,
                  message: '',
                  reason: 'duplicate_document',
                  recommended_action: null,
                  stage: 'canonical_hydration',
                }],
              }}
              collectionCount={1}
              onOpenReference={vi.fn()}
            />
          </TooltipProvider>
        </LocaleProvider>,
      )

      expect(markup).toContain('data-knowledge-retrieval-notice="informational"')
      expect(markup).not.toContain('data-knowledge-retrieval-degraded="true"')
      expect(markup).toContain(title)
      expect(markup).toContain(message)
      expect(markup).not.toContain('Retrieval technisch eingeschränkt')
      expect(markup).not.toContain('Retrieval technically limited')
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('AnswerCard AI marking', () => {
  it('marks the generated answer body machine-readably', () => {
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <TooltipProvider>
          <AnswerCard
            answer={{
              answerMarkdown: 'Belegte Teilantwort.',
              degradedStages: [],
              quotes: [],
              references: [],
              refusal: false,
              retrievalDegradations: [],
            }}
            collectionCount={1}
            onOpenReference={vi.fn()}
          />
        </TooltipProvider>
      </LocaleProvider>,
    )

    expect(markup).toContain('data-ai-generated="true"')
    expect(markup).toContain('data-ai-producer="Inqtrix"')
    // Anchor the probe: the marker sits on the body that carries the answer,
    // not on some unrelated wrapper that happens to render first.
    const bodyIndex = markup.indexOf('Belegte Teilantwort.')
    const markerIndex = markup.lastIndexOf('data-ai-generated="true"', bodyIndex)
    expect(markerIndex).toBeGreaterThan(-1)
  })
})
