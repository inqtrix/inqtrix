import { describe, expect, it } from 'vitest'
import type { ResearchRunResult } from '@/features/researchRuns/types'
import { citationLabelFromHref, knowledgeAnswerFromRunResult, linkifyCitationLabels } from './answer'

const known = new Set(['K1', 'K2', 'K6'])

describe('linkifyCitationLabels', () => {
  it('linkifies bracketed [K#] markers (always)', () => {
    expect(linkifyCitationLabels('siehe [K1] und [K2]')).toBe(
      'siehe [K1](#kref-K1) und [K2](#kref-K2)',
    )
  })

  it('linkifies BARE K# markers that match a known reference label', () => {
    expect(linkifyCitationLabels('Recruiting ist Hochrisiko K1.', known)).toBe(
      'Recruiting ist Hochrisiko [K1](#kref-K1).',
    )
  })

  it('splits compact adjacent K# markers when every label is known', () => {
    expect(linkifyCitationLabels('Belegt durch K1K2.', known)).toBe(
      'Belegt durch [K1](#kref-K1)[K2](#kref-K2).',
    )
  })

  it('leaves compact adjacent K# markers untouched when one label is unknown', () => {
    expect(linkifyCitationLabels('Belegt durch K1K9.', known)).toBe('Belegt durch K1K9.')
  })

  it('leaves a bare K# that is NOT a known label untouched (no dead links)', () => {
    // K9 is not a reference here; K2 as a plain token only links when known.
    expect(linkifyCitationLabels('Kaliumoxid K9 ist egal', known)).toBe(
      'Kaliumoxid K9 ist egal',
    )
  })

  it('does not linkify bare K# without a known-labels set (brackets only)', () => {
    expect(linkifyCitationLabels('nur K1 ohne Klammern')).toBe('nur K1 ohne Klammern')
  })

  it('does not touch tokens already rewritten into #kref links', () => {
    const once = linkifyCitationLabels('[K1] und K2', known)
    // Idempotent: a second pass must not double-wrap the existing link.
    expect(linkifyCitationLabels(once, known)).toBe(once)
    expect(once).toBe('[K1](#kref-K1) und [K2](#kref-K2)')
  })

  it('does not linkify K# embedded in a word', () => {
    expect(linkifyCitationLabels('das Modell K1A', new Set(['K1']))).toBe('das Modell K1A')
  })
})

describe('knowledgeAnswerFromRunResult references', () => {
  it('carries the explicit excerpt + ids from the backend reference (reliable open)', () => {
    const result = {
      answer: 'Antwort [K1].',
      report_references: [
        {
          chunk_index: 7,
          // An unparseable URL shape — the explicit document_id must still win,
          // so the citation opens reliably.
          document_id: 'kd_abc',
          excerpt: 'Vortext. Die Haftung ist begrenzt. Nachtext.',
          label: 'K1',
          source_text: 'Vortext. Die Haftung ist begrenzt. Nachtext.',
          tier: 'primary',
          title: 'AI Act.pdf',
          url: 'inqtrix://unparseable-shape',
        },
      ],
    } as unknown as ResearchRunResult

    const ref = knowledgeAnswerFromRunResult(result).references[0]
    expect(ref.documentId).toBe('kd_abc')
    expect(ref.chunkIndex).toBe(7)
    expect(ref.excerpt).toBe('Vortext. Die Haftung ist begrenzt. Nachtext.')
    expect(ref.sourceText).toBe('Vortext. Die Haftung ist begrenzt. Nachtext.')
    expect(ref.title).toBe('AI Act.pdf')
  })

  it('falls back to the URL-parsed id when no explicit document_id (older payload)', () => {
    const result = {
      answer: 'Antwort [K1].',
      report_references: [
        { label: 'K1', tier: 'primary', url: 'https://x/v1/sources/kd_old?chunk=3' },
      ],
    } as unknown as ResearchRunResult

    const ref = knowledgeAnswerFromRunResult(result).references[0]
    expect(ref.documentId).toBe('kd_old')
    expect(ref.chunkIndex).toBe(3)
    expect(ref.excerpt).toBeNull()
  })

  it('takes the verified quotes from the reference they support', () => {
    // The server attaches each published quote to its own citation; the
    // run's grounding summary carries counts only. A reference from an older
    // run has no quotes and gets no badge.
    const result = {
      answer: 'Antwort [K1] [K2].',
      knowledge_grounding: { enabled: true, quotes_total: 2, quotes_verified: 2 },
      report_references: [
        { label: 'K1', tier: 'primary', url: 'inqtrix://documents/kd_a#chunk-0' },
        {
          label: 'K2',
          quotes: [
            { artifact_tolerated: false, text: 'Die Haftung ist begrenzt.', verified: true },
            { artifact_tolerated: true, text: 'auf den Auftragswert', verified: true },
          ],
          tier: 'primary',
          url: 'inqtrix://documents/kd_b#chunk-1',
        },
      ],
    } as unknown as ResearchRunResult

    expect(knowledgeAnswerFromRunResult(result).quotes).toEqual([
      { label: 'K2', text: 'Die Haftung ist begrenzt.', verified: true },
      { label: 'K2', text: 'auf den Auftragswert', verified: true },
    ])
  })

  it('preserves an unparseable grounding result as a visible degradation', () => {
    const result = {
      answer: 'Antwort ohne auswertbaren Zitate-Block.',
      knowledge_grounding: {
        enabled: true,
        marker: '_knowledge_grounding_fallback',
        quotes_total: 0,
        quotes_verified: 0,
      },
    } as unknown as ResearchRunResult

    expect(knowledgeAnswerFromRunResult(result).grounding).toEqual({
      degraded: true,
      total: 0,
      verified: 0,
    })
  })

  it('projects persisted retrieval degradations onto the final answer record', () => {
    const degradation = {
      candidate_cap: 64,
      final_evidence_complete: false,
      final_top_k: 8,
      reason: 'vector_overfetch_cap',
      requested_candidate_pool: 40,
      requested_top_k: 8,
      retrieval_mode: 'hybrid',
      returned_candidate_pool: 6,
      returned_hits: 3,
      stage: 'vector_candidate_pool',
    }
    const result = {
      answer: 'Teilweise belegte Antwort.',
      knowledge_retrieval: { degradations: [degradation] },
    } as unknown as ResearchRunResult

    expect(knowledgeAnswerFromRunResult(result).retrievalDegradations).toEqual([
      degradation,
    ])
  })

  it('projects persisted source-integrity warnings onto the final answer record', () => {
    const warning = {
      code: 'chunks_pending_reconciliation',
      count: 3,
      message: 'server fallback',
      reason: 'canonical_chunk_unavailable',
      recommended_action: 'reconcile',
      stage: 'canonical_hydration',
    }
    const result = {
      answer: 'Teilweise belegte Antwort.',
      knowledge_retrieval: { warnings: [warning] },
    } as unknown as ResearchRunResult

    expect(knowledgeAnswerFromRunResult(result).retrievalWarnings).toEqual([
      warning,
    ])
  })
})

describe('citationLabelFromHref', () => {
  it('extracts the label from a #kref href', () => {
    expect(citationLabelFromHref('#kref-K3')).toBe('K3')
  })
  it('returns null for unrelated hrefs', () => {
    expect(citationLabelFromHref('https://example.com')).toBeNull()
    expect(citationLabelFromHref(null)).toBeNull()
  })
})
