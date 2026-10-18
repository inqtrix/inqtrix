import { getSchema, type JSONContent } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { EditorState } from '@tiptap/pm/state'
import { initProseMirrorDoc, updateYFragment } from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  createEditorSchemaExtensions,
  editorCollaborationRoom,
  editorJsonToYDoc,
  editorYDocToJson,
  getEditorSchemaFingerprint,
  parseEditorMarkdown,
  serializeEditorJson,
  transformToInqtrixSuggestionTransaction,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import type { ConnectionContext } from '../src/contracts'
import { DocumentCoordinator } from '../src/documentCoordinator'
import { hashString } from '../src/documentState'
import { ApiRequestError, CollaborationError } from '../src/errors'
import { InstanceLeaseManager } from '../src/instanceLease'
import { SidecarMetrics } from '../src/metrics'
import { CollaborationOperations } from '../src/operations'
import { collectSuggestionRecords } from '../src/suggestPolicy'
import {
  FakeCollaborationApi,
  USER_ID,
  documentState,
  settings,
  silentLogger,
} from './helpers'

const DOCUMENT_ID = 'ed_test'
const ROOM = editorCollaborationRoom(DOCUMENT_ID, 1)
const PATCH_ID = '22222222-2222-4222-8222-222222222222'
const SUGGESTION_ID = '33333333-3333-4333-8333-333333333333'
const COMMAND_ID = '44444444-4444-4444-8444-444444444444'
const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

describe('FastAPI-to-Node operations', () => {
  it('persists an accept decision with CAS, decision metadata, and the closed patch state', async () => {
    const plain = schema.nodeFromJSON(parseEditorMarkdown('Hello'))
    const state = EditorState.create({ schema, doc: plain })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', 6),
      state,
      { authorId: USER_ID, createdAt: 1_784_112_000, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc
    const document = editorJsonToYDoc(tracked.toJSON())
    const fixture = await operationsFixture(document, 5)

    const response = await fixture.operations.decide(DOCUMENT_ID, {
      actor_user_id: USER_ID,
      command_id: COMMAND_ID,
      decision: 'accept',
      expected_sequence: 5,
      generation: 1,
      patch_ids: [PATCH_ID],
    })

    expect(response).toEqual({
      command_id: COMMAND_ID,
      decision: 'accept',
      patch_ids: [PATCH_ID],
      sequence: 6,
      suggestion_ids: [SUGGESTION_ID],
    })
    expect(fixture.api.persisted[0]).toMatchObject({
      actorKind: 'human',
      changeKind: 'decision',
      commandId: COMMAND_ID,
      decision: 'accept',
      expectedSequence: 5,
      patches: [{
        activeSuggestionIds: [],
        authorId: USER_ID,
        createdAt: 1_784_112_000,
        kinds: [],
        patchId: PATCH_ID,
      }],
      suggestionIds: [SUGGESTION_ID],
    })
    await expect(fixture.operations.decide(DOCUMENT_ID, {
      actor_user_id: USER_ID,
      command_id: COMMAND_ID,
      decision: 'accept',
      expected_sequence: 5,
      generation: 1,
      patch_ids: [PATCH_ID],
    })).resolves.toEqual(response)
    expect(fixture.api.persisted).toHaveLength(1)
    await expect(fixture.operations.decide(DOCUMENT_ID, {
      actor_user_id: USER_ID,
      command_id: COMMAND_ID,
      decision: 'reject',
      expected_sequence: 5,
      generation: 1,
      patch_ids: [PATCH_ID],
    })).rejects.toThrowError('sequence_conflict')
    await fixture.close()
  })

  it('reconciles a committed decision retry after the persistence response is lost', async () => {
    const plain = schema.nodeFromJSON(parseEditorMarkdown('Hello'))
    const state = EditorState.create({ schema, doc: plain })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', 6),
      state,
      { authorId: USER_ID, createdAt: 1_784_112_000, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc
    const document = editorJsonToYDoc(tracked.toJSON())
    const fixture = await operationsFixture(document, 5)
    fixture.api.persistResponseErrorAfterCommit = new ApiRequestError(
      503,
      'internal_api_timeout',
    )
    const request = {
      actor_user_id: USER_ID,
      command_id: COMMAND_ID,
      decision: 'accept',
      expected_sequence: 5,
      generation: 1,
      patch_ids: [PATCH_ID],
    }

    await expect(fixture.operations.decide(DOCUMENT_ID, request))
      .rejects.toThrowError('service_unavailable')
    expect(collectSuggestionRecords(editorYDocToJson(document)).size).toBe(1)
    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(true)
    expect(fixture.coordinator.shouldSnapshot(ROOM)).toBe(false)
    expect(() => fixture.coordinator.assertJoinAllowed(ROOM)).toThrowError('restarting')

    await expect(fixture.operations.decide(DOCUMENT_ID, request)).resolves.toEqual({
      command_id: COMMAND_ID,
      decision: 'accept',
      patch_ids: [PATCH_ID],
      sequence: 6,
      suggestion_ids: [SUGGESTION_ID],
    })
    expect(fixture.api.persisted).toHaveLength(1)
    expect(collectSuggestionRecords(editorYDocToJson(document)).size).toBe(0)
    expect(fixture.coordinator.getPersistedSequence(ROOM)).toBe(6)
    await fixture.close()
  })

  it('replays an accepted text modification idempotently as one decision', async () => {
    const plain = schema.nodeFromJSON(parseEditorMarkdown('Hello'))
    const state = EditorState.create({ schema, doc: plain })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('World', 1, 6),
      state,
      { authorId: USER_ID, createdAt: 1_784_112_000, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc
    const document = editorJsonToYDoc(tracked.toJSON())
    const fixture = await operationsFixture(document, 5)
    const request = {
      actor_user_id: USER_ID,
      command_id: COMMAND_ID,
      decision: 'accept',
      expected_sequence: 5,
      generation: 1,
      patch_ids: [PATCH_ID],
    }

    const first = await fixture.operations.decide(DOCUMENT_ID, request)
    await expect(fixture.operations.decide(DOCUMENT_ID, request)).resolves.toEqual(first)

    expect(first).toMatchObject({
      decision: 'accept',
      sequence: 6,
      suggestion_ids: [SUGGESTION_ID],
    })
    expect(fixture.api.persisted).toHaveLength(1)
    expect(collectSuggestionRecords(editorYDocToJson(document)).size).toBe(0)
    await fixture.close()
  })

  it('publishes target Markdown as an assistant suggestion in the same durable flow', async () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const fixture = await operationsFixture(document, 8)

    const response = await fixture.operations.publishSuggestion(DOCUMENT_ID, {
      actor_kind: 'assistant',
      actor_user_id: USER_ID,
      command_id: COMMAND_ID,
      expected_sequence: 8,
      generation: 1,
      patch_id: PATCH_ID,
      target_markdown: 'Hello world',
    })

    expect(response).toMatchObject({
      command_id: COMMAND_ID,
      patch_id: PATCH_ID,
      sequence: 9,
      suggestion_ids: [expect.stringMatching(/^[0-9a-f-]{36}$/)],
    })
    expect(fixture.api.persisted[0]).toMatchObject({
      actorKind: 'assistant',
      actorUserId: USER_ID,
      changeKind: 'suggestion',
      commandId: COMMAND_ID,
      decision: null,
      expectedSequence: 8,
      patches: [{
        activeSuggestionIds: [expect.stringMatching(/^[0-9a-f-]{36}$/)],
        authorId: USER_ID,
        kinds: ['insertion'],
        patchId: PATCH_ID,
      }],
    })
    await expect(fixture.operations.publishSuggestion(DOCUMENT_ID, {
      actor_kind: 'assistant',
      actor_user_id: USER_ID,
      command_id: COMMAND_ID,
      expected_sequence: 8,
      generation: 1,
      patch_id: PATCH_ID,
      target_markdown: 'Hello world',
    })).resolves.toEqual(response)
    expect(fixture.api.persisted).toHaveLength(1)
    await fixture.close()
  })

  it('reconciles a committed suggestion publication after the response is lost', async () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const fixture = await operationsFixture(document, 8)
    fixture.api.persistResponseErrorAfterCommit = new ApiRequestError(
      503,
      'internal_api_timeout',
    )
    const request = {
      actor_kind: 'assistant',
      actor_user_id: USER_ID,
      command_id: COMMAND_ID,
      expected_sequence: 8,
      generation: 1,
      patch_id: PATCH_ID,
      target_markdown: 'Hello world',
    }

    await expect(fixture.operations.publishSuggestion(DOCUMENT_ID, request))
      .rejects.toThrowError('service_unavailable')
    expect(collectSuggestionRecords(editorYDocToJson(document)).size).toBe(0)
    expect(fixture.coordinator.requiresReconstruction(ROOM)).toBe(true)
    expect(fixture.coordinator.shouldSnapshot(ROOM)).toBe(false)

    const replay = await fixture.operations.publishSuggestion(DOCUMENT_ID, request)
    expect(replay).toMatchObject({
      command_id: COMMAND_ID,
      patch_id: PATCH_ID,
      sequence: 9,
      suggestion_ids: [expect.stringMatching(/^[0-9a-f-]{36}$/)],
    })
    expect(fixture.api.persisted).toHaveLength(1)
    expect(collectSuggestionRecords(editorYDocToJson(document)).size).toBe(1)
    expect(fixture.coordinator.getPersistedSequence(ROOM)).toBe(9)
    await fixture.close()
  })

  it('returns conversion and projection fields expected by the Python client', async () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('# Shared'))
    const fixture = await operationsFixture(document, 3)

    const conversion = await fixture.operations.convert({
      document_id: DOCUMENT_ID,
      markdown: '# Shared',
      max_document_bytes: 10 * 1024 * 1024,
      schema_version: 2,
    })
    expect(conversion).toMatchObject({
      projection_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      projection_markdown: expect.any(String),
      schema_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      schema_version: 2,
      snapshot: {
        covered_sequence: 0,
        state_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
        state_update_base64: expect.any(String),
        state_vector_base64: expect.any(String),
      },
    })

    const projection = await fixture.operations.project(DOCUMENT_ID, {
      generation: 1,
      include_snapshot: true,
      minimum_sequence: 3,
    })
    expect(projection).toMatchObject({
      generation: 1,
      projection_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      projection_markdown: expect.any(String),
      schema_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      schema_version: 2,
      sequence: 3,
      snapshot: {
        covered_sequence: 3,
        state_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
        state_update_base64: expect.any(String),
        state_vector_base64: expect.any(String),
      },
    })
    await fixture.close()
  })

  it('converts an empty editor document to canonical collaboration state', async () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('Seed'))
    const fixture = await operationsFixture(document, 0)

    const conversion = await fixture.operations.convert({
      document_id: DOCUMENT_ID,
      markdown: '',
      max_document_bytes: 10 * 1024 * 1024,
      schema_version: 2,
    })

    expect(conversion).toMatchObject({
      projection_markdown: '',
      schema_version: 2,
      snapshot: {
        covered_sequence: 0,
        state_update_base64: expect.any(String),
      },
    })
    await fixture.close()
  })

  it('converts a line break the editor wrote inside a table cell into the hard break it stands for', async () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('Seed'))
    const fixture = await operationsFixture(document, 0)
    const markdown = '| Head |\n| --- |\n| first<br>second |\n'

    const conversion = await fixture.operations.convert({
      document_id: DOCUMENT_ID,
      markdown,
      max_document_bytes: 10 * 1024 * 1024,
      schema_version: 2,
    }) as { projection_markdown: string; snapshot: { state_update_base64: string } }

    const activated = new Y.Doc()
    try {
      Y.applyUpdate(activated, Buffer.from(conversion.snapshot.state_update_base64, 'base64'))
      const cell = editorYDocToJson(activated).content?.[0]?.content?.[1]?.content?.[0]?.content?.[0]
      expect(cell?.content).toEqual([
        { type: 'text', text: 'first' },
        { type: 'hardBreak' },
        { type: 'text', text: 'second' },
      ])
    } finally {
      activated.destroy()
    }
    // The projection is the editor's own Markdown again, not escaped text.
    expect(conversion.projection_markdown).toContain('| first<br>second |')
    await fixture.close()
  })

  it('stores projection with the snapshot and reports compaction failure without rollback', async () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('# Shared'))
    const fixture = await operationsFixture(document, 3)
    fixture.api.compactImplementation = async () => {
      throw new Error('maintenance unavailable')
    }

    await expect(fixture.operations.storeSnapshot(ROOM, document)).resolves.toEqual({
      coveredSequence: 3,
      currentSequenceCovered: true,
    })
    expect(fixture.api.snapshots).toHaveLength(1)
    expect(fixture.api.snapshots[0]).toMatchObject({
      coveredSequence: 3,
      documentId: DOCUMENT_ID,
      generation: 1,
      projectionHash: expect.stringMatching(/^[0-9a-f]{64}$/),
      projectionMarkdown: expect.stringContaining('# Shared'),
    })
    expect(fixture.api.snapshots[0]?.projectionHash).toBe(
      hashString(fixture.api.snapshots[0]?.projectionMarkdown ?? ''),
    )
    expect(fixture.api.compactions).toEqual([{
      documentId: DOCUMENT_ID,
      fence: fixture.api.fence,
      generation: 1,
    }])
    expect(fixture.metrics.render()).toContain(
      'inqtrix_collaboration_compaction_runs_total{status="failure"} 1',
    )
    expect(fixture.warnings).toContain('collaboration_compaction_failed')
    await fixture.close()
  })

  it('records how long each stored snapshot took from capture until the store confirmed it', async () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('# Shared'))
    const fixture = await operationsFixture(document, 3)
    let fail = true
    fixture.api.snapshotImplementation = async () => {
      await new Promise((resolve) => setTimeout(resolve, 60))
      if (fail) throw new Error('snapshot unavailable')
    }

    await expect(fixture.operations.storeSnapshot(ROOM, document)).rejects.toThrow()
    expect(fixture.metrics.render()).not.toContain('inqtrix_collaboration_snapshot_seconds_count')
    fail = false
    await expect(fixture.operations.storeSnapshot(ROOM, document)).resolves.toEqual({
      coveredSequence: 3,
      currentSequenceCovered: true,
    })

    const rendered = fixture.metrics.render()
    expect(rendered).toContain('inqtrix_collaboration_snapshot_seconds_count 1\n')
    const sum = Number(rendered
      .split('\n')
      .find((line) => line.startsWith('inqtrix_collaboration_snapshot_seconds_sum '))
      ?.split(' ')
      .at(-1))
    // The store's own 60 ms lie inside the measured span.
    expect(sum).toBeGreaterThan(0.055)
    await fixture.close()
  })

  it('runs global maintenance independently of document snapshots', async () => {
    const document = editorJsonToYDoc(parseEditorMarkdown('# Shared'))
    const fixture = await operationsFixture(document, 3)

    await fixture.operations.runMaintenance()

    expect(fixture.api.snapshots).toHaveLength(0)
    expect(fixture.api.compactions).toEqual([{ fence: fixture.api.fence }])
    expect(fixture.metrics.render()).toContain(
      'inqtrix_collaboration_compaction_runs_total{status="success"} 1',
    )
    await fixture.close()
  })
})

// A decision or an AI publication is based on the projection the browser
// read before the click. Another writer who types in the meantime moves the
// room on; the command still applies to the room as it is now, never to the
// state it was based on, and what the other writer typed stays exactly once.
describe('decisions and AI publications while another writer types', () => {
  const SHARED = [
    '# Abschnitt A',
    '',
    'Der Assistent ersetzt hier ein Wort.',
    '',
    'Notizen B:',
    '',
    '## Abschnitt C',
    '',
    'Notizen C:',
    '',
  ].join('\n')

  it('publishes onto the room as it is now when another writer typed after the base was read', async () => {
    const room = await typingRoom(editorJsonToYDoc(parseEditorMarkdown(SHARED)), 5)
    const base = await room.projectMarkdown(5)
    const target = base.replace('ersetzt', 'tauscht')
    await room.typeAtEnd('Notizen B:', ' MARKE1')
    expect(room.coordinator.getPersistedSequence(ROOM)).toBe(6)
    const request = {
      actor_kind: 'assistant',
      actor_user_id: USER_ID,
      base_markdown: base,
      command_id: COMMAND_ID,
      expected_sequence: 5,
      generation: 1,
      patch_id: PATCH_ID,
      target_markdown: target,
    }

    const response = await room.operations.publishSuggestion(DOCUMENT_ID, request)

    expect(response).toMatchObject({ command_id: COMMAND_ID, patch_id: PATCH_ID, sequence: 7 })
    expect(room.api.persisted).toHaveLength(2)
    // The store's compare-and-set is the room's current sequence, not the
    // sequence the browser based the publication on.
    expect(room.api.persisted[1]).toMatchObject({
      actorKind: 'assistant',
      changeKind: 'suggestion',
      expectedSequence: 6,
    })
    const final = projectionOf(room.document, 'final')
    const original = projectionOf(room.document, 'original')
    expect(final).toContain('Der Assistent tauscht hier ein Wort.')
    expect(original).toContain('Der Assistent ersetzt hier ein Wort.')
    expect(final.split('MARKE1')).toHaveLength(2)
    expect(original.split('MARKE1')).toHaveLength(2)
    // The assistant's marks cover the changed word, whole, and nothing the
    // other writer typed.
    expect(markedText(room.document, PATCH_ID)).toEqual({ deleted: 'ersetzt', inserted: 'tauscht' })
    expect(room.metrics.render()).toContain(
      'inqtrix_collaboration_publication_rebase_total{outcome="rebased"} 1',
    )

    await expect(room.operations.publishSuggestion(DOCUMENT_ID, request)).resolves.toEqual(response)
    expect(room.api.persisted).toHaveLength(2)
    await room.close()
  })

  it('places the AI section unchanged when nobody typed since the base was read', async () => {
    const room = await typingRoom(editorJsonToYDoc(parseEditorMarkdown(SHARED)), 5)
    const base = await room.projectMarkdown(5)

    await expect(room.operations.publishSuggestion(DOCUMENT_ID, {
      actor_kind: 'assistant',
      actor_user_id: USER_ID,
      base_markdown: base,
      command_id: COMMAND_ID,
      expected_sequence: 5,
      generation: 1,
      patch_id: PATCH_ID,
      target_markdown: base.replace('ersetzt', 'tauscht'),
    })).resolves.toMatchObject({ sequence: 6 })

    expect(room.api.persisted[0]).toMatchObject({ expectedSequence: 5 })
    expect(markedText(room.document, PATCH_ID)).toEqual({ deleted: 'ersetzt', inserted: 'tauscht' })
    expect(room.metrics.render()).toContain(
      'inqtrix_collaboration_publication_rebase_total{outcome="direct"} 1',
    )
    await room.close()
  })

  it('refuses a publication whose AI section another writer changed, and stores nothing', async () => {
    const room = await typingRoom(editorJsonToYDoc(parseEditorMarkdown(SHARED)), 5)
    const base = await room.projectMarkdown(5)
    await room.typeAfter('Der Assistent er', 'X')
    const before = projectionOf(room.document, 'original')

    const refusal = await room.operations.publishSuggestion(DOCUMENT_ID, {
      actor_kind: 'assistant',
      actor_user_id: USER_ID,
      base_markdown: base,
      command_id: COMMAND_ID,
      expected_sequence: 5,
      generation: 1,
      patch_id: PATCH_ID,
      target_markdown: base.replace('ersetzt', 'tauscht'),
    }).then(() => null, (error: unknown) => error)

    expect(refusal).toBeInstanceOf(CollaborationError)
    expect(refusal).toMatchObject({
      httpStatus: 409,
      reason: 'base_changed',
      suggestionReason: 'hunk_touched',
    })
    expect(room.api.persisted).toHaveLength(1)
    expect(projectionOf(room.document, 'original')).toBe(before)
    expect(projectionOf(room.document, 'final')).toBe(before)
    expect(room.coordinator.requiresReconstruction(ROOM)).toBe(false)
    expect(room.metrics.render()).toContain(
      'inqtrix_collaboration_publication_rebase_total{outcome="conflict",reason="hunk_touched"} 1',
    )
    await room.close()
  })

  it('keeps the exact sequence rule for a publication that names no base', async () => {
    const room = await typingRoom(editorJsonToYDoc(parseEditorMarkdown(SHARED)), 5)
    const base = await room.projectMarkdown(5)
    await room.typeAtEnd('Notizen B:', ' MARKE1')

    await expect(room.operations.publishSuggestion(DOCUMENT_ID, {
      actor_kind: 'assistant',
      actor_user_id: USER_ID,
      command_id: COMMAND_ID,
      expected_sequence: 5,
      generation: 1,
      patch_id: PATCH_ID,
      target_markdown: base.replace('ersetzt', 'tauscht'),
    })).rejects.toThrowError('sequence_conflict')
    expect(room.api.persisted).toHaveLength(1)
    await room.close()
  })

  it('refuses a base the room has not reached yet', async () => {
    const room = await typingRoom(editorJsonToYDoc(parseEditorMarkdown(SHARED)), 5)
    const base = await room.projectMarkdown(5)

    await expect(room.operations.publishSuggestion(DOCUMENT_ID, {
      actor_kind: 'assistant',
      actor_user_id: USER_ID,
      base_markdown: base,
      command_id: COMMAND_ID,
      expected_sequence: 6,
      generation: 1,
      patch_id: PATCH_ID,
      target_markdown: base.replace('ersetzt', 'tauscht'),
    })).rejects.toThrowError('sequence_conflict')
    expect(room.api.persisted).toHaveLength(0)
    await room.close()
  })

  it('decides a suggestion when another writer typed after the decision was based', async () => {
    const plain = schema.nodeFromJSON(parseEditorMarkdown('Hello\n\nNotizen B:'))
    const state = EditorState.create({ schema, doc: plain })
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText('!', 6),
      state,
      { authorId: USER_ID, createdAt: 1_784_112_000, patchId: PATCH_ID },
      () => SUGGESTION_ID,
    ).doc
    const room = await typingRoom(editorJsonToYDoc(tracked.toJSON()), 5)
    await room.typeAtEnd('Notizen B:', ' MARKE1')
    const request = {
      actor_user_id: USER_ID,
      command_id: COMMAND_ID,
      decision: 'accept',
      expected_sequence: 5,
      generation: 1,
      patch_ids: [PATCH_ID],
    }

    const response = await room.operations.decide(DOCUMENT_ID, request)

    expect(response).toEqual({
      command_id: COMMAND_ID,
      decision: 'accept',
      patch_ids: [PATCH_ID],
      sequence: 7,
      suggestion_ids: [SUGGESTION_ID],
    })
    expect(room.api.persisted[1]).toMatchObject({
      changeKind: 'decision',
      decision: 'accept',
      expectedSequence: 6,
    })
    expect(collectSuggestionRecords(editorYDocToJson(room.document)).size).toBe(0)
    expect(projectionOf(room.document, 'original')).toBe('Hello!\n\nNotizen B: MARKE1')
    await expect(room.operations.decide(DOCUMENT_ID, request)).resolves.toEqual(response)
    expect(room.api.persisted).toHaveLength(2)

    await expect(room.operations.decide(DOCUMENT_ID, {
      ...request,
      command_id: '66666666-6666-4666-8666-666666666666',
      expected_sequence: 8,
    })).rejects.toThrowError('sequence_conflict')
    await room.close()
  })
})

const TYPIST_ID = '77777777-7777-4777-8777-777777777777'

/** A room whose store hands out the next sequence for every row, and a
 * second writer who types into it through the same acceptance path a
 * browser's update takes. */
async function typingRoom(document: Y.Doc, sequence: number): Promise<{
  api: FakeCollaborationApi
  close: () => Promise<void>
  coordinator: DocumentCoordinator
  document: Y.Doc
  metrics: SidecarMetrics
  operations: CollaborationOperations
  projectMarkdown: (minimumSequence: number) => Promise<string>
  typeAfter: (prefix: string, text: string) => Promise<void>
  typeAtEnd: (needle: string, text: string) => Promise<void>
}> {
  const fixture = await operationsFixture(document, sequence)
  let stored = sequence
  fixture.api.persistImplementation = async () => {
    stored += 1
    return { duplicate: false, persistedSequence: stored, sequence: stored }
  }
  const configured = settings()
  const schemaHash = await getEditorSchemaFingerprint()
  let connections = 0
  const type = async (position: (doc: ProseMirrorNode) => number, text: string): Promise<void> => {
    connections += 1
    const connectionId = `typist-${connections}`
    const context: ConnectionContext = {
      access: 'edit',
      documentId: DOCUMENT_ID,
      expiresAt: Date.now() / 1_000 + 600,
      generation: 1,
      leaseId: 'typist-lease',
      policyCursor: 0,
      protocolVersion: configured.protocolVersion,
      schemaHash,
      schemaVersion: configured.schemaVersion,
      sessionId: connectionId,
      tenantId: configured.tenantId,
      user: { color: '#123456', id: TYPIST_ID, name: 'Typist' },
    }
    const update = typedUpdate(document, 7_000 + connections, position, text)
    expect(await fixture.coordinator.prepareClientUpdate({
      allowNoop: false,
      connectionId,
      context,
      document,
      room: ROOM,
      update,
    })).toBe('pending')
    Y.applyUpdate(document, update)
    fixture.coordinator.finishClientUpdate(connectionId, document)
  }
  return {
    ...fixture,
    document,
    projectMarkdown: async (minimumSequence) => {
      const projection = await fixture.operations.project(DOCUMENT_ID, {
        generation: 1,
        minimum_sequence: minimumSequence,
      })
      expect(projection.sequence).toBe(minimumSequence)
      return projection.projection_markdown as string
    },
    typeAfter: (prefix, text) => type((doc) => textPosition(doc, prefix), text),
    typeAtEnd: (needle, text) => type((doc) => textblockEnd(doc, needle), text),
  }
}

/** The update a second client sends for typing *text* at a position of the
 * shared document, written through y-tiptap as the editor writes it. */
function typedUpdate(
  base: Y.Doc,
  clientId: number,
  position: (doc: ProseMirrorNode) => number,
  text: string,
): Uint8Array {
  const document = new Y.Doc()
  document.clientID = clientId
  Y.applyUpdate(document, Y.encodeStateAsUpdate(base))
  const fragment = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, schema)
  const state = EditorState.create({ doc: initialized.doc, schema })
  const target = state.tr.insertText(text, position(state.doc)).doc
  const vector = Y.encodeStateVector(document)
  document.transact(() => updateYFragment(document, fragment, target, initialized.meta))
  const update = Y.encodeStateAsUpdate(document, vector)
  document.destroy()
  return update
}

function textPosition(doc: ProseMirrorNode, prefix: string): number {
  let found: number | null = null
  doc.descendants((node, position) => {
    if (found !== null) return false
    if (node.isTextblock && node.textContent.startsWith(prefix)) {
      found = position + 1 + prefix.length
      return false
    }
    return true
  })
  if (found === null) throw new Error(`no text block starts with ${prefix}`)
  return found
}

function textblockEnd(doc: ProseMirrorNode, needle: string): number {
  let found: number | null = null
  doc.descendants((node, position) => {
    if (found !== null) return false
    if (node.isTextblock && node.textContent.includes(needle)) {
      found = position + 1 + node.content.size
      return false
    }
    return true
  })
  if (found === null) throw new Error(`no text block contains ${needle}`)
  return found
}

function projectionOf(document: Y.Doc, mode: 'final' | 'original'): string {
  return serializeEditorJson(editorYDocToJson(document), mode).trim()
}

/** The text a patch's suggestions delete and insert, each joined in
 * document order. */
function markedText(document: Y.Doc, patchId: string): { deleted: string; inserted: string } {
  const result = { deleted: '', inserted: '' }
  const visit = (node: JSONContent): void => {
    if (node.type === 'text') {
      for (const mark of node.marks ?? []) {
        if (mark.attrs?.patchId !== patchId) continue
        if (mark.type === 'deletion') result.deleted += node.text ?? ''
        if (mark.type === 'insertion') result.inserted += node.text ?? ''
      }
    }
    for (const child of node.content ?? []) visit(child)
  }
  visit(editorYDocToJson(document))
  return result
}

async function operationsFixture(document: Y.Doc, sequence: number): Promise<{
  api: FakeCollaborationApi
  close: () => Promise<void>
  coordinator: DocumentCoordinator
  metrics: SidecarMetrics
  operations: CollaborationOperations
  warnings: string[]
}> {
  const api = new FakeCollaborationApi()
  const configured = settings()
  api.persistImplementation = async () => ({
    duplicate: false,
    persistedSequence: sequence + 1,
    sequence: sequence + 1,
  })
  const metrics = new SidecarMetrics()
  const warnings: string[] = []
  const logger = {
    ...silentLogger,
    warn: (event: string): void => {
      warnings.push(event)
    },
  }
  api.loadedState = await documentState(DOCUMENT_ID, document, 1, sequence)
  const lease = new InstanceLeaseManager(api, configured, silentLogger, metrics, () => undefined)
  await lease.start()
  const coordinator = new DocumentCoordinator(api, lease, configured, silentLogger, metrics)
  coordinator.initialize(ROOM, sequence)
  const operations = new CollaborationOperations(
    api,
    coordinator,
    lease,
    configured,
    async (room) => {
      expect(room).toBe(ROOM)
      return { document, release: async () => undefined }
    },
    logger,
    metrics,
  )
  return {
    api,
    close: () => lease.stop(),
    coordinator,
    metrics,
    operations,
    warnings,
  }
}
