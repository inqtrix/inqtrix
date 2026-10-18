import { describe, expect, it } from 'vitest'

import {
  createDefaultFileLibrarySections,
  legacyFileSectionIdReplacements,
  LEGACY_FILE_SECTION_IDS,
  temporaryFileSectionId,
} from './sections'

describe('file section identity', () => {
  it('creates opaque unique ids and resolves temporary semantics by kind', () => {
    const sections = createDefaultFileLibrarySections('2026-01-01T00:00:00.000Z')
    expect(new Set(sections.map((section) => section.id)).size).toBe(3)
    expect(sections.every((section) => section.isBootstrapPlaceholder === true))
      .toBe(true)
    expect(sections.map((section) => section.id)).not.toEqual(
      expect.arrayContaining([...LEGACY_FILE_SECTION_IDS]),
    )
    expect(temporaryFileSectionId(sections)).toBe(
      sections.find((section) => section.kind === 'temporary')?.id,
    )
  })

  it('rekeys only a legacy id not owned by the current server scope', () => {
    const [legacyTemp, legacyLibrary] = LEGACY_FILE_SECTION_IDS
    const sections = {
      [legacyLibrary]: {
        createdAt: '2026-01-01T00:00:00.000Z',
        id: legacyLibrary,
        kind: 'custom' as const,
        title: 'Library',
        updatedAt: '2026-01-01T00:00:00.000Z',
      },
      [legacyTemp]: {
        createdAt: '2026-01-01T00:00:00.000Z',
        id: legacyTemp,
        kind: 'temporary' as const,
        title: 'Temporary',
        updatedAt: '2026-01-01T00:00:00.000Z',
      },
    }
    const replacements = legacyFileSectionIdReplacements(
      sections,
      new Set([legacyLibrary]),
    )
    expect(replacements[legacyLibrary]).toBeUndefined()
    expect(replacements[legacyTemp]).toMatch(/^file-section-/)
    expect(replacements[legacyTemp]).not.toBe(legacyTemp)
  })
})
