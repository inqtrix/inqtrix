export type MarkdownTableAlignment = 'left' | 'right' | 'center' | 'none'

export type MarkdownTableModel = {
  alignments: MarkdownTableAlignment[]
  rows: string[][]
}

export type MarkdownTableSelection = {
  from: number
  fromLine: number
  table: MarkdownTableModel
  to: number
  toLine: number
}

type ParsedTableRow = {
  cells: string[]
  hasLeadingPipe: boolean
  hasTrailingPipe: boolean
}

export function formatMarkdownTables(markdown: string): string {
  const newline = markdown.includes('\r\n') ? '\r\n' : '\n'
  const lines = markdown.split(/\r?\n/)
  const formattedLines: string[] = []
  let index = 0
  let fence: string | null = null

  while (index < lines.length) {
    const line = lines[index]
    const fenceMarker = markdownFenceMarker(line)
    if (fence) {
      formattedLines.push(line)
      if (fenceMarker?.startsWith(fence)) fence = null
      index += 1
      continue
    }
    if (fenceMarker) {
      fence = fenceMarker[0]
      formattedLines.push(line)
      index += 1
      continue
    }

    const table = readTable(lines, index)
    if (!table) {
      formattedLines.push(line)
      index += 1
      continue
    }

    formattedLines.push(...serializeMarkdownTable(toMarkdownTableModel(table)).split('\n'))
    index += table.rows.length
  }

  return formattedLines.join(newline)
}

export function findMarkdownTableAtOffset(markdown: string, offset: number): MarkdownTableSelection | null {
  const lines = markdown.split(/\r?\n/)
  const lineStarts = markdownLineStarts(markdown)
  const boundedOffset = Math.max(0, Math.min(offset, markdown.length))
  let index = 0
  let fence: string | null = null

  while (index < lines.length) {
    const line = lines[index]
    const fenceMarker = markdownFenceMarker(line)
    if (fence) {
      if (fenceMarker?.startsWith(fence)) fence = null
      index += 1
      continue
    }
    if (fenceMarker) {
      fence = fenceMarker[0]
      index += 1
      continue
    }

    const table = readTable(lines, index)
    if (!table) {
      index += 1
      continue
    }

    const from = lineStarts[index]
    const lastLineIndex = index + table.rows.length - 1
    const to = lineStarts[lastLineIndex] + lines[lastLineIndex].length
    if (boundedOffset >= from && boundedOffset <= to) {
      return {
        from,
        fromLine: index + 1,
        table: toMarkdownTableModel(table),
        to,
        toLine: lastLineIndex + 1,
      }
    }

    index += table.rows.length
  }

  return null
}

export function isMarkdownTableRowLine(line: string): boolean {
  const row = parseTableRow(line)
  if (!row) return false
  return row.hasLeadingPipe || row.hasTrailingPipe || splitUnescapedPipesOutsideCode(line).length > 2
}

export function serializeMarkdownTable(table: MarkdownTableModel): string {
  const normalized = normalizeMarkdownTable(table)
  const [header, ...bodyRows] = normalized.rows
  return [
    formatCompactContentRow(header),
    formatCompactSeparatorRow(normalized.alignments),
    ...bodyRows.map(formatCompactContentRow),
  ].join('\n')
}

export function createMarkdownTable(columnPrefix: string, columns = 3, bodyRows = 2): MarkdownTableModel {
  const columnCount = Math.max(2, columns)
  const rowCount = Math.max(1, bodyRows)
  return {
    alignments: Array.from({ length: columnCount }, () => 'none'),
    rows: [
      Array.from({ length: columnCount }, (_cell, index) => `${columnPrefix} ${index + 1}`),
      ...Array.from({ length: rowCount }, () => Array.from({ length: columnCount }, () => '')),
    ],
  }
}

export function updateMarkdownTableCell(
  table: MarkdownTableModel,
  rowIndex: number,
  columnIndex: number,
  value: string,
): MarkdownTableModel {
  const normalized = normalizeMarkdownTable(table)
  if (!normalized.rows[rowIndex] || columnIndex < 0 || columnIndex >= normalized.alignments.length) {
    return normalized
  }

  return {
    ...normalized,
    rows: normalized.rows.map((row, currentRowIndex) => {
      if (currentRowIndex !== rowIndex) return row
      return row.map((cell, currentColumnIndex) => (
        currentColumnIndex === columnIndex ? normalizeCellInput(value) : cell
      ))
    }),
  }
}

export function insertMarkdownTableRow(table: MarkdownTableModel, afterRowIndex: number): MarkdownTableModel {
  const normalized = normalizeMarkdownTable(table)
  const nextRowIndex = Math.max(1, Math.min(afterRowIndex + 1, normalized.rows.length))
  const emptyRow = Array.from({ length: normalized.alignments.length }, () => '')
  return {
    ...normalized,
    rows: [
      ...normalized.rows.slice(0, nextRowIndex),
      emptyRow,
      ...normalized.rows.slice(nextRowIndex),
    ],
  }
}

export function deleteMarkdownTableRow(table: MarkdownTableModel, rowIndex: number): MarkdownTableModel {
  const normalized = normalizeMarkdownTable(table)
  if (rowIndex <= 0 || normalized.rows.length <= 2) return normalized
  return {
    ...normalized,
    rows: normalized.rows.filter((_row, currentRowIndex) => currentRowIndex !== rowIndex),
  }
}

export function insertMarkdownTableColumn(
  table: MarkdownTableModel,
  afterColumnIndex: number,
  headerLabel = '',
): MarkdownTableModel {
  const normalized = normalizeMarkdownTable(table)
  const nextColumnIndex = Math.max(0, Math.min(afterColumnIndex + 1, normalized.alignments.length))
  return {
    alignments: [
      ...normalized.alignments.slice(0, nextColumnIndex),
      'none',
      ...normalized.alignments.slice(nextColumnIndex),
    ],
    rows: normalized.rows.map((row, rowIndex) => [
      ...row.slice(0, nextColumnIndex),
      rowIndex === 0 ? headerLabel : '',
      ...row.slice(nextColumnIndex),
    ]),
  }
}

export function deleteMarkdownTableColumn(table: MarkdownTableModel, columnIndex: number): MarkdownTableModel {
  const normalized = normalizeMarkdownTable(table)
  if (normalized.alignments.length <= 2 || columnIndex < 0 || columnIndex >= normalized.alignments.length) {
    return normalized
  }

  return {
    alignments: normalized.alignments.filter((_alignment, currentColumnIndex) => currentColumnIndex !== columnIndex),
    rows: normalized.rows.map((row) => row.filter((_cell, currentColumnIndex) => currentColumnIndex !== columnIndex)),
  }
}

export function setMarkdownTableAlignment(
  table: MarkdownTableModel,
  columnIndex: number,
  alignment: MarkdownTableAlignment,
): MarkdownTableModel {
  const normalized = normalizeMarkdownTable(table)
  if (columnIndex < 0 || columnIndex >= normalized.alignments.length) return normalized

  return {
    ...normalized,
    alignments: normalized.alignments.map((currentAlignment, currentColumnIndex) => (
      currentColumnIndex === columnIndex ? alignment : currentAlignment
    )),
  }
}

function readTable(
  lines: string[],
  startIndex: number,
): { alignments: MarkdownTableAlignment[]; rows: ParsedTableRow[] } | null {
  const header = parseTableRow(lines[startIndex])
  const separator = parseTableRow(lines[startIndex + 1] ?? '')
  if (!header || !separator || header.cells.length !== separator.cells.length) return null

  const alignments = parseSeparatorCells(separator.cells)
  if (!alignments) return null

  const rows = [header, separator]
  const columnCount = header.cells.length
  let index = startIndex + 2

  while (index < lines.length) {
    const row = parseTableRow(lines[index])
    if (!row) break
    if (row.cells.length !== columnCount) return null
    rows.push(row)
    index += 1
  }

  return { alignments, rows }
}

function parseTableRow(line: string): ParsedTableRow | null {
  if (!line.includes('|')) return null

  const hasLeadingPipe = /^\s*\|/.test(line)
  const hasTrailingPipe = /\|\s*$/.test(line) && !endsWithEscapedPipe(line)
  const cells = splitUnescapedPipesOutsideCode(line)

  if (hasLeadingPipe) cells.shift()
  if (hasTrailingPipe) cells.pop()
  if (cells.length < 2) return null

  return {
    cells: cells.map((cell) => cell.trim()),
    hasLeadingPipe,
    hasTrailingPipe,
  }
}

function splitUnescapedPipesOutsideCode(line: string): string[] {
  const cells: string[] = []
  let current = ''
  let isEscaped = false
  let codeSpanTicks = 0

  for (let index = 0; index < line.length; index += 1) {
    const char = line[index]

    if (isEscaped) {
      current += char
      isEscaped = false
      continue
    }

    if (char === '\\') {
      current += char
      isEscaped = true
      continue
    }

    if (char === '`') {
      const tickLength = countBackticks(line, index)
      if (codeSpanTicks === 0) {
        codeSpanTicks = tickLength
      } else if (tickLength === codeSpanTicks) {
        codeSpanTicks = 0
      }
      current += line.slice(index, index + tickLength)
      index += tickLength - 1
      continue
    }

    if (char === '|' && codeSpanTicks === 0) {
      cells.push(current)
      current = ''
      continue
    }

    current += char
  }

  cells.push(current)
  return cells
}

function countBackticks(line: string, startIndex: number): number {
  let index = startIndex
  while (line[index] === '`') index += 1
  return index - startIndex
}

function endsWithEscapedPipe(line: string): boolean {
  const trimmed = line.trimEnd()
  const pipeIndex = trimmed.length - 1
  if (line[pipeIndex] !== '|') return false
  let slashCount = 0
  for (let index = pipeIndex - 1; index >= 0 && line[index] === '\\'; index -= 1) {
    slashCount += 1
  }
  return slashCount % 2 === 1
}

function parseSeparatorCells(cells: string[]): MarkdownTableAlignment[] | null {
  const alignments = cells.map((cell) => {
    const normalized = cell.replace(/\s/g, '')
    if (!/^:?-{3,}:?$/.test(normalized)) return null
    const starts = normalized.startsWith(':')
    const ends = normalized.endsWith(':')
    if (starts && ends) return 'center'
    if (starts) return 'left'
    if (ends) return 'right'
    return 'none'
  })

  return alignments.every((alignment): alignment is MarkdownTableAlignment => alignment !== null)
    ? alignments
    : null
}

function toMarkdownTableModel(table: { alignments: MarkdownTableAlignment[]; rows: ParsedTableRow[] }): MarkdownTableModel {
  return normalizeMarkdownTable({
    alignments: table.alignments,
    rows: table.rows.filter((_row, index) => index !== 1).map((row) => row.cells),
  })
}

function normalizeMarkdownTable(table: MarkdownTableModel): MarkdownTableModel {
  const columnCount = Math.max(
    2,
    table.alignments.length,
    ...table.rows.map((row) => row.length),
  )
  const rows = table.rows.length > 0 ? table.rows : [Array.from({ length: columnCount }, () => '')]
  const normalizedRows = rows.map((row) => Array.from(
    { length: columnCount },
    (_cell, index) => normalizeCellInput(row[index] ?? ''),
  ))

  while (normalizedRows.length < 2) {
    normalizedRows.push(Array.from({ length: columnCount }, () => ''))
  }

  return {
    alignments: Array.from(
      { length: columnCount },
      (_cell, index) => table.alignments[index] ?? 'none',
    ),
    rows: normalizedRows,
  }
}

function formatCompactContentRow(cells: string[]): string {
  return `| ${cells.map((cell) => escapeMarkdownTableCell(cell)).join(' | ')} |`
}

function formatCompactSeparatorRow(alignments: MarkdownTableAlignment[]): string {
  const cells = alignments.map((alignment) => {
    if (alignment === 'left') return ':---'
    if (alignment === 'right') return '---:'
    if (alignment === 'center') return ':---:'
    return '---'
  })
  return `| ${cells.join(' | ')} |`
}

function normalizeCellInput(value: string): string {
  return value.replace(/\r?\n/g, ' ').replace(/\s+/g, ' ').trim()
}

function escapeMarkdownTableCell(value: string): string {
  let result = ''
  let isEscaped = false
  let codeSpanTicks = 0

  for (let index = 0; index < value.length; index += 1) {
    const char = value[index]

    if (isEscaped) {
      result += char
      isEscaped = false
      continue
    }

    if (char === '\\') {
      result += char
      isEscaped = true
      continue
    }

    if (char === '`') {
      const tickLength = countBackticks(value, index)
      if (codeSpanTicks === 0) {
        codeSpanTicks = tickLength
      } else if (tickLength === codeSpanTicks) {
        codeSpanTicks = 0
      }
      result += value.slice(index, index + tickLength)
      index += tickLength - 1
      continue
    }

    if (char === '|' && codeSpanTicks === 0) {
      result += '\\|'
      continue
    }

    result += char
  }

  return result
}

function markdownLineStarts(markdown: string): number[] {
  const starts = [0]
  for (let index = 0; index < markdown.length; index += 1) {
    if (markdown[index] !== '\n') continue
    starts.push(index + 1)
  }
  return starts
}

function markdownFenceMarker(line: string): string | null {
  return line.match(/^\s*(`{3,}|~{3,})/)?.[1] ?? null
}
