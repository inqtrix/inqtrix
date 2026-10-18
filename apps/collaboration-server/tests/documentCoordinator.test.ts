import { getSchema } from '@tiptap/core'
import { EditorState } from '@tiptap/pm/state'
import { initProseMirrorDoc, updateYFragment } from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  INQTRIX_STRUCTURE_COMMAND_META,
  createEditorSchemaExtensions,
  editorCollaborationRoom,
  editorJsonToYDoc,
  editorYDocToJson,
  getEditorSchemaFingerprint,
  parseEditorMarkdown,
  serializeEditorJson,
  transformToInqtrixSuggestionTransaction,
  validateEditorYDoc,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import type { ConnectionContext, SidecarLogger } from '../src/contracts'
import { DocumentCoordinator } from '../src/documentCoordinator'
import { ApiRequestError, CloseCodes, CollaborationError } from '../src/errors'
import * as documentStateModule from '../src/documentState'
import {
  hashBytes,
  reconstructDocument,
  validateDocument,
} from '../src/documentState'
import { InstanceLeaseManager } from '../src/instanceLease'
import { SidecarMetrics } from '../src/metrics'
import {
  FakeCollaborationApi,
  USER_ID,
  deferred,
  documentState,
  settings,
  silentLogger,
} from './helpers'

const DOCUMENT_ID = 'ed_test'
const ROOM = editorCollaborationRoom(DOCUMENT_ID, 1)
const PATCH_ID = '22222222-2222-4222-8222-222222222222'
const SUGGESTION_ID = '33333333-3333-4333-8333-333333333333'
const EXISTING_PATCH_ID = '55555555-5555-4555-8555-555555555555'
const EXISTING_SUGGESTION_ID = '66666666-6666-4666-8666-666666666666'
const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

describe('document coordinator latency instrumentation', () => {
  it('records how long an update waited for the room gate', async () => {
    // The serial room gate is what turns five near-simultaneous writers
    // into a queue, so it is the number that explains the p95. Only the
    // queue DEPTH gauge existed, which says how many are waiting but
    // never how long any of them waited.
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const update = updateFor(document, 'Hello!')

    await fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'connection-1',
      context: fixture.context,
      document,
      room: ROOM,
      update,
    })
    Y.applyUpdate(document, update)
    fixture.coordinator.finishClientUpdate('connection-1', document)

    const rendered = fixture.metrics.render()
    expect(rendered).toContain('inqtrix_collaboration_gate_wait_seconds_count 1')
    document.destroy()
    await fixture.close()
  })

  it('measures the acknowledged latency from gate entry, not from after persistence', async () => {
    // `durable_ack_seconds` starts its clock once persistence already
    // returned, so it reports only apply+verify. A user waits for the
    // whole chain; a metric that hides the expensive part of it cannot
    // show an improvement or a regression in that part.
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const update = updateFor(document, 'Hello!')
    const persistence = deferred<{ duplicate: boolean; persistedSequence: number; sequence: number }>()
    fixture.api.persistImplementation = () => persistence.promise

    const preparing = fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'connection-1',
      context: fixture.context,
      document,
      room: ROOM,
      update,
    })
    await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))
    await new Promise((resolve) => setTimeout(resolve, 30))
    persistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
    await preparing
    Y.applyUpdate(document, update)
    fixture.coordinator.finishClientUpdate('connection-1', document)

    const rendered = fixture.metrics.render()
    const totalSum = Number(
      rendered
        .split('\n')
        .find((line) => line.startsWith('inqtrix_collaboration_update_total_seconds_sum'))
        ?.split(' ')
        .at(-1),
    )
    const ackSum = Number(
      rendered
        .split('\n')
        .find((line) => line.startsWith('inqtrix_collaboration_durable_ack_seconds_sum'))
        ?.split(' ')
        .at(-1),
    )
    // The deliberate 30ms persistence stall must land inside the
    // end-to-end measurement and outside the old one.
    expect(totalSum).toBeGreaterThan(0.02)
    expect(totalSum).toBeGreaterThan(ackSum)
    document.destroy()
    await fixture.close()
  })
})

describe('concurrent structure changes next to suggestions', () => {
  // Two writers work on the same state without seeing each other. The room
  // judges the second update after the first one merged, so what it refuses
  // must follow from that update, not from the merge.
  const OTHER_USER_ID = '77777777-7777-4777-8777-777777777777'
  const FOREIGN = { patchId: 'bbbbbbbb-0000-4000-8000-0000000000f1', suggestionId: 'bbbbbbbb-0000-4000-8000-000000000001' }
  const SUGGESTED = { patchId: 'dddddddd-0000-4000-8000-0000000000f1', suggestionId: 'dddddddd-0000-4000-8000-000000000001' }

  /** A change the editor writes through y-tiptap on the given state, in edit
   * mode or, with `suggestion`, transformed into a suggestion. */
  function editorUpdateOn(
    base: Uint8Array,
    clientId: number,
    build: (state: EditorState) => ReturnType<EditorState['tr']['split']>,
    suggestion?: { authorId: string; patchId: string; suggestionId: string },
  ): Uint8Array {
    const replica = new Y.Doc()
    replica.clientID = clientId
    Y.applyUpdate(replica, base)
    const fragment = replica.getXmlFragment(EDITOR_YJS_FRAGMENT)
    const initialized = initProseMirrorDoc(fragment, schema)
    const state = EditorState.create({ doc: initialized.doc, schema })
    const transaction = build(state)
    const target = suggestion
      ? transformToInqtrixSuggestionTransaction(
          transaction,
          state,
          { authorId: suggestion.authorId, createdAt: 1_784_112_000, patchId: suggestion.patchId },
          () => suggestion.suggestionId,
        ).doc
      : transaction.doc
    const vector = Y.encodeStateVector(replica)
    replica.transact(() => updateYFragment(replica, fragment, target, initialized.meta))
    const update = Y.encodeStateAsUpdate(replica, vector)
    replica.destroy()
    return update
  }

  const at = (state: EditorState, needle: string): number => {
    let found = -1
    state.doc.descendants((node, position) => {
      if (found >= 0) return false
      if (node.isText && node.text?.includes(needle)) found = position + node.text.indexOf(needle)
      return true
    })
    if (found < 0) throw new Error(`fixture has no text ${needle}`)
    return found
  }
  const blockOf = (state: EditorState, needle: string) => {
    const $position = state.doc.resolve(at(state, needle))
    return { from: $position.before($position.depth), to: $position.after($position.depth) }
  }

  async function submit(
    fixture: Awaited<ReturnType<typeof coordinatorFixture>>,
    room: Y.Doc,
    connectionId: string,
    update: Uint8Array,
    access: 'edit' | 'suggest',
    userId = USER_ID,
  ): Promise<string> {
    try {
      const result = await fixture.coordinator.prepareClientUpdate({
        allowNoop: false,
        connectionId,
        context: { ...fixture.context, access, user: { ...fixture.context.user, id: userId } },
        document: room,
        room: ROOM,
        update,
      })
      if (result !== 'pending') return result
      Y.applyUpdate(room, update)
      fixture.coordinator.finishClientUpdate(connectionId, room)
      return `accepted ${fixture.api.persisted.at(-1)?.changeKind ?? 'unknown'}`
    } catch (error) {
      if (error instanceof CollaborationError) return `${error.code} ${error.reason}`
      throw error
    }
  }

  function replay(base: Uint8Array, rows: readonly { update: Uint8Array }[]): Y.Doc {
    const replayed = new Y.Doc()
    Y.applyUpdate(replayed, base)
    for (const row of rows) Y.applyUpdate(replayed, row.update)
    return replayed
  }

  it('accepts an edit-mode split before another person\'s pending insertion, and the store reproduces the room', async () => {
    const fixture = await coordinatorFixture()
    const base = Y.encodeStateAsUpdate(editorJsonToYDoc({
      type: 'doc',
      content: [{
        type: 'paragraph',
        content: [
          { type: 'text', text: 'Alpha paragraph' },
          {
            type: 'text',
            text: ' inserted',
            marks: [{
              type: 'insertion',
              attrs: { authorId: OTHER_USER_ID, createdAt: 1_784_112_000, id: FOREIGN.suggestionId, kind: 'insertion', ...FOREIGN },
            }],
          },
          { type: 'text', text: ' tail' },
        ],
      }],
    }))
    const room = new Y.Doc()
    Y.applyUpdate(room, base)
    const split = editorUpdateOn(base, 101, (state) => state.tr.split(at(state, 'paragraph')))

    expect(await submit(fixture, room, 'writer', split, 'edit')).toBe('accepted direct')
    const replayed = replay(base, fixture.api.persisted)
    expect(Y.encodeStateAsUpdate(replayed)).toEqual(Y.encodeStateAsUpdate(room))
    expect(markdown(room)).toBe('Alpha \n\nparagraph inserted tail')
    replayed.destroy()
    room.destroy()
    await fixture.close()
  })

  it.each([
    ['deleted', (state: EditorState) => {
      const block = blockOf(state, 'Second')
      return state.tr.delete(block.from, block.to)
    }],
    ['turned into a heading', (state: EditorState) => {
      const block = blockOf(state, 'Second')
      return state.tr.setBlockType(block.from + 1, block.from + 1, schema.nodes.heading!, { level: 2 })
    }],
  ] as const)(
    'accepts a suggestion that lands in a paragraph another writer %s at the same time, as nothing visible',
    async (_label, structureChange) => {
      const fixture = await coordinatorFixture()
      const base = Y.encodeStateAsUpdate(editorJsonToYDoc(parseEditorMarkdown('First paragraph\n\nSecond paragraph')))
      const room = new Y.Doc()
      Y.applyUpdate(room, base)
      const direct = editorUpdateOn(base, 101, structureChange)
      const suggested = editorUpdateOn(
        base,
        102,
        (state) => state.tr.insertText(' proposed', at(state, 'Second paragraph') + 'Second paragraph'.length),
        { authorId: OTHER_USER_ID, ...SUGGESTED },
      )

      expect(await submit(fixture, room, 'editor', direct, 'edit')).toBe('accepted direct')
      const before = markdown(room)
      expect(await submit(fixture, room, 'suggester', suggested, 'suggest', OTHER_USER_ID))
        .toBe('accepted suggestion')

      const row = fixture.api.persisted.at(-1)
      expect(row).toMatchObject({ changeKind: 'suggestion', patches: [], suggestionIds: [], suggestions: [] })
      expect(markdown(room)).toBe(before)
      const replayed = replay(base, fixture.api.persisted)
      expect(Y.encodeStateAsUpdate(replayed)).toEqual(Y.encodeStateAsUpdate(room))
      expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(false)
      replayed.destroy()
      room.destroy()
      await fixture.close()
    },
  )

  it.each([
    ['writes an original character again unmarked', (fragment: Y.XmlFragment) => {
      const text = (fragment.get(0) as Y.XmlElement).get(0) as Y.XmlText
      const character = text.toString().charAt(2)
      text.delete(2, 1)
      text.insert(2, character, {})
    }],
    ['inserts unmarked text', (fragment: Y.XmlFragment) => {
      const text = (fragment.get(0) as Y.XmlElement).get(0) as Y.XmlText
      text.insert(2, 'x', {})
    }],
  ] as const)('still refuses a suggest-only update that %s', async (_label, mutate) => {
    const fixture = await coordinatorFixture()
    const base = Y.encodeStateAsUpdate(editorJsonToYDoc(parseEditorMarkdown('First paragraph')))
    const room = new Y.Doc()
    Y.applyUpdate(room, base)
    const replica = new Y.Doc()
    replica.clientID = 103
    Y.applyUpdate(replica, base)
    const vector = Y.encodeStateVector(replica)
    replica.transact(() => mutate(replica.getXmlFragment(EDITOR_YJS_FRAGMENT)))
    const update = Y.encodeStateAsUpdate(replica, vector)
    replica.destroy()

    expect(await submit(fixture, room, 'suggester', update, 'suggest')).toBe('4403 suggestion_policy_violation')
    expect(fixture.api.persisted).toHaveLength(0)
    room.destroy()
    await fixture.close()
  })

  it('still refuses a suggest-only update that changes only a block its own pending suggestion proposes', async () => {
    const fixture = await coordinatorFixture()
    const initial = Y.encodeStateAsUpdate(editorJsonToYDoc(parseEditorMarkdown('First paragraph')))
    // Enter at the end of the paragraph in suggest mode, then a word: the
    // new block exists only as part of the pending suggestion.
    const proposal = editorUpdateOn(initial, 104, (state) => {
      const end = at(state, 'First paragraph') + 'First paragraph'.length
      return state.tr.split(end).insertText('Proposed', end + 2)
    }, { authorId: USER_ID, ...SUGGESTED })
    const room = new Y.Doc()
    Y.applyUpdate(room, initial)
    expect(await submit(fixture, room, 'proposer', proposal, 'suggest')).toBe('accepted suggestion')
    const base = Y.encodeStateAsUpdate(room)
    const replica = new Y.Doc()
    replica.clientID = 105
    Y.applyUpdate(replica, base)
    const vector = Y.encodeStateVector(replica)
    replica.transact(() => {
      const proposed = replica.getXmlFragment(EDITOR_YJS_FRAGMENT).get(1) as Y.XmlElement
      proposed.setAttribute('textAlign', 'center')
    })
    const realign = Y.encodeStateAsUpdate(replica, vector)
    replica.destroy()

    expect(await submit(fixture, room, 'realigner', realign, 'suggest')).toBe('4403 suggestion_policy_violation')
    expect(fixture.api.persisted).toHaveLength(1)
    room.destroy()
    await fixture.close()
  })
})

describe('document coordinator', () => {
  it('persists an update before the authoritative document is applied and acknowledged', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const update = updateFor(document, 'Hello!')
    const persistence = deferred<{ duplicate: boolean; persistedSequence: number; sequence: number }>()
    fixture.api.persistImplementation = () => persistence.promise

    const preparing = fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'connection-1',
      context: fixture.context,
      document,
      room: ROOM,
      update,
    })
    await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))
    expect(markdown(document)).toBe('Hello')

    persistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
    await expect(preparing).resolves.toBe('pending')
    expect(markdown(document)).toBe('Hello')
    expect(fixture.coordinator.getPersistedSequence(ROOM)).toBe(0)
    expect(fixture.coordinator.shouldSnapshot(ROOM)).toBe(false)
    expect(() => fixture.coordinator.assertJoinAllowed(ROOM)).toThrowError('restarting')
    Y.applyUpdate(document, update)
    expect(fixture.coordinator.finishClientUpdate('connection-1', document)).toEqual({
      hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      sequence: 1,
      type: 'durable_ack',
    })
    expect(fixture.coordinator.getPersistedSequence(ROOM)).toBe(1)
    expect(fixture.api.persisted[0]).toMatchObject({
      changeKind: 'direct',
      decision: null,
      patches: [],
      suggestions: [],
    })
    expect(fixture.api.persisted[0]?.update).toBe(update)
    await fixture.close()
  })

  it('keeps the per-document gate held until authoritative apply completes', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('A'))
    const first = updateFor(document, 'AB')
    const replicaAfterFirst = clone(document)
    Y.applyUpdate(replicaAfterFirst, first)
    const second = updateFor(replicaAfterFirst, 'ABC')
    replicaAfterFirst.destroy()
    const firstPersistence = deferred<{
      duplicate: boolean
      persistedSequence: number
      sequence: number
    }>()
    fixture.api.persistImplementation = async () => {
      if (fixture.api.persisted.length === 1) return firstPersistence.promise
      return { duplicate: false, persistedSequence: 2, sequence: 2 }
    }

    const firstPreparing = fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'connection-1',
      context: fixture.context,
      document,
      room: ROOM,
      update: first,
    })
    await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))
    const secondPreparing = fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'connection-2',
      context: fixture.context,
      document,
      room: ROOM,
      update: second,
    })
    await new Promise((resolve) => setTimeout(resolve, 10))
    expect(fixture.api.persisted).toHaveLength(1)

    firstPersistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
    await firstPreparing
    Y.applyUpdate(document, first)
    fixture.coordinator.finishClientUpdate('connection-1', document)
    await expect(secondPreparing).resolves.toBe('pending')
    expect(fixture.api.persisted).toHaveLength(2)
    Y.applyUpdate(document, second)
    fixture.coordinator.finishClientUpdate('connection-2', document)
    expect(markdown(document)).toBe('ABC')
    await fixture.close()
  })

  it('fully validates only the candidate after authoritative state is established', async () => {
    const validation = vi.spyOn(documentStateModule, 'validateDocument')
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('A'))
    try {
      const first = updateFor(document, 'AB')
      await expect(fixture.coordinator.prepareClientUpdate({
        allowNoop: false,
        connectionId: 'cache-first',
        context: fixture.context,
        document,
        room: ROOM,
        update: first,
      })).resolves.toBe('pending')
      Y.applyUpdate(document, first)
      expect(fixture.coordinator.finishClientUpdate('cache-first', document)).toMatchObject({
        sequence: 1,
        type: 'durable_ack',
      })

      validation.mockClear()
      const second = updateFor(document, 'ABC')
      await expect(fixture.coordinator.prepareClientUpdate({
        allowNoop: false,
        connectionId: 'cache-second',
        context: fixture.context,
        document,
        room: ROOM,
        update: second,
      })).resolves.toBe('pending')
      Y.applyUpdate(document, second)
      expect(fixture.coordinator.finishClientUpdate('cache-second', document)).toMatchObject({
        sequence: 2,
        type: 'durable_ack',
      })
      expect(validation).toHaveBeenCalledTimes(1)
    } finally {
      validation.mockRestore()
      fixture.coordinator.abortClientUpdate('cache-first')
      fixture.coordinator.abortClientUpdate('cache-second')
      document.destroy()
      await fixture.close()
    }
  })

  it('requires reconstruction when authoritative content changes without its sequence', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('A'))
    try {
      const first = updateFor(document, 'AB')
      await expect(fixture.coordinator.prepareClientUpdate({
        allowNoop: false,
        connectionId: 'authoritative-first',
        context: fixture.context,
        document,
        room: ROOM,
        update: first,
      })).resolves.toBe('pending')
      Y.applyUpdate(document, first)
      fixture.coordinator.finishClientUpdate('authoritative-first', document)

      Y.applyUpdate(document, updateFor(document, 'Out of band'))
      await expect(fixture.coordinator.prepareClientUpdate({
        allowNoop: false,
        connectionId: 'authoritative-stale',
        context: fixture.context,
        document,
        room: ROOM,
        update: updateFor(document, 'Out of band!'),
      })).rejects.toThrowError('internal_consistency')
      expect(fixture.api.persisted).toHaveLength(1)
      expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(true)
    } finally {
      fixture.coordinator.abortClientUpdate('authoritative-first')
      fixture.coordinator.abortClientUpdate('authoritative-stale')
      document.destroy()
      await fixture.close()
    }
  })

  it('rejects an initialized validation that does not match the authoritative document', async () => {
    const expected = editorJsonToYDoc(parseEditorMarkdown('Expected'))
    const document = editorJsonToYDoc(parseEditorMarkdown('Different'))
    const fixture = await coordinatorFixture(
      0,
      {},
      validateDocument(expected, settings().documentLimitBytes),
    )
    try {
      await expect(fixture.coordinator.prepareClientUpdate({
        allowNoop: false,
        connectionId: 'initialized-mismatch',
        context: fixture.context,
        document,
        room: ROOM,
        update: updateFor(document, 'Different!'),
      })).rejects.toThrowError('internal_consistency')
      expect(fixture.api.persisted).toHaveLength(0)
      expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(true)
    } finally {
      fixture.coordinator.abortClientUpdate('initialized-mismatch')
      document.destroy()
      expected.destroy()
      await fixture.close()
    }
  })

  it('releases an abandoned persisted pending update and forces an authoritative reload', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const update = updateFor(document, 'Hello!')

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'abandoned-connection',
      context: fixture.context,
      document,
      room: ROOM,
      update,
    })).resolves.toBe('pending')

    fixture.coordinator.abortClientUpdate('abandoned-connection')
    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(true)
    expect(fixture.coordinator.shouldSnapshot(ROOM)).toBe(false)
    expect(() => fixture.coordinator.assertJoinAllowed(ROOM)).toThrowError('restarting')
    await expect(fixture.coordinator.awaitRoom(ROOM)).resolves.toBeUndefined()
    expect(fixture.coordinator.finishClientUpdate('abandoned-connection', document)).toBeNull()
    await expect(fixture.coordinator.captureDocument(ROOM, document)).rejects.toThrowError(
      'internal_consistency',
    )
    fixture.coordinator.markUnloaded(ROOM)
    expect(() => fixture.coordinator.assertJoinAllowed(ROOM)).not.toThrow()
    document.destroy()
    await fixture.close()
  })

  it('refuses updates queued behind a persist with an unknown outcome as restarting', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const persistence = deferred<{
      duplicate: boolean
      persistedSequence: number
      sequence: number
    }>()
    fixture.api.persistImplementation = () => persistence.promise

    try {
      const initiating = fixture.coordinator.prepareClientUpdate({
        allowNoop: false,
        connectionId: 'unknown-outcome-writer',
        context: fixture.context,
        document,
        room: ROOM,
        update: updateFor(document, 'Hello from writer 1'),
      })
      await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))

      const waiting = [2, 3, 4, 5].map((writer) => (
        fixture.coordinator.prepareClientUpdate({
          allowNoop: false,
          connectionId: `waiting-writer-${writer}`,
          context: fixture.context,
          document,
          room: ROOM,
          update: updateFor(document, `Hello from writer ${writer}`),
        })
      ))
      await new Promise((resolve) => setTimeout(resolve, 10))
      expect(fixture.api.persisted).toHaveLength(1)

      // The lookup answers "not found", which does not prove that nothing
      // was written, so the room is rebuilt from the store.
      persistence.reject(new ApiRequestError(503, 'internal_api_timeout'))

      const [initiatingOutcome, ...waitingOutcomes] = await Promise.allSettled([
        initiating,
        ...waiting,
      ])
      expect(initiatingOutcome).toMatchObject({
        reason: {
          code: 4503,
          reason: 'service_unavailable',
        },
        status: 'rejected',
      })
      for (const outcome of waitingOutcomes) {
        expect(outcome).toMatchObject({
          reason: {
            code: CloseCodes.restarting,
            reason: 'restarting',
          },
          status: 'rejected',
        })
      }
      expect(fixture.api.persisted).toHaveLength(1)
      expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_rejections_total{reason="restarting"} 4',
      )
      expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(true)
      expect(() => fixture.coordinator.assertJoinAllowed(ROOM)).toThrowError('restarting')
    } finally {
      document.destroy()
      await fixture.close()
    }
  })

  it('keeps snapshots and fast joins off until a room with an unknown persist outcome is rebuilt', async () => {
    const fixture = await coordinatorFixture(0, { snapshotMaxUpdates: 1 })
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    fixture.api.loadedState = await documentState(DOCUMENT_ID, document)
    const update = updateFor(document, 'Hello!')
    // The write commits, its answer is lost and the lookup does not find it.
    fixture.api.persistResponseErrorAfterCommit = new ApiRequestError(
      503,
      'internal_api_timeout',
    )

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'unknown-outcome-before-apply',
      context: fixture.context,
      document,
      room: ROOM,
      update,
    })).rejects.toMatchObject({ code: 4503, reason: 'service_unavailable' })
    expect(fixture.coordinator.shouldSnapshot(ROOM)).toBe(false)
    expect(() => fixture.coordinator.assertJoinAllowed(ROOM)).toThrowError('restarting')
    await fixture.coordinator.awaitRoom(ROOM)
    expect(fixture.coordinator.shouldSnapshot(ROOM)).toBe(false)

    fixture.coordinator.markUnloaded(ROOM)
    expect(() => fixture.coordinator.assertJoinAllowed(ROOM)).not.toThrow()
    const loaded = await fixture.api.loadDocumentState()
    const reconstructed = await reconstructDocument(loaded, {
      documentId: DOCUMENT_ID,
      generation: 1,
      schemaVersion: fixture.context.schemaVersion,
    }, settings().documentLimitBytes)
    fixture.coordinator.initialize(ROOM, loaded.persistedSequence, {
      bytes: loaded.updates.reduce((total, item) => total + item.update.byteLength, 0),
      updates: loaded.updates.length,
    })
    expect(markdown(reconstructed)).toBe('Hello!')
    expect(fixture.coordinator.getPersistedSequence(ROOM)).toBe(1)
    expect(fixture.coordinator.shouldSnapshot(ROOM)).toBe(true)

    reconstructed.destroy()
    document.destroy()
    await fixture.close()
  })

  it('keeps the room usable when the API rejects an update deterministically', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const update = updateFor(document, 'Hello!')
    fixture.api.persistImplementation = async () => {
      throw new ApiRequestError(401, 'invalid_lease')
    }

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'rejected',
      context: fixture.context,
      document,
      room: ROOM,
      update,
    })).rejects.toThrowError('invalid_lease')

    // A 4xx is a decision, not an outage: the transaction rolled back, so the
    // room still matches the store. Tearing it down here would discard state
    // that every other participant in the room is already showing.
    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(false)
    expect(() => fixture.coordinator.assertJoinAllowed(ROOM)).not.toThrow()
    // A decision needs no outcome lookup: nothing was written.
    expect(fixture.api.lookups).toHaveLength(0)

    document.destroy()
    await fixture.close()
  })

  it('still requires reload when persistence fails with an unknown outcome', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const update = updateFor(document, 'Hello!')
    fixture.api.persistImplementation = async () => {
      throw new ApiRequestError(503, 'service_unavailable')
    }

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'unknown-outcome',
      context: fixture.context,
      document,
      room: ROOM,
      update,
    })).rejects.toThrowError()

    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(true)

    document.destroy()
    await fixture.close()
  })

  it('accepts a sync response whose payload is partly already known', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    // The room already holds the first edit; the browser answering the initial
    // sync sends BOTH its edits, so part of the payload is redundant and the
    // applied delta is smaller than the bytes sent.
    const first = updateFor(document, 'Hello!')
    Y.applyUpdate(document, first)
    const second = updateFor(document, 'Hello!!')
    const syncResponse = Y.mergeUpdates([first, second])

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: true,
      connectionId: 'sync-response',
      context: fixture.context,
      document,
      room: ROOM,
      update: syncResponse,
    })).resolves.toBe('pending')

    document.destroy()
    await fixture.close()
  })

  it('admits a partly-known ordinary frame, the honest concurrent shape', async () => {
    // The counterpart to the sync-response case above. Two editors working at
    // once routinely produce a frame carrying a struct the room already holds.
    // CRDT integration makes that harmless, and refusing it cost one of the
    // two their whole session.
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const first = updateFor(document, 'Hello!')
    Y.applyUpdate(document, first)
    const second = updateFor(document, 'Hello!!')

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'ordinary-frame',
      context: fixture.context,
      document,
      room: ROOM,
      update: Y.mergeUpdates([first, second]),
    })).resolves.toBe('pending')

    document.destroy()
    await fixture.close()
  })

  it('requires reload when persistence may commit before returning an error', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const update = updateFor(document, 'Hello!')
    fixture.api.persistResponseErrorAfterCommit = new Error('response lost')

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'lost-response',
      context: fixture.context,
      document,
      room: ROOM,
      update,
    })).rejects.toThrowError('internal_consistency')

    // The lookup can only confirm a write. "Not found" does not prove the
    // write will never land, so the room is still rebuilt from the store.
    expect(fixture.api.lookups).toEqual([{
      documentId: DOCUMENT_ID,
      fence: fixture.api.fence,
      generation: 1,
      hashes: [hashBytes(update)],
    }])
    expect(fixture.metrics.render()).toContain(
      'inqtrix_collaboration_persist_outcome_resolutions_total{outcome="not_found"} 1',
    )
    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(true)
    expect(fixture.coordinator.shouldSnapshot(ROOM)).toBe(false)
    expect(() => fixture.coordinator.assertJoinAllowed(ROOM)).toThrowError('restarting')
    await expect(fixture.coordinator.awaitRoom(ROOM)).resolves.toBeUndefined()
    document.destroy()
    await fixture.close()
  })

  it('treats a persistence failure whose update is found durable as committed', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const update = updateFor(document, 'Hello!')
    const hash = hashBytes(update)
    fixture.api.persistResponseErrorAfterCommit = new ApiRequestError(
      503,
      'internal_api_timeout',
    )
    fixture.api.lookupResults = [{ hash, sequence: 1 }]

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'timed-out-commit',
      context: fixture.context,
      document,
      room: ROOM,
      update,
    })).resolves.toBe('pending')
    Y.applyUpdate(document, update)

    expect(fixture.coordinator.finishClientUpdate('timed-out-commit', document)).toEqual({
      hash,
      sequence: 1,
      type: 'durable_ack',
    })
    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(false)
    expect(fixture.coordinator.getPersistedSequence(ROOM)).toBe(1)
    expect(fixture.api.lookups).toEqual([{
      documentId: DOCUMENT_ID,
      fence: fixture.api.fence,
      generation: 1,
      hashes: [hash],
    }])
    expect(fixture.metrics.render()).toContain(
      'inqtrix_collaboration_persist_outcome_resolutions_total{outcome="landed"} 1',
    )
    document.destroy()
    await fixture.close()
  })

  it('reconstructs with the original failure when the outcome lookup fails too', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const update = updateFor(document, 'Hello!')
    fixture.api.persistResponseErrorAfterCommit = new ApiRequestError(
      503,
      'internal_api_timeout',
    )
    fixture.api.lookupUpdates = async (input) => {
      fixture.api.lookups.push(input)
      throw new Error('lookup transport failed')
    }

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'unresolved-commit',
      context: fixture.context,
      document,
      room: ROOM,
      update,
    })).rejects.toMatchObject({ code: 4503, reason: 'service_unavailable' })

    expect(fixture.api.lookups.map((lookup) => lookup.hashes)).toEqual([[hashBytes(update)]])
    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(true)
    expect(fixture.metrics.render()).toContain(
      'inqtrix_collaboration_persist_outcome_resolutions_total{outcome="lookup_failed"} 1',
    )
    document.destroy()
    await fixture.close()
  })

  it.each([
    ['the store does not know the hash', 'not_found'],
    ['the lookup fails itself', 'lookup_failed'],
    ['the store answers with another update', 'inconsistent'],
  ] as const)('records why a persist outcome stayed unknown: %s', async (_label, outcome) => {
    const warnings: Array<{ event: string; fields: Record<string, unknown> | undefined }> = []
    const fixture = await coordinatorFixture(0, {}, null, {
      ...silentLogger,
      warn: (event, fields) => {
        warnings.push({ event, fields })
      },
    })
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const update = updateFor(document, 'Hello!')
    fixture.api.persistResponseErrorAfterCommit = new ApiRequestError(
      503,
      'internal_api_timeout',
    )
    if (outcome === 'lookup_failed') {
      fixture.api.lookupUpdates = async (input) => {
        fixture.api.lookups.push(input)
        throw new ApiRequestError(502, 'internal_api_unreachable')
      }
    }
    if (outcome === 'inconsistent') {
      fixture.api.lookupResults = [{ hash: 'another-update', sequence: 1 }]
    }

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: `unknown-outcome-${outcome}`,
      context: fixture.context,
      document,
      room: ROOM,
      update,
    })).rejects.toBeInstanceOf(CollaborationError)

    expect(fixture.metrics.render()).toContain(
      `inqtrix_collaboration_persist_outcome_resolutions_total{outcome="${outcome}"} 1`,
    )
    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(true)
    expect(warnings.filter((warning) => warning.event === 'persist_outcome_resolved')).toEqual([{
      event: 'persist_outcome_resolved',
      fields: {
        outcome,
        persist_reason: 'internal_api_timeout',
        persist_status: 503,
        room: ROOM,
        ...(outcome === 'lookup_failed'
          ? { lookup_reason: 'internal_api_unreachable', lookup_status: 502 }
          : {}),
      },
    }])
    document.destroy()
    await fixture.close()
  })

  it('returns an older original sequence when a duplicate replay is current globally', async () => {
    const fixture = await coordinatorFixture(5)
    const original = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const update = suggestionUpdate(original)
    Y.applyUpdate(original, update)
    fixture.api.lookupResults = [{ hash: hashBytes(update), sequence: 2 }]

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'duplicate',
      context: { ...fixture.context, access: 'suggest' },
      document: original,
      room: ROOM,
      update,
    })).resolves.toBe('pending')
    Y.applyUpdate(original, update)
    expect(fixture.coordinator.finishClientUpdate('duplicate', original)?.sequence).toBe(2)
    expect(fixture.coordinator.getPersistedSequence(ROOM)).toBe(5)
    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(false)
    expect(() => fixture.coordinator.assertJoinAllowed(ROOM)).not.toThrow()
    expect(fixture.api.lookups).toHaveLength(1)
    expect(fixture.api.persisted).toHaveLength(0)
    await fixture.close()
  })

  it('reconciles a known direct-edit replay from suggest access after generic validation', async () => {
    const fixture = await coordinatorFixture(3)
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const update = updateFor(document, 'Hello!')
    Y.applyUpdate(document, update)
    fixture.api.lookupResults = [{ hash: hashBytes(update), sequence: 2 }]

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'known-direct-replay',
      context: { ...fixture.context, access: 'suggest' },
      document,
      room: ROOM,
      update,
    })).resolves.toBe('pending')
    expect(fixture.coordinator.finishClientUpdate('known-direct-replay', document)).toMatchObject({
      sequence: 2,
      type: 'durable_ack',
    })
    expect(fixture.api.lookups).toHaveLength(1)
    expect(fixture.api.persisted).toHaveLength(0)

    document.destroy()
    await fixture.close()
  })

  it('quarantines a duplicate lookup whose durable sequence is ahead of the room', async () => {
    const fixture = await coordinatorFixture(5)
    const original = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const update = updateFor(original, 'Hello!')
    Y.applyUpdate(original, update)
    fixture.api.lookupResults = [{ hash: hashBytes(update), sequence: 6 }]

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'inconsistent-duplicate',
      context: { ...fixture.context, access: 'suggest' },
      document: original,
      room: ROOM,
      update,
    })).rejects.toThrowError('internal_consistency')

    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(true)
    expect(() => fixture.coordinator.assertJoinAllowed(ROOM)).toThrowError('restarting')
    await fixture.close()
  })

  it('still rejects a genuinely new direct edit from a suggest-only lease', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'new-direct-suggest-only',
      context: { ...fixture.context, access: 'suggest' },
      document,
      room: ROOM,
      update: updateFor(document, 'Hello!'),
    })).rejects.toThrowError('suggestion_policy_violation')
    expect(fixture.api.lookups).toHaveLength(0)
    expect(fixture.api.persisted).toHaveLength(0)
    document.destroy()
    await fixture.close()
  })

  it('reduces the tail by what a stale snapshot captured and keeps the rest', async () => {
    const fixture = await coordinatorFixture(2, { snapshotMaxUpdates: 2 })
    fixture.coordinator.initialize(ROOM, 2, { bytes: 16, updates: 2 })
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const captured = await fixture.coordinator.captureDocument(ROOM, document)
    expect(captured.sequence).toBe(2)

    const update = updateFor(document, 'Hello!')
    fixture.api.persistImplementation = async () => ({
      duplicate: false,
      persistedSequence: 3,
      sequence: 3,
    })
    await fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'connection-1',
      context: fixture.context,
      document,
      room: ROOM,
      update,
    })
    Y.applyUpdate(document, update)
    fixture.coordinator.finishClientUpdate('connection-1', document)
    const before = tailOf(fixture.coordinator)
    expect(before.updatesSinceSnapshot).toBe(3)

    expect(fixture.coordinator.markSnapshot(ROOM, captured)).toBe(false)
    expect(tailOf(fixture.coordinator)).toEqual({
      tailBytes: before.tailBytes - 16,
      updatesSinceSnapshot: 1,
    })
    expect(fixture.coordinator.hasUnsnapshottedUpdates(ROOM)).toBe(true)
    expect(fixture.coordinator.shouldSnapshot(ROOM)).toBe(false)

    const current = await fixture.coordinator.captureDocument(ROOM, document)
    expect(fixture.coordinator.markSnapshot(ROOM, current)).toBe(true)
    expect(tailOf(fixture.coordinator)).toEqual({ tailBytes: 0, updatesSinceSnapshot: 0 })
    expect(fixture.coordinator.hasUnsnapshottedUpdates(ROOM)).toBe(false)

    captured.document.destroy()
    current.document.destroy()
    document.destroy()
    await fixture.close()
  })

  it('keeps a tail counted again from the store when a snapshot captured an earlier load', async () => {
    const fixture = await coordinatorFixture(2)
    fixture.coordinator.initialize(ROOM, 2, { bytes: 16, updates: 2 })
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const captured = await fixture.coordinator.captureDocument(ROOM, document)

    fixture.coordinator.markUnloaded(ROOM)
    fixture.coordinator.initialize(ROOM, 2, { bytes: 40, updates: 5 })

    expect(fixture.coordinator.markSnapshot(ROOM, captured)).toBe(true)
    expect(tailOf(fixture.coordinator)).toEqual({ tailBytes: 40, updatesSinceSnapshot: 5 })

    captured.document.destroy()
    document.destroy()
    await fixture.close()
  })

  it('rejects oversized and malformed Yjs updates before persistence', async () => {
    const fixture = await coordinatorFixture(0, { frameLimitBytes: 4 })
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'too-large',
      context: fixture.context,
      document,
      room: ROOM,
      update: new Uint8Array(5),
    })).rejects.toThrowError('message_too_large')
    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'malformed',
      context: fixture.context,
      document,
      room: ROOM,
      update: new Uint8Array([1, 2, 3]),
    })).rejects.toThrowError('invalid_schema')
    expect(fixture.api.persisted).toHaveLength(0)
    await fixture.close()
  })

  // The canonicality boundary, stated deliberately. Encoding faults are
  // refused for everyone; a merged frame is refused where per-change policy
  // needs it (suggest) and admitted where the caller may change the document
  // directly anyway (edit) — sending the same two edits as two frames a
  // millisecond apart has the identical effect, so refusing the merged form
  // bought nothing and cost honest concurrent editors their session.
  it.each([
    ['suffix bytes', 'suffix', 'edit'],
    ['suffix bytes', 'suffix', 'suggest'],
    ['V2-as-V1 payload', 'v2', 'edit'],
    ['V2-as-V1 payload', 'v2', 'suggest'],
  ] as const)('rejects %s (%s) for %s access before role policy', async (
    _label,
    attack,
    access,
  ) => {
    const fixture = await coordinatorFixture()
    const { document, update } = genericCanonicalityAttack(attack)

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: `canonical-${attack}-${access}`,
      context: { ...fixture.context, access },
      document,
      room: ROOM,
      update,
    })).rejects.toMatchObject({ code: 4409, reason: 'invalid_schema' })
    expect(fixture.api.lookups).toHaveLength(0)
    expect(fixture.api.persisted).toHaveLength(0)

    document.destroy()
    await fixture.close()
  })

  it.each([
    ['redundant old plus novel merge', 'merged'],
    ['redundant delete-set plus novel merge', 'merged-delete'],
  ] as const)('refuses %s under suggest access via the per-change policy', async (
    _label,
    attack,
  ) => {
    // Suggest access is where merging would matter: the policy reasons about
    // ONE proposed change. It refuses the merged frame itself, without any
    // store round trip — so the encoding never had to carry that weight.
    const fixture = await coordinatorFixture()
    const { document, update } = genericCanonicalityAttack(attack)

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: `policy-${attack}`,
      context: { ...fixture.context, access: 'suggest' },
      document,
      room: ROOM,
      update,
    })).rejects.toMatchObject({ code: 4403, reason: 'suggestion_policy_violation' })
    expect(fixture.api.lookups).toHaveLength(0)
    expect(fixture.api.persisted).toHaveLength(0)

    document.destroy()
    await fixture.close()
  })

  it.each([
    ['redundant old plus novel merge', 'merged'],
    ['redundant delete-set plus novel merge', 'merged-delete'],
  ] as const)('admits %s under edit access', async (_label, attack) => {
    // Deliberate: an edit-access caller may change the document directly, so a
    // merged frame is not an escalation. Refusing it locked out honest
    // editors, because two people typing at once produce exactly this shape.
    const fixture = await coordinatorFixture()
    const { document, update } = genericCanonicalityAttack(attack)

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: `admitted-${attack}`,
      context: fixture.context,
      document,
      room: ROOM,
      update,
    })).resolves.toBe('pending')
    expect(fixture.api.lookups).toHaveLength(0)

    document.destroy()
    await fixture.close()
  })

  it.each(['edit', 'suggest'] as const)(
    'repeatedly rejects near-frame-limit suffix bloat for %s access',
    async (access) => {
      const configured = settings()
      const fixture = await coordinatorFixture()
      const document = editorJsonToYDoc(parseEditorMarkdown('Bloat'))
      const novel = updateFor(document, 'Bloat!')
      const bloated = new Uint8Array(configured.frameLimitBytes)
      bloated.set(novel)

      for (let attempt = 0; attempt < 3; attempt += 1) {
        await expect(fixture.coordinator.prepareClientUpdate({
          allowNoop: false,
          connectionId: `bloat-${access}-${attempt}`,
          context: { ...fixture.context, access },
          document,
          room: ROOM,
          update: bloated,
        })).rejects.toMatchObject({ code: 4409, reason: 'invalid_schema' })
      }
      expect(fixture.api.lookups).toHaveLength(0)
      expect(fixture.api.persisted).toHaveLength(0)

      document.destroy()
      await fixture.close()
    },
  )

  it.each(['edit', 'suggest'] as const)(
    'treats an unknown redundant update as a benign duplicate for %s access',
    async (access) => {
      const fixture = await coordinatorFixture()
      const document = editorJsonToYDoc(parseEditorMarkdown('Duplicate'))
      const update = suggestionUpdate(document)
      Y.applyUpdate(document, update)

      await expect(fixture.coordinator.prepareClientUpdate({
        allowNoop: false,
        connectionId: `unknown-redundant-${access}`,
        context: { ...fixture.context, access },
        document,
        room: ROOM,
        update,
      })).resolves.toBe('noop')
      expect(fixture.api.lookups).toHaveLength(1)
      expect(fixture.api.persisted).toHaveLength(0)

      document.destroy()
      await fixture.close()
    },
  )

  it.each(['edit', 'suggest'] as const)(
    'treats an unknown canonical empty Type-2 update as a benign duplicate for %s access',
    async (access) => {
      const fixture = await coordinatorFixture()
      const document = editorJsonToYDoc(parseEditorMarkdown('Unknown no-op'))

      await expect(fixture.coordinator.prepareClientUpdate({
        allowNoop: false,
        connectionId: `unknown-empty-${access}`,
        context: { ...fixture.context, access },
        document,
        room: ROOM,
        update: new Uint8Array([0, 0]),
      })).resolves.toBe('noop')
      expect(fixture.api.lookups).toHaveLength(1)
      expect(fixture.api.persisted).toHaveLength(0)

      document.destroy()
      await fixture.close()
    },
  )

  it('acknowledges a deletion another writer already made at the room durable sequence', async () => {
    const fixture = await coordinatorFixture()
    const room = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    // Both writers start from the same state; B deletes "lo", A deletes "o".
    const deletionB = updateFor(room, 'Hel')
    const deletionA = updateFor(room, 'Hell')
    expect(hashBytes(deletionA)).not.toBe(hashBytes(deletionB))

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'writer-b',
      context: fixture.context,
      document: room,
      room: ROOM,
      update: deletionB,
    })).resolves.toBe('pending')
    Y.applyUpdate(room, deletionB)
    expect(fixture.coordinator.finishClientUpdate('writer-b', room)?.sequence).toBe(1)

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'writer-a',
      context: fixture.context,
      document: room,
      room: ROOM,
      update: deletionA,
    })).resolves.toBe('pending')
    Y.applyUpdate(room, deletionA)
    expect(fixture.coordinator.finishClientUpdate('writer-a', room)).toEqual({
      hash: hashBytes(deletionA),
      sequence: 1,
      type: 'durable_ack',
    })
    expect(fixture.api.persisted).toHaveLength(1)
    expect(fixture.api.lookups.map((lookup) => lookup.hashes)).toEqual([[hashBytes(deletionA)]])
    expect(fixture.metrics.render()).toContain(
      'inqtrix_collaboration_redundant_updates_acknowledged_total 1\n',
    )
    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(false)
    expect(fixture.coordinator.getPersistedSequence(ROOM)).toBe(1)
    expect(markdown(room)).toBe('Hel')

    room.destroy()
    await fixture.close()
  })

  it.each([
    ['the first word of a paragraph', 1, 6, 1],
    ['the second word of a paragraph', 7, 12, 0],
  ] as const)(
    'accepts two writers who bold %s at the same time, and the room stays reproducible from the store',
    async (_label, from, to, cleanups) => {
      const fixture = await coordinatorFixture()
      const base = Y.encodeStateAsUpdate(editorJsonToYDoc(parseEditorMarkdown('Hello world')))
      const room = new Y.Doc()
      Y.applyUpdate(room, base)
      // Both writers bold the same range of the same base without seeing
      // each other; the second arrives after the first is stored.
      const boldA = markUpdate(base, 101, from, to, 'bold')
      const boldB = markUpdate(base, 102, from, to, 'bold')

      for (const [connectionId, update] of [['writer-a', boldA], ['writer-b', boldB]] as const) {
        await expect(fixture.coordinator.prepareClientUpdate({
          allowNoop: false,
          connectionId,
          context: fixture.context,
          document: room,
          room: ROOM,
          update,
        })).resolves.toBe('pending')
        Y.applyUpdate(room, update)
        // The coordinator verifies that the room equals its validated clone.
        expect(fixture.coordinator.finishClientUpdate(connectionId, room)).toMatchObject({
          hash: hashBytes(update),
          type: 'durable_ack',
        })
      }

      const rendered = fixture.metrics.render()
      if (cleanups === 0) {
        expect(rendered).not.toContain('inqtrix_collaboration_formatting_cleanups_accepted_total')
      } else {
        expect(rendered).toContain(
          `inqtrix_collaboration_formatting_cleanups_accepted_total ${cleanups}\n`,
        )
      }
      expect(fixture.api.persisted.map((row) => row.update)).toEqual([boldA, boldB])
      // The second bold is stored, but the reader sees the document the first
      // one left: its summary says it changed nothing.
      expect(fixture.api.persisted.map((row) => row.changeSummary.changesDocument)).toEqual([true, false])
      // A reconstruction applies each stored update in its own transaction.
      const replayed = new Y.Doc()
      Y.applyUpdate(replayed, base)
      for (const row of fixture.api.persisted) Y.applyUpdate(replayed, row.update)
      expect(Y.encodeStateAsUpdate(replayed)).toEqual(Y.encodeStateAsUpdate(room))
      expect(markdown(room)).toBe(from === 1 ? '**Hello** world' : 'Hello **world**')
      expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(false)

      replayed.destroy()
      room.destroy()
      await fixture.close()
    },
  )

  it.each([
    ['inserts a character', (text: Y.XmlText) => text.insert(0, 'x')],
    ['deletes a character', (text: Y.XmlText) => text.delete(0, 1)],
    ['formats a range', (text: Y.XmlText) => text.format(0, 2, { italic: {} })],
  ] as const)(
    'refuses an update after which the document changes again in a way that %s',
    async (_label, followUp) => {
      // Only a Yjs formatting cleanup may follow the client's transaction.
      // An observer of the clone stands in for any other writer.
      const clone = documentStateModule.cloneDocument
      vi.spyOn(documentStateModule, 'cloneDocument').mockImplementation((document, state) => {
        const cloned = clone(document, state)
        let written = false
        cloned.on('afterTransaction', (transaction: Y.Transaction) => {
          if (transaction.local || written) return
          written = true
          const text = (cloned.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0) as Y.XmlElement)
            .get(0) as Y.XmlText
          cloned.transact(() => followUp(text))
        })
        return cloned
      })
      const rejectionCauses: unknown[] = []
      const fixture = await coordinatorFixture(0, {}, null, {
        ...silentLogger,
        warn: (event, fields) => {
          if (event === 'client_update_rejected') rejectionCauses.push(fields?.cause)
        },
      })
      const document = editorJsonToYDoc(parseEditorMarkdown('Hello world'))

      await expect(fixture.coordinator.prepareClientUpdate({
        allowNoop: false,
        connectionId: 'observed',
        context: fixture.context,
        document,
        room: ROOM,
        update: updateFor(document, 'Hello world!'),
      })).rejects.toMatchObject({ code: 4409, reason: 'invalid_schema' })
      expect(rejectionCauses).toEqual(['multiple_applied'])
      expect(fixture.api.persisted).toHaveLength(0)
      document.destroy()
      await fixture.close()
    },
  )

  it('refuses a suggestion whose formatting cleanup would delete another writer\'s concurrent suggestion', async () => {
    // Two writers with suggest access mark the same first word for deletion
    // without seeing each other. Applying the second update makes Yjs delete
    // one of the two deletion marks; the suggestion policy judges that end
    // state and refuses the update, so nothing of it is stored.
    const fixture = await coordinatorFixture()
    const otherUser = '77777777-7777-4777-8777-777777777777'
    const base = Y.encodeStateAsUpdate(editorJsonToYDoc(parseEditorMarkdown('Hello world')))
    const room = new Y.Doc()
    Y.applyUpdate(room, base)
    const deletionA = deletionSuggestionUpdate(base, 101, USER_ID, PATCH_ID, SUGGESTION_ID)
    const deletionB = deletionSuggestionUpdate(
      base,
      102,
      otherUser,
      EXISTING_PATCH_ID,
      EXISTING_SUGGESTION_ID,
    )

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'writer-a',
      context: { ...fixture.context, access: 'suggest' },
      document: room,
      room: ROOM,
      update: deletionA,
    })).resolves.toBe('pending')
    Y.applyUpdate(room, deletionA)
    fixture.coordinator.finishClientUpdate('writer-a', room)
    const before = Y.encodeStateAsUpdate(room)

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'writer-b',
      context: {
        ...fixture.context,
        access: 'suggest',
        user: { ...fixture.context.user, id: otherUser },
      },
      document: room,
      room: ROOM,
      update: deletionB,
    })).rejects.toMatchObject({ code: 4403, reason: 'suggestion_policy_violation' })

    expect(fixture.api.persisted.map((row) => row.update)).toEqual([deletionA])
    expect(Y.encodeStateAsUpdate(room)).toEqual(before)
    expect(fixture.metrics.render()).not.toContain(
      'inqtrix_collaboration_formatting_cleanups_accepted_total',
    )
    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(false)
    room.destroy()
    await fixture.close()
  })

  it('allows canonical V1 updates with no novel state as protocol sync no-ops', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Sync'))
    const redundantState = Y.encodeStateAsUpdate(document)

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: true,
      connectionId: 'canonical-empty-sync',
      context: fixture.context,
      document,
      room: ROOM,
      update: new Uint8Array([0, 0]),
    })).resolves.toBe('noop')
    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: true,
      connectionId: 'redundant-state-sync',
      context: fixture.context,
      document,
      room: ROOM,
      update: redundantState,
    })).resolves.toBe('noop')
    expect(fixture.api.lookups).toHaveLength(0)
    expect(fixture.api.persisted).toHaveLength(0)

    document.destroy()
    await fixture.close()
  })

  it.each([
    ['top-level subtree replacement', 'abc', [0]],
    ['nested subtree replacement rebased from a replica', '> abc', [0, 0]],
  ])('rejects a same-JSON suggest-mode %s that invalidates anchors', async (
    _label,
    markdownSource,
    path,
  ) => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown(markdownSource))
    const attack = topologyReplacementUpdate(document, path)
    const attacked = clone(document)
    Y.applyUpdate(attacked, attack.update)

    expect(editorYDocToJson(attacked)).toEqual(editorYDocToJson(document))
    expect(Y.createAbsolutePositionFromRelativePosition(
      attack.anchor,
      attacked,
    )).toBeNull()
    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: true,
      connectionId: `topology-${path.join('-')}`,
      context: { ...fixture.context, access: 'suggest' },
      document,
      room: ROOM,
      update: attack.update,
    })).rejects.toThrowError('suggestion_policy_violation')
    expect(fixture.api.persisted).toHaveLength(0)

    attacked.destroy()
    document.destroy()
    await fixture.close()
  })

  it.each([
    ['huge XmlElement attribute', 'element-attribute'],
    ['huge XmlText attribute', 'text-attribute'],
    ['insert-delete text', 'text-content'],
    ['format churn', 'text-format'],
  ] as const)('rejects a valid suggestion bundled with transient %s history', async (
    _label,
    hiddenHistory,
  ) => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('First\n\nSecond'))
    const update = suggestionWithTransientYjsHistoryUpdate(document, hiddenHistory)
    const attacked = clone(document)
    Y.applyUpdate(attacked, update)

    expect(JSON.stringify(validateEditorYDoc(attacked))).toContain(SUGGESTION_ID)
    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: `transient-${hiddenHistory}`,
      context: { ...fixture.context, access: 'suggest' },
      document,
      room: ROOM,
      update,
    })).rejects.toMatchObject({
      code: 4403,
      reason: 'suggestion_policy_violation',
    })
    expect(fixture.api.lookups).toHaveLength(0)
    expect(fixture.api.persisted).toHaveLength(0)

    attacked.destroy()
    document.destroy()
    await fixture.close()
  })

  it('rejects a valid suggestion bundled with an unknown top-level Yjs type', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('First\n\nSecond'))
    const update = suggestionWithTransientYjsHistoryUpdate(document, 'unknown-root')
    const attacked = clone(document)
    Y.applyUpdate(attacked, update)

    expect([...attacked.share.keys()]).toContain('rogue')
    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'unknown-root-history',
      context: { ...fixture.context, access: 'suggest' },
      document,
      room: ROOM,
      update,
    })).rejects.toMatchObject({ code: 4409, reason: 'invalid_schema' })
    expect(fixture.api.lookups).toHaveLength(0)
    expect(fixture.api.persisted).toHaveLength(0)

    attacked.destroy()
    document.destroy()
    await fixture.close()
  })

  it.each([
    ['missing struct dependency', 'struct'],
    ['pending delete dependency', 'delete'],
  ] as const)('rejects a valid suggestion bundled with a %s', async (_label, dependency) => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('First\n\nSecond'))
    const update = suggestionWithMissingCausalUpdate(document, dependency)
    const attacked = clone(document)
    Y.applyUpdate(attacked, update)

    expect(() => validateEditorYDoc(attacked)).toThrow(/unresolved/)
    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: `causal-${dependency}`,
      context: { ...fixture.context, access: 'suggest' },
      document,
      room: ROOM,
      update,
    })).rejects.toMatchObject({ code: 4412, reason: 'causal_gap' })
    expect(fixture.api.lookups).toHaveLength(0)
    expect(fixture.api.persisted).toHaveLength(0)

    attacked.destroy()
    document.destroy()
    await fixture.close()
  })

  it.each([
    ['a missing inserted predecessor', 'insertion'],
    ['a missing deleted predecessor', 'deletion'],
  ] as const)('rejects an update whose predecessor is missing as a retryable causal gap: %s', async (
    _label,
    dependency,
  ) => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    // One replica, one Yjs client: the second update continues the first.
    const [, successor] = sequentialUpdates(
      document,
      dependency === 'insertion' ? ['Hello!', 'Hello!!'] : ['Hello!', 'Hello'],
    )

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: `causal-gap-${dependency}`,
      context: fixture.context,
      document,
      room: ROOM,
      update: successor!,
    })).rejects.toMatchObject({ code: 4412, reason: 'causal_gap' })
    expect(fixture.api.persisted).toHaveLength(0)
    expect(fixture.api.lookups).toHaveLength(0)
    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(false)
    expect(fixture.metrics.render()).toContain(
      'inqtrix_collaboration_rejections_total{reason="causal_gap"} 1',
    )

    document.destroy()
    await fixture.close()
  })

  it('keeps a non-canonical document structure a schema violation', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const replica = clone(document)
    const vector = Y.encodeStateVector(replica)
    replica.getMap('rogue').set('payload', 'foreign root')
    const update = Y.encodeStateAsUpdate(replica, vector)
    replica.destroy()

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'foreign-root',
      context: fixture.context,
      document,
      room: ROOM,
      update,
    })).rejects.toMatchObject({ code: 4409, reason: 'invalid_schema' })
    expect(fixture.api.persisted).toHaveLength(0)
    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(false)

    document.destroy()
    await fixture.close()
  })

  it.each([
    ['XmlElement attribute', 'element-attribute'],
    ['XmlText attribute', 'text-attribute'],
    ['empty XmlText topology', 'empty-text'],
  ] as const)('rejects a legitimate suggestion bundled with hidden %s', async (
    _label,
    hiddenState,
  ) => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('First\n\nSecond'))
    const update = suggestionWithHiddenYjsStateUpdate(document, hiddenState)
    const attacked = clone(document)
    Y.applyUpdate(attacked, update)

    expect(JSON.stringify(editorYDocToJson(attacked))).toContain(SUGGESTION_ID)
    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: `hidden-${hiddenState}`,
      context: { ...fixture.context, access: 'suggest' },
      document,
      room: ROOM,
      update,
    })).rejects.toThrowError('invalid_schema')
    expect(fixture.api.persisted).toHaveLength(0)

    attacked.destroy()
    document.destroy()
    await fixture.close()
  })

  it('rejects cloning an unchanged insertion text beside a legitimate new suggestion', async () => {
    const fixture = await coordinatorFixture()
    const plain = schema.node('doc', null, [
      schema.node('paragraph', null, schema.text('First')),
      schema.node('paragraph'),
    ])
    const state = EditorState.create({ schema, doc: plain })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('X', state.doc.child(0).nodeSize + 1),
      state,
      {
        authorId: USER_ID,
        createdAt: 1_784_111_000,
        patchId: EXISTING_PATCH_ID,
      },
      () => EXISTING_SUGGESTION_ID,
    ).doc
    const document = editorJsonToYDoc(tracked.toJSON())
    const sourceText = firstXmlText(xmlElementAt(
      document.getXmlFragment(EDITOR_YJS_FRAGMENT),
      [1],
    ))
    if (!sourceText) throw new Error('Existing suggestion fixture has no text anchor')
    const anchor = Y.createRelativePositionFromTypeIndex(sourceText, 0)

    const replica = clone(document)
    const vector = Y.encodeStateVector(replica)
    applySuggestion(replica)
    replaceFirstXmlText(replica.getXmlFragment(EDITOR_YJS_FRAGMENT), [1])
    const update = Y.encodeStateAsUpdate(replica, vector)
    const attacked = clone(document)
    Y.applyUpdate(attacked, update)

    expect(JSON.stringify(editorYDocToJson(attacked))).toContain(SUGGESTION_ID)
    expect(JSON.stringify(editorYDocToJson(attacked))).toContain(EXISTING_SUGGESTION_ID)
    expect(Y.createAbsolutePositionFromRelativePosition(anchor, attacked)).toBeNull()
    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'combined-insertion-text-topology',
      context: { ...fixture.context, access: 'suggest' },
      document,
      room: ROOM,
      update,
    })).rejects.toThrowError('suggestion_policy_violation')
    expect(fixture.api.persisted).toHaveLength(0)

    replica.destroy()
    attacked.destroy()
    document.destroy()
    await fixture.close()
  })

  it.each([
    ['top-level subtree replacement', 'First\n\nSecond', [1]],
    ['nested subtree replacement', 'First\n\n> Second', [1, 0]],
    ['unrelated relative-position invalidation', 'First\n\nabcabc', [1]],
  ])('rejects a legitimate suggestion bundled with %s', async (
    _label,
    markdownSource,
    path,
  ) => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown(markdownSource))
    const attack = suggestionWithTopologyReplacementUpdate(document, path)
    const attacked = clone(document)
    Y.applyUpdate(attacked, attack.update)

    expect(editorYDocToJson(attacked)).not.toEqual(editorYDocToJson(document))
    expect(JSON.stringify(editorYDocToJson(attacked))).toContain(SUGGESTION_ID)
    expect(Y.createAbsolutePositionFromRelativePosition(
      attack.anchor,
      attacked,
    )).toBeNull()
    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: `combined-topology-${path.join('-')}`,
      context: { ...fixture.context, access: 'suggest' },
      document,
      room: ROOM,
      update: attack.update,
    })).rejects.toThrowError('suggestion_policy_violation')
    expect(fixture.api.persisted).toHaveLength(0)

    attacked.destroy()
    document.destroy()
    await fixture.close()
  })

  it('persists a legitimate suggestion that preserves shared Yjs topology', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('First\n\nSecond'))
    const anchoredText = firstXmlText(xmlElementAt(
      document.getXmlFragment(EDITOR_YJS_FRAGMENT),
      [1],
    ))
    if (!anchoredText) throw new Error('Legitimate suggestion fixture has no text anchor')
    const anchor = Y.createRelativePositionFromTypeIndex(anchoredText, 1)
    const update = suggestionUpdate(document)
    const updated = clone(document)
    Y.applyUpdate(updated, update)

    expect(Y.createAbsolutePositionFromRelativePosition(anchor, updated)).not.toBeNull()
    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'legitimate-suggestion',
      context: { ...fixture.context, access: 'suggest' },
      document,
      room: ROOM,
      update,
    })).resolves.toBe('pending')
    Y.applyUpdate(document, update)
    expect(fixture.coordinator.finishClientUpdate('legitimate-suggestion', document)).toEqual({
      hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      sequence: 1,
      type: 'durable_ack',
    })
    expect(fixture.api.persisted).toHaveLength(1)
    expect(fixture.api.persisted[0]).toMatchObject({
      changeKind: 'suggestion',
      suggestionIds: [SUGGESTION_ID],
    })
    expect(fixture.api.persisted[0]?.update).toBe(update)

    updated.destroy()
    document.destroy()
    await fixture.close()
  })

  it.each(['deletion', 'modification', 'structure'] as const)(
    'persists a normal %s suggestion without rejecting its causal history',
    async (operation) => {
      const fixture = await coordinatorFixture()
      const document = editorJsonToYDoc(parseEditorMarkdown('First'))
      const update = suggestionUpdate(document, operation)

      await expect(fixture.coordinator.prepareClientUpdate({
        allowNoop: false,
        connectionId: `legitimate-${operation}`,
        context: { ...fixture.context, access: 'suggest' },
        document,
        room: ROOM,
        update,
      })).resolves.toBe('pending')
      Y.applyUpdate(document, update)
      expect(fixture.coordinator.finishClientUpdate(
        `legitimate-${operation}`,
        document,
      )?.sequence).toBe(1)
      expect(fixture.api.persisted).toHaveLength(1)
      expect(fixture.api.persisted[0]).toMatchObject({
        changeKind: 'suggestion',
        suggestions: [{
          kind: operation === 'modification' ? 'replacement' : operation,
        }],
        update,
      })

      document.destroy()
      await fixture.close()
    },
  )

  it('allows a suggest author to edit existing insertion text with a resolved tombstone', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('First'))
    Y.applyUpdate(document, suggestionUpdate(document, 'insertion', 'XY'))
    const replica = clone(document)
    const vector = Y.encodeStateVector(replica)
    const text = firstXmlText(xmlElementAt(
      replica.getXmlFragment(EDITOR_YJS_FRAGMENT),
      [0],
    ))
    if (!text) throw new Error('Resolved-tombstone fixture has no XML text')
    let offset = 0
    let deletedAt = -1
    for (const part of text.toDelta()) {
      if (typeof part.insert !== 'string') continue
      const index = part.insert.indexOf('Y')
      if (index >= 0) {
        deletedAt = offset + index
        break
      }
      offset += part.insert.length
    }
    if (deletedAt < 0) throw new Error('Resolved-tombstone fixture has no inserted text')
    text.delete(deletedAt, 1)
    const update = Y.encodeStateAsUpdate(replica, vector)
    replica.destroy()

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'resolved-suggestion-tombstone',
      context: { ...fixture.context, access: 'suggest' },
      document,
      room: ROOM,
      update,
    })).resolves.toBe('pending')
    Y.applyUpdate(document, update)
    expect(fixture.coordinator.finishClientUpdate(
      'resolved-suggestion-tombstone',
      document,
    )?.sequence).toBe(1)
    expect(fixture.api.persisted).toHaveLength(1)
    expect(fixture.api.persisted[0]).toMatchObject({
      changeKind: 'suggestion',
      suggestionIds: [SUGGESTION_ID],
      update,
    })

    document.destroy()
    await fixture.close()
  })

  it('persists the first slash structure suggestion without replacing its shared text anchor', async () => {
    const fixture = await coordinatorFixture()
    const document = editorJsonToYDoc(parseEditorMarkdown('## Suggest target'))
    const sourceText = firstXmlText(xmlElementAt(
      document.getXmlFragment(EDITOR_YJS_FRAGMENT),
      [0],
    ))
    if (!sourceText) throw new Error('Structure suggestion fixture has no text anchor')
    const anchor = Y.createRelativePositionFromTypeIndex(sourceText, 1)

    const slashReplica = clone(document)
    const slashVector = Y.encodeStateVector(slashReplica)
    const slashFragment = slashReplica.getXmlFragment(EDITOR_YJS_FRAGMENT)
    const slashInitialized = initProseMirrorDoc(slashFragment, schema)
    const initialState = EditorState.create({ schema, doc: slashInitialized.doc })
    const trackedSlash = transformToInqtrixSuggestionTransaction(
      initialState.tr.insertText('/', 1),
      initialState,
      { authorId: USER_ID, createdAt: 1_784_112_000, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    )
    updateYFragment(
      slashReplica,
      slashFragment,
      trackedSlash.doc,
      slashInitialized.meta,
    )
    const slashUpdate = Y.encodeStateAsUpdate(slashReplica, slashVector)

    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'first-structure-slash',
      context: { ...fixture.context, access: 'suggest' },
      document,
      room: ROOM,
      update: slashUpdate,
    })).resolves.toBe('pending')
    Y.applyUpdate(document, slashUpdate)
    expect(fixture.coordinator.finishClientUpdate(
      'first-structure-slash',
      document,
    )).toMatchObject({ sequence: 1, type: 'durable_ack' })

    const structureReplica = clone(document)
    const structureVector = Y.encodeStateVector(structureReplica)
    const structureFragment = structureReplica.getXmlFragment(EDITOR_YJS_FRAGMENT)
    const structureInitialized = initProseMirrorDoc(structureFragment, schema)
    const slashState = EditorState.create({ schema, doc: structureInitialized.doc })
    const paragraph = schema.nodes.paragraph
    if (!paragraph) throw new Error('Structure suggestion fixture has no paragraph type')
    const proposed = slashState.tr
      .delete(1, 2)
      .setNodeMarkup(0, paragraph, { textAlign: null })
      .setMeta(INQTRIX_STRUCTURE_COMMAND_META, {
        action: 'paragraph',
        commandRange: { from: 1, to: 2 },
      })
    const trackedStructure = transformToInqtrixSuggestionTransaction(
      proposed,
      slashState,
      {
        authorId: USER_ID,
        createdAt: 1_784_112_001,
        patchId: EXISTING_PATCH_ID,
      },
      () => EXISTING_SUGGESTION_ID,
    )
    updateYFragment(
      structureReplica,
      structureFragment,
      trackedStructure.doc,
      structureInitialized.meta,
    )
    const structureUpdate = Y.encodeStateAsUpdate(structureReplica, structureVector)
    const updated = clone(document)
    Y.applyUpdate(updated, structureUpdate)

    expect(Y.createAbsolutePositionFromRelativePosition(anchor, updated)).not.toBeNull()
    await expect(fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId: 'first-structure-suggestion',
      context: { ...fixture.context, access: 'suggest' },
      document,
      room: ROOM,
      update: structureUpdate,
    })).resolves.toBe('pending')
    Y.applyUpdate(document, structureUpdate)
    expect(fixture.coordinator.finishClientUpdate(
      'first-structure-suggestion',
      document,
    )).toMatchObject({ sequence: 2, type: 'durable_ack' })
    expect(fixture.api.persisted).toHaveLength(2)
    expect(fixture.api.persisted[1]).toMatchObject({
      changeKind: 'suggestion',
      suggestionIds: [SUGGESTION_ID, EXISTING_SUGGESTION_ID],
      update: structureUpdate,
    })

    updated.destroy()
    structureReplica.destroy()
    slashReplica.destroy()
    document.destroy()
    await fixture.close()
  })
})

async function coordinatorFixture(
  sequence = 0,
  overrides: Parameters<typeof settings>[0] = {},
  validatedDocument: ReturnType<typeof validateDocument> | null = null,
  logger: SidecarLogger = silentLogger,
): Promise<{
  api: FakeCollaborationApi
  close: () => Promise<void>
  context: ConnectionContext
  coordinator: DocumentCoordinator
  metrics: SidecarMetrics
}> {
  const api = new FakeCollaborationApi()
  const configured = settings(overrides)
  const metrics = new SidecarMetrics()
  const lease = new InstanceLeaseManager(api, configured, silentLogger, metrics, () => undefined)
  await lease.start()
  const coordinator = new DocumentCoordinator(api, lease, configured, logger, metrics)
  coordinator.initialize(ROOM, sequence, { bytes: 0, updates: 0 }, validatedDocument)
  return {
    api,
    close: () => lease.stop(),
    metrics,
    context: {
      access: 'edit',
      documentId: DOCUMENT_ID,
      expiresAt: Date.now() / 1_000 + 60,
      generation: 1,
      leaseId: 'lease-1',
      policyCursor: 0,
      protocolVersion: configured.protocolVersion,
      schemaHash: await getEditorSchemaFingerprint(),
      schemaVersion: configured.schemaVersion,
      sessionId: 'session-1',
      tenantId: 'tenant-1',
      user: { color: '#123456', id: USER_ID, name: 'Ada' },
    },
    coordinator,
  }
}

function updateFor(document: Y.Doc, markdownTarget: string): Uint8Array {
  const replica = clone(document)
  const vector = Y.encodeStateVector(replica)
  const fragment = replica.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, schema)
  const target = schema.nodeFromJSON(parseEditorMarkdown(markdownTarget))
  updateYFragment(replica, fragment, target, initialized.meta)
  const update = Y.encodeStateAsUpdate(replica, vector)
  replica.destroy()
  return update
}

/** A client-shaped update of one writer that adds a mark to a range of the
 * given state, as the editor writes it through y-tiptap. */
function markUpdate(
  base: Uint8Array,
  clientId: number,
  from: number,
  to: number,
  mark: 'bold' | 'italic',
): Uint8Array {
  const replica = new Y.Doc()
  replica.clientID = clientId
  Y.applyUpdate(replica, base)
  const fragment = replica.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, schema)
  const state = EditorState.create({ doc: initialized.doc, schema })
  const vector = Y.encodeStateVector(replica)
  updateYFragment(
    replica,
    fragment,
    state.tr.addMark(from, to, schema.marks[mark]!.create()).doc,
    initialized.meta,
  )
  const update = Y.encodeStateAsUpdate(replica, vector)
  replica.destroy()
  return update
}

/** A suggest-mode writer marking the first word of the given state for
 * deletion. */
function deletionSuggestionUpdate(
  base: Uint8Array,
  clientId: number,
  authorId: string,
  patchId: string,
  suggestionId: string,
): Uint8Array {
  const replica = new Y.Doc()
  replica.clientID = clientId
  Y.applyUpdate(replica, base)
  const fragment = replica.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, schema)
  const state = EditorState.create({ doc: initialized.doc, schema })
  const vector = Y.encodeStateVector(replica)
  const tracked = transformToInqtrixSuggestionTransaction(
    state.tr.delete(1, 6),
    state,
    { authorId, createdAt: 1_784_112_000, patchId },
    () => suggestionId,
  )
  updateYFragment(replica, fragment, tracked.doc, initialized.meta)
  const update = Y.encodeStateAsUpdate(replica, vector)
  replica.destroy()
  return update
}

function sequentialUpdates(document: Y.Doc, markdownTargets: readonly string[]): Uint8Array[] {
  const replica = clone(document)
  const fragment = replica.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const updates: Uint8Array[] = []
  for (const markdownTarget of markdownTargets) {
    const vector = Y.encodeStateVector(replica)
    const initialized = initProseMirrorDoc(fragment, schema)
    const target = schema.nodeFromJSON(parseEditorMarkdown(markdownTarget))
    updateYFragment(replica, fragment, target, initialized.meta)
    updates.push(Y.encodeStateAsUpdate(replica, vector))
  }
  replica.destroy()
  return updates
}

function updateForV2(document: Y.Doc, markdownTarget: string): Uint8Array {
  const replica = clone(document)
  const vector = Y.encodeStateVector(replica)
  const fragment = replica.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, schema)
  const target = schema.nodeFromJSON(parseEditorMarkdown(markdownTarget))
  updateYFragment(replica, fragment, target, initialized.meta)
  const update = Y.encodeStateAsUpdateV2(replica, vector)
  replica.destroy()
  return update
}

function genericCanonicalityAttack(
  attack: 'merged' | 'merged-delete' | 'suffix' | 'v2',
): { document: Y.Doc; update: Uint8Array } {
  const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
  if (attack === 'v2') {
    return { document, update: updateForV2(document, 'Hello!') }
  }
  const first = updateFor(document, attack === 'merged-delete' ? 'ello' : 'Hello!')
  if (attack === 'suffix') {
    const update = new Uint8Array(first.byteLength + 3)
    update.set(first)
    update.set([0xde, 0xad, 0xbe], first.byteLength)
    return { document, update }
  }
  Y.applyUpdate(document, first)
  const second = updateFor(document, attack === 'merged-delete' ? 'ello!' : 'Hello!!')
  return { document, update: Y.mergeUpdates([first, second]) }
}

function clone(document: Y.Doc): Y.Doc {
  const result = new Y.Doc()
  Y.applyUpdate(result, Y.encodeStateAsUpdate(document))
  return result
}

function topologyReplacementUpdate(
  document: Y.Doc,
  path: readonly number[],
): { anchor: Y.RelativePosition; update: Uint8Array } {
  const sourceNode = xmlElementAt(document.getXmlFragment(EDITOR_YJS_FRAGMENT), path)
  const sourceText = firstXmlText(sourceNode)
  if (!sourceText) throw new Error('Topology fixture has no text anchor')
  const anchor = Y.createRelativePositionFromTypeIndex(sourceText, 1)

  const replica = clone(document)
  const vector = Y.encodeStateVector(replica)
  replaceXmlElement(replica.getXmlFragment(EDITOR_YJS_FRAGMENT), path)
  const update = Y.encodeStateAsUpdate(replica, vector)
  replica.destroy()
  return { anchor, update }
}

function suggestionWithTopologyReplacementUpdate(
  document: Y.Doc,
  path: readonly number[],
): { anchor: Y.RelativePosition; update: Uint8Array } {
  const sourceNode = xmlElementAt(document.getXmlFragment(EDITOR_YJS_FRAGMENT), path)
  const sourceText = firstXmlText(sourceNode)
  if (!sourceText) throw new Error('Combined topology fixture has no text anchor')
  const anchor = Y.createRelativePositionFromTypeIndex(sourceText, 1)

  const replica = clone(document)
  const vector = Y.encodeStateVector(replica)
  applySuggestion(replica)
  replaceXmlElement(replica.getXmlFragment(EDITOR_YJS_FRAGMENT), path)
  const update = Y.encodeStateAsUpdate(replica, vector)
  replica.destroy()
  return { anchor, update }
}

type SuggestionOperation = 'deletion' | 'insertion' | 'modification' | 'structure'

function suggestionUpdate(
  document: Y.Doc,
  operation: SuggestionOperation = 'insertion',
  insertedText = '!',
): Uint8Array {
  const replica = clone(document)
  const vector = Y.encodeStateVector(replica)
  applySuggestion(replica, operation, insertedText)
  const update = Y.encodeStateAsUpdate(replica, vector)
  replica.destroy()
  return update
}

function suggestionWithTransientYjsHistoryUpdate(
  document: Y.Doc,
  hiddenHistory:
    | 'element-attribute'
    | 'text-attribute'
    | 'text-content'
    | 'text-format'
    | 'unknown-root',
): Uint8Array {
  const replica = clone(document)
  const vector = Y.encodeStateVector(replica)
  applySuggestion(replica)
  const paragraph = xmlElementAt(replica.getXmlFragment(EDITOR_YJS_FRAGMENT), [1])
  const text = firstXmlText(paragraph)
  if (!text) throw new Error('Transient-history fixture has no XML text')
  const hidden = 'x'.repeat(256 * 1024)
  if (hiddenHistory === 'element-attribute') {
    paragraph.setAttribute('rogue', hidden)
    paragraph.removeAttribute('rogue')
  } else if (hiddenHistory === 'text-attribute') {
    text.setAttribute('rogue', hidden)
    text.removeAttribute('rogue')
  } else if (hiddenHistory === 'text-content') {
    text.insert(1, hidden)
    text.delete(1, hidden.length)
  } else if (hiddenHistory === 'text-format') {
    text.format(0, text.length, { rogue: hidden })
    text.format(0, text.length, { rogue: null })
  } else {
    const rogue = replica.getMap('rogue')
    rogue.set('payload', hidden)
    rogue.delete('payload')
  }
  const update = Y.encodeStateAsUpdate(replica, vector)
  replica.destroy()
  return update
}

function suggestionWithMissingCausalUpdate(
  document: Y.Doc,
  dependency: 'delete' | 'struct',
): Uint8Array {
  const causalReplica = clone(document)
  const text = firstXmlText(xmlElementAt(
    causalReplica.getXmlFragment(EDITOR_YJS_FRAGMENT),
    [1],
  ))
  if (!text) throw new Error('Causal fixture has no XML text')
  text.insert(text.length, 'missing')
  const dependencyVector = Y.encodeStateVector(causalReplica)
  if (dependency === 'struct') text.insert(text.length, '!')
  else text.delete(text.length - 'missing'.length, 'missing'.length)
  const incomplete = Y.encodeStateAsUpdate(causalReplica, dependencyVector)
  causalReplica.destroy()
  return Y.mergeUpdates([suggestionUpdate(document), incomplete])
}

function suggestionWithHiddenYjsStateUpdate(
  document: Y.Doc,
  hiddenState: 'element-attribute' | 'empty-text' | 'text-attribute',
): Uint8Array {
  const replica = clone(document)
  const vector = Y.encodeStateVector(replica)
  applySuggestion(replica)
  const paragraph = xmlElementAt(replica.getXmlFragment(EDITOR_YJS_FRAGMENT), [1])
  if (hiddenState === 'element-attribute') {
    paragraph.setAttribute('rogue', 'hidden')
  } else if (hiddenState === 'text-attribute') {
    const text = firstXmlText(paragraph)
    if (!text) throw new Error('Hidden-state fixture has no XML text')
    text.setAttribute('rogue', 'hidden')
  } else {
    paragraph.insert(paragraph.length, [new Y.XmlText()])
  }
  const update = Y.encodeStateAsUpdate(replica, vector)
  replica.destroy()
  return update
}

function applySuggestion(
  document: Y.Doc,
  operation: SuggestionOperation = 'insertion',
  insertedText = '!',
): void {
  const fragment = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, schema)
  let textPosition: number | null = null
  initialized.doc.descendants((node, position) => {
    if (textPosition === null && node.isText) textPosition = position
  })
  if (textPosition === null) throw new Error('Suggestion fixture has no text position')
  const state = EditorState.create({ schema, doc: initialized.doc })
  const transaction = operation === 'insertion'
    ? state.tr.insertText(insertedText, textPosition + 1)
    : operation === 'deletion'
      ? state.tr.delete(textPosition + 1, textPosition + 2)
      : operation === 'structure'
        ? state.tr
            .setNodeMarkup(0, schema.nodes.heading, {
              level: 2,
              textAlign: null,
            })
            .setMeta(INQTRIX_STRUCTURE_COMMAND_META, { action: 'heading2' })
        : state.tr.insertText('X', textPosition + 1, textPosition + 2)
  const tracked = transformToInqtrixSuggestionTransaction(
    transaction,
    state,
    { authorId: USER_ID, createdAt: 1_784_112_000, patchId: PATCH_ID },
    () => SUGGESTION_ID,
  )
  updateYFragment(document, fragment, tracked.doc, initialized.meta)
}

function replaceXmlElement(root: Y.XmlFragment, path: readonly number[]): void {
  const parent = xmlParentAt(root, path)
  const index = path.at(-1)
  if (index === undefined) throw new Error('Topology fixture path is empty')
  const current = parent.get(index)
  if (!(current instanceof Y.XmlElement)) {
    throw new Error('Topology fixture does not point to an XML element')
  }
  const replacement = current.clone()
  parent.delete(index, 1)
  parent.insert(index, [replacement])
}

function replaceFirstXmlText(root: Y.XmlFragment, path: readonly number[]): void {
  const element = xmlElementAt(root, path)
  const current = element.get(0)
  if (!(current instanceof Y.XmlText)) {
    throw new Error('Topology fixture does not point to XML text')
  }
  const replacement = current.clone()
  element.delete(0, 1)
  element.insert(0, [replacement])
}

function xmlParentAt(
  root: Y.XmlFragment,
  path: readonly number[],
): Y.XmlFragment | Y.XmlElement {
  if (path.length === 0) throw new Error('Topology fixture path is empty')
  let parent: Y.XmlFragment | Y.XmlElement = root
  for (const index of path.slice(0, -1)) {
    const child: unknown = parent.get(index)
    if (!(child instanceof Y.XmlElement)) {
      throw new Error('Topology fixture path is invalid')
    }
    parent = child
  }
  return parent
}

function xmlElementAt(root: Y.XmlFragment, path: readonly number[]): Y.XmlElement {
  const parent = xmlParentAt(root, path)
  const index = path.at(-1)
  const node = index === undefined ? null : parent.get(index)
  if (!(node instanceof Y.XmlElement)) throw new Error('Topology fixture path is invalid')
  return node
}

function firstXmlText(node: Y.XmlElement): Y.XmlText | null {
  for (let index = 0; index < node.length; index += 1) {
    const child = node.get(index)
    if (child instanceof Y.XmlText) return child
    if (child instanceof Y.XmlElement) {
      const nested = firstXmlText(child)
      if (nested) return nested
    }
  }
  return null
}

function markdown(document: Y.Doc): string {
  return serializeEditorJson(editorYDocToJson(document), 'final').trim()
}

/** The room's unsnapshotted tail, read from the coordinator's own state. */
function tailOf(coordinator: DocumentCoordinator): {
  tailBytes: number
  updatesSinceSnapshot: number
} {
  const state = (coordinator as unknown as {
    rooms: Map<string, { tailBytes: number; updatesSinceSnapshot: number }>
  }).rooms.get(ROOM)
  if (!state) throw new Error('room state missing')
  return { tailBytes: state.tailBytes, updatesSinceSnapshot: state.updatesSinceSnapshot }
}
