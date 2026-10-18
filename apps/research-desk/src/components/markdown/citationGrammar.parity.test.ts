/**
 * Cross-language parity of the citation grammar: the frontend copy
 * reproduces every case of the shared fixture the Python grammar
 * (``src/inqtrix/citation_labels.py``) reproduces as well.
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import {
  canonicalizeCitationGroups,
  familyPredicate,
  malformedCitationBrackets,
} from './citationGrammar'

type Fixture = {
  cases: {
    name: string
    families: string
    text: string
    canonical: string
    malformed: string[]
  }[]
}

const fixture: Fixture = JSON.parse(
  readFileSync(
    fileURLToPath(
      new URL('../../../../../tests/fixtures/citation_label_parity.json', import.meta.url),
    ),
    'utf-8',
  ),
)

describe('citation grammar parity (shared fixture)', () => {
  it('covers a meaningful case count', () => {
    expect(fixture.cases.length).toBeGreaterThanOrEqual(25)
  })

  it.each(fixture.cases)('$name', (testCase) => {
    const isLabel = familyPredicate(testCase.families)
    expect(canonicalizeCitationGroups(testCase.text, isLabel)).toBe(testCase.canonical)
    expect(malformedCitationBrackets(testCase.text, isLabel)).toEqual(testCase.malformed)
  })
})
