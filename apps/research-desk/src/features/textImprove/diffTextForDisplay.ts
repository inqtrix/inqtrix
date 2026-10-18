export type TextDiffToken = {
  status: 'changed' | 'unchanged'
  text: string
}

type SignificantToken = {
  index: number
  normalized: string
}

export function diffTextForDisplay(
  originalText: string,
  improvedText: string,
): TextDiffToken[] {
  const originalTokens = tokenize(originalText)
  const improvedTokens = tokenize(improvedText)
  const originalSignificant = significantTokens(originalTokens)
  const improvedSignificant = significantTokens(improvedTokens)
  const matchedImprovedIndexes = lcsMatchedImprovedIndexes(
    originalSignificant,
    improvedSignificant,
  )

  return improvedTokens.map((token, index) => ({
    status: isWhitespace(token) || matchedImprovedIndexes.has(index)
      ? 'unchanged'
      : 'changed',
    text: token,
  }))
}

function tokenize(text: string) {
  return text.split(/(\s+)/).filter((token) => token.length > 0)
}

function significantTokens(tokens: string[]): SignificantToken[] {
  return tokens.flatMap((token, index) => (
    isWhitespace(token)
      ? []
      : [{ index, normalized: normalizeToken(token) }]
  ))
}

function lcsMatchedImprovedIndexes(
  originalTokens: SignificantToken[],
  improvedTokens: SignificantToken[],
) {
  const table = Array.from({ length: originalTokens.length + 1 }, () =>
    Array.from({ length: improvedTokens.length + 1 }, () => 0)
  )

  for (let originalIndex = originalTokens.length - 1; originalIndex >= 0; originalIndex -= 1) {
    for (let improvedIndex = improvedTokens.length - 1; improvedIndex >= 0; improvedIndex -= 1) {
      table[originalIndex][improvedIndex] = originalTokens[originalIndex].normalized === improvedTokens[improvedIndex].normalized
        ? table[originalIndex + 1][improvedIndex + 1] + 1
        : Math.max(table[originalIndex + 1][improvedIndex], table[originalIndex][improvedIndex + 1])
    }
  }

  const matchedImprovedIndexes = new Set<number>()
  let originalIndex = 0
  let improvedIndex = 0
  while (originalIndex < originalTokens.length && improvedIndex < improvedTokens.length) {
    if (originalTokens[originalIndex].normalized === improvedTokens[improvedIndex].normalized) {
      matchedImprovedIndexes.add(improvedTokens[improvedIndex].index)
      originalIndex += 1
      improvedIndex += 1
    } else if (table[originalIndex + 1][improvedIndex] >= table[originalIndex][improvedIndex + 1]) {
      originalIndex += 1
    } else {
      improvedIndex += 1
    }
  }

  return matchedImprovedIndexes
}

function normalizeToken(token: string) {
  return token.toLocaleLowerCase()
}

function isWhitespace(token: string) {
  return /^\s+$/.test(token)
}
