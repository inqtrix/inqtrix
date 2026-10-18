import { describe, expect, it } from 'vitest'

import {
  citationViews,
  groupCitationsByDocument,
} from '@/features/knowledge/citations'
import type { AgentArtifactReference } from './artifactCitations'
import {
  agentArtifactReferences,
  agentReferenceAsKnowledge,
  agentReferenceViewerTarget,
  isKnowledgeReference,
  isWebEvidenceReference,
  linkifyAgentArtifactCitations,
  answerCitationLabels,
} from './artifactCitations'

describe('agent artifact citations', () => {
  it('normalizes web and RAG provenance, including legacy refs without excerpts', () => {
    const refs = agentArtifactReferences([
      {
        citation_id: 'citation-1',
        citation_ids: ['citation-1'],
        excerpt: 'Supporting web passage',
        grounded_support: 'Provider-grounded market statement',
        label: 'W1',
        provider_snippet: 'Provider citation metadata',
        query_id: 'query-1',
        query_ids: ['query-1'],
        reference_id: 'reference-1',
        source_id: 'source-1',
        source_run_id: 'run-1',
        source_run_ids: ['run-1'],
        title: 'Market report',
        url: 'https://example.com/report',
      },
      {
        chunk_id: 'chunk-1',
        chunk_index: 4,
        collection_id: 'collection-1',
        document_id: 'doc-1',
        generation_id: 'generation-1',
        label: 'K1',
        provenance_status: 'verified_span',
        revision_id: 'revision-1',
        source_span: {
          document_content_hash: 'sha256:document',
          end: 42,
          offset_unit: 'utf8_byte',
          start: 12,
        },
        title: 'Internal study',
      },
    ])
    expect(refs).toEqual([
      expect.objectContaining({
        excerpt: 'Supporting web passage',
        groundedSupport: 'Provider-grounded market statement',
        label: 'W1',
        providerSnippet: 'Provider citation metadata',
        queryId: 'query-1',
        sourceRunId: 'run-1',
      }),
      expect.objectContaining({
        chunkIndex: 4,
        documentId: 'doc-1',
        excerpt: null,
        generationId: 'generation-1',
        groundedSupport: null,
        label: 'K1',
        provenanceStatus: 'verified_span',
        revisionId: 'revision-1',
        sourceSpan: {
          documentContentHash: 'sha256:document',
          end: 42,
          offsetUnit: 'utf8_byte',
          start: 12,
        },
      }),
    ])
    expect(agentReferenceAsKnowledge(refs[1]).url).toBe(
      'inqtrix://documents/doc-1',
    )
  })

  it('linkifies only labels present in the artifact ledger', () => {
    const refs = agentArtifactReferences([
      { label: 'W1', url: 'https://example.com', title: 'Example' },
      { label: 'K2', document_id: 'doc-2', chunk_index: 0 },
    ])
    expect(linkifyAgentArtifactCitations(
      'Aussage [W1] mit K2W1; W9 bleibt Text.',
      refs,
    )).toBe(
      'Aussage [W1](#kref-W1) mit [K2](#kref-K2)[W1](#kref-W1); W9 bleibt Text.',
    )
  })

  it('keeps a provider search result inspectable when no URL was returned', () => {
    const refs = agentArtifactReferences([
      {
        grounded_support: 'Frankreich gewann die WM 2018.',
        label: 'W1',
        query_id: 'query-answer-only',
        query_ids: ['query-answer-only'],
        reference_id: 'reference-answer-only',
        title: 'Fußball-Weltmeisterschaft 2018 Sieger',
      },
    ])

    expect(refs).toEqual([
      expect.objectContaining({
        documentId: null,
        groundedSupport: 'Frankreich gewann die WM 2018.',
        label: 'W1',
        queryId: 'query-answer-only',
        url: null,
      }),
    ])
  })
})

describe('delegated research citations', () => {
  it('keeps an E-labelled reference from a delegated research run', () => {
    // The kernel hands work to research; that run's evidence keeps its own
    // labels. Dropping them emptied the evidence panel on exactly the answers
    // that had done the most work.
    const refs = agentArtifactReferences([
      {
        label: 'E3',
        url: 'https://eur-lex.europa.eu/eli/reg/2024/1689/oj/eng',
        query_id: 'qry_36bb4f8bfe38a2',
        source_run_id: 'run_13c5bcac58ff44a59bab399b4090bcfd',
        tier: 'primary',
      },
    ])

    expect(refs).toHaveLength(1)
    expect(refs[0]?.label).toBe('E3')
    expect(refs[0]?.sourceRunId).toBe('run_13c5bcac58ff44a59bab399b4090bcfd')
  })

  it('still drops a label outside the citation vocabulary', () => {
    expect(agentArtifactReferences([
      { label: 'X7', url: 'https://example.invalid/x' },
      { label: 'note', url: 'https://example.invalid/n' },
    ])).toHaveLength(0)
  })
})

describe('citation target', () => {
  const refs = agentArtifactReferences([
    { label: 'E3', url: 'https://eur-lex.europa.eu/x', query_id: 'q1' },
  ])

  it('sends a known citation to the evidence panel, not out of the app', () => {
    // The panel carries the excerpt, the provenance and the verification
    // status; the URL stays reachable from there. A click on evidence should
    // land on the evidence.
    const out = linkifyAgentArtifactCitations(
      'Stand [E3](https://eur-lex.europa.eu/x) gilt die Verordnung.',
      refs,
    )

    expect(out).toContain('[E3](#kref-E3)')
    expect(out).not.toContain('https://eur-lex.europa.eu/x')
  })

  it('leaves an ordinary link alone', () => {
    const out = linkifyAgentArtifactCitations(
      'Siehe [Z9](https://example.invalid/z) und [den Bericht](https://example.invalid/r).',
      refs,
    )

    expect(out).toContain('[Z9](https://example.invalid/z)')
    expect(out).toContain('[den Bericht](https://example.invalid/r)')
  })
})

describe('web vs knowledge row classification', () => {
  const base = { documentId: null, queryId: null, url: null }

  it('keeps a knowledge citation OUT of the web row even though its url is http', () => {
    // A K reference carries the internal source endpoint as its url
    // (http://<host>/v1/sources/<doc>?chunk=N). Reading that as a web
    // hit rendered index citations as "Websuche - provider-belegt"
    // with the server host as their domain and a raw API link in
    // place of the evidence opener.
    const knowledge = {
      ...base,
      documentId: 'kd_58b674c1e8e842ce99c4',
      url: 'http://192.168.178.62:8080/v1/sources/kd_58b674c1e8e842ce99c4?chunk=194',
    }

    expect(isKnowledgeReference(knowledge)).toBe(true)
    expect(isWebEvidenceReference(knowledge)).toBe(false)
  })

  it('still classifies provider hits and plain web urls as web rows', () => {
    expect(isWebEvidenceReference({ ...base, queryId: 'query-1' })).toBe(true)
    expect(
      isWebEvidenceReference({ ...base, url: 'https://eur-lex.europa.eu/x' }),
    ).toBe(true)
  })

  it('treats a reference without provenance as a plain (non-web) row', () => {
    expect(isWebEvidenceReference(base)).toBe(false)
    expect(isWebEvidenceReference({ ...base, url: 'mailto:x@example.invalid' }))
      .toBe(false)
  })
})

describe('agentReferenceViewerTarget', () => {
  const knowledge: AgentArtifactReference = {
    citationId: null,
    citationIds: [],
    chunkId: null,
    generationId: null,
    groundedSupport: null,
    providerSnippet: null,
    queryIds: [],
    referenceId: null,
    revisionId: null,
    sourceId: null,
    sourceRunId: null,
    sourceRunIds: [],
    sourceSpan: null,
    chunkIndex: 3,
    collectionId: 'kc_1',
    documentId: 'kd_1',
    excerpt: 'Ref-Auszug mit Retrieval-Kontext',
    label: 'K1',
    pageNumber: 12,
    provenanceStatus: 'verified_span',
    queryId: null,
    title: 'AI ACT Regulation.pdf',
    url: 'http://host/v1/sources/kd_1?chunk=3',
  }

  it('prefers the canonical chunk text over the stored ref excerpt', () => {
    // The chunk text IS document text; a stored excerpt may carry
    // contextual-retrieval scaffolding that never appears in the
    // document and would silently fail to highlight.
    const target = agentReferenceViewerTarget(knowledge, 'Wortlaut im Dokument', 'EU-AI-Act-vec')

    expect(target?.highlightTargets).toEqual([
      'Wortlaut im Dokument',
      'Ref-Auszug mit Retrieval-Kontext',
    ])
    expect(target?.excerpt).toBe('Wortlaut im Dokument')
    expect(target?.collectionLabel).toBe('EU-AI-Act-vec')
    expect(target?.verified).toBe(true)
    expect(target?.pageNumber).toBe(12)
  })

  it('falls back to the ref excerpt before the chunk has landed', () => {
    const target = agentReferenceViewerTarget(knowledge, null, undefined)

    expect(target?.highlightTargets).toEqual(['Ref-Auszug mit Retrieval-Kontext'])
  })

  it('returns null for a web reference — there is no document to open', () => {
    const web = { ...knowledge, documentId: null, queryId: 'q1' }

    expect(agentReferenceViewerTarget(web, null, undefined)).toBeNull()
  })

  it('drops blank highlight candidates instead of matching everything', () => {
    const blank = { ...knowledge, excerpt: '   ' }

    expect(agentReferenceViewerTarget(blank, null, undefined)?.highlightTargets)
      .toEqual([])
  })
})

const emptyReference: AgentArtifactReference = {
  chunkId: null,
  chunkIndex: null,
  citationId: null,
  citationIds: [],
  collectionId: null,
  documentId: null,
  excerpt: null,
  generationId: null,
  groundedSupport: null,
  label: '',
  pageNumber: null,
  provenanceStatus: null,
  providerSnippet: null,
  queryId: null,
  queryIds: [],
  referenceId: null,
  revisionId: null,
  sourceId: null,
  sourceRunId: null,
  sourceRunIds: [],
  sourceSpan: null,
  title: '',
  url: null,
}

describe('agent citations rendered as knowledge citations', () => {
  it('carries passage, section and page into the shared citation view', () => {
    // A source list printing only the file name would make seven
    // citations of one PDF read as seven identical rows with no hint
    // WHERE in the document each one sits.
    const views = citationViews(
      [
        agentReferenceAsKnowledge({
          ...emptyReference,
          chunkIndex: 289,
          documentId: 'kd_1',
          excerpt: 'Article 50 Transparency obligations',
          label: 'K1',
          pageNumber: 168,
          title: 'AI ACT Regulation.pdf',
        }),
        agentReferenceAsKnowledge({
          ...emptyReference,
          chunkIndex: 140,
          documentId: 'kd_1',
          excerpt: 'machine readable marking',
          label: 'K3',
          pageNumber: 75,
          title: 'AI ACT Regulation.pdf',
        }),
      ],
      [],
      'Abschnitt {n}',
    )

    expect(views[0].snippet).toContain('Article 50 Transparency')
    expect(views[0].sectionLabel).toBe('Abschnitt 290')
    expect(views[0].pageNumber).toBe(168)

    // ... and both passages of the same PDF collapse into ONE source.
    const groups = groupCitationsByDocument(views)
    expect(groups).toHaveLength(1)
    expect(groups[0].citations.map((view) => view.label)).toEqual(['K1', 'K3'])
  })
})

describe('answerCitationLabels', () => {
  const announced = ['W1', 'K2']
  const refs = [{ label: 'W1', title: 'A' }, { label: 'K2', title: 'B' }] as never[]

  it('uses the announced labels while writing AND at ready until the refs land', () => {
    expect(answerCitationLabels(true, announced, []).map((r) => r.label)).toEqual(['W1', 'K2'])
    // Ready, refs not fetched yet: the same labels — no plain-text step
    // between the streamed body and the detail fetch.
    expect(answerCitationLabels(false, announced, []).map((r) => r.label)).toEqual(['W1', 'K2'])
  })

  it('prefers the real references once they exist', () => {
    expect(answerCitationLabels(false, ['W9'], refs)).toEqual(refs)
  })
})

describe('agent artifact citations read the citation grammar', () => {
  it('links both members of a grouped bracket the references hold', () => {
    const references = [{ label: 'K2' }, { label: 'W7' }] as AgentArtifactReference[]
    expect(linkifyAgentArtifactCitations('Marktteilnehmer wäre.[K2, W7]', references)).toBe(
      'Marktteilnehmer wäre.[K2](#kref-K2) [W7](#kref-W7)',
    )
  })
})
