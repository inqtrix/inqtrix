import { describe, expect, it } from 'vitest'

import {
  markdownSourceFromPosition,
  serializeMarkdownTableCsv,
} from './markdownBlockExport'

describe('markdown table source export', () => {
  const markdown = [
    'Before',
    '',
    '| Name | Wert |',
    '| :--- | ---: |',
    '| **Größe** | `1,5` |',
    '',
    'After',
  ].join('\n')

  it('preserves alignment markers and inline Markdown exactly', () => {
    const start = markdown.indexOf('| Name')
    const end = markdown.indexOf('\n\nAfter')
    expect(markdownSourceFromPosition(markdown, {
      end: { offset: end },
      start: { offset: start },
    })).toBe('| Name | Wert |\n| :--- | ---: |\n| **Größe** | `1,5` |')
  })

  it('rejects missing, reversed, and out-of-range offsets', () => {
    expect(markdownSourceFromPosition(markdown, undefined)).toBeNull()
    expect(markdownSourceFromPosition(markdown, {
      end: { offset: 3 },
      start: { offset: 4 },
    })).toBeNull()
    expect(markdownSourceFromPosition(markdown, {
      end: { offset: markdown.length + 1 },
      start: { offset: 0 },
    })).toBeNull()
  })
})

describe('markdown table CSV export', () => {
  it('uses CRLF rows and escapes commas, quotes, newlines, and Unicode', () => {
    expect(serializeMarkdownTableCsv([
      ['Name', 'Notiz'],
      ['Größe', '1,5'],
      ['Zitat', 'Er sagt "Hallo"'],
      ['Mehrzeilig', 'Zeile 1\nZeile 2'],
    ])).toBe(
      'Name,Notiz\r\n'
      + 'Größe,"1,5"\r\n'
      + 'Zitat,"Er sagt ""Hallo"""\r\n'
      + 'Mehrzeilig,"Zeile 1\nZeile 2"',
    )
  })
})
