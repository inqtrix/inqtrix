import { getSchema, type JSONContent } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { EditorState, type Transaction } from '@tiptap/pm/state'
import { initProseMirrorDoc, updateYFragment } from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  createEditorSchemaExtensions,
  editorCollaborationRoom,
  editorJsonToYDoc,
  editorYDocToJson,
  getEditorSchemaFingerprint,
  parseEditorMarkdown,
  transformToInqtrixSuggestionTransaction,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import type {
  ConnectionContext,
  DurableUpdateLookup,
  LoadedDocumentState,
  PersistUpdateInput,
  PersistUpdateResult,
  SidecarLogger,
} from '../src/contracts'
import { resolvePatchDecision } from '../src/decisionResolver'
import { DocumentCoordinator } from '../src/documentCoordinator'
import {
  hashBytes,
  reconstructValidatedDocument,
  validateDocument,
} from '../src/documentState'
import { ApiRequestError, CollaborationError } from '../src/errors'
import { InstanceLeaseManager } from '../src/instanceLease'
import { normalizeEditMerge } from '../src/mergeNormalization'
import { SidecarMetrics } from '../src/metrics'
import { collectSuggestionRecords } from '../src/suggestPolicy'
import { FakeCollaborationApi, documentState, settings, silentLogger } from './helpers'

// An edit lease's update meets concurrent changes in the room. Where the
// merge leaves a state the checks refuse, the service normalizes it in the
// writer's own row instead of ending the writer's session, within what edit
// rights allow, and never in another person's name. Every case goes through
// the real acceptance path, DocumentCoordinator.prepareClientUpdate, with
// client-shaped updates written by the editor binding.

const WRITER = '11111111-1111-4111-8111-111111111111'
const REVIEWER = '44444444-4444-4444-8444-444444444444'
const THIRD = '55555555-5555-4555-8555-555555555555'
const DECIDER = '22222222-2222-4222-8222-222222222222'
const DOCUMENT_ID = 'ed_normalization'
const ROOM = editorCollaborationRoom(DOCUMENT_ID, 1)
const CREATED_AT = 1_784_112_000
const POLICY_REFUSAL = '4403 suggestion_policy_violation'
const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

let generated = 0
function uuid(): string {
  generated += 1
  return `abcdabcd-0000-4000-8000-${generated.toString(16).padStart(12, '0')}`
}

function markdown(value: string): Uint8Array {
  return Y.encodeStateAsUpdate(editorJsonToYDoc(parseEditorMarkdown(value)))
}

function documentFrom(state: Uint8Array, clientId: number): Y.Doc {
  const document = new Y.Doc()
  document.clientID = clientId
  Y.applyUpdate(document, state)
  return document
}

/** The updates a client emits for one change, as its provider sends them:
 * the transaction's own items and deletions, not the document's history. */
function emitted(document: Y.Doc, change: () => void): Uint8Array {
  const updates: Uint8Array[] = []
  const capture = (update: Uint8Array): void => {
    updates.push(update)
  }
  document.on('update', capture)
  try {
    document.transact(change)
  } finally {
    document.off('update', capture)
  }
  return Y.mergeUpdates(updates)
}

/** A change the editor writes through y-tiptap, as a client update. */
function editorUpdate(
  base: Y.Doc,
  clientId: number,
  build: (state: EditorState) => Transaction,
): Uint8Array {
  const document = documentFrom(Y.encodeStateAsUpdate(base), clientId)
  const fragment = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, schema)
  const target = build(EditorState.create({ doc: initialized.doc, schema })).doc
  const update = emitted(document, () => updateYFragment(document, fragment, target, initialized.meta))
  document.destroy()
  return update
}

/** A change the editor writes through y-tiptap on a client that wrote
 * before and keeps its id: its new characters continue its own clocks. The
 * id is set after the state is loaded, since Yjs gives a document a new id
 * when a remote update carries items under its current one. */
function continuedUpdate(
  base: Y.Doc,
  clientId: number,
  build: (state: EditorState) => Transaction,
): Uint8Array {
  const document = new Y.Doc()
  Y.applyUpdate(document, Y.encodeStateAsUpdate(base))
  document.clientID = clientId
  const fragment = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, schema)
  const target = build(EditorState.create({ doc: initialized.doc, schema })).doc
  const update = emitted(document, () => updateYFragment(document, fragment, target, initialized.meta))
  document.destroy()
  return update
}

/** The same change made in suggest mode by the real suggestion transform. */
function suggestionUpdate(
  base: Y.Doc,
  clientId: number,
  authorId: string,
  build: (state: EditorState) => Transaction,
): Uint8Array {
  return editorUpdate(base, clientId, (state) => transformToInqtrixSuggestionTransaction(
    build(state),
    state,
    { authorId, createdAt: CREATED_AT, patchId: uuid() },
    uuid,
  ))
}

/** A change written directly into the shared types, as a modified client could. */
function rawUpdate(base: Y.Doc, clientId: number, mutate: (fragment: Y.XmlFragment) => void): Uint8Array {
  const document = documentFrom(Y.encodeStateAsUpdate(base), clientId)
  const update = emitted(document, () => mutate(document.getXmlFragment(EDITOR_YJS_FRAGMENT)))
  document.destroy()
  return update
}

/** Where a needle stands in the editor document. */
function range(document: ProseMirrorNode, needle: string): { from: number; to: number } {
  let found: { from: number; to: number } | null = null
  document.descendants((node, position) => {
    if (found || !node.isText) return !found
    const at = node.text!.indexOf(needle)
    if (at >= 0) found = { from: position + at, to: position + at + needle.length }
    return false
  })
  if (!found) throw new Error(`the document has no ${needle}`)
  return found
}

const deleteText = (needle: string) => (state: EditorState): Transaction => {
  const { from, to } = range(state.doc, needle)
  return state.tr.delete(from, to)
}

const addMark = (name: string, from: string, to = from) => (state: EditorState): Transaction => {
  const mark = schema.marks[name]
  if (!mark) throw new Error(`schema has no ${name} mark`)
  return state.tr.addMark(range(state.doc, from).from, range(state.doc, to).to, mark.create())
}

/** Every block of a document as text, each run followed by its marks. */
function blocks(document: JSONContent): string[] {
  const runs = (node: JSONContent): string => (node.content ?? []).map((child) => {
    if (child.type !== 'text') return `[${child.type}${child.content ? `:${runs(child)}` : ''}]`
    const marks = (child.marks ?? []).map((mark) => mark.type).sort()
    return marks.length > 0 ? `${child.text}{${marks.join(',')}}` : child.text ?? ''
  }).join('')
  return (document.content ?? []).map((node) => `${node.type}:${runs(node)}`)
}

type Logged = { event: string; fields: Record<string, unknown>; level: string }

/**
 * A store that keeps rows the way the real one does: one row per hash and
 * document generation, sequences without gaps. A persist can be held in
 * flight: the request fails for the sender and commits later, or never.
 */
class LedgerApi extends FakeCollaborationApi {
  readonly held: PersistUpdateInput[] = []
  readonly rows: Array<{ hash: string; sequence: number; update: Uint8Array }> = []
  holdNextPersist = false

  constructor(private readonly initial: LoadedDocumentState) {
    super()
  }

  override async persistUpdate(input: PersistUpdateInput): Promise<PersistUpdateResult> {
    this.persisted.push(input)
    if (this.holdNextPersist) {
      this.holdNextPersist = false
      this.held.push(input)
      throw new ApiRequestError(503, 'internal_api_timeout')
    }
    return this.commit(input)
  }

  commit(input: PersistUpdateInput): PersistUpdateResult {
    const current = this.rows.length
    const existing = this.rows.find((row) => row.hash === input.hash)
    if (existing) return { duplicate: true, persistedSequence: current, sequence: existing.sequence }
    this.rows.push({ hash: input.hash, sequence: current + 1, update: input.update })
    return { duplicate: false, persistedSequence: current + 1, sequence: current + 1 }
  }

  /** Lets the held requests reach the store now. */
  commitHeld(): PersistUpdateResult[] {
    return this.held.splice(0).map((input) => this.commit(input))
  }

  override async lookupUpdates(input: { hashes: string[] }): Promise<DurableUpdateLookup[]> {
    return this.rows
      .filter((row) => input.hashes.includes(row.hash))
      .map((row) => ({ hash: row.hash, sequence: row.sequence }))
  }

  override async loadDocumentState(): Promise<LoadedDocumentState> {
    return {
      ...this.initial,
      persistedSequence: this.rows.length,
      updates: this.rows.map((row) => ({ ...row })),
    }
  }
}

type Room = {
  api: LedgerApi
  close: () => Promise<void>
  coordinator: DocumentCoordinator
  decide: (decision: 'accept' | 'reject', patchIds: string[]) => Promise<string>
  document: Y.Doc
  /** Room frames broadcast while the last submit was handled. */
  frames: Uint8Array[]
  logs: Logged[]
  metrics: SidecarMetrics
  /** Rebuilds the room from the store, as the service does after an
   * unknown persistence outcome. */
  reload: () => Promise<void>
  /** Whether replaying every stored row gives exactly the room's state. */
  replayMatchesRoom: () => Promise<boolean>
  submit: (access: 'edit' | 'suggest', user: string, update: Uint8Array) => Promise<string>
}

async function openRoom(initial: Uint8Array): Promise<Room> {
  const configured = settings()
  const base = new Y.Doc()
  Y.applyUpdate(base, initial)
  const api = new LedgerApi(await documentState(DOCUMENT_ID, base))
  base.destroy()
  const metrics = new SidecarMetrics()
  const logs: Logged[] = []
  const record = (level: string) => (event: string, fields: Record<string, unknown> = {}) => {
    logs.push({ event, fields, level })
  }
  const logger: SidecarLogger = {
    debug: record('debug'),
    error: record('error'),
    info: record('info'),
    warn: record('warn'),
  }
  const lease = new InstanceLeaseManager(api, configured, silentLogger, metrics, () => undefined)
  await lease.start()
  const coordinator = new DocumentCoordinator(api, lease, configured, logger, metrics)
  coordinator.initialize(ROOM, 0, { bytes: 0, updates: 0 })
  const schemaHash = await getEditorSchemaFingerprint()
  let connections = 0
  let commands = 0
  const frames: Uint8Array[] = []
  const room: Room = {
    api,
    close: async () => {
      room.document.destroy()
      await lease.stop()
    },
    coordinator,
    decide: async (decision, patchIds) => {
      commands += 1
      try {
        await coordinator.applyServerMutation({
          actorKind: 'human',
          actorUserId: DECIDER,
          changeKind: 'decision',
          commandId: `abcdabcd-1111-4000-8000-${commands.toString(16).padStart(12, '0')}`,
          commandPayloadHash: hashBytes(new TextEncoder().encode(`${decision}:${patchIds.join(',')}`)),
          decision,
          document: room.document,
          documentId: DOCUMENT_ID,
          expectedSequence: coordinator.getPersistedSequence(ROOM),
          generation: 1,
          mutate: (clone) => resolvePatchDecision(clone, { decision, patchIds }),
          requestedPatchIds: patchIds,
          room: ROOM,
          sequenceRule: 'not_ahead',
        })
        return 'decided'
      } catch (error) {
        if (error instanceof CollaborationError) return `${error.code} ${error.reason}`
        throw error
      }
    },
    document: documentFrom(initial, 1),
    frames,
    logs,
    metrics,
    reload: async () => {
      coordinator.markUnloaded(ROOM)
      const loaded = await api.loadDocumentState()
      const validated = await reconstructValidatedDocument(loaded, {
        documentId: DOCUMENT_ID,
        generation: 1,
        schemaVersion: configured.schemaVersion,
      }, configured.documentLimitBytes)
      room.document.destroy()
      room.document = validated.document
      coordinator.initialize(ROOM, loaded.persistedSequence, {
        bytes: 0,
        updates: loaded.updates.length,
      }, validated)
    },
    replayMatchesRoom: async () => {
      const replayed = await reconstructValidatedDocument(await api.loadDocumentState(), {
        documentId: DOCUMENT_ID,
        generation: 1,
        schemaVersion: configured.schemaVersion,
      }, configured.documentLimitBytes)
      try {
        return hashBytes(Y.encodeStateAsUpdate(replayed.document))
          === hashBytes(Y.encodeStateAsUpdate(room.document))
      } finally {
        replayed.document.destroy()
      }
    },
    submit: async (access, user, update) => {
      connections += 1
      const connectionId = `connection-${connections}`
      const context: ConnectionContext = {
        access,
        documentId: DOCUMENT_ID,
        expiresAt: Date.now() / 1_000 + 600,
        generation: 1,
        leaseId: 'lease-1',
        policyCursor: 0,
        protocolVersion: configured.protocolVersion,
        schemaHash,
        schemaVersion: configured.schemaVersion,
        sessionId: connectionId,
        tenantId: 'tenant-1',
        user: { color: '#123456', id: user, name: 'Collaborator' },
      }
      frames.length = 0
      const observe = (frame: Uint8Array): void => {
        frames.push(frame)
      }
      room.document.on('update', observe)
      try {
        const result = await coordinator.prepareClientUpdate({
          allowNoop: false,
          connectionId,
          context,
          document: room.document,
          room: ROOM,
          update,
        })
        if (result !== 'pending') return result
        // The transport applies the client's own bytes after the service
        // prepared them, as Hocuspocus does.
        Y.applyUpdate(room.document, update)
        const ack = coordinator.finishClientUpdate(connectionId, room.document)
        if (ack?.hash !== hashBytes(update)) return 'acknowledged under another hash'
        return `accepted ${api.persisted.at(-1)?.changeKind ?? 'unknown'}`
      } catch (error) {
        if (error instanceof CollaborationError) return `${error.code} ${error.reason}`
        throw error
      } finally {
        room.document.off('update', observe)
      }
    },
  }
  return room
}

function json(document: Y.Doc): JSONContent {
  return editorYDocToJson(document)
}

function counter(metrics: SidecarMetrics, series: string): number {
  const line = metrics.render().split('\n').find((entry) => entry.startsWith(`${series} `))
  return line ? Number(line.split(' ').at(-1)) : 0
}

const SENTENCE = 'Alpha several words to edit end.'

describe('normalizing the merge of an edit lease\'s update', () => {
  it('keeps another person\'s concurrent deletion pending when characters typed at its end inherited its mark', async () => {
    // The writer types directly behind "words" in a view without the
    // deletion; in the merge the typed character lands inside the
    // deletion's formatting. Edit-mode text never proposes in another
    // person's name, so the character loses the mark and the deletion
    // stays exactly as its author made it.
    const room = await openRoom(markdown(SENTENCE))
    try {
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const deletion = suggestionUpdate(base, 4002, REVIEWER, deleteText('words'))
      const typed = editorUpdate(base, 4001, (state) => state.tr.insertText('X', range(state.doc, 'words').to))
      base.destroy()

      expect(await room.submit('suggest', REVIEWER, deletion)).toBe('accepted suggestion')
      expect(await room.submit('edit', WRITER, typed)).toBe('accepted direct')

      expect(blocks(json(room.document))).toEqual(['paragraph:Alpha several words{deletion}X to edit end.'])
      const row = room.api.persisted.at(-1)!
      expect(row.rejectedSuggestionIds).toEqual([])
      expect(row.hash).not.toBe(hashBytes(typed))
      expect(counter(room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_total{rule="inherited_mark"}')).toBe(1)
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  it('keeps another person\'s concurrent deletion pending when the writer extends a run of their own it covers', async () => {
    // The writer's client keeps its id, so the character it types at the
    // end of its own earlier run continues that run's clocks; Yjs then
    // squashes both into one item that starts before the room's state.
    // The character is still the update's own and loses the inherited mark.
    const room = await openRoom(markdown(SENTENCE))
    try {
      const own = editorUpdate(room.document, 4001, (state) => state.tr.insertText('abc', range(state.doc, 'words').to))
      expect(await room.submit('edit', WRITER, own)).toBe('accepted direct')
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const deletion = suggestionUpdate(base, 4002, REVIEWER, deleteText('abc'))
      const typed = continuedUpdate(base, 4001, (state) => state.tr.insertText('d', range(state.doc, 'abc').to))
      base.destroy()

      expect(await room.submit('suggest', REVIEWER, deletion)).toBe('accepted suggestion')
      expect(await room.submit('edit', WRITER, typed)).toBe('accepted direct')

      expect(blocks(json(room.document))).toEqual(['paragraph:Alpha several wordsabc{deletion}d to edit end.'])
      expect(room.api.persisted.at(-1)!.rejectedSuggestionIds).toEqual([])
      expect(counter(room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_total{rule="inherited_mark"}')).toBe(1)
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  it('keeps another person\'s concurrent deletion pending in two parts where the writer\'s typing landed inside it', async () => {
    // The typed text takes the deletion's mark in the merge and loses it
    // again: it is the writer's own text, and the deletion keeps every
    // character it proposed, on both sides of it.
    const room = await openRoom(markdown(SENTENCE))
    try {
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const deletion = suggestionUpdate(base, 4002, REVIEWER, deleteText('words to'))
      const typed = editorUpdate(base, 4001, (state) => state.tr.insertText(' typed', range(state.doc, 'words').to))
      base.destroy()

      expect(await room.submit('suggest', REVIEWER, deletion)).toBe('accepted suggestion')
      const proposed = room.api.persisted[0]!.suggestionIds
      expect(await room.submit('edit', WRITER, typed)).toBe('accepted direct')

      // The writer's binding wrote the typed text behind the " t" it shares
      // with the text that follows: the deletion keeps exactly the
      // characters "words to" it proposed, and the typed ones are plain.
      expect(blocks(json(room.document))).toEqual(['paragraph:Alpha several words t{deletion}yped to{deletion} edit end.'])
      expect(room.api.persisted.at(-1)!).toMatchObject({ patches: [], rejectedSuggestionIds: [] })
      expect([...collectSuggestionRecords(json(room.document)).keys()]).toEqual(proposed)
      expect(counter(room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_total{rule="inherited_mark"}')).toBe(1)
      expect(counter(room.metrics, 'inqtrix_collaboration_suggestions_removed_by_edit_total')).toBe(0)
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  it('lets the writer\'s code formatting yield where it meets another person\'s concurrent deletion', async () => {
    const room = await openRoom(markdown(SENTENCE))
    try {
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const deletion = suggestionUpdate(base, 4002, REVIEWER, deleteText('words'))
      const code = editorUpdate(base, 4001, addMark('code', 'words to'))
      base.destroy()

      expect(await room.submit('suggest', REVIEWER, deletion)).toBe('accepted suggestion')
      expect(await room.submit('edit', WRITER, code)).toBe('accepted direct')

      expect(blocks(json(room.document))).toEqual(['paragraph:Alpha several words{deletion} to{code} edit end.'])
      expect(room.api.persisted.at(-1)!.rejectedSuggestionIds).toEqual([])
      expect(counter(room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_total{rule="excluded_mark"}')).toBeGreaterThan(0)
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  it('gives a list whose items two writers deleted at the same time an empty item instead of refusing the second deletion', async () => {
    const room = await openRoom(markdown('Intro.\n\n- one ITEMA\n- two ITEMB\n\nOutro.'))
    const deleteItem = (needle: string) => (state: EditorState): Transaction => {
      let from = -1
      let size = 0
      state.doc.descendants((node, position) => {
        if (node.type.name === 'listItem' && node.textContent.includes(needle)) {
          from = position
          size = node.nodeSize
        }
        return true
      })
      return state.tr.delete(from, from + size)
    }
    try {
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const second = editorUpdate(base, 4002, deleteItem('ITEMB'))
      const first = editorUpdate(base, 4001, deleteItem('ITEMA'))
      base.destroy()

      expect(await room.submit('edit', THIRD, second)).toBe('accepted direct')
      expect(await room.submit('edit', WRITER, first)).toBe('accepted direct')

      expect(blocks(json(room.document))).toEqual([
        'paragraph:Intro.',
        'bulletList:[listItem:[paragraph]]',
        'paragraph:Outro.',
      ])
      expect(counter(room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_total{rule="empty_container"}')).toBe(1)
      expect(await room.replayMatchesRoom()).toBe(true)

      // The item's paragraph holds one empty text node, as every new
      // textblock does: two writers who then type into the empty item at the
      // same time write into that one node, and both are accepted.
      const item = ((room.document.getXmlFragment(EDITOR_YJS_FRAGMENT).get(1) as Y.XmlElement)
        .get(0) as Y.XmlElement).get(0) as Y.XmlElement
      expect(item.toArray().map((child) => child instanceof Y.XmlText && child.length === 0))
        .toEqual([true])
      const filled = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const typeIntoItem = (text: string) => (state: EditorState): Transaction => {
        let at = -1
        state.doc.descendants((node, position) => {
          if (at < 0 && node.type.name === 'listItem') at = position + 2
          return at < 0
        })
        return state.tr.insertText(text, at)
      }
      const fromThird = editorUpdate(filled, 4003, typeIntoItem('Drei'))
      const fromWriter = editorUpdate(filled, 4004, typeIntoItem('Vier'))
      filled.destroy()
      expect(await room.submit('edit', THIRD, fromThird)).toBe('accepted direct')
      expect(await room.submit('edit', WRITER, fromWriter)).toBe('accepted direct')
      expect(blocks(json(room.document))[1]).toMatch(/^bulletList:\[listItem:\[paragraph:(DreiVier|VierDrei)\]\]$/)
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  it('removes the formatting two overlapping bold ranges leave redundant', async () => {
    const room = await openRoom(markdown(SENTENCE))
    try {
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const earlier = editorUpdate(base, 4002, addMark('bold', 'several', 'words'))
      const later = editorUpdate(base, 4001, addMark('bold', 'words', 'edit'))
      base.destroy()

      expect(await room.submit('edit', THIRD, earlier)).toBe('accepted direct')
      expect(await room.submit('edit', WRITER, later)).toBe('accepted direct')

      // Yjs reads the merged ranges as the earlier one: the end of the
      // earlier range lies inside the later one. The normalization keeps
      // exactly what the merge shows and removes only what repeats it.
      expect(blocks(json(room.document))).toEqual(['paragraph:Alpha several words{bold} to edit end.'])
      expect(counter(room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_total{rule="redundant_format"}')).toBeGreaterThan(0)
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  it('removes another person\'s format suggestion as a whole where the writer\'s typing split it, and keeps the original text it formats', async () => {
    // A format suggestion is a pair of halves that is valid only side by
    // side; separated, it cannot stay pending, and it is removed.
    // A proposed format keeps the original text marked for deletion and a
    // formatted copy marked as inserted; rejecting it drops the copy.
    const room = await openRoom(markdown(SENTENCE))
    try {
      const format = suggestionUpdate(room.document, 4002, REVIEWER, addMark('bold', 'several', 'words'))
      expect(await room.submit('suggest', REVIEWER, format)).toBe('accepted suggestion')
      const [proposal] = room.api.persisted[0]!.suggestions
      expect(proposal).toMatchObject({ kind: 'format' })
      const typed = editorUpdate(room.document, 4001, (state) => state.tr.insertText('X', range(state.doc, 'several').from + 3))

      expect(await room.submit('edit', WRITER, typed)).toBe('accepted direct')
      expect(blocks(json(room.document))).toEqual(['paragraph:Alpha sevXeral words to edit end.'])
      expect(room.api.persisted.at(-1)!.rejectedSuggestionIds).toEqual([proposal!.suggestionId])
      expect(counter(room.metrics, 'inqtrix_collaboration_suggestions_removed_by_edit_total')).toBe(1)
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  it('leaves an edit refused that would have to revert formatting recorded as another person\'s modification, rather than delete the text', async () => {
    // Stripping the modification's mark would decide it; removing it the way
    // a rejection does restores the formatting before it, which is a
    // decision's work. Deleting the formatted text instead would destroy
    // original text the proposal never touched.
    const ids = { patchId: uuid(), suggestionId: uuid() }
    const room = await openRoom(Y.encodeStateAsUpdate(editorJsonToYDoc({
      type: 'doc',
      content: [{
        type: 'paragraph',
        content: [
          { type: 'text', text: 'Alpha several ' },
          {
            type: 'text',
            text: 'words',
            marks: [
              { type: 'bold' },
              {
                type: 'modification',
                attrs: {
                  authorId: REVIEWER,
                  createdAt: CREATED_AT,
                  id: ids.suggestionId,
                  kind: 'format',
                  newValue: { type: 'bold' },
                  patchId: ids.patchId,
                  previousValue: null,
                  suggestionId: ids.suggestionId,
                  type: 'mark',
                },
              },
            ],
          },
          { type: 'text', text: ' to edit end.' },
        ],
      }],
    })))
    try {
      const stripped = rawUpdate(room.document, 4001, (fragment) => {
        const text = (fragment.get(0) as Y.XmlElement).get(0) as Y.XmlText
        const keys = (text.toDelta() as Array<{ attributes?: Record<string, unknown> }>)
          .flatMap((part) => Object.keys(part.attributes ?? {}))
          .filter((key) => key.startsWith('modification'))
        if (keys.length === 0) throw new Error('fixture has no modification')
        text.format('Alpha several '.length, 'words'.length, Object.fromEntries(keys.map((key) => [key, null])))
      })

      expect(await room.submit('edit', WRITER, stripped)).toBe(POLICY_REFUSAL)
      expect(room.api.persisted).toEqual([])
      expect(counter(room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_declined_total{reason="unsupported_suggestion"}')).toBe(1)
    } finally {
      await room.close()
    }
  })

  it('keeps another person\'s modification pending where text typed inside it took its mark', async () => {
    const ids = { patchId: uuid(), suggestionId: uuid() }
    const room = await openRoom(Y.encodeStateAsUpdate(editorJsonToYDoc({
      type: 'doc',
      content: [{
        type: 'paragraph',
        content: [
          { type: 'text', text: 'Alpha several ' },
          {
            type: 'text',
            text: 'words',
            marks: [
              { type: 'bold' },
              {
                type: 'modification',
                attrs: {
                  authorId: REVIEWER,
                  createdAt: CREATED_AT,
                  id: ids.suggestionId,
                  kind: 'format',
                  newValue: { type: 'bold' },
                  patchId: ids.patchId,
                  previousValue: null,
                  suggestionId: ids.suggestionId,
                  type: 'mark',
                },
              },
            ],
          },
          { type: 'text', text: ' to edit end.' },
        ],
      }],
    })))
    try {
      const typed = editorUpdate(room.document, 4001, (state) => state.tr.insertText('X', range(state.doc, 'words').from + 2))

      expect(await room.submit('edit', WRITER, typed)).toBe('accepted direct')
      expect(blocks(json(room.document))).toEqual(['paragraph:Alpha several wo{bold,modification}X{bold}rds{bold,modification} to edit end.'])
      expect(room.api.persisted.at(-1)!.rejectedSuggestionIds).toEqual([])
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  it('removes another person\'s replacement whose halves the writer\'s concurrent typing separated', async () => {
    const room = await openRoom(markdown('Alpha OLDWORD end.'))
    try {
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const replacement = suggestionUpdate(base, 4002, REVIEWER, (state) => {
        const { from, to } = range(state.doc, 'OLDWORD')
        return state.tr.insertText('NEWWORD', from, to)
      })
      const typed = editorUpdate(base, 4001, (state) => state.tr.insertText('x', range(state.doc, 'OLDWORD').to))
      base.destroy()

      expect(await room.submit('suggest', REVIEWER, replacement)).toBe('accepted suggestion')
      const proposed = room.api.persisted[0]!.suggestionIds
      expect(await room.submit('edit', WRITER, typed)).toBe('accepted direct')

      expect(blocks(json(room.document))).toEqual(['paragraph:Alpha OLDWORDx end.'])
      expect(room.api.persisted.at(-1)!.rejectedSuggestionIds).toEqual(proposed)
      expect(counter(room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_total{rule="invalid_composition"}')).toBe(1)
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  // A pair the merge left in a form no pending suggestion may take is
  // removed as a whole, whoever proposed it: in either order, the edit that
  // lands second removes the author's replacement, the author's own edit as
  // well as another writer's.
  it.each([
    ['the author', THIRD, WRITER],
    ['another writer', WRITER, THIRD],
  ] as const)('removes a replacement whose proposed text its author and another writer deleted between them at the same time, %s last', async (_last, first, last) => {
    // Each deletion alone leaves part of the proposed half; together they
    // leave the struck half without its counterpart.
    const room = await openRoom(markdown('Alpha OLDWORD end.'))
    try {
      const replacement = suggestionUpdate(room.document, 4002, WRITER, (state) => {
        const { from, to } = range(state.doc, 'OLDWORD')
        return state.tr.insertText('NEWWORD', from, to)
      })
      expect(await room.submit('suggest', WRITER, replacement)).toBe('accepted suggestion')
      const proposed = room.api.persisted[0]!.suggestionIds
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const front = (state: EditorState): Transaction => {
        const { from } = range(state.doc, 'NEWWORD')
        return state.tr.delete(from, from + 'NEWW'.length)
      }
      const back = (state: EditorState): Transaction => {
        const { from, to } = range(state.doc, 'NEWWORD')
        return state.tr.delete(from + 'NEWW'.length, to)
      }
      const earlier = editorUpdate(base, first === WRITER ? 4001 : 4003, front)
      const later = editorUpdate(base, last === WRITER ? 4001 : 4003, back)
      base.destroy()

      expect(await room.submit('edit', first, earlier)).toBe('accepted direct')
      expect(room.api.persisted.at(-1)!.rejectedSuggestionIds).toEqual([])
      expect(await room.submit('edit', last, later)).toBe('accepted direct')

      expect(blocks(json(room.document))).toEqual(['paragraph:Alpha OLDWORD end.'])
      expect(room.api.persisted.at(-1)!.rejectedSuggestionIds).toEqual(proposed)
      expect(collectSuggestionRecords(json(room.document)).size).toBe(0)
      expect(counter(room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_total{rule="invalid_composition"}')).toBe(1)
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })
})

describe('a suggestion that comes back through a writer\'s stale view', () => {
  const INITIAL = markdown('Alpha OLD end.')
  const toHeading = (state: EditorState): Transaction => (
    state.tr.setBlockType(1, 1, schema.nodes.heading!, { level: 2 })
  )
  const proposals = {
    deletion: deleteText('OLD'),
    insertion: (state: EditorState) => state.tr.insertText(' NEW', range(state.doc, 'Alpha').to),
  } as const

  /** A proposal by the reviewer, then a decision on it and an edit made in a
   * view that still showed it pending, in the order given. The edit turns
   * the paragraph into a heading, which the binding writes as a new block
   * with a copy of the text and its marks. */
  async function race(
    kind: keyof typeof proposals,
    decision: 'accept' | 'reject',
    order: 'decision first' | 'edit first',
  ): Promise<{ room: Room; verdicts: string[] }> {
    const room = await openRoom(INITIAL)
    const proposed = await room.submit('suggest', REVIEWER, suggestionUpdate(room.document, 4002, REVIEWER, proposals[kind]))
    const patchId = room.api.persisted[0]!.patches[0]!.patchId
    const stale = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
    const edit = editorUpdate(stale, 4001, toHeading)
    stale.destroy()
    const verdicts = order === 'decision first'
      ? [proposed, await room.decide(decision, [patchId]), await room.submit('edit', WRITER, edit)]
      : [proposed, await room.submit('edit', WRITER, edit), await room.decide(decision, [patchId])]
    return { room, verdicts }
  }

  const plain = (document: JSONContent): string => blocks(document).join(' | ').replace(/ +/g, ' ')

  it.each([
    ['accepted', 'insertion', 'accept', 'heading:Alpha NEW OLD end.'],
    ['rejected', 'insertion', 'reject', 'heading:Alpha OLD end.'],
    ['accepted', 'deletion', 'accept', 'heading:Alpha end.'],
    ['rejected', 'deletion', 'reject', 'heading:Alpha OLD end.'],
  ] as const)('takes its recorded outcome, not a blind rejection, when a decision %s the %s just before the edit arrived', async (_outcome, kind, decision, expected) => {
    const decisionFirst = await race(kind, decision, 'decision first')
    const editFirst = await race(kind, decision, 'edit first')
    try {
      expect(decisionFirst.verdicts).toEqual(['accepted suggestion', 'decided', 'accepted direct'])
      expect(editFirst.verdicts).toEqual(['accepted suggestion', 'accepted direct', 'decided'])
      const decided = json(decisionFirst.room.document)
      // No copy stays pending, and the text is what the decision left, the
      // same as when the edit came first.
      expect(collectSuggestionRecords(decided).size).toBe(0)
      expect(plain(decided)).toBe(expected)
      expect(plain(json(editFirst.room.document))).toBe(expected)
      // The decision rejected or accepted it; the edit's row names nothing.
      expect(decisionFirst.room.api.persisted.at(-1)!.rejectedSuggestionIds).toEqual([])
      expect(counter(decisionFirst.room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_total{rule="resurrected_suggestion"}')).toBe(1)
      expect(await decisionFirst.room.replayMatchesRoom()).toBe(true)
    } finally {
      await decisionFirst.room.close()
      await editFirst.room.close()
    }
  })

  it('removes a copy of a suggestion another writer\'s edit rejected, the way that rejection removed it', async () => {
    const room = await openRoom(INITIAL)
    try {
      expect(await room.submit('suggest', REVIEWER, suggestionUpdate(room.document, 4002, REVIEWER, proposals.insertion))).toBe('accepted suggestion')
      const stale = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const edit = editorUpdate(stale, 4001, toHeading)
      stale.destroy()
      // Another edit writer deletes the proposed text, which rejects it.
      const rejecting = editorUpdate(room.document, 4003, deleteText(' NEW'))
      expect(await room.submit('edit', THIRD, rejecting)).toBe('accepted direct')
      expect(room.api.persisted.at(-1)!.rejectedSuggestionIds).toHaveLength(1)

      expect(await room.submit('edit', WRITER, edit)).toBe('accepted direct')

      expect(blocks(json(room.document))).toEqual(['heading:Alpha OLD end.'])
      expect(room.api.persisted.at(-1)!.rejectedSuggestionIds).toEqual([])
      expect(counter(room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_total{rule="resurrected_suggestion"}')).toBe(1)
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  it('refuses a record in another person\'s name the room never held, with a warning naming it', async () => {
    const room = await openRoom(markdown(SENTENCE))
    try {
      const ids = { patchId: uuid(), suggestionId: uuid() }
      const forged = rawUpdate(room.document, 4001, (fragment) => {
        const text = (fragment.get(0) as Y.XmlElement).get(0) as Y.XmlText
        text.insert(text.length, ' forged', {
          insertion: { authorId: REVIEWER, createdAt: CREATED_AT, id: ids.suggestionId, kind: 'insertion', ...ids },
        })
      })

      expect(await room.submit('edit', WRITER, forged)).toBe(POLICY_REFUSAL)
      expect(room.api.persisted).toEqual([])
      // The warning leads to the writer through the collaboration lease,
      // whose record in the service names them. The log holds no person's id
      // and never the writer's session: a member's session id is the value of
      // the browser's session cookie. This room gives the first connection
      // the session id `connection-1`.
      expect(room.logs).toContainEqual({
        event: 'edit_merge_foreign_record_refused',
        fields: { actor_kind: 'user', lease_id: 'lease-1', room: ROOM, suggestion_id: ids.suggestionId },
        level: 'warn',
      })
      expect(JSON.stringify(room.logs)).not.toContain(WRITER)
      expect(JSON.stringify(room.logs)).not.toContain('connection-1')
      expect(counter(room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_declined_total{reason="forged_record"}')).toBe(1)
    } finally {
      await room.close()
    }
  })

  it('refuses a copy of a decided suggestion that names another patch than the one the room recorded', async () => {
    const room = await openRoom(INITIAL)
    try {
      expect(await room.submit('suggest', REVIEWER, suggestionUpdate(room.document, 4002, REVIEWER, proposals.insertion))).toBe('accepted suggestion')
      const [descriptor] = room.api.persisted[0]!.suggestions
      expect(await room.decide('accept', [descriptor!.patchId])).toBe('decided')
      const copy = rawUpdate(room.document, 4001, (fragment) => {
        const text = (fragment.get(0) as Y.XmlElement).get(0) as Y.XmlText
        text.insert(text.length, ' again', {
          insertion: {
            authorId: descriptor!.authorId,
            createdAt: descriptor!.createdAt,
            id: descriptor!.suggestionId,
            kind: 'insertion',
            patchId: uuid(),
            suggestionId: descriptor!.suggestionId,
          },
        })
      })

      expect(await room.submit('edit', WRITER, copy)).toBe(POLICY_REFUSAL)
      expect(room.api.persisted).toHaveLength(2)
    } finally {
      await room.close()
    }
  })

  it('refuses a copy of a suggestion decided before the room was loaded, since the room knows no outcome for it', async () => {
    const earlier = await openRoom(INITIAL)
    let decidedState: Uint8Array
    let edit: Uint8Array
    try {
      expect(await earlier.submit('suggest', REVIEWER, suggestionUpdate(earlier.document, 4002, REVIEWER, proposals.insertion))).toBe('accepted suggestion')
      const stale = documentFrom(Y.encodeStateAsUpdate(earlier.document), 7)
      edit = editorUpdate(stale, 4001, toHeading)
      stale.destroy()
      expect(await earlier.decide('accept', [earlier.api.persisted[0]!.patches[0]!.patchId])).toBe('decided')
      decidedState = Y.encodeStateAsUpdate(earlier.document)
    } finally {
      await earlier.close()
    }
    const room = await openRoom(decidedState)
    try {
      expect(await room.submit('edit', WRITER, edit)).toBe(POLICY_REFUSAL)
    } finally {
      await room.close()
    }
  })
})

describe('the row of a normalized update', () => {
  /** The room holds the reviewer's deletion; the writer's typing inherits it. */
  async function inheritedMarkRoom(initial: Uint8Array): Promise<{ room: Room; typed: Uint8Array }> {
    const room = await openRoom(initial)
    const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
    const deletion = suggestionUpdate(base, 4002, REVIEWER, deleteText('words'))
    const typed = editorUpdate(base, 4001, (state) => state.tr.insertText('X', range(state.doc, 'words').to))
    base.destroy()
    expect(await room.submit('suggest', REVIEWER, deletion)).toBe('accepted suggestion')
    return { room, typed }
  }

  it('is the same bytes when the same update is prepared again on the same room state', async () => {
    const initial = markdown(SENTENCE)
    const first = await inheritedMarkRoom(initial)
    const second = await openRoom(initial)
    try {
      expect(await second.submit('suggest', REVIEWER, first.room.api.persisted[0]!.update)).toBe('accepted suggestion')
      expect(await first.room.submit('edit', WRITER, first.typed)).toBe('accepted direct')
      expect(await second.submit('edit', WRITER, first.typed)).toBe('accepted direct')

      const [left, right] = [first.room.api.persisted.at(-1)!, second.api.persisted.at(-1)!]
      expect(left.hash).not.toBe(hashBytes(first.typed))
      expect(right.hash).toBe(left.hash)
      expect(Buffer.compare(right.update, left.update)).toBe(0)
    } finally {
      await first.room.close()
      await second.close()
    }
  })

  it('lands once when its persistence outcome stayed unknown and the writer sent the update again', async () => {
    const room = await openRoom(markdown('Intro.\n\n- one ITEMA\n- two ITEMB\n\nOutro.'))
    const deleteItem = (needle: string) => (state: EditorState): Transaction => {
      let from = -1
      let size = 0
      state.doc.descendants((node, position) => {
        if (node.type.name === 'listItem' && node.textContent.includes(needle)) {
          from = position
          size = node.nodeSize
        }
        return true
      })
      return state.tr.delete(from, from + size)
    }
    try {
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const second = editorUpdate(base, 4002, deleteItem('ITEMB'))
      const first = editorUpdate(base, 4001, deleteItem('ITEMA'))
      base.destroy()
      expect(await room.submit('edit', THIRD, second)).toBe('accepted direct')

      // The normalized row is sent, and the answer is lost while the request
      // is still on its way to the store.
      room.api.holdNextPersist = true
      expect(await room.submit('edit', WRITER, first)).toBe('4503 service_unavailable')
      await room.reload()
      // The writer sends the same update again; then the first request
      // reaches the store after all.
      expect(await room.submit('edit', WRITER, first)).toBe('accepted direct')
      expect(room.api.commitHeld()).toEqual([expect.objectContaining({ duplicate: true })])

      expect(room.api.rows).toHaveLength(2)
      expect(blocks(json(room.document))).toEqual([
        'paragraph:Intro.',
        'bulletList:[listItem:[paragraph]]',
        'paragraph:Outro.',
      ])
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  it('names the removal it made again when the writer sends the update again after an unknown outcome', async () => {
    // The resend is prepared from the earlier row's bytes, which already
    // remove the replacement the writer's typing separated; the row names
    // the removal as the earlier one did.
    const room = await openRoom(markdown('Alpha OLDWORD end.'))
    try {
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const replacement = suggestionUpdate(base, 4002, REVIEWER, (state) => {
        const { from, to } = range(state.doc, 'OLDWORD')
        return state.tr.insertText('NEWWORD', from, to)
      })
      const typed = editorUpdate(base, 4001, (state) => state.tr.insertText('x', range(state.doc, 'OLDWORD').to))
      base.destroy()
      expect(await room.submit('suggest', REVIEWER, replacement)).toBe('accepted suggestion')
      const [proposed] = room.api.persisted[0]!.suggestionIds

      room.api.holdNextPersist = true
      expect(await room.submit('edit', WRITER, typed)).toBe('4503 service_unavailable')
      expect(room.api.held[0]!.rejectedSuggestionIds).toEqual([proposed])
      await room.reload()
      expect(await room.submit('edit', WRITER, typed)).toBe('accepted direct')

      const row = room.api.persisted.at(-1)!
      expect(Buffer.compare(row.update, room.api.held[0]!.update)).toBe(0)
      expect(row.rejectedSuggestionIds).toEqual([proposed])
    } finally {
      await room.close()
    }
  })

  it('lands its effect once when the room moved on before the writer sent the update again', async () => {
    const room = await openRoom(markdown('Intro.\n\n- one ITEMA\n- two ITEMB\n\nOutro.'))
    const deleteItem = (needle: string) => (state: EditorState): Transaction => {
      let from = -1
      let size = 0
      state.doc.descendants((node, position) => {
        if (node.type.name === 'listItem' && node.textContent.includes(needle)) {
          from = position
          size = node.nodeSize
        }
        return true
      })
      return state.tr.delete(from, from + size)
    }
    try {
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const second = editorUpdate(base, 4002, deleteItem('ITEMB'))
      const first = editorUpdate(base, 4001, deleteItem('ITEMA'))
      base.destroy()
      expect(await room.submit('edit', THIRD, second)).toBe('accepted direct')

      room.api.holdNextPersist = true
      expect(await room.submit('edit', WRITER, first)).toBe('4503 service_unavailable')
      await room.reload()
      // Another writer's change lands before the resend, so a new
      // normalization of the update would be planned on another state.
      const elsewhere = editorUpdate(room.document, 4003, (state) => state.tr.insertText(' more', range(state.doc, 'Outro.').to))
      expect(await room.submit('edit', THIRD, elsewhere)).toBe('accepted direct')
      expect(await room.submit('edit', WRITER, first)).toBe('accepted direct')
      room.api.commitHeld()

      // Whatever the late request added, replaying the store gives the room:
      // one empty item, not one per normalization.
      expect(blocks(json(room.document))).toEqual([
        'paragraph:Intro.',
        'bulletList:[listItem:[paragraph]]',
        'paragraph:Outro. more',
      ])
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  it('stays small on a document whose history holds thousands of deleted ranges', async () => {
    const long = Array.from({ length: 3_000 }, (_value, index) => `w${index}`).join(' ')
    const room = await openRoom(markdown(`${long}\n\n${SENTENCE}`))
    try {
      // Every other word goes: three thousand separate deleted ranges.
      const sparse = rawUpdate(room.document, 4003, (fragment) => {
        const text = (fragment.get(0) as Y.XmlElement).get(0) as Y.XmlText
        const words = text.toString().split(' ')
        let index = text.length
        for (let word = words.length - 1; word >= 0; word -= 1) {
          index -= words[word]!.length
          if (word % 2 === 0) text.delete(index, words[word]!.length)
          index -= 1
        }
      })
      expect(await room.submit('edit', THIRD, sparse)).toBe('accepted direct')
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const deletion = suggestionUpdate(base, 4002, REVIEWER, deleteText('words'))
      const typed = editorUpdate(base, 4001, (state) => state.tr.insertText('X', range(state.doc, 'words').to))
      base.destroy()
      expect(await room.submit('suggest', REVIEWER, deletion)).toBe('accepted suggestion')
      expect(await room.submit('edit', WRITER, typed)).toBe('accepted direct')

      const row = room.api.persisted.at(-1)!
      expect(row.hash).not.toBe(hashBytes(typed))
      // The row carries the update, the normalization's own items and the
      // deletions they made, never the document's deletion history, which
      // alone takes kilobytes here.
      const history = Y.encodeStateAsUpdate(room.document, Y.encodeStateVector(room.document))
      expect(history.byteLength).toBeGreaterThan(2_000)
      expect(typed.byteLength).toBeLessThan(100)
      expect(row.update.byteLength).toBeLessThan(typed.byteLength + 200)
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })
})

describe('when the service normalizes', () => {
  it('persists an update the checks accept with the client\'s own bytes, and never plans a normalization for it', async () => {
    const room = await openRoom(markdown(SENTENCE))
    try {
      const typed = editorUpdate(room.document, 4001, (state) => state.tr.insertText(' more', range(state.doc, 'end.').to))
      expect(await room.submit('edit', WRITER, typed)).toBe('accepted direct')

      expect(room.api.persisted.at(-1)!.hash).toBe(hashBytes(typed))
      expect(Buffer.compare(room.api.persisted.at(-1)!.update, typed)).toBe(0)
      expect(room.metrics.render()).not.toContain('inqtrix_collaboration_edit_merge_normalization_seconds')
    } finally {
      await room.close()
    }
  })

  it('never normalizes the update of a suggest-only lease', async () => {
    const room = await openRoom(markdown(SENTENCE))
    try {
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const code = editorUpdate(base, 4001, addMark('code', 'words to'))
      const deletion = suggestionUpdate(base, 4002, REVIEWER, deleteText('words'))
      base.destroy()
      expect(await room.submit('edit', WRITER, code)).toBe('accepted direct')

      // The proposed deletion meets the concurrent code formatting: for an
      // edit lease its own mark would yield; a suggest-only lease is refused.
      expect(await room.submit('suggest', REVIEWER, deletion)).toMatch(/^44(03|09) /)
      expect(room.api.persisted).toHaveLength(1)
      expect(room.metrics.render()).not.toContain('inqtrix_collaboration_edit_merge_normalization_seconds')
    } finally {
      await room.close()
    }
  })

  it('never trims a suggestion an edit lease proposes to fit concurrent formatting', async () => {
    // An edit writer in suggest mode proposes deleting "words" while another
    // writer formats it as code. Code excludes the proposal's mark; where the
    // writer's own formatting would yield, the writer's proposal stays as
    // proposed, and the refusal stands.
    const room = await openRoom(markdown(SENTENCE))
    try {
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const code = editorUpdate(base, 4003, addMark('code', 'words to'))
      const deletion = suggestionUpdate(base, 4001, WRITER, deleteText('words'))
      base.destroy()
      expect(await room.submit('edit', THIRD, code)).toBe('accepted direct')

      expect(await room.submit('edit', WRITER, deletion)).toMatch(/^44(03|09) /)
      expect(room.api.persisted).toHaveLength(1)
    } finally {
      await room.close()
    }
  })

  it('reads no more than its unit budget and then leaves the refusal standing', async () => {
    const room = await openRoom(markdown(SENTENCE))
    try {
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const deletion = suggestionUpdate(base, 4002, REVIEWER, deleteText('words'))
      const typed = editorUpdate(base, 4001, (state) => state.tr.insertText('X', range(state.doc, 'words').to))
      base.destroy()
      expect(await room.submit('suggest', REVIEWER, deletion)).toBe('accepted suggestion')

      const validated = validateDocument(room.document, settings().documentLimitBytes)
      const merged = documentFrom(validated.encodedState, 9)
      Y.applyUpdate(merged, typed)
      const normalize = (unitBudget: number) => normalizeEditMerge({
        actorUserId: WRITER,
        before: validated.canonicalJson,
        documentLimitBytes: settings().documentLimitBytes,
        followUps: [],
        known: () => undefined,
        merged,
        room: room.document,
        roomState: validated.encodedState,
        roomStateHash: validated.stateHash,
        unitBudget,
        update: typed,
      })
      const generous = normalize(100_000)
      expect(generous.declined).toBeNull()
      if (generous.declined === null) {
        expect(generous.normalization.unitsRead).toBeGreaterThan(10)
        generous.normalization.state.destroy()
      }
      expect(normalize(10)).toMatchObject({ declined: 'budget' })
      merged.destroy()
    } finally {
      await room.close()
    }
  })

  it('sends every connection one frame for a normalized update, never the state the checks refused', async () => {
    const room = await openRoom(markdown(SENTENCE))
    try {
      const before = Y.encodeStateAsUpdate(room.document)
      const base = documentFrom(before, 7)
      const deletion = suggestionUpdate(base, 4002, REVIEWER, deleteText('words'))
      const typed = editorUpdate(base, 4001, (state) => state.tr.insertText('X', range(state.doc, 'words').to))
      base.destroy()
      expect(await room.submit('suggest', REVIEWER, deletion)).toBe('accepted suggestion')
      const proposed = Y.encodeStateAsUpdate(room.document)

      expect(await room.submit('edit', WRITER, typed)).toBe('accepted direct')

      expect(room.frames).toHaveLength(1)
      // A receiver that held the room's state before gets the normalized
      // state from that one frame.
      const receiver = documentFrom(proposed, 8)
      Y.applyUpdate(receiver, room.frames[0]!)
      expect(blocks(json(receiver))).toEqual(['paragraph:Alpha several words{deletion}X to edit end.'])
      receiver.destroy()
    } finally {
      await room.close()
    }
  })
})

describe('editing a pending suggestion while others work on it', () => {
  // The reviewer proposes " NEW WORDS" behind "Alpha"; every case starts
  // from such a room.
  const PROPOSAL = ' NEW WORDS'

  async function proposedRoom(proposal = PROPOSAL): Promise<{ proposed: string; room: Room }> {
    const room = await openRoom(markdown('Alpha end.'))
    const proposalUpdate = suggestionUpdate(room.document, 4002, REVIEWER, (state) => (
      state.tr.insertText(proposal, range(state.doc, 'Alpha').to)
    ))
    expect(await room.submit('suggest', REVIEWER, proposalUpdate)).toBe('accepted suggestion')
    const [proposed] = room.api.persisted[0]!.suggestionIds
    return { proposed: proposed!, room }
  }

  /** The suggestion the room holds under an id, with its text, or null. */
  function pending(document: Y.Doc, suggestionId: string): { authorId: string; text: string } | null {
    const record = collectSuggestionRecords(json(document)).get(suggestionId)
    if (!record) return null
    let text = ''
    const visit = (node: JSONContent): void => {
      if ((node.marks ?? []).some((mark) => (mark.attrs as { suggestionId?: string } | undefined)?.suggestionId === suggestionId)) {
        text += node.text ?? ''
      }
      for (const child of node.content ?? []) visit(child)
    }
    visit(json(document))
    return { authorId: record.authorId, text }
  }

  const plainAt = (needle: string, offset: number, value: string) => (state: EditorState): Transaction => (
    state.tr.insert(range(state.doc, needle).from + offset, schema.text(value))
  )

  it.each(['the author first', 'the writer first'] as const)('keeps the suggestion pending when a writer deletes part of it while its author extends it, %s', async (order) => {
    const { proposed, room } = await proposedRoom()
    try {
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const extension = suggestionUpdate(base, 4002, REVIEWER, (state) => state.tr.insertText('!', range(state.doc, 'WORDS').to))
      const shrink = editorUpdate(base, 4001, deleteText(' NEW'))
      base.destroy()
      const verdicts = order === 'the author first'
        ? [await room.submit('suggest', REVIEWER, extension), await room.submit('edit', WRITER, shrink)]
        : [await room.submit('edit', WRITER, shrink), await room.submit('suggest', REVIEWER, extension)]

      expect(verdicts.sort()).toEqual(['accepted direct', 'accepted suggestion'])
      expect(pending(room.document, proposed)).toEqual({ authorId: REVIEWER, text: ' WORDS!' })
      expect(room.api.persisted.every((row) => row.rejectedSuggestionIds.length === 0)).toBe(true)
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  it.each(['the author first', 'the writer first'] as const)('keeps the suggestion pending and formatted when a writer formats it while its author deletes part of it, %s', async (order) => {
    const { proposed, room } = await proposedRoom()
    try {
      const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
      const shrink = suggestionUpdate(base, 4002, REVIEWER, deleteText('NEW '))
      const bold = editorUpdate(base, 4001, addMark('bold', PROPOSAL))
      base.destroy()
      const verdicts = order === 'the author first'
        ? [await room.submit('suggest', REVIEWER, shrink), await room.submit('edit', WRITER, bold)]
        : [await room.submit('edit', WRITER, bold), await room.submit('suggest', REVIEWER, shrink)]

      expect(verdicts.sort()).toEqual(['accepted direct', 'accepted suggestion'])
      expect(blocks(json(room.document))).toEqual(['paragraph:Alpha WORDS{bold,insertion} end.'])
      expect(pending(room.document, proposed)).toEqual({ authorId: REVIEWER, text: ' WORDS' })
      expect(room.api.persisted.every((row) => row.rejectedSuggestionIds.length === 0)).toBe(true)
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      await room.close()
    }
  })

  // The proposal holds no character equal to its neighbours, so each text
  // write pairs the characters it keeps unambiguously.
  it.each([
    ['accept', 'paragraph:AlphaNEWxWORDS end.'],
    ['reject', 'paragraph:Alphax end.'],
  ] as const)('lands on the same text when a decision to %s meets a writer typing inside the suggestion, in either order', async (decision, expected) => {
    const outcomes: string[][] = []
    for (const order of ['decision first', 'typing first'] as const) {
      const { proposed, room } = await proposedRoom('NEWWORDS')
      try {
        const patchId = room.api.persisted[0]!.patches[0]!.patchId
        const base = documentFrom(Y.encodeStateAsUpdate(room.document), 7)
        const typed = editorUpdate(base, 4001, plainAt('NEWWORDS', 3, 'x'))
        base.destroy()
        const verdicts = order === 'decision first'
          ? [await room.decide(decision, [patchId]), await room.submit('edit', WRITER, typed)]
          : [await room.submit('edit', WRITER, typed), await room.decide(decision, [patchId])]

        expect(verdicts.sort()).toEqual(['accepted direct', 'decided'])
        expect(pending(room.document, proposed)).toBeNull()
        expect(await room.replayMatchesRoom()).toBe(true)
        outcomes.push(blocks(json(room.document)))
      } finally {
        await room.close()
      }
    }
    expect(outcomes).toEqual([[expected], [expected]])
  })
})

describe('undo after editing a pending suggestion', () => {
  const ORIGIN = { writer: 'local' }

  /** A writer's client with the undo history the editor keeps: its own
   * changes only. */
  function undoableClient(state: Uint8Array, clientId: number) {
    const document = documentFrom(state, clientId)
    const fragment = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
    const history = new Y.UndoManager(fragment, { trackedOrigins: new Set([ORIGIN]) })
    const capture = (change: () => void): Uint8Array => {
      const updates: Uint8Array[] = []
      const record = (update: Uint8Array): void => {
        updates.push(update)
      }
      document.on('update', record)
      try {
        change()
      } finally {
        document.off('update', record)
      }
      return Y.mergeUpdates(updates)
    }
    return {
      destroy: () => {
        history.destroy()
        document.destroy()
      },
      edit: (build: (state: EditorState) => Transaction): Uint8Array => capture(() => {
        const initialized = initProseMirrorDoc(fragment, schema)
        const target = build(EditorState.create({ doc: initialized.doc, schema })).doc
        document.transact(() => updateYFragment(document, fragment, target, initialized.meta), ORIGIN)
      }),
      history,
      receive: (update: Uint8Array): void => Y.applyUpdate(document, update),
      undo: (): Uint8Array => capture(() => {
        history.undo()
      }),
    }
  }

  async function proposed(): Promise<{ room: Room; suggestionId: string }> {
    const room = await openRoom(markdown('Alpha end.'))
    expect(await room.submit('suggest', REVIEWER, suggestionUpdate(room.document, 4002, REVIEWER, (state) => (
      state.tr.insertText(' NEW WORDS', range(state.doc, 'Alpha').to)
    )))).toBe('accepted suggestion')
    return { room, suggestionId: room.api.persisted[0]!.suggestionIds[0]! }
  }

  it('gives characters deleted from another person\'s suggestion back as the writer\'s plain text, and keeps the suggestion pending', async () => {
    const { room, suggestionId } = await proposed()
    const client = undoableClient(Y.encodeStateAsUpdate(room.document), 4001)
    try {
      expect(await room.submit('edit', WRITER, client.edit(deleteText('NEW ')))).toBe('accepted direct')
      expect(blocks(json(room.document))).toEqual(['paragraph:Alpha WORDS{insertion} end.'])

      const undone = client.undo()
      expect(await room.submit('edit', WRITER, undone)).toBe('accepted direct')

      // " " and "WORDS" are what is left of the suggestion; "NEW " is back as
      // the writer's own text between them.
      expect(blocks(json(room.document))).toEqual(['paragraph:Alpha {insertion}NEW WORDS{insertion} end.'])
      expect(collectSuggestionRecords(json(room.document)).has(suggestionId)).toBe(true)
      expect(room.api.persisted.at(-1)!.rejectedSuggestionIds).toEqual([])
      // The undo writes the characters back into the suggestion's
      // formatting; the service clears the mark on them.
      expect(counter(room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_total{rule="inherited_mark"}')).toBe(1)
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      client.destroy()
      await room.close()
    }
  })

  it('leaves a suggestion the writer removed removed when the writer undoes the removal, and keeps the rest of the history', async () => {
    const { room, suggestionId } = await proposed()
    const client = undoableClient(Y.encodeStateAsUpdate(room.document), 4001)
    try {
      expect(await room.submit('edit', WRITER, client.edit((state) => state.tr.insertText('!', range(state.doc, 'end.').to)))).toBe('accepted direct')
      client.history.stopCapturing()
      expect(await room.submit('edit', WRITER, client.edit(deleteText(' NEW WORDS')))).toBe('accepted direct')
      expect(room.api.persisted.at(-1)!.rejectedSuggestionIds).toEqual([suggestionId])
      client.history.stopCapturing()

      expect(await room.submit('edit', WRITER, client.undo())).toBe('accepted direct')
      expect(collectSuggestionRecords(json(room.document)).has(suggestionId)).toBe(false)
      expect(room.api.persisted.at(-1)!.rejectedSuggestionIds).toEqual([])
      // The undo writes the suggestion back in its author's name; it takes
      // the outcome the room recorded for it, and its text goes again.
      expect(counter(room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_total{rule="resurrected_suggestion"}')).toBe(1)
      const afterRemovalUndo = blocks(json(room.document))
      // The writer's client takes the service's row, and the next undo step
      // still works on it.
      client.receive(Y.encodeStateAsUpdate(room.document))
      expect(await room.submit('edit', WRITER, client.undo())).toBe('accepted direct')

      expect(afterRemovalUndo).toEqual(['paragraph:Alpha end.!'])
      expect(blocks(json(room.document))).toEqual(['paragraph:Alpha end.'])
      expect(await room.replayMatchesRoom()).toBe(true)
    } finally {
      client.destroy()
      await room.close()
    }
  })

  it('leaves the writer\'s own suggestion withdrawn when the writer undoes the edit that withdrew it, and keeps the rest of the history', async () => {
    const room = await openRoom(markdown('Alpha end.'))
    try {
      expect(await room.submit('suggest', WRITER, suggestionUpdate(room.document, 4003, WRITER, (state) => (
        state.tr.insertText(' NEW WORDS', range(state.doc, 'Alpha').to)
      )))).toBe('accepted suggestion')
      const suggestionId = room.api.persisted[0]!.suggestionIds[0]!
      const client = undoableClient(Y.encodeStateAsUpdate(room.document), 4001)
      try {
        expect(await room.submit('edit', WRITER, client.edit((state) => state.tr.insertText('!', range(state.doc, 'end.').to)))).toBe('accepted direct')
        client.history.stopCapturing()
        // The edit deletes original text together with every character the
        // writer proposed: the writer's own suggestion is withdrawn.
        expect(await room.submit('edit', WRITER, client.edit((state) => (
          state.tr.delete(range(state.doc, 'Alpha').from, range(state.doc, 'WORDS').to)
        )))).toBe('accepted direct')
        expect(room.api.persisted.at(-1)!.rejectedSuggestionIds).toEqual([suggestionId])
        client.history.stopCapturing()

        expect(await room.submit('edit', WRITER, client.undo())).toBe('accepted direct')
        // The undo writes the suggestion back in the writer's own name; the
        // room knows it as removed, so it takes that outcome, exactly as
        // another person's suggestion would. The original text comes back.
        expect(collectSuggestionRecords(json(room.document)).has(suggestionId)).toBe(false)
        expect(blocks(json(room.document))).toEqual(['paragraph:Alpha end.!'])
        expect(room.api.persisted.at(-1)!.rejectedSuggestionIds).toEqual([])
        expect(room.api.persisted.at(-1)!.suggestionIds).toEqual([])
        expect(counter(room.metrics, 'inqtrix_collaboration_edit_merge_normalizations_total{rule="resurrected_suggestion"}')).toBe(1)
        client.receive(Y.encodeStateAsUpdate(room.document))
        expect(await room.submit('edit', WRITER, client.undo())).toBe('accepted direct')

        expect(blocks(json(room.document))).toEqual(['paragraph:Alpha end.'])
        expect(await room.replayMatchesRoom()).toBe(true)
      } finally {
        client.destroy()
      }
    } finally {
      await room.close()
    }
  })
})
