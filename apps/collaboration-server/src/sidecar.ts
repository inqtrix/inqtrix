import { randomUUID } from 'node:crypto'
import { createServer, type IncomingMessage, type Server as HttpServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { monitorEventLoopDelay } from 'node:perf_hooks'
import type { Duplex } from 'node:stream'

import {
  Hocuspocus,
  MessageType,
  type beforeSyncPayload,
  type Connection,
  type Document as HocuspocusDocument,
  type WebSocketLike,
} from '@hocuspocus/server'
import {
  COLLABORATION_INGRESS_OVERFLOW_CLOSE,
  COLLABORATION_PRE_OPEN_QUEUE_FRAMES,
  parseEditorCollaborationRoom,
} from '@inqtrix/editor-schema'
import WebSocket, {
  type RawData,
  WebSocketServer,
} from 'ws'
import * as Y from 'yjs'

import { FastApiCollaborationClient } from './apiClient'
import { CollaborationAuthenticator } from './authenticator'
import {
  enforceAwarenessIdentity,
  removeHocuspocusScratchAwarenessState,
} from './awareness'
import {
  AwarenessOwnership,
  awarenessPrincipal,
  type AwarenessDropReason,
} from './awarenessOwnership'
import type {
  CollaborationApi,
  CollaborationPolicyEvent,
  CollaborationSettings,
  ConnectionContext,
  SidecarLogger,
} from './contracts'
import { DocumentCoordinator } from './documentCoordinator'
import {
  reconstructValidatedDocument,
} from './documentState'
import {
  collaborationError,
  ApiRequestError,
  CloseCodes,
  CollaborationError,
  isDocumentRemoved,
} from './errors'
import { hasBearerSecret, InternalHttpRouter } from './httpRouter'
import { InstanceLeaseManager, reportingFencedAnswers } from './instanceLease'
import { createJsonLogger } from './logger'
import { SidecarMetrics } from './metrics'
import {
  OutboundFlowController,
  type OutboundReservation,
} from './outboundFlow'
import {
  CollaborationOperations,
  type OperationDocumentAccess,
} from './operations'
import { SessionRegistry, SlidingWindowRateLimiter } from './rateLimit'
import { SnapshotRetryController } from './snapshotRetry'
import { VerificationFaultGate } from './verificationFault'

type InternalContext = {
  internalOperation: true
}

type TransportContext = {
  transportId: string
}

type ReconstructionContext = {
  reconstructionEpoch: number
}

type AuthenticationOrderContext = {
  /** Order in which connections authenticated with this service; a higher
   * value authenticated later. */
  authenticationSequence: number
}

type HocuspocusContext = Partial<ConnectionContext>
  & Partial<AuthenticationOrderContext>
  & Partial<InternalContext>
  & Partial<ReconstructionContext>
  & Partial<TransportContext>

type HocuspocusPayload = Parameters<WebSocketLike['send']>[0]

type HeldOutboundFrame = {
  payload: HocuspocusPayload
  reservation: OutboundReservation
  socket: WebSocket
  transportId: string
}

type IngressReservation = {
  /** Set for an awareness frame of an open address, whose reservation holds
   * bytes but no frame slot. */
  awarenessAddress: string | null
  bytes: number
  released: boolean
  transportId: string
}

/** A document frame of an open writable address that waits in the sidecar
 * because the frame budget of its transport is spent. Its bytes are charged
 * to the transport's byte bound while it waits. */
type DeferredFrame = {
  addressKey: string
  bytes: number
  update: Uint8Array
}

/** The latest awareness frame of an open address that waits behind the one
 * already handed to Hocuspocus. Its bytes are charged while it waits. */
type HeldAwarenessFrame = {
  bytes: number
  update: Uint8Array
}

type IngressAdmission =
  | { kind: 'deliver'; awarenessAddress: string | null }
  | { kind: 'defer'; addressKey: string }
  | { kind: 'hold'; addressKey: string }
  | { kind: 'refuse'; reason: IngressRefusal }

type IngressRefusal =
  | typeof COLLABORATION_INGRESS_OVERFLOW_CLOSE.reason
  | 'rate_limited'

type RegisteredConnection = {
  connection: Connection<HocuspocusContext>
  documentName: string
  socketId: string
  transportId: string
}

type TransportTeardown = (code: number, reason: string) => void

/** Why a presence state was not published: an ownership decision, or a
 * connection that already left its room. */
type AwarenessRefusal = AwarenessDropReason | 'closed_connection'

export type CollaborationSidecarDependencies = {
  api?: CollaborationApi
  logger?: SidecarLogger
  metrics?: SidecarMetrics
  shutdownTimeoutMs?: number
  socketCloseGraceMs?: number
  verificationFaults?: VerificationFaultGate | null
}

const EVENT_LOOP_DELAY_RESOLUTION_MS = 20
const DEFAULT_SHUTDOWN_TIMEOUT_MS = 25_000
const DEFAULT_SOCKET_CLOSE_GRACE_MS = 1_000
/** An awareness state as y-protocols encodes a removal: JSON `null`. */
const NULL_STATE = new TextEncoder().encode('null')
/**
 * Stateless messages a client may send per socket within one window. The
 * service answers no client stateless message: every update a client resends
 * is acknowledged as a duplicate or as redundant, so a `durability_reconcile`
 * request adds nothing and is ignored. The budget bounds how often a client
 * can make the service decode such a payload; beyond it, and for any other
 * payload, the socket closes.
 */
const CLIENT_STATELESS_MESSAGES_PER_WINDOW = 10
const CLIENT_STATELESS_WINDOW_MS = 10_000
/**
 * Token renewals one document connection may send on its own within one
 * window. Each renewal introspects the lease through the API, which reads the
 * lease, the account or guest link and the document state, so an unbudgeted
 * stream of small token frames would make the store do that work for every
 * frame. The browser renews on its own once per refresh interval, at least
 * five seconds apart; beyond the budget the socket closes `rate_limited`. A
 * renewal that answers the service's own request for a token is admitted
 * without counting.
 */
const CLIENT_TOKEN_RENEWALS_PER_WINDOW = 10
const CLIENT_TOKEN_RENEWAL_WINDOW_MS = 10_000
/**
 * The message types a browser's provider sends: sync, presence,
 * authentication, presence query, stateless and the close of a document.
 * Hocuspocus has no handler for any other type and writes an error line to
 * the log for every such frame, so a socket that sends one is closed before
 * the frame reaches it.
 */
const CLIENT_MESSAGE_TYPES: ReadonlySet<number> = new Set([
  MessageType.Sync,
  MessageType.Awareness,
  MessageType.Auth,
  MessageType.QueryAwareness,
  MessageType.Stateless,
  MessageType.CLOSE,
])
/** The kind of authentication message that carries a token, the only kind
 * a browser sends (`AuthMessageType.Token` of the Hocuspocus protocol). */
const TOKEN_AUTHENTICATION_MESSAGE = 0
/**
 * Document frames an open connection with edit or suggest access may have
 * waiting in the sidecar, per frame of the transport's frame budget, while
 * earlier frames are processed. Beyond it, or beyond the byte bound, the
 * socket closes `ingress_overflow`.
 */
const DEFERRED_FRAMES_PER_QUEUED_FRAME = 4

export class CollaborationSidecar {
  private readonly api: CollaborationApi
  private authenticationSequence = 0
  private readonly authenticator: CollaborationAuthenticator
  private readonly awarenessLimiter: SlidingWindowRateLimiter
  private readonly awarenessOwnership = new AwarenessOwnership<
    HocuspocusDocument,
    Connection<HocuspocusContext>
  >()
  /** Refusal reasons already logged per connection: every refused frame is
   * counted, but a connection is warned about once per reason. */
  private readonly awarenessWarnings = new WeakMap<
    Connection<HocuspocusContext>,
    Set<AwarenessRefusal>
  >()
  private readonly coordinator: DocumentCoordinator
  /** Delay of the event loop every document shares, sampled between two
   * scrapes. */
  private readonly eventLoopDelay = monitorEventLoopDelay({
    resolution: EVENT_LOOP_DELAY_RESOLUTION_MS,
  })
  private readonly hocuspocus: Hocuspocus<HocuspocusContext>
  private readonly httpServer: HttpServer
  private readonly heldOutbound = new Map<string, HeldOutboundFrame[]>()
  private readonly authenticatedAddresses = new Set<string>()
  /** Awareness frame handed to Hocuspocus and not yet processing, per open
   * address. */
  private readonly awarenessInFlight = new Map<string, IngressReservation>()
  /** Transports whose deferred frames are being handed to Hocuspocus. */
  private readonly drainingIngress = new Set<string>()
  private readonly deferredIngress = new Map<string, DeferredFrame[]>()
  private readonly deferredIngressByAddress = new Map<string, number>()
  private deferredIngressFrames = 0
  private readonly heldAwareness = new Map<string, HeldAwarenessFrame>()
  private readonly ingressAuthReservations = new Map<string, IngressReservation[]>()
  private readonly ingressFrames = new WeakMap<Uint8Array, IngressReservation>()
  private readonly ingressUsage = new Map<string, { bytes: number; frames: number }>()
  private readonly leaseManager: InstanceLeaseManager
  private readonly connectionLeaseTimers = new Map<string, ReturnType<typeof setTimeout>>()
  private readonly connections = new Map<string, RegisteredConnection>()
  private readonly logger: SidecarLogger
  private maintenanceInFlight: Promise<void> | null = null
  private maintenanceTimer: ReturnType<typeof setInterval> | null = null
  private readonly metrics: SidecarMetrics
  /** Document connections that are open, by transport and address. */
  private readonly openDocumentAddresses = new Map<string, Connection<HocuspocusContext>>()
  private readonly operations: CollaborationOperations
  private readonly outboundFlow: OutboundFlowController<HocuspocusPayload>
  private readonly outboundRejected = new Set<string>()
  private policyCursor = 0
  private policyPollInFlight: Promise<void> | null = null
  private policyTimer: ReturnType<typeof setInterval> | null = null
  private readonly policyRevalidationTimers = new Map<
    string,
    ReturnType<typeof setTimeout>
  >()
  private readonly router: InternalHttpRouter
  /** Document connections whose request for the room state (a sync step 1)
   * was answered. */
  private readonly answeredSyncRequests = new WeakSet<Connection<HocuspocusContext>>()
  /** Connections whose sync or stateless frame the sidecar refused and
   * closed. Their frames that were already queued are not processed. */
  private readonly refusedConnections = new WeakSet<Connection<HocuspocusContext>>()
  private readonly sessions: SessionRegistry
  private readonly shutdownTimeoutMs: number
  private readonly snapshotTasks = new Map<string, Promise<number>>()
  private readonly snapshotRetries: SnapshotRetryController
  /** Loaded room documents whose document the API no longer has, with the
   * refusal their connections close with. They store nothing more and unload
   * once their last connection is gone. Keyed by the loaded document, not the
   * room name, so a later load of the same room starts clean. */
  private readonly removedDocuments = new WeakMap<Y.Doc, CollaborationError>()
  private readonly sockets = new Map<string, WebSocket>()
  private readonly statelessLimiter = new SlidingWindowRateLimiter(
    CLIENT_STATELESS_MESSAGES_PER_WINDOW,
    CLIENT_STATELESS_WINDOW_MS,
  )
  /** Token renewals each document connection sent on its own. */
  private readonly tokenRenewalLimiter = new SlidingWindowRateLimiter(
    CLIENT_TOKEN_RENEWALS_PER_WINDOW,
    CLIENT_TOKEN_RENEWAL_WINDOW_MS,
  )
  /** Document connections the service asked for a token that have not sent
   * one since. */
  private readonly requestedTokenRenewals = new Set<string>()
  private readonly socketCloseGraceMs: number
  private startPromise: Promise<void> | null = null
  private stopPromise: Promise<void> | null = null
  private readonly transportClients = new Map<string, (update: Uint8Array) => void>()
  private readonly transportConnections = new Map<string, Set<string>>()
  private readonly transportTeardowns = new Map<string, TransportTeardown>()
  private readonly updateLimiter: SlidingWindowRateLimiter
  private readonly verificationFaults: VerificationFaultGate | null
  private readonly webSocketServer: WebSocketServer
  private started = false
  private stopping = false

  constructor(
    private readonly settings: CollaborationSettings,
    dependencies: CollaborationSidecarDependencies = {},
  ) {
    this.logger = dependencies.logger ?? createJsonLogger()
    this.metrics = dependencies.metrics ?? new SidecarMetrics()
    this.shutdownTimeoutMs = dependencies.shutdownTimeoutMs ?? DEFAULT_SHUTDOWN_TIMEOUT_MS
    this.socketCloseGraceMs = dependencies.socketCloseGraceMs ?? DEFAULT_SOCKET_CLOSE_GRACE_MS
    this.verificationFaults = dependencies.verificationFaults ?? null
    if (this.shutdownTimeoutMs < 1 || this.socketCloseGraceMs < 0) {
      throw new Error('Shutdown deadlines must be non-negative')
    }
    this.outboundFlow = new OutboundFlowController(
      settings.maxQueuedFrames,
      settings.maxQueuedBytes,
      settings.socketBackpressureBytes,
      WebSocket.OPEN,
      (bytes, frames) => {
        this.metrics.set('inqtrix_collaboration_outbound_queued_bytes', bytes)
        this.metrics.set('inqtrix_collaboration_outbound_queued_frames', frames)
      },
    )
    this.api = reportingFencedAnswers(
      dependencies.api ?? new FastApiCollaborationClient(settings, this.logger, this.metrics),
      (fence) => this.leaseManager.loseLease(fence),
    )
    this.updateLimiter = new SlidingWindowRateLimiter(
      settings.updateRateLimit,
      settings.updateRateWindowMs,
    )
    this.awarenessLimiter = new SlidingWindowRateLimiter(
      settings.awarenessRateLimit,
      settings.awarenessRateWindowMs,
    )
    this.sessions = new SessionRegistry(settings.maxSessionsPerUserDocument)
    this.leaseManager = new InstanceLeaseManager(
      this.api,
      settings,
      this.logger,
      this.metrics,
      () => this.handleLeaseLoss(),
    )
    this.coordinator = new DocumentCoordinator(
      this.api,
      this.leaseManager,
      settings,
      this.logger,
      this.metrics,
      {
        onAuthoritativeApplyFailure: (room) => {
          this.discardHeldOutbound(room)
          const document = this.hocuspocus.documents.get(room)
          if (document) this.closeDocument(document, reloadRequired())
        },
        onAuthoritativeApplySuccess: (room) => this.flushHeldOutbound(room),
      },
    )
    this.authenticator = new CollaborationAuthenticator(
      this.api,
      this.leaseManager,
      settings,
    )
    this.hocuspocus = this.createHocuspocus()
    this.snapshotRetries = new SnapshotRetryController(
      settings.snapshotRetryBaseMs,
      settings.snapshotRetryMaxMs,
      {
        isEligible: (room) => (
          !this.stopping
          && this.hocuspocus.documents.has(room)
          && this.coordinator.hasUnsnapshottedUpdates(room)
        ),
        onFailure: (room, error) => {
          this.recordSnapshotFailure(room, error)
        },
        onSuccess: (room) => {
          const document = this.hocuspocus.documents.get(room)
          if (document?.getConnectionsCount() === 0) {
            void this.hocuspocus.unloadDocument(document)
          }
        },
      },
    )
    const acquireDocument: OperationDocumentAccess = (room) => (
      this.acquireOperationDocument(room)
    )
    this.operations = new CollaborationOperations(
      this.api,
      this.coordinator,
      this.leaseManager,
      settings,
      acquireDocument,
      this.logger,
      this.metrics,
    )
    this.router = new InternalHttpRouter(
      settings,
      this.leaseManager,
      this.operations,
      this.metrics,
      this.logger,
      () => this.isReady(),
    )
    this.webSocketServer = new WebSocketServer({
      clientTracking: false,
      maxPayload: settings.frameLimitBytes,
      noServer: true,
      perMessageDeflate: false,
    })
    this.httpServer = createServer((request, response) => {
      void this.router.handle(request, response).catch(() => {
        this.logger.error('http_router_failed')
        if (!response.headersSent) {
          response.writeHead(500, {
            'Cache-Control': 'no-store',
            'Content-Type': 'application/json; charset=utf-8',
          })
        }
        response.end(JSON.stringify({ error: { reason: 'internal_consistency' } }))
      })
    })
    this.httpServer.on('upgrade', (request, socket, head) => {
      this.handleUpgrade(request, socket, head)
    })
    this.metrics.set('inqtrix_collaboration_active_connections', 0)
    this.metrics.addCollector(() => {
      const delay = this.eventLoopDelay
      this.metrics.set(
        'inqtrix_collaboration_event_loop_delay_seconds',
        delay.count > 0 ? delay.percentile(99) / 1e9 : 0,
      )
      delay.reset()
    })
  }

  get address(): AddressInfo | null {
    const value = this.httpServer.address()
    return value && typeof value === 'object' ? value : null
  }

  start(): Promise<void> {
    if (this.started) return Promise.resolve()
    if (this.startPromise) return this.startPromise
    if (this.stopping) return Promise.reject(new Error('Collaboration sidecar is stopping'))
    this.stopping = false
    const starting = this.performStart().finally(() => {
      if (this.startPromise === starting) this.startPromise = null
    })
    this.startPromise = starting
    return starting
  }

  private async performStart(): Promise<void> {
    try {
      await this.leaseManager.start()
      if (this.stopping) return
      await this.listenHttpServer()
      if (this.stopping) return
      this.started = true
      this.eventLoopDelay.enable()
      this.startMaintenance()
      this.startPolicyPolling()
      this.logger.info('collaboration_sidecar_listening', {
        address: this.settings.bindAddress,
        port: this.address?.port ?? this.settings.port,
      })
    } finally {
      if (!this.started) {
        await this.closeHttpServer()
        await this.leaseManager.stop()
      }
    }
  }

  stop(): Promise<void> {
    if (this.stopPromise) return this.stopPromise
    this.stopping = true
    const starting = this.startPromise
    this.stopPromise = this.performStop(starting).finally(() => {
      this.stopPromise = null
    })
    return this.stopPromise
  }

  private async performStop(starting: Promise<void> | null): Promise<void> {
    const deadline = Date.now() + this.shutdownTimeoutMs
    this.eventLoopDelay.disable()
    this.snapshotRetries.cancelAll()
    const maintenance = this.stopMaintenance()
    const policyPolling = this.stopPolicyPolling()
    const startupLeaseStop = this.started ? null : this.leaseManager.stop()
    const closeHttp = this.closeHttpServer()
    void closeHttp.catch(() => undefined)
    this.closeAllSockets(CloseCodes.restarting, 'restarting')
    this.clearAllConnectionLeaseTimers()
    await this.awaitShutdownPhase('startup', starting, deadline)
    await this.teardownSockets(deadline)
    if (!this.started) {
      await this.awaitShutdownPhase('instance_fence', startupLeaseStop, deadline)
      await this.awaitShutdownPhase('http_server', this.closeHttpServer(), deadline)
      this.webSocketServer.close()
      this.logger.info('collaboration_sidecar_stopped')
      return
    }
    const gatesDrained = await this.awaitShutdownPhase(
      'document_gates',
      this.coordinator.awaitAll(),
      deadline,
    )
    if (gatesDrained) {
      await this.awaitShutdownPhase(
        'snapshots',
        this.flushSnapshotsForShutdown(),
        deadline,
      )
    }
    await this.awaitShutdownPhase(
      'background_requests',
      Promise.allSettled([maintenance, policyPolling]).then(() => undefined),
      deadline,
    )
    const leaseStop = this.leaseManager.stop()
    await this.awaitShutdownPhase('instance_fence', leaseStop, deadline)
    this.httpServer.closeAllConnections()
    await this.awaitShutdownPhase('http_server', closeHttp, deadline)
    this.webSocketServer.close()
    this.started = false
    this.logger.info('collaboration_sidecar_stopped')
  }

  private listenHttpServer(): Promise<void> {
    return new Promise((resolve, reject) => {
      const onError = (error: Error): void => {
        this.httpServer.off('listening', onListening)
        reject(error)
      }
      const onListening = (): void => {
        this.httpServer.off('error', onError)
        resolve()
      }
      this.httpServer.once('error', onError)
      this.httpServer.once('listening', onListening)
      this.httpServer.listen(this.settings.port, this.settings.bindAddress)
    })
  }

  private closeHttpServer(): Promise<void> {
    if (!this.httpServer.listening) return Promise.resolve()
    return new Promise((resolve, reject) => {
      this.httpServer.close((error) => error ? reject(error) : resolve())
    })
  }

  private createHocuspocus(): Hocuspocus<HocuspocusContext> {
    return new Hocuspocus<HocuspocusContext>({
      debounce: this.settings.snapshotIdleMs,
      maxDebounce: Math.max(this.settings.snapshotIdleMs, 60_000),
      maxPendingDocuments: 8,
      maxUnauthenticatedQueueMessages: COLLABORATION_PRE_OPEN_QUEUE_FRAMES,
      maxUnauthenticatedQueueSize: this.settings.frameLimitBytes,
      name: 'inqtrix-collaboration',
      quiet: true,
      unloadImmediately: true,
      beforeHandleMessage: async ({ connection, context, document, update }) => {
        this.releaseIngress(update)
        if (!this.isReady()) throw unavailable()
        if (!isInternalContext(context)) this.enforceConnectionLease(context)
        if (update.byteLength > this.settings.frameLimitBytes) throw tooLarge()
        if (!isInternalContext(context)) {
          this.applyOwnAwarenessRemoval(document, connection, update)
        }
      },
      onConnect: async ({ context }) => {
        if (this.isReady()) return
        const error = unavailable()
        this.closeSocketAfterHook(context, error)
        throw error
      },
      onAuthenticate: async ({
        connectionConfig,
        context: hookContext,
        documentName,
        socketId,
        token,
      }) => {
        try {
          const context = await this.authenticator.authenticate(documentName, token)
          if (this.verificationFaults?.blocksConnection(context)) {
            throw reloadRequired()
          }
          this.coordinator.assertJoinAllowed(documentName)
          if (!this.sessions.add(context.documentId, context.user.id, socketId)) {
            throw rateLimited()
          }
          connectionConfig.readOnly = context.access === 'view'
          this.authenticationSequence += 1
          return {
            ...context,
            authenticationSequence: this.authenticationSequence,
            reconstructionEpoch: this.coordinator.joinEpoch(documentName),
          }
        } catch (error) {
          const mapped = collaborationError(error)
          this.closeSocketAfterHook(hookContext, mapped)
          throw mapped
        }
      },
      onTokenSync: async ({ connection, context, documentName, token }) => {
        if (!this.admitTokenRenewal(connection, documentName)) return
        return this.renewConnectionToken(connection, context, documentName, token)
      },
      onLoadDocument: async ({ context, documentName, socketId }) => {
        const parsed = parseEditorCollaborationRoom(documentName)
        if (!parsed) throw invalidRoom()
        const authenticated = isInternalContext(context)
          ? null
          : connectionContext(context)
        if (
          authenticated
          && (
            authenticated.documentId !== parsed.documentId
            || authenticated.generation !== parsed.generation
          )
        ) {
          throw incompatible()
        }
        try {
          const state = await this.api.loadDocumentState({
            documentId: parsed.documentId,
            fence: this.leaseManager.assertActive(),
            generation: parsed.generation,
          })
          let selectedUpdates = state.updates
          const reconstructedValidation = await reconstructValidatedDocument(state, {
            documentId: parsed.documentId,
            generation: parsed.generation,
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
              selectedUpdates = [...updates]
            },
          })
          const document = reconstructedValidation.document
          try {
            this.coordinator.initialize(documentName, state.persistedSequence, {
              bytes: selectedUpdates.reduce(
                (total, update) => total + update.update.byteLength,
                0,
              ),
              updates: selectedUpdates.length,
            }, reconstructedValidation)
            return reconstructedValidation.encodedState.slice()
          } finally {
            document.destroy()
          }
        } catch (error) {
          if (authenticated) {
            this.sessions.delete(authenticated.documentId, authenticated.user.id, socketId)
          }
          throw collaborationError(error)
        }
      },
      afterLoadDocument: async ({ document, documentName }) => {
        if (this.coordinator.shouldSnapshot(documentName)) {
          this.queueSnapshot(documentName, document)
        }
      },
      beforeSync: async (payload) => {
        const { connection, type } = payload
        if (type === 0) {
          // A document connection asks for the room state once, when it
          // opens. Each answer encodes the whole document for it, so a
          // repeated request is refused instead of answered.
          if (!this.answeredSyncRequests.has(connection)) {
            this.answeredSyncRequests.add(connection)
            return
          }
          if (!this.refusedConnections.has(connection)) {
            this.refusedConnections.add(connection)
            this.metrics.increment('inqtrix_collaboration_rejections_total', {
              reason: 'rate_limited',
            })
          }
          throw rateLimited()
        }
        if (this.refusedConnections.has(connection)) {
          // A refused frame closed this connection, and Hocuspocus still
          // drains the frames it had queued. They can depend on the refused
          // one, and the client's own sync answer among them would persist
          // everything the room lacks under a hash no unconfirmed update
          // carries, so the client's resent updates would never be
          // acknowledged. The read-only path neither applies nor
          // acknowledges them; the client resends its unconfirmed updates in
          // order after it reconnects.
          connection.readOnly = true
          return
        }
        try {
          await this.admitClientSync(payload)
        } catch (error) {
          this.refusedConnections.add(connection)
          throw error
        }
      },
      afterHandleMessage: async ({ connection, document, documentName }) => {
        try {
          const connectionId = pendingKey(connection.socketId, documentName)
          const pending = this.coordinator.pendingClientUpdate(connectionId)
          const ack = this.coordinator.finishClientUpdate(
            connectionId,
            document,
          )
          if (ack) {
            if (this.coordinator.shouldSnapshot(documentName)) {
              this.queueSnapshot(documentName, document)
            }
            this.flushHeldOutbound(documentName)
            const transportId = connection.context.transportId
            const socket = transportId ? this.sockets.get(transportId) : undefined
            const deliverable = socket?.readyState === WebSocket.OPEN
            if (!deliverable) {
              // The update was received before its transport began closing
              // and is durable and applied; only this acknowledgement has no
              // open socket left, whether the peer went away or the server
              // itself closed it. The client resends the update after it
              // reconnects and receives the acknowledgement as a duplicate
              // then.
              this.metrics.increment(
                'inqtrix_collaboration_acknowledgements_undeliverable_total',
              )
              this.logger.info('durable_ack_undeliverable', { room: documentName })
            }
            if (this.verificationFaults?.triggerLostAcknowledgement(
              connectionContext(connection.context),
              pending,
            )) {
              this.closeConnectionTransport(connection, reloadRequired())
              return
            }
            connection.sendStateless(JSON.stringify(ack))
            if (deliverable) {
              this.verificationFaults?.recordAcknowledgementDelivery(
                connectionContext(connection.context),
                ack,
              )
            }
          }
        } catch (error) {
          const mapped = collaborationError(error)
          this.logger.error('authoritative_update_apply_failed', {
            reason: mapped.reason,
          })
          this.discardHeldOutbound(documentName)
          this.closeDocument(document, mapped)
        }
      },
      beforeHandleAwareness: async ({ connection, context, document, states }) => {
        if (!connection || !context) return
        this.enforceConnectionLease(context)
        if (!this.awarenessLimiter.consume(connection.socketId)) {
          // Cursor and selection changes are ephemeral. Fast typing can
          // legitimately emit more awareness frames than the configured
          // sampling rate; dropping only the excess state keeps the durable
          // document transport healthy while still bounding presence work.
          states.clear()
          this.metrics.increment('inqtrix_collaboration_awareness_dropped_total', {
            reason: 'rate_limited',
          })
          return
        }
        if (removeHocuspocusScratchAwarenessState(states)) {
          this.metrics.increment(
            'inqtrix_collaboration_awareness_scratch_states_removed_total',
          )
        }
        enforceAwarenessIdentity(states, connectionContext(context))
        this.admitAwarenessState(document, connection, context, states)
      },
      onStateless: async ({ connection, payload }) => {
        if (this.refusedConnections.has(connection)) return
        try {
          this.enforceConnectionLease(connection.context)
          if (!this.statelessLimiter.consume(connection.socketId)) throw rateLimited()
          if (!isDurabilityReconcileMessage(payload)) throw invalidRequest()
          this.metrics.increment('inqtrix_collaboration_stateless_messages_ignored_total')
        } catch (error) {
          const mapped = collaborationError(error)
          this.metrics.increment('inqtrix_collaboration_rejections_total', {
            reason: mapped.reason,
          })
          this.logger.warn('client_stateless_refused', { reason: mapped.reason })
          this.refusedConnections.add(connection)
          this.closeConnectionTransport(connection, mapped)
        }
      },
      onChange: async ({ document, documentName }) => {
        if (this.coordinator.shouldSnapshot(documentName)) {
          this.queueSnapshot(documentName, document)
        }
      },
      onStoreDocument: async ({ document, documentName }) => {
        try {
          await this.snapshotCovering(documentName, document)
        } catch (error) {
          this.handleSnapshotFailure(documentName, error)
          // A failed store keeps the room in memory so a retry can save
          // it. A removed document has nothing left to save: ending the hook
          // normally lets the room unload.
          if (isDocumentRemoved(error)) return
          throw error
        }
      },
      connected: async ({ connection, documentName }) => {
        this.ensureConnectionRegistered(connection, documentName)
        this.metrics.set('inqtrix_collaboration_active_connections', this.sockets.size)
        // A connection that finished opening after its room's document was
        // found removed closes like the ones that were already there.
        const removed = this.removedDocuments.get(connection.document)
        if (removed) this.closeConnectionTransport(connection, removed)
      },
      onDisconnect: async ({ context, documentName, socketId }) => {
        if (!isInternalContext(context) && hasConnectionContext(context)) {
          this.sessions.delete(context.documentId, context.user.id, socketId)
        }
        this.updateLimiter.delete(socketId)
        this.awarenessLimiter.delete(socketId)
        this.statelessLimiter.delete(socketId)
        this.tokenRenewalLimiter.delete(pendingKey(socketId, documentName))
        this.discardHeldOutboundForTransport(context.transportId)
        this.unregisterConnection(socketId, documentName)
        this.clearPolicyRevalidationsForSocket(socketId)
      },
      beforeUnloadDocument: async ({ documentName }) => {
        this.snapshotRetries.cancel(documentName)
        await this.coordinator.awaitRoom(documentName)
        if (this.coordinator.requiresReconstruction(documentName)) {
          this.discardHeldOutbound(documentName)
        }
      },
      afterUnloadDocument: async ({ documentName }) => {
        this.snapshotRetries.cancel(documentName)
        this.discardHeldOutbound(documentName)
        this.coordinator.markUnloaded(documentName)
      },
    })
  }

  private async acquireOperationDocument(
    room: string,
  ): Promise<{ document: Y.Doc; release: () => Promise<void> }> {
    const connection = await this.hocuspocus.openDirectConnection(room, {
      internalOperation: true,
    })
    if (!connection.document) {
      throw new CollaborationError('internal_consistency', {
        closeCode: CloseCodes.internalConsistency,
      })
    }
    return {
      document: connection.document,
      release: () => connection.disconnect({ unloadImmediately: true }),
    }
  }

  private handleUpgrade(request: IncomingMessage, socket: Duplex, head: Buffer): void {
    const url = new URL(request.url ?? '/', 'http://collaboration.internal')
    if (
      request.method !== 'GET'
      || url.pathname !== this.settings.websocketPath
      || url.search !== ''
    ) {
      rejectUpgrade(socket, 404, 'Not Found')
      return
    }
    if (!hasBearerSecret(request.headers.authorization, this.settings.secret)) {
      this.metrics.increment('inqtrix_collaboration_websocket_rejections_total', {
        reason: 'unauthorized_gateway',
      })
      rejectUpgrade(socket, 401, 'Unauthorized')
      return
    }
    if (!this.isReady()) {
      rejectUpgrade(socket, 503, 'Service Unavailable')
      return
    }

    try {
      this.webSocketServer.handleUpgrade(request, socket, head, (webSocket) => {
        const transportId = randomUUID()
        const hocusRequest = sanitizedHocuspocusRequest(request, this.settings.websocketPath)
        const guardedSocket = guardedWebSocket(webSocket, (payload) => {
          this.sendHocuspocusPayload(transportId, webSocket, payload)
        })
        const client = this.hocuspocus.handleConnection(
          guardedSocket,
          hocusRequest,
          { transportId },
        )
        this.sockets.set(transportId, webSocket)
        this.metrics.set('inqtrix_collaboration_active_connections', this.sockets.size)
        webSocket.on('message', (data, isBinary) => {
          // The socket library keeps delivering frames while a close the
          // server started waits for the peer's answer. Such a frame may
          // follow one this close refused, so it can depend on an update the
          // room never received. The client resends every unacknowledged
          // update in order after it reconnects, so the frame is dropped.
          if (webSocket.readyState !== WebSocket.OPEN) return
          if (this.stopping) {
            webSocket.close(CloseCodes.restarting, 'restarting')
            return
          }
          if (!isBinary) {
            webSocket.close(CloseCodes.incompatible, 'binary_protocol_required')
            return
          }
          const bytes = rawDataByteLength(data)
          if (bytes > this.settings.frameLimitBytes) {
            webSocket.close(CloseCodes.messageTooLarge, 'message_too_large')
            return
          }
          if (webSocket.bufferedAmount > this.settings.socketBackpressureBytes) {
            this.refuseIngress(transportId, webSocket, 'rate_limited')
            return
          }
          const view = rawDataView(data)
          const header = hocuspocusFrameHeader(view)
          if (header && !isClientMessage(view, header)) {
            this.refuseClientMessage(webSocket, header.type)
            return
          }
          const addressKey = header ? authAddressKey(transportId, header.address) : null
          const admission = this.admitIngress(transportId, addressKey, header?.type, bytes)
          if (admission.kind === 'refuse') {
            this.refuseIngress(transportId, webSocket, admission.reason)
            return
          }
          let update: Uint8Array
          try {
            update = rawDataBytes(data)
          } catch {
            webSocket.close(CloseCodes.incompatible, 'invalid_schema')
            return
          }
          if (admission.kind === 'defer') {
            this.deferIngress(transportId, admission.addressKey, update)
            return
          }
          if (admission.kind === 'hold') {
            this.holdAwareness(transportId, admission.addressKey, update)
            return
          }
          const reservation = this.reserveIngress(
            transportId,
            bytes,
            admission.awarenessAddress,
          )
          if (
            header?.type === MessageType.Auth
            && addressKey
            && !this.authenticatedAddresses.has(addressKey)
          ) {
            const reservations = this.ingressAuthReservations.get(addressKey) ?? []
            reservations.push(reservation)
            this.ingressAuthReservations.set(addressKey, reservations)
          } else {
            this.ingressFrames.set(update, reservation)
          }
          client.handleMessage(update)
        })
        this.transportClients.set(transportId, (update) => client.handleMessage(update))
        let transportClosed = false
        // A closing transport aborts nothing. Hocuspocus keeps processing the
        // frames this connection already delivered: each is validated,
        // persisted and applied in order, and only its acknowledgement is
        // lost. Whether a frame is written is decided by the store's own
        // lease, session and access checks inside its transaction.
        const teardownTransport: TransportTeardown = (code, reason) => {
          if (transportClosed) return
          transportClosed = true
          this.clearTransportConnections(transportId)
          this.clearIngress(transportId)
          this.sockets.delete(transportId)
          this.discardHeldOutboundForTransport(transportId)
          this.outboundFlow.clear(transportId)
          this.outboundRejected.delete(transportId)
          this.transportTeardowns.delete(transportId)
          this.metrics.set('inqtrix_collaboration_active_connections', this.sockets.size)
          client.handleClose({ code, reason })
        }
        this.transportTeardowns.set(transportId, teardownTransport)
        webSocket.on('close', (code, reason) => {
          teardownTransport(code, reason.toString('utf8'))
        })
        webSocket.on('error', () => {
          this.logger.warn('websocket_transport_error')
        })
      })
    } catch {
      this.logger.warn('websocket_upgrade_failed')
      socket.destroy()
    }
  }

  private isReady(): boolean {
    return this.started
      && !this.stopping
      && this.leaseManager.isReady()
      && !this.verificationFaults?.sidecarOutageActive()
  }

  private handleLeaseLoss(): void {
    this.closeAllSockets(CloseCodes.serviceUnavailable, 'instance_lease_lost')
    void this.coordinator.awaitAll().then(() => {
      for (const document of this.hocuspocus.documents.values()) {
        if (document.getConnectionsCount() === 0) {
          void this.hocuspocus.unloadDocument(document)
        }
      }
    })
  }

  private startMaintenance(): void {
    if (this.maintenanceTimer) return
    this.maintenanceTimer = setInterval(() => {
      this.queueMaintenance()
    }, this.settings.maintenanceIntervalMs)
    this.maintenanceTimer.unref()
    this.queueMaintenance()
  }

  private stopMaintenance(): Promise<void> | null {
    if (this.maintenanceTimer) clearInterval(this.maintenanceTimer)
    this.maintenanceTimer = null
    return this.maintenanceInFlight
  }

  private queueMaintenance(): void {
    if (!this.isReady() || this.maintenanceInFlight) return
    const maintenance = this.operations.runMaintenance().finally(() => {
      this.maintenanceInFlight = null
    })
    this.maintenanceInFlight = maintenance
  }

  private startPolicyPolling(): void {
    if (this.policyTimer) return
    this.policyTimer = setInterval(() => {
      this.queuePolicyPoll()
    }, this.settings.policyPollMs)
    this.policyTimer.unref()
    this.queuePolicyPoll()
  }

  private stopPolicyPolling(): Promise<void> | null {
    if (this.policyTimer) clearInterval(this.policyTimer)
    this.policyTimer = null
    for (const timer of this.policyRevalidationTimers.values()) clearTimeout(timer)
    this.policyRevalidationTimers.clear()
    return this.policyPollInFlight
  }

  private queuePolicyPoll(): void {
    if (!this.isReady() || this.policyPollInFlight) return
    const startedAt = performance.now()
    const poll = this.pollPolicyEvents()
      .catch((error) => {
        const mapped = collaborationError(error)
        this.metrics.increment('inqtrix_collaboration_policy_poll_errors_total', {
          reason: mapped.reason,
        })
        this.logger.warn('policy_feed_poll_failed', { reason: mapped.reason })
      })
      .finally(() => {
        this.metrics.observeMilliseconds(
          'inqtrix_collaboration_policy_poll_seconds',
          performance.now() - startedAt,
        )
        this.policyPollInFlight = null
      })
    this.policyPollInFlight = poll
  }

  private async pollPolicyEvents(): Promise<void> {
    const page = await this.api.pollPolicyEvents({
      afterId: this.policyCursor,
      fence: this.leaseManager.assertActive(),
      limit: 500,
    })
    if (this.stopping) return
    if (page.resetRequired) {
      this.metrics.increment('inqtrix_collaboration_policy_resets_total')
      this.logger.warn('policy_feed_reset_required', { cursor: page.cursor })
      this.requestAllConnectionRevalidations()
      this.policyCursor = page.cursor
      this.metrics.set('inqtrix_collaboration_policy_cursor', page.cursor)
      return
    }

    const checkedDocuments = new Map<string, boolean>()
    const commentDocuments = new Set<string>()
    const commentMentions = new Map<string, Set<string>>()
    for (const event of page.events) {
      if (
        event.resourceType === 'editor_document'
        && event.resourceId
        && event.scope === 'collaboration_guest_policy'
      ) {
        this.requestGuestConnectionRevalidations(event)
        continue
      }
      if (
        event.resourceType === 'editor_document'
        && event.resourceId
        && event.scope === 'collaboration_comment_changed'
      ) {
        commentDocuments.add(event.resourceId)
        continue
      }
      if (
        event.resourceType === 'editor_document'
        && event.resourceId
        && event.scope === 'collaboration_comment_mention'
      ) {
        const recipients = commentMentions.get(event.resourceId) ?? new Set<string>()
        recipients.add(event.targetUserId)
        commentMentions.set(event.resourceId, recipients)
        continue
      }
      if (
        event.resourceType === 'editor_document'
        && event.resourceId
        && this.documentConnectionsRequireGenerationCheck(event)
      ) {
        let generationInvalid = checkedDocuments.get(event.resourceId)
        if (generationInvalid === undefined) {
          generationInvalid = await this.closeGenerationMismatches(event.resourceId)
          checkedDocuments.set(event.resourceId, generationInvalid)
        }
        if (generationInvalid) continue
      }
      this.requestAffectedConnectionRevalidations(event)
    }
    for (const documentId of commentDocuments) {
      this.broadcastCommentEvent(
        documentId,
        'collaboration_comment_changed',
      )
    }
    for (const [documentId, recipients] of commentMentions) {
      this.broadcastCommentEvent(
        documentId,
        'collaboration_comment_mentioned',
        recipients,
      )
    }
    this.policyCursor = page.cursor
    this.metrics.set('inqtrix_collaboration_policy_cursor', page.cursor)
  }

  private broadcastCommentEvent(
    documentId: string,
    type: 'collaboration_comment_changed' | 'collaboration_comment_mentioned',
    recipients?: ReadonlySet<string>,
  ): void {
    const payload = JSON.stringify({
      document_id: documentId,
      type,
    })
    for (const document of this.hocuspocus.documents.values()) {
      const parsed = parseEditorCollaborationRoom(document.name)
      if (parsed?.documentId !== documentId) continue
      for (const connection of document.getConnections()) {
        if (
          recipients
          && (
            !hasConnectionContext(connection.context)
            || !recipients.has(connection.context.user.id)
          )
        ) {
          continue
        }
        connection.sendStateless(payload)
      }
    }
  }

  private documentConnectionsRequireGenerationCheck(
    event: CollaborationPolicyEvent,
  ): boolean {
    const documentId = event.resourceId
    if (!documentId) return false
    for (const document of this.hocuspocus.documents.values()) {
      const parsed = parseEditorCollaborationRoom(document.name)
      if (parsed?.documentId !== documentId) continue
      for (const connection of document.getConnections()) {
        if (
          !hasConnectionContext(connection.context)
          || connection.context.policyCursor < event.id
        ) {
          return true
        }
      }
    }
    return false
  }

  private async closeGenerationMismatches(documentId: string): Promise<boolean> {
    let closed = false
    const matching = [...this.hocuspocus.documents.values()].filter((document) => (
      parseEditorCollaborationRoom(document.name)?.documentId === documentId
    ))
    for (const document of matching) {
      const parsed = parseEditorCollaborationRoom(document.name)
      if (!parsed) continue
      try {
        const state = await this.api.loadDocumentState({
          documentId,
          fence: this.leaseManager.assertActive(),
          generation: parsed.generation,
        })
        if (state.generation !== parsed.generation) {
          this.closeDocument(document, incompatible())
          closed = true
        }
      } catch (error) {
        const mapped = collaborationError(error)
        if (
          (error instanceof ApiRequestError && (error.status === 404 || error.status === 409))
          || mapped.reason === 'generation_mismatch'
          || mapped.reason === 'update_required'
        ) {
          this.closeDocument(document, incompatible())
          closed = true
          continue
        }
        throw error
      }
    }
    return closed
  }

  private requestAffectedConnectionRevalidations(event: CollaborationPolicyEvent): void {
    for (const document of this.hocuspocus.documents.values()) {
      const parsed = parseEditorCollaborationRoom(document.name)
      if (
        event.resourceType === 'editor_document'
        && event.resourceId !== null
        && parsed?.documentId !== event.resourceId
      ) {
        continue
      }
      for (const connection of document.getConnections()) {
        if (!hasConnectionContext(connection.context)) continue
        if (connection.context.user.id !== event.targetUserId) continue
        if (event.id <= connection.context.policyCursor) continue
        this.requestPolicyRevalidation(connection, document.name)
      }
    }
  }

  private requestGuestConnectionRevalidations(
    event: CollaborationPolicyEvent,
  ): void {
    const documentId = event.resourceId
    if (!documentId) return
    for (const document of this.hocuspocus.documents.values()) {
      const parsed = parseEditorCollaborationRoom(document.name)
      if (parsed?.documentId !== documentId) continue
      for (const connection of document.getConnections()) {
        if (
          !hasConnectionContext(connection.context)
          || connection.context.user.kind !== 'guest'
        ) {
          continue
        }
        if (event.id <= connection.context.policyCursor) continue
        this.requestPolicyRevalidation(connection, document.name)
      }
    }
  }

  private requestAllConnectionRevalidations(): void {
    for (const document of this.hocuspocus.documents.values()) {
      for (const connection of document.getConnections()) {
        if (hasConnectionContext(connection.context)) {
          this.requestPolicyRevalidation(connection, document.name)
        }
      }
    }
  }

  private requestPolicyRevalidation(
    connection: Connection<HocuspocusContext>,
    documentName: string,
  ): void {
    const key = pendingKey(connection.socketId, documentName)
    if (this.policyRevalidationTimers.has(key)) return
    if (hasConnectionContext(connection.context)) {
      this.scheduleConnectionLease(
        connection,
        documentName,
        connection.context.expiresAt,
      )
    }
    const timer = setTimeout(() => {
      this.policyRevalidationTimers.delete(key)
      this.metrics.increment('inqtrix_collaboration_policy_revalidation_timeouts_total')
      this.logger.warn('policy_revalidation_timed_out')
      connection.close({
        code: CloseCodes.accessRevoked,
        reason: 'policy_revalidation_timeout',
      })
    }, this.settings.policyRevalidationTimeoutMs)
    timer.unref()
    this.policyRevalidationTimers.set(key, timer)
    this.metrics.increment('inqtrix_collaboration_policy_revalidations_total')
    this.requestedTokenRenewals.add(key)
    connection.requestToken()
  }

  private async renewConnectionToken(
    connection: Connection<HocuspocusContext>,
    context: HocuspocusContext,
    documentName: string,
    token: string,
  ): Promise<ConnectionContext> {
    try {
      const current = connectionContext(context)
      const renewed = await this.authenticator.renew(current, documentName, token)
      connection.context = { ...connection.context, ...renewed }
      connection.readOnly = renewed.access === 'view'
      this.scheduleConnectionLease(connection, documentName, renewed.expiresAt)
      return renewed
    } catch (error) {
      const mapped = collaborationError(error)
      this.closeConnectionTransport(connection, mapped)
      throw mapped
    } finally {
      this.clearPolicyRevalidation(connection, documentName)
    }
  }

  private clearPolicyRevalidation(
    connection: Connection<HocuspocusContext>,
    documentName: string,
  ): void {
    const key = pendingKey(connection.socketId, documentName)
    const timer = this.policyRevalidationTimers.get(key)
    if (timer) clearTimeout(timer)
    this.policyRevalidationTimers.delete(key)
  }

  private clearPolicyRevalidationsForSocket(socketId: string): void {
    const prefix = `${socketId}\0`
    for (const [key, timer] of this.policyRevalidationTimers) {
      if (!key.startsWith(prefix)) continue
      clearTimeout(timer)
      this.policyRevalidationTimers.delete(key)
    }
    for (const key of this.requestedTokenRenewals) {
      if (key.startsWith(prefix)) this.requestedTokenRenewals.delete(key)
    }
  }

  private registerConnection(registration: RegisteredConnection): void {
    const key = pendingKey(registration.socketId, registration.documentName)
    this.connections.set(key, registration)
    const keys = this.transportConnections.get(registration.transportId) ?? new Set<string>()
    keys.add(key)
    this.transportConnections.set(registration.transportId, keys)
  }

  /** Screens one client sync frame and prepares its durable apply. A throw
   * is a refusal that closes the connection. */
  private async admitClientSync({
    connection,
    context,
    document,
    documentName,
    payload,
    type,
  }: beforeSyncPayload<HocuspocusContext>): Promise<void> {
    if (type !== 1 && type !== 2) throw incompatible()
    this.ensureConnectionRegistered(connection, documentName)
    if (
      this.coordinator.requiresReconstruction(documentName)
      || (
        context.reconstructionEpoch !== undefined
        && this.coordinator.reconstructionEpoch(documentName)
          !== context.reconstructionEpoch
      )
    ) {
      // Hocuspocus drains frames that were already queued before the
      // transport began closing. Its read-only path rejects those updates
      // without applying or acknowledging them, while the retryable close
      // moves the provider onto a freshly reconstructed room.
      connection.readOnly = true
      this.closeConnectionTransport(connection, reloadRequired())
      return
    }
    if (!this.updateLimiter.consume(connection.socketId)) {
      this.metrics.increment('inqtrix_collaboration_rejections_total', {
        reason: 'rate_limited',
      })
      throw rateLimited()
    }
    if (context.access === 'view') {
      // A view connection is read-only in Hocuspocus, so none of its frames
      // can change the room. It is answered or refused without the room gate.
      try {
        this.coordinator.screenViewSyncFrame(document, payload)
      } catch (error) {
        const mapped = collaborationError(error)
        this.closeConnectionTransport(connection, mapped)
        throw mapped
      }
      return
    }
    let verificationOutageTriggered = false
    try {
      await this.coordinator.prepareClientUpdate({
        allowNoop: type === 1,
        connectionId: pendingKey(connection.socketId, documentName),
        context: connectionContext(context),
        document,
        room: documentName,
        update: payload,
      })
      const connectionId = pendingKey(connection.socketId, documentName)
      if (this.verificationFaults?.triggerSidecarOutage(
        connectionContext(context),
        this.coordinator.pendingClientUpdate(connectionId),
      )) {
        verificationOutageTriggered = true
        connection.readOnly = true
        this.coordinator.abortClientUpdate(connectionId)
        throw unavailable()
      }
    } catch (error) {
      const mapped = collaborationError(error)
      if (this.coordinator.requiresReconstruction(documentName)) {
        this.discardHeldOutbound(documentName)
        this.closeDocument(
          document,
          verificationOutageTriggered ? mapped : reloadRequired(),
        )
      }
      this.closeConnectionTransport(connection, mapped)
      throw mapped
    }
  }

  private ensureConnectionRegistered(
    connection: Connection<HocuspocusContext>,
    documentName: string,
  ): void {
    const context = connection.context
    const transportId = context.transportId
    if (!hasConnectionContext(context) || !transportId) return
    // Frames of a closed transport still drain; they must not bring back the
    // mapping, the authenticated address or the lease timer that the
    // teardown removed for a socket that no longer exists.
    if (!this.sockets.has(transportId)) return
    const key = pendingKey(connection.socketId, documentName)
    if (this.connections.has(key)) return
    this.registerConnection({
      connection,
      documentName,
      socketId: connection.socketId,
      transportId,
    })
    this.authenticatedAddresses.add(authAddressKey(transportId, connection.messageAddress))
    this.openDocumentAddress(transportId, connection)
    this.releaseInitialAuthIngress(transportId, connection.messageAddress)
    this.scheduleConnectionLease(connection, documentName, context.expiresAt)
  }

  private enforceConnectionLease(context: HocuspocusContext): void {
    try {
      assertConnectionLease(context)
    } catch (error) {
      const transportId = context.transportId
      const socket = transportId ? this.sockets.get(transportId) : undefined
      if (socket?.readyState === WebSocket.OPEN) {
        socket.close(CloseCodes.leaseInvalid, 'invalid_lease')
      }
      throw error
    }
  }

  /**
   * Publishes the presence state of a connection only when the connection may
   * own its awareness id (see `AwarenessOwnership`). Nothing here closes a
   * connection that behaves: a refused state is dropped and counted, and only
   * a stale connection of the same participant is superseded. The owner is
   * mirrored into the client set Hocuspocus keeps per connection, because
   * Hocuspocus removes exactly those states when the connection closes.
   */
  private admitAwarenessState(
    document: HocuspocusDocument,
    connection: Connection<HocuspocusContext>,
    context: HocuspocusContext,
    states: Map<number, Record<string, unknown>>,
  ): void {
    const [clientId] = states.keys()
    if (clientId === undefined) return
    if (!document.hasConnection(connection)) {
      // A connection that left the room still drains the frames it delivered
      // before. Its state would stay in the room with nobody to remove it.
      states.clear()
      this.refuseAwareness(connection, 'closed_connection')
      return
    }
    const verdict = this.awarenessOwnership.admit(document, clientId, {
      authenticationSequence: context.authenticationSequence ?? 0,
      connection,
      principal: awarenessPrincipal(connectionContext(context)),
    })
    if (verdict.kind === 'drop') {
      states.delete(clientId)
      this.refuseAwareness(connection, verdict.reason)
      if (verdict.supersede) this.supersedeConnection(connection)
      return
    }
    if (verdict.kind === 'takeover') {
      // Taken out of the superseded connection's set first, so that its
      // close does not remove the presence its participant still shows.
      document.getClients(verdict.superseded).delete(clientId)
      this.supersedeConnection(verdict.superseded)
    }
    if (verdict.kind !== 'accept') {
      connection.onClose(() => this.awarenessOwnership.release(connection))
    }
    document.getClients(connection).add(clientId)
  }

  /**
   * Hocuspocus decodes an awareness frame through a scratch `Awareness` that
   * drops a removal (a null state) of an id it has not seen, so a client's
   * removal of its own presence never reaches the room. An owner's removal is
   * applied here instead, before Hocuspocus handles the frame. A removal of
   * an id bound to another participant is refused; one of an id nobody
   * published, or one the same participant published on another connection,
   * has nothing of this connection to remove.
   */
  private applyOwnAwarenessRemoval(
    document: HocuspocusDocument,
    connection: Connection<HocuspocusContext>,
    frame: Uint8Array,
  ): void {
    const removal = awarenessRemoval(frame)
    if (!removal || !document.hasConnection(connection)) return
    if (!this.awarenessOwnership.owns(document, removal.clientId, connection)) {
      const principal = this.awarenessOwnership.principalOf(document, removal.clientId)
      if (
        principal !== null
        && principal !== awarenessPrincipal(connectionContext(connection.context))
      ) {
        this.refuseAwareness(connection, 'foreign_client_id')
      }
      return
    }
    if (!document.awareness.getStates().has(removal.clientId)) return
    // Applied as the owner's own update: y-protocols ignores a clock older
    // than the room's, and tells every other connection.
    document.applyAwarenessUpdate(
      connection,
      encodeAwarenessRemoval(removal.clientId, removal.clock),
    )
  }

  private refuseAwareness(
    connection: Connection<HocuspocusContext>,
    reason: AwarenessRefusal,
  ): void {
    this.metrics.increment('inqtrix_collaboration_awareness_dropped_total', { reason })
    // Frames a closed connection drained are expected after any close.
    if (reason === 'closed_connection') return
    const warned = this.awarenessWarnings.get(connection) ?? new Set<AwarenessRefusal>()
    if (warned.has(reason)) return
    warned.add(reason)
    this.awarenessWarnings.set(connection, warned)
    this.logger.warn('awareness_state_refused', { reason })
  }

  /**
   * Whether a token renewal of a document connection may reach the API. A
   * renewal that answers the service's own request is admitted once per
   * request; any other counts against the connection's budget, and one
   * beyond it closes the socket. A connection whose frames were refused
   * renews nothing: it is closing, and its successor authenticates anew.
   */
  private admitTokenRenewal(
    connection: Connection<HocuspocusContext>,
    documentName: string,
  ): boolean {
    if (this.refusedConnections.has(connection)) return false
    const key = pendingKey(connection.socketId, documentName)
    if (this.requestedTokenRenewals.delete(key)) return true
    if (this.tokenRenewalLimiter.consume(key)) return true
    this.refusedConnections.add(connection)
    this.metrics.increment('inqtrix_collaboration_rejections_total', {
      reason: 'rate_limited',
    })
    this.logger.warn('token_renewal_refused', { reason: 'rate_limited' })
    this.closeConnectionTransport(connection, rateLimited())
    return false
  }

  /**
   * Closes a connection whose participant authenticated again on another
   * one. The frames it delivered before are drained without being applied
   * or acknowledged: its successor resends whatever was not confirmed.
   */
  private supersedeConnection(connection: Connection<HocuspocusContext>): void {
    this.refusedConnections.add(connection)
    const error = connectionSuperseded()
    connection.close({ code: error.code, reason: error.closeReason })
    this.closeConnectionTransport(connection, error)
    this.metrics.increment('inqtrix_collaboration_connections_superseded_total')
    this.logger.info('connection_superseded')
  }

  private closeConnectionTransport(
    connection: Connection<HocuspocusContext>,
    error: CollaborationError,
  ): void {
    const transportId = connection.context.transportId
    const socket = transportId ? this.sockets.get(transportId) : undefined
    if (socket?.readyState === WebSocket.OPEN) {
      socket.close(error.code, error.closeReason)
      return
    }
    connection.close({ code: error.code, reason: error.closeReason })
  }

  private unregisterConnection(socketId: string, documentName: string): void {
    const key = pendingKey(socketId, documentName)
    const registration = this.connections.get(key)
    this.connections.delete(key)
    this.clearConnectionLeaseTimer(key)
    if (!registration) return
    this.authenticatedAddresses.delete(
      authAddressKey(registration.transportId, registration.connection.messageAddress),
    )
    const keys = this.transportConnections.get(registration.transportId)
    keys?.delete(key)
    if (keys?.size === 0) this.transportConnections.delete(registration.transportId)
  }

  private clearTransportConnections(transportId: string): void {
    const keys = this.transportConnections.get(transportId)
    if (!keys) return
    for (const key of keys) {
      this.connections.delete(key)
      this.clearConnectionLeaseTimer(key)
    }
    this.transportConnections.delete(transportId)
  }

  private scheduleConnectionLease(
    connection: Connection<HocuspocusContext>,
    documentName: string,
    expiresAt: number,
  ): void {
    const key = pendingKey(connection.socketId, documentName)
    this.clearConnectionLeaseTimer(key)
    const remainingMs = Math.max(0, Math.ceil(expiresAt * 1_000 - Date.now()))
    const timer = setTimeout(() => {
      this.connectionLeaseTimers.delete(key)
      const registration = this.connections.get(key)
      if (!registration) return
      const context = registration.connection.context
      if (hasConnectionContext(context) && context.expiresAt * 1_000 > Date.now()) {
        this.scheduleConnectionLease(
          registration.connection,
          registration.documentName,
          context.expiresAt,
        )
        return
      }
      this.metrics.increment('inqtrix_collaboration_lease_expirations_total')
      this.logger.warn('connection_lease_expired')
      const socket = this.sockets.get(registration.transportId)
      if (socket?.readyState === WebSocket.OPEN) {
        socket.close(CloseCodes.leaseInvalid, 'invalid_lease')
      } else {
        registration.connection.close({
          code: CloseCodes.leaseInvalid,
          reason: 'invalid_lease',
        })
      }
    }, Math.min(remainingMs, 2_147_483_647))
    timer.unref()
    this.connectionLeaseTimers.set(key, timer)
  }

  private clearConnectionLeaseTimer(key: string): void {
    const timer = this.connectionLeaseTimers.get(key)
    if (timer) clearTimeout(timer)
    this.connectionLeaseTimers.delete(key)
  }

  private clearAllConnectionLeaseTimers(): void {
    for (const timer of this.connectionLeaseTimers.values()) clearTimeout(timer)
    this.connectionLeaseTimers.clear()
  }

  /**
   * Decides how an inbound frame enters Hocuspocus. Every frame counts its
   * bytes against the transport's byte bound. A frame of an address that is
   * not open yet takes one of the transport's frame slots, or closes the
   * socket when none is left. On an open address, awareness takes no frame
   * slot: while one awareness frame waits in the connection, only the latest
   * later one is held. A document frame of an open address with edit or
   * suggest access that finds the frame budget spent waits in the sidecar
   * instead, in arrival order and bounded by count and bytes; once an address
   * has a waiting frame, its later frames wait behind it whatever its access.
   */
  private admitIngress(
    transportId: string,
    addressKey: string | null,
    type: number | undefined,
    bytes: number,
  ): IngressAdmission {
    const usage = this.ingressUsage.get(transportId) ?? { bytes: 0, frames: 0 }
    const connection = addressKey ? this.openDocumentAddresses.get(addressKey) : undefined
    if (!addressKey || !connection) {
      return this.admitFrameSlot(usage, bytes, 'rate_limited')
    }
    const writable = isWritableConnection(connection)
    if (type === MessageType.Awareness) {
      const overflow: IngressRefusal = writable
        ? COLLABORATION_INGRESS_OVERFLOW_CLOSE.reason
        : 'rate_limited'
      if (!this.awarenessInFlight.has(addressKey)) {
        return usage.bytes + bytes > this.settings.maxQueuedBytes
          ? { kind: 'refuse', reason: overflow }
          : { kind: 'deliver', awarenessAddress: addressKey }
      }
      const held = this.heldAwareness.get(addressKey)
      return usage.bytes - (held?.bytes ?? 0) + bytes > this.settings.maxQueuedBytes
        ? { kind: 'refuse', reason: overflow }
        : { kind: 'hold', addressKey }
    }
    if ((this.deferredIngressByAddress.get(addressKey) ?? 0) > 0) {
      return this.admitDeferral(transportId, usage, addressKey, bytes)
    }
    if (!writable) return this.admitFrameSlot(usage, bytes, 'rate_limited')
    if (
      (this.deferredIngress.get(transportId)?.length ?? 0) === 0
      && usage.frames + 1 <= this.settings.maxQueuedFrames
      && usage.bytes + bytes <= this.settings.maxQueuedBytes
    ) {
      return { kind: 'deliver', awarenessAddress: null }
    }
    return this.admitDeferral(transportId, usage, addressKey, bytes)
  }

  private admitFrameSlot(
    usage: { bytes: number; frames: number },
    bytes: number,
    overflow: IngressRefusal,
  ): IngressAdmission {
    return usage.frames + 1 > this.settings.maxQueuedFrames
      || usage.bytes + bytes > this.settings.maxQueuedBytes
      ? { kind: 'refuse', reason: overflow }
      : { kind: 'deliver', awarenessAddress: null }
  }

  private admitDeferral(
    transportId: string,
    usage: { bytes: number; frames: number },
    addressKey: string,
    bytes: number,
  ): IngressAdmission {
    const deferred = this.deferredIngress.get(transportId)?.length ?? 0
    return deferred + 1 > DEFERRED_FRAMES_PER_QUEUED_FRAME * this.settings.maxQueuedFrames
      || usage.bytes + bytes > this.settings.maxQueuedBytes
      ? { kind: 'refuse', reason: COLLABORATION_INGRESS_OVERFLOW_CLOSE.reason }
      : { kind: 'defer', addressKey }
  }

  /** Closes a socket that sent a frame of a message type no browser sends.
   * Frames that follow it are dropped while the socket closes. */
  private refuseClientMessage(socket: WebSocket, type: number): void {
    const error = invalidRequest()
    this.metrics.increment('inqtrix_collaboration_rejections_total', {
      reason: error.reason,
    })
    this.logger.warn('client_message_refused', { type })
    socket.close(error.code, error.closeReason)
  }

  private refuseIngress(
    transportId: string,
    socket: WebSocket,
    reason: IngressRefusal,
  ): void {
    this.metrics.increment('inqtrix_collaboration_ingress_rejections_total', { reason })
    if (reason === COLLABORATION_INGRESS_OVERFLOW_CLOSE.reason) {
      this.logger.warn('ingress_overflow_closed', {
        bytes: this.ingressUsage.get(transportId)?.bytes ?? 0,
        deferred_frames: this.deferredIngress.get(transportId)?.length ?? 0,
      })
      socket.close(COLLABORATION_INGRESS_OVERFLOW_CLOSE.code, reason)
      return
    }
    socket.close(CloseCodes.rateLimited, reason)
  }

  private reserveIngress(
    transportId: string,
    bytes: number,
    awarenessAddress: string | null,
  ): IngressReservation {
    const reservation: IngressReservation = {
      awarenessAddress,
      bytes,
      released: false,
      transportId,
    }
    this.chargeIngress(transportId, bytes, awarenessAddress ? 0 : 1)
    if (awarenessAddress) this.awarenessInFlight.set(awarenessAddress, reservation)
    return reservation
  }

  private deferIngress(transportId: string, addressKey: string, update: Uint8Array): void {
    const queue = this.deferredIngress.get(transportId) ?? []
    if (queue.length === 0) {
      this.logger.info('ingress_deferred', {
        frames_in_hocuspocus: this.ingressUsage.get(transportId)?.frames ?? 0,
      })
    }
    queue.push({ addressKey, bytes: update.byteLength, update })
    this.deferredIngress.set(transportId, queue)
    this.deferredIngressByAddress.set(
      addressKey,
      (this.deferredIngressByAddress.get(addressKey) ?? 0) + 1,
    )
    this.deferredIngressFrames += 1
    this.chargeIngress(transportId, update.byteLength, 0)
  }

  private holdAwareness(transportId: string, addressKey: string, update: Uint8Array): void {
    const replaced = this.heldAwareness.get(addressKey)
    this.heldAwareness.set(addressKey, { bytes: update.byteLength, update })
    this.chargeIngress(transportId, update.byteLength - (replaced?.bytes ?? 0), 0)
    if (replaced) this.metrics.increment('inqtrix_collaboration_awareness_coalesced_total')
  }

  /** Hands deferred frames to Hocuspocus in arrival order while the
   * transport's frame budget allows. Their bytes stay charged and move to
   * their reservation. */
  private drainDeferredIngress(transportId: string): void {
    if (this.drainingIngress.has(transportId)) return
    this.drainingIngress.add(transportId)
    try {
      while (true) {
        const deliver = this.transportClients.get(transportId)
        const queue = this.deferredIngress.get(transportId)
        if (!deliver || !queue || queue.length === 0) break
        const frames = this.ingressUsage.get(transportId)?.frames ?? 0
        if (frames + 1 > this.settings.maxQueuedFrames) break
        const frame = queue.shift()!
        if (queue.length === 0) this.deferredIngress.delete(transportId)
        this.forgetDeferredFrame(frame)
        const reservation: IngressReservation = {
          awarenessAddress: null,
          bytes: frame.bytes,
          released: false,
          transportId,
        }
        this.chargeIngress(transportId, 0, 1)
        this.ingressFrames.set(frame.update, reservation)
        deliver(frame.update)
      }
    } finally {
      this.drainingIngress.delete(transportId)
    }
  }

  private forgetDeferredFrame(frame: DeferredFrame): void {
    const remaining = (this.deferredIngressByAddress.get(frame.addressKey) ?? 1) - 1
    if (remaining > 0) this.deferredIngressByAddress.set(frame.addressKey, remaining)
    else this.deferredIngressByAddress.delete(frame.addressKey)
    this.deferredIngressFrames -= 1
  }

  /** Discards the deferred frames that match and releases their bytes. They
   * never reached Hocuspocus and were not acknowledged; a client resends its
   * unconfirmed updates after it authenticates again. */
  private discardDeferredIngress(
    transportId: string,
    matches: (frame: DeferredFrame) => boolean,
  ): void {
    const queue = this.deferredIngress.get(transportId)
    if (!queue) return
    let frames = 0
    let bytes = 0
    for (let index = queue.length - 1; index >= 0; index -= 1) {
      const frame = queue[index]!
      if (!matches(frame)) continue
      queue.splice(index, 1)
      frames += 1
      bytes += frame.bytes
      this.forgetDeferredFrame(frame)
    }
    if (queue.length === 0) this.deferredIngress.delete(transportId)
    if (frames === 0) return
    this.chargeIngress(transportId, -bytes, 0)
    this.metrics.add('inqtrix_collaboration_ingress_deferred_discarded_total', frames)
    this.logger.info('ingress_deferred_discarded', { bytes, frames })
  }

  private openDocumentAddress(
    transportId: string,
    connection: Connection<HocuspocusContext>,
  ): void {
    // A connection that closed before it was registered never opens its
    // address: its close callbacks already ran.
    if (!connection.document.hasConnection(connection)) return
    const addressKey = authAddressKey(transportId, connection.messageAddress)
    this.openDocumentAddresses.set(addressKey, connection)
    connection.onClose(() => this.closeDocumentAddress(transportId, addressKey, connection))
  }

  /** Runs synchronously when the document connection closes, by whatever
   * path. Frames of the address that arrive afterwards take the rule of an
   * address that is not open. */
  private closeDocumentAddress(
    transportId: string,
    addressKey: string,
    connection: Connection<HocuspocusContext>,
  ): void {
    if (this.openDocumentAddresses.get(addressKey) !== connection) return
    this.openDocumentAddresses.delete(addressKey)
    this.awarenessInFlight.delete(addressKey)
    const held = this.heldAwareness.get(addressKey)
    this.heldAwareness.delete(addressKey)
    if (held) this.chargeIngress(transportId, -held.bytes, 0)
    this.discardDeferredIngress(transportId, (frame) => frame.addressKey === addressKey)
    this.drainDeferredIngress(transportId)
  }

  private releaseIngress(update: Uint8Array): void {
    const reservation = this.ingressFrames.get(update)
    if (!reservation) return
    this.ingressFrames.delete(update)
    this.releaseIngressReservation(reservation)
  }

  private releaseInitialAuthIngress(transportId: string, address: string): void {
    const key = authAddressKey(transportId, address)
    const reservations = this.ingressAuthReservations.get(key) ?? []
    this.ingressAuthReservations.delete(key)
    for (const reservation of reservations) this.releaseIngressReservation(reservation)
  }

  private releaseIngressReservation(reservation: IngressReservation): void {
    if (reservation.released) return
    reservation.released = true
    const { awarenessAddress, transportId } = reservation
    if (!this.ingressUsage.has(transportId)) return
    this.chargeIngress(transportId, -reservation.bytes, awarenessAddress ? 0 : -1)
    if (!awarenessAddress) {
      this.drainDeferredIngress(transportId)
      return
    }
    if (this.awarenessInFlight.get(awarenessAddress) !== reservation) return
    this.awarenessInFlight.delete(awarenessAddress)
    const held = this.heldAwareness.get(awarenessAddress)
    const deliver = this.transportClients.get(transportId)
    if (!held || !deliver) return
    // The held frame's bytes are already charged; they become the
    // reservation of the frame now handed over.
    this.heldAwareness.delete(awarenessAddress)
    const next: IngressReservation = {
      awarenessAddress,
      bytes: held.bytes,
      released: false,
      transportId,
    }
    this.awarenessInFlight.set(awarenessAddress, next)
    this.ingressFrames.set(held.update, next)
    deliver(held.update)
  }

  private chargeIngress(transportId: string, bytes: number, frames: number): void {
    const current = this.ingressUsage.get(transportId) ?? { bytes: 0, frames: 0 }
    const next = {
      bytes: Math.max(0, current.bytes + bytes),
      frames: Math.max(0, current.frames + frames),
    }
    if (next.bytes === 0 && next.frames === 0) {
      this.ingressUsage.delete(transportId)
    } else {
      this.ingressUsage.set(transportId, next)
    }
    this.updateIngressMetrics()
  }

  private clearIngress(transportId: string): void {
    this.discardDeferredIngress(transportId, () => true)
    this.ingressUsage.delete(transportId)
    this.transportClients.delete(transportId)
    const prefix = `${transportId}\0`
    for (const key of this.ingressAuthReservations.keys()) {
      if (key.startsWith(prefix)) this.ingressAuthReservations.delete(key)
    }
    for (const key of this.authenticatedAddresses) {
      if (key.startsWith(prefix)) this.authenticatedAddresses.delete(key)
    }
    for (const key of this.openDocumentAddresses.keys()) {
      if (key.startsWith(prefix)) this.openDocumentAddresses.delete(key)
    }
    for (const key of this.awarenessInFlight.keys()) {
      if (key.startsWith(prefix)) this.awarenessInFlight.delete(key)
    }
    for (const key of this.heldAwareness.keys()) {
      if (key.startsWith(prefix)) this.heldAwareness.delete(key)
    }
    this.updateIngressMetrics()
  }

  private updateIngressMetrics(): void {
    let bytes = 0
    let frames = 0
    for (const usage of this.ingressUsage.values()) {
      bytes += usage.bytes
      frames += usage.frames
    }
    this.metrics.set('inqtrix_collaboration_ingress_queued_bytes', bytes)
    this.metrics.set('inqtrix_collaboration_ingress_queued_frames', frames)
    this.metrics.set('inqtrix_collaboration_ingress_deferred_frames', this.deferredIngressFrames)
  }

  private closeSocketAfterHook(
    context: HocuspocusContext,
    error: CollaborationError,
  ): void {
    const transportId = context.transportId
    if (!transportId) return
    setTimeout(() => {
      const socket = this.sockets.get(transportId)
      if (socket?.readyState === WebSocket.OPEN) {
        socket.close(error.code, error.closeReason)
      }
    }, 0)
  }

  private closeAllSockets(code: number, reason: string): void {
    for (const socket of this.sockets.values()) {
      if (socket.readyState === WebSocket.OPEN) socket.close(code, reason)
    }
  }

  private async teardownSockets(deadline: number): Promise<void> {
    const gracefulDeadline = Math.min(
      deadline,
      Date.now() + this.socketCloseGraceMs,
    )
    await waitForCondition(() => this.sockets.size === 0, gracefulDeadline)
    if (this.sockets.size === 0) return

    for (const [transportId, socket] of [...this.sockets]) {
      socket.terminate()
      this.transportTeardowns.get(transportId)?.(
        CloseCodes.restarting,
        'restarting',
      )
    }
    await waitForCondition(() => this.sockets.size === 0, deadline)
    if (this.sockets.size > 0) {
      this.logger.warn('shutdown_socket_teardown_timed_out', {
        connections: this.sockets.size,
      })
    }
  }

  private async awaitShutdownPhase(
    phase: string,
    promise: Promise<unknown> | null,
    deadline: number,
  ): Promise<boolean> {
    if (!promise) return true
    const outcome = await settleByDeadline(promise, deadline)
    if (outcome === 'settled') return true
    this.logger.warn(
      outcome === 'timeout' ? 'shutdown_phase_timed_out' : 'shutdown_phase_failed',
      { phase },
    )
    return false
  }

  private closeDocument(document: HocuspocusDocument, error: CollaborationError): void {
    for (const connection of document.getConnections()) {
      connection.close({ code: error.code, reason: error.closeReason })
    }
  }

  private sendHocuspocusPayload(
    transportId: string,
    socket: WebSocket,
    payload: HocuspocusPayload,
  ): void {
    if (this.stopping || this.outboundRejected.has(transportId)) return
    const bytes = hocuspocusPayloadBytes(payload)
    const reservation = this.outboundFlow.reserve(
      transportId,
      socket,
      bytes,
    )
    if (!reservation) {
      this.rejectSlowReceiver(transportId, socket)
      return
    }
    const room = hocuspocusPayloadRoom(payload)
    if (!room || !this.coordinator.isBroadcastBlocked(room)) {
      this.sendReservedOutbound({ payload, reservation, socket, transportId })
      return
    }
    const frames = this.heldOutbound.get(room) ?? []
    frames.push({ payload, reservation, socket, transportId })
    this.heldOutbound.set(room, frames)
  }

  private flushHeldOutbound(room: string): void {
    const frames = this.takeHeldOutbound(room)
    for (const frame of frames) {
      this.sendReservedOutbound(frame)
    }
  }

  private discardHeldOutbound(room: string): void {
    for (const frame of this.takeHeldOutbound(room)) {
      this.outboundFlow.release(frame.reservation)
    }
  }

  private discardHeldOutboundForTransport(transportId: string | undefined): void {
    if (!transportId) return
    for (const [room, frames] of this.heldOutbound) {
      const retained: HeldOutboundFrame[] = []
      for (const frame of frames) {
        if (frame.transportId === transportId) {
          this.outboundFlow.release(frame.reservation)
        } else {
          retained.push(frame)
        }
      }
      if (retained.length > 0) this.heldOutbound.set(room, retained)
      else this.heldOutbound.delete(room)
    }
  }

  private takeHeldOutbound(room: string): HeldOutboundFrame[] {
    const frames = this.heldOutbound.get(room) ?? []
    this.heldOutbound.delete(room)
    return frames
  }

  private sendReservedOutbound(frame: HeldOutboundFrame): void {
    if (this.outboundRejected.has(frame.transportId)) {
      this.outboundFlow.release(frame.reservation)
      return
    }
    const sent = this.outboundFlow.send(
      frame.reservation,
      frame.socket,
      frame.payload,
      () => this.rejectSlowReceiver(frame.transportId, frame.socket),
    )
    if (!sent && frame.socket.readyState === WebSocket.OPEN) {
      this.rejectSlowReceiver(frame.transportId, frame.socket)
    }
  }

  private rejectSlowReceiver(transportId: string, socket: WebSocket): void {
    if (this.outboundRejected.has(transportId)) return
    this.outboundRejected.add(transportId)
    this.metrics.increment('inqtrix_collaboration_outbound_rejections_total', {
      reason: 'rate_limited',
    })
    this.discardHeldOutboundForTransport(transportId)
    if (socket.readyState === WebSocket.OPEN) {
      socket.close(CloseCodes.rateLimited, 'rate_limited')
    }
  }

  private queueSnapshot(room: string, document: Y.Doc): void {
    void this.snapshot(room, document).catch((error) => {
      this.handleSnapshotFailure(room, error)
    })
  }

  /** Stores one snapshot of the room, or joins the one already running,
   * and resolves with the sequence it covers. Updates applied while it runs
   * stay in the tail; a follow-up is queued only when that tail reaches a
   * snapshot threshold again. */
  private snapshot(room: string, document: Y.Doc): Promise<number> {
    const existing = this.snapshotTasks.get(room)
    if (existing) return existing
    let completed = false
    const task = this.storeSnapshot(room, document)
      .then((coveredSequence) => {
        completed = true
        return coveredSequence
      })
      .finally(() => {
        this.snapshotTasks.delete(room)
        if (
          completed
          && this.hocuspocus.documents.get(room) === document
          && this.coordinator.shouldSnapshot(room)
        ) {
          this.queueSnapshot(room, document)
        }
      })
    this.snapshotTasks.set(room, task)
    return task
  }

  private async storeSnapshot(room: string, document: Y.Doc): Promise<number> {
    const stored = await this.operations.storeSnapshot(room, document)
    this.metrics.increment('inqtrix_collaboration_snapshots_total')
    if (stored.currentSequenceCovered) this.snapshotRetries.cancel(room)
    return stored.coveredSequence
  }

  /** Serves a store request (idle, maximum delay, the last connection
   * leaving, an internal operation releasing the room, shutdown, retry): the
   * stored state must cover the tail persisted when the store was asked
   * for. A snapshot already running is awaited; when it captured less,
   * exactly one more follows. Updates applied meanwhile ask for a store of
   * their own. */
  private async snapshotCovering(room: string, document: Y.Doc): Promise<void> {
    if (this.removedDocuments.has(document)) return
    if (!this.coordinator.hasUnsnapshottedUpdates(room)) return
    const requestedSequence = this.coordinator.getPersistedSequence(room)
    if (await this.snapshot(room, document) >= requestedSequence) return
    await this.coordinator.awaitRoom(room)
    if (!this.coordinator.hasUnsnapshottedUpdates(room)) return
    await eventLoopTurn()
    await this.snapshot(room, document)
  }

  private handleSnapshotFailure(room: string, error: unknown): void {
    if (!this.recordSnapshotFailure(room, error)) return
    this.snapshotRetries.schedule(room, async () => {
      const document = this.hocuspocus.documents.get(room)
      if (!document) return
      await this.snapshotCovering(room, document)
    })
  }

  /** The API no longer has the room's document. A snapshot retry could only
   * fail again and would keep the room in memory for good, so the room stops
   * storing and its connections close as on any other loss of access. Once
   * it is empty it unloads on the usual paths: its store hook ends normally,
   * its last connection leaves, or its pending retry finds nothing to store.
   * Every other snapshot failure keeps its retry. */
  private retireRemovedRoom(room: string, error: unknown): void {
    const document = this.hocuspocus.documents.get(room)
    if (!document || this.removedDocuments.has(document)) return
    const refusal = collaborationError(error)
    this.removedDocuments.set(document, refusal)
    this.logger.info('room_document_removed')
    for (const connection of document.getConnections()) {
      this.closeConnectionTransport(connection, refusal)
    }
  }

  /** Records one failed snapshot store and answers whether a retry can
   * still store it: not when the document is removed, whose room retires. */
  private recordSnapshotFailure(room: string, error: unknown): boolean {
    if (isDocumentRemoved(error)) {
      this.retireRemovedRoom(room, error)
      return false
    }
    const mapped = collaborationError(error)
    this.metrics.increment('inqtrix_collaboration_snapshot_errors_total', {
      reason: mapped.reason,
    })
    this.logger.warn('snapshot_store_failed', { reason: mapped.reason })
    return true
  }

  private async flushSnapshotsForShutdown(): Promise<void> {
    if (!this.leaseManager.isReady()) return
    const snapshots = [...this.hocuspocus.documents].map(async ([room, document]) => {
      try {
        await this.snapshotCovering(room, document)
      } catch (error) {
        const mapped = collaborationError(error)
        this.logger.warn('shutdown_snapshot_failed', { reason: mapped.reason })
      }
    })
    await Promise.all(snapshots)
  }
}

function pendingKey(socketId: string, documentName: string): string {
  return `${socketId}\0${documentName}`
}

function eventLoopTurn(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

type DeadlineOutcome = 'failed' | 'settled' | 'timeout'

function settleByDeadline(
  promise: Promise<unknown>,
  deadline: number,
): Promise<DeadlineOutcome> {
  const remaining = deadline - Date.now()
  if (remaining <= 0) return Promise.resolve('timeout')
  return new Promise((resolve) => {
    let finished = false
    const finish = (outcome: DeadlineOutcome): void => {
      if (finished) return
      finished = true
      clearTimeout(timer)
      resolve(outcome)
    }
    const timer = setTimeout(() => finish('timeout'), remaining)
    void promise.then(
      () => finish('settled'),
      () => finish('failed'),
    )
  })
}

async function waitForCondition(
  condition: () => boolean,
  deadline: number,
): Promise<void> {
  while (!condition() && Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 10))
  }
}

function guardedWebSocket(
  socket: WebSocket,
  send: (payload: HocuspocusPayload) => void,
): WebSocketLike {
  return {
    close: (code, reason) => socket.close(code, reason),
    get readyState() {
      return socket.readyState
    },
    send,
  }
}

function hocuspocusPayloadBytes(payload: HocuspocusPayload): number {
  if (typeof payload === 'string') return Buffer.byteLength(payload, 'utf8')
  if (payload instanceof Blob) return payload.size
  if (ArrayBuffer.isView(payload)) return payload.byteLength
  return payload.byteLength
}

function hocuspocusPayloadRoom(payload: HocuspocusPayload): string | null {
  if (typeof payload === 'string' || payload instanceof Blob) return null
  const bytes = ArrayBuffer.isView(payload)
    ? new Uint8Array(payload.buffer, payload.byteOffset, payload.byteLength)
    : new Uint8Array(payload)
  let length = 0
  let shift = 0
  let offset = 0
  while (offset < bytes.byteLength && shift <= 35) {
    const value = bytes[offset++]!
    length |= (value & 0x7f) << shift
    if ((value & 0x80) === 0) break
    shift += 7
  }
  if (length < 1 || length > 512 || offset + length > bytes.byteLength) return null
  const rawKey = new TextDecoder().decode(bytes.subarray(offset, offset + length))
  return rawKey.split('\0', 1)[0] || null
}

function isInternalContext(context: HocuspocusContext): context is InternalContext {
  return context.internalOperation === true
}

function hasConnectionContext(context: HocuspocusContext): context is ConnectionContext {
  return (
    typeof context.documentId === 'string'
    && typeof context.generation === 'number'
    && typeof context.leaseId === 'string'
    && typeof context.expiresAt === 'number'
    && Number.isSafeInteger(context.policyCursor)
    && (context.policyCursor ?? -1) >= 0
    && typeof context.tenantId === 'string'
    && typeof context.user?.id === 'string'
  )
}

function connectionContext(context: HocuspocusContext): ConnectionContext {
  if (!hasConnectionContext(context)) {
    throw new CollaborationError('invalid_lease', {
      closeCode: CloseCodes.leaseInvalid,
      httpStatus: 401,
    })
  }
  return context
}

function assertConnectionLease(context: HocuspocusContext): void {
  const authenticated = connectionContext(context)
  if (authenticated.expiresAt > Date.now() / 1_000) return
  throw new CollaborationError('invalid_lease', {
    closeCode: CloseCodes.leaseInvalid,
    httpStatus: 401,
  })
}

function sanitizedHocuspocusRequest(
  request: IncomingMessage,
  path: string,
): Request {
  const headers = new Headers()
  for (const name of ['origin', 'user-agent', 'x-forwarded-host', 'x-forwarded-proto']) {
    const value = request.headers[name]
    if (typeof value === 'string') headers.set(name, value)
  }
  return new Request(`http://collaboration.internal${path}`, { headers })
}

function rawDataBytes(data: RawData): Uint8Array {
  if (data instanceof ArrayBuffer) return new Uint8Array(data.slice(0))
  if (Array.isArray(data)) return new Uint8Array(Buffer.concat(data))
  return new Uint8Array(Buffer.from(data))
}

/** The frame's bytes without a copy, for reading its header. */
function rawDataView(data: RawData): Uint8Array {
  if (data instanceof ArrayBuffer) return new Uint8Array(data)
  if (Array.isArray(data)) return new Uint8Array(Buffer.concat(data))
  return data
}

function isWritableConnection(connection: Connection<HocuspocusContext>): boolean {
  const access = connection.context.access
  return access === 'edit' || access === 'suggest'
}

function rawDataByteLength(data: RawData): number {
  if (data instanceof ArrayBuffer) return data.byteLength
  if (Array.isArray(data)) {
    return data.reduce((total, item) => total + item.byteLength, 0)
  }
  return data.byteLength
}

function hocuspocusFrameHeader(
  update: Uint8Array,
): { address: string; payloadOffset: number; type: number } | null {
  const addressLength = readVarUint(update, 0)
  if (!addressLength) return null
  const typeOffset = addressLength.offset + addressLength.value
  if (typeOffset > update.byteLength) return null
  const type = readVarUint(update, typeOffset)
  if (!type) return null
  return {
    address: new TextDecoder().decode(
      update.subarray(addressLength.offset, typeOffset),
    ),
    payloadOffset: type.offset,
    type: type.value,
  }
}

/** Whether a frame is a message a browser's provider sends. */
function isClientMessage(
  frame: Uint8Array,
  header: { payloadOffset: number; type: number },
): boolean {
  if (!CLIENT_MESSAGE_TYPES.has(header.type)) return false
  if (header.type !== MessageType.Auth) return true
  return readVarUint(frame, header.payloadOffset)?.value === TOKEN_AUTHENTICATION_MESSAGE
}

/** The single entry of an awareness frame that removes a presence (a null
 * state), or null for any other frame. */
function awarenessRemoval(frame: Uint8Array): { clientId: number; clock: number } | null {
  const addressLength = readVarUint(frame, 0)
  if (!addressLength) return null
  const type = readVarUint(frame, addressLength.offset + addressLength.value)
  if (type?.value !== MessageType.Awareness) return null
  const payloadLength = readVarUint(frame, type.offset)
  if (!payloadLength || payloadLength.offset + payloadLength.value !== frame.byteLength) {
    return null
  }
  const count = readVarUint(frame, payloadLength.offset)
  const clientId = count?.value === 1 ? readVarUint(frame, count.offset) : null
  const clock = clientId ? readVarUint(frame, clientId.offset) : null
  const stateLength = clock ? readVarUint(frame, clock.offset) : null
  if (!clientId || !clock || stateLength?.value !== NULL_STATE.byteLength) return null
  if (stateLength.offset + NULL_STATE.byteLength !== frame.byteLength) return null
  const isNull = NULL_STATE.every((byte, index) => frame[stateLength.offset + index] === byte)
  return isNull ? { clientId: clientId.value, clock: clock.value } : null
}

/** A y-protocols awareness update that removes one presence. */
function encodeAwarenessRemoval(clientId: number, clock: number): Uint8Array {
  return Uint8Array.from([
    ...encodeVarUint(1),
    ...encodeVarUint(clientId),
    ...encodeVarUint(clock),
    ...encodeVarUint(NULL_STATE.byteLength),
    ...NULL_STATE,
  ])
}

function encodeVarUint(value: number): number[] {
  const bytes: number[] = []
  let remaining = value
  while (remaining > 0x7f) {
    bytes.push(0x80 | (remaining % 0x80))
    remaining = Math.floor(remaining / 0x80)
  }
  bytes.push(remaining)
  return bytes
}

function authAddressKey(transportId: string, address: string): string {
  return `${transportId}\0${address}`
}

function readVarUint(
  bytes: Uint8Array,
  start: number,
): { offset: number; value: number } | null {
  let value = 0
  let shift = 0
  let offset = start
  while (offset < bytes.byteLength && shift <= 35) {
    const byte = bytes[offset++]!
    value += (byte & 0x7f) * (2 ** shift)
    if ((byte & 0x80) === 0) {
      if (!Number.isSafeInteger(value)) return null
      return { offset, value }
    }
    shift += 7
  }
  return null
}

function rejectUpgrade(socket: Duplex, status: number, reason: string): void {
  socket.end(
    `HTTP/1.1 ${status} ${reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n`,
  )
}

function invalidRoom(): CollaborationError {
  return new CollaborationError('invalid_room', {
    closeCode: CloseCodes.incompatible,
    httpStatus: 409,
  })
}

function incompatible(): CollaborationError {
  return new CollaborationError('update_required', {
    closeCode: CloseCodes.incompatible,
    httpStatus: 409,
  })
}

function unavailable(): CollaborationError {
  return new CollaborationError('service_unavailable', {
    closeCode: CloseCodes.serviceUnavailable,
    httpStatus: 503,
  })
}

function reloadRequired(): CollaborationError {
  return new CollaborationError('restarting', {
    closeCode: CloseCodes.restarting,
    httpStatus: 503,
  })
}

function tooLarge(): CollaborationError {
  return new CollaborationError('message_too_large', {
    closeCode: CloseCodes.messageTooLarge,
    httpStatus: 413,
  })
}

function rateLimited(): CollaborationError {
  return new CollaborationError('rate_limited', {
    closeCode: CloseCodes.rateLimited,
    httpStatus: 429,
  })
}

function connectionSuperseded(): CollaborationError {
  return new CollaborationError('connection_superseded', {
    closeCode: CloseCodes.leaseInvalid,
    httpStatus: 409,
  })
}

function invalidRequest(): CollaborationError {
  return new CollaborationError('invalid_request', {
    closeCode: CloseCodes.accessRevoked,
    httpStatus: 400,
  })
}

/** Whether a client stateless payload is a well-formed `durability_reconcile`
 * request: exactly a type and a non-empty list of distinct SHA-256 hashes. */
function isDurabilityReconcileMessage(payload: string): boolean {
  let value: unknown
  try {
    value = JSON.parse(payload)
  } catch {
    return false
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const message = value as Record<string, unknown>
  const hashes = message.hashes
  return Object.keys(message).sort().join(',') === 'hashes,type'
    && message.type === 'durability_reconcile'
    && Array.isArray(hashes)
    && hashes.length > 0
    && hashes.every((hash) => typeof hash === 'string' && /^[a-f0-9]{64}$/.test(hash))
    && new Set(hashes).size === hashes.length
}
