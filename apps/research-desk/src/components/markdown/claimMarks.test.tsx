import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'

import { TooltipProvider } from '@/components/ui/tooltip'
import { translations } from '@/i18n/translations'
import { linkifyCitationLabels } from './citationLinks'
import type { ClaimMarkView } from './claimMarks'
import { MarkdownRenderer } from './MarkdownRenderer'

vi.mock('@/i18n/LocaleProvider', () => ({
  useLocale: () => ({ locale: 'de', t: translations.de }),
}))
vi.mock('@/theme/ThemeProvider', () => ({
  useTheme: () => ({ contrastMode: 'standard', preset: 'standard', resolvedTheme: 'light' }),
}))

const NOTE = 'Nicht ausreichend belegt: W1 nennt keinen Prozentsatz.'

function mark(quote: string, note = NOTE): ClaimMarkView {
  return { quote, state: 'insufficient', note }
}

function render(markdown: string, marks: readonly ClaimMarkView[]): string {
  return renderToStaticMarkup(
    <TooltipProvider>
      <MarkdownRenderer
        claimMarks={marks}
        markdown={linkifyCitationLabels(markdown, (label) => /^[KWE]\d+$/.test(label))}
        variant="chat"
      />
    </TooltipProvider>,
  )
}

/** The text of every marked span, in order. */
function marked(html: string): string[] {
  return [...html.matchAll(/<span[^>]*data-claim-state="insufficient"[^>]*>([^<]*)<\/span>/g)]
    .map((match) => match[1])
}

describe('the claim marks in a rendered text', () => {
  it('marks the words of a statement and leaves its citation chip outside the mark', () => {
    const html = render(
      'Die Förderung ist neu geregelt [W2].\n\nDer Bonus beträgt 16 Prozent [W1]. Danach folgt mehr.',
      [mark('Der Bonus beträgt 16 Prozent [W1].')],
    )
    expect(marked(html)).toEqual(['Der Bonus beträgt 16 Prozent'])
  })

  it('marks a statement across emphasis and a list item', () => {
    const html = render(
      '- Der **Klimabonus** sinkt auf 16 Prozent [W1]\n- Rest bleibt [W2]',
      [mark('- Der **Klimabonus** sinkt auf 16 Prozent [W1]')],
    )
    expect(marked(html)).toEqual(['Der ', 'Klimabonus', ' sinkt auf 16 Prozent'])
  })

  it('marks every cell of a table row', () => {
    const html = render(
      '| Punkt | Wert |\n| --- | --- |\n| Grundförderung | 30 % [W1] |\n| Bonus | 16 % [W2] |',
      [mark('| Bonus | 16 % [W2] |')],
    )
    expect(marked(html)).toEqual(['Bonus', '16 %'])
  })

  it('carries the note and makes only the first part of a mark focusable', () => {
    const html = render(
      '- Der **Klimabonus** sinkt auf 16 Prozent [W1]',
      [mark('- Der **Klimabonus** sinkt auf 16 Prozent [W1]')],
    )
    expect([
      (html.match(/data-claim-note="Nicht ausreichend belegt: W1 nennt keinen Prozentsatz."/g) ?? []).length,
      (html.match(/tabindex="0"/g) ?? []).length,
    ]).toEqual([3, 1])
  })

  it('marks the same passage where it stands again in order', () => {
    const html = render(
      'Der Bonus beträgt 16 Prozent [W1].\n\nDie Förderung ist neu geregelt [W2].\n\nDer Bonus beträgt 16 Prozent [W1].',
      [mark('Der Bonus beträgt 16 Prozent [W1].'), mark('Der Bonus beträgt 16 Prozent [W1].')],
    )
    expect(marked(html)).toEqual(['Der Bonus beträgt 16 Prozent', 'Der Bonus beträgt 16 Prozent'])
  })

  it('leaves a citation inside a statement outside its mark', () => {
    const html = render(
      'Der Bonus [W1] beträgt 16 Prozent [W2].',
      [mark('Der Bonus [W1] beträgt 16 Prozent [W2].')],
    )
    expect(marked(html)).toEqual(['Der Bonus ', ' beträgt 16 Prozent'])
  })

  it('reads a link inside a statement by its text, not its address', () => {
    const html = render(
      'Die [Übersicht](https://example.org/news) nennt drei Funktionen [W1].',
      [mark('Die [Übersicht](https://example.org/news) nennt drei Funktionen [W1].')],
    )
    expect(marked(html)).toEqual(['Die ', 'Übersicht', ' nennt drei Funktionen'])
  })

  it('marks nothing for a passage the text does not hold', () => {
    expect(marked(render('Der Bonus beträgt 16 Prozent [W1].', [mark('Der Bonus beträgt 20 Prozent [W1].')])))
      .toEqual([])
  })
})
