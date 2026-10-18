import type {
  FileAssetOrigin,
  FileAssetRecord,
  FileAssetUploadStatus,
} from '@/features/project/types'
import { createProjectEntityId } from '@/features/project/entityId'
import { createDefaultFileParser, type FileParser, type ParsedFile } from './parsing'

export type IngestOrigin = {
  groupId?: string | null
  kind: FileAssetOrigin
  sectionId: string
}

function slugifyFileName(fileName: string): string {
  const base = fileName.replace(/\.[^.]+$/, '')
  const slug = base
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[̀-ͯ]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48)
  return slug || 'datei'
}

function uniqueLabel(base: string, used: Set<string>): string {
  if (!used.has(base)) {
    used.add(base)
    return base
  }
  let suffix = 2
  while (used.has(`${base}-${suffix}`)) suffix += 1
  const label = `${base}-${suffix}`
  used.add(label)
  return label
}

/** Stable target binding shared by the reservation and byte-transfer calls.
 * The server persists the section-bound asset identity before `POST /v1/files`
 * moves bytes; the upload operation then advances that same aggregate through
 * durable checkpoints. A reload can therefore recover the reserved row and
 * operation without inventing another file identity. Timestamps stay ISO here;
 * the API layer converts them to the wire's unix seconds. */
export type UploadBinding = {
  assetId: string
  createdAt: string
  groupId: string | null
  label: string
  origin: FileAssetOrigin
  sectionId: string
  title: string
  updatedAt: string
}

/**
 * The single ingest pipeline used by every upload entry point (chat paperclip,
 * chat drag-and-drop, editor drag-and-drop, library upload). Each file is parsed
 * CLIENT-SIDE for instant feedback and shaped into a `FileAssetRecord`; the
 * caller dispatches the records and decides what to do with the returned ids.
 * Labels are de-duplicated against `existingLabels` so mention tokens stay
 * unique across the library.
 *
 * The client parse is display-only. When an original is uploaded, canonical
 * server preparation continues in the durable upload operation; knowledge
 * indexing later references that operation-fenced asset source and never
 * promotes this browser result to source authority.
 */
/** Authoritative projection returned by the bound upload endpoint. A resolved
 * promise can mean either fully ready or durably queued; callers must preserve
 * the latter as an in-progress server operation rather than presenting it as
 * a completed upload. */
export type FileUploadResult = {
  error: string | null
  operationId: string | null
  serverFileId: string | null
  status: FileAssetUploadStatus
}

export type ServerFileUpload = (
  file: File,
  binding: UploadBinding,
) => Promise<FileUploadResult>

/** The visible failure trace when the original bytes could not reach the
 * server. One wording for every ingest path (batch and pipeline), so the
 * persisted warning and the transient badge always read the same. */
export function serverUploadFailureMessage(error: unknown): string {
  return `Server-Upload fehlgeschlagen (${
    error instanceof Error ? error.message : 'unbekannt'
  }) — die Datei kann erst nach einem erfolgreichen Upload verwendet werden.`
}

/** Remove a persisted {@link serverUploadFailureMessage} trace from a parse
 * warning — a later SUCCESSFUL upload makes the "Datei bleibt lokal" claim
 * false, so the retry's completion must retract it. Returns null when nothing
 * else remains. */
export function stripServerUploadFailureWarning(warning: string | null): string | null {
  if (!warning) return null
  const cleaned = warning
    .replace(/Server-Upload fehlgeschlagen \([\s\S]*?\) — Datei bleibt lokal\./g, '')
    .replace(
      /Server-Upload fehlgeschlagen \([\s\S]*?\) — der lokal extrahierte Inhalt bleibt verfügbar\./g,
      '',
    )
    .replace(
      /Server-Upload fehlgeschlagen \([\s\S]*?\) — die Datei kann erst nach einem erfolgreichen Upload verwendet werden\./g,
      '',
    )
    .replace(/\s{2,}/g, ' ')
    .trim()
  return cleaned || null
}

export async function ingestFiles(
  files: readonly File[],
  origin: IngestOrigin,
  parser: FileParser = createDefaultFileParser(),
  existingLabels: readonly string[] = [],
  serverUpload?: ServerFileUpload,
): Promise<FileAssetRecord[]> {
  const used = new Set(existingLabels)
  const sectionId = origin.sectionId
  const groupId = origin.groupId ?? null
  const records: FileAssetRecord[] = []

  for (const file of files) {
    const now = new Date().toISOString()
    const id = createProjectEntityId('file')
    const label = uniqueLabel(slugifyFileName(file.name), used)
    const parsed = await parser.parse(file)
    // Progressive enhancement: when the backend advertises the files
    // feature, the ORIGINAL bytes go to the server too (enables the
    // higher-fidelity server parse + ingestion at vector-index time).
    // A failed upload leaves the reserved server aggregate visible and
    // retryable. Normal Chat/Editor consumers gate on this durable lifecycle;
    // only the explicit incognito flow may use a local parse without binding.
    let serverFileId: string | null = null
    let uploadOperationId: string | null = null
    let uploadStatus: FileAssetUploadStatus | undefined
    let serverWarning: string | null = null
    if (serverUpload) {
      try {
        const upload = await serverUpload(file, {
          assetId: id,
          createdAt: now,
          groupId,
          label,
          origin: origin.kind,
          sectionId,
          title: file.name,
          updatedAt: now,
        })
        serverFileId = upload.serverFileId
        uploadOperationId = upload.operationId
        uploadStatus = upload.status
        serverWarning = upload.error
      } catch (error) {
        serverWarning = serverUploadFailureMessage(error)
        uploadStatus = 'failed'
      }
    }
    records.push({
      createdAt: now,
      extractedText: parsed.extractedText,
      serverFileId,
      // Uploaded here = browser-parsed. The provenance upgrades to
      // 'markitdown' (with the text) when the file is indexed server-side.
      parserId: 'client',
      fileName: file.name,
      groupId,
      id,
      label,
      mimeType: file.type || 'application/octet-stream',
      origin: origin.kind,
      pageCount: parsed.pageCount,
      parseStatus: parsed.parseStatus,
      parseWarning: serverWarning
        ? parsed.parseWarning
          ? `${parsed.parseWarning} ${serverWarning}`
          : serverWarning
        : parsed.parseWarning,
      sectionId,
      sizeBytes: file.size,
      textTruncated: parsed.textTruncated,
      title: file.name,
      updatedAt: now,
      uploadError: uploadStatus === 'failed' || uploadStatus === 'cancelled'
        ? serverWarning
        : null,
      uploadOperationId,
      uploadPending: uploadStatus !== undefined
        && uploadStatus !== 'ready'
        && uploadStatus !== 'failed'
        && uploadStatus !== 'cancelled',
      uploadStatus,
    })
  }

  return records
}

export type FileIngestQueueItem = { assetId: string; file: File }

export type PendingFileUpload = { binding: UploadBinding; file: File }

/** Session-local byte handles for explicit retry/reselect flows. The durable
 * operation itself remains server-owned; this registry only retains browser
 * File objects, which cannot be serialized across reloads. One instance is
 * shared by Library, Chat, and Editor so navigation does not orphan a retry. */
export function createFileUploadRegistry() {
  const entries = new Map<string, PendingFileUpload>()
  return {
    clear: () => entries.clear(),
    delete: (assetId: string) => entries.delete(assetId),
    get: (assetId: string) => entries.get(assetId),
    has: (assetId: string) => entries.has(assetId),
    register: (assetId: string, entry: PendingFileUpload) => {
      entries.set(assetId, entry)
    },
  }
}

export type FileUploadRegistry = ReturnType<typeof createFileUploadRegistry>

/** Derive the immutable server binding from the placeholder itself. Keeping
 * this mapping next to placeholder creation prevents Library, Chat, and Editor
 * from drifting in which identity/timestamps they bind before byte transfer. */
export function uploadBindingForRecord(record: FileAssetRecord): UploadBinding {
  return {
    assetId: record.id,
    createdAt: record.createdAt,
    groupId: record.groupId,
    label: record.label,
    origin: record.origin,
    sectionId: record.sectionId,
    title: record.title,
    updatedAt: record.updatedAt,
  }
}

/**
 * Build placeholder records for a fresh library upload batch, SYNCHRONOUSLY —
 * the caller dispatches them before any parse or upload starts, so every
 * selected file appears in the list immediately (the pipeline then settles
 * each row via the per-file reducer actions). Labels are deduped exactly like
 * {@link ingestFiles}; ids are final (they seed the upload binding).
 */
export function createFileAssetPlaceholders(
  files: readonly File[],
  origin: IngestOrigin,
  existingLabels: readonly string[] = [],
  willUpload = false,
): { queue: FileIngestQueueItem[]; records: FileAssetRecord[] } {
  const used = new Set(existingLabels)
  const records: FileAssetRecord[] = []
  const queue: FileIngestQueueItem[] = []
  for (const file of files) {
    const now = new Date().toISOString()
    const id = createProjectEntityId('file')
    records.push({
      createdAt: now,
      extractedText: '',
      fileName: file.name,
      groupId: origin.groupId ?? null,
      id,
      label: uniqueLabel(slugifyFileName(file.name), used),
      mimeType: file.type || 'application/octet-stream',
      origin: origin.kind,
      pageCount: null,
      // Neutral until a parse settles; the pending flags own the badge.
      parseStatus: 'parsed',
      parseWarning: null,
      parsePending: true,
      parserId: null,
      sectionId: origin.sectionId,
      serverFileId: null,
      sizeBytes: file.size,
      textTruncated: false,
      title: file.name,
      updatedAt: now,
      uploadPending: willUpload,
      uploadStatus: willUpload ? 'awaiting_upload' : undefined,
    })
    queue.push({ assetId: id, file })
  }
  return { queue, records }
}

/** Side-effect callbacks for {@link runFileIngestPipeline}, kept abstract so
 * the pipeline imports neither the API client nor the reducer. */
export type FileIngestPipelineHandlers = {
  /** Still worth running the client parse? The caller answers from live
   * state (false once the server MarkItDown text already landed, making
   * the expensive in-browser parse pure waste). */
  needsClientParse: (assetId: string) => boolean
  /** A client parse settled. `clearParsePending=false` hands the
   * "Parsing…" badge over to a still-running background server parse. */
  onParsed: (assetId: string, parsed: ParsedFile, clearParsePending: boolean) => void
  /** The upload failed (visible warning + retry; the reservation remains). */
  onUploadFailed: (assetId: string, message: string) => void
  /** The server accepted the upload lifecycle. It may already be ready or be
   * durably queued for recovery; both projections must reach application
   * state. */
  onUploadAccepted: (assetId: string, result: FileUploadResult) => void
  parse: (file: File) => Promise<ParsedFile>
  /** Will a background server parse deliver text for this asset? Decides
   * who clears the "Parsing…" badge. */
  serverParseWillRun: (assetId: string, uploaded: boolean) => boolean
  upload?: (item: FileIngestQueueItem) => Promise<FileUploadResult>
}

const yieldToMainThread = () => new Promise<void>((resolve) => setTimeout(resolve, 0))

// ONE serial parse lane for the whole app, across pipeline invocations: two
// overlapping batches (drop while a drop still parses) must not run two
// pdf.js/mammoth extractions on the main thread at once. Every link is
// failure-wrapped, so the chain never rejects.
let clientParseChain: Promise<void> = Promise.resolve()

/**
 * Run a placeholder batch through upload + client parse. Uploads use a small
 * worker pool (bounded parallelism — network-bound, cheap); the client parse
 * stays STRICTLY SERIAL app-wide and yields between files, because PDF/DOCX
 * extraction does main-thread work and running it in parallel starves the UI
 * (the historical all-at-once batch froze the app for the whole selection).
 * Each file's parse is queued only after its upload settles, so feedback and
 * the other uploads never wait behind a heavy parse. Never rejects: every
 * failure lands in a handler.
 */
export async function runFileIngestPipeline(
  queue: readonly FileIngestQueueItem[],
  handlers: FileIngestPipelineHandlers,
  options: { uploadConcurrency?: number } = {},
): Promise<void> {
  const concurrency = Math.max(1, Math.min(options.uploadConcurrency ?? 3, queue.length))
  const parseTasks: Promise<void>[] = []
  const enqueueParse = (item: FileIngestQueueItem, uploaded: boolean) => {
    const clearParsePending = !handlers.serverParseWillRun(item.assetId, uploaded)
    clientParseChain = clientParseChain.then(async () => {
      await yieldToMainThread()
      if (!handlers.needsClientParse(item.assetId)) return
      let parsed: ParsedFile
      try {
        parsed = await handlers.parse(item.file)
      } catch (error) {
        parsed = {
          extractedText: '',
          pageCount: null,
          parseStatus: 'error',
          parseWarning: error instanceof Error ? error.message : 'Parse fehlgeschlagen',
          textTruncated: false,
        }
      }
      handlers.onParsed(item.assetId, parsed, clearParsePending)
    })
    parseTasks.push(clientParseChain)
  }
  let cursor = 0
  const workers = Array.from({ length: concurrency }, async () => {
    while (cursor < queue.length) {
      const item = queue[cursor]
      cursor += 1
      let serverSourceAccepted = false
      if (handlers.upload) {
        try {
          const result = await handlers.upload(item)
          serverSourceAccepted = result.serverFileId !== null
            && result.status !== 'failed'
            && result.status !== 'cancelled'
          handlers.onUploadAccepted(item.assetId, result)
        } catch (error) {
          handlers.onUploadFailed(item.assetId, serverUploadFailureMessage(error))
        }
      }
      enqueueParse(item, serverSourceAccepted)
    }
  })
  await Promise.all(workers)
  await Promise.all(parseTasks)
}
