import { createHash } from 'node:crypto'

import { expect, test, type Browser, type Page } from '@playwright/test'
import { EDITOR_YJS_FRAGMENT } from '@inqtrix/editor-schema'
import WebSocket from 'ws'
import * as Y from 'yjs'

import {
  CollaborationStack,
  WRITER_A,
  WRITER_B,
  WRITER_C,
  WRITER_D,
  closeWriters,
  editorText,
  expectSaved,
  observedStatusKinds,
  openWriter,
  placeCaretAtBlockEnd,
  pressKeys,
  typeSlowly,
  watchStatusKinds,
  writerText,
  type FixtureWriter,
  type TransportRecord,
  type WriterAccess,
  type WriterPage,
} from './collaborationStack'

const DOCUMENT_ID = 'reconnect-doc'
const SEED = 'Seed paragraph for continuity'

function startStack(): Promise<CollaborationStack> {
  return CollaborationStack.start({ documents: [{ id: DOCUMENT_ID, markdown: SEED }] })
}

function openReconnectWriter(
  browser: Browser,
  stack: CollaborationStack,
  writer: FixtureWriter,
): Promise<WriterPage> {
  return openWriter(browser, stack, writer, {
    documentId: DOCUMENT_ID,
    fixture: 'reconnect',
    seedText: SEED,
  })
}

function currentTransport(writer: WriterPage): string {
  const transport = writer.transports.at(-1)?.transportId
  if (!transport) throw new Error('The writer transport was not identified')
  return transport
}

function closesOf(writer: WriterPage): Array<{ code: number; reason: string }> {
  return writer.transports.flatMap((transport) => transport.closes)
}

/** Records, on every change of the editor surface, whether `marker` is
 * missing after it first appeared. Absence over time, not a sample. */
async function watchMarker(writer: WriterPage, marker: string): Promise<void> {
  await writer.page.evaluate((expected) => {
    const surface = document.querySelector('[data-testid="reconnect-surface"]')
    if (!surface) throw new Error('reconnect surface missing')
    const record = window as unknown as { __markerSeen?: boolean; __markerMissing?: boolean }
    record.__markerSeen = false
    record.__markerMissing = false
    const check = () => {
      const present = (surface.textContent ?? '').includes(expected)
      if (present) record.__markerSeen = true
      else if (record.__markerSeen) record.__markerMissing = true
    }
    new MutationObserver(check).observe(surface, {
      characterData: true,
      childList: true,
      subtree: true,
    })
    check()
  }, marker)
}

async function placeCaretAtEnd(writer: WriterPage): Promise<void> {
  await writer.editor.click()
  await writer.page.keyboard.press('Control+End')
}

async function undoShortcut(page: Page): Promise<string> {
  return page.evaluate(() => (
    /Mac|iPhone|iPad|iPod/.test(navigator.platform) ? 'Meta+z' : 'Control+z'
  ))
}

test("a reconnect keeps the writer's editor, text, focus and undo history and leaves the other writer connected", async ({ browser }) => {
  test.setTimeout(120_000)
  const stack = await startStack()
  const writers: WriterPage[] = []
  try {
    const writerA = await openReconnectWriter(browser, stack, WRITER_A)
    writers.push(writerA)
    const writerB = await openReconnectWriter(browser, stack, WRITER_B)
    writers.push(writerB)
    const editorInstances = await writerA.state.getAttribute('data-editor-instances')

    await placeCaretAtEnd(writerA)
    await watchMarker(writerA, 'MARKE1')
    await writerA.page.keyboard.type('MARKE1', { delay: 40 })
    await expect.poll(() => stack.storeText(DOCUMENT_ID), { timeout: 10_000 }).toBe(`${SEED}MARKE1`)
    await expect(writerA.state).toHaveAttribute('data-durability-status', 'saved')

    // With persistence held, the writer keeps typing: one update waits for
    // its acknowledgement and the rest waits in the editor. The service then
    // closes the writer's socket, as it does when it cannot keep up.
    stack.holdPersistence()
    await writerA.page.keyboard.type(`MARKE2${'abcdefghij'.repeat(2)}`, { delay: 60 })
    stack.closeTransport(currentTransport(writerA), 4429, 'rate_limited')
    await expect.poll(() => closesOf(writerA), { timeout: 10_000 }).toEqual([
      { code: 4429, reason: 'rate_limited' },
    ])
    await expect(writerA.state).toHaveAttribute('data-connection-status', 'reconnecting')
    await expect(writerA.state).toHaveAttribute('data-can-edit', 'false')
    await expect(writerA.status).toContainText('Verbindung wird wiederhergestellt')
    const textWhileReconnecting = await editorText(writerA)
    expect(textWhileReconnecting).toBe(`${SEED}MARKE1MARKE2${'abcdefghij'.repeat(2)}`)

    stack.releasePersistence()
    await expect(writerA.state).toHaveAttribute('data-connection-status', 'connected', {
      timeout: 20_000,
    })
    await expect(writerA.state).toHaveAttribute('data-can-edit', 'true')
    // Focus comes back without a click, at the caret the writer left.
    await expect(writerA.editor).toBeFocused()
    await writerA.page.keyboard.type('MARKE3')
    const withMarke3 = `${textWhileReconnecting}MARKE3`
    await expect.poll(() => editorText(writerA)).toBe(withMarke3)
    await expect.poll(() => stack.storeText(DOCUMENT_ID), { timeout: 10_000 }).toBe(withMarke3)
    await expect.poll(() => editorText(writerB)).toBe(withMarke3)

    const undo = await undoShortcut(writerA.page)
    await writerA.page.keyboard.press(undo)
    await expect.poll(() => editorText(writerA)).toBe(textWhileReconnecting)
    await expect.poll(() => stack.storeText(DOCUMENT_ID), { timeout: 10_000 })
      .toBe(textWhileReconnecting)

    await expect(writerA.state).toHaveAttribute('data-editor-instances', editorInstances!)
    // Read before the undo below removes the marker on purpose.
    expect(await writerA.page.evaluate(() => (
      (window as unknown as { __markerMissing?: boolean }).__markerMissing
    ))).toBe(false)
    // One closed socket, one reconnect: the ordered replay went through.
    expect(closesOf(writerA)).toEqual([{ code: 4429, reason: 'rate_limited' }])
    expect(closesOf(writerB)).toEqual([])
    expect(await writerB.page.evaluate(() => (
      (window as unknown as { __reconnectConnectionStatuses?: string[] })
        .__reconnectConnectionStatuses ?? []
    ))).not.toContain('reconnecting')
    await expect(writerB.state).toHaveAttribute('data-connection-status', 'connected')

    // The undo history from before the reconnect is still there: undo reaches
    // back across the reconnect to the text typed before it.
    for (let step = 0; step < 20 && (await editorText(writerA)).includes('MARKE1'); step += 1) {
      await writerA.page.keyboard.press(undo)
      await writerA.page.waitForTimeout(100)
    }
    await expect.poll(() => editorText(writerA)).toBe(SEED)
    await expect.poll(() => stack.storeText(DOCUMENT_ID), { timeout: 10_000 }).toBe(SEED)
    await expect.poll(() => editorText(writerB)).toBe(SEED)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test('an IME composition that spans a reconnect is committed once and keeps focus', async ({
  browser,
  browserName,
}) => {
  test.skip(
    browserName !== 'chromium',
    'IME composition is driven through the Chromium DevTools protocol, which the other engines do not provide',
  )
  test.setTimeout(120_000)
  const stack = await startStack()
  const writers: WriterPage[] = []
  try {
    const writerA = await openReconnectWriter(browser, stack, WRITER_A)
    writers.push(writerA)
    const writerB = await openReconnectWriter(browser, stack, WRITER_B)
    writers.push(writerB)
    const transport = currentTransport(writerA)
    await placeCaretAtEnd(writerA)
    const session = await writerA.page.context().newCDPSession(writerA.page)

    await session.send('Input.imeSetComposition', {
      selectionEnd: 2,
      selectionStart: 2,
      text: 'かん',
    })
    stack.closeTransport(transport, 4429, 'rate_limited')
    await expect(writerA.state).toHaveAttribute('data-connection-status', 'reconnecting')
    await session.send('Input.insertText', { text: '漢字' })

    await expect(writerA.state).toHaveAttribute('data-connection-status', 'connected', {
      timeout: 20_000,
    })
    await expect(writerA.state).toHaveAttribute('data-can-edit', 'true')
    await expect(writerA.editor).toBeFocused()
    await expect.poll(() => editorText(writerA)).toBe(`${SEED}漢字`)
    await expect.poll(() => stack.storeText(DOCUMENT_ID), { timeout: 10_000 }).toBe(`${SEED}漢字`)
    await expect.poll(() => editorText(writerB)).toBe(`${SEED}漢字`)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test('a reconnect does not take the focus back after the user selected text elsewhere', async ({ browser }) => {
  test.setTimeout(120_000)
  const stack = await startStack()
  const writers: WriterPage[] = []
  try {
    const writerA = await openReconnectWriter(browser, stack, WRITER_A)
    writers.push(writerA)
    const transport = currentTransport(writerA)
    await placeCaretAtEnd(writerA)
    await expect(writerA.editor).toBeFocused()

    stack.closeTransport(transport, 4429, 'rate_limited')
    await expect(writerA.state).toHaveAttribute('data-connection-status', 'reconnecting')
    const outside = writerA.page.getByTestId('reconnect-outside')
    await outside.click({ clickCount: 3 })
    const selected = (await outside.textContent())?.trim() ?? ''
    expect(selected).not.toBe('')
    await expect.poll(() => writerA.page.evaluate(() => (
      window.getSelection()?.toString().trim() ?? ''
    ))).toBe(selected)

    await expect(writerA.state).toHaveAttribute('data-connection-status', 'connected', {
      timeout: 20_000,
    })
    await expect(writerA.state).toHaveAttribute('data-can-edit', 'true')
    // Give a focus restore the chance to run before judging its absence.
    await writerA.page.waitForTimeout(300)
    await expect(writerA.editor).not.toBeFocused()
    expect(await writerA.page.evaluate(() => (
      window.getSelection()?.toString().trim() ?? ''
    ))).toBe(selected)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

// Status kinds that end a writer's session or its changes.
const TERMINAL_KINDS = ['access_revoked', 'error', 'origin_rejected', 'reload_required']

/** The page's liveness check gives up a socket that heard nothing for 30
 * seconds; this moves the page's clock past it and past the retries of a
 * close that never completes. */
const PAST_LIVENESS_MS = 40_000
/** The page gives up a connect attempt without a first answer after 35
 * seconds. */
const PAST_CONNECT_ANSWER_MS = 36_000

function acknowledged(transport: TransportRecord, hash: string | null): boolean {
  return transport.acks().some((ack) => ack.hash === hash)
}

test('a connection that dies silently loses nothing the writer typed before or after it: its unacknowledged update is sent again and acknowledged on the next connection', async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'silent-path-doc'
  const seed = 'Silent path paragraph'
  const stack = await CollaborationStack.start({ documents: [{ id: documentId, markdown: seed }] })
  const writers: WriterPage[] = []
  try {
    const writer = await openWriter(browser, stack, WRITER_A, { clock: true, documentId, seedText: seed })
    writers.push(writer)
    await watchStatusKinds(writer)
    await placeCaretAtBlockEnd(writer, 0)
    // A deletion first: the room's answer to a later sync request then
    // differs from any single update of this writer.
    await pressKeys(writer, ['Backspace'], 60)
    await expectSaved(writer)
    const first = writer.transports[0]!

    writer.bridge.silenceCurrent()
    await typeSlowly(writer, 'MARKE1', 60)
    const lost = first.updates().at(-1)!
    expect(acknowledged(first, lost.hash)).toBe(false)
    await writer.page.clock.runFor(PAST_LIVENESS_MS)

    await expect(writer.state).toHaveAttribute('data-connection-status', 'connected', { timeout: 20_000 })
    await typeSlowly(writer, 'MARKE2', 60)
    await expectSaved(writer, 10_000)

    expect(stack.storeText(documentId)).toBe(`${seed.slice(0, -1)}MARKE1MARKE2`)
    expect(writer.transports).toHaveLength(2)
    const second = writer.transports[1]!
    // The update that went into the dead path is the first one the next
    // connection carries, and the service acknowledged it there.
    expect(second.updates()[0]?.hash).toBe(lost.hash)
    expect(acknowledged(second, lost.hash)).toBe(true)
    // The service closes the connection the page gave up once the new one
    // took its place; the page never hears of it, its path is dead.
    expect(first.closes).toEqual([{ code: 4401, reason: 'connection_superseded' }])
    expect(stack.rejections()).toEqual({})
    const kinds = await observedStatusKinds(writer)
    expect(kinds).toContain('reconnecting')
    expect(kinds.filter((kind) => TERMINAL_KINDS.includes(kind))).toEqual([])
    expect(writer.consoleErrors).toEqual([])
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test('a connection whose first answer never arrives is given up and replaced, and the writer keeps writing', async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'unanswered-connect-doc'
  const seed = 'Unanswered connect paragraph'
  const stack = await CollaborationStack.start({ documents: [{ id: documentId, markdown: seed }] })
  const writers: WriterPage[] = []
  try {
    const writer = await openWriter(browser, stack, WRITER_A, { clock: true, documentId, seedText: seed })
    writers.push(writer)
    await watchStatusKinds(writer)
    await placeCaretAtBlockEnd(writer, 0)
    await typeSlowly(writer, 'MARKE1', 60)
    await expectSaved(writer)

    writer.bridge.stallNext()
    stack.closeTransport(writer.transports[0]!.transportId!, 4429, 'rate_limited')
    await expect.poll(() => writer.transports.length, { timeout: 15_000 }).toBe(2)
    const stalled = writer.transports[1]!
    expect(stalled.stalled).toBe(true)
    // The page opened the socket and asked to be let in; nothing came back.
    await expect.poll(() => stalled.frames.filter((frame) => frame.direction === 'out').length)
      .toBeGreaterThan(0)
    await expect(writer.state).not.toHaveAttribute('data-connection-status', 'connected')

    await writer.page.clock.runFor(PAST_CONNECT_ANSWER_MS)
    await expect(writer.state).toHaveAttribute('data-connection-status', 'connected', { timeout: 20_000 })
    expect(writer.transports).toHaveLength(3)
    await typeSlowly(writer, 'MARKE2', 60)
    await expectSaved(writer, 10_000)

    expect(stack.storeText(documentId)).toBe(`${seed}MARKE1MARKE2`)
    expect(stack.rejections()).toEqual({})
    const kinds = await observedStatusKinds(writer)
    expect(kinds).toContain('reconnecting')
    expect(kinds.filter((kind) => TERMINAL_KINDS.includes(kind))).toEqual([])
    expect(writer.consoleErrors).toEqual([])
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

/** The collaboration carets an editor shows for a writer, by label. */
async function caretsOf(page: WriterPage, writer: FixtureWriter): Promise<number> {
  return page.editor.evaluate((root, name) => (
    [...root.querySelectorAll('.inqtrix-collaboration-caret-label')]
      .filter((label) => label.textContent === name).length
  ), writer.name)
}

test("a writer who reconnects while the service still holds the old connection keeps its presence for the others, and the room refuses none of its presence frames", async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'presence-reconnect-doc'
  const seed = 'Presence paragraph'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `${seed}\n\nSecond paragraph` }],
  })
  const writers: WriterPage[] = []
  try {
    const writerA = await openWriter(browser, stack, WRITER_A, { clock: true, documentId, seedText: seed })
    writers.push(writerA)
    const writerB = await openWriter(browser, stack, WRITER_B, { documentId, seedText: seed })
    writers.push(writerB)
    await watchStatusKinds(writerA)
    await placeCaretAtBlockEnd(writerB, 1)
    await placeCaretAtBlockEnd(writerA, 0)
    await typeSlowly(writerA, 'MARKE1', 60)
    await expectSaved(writerA)
    await expect.poll(() => caretsOf(writerB, WRITER_A)).toBe(1)

    // The old connection stays open at the service while the page gives it
    // up and connects again.
    writerA.bridge.silenceCurrent()
    await writerA.page.clock.runFor(PAST_LIVENESS_MS)
    await expect(writerA.state).toHaveAttribute('data-connection-status', 'connected', { timeout: 20_000 })
    await typeSlowly(writerA, 'MARKE2', 60)
    await expectSaved(writerA, 10_000)
    await expect.poll(() => editorText(writerB)).toBe(`${seed}MARKE1MARKE2Second paragraph`)
    await expect.poll(() => caretsOf(writerB, WRITER_A)).toBe(1)

    // A close loop would show within a few seconds as further sockets.
    await writerA.page.waitForTimeout(3_000)
    expect(writerA.transports).toHaveLength(2)
    expect(writerA.transports[0]!.closes).toEqual([{ code: 4401, reason: 'connection_superseded' }])
    expect(writerA.transports[1]!.closes).toEqual([])
    expect(writerB.transports.flatMap((transport) => transport.closes)).toEqual([])
    expect(await caretsOf(writerB, WRITER_A)).toBe(1)
    // Every reason the room's presence ownership refuses a frame for.
    for (const reason of ['foreign_client_id', 'second_client_id', 'superseded_connection']) {
      expect(stack.metric('inqtrix_collaboration_awareness_dropped_total', { reason }), reason).toBe(0)
    }
    expect(stack.rejections()).toEqual({})
    const kinds = await observedStatusKinds(writerA)
    expect(kinds.filter((kind) => TERMINAL_KINDS.includes(kind))).toEqual([])
    for (const writer of writers) expect(writer.consoleErrors).toEqual([])
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

// Hocuspocus message types, y-protocols sync steps and authentication kinds
// a writer outside any page speaks.
const MESSAGE_SYNC = 0
const MESSAGE_AUTH = 2
const MESSAGE_STATELESS = 5
const SYNC_REQUEST = 0
const SYNC_ANSWER = 1
const SYNC_UPDATE = 2
const AUTH_TOKEN = 0
const AUTH_AUTHENTICATED = 2

function varUint(value: number): number[] {
  const bytes: number[] = []
  let remaining = value
  do {
    let byte = remaining & 0x7f
    remaining = Math.floor(remaining / 128)
    if (remaining > 0) byte |= 0x80
    bytes.push(byte)
  } while (remaining > 0)
  return bytes
}

function varBytes(bytes: Uint8Array): number[] {
  return [...varUint(bytes.byteLength), ...bytes]
}

function varString(value: string): number[] {
  return varBytes(new TextEncoder().encode(value))
}

/** Reads the frames the service sends a writer outside any page. */
class InboundFrame {
  private offset = 0
  private readonly source: Uint8Array

  constructor(source: Uint8Array) {
    this.source = source
  }

  varUint(): number {
    let value = 0
    let factor = 1
    while (this.offset < this.source.byteLength) {
      const byte = this.source[this.offset]!
      this.offset += 1
      value += (byte & 0x7f) * factor
      if ((byte & 0x80) === 0) return value
      factor *= 128
    }
    throw new Error('A frame of the service ended inside a number')
  }

  bytes(): Uint8Array {
    const length = this.varUint()
    const slice = this.source.slice(this.offset, this.offset + length)
    this.offset += length
    return slice
  }

  string(): string {
    return new TextDecoder().decode(this.bytes())
  }
}

/**
 * A writer outside any page: one socket to the service under an edit lease,
 * a replica of the room kept from everything the service sends it, and
 * changes made on that replica, each sent as one update and awaited until the
 * service acknowledged it durably.
 */
class RoomWriter {
  private readonly acknowledged = new Set<string>()
  private authenticated = false
  private readonly replica = new Y.Doc()
  private readonly room: string
  private readonly socket: WebSocket
  private synced = false

  private constructor(socket: WebSocket, room: string) {
    this.socket = socket
    this.room = room
    socket.on('message', (data: WebSocket.RawData) => {
      this.receive(new Uint8Array(Array.isArray(data) ? Buffer.concat(data) : data))
    })
  }

  static async join(
    stack: CollaborationStack,
    writer: FixtureWriter,
    documentId: string,
  ): Promise<RoomWriter> {
    const session = stack.session(writer, 'edit', documentId)
    const socket = new WebSocket(`ws://127.0.0.1:${stack.port}${stack.configured.websocketPath}`, {
      headers: { Authorization: `Bearer ${stack.configured.secret}` },
    })
    await new Promise<void>((resolve, reject) => {
      socket.once('open', () => resolve())
      socket.once('error', reject)
    })
    const client = new RoomWriter(socket, String(session.room))
    client.write([MESSAGE_AUTH, AUTH_TOKEN, ...varString(String(session.lease_token))])
    await expect.poll(() => client.authenticated).toBe(true)
    client.write([MESSAGE_SYNC, SYNC_REQUEST, ...varBytes(Y.encodeStateVector(client.replica))])
    await expect.poll(() => client.synced).toBe(true)
    return client
  }

  /** Makes a change to the first text of the replica and returns its
   * update, not yet sent. */
  change(edit: (text: Y.XmlText) => void): Uint8Array {
    const paragraph = this.replica.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0)
    const text = paragraph instanceof Y.XmlElement ? paragraph.get(0) : null
    if (!(text instanceof Y.XmlText)) throw new Error('The room has no first text')
    const updates: Uint8Array[] = []
    const capture = (update: Uint8Array): void => {
      updates.push(update)
    }
    this.replica.on('update', capture)
    try {
      this.replica.transact(() => edit(text))
    } finally {
      this.replica.off('update', capture)
    }
    if (updates.length !== 1) throw new Error(`The change made ${updates.length} updates`)
    return updates[0]!
  }

  /** Sends an update and waits until the service acknowledged it durably. */
  async send(update: Uint8Array): Promise<void> {
    const hash = createHash('sha256').update(update).digest('hex')
    this.write([MESSAGE_SYNC, SYNC_UPDATE, ...varBytes(update)])
    await expect.poll(() => this.acknowledged.has(hash), { timeout: 10_000 }).toBe(true)
  }

  close(): void {
    this.socket.close()
    this.replica.destroy()
  }

  private write(message: number[]): void {
    this.socket.send(Uint8Array.from([...varString(this.room), ...message]))
  }

  private receive(bytes: Uint8Array): void {
    const frame = new InboundFrame(bytes)
    frame.string()
    const type = frame.varUint()
    if (type === MESSAGE_AUTH) {
      if (frame.varUint() === AUTH_AUTHENTICATED) this.authenticated = true
      return
    }
    if (type === MESSAGE_SYNC) {
      const step = frame.varUint()
      if (step !== SYNC_ANSWER && step !== SYNC_UPDATE) return
      Y.applyUpdate(this.replica, frame.bytes(), this)
      if (step === SYNC_ANSWER) this.synced = true
      return
    }
    if (type === MESSAGE_STATELESS) {
      const message = JSON.parse(frame.string()) as { hash?: unknown; type?: unknown }
      if (message.type === 'durable_ack' && typeof message.hash === 'string') {
        this.acknowledged.add(message.hash)
      }
    }
  }
}

// The room applies every update as a transaction of its own, each with the
// cleanup Yjs attaches to it: a deletion alone gets only the cleanup of the
// gap it leaves. The answer to a reconnecting page's sync request carries all
// of them merged into one update, and the page's cleanup of that merge reads
// the whole text, so it can remove a format item the room keeps. The page
// sends that cleanup; a bold letter another writer types next to the item
// then reads the same on the page as in the room.
async function expectSyncAnswerCleanupAccepted(browser: Browser, access: WriterAccess): Promise<void> {
  const documentId = `merged-sync-answer-${access}-doc`
  const stack = await CollaborationStack.start({ documents: [{ id: documentId, markdown: 'abcdef' }] })
  const writers: WriterPage[] = []
  const roomWriters: RoomWriter[] = []
  try {
    const writer = await openWriter(browser, stack, WRITER_C, {
      access,
      clock: true,
      documentId,
      history: true,
      seedText: 'abcdef',
    })
    writers.push(writer)
    await watchStatusKinds(writer)
    const bolder = await RoomWriter.join(stack, WRITER_A, documentId)
    roomWriters.push(bolder)
    const deleter = await RoomWriter.join(stack, WRITER_B, documentId)
    roomWriters.push(deleter)

    // The page's path dies silently: it hears of nothing that follows.
    writer.bridge.silenceCurrent()
    // One writer bolds `a`; the other deletes `abc` on a state without the
    // bold run. The room keeps the end of the bold run, which sets nothing
    // any more.
    const bold = bolder.change((text) => text.format(0, 1, { bold: {} }))
    const deletion = deleter.change((text) => text.delete(0, 3))
    await bolder.send(bold)
    await deleter.send(deletion)
    expect(stack.storeText(documentId)).toBe('def')

    // The page gives up the dead path and connects again; the answer to its
    // sync request carries both changes merged into one update.
    await writer.page.clock.runFor(PAST_LIVENESS_MS)
    await expect(writer.state).toHaveAttribute('data-connection-status', 'connected', { timeout: 20_000 })
    await expect(writer.state).toHaveAttribute('data-has-unconfirmed', 'false', { timeout: 10_000 })

    // A third writer, who joins now, types a bold letter at the start.
    const typist = await RoomWriter.join(stack, WRITER_D, documentId)
    roomWriters.push(typist)
    await typist.send(typist.change((text) => text.insert(0, 'Q', { bold: {} })))

    await expect.poll(() => stack.storeText(documentId)).toBe('**Q**def')
    await expect.poll(() => writerText(writer)).toBe('**Q**def')
    await expect.poll(() => editorText(writer)).toBe('Qdef')
    await expect(writer.editor.locator('strong')).toHaveText('Q')
    // The service accepted the cleanup: the one update the new connection
    // carried, stored and acknowledged, and nothing was refused.
    expect(writer.transports).toHaveLength(2)
    const reconnected = writer.transports[1]!
    expect(reconnected.updates()).toHaveLength(1)
    expect(acknowledged(reconnected, reconnected.updates()[0]!.hash)).toBe(true)
    expect(stack.rejections()).toEqual({})
    await expectSaved(writer)
    await expect(writer.state).toHaveAttribute('data-can-edit', 'true')
    // The cleanup is stored under the page's writer, and it changed nothing a
    // reader sees: the history lists the other writers' edits and no entry
    // for it.
    expect(stack.storedRows(documentId).filter(({ input }) => input.actorUserId === WRITER_C.id)
      .map(({ input }) => input.changeSummary.changesDocument)).toEqual([false])
    const history = writer.page.getByTestId('writer-history')
    await expect(history).toContainText(WRITER_D.name)
    await expect(history).toContainText(WRITER_A.name)
    await expect(history).not.toContainText(WRITER_C.name)
    const kinds = await observedStatusKinds(writer)
    expect(kinds.filter((kind) => TERMINAL_KINDS.includes(kind))).toEqual([])
    expect(writer.consoleErrors).toEqual([])
  } finally {
    for (const roomWriter of roomWriters) roomWriter.close()
    await closeWriters(writers)
    await stack.stop()
  }
}

test('a writer with edit access who reconnects after a room change left a format item redundant sends the cleanup, which the service accepts, and a bold letter typed there reads the same everywhere', async ({ browser }) => {
  test.setTimeout(120_000)
  await expectSyncAnswerCleanupAccepted(browser, 'edit')
})

test('a writer with suggest access who reconnects after a room change left a format item redundant sends the cleanup, which the service accepts, and a bold letter typed there reads the same everywhere', async ({ browser }) => {
  test.setTimeout(120_000)
  await expectSyncAnswerCleanupAccepted(browser, 'suggest')
})
