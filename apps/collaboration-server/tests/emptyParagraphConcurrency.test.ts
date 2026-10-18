import { getSchema, type JSONContent } from '@tiptap/core'
import { EditorState, TextSelection, type PluginView, type Transaction } from '@tiptap/pm/state'
import type { EditorView } from '@tiptap/pm/view'
import { splitBlock } from '@tiptap/pm/commands'
import {
  initProseMirrorDoc,
  prosemirrorJSONToYDoc,
  updateYFragment,
  ySyncPlugin,
} from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  EDITOR_YJS_TEXTBLOCK_NAMES,
  completeCreatedTextblocks,
  createEditorSchemaExtensions,
  editorCollaborationRoom,
  editorJsonToYDoc,
  editorYDocToJson,
  getEditorSchemaFingerprint,
  transformToInqtrixSuggestionTransaction,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import type { ConnectionContext } from '../src/contracts'
import { DocumentCoordinator } from '../src/documentCoordinator'
import { hashBytes, reconstructValidatedDocument, validateDocument } from '../src/documentState'
import { CollaborationError } from '../src/errors'
import { InstanceLeaseManager } from '../src/instanceLease'
import { SidecarMetrics } from '../src/metrics'
import { FakeCollaborationApi, documentState, settings, silentLogger } from './helpers'

// Two writers type into the same empty line before either has seen the
// other's text. Every empty textblock holds one empty text node from the
// update that created it, so both writers' first characters go into that one
// node as ordinary concurrent insertions, the same way they go into a line
// that already holds text. Nobody is disconnected, nothing is lost or written
// twice, no writer writes anything while it applies the other's change, and
// every replica, the room and the store end with the same line.
//
// The writers are the real editor binding (@tiptap/y-tiptap) over the editor
// schema, with a minimal view whose dispatch hands each local change to the
// binding as the editor view does, and they complete the textblocks their own
// changes create as the browser does. The room is the real acceptance path,
// DocumentCoordinator.prepareClientUpdate, over a store double.

const DOCUMENT_ID = 'ed_empty_paragraph'
const ROOM = editorCollaborationRoom(DOCUMENT_ID, 1)
const WRITER_A = '11111111-1111-4111-8111-111111111111'
const WRITER_B = '22222222-2222-4222-8222-222222222222'
const PATCH_ID = '44444444-4444-4444-8444-444444444444'
const SUGGESTION_ID = '55555555-5555-4555-8555-555555555555'
const SECOND_PATCH_ID = '66666666-6666-4666-8666-666666666666'
const SECOND_SUGGESTION_ID = '77777777-7777-4777-8777-777777777777'
const CREATED_AT = 1_784_112_000
const SCHEMA = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
const FROM_ROOM = Symbol('room')
const MAX_FOLLOW_UP_ROUNDS = 4
const INVALID_SCHEMA = '4409 invalid_schema'
const POLICY_REFUSAL = '4403 suggestion_policy_violation'

function paragraph(text?: string): JSONContent {
  return text ? { content: [{ text, type: 'text' }], type: 'paragraph' } : { type: 'paragraph' }
}

/** A document whose middle line is empty. */
const EMPTY_LINE_DOCUMENT: JSONContent = {
  content: [paragraph('Oben.'), paragraph(), paragraph('Unten.')],
  type: 'doc',
}

const TWO_LINES: JSONContent = {
  content: [paragraph('Oben.'), paragraph('Unten.')],
  type: 'doc',
}

/** The stored state of `content`, as the service builds new documents. */
function stored(content: JSONContent): Uint8Array {
  const document = editorJsonToYDoc(content)
  const state = Y.encodeStateAsUpdate(document)
  document.destroy()
  return state
}

/** The stored state of `content` as documents written without the empty
 * text node hold it: an empty line has no child at all. */
function storedWithoutTextNode(content: JSONContent): Uint8Array {
  const document = prosemirrorJSONToYDoc(SCHEMA, content, EDITOR_YJS_FRAGMENT)
  const state = Y.encodeStateAsUpdate(document)
  document.destroy()
  return state
}

/** An editor on its own replica, as a browser holds it. */
class BoundWriter {
  readonly document = new Y.Doc()
  /** Refused by the room: its connection is closed and it sends nothing more. */
  closed = false
  /** Local updates the writer produced while it applied a room update. */
  writesDuringRemote = 0
  private applying = false
  private readonly outbox: Uint8Array[] = []
  private state: EditorState
  // The plugin view renders the replica while it is created, before it can
  // take updates.
  private pluginView: PluginView | null = null
  private readonly view: { dispatch: (transaction: Transaction) => void }

  constructor(readonly userId: string, clientId: number, initial: Uint8Array) {
    this.document.clientID = clientId
    Y.applyUpdate(this.document, initial, FROM_ROOM)
    // As the browser does: the textblocks a local change created get their
    // empty text node right after it.
    this.document.on('afterTransaction', (transaction: Y.Transaction) => {
      if (transaction.origin !== FROM_ROOM) completeCreatedTextblocks(transaction)
    })
    const plugin = ySyncPlugin(this.document.getXmlFragment(EDITOR_YJS_FRAGMENT))
    this.state = EditorState.create({ plugins: [plugin], schema: SCHEMA })
    const writerState = (): EditorState => this.state
    const view = {
      dispatch: (transaction: Transaction) => {
        const previous = this.state
        this.state = this.state.apply(transaction)
        this.pluginView?.update?.(view as unknown as EditorView, previous)
      },
      hasFocus: () => false,
      get state() {
        return writerState()
      },
    }
    this.view = view
    this.pluginView = plugin.spec.view!(view as unknown as EditorView)
    this.document.on('update', (update: Uint8Array, origin: unknown) => {
      if (origin === FROM_ROOM) return
      if (this.applying) this.writesDuringRemote += 1
      this.outbox.push(update)
    })
  }

  /** The caret goes to `offset` in top-level block `block`, as a click. */
  placeCaret(block: number, offset = 0): void {
    let position = 0
    for (let index = 0; index < block; index += 1) position += this.state.doc.child(index).nodeSize
    this.view.dispatch(this.state.tr.setSelection(TextSelection.create(this.state.doc, position + 1 + offset)))
  }

  type(text: string): void {
    this.view.dispatch(this.state.tr.insertText(text))
  }

  enter(): void {
    splitBlock(this.state, this.view.dispatch)
  }

  hardBreak(): void {
    this.view.dispatch(this.state.tr.replaceSelectionWith(SCHEMA.nodes.hardBreak!.create()))
  }

  /** Whether the caret stands at the end of its line. */
  caretAtLineEnd(): boolean {
    const { $from } = this.state.selection
    return $from.parentOffset === $from.parent.content.size
  }

  receive(update: Uint8Array): void {
    this.applying = true
    try {
      Y.applyUpdate(this.document, update, FROM_ROOM)
    } finally {
      this.applying = false
    }
  }

  /** Everything this writer wrote since the last call, as one update. */
  take(): Uint8Array | null {
    const updates = this.outbox.splice(0)
    return updates.length > 0 ? Y.mergeUpdates(updates) : null
  }

  texts(): string[] {
    const texts: string[] = []
    this.state.doc.forEach((node) => texts.push(node.textContent))
    return texts
  }

  destroy(): void {
    this.pluginView?.destroy?.()
    this.document.destroy()
  }
}

async function openRoom(initial: Uint8Array) {
  const configured = settings()
  const api = new FakeCollaborationApi()
  const seed = new Y.Doc()
  Y.applyUpdate(seed, initial)
  api.loadedState = await documentState(DOCUMENT_ID, seed)
  seed.destroy()
  const metrics = new SidecarMetrics()
  const lease = new InstanceLeaseManager(api, configured, silentLogger, metrics, () => undefined)
  await lease.start()
  const coordinator = new DocumentCoordinator(api, lease, configured, silentLogger, metrics)
  coordinator.initialize(ROOM, 0, { bytes: 0, updates: 0 })
  const schemaHash = await getEditorSchemaFingerprint()
  const document = new Y.Doc()
  Y.applyUpdate(document, initial)
  let connections = 0

  /** One update of a writer through the acceptance path. Returns the verdict
   * and every frame the room broadcast for it. */
  async function submit(
    userId: string,
    update: Uint8Array,
    access: 'edit' | 'suggest' = 'edit',
  ): Promise<{ frames: Uint8Array[]; verdict: string }> {
    connections += 1
    const connectionId = `connection-${connections}`
    const context: ConnectionContext = {
      access,
      documentId: DOCUMENT_ID,
      expiresAt: Date.now() / 1_000 + 600,
      generation: 1,
      leaseId: `lease-${userId}`,
      policyCursor: 0,
      protocolVersion: configured.protocolVersion,
      schemaHash,
      schemaVersion: configured.schemaVersion,
      sessionId: `session-${userId}`,
      tenantId: configured.tenantId,
      user: { color: '#123456', id: userId, name: 'Writer' },
    }
    const frames: Uint8Array[] = []
    const observe = (frame: Uint8Array): void => {
      frames.push(frame)
    }
    document.on('update', observe)
    try {
      const result = await coordinator.prepareClientUpdate({
        allowNoop: false,
        connectionId,
        context,
        document,
        room: ROOM,
        update,
      })
      if (result !== 'pending') return { frames, verdict: result }
      // The transport applies the client's bytes after the service prepared
      // them, as Hocuspocus does.
      Y.applyUpdate(document, update)
      coordinator.finishClientUpdate(connectionId, document)
      return { frames, verdict: 'accepted' }
    } catch (error) {
      if (error instanceof CollaborationError) return { frames, verdict: `${error.code} ${error.reason}` }
      throw error
    } finally {
      document.off('update', observe)
    }
  }

  /** Whether replaying every stored row gives exactly the room's state. */
  async function storeMatchesRoom(): Promise<boolean> {
    const replayed = await reconstructValidatedDocument(api.loadedState!, {
      documentId: DOCUMENT_ID,
      generation: 1,
      schemaVersion: configured.schemaVersion,
    }, configured.documentLimitBytes)
    try {
      return hashBytes(Y.encodeStateAsUpdate(replayed.document))
        === hashBytes(Y.encodeStateAsUpdate(document))
    } finally {
      replayed.document.destroy()
    }
  }

  return {
    close: async () => {
      document.destroy()
      await lease.stop()
    },
    document,
    storeMatchesRoom,
    submit,
  }
}

type Room = Awaited<ReturnType<typeof openRoom>>

/** One update of a writer goes to the room. An accepted one reaches every
 * writer; a refused one closes the writer's connection, and the room
 * processes nothing more from it. */
async function send(
  room: Room,
  writer: BoundWriter,
  update: Uint8Array | null,
  writers: readonly BoundWriter[],
  verdicts: string[],
): Promise<boolean> {
  if (!update || writer.closed) return false
  const { frames, verdict } = await room.submit(writer.userId, update)
  verdicts.push(verdict)
  if (verdict !== 'accepted' && verdict !== 'noop') {
    writer.closed = true
    return true
  }
  for (const frame of frames) {
    for (const other of writers) other.receive(frame)
  }
  return true
}

/** Whatever each writer still holds goes out too, until nothing is left.
 * Bounded: a loop that never settles is a finding, not a hang. */
async function settle(room: Room, writers: readonly BoundWriter[], verdicts: string[]): Promise<number> {
  for (let round = 0; round < MAX_FOLLOW_UP_ROUNDS; round += 1) {
    let sent = false
    for (const writer of writers) {
      if (await send(room, writer, writer.take(), writers, verdicts)) sent = true
    }
    if (!sent) return round
  }
  return MAX_FOLLOW_UP_ROUNDS
}

function roomParagraphs(room: Room): string[] {
  return (editorYDocToJson(room.document).content ?? []).map((node) => (
    (node.content ?? []).map((child) => child.text ?? (child.type === 'hardBreak' ? '|' : '')).join('')
  ))
}

function roomValid(room: Room): boolean {
  try {
    validateDocument(room.document, settings().documentLimitBytes)
    return true
  } catch {
    return false
  }
}

/** The number of text nodes in every textblock of the room, in order. */
function textNodesPerTextblock(document: Y.Doc): number[] {
  const counts: number[] = []
  const visit = (element: Y.XmlElement): void => {
    if (EDITOR_YJS_TEXTBLOCK_NAMES.has(element.nodeName)) {
      counts.push(element.toArray().filter((child) => child instanceof Y.XmlText).length)
      return
    }
    for (const child of element.toArray()) {
      if (child instanceof Y.XmlElement) visit(child)
    }
  }
  for (const child of document.getXmlFragment(EDITOR_YJS_FRAGMENT).toArray()) {
    if (child instanceof Y.XmlElement) visit(child)
  }
  return counts
}

/** Whether the writer's replica holds the room's state byte for byte. */
function holdsRoomState(room: Room, writer: BoundWriter): boolean {
  return hashBytes(Y.encodeStateAsUpdate(writer.document))
    === hashBytes(Y.encodeStateAsUpdate(room.document))
}

/** What the room, the store and every writer ended with. */
async function outcome(room: Room, writers: readonly BoundWriter[], rounds: number, verdicts: string[]) {
  const line = roomParagraphs(room)
  return {
    closed: writers.map((writer) => writer.closed),
    replicasAgree: writers.every((writer) => JSON.stringify(writer.texts()) === JSON.stringify(line)),
    replicasHoldRoomState: writers.every((writer) => holdsRoomState(room, writer)),
    roomValid: roomValid(room),
    settled: rounds < MAX_FOLLOW_UP_ROUNDS,
    storeMatchesRoom: await room.storeMatchesRoom(),
    textNodesPerTextblock: textNodesPerTextblock(room.document).every((count) => count === 1),
    verdicts: verdicts.filter((verdict) => verdict !== 'accepted'),
    writesDuringRemote: writers.map((writer) => writer.writesDuringRemote),
  }
}

const SETTLED = {
  closed: [false, false],
  replicasAgree: true,
  replicasHoldRoomState: true,
  roomValid: true,
  settled: true,
  storeMatchesRoom: true,
  textNodesPerTextblock: true,
  verdicts: [],
  writesDuringRemote: [0, 0],
}

/** Concurrent characters at one place are ordered by client id, the lower
 * one first. */
const ORDERS = [
  { first: 2_000, label: 'the second writer\'s text sorts first', second: 1_000, shown: 'BetaAlpha' },
  { first: 1_000, label: 'the second writer\'s text sorts last', second: 2_000, shown: 'AlphaBeta' },
] as const

describe('two writers typing into the same empty line at the same time', () => {
  const writers: BoundWriter[] = []
  afterEach(() => {
    for (const writer of writers.splice(0)) writer.destroy()
  })

  function twoWriters(initial: Uint8Array, firstId: number, secondId: number): [BoundWriter, BoundWriter] {
    const first = new BoundWriter(WRITER_A, firstId, initial)
    const second = new BoundWriter(WRITER_B, secondId, initial)
    writers.push(first, second)
    return [first, second]
  }

  it.each(ORDERS)('accepts both writers\' first characters in a stored empty line both clicked into, and every replica, the room and the store show both words once ($label)', async ({ first: firstId, second: secondId, shown }) => {
    const initial = stored(EMPTY_LINE_DOCUMENT)
    const room = await openRoom(initial)
    try {
      const [first, second] = twoWriters(initial, firstId, secondId)
      first.placeCaret(1)
      second.placeCaret(1)
      first.type('Alpha')
      second.type('Beta')
      // Both updates are on the wire before either writer received anything.
      const fromFirst = first.take()
      const fromSecond = second.take()

      const verdicts: string[] = []
      await send(room, first, fromFirst, [first, second], verdicts)
      await send(room, second, fromSecond, [first, second], verdicts)
      const rounds = await settle(room, [first, second], verdicts)

      expect(roomParagraphs(room)).toEqual(['Oben.', shown, 'Unten.'])
      expect(await outcome(room, [first, second], rounds, verdicts)).toEqual(SETTLED)
    } finally {
      await room.close()
    }
  })

  it.each(ORDERS)('keeps every character once and each word whole when both writers type on in a stored empty line while the other\'s first characters arrive ($label)', async ({ first: firstId, second: secondId }) => {
    // People type whole words. Each writer's first characters travel alone;
    // the rest follows while the other writer's first characters are still
    // on their way.
    const initial = stored(EMPTY_LINE_DOCUMENT)
    const room = await openRoom(initial)
    try {
      const [first, second] = twoWriters(initial, firstId, secondId)
      first.placeCaret(1)
      second.placeCaret(1)
      first.type('Al')
      second.type('Be')
      const firstStart = first.take()
      const secondStart = second.take()

      const verdicts: string[] = []
      await send(room, first, firstStart, [first, second], verdicts)
      second.type('ta')
      const secondRest = second.take()
      await send(room, second, secondStart, [first, second], verdicts)
      first.type('pha')
      await send(room, second, secondRest, [first, second], verdicts)
      await send(room, first, first.take(), [first, second], verdicts)
      const rounds = await settle(room, [first, second], verdicts)

      expect(['AlphaBeta', 'BetaAlpha']).toContain(roomParagraphs(room)[1])
      expect(await outcome(room, [first, second], rounds, verdicts)).toEqual(SETTLED)
    } finally {
      await room.close()
    }
  })

  it('accepts both writers\' first characters in a line one of them just created with Enter and the other clicked into', async () => {
    // No caret moves on its own here: the second writer receives the new
    // empty line and deliberately clicks into it, as a colleague joining a
    // sentence does.
    const initial = stored(TWO_LINES)
    const room = await openRoom(initial)
    try {
      const [first, second] = twoWriters(initial, 2_000, 1_000)
      const verdicts: string[] = []
      first.placeCaret(0, 'Oben.'.length)
      first.enter()
      await send(room, first, first.take(), [first, second], verdicts)
      expect(second.texts()).toEqual(['Oben.', '', 'Unten.'])
      second.placeCaret(1)
      first.type('Alpha')
      second.type('Beta')
      const fromFirst = first.take()
      const fromSecond = second.take()

      await send(room, first, fromFirst, [first, second], verdicts)
      await send(room, second, fromSecond, [first, second], verdicts)
      const rounds = await settle(room, [first, second], verdicts)

      expect(roomParagraphs(room)).toEqual(['Oben.', 'BetaAlpha', 'Unten.'])
      expect(await outcome(room, [first, second], rounds, verdicts)).toEqual(SETTLED)
    } finally {
      await room.close()
    }
  })

  it.each(ORDERS)('keeps every word whole when both writers type on in a line one of them just created with Enter ($label)', async ({ first: firstId, second: secondId }) => {
    const initial = stored(TWO_LINES)
    const room = await openRoom(initial)
    try {
      const [first, second] = twoWriters(initial, firstId, secondId)
      const verdicts: string[] = []
      first.placeCaret(0, 'Oben.'.length)
      first.enter()
      await send(room, first, first.take(), [first, second], verdicts)
      second.placeCaret(1)
      first.type('Al')
      second.type('Be')
      const firstStart = first.take()
      const secondStart = second.take()
      await send(room, first, firstStart, [first, second], verdicts)
      second.type('ta')
      const secondRest = second.take()
      await send(room, second, secondStart, [first, second], verdicts)
      first.type('pha')
      await send(room, second, secondRest, [first, second], verdicts)
      await send(room, first, first.take(), [first, second], verdicts)
      const rounds = await settle(room, [first, second], verdicts)

      expect(['AlphaBeta', 'BetaAlpha']).toContain(roomParagraphs(room)[1])
      expect(await outcome(room, [first, second], rounds, verdicts)).toEqual(SETTLED)
    } finally {
      await room.close()
    }
  })

  // Both writers press Enter at the end of the same line at nearly the same
  // time, then both type into the first new line. Where the characters land
  // is the editor stack's own ordering of concurrent characters at one place:
  // the result equals the same keystrokes in a line that already held text
  // (here "X", removed afterwards), and nothing is refused.
  it.each(ORDERS)('treats two writers typing after both pressed Enter at the same line end like two writers typing in a line that holds text ($label)', async ({ first: firstId, second: secondId }) => {
    async function keystrokes(
      initial: Uint8Array,
      prepare: (first: BoundWriter, second: BoundWriter, verdicts: string[], room: Room) => Promise<void>,
    ) {
      const room = await openRoom(initial)
      try {
        const [first, second] = twoWriters(initial, firstId, secondId)
        const verdicts: string[] = []
        await prepare(first, second, verdicts, room)
        first.type('D')
        second.type('D')
        const fromFirst = first.take()
        const fromSecond = second.take()
        await send(room, first, fromFirst, [first, second], verdicts)
        await send(room, second, fromSecond, [first, second], verdicts)
        first.type('as')
        second.type('ie')
        await send(room, first, first.take(), [first, second], verdicts)
        await send(room, second, second.take(), [first, second], verdicts)
        const rounds = await settle(room, [first, second], verdicts)
        return {
          line: roomParagraphs(room)[1] ?? '',
          outcome: await outcome(room, [first, second], rounds, verdicts),
        }
      } finally {
        await room.close()
      }
    }

    const afterEnters = await keystrokes(stored(TWO_LINES), async (first, second, verdicts, room) => {
      first.placeCaret(0, 'Oben.'.length)
      second.placeCaret(0, 'Oben.'.length)
      first.enter()
      second.enter()
      const fromFirst = first.take()
      const fromSecond = second.take()
      await send(room, first, fromFirst, [first, second], verdicts)
      await send(room, second, fromSecond, [first, second], verdicts)
      expect(first.texts()).toEqual(['Oben.', '', '', 'Unten.'])
      first.placeCaret(1)
      second.placeCaret(1)
    })
    const inText = await keystrokes(stored({
      content: [paragraph('Oben.'), paragraph('X'), paragraph('Unten.')],
      type: 'doc',
    }), async (first, second) => {
      first.placeCaret(1, 1)
      second.placeCaret(1, 1)
    })

    expect(afterEnters.outcome).toEqual(SETTLED)
    expect(inText.outcome).toEqual(SETTLED)
    expect(afterEnters.line).toHaveLength('DasDie'.length)
    expect(afterEnters.line).toBe(inText.line.replace('X', ''))
  })

  // An editor and a suggester type into the same empty line at the same
  // time. Both are accepted, whichever update reaches the room first: the
  // suggestion marks exactly the suggester's characters, in the suggester's
  // name, and the editor's characters carry no mark.
  it.each([
    { editor: 7_000, first: 'editor', suggester: 6_000 },
    { editor: 6_000, first: 'editor', suggester: 7_000 },
    { editor: 7_000, first: 'suggester', suggester: 6_000 },
    { editor: 6_000, first: 'suggester', suggester: 7_000 },
  ] as const)('accepts an editor and a suggester typing into the same empty line, each in their own name (editor $editor, suggester $suggester, first at the room: $first)', async ({ editor, first, suggester }) => {
    const initial = stored(EMPTY_LINE_DOCUMENT)
    const room = await openRoom(initial)
    try {
      const typed = typedIntoLine(initial, editor, 'Text')
      const proposed = typedIntoLine(initial, suggester, 'Idee', WRITER_B)
      const fromEditor = () => room.submit(WRITER_A, typed, 'edit')
      const fromSuggester = () => room.submit(WRITER_B, proposed, 'suggest')
      const verdicts: string[] = []
      for (const submit of first === 'editor' ? [fromEditor, fromSuggester] : [fromSuggester, fromEditor]) {
        verdicts.push((await submit()).verdict)
      }
      const runs = (editorYDocToJson(room.document).content?.[1]?.content ?? []).map((run) => ({
        marks: (run.marks ?? []).map((mark) => `${mark.type}:${String(mark.attrs?.authorId)}`),
        text: run.text,
      }))

      expect(verdicts).toEqual(['accepted', 'accepted'])
      expect(runs).toEqual(suggester < editor
        ? [{ marks: [`insertion:${WRITER_B}`], text: 'Idee' }, { marks: [], text: 'Text' }]
        : [{ marks: [], text: 'Text' }, { marks: [`insertion:${WRITER_B}`], text: 'Idee' }])
      expect(textNodesPerTextblock(room.document)).toEqual([1, 1, 1])
      expect(roomValid(room)).toBe(true)
      expect(await room.storeMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  // Two suggesters type into the same empty line at the same time. Both are
  // accepted, and each proposal marks exactly its own author's characters.
  it.each([
    { first: 6_000, second: 7_000, shown: ['Idee', 'Plan'] },
    { first: 7_000, second: 6_000, shown: ['Plan', 'Idee'] },
  ] as const)('accepts two suggesters typing into the same empty line, each proposal in its author\'s name (clients $first, $second)', async ({ first, second, shown }) => {
    const initial = stored(EMPTY_LINE_DOCUMENT)
    const room = await openRoom(initial)
    try {
      const fromFirst = typedIntoLine(initial, first, 'Idee', WRITER_A)
      const fromSecond = typedIntoLine(initial, second, 'Plan', WRITER_B, {
        patchId: SECOND_PATCH_ID,
        suggestionId: SECOND_SUGGESTION_ID,
      })
      const verdicts = [
        (await room.submit(WRITER_A, fromFirst, 'suggest')).verdict,
        (await room.submit(WRITER_B, fromSecond, 'suggest')).verdict,
      ]
      const runs = (editorYDocToJson(room.document).content?.[1]?.content ?? []).map((run) => ({
        marks: (run.marks ?? []).map((mark) => (
          `${mark.type}:${String(mark.attrs?.authorId)}:${String(mark.attrs?.suggestionId)}`
        )),
        text: run.text,
      }))
      const proposals = {
        Idee: `insertion:${WRITER_A}:${SUGGESTION_ID}`,
        Plan: `insertion:${WRITER_B}:${SECOND_SUGGESTION_ID}`,
      }

      expect(verdicts).toEqual(['accepted', 'accepted'])
      expect(runs).toEqual(shown.map((text) => ({ marks: [proposals[text]], text })))
      expect(textNodesPerTextblock(room.document)).toEqual([1, 1, 1])
      expect(roomValid(room)).toBe(true)
      expect(await room.storeMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })
})

/** The update a client writes when it types `text` at the start of the
 * second line of `base`: directly, or as a suggestion in `author`'s name
 * (`proposal` names it when one fixture holds more than one). */
function typedIntoLine(
  base: Uint8Array,
  clientId: number,
  text: string,
  author?: string,
  proposal: { patchId: string; suggestionId: string } = {
    patchId: PATCH_ID,
    suggestionId: SUGGESTION_ID,
  },
): Uint8Array {
  const replica = new Y.Doc()
  replica.clientID = clientId
  Y.applyUpdate(replica, base)
  const vector = Y.encodeStateVector(replica)
  const fragment = replica.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, SCHEMA)
  const state = EditorState.create({ doc: initialized.doc, schema: SCHEMA })
  const lineStart = state.doc.child(0).nodeSize + 1
  const direct = state.tr.insertText(text, lineStart)
  const target = author
    ? transformToInqtrixSuggestionTransaction(
        direct,
        state,
        { authorId: author, createdAt: CREATED_AT, patchId: proposal.patchId },
        () => proposal.suggestionId,
      ).doc
    : direct.doc
  updateYFragment(replica, fragment, target, initialized.meta)
  const update = Y.encodeStateAsUpdate(replica, vector)
  replica.destroy()
  return update
}

/** An update written directly into the shared types of `base`, as a
 * modified or outdated client could write it. */
function rawUpdate(base: Uint8Array, clientId: number, change: (fragment: Y.XmlFragment) => void): Uint8Array {
  const replica = new Y.Doc()
  replica.clientID = clientId
  Y.applyUpdate(replica, base)
  const vector = Y.encodeStateVector(replica)
  replica.transact(() => change(replica.getXmlFragment(EDITOR_YJS_FRAGMENT)))
  const update = Y.encodeStateAsUpdate(replica, vector)
  replica.destroy()
  return update
}

/** The insertion mark of a suggestion by `author`, with every attribute the
 * schema requires: a mark that lacks one is refused as an invalid schema
 * wherever it stands, and would prove nothing about the place it stands in. */
function insertionBy(author: string): Record<string, unknown> {
  return {
    insertion: {
      authorId: author,
      createdAt: CREATED_AT,
      id: SUGGESTION_ID,
      kind: 'insertion',
      patchId: PATCH_ID,
      suggestionId: SUGGESTION_ID,
    },
  }
}

describe('what stays refused around an empty line', () => {
  // The same characters with the same mark are judged in two places: in the
  // line's own text node, where only the suggestion rules decide (`inLine`),
  // and in a second text node beside it, which the schema refuses whatever
  // those rules say. The first verdict shows that the second is about the
  // text node and not about the characters or their mark.
  it.each([
    { access: 'edit', author: null, inLine: 'accepted', label: 'plain text from an edit lease', user: WRITER_A },
    { access: 'suggest', author: WRITER_B, inLine: 'accepted', label: 'the suggester\'s own insertion', user: WRITER_B },
    { access: 'edit', author: WRITER_B, inLine: POLICY_REFUSAL, label: 'another person\'s insertion from an edit lease', user: WRITER_A },
  ] as const)('refuses a second text node beside the text node of an empty line: $label', async ({ access, author, inLine, user }) => {
    const initial = stored(EMPTY_LINE_DOCUMENT)
    const written = (beside: boolean): Uint8Array => rawUpdate(initial, 3_000, (fragment) => {
      const line = fragment.get(1) as Y.XmlElement
      let text = line.get(0) as Y.XmlText
      if (beside) {
        text = new Y.XmlText()
        line.insert(0, [text])
      }
      text.insert(0, 'Beta', author ? insertionBy(author) : {})
    })
    const control = await openRoom(initial)
    const room = await openRoom(initial)
    try {
      const before = Y.encodeStateAsUpdate(room.document)

      expect((await control.submit(user, written(false), access)).verdict).toBe(inLine)
      expect((await room.submit(user, written(true), access)).verdict).toBe(INVALID_SCHEMA)
      expect(Y.encodeStateAsUpdate(room.document)).toEqual(before)
    } finally {
      await control.close()
      await room.close()
    }
  })

  it.each([
    { access: 'suggest', user: WRITER_B },
    { access: 'edit', user: WRITER_B },
  ] as const)('refuses characters typed into an empty line as a suggestion in another person\'s name ($access lease)', async ({ access, user }) => {
    const initial = stored(EMPTY_LINE_DOCUMENT)
    const room = await openRoom(initial)
    try {
      const forged = typedIntoLine(initial, 3_001, 'Idee', WRITER_A)

      expect((await room.submit(user, forged, access)).verdict).toBe(POLICY_REFUSAL)
      expect(roomParagraphs(room)).toEqual(['Oben.', '', 'Unten.'])
    } finally {
      await room.close()
    }
  })

  it.each([
    ['without a text node', false],
    ['with its empty text node', true],
  ] as const)('refuses a suggester\'s new line that proposes no text, %s', async (_label, withText) => {
    const initial = stored(TWO_LINES)
    const room = await openRoom(initial)
    try {
      const update = rawUpdate(initial, 3_002, (fragment) => {
        const line = new Y.XmlElement('paragraph')
        fragment.insert(1, [line])
        if (withText) line.insert(0, [new Y.XmlText()])
      })

      expect((await room.submit(WRITER_B, update, 'suggest')).verdict).toBe(POLICY_REFUSAL)
      expect(roomParagraphs(room)).toEqual(['Oben.', 'Unten.'])
    } finally {
      await room.close()
    }
  })

  it('refuses a suggester who removes the text node of an empty line', async () => {
    const initial = stored(EMPTY_LINE_DOCUMENT)
    const room = await openRoom(initial)
    try {
      const update = rawUpdate(initial, 3_003, (fragment) => {
        (fragment.get(1) as Y.XmlElement).delete(0, 1)
      })

      expect((await room.submit(WRITER_B, update, 'suggest')).verdict).toBe(POLICY_REFUSAL)
      expect(textNodesPerTextblock(room.document)).toEqual([1, 1, 1])
    } finally {
      await room.close()
    }
  })

  // The service takes the empty text node for a line stored without one only
  // from a writer with edit rights: the node shows nothing, so the schema
  // accepts it, and the suggestion rules refuse a suggest-only lease that
  // writes anything but a proposal.
  it.each([
    ['accepts', 'edit', 'accepted', [1, 1, 1]],
    ['refuses', 'suggest', POLICY_REFUSAL, [1, 0, 1]],
  ] as const)('%s the empty text node written into a line stored without one from a lease to %s', async (_expectation, access, verdict, nodes) => {
    const initial = storedWithoutTextNode(EMPTY_LINE_DOCUMENT)
    const room = await openRoom(initial)
    try {
      const update = rawUpdate(initial, 3_004, (fragment) => {
        (fragment.get(1) as Y.XmlElement).insert(0, [new Y.XmlText()])
      })

      expect((await room.submit(WRITER_B, update, access)).verdict).toBe(verdict)
      expect(textNodesPerTextblock(room.document)).toEqual(nodes)
      expect(roomParagraphs(room)).toEqual(['Oben.', '', 'Unten.'])
    } finally {
      await room.close()
    }
  })
})

describe('limits that remain', () => {
  const writers: BoundWriter[] = []
  afterEach(() => {
    for (const writer of writers.splice(0)) writer.destroy()
  })

  it('accepts one writer typing into a stored empty line that has no text node yet', async () => {
    const initial = storedWithoutTextNode(EMPTY_LINE_DOCUMENT)
    const room = await openRoom(initial)
    try {
      const writer = new BoundWriter(WRITER_A, 1_000, initial)
      writers.push(writer)
      writer.placeCaret(1)
      writer.type('Alpha')
      const verdicts: string[] = []
      await send(room, writer, writer.take(), [writer], verdicts)

      expect(verdicts).toEqual(['accepted'])
      expect(roomParagraphs(room)).toEqual(['Oben.', 'Alpha', 'Unten.'])
      expect(await room.storeMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  // A stored line without a text node gets none from a writer that did not
  // create it; two writers who type into it at the same time still write two
  // text nodes side by side, and the second is refused.
  it.fails('keeps both words when two writers type into a stored empty line that has no text node yet', async () => {
    const initial = storedWithoutTextNode(EMPTY_LINE_DOCUMENT)
    const room = await openRoom(initial)
    try {
      const first = new BoundWriter(WRITER_A, 2_000, initial)
      const second = new BoundWriter(WRITER_B, 1_000, initial)
      writers.push(first, second)
      first.placeCaret(1)
      second.placeCaret(1)
      first.type('Alpha')
      second.type('Beta')
      const fromFirst = first.take()
      const fromSecond = second.take()
      const verdicts: string[] = []
      await send(room, first, fromFirst, [first, second], verdicts)
      await send(room, second, fromSecond, [first, second], verdicts)

      expect(verdicts).toEqual(['accepted', 'accepted'])
    } finally {
      await room.close()
    }
  })

  // The text node an empty line keeps exists only while it is the line's only
  // child. Right after an inline node such as a line break, the binding
  // writes the first character into a new text node, and two writers who
  // type there at the same time still write two side by side.
  it.fails('keeps both words when two writers type right after the same line break at the same time', async () => {
    const initial = stored(TWO_LINES)
    const room = await openRoom(initial)
    try {
      const first = new BoundWriter(WRITER_A, 2_000, initial)
      const second = new BoundWriter(WRITER_B, 1_000, initial)
      writers.push(first, second)
      const verdicts: string[] = []
      first.placeCaret(0, 'Oben.'.length)
      first.hardBreak()
      await send(room, first, first.take(), [first, second], verdicts)
      second.placeCaret(0, 'Oben.'.length + 1)
      first.type('Alpha')
      second.type('Beta')
      const fromFirst = first.take()
      const fromSecond = second.take()
      await send(room, first, fromFirst, [first, second], verdicts)
      await send(room, second, fromSecond, [first, second], verdicts)

      expect(verdicts).toEqual(['accepted', 'accepted', 'accepted'])
    } finally {
      await room.close()
    }
  })

  // The binding keeps a text node only beside text: a line break (or a
  // formula) that becomes the first content of an empty line takes the place
  // of the line's empty text node. Characters another writer typed into that
  // text node at the same moment are removed with it. It is what the binding
  // does in every line whose text was deleted. Both updates are accepted and
  // every replica ends with the room's state; only the typed word is gone.
  async function lineBreakWhileTheOtherTypes() {
    const initial = stored(EMPTY_LINE_DOCUMENT)
    const room = await openRoom(initial)
    try {
      const first = new BoundWriter(WRITER_A, 2_000, initial)
      const second = new BoundWriter(WRITER_B, 1_000, initial)
      writers.push(first, second)
      first.placeCaret(1)
      second.placeCaret(1)
      first.hardBreak()
      second.type('Beta')
      const fromFirst = first.take()
      const fromSecond = second.take()
      const verdicts: string[] = []
      await send(room, first, fromFirst, [first, second], verdicts)
      await send(room, second, fromSecond, [first, second], verdicts)
      const rounds = await settle(room, [first, second], verdicts)
      const ended = await outcome(room, [first, second], rounds, verdicts)
      return {
        closed: ended.closed,
        line: roomParagraphs(room)[1] ?? '',
        replicasHoldRoomState: ended.replicasHoldRoomState,
        roomValid: ended.roomValid,
        storeMatchesRoom: ended.storeMatchesRoom,
        verdicts: ended.verdicts,
        writesDuringRemote: ended.writesDuringRemote,
      }
    } finally {
      await room.close()
    }
  }

  it('accepts a line break put into an empty line while another writer types into it, and every replica holds the room\'s state', async () => {
    const { line, ...ended } = await lineBreakWhileTheOtherTypes()

    // The line break is in the line (the room's text shows it as `|`).
    expect(line).toContain('|')
    expect(ended).toEqual({
      closed: [false, false],
      replicasHoldRoomState: true,
      roomValid: true,
      storeMatchesRoom: true,
      verdicts: [],
      writesDuringRemote: [0, 0],
    })
  })

  it.fails('keeps the word a writer types into an empty line while another writer puts a line break into it', async () => {
    expect((await lineBreakWhileTheOtherTypes()).line).toContain('Beta')
  })
})

// The frame an editor without the empty text node writes for its first
// character in an empty line whose first text another writer's update
// brought to the room just before: a new text node at the start of the line
// (no left or right neighbour known to the sender) holding "D". Client
// 595423906 writes it into the paragraph element 2886455771:323, right after
// its own clock 192.
const FIRST_CHARACTER_UPDATE = 'AQKi5fWbAsEBBwDbo6/gCsMCBgQAouX1mwLBAQFEAA=='
const SENDER_CLIENT = 595_423_906
const STARTER_CLIENT = 2_886_455_771
const STARTED_SENTENCE = 'Das Projekt bewertet die Daten.'

/** A room in which the starter's earlier typing took clocks 0..322, its
 * Enter created the empty paragraph at clock 323 without a text node, and
 * its first sentence went into a text node of its own; the sender's earlier
 * typing took clocks 0..192. */
function roomWithStartedParagraphWithoutTextNode(): Y.Doc {
  const base = editorJsonToYDoc(TWO_LINES)
  const starter = new Y.Doc()
  Y.applyUpdate(starter, Y.encodeStateAsUpdate(base))
  starter.clientID = STARTER_CLIENT
  const fragment = starter.getXmlFragment(EDITOR_YJS_FRAGMENT)
  starter.transact(() => {
    const text = (fragment.get(0) as Y.XmlElement).get(0) as Y.XmlText
    text.insert(text.length, ' x'.repeat(161) + 'x')
  })
  starter.transact(() => {
    fragment.insert(1, [new Y.XmlElement('paragraph')])
  })
  starter.transact(() => {
    const started = new Y.XmlText()
    ;(fragment.get(1) as Y.XmlElement).insert(0, [started])
    started.insert(0, STARTED_SENTENCE)
  })
  const sender = new Y.Doc()
  Y.applyUpdate(sender, Y.encodeStateAsUpdate(starter))
  sender.clientID = SENDER_CLIENT
  sender.transact(() => {
    const text = (sender.getXmlFragment(EDITOR_YJS_FRAGMENT).get(2) as Y.XmlElement).get(0) as Y.XmlText
    text.insert(text.length, ' y'.repeat(96) + 'y')
  })
  const room = new Y.Doc()
  Y.applyUpdate(room, Y.encodeStateAsUpdate(sender))
  for (const document of [base, starter, sender]) document.destroy()
  return room
}

describe('the first character a writer types into a line another writer has just started', () => {
  const writers: BoundWriter[] = []
  afterEach(() => {
    for (const writer of writers.splice(0)) writer.destroy()
  })

  it('accepts the character in the line\'s one text node and shows it before the started sentence, where Yjs places it', async () => {
    // The starter presses Enter; its update carries the new line with its
    // text node. The sender receives only that and types "D" while the
    // starter's first sentence is on its way.
    const initial = stored(TWO_LINES)
    const room = await openRoom(initial)
    try {
      const starter = new BoundWriter(WRITER_A, STARTER_CLIENT, initial)
      const sender = new BoundWriter(WRITER_B, SENDER_CLIENT, initial)
      writers.push(starter, sender)
      const verdicts: string[] = []
      starter.placeCaret(0, 'Oben.'.length)
      starter.enter()
      await send(room, starter, starter.take(), [starter, sender], verdicts)
      sender.placeCaret(1)
      starter.type(STARTED_SENTENCE)
      sender.type('D')
      const fromStarter = starter.take()
      const fromSender = sender.take()
      await send(room, starter, fromStarter, [starter, sender], verdicts)
      await send(room, sender, fromSender, [starter, sender], verdicts)
      const rounds = await settle(room, [starter, sender], verdicts)

      expect(roomParagraphs(room)).toEqual(['Oben.', `D${STARTED_SENTENCE}`, 'Unten.'])
      expect(await outcome(room, [starter, sender], rounds, verdicts)).toEqual(SETTLED)
    } finally {
      await room.close()
    }
  })

  it('still refuses the frame that puts a second text node beside the started one', async () => {
    const update = Buffer.from(FIRST_CHARACTER_UPDATE, 'base64')
    const built = roomWithStartedParagraphWithoutTextNode()
    const initial = Y.encodeStateAsUpdate(built)
    const vector = Y.decodeStateVector(Y.encodeStateVector(built))
    const paragraphItem = (built.getXmlFragment(EDITOR_YJS_FRAGMENT).get(1) as Y.XmlElement)._item!
    // The fixture holds the context the frame names, and is itself valid:
    // the frame is judged on this room, not on a broken one.
    expect({
      paragraph: `${paragraphItem.id.client}:${paragraphItem.id.clock}`,
      parent: (() => {
        const struct = Y.decodeUpdate(update).structs[0] as Y.Item
        return `${(struct.parent as Y.ID).client}:${(struct.parent as Y.ID).clock}`
      })(),
      senderClock: vector.get(SENDER_CLIENT),
      valid: (() => {
        try {
          validateDocument(built, settings().documentLimitBytes)
          return true
        } catch {
          return false
        }
      })(),
    }).toEqual({
      paragraph: `${STARTER_CLIENT}:323`,
      parent: `${STARTER_CLIENT}:323`,
      senderClock: 193,
      valid: true,
    })
    built.destroy()

    const room = await openRoom(initial)
    try {
      const { verdict } = await room.submit(WRITER_B, update)

      expect({
        line: roomParagraphs(room)[1],
        roomValid: roomValid(room),
        storeMatchesRoom: await room.storeMatchesRoom(),
        verdict,
      }).toEqual({
        line: STARTED_SENTENCE,
        roomValid: true,
        storeMatchesRoom: true,
        verdict: INVALID_SCHEMA,
      })
    } finally {
      await room.close()
    }
  })
})

/** A deterministic sequence of numbers in [0, 1) for a seed. */
function seededRandom(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (state + 0x6D2B79F5) | 0
    let mixed = Math.imul(state ^ (state >>> 15), 1 | state)
    mixed = (mixed + Math.imul(mixed ^ (mixed >>> 7), 61 | mixed)) ^ mixed
    return ((mixed ^ (mixed >>> 14)) >>> 0) / 4_294_967_296
  }
}

const WRITER_C = '33333333-3333-4333-8333-333333333333'
// Each character is typed at most once in a session and none occurs in the
// stored text, so a lost or a doubled character shows in the room's text.
const TYPED_CHARACTERS = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
const SESSION_DOCUMENT: JSONContent = {
  content: [paragraph('Öß.'), paragraph(), paragraph('Üß.')],
  type: 'doc',
}
const CLIENT_ID_ORDERS = [
  [1_000, 2_000, 3_000],
  [1_000, 3_000, 2_000],
  [2_000, 1_000, 3_000],
  [2_000, 3_000, 1_000],
  [3_000, 1_000, 2_000],
  [3_000, 2_000, 1_000],
] as const
const SESSIONS = 120
const SESSION_STEPS = 50
const SESSION_SETTLE_ROUNDS = 50

describe('writers typing, pressing Enter and clicking into lines while their updates cross in any order', () => {
  // Each session is a seeded script for two or three writers. A step is a
  // keystroke (characters at the caret, Enter at the end of a line, a click
  // to the start or the end of a line) or one hop of the network: a writer's
  // pending changes leave as one update, the room judges a writer's oldest
  // update, or a writer receives the oldest frame the room broadcast to it.
  // Updates and frames keep their order per writer, as on one socket;
  // everything else interleaves freely, so writers type into lines, empty
  // ones included, whose latest changes they have not received.
  //
  // Enter is pressed at the end of a line only. Two writers who split the
  // same text at the same moment each move the rest of the line into a new
  // line, which the editor stack keeps twice; that would hide the doubled
  // character this test looks for.
  async function session(seed: number) {
    const random = seededRandom(seed)
    const pick = <T>(items: readonly T[]): T => items[Math.floor(random() * items.length)]!
    const initial = stored(SESSION_DOCUMENT)
    const room = await openRoom(initial)
    const clientIds = pick(CLIENT_ID_ORDERS)
    const members = [WRITER_A, WRITER_B, WRITER_C].slice(0, seed % 3 === 0 ? 3 : 2).map((userId, index) => ({
      /** Frames the room broadcast that the writer has not received yet. */
      inbox: [] as Uint8Array[],
      /** Updates on their way to the room. */
      underway: [] as Uint8Array[],
      writer: new BoundWriter(userId, clientIds[index]!, initial),
    }))
    type Member = typeof members[number]
    const verdicts: string[] = []
    let typed = ''
    let enters = 0
    const leave = (member: Member): void => {
      const update = member.writer.take()
      if (update) member.underway.push(update)
    }
    const judge = async (member: Member): Promise<boolean> => {
      const update = member.underway.shift()
      if (!update) return false
      const { frames, verdict } = await room.submit(member.writer.userId, update)
      verdicts.push(verdict)
      if (verdict !== 'accepted') {
        member.writer.closed = true
        member.underway.length = 0
        return true
      }
      for (const other of members) {
        if (other !== member) other.inbox.push(...frames)
      }
      return true
    }
    try {
      for (let step = 0; step < SESSION_STEPS; step += 1) {
        const member = pick(members)
        const { writer } = member
        const move = random()
        if (move < 0.15) {
          leave(member)
        } else if (move < 0.3) {
          await judge(member)
        } else if (move < 0.45) {
          const frame = member.inbox.shift()
          if (frame) writer.receive(frame)
        } else if (writer.closed) {
          continue
        } else if (move < 0.75) {
          const text = TYPED_CHARACTERS.slice(typed.length, typed.length + 1 + Math.floor(random() * 3))
          if (text) {
            typed += text
            writer.type(text)
          }
        } else if (move < 0.88) {
          if (writer.caretAtLineEnd()) {
            writer.enter()
            enters += 1
          }
        } else {
          const lines = writer.texts()
          const line = Math.floor(random() * lines.length)
          writer.placeCaret(line, random() < 0.5 ? 0 : lines[line]!.length)
        }
      }
      // Read before anything still underway arrives: the comparison of a
      // replica with the room reads a difference where there is one.
      const behind = members.some((member) => !holdsRoomState(room, member.writer))
      let settled = false
      for (let round = 0; round < SESSION_SETTLE_ROUNDS && !settled; round += 1) {
        settled = true
        for (const member of members) {
          leave(member)
          while (await judge(member)) settled = false
        }
        for (const member of members) {
          for (const frame of member.inbox.splice(0)) {
            member.writer.receive(frame)
            settled = false
          }
        }
      }

      const lines = roomParagraphs(room)
      const roomText = lines.join('\n')
      const problems: string[] = []
      if (!settled) problems.push('did not settle')
      const refused = verdicts.filter((verdict) => verdict !== 'accepted')
      if (refused.length > 0) problems.push(`refused: ${refused.join(', ')}`)
      const lost = [...typed].filter((character) => !roomText.includes(character))
      if (lost.length > 0) problems.push(`lost: ${lost.join('')}`)
      const doubled = [...typed].filter((character) => (
        roomText.indexOf(character) !== roomText.lastIndexOf(character)
      ))
      if (doubled.length > 0) problems.push(`doubled: ${doubled.join('')}`)
      if (!textNodesPerTextblock(room.document).every((count) => count === 1)) {
        problems.push('a line without exactly one text node')
      }
      if (!roomValid(room)) problems.push('the room is not valid')
      if (!(await room.storeMatchesRoom())) problems.push('the store does not replay to the room')
      members.forEach(({ writer }, index) => {
        if (!holdsRoomState(room, writer)) problems.push(`writer ${index} does not hold the room's state`)
        if (JSON.stringify(writer.texts()) !== JSON.stringify(lines)) {
          problems.push(`writer ${index} shows other text than the room`)
        }
        if (writer.writesDuringRemote > 0) {
          problems.push(`writer ${index} wrote while applying a room update`)
        }
      })
      return { accepted: verdicts.length - refused.length, behind, enters, problems, typed: typed.length }
    } finally {
      for (const { writer } of members) writer.destroy()
      await room.close()
    }
  }

  it('accepts every update, keeps every typed character exactly once, and leaves one text node in every line and the room\'s exact state in every replica', async () => {
    const failed: Array<{ problems: string[]; seed: number }> = []
    const totals = { accepted: 0, behind: 0, enters: 0, typed: 0 }
    for (let seed = 1; seed <= SESSIONS; seed += 1) {
      const { accepted, behind, enters, problems, typed } = await session(seed)
      if (problems.length > 0) failed.push({ problems, seed })
      totals.accepted += accepted
      totals.behind += Number(behind)
      totals.enters += enters
      totals.typed += typed
    }

    expect(failed).toEqual([])
    // What the sessions did, so that a script that stops typing, pressing
    // Enter or sending cannot pass unnoticed: accepted updates, sessions in
    // which a replica was behind the room before the last frames arrived,
    // Enter presses, and typed characters. The numbers follow from the seeds
    // and from where the binding leaves each caret.
    expect(totals).toEqual({ accepted: 766, behind: 120, enters: 315, typed: 3_539 })
  }, 120_000)
})
