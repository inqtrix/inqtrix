import { describe, expect, it, vi } from 'vitest'
import {
  createFileAssetPlaceholders,
  createFileUploadRegistry,
  ingestFiles,
  runFileIngestPipeline,
  stripServerUploadFailureWarning,
  type FileIngestQueueItem,
} from './ingest'
import type { FileParser, ParsedFile } from './parsing'

const stubParsed: ParsedFile = {
  extractedText: 'hello',
  pageCount: null,
  parseStatus: 'parsed',
  parseWarning: null,
  textTruncated: false,
}

const stubParser: FileParser = {
  parse: async () => stubParsed,
  supports: () => true,
}

function textFile(name: string): File {
  return new File(['hello'], name, { type: 'text/plain' })
}

function readyUpload(serverFileId: string) {
  return {
    error: null,
    operationId: `up_${serverFileId}`,
    serverFileId,
    status: 'ready' as const,
  }
}

describe('ingestFiles', () => {
  it('shapes each file into an asset with the origin and section', async () => {
    const assets = await ingestFiles(
      [textFile('Report.txt')],
      { kind: 'editor', sectionId: 'sec-1' },
      stubParser,
    )
    expect(assets).toHaveLength(1)
    expect(assets[0]).toMatchObject({
      extractedText: 'hello',
      fileName: 'Report.txt',
      label: 'report',
      origin: 'editor',
      sectionId: 'sec-1',
    })
  })

  it('de-duplicates labels against existing and incoming files', async () => {
    const assets = await ingestFiles(
      [textFile('Doc.txt'), textFile('Doc.txt')],
      { kind: 'chat', sectionId: 'sec-1' },
      stubParser,
      ['doc'],
    )
    expect(assets.map((asset) => asset.label)).toEqual(['doc-2', 'doc-3'])
  })

  it('uploads the original bytes and tags the upload as client-parsed', async () => {
    const upload = vi.fn(async () => readyUpload('srv-file-1'))
    const assets = await ingestFiles(
      [textFile('Doc.txt')],
      { kind: 'chat', sectionId: 'sec-1' },
      stubParser,
      [],
      upload,
    )
    expect(assets[0].serverFileId).toBe('srv-file-1')
    expect(assets[0].parseWarning).toBeNull()
    // Upload is fast/local: text is the client parse, provenance is 'client'
    // (it upgrades to 'markitdown' later, at vector-index time).
    expect(assets[0].extractedText).toBe('hello')
    expect(assets[0].parserId).toBe('client')
    expect(upload).toHaveBeenCalledWith(
      expect.any(File),
      expect.objectContaining({
        assetId: assets[0].id,
        groupId: null,
        origin: 'chat',
        sectionId: 'sec-1',
      }),
    )
  })

  it('keeps the asset local with a visible warning when the upload fails', async () => {
    const assets = await ingestFiles(
      [textFile('Doc.txt')],
      { kind: 'chat', sectionId: 'sec-1' },
      stubParser,
      [],
      async () => {
        throw new Error('server down')
      },
    )
    expect(assets[0].serverFileId).toBeNull()
    expect(assets[0].parseWarning).toContain('Server-Upload fehlgeschlagen')
    expect(assets[0].parseWarning).toContain('server down')
    expect(assets[0].extractedText).toBe('hello')
    expect(assets[0].parserId).toBe('client')
  })

  it('stays purely local without a server upload function', async () => {
    const assets = await ingestFiles(
      [textFile('Doc.txt')],
      { kind: 'chat', sectionId: 'sec-1' },
      stubParser,
    )
    expect(assets[0].serverFileId).toBeNull()
    expect(assets[0].parserId).toBe('client')
  })
})

describe('createFileAssetPlaceholders', () => {
  it('creates pending rows synchronously with deduped labels and stable order', () => {
    const { queue, records } = createFileAssetPlaceholders(
      [textFile('Doc.txt'), textFile('Doc.txt'), textFile('Other.txt')],
      { kind: 'library', sectionId: 'sec-1' },
      ['doc'],
      true,
    )
    expect(records.map((record) => record.label)).toEqual(['doc-2', 'doc-3', 'other'])
    expect(records.every((record) => record.parsePending === true)).toBe(true)
    expect(records.every((record) => record.uploadPending === true)).toBe(true)
    expect(records.every((record) => record.extractedText === '')).toBe(true)
    expect(records.every((record) => record.serverFileId === null)).toBe(true)
    expect(queue.map((item) => item.assetId)).toEqual(records.map((record) => record.id))
  })

  it('skips the upload-pending flag when no upload will run (demo/offline)', () => {
    const { records } = createFileAssetPlaceholders(
      [textFile('Doc.txt')],
      { kind: 'library', sectionId: 'sec-1' },
    )
    expect(records[0].uploadPending).toBe(false)
    expect(records[0].parsePending).toBe(true)
  })
})

describe('shared upload registry', () => {
  it('keeps the same browser file and binding available across desk consumers', () => {
    const registry = createFileUploadRegistry()
    const file = textFile('Doc.txt')
    const binding = {
      assetId: 'file-a',
      createdAt: '2026-01-01T00:00:00.000Z',
      groupId: null,
      label: 'doc',
      origin: 'chat' as const,
      sectionId: 'section-a',
      title: 'Doc.txt',
      updatedAt: '2026-01-01T00:00:00.000Z',
    }
    registry.register('file-a', { binding, file })

    expect(registry.get('file-a')).toEqual({ binding, file })
    expect(registry.has('file-a')).toBe(true)
    registry.delete('file-a')
    expect(registry.has('file-a')).toBe(false)
  })
})

describe('runFileIngestPipeline', () => {
  const parsedOk: ParsedFile = stubParsed

  function makeQueue(n: number): FileIngestQueueItem[] {
    return Array.from({ length: n }, (_, i) => ({
      assetId: `a${i}`,
      file: textFile(`f${i}.txt`),
    }))
  }

  it('bounds upload parallelism and settles every file', async () => {
    const events: string[] = []
    let inFlight = 0
    let maxInFlight = 0
    await runFileIngestPipeline(
      makeQueue(8),
      {
        needsClientParse: () => true,
        onParsed: (id) => events.push(`parsed:${id}`),
        onUploadFailed: (id) => events.push(`upload-failed:${id}`),
        onUploadAccepted: (id) => events.push(`uploaded:${id}`),
        parse: async () => parsedOk,
        serverParseWillRun: () => false,
        upload: async (item) => {
          inFlight += 1
          maxInFlight = Math.max(maxInFlight, inFlight)
          await new Promise((resolve) => setTimeout(resolve, 1))
          inFlight -= 1
          return readyUpload(`fl_${item.assetId}`)
        },
      },
      { uploadConcurrency: 3 },
    )
    expect(maxInFlight).toBeLessThanOrEqual(3)
    expect(events.filter((event) => event.startsWith('uploaded:'))).toHaveLength(8)
    expect(events.filter((event) => event.startsWith('parsed:'))).toHaveLength(8)
  })

  it('reports a failed upload and still parses the file locally', async () => {
    const events: string[] = []
    await runFileIngestPipeline(makeQueue(2), {
      needsClientParse: () => true,
      onParsed: (id, _parsed, clearParsePending) =>
        events.push(`parsed:${id}:${clearParsePending}`),
      onUploadFailed: (id, message) => events.push(`upload-failed:${id}:${message}`),
      onUploadAccepted: (id) => events.push(`uploaded:${id}`),
      parse: async () => parsedOk,
      serverParseWillRun: (_id, uploaded) => uploaded,
      upload: async (item) => {
        if (item.assetId === 'a0') throw new Error('server down')
        return readyUpload(`fl_${item.assetId}`)
      },
    })
    const failed = events.find((event) => event.startsWith('upload-failed:a0'))
    expect(failed).toContain('Server-Upload fehlgeschlagen')
    expect(failed).toContain('server down')
    // The local-only file must clear the badge itself; the uploaded file
    // hands it over to the background server parse.
    expect(events).toContain('parsed:a0:true')
    expect(events).toContain('parsed:a1:false')
  })

  it('keeps an accepted queued operation pending while completing the local parse', async () => {
    const accepted: Array<{ assetId: string; status: string }> = []
    const parsed: Array<{ assetId: string; clear: boolean }> = []
    await runFileIngestPipeline(makeQueue(1), {
      needsClientParse: () => true,
      onParsed: (assetId, _result, clear) => parsed.push({ assetId, clear }),
      onUploadAccepted: (assetId, result) => accepted.push({ assetId, status: result.status }),
      onUploadFailed: () => undefined,
      parse: async () => parsedOk,
      serverParseWillRun: (_assetId, uploaded) => uploaded,
      upload: async () => ({
        error: 'storage unavailable',
        operationId: 'up_1',
        serverFileId: null,
        status: 'retrying',
      }),
    })

    expect(accepted).toEqual([{ assetId: 'a0', status: 'retrying' }])
    expect(parsed).toEqual([{ assetId: 'a0', clear: true }])
  })

  it('hands parse feedback to a 202 server preparation without declaring it ready', async () => {
    const accepted: Array<{ assetId: string; status: string }> = []
    const parsed: Array<{ assetId: string; clear: boolean }> = []
    await runFileIngestPipeline(makeQueue(1), {
      needsClientParse: () => true,
      onParsed: (assetId, _result, clear) => parsed.push({ assetId, clear }),
      onUploadAccepted: (assetId, result) => accepted.push({ assetId, status: result.status }),
      onUploadFailed: () => undefined,
      parse: async () => parsedOk,
      serverParseWillRun: (_assetId, acceptedByServer) => acceptedByServer,
      upload: async () => ({
        error: null,
        operationId: 'up_parse',
        serverFileId: 'fl_parse',
        status: 'parsing',
      }),
    })

    expect(accepted).toEqual([{ assetId: 'a0', status: 'parsing' }])
    expect(parsed).toEqual([{ assetId: 'a0', clear: false }])
  })

  it('parses serially even while uploads run in parallel', async () => {
    let parsing = 0
    let maxParsing = 0
    await runFileIngestPipeline(
      makeQueue(5),
      {
        needsClientParse: () => true,
        onParsed: () => undefined,
        onUploadFailed: () => undefined,
        onUploadAccepted: () => undefined,
        parse: async () => {
          parsing += 1
          maxParsing = Math.max(maxParsing, parsing)
          await new Promise((resolve) => setTimeout(resolve, 1))
          parsing -= 1
          return parsedOk
        },
        serverParseWillRun: () => false,
        upload: async (item) => readyUpload(`fl_${item.assetId}`),
      },
      { uploadConcurrency: 4 },
    )
    expect(maxParsing).toBe(1)
  })

  it('skips the client parse once the server text made it obsolete', async () => {
    const parsedIds: string[] = []
    await runFileIngestPipeline(makeQueue(2), {
      // a0's MarkItDown text already landed by the time its turn comes.
      needsClientParse: (id) => id !== 'a0',
      onParsed: (id) => parsedIds.push(id),
      onUploadFailed: () => undefined,
      onUploadAccepted: () => undefined,
      parse: async () => parsedOk,
      serverParseWillRun: () => true,
      upload: async (item) => readyUpload(`fl_${item.assetId}`),
    })
    expect(parsedIds).toEqual(['a1'])
  })

  it('turns a rejecting parser into a visible error result, never a rejection', async () => {
    const results: ParsedFile[] = []
    await runFileIngestPipeline(makeQueue(1), {
      needsClientParse: () => true,
      onParsed: (_id, parsed) => results.push(parsed),
      onUploadFailed: () => undefined,
      onUploadAccepted: () => undefined,
      parse: async () => {
        throw new Error('pdf.js exploded')
      },
      serverParseWillRun: () => false,
    })
    expect(results).toHaveLength(1)
    expect(results[0].parseStatus).toBe('error')
    expect(results[0].parseWarning).toContain('pdf.js exploded')
  })

  it('serializes parsing ACROSS overlapping pipeline invocations', async () => {
    let parsing = 0
    let maxParsing = 0
    const handlers = {
      needsClientParse: () => true,
      onParsed: () => undefined,
      onUploadFailed: () => undefined,
      onUploadAccepted: () => undefined,
      parse: async () => {
        parsing += 1
        maxParsing = Math.max(maxParsing, parsing)
        await new Promise((resolve) => setTimeout(resolve, 1))
        parsing -= 1
        return parsedOk
      },
      serverParseWillRun: () => false,
    }
    // Two batches dropped back to back (each its own pipeline) must still
    // never run two heavy client parses at once.
    await Promise.all([
      runFileIngestPipeline(makeQueue(3), handlers),
      runFileIngestPipeline(
        [{ assetId: 'b0', file: textFile('b0.txt') }, { assetId: 'b1', file: textFile('b1.txt') }],
        handlers,
      ),
    ])
    expect(maxParsing).toBe(1)
  })

  it('runs the client parse immediately when there is no upload path', async () => {
    const events: string[] = []
    await runFileIngestPipeline(makeQueue(2), {
      needsClientParse: () => true,
      onParsed: (id, _parsed, clearParsePending) =>
        events.push(`parsed:${id}:${clearParsePending}`),
      onUploadFailed: () => undefined,
      onUploadAccepted: () => undefined,
      parse: async () => parsedOk,
      serverParseWillRun: () => false,
    })
    expect(events).toEqual(['parsed:a0:true', 'parsed:a1:true'])
  })
})

describe('stripServerUploadFailureWarning', () => {
  it('removes the failure trace and nulls an empty remainder', () => {
    expect(stripServerUploadFailureWarning(
      'Server-Upload fehlgeschlagen (HTTP 503 (retry)) — Datei bleibt lokal.',
    )).toBeNull()
  })

  it('keeps unrelated warnings intact', () => {
    expect(stripServerUploadFailureWarning(
      'Nur ein Teil wurde verarbeitet. Server-Upload fehlgeschlagen (503) — Datei bleibt lokal.',
    )).toBe('Nur ein Teil wurde verarbeitet.')
    expect(stripServerUploadFailureWarning('Nur ein Teil wurde verarbeitet.')).toBe('Nur ein Teil wurde verarbeitet.')
    expect(stripServerUploadFailureWarning(null)).toBeNull()
  })
})
