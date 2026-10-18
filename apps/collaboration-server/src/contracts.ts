import type {
  CollaborationAccess,
  CollaborationActorKind,
  CollaborationChangeKind,
  SuggestionDescriptor,
  SuggestionKind,
} from '@inqtrix/editor-schema'
import type { BoundedChangeSummary } from './changeSummary'

export type CollaborationSettings = {
  apiBaseUrl: string
  apiTimeoutMs: number
  awarenessRateLimit: number
  awarenessRateWindowMs: number
  bindAddress: string
  documentLimitBytes: number
  frameLimitBytes: number
  instanceId: string
  instanceLeaseSeconds: number
  instanceRenewSeconds: number
  maintenanceIntervalMs: number
  maxQueuedBytes: number
  maxQueuedFrames: number
  maxSessionsPerUserDocument: number
  port: number
  policyPollMs: number
  policyRevalidationTimeoutMs: number
  protocolVersion: number
  schemaVersion: number
  secret: string
  snapshotIdleMs: number
  snapshotMaxUpdates: number
  snapshotRetryBaseMs: number
  snapshotRetryMaxMs: number
  snapshotTailBytes: number
  socketBackpressureBytes: number
  tenantId: string
  updateRateLimit: number
  updateRateWindowMs: number
  websocketPath: '/collaboration'
}

export type InstanceFence = {
  epoch: number
  instanceId: string
  leaseExpiresAt: number
}

export type VerifiedUser = {
  color: string
  id: string
  kind?: 'guest' | 'user'
  linkLabel?: string
  name: string
}

export type IntrospectedLease = {
  documentId: string
  expiresAt: number
  generation: number
  leaseId: string
  permission: CollaborationAccess
  policyCursor: number
  protocolVersion: number
  schemaHash: string
  schemaVersion: number
  sessionId: string
  tenantId: string
  user: VerifiedUser
}

export type LoadedDocumentUpdate = {
  hash: string
  sequence: number
  update: Uint8Array
}

export type LoadedDocumentSnapshot = {
  coveredSequence: number
  stateHash: string
  stateUpdate: Uint8Array
  stateVector: Uint8Array
}

export type LoadedDocumentState = {
  documentId: string
  generation: number
  persistedSequence: number
  schemaHash: string
  schemaVersion: number
  snapshot: LoadedDocumentSnapshot | null
  snapshotCandidates?: LoadedDocumentCandidate[]
  updates: LoadedDocumentUpdate[]
}

export type LoadedDocumentCandidate = {
  snapshot: LoadedDocumentSnapshot
  updates: LoadedDocumentUpdate[]
}

export type DurableUpdateLookup = {
  hash: string
  sequence: number
}

export type PersistUpdateInput = {
  actorKind: CollaborationActorKind
  actorUserId: string
  changeKind: CollaborationChangeKind
  changeSummary: BoundedChangeSummary
  commandId?: string
  commandPayloadHash: string | null
  decision: 'accept' | 'reject' | null
  decisionOutcome: 'accepted' | 'rejected' | null
  documentId: string
  expectedSequence?: number
  fence: InstanceFence
  generation: number
  hash: string
  leaseId: string | null
  patches: SuggestionPatchState[]
  /** Suggestions an edit-lease update removed, each by the update's actor:
   * the store records the removal in the update's transaction. Empty for
   * every other update. */
  rejectedSuggestionIds: string[]
  suggestions: SuggestionDescriptor[]
  suggestionIds: string[]
  update: Uint8Array
}

export type PersistedCommand = {
  actorKind: CollaborationActorKind
  actorUserId: string
  changeKind: Extract<CollaborationChangeKind, 'decision' | 'suggestion'>
  commandId: string
  commandPayloadHash: string
  decision: 'accept' | 'reject' | null
  generation: number
  patchIds: string[]
  sequence: number
  suggestionIds: string[]
  updateHash: string
}

export type SuggestionPatchState = {
  activeSuggestionIds: string[]
  authorId: string
  createdAt: number
  kinds: SuggestionKind[]
  patchId: string
  supersededSuggestionIds: string[]
}

export type PersistUpdateResult = {
  duplicate: boolean
  persistedSequence: number
  sequence: number
}

export type StoreSnapshotInput = {
  coveredSequence: number
  documentId: string
  fence: InstanceFence
  generation: number
  projectionHash: string
  projectionMarkdown: string
  schemaHash: string
  schemaVersion: number
  stateHash: string
  stateUpdate: Uint8Array
  stateVector: Uint8Array
}

export type CompactMaintenanceResult = {
  metadataPruned: number
  payloadsPruned: number
  tombstonesPurged: number
}

export type CollaborationPolicyEvent = {
  id: number
  resourceId: string | null
  resourceType: 'editor_document' | 'user'
  scope: string
  targetUserId: string
}

export type CollaborationPolicyPage = {
  cursor: number
  events: CollaborationPolicyEvent[]
  resetRequired: boolean
}

export interface CollaborationApi {
  acquireInstance(input: {
    instanceId: string
    leaseSeconds: number
    protocolVersion: number
    schemaVersion: number
  }): Promise<InstanceFence>

  introspectLease(input: {
    fence: InstanceFence
    room: string
    token: string
  }): Promise<IntrospectedLease>

  loadDocumentState(input: {
    documentId: string
    generation: number
    fence: InstanceFence
  }): Promise<LoadedDocumentState>

  lookupUpdates(input: {
    documentId: string
    fence: InstanceFence
    generation: number
    hashes: string[]
  }): Promise<DurableUpdateLookup[]>

  lookupCommand(input: {
    commandId: string
    commandPayloadHash: string
    documentId: string
    fence: InstanceFence
    generation: number
  }): Promise<PersistedCommand | null>

  persistUpdate(input: PersistUpdateInput): Promise<PersistUpdateResult>

  pollPolicyEvents(input: {
    afterId: number
    fence: InstanceFence
    limit: number
  }): Promise<CollaborationPolicyPage>

  renewInstance(input: {
    fence: InstanceFence
    leaseSeconds: number
  }): Promise<InstanceFence>

  storeSnapshot(input: StoreSnapshotInput): Promise<void>

  compactMaintenance(input: {
    documentId?: string
    fence: InstanceFence
    generation?: number
  }): Promise<CompactMaintenanceResult>
}

export type ConnectionContext = {
  access: CollaborationAccess
  documentId: string
  expiresAt: number
  generation: number
  leaseId: string
  policyCursor: number
  protocolVersion: number
  schemaHash: string
  schemaVersion: number
  sessionId: string
  tenantId: string
  user: VerifiedUser
}

export type LogFieldValue = boolean | null | number | string | undefined
export type LogFields = Record<string, LogFieldValue>

export interface SidecarLogger {
  debug(event: string, fields?: LogFields): void
  error(event: string, fields?: LogFields): void
  info(event: string, fields?: LogFields): void
  warn(event: string, fields?: LogFields): void
}

export type TimerHandle = ReturnType<typeof setInterval>

export interface IntervalScheduler {
  clear(handle: TimerHandle): void
  every(callback: () => void, intervalMs: number): TimerHandle
}
