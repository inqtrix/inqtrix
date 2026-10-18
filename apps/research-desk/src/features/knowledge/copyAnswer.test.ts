import { describe, expect, it } from 'vitest'
import type {
  KnowledgeAnswerRecord,
  KnowledgeQuoteRecord,
  KnowledgeReferenceRecord,
} from '@/features/project/types'
import { formatAnswerForCopy } from './copyAnswer'

const AI_NOTICE = 'Dieser Text wurde von einem KI-System erzeugt (Inqtrix).'

const labels = {
  aiDisclosure: AI_NOTICE,
  evidenceHeading: 'Belege',
  pageLabel: 'S. {n}',
  sectionLabel: 'Abschnitt {n}',
  sourcesHeading: 'Quellen',
  verifiedLabel: 'belegt',
}

// >220 chars (longer than citations.ts' SNIPPET_MAX clamp) and carrying newlines
// plus whitespace runs, so the "full + collapsed" contract is observable.
const LONG_EXCERPT = 'Dies ist ein langer Auszug mit\n  mehreren   Leerzeichen. '.repeat(8)

const REFERENCES: KnowledgeReferenceRecord[] = [
  {
    label: 'K1',
    url: 'kref://doc-a/2',
    tier: 'primary',
    title: 'Doc A',
    documentId: 'doc-a',
    chunkIndex: 2,
    pageNumber: 12,
    excerpt: LONG_EXCERPT,
  },
  {
    label: 'K2',
    url: 'https://example.com/report',
    tier: 'mainstream',
    title: 'Doc B',
    documentId: 'doc-b',
    chunkIndex: null,
    pageNumber: null,
    excerpt: 'Kurzer Beleg.',
  },
]

const QUOTES: KnowledgeQuoteRecord[] = [{ label: 'K1', text: 'woertlich', verified: true }]

function makeAnswer(overrides: Partial<KnowledgeAnswerRecord> = {}): KnowledgeAnswerRecord {
  return {
    answerMarkdown: 'Antwort [K1] und [K2].',
    refusal: false,
    references: REFERENCES,
    quotes: QUOTES,
    degradedStages: [],
    retrievalDegradations: [],
    ...overrides,
  }
}

describe('formatAnswerForCopy', () => {
  it("'answer' mode copies the answer markdown and no source list", () => {
    const out = formatAnswerForCopy(makeAnswer(), 'answer', labels)
    expect(out).toBe(`Antwort [K1] und [K2].\n\n> ${AI_NOTICE}\n`)
    expect(out).not.toContain('## Quellen')
  })

  it('a refusal / reference-less answer copies the bare text in every mode', () => {
    const refusal = makeAnswer({ answerMarkdown: 'Keine belegbare Antwort.', refusal: true, references: [], quotes: [] })
    for (const mode of ['answer', 'sources', 'evidence'] as const) {
      const out = formatAnswerForCopy(refusal, mode, labels)
      expect(out).toBe(`Keine belegbare Antwort.\n\n> ${AI_NOTICE}\n`)
      expect(out).not.toContain('## Quellen')
      expect(out).not.toContain('## Belege')
    }
  })

  it('carries the AI disclosure in every mode, including the refusal path', () => {
    const answer = makeAnswer()
    const refusal = makeAnswer({ answerMarkdown: 'Keine belegbare Antwort.', refusal: true, references: [], quotes: [] })
    for (const mode of ['answer', 'sources', 'evidence'] as const) {
      expect(formatAnswerForCopy(answer, mode, labels)).toContain(AI_NOTICE)
      expect(formatAnswerForCopy(refusal, mode, labels)).toContain(AI_NOTICE)
    }
  })

  it("'sources' lists one line per citation, in [K#] order, without excerpts or the belegt marker", () => {
    const out = formatAnswerForCopy(makeAnswer(), 'sources', labels)
    expect(out).toContain('## Quellen')
    expect(out).toContain('- [K1] Doc A · Abschnitt 3 · S. 12')
    expect(out).toContain('- [K2] Doc B · https://example.com/report')
    // The belegt marker rides with the visible excerpt — never in the source list.
    expect(out).not.toContain('belegt')
    // Internal (non-http) citation URLs are dropped; excerpts never appear here.
    expect(out).not.toContain('kref://')
    expect(out).not.toContain('Dies ist ein langer')
    expect(out.indexOf('[K1]')).toBeLessThan(out.indexOf('[K2]'))
  })

  it('never leaks an internal citation URL as the visible source name', () => {
    const titleless: KnowledgeReferenceRecord = {
      label: 'K9',
      url: 'inqtrix://doc/3',
      tier: 'primary',
      chunkIndex: null,
      pageNumber: null,
      excerpt: 'Beleg ohne Titel.',
    }
    const answer = makeAnswer({ references: [titleless], quotes: [] })
    for (const mode of ['sources', 'evidence'] as const) {
      const out = formatAnswerForCopy(answer, mode, labels)
      expect(out).toContain('[K9]')
      expect(out).not.toContain('inqtrix://')
    }
  })

  it("'evidence' adds the full collapsed excerpt and marks only verified sources", () => {
    const out = formatAnswerForCopy(makeAnswer(), 'evidence', labels)
    expect(out).toContain('## Belege')
    expect(out).toContain('**[K1] Doc A · Abschnitt 3 · S. 12 · belegt**')
    expect(out).toContain('**[K2] Doc B · https://example.com/report**')

    // K2 has no verified quote, so its headline carries no marker.
    const k2Line = out.split('\n').find((line) => line.startsWith('**[K2]'))
    expect(k2Line).not.toContain('belegt')

    // The full excerpt is preserved (not clamped) and whitespace-collapsed.
    const block = out.split('\n').find((line) => line.startsWith('> Dies'))
    expect(block).toBeDefined()
    expect(block).toContain('Dies ist ein langer Auszug mit mehreren Leerzeichen.')
    expect(block!.length).toBeGreaterThan(220)
  })

  it("'evidence' keeps the headline when a reference has no excerpt", () => {
    const out = formatAnswerForCopy(
      makeAnswer({ references: [{ ...REFERENCES[1], excerpt: null }], quotes: [] }),
      'evidence',
      labels,
    )
    expect(out).toContain('**[K2] Doc B · https://example.com/report**')
    // The trailing AI disclosure is the only blockquote; no excerpt block.
    expect(out.split('\n').filter((line) => line.startsWith('> '))).toEqual([`> ${AI_NOTICE}`])
  })
})
