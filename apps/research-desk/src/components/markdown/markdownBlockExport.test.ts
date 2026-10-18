import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { toBlobMock } = vi.hoisted(() => ({
  toBlobMock: vi.fn(),
}))

vi.mock('html-to-image', () => ({
  toBlob: toBlobMock,
}))

import {
  MARKDOWN_BLOCK_EXPORT_PADDING_PX,
  MARKDOWN_BLOCK_EXPORT_PIXEL_RATIO,
  MARKDOWN_BLOCK_FILE_NAMES,
  downloadMarkdownBlockPng,
  downloadMarkdownTableCsv,
  markdownBlockCaptureMetrics,
  markdownBlockPngOptions,
} from './markdownBlockExport'

type FakeAnchor = {
  click: ReturnType<typeof vi.fn>
  download: string
  href: string
  remove: ReturnType<typeof vi.fn>
}

let anchor: FakeAnchor
let downloadedFileNames: string[]
let objectUrlMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  downloadedFileNames = []
  anchor = {
    click: vi.fn(() => downloadedFileNames.push(anchor.download)),
    download: '',
    href: '',
    remove: vi.fn(),
  }
  objectUrlMock = vi.fn(() => 'blob:markdown-export')
  toBlobMock.mockReset()
  toBlobMock.mockResolvedValue(new Blob(['png'], { type: 'image/png' }))
  vi.stubGlobal('document', {
    body: { appendChild: vi.fn() },
    createElement: vi.fn(() => anchor),
    documentElement: {},
  })
  vi.stubGlobal('getComputedStyle', vi.fn(() => ({
    getPropertyValue: () => 'oklch(0.98 0.01 250)',
  })))
  vi.stubGlobal('URL', {
    createObjectURL: objectUrlMock,
    revokeObjectURL: vi.fn(),
  })
  vi.stubGlobal('window', {
    setTimeout: vi.fn((callback: () => void) => {
      callback()
      return 1
    }),
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('markdown block PNG export metrics', () => {
  it('captures the full scroll extent with stable high-resolution padding', () => {
    expect(markdownBlockCaptureMetrics({
      clientHeight: 240,
      clientWidth: 640,
      scrollHeight: 240,
      scrollWidth: 1280,
    })).toEqual({
      contentHeight: 240,
      contentWidth: 1280,
      exportHeight: 240 + MARKDOWN_BLOCK_EXPORT_PADDING_PX * 2,
      exportWidth: 1280 + MARKDOWN_BLOCK_EXPORT_PADDING_PX * 2,
      padding: MARKDOWN_BLOCK_EXPORT_PADDING_PX,
      pixelRatio: MARKDOWN_BLOCK_EXPORT_PIXEL_RATIO,
    })
  })

  it('never emits a zero-sized canvas', () => {
    expect(markdownBlockCaptureMetrics({
      clientHeight: 0,
      clientWidth: 0,
      scrollHeight: 0,
      scrollWidth: 0,
    }).contentWidth).toBe(1)
  })

  it('builds full-size options that clear source max-width before adding padding', () => {
    expect(markdownBlockPngOptions({
      clientHeight: 240,
      clientWidth: 640,
      scrollHeight: 300,
      scrollWidth: 1280,
    }, 'oklch(0.98 0.01 250)')).toEqual({
      backgroundColor: 'oklch(0.98 0.01 250)',
      height: 348,
      pixelRatio: 3,
      style: {
        boxSizing: 'border-box',
        height: '348px',
        margin: '0',
        maxWidth: 'none',
        overflow: 'visible',
        padding: '24px',
        width: '1328px',
      },
      width: 1328,
    })
  })

  it('neutralizes the centring margin the capture would otherwise inherit', () => {
    // A centred block computes `margin: 0px 404px` inside a wide column;
    // carried into a capture frame of the block's own width, that offset
    // moves every drawn pixel outside the image.
    expect(markdownBlockPngOptions({
      clientHeight: 121,
      clientWidth: 88,
      scrollHeight: 121,
      scrollWidth: 88,
    }, '#ffffff').style.margin).toBe('0')
  })

  it('passes the effective PNG contract to html-to-image and downloads the returned Blob', async () => {
    const node = {
      clientHeight: 240,
      clientWidth: 640,
      scrollHeight: 300,
      scrollWidth: 1280,
    } as HTMLElement

    await downloadMarkdownBlockPng(node, MARKDOWN_BLOCK_FILE_NAMES.tablePng)

    expect(toBlobMock).toHaveBeenCalledWith(
      node,
      markdownBlockPngOptions(node, 'oklch(0.98 0.01 250)'),
    )
    expect(objectUrlMock).toHaveBeenCalledWith(expect.any(Blob))
    expect(downloadedFileNames).toEqual(['inqtrix-table.png'])
  })

  it('downloads visible table cells as UTF-8 CSV under the stable file name', async () => {
    const table = {
      rows: [
        { cells: [{ innerText: 'Name' }, { innerText: 'Notiz' }] },
        { cells: [{ innerText: 'Größe' }, { innerText: '1,5' }] },
      ],
    } as unknown as HTMLTableElement

    downloadMarkdownTableCsv(table)

    const blob = objectUrlMock.mock.calls[0]?.[0] as Blob
    expect(blob.type).toBe('text/csv;charset=utf-8')
    expect(await blob.text()).toBe('Name,Notiz\r\nGröße,"1,5"')
    expect(downloadedFileNames).toEqual(['inqtrix-table.csv'])
  })
})
