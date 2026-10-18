import { Buffer } from 'node:buffer'

import {
  EDITOR_SCHEMA_VERSION,
  editorCollaborationRoom,
  editorJsonToYDoc,
  editorYDocToJson,
  getEditorSchemaFingerprint,
  parseEditorMarkdown,
  parseEditorCollaborationRoom,
  serializeEditorJson,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import type {
  CollaborationApi,
  CollaborationSettings,
  SidecarLogger,
} from './contracts'
import { resolvePatchDecision } from './decisionResolver'
import { DocumentCoordinator } from './documentCoordinator'
import {
  hashString,
  validateDocument,
} from './documentState'
import { collaborationError, CloseCodes, CollaborationError } from './errors'
import { InstanceLeaseManager } from './instanceLease'
import { SidecarMetrics } from './metrics'
import {
  parsePublicationBase,
  parseRoundTrippableTarget,
  publishTargetSuggestion,
  type PublishSuggestionResult,
} from './suggestionPublisher'

type JsonRecord = Record<string, unknown>

type LoadedOperationDocument = {
  document: Y.Doc
  release: () => Promise<void>
  room: string
}

export type OperationDocumentAccess = (
  room: string,
) => Promise<{ document: Y.Doc; release: () => Promise<void> }>

export type StoredSnapshot = {
  coveredSequence: number
  currentSequenceCovered: boolean
}

export class CollaborationOperations {
  constructor(
    private readonly api: CollaborationApi,
    private readonly coordinator: DocumentCoordinator,
    private readonly leaseManager: InstanceLeaseManager,
    private readonly settings: CollaborationSettings,
    private readonly acquireDocument: OperationDocumentAccess,
    private readonly logger: SidecarLogger,
    private readonly metrics: SidecarMetrics,
  ) {}

  async convert(value: unknown): Promise<JsonRecord> {
    const payload = record(value)
    documentId(payload, 'document_id')
    const maximumBytes = positiveInteger(payload, 'max_document_bytes')
    if (maximumBytes > this.settings.documentLimitBytes) throw invalidRequest()
    const markdown = string(payload, 'markdown', maximumBytes)
    const schemaVersion = positiveInteger(payload, 'schema_version')
    if (schemaVersion !== EDITOR_SCHEMA_VERSION) throw incompatible()
    if (Buffer.byteLength(markdown, 'utf8') > maximumBytes) {
      throw tooLarge()
    }

    let document: Y.Doc
    try {
      document = editorJsonToYDoc(parseEditorMarkdown(markdown))
    } catch {
      throw new CollaborationError('invalid_schema', {
        closeCode: CloseCodes.incompatible,
        httpStatus: 409,
      })
    }
    try {
      const validated = validateDocument(document, maximumBytes)
      const projection = serializeEditorJson(editorYDocToJson(document), 'final')
      return {
        projection_hash: hashString(projection),
        projection_markdown: projection,
        schema_hash: await getEditorSchemaFingerprint(),
        schema_version: this.settings.schemaVersion,
        snapshot: {
          covered_sequence: 0,
          state_hash: validated.stateHash,
          state_update_base64: Buffer.from(validated.encodedState).toString('base64'),
          state_vector_base64: Buffer.from(Y.encodeStateVector(document)).toString('base64'),
        },
      }
    } finally {
      document.destroy()
    }
  }

  async project(documentId: string, value: unknown): Promise<JsonRecord> {
    const payload = record(value)
    const generation = positiveInteger(payload, 'generation')
    const minimumSequence = nonNegativeInteger(payload, 'minimum_sequence')
    const includeSnapshot = optionalBoolean(payload, 'include_snapshot', false)

    const loaded = await this.load(documentId, generation)
    let captured: Y.Doc | null = null
    try {
      const state = await this.coordinator.captureDocument(loaded.room, loaded.document)
      captured = state.document
      const sequence = state.sequence
      if (sequence < minimumSequence) {
        throw new CollaborationError('sequence_conflict', {
          closeCode: CloseCodes.incompatible,
          httpStatus: 409,
        })
      }
      const markdown = serializeEditorJson(editorYDocToJson(captured), 'final')
      const result: JsonRecord = {
        generation,
        sequence,
        projection_hash: hashString(markdown),
        projection_markdown: markdown,
        schema_hash: await getEditorSchemaFingerprint(),
        schema_version: this.settings.schemaVersion,
      }
      if (includeSnapshot) {
        const validated = validateDocument(captured, this.settings.documentLimitBytes)
        result.snapshot = {
          covered_sequence: sequence,
          state_hash: validated.stateHash,
          state_update_base64: Buffer.from(validated.encodedState).toString('base64'),
          state_vector_base64: Buffer.from(Y.encodeStateVector(captured)).toString('base64'),
        }
      }
      return result
    } finally {
      captured?.destroy()
      await loaded.release()
    }
  }

  async decide(documentId: string, value: unknown): Promise<JsonRecord> {
    const payload = record(value)
    const actorUserId = uuid(payload, 'actor_user_id')
    const commandId = uuid(payload, 'command_id')
    const decision = payload.decision
    if (decision !== 'accept' && decision !== 'reject') throw invalidRequest()
    const expectedSequence = nonNegativeInteger(payload, 'expected_sequence')
    const generation = positiveInteger(payload, 'generation')
    const patchIds = uuidArray(payload, 'patch_ids').sort()
    const commandPayloadHash = commandFingerprint({
      actor_user_id: actorUserId,
      change_kind: 'decision',
      command_id: commandId,
      decision,
      document_id: documentId,
      expected_sequence: expectedSequence,
      generation,
      patch_ids: patchIds,
    })

    const loaded = await this.load(documentId, generation)
    try {
      const result = await this.coordinator.applyServerMutation({
        actorKind: 'human',
        actorUserId,
        changeKind: 'decision',
        commandId,
        commandPayloadHash,
        decision,
        document: loaded.document,
        documentId,
        expectedSequence,
        generation,
        mutate: (clone) => resolvePatchDecision(clone, { decision, patchIds }),
        requestedPatchIds: patchIds,
        room: loaded.room,
        // A decision names its suggestions by id and applies to the room as
        // it is now; what others typed since the base stays theirs.
        sequenceRule: 'not_ahead',
      })
      return {
        command_id: commandId,
        decision,
        patch_ids: result.patchIds,
        sequence: result.ack.sequence,
        suggestion_ids: result.suggestionIds,
      }
    } finally {
      await loaded.release()
    }
  }

  async publishSuggestion(documentId: string, value: unknown): Promise<JsonRecord> {
    const payload = record(value)
    const actorKind = payload.actor_kind
    if (actorKind !== 'assistant' && actorKind !== 'agent') throw invalidRequest()
    const actorUserId = uuid(payload, 'actor_user_id')
    const commandId = uuid(payload, 'command_id')
    const expectedSequence = nonNegativeInteger(payload, 'expected_sequence')
    const generation = positiveInteger(payload, 'generation')
    const patchId = uuid(payload, 'patch_id')
    const targetMarkdown = string(
      payload,
      'target_markdown',
      this.settings.documentLimitBytes,
    )
    if (Buffer.byteLength(targetMarkdown, 'utf8') > this.settings.documentLimitBytes) {
      throw tooLarge()
    }
    const baseMarkdown = optionalString(
      payload,
      'base_markdown',
      this.settings.documentLimitBytes,
    )
    if (
      baseMarkdown !== null
      && Buffer.byteLength(baseMarkdown, 'utf8') > this.settings.documentLimitBytes
    ) {
      throw tooLarge()
    }
    // Both are read before the room's queue: parsing a large document takes
    // long, and every writer of the room waits behind the queue.
    const target = parseRoundTrippableTarget(targetMarkdown)
    const base = baseMarkdown === null ? null : parsePublicationBase(baseMarkdown)
    const commandPayloadHash = commandFingerprint({
      actor_kind: actorKind,
      actor_user_id: actorUserId,
      // Only present with a base, so a publication without one keeps the
      // fingerprint it always had.
      ...(baseMarkdown === null ? {} : { base_markdown_hash: hashString(baseMarkdown) }),
      change_kind: 'suggestion',
      command_id: commandId,
      document_id: documentId,
      expected_sequence: expectedSequence,
      generation,
      patch_id: patchId,
      target_markdown_hash: hashString(targetMarkdown),
    })

    const loaded = await this.load(documentId, generation)
    const published: { result: PublishSuggestionResult | null } = { result: null }
    try {
      const result = await this.coordinator.applyServerMutation({
        actorKind,
        actorUserId,
        changeKind: 'suggestion',
        commandId,
        commandPayloadHash,
        decision: null,
        document: loaded.document,
        documentId,
        expectedSequence,
        generation,
        mutate: (clone) => {
          published.result = publishTargetSuggestion(clone, {
            actorUserId,
            base,
            patchId,
            target,
          })
          return published.result
        },
        requestedPatchIds: [patchId],
        room: loaded.room,
        // With its base, the publication is placed onto the room as it is
        // now. Without one, the target can only be compared with the room it
        // was built on.
        sequenceRule: base === null ? 'exact' : 'not_ahead',
      })
      const placement = published.result?.placement ?? null
      if (placement !== null) {
        this.metrics.increment('inqtrix_collaboration_publication_rebase_total', {
          outcome: placement,
        })
      }
      return {
        command_id: commandId,
        patch_id: patchId,
        sequence: result.ack.sequence,
        suggestion_ids: result.suggestionIds,
      }
    } catch (error) {
      if (error instanceof CollaborationError && error.reason === 'base_changed') {
        this.metrics.increment('inqtrix_collaboration_publication_rebase_total', {
          outcome: 'conflict',
          reason: error.suggestionReason ?? 'unknown',
        })
      }
      throw error
    } finally {
      await loaded.release()
    }
  }

  /** Stores one snapshot of the room as it is now. Resolves with the
   * sequence the snapshot covers and whether that is still the room's
   * current sequence. */
  async storeSnapshot(room: string, document: Y.Doc): Promise<StoredSnapshot> {
    const parsed = parseEditorCollaborationRoom(room)
    if (!parsed) throw invalidRequest()
    const startedAt = performance.now()
    const captured = await this.coordinator.captureDocument(room, document)
    try {
      const validated = validateDocument(captured.document, this.settings.documentLimitBytes)
      const projection = serializeEditorJson(editorYDocToJson(captured.document), 'final')
      if (Buffer.byteLength(projection, 'utf8') > this.settings.documentLimitBytes) {
        throw tooLarge()
      }
      const fence = this.leaseManager.assertActive()
      await this.api.storeSnapshot({
        coveredSequence: captured.sequence,
        documentId: parsed.documentId,
        fence,
        generation: parsed.generation,
        projectionHash: hashString(projection),
        projectionMarkdown: projection,
        schemaHash: await getEditorSchemaFingerprint(),
        schemaVersion: this.settings.schemaVersion,
        stateHash: validated.stateHash,
        stateUpdate: validated.encodedState,
        stateVector: Y.encodeStateVector(captured.document),
      })
      this.metrics.observeMilliseconds(
        'inqtrix_collaboration_snapshot_seconds',
        performance.now() - startedAt,
      )
      const currentSequenceCovered = this.coordinator.markSnapshot(room, captured)
      await this.compactMaintenance({
        documentId: parsed.documentId,
        generation: parsed.generation,
      })
      return { coveredSequence: captured.sequence, currentSequenceCovered }
    } finally {
      captured.document.destroy()
    }
  }

  async runMaintenance(): Promise<void> {
    await this.compactMaintenance()
  }

  private async compactMaintenance(
    scope?: { documentId: string; generation: number },
  ): Promise<void> {
    try {
      const fence = this.leaseManager.assertActive()
      const result = await this.api.compactMaintenance({
        fence,
        ...scope,
      })
      this.metrics.increment('inqtrix_collaboration_compaction_runs_total', {
        status: 'success',
      })
      this.metrics.add(
        'inqtrix_collaboration_compaction_pruned_total',
        result.payloadsPruned,
        { kind: 'payload' },
      )
      this.metrics.add(
        'inqtrix_collaboration_compaction_pruned_total',
        result.metadataPruned,
        { kind: 'metadata' },
      )
      this.metrics.add(
        'inqtrix_collaboration_compaction_pruned_total',
        result.tombstonesPurged,
        { kind: 'tombstone' },
      )
    } catch (error) {
      const mapped = collaborationError(error)
      this.metrics.increment('inqtrix_collaboration_compaction_runs_total', {
        status: 'failure',
      })
      this.logger.warn('collaboration_compaction_failed', {
        generation: scope?.generation,
        reason: mapped.reason,
        scope: scope ? 'document' : 'global',
      })
    }
  }

  private async load(documentId: string, generation: number): Promise<LoadedOperationDocument> {
    const room = editorCollaborationRoom(documentId, generation)
    const acquired = await this.acquireDocument(room)
    return { ...acquired, room }
  }
}

function record(value: unknown): JsonRecord {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw invalidRequest()
  return value as JsonRecord
}

function string(payload: JsonRecord, key: string, maximumLength: number): string {
  const value = payload[key]
  if (typeof value !== 'string' || value.length > maximumLength) throw invalidRequest()
  return value
}

function optionalString(payload: JsonRecord, key: string, maximumLength: number): string | null {
  if (payload[key] === undefined) return null
  return string(payload, key, maximumLength)
}

function positiveInteger(payload: JsonRecord, key: string): number {
  const value = payload[key]
  if (!Number.isSafeInteger(value) || (value as number) < 1) throw invalidRequest()
  return value as number
}

function nonNegativeInteger(payload: JsonRecord, key: string): number {
  const value = payload[key]
  if (!Number.isSafeInteger(value) || (value as number) < 0) throw invalidRequest()
  return value as number
}

function optionalBoolean(payload: JsonRecord, key: string, defaultValue: boolean): boolean {
  const value = payload[key]
  if (value === undefined) return defaultValue
  if (typeof value !== 'boolean') throw invalidRequest()
  return value
}

function uuid(payload: JsonRecord, key: string): string {
  const value = payload[key]
  if (typeof value !== 'string' || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(value)) {
    throw invalidRequest()
  }
  return value
}

function uuidArray(payload: JsonRecord, key: string): string[] {
  const value = payload[key]
  if (!Array.isArray(value) || value.length === 0 || value.length > 1_000) throw invalidRequest()
  const result = value.map((item) => uuid({ item }, 'item'))
  if (new Set(result).size !== result.length) throw invalidRequest()
  return result
}

function documentId(payload: JsonRecord, key: string): string {
  const value = payload[key]
  if (
    typeof value !== 'string'
    || !/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(value)
  ) {
    throw invalidRequest()
  }
  return value
}

function commandFingerprint(payload: JsonRecord): string {
  return hashString(JSON.stringify(payload))
}

function invalidRequest(): CollaborationError {
  return new CollaborationError('invalid_request', {
    closeCode: CloseCodes.incompatible,
    httpStatus: 400,
  })
}

function incompatible(): CollaborationError {
  return new CollaborationError('update_required', {
    closeCode: CloseCodes.incompatible,
    httpStatus: 409,
  })
}

function tooLarge(): CollaborationError {
  return new CollaborationError('document_too_large', {
    closeCode: CloseCodes.messageTooLarge,
    httpStatus: 413,
  })
}
