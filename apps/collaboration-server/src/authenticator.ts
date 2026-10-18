import {
  getEditorSchemaFingerprint,
  parseEditorCollaborationRoom,
} from '@inqtrix/editor-schema'

import type {
  CollaborationApi,
  CollaborationSettings,
  ConnectionContext,
} from './contracts'
import { CloseCodes, CollaborationError, collaborationError } from './errors'
import { InstanceLeaseManager } from './instanceLease'

export class CollaborationAuthenticator {
  constructor(
    private readonly api: CollaborationApi,
    private readonly leaseManager: InstanceLeaseManager,
    private readonly settings: CollaborationSettings,
  ) {}

  async authenticate(room: string, token: string): Promise<ConnectionContext> {
    const parsedRoom = parseEditorCollaborationRoom(room)
    if (!parsedRoom) {
      throw new CollaborationError('invalid_room', {
        closeCode: CloseCodes.incompatible,
        httpStatus: 409,
      })
    }
    if (!token || token.length > 8_192) {
      throw new CollaborationError('invalid_lease', {
        closeCode: CloseCodes.leaseInvalid,
        httpStatus: 401,
      })
    }

    try {
      const introspected = await this.api.introspectLease({
        fence: this.leaseManager.assertActive(),
        room,
        token,
      })
      const schemaHash = await getEditorSchemaFingerprint()
      if (introspected.tenantId !== this.settings.tenantId) {
        throw new CollaborationError('invalid_lease', {
          closeCode: CloseCodes.leaseInvalid,
          httpStatus: 401,
        })
      }
      if (
        introspected.documentId !== parsedRoom.documentId
        || introspected.generation !== parsedRoom.generation
        || introspected.protocolVersion !== this.settings.protocolVersion
        || introspected.schemaVersion !== this.settings.schemaVersion
        || introspected.schemaHash !== schemaHash
      ) {
        throw new CollaborationError('update_required', {
          closeCode: CloseCodes.incompatible,
          httpStatus: 409,
        })
      }
      if (introspected.expiresAt <= Date.now() / 1_000) {
        throw new CollaborationError('invalid_lease', {
          closeCode: CloseCodes.leaseInvalid,
          httpStatus: 401,
        })
      }
      return {
        access: introspected.permission,
        documentId: introspected.documentId,
        expiresAt: introspected.expiresAt,
        generation: introspected.generation,
        leaseId: introspected.leaseId,
        policyCursor: introspected.policyCursor,
        protocolVersion: introspected.protocolVersion,
        schemaHash: introspected.schemaHash,
        schemaVersion: introspected.schemaVersion,
        sessionId: introspected.sessionId,
        tenantId: this.settings.tenantId,
        user: introspected.user,
      }
    } catch (error) {
      throw collaborationError(error)
    }
  }

  async renew(
    current: ConnectionContext,
    room: string,
    token: string,
  ): Promise<ConnectionContext> {
    const renewed = await this.authenticate(room, token)
    if (
      renewed.documentId !== current.documentId
      || renewed.generation !== current.generation
      || renewed.sessionId !== current.sessionId
      || renewed.tenantId !== current.tenantId
      || renewed.user.id !== current.user.id
    ) {
      throw new CollaborationError('access_revoked', {
        closeCode: CloseCodes.accessRevoked,
        httpStatus: 403,
      })
    }
    return renewed
  }
}
