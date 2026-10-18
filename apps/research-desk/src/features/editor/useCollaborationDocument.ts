import {
  HocuspocusProvider,
  HocuspocusProviderWebsocket,
  MessageType,
} from '@hocuspocus/provider'
import {
  COLLABORATION_CAUSAL_GAP_CLOSE,
  COLLABORATION_INGRESS_OVERFLOW_CLOSE,
  COLLABORATION_PRE_OPEN_QUEUE_FRAMES,
  EDITOR_COLLABORATION_PROTOCOL_VERSION,
  EDITOR_SCHEMA_VERSION,
  collaborationMergeByteCap,
  completeCreatedTextblocks,
  describeLocalEditorTransaction,
  describeLocalEditorUpdate,
  isCollaborationDurableAck,
  localBatchSealReason,
  mergeLocalBatchFacts,
  type CollaborationAccess,
  type CollaborationWriteStamp,
  type LocalBatchFacts,
  type LocalEditorTransactionFacts,
  type LocalUpdateClass,
} from '@inqtrix/editor-schema'
import { useEffect, useMemo, useState } from 'react'
import * as Y from 'yjs'

import {
  createEditorCollaborationSession,
  createGuestEditorCollaborationSession,
  type EditorCollaborationSession,
  type EditorCollaborationUser,
  type EditorGuestAccessSession,
  type InqtrixRequestError,
} from '@/api/inqtrixClient'
import type {
  EditorCollaborationConnectionStatus,
  EditorCollaborationDurabilityStatus,
  EditorDocumentRecord,
} from '@/features/project/types'
import type { CollaborationLiveAuthority } from './collaborationAuthority'
import {
  COLLABORATION_NOTICES,
  type CollaborationNotice,
} from './collaborationNotices'
import {
  collaborationCommandId,
  collaborationSha256Hex,
} from './collaborationCrypto'
import { messageFromError } from '@/api/requestErrorText'

export const COLLABORATION_UPDATE_BATCH_MS = 50
const COLLABORATION_ACK_DELAY_MS = 10_000
/** A connect attempt must bring the service's first message within this
 * long, or it is given up and retried on the reconnect ladder. The first
 * message answers the authentication, which the service gives after at most
 * one lease check bounded by its HTTP timeout (at most 30 seconds); the rest
 * covers the socket handshake and the gateway. */
export const COLLABORATION_CONNECT_ANSWER_TIMEOUT_MS = 35_000
/** A session request that has not answered within this long is abandoned
 * and counts as a failed attempt. */
export const COLLABORATION_SESSION_REQUEST_TIMEOUT_MS = 20_000
/** A socket that stayed synchronized this long proves the connection
 * stable: the reconnect ladder starts again from its first step. A durable
 * acknowledgement proves it at once. */
export const COLLABORATION_STABLE_CONNECTION_MS = 10_000

export const COLLABORATION_RECONNECT_DELAYS_MS = [
  1_000,
  2_000,
  4_000,
  8_000,
  15_000,
  30_000,
] as const
/** A released controller that still waits for acknowledgements fails its
 * navigation only after this long without any acknowledgement. */
const RELEASE_DURABILITY_TIMEOUT_MS = 60_000
const RELEASE_FAILURE_TTL_MS = 5 * 60_000
const MAX_RELEASE_FAILURES = 20
const MIN_PROVIDER_FLUSH_MS = 10
const MAX_PROVIDER_FLUSH_MS = 1_000
const LOCAL_TRANSPORT_ORIGIN = Object.freeze({ kind: 'local-collaboration-batch' })
const REMOTE_EDITOR_ORIGIN = Object.freeze({ kind: 'remote-collaboration-update' })

/** What becomes of the transport document's own cleanups: nothing more
 * (`echo`, the room ran the same one), a local update of their own
 * (`send`), or a part of the batch being applied (`batch`). */
type TransportFollowUpRoute = 'batch' | 'echo' | 'send'

/** The limits the collaboration service enforces on one socket. */
type CollaborationSendLimits = {
  frameLimitBytes: number
  maxQueuedBytes: number
  maxQueuedFrames: number
  updateRateCount: number
  updateRateWindowMs: number
}

/** The service's defaults, used only while a session does not publish its
 * limits (an older API during a rollout), with a warning. */
const DEFAULT_SEND_LIMITS: CollaborationSendLimits = Object.freeze({
  frameLimitBytes: 2 * 1024 * 1024,
  maxQueuedBytes: 8 * 1024 * 1024,
  maxQueuedFrames: 32,
  updateRateCount: 120,
  updateRateWindowMs: 10_000,
})

/** The session fields that publish the limits, with the ranges the service
 * validates them in. */
const SESSION_LIMIT_FIELDS = [
  { field: 'max_frame_bytes', key: 'frameLimitBytes', max: 16 * 1024 * 1024, min: 64 * 1024, scale: 1 },
  { field: 'max_queued_bytes', key: 'maxQueuedBytes', max: 64 * 1024 * 1024, min: 64 * 1024, scale: 1 },
  { field: 'max_queued_frames', key: 'maxQueuedFrames', max: 256, min: 1, scale: 1 },
  { field: 'update_rate_count', key: 'updateRateCount', max: 10_000, min: 1, scale: 1 },
  { field: 'update_rate_window_seconds', key: 'updateRateWindowMs', max: 60, min: 1, scale: 1_000 },
] as const

// Paced sends use at most four fifths of the update allowance, and at least
// one send per window. The rest leaves room for the sync answer and for what
// a page exit sends at once.
const PACED_ALLOWANCE_NUMERATOR = 4
const PACED_ALLOWANCE_DENOMINATOR = 5
// A page exit leaves this many updates of the allowance unused: the server
// may count the update in flight within the window although it was sent
// before it, and the sync answer counts once per socket.
const PAGE_EXIT_ALLOWANCE_MARGIN = 8
// Frames that share the service's queue with the updates a page exit sends,
// and that precede a replay on the next socket: the token, the sync request,
// the provider's own presence and one presence change or token refresh.
const CONTROL_FRAMES = 4
// Bytes kept free for those control frames next to the unconfirmed updates.
// The unconfirmed updates also bound the sync answer on the next socket,
// which carries every one of them the room is missing.
const CONTROL_FRAME_MARGIN_BYTES = 16 * 1024

export type CollaborationProviderEvents = {
  onAuthenticated: () => void
  onAuthenticationFailed: (reason: string) => void
  /** Every end of a socket or of its document connection that the
   * controller did not request itself, reported once per socket. */
  onClose: (code: number, reason: string) => void
  onStateless: (payload: string) => void
  onSynced: () => void
}

export type CollaborationProviderAdapter = {
  /** Whether the provider is applying the room's answer to its sync request
   * right now: every room change the transport lacks, merged into one
   * update. Any other room update it applies is one broadcast, which
   * carries one transaction the room ran. */
  applyingSyncAnswer: () => boolean
  /** Opens a new socket. The controller does not wait for it: the socket's
   * answer arrives as an event, and the controller bounds the attempt. */
  connect: () => Promise<void> | void
  destroy: () => void
  /** Closes the transport at once: no close event follows for it, and the
   * next `connect` opens a new socket. */
  disconnect: () => void
  /** Whether a socket exists, open or still opening. */
  hasSocket: () => boolean
  /** Whether the socket is open right now; a closing socket is not. */
  isSocketOpen: () => boolean
  /** Removes this editor's own presence and writes the removal on the open
   * socket at once, after everything written before. */
  leaveAwareness: () => void
  /** The controller just wrote a local document update on the socket, or
   * dropped waiting changes the transport already held. A change of the own
   * presence that waited for the local updates it may point into is written
   * now, directly behind the last update frame, once none of them remains
   * unsent. */
  localUpdatesWritten: () => void
  provider: HocuspocusProvider
  /** Brings back the own presence that leaveAwareness removed. */
  restoreAwareness: () => void
  sendStateless: (payload: string) => void
  /** The only path for local document updates to the server. */
  sendUpdate: (update: Uint8Array) => void
  /** While disabled, changes of the own presence wait. Enabling announces
   * the current presence with a new clock at once when the socket is open,
   * or as soon as no local update it may point into remains unsent. */
  setAwarenessForwarding: (enabled: boolean) => void
  setAwarenessUser: (user: EditorCollaborationUser) => void
  /** The identity of the socket the transport uses now, or used last. Every
   * socket it opens gets a new one. */
  socketId: () => number
  syncToken: () => Promise<void> | void
}

/** `beforeunload` is announced only while a leave guard is set. */
export type CollaborationPageExitReason = 'beforeunload' | 'hidden' | 'pagehide'

export type CollaborationPageLifecycleHandlers = {
  /** The page is about to unload, was hidden, or is being unloaded or
   * cached. */
  onExit: (reason: CollaborationPageExitReason) => void
  /** The page is shown again; `persisted` when it returns from the
   * back/forward cache. */
  onShow: (persisted: boolean) => void
}

export type CollaborationPageLifecycleRegistration = {
  dispose: () => void
  /** Adds the page's leave question with this guard, or removes it for
   * null. When the page is about to unload, the page exit runs first and
   * the browser then asks only when the guard returns true. Idempotent. */
  setLeaveGuard: (guard: (() => boolean) | null) => void
}

/** The write mode the editor enforces for local changes. */
export type CollaborationEditorWriteMode = 'comment' | 'edit' | 'suggest' | 'view'

export type CollaborationProviderFactoryOptions = {
  document: Y.Doc
  events: CollaborationProviderEvents
  getToken: () => string
  /** Whether local document updates of the editor are not yet written on
   * the socket. The own presence may point into them, so it waits while
   * they are. */
  holdsUnsentLocalUpdates: () => boolean
  /** Whether the provider may answer the room's sync request now. The answer
   * carries every change of `document` the room lacks, merged into one
   * update, and every deletion `document` holds; a refused answer is not
   * sent, and the caller says why it refused it. */
  mayAnswerSync: () => boolean
  room: string
  url: string
}

export type CollaborationDocumentHandle = {
  access: 'comment' | 'edit' | 'suggest' | 'view' | null
  activityRevision: number
  authorityRevision: number
  canEdit: boolean
  blockingFailure: CollaborationNotice | null
  commentEventVersion: number
  commentMentionEventVersion: number
  connectionStatus: EditorCollaborationConnectionStatus
  document: Y.Doc | null
  documentId: string | null
  /** The oldest unconfirmed local change has waited longer than the
   * acknowledgement delay; see COLLABORATION_NOTICES.ack_delayed. */
  durabilityDelayed: boolean
  durabilityStatus: EditorCollaborationDurabilityStatus
  /** Ends the write-mode report of `reporter`; a report of another editor
   * stays in force. */
  endLocalWriteMode: (reporter: object) => void
  error: CollaborationNotice | null
  generation: number | null
  hasUnconfirmedLocalChanges: boolean
  lastPersistedSequence: number
  lastLocalDurableSequence: number
  lifecycleStatus: 'connecting' | 'error' | 'inactive' | 'read_only' | 'reconnecting' | 'saved' | 'syncing'
  lifecycleKey: string
  /** The live Y.Doc may be shown: it completed a sync once, and neither a
   * release nor an access revocation followed. A reconnect, a durability
   * failure or an incompatibility keep it; the editor stays bound and
   * read-only and shows the local state those notices promise to retain. */
  liveDocumentReady: boolean
  /** The editor writing into `document` reports the write mode it enforces,
   * from its construction until its destruction. `reporter` identifies the
   * editor. */
  markLocalWriteMode: (mode: CollaborationEditorWriteMode, reporter: object) => void
  pendingHashes: readonly string[]
  provider: HocuspocusProvider | null
  readAuthority: () => CollaborationLiveAuthority
  reconnectAttempt: number
  recoverability: 'login' | 'none' | 'reload' | 'retry'
  /** Starts the document over from the service's state while
   * `updateRejected`, discarding the local state; does nothing otherwise. */
  reloadDocument: () => void
  retryConnection: () => Promise<void>
  nextReconnectAt: number | null
  synced: boolean
  /** When the oldest local change the service has not durably confirmed
   * yet was made, on the controller's clock; null while none waits. */
  unconfirmedSince: number | null
  /** The service refused a local update of this editor: the session sends
   * nothing more, and the local state stays shown as a backup until
   * `reloadDocument`. */
  updateRejected: boolean
  flushAndAwaitDurable: () => Promise<number>
  flushAndAwaitDurability: () => Promise<void>
  setAuthoritativeSequence: (sequence: number) => void
  updateAuthoritativeSequence: (sequence: number) => void
  user: EditorCollaborationUser | null
}

export type CollaborationDocumentControllerOptions = {
  documentId: string
  generation: number
  initialPersistedSequence: number
  /** Requests a session. The controller stops waiting and aborts `signal`
   * once the request did not answer within
   * COLLABORATION_SESSION_REQUEST_TIMEOUT_MS. */
  requestSession: (
    leaseToken?: string,
    rotationCommandId?: string,
    signal?: AbortSignal,
  ) => Promise<EditorCollaborationSession>
  resolveWebSocketUrl: (path: string) => string
  schemaVersion: number
}

export type CollaborationDocumentControllerDependencies = {
  cancelTimer: (timer: number) => void
  createDocument: () => Y.Doc
  createCommandId: () => string
  createProvider: (
    options: CollaborationProviderFactoryOptions,
  ) => CollaborationProviderAdapter
  hashUpdate: (update: Uint8Array) => Promise<string>
  now: () => number
  /** Observes the page lifecycle for the controller's page-exit flush. */
  observePageLifecycle: (
    handlers: CollaborationPageLifecycleHandlers,
  ) => CollaborationPageLifecycleRegistration
  random: () => number
  scheduleTimer: (callback: () => void, delayMs: number) => number
}

type RefreshMode = 'periodic' | 'reconnect'

/** A local update the server has not durably acknowledged yet. */
type UnconfirmedUpdate = {
  /** How the batch it was sent as groups with others. */
  class: LocalUpdateClass
  /** When its oldest local change entered the log. */
  enteredAt: number
  /** Known once its digest resolved; the durable acknowledgement names it. */
  hash: string | null
  /** The socket it was written on last; any other socket has to carry it
   * again before anything newer. */
  sentOn: number | null
  update: Uint8Array
}

/** Local editor updates that travel as one update once sent. */
type LocalBatch = {
  /** When its first update entered the log. */
  enteredAt: number
  facts: LocalBatchFacts
  /** The members merged, computed when needed and dropped on every append. */
  merged: Uint8Array | null
  updates: Uint8Array[]
}

type ConfiguredCollaborationSession = EditorCollaborationSession & {
  provider_flush_ms?: number
  refresh_after?: number
}

type LifecycleRegistryEntry = {
  controller: CollaborationDocumentController
  identity: string | null
  lingerSince: number | null
  lingerTimer: ReturnType<typeof setTimeout> | null
  references: number
}

/** How long a fully released lifecycle keeps its live session before the
 * real teardown. Re-entering the editor within this window retains the same
 * controller — no session POST, no websocket rebuild, no resync — which is
 * what keeps view switching from burning the server's per-user-per-document
 * lease budget (5 active leases, 60s TTL). The rotation timer keeps the lease
 * fresh for the whole window because release() is deferred, not softened. */
export const LIFECYCLE_LINGER_MS = 180_000
/** Upper bound on lingering documents; each holds a Y.Doc and an open
 * websocket, so the oldest is torn down when a fourth starts lingering. */
export const MAX_LINGERING_LIFECYCLES = 3

const lifecycleRegistry = new Map<string, LifecycleRegistryEntry>()
const lifecycleFailures = new Map<string, { error: CollaborationNotice; expiresAt: number }>()

/** A lifecycle of a document generation that was replaced while it held
 * local changes the service can no longer confirm. */
export type ReplacedGenerationRecovery = {
  /** The editor document holding those changes; read it during the call. */
  document: Y.Doc
  documentId: string
  generation: number
  /** What the lifecycle's registry key names before the document: the
   * workspace, or the guest identity of a guest page. */
  scope: string
}

/** Keeps the local state of a replaced generation; true once it is kept. */
export type ReplacedGenerationRecoveryStore = (
  recovery: ReplacedGenerationRecovery,
) => boolean

let replacedGenerationRecoveryStore: ReplacedGenerationRecoveryStore | null = null
/** The newest generation known for each document, by the registry key up to
 * the generation number. */
const newestKnownGenerations = new Map<string, number>()

/** Flush every retained collaboration document before an identity boundary.
 *
 * The registry is the lifecycle authority shared by editor surfaces. Taking a
 * controller snapshot prevents React cleanup from changing the iteration while
 * acknowledgements are in flight; a failed durability boundary rejects the
 * caller so logout can leave the authenticated session intact.
 */
export async function flushActiveCollaborationDocuments(): Promise<void> {
  const controllers = new Set(
    [...lifecycleRegistry.values()].map((entry) => entry.controller),
  )
  await Promise.all(
    [...controllers].map((controller) => controller.flushAndAwaitDurability()),
  )
}

export class CollaborationDocumentController {
  private readonly dependencies: CollaborationDocumentControllerDependencies
  private readonly document: Y.Doc
  private readonly transportDocument: Y.Doc
  private readonly listeners = new Set<(state: CollaborationDocumentHandle) => void>()
  private readonly documentReloadListeners = new Set<() => void>()
  private readonly options: CollaborationDocumentControllerOptions
  /** Unconfirmed local updates in the order they were produced. Each socket
   * carries them in this order before any newer update, so every update the
   * server receives has its predecessors in the room or earlier on the same
   * socket. Outside a page exit at most one of them waits for its
   * acknowledgement on the current socket. */
  private readonly unconfirmedUpdates: UnconfirmedUpdate[] = []
  /** Local batches after the unconfirmed updates, in production order, each
   * closed before an update the service must not receive merged with it. */
  private readonly sealedBatches: LocalBatch[] = []
  private readonly earlyAcks = new Set<string>()
  private readonly durabilityWaiters = new Set<{
    reject: (error: Error) => void
    resolve: () => void
  }>()
  /** Facts of the editor document's local transactions, recorded before
   * Yjs removes their deleted content and read by the update they emit. */
  private readonly transactionFacts = new WeakMap<Y.Transaction, LocalEditorTransactionFacts>()
  /** When the current socket sent each update within the rate window. */
  private readonly socketSendTimes: number[] = []
  /** The batch local updates join until it is sealed. */
  private openBatch: LocalBatch | null = null
  /** The controller is applying a remote update to the editor document. */
  private applyingRemoteUpdate = false
  /** The controller is applying a local batch to the transport document. */
  private applyingBatch = false
  /** Set while the controller applies a remote update or a local batch to
   * the transport document, until the transport settled. */
  private collectingTransportFollowUps = false
  /** What becomes of the transport document's own transactions once it
   * settled, see handleTransportSettled. */
  private transportFollowUpRoute: TransportFollowUpRoute = 'echo'
  /** The transport document's own transactions (formatting cleanups) while
   * it applies an update, forwarded to the editor document once it settled. */
  private readonly transportFollowUps: Uint8Array[] = []
  /** The transport's cleanups after the batch being applied to it; they
   * travel in the same update as the batch. */
  private readonly batchFollowUps: Uint8Array[] = []
  /** Formatting cleanups the editor document ran while the transport was
   * settling, judged once it settled. */
  private readonly heldEditorCleanups: Array<{
    deleted: DeletedRanges
    facts: LocalEditorTransactionFacts
    update: Uint8Array
  }> = []
  private awarenessUserKey: string | null = null
  private authenticated = false
  private blockingFailure: CollaborationNotice | null = null
  /** A causal-gap recovery ran and no durable acknowledgement followed yet. */
  private causalGapRecoveryPending = false
  /** Counts the connect attempts; a late failure of an older one is ignored. */
  private connectAttempt = 0
  /** Runs out when the current connect attempt brought no answer in time. */
  private connectDeadlineTimer: number | null = null
  /** The unconfirmed change the acknowledgement-delay timer watches. */
  private delayReference: number | null = null
  private delayTimer: number | null = null
  private destroyed = false
  private durabilityFailed = false
  private editorInputAttached = true
  /** An ingress-overflow reconnect ran and no durable acknowledgement
   * followed yet: the reconnect ladder keeps its attempt count. */
  private ingressOverflowRecovery = false
  private leaseToken: string | null = null
  private limitsWarned = false
  private missingFactsWarned = false
  private nestedLocalUpdateWarned = false
  /** The socket that replayed the unconfirmed updates after it
   * authenticated and now carries new local updates directly; null while
   * none does. Bound to the socket's identity, so a socket that replaced
   * another one without a reported close never inherits it. */
  private orderedSocket: number | null = null
  private pageLifecycle: CollaborationPageLifecycleRegistration | null = null
  private providerAdapter: CollaborationProviderAdapter | null = null
  /** The idle delay or the pacing interval before the next send. */
  private pumpTimer: number | null = null
  private sendLimits: CollaborationSendLimits = DEFAULT_SEND_LIMITS
  /** The write mode last reported by the editor that writes into the
   * editor document; `edit` before any report and after it ended. */
  private writeStamp: CollaborationWriteStamp = 'edit'
  private writeModeReporter: object | null = null
  private onReleaseFailed: ((error: CollaborationNotice) => void) | null = null
  private onReleasedSettled: (() => void) | null = null
  private providerFlushMs = COLLABORATION_UPDATE_BATCH_MS
  private reconnectTimer: number | null = null
  private released = false
  private releaseTimer: number | null = null
  private refreshInFlight: Promise<void> | null = null
  private refreshTimer: number | null = null
  private room: string | null = null
  private rotationCommandId: string | null = null
  private retryInFlight: Promise<void> | null = null
  /** Runs out when a reconnected socket stayed synchronized long enough to
   * count as stable. */
  private stabilityTimer: number | null = null
  private started = false
  private synced = false
  /** A sync completed once; the live document is then worth showing. */
  private syncedOnce = false
  private accessRevoked = false
  private state: CollaborationDocumentHandle

  constructor(
    options: CollaborationDocumentControllerOptions,
    dependencies: CollaborationDocumentControllerDependencies = browserDependencies,
  ) {
    this.options = options
    this.dependencies = dependencies
    this.document = dependencies.createDocument()
    this.transportDocument = dependencies.createDocument()
    this.state = {
      access: null,
      activityRevision: 0,
      authorityRevision: 0,
      blockingFailure: null,
      canEdit: false,
      commentEventVersion: 0,
      commentMentionEventVersion: 0,
      connectionStatus: 'inactive',
      document: this.document,
      documentId: options.documentId,
      durabilityDelayed: false,
      durabilityStatus: 'idle',
      endLocalWriteMode: this.endLocalWriteMode,
      error: null,
      flushAndAwaitDurable: this.flushAndAwaitDurable,
      flushAndAwaitDurability: this.flushAndAwaitDurability,
      lastPersistedSequence: options.initialPersistedSequence,
      lastLocalDurableSequence: 0,
      generation: options.generation,
      hasUnconfirmedLocalChanges: false,
      lifecycleStatus: 'inactive',
      lifecycleKey: `${options.documentId}:g${options.generation}:${this.document.clientID}`,
      liveDocumentReady: false,
      markLocalWriteMode: this.markLocalWriteMode,
      pendingHashes: [],
      provider: null,
      readAuthority: this.readAuthority,
      reconnectAttempt: 0,
      recoverability: 'none',
      reloadDocument: this.reloadDocument,
      retryConnection: this.retryConnection,
      nextReconnectAt: null,
      setAuthoritativeSequence: this.updateAuthoritativeSequence,
      synced: false,
      unconfirmedSince: null,
      updateAuthoritativeSequence: this.updateAuthoritativeSequence,
      updateRejected: false,
      user: null,
    }
    this.document.on('afterTransaction', this.handleEditorAfterTransaction)
    this.document.on('update', this.handleEditorDocumentUpdate)
    this.transportDocument.on('update', this.handleTransportDocumentUpdate)
    this.transportDocument.on('afterAllTransactions', this.handleTransportSettled)
  }

  getSnapshot(): CollaborationDocumentHandle {
    return this.state
  }

  subscribe(listener: (state: CollaborationDocumentHandle) => void): () => void {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  readonly readAuthority = (): CollaborationLiveAuthority => ({
    access: this.state.access,
    blockingFailure: this.state.blockingFailure,
    canEdit: this.state.canEdit,
    connectionStatus: this.state.connectionStatus,
    documentId: this.state.documentId,
    generation: this.state.generation,
    lifecycleStatus: this.state.lifecycleStatus,
    revision: this.state.authorityRevision,
    synced: this.state.synced,
  })

  readonly updateAuthoritativeSequence = (sequence: number): void => {
    if (!Number.isSafeInteger(sequence) || sequence < 0) {
      throw new Error('The authoritative collaboration sequence is invalid.')
    }
    if (sequence <= this.state.lastPersistedSequence) return
    this.publish({
      activityRevision: this.state.activityRevision + 1,
      lastPersistedSequence: sequence,
    })
  }

  readonly markLocalWriteMode = (
    mode: CollaborationEditorWriteMode,
    reporter: object,
  ): void => {
    // A change of mode needs no seal of its own: the shared grouping rule
    // seals before the first update whose class the new mode changes.
    this.writeModeReporter = reporter
    this.writeStamp = mode === 'suggest' ? 'suggest' : 'edit'
  }

  readonly endLocalWriteMode = (reporter: object): void => {
    // An older editor is destroyed after the newer one reported; its end
    // must not reset the newer report.
    if (this.writeModeReporter !== reporter) return
    this.writeModeReporter = null
    this.writeStamp = 'edit'
  }

  readonly flushAndAwaitDurability = async (): Promise<void> => {
    if (this.destroyed) {
      throw new Error('The collaboration controller has already been disposed.')
    }
    this.flushScheduledBatch()
    if (this.durabilityFailed || this.blockingFailure !== null) {
      throw new Error(
        this.blockingFailure
          ?? this.state.error
          ?? 'Collaboration durability is unavailable.',
      )
    }
    if (!this.hasPendingDurability()) return
    await new Promise<void>((resolve, reject) => {
      this.durabilityWaiters.add({ reject, resolve })
    })
  }

  readonly flushAndAwaitDurable = async (): Promise<number> => {
    await this.flushAndAwaitDurability()
    return this.state.lastLocalDurableSequence
  }

  readonly retryConnection = async (): Promise<void> => {
    if (this.retryInFlight) return this.retryInFlight
    if (
      this.destroyed
      || this.state.recoverability !== 'retry'
      || this.durabilityFailed
    ) return
    const retry = this.performManualRetry().finally(() => {
      if (this.retryInFlight === retry) this.retryInFlight = null
    })
    this.retryInFlight = retry
    return retry
  }

  retain(): void {
    if (this.destroyed || !this.released) return
    this.released = false
    this.onReleaseFailed = null
    this.onReleasedSettled = null
    this.clearReleaseTimer()
    if (!this.durabilityFailed && this.blockingFailure === null) {
      this.attachEditorInput()
    }
    const canEdit = this.authenticated
      && this.synced
      && this.blockingFailure === null
      && (
        this.state.access === 'comment'
        || this.state.access === 'edit'
        || this.state.access === 'suggest'
      )
    this.publish({
      canEdit,
      liveDocumentReady: this.syncedOnce && !this.accessRevoked,
      synced: this.authenticated
        && this.synced
        && this.blockingFailure === null,
    })
    this.publishReadiness()
  }

  async start(): Promise<void> {
    if (this.started || this.destroyed) return
    this.started = true
    if (this.options.schemaVersion !== EDITOR_SCHEMA_VERSION) {
      this.enterIncompatible(COLLABORATION_NOTICES.schema_unsupported)
      return
    }
    // Every controller observes the page itself, lingering ones included,
    // and before its provider: the provider removes the own presence on its
    // own page-hide listener, too late to reach the socket.
    this.pageLifecycle ??= this.dependencies.observePageLifecycle({
      onExit: this.handlePageExit,
      onShow: this.handlePageShow,
    })
    this.publish({
      connectionStatus: 'connecting',
      error: null,
      lifecycleStatus: 'connecting',
    })
    try {
      const session = await this.boundedSessionRequest()
      if (!this.applySession(session, true)) return
      const providerAdapter = this.dependencies.createProvider({
        document: this.transportDocument,
        events: {
          onAuthenticated: this.handleAuthenticated,
          onAuthenticationFailed: this.handleAuthenticationFailed,
          onClose: this.handleClose,
          onStateless: this.handleStateless,
          onSynced: this.handleSynced,
        },
        getToken: () => this.leaseToken ?? '',
        holdsUnsentLocalUpdates: this.holdsUnsentLocalUpdates,
        mayAnswerSync: this.mayAnswerSync,
        room: session.room,
        url: this.options.resolveWebSocketUrl(session.websocket_path),
      })
      this.providerAdapter = providerAdapter
      // The own presence travels once the document synchronized: until the
      // room has loaded, every frame counts against the service's queue.
      providerAdapter.setAwarenessForwarding(false)
      this.setAwarenessUser(session.user)
      this.publish({ provider: providerAdapter.provider })
      this.openTransport()
    } catch (error) {
      this.handleSessionFailure(error, this.providerAdapter !== null)
    }
  }

  /** Rotates the lease. One session request at a time: a refresh asked for
   * while one is in flight joins it. The refresh ends by opening a socket
   * whenever none exists, whichever mode started it, so a reconnect that
   * joined a periodic refresh still connects. */
  async refreshLease(mode: RefreshMode = 'periodic'): Promise<void> {
    if (this.refreshInFlight) return this.refreshInFlight
    if (this.destroyed || !this.leaseToken) return
    const refresh = this.performRefresh(mode).finally(() => {
      if (this.refreshInFlight === refresh) this.refreshInFlight = null
    })
    this.refreshInFlight = refresh
    return refresh
  }

  /** Close the batch local edits are joining and send what the log allows
   * now, without releasing. The linger path calls this at view exit: the
   * page and its socket stay, so the log keeps draining one update per
   * acknowledgement while the deferred teardown window runs. */
  flushPendingLocalUpdates(): void {
    if (this.destroyed) return
    this.flushScheduledBatch()
  }

  release(
    onSettled?: () => void,
    onFailed?: (error: CollaborationNotice) => void,
  ): void {
    if (this.destroyed || this.released) return
    this.flushScheduledBatch()
    this.detachEditorInput()
    this.released = true
    this.onReleasedSettled = onSettled ?? null
    this.onReleaseFailed = onFailed ?? null
    this.publish({ canEdit: false, liveDocumentReady: false, synced: false })
    if (!this.hasPendingDurability() && !this.durabilityFailed) {
      this.finalizeDestroy()
      return
    }
    if (this.durabilityFailed) {
      this.finalizeFailedRelease(
        this.state.error ?? COLLABORATION_NOTICES.durability_failed_before_navigation,
      )
      return
    }
    this.scheduleReleaseTimeout()
  }

  destroy(): void {
    this.release()
  }

  /**
   * Permanently dispose a lifecycle after its unconfirmed local state was
   * copied into a separate recovery artifact. This is intentionally distinct
   * from normal release, which waits for durable acknowledgement and retains a
   * failed controller for retry.
   */
  discardAfterRecoveryCapture(): void {
    this.finalizeDestroy()
  }

  /** Local changes remain that the service will never confirm: it refused
   * them, the session ended incompatibly, or the access changed while they
   * were unconfirmed. Only a recovery copy can keep them. */
  holdsUnconfirmableLocalChanges(): boolean {
    return !this.destroyed && this.durabilityFailed && this.hasPendingDurability()
  }

  private finalizeDestroy(): void {
    if (this.destroyed) return
    const onSettled = this.onReleasedSettled
    this.onReleaseFailed = null
    this.onReleasedSettled = null
    this.destroyed = true
    this.authenticated = false
    this.synced = false
    this.clearRefreshTimer()
    this.clearReconnectTimer()
    this.clearReleaseTimer()
    this.clearDelayTimer()
    this.clearConnectDeadline()
    this.clearStabilityTimer()
    this.clearLocalUpdateBatch()
    this.detachEditorInput()
    this.pageLifecycle?.dispose()
    this.pageLifecycle = null
    this.transportDocument.off('update', this.handleTransportDocumentUpdate)
    this.transportDocument.off('afterAllTransactions', this.handleTransportSettled)
    this.transportFollowUps.splice(0)
    this.batchFollowUps.splice(0)
    this.heldEditorCleanups.splice(0)
    this.providerAdapter?.destroy()
    this.providerAdapter = null
    this.document.destroy()
    this.transportDocument.destroy()
    this.listeners.clear()
    this.unconfirmedUpdates.splice(0)
    this.earlyAcks.clear()
    this.rejectDurabilityWaiters(
      new Error('The collaboration controller was disposed.'),
    )
    onSettled?.()
  }

  private readonly performRefresh = async (mode: RefreshMode): Promise<void> => {
    const currentToken = this.leaseToken
    if (!currentToken || this.destroyed) return
    this.clearRefreshTimer()
    if (mode === 'reconnect') {
      this.publish({
        canEdit: false,
        connectionStatus: 'reconnecting',
        error: null,
        lifecycleStatus: 'reconnecting',
        nextReconnectAt: null,
        recoverability: 'retry',
      })
    } else {
      // Hocuspocus token sync is deliberately one-way: sendToken() resolves
      // after sending and the server updates the connection context without a
      // second "authenticated" response. Keep the already-authenticated,
      // already-synced transport ready while its lease rotates. Any HTTP,
      // send, or later server-close failure still enters the fail-closed
      // reconnect path below.
      this.publish({ error: null })
    }
    try {
      const rotationCommandId = this.rotationCommandId
        ?? this.dependencies.createCommandId()
      this.rotationCommandId = rotationCommandId
      const session = await this.boundedSessionRequest(
        currentToken,
        rotationCommandId,
      )
      if (!this.applySession(session, false)) return
      this.rotationCommandId = null
      await this.finishRefresh()
    } catch (error) {
      const status = requestStatus(error)
      if (
        (status === 401 || status === 403 || status === 404)
        && !this.destroyed
      ) {
        // A policy event invalidates the old lease for both a real revocation
        // and a still-authorized permission downgrade. Re-issuing once through
        // the cookie-authenticated session boundary is the only trustworthy
        // way to distinguish those cases: a downgrade returns a fresh
        // read-only lease, while a revocation remains a non-disclosing 404.
        this.rotationCommandId = null
        try {
          const session = await this.boundedSessionRequest()
          if (!this.applySession(session, false)) return
          await this.finishRefresh()
          return
        } catch (recoveryError) {
          this.handleSessionFailure(recoveryError, true)
          return
        }
      }
      this.handleSessionFailure(error, true)
    }
  }

  /** Ends a refresh whose session applied: without a socket it opens one
   * with the new lease; an existing socket, open or opening, receives the
   * new token, and the editor is ready again if the socket is. */
  private async finishRefresh(): Promise<void> {
    const providerAdapter = this.providerAdapter
    if (!providerAdapter || this.destroyed) return
    if (!providerAdapter.hasSocket()) {
      this.openTransport()
      return
    }
    await providerAdapter.syncToken()
    this.publishReadiness()
  }

  /** One session request, bounded on the controller's clock: after
   * COLLABORATION_SESSION_REQUEST_TIMEOUT_MS its signal is aborted and the
   * controller stops waiting for it, whether or not the request observes
   * the signal. The failure carries no status and counts as transient. */
  private boundedSessionRequest(
    leaseToken?: string,
    rotationCommandId?: string,
  ): Promise<EditorCollaborationSession> {
    const abort = new AbortController()
    return new Promise<EditorCollaborationSession>((resolve, reject) => {
      let settled = false
      const timer = this.dependencies.scheduleTimer(() => {
        if (settled) return
        settled = true
        abort.abort()
        if (!this.destroyed) {
          console.warn(
            'The collaboration session request did not answer within '
            + `${COLLABORATION_SESSION_REQUEST_TIMEOUT_MS / 1_000} seconds and was abandoned.`,
          )
        }
        reject(new Error(COLLABORATION_NOTICES.connection_interrupted))
      }, COLLABORATION_SESSION_REQUEST_TIMEOUT_MS)
      const settle = (): boolean => {
        if (settled) return false
        settled = true
        this.dependencies.cancelTimer(timer)
        return true
      }
      let request: Promise<EditorCollaborationSession>
      try {
        request = this.options.requestSession(leaseToken, rotationCommandId, abort.signal)
      } catch (error) {
        request = Promise.reject(error)
      }
      request.then(
        (session) => {
          if (settle()) resolve(session)
        },
        (error: unknown) => {
          if (settle()) reject(error)
        },
      )
    })
  }

  /** Opens the next socket without waiting for it and bounds the attempt:
   * it has to bring the service's first message within
   * COLLABORATION_CONNECT_ANSWER_TIMEOUT_MS. Opening a socket also ends a
   * reconnect that was still scheduled, so the new socket is not replaced
   * right away. Never called while a socket exists: the provider would drop
   * that socket without any event. */
  private openTransport(): void {
    const providerAdapter = this.providerAdapter
    if (!providerAdapter || this.destroyed) return
    if (providerAdapter.hasSocket()) {
      console.warn('A collaboration socket was not opened because one already exists.')
      return
    }
    if (this.reconnectTimer !== null) {
      this.clearReconnectTimer()
      this.publish({ nextReconnectAt: null })
    }
    this.clearConnectDeadline()
    this.connectAttempt += 1
    const attempt = this.connectAttempt
    this.connectDeadlineTimer = this.dependencies.scheduleTimer(
      this.handleConnectDeadline,
      COLLABORATION_CONNECT_ANSWER_TIMEOUT_MS,
    )
    let connecting: Promise<void> | void
    try {
      connecting = providerAdapter.connect()
    } catch (error) {
      this.handleConnectFailure(attempt, error)
      return
    }
    void Promise.resolve(connecting).catch((error: unknown) => {
      this.handleConnectFailure(attempt, error)
    })
  }

  private handleConnectFailure(attempt: number, error: unknown): void {
    if (this.destroyed || attempt !== this.connectAttempt) return
    console.warn(`A collaboration connect attempt failed: ${messageFromError(error)}`)
    // Only an attempt that is still waiting for its answer is replaced here;
    // one that already answered reports its end as a close.
    if (this.connectDeadlineTimer === null) return
    this.enterReconnect(COLLABORATION_NOTICES.connection_interrupted)
  }

  private readonly handleConnectDeadline = (): void => {
    this.connectDeadlineTimer = null
    if (this.destroyed || this.durabilityFailed) return
    console.warn(
      'The collaboration service did not answer a new connection within '
      + `${COLLABORATION_CONNECT_ANSWER_TIMEOUT_MS / 1_000} seconds; reconnecting.`,
    )
    this.enterReconnect(COLLABORATION_NOTICES.connection_interrupted)
  }

  private clearConnectDeadline(): void {
    if (this.connectDeadlineTimer === null) return
    this.dependencies.cancelTimer(this.connectDeadlineTimer)
    this.connectDeadlineTimer = null
  }

  private readonly performManualRetry = async (): Promise<void> => {
    this.clearReconnectTimer()
    this.publish({
      canEdit: false,
      connectionStatus: 'reconnecting',
      error: null,
      lifecycleStatus: 'reconnecting',
      nextReconnectAt: null,
      recoverability: 'retry',
      synced: false,
    })
    this.disconnectTransport()
    if (this.leaseToken && this.providerAdapter) {
      await this.refreshLease('reconnect')
      return
    }
    this.started = false
    await this.start()
  }

  private applySession(
    session: EditorCollaborationSession,
    initial: boolean,
  ): boolean {
    if (this.destroyed || this.durabilityFailed) return false
    if (
      session.protocol_version !== EDITOR_COLLABORATION_PROTOCOL_VERSION
      || session.schema_version !== this.options.schemaVersion
      || !session.lease_token
      || !session.room
      || !session.websocket_path
    ) {
      this.enterIncompatible(COLLABORATION_NOTICES.protocol_incompatible)
      return false
    }
    const configuredSession = session as ConfiguredCollaborationSession
    const providerFlushMs = configuredSession.provider_flush_ms
    const refreshAfter = configuredSession.refresh_after
    if (
      providerFlushMs !== undefined
      && (!Number.isInteger(providerFlushMs)
        || providerFlushMs < MIN_PROVIDER_FLUSH_MS
        || providerFlushMs > MAX_PROVIDER_FLUSH_MS)
    ) {
      this.enterIncompatible(COLLABORATION_NOTICES.flush_interval_incompatible)
      return false
    }
    if (
      refreshAfter !== undefined
      && (!Number.isFinite(refreshAfter)
        || refreshAfter * 1_000 <= this.dependencies.now()
        || refreshAfter >= session.expires_at)
    ) {
      this.enterIncompatible(COLLABORATION_NOTICES.refresh_interval_incompatible)
      return false
    }
    const sendLimits = this.sessionSendLimits(session)
    if (sendLimits === null) {
      this.enterIncompatible(COLLABORATION_NOTICES.limits_incompatible)
      return false
    }
    if (!initial && this.room !== session.room) {
      this.enterIncompatible(COLLABORATION_NOTICES.room_changed)
      return false
    }
    if (this.state.user && this.state.user.id !== session.user.id) {
      this.enterAccessRevoked(COLLABORATION_NOTICES.identity_changed)
      return false
    }
    // A lower access under which some unconfirmed change cannot be stored:
    // any change under view access, a change made in edit mode under suggest
    // access. Sending it would only earn a refusal.
    const lowered = (
      this.state.access !== 'view'
      && session.access === 'view'
    ) || (
      this.state.access === 'edit'
      && session.access === 'suggest'
      && this.holdsNonSuggestionChanges()
    )
    if (!initial && lowered && this.hasUnreconciledUpdates()) {
      this.enterAccessChangeWithUnconfirmedUpdates()
      return false
    }
    this.room = session.room
    this.leaseToken = session.lease_token
    this.sendLimits = sendLimits
    this.providerFlushMs = providerFlushMs ?? COLLABORATION_UPDATE_BATCH_MS
    this.scheduleRefresh(session.expires_at, refreshAfter)
    const preserveCanEdit = (
      !initial
      && this.state.canEdit
      && (
        session.access === 'comment'
        || session.access === 'edit'
        || session.access === 'suggest'
      )
    )
    this.publish({
      access: session.access,
      canEdit: preserveCanEdit,
      connectionStatus: initial ? 'connecting' : this.state.connectionStatus,
      error: null,
      lifecycleStatus: initial ? 'connecting' : this.state.lifecycleStatus,
      user: deterministicUser(session.user),
    })
    return true
  }

  /** The limits the session publishes, the service defaults for absent ones
   * (with one warning per controller), or null when a published value lies
   * outside the range the service accepts. */
  private sessionSendLimits(
    session: EditorCollaborationSession,
  ): CollaborationSendLimits | null {
    const limits: CollaborationSendLimits = { ...DEFAULT_SEND_LIMITS }
    const absent: string[] = []
    for (const { field, key, max, min, scale } of SESSION_LIMIT_FIELDS) {
      const value = session[field]
      if (value === undefined) {
        absent.push(field)
        continue
      }
      if (!Number.isInteger(value) || value < min || value > max) return null
      limits[key] = value * scale
    }
    if (absent.length > 0 && !this.limitsWarned) {
      this.limitsWarned = true
      console.warn(
        `The collaboration session does not publish ${absent.join(', ')}; `
        + 'the service defaults apply.',
      )
    }
    return limits
  }

  private holdsNonSuggestionChanges(): boolean {
    return this.unconfirmedUpdates.some((entry) => entry.class !== 'suggestion')
      || this.sealedBatches.some((batch) => batch.facts.class !== 'suggestion')
      || (this.openBatch !== null && this.openBatch.facts.class !== 'suggestion')
  }

  private readonly handleAuthenticated = (): void => {
    if (this.destroyed || this.durabilityFailed) return
    this.clearConnectDeadline()
    this.authenticated = true
    if (this.state.user) {
      this.setAwarenessUser(this.state.user)
    }
    this.publish({
      canEdit: false,
      connectionStatus: this.synced
        ? this.readyConnectionStatus()
        : 'connecting',
      error: null,
      lifecycleStatus: this.synced
        ? this.readyLifecycleStatus()
        : 'syncing',
      synced: this.synced,
    })
    this.replayUnconfirmedUpdates()
    this.publishReadiness()
  }

  private readonly handleSynced = (): void => {
    if (this.destroyed || this.durabilityFailed) return
    this.synced = true
    this.providerAdapter?.setAwarenessForwarding(true)
    this.publishReadiness()
    this.watchConnectionStability()
  }

  /** After a reconnect the ladder keeps its count until the connection
   * proves stable: a durable acknowledgement, or this socket staying
   * synchronized for COLLABORATION_STABLE_CONNECTION_MS. A socket that
   * closes right after its sync thus reconnects later each time instead of
   * once per second. After an ingress overflow only an acknowledgement
   * counts. */
  private watchConnectionStability(): void {
    this.clearStabilityTimer()
    if (
      this.state.reconnectAttempt === 0
      || this.ingressOverflowRecovery
      || !this.providerAdapter
    ) return
    const socket = this.providerAdapter.socketId()
    this.stabilityTimer = this.dependencies.scheduleTimer(() => {
      this.stabilityTimer = null
      if (
        this.destroyed
        || !this.synced
        || this.providerAdapter?.socketId() !== socket
      ) return
      this.resetReconnectLadder()
    }, COLLABORATION_STABLE_CONNECTION_MS)
  }

  private resetReconnectLadder(): void {
    this.clearStabilityTimer()
    if (this.state.reconnectAttempt !== 0) this.publish({ reconnectAttempt: 0 })
  }

  private clearStabilityTimer(): void {
    if (this.stabilityTimer === null) return
    this.dependencies.cancelTimer(this.stabilityTimer)
    this.stabilityTimer = null
  }

  private readonly handleAuthenticationFailed = (reason: string): void => {
    if (this.destroyed) return
    this.clearConnectDeadline()
    this.authenticated = false
    this.synced = false
    if (isCompatibilityReason(reason)) {
      this.enterIncompatible(COLLABORATION_NOTICES.protocol_incompatible)
      return
    }
    if (reason.includes('access_revoked')) {
      this.revalidateAccess()
      return
    }
    this.enterReconnect(COLLABORATION_NOTICES.authentication_failed)
  }

  private readonly handleClose = (code: number, reason: string): void => {
    if (this.destroyed) return
    this.clearConnectDeadline()
    this.authenticated = false
    this.synced = false
    this.markSocketUnordered()
    // The reason decides and the code only confirms. A close the server sends
    // in band reaches the provider with its code rewritten to 1000 and only
    // the reason preserved, so branching on the code first is blind to every
    // close the collaboration service itself initiates.
    if (reason.includes(COLLABORATION_CAUSAL_GAP_CLOSE.reason)) {
      // The server is missing earlier updates of this editor; the ordered
      // replay on the next socket supplies them.
      this.recoverFromCausalGap()
      return
    }
    if (reason.includes(COLLABORATION_INGRESS_OVERFLOW_CLOSE.reason)) {
      // The service held back more of this editor's frames than it allows;
      // the next socket carries the unconfirmed updates first. The ladder
      // keeps counting until an acknowledgement shows progress.
      this.ingressOverflowRecovery = true
      this.enterReconnect(COLLABORATION_NOTICES.ingress_overflow_reconnecting)
      return
    }
    const rejection = rejectedUpdateNotice(code, reason)
    if (rejection !== null) {
      // A rejected update stays rejected: resending the same local state
      // cannot succeed. The editor keeps that state as a backup until the
      // user reloads the document.
      console.warn(`The collaboration service rejected a local update (${code} ${reason}).`)
      this.enterUpdateRejected(rejection)
      return
    }
    // Code 4409 carries both the real compatibility reasons and every other
    // 409 of the internal API, for example a conflict when two users decide
    // the same suggestion. The session ends in both cases; only the
    // explanation must not be invented.
    const versionMismatch = isCompatibilityReason(reason)
    if (versionMismatch || code === 4409) {
      this.enterIncompatible(versionMismatch
        ? COLLABORATION_NOTICES.protocol_incompatible
        : COLLABORATION_NOTICES.session_ended)
      return
    }
    if (reason.includes('origin_rejected')) {
      // A transport misconfiguration, not a permission change: the address the
      // browser reached is not the one the server publishes as its public
      // origin. Terminal like any other incompatibility, but it must not be
      // reported as a revoked authorization or the operator searches the
      // sharing settings instead of the base URL.
      this.enterOriginRejected(COLLABORATION_NOTICES.origin_rejected)
      return
    }
    if (reason.includes('access_revoked') || code === 4403) {
      // Revalidate rather than declare: the authoritative revocation comes
      // from the access refresh answering 403/404, not from a close frame.
      this.revalidateAccess()
      return
    }
    this.enterReconnect(COLLABORATION_NOTICES.connection_interrupted)
  }

  private readonly handleStateless = (payload: string): void => {
    if (this.destroyed) return
    let message: unknown
    try {
      message = JSON.parse(payload)
    } catch {
      return
    }
    if (isCollaborationDurableAck(message)) {
      if (this.durabilityFailed) return
      const acknowledged = this.unconfirmedUpdates.findIndex(
        (entry) => entry.hash === message.hash,
      )
      if (acknowledged >= 0) {
        this.unconfirmedUpdates.splice(acknowledged, 1)
        this.recordReleaseProgress()
      } else if (this.hasHashesInFlight()) {
        this.earlyAcks.add(message.hash)
      }
      // Durable progress: a later causal gap may be recovered again.
      this.causalGapRecoveryPending = false
      const lastLocalDurableSequence = Math.max(
        this.state.lastLocalDurableSequence,
        message.sequence,
      )
      this.publish({
        ...(lastLocalDurableSequence > this.state.lastLocalDurableSequence
          ? { activityRevision: this.state.activityRevision + 1 }
          : {}),
        durabilityStatus: this.durabilityFailed
          ? 'error'
          : this.hasPendingDurability() ? 'pending' : 'saved',
        lastLocalDurableSequence,
      })
      // Durable progress on this socket proves the connection stable.
      this.ingressOverflowRecovery = false
      this.resetReconnectLadder()
      // The acknowledgement paces the next send.
      this.pumpSend()
      return
    }
    if (
      isCollaborationCommentEvent(message, this.options.documentId)
    ) {
      this.publish({
        commentEventVersion: this.state.commentEventVersion + 1,
        ...(message.type === 'collaboration_comment_mentioned'
          ? {
              commentMentionEventVersion:
                this.state.commentMentionEventVersion + 1,
            }
          : {}),
      })
      return
    }
    if (isDurableRejection(message)) {
      console.warn(`The collaboration service rejected a local update (${message.code}).`)
      this.enterUpdateRejected(COLLABORATION_NOTICES.update_rejected)
    }
  }

  private readonly handleEditorAfterTransaction = (transaction: Y.Transaction): void => {
    if (transaction.origin === REMOTE_EDITOR_ORIGIN) return
    this.transactionFacts.set(transaction, describeLocalEditorTransaction(transaction))
    // A textblock this editor created leaves in the same update as its one
    // empty text node, so that the first characters two writers type into it
    // go into one text node. The update of `transaction` already carries it.
    completeCreatedTextblocks(transaction)
  }

  private readonly handleEditorDocumentUpdate = (
    update: Uint8Array,
    origin: unknown,
    _document: Y.Doc,
    transaction: Y.Transaction,
  ): void => {
    if (
      this.destroyed
      || this.durabilityFailed
      || origin === REMOTE_EDITOR_ORIGIN
    ) return
    const transactionFacts = this.transactionFacts.get(transaction) ?? null
    if (this.applyingRemoteUpdate) {
      // Yjs removes format items that a remote change made redundant, in a
      // local transaction of its own. The room runs the same cleanup when it
      // applies the same change, so that echo is not sent. Whether it is the
      // same cleanup is decided once the transport document settled (see
      // handleTransportSettled).
      if (transactionFacts?.formattingCleanup) {
        if (this.collectingTransportFollowUps) {
          this.heldEditorCleanups.push({
            deleted: deletedRanges(transaction.deleteSet),
            facts: transactionFacts,
            update: Uint8Array.from(update),
          })
          return
        }
        console.debug('A formatting cleanup of a remote collaboration update was not sent.')
        return
      }
      if (!this.nestedLocalUpdateWarned) {
        this.nestedLocalUpdateWarned = true
        console.warn(
          'A local change was made while a remote collaboration update was applied; '
          + 'it is sent once the update is applied.',
        )
      }
    }
    this.enqueueLocalEditorUpdate(update, transactionFacts, true)
  }

  /** Adds a local editor update to the open batch. `mayPumpNow` is false
   * while the controller is applying a batch to the transport document: a
   * send from there would overtake the batch being applied. */
  private enqueueLocalEditorUpdate(
    update: Uint8Array,
    transactionFacts: LocalEditorTransactionFacts | null,
    mayPumpNow: boolean,
  ): void {
    if (transactionFacts === null && !this.missingFactsWarned) {
      this.missingFactsWarned = true
      console.warn(
        'A local collaboration update arrived without its transaction facts; '
        + 'it is sent alone.',
      )
    }
    const facts = describeLocalEditorUpdate({
      access: this.writeAccess(),
      stamp: this.writeStamp,
      transaction: transactionFacts,
      update,
    })
    const open = this.openBatch
    const sealed = open !== null
      && localBatchSealReason(open.facts, facts, this.mergeByteCap()) !== null
    if (sealed) this.sealOpenBatch()
    if (this.openBatch === null) {
      this.openBatch = {
        enteredAt: this.dependencies.now(),
        facts: mergeLocalBatchFacts(null, facts),
        merged: null,
        updates: [update],
      }
    } else {
      this.openBatch.updates.push(update)
      this.openBatch.facts = mergeLocalBatchFacts(this.openBatch.facts, facts)
      this.openBatch.merged = null
    }
    this.publish({ durabilityStatus: 'pending' })
    if (sealed && mayPumpNow) {
      this.pumpSend()
    } else if (sealed) {
      this.schedulePump(0)
    } else if (!this.hasUpdateInFlight() && this.pumpTimer === null) {
      // Nothing waits for an acknowledgement: send after the idle delay, so
      // a burst of keystrokes still travels as one update.
      this.schedulePump(this.providerFlushMs)
    }
    this.refreshDelayNotice()
  }

  private readonly handleTransportDocumentUpdate = (
    update: Uint8Array,
    origin: unknown,
  ): void => {
    if (this.destroyed || this.durabilityFailed) return
    if (origin !== this.providerAdapter?.provider) {
      // A transaction of the transport document's own while the controller
      // applies a remote update or a local batch to it: Yjs removing format
      // items the applied update left redundant. The editor document ran
      // its own cleanup on its own state, which may differ; this one is
      // handed to it once the transport settled.
      if (
        this.collectingTransportFollowUps
        && origin !== LOCAL_TRANSPORT_ORIGIN
      ) {
        this.transportFollowUps.push(Uint8Array.from(update))
      }
      return
    }
    // Hocuspocus 4.3 applies incoming sync messages with the provider itself
    // as origin. Keeping this identity check exact prevents remote changes
    // from entering the local durable-ack queue.
    this.collectingTransportFollowUps = true
    // A room broadcast carries one transaction the room ran. Without an
    // unconfirmed update of this editor the transport then holds exactly
    // what the room held when it ran it, so the transport's cleanup is the
    // one the room ran. With one, the room may have applied this update
    // before it and cleaned up another state. A sync answer merges changes
    // the room ran as transactions of their own, each with its own cleanup;
    // the transport's cleanup of the merge can remove what none of them
    // did. It is sent only under a lease that may change the document; a
    // reader keeps it and so never answers a sync request (mayAnswerSync).
    this.transportFollowUpRoute = (
      this.unconfirmedUpdates.length > 0
      || (this.providerAdapter?.applyingSyncAnswer() === true && this.leaseWritesDocument())
    ) ? 'send' : 'echo'
    this.applyingRemoteUpdate = true
    try {
      Y.applyUpdate(this.document, update, REMOTE_EDITOR_ORIGIN)
    } finally {
      this.applyingRemoteUpdate = false
    }
    this.publish({ activityRevision: this.state.activityRevision + 1 })
  }

  /** The transport document settled after the controller applied a remote
   * update or a local batch to it. A formatting cleanup Yjs runs is a local
   * deletion of this editor, and every deletion this editor makes must
   * reach the room unless the room certainly makes the same one; a deletion
   * held only here makes the replicas' items differ, and a later change
   * next to the item can then read differently here than in the room.
   *
   * The transport's own cleanups are applied to the editor document, so the
   * editor holds every deletion the transport holds; without them the
   * editor keeps format items the room and the transport removed, and can
   * hold a document the schema refuses. They are sent when the room may
   * have cleaned up another state (see transportFollowUpRoute); after a
   * batch they travel in the same update as the batch. The editor's own
   * cleanups that ran meanwhile are sent when they delete an item the
   * transport does not hold deleted: the editor judged its unsent changes
   * with it, which neither the transport nor the room did. */
  private readonly handleTransportSettled = (): void => {
    if (!this.collectingTransportFollowUps) return
    const followUps = this.transportFollowUps.splice(0)
    const route = this.transportFollowUpRoute
    this.transportFollowUpRoute = 'echo'
    if (this.destroyed || this.durabilityFailed) {
      this.collectingTransportFollowUps = false
      this.heldEditorCleanups.splice(0)
      return
    }
    // Still collecting: a cleanup the editor runs after a forwarded
    // follow-up is judged below with the others.
    for (const followUp of followUps) {
      this.applyingRemoteUpdate = true
      try {
        Y.applyUpdate(this.document, followUp, REMOTE_EDITOR_ORIGIN)
      } finally {
        this.applyingRemoteUpdate = false
      }
    }
    this.collectingTransportFollowUps = false
    let echoed = false
    if (followUps.length > 0) {
      if (route === 'batch') this.batchFollowUps.push(...followUps)
      else if (route === 'send') this.trackTransportFollowUps(followUps)
      else echoed = true
    }
    const held = this.heldEditorCleanups.splice(0)
    for (const cleanup of held) {
      if (this.transportHoldsDeleted(cleanup.deleted)) {
        echoed = true
        continue
      }
      this.enqueueLocalEditorUpdate(cleanup.update, cleanup.facts, !this.applyingBatch)
    }
    if (echoed) console.debug('A formatting cleanup of a remote collaboration update was not sent.')
  }

  /** Whether every item in `deleted` is deleted in the transport document. */
  private transportHoldsDeleted(deleted: DeletedRanges): boolean {
    const store = this.transportDocument.store
    for (const { client, clock, length } of deleted) {
      const end = clock + length
      if (end > Y.getState(store, client)) return false
      let cursor = clock
      while (cursor < end) {
        const struct = Y.getItem(store, Y.createID(client, cursor))
        if (!struct.deleted) return false
        cursor = struct.id.clock + struct.length
      }
    }
    return true
  }

  /** Logs the transport's cleanups after a remote update as a local update
   * of their own. They are in the transport document already, so they join
   * the unconfirmed updates directly, behind every update the transport
   * held before them, and go out in that order. */
  private trackTransportFollowUps(followUps: readonly Uint8Array[]): void {
    const update = followUps.length === 1 ? followUps[0]! : Y.mergeUpdates([...followUps])
    const now = this.dependencies.now()
    this.trackUnconfirmedUpdate(update, {
      class: this.writeAccess() === 'suggest' || this.writeStamp === 'suggest'
        ? 'suggestion'
        : 'isolated',
      enteredAt: now,
    }, now, false)
    if (!this.applyingBatch && !this.hasUpdateInFlight()) this.pumpSend()
  }

  /** Sends the next update the log allows: the oldest unconfirmed update not
   * yet resent on this socket, else the oldest batch. It sends only while no
   * update waits for its acknowledgement on this socket and only within the
   * pacing the session's update allowance sets; an acknowledgement or the
   * pacing timer calls it again. */
  private readonly pumpSend = (): void => {
    this.cancelPumpTimer()
    if (this.destroyed || this.durabilityFailed) return
    if (this.applyingUpdate()) {
      // Yjs emits a transaction that starts while another one of the same
      // document is still being cleaned up only after that cleanup. A batch
      // applied to the transport document now would reach it without
      // reaching the socket, and the room would never receive it. The send
      // runs once the application ended.
      this.schedulePump(0)
      return
    }
    if (this.isSocketOrdered() && !this.hasUpdateInFlight()) {
      this.sendNext()
    }
    this.publish({ durabilityStatus: this.settledDurabilityStatus() })
    this.refreshDelayNotice()
    this.handleDurabilitySettled()
  }

  /** The controller is applying a room update, or the transport's cleanups
   * of one, to the editor document, or a local batch to the transport
   * document. A room update reaches the editor from inside the transport
   * document's update event, so the transport is still cleaning up its
   * transaction meanwhile. */
  private applyingUpdate(): boolean {
    return this.applyingRemoteUpdate || this.applyingBatch
  }

  /** `pending` while anything is unconfirmed, else `saved`; a controller
   * that never had a local change stays `idle`. */
  private settledDurabilityStatus(): EditorCollaborationDurabilityStatus {
    if (this.hasPendingDurability()) return 'pending'
    return this.state.durabilityStatus === 'idle' ? 'idle' : 'saved'
  }

  private sendNext(): void {
    const resend = this.unconfirmedUpdates.find((entry) => entry.sentOn !== this.orderedSocket)
    if (!resend && this.sealedBatches.length === 0 && this.openBatch === null) return
    const now = this.dependencies.now()
    const waitMs = this.pacingDelayMs(now)
    if (waitMs > 0) {
      this.schedulePump(waitMs)
      return
    }
    if (resend) {
      // A replay resends one update per acknowledgement: before the room
      // has loaded, every frame counts against the service's queue.
      this.sendEntry(resend, now)
      return
    }
    for (;;) {
      const batch = this.sealedBatches.shift() ?? this.takeOpenBatch()
      if (!batch) {
        // Every waiting batch was one the transport already held, so nothing
        // was written; a presence change that waited for them may go now.
        this.providerAdapter?.localUpdatesWritten()
        return
      }
      if (this.sendBatch(batch, now)) return
    }
  }

  /** Applies a batch to the transport document and sends what the
   * transport emits; false when the transport already held all of it. A
   * batch the transport took in without emitting it is never dropped: the
   * editor stops sending, see transportTookIn. */
  private sendBatch(batch: LocalBatch, now: number): boolean {
    const update = this.mergedUpdateOf(batch)
    const transportVector = Y.encodeStateVector(this.transportDocument)
    let transportedUpdate: Uint8Array | null = null
    let batchTransaction: Y.Transaction | null = null
    const noteBatchTransaction = (transaction: Y.Transaction): void => {
      if (transaction.origin === LOCAL_TRANSPORT_ORIGIN) batchTransaction ??= transaction
    }
    const captureTransportedUpdate = (
      emittedUpdate: Uint8Array,
      origin: unknown,
    ): void => {
      if (origin === LOCAL_TRANSPORT_ORIGIN) {
        transportedUpdate = Uint8Array.from(emittedUpdate)
      }
    }
    this.transportDocument.on('beforeTransaction', noteBatchTransaction)
    this.transportDocument.on('update', captureTransportedUpdate)
    this.collectingTransportFollowUps = true
    // The room applies the batch to a state that may hold changes this
    // transport has not received yet, so its cleanup can differ; the
    // transport's cleanup travels with the batch.
    this.transportFollowUpRoute = 'batch'
    this.applyingBatch = true
    try {
      Y.applyUpdate(this.transportDocument, update, LOCAL_TRANSPORT_ORIGIN)
    } finally {
      this.applyingBatch = false
      this.transportDocument.off('beforeTransaction', noteBatchTransaction)
      this.transportDocument.off('update', captureTransportedUpdate)
    }
    const followUps = this.batchFollowUps.splice(0)
    // The editor document may merge a local transaction with structs that
    // already arrived remotely on the transport document. Yjs removes those
    // redundant structs before emitting the actual provider update. Durable
    // acknowledgements hash that emitted payload, so tracking the pre-merge
    // input can leave a phantom pending hash forever after concurrent edits.
    if (!transportedUpdate) {
      if (!transportTookIn(this.transportDocument, batchTransaction, transportVector)) {
        return false
      }
      // The transport holds changes the socket never carried: the room
      // would never receive them, and would refuse every later update that
      // depends on them as a causal gap. Nothing more is sent; the editor
      // keeps its state.
      console.warn(
        'A local collaboration batch changed the transport document without '
        + 'being emitted for sending; this editor stops sending and keeps its '
        + 'local state as a backup.',
      )
      this.enterUpdateRejected(COLLABORATION_NOTICES.causal_gap_unrecoverable)
      return true
    }
    const own = withOwnItems(transportedUpdate, update, transportVector)
    this.trackSentUpdate(
      followUps.length === 0 ? own : Y.mergeUpdates([own, ...followUps]),
      batch,
      now,
    )
    return true
  }

  private mergedUpdateOf(batch: LocalBatch): Uint8Array {
    batch.merged ??= batch.updates.length === 1
      ? batch.updates[0]!
      : Y.mergeUpdates(batch.updates)
    return batch.merged
  }

  private trackSentUpdate(update: Uint8Array, batch: LocalBatch, now: number): void {
    this.trackUnconfirmedUpdate(update, {
      class: batch.facts.class,
      enteredAt: batch.enteredAt,
    }, now, true)
  }

  /** Appends an update to the unconfirmed log and writes it on the socket
   * when `send`; otherwise the pump sends it in its turn. */
  private trackUnconfirmedUpdate(
    update: Uint8Array,
    origin: Pick<UnconfirmedUpdate, 'class' | 'enteredAt'>,
    now: number,
    send: boolean,
  ): void {
    // Appended synchronously, so the log keeps the order in which updates
    // were produced even when their digests resolve in another order.
    const entry: UnconfirmedUpdate = {
      class: origin.class,
      enteredAt: origin.enteredAt,
      hash: null,
      sentOn: null,
      update: Uint8Array.from(update),
    }
    this.unconfirmedUpdates.push(entry)
    if (send) this.sendEntry(entry, now)
    this.publish({ durabilityStatus: 'pending' })
    void this.dependencies.hashUpdate(update).then((hash) => {
      if (this.destroyed) return
      if (this.durabilityFailed || this.earlyAcks.delete(hash)) {
        this.forgetUnconfirmedUpdate(entry)
        if (this.durabilityFailed) return
        if (!this.hasHashesInFlight()) this.earlyAcks.clear()
        this.recordReleaseProgress()
        // The acknowledgement arrived before the digest; it paces the next
        // send all the same.
        this.pumpSend()
        return
      }
      entry.hash = hash
      if (!this.hasHashesInFlight()) this.earlyAcks.clear()
      this.publish({ durabilityStatus: 'pending' })
      this.handleDurabilitySettled()
    }).catch(() => {
      if (this.destroyed) return
      this.forgetUnconfirmedUpdate(entry)
      if (!this.hasHashesInFlight()) this.earlyAcks.clear()
      this.enterDurabilityFailure(COLLABORATION_NOTICES.update_untracked)
    })
  }

  private forgetUnconfirmedUpdate(entry: UnconfirmedUpdate): void {
    const index = this.unconfirmedUpdates.indexOf(entry)
    if (index >= 0) this.unconfirmedUpdates.splice(index, 1)
  }

  private hasHashesInFlight(): boolean {
    return this.unconfirmedUpdates.some((entry) => entry.hash === null)
  }

  /** Some update went out on the ordered socket and waits for its
   * acknowledgement. */
  private hasUpdateInFlight(): boolean {
    return this.isSocketOrdered()
      && this.unconfirmedUpdates.some((entry) => entry.sentOn === this.orderedSocket)
  }

  /** The socket the transport uses now replayed the unconfirmed updates. */
  private isSocketOrdered(): boolean {
    return this.orderedSocket !== null
      && this.providerAdapter !== null
      && this.providerAdapter.socketId() === this.orderedSocket
  }

  /** Writes an unconfirmed update on the current socket. Only the pump and
   * a page exit call it, on a socket that already carried every
   * unconfirmed update before this one. */
  private sendEntry(entry: UnconfirmedUpdate, now: number): void {
    const providerAdapter = this.providerAdapter
    if (!this.isSocketOrdered() || !providerAdapter) return
    try {
      providerAdapter.sendUpdate(Uint8Array.from(entry.update))
    } catch {
      this.enterReconnect(COLLABORATION_NOTICES.send_failed)
      return
    }
    entry.sentOn = this.orderedSocket
    this.socketSendTimes.push(now)
    providerAdapter.localUpdatesWritten()
  }

  /** Local updates the editor produced that the current socket did not
   * carry yet: waiting batches, and unconfirmed updates this socket has not
   * resent. The own presence may point into them. */
  private readonly holdsUnsentLocalUpdates = (): boolean => (
    this.openBatch !== null
    || this.sealedBatches.length > 0
    || this.unconfirmedUpdates.some((entry) => (
      !this.isSocketOrdered() || entry.sentOn !== this.orderedSocket
    ))
  )

  /** The lease lets this editor change the document. The service refuses
   * every document update under a view lease, which a comment access holds
   * as well. */
  private leaseWritesDocument(): boolean {
    return this.state.access === 'edit' || this.state.access === 'suggest'
  }

  private writeAccess(): CollaborationAccess {
    return this.state.access === 'suggest' ? 'suggest' : 'edit'
  }

  /** The service admits no frame larger than its frame limit or than the
   * bytes it queues for one socket, so a merge stays within the smaller. */
  private mergeByteCap(): number {
    return collaborationMergeByteCap(Math.min(
      this.sendLimits.frameLimitBytes,
      this.sendLimits.maxQueuedBytes,
    ))
  }

  /** Updates this socket sent within the last rate window. */
  private sendsInWindow(now: number): number {
    const windowStart = now - this.sendLimits.updateRateWindowMs
    while (
      this.socketSendTimes.length > 0
      && this.socketSendTimes[0]! <= windowStart
    ) this.socketSendTimes.shift()
    return this.socketSendTimes.length
  }

  /** How long the next paced send waits: at most four fifths of the update
   * allowance (at least one) per window, evenly spaced. */
  private pacingDelayMs(now: number): number {
    const windowMs = this.sendLimits.updateRateWindowMs
    const perWindow = Math.max(1, Math.floor(
      this.sendLimits.updateRateCount * PACED_ALLOWANCE_NUMERATOR / PACED_ALLOWANCE_DENOMINATOR,
    ))
    const intervalMs = Math.ceil(windowMs / perWindow)
    const sent = this.sendsInWindow(now)
    let waitMs = 0
    const last = this.socketSendTimes.at(-1)
    if (last !== undefined) waitMs = last + intervalMs - now
    if (sent >= perWindow) {
      waitMs = Math.max(waitMs, this.socketSendTimes[sent - perWindow]! + windowMs - now)
    }
    return Math.max(0, waitMs)
  }

  /** What a page exit may send now: the unconfirmed updates this socket has
   * not carried yet, then the waiting batches in order, one frame each, as
   * far as the published limits allow together with every unconfirmed
   * update. */
  private planPageExit(now: number): {
    batches: number
    carriesAll: boolean
    resends: number
  } {
    const waiting = this.sealedBatches.length + (this.openBatch === null ? 0 : 1)
    if (!this.canWriteForPageExit()) return { batches: 0, carriesAll: false, resends: 0 }
    const limits = this.sendLimits
    let frames = limits.updateRateCount - PAGE_EXIT_ALLOWANCE_MARGIN - this.sendsInWindow(now)
    let unconfirmedRoom = Math.min(
      limits.maxQueuedFrames - CONTROL_FRAMES,
      limits.updateRateCount - PAGE_EXIT_ALLOWANCE_MARGIN,
      COLLABORATION_PRE_OPEN_QUEUE_FRAMES - CONTROL_FRAMES,
    ) - this.unconfirmedUpdates.length
    let byteRoom = Math.min(limits.frameLimitBytes, limits.maxQueuedBytes)
      - CONTROL_FRAME_MARGIN_BYTES
      - this.unconfirmedUpdates.reduce((total, entry) => total + entry.update.byteLength, 0)
    const pendingResends = this.unconfirmedUpdates
      .filter((entry) => entry.sentOn !== this.orderedSocket).length
    const resends = Math.max(0, Math.min(pendingResends, frames))
    frames -= resends
    if (resends < pendingResends) {
      return { batches: 0, carriesAll: false, resends }
    }
    const batches = this.openBatch === null
      ? this.sealedBatches
      : [...this.sealedBatches, this.openBatch]
    let count = 0
    for (const batch of batches) {
      if (frames <= 0 || unconfirmedRoom <= 0) break
      const bytes = this.mergedUpdateOf(batch).byteLength
      if (bytes > byteRoom) break
      byteRoom -= bytes
      frames -= 1
      unconfirmedRoom -= 1
      count += 1
    }
    return { batches: count, carriesAll: count === waiting, resends }
  }

  /** A page exit may write document frames now: nothing failed, the socket
   * carried every unconfirmed update in order, and it is open. A frame on a
   * socket that is not ordered could precede its predecessors; one on a
   * closing socket is dropped without a trace. */
  private canWriteForPageExit(): boolean {
    return !this.destroyed
      && !this.durabilityFailed
      && this.blockingFailure === null
      && this.isSocketOrdered()
      && this.providerAdapter !== null
      && this.providerAdapter.isSocketOpen()
  }

  /** The page is hidden, about to unload or leaving: send everything
   * assembled at once, in order, one frame per batch, within the published
   * limits. Nothing is merged anew, and nothing waits for a timer or a
   * microtask: the page may not run again. The unload announcement comes
   * first because some engines close the socket before `pagehide`; a later
   * exit sends only what an earlier one left. Inside the app (another route,
   * another document) the page and its socket stay, and the log keeps
   * draining one update per acknowledgement instead. */
  private readonly handlePageExit = (reason: CollaborationPageExitReason): void => {
    if (this.destroyed) return
    if (this.canWriteForPageExit()) {
      this.sealOpenBatch()
      const now = this.dependencies.now()
      const plan = this.planPageExit(now)
      let resends = plan.resends
      for (const entry of this.unconfirmedUpdates) {
        if (resends === 0) break
        if (entry.sentOn === this.orderedSocket) continue
        this.sendEntry(entry, now)
        resends -= 1
      }
      for (let index = 0; index < plan.batches; index += 1) {
        const batch = this.sealedBatches.shift()
        if (!batch) break
        this.sendBatch(batch, now)
      }
    }
    if (reason === 'pagehide' && this.providerAdapter?.isSocketOpen()) {
      // A hidden tab stays open and its user may return, as does a user who
      // stays when the browser asks about leaving; a leaving page takes its
      // cursor with it, after its document frames.
      this.providerAdapter.leaveAwareness()
    }
    this.publish({
      durabilityStatus: this.durabilityFailed
        ? this.state.durabilityStatus
        : this.settledDurabilityStatus(),
    })
  }

  private readonly handlePageShow = (persisted: boolean): void => {
    if (this.destroyed || !persisted) return
    this.providerAdapter?.restoreAwareness()
    this.publish({})
  }

  /** Evaluated when the browser is about to unload the page, after the page
   * exit sent what it could: ask only when changes remain that a page exit
   * cannot send now. */
  private readonly leaveGuard = (): boolean => (
    !this.destroyed
    && this.hasPendingDurability()
    && !this.planPageExit(this.dependencies.now()).carriesAll
  )

  private handleSessionFailure(error: unknown, refresh: boolean): void {
    if (this.destroyed) return
    const status = requestStatus(error)
    const reason = requestReason(error)
    // The same cut as in handleClose: a 409 without a compatibility reason is
    // an ordinary conflict and says nothing about the app version.
    const versionMismatch = isCompatibilityReason(reason)
    if (versionMismatch || status === 409) {
      this.enterIncompatible(versionMismatch
        ? COLLABORATION_NOTICES.protocol_or_schema_incompatible
        : COLLABORATION_NOTICES.session_ended)
      return
    }
    if (status === 403 || status === 404 || reason === 'access_revoked') {
      this.enterAccessRevoked(COLLABORATION_NOTICES.access_unavailable)
      return
    }
    if (status === 401) {
      this.clearRefreshTimer()
      this.clearReconnectTimer()
      this.disconnectTransport()
      this.publish({
        canEdit: false,
        connectionStatus: 'error',
        error: COLLABORATION_NOTICES.session_expired,
        lifecycleStatus: 'error',
        nextReconnectAt: null,
        recoverability: 'login',
        synced: false,
      })
      return
    }
    if (refresh) {
      this.enterReconnect(COLLABORATION_NOTICES.lease_refresh_failed)
      return
    }
    // Transient initial-open failures (429 lease budget, network, 5xx) enter
    // the same backoff ladder rotations already use — an initial open that
    // fails on a rate limit self-heals within the lease TTL instead of
    // parking in a terminal error. Every other 4xx is a client defect and
    // stays loud and terminal.
    const transientOpenFailure =
      status === undefined || status === 429 || status >= 500
    console.warn(`The collaboration session could not be opened: ${messageFromError(error)}`)
    if (transientOpenFailure) {
      this.enterReconnect(COLLABORATION_NOTICES.session_open_failed_retrying)
      return
    }
    this.clearRefreshTimer()
    this.disconnectTransport()
    this.publish({
      canEdit: false,
      connectionStatus: 'error',
      error: COLLABORATION_NOTICES.session_open_refused,
      lifecycleStatus: 'error',
      nextReconnectAt: null,
      recoverability: 'retry',
    })
  }

  private enterReconnect(error: CollaborationNotice): void {
    if (this.destroyed) return
    this.clearRefreshTimer()
    this.disconnectTransport()
    const reconnectAttempt = this.state.reconnectAttempt + 1
    const delayMs = collaborationReconnectDelayMs(
      reconnectAttempt,
      this.dependencies.random(),
    )
    const nextReconnectAt = this.dependencies.now() + delayMs
    this.publish({
      canEdit: false,
      connectionStatus: 'reconnecting',
      error,
      lifecycleStatus: 'reconnecting',
      nextReconnectAt,
      reconnectAttempt,
      recoverability: 'retry',
      synced: false,
    })
    this.clearReconnectTimer()
    this.reconnectTimer = this.dependencies.scheduleTimer(() => {
      this.reconnectTimer = null
      this.publish({ nextReconnectAt: null })
      if (this.leaseToken) {
        void this.refreshLease('reconnect')
        return
      }
      // The ladder can also carry a failed INITIAL open (no lease yet):
      // refreshLease would bail without a token, so re-run the open itself.
      this.started = false
      void this.start()
    }, delayMs)
  }

  /** Bounded: a second gap without a durable acknowledgement in between
   * means the replay cannot restore the order, so the editor stops with its
   * local state retained instead of reconnecting forever. */
  private recoverFromCausalGap(): void {
    if (this.causalGapRecoveryPending) {
      this.enterUpdateRejected(COLLABORATION_NOTICES.causal_gap_unrecoverable)
      return
    }
    this.causalGapRecoveryPending = true
    this.enterReconnect(COLLABORATION_NOTICES.causal_gap_resending)
  }

  private revalidateAccess(): void {
    if (this.hasUnreconciledUpdates()) {
      this.enterAccessChangeWithUnconfirmedUpdates()
      return
    }
    if (
      this.state.connectionStatus === 'reconnecting'
      && (this.reconnectTimer !== null || this.refreshInFlight !== null)
    ) return
    this.enterReconnect(COLLABORATION_NOTICES.access_changed_revalidating)
  }

  private enterAccessChangeWithUnconfirmedUpdates(): void {
    this.enterDurabilityFailure(COLLABORATION_NOTICES.access_changed_with_unconfirmed_updates)
  }

  private enterIncompatible(error: CollaborationNotice): void {
    this.enterUnrecoverable('incompatible', error)
  }

  private enterOriginRejected(error: CollaborationNotice): void {
    this.enterUnrecoverable('origin_rejected', error)
  }

  private enterUnrecoverable(
    connectionStatus: 'incompatible' | 'origin_rejected',
    error: CollaborationNotice,
  ): void {
    const durabilityStatus = this.hasUnreconciledUpdates()
      ? 'error'
      : this.state.durabilityStatus
    this.durabilityFailed = true
    this.clearDelayNotice()
    this.clearRefreshTimer()
    this.clearReconnectTimer()
    this.disconnectTransport()
    this.publish({
      canEdit: false,
      connectionStatus,
      durabilityStatus,
      error,
      lifecycleStatus: 'error',
      nextReconnectAt: null,
      recoverability: 'reload',
      synced: false,
    })
    this.rejectDurabilityWaiters(new Error(error))
    this.finalizeFailedRelease(error)
  }

  private enterAccessRevoked(error: CollaborationNotice): void {
    const durabilityStatus = this.hasUnreconciledUpdates()
      ? 'error'
      : this.state.durabilityStatus
    this.durabilityFailed = true
    this.clearDelayNotice()
    this.accessRevoked = true
    this.clearRefreshTimer()
    this.clearReconnectTimer()
    this.disconnectTransport()
    this.publish({
      access: 'view',
      canEdit: false,
      connectionStatus: 'access_revoked',
      durabilityStatus,
      error,
      lifecycleStatus: 'error',
      liveDocumentReady: false,
      nextReconnectAt: null,
      recoverability: 'none',
      synced: false,
    })
    this.rejectDurabilityWaiters(new Error(error))
    this.finalizeFailedRelease(error)
  }

  private enterDurabilityFailure(error: CollaborationNotice): void {
    this.enterFailure(error, false)
  }

  /** The service refused a local update of this editor. Nothing is sent
   * again: resending the refused state cannot succeed, and it must not reach
   * the room through a sync answer either. The editor keeps showing the local
   * state, read-only, as a backup; a document reload (reloadDocument) starts
   * over from the service's state and discards it. The rest of the page and
   * the other documents are not affected. */
  private enterUpdateRejected(error: CollaborationNotice): void {
    this.enterFailure(error, true)
  }

  private enterFailure(error: CollaborationNotice, updateRejected: boolean): void {
    this.durabilityFailed = true
    this.clearDelayNotice()
    this.clearRefreshTimer()
    this.clearReconnectTimer()
    this.publish({
      canEdit: false,
      connectionStatus: 'error',
      durabilityStatus: 'error',
      error,
      lifecycleStatus: 'error',
      nextReconnectAt: null,
      recoverability: updateRejected ? 'reload' : 'none',
      synced: false,
      updateRejected,
    })
    this.rejectDurabilityWaiters(new Error(error))
    this.disconnectTransport()
    this.finalizeFailedRelease(error)
  }

  /** Asks the owner of this lifecycle to start the document over from the
   * service's state. Only a rejected update is recovered this way; every
   * other state has its own recovery. */
  readonly reloadDocument = (): void => {
    if (this.destroyed || !this.state.updateRejected) return
    for (const listener of [...this.documentReloadListeners]) listener()
  }

  onDocumentReload(listener: () => void): () => void {
    this.documentReloadListeners.add(listener)
    return () => this.documentReloadListeners.delete(listener)
  }

  private scheduleRefresh(expiresAt: number, refreshAfter?: number): void {
    this.clearRefreshTimer()
    this.refreshTimer = this.dependencies.scheduleTimer(() => {
      this.refreshTimer = null
      void this.refreshLease('periodic')
    }, refreshAfter === undefined
      ? leaseRefreshDelayMs(expiresAt, this.dependencies.now())
      : Math.max(1_000, Math.floor(
          refreshAfter * 1_000 - this.dependencies.now(),
        )))
  }

  private clearRefreshTimer(): void {
    if (this.refreshTimer === null) return
    this.dependencies.cancelTimer(this.refreshTimer)
    this.refreshTimer = null
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer === null) return
    this.dependencies.cancelTimer(this.reconnectTimer)
    this.reconnectTimer = null
  }

  private clearReleaseTimer(): void {
    if (this.releaseTimer === null) return
    this.dependencies.cancelTimer(this.releaseTimer)
    this.releaseTimer = null
  }

  private clearDelayTimer(): void {
    if (this.delayTimer === null) return
    this.dependencies.cancelTimer(this.delayTimer)
    this.delayTimer = null
  }

  private scheduleReleaseTimeout(): void {
    this.clearReleaseTimer()
    this.releaseTimer = this.dependencies.scheduleTimer(
      this.handleReleaseTimeout,
      RELEASE_DURABILITY_TIMEOUT_MS,
    )
  }

  /** An acknowledgement arrived: a released controller that still drains
   * its log is making progress, so its timeout starts again. */
  private recordReleaseProgress(): void {
    if (this.released && this.releaseTimer !== null) this.scheduleReleaseTimeout()
  }

  private cancelPumpTimer(): void {
    if (this.pumpTimer === null) return
    this.dependencies.cancelTimer(this.pumpTimer)
    this.pumpTimer = null
  }

  private schedulePump(delayMs: number): void {
    this.cancelPumpTimer()
    this.pumpTimer = this.dependencies.scheduleTimer(this.pumpSend, delayMs)
  }

  private clearLocalUpdateBatch(): void {
    this.cancelPumpTimer()
    this.openBatch = null
    this.sealedBatches.splice(0)
  }

  private sealOpenBatch(): void {
    if (this.openBatch === null) return
    this.sealedBatches.push(this.openBatch)
    this.openBatch = null
  }

  private takeOpenBatch(): LocalBatch | null {
    const batch = this.openBatch
    this.openBatch = null
    return batch
  }

  /** Closes the batch local edits join and sends what the log allows now. */
  private flushScheduledBatch(): void {
    this.sealOpenBatch()
    this.pumpSend()
  }

  private attachEditorInput(): void {
    if (this.editorInputAttached) return
    this.document.on('afterTransaction', this.handleEditorAfterTransaction)
    this.document.on('update', this.handleEditorDocumentUpdate)
    this.editorInputAttached = true
  }

  private detachEditorInput(): void {
    if (!this.editorInputAttached) return
    this.document.off('afterTransaction', this.handleEditorAfterTransaction)
    this.document.off('update', this.handleEditorDocumentUpdate)
    this.editorInputAttached = false
  }

  private disconnectTransport(): void {
    if (!this.providerAdapter) return
    this.clearConnectDeadline()
    this.authenticated = false
    this.synced = false
    this.markSocketUnordered()
    this.providerAdapter.disconnect()
  }

  private markSocketUnordered(): void {
    this.orderedSocket = null
    this.clearStabilityTimer()
    // The service counts its update allowance per socket.
    this.socketSendTimes.splice(0)
    this.providerAdapter?.setAwarenessForwarding(false)
  }

  /** On a freshly authenticated socket: resend the unconfirmed updates in
   * the order they were produced, one per acknowledgement, and only then
   * let new local updates through. The service acknowledges an update it
   * already stored, or one whose every change the room already holds, so no
   * separate question is needed. A socket other than the ordered one always
   * replays, also when the end of its predecessor was never reported. */
  private replayUnconfirmedUpdates(): void {
    const providerAdapter = this.providerAdapter
    if (!providerAdapter) return
    const socket = providerAdapter.socketId()
    if (this.orderedSocket === socket) return
    if (this.orderedSocket !== null) {
      console.warn(
        'A collaboration socket was replaced without a reported close; the '
        + 'unconfirmed changes are sent again in order.',
      )
    }
    this.orderedSocket = socket
    this.socketSendTimes.splice(0)
    this.pumpSend()
  }

  /** The provider answers the room's sync request with every change of the
   * transport document the room lacks, merged into one update, and with
   * every deletion the transport holds.
   *
   * Under a lease that cannot change the document the answer is never
   * sent. The service closes a view connection whose frame holds anything
   * the room lacks, and a reader's transport can hold such a deletion: the
   * cleanup of a merged sync answer, which a reader keeps (see
   * transportFollowUpRoute). Every later socket of the reader would end
   * so, and the room needs nothing from a reader.
   *
   * While an unconfirmed update has not been written on this socket yet,
   * that answer would carry it ahead of its resend and merged with others,
   * outside the grouping the service accepts; the replay resends one update
   * per acknowledgement, so this holds whenever more than one update was
   * unconfirmed when the socket authenticated. The answer is then not sent
   * at all: besides the room's own changes the transport document holds
   * only sent updates, and every unconfirmed one is resent in order, so the
   * answer adds nothing. Once every unconfirmed update went out on this
   * socket, an answer follows them and changes nothing in the room. */
  private readonly mayAnswerSync = (): boolean => {
    if (!this.leaseWritesDocument()) {
      console.debug(
        'An answer to the collaboration sync request was not sent: this '
        + 'lease cannot change the document.',
      )
      return false
    }
    const socket = this.providerAdapter?.socketId() ?? null
    const resent = this.unconfirmedUpdates.every((entry) => (
      socket !== null && entry.sentOn === socket
    ))
    if (!resent) {
      console.debug(
        'An answer to the collaboration sync request was not sent: unconfirmed '
        + 'updates of this editor are still being resent.',
      )
    }
    return resent
  }

  private pendingHashList(): string[] {
    return this.unconfirmedUpdates.flatMap((entry) => (
      entry.hash === null ? [] : [entry.hash]
    ))
  }

  private handleDurabilitySettled(): void {
    if (this.destroyed || this.hasPendingDurability()) return
    this.resolveDurabilityWaiters()
    if (this.blockingFailure !== null && !this.durabilityFailed) {
      this.blockingFailure = null
      if (!this.released) this.attachEditorInput()
      this.publish({
        blockingFailure: null,
        durabilityStatus: 'saved',
        error: null,
      })
      this.publishReadiness()
    }
    if (this.released && !this.durabilityFailed) this.finalizeDestroy()
  }

  /** Every failure that leaves local changes unconfirmable ends here. */
  private finalizeFailedRelease(error: CollaborationNotice): void {
    // A lifecycle of a replaced generation that no surface shows goes to
    // the recovery store now, released or still lingering.
    recoverReplacedGenerations()
    if (!this.released || this.destroyed) return
    this.clearReleaseTimer()
    if (this.hasPendingDurability()) {
      this.markBlockingFailure(error)
      return
    }
    this.finalizeDestroy()
  }

  private readonly handleReleaseTimeout = (): void => {
    this.releaseTimer = null
    if (this.destroyed || !this.released || !this.hasPendingDurability()) return
    const error = COLLABORATION_NOTICES.release_unconfirmed
    this.markBlockingFailure(error)
  }

  private markBlockingFailure(error: CollaborationNotice): void {
    if (this.blockingFailure !== null) return
    this.blockingFailure = error
    this.clearDelayNotice()
    this.publish({
      blockingFailure: error,
      canEdit: false,
      connectionStatus: 'error',
      durabilityStatus: 'error',
      error,
      lifecycleStatus: 'error',
      nextReconnectAt: null,
      // A rejected update keeps its document reload.
      recoverability: this.state.updateRejected ? 'reload' : 'none',
      synced: false,
    })
    this.rejectDurabilityWaiters(new Error(error))
    this.onReleaseFailed?.(error)
  }

  private publishReadiness(): void {
    if (
      !this.authenticated
      || !this.synced
      || this.destroyed
      || this.durabilityFailed
      || this.blockingFailure !== null
    ) return
    const canEdit = !this.released
      && (
        this.state.access === 'comment'
        || this.state.access === 'edit'
        || this.state.access === 'suggest'
      )
    this.syncedOnce = true
    this.publish({
      canEdit,
      connectionStatus: this.readyConnectionStatus(),
      error: null,
      lifecycleStatus: this.readyLifecycleStatus(),
      liveDocumentReady: !this.released,
      nextReconnectAt: null,
      // The reconnect ladder keeps its count until the connection proves
      // stable (watchConnectionStability).
      recoverability: 'none',
      synced: true,
    })
  }

  private readyConnectionStatus(): EditorCollaborationConnectionStatus {
    return this.state.access === 'view' ? 'read_only' : 'connected'
  }

  private readyLifecycleStatus(): CollaborationDocumentHandle['lifecycleStatus'] {
    return this.state.access === 'view' ? 'read_only' : 'saved'
  }

  private resolveDurabilityWaiters(): void {
    const waiters = [...this.durabilityWaiters]
    this.durabilityWaiters.clear()
    for (const waiter of waiters) waiter.resolve()
  }

  private rejectDurabilityWaiters(error: Error): void {
    const waiters = [...this.durabilityWaiters]
    this.durabilityWaiters.clear()
    for (const waiter of waiters) waiter.reject(error)
  }

  private setAwarenessUser(user: EditorCollaborationUser): void {
    const deterministic = deterministicUser(user)
    const key = `${deterministic.color}\u0000${deterministic.id}\u0000${deterministic.name}`
    if (this.awarenessUserKey === key) return
    this.providerAdapter?.setAwarenessUser(deterministic)
    this.awarenessUserKey = key
  }

  private hasPendingDurability(): boolean {
    return this.openBatch !== null
      || this.sealedBatches.length > 0
      || this.unconfirmedUpdates.length > 0
  }

  /** When the oldest local change that is not confirmed yet was made: the
   * earliest among the unconfirmed updates and the waiting batches. The
   * unconfirmed updates are not in the order their changes were made: a
   * cleanup of the transport document joins them when it runs, ahead of
   * batches made before it that still wait. */
  private oldestPendingEnteredAt(): number | null {
    // Batches wait in the order they were made.
    let oldest = (this.sealedBatches[0] ?? this.openBatch)?.enteredAt ?? null
    for (const entry of this.unconfirmedUpdates) {
      if (oldest === null || entry.enteredAt < oldest) oldest = entry.enteredAt
    }
    return oldest
  }

  /** Watches the oldest unconfirmed local change and shows the delay notice
   * once it waited longer than COLLABORATION_ACK_DELAY_MS, whether the
   * service is slow or the change still waits in this editor. It takes no
   * action of its own. */
  private refreshDelayNotice(): void {
    if (this.destroyed) return
    const oldest = this.durabilityFailed || this.blockingFailure !== null
      ? null
      : this.oldestPendingEnteredAt()
    if (oldest === this.delayReference) return
    this.clearDelayTimer()
    this.delayReference = oldest
    if (oldest === null) {
      if (this.state.durabilityDelayed) this.publish({ durabilityDelayed: false })
      return
    }
    const remainingMs = oldest + COLLABORATION_ACK_DELAY_MS - this.dependencies.now()
    if (remainingMs <= 0) {
      this.markDurabilityDelayed()
      return
    }
    if (this.state.durabilityDelayed) this.publish({ durabilityDelayed: false })
    this.delayTimer = this.dependencies.scheduleTimer(() => {
      this.delayTimer = null
      if (this.delayReference === oldest) this.markDurabilityDelayed()
    }, remainingMs)
  }

  private markDurabilityDelayed(): void {
    if (this.state.durabilityDelayed || this.durabilityFailed) return
    const hash = this.unconfirmedUpdates[0]?.hash
    console.warn(
      `The collaboration change ${hash ? hash.slice(0, 12) : 'unsent'} has waited `
      + `more than ${COLLABORATION_ACK_DELAY_MS / 1_000} seconds for its acknowledgement.`,
    )
    this.publish({ durabilityDelayed: true })
  }

  private clearDelayNotice(): void {
    this.clearDelayTimer()
    this.delayReference = null
    if (this.state.durabilityDelayed) this.publish({ durabilityDelayed: false })
  }

  private hasUnreconciledUpdates(): boolean {
    return this.hasPendingDurability()
  }

  private publish(patch: Partial<CollaborationDocumentHandle>): void {
    if (this.destroyed) return
    const nextState = {
      ...this.state,
      ...patch,
      hasUnconfirmedLocalChanges: this.hasPendingDurability(),
      pendingHashes: this.pendingHashList(),
      unconfirmedSince: this.oldestPendingEnteredAt(),
    }
    this.state = {
      ...nextState,
      authorityRevision: collaborationAuthorityChanged(this.state, nextState)
        ? this.state.authorityRevision + 1
        : this.state.authorityRevision,
    }
    // The browser asks before leaving only while something is unconfirmed;
    // without a guard the page stays eligible for the back/forward cache.
    this.pageLifecycle?.setLeaveGuard(this.hasPendingDurability() ? this.leaveGuard : null)
    for (const listener of this.listeners) listener(this.state)
  }
}

export function acquireLifecycleController(
  key: string,
  identity: string | null,
  create: () => CollaborationDocumentController,
): CollaborationDocumentController {
  const existing = lifecycleRegistry.get(key)
  if (existing && (existing.references > 0 || existing.identity === identity)) {
    lifecycleFailures.delete(key)
    clearLinger(existing)
    existing.references += 1
    existing.controller.retain()
    return existing.controller
  }
  if (existing) {
    // A lingering controller whose request identity went stale (api key
    // changed under the same registry key) would keep signing rotations with
    // the old closure. Tear it down and start fresh.
    finalizeEntryRelease(key, existing)
  }
  const controller = create()
  lifecycleRegistry.set(key, {
    controller,
    identity,
    lingerSince: null,
    lingerTimer: null,
    references: 1,
  })
  releaseSupersededGenerations(key)
  const parts = lifecycleKeyParts(key)
  if (parts) noteGeneration(parts.documentKey, parts.generation)
  return controller
}

export function releaseLifecycleController(
  key: string,
  controller: CollaborationDocumentController,
): void {
  const entry = lifecycleRegistry.get(key)
  if (!entry || entry.controller !== controller) {
    controller.release()
    return
  }
  entry.references = Math.max(0, entry.references - 1)
  if (entry.references > 0) return
  if (!controllerCanLinger(controller)) {
    finalizeEntryRelease(key, entry)
    return
  }
  controller.flushPendingLocalUpdates()
  entry.lingerSince = Date.now()
  const timer = setTimeout(() => {
    entry.lingerTimer = null
    finalizeEntryRelease(key, entry)
  }, LIFECYCLE_LINGER_MS)
  // In node (tests) a pending linger timer must not pin the process open.
  if (typeof timer === 'object' && 'unref' in timer) timer.unref()
  entry.lingerTimer = timer
  enforceLingerCap()
}

/** Immediately tear down every lifecycle that is only lingering (no mounted
 * surface). An identity boundary that does not hard-reload the document must
 * not keep rotating leases for the previous identity; the test suite uses it
 * to keep the module-level registry hermetic between cases. The generations
 * known for the previous identity's documents are forgotten with them. */
export function destroyLingeringCollaborationLifecycles(): void {
  for (const [key, entry] of [...lifecycleRegistry.entries()]) {
    if (entry.references > 0 || entry.lingerTimer === null) continue
    finalizeEntryRelease(key, entry)
  }
  newestKnownGenerations.clear()
}

/** Installs where the local state of a replaced generation goes. A document
 * whose generation was replaced cannot be reloaded in place any more, so a
 * lifecycle of the old generation that holds changes the service will never
 * confirm (see holdsUnconfirmableLocalChanges) would stay registered for
 * good and refuse every flush, the sign-out's included. Once no surface
 * shows it and a newer generation of its document is known, it is handed to
 * `store`; when `store` returns true it kept the state, and the lifecycle is
 * disposed. When it returns false, or none is installed, the lifecycle stays
 * as it is. Returns the uninstall. */
export function registerReplacedGenerationRecovery(
  store: ReplacedGenerationRecoveryStore,
): () => void {
  replacedGenerationRecoveryStore = store
  recoverReplacedGenerations()
  return () => {
    if (replacedGenerationRecoveryStore === store) replacedGenerationRecoveryStore = null
  }
}

/** Reports the generation a workspace document has now, as the server
 * lists it. A document whose generation was replaced while it was not open
 * never acquires the new one, so the registry learns it here. */
export function noteCollaborationDocumentGeneration({
  documentId,
  generation,
  workspaceId,
}: {
  documentId: string
  generation: number
  workspaceId: string
}): void {
  noteGeneration(`${workspaceId}:${documentId}:g`, generation)
}

function noteGeneration(documentKey: string, generation: number): void {
  if ((newestKnownGenerations.get(documentKey) ?? -1) >= generation) return
  newestKnownGenerations.set(documentKey, generation)
  recoverReplacedGenerations()
}

/** Hands every lifecycle of a replaced generation that no surface shows and
 * that holds unconfirmable local changes to the recovery store. */
function recoverReplacedGenerations(): void {
  const store = replacedGenerationRecoveryStore
  if (!store) return
  for (const [key, entry] of [...lifecycleRegistry.entries()]) {
    if (entry.references > 0) continue
    const parts = lifecycleKeyParts(key)
    if (!parts) continue
    const newest = newestKnownGenerations.get(parts.documentKey) ?? parts.generation
    if (newest <= parts.generation) continue
    const { controller } = entry
    if (!controller.holdsUnconfirmableLocalChanges()) continue
    const snapshot = controller.getSnapshot()
    const suffix = `:${snapshot.documentId}:g${parts.generation}`
    if (!snapshot.document || !snapshot.documentId || !key.endsWith(suffix)) continue
    let kept = false
    try {
      kept = store({
        document: snapshot.document,
        documentId: snapshot.documentId,
        generation: parts.generation,
        scope: key.slice(0, -suffix.length),
      })
    } catch (error) {
      console.warn(
        'The local changes of a replaced collaboration document generation '
        + `could not be kept for recovery: ${messageFromError(error)}`,
      )
    }
    if (!kept) continue
    clearLinger(entry)
    lifecycleRegistry.delete(key)
    lifecycleFailures.delete(key)
    controller.discardAfterRecoveryCapture()
  }
}

/** The document part of a lifecycle key (up to and including `:g`) and its
 * generation; null for a key of another shape. */
function lifecycleKeyParts(key: string): { documentKey: string; generation: number } | null {
  const marker = key.lastIndexOf(':g')
  if (marker === -1) return null
  const generation = Number(key.slice(marker + 2))
  if (!Number.isSafeInteger(generation)) return null
  return { documentKey: key.slice(0, marker + 2), generation }
}

/** Linger only sessions that are healthy or already self-healing. Terminal
 * states (revoked, incompatible, hard error) must NOT be preserved across a
 * re-entry: a fresh session request is the only path that can observe a
 * re-grant or a fixed deployment. */
function controllerCanLinger(
  controller: CollaborationDocumentController,
): boolean {
  const snapshot = controller.getSnapshot()
  return (
    snapshot.blockingFailure === null
    && snapshot.durabilityStatus !== 'error'
    && snapshot.connectionStatus !== 'access_revoked'
    && snapshot.connectionStatus !== 'error'
    && snapshot.connectionStatus !== 'incompatible'
    && snapshot.connectionStatus !== 'origin_rejected'
  )
}

function clearLinger(entry: LifecycleRegistryEntry): void {
  if (entry.lingerTimer !== null) clearTimeout(entry.lingerTimer)
  entry.lingerTimer = null
  entry.lingerSince = null
}

/** The pre-linger release path: hand the controller to release(), which
 * destroys immediately when durability is settled and otherwise waits for
 * the acknowledgement (the entry stays registered so a remount can still
 * retain it during that wait). */
function finalizeEntryRelease(
  key: string,
  entry: LifecycleRegistryEntry,
): void {
  clearLinger(entry)
  entry.controller.release(
    () => {
      const current = lifecycleRegistry.get(key)
      if (current === entry && current.references === 0) {
        lifecycleRegistry.delete(key)
        lifecycleFailures.delete(key)
      }
    },
    (error) => recordLifecycleFailure(key, error),
  )
}

function enforceLingerCap(): void {
  const lingering = [...lifecycleRegistry.entries()]
    .filter(([, entry]) => entry.references === 0 && entry.lingerTimer !== null)
  if (lingering.length <= MAX_LINGERING_LIFECYCLES) return
  lingering.sort(
    (a, b) => (a[1].lingerSince ?? 0) - (b[1].lingerSince ?? 0),
  )
  for (const [key, entry] of lingering
    .slice(0, lingering.length - MAX_LINGERING_LIFECYCLES)) {
    finalizeEntryRelease(key, entry)
  }
}

/** A new generation supersedes every lingering lifecycle of the same
 * document: their leases and rooms belong to a rebuilt history and can only
 * rot into incompatibility. Mounted surfaces (references > 0) are left to
 * the recovery flow. */
function releaseSupersededGenerations(key: string): void {
  const marker = key.lastIndexOf(':g')
  if (marker === -1) return
  const prefix = key.slice(0, marker + 2)
  for (const [candidateKey, entry] of [...lifecycleRegistry.entries()]) {
    if (candidateKey === key || !candidateKey.startsWith(prefix)) continue
    if (entry.references > 0) continue
    finalizeEntryRelease(candidateKey, entry)
  }
}

export function retireCollaborationDocumentLifecycle({
  documentId,
  generation,
  workspaceId,
}: {
  documentId: string
  generation: number
  workspaceId: string
}): boolean {
  const key = `${workspaceId}:${documentId}:g${generation}`
  const entry = lifecycleRegistry.get(key)
  if (!entry) return false
  clearLinger(entry)
  lifecycleRegistry.delete(key)
  lifecycleFailures.delete(key)
  entry.controller.discardAfterRecoveryCapture()
  return true
}

/** The registry key of a guest's lifecycle for one document generation. */
function guestLifecycleKey(guestId: string, documentId: string, generation: number): string {
  return `guest:${guestId}:${documentId}:g${generation}`
}

/**
 * Disposes a guest's lifecycle once the guest's access has ended. The
 * service will confirm nothing more, and the page has told the guest that
 * unconfirmed changes are lost; neither the document nor a leave prompt
 * for those changes may stay behind. A normal release would keep a
 * controller with unconfirmed changes registered, its document in memory
 * and its leave guard armed. False when no lifecycle is registered.
 */
export function discardGuestCollaborationLifecycle({
  documentId,
  generation,
  guestId,
}: {
  documentId: string
  generation: number
  guestId: string
}): boolean {
  const key = guestLifecycleKey(guestId, documentId, generation)
  const entry = lifecycleRegistry.get(key)
  if (!entry) return false
  clearLinger(entry)
  lifecycleRegistry.delete(key)
  lifecycleFailures.delete(key)
  entry.controller.discardAfterRecoveryCapture()
  return true
}

/** Drops the lifecycle of `key` when its controller holds an update the
 * service rejected, so the next acquire of the key starts the document over
 * from the service's state. The controller and its refused local state are
 * discarded. False, and nothing changes, for any other lifecycle. */
export function discardRejectedCollaborationLifecycle(
  key: string,
  controller: CollaborationDocumentController,
): boolean {
  const entry = lifecycleRegistry.get(key)
  if (!entry || entry.controller !== controller) return false
  if (!controller.getSnapshot().updateRejected) return false
  clearLinger(entry)
  lifecycleRegistry.delete(key)
  lifecycleFailures.delete(key)
  controller.discardAfterRecoveryCapture()
  return true
}

export function collaborationDocumentLifecycleHasUnconfirmedChanges({
  documentId,
  generation,
  workspaceId,
}: {
  documentId: string
  generation: number
  workspaceId: string
}): boolean {
  return lifecycleRegistry
    .get(`${workspaceId}:${documentId}:g${generation}`)
    ?.controller.getSnapshot().hasUnconfirmedLocalChanges === true
}

function recordLifecycleFailure(key: string, error: CollaborationNotice): void {
  const now = Date.now()
  for (const [candidate, failure] of lifecycleFailures) {
    if (failure.expiresAt <= now) lifecycleFailures.delete(candidate)
  }
  if (lifecycleFailures.size >= MAX_RELEASE_FAILURES) {
    const oldest = lifecycleFailures.keys().next().value
    if (oldest !== undefined) lifecycleFailures.delete(oldest)
  }
  lifecycleFailures.set(key, {
    error,
    expiresAt: now + RELEASE_FAILURE_TTL_MS,
  })
}

export function consumeLifecycleFailure(key: string): CollaborationNotice | null {
  const failure = lifecycleFailures.get(key)
  lifecycleFailures.delete(key)
  if (!failure || failure.expiresAt <= Date.now()) return null
  return failure.error
}

export type UseCollaborationDocumentOptions = {
  active: boolean
  apiKey: string | undefined
  document: EditorDocumentRecord | null
  workspaceId: string
}

export function useCollaborationDocument({
  active,
  apiKey,
  document,
  workspaceId,
}: UseCollaborationDocumentOptions): CollaborationDocumentHandle {
  const [state, setState] = useState<CollaborationDocumentHandle>(() => inactiveHandle(null, null))
  // Bumped when a rejected lifecycle was discarded: the effect then acquires
  // a new one under the same key.
  const [reloadRevision, setReloadRevision] = useState(0)
  const documentId = document?.id ?? null
  const generation = document?.collaboration?.generation ?? null
  const persistedSequence = document?.collaboration?.persistedSequence ?? 0
  const schemaVersion = document?.collaboration?.schemaVersion ?? null
  const collaborationActive = active
    && document?.contentMode === 'collaboration'
    && generation !== null
    && schemaVersion !== null
  const requestedInactiveHandle = useMemo(
    () => inactiveHandle(documentId, generation),
    [documentId, generation],
  )

  useEffect(() => {
    if (!collaborationActive || !documentId || generation === null || schemaVersion === null) {
      setState(requestedInactiveHandle)
      return
    }
    const lifecycleRegistryKey = `${workspaceId}:${documentId}:g${generation}`
    const controller = acquireLifecycleController(
      lifecycleRegistryKey,
      apiKey ?? null,
      () => new CollaborationDocumentController({
        documentId,
        generation,
        initialPersistedSequence: persistedSequence,
        requestSession: (leaseToken, rotationCommandId, signal) => createEditorCollaborationSession(
          documentId,
          {
            protocol_version: EDITOR_COLLABORATION_PROTOCOL_VERSION,
            schema_version: EDITOR_SCHEMA_VERSION,
            ...(leaseToken === undefined ? {} : { lease_token: leaseToken }),
            ...(rotationCommandId === undefined
              ? {}
              : { rotation_command_id: rotationCommandId }),
          },
          { apiKey, signal, workspaceId },
        ),
        resolveWebSocketUrl: collaborationWebSocketUrl,
        schemaVersion,
      }),
    )
    const unsubscribe = controller.subscribe(setState)
    const stopReload = controller.onDocumentReload(() => {
      if (discardRejectedCollaborationLifecycle(lifecycleRegistryKey, controller)) {
        setReloadRevision((revision) => revision + 1)
      }
    })
    setState(controller.getSnapshot())
    void controller.start()
    return () => {
      stopReload()
      unsubscribe()
      releaseLifecycleController(lifecycleRegistryKey, controller)
    }
  }, [
    active,
    apiKey,
    collaborationActive,
    documentId,
    generation,
    reloadRevision,
    schemaVersion,
    requestedInactiveHandle,
    workspaceId,
  ])

  return collaborationHandleForRequestedDocument(
    state,
    documentId,
    generation,
    collaborationActive,
    requestedInactiveHandle,
  )
}

export function useGuestCollaborationDocument({
  access,
  active,
}: {
  access: EditorGuestAccessSession | null
  active: boolean
}): CollaborationDocumentHandle {
  const documentId = access?.document.id ?? null
  const generation = access?.document.generation ?? null
  const persistedSequence = access?.document.persisted_sequence ?? 0
  const guestId = access?.guest.id ?? null
  const displayName = access?.guest.display_name ?? undefined
  const collaborationActive = (
    active
    && documentId !== null
    && generation !== null
    && guestId !== null
  )
  const requestedInactiveHandle = useMemo(
    () => inactiveHandle(documentId, generation),
    [documentId, generation],
  )
  const [state, setState] = useState<CollaborationDocumentHandle>(
    () => requestedInactiveHandle,
  )
  // Bumped when a rejected lifecycle was discarded: the effect then acquires
  // a new one under the same key.
  const [reloadRevision, setReloadRevision] = useState(0)

  useEffect(() => {
    if (
      !collaborationActive
      || documentId === null
      || generation === null
      || guestId === null
    ) {
      setState(requestedInactiveHandle)
      return
    }
    const lifecycleRegistryKey = guestLifecycleKey(guestId, documentId, generation)
    const controller = acquireLifecycleController(
      lifecycleRegistryKey,
      null,
      () => new CollaborationDocumentController({
        documentId,
        generation,
        initialPersistedSequence: persistedSequence,
        requestSession: (leaseToken, rotationCommandId, signal) => (
          createGuestEditorCollaborationSession({
            protocol_version: EDITOR_COLLABORATION_PROTOCOL_VERSION,
            schema_version: EDITOR_SCHEMA_VERSION,
            ...(displayName === undefined
              ? {}
              : { display_name: displayName }),
            ...(leaseToken === undefined
              ? {}
              : { lease_token: leaseToken }),
            ...(rotationCommandId === undefined
              ? {}
              : { rotation_command_id: rotationCommandId }),
          }, { signal })
        ),
        resolveWebSocketUrl: collaborationWebSocketUrl,
        schemaVersion: EDITOR_SCHEMA_VERSION,
      }),
    )
    const unsubscribe = controller.subscribe(setState)
    const stopReload = controller.onDocumentReload(() => {
      if (discardRejectedCollaborationLifecycle(lifecycleRegistryKey, controller)) {
        setReloadRevision((revision) => revision + 1)
      }
    })
    setState(controller.getSnapshot())
    void controller.start()
    return () => {
      stopReload()
      unsubscribe()
      releaseLifecycleController(lifecycleRegistryKey, controller)
    }
  }, [
    collaborationActive,
    displayName,
    documentId,
    generation,
    guestId,
    persistedSequence,
    reloadRevision,
    requestedInactiveHandle,
  ])

  return collaborationHandleForRequestedDocument(
    state,
    documentId,
    generation,
    collaborationActive,
    requestedInactiveHandle,
  )
}

/** Never expose a retained lifecycle snapshot under a newly requested
 * document while React is waiting to run the switch effect. */
export function collaborationHandleForRequestedDocument(
  handle: CollaborationDocumentHandle,
  documentId: string | null,
  generation: number | null,
  active: boolean,
  inactive = inactiveHandle(documentId, generation),
): CollaborationDocumentHandle {
  if (
    active
    && documentId !== null
    && generation !== null
    && handle.documentId === documentId
    && handle.generation === generation
  ) return handle
  return inactive
}

export function collaborationWebSocketUrl(
  path: string,
  location: Pick<Location, 'host' | 'protocol'> = window.location,
): string {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:'
  const normalizedPath = path.startsWith('/') ? path : `/${path}`
  return `${protocol}//${location.host}${normalizedPath}`
}

export function leaseRefreshDelayMs(expiresAt: number, nowMs: number): number {
  const remainingMs = expiresAt * 1_000 - nowMs
  return Math.max(1_000, Math.floor(remainingMs / 2))
}

export function collaborationReconnectDelayMs(
  attempt: number,
  randomValue: number,
): number {
  const safeAttempt = Math.max(1, Math.floor(attempt))
  const baseDelay = COLLABORATION_RECONNECT_DELAYS_MS[
    Math.min(safeAttempt - 1, COLLABORATION_RECONNECT_DELAYS_MS.length - 1)
  ] ?? COLLABORATION_RECONNECT_DELAYS_MS.at(-1)!
  const normalizedRandom = Number.isFinite(randomValue)
    ? Math.min(1, Math.max(0, randomValue))
    : 0.5
  const jitteredDelay = Math.round(baseDelay * (0.85 + normalizedRandom * 0.3))
  return Math.min(COLLABORATION_RECONNECT_DELAYS_MS.at(-1)!, jitteredDelay)
}

export const COLLABORATION_AWARENESS_THROTTLE_MS = 75

type SocketEnd = { code: number; reason: string }

/**
 * A provider websocket that writes only to an open socket, never queues,
 * and leaves every reconnect to the controller.
 *
 * The base class queues every message produced while the socket is not open
 * and flushes that queue on the next socket as soon as the server answers,
 * ahead of anything the controller sends after authentication. Queued
 * document updates would then reach the server before the ordered replay of
 * their predecessors. Nothing is lost by dropping them: the provider's open
 * handshake sends the token and the sync request again, the own presence is
 * announced anew once the document synchronized, and every local document
 * update stays in the controller's log until it is durably acknowledged.
 *
 * The base class ends a socket in three ways: its close event, its liveness
 * check, which after thirty seconds without a server message calls its own
 * close handler directly and so emits no close event, and a close the
 * adapter requests. All three run `onClose`. The override reports every end
 * once and keeps the base class from opening the next socket by itself,
 * which would present an old lease and skip the controller's replay.
 */
class OrderedProviderWebsocket extends HocuspocusProviderWebsocket {
  /** Decides whether a frame may be written; a refused frame is dropped. */
  admitFrame: (frame: Uint8Array) => boolean = () => true
  /** The adapter closes the socket itself; that end is not reported. */
  closeRequested = false
  /** Receives every end of a socket once the base class released it. */
  socketEnded: (socket: number, event: SocketEnd, requested: boolean) => void = () => undefined

  override send(message: Parameters<HocuspocusProviderWebsocket['send']>[0]): void {
    const socket = this.webSocket
    if (socket?.readyState !== WebSocket.OPEN) return
    if (message instanceof Uint8Array && !this.admitFrame(message)) return
    socket.send(message)
  }

  override onClose(parameters: Parameters<HocuspocusProviderWebsocket['onClose']>[0]): void {
    // A second call for a socket the base class already released ends
    // nothing.
    const socket = this.webSocket === null ? null : this.identifier
    const requested = this.closeRequested
    this.closeRequested = false
    this.shouldConnect = false
    super.onClose(parameters)
    if (socket !== null) this.socketEnded(socket, parameters.event, requested)
  }
}

// The y-protocols sync message that answers a sync request
// (`messageYjsSyncStep2`).
const YJS_SYNC_ANSWER = 1

/** The message type of a provider frame and, for a sync message, its sync
 * step: after the document name. Null for a frame too short to tell. */
function providerFrameKind(frame: Uint8Array): { message: number; step: number | null } | null {
  let offset = 0
  const readVarUint = (): number | null => {
    let value = 0
    let factor = 1
    while (offset < frame.byteLength) {
      const byte = frame[offset]!
      offset += 1
      value += (byte & 0x7f) * factor
      if ((byte & 0x80) === 0) return value
      factor *= 128
    }
    return null
  }
  const nameLength = readVarUint()
  if (nameLength === null) return null
  offset += nameLength
  const message = readVarUint()
  if (message === null) return null
  return { message, step: message === MessageType.Sync ? readVarUint() : null }
}

/** A provider that knows, while it applies a frame, whether the frame is the
 * room's answer to its sync request. The provider applies every room change
 * with itself as the origin, whichever frame carried it. */
class RoomAnswerProvider extends HocuspocusProvider {
  applyingSyncAnswer = false

  override onMessage(event: MessageEvent): void {
    const kind = providerFrameKind(new Uint8Array(event.data as ArrayBuffer))
    this.applyingSyncAnswer = kind?.message === MessageType.Sync && kind.step === YJS_SYNC_ANSWER
    try {
      super.onMessage(event)
    } finally {
      this.applyingSyncAnswer = false
    }
  }
}

export function createHocuspocusProvider({
  document,
  events,
  getToken,
  holdsUnsentLocalUpdates,
  mayAnswerSync,
  room,
  url,
}: CollaborationProviderFactoryOptions): CollaborationProviderAdapter {
  const websocket = new OrderedProviderWebsocket({ autoConnect: false, url })
  // Socket identities only grow, so the highest one whose end was reported
  // or requested stands for all earlier ones: one report per socket, however
  // many ways it ended.
  let endedSocket = 0
  const reportEnd = (socket: number, event: SocketEnd): void => {
    if (socket <= endedSocket) return
    endedSocket = socket
    events.onClose(event.code, event.reason)
  }
  // A close the service sends in band arrives while its socket is the
  // current one and still open.
  const handleInBandClose = ({ event }: { event: SocketEnd }): void => {
    reportEnd(websocket.identifier, event)
  }
  // Own presence frames leave only through the adapter's own write path.
  let writingOwnPresence = false
  websocket.admitFrame = (frame) => {
    const kind = providerFrameKind(frame)
    if (kind?.message === MessageType.Awareness && !writingOwnPresence) {
      // The provider's opening handshake repeats the presence of the last
      // socket, ahead of the sync and of the replay of the changes its cursor
      // may point into. The adapter announces the presence anew once the
      // document synchronized.
      return false
    }
    if (kind?.message !== MessageType.Sync || kind.step !== YJS_SYNC_ANSWER) return true
    return mayAnswerSync()
  }
  const provider = new RoomAnswerProvider({
    document,
    name: room,
    onAuthenticated: events.onAuthenticated,
    onAuthenticationFailed: ({ reason }) => events.onAuthenticationFailed(reason),
    onClose: handleInBandClose,
    onStateless: ({ payload }) => events.onStateless(payload),
    onSynced: ({ state }) => {
      if (state) events.onSynced()
    },
    token: getToken,
    websocketProvider: websocket,
  })
  // Local document updates reach the server only through the controller,
  // which owns their order across reconnects (`sendUpdate`). Remote updates
  // are applied by the provider itself and are unaffected.
  document.off('update', provider.boundDocumentUpdateHandler)
  // With an externally managed websocketProvider, Hocuspocus 4.x does NOT
  // auto-attach the provider (manageSocket=false skips attach() in the
  // constructor). Without this attach the provider never registers on the
  // socket, its onOpen never fires, and the authentication token is NEVER
  // sent. The connection would remain unauthenticated until teardown and
  // every participant, including the owner, would stay read-only. Unit
  // fakes replace this factory entirely, so this integration contract needs
  // a test that drives the real provider.
  provider.attach()
  // attach() also registers the provider's close option on the websocket's
  // close event. Every socket end reaches the controller through the
  // websocket's own close handler instead, so the option serves only the
  // close the service sends in band; on the close event it would run after
  // the controller handled the end, when a socket opened meanwhile would be
  // taken for the one that ended.
  websocket.off('close', handleInBandClose)
  websocket.socketEnded = (socket, event, requested) => {
    // The provider forgets its authentication, its sync state and the other
    // participants' presence for every end, so that the next socket's sync
    // reports the document synchronized again.
    provider.onClose()
    if (requested) {
      endedSocket = Math.max(endedSocket, socket)
      return
    }
    reportEnd(socket, event)
  }
  // Hocuspocus 4.3's default awareness handler re-broadcasts EVERY changed
  // client — including states just RECEIVED from the server (no origin
  // filter). The sidecar's identity gate rejects any client message that
  // carries more than the sender's own state (close 4403, e.g. with two
  // participants + a stale clientID after a reload) and would rewrite
  // a foreign state to the sender's identity otherwise. Forward only OUR
  // OWN awareness changes; the server relays everyone else authoritatively.
  const awareness = provider.awareness
  const isSocketOpen = () => websocket.webSocket?.readyState === WebSocket.OPEN
  let disposeAwarenessForwarding = () => undefined
  let leaveAwareness = () => undefined
  let localUpdatesWritten = () => undefined
  let restoreAwareness = () => undefined
  let setAwarenessForwarding = (enabled: boolean) => {
    void enabled
  }
  if (awareness) {
    awareness.off('update', provider.boundAwarenessUpdateHandler)
    let forwarding = true
    let pendingOwnUpdate = false
    let lastOwnState: Record<string, unknown> | null = null
    let lastWrittenAt = Number.NEGATIVE_INFINITY
    let timer: ReturnType<typeof setTimeout> | null = null
    const cancelTimer = () => {
      if (timer === null) return
      clearTimeout(timer)
      timer = null
    }
    const writeOwnState = () => {
      const ownClientId = document.clientID
      const removed = awareness.getLocalState() === null
      pendingOwnUpdate = false
      lastWrittenAt = Date.now()
      writingOwnPresence = true
      try {
        provider.awarenessUpdateHandler({
          added: [],
          removed: removed ? [ownClientId] : [],
          updated: removed ? [] : [ownClientId],
        }, 'local')
      } finally {
        writingOwnPresence = false
      }
    }
    // A cursor points at the text the editor holds. An observer that does
    // not hold that text yet cannot place the cursor and drops it until the
    // text arrives, so a waiting presence change follows the local updates
    // that are still unsent (localUpdatesWritten).
    const writeWhenReady = () => {
      if (!pendingOwnUpdate || !forwarding) return
      // A removal points at no text and never waits.
      if (awareness.getLocalState() !== null && holdsUnsentLocalUpdates()) return
      writeOwnState()
    }
    const flushOwnUpdate = () => {
      timer = null
      writeWhenReady()
    }
    const handleAwarenessUpdate = (
      changes: { added: number[]; removed: number[]; updated: number[] },
      origin: unknown,
    ) => {
      // Incoming server messages are applied with the provider as origin.
      // Sending them back can form an awareness echo, including for our own
      // clientID when the server normalized the state.
      if (origin === provider) return
      const ownClientId = document.clientID
      if (
        !changes.added.includes(ownClientId)
        && !changes.removed.includes(ownClientId)
        && !changes.updated.includes(ownClientId)
      ) return
      lastOwnState = awareness.getLocalState() ?? lastOwnState
      pendingOwnUpdate = true
      if (forwarding && timer === null) {
        timer = setTimeout(flushOwnUpdate, COLLABORATION_AWARENESS_THROTTLE_MS)
      }
    }
    awareness.on('update', handleAwarenessUpdate)
    disposeAwarenessForwarding = () => {
      awareness.off('update', handleAwarenessUpdate)
      pendingOwnUpdate = false
      cancelTimer()
    }
    localUpdatesWritten = () => {
      // A running throttle timer checks again when it runs out; otherwise
      // the change goes directly behind the update frame, at most once per
      // throttle interval.
      if (!pendingOwnUpdate || !forwarding || timer !== null) return
      const waitMs = lastWrittenAt + COLLABORATION_AWARENESS_THROTTLE_MS - Date.now()
      if (waitMs > 0) {
        timer = setTimeout(flushOwnUpdate, waitMs)
        return
      }
      writeWhenReady()
    }
    setAwarenessForwarding = (enabled) => {
      forwarding = enabled
      if (!enabled) {
        cancelTimer()
        return
      }
      // Announce the presence with a new clock. The room may still hold
      // this editor's state from an earlier socket with the same clock, and
      // an update with a clock no newer than the known one is ignored.
      const state = awareness.getLocalState()
      if (state !== null && isSocketOpen()) awareness.setLocalState(state)
      if (!pendingOwnUpdate) return
      cancelTimer()
      writeWhenReady()
    }
    leaveAwareness = () => {
      lastOwnState = awareness.getLocalState() ?? lastOwnState
      // Also written when the provider's own page-hide listener removed the
      // state first: that removal only waits for the throttle. A removal
      // points at no text, so it never waits for local updates.
      if (awareness.getLocalState() !== null) awareness.setLocalState(null)
      cancelTimer()
      pendingOwnUpdate = false
      if (isSocketOpen()) writeOwnState()
    }
    restoreAwareness = () => {
      if (awareness.getLocalState() !== null || lastOwnState === null) return
      awareness.setLocalState(lastOwnState)
    }
  }
  return {
    applyingSyncAnswer: () => provider.applyingSyncAnswer,
    connect: async () => {
      await websocket.connect()
    },
    destroy: () => {
      disposeAwarenessForwarding()
      provider.destroy()
      websocket.destroy()
    },
    disconnect: () => {
      const closing = websocket.webSocket !== null
      websocket.disconnect()
      if (!closing) return
      // Finish the close here instead of waiting for the socket's close
      // event. A close handshake can take longer than the reconnect delay:
      // until its event arrives the websocket still reports itself connected,
      // so `connect()` would return without opening a socket, and the late
      // event would then have to be told apart from the close of the next
      // attempt. Detaching the socket now leaves no event to come; the
      // websocket's close handler resets its state without reconnecting, the
      // provider forgets its authentication and sync state as it does for
      // any end, and a requested end is not reported.
      websocket.closeRequested = true
      websocket.onClose({ event: { code: 1000, reason: 'client_disconnect' } })
    },
    hasSocket: () => websocket.webSocket !== null,
    isSocketOpen,
    leaveAwareness,
    localUpdatesWritten: () => localUpdatesWritten(),
    provider,
    restoreAwareness,
    sendStateless: (payload) => provider.sendStateless(payload),
    sendUpdate: (update) => provider.documentUpdateHandler(update, LOCAL_TRANSPORT_ORIGIN),
    setAwarenessForwarding,
    setAwarenessUser: (user) => provider.setAwarenessField('user', user),
    socketId: () => websocket.identifier,
    syncToken: () => provider.sendToken(),
  }
}

/** The page lifecycle as browsers report it: `pagehide` is the last event
 * every engine fires before unloading a page or caching it, and a hidden
 * document is the last state mobile engines report before discarding a
 * background page. `beforeunload` is observed only while a leave guard is
 * set, so a page without pending changes stays eligible for the
 * back/forward cache. Some engines close the page's sockets before
 * `pagehide` when the page itself reloads or navigates, so the page exit
 * also runs at `beforeunload`, while the socket is still open, and the
 * browser then asks only when the guard says that changes remain. */
export function observeBrowserPageLifecycle(
  handlers: CollaborationPageLifecycleHandlers,
): CollaborationPageLifecycleRegistration {
  if (typeof window === 'undefined') {
    return { dispose: () => undefined, setLeaveGuard: () => undefined }
  }
  let guard: (() => boolean) | null = null
  const handlePageHide = () => handlers.onExit('pagehide')
  const handlePageShow = (event: PageTransitionEvent) => handlers.onShow(event.persisted)
  const handleVisibilityChange = () => {
    if (window.document.visibilityState === 'hidden') handlers.onExit('hidden')
  }
  const handleBeforeUnload = (event: BeforeUnloadEvent) => {
    handlers.onExit('beforeunload')
    if (!guard?.()) return
    event.preventDefault()
    // Engines that still need the legacy form of the question.
    event.returnValue = ''
  }
  window.addEventListener('pagehide', handlePageHide)
  window.addEventListener('pageshow', handlePageShow)
  window.document.addEventListener('visibilitychange', handleVisibilityChange)
  return {
    dispose: () => {
      window.removeEventListener('pagehide', handlePageHide)
      window.removeEventListener('pageshow', handlePageShow)
      window.document.removeEventListener('visibilitychange', handleVisibilityChange)
      if (guard !== null) window.removeEventListener('beforeunload', handleBeforeUnload)
      guard = null
    },
    setLeaveGuard: (next) => {
      if (next === null) {
        if (guard !== null) window.removeEventListener('beforeunload', handleBeforeUnload)
        guard = null
        return
      }
      if (guard === null) window.addEventListener('beforeunload', handleBeforeUnload)
      guard = next
    },
  }
}

const browserDependencies: CollaborationDocumentControllerDependencies = {
  cancelTimer: (timer) => window.clearTimeout(timer),
  createCommandId: collaborationCommandId,
  createDocument: () => new Y.Doc(),
  createProvider: createHocuspocusProvider,
  hashUpdate: collaborationSha256Hex,
  now: () => Date.now(),
  observePageLifecycle: observeBrowserPageLifecycle,
  random: secureRandomUnitInterval,
  scheduleTimer: (callback, delayMs) => window.setTimeout(callback, delayMs),
}

function inactiveHandle(
  documentId: string | null,
  generation: number | null,
): CollaborationDocumentHandle {
  const authority: CollaborationLiveAuthority = {
    access: null,
    blockingFailure: null,
    canEdit: false,
    connectionStatus: 'inactive',
    documentId,
    generation,
    lifecycleStatus: 'inactive',
    revision: 0,
    synced: false,
  }
  return {
    access: null,
    activityRevision: 0,
    authorityRevision: 0,
    blockingFailure: null,
    canEdit: false,
    commentEventVersion: 0,
    commentMentionEventVersion: 0,
    connectionStatus: 'inactive',
    document: null,
    documentId,
    durabilityDelayed: false,
    durabilityStatus: 'idle',
    endLocalWriteMode: () => undefined,
    error: null,
    flushAndAwaitDurability: async () => undefined,
    lastPersistedSequence: 0,
    lastLocalDurableSequence: 0,
    generation,
    hasUnconfirmedLocalChanges: false,
    lifecycleStatus: 'inactive',
    lifecycleKey: 'inactive',
    liveDocumentReady: false,
    markLocalWriteMode: () => undefined,
    pendingHashes: [],
    provider: null,
    readAuthority: () => authority,
    reconnectAttempt: 0,
    recoverability: 'none',
    reloadDocument: () => undefined,
    retryConnection: async () => undefined,
    nextReconnectAt: null,
    setAuthoritativeSequence: () => undefined,
    synced: false,
    flushAndAwaitDurable: async () => 0,
    unconfirmedSince: null,
    updateAuthoritativeSequence: () => undefined,
    updateRejected: false,
    user: null,
  }
}

function collaborationAuthorityChanged(
  previous: CollaborationDocumentHandle,
  next: CollaborationDocumentHandle,
): boolean {
  return previous.access !== next.access
    || previous.blockingFailure !== next.blockingFailure
    || previous.canEdit !== next.canEdit
    || previous.connectionStatus !== next.connectionStatus
    || previous.documentId !== next.documentId
    || previous.generation !== next.generation
    || previous.lifecycleStatus !== next.lifecycleStatus
    || previous.synced !== next.synced
}

function deterministicUser(user: EditorCollaborationUser): EditorCollaborationUser {
  return { color: user.color, id: user.id, name: user.name }
}

function secureRandomUnitInterval(): number {
  const value = globalThis.crypto.getRandomValues(new Uint32Array(1))[0] ?? 0
  return value / 0x1_0000_0000
}

function requestStatus(error: unknown): number | undefined {
  return error instanceof Error
    ? (error as InqtrixRequestError).status
    : undefined
}

function requestReason(error: unknown): string {
  if (!(error instanceof Error)) return ''
  const reason = (error as InqtrixRequestError).detail?.reason
  return typeof reason === 'string' ? reason : ''
}

/**
 * The update to send for a batch the transport integrated. When a remote
 * change the transport received while the batch was held deleted the parent
 * of some of the batch's structs, the transport stores them as collected
 * garbage and emits them that way, and the service's suggestion rules accept
 * no such struct. The batch's own structs beyond the transport's earlier
 * state are then sent as the items they are: the room integrates them into
 * the deleted parent and ends in the same state. Any other emitted update is
 * sent unchanged.
 */
function withOwnItems(
  emitted: Uint8Array,
  batch: Uint8Array,
  transportVector: Uint8Array,
): Uint8Array {
  const collected = Y.decodeUpdate(emitted).structs
    .some((struct) => !(struct instanceof Y.Item))
  if (!collected) return emitted
  return Y.diffUpdate(batch, transportVector)
}

/**
 * Whether applying a batch changed the transport document although the
 * transport emitted no update for it: the batch's transaction inserted
 * structs or deleted items, the condition under which Yjs emits one. A batch
 * without a transaction of its own (null) joined a transaction already
 * running, whose update Yjs emits later under that transaction's origin; it
 * counts as changed, since nothing it carried can have been emitted for it.
 */
function transportTookIn(
  transport: Y.Doc,
  transaction: Y.Transaction | null,
  vectorBefore: Uint8Array,
): boolean {
  if (transaction === null || transaction.deleteSet.clients.size > 0) return true
  const before = Y.decodeStateVector(vectorBefore)
  for (const [client, clock] of Y.decodeStateVector(Y.encodeStateVector(transport))) {
    if ((before.get(client) ?? 0) !== clock) return true
  }
  return false
}

type DeletedRanges = ReadonlyArray<{ client: number; clock: number; length: number }>

/** The item ranges a transaction's delete set names. */
function deletedRanges(deleteSet: Y.Transaction['deleteSet']): DeletedRanges {
  return [...deleteSet.clients.entries()].flatMap(([client, ranges]) => (
    ranges.map((range) => ({ client, clock: range.clock, length: range.len }))
  ))
}

/** The notice for a close that refuses a local update of this editor, or
 * null for any other close. The reason decides, because a close the service
 * sends in band reaches the provider with its code rewritten to 1000; a
 * close without a reason for a frame that is too big decides by its code,
 * 1009. */
function rejectedUpdateNotice(code: number, reason: string): CollaborationNotice | null {
  if (
    reason.includes('invalid_schema')
    || reason.includes('suggestion_policy_violation')
    || reason.includes('unsupported_suggestion_structure')
  ) return COLLABORATION_NOTICES.update_rejected
  if (
    reason.includes('message_too_large')
    || reason.includes('document_too_large')
    || code === 1009
  ) return COLLABORATION_NOTICES.update_too_large
  return null
}

function isCompatibilityReason(reason: string): boolean {
  // The reasons the collaboration service and the gateway actually emit for
  // "this client cannot proceed without changing": see CollaborationErrorReason
  // in apps/collaboration-server/src/errors.ts.
  return reason.includes('update_required')
    || reason.includes('invalid_schema')
    || reason.includes('generation_mismatch')
    || reason.includes('binary_frames_required')
}

function isCollaborationCommentEvent(
  value: unknown,
  documentId: string,
): value is {
  document_id: string
  type: 'collaboration_comment_changed' | 'collaboration_comment_mentioned'
} {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Record<string, unknown>
  return candidate.document_id === documentId
    && (
      candidate.type === 'collaboration_comment_changed'
      || candidate.type === 'collaboration_comment_mentioned'
    )
}

function isDurableRejection(value: unknown): value is {
  code: string
  hash: string
  type: 'durable_rejection'
} {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Record<string, unknown>
  return candidate.type === 'durable_rejection'
    && typeof candidate.code === 'string'
    && typeof candidate.hash === 'string'
}
