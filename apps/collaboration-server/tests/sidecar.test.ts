import { mkdtempSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import WebSocket from 'ws'
import { getSchema } from '@tiptap/core'
import { EditorState } from '@tiptap/pm/state'
import { initProseMirrorDoc, updateYFragment } from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  createEditorSchemaExtensions,
  editorCollaborationRoom,
  editorYDocToJson,
  parseEditorMarkdown,
  serializeEditorJson,
  transformToInqtrixSuggestionTransaction,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import type { IntrospectedLease } from '../src/contracts'
import { hashBytes } from '../src/documentState'
import { ApiRequestError, CloseCodes, CollaborationError } from '../src/errors'
import { SidecarMetrics } from '../src/metrics'
import { CollaborationSidecar } from '../src/sidecar'
import { VerificationFaultGate } from '../src/verificationFault'
import {
  FakeCollaborationApi,
  USER_ID,
  deferred,
  documentState,
  markdownDocument,
  settings,
  silentLogger,
} from './helpers'

const DOCUMENT_ID = 'ed_test'
const ROOM = editorCollaborationRoom(DOCUMENT_ID, 1)
const editorSchema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

describe('private sidecar listener', () => {
  it('disconnects a non-draining receiver without penalizing concurrent draining broadcasts', () => {
    const configured = settings({ maxQueuedFrames: 2 })
    const sidecar = new CollaborationSidecar(configured, {
      api: new FakeCollaborationApi(),
      logger: silentLogger,
    })
    const harness = sidecar as unknown as OutboundHarness
    const slow = fakeOutboundSocket(false)
    const draining = fakeOutboundSocket(true)
    const payload = new Uint8Array(8)

    for (let index = 0; index < 3; index += 1) {
      harness.sendHocuspocusPayload('slow', slow.socket, payload)
      harness.sendHocuspocusPayload('draining', draining.socket, payload)
    }

    expect(slow.close).toHaveBeenCalledWith(4429, 'rate_limited')
    expect(draining.close).not.toHaveBeenCalled()
    expect(draining.send).toHaveBeenCalledTimes(3)
  })

  it('keeps held broadcasts charged through a flush until send callbacks drain', () => {
    const configured = settings({ maxQueuedFrames: 2 })
    const sidecar = new CollaborationSidecar(configured, {
      api: new FakeCollaborationApi(),
      logger: silentLogger,
    })
    const harness = sidecar as unknown as OutboundHarness
    const receiver = fakeOutboundSocket(false)
    const payload = protocolFrame(ROOM, 0, new Uint8Array([0]))
    let blocked = true
    harness.coordinator.isBroadcastBlocked = () => blocked

    harness.sendHocuspocusPayload('held', receiver.socket, payload)
    harness.sendHocuspocusPayload('held', receiver.socket, payload)
    expect(receiver.send).not.toHaveBeenCalled()

    blocked = false
    harness.flushHeldOutbound(ROOM)
    expect(receiver.send).toHaveBeenCalledTimes(2)
    harness.sendHocuspocusPayload('held', receiver.socket, payload)

    expect(receiver.close).toHaveBeenCalledWith(4429, 'rate_limited')
  })

  it('rejects direct WebSocket access without the gateway bearer secret', async () => {
    const configured = settings({ policyPollMs: 60_000 })
    const sidecar = new CollaborationSidecar(configured, {
      api: new FakeCollaborationApi(),
      logger: silentLogger,
    })
    await sidecar.start()
    const port = sidecar.address?.port
    expect(port).toBeTypeOf('number')

    const status = await rejectedUpgrade(`ws://127.0.0.1:${port}/collaboration`)
    expect(status).toBe(401)
    await sidecar.stop()
  })

  it('exposes readiness, metrics, and secret-protected internal conversion', async () => {
    const configured = settings({ policyPollMs: 60_000 })
    const sidecar = new CollaborationSidecar(configured, {
      api: new FakeCollaborationApi(),
      logger: silentLogger,
    })
    await sidecar.start()
    const base = `http://127.0.0.1:${sidecar.address?.port}`

    const ready = await fetch(`${base}/health/ready`)
    expect(ready.status).toBe(200)
    await expect(ready.json()).resolves.toMatchObject({
      mode: 'single_replica',
      status: 'ready',
    })
    const metrics = await fetch(`${base}/metrics`)
    expect(metrics.status).toBe(200)
    expect(await metrics.text()).toContain('inqtrix_collaboration_instance_ready 1')

    const unauthorized = await fetch(`${base}/internal/convert`, {
      body: JSON.stringify({}),
      headers: { 'Content-Type': 'application/json' },
      method: 'POST',
    })
    expect(unauthorized.status).toBe(401)

    const converted = await fetch(`${base}/internal/convert`, {
      body: JSON.stringify({
        document_id: 'ed_test',
        markdown: '# Shared',
        max_document_bytes: configured.documentLimitBytes,
        schema_version: configured.schemaVersion,
      }),
      headers: {
        Authorization: `Bearer ${configured.secret}`,
        'Content-Type': 'application/json',
      },
      method: 'POST',
    })
    expect(converted.status).toBe(200)
    await expect(converted.json()).resolves.toMatchObject({
      projection_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      schema_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      snapshot: {
        state_hash: expect.stringMatching(/^[0-9a-f]{64}$/),
      },
    })
    await sidecar.stop()
  })

  it('broadcasts comment invalidations without revalidating document leases', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const harness = sidecar as unknown as CommentPolicyHarness
    await vi.waitFor(() => expect(harness.policyPollInFlight).toBeNull())
    const first = {
      context: {
        documentId: DOCUMENT_ID,
        expiresAt: Date.now() / 1_000 + 60,
        generation: 1,
        leaseId: 'lease-first',
        policyCursor: 0,
        tenantId: configured.tenantId,
        user: { id: USER_ID },
      },
      sendStateless: vi.fn(),
    }
    const second = {
      context: {
        documentId: DOCUMENT_ID,
        expiresAt: Date.now() / 1_000 + 60,
        generation: 1,
        leaseId: 'lease-second',
        policyCursor: 0,
        tenantId: configured.tenantId,
        user: { id: '22222222-2222-4222-8222-222222222222' },
      },
      sendStateless: vi.fn(),
    }
    harness.hocuspocus.documents.set(ROOM, {
      getConnections: () => [first, second],
      name: ROOM,
    })
    api.policyImplementation = async () => ({
      cursor: 3,
      events: [
        {
          id: 1,
          resourceId: DOCUMENT_ID,
          resourceType: 'editor_document',
          scope: 'collaboration_comment_changed',
          targetUserId: USER_ID,
        },
        {
          id: 2,
          resourceId: DOCUMENT_ID,
          resourceType: 'editor_document',
          scope: 'collaboration_comment_changed',
          targetUserId: second.context.user.id,
        },
        {
          id: 3,
          resourceId: DOCUMENT_ID,
          resourceType: 'editor_document',
          scope: 'collaboration_comment_mention',
          targetUserId: USER_ID,
        },
      ],
      resetRequired: false,
    })

    await harness.pollPolicyEvents()

    expect(first.sendStateless).toHaveBeenNthCalledWith(
      1,
      JSON.stringify({
        document_id: DOCUMENT_ID,
        type: 'collaboration_comment_changed',
      }),
    )
    expect(first.sendStateless).toHaveBeenNthCalledWith(
      2,
      JSON.stringify({
        document_id: DOCUMENT_ID,
        type: 'collaboration_comment_mentioned',
      }),
    )
    expect(second.sendStateless).toHaveBeenCalledTimes(1)
    expect(second.sendStateless).toHaveBeenCalledWith(
      JSON.stringify({
        document_id: DOCUMENT_ID,
        type: 'collaboration_comment_changed',
      }),
    )

    harness.hocuspocus.documents.delete(ROOM)
    await sidecar.stop()
  })

  it('revalidates only account sockets whose lease check predates a policy event', async () => {
    const api = new FakeCollaborationApi()
    api.loadedState = await documentState(
      DOCUMENT_ID,
      markdownDocument('Policy cursor'),
    )
    const configured = settings({ policyPollMs: 60_000 })
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const harness = sidecar as unknown as GuestPolicyHarness
    await vi.waitFor(() => expect(harness.policyPollInFlight).toBeNull())
    const stale = policyConnection({
      id: USER_ID,
      kind: 'user',
      policyCursor: 11,
      socketId: 'stale-account-socket',
    })
    const covered = policyConnection({
      id: USER_ID,
      kind: 'user',
      policyCursor: 12,
      socketId: 'covered-account-socket',
    })
    harness.hocuspocus.documents.set(ROOM, {
      getConnections: () => [stale, covered],
      name: ROOM,
    })
    api.policyImplementation = async () => ({
      cursor: 12,
      events: [{
        id: 12,
        resourceId: DOCUMENT_ID,
        resourceType: 'editor_document',
        scope: 'share:accepted',
        targetUserId: USER_ID,
      }],
      resetRequired: false,
    })

    await harness.pollPolicyEvents()

    expect(api.loads).toHaveLength(1)
    expect(stale.requestToken).toHaveBeenCalledTimes(1)
    expect(covered.requestToken).not.toHaveBeenCalled()

    harness.hocuspocus.documents.delete(ROOM)
    await sidecar.stop()
  })

  it('skips an authoritative generation read when every socket covers the policy event', async () => {
    const api = new FakeCollaborationApi()
    api.loadedState = await documentState(
      DOCUMENT_ID,
      markdownDocument('Covered policy cursor'),
    )
    const configured = settings({ policyPollMs: 60_000 })
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const harness = sidecar as unknown as GuestPolicyHarness
    await vi.waitFor(() => expect(harness.policyPollInFlight).toBeNull())
    const first = policyConnection({
      id: USER_ID,
      kind: 'user',
      policyCursor: 12,
      socketId: 'first-covered-account-socket',
    })
    const second = policyConnection({
      id: USER_ID,
      kind: 'user',
      policyCursor: 13,
      socketId: 'second-covered-account-socket',
    })
    harness.hocuspocus.documents.set(ROOM, {
      getConnections: () => [first, second],
      name: ROOM,
    })
    api.policyImplementation = async () => ({
      cursor: 12,
      events: [{
        id: 12,
        resourceId: DOCUMENT_ID,
        resourceType: 'editor_document',
        scope: 'share:accepted',
        targetUserId: USER_ID,
      }],
      resetRequired: false,
    })

    await harness.pollPolicyEvents()

    expect(api.loads).toHaveLength(0)
    expect(first.requestToken).not.toHaveBeenCalled()
    expect(second.requestToken).not.toHaveBeenCalled()

    harness.hocuspocus.documents.delete(ROOM)
    await sidecar.stop()
  })

  it('keeps the generation check when any socket context is unclear', async () => {
    const api = new FakeCollaborationApi()
    api.loadedState = await documentState(
      DOCUMENT_ID,
      markdownDocument('Unclear policy cursor'),
    )
    const configured = settings({ policyPollMs: 60_000 })
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const harness = sidecar as unknown as GuestPolicyHarness
    await vi.waitFor(() => expect(harness.policyPollInFlight).toBeNull())
    const covered = policyConnection({
      id: USER_ID,
      kind: 'user',
      policyCursor: 12,
      socketId: 'covered-account-socket',
    })
    const unclear = policyConnection({
      id: USER_ID,
      kind: 'user',
      policyCursor: 12,
      socketId: 'unclear-account-socket',
    })
    delete (unclear.context as Partial<GuestPolicyConnection['context']>).policyCursor
    harness.hocuspocus.documents.set(ROOM, {
      getConnections: () => [covered, unclear],
      name: ROOM,
    })
    api.policyImplementation = async () => ({
      cursor: 12,
      events: [{
        id: 12,
        resourceId: DOCUMENT_ID,
        resourceType: 'editor_document',
        scope: 'share:accepted',
        targetUserId: USER_ID,
      }],
      resetRequired: false,
    })

    await harness.pollPolicyEvents()

    expect(api.loads).toHaveLength(1)
    expect(covered.requestToken).not.toHaveBeenCalled()
    expect(unclear.requestToken).not.toHaveBeenCalled()

    harness.hocuspocus.documents.delete(ROOM)
    await sidecar.stop()
  })

  it('revalidates all account sockets when the policy feed requires a reset', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const harness = sidecar as unknown as GuestPolicyHarness
    await vi.waitFor(() => expect(harness.policyPollInFlight).toBeNull())
    const covered = policyConnection({
      id: USER_ID,
      kind: 'user',
      policyCursor: 99,
      socketId: 'covered-account-socket',
    })
    harness.hocuspocus.documents.set(ROOM, {
      getConnections: () => [covered],
      name: ROOM,
    })
    api.policyImplementation = async () => ({
      cursor: 100,
      events: [],
      resetRequired: true,
    })

    await harness.pollPolicyEvents()

    expect(covered.requestToken).toHaveBeenCalledTimes(1)

    harness.hocuspocus.documents.delete(ROOM)
    await sidecar.stop()
  })

  it('revalidates only guest sockets whose lease check predates a guest policy event', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const harness = sidecar as unknown as GuestPolicyHarness
    await vi.waitFor(() => expect(harness.policyPollInFlight).toBeNull())
    const guest = policyConnection({
      id: '33333333-3333-4333-8333-333333333333',
      kind: 'guest',
      policyCursor: 0,
      socketId: 'guest-socket',
    })
    const coveredGuest = policyConnection({
      id: '44444444-4444-4444-8444-444444444444',
      kind: 'guest',
      policyCursor: 1,
      socketId: 'covered-guest-socket',
    })
    const account = policyConnection({
      id: USER_ID,
      kind: 'user',
      policyCursor: 0,
      socketId: 'account-socket',
    })
    harness.hocuspocus.documents.set(ROOM, {
      getConnections: () => [guest, coveredGuest, account],
      name: ROOM,
    })
    api.policyImplementation = async () => ({
      cursor: 1,
      events: [{
        id: 1,
        resourceId: DOCUMENT_ID,
        resourceType: 'editor_document',
        scope: 'collaboration_guest_policy',
        targetUserId: USER_ID,
      }],
      resetRequired: false,
    })

    await harness.pollPolicyEvents()

    expect(guest.requestToken).toHaveBeenCalledTimes(1)
    expect(coveredGuest.requestToken).not.toHaveBeenCalled()
    expect(account.requestToken).not.toHaveBeenCalled()

    harness.hocuspocus.documents.delete(ROOM)
    await sidecar.stop()
  })

  it('closes a connection when token revalidation rejects its current lease', async () => {
    const sidecar = new CollaborationSidecar(settings(), {
      api: new FakeCollaborationApi(),
      logger: silentLogger,
    })
    const harness = sidecar as unknown as TokenRenewalHarness
    const guest = policyConnection({
      id: '33333333-3333-4333-8333-333333333333',
      kind: 'guest',
      socketId: 'guest-socket',
    })
    harness.authenticator.renew = vi.fn().mockRejectedValue(
      new CollaborationError('access_revoked', {
        closeCode: CloseCodes.accessRevoked,
        httpStatus: 403,
      }),
    )

    await expect(
      harness.renewConnectionToken(guest, guest.context, ROOM, 'replacement-token'),
    ).rejects.toMatchObject({ reason: 'access_revoked' })

    expect(guest.close).toHaveBeenCalledWith({
      code: CloseCodes.accessRevoked,
      reason: 'access_revoked',
    })
  })

  it('schedules global maintenance without waiting for a snapshot', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({
      maintenanceIntervalMs: 10,
      policyPollMs: 60_000,
    })
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })

    await sidecar.start()
    await vi.waitFor(() => expect(api.compactions).not.toHaveLength(0))

    expect(api.snapshots).toHaveLength(0)
    expect(api.compactions[0]).toEqual({ fence: api.fence })
    await sidecar.stop()
  })

  it('closes an authenticated socket when its client lease expires without renewal', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Lease protected')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    document.destroy()
    api.lease = validLease(configured, api.loadedState.schemaHash, 0.25)
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const socket = await openSocket(sidecar, configured.secret)
    const authenticated = nextMessage(socket)
    const closed = nextClose(socket)

    socket.send(authFrame(ROOM, 'lease-token'))
    await withTimeout(authenticated, 1_000, 'authentication response')

    await expect(withTimeout(closed, 1_000, 'lease close')).resolves.toMatchObject({
      code: 4401,
      reason: 'invalid_lease',
    })
    await sidecar.stop()
  })

  it('ignores a well-formed durability reconcile request without a store lookup', async () => {
    const api = new FakeCollaborationApi()
    const metrics = new SidecarMetrics()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Reconcile')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    api.lookupResults = [{ hash: 'a'.repeat(64), sequence: 1 }]
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
      metrics,
    })
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const client = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(client)

      client.socket.send(reconcileFrame(ROOM, 'a'.repeat(64)))
      client.socket.send(reconcileFrame(ROOM, 'b'.repeat(64)))
      await vi.waitFor(() => expect(metrics.render()).toContain(
        'inqtrix_collaboration_stateless_messages_ignored_total 2\n',
      ))
      await new Promise((resolve) => setTimeout(resolve, 30))
      expect(api.lookups).toEqual([])
      expect(client.stateless).toEqual([])
      expect(client.closes).toEqual([])
      expect(client.closeMessages).toEqual([])
      expect(client.socket.readyState).toBe(WebSocket.OPEN)
    } finally {
      await closeClients(clients)
      document.destroy()
      await sidecar.stop()
    }
  })

  it.each([
    ['a payload that is not JSON', 'not json'],
    ['another message type', JSON.stringify({ hashes: ['a'.repeat(64)], type: 'something_else' })],
    ['an extra field', JSON.stringify({ extra: true, hashes: ['a'.repeat(64)], type: 'durability_reconcile' })],
    ['an empty hash list', JSON.stringify({ hashes: [], type: 'durability_reconcile' })],
    ['a malformed hash', JSON.stringify({ hashes: ['not-a-hash'], type: 'durability_reconcile' })],
    ['a repeated hash', JSON.stringify({ hashes: ['a'.repeat(64), 'a'.repeat(64)], type: 'durability_reconcile' })],
  ])('closes the socket on a client stateless message with %s', async (_label, payload) => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Stateless')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const client = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(client)

      client.socket.send(protocolFrame(ROOM, 5, varString(payload)))
      await vi.waitFor(() => expect(client.closes).toEqual([
        { code: 4403, reason: 'invalid_request' },
      ]))
      expect(api.lookups).toEqual([])
    } finally {
      await closeClients(clients)
      document.destroy()
      await sidecar.stop()
    }
  })

  it('accepts one authenticated awareness state without counting a scratch client', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Awareness')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    document.destroy()
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const socket = await openSocket(sidecar, configured.secret)
    const authenticated = nextMessage(socket)
    socket.send(authFrame(ROOM, 'lease-token'))
    await withTimeout(authenticated, 1_000, 'awareness authentication')

    const clientId = 7
    socket.send(awarenessFrame(ROOM, clientId, 1, {
      user: { color: '#badbad', id: 'spoofed', name: 'Spoofed' },
    }))

    const hocuspocusDocument = (
      sidecar as unknown as {
        hocuspocus: {
          documents: Map<string, {
            awareness: { getStates(): Map<number, Record<string, unknown>> }
          }>
        }
      }
    ).hocuspocus.documents.get(ROOM)
    await vi.waitFor(() => expect(hocuspocusDocument?.awareness.getStates().get(clientId)).toEqual({
      user: { color: '#123456', id: USER_ID, name: 'Ada' },
    }))
    expect(socket.readyState).toBe(WebSocket.OPEN)
    const metrics = await fetch(`http://127.0.0.1:${sidecar.address?.port}/metrics`)
    expect(await metrics.text()).toContain(
      'inqtrix_collaboration_awareness_scratch_states_removed_total 1',
    )

    const closed = nextClose(socket)
    socket.close()
    await withTimeout(closed, 1_000, 'awareness disconnect')
    await sidecar.stop()
  })

  it('drops excess awareness without closing the durable document transport', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({
      awarenessRateLimit: 1,
      awarenessRateWindowMs: 60_000,
      policyPollMs: 60_000,
    })
    const document = markdownDocument('Awareness burst')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    document.destroy()
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const socket = await openSocket(sidecar, configured.secret)
    const authenticated = nextMessage(socket)
    socket.send(authFrame(ROOM, 'lease-token'))
    await withTimeout(authenticated, 1_000, 'awareness-burst authentication')

    const clientId = 8
    socket.send(awarenessFrame(ROOM, clientId, 1, {
      cursor: { anchor: 1, head: 1 },
      user: { color: '#badbad', id: 'spoofed', name: 'Spoofed' },
    }))
    const hocuspocusDocument = (
      sidecar as unknown as {
        hocuspocus: {
          documents: Map<string, {
            awareness: { getStates(): Map<number, Record<string, unknown>> }
          }>
        }
      }
    ).hocuspocus.documents.get(ROOM)
    await vi.waitFor(() => expect(hocuspocusDocument?.awareness.getStates().get(clientId)).toEqual({
      cursor: { anchor: 1, head: 1 },
      user: { color: '#123456', id: USER_ID, name: 'Ada' },
    }))

    socket.send(awarenessFrame(ROOM, clientId, 2, {
      cursor: { anchor: 2, head: 2 },
      user: { color: '#badbad', id: 'spoofed', name: 'Spoofed' },
    }))
    await new Promise((resolve) => setTimeout(resolve, 30))

    expect(socket.readyState).toBe(WebSocket.OPEN)
    expect(hocuspocusDocument?.awareness.getStates().get(clientId)).toEqual({
      cursor: { anchor: 1, head: 1 },
      user: { color: '#123456', id: USER_ID, name: 'Ada' },
    })
    const closed = nextClose(socket)
    socket.close()
    await withTimeout(closed, 1_000, 'awareness-burst disconnect')
    await sidecar.stop()
  })

  it('closes a stateless flood from a view connection once the per-socket budget is spent', async () => {
    const api = new FakeCollaborationApi()
    const metrics = new SidecarMetrics()
    // Room for the whole flood in the ingress, so only the stateless budget
    // can close the socket.
    const configured = settings({ maxQueuedFrames: 256, policyPollMs: 60_000 })
    const document = markdownDocument('Stateless flood')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = {
      ...validLease(configured, api.loadedState.schemaHash, 60),
      permission: 'view',
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
      metrics,
    })
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const client = await joinRoom(sidecar, configured.secret, 'view-token', document)
      clients.push(client)

      // Ten messages per socket and ten seconds; the eleventh closes.
      for (let index = 0; index < 200; index += 1) {
        client.socket.send(reconcileFrame(ROOM, index.toString(16).padStart(64, '0')))
      }
      await vi.waitFor(() => expect(client.closes).toEqual([
        { code: 4429, reason: 'rate_limited' },
      ]))
      const rendered = metrics.render()
      expect(rendered).toContain('inqtrix_collaboration_stateless_messages_ignored_total 10\n')
      expect(rendered).toContain('inqtrix_collaboration_rejections_total{reason="rate_limited"} 1\n')
      expect(api.lookups).toEqual([])
    } finally {
      await closeClients(clients)
      document.destroy()
      await sidecar.stop()
    }
  })

  it('closes a connection whose own token renewals exceed its budget before the store is asked again', async () => {
    // Room for the whole flood in the ingress, so only the renewal budget can
    // close the socket.
    const fixture = await ingressFixture({ maxQueuedFrames: 256 })
    const clients: RoomClient[] = []
    try {
      const viewer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'view-v', fixture.document)
      clients.push(viewer)
      const introspect = fixture.api.introspectLease.bind(fixture.api)
      let introspections = 0
      fixture.api.introspectLease = async (input) => {
        introspections += 1
        return introspect(input)
      }

      // Every renewal introspects the lease through the store. Ten renewals
      // per connection and ten seconds; the eleventh closes the socket and
      // the renewals queued behind it reach nothing.
      for (let index = 0; index < 40; index += 1) {
        viewer.socket.send(authFrame(ROOM, 'view-v'))
      }
      await vi.waitFor(() => expect(viewer.closes).toEqual([
        { code: 4429, reason: 'rate_limited' },
      ]))
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(introspections).toBe(10)
      expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_rejections_total{reason="rate_limited"} 1\n',
      )
    } finally {
      await fixture.close(clients)
    }
  })

  it('renews a token the service asked for even after the connection spent its own renewal budget', async () => {
    const fixture = await ingressFixture({ maxQueuedFrames: 256 })
    const clients: RoomClient[] = []
    try {
      const viewer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'view-v', fixture.document)
      clients.push(viewer)
      const introspect = fixture.api.introspectLease.bind(fixture.api)
      let introspections = 0
      fixture.api.introspectLease = async (input) => {
        introspections += 1
        return introspect(input)
      }
      for (let index = 0; index < 10; index += 1) {
        viewer.socket.send(authFrame(ROOM, 'view-v'))
      }
      await vi.waitFor(() => expect(introspections).toBe(10))

      // A policy change makes the service ask for the token; the answer is
      // renewed although the connection's own budget is spent.
      const connection = fixture.harness.hocuspocus.documents.get(ROOM)?.getConnections()[0]
      if (!connection) throw new Error('ingress fixture has no viewer connection')
      fixture.harness.requestPolicyRevalidation(connection, ROOM)
      viewer.socket.send(authFrame(ROOM, 'view-v'))
      await vi.waitFor(() => expect(introspections).toBe(11))
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(viewer.closes).toEqual([])
      expect(viewer.closeMessages).toEqual([])

      // The request was answered once; a further renewal of its own is not.
      viewer.socket.send(authFrame(ROOM, 'view-v'))
      await vi.waitFor(() => expect(viewer.closes).toEqual([
        { code: 4429, reason: 'rate_limited' },
      ]))
      expect(introspections).toBe(11)
    } finally {
      await fixture.close(clients)
    }
  })

  it.each([
    ['a message type no client sends', () => protocolFrame(ROOM, 99, new Uint8Array([0]))],
    ['a sync status', () => protocolFrame(ROOM, 8, varUint(1))],
    ['a sync reply', () => protocolFrame(ROOM, 4, concat(varUint(0), varBytes(new Uint8Array([0]))))],
    ['an authentication message that carries no token', () => protocolFrame(ROOM, 2, concat(varUint(1), varString('denied')))],
  ] as const)('closes an open connection that sends %s, before anything is logged for it', async (_label, frame) => {
    const fixture = await ingressFixture({ maxQueuedFrames: 256 })
    const clients: RoomClient[] = []
    const errors = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    try {
      const viewer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'view-v', fixture.document)
      clients.push(viewer)

      // Hocuspocus has no handler for these frames and would write one error
      // line per frame to the service's log, without end.
      for (let index = 0; index < 40; index += 1) viewer.socket.send(frame())
      await vi.waitFor(() => expect(viewer.closes).toEqual([
        { code: 4403, reason: 'invalid_request' },
      ]))
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(errors).not.toHaveBeenCalled()
      expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_rejections_total{reason="invalid_request"} 1\n',
      )
    } finally {
      errors.mockRestore()
      await fixture.close(clients)
    }
  })

  it('keeps serving the other messages a browser sends: a presence query and the close of its own document', async () => {
    const fixture = await ingressFixture({})
    const clients: RoomClient[] = []
    try {
      const viewer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'view-v', fixture.document)
      clients.push(viewer)
      viewer.socket.send(awarenessFrame(ROOM, 7, 1, { cursor: null }))
      await vi.waitFor(() => expect(viewer.awareness).toHaveLength(1))

      // Frames are handled in order: the close answers only after the query
      // was handled without a refusal.
      viewer.socket.send(protocolFrame(ROOM, 3, new Uint8Array()))
      viewer.socket.send(protocolFrame(ROOM, 7, new Uint8Array()))
      await vi.waitFor(() => expect(viewer.closeMessages).toEqual(['provider_initiated']))
      expect(viewer.closes).toEqual([])
      expect(fixture.metrics.render()).not.toContain('reason="invalid_request"')
    } finally {
      await fixture.close(clients)
    }
  })

  it('closes a suggest socket before persisting transient Yjs history', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('First\n\nSecond')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = {
      ...validLease(configured, api.loadedState.schemaHash, 60),
      permission: 'suggest',
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const socket = await openSocket(sidecar, configured.secret)
    const authenticated = nextMessage(socket)
    socket.send(authFrame(ROOM, 'lease-token'))
    await withTimeout(authenticated, 1_000, 'transient-history authentication')
    const synchronized = nextMessage(socket)
    socket.send(syncFrame(ROOM, 0, Y.encodeStateVector(document)))
    await withTimeout(synchronized, 1_000, 'transient-history synchronization')

    const closed = nextClose(socket)
    socket.send(syncFrame(ROOM, 2, suggestionWithTransientTextUpdate(document)))

    await expect(withTimeout(closed, 1_000, 'transient-history rejection'))
      .resolves.toEqual({ code: 4403, reason: 'suggestion_policy_violation' })
    expect(api.persisted).toHaveLength(0)

    document.destroy()
    await sidecar.stop()
  })

  it('rejects a burst while authentication is slow before Hocuspocus can queue it', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({
      maxQueuedFrames: 2,
      policyPollMs: 60_000,
    })
    const document = markdownDocument('Queued')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    document.destroy()
    const introspection = deferred<IntrospectedLease>()
    let introspections = 0
    api.introspectLease = async () => {
      introspections += 1
      return introspection.promise
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const socket = await openSocket(sidecar, configured.secret)
    const closed = nextClose(socket)

    socket.send(authFrame(ROOM, 'slow-token'))
    await vi.waitFor(() => expect(introspections).toBe(1))
    socket.send(protocolFrame(ROOM, 0, new Uint8Array([0])))
    socket.send(protocolFrame(ROOM, 0, new Uint8Array([0])))

    await expect(closed).resolves.toMatchObject({
      code: 4429,
      reason: 'rate_limited',
    })
    introspection.resolve(validLease(configured, api.loadedState.schemaHash, 60))
    await sidecar.stop()
  })

  it('rejects queued bytes before copying a slow-authentication frame', async () => {
    const api = new FakeCollaborationApi()
    const auth = authFrame(ROOM, 'slow-token')
    const queued = protocolFrame(ROOM, 0, new Uint8Array(64))
    const configured = settings({
      maxQueuedBytes: auth.byteLength + queued.byteLength - 1,
      maxQueuedFrames: 8,
      policyPollMs: 60_000,
    })
    const document = markdownDocument('Queued bytes')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    document.destroy()
    const introspection = deferred<IntrospectedLease>()
    let introspections = 0
    api.introspectLease = async () => {
      introspections += 1
      return introspection.promise
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const socket = await openSocket(sidecar, configured.secret)
    const closed = nextClose(socket)

    socket.send(auth)
    await vi.waitFor(() => expect(introspections).toBe(1))
    socket.send(queued)

    await expect(closed).resolves.toMatchObject({
      code: 4429,
      reason: 'rate_limited',
    })
    introspection.resolve(validLease(configured, api.loadedState.schemaHash, 60))
    await sidecar.stop()
  })

  it('holds 64 frames for a document that is still loading and resets the socket at one more', async () => {
    // The pre-open frame count is published so that clients can bound what
    // they send before a document has loaded; the service must reset exactly
    // one frame later. The ingress frame limit is set above it so that only
    // the pre-open bound can close the socket.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    try {
      for (const [frames, reset] of [[64, false], [65, true]] as const) {
        const api = new FakeCollaborationApi()
        const metrics = new SidecarMetrics()
        const configured = settings({ maxQueuedFrames: 128, policyPollMs: 60_000 })
        const document = markdownDocument('Loading')
        api.loadedState = await documentState(DOCUMENT_ID, document)
        document.destroy()
        api.lease = validLease(configured, api.loadedState.schemaHash, 60)
        const load = deferred<void>()
        const loadDocumentState = api.loadDocumentState.bind(api)
        api.loadDocumentState = async (input) => {
          await load.promise
          return loadDocumentState(input)
        }
        const sidecar = new CollaborationSidecar(configured, {
          api,
          logger: silentLogger,
          metrics,
        })
        await sidecar.start()
        try {
          const socket = await openSocket(sidecar, configured.secret)
          const closes: Array<{ code: number; reason: string }> = []
          void nextClose(socket).then((close) => closes.push(close))
          const authenticated = nextMessage(socket)
          socket.send(authFrame(ROOM, 'lease-token'))
          await withTimeout(authenticated, 1_000, 'authentication')
          // Presence frames: after the load the connection processes every
          // one of them without refusing any.
          for (let index = 0; index < frames; index += 1) {
            socket.send(awarenessFrame(ROOM, 75, index + 1, { cursor: null }))
          }

          if (reset) {
            await vi.waitFor(() => expect(closes).toEqual([
              { code: 4205, reason: 'Reset Connection' },
            ]), { timeout: 2_000 })
            continue
          }
          // Every frame reached the service: each holds an ingress
          // reservation, and the authentication frame keeps its own until
          // the document has loaded.
          await vi.waitFor(() => {
            expect(closes).toEqual([])
            expect(metrics.render()).toContain(
              `inqtrix_collaboration_ingress_queued_frames ${frames + 1}\n`,
            )
          }, { timeout: 2_000 })
          load.resolve()
          await vi.waitFor(() => expect(metrics.render()).toContain(
            'inqtrix_collaboration_ingress_queued_frames 0\n',
          ), { timeout: 2_000 })
          await new Promise((resolve) => setTimeout(resolve, 100))
          expect(closes).toEqual([])
          expect(socket.readyState).toBe(WebSocket.OPEN)
          socket.close()
        } finally {
          load.resolve()
          await sidecar.stop()
        }
      }
    } finally {
      warn.mockRestore()
    }
  }, 20_000)

  it('keeps other writers connected and applies a committed update whose transport closed during persistence', async () => {
    const api = new FakeCollaborationApi()
    const metrics = new SidecarMetrics()
    const configured = settings({ policyPollMs: 60_000, snapshotIdleMs: 50 })
    const document = markdownDocument('Hello\n\nWorld')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const firstPersistence = deferred<PersistResult>()
    api.persistImplementation = async () => {
      const sequence = api.persisted.length
      if (sequence === 1) return firstPersistence.promise
      return { duplicate: false, persistedSequence: sequence, sequence }
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
      metrics,
    })
    const harness = sidecar as unknown as RoomHarness
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const closing = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      const waiting = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      const observer = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(closing, waiting, observer)

      closing.socket.send(syncFrame(ROOM, 2, editorUpdate(document, 'Hello!\n\nWorld')))
      await vi.waitFor(() => expect(api.persisted).toHaveLength(1))
      // Queued behind the persisting commit on the room gate.
      const waitingUpdate = editorUpdate(document, 'Hello\n\nWorld!')
      waiting.socket.send(syncFrame(ROOM, 2, waitingUpdate))
      closing.socket.close()
      await vi.waitFor(() => expect(harness.sockets.size).toBe(2))

      firstPersistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
      await harness.coordinator.awaitRoom(ROOM)
      await new Promise((resolve) => setTimeout(resolve, 100))

      expect(waiting.closes).toEqual([])
      expect(observer.closes).toEqual([])
      expect(waiting.closeMessages).toEqual([])
      expect(observer.closeMessages).toEqual([])
      expect(waiting.socket.readyState).toBe(WebSocket.OPEN)
      expect(observer.socket.readyState).toBe(WebSocket.OPEN)
      await vi.waitFor(() => expect(waiting.stateless).toContainEqual({
        hash: hashBytes(waitingUpdate),
        sequence: 2,
        type: 'durable_ack',
      }))
      await vi.waitFor(() => expect(
        markdownAfterUpdates(document, observer.syncPayloads),
      ).toBe('Hello!\n\nWorld!'))
      expect(harness.coordinator.requiresReconstruction(ROOM)).toBe(false)
      expect(harness.coordinator.getPersistedSequence(ROOM)).toBe(2)
      expect(api.persisted).toHaveLength(2)
      expect(api.loads).toHaveLength(1)
      const rendered = metrics.render()
      expect(rendered).not.toContain('inqtrix_collaboration_rejections_total{reason="restarting"}')
      expect(rendered).not.toContain('inqtrix_collaboration_rejections_total{reason="invalid_lease"}')
      expect(rendered).toContain('inqtrix_collaboration_acknowledgements_undeliverable_total 1')
      const processingFailures = consoleError.mock.calls.filter(([message]) => (
        typeof message === 'string' && message.startsWith('closing connection ')
      ))
      expect(processingFailures).toEqual([])
    } finally {
      firstPersistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
      await closeClients(clients)
      consoleError.mockRestore()
      document.destroy()
      await sidecar.stop()
    }
  })

  it.each([
    ['while another writer keeps the room loaded', 'persisting', true],
    ['while the room drains without another writer', 'persisting', false],
    ['after the room unloaded', 'unloaded', false],
  ] as const)(
    "accepts a writer's reconnect right after its transport closed during persistence and acknowledges its replay: %s",
    async (_label, reconnectWhen, withOtherWriter) => {
      const api = new FakeCollaborationApi()
      const configured = settings({ policyPollMs: 60_000, snapshotIdleMs: 50 })
      const document = markdownDocument('Hello')
      api.loadedState = await documentState(DOCUMENT_ID, document)
      api.lease = validLease(configured, api.loadedState.schemaHash, 60)
      const update = editorUpdate(document, 'Hello!')
      const persistence = deferred<PersistResult>()
      api.persistImplementation = () => persistence.promise
      api.lookupResults = [{ hash: hashBytes(update), sequence: 1 }]
      const sidecar = new CollaborationSidecar(configured, {
        api,
        logger: silentLogger,
      })
      const harness = sidecar as unknown as RoomHarness
      await sidecar.start()
      const clients: RoomClient[] = []
      try {
        const writer = await joinRoom(sidecar, configured.secret, 'lease-token', document)
        clients.push(writer)
        const other = withOtherWriter
          ? await joinRoom(sidecar, configured.secret, 'lease-token', document)
          : null
        if (other) clients.push(other)

        writer.socket.send(syncFrame(ROOM, 2, update))
        await vi.waitFor(() => expect(api.persisted).toHaveLength(1))
        writer.socket.close()
        await vi.waitFor(() => expect(harness.sockets.size).toBe(other ? 1 : 0))
        if (reconnectWhen === 'unloaded') {
          persistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
          await harness.coordinator.awaitRoom(ROOM)
          await vi.waitFor(() => expect(harness.hocuspocus.documents.has(ROOM)).toBe(false))
        }

        const reconnected = await openRoomClient(sidecar, configured.secret)
        clients.push(reconnected)
        reconnected.socket.send(authFrame(ROOM, 'lease-token'))
        await vi.waitFor(() => expect(
          reconnected.auth.length + reconnected.closes.length,
        ).toBeGreaterThan(0))
        expect(reconnected.closes).toEqual([])
        expect(reconnected.auth).toEqual([{ kind: 'authenticated' }])
        reconnected.socket.send(syncFrame(ROOM, 0, Y.encodeStateVector(document)))
        reconnected.socket.send(syncFrame(ROOM, 2, update))
        persistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })

        await vi.waitFor(() => expect(reconnected.stateless).toContainEqual({
          hash: hashBytes(update),
          sequence: 1,
          type: 'durable_ack',
        }))
        expect(reconnected.closes).toEqual([])
        expect(reconnected.closeMessages).toEqual([])
        expect(other?.closes ?? []).toEqual([])
        expect(other?.closeMessages ?? []).toEqual([])
        expect(api.persisted).toHaveLength(1)
        expect(harness.coordinator.requiresReconstruction(ROOM)).toBe(false)
      } finally {
        persistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
        await closeClients(clients)
        document.destroy()
        await sidecar.stop()
      }
    },
  )

  it('keeps the room open when the store rejects a revoked writer during persistence', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const persistence = deferred<PersistResult>()
    api.persistImplementation = () => persistence.promise
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    const harness = sidecar as unknown as RoomHarness
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const revoked = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      const other = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(revoked, other)

      revoked.socket.send(syncFrame(ROOM, 2, editorUpdate(document, 'Hello!')))
      await vi.waitFor(() => expect(api.persisted).toHaveLength(1))
      persistence.reject(new ApiRequestError(403, 'access_revoked'))

      await vi.waitFor(() => expect(revoked.closes).toEqual([
        { code: 4403, reason: 'access_revoked' },
      ]))
      await new Promise((resolve) => setTimeout(resolve, 100))
      expect(other.closes).toEqual([])
      expect(other.closeMessages).toEqual([])
      expect(other.socket.readyState).toBe(WebSocket.OPEN)
      expect(harness.coordinator.requiresReconstruction(ROOM)).toBe(false)
      expect(harness.coordinator.getPersistedSequence(ROOM)).toBe(0)
      expect(api.lookups).toHaveLength(0)
    } finally {
      await closeClients(clients)
      consoleError.mockRestore()
      document.destroy()
      await sidecar.stop()
    }
  })

  // The store refuses a lease or access for several causes that share one
  // status. The close reason and the log line name the store's own reason
  // next to the sidecar's, so a signed-out writer, a lowered permission and
  // a rotated lease can be told apart from the logs and from the browser.
  it.each([
    [401, 'lease_invalid', { code: 4401, reason: 'invalid_lease:lease_invalid' }],
    [401, 'session_invalid', { code: 4401, reason: 'invalid_lease:session_invalid' }],
    [401, 'permission_denied', { code: 4401, reason: 'invalid_lease:permission_denied' }],
    [404, 'not_found', { code: 4403, reason: 'access_revoked:not_found' }],
  ] as const)('closes a writer the store refuses with %i %s under the store reason', async (
    status,
    upstream,
    close,
  ) => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const persistence = deferred<PersistResult>()
    api.persistImplementation = () => persistence.promise
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const refused = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      const other = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(refused, other)

      refused.socket.send(syncFrame(ROOM, 2, editorUpdate(document, 'Hello!')))
      await vi.waitFor(() => expect(api.persisted).toHaveLength(1))
      persistence.reject(new ApiRequestError(status, upstream))

      await vi.waitFor(() => expect(refused.closes).toEqual([close]))
      // The refusal is logged under the same text the socket closed with.
      await vi.waitFor(() => expect(consoleError.mock.calls.some((call) => (
        call.some((argument) => String(argument).includes(close.reason))
      ))).toBe(true))
      await new Promise((resolve) => setTimeout(resolve, 100))
      expect(other.closes).toEqual([])
      expect(other.socket.readyState).toBe(WebSocket.OPEN)
    } finally {
      await closeClients(clients)
      consoleError.mockRestore()
      document.destroy()
      await sidecar.stop()
    }
  })

  it('applies a committed update when its connection turns read-only while it persists', async () => {
    const api = new FakeCollaborationApi()
    const metrics = new SidecarMetrics()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    const schemaHash = api.loadedState.schemaHash
    const renewal = deferred<IntrospectedLease>()
    let renewalRequested = false
    api.introspectLease = async (input?: { token: string }) => {
      const token = input?.token ?? ''
      if (token === 'downgraded-token') {
        renewalRequested = true
        return renewal.promise
      }
      return { ...validLease(configured, schemaHash, 60), leaseId: token }
    }
    const persistence = deferred<PersistResult>()
    api.persistImplementation = () => persistence.promise
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
      metrics,
    })
    const harness = sidecar as unknown as RoomHarness
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const downgraded = await joinRoom(sidecar, configured.secret, 'downgraded-writer', document)
      const other = await joinRoom(sidecar, configured.secret, 'other-writer', document)
      clients.push(downgraded, other)
      const downgradedConnection = harness.hocuspocus.documents.get(ROOM)
        ?.getConnections()
        .find((connection) => connection.context.leaseId === 'downgraded-writer')
      if (!downgradedConnection) throw new Error('downgrade fixture has no connection')

      // Token sync is started without being awaited, so the next frame of
      // the same connection persists while the renewal is still running.
      downgraded.socket.send(authFrame(ROOM, 'downgraded-token'))
      await vi.waitFor(() => expect(renewalRequested).toBe(true))
      downgraded.socket.send(syncFrame(ROOM, 2, editorUpdate(document, 'Hello!')))
      await vi.waitFor(() => expect(api.persisted).toHaveLength(1))
      renewal.resolve({
        ...validLease(configured, schemaHash, 60),
        leaseId: 'downgraded-renewed',
        permission: 'view',
      })
      await vi.waitFor(() => expect(downgradedConnection.readOnly).toBe(true))
      persistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
      await harness.coordinator.awaitRoom(ROOM)
      await new Promise((resolve) => setTimeout(resolve, 100))

      expect(other.closes).toEqual([])
      expect(other.closeMessages).toEqual([])
      expect(other.socket.readyState).toBe(WebSocket.OPEN)
      await vi.waitFor(() => expect(
        markdownAfterUpdates(document, other.syncPayloads),
      ).toBe('Hello!'))
      expect(harness.coordinator.requiresReconstruction(ROOM)).toBe(false)
      expect(harness.coordinator.getPersistedSequence(ROOM)).toBe(1)
      expect(metrics.render()).toContain(
        'inqtrix_collaboration_committed_updates_applied_by_coordinator_total 1',
      )
    } finally {
      renewal.resolve({
        ...validLease(configured, schemaHash, 60),
        leaseId: 'downgraded-renewed',
        permission: 'view',
      })
      persistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
      await closeClients(clients)
      consoleError.mockRestore()
      document.destroy()
      await sidecar.stop()
    }
  })

  it('does not re-register a closed transport while its queued frames drain', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello\n\nWorld')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const persistences = [deferred<PersistResult>(), deferred<PersistResult>()]
    api.persistImplementation = () => {
      const pending = persistences[api.persisted.length - 1]
      if (!pending) throw new Error('drain fixture persisted an unexpected update')
      return pending.promise
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    const harness = sidecar as unknown as RoomHarness
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const observer = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(observer)
      const knownTransports = new Set(harness.sockets.keys())
      const closing = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(closing)
      const closingTransport = [...harness.sockets.keys()]
        .find((transportId) => !knownTransports.has(transportId))
      if (!closingTransport) throw new Error('drain fixture has no transport')

      closing.socket.send(syncFrame(ROOM, 2, editorUpdate(document, 'Hello!\n\nWorld')))
      await vi.waitFor(() => expect(api.persisted).toHaveLength(1))
      closing.socket.send(syncFrame(ROOM, 2, editorUpdate(document, 'Hello\n\nWorld!')))
      closing.socket.close()
      await vi.waitFor(() => expect(harness.sockets.has(closingTransport)).toBe(false))
      persistences[0]!.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
      await vi.waitFor(() => expect(api.persisted).toHaveLength(2))

      expect([...harness.connections.values()].map((registration) => (
        registration.transportId
      ))).not.toContain(closingTransport)
      expect(harness.transportConnections.has(closingTransport)).toBe(false)
      expect([...harness.authenticatedAddresses].some((key) => (
        key.startsWith(`${closingTransport}\0`)
      ))).toBe(false)

      persistences[1]!.resolve({ duplicate: false, persistedSequence: 2, sequence: 2 })
      await harness.coordinator.awaitRoom(ROOM)
      await vi.waitFor(() => expect(
        markdownAfterUpdates(document, observer.syncPayloads),
      ).toBe('Hello!\n\nWorld!'))
    } finally {
      for (const persistence of persistences) {
        persistence.resolve({ duplicate: false, persistedSequence: 2, sequence: 2 })
      }
      await closeClients(clients)
      document.destroy()
      await sidecar.stop()
    }
  })

  it('closes only the connection whose update has a causal gap, with the retryable reason', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const [predecessor, successor] = sequentialEditorUpdates(document, ['Hello!', 'Hello!!'])
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    const harness = sidecar as unknown as RoomHarness
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const gapped = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      const other = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(gapped, other)

      gapped.socket.send(syncFrame(ROOM, 2, successor!))
      await vi.waitFor(() => expect(gapped.closes).toHaveLength(1))
      expect(gapped.closes).toEqual([{ code: 4412, reason: 'causal_gap' }])
      await new Promise((resolve) => setTimeout(resolve, 100))
      expect(other.closes).toEqual([])
      expect(other.closeMessages).toEqual([])
      expect(other.socket.readyState).toBe(WebSocket.OPEN)
      expect(harness.coordinator.requiresReconstruction(ROOM)).toBe(false)
      expect(api.persisted).toHaveLength(0)

      const resent = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(resent)
      resent.socket.send(syncFrame(ROOM, 2, predecessor!))
      resent.socket.send(syncFrame(ROOM, 2, successor!))
      await vi.waitFor(() => expect(resent.stateless).toEqual([
        { hash: hashBytes(predecessor!), sequence: 1, type: 'durable_ack' },
        { hash: hashBytes(successor!), sequence: 2, type: 'durable_ack' },
      ]))
      expect(resent.closes).toEqual([])
      await vi.waitFor(() => expect(
        markdownAfterUpdates(document, other.syncPayloads),
      ).toBe('Hello!!'))
    } finally {
      await closeClients(clients)
      consoleError.mockRestore()
      document.destroy()
      await sidecar.stop()
    }
  })

  it('does not process frames that arrive after the server began closing their transport', async () => {
    const api = new FakeCollaborationApi()
    const metrics = new SidecarMetrics()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    // One Yjs client typing: the second update continues the first.
    const [, successor] = sequentialEditorUpdates(document, ['Hello!', 'Hello!!'])
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
      metrics,
    })
    const harness = sidecar as unknown as RoomHarness
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const other = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(other)
      const knownTransports = new Set(harness.sockets.keys())
      const writer = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(writer)
      const writerTransport = [...harness.sockets.keys()]
        .find((transportId) => !knownTransports.has(transportId))
      if (!writerTransport) throw new Error('closing fixture has no transport')
      // The writer does not read the server's close, so the server's side
      // stays in the closing handshake while the writer keeps sending, as a
      // browser does until the close frame reaches it.
      writer.socket.pause()

      // Any close the server starts; here for a text frame.
      writer.socket.send('not a binary frame')
      await vi.waitFor(() => expect(
        harness.sockets.get(writerTransport)?.readyState,
      ).toBe(WebSocket.CLOSING))
      // Sent before the writer saw the close; its predecessor never reached
      // the room.
      writer.socket.send(syncFrame(ROOM, 2, successor!))
      await new Promise((resolve) => setTimeout(resolve, 100))
      await harness.coordinator.awaitRoom(ROOM)

      expect(metrics.render()).not.toContain('inqtrix_collaboration_rejections_total{reason="causal_gap"}')
      expect(api.persisted).toHaveLength(0)
      expect(consoleError.mock.calls.filter(([message]) => (
        typeof message === 'string' && message.startsWith('closing connection ')
      ))).toEqual([])
      writer.socket.resume()
      await vi.waitFor(() => expect(writer.closes).toEqual([
        { code: 4409, reason: 'binary_protocol_required' },
      ]))
      expect(other.closes).toEqual([])
    } finally {
      for (const client of clients) client.socket.resume()
      await closeClients(clients)
      consoleError.mockRestore()
      document.destroy()
      await sidecar.stop()
    }
  })

  it('counts the acknowledgement of an update that persisted after the server began closing its transport as undeliverable', async () => {
    const api = new FakeCollaborationApi()
    const metrics = new SidecarMetrics()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const persistence = deferred<PersistResult>()
    api.persistImplementation = () => persistence.promise
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
      metrics,
    })
    const harness = sidecar as unknown as RoomHarness
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const other = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(other)
      const knownTransports = new Set(harness.sockets.keys())
      const writer = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(writer)
      const writerTransport = [...harness.sockets.keys()]
        .find((transportId) => !knownTransports.has(transportId))
      if (!writerTransport) throw new Error('closing fixture has no transport')
      writer.socket.pause()

      writer.socket.send(syncFrame(ROOM, 2, editorUpdate(document, 'Hello!')))
      await vi.waitFor(() => expect(api.persisted).toHaveLength(1))
      // The server closes the writer itself while the update persists; the
      // socket stays registered until the writer answers the close.
      writer.socket.send('not a binary frame')
      await vi.waitFor(() => expect(
        harness.sockets.get(writerTransport)?.readyState,
      ).toBe(WebSocket.CLOSING))
      persistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
      await harness.coordinator.awaitRoom(ROOM)
      await vi.waitFor(() => expect(
        markdownAfterUpdates(document, other.syncPayloads),
      ).toBe('Hello!'))

      expect(metrics.render()).toContain(
        'inqtrix_collaboration_acknowledgements_undeliverable_total 1',
      )
      expect(harness.coordinator.getPersistedSequence(ROOM)).toBe(1)
      expect(other.closes).toEqual([])
    } finally {
      persistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
      for (const client of clients) client.socket.resume()
      await closeClients(clients)
      consoleError.mockRestore()
      document.destroy()
      await sidecar.stop()
    }
  })

  it('does not process the queued frames of a connection after refusing one of its updates', async () => {
    const api = new FakeCollaborationApi()
    const metrics = new SidecarMetrics()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const [predecessor, successor] = sequentialEditorUpdates(document, ['Hello!', 'Hello!!'])
    const blocking = deferred<PersistResult>()
    api.persistImplementation = async () => {
      const sequence = api.persisted.length
      if (sequence === 1) return blocking.promise
      return { duplicate: false, persistedSequence: sequence, sequence }
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
      metrics,
    })
    const harness = sidecar as unknown as RoomHarness
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const gapped = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      const other = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(gapped, other)
      // Another writer's persisting update holds the room gate, so both of
      // the gapped writer's frames are queued before the first is refused.
      other.socket.send(syncFrame(ROOM, 2, editorUpdate(document, 'Hello\n\nWorld')))
      await vi.waitFor(() => expect(api.persisted).toHaveLength(1))
      gapped.socket.send(syncFrame(ROOM, 2, successor!))
      // The client's own sync answer: everything the room lacks, gap-free.
      gapped.socket.send(syncFrame(ROOM, 1, Y.mergeUpdates([predecessor!, successor!])))
      await vi.waitFor(() => expect(metrics.render()).toContain(
        'inqtrix_collaboration_ingress_queued_frames 1',
      ))

      blocking.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
      await vi.waitFor(() => expect(gapped.closes).toEqual([
        { code: 4412, reason: 'causal_gap' },
      ]))
      await new Promise((resolve) => setTimeout(resolve, 100))
      await harness.coordinator.awaitRoom(ROOM)
      expect(api.persisted).toHaveLength(1)
      expect(metrics.render()).toContain(
        'inqtrix_collaboration_rejections_total{reason="causal_gap"} 1',
      )

      // The resent updates are new to the room and each is acknowledged.
      const resent = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(resent)
      resent.socket.send(syncFrame(ROOM, 2, predecessor!))
      resent.socket.send(syncFrame(ROOM, 2, successor!))
      await vi.waitFor(() => expect(resent.stateless).toEqual([
        { hash: hashBytes(predecessor!), sequence: 2, type: 'durable_ack' },
        { hash: hashBytes(successor!), sequence: 3, type: 'durable_ack' },
      ]))
      expect(other.closes).toEqual([])
    } finally {
      blocking.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
      await closeClients(clients)
      consoleError.mockRestore()
      document.destroy()
      await sidecar.stop()
    }
  })

  it('acknowledges a writer whose deletion another writer made first', async () => {
    const api = new FakeCollaborationApi()
    const metrics = new SidecarMetrics()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const firstPersistence = deferred<PersistResult>()
    api.persistImplementation = async () => {
      const sequence = api.persisted.length
      if (sequence === 1) return firstPersistence.promise
      return { duplicate: false, persistedSequence: sequence, sequence }
    }
    const deletionB = editorUpdate(document, 'Hel')
    const deletionA = editorUpdate(document, 'Hell')
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
      metrics,
    })
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const writerA = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      const writerB = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(writerA, writerB)

      writerB.socket.send(syncFrame(ROOM, 2, deletionB))
      await vi.waitFor(() => expect(api.persisted).toHaveLength(1))
      // A's deletion waits behind B's at the room gate.
      writerA.socket.send(syncFrame(ROOM, 2, deletionA))
      await new Promise((resolve) => setTimeout(resolve, 30))
      firstPersistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })

      await vi.waitFor(() => expect(writerA.stateless).toContainEqual({
        hash: hashBytes(deletionA),
        sequence: 1,
        type: 'durable_ack',
      }), { timeout: 1_000 })
      expect(writerB.stateless).toContainEqual({
        hash: hashBytes(deletionB),
        sequence: 1,
        type: 'durable_ack',
      })
      expect(writerA.closes).toEqual([])
      expect(writerA.closeMessages).toEqual([])
      expect(writerA.socket.readyState).toBe(WebSocket.OPEN)
      expect(api.persisted).toHaveLength(1)
      expect(metrics.render()).toContain(
        'inqtrix_collaboration_redundant_updates_acknowledged_total 1\n',
      )
    } finally {
      firstPersistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
      await closeClients(clients)
      document.destroy()
      await sidecar.stop()
    }
  })

  it('records the delivery of a suppressed acknowledgement when the resent update is acknowledged on a later connection', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-sidecar-fault-'))
    const faultPath = join(directory, 'fault.json')
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const update = editorUpdate(document, 'Hello!')
    const gate = new VerificationFaultGate(faultPath)
    const faultRecord = {
      close_code: null,
      contract: 'inqtrix-collaboration-verification-fault-v1',
      document_id: DOCUMENT_ID,
      durability_reconciled: null,
      durable_sequence: null,
      kind: 'lost_ack',
      loaded: false,
      operation_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      pending_durability_count: null,
      projection_sequence: null,
      reconciliation_sequence: null,
      run_id: 'inqv-sidecar-lost-ack-0001',
      state: 'armed',
      update_hash: null,
      user_id: USER_ID,
    }
    writeFileSync(faultPath, `${JSON.stringify(faultRecord)}\n`, { mode: 0o600 })
    gate.reload()
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
      verificationFaults: gate,
    })
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const writer = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(writer)
      writer.socket.send(syncFrame(ROOM, 2, update))
      await vi.waitFor(() => expect(writer.closes).toEqual([
        { code: 1012, reason: 'restarting' },
      ]))
      expect(writer.stateless).toEqual([])
      expect(gate.current()).toMatchObject({
        durability_reconciled: false,
        state: 'triggered',
        update_hash: hashBytes(update),
      })

      // The verifier releases the actor; the client reconnects and resends.
      writeFileSync(faultPath, `${JSON.stringify({ ...gate.current(), loaded: false, state: 'ready' })}\n`, {
        mode: 0o600,
      })
      gate.reload()
      api.lookupResults = [{ hash: hashBytes(update), sequence: 1 }]
      const resent = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(resent)
      resent.socket.send(syncFrame(ROOM, 2, update))
      await vi.waitFor(() => expect(resent.stateless).toContainEqual({
        hash: hashBytes(update),
        sequence: 1,
        type: 'durable_ack',
      }))
      expect(gate.current()).toMatchObject({
        durability_reconciled: true,
        pending_durability_count: 0,
        reconciliation_sequence: 1,
        state: 'ready',
      })
      expect(api.persisted).toHaveLength(1)
    } finally {
      await closeClients(clients)
      document.destroy()
      await sidecar.stop()
      rmSync(directory, { force: true, recursive: true })
    }
  })

  it.each([
    ['update-count', { snapshotMaxUpdates: 1, snapshotTailBytes: 100 * 1024 * 1024 }],
    ['tail-bytes', { snapshotMaxUpdates: 256, snapshotTailBytes: 1 }],
  ] as const)('snapshots immediately when the %s threshold crosses after durable apply', async (
    _label,
    threshold,
  ) => {
    const api = new FakeCollaborationApi()
    const configured = settings({
      ...threshold,
      policyPollMs: 60_000,
      snapshotIdleMs: 60_000,
    })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const socket = await openSocket(sidecar, configured.secret)
    const authenticated = nextMessage(socket)
    socket.send(authFrame(ROOM, 'lease-token'))
    await withTimeout(authenticated, 1_000, `${_label} authentication`)
    const synchronized = nextMessage(socket)
    socket.send(syncFrame(ROOM, 0, Y.encodeStateVector(document)))
    await withTimeout(synchronized, 1_000, `${_label} synchronization`)

    socket.send(syncFrame(ROOM, 2, editorUpdate(document, 'Hello!')))
    await vi.waitFor(() => expect(api.snapshots).toHaveLength(1))
    expect(api.snapshots[0]?.coveredSequence).toBe(1)
    await new Promise((resolve) => setTimeout(resolve, 30))
    expect(api.snapshots).toHaveLength(1)

    const closed = nextClose(socket)
    socket.close()
    await withTimeout(closed, 1_000, `${_label} disconnect`)
    document.destroy()
    await sidecar.stop()
  })

  it('retries a failed loaded-tail snapshot autonomously while the room stays dirty', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({
      policyPollMs: 60_000,
      snapshotMaxUpdates: 1,
      snapshotRetryBaseMs: 10,
      snapshotRetryMaxMs: 20,
    })
    const document = markdownDocument('Hello')
    const tail = editorUpdate(document, 'Hello!')
    const loaded = await documentState(DOCUMENT_ID, document)
    api.loadedState = {
      ...loaded,
      persistedSequence: 1,
      updates: [{ hash: hashBytes(tail), sequence: 1, update: tail }],
    }
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    let attempts = 0
    api.snapshotImplementation = async () => {
      attempts += 1
      if (attempts === 1) throw new Error('snapshot unavailable')
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const socket = await openSocket(sidecar, configured.secret)
    const authenticated = nextMessage(socket)

    socket.send(authFrame(ROOM, 'lease-token'))
    await withTimeout(authenticated, 1_000, 'snapshot retry authentication')
    await vi.waitFor(() => expect(api.snapshots).toHaveLength(2))
    await new Promise((resolve) => setTimeout(resolve, 30))
    expect(api.snapshots).toHaveLength(2)
    expect(api.snapshots[1]).toMatchObject({
      coveredSequence: 1,
      projectionMarkdown: expect.stringContaining('Hello!'),
    })

    const closed = nextClose(socket)
    socket.close()
    await withTimeout(closed, 1_000, 'snapshot retry disconnect')
    document.destroy()
    await sidecar.stop()
  })

  it('unloads the room of a deleted document instead of retrying its snapshot', async () => {
    // The writer's lease ends with the deletion, the room is left with
    // updates no snapshot covers, and the store on the last disconnect is
    // answered "document not found". Nothing can be stored any more: the
    // room must leave memory, not retry a request that only fails again.
    const api = new FakeCollaborationApi()
    const configured = settings({
      policyPollMs: 60_000,
      snapshotIdleMs: 60_000,
      snapshotRetryBaseMs: 10,
      snapshotRetryMaxMs: 20,
    })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    const harness = sidecar as unknown as RoomHarness
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const writer = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(writer)
      await typeAcknowledged(writer, transactionUpdates(document, ['Hello!']))
      expect(api.snapshots).toHaveLength(0)
      api.snapshotImplementation = async () => {
        throw new ApiRequestError(404, 'not_found')
      }

      await closeClients([writer])

      await vi.waitFor(() => expect(harness.hocuspocus.documents.has(ROOM)).toBe(false))
      // Ten retry periods later there was still exactly the one attempt.
      await sleep(200)
      expect(api.snapshots).toHaveLength(1)
    } finally {
      await closeClients(clients)
      document.destroy()
      await sidecar.stop()
    }
  })

  it('unloads an idle room once a retried snapshot finds its document removed', async () => {
    // The last store fails during an outage, so the room stays in memory
    // with no connection and retries; meanwhile the document is deleted.
    const api = new FakeCollaborationApi()
    const configured = settings({
      policyPollMs: 60_000,
      snapshotIdleMs: 60_000,
      snapshotRetryBaseMs: 10,
      snapshotRetryMaxMs: 20,
    })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    const harness = sidecar as unknown as RoomHarness
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const writer = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(writer)
      await typeAcknowledged(writer, transactionUpdates(document, ['Hello!']))
      api.snapshotImplementation = async () => {
        throw api.snapshots.length === 1
          ? new ApiRequestError(503, 'service_unavailable')
          : new ApiRequestError(404, 'not_found')
      }

      await closeClients([writer])

      await vi.waitFor(() => expect(harness.hocuspocus.documents.has(ROOM)).toBe(false))
      await sleep(200)
      expect(api.snapshots).toHaveLength(2)
    } finally {
      await closeClients(clients)
      document.destroy()
      await sidecar.stop()
    }
  })

  it('closes and unloads the room of a document deleted while a writer is connected', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({
      policyPollMs: 60_000,
      snapshotMaxUpdates: 1,
      snapshotRetryBaseMs: 10,
      snapshotRetryMaxMs: 20,
    })
    const document = markdownDocument('Hello')
    const tail = editorUpdate(document, 'Hello!')
    const loaded = await documentState(DOCUMENT_ID, document)
    api.loadedState = {
      ...loaded,
      persistedSequence: 1,
      updates: [{ hash: hashBytes(tail), sequence: 1, update: tail }],
    }
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    api.snapshotImplementation = async () => {
      throw new ApiRequestError(404, 'not_found')
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    const harness = sidecar as unknown as RoomHarness
    await sidecar.start()
    const socket = await openSocket(sidecar, configured.secret)
    try {
      const closed = nextClose(socket)
      socket.send(authFrame(ROOM, 'lease-token'))

      // The writer is closed the way any other loss of access closes it.
      expect(await withTimeout(closed, 1_000, 'deleted document close')).toEqual({
        code: CloseCodes.accessRevoked,
        reason: 'access_revoked:not_found',
      })
      await vi.waitFor(() => expect(harness.hocuspocus.documents.has(ROOM)).toBe(false))
      await sleep(200)
      expect(api.snapshots).toHaveLength(1)
    } finally {
      if (socket.readyState === WebSocket.OPEN) socket.close()
      document.destroy()
      await sidecar.stop()
    }
  })

  it('refuses a connection that opens onto a room whose document was found removed', async () => {
    // An internal operation still holds the room when its snapshot learns
    // that the document is gone, so the room cannot unload yet. A browser
    // that joins in that window must not be served the removed document.
    const api = new FakeCollaborationApi()
    const configured = settings({
      policyPollMs: 60_000,
      snapshotMaxUpdates: 1,
      snapshotRetryBaseMs: 10,
      snapshotRetryMaxMs: 20,
    })
    const document = markdownDocument('Hello')
    const tail = editorUpdate(document, 'Hello!')
    const loaded = await documentState(DOCUMENT_ID, document)
    api.loadedState = {
      ...loaded,
      persistedSequence: 1,
      updates: [{ hash: hashBytes(tail), sequence: 1, update: tail }],
    }
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    api.snapshotImplementation = async () => {
      throw new ApiRequestError(404, 'not_found')
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    const hocuspocus = (sidecar as unknown as {
      hocuspocus: {
        documents: Map<string, unknown>
        openDirectConnection(
          room: string,
          context: Record<string, unknown>,
        ): Promise<{ disconnect(): Promise<void> }>
      }
    }).hocuspocus
    await sidecar.start()
    const operation = await hocuspocus.openDirectConnection(ROOM, {
      internalOperation: true,
    })
    const socket = await openSocket(sidecar, configured.secret)
    try {
      await vi.waitFor(() => expect(api.snapshots).toHaveLength(1))
      await sleep(50)
      expect(hocuspocus.documents.has(ROOM)).toBe(true)

      const closed = nextClose(socket)
      socket.send(authFrame(ROOM, 'lease-token'))
      expect(await withTimeout(closed, 1_000, 'late joiner close')).toEqual({
        code: CloseCodes.accessRevoked,
        reason: 'access_revoked:not_found',
      })

      await operation.disconnect()
      await vi.waitFor(() => expect(hocuspocus.documents.has(ROOM)).toBe(false))
      expect(api.snapshots).toHaveLength(1)
    } finally {
      if (socket.readyState === WebSocket.OPEN) socket.close()
      document.destroy()
      await sidecar.stop()
    }
  })

  it('stores an immediate follow-up when an update races an in-flight snapshot', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({
      policyPollMs: 60_000,
      snapshotMaxUpdates: 1,
    })
    const base = markdownDocument('Hello')
    const firstUpdate = editorUpdate(base, 'Hello!')
    const current = new Y.Doc()
    Y.applyUpdate(current, Y.encodeStateAsUpdate(base))
    Y.applyUpdate(current, firstUpdate)
    const loaded = await documentState(DOCUMENT_ID, base)
    api.loadedState = {
      ...loaded,
      persistedSequence: 1,
      updates: [{ hash: hashBytes(firstUpdate), sequence: 1, update: firstUpdate }],
    }
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    api.persistImplementation = async () => ({
      duplicate: false,
      persistedSequence: 2,
      sequence: 2,
    })
    const firstSnapshot = deferred<void>()
    let firstSnapshotPending = true
    api.snapshotImplementation = async (input) => {
      if (input.coveredSequence === 1 && firstSnapshotPending) {
        firstSnapshotPending = false
        await firstSnapshot.promise
      }
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const socket = await openSocket(sidecar, configured.secret)
    const authenticated = nextMessage(socket)
    socket.send(authFrame(ROOM, 'lease-token'))
    await withTimeout(authenticated, 1_000, 'snapshot-race authentication')
    await vi.waitFor(() => expect(api.snapshots).toHaveLength(1))

    const synchronized = nextMessage(socket)
    socket.send(syncFrame(ROOM, 0, Y.encodeStateVector(current)))
    await withTimeout(synchronized, 1_000, 'snapshot-race synchronization')
    socket.send(syncFrame(ROOM, 2, editorUpdate(current, 'Hello!!')))
    await vi.waitFor(() => expect(api.persisted).toHaveLength(1))

    firstSnapshot.resolve()
    await vi.waitFor(() => expect(api.snapshots).toHaveLength(2))
    expect(api.snapshots.map((snapshot) => snapshot.coveredSequence)).toEqual([1, 2])
    await new Promise((resolve) => setTimeout(resolve, 30))
    expect(api.snapshots).toHaveLength(2)

    const closed = nextClose(socket)
    socket.close()
    await withTimeout(closed, 1_000, 'snapshot-race disconnect')
    current.destroy()
    base.destroy()
    await sidecar.stop()
  })

  it('stores one snapshot per threshold while a writer keeps typing', async () => {
    // A snapshot captures the room at one sequence; updates acknowledged
    // while its store call runs stay in the tail. Each threshold stores one
    // snapshot instead of chasing the moving sequence with one snapshot per
    // acknowledged update.
    const api = new FakeCollaborationApi()
    const configured = settings({
      policyPollMs: 60_000,
      snapshotIdleMs: 60_000,
      snapshotMaxUpdates: 4,
      snapshotTailBytes: 100 * 1024 * 1024,
    })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    let nextPersist = deferred<void>()
    const persist = api.persistImplementation
    api.persistImplementation = async (input) => {
      const result = await persist(input)
      nextPersist.resolve()
      nextPersist = deferred<void>()
      return result
    }
    // A store call lasts until the next update is persisted and applied.
    // The bound ends a per-update snapshot loop instead of letting this test
    // hang on it.
    api.snapshotImplementation = async () => {
      await Promise.race([nextPersist.promise.then(() => sleep(20)), sleep(100)])
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const writer = await joinRoom(sidecar, configured.secret, 'lease-token', document)
    const clients = [writer]
    try {
      await typeAcknowledged(writer, transactionUpdates(
        document,
        Array.from({ length: 20 }, (_, index) => `Hello ${'!'.repeat(index + 1)}`),
      ))
      await (sidecar as unknown as RoomHarness).coordinator.awaitRoom(ROOM)
      await sleep(150)

      expect(api.snapshots.length).toBeGreaterThanOrEqual(4)
      expect(api.snapshots.length).toBeLessThanOrEqual(6)
      expect(api.compactions.filter((compaction) => compaction.documentId === DOCUMENT_ID))
        .toHaveLength(api.snapshots.length)
    } finally {
      await closeClients(clients)
      document.destroy()
      await sidecar.stop()
    }
  })

  it('stores a follow-up when the idle store fires during a threshold snapshot', async () => {
    // The idle store asks for everything typed so far. A threshold snapshot
    // that is already running captured less, so the store waits for it and
    // then stores one more snapshot that covers the tail it was asked for.
    const api = new FakeCollaborationApi()
    const configured = settings({
      policyPollMs: 60_000,
      snapshotIdleMs: 150,
      snapshotMaxUpdates: 4,
      snapshotTailBytes: 100 * 1024 * 1024,
    })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const firstSnapshot = deferred<void>()
    let firstSnapshotPending = true
    api.snapshotImplementation = async () => {
      if (!firstSnapshotPending) return
      firstSnapshotPending = false
      await firstSnapshot.promise
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const writer = await joinRoom(sidecar, configured.secret, 'lease-token', document)
    const clients = [writer]
    try {
      const updates = transactionUpdates(
        document,
        Array.from({ length: 7 }, (_, index) => `Hello ${'!'.repeat(index + 1)}`),
      )
      await typeAcknowledged(writer, updates.slice(0, 4))
      await vi.waitFor(() => expect(api.snapshots).toHaveLength(1))
      await typeAcknowledged(writer, updates.slice(4), 5)
      // The idle store fires while the threshold snapshot is still running.
      await sleep(450)
      expect(api.snapshots.map((snapshot) => snapshot.coveredSequence)).toEqual([4])

      firstSnapshot.resolve()
      await vi.waitFor(() => expect(
        api.snapshots.map((snapshot) => snapshot.coveredSequence),
      ).toEqual([4, 7]))
    } finally {
      firstSnapshot.resolve()
      await closeClients(clients)
      document.destroy()
      await sidecar.stop()
    }
  })

  it('catches the snapshot up before an idle room unloads, even when a writer rejoins meanwhile', async () => {
    // The last writer leaves while a threshold snapshot runs. The store that
    // leaving triggers covers the whole tail, also when another writer joins
    // and leaves without typing: nothing would trigger a store after that,
    // and the room unloads.
    const api = new FakeCollaborationApi()
    const configured = settings({
      policyPollMs: 60_000,
      snapshotIdleMs: 60_000,
      snapshotMaxUpdates: 4,
      snapshotTailBytes: 100 * 1024 * 1024,
    })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const firstSnapshot = deferred<void>()
    let firstSnapshotPending = true
    api.snapshotImplementation = async () => {
      if (!firstSnapshotPending) return
      firstSnapshotPending = false
      await firstSnapshot.promise
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    const harness = sidecar as unknown as RoomHarness
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const writer = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(writer)
      const updates = transactionUpdates(
        document,
        Array.from({ length: 6 }, (_, index) => `Hello ${'!'.repeat(index + 1)}`),
      )
      await typeAcknowledged(writer, updates.slice(0, 4))
      await vi.waitFor(() => expect(api.snapshots).toHaveLength(1))
      await typeAcknowledged(writer, updates.slice(4), 5)
      await closeClients([writer])

      const rejoined = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(rejoined)
      firstSnapshot.resolve()
      await vi.waitFor(() => expect(api.snapshots.at(-1)?.coveredSequence).toBe(6))
      await closeClients([rejoined])

      await vi.waitFor(() => expect(harness.hocuspocus.documents.has(ROOM)).toBe(false))
      expect(api.snapshots.map((snapshot) => snapshot.coveredSequence)).toEqual([4, 6])
    } finally {
      firstSnapshot.resolve()
      await closeClients(clients)
      document.destroy()
      await sidecar.stop()
    }
  })

  it('stops only after the snapshot that covers the durable tail is stored', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({
      policyPollMs: 60_000,
      snapshotIdleMs: 60_000,
      snapshotMaxUpdates: 4,
      snapshotTailBytes: 100 * 1024 * 1024,
    })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const firstSnapshot = deferred<void>()
    const secondSnapshot = deferred<void>()
    api.snapshotImplementation = async () => {
      await (api.snapshots.length === 1 ? firstSnapshot.promise : secondSnapshot.promise)
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const writer = await joinRoom(sidecar, configured.secret, 'lease-token', document)
    let stopped = false
    try {
      await typeAcknowledged(writer, transactionUpdates(
        document,
        Array.from({ length: 6 }, (_, index) => `Hello ${'!'.repeat(index + 1)}`),
      ))
      await vi.waitFor(() => expect(api.snapshots).toHaveLength(1))

      const stopping = sidecar.stop().then(() => {
        stopped = true
      })
      // The last connection's store and the shutdown flush both wait for the
      // running snapshot before it completes.
      await vi.waitFor(() => expect(writer.closes).toHaveLength(1))
      await sleep(100)
      expect(api.snapshots).toHaveLength(1)
      firstSnapshot.resolve()
      await vi.waitFor(() => expect(
        api.snapshots.map((snapshot) => snapshot.coveredSequence),
      ).toEqual([4, 6]))
      await sleep(100)
      expect(stopped).toBe(false)

      secondSnapshot.resolve()
      await stopping
      expect(stopped).toBe(true)
    } finally {
      firstSnapshot.resolve()
      secondSnapshot.resolve()
      document.destroy()
      await sidecar.stop()
    }
  })

  it('keeps rooms open when an instance lease renewal fails transiently', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    const harness = sidecar as unknown as LeaseHarness
    await sidecar.start()
    const writer = await joinRoom(sidecar, configured.secret, 'lease-token', document)
    try {
      api.renewError = new ApiRequestError(503, 'internal_api_timeout')
      await harness.leaseManager.renewNow()
      await sleep(100)

      expect(writer.closes).toEqual([])
      expect(harness.leaseManager.isReady()).toBe(true)
    } finally {
      api.renewError = null
      await closeClients([writer])
      document.destroy()
      await sidecar.stop()
    }
  })

  it('ends the instance lease and reconnects every room when a write finds the instance fenced', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    api.persistImplementation = async () => {
      throw new ApiRequestError(409, 'instance_fenced')
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    const harness = sidecar as unknown as LeaseHarness
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const writer = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(writer)
      const reader = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(reader)

      writer.socket.send(syncFrame(ROOM, 2, editorUpdate(document, 'Hello!')))
      await vi.waitFor(() => expect(writer.closes).toHaveLength(1))
      await vi.waitFor(() => expect(reader.closes).toHaveLength(1))

      expect(writer.closes).toEqual([{ code: 4503, reason: 'instance_lease_lost' }])
      expect(reader.closes).toEqual([{ code: 4503, reason: 'instance_lease_lost' }])
      expect(harness.leaseManager.isReady()).toBe(false)
    } finally {
      await closeClients(clients)
      document.destroy()
      await sidecar.stop()
    }
  })

  it('refuses a join with a reconnect and ends the lease when loading finds the instance fenced', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    api.lease = validLease(configured, api.loadedState.schemaHash, 60)
    api.loadDocumentState = async () => {
      throw new ApiRequestError(409, 'instance_fenced')
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    const harness = sidecar as unknown as LeaseHarness
    await sidecar.start()
    const client = await openRoomClient(sidecar, configured.secret)
    try {
      client.socket.send(authFrame(ROOM, 'lease-token'))
      await vi.waitFor(() => expect(client.closes).toHaveLength(1))

      expect(client.closes[0]?.code).toBe(4503)
      expect(harness.leaseManager.isReady()).toBe(false)
    } finally {
      await closeClients([client])
      document.destroy()
      await sidecar.stop()
    }
  })

  it('bounds shutdown with hung policy, maintenance, and socket teardown', async () => {
    const api = new FakeCollaborationApi()
    let policyCalls = 0
    api.compactImplementation = async () => await new Promise<never>(() => undefined)
    api.policyImplementation = async () => {
      policyCalls += 1
      return await new Promise<never>(() => undefined)
    }
    const configured = settings({
      maintenanceIntervalMs: 60_000,
      policyPollMs: 60_000,
    })
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
      shutdownTimeoutMs: 120,
      socketCloseGraceMs: 20,
    })
    await sidecar.start()
    const client = await openSocket(sidecar, configured.secret)
    const clientClosed = nextClose(client)
    await vi.waitFor(() => expect(api.compactions).toHaveLength(1))
    await vi.waitFor(() => expect(policyCalls).toBe(1))
    const serverSocket = [...(sidecar as unknown as ShutdownHarness).sockets.values()][0]
    if (!serverSocket) throw new Error('shutdown socket fixture is invalid')
    const close = vi.spyOn(serverSocket, 'close').mockImplementation(() => undefined)
    const terminate = vi.spyOn(serverSocket, 'terminate')

    const startedAt = performance.now()
    const stopping = sidecar.stop()
    expect(close).toHaveBeenCalledWith(1012, 'restarting')
    await withTimeout(stopping, 500, 'bounded shutdown')

    expect(terminate).toHaveBeenCalledTimes(1)
    expect(performance.now() - startedAt).toBeLessThan(500)
    await withTimeout(clientClosed, 500, 'forced client teardown')
  })

  it('aborts startup cleanly when stopped during instance lease acquisition', async () => {
    const api = new FakeCollaborationApi()
    const acquisition = deferred<typeof api.fence>()
    let acquisitions = 0
    api.acquireInstance = async () => {
      acquisitions += 1
      return acquisition.promise
    }
    const sidecar = new CollaborationSidecar(settings({ policyPollMs: 60_000 }), {
      api,
      logger: silentLogger,
      shutdownTimeoutMs: 500,
    })
    const starting = sidecar.start()
    await vi.waitFor(() => expect(acquisitions).toBe(1))

    const stopping = sidecar.stop()
    acquisition.resolve(api.fence)
    await withTimeout(Promise.all([starting, stopping]), 1_000, 'lease-startup shutdown')

    const harness = sidecar as unknown as StartupHarness
    expect(sidecar.address).toBeNull()
    expect(harness.leaseManager.isReady()).toBe(false)
    expect(harness.maintenanceTimer).toBeNull()
    expect(harness.policyTimer).toBeNull()
  })

  it('closes a delayed socket bind without ever becoming ready after stop', async () => {
    const sidecar = new CollaborationSidecar(settings({ policyPollMs: 60_000 }), {
      api: new FakeCollaborationApi(),
      logger: silentLogger,
      shutdownTimeoutMs: 500,
    })
    const harness = sidecar as unknown as StartupHarness
    const binding = deferred<void>()
    const entered = deferred<void>()
    const listen = harness.listenHttpServer.bind(sidecar)
    harness.listenHttpServer = async () => {
      entered.resolve()
      await binding.promise
      await listen()
    }
    const starting = sidecar.start()
    await entered.promise

    const stopping = sidecar.stop()
    binding.resolve()
    await withTimeout(Promise.all([starting, stopping]), 1_000, 'bind-startup shutdown')

    expect(sidecar.address).toBeNull()
    expect(harness.leaseManager.isReady()).toBe(false)
    expect(harness.maintenanceTimer).toBeNull()
    expect(harness.policyTimer).toBeNull()
  })
})

describe('acknowledgement of resent updates', () => {
  // After a reconnect the client resends every update the service has not
  // confirmed to it. The service confirms a resend whose effect is already
  // durable, answering only the connection that sent it, and it looks the
  // update up only in that connection's own document and generation, behind
  // the fence of the active instance.
  const OTHER_DOCUMENT_ID = 'ed_other'
  const OTHER_USER = { color: '#654321', id: '44444444-4444-4444-8444-444444444444', name: 'Grace' }

  /** A store lookup that answers only rows of the named document and
   * generation, and only to the active instance, as the store does. */
  function scopedLookups(api: FakeCollaborationApi): void {
    api.lookupUpdates = async (input) => {
      api.lookups.push(input)
      if (
        input.fence.epoch !== api.fence.epoch
        || input.fence.instanceId !== api.fence.instanceId
      ) {
        throw new ApiRequestError(409, 'instance_fence_stale')
      }
      return api.persisted.flatMap((row, index) => (
        row.documentId === input.documentId
        && row.generation === input.generation
        && input.hashes.includes(row.hash)
          ? [{ hash: row.hash, sequence: index + 1 }]
          : []
      ))
    }
  }

  const ackFor = (update: Uint8Array, sequence: number) => ({
    hash: hashBytes(update),
    sequence,
    type: 'durable_ack',
  })

  const acknowledged = (client: RoomClient, update: Uint8Array) => client.stateless.filter((message) => (
    typeof message === 'object'
    && message !== null
    && Reflect.get(message, 'hash') === hashBytes(update)
  ))

  it('acknowledges a resent update whose content reached the room in an earlier sync answer, on the resending connection only', async () => {
    const api = new FakeCollaborationApi()
    const metrics = new SidecarMetrics()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello world')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    const base = validLease(configured, api.loadedState.schemaHash, 60)
    api.lease = base
    api.leasesByToken.set('peer-token', {
      ...base,
      leaseId: 'lease-peer',
      sessionId: 'session-peer',
      user: OTHER_USER,
    })
    api.leasesByToken.set('view-token', { ...base, leaseId: 'lease-view', permission: 'view' })
    scopedLookups(api)
    // The writer deleted a word and then added one. Its answer to the room's
    // state request carries everything the room lacks, including the whole
    // delete set, so it is not the bytes of the later update.
    const [deletion, addition] = transactionUpdates(document, ['Hello', 'Hello!'])
    const replica = new Y.Doc()
    Y.applyUpdate(replica, Y.encodeStateAsUpdate(document))
    Y.applyUpdate(replica, deletion!)
    Y.applyUpdate(replica, addition!)
    const answer = Y.encodeStateAsUpdate(replica, Y.encodeStateVector(document))
    replica.destroy()
    expect(hashBytes(answer)).not.toBe(hashBytes(addition!))
    const sidecar = new CollaborationSidecar(configured, { api, logger: silentLogger, metrics })
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const first = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      const peer = await joinRoom(sidecar, configured.secret, 'peer-token', document)
      const viewer = await joinRoom(sidecar, configured.secret, 'view-token', document)
      clients.push(first, peer, viewer)

      first.socket.send(syncFrame(ROOM, 1, answer))
      await vi.waitFor(() => expect(first.stateless).toContainEqual(ackFor(answer, 1)))
      // The path of the first connection dies with the addition unconfirmed.
      await closeClients([first])
      const resent = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(resent)
      resent.socket.send(syncFrame(ROOM, 2, addition!))
      await vi.waitFor(() => expect(resent.stateless).toContainEqual(ackFor(addition!, 1)))

      // A view connection never reaches the store with a resend.
      const viewerStatuses = viewer.syncStatuses.length
      viewer.socket.send(syncFrame(ROOM, 2, addition!))
      await vi.waitFor(() => expect(viewer.syncStatuses.length).toBeGreaterThan(viewerStatuses))

      expect(api.persisted.map((row) => row.hash)).toEqual([hashBytes(answer)])
      expect(api.lookups).toEqual([{
        documentId: DOCUMENT_ID,
        fence: api.fence,
        generation: 1,
        hashes: [hashBytes(addition!)],
      }])
      expect(resent.stateless).toEqual([ackFor(addition!, 1)])
      expect(peer.stateless).toEqual([])
      expect(viewer.stateless).toEqual([])
      expect(acknowledged(first, addition!)).toEqual([])
      expect(resent.closes).toEqual([])
      expect(metrics.render()).toContain(
        'inqtrix_collaboration_redundant_updates_acknowledged_total 1\n',
      )
    } finally {
      await closeClients(clients)
      document.destroy()
      await sidecar.stop()
    }
  })

  it('looks a resent update up only in its own document and generation, behind the active fence', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Hello')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    const base = validLease(configured, api.loadedState.schemaHash, 60)
    api.lease = base
    const [update] = sequentialEditorUpdates(document, ['Hello!'])
    // Two further rooms already hold the same update: another document, and
    // a later generation of this one.
    const withUpdate = new Y.Doc()
    Y.applyUpdate(withUpdate, Y.encodeStateAsUpdate(document))
    Y.applyUpdate(withUpdate, update!)
    const otherRoom = editorCollaborationRoom(OTHER_DOCUMENT_ID, 1)
    const laterRoom = editorCollaborationRoom(DOCUMENT_ID, 2)
    api.loadedStatesByRoom.set(`${OTHER_DOCUMENT_ID}:1`, await documentState(OTHER_DOCUMENT_ID, withUpdate))
    api.loadedStatesByRoom.set(`${DOCUMENT_ID}:2`, await documentState(DOCUMENT_ID, withUpdate, 2))
    api.leasesByToken.set('other-token', { ...base, documentId: OTHER_DOCUMENT_ID, leaseId: 'lease-other' })
    api.leasesByToken.set('later-token', { ...base, generation: 2, leaseId: 'lease-later' })
    api.leasesByToken.set('peer-token', {
      ...base,
      leaseId: 'lease-peer',
      sessionId: 'session-peer',
      user: OTHER_USER,
    })
    scopedLookups(api)
    const sidecar = new CollaborationSidecar(configured, { api, logger: silentLogger })
    await sidecar.start()
    const clients: RoomClient[] = []
    try {
      const first = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      const peer = await joinRoom(sidecar, configured.secret, 'peer-token', document)
      clients.push(first, peer)
      first.socket.send(syncFrame(ROOM, 2, update!))
      await vi.waitFor(() => expect(first.stateless).toContainEqual(ackFor(update!, 1)))

      // The confirmation did not reach the writer; it resends on a new
      // connection and is confirmed with the stored sequence.
      const resent = await joinRoom(sidecar, configured.secret, 'lease-token', document)
      clients.push(resent)
      resent.socket.send(syncFrame(ROOM, 2, update!))
      await vi.waitFor(() => expect(resent.stateless).toContainEqual(ackFor(update!, 1)))

      // The same bytes in the other rooms name no row of theirs: nothing is
      // durable there, so nothing is confirmed and nothing is rebuilt.
      const other = await joinRoom(sidecar, configured.secret, 'other-token', withUpdate, otherRoom)
      const later = await joinRoom(sidecar, configured.secret, 'later-token', withUpdate, laterRoom)
      clients.push(other, later)
      other.socket.send(syncFrame(otherRoom, 2, update!))
      later.socket.send(syncFrame(laterRoom, 2, update!))
      await vi.waitFor(() => expect(api.lookups).toHaveLength(3))
      await new Promise((resolve) => setTimeout(resolve, 30))

      for (const client of [other, later]) {
        expect(client.stateless).toEqual([])
        expect(client.closes).toEqual([])
        expect(client.closeMessages).toEqual([])
      }
      const [ownLookup, ...laterLookups] = api.lookups
      expect(ownLookup).toEqual({
        documentId: DOCUMENT_ID,
        fence: api.fence,
        generation: 1,
        hashes: [hashBytes(update!)],
      })
      const roomOf = (lookup: { documentId: string; generation: number }) => (
        `${lookup.documentId}:${lookup.generation}`
      )
      expect(laterLookups.sort((left, right) => roomOf(left).localeCompare(roomOf(right)))).toEqual([
        { documentId: OTHER_DOCUMENT_ID, fence: api.fence, generation: 1, hashes: [hashBytes(update!)] },
        { documentId: DOCUMENT_ID, fence: api.fence, generation: 2, hashes: [hashBytes(update!)] },
      ])
      expect(resent.stateless).toEqual([ackFor(update!, 1)])
      expect(peer.stateless).toEqual([])
      expect(api.persisted).toHaveLength(1)
    } finally {
      await closeClients(clients)
      withUpdate.destroy()
      document.destroy()
      await sidecar.stop()
    }
  })
})

describe('awareness ownership', () => {
  // A presence id is chosen by the client. It belongs to the participant who
  // published it first in the room, and a reconnect of the same participant
  // takes it over; nobody else can change it, and no ownership decision
  // closes a connection that behaves.
  const OTHER_USER = { color: '#654321', id: '44444444-4444-4444-8444-444444444444', name: 'Grace' }
  const THIRD_USER = { color: '#abcdef', id: '55555555-5555-4555-8555-555555555555', name: 'Linus' }

  type PresenceRoom = {
    awareness: {
      getStates(): Map<number, Record<string, unknown>>
      meta: Map<number, { clock: number }>
    }
    getClients(connection: object): Set<number>
    getConnections(): Array<{ context: { leaseId?: string } }>
  }

  async function presenceFixture(): Promise<{
    api: FakeCollaborationApi
    close: () => Promise<void>
    document: Y.Doc
    join: (token: string) => Promise<RoomClient>
    lease: (token: string, changes?: Partial<IntrospectedLease>) => void
    leave: (client: RoomClient) => Promise<void>
    metrics: SidecarMetrics
    room: () => PresenceRoom | undefined
    sidecar: CollaborationSidecar
    warnings: Array<{ event: string; reason: unknown }>
  }> {
    const api = new FakeCollaborationApi()
    const metrics = new SidecarMetrics()
    const warnings: Array<{ event: string; reason: unknown }> = []
    const configured = settings({ policyPollMs: 60_000 })
    const document = markdownDocument('Presence')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    const base = validLease(configured, api.loadedState.schemaHash, 60)
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: {
        ...silentLogger,
        warn: (event, fields) => warnings.push({ event, reason: fields?.reason }),
      },
      metrics,
    })
    await sidecar.start()
    const clients: RoomClient[] = []
    return {
      api,
      close: async () => {
        await closeClients(clients)
        document.destroy()
        await sidecar.stop()
      },
      document,
      join: async (token) => {
        const client = await joinRoom(sidecar, configured.secret, token, document)
        clients.push(client)
        return client
      },
      lease: (token, changes = {}) => {
        api.leasesByToken.set(token, { ...base, leaseId: token, ...changes })
      },
      leave: (client) => closeClients([client]),
      metrics,
      room: () => (
        sidecar as unknown as { hocuspocus: { documents: Map<string, PresenceRoom> } }
      ).hocuspocus.documents.get(ROOM),
      sidecar,
      warnings,
    }
  }

  const presenceOf = (fixture: { room: () => PresenceRoom | undefined }, clientId: number) => (
    fixture.room()?.awareness.getStates().get(clientId)
  )

  const removalsOf = (client: RoomClient, clientId: number) => client.awareness.filter((entry) => (
    entry.clientId === clientId && entry.state === null
  ))

  it('lets a newer connection of the same participant take over its presence from a silent predecessor', async () => {
    const fixture = await presenceFixture()
    try {
      fixture.lease('lease-a1')
      fixture.lease('lease-a2')
      fixture.lease('lease-o', { sessionId: 'session-o', user: THIRD_USER })
      const predecessor = await fixture.join('lease-a1')
      const observer = await fixture.join('lease-o')
      predecessor.socket.send(awarenessFrame(ROOM, 41, 1, { cursor: { anchor: 1, head: 1 } }))
      await vi.waitFor(() => expect(presenceOf(fixture, 41)).toMatchObject({
        cursor: { anchor: 1, head: 1 },
      }))

      // The predecessor stays open and silent, as a connection whose network
      // path died looks to the service. The editor reconnects with a rotated
      // lease and sends its presence again, first with the clock it had.
      const successor = await fixture.join('lease-a2')
      successor.socket.send(awarenessFrame(ROOM, 41, 1, { cursor: { anchor: 1, head: 1 } }))
      successor.socket.send(awarenessFrame(ROOM, 41, 2, { cursor: { anchor: 3, head: 3 } }))
      await vi.waitFor(() => expect(
        predecessor.closeMessages.length + successor.closeMessages.length,
      ).toBeGreaterThan(0))

      expect(successor.closeMessages).toEqual([])
      expect(predecessor.closeMessages).toEqual(['connection_superseded'])
      await vi.waitFor(() => expect(predecessor.closes).toEqual([
        { code: 4401, reason: 'connection_superseded' },
      ]))
      await vi.waitFor(() => expect(observer.awareness).toContainEqual({
        clientId: 41,
        clock: 2,
        state: { cursor: { anchor: 3, head: 3 }, user: { color: '#123456', id: USER_ID, name: 'Ada' } },
      }))
      expect(presenceOf(fixture, 41)).toEqual({
        cursor: { anchor: 3, head: 3 },
        user: { color: '#123456', id: USER_ID, name: 'Ada' },
      })
      // Handing the id over never removed the participant's presence.
      expect(removalsOf(observer, 41)).toEqual([])
      expect(fixture.room()?.getConnections().map((connection) => connection.context.leaseId).sort())
        .toEqual(['lease-a2', 'lease-o'])
      expect(successor.socket.readyState).toBe(WebSocket.OPEN)
      expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_connections_superseded_total 1\n',
      )
    } finally {
      await fixture.close()
    }
  })

  it.each([
    ['while its previous connection still looks open', false],
    ['after its previous connection left', true],
  ] as const)('keeps the presence of a participant who signed in again %s', async (_label, leaveFirst) => {
    // Signing in again, for example in another tab, starts a new login
    // session. The editor keeps its document and with it its presence id.
    const fixture = await presenceFixture()
    try {
      fixture.lease('lease-a1')
      fixture.lease('lease-a2', { sessionId: 'session-2' })
      fixture.lease('lease-o', { sessionId: 'session-o', user: THIRD_USER })
      const observer = await fixture.join('lease-o')
      const previous = await fixture.join('lease-a1')
      previous.socket.send(awarenessFrame(ROOM, 61, 1, { cursor: { anchor: 1, head: 1 } }))
      await vi.waitFor(() => expect(presenceOf(fixture, 61)).toMatchObject({
        cursor: { anchor: 1, head: 1 },
      }))
      if (leaveFirst) await fixture.leave(previous)

      const current = await fixture.join('lease-a2')
      current.socket.send(awarenessFrame(ROOM, 61, 2, { cursor: { anchor: 2, head: 2 } }))
      await vi.waitFor(() => expect(
        current.closeMessages.length
        + ((presenceOf(fixture, 61)?.cursor as { anchor?: number } | undefined)?.anchor === 2 ? 1 : 0),
      ).toBeGreaterThan(0))

      expect(current.closeMessages).toEqual([])
      await vi.waitFor(() => expect(presenceOf(fixture, 61)).toEqual({
        cursor: { anchor: 2, head: 2 },
        user: { color: '#123456', id: USER_ID, name: 'Ada' },
      }))
      await vi.waitFor(() => expect(observer.awareness).toContainEqual({
        clientId: 61,
        clock: 2,
        state: { cursor: { anchor: 2, head: 2 }, user: { color: '#123456', id: USER_ID, name: 'Ada' } },
      }))
      const rendered = fixture.metrics.render()
      expect(rendered).not.toContain('inqtrix_collaboration_awareness_dropped_total')
      if (leaveFirst) {
        expect(rendered).not.toContain('inqtrix_collaboration_connections_superseded_total')
      } else {
        expect(previous.closeMessages).toEqual(['connection_superseded'])
        expect(rendered).toContain('inqtrix_collaboration_connections_superseded_total 1\n')
      }
    } finally {
      await fixture.close()
    }
  })

  it.each([
    ['on its first connection', 'first'],
    ['that already publishes its own presence', 'owner'],
    ['after it reconnected itself', 'reconnected'],
  ] as const)(
    'never lets another participant %s change a presence, also after its owner reconnected',
    async (_label, shape) => {
      const fixture = await presenceFixture()
      try {
        fixture.lease('lease-a1')
        fixture.lease('lease-a2')
        fixture.lease('lease-b1', { sessionId: 'session-b', user: OTHER_USER })
        fixture.lease('lease-b2', { sessionId: 'session-b', user: OTHER_USER })
        fixture.lease('lease-o', { sessionId: 'session-o', user: THIRD_USER })
        const observer = await fixture.join('lease-o')
        const firstA = await fixture.join('lease-a1')
        firstA.socket.send(awarenessFrame(ROOM, 61, 1, { cursor: { anchor: 1, head: 1 } }))
        await vi.waitFor(() => expect(presenceOf(fixture, 61)).toBeDefined())
        await fixture.leave(firstA)
        const owner = await fixture.join('lease-a2')
        owner.socket.send(awarenessFrame(ROOM, 61, 2, { cursor: { anchor: 2, head: 2 } }))
        await vi.waitFor(() => expect(presenceOf(fixture, 61)).toMatchObject({
          cursor: { anchor: 2, head: 2 },
        }))

        let intruder = await fixture.join('lease-b1')
        if (shape !== 'first') {
          intruder.socket.send(awarenessFrame(ROOM, 62, 1, { cursor: { anchor: 5, head: 5 } }))
          await vi.waitFor(() => expect(presenceOf(fixture, 62)).toBeDefined())
        }
        if (shape === 'reconnected') {
          await fixture.leave(intruder)
          intruder = await fixture.join('lease-b2')
        }
        intruder.socket.send(awarenessFrame(ROOM, 61, 1_000_000, { cursor: { anchor: 9, head: 9 } }))
        const reason = shape === 'owner' ? 'second_client_id' : 'foreign_client_id'
        await vi.waitFor(() => expect(
          intruder.closeMessages.length
          + (fixture.metrics.render().includes('inqtrix_collaboration_awareness_dropped_total') ? 1 : 0)
          + (presenceOf(fixture, 61)?.cursor && (presenceOf(fixture, 61)?.cursor as { anchor: number }).anchor === 9 ? 1 : 0),
        ).toBeGreaterThan(0))

        expect(intruder.closeMessages).toEqual([])
        expect(presenceOf(fixture, 61)).toEqual({
          cursor: { anchor: 2, head: 2 },
          user: { color: '#123456', id: USER_ID, name: 'Ada' },
        })
        expect(fixture.room()?.awareness.meta.get(61)?.clock).toBe(2)
        expect(fixture.metrics.render()).toContain(
          `inqtrix_collaboration_awareness_dropped_total{reason="${reason}"} 1\n`,
        )
        expect(fixture.warnings).toEqual([{ event: 'awareness_state_refused', reason }])
        expect(observer.awareness.filter((entry) => entry.clientId === 61).map((entry) => entry.clock))
          .not.toContain(1_000_000)
        expect(intruder.socket.readyState).toBe(WebSocket.OPEN)
      } finally {
        await fixture.close()
      }
    },
  )

  it('counts every refused presence frame and warns about it once per connection', async () => {
    const fixture = await presenceFixture()
    try {
      fixture.lease('lease-a1')
      fixture.lease('lease-b1', { sessionId: 'session-b', user: OTHER_USER })
      const owner = await fixture.join('lease-a1')
      owner.socket.send(awarenessFrame(ROOM, 63, 1, { cursor: { anchor: 1, head: 1 } }))
      await vi.waitFor(() => expect(presenceOf(fixture, 63)).toBeDefined())
      const intruder = await fixture.join('lease-b1')
      for (let attempt = 1; attempt <= 3; attempt += 1) {
        intruder.socket.send(awarenessFrame(ROOM, 63, 10 + attempt, { cursor: { anchor: 9, head: 9 } }))
        await vi.waitFor(() => expect(
          intruder.closeMessages.length > 0
          || fixture.metrics.render().includes(
            `inqtrix_collaboration_awareness_dropped_total{reason="foreign_client_id"} ${attempt}\n`,
          ),
        ).toBe(true))
      }
      expect(intruder.closeMessages).toEqual([])
      expect(fixture.warnings).toEqual([
        { event: 'awareness_state_refused', reason: 'foreign_client_id' },
      ])
      expect(presenceOf(fixture, 63)).toMatchObject({ cursor: { anchor: 1, head: 1 } })
    } finally {
      await fixture.close()
    }
  })

  it('takes the presence of a reconnected participant away with its connection', async () => {
    const fixture = await presenceFixture()
    try {
      fixture.lease('lease-a1')
      fixture.lease('lease-a2')
      fixture.lease('lease-o', { sessionId: 'session-o', user: THIRD_USER })
      const observer = await fixture.join('lease-o')
      const first = await fixture.join('lease-a1')
      first.socket.send(awarenessFrame(ROOM, 71, 1, { cursor: { anchor: 1, head: 1 } }))
      await vi.waitFor(() => expect(presenceOf(fixture, 71)).toBeDefined())
      await fixture.leave(first)
      await vi.waitFor(() => expect(presenceOf(fixture, 71)).toBeUndefined())
      const reconnected = await fixture.join('lease-a2')
      reconnected.socket.send(awarenessFrame(ROOM, 71, 2, { cursor: { anchor: 2, head: 2 } }))
      await vi.waitFor(() => expect(presenceOf(fixture, 71)).toMatchObject({
        cursor: { anchor: 2, head: 2 },
      }))
      const removalsBefore = removalsOf(observer, 71).length

      await fixture.leave(reconnected)
      await vi.waitFor(() => expect(presenceOf(fixture, 71)).toBeUndefined(), { timeout: 500 })
      await vi.waitFor(() => expect(removalsOf(observer, 71).length).toBe(removalsBefore + 1))
      // Coming back after leaving supersedes nobody.
      expect(fixture.metrics.render()).not.toContain(
        'inqtrix_collaboration_connections_superseded_total',
      )
    } finally {
      await fixture.close()
    }
  })

  it('drops a late presence frame of a connection that authenticated before the current owner, and supersedes it', async () => {
    const fixture = await presenceFixture()
    try {
      fixture.lease('lease-a1')
      fixture.lease('lease-a2')
      // The earlier connection authenticated first and has published nothing
      // yet; its successor publishes the presence.
      const stale = await fixture.join('lease-a1')
      const current = await fixture.join('lease-a2')
      current.socket.send(awarenessFrame(ROOM, 41, 1, { cursor: { anchor: 1, head: 1 } }))
      current.socket.send(awarenessFrame(ROOM, 41, 2, { cursor: { anchor: 2, head: 2 } }))
      await vi.waitFor(() => expect(presenceOf(fixture, 41)).toMatchObject({
        cursor: { anchor: 2, head: 2 },
      }))

      stale.socket.send(awarenessFrame(ROOM, 41, 3, { cursor: { anchor: 9, head: 9 } }))
      await vi.waitFor(() => expect(
        stale.closeMessages.length + current.closeMessages.length,
      ).toBeGreaterThan(0))

      expect(stale.closeMessages).toEqual(['connection_superseded'])
      await vi.waitFor(() => expect(stale.closes).toEqual([
        { code: 4401, reason: 'connection_superseded' },
      ]))
      expect(current.closeMessages).toEqual([])
      expect(presenceOf(fixture, 41)).toMatchObject({ cursor: { anchor: 2, head: 2 } })
      expect(fixture.room()?.awareness.meta.get(41)?.clock).toBe(2)
      const rendered = fixture.metrics.render()
      expect(rendered).toContain(
        'inqtrix_collaboration_awareness_dropped_total{reason="superseded_connection"} 1\n',
      )
      expect(rendered).toContain('inqtrix_collaboration_connections_superseded_total 1\n')
    } finally {
      await fixture.close()
    }
  })

  it('keeps one presence id per connection without closing it', async () => {
    const fixture = await presenceFixture()
    try {
      fixture.lease('lease-a1')
      const writer = await fixture.join('lease-a1')
      writer.socket.send(awarenessFrame(ROOM, 81, 1, { cursor: { anchor: 1, head: 1 } }))
      await vi.waitFor(() => expect(presenceOf(fixture, 81)).toBeDefined())
      writer.socket.send(awarenessFrame(ROOM, 82, 1, { cursor: { anchor: 2, head: 2 } }))
      writer.socket.send(awarenessFrame(ROOM, 81, 2, { cursor: { anchor: 3, head: 3 } }))
      await vi.waitFor(() => expect(
        writer.closeMessages.length + ((presenceOf(fixture, 81)?.cursor as { anchor?: number } | undefined)?.anchor === 3 ? 1 : 0),
      ).toBeGreaterThan(0))

      expect(writer.closeMessages).toEqual([])
      expect(presenceOf(fixture, 82)).toBeUndefined()
      expect(presenceOf(fixture, 81)).toMatchObject({ cursor: { anchor: 3, head: 3 } })
      expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_awareness_dropped_total{reason="second_client_id"} 1\n',
      )
    } finally {
      await fixture.close()
    }
  })

  it('removes at a connection close exactly the ids of its live client set (Hocuspocus contract)', async () => {
    // Ownership is mirrored into the client set Hocuspocus keeps per
    // connection, because Hocuspocus removes exactly those states when the
    // connection closes. An upgrade that copies the set or changes that
    // removal breaks the mirror.
    const fixture = await presenceFixture()
    try {
      fixture.lease('lease-a1')
      fixture.lease('lease-b1', { sessionId: 'session-b', user: OTHER_USER })
      const first = await fixture.join('lease-a1')
      const second = await fixture.join('lease-b1')
      first.socket.send(awarenessFrame(ROOM, 91, 1, { cursor: null }))
      second.socket.send(awarenessFrame(ROOM, 92, 1, { cursor: null }))
      await vi.waitFor(() => {
        expect(presenceOf(fixture, 91)).toBeDefined()
        expect(presenceOf(fixture, 92)).toBeDefined()
      })
      const room = fixture.room()!
      const byLease = (leaseId: string) => {
        const connection = room.getConnections().find((entry) => entry.context.leaseId === leaseId)
        if (!connection) throw new Error(`no connection with ${leaseId}`)
        return connection
      }
      const firstConnection = byLease('lease-a1')
      expect(room.getClients(firstConnection)).toBe(room.getClients(firstConnection))
      room.getClients(byLease('lease-b1')).delete(92)
      room.getClients(firstConnection).add(92)

      await fixture.leave(first)
      await vi.waitFor(() => {
        expect(presenceOf(fixture, 91)).toBeUndefined()
        expect(presenceOf(fixture, 92)).toBeUndefined()
      })
      expect(second.socket.readyState).toBe(WebSocket.OPEN)
    } finally {
      await fixture.close()
    }
  })

  it('removes the presence of an owner who withdraws it, and of no one else', async () => {
    const fixture = await presenceFixture()
    try {
      fixture.lease('lease-a1')
      fixture.lease('lease-b1', { sessionId: 'session-b', user: OTHER_USER })
      fixture.lease('lease-o', { sessionId: 'session-o', user: THIRD_USER })
      const observer = await fixture.join('lease-o')
      const owner = await fixture.join('lease-a1')
      const other = await fixture.join('lease-b1')
      // Withdrawing a presence that was never published removes nothing and
      // refuses nothing, as a page closed before its first cursor does.
      other.socket.send(awarenessFrame(ROOM, 94, 1, null))
      other.socket.send(awarenessFrame(ROOM, 94, 2, { cursor: null }))
      await vi.waitFor(() => expect(presenceOf(fixture, 94)).toBeDefined())
      expect(fixture.metrics.render()).not.toContain('inqtrix_collaboration_awareness_dropped_total')
      expect(fixture.warnings).toEqual([])
      owner.socket.send(awarenessFrame(ROOM, 93, 1, { cursor: { anchor: 1, head: 1 } }))
      await vi.waitFor(() => expect(presenceOf(fixture, 93)).toBeDefined())

      // The owner withdraws it when its page is closed.
      owner.socket.send(awarenessFrame(ROOM, 93, 2, null))
      await vi.waitFor(() => expect(presenceOf(fixture, 93)).toBeUndefined(), { timeout: 500 })
      await vi.waitFor(() => expect(removalsOf(observer, 93)).toHaveLength(1))
      expect(owner.closeMessages).toEqual([])

      // The page comes back: the presence returns.
      owner.socket.send(awarenessFrame(ROOM, 93, 3, { cursor: { anchor: 4, head: 4 } }))
      await vi.waitFor(() => expect(presenceOf(fixture, 93)).toMatchObject({
        cursor: { anchor: 4, head: 4 },
      }))

      // Another participant cannot withdraw it.
      other.socket.send(awarenessFrame(ROOM, 93, 4, null))
      await vi.waitFor(() => expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_awareness_dropped_total{reason="foreign_client_id"} 1\n',
      ))
      expect(presenceOf(fixture, 93)).toMatchObject({ cursor: { anchor: 4, head: 4 } })
      expect(other.closeMessages).toEqual([])

      // It leaves with the owner's connection.
      await fixture.leave(owner)
      await vi.waitFor(() => expect(presenceOf(fixture, 93)).toBeUndefined(), { timeout: 500 })
      await vi.waitFor(() => expect(removalsOf(observer, 93)).toHaveLength(2))
    } finally {
      await fixture.close()
    }
  })

  it('removes an owner\'s presence under a full-width id and clock, and no other presence', async () => {
    // A browser picks its presence id at random from the whole 32-bit range,
    // so the removal the service applies for the owner spans several varint
    // bytes in both the id and the clock.
    const ownerId = 0xdeadbeef
    const otherId = 0xcafebabe
    const fixture = await presenceFixture()
    try {
      fixture.lease('lease-a1')
      fixture.lease('lease-b1', { sessionId: 'session-b', user: OTHER_USER })
      fixture.lease('lease-o', { sessionId: 'session-o', user: THIRD_USER })
      const observer = await fixture.join('lease-o')
      const owner = await fixture.join('lease-a1')
      const other = await fixture.join('lease-b1')
      owner.socket.send(awarenessFrame(ROOM, ownerId, 300, { cursor: { anchor: 1, head: 1 } }))
      other.socket.send(awarenessFrame(ROOM, otherId, 300, { cursor: { anchor: 2, head: 2 } }))
      await vi.waitFor(() => {
        expect(presenceOf(fixture, ownerId)).toBeDefined()
        expect(presenceOf(fixture, otherId)).toBeDefined()
      })

      owner.socket.send(awarenessFrame(ROOM, ownerId, 301, null))
      await vi.waitFor(() => expect(removalsOf(observer, ownerId)).toHaveLength(1))

      expect(removalsOf(observer, ownerId)).toEqual([{ clientId: ownerId, clock: 301, state: null }])
      expect(presenceOf(fixture, ownerId)).toBeUndefined()
      expect(owner.closeMessages).toEqual([])
      expect(presenceOf(fixture, otherId)).toMatchObject({ cursor: { anchor: 2, head: 2 } })
      expect(removalsOf(observer, otherId)).toEqual([])
      expect(fixture.metrics.render()).not.toContain('inqtrix_collaboration_awareness_dropped_total')
    } finally {
      await fixture.close()
    }
  })

  it('forgets presence ownership when the room unloads', async () => {
    const fixture = await presenceFixture()
    try {
      fixture.lease('lease-a1')
      fixture.lease('lease-b1', { sessionId: 'session-b', user: OTHER_USER })
      const first = await fixture.join('lease-a1')
      first.socket.send(awarenessFrame(ROOM, 95, 1, { cursor: null }))
      await vi.waitFor(() => expect(presenceOf(fixture, 95)).toBeDefined())
      await fixture.leave(first)
      await vi.waitFor(() => expect(fixture.room()).toBeUndefined())

      const next = await fixture.join('lease-b1')
      next.socket.send(awarenessFrame(ROOM, 95, 1, { cursor: { anchor: 1, head: 1 } }))
      await vi.waitFor(() => expect(presenceOf(fixture, 95)).toEqual({
        cursor: { anchor: 1, head: 1 },
        user: OTHER_USER,
      }))
      expect(fixture.metrics.render()).not.toContain('inqtrix_collaboration_awareness_dropped_total')
    } finally {
      await fixture.close()
    }
  })

  it('drops a presence frame that a connection still drains after it closed', async () => {
    const fixture = await presenceFixture()
    const firstPersistence = deferred<PersistResult>()
    fixture.api.persistImplementation = async () => {
      const sequence = fixture.api.persisted.length
      if (sequence === 1) return firstPersistence.promise
      return { duplicate: false, persistedSequence: sequence, sequence }
    }
    try {
      fixture.lease('lease-a1')
      fixture.lease('lease-o', { sessionId: 'session-o', user: THIRD_USER })
      const observer = await fixture.join('lease-o')
      const writer = await fixture.join('lease-a1')
      writer.socket.send(syncFrame(ROOM, 2, editorUpdate(fixture.document, 'Presence!')))
      await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))
      // Queued behind the update that waits for the store.
      writer.socket.send(awarenessFrame(ROOM, 51, 1, { cursor: null }))
      await new Promise((resolve) => setTimeout(resolve, 30))
      await fixture.leave(writer)

      firstPersistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
      await vi.waitFor(() => expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_acknowledgements_undeliverable_total 1\n',
      ))
      await new Promise((resolve) => setTimeout(resolve, 50))
      // Nobody would ever remove it: its connection is gone.
      expect(presenceOf(fixture, 51)).toBeUndefined()
      expect(observer.awareness.filter((entry) => entry.clientId === 51)).toEqual([])
      expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_awareness_dropped_total{reason="closed_connection"} 1\n',
      )
    } finally {
      firstPersistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
      await fixture.close()
    }
  })

  it('does not persist the queued updates of a connection whose participant reconnected', async () => {
    const fixture = await presenceFixture()
    const api = fixture.api
    const firstPersistence = deferred<PersistResult>()
    api.persistImplementation = async () => {
      const sequence = api.persisted.length
      if (sequence === 1) return firstPersistence.promise
      return { duplicate: false, persistedSequence: sequence, sequence }
    }
    const [first, second] = sequentialEditorUpdates(
      fixture.document,
      ['Presence one', 'Presence one two'],
    )
    try {
      fixture.lease('lease-a1')
      fixture.lease('lease-a2')
      const predecessor = await fixture.join('lease-a1')
      predecessor.socket.send(awarenessFrame(ROOM, 41, 1, { cursor: null }))
      await vi.waitFor(() => expect(presenceOf(fixture, 41)).toBeDefined())
      predecessor.socket.send(syncFrame(ROOM, 2, first!))
      await vi.waitFor(() => expect(api.persisted).toHaveLength(1))
      // The second update waits in the predecessor's queue behind the first.
      // It must have reached the service, or its absence from the store
      // below would prove nothing.
      predecessor.socket.send(syncFrame(ROOM, 2, second!))
      await vi.waitFor(() => expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_ingress_queued_frames 1\n',
      ))

      const successor = await fixture.join('lease-a2')
      successor.socket.send(awarenessFrame(ROOM, 41, 2, { cursor: null }))
      await vi.waitFor(() => expect(predecessor.closeMessages).toEqual(['connection_superseded']))
      firstPersistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
      await vi.waitFor(() => expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_acknowledgements_undeliverable_total 1\n',
      ))
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(api.persisted.map((row) => row.update)).toEqual([first])

      // The successor resends what was not confirmed to it.
      successor.socket.send(syncFrame(ROOM, 2, second!))
      await vi.waitFor(() => expect(successor.stateless).toContainEqual({
        hash: hashBytes(second!),
        sequence: 2,
        type: 'durable_ack',
      }))
      expect(api.persisted.map((row) => row.update)).toEqual([first, second])
    } finally {
      firstPersistence.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
      await fixture.close()
    }
  })
})

describe('collaboration ingress backpressure', () => {
  it('lets awareness of an open connection pass a stuck document queue and keeps only the latest state', async () => {
    const fixture = await ingressFixture({ maxQueuedFrames: 4 })
    const clients: RoomClient[] = []
    try {
      const writer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-w', fixture.document)
      const flooder = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-f', fixture.document)
      clients.push(writer, flooder)
      const [first, second] = sequentialEditorUpdates(fixture.document, ['Hello!', 'Hello!!'])

      writer.socket.send(syncFrame(ROOM, 2, editorUpdate(fixture.document, 'Hello\n\nWorld')))
      await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))
      // The flooder's update waits at the room gate, so its connection stops
      // processing while everything below arrives.
      flooder.socket.send(syncFrame(ROOM, 2, first!))
      const clientId = 71
      for (let clock = 1; clock <= 20; clock += 1) {
        flooder.socket.send(awarenessFrame(ROOM, clientId, clock, {
          cursor: { anchor: clock, head: clock },
        }))
      }
      flooder.socket.send(syncFrame(ROOM, 2, second!))
      await new Promise((resolve) => setTimeout(resolve, 500))
      expect(flooder.closes).toEqual([])
      expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_awareness_coalesced_total 18\n',
      )

      fixture.releaseWriter()
      await vi.waitFor(() => expect(flooder.stateless).toEqual([
        { hash: hashBytes(first!), sequence: 2, type: 'durable_ack' },
        { hash: hashBytes(second!), sequence: 3, type: 'durable_ack' },
      ]))
      const awareness = fixture.harness.hocuspocus.documents.get(ROOM)?.awareness
      await vi.waitFor(() => expect(awareness?.getStates().get(clientId)).toMatchObject({
        cursor: { anchor: 20, head: 20 },
      }))
      expect(awareness?.meta.get(clientId)?.clock).toBe(20)
      expect(flooder.closes).toEqual([])
      expect(fixture.api.persisted.map((row) => row.hash).slice(1)).toEqual([
        hashBytes(first!),
        hashBytes(second!),
      ])
    } finally {
      await fixture.close(clients)
    }
  })

  it('still counts awareness against the frame budget before a connection has opened', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ maxQueuedFrames: 2, policyPollMs: 60_000 })
    const document = markdownDocument('Queued awareness')
    api.loadedState = await documentState(DOCUMENT_ID, document)
    document.destroy()
    const introspection = deferred<IntrospectedLease>()
    let introspections = 0
    api.introspectLease = async () => {
      introspections += 1
      return introspection.promise
    }
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    const socket = await openSocket(sidecar, configured.secret)
    const closed = nextClose(socket)

    socket.send(authFrame(ROOM, 'slow-token'))
    await vi.waitFor(() => expect(introspections).toBe(1))
    socket.send(awarenessFrame(ROOM, 72, 1, { cursor: null }))
    socket.send(awarenessFrame(ROOM, 72, 2, { cursor: null }))

    await expect(withTimeout(closed, 1_000, 'pre-open awareness close'))
      .resolves.toEqual({ code: 4429, reason: 'rate_limited' })
    introspection.resolve(validLease(configured, api.loadedState.schemaHash, 60))
    await sidecar.stop()
  })

  it.each(['edit', 'suggest'] as const)(
    'defers document frames of an open %s connection beyond the frame budget and processes all of them in order',
    async (access) => {
      const fixture = await ingressFixture({ maxQueuedFrames: 4 })
      const clients: RoomClient[] = []
      try {
        const writer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-w', fixture.document)
        const flooder = await joinRoom(fixture.sidecar, fixture.configured.secret, `${access}-f`, fixture.document)
        clients.push(writer, flooder)
        const updates = access === 'suggest'
          ? sequentialSuggestionUpdates(fixture.document, 'abcdefghijkl')
          : sequentialEditorUpdates(
              fixture.document,
              Array.from({ length: 12 }, (_value, index) => `Hello${'!'.repeat(index + 1)}`),
            )

        writer.socket.send(syncFrame(ROOM, 2, editorUpdate(fixture.document, 'Hello\n\nWorld')))
        await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))
        for (const update of updates) flooder.socket.send(syncFrame(ROOM, 2, update))
        // The first update waits at the room gate; four more hold the frame
        // budget and seven wait in the sidecar.
        await vi.waitFor(() => {
          const rendered = fixture.metrics.render()
          expect(rendered).toContain('inqtrix_collaboration_ingress_deferred_frames 7\n')
          expect(rendered).toContain('inqtrix_collaboration_ingress_queued_frames 4\n')
        })
        await new Promise((resolve) => setTimeout(resolve, 100))
        expect(flooder.closes).toEqual([])

        fixture.releaseWriter()
        await vi.waitFor(() => expect(flooder.stateless).toHaveLength(12), { timeout: 5_000 })
        expect(flooder.stateless).toEqual(updates.map((update, index) => ({
          hash: hashBytes(update),
          sequence: index + 2,
          type: 'durable_ack',
        })))
        expect(fixture.api.persisted.map((row) => row.hash).slice(1)).toEqual(
          updates.map((update) => hashBytes(update)),
        )
        expect(flooder.closes).toEqual([])
        expect(flooder.closeMessages).toEqual([])
        const rendered = fixture.metrics.render()
        expect(rendered).toContain('inqtrix_collaboration_ingress_deferred_frames 0\n')
        expect(rendered).not.toContain('inqtrix_collaboration_ingress_rejections_total')
      } finally {
        await fixture.close(clients)
      }
    },
    15_000,
  )

  it('closes raw with a distinct reason when the waiting frames of an open connection exceed the byte limit', async () => {
    // Frames of 1 000 bytes against a 5 500 byte limit: the four frames in
    // the budget and one waiting frame fit, the sixth that counts does not.
    const frame = sizedSyncFrame(1_000)
    const fixture = await ingressFixture({ maxQueuedBytes: 5_500, maxQueuedFrames: 4 })
    const clients: RoomClient[] = []
    try {
      const writer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-w', fixture.document)
      const flooder = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-f', fixture.document)
      clients.push(writer, flooder)

      writer.socket.send(syncFrame(ROOM, 2, editorUpdate(fixture.document, 'Hello\n\nWorld')))
      await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))
      flooder.socket.send(syncFrame(ROOM, 2, editorUpdate(fixture.document, 'Hello!')))
      await vi.waitFor(() => expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_document_queue_depth 2\n',
      ))
      for (let index = 0; index < 12; index += 1) flooder.socket.send(frame)

      await vi.waitFor(() => expect(flooder.closes).toEqual([
        { code: 4429, reason: 'ingress_overflow' },
      ]))
      // The waiting frame is discarded when the service tears the socket down.
      await vi.waitFor(() => {
        const rendered = fixture.metrics.render()
        expect(rendered).toContain(
          'inqtrix_collaboration_ingress_rejections_total{reason="ingress_overflow"} 1\n',
        )
        expect(rendered).toContain('inqtrix_collaboration_ingress_deferred_discarded_total 1\n')
        expect(rendered).toContain('inqtrix_collaboration_ingress_deferred_frames 0\n')
      })
    } finally {
      await fixture.close(clients)
    }
  })

  it('discards the waiting frames of a document connection that closes and releases their budget', async () => {
    const fixture = await ingressFixture({ maxQueuedFrames: 4, policyRevalidationTimeoutMs: 50 })
    const clients: RoomClient[] = []
    try {
      const writer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-w', fixture.document)
      const flooder = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-f', fixture.document)
      clients.push(writer, flooder)
      const updates = sequentialEditorUpdates(
        fixture.document,
        Array.from({ length: 12 }, (_value, index) => `Hello${'!'.repeat(index + 1)}`),
      )

      writer.socket.send(syncFrame(ROOM, 2, editorUpdate(fixture.document, 'Hello\n\nWorld')))
      await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))
      for (const update of updates) flooder.socket.send(syncFrame(ROOM, 2, update))
      await vi.waitFor(() => expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_ingress_deferred_frames 7\n',
      ))

      // The flooder does not answer the token request, so the revalidation
      // times out and closes only its document connection.
      const connection = fixture.harness.hocuspocus.documents.get(ROOM)
        ?.getConnections()
        .find((candidate) => candidate.context.leaseId === 'writer-f')
      if (!connection) throw new Error('ingress fixture has no flooder connection')
      fixture.harness.requestPolicyRevalidation(connection, ROOM)
      await vi.waitFor(() => expect(flooder.closeMessages).toEqual([
        'policy_revalidation_timeout',
      ]))
      let rendered = fixture.metrics.render()
      expect(rendered).toContain('inqtrix_collaboration_ingress_deferred_frames 0\n')
      expect(rendered).toContain('inqtrix_collaboration_ingress_deferred_discarded_total 7\n')
      expect(flooder.socket.readyState).toBe(WebSocket.OPEN)
      expect(flooder.closes).toEqual([])

      fixture.releaseWriter()
      await vi.waitFor(() => expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_ingress_queued_frames 0\n',
      ))
      await fixture.harness.coordinator.awaitRoom(ROOM)
      await new Promise((resolve) => setTimeout(resolve, 100))
      // The five frames that were inside Hocuspocus drain as after a
      // transport close; none of the discarded seven reaches the store.
      expect(fixture.api.persisted.map((row) => row.hash).slice(1)).toEqual(
        updates.slice(0, 5).map((update) => hashBytes(update)),
      )
      rendered = fixture.metrics.render()
      expect(rendered).toContain('inqtrix_collaboration_ingress_queued_bytes 0\n')
      expect(flooder.closes).toEqual([])
    } finally {
      await fixture.close(clients)
    }
  })

  it.each(['edit', 'suggest'] as const)(
    'closes an open %s connection that floods minimal frames behind a held update once four times the frame budget waits',
    async (access) => {
      const fixture = await ingressFixture({ maxQueuedFrames: 4 })
      const clients: RoomClient[] = []
      try {
        const writer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-w', fixture.document)
        const flooder = await joinRoom(fixture.sidecar, fixture.configured.secret, `${access}-f`, fixture.document)
        clients.push(writer, flooder)
        const writerUpdate = editorUpdate(fixture.document, 'Hello\n\nWorld')

        writer.socket.send(syncFrame(ROOM, 2, writerUpdate))
        await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))
        flooder.socket.send(syncFrame(ROOM, 2, new Uint8Array([0, 0])))
        await vi.waitFor(() => expect(fixture.metrics.render()).toContain(
          'inqtrix_collaboration_document_queue_depth 2\n',
        ))
        for (let index = 0; index < 500; index += 1) {
          flooder.socket.send(syncFrame(ROOM, 2, new Uint8Array([0, 0])))
        }

        await vi.waitFor(() => expect(flooder.closes).toEqual([
          { code: 4429, reason: 'ingress_overflow' },
        ]))
        await vi.waitFor(() => {
          const rendered = fixture.metrics.render()
          expect(rendered).toContain('inqtrix_collaboration_ingress_deferred_discarded_total 16\n')
          expect(rendered).toContain('inqtrix_collaboration_ingress_deferred_frames 0\n')
        })

        fixture.releaseWriter()
        await vi.waitFor(() => expect(writer.stateless).toContainEqual({
          hash: hashBytes(writerUpdate),
          sequence: 1,
          type: 'durable_ack',
        }))
        expect(writer.closes).toEqual([])
        expect(writer.closeMessages).toEqual([])
      } finally {
        await fixture.close(clients)
      }
    },
  )

  it('persists a burst of the frame limit minus five sent just before the writer closes, in order', async () => {
    const fixture = await ingressFixture({})
    const clients: RoomClient[] = []
    try {
      const other = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-o', fixture.document)
      const leaving = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-l', fixture.document)
      clients.push(other, leaving)
      const targets = Array.from({ length: 28 }, (_value, index) => `Hello ${index + 1}`)
      const updates = sequentialEditorUpdates(fixture.document, targets)

      // The first update holds the room gate while the rest of the burst
      // arrives; the peer closes right after the burst, and the other
      // writer's update is broadcast to the closed connection meanwhile.
      for (const update of updates) leaving.socket.send(syncFrame(ROOM, 2, update))
      leaving.socket.close(1001)
      await vi.waitFor(() => expect(leaving.closes).toHaveLength(1))
      other.socket.send(syncFrame(ROOM, 2, editorUpdate(fixture.document, 'Other\n\nHello')))
      await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))
      fixture.releaseWriter()

      await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(29), { timeout: 5_000 })
      const leavingRows = fixture.api.persisted
        .map((row) => row.hash)
        .filter((hash) => updates.some((update) => hashBytes(update) === hash))
      expect(leavingRows).toEqual(updates.map((update) => hashBytes(update)))
      // A row is written before its update is applied and acknowledged.
      await vi.waitFor(() => expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_acknowledgements_undeliverable_total 28\n',
      ))
      await fixture.harness.coordinator.awaitRoom(ROOM)
      await vi.waitFor(() => expect(
        markdownAfterUpdates(fixture.document, other.syncPayloads),
      ).toContain('Hello 28'))
      expect(other.closes).toEqual([])
    } finally {
      await fixture.close(clients)
    }
  }, 15_000)

  it('charges a waiting awareness state to the byte limit and replaces it without counting both', async () => {
    const fixture = await ingressFixture({ maxQueuedBytes: 4_096, maxQueuedFrames: 4 })
    const clients: RoomClient[] = []
    try {
      const writer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-w', fixture.document)
      const flooder = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-f', fixture.document)
      clients.push(writer, flooder)

      writer.socket.send(syncFrame(ROOM, 2, editorUpdate(fixture.document, 'Hello\n\nWorld')))
      await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))
      // The flooder's own update is processing (its reservation is released)
      // and waits at the gate, so its later frames wait in its connection.
      flooder.socket.send(syncFrame(ROOM, 2, editorUpdate(fixture.document, 'Hello!')))
      await vi.waitFor(() => expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_document_queue_depth 2\n',
      ))
      flooder.socket.send(sizedAwarenessFrame(73, 1, 100))
      flooder.socket.send(sizedAwarenessFrame(73, 2, 1_500))
      flooder.socket.send(sizedAwarenessFrame(73, 3, 1_600))
      // In flight 100 bytes, waiting 1 600 bytes: the replaced 1 500 bytes
      // are released in the same step.
      await vi.waitFor(() => expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_ingress_queued_bytes 1700\n',
      ))
      expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_awareness_coalesced_total 1\n',
      )
      expect(flooder.closes).toEqual([])

      flooder.socket.send(sizedSyncFrame(2_500))
      await vi.waitFor(() => expect(flooder.closes).toEqual([
        { code: 4429, reason: 'ingress_overflow' },
      ]))
    } finally {
      await fixture.close(clients)
    }
  })

  it('never defers a frame of an open view connection, and closes its awareness overflow as rate limited', () => {
    const sidecar = new CollaborationSidecar(
      settings({ maxQueuedBytes: 4_096, maxQueuedFrames: 4 }),
      { api: new FakeCollaborationApi(), logger: silentLogger },
    )
    const harness = sidecar as unknown as AdmissionHarness
    const transportId = 'transport-under-test'
    const addressKey = `${transportId}\0${ROOM}`
    harness.ingressUsage.set(transportId, { bytes: 3_000, frames: 4 })

    harness.openDocumentAddresses.set(addressKey, admissionConnection('view'))
    expect(harness.admitIngress(transportId, addressKey, 0, 20)).toEqual({
      kind: 'refuse',
      reason: 'rate_limited',
    })
    // The first awareness frame takes bytes but no frame slot.
    expect(harness.admitIngress(transportId, addressKey, 1, 1_000)).toEqual({
      awarenessAddress: addressKey,
      kind: 'deliver',
    })
    expect(harness.admitIngress(transportId, addressKey, 1, 1_200)).toEqual({
      kind: 'refuse',
      reason: 'rate_limited',
    })
    harness.awarenessInFlight.set(addressKey, {
      awarenessAddress: addressKey,
      bytes: 100,
      released: false,
      transportId,
    })
    expect(harness.admitIngress(transportId, addressKey, 1, 1_000)).toEqual({
      addressKey,
      kind: 'hold',
    })
    expect(harness.admitIngress(transportId, addressKey, 1, 1_200)).toEqual({
      kind: 'refuse',
      reason: 'rate_limited',
    })

    // The same frames of a writable connection wait, and its awareness
    // overflow closes with the reason of a writable connection.
    harness.openDocumentAddresses.set(addressKey, admissionConnection('suggest'))
    expect(harness.admitIngress(transportId, addressKey, 0, 20)).toEqual({
      addressKey,
      kind: 'defer',
    })
    harness.awarenessInFlight.delete(addressKey)
    expect(harness.admitIngress(transportId, addressKey, 1, 1_200)).toEqual({
      kind: 'refuse',
      reason: 'ingress_overflow',
    })
    harness.awarenessInFlight.set(addressKey, {
      awarenessAddress: addressKey,
      bytes: 100,
      released: false,
      transportId,
    })
    expect(harness.admitIngress(transportId, addressKey, 1, 1_200)).toEqual({
      kind: 'refuse',
      reason: 'ingress_overflow',
    })
  })

  it('charges the waiting awareness state of an open view connection to the byte limit', () => {
    const sidecar = new CollaborationSidecar(
      settings({ maxQueuedBytes: 4_096, maxQueuedFrames: 4 }),
      { api: new FakeCollaborationApi(), logger: silentLogger },
    )
    const harness = sidecar as unknown as AdmissionHarness
    const transportId = 'transport-under-test'
    const addressKey = `${transportId}\0${ROOM}`
    harness.ingressUsage.set(transportId, { bytes: 2_000, frames: 1 })
    harness.openDocumentAddresses.set(addressKey, admissionConnection('view'))
    harness.awarenessInFlight.set(addressKey, {
      awarenessAddress: addressKey,
      bytes: 100,
      released: false,
      transportId,
    })

    harness.holdAwareness(transportId, addressKey, new Uint8Array(1_500))
    expect(harness.ingressUsage.get(transportId)).toEqual({ bytes: 3_500, frames: 1 })
    // A later state replaces the waiting one; only the later one is charged.
    harness.holdAwareness(transportId, addressKey, new Uint8Array(1_600))
    expect(harness.ingressUsage.get(transportId)).toEqual({ bytes: 3_600, frames: 1 })
    // A document frame that fits the frame budget but not next to the waiting
    // state closes the view socket; a smaller one still enters.
    expect(harness.admitIngress(transportId, addressKey, 0, 497)).toEqual({
      kind: 'refuse',
      reason: 'rate_limited',
    })
    expect(harness.admitIngress(transportId, addressKey, 0, 496)).toEqual({
      awarenessAddress: null,
      kind: 'deliver',
    })
  })

  it('keeps the arrival order of an address whose access is lowered while its frames wait', async () => {
    const fixture = await ingressFixture({ maxQueuedFrames: 4 })
    const renewal = deferred<IntrospectedLease>()
    let renewalRequested = false
    const introspect = fixture.api.introspectLease.bind(fixture.api)
    fixture.api.introspectLease = async (input?: { token: string }) => {
      if (input?.token !== 'renewal-d') return introspect(input)
      renewalRequested = true
      return renewal.promise
    }
    const clients: RoomClient[] = []
    try {
      const writer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-w', fixture.document)
      const lowered = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-d', fixture.document)
      clients.push(writer, lowered)
      const updates = sequentialEditorUpdates(
        fixture.document,
        Array.from({ length: 10 }, (_value, index) => `Hello${'!'.repeat(index + 1)}`),
      )
      const connection = fixture.harness.hocuspocus.documents.get(ROOM)
        ?.getConnections()
        .find((candidate) => candidate.context.leaseId === 'writer-d')
      if (!connection) throw new Error('ingress fixture has no connection to lower')

      writer.socket.send(syncFrame(ROOM, 2, editorUpdate(fixture.document, 'Hello\n\nWorld')))
      await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))
      // The renewal runs next to the connection's frames; its answer lowers
      // the access while frames still wait.
      lowered.socket.send(authFrame(ROOM, 'renewal-d'))
      await vi.waitFor(() => expect(renewalRequested).toBe(true))
      for (const update of updates.slice(0, 8)) lowered.socket.send(syncFrame(ROOM, 2, update))
      await vi.waitFor(() => expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_ingress_deferred_frames 3\n',
      ))
      renewal.resolve({
        ...validLease(fixture.configured, fixture.api.loadedState!.schemaHash, 60),
        leaseId: 'writer-d-renewed',
        permission: 'view',
      })
      await vi.waitFor(() => expect(connection.readOnly).toBe(true))
      for (const update of updates.slice(8)) lowered.socket.send(syncFrame(ROOM, 2, update))
      await vi.waitFor(() => expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_ingress_deferred_frames 5\n',
      ))
      expect(lowered.closes).toEqual([])

      fixture.releaseWriter()
      // The update that was already at the gate is stored; the next frame
      // is the first judged under view access and refused.
      await vi.waitFor(() => expect(lowered.closes).toEqual([
        { code: 4403, reason: 'access_revoked' },
      ]))
      await fixture.harness.coordinator.awaitRoom(ROOM)
      await new Promise((resolve) => setTimeout(resolve, 100))
      expect(fixture.api.persisted.map((row) => row.hash).slice(1)).toEqual([
        hashBytes(updates[0]!),
      ])
      // The refused frame released its slot when it started processing, so
      // one waiting frame had moved into the connection; the other four were
      // discarded with it.
      await vi.waitFor(() => expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_ingress_deferred_discarded_total 4\n',
      ))
    } finally {
      renewal.resolve({
        ...validLease(fixture.configured, fixture.api.loadedState!.schemaHash, 60),
        leaseId: 'writer-d-renewed',
        permission: 'view',
      })
      await fixture.close(clients)
    }
  })

  it('answers the already-contained sync frames of a view connection without waiting for the room', async () => {
    const fixture = await ingressFixture({})
    const clients: RoomClient[] = []
    try {
      const writer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-w', fixture.document)
      const viewer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'view-v', fixture.document)
      clients.push(writer, viewer)

      writer.socket.send(syncFrame(ROOM, 2, editorUpdate(fixture.document, 'Hello\n\nWorld')))
      await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))
      viewer.socket.send(syncFrame(ROOM, 1, Y.encodeStateAsUpdate(fixture.document)))
      viewer.socket.send(syncFrame(ROOM, 2, new Uint8Array([0, 0])))

      // Hocuspocus's read-only path answers both while the writer's update
      // still holds the room gate.
      await vi.waitFor(() => expect(viewer.syncStatuses).toEqual([true, false]))
      const rendered = fixture.metrics.render()
      expect(rendered).toContain(
        'inqtrix_collaboration_view_sync_frames_answered_before_gate_total 2\n',
      )
      expect(rendered).toContain('inqtrix_collaboration_gate_wait_seconds_count 1\n')
      expect(viewer.closes).toEqual([])
      expect(viewer.closeMessages).toEqual([])
    } finally {
      await fixture.close(clients)
    }
  })

  it.each([
    ['a change', (document: Y.Doc) => editorUpdate(document, 'Hello!'), { code: 4403, reason: 'access_revoked' }],
    ['a malformed update', () => new Uint8Array([1, 2, 3]), { code: 4409, reason: 'invalid_schema' }],
  ] as const)('refuses %s from a view connection without waiting for the room', async (
    _label,
    frame,
    close,
  ) => {
    const fixture = await ingressFixture({})
    const clients: RoomClient[] = []
    try {
      const writer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'writer-w', fixture.document)
      const viewer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'view-v', fixture.document)
      clients.push(writer, viewer)

      writer.socket.send(syncFrame(ROOM, 2, editorUpdate(fixture.document, 'Hello\n\nWorld')))
      await vi.waitFor(() => expect(fixture.api.persisted).toHaveLength(1))
      viewer.socket.send(syncFrame(ROOM, 2, frame(fixture.document)))

      await vi.waitFor(() => expect(viewer.closes).toEqual([close]))
      expect(fixture.api.persisted).toHaveLength(1)
      expect(fixture.metrics.render()).toContain(
        `inqtrix_collaboration_rejections_total{reason="${close.reason}"} 1\n`,
      )
    } finally {
      await fixture.close(clients)
    }
  })

  it('closes a view connection in band once its already-contained frames exceed the update allowance', async () => {
    const fixture = await ingressFixture({ updateRateLimit: 3 })
    const clients: RoomClient[] = []
    try {
      const viewer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'view-v', fixture.document)
      clients.push(viewer)
      for (let index = 0; index < 5; index += 1) {
        viewer.socket.send(syncFrame(ROOM, 2, new Uint8Array([0, 0])))
      }

      await vi.waitFor(() => expect(viewer.closeMessages).toEqual(['rate_limited']))
      const rendered = fixture.metrics.render()
      expect(rendered).toContain(
        'inqtrix_collaboration_view_sync_frames_answered_before_gate_total 3\n',
      )
      expect(rendered).toContain('inqtrix_collaboration_rejections_total{reason="rate_limited"} 1\n')
    } finally {
      await fixture.close(clients)
    }
  })

  it('answers one request for the room state per document connection and refuses a repeated one', async () => {
    const fixture = await ingressFixture({ maxQueuedFrames: 64 })
    const clients: RoomClient[] = []
    try {
      const viewer = await joinRoom(fixture.sidecar, fixture.configured.secret, 'view-v', fixture.document)
      clients.push(viewer)
      expect(viewer.syncStepTwos).toBe(1)
      for (let index = 0; index < 20; index += 1) {
        viewer.socket.send(syncFrame(ROOM, 0, Y.encodeStateVector(new Y.Doc())))
      }

      await vi.waitFor(() => expect(viewer.closeMessages).toEqual(['rate_limited']))
      await new Promise((resolve) => setTimeout(resolve, 100))
      expect(viewer.syncStepTwos).toBe(1)
      expect(fixture.metrics.render()).toContain(
        'inqtrix_collaboration_rejections_total{reason="rate_limited"} 1\n',
      )
    } finally {
      await fixture.close(clients)
    }
  })

  it.each([
    ['the default limits', {}, 28, 60_000],
    ['the smallest frame and byte limits', { frameLimitBytes: 64 * 1024, maxQueuedBytes: 64 * 1024 }, 3, 14_000],
  ] as const)(
    'stores a replay that resends one update per acknowledgement into a slowly loading room, with %s',
    async (_label, limits, count, entryBytes) => {
      const fixture = await ingressFixture({ ...limits }, { holdWriter: false, loadDelayMs: 300 })
      const entries = paddedEditorUpdates(fixture.document, count, entryBytes)
      const transport = new Y.Doc()
      Y.applyUpdate(transport, Y.encodeStateAsUpdate(fixture.document))
      for (const entry of entries) Y.applyUpdate(transport, entry)
      const clients: RoomClient[] = []
      try {
        const writer = await openRoomClient(fixture.sidecar, fixture.configured.secret)
        clients.push(writer)
        // What a browser sends on a new socket: its token, its sync request,
        // its own presence, and the first unacknowledged update. The room
        // loads meanwhile.
        writer.socket.send(authFrame(ROOM, 'writer-r'))
        await vi.waitFor(() => expect(writer.auth).toEqual([{ kind: 'authenticated' }]))
        writer.socket.send(syncFrame(ROOM, 0, Y.encodeStateVector(transport)))
        writer.socket.send(awarenessFrame(ROOM, 74, 1, { cursor: null }))
        writer.socket.send(syncFrame(ROOM, 2, entries[0]!))
        // The provider answers the room's own sync request with everything
        // the room lacks; then one update follows each acknowledgement.
        await vi.waitFor(() => expect(writer.syncRequests).toHaveLength(1), { timeout: 2_000 })
        writer.socket.send(syncFrame(
          ROOM,
          1,
          Y.encodeStateAsUpdate(transport, writer.syncRequests[0]!),
        ))
        const entryHashes = entries.map((entry) => hashBytes(entry))
        // The sync answer is acknowledged under its own hash, which no entry
        // carries; the browser ignores that acknowledgement.
        const acknowledgedEntries = (): string[] => writer.stateless
          .map((ack) => (ack as { hash: string }).hash)
          .filter((hash) => entryHashes.includes(hash))
        for (let index = 1; index < entries.length; index += 1) {
          await vi.waitFor(
            () => expect(acknowledgedEntries()).toContain(entryHashes[index - 1]),
            { timeout: 2_000 },
          )
          writer.socket.send(syncFrame(ROOM, 2, entries[index]!))
        }
        await vi.waitFor(
          () => expect(acknowledgedEntries()).toEqual(entryHashes),
          { timeout: 2_000 },
        )
        expect(writer.closes).toEqual([])
        expect(writer.closeMessages).toEqual([])
        expect(fixture.api.persisted[0]?.hash).toBe(hashBytes(entries[0]!))
        expect(fixture.api.persisted).toHaveLength(2)
        const stored = new Y.Doc()
        Y.applyUpdate(stored, Y.encodeStateAsUpdate(fixture.document))
        for (const row of fixture.api.persisted) Y.applyUpdate(stored, row.update)
        expect(serializeEditorJson(editorYDocToJson(stored), 'final'))
          .toBe(serializeEditorJson(editorYDocToJson(transport), 'final'))
        stored.destroy()
      } finally {
        transport.destroy()
        await fixture.close(clients)
      }
    },
    20_000,
  )

  it('reports the event-loop delay since the previous scrape', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ policyPollMs: 60_000 })
    const sidecar = new CollaborationSidecar(configured, {
      api,
      logger: silentLogger,
    })
    await sidecar.start()
    try {
      const scrape = async (): Promise<number> => {
        const response = await fetch(`http://127.0.0.1:${sidecar.address?.port}/metrics`)
        const line = (await response.text())
          .split('\n')
          .find((entry) => entry.startsWith('inqtrix_collaboration_event_loop_delay_seconds '))
        if (!line) throw new Error('event-loop delay gauge is missing')
        return Number(line.split(' ').at(-1))
      }
      await scrape()
      await new Promise<void>((resolve) => setTimeout(() => {
        const blockedUntil = Date.now() + 200
        while (Date.now() < blockedUntil) {
          // Blocks the event loop the service shares with this test.
        }
        resolve()
      }, 30))
      await new Promise((resolve) => setTimeout(resolve, 50))
      expect(await scrape()).toBeGreaterThanOrEqual(0.15)
      expect(await scrape()).toBeLessThan(0.1)
    } finally {
      await sidecar.stop()
    }
  })
})

function validLease(
  configured: ReturnType<typeof settings>,
  schemaHash: string,
  lifetimeSeconds: number,
): IntrospectedLease {
  return {
    documentId: DOCUMENT_ID,
    expiresAt: Date.now() / 1_000 + lifetimeSeconds,
    generation: 1,
    leaseId: 'lease-1',
    permission: 'edit',
    policyCursor: 0,
    protocolVersion: configured.protocolVersion,
    schemaHash,
    schemaVersion: configured.schemaVersion,
    sessionId: 'session-1',
    tenantId: configured.tenantId,
    user: { color: '#123456', id: USER_ID, name: 'Ada' },
  }
}

async function openSocket(
  sidecar: CollaborationSidecar,
  secret: string,
): Promise<WebSocket> {
  const socket = new WebSocket(
    `ws://127.0.0.1:${sidecar.address?.port}/collaboration`,
    { headers: { Authorization: `Bearer ${secret}` } },
  )
  await new Promise<void>((resolve, reject) => {
    socket.once('open', resolve)
    socket.once('error', reject)
  })
  return socket
}

function nextMessage(socket: WebSocket): Promise<void> {
  return new Promise((resolve, reject) => {
    socket.once('message', () => resolve())
    socket.once('error', reject)
  })
}

function nextClose(socket: WebSocket): Promise<{ code: number; reason: string }> {
  return new Promise((resolve) => {
    socket.once('close', (code, reason) => {
      resolve({ code, reason: reason.toString('utf8') })
    })
  })
}

function withTimeout<T>(promise: Promise<T>, timeoutMs: number, label: string): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`${label} timed out`)), timeoutMs)
    void promise.then(
      (value) => {
        clearTimeout(timer)
        resolve(value)
      },
      (error: unknown) => {
        clearTimeout(timer)
        reject(error)
      },
    )
  })
}

function authFrame(room: string, token: string): Uint8Array {
  return protocolFrame(room, 2, concat(varUint(0), varString(token)))
}

/** One awareness entry; a null state is its sender's removal. */
function awarenessFrame(
  room: string,
  clientId: number,
  clock: number,
  state: Record<string, unknown> | null,
): Uint8Array {
  const update = concat(
    varUint(1),
    varUint(clientId),
    varUint(clock),
    varString(JSON.stringify(state)),
  )
  return protocolFrame(room, 1, varBytes(update))
}

function syncFrame(room: string, type: number, update: Uint8Array): Uint8Array {
  return protocolFrame(room, 0, concat(varUint(type), varBytes(update)))
}

function reconcileFrame(room: string, hash: string): Uint8Array {
  return protocolFrame(room, 5, varString(JSON.stringify({
    hashes: [hash],
    type: 'durability_reconcile',
  })))
}

function varBytes(value: Uint8Array): Uint8Array {
  return concat(varUint(value.byteLength), value)
}

function editorUpdate(document: Y.Doc, targetMarkdown: string): Uint8Array {
  const replica = new Y.Doc()
  Y.applyUpdate(replica, Y.encodeStateAsUpdate(document))
  const vector = Y.encodeStateVector(replica)
  const fragment = replica.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, editorSchema)
  updateYFragment(
    replica,
    fragment,
    editorSchema.nodeFromJSON(parseEditorMarkdown(targetMarkdown)),
    initialized.meta,
  )
  const update = Y.encodeStateAsUpdate(replica, vector)
  replica.destroy()
  return update
}

function suggestionWithTransientTextUpdate(document: Y.Doc): Uint8Array {
  const replica = new Y.Doc()
  Y.applyUpdate(replica, Y.encodeStateAsUpdate(document))
  const vector = Y.encodeStateVector(replica)
  const fragment = replica.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, editorSchema)
  let textPosition: number | null = null
  initialized.doc.descendants((node, position) => {
    if (textPosition === null && node.isText) textPosition = position
  })
  if (textPosition === null) throw new Error('Transient-history fixture has no text')
  const state = EditorState.create({ schema: editorSchema, doc: initialized.doc })
  const tracked = transformToInqtrixSuggestionTransaction(
    state.tr.insertText('!', textPosition + 1),
    state,
    {
      authorId: USER_ID,
      createdAt: 1_784_112_100,
      patchId: '22222222-2222-4222-8222-222222222222',
    },
    () => '33333333-3333-4333-8333-333333333333',
  )
  updateYFragment(replica, fragment, tracked.doc, initialized.meta)
  const paragraph = fragment.get(1)
  const text = paragraph instanceof Y.XmlElement ? paragraph.get(0) : null
  if (!(text instanceof Y.XmlText)) throw new Error('Transient-history target is invalid')
  const hidden = 'x'.repeat(64 * 1024)
  text.insert(1, hidden)
  text.delete(1, hidden.length)
  const update = Y.encodeStateAsUpdate(replica, vector)
  replica.destroy()
  return update
}

function protocolFrame(room: string, type: number, payload: Uint8Array): Uint8Array {
  return concat(varString(room), varUint(type), payload)
}

function varString(value: string): Uint8Array {
  const encoded = new TextEncoder().encode(value)
  return concat(varUint(encoded.byteLength), encoded)
}

function varUint(value: number): Uint8Array {
  const bytes: number[] = []
  let remaining = value
  do {
    let byte = remaining & 0x7f
    remaining = Math.floor(remaining / 128)
    if (remaining > 0) byte |= 0x80
    bytes.push(byte)
  } while (remaining > 0)
  return new Uint8Array(bytes)
}

function concat(...parts: Uint8Array[]): Uint8Array {
  const result = new Uint8Array(parts.reduce((total, part) => total + part.byteLength, 0))
  let offset = 0
  for (const part of parts) {
    result.set(part, offset)
    offset += part.byteLength
  }
  return result
}

function sequentialEditorUpdates(
  document: Y.Doc,
  targetMarkdowns: readonly string[],
): Uint8Array[] {
  const replica = new Y.Doc()
  Y.applyUpdate(replica, Y.encodeStateAsUpdate(document))
  const fragment = replica.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const updates: Uint8Array[] = []
  for (const targetMarkdown of targetMarkdowns) {
    const vector = Y.encodeStateVector(replica)
    const initialized = initProseMirrorDoc(fragment, editorSchema)
    updateYFragment(
      replica,
      fragment,
      editorSchema.nodeFromJSON(parseEditorMarkdown(targetMarkdown)),
      initialized.meta,
    )
    updates.push(Y.encodeStateAsUpdate(replica, vector))
  }
  replica.destroy()
  return updates
}

/** The updates an editor emits for consecutive changes: each carries only
 * what its own transaction inserted and deleted. */
function transactionUpdates(
  document: Y.Doc,
  targetMarkdowns: readonly string[],
): Uint8Array[] {
  const replica = new Y.Doc()
  Y.applyUpdate(replica, Y.encodeStateAsUpdate(document))
  const fragment = replica.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const updates: Uint8Array[] = []
  replica.on('update', (update: Uint8Array) => updates.push(update))
  for (const targetMarkdown of targetMarkdowns) {
    const initialized = initProseMirrorDoc(fragment, editorSchema)
    replica.transact(() => updateYFragment(
      replica,
      fragment,
      editorSchema.nodeFromJSON(parseEditorMarkdown(targetMarkdown)),
      initialized.meta,
    ))
  }
  replica.destroy()
  if (updates.length !== targetMarkdowns.length) throw new Error('one update per change expected')
  return updates
}

function markdownAfterUpdates(seed: Y.Doc, updates: readonly Uint8Array[]): string {
  const replica = new Y.Doc()
  Y.applyUpdate(replica, Y.encodeStateAsUpdate(seed))
  for (const update of updates) Y.applyUpdate(replica, update)
  const markdown = serializeEditorJson(editorYDocToJson(replica), 'final').trim()
  replica.destroy()
  return markdown
}

type PersistResult = {
  duplicate: boolean
  persistedSequence: number
  sequence: number
}

type RoomClient = {
  auth: Array<{ kind: 'authenticated' } | { kind: 'denied'; reason: string }>
  /** Every awareness entry the room sent, in order; `state` is null for a
   * removal. */
  awareness: Array<{ clientId: number; clock: number; state: unknown }>
  /** Reasons of in-band close messages; the socket itself stays open. */
  closeMessages: string[]
  closes: Array<{ code: number; reason: string }>
  socket: WebSocket
  stateless: unknown[]
  syncPayloads: Uint8Array[]
  /** State vectors of the room's own sync requests. */
  syncRequests: Uint8Array[]
  /** Hocuspocus sync status answers: true when the update was saved. */
  syncStatuses: boolean[]
  syncStepTwos: number
}

type RoomHarness = {
  authenticatedAddresses: Set<string>
  connections: Map<string, { transportId: string }>
  coordinator: {
    awaitRoom(room: string): Promise<void>
    getPersistedSequence(room: string): number
    requiresReconstruction(room: string): boolean
  }
  hocuspocus: {
    documents: Map<string, {
      getConnections(): Array<{
        context: { leaseId?: string }
        readOnly: boolean
      }>
    }>
  }
  sockets: Map<string, WebSocket>
  transportConnections: Map<string, Set<string>>
}

async function openRoomClient(
  sidecar: CollaborationSidecar,
  secret: string,
): Promise<RoomClient> {
  const socket = await openSocket(sidecar, secret)
  const client: RoomClient = {
    auth: [],
    awareness: [],
    closeMessages: [],
    closes: [],
    socket,
    stateless: [],
    syncPayloads: [],
    syncRequests: [],
    syncStatuses: [],
    syncStepTwos: 0,
  }
  socket.on('message', (data) => recordServerFrame(client, rawMessageBytes(data)))
  socket.on('close', (code, reason) => {
    client.closes.push({ code, reason: reason.toString('utf8') })
  })
  return client
}

async function joinRoom(
  sidecar: CollaborationSidecar,
  secret: string,
  token: string,
  seed: Y.Doc,
  room = ROOM,
): Promise<RoomClient> {
  const client = await openRoomClient(sidecar, secret)
  client.socket.send(authFrame(room, token))
  await vi.waitFor(() => expect(client.auth.length + client.closes.length).toBeGreaterThan(0))
  if (client.auth[0]?.kind !== 'authenticated') {
    throw new Error(`room join was refused: ${JSON.stringify({
      auth: client.auth,
      closes: client.closes,
    })}`)
  }
  client.socket.send(syncFrame(room, 0, Y.encodeStateVector(seed)))
  await vi.waitFor(() => expect(client.syncStepTwos).toBeGreaterThan(0))
  return client
}

async function closeClients(clients: readonly RoomClient[]): Promise<void> {
  await Promise.all(clients.map(async (client) => {
    if (client.socket.readyState === WebSocket.CLOSED) return
    const closed = new Promise<void>((resolve) => {
      client.socket.once('close', () => resolve())
    })
    if (client.socket.readyState === WebSocket.OPEN) client.socket.close()
    await withTimeout(closed, 1_000, 'room client close')
  }))
}

function rawMessageBytes(data: WebSocket.RawData): Uint8Array {
  if (data instanceof ArrayBuffer) return new Uint8Array(data)
  if (Array.isArray(data)) return new Uint8Array(Buffer.concat(data))
  return new Uint8Array(data)
}

/** Decodes the Hocuspocus frames the tests assert on: authentication,
 * document sync payloads, awareness entries, stateless JSON and in-band close
 * messages. */
function recordServerFrame(client: RoomClient, bytes: Uint8Array): void {
  const reader = frameReader(bytes)
  reader.string()
  const type = reader.uint()
  if (type === 0) {
    const syncType = reader.uint()
    if (syncType === 0) client.syncRequests.push(reader.bytes())
    if (syncType === 1 || syncType === 2) {
      client.syncPayloads.push(reader.bytes())
      if (syncType === 1) client.syncStepTwos += 1
    }
    return
  }
  if (type === 1) {
    const entries = frameReader(reader.bytes())
    const count = entries.uint()
    for (let index = 0; index < count; index += 1) {
      client.awareness.push({
        clientId: entries.uint(),
        clock: entries.uint(),
        state: JSON.parse(entries.string()),
      })
    }
    return
  }
  if (type === 8) {
    client.syncStatuses.push(reader.uint() === 1)
    return
  }
  if (type === 2) {
    const authType = reader.uint()
    if (authType === 2) client.auth.push({ kind: 'authenticated' })
    if (authType === 1) client.auth.push({ kind: 'denied', reason: reader.string() })
    return
  }
  if (type === 5) client.stateless.push(JSON.parse(reader.string()))
  if (type === 7) client.closeMessages.push(reader.string())
}

function frameReader(bytes: Uint8Array): {
  bytes: () => Uint8Array
  string: () => string
  uint: () => number
} {
  let offset = 0
  const uint = (): number => {
    let value = 0
    let shift = 0
    while (offset < bytes.byteLength) {
      const byte = bytes[offset++]!
      value += (byte & 0x7f) * (2 ** shift)
      if ((byte & 0x80) === 0) return value
      shift += 7
    }
    throw new Error('truncated server frame')
  }
  const slice = (): Uint8Array => {
    const length = uint()
    const value = bytes.slice(offset, offset + length)
    offset += length
    return value
  }
  return {
    bytes: slice,
    string: () => new TextDecoder().decode(slice()),
    uint,
  }
}

function rejectedUpgrade(url: string): Promise<number> {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(url)
    socket.once('open', () => {
      socket.close()
      reject(new Error('WebSocket unexpectedly opened'))
    })
    socket.once('unexpected-response', (_request, response) => {
      response.resume()
      resolve(response.statusCode ?? 0)
    })
    socket.once('error', () => undefined)
  })
}

type OutboundHarness = {
  coordinator: { isBroadcastBlocked(room: string): boolean }
  flushHeldOutbound(room: string): void
  sendHocuspocusPayload(
    transportId: string,
    socket: WebSocket,
    payload: Uint8Array,
  ): void
}

type CommentPolicyHarness = {
  hocuspocus: {
    documents: Map<string, {
      getConnections(): Array<{
        context: {
          documentId: string
          expiresAt: number
          generation: number
          leaseId: string
          policyCursor: number
          tenantId: string
          user: { id: string }
        }
        sendStateless(payload: string): void
      }>
      name: string
    }>
  }
  policyPollInFlight: Promise<void> | null
  pollPolicyEvents(): Promise<void>
}

type GuestPolicyConnection = {
  close: ReturnType<typeof vi.fn>
  context: {
    documentId: string
    expiresAt: number
    generation: number
    leaseId: string
    policyCursor: number
    tenantId: string
    user: { id: string; kind: 'guest' | 'user' }
  }
  requestToken: ReturnType<typeof vi.fn>
  socketId: string
}

type GuestPolicyHarness = {
  hocuspocus: {
    documents: Map<string, {
      getConnections(): GuestPolicyConnection[]
      name: string
    }>
  }
  policyPollInFlight: Promise<void> | null
  pollPolicyEvents(): Promise<void>
}

type TokenRenewalHarness = {
  authenticator: {
    renew: ReturnType<typeof vi.fn>
  }
  renewConnectionToken(
    connection: GuestPolicyConnection,
    context: GuestPolicyConnection['context'],
    documentName: string,
    token: string,
  ): Promise<unknown>
}

type ShutdownHarness = {
  sockets: Map<string, WebSocket>
}

type LeaseHarness = {
  leaseManager: { isReady(): boolean; renewNow(): Promise<void> }
}

type StartupHarness = {
  leaseManager: { isReady(): boolean }
  listenHttpServer(): Promise<void>
  maintenanceTimer: ReturnType<typeof setInterval> | null
  policyTimer: ReturnType<typeof setInterval> | null
}

function policyConnection({
  id,
  kind,
  policyCursor = 0,
  socketId,
}: {
  id: string
  kind: 'guest' | 'user'
  policyCursor?: number
  socketId: string
}): GuestPolicyConnection {
  return {
    close: vi.fn(),
    context: {
      documentId: DOCUMENT_ID,
      expiresAt: Date.now() / 1_000 + 60,
      generation: 1,
      leaseId: `lease-${socketId}`,
      policyCursor,
      tenantId: 'tenant-1',
      user: { id, kind },
    },
    requestToken: vi.fn(),
    socketId,
  }
}

function fakeOutboundSocket(drainImmediately: boolean): {
  close: ReturnType<typeof vi.fn>
  send: ReturnType<typeof vi.fn>
  socket: WebSocket
} {
  const close = vi.fn()
  const send = vi.fn((
    _payload: unknown,
    callback: (error?: Error) => void,
  ) => {
    if (drainImmediately) callback()
  })
  return {
    close,
    send,
    socket: {
      bufferedAmount: 0,
      close,
      readyState: WebSocket.OPEN,
      send,
    } as unknown as WebSocket,
  }
}

type IngressFixture = {
  api: FakeCollaborationApi
  close: (clients: readonly RoomClient[]) => Promise<void>
  configured: ReturnType<typeof settings>
  document: Y.Doc
  harness: IngressHarness
  metrics: SidecarMetrics
  releaseWriter: () => void
  sidecar: CollaborationSidecar
}

type IngressHarness = Omit<RoomHarness, 'hocuspocus'> & {
  hocuspocus: {
    documents: Map<string, {
      awareness: {
        getStates(): Map<number, Record<string, unknown>>
        meta: Map<number, { clock: number }>
      }
      getConnections(): Array<{
        context: { leaseId?: string }
        readOnly: boolean
      }>
    }>
  }
  requestPolicyRevalidation(connection: unknown, documentName: string): void
}

type AdmissionHarness = {
  admitIngress(
    transportId: string,
    addressKey: string | null,
    type: number | undefined,
    bytes: number,
  ): unknown
  awarenessInFlight: Map<string, {
    awarenessAddress: string | null
    bytes: number
    released: boolean
    transportId: string
  }>
  holdAwareness(transportId: string, addressKey: string, update: Uint8Array): void
  ingressUsage: Map<string, { bytes: number; frames: number }>
  openDocumentAddresses: Map<string, unknown>
}

/**
 * A room with the seed "Hello". The lease's access follows the token:
 * `view-…` and `suggest-…` tokens get that access, every other token edit.
 * Unless `holdWriter` is false, the first update that reaches the store waits
 * until `releaseWriter()`, so it holds the room gate; every other store call
 * completes after a macrotask, as a real store does.
 */
async function ingressFixture(
  overrides: Parameters<typeof settings>[0],
  options: { holdWriter?: boolean; loadDelayMs?: number } = {},
): Promise<IngressFixture> {
  const api = new FakeCollaborationApi()
  const metrics = new SidecarMetrics()
  const configured = settings({ policyPollMs: 60_000, ...overrides })
  const document = markdownDocument('Hello')
  api.loadedState = await documentState(DOCUMENT_ID, document)
  const schemaHash = api.loadedState.schemaHash
  api.introspectLease = async (input?: { token: string }) => {
    const token = input?.token ?? ''
    return {
      ...validLease(configured, schemaHash, 60),
      leaseId: token,
      permission: token.startsWith('view-')
        ? 'view'
        : token.startsWith('suggest-') ? 'suggest' : 'edit',
    }
  }
  if (options.loadDelayMs) {
    const load = api.loadDocumentState.bind(api)
    api.loadDocumentState = async (input) => {
      await new Promise((resolve) => setTimeout(resolve, options.loadDelayMs))
      return load(input)
    }
  }
  const writer = deferred<PersistResult>()
  const holdWriter = options.holdWriter ?? true
  api.persistImplementation = async () => {
    const sequence = api.persisted.length
    if (holdWriter && sequence === 1) return writer.promise
    await new Promise((resolve) => setTimeout(resolve, 0))
    return { duplicate: false, persistedSequence: sequence, sequence }
  }
  const sidecar = new CollaborationSidecar(configured, {
    api,
    logger: silentLogger,
    metrics,
  })
  await sidecar.start()
  const releaseWriter = (): void => {
    writer.resolve({ duplicate: false, persistedSequence: 1, sequence: 1 })
  }
  return {
    api,
    close: async (clients) => {
      releaseWriter()
      await closeClients(clients)
      document.destroy()
      await sidecar.stop()
    },
    configured,
    document,
    harness: sidecar as unknown as IngressHarness,
    metrics,
    releaseWriter,
    sidecar,
  }
}

function admissionConnection(access: 'edit' | 'suggest' | 'view'): unknown {
  return { context: { access } }
}

/** An awareness frame of exactly `totalBytes` bytes. */
function sizedAwarenessFrame(clientId: number, clock: number, totalBytes: number): Uint8Array {
  let padding = 0
  while (true) {
    const frame = awarenessFrame(ROOM, clientId, clock, { pad: 'p'.repeat(padding) })
    if (frame.byteLength === totalBytes) return frame
    if (frame.byteLength > totalBytes) throw new Error(`no awareness frame of ${totalBytes} bytes`)
    padding += 1
  }
}

/** A document update frame of exactly `totalBytes` bytes. */
function sizedSyncFrame(totalBytes: number): Uint8Array {
  const overhead = syncFrame(ROOM, 2, new Uint8Array(0)).byteLength
  for (let payload = totalBytes - overhead; payload >= 0; payload -= 1) {
    const frame = syncFrame(ROOM, 2, new Uint8Array(payload))
    if (frame.byteLength === totalBytes) return frame
  }
  throw new Error(`no sync frame of ${totalBytes} bytes`)
}

/** One suggest-mode writer typing `characters` at the end of the first
 * paragraph, one update per keystroke. */
function sequentialSuggestionUpdates(document: Y.Doc, characters: string): Uint8Array[] {
  const replica = new Y.Doc()
  Y.applyUpdate(replica, Y.encodeStateAsUpdate(document))
  const fragment = replica.getXmlFragment(EDITOR_YJS_FRAGMENT)
  let identifier = 0
  const nextId = (): string => {
    identifier += 1
    return `44444444-4444-4444-8444-${identifier.toString(16).padStart(12, '0')}`
  }
  const updates: Uint8Array[] = []
  for (const character of characters) {
    const vector = Y.encodeStateVector(replica)
    const initialized = initProseMirrorDoc(fragment, editorSchema)
    const state = EditorState.create({ schema: editorSchema, doc: initialized.doc })
    const end = 1 + (initialized.doc.firstChild?.content.size ?? 0)
    const tracked = transformToInqtrixSuggestionTransaction(
      state.tr.insertText(character, end),
      state,
      {
        authorId: USER_ID,
        createdAt: 1_784_112_100,
        patchId: '22222222-2222-4222-8222-222222222222',
      },
      nextId,
    )
    updateYFragment(replica, fragment, tracked.doc, initialized.meta)
    updates.push(Y.encodeStateAsUpdate(replica, vector))
  }
  replica.destroy()
  return updates
}

/** `count` sequential updates, each adding a paragraph of about
 * `paragraphBytes` letters. */
function paddedEditorUpdates(
  document: Y.Doc,
  count: number,
  paragraphBytes: number,
): Uint8Array[] {
  const paragraphs: string[] = []
  const targets: string[] = []
  for (let index = 0; index < count; index += 1) {
    paragraphs.push(`MARKE${index} ${'a'.repeat(paragraphBytes)}`)
    targets.push(['Hello', ...paragraphs].join('\n\n'))
  }
  return sequentialEditorUpdates(document, targets)
}

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds))
}

/** Sends each update and waits for its acknowledgement, the way an editor
 * keeps typing; the first update is acknowledged at `firstSequence`. */
async function typeAcknowledged(
  client: RoomClient,
  updates: readonly Uint8Array[],
  firstSequence = 1,
): Promise<void> {
  for (const [index, update] of updates.entries()) {
    client.socket.send(syncFrame(ROOM, 2, update))
    await vi.waitFor(() => expect(client.stateless).toContainEqual(expect.objectContaining({
      sequence: firstSequence + index,
      type: 'durable_ack',
    })))
  }
}
