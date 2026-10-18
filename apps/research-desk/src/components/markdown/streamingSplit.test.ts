// Streaming split heuristics: a lone "$5" money token
// must never open "unclosed inline math" and demote the rest of the
// answer to raw pending text — a visible raw-markdown flash.
import { describe, expect, it } from 'vitest'

import { splitStreamingMarkdown } from './MarkdownRenderer'

describe('splitStreamingMarkdown', () => {
  it('keeps a money token on a completed line stable', () => {
    const markdown = 'Das kostet $5 Millionen pro Jahr.\nNaechster Absatz.'
    expect(splitStreamingMarkdown(markdown)).toEqual({
      pendingKind: null,
      pendingText: '',
      stableMarkdown: markdown,
    })
  })

  it('keeps two money tokens on one line stable', () => {
    // " $7" does not qualify as a CLOSE delimiter (whitespace before
    // it), so "$5 ... $7" does not pair up into one giant formula.
    const markdown = 'Zwischen $5 Millionen und $7 Milliarden.\nEnde.'
    expect(splitStreamingMarkdown(markdown).pendingKind).toBeNull()
  })

  it('still parks a genuinely open formula on the streaming tail', () => {
    const markdown = 'Die Formel $E=mc'
    const split = splitStreamingMarkdown(markdown)
    expect(split.pendingKind).toBe('math')
    expect(split.pendingText).toBe('$E=mc')
    expect(split.stableMarkdown).toBe('Die Formel')
  })

  it('treats a closed inline formula as stable', () => {
    const markdown = 'Die Formel $E=mc^2$ gilt.\nWeiter.'
    expect(splitStreamingMarkdown(markdown).pendingKind).toBeNull()
  })

  it('parks an unclosed code fence', () => {
    const markdown = 'Text davor.\n```python\nprint("x")'
    const split = splitStreamingMarkdown(markdown)
    expect(split.pendingKind).toBe('code')
    expect(split.stableMarkdown).toBe('Text davor.')
  })
})
