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
