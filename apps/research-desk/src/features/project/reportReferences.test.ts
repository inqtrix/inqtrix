import { describe, expect, it } from 'vitest'

import { normalizeReportReferences, partitionReportReferences, sourceRoleLabel } from './reportReferences'

describe('report reference normalization', () => {
  it('preserves compact web and Knowledge provenance without exposing arbitrary fields', () => {
    const [reference] = normalizeReportReferences(
      [{
        citation_id: 'citation-1',
        citation_ids: ['citation-1'],
        chunk_id: 'chunk-1',
        chunk_index: 3,
        document_id: 'document-1',
        generation_id: 'generation-1',
        ignored_provider_payload: { secret: true },
        label: 'K1',
        provenance_status: 'verified_span',
        provider_snippet: 'Provider metadata',
        query_id: 'query-1',
        query_ids: ['query-1'],
        reference_id: 'reference-1',
        revision_id: 'revision-1',
        source_id: 'source-1',
        source_run_id: 'run-1',
        source_run_ids: ['run-1'],
        source_span: {
          document_content_hash: 'sha256:document',
          end: 42,
          offset_unit: 'utf8_byte',
          start: 12,
        },
        tier: 'primary',
        title: 'Official source',
        url: 'https://example.test/source',
      }],
      '',
      [],
    )

    expect(reference).toEqual(expect.objectContaining({
      citation_id: 'citation-1',
      citation_ids: ['citation-1'],
      chunk_id: 'chunk-1',
      chunk_index: 3,
      document_id: 'document-1',
      generation_id: 'generation-1',
      provenance_status: 'verified_span',
      provider_snippet: 'Provider metadata',
      query_id: 'query-1',
      query_ids: ['query-1'],
      reference_id: 'reference-1',
      revision_id: 'revision-1',
      source_id: 'source-1',
      source_run_id: 'run-1',
      source_run_ids: ['run-1'],
      source_span: {
        document_content_hash: 'sha256:document',
        end: 42,
        offset_unit: 'utf8_byte',
        start: 12,
      },
    }))
    expect(reference).not.toHaveProperty('ignored_provider_payload')
  })

  it('keeps the cited flag and treats its absence as cited', () => {
    const references = normalizeReportReferences(
      [
        { label: 'W1', url: 'https://example.test/cited', cited: true },
        { label: 'W2', url: 'https://example.test/examined', cited: false },
        { label: 'W3', url: 'https://example.test/legacy' },
        { label: 'W4', url: 'https://example.test/garbage', cited: 'yes' },
      ],
      '',
      [],
    )

    expect(references.map((reference) => reference.cited)).toEqual([true, false, undefined, undefined])

    const { cited, examined } = partitionReportReferences(references)
    expect(cited.map((reference) => reference.label)).toEqual(['W1', 'W3', 'W4'])
    expect(examined.map((reference) => reference.label)).toEqual(['W2'])
  })

  it('keeps the tier provenance and labels the judged or proven role', () => {
    const [judged, structural, unjudged] = normalizeReportReferences(
      [
        { label: 'W1', url: 'https://www.kfw.de/x', tier: 'primary', tier_reason: 'model:official_primary', role_reason: 'Foerderbank des Bundes' },
        { label: 'W2', url: 'https://ecb.europa.eu/x', tier: 'primary', tier_reason: 'structural:official_primary', role_reason: '' },
        { label: 'W3', url: 'https://example.test/x', tier: 'unknown', tier_reason: 'unjudged' },
      ],
      '',
      [],
    )
    expect(judged.role_reason).toBe('Foerderbank des Bundes')
    expect(sourceRoleLabel(judged)).toBe('primary · official_primary')
    expect(sourceRoleLabel(structural)).toBe('primary · official_primary')
    expect(sourceRoleLabel(unjudged)).toBe('unknown')
    expect(sourceRoleLabel({ tier: 'mainstream' })).toBe('mainstream')
  })
})
