import { describe, expect, it } from 'vitest'

import { DEMO_DOCUMENTS, demoDocumentText } from '@/features/knowledge/demo'
import { agentDemoKnowledgeRefs } from './demo'

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
