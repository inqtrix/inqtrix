import { describe, expect, it } from 'vitest'

import { reviewChangedLines } from './answerDraft'

describe('the lines the review changed', () => {
  it('names none when the reviewed answer is the draft', () => {
    expect([...reviewChangedLines('A.\n\nB.', 'A.\n\nB.')]).toEqual([])
  })

  it('names the lines of the reviewed answer the draft did not hold', () => {
    expect([...reviewChangedLines('Erste Antwort.', '## Lage\n\nErste Antwort.')]).toEqual([1, 2])
  })

  it('takes a trailing line end for no change', () => {
    expect([...reviewChangedLines('A.', 'A.\n')]).toEqual([])
  })

  it('names one item of a list, not the list', () => {
    expect([
      ...reviewChangedLines('1. Eins\n2. Zwei\n3. Drei', '1. Eins\n2. Zwei, belegt\n3. Drei'),
    ]).toEqual([2])
  })

  it('counts lines as the renderer reads them after display math', () => {
    // The renderer turns `\[ x \]` into three lines; a change after it is
    // marked on the line the renderer sees, not on the source line.
    expect([
      ...reviewChangedLines('\\[ x^2 \\]\n\nAlt.', '\\[ x^2 \\]\n\nNeu.'),
    ]).toEqual([5])
  })
})
