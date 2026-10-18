import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import {
  pickStrictCandidate,
  resolveAnchorInMarkdown,
  type IndexedCandidate,
} from './anchoring'

type ParityCase = {
  name: string
  content: string
  find: string
  quote_before: string
  quote_after: string
  expected: number | null
}

const fixture = JSON.parse(
  readFileSync(
    fileURLToPath(
      new URL('../../../../../tests/fixtures/anchor_parity.json', import.meta.url),
    ),
    'utf8',
  ),
) as { cases: ParityCase[] }

describe('resolveAnchorInMarkdown — cross-language parity', () => {
  it('carries at least the agreed case count', () => {
    expect(fixture.cases.length).toBeGreaterThanOrEqual(10)
  })

  for (const parityCase of fixture.cases) {
    it(`matches the server resolver: ${parityCase.name}`, () => {
      expect(
        resolveAnchorInMarkdown(parityCase.content, {
          find: parityCase.find,
          quoteAfter: parityCase.quote_after,
          quoteBefore: parityCase.quote_before,
        }),
      ).toBe(parityCase.expected)
    })
  }
})

describe('pickStrictCandidate — server ambiguity policy on editor text', () => {
  const text = 'Q Wert a. Marker Wert b.'
  const candidates: IndexedCandidate[] = [
    { length: 4, range: { from: 3, to: 7 }, start: 2 },
    { length: 4, range: { from: 18, to: 22 }, start: 17 },
  ]

  it('abstains without quotes instead of guessing', () => {
    expect(
      pickStrictCandidate(text, candidates, { hint: 1, text: 'Wert' }),
    ).toBeNull()
  })

  it('hard-disqualifies candidates whose quote is missing', () => {
    expect(
      pickStrictCandidate(text, candidates, {
        hint: 1,
        quoteBefore: 'Marker',
        text: 'Wert',
      }),
    ).toEqual({ from: 18, to: 22 })
  })

  it('abstains on a symmetric tie', () => {
    const tieText = 'Q Wert a. Q Wert b.'
    const tieCandidates: IndexedCandidate[] = [
      { length: 4, range: { from: 3, to: 7 }, start: 2 },
      { length: 4, range: { from: 13, to: 17 }, start: 12 },
    ]
    expect(
      pickStrictCandidate(tieText, tieCandidates, {
        hint: 1,
        quoteBefore: 'Q',
        text: 'Wert',
      }),
    ).toBeNull()
  })

  it('ignores the hint entirely (strict has no nearest fallback)', () => {
    expect(
      pickStrictCandidate(text, candidates, {
        hint: 9_999,
        quoteBefore: 'Marker',
        text: 'Wert',
      }),
    ).toEqual({ from: 18, to: 22 })
  })
})

describe('call-site pins (source-level, like the Python doctrine pins)', () => {
  it('the model-edit path resolves strictly', () => {
    const source = readFileSync(
      fileURLToPath(new URL('./useEditorSuggestions.ts', import.meta.url)),
      'utf8',
    )
    const anchorFn = source.slice(source.indexOf('function createInstructionAnchor'))
      .slice(0, 1200)
    expect(anchorFn).toContain("mode: 'strict'")
  })

  it('the decoration wrapper delegates to the shared resolver', () => {
    const source = readFileSync(
      fileURLToPath(new URL('./core/MarkdownEditorSurface.tsx', import.meta.url)),
      'utf8',
    )
    const wrapper = source.slice(
      source.indexOf('function resolveSuggestionDecorationTarget'),
    ).slice(0, 900)
    expect(wrapper).toContain('resolveSuggestionTarget(editor, suggestion)')
    expect(wrapper).not.toContain('resolveAnchorRange(')
  })
})
