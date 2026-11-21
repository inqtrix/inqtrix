import { describe, expect, it } from 'vitest'

import {
  MARKDOWN_MERGE_MAX_CHANGED_LINES,
  MARKDOWN_MERGE_MAX_LINES,
  mergeKeepsEveryChange,
  mergeMarkdownRevisions,
  mergeMarkdownRevisionsWith,
  sameMarkdownText,
} from './markdownMerge'

/** Paragraphs as the editor writes them: one line each, a blank line between. */
function paragraphs(...lines: string[]): string {
  return `${lines.join('\n\n')}\n`
}

/** Lines without a blank line between them, as in a list or a table. */
function lines(...items: string[]): string {
  return `${items.join('\n')}\n`
}

describe('merging two versions of a Markdown document line by line', () => {
  const base = paragraphs('# Plan', 'Der Bau dauert sechs Wochen.', 'Die Kosten liegen bei 18.000 Euro.', 'Das Team trifft sich montags.')

  it('keeps both changes to neighbouring paragraphs with a blank line between them', () => {
    const ours = base.replace('sechs Wochen', 'sieben Wochen')
    const theirs = base.replace('18.000 Euro', '21.500 Euro')

    expect(mergeMarkdownRevisions(base, ours, theirs)).toEqual({
      contentMarkdown: paragraphs('# Plan', 'Der Bau dauert sieben Wochen.', 'Die Kosten liegen bei 21.500 Euro.', 'Das Team trifft sich montags.'),
      kind: 'merged',
    })
  })

  it('reports two different changes to the same paragraph as a conflict', () => {
    const ours = base.replace('sechs Wochen', 'sieben Wochen')
    const theirs = base.replace('dauert sechs', 'dauert gut sechs')

    expect(mergeMarkdownRevisions(base, ours, theirs)).toEqual({ kind: 'conflict', reason: 'overlap' })
  })

  it('reports changes to neighbouring list items and table rows as a conflict', () => {
    const list = lines('- Ziegel', '- Holz', '- Glas')
    expect(mergeMarkdownRevisions(
      list,
      lines('- Ziegel, rot', '- Holz', '- Glas'),
      lines('- Ziegel', '- Holz, Eiche', '- Glas'),
    )).toEqual({ kind: 'conflict', reason: 'overlap' })

    const table = lines('| Posten | Euro |', '| --- | --- |', '| Bau | 10 |', '| Planung | 5 |')
    expect(mergeMarkdownRevisions(
      table,
      lines('| Posten | Euro |', '| --- | --- |', '| Bau | 12 |', '| Planung | 5 |'),
      lines('| Posten | Euro |', '| --- | --- |', '| Bau | 10 |', '| Planung | 6 |'),
    )).toEqual({ kind: 'conflict', reason: 'overlap' })
  })

  it('takes the same change made on both sides once', () => {
    // Both sides made the cost change; one side also changed the meeting
    // day. Neither whole text equals the other, so the paragraphs decide.
    const ours = base.replace('18.000 Euro', '21.500 Euro').replace('montags', 'dienstags')
    const theirs = base.replace('18.000 Euro', '21.500 Euro')

    expect(mergeMarkdownRevisions(base, ours, theirs)).toEqual({
      contentMarkdown: paragraphs('# Plan', 'Der Bau dauert sechs Wochen.', 'Die Kosten liegen bei 21.500 Euro.', 'Das Team trifft sich dienstags.'),
      kind: 'merged',
    })
  })

  it('reports two different appends at the end as a conflict and takes the same append once', () => {
    const short = paragraphs('Eins.', 'Zwei.')
    expect(mergeMarkdownRevisions(
      short,
      paragraphs('Eins.', 'Zwei.', 'Drei von hier.'),
      paragraphs('Eins.', 'Zwei.', 'Drei von dort.'),
    )).toEqual({ kind: 'conflict', reason: 'overlap' })

    expect(mergeMarkdownRevisions(
      short,
      paragraphs('Eins, geändert.', 'Zwei.', 'Drei.'),
      paragraphs('Eins.', 'Zwei.', 'Drei.'),
    )).toEqual({ contentMarkdown: paragraphs('Eins, geändert.', 'Zwei.', 'Drei.'), kind: 'merged' })
  })

  it('reports a line inserted right at a line the other side changed as a conflict', () => {
    // No line lies between the insertion and the change: they touch.
    expect(mergeMarkdownRevisions(
      lines('a', 'b', 'c'),
      lines('a', 'b', 'neu', 'c'),
      lines('a', 'b', 'C'),
    )).toEqual({ kind: 'conflict', reason: 'overlap' })
    expect(mergeMarkdownRevisions(
      lines('a', 'b', 'c'),
      lines('a', 'neu', 'b', 'c'),
      lines('a', 'B', 'c'),
    )).toEqual({ kind: 'conflict', reason: 'overlap' })
  })

  it('takes the other side when one side kept the base', () => {
    const changed = base.replace('montags', 'freitags')
    expect(mergeMarkdownRevisions(base, base, changed)).toEqual({ contentMarkdown: changed, kind: 'merged' })
    expect(mergeMarkdownRevisions(base, changed, base)).toEqual({ contentMarkdown: changed, kind: 'merged' })
    expect(mergeMarkdownRevisions(base, changed, changed)).toEqual({ contentMarkdown: changed, kind: 'merged' })
  })

  it('compares Windows and Unix line endings as the same', () => {
    const windowsBase = base.replace(/\n/g, '\r\n')
    const windowsOurs = windowsBase.replace('sechs Wochen', 'sieben Wochen')
    const unixTheirs = base.replace('18.000 Euro', '21.500 Euro')

    expect(mergeMarkdownRevisions(windowsBase, windowsOurs, unixTheirs)).toEqual({
      contentMarkdown: paragraphs('# Plan', 'Der Bau dauert sieben Wochen.', 'Die Kosten liegen bei 21.500 Euro.', 'Das Team trifft sich montags.'),
      kind: 'merged',
    })
    expect(sameMarkdownText(windowsBase, base)).toBe(true)
    expect(sameMarkdownText(windowsOurs, base)).toBe(false)
  })

  it(`ends as a conflict above ${MARKDOWN_MERGE_MAX_LINES} lines in any of the three texts`, () => {
    const long = lines(...Array.from({ length: MARKDOWN_MERGE_MAX_LINES + 1 }, (_, index) => `Zeile ${index}`))
    const ours = long.replace('Zeile 10\n', 'Zeile zehn\n')
    const theirs = long.replace('Zeile 20000\n', 'Zeile zwanzigtausend\n')

    expect(mergeMarkdownRevisions(long, ours, theirs)).toEqual({ kind: 'conflict', reason: 'too_large' })
  })

  it(`ends as a conflict when a side changes more than ${MARKDOWN_MERGE_MAX_CHANGED_LINES} lines`, () => {
    const items = Array.from({ length: 3_000 }, (_, index) => `Zeile ${index}`)
    const base = lines(...items)
    const theirs = base.replace('Zeile 2999\n', 'Zeile zweitausendneunhundertneunundneunzig\n')
    // Rewriting 600 lines removes and inserts 1 200: past the bound, however
    // far the other side's change lies.
    const rewritten = lines(...items.map((item, index) => (index < 600 ? `${item} neu` : item)))
    expect(mergeMarkdownRevisions(base, rewritten, theirs)).toEqual({ kind: 'conflict', reason: 'too_large' })
    // Within the bound the same kind of change merges.
    const fewer = lines(...items.map((item, index) => (index < 100 ? `${item} neu` : item)))
    expect(mergeMarkdownRevisions(base, fewer, theirs).kind).toBe('merged')
  })

  it('turns a merge result that drops or repeats text into a conflict', () => {
    const ours = base.replace('sechs Wochen', 'sieben Wochen')
    const theirs = base.replace('18.000 Euro', '21.500 Euro')

    // A merger that keeps only one side, and one that writes both versions
    // one after the other: both look clean, and both lose or double text.
    expect(mergeMarkdownRevisionsWith(() => ours, base, ours, theirs))
      .toEqual({ kind: 'conflict', reason: 'unsafe' })
    expect(mergeMarkdownRevisionsWith((_, mine, other) => `${mine}${other}`, base, ours, theirs))
      .toEqual({ kind: 'conflict', reason: 'unsafe' })
  })
})

describe('the check after a clean merge', () => {
  const base = paragraphs('Eins.', 'Zwei.', 'Drei.')
  const ours = paragraphs('Eins, hier.', 'Zwei.', 'Drei.')
  const theirs = paragraphs('Eins.', 'Zwei.', 'Drei, dort.')
  const merged = paragraphs('Eins, hier.', 'Zwei.', 'Drei, dort.')

  it('accepts a correct merge', () => {
    expect(mergeKeepsEveryChange(base, ours, theirs, merged)).toBe(true)
  })

  it('finds a line one side inserted missing from the result', () => {
    expect(mergeKeepsEveryChange(base, ours, theirs, paragraphs('Eins, hier.', 'Zwei.', 'Drei.'))).toBe(false)
  })

  it('finds a line both sides kept missing from the result', () => {
    expect(mergeKeepsEveryChange(base, ours, theirs, paragraphs('Eins, hier.', 'Drei, dort.'))).toBe(false)
  })

  it('finds text the result repeats', () => {
    expect(mergeKeepsEveryChange(base, ours, theirs, `${merged}${paragraphs('Zwei.')}`)).toBe(false)
  })
})

/** A small seeded generator, so a failing case can be replayed. */
function seededRandom(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (state + 0x6d2b79f5) >>> 0
    let value = state
    value = Math.imul(value ^ (value >>> 15), value | 1)
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61)
    return ((value ^ (value >>> 14)) >>> 0) / 4_294_967_296
  }
}

function randomParagraphs(random: () => number, count: number): string[] {
  return Array.from({ length: count }, (_, index) => `Absatz ${index} mit Wert ${Math.floor(random() * 1_000_000)}.`)
}

function pick(random: () => number, from: readonly number[], count: number): number[] {
  const pool = [...from]
  const picked: number[] = []
  while (picked.length < count && pool.length > 0) {
    picked.push(pool.splice(Math.floor(random() * pool.length), 1)[0])
  }
  return picked.sort((left, right) => left - right)
}

describe('merge properties over seeded random documents', () => {
  it('returns the changed side when the other kept the base, and a version both share once', () => {
    const random = seededRandom(918_273)
    for (let run = 0; run < 150; run += 1) {
      const items = randomParagraphs(random, 2 + Math.floor(random() * 30))
      const base = paragraphs(...items)
      const changed = [...items]
      for (const index of pick(random, items.map((_, position) => position), 1 + Math.floor(random() * 3))) {
        changed[index] = `Geändert ${run}-${index}.`
      }
      const x = paragraphs(...changed)

      expect(mergeMarkdownRevisions(base, x, base)).toEqual({ contentMarkdown: x, kind: 'merged' })
      expect(mergeMarkdownRevisions(base, base, x)).toEqual({ contentMarkdown: x, kind: 'merged' })
      expect(mergeMarkdownRevisions(base, x, x)).toEqual({ contentMarkdown: x, kind: 'merged' })
    }
  })

  it('combines changes to separate paragraphs into the sum of both', () => {
    const random = seededRandom(564_738)
    for (let run = 0; run < 200; run += 1) {
      const items = randomParagraphs(random, 4 + Math.floor(random() * 40))
      const positions = items.map((_, position) => position)
      const ourIndexes = pick(random, positions, 1 + Math.floor(random() * 4))
      const theirIndexes = pick(
        random,
        positions.filter((position) => !ourIndexes.includes(position)),
        1 + Math.floor(random() * 4),
      )
      const ours = [...items]
      const theirs = [...items]
      const expected = [...items]
      for (const index of ourIndexes) {
        ours[index] = `Von hier ${run}-${index}.`
        expected[index] = ours[index]
      }
      for (const index of theirIndexes) {
        theirs[index] = `Von dort ${run}-${index}.`
        expected[index] = theirs[index]
      }

      expect(
        mergeMarkdownRevisions(paragraphs(...items), paragraphs(...ours), paragraphs(...theirs)),
        `run ${run}`,
      ).toEqual({ contentMarkdown: paragraphs(...expected), kind: 'merged' })
    }
  })
})

describe('merge time on a long document', () => {
  it('merges scattered changes in a document of 10 000 lines', async ({ annotate }) => {
    // A paragraph on every even line, a blank line on every odd one.
    const items = Array.from({ length: 10_000 }, (_, index) => (
      index % 2 === 0 ? `Absatz ${index / 2} eines langen Berichts.` : ''
    ))
    const ours = [...items]
    const theirs = [...items]
    const expected = [...items]
    for (let index = 14; index + 200 < items.length; index += 500) {
      ours[index] = `Von hier ${index}.`
      expected[index] = ours[index]
      theirs[index + 200] = `Von dort ${index + 200}.`
      expected[index + 200] = theirs[index + 200]
    }
    const base = lines(...items)

    const started = performance.now()
    const result = mergeMarkdownRevisions(base, lines(...ours), lines(...theirs))
    const elapsed = performance.now() - started

    expect(base.match(/\n/g)).toHaveLength(10_000)
    expect(result).toEqual({ contentMarkdown: lines(...expected), kind: 'merged' })
    // Measured, not promised: the number goes into the report, the bound only
    // catches a merge that runs away.
    await annotate(`merge of 10 000 lines took ${elapsed.toFixed(1)} ms`)
    expect(elapsed).toBeLessThan(30_000)
  })
})
