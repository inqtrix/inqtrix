import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { LocaleProvider } from '@/i18n/LocaleProvider'
import { agentArtifactReferences } from './artifactCitations'
import { EvidenceProvenancePanel } from './EvidenceProvenancePanel'
import { evidenceLineageFromArtifactPayload } from './evidenceProvenance'

function reference(raw: Record<string, unknown>) {
  const [value] = agentArtifactReferences([raw])
  if (!value) throw new Error('reference fixture was rejected')
  return value
}

describe('EvidenceProvenancePanel', () => {
  it('renders the exact provider answer and an honest source mapping', () => {
    const web = reference({
      citation_id: 'citation-1',
      label: 'W1',
      query_id: 'query-1',
      reference_id: 'reference-1',
      source_id: 'source-1',
      title: 'Official source',
      url: 'https://example.test/data',
    })
    const payload = {
      web_search_ledger: {
        searches: {
          'query-1': {
            citations: [{
              citation_id: 'citation-1',
              mapping_status: 'source_only',
              snippet: 'Provider supplied snippet.',
              source_id: 'source-1',
              title: 'Official source',
              url: 'https://example.test/data',
            }],
            invocation_id: 'search-1',
            provider: 'AzureWebSearch',
            provider_answer: 'A coherent answer with **several sources**.',
            query: 'exact official price',
            query_id: 'query-1',
            status: 'completed',
          },
        },
      },
    }
    const lineage = evidenceLineageFromArtifactPayload(payload, web)
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <EvidenceProvenancePanel lineage={lineage} reference={web} />
      </LocaleProvider>,
    )

    expect(markup).toContain('data-evidence-provenance="web"')
    expect(markup).toContain('aria-labelledby="evidence-provenance-W1"')
    expect(markup).toContain('Websuche · provider-belegt')
    expect(markup).toContain('exact official price')
    expect(markup).toContain('search-1')
    expect(markup).toContain('AzureWebSearch')
    expect(markup).toContain('Suche abgeschlossen')
    expect(markup).toContain('Provider supplied snippet.')
    expect(markup).toContain('several sources')
    expect(markup).toContain('keinen eindeutigen Einzelabschnitt')
    expect(markup).toContain('href="https://example.test/data"')
  })

  it('shows exact Knowledge revision, generation and UTF-8 source span', () => {
    const knowledge = reference({
      chunk_id: 'chunk-1',
      collection_id: 'collection-1',
      document_id: 'document-1',
      generation_id: 'generation-1',
      label: 'K1',
      provenance_status: 'verified_span',
      reference_id: 'reference-1',
      revision_id: 'revision-1',
      source_span: {
        document_content_hash: 'sha256:document',
        end: 58,
        offset_unit: 'utf8_byte',
        start: 12,
      },
    })
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <EvidenceProvenancePanel lineage={null} reference={knowledge} />
      </LocaleProvider>,
    )

    expect(markup).toContain('data-evidence-provenance="knowledge"')
    expect(markup).toContain('Quellspan verifiziert')
    expect(markup).toContain('12–58 utf8_byte')
    expect(markup).toContain('revision-1')
    expect(markup).toContain('generation-1')
    expect(markup).toContain('sha256:document')
  })

  it('labels context near an Azure link marker without inventing exclusivity', () => {
    const web = reference({
      citation_id: 'citation-1',
      label: 'W1',
      query_id: 'query-1',
      source_id: 'source-1',
      title: 'Azure price',
      url: 'https://example.test/data',
    })
    const lineage = evidenceLineageFromArtifactPayload({
      web_search_ledger: {
        searches: {
          'query-1': {
            citations: [{
              citation_id: 'citation-1',
              grounded_support: '**Global input** costs 5 USD.',
              mapping_status: 'provider_citation_marker',
              source_id: 'source-1',
              url: 'https://example.test/data',
            }],
            provider_answer: 'Global input costs 5 USD.',
            query: 'exact official price',
            query_id: 'query-1',
          },
        },
      },
    }, web)
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <EvidenceProvenancePanel lineage={lineage} reference={web} />
      </LocaleProvider>,
    )

    expect(markup).toContain('Kontext an der Azure-Quellenmarke')
    expect(markup).toContain('keine von Azure garantierte 1:1-Zuordnung')
    expect(markup).toContain('>Global input</strong>')
    expect(markup).not.toContain('**Global input**')
  })

  it('labels a legacy web reference without inventing ledger detail', () => {
    const legacy = reference({
      label: 'W1',
      title: 'Legacy source',
      url: 'https://example.test/legacy',
    })
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <EvidenceProvenancePanel lineage={null} reference={legacy} />
      </LocaleProvider>,
    )

    expect(markup).toContain('kein passender Websuch-Ledgereintrag')
    expect(markup).not.toContain('Original gelesen')
  })
})
