import {
  editorCollaborationRoom,
  getEditorSchemaFingerprint,
} from '@inqtrix/editor-schema'

import { CollaborationAuthenticator } from '../src/authenticator'
import { InstanceLeaseManager } from '../src/instanceLease'
import { SidecarMetrics } from '../src/metrics'
import { FakeCollaborationApi, USER_ID, settings, silentLogger } from './helpers'

describe('collaboration authentication', () => {
  it('rejects a lease whose document or generation does not match the room', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings()
    const leaseManager = new InstanceLeaseManager(
      api,
      configured,
      silentLogger,
      new SidecarMetrics(),
      () => undefined,
    )
    await leaseManager.start()
    api.lease = {
      documentId: 'ed_other',
      expiresAt: Date.now() / 1_000 + 60,
      generation: 1,
      leaseId: 'lease-1',
      permission: 'edit',
      policyCursor: 0,
      protocolVersion: configured.protocolVersion,
      schemaHash: await getEditorSchemaFingerprint(),
      schemaVersion: configured.schemaVersion,
      sessionId: 'session-1',
      tenantId: 'tenant-1',
      user: { color: '#123456', id: USER_ID, name: 'Ada' },
    }
    const authenticator = new CollaborationAuthenticator(api, leaseManager, configured)

    await expect(authenticator.authenticate(
      editorCollaborationRoom('ed_test', 1),
      'opaque-document-lease',
    )).rejects.toMatchObject({ code: 4409, reason: 'update_required' })
    await leaseManager.stop()
  })

  it('keeps a renewed token bound to the original user and session', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings()
    const leaseManager = new InstanceLeaseManager(
      api,
      configured,
      silentLogger,
      new SidecarMetrics(),
      () => undefined,
    )
    await leaseManager.start()
    api.lease = {
      documentId: 'ed_test',
      expiresAt: Date.now() / 1_000 + 60,
      generation: 1,
      leaseId: 'lease-2',
      permission: 'edit',
      policyCursor: 0,
      protocolVersion: configured.protocolVersion,
      schemaHash: await getEditorSchemaFingerprint(),
      schemaVersion: configured.schemaVersion,
      sessionId: 'different-session',
      tenantId: 'tenant-1',
      user: { color: '#123456', id: USER_ID, name: 'Ada' },
    }
    const authenticator = new CollaborationAuthenticator(api, leaseManager, configured)

    await expect(authenticator.renew({
      access: 'edit',
      documentId: 'ed_test',
      expiresAt: Date.now() / 1_000 + 30,
      generation: 1,
      leaseId: 'lease-1',
      policyCursor: 0,
      protocolVersion: configured.protocolVersion,
      schemaHash: await getEditorSchemaFingerprint(),
      schemaVersion: configured.schemaVersion,
      sessionId: 'session-1',
      tenantId: 'tenant-1',
      user: { color: '#123456', id: USER_ID, name: 'Ada' },
    }, editorCollaborationRoom('ed_test', 1), 'renewed-token')).rejects.toMatchObject({
      code: 4403,
      reason: 'access_revoked',
    })
    await leaseManager.stop()
  })

  it('rejects an introspected lease from another tenant', async () => {
    const api = new FakeCollaborationApi()
    const configured = settings({ tenantId: 'tenant-primary' })
    const leaseManager = new InstanceLeaseManager(
      api,
      configured,
      silentLogger,
      new SidecarMetrics(),
      () => undefined,
    )
    await leaseManager.start()
    api.lease = {
      documentId: 'ed_test',
      expiresAt: Date.now() / 1_000 + 60,
      generation: 1,
      leaseId: 'lease-1',
      permission: 'edit',
      policyCursor: 0,
      protocolVersion: configured.protocolVersion,
      schemaHash: await getEditorSchemaFingerprint(),
      schemaVersion: configured.schemaVersion,
      sessionId: 'session-1',
      tenantId: 'tenant-other',
      user: { color: '#123456', id: USER_ID, name: 'Ada' },
    }
    const authenticator = new CollaborationAuthenticator(api, leaseManager, configured)

    await expect(authenticator.authenticate(
      editorCollaborationRoom('ed_test', 1),
      'opaque-document-lease',
    )).rejects.toMatchObject({ code: 4401, reason: 'invalid_lease' })
    await leaseManager.stop()
  })
})
