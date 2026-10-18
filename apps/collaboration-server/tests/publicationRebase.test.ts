import type { JSONContent } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { serializeEditorJson } from '@inqtrix/editor-schema'
import { describe, expect, it } from 'vitest'

import { CollaborationError } from '../src/errors'
import {
  MAX_REBASE_LEAF_EDITS,
  rebasePublication,
  replacesBaseContent,
} from '../src/publicationRebase'
import { parsePublicationBase } from '../src/suggestionPublisher'

// The publisher's own reader, so that the base, the target and the current
// state share one schema as they do in the service.
const read = (markdown: string): ProseMirrorNode => parsePublicationBase(markdown)
const publisherSchema = read('x').type.schema
const fromJson = (content: JSONContent): ProseMirrorNode => publisherSchema.nodeFromJSON(content)

type Outcome =
  | { placed: ProseMirrorNode }
  | { refused: string }
  /** The placement would have replaced other content than the AI replaced
   * in its base, and was stopped by the content check. */
  | { inconsistent: string }

function place(base: ProseMirrorNode, target: ProseMirrorNode, current: ProseMirrorNode): Outcome {
  try {
    return { placed: rebasePublication(base, target, current).target }
  } catch (error) {
    if (error instanceof CollaborationError && error.reason === 'base_changed') {
      return { refused: error.suggestionReason ?? 'unknown' }
    }
    if (error instanceof CollaborationError && error.reason === 'internal_consistency') {
      return { inconsistent: error.suggestionReason ?? 'unknown' }
    }
    throw error
  }
}

function placedMarkdown(outcome: Outcome): string {
  if (!('placed' in outcome)) throw new Error(`not placed: ${JSON.stringify(outcome)}`)
  return serializeEditorJson(outcome.placed.toJSON(), 'final').trim()
}

/** Markdown as the editor writes it back. */
const canonical = (markdown: string): string => serializeEditorJson(read(markdown).toJSON(), 'final').trim()

const SENTENCE = 'Der Assistent ersetzt hier ein Wort.'
const REWRITTEN = 'Der Assistent tauscht hier ein Wort.'

function shared(
  sentence: string,
  { heading = 'Abschnitt A', notesB = 'Notizen B:', notesC = 'Notizen C:' } = {},
): string {
  return `# ${heading}\n\n${sentence}\n\n${notesB}\n\n## Abschnitt C\n\n${notesC}`
}

describe('placing an AI publication onto the state other writers moved on', () => {
  const base = read(shared(SENTENCE))
  const target = read(shared(REWRITTEN))

  it.each([
    ['another writer typed in another paragraph below', shared(SENTENCE, { notesB: 'Notizen B: MARKE1 MARKE2' }), shared(REWRITTEN, { notesB: 'Notizen B: MARKE1 MARKE2' })],
    ['another writer typed above, which moves every position', `# Abschnitt A neu\n\nganz neu\n\n${SENTENCE}\n\nNotizen B:\n\n## Abschnitt C\n\nNotizen C:`, `# Abschnitt A neu\n\nganz neu\n\n${REWRITTEN}\n\nNotizen B:\n\n## Abschnitt C\n\nNotizen C:`],
    ['two writers typed above and below', shared(SENTENCE, { heading: 'Abschnitt A!', notesB: 'Notizen B: x', notesC: 'Notizen C: y' }), shared(REWRITTEN, { heading: 'Abschnitt A!', notesB: 'Notizen B: x', notesC: 'Notizen C: y' })],
    ['another writer typed after the AI sentence in its paragraph', shared(`${SENTENCE} Zusatz`), shared(`${REWRITTEN} Zusatz`)],
    ['another writer typed before the AI sentence in its paragraph', shared(`Vorab: ${SENTENCE}`), shared(`Vorab: ${REWRITTEN}`)],
    ['another writer changed another word of the AI sentence', shared('Der Assistent ersetzt dort ein Wort.'), shared('Der Assistent tauscht dort ein Wort.')],
    ['another writer typed right after the letter the AI kept at the end of the word', shared('Der Assistent ersetztX hier ein Wort.'), shared('Der Assistent tauschtX hier ein Wort.')],
    ['another writer made another word of the AI paragraph bold', shared('Der **Assistent** ersetzt hier ein Wort.'), shared('Der **Assistent** tauscht hier ein Wort.')],
    ['another writer deleted text right before the AI section without touching it', shared('Assistent ersetzt hier ein Wort.'), shared('Assistent tauscht hier ein Wort.')],
    ['another writer typed in the AI paragraph and in the paragraph after it', shared('Der Assistent ersetzt hier ein Wort. Zusatz', { notesB: 'Notizen B: MARKE1' }), shared('Der Assistent tauscht hier ein Wort. Zusatz', { notesB: 'Notizen B: MARKE1' })],
  ])('places the AI section when %s', (_name, current, expected) => {
    expect(placedMarkdown(place(base, target, read(current)))).toBe(canonical(expected))
  })

  it.each([
    ['another writer typed exactly at the start of the AI section', shared('Der Assistent Xersetzt hier ein Wort.'), 'hunk_touched'],
    ['another writer typed inside the AI section', shared('Der Assistent erXsetzt hier ein Wort.'), 'hunk_touched'],
    ['another writer deleted the AI section', shared('Der Assistent hier ein Wort.'), 'hunk_touched'],
    ['another writer deleted the AI paragraph', '# Abschnitt A\n\nNotizen B:\n\n## Abschnitt C\n\nNotizen C:', 'hunk_touched'],
    ['two writers typed on both sides of the AI section in its paragraph', shared(`V ${SENTENCE} N`), 'hunk_touched'],
    ['another writer turned the AI paragraph into a heading', `# Abschnitt A\n\n## ${SENTENCE}\n\nNotizen B:\n\n## Abschnitt C\n\nNotizen C:`, 'hunk_touched'],
    ['another writer moved the AI paragraph into a list', `# Abschnitt A\n\n- ${SENTENCE}\n\nNotizen B:\n\n## Abschnitt C\n\nNotizen C:`, 'hunk_touched'],
  ])('refuses when %s', (_name, current, reason) => {
    expect(place(base, target, read(current))).toEqual({ refused: reason })
  })

  it('places the AI section when another writer typed the AI word again away from it', () => {
    expect(placedMarkdown(place(
      read('Wir prüfen die Daten.'),
      read('Wir prüfen diese Daten.'),
      read('Wir prüfen die Daten die.'),
    ))).toBe(canonical('Wir prüfen diese Daten die.'))
  })

  it('publishes the target itself when nobody changed anything since the base', () => {
    const placement = rebasePublication(base, target, read(shared(SENTENCE)))
    expect(placement.outcome).toBe('direct')
    expect(placement.target).toBe(target)
  })
})

describe('placing a new AI paragraph between two blocks', () => {
  const base = read('Bei Fragen bitte melden.\n\n## Schreibzone\n\nNotizen A:')
  const target = read('Bei Fragen bitte melden.\n\nDie neuen Geräte werden ab Montag ausgegeben.\n\n## Schreibzone\n\nNotizen A:')

  it.each([
    ['another writer typed below the heading after it', 'Bei Fragen bitte melden.\n\n## Schreibzone\n\nNotizen A: MARKE1', 'Bei Fragen bitte melden.\n\nDie neuen Geräte werden ab Montag ausgegeben.\n\n## Schreibzone\n\nNotizen A: MARKE1'],
    ['another writer typed at the end of the paragraph before it', 'Bei Fragen bitte melden. Danke\n\n## Schreibzone\n\nNotizen A:', 'Bei Fragen bitte melden. Danke\n\nDie neuen Geräte werden ab Montag ausgegeben.\n\n## Schreibzone\n\nNotizen A:'],
    ['another writer typed at the start of the heading after it', 'Bei Fragen bitte melden.\n\n## NEU Schreibzone\n\nNotizen A:', 'Bei Fragen bitte melden.\n\nDie neuen Geräte werden ab Montag ausgegeben.\n\n## NEU Schreibzone\n\nNotizen A:'],
  ])('places it when %s', (_name, current, expected) => {
    expect(placedMarkdown(place(base, target, read(current)))).toBe(canonical(expected))
  })

  it('refuses when another writer inserted a paragraph at the same boundary before a heading', () => {
    expect(place(base, target, read('Bei Fragen bitte melden.\n\nFremder Absatz\n\n## Schreibzone\n\nNotizen A:')))
      .toEqual({ refused: 'hunk_touched' })
  })

  it('places it after a paragraph another writer inserted at the same boundary before a paragraph, losing no text', () => {
    // The difference finds a paragraph inserted before another paragraph at
    // the start of that paragraph, so the foreign paragraph that arrived at
    // the same boundary comes first: a tie, as two concurrent insertions at
    // one position are in operational transform.
    const paragraphBase = read('Erster Absatz.\n\nLetzter Absatz.')
    const paragraphTarget = read('Erster Absatz.\n\nNeuer Absatz.\n\nLetzter Absatz.')
    expect(placedMarkdown(place(
      paragraphBase,
      paragraphTarget,
      read('Erster Absatz.\n\nFremder Absatz.\n\nLetzter Absatz.'),
    ))).toBe(canonical('Erster Absatz.\n\nFremder Absatz.\n\nNeuer Absatz.\n\nLetzter Absatz.'))
  })
})

describe('placing an AI change in lists, tables, tasks, code and next to formulas', () => {
  it('places a list item change when another writer changed another item', () => {
    expect(placedMarkdown(place(
      read('- Punkt eins\n- Punkt zwei\n- Punkt drei'),
      read('- Punkt Eins\n- Punkt zwei\n- Punkt drei'),
      read('- Punkt eins\n- Punkt zwei\n- Punkt drei!'),
    ))).toBe(canonical('- Punkt Eins\n- Punkt zwei\n- Punkt drei!'))
  })

  it('places a list item change when another writer split the list after another item', () => {
    const item = (text: string): JSONContent => ({
      type: 'listItem',
      content: [{ type: 'paragraph', content: [{ type: 'text', text }] }],
    })
    const list = (...texts: string[]): JSONContent => ({ type: 'bulletList', content: texts.map(item) })
    const placed = place(
      fromJson({ type: 'doc', content: [list('Punkt eins', 'Punkt zwei', 'Punkt drei')] }),
      fromJson({ type: 'doc', content: [list('Punkt Eins', 'Punkt zwei', 'Punkt drei')] }),
      fromJson({ type: 'doc', content: [list('Punkt eins', 'Punkt zwei'), list('Punkt drei')] }),
    )
    if (!('placed' in placed)) throw new Error(JSON.stringify(placed))
    expect(placed.placed.eq(fromJson({
      type: 'doc',
      content: [list('Punkt Eins', 'Punkt zwei'), list('Punkt drei')],
    }))).toBe(true)
  })

  it('refuses when another writer turned the AI list item into a paragraph', () => {
    expect(place(
      read('- Punkt eins\n- Punkt zwei\n- Punkt drei'),
      read('- Punkt Eins\n- Punkt zwei\n- Punkt drei'),
      read('Punkt eins\n\n- Punkt zwei\n- Punkt drei'),
    )).toEqual({ refused: 'hunk_touched' })
  })

  it('places a change in one table cell when another writer typed in another cell', () => {
    const table = (first: string, second: string) => (
      `| Kopf | Wert |\n| --- | --- |\n| ${first} | ${second} |\n| Zeile | zwei |`
    )
    expect(placedMarkdown(place(
      read(table('alt', 'eins')),
      read(table('neu', 'eins')),
      read(table('alt', 'eins MARKE1')),
    ))).toBe(canonical(table('neu', 'eins MARKE1')))
  })

  it('places a change in a task item when another writer checked another task', () => {
    expect(placedMarkdown(place(
      read('- [ ] Aufgabe eins\n- [ ] Aufgabe zwei'),
      read('- [ ] Aufgabe Eins\n- [ ] Aufgabe zwei'),
      read('- [ ] Aufgabe eins\n- [x] Aufgabe zwei'),
    ))).toBe(canonical('- [ ] Aufgabe Eins\n- [x] Aufgabe zwei'))
  })

  it('refuses when another writer checked the task the AI changed', () => {
    expect(place(
      read('- [ ] Aufgabe eins\n- [ ] Aufgabe zwei'),
      read('- [ ] Aufgabe Eins\n- [ ] Aufgabe zwei'),
      read('- [x] Aufgabe eins\n- [ ] Aufgabe zwei'),
    )).toEqual({ refused: 'hunk_touched' })
  })

  it('places a change in a code block when another writer typed in another line of it', () => {
    expect(placedMarkdown(place(
      read('```\nlet a = 1\nlet b = 2\n```'),
      read('```\nlet a = 3\nlet b = 2\n```'),
      read('```\nlet a = 1\nlet b = 2 + 0\n```'),
    ))).toBe(canonical('```\nlet a = 3\nlet b = 2 + 0\n```'))
  })

  it('places a change next to an inline formula another writer kept, and around a block formula', () => {
    expect(placedMarkdown(place(
      read('Formel $x + 1$ und Text.\n\n$$\ny = 2\n$$\n\nEnde.'),
      read('Formel $x + 1$ und mehr Text.\n\n$$\ny = 2\n$$\n\nEnde.'),
      read('Formel $x + 1$ und Text.\n\n$$\ny = 2\n$$\n\nEnde MARKE1.'),
    ))).toBe(canonical('Formel $x + 1$ und mehr Text.\n\n$$\ny = 2\n$$\n\nEnde MARKE1.'))
  })
})

describe('refusing a placement another reading of the changes since would put elsewhere', () => {
  // Without an operation log, the same current text can come from more than
  // one change of the other writers. When one of those readings touches the
  // AI section or moves it, the publication is refused instead of placed by
  // the reading the difference happened to find first.
  it.each([
    ['another writer typed the AI word again right before it', 'Wir prüfen die Daten.', 'Wir prüfen diese Daten.', 'Wir prüfen die die Daten.'],
    ['another writer typed a copy of the AI section right before it', 'ca b', 'cZ b', 'ca a b'],
    ['another writer deleted a repeated word that reads as removed right after the AI section', 'Das ist gut gut.', 'Das war gut gut.', 'Das ist gut.'],
  ])('refuses when %s', (_name, base, target, current) => {
    expect(place(read(base), read(target), read(current))).toEqual({ refused: 'hunk_touched' })
  })

  it.each([
    ['another writer swapped the AI paragraph with the next one and edited both', 'Intro: alpha\n\nIntro: beta', 'Einleitung: alpha\n\nIntro: beta', 'Intro: beta!\n\nIntro: alpha?'],
    ['another writer moved a paragraph above the AI paragraph and edited both', 'Punkt: eins\n\nPunkt: zwei\n\nEnde', 'Satz: eins\n\nPunkt: zwei\n\nEnde', 'Punkt: zwei!\n\nPunkt: eins?\n\nEnde'],
    ['another writer edited two identical paragraphs, one of them the AI paragraph', 'Gleich.\n\nGleich.', 'Gleich.\n\nAnders.', 'Gleich. A\n\nGleich. B'],
    ['another writer moved the AI paragraph past two others and edited all three', 'Punkt: eins\n\nPunkt: zwei\n\nPunkt: drei', 'Satz: eins\n\nPunkt: zwei\n\nPunkt: drei', 'Punkt: zwei!\n\nPunkt: drei?\n\nPunkt: eins.'],
    // The paragraph in between stays, so each moved paragraph is found
    // changed on its own: the swap still reads as both edited in place.
    ['another writer swapped the AI paragraph with one past an unchanged paragraph and edited both', 'Punkt: eins\n\nMitte bleibt.\n\nPunkt: zwei', 'Satz: eins\n\nMitte bleibt.\n\nPunkt: zwei', 'Punkt: zwei!\n\nMitte bleibt.\n\nPunkt: eins?'],
  ])('refuses when %s', (_name, base, target, current) => {
    expect(place(read(base), read(target), read(current))).toEqual({ refused: 'base_ambiguous' })
  })

  it.each([
    // Swapped, the heading and the paragraph would change their type: no
    // smaller reading than both edited in place.
    ['another writer exchanged the texts of a heading and the AI paragraph', '## Intro: beta\n\nIntro: alpha', '## Intro: beta\n\nEinleitung: alpha', '## Intro: alpha?\n\nIntro: beta!', '## Intro: alpha?\n\nEinleitung: beta!'],
    ['another writer swapped and edited two other paragraphs', 'Ziel: eins\n\nIntro: alpha\n\nIntro: beta', 'Zweck: eins\n\nIntro: alpha\n\nIntro: beta', 'Ziel: eins\n\nIntro: beta!\n\nIntro: alpha?', 'Zweck: eins\n\nIntro: beta!\n\nIntro: alpha?'],
    // " gut" typed again could sit before or after "gut", but it is four
    // positions either way: edited in place, the two paragraphs change by
    // five positions together, swapped by thirteen.
    ['another writer repeated a word in the AI paragraph and edited a neighbour that starts alike', 'Plan: sehr gut\n\nPlan: sehr', 'Ziel: sehr gut\n\nPlan: sehr', 'Plan: sehr gut gut\n\nPlan: sehr!', 'Ziel: sehr gut gut\n\nPlan: sehr!'],
    // Edited in place, the two paragraphs change by two positions, swapped
    // past the paragraph between them by eighteen.
    ['another writer edited the AI paragraph and one alike past an unchanged paragraph', 'Punkt: eins\n\nMitte bleibt.\n\nPunkt: zwei', 'Satz: eins\n\nMitte bleibt.\n\nPunkt: zwei', 'Punkt: eins?\n\nMitte bleibt.\n\nPunkt: zwei!', 'Satz: eins?\n\nMitte bleibt.\n\nPunkt: zwei!'],
  ])('places the AI section when %s', (_name, base, target, current, expected) => {
    expect(placedMarkdown(place(read(base), read(target), read(current)))).toBe(canonical(expected))
  })
})

describe('placing an AI block at the edges of the document', () => {
  it('places a heading the AI inserted at the very start when another writer typed below', () => {
    expect(placedMarkdown(place(
      read('Erster Absatz.\n\nLetzter Absatz.'),
      read('# Titel\n\nErster Absatz.\n\nLetzter Absatz.'),
      read('Erster Absatz.\n\nLetzter Absatz. MARKE1'),
    ))).toBe(canonical('# Titel\n\nErster Absatz.\n\nLetzter Absatz. MARKE1'))
  })

  it('places a paragraph the AI appended at the very end when another writer typed above', () => {
    expect(placedMarkdown(place(
      read('Erster Absatz.\n\nLetzter Absatz.'),
      read('Erster Absatz.\n\nLetzter Absatz.\n\nNeuer Schluss.'),
      read('MARKE1 Erster Absatz.\n\nLetzter Absatz.'),
    ))).toBe(canonical('MARKE1 Erster Absatz.\n\nLetzter Absatz.\n\nNeuer Schluss.'))
  })
})

describe('placing an AI change among identical paragraphs', () => {
  const twins = read('X\n\nY\n\nX')
  const twinsTarget = read('X\n\nY\n\nX2')

  it('places it when an identical paragraph was inserted far above', () => {
    expect(placedMarkdown(place(twins, twinsTarget, read('X\n\nX\n\nY\n\nX')))).toBe(canonical('X\n\nX\n\nY\n\nX2'))
  })

  it('refuses when a twin was inserted right before the AI paragraph', () => {
    expect(place(twins, twinsTarget, read('X\n\nY\n\nX\n\nX'))).toEqual({ refused: 'base_ambiguous' })
  })

  it('refuses when the AI paragraph was deleted and a twin of it remains', () => {
    expect(place(
      read('X\n\nY\n\nX\n\nZ'),
      read('X\n\nY\n\nX2\n\nZ'),
      read('X\n\nY\n\nZ'),
    )).toEqual({ refused: 'hunk_touched' })
  })
})

describe('the bounds of a placement', () => {
  it('refuses when more leaf blocks changed than the alignment covers', () => {
    const paragraphs = (count: number, prefix: string) => (
      Array.from({ length: count }, (_value, index) => `${prefix} ${index}`).join('\n\n')
    )
    const base = read(`${paragraphs(5, 'Basis')}\n\nDer Satz.`)
    const target = read(`${paragraphs(5, 'Basis')}\n\nDer neue Satz.`)
    const current = read(`${paragraphs(MAX_REBASE_LEAF_EDITS + 1, 'Fremd')}\n\n${paragraphs(5, 'Basis')}\n\nDer Satz.`)
    expect(place(base, target, current)).toEqual({ refused: 'base_too_far' })
  })

  it('compares the replaced content with the base content, positions included', () => {
    const base = read(shared(SENTENCE))
    const current = read(shared(SENTENCE, { heading: 'Abschnitt A neu' }))
    const at = SENTENCE.indexOf('ersetz')
    // "# Abschnitt A" is 13 positions long with its tokens; the heading in the
    // current state is 4 letters longer.
    const baseFrom = 13 + 1 + at
    expect(base.textBetween(baseFrom, baseFrom + 6)).toBe('ersetz')
    expect(replacesBaseContent(current, baseFrom + 4, baseFrom + 10, base, baseFrom, baseFrom + 6)).toBe(true)
    expect(replacesBaseContent(current, baseFrom + 5, baseFrom + 11, base, baseFrom, baseFrom + 6)).toBe(false)
    expect(replacesBaseContent(current, baseFrom + 3, baseFrom + 9, base, baseFrom, baseFrom + 6)).toBe(false)
    expect(replacesBaseContent(current, -1, 2, base, baseFrom, baseFrom + 6)).toBe(false)
  })

  it('compares one position on each side, so a pure insertion placed off by one is caught', () => {
    // A new paragraph replaces nothing: without the positions around it,
    // every insertion point would hold "the same" empty content.
    const document = read('Erster Absatz.\n\nLetzter Absatz.')
    const boundary = document.child(0).nodeSize
    expect(replacesBaseContent(document, boundary, boundary, document, boundary, boundary)).toBe(true)
    expect(replacesBaseContent(document, boundary + 1, boundary + 1, document, boundary, boundary)).toBe(false)
    expect(replacesBaseContent(document, boundary - 1, boundary - 1, document, boundary, boundary)).toBe(false)
    // In "Haar", the letter before tells "H|aar" from "Ha|ar", and the letter
    // after tells "Ha|ar" from "Haa|r".
    const word = read('Haar')
    const afterH = 1 + 1
    expect(replacesBaseContent(word, afterH, afterH, word, afterH, afterH)).toBe(true)
    expect(replacesBaseContent(word, afterH + 1, afterH + 1, word, afterH, afterH)).toBe(false)
    expect(replacesBaseContent(word, afterH + 2, afterH + 2, word, afterH + 1, afterH + 1)).toBe(false)
  })

  it('takes the edge of the document as the context at its start and its end', () => {
    // Two paragraphs read alike from their boundary as from the document's
    // start or end; only the edge itself tells them apart.
    const document = read('Erster Absatz.\n\nLetzter Absatz.')
    const boundary = document.child(0).nodeSize
    const end = document.content.size
    expect(replacesBaseContent(document, 0, 0, document, 0, 0)).toBe(true)
    expect(replacesBaseContent(document, 1, 1, document, 0, 0)).toBe(false)
    expect(replacesBaseContent(document, boundary, boundary, document, 0, 0)).toBe(false)
    expect(replacesBaseContent(document, end, end, document, end, end)).toBe(true)
    expect(replacesBaseContent(document, end - 1, end - 1, document, end, end)).toBe(false)
    expect(replacesBaseContent(document, boundary, boundary, document, end, end)).toBe(false)
  })
})

describe('canonical documents read back unchanged, so a base equals its own state', () => {
  it('holds for every kind of block and mark the editor writes', () => {
    const markdown = [
      '# Überschrift eins',
      '',
      '## Überschrift zwei',
      '',
      'Absatz mit **fett**, *kursiv*, ~~durch~~, `code`, ==markiert== und [Link](https://example.org).',
      'Zweite Zeile nach einem Umbruch.  ',
      'Dritte Zeile.',
      '',
      '- Punkt eins',
      '  - verschachtelt',
      '- Punkt zwei',
      '',
      '1. erstens',
      '2. zweitens',
      '',
      '- [ ] offen',
      '- [x] erledigt',
      '',
      '> Zitat',
      '',
      '```ts',
      'const a = 1',
      '```',
      '',
      '---',
      '',
      '| Kopf | Wert |',
      '| --- | --- |',
      '| a | b |',
      '',
      '$$',
      'y = x^2',
      '$$',
      '',
      'Formel $x + 1$ im Satz.',
    ].join('\n')
    const document = read(markdown)
    const reread = read(serializeEditorJson(document.toJSON(), 'final'))
    expect(reread.eq(document)).toBe(true)
    expect(rebasePublication(reread, read(markdown.replace('Zitat', 'Zitat neu')), document).outcome)
      .toBe('direct')
  })
})

// Fuzz with an exact oracle. Paragraphs of base words; the AI replaces a
// range of one paragraph with letters no base word contains, and other
// writers insert unique markers, delete letters, add or drop paragraphs. The
// merge each case must produce is known exactly; a refusal must come from a
// change that really touches the AI section, or is counted as a false
// conflict (a refusal the client answers by building the publication again).
const WORDS = ['alpha', 'beta', 'gamma', 'delta', 'eps']

function generator(seed: number): (limit: number) => number {
  let state = seed
  return (limit) => {
    state = (state * 1103515245 + 12345) & 0x7fffffff
    return Math.floor((state / 0x7fffffff) * limit)
  }
}

const paragraphsDocument = (texts: readonly string[]): ProseMirrorNode => fromJson({
  type: 'doc',
  content: texts.map((text) => ({ type: 'paragraph', content: [{ type: 'text', text }] })),
})

type FuzzTally = {
  conflict: number
  falseConflict: number
  /** Stopped by the content check: a placement that would have replaced
   * other content. Never expected; counted so a broken mapping shows. */
  inconsistent: number
  merged: number
  /** Runs whose current document would have no block left. */
  skipped: number
  wrong: number
}

const emptyTally = (): FuzzTally => ({
  conflict: 0,
  falseConflict: 0,
  inconsistent: 0,
  merged: 0,
  skipped: 0,
  wrong: 0,
})

/** A merge is wrong unless it is one of the *acceptable* documents; a
 * refusal is a false conflict unless another writer's change *touches* the
 * AI section. */
function score(
  tally: FuzzTally,
  outcome: Outcome,
  touches: boolean,
  acceptable: readonly ProseMirrorNode[],
): void {
  if ('placed' in outcome) {
    tally.merged += 1
    if (!acceptable.some((candidate) => outcome.placed.eq(candidate))) tally.wrong += 1
  } else if ('refused' in outcome) {
    tally.conflict += 1
    if (!touches) tally.falseConflict += 1
  } else {
    tally.inconsistent += 1
  }
}

type OtherEdit =
  | { at: number; index: number; kind: 'insert'; text: string }
  | { from: number; index: number; kind: 'delete'; to: number }
  | { index: number; kind: 'drop' }
  | { index: number; kind: 'paragraph'; text: string }

function otherEdits(pick: (limit: number) => number, paragraphs: readonly string[], run: number, avoidDropping: number | null): OtherEdit[] {
  const edits: OtherEdit[] = []
  const used = new Set<number>()
  const attempts = 1 + pick(3)
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const index = pick(paragraphs.length)
    if (used.has(index)) continue
    used.add(index)
    const kind = pick(4)
    const length = paragraphs[index]!.length
    if (kind === 0) {
      edits.push({ at: pick(length + 1), index, kind: 'insert', text: `M${run}x${attempt}` })
    } else if (kind === 1 && length > 2) {
      const from = pick(length - 1)
      edits.push({ from, index, kind: 'delete', to: from + 1 + pick(Math.min(3, length - from - 1)) })
    } else if (kind === 2) {
      edits.push({ index, kind: 'paragraph', text: `P${run}x${attempt}` })
    } else if (avoidDropping === null || index !== avoidDropping || pick(4) === 0) {
      edits.push({ index, kind: 'drop' })
    }
  }
  return edits
}

function edited(text: string, edit: OtherEdit | undefined): string {
  if (edit?.kind === 'insert') return text.slice(0, edit.at) + edit.text + text.slice(edit.at)
  if (edit?.kind === 'delete') return text.slice(0, edit.from) + text.slice(edit.to)
  return text
}

function fuzzReplacements(seed: number, runs: number): FuzzTally {
  const pick = generator(seed)
  const tally = emptyTally()
  for (let run = 0; run < runs; run += 1) {
    const count = 3 + pick(5)
    const paragraphs = Array.from({ length: count }, (_value, index) => (
      `${WORDS[pick(5)]} ${index} ${WORDS[pick(5)]} ${WORDS[pick(5)]}`
    ))
    const aiIndex = pick(count)
    const text = paragraphs[aiIndex]!
    const start = pick(text.length)
    const end = start + 1 + pick(Math.min(6, text.length - start))
    const replacement = 'ZQ'.slice(0, 1 + pick(2)) + pick(9)
    const targetParagraphs = [...paragraphs]
    targetParagraphs[aiIndex] = text.slice(0, start) + replacement + text.slice(end)
    const edits = otherEdits(pick, paragraphs, run, aiIndex)

    let touches = false
    const current: string[] = []
    const expected: string[] = []
    paragraphs.forEach((paragraph, index) => {
      const edit = edits.find((candidate) => candidate.index === index)
      if (edit?.kind === 'paragraph') {
        current.push(edit.text)
        expected.push(edit.text)
      }
      if (edit?.kind === 'drop') {
        if (index === aiIndex) touches = true
        return
      }
      if (index === aiIndex && edit?.kind === 'insert' && edit.at >= start && edit.at <= end) touches = true
      if (index === aiIndex && edit?.kind === 'delete' && edit.from <= end && edit.to >= start) touches = true
      const now = edited(paragraph, edit)
      current.push(now)
      if (index !== aiIndex) {
        expected.push(now)
        return
      }
      const shift = edit?.kind === 'insert' && edit.at < start
        ? edit.text.length
        : edit?.kind === 'delete' && edit.to <= start ? -(edit.to - edit.from) : 0
      expected.push(now.slice(0, start + shift) + replacement + now.slice(end + shift))
    })
    if (current.length === 0) {
      tally.skipped += 1
      continue
    }
    const outcome = place(paragraphsDocument(paragraphs), paragraphsDocument(targetParagraphs), paragraphsDocument(current))
    // A change that touches the AI section admits no merge at all.
    score(tally, outcome, touches, touches ? [] : [paragraphsDocument(expected)])
  }
  return tally
}

function fuzzNewParagraphs(seed: number, runs: number): FuzzTally {
  const pick = generator(seed)
  const tally = emptyTally()
  for (let run = 0; run < runs; run += 1) {
    const count = 2 + pick(5)
    const paragraphs = Array.from({ length: count }, (_value, index) => `${WORDS[pick(5)]} ${index} ${WORDS[pick(5)]}`)
    const after = pick(count)
    const inserted = `ZNEW${run}`
    const targetParagraphs = [...paragraphs.slice(0, after + 1), inserted, ...paragraphs.slice(after + 1)]
    const edits = otherEdits(pick, paragraphs, run, null)

    let touches = false
    const current: string[] = []
    // The current paragraphs, each with the base paragraph it stands for
    // (null for a paragraph another writer added) and dropped base slots.
    const slots: Array<{ text: string | null; slot: number | null }> = []
    paragraphs.forEach((paragraph, index) => {
      const edit = edits.find((candidate) => candidate.index === index)
      if (edit?.kind === 'paragraph') {
        current.push(edit.text)
        slots.push({ slot: null, text: edit.text })
        if (index === after + 1) touches = true
      }
      if (edit?.kind === 'drop') {
        if (index === after || index === after + 1) touches = true
        slots.push({ slot: index, text: null })
        return
      }
      const now = edited(paragraph, edit)
      current.push(now)
      slots.push({ slot: index, text: now })
    })
    if (current.length === 0) {
      tally.skipped += 1
      continue
    }
    // Acceptable merges: the new paragraph right after the AI's anchor
    // paragraph, or after a paragraph another writer inserted at that same
    // boundary (a tie that loses no text).
    const anchor = slots.findIndex((entry) => entry.slot === after)
    const positions = [anchor + 1]
    if (slots[anchor + 1]?.slot === null) positions.push(anchor + 2)
    const acceptable = positions.map((position) => paragraphsDocument([
      ...slots.slice(0, position).map((entry) => entry.text),
      inserted,
      ...slots.slice(position).map((entry) => entry.text),
    ].filter((text): text is string => text !== null)))

    const outcome = place(paragraphsDocument(paragraphs), paragraphsDocument(targetParagraphs), paragraphsDocument(current))
    // Where a change touches the boundary of the new paragraph, a merge is
    // still right when it is one of the acceptable documents: a foreign
    // paragraph at the same boundary is a tie, and a dropped neighbour leaves
    // the new paragraph where the neighbour stood.
    score(tally, outcome, touches, acceptable)
  }
  return tally
}

/** Every base range `[from, to]` whose replacement by one insertion or one
 * removal of minimal size turns *text* into *now*: the readings of a change
 * that the current text cannot tell apart. */
function readingsOf(text: string, now: string): Array<[number, number]> {
  const grow = now.length - text.length
  const readings: Array<[number, number]> = []
  for (let at = 0; at <= Math.min(text.length, now.length); at += 1) {
    if (text.slice(0, at) !== now.slice(0, at)) break
    const removed = Math.max(0, -grow)
    if (at + removed <= text.length && text.slice(at + removed) === now.slice(at + Math.max(0, grow))) {
      readings.push([at, at + removed])
    }
  }
  return readings
}

/** The AI paragraph after another writer's one change in it, with the AI
 * replacement applied, or null when a reading of that change touches the AI
 * section, edges included. */
function mergedParagraph(
  text: string,
  now: string,
  section: { end: number; replacement: string; start: number },
): string | null {
  if (now === text) return text.slice(0, section.start) + section.replacement + text.slice(section.end)
  const readings = readingsOf(text, now)
  if (readings.length === 0) throw new Error(`no reading turns ${text} into ${now}`)
  if (readings.some(([from, to]) => from <= section.end && to >= section.start)) return null
  // No reading touches the section, and the readings are contiguous, so all
  // of them lie on one side of it.
  const shift = readings[0]![1] < section.start ? now.length - text.length : 0
  return now.slice(0, section.start + shift) + section.replacement + now.slice(section.end + shift)
}

function aiReplacement(pick: (limit: number) => number, text: string): { end: number; replacement: string; start: number } {
  const start = pick(text.length)
  return {
    end: start + 1 + pick(Math.min(6, text.length - start)),
    replacement: 'ZQ'.slice(0, 1 + pick(2)) + pick(9),
    start,
  }
}

const baseParagraphs = (pick: (limit: number) => number): string[] => Array.from(
  { length: 3 + pick(5) },
  (_value, index) => `${WORDS[pick(5)]} ${index} ${WORDS[pick(5)]} ${WORDS[pick(5)]}`,
)

/** Another writer types, in the AI paragraph, a copy of the letters next to
 * the insertion point, or removes letters, so that the change often has more
 * than one reading; elsewhere they edit as in the other families. A merge is
 * right only when no reading touches the AI section. */
function fuzzRepeatedText(seed: number, runs: number): FuzzTally {
  const pick = generator(seed)
  const tally = emptyTally()
  for (let run = 0; run < runs; run += 1) {
    const paragraphs = baseParagraphs(pick)
    const aiIndex = pick(paragraphs.length)
    const text = paragraphs[aiIndex]!
    const section = aiReplacement(pick, text)
    const targetParagraphs = [...paragraphs]
    targetParagraphs[aiIndex] = text.slice(0, section.start) + section.replacement + text.slice(section.end)
    const at = pick(text.length + 1)
    const length = 1 + pick(4)
    const copy = pick(2) === 0 ? text.slice(at, at + length) : text.slice(Math.max(0, at - length), at)
    const now = pick(3) === 0
      ? text.slice(0, at) + text.slice(at + length)
      : text.slice(0, at) + copy + text.slice(at)
    const edits = otherEdits(pick, paragraphs, run, aiIndex).filter((edit) => edit.index !== aiIndex)

    const merged = mergedParagraph(text, now, section)
    const current: string[] = []
    const expected: string[] = []
    paragraphs.forEach((paragraph, index) => {
      const edit = edits.find((candidate) => candidate.index === index)
      if (edit?.kind === 'paragraph') {
        current.push(edit.text)
        expected.push(edit.text)
      }
      if (edit?.kind === 'drop') return
      const changed = index === aiIndex ? now : edited(paragraph, edit)
      current.push(changed)
      expected.push(index === aiIndex ? merged ?? '' : changed)
    })
    const outcome = place(paragraphsDocument(paragraphs), paragraphsDocument(targetParagraphs), paragraphsDocument(current))
    score(tally, outcome, merged === null, merged === null ? [] : [paragraphsDocument(expected)])
  }
  return tally
}

/** Another writer swaps the AI paragraph with the paragraph *distance*
 * blocks away, a neighbour by default, and may edit either: the AI change
 * belongs to the moved paragraph, and a merge that pairs the paragraphs by
 * position instead puts it into the other one. Paragraphs between the two
 * stay as they are. A run without a paragraph that far away is skipped. */
function fuzzMovedParagraphs(seed: number, runs: number, distance = 1): FuzzTally {
  const pick = generator(seed)
  const tally = emptyTally()
  for (let run = 0; run < runs; run += 1) {
    const paragraphs = baseParagraphs(pick)
    const aiIndex = pick(paragraphs.length)
    const below = aiIndex + distance < paragraphs.length
    const above = aiIndex - distance >= 0
    if (!below && !above) {
      tally.skipped += 1
      continue
    }
    const otherIndex = below && (!above || pick(2) === 0) ? aiIndex + distance : aiIndex - distance
    const text = paragraphs[aiIndex]!
    const section = aiReplacement(pick, text)
    const targetParagraphs = [...paragraphs]
    targetParagraphs[aiIndex] = text.slice(0, section.start) + section.replacement + text.slice(section.end)
    const change = (paragraph: string, marker: string): string => {
      const kind = pick(3)
      const at = pick(paragraph.length)
      if (kind === 0) return paragraph.slice(0, at) + marker + paragraph.slice(at)
      if (kind === 1) return paragraph.slice(0, at) + paragraph.slice(at + 1 + pick(3))
      return paragraph
    }
    const now = change(text, `M${run}x0`)
    const otherNow = change(paragraphs[otherIndex]!, `M${run}x1`)

    const merged = mergedParagraph(text, now, section)
    const current = [...paragraphs]
    current[aiIndex] = otherNow
    current[otherIndex] = now
    const expected = [...current]
    expected[otherIndex] = merged ?? ''
    const outcome = place(paragraphsDocument(paragraphs), paragraphsDocument(targetParagraphs), paragraphsDocument(current))
    score(tally, outcome, merged === null, merged === null ? [] : [paragraphsDocument(expected)])
  }
  return tally
}

describe('placement fuzz where the changes since have more than one reading: 18,000 runs, no wrong merge', () => {
  // Fixed by the seeds like the tallies below. The false conflicts come from
  // paragraphs other writers inserted or removed among the changed
  // paragraphs around the AI paragraph, which make that run one range, and,
  // for moved paragraphs, from moving the AI paragraph itself: a paragraph
  // the alignment finds removed and inserted elsewhere touches the section,
  // and a swap that reads as small as both paragraphs edited in place is
  // refused as ambiguous, near or far.
  it.each([
    [7, { conflict: 715, falseConflict: 205, inconsistent: 0, merged: 785, skipped: 0, wrong: 0 }],
    [19, { conflict: 699, falseConflict: 203, inconsistent: 0, merged: 801, skipped: 0, wrong: 0 }],
    [31, { conflict: 717, falseConflict: 205, inconsistent: 0, merged: 783, skipped: 0, wrong: 0 }],
    [43, { conflict: 709, falseConflict: 204, inconsistent: 0, merged: 791, skipped: 0, wrong: 0 }],
  ])('repeated text typed or removed in the AI paragraph, seed %i', (seed, tally) => {
    expect(fuzzRepeatedText(seed, 1_500)).toEqual(tally)
  })

  it.each([
    [5, { conflict: 1_095, falseConflict: 784, inconsistent: 0, merged: 405, skipped: 0, wrong: 0 }],
    [17, { conflict: 1_097, falseConflict: 788, inconsistent: 0, merged: 403, skipped: 0, wrong: 0 }],
    [29, { conflict: 1_092, falseConflict: 790, inconsistent: 0, merged: 408, skipped: 0, wrong: 0 }],
    [41, { conflict: 1_088, falseConflict: 781, inconsistent: 0, merged: 412, skipped: 0, wrong: 0 }],
  ])('the AI paragraph swapped with a neighbour, seed %i', (seed, tally) => {
    expect(fuzzMovedParagraphs(seed, 1_500)).toEqual(tally)
  })

  it.each([
    [5, { conflict: 1_218, falseConflict: 976, inconsistent: 0, merged: 194, skipped: 88, wrong: 0 }],
    [17, { conflict: 1_214, falseConflict: 969, inconsistent: 0, merged: 197, skipped: 89, wrong: 0 }],
    [29, { conflict: 1_209, falseConflict: 962, inconsistent: 0, merged: 202, skipped: 89, wrong: 0 }],
    [41, { conflict: 1_208, falseConflict: 966, inconsistent: 0, merged: 202, skipped: 90, wrong: 0 }],
  ])('the AI paragraph swapped with one past an unchanged paragraph, seed %i', (seed, tally) => {
    expect(fuzzMovedParagraphs(seed, 1_500, 2)).toEqual(tally)
  })
})

describe('placement fuzz with an exact oracle: 12,000 runs, no wrong merge', () => {
  // The tallies are fixed by the seeds. A change of the placement rules
  // shows here first; it is acceptable only with `wrong: 0` and
  // `inconsistent: 0`. False conflicts are refusals of a publication no
  // change touched; the client builds such a publication again once. A run
  // whose other writers removed every paragraph is skipped: the editor
  // always keeps one block.
  it.each([
    [7, { conflict: 152, falseConflict: 8, inconsistent: 0, merged: 1_348, skipped: 0, wrong: 0 }],
    [19, { conflict: 149, falseConflict: 6, inconsistent: 0, merged: 1_351, skipped: 0, wrong: 0 }],
    [31, { conflict: 146, falseConflict: 4, inconsistent: 0, merged: 1_354, skipped: 0, wrong: 0 }],
    [43, { conflict: 152, falseConflict: 7, inconsistent: 0, merged: 1_348, skipped: 0, wrong: 0 }],
  ])('replacements inside a paragraph, seed %i', (seed, tally) => {
    expect(fuzzReplacements(seed, 1_500)).toEqual(tally)
  })

  it.each([
    [5, { conflict: 247, falseConflict: 57, inconsistent: 0, merged: 1_246, skipped: 7, wrong: 0 }],
    [17, { conflict: 241, falseConflict: 50, inconsistent: 0, merged: 1_254, skipped: 5, wrong: 0 }],
    [29, { conflict: 238, falseConflict: 48, inconsistent: 0, merged: 1_255, skipped: 7, wrong: 0 }],
    [41, { conflict: 242, falseConflict: 53, inconsistent: 0, merged: 1_252, skipped: 6, wrong: 0 }],
  ])('new paragraphs between two paragraphs, seed %i', (seed, tally) => {
    expect(fuzzNewParagraphs(seed, 1_500)).toEqual(tally)
  })
})
