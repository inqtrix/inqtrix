/**
 * What one word is when a suggestion shows a change. Comparing two versions
 * marks whole words, and a number together with the separators inside it is
 * one: 18.000, 21.500,00, 1,5, 14:30, 01.10.2026, 2024–2025, and 18 000
 * written with a (narrow) no-break space. Split at its separators, a changed
 * amount read as fragments ("1821.000500"). The suggestion diff in the browser
 * and the publication on the collaboration server share this one rule.
 */

/** Characters that hold a number together when digits stand on both sides. */
export const NUMBER_WORD_SEPARATORS: ReadonlySet<string> = new Set([
  '.', ',', ':', '/', '-', '–', "'", '’', '\u00a0', '\u202f',
])

const DIGITS = /^[0-9]+$/
const WORD_OR_OTHER = /[\p{L}\p{N}_]+|[^\p{L}\p{N}_]/gu
const WORD_START = /^[\p{L}\p{N}_]/u

/**
 * `tokens`, each a word, a separator or other text, with every run of
 * digits, separator, digits (and so on) joined into one token.
 */
export function joinNumberWords(tokens: readonly string[]): string[] {
  const joined: string[] = []
  for (let index = 0; index < tokens.length; index += 1) {
    let token = tokens[index]!
    if (DIGITS.test(token)) {
      while (
        NUMBER_WORD_SEPARATORS.has(tokens[index + 1] ?? '')
        && DIGITS.test(tokens[index + 2] ?? '')
      ) {
        token += tokens[index + 1]! + tokens[index + 2]!
        index += 2
      }
    }
    joined.push(token)
  }
  return joined
}

/** The `[start, end)` offsets of every word in `text`, a number with its
 * separators as one word. */
export function wordSpans(text: string): Array<[number, number]> {
  const spans: Array<[number, number]> = []
  let offset = 0
  for (const token of joinNumberWords(text.match(WORD_OR_OTHER) ?? [])) {
    if (WORD_START.test(token)) spans.push([offset, offset + token.length])
    offset += token.length
  }
  return spans
}
