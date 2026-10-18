import { describe, expect, it } from 'vitest'
import {
  documentDiffPlan,
  suggestionDiffPlan,
  suggestionDiffSegments,
  type SuggestionDiffSegment,
} from './suggestionDiff'

function rebuilt(segments: SuggestionDiffSegment[], side: 'delete' | 'insert'): string {
  return segments
    .filter((segment) => segment.type === 'equal' || segment.type === side)
    .map((segment) => segment.text)
    .join('')
}

function changes(segments: SuggestionDiffSegment[]): SuggestionDiffSegment[] {
  return segments.filter((segment) => segment.type !== 'equal')
}

describe('suggestionDiffSegments', () => {
  it.each([
    ['Budget 18.000 Euro', 'Budget 21.500 Euro', '18.000', '21.500'],
    ['Summe 21.500,00 €', 'Summe 22.000,50 €', '21.500,00', '22.000,50'],
    ['Frist 1,5 Jahre', 'Frist 2,5 Jahre', '1,5', '2,5'],
    ['Total $1,250.75', 'Total $1,300.00', '1,250.75', '1,300.00'],
    ['Beginn 14:30 Uhr', 'Beginn 15:00 Uhr', '14:30', '15:00'],
    ['Stand 01.10.2026', 'Stand 14.10.2026', '01.10.2026', '14.10.2026'],
    ['Zeitraum 2024–2025', 'Zeitraum 2025–2026', '2024–2025', '2025–2026'],
    ['Preis 18\u202f000 CHF', 'Preis 21\u202f500 CHF', '18\u202f000', '21\u202f500'],
  ])('shows a changed number whole: %s -> %s', (original, proposed, removed, added) => {
    const segments = suggestionDiffSegments(original, proposed)

    expect(changes(segments)).toEqual([
      { text: removed, type: 'delete' },
      { text: added, type: 'insert' },
    ])
    expect(rebuilt(segments, 'delete')).toBe(original)
    expect(rebuilt(segments, 'insert')).toBe(proposed)
  })

  it('reads changes that only punctuation separates as one replacement', () => {
    const segments = suggestionDiffSegments('Status A-B bleibt', 'Status C-D bleibt')

    expect(changes(segments)).toEqual([
      { text: 'A-B', type: 'delete' },
      { text: 'C-D', type: 'insert' },
    ])
    expect(rebuilt(segments, 'delete')).toBe('Status A-B bleibt')
    expect(rebuilt(segments, 'insert')).toBe('Status C-D bleibt')
  })

  it.each([
    ['a changed word', 'Die Kosten steigen', 'Die Kostet steigen', [['Kosten', 'Kostet']]],
    ['a punctuation-only change', 'Das ist das Ende.', 'Das ist das Ende!', [['.', '!']]],
    ['numbers that a plain space separates', 'Werte 3 4 5', 'Werte 3 4 6', [['5', '6']]],
    ['a number before a sentence end', 'im Jahr 2026. Danach', 'im Jahr 2027. Danach', [['2026', '2027']]],
    ['a number after an abbreviation', 'gemäß Art.5 DSGVO', 'gemäß Art.6 DSGVO', [['5', '6']]],
    ['two words that a space separates', 'Wir prüfen heute', 'Sie prüften heute', [['Wir', 'Sie'], ['prüfen', 'prüften']]],
  ])('keeps %s as it was', (_case, original, proposed, pairs) => {
    const segments = suggestionDiffSegments(original, proposed)

    expect(changes(segments)).toEqual(
      pairs.flatMap(([removed, added]) => [
        { text: removed, type: 'delete' },
        { text: added, type: 'insert' },
      ]),
    )
    expect(rebuilt(segments, 'delete')).toBe(original)
    expect(rebuilt(segments, 'insert')).toBe(proposed)
  })
})

describe('documentDiffPlan', () => {
  it('groups adjacent deleted and inserted markdown lines as one replacement', () => {
    const plan = documentDiffPlan(
      'Intro\n\nThe claim is false.\n\nEnd',
      'Intro\n\nThe claim is partly false.\n\nEnd',
    )

    expect(plan.map((block) => block.kind)).toEqual(['equal', 'replace', 'equal'])
    const replacement = plan[1]
    expect(replacement.kind).toBe('replace')
    if (replacement.kind !== 'replace') return
    expect(replacement.beforeMarkdown).toBe('The claim is false.\n')
    expect(replacement.afterMarkdown).toBe('The claim is partly false.\n')
  })

  it('uses inline tokens for simple prose replacements', () => {
    const plan = documentDiffPlan(
      'Google Gemini remains active and available.\n',
      'Google Gemini remains active, expanded, and available.\n',
    )

    const replacement = plan[0]
    expect(replacement.kind).toBe('replace')
    if (replacement.kind !== 'replace') return
    expect(replacement.inlineSegments).not.toBeNull()
    expect(replacement.inlineSegments?.some((segment) => segment.type === 'insert' && segment.text.includes('expanded'))).toBe(true)
  })

  it('keeps structural markdown replacements in a compact rendered fallback', () => {
    const plan = documentDiffPlan(
      '- Old evidence item\n',
      '- New evidence item\n',
    )

    const replacement = plan[0]
    expect(replacement.kind).toBe('replace')
    if (replacement.kind !== 'replace') return
    expect(replacement.inlineSegments).toBeNull()
  })

  it('does not inline changed markdown link syntax', () => {
    const plan = documentDiffPlan(
      'Evidence points to [E1](https://example.com/old).\n',
      'Evidence points to [E2](https://example.com/new).\n',
    )

    const replacement = plan[0]
    expect(replacement.kind).toBe('replace')
    if (replacement.kind !== 'replace') return
    expect(replacement.inlineSegments).toBeNull()
  })
})

describe('suggestionDiffPlan', () => {
  it('keeps the existing model suggestion diff path inline for short prose', () => {
    const plan = suggestionDiffPlan('Use short text.', 'Use sharper short text.')

    expect(plan.display).toBe('inline')
    expect(plan.reviewSurface).toBe('panel')
  })
})
