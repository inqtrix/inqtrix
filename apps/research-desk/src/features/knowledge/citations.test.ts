import { describe, expect, it } from 'vitest'
import type { KnowledgeReferenceRecord } from '@/features/project/types'
import {
  activeCitationGroup,
  citationKey,
  citationViews,
  firstOpenableCitation,
  groupCitationsByDocument,
} from './citations'

const SECTION = 'Abschnitt {n}'

function ref(over: Partial<KnowledgeReferenceRecord>): KnowledgeReferenceRecord {
  return { label: 'K1', tier: 'primary', url: 'inqtrix://documents/d1#chunk-0', ...over }
}

describe('citationViews', () => {
  it('leads with the verbatim quote, falls back to excerpt then null (title at render)', () => {
    const refs = [
      ref({ label: 'K1', documentId: 'd1', chunkIndex: 2, excerpt: 'Voller Chunk-Text.' }),
      ref({ label: 'K2', documentId: 'd2', chunkIndex: 0, excerpt: 'Anderer Chunk.', title: 'B.pdf', url: 'inqtrix://documents/d2#chunk-0' }),
      ref({ label: 'K3', documentId: 'd3', title: 'C.pdf', url: 'inqtrix://documents/d3' }),
    ]
    const quotes = [{ label: 'K1', text: 'Die zitierte Stelle.', verified: true }]

    const views = citationViews(refs, quotes, SECTION)

    expect(views[0].snippet).toBe('Die zitierte Stelle.') // quote wins over excerpt
    expect(views[0].verified).toBe(true)
    expect(views[0].sectionLabel).toBe('Abschnitt 3') // chunkIndex + 1
    expect(views[1].snippet).toBe('Anderer Chunk.') // excerpt fallback (no quote)
    expect(views[1].verified).toBe(false)
    expect(views[2].snippet).toBeNull() // neither quote nor excerpt → title fallback at render
  })

  it('clamps a long snippet with an ellipsis', () => {
    const long = `${'a'.repeat(400)}`
    const views = citationViews([ref({ documentId: 'd', chunkIndex: 0, excerpt: long })], [], SECTION)
    expect(views[0].snippet?.endsWith('…')).toBe(true)
    expect((views[0].snippet ?? '').length).toBeLessThan(long.length)
  })
})

describe('groupCitationsByDocument', () => {
  it('collapses chunks of the same document into one group, in first-appearance order', () => {
    const refs = [
      ref({ label: 'K1', documentId: 'd1', chunkIndex: 0, title: 'A.pdf', url: 'inqtrix://documents/d1#chunk-0' }),
      ref({ label: 'K2', documentId: 'd2', chunkIndex: 0, title: 'B.pdf', url: 'inqtrix://documents/d2#chunk-0' }),
      ref({ label: 'K3', documentId: 'd1', chunkIndex: 5, title: 'A.pdf', url: 'inqtrix://documents/d1#chunk-5' }),
    ]

    const groups = groupCitationsByDocument(citationViews(refs, [], SECTION))

    expect(groups.map((group) => group.documentId)).toEqual(['d1', 'd2'])
    expect(groups[0].citations.map((view) => view.label)).toEqual(['K1', 'K3'])
    expect(groups[1].citations.map((view) => view.label)).toEqual(['K2'])
  })
})

describe('citationKey', () => {
  it('keys a cited passage by (document, chunk)', () => {
    expect(citationKey('d1', 3)).toBe('d1:3')
    expect(citationKey(null, null)).toBe(':')
  })
})

describe('activeCitationGroup', () => {
  const groups = groupCitationsByDocument(
    citationViews(
      [
        ref({ label: 'K1', documentId: 'd1', chunkIndex: 0, title: 'A.pdf', url: 'inqtrix://documents/d1#chunk-0' }),
        ref({ label: 'K2', documentId: 'd2', chunkIndex: 0, title: 'B.pdf', url: 'inqtrix://documents/d2#chunk-0' }),
      ],
      [],
      SECTION,
    ),
  )

  it('returns the group for the active document', () => {
    expect(activeCitationGroup(groups, 'd2')?.documentId).toBe('d2')
  })

  it('returns null for an unknown or missing document id', () => {
    expect(activeCitationGroup(groups, 'd9')).toBeNull()
    expect(activeCitationGroup(groups, null)).toBeNull()
    expect(activeCitationGroup(groups, undefined)).toBeNull()
  })
})

describe('firstOpenableCitation', () => {
  it('returns the first citation that can be opened', () => {
    const groups = groupCitationsByDocument(
      citationViews(
        [
          ref({ label: 'K5', documentId: 'd1', chunkIndex: 1, title: 'A.pdf', url: 'inqtrix://documents/d1#chunk-1' }),
          ref({ label: 'K6', documentId: 'd1', chunkIndex: 2, title: 'A.pdf', url: 'inqtrix://documents/d1#chunk-2' }),
        ],
        [],
        SECTION,
      ),
    )
    expect(firstOpenableCitation(groups[0])?.label).toBe('K5')
  })

  it('returns null when no citation in the group can be opened', () => {
    // A reference without a documentId is not openable (canOpen === false).
    const views = citationViews([ref({ label: 'K7', title: 'C.pdf', url: 'inqtrix://documents/x' })], [], SECTION)
    const group = { citations: views, documentId: null, title: 'C.pdf' }
    expect(firstOpenableCitation(group)).toBeNull()
  })
})
