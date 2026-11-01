import { getSchema } from '@tiptap/core'
import { initProseMirrorDoc, updateYFragment } from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  createEditorSchemaExtensions,
  editorCollaborationRoom,
  editorJsonToYDoc,
  editorYDocToJson,
  getEditorSchemaFingerprint,
  parseEditorMarkdown,
} from '@inqtrix/editor-schema'
import { describe, expect, it, vi } from 'vitest'
import * as Y from 'yjs'

import type { ConnectionContext, SidecarLogger } from '../src/contracts'
import { ENCODED_GROWTH_PER_UPDATE_BYTE, DocumentCoordinator } from '../src/documentCoordinator'
import * as documentStateModule from '../src/documentState'
import { hashBytes, sameEncodedState } from '../src/documentState'
import { ApiRequestError, CollaborationError } from '../src/errors'
import { InstanceLeaseManager } from '../src/instanceLease'
import { SidecarMetrics } from '../src/metrics'
import {
  FakeCollaborationApi,
  USER_ID,
  documentState,
  frameReferringInto,
  highSurrogateOf,
  settings,
  silentLogger,
  varString,
  varUint,
} from './helpers'

// A room keeps a private copy of itself, on which every update is applied and
// validated before anything else sees it. These cases hold what makes that
// copy safe: it never carries what was refused, and whatever it can no longer
// vouch for is found out, as it was when each update got a fresh copy.
const DOCUMENT_ID = 'ed_check_document'
const ROOM = editorCollaborationRoom(DOCUMENT_ID, 1)
const REBUILDS = 'inqtrix_collaboration_check_document_rebuilds_total'
const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

type SendOptions = {
  allowNoop?: boolean
  /** What the transport applies to the room: the client's bytes unless given,
   * nothing for a connection that turned read-only meanwhile. */
  applied?: Uint8Array | null
}

async function openRoom(
  initial: string | Uint8Array,
  overrides: Parameters<typeof settings>[0] = {},
) {
  const api = new FakeCollaborationApi()
  const configured = settings(overrides)
  const metrics = new SidecarMetrics()
  const logged: string[] = []
  const logger: SidecarLogger = {
    ...silentLogger,
    error: (event) => { logged.push(event) },
    warn: (event) => { logged.push(event) },
  }
  const lease = new InstanceLeaseManager(api, configured, silentLogger, metrics, () => undefined)
  await lease.start()
  const coordinator = new DocumentCoordinator(api, lease, configured, logger, metrics)
  coordinator.initialize(ROOM, 0, { bytes: 0, updates: 0 })
  const schemaHash = await getEditorSchemaFingerprint()
  let document = typeof initial === 'string'
    ? editorJsonToYDoc(parseEditorMarkdown(initial))
    : (() => {
        const created = new Y.Doc()
        Y.applyUpdate(created, initial)
        return created
      })()
  let connections = 0
  let commands = 0
  const context: ConnectionContext = {
    access: 'edit',
    documentId: DOCUMENT_ID,
    expiresAt: Date.now() / 1_000 + 600,
    generation: 1,
    leaseId: 'lease-1',
    policyCursor: 0,
    protocolVersion: configured.protocolVersion,
    schemaHash,
    schemaVersion: configured.schemaVersion,
    sessionId: 'session-1',
    tenantId: 'tenant-1',
    user: { color: '#123456', id: USER_ID, name: 'Ada' },
  }
  return {
    api,
    configured,
    coordinator,
    logged,
    metrics,
    get document() {
      return document
    },
    close: async () => {
      document.destroy()
      await lease.stop()
    },
    text: () => JSON.stringify(editorYDocToJson(document)),
    rebuilds: () => counter(metrics, REBUILDS),
    /** What the sidecar does for a frame: prepare, let the transport apply,
     * finish. Answers with the outcome. */
    send: async (update: Uint8Array, options: SendOptions = {}): Promise<string> => {
      connections += 1
      const connectionId = `connection-${connections}`
      try {
        const prepared = await coordinator.prepareClientUpdate({
          allowNoop: options.allowNoop ?? false,
          connectionId,
          context,
          document,
          room: ROOM,
          update,
        })
        const applied = options.applied === undefined ? update : options.applied
        if (applied) Y.applyUpdate(document, applied)
        if (prepared === 'noop') return 'noop'
        coordinator.finishClientUpdate(connectionId, document)
        return 'accepted'
      } catch (error) {
        if (error instanceof CollaborationError) return error.reason
        throw error
      }
    },
    decide: async (change: (clone: Y.Doc) => void, command?: string): Promise<string> => {
      commands += 1
      try {
        await coordinator.applyServerMutation({
          actorKind: 'human',
          actorUserId: USER_ID,
          changeKind: 'decision',
          commandId: command ?? `abcdabcd-1111-4000-8000-${commands.toString(16).padStart(12, '0')}`,
          commandPayloadHash: command ?? hashBytes(new TextEncoder().encode(`decision-${commands}`)),
          decision: 'accept',
          document,
          documentId: DOCUMENT_ID,
          expectedSequence: coordinator.getPersistedSequence(ROOM),
          generation: 1,
          mutate: (clone) => {
            change(clone)
            return { patchIds: [], suggestionIds: [], suggestions: [] }
          },
          requestedPatchIds: [],
          room: ROOM,
          sequenceRule: 'not_ahead',
        })
        return 'decided'
      } catch (error) {
        if (error instanceof CollaborationError) return error.reason
        throw error
      }
    },
    /** A new room document from the given state, as a reload of the store builds it. */
    reload: (state: Uint8Array) => {
      const sequence = api.persisted.length
      coordinator.markUnloaded(ROOM)
      document.destroy()
      document = new Y.Doc()
      Y.applyUpdate(document, state)
      coordinator.initialize(
        ROOM,
        sequence,
        { bytes: 0, updates: 0 },
        documentStateModule.validateDocument(document, configured.documentLimitBytes),
      )
    },
  }
}

function counter(metrics: SidecarMetrics, series: string): number {
  const line = metrics.render().split('\n').find((entry) => entry.startsWith(`${series} `))
  return line ? Number(line.split(' ').at(-1)) : 0
}

/** One person's editor: a replica with a client id of its own that writes
 * through the editor binding. */
class Writer {
  readonly doc = new Y.Doc()

  constructor(clientId: number, state: Uint8Array | Y.Doc) {
    this.doc.clientID = clientId
    Y.applyUpdate(this.doc, state instanceof Y.Doc ? Y.encodeStateAsUpdate(state) : state)
  }

  write(markdown: string): Uint8Array {
    const fragment = this.doc.getXmlFragment(EDITOR_YJS_FRAGMENT)
    const initialized = initProseMirrorDoc(fragment, schema)
    const vector = Y.encodeStateVector(this.doc)
    updateYFragment(this.doc, fragment, schema.nodeFromJSON(parseEditorMarkdown(markdown)), initialized.meta)
    return Y.encodeStateAsUpdate(this.doc, vector)
  }

  change(mutate: (fragment: Y.XmlFragment, doc: Y.Doc) => void): Uint8Array {
    const vector = Y.encodeStateVector(this.doc)
    this.doc.transact(() => mutate(this.doc.getXmlFragment(EDITOR_YJS_FRAGMENT), this.doc))
    return Y.encodeStateAsUpdate(this.doc, vector)
  }

  receive(update: Uint8Array): void {
    Y.applyUpdate(this.doc, update)
  }
}

/** A frame whose one struct the room already holds, naming a root of its own. */
function frameNamingAForeignRoot(client: number): Uint8Array {
  return new Uint8Array([
    ...varUint(1), ...varUint(1), ...varUint(client), ...varUint(0),
    4, // a string, without origins
    ...varUint(1), ...varString('evil'),
    ...varString('x'),
    ...varUint(0),
  ])
}

/** Deletes every other clock of a run, in one frame without structs. */
function frameDeletingEveryOther(client: number, start: number, count: number): Uint8Array {
  const ranges: number[] = []
  for (let index = 0; index < count; index += 1) {
    ranges.push(...varUint(start + index * 2), ...varUint(1))
  }
  return new Uint8Array([
    ...varUint(0),
    ...varUint(1), ...varUint(client), ...varUint(count),
    ...ranges,
  ])
}

/** Two frames with the same ids and different content, as only a transport
 * that applied other bytes than it reported could produce. */
function sameIdsOtherContent(document: Y.Doc): { applied: Uint8Array; reported: Uint8Array } {
  const reported = new Writer(4_200, document).write('Hello!')
  const applied = new Writer(4_200, document).write('Hello?')
  return { applied, reported }
}

function invented(document: Y.Doc, clientId: number): Uint8Array {
  return new Writer(clientId, document).change((_fragment, doc) => {
    doc.getText('rogue').insert(0, 'secret')
  })
}

describe('the check document of a room', () => {
  it('carries nothing of a refused update', async () => {
    const room = await openRoom('Hello')
    const writer = new Writer(701, room.document)
    expect(await room.send(writer.write('Hello!'))).toBe('accepted')

    expect(await room.send(invented(room.document, 702))).toBe('invalid_schema')

    expect(await room.send(writer.write('Hello!!'))).toBe('accepted')
    expect(room.text()).toContain('Hello!!')
    expect(room.document.share.has('rogue')).toBe(false)
    expect(room.api.persisted).toHaveLength(2)
    expect(room.rebuilds()).toBe(1)
    await room.close()
  })

  it('carries nothing of an update the store refused', async () => {
    const room = await openRoom('Hello')
    const refused = new Writer(703, room.document).write('Hello refused')
    const persist = room.api.persistImplementation
    room.api.persistImplementation = async () => {
      throw new ApiRequestError(401, 'invalid_lease')
    }
    expect(await room.send(refused)).toBe('invalid_lease')
    // The store wrote nothing; the fake lists every attempt.
    room.api.persisted.pop()
    room.api.persistImplementation = persist

    expect(await room.send(new Writer(704, room.document).write('Hello!'))).toBe('accepted')
    expect(room.text()).toContain('Hello!')
    expect(room.text()).not.toContain('refused')
    await room.close()
  })

  it('does not outlive an update of unknown outcome', async () => {
    const room = await openRoom('Hello')
    const stored = Y.encodeStateAsUpdate(room.document)
    const persist = room.api.persistImplementation
    room.api.persistImplementation = async () => {
      throw new ApiRequestError(503, 'service_unavailable')
    }
    expect(await room.send(new Writer(705, room.document).write('Hello lost'))).not.toBe('accepted')
    expect(room.coordinator.requiresReconstruction(ROOM)).toBe(true)
    room.api.persistImplementation = persist

    room.reload(stored)
    expect(await room.send(new Writer(706, room.document).write('Hello!'))).toBe('accepted')
    expect(room.text()).toContain('Hello!')
    expect(room.text()).not.toContain('lost')
    await room.close()
  })

  it('checks the update after a decision against the decided state', async () => {
    const room = await openRoom('Hello')
    const writer = new Writer(707, room.document)
    expect(await room.send(writer.write('Hello!'))).toBe('accepted')

    expect(await room.decide((clone) => {
      const text = (clone.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0) as Y.XmlElement).get(0) as Y.XmlText
      text.insert(0, 'Decided ')
    })).toBe('decided')

    writer.receive(Y.encodeStateAsUpdate(room.document))
    expect(await room.send(writer.write('Decided Hello!!'))).toBe('accepted')
    expect(room.text()).toContain('Decided Hello!!')
    await expect(room.coordinator.captureDocument(ROOM, room.document)).resolves.toBeTruthy()
    await room.close()
  })

  it('checks the update after a replayed command against what the replay brought', async () => {
    const room = await openRoom('Hello')
    room.api.loadedState = await documentState(DOCUMENT_ID, room.document)
    const writer = new Writer(724, room.document)
    expect(await room.send(writer.write('Hello!'))).toBe('accepted')

    // Another instance committed this command; the store holds it, the room does not.
    const stored = new Writer(725, room.document).write('Hello! From the store')
    const command = 'abcdabcd-1111-4000-8000-00000000abcd'
    room.api.loadedState = {
      ...room.api.loadedState,
      persistedSequence: 2,
      updates: [
        ...room.api.loadedState.updates,
        { hash: hashBytes(stored), sequence: 2, update: stored },
      ],
    }
    room.api.commands.set(command, {
      actorKind: 'human',
      actorUserId: USER_ID,
      changeKind: 'decision',
      commandId: command,
      commandPayloadHash: command,
      decision: 'accept',
      generation: 1,
      patchIds: [],
      sequence: 2,
      suggestionIds: [],
      updateHash: hashBytes(stored),
    })
    expect(await room.decide(() => undefined, command)).toBe('decided')
    expect(room.text()).toContain('From the store')

    // The fake numbers the next row by this list.
    room.api.persisted.push(room.api.persisted[0]!)
    writer.receive(stored)
    expect(await room.send(writer.write('Hello! From the store!'))).toBe('accepted')
    expect(room.text()).toContain('From the store!')
    await room.close()
  })

  it('applies an update of deletions alone that the transport skipped, at once', async () => {
    const room = await openRoom('Hello world')
    const deletion = new Writer(708, room.document).write('Hello')
    expect(Y.decodeUpdate(deletion).structs).toHaveLength(0)

    expect(await room.send(deletion, { applied: null })).toBe('accepted')

    expect(room.text()).not.toContain('world')
    expect(room.coordinator.requiresReconstruction(ROOM)).toBe(false)
    expect(counter(
      room.metrics,
      'inqtrix_collaboration_committed_updates_applied_by_coordinator_total',
    )).toBe(1)
    await room.close()
  })

  it('finds a deletion that reached the room past the coordinator at the next update', async () => {
    const room = await openRoom('Hello world')
    const writer = new Writer(709, room.document)
    expect(await room.send(writer.write('Hello world!'))).toBe('accepted')

    Y.applyUpdate(room.document, new Writer(710, room.document).write('Hello'))

    expect(await room.send(writer.write('Hello world!!'))).toBe('internal_consistency')
    expect(room.logged).toContain('authoritative_state_mismatch')
    expect(room.coordinator.requiresReconstruction(ROOM)).toBe(true)
    await room.close()
  })

  it('keeps taking updates after a frame that named a foreign root and changed nothing', async () => {
    const room = await openRoom('Hello world')
    const [client] = [...room.document.store.clients.keys()]

    expect(await room.send(frameNamingAForeignRoot(client!), { allowNoop: true })).toBe('noop')

    expect(await room.send(new Writer(711, room.document).write('Hello world!'))).toBe('accepted')
    expect(room.coordinator.requiresReconstruction(ROOM)).toBe(false)
    await expect(room.coordinator.captureDocument(ROOM, room.document)).resolves.toBeTruthy()
    await room.close()
  })

  it('keeps the same document through a frame of deletions alone', async () => {
    const room = await openRoom('Hello world')
    const writer = new Writer(712, room.document)
    const deletion = writer.write('Hello')
    expect(await room.send(deletion)).toBe('accepted')
    expect(await room.send(writer.write('Hello!'))).toBe('accepted')
    const rebuilds = room.rebuilds()

    // A client answers the sync of its connection like this: what the room
    // already deleted, and nothing else.
    expect(await room.send(deletion, { allowNoop: true })).toBe('noop')

    expect(await room.send(writer.write('Hello!!'))).toBe('accepted')
    expect(room.rebuilds()).toBe(rebuilds)
    await room.close()
  })

  it('finds a frame that changed nothing and still split an emoji at the next update', async () => {
    const room = await openRoom('Hallo 😀 Welt')
    const { client, clock } = highSurrogateOf(room.document)
    const stored = room.text()
    const frame = frameReferringInto(client, Y.getState(room.document.store, client) - 1, clock)
    const writer = new Writer(713, room.document)

    expect(await room.send(frame, { allowNoop: true })).toBe('noop')
    expect(room.text()).not.toBe(stored)
    expect(room.api.persisted).toHaveLength(0)

    expect(await room.send(writer.write('Hallo 😀 Welt!'))).toBe('internal_consistency')
    expect(room.logged).toContain('authoritative_state_mismatch')
    expect(room.coordinator.requiresReconstruction(ROOM)).toBe(true)
    await room.close()
  })

  it('takes no snapshot of a room such a frame has split', async () => {
    const room = await openRoom('Hallo 😀 Welt')
    const { client, clock } = highSurrogateOf(room.document)
    const frame = frameReferringInto(client, Y.getState(room.document.store, client) - 1, clock)

    expect(await room.send(frame, { allowNoop: true })).toBe('noop')

    await expect(room.coordinator.captureDocument(ROOM, room.document))
      .rejects.toThrowError('internal_consistency')
    await room.close()
  })

  it('takes no snapshot when such a frame reached the room only after the next update', async () => {
    const room = await openRoom('Hallo 😀 Welt')
    const { client, clock } = highSurrogateOf(room.document)
    const frame = frameReferringInto(client, Y.getState(room.document.store, client) - 1, clock)
    const writer = new Writer(714, room.document)
    expect(await room.send(writer.write('Hallo 😀 Welt!'))).toBe('accepted')

    // The gate opens before the transport applies a frame that changed nothing.
    Y.applyUpdate(room.document, frame)

    await expect(room.coordinator.captureDocument(ROOM, room.document))
      .rejects.toThrowError('internal_consistency')
    await room.close()
  })

  it('finds other content under the same ids within the update interval', async () => {
    const room = await openRoom('Hello')
    const { applied, reported } = sameIdsOtherContent(room.document)
    const writer = new Writer(715, room.document)
    writer.receive(reported)

    expect(await room.send(reported, { applied })).toBe('accepted')

    let outcome = 'accepted'
    let updates = 0
    for (let index = 0; index < 40 && outcome === 'accepted'; index += 1) {
      updates += 1
      outcome = await room.send(writer.change((fragment) => {
        ((fragment.get(0) as Y.XmlElement).get(0) as Y.XmlText).insert(0, 'x')
      }))
    }
    expect(outcome).toBe('internal_consistency')
    expect(updates).toBeLessThanOrEqual(32)
    expect(room.logged).toContain('authoritative_state_mismatch')
    await room.close()
  })

  it('finds other content under the same ids before a snapshot', async () => {
    const room = await openRoom('Hello')
    const { applied, reported } = sameIdsOtherContent(room.document)
    expect(await room.send(reported, { applied })).toBe('accepted')

    await expect(room.coordinator.captureDocument(ROOM, room.document))
      .rejects.toThrowError('internal_consistency')
    await room.close()
  })

  it('finds other content under the same ids when it has to build a new document', async () => {
    const room = await openRoom('Hello')
    const { applied, reported } = sameIdsOtherContent(room.document)
    expect(await room.send(reported, { applied })).toBe('accepted')

    // A refused update gives the document up, and with it the comparison.
    expect(await room.send(invented(room.document, 716))).toBe('invalid_schema')

    expect(await room.send(new Writer(717, room.document).change((fragment) => {
      ((fragment.get(0) as Y.XmlElement).get(0) as Y.XmlText).insert(0, 'x')
    }))).toBe('internal_consistency')
    expect(room.logged).toContain('authoritative_state_mismatch')
    await room.close()
  })

  it('compares a room with what was validated when no document is left to compare with', async () => {
    const room = await openRoom('Hello')
    const { applied, reported } = sameIdsOtherContent(room.document)
    expect(await room.send(reported, { applied })).toBe('accepted')
    expect(await room.send(invented(room.document, 718))).toBe('invalid_schema')

    await expect(room.coordinator.captureDocument(ROOM, room.document))
      .rejects.toThrowError('internal_consistency')
    await room.close()
  })

  it('does not take a heading edit that leaves a room in another form for a mismatch', async () => {
    const room = await openRoom('# Title\n\nHello')
    const writer = new Writer(719, room.document)
    for (const target of [
      '# Title\n\nHello\n\nA',
      '## Title\n\nHello\n\nA',
      '## Title\n\nHello\n\nA\n\nB',
      '### Title\n\nHello\n\nA\n\nB',
      '### Title\n\nHello',
      'Hello',
      'Hello!',
      'Hello!!',
    ]) {
      expect(await room.send(writer.write(target))).toBe('accepted')
    }
    expect(room.logged).toEqual([])

    // The room's own form must not make an empty decision look like a change.
    expect(await room.decide(() => undefined)).toBe('decision_conflict')
    expect(await room.decide((clone) => {
      const text = (clone.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0) as Y.XmlElement).get(0) as Y.XmlText
      text.insert(0, 'Decided ')
    })).toBe('decided')
    writer.receive(Y.encodeStateAsUpdate(room.document))
    expect(await room.send(writer.write('Decided Hello!!!'))).toBe('accepted')
    await expect(room.coordinator.captureDocument(ROOM, room.document)).resolves.toBeTruthy()

    room.reload(Y.encodeStateAsUpdate(room.document))
    expect(await room.send(writer.write('Decided Hello!!!!'))).toBe('accepted')
    expect(room.logged).toEqual([])
    await room.close()
  })

  it('refuses what would grow past the limit, though the update itself is small', async () => {
    const characters = 110_000
    const room = await openRoom(Y.encodeStateAsUpdate(editorJsonToYDoc({
      content: [{ content: [{ text: 'x'.repeat(characters), type: 'text' }], type: 'paragraph' }],
      type: 'doc',
    })), { documentLimitBytes: 1024 * 1024 })
    const [client] = [...room.document.store.clients.keys()]
    const run = room.document.store.clients.get(client!)!
      .find((struct) => ((struct as Y.Item).content as { str?: string }).str !== undefined)!
    const frame = frameDeletingEveryOther(client!, run.id.clock + 1, Math.floor(characters / 2) - 1)
    expect(Y.encodeStateAsUpdate(room.document).byteLength + frame.byteLength)
      .toBeLessThan(room.configured.documentLimitBytes)

    expect(await room.send(frame)).toBe('document_too_large')
    expect(room.api.persisted).toHaveLength(0)
    await room.close()
  })

  it('never lets a room pass the limit while many small updates grow it', async () => {
    const room = await openRoom('Hello', { documentLimitBytes: 16 * 1024 })
    const writer = new Writer(720, room.document)
    let outcome = 'accepted'
    while (outcome === 'accepted') {
      outcome = await room.send(writer.change((fragment) => {
        const text = (fragment.get(0) as Y.XmlElement).get(0) as Y.XmlText
        text.insert(Math.floor(text.length / 2), 'some more words')
      }))
      expect(Y.encodeStateAsUpdate(room.document).byteLength)
        .toBeLessThanOrEqual(room.configured.documentLimitBytes)
    }
    expect(outcome).toBe('document_too_large')
    await room.close()
  })

  it.each([
    ['many clients with short runs', 200, 128],
    ['few clients with long runs', 20, 4_096],
  ])('bounds what an update can add to the encoded state: %s', (_label, clients, perClient) => {
    // Inserting in front gives every struct both origins, and low clocks the
    // shortest deletion ranges: the most an update's byte can add.
    const document = new Y.Doc()
    const text = document.getText('content')
    document.clientID = 4_000_000_000
    text.insert(0, 'E')
    const owners: number[] = []
    for (let index = 0; index < clients; index += 1) {
      document.clientID = 3_000_000_000 + index
      text.insert(0, 'x'.repeat(perClient))
      owners.push(document.clientID)
    }
    const before = Y.encodeStateAsUpdate(document)
    const ranges = Math.floor((perClient - 1) / 2)
    const frame = new Uint8Array([
      ...varUint(0),
      ...varUint(owners.length),
      ...owners.flatMap((owner) => {
        const entries: number[] = [...varUint(owner), ...varUint(ranges)]
        for (let index = 0; index < ranges; index += 1) {
          entries.push(...varUint(1 + index * 2), ...varUint(1))
        }
        return entries
      }),
    ])
    const after = new Y.Doc()
    Y.applyUpdate(after, before)
    Y.applyUpdate(after, frame)
    const growth = Y.encodeStateAsUpdate(after).byteLength - before.byteLength

    expect(growth).toBeGreaterThan(frame.byteLength)
    expect(growth).toBeLessThan(ENCODED_GROWTH_PER_UPDATE_BYTE * frame.byteLength)
    after.destroy()
    document.destroy()
  })

  it('copies and encodes nothing whole for an update while the room is in order', async () => {
    const room = await openRoom('Hello')
    const writer = new Writer(721, room.document)
    expect(await room.send(writer.write('Hello!'))).toBe('accepted')
    const copy = vi.spyOn(documentStateModule, 'cloneDocument')
    const full = vi.spyOn(documentStateModule, 'validateDocument')
    const content = vi.spyOn(documentStateModule, 'validateDocumentContent')
    try {
      expect(await room.send(writer.write('Hello!!'))).toBe('accepted')
      expect(await room.send(writer.write('Hello!!!'))).toBe('accepted')
      expect(copy).not.toHaveBeenCalled()
      expect(full).not.toHaveBeenCalled()
      // The schema round trip stays with every single update, and only its
      // causal check, whose question the pending state has already answered, goes.
      expect(content).toHaveBeenCalledTimes(2)
      expect(content).toHaveBeenLastCalledWith(expect.any(Y.Doc), { causallyComplete: true })
    } finally {
      copy.mockRestore()
      full.mockRestore()
      content.mockRestore()
    }
    await room.close()
  })

  it('leaves no old document behind when a room is loaded again', async () => {
    const room = await openRoom('Hello')
    const stored = Y.encodeStateAsUpdate(room.document)
    const writer = new Writer(722, room.document)
    expect(await room.send(writer.write('Hello there'))).toBe('accepted')

    // Another operation holds the gate, so unloading keeps the room's state.
    let endOperation = (): void => undefined
    room.api.lookupCommand = () => new Promise((_resolve, reject) => {
      endOperation = () => reject(new ApiRequestError(503, 'service_unavailable'))
    })
    const operation = room.decide(() => undefined)
    await new Promise((resolve) => setTimeout(resolve, 0))

    // The new room document does not hold that update; the old check document does.
    room.reload(stored)
    endOperation()
    await operation

    const other = new Writer(723, room.document)
    expect(await room.send(other.write('Hello!'))).toBe('accepted')
    expect(room.text()).toContain('Hello!')
    expect(room.text()).not.toContain('there')
    await room.close()
  })
})

describe('sameEncodedState', () => {
  it('holds two forms of one state the same, and other content not', () => {
    const room = editorJsonToYDoc(parseEditorMarkdown('# Title\n\nHello'))
    const writer = new Writer(731, room)
    for (const target of [
      '# Title\n\nHello\n\nA',
      '## Title\n\nHello\n\nA',
      '## Title\n\nHello\n\nA\n\nB',
      '### Title\n\nHello\n\nA\n\nB',
      '### Title\n\nHello',
      'Hello',
    ]) Y.applyUpdate(room, writer.write(target))
    const fresh = new Y.Doc()
    Y.applyUpdate(fresh, Y.encodeStateAsUpdate(room))

    const roomBytes = Y.encodeStateAsUpdate(room)
    const freshBytes = Y.encodeStateAsUpdate(fresh)
    expect(Buffer.compare(roomBytes, freshBytes)).not.toBe(0)
    expect(sameEncodedState(roomBytes, freshBytes)).toBe(true)

    Y.applyUpdate(fresh, writer.write('Hell'))
    expect(sameEncodedState(roomBytes, Y.encodeStateAsUpdate(fresh))).toBe(false)
    room.destroy()
    fresh.destroy()
  })
})

// The decision on an update must not depend on how long the check document has
// lived. So one room keeps its document, and the other starts each update from
// a fresh copy of the room, as every update once did; every step goes to both.
describe('a long-lived check document against a fresh one for every update', () => {
  const start = Y.encodeStateAsUpdate(editorJsonToYDoc(parseEditorMarkdown(
    '# Title\n\nFirst paragraph with **bold** words\n\nSecond paragraph\n\n- one\n- two',
  )))

  it.each([11, 23, 37, 41, 59, 67])('decides the same for every update, seed %i', async (seed) => {
    const lived = await openRoom(start)
    const fresh = await openRoom(start)
    let state = seed
    const random = (limit: number): number => {
      state = (state * 1_664_525 + 1_013_904_223) % 4_294_967_296
      return Math.floor((state / 4_294_967_296) * limit)
    }
    let nextClient = 8_000 + seed * 100
    const writers = [0, 1, 2].map(() => new Writer(nextClient++, start))
    const sent: Uint8Array[] = []
    const textOf = (writer: Writer): Y.XmlText | null => {
      const fragment = writer.doc.getXmlFragment(EDITOR_YJS_FRAGMENT)
      const block = fragment.get(random(Math.max(1, fragment.length))) as Y.XmlElement | undefined
      const text = block?.get(0)
      return text instanceof Y.XmlText ? text : null
    }
    const outcomes = new Map<string, number>()
    for (let step = 0; step < 220; step += 1) {
      const writer = writers[random(writers.length)]!
      // Writers lag behind the room, so updates meet what they have not seen.
      if (random(3) === 0) writer.receive(Y.encodeStateAsUpdate(lived.document))
      const kind = random(100)
      let update: Uint8Array
      let throwaway = false
      if (kind < 40) {
        update = writer.change(() => {
          const text = textOf(writer)
          text?.insert(random(text.length + 1), 'abc 😀'.slice(0, 1 + random(5)))
        })
      } else if (kind < 55) {
        update = writer.change(() => {
          const text = textOf(writer)
          if (text && text.length > 0) {
            const at = random(text.length)
            text.delete(at, Math.min(1 + random(4), text.length - at))
          }
        })
      } else if (kind < 65) {
        update = writer.change(() => {
          const text = textOf(writer)
          if (text && text.length > 1) {
            const at = random(text.length - 1)
            text.format(at, 1, { bold: random(2) === 0 ? {} : null })
          }
        })
      } else if (kind < 72) {
        update = writer.change((fragment) => {
          const block = fragment.get(0) as Y.XmlElement | undefined
          if (block?.nodeName === 'heading') block.setAttribute('level', String(1 + random(3)))
        })
      } else if (kind < 80) {
        update = writer.change((fragment) => {
          const block = new Y.XmlElement('paragraph')
          block.insert(0, [new Y.XmlText('new block')])
          fragment.insert(random(fragment.length + 1), [block])
        })
      } else if (kind < 85) {
        update = writer.change((fragment) => {
          if (fragment.length > 2) fragment.delete(random(fragment.length), 1)
        })
      } else if (kind < 90 && sent.length > 0) {
        // The same frame again, as a client resends after a reconnect.
        update = sent[random(sent.length)]!
      } else if (kind < 93) {
        update = invented(lived.document, nextClient++)
        throwaway = true
      } else if (kind < 95) {
        // Format items directly under the root: no editor writes them, and a
        // document built anew reads such a root differently than one that took them.
        const first = lived.document.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0) as Y.XmlElement
        const client = nextClient++
        update = new Uint8Array([
          ...varUint(1), ...varUint(2), ...varUint(client), ...varUint(0),
          6 | 0x80, ...varUint(first._item!.id.client), ...varUint(first._item!.id.clock),
          ...varString('bold'), ...varString('{}'),
          6 | 0x80, ...varUint(client), ...varUint(0),
          ...varString('bold'), ...varString('{}'),
          ...varUint(0),
        ])
        throwaway = true
      } else {
        // The later of two updates alone: a causal gap.
        const replica = new Writer(nextClient++, lived.document)
        replica.change(() => {
          const text = textOf(replica)
          text?.insert(0, 'y')
        })
        update = replica.change(() => {
          const text = textOf(replica)
          text?.insert(0, 'z')
        })
        throwaway = true
      }
      if (update.byteLength <= 2) continue
      // The fresh room gets what a fresh copy per update means: its document
      // given up and built again from the room before this update.
      const sequence = fresh.coordinator.getPersistedSequence(ROOM)
      fresh.coordinator.markUnloaded(ROOM)
      fresh.coordinator.initialize(ROOM, sequence, { bytes: 0, updates: 0 })

      const allowNoop = random(2) === 0
      const [inLived, inFresh] = await Promise.all([
        lived.send(update, { allowNoop }),
        fresh.send(update, { allowNoop }),
      ])
      expect(`${step} ${inLived}`).toBe(`${step} ${inFresh}`)
      expect(inLived).not.toBe('internal_consistency')
      expect(sameEncodedState(
        Y.encodeStateAsUpdate(lived.document),
        Y.encodeStateAsUpdate(fresh.document),
      )).toBe(true)
      outcomes.set(inLived, (outcomes.get(inLived) ?? 0) + 1)
      if (inLived === 'accepted' && !throwaway) sent.push(update)
      if (inLived !== 'accepted' && inLived !== 'noop' && !throwaway) {
        // This writer's replica now holds what the room refused.
        const index = writers.indexOf(writer)
        writers[index] = new Writer(nextClient++, lived.document)
      }
    }
    // Every case actually occurred, or this compared less than it claims.
    expect(outcomes.get('accepted') ?? 0).toBeGreaterThan(50)
    expect(outcomes.get('invalid_schema') ?? 0).toBeGreaterThan(0)
    expect(outcomes.get('causal_gap') ?? 0).toBeGreaterThan(0)
    expect(lived.rebuilds()).toBeGreaterThan(0)
    await lived.close()
    await fresh.close()
  }, 60_000)
})
