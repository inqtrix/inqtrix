import { describe, expect, it } from 'vitest'

import {
  normalizeEditorMarkdown,
  parseEditorMarkdown,
  sanitizeSerializedEditorMarkdown,
  serializeEditorJson,
} from '../src/markdown.js'

const SATZ = 'Ein Platzhalter [Marke] im Satz.'

function absatz(text: string) {
  return { type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text }] }] }
}

describe('Markdown: eigener Text gegen fremden Text', () => {
  // Der Serialisierer escapt jede literale eckige Klammer als "\[" / "\]".
  // Die LaTeX-Bequemlichkeitsregel liest genau diese Folge als
  // Formeltrenner. Wird sie auf die EIGENE Serialisierung angewandt, wird
  // aus einem gewoehnlichen Satz eine Formel, und der Absatz zerfaellt --
  // beim naechsten Speichern ist der Originaltext unwiederbringlich weg.
  it('liest die eigene Serialisierung unveraendert zurueck', () => {
    const markdown = serializeEditorJson(absatz(SATZ), 'final')
    expect(markdown).toBe('Ein Platzhalter \\[Marke\\] im Satz.')

    const zurueck = parseEditorMarkdown(markdown)
    const bloecke = (zurueck.content ?? []).map((node) => node.type)

    // Inhaltlich, nicht mechanisch: EIN Absatz mit demselben Satz.
    expect(bloecke).toEqual(['paragraph'])
    expect(serializeEditorJson(zurueck, 'final')).toBe(markdown)
  })

  it('bleibt auch ueber mehrere Runden stabil', () => {
    // Ein Fixpunkt, kein einmaliges Glueck: Speichern/Laden/Speichern darf
    // den Text nie weiterdrehen.
    let markdown = serializeEditorJson(absatz(SATZ), 'final')
    for (let runde = 0; runde < 3; runde += 1) {
      markdown = serializeEditorJson(parseEditorMarkdown(markdown), 'final')
    }
    expect(markdown).toBe('Ein Platzhalter \\[Marke\\] im Satz.')
    expect(markdown).not.toContain('$$')
  })

  it('nimmt Display-Mathematik aus FREMDEM Text weiterhin auf', () => {
    // Gegenprobe, die gruen bleiben muss: die Regel ist eine Einfuhr-Regel
    // fuer fremde Markdown (LLM-Ausgabe, Zwischenablage). Sie ersatzlos zu
    // streichen wuerde eine funktionierende Faehigkeit zerstoeren.
    const fremd = normalizeEditorMarkdown('Formel: \\[E = mc^2\\] Ende.')
    expect(fremd).toContain('$$')
    expect(fremd).toContain('E = mc^2')
  })

  it('trennt die beiden Pfade sichtbar voneinander', () => {
    // Derselbe Eingabetext, zwei Bedeutungen -- genau deshalb darf die
    // Einfuhr-Regel nicht im kanonischen Lesepfad sitzen.
    const eigen = sanitizeSerializedEditorMarkdown('Text \\[Marke\\] Ende.')
    const fremd = normalizeEditorMarkdown('Text \\[Marke\\] Ende.')
    expect(eigen).toBe('Text \\[Marke\\] Ende.')
    expect(fremd).toContain('$$')
    expect(eigen).not.toBe(fremd)
  })
})
