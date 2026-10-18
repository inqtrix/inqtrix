import { getSchema } from '@tiptap/core'
import { setBlockType } from '@tiptap/pm/commands'
import { EditorState, TextSelection, type Transaction } from '@tiptap/pm/state'
import { initProseMirrorDoc, updateYFragment } from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  INQTRIX_STRUCTURE_COMMAND_META,
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  UnsupportedSuggestionStructureError,
  assertDirectEditKeepsSuggestionRecords,
  collaborationMergeByteCap,
  completeCreatedTextblocks,
  createEditorSchemaExtensions,
  describeLocalEditorTransaction,
  describeLocalEditorUpdate,
  editorCollaborationRoom,
  editorJsonToYDoc,
  getEditorSchemaFingerprint,
  localBatchSealReason,
  mergeLocalBatchFacts,
  parseEditorMarkdown,
  transformToInqtrixSuggestionTransaction,
  type CollaborationWriteStamp,
  type LocalBatchFacts,
  type LocalEditorTransactionFacts,
  type LocalEditorUpdateFacts,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import type { CollaborationSettings, ConnectionContext } from '../src/contracts'
import { DocumentCoordinator } from '../src/documentCoordinator'
import { CollaborationError } from '../src/errors'
import { InstanceLeaseManager } from '../src/instanceLease'
import { SidecarMetrics } from '../src/metrics'
import { FakeCollaborationApi, USER_ID, settings, silentLogger } from './helpers'

// The browser merges the local updates its editor produces while one update
// waits for its acknowledgement, and seals a group before an update that must
// not join it. These tests form groups with the browser's own grouping
// functions from updates shaped like the editor's (y-tiptap writes, the real
// suggestion transform) and send every group through the real acceptance path
// of the collaboration service. A group must be accepted whenever each of its
// members is accepted alone after its predecessors.

const DOCUMENT_ID = 'ed_batching'
const ROOM = editorCollaborationRoom(DOCUMENT_ID, 1)
const LOCAL_EDIT = Object.freeze({ kind: 'local-edit' })
const LOCAL_TRANSPORT = Object.freeze({ kind: 'local-transport' })
const WRITER_CLIENT_ID_BASE = 4_000
const CONTRACT_TIMEOUT_MS = 180_000
const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
const MARK_NAMES = ['bold', 'italic', 'strike', 'code'].filter((name) => schema.marks[name])
const DEFAULT_MERGE_BYTE_CAP = collaborationMergeByteCap(settings().frameLimitBytes)

type WriterAccess = 'edit' | 'suggest'

type LocalUpdate = {
  facts: LocalEditorUpdateFacts
  label: string
  update: Uint8Array
}

// Deterministic RFC 4122 version 4 identifiers, so every run forms the same
// suggestions and the same groups.
function uuidSequence(seed: number): () => string {
  let next = 0
  const prefix = (seed >>> 0).toString(16).padStart(8, '0')
  return () => {
    next += 1
    return `${prefix}-0000-4000-8000-${next.toString(16).padStart(12, '0')}`
  }
}

function lcg(seed: number): () => number {
  let state = seed >>> 0
  return () => {
    state = (Math.imul(state, 1_664_525) + 1_013_904_223) >>> 0
    return state / 2 ** 32
  }
}

function initialState(markdown: string): Uint8Array {
  return Y.encodeStateAsUpdate(editorJsonToYDoc(parseEditorMarkdown(markdown)))
}

// An editor that writes into its Yjs document the way the browser's editor
// does, and records every local update with the facts the browser's grouping
// functions derive from it. Like the browser, it completes the textblocks a
// change created right after the change; the completing update is recorded
// as a member of its own, labelled after the change.
class Writer {
  readonly document = new Y.Doc()
  readonly fragment: Y.XmlFragment
  readonly updates: LocalUpdate[] = []
  mode: CollaborationWriteStamp
  private createdAt = 1_784_112_000
  private patchId: string
  private readonly nextId: () => string
  private produced: Array<Omit<LocalUpdate, 'label'>> = []
  private readonly transactionFacts = new WeakMap<Y.Transaction, LocalEditorTransactionFacts>()

  constructor(initial: Uint8Array, readonly access: WriterAccess, idSeed: number) {
    // One client id per writer: a document that loads structs of its own
    // client id from elsewhere replaces its id with a random one.
    this.document.clientID = WRITER_CLIENT_ID_BASE + idSeed
    Y.applyUpdate(this.document, initial)
    this.fragment = this.document.getXmlFragment(EDITOR_YJS_FRAGMENT)
    this.mode = access === 'suggest' ? 'suggest' : 'edit'
    this.nextId = uuidSequence(idSeed)
    this.patchId = this.nextId()
    this.document.on('afterTransaction', (transaction: Y.Transaction) => {
      if (transaction.origin !== LOCAL_EDIT) return
      this.transactionFacts.set(transaction, describeLocalEditorTransaction(transaction))
      completeCreatedTextblocks(transaction)
    })
    this.document.on(
      'update',
      (update: Uint8Array, origin: unknown, _document: Y.Doc, transaction: Y.Transaction) => {
        if (origin !== LOCAL_EDIT) return
        this.produced.push({
          facts: describeLocalEditorUpdate({
            access: this.access,
            stamp: this.mode,
            transaction: this.transactionFacts.get(transaction) ?? null,
            update,
          }),
          update,
        })
      },
    )
  }

  newPatch(): void {
    this.patchId = this.nextId()
    this.createdAt += 1
  }

  edit(label: string, build: (state: EditorState) => Transaction | null): LocalUpdate | null {
    const initialized = initProseMirrorDoc(this.fragment, schema)
    const state = EditorState.create({ doc: initialized.doc, schema })
    let transaction: Transaction | null
    try {
      transaction = build(state)
    } catch {
      // A position the editor would not offer: it dispatches nothing.
      return null
    }
    if (!transaction) return null
    if (this.mode === 'suggest') {
      try {
        transaction = transformToInqtrixSuggestionTransaction(
          transaction,
          state,
          { authorId: USER_ID, createdAt: this.createdAt, patchId: this.patchId },
          this.nextId,
        )
      } catch {
        // The editor refuses to dispatch a change the transform refuses.
        return null
      }
    } else {
      try {
        assertDirectEditKeepsSuggestionRecords(transaction, USER_ID)
      } catch (error) {
        // In edit mode the editor refuses a direct change of a pending
        // suggestion that the service would refuse.
        if (error instanceof UnsupportedSuggestionStructureError) return null
        throw error
      }
    }
    const target = transaction.doc
    this.produced = []
    this.document.transact(() => {
      updateYFragment(this.document, this.fragment, target, initialized.meta)
    }, LOCAL_EDIT)
    if (this.produced.length === 0) return null
    const [produced, completion] = this.produced
    if (
      !produced
      || this.produced.length > 2
      || (completion !== undefined && !completion.facts.completion)
    ) {
      throw new Error(`${label} produced ${this.produced.length} updates`)
    }
    const local = { ...produced, label }
    this.updates.push(local)
    if (completion) this.updates.push({ ...completion, label: `${label} completion` })
    return local
  }
}

function textblockRanges(state: EditorState): Array<[number, number]> {
  const ranges: Array<[number, number]> = []
  state.doc.descendants((node, position) => {
    if (node.isTextblock) ranges.push([position + 1, position + 1 + node.content.size])
    return !node.isTextblock
  })
  return ranges
}

function ownInsertionRanges(state: EditorState): Array<[number, number]> {
  const ranges: Array<[number, number]> = []
  state.doc.descendants((node, position) => {
    if (!node.isText) return true
    const mark = node.marks.find((candidate) => candidate.type.name === 'insertion')
    if ((mark?.attrs as { authorId?: string } | undefined)?.authorId === USER_ID) {
      ranges.push([position, position + node.nodeSize])
    }
    return false
  })
  return ranges
}

const keys = {
  typeAtEnd: (writer: Writer, text: string, block: number, label = `type ${text}`) =>
    writer.edit(label, (state) => {
      const range = textblockRanges(state)[block]
      return range ? state.tr.insertText(text, range[1]) : null
    }),
  backspaceAtEnd: (writer: Writer, block: number, fromEnd = 0, label = 'backspace') =>
    writer.edit(label, (state) => {
      const range = textblockRanges(state)[block]
      if (!range) return null
      const to = range[1] - fromEnd
      return to - 1 < range[0] ? null : state.tr.delete(to - 1, to)
    }),
  enterAtEnd: (writer: Writer, block: number) => writer.edit('enter', (state) => {
    const range = textblockRanges(state)[block]
    return range ? state.tr.split(range[1]) : null
  }),
  blockType: (
    writer: Writer,
    block: number,
    type: string,
    attrs: Record<string, unknown> = {},
  ) => writer.edit(`to ${type}`, (state) => {
    const range = textblockRanges(state)[block]
    const nodeType = schema.nodes[type]
    if (!range || !nodeType) return null
    const selected = state.apply(state.tr.setSelection(TextSelection.create(state.doc, range[0])))
    let changed: Transaction | null = null
    setBlockType(nodeType, attrs)(selected, (transaction) => {
      changed = transaction
    })
    return changed
  }),
  typeInsideOwnSuggestion: (
    writer: Writer,
    text: string,
    offset = 1,
    label = `type ${text} inside`,
  ) => writer.edit(label, (state) => {
    const range = ownInsertionRanges(state).find(([from, to]) => to - from > offset)
    return range ? state.tr.insertText(text, range[0] + offset) : null
  }),
  deleteInsideOwnSuggestion: (writer: Writer, offset = 1, label = 'delete inside') =>
    writer.edit(label, (state) => {
      const range = ownInsertionRanges(state).find(([from, to]) => to - from > offset + 1)
      return range ? state.tr.delete(range[0] + offset, range[0] + offset + 1) : null
    }),
  addMark: (writer: Writer, name: string, block: number, from: number, to: number) =>
    writer.edit(`+${name}`, (state) => {
      const range = textblockRanges(state)[block]
      const mark = schema.marks[name]
      if (!range || !mark) return null
      const start = Math.min(range[0] + from, range[1])
      const end = Math.min(range[0] + to, range[1])
      return end > start ? state.tr.addMark(start, end, mark.create()) : null
    }),
  removeMark: (writer: Writer, name: string, block: number, from: number, to: number) =>
    writer.edit(`-${name}`, (state) => {
      const range = textblockRanges(state)[block]
      const mark = schema.marks[name]
      if (!range || !mark) return null
      const start = Math.min(range[0] + from, range[1])
      const end = Math.min(range[0] + to, range[1])
      return end > start ? state.tr.removeMark(start, end, mark) : null
    }),
  typeMarked: (writer: Writer, text: string, name: string | null, block: number, offset: number) =>
    writer.edit(`type ${text}${name ? ` ${name}` : ''}`, (state) => {
      const range = textblockRanges(state)[block]
      const mark = name ? schema.marks[name] : undefined
      if (!range || (name && !mark)) return null
      const position = Math.min(range[0] + offset, range[1])
      return state.tr.insert(position, schema.text(text, mark ? [mark.create()] : []))
    }),
  deleteRange: (writer: Writer, block: number, from: number, to: number) =>
    writer.edit('delete range', (state) => {
      const range = textblockRanges(state)[block]
      if (!range) return null
      const start = Math.min(range[0] + from, range[1])
      const end = Math.min(range[0] + to, range[1])
      return end > start ? state.tr.delete(start, end) : null
    }),
}

// The collaboration service's acceptance path for one writer: the room, the
// real coordinator with a fake internal API, and the writer's transport
// document, which emits the update the browser sends for a merged group.
async function acceptanceFixture(
  initial: Uint8Array,
  access: WriterAccess,
  overrides: Partial<CollaborationSettings> = {},
) {
  const api = new FakeCollaborationApi()
  const configured = settings(overrides)
  const metrics = new SidecarMetrics()
  const lease = new InstanceLeaseManager(api, configured, silentLogger, metrics, () => undefined)
  await lease.start()
  const coordinator = new DocumentCoordinator(api, lease, configured, silentLogger, metrics)
  coordinator.initialize(ROOM, 0, { bytes: 0, updates: 0 }, null)
  const room = new Y.Doc()
  Y.applyUpdate(room, initial)
  const transport = new Y.Doc()
  Y.applyUpdate(transport, initial)
  const context: ConnectionContext = {
    access,
    documentId: DOCUMENT_ID,
    expiresAt: Date.now() / 1_000 + 600,
    generation: 1,
    leaseId: 'lease-1',
    policyCursor: 0,
    protocolVersion: configured.protocolVersion,
    schemaHash: await getEditorSchemaFingerprint(),
    schemaVersion: configured.schemaVersion,
    sessionId: 'session-1',
    tenantId: 'tenant-1',
    user: { color: '#123456', id: USER_ID, name: 'Ada' },
  }
  let connections = 0

  // 'accepted', 'noop' or the rejection's close code and reason.
  async function submit(update: Uint8Array): Promise<string> {
    connections += 1
    const connectionId = `connection-${connections}`
    try {
      const result = await coordinator.prepareClientUpdate({
        allowNoop: false,
        connectionId,
        context,
        document: room,
        room: ROOM,
        update,
      })
      if (result !== 'pending') return result
      Y.applyUpdate(room, update)
      coordinator.finishClientUpdate(connectionId, room)
      return 'accepted'
    } catch (error) {
      if (error instanceof CollaborationError) return `${error.code} ${error.reason}`
      throw error
    }
  }

  // The update the browser sends for a group: the members merged and applied
  // to its transport document, which emits only what is new there.
  function transportUpdate(group: readonly LocalUpdate[]): Uint8Array | null {
    let emitted: Uint8Array | null = null
    const capture = (update: Uint8Array, origin: unknown): void => {
      if (origin === LOCAL_TRANSPORT) emitted = Uint8Array.from(update)
    }
    transport.on('update', capture)
    try {
      const merged = group.length === 1
        ? group[0]!.update
        : Y.mergeUpdates(group.map((member) => member.update))
      Y.applyUpdate(transport, merged, LOCAL_TRANSPORT)
    } finally {
      transport.off('update', capture)
    }
    return emitted
  }

  return { close: () => lease.stop(), room, submit, transportUpdate }
}

type GroupRun = {
  failure: string | null
  replayRejections: string[]
  uncleanStates: number
  gapDuplicates: number
}

async function runGroups(
  initial: Uint8Array,
  access: WriterAccess,
  groups: readonly LocalUpdate[][],
  options: {
    checkFormattingClean?: boolean
    overrides?: Partial<CollaborationSettings>
    replayFrom?: number
  } = {},
): Promise<GroupRun> {
  const fixture = await acceptanceFixture(initial, access, options.overrides)
  const run: GroupRun = { failure: null, gapDuplicates: 0, replayRejections: [], uncleanStates: 0 }
  const sent: Array<{ group: number; update: Uint8Array }> = []
  try {
    for (const [index, group] of groups.entries()) {
      const update = fixture.transportUpdate(group)
      // Every local update is new to the transport document, so a group that
      // emits nothing means the fixture sent nothing: a run that submitted
      // nothing must fail instead of reading as accepted.
      if (!update) {
        throw new Error(
          `[${group.map((member) => member.label).join(', ')}] emitted no update to send`,
        )
      }
      sent.push({ group: index, update })
      const result = await fixture.submit(update)
      if (result !== 'accepted') {
        run.failure = `[${group.map((member) => member.label).join(', ')}] ${result}`
        break
      }
      if (options.checkFormattingClean) {
        const redundant = redundantFormatItems(fixture.room)
        if (redundant.fullCleanup > 0) run.uncleanStates += 1
        run.gapDuplicates += redundant.gapDuplicates
      }
    }
    if (run.failure === null && options.replayFrom !== undefined) {
      // A reconnect resends every unacknowledged update byte for byte.
      for (const entry of sent.filter(({ group }) => group >= options.replayFrom!)) {
        const result = await fixture.submit(entry.update)
        if (result !== 'accepted' && result !== 'noop') run.replayRejections.push(result)
      }
    }
  } finally {
    await fixture.close()
  }
  return run
}

// The browser's grouping: one update in flight; while it waits, local updates
// join the open batch unless the grouping rule seals it first. `ackAfter(i)`
// acknowledges the update in flight after local update `i`. A page exit after
// `exitAfter` updates seals the open batch and sends every batch at once. The
// editor emits a completing update in the same task as the update it
// completes, so nothing is sent, acknowledged or flushed between the two.
function formGroups(
  updates: readonly LocalUpdate[],
  ackAfter: (index: number) => boolean,
  options: { exitAfter?: number; mergeByteCap?: number } = {},
): { flushedFrom: number; groups: LocalUpdate[][] } {
  const mergeByteCap = options.mergeByteCap ?? DEFAULT_MERGE_BYTE_CAP
  let exitAfter = Math.min(options.exitAfter ?? updates.length, updates.length)
  while (exitAfter < updates.length && updates[exitAfter]!.facts.completion) exitAfter += 1
  const groups: LocalUpdate[][] = []
  const sealed: LocalUpdate[][] = []
  let open: LocalUpdate[] = []
  let openFacts: LocalBatchFacts | null = null
  let inFlight = false
  const takeOpen = (): LocalUpdate[] => {
    const taken = open
    open = []
    openFacts = null
    return taken
  }
  const pump = (): void => {
    if (inFlight) return
    const next = sealed.shift() ?? (open.length > 0 ? takeOpen() : null)
    if (!next) return
    groups.push(next)
    inFlight = true
  }
  let deferredAck = false
  for (let index = 0; index < exitAfter; index += 1) {
    const update = updates[index]!
    if (localBatchSealReason(openFacts, update.facts, mergeByteCap) !== null) {
      sealed.push(takeOpen())
    }
    open.push(update)
    openFacts = mergeLocalBatchFacts(openFacts, update.facts)
    const acknowledged: boolean = ackAfter(index) || deferredAck
    deferredAck = false
    if (updates[index + 1]?.facts.completion) {
      deferredAck = acknowledged
      continue
    }
    pump()
    if (acknowledged) {
      inFlight = false
      pump()
    }
  }
  if (options.exitAfter === undefined) {
    while (sealed.length > 0 || open.length > 0) {
      inFlight = false
      pump()
    }
    return { flushedFrom: groups.length, groups }
  }
  const flushedFrom = inFlight ? groups.length - 1 : groups.length
  if (open.length > 0) sealed.push(takeOpen())
  groups.push(...sealed.splice(0))
  groups.push(...singleGroups(updates.slice(exitAfter)))
  return { flushedFrom, groups }
}

// Every update in a group of its own, except a completing update: it is
// emitted in the same task as the update it completes and is never sent
// without it (alone it adds nothing the transport does not already hold).
function singleGroups(updates: readonly LocalUpdate[]): LocalUpdate[][] {
  const groups: LocalUpdate[][] = []
  for (const update of updates) {
    const previous = groups.at(-1)
    if (update.facts.completion && previous) previous.push(update)
    else groups.push([update])
  }
  return groups
}

function labels(groups: readonly LocalUpdate[][]): string[][] {
  return groups.map((group) => group.map((member) => member.label))
}

// Formatting items Yjs's cleanup would remove from a state, counted on a
// fresh copy. `fullCleanup` counts what Y.cleanupYTextFormatting removes over
// every text node, the scope of the cleanup that follows a transaction
// inserting a format item. `gapDuplicates` counts format items followed by a
// later one of the same key in the same gap between two content items (or
// after the last one), the scope of the cleanup that follows a deletion next
// to that gap. A state loaded in one transaction never shows either count as
// an update event: nested text nodes created while loading are not reported
// as changed, so counting events says nothing about a loaded state.
function redundantFormatItems(document: Y.Doc): { fullCleanup: number; gapDuplicates: number } {
  const copy = new Y.Doc()
  Y.applyUpdate(copy, Y.encodeStateAsUpdate(document))
  const texts = textNodes(copy)
  let gapDuplicates = 0
  for (const text of texts) gapDuplicates += gapDuplicateFormatItems(text)
  let fullCleanup = 0
  for (const text of texts) fullCleanup += Y.cleanupYTextFormatting(text)
  copy.destroy()
  return { fullCleanup, gapDuplicates }
}

function textNodes(document: Y.Doc): Y.XmlText[] {
  const texts: Y.XmlText[] = []
  const visit = (node: unknown): void => {
    if (node instanceof Y.XmlText) {
      texts.push(node)
      return
    }
    if (node instanceof Y.XmlElement || node instanceof Y.XmlFragment) {
      node.toArray().forEach(visit)
    }
  }
  for (const name of document.share.keys()) visit(document.getXmlFragment(name))
  return texts
}

function gapDuplicateFormatItems(text: Y.XmlText): number {
  let duplicates = 0
  let gapKeys: string[] = []
  const closeGap = (): void => {
    const later = new Set<string>()
    for (let index = gapKeys.length - 1; index >= 0; index -= 1) {
      const key = gapKeys[index]!
      if (later.has(key)) duplicates += 1
      else later.add(key)
    }
    gapKeys = []
  }
  for (let item = text._start; item !== null; item = item.right) {
    if (item.deleted) continue
    if (item.content instanceof Y.ContentFormat) gapKeys.push(item.content.key)
    else if (item.countable) closeGap()
  }
  closeGap()
  return duplicates
}

type SessionKind = 'direct-format' | 'edit-modes' | 'mixed-format' | 'suggest'

// A seeded editing session: typing, corrections, deletions inside the own
// suggestion, Enter, mode switches, and formatting, depending on the kind.
function buildSession(kind: SessionKind, seed: number, steps: number): {
  access: WriterAccess
  initial: Uint8Array
  writer: Writer
} {
  const access: WriterAccess = kind === 'suggest' ? 'suggest' : 'edit'
  const random = lcg(seed * 2_654_435_761 + kind.length * 97)
  const initial = initialState(
    'Alpha beta gamma delta\n\nEpsilon zeta eta theta\n\nIota kappa lambda mu',
  )
  const writer = new Writer(initial, access, seed)
  let block = 0
  for (let step = 0; step < steps; step += 1) {
    const roll = random()
    if (kind === 'suggest' || kind === 'edit-modes') {
      if (kind === 'edit-modes' && roll < 0.06) {
        writer.mode = writer.mode === 'edit' ? 'suggest' : 'edit'
        writer.newPatch()
      } else if (roll < 0.12) {
        block = Math.floor(random() * 3)
        writer.newPatch()
      } else if (roll < 0.62) {
        keys.typeAtEnd(writer, 'abcdefghij'[Math.floor(random() * 10)]!, block)
      } else if (roll < 0.77) {
        keys.backspaceAtEnd(writer, block)
      } else if (roll < 0.84) {
        keys.backspaceAtEnd(writer, block, 1 + Math.floor(random() * 3))
      } else if (roll < 0.92) {
        keys.typeInsideOwnSuggestion(writer, 'Q')
      } else if (roll < 0.96) {
        keys.deleteInsideOwnSuggestion(writer)
      } else if (writer.mode === 'edit') {
        keys.enterAtEnd(writer, block)
      }
      continue
    }
    const mark = MARK_NAMES[Math.floor(random() * MARK_NAMES.length)]!
    const from = Math.floor(random() * 24)
    const to = from + 1 + Math.floor(random() * 8)
    if (kind === 'mixed-format' && roll < 0.05) {
      writer.mode = writer.mode === 'edit' ? 'suggest' : 'edit'
      writer.newPatch()
    } else if (roll < 0.08) {
      block = Math.floor(random() * 3)
      writer.newPatch()
    } else if (kind === 'mixed-format' && roll < 0.14) {
      keys.typeInsideOwnSuggestion(writer, 'Q')
    } else if (roll < 0.40) {
      const text = 'abcdefgh'[Math.floor(random() * 8)]!
      keys.typeMarked(writer, text, random() < 0.5 ? mark : null, block, from)
    } else if (roll < 0.58) {
      keys.addMark(writer, mark, block, from, to)
    } else if (roll < 0.72) {
      keys.removeMark(writer, mark, block, from, to)
    } else if (roll < 0.86) {
      keys.deleteRange(writer, block, from, from + 1 + Math.floor(random() * 3))
    } else if (roll < 0.93) {
      keys.backspaceAtEnd(writer, block)
    } else if (writer.mode === 'edit') {
      keys.enterAtEnd(writer, block)
    }
  }
  return { access, initial, writer }
}

describe('local batching contract: the formatting-clean predicate', () => {
  // Seeded states with a known number of redundant format items. The
  // predicate must see the leading-gap duplicate that makes a later
  // formatted keystroke produce a second update event on the service's
  // clone, and must report nothing for a single writer's formatting.
  function paragraphText(document: Y.Doc): Y.XmlText {
    const paragraph = document.getXmlFragment('content').get(0)
    if (!(paragraph instanceof Y.XmlElement)) throw new Error('fixture has no paragraph')
    const text = paragraph.get(0)
    if (!(text instanceof Y.XmlText)) throw new Error('fixture has no text')
    return text
  }

  function loadedAtOnce(...edits: Array<(text: Y.XmlText) => void>): Y.Doc {
    const base = new Y.Doc()
    base.clientID = 1
    const paragraph = new Y.XmlElement('paragraph')
    base.getXmlFragment('content').insert(0, [paragraph])
    const text = new Y.XmlText()
    paragraph.insert(0, [text])
    text.insert(0, 'abcdef')
    const baseState = Y.encodeStateAsUpdate(base)
    const parts = [baseState]
    edits.forEach((edit, index) => {
      const writer = new Y.Doc()
      writer.clientID = 2 + index
      Y.applyUpdate(writer, baseState)
      edit(paragraphText(writer))
      parts.push(Y.encodeStateAsUpdate(writer))
    })
    const loaded = new Y.Doc()
    Y.applyUpdate(loaded, Y.mergeUpdates(parts))
    return loaded
  }

  // Update events a formatted keystroke produces when the service applies it
  // to a clone of `state`, as prepareClientUpdate does.
  function cloneUpdateEvents(state: Y.Doc): number {
    const writer = new Y.Doc()
    writer.clientID = 9
    Y.applyUpdate(writer, Y.encodeStateAsUpdate(state))
    let keystroke: Uint8Array | null = null
    writer.on('update', (update: Uint8Array) => {
      keystroke = update
    })
    paragraphText(writer).insert(6, 'g', { italic: true })
    if (!keystroke) throw new Error('the keystroke produced no update')
    const clone = new Y.Doc()
    Y.applyUpdate(clone, Y.encodeStateAsUpdate(state))
    let events = 0
    clone.on('update', () => {
      events += 1
    })
    Y.applyUpdate(clone, keystroke)
    return events
  }

  it('reports no redundant formatting for one writer and the leading-gap duplicate of two concurrent bolds', () => {
    const singleBold = loadedAtOnce((text) => text.format(0, 4, { bold: true }))
    expect(redundantFormatItems(singleBold)).toEqual({ fullCleanup: 0, gapDuplicates: 0 })
    expect(cloneUpdateEvents(singleBold)).toBe(1)

    const concurrentBolds = loadedAtOnce(
      (text) => text.format(0, 4, { bold: true }),
      (text) => text.format(0, 4, { bold: true }),
    )
    expect(redundantFormatItems(concurrentBolds)).toEqual({ fullCleanup: 1, gapDuplicates: 2 })
    // Yjs's cleanup of that duplicate follows the keystroke as a second
    // update event on the clone.
    expect(cloneUpdateEvents(concurrentBolds)).toBe(2)
  })
})

describe('local batching contract: fixed scripts', () => {
  it('a suggestion group never holds both deleting and inserting updates', async () => {
    // Own suggestion typed while the first update waits, then corrected.
    const initial = initialState('Hello')
    const writer = new Writer(initial, 'suggest', 101)
    keys.typeAtEnd(writer, 'a', 0, 'a')
    keys.typeAtEnd(writer, 'b', 0, 'b')
    keys.typeAtEnd(writer, 'c', 0, 'c')
    keys.backspaceAtEnd(writer, 0, 0, 'delete c')
    keys.backspaceAtEnd(writer, 0, 0, 'delete b')
    keys.typeAtEnd(writer, 'd', 0, 'd')
    const { groups } = formGroups(writer.updates, () => false)
    expect((await runGroups(initial, 'suggest', groups)).failure).toBeNull()
    expect(labels(groups)).toEqual([['a'], ['b', 'c'], ['delete c', 'delete b'], ['d']])
  }, CONTRACT_TIMEOUT_MS)

  it('deleting and retyping a character inside the own suggestion travel apart', async () => {
    // Together they would leave every suggestion unchanged, which the service
    // refuses.
    const initial = initialState('Hello')
    const writer = new Writer(initial, 'suggest', 102)
    for (const text of 'abcd') keys.typeAtEnd(writer, text, 0, text)
    keys.deleteInsideOwnSuggestion(writer, 1, 'delete b')
    keys.typeInsideOwnSuggestion(writer, 'b', 1, 'retype b')
    const { groups } = formGroups(writer.updates, () => false)
    expect((await runGroups(initial, 'suggest', groups)).failure).toBeNull()
    expect(labels(groups)).toEqual([['a'], ['b', 'c', 'd'], ['delete b'], ['retype b']])
  }, CONTRACT_TIMEOUT_MS)

  it('a change of write mode never shares a group with the changes before it', async () => {
    const initial = initialState('Hello\n\nWorld')
    const writer = new Writer(initial, 'edit', 103)
    writer.mode = 'suggest'
    keys.typeAtEnd(writer, 'x', 0, 'x')
    keys.typeAtEnd(writer, 'y', 0, 'y')
    writer.mode = 'edit'
    keys.typeAtEnd(writer, 'z', 1, 'z')
    const { groups } = formGroups(writer.updates, () => false)
    expect((await runGroups(initial, 'edit', groups)).failure).toBeNull()
    expect(labels(groups)).toEqual([['x'], ['y'], ['z']])
  }, CONTRACT_TIMEOUT_MS)

  it('an edit-mode change in a paragraph that carries a suggestion travels alone', async () => {
    const base = initialState('Hello\n\nWorld')
    const suggester = new Writer(base, 'suggest', 104)
    keys.typeAtEnd(suggester, ' there', 0, 'suggestion')
    const initial = Y.encodeStateAsUpdate(suggester.document)
    const writer = new Writer(initial, 'edit', 105)
    keys.typeAtEnd(writer, 'a', 1, 'a')
    keys.typeInsideOwnSuggestion(writer, 'Q', 2, 'Q')
    keys.typeAtEnd(writer, 'r', 1, 'r')
    keys.typeAtEnd(writer, 's', 1, 's')
    const { groups } = formGroups(writer.updates, () => false)
    expect((await runGroups(initial, 'edit', groups)).failure).toBeNull()
    expect(labels(groups)).toEqual([['a'], ['Q'], ['r', 's']])
  }, CONTRACT_TIMEOUT_MS)

  it('a slash command reaches the service before the structure suggestion that replaces it', async () => {
    const initial = initialState('## Suggest target\n\nWorld')
    const writer = new Writer(initial, 'suggest', 106)
    keys.typeAtEnd(writer, 'x', 1, 'x')
    writer.newPatch()
    expect(writer.edit('slash', (state) => state.tr.insertText('/', 1))).not.toBeNull()
    writer.newPatch()
    const paragraph = schema.nodes.paragraph
    if (!paragraph) throw new Error('schema has no paragraph')
    writer.edit('structure', (state) => state.tr
      .delete(1, 2)
      .setNodeMarkup(0, paragraph, { textAlign: null })
      .setMeta(INQTRIX_STRUCTURE_COMMAND_META, {
        action: 'paragraph',
        commandRange: { from: 1, to: 2 },
      }))
    const { groups } = formGroups(writer.updates, () => false)
    expect((await runGroups(initial, 'suggest', groups)).failure).toBeNull()
    expect(labels(groups)).toEqual([['x'], ['slash'], ['structure']])
  }, CONTRACT_TIMEOUT_MS)

  it('direct formatting and typing merge into groups the service accepts', async () => {
    const initial = initialState('Hello brave new world')
    const writer = new Writer(initial, 'edit', 107)
    keys.typeAtEnd(writer, '!', 0, 'first')
    keys.addMark(writer, 'bold', 0, 0, 5)
    keys.typeMarked(writer, 'X', 'bold', 0, 2)
    keys.addMark(writer, 'italic', 0, 3, 11)
    keys.deleteRange(writer, 0, 4, 8)
    keys.removeMark(writer, 'bold', 0, 1, 3)
    keys.typeMarked(writer, 'Y', null, 0, 6)
    const { groups } = formGroups(writer.updates, () => false)
    expect(groups).toHaveLength(2)
    expect((await runGroups(initial, 'edit', groups)).failure).toBeNull()
  }, CONTRACT_TIMEOUT_MS)

  it('the update completing a created textblock travels in the group of the update that created it', async () => {
    // Enter at the end of a block, and an empty line turned into a heading,
    // create a textblock without content; the browser completes it at once.
    // Turning the empty line into a heading deletes a block, so that change
    // travels alone, with its completion. Every group is accepted.
    const initial = initialState('Hello\n\n\n\nWorld')
    const writer = new Writer(initial, 'edit', 109)
    keys.typeAtEnd(writer, 'a', 0, 'a')
    keys.enterAtEnd(writer, 0)
    // Into the line Enter created, then the stored empty line below it.
    keys.typeAtEnd(writer, 'b', 1, 'b')
    keys.blockType(writer, 2, 'heading', { level: 2 })
    keys.typeAtEnd(writer, 'c', 0, 'c')
    expect(writer.updates.map((update) => [update.label, update.facts.class, update.facts.completion]))
      .toEqual([
        ['a', 'direct', false],
        ['enter', 'direct', false],
        ['enter completion', 'direct', true],
        ['b', 'direct', false],
        ['to heading', 'isolated', false],
        ['to heading completion', 'direct', true],
        ['c', 'direct', false],
      ])
    const { groups } = formGroups(writer.updates, () => false)
    expect(labels(groups)).toEqual([
      ['a'],
      ['enter', 'enter completion', 'b'],
      ['to heading', 'to heading completion'],
      ['c'],
    ])
    expect((await runGroups(initial, 'edit', groups)).failure).toBeNull()
    // The same with an acknowledgement after every update: the completion
    // still leaves with the change it completes.
    const acknowledged = formGroups(writer.updates, () => true).groups
    expect(labels(acknowledged)).toEqual([
      ['a'],
      ['enter', 'enter completion'],
      ['b'],
      ['to heading', 'to heading completion'],
      ['c'],
    ])
    expect((await runGroups(initial, 'edit', acknowledged)).failure).toBeNull()
  }, CONTRACT_TIMEOUT_MS)

  it('an empty line with a structure suggestion turned into a code block travels alone with its completion', async () => {
    const base = initialState('Hello\n\n\n\nWorld')
    const suggester = new Writer(base, 'suggest', 110)
    expect(suggester.edit('slash', (state) => (
      state.tr.insertText('/', textblockRanges(state)[1]![0])
    ))).not.toBeNull()
    suggester.newPatch()
    const heading = schema.nodes.heading
    if (!heading) throw new Error('schema has no heading')
    expect(suggester.edit('structure', (state) => {
      const [from] = textblockRanges(state)[1]!
      return state.tr
        .delete(from, from + 1)
        .setNodeMarkup(from - 1, heading, { level: 2 })
        .setMeta(INQTRIX_STRUCTURE_COMMAND_META, {
          action: 'heading2',
          commandRange: { from, to: from + 1 },
        })
    })).not.toBeNull()
    const initial = Y.encodeStateAsUpdate(suggester.document)
    const proposed = suggester.document.getXmlFragment(EDITOR_YJS_FRAGMENT).get(1) as Y.XmlElement
    expect(proposed.getAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR)).not.toBeNull()

    const writer = new Writer(initial, 'edit', 111)
    keys.typeAtEnd(writer, 'a', 0, 'a')
    keys.typeAtEnd(writer, 'b', 0, 'b')
    keys.blockType(writer, 1, 'codeBlock')
    keys.typeAtEnd(writer, 'c', 2, 'c')
    expect(writer.updates.map((update) => [update.label, update.facts.class, update.facts.completion]))
      .toEqual([
        ['a', 'direct', false],
        ['b', 'direct', false],
        ['to codeBlock', 'isolated', false],
        ['to codeBlock completion', 'direct', true],
        ['c', 'direct', false],
      ])
    const { groups } = formGroups(writer.updates, () => false)
    expect(labels(groups)).toEqual([
      ['a'],
      ['b'],
      ['to codeBlock', 'to codeBlock completion'],
      ['c'],
    ])
    expect((await runGroups(initial, 'edit', groups)).failure).toBeNull()
  }, CONTRACT_TIMEOUT_MS)

  it('merging never produces a frame above the published frame limit', async () => {
    // A 64 KiB frame limit: two pastes of 40 KiB each fit alone, together
    // they would not.
    const frameLimitBytes = 64 * 1024
    const initial = initialState('Hello\n\nWorld')
    const writer = new Writer(initial, 'edit', 108)
    keys.typeAtEnd(writer, 'x', 0, 'x')
    keys.typeAtEnd(writer, 'lorem ipsum '.repeat(3_414), 1, 'first paste')
    keys.typeAtEnd(writer, 'dolor sit amet '.repeat(2_731), 1, 'second paste')
    for (const update of writer.updates.slice(1)) {
      expect(update.update.byteLength).toBeGreaterThan(40 * 1024)
      expect(update.update.byteLength).toBeLessThan(frameLimitBytes)
    }
    const { groups } = formGroups(writer.updates, () => false, {
      mergeByteCap: collaborationMergeByteCap(frameLimitBytes),
    })
    expect((await runGroups(initial, 'edit', groups, {
      overrides: { frameLimitBytes },
    })).failure).toBeNull()
    expect(labels(groups)).toEqual([['x'], ['first paste'], ['second paste']])
  }, CONTRACT_TIMEOUT_MS)
})

describe('local batching contract: seeded sessions', () => {
  // Seeds whose session contains a single update the service rejects even
  // when it is sent alone are excluded. The counts are fixed so that a change
  // in the model or in the service's acceptance is visible here, and so that
  // a model that produces fewer updates, or groups that no longer merge,
  // cannot pass unnoticed: per family, the excluded sessions, then the local
  // updates and the groups of more than one update in the sessions that
  // remain. The local updates include the update completing each textblock an
  // edit-mode Enter created, and a change travelling with its completion
  // counts as a group of more than one.
  //
  // The writer refuses locally what the editor refuses (the suggest-mode
  // transform, the edit-mode guard), so every excluded session is a change
  // the editor sends and the service refuses. The edit-mode guard lets a
  // change delete characters of a pending suggestion, and remove it
  // completely, so an edit-mode writer here also shrinks and removes the
  // writer's own suggestions. Every excluded session types or deletes, in
  // edit mode, a character equal to one of the writer's own pending
  // suggestion beside it: the plain Yjs binding this model writes through
  // then gives the typed character the identity of the suggestion's own one,
  // or keeps a deleted character's identity for the suggestion's, which the
  // service refuses as a suggestion that moved onto another character (the
  // editor writes through the mark-aware text update, which keeps every
  // identity). That limitation of the binding is counted here, not hidden;
  // any other exclusion is a defect.
  const families: Array<[SessionKind, number, number, number, number]> = [
    ['suggest', 100, 0, 3_992, 800],
    ['edit-modes', 80, 1, 3_559, 407],
    ['direct-format', 30, 0, 1_406, 33],
    ['mixed-format', 60, 4, 1_996, 186],
  ]

  it.each(families)(
    'merged %s updates formed by the batching rule are accepted, also across page exits and replays',
    async (kind, seeds, expectedExcluded, expectedUpdates, expectedMergedGroups) => {
      let excluded = 0
      let gapDuplicates = 0
      let localUpdates = 0
      let mergedGroups = 0
      const failures: string[] = []
      for (let seed = 1; seed <= seeds; seed += 1) {
        const { access, initial, writer } = buildSession(kind, seed, 60)
        const updates = writer.updates
        const singles = await runGroups(initial, access, singleGroups(updates), {
          checkFormattingClean: true,
        })
        if (singles.failure !== null) {
          excluded += 1
          continue
        }
        gapDuplicates += singles.gapDuplicates
        localUpdates += updates.length
        if (singles.uncleanStates > 0) {
          failures.push(
            `seed ${seed}: ${singles.uncleanStates} states the full cleanup would change`,
          )
        }
        const maxMergeGroups = formGroups(updates, () => false).groups
        mergedGroups += maxMergeGroups.filter((group) => group.length > 1).length
        const maxMerge = await runGroups(initial, access, maxMergeGroups)
        if (maxMerge.failure) failures.push(`seed ${seed} max-merge: ${maxMerge.failure}`)
        const pick = lcg(seed * 31_337 + 7)
        const exits = [
          1 + Math.floor(pick() * (updates.length - 1)),
          1 + Math.floor(pick() * (updates.length - 1)),
          1 + Math.floor(pick() * (updates.length - 1)),
          updates.length,
        ]
        for (const exitAfter of exits) {
          const acknowledge = lcg(seed * 104_729 + exitAfter)
          const { flushedFrom, groups } = formGroups(updates, () => acknowledge() < 0.35, {
            exitAfter,
          })
          const run = await runGroups(initial, access, groups, { replayFrom: flushedFrom })
          if (run.failure) failures.push(`seed ${seed} exit ${exitAfter}: ${run.failure}`)
          if (run.replayRejections.length > 0) {
            failures.push(
              `seed ${seed} exit ${exitAfter} replay: ${run.replayRejections.join('; ')}`,
            )
          }
        }
      }
      // Reported, not asserted: the deleting writer's own local deletion next
      // to such a gap removes these items in the same transaction.
      console.info(
        `${kind}: ${seeds - excluded} sessions, ${localUpdates} local updates, `
          + `${mergedGroups} merged groups; ${gapDuplicates} same-key format items `
          + 'in gaps after accepted single updates',
      )
      expect(failures).toEqual([])
      expect({ excluded, localUpdates, mergedGroups }).toEqual({
        excluded: expectedExcluded,
        localUpdates: expectedUpdates,
        mergedGroups: expectedMergedGroups,
      })
    },
    CONTRACT_TIMEOUT_MS,
  )
})
