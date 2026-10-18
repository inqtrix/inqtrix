import { readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import { editorCopy } from '@/features/editor/editorCopy'

import { copyLeaves } from './shippedCopy'
import { translations } from './translations'

/** Every key path in `value`, joined with dots. Arrays and functions are
 * leaves: their identity as a key is what matters here, not their shape. */
function keyPaths(value: unknown, prefix = ''): string[] {
  if (
    typeof value !== 'object'
    || value === null
    || Array.isArray(value)
  ) {
    return prefix ? [prefix] : []
  }
  return Object.entries(value).flatMap(([key, child]) =>
    keyPaths(child, prefix ? `${prefix}.${key}` : key),
  )
}

function missingFrom(reference: unknown, candidate: unknown): string[] {
  const candidateKeys = new Set(keyPaths(candidate))
  return keyPaths(reference).filter((path) => !candidateKeys.has(path))
}

/** The `TranslationDictionary` union type already errors when a CONSUMED key
 * is missing from one locale, but it stays silent about a key that exists on
 * only one side and is never read — the kind that surfaces as an untranslated
 * string months later. These tests make the parity explicit. */
describe('locale parity', () => {
  it('every German key has an English counterpart', () => {
    expect(missingFrom(translations.de, translations.en)).toEqual([])
  })

  it('every English key has a German counterpart', () => {
    expect(missingFrom(translations.en, translations.de)).toEqual([])
  })

  it('every German editor string has an English counterpart', () => {
    expect(missingFrom(editorCopy.de, editorCopy.en)).toEqual([])
  })

  it('every English editor string has a German counterpart', () => {
    expect(missingFrom(editorCopy.en, editorCopy.de)).toEqual([])
  })
})

describe('German copy', () => {
  it('names a fallback path in German, never with the English word', () => {
    const source = readFileSync(path.resolve(import.meta.dirname, 'translations.ts'), 'utf8')
    const english = copyLeaves(source, 'i18n/translations.ts', 'de')
      .filter((leaf) => /fallback/i.test(leaf.fragments.join(' ')))
      .map((leaf) => leaf.key)
    expect(english).toEqual([])
  })

  it('names a rule a Regel, never with the English word', () => {
    // The agent desk said "Regeln" while the chat and the prompt library
    // said "Rules", side by side in one menu.
    const source = readFileSync(path.resolve(import.meta.dirname, 'translations.ts'), 'utf8')
    const english = copyLeaves(source, 'i18n/translations.ts', 'de')
      .filter((leaf) => /(^|[^A-Za-z])Rules?([^A-Za-z]|$)/.test(leaf.fragments.join(' ')))
      .map((leaf) => leaf.key)
    expect(english).toEqual([])
  })
})

describe('AI transparency strings', () => {
  it('states that Inqtrix is an AI system on both notice lists', () => {
    expect(translations.de.authLock.notices[0]).toContain('KI-System')
    expect(translations.en.authLock.notices[0]).toContain('AI system')
  })

  it('carries a first-interaction line and an export notice per locale', () => {
    for (const locale of [translations.de, translations.en]) {
      expect(locale.aiTransparency.firstInteraction.trim()).not.toBe('')
      expect(locale.aiTransparency.exportNotice.trim()).not.toBe('')
    }
  })
})
