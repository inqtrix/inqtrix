import { describe, expect, it } from 'vitest'

import { taskResultReferenceGroups } from './taskResultReferences'

describe('taskResultReferenceGroups', () => {
  it('deduplicates web sources by URL without creating title groups', () => {
    const groups = taskResultReferenceGroups([
      {
        label: 'W1',
        title: 'Report',
        url: 'https://Example.test/report?utm_source=newsletter#section',
      },
      {
        citation_id: 'citation-2',
        label: 'W2',
        provider_snippet: 'Provider metadata',
        query_id: 'query-1',
        source_id: 'source-1',
        title: 'Duplicate title',
        url: 'https://example.test/report?fbclid=tracking',
      },
    ])

    expect(groups).toHaveLength(1)
    expect(groups[0]?.kind).toBe('web')
    if (groups[0]?.kind === 'web') {
      expect(groups[0].reference.title).toBe('Report')
      expect(groups[0].reference.domain).toBe('example.test')
      expect(groups[0].reference.label).toBe('W1')
      expect(groups[0].reference).toEqual(expect.objectContaining({
        citationId: 'citation-2',
        providerSnippet: 'Provider metadata',
        queryId: 'query-1',
        sourceId: 'source-1',
      }))
    }
  })

  it('groups several chunks of one document but keeps distinct chunks', () => {
    const groups = taskResultReferenceGroups([
      { document_id: 'doc-1', chunk_index: 0, title: 'Study', excerpt: 'A' },
      { document_id: 'doc-1', chunk_index: 1, title: 'Study', excerpt: 'B' },
      { document_id: 'doc-1', chunk_index: 1, title: 'Study', excerpt: 'B' },
    ])

    expect(groups).toEqual([
      {
        kind: 'document',
        title: 'Study',
        references: [
          expect.objectContaining({ chunkIndex: 0, excerpt: 'A' }),
          expect.objectContaining({ chunkIndex: 1, excerpt: 'B' }),
        ],
      },
    ])
  })

  it('keeps a URL-less provider search as one web evidence row', () => {
    const groups = taskResultReferenceGroups([
      {
        grounded_support: 'Frankreich gewann die WM 2018.',
        label: 'W1',
        query_id: 'query-answer-only',
        reference_id: 'reference-answer-only',
        title: 'Fußball-Weltmeisterschaft 2018 Sieger',
      },
    ])

    expect(groups).toEqual([
      {
        kind: 'web',
        reference: expect.objectContaining({
          domain: null,
          key: 'query:query-answer-only',
          label: 'W1',
          queryId: 'query-answer-only',
          url: null,
        }),
      },
    ])
  })
})
