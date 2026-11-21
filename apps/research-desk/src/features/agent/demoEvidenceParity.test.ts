import { describe, expect, it } from 'vitest'

import { DEMO_DOCUMENTS, demoDocumentText } from '@/features/knowledge/demo'
import { agentArtifactReferences } from './artifactCitations'
import { agentDemoKnowledgeRefs, demoWebRefWire, demoWebSearchLedger } from './demo'
import { evidenceLineageFromArtifactPayload } from './evidenceProvenance'

/**
 * The agent demo's K citations must resolve in the KNOWLEDGE demo
 * corpus. They did not: the refs pointed at `doc-demo-1`/`doc-demo-2`,
 * ids that exist nowhere, so opening a demo citation in the document
 * reader hit `demoDocumentText`'s "Dokument nicht gefunden." throw.
 * The demo is the shop window — a citation that cannot be verified
 * there teaches the wrong thing about the product.
 */
describe('agent demo evidence resolves in the knowledge demo corpus', () => {
  const knownIds = new Set(DEMO_DOCUMENTS.map((document) => document.id))

  it('cites only documents the demo actually has', () => {
    expect(agentDemoKnowledgeRefs.length).toBeGreaterThan(0)
    for (const reference of agentDemoKnowledgeRefs) {
      expect(knownIds, `unknown demo document ${reference.documentId}`)
        .toContain(reference.documentId)
    }
  })

  it('quotes text that really occurs in that document', () => {
    // Without a verbatim match the reader opens with no highlight —
    // functional, but it shows nothing of what the feature is for.
    for (const reference of agentDemoKnowledgeRefs) {
      const document = demoDocumentText(reference.documentId)
      expect(reference.sourceText, `${reference.label} has no passage`)
        .toBeTruthy()
      expect(
        document.text,
        `${reference.label} passage missing from ${reference.documentId}`,
      ).toContain(reference.sourceText)
    }
  })
})

/**
 * The demo's W citations must open on the search that produced them. The
 * evidence view reads the search record of the run; without one a demo
 * citation shows "no search record", and the list of the places where a
 * search cites a source could not be shown at all.
 */
describe('agent demo web citations resolve in the demo search record', () => {
  const ledger = demoWebSearchLedger('run-demo')
  const search = Object.values(
    ledger.searches as Record<string, {
      citations: {
        annotation_end: number
        annotation_start: number
        grounded_support: string
        title: string
        url: string
      }[]
      provider_answer: string
    }>,
  )[0]
  const places = (label: 'W1' | 'W2' | 'W3') => {
    const [reference] = agentArtifactReferences([demoWebRefWire(label, 'run-demo')])
    if (!reference) throw new Error(`demo reference ${label} was rejected`)
    return evidenceLineageFromArtifactPayload({ web_search_ledger: ledger }, reference)
      ?.searches.map((item) => item.places)
  }

  it('shows a source the search cites at three places as a list of three', () => {
    // The seed of the list: one source, several places, in answer order.
    expect(places('W1')).toEqual([[
      'Der Markt für KI-Compliance konsolidiert sich: Kleinere Anbieter suchen Partnerschaften mit etablierten Compliance-Plattformen.',
      'Seit Anfang 2026 haben drei Plattformen Übernahmen angekündigt.',
      'Für den Mittelstand entstehen daraus gebündelte Angebote aus Beratung und Software.',
    ]])
  })

  it.each(['W2', 'W3'] as const)('shows %s, cited once, with its one place', (label) => {
    expect(places(label)?.map((list) => list.length)).toEqual([1])
  })

  it('marks every citation at the link of its source, behind its statement', () => {
    // A position that points elsewhere would show a statement the answer
    // does not make at that link.
    for (const citation of search.citations) {
      const link = `([${citation.title}](${citation.url}))`
      expect(search.provider_answer.slice(citation.annotation_start, citation.annotation_end)).toBe(link)
      expect(
        search.provider_answer.slice(0, citation.annotation_start).trimEnd().endsWith(citation.grounded_support),
      ).toBe(true)
    }
  })
})

