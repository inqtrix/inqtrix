/**
 * Model descriptions follow the UI language like the rest of the picker.
 *
 * The server ships a German-only `card.description` in `models_catalog`
 * (`/health` is unauthenticated and knows no user locale). Rendered raw,
 * it gives a picker whose header, tiers and group labels are English while
 * every model line stays German.
 */
import { describe, expect, it } from 'vitest'

import { translations } from '@/i18n/translations'
import { modelCardDescription } from './modelCard'

const CARD = {
  id: 'gpt-5.4',
  description: 'Stärkste Allzweck-Qualität für Recherche und Synthese.',
}

describe('modelCardDescription', () => {
  it('reads English when the UI is English', () => {
    const text = modelCardDescription(CARD, translations.en.chat.modelDescriptions)
    expect(text).toBe('Strongest general-purpose quality for research and synthesis.')
    expect(text).not.toContain('Qualität')
  })

  it('reads German when the UI is German', () => {
    expect(modelCardDescription(CARD, translations.de.chat.modelDescriptions)).toBe(
      'Stärkste Allzweck-Qualität für Recherche und Synthese.',
    )
  })

  it('falls back to the server text for a model the table does not know', () => {
    // A deployment can run a model this build has never heard of; showing its
    // server description beats showing nothing.
    const unknown = { id: 'some-new-model-2027', description: 'Provider blurb.' }
    for (const locale of ['de', 'en'] as const) {
      expect(
        modelCardDescription(unknown, translations[locale].chat.modelDescriptions),
      ).toBe('Provider blurb.')
    }
  })

  it('covers every model the server catalog ships', () => {
    // Drift guard: a model added to model_cards.py without a translation
    // silently falls back to German for English readers.
    const de = Object.keys(translations.de.chat.modelDescriptions)
    const en = Object.keys(translations.en.chat.modelDescriptions)
    expect(new Set(de)).toEqual(new Set(en))
    expect(de).toContain('claude-opus-4-8')
    expect(de).toContain('gemini-3.1-pro')
    expect(de.length).toBeGreaterThanOrEqual(19)
  })
})
