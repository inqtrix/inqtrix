import { copyTextToClipboard } from '@/lib/clipboard'

export const MARKDOWN_BLOCK_EXPORT_PADDING_PX = 24
export const MARKDOWN_BLOCK_EXPORT_PIXEL_RATIO = 3
export const MARKDOWN_BLOCK_FILE_NAMES = {
  diagramPng: 'inqtrix-diagram.png',
  tableCsv: 'inqtrix-table.csv',
  tablePng: 'inqtrix-table.png',
} as const

export type MarkdownSourcePosition = {
  end?: {
    offset?: number
  }
  start?: {
    offset?: number
  }
}

export type MarkdownBlockCaptureMetrics = {
  contentHeight: number
  contentWidth: number
  exportHeight: number
  exportWidth: number
  padding: number
  pixelRatio: number
}

export type MarkdownBlockPngOptions = {
  backgroundColor: string
  height: number
  pixelRatio: number
  style: {
    boxSizing: 'border-box'
    height: string
    margin: '0'
    maxWidth: 'none'
    overflow: 'visible'
    padding: string
    width: string
  }
  width: number
}

/**
 * Returns the exact Markdown span represented by a parsed syntax-tree node.
 *
 * Invalid or missing offsets deliberately return `null`: reconstructing a
 * table from rendered cells would silently discard alignment and inline
 * Markdown, violating the source-copy contract.
 */
export function markdownSourceFromPosition(
  source: string,
  position: MarkdownSourcePosition | null | undefined,
): string | null {
  const start = position?.start?.offset
  const end = position?.end?.offset
  if (
    !Number.isInteger(start)
    || !Number.isInteger(end)
    || start === undefined
    || end === undefined
    || start < 0
    || end <= start
    || end > source.length
  ) {
    return null
  }
  return source.slice(start, end)
}

/** Serializes visible table cells as UTF-8/RFC-style comma-separated rows. */
export function serializeMarkdownTableCsv(rows: readonly (readonly string[])[]): string {
  return rows
    .map((row) => row.map(escapeCsvCell).join(','))
    .join('\r\n')
}

export function tableRowsFromElement(table: HTMLTableElement): string[][] {
  return Array.from(table.rows, (row) => (
    Array.from(row.cells, (cell) => cell.innerText.trim())
  ))
}

export function markdownBlockCaptureMetrics(
  node: Pick<HTMLElement, 'clientHeight' | 'clientWidth' | 'scrollHeight' | 'scrollWidth'>,
): MarkdownBlockCaptureMetrics {
  const contentWidth = Math.max(node.scrollWidth, node.clientWidth, 1)
  const contentHeight = Math.max(node.scrollHeight, node.clientHeight, 1)
  return {
    contentHeight,
    contentWidth,
    exportHeight: contentHeight + MARKDOWN_BLOCK_EXPORT_PADDING_PX * 2,
    exportWidth: contentWidth + MARKDOWN_BLOCK_EXPORT_PADDING_PX * 2,
    padding: MARKDOWN_BLOCK_EXPORT_PADDING_PX,
    pixelRatio: MARKDOWN_BLOCK_EXPORT_PIXEL_RATIO,
  }
}

export function markdownBlockPngOptions(
  node: Pick<HTMLElement, 'clientHeight' | 'clientWidth' | 'scrollHeight' | 'scrollWidth'>,
  backgroundColor: string,
): MarkdownBlockPngOptions {
  const metrics = markdownBlockCaptureMetrics(node)
  return {
    backgroundColor,
    height: metrics.exportHeight,
    pixelRatio: metrics.pixelRatio,
    style: {
      boxSizing: 'border-box',
      height: `${metrics.exportHeight}px`,
      // The capture inlines the node's COMPUTED styles, and a centered
      // block (`margin-inline: auto`) computes to the pixel offset that
      // centred it inside its original, much wider column. Re-rendered at
      // the block's own width that offset pushes the whole drawing outside
      // the image — a silently empty PNG. The capture frame is the block
      // itself, so it is never centred in anything: pin the margin to zero.
      margin: '0',
      maxWidth: 'none',
      overflow: 'visible',
      padding: `${metrics.padding}px`,
      width: `${metrics.exportWidth}px`,
    },
    width: metrics.exportWidth,
  }
}

export async function copyMarkdownBlockText(text: string): Promise<void> {
  if (!(await copyTextToClipboard(text))) {
    throw new Error('Browser rejected the clipboard operation.')
  }
}

export function downloadMarkdownBlockBlob(blob: Blob, fileName: string): void {
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.download = fileName
  link.href = url
  document.body.appendChild(link)
  link.click()
  link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 0)
}

export function downloadMarkdownTableCsv(table: HTMLTableElement): void {
  const csv = serializeMarkdownTableCsv(tableRowsFromElement(table))
  downloadMarkdownBlockBlob(
    new Blob([csv], { type: 'text/csv;charset=utf-8' }),
    MARKDOWN_BLOCK_FILE_NAMES.tableCsv,
  )
}

export async function downloadMarkdownBlockPng(
  node: HTMLElement,
  fileName: typeof MARKDOWN_BLOCK_FILE_NAMES.diagramPng | typeof MARKDOWN_BLOCK_FILE_NAMES.tablePng,
): Promise<void> {
  const backgroundColor = getComputedStyle(document.documentElement)
    .getPropertyValue('--background')
    .trim() || '#ffffff'
  const { toBlob } = await import('html-to-image')
  const blob = await toBlob(node, markdownBlockPngOptions(node, backgroundColor))
  if (!blob) {
    throw new Error('PNG renderer returned no image data.')
  }
  downloadMarkdownBlockBlob(blob, fileName)
}

function escapeCsvCell(value: string): string {
  if (!/[",\r\n]/.test(value)) return value
  return `"${value.replace(/"/g, '""')}"`
}
