import { describe, expect, it } from 'vitest'

import { normalizeLatexDelimiters } from './latexDelimiters'

describe('normalizeLatexDelimiters', () => {
  it('turns a bracket block on its own lines into display math', () => {
    expect(normalizeLatexDelimiters('Es gilt\n\\[\nE = mc^2\n\\]\nfertig')).toBe(
      'Es gilt\n$$\nE = mc^2\n$$\nfertig',
    )
    expect(normalizeLatexDelimiters('\\[ a + b \\]')).toBe('$$\na + b\n$$')
  })

  it('turns an inline pair into dollar math', () => {
    expect(normalizeLatexDelimiters('mit \\( x^2 \\) im Satz')).toBe('mit $x^2$ im Satz')
  })

  it('keeps an escaped bracket inside a sentence as the literal bracket it is', () => {
    // The kernel escapes brackets in reference titles; a title is not an
    // equation. This is the regression that motivated the rewrite.
    const line = '- [W1] Lagebericht \\[2024\\] der Bundesnetzagentur'
    expect(normalizeLatexDelimiters(line)).toBe(line)
  })

  it('never touches fenced or inline code', () => {
    const fenced = 'Text\n```tex\n\\[ x \\]\n\\( y \\)\n```\nEnde'
    expect(normalizeLatexDelimiters(fenced)).toBe(fenced)
    const tilde = '~~~\n\\[ x \\]\n~~~'
    expect(normalizeLatexDelimiters(tilde)).toBe(tilde)
    const inline = 'Der Regex `\\( x \\)` bleibt, aber \\( y \\) nicht'
    expect(normalizeLatexDelimiters(inline)).toBe('Der Regex `\\( x \\)` bleibt, aber $y$ nicht')
  })

  it('leaves an unclosed bracket block as prose', () => {
    const open = '\\[ nur offen\n\nNeuer Absatz'
    expect(normalizeLatexDelimiters(open)).toBe(open)
    const trailing = 'Absatz\n\\[ bis zum Ende'
    expect(normalizeLatexDelimiters(trailing)).toBe(trailing)
  })

  it('does not join an inline pair across lines', () => {
    const split = 'Erste \\( a\nzweite b \\)'
    expect(normalizeLatexDelimiters(split)).toBe(split)
  })

  it('returns the input untouched when no bracket delimiter is present', () => {
    const plain = 'Preis $5 und $x^2$ bleiben'
    expect(normalizeLatexDelimiters(plain)).toBe(plain)
  })
})
