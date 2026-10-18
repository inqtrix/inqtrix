import { describe, expect, it } from 'vitest'
import {
  createMarkdownTable,
  deleteMarkdownTableColumn,
  deleteMarkdownTableRow,
  findMarkdownTableAtOffset,
  formatMarkdownTables,
  insertMarkdownTableColumn,
  insertMarkdownTableRow,
  isMarkdownTableRowLine,
  serializeMarkdownTable,
  setMarkdownTableAlignment,
  updateMarkdownTableCell,
} from './markdownSourceFormatting'

describe('formatMarkdownTables', () => {
  it('formats valid markdown pipe tables compactly', () => {
    const markdown = [
      '| Dimension | Gemini | Project Astra |',
      '| --- | --- | --- |',
      '| Funktion | Foundation-Modell | Agent-Schicht |',
      '| Vertrieb | App, API | Eingebettet |',
    ].join('\n')

    expect(formatMarkdownTables(markdown)).toBe([
      '| Dimension | Gemini | Project Astra |',
      '| --- | --- | --- |',
      '| Funktion | Foundation-Modell | Agent-Schicht |',
      '| Vertrieb | App, API | Eingebettet |',
    ].join('\n'))
  })

  it('preserves escaped and inline-code pipes inside cells', () => {
    const markdown = [
      '| Name | Note |',
      '| --- | --- |',
      String.raw`| A | value \| detail |`,
      '| B | `x | y` |',
    ].join('\n')

    expect(formatMarkdownTables(markdown)).toBe([
      '| Name | Note |',
      '| --- | --- |',
      String.raw`| A | value \| detail |`,
      '| B | `x | y` |',
    ].join('\n'))
  })

  it('skips tables inside fenced code blocks', () => {
    const markdown = [
      '```markdown',
      '| A | B |',
      '| --- | --- |',
      '| x | long value |',
      '```',
    ].join('\n')

    expect(formatMarkdownTables(markdown)).toBe(markdown)
  })

  it('skips malformed tables without changing them', () => {
    const markdown = [
      '| A | B |',
      '| --- | --- |',
      '| one | two |',
      '| uneven | row | extra |',
    ].join('\n')

    expect(formatMarkdownTables(markdown)).toBe(markdown)
  })
})

describe('findMarkdownTableAtOffset', () => {
  it('returns a valid table selection for a cursor inside a table', () => {
    const markdown = [
      'Intro',
      '',
      '| A | B |',
      '| --- | ---: |',
      '| x | y |',
      '',
      'Outro',
    ].join('\n')

    const selection = findMarkdownTableAtOffset(markdown, markdown.indexOf('x | y'))
    expect(selection).toMatchObject({
      fromLine: 3,
      table: {
        alignments: ['none', 'right'],
        rows: [
          ['A', 'B'],
          ['x', 'y'],
        ],
      },
      toLine: 5,
    })
  })

  it('ignores table-looking content inside fenced code blocks', () => {
    const markdown = [
      '```markdown',
      '| A | B |',
      '| --- | --- |',
      '| x | y |',
      '```',
    ].join('\n')

    expect(findMarkdownTableAtOffset(markdown, markdown.indexOf('| x'))).toBeNull()
  })
})

describe('markdown table model helpers', () => {
  it('serializes edited tables with escaped pipes and alignment markers', () => {
    const table = setMarkdownTableAlignment(
      updateMarkdownTableCell(createMarkdownTable('Column', 2, 1), 1, 1, 'value | detail'),
      1,
      'right',
    )

    expect(serializeMarkdownTable(table)).toBe([
      '| Column 1 | Column 2 |',
      '| --- | ---: |',
      String.raw`|  | value \| detail |`,
    ].join('\n'))
  })

  it('adds and removes rows without deleting the header or final body row', () => {
    const table = createMarkdownTable('Column', 2, 1)
    const expanded = insertMarkdownTableRow(table, 1)
    expect(expanded.rows).toHaveLength(3)

    const reduced = deleteMarkdownTableRow(expanded, 1)
    expect(reduced.rows).toHaveLength(2)
    expect(deleteMarkdownTableRow(reduced, 1).rows).toHaveLength(2)
    expect(deleteMarkdownTableRow(reduced, 0).rows).toHaveLength(2)
  })

  it('adds and removes columns while preserving a valid markdown table width', () => {
    const table = createMarkdownTable('Column', 2, 1)
    const expanded = insertMarkdownTableColumn(table, 0, 'Inserted')
    expect(expanded.alignments).toHaveLength(3)
    expect(expanded.rows.every((row) => row.length === 3)).toBe(true)
    expect(expanded.rows[0][1]).toBe('Inserted')

    const reduced = deleteMarkdownTableColumn(expanded, 1)
    expect(reduced.alignments).toHaveLength(2)
    expect(deleteMarkdownTableColumn(reduced, 1).alignments).toHaveLength(2)
  })
})

describe('isMarkdownTableRowLine', () => {
  it('detects table rows while ignoring ordinary prose', () => {
    expect(isMarkdownTableRowLine('| A | B |')).toBe(true)
    expect(isMarkdownTableRowLine('| --- | --- |')).toBe(true)
    expect(isMarkdownTableRowLine('A | B | C')).toBe(true)
    expect(isMarkdownTableRowLine('Text with one | pipe')).toBe(false)
    expect(isMarkdownTableRowLine('Text without table syntax')).toBe(false)
  })
})
