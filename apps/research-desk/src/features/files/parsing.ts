import type { FileParseStatus } from '@/features/project/types'
import { clampText, emptyTextWarning, truncationWarning } from './parseResult'
import { configurePdfWorker } from './pdfWorker'

export type ParsedFile = {
  extractedText: string
  pageCount: number | null
  parseStatus: FileParseStatus
  parseWarning: string | null
  textTruncated: boolean
}

/**
 * Swappable document parser. The default implementation parses client-side
 * (PDF via pdfjs-dist, DOCX via mammoth, plain text natively); a future backend
 * parser can implement the same interface without touching the data model.
 */
export interface FileParser {
  parse(file: File): Promise<ParsedFile>
  supports(file: File): boolean
}

const TEXT_EXTENSIONS = new Set(['txt', 'md', 'markdown', 'csv', 'tsv', 'json', 'log', 'text'])
const TEXT_MIME_EXACT = new Set(['application/json', 'application/csv'])

function extensionOf(file: File): string {
  const dot = file.name.lastIndexOf('.')
  return dot >= 0 ? file.name.slice(dot + 1).toLowerCase() : ''
}

function isPdf(file: File): boolean {
  return file.type === 'application/pdf' || extensionOf(file) === 'pdf'
}

function isDocx(file: File): boolean {
  return file.type === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
    || extensionOf(file) === 'docx'
}

function isText(file: File): boolean {
  if (file.type.startsWith('text/')) return true
  if (TEXT_MIME_EXACT.has(file.type)) return true
  return TEXT_EXTENSIONS.has(extensionOf(file))
}

/**
 * Turn raw extracted text into a `ParsedFile`. Single finalisation point so an
 * empty extraction (e.g. a scanned PDF without a text layer) or a truncation is
 * always surfaced as a visible status, never silently dropped.
 */
function finalizeText(rawText: string, pageCount: number | null): ParsedFile {
  const trimmed = rawText.trim()
  if (!trimmed) {
    return {
      extractedText: '',
      pageCount,
      parseStatus: 'partial',
      parseWarning: emptyTextWarning,
      textTruncated: false,
    }
  }
  const { extractedText, textTruncated } = clampText(trimmed)
  return {
    extractedText,
    pageCount,
    parseStatus: textTruncated ? 'partial' : 'parsed',
    parseWarning: textTruncated ? truncationWarning : null,
    textTruncated,
  }
}

async function parsePdf(file: File): Promise<ParsedFile> {
  const pdfjs = await import('pdfjs-dist')
  configurePdfWorker(pdfjs)
  const data = new Uint8Array(await file.arrayBuffer())
  const doc = await pdfjs.getDocument({ data }).promise
  const pageCount = doc.numPages
  const parts: string[] = []
  for (let pageNumber = 1; pageNumber <= pageCount; pageNumber += 1) {
    const page = await doc.getPage(pageNumber)
    const content = await page.getTextContent()
    parts.push(content.items.map((item) => ('str' in item ? item.str : '')).join(' '))
  }
  return finalizeText(parts.join('\n\n'), pageCount)
}

async function parseDocx(file: File): Promise<ParsedFile> {
  const mammoth = await import('mammoth')
  const result = await mammoth.extractRawText({ arrayBuffer: await file.arrayBuffer() })
  return finalizeText(result.value, null)
}

async function parseText(file: File): Promise<ParsedFile> {
  return finalizeText(await file.text(), null)
}

/**
 * Default client-side parser. PDF and DOCX dependencies are lazy-imported so
 * they never enter the initial bundle.
 */
export function createDefaultFileParser(): FileParser {
  return {
    supports(file) {
      return isPdf(file) || isDocx(file) || isText(file)
    },
    async parse(file) {
      try {
        if (isPdf(file)) return await parsePdf(file)
        if (isDocx(file)) return await parseDocx(file)
        if (isText(file)) return await parseText(file)
        return {
          extractedText: '',
          pageCount: null,
          parseStatus: 'unsupported',
          parseWarning: `Dateityp wird nicht unterstützt (${file.type || extensionOf(file) || 'unbekannt'}).`,
          textTruncated: false,
        }
      } catch (error) {
        return {
          extractedText: '',
          pageCount: null,
          parseStatus: 'error',
          parseWarning: `Datei konnte nicht gelesen werden: ${error instanceof Error ? error.message : String(error)}`,
          textTruncated: false,
        }
      }
    },
  }
}
