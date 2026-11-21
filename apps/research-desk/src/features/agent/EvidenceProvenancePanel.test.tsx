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

  describe('a source the search cites at several places', () => {
    const cited = (citationId: string, start: number, text: string) => ({
      annotation_start: start,
      citation_id: citationId,
      grounded_support: text,
      mapping_status: 'provider_citation_marker',
      source_id: 'source-1',
      url: 'https://example.test/data',
    })
    function panel(citations: Record<string, unknown>[]): string {
      const web = reference({
        citation_id: String(citations[0]?.citation_id),
        citation_ids: citations.map((citation) => String(citation.citation_id)),
        label: 'W1',
        query_id: 'query-1',
        source_id: 'source-1',
        url: 'https://example.test/data',
      })
      const lineage = evidenceLineageFromArtifactPayload({
        web_search_ledger: {
          searches: {
            'query-1': { citations, provider_answer: 'The whole table.', query_id: 'query-1' },
          },
        },
      }, web)
      return renderToStaticMarkup(
        <LocaleProvider initialLocale="de">
          <EvidenceProvenancePanel lineage={lineage} reference={web} />
        </LocaleProvider>,
      )
    }
    const entries = (markup: string) =>
      [...markup.matchAll(/<li[^>]*data-cited-place="(\d+)"[^>]*>(.*?)<\/li>/gs)].map(
        (entry) => [entry[1], entry[2].replace(/<[^>]+>/g, '').trim()],
      )

    it('lists them numbered under a heading that names their count', () => {
      const markup = panel([
        cited('citation-september', 400, 'Im September 3,3 Prozent.'),
        cited('citation-june', 40, 'Im Juni 2,3 Prozent.'),
        cited('citation-august', 300, 'Im August 2,9 Prozent.'),
      ])

      expect(markup).toContain('Stellen der Suchantwort, die diese Quelle zitieren (3)')
      expect(entries(markup)).toEqual([
        ['1', 'Im Juni 2,3 Prozent.'],
        ['2', 'Im August 2,9 Prozent.'],
        ['3', 'Im September 3,3 Prozent.'],
      ])
      // The single view of the first place gives way to the list, and so
      // does its hint, which speaks of one place.
      expect(markup).not.toContain('Kontext an der Azure-Quellenmarke')
      expect(markup).toContain('Die Websuche hat die Quelle an diesen Stellen ihrer zusammenhängenden Antwort zitiert.')
      expect(markup).not.toContain('an dieser Stelle')
    })

    it('draws each place on its own', () => {
      // Drawn as one text, the dollar signs of two places would pair up
      // into a formula and swallow what stands between them.
      const markup = panel([
        cited('citation-1', 10, 'Eingabe kostet 5 $ je Einheit.'),
        cited('citation-2', 90, 'Ausgabe kostet 15 $ je Einheit.'),
      ])

      expect(entries(markup)).toEqual([
        ['1', 'Eingabe kostet 5 $ je Einheit.'],
        ['2', 'Ausgabe kostet 15 $ je Einheit.'],
      ])
    })

    it('keeps the view of a source cited at one place as it was', () => {
      const markup = panel([cited('citation-1', 10, 'Im Juni 2,3 Prozent.')])

      expect(markup).toContain('Kontext an der Azure-Quellenmarke')
      expect(markup).toContain('Im Juni 2,3 Prozent.')
      expect(markup).toContain('Azure hat die URL an dieser Stelle')
      expect(markup).not.toContain('an diesen Stellen')
      expect(markup).not.toContain('Stellen der Suchantwort')
      expect(markup).not.toContain('data-cited-place')
    })
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

  it.each([
    [
      'de',
      'Ausschnitt des Suchanbieters',
      'Für diese URL liegt ein Ausschnitt des Suchanbieters vor. Die vollständige Antwort des Suchanbieters kann mehrere Quellen zusammenführen.',
    ],
    [
      'en',
      "The search provider's excerpt",
      "The search provider's excerpt is available for this URL. The search provider's complete answer may combine several sources.",
    ],
  ] as const)('names the search provider\'s excerpt with the one word of the report (%s)', (locale, heading, hint) => {
    const web = reference({
      citation_id: 'citation-1',
      label: 'W1',
      query_id: 'query-1',
      source_id: 'source-1',
      url: 'https://example.test/data',
    })
    const lineage = evidenceLineageFromArtifactPayload({
      web_search_ledger: {
        searches: {
          'query-1': {
            citations: [{
              citation_id: 'citation-1',
              mapping_status: 'provider_snippet',
              snippet: 'Die Frist endet am 2. August 2026.',
              source_id: 'source-1',
              url: 'https://example.test/data',
            }],
            provider_answer: 'Die Frist endet am 2. August 2026.',
            query: 'Frist',
            query_id: 'query-1',
          },
        },
      },
    }, web)
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale={locale}>
        <EvidenceProvenancePanel lineage={lineage} reference={web} />
      </LocaleProvider>,
    )

    const escaped = (text: string) => text.split("'").join('&#x27;')
    expect(markup).toContain(`>${escaped(heading)}</h3>`)
    expect(markup).toContain(escaped(hint))
    // The same excerpt is called the same in the report's statements; no
    // text names it a snippet or the provider's answer by an internal word.
    expect(markup.toLowerCase()).not.toContain('snippet')
    expect(markup.toLowerCase()).not.toContain('provider-antwort')
    expect(markup.toLowerCase()).not.toContain('provider answer')
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
