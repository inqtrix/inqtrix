import { describe, expect, it } from 'vitest'

import { literalTextFromMarkdown, markdownToPlainTextForEditor } from './anchoring'

describe('literalTextFromMarkdown: der Suchtext muss dem Editor-Text gleichen', () => {
  // Das Modell bekommt die Markdown-Projektion des Dokuments und antwortet in
  // derselben Sprache. Der Anker wird aber im EDITOR-Text gesucht, wo das
  // Zeichen bereits aufgeloest ist. Ohne diese Umkehrung suchte ein Lauf nach
  // "&lt;FIX&gt;", waehrend im Editor "<FIX>" steht -- kein Treffer, leerer
  // Anker, und der Wurf in der Edit-Schleife risse den ganzen Lauf ab.
  it('loest die Entitaeten auf, die der Serialisierer erzeugt', () => {
    expect(literalTextFromMarkdown('Bearbeitun &lt;FIX&gt; weiter'))
      .toBe('Bearbeitun <FIX> weiter')
  })

  // Der Serialisierer maskiert auf ZWEI Wegen. Wer nur die Entitaeten
  // aufloest, laesst die Haelfte der Faelle scheitern.
  it('loest auch die Backslash-Maskierung auf', () => {
    expect(literalTextFromMarkdown('Ein Platzhalter \\[Marke\\] im Satz.'))
      .toBe('Ein Platzhalter [Marke] im Satz.')
    expect(literalTextFromMarkdown('Der Wert snake\\_case')).toBe('Der Wert snake_case')
  })

  it('loest &amp; zuletzt auf, damit maskierte Entitaeten woertlich bleiben', () => {
    // "&amp;lt;" ist der maskierte Text "&lt;" -- er darf NICHT zu "<" werden.
    expect(literalTextFromMarkdown('Rot &amp;lt; Blau')).toBe('Rot &lt; Blau')
    expect(literalTextFromMarkdown('Meier &amp; Sohn')).toBe('Meier & Sohn')
  })

  it('laesst Markdown-Struktur stehen, statt sie zu deuten', () => {
    // Ein maskiertes '>' ist Text, kein Zitat. Genau deshalb liegt diese
    // Umkehrung NEBEN der Blockregel-Kette und nicht in ihr.
    expect(literalTextFromMarkdown('\\> kein Zitat')).toBe('> kein Zitat')
    expect(literalTextFromMarkdown('## Titel')).toBe('## Titel')
  })
})

describe('markdownToPlainTextForEditor bleibt unveraendert', () => {
  // Gegenprobe: die Umkehrung ist eine dritte Suchvariante NEBEN der
  // bestehenden Kette und fasst sie nicht an -- sonst faende sie alte Anker
  // nicht mehr.
  it('deutet Markdown-Struktur weiterhin', () => {
    expect(markdownToPlainTextForEditor('## Titel')).toBe('Titel')
    expect(markdownToPlainTextForEditor('**fett** und *kursiv*')).toBe('fett und kursiv')
    expect(markdownToPlainTextForEditor('[Text](https://example.test)')).toBe('Text')
    expect(markdownToPlainTextForEditor('- Punkt')).toBe('Punkt')
  })

  it('loest Entitaeten NICHT auf -- das ist Sache der literalen Variante', () => {
    expect(markdownToPlainTextForEditor('&lt;FIX&gt;')).toBe('&lt;FIX&gt;')
  })
})
