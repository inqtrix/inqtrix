/**
 * Cross-language parity of the derived artifact file names.
 * The fixture is generated FROM the Python reference
 * (`src/inqtrix/agents/artifact_names.py`) and consumed here
 * byte-identically — the same pattern as the anchor-resolver fixture.
 */
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import { artifactSlug, assignArtifactFileNames } from './artifactNames'

type Fixture = {
  slug_cases: { title: string; slug: string }[]
  assign_cases: {
    items: [string, string][]
    expected: Record<string, string>
  }[]
}

const fixture: Fixture = JSON.parse(
  readFileSync(
    fileURLToPath(
      new URL(
        '../../../../../tests/fixtures/artifact_name_parity.json',
        import.meta.url,
      ),
    ),
    'utf-8',
  ),
)

describe('artifact name parity (shared fixture)', () => {
  it('covers a meaningful case count', () => {
    expect(fixture.slug_cases.length).toBeGreaterThanOrEqual(15)
    expect(fixture.assign_cases.length).toBeGreaterThanOrEqual(5)
  })

  it.each(fixture.slug_cases)('slugs %j like Python', (testCase) => {
    expect(artifactSlug(testCase.title)).toBe(testCase.slug)
  })

  it.each(fixture.assign_cases)('assigns %j like Python', (testCase) => {
    const got = assignArtifactFileNames(
      testCase.items.map(([artifactId, title]) => ({ artifactId, title })),
    )
    expect(got).toEqual(testCase.expected)
  })
})
