import { describe, expect, it } from 'vitest'

import { agentArtifactReferences } from './artifactCitations'
import { evidenceLineageFromArtifactPayload } from './evidenceProvenance'

function webReference(overrides: Record<string, unknown> = {}) {
  const [reference] = agentArtifactReferences([{
    citation_id: 'citation-1',
    label: 'W1',
    query_id: 'query-1',
    query_ids: ['query-1'],
    source_id: 'source-1',
    source_run_id: 'run-child',
    title: 'Official price page',
    url: 'https://example.test/prices',
    ...overrides,
  }])
  if (!reference) throw new Error('reference fixture was rejected')
  return reference
}

describe('web-search provenance projection', () => {
  it('shows the exact provider answer and its honest citation mapping', () => {
    const reference = webReference()
    const payload = {
      web_search_ledger: {
        kind: 'web_search_ledger',
        schema_version: 1,
        searches: {
          'query-1': {
            citations: [{
              citation_id: 'citation-1',
              grounded_support: 'Global input costs 5 USD.',
              mapping_status: 'provider_answer_context',
              rank: 1,
              snippet: 'Azure list pricing.',
              source_id: 'source-1',
              title: 'Official price page',
              url: 'https://example.test/prices',
            }],
            duration_ms: 431,
            invocation_id: 'invocation-1',
            provider: 'AzureWebSearch',
            provider_answer: 'Global input costs 5 USD. [Source](https://example.test/prices)',
            query: 'GPT-5.6 Sol Azure price',
            query_id: 'query-1',
            source_run_id: 'run-child',
            status: 'completed',
          },
        },
      },
    }

    const lineage = evidenceLineageFromArtifactPayload(payload, reference)

    expect(lineage?.searches).toEqual([
      expect.objectContaining({
        durationMs: 431,
        invocationId: 'invocation-1',
        provider: 'AzureWebSearch',
        providerAnswer: expect.stringContaining('Global input costs 5 USD'),
        query: 'GPT-5.6 Sol Azure price',
        sourceRunId: 'run-child',
        status: 'completed',
        citation: expect.objectContaining({
          groundedSupport: 'Global input costs 5 USD.',
          mappingStatus: 'provider_answer_context',
          providerSnippet: 'Azure list pricing.',
        }),
      }),
    ])
  })

  it('does not borrow another query when no reference lineage matches', () => {
    const lineage = evidenceLineageFromArtifactPayload({
      web_search_ledger: {
        searches: {
          'query-2': {
            citations: [{
              citation_id: 'citation-2',
              source_id: 'source-2',
              url: 'https://other.test/',
            }],
            provider_answer: 'Unrelated answer',
            query_id: 'query-2',
          },
        },
      },
    }, webReference())

    expect(lineage).toBeNull()
  })

  it('preserves Azure citation-marker precision without claiming a 1:1 passage', () => {
    const lineage = evidenceLineageFromArtifactPayload({
      web_search_ledger: {
        searches: {
          'query-1': {
            citations: [{
              citation_id: 'citation-1',
              grounded_support: 'Global input costs 5 USD.',
              mapping_status: 'provider_citation_marker',
              source_id: 'source-1',
              url: 'https://example.test/prices',
            }],
            provider_answer: 'Global input costs 5 USD. [Source](https://example.test/prices)',
            query_id: 'query-1',
          },
        },
      },
    }, webReference())

    expect(lineage?.searches[0]?.citation).toEqual(expect.objectContaining({
      groundedSupport: 'Global input costs 5 USD.',
      mappingStatus: 'provider_citation_marker',
    }))
  })

  it('shows the place of a source that carries a statement, the first of them in the answer', () => {
    const cited = (
      citationId: string,
      start: number,
      groundedSupport?: string,
    ) => ({
      annotation_start: start,
      citation_id: citationId,
      ...(groundedSupport
        ? { grounded_support: groundedSupport, mapping_status: 'provider_citation_marker' }
        : { mapping_status: 'source_only' }),
      source_id: 'source-1',
      url: 'https://example.test/prices',
    })
    const lineage = evidenceLineageFromArtifactPayload({
      web_search_ledger: {
        searches: {
          'query-1': {
            citations: [
              cited('citation-alone', 12),
              cited('citation-late', 400, 'Data Zone costs more.'),
              cited('citation-early', 40, 'Global input costs 5 USD.'),
            ],
            provider_answer: 'Global input costs 5 USD. Data Zone costs more.',
            query_id: 'query-1',
          },
        },
      },
    }, webReference({
      citation_id: 'citation-alone',
      citation_ids: ['citation-alone', 'citation-late', 'citation-early'],
      grounded_support: 'Global input costs 5 USD.',
    }))

    expect(lineage?.searches[0]?.citation).toEqual(expect.objectContaining({
      citationId: 'citation-early',
      groundedSupport: 'Global input costs 5 USD.',
      mappingStatus: 'provider_citation_marker',
    }))
  })

  it('shows a place without a statement when the source has no other', () => {
    const lineage = evidenceLineageFromArtifactPayload({
      web_search_ledger: {
        searches: {
          'query-1': {
            citations: [{
              annotation_start: 12,
              citation_id: 'citation-1',
              mapping_status: 'source_only',
              source_id: 'source-1',
              url: 'https://example.test/prices',
            }],
            provider_answer: 'Sources: [Prices](https://example.test/prices)',
            query_id: 'query-1',
          },
        },
      },
    }, webReference())

    expect(lineage?.searches[0]?.citation).toEqual(expect.objectContaining({
      citationId: 'citation-1',
      mappingStatus: 'source_only',
    }))
  })

  describe('the places where a search cites a source', () => {
    const place = (
      citationId: string,
      start: number | null,
      groundedSupport?: string,
      url = 'https://example.test/prices',
    ) => ({
      annotation_start: start,
      citation_id: citationId,
      ...(groundedSupport
        ? { grounded_support: groundedSupport, mapping_status: 'provider_citation_marker' }
        : { mapping_status: 'source_only' }),
      source_id: url.endsWith('/prices') ? 'source-1' : 'source-2',
      url,
    })
    const search = (queryId: string, citations: Record<string, unknown>[]) => ({
      citations,
      provider_answer: 'A table with one row per month.',
      query_id: queryId,
    })

    it('lists each statement once, in the order of the answer', () => {
      // A table of monthly values from one source: every row cites it.
      const lineage = evidenceLineageFromArtifactPayload({
        web_search_ledger: {
          searches: {
            'query-1': search('query-1', [
              place('citation-september', 400, '3,3 | % | September 2026'),
              place('citation-unplaced', null, 'Jahresdurchschnitt 2,7 %'),
              place('citation-june', 40, '2,3 | % | Juni 2026'),
              place('citation-june-twin', 60, '2,3 | % | Juni 2026'),
              place('citation-alone', 12),
              place('citation-other', 20, 'Another source.', 'https://example.test/other'),
            ]),
          },
        },
      }, webReference({
        citation_id: 'citation-september',
        citation_ids: [
          'citation-september', 'citation-unplaced', 'citation-june', 'citation-june-twin', 'citation-alone',
        ],
        grounded_support: '2,3 | % | Juni 2026',
      }))

      expect(lineage?.searches[0]?.places).toEqual([
        '2,3 | % | Juni 2026',
        '3,3 | % | September 2026',
        'Jahresdurchschnitt 2,7 %',
      ])
      // The list opens with the place the single view shows.
      expect(lineage?.searches[0]?.citation?.groundedSupport).toBe('2,3 | % | Juni 2026')
    })

    it('keeps the places of another search with that search', () => {
      const lineage = evidenceLineageFromArtifactPayload({
        web_search_ledger: {
          searches: {
            'query-1': search('query-1', [place('citation-1', 10, 'First search, first row.')]),
            'query-2': search('query-2', [
              place('citation-2', 10, 'Second search, first row.'),
              place('citation-3', 90, 'Second search, second row.'),
            ]),
          },
        },
      }, webReference({
        citation_ids: ['citation-1', 'citation-2', 'citation-3'],
        query_ids: ['query-1', 'query-2'],
        source_run_id: null,
      }))

      expect(lineage?.searches.map((item) => item.places)).toEqual([
        ['First search, first row.'],
        ['Second search, first row.', 'Second search, second row.'],
      ])
    })

    it('lends no text of the reference to a place that has none', () => {
      // The reference keeps the text of the search it was registered with;
      // in another search of the source that text stands nowhere.
      const lineage = evidenceLineageFromArtifactPayload({
        web_search_ledger: {
          searches: { 'query-1': search('query-1', [place('citation-1', 12)]) },
        },
      }, webReference({ grounded_support: 'Text of an earlier search.' }))

      expect(lineage?.searches[0]?.places).toEqual([])
      expect(lineage?.searches[0]?.citation?.groundedSupport).toBe('Text of an earlier search.')
    })

    it('lists nothing for a search that returned no citation of the source', () => {
      const lineage = evidenceLineageFromArtifactPayload({
        web_search_ledger: {
          searches: {
            'query-1': search('query-1', [
              place('citation-other', 20, 'Another source.', 'https://example.test/other'),
            ]),
          },
        },
      }, webReference())

      expect(lineage?.searches[0]?.places).toEqual([])
    })
  })

  it('opens the raw provider answer even when the search returned no URLs', () => {
    const reference = webReference({
      citation_id: undefined,
      query_id: 'query-answer-only',
      query_ids: ['query-answer-only'],
      source_id: undefined,
      title: 'Fußball-Weltmeisterschaft 2018 Sieger',
      url: undefined,
    })
    const lineage = evidenceLineageFromArtifactPayload({
      web_search_ledger: {
        searches: {
          'query-answer-only': {
            citations: [],
            invocation_id: 'query-answer-only',
            provider: 'AzureFoundryWebSearch',
            provider_answer: 'Frankreich gewann die WM 2018 gegen Kroatien.',
            query: 'Fußball-Weltmeisterschaft 2018 Sieger Finalgegner',
            query_id: 'query-answer-only',
            status: 'completed',
          },
        },
      },
    }, reference)

    expect(lineage?.searches).toEqual([
      expect.objectContaining({
        citation: null,
        provider: 'AzureFoundryWebSearch',
        providerAnswer: 'Frankreich gewann die WM 2018 gegen Kroatien.',
        queryId: 'query-answer-only',
      }),
    ])
  })
})
