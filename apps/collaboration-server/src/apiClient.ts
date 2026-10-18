import { Buffer } from 'node:buffer'

import type {
  CollaborationAccess,
  CollaborationActorKind,
  CollaborationChangeKind,
} from '@inqtrix/editor-schema'

import type {
  CollaborationApi,
  CompactMaintenanceResult,
  CollaborationPolicyEvent,
  CollaborationPolicyPage,
  CollaborationSettings,
  DurableUpdateLookup,
  InstanceFence,
  IntrospectedLease,
  LoadedDocumentState,
  PersistUpdateInput,
  PersistUpdateResult,
  PersistedCommand,
  SidecarLogger,
  StoreSnapshotInput,
  VerifiedUser,
} from './contracts'
import { ApiRequestError } from './errors'
import { SidecarMetrics } from './metrics'

type JsonRecord = Record<string, unknown>

export class FastApiCollaborationClient implements CollaborationApi {
  constructor(
    private readonly settings: CollaborationSettings,
    private readonly logger: SidecarLogger,
    private readonly metrics: SidecarMetrics,
    private readonly fetchImplementation: typeof fetch = fetch,
  ) {}

  async acquireInstance(input: {
    instanceId: string
    leaseSeconds: number
    protocolVersion: number
    schemaVersion: number
  }): Promise<InstanceFence> {
    const payload = await this.requestJson(
      'instance_acquire',
      '/internal/collaboration/instances/acquire',
      {
        instance_id: input.instanceId,
        lease_seconds: input.leaseSeconds,
        protocol_version: input.protocolVersion,
        schema_version: input.schemaVersion,
      },
    )
    return parseInstanceFence(payload)
  }

  async renewInstance(input: {
    fence: InstanceFence
    leaseSeconds: number
  }): Promise<InstanceFence> {
    const payload = await this.requestJson(
      'instance_renew',
      '/internal/collaboration/instances/renew',
      {
        epoch: input.fence.epoch,
        instance_id: input.fence.instanceId,
        lease_seconds: input.leaseSeconds,
      },
    )
    return parseInstanceFence(payload)
  }

  async introspectLease(input: {
    fence: InstanceFence
    room: string
    token: string
  }): Promise<IntrospectedLease> {
    const payload = record(await this.requestJson(
      'lease_introspect',
      '/internal/collaboration/leases/introspect',
      {
        epoch: input.fence.epoch,
        instance_id: input.fence.instanceId,
        lease_token: input.token,
        room: input.room,
      },
    ))
    if (payload.valid !== true) throw new ApiRequestError(401, 'invalid_lease')
    return {
      documentId: requiredString(payload, 'document_id'),
      expiresAt: requiredNumber(payload, 'expires_at'),
      generation: positiveInteger(payload, 'generation'),
      leaseId: requiredString(payload, 'lease_id'),
      permission: collaborationAccess(payload.permission),
      policyCursor: payload.policy_cursor === undefined
        ? 0
        : nonNegativeInteger(payload, 'policy_cursor'),
      protocolVersion: positiveInteger(payload, 'protocol_version'),
      schemaHash: sha256String(payload, 'schema_hash'),
      schemaVersion: positiveInteger(payload, 'schema_version'),
      sessionId: requiredString(payload, 'session_id'),
      tenantId: requiredString(payload, 'tenant_id'),
      user: verifiedUser(payload.user),
    }
  }

  async loadDocumentState(input: {
    documentId: string
    generation: number
    fence: InstanceFence
  }): Promise<LoadedDocumentState> {
    const query = new URLSearchParams({
      epoch: String(input.fence.epoch),
      generation: String(input.generation),
      instance_id: input.fence.instanceId,
    })
    const payload = record(await this.requestJson(
      'document_load',
      `/internal/collaboration/documents/${encodeURIComponent(input.documentId)}/state?${query}`,
      undefined,
      'GET',
    ))
    const updates = requiredArray(payload, 'updates').map((value, index) => {
      return parseLoadedUpdate(
        value,
        `updates[${index}]`,
        this.settings.frameLimitBytes,
      )
    })
    const snapshotValue = payload.snapshot
    const snapshot = snapshotValue === null
      ? null
      : parseLoadedSnapshot(
          snapshotValue,
          'snapshot',
          this.settings.documentLimitBytes,
        )
    const snapshotCandidates = parseSnapshotCandidates(payload, {
      documentLimitBytes: this.settings.documentLimitBytes,
      frameLimitBytes: this.settings.frameLimitBytes,
    })
    return {
      documentId: requiredString(payload, 'document_id'),
      generation: positiveInteger(payload, 'generation'),
      persistedSequence: nonNegativeInteger(payload, 'persisted_sequence'),
      schemaHash: sha256String(payload, 'schema_hash'),
      schemaVersion: positiveInteger(payload, 'schema_version'),
      snapshot,
      ...(snapshotCandidates ? { snapshotCandidates } : {}),
      updates,
    }
  }

  async lookupUpdates(input: {
    documentId: string
    fence: InstanceFence
    generation: number
    hashes: string[]
  }): Promise<DurableUpdateLookup[]> {
    if (
      input.hashes.length < 1
      || new Set(input.hashes).size !== input.hashes.length
      || input.hashes.some((hash) => !/^[a-f0-9]{64}$/.test(hash))
    ) {
      throw new ApiRequestError(400, 'invalid_request')
    }
    const payload = record(await this.requestJson(
      'updates_lookup',
      `/internal/collaboration/documents/${encodeURIComponent(input.documentId)}/updates:lookup`,
      {
        epoch: input.fence.epoch,
        generation: input.generation,
        hashes: input.hashes,
        instance_id: input.fence.instanceId,
      },
    ))
    const requested = new Set(input.hashes)
    const seen = new Set<string>()
    const updates = requiredArray(payload, 'updates')
    if (updates.length > input.hashes.length) {
      throw new ApiRequestError(503, 'invalid_internal_response')
    }
    return updates.map((value) => {
      const item = record(value)
      const hash = sha256String(item, 'hash')
      if (!requested.has(hash) || seen.has(hash)) {
        throw new ApiRequestError(503, 'invalid_internal_response')
      }
      seen.add(hash)
      return { hash, sequence: positiveInteger(item, 'sequence') }
    })
  }

  async persistUpdate(input: PersistUpdateInput): Promise<PersistUpdateResult> {
    const payload = record(await this.requestJson(
      'update_persist',
      `/internal/collaboration/documents/${encodeURIComponent(input.documentId)}/updates`,
      {
        actor_kind: actorKind(input.actorKind),
        actor_user_id: input.actorUserId,
        change_kind: changeKind(input.changeKind),
        change_summary: {
          changes_document: input.changeSummary.changesDocument,
          edits: input.changeSummary.edits,
          omitted_edit_count: input.changeSummary.omittedEditCount,
        },
        command_id: input.commandId ?? null,
        command_payload_hash: input.commandPayloadHash,
        decision: input.decision,
        decision_outcome: input.decisionOutcome,
        epoch: input.fence.epoch,
        expected_sequence: input.expectedSequence ?? null,
        generation: input.generation,
        instance_id: input.fence.instanceId,
        lease_id: input.leaseId,
        patches: input.patches.map((patch) => ({
          active_suggestion_ids: patch.activeSuggestionIds,
          author_id: patch.authorId,
          created_at: patch.createdAt,
          kinds: patch.kinds,
          patch_id: patch.patchId,
          superseded_suggestion_ids: patch.supersededSuggestionIds,
        })),
        rejected_suggestion_ids: input.rejectedSuggestionIds,
        suggestions: input.suggestions.map((suggestion) => ({
          author_id: suggestion.authorId,
          created_at: suggestion.createdAt,
          kind: suggestion.kind,
          patch_id: suggestion.patchId,
          suggestion_id: suggestion.suggestionId,
        })),
        suggestion_ids: input.suggestionIds,
        update_base64: bytesBase64(input.update),
        update_hash: input.hash,
      },
    ))
    return {
      duplicate: requiredBoolean(payload, 'duplicate'),
      persistedSequence: positiveInteger(payload, 'persisted_sequence'),
      sequence: positiveInteger(payload, 'sequence'),
    }
  }

  async lookupCommand(input: {
    commandId: string
    commandPayloadHash: string
    documentId: string
    fence: InstanceFence
    generation: number
  }): Promise<PersistedCommand | null> {
    const payload = record(await this.requestJson(
      'command_lookup',
      `/internal/collaboration/documents/${encodeURIComponent(input.documentId)}/commands:lookup`,
      {
        command_id: input.commandId,
        command_payload_hash: input.commandPayloadHash,
        epoch: input.fence.epoch,
        generation: input.generation,
        instance_id: input.fence.instanceId,
      },
    ))
    const found = requiredBoolean(payload, 'found')
    if (!found) return null
    const decision = payload.decision
    if (decision !== null && decision !== 'accept' && decision !== 'reject') {
      throw new ApiRequestError(503, 'invalid_internal_response')
    }
    const stored: PersistedCommand = {
      actorKind: actorKindValue(payload.actor_kind),
      actorUserId: uuidString(payload, 'actor_user_id'),
      changeKind: serverChangeKind(payload.change_kind),
      commandId: uuidString(payload, 'command_id'),
      commandPayloadHash: sha256String(payload, 'command_payload_hash'),
      decision,
      generation: positiveInteger(payload, 'generation'),
      patchIds: uuidArray(payload, 'patch_ids'),
      sequence: positiveInteger(payload, 'sequence'),
      suggestionIds: uuidArray(payload, 'suggestion_ids'),
      updateHash: sha256String(payload, 'update_hash'),
    }
    if (
      stored.commandId !== input.commandId
      || stored.commandPayloadHash !== input.commandPayloadHash
      || stored.generation !== input.generation
    ) {
      throw new ApiRequestError(409, 'command_conflict')
    }
    return stored
  }

  async pollPolicyEvents(input: {
    afterId: number
    fence: InstanceFence
    limit: number
  }): Promise<CollaborationPolicyPage> {
    if (
      !Number.isSafeInteger(input.afterId)
      || input.afterId < 0
      || !Number.isSafeInteger(input.limit)
      || input.limit < 1
      || input.limit > 500
    ) {
      throw new ApiRequestError(503, 'invalid_policy_cursor')
    }
    const query = new URLSearchParams({
      after_id: String(input.afterId),
      limit: String(input.limit),
    })
    const payload = record(await this.requestJson(
      'policy_events',
      `/internal/collaboration/policy-events?${query}`,
      undefined,
      'GET',
    ))
    const cursor = nonNegativeInteger(payload, 'cursor')
    const resetRequired = requiredBoolean(payload, 'reset_required')
    if (!resetRequired && cursor < input.afterId) {
      throw new ApiRequestError(503, 'invalid_internal_response')
    }
    let previousId = input.afterId
    const events = requiredArray(payload, 'events').map((value) => {
      const item = record(value)
      const id = positiveInteger(item, 'id')
      if (id <= previousId || id > cursor) {
        throw new ApiRequestError(503, 'invalid_internal_response')
      }
      previousId = id
      const resourceType: CollaborationPolicyEvent['resourceType'] = item.resource_type === 'user'
        ? 'user'
        : item.resource_type === 'editor_document'
          ? 'editor_document'
          : (() => {
              throw new ApiRequestError(503, 'invalid_internal_response')
            })()
      const resourceIdValue = item.resource_id
      if (
        resourceIdValue !== null
        && (
          typeof resourceIdValue !== 'string'
          || resourceIdValue.length === 0
          || resourceIdValue.length > 128
        )
      ) {
        throw new ApiRequestError(503, 'invalid_internal_response')
      }
      return {
        id,
        resourceId: resourceIdValue,
        resourceType,
        scope: requiredString(item, 'scope'),
        targetUserId: uuidString(item, 'target_user_id'),
      }
    })
    return { cursor, events, resetRequired }
  }

  async storeSnapshot(input: StoreSnapshotInput): Promise<void> {
    await this.requestVoid(
      'snapshot_store',
      `/internal/collaboration/documents/${encodeURIComponent(input.documentId)}/snapshots`,
      {
        covered_sequence: input.coveredSequence,
        epoch: input.fence.epoch,
        generation: input.generation,
        instance_id: input.fence.instanceId,
        projection_hash: input.projectionHash,
        projection_markdown: input.projectionMarkdown,
        schema_hash: input.schemaHash,
        schema_version: input.schemaVersion,
        state_hash: input.stateHash,
        state_update_base64: bytesBase64(input.stateUpdate),
        state_vector_base64: bytesBase64(input.stateVector),
      },
    )
  }

  async compactMaintenance(input: {
    documentId?: string
    fence: InstanceFence
    generation?: number
  }): Promise<CompactMaintenanceResult> {
    const payload = record(await this.requestJson(
      'maintenance_compact',
      '/internal/collaboration/maintenance:compact',
      {
        ...(input.documentId === undefined ? {} : { document_id: input.documentId }),
        epoch: input.fence.epoch,
        ...(input.generation === undefined ? {} : { generation: input.generation }),
        instance_id: input.fence.instanceId,
      },
    ))
    return {
      metadataPruned: nonNegativeInteger(payload, 'metadata_pruned'),
      payloadsPruned: nonNegativeInteger(payload, 'payloads_pruned'),
      tombstonesPurged: nonNegativeInteger(payload, 'tombstones_purged'),
    }
  }

  private async requestJson(
    operation: string,
    path: string,
    body: JsonRecord | undefined,
    method: 'GET' | 'POST' = 'POST',
  ): Promise<unknown> {
    const response = await this.request(operation, path, body, method)
    try {
      return await response.json()
    } catch {
      this.logger.warn('internal_api_invalid_json', { operation, status: response.status })
      throw new ApiRequestError(503, 'invalid_internal_response')
    }
  }

  private async requestVoid(
    operation: string,
    path: string,
    body: JsonRecord,
  ): Promise<void> {
    await this.request(operation, path, body, 'POST')
  }

  private async request(
    operation: string,
    path: string,
    body: JsonRecord | undefined,
    method: 'GET' | 'POST',
  ): Promise<Response> {
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), this.settings.apiTimeoutMs)
    const startedAt = performance.now()
    try {
      const url = new URL(`${this.settings.apiBaseUrl}${path}`)
      const scopedBody = method === 'POST'
        ? { ...(body ?? {}), tenant_id: this.settings.tenantId }
        : body
      if (method === 'GET') url.searchParams.set('tenant_id', this.settings.tenantId)
      const request: RequestInit = {
        headers: {
          Accept: 'application/json',
          Authorization: `Bearer ${this.settings.secret}`,
          'Content-Type': 'application/json',
        },
        method,
        signal: controller.signal,
      }
      if (scopedBody !== undefined) request.body = JSON.stringify(scopedBody)
      const response = await this.fetchImplementation(url, request)
      this.metrics.observeMilliseconds(
        'inqtrix_collaboration_internal_api_seconds',
        performance.now() - startedAt,
      )
      if (!response.ok) {
        const reason = await responseReason(response)
        this.metrics.increment('inqtrix_collaboration_internal_api_errors_total', {
          operation,
          status: String(response.status),
        })
        throw new ApiRequestError(response.status, reason)
      }
      return response
    } catch (error) {
      if (error instanceof ApiRequestError) throw error
      const reason = controller.signal.aborted ? 'internal_api_timeout' : 'internal_api_unreachable'
      this.logger.warn(reason, { operation })
      this.metrics.increment('inqtrix_collaboration_internal_api_errors_total', {
        operation,
        status: 'transport',
      })
      throw new ApiRequestError(503, reason)
    } finally {
      clearTimeout(timeout)
    }
  }
}

function parseSnapshotCandidates(
  payload: JsonRecord,
  limits: {
    documentLimitBytes: number
    frameLimitBytes: number
  },
): LoadedDocumentState['snapshotCandidates'] {
  const value = payload.snapshot_candidates
  if (value === undefined) return undefined
  if (!Array.isArray(value) || value.length < 1 || value.length > 2) {
    throw new ApiRequestError(503, 'invalid_internal_response')
  }
  const candidates = value.map((candidate, index) => {
    const item = record(candidate)
    return {
      snapshot: parseLoadedSnapshot(
        item,
        `snapshot_candidates[${index}]`,
        limits.documentLimitBytes,
      ),
      updates: requiredArray(item, 'updates').map((update, updateIndex) => (
        parseLoadedUpdate(
          update,
          `snapshot_candidates[${index}].updates[${updateIndex}]`,
          limits.frameLimitBytes,
        )
      )),
    }
  })
  for (let index = 1; index < candidates.length; index += 1) {
    if (candidates[index]!.snapshot.coveredSequence >= candidates[index - 1]!.snapshot.coveredSequence) {
      throw new ApiRequestError(503, 'invalid_internal_response')
    }
  }
  return candidates
}

function parseLoadedSnapshot(
  value: unknown,
  label: string,
  documentLimitBytes: number,
) {
  const item = record(value)
  return {
    coveredSequence: nonNegativeInteger(item, 'covered_sequence'),
    stateHash: sha256String(item, 'state_hash'),
    stateUpdate: base64Bytes(
      item,
      'state_update_base64',
      `${label}.state_update_base64`,
      documentLimitBytes,
    ),
    stateVector: base64Bytes(
      item,
      'state_vector_base64',
      `${label}.state_vector_base64`,
      documentLimitBytes,
    ),
  }
}

function parseLoadedUpdate(
  value: unknown,
  label: string,
  frameLimitBytes: number,
) {
  const item = record(value)
  return {
    hash: sha256String(item, 'update_hash'),
    sequence: positiveInteger(item, 'sequence'),
    update: base64Bytes(
      item,
      'update_base64',
      `${label}.update_base64`,
      frameLimitBytes,
    ),
  }
}

function parseInstanceFence(value: unknown): InstanceFence {
  const payload = record(value)
  return {
    epoch: positiveInteger(payload, 'epoch'),
    instanceId: requiredString(payload, 'instance_id'),
    leaseExpiresAt: requiredNumber(payload, 'lease_expires_at'),
  }
}

async function responseReason(response: Response): Promise<string> {
  try {
    const payload = record(await response.json())
    const nested = payload.error && typeof payload.error === 'object'
      ? payload.error as JsonRecord
      : payload.detail && typeof payload.detail === 'object'
        ? payload.detail as JsonRecord
        : payload
    const value = nested.reason
    if (typeof value === 'string' && /^[a-z0-9_:-]{1,80}$/.test(value)) return value
  } catch {
    return 'internal_api_error'
  }
  return 'internal_api_error'
}

function record(value: unknown): JsonRecord {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new ApiRequestError(503, 'invalid_internal_response')
  }
  return value as JsonRecord
}

function requiredString(payload: JsonRecord, key: string): string {
  const value = payload[key]
  if (typeof value !== 'string' || value.length === 0 || value.length > 512) {
    throw new ApiRequestError(503, 'invalid_internal_response')
  }
  return value
}

function sha256String(payload: JsonRecord, key: string): string {
  const value = requiredString(payload, key)
  if (!/^[a-f0-9]{64}$/.test(value)) {
    throw new ApiRequestError(503, 'invalid_internal_response')
  }
  return value
}

function requiredNumber(payload: JsonRecord, key: string): number {
  const value = payload[key]
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new ApiRequestError(503, 'invalid_internal_response')
  }
  return value
}

function positiveInteger(payload: JsonRecord, key: string): number {
  const value = requiredNumber(payload, key)
  if (!Number.isSafeInteger(value) || value < 1) {
    throw new ApiRequestError(503, 'invalid_internal_response')
  }
  return value
}

function nonNegativeInteger(payload: JsonRecord, key: string): number {
  const value = requiredNumber(payload, key)
  if (!Number.isSafeInteger(value) || value < 0) {
    throw new ApiRequestError(503, 'invalid_internal_response')
  }
  return value
}

function requiredBoolean(payload: JsonRecord, key: string): boolean {
  const value = payload[key]
  if (typeof value !== 'boolean') throw new ApiRequestError(503, 'invalid_internal_response')
  return value
}

function requiredArray(payload: JsonRecord, key: string): unknown[] {
  const value = payload[key]
  if (!Array.isArray(value)) throw new ApiRequestError(503, 'invalid_internal_response')
  return value
}

function verifiedUser(value: unknown): VerifiedUser {
  const payload = record(value)
  const color = requiredString(payload, 'color')
  if (!/^#[0-9A-Fa-f]{6}$/.test(color)) {
    throw new ApiRequestError(503, 'invalid_internal_response')
  }
  return {
    color,
    id: requiredString(payload, 'id'),
    kind: payload.kind === 'guest' ? 'guest' : 'user',
    ...(typeof payload.link_label === 'string'
      ? { linkLabel: payload.link_label }
      : {}),
    name: requiredString(payload, 'name'),
  }
}

function uuidString(payload: JsonRecord, key: string): string {
  const value = requiredString(payload, key)
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
    throw new ApiRequestError(503, 'invalid_internal_response')
  }
  return value
}

function uuidArray(payload: JsonRecord, key: string): string[] {
  const values = requiredArray(payload, key)
  if (values.length === 0 || values.length > 1_000) {
    throw new ApiRequestError(503, 'invalid_internal_response')
  }
  const result = values.map((value) => uuidString({ value }, 'value'))
  if (new Set(result).size !== result.length) {
    throw new ApiRequestError(503, 'invalid_internal_response')
  }
  return result
}

function collaborationAccess(value: unknown): CollaborationAccess {
  if (value !== 'edit' && value !== 'suggest' && value !== 'view') {
    throw new ApiRequestError(503, 'invalid_internal_response')
  }
  return value
}

function actorKind(value: CollaborationActorKind): CollaborationActorKind {
  return value
}

function actorKindValue(value: unknown): CollaborationActorKind {
  if (value !== 'assistant' && value !== 'agent' && value !== 'guest' && value !== 'human' && value !== 'system') {
    throw new ApiRequestError(503, 'invalid_internal_response')
  }
  return value
}

function changeKind(value: CollaborationChangeKind): CollaborationChangeKind {
  return value
}

function serverChangeKind(value: unknown): PersistedCommand['changeKind'] {
  if (value !== 'decision' && value !== 'suggestion') {
    throw new ApiRequestError(503, 'invalid_internal_response')
  }
  return value
}

function base64Bytes(
  payload: JsonRecord,
  key: string,
  label: string,
  maxDecodedBytes: number,
): Uint8Array {
  const value = payload[key]
  const maxEncodedLength = Math.ceil(maxDecodedBytes / 3) * 4
  if (
    typeof value !== 'string'
    || value.length === 0
    || value.length > maxEncodedLength
    || !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value)
  ) {
    throw new ApiRequestError(503, `invalid_${label.replace(/[^a-z0-9]/gi, '_')}`)
  }
  const decoded = new Uint8Array(Buffer.from(value, 'base64'))
  if (decoded.byteLength > maxDecodedBytes) {
    throw new ApiRequestError(503, `invalid_${label.replace(/[^a-z0-9]/gi, '_')}`)
  }
  return decoded
}

function bytesBase64(value: Uint8Array): string {
  return Buffer.from(value).toString('base64')
}
