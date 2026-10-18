import { describe, expect, it } from 'vitest'

import { createDefaultFileParser } from './parsing'

/**
 * Contract tests for the default client-side file parser.
 *
 * Scope note: the actual PDF/DOCX EXTRACTION (pdfjs / mammoth) is intentionally
 * NOT exercised here — it needs a browser runtime (File/Worker/canvas) that the
 * pure `node` vitest environment does not provide, which is why the rest of the
 * file feature stubs the parser (see ingest.test.ts). The real pdfjs path is
 * covered by live browser verification. What IS deterministic and worth guarding
 * is the parser's observable status contract for the non-PDF paths.
 */
describe('createDefaultFileParser', () => {
  const parser = createDefaultFileParser()

  function file(name: string, type: string, body = ''): File {
    return new File([body], name, { type })
  }

  it('parses a plain text file into extracted text', async () => {
    const result = await parser.parse(file('notes.txt', 'text/plain', 'Hello world'))
    expect(result.parseStatus).toBe('parsed')
    expect(result.extractedText).toContain('Hello world')
    expect(result.parseWarning).toBeNull()
  })

  it('marks an empty/whitespace document as partial with a visible warning', async () => {
    const result = await parser.parse(file('blank.txt', 'text/plain', '   \n  '))
    expect(result.parseStatus).toBe('partial')
    expect(result.extractedText).toBe('')
    expect(result.parseWarning).toBeTruthy()
  })

  it('flags an unsupported file type without throwing', async () => {
    const result = await parser.parse(file('archive.zip', 'application/zip'))
    expect(result.parseStatus).toBe('unsupported')
    expect(result.parseWarning).toBeTruthy()
  })

  it('treats known text extensions without a MIME type as text', () => {
    expect(parser.supports(file('data.csv', ''))).toBe(true)
    expect(parser.supports(file('image.png', 'image/png'))).toBe(false)
  })
})
