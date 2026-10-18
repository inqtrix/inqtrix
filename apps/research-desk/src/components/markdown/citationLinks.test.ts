import { describe, expect, it } from 'vitest'

import { citationLabelFromHref, linkifyCitationLabels } from './citationLinks'

const isAgentLabel = (label: string) => /^[KW]\d+$/.test(label)

describe('generic citation links', () => {
  it('linkifies mixed known web and knowledge labels, including adjacent runs', () => {
    const known = new Set(['K2', 'W1'])
    expect(linkifyCitationLabels(
      'Belegt durch [W1] und K2W1.',
      isAgentLabel,
      known,
    )).toBe(
      'Belegt durch [W1](#kref-W1) und [K2](#kref-K2)[W1](#kref-W1).',
    )
  })

  it('leaves unknown bare labels and unrelated label families untouched', () => {
    expect(linkifyCitationLabels(
      'K9 und [Q1] bleiben Text.',
      isAgentLabel,
      new Set(['K1']),
    )).toBe('K9 und [Q1] bleiben Text.')
  })

  it('can require bracketed labels to exist in the caller ledger', () => {
    expect(linkifyCitationLabels(
      '[W1] [W9]',
      isAgentLabel,
      new Set(['W1']),
      { requireKnownBracketed: true },
    )).toBe('[W1](#kref-W1) [W9]')
  })

  it('extracts only labels accepted by the caller', () => {
    expect(citationLabelFromHref('#kref-W3', isAgentLabel)).toBe('W3')
    expect(citationLabelFromHref('#kref-Q3', isAgentLabel)).toBeNull()
  })
})

describe('the citation grammar in the rendered text', () => {
  const known = new Set(['K2', 'W7'])

  it('chips every member of a grouped bracket', () => {
    expect(linkifyCitationLabels('Satz.[K2, W7]', isAgentLabel, known)).toBe(
      'Satz.[K2](#kref-K2) [W7](#kref-W7)',
    )
  })

  it('never chips a label inside code', () => {
    const text = 'Code `[K2, W7]` und `K2`\n```\n[W7] K2\n```\n'
    expect(linkifyCitationLabels(text, isAgentLabel, known)).toBe(text)
  })

  it('leaves a marker and an unreadable bracket as written', () => {
    const text = 'Marker [unbelegt: K2], Spanne [K2–W7], Gruppe als Link [K2, W7](https://x).'
    expect(linkifyCitationLabels(text, isAgentLabel, known)).toBe(text)
  })

  it('leaves a marker used as link text as written', () => {
    const text = 'Markiert [unbelegt: K2, W7](https://x).'
    expect(linkifyCitationLabels(text, isAgentLabel, known)).toBe(text)
  })

  it('chips a label right after a link destination', () => {
    expect(linkifyCitationLabels('Laut [Bericht](https://a.org/x)[W7].', isAgentLabel, known)).toBe(
      'Laut [Bericht](https://a.org/x)[W7](#kref-W7).',
    )
  })

  it('chips a label right after an exclamation mark, never an image', () => {
    // `![W7](#kref-W7)` would render as an image: the mark stays text.
    expect(linkifyCitationLabels('Das ist falsch![W7] Weiter.', isAgentLabel, known)).toBe(
      'Das ist falsch\\![W7](#kref-W7) Weiter.',
    )
    expect(linkifyCitationLabels('Wichtig!K2 und !K2W7', isAgentLabel, known)).toBe(
      'Wichtig\\![K2](#kref-K2) und \\![K2](#kref-K2)[W7](#kref-W7)',
    )
    // An escaped mark is text already; an escaped backslash escapes nothing.
    expect(linkifyCitationLabels('Schon \\![W7] und \\\\![W7]', isAgentLabel, known)).toBe(
      'Schon \\![W7](#kref-W7) und \\\\\\![W7](#kref-W7)',
    )
  })

  it('reads an escaped bracket as literal text as a whole', () => {
    // A known bare label inside an escaped group would otherwise be chipped
    // on its own while its neighbour stays text: `\[K2, [W7](#kref-W7)\]`.
    for (const text of [
      'Satz \\[K2, W7\\].',
      'Satz \\[K2; W7\\].',
      'Satz \\[K2\\] und \\[W7\\].',
      'Marker \\[unbelegt: K2\\], Spanne \\[K2–W7\\], Join \\[K2 und W7\\].',
      'Offen \\[K2] zu.',
    ]) {
      expect(linkifyCitationLabels(text, isAgentLabel, known), text).toBe(text)
    }
  })

  it('still chips an unescaped label beside an escaped bracket', () => {
    expect(linkifyCitationLabels('[K2] und \\[W7\\] und W7', isAgentLabel, known)).toBe(
      '[K2](#kref-K2) und \\[W7\\] und [W7](#kref-W7)',
    )
    // An escaped backslash escapes nothing after it: the bracket is real.
    expect(linkifyCitationLabels('Pfad \\\\[K2] Ende', isAgentLabel, known)).toBe(
      'Pfad \\\\[K2](#kref-K2) Ende',
    )
  })
})
