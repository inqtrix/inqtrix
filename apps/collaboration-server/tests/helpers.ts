import {
  editorJsonToYDoc,
  getEditorSchemaFingerprint,
  parseEditorMarkdown,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import type {
  CollaborationApi,
  CompactMaintenanceResult,
  CollaborationSettings,
  InstanceFence,
  IntrospectedLease,
  LoadedDocumentState,
  CollaborationPolicyPage,
  DurableUpdateLookup,
  PersistUpdateInput,
  PersistUpdateResult,
  PersistedCommand,
  StoreSnapshotInput,
  SidecarLogger,
} from '../src/contracts'
import { hashBytes } from '../src/documentState'

export const USER_ID = '11111111-1111-4111-8111-111111111111'

export function settings(
  overrides: Partial<CollaborationSettings> = {},
): CollaborationSettings {
  return {
    apiBaseUrl: 'http://fastapi.internal',
    apiTimeoutMs: 1_000,
    awarenessRateLimit: 20,
    awarenessRateWindowMs: 1_000,
    bindAddress: '127.0.0.1',
    documentLimitBytes: 10 * 1024 * 1024,
    frameLimitBytes: 2 * 1024 * 1024,
    instanceId: 'test-instance',
    instanceLeaseSeconds: 15,
    instanceRenewSeconds: 5,
    maintenanceIntervalMs: 60_000,
    maxQueuedBytes: 8 * 1024 * 1024,
    maxQueuedFrames: 32,
    maxSessionsPerUserDocument: 5,
    port: 0,
    policyPollMs: 2_000,
    policyRevalidationTimeoutMs: 7_500,
    protocolVersion: 1,
    schemaVersion: 2,
    secret: '0123456789abcdef0123456789abcdef',
    snapshotIdleMs: 5_000,
    snapshotMaxUpdates: 256,
    snapshotRetryBaseMs: 1_000,
    snapshotRetryMaxMs: 30_000,
    snapshotTailBytes: 1024 * 1024,
    socketBackpressureBytes: 4 * 1024 * 1024,
    tenantId: 'tenant-1',
    updateRateLimit: 120,
    updateRateWindowMs: 10_000,
    websocketPath: '/collaboration',
    ...overrides,
  }
}

export async function documentState(
  documentId: string,
  document: Y.Doc,
  generation = 1,
  sequence = 0,
): Promise<LoadedDocumentState> {
  const stateUpdate = Y.encodeStateAsUpdate(document)
  return {
    documentId,
    generation,
    persistedSequence: sequence,
    schemaHash: await getEditorSchemaFingerprint(),
    schemaVersion: 2,
    snapshot: {
      coveredSequence: sequence,
      stateHash: hashBytes(stateUpdate),
      stateUpdate,
      stateVector: Y.encodeStateVector(document),
    },
    updates: [],
  }
}

export function markdownDocument(markdown: string): Y.Doc {
  return editorJsonToYDoc(parseEditorMarkdown(markdown))
}

export class FakeCollaborationApi implements CollaborationApi {
  readonly commands = new Map<string, PersistedCommand>()
  readonly persisted: PersistUpdateInput[] = []
  readonly snapshots: StoreSnapshotInput[] = []
  readonly compactions: Array<{
    documentId?: string
    fence: InstanceFence
    generation?: number
  }> = []
  readonly lookups: Array<{
    documentId: string
    fence: InstanceFence
    generation: number
    hashes: string[]
  }> = []
  readonly loads: Array<{ documentId: string; generation: number }> = []
  lookupResults: DurableUpdateLookup[] = []
  fence: InstanceFence = {
    epoch: 1,
    instanceId: 'test-instance',
    leaseExpiresAt: Date.now() / 1_000 + 60,
  }
  lease: IntrospectedLease | null = null
  /** Leases answered for a specific token; any other token gets `lease`. */
  readonly leasesByToken = new Map<string, IntrospectedLease>()
  loadedState: LoadedDocumentState | null = null
  /** States of further rooms, by `documentId:generation`; any other room
   * loads `loadedState`. */
  readonly loadedStatesByRoom = new Map<string, LoadedDocumentState>()
  persistImplementation: (
    input: PersistUpdateInput,
  ) => Promise<PersistUpdateResult> = async () => {
    const sequence = this.persisted.length
    return { duplicate: false, persistedSequence: sequence, sequence }
  }
  persistResponseErrorAfterCommit: Error | null = null
  renewError: Error | null = null
  snapshotImplementation: (input: StoreSnapshotInput) => Promise<void> = async () => undefined
  policyImplementation: () => Promise<CollaborationPolicyPage> = async () => ({
    cursor: 0,
    events: [],
    resetRequired: false,
  })
  compactImplementation: () => Promise<CompactMaintenanceResult> = async () => ({
    metadataPruned: 0,
    payloadsPruned: 0,
    tombstonesPurged: 0,
  })

  async acquireInstance(): Promise<InstanceFence> {
    return this.fence
  }

  async renewInstance(): Promise<InstanceFence> {
    if (this.renewError) throw this.renewError
    return this.fence
  }

  async introspectLease(input?: { token: string }): Promise<IntrospectedLease> {
    const lease = (input ? this.leasesByToken.get(input.token) : undefined) ?? this.lease
    if (!lease) throw new Error('No introspected lease configured')
    return lease
  }

  async loadDocumentState(input?: {
    documentId: string
    generation: number
  }): Promise<LoadedDocumentState> {
    const state = (
      input ? this.loadedStatesByRoom.get(`${input.documentId}:${input.generation}`) : undefined
    ) ?? this.loadedState
    if (!state) throw new Error('No document state configured')
    if (input) this.loads.push(input)
    return state
  }

  async lookupCommand(input: { commandId: string }): Promise<PersistedCommand | null> {
    return this.commands.get(input.commandId) ?? null
  }

  async lookupUpdates(input: {
    documentId: string
    fence: InstanceFence
    generation: number
    hashes: string[]
  }): Promise<DurableUpdateLookup[]> {
    this.lookups.push(input)
    return this.lookupResults
  }

  async persistUpdate(input: PersistUpdateInput): Promise<PersistUpdateResult> {
    this.persisted.push(input)
    const result = await this.persistImplementation(input)
    if (
      input.commandId
      && input.commandPayloadHash
      && (input.changeKind === 'decision' || input.changeKind === 'suggestion')
    ) {
      this.commands.set(input.commandId, {
        actorKind: input.actorKind,
        actorUserId: input.actorUserId,
        changeKind: input.changeKind,
        commandId: input.commandId,
        commandPayloadHash: input.commandPayloadHash,
        decision: input.decision,
        generation: input.generation,
        patchIds: input.patches.map((patch) => patch.patchId).sort(),
        sequence: result.sequence,
        suggestionIds: [...input.suggestionIds].sort(),
        updateHash: input.hash,
      })
    }
    if (
      this.loadedState
      && !result.duplicate
      && this.loadedState.documentId === input.documentId
      && this.loadedState.generation === input.generation
    ) {
      this.loadedState = {
        ...this.loadedState,
        persistedSequence: result.persistedSequence,
        updates: [
          ...this.loadedState.updates,
          { hash: input.hash, sequence: result.sequence, update: input.update },
        ],
      }
    }
    if (this.persistResponseErrorAfterCommit) {
      const error = this.persistResponseErrorAfterCommit
      this.persistResponseErrorAfterCommit = null
      throw error
    }
    return result
  }

  async pollPolicyEvents(): Promise<CollaborationPolicyPage> {
    return this.policyImplementation()
  }

  async storeSnapshot(input: StoreSnapshotInput): Promise<void> {
    this.snapshots.push(input)
    await this.snapshotImplementation(input)
  }

  async compactMaintenance(input: {
    documentId?: string
    fence: InstanceFence
    generation?: number
  }): Promise<CompactMaintenanceResult> {
    this.compactions.push(input)
    return this.compactImplementation()
  }
}

export const silentLogger: SidecarLogger = {
  debug: () => undefined,
  error: () => undefined,
  info: () => undefined,
  warn: () => undefined,
}

export function deferred<T>(): {
  promise: Promise<T>
  reject: (error: unknown) => void
  resolve: (value: T) => void
} {
  let reject!: (error: unknown) => void
  let resolve!: (value: T) => void
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve
    reject = promiseReject
  })
  return { promise, reject, resolve }
}
