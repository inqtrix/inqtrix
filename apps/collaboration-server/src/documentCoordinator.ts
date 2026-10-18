import {
  getEditorSchemaFingerprint,
  isFormattingCleanupTransaction,
  validateCanonicalYjsV1Update,
  validateSuggestionYjsUpdate,
  type CollaborationActorKind,
  type CollaborationChangeKind,
  type CollaborationDurableAck,
  type SuggestionDescriptor,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import type {
  CollaborationApi,
  CollaborationSettings,
  ConnectionContext,
  DurableUpdateLookup,
  PersistUpdateInput,
  PersistUpdateResult,
  SidecarLogger,
} from './contracts'
import { summarizeEditorChange } from './changeSummary'
import {
  cloneDocument,
  hashBytes,
  reconstructValidatedDocument,
  sameBytes,
  type ValidatedDocument,
  validateDocument,
} from './documentState'
import {
  ApiRequestError,
  collaborationError,
  CloseCodes,
  CollaborationError,
  isDeterministicRejection,
} from './errors'
import { InstanceLeaseManager } from './instanceLease'
import {
  normalizeEditMerge,
  type EditMergeNormalization,
  type KnownSuggestion,
  type SuggestionOutcome,
} from './mergeNormalization'
import { SidecarMetrics } from './metrics'
import {
  collectSuggestionRecords,
  deriveSuggestionPatchStates,
  validateSuggestionUpdate,
  type SuggestionPolicyResult,
} from './suggestPolicy'

type Release = () => void

type RoomPhase = 'loading' | 'ready' | 'awaiting_apply' | 'reconstruction_required'

type ValidatedDocumentState = Pick<
  ValidatedDocument,
  'canonicalJson' | 'encodedState' | 'stateHash'
>

type BoundValidatedDocumentState = ValidatedDocumentState & {
  authoritativeSequence: number
  reconstructionEpoch: number
}

type RoomState = {
  authoritativeSequence: number
  gate: SerialGate
  initialized: boolean
  /** Suggestions this room saw leave the document durably, by id, with how
   * they left: a copy of one that comes back through a writer's stale view
   * takes that outcome. Facts about the store, so they survive a rebuild of
   * the room; bounded, oldest first out. */
  knownSuggestions: Map<string, KnownSuggestion>
  loaded: boolean
  phase: RoomPhase
  reconstructionEpoch: number
  /** Changes whenever the tail counters are assigned rather than counted
   * up, so a snapshot captured before can tell that its numbers no longer
   * belong to them. */
  tailAccountingEpoch: number
  tailBytes: number
  /** Normalized rows whose persistence outcome stayed unknown, by writer and
   * the hash of the client update they contain. The writer's resent update
   * is prepared from these bytes, so a late commit of the earlier row
   * changes nothing. */
  unresolvedNormalizations: Map<string, Uint8Array>
  updatesSinceSnapshot: number
  validatedDocument: BoundValidatedDocumentState | null
}

/** How many decided suggestions a room remembers for resurrected copies. */
const KNOWN_SUGGESTIONS_PER_ROOM = 4_096

/** How many normalized rows of unknown outcome a room remembers. */
const UNRESOLVED_NORMALIZATIONS_PER_ROOM = 64

type PendingUpdate = {
  /** Suggestions the update removed durably, registered once it is applied. */
  decided: KnownSuggestion[]
  gateEnteredAt: number
  hash: string
  persistedSequence: number
  /** No row holds the update, and the room already holds every effect of
   * it; acknowledged at the room's durable sequence. */
  redundant: boolean
  release: Release
  room: string
  sequence: number
  startedAt: number
  /** The persisted update bytes; null for a duplicate that wrote nothing. */
  update: Uint8Array | null
  updateBytes: number
  validatedDocument: ValidatedDocumentState
  wasDuplicate: boolean
}

export type PendingClientUpdateState = {
  hash: string
  persistedSequence: number
  projectionSequence: number
  sequence: number
}

type PreparingUpdate = {
  /** The normalized bytes sent to the store, while their outcome is open. */
  normalizedInFlight: { bytes: Uint8Array; key: string } | null
  persistenceAttempted: boolean
  reconstructionEpoch: number
}

export type PrepareClientUpdateInput = {
  allowNoop: boolean
  connectionId: string
  context: ConnectionContext
  document: Y.Doc
  room: string
  update: Uint8Array
}

export type CapturedDocument = {
  document: Y.Doc
  sequence: number
  /** The tail counters at capture time, which the stored snapshot covers. */
  tail: { accountingEpoch: number; bytes: number; updates: number }
}

export type ServerMutationInput = {
  actorKind: CollaborationActorKind
  actorUserId: string
  changeKind: Extract<CollaborationChangeKind, 'decision' | 'suggestion'>
  commandId: string
  commandPayloadHash: string
  decision: 'accept' | 'reject' | null
  document: Y.Doc
  documentId: string
  /** The room sequence the command was based on. */
  expectedSequence: number
  generation: number
  mutate: (clone: Y.Doc) => {
    patchIds: string[]
    suggestions: SuggestionDescriptor[]
    suggestionIds: string[]
  }
  requestedPatchIds: string[]
  room: string
  /**
   * How the room's sequence must relate to `expectedSequence`.
   *
   * `exact`: the room is still at the base. A command whose mutation is
   * computed against its base and cannot be moved onto later changes needs
   * this.
   *
   * `not_ahead`: the room is at the base or past it. The mutation runs on the
   * room as it is now, so it suits a command that applies to the current
   * state: a decision names its suggestions by id, and a publication that
   * carries its base is placed onto the current state before it is checked.
   * A base the room has not reached is refused under either rule.
   *
   * The durable store's compare-and-set always uses the room's current
   * sequence, so the order of persisted updates stays fail-closed.
   */
  sequenceRule: 'exact' | 'not_ahead'
}

export type ServerMutationResult = {
  ack: CollaborationDurableAck
  patchIds: string[]
  suggestionIds: string[]
}

export type DocumentCoordinatorCallbacks = {
  onAuthoritativeApplyFailure?: (room: string) => void
  onAuthoritativeApplySuccess?: (room: string) => void
}

export class DocumentCoordinator {
  private readonly pending = new Map<string, PendingUpdate>()
  private readonly preparing = new Set<string>()
  private readonly rooms = new Map<string, RoomState>()
  private nextReconstructionEpoch = 1
  private nextTailAccountingEpoch = 1

  constructor(
    private readonly api: CollaborationApi,
    private readonly leaseManager: InstanceLeaseManager,
    private readonly settings: CollaborationSettings,
    private readonly logger: SidecarLogger,
    private readonly metrics: SidecarMetrics,
    private readonly callbacks: DocumentCoordinatorCallbacks = {},
  ) {}

  initialize(
    room: string,
    persistedSequence: number,
    tail: { bytes: number; updates: number } = { bytes: 0, updates: 0 },
    validatedDocument: ValidatedDocumentState | null = null,
  ): void {
    const state = this.room(room)
    if (
      state.phase === 'awaiting_apply'
      || (state.phase === 'reconstruction_required' && state.loaded)
      || (state.initialized && state.authoritativeSequence !== persistedSequence)
    ) {
      throw new CollaborationError('internal_consistency', {
        closeCode: CloseCodes.internalConsistency,
      })
    }
    state.authoritativeSequence = persistedSequence
    state.initialized = true
    state.loaded = true
    state.phase = 'ready'
    this.resetTail(state, tail.bytes, tail.updates)
    state.validatedDocument = validatedDocument
      ? bindValidatedDocument(state, persistedSequence, validatedDocument)
      : null
  }

  assertJoinAllowed(room: string): void {
    const state = this.rooms.get(room)
    if (
      state?.phase === 'awaiting_apply'
      || (state?.phase === 'reconstruction_required' && state.loaded)
    ) {
      throw new CollaborationError('restarting', {
        closeCode: CloseCodes.restarting,
        httpStatus: 503,
      })
    }
  }

  isBroadcastBlocked(room: string): boolean {
    return this.rooms.get(room)?.phase === 'awaiting_apply'
  }

  pendingClientUpdate(connectionId: string): PendingClientUpdateState | null {
    const pending = this.pending.get(connectionId)
    if (!pending) return null
    const room = this.rooms.get(pending.room)
    if (!room) return null
    return {
      hash: pending.hash,
      persistedSequence: pending.persistedSequence,
      projectionSequence: room.authoritativeSequence,
      sequence: pending.sequence,
    }
  }

  requiresReconstruction(room: string): boolean {
    return this.rooms.get(room)?.phase === 'reconstruction_required'
  }

  reconstructionEpoch(room: string): number | null {
    return this.rooms.get(room)?.reconstructionEpoch ?? null
  }

  joinEpoch(room: string): number {
    return this.room(room).reconstructionEpoch
  }

  async prepareClientUpdate(input: PrepareClientUpdateInput): Promise<'noop' | 'pending'> {
    if (this.pending.has(input.connectionId) || this.preparing.has(input.connectionId)) {
      throw new CollaborationError('internal_consistency', {
        closeCode: CloseCodes.internalConsistency,
      })
    }
    if (input.update.byteLength > this.settings.frameLimitBytes) {
      throw new CollaborationError('message_too_large', {
        closeCode: CloseCodes.messageTooLarge,
        httpStatus: 413,
      })
    }

    const roomState = this.room(input.room)
    const preparing: PreparingUpdate = {
      normalizedInFlight: null,
      persistenceAttempted: false,
      reconstructionEpoch: roomState.reconstructionEpoch,
    }
    this.preparing.add(input.connectionId)
    let release: Release | null = null
    let clone: Y.Doc | null = null
    // Started before the gate on purpose: the wait in front of the room
    // queue is what a late writer in a burst actually pays, and it is
    // the only part the depth gauge could never express.
    const gateEnteredAt = performance.now()
    try {
      release = await roomState.gate.reserve()
      this.metrics.observeMilliseconds(
        'inqtrix_collaboration_gate_wait_seconds',
        performance.now() - gateEnteredAt,
      )
      assertPreparationCurrent(preparing, roomState)
      const fence = this.leaseManager.assertActive()
      if (!roomState.initialized || roomState.phase !== 'ready') {
        if (roomState.phase === 'reconstruction_required') {
          throw reconstructionRequired()
        }
        throw new CollaborationError('internal_consistency', {
          closeCode: CloseCodes.internalConsistency,
        })
      }
      if (input.context.expiresAt <= Date.now() / 1_000) {
        throw new CollaborationError('invalid_lease', {
          closeCode: CloseCodes.leaseInvalid,
          httpStatus: 401,
        })
      }
      const schemaHash = await getEditorSchemaFingerprint()
      if (
        input.context.protocolVersion !== this.settings.protocolVersion
        || input.context.schemaVersion !== this.settings.schemaVersion
        || input.context.schemaHash !== schemaHash
      ) {
        throw new CollaborationError('update_required', {
          closeCode: CloseCodes.incompatible,
          httpStatus: 409,
        })
      }

      const validationStartedAt = performance.now()
      const safeUpdate = validateSafeClientUpdate(input.update)
      const hash = hashBytes(safeUpdate)
      // An earlier preparation of this very update may have sent a
      // normalized row whose outcome stayed unknown. Those bytes contain the
      // update, so preparing them instead makes a late commit of the earlier
      // row change nothing: everything it holds is then in the room already.
      const unresolvedKey = `${input.context.user.id}:${hash}`
      const earlierNormalization = input.context.access === 'edit'
        ? roomState.unresolvedNormalizations.get(unresolvedKey) ?? null
        : null
      const preparedUpdate = earlierNormalization ?? safeUpdate
      const beforeValidated = this.authoritativeDocumentState(
        input.room,
        roomState,
        input.document,
      )
      const before = beforeValidated.canonicalJson
      clone = cloneDocument(input.document, beforeValidated.encodedState)
      const appliedUpdates: Uint8Array[] = []
      const captureAppliedUpdate = (update: Uint8Array): void => {
        appliedUpdates.push(update)
      }
      // Yjs applies the client's bytes in one remote transaction and may
      // follow it with one local transaction of its own that deletes
      // formatting items the merge left redundant. The room and every replay
      // of the store run the same follow-up for the same bytes, so it is part
      // of applying them. Any other follow-up is not.
      const followUps = { cleanups: 0, other: 0 }
      // What the update deleted, for the suggestion rules: each transaction
      // reports only the items it newly deleted.
      const deletions: Array<Y.Transaction['deleteSet']> = []
      let clientTransactionSeen = false
      const observeTransaction = (transaction: Y.Transaction): void => {
        deletions.push(transaction.deleteSet)
        if (!clientTransactionSeen) {
          clientTransactionSeen = true
          return
        }
        if (!changesState(transaction)) return
        if (isFormattingCleanupTransaction(transaction)) followUps.cleanups += 1
        else followUps.other += 1
      }
      clone.on('update', captureAppliedUpdate)
      clone.on('afterTransaction', observeTransaction)
      try {
        Y.applyUpdate(clone, preparedUpdate)
      } catch {
        this.logger.warn('client_update_rejected', { cause: 'apply_threw' })
        throw new CollaborationError('invalid_schema', {
          closeCode: CloseCodes.incompatible,
          httpStatus: 409,
        })
      } finally {
        clone.off('update', captureAppliedUpdate)
        clone.off('afterTransaction', observeTransaction)
      }
      // The clone started from the validated, causally complete room state,
      // so anything Yjs had to park as pending was caused by this update: it
      // depends on structs or deletions the room does not hold. That is a gap
      // in what the client sent, not a document that violates the schema, and
      // the client recovers from it by resending its unconfirmed updates in
      // order.
      if (clone.store.pendingStructs !== null || clone.store.pendingDs !== null) {
        this.logger.warn('client_update_rejected', { cause: 'causal_gap' })
        throw new CollaborationError('causal_gap', {
          closeCode: CloseCodes.causalGap,
          httpStatus: 409,
        })
      }
      // Every check below judges the state after the follow-up. A refusal of
      // an edit lease's update is held back until the normalization below
      // has had its chance.
      let refusal: CollaborationError | null = null
      let validated: ValidatedDocument | null = null
      try {
        validated = validateDocument(clone, this.settings.documentLimitBytes)
      } catch (error) {
        if (!isNormalizableRefusal(error, input.context.access)) throw error
        refusal = error
      }
      const formattingCleanup = appliedUpdates.length === 2
        && followUps.cleanups === 1
        && followUps.other === 0
      if (appliedUpdates.length > 1 && !formattingCleanup) {
        this.logger.warn('client_update_rejected', {
          cause: 'multiple_applied', applied: appliedUpdates.length,
        })
        throw invalidClientUpdate()
      }
      const novelUpdate = appliedUpdates[0] ?? null
      if (!novelUpdate) {
        // The room holds all of the update: whatever an earlier preparation
        // sent for it is settled.
        roomState.unresolvedNormalizations.delete(unresolvedKey)
        if (
          input.context.access === 'view'
          || input.allowNoop
        ) return 'noop'
        assertPreparationCurrent(preparing, roomState)
        const durable = await this.api.lookupUpdates({
          documentId: input.context.documentId,
          fence,
          generation: input.context.generation,
          hashes: [hash],
        })
        assertPreparationCurrent(preparing, roomState)
        let sequence: number
        const redundant = durable.length === 0
        if (redundant) {
          // The store holds no row under this hash, yet every effect of the
          // update is already in the room: another writer made the same
          // change first, or the update repeats changes that reached the
          // store in another update. The clone started from the validated
          // room state, which equals the store at the room's durable
          // sequence, so the update is durable there. A room without any
          // durable update has no sequence an acknowledgement could name.
          if (roomState.authoritativeSequence < 1) return 'noop'
          sequence = roomState.authoritativeSequence
        } else {
          const replay = durable[0]
          if (!replay || durable.length !== 1 || replay.hash !== hash) {
            throw new CollaborationError('internal_consistency', {
              closeCode: CloseCodes.internalConsistency,
            })
          }
          if (replay.sequence > roomState.authoritativeSequence) {
            this.requireReconstruction(input.room)
            throw new CollaborationError('internal_consistency', {
              closeCode: CloseCodes.internalConsistency,
            })
          }
          sequence = replay.sequence
        }
        roomState.phase = 'awaiting_apply'
        this.pending.set(input.connectionId, {
          decided: [],
          gateEnteredAt,
          hash,
          persistedSequence: roomState.authoritativeSequence,
          redundant,
          release,
          room: input.room,
          sequence,
          startedAt: performance.now(),
          update: null,
          updateBytes: 0,
          validatedDocument: beforeValidated,
          wasDuplicate: true,
        })
        release = null
        return 'pending'
      }
      let suggestionOperationsValidated = false
      if (input.context.access === 'suggest') {
        validateSafeSuggestionUpdate(safeUpdate, this.logger)
        suggestionOperationsValidated = true
      }
      let policy: SuggestionPolicyResult | null = null
      if (validated) {
        try {
          policy = validateSuggestionUpdate(
            before,
            validated.canonicalJson,
            input.context.access,
            input.context.user.id,
            {
              afterDocument: clone,
              beforeDocument: input.document,
              deletedByUpdate: Y.mergeDeleteSets(deletions),
            },
          )
        } catch (error) {
          if (!isNormalizableRefusal(error, input.context.access)) throw error
          refusal = error
        }
      }
      let normalization: EditMergeNormalization | null = null
      if (refusal) {
        normalization = this.normalizeRefusedEdit(input, roomState, refusal, {
          before,
          followUps: appliedUpdates.slice(1),
          merged: clone,
          roomState: beforeValidated,
          update: preparedUpdate,
        })
        clone.destroy()
        clone = normalization.state
        try {
          validated = validateDocument(clone, this.settings.documentLimitBytes)
          policy = validateSuggestionUpdate(
            before,
            validated.canonicalJson,
            input.context.access,
            input.context.user.id,
            {
              afterDocument: clone,
              beforeDocument: input.document,
              deletedByUpdate: normalization.deletedByUpdate,
            },
          )
        } catch (error) {
          this.metrics.increment('inqtrix_collaboration_edit_merge_normalizations_declined_total', {
            reason: 'still_refused',
          })
          this.logger.info('edit_merge_normalization_declined', {
            reason: 'still_refused',
            refusal: error instanceof CollaborationError ? error.reason : 'unexpected',
            room: input.room,
            units_read: normalization.unitsRead,
          })
          throw error
        }
      }
      if (!validated || !policy) {
        throw new CollaborationError('internal_consistency', {
          closeCode: CloseCodes.internalConsistency,
        })
      }
      const after = validated.canonicalJson
      if (policy.changeKind === 'suggestion' && !suggestionOperationsValidated) {
        validateSafeSuggestionUpdate(safeUpdate, this.logger)
      }
      this.metrics.observeMilliseconds(
        'inqtrix_collaboration_update_validation_seconds',
        performance.now() - validationStartedAt,
      )
      // Every local computation happens before the persistence attempt: a
      // failure here is a decision about this update, never an unknown
      // persistence outcome that would rebuild the room for everyone.
      const changeSummary = this.localChangeSummary(input.room, 'client', {
        after,
        before,
        changeKind: policy.changeKind,
        decision: null,
        suggestions: policy.suggestions,
      })
      const decided = decidedSuggestions(before, policy)
      // What the row holds: the client's bytes, or, when the update was
      // normalized or prepared from an earlier normalized row, those bytes.
      const persistedUpdate = normalization?.bytes ?? preparedUpdate
      const normalized = persistedUpdate !== safeUpdate

      const persistenceStartedAt = performance.now()
      assertPreparationCurrent(preparing, roomState)
      preparing.persistenceAttempted = true
      if (normalized) {
        preparing.normalizedInFlight = { bytes: persistedUpdate, key: unresolvedKey }
      }
      const persisted = await this.persistResolvingOutcome(input.room, {
        actorKind: input.context.user.kind === 'guest' ? 'guest' : 'human',
        actorUserId: input.context.user.id,
        changeKind: policy.changeKind,
        changeSummary,
        commandPayloadHash: null,
        decision: null,
        decisionOutcome: null,
        documentId: input.context.documentId,
        fence,
        generation: input.context.generation,
        hash: normalized ? hashBytes(persistedUpdate) : hash,
        leaseId: input.context.leaseId,
        patches: policy.patches,
        rejectedSuggestionIds: policy.rejectedSuggestionIds,
        suggestions: policy.suggestions,
        suggestionIds: policy.suggestionIds,
        update: persistedUpdate,
      })
      preparing.normalizedInFlight = null
      roomState.unresolvedNormalizations.delete(unresolvedKey)
      assertPreparationCurrent(preparing, roomState)
      this.metrics.observeMilliseconds(
        'inqtrix_collaboration_update_persistence_seconds',
        performance.now() - persistenceStartedAt,
      )
      verifyPersistedResult(roomState, persisted)
      if (formattingCleanup && !normalization) {
        this.metrics.increment('inqtrix_collaboration_formatting_cleanups_accepted_total')
        this.logger.debug('formatting_cleanup_accepted', { room: input.room })
      }
      roomState.phase = 'awaiting_apply'
      if (normalized) {
        // The room applies the whole row as one update while its broadcast
        // is held, before the transport applies the client's bytes, which
        // then change nothing. Every connection, the writer's included,
        // receives one frame for it and never the state the checks refused.
        Y.applyUpdate(input.document, persistedUpdate, { source: 'local' })
        if (!matchesValidatedDocument(input.document, validated)) {
          this.logger.error('authoritative_apply_mismatch', { room: input.room })
          throw new CollaborationError('internal_consistency', {
            closeCode: CloseCodes.internalConsistency,
          })
        }
      }
      if (normalization) this.recordNormalization(input.room, normalization)
      this.pending.set(input.connectionId, {
        decided,
        hash,
        persistedSequence: persisted.persistedSequence,
        redundant: false,
        release,
        room: input.room,
        sequence: persisted.sequence,
        // Two clocks on purpose. `startedAt` keeps its established
        // meaning (apply + verify after persistence returned) so the
        // existing durable_ack series stays comparable across releases;
        // `gateEnteredAt` carries the whole chain the user waits for.
        gateEnteredAt,
        startedAt: performance.now(),
        update: persisted.duplicate ? null : persistedUpdate,
        updateBytes: persistedUpdate.byteLength,
        validatedDocument: validatedDocumentState(validated),
        wasDuplicate: persisted.duplicate,
      })
      release = null
      return 'pending'
    } catch (error) {
      // Reconstruct only when the outcome is genuinely unknown. A 4xx from the
      // API is a deterministic rejection: the transaction rolled back and
      // nothing was written, so the room still matches the store. Tearing it
      // down would discard confirmed state that every other participant is
      // already showing. Timeouts, network errors and 5xx stay unknown.
      if (preparing.persistenceAttempted && !isDeterministicRejection(error)) {
        const inFlight = preparing.normalizedInFlight
        if (inFlight) {
          boundedSet(
            roomState.unresolvedNormalizations,
            inFlight.key,
            inFlight.bytes,
            UNRESOLVED_NORMALIZATIONS_PER_ROOM,
            () => this.metrics.increment('inqtrix_collaboration_edit_merge_unresolved_evicted_total'),
          )
        }
        this.requireReconstruction(input.room)
      }
      const mapped = collaborationError(error)
      this.metrics.increment('inqtrix_collaboration_rejections_total', { reason: mapped.reason })
      throw mapped
    } finally {
      clone?.destroy()
      release?.()
      this.preparing.delete(input.connectionId)
    }
  }

  /**
   * Normalizes the merge of an edit lease's update that a check refused.
   * Returns the normalization, or throws the refusal when none applies. A
   * record in another person's name that the room does not know is refused
   * with a warning: no normalization writes or keeps one.
   */
  private normalizeRefusedEdit(
    input: PrepareClientUpdateInput,
    roomState: RoomState,
    refusal: CollaborationError,
    merge: {
      before: ValidatedDocumentState['canonicalJson']
      followUps: readonly Uint8Array[]
      merged: Y.Doc
      roomState: ValidatedDocumentState
      update: Uint8Array
    },
  ): EditMergeNormalization {
    const startedAt = performance.now()
    const result = normalizeEditMerge({
      actorUserId: input.context.user.id,
      before: merge.before,
      documentLimitBytes: this.settings.documentLimitBytes,
      followUps: merge.followUps,
      known: (suggestionId) => roomState.knownSuggestions.get(suggestionId),
      merged: merge.merged,
      room: input.document,
      roomState: merge.roomState.encodedState,
      roomStateHash: merge.roomState.stateHash,
      update: merge.update,
    })
    this.metrics.observeMilliseconds(
      'inqtrix_collaboration_edit_merge_normalization_seconds',
      performance.now() - startedAt,
    )
    if (result.declined === null) return result.normalization
    this.metrics.increment('inqtrix_collaboration_edit_merge_normalizations_declined_total', {
      reason: result.declined,
    })
    if (result.declined === 'forged_record') {
      // The collaboration lease leads to the writer through its record in
      // the service. A person's id never stands in a log line, and neither
      // does the session: a member's session id is the value of the
      // browser's session cookie.
      this.logger.warn('edit_merge_foreign_record_refused', {
        actor_kind: input.context.user.kind === 'guest' ? 'guest' : 'user',
        lease_id: input.context.leaseId,
        room: input.room,
        suggestion_id: result.suggestionId,
      })
    } else {
      // A spent budget is a cap that cut a normalization short: loud.
      const log = result.declined === 'budget' ? this.logger.warn : this.logger.info
      log.call(this.logger, 'edit_merge_normalization_declined', {
        reason: result.declined,
        refusal: refusal.reason,
        room: input.room,
        units_read: result.unitsRead,
      })
    }
    throw refusal
  }

  private recordNormalization(room: string, normalization: EditMergeNormalization): void {
    this.metrics.increment('inqtrix_collaboration_edit_merge_normalized_updates_total')
    for (const [rule, runs] of Object.entries(normalization.rules).sort(([left], [right]) => left.localeCompare(right))) {
      if (runs) this.metrics.add('inqtrix_collaboration_edit_merge_normalizations_total', runs, { rule })
    }
    this.logger.info('edit_merge_normalized', {
      room,
      rounds: normalization.rounds,
      rules: Object.keys(normalization.rules).sort().join(','),
      units_read: normalization.unitsRead,
    })
  }

  private registerDecidedSuggestions(roomState: RoomState, decided: readonly KnownSuggestion[]): void {
    for (const suggestion of decided) {
      boundedSet(
        roomState.knownSuggestions,
        suggestion.suggestionId,
        suggestion,
        KNOWN_SUGGESTIONS_PER_ROOM,
        () => this.metrics.increment('inqtrix_collaboration_known_suggestions_evicted_total'),
      )
    }
  }

  /**
   * Judges a sync frame of a view connection without the room gate. A frame
   * whose every struct and deletion the room already holds changes nothing
   * and is answered by Hocuspocus's read-only path. Any other frame would be
   * a change, which view access never may make, so it is refused at once: a
   * malformed frame `invalid_schema`, anything else `access_revoked`.
   */
  screenViewSyncFrame(document: Y.Doc, update: Uint8Array): void {
    try {
      if (update.byteLength > this.settings.frameLimitBytes) {
        throw new CollaborationError('message_too_large', {
          closeCode: CloseCodes.messageTooLarge,
          httpStatus: 413,
        })
      }
      const safeUpdate = validateSafeClientUpdate(update)
      if (!Y.snapshotContainsUpdate(Y.snapshot(document), safeUpdate)) {
        throw new CollaborationError('access_revoked', {
          closeCode: CloseCodes.accessRevoked,
          httpStatus: 403,
        })
      }
      this.metrics.increment('inqtrix_collaboration_view_sync_frames_answered_before_gate_total')
    } catch (error) {
      const mapped = collaborationError(error)
      this.metrics.increment('inqtrix_collaboration_rejections_total', { reason: mapped.reason })
      throw mapped
    }
  }

  finishClientUpdate(connectionId: string, document: Y.Doc): CollaborationDurableAck | null {
    const pending = this.pending.get(connectionId)
    if (!pending) return null
    this.pending.delete(connectionId)
    try {
      const roomState = this.rooms.get(pending.room)
      if (!roomState || roomState.phase !== 'awaiting_apply') {
        throw new CollaborationError('internal_consistency', {
          closeCode: CloseCodes.internalConsistency,
        })
      }
      if (
        !matchesValidatedDocument(document, pending.validatedDocument)
        && !this.applyCommittedUpdate(pending, roomState, document)
      ) {
        this.logger.error('authoritative_apply_mismatch', { room: pending.room })
        throw new CollaborationError('internal_consistency', {
          closeCode: CloseCodes.internalConsistency,
        })
      }
      roomState.authoritativeSequence = pending.persistedSequence
      if (!pending.wasDuplicate) {
        roomState.tailBytes += pending.updateBytes
        roomState.updatesSinceSnapshot += 1
      }
      this.registerDecidedSuggestions(roomState, pending.decided)
      const removedByEdit = pending.decided.filter((suggestion) => suggestion.outcome === 'rejected').length
      if (removedByEdit > 0 && !pending.wasDuplicate) {
        this.metrics.add('inqtrix_collaboration_suggestions_removed_by_edit_total', removedByEdit)
      }
      roomState.validatedDocument = bindValidatedDocument(
        roomState,
        pending.persistedSequence,
        pending.validatedDocument,
      )
      roomState.phase = 'ready'
      if (pending.redundant) {
        this.metrics.increment('inqtrix_collaboration_redundant_updates_acknowledged_total')
      }
      this.metrics.observeMilliseconds(
        'inqtrix_collaboration_durable_ack_seconds',
        performance.now() - pending.startedAt,
      )
      // What the writer actually waited for: gate queue, validation,
      // persistence, authoritative apply and verification together.
      this.metrics.observeMilliseconds(
        'inqtrix_collaboration_update_total_seconds',
        performance.now() - pending.gateEnteredAt,
      )
      return {
        hash: pending.hash,
        sequence: pending.sequence,
        type: 'durable_ack',
      }
    } catch (error) {
      this.requireReconstruction(pending.room)
      throw error
    } finally {
      pending.release()
    }
  }

  /**
   * Apply a committed update that the transport path did not apply.
   *
   * The transport skips an update when its connection turned read-only after
   * the update was received, for example when a token sync downgraded the
   * access while the update was already persisting. The store has the update,
   * so the room must have it too. The gate is still held, so the room can only
   * be in the state it had before this update; any other state stays a
   * mismatch.
   */
  private applyCommittedUpdate(
    pending: PendingUpdate,
    roomState: RoomState,
    document: Y.Doc,
  ): boolean {
    const before = roomState.validatedDocument
    if (
      pending.update === null
      || before === null
      || !matchesValidatedDocument(document, before)
    ) return false
    Y.applyUpdate(document, pending.update, { source: 'local' })
    if (!matchesValidatedDocument(document, pending.validatedDocument)) return false
    this.metrics.increment('inqtrix_collaboration_committed_updates_applied_by_coordinator_total')
    this.logger.warn('committed_update_applied_by_coordinator', { room: pending.room })
    return true
  }

  /**
   * Persist an update and resolve an unknown outcome by its hash.
   *
   * A 4xx is a decision: nothing was written and the error stands. Any other
   * failure (timeout, transport, 5xx, unreadable answer) leaves the outcome
   * unknown, so the store is asked whether the hash landed. A row proves the
   * write committed. No row does NOT prove the opposite, because a request the
   * store has not started yet can still commit after the lookup, so the
   * original error stands and the caller rebuilds the room from the store.
   * The recorded outcome separates a store that answered "not found" from a
   * lookup that failed itself and from an answer that cannot be this update.
   */
  private async persistResolvingOutcome(
    room: string,
    request: PersistUpdateInput,
  ): Promise<PersistUpdateResult> {
    try {
      return await this.api.persistUpdate(request)
    } catch (error) {
      if (isDeterministicRejection(error)) throw error
      let landed: DurableUpdateLookup[]
      try {
        landed = await this.api.lookupUpdates({
          documentId: request.documentId,
          fence: request.fence,
          generation: request.generation,
          hashes: [request.hash],
        })
      } catch (lookupError) {
        this.recordPersistOutcome(room, 'lookup_failed', error, lookupError)
        throw error
      }
      if (landed.length === 0) {
        this.recordPersistOutcome(room, 'not_found', error)
        throw error
      }
      const row = landed[0]
      if (!row || landed.length !== 1 || row.hash !== request.hash) {
        this.recordPersistOutcome(room, 'inconsistent', error)
        throw new CollaborationError('internal_consistency', {
          closeCode: CloseCodes.internalConsistency,
        })
      }
      this.recordPersistOutcome(room, 'landed', error)
      return {
        duplicate: false,
        persistedSequence: row.sequence,
        sequence: row.sequence,
      }
    }
  }

  private recordPersistOutcome(
    room: string,
    outcome: 'inconsistent' | 'landed' | 'lookup_failed' | 'not_found',
    persistError: unknown,
    lookupError?: unknown,
  ): void {
    this.metrics.increment('inqtrix_collaboration_persist_outcome_resolutions_total', {
      outcome,
    })
    this.logger.warn('persist_outcome_resolved', {
      outcome,
      persist_reason: failureReason(persistError),
      persist_status: failureStatus(persistError),
      room,
      ...(lookupError === undefined
        ? {}
        : {
            lookup_reason: failureReason(lookupError),
            lookup_status: failureStatus(lookupError),
          }),
    })
  }

  /** Releases a persisted update that will not be applied; its room is then
   * rebuilt from the store. */
  abortClientUpdate(connectionId: string): void {
    const pending = this.pending.get(connectionId)
    if (!pending) return
    this.pending.delete(connectionId)
    pending.release()
    this.requireReconstruction(pending.room)
  }

  async applyServerMutation(input: ServerMutationInput): Promise<ServerMutationResult> {
    const roomState = this.room(input.room)
    const release = await roomState.gate.reserve()
    let clone: Y.Doc | null = null
    let authoritativeApplyStarted = false
    let persistenceAttempted = false
    try {
      const fence = this.leaseManager.assertActive()
      const replay = await this.api.lookupCommand({
        commandId: input.commandId,
        commandPayloadHash: input.commandPayloadHash,
        documentId: input.documentId,
        fence,
        generation: input.generation,
      })
      if (replay) {
        if (
          replay.actorKind !== input.actorKind
          || replay.actorUserId !== input.actorUserId
          || replay.changeKind !== input.changeKind
          || replay.commandId !== input.commandId
          || replay.commandPayloadHash !== input.commandPayloadHash
          || replay.decision !== input.decision
          || replay.generation !== input.generation
          || !sameIds(replay.patchIds, input.requestedPatchIds)
        ) {
          throw new CollaborationError('sequence_conflict', {
            closeCode: CloseCodes.incompatible,
            httpStatus: 409,
          })
        }
        if (
          !roomState.initialized
          || roomState.phase !== 'ready'
          || roomState.authoritativeSequence < replay.sequence
        ) {
          roomState.phase = 'awaiting_apply'
          authoritativeApplyStarted = true
          await this.reconcileCommandReplay(roomState, input, replay.sequence, fence)
          roomState.phase = 'ready'
          authoritativeApplyStarted = false
          this.notifyAuthoritativeApplySuccess(input.room)
        } else {
          this.authoritativeDocumentState(input.room, roomState, input.document)
        }
        return {
          ack: {
            hash: replay.updateHash,
            sequence: replay.sequence,
            type: 'durable_ack',
          },
          patchIds: replay.patchIds,
          suggestionIds: replay.suggestionIds,
        }
      }
      if (
        !roomState.initialized
        || roomState.phase !== 'ready'
        || (
          input.sequenceRule === 'exact'
            ? roomState.authoritativeSequence !== input.expectedSequence
            : roomState.authoritativeSequence < input.expectedSequence
        )
      ) {
        throw new CollaborationError('sequence_conflict', {
          closeCode: CloseCodes.incompatible,
          httpStatus: 409,
        })
      }
      const beforeValidated = this.authoritativeDocumentState(
        input.room,
        roomState,
        input.document,
      )
      clone = cloneDocument(input.document, beforeValidated.encodedState)
      const beforeStateHash = beforeValidated.stateHash
      const before = beforeValidated.canonicalJson
      const beforeVector = Y.encodeStateVector(clone)
      const mutation = input.mutate(clone)
      const validated = validateDocument(clone, this.settings.documentLimitBytes)
      if (validated.stateHash === beforeStateHash) {
        throw new CollaborationError('decision_conflict', {
          closeCode: CloseCodes.incompatible,
          httpStatus: 409,
        })
      }
      const update = Y.encodeStateAsUpdate(clone, beforeVector)
      const hash = hashBytes(update)
      const after = validated.canonicalJson
      const patches = deriveSuggestionPatchStates(
        before,
        after,
        mutation.suggestionIds,
      )
      const changeSummary = this.localChangeSummary(input.room, 'decision', {
        after,
        before,
        changeKind: input.changeKind,
        decision: input.decision,
        suggestions: mutation.suggestions,
      })
      persistenceAttempted = true
      const persisted = await this.api.persistUpdate({
        actorKind: input.actorKind,
        actorUserId: input.actorUserId,
        changeKind: input.changeKind,
        changeSummary,
        commandId: input.commandId,
        commandPayloadHash: input.commandPayloadHash,
        decision: input.decision,
        decisionOutcome: input.decision === 'accept'
          ? 'accepted'
          : input.decision === 'reject'
            ? 'rejected'
            : null,
        documentId: input.documentId,
        expectedSequence: roomState.authoritativeSequence,
        fence,
        generation: input.generation,
        hash,
        leaseId: null,
        patches,
        rejectedSuggestionIds: [],
        suggestions: mutation.suggestions,
        suggestionIds: mutation.suggestionIds,
        update,
      })
      verifyPersistedResult(roomState, persisted)
      roomState.phase = 'awaiting_apply'
      authoritativeApplyStarted = true
      Y.applyUpdate(input.document, update, {
        context: { actorUserId: input.actorUserId, commandId: input.commandId },
        source: 'local',
      })
      if (!matchesValidatedDocument(input.document, validated)) {
        throw new CollaborationError('internal_consistency', {
          closeCode: CloseCodes.internalConsistency,
        })
      }
      roomState.authoritativeSequence = persisted.persistedSequence
      roomState.tailBytes += persisted.duplicate ? 0 : update.byteLength
      roomState.updatesSinceSnapshot += persisted.duplicate ? 0 : 1
      roomState.validatedDocument = bindValidatedDocument(
        roomState,
        persisted.persistedSequence,
        validated,
      )
      roomState.phase = 'ready'
      authoritativeApplyStarted = false
      if (input.decision !== null) {
        const outcome: SuggestionOutcome = input.decision === 'accept' ? 'accepted' : 'rejected'
        this.registerDecidedSuggestions(
          roomState,
          mutation.suggestions.map((suggestion) => ({ ...suggestion, outcome })),
        )
      }
      this.notifyAuthoritativeApplySuccess(input.room)
      return {
        ack: { hash, sequence: persisted.sequence, type: 'durable_ack' },
        patchIds: mutation.patchIds,
        suggestionIds: mutation.suggestionIds,
      }
    } catch (error) {
      if (persistenceAttempted || authoritativeApplyStarted) {
        this.requireReconstruction(input.room)
        this.notifyAuthoritativeApplyFailure(input.room)
      }
      throw collaborationError(error)
    } finally {
      clone?.destroy()
      release()
    }
  }

  private async reconcileCommandReplay(
    roomState: RoomState,
    input: ServerMutationInput,
    commandSequence: number,
    fence: ReturnType<InstanceLeaseManager['assertActive']>,
  ): Promise<void> {
    const loaded = await this.api.loadDocumentState({
      documentId: input.documentId,
      fence,
      generation: input.generation,
    })
    if (
      loaded.persistedSequence < commandSequence
      || loaded.persistedSequence < roomState.authoritativeSequence
    ) {
      throw new CollaborationError('internal_consistency', {
        closeCode: CloseCodes.internalConsistency,
      })
    }
    let recoveredUpdates = loaded.updates
    const recoveredValidation = await reconstructValidatedDocument(loaded, {
      documentId: input.documentId,
      generation: input.generation,
      schemaVersion: this.settings.schemaVersion,
    }, this.settings.documentLimitBytes, {
      onCandidateRejected: ({ candidateIndex, reason }) => {
        this.metrics.increment('inqtrix_collaboration_snapshot_fallbacks_total')
        this.logger.warn('snapshot_candidate_rejected', {
          candidate_index: candidateIndex,
          reason,
        })
      },
      onCandidateSelected: ({ updates }) => {
        recoveredUpdates = [...updates]
      },
    })
    const recovered = recoveredValidation.document
    try {
      const update = Y.encodeStateAsUpdate(recovered, Y.encodeStateVector(input.document))
      Y.applyUpdate(input.document, update, {
        context: { commandId: input.commandId },
        source: 'command_replay',
      })
      if (!matchesValidatedDocument(input.document, recoveredValidation)) {
        throw new CollaborationError('internal_consistency', {
          closeCode: CloseCodes.internalConsistency,
        })
      }
      roomState.initialized = true
      roomState.loaded = true
      roomState.authoritativeSequence = loaded.persistedSequence
      this.resetTail(
        roomState,
        recoveredUpdates.reduce((total, item) => total + item.update.byteLength, 0),
        recoveredUpdates.length,
      )
      roomState.validatedDocument = bindValidatedDocument(
        roomState,
        loaded.persistedSequence,
        recoveredValidation,
      )
      this.metrics.increment('inqtrix_collaboration_command_reconciliations_total')
      this.logger.warn('command_replay_reconciled', {
        sequence: commandSequence,
      })
    } finally {
      recovered.destroy()
    }
  }

  getPersistedSequence(room: string): number {
    const state = this.rooms.get(room)
    if (
      !state?.initialized
      || state.phase === 'loading'
      || state.phase === 'reconstruction_required'
    ) {
      throw new CollaborationError('internal_consistency', {
        closeCode: CloseCodes.internalConsistency,
      })
    }
    return state.authoritativeSequence
  }

  async captureDocument(room: string, document: Y.Doc): Promise<CapturedDocument> {
    const roomState = this.room(room)
    const release = await roomState.gate.reserve()
    try {
      this.leaseManager.assertActive()
      if (!roomState.initialized || roomState.phase !== 'ready') {
        throw new CollaborationError('internal_consistency', {
          closeCode: CloseCodes.internalConsistency,
        })
      }
      const validated = this.authoritativeDocumentState(room, roomState, document)
      const captured = cloneDocument(document, validated.encodedState)
      return {
        document: captured,
        sequence: roomState.authoritativeSequence,
        tail: {
          accountingEpoch: roomState.tailAccountingEpoch,
          bytes: roomState.tailBytes,
          updates: roomState.updatesSinceSnapshot,
        },
      }
    } finally {
      release()
    }
  }

  shouldSnapshot(room: string): boolean {
    const state = this.rooms.get(room)
    return Boolean(state?.initialized && state.phase === 'ready' && (
      state.updatesSinceSnapshot >= this.settings.snapshotMaxUpdates
      || state.tailBytes >= this.settings.snapshotTailBytes
    ))
  }

  hasUnsnapshottedUpdates(room: string): boolean {
    const state = this.rooms.get(room)
    return Boolean(
      state?.initialized
      && state.phase === 'ready'
      && (state.updatesSinceSnapshot > 0 || state.tailBytes > 0),
    )
  }

  /** Records a stored snapshot: the tail shrinks by what the snapshot
   * captured, and updates applied after the capture stay in it. Returns
   * whether the snapshot covers the room's current sequence. */
  markSnapshot(room: string, captured: CapturedDocument): boolean {
    const state = this.rooms.get(room)
    if (!state?.initialized) return false
    if (state.tailAccountingEpoch === captured.tail.accountingEpoch) {
      // Within one accounting epoch the counters only grow, under the room
      // gate the capture also held, so the difference is exactly the tail
      // applied after the capture. Counters assigned since then (a reload
      // counted the tail again) keep their value.
      const updates = state.updatesSinceSnapshot - captured.tail.updates
      const bytes = state.tailBytes - captured.tail.bytes
      if (updates < 0 || bytes < 0) {
        this.logger.warn('snapshot_tail_accounting_mismatch', {
          bytes,
          room,
          updates,
        })
      }
      this.resetTail(state, Math.max(0, bytes), Math.max(0, updates))
    }
    return state.phase === 'ready' && captured.sequence === state.authoritativeSequence
  }

  async awaitRoom(room: string): Promise<void> {
    await this.rooms.get(room)?.gate.awaitIdle()
  }

  async awaitAll(): Promise<void> {
    await Promise.all([...this.rooms.values()].map((state) => state.gate.awaitIdle()))
  }

  markUnloaded(room: string): void {
    const state = this.rooms.get(room)
    if (!state) return
    state.loaded = false
    state.initialized = false
    state.validatedDocument = null
    if (state.phase === 'reconstruction_required') {
      this.resetTail(state, 0, 0)
    } else if (state.gate.isIdle()) {
      this.rooms.delete(room)
    }
    this.updateQueueMetrics()
  }

  private room(room: string): RoomState {
    const existing = this.rooms.get(room)
    if (existing) return existing
    const created: RoomState = {
      authoritativeSequence: 0,
      gate: new SerialGate(() => this.updateQueueMetrics()),
      initialized: false,
      knownSuggestions: new Map(),
      loaded: false,
      phase: 'loading',
      reconstructionEpoch: this.nextReconstructionEpoch++,
      tailAccountingEpoch: this.nextTailAccountingEpoch++,
      tailBytes: 0,
      unresolvedNormalizations: new Map(),
      updatesSinceSnapshot: 0,
      validatedDocument: null,
    }
    this.rooms.set(room, created)
    this.updateQueueMetrics()
    return created
  }

  /** The bounded summary of a change, computed before anything is
   * persisted. It projects the document, so a document it cannot project
   * refuses the update deterministically: the client update as
   * `invalid_schema`, the decision as `decision_conflict`. */
  private localChangeSummary(
    room: string,
    path: 'client' | 'decision',
    change: Parameters<typeof summarizeEditorChange>[0],
  ): ReturnType<typeof summarizeEditorChange> {
    try {
      return summarizeEditorChange(change)
    } catch (error) {
      this.logger.warn('change_summary_failed', {
        cause: error instanceof Error ? error.name : 'unknown',
        path,
        room,
      })
      throw new CollaborationError(path === 'client' ? 'invalid_schema' : 'decision_conflict', {
        closeCode: CloseCodes.incompatible,
        httpStatus: 409,
      })
    }
  }

  private requireReconstruction(room: string): void {
    const state = this.room(room)
    if (state.phase !== 'reconstruction_required') {
      state.reconstructionEpoch = this.nextReconstructionEpoch++
    }
    state.initialized = false
    state.phase = 'reconstruction_required'
    this.resetTail(state, 0, 0)
    state.validatedDocument = null
  }

  /** Assigns the tail counters; every assignment starts a new accounting
   * epoch, while applied updates only count up within one. */
  private resetTail(state: RoomState, bytes: number, updates: number): void {
    state.tailAccountingEpoch = this.nextTailAccountingEpoch++
    state.tailBytes = bytes
    state.updatesSinceSnapshot = updates
  }

  private authoritativeDocumentState(
    room: string,
    state: RoomState,
    document: Y.Doc,
  ): ValidatedDocumentState {
    const cached = state.validatedDocument
    if (!cached) {
      const validated = validateDocument(document, this.settings.documentLimitBytes)
      const value = validatedDocumentState(validated)
      state.validatedDocument = bindValidatedDocument(
        state,
        state.authoritativeSequence,
        value,
      )
      return value
    }
    if (
      cached.authoritativeSequence !== state.authoritativeSequence
      || cached.reconstructionEpoch !== state.reconstructionEpoch
      || !matchesValidatedDocument(document, cached)
    ) {
      this.logger.error('authoritative_state_mismatch', { room })
      this.requireReconstruction(room)
      throw new CollaborationError('internal_consistency', {
        closeCode: CloseCodes.internalConsistency,
      })
    }
    return cached
  }

  private notifyAuthoritativeApplyFailure(room: string): void {
    try {
      this.callbacks.onAuthoritativeApplyFailure?.(room)
    } catch {
      this.logger.error('authoritative_apply_callback_failed', { outcome: 'failure' })
    }
  }

  private notifyAuthoritativeApplySuccess(room: string): void {
    try {
      this.callbacks.onAuthoritativeApplySuccess?.(room)
    } catch {
      this.logger.error('authoritative_apply_callback_failed', { outcome: 'success' })
    }
  }

  private updateQueueMetrics(): void {
    this.metrics.set('inqtrix_collaboration_rooms', this.rooms.size)
    this.metrics.set(
      'inqtrix_collaboration_document_queue_depth',
      [...this.rooms.values()].reduce((total, state) => total + state.gate.depth, 0),
    )
  }
}

/** Whether a transaction inserted a struct or deleted anything. */
function changesState(transaction: Y.Transaction): boolean {
  for (const [client, clock] of transaction.afterState) {
    if ((transaction.beforeState.get(client) ?? 0) !== clock) return true
  }
  for (const ranges of transaction.deleteSet.clients.values()) {
    if (ranges.length > 0) return true
  }
  return false
}

/** A refusal the normalization may still resolve: the document or
 * suggestion checks refused the merge of an edit lease's update. A
 * suggest-only lease is judged strictly and never normalized. */
function isNormalizableRefusal(
  error: unknown,
  access: ConnectionContext['access'],
): error is CollaborationError {
  return access === 'edit'
    && error instanceof CollaborationError
    && (error.reason === 'invalid_schema' || error.reason === 'suggestion_policy_violation')
}

/** The suggestions an accepted client update removed for good, with the
 * outcome a copy of one takes: removed by the edit, which a copy takes as a
 * rejection, or superseded by the writer's own slash command. */
function decidedSuggestions(
  before: ValidatedDocumentState['canonicalJson'],
  policy: SuggestionPolicyResult,
): KnownSuggestion[] {
  const superseded = policy.patches.flatMap((patch) => patch.supersededSuggestionIds)
  if (policy.rejectedSuggestionIds.length === 0 && superseded.length === 0) return []
  const records = collectSuggestionRecords(before)
  const decided: KnownSuggestion[] = []
  const add = (suggestionId: string, outcome: SuggestionOutcome): void => {
    const record = records.get(suggestionId)
    if (!record) return
    decided.push({
      authorId: record.authorId,
      createdAt: record.createdAt,
      kind: record.kind,
      outcome,
      patchId: record.patchId,
      suggestionId,
    })
  }
  for (const suggestionId of policy.rejectedSuggestionIds) add(suggestionId, 'rejected')
  for (const suggestionId of superseded) add(suggestionId, 'superseded')
  return decided
}

/** Sets a key in an insertion-ordered map, dropping the oldest entries
 * beyond `limit`; `onEvict` counts each dropped entry. */
function boundedSet<V>(
  map: Map<string, V>,
  key: string,
  value: V,
  limit: number,
  onEvict: () => void,
): void {
  map.delete(key)
  map.set(key, value)
  while (map.size > limit) {
    const oldest = map.keys().next()
    if (oldest.done) break
    map.delete(oldest.value)
    onEvict()
  }
}

function validatedDocumentState(
  validated: ValidatedDocumentState,
): ValidatedDocumentState {
  return {
    canonicalJson: validated.canonicalJson,
    encodedState: validated.encodedState,
    stateHash: validated.stateHash,
  }
}

function bindValidatedDocument(
  state: RoomState,
  authoritativeSequence: number,
  validated: ValidatedDocumentState,
): BoundValidatedDocumentState {
  return {
    ...validatedDocumentState(validated),
    authoritativeSequence,
    reconstructionEpoch: state.reconstructionEpoch,
  }
}

function matchesValidatedDocument(
  document: Y.Doc,
  validated: ValidatedDocumentState,
): boolean {
  const encodedState = Y.encodeStateAsUpdate(document)
  return hashBytes(encodedState) === validated.stateHash
    && sameBytes(encodedState, validated.encodedState)
}

function assertPreparationCurrent(
  preparing: PreparingUpdate,
  roomState: RoomState,
): void {
  if (preparing.reconstructionEpoch !== roomState.reconstructionEpoch) {
    throw reconstructionRequired()
  }
}

/** The bounded reason of a failed internal API call, for logs. The message
 * of an unexpected error is not logged: it can carry anything. */
function failureReason(error: unknown): string {
  if (error instanceof ApiRequestError || error instanceof CollaborationError) {
    return error.reason
  }
  return 'unexpected'
}

function failureStatus(error: unknown): number | null {
  return error instanceof ApiRequestError ? error.status : null
}

function reconstructionRequired(): CollaborationError {
  return new CollaborationError('restarting', {
    closeCode: CloseCodes.restarting,
    httpStatus: 503,
  })
}

function suggestionPolicyViolation(): CollaborationError {
  return new CollaborationError('suggestion_policy_violation', {
    closeCode: CloseCodes.accessRevoked,
    httpStatus: 403,
  })
}

function invalidClientUpdate(): CollaborationError {
  return new CollaborationError('invalid_schema', {
    closeCode: CloseCodes.incompatible,
    httpStatus: 409,
  })
}

function validateSafeClientUpdate(update: Uint8Array): Uint8Array {
  try {
    return validateCanonicalYjsV1Update(update)
  } catch {
    throw invalidClientUpdate()
  }
}

function validateSafeSuggestionUpdate(
  update: Uint8Array,
  logger: SidecarLogger,
): Uint8Array {
  try {
    return validateSuggestionYjsUpdate(update)
  } catch (error) {
    logger.warn('suggestion_update_rejected', {
      validation_reason: error instanceof Error ? error.message : 'unknown',
    })
    throw suggestionPolicyViolation()
  }
}


function sameIds(left: readonly string[], right: readonly string[]): boolean {
  if (left.length !== right.length) return false
  const sortedLeft = [...left].sort()
  const sortedRight = [...right].sort()
  return sortedLeft.every((value, index) => value === sortedRight[index])
}

class SerialGate {
  private currentTail: Promise<void> = Promise.resolve()
  depth = 0

  constructor(private readonly onChange: () => void) {}

  async reserve(): Promise<Release> {
    const predecessor = this.currentTail
    let unlock = (): void => undefined
    const held = new Promise<void>((resolve) => {
      unlock = resolve
    })
    this.currentTail = predecessor.then(() => held)
    this.depth += 1
    this.onChange()
    await predecessor
    let released = false
    return () => {
      if (released) return
      released = true
      this.depth -= 1
      unlock()
      this.onChange()
    }
  }

  async awaitIdle(): Promise<void> {
    await this.currentTail
  }

  isIdle(): boolean {
    return this.depth === 0
  }
}

function verifyPersistedResult(
  state: RoomState,
  result: {
    duplicate: boolean
    persistedSequence: number
    sequence: number
  },
): void {
  const valid = result.duplicate
    ? (
        result.sequence <= result.persistedSequence
        && result.persistedSequence === state.authoritativeSequence
      )
    : (
        result.sequence === state.authoritativeSequence + 1
        && result.persistedSequence === result.sequence
      )
  if (!valid) {
    throw new CollaborationError('internal_consistency', {
      closeCode: CloseCodes.internalConsistency,
    })
  }
}
