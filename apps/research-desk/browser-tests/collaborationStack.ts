import { createHash, randomUUID } from 'node:crypto'
import type { AddressInfo } from 'node:net'

import { expect, test, type Browser, type ConsoleMessage, type Locator, type Page } from '@playwright/test'
import {
  EDITOR_SCHEMA_VERSION,
  SUGGESTION_MARK_NAMES,
  editorCollaborationRoom,
  editorJsonToYDoc,
  editorYDocToJson,
  getEditorSchemaFingerprint,
  serializeEditorJson,
  type EditorProjectionMode,
} from '@inqtrix/editor-schema'
import type { JSONContent } from '@tiptap/core'
import WebSocket, { WebSocketServer } from 'ws'
import * as Y from 'yjs'

import type {
  DurableUpdateLookup,
  IntrospectedLease,
  LoadedDocumentState,
  LogFields,
  PersistUpdateInput,
  PersistUpdateResult,
  SidecarLogger,
} from '../../collaboration-server/src/contracts'
import { CollaborationError } from '../../collaboration-server/src/errors'
import { SidecarMetrics } from '../../collaboration-server/src/metrics'
import type { CollaborationOperations } from '../../collaboration-server/src/operations'
import { CollaborationSidecar } from '../../collaboration-server/src/sidecar'
import {
  FakeCollaborationApi,
  documentState,
  markdownDocument,
  settings,
} from '../../collaboration-server/tests/helpers'

// A real collaboration service in the Playwright worker, a store double behind
// it, and the browser's socket carried to it frame by frame, so a browser
// test sees exactly which frames a page sends and which the service answers.

export type FixtureWriter = {
  color: string
  id: string
  key: string
  name: string
}

export const WRITER_A: FixtureWriter = {
  color: '#2563EB',
  id: '11111111-1111-4111-8111-111111111111',
  key: 'writer-a',
  name: 'Ada',
}
export const WRITER_B: FixtureWriter = {
  color: '#047857',
  id: '22222222-2222-4222-8222-222222222222',
  key: 'writer-b',
  name: 'Lin',
}
export const WRITER_C: FixtureWriter = {
  color: '#B45309',
  id: '33333333-3333-4333-8333-333333333333',
  key: 'writer-c',
  name: 'Kim',
}
/** A writer whose colour is bright, so the text on its caret label must be
 * dark to stay readable. */
export const WRITER_D: FixtureWriter = {
  color: '#FACC15',
  id: '44444444-4444-4444-8444-444444444444',
  key: 'writer-d',
  name: 'Sol',
}
const WRITERS = [WRITER_A, WRITER_B, WRITER_C, WRITER_D]

export type WriterAccess = 'edit' | 'suggest'

/** A document the store holds at start, as Markdown or, for content
 * Markdown cannot carry (pending suggestions), as editor JSON. */
export type StackDocument =
  | { id: string; markdown: string }
  | { content: JSONContent; id: string }

export type TransportClose = { code: number; reason: string }

/** One Hocuspocus frame as the socket carried it. */
export type FrameRecord = {
  /** When the frame passed the socket's relay, in this worker's clock. */
  at: number
  /** `out`: from the page to the service; `in`: from the service. */
  direction: 'in' | 'out'
  /** For an update and a durable acknowledgement: the update's SHA-256. */
  hash: string | null
  kind: FrameKind
  /** For an update: the Yjs update the frame carries. */
  payload: Uint8Array | null
  room: string | null
}

export type FrameKind =
  | 'ack'
  | 'awareness'
  | 'other'
  | 'stateless'
  | 'sync-answer'
  | 'sync-request'
  | 'update'

/** Everything one page socket carried, in order. */
export class TransportRecord {
  readonly closes: TransportClose[] = []
  readonly frames: FrameRecord[] = []
  /** The service's transport id of this socket, once it is known. */
  transportId: string | null = null
  /** The most updates the page ever had sent on this socket and not seen
   * acknowledged. */
  maxUnacknowledged = 0
  /** The socket's path died silently at some point (SocketBridge). */
  silenced = false
  /** The socket opened and was never answered (SocketBridge). */
  stalled = false
  private readonly unacknowledged = new Set<string>()

  record(direction: FrameRecord['direction'], bytes: Uint8Array): void {
    const frame = decodeFrame(direction, bytes)
    this.frames.push(frame)
    if (frame.kind === 'update' && frame.direction === 'out' && frame.hash !== null) {
      this.unacknowledged.add(frame.hash)
      this.maxUnacknowledged = Math.max(this.maxUnacknowledged, this.unacknowledged.size)
    }
    if (frame.kind === 'ack' && frame.hash !== null) this.unacknowledged.delete(frame.hash)
  }

  /** The updates the page sent on this socket. */
  updates(room?: string): FrameRecord[] {
    return this.frames.filter((frame) => (
      frame.kind === 'update'
      && frame.direction === 'out'
      && (room === undefined || frame.room === room)
    ))
  }

  acks(): FrameRecord[] {
    return this.frames.filter((frame) => frame.kind === 'ack')
  }
}

type PerDocumentStore = {
  committed: Map<string, number>
  /** Every row the store committed, with what the service sent for it. */
  rows: Array<{ at: number; input: PersistUpdateInput; sequence: number }>
  sequence: number
  state: LoadedDocumentState
}

export type CollaborationStackOptions = {
  documents: readonly StackDocument[]
  /** How long every store write takes, like a network round trip to it. */
  persistDelayMs?: number
}

/**
 * A real collaboration sidecar in the test worker. The store double answers
 * a repeated update hash as a duplicate with its original sequence and looks
 * updates up from what it committed, as the durable store does; a narrower
 * double could not acknowledge a replayed update the store already holds.
 * Every store call crosses the event loop like a network round trip; a double
 * that answers within one turn would let the sidecar send a whole queue of
 * replies before any socket write completes, a burst the real store never
 * produces.
 */
export class CollaborationStack {
  readonly api = new FakeCollaborationApi()
  // The production queue bounds: held persistence has to meet the same
  // ingress bound the service applies, and the other writers' outbound bound
  // (the same setting) must hold the resulting broadcast burst.
  readonly configured = settings({
    maintenanceIntervalMs: 60_000,
    policyPollMs: 60_000,
  })
  readonly metrics = new SidecarMetrics()
  readonly sidecar: CollaborationSidecar
  /** The service's warnings and errors, in order. */
  readonly warnings: Array<{ event: string; fields: LogFields | undefined }> = []
  private readonly stores = new Map<string, PerDocumentStore>()
  private hold: { promise: Promise<void>; release: () => void } | null = null
  private readonly persistDelayMs: number

  private constructor(persistDelayMs: number) {
    this.persistDelayMs = persistDelayMs
    const record = (event: string, fields?: LogFields): void => {
      this.warnings.push({ event, fields })
    }
    const logger: SidecarLogger = {
      debug: () => undefined,
      error: record,
      info: () => undefined,
      warn: record,
    }
    this.sidecar = new CollaborationSidecar(this.configured, {
      api: this.api,
      logger,
      metrics: this.metrics,
    })
    this.api.persistImplementation = async (input: PersistUpdateInput) => (
      this.persist(input)
    )
    // The instance lease is extended on every renewal, as the API does; a
    // fixed lease would take the service down a minute into a long test.
    this.api.renewInstance = async () => {
      this.api.fence = {
        ...this.api.fence,
        leaseExpiresAt: Date.now() / 1_000 + this.configured.instanceLeaseSeconds,
      }
      return this.api.fence
    }
    this.api.loadDocumentState = async (input) => {
      if (input) this.api.loads.push(input)
      await storeRoundTrip()
      return this.store(input?.documentId ?? '').state
    }
    this.api.lookupUpdates = async (input) => {
      this.api.lookups.push(input)
      await storeRoundTrip()
      const committed = this.store(input.documentId).committed
      return input.hashes.flatMap((hash): DurableUpdateLookup[] => {
        const sequence = committed.get(hash)
        return sequence === undefined ? [] : [{ hash, sequence }]
      })
    }
  }

  static async start(options: CollaborationStackOptions): Promise<CollaborationStack> {
    const stack = new CollaborationStack(options.persistDelayMs ?? 0)
    for (const document of options.documents) {
      const seed = 'content' in document
        ? editorJsonToYDoc(document.content)
        : markdownDocument(document.markdown)
      stack.stores.set(document.id, {
        committed: new Map(),
        rows: [],
        sequence: 0,
        state: await documentState(document.id, seed),
      })
      seed.destroy()
    }
    const schemaHash = await getEditorSchemaFingerprint()
    stack.api.introspectLease = async (input?: { token: string }) => (
      stack.lease(parseLeaseToken(input?.token ?? ''), schemaHash)
    )
    await stack.sidecar.start()
    return stack
  }

  get port(): number {
    const port = this.sidecar.address?.port
    if (!port) throw new Error('The collaboration sidecar is not listening')
    return port
  }

  /** The session the API would answer for this writer and document. */
  session(writer: FixtureWriter, access: WriterAccess, documentId: string): Record<string, unknown> {
    this.store(documentId)
    return {
      access,
      expires_at: Math.floor(Date.now() / 1_000) + 3_600,
      initial_write_mode: access,
      lease_token: leaseToken(writer, access, documentId),
      max_frame_bytes: this.configured.frameLimitBytes,
      max_queued_bytes: this.configured.maxQueuedBytes,
      max_queued_frames: this.configured.maxQueuedFrames,
      protocol_version: this.configured.protocolVersion,
      room: editorCollaborationRoom(documentId, 1),
      schema_version: EDITOR_SCHEMA_VERSION,
      update_rate_count: this.configured.updateRateLimit,
      update_rate_window_seconds: this.configured.updateRateWindowMs / 1_000,
      user: { color: writer.color, id: writer.id, name: writer.name },
      websocket_path: this.configured.websocketPath,
    }
  }

  holdPersistence(): void {
    let release = (): void => undefined
    const promise = new Promise<void>((resolve) => {
      release = resolve
    })
    this.hold = { promise, release }
  }

  releasePersistence(): void {
    this.hold?.release()
    this.hold = null
  }

  /** The document the store holds right now, replayed from its rows. */
  storeDocument(documentId: string): Y.Doc {
    const state = this.store(documentId).state
    const document = new Y.Doc()
    if (state.snapshot) Y.applyUpdate(document, state.snapshot.stateUpdate)
    for (const update of state.updates) Y.applyUpdate(document, update.update)
    return document
  }

  /** The stored document's text in one projection. */
  storeText(documentId: string, projection: EditorProjectionMode = 'final'): string {
    const document = this.storeDocument(documentId)
    try {
      return projectionText(document, projection)
    } finally {
      document.destroy()
    }
  }

  /** The rows the store committed, in order, as the service sent them. */
  storedRows(documentId: string): ReadonlyArray<{ input: PersistUpdateInput; sequence: number }> {
    return this.store(documentId).rows
  }

  /**
   * The history the activity endpoint answers for a document, newest first:
   * each row with its actor and the suggestions it removed, each named
   * with the author of the patch that proposed it. A row that changed
   * nothing a reader sees stays stored and is no entry, as in the service.
   */
  activity(documentId: string): Array<Record<string, unknown>> {
    const rows = this.store(documentId).rows
    const proposals = new Map<string, { authorId: string; patchId: string }>()
    for (const row of rows) {
      for (const patch of row.input.patches) {
        for (const suggestionId of patch.activeSuggestionIds) {
          proposals.set(suggestionId, { authorId: patch.authorId, patchId: patch.patchId })
        }
      }
    }
    return [...rows].reverse().filter((row) => row.input.changeSummary.changesDocument).map((row) => ({
      actor: activityActor(row.input.actorUserId),
      actor_kind: row.input.actorKind,
      command_id: row.input.commandId ?? null,
      created_at: row.at,
      from_sequence: row.sequence,
      outcome: row.input.decisionOutcome,
      rejected_suggestion_count: row.input.rejectedSuggestionIds.length,
      rejected_suggestions: row.input.rejectedSuggestionIds.map((suggestionId) => {
        const proposal = proposals.get(suggestionId)
        if (!proposal) throw new Error(`No stored patch proposed ${suggestionId}`)
        return {
          author: activityActor(proposal.authorId),
          patch_id: proposal.patchId,
          suggestion_id: suggestionId,
        }
      }),
      suggestion_ids: row.input.suggestionIds,
      summary: {
        edits: row.input.changeSummary.edits,
        omitted_edit_count: row.input.changeSummary.omittedEditCount,
      },
      to_sequence: row.sequence,
      type: row.input.changeKind,
      update_count: 1,
    }))
  }

  /**
   * What the API does when a writer with edit access accepts or rejects
   * patches: the service's own decision operation, in that writer's name,
   * against the sequence the store holds now.
   */
  async decide(
    documentId: string,
    decider: FixtureWriter,
    decision: 'accept' | 'reject',
    patchIds: readonly string[],
  ): Promise<void> {
    await this.operations().decide(documentId, {
      actor_user_id: decider.id,
      command_id: randomUUID(),
      decision,
      expected_sequence: this.store(documentId).sequence,
      generation: 1,
      patch_ids: [...patchIds],
    })
  }

  /**
   * What the API's projection barrier answers a browser: the service's own
   * projection of the room at its current sequence, in the API's fields.
   */
  async browserProjection(documentId: string): Promise<Record<string, unknown>> {
    const projection = await this.operations().project(documentId, {
      generation: 1,
      minimum_sequence: 0,
    })
    return {
      authoritative_sequence: projection.sequence,
      content_markdown: projection.projection_markdown,
      generation: projection.generation,
      projection_hash: projection.projection_hash,
      sequence: projection.sequence,
    }
  }

  /**
   * What the API does with a browser's AI publication: the fields the API
   * passes on reach the service's own operation in the writer's name, and a
   * refusal comes back in the API's error envelope with the service's
   * reason, a conflict for every refusal and an outage for a failure.
   */
  async browserPublication(
    documentId: string,
    writer: FixtureWriter,
    body: Readonly<Record<string, unknown>>,
  ): Promise<{ json: Record<string, unknown>; status: number }> {
    const forwarded: Record<string, unknown> = {
      actor_kind: body.actor_kind,
      actor_user_id: writer.id,
      command_id: body.command_id,
      expected_sequence: body.expected_sequence,
      generation: 1,
      patch_id: body.patch_id,
      target_markdown: body.target_markdown,
    }
    if (body.base_markdown !== undefined) forwarded.base_markdown = body.base_markdown
    try {
      return { json: await this.operations().publishSuggestion(documentId, forwarded), status: 200 }
    } catch (error) {
      if (!(error instanceof CollaborationError)) throw error
      if (error.httpStatus >= 500) {
        return {
          json: { error: { message: 'Live collaboration is currently unavailable.', reason: 'collaboration_unavailable', type: 'service_unavailable' } },
          status: 503,
        }
      }
      const reason = error.reason === 'upstream_conflict' && error.upstreamReason
        ? error.upstreamReason
        : error.reason
      return {
        json: { error: { message: `The change was rejected (reason: ${reason}).`, reason, type: 'conflict' } },
        status: 409,
      }
    }
  }

  /** How many update rows the store committed for a document. */
  storedUpdateCount(documentId: string): number {
    return this.store(documentId).state.updates.length
  }

  /** The value of a counter or gauge the service reports, or 0 when the
   * series does not exist; the render proves the registry is readable. */
  metric(name: string, labels: Readonly<Record<string, string>> = {}): number {
    const rendered = this.metrics.render()
    if (!rendered.includes('inqtrix_collaboration_active_connections')) {
      throw new Error('The collaboration service metrics are not being recorded')
    }
    const encoded = Object.entries(labels)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([key, value]) => `${key}="${value}"`)
      .join(',')
    const series = encoded ? `${name}{${encoded}}` : name
    const line = rendered.split('\n').find((candidate) => candidate.startsWith(`${series} `))
    return line === undefined ? 0 : Number(line.slice(series.length + 1))
  }

  /** Every refusal the service counted: `update:<reason>` for a refused
   * update or message, `ingress:<reason>` for a socket closed at its inbound
   * bounds, `outbound:<reason>` for a socket closed because it did not take
   * what the service sent it. */
  rejections(): Record<string, number> {
    const counts: Record<string, number> = {}
    for (const line of this.metrics.render().split('\n')) {
      const match = /^inqtrix_collaboration_(?:(ingress|outbound)_)?rejections_total\{reason="([^"]+)"\} (\d+)$/
        .exec(line)
      if (match) counts[`${match[1] ?? 'update'}:${match[2]!}`] = Number(match[3])
    }
    return counts
  }

  /** Transport ids the sidecar currently holds open. */
  transportIds(): string[] {
    return [...(this.sidecar as unknown as { sockets: Map<string, WebSocket> }).sockets.keys()]
  }

  closeTransport(transportId: string, code: number, reason: string): void {
    const socket = (this.sidecar as unknown as { sockets: Map<string, WebSocket> })
      .sockets.get(transportId)
    if (!socket) throw new Error('The transport to close is not open')
    socket.close(code, reason)
  }

  async stop(): Promise<void> {
    this.releasePersistence()
    await this.sidecar.stop()
  }

  private operations(): CollaborationOperations {
    return (this.sidecar as unknown as { operations: CollaborationOperations }).operations
  }

  private store(documentId: string): PerDocumentStore {
    const store = this.stores.get(documentId)
    if (!store) throw new Error(`No fixture document ${documentId}`)
    return store
  }

  private async persist(input: PersistUpdateInput): Promise<PersistUpdateResult> {
    await this.hold?.promise
    if (this.persistDelayMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, this.persistDelayMs))
    }
    await storeRoundTrip()
    const store = this.store(input.documentId)
    const original = store.committed.get(input.hash)
    if (original !== undefined) {
      return { duplicate: true, persistedSequence: store.sequence, sequence: original }
    }
    store.sequence += 1
    store.committed.set(input.hash, store.sequence)
    store.rows.push({ at: Date.now() / 1_000, input, sequence: store.sequence })
    store.state = {
      ...store.state,
      persistedSequence: store.sequence,
      updates: [
        ...store.state.updates,
        { hash: input.hash, sequence: store.sequence, update: input.update },
      ],
    }
    return { duplicate: false, persistedSequence: store.sequence, sequence: store.sequence }
  }

  private lease(
    token: { access: WriterAccess; documentId: string; writer: FixtureWriter },
    schemaHash: string,
  ): IntrospectedLease {
    return {
      documentId: token.documentId,
      expiresAt: Date.now() / 1_000 + 3_600,
      generation: 1,
      leaseId: leaseToken(token.writer, token.access, token.documentId),
      permission: token.access,
      policyCursor: 0,
      protocolVersion: this.configured.protocolVersion,
      schemaHash,
      schemaVersion: this.configured.schemaVersion,
      sessionId: leaseToken(token.writer, token.access, token.documentId),
      tenantId: this.configured.tenantId,
      user: { color: token.writer.color, id: token.writer.id, name: token.writer.name },
    }
  }
}

/** A row's actor as the activity endpoint names it. */
function activityActor(id: string): Record<string, unknown> {
  const writer = WRITERS.find((candidate) => candidate.id === id)
  if (!writer) throw new Error(`No fixture writer ${id}`)
  return { id: writer.id, name: writer.name }
}

function leaseToken(writer: FixtureWriter, access: WriterAccess, documentId: string): string {
  return `lease.${writer.key}.${access}.${documentId}`
}

function parseLeaseToken(token: string): {
  access: WriterAccess
  documentId: string
  writer: FixtureWriter
} {
  const [prefix, key, access, documentId] = token.split('.')
  const writer = WRITERS.find((candidate) => candidate.key === key)
  if (
    prefix !== 'lease'
    || !writer
    || (access !== 'edit' && access !== 'suggest')
    || !documentId
  ) throw new Error('Unknown fixture lease token')
  return { access, documentId, writer }
}

function storeRoundTrip(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

/** A document's text in one projection, as the store would render it. */
export function projectionText(document: Y.Doc, projection: EditorProjectionMode): string {
  return serializeEditorJson(editorYDocToJson(document), projection).trim()
}

/** Suggestions of a document by id, with the kind of mark and the author. */
export function suggestionsOf(document: Y.Doc): string[] {
  const found = new Set<string>()
  const visit = (node: unknown): void => {
    if (!node || typeof node !== 'object') return
    const record = node as { content?: unknown[]; marks?: Array<{ attrs?: Record<string, unknown>; type?: string }> }
    for (const mark of record.marks ?? []) {
      if (!mark.type || !SUGGESTION_MARK_NAMES.has(mark.type)) continue
      found.add(`${mark.type}:${String(mark.attrs?.suggestionId)}:${String(mark.attrs?.authorId)}`)
    }
    for (const child of record.content ?? []) visit(child)
  }
  visit(editorYDocToJson(document))
  return [...found].sort()
}

// Hocuspocus message types and y-protocols sync steps the records name.
const MESSAGE_SYNC = 0
const MESSAGE_AWARENESS = 1
const MESSAGE_STATELESS = 5
const SYNC_REQUEST = 0
const SYNC_ANSWER = 1
const SYNC_UPDATE = 2

function decodeFrame(direction: FrameRecord['direction'], bytes: Uint8Array): FrameRecord {
  const frame: FrameRecord = {
    at: Date.now(),
    direction,
    hash: null,
    kind: 'other',
    payload: null,
    room: null,
  }
  const reader = new FrameReader(bytes)
  const room = reader.string()
  const type = reader.varUint()
  if (room === null || type === null) return frame
  frame.room = room
  if (type === MESSAGE_AWARENESS) {
    frame.kind = 'awareness'
  } else if (type === MESSAGE_SYNC) {
    const step = reader.varUint()
    if (step === SYNC_REQUEST) frame.kind = 'sync-request'
    if (step === SYNC_ANSWER) frame.kind = 'sync-answer'
    if (step === SYNC_UPDATE) {
      const payload = reader.bytes()
      if (payload !== null) {
        frame.kind = 'update'
        frame.payload = payload
        frame.hash = createHash('sha256').update(payload).digest('hex')
      }
    }
  } else if (type === MESSAGE_STATELESS) {
    frame.kind = 'stateless'
    const text = reader.string()
    if (text !== null) {
      try {
        const message = JSON.parse(text) as { hash?: unknown; type?: unknown }
        if (message.type === 'durable_ack' && typeof message.hash === 'string') {
          frame.kind = 'ack'
          frame.hash = message.hash
        }
      } catch {
        // Not JSON: another stateless payload.
      }
    }
  }
  return frame
}

class FrameReader {
  private offset = 0
  private readonly source: Uint8Array

  constructor(source: Uint8Array) {
    this.source = source
  }

  varUint(): number | null {
    let value = 0
    let factor = 1
    while (this.offset < this.source.byteLength) {
      const byte = this.source[this.offset]!
      this.offset += 1
      value += (byte & 0x7f) * factor
      if ((byte & 0x80) === 0) return value
      factor *= 128
    }
    return null
  }

  bytes(): Uint8Array | null {
    const length = this.varUint()
    if (length === null || this.offset + length > this.source.byteLength) return null
    const slice = this.source.slice(this.offset, this.offset + length)
    this.offset += length
    return slice
  }

  string(): string | null {
    const slice = this.bytes()
    return slice === null ? null : new TextDecoder().decode(slice)
  }
}

/** The characters the update of a frame inserts, in struct order. */
export function insertedText(update: Uint8Array): string {
  let text = ''
  for (const struct of Y.decodeUpdate(update).structs) {
    if (struct instanceof Y.Item && struct.content instanceof Y.ContentString) {
      text += struct.content.str
    }
  }
  return text
}

export function gatewayCloseCode(code: number): number {
  if ([1000, 1001, 1002, 1003, 1008, 1009, 1011, 1012].includes(code)) return code
  if (code >= 4000 && code <= 4999) return code
  return 1011
}

/** Opens a socket to the sidecar with the bearer secret and records what it
 * carries both ways; frames written before it is open are sent in order once
 * it is. */
function openUpstream(
  stack: CollaborationStack,
  record: TransportRecord,
  handlers: {
    /** While true, nothing reaches the page and nothing leaves for the
     * service; the page's frames are still recorded as sent. */
    isSilent?: () => boolean
    onClose: (code: number, reason: string) => void
    onMessage: (bytes: Buffer) => void
  },
): { close: (code?: number) => void; send: (bytes: Buffer) => void } {
  const known = new Set(stack.transportIds())
  const upstream = new WebSocket(`ws://127.0.0.1:${stack.port}${stack.configured.websocketPath}`, {
    headers: { Authorization: `Bearer ${stack.configured.secret}` },
  })
  const pending: Buffer[] = []
  let closeAfterOpen: number | null = null
  upstream.on('open', () => {
    record.transportId = stack.transportIds().find((id) => !known.has(id)) ?? null
    for (const bytes of pending.splice(0)) upstream.send(bytes, { binary: true })
    if (closeAfterOpen !== null) upstream.close(closeAfterOpen)
  })
  upstream.on('message', (data) => {
    if (handlers.isSilent?.()) return
    const bytes = Buffer.isBuffer(data) ? data : Buffer.from(data as ArrayBuffer)
    record.record('in', bytes)
    handlers.onMessage(bytes)
  })
  upstream.on('close', (code, reason) => {
    const text = reason.toString('utf8')
    record.closes.push({ code, reason: text })
    handlers.onClose(code, text)
  })
  upstream.on('error', () => undefined)
  return {
    close: (code = 1000) => {
      if (upstream.readyState === WebSocket.OPEN) upstream.close(code)
      else if (upstream.readyState === WebSocket.CONNECTING) closeAfterOpen = code
    },
    send: (bytes) => {
      record.record('out', bytes)
      if (handlers.isSilent?.()) return
      if (upstream.readyState === WebSocket.OPEN) upstream.send(bytes, { binary: true })
      else pending.push(bytes)
    },
  }
}

/**
 * A stand-in for the gateway on its own port, for pages whose collaboration
 * socket is the engine's own WebSocket: it forwards every frame in order and
 * closes the upstream socket with the browser's code only after the frames
 * the browser sent before its close.
 */
export class CollaborationRelay {
  readonly transports: TransportRecord[] = []
  private readonly server: WebSocketServer

  private constructor(server: WebSocketServer, stack: CollaborationStack) {
    this.server = server
    server.on('connection', (browserSocket) => {
      const record = new TransportRecord()
      this.transports.push(record)
      const upstream = openUpstream(stack, record, {
        onClose: (code, reason) => {
          if (browserSocket.readyState === WebSocket.OPEN) {
            browserSocket.close(gatewayCloseCode(code), reason.slice(0, 123))
          }
        },
        onMessage: (bytes) => {
          if (browserSocket.readyState === WebSocket.OPEN) browserSocket.send(bytes, { binary: true })
        },
      })
      browserSocket.on('message', (data) => {
        upstream.send(Buffer.isBuffer(data) ? data : Buffer.from(data as ArrayBuffer))
      })
      browserSocket.on('close', (code) => upstream.close(code >= 1000 && code < 5000 && code !== 1005 && code !== 1006 ? code : 1000))
      browserSocket.on('error', () => undefined)
    })
  }

  static async start(stack: CollaborationStack): Promise<CollaborationRelay> {
    const server = new WebSocketServer({ host: '127.0.0.1', port: 0 })
    await new Promise<void>((resolve) => server.once('listening', () => resolve()))
    return new CollaborationRelay(server, stack)
  }

  get port(): number {
    return (this.server.address() as AddressInfo).port
  }

  async stop(): Promise<void> {
    for (const client of this.server.clients) client.terminate()
    await new Promise<void>((resolve) => this.server.close(() => resolve()))
  }
}

/**
 * The network between a page and the service, for the faults a real path
 * produces: a path that dies silently (frames vanish both ways and neither
 * side sees a close), and a socket that opens but is never answered. Only
 * for pages whose socket the harness routes (no relay).
 */
export class SocketBridge {
  private readonly silencers: Array<() => void> = []
  private stalls = 0

  /** The path of the page's current socket dies silently. */
  silenceCurrent(): void {
    const silence = this.silencers.at(-1)
    if (!silence) throw new Error('The page has no routed socket to silence')
    silence()
  }

  /** The page's next sockets open and are never answered. */
  stallNext(count = 1): void {
    this.stalls += count
  }

  /** Called by the route for every new socket. */
  admit(silence: () => void): 'stall' | 'forward' {
    if (this.stalls > 0) {
      this.stalls -= 1
      return 'stall'
    }
    this.silencers.push(silence)
    return 'forward'
  }
}

export type WriterPage = {
  access: WriterAccess
  /** Faults of the path between the page and the service. */
  bridge: SocketBridge
  /** Console errors of the page, in order. */
  consoleErrors: string[]
  editor: Locator
  page: Page
  state: Locator
  status: Locator
  /** The fixture's test ids of the state list, the status and the surface. */
  testIds: { state: string; status: string; surface: string }
  /** Every socket of the page, in connection order. */
  transports: TransportRecord[]
  writer: FixtureWriter
}

/** A private AI suggestion the AI publication page anchors in its live
 * document: replace `find`, which stands between `before` and `after`. */
export type AiSuggestionSeed = {
  after: string
  before: string
  find: string
  /** A second replacement of the same run, anchored like the first. */
  after2?: string
  before2?: string
  find2?: string
  replace2?: string
  /** The row the suggestion hangs on (`origin2`: the second one's): the
   * machine carrier of an assistant run, or a note the user wrote. Without
   * it a suggestion has none. */
  origin?: 'carrier' | 'note'
  origin2?: 'carrier' | 'note'
  replace: string
}

export type OpenWriterOptions = {
  access?: WriterAccess
  /** Opens the writer with the AI suggestion flow of the editor workspace
   * for this suggestion (the page's `ai-seed` control creates it). */
  aiSuggestion?: AiSuggestionSeed
  /** The document the page opens first. */
  documentId: string
  /**
   * `writer`: the writer fixture with its write-mode, leave and document
   * controls. `reconnect`: the reconnect fixture, which counts the editor
   * instances the page created and has text outside the editor; it always
   * opens the document `reconnect-doc`.
   */
  fixture?: 'reconnect' | 'writer'
  /** A second document the page's switch control opens. */
  alternateDocumentId?: string
  /** The page's clock is installed before it loads, so a test can move the
   * page's timers forward (`page.clock`). */
  clock?: boolean
  /** The page shows the inspector's history below the editor, loaded from
   * the activity the stack's store holds. */
  history?: boolean
  /** The page connects through this relay with the engine's own WebSocket. */
  relay?: CollaborationRelay
  /** The seed text the editor shows once it is connected. */
  seedText: string
}

/**
 * Opens the writer fixture for one writer. The session endpoint answers as
 * the API does; the socket is carried to the sidecar with the bearer secret,
 * frames flow both ways unchanged, and the sidecar's close codes and reasons
 * reach the page.
 */
export async function openWriter(
  browser: Browser,
  stack: CollaborationStack,
  writer: FixtureWriter,
  options: OpenWriterOptions,
): Promise<WriterPage> {
  const access = options.access ?? 'edit'
  const context = await browser.newContext()
  await context.addInitScript(() => {
    window.localStorage.setItem('inqtrix.researchDesk.locale', 'de')
  })
  const page = await context.newPage()
  page.on('pageerror', (error) => {
    throw error
  })
  const consoleErrors: string[] = []
  page.on('console', (message: ConsoleMessage) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  const transports: TransportRecord[] = options.relay?.transports ?? []
  const bridge = new SocketBridge()
  if (options.clock) await page.clock.install()
  await page.route('**/v1/editor/documents/*/collaboration/session', async (route) => {
    const documentId = /\/v1\/editor\/documents\/([^/]+)\/collaboration\/session/
      .exec(new URL(route.request().url()).pathname)?.[1]
    if (!documentId) throw new Error('The session request names no document')
    await route.fulfill({
      contentType: 'application/json',
      json: stack.session(writer, access, decodeURIComponent(documentId)),
    })
  })
  if (!options.relay) {
    await page.routeWebSocket('**/collaboration', (route) => {
      const record = new TransportRecord()
      transports.push(record)
      const bytesOf = (message: string | Buffer): Buffer => (
        typeof message === 'string' ? Buffer.from(message, 'utf8') : message
      )
      let silent = false
      if (bridge.admit(() => {
        silent = true
        record.silenced = true
      }) === 'stall') {
        // Opened for the page, never answered, and a close from the page
        // reaches nobody.
        record.stalled = true
        route.onMessage((message) => record.record('out', bytesOf(message)))
        route.onClose(() => undefined)
        return
      }
      const upstream = openUpstream(stack, record, {
        isSilent: () => silent,
        onClose: (code, reason) => {
          if (silent) return
          void route.close({ code: gatewayCloseCode(code), reason: reason.slice(0, 123) })
            .catch(() => undefined)
        },
        onMessage: (bytes) => route.send(bytes),
      })
      route.onMessage((message) => upstream.send(bytesOf(message)))
      // A silent path forwards no close either way: the page's own close
      // never reaches the service, and the page never hears of one.
      route.onClose(() => {
        if (!silent) upstream.close()
      })
    })
  }
  const fixture = options.fixture ?? 'writer'
  if (fixture === 'reconnect' && options.documentId !== 'reconnect-doc') {
    throw new Error('The reconnect fixture opens the document reconnect-doc')
  }
  const query = new URLSearchParams(fixture === 'reconnect'
    ? { scenario: 'reconnect-continuity' }
    : options.aiSuggestion
      ? { ...options.aiSuggestion, document: options.documentId, scenario: 'ai-publication' }
      : { document: options.documentId, scenario: 'collaboration-writer' })
  if (options.alternateDocumentId) query.set('alternate', options.alternateDocumentId)
  if (options.relay) query.set('relay', String(options.relay.port))
  if (options.history) {
    query.set('history', '1')
    await page.route('**/v1/editor/documents/*/activity?*', async (route) => {
      const documentId = /\/v1\/editor\/documents\/([^/]+)\/activity/
        .exec(new URL(route.request().url()).pathname)?.[1]
      if (!documentId) throw new Error('The activity request names no document')
      await route.fulfill({
        json: { data: stack.activity(decodeURIComponent(documentId)), next_cursor: null, object: 'list' },
      })
    })
  }
  await page.goto(`/browser-tests/fixtures/editor-lifecycle.html?${query}`)
  const testIds = fixture === 'reconnect'
    ? { state: 'reconnect-state', status: 'reconnect-status', surface: 'reconnect-surface' }
    : { state: 'writer-state', status: 'writer-status', surface: 'writer-surface' }
  const writerPage: WriterPage = {
    access,
    bridge,
    consoleErrors,
    editor: page.getByTestId(testIds.surface).locator('.ProseMirror'),
    page,
    state: page.getByTestId(testIds.state),
    status: page.getByTestId(testIds.status),
    testIds,
    transports,
    writer,
  }
  await expectWriterReady(writerPage, options.seedText)
  return writerPage
}

/** The page is connected, editable and shows the live document. */
export async function expectWriterReady(writer: WriterPage, text: string): Promise<void> {
  await expect(writer.state).toHaveAttribute('data-connection-status', 'connected', { timeout: 15_000 })
  await expect(writer.state).toHaveAttribute('data-can-edit', 'true')
  await expect(writer.editor).toHaveAttribute('contenteditable', 'true')
  await expect(writer.editor).toContainText(text)
}

/** The live document of the page's controller, as the editor shows it. */
export async function writerDocument(writer: WriterPage): Promise<Y.Doc> {
  const state = await writer.page.evaluate(() => (
    (window as unknown as { __writerDocumentState?: () => number[] })
      .__writerDocumentState?.() ?? null
  ))
  if (state === null) throw new Error('The writer fixture exposes no live document')
  const document = new Y.Doc()
  Y.applyUpdate(document, Uint8Array.from(state))
  return document
}

export async function writerText(
  writer: WriterPage,
  projection: EditorProjectionMode = 'final',
): Promise<string> {
  const document = await writerDocument(writer)
  try {
    return projectionText(document, projection)
  } finally {
    document.destroy()
  }
}

/**
 * Records every status kind the page's collaboration status shows, from now
 * on, on every change of the element. An absence over time is observed, not
 * sampled; the kind shown at installation is recorded first, so an empty
 * record means the observer is not running.
 */
export async function watchStatusKinds(writer: WriterPage): Promise<void> {
  await writer.page.evaluate((statusTestId) => {
    const root = document.querySelector(`[data-testid="${statusTestId}"]`)
    if (!root) throw new Error('writer status missing')
    const record = window as unknown as { __statusKinds?: string[] }
    record.__statusKinds = []
    const check = () => {
      const kind = root.querySelector('[data-editor-status-kind]')
        ?.getAttribute('data-editor-status-kind') ?? 'none'
      const kinds = record.__statusKinds!
      if (kinds.at(-1) !== kind) kinds.push(kind)
    }
    new MutationObserver(check).observe(root, {
      attributes: true,
      characterData: true,
      childList: true,
      subtree: true,
    })
    check()
  }, writer.testIds.status)
}

export async function observedStatusKinds(writer: WriterPage): Promise<string[]> {
  const kinds = await writer.page.evaluate(() => (
    (window as unknown as { __statusKinds?: string[] }).__statusKinds ?? null
  ))
  if (kinds === null || kinds.length === 0) {
    throw new Error('The status observer was not recording')
  }
  return kinds
}

/**
 * Records, from now on and on every change of the page, each alert, dialog,
 * toast, refusal line, rejection bar or status message with text that the
 * page shows outside the collaboration status itself, and counts the
 * changes it saw in the editor. An absence is only read from an observer
 * that saw the editor change.
 */
export async function watchNotices(writer: WriterPage): Promise<void> {
  await writer.page.evaluate((surfaceTestId) => {
    const surface = document.querySelector(`[data-testid="${surfaceTestId}"]`)
    if (!surface) throw new Error('writer surface missing')
    const record = { editorChanges: 0, shown: [] as string[] }
    ;(window as unknown as { __notices?: typeof record }).__notices = record
    const selector = [
      '[role="alert"]',
      '[role="alertdialog"]',
      '[role="status"]',
      '[data-sonner-toast]',
      '[data-collaboration-edit-refusal]',
      '[data-collaboration-update-rejected]',
    ].join(', ')
    const check = () => {
      for (const element of document.querySelectorAll(selector)) {
        if (element.closest('[data-editor-status-kind]')) continue
        const text = (element.textContent ?? '').trim()
        if (!text) continue
        const entry = `${element.getAttribute('role') ?? 'notice'}: ${text}`
        if (!record.shown.includes(entry)) record.shown.push(entry)
      }
    }
    new MutationObserver((mutations) => {
      if (mutations.some((mutation) => surface.contains(mutation.target))) record.editorChanges += 1
      check()
    }).observe(document.body, { attributes: true, characterData: true, childList: true, subtree: true })
    check()
  }, writer.testIds.surface)
}

/** What the notice observer recorded; fails when it never ran. */
export async function observedNotices(writer: WriterPage): Promise<{ editorChanges: number; shown: string[] }> {
  const record = await writer.page.evaluate(() => (
    (window as unknown as { __notices?: { editorChanges: number; shown: string[] } }).__notices ?? null
  ))
  if (record === null) throw new Error('The notice observer was not recording')
  return record
}

/** The caret goes to the end of a block of the editor (0-based). */
export async function placeCaretAtBlockEnd(writer: WriterPage, block: number): Promise<void> {
  const target = writer.editor.locator(':scope > *').nth(block)
  const box = await target.boundingBox()
  if (!box) throw new Error(`Editor block ${block} is not visible`)
  await writer.page.mouse.click(box.x + box.width - 2, box.y + box.height / 2)
  // Anchored on the selection itself: collapsed, inside the block, and no
  // document text after it in that block (another writer's caret label is
  // not document text).
  await expect.poll(() => writer.page.evaluate(({ index, surfaceTestId }) => {
    const selection = window.getSelection()
    const root = document.querySelector(`[data-testid="${surfaceTestId}"] .ProseMirror`)
    const target = root?.children.item(index)
    if (!selection || !target || selection.rangeCount === 0 || !selection.isCollapsed) return false
    const caret = selection.getRangeAt(0)
    if (!target.contains(caret.endContainer)) return false
    const rest = document.createRange()
    rest.setStart(caret.endContainer, caret.endOffset)
    rest.setEnd(target, target.childNodes.length)
    const after = rest.cloneContents()
    for (const label of after.querySelectorAll('[class*="collaboration-caret"]')) label.remove()
    return (after.textContent ?? '') === ''
  }, { index: block, surfaceTestId: writer.testIds.surface })).toBe(true)
  await expectEditorSelectionFollowsPage(writer)
}

/** The editor's selection as its own view reads the page's selection. */
type EditorSelectionView = {
  posAtDOM: (node: Node, offset: number) => number
  state: { selection: { anchor: number; head: number } }
}

/**
 * Waits until the editor's own selection is the one the page shows. The
 * editor reads a click's selection from a later selection event, and a key
 * pressed before that runs where the caret was before the click: a Backspace
 * there deletes nothing. The page's selection alone is no anchor for the
 * next key.
 */
export async function expectEditorSelectionFollowsPage(writer: WriterPage): Promise<void> {
  await expect.poll(() => writer.editor.evaluate((root) => {
    const view = (root as HTMLElement & { editor?: { view?: EditorSelectionView } }).editor?.view
    const selection = window.getSelection()
    if (!view) throw new Error('The editor surface exposes no editor view')
    if (!selection?.anchorNode || !selection.focusNode) return false
    try {
      return view.state.selection.anchor === view.posAtDOM(selection.anchorNode, selection.anchorOffset)
        && view.state.selection.head === view.posAtDOM(selection.focusNode, selection.focusOffset)
    } catch {
      return false
    }
  })).toBe(true)
}

/** Types characters one key at a time, `delayMs` apart. Returns when each
 * key was typed, in this worker's clock. */
export async function typeSlowly(
  writer: WriterPage,
  text: string,
  delayMs: number,
): Promise<number[]> {
  const typedAt: number[] = []
  for (const character of text) {
    await writer.page.keyboard.type(character)
    typedAt.push(Date.now())
    await writer.page.waitForTimeout(delayMs)
  }
  return typedAt
}

/** Waits until the page reports every local change as durably stored. */
export async function expectSaved(writer: WriterPage, timeout = 20_000): Promise<void> {
  await expect(writer.state).toHaveAttribute('data-durability-status', 'saved', { timeout })
  await expect(writer.state).toHaveAttribute('data-has-unconfirmed', 'false', { timeout })
}

export async function closeWriters(writers: readonly WriterPage[]): Promise<void> {
  await Promise.all(writers.map((writer) => writer.page.context().close()))
}

/** The document text an editor shows, without the other writers' caret
 * labels that the collaboration caret renders inline and without the
 * Markdown markers the editor shows around formatted text at the caret. */
export async function editorText(writer: WriterPage): Promise<string> {
  return writer.editor.evaluate((root) => {
    const clone = root.cloneNode(true) as HTMLElement
    for (const widget of clone.querySelectorAll('[class*="collaboration-caret"], .editor-syntax-marker')) {
      widget.remove()
    }
    return clone.textContent ?? ''
  })
}

/** Presses keys one at a time, `delayMs` apart: a single character is
 * typed, anything longer is a key name such as `Backspace`. */
export async function pressKeys(
  writer: WriterPage,
  keys: readonly string[],
  delayMs: number,
): Promise<void> {
  for (const key of keys) {
    if ([...key].length === 1) await writer.page.keyboard.type(key)
    else await writer.page.keyboard.press(key)
    await writer.page.waitForTimeout(delayMs)
  }
}

/** Selects the first occurrence of `word` in a block of the editor with a
 * double click, as a user selects a word, and returns what is selected. */
export async function selectWord(
  writer: WriterPage,
  block: number,
  word: string,
): Promise<string> {
  const center = await writer.page.evaluate(({ index, surfaceTestId, target }) => {
    const root = document.querySelector(`[data-testid="${surfaceTestId}"] .ProseMirror`)
    const element = root?.children.item(index)
    if (!element) return null
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT)
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const offset = (node.textContent ?? '').indexOf(target)
      if (offset < 0) continue
      const range = document.createRange()
      range.setStart(node, offset)
      range.setEnd(node, offset + target.length)
      const rect = range.getBoundingClientRect()
      return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 }
    }
    return null
  }, { index: block, surfaceTestId: writer.testIds.surface, target: word })
  if (!center) throw new Error(`The word ${word} is not in editor block ${block}`)
  await writer.page.mouse.dblclick(center.x, center.y)
  let selected = ''
  await expect.poll(async () => {
    selected = await writer.page.evaluate(() => window.getSelection()?.toString() ?? '')
    return selected
  }).not.toBe('')
  await expectEditorSelectionFollowsPage(writer)
  return selected
}

/** What an update does, for failure messages: the text it inserts and the
 * number of deleted ranges it carries. */
export function describeUpdate(update: Uint8Array): string {
  const decoded = Y.decodeUpdate(update)
  let ranges = 0
  for (const deletions of decoded.ds.clients.values()) ranges += deletions.length
  const formats: string[] = []
  for (const struct of decoded.structs) {
    if (struct instanceof Y.Item && struct.content instanceof Y.ContentFormat) {
      const value = struct.content.value as Record<string, unknown> | null
      formats.push(value === null ? `${struct.content.key}=null` : struct.content.key)
    }
  }
  const formatted = formats.length > 0 ? `[${formats.join(',')}]` : ''
  return `+${JSON.stringify(insertedText(update))}${formatted}/-${ranges}`
}

/** Waits until the page either stored every change or stopped with a
 * failure, and returns which. */
export async function settledDurability(writer: WriterPage, timeout = 20_000): Promise<string> {
  await expect.poll(
    () => writer.state.getAttribute('data-durability-status'),
    { timeout },
  ).toMatch(/^(saved|error)$/)
  return (await writer.state.getAttribute('data-durability-status')) ?? ''
}

/** A text run of a block with the names of its marks, sorted. */
export type Run = { marks: string[]; text: string }

/** The text runs of a block of a document, with the names of their marks. */
function runsOf(document: Y.Doc, block: number): Run[] {
  const json = editorYDocToJson(document) as {
    content?: Array<{ content?: Array<{ marks?: Array<{ type: string }>; text?: string }> }>
  }
  return (json.content?.[block]?.content ?? []).map((node) => ({
    marks: (node.marks ?? []).map((mark) => mark.type).sort(),
    text: node.text ?? '',
  }))
}

/** Every text node that carries a suggestion mark, with all its marks and
 * their attributes: what "exactly as proposed" compares. */
function proposalsOf(document: Y.Doc): unknown[] {
  const found: unknown[] = []
  const visit = (node: unknown): void => {
    if (!node || typeof node !== 'object') return
    const record = node as { content?: unknown[]; marks?: Array<{ type?: string }>; text?: string }
    if (record.marks?.some((mark) => ['deletion', 'insertion', 'modification'].includes(mark.type ?? ''))) {
      found.push({ marks: record.marks, text: record.text })
    }
    for (const child of record.content ?? []) visit(child)
  }
  visit(editorYDocToJson(document))
  return found
}

export async function liveRuns(writer: WriterPage, block = 0): Promise<Run[]> {
  const document = await writerDocument(writer)
  try {
    return runsOf(document, block)
  } finally {
    document.destroy()
  }
}

export async function liveProposals(writer: WriterPage): Promise<unknown[]> {
  const document = await writerDocument(writer)
  try {
    return proposalsOf(document)
  } finally {
    document.destroy()
  }
}

export function storedProposals(stack: CollaborationStack, documentId: string): { proposals: unknown[]; suggestions: string[] } {
  const document = stack.storeDocument(documentId)
  try {
    return { proposals: proposalsOf(document), suggestions: suggestionsOf(document) }
  } finally {
    document.destroy()
  }
}

export function storedRuns(stack: CollaborationStack, documentId: string): Run[] {
  const document = stack.storeDocument(documentId)
  try {
    return runsOf(document, 0)
  } finally {
    document.destroy()
  }
}

/** The document text of every element of the editor that matches a
 * selector, without the other writers' caret labels rendered inline. */
export async function shownText(writer: WriterPage, selector: string): Promise<string[]> {
  return writer.editor.evaluate((root, css) => [...root.querySelectorAll(css)].map((element) => {
    const clone = element.cloneNode(true) as HTMLElement
    for (const caret of clone.querySelectorAll('[class*="collaboration-caret"]')) caret.remove()
    return clone.textContent ?? ''
  }), selector)
}

/**
 * The viewport point a user clicks to put the caret before the first
 * character of `word` in a block, or after its last: inside that
 * character, on the side the caret should land. Other writers' caret
 * labels are not document text.
 */
async function wordEdge(
  writer: WriterPage,
  block: number,
  word: string,
  side: 'after' | 'before',
): Promise<{ x: number; y: number }> {
  const point = await writer.page.evaluate(({ edge, index, surfaceTestId, target }) => {
    const root = document.querySelector(`[data-testid="${surfaceTestId}"] .ProseMirror`)
    const element = root?.children.item(index)
    if (!element) return null
    const walker = document.createTreeWalker(element, NodeFilter.SHOW_TEXT, {
      acceptNode: (node) => (
        node.parentElement?.closest('[class*="collaboration-caret"]')
          ? NodeFilter.FILTER_REJECT
          : NodeFilter.FILTER_ACCEPT
      ),
    })
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const offset = (node.textContent ?? '').indexOf(target)
      if (offset < 0) continue
      const at = edge === 'before' ? offset : offset + target.length - 1
      const range = document.createRange()
      range.setStart(node, at)
      range.setEnd(node, at + 1)
      const rect = range.getBoundingClientRect()
      return { x: edge === 'before' ? rect.left + 1 : rect.right - 1, y: rect.top + rect.height / 2 }
    }
    return null
  }, { edge: side, index: block, surfaceTestId: writer.testIds.surface, target: word })
  if (!point) throw new Error(`The word ${word} is not in editor block ${block}`)
  return point
}

/** What the page shows selected, and whether the editor's own selection is
 * the page's: the text after a caret within its block (other writers' caret
 * labels aside), or the selected text of a range. */
async function pageSelection(writer: WriterPage, block: number): Promise<{
  after: string | null
  editorFollows: boolean
  selected: string
}> {
  return writer.editor.evaluate((root, index) => {
    const view = (root as HTMLElement & {
      editor?: { view?: { posAtDOM: (node: Node, offset: number) => number; state: { selection: { anchor: number; head: number } } } }
    }).editor?.view
    const selection = window.getSelection()
    if (!view || !selection?.anchorNode || !selection.focusNode || selection.rangeCount === 0) {
      return { after: null, editorFollows: false, selected: '' }
    }
    let editorFollows: boolean
    try {
      editorFollows = view.state.selection.anchor === view.posAtDOM(selection.anchorNode, selection.anchorOffset)
        && view.state.selection.head === view.posAtDOM(selection.focusNode, selection.focusOffset)
    } catch {
      editorFollows = false
    }
    const target = root.children.item(index)
    let after: string | null = null
    if (target && selection.isCollapsed && target.contains(selection.getRangeAt(0).endContainer)) {
      const caret = selection.getRangeAt(0)
      const rest = document.createRange()
      rest.setStart(caret.endContainer, caret.endOffset)
      rest.setEnd(target, target.childNodes.length)
      const contents = rest.cloneContents()
      for (const label of contents.querySelectorAll('[class*="collaboration-caret"]')) label.remove()
      after = contents.textContent ?? ''
    }
    return { after, editorFollows, selected: selection.toString() }
  }, block)
}

/**
 * Repeats a user's input until the page and the editor show what it was
 * meant to produce. Automated WebKit input occasionally lands one character
 * off in this editor; this test is about formatting, and the input it
 * builds on must be exact, so the input is checked on the page and repeated,
 * three times at most.
 */
async function inputUntil(
  input: () => Promise<void>,
  reached: () => Promise<boolean>,
  what: string,
): Promise<void> {
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      await input()
      await expect.poll(reached, { timeout: 2_000 }).toBe(true)
      return
    } catch {
      // The input landed elsewhere; the next attempt starts over, and the
      // report names every repeated input.
      test.info().annotations.push({ type: 'input-repeated', description: what })
    }
  }
  throw new Error(`${what} did not reach the page in three attempts`)
}

/** A click that puts the caret right after `word` in a block. */
export async function placeCaretAfter(writer: WriterPage, block: number, word: string, rest: string): Promise<void> {
  await inputUntil(
    async () => {
      const point = await wordEdge(writer, block, word, 'after')
      await writer.page.mouse.click(point.x, point.y)
    },
    async () => {
      const shown = await pageSelection(writer, block)
      return shown.editorFollows && shown.after === rest
    },
    `The caret after ${word}`,
  )
}

/** A click before `from`, and once the editor holds that caret, a
 * Shift-click after `to`, in one block. */
export async function selectFromTo(writer: WriterPage, block: number, from: string, to: string, expected: string): Promise<void> {
  await inputUntil(
    async () => {
      const start = await wordEdge(writer, block, from, 'before')
      await writer.page.mouse.click(start.x, start.y)
      await expect.poll(async () => {
        const shown = await pageSelection(writer, block)
        return shown.editorFollows && shown.after?.startsWith(expected) === true
      }, { timeout: 2_000 }).toBe(true)
      const end = await wordEdge(writer, block, to, 'after')
      await writer.page.keyboard.down('Shift')
      await writer.page.mouse.click(end.x, end.y)
      await writer.page.keyboard.up('Shift')
    },
    async () => {
      const shown = await pageSelection(writer, block)
      return shown.editorFollows && shown.selected === expected
    },
    `The selection ${expected}`,
  )
}

/** A suggest-mode writer proposes `word` in the middle of the first block,
 * before its last word, and its caret leaves the block. */
export async function proposeInTheMiddle(author: WriterPage, word: string): Promise<void> {
  await placeCaretAfter(author, 0, 'paragraph', ' text')
  await typeSlowly(author, ` ${word}`, 40)
  await expectSaved(author)
  // The author's caret label must not sit inside the other writer's
  // selection.
  await placeCaretAfter(author, 1, 'paragraph', '')
}
