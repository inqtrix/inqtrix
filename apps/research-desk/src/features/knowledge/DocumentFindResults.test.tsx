import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { LocaleProvider } from '@/i18n/LocaleProvider'
import type {
  KnowledgeRetrievalDegradation,
  KnowledgeSearchHit,
} from '@/features/researchRuns/types'
import { DocumentFindResults } from './DocumentFindResults'
import { groupHitsByDocument } from './findGrouping'

const hit: KnowledgeSearchHit = {
  chunk_id: 'chunk-1',
  chunk_index: 0,
  collection_id: 'collection-1',
  document_id: 'document-1',
  document_title: 'Vertrag.pdf',
  excerpt: 'Die Haftung ist begrenzt.',
  generation_id: 'generation-1',
  page_number: 4,
  provenance_status: 'verified_span',
  rank: 1,
  reference_id: 'K1',
  revision_id: 'revision-1',
  score: 0.91,
  source_span: {
    document_content_hash: 'sha256',
    end: 28,
    offset_unit: 'utf8_byte',
    start: 0,
  },
}

describe('DocumentFindResults retrieval warnings', () => {
  const cases = [
    ['vector_overfetch_cap', true, 3, 3, 64],
    ['vector_overfetch_cap', false, 8, 3, 64],
    ['vector_candidate_stalled', true, 3, 3, null],
    ['vector_candidate_stalled', false, 8, 3, null],
  ] as const

  it.each(cases)(
    'shows %s with final completeness %s without hiding usable hits',
    (reason, finalEvidenceComplete, finalTopK, returnedHits, candidateCap) => {
      const degradation: KnowledgeRetrievalDegradation = {
        candidate_cap: candidateCap,
        final_evidence_complete: finalEvidenceComplete,
        final_top_k: finalTopK,
        reason,
        requested_candidate_pool: 12,
        requested_top_k: finalTopK,
        retrieval_mode: 'hybrid',
        returned_candidate_pool: 6,
        returned_hits: returnedHits,
        stage: 'vector_candidate_pool',
      }
      const markup = renderToStaticMarkup(
        <LocaleProvider initialLocale="de">
          <DocumentFindResults
            collectionTitleFor={() => 'Verträge'}
            error={null}
            groups={groupHitsByDocument([hit])}
            onOpenSnippet={vi.fn()}
            query="Haftung"
            state="ready"
            warnings={[{
              ...degradation,
              code: reason,
              message: 'candidate limit reached',
            }]}
          />
        </LocaleProvider>,
      )

      expect(markup).toContain('data-knowledge-retrieval-degraded="true"')
      expect(markup).toContain('Retrieval technisch eingeschränkt')
      expect(markup).toContain('Der Vektor-Kandidatenpool erreichte 6 von 12 geplanten Kandidaten.')
      expect(markup).toContain(finalEvidenceComplete
        ? `Die ${finalTopK} final angeforderten verifizierten Belege sind dennoch vollständig verfügbar.`
        : `Final sind nur ${returnedHits} von ${finalTopK} angeforderten verifizierten Belegen verfügbar.`)
      expect(markup).toContain(reason === 'vector_candidate_stalled'
        ? 'Die nächste Kandidatenstufe lieferte keine neuen aktiven Kandidaten und wurde beendet.'
        : 'Die Prüfung endete bei 64 Kandidaten.')
      expect(markup).toContain('Vertrag.pdf')
      expect(markup).toContain('border-warning/40')
    },
  )

  it('shows a canonical-reconciliation exclusion without exposing hidden data', () => {
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <DocumentFindResults
          collectionTitleFor={() => 'Verträge'}
          error={null}
          groups={[]}
          onOpenSnippet={vi.fn()}
          query="Haftung"
          state="ready"
          warnings={[{
            code: 'chunks_pending_reconciliation',
            count: 2,
            message: 'internal fallback',
          }]}
        />
      </LocaleProvider>,
    )

    expect(markup).toContain('2 Vektortreffer wurden ausgeschlossen')
    expect(markup).toContain('Indexabgleich muss abgeschlossen werden')
    expect(markup).not.toContain('internal fallback')
  })

  it.each([
    {
      locale: 'de',
      message: '2 Treffer wurden übergangen, weil inhaltsgleiche Dokumente',
      title: 'Inhaltsgleiche Dokumente zusammengeführt',
    },
    {
      locale: 'en',
      message: '2 hits were skipped because content-identical documents',
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
          <DocumentFindResults
            collectionTitleFor={() => 'Verträge'}
            error={null}
            groups={groupHitsByDocument([hit])}
            onOpenSnippet={vi.fn()}
            query="Haftung"
            state="ready"
            warnings={[{
              code: 'duplicate_documents_collapsed',
              count: 2,
              message: '',
              reason: 'duplicate_document',
              recommended_action: null,
              stage: 'canonical_hydration',
            }]}
          />
        </LocaleProvider>,
      )

      expect(markup).toContain('data-knowledge-retrieval-notice="informational"')
      expect(markup).not.toContain('data-knowledge-retrieval-degraded="true"')
      expect(markup).toContain(title)
      expect(markup).toContain(message)
      expect(markup).not.toContain('Retrieval technisch eingeschränkt')
      expect(markup).not.toContain('Retrieval technically limited')
      expect(markup).toContain('Vertrag.pdf')
    } finally {
      vi.unstubAllGlobals()
    }
  })
})
