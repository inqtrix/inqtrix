import { readFileSync } from 'node:fs'

import {
  HocuspocusProviderWebsocket,
  type HocuspocusProvider,
} from '@hocuspocus/provider'
import {
  EDITOR_SCHEMA_VERSION,
  EDITOR_YJS_FRAGMENT,
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  SUGGESTION_MARK_NAMES,
  containsStructureSuggestionAttribute,
  validateEditorYDoc,
  validateSuggestionYjsUpdate,
} from '@inqtrix/editor-schema'
import { defaultDeleteFilter, defaultProtectedNodes } from '@tiptap/y-tiptap'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as Y from 'yjs'
import { COLLABORATION_NOTICES } from './collaborationNotices'
import { localizeEditorCollaborationNotice } from './inspector/model'

// Linger contracts drive the registry's real timers with vi fake timers; a
// failed assertion must not leak the fake clock — or a lingering registry
// entry — into unrelated tests.
afterEach(() => {
  destroyLingeringCollaborationLifecycles()
  vi.useRealTimers()
})

import type { EditorCollaborationSession } from '@/api/inqtrixClient'
import {
  COLLABORATION_UPDATE_BATCH_MS,
  CollaborationDocumentController,
  COLLABORATION_AWARENESS_THROTTLE_MS,
  COLLABORATION_CONNECT_ANSWER_TIMEOUT_MS,
  COLLABORATION_STABLE_CONNECTION_MS,
  collaborationReconnectDelayMs,
  LIFECYCLE_LINGER_MS,
  MAX_LINGERING_LIFECYCLES,
  acquireLifecycleController,
  collaborationDocumentLifecycleHasUnconfirmedChanges,
  collaborationHandleForRequestedDocument,
  collaborationWebSocketUrl,
  consumeLifecycleFailure,
  createHocuspocusProvider,
  destroyLingeringCollaborationLifecycles,
  discardGuestCollaborationLifecycle,
  discardRejectedCollaborationLifecycle,
  flushActiveCollaborationDocuments,
  leaseRefreshDelayMs,
  noteCollaborationDocumentGeneration,
  observeBrowserPageLifecycle,
  registerReplacedGenerationRecovery,
  releaseLifecycleController,
  retireCollaborationDocumentLifecycle,
  type CollaborationDocumentControllerDependencies,
  type CollaborationProviderFactoryOptions,
} from './useCollaborationDocument'

const NOW_MS = 1_000_000

async function settleMicrotasks(rounds = 8): Promise<void> {
  for (let round = 0; round < rounds; round += 1) await Promise.resolve()
}

function updateSuggestionFormatKeys(update: Uint8Array): string[] {
  return Y.decodeUpdate(update).structs.flatMap((struct) => (
    struct instanceof Y.Item
    && struct.content instanceof Y.ContentFormat
    && SUGGESTION_MARK_NAMES.has(struct.content.key)
      ? [struct.content.key]
      : []
  ))
}

/** The limits the collaboration service enforces, as the session publishes
 * them; these are the service's defaults. */
type SessionLimits = {
  max_frame_bytes?: number
  max_queued_bytes?: number
  max_queued_frames?: number
  update_rate_count?: number
  update_rate_window_seconds?: number
}

const DEFAULT_SESSION_LIMITS = {
  max_frame_bytes: 2 * 1024 * 1024,
  max_queued_bytes: 8 * 1024 * 1024,
  max_queued_frames: 32,
  update_rate_count: 120,
  update_rate_window_seconds: 10,
} as const satisfies Required<SessionLimits>

type ConfiguredSession = EditorCollaborationSession & SessionLimits & {
  provider_flush_ms: number
  refresh_after: number
}

function session(
  leaseToken: string,
  timing: Partial<Pick<ConfiguredSession, 'provider_flush_ms' | 'refresh_after'>> = {},
): ConfiguredSession {
  return {
    access: 'edit',
    expires_at: (NOW_MS + 60_000) / 1_000,
    initial_write_mode: 'edit',
    lease_token: leaseToken,
    provider_flush_ms: timing.provider_flush_ms ?? COLLABORATION_UPDATE_BATCH_MS,
    protocol_version: 1,
    room: 'inqtrix-editor-v1:document-1:g2',
    schema_version: EDITOR_SCHEMA_VERSION,
    refresh_after: timing.refresh_after ?? (NOW_MS + 30_000) / 1_000,
    user: { color: '#2563EB', id: 'user-1', name: 'Ada' },
    websocket_path: '/collaboration',
    ...DEFAULT_SESSION_LIMITS,
  }
}

/** A session issued at `nowMs`, for tests in which time passes beyond the
 * lease refresh of an earlier session. */
function sessionAt(
  nowMs: number,
  overrides: Partial<ConfiguredSession> = {},
): ConfiguredSession {
  return {
    ...session('lease-token'),
    expires_at: (nowMs + 60_000) / 1_000,
    refresh_after: (nowMs + 30_000) / 1_000,
    ...overrides,
  }
}

/** A deterministic digest of an update's bytes, standing in for the
 * browser's SHA-256 wherever a test acknowledges updates by their hash. */
function contentHash(update: Uint8Array | readonly number[]): string {
  let first = 0x811c9dc5
  let second = 0x9747b28c ^ update.length
  for (const byte of update) {
    first = Math.imul(first ^ byte, 0x01000193) >>> 0
    second = Math.imul(second ^ byte, 0x5bd1e995) >>> 0
  }
  return `${first.toString(16).padStart(8, '0')}${second.toString(16).padStart(8, '0')}`
    + update.length.toString(16)
}

type PageLifecycleHandlers = Parameters<
  CollaborationDocumentControllerDependencies['observePageLifecycle']
>[0]

/** The lease and rotation arguments of each session request, without the
 * abort signal the controller adds to every request. */
function sessionRequestArguments(
  requestSession: { mock: { calls: unknown[][] } },
): unknown[][] {
  return requestSession.mock.calls.map((call) => {
    const [leaseToken, rotationCommandId] = call
    if (leaseToken === undefined && rotationCommandId === undefined) return []
    return [leaseToken, rotationCommandId]
  })
}

/** The browser page lifecycle as the controller sees it: the test fires the
 * events and reads every leave guard the controller registered. */
function createPageLifecycleFake() {
  const registrations: Array<{
    disposed: boolean
    guards: Array<(() => boolean) | null>
    handlers: PageLifecycleHandlers
  }> = []
  const live = () => registrations.filter((registration) => !registration.disposed)
  return {
    /** The browser announces the unload the way the page observer handles
     * it: only a registration with a leave guard listens, it flushes first
     * and the guard then decides whether the browser asks. */
    beforeUnload: (): boolean => {
      let asks = false
      for (const registration of live()) {
        if ((registration.guards.at(-1) ?? null) === null) continue
        registration.handlers.onExit('beforeunload')
        if (registration.guards.at(-1)?.()) asks = true
      }
      return asks
    },
    exit: (reason: 'hidden' | 'pagehide') => {
      for (const registration of live()) registration.handlers.onExit(reason)
    },
    /** The guard the browser would evaluate now: the last one registered. */
    guard: (): (() => boolean) | null => {
      const [registration] = live()
      return registration?.guards.at(-1) ?? null
    },
    guardCalls: () => live()[0]?.guards ?? [],
    observe: (handlers: PageLifecycleHandlers) => {
      const registration = {
        disposed: false,
        guards: [] as Array<(() => boolean) | null>,
        handlers,
      }
      registrations.push(registration)
      return {
        dispose: () => {
          registration.disposed = true
        },
        setLeaveGuard: (guard: (() => boolean) | null) => {
          registration.guards.push(guard)
        },
      }
    },
    registrations,
    show: (persisted: boolean) => {
      for (const registration of live()) registration.handlers.onShow(persisted)
    },
  }
}

function createHarness(
  requestSession: (
    leaseToken?: string,
    rotationCommandId?: string,
    signal?: AbortSignal,
  ) => Promise<EditorCollaborationSession>,
  hashUpdate = vi.fn(async (update: Uint8Array) => {
    void update
    return 'local-update-hash'
  }),
  options: {
    acknowledgeTokenSync?: boolean
    autoSync?: boolean
    /** The harness clock, shared with a session factory that issues sessions
     * relative to it. */
    clock?: { now: number }
    /** The document generation the controller opens; 2 by default. */
    generation?: number
    /** Schedule on the (fake) global timers and read Date.now(), so a test
     * can let time pass with vi.advanceTimersByTimeAsync. */
    globalTimers?: boolean
  } = {},
) {
  let providerOptions: CollaborationProviderFactoryOptions | null = null
  const awarenessUsers: unknown[] = []
  const provider = {} as HocuspocusProvider
  const timers = new Map<number, { callback: () => void; delayMs: number }>()
  const transportUpdates: Uint8Array[] = []
  // Every local update the controller hands to the transport, with the
  // number of the socket (one per connect) it was handed to.
  const sentUpdates: Array<{ socket: number; time: number; update: Uint8Array }> = []
  const rotationCommandIds: string[] = []
  const clock = options.clock ?? { now: NOW_MS }
  const lifecycle = createPageLifecycleFake()
  const socketState = { open: false }
  // Sends that fail the way a socket write can: the adapter throws.
  const sendFailures = { remaining: 0 }
  let commandCounter = 0
  let nextTimerId = 1
  let socket = 0
  let ackSequence = 4
  const cancelTimer = vi.fn((timer: number) => {
    if (options.globalTimers) {
      clearTimeout(timer)
      return
    }
    timers.delete(timer)
  })
  const connect = vi.fn(async () => {
    socket += 1
    socketState.open = true
    providerOptions?.events.onAuthenticated()
    if (options.autoSync !== false) providerOptions?.events.onSynced()
  })
  const destroy = vi.fn()
  // Like the real adapter, a disconnect is final: no close event follows.
  const disconnect = vi.fn(() => {
    socketState.open = false
  })
  const leaveAwareness = vi.fn()
  const restoreAwareness = vi.fn()
  const setAwarenessForwarding = vi.fn()
  const localUpdatesWritten = vi.fn()
  const sendUpdate = (update: Uint8Array) => {
    if (sendFailures.remaining > 0) {
      sendFailures.remaining -= 1
      throw new Error('The socket write failed.')
    }
    sentUpdates.push({
      socket,
      time: options.globalTimers ? Date.now() : clock.now,
      update: Uint8Array.from(update),
    })
  }
  const syncToken = vi.fn(async () => {
    if (options.acknowledgeTokenSync !== false) {
      providerOptions?.events.onAuthenticated()
    }
  })
  const sendStateless = vi.fn()
  const scheduleTimer = vi.fn((callback: () => void, delayMs: number) => {
    if (options.globalTimers) {
      return setTimeout(callback, delayMs) as unknown as number
    }
    const timer = nextTimerId
    nextTimerId += 1
    timers.set(timer, { callback, delayMs })
    return timer
  })
  const controller = new CollaborationDocumentController(
    {
      documentId: 'document-1',
      generation: options.generation ?? 2,
      initialPersistedSequence: 4,
      requestSession,
      resolveWebSocketUrl: (path) => collaborationWebSocketUrl(path, {
        host: 'desk.test',
        protocol: 'https:',
      }),
      schemaVersion: EDITOR_SCHEMA_VERSION,
    },
    {
      cancelTimer,
      createCommandId: () => {
        commandCounter += 1
        const commandId = `00000000-0000-4000-8000-${String(commandCounter).padStart(12, '0')}`
        rotationCommandIds.push(commandId)
        return commandId
      },
      createDocument: () => new Y.Doc(),
      createProvider: (options) => {
        providerOptions = options
        options.document.on('update', (update, origin) => {
          if (origin !== provider) transportUpdates.push(update)
        })
        return {
          // Every room update a test applies stands for one broadcast; sync
          // answers are covered over the real provider transport.
          applyingSyncAnswer: () => false,
          localUpdatesWritten,
          connect,
          destroy,
          disconnect,
          hasSocket: () => socketState.open,
          isSocketOpen: () => socketState.open,
          leaveAwareness,
          provider,
          restoreAwareness,
          sendUpdate,
          sendStateless,
          setAwarenessForwarding,
          setAwarenessUser: (user) => awarenessUsers.push(user),
          socketId: () => socket,
          syncToken,
        }
      },
      hashUpdate,
      now: () => (options.globalTimers ? Date.now() : clock.now),
      observePageLifecycle: lifecycle.observe,
      random: () => 0.5,
      scheduleTimer,
    },
  )

  function runTimer(delayMs: number): void {
    const match = [...timers.entries()].find(([, timer]) => timer.delayMs === delayMs)
    if (!match) throw new Error(`No ${delayMs}ms timer is scheduled.`)
    const [timerId, timer] = match
    timers.delete(timerId)
    timer.callback()
  }

  /** Runs every timer scheduled now whose delay is at most `maxDelayMs`. */
  function runTimersUpTo(maxDelayMs: number): void {
    for (const [timerId, timer] of [...timers.entries()]) {
      if (timer.delayMs > maxDelayMs || !timers.has(timerId)) continue
      timers.delete(timerId)
      timer.callback()
    }
  }

  function ack(hash: string, sequence?: number): void {
    ackSequence = Math.max(ackSequence + 1, sequence ?? 0)
    providerOptions?.events.onStateless(JSON.stringify({
      hash,
      sequence: sequence ?? ackSequence,
      type: 'durable_ack',
    }))
  }

  return {
    ack,
    advance: (ms: number) => {
      clock.now += ms
    },
    localUpdatesWritten,
    awarenessUsers,
    connect,
    controller,
    destroy,
    disconnect,
    getProviderOptions: () => providerOptions,
    hashUpdate,
    leaveAwareness,
    lifecycle,
    provider,
    restoreAwareness,
    rotationCommandIds,
    runTimer,
    runTimersUpTo,
    scheduleTimer,
    sendStateless,
    scheduledDelays: () => [...timers.values()].map((timer) => timer.delayMs),
    sentOnSocket: (socketNumber: number) => sentUpdates
      .filter((sent) => sent.socket === socketNumber)
      .map((sent) => [...sent.update]),
    sentUpdates,
    setAwarenessForwarding,
    socketNumber: () => socket,
    socketState,
    syncToken,
    transportUpdates,
    emitSynced: () => providerOptions?.events.onSynced(),
    /** The next `count` sends throw. */
    failNextSends: (count: number) => {
      sendFailures.remaining = count
    },
  }
}

describe('collaboration document lifecycle', () => {
  it('returns an inactive B handle synchronously on the hook A-to-B rerender boundary', () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    const documentA = harness.controller.getSnapshot()
    const renderedA = collaborationHandleForRequestedDocument(
      documentA,
      'document-1',
      2,
      true,
    )

    const documentB = collaborationHandleForRequestedDocument(
      renderedA,
      'document-2',
      3,
      true,
    )

    expect(renderedA).toBe(documentA)
    expect(documentB).toMatchObject({
      canEdit: false,
      document: null,
      documentId: 'document-2',
      generation: 3,
      lifecycleStatus: 'inactive',
      provider: null,
      synced: false,
    })
  })

  it('remains read-only while authentication has completed but Yjs sync is delayed', async () => {
    const harness = createHarness(
      vi.fn().mockResolvedValue(session('initial-token')),
      undefined,
      { autoSync: false },
    )

    await harness.controller.start()

    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: false,
      connectionStatus: 'connecting',
      lifecycleStatus: 'syncing',
    })

    harness.emitSynced()

    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: true,
      connectionStatus: 'connected',
      lifecycleStatus: 'saved',
    })
  })

  it('joins without a lease then refreshes the same lease and token-syncs the socket', async () => {
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('rotated-token'))
    const harness = createHarness(requestSession)

    await harness.controller.start()

    expect(sessionRequestArguments(requestSession)[0]).toEqual([])
    expect(harness.getProviderOptions()?.url).toBe('wss://desk.test/collaboration')
    expect(harness.getProviderOptions()?.getToken()).toBe('initial-token')
    expect(harness.awarenessUsers).toEqual([
      { color: '#2563EB', id: 'user-1', name: 'Ada' },
    ])
    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: true,
      connectionStatus: 'connected',
    })

    await harness.controller.refreshLease()

    expect(sessionRequestArguments(requestSession)[1]).toEqual([
      'initial-token',
      harness.rotationCommandIds[0],
    ])
    expect(harness.getProviderOptions()?.getToken()).toBe('rotated-token')
    expect(harness.syncToken).toHaveBeenCalledOnce()
    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: true,
      connectionStatus: 'connected',
    })
    expect(JSON.stringify(harness.controller.getSnapshot())).not.toContain('rotated-token')
  })

  it('stays ready when periodic token sync has no authentication acknowledgement', async () => {
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('rotated-token'))
    const harness = createHarness(requestSession, undefined, {
      acknowledgeTokenSync: false,
    })
    await harness.controller.start()

    await harness.controller.refreshLease()

    expect(harness.syncToken).toHaveBeenCalledOnce()
    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: true,
      connectionStatus: 'connected',
      lifecycleStatus: 'saved',
      synced: true,
    })
  })

  it('becomes reconnecting and read-only when a clean lease refresh fails', async () => {
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockRejectedValueOnce(new Error('network unavailable'))
    const harness = createHarness(requestSession)
    await harness.controller.start()

    await harness.controller.refreshLease()

    expect(sessionRequestArguments(requestSession)[1]).toEqual([
      'initial-token',
      harness.rotationCommandIds[0],
    ])
    expect(harness.disconnect).toHaveBeenCalled()
    expect(harness.scheduledDelays()).toContain(1_000)
    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: false,
      connectionStatus: 'reconnecting',
      error: expect.stringContaining('lease could not be refreshed'),
      nextReconnectAt: NOW_MS + 1_000,
      reconnectAttempt: 1,
      recoverability: 'retry',
    })
  })

  it('reuses one lease rotation command id after a lost HTTP response', async () => {
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockRejectedValueOnce(new Error('response lost'))
      .mockResolvedValueOnce(session('reconstructed-token'))
    const harness = createHarness(requestSession)
    await harness.controller.start()

    await harness.controller.refreshLease()
    await harness.controller.refreshLease('reconnect')

    expect(sessionRequestArguments(requestSession).slice(1)).toEqual([
      ['initial-token', harness.rotationCommandIds[0]],
      ['initial-token', harness.rotationCommandIds[0]],
    ])
    expect(harness.rotationCommandIds).toHaveLength(1)
    expect(harness.getProviderOptions()?.getToken()).toBe('reconstructed-token')
  })

  it('recovers an expired rotation with a fresh cookie-authenticated lease', async () => {
    const expired = Object.assign(new Error('lease expired'), {
      detail: { reason: 'lease_expired' },
      status: 401,
    })
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockRejectedValueOnce(expired)
      .mockResolvedValueOnce(session('fresh-token'))
    const harness = createHarness(requestSession)
    await harness.controller.start()

    await harness.controller.refreshLease()

    expect(sessionRequestArguments(requestSession)).toEqual([
      [],
      ['initial-token', harness.rotationCommandIds[0]],
      [],
    ])
    expect(harness.getProviderOptions()?.getToken()).toBe('fresh-token')
    expect(harness.syncToken).toHaveBeenCalledOnce()
    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: true,
      connectionStatus: 'connected',
    })
  })

  it('rotates the lease before reconnecting a clean transport', async () => {
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token'))
    const harness = createHarness(requestSession)
    await harness.controller.start()

    harness.getProviderOptions()?.events.onClose(1006, 'network_lost')
    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: false,
      connectionStatus: 'reconnecting',
    })

    harness.runTimer(1_000)
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    expect(sessionRequestArguments(requestSession)).toEqual([
      [],
      ['initial-token', harness.rotationCommandIds[0]],
    ])
    expect(harness.getProviderOptions()?.getToken()).toBe('reconnect-token')
    expect(harness.connect).toHaveBeenCalledTimes(2)
    expect(harness.controller.getSnapshot().connectionStatus).toBe('connected')
  })

  it.each(['service_unavailable', 'restarting'])(
    'reissues the lease after the in-band %s recovery signal',
    async (reason) => {
      const requestSession = vi.fn()
        .mockResolvedValueOnce(session('initial-token'))
        .mockResolvedValueOnce(session(`${reason}-token`))
      const harness = createHarness(requestSession)
      await harness.controller.start()
      harness.disconnect.mockClear()

      harness.getProviderOptions()?.events.onClose(1_000, reason)

      expect(harness.disconnect).toHaveBeenCalled()
      expect(harness.controller.getSnapshot()).toMatchObject({
        canEdit: false,
        connectionStatus: 'reconnecting',
        recoverability: 'retry',
      })

      harness.runTimer(1_000)
      await Promise.resolve()
      await Promise.resolve()
      await Promise.resolve()

      expect(sessionRequestArguments(requestSession)).toEqual([
        [],
        ['initial-token', harness.rotationCommandIds[0]],
      ])
      expect(harness.getProviderOptions()?.getToken()).toBe(`${reason}-token`)
      expect(harness.connect).toHaveBeenCalledTimes(2)
      expect(harness.controller.getSnapshot()).toMatchObject({
        canEdit: true,
        connectionStatus: 'connected',
      })
    },
  )

  it('guards parallel manual retries and cancels the scheduled reconnect', async () => {
    let resolveRetry!: (value: EditorCollaborationSession) => void
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolveRetry = resolve
      }))
    const harness = createHarness(requestSession)
    await harness.controller.start()
    harness.getProviderOptions()?.events.onClose(1006, 'network_lost')

    const first = harness.controller.getSnapshot().retryConnection()
    const second = harness.controller.getSnapshot().retryConnection()
    expect(requestSession).toHaveBeenCalledTimes(2)
    expect(harness.scheduledDelays()).not.toContain(1_000)
    expect(harness.controller.getSnapshot()).toMatchObject({
      nextReconnectAt: null,
      reconnectAttempt: 1,
      recoverability: 'retry',
    })

    resolveRetry(session('manual-retry-token'))
    await Promise.all([first, second])

    expect(requestSession).toHaveBeenCalledTimes(2)
    // Connected again, but the ladder starts from its first step only once
    // the connection proved stable.
    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: true,
      connectionStatus: 'connected',
      nextReconnectAt: null,
      reconnectAttempt: 1,
      recoverability: 'none',
    })
    harness.runTimer(10_000)
    expect(harness.controller.getSnapshot().reconnectAttempt).toBe(0)
  })

  it('classifies an expired authenticated session as a login recovery', async () => {
    const expired = Object.assign(new Error('session expired'), { status: 401 })
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockRejectedValueOnce(expired)
      .mockRejectedValueOnce(expired)
    const harness = createHarness(requestSession)
    await harness.controller.start()

    await harness.controller.refreshLease()

    expect(harness.controller.getSnapshot()).toMatchObject({
      connectionStatus: 'error',
      nextReconnectAt: null,
      recoverability: 'login',
    })
    expect(harness.scheduledDelays()).not.toContain(1_000)
  })

  it('stops reconnecting on protocol close conflicts', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()

    harness.getProviderOptions()?.events.onClose(4409, 'schema_conflict')

    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: false,
      connectionStatus: 'incompatible',
    })
  })

  it('classifies an in-band close by its reason, because the code arrives as 1000', async () => {
    // A close the collaboration service sends in band reaches the provider
    // with the code rewritten to 1000 and only the reason preserved. Reading
    // the code first therefore misses every close the service itself starts.
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()

    harness.getProviderOptions()?.events.onClose(1_000, 'update_required')

    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: false,
      connectionStatus: 'incompatible',
    })
  })

  it('does not report a data conflict as an outdated app version', async () => {
    // The collaboration service maps every 409 of the internal API to 4409;
    // only four reasons are real compatibility reasons, the others are
    // ordinary conflicts. A patch metadata conflict (two people decide the
    // same suggestion) must not arrive as "update required" and send the
    // user looking for a version that does not exist. The session ends in
    // both cases; only the explanation must not be invented.
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()

    harness.getProviderOptions()?.events.onClose(4_409, 'upstream_conflict')

    const snapshot = harness.controller.getSnapshot()

    expect(snapshot).toMatchObject({
      canEdit: false,
      connectionStatus: 'incompatible',
    })
    expect(snapshot.error).not.toMatch(/not compatible/i)
    expect(snapshot.error).toMatch(/reload the page/i)

    // The sentence is also the key of the translation table. If the two
    // drift apart, the lookup silently falls back to the English original
    // and a German user reads English.
    expect(localizeEditorCollaborationNotice(snapshot.error!, 'de'))
      .toMatch(/Laden Sie die Seite neu/)
  })

  it('disconnects the transport before reconnecting after a rejected update', async () => {
    // The reconnect request is a no-op while the socket is still open, so a
    // close that does not tear down the transport leaves the session silently
    // detached: it keeps accepting input and never receives again.
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()
    harness.disconnect.mockClear()

    harness.getProviderOptions()?.events.onClose(1_000, 'invalid_lease')

    expect(harness.disconnect).toHaveBeenCalled()
    expect(harness.controller.getSnapshot()).toMatchObject({
      connectionStatus: 'reconnecting',
    })
  })

  it('reports a rejected page origin as a transport fault, not as revoked access', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()

    harness.getProviderOptions()?.events.onClose(4_403, 'origin_rejected')

    const snapshot = harness.controller.getSnapshot()
    // Its own terminal state, not 'incompatible': both stop the session, but
    // one tells the operator to update the client and the other to fix an
    // address. Reusing the incompatible state produced the wrong advice.
    expect(snapshot).toMatchObject({
      access: 'edit',
      canEdit: false,
      connectionStatus: 'origin_rejected',
    })
    expect(snapshot.error).toContain('origin')
  })

  it('keeps a suggestion-policy rejection separate from access revocation', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()

    harness.getProviderOptions()?.events.onClose(4403, 'suggestion_policy_violation')

    expect(harness.controller.getSnapshot()).toMatchObject({
      access: 'edit',
      canEdit: false,
      connectionStatus: 'error',
      durabilityStatus: 'error',
      recoverability: 'reload',
      updateRejected: true,
    })
    expect(harness.controller.getSnapshot().error).toBe(COLLABORATION_NOTICES.update_rejected)
  })

  describe('a change the service rejects', () => {
    const rejections: Array<[string, (harness: ReturnType<typeof createHarness>) => void, string]> = [
      ['a schema rejection', (harness) => harness.getProviderOptions()?.events.onClose(4409, 'invalid_schema'), COLLABORATION_NOTICES.update_rejected],
      ['a suggestion-policy rejection', (harness) => harness.getProviderOptions()?.events.onClose(4403, 'suggestion_policy_violation'), COLLABORATION_NOTICES.update_rejected],
      ['an unsupported suggestion structure', (harness) => harness.getProviderOptions()?.events.onClose(4409, 'unsupported_suggestion_structure'), COLLABORATION_NOTICES.update_rejected],
      ['an in-band rejection whose code was rewritten', (harness) => harness.getProviderOptions()?.events.onClose(1000, 'suggestion_policy_violation'), COLLABORATION_NOTICES.update_rejected],
      ['an oversized update', (harness) => harness.getProviderOptions()?.events.onClose(1009, 'message_too_large'), COLLABORATION_NOTICES.update_too_large],
      ['an update that makes the document too large', (harness) => harness.getProviderOptions()?.events.onClose(1009, 'document_too_large'), COLLABORATION_NOTICES.update_too_large],
      ['a close for a frame that is too big, without a reason', (harness) => harness.getProviderOptions()?.events.onClose(1009, ''), COLLABORATION_NOTICES.update_too_large],
      ['a durable rejection', (harness) => harness.getProviderOptions()?.events.onStateless(JSON.stringify({
        code: 'too_large',
        hash: 'local-update-hash',
        type: 'durable_rejection',
      })), COLLABORATION_NOTICES.update_rejected],
    ]

    it.each(rejections)('keeps the local state shown as a backup and offers a document reload after %s', async (_name, reject, notice) => {
      const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
      await harness.controller.start()
      harness.controller.getSnapshot().document?.getMap('test').set('title', 'Refused draft')
      harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      await settleMicrotasks()
      harness.disconnect.mockClear()

      reject(harness)

      expect(harness.controller.getSnapshot()).toMatchObject({
        canEdit: false,
        connectionStatus: 'error',
        durabilityStatus: 'error',
        error: notice,
        hasUnconfirmedLocalChanges: true,
        liveDocumentReady: true,
        nextReconnectAt: null,
        recoverability: 'reload',
        updateRejected: true,
      })
      // Resending the refused state cannot succeed: the transport is gone and
      // nothing reconnects on its own.
      expect(harness.disconnect).toHaveBeenCalled()
      expect(harness.scheduledDelays().filter((delay) => delay >= 1_000)).toEqual([])
      expect(harness.controller.getSnapshot().document?.getMap('test').get('title'))
        .toBe('Refused draft')
    })

    it('does not treat a connection loss as a rejection', async () => {
      const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
      await harness.controller.start()

      harness.getProviderOptions()?.events.onClose(1006, '')

      expect(harness.controller.getSnapshot()).toMatchObject({
        recoverability: 'retry',
        updateRejected: false,
      })
    })

    it('asks for a document reload only while the change stays rejected', async () => {
      const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
      await harness.controller.start()
      const reloads = vi.fn()
      harness.controller.onDocumentReload(reloads)

      harness.controller.getSnapshot().reloadDocument()
      expect(reloads).not.toHaveBeenCalled()

      harness.getProviderOptions()?.events.onClose(4409, 'invalid_schema')
      harness.controller.getSnapshot().reloadDocument()
      expect(reloads).toHaveBeenCalledOnce()
    })

    it('still offers the document reload after the surface left and came back', async () => {
      const key = 'workspace-1:rejected-return:g2'
      const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
      const controller = acquireLifecycleController(key, null, () => harness.controller)
      await controller.start()
      controller.getSnapshot().document?.getMap('test').set('title', 'Refused draft')
      harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      await settleMicrotasks()
      harness.getProviderOptions()?.events.onClose(4409, 'invalid_schema')

      releaseLifecycleController(key, controller)
      const returned = acquireLifecycleController(key, null, () => {
        throw new Error('the rejected controller keeps the backup and is reused')
      })
      try {
        expect(returned).toBe(controller)
        expect(returned.getSnapshot()).toMatchObject({
          liveDocumentReady: true,
          recoverability: 'reload',
          updateRejected: true,
        })
        const reloads = vi.fn()
        returned.onDocumentReload(reloads)
        returned.getSnapshot().reloadDocument()
        expect(reloads).toHaveBeenCalledOnce()
      } finally {
        // The registry is shared by every test of this file.
        expect(discardRejectedCollaborationLifecycle(key, returned)).toBe(true)
      }
    })

    it('starts the document over from the service after a reload and discards the refused state', async () => {
      const key = 'workspace-1:rejected-reload:g2'
      const rejected = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
      const controller = acquireLifecycleController(key, null, () => rejected.controller)
      await controller.start()
      controller.getSnapshot().document?.getMap('test').set('title', 'Refused draft')
      rejected.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      await settleMicrotasks()

      expect(discardRejectedCollaborationLifecycle(key, controller)).toBe(false)
      rejected.getProviderOptions()?.events.onClose(4409, 'invalid_schema')
      expect(discardRejectedCollaborationLifecycle(key, controller)).toBe(true)
      expect(rejected.destroy).toHaveBeenCalledOnce()

      const fresh = createHarness(vi.fn().mockResolvedValue(session('fresh-token')))
      const reloaded = acquireLifecycleController(key, null, () => fresh.controller)
      expect(reloaded).toBe(fresh.controller)
      expect(reloaded.getSnapshot().lifecycleKey)
        .not.toBe(controller.getSnapshot().lifecycleKey)
      // The surface's own release of the discarded controller changes nothing.
      releaseLifecycleController(key, controller)
      expect(acquireLifecycleController(key, null, () => {
        throw new Error('the reloaded controller must be reused')
      })).toBe(fresh.controller)
      releaseLifecycleController(key, reloaded)
      releaseLifecycleController(key, reloaded)
    })
  })

  // Once a guest's access has ended, the page says unconfirmed changes are
  // lost. A normal release keeps a controller with such changes registered,
  // its document in memory and the browser's leave prompt armed for changes
  // that can never be saved.
  it('leaves neither the document nor a leave prompt behind once a guest\'s access ended', async () => {
    const key = 'guest:guest-ended:document-1:g2'
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    const controller = acquireLifecycleController(key, null, () => harness.controller)
    try {
      await controller.start()
      controller.getSnapshot().document?.getMap('test').set('title', 'Unconfirmed guest edit')
      harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      await settleMicrotasks()
      harness.getProviderOptions()?.events.onClose(4403, 'access_revoked')
      await settleMicrotasks()
      expect(controller.getSnapshot().hasUnconfirmedLocalChanges).toBe(true)

      releaseLifecycleController(key, controller)
      expect(harness.lifecycle.beforeUnload()).toBe(true)

      expect(discardGuestCollaborationLifecycle({
        documentId: 'document-1',
        generation: 2,
        guestId: 'guest-ended',
      })).toBe(true)

      expect(harness.lifecycle.beforeUnload()).toBe(false)
      expect(harness.destroy).toHaveBeenCalledOnce()
      expect(controller.getSnapshot().document?.isDestroyed).toBe(true)
      const fresh = createHarness(vi.fn().mockResolvedValue(session('fresh-token')))
      const reopened = acquireLifecycleController(key, null, () => fresh.controller)
      expect(reopened).toBe(fresh.controller)
      releaseLifecycleController(key, reopened)
      expect(discardGuestCollaborationLifecycle({
        documentId: 'document-1',
        generation: 2,
        guestId: 'guest-unknown',
      })).toBe(false)
    } finally {
      // The registry is shared by every test of this file; the key has the
      // shape `${scope}:${documentId}:g${generation}` the retire path reads.
      retireCollaborationDocumentLifecycle({
        documentId: 'document-1',
        generation: 2,
        workspaceId: 'guest:guest-ended',
      })
    }
  })

  it('reissues a revoked lease and recovers a live permission downgrade as read-only', async () => {
    const invalidated = Object.assign(new Error('lease invalidated'), {
      detail: { reason: 'lease_revoked' },
      status: 401,
    })
    const downgradedSession = {
      ...session('view-token'),
      access: 'view' as const,
      initial_write_mode: 'view' as const,
    }
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockRejectedValueOnce(invalidated)
      .mockResolvedValueOnce(downgradedSession)
    const harness = createHarness(requestSession)
    await harness.controller.start()

    harness.getProviderOptions()?.events.onClose(4403, 'access_revoked')

    expect(harness.controller.getSnapshot()).toMatchObject({
      access: 'edit',
      canEdit: false,
      connectionStatus: 'reconnecting',
      recoverability: 'retry',
    })

    harness.runTimer(1_000)
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    expect(sessionRequestArguments(requestSession)).toEqual([
      [],
      ['initial-token', harness.rotationCommandIds[0]],
      [],
    ])
    expect(harness.controller.getSnapshot()).toMatchObject({
      access: 'view',
      canEdit: false,
      connectionStatus: 'read_only',
      lifecycleStatus: 'read_only',
      recoverability: 'none',
      synced: true,
    })
    expect(harness.connect).toHaveBeenCalledTimes(2)
  })

  it('enters terminal access-revoked state only after a fresh lease is denied', async () => {
    const invalidated = Object.assign(new Error('lease invalidated'), {
      detail: { reason: 'lease_revoked' },
      status: 401,
    })
    const denied = Object.assign(new Error('resource unavailable'), {
      status: 404,
    })
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockRejectedValueOnce(invalidated)
      .mockRejectedValueOnce(denied)
    const harness = createHarness(requestSession)
    await harness.controller.start()

    harness.getProviderOptions()?.events.onClose(4403, 'access_revoked')
    harness.runTimer(1_000)
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    expect(sessionRequestArguments(requestSession)).toEqual([
      [],
      ['initial-token', harness.rotationCommandIds[0]],
      [],
    ])
    expect(harness.controller.getSnapshot()).toMatchObject({
      access: 'view',
      canEdit: false,
      connectionStatus: 'access_revoked',
      lifecycleStatus: 'error',
      recoverability: 'none',
      synced: false,
    })
  })

  it('does not replay unconfirmed writes after access changes', async () => {
    const requestSession = vi.fn().mockResolvedValue(session('initial-token'))
    const harness = createHarness(requestSession)
    await harness.controller.start()
    harness.controller.getSnapshot().document?.getMap('test').set(
      'title',
      'Unconfirmed draft',
    )
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await Promise.resolve()
    await Promise.resolve()

    harness.getProviderOptions()?.events.onClose(4403, 'access_revoked')

    expect(requestSession).toHaveBeenCalledOnce()
    expect(harness.connect).toHaveBeenCalledOnce()
    expect(harness.sentOnSocket(2)).toEqual([])
    expect(harness.scheduledDelays()).not.toContain(1_000)
    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: false,
      connectionStatus: 'error',
      durabilityStatus: 'error',
      hasUnconfirmedLocalChanges: true,
      lifecycleStatus: 'error',
      recoverability: 'none',
      synced: false,
    })
    expect(harness.controller.getSnapshot().error).toContain(
      'local document state is retained',
    )
  })

  it('rejects a periodic write-to-view lease while local writes are unconfirmed', async () => {
    const downgradedSession = {
      ...session('view-token'),
      access: 'view' as const,
      initial_write_mode: 'view' as const,
    }
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(downgradedSession)
    const harness = createHarness(requestSession)
    await harness.controller.start()
    harness.controller.getSnapshot().document?.getMap('test').set(
      'title',
      'Unconfirmed periodic draft',
    )
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await Promise.resolve()
    await Promise.resolve()

    await harness.controller.refreshLease()

    expect(requestSession).toHaveBeenCalledTimes(2)
    expect(harness.connect).toHaveBeenCalledOnce()
    expect(harness.syncToken).not.toHaveBeenCalled()
    expect(harness.sentOnSocket(2)).toEqual([])
    expect(harness.controller.getSnapshot()).toMatchObject({
      connectionStatus: 'error',
      durabilityStatus: 'error',
      hasUnconfirmedLocalChanges: true,
    })
  })

  it('coalesces authentication-failed and close callbacks into one access revalidation', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()

    harness.getProviderOptions()?.events.onClose(4403, 'access_revoked')
    harness.getProviderOptions()?.events.onAuthenticationFailed('access_revoked')

    expect(
      harness.scheduledDelays().filter((delay) => delay === 1_000),
    ).toHaveLength(1)
    expect(harness.controller.getSnapshot()).toMatchObject({
      connectionStatus: 'reconnecting',
      reconnectAttempt: 1,
    })
  })

  it('sends the first local update after the idle delay and hashes the transported payload', async () => {
    const hashUpdate = vi.fn(async (update: Uint8Array) => {
      void update
      return 'merged-update-hash'
    })
    const harness = createHarness(
      vi.fn().mockResolvedValue(session('initial-token')),
      hashUpdate,
    )
    await harness.controller.start()
    const editorDocument = harness.controller.getSnapshot().document

    editorDocument?.getMap('test').set('title', 'Draft')
    editorDocument?.getMap('test').set('subtitle', 'Notes')

    expect(harness.scheduledDelays()).toContain(COLLABORATION_UPDATE_BATCH_MS)
    expect(COLLABORATION_UPDATE_BATCH_MS).toBeLessThanOrEqual(50)
    expect(harness.transportUpdates).toHaveLength(0)

    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await Promise.resolve()
    await Promise.resolve()

    expect(harness.transportUpdates).toHaveLength(1)
    expect(hashUpdate).toHaveBeenCalledOnce()
    expect([...hashUpdate.mock.calls[0][0]]).toEqual([...harness.transportUpdates[0]])
    expect(harness.getProviderOptions()?.document.getMap('test').toJSON()).toEqual({
      subtitle: 'Notes',
      title: 'Draft',
    })
    expect(harness.controller.getSnapshot()).toMatchObject({
      durabilityStatus: 'pending',
      pendingHashes: ['merged-update-hash'],
    })
  })

  it('sends the update before a structure suggestion on its own so the server can inspect the slash token', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()
    const editorDocument = harness.controller.getSnapshot().document
    expect(editorDocument).toBeDefined()

    const root = editorDocument!.getXmlFragment('content')
    const paragraph = new Y.XmlElement('paragraph')
    root.insert(0, [paragraph])
    expect(harness.transportUpdates).toHaveLength(0)

    paragraph.setAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR, 'structure')

    expect(harness.transportUpdates).toHaveLength(1)
    expect(containsStructureSuggestionAttribute(harness.transportUpdates[0])).toBe(false)

    harness.advance(1_000)
    harness.ack('local-update-hash')
    await settleMicrotasks()

    expect(harness.transportUpdates).toHaveLength(2)
    expect(containsStructureSuggestionAttribute(harness.transportUpdates[1])).toBe(true)
  })

  it('sends pending direct normalization on its own before the first suggestion mark', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()
    const editorDocument = harness.controller.getSnapshot().document
    expect(editorDocument).toBeDefined()

    const root = editorDocument!.getXmlFragment('content')
    const paragraph = new Y.XmlElement('paragraph')
    const text = new Y.XmlText()
    root.insert(0, [paragraph])
    paragraph.insert(0, [text])
    expect(harness.transportUpdates).toHaveLength(0)

    text.insert(0, '/', {
      insertion: {
        authorId: '11111111-1111-4111-8111-111111111111',
        createdAt: NOW_MS,
        kind: 'insertion',
        patchId: '22222222-2222-4222-8222-222222222222',
        suggestionId: '33333333-3333-4333-8333-333333333333',
      },
    })

    expect(harness.transportUpdates).toHaveLength(1)
    expect(updateSuggestionFormatKeys(harness.transportUpdates[0])).toEqual([])

    harness.advance(1_000)
    harness.ack('local-update-hash')
    await settleMicrotasks()

    expect(harness.transportUpdates).toHaveLength(2)
    expect(updateSuggestionFormatKeys(harness.transportUpdates[1])).toEqual([
      'insertion',
      'insertion',
    ])
  })

  it('recognizes a repeated structure suggestion when Yjs infers the replaced attribute', () => {
    const document = new Y.Doc()
    const root = document.getXmlFragment('content')
    const paragraph = new Y.XmlElement('paragraph')
    root.insert(0, [paragraph])
    paragraph.setAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR, {
      action: 'heading1',
      authorId: '11111111-1111-4111-8111-111111111111',
      createdAt: NOW_MS,
      kind: 'structure',
      patchId: '22222222-2222-4222-8222-222222222222',
      suggestionId: '33333333-3333-4333-8333-333333333333',
    } as never)
    const vector = Y.encodeStateVector(document)

    paragraph.setAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR, {
      action: 'paragraph',
      authorId: '11111111-1111-4111-8111-111111111111',
      createdAt: NOW_MS + 1,
      kind: 'structure',
      patchId: '44444444-4444-4444-8444-444444444444',
      suggestionId: '55555555-5555-4555-8555-555555555555',
    } as never)
    const update = Y.encodeStateAsUpdate(document, vector)
    const replacement = Y.decodeUpdate(update).structs.find((struct) => (
      struct instanceof Y.Item
      && struct.content instanceof Y.ContentAny
    ))

    expect(replacement).toMatchObject({ parentSub: null })
    expect(containsStructureSuggestionAttribute(update)).toBe(true)
    document.destroy()
  })

  it('does not retain a phantom hash when Yjs removes an already transported update', async () => {
    const hashUpdate = vi.fn(async (update: Uint8Array) => {
      void update
      return 'must-not-be-called'
    })
    const harness = createHarness(
      vi.fn().mockResolvedValue(session('initial-token')),
      hashUpdate,
    )
    await harness.controller.start()
    const editorDocument = harness.controller.getSnapshot().document
    const transportDocument = harness.getProviderOptions()?.document
    expect(editorDocument).toBeDefined()
    expect(transportDocument).toBeDefined()

    const remoteDocument = new Y.Doc()
    remoteDocument.getMap('test').set('remote-title', 'Already transported')
    const remoteUpdate = Y.encodeStateAsUpdate(remoteDocument)

    // Model the multi-user race directly: the transport has already observed
    // a remote struct while the editor-side batch still contains that struct.
    Y.applyUpdate(transportDocument!, remoteUpdate, { source: 'remote-seed' })
    harness.transportUpdates.length = 0
    Y.applyUpdate(editorDocument!, remoteUpdate, { source: 'editor-replay' })

    const flushed = harness.controller.flushAndAwaitDurability()
    await flushed

    expect(harness.transportUpdates).toHaveLength(0)
    expect(hashUpdate).not.toHaveBeenCalled()
    expect(harness.controller.getSnapshot()).toMatchObject({
      durabilityStatus: 'saved',
      hasUnconfirmedLocalChanges: false,
      pendingHashes: [],
    })
    remoteDocument.destroy()
  })

  it('hashes the Yjs-deduplicated transport payload after concurrent state merge', async () => {
    const hashUpdate = vi.fn(async (update: Uint8Array) => {
      void update
      return 'deduplicated-update-hash'
    })
    const harness = createHarness(
      vi.fn().mockResolvedValue(session('initial-token')),
      hashUpdate,
    )
    await harness.controller.start()
    const editorDocument = harness.controller.getSnapshot().document
    const transportDocument = harness.getProviderOptions()?.document
    expect(editorDocument).toBeDefined()
    expect(transportDocument).toBeDefined()

    const remoteDocument = new Y.Doc()
    remoteDocument.getMap('test').set('remote-title', 'Remote')
    const remoteUpdate = Y.encodeStateAsUpdate(remoteDocument)
    Y.applyUpdate(transportDocument!, remoteUpdate, { source: 'remote-seed' })
    harness.transportUpdates.length = 0

    const combinedDocument = new Y.Doc()
    Y.applyUpdate(combinedDocument, remoteUpdate)
    combinedDocument.getMap('test').set('local-title', 'Local')
    Y.applyUpdate(
      editorDocument!,
      Y.encodeStateAsUpdate(combinedDocument),
      { source: 'combined-editor-update' },
    )
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await Promise.resolve()
    await Promise.resolve()

    expect(harness.transportUpdates).toHaveLength(1)
    expect(hashUpdate).toHaveBeenCalledOnce()
    expect([...hashUpdate.mock.calls[0][0]]).toEqual([
      ...harness.transportUpdates[0],
    ])
    harness.getProviderOptions()?.events.onStateless(JSON.stringify({
      hash: 'deduplicated-update-hash',
      sequence: 12,
      type: 'durable_ack',
    }))
    expect(harness.controller.getSnapshot()).toMatchObject({
      durabilityStatus: 'saved',
      hasUnconfirmedLocalChanges: false,
      lastLocalDurableSequence: 12,
      pendingHashes: [],
    })
    combinedDocument.destroy()
    remoteDocument.destroy()
  })

  it('reports pending local hashes as saved only after the matching durable ack', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()

    harness.controller.getSnapshot().document?.getMap('test').set('title', 'Draft')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await Promise.resolve()
    await Promise.resolve()

    expect(harness.controller.getSnapshot()).toMatchObject({
      durabilityStatus: 'pending',
      pendingHashes: ['local-update-hash'],
    })

    harness.getProviderOptions()?.events.onStateless(JSON.stringify({
      hash: 'local-update-hash',
      sequence: 5,
      type: 'durable_ack',
    }))

    expect(harness.controller.getSnapshot()).toMatchObject({
      activityRevision: 1,
      durabilityStatus: 'saved',
      lastLocalDurableSequence: 5,
      lastPersistedSequence: 4,
      pendingHashes: [],
    })
  })

  it('publishes when the oldest unconfirmed local change was made, and moves on as changes are confirmed', async () => {
    const clock = { now: NOW_MS }
    const harness = contentHarness(session('initial-token'), { clock })
    await harness.controller.start()
    const map = editorDocumentOf(harness).getMap('test')
    expect(harness.controller.getSnapshot().unconfirmedSince).toBeNull()

    map.set('a', 'a')
    expect(harness.controller.getSnapshot().unconfirmedSince).toBe(NOW_MS)
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    const [first] = harness.sentOnSocket(1)
    // Typed while the first update waits for its acknowledgement.
    harness.advance(300)
    map.set('b', 'b')
    expect(harness.controller.getSnapshot().unconfirmedSince).toBe(NOW_MS)

    await acknowledge(harness, first!)
    expect(harness.controller.getSnapshot().unconfirmedSince).toBe(NOW_MS + 300)
    await acknowledge(harness, harness.sentOnSocket(1)[1]!)
    expect(harness.controller.getSnapshot()).toMatchObject({
      durabilityStatus: 'saved',
      unconfirmedSince: null,
    })

    // A page exit sends the waiting change at once: two updates wait for
    // their acknowledgements, and the older one counts.
    map.set('c', 'c')
    const cAt = clock.now
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    harness.advance(200)
    map.set('d', 'd')
    harness.lifecycle.exit('hidden')
    await settleMicrotasks()
    expect(harness.sentOnSocket(1)).toHaveLength(4)
    expect(harness.controller.getSnapshot().unconfirmedSince).toBe(cAt)
  })

  it('counts from the oldest waiting change when a cleanup of the transport is logged after it was made', async () => {
    const clock = { now: NOW_MS }
    const harness = contentHarness(session('initial-token'), { clock })
    await harness.controller.start()
    const seed = seedParagraphs(harness, [[{ text: 'Hello world' }]])
    editorText(harness, 0).format(6, 5, { bold: true })
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    const [bolded] = harness.sentOnSocket(1)
    // Typed while the bold run waits for its acknowledgement.
    harness.advance(3_000)
    const typedAt = clock.now
    editorText(harness, 0).insert(0, 'x')
    // Another writer's deletion of the bold word arrives later. The
    // transport's cleanup of it becomes an unconfirmed update of its own,
    // made after the waiting 'x' and sent ahead of it.
    harness.advance(4_000)
    Y.applyUpdate(
      harness.getProviderOptions()!.document,
      remoteChange([seed], (text) => text.delete(6, 5)),
      harness.provider,
    )
    await settleMicrotasks()
    await acknowledge(harness, bolded!)

    expect(harness.sentOnSocket(1)).toHaveLength(2)
    expect(plainText(editorText(harness, 0))).toBe('xHello ')
    expect(harness.controller.getSnapshot()).toMatchObject({
      hasUnconfirmedLocalChanges: true,
      unconfirmedSince: typedAt,
    })
  })

  it('publishes shared-comment and mention invalidations independently', async () => {
    const harness = createHarness(
      vi.fn().mockResolvedValue(session('initial-token')),
    )
    await harness.controller.start()

    harness.getProviderOptions()?.events.onStateless(JSON.stringify({
      document_id: 'document-1',
      type: 'collaboration_comment_changed',
    }))
    harness.getProviderOptions()?.events.onStateless(JSON.stringify({
      document_id: 'document-1',
      type: 'collaboration_comment_mentioned',
    }))
    harness.getProviderOptions()?.events.onStateless(JSON.stringify({
      document_id: 'another-document',
      type: 'collaboration_comment_changed',
    }))

    expect(harness.controller.getSnapshot()).toMatchObject({
      commentEventVersion: 2,
      commentMentionEventVersion: 1,
    })
  })

  it('flushes the browser batch and waits for every durable acknowledgement', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()
    harness.controller.getSnapshot().document?.getMap('test').set('title', 'Export')

    let resolved = false
    const flushed = harness.controller.flushAndAwaitDurability().then(() => {
      resolved = true
    })
    await Promise.resolve()
    await Promise.resolve()

    expect(harness.transportUpdates).toHaveLength(1)
    expect(resolved).toBe(false)
    harness.getProviderOptions()?.events.onStateless(JSON.stringify({
      hash: 'local-update-hash',
      sequence: 8,
      type: 'durable_ack',
    }))
    await flushed
    expect(resolved).toBe(true)
  })

  it('keeps local acknowledgements separate from authoritative peer and decision sequences', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()
    expect(harness.controller.getSnapshot()).toMatchObject({
      activityRevision: 0,
      lastLocalDurableSequence: 0,
      lastPersistedSequence: 4,
    })
    harness.controller.getSnapshot().document?.getMap('test').set('title', 'Local')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await Promise.resolve()
    await Promise.resolve()
    harness.getProviderOptions()?.events.onStateless(JSON.stringify({
      hash: 'local-update-hash',
      sequence: 9,
      type: 'durable_ack',
    }))

    expect(harness.controller.getSnapshot()).toMatchObject({
      activityRevision: 1,
      lastLocalDurableSequence: 9,
      lastPersistedSequence: 4,
    })
    harness.controller.updateAuthoritativeSequence(12)
    harness.controller.updateAuthoritativeSequence(11)
    harness.controller.getSnapshot().updateAuthoritativeSequence(15)

    expect(harness.controller.getSnapshot()).toMatchObject({
      activityRevision: 3,
      lastLocalDurableSequence: 9,
      lastPersistedSequence: 15,
    })
  })

  it('recovers from a causal gap by resending unconfirmed updates in send order', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const editorDocument = harness.controller.getSnapshot().document!
    editorDocument.getMap('test').set('first', 'e1')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    editorDocument.getMap('test').set('second', 'e2')
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    const [e1] = harness.sentOnSocket(1)
    expect(harness.sentOnSocket(1)).toHaveLength(1)

    harness.getProviderOptions()?.events.onClose(4412, 'causal_gap')

    const recovering = harness.controller.getSnapshot()
    // The controller's own wording; a reword must be caught here.
    expect(recovering).toMatchObject({
      connectionStatus: 'reconnecting',
      error: 'The collaboration service is missing earlier changes from this editor; '
        + 'they are being sent again in order.',
      recoverability: 'retry',
    })
    expect(localizeEditorCollaborationNotice(recovering.error, 'de')).toBe(
      'Dem Kollaborationsdienst fehlen frühere Änderungen aus diesem Editor. '
      + 'Inqtrix sendet sie in der richtigen Reihenfolge erneut.',
    )

    harness.runTimer(1_000)
    await settleMicrotasks()

    expect(harness.sentOnSocket(2)).toEqual([e1])
    await acknowledge(harness, e1!)
    const second = harness.sentOnSocket(2)
    expect(second).toHaveLength(2)
    expect(keysAddedBy(second, 1)).toEqual(['second'])
    await acknowledge(harness, second[1]!)
    expect(harness.controller.getSnapshot()).toMatchObject({
      connectionStatus: 'connected',
      durabilityStatus: 'saved',
      pendingHashes: [],
    })

    // Durable progress renews the recovery: a later gap reconnects again.
    harness.getProviderOptions()?.events.onClose(4412, 'causal_gap')
    expect(harness.controller.getSnapshot()).toMatchObject({
      connectionStatus: 'reconnecting',
      recoverability: 'retry',
    })
  })

  it('stops with retained local state when a causal gap repeats without durable progress', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()
    harness.controller.getSnapshot().document?.getMap('test').set('title', 'Unconfirmed')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    harness.getProviderOptions()?.events.onClose(4412, 'causal_gap')
    harness.runTimer(1_000)
    await settleMicrotasks()

    harness.getProviderOptions()?.events.onClose(4412, 'causal_gap')

    const stopped = harness.controller.getSnapshot()
    expect(stopped).toMatchObject({
      connectionStatus: 'error',
      durabilityStatus: 'error',
      error: 'Earlier changes from this editor could not be restored in order. '
        + 'Your local document state is kept as a backup until you reload the document.',
      hasUnconfirmedLocalChanges: true,
      nextReconnectAt: null,
      recoverability: 'reload',
      updateRejected: true,
    })
    expect(harness.scheduledDelays()).not.toContain(2_000)
    expect(localizeEditorCollaborationNotice(stopped.error, 'de')).toBe(
      'Frühere Änderungen aus diesem Editor ließen sich nicht in der richtigen '
      + 'Reihenfolge wiederherstellen. Ihr lokaler Stand bleibt als Sicherung '
      + 'erhalten, bis Sie das Dokument neu laden.',
    )
  })

  it.each([
    ['a raw close', 4409],
    ['an in-band close', 1_000],
  ])('keeps a rejected update rejected and offers a document reload: %s', async (
    _label,
    code,
  ) => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()

    harness.getProviderOptions()?.events.onClose(code, 'invalid_schema')

    const rejected = harness.controller.getSnapshot()
    expect(rejected).toMatchObject({
      canEdit: false,
      connectionStatus: 'error',
      durabilityStatus: 'error',
      error: 'The collaboration service rejected a change from this editor. Your local '
        + 'document state is kept as a backup until you reload the document.',
      nextReconnectAt: null,
      recoverability: 'reload',
      updateRejected: true,
    })
    expect(harness.scheduledDelays()).not.toContain(1_000)
    const german = localizeEditorCollaborationNotice(rejected.error, 'de')
    expect(german).toBe(
      'Der Kollaborationsdienst hat eine Änderung aus diesem Editor abgelehnt. Ihr lokaler '
      + 'Stand bleibt als Sicherung erhalten, bis Sie das Dokument neu laden.',
    )
    expect(german).not.toContain('App-Version')
  })

  it('keeps the live document bound across a reconnect and releases it on revocation or release', async () => {
    const invalidated = Object.assign(new Error('lease invalidated'), {
      detail: { reason: 'lease_revoked' },
      status: 401,
    })
    const denied = Object.assign(new Error('resource unavailable'), { status: 404 })
    const revokedHarness = createHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockRejectedValueOnce(invalidated)
      .mockRejectedValueOnce(denied))
    expect(revokedHarness.controller.getSnapshot().liveDocumentReady).toBe(false)
    await revokedHarness.controller.start()
    expect(revokedHarness.controller.getSnapshot().liveDocumentReady).toBe(true)

    revokedHarness.getProviderOptions()?.events.onClose(1006, 'network_lost')
    expect(revokedHarness.controller.getSnapshot()).toMatchObject({
      canEdit: false,
      connectionStatus: 'reconnecting',
      liveDocumentReady: true,
      synced: false,
    })

    revokedHarness.runTimer(1_000)
    await settleMicrotasks()
    expect(revokedHarness.controller.getSnapshot()).toMatchObject({
      connectionStatus: 'access_revoked',
      liveDocumentReady: false,
    })

    const failedHarness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await failedHarness.controller.start()
    failedHarness.getProviderOptions()?.events.onClose(4409, 'invalid_schema')
    // A durability failure keeps the local state on screen, read-only.
    expect(failedHarness.controller.getSnapshot()).toMatchObject({
      canEdit: false,
      connectionStatus: 'error',
      liveDocumentReady: true,
    })

    const releasedHarness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await releasedHarness.controller.start()
    const retained = releasedHarness.controller.getSnapshot()
    releasedHarness.controller.release()
    expect(retained.liveDocumentReady).toBe(true)
    expect(releasedHarness.controller.getSnapshot().liveDocumentReady).toBe(false)

    const nextGeneration = collaborationHandleForRequestedDocument(
      retained,
      'document-1',
      3,
      true,
    )
    expect(nextGeneration).toMatchObject({ generation: 3, liveDocumentReady: false })
  })

  it('exposes a live monotonic authority revision through retained handle snapshots', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()
    const retainedHandle = harness.controller.getSnapshot()
    const writable = retainedHandle.readAuthority()

    expect(writable).toMatchObject({
      access: 'edit',
      canEdit: true,
      connectionStatus: 'connected',
      lifecycleStatus: 'saved',
      synced: true,
    })

    retainedHandle.document?.getMap('test').set('title', 'Pending')
    expect(retainedHandle.readAuthority().revision).toBe(writable.revision)

    harness.getProviderOptions()?.events.onClose(1006, 'network_lost')
    expect(retainedHandle.readAuthority()).toMatchObject({
      access: 'edit',
      canEdit: false,
      connectionStatus: 'reconnecting',
      lifecycleStatus: 'reconnecting',
      synced: false,
    })
    expect(retainedHandle.readAuthority().revision).toBeGreaterThan(writable.revision)
  })

  it('flushes before release and retains the provider until durable ack', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()
    harness.controller.getSnapshot().document?.getMap('test').set('title', 'Navigate')

    harness.controller.release()

    expect(harness.transportUpdates).toHaveLength(1)
    expect(harness.destroy).not.toHaveBeenCalled()
    await Promise.resolve()
    await Promise.resolve()
    expect(harness.controller.getSnapshot().pendingHashes).toEqual([
      'local-update-hash',
    ])
    expect(harness.destroy).not.toHaveBeenCalled()

    harness.getProviderOptions()?.events.onStateless(JSON.stringify({
      hash: 'local-update-hash',
      sequence: 5,
      type: 'durable_ack',
    }))

    expect(harness.destroy).toHaveBeenCalledOnce()
  })

  it('lets the sign-out through once a rejected lifecycle of a replaced generation handed its local state to the recovery store', async () => {
    const recovered: Array<{ documentId: string; generation: number; scope: string; title: unknown }> = []
    const stopRecovery = registerReplacedGenerationRecovery((recovery) => {
      recovered.push({
        documentId: recovery.documentId,
        generation: recovery.generation,
        scope: recovery.scope,
        title: recovery.document.getMap('test').get('title'),
      })
      return true
    })
    const rejectedKey = 'workspace-1:document-1:g2'
    const rejected = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    const controller = acquireLifecycleController(rejectedKey, null, () => rejected.controller)
    const replacedKey = 'workspace-1:document-1:g3'
    let replaced: CollaborationDocumentController | null = null
    try {
      await controller.start()
      controller.getSnapshot().document?.getMap('test').set('title', 'Refused draft')
      rejected.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      await settleMicrotasks()
      rejected.getProviderOptions()?.events.onClose(4409, 'invalid_schema')
      expect(controller.getSnapshot().updateRejected).toBe(true)
      // The document got a new generation: the surface lets the rejected
      // lifecycle go and opens the new one.
      releaseLifecycleController(rejectedKey, controller)
      expect(recovered).toEqual([])
      const fresh = createHarness(vi.fn().mockResolvedValue(session('fresh-token')), undefined, {
        generation: 3,
      })
      replaced = acquireLifecycleController(replacedKey, null, () => fresh.controller)
      await replaced.start()

      await expect(flushActiveCollaborationDocuments()).resolves.toBeUndefined()
      expect(recovered).toEqual([{
        documentId: 'document-1',
        generation: 2,
        scope: 'workspace-1',
        title: 'Refused draft',
      }])
      expect(rejected.destroy).toHaveBeenCalledOnce()
      expect(discardRejectedCollaborationLifecycle(rejectedKey, controller)).toBe(false)
    } finally {
      stopRecovery()
      if (replaced) releaseLifecycleController(replacedKey, replaced)
      discardRejectedCollaborationLifecycle(rejectedKey, controller)
    }
  })

  it('hands the rejected lifecycle to the recovery store when the server lists a newer generation of its document', async () => {
    const recovered: string[] = []
    const stopRecovery = registerReplacedGenerationRecovery((recovery) => {
      recovered.push(`${recovery.scope}:${recovery.documentId}:g${recovery.generation}`)
      return true
    })
    const rejectedKey = 'workspace-listed:document-1:g2'
    const rejected = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    const controller = acquireLifecycleController(rejectedKey, null, () => rejected.controller)
    try {
      await controller.start()
      controller.getSnapshot().document?.getMap('test').set('title', 'Refused draft')
      rejected.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      await settleMicrotasks()
      rejected.getProviderOptions()?.events.onClose(4409, 'invalid_schema')
      releaseLifecycleController(rejectedKey, controller)
      noteCollaborationDocumentGeneration({
        documentId: 'document-1',
        generation: 2,
        workspaceId: 'workspace-listed',
      })
      expect(recovered).toEqual([])

      noteCollaborationDocumentGeneration({
        documentId: 'document-1',
        generation: 3,
        workspaceId: 'workspace-listed',
      })

      expect(recovered).toEqual([rejectedKey])
      await expect(flushActiveCollaborationDocuments()).resolves.toBeUndefined()
    } finally {
      stopRecovery()
      discardRejectedCollaborationLifecycle(rejectedKey, controller)
    }
  })

  it('hands a released lifecycle of a replaced generation to the recovery store once its pending change is refused', async () => {
    const recovered: string[] = []
    const stopRecovery = registerReplacedGenerationRecovery((recovery) => {
      recovered.push(`${recovery.scope}:${recovery.documentId}:g${recovery.generation}`)
      return true
    })
    const pendingKey = 'workspace-late:document-1:g2'
    const pending = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    const controller = acquireLifecycleController(pendingKey, null, () => pending.controller)
    try {
      await controller.start()
      controller.getSnapshot().document?.getMap('test').set('title', 'Unconfirmed draft')
      pending.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      await settleMicrotasks()
      releaseLifecycleController(pendingKey, controller)
      noteCollaborationDocumentGeneration({
        documentId: 'document-1',
        generation: 3,
        workspaceId: 'workspace-late',
      })
      // Still waiting for its acknowledgement: nothing is lost yet.
      expect(recovered).toEqual([])

      pending.getProviderOptions()?.events.onClose(4409, 'invalid_schema')

      expect(recovered).toEqual([pendingKey])
      expect(pending.destroy).toHaveBeenCalledOnce()
      await expect(flushActiveCollaborationDocuments()).resolves.toBeUndefined()
    } finally {
      stopRecovery()
      discardRejectedCollaborationLifecycle(pendingKey, controller)
    }
  })

  it('keeps a rejected lifecycle of a replaced generation when no recovery store takes its local state', async () => {
    const stopRecovery = registerReplacedGenerationRecovery(() => false)
    const rejectedKey = 'workspace-kept:document-1:g2'
    const rejected = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    const controller = acquireLifecycleController(rejectedKey, null, () => rejected.controller)
    try {
      await controller.start()
      controller.getSnapshot().document?.getMap('test').set('title', 'Refused draft')
      rejected.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      await settleMicrotasks()
      rejected.getProviderOptions()?.events.onClose(4409, 'invalid_schema')
      releaseLifecycleController(rejectedKey, controller)
      noteCollaborationDocumentGeneration({
        documentId: 'document-1',
        generation: 3,
        workspaceId: 'workspace-kept',
      })

      await expect(flushActiveCollaborationDocuments()).rejects.toThrow()
      expect(rejected.destroy).not.toHaveBeenCalled()
      expect(controller.getSnapshot().document?.getMap('test').get('title')).toBe('Refused draft')
    } finally {
      stopRecovery()
      discardRejectedCollaborationLifecycle(rejectedKey, controller)
    }
  })

  it('waits for active collaboration durability before the logout boundary', async () => {
    const key = 'workspace-1:logout-boundary:g2'
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    const controller = acquireLifecycleController(key, null, () => harness.controller)
    await controller.start()
    controller.getSnapshot().document?.getMap('test').set('title', 'Before logout')

    let durable = false
    const boundary = flushActiveCollaborationDocuments().then(() => {
      durable = true
    })
    await Promise.resolve()
    await Promise.resolve()

    expect(harness.transportUpdates).toHaveLength(1)
    expect(durable).toBe(false)
    harness.getProviderOptions()?.events.onStateless(JSON.stringify({
      hash: 'local-update-hash',
      sequence: 5,
      type: 'durable_ack',
    }))
    await boundary
    expect(durable).toBe(true)
    releaseLifecycleController(key, controller)
  })

  it('reuses a released pending controller and removes it after the acked remount', async () => {
    vi.useFakeTimers()
    try {
      const key = 'workspace-1:registry-remount:g2'
      const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
      const controller = acquireLifecycleController(key, null, () => harness.controller)
      await controller.start()
      controller.getSnapshot().document?.getMap('test').set('title', 'Retained')
      harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      await Promise.resolve()
      await Promise.resolve()

      releaseLifecycleController(key, controller)
      expect(harness.destroy).not.toHaveBeenCalled()
      const remounted = acquireLifecycleController(key, null, () => {
        throw new Error('pending lifecycle entry should be reused')
      })
      expect(remounted).toBe(controller)

      harness.getProviderOptions()?.events.onStateless(JSON.stringify({
        hash: 'local-update-hash',
        sequence: 5,
        type: 'durable_ack',
      }))
      expect(harness.destroy).not.toHaveBeenCalled()
      releaseLifecycleController(key, remounted)
      // The settled controller lingers with its live session; the real
      // teardown happens only after the linger window elapses.
      expect(harness.destroy).not.toHaveBeenCalled()
      vi.advanceTimersByTime(LIFECYCLE_LINGER_MS)
      expect(harness.destroy).toHaveBeenCalledOnce()

      const replacementHarness = createHarness(
        vi.fn().mockResolvedValue(session('replacement-token')),
      )
      const replacement = acquireLifecycleController(
        key,
        null,
        () => replacementHarness.controller,
      )
      expect(replacement).toBe(replacementHarness.controller)
      releaseLifecycleController(key, replacement)
      vi.advanceTimersByTime(LIFECYCLE_LINGER_MS)
    } finally {
      vi.useRealTimers()
    }
  })

  it('disposes an exact retired lifecycle after its recovery snapshot is captured', async () => {
    const key = 'workspace-1:document-1:g2'
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    const controller = acquireLifecycleController(key, null, () => harness.controller)
    await controller.start()
    controller.getSnapshot().document?.getMap('test').set('title', 'Unconfirmed')
    expect(collaborationDocumentLifecycleHasUnconfirmedChanges({
      documentId: 'document-1',
      generation: 2,
      workspaceId: 'workspace-1',
    })).toBe(true)

    expect(retireCollaborationDocumentLifecycle({
      documentId: 'document-1',
      generation: 2,
      workspaceId: 'workspace-1',
    })).toBe(true)
    expect(harness.destroy).toHaveBeenCalledOnce()
    expect(collaborationDocumentLifecycleHasUnconfirmedChanges({
      documentId: 'document-1',
      generation: 2,
      workspaceId: 'workspace-1',
    })).toBe(false)

    const replacementHarness = createHarness(
      vi.fn().mockResolvedValue(session('replacement-token')),
    )
    const replacement = acquireLifecycleController(
      key,
      null,
      () => replacementHarness.controller,
    )
    expect(replacement).toBe(replacementHarness.controller)
    releaseLifecycleController(key, replacement)
  })

  it('retains a timed-out controller for recovery and cleans the registry after ack', async () => {
    vi.useFakeTimers()
    const key = 'workspace-1:registry-timeout:g2'
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    const controller = acquireLifecycleController(key, null, () => harness.controller)
    await controller.start()
    controller.getSnapshot().document?.getMap('test').set('title', 'Unconfirmed')

    releaseLifecycleController(key, controller)
    expect(harness.transportUpdates).toHaveLength(1)
    // The pending update rides out the linger window on the live session;
    // only then does release() start its durability timeout.
    vi.advanceTimersByTime(LIFECYCLE_LINGER_MS)
    harness.runTimer(60_000)

    expect(harness.destroy).not.toHaveBeenCalled()
    expect(harness.controller.getSnapshot()).toMatchObject({
      blockingFailure: expect.stringContaining('could not be confirmed'),
      canEdit: false,
      durabilityStatus: 'error',
    })
    expect(consumeLifecycleFailure(key)).toContain('could not be confirmed')
    const replacementHarness = createHarness(
      vi.fn().mockResolvedValue(session('replacement-token')),
    )
    const remounted = acquireLifecycleController(
      key,
      null,
      () => {
        throw new Error('the retained controller must be reused')
      },
    )
    expect(remounted).toBe(controller)
    harness.getProviderOptions()?.events.onStateless(JSON.stringify({
      hash: 'local-update-hash',
      sequence: 5,
      type: 'durable_ack',
    }))
    await Promise.resolve()
    await Promise.resolve()
    expect(remounted.getSnapshot()).toMatchObject({
      blockingFailure: null,
      durabilityStatus: 'saved',
    })
    releaseLifecycleController(key, remounted)
    vi.advanceTimersByTime(LIFECYCLE_LINGER_MS)
    expect(harness.destroy).toHaveBeenCalledOnce()

    const replacement = acquireLifecycleController(key, null, () => replacementHarness.controller)
    expect(replacement).toBe(replacementHarness.controller)
    releaseLifecycleController(key, replacement)
    vi.advanceTimersByTime(LIFECYCLE_LINGER_MS)
    vi.useRealTimers()
  })

  it('re-entering within the linger window retains the live session without a new open', async () => {
    vi.useFakeTimers()
    try {
      const key = 'workspace-1:linger-warm-reentry:g2'
      const requestSession = vi.fn().mockResolvedValue(session('initial-token'))
      const harness = createHarness(requestSession)
      const controller = acquireLifecycleController(key, null, () => harness.controller)
      await controller.start()
      expect(requestSession).toHaveBeenCalledOnce()

      releaseLifecycleController(key, controller)
      expect(harness.destroy).not.toHaveBeenCalled()
      expect(harness.disconnect).not.toHaveBeenCalled()

      const remounted = acquireLifecycleController(key, null, () => {
        throw new Error('the lingering session must be retained, not rebuilt')
      })
      expect(remounted).toBe(controller)
      // The remount effect always calls start(); on a retained lifecycle it
      // must be a no-op — the lease budget sees exactly ONE open.
      await remounted.start()
      expect(requestSession).toHaveBeenCalledOnce()
      expect(harness.connect).toHaveBeenCalledOnce()
      expect(remounted.getSnapshot()).toMatchObject({
        connectionStatus: 'connected',
        synced: true,
      })
      releaseLifecycleController(key, remounted)
      vi.advanceTimersByTime(LIFECYCLE_LINGER_MS)
      expect(harness.destroy).toHaveBeenCalledOnce()
    } finally {
      vi.useRealTimers()
    }
  })

  it('caps lingering lifecycles and tears down the oldest first', async () => {
    vi.useFakeTimers()
    try {
      const count = MAX_LINGERING_LIFECYCLES + 1
      const harnesses = Array.from({ length: count }, () => (
        createHarness(vi.fn().mockResolvedValue(session('initial-token')))
      ))
      for (const [index, harness] of harnesses.entries()) {
        const key = `workspace-1:linger-cap-${index}:g2`
        const controller = acquireLifecycleController(key, null, () => harness.controller)
        await controller.start()
        releaseLifecycleController(key, controller)
        // Distinct release instants so the LRU ordering is deterministic.
        vi.advanceTimersByTime(1)
      }
      expect(harnesses[0]!.destroy).toHaveBeenCalledOnce()
      for (const harness of harnesses.slice(1)) {
        expect(harness.destroy).not.toHaveBeenCalled()
      }
      vi.advanceTimersByTime(LIFECYCLE_LINGER_MS)
      for (const harness of harnesses) {
        expect(harness.destroy).toHaveBeenCalledOnce()
      }
    } finally {
      vi.useRealTimers()
    }
  })

  it('replaces a lingering lifecycle when the request identity changes', async () => {
    vi.useFakeTimers()
    try {
      const key = 'workspace-1:linger-identity:g2'
      const staleHarness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
      const stale = acquireLifecycleController(key, 'api-key-a', () => staleHarness.controller)
      await stale.start()
      releaseLifecycleController(key, stale)
      expect(staleHarness.destroy).not.toHaveBeenCalled()

      const freshHarness = createHarness(vi.fn().mockResolvedValue(session('fresh-token')))
      const fresh = acquireLifecycleController(key, 'api-key-b', () => freshHarness.controller)
      expect(fresh).toBe(freshHarness.controller)
      expect(staleHarness.destroy).toHaveBeenCalledOnce()
      releaseLifecycleController(key, fresh)
      vi.advanceTimersByTime(LIFECYCLE_LINGER_MS)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not linger a terminally failed lifecycle', async () => {
    vi.useFakeTimers()
    try {
      const key = 'workspace-1:linger-terminal:g2'
      const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
      const controller = acquireLifecycleController(key, null, () => harness.controller)
      await controller.start()
      controller.getSnapshot().document?.getMap('test').set('title', 'Draft')
      harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      await Promise.resolve()
      await Promise.resolve()
      harness.getProviderOptions()?.events.onStateless(JSON.stringify({
        code: 'too_large',
        hash: 'local-update-hash',
        type: 'durable_rejection',
      }))
      expect(controller.getSnapshot().connectionStatus).toBe('error')

      releaseLifecycleController(key, controller)
      // Without any timer advance the failed release must already have been
      // recorded — a linger here would hide the failure for three minutes.
      expect(consumeLifecycleFailure(key)).toContain('rejected')
    } finally {
      vi.useRealTimers()
    }
  })

  it('sweeps a superseded generation when the next one is acquired', async () => {
    vi.useFakeTimers()
    try {
      const staleHarness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
      const stale = acquireLifecycleController(
        'workspace-1:linger-generation:g2',
        null,
        () => staleHarness.controller,
      )
      await stale.start()
      releaseLifecycleController('workspace-1:linger-generation:g2', stale)
      expect(staleHarness.destroy).not.toHaveBeenCalled()

      const freshHarness = createHarness(vi.fn().mockResolvedValue(session('fresh-token')))
      const fresh = acquireLifecycleController(
        'workspace-1:linger-generation:g3',
        null,
        () => freshHarness.controller,
      )
      expect(staleHarness.destroy).toHaveBeenCalledOnce()
      releaseLifecycleController('workspace-1:linger-generation:g3', fresh)
      vi.advanceTimersByTime(LIFECYCLE_LINGER_MS)
    } finally {
      vi.useRealTimers()
    }
  })

  it('enters the reconnect ladder when the initial open is rate limited', async () => {
    const limited = Object.assign(new Error('Zu viele Kollaborationssitzungen.'), {
      detail: { reason: 'session_limit' },
      status: 429,
    })
    const requestSession = vi.fn()
      .mockRejectedValueOnce(limited)
      .mockResolvedValueOnce(session('recovered-token'))
    const harness = createHarness(requestSession)

    await harness.controller.start()

    expect(harness.controller.getSnapshot()).toMatchObject({
      connectionStatus: 'reconnecting',
      nextReconnectAt: NOW_MS + 1_000,
      reconnectAttempt: 1,
      recoverability: 'retry',
    })

    harness.runTimer(1_000)
    await Promise.resolve()
    await Promise.resolve()
    await Promise.resolve()

    expect(requestSession).toHaveBeenCalledTimes(2)
    expect(harness.controller.getSnapshot()).toMatchObject({
      connectionStatus: 'connected',
      synced: true,
    })
  })

  it('keeps a malformed initial open terminal', async () => {
    const malformed = Object.assign(new Error('invalid payload'), { status: 400 })
    const harness = createHarness(vi.fn().mockRejectedValue(malformed))

    await harness.controller.start()

    expect(harness.controller.getSnapshot()).toMatchObject({
      connectionStatus: 'error',
      nextReconnectAt: null,
      recoverability: 'retry',
    })
    expect(harness.scheduledDelays()).toEqual([])
  })

  it('mirrors exact provider-origin updates without tracking them as local durability', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()
    const remoteDocument = new Y.Doc()
    remoteDocument.getMap('test').set('remote-title', 'Published')
    const remoteUpdate = Y.encodeStateAsUpdate(remoteDocument)
    const scheduledCallsBeforeRemoteUpdate = harness.scheduleTimer.mock.calls.length

    const transportDocument = harness.getProviderOptions()?.document
    expect(transportDocument).toBeDefined()
    Y.applyUpdate(transportDocument!, remoteUpdate, harness.provider)

    const unrelatedDocument = new Y.Doc()
    unrelatedDocument.getMap('test').set('wrong-origin', 'ignored')
    Y.applyUpdate(
      transportDocument!,
      Y.encodeStateAsUpdate(unrelatedDocument),
      { source: 'not-the-provider' },
    )

    expect(harness.controller.getSnapshot().document?.getMap('test').toJSON()).toEqual({
      'remote-title': 'Published',
    })
    expect(harness.controller.getSnapshot().activityRevision).toBe(1)
    expect(harness.scheduleTimer).toHaveBeenCalledTimes(scheduledCallsBeforeRemoteUpdate)
    expect(harness.hashUpdate).not.toHaveBeenCalled()
    expect(harness.controller.getSnapshot().durabilityStatus).toBe('idle')
    remoteDocument.destroy()
    unrelatedDocument.destroy()
  })

  it('stays read-only after a durable rejection even if a later ack arrives', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()
    harness.controller.getSnapshot().document?.getMap('test').set('title', 'Draft')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await Promise.resolve()
    await Promise.resolve()

    harness.getProviderOptions()?.events.onStateless(JSON.stringify({
      code: 'too_large',
      hash: 'local-update-hash',
      type: 'durable_rejection',
    }))
    harness.getProviderOptions()?.events.onStateless(JSON.stringify({
      hash: 'local-update-hash',
      sequence: 5,
      type: 'durable_ack',
    }))

    expect(harness.disconnect).toHaveBeenCalled()
    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: false,
      connectionStatus: 'error',
      durabilityStatus: 'error',
      pendingHashes: ['local-update-hash'],
    })

    harness.controller.release()
    expect(harness.destroy).not.toHaveBeenCalled()
    expect(harness.controller.getSnapshot()).toMatchObject({
      blockingFailure: expect.stringContaining('rejected'),
      pendingHashes: ['local-update-hash'],
    })
  })

  it('rejects a document schema conflict before issuing a session', async () => {
    const requestSession = vi.fn().mockResolvedValue(session('unused-token'))
    const controller = new CollaborationDocumentController(
      {
        documentId: 'document-1',
        generation: 2,
        initialPersistedSequence: 0,
        requestSession,
        resolveWebSocketUrl: (path) => path,
        schemaVersion: EDITOR_SCHEMA_VERSION + 1,
      },
      {
        cancelTimer: vi.fn(),
        createCommandId: () => '00000000-0000-4000-8000-000000000001',
        createDocument: () => new Y.Doc(),
        createProvider: () => {
          throw new Error('must not create provider')
        },
        hashUpdate: async () => 'hash',
        now: () => NOW_MS,
        observePageLifecycle: createPageLifecycleFake().observe,
        random: () => 0.5,
        scheduleTimer: vi.fn(() => 1),
      },
    )

    await controller.start()

    expect(requestSession).not.toHaveBeenCalled()
    expect(controller.getSnapshot().connectionStatus).toBe('incompatible')
    controller.destroy()
  })
})

const AUTHOR_ID = '11111111-1111-4111-8111-111111111111'

function insertionMark(suffix = 1): Record<string, unknown> {
  return {
    insertion: {
      authorId: AUTHOR_ID,
      createdAt: NOW_MS,
      kind: 'insertion',
      patchId: `22222222-2222-4222-8222-${String(suffix).padStart(12, '0')}`,
      suggestionId: `33333333-3333-4333-8333-${String(suffix).padStart(12, '0')}`,
    },
  }
}

function seededRandom(seed: number): () => number {
  let state = (seed >>> 0) || 1
  return () => {
    state = (Math.imul(state, 1_664_525) + 1_013_904_223) >>> 0
    return state / 0x1_0000_0000
  }
}

type Harness = ReturnType<typeof createHarness>

function contentHarness(
  sessionValue: EditorCollaborationSession | (() => EditorCollaborationSession),
  options: Parameters<typeof createHarness>[2] = {},
): Harness {
  const requestSession = vi.fn(async () => (
    typeof sessionValue === 'function' ? sessionValue() : sessionValue
  ))
  return createHarness(
    requestSession,
    vi.fn(async (update: Uint8Array) => contentHash(update)),
    options,
  )
}

function suggestSession(overrides: Partial<ConfiguredSession> = {}): ConfiguredSession {
  return {
    ...session('suggest-token'),
    access: 'suggest',
    initial_write_mode: 'suggest',
    ...overrides,
  }
}

function editorDocumentOf(harness: Harness): Y.Doc {
  const document = harness.controller.getSnapshot().document
  if (!document) throw new Error('The controller has no editor document.')
  return document
}

/** Writes paragraphs into the room as another writer did, and returns the
 * update, so a replica can start from the same state. */
function seedParagraphs(
  harness: Harness,
  paragraphs: ReadonlyArray<ReadonlyArray<{ attributes?: Record<string, unknown>; text: string }>>,
): Uint8Array {
  const source = new Y.Doc()
  const root = source.getXmlFragment(EDITOR_YJS_FRAGMENT)
  source.transact(() => {
    for (const runs of paragraphs) {
      const paragraph = new Y.XmlElement('paragraph')
      root.push([paragraph])
      const text = new Y.XmlText()
      paragraph.insert(0, [text])
      let offset = 0
      for (const run of runs) {
        text.insert(offset, run.text, run.attributes)
        offset += run.text.length
      }
    }
  })
  const update = Y.encodeStateAsUpdate(source)
  source.destroy()
  const transport = harness.getProviderOptions()?.document
  if (!transport) throw new Error('The controller has no transport document.')
  Y.applyUpdate(transport, update, harness.provider)
  return update
}

function editorParagraph(harness: Harness, index: number): Y.XmlElement {
  const paragraph = editorDocumentOf(harness).getXmlFragment(EDITOR_YJS_FRAGMENT).get(index)
  if (!(paragraph instanceof Y.XmlElement)) throw new Error(`No paragraph ${index}.`)
  return paragraph
}

function editorText(harness: Harness, index: number): Y.XmlText {
  const text = editorParagraph(harness, index).get(0)
  if (!(text instanceof Y.XmlText)) throw new Error(`Paragraph ${index} has no text.`)
  return text
}

function replicaOf(updates: ReadonlyArray<Uint8Array | readonly number[]>): Y.Doc {
  const replica = new Y.Doc()
  for (const update of updates) Y.applyUpdate(replica, Uint8Array.from(update))
  return replica
}

/** The characters of a text node, without its formatting. */
function plainText(text: Y.XmlText): string {
  return text.toDelta()
    .map((operation: { insert?: unknown }) => (
      typeof operation.insert === 'string' ? operation.insert : ''
    ))
    .join('')
}

function replicaText(updates: ReadonlyArray<Uint8Array | readonly number[]>, paragraph = 0): string {
  const replica = replicaOf(updates)
  const node = replica.getXmlFragment(EDITOR_YJS_FRAGMENT).get(paragraph)
  const text = node instanceof Y.XmlElement ? node.get(0) : null
  const value = text instanceof Y.XmlText ? plainText(text) : ''
  replica.destroy()
  return value
}

/** The map keys the update at `index` added to the updates before it. */
function keysAddedBy(updates: ReadonlyArray<Uint8Array | readonly number[]>, index: number): string[] {
  const before = Object.keys(replicaOf(updates.slice(0, index)).getMap('test').toJSON())
  return Object.keys(replicaOf(updates.slice(0, index + 1)).getMap('test').toJSON())
    .filter((key) => !before.includes(key))
    .sort()
}

/** Lets the pacing interval pass and acknowledges one sent update. */
async function acknowledge(harness: Harness, update: readonly number[] | Uint8Array): Promise<void> {
  harness.advance(1_000)
  harness.ack(contentHash(update))
  await settleMicrotasks()
}

/** Acknowledges every update the controller sends until nothing waits,
 * letting time pass in between; returns every update sent meanwhile. */
async function drainSends(
  harness: Harness,
  acknowledged = new Set<string>(),
  label = 'drain',
): Promise<number[][]> {
  const before = harness.sentUpdates.length
  for (let round = 0; round < 200; round += 1) {
    harness.advance(1_000)
    // A paced send may wait up to a whole rate window; the pump recomputes
    // the wait against the clock whenever its timer runs.
    harness.runTimersUpTo(10_000)
    await settleMicrotasks()
    const open = harness.sentUpdates.filter(({ socket, update }) => (
      socket === harness.socketNumber() && !acknowledged.has(contentHash(update))
    ))
    for (const { update } of open) {
      acknowledged.add(contentHash(update))
      harness.ack(contentHash(update))
      await settleMicrotasks()
    }
    if (!harness.controller.getSnapshot().hasUnconfirmedLocalChanges) break
  }
  const snapshot = harness.controller.getSnapshot()
  expect(
    snapshot.hasUnconfirmedLocalChanges,
    `${label}: ${snapshot.connectionStatus} ${snapshot.durabilityStatus} ${snapshot.error ?? ''}`,
  ).toBe(false)
  return harness.sentUpdates.slice(before).map(({ update }) => [...update])
}

describe('collaboration sending: one update in flight', () => {
  it('sends at most one local update until the previous one is durably acknowledged', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const map = editorDocumentOf(harness).getMap('test')

    map.set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    map.set('b', 'b')
    // Past any pacing interval: only the update in flight holds b back.
    harness.advance(1_000)
    harness.runTimersUpTo(1_000)
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()

    expect(harness.sentOnSocket(1)).toHaveLength(1)
    await acknowledge(harness, harness.sentOnSocket(1)[0]!)
    expect(harness.sentOnSocket(1)).toHaveLength(2)
    expect(replicaOf(harness.sentOnSocket(1)).getMap('test').toJSON()).toEqual({ a: 'a', b: 'b' })
  })

  it('merges everything typed while an update is in flight into the next update', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const editorDocument = editorDocumentOf(harness)
    const map = editorDocument.getMap('test')
    map.set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    for (const key of ['c', 'd', 'e']) {
      map.set(key, key)
      // Past any pacing interval: only the update in flight holds them back.
      harness.advance(1_000)
      harness.runTimersUpTo(1_000)
      harness.runTimersUpTo(1_000)
      await settleMicrotasks()
    }
    expect(harness.sentOnSocket(1)).toHaveLength(1)

    await acknowledge(harness, harness.sentOnSocket(1)[0]!)

    const sent = harness.sentOnSocket(1)
    expect(sent).toHaveLength(2)
    expect(harness.hashUpdate.mock.calls.map(([update]) => [...update])).toContainEqual(sent[1])
    const replica = replicaOf(sent)
    expect(replica.getMap('test').toJSON()).toEqual(map.toJSON())
    expect(Y.encodeStateVector(replica)).toEqual(Y.encodeStateVector(editorDocument))
    replica.destroy()
  })

  it('sends updates whose union equals the editor document (guard)', async () => {
    for (let seed = 1; seed <= 20; seed += 1) {
      const random = seededRandom(seed)
      const harness = contentHarness(session('initial-token'))
      await harness.controller.start()
      const seedUpdate = seedParagraphs(harness, [[{ text: 'Seed text' }]])
      const editorDocument = editorDocumentOf(harness)
      const map = editorDocument.getMap('test')
      const text = editorText(harness, 0)
      const acknowledged = new Set<string>()
      const steps = 5 + Math.floor(random() * 11)
      for (let step = 0; step < steps; step += 1) {
        const roll = random()
        if (roll < 0.3) {
          map.set(`k${Math.floor(random() * 4)}`, `v${step}`)
        } else if (roll < 0.55) {
          text.insert(Math.floor(random() * (text.length + 1)), String.fromCharCode(97 + (step % 26)))
        } else if (roll < 0.65 && text.length > 0) {
          text.delete(Math.floor(random() * text.length), 1)
        } else if (roll < 0.85) {
          harness.runTimersUpTo(1_000)
        } else {
          const oldest = harness.sentUpdates.find(({ update }) => (
            !acknowledged.has(contentHash(update))
          ))
          if (oldest) {
            acknowledged.add(contentHash(oldest.update))
            await acknowledge(harness, oldest.update)
          }
        }
        await settleMicrotasks()
      }
      await drainSends(harness, acknowledged)
      const sent = harness.sentUpdates.map(({ update }) => update)
      const replica = replicaOf([seedUpdate, ...sent])
      expect(replica.getMap('test').toJSON(), `seed ${seed}`).toEqual(map.toJSON())
      const replicaParagraph = replica.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0) as Y.XmlElement
      expect((replicaParagraph.get(0) as Y.XmlText).toString(), `seed ${seed}`).toBe(text.toString())
      expect(Y.encodeStateVector(replica), `seed ${seed}`).toEqual(Y.encodeStateVector(editorDocument))
      const hashed = new Set(harness.hashUpdate.mock.calls.map(([update]) => contentHash(update)))
      for (const update of sent) expect(hashed.has(contentHash(update)), `seed ${seed}`).toBe(true)
      replica.destroy()
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('keeps a structure or first-suggestion boundary as its own update while an update waits', async () => {
    const harness = contentHarness(suggestSession())
    await harness.controller.start()
    const seedUpdate = seedParagraphs(harness, [[{ text: 'Hello' }]])
    const text = editorText(harness, 0)
    text.insert(5, 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()

    text.insert(6, 'x')
    text.insert(7, '/', insertionMark())
    text.insert(8, 'q', insertionMark())
    editorParagraph(harness, 0).setAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR, 'structure')
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    expect(harness.sentOnSocket(1)).toHaveLength(1)

    for (let index = 0; index < 3; index += 1) {
      await acknowledge(harness, harness.sentOnSocket(1).at(-1)!)
    }
    const [, direct, suggestion, structure] = harness.sentOnSocket(1)
    expect(harness.sentOnSocket(1)).toHaveLength(4)
    expect(updateSuggestionFormatKeys(Uint8Array.from(direct!))).toEqual([])
    expect(replicaText([seedUpdate, ...harness.sentOnSocket(1).slice(0, 2)])).toBe('Helloax')
    const suggestionKeys = updateSuggestionFormatKeys(Uint8Array.from(suggestion!))
    expect(suggestionKeys.length).toBeGreaterThan(0)
    expect(new Set(suggestionKeys)).toEqual(new Set(['insertion']))
    expect(containsStructureSuggestionAttribute(Uint8Array.from(suggestion!))).toBe(false)
    expect(replicaText([seedUpdate, ...harness.sentOnSocket(1).slice(0, 3)])).toBe('Helloax/q')
    expect(containsStructureSuggestionAttribute(Uint8Array.from(structure!))).toBe(true)
  })

  it('applies a local batch to the transport document only when it is sent', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const map = editorDocumentOf(harness).getMap('test')
    const transport = harness.getProviderOptions()!.document
    map.set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()

    map.set('x', 'x')
    harness.advance(1_000)
    harness.runTimersUpTo(1_000)
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()

    expect(harness.transportUpdates).toHaveLength(1)
    expect(transport.getMap('test').get('x')).toBeUndefined()
    await acknowledge(harness, harness.sentOnSocket(1)[0]!)
    expect(transport.getMap('test').get('x')).toBe('x')
  })

  it('after a reconnect resends the waiting update first, without a reconcile, and merges the rest after its acknowledgement', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const map = editorDocumentOf(harness).getMap('test')
    map.set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    const [a] = harness.sentOnSocket(1)
    map.set('x', 'x')
    map.set('y', 'y')

    harness.getProviderOptions()?.events.onClose(4429, 'rate_limited')
    harness.runTimer(1_000)
    await settleMicrotasks()

    expect(harness.sendStateless).not.toHaveBeenCalled()
    expect(harness.sentOnSocket(2)).toEqual([a])
    await acknowledge(harness, a!)
    const second = harness.sentOnSocket(2)
    expect(second).toHaveLength(2)
    expect(replicaOf(second).getMap('test').toJSON()).toEqual({ a: 'a', x: 'x', y: 'y' })
    await acknowledge(harness, second[1]!)
    expect(harness.controller.getSnapshot()).toMatchObject({
      durabilityStatus: 'saved',
      hasUnconfirmedLocalChanges: false,
      pendingHashes: [],
    })
  })

  it('resends a waiting update byte for byte under the same hash (guard)', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const map = editorDocumentOf(harness).getMap('test')
    map.set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    const [a] = harness.sentOnSocket(1)

    harness.getProviderOptions()?.events.onClose(1006, 'network_lost')
    map.set('x', 'x')
    harness.runTimer(1_000)
    await settleMicrotasks()
    // A close before the connection proved stable waits one step longer.
    harness.getProviderOptions()?.events.onClose(1006, 'network_lost')
    harness.runTimer(2_000)
    await settleMicrotasks()

    expect(harness.sentOnSocket(2)[0]).toEqual(a)
    expect(harness.sentOnSocket(3)[0]).toEqual(a)
    expect(
      harness.hashUpdate.mock.calls.filter(([update]) => contentHash(update) === contentHash(a!)),
    ).toHaveLength(1)
  })

  it('reconnects after an ingress overflow close with its own notice', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()

    harness.getProviderOptions()?.events.onClose(4429, 'ingress_overflow')

    const snapshot = harness.controller.getSnapshot()
    // The controller's own wording; a reword must be caught here.
    expect(snapshot).toMatchObject({
      connectionStatus: 'reconnecting',
      error: 'The collaboration service received changes from this editor faster than it '
        + 'could process them; reconnecting and sending them again.',
      recoverability: 'retry',
    })
    expect(localizeEditorCollaborationNotice(snapshot.error, 'de')).toBe(
      'Der Kollaborationsdienst hat Änderungen aus diesem Editor schneller erhalten, als er '
      + 'sie verarbeiten konnte. Inqtrix verbindet sich neu und sendet sie erneut.',
    )
  })

  it('reconnects with its own notice when a send fails and resends the update in order (guard)', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    editorDocumentOf(harness).getMap('test').set('a', 'a')
    harness.failNextSends(1)

    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()

    expect(harness.sentOnSocket(1)).toEqual([])
    const snapshot = harness.controller.getSnapshot()
    // The controller's own wording; a reword must be caught here.
    expect(snapshot).toMatchObject({
      connectionStatus: 'reconnecting',
      error: 'Local changes could not be sent to the collaboration service; reconnecting read-only.',
      hasUnconfirmedLocalChanges: true,
    })
    expect(localizeEditorCollaborationNotice(snapshot.error, 'de')).toBe(
      'Lokale Änderungen konnten nicht an den Kollaborationsdienst gesendet werden. '
      + 'Inqtrix verbindet sich automatisch erneut.',
    )
    harness.runTimer(1_000)
    await settleMicrotasks()
    const [resent] = harness.sentOnSocket(2)
    expect(replicaOf([resent!]).getMap('test').toJSON()).toEqual({ a: 'a' })
    await acknowledge(harness, resent!)
    expect(harness.controller.getSnapshot()).toMatchObject({
      durabilityStatus: 'saved',
      hasUnconfirmedLocalChanges: false,
    })
  })

  it('keeps the reconnect ladder after an ingress overflow until durable progress', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    editorDocumentOf(harness).getMap('test').set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    const [a] = harness.sentOnSocket(1)

    harness.getProviderOptions()?.events.onClose(4429, 'ingress_overflow')
    harness.runTimer(1_000)
    await settleMicrotasks()
    expect(harness.controller.getSnapshot().synced).toBe(true)
    harness.getProviderOptions()?.events.onClose(4429, 'ingress_overflow')
    expect(harness.scheduledDelays()).toContain(2_000)
    expect(harness.scheduledDelays()).not.toContain(1_000)

    harness.runTimer(2_000)
    await settleMicrotasks()
    await acknowledge(harness, a!)
    harness.getProviderOptions()?.events.onClose(4429, 'ingress_overflow')
    expect(harness.scheduledDelays()).toContain(1_000)
  })

  it('seals a merged batch before it exceeds the byte cap', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const map = editorDocumentOf(harness).getMap('test')
    map.set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()

    map.set('large', 'x'.repeat(200 * 1024))
    map.set('medium', 'y'.repeat(100 * 1024))
    map.set('small-1', 's')
    map.set('small-2', 't')
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    for (let index = 0; index < 2; index += 1) {
      await acknowledge(harness, harness.sentOnSocket(1).at(-1)!)
    }

    const sent = harness.sentOnSocket(1)
    expect(sent).toHaveLength(3)
    expect(keysAddedBy(sent, 1)).toEqual(['large'])
    expect(keysAddedBy(sent, 2)).toEqual(['medium', 'small-1', 'small-2'])
    expect(sent[2]!.length).toBeLessThanOrEqual(256 * 1024)
  })

  it('derives the merge cap from the frame limit the session publishes', async () => {
    const harness = contentHarness({ ...session('initial-token'), max_frame_bytes: 64 * 1024 })
    await harness.controller.start()
    const map = editorDocumentOf(harness).getMap('test')
    map.set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()

    map.set('paste-1', 'p'.repeat(40 * 1024))
    map.set('paste-2', 'q'.repeat(40 * 1024))
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    for (let index = 0; index < 2; index += 1) {
      await acknowledge(harness, harness.sentOnSocket(1).at(-1)!)
    }

    const sent = harness.sentOnSocket(1)
    expect(sent).toHaveLength(3)
    expect(keysAddedBy(sent, 1)).toEqual(['paste-1'])
    expect(keysAddedBy(sent, 2)).toEqual(['paste-2'])
    for (const frame of sent) expect(frame.length).toBeLessThan(64 * 1024)
  })

  it('derives the merge cap from the queued-byte limit when that is the smaller one', async () => {
    // The service admits no frame larger than the bytes it queues for a
    // socket, whatever the frame limit is. Two pastes that each fit must not
    // merge into one update that does not.
    const harness = contentHarness({
      ...session('initial-token'),
      max_frame_bytes: 2 * 1024 * 1024,
      max_queued_bytes: 64 * 1024,
    })
    await harness.controller.start()
    const map = editorDocumentOf(harness).getMap('test')
    map.set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()

    map.set('paste-1', 'p'.repeat(40 * 1024))
    map.set('paste-2', 'q'.repeat(40 * 1024))
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    await drainSends(harness)

    const sent = harness.sentOnSocket(1)
    expect(sent).toHaveLength(3)
    expect(keysAddedBy(sent, 1)).toEqual(['paste-1'])
    expect(keysAddedBy(sent, 2)).toEqual(['paste-2'])
    for (const frame of sent) expect(frame.length).toBeLessThan(64 * 1024)
  })

  it('undoes text that was sent inside a merged update without touching the rest (guard)', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const seedUpdate = seedParagraphs(harness, [[{ text: 'Seed' }]])
    const editorDocument = editorDocumentOf(harness)
    const text = editorText(harness, 0)
    const origin = { kind: 'test-editor-input' }
    const undoManager = new Y.UndoManager(text, {
      captureTimeout: 0,
      trackedOrigins: new Set([origin]),
    })
    editorDocument.getMap('test').set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()

    editorDocument.transact(() => text.insert(4, ' one'), origin)
    editorDocument.transact(() => text.insert(8, ' two'), origin)
    editorDocument.transact(() => text.insert(12, ' three'), origin)
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    await acknowledge(harness, harness.sentOnSocket(1).at(-1)!)
    undoManager.undo()
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    await acknowledge(harness, harness.sentOnSocket(1).at(-1)!)

    const sent = harness.sentOnSocket(1)
    expect(sent).toHaveLength(3)
    expect(replicaText([seedUpdate, ...sent.slice(0, 2)])).toBe('Seed one two three')
    expect(Y.decodeUpdate(Uint8Array.from(sent[2]!)).structs).toEqual([])
    expect(replicaText([seedUpdate, ...sent])).toBe('Seed one two')
    expect(plainText(text)).toBe('Seed one two')
    undoManager.destroy()
  })

  it('continues with the next update when the acknowledgement arrives before the digest', async () => {
    const digests: Array<(hash: string) => void> = []
    const hashUpdate = vi.fn((update: Uint8Array) => {
      if (digests.length === 0) {
        return new Promise<string>((resolve) => {
          digests.push(resolve)
        })
      }
      return Promise.resolve(contentHash(update))
    })
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')), hashUpdate)
    await harness.controller.start()
    const map = editorDocumentOf(harness).getMap('test')
    map.set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    const [a] = harness.sentOnSocket(1)
    map.set('b', 'b')
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    expect(harness.sentOnSocket(1)).toHaveLength(1)

    harness.advance(1_000)
    harness.ack(contentHash(a!))
    await settleMicrotasks()
    expect(harness.sentOnSocket(1)).toHaveLength(1)
    digests[0]!(contentHash(a!))
    await settleMicrotasks()

    const sent = harness.sentOnSocket(1)
    expect(sent).toHaveLength(2)
    expect(keysAddedBy(sent, 1)).toEqual(['b'])
  })

  it('shows a delay notice when an acknowledgement is overdue and clears it with the acknowledgement', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    try {
      const harness = contentHarness(session('initial-token'))
      await harness.controller.start()
      editorDocumentOf(harness).getMap('test').set('a', 'a')
      harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      await settleMicrotasks()
      const [a] = harness.sentOnSocket(1)
      expect(harness.scheduledDelays()).toContain(10_000)

      harness.runTimer(10_000)

      expect(harness.controller.getSnapshot()).toMatchObject({
        connectionStatus: 'connected',
        durabilityDelayed: true,
      })
      expect(harness.scheduledDelays()).not.toContain(1_000)
      expect(harness.sentOnSocket(1)).toHaveLength(1)
      expect(warn.mock.calls.flat().join(' ')).toContain(contentHash(a!).slice(0, 12))
      // The controller's own wording; a reword must be caught here.
      expect(COLLABORATION_NOTICES.ack_delayed).toBe(
        'The collaboration service has not confirmed the latest change for more than ten '
        + 'seconds. Your changes are kept in this editor.',
      )
      expect(localizeEditorCollaborationNotice(COLLABORATION_NOTICES.ack_delayed, 'de')).toBe(
        'Der Kollaborationsdienst hat die letzte Änderung seit mehr als zehn Sekunden nicht '
        + 'bestätigt. Ihre Änderungen bleiben in diesem Editor erhalten.',
      )

      await acknowledge(harness, a!)
      expect(harness.controller.getSnapshot().durabilityDelayed).toBe(false)
    } finally {
      warn.mockRestore()
    }
  })

  it('sends a held suggestion whose paragraph was deleted meanwhile as its own items, never as garbage-collected structs', async () => {
    const harness = contentHarness(suggestSession())
    await harness.controller.start()
    const seedUpdate = seedParagraphs(harness, [[{ text: 'Keep' }], [{ text: 'Gone' }]])
    // The first keystroke goes out and waits for its acknowledgement, so the
    // second one is held back in the editor.
    editorText(harness, 0).insert(4, 'a', insertionMark())
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    expect(harness.sentOnSocket(1)).toHaveLength(1)
    editorText(harness, 1).insert(4, 'b', insertionMark(2))
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    expect(harness.sentOnSocket(1)).toHaveLength(1)

    // Another writer deletes the second paragraph before the held keystroke
    // is sent.
    const other = replicaOf([seedUpdate])
    const otherVector = Y.encodeStateVector(other)
    other.getXmlFragment(EDITOR_YJS_FRAGMENT).delete(1, 1)
    const deletion = Y.encodeStateAsUpdate(other, otherVector)
    other.destroy()
    Y.applyUpdate(harness.getProviderOptions()!.document, deletion, harness.provider)

    await acknowledge(harness, harness.sentOnSocket(1).at(-1)!)
    harness.runTimersUpTo(10_000)
    await settleMicrotasks()

    const sent = harness.sentOnSocket(1)
    expect(sent).toHaveLength(2)
    const held = Uint8Array.from(sent[1]!)
    const kinds = Y.decodeUpdate(held).structs.map((struct) => struct.constructor.name)
    expect(kinds.length).toBeGreaterThan(0)
    expect(new Set(kinds)).toEqual(new Set(['Item']))
    expect(() => validateSuggestionYjsUpdate(held)).not.toThrow()
    // The room ends where the editor is: the deleted paragraph stays deleted.
    expect(replicaText([seedUpdate, deletion, ...sent], 0)).toBe('Keepa')
    expect(replicaOf([seedUpdate, deletion, ...sent])
      .getXmlFragment(EDITOR_YJS_FRAGMENT).length).toBe(1)
  })

  it('in suggest mode, deleting and inserting changes never share an update', async () => {
    const harness = contentHarness(suggestSession())
    await harness.controller.start()
    const seedUpdate = seedParagraphs(harness, [[{ text: 'Hello' }]])
    const text = editorText(harness, 0)
    text.insert(5, 'a', insertionMark())
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()

    text.insert(6, 'b', insertionMark())
    text.insert(7, 'c', insertionMark())
    text.delete(7, 1)
    text.delete(6, 1)
    text.insert(6, 'd', insertionMark())
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    expect(harness.sentOnSocket(1)).toHaveLength(1)
    for (let index = 0; index < 3; index += 1) {
      await acknowledge(harness, harness.sentOnSocket(1).at(-1)!)
    }

    const sent = harness.sentOnSocket(1)
    expect(sent).toHaveLength(4)
    expect(sent.slice(1).map((_update, index) => (
      replicaText([seedUpdate, ...sent.slice(0, index + 2)])
    ))).toEqual(['Helloabc', 'Helloa', 'Helload'])
    for (const update of sent) {
      expect(() => validateSuggestionYjsUpdate(Uint8Array.from(update))).not.toThrow()
    }

    // Deleting and retyping a character inside the writer's own suggestion
    // travel as two updates.
    text.insert(7, 'e', insertionMark())
    text.insert(8, 'f', insertionMark())
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    await acknowledge(harness, harness.sentOnSocket(1).at(-1)!)
    text.delete(7, 1)
    text.insert(7, 'e', insertionMark())
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    for (let index = 0; index < 2; index += 1) {
      await acknowledge(harness, harness.sentOnSocket(1).at(-1)!)
    }
    const retyped = harness.sentOnSocket(1)
    expect(retyped).toHaveLength(7)
    expect(retyped.slice(4).map((_update, index) => (
      replicaText([seedUpdate, ...retyped.slice(0, index + 5)])
    ))).toEqual(['Helloadef', 'Helloadf', 'Helloadef'])
  })

  it('a write-mode change never shares an update with the changes before it', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const seedUpdate = seedParagraphs(harness, [[{ text: 'One' }], [{ text: 'Two' }]])
    const reporter = {}
    const handle = harness.controller.getSnapshot()
    handle.markLocalWriteMode('suggest', reporter)
    editorText(harness, 0).insert(3, 'x', insertionMark())
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    editorText(harness, 0).insert(4, 'y', insertionMark())
    handle.markLocalWriteMode('edit', reporter)
    handle.markLocalWriteMode('edit', reporter)
    editorText(harness, 1).insert(3, 'z')
    editorText(harness, 1).insert(4, 'w')
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    for (let index = 0; index < 2; index += 1) {
      await acknowledge(harness, harness.sentOnSocket(1).at(-1)!)
    }

    const sent = harness.sentOnSocket(1)
    expect(sent).toHaveLength(3)
    expect(replicaText([seedUpdate, ...sent.slice(0, 2)], 0)).toBe('Onexy')
    expect(replicaText([seedUpdate, ...sent.slice(0, 2)], 1)).toBe('Two')
    expect(replicaText([seedUpdate, ...sent], 1)).toBe('Twozw')
    expect(updateSuggestionFormatKeys(Uint8Array.from(sent[2]!))).toEqual([])
  })

  it('an edit-mode change in a paragraph that carries a suggestion is sent alone', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const seedUpdate = seedParagraphs(harness, [
      [{ attributes: insertionMark(), text: 'Mark' }],
      [{ text: 'Plain' }],
    ])
    editorText(harness, 1).insert(5, 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()

    editorText(harness, 0).insert(2, 'Q')
    editorText(harness, 1).insert(6, 'r')
    editorText(harness, 1).insert(7, 's')
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    expect(harness.sentOnSocket(1)).toHaveLength(1)
    for (let index = 0; index < 2; index += 1) {
      await acknowledge(harness, harness.sentOnSocket(1).at(-1)!)
    }

    const sent = harness.sentOnSocket(1)
    expect(sent).toHaveLength(3)
    expect(replicaText([seedUpdate, ...sent.slice(0, 2)], 0)).toBe('MaQrk')
    expect(replicaText([seedUpdate, ...sent.slice(0, 2)], 1)).toBe('Plaina')
    expect(replicaText([seedUpdate, ...sent], 1)).toBe('Plainars')
  })

  it('the controller stamps local updates as edit until an editor reports, and a stale editor\'s end never resets a newer report', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    seedParagraphs(harness, [
      [{ attributes: insertionMark(), text: 'Mark' }],
      [{ text: 'Plain' }],
    ])
    const handle = harness.controller.getSnapshot()
    const acknowledged = new Set<string>()
    const sendsFor = async (type: () => void): Promise<number> => {
      const before = harness.sentUpdates.length
      type()
      harness.runTimersUpTo(1_000)
      await settleMicrotasks()
      await drainSends(harness, acknowledged)
      return harness.sentUpdates.length - before
    }

    // No editor has reported: the in-span edit travels alone.
    expect(await sendsFor(() => {
      editorText(harness, 1).insert(5, 'a')
      harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      editorText(harness, 0).insert(2, 'Q')
      editorText(harness, 1).insert(6, 'r')
    })).toBe(3)

    const first = {}
    const second = {}
    handle.markLocalWriteMode('suggest', first)
    handle.markLocalWriteMode('suggest', second)
    handle.endLocalWriteMode(first)
    const suggestionKeystrokes = (count: number, at: number) => () => {
      editorText(harness, 1).insert(at, 'k', insertionMark(7))
      harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      for (let index = 1; index < count; index += 1) {
        editorText(harness, 1).insert(at + index, 'k', insertionMark(7))
      }
    }
    expect(await sendsFor(suggestionKeystrokes(4, 7))).toBe(2)

    handle.endLocalWriteMode(second)
    expect(await sendsFor(suggestionKeystrokes(3, 11))).toBe(3)
  })

  it('sends a local update without transaction facts alone and warns once', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    try {
      const harness = contentHarness(session('initial-token'))
      await harness.controller.start()
      const editorDocument = editorDocumentOf(harness)
      const map = editorDocument.getMap('test')
      map.set('a', 'a')
      harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      await settleMicrotasks()

      // An update event whose transaction never passed afterTransaction.
      const unknown = (key: string) => {
        const other = new Y.Doc()
        other.getMap('test').set(key, key)
        const update = Y.encodeStateAsUpdate(other)
        other.destroy()
        editorDocument.emit('update', [update, null, editorDocument, {} as Y.Transaction])
      }
      unknown('unknown-1')
      map.set('b', 'b')
      unknown('unknown-2')
      map.set('c', 'c')
      harness.runTimersUpTo(1_000)
      await settleMicrotasks()
      for (let index = 0; index < 4; index += 1) {
        await acknowledge(harness, harness.sentOnSocket(1).at(-1)!)
      }

      const sent = harness.sentOnSocket(1)
      expect(sent.map((_update, index) => keysAddedBy(sent, index))).toEqual([
        ['a'],
        ['unknown-1'],
        ['b'],
        ['unknown-2'],
        ['c'],
      ])
      expect(warn).toHaveBeenCalledOnce()
    } finally {
      warn.mockRestore()
    }
  })

  it('does not send a formatting cleanup that Yjs runs while the controller applies a remote update', async () => {
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    try {
      const harness = contentHarness(session('initial-token'))
      await harness.controller.start()
      const seedUpdate = seedParagraphs(harness, [[{ text: 'Hello world' }]])
      editorText(harness, 0).format(0, 5, { bold: true })
      harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
      await settleMicrotasks()
      await acknowledge(harness, harness.sentOnSocket(1)[0]!)
      const sentBefore = harness.sentUpdates.length

      const other = new Y.Doc()
      Y.applyUpdate(other, seedUpdate)
      const vector = Y.encodeStateVector(other)
      const otherText = (other.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0) as Y.XmlElement)
        .get(0) as Y.XmlText
      otherText.format(0, 5, { bold: true })
      Y.applyUpdate(
        harness.getProviderOptions()!.document,
        Y.encodeStateAsUpdate(other, vector),
        harness.provider,
      )
      other.destroy()
      harness.advance(1_000)
      harness.runTimersUpTo(1_000)
      await settleMicrotasks()

      expect(harness.sentUpdates.length).toBe(sentBefore)
      expect(harness.controller.getSnapshot().hasUnconfirmedLocalChanges).toBe(false)
      expect(debug).toHaveBeenCalledOnce()
      expect(warn).not.toHaveBeenCalled()

      // Any other local change inside a remote apply is sent, with a warning.
      const text = editorText(harness, 0)
      let written = false
      text.observe(() => {
        if (written) return
        written = true
        text.insert(0, '!')
      })
      const third = new Y.Doc()
      Y.applyUpdate(third, Y.encodeStateAsUpdate(editorDocumentOf(harness)))
      const thirdVector = Y.encodeStateVector(third)
      ;((third.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0) as Y.XmlElement).get(0) as Y.XmlText)
        .insert(11, '?')
      Y.applyUpdate(
        harness.getProviderOptions()!.document,
        Y.encodeStateAsUpdate(third, thirdVector),
        harness.provider,
      )
      third.destroy()
      harness.runTimersUpTo(1_000)
      await settleMicrotasks()

      expect(harness.sentUpdates.length).toBe(sentBefore + 1)
      expect(warn).toHaveBeenCalledOnce()
    } finally {
      debug.mockRestore()
      warn.mockRestore()
    }
  })
})

/** The first text of the first paragraph of a replica or editor document. */
function firstText(document: Y.Doc): Y.XmlText {
  const paragraph = document.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0)
  const text = paragraph instanceof Y.XmlElement ? paragraph.get(0) : null
  if (!(text instanceof Y.XmlText)) throw new Error('The document has no first text.')
  return text
}

/** Every deleted item of a document, as `client:clock`. */
function deletedItems(document: Y.Doc): string[] {
  const deleted: string[] = []
  for (const [client, ranges] of Y.createDeleteSetFromStructStore(document.store).clients) {
    for (const range of ranges) {
      for (let clock = range.clock; clock < range.clock + range.len; clock += 1) {
        deleted.push(`${client}:${clock}`)
      }
    }
  }
  return deleted.sort()
}

/** An update another writer makes on `base`. */
function remoteChange(
  base: ReadonlyArray<Uint8Array | readonly number[]>,
  change: (text: Y.XmlText) => void,
): Uint8Array {
  const replica = replicaOf(base)
  const vector = Y.encodeStateVector(replica)
  change(firstText(replica))
  const update = Y.encodeStateAsUpdate(replica, vector)
  replica.destroy()
  return update
}

/** Leaves a redundant format item at the start of the editor's text, the
 * way the room holds it too: one writer bolds `a` in `abcdef`, another
 * deletes `abc` on a state without the bold run. The deletion's cleanup
 * removes the start of the bold run and keeps its end, which sets nothing
 * any more. Returns the room's updates in order and that item. */
async function seedRedundantBoldEnd(harness: Harness): Promise<{
  redundantEnd: string
  roomUpdates: Uint8Array[]
}> {
  const seed = seedParagraphs(harness, [[{ text: 'abcdef' }]])
  const bolded = remoteChange([seed], (text) => text.format(0, 1, { bold: true }))
  const deleted = remoteChange([seed], (text) => text.delete(0, 3))
  const transport = harness.getProviderOptions()!.document
  Y.applyUpdate(transport, bolded, harness.provider)
  Y.applyUpdate(transport, deleted, harness.provider)
  await settleMicrotasks()
  const end = Y.decodeUpdate(bolded).structs.filter((struct) => (
    struct instanceof Y.Item
    && struct.content instanceof Y.ContentFormat
    && struct.content.value === null
  ))
  if (end.length !== 1) throw new Error('The bold run has no single end item.')
  const redundantEnd = `${end[0]!.id.client}:${end[0]!.id.clock}`
  expect(deletedItems(transport)).not.toContain(redundantEnd)
  expect(deletedItems(editorDocumentOf(harness))).toEqual(deletedItems(transport))
  return { redundantEnd, roomUpdates: [seed, bolded, deleted] }
}

/** How the room reads the first text after `updates`, in room order. */
function roomDelta(updates: ReadonlyArray<Uint8Array | readonly number[]>): unknown {
  const room = replicaOf(updates)
  const delta = firstText(room).toDelta()
  room.destroy()
  return delta
}

// Yjs removes format items an applied update left redundant, in a local
// transaction of its own. The editor and the transport are two documents and
// run their cleanups on two states; the room runs a third one on its own
// state and order. A deletion only one replica holds changes nothing visible
// at first, but text a writer later places next to the item then reads
// differently there than everywhere else.
describe('collaboration sending: textblocks the editor creates', () => {
  // The origin the editor's binding writes local changes under.
  const EDITOR_ORIGIN = Object.freeze({ kind: 'editor-binding' })

  /** The children of every top-level block of a document: `text:` and the
   * characters of a text node, or the name of an element. */
  function blockChildren(document: Y.Doc): string[][] {
    return document.getXmlFragment(EDITOR_YJS_FRAGMENT).toArray().map((block) => (
      block instanceof Y.XmlElement
        ? block.toArray().map((child) => (
          child instanceof Y.XmlText ? `text:${plainText(child)}` : (child as Y.XmlElement).nodeName
        ))
        : []
    ))
  }

  /** Writes into the room, as another writer did, a line without any child:
   * what an editor without the empty text node writes. */
  function seedLineWithoutTextNode(harness: Harness, index: number): Uint8Array {
    const transport = harness.getProviderOptions()!.document
    const source = new Y.Doc()
    Y.applyUpdate(source, Y.encodeStateAsUpdate(transport))
    const vector = Y.encodeStateVector(source)
    source.transact(() => {
      source.getXmlFragment(EDITOR_YJS_FRAGMENT).insert(index, [new Y.XmlElement('paragraph')])
    })
    const update = Y.encodeStateAsUpdate(source, vector)
    source.destroy()
    Y.applyUpdate(transport, update, harness.provider)
    return update
  }

  it('sends a textblock the editor created in the same update as its one empty text node', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const seedUpdate = seedParagraphs(harness, [[{ text: 'Oben.' }], [{ text: '' }], [{ text: 'Unten.' }]])
    const document = editorDocumentOf(harness)
    const root = document.getXmlFragment(EDITOR_YJS_FRAGMENT)

    // Enter at the end of the first line: a new line without content.
    document.transact(() => root.insert(1, [new Y.XmlElement('paragraph')]), EDITOR_ORIGIN)
    expect(blockChildren(document)).toEqual([['text:Oben.'], ['text:'], ['text:'], ['text:Unten.']])
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    await acknowledge(harness, harness.sentOnSocket(1)[0]!)

    // The stored empty line turned into a heading: the change deletes a
    // block, so it travels alone, and the heading leaves with its text node.
    editorText(harness, 0).insert(5, '!')
    document.transact(() => {
      root.delete(2, 1)
      root.insert(2, [new Y.XmlElement('heading')])
    }, EDITOR_ORIGIN)
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    await acknowledge(harness, harness.sentOnSocket(1)[1]!)
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()

    const sent = harness.sentOnSocket(1)
    expect(sent).toHaveLength(3)
    // Each update, applied after the ones before it, already holds every
    // created textblock with its text node.
    const afterEnter = replicaOf([seedUpdate, sent[0]!])
    expect(blockChildren(afterEnter)).toEqual([['text:Oben.'], ['text:'], ['text:'], ['text:Unten.']])
    expect(() => validateEditorYDoc(afterEnter)).not.toThrow()
    const afterTyping = replicaOf([seedUpdate, ...sent.slice(0, 2)])
    expect(blockChildren(afterTyping)).toEqual([['text:Oben.!'], ['text:'], ['text:'], ['text:Unten.']])
    const afterHeading = replicaOf([seedUpdate, ...sent])
    expect(afterHeading.getXmlFragment(EDITOR_YJS_FRAGMENT).toArray().map((block) => (
      (block as Y.XmlElement).nodeName
    ))).toEqual(['paragraph', 'paragraph', 'heading', 'paragraph'])
    expect(blockChildren(afterHeading)).toEqual([['text:Oben.!'], ['text:'], ['text:'], ['text:Unten.']])
    expect(Y.encodeStateVector(afterHeading)).toEqual(Y.encodeStateVector(document))
    for (const replica of [afterEnter, afterTyping, afterHeading]) replica.destroy()
  })

  it('writes nothing while it applies a room update, and never completes a line another writer created', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    seedParagraphs(harness, [[{ text: 'Oben.' }], [{ text: 'Unten.' }]])
    const document = editorDocumentOf(harness)
    const localUpdates: Uint8Array[] = []
    document.on('update', (update: Uint8Array, origin: unknown) => {
      if (origin !== harness.provider) localUpdates.push(update)
    })

    seedLineWithoutTextNode(harness, 1)
    expect(blockChildren(document)).toEqual([['text:Oben.'], [], ['text:Unten.']])
    // Applying the room's update is one transaction of the controller's own
    // origin: nothing else was written.
    expect(localUpdates).toHaveLength(1)
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    expect(harness.sentOnSocket(1)).toEqual([])

    // A later change of this editor elsewhere leaves that line as it is.
    document.transact(() => editorText(harness, 0).insert(5, '!'), EDITOR_ORIGIN)
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    expect(blockChildren(document)).toEqual([['text:Oben.!'], [], ['text:Unten.']])
    expect(harness.sentOnSocket(1)).toHaveLength(1)
    expect(Y.decodeUpdate(Uint8Array.from(harness.sentOnSocket(1)[0]!)).structs
      .some((struct) => struct instanceof Y.Item && struct.content instanceof Y.ContentType))
      .toBe(false)
    expect(warn).not.toHaveBeenCalled()
    warn.mockRestore()
  })

  it('undoes an Enter and its text node in one step and redoes one text node', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    seedParagraphs(harness, [[{ text: 'Oben.' }], [{ text: 'Unten.' }]])
    const document = editorDocumentOf(harness)
    const root = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
    // Configured as the editor's undo plugin configures it. Its delete filter
    // lets an undo remove a paragraph it inserted only while that paragraph
    // is empty, so the text node must belong to the same undo step as the
    // Enter, or the undo would leave the new line in place.
    const undo = new Y.UndoManager(root, {
      captureTransaction: (transaction) => transaction.meta.get('addToHistory') !== false,
      deleteFilter: (item) => defaultDeleteFilter(item, defaultProtectedNodes),
      trackedOrigins: new Set([EDITOR_ORIGIN]),
    })

    document.transact(() => root.insert(1, [new Y.XmlElement('paragraph')]), EDITOR_ORIGIN)
    expect(blockChildren(document)).toEqual([['text:Oben.'], ['text:'], ['text:Unten.']])
    expect(undo.undoStack).toHaveLength(1)
    undo.undo()
    expect(blockChildren(document)).toEqual([['text:Oben.'], ['text:Unten.']])
    undo.redo()
    expect(blockChildren(document)).toEqual([['text:Oben.'], ['text:'], ['text:Unten.']])
    expect([undo.undoStack.length, undo.redoStack.length]).toEqual([1, 0])

    await drainSends(harness)
    const transport = harness.getProviderOptions()!.document
    expect(Y.encodeStateVector(transport)).toEqual(Y.encodeStateVector(document))
    expect(blockChildren(transport)).toEqual(blockChildren(document))
    undo.destroy()
  })
})

describe('collaboration sending: formatting cleanups', () => {
  it('applies the cleanup the transport runs for a local batch to the editor document', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const { redundantEnd } = await seedRedundantBoldEnd(harness)
    const transport = harness.getProviderOptions()!.document
    const deletedBefore = deletedItems(transport).length
    editorText(harness, 0).format(2, 1, { italic: true })
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()

    expect(harness.sentOnSocket(1)).toHaveLength(1)
    // The batch deletes nothing; the transport removed the redundant item
    // on its own when it applied the batch.
    expect(deletedItems(transport)).toEqual(
      [...deletedItems(editorDocumentOf(harness))].sort(),
    )
    expect(deletedItems(transport)).toHaveLength(deletedBefore + 1)
    expect(deletedItems(transport)).toContain(redundantEnd)
  })

  it('sends the transport\'s cleanup of a local batch in the same update, so a change the room applied first reads the same', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const { roomUpdates } = await seedRedundantBoldEnd(harness)
    const transport = harness.getProviderOptions()!.document
    // A third writer types a bold letter at the start; the room applies it
    // before this editor's batch arrives. The redundant item ends its bold
    // run there.
    const typed = remoteChange(roomUpdates, (text) => text.insert(0, 'Q', { bold: true }))
    editorText(harness, 0).format(2, 1, { italic: true })
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    const [sent] = harness.sentOnSocket(1)
    Y.applyUpdate(transport, typed, harness.provider)
    await acknowledge(harness, sent!)

    expect(harness.sentOnSocket(1)).toHaveLength(1)
    expect(plainText(editorText(harness, 0))).toBe('Qdef')
    expect(roomDelta([...roomUpdates, typed, sent!])).toEqual(editorText(harness, 0).toDelta())
  })

  it('sends the transport\'s cleanup of a remote change that met an unconfirmed update of this editor, behind it', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const seed = seedParagraphs(harness, [[{ text: 'Hello world' }]])
    editorText(harness, 0).format(6, 5, { bold: true })
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    const [bolded] = harness.sentOnSocket(1)
    // Another writer deleted the word on a state without the bold run; the
    // room applied that first and this editor's bold run after it.
    const deleted = remoteChange([seed], (text) => text.delete(6, 5))
    Y.applyUpdate(harness.getProviderOptions()!.document, deleted, harness.provider)
    await settleMicrotasks()
    expect(harness.sentOnSocket(1)).toHaveLength(1)

    await acknowledge(harness, bolded!)
    const [, cleanup] = harness.sentOnSocket(1)
    expect(cleanup).toBeDefined()
    // A third writer types bold text where the word was, on the room's
    // state before the cleanup reached it.
    const typed = remoteChange(
      [seed, deleted, bolded!],
      (text) => text.insert(6, 'Y', { bold: true }),
    )
    Y.applyUpdate(harness.getProviderOptions()!.document, typed, harness.provider)
    await acknowledge(harness, cleanup!)

    expect(plainText(editorText(harness, 0))).toBe('Hello Y')
    expect(roomDelta([seed, deleted, bolded!, typed, cleanup!]))
      .toEqual(editorText(harness, 0).toDelta())
    expect(harness.controller.getSnapshot().hasUnconfirmedLocalChanges).toBe(false)
  })

  it('sends the editor\'s cleanup of its unsent changes that the transport never ran', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const seed = seedParagraphs(harness, [[{ text: 'Hello world' }]])
    // Unsent: the bold run still waits in the open batch when another
    // writer's deletion of the word arrives.
    editorText(harness, 0).format(6, 5, { bold: true })
    const deleted = remoteChange([seed], (text) => text.delete(6, 5))
    Y.applyUpdate(harness.getProviderOptions()!.document, deleted, harness.provider)
    harness.runTimersUpTo(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    const sent = [...harness.sentOnSocket(1)]
    for (const update of sent) await acknowledge(harness, update)
    const typed = remoteChange(
      [seed, deleted, ...sent],
      (text) => text.insert(6, 'Y', { bold: true }),
    )
    Y.applyUpdate(harness.getProviderOptions()!.document, typed, harness.provider)
    await settleMicrotasks()

    expect(plainText(editorText(harness, 0))).toBe('Hello Y')
    expect(roomDelta([seed, deleted, ...sent, typed])).toEqual(editorText(harness, 0).toDelta())
  })
})

/** A suggest-access writer with one update in flight and `batches` further
 * updates that each seal the one before (insertions and deletions
 * alternate, which a suggestion update never mixes). */
async function pageExitHarness(limits: SessionLimits, batches: number) {
  const harness = contentHarness(suggestSession(limits))
  await harness.controller.start()
  const seedUpdate = seedParagraphs(harness, [[{ text: 'Hello world' }]])
  const text = editorText(harness, 0)
  text.insert(11, 'a')
  harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
  await settleMicrotasks()
  const [inFlight] = harness.sentOnSocket(1)
  const expected: string[] = []
  typeAlternating(text, batches, expected)
  await settleMicrotasks()
  return { expected, harness, inFlight: inFlight!, seedUpdate, text }
}

function typeAlternating(text: Y.XmlText, count: number, expected: string[]): void {
  for (let index = 0; index < count; index += 1) {
    const position = expected.length
    if (position % 2 === 0) {
      text.insert(text.length, String.fromCharCode(98 + (position % 20)))
    } else {
      text.delete(text.length - 1, 1)
    }
    expected.push(plainText(text))
  }
}

describe('collaboration sending: page exit', () => {
  it('a page exit sends every assembled update at once, in production order, within the published limits and the updates still waiting', async () => {
    const { expected, harness, seedUpdate } = await pageExitHarness({ max_queued_frames: 8 }, 6)
    const before = harness.sentOnSocket(1).length

    harness.lifecycle.exit('pagehide')

    // Synchronously, without any timer: three frames (4 = Q - 4 with the one
    // in flight), one per batch, in production order.
    const sent = harness.sentOnSocket(1)
    expect(sent.length - before).toBe(3)
    expect(sent.slice(1).map((_update, index) => (
      replicaText([seedUpdate, ...sent.slice(0, index + 2)])
    ))).toEqual(expected.slice(0, 3))
    await settleMicrotasks()
    expect(harness.controller.getSnapshot().pendingHashes).toHaveLength(4)
    expect(harness.controller.getSnapshot().hasUnconfirmedLocalChanges).toBe(true)
    expect(harness.lifecycle.guard()?.()).toBe(true)
    expect(harness.leaveAwareness).toHaveBeenCalledOnce()
  })

  it('a page exit that carries every batch leaves the page free to go', async () => {
    const { expected, harness, seedUpdate } = await pageExitHarness({ max_queued_frames: 8 }, 2)

    harness.lifecycle.exit('pagehide')

    const sent = harness.sentOnSocket(1)
    expect(sent).toHaveLength(3)
    expect(replicaText([seedUpdate, ...sent])).toBe(expected[1])
    expect(harness.lifecycle.guard()?.()).toBe(false)
  })

  it('a page exit sends nothing on a socket that has not replayed its unconfirmed updates', async () => {
    const { harness } = await pageExitHarness({ max_queued_frames: 8 }, 3)
    harness.getProviderOptions()?.events.onClose(1006, 'network_lost')
    // The next socket is open but has not authenticated yet.
    harness.socketState.open = true
    const before = harness.sentUpdates.length

    harness.lifecycle.exit('pagehide')

    expect(harness.sentUpdates).toHaveLength(before)
    expect(harness.lifecycle.guard()?.()).toBe(true)
  })

  it('a page exit sends nothing while the socket is already closing', async () => {
    const { harness } = await pageExitHarness({ max_queued_frames: 8 }, 3)
    harness.socketState.open = false
    const before = harness.sentUpdates.length

    harness.lifecycle.exit('pagehide')

    expect(harness.sentUpdates).toHaveLength(before)
    expect(harness.leaveAwareness).not.toHaveBeenCalled()
    expect(harness.lifecycle.guard()?.()).toBe(true)
  })

  it('a page restored from the back-forward cache resends one waiting update per acknowledgement, byte for byte', async () => {
    const { expected, harness, inFlight, seedUpdate } = await pageExitHarness({ max_queued_frames: 8 }, 6)
    harness.lifecycle.exit('pagehide')
    const burst = harness.sentOnSocket(1)
    harness.getProviderOptions()?.events.onClose(1001, 'page_hidden')

    harness.lifecycle.show(true)
    expect(harness.restoreAwareness).toHaveBeenCalledOnce()
    harness.runTimer(1_000)
    await settleMicrotasks()

    // Before the first acknowledgement only the oldest waiting update.
    expect(harness.sentOnSocket(2)).toEqual([inFlight])
    for (let index = 0; index < 6; index += 1) {
      await acknowledge(harness, harness.sentOnSocket(2).at(-1)!)
    }
    const second = harness.sentOnSocket(2)
    expect(second).toHaveLength(7)
    expect(second.slice(0, 4)).toEqual(burst)
    expect(replicaText([seedUpdate, ...second])).toBe(expected[5])
  })

  it('leaving the editor inside the app keeps sending one update per acknowledgement', async () => {
    const { expected, harness, inFlight, seedUpdate } = await pageExitHarness({}, 1)
    const before = harness.sentUpdates.length

    harness.controller.flushPendingLocalUpdates()

    expect(harness.sentUpdates).toHaveLength(before)
    expect(harness.controller.getSnapshot().hasUnconfirmedLocalChanges).toBe(true)
    await acknowledge(harness, inFlight)
    expect(harness.sentUpdates).toHaveLength(before + 1)
    expect(replicaText([seedUpdate, ...harness.sentOnSocket(1)])).toBe(expected[0])
  })

  it('a second page exit before the first burst is confirmed sends nothing more', async () => {
    const { expected, harness, text } = await pageExitHarness({ max_queued_frames: 8 }, 5)
    harness.lifecycle.exit('hidden')
    expect(harness.sentOnSocket(1)).toHaveLength(4)
    expect(harness.leaveAwareness).not.toHaveBeenCalled()
    typeAlternating(text, 2, expected)

    harness.lifecycle.exit('pagehide')

    expect(harness.sentOnSocket(1)).toHaveLength(4)
    expect(harness.lifecycle.guard()?.()).toBe(true)
    expect(harness.leaveAwareness).toHaveBeenCalledOnce()

    const reverse = await pageExitHarness({ max_queued_frames: 8 }, 5)
    reverse.harness.lifecycle.exit('pagehide')
    expect(reverse.harness.sentOnSocket(1)).toHaveLength(4)
    reverse.harness.lifecycle.exit('hidden')
    expect(reverse.harness.sentOnSocket(1)).toHaveLength(4)
  })

  it('the page-exit flush runs when the browser announces the unload, and the browser asks only for what remains', async () => {
    const { expected, harness, seedUpdate } = await pageExitHarness({ max_queued_frames: 8 }, 2)

    expect(harness.lifecycle.beforeUnload()).toBe(false)

    const sent = harness.sentOnSocket(1)
    expect(sent).toHaveLength(3)
    expect(replicaText([seedUpdate, ...sent])).toBe(expected[1])
    // The user may still stay on the page: the presence stays.
    expect(harness.leaveAwareness).not.toHaveBeenCalled()
    harness.lifecycle.exit('pagehide')
    expect(harness.sentOnSocket(1)).toHaveLength(3)
    expect(harness.leaveAwareness).toHaveBeenCalledOnce()

    const backlog = await pageExitHarness({ max_queued_frames: 8 }, 6)
    expect(backlog.harness.lifecycle.beforeUnload()).toBe(true)
    expect(backlog.harness.sentOnSocket(1)).toHaveLength(4)
    backlog.harness.lifecycle.exit('hidden')
    backlog.harness.lifecycle.exit('pagehide')
    expect(backlog.harness.sentOnSocket(1)).toHaveLength(4)
  })

  it('a page exit keeps the unconfirmed updates within the queue the service holds before a room opens', async () => {
    const { harness } = await pageExitHarness(
      { max_queued_frames: 128, update_rate_count: 200 },
      70,
    )

    harness.lifecycle.exit('pagehide')

    // 60 = the 64 frames queued before a room opens, less the control frames.
    expect(harness.sentOnSocket(1)).toHaveLength(60)
    await settleMicrotasks()
    expect(harness.controller.getSnapshot().pendingHashes).toHaveLength(60)
    expect(harness.lifecycle.guard()?.()).toBe(true)
  })

  it('a page exit keeps the unconfirmed updates within the update allowance of the next socket', async () => {
    const { expected, harness, text } = await pageExitHarness({ update_rate_count: 20 }, 20)
    harness.lifecycle.exit('pagehide')
    // 12 = the allowance of 20 less its margin of 8, one of them in flight.
    expect(harness.sentOnSocket(1)).toHaveLength(12)

    // A window later the allowance is free again, the unconfirmed updates
    // still are not.
    harness.advance(11_000)
    typeAlternating(text, 20, expected)
    harness.lifecycle.exit('pagehide')

    expect(harness.sentOnSocket(1)).toHaveLength(12)
    await settleMicrotasks()
    expect(harness.controller.getSnapshot().pendingHashes).toHaveLength(12)
  })

  it('a page exit sends no more bytes than the published frame and queue limits hold', async () => {
    const harness = contentHarness({
      ...session('initial-token'),
      max_frame_bytes: 64 * 1024,
      max_queued_bytes: 64 * 1024,
    })
    await harness.controller.start()
    const map = editorDocumentOf(harness).getMap('test')
    map.set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    for (let index = 0; index < 6; index += 1) {
      map.set(`paste-${index}`, String.fromCharCode(97 + index).repeat(10 * 1024))
    }

    harness.lifecycle.exit('pagehide')

    const sent = harness.sentOnSocket(1)
    expect(sent).toHaveLength(5)
    expect(sent.reduce((total, update) => total + update.length, 0)).toBeLessThanOrEqual(48 * 1024)
    expect(harness.lifecycle.guard()?.()).toBe(true)
  })

  it('the page asks before leaving only while changes would remain unsent', async () => {
    const idle = contentHarness(session('initial-token'))
    await idle.controller.start()
    expect(idle.lifecycle.guardCalls().at(-1)).toBeNull()

    // One update in flight, nothing else: a page exit carries it.
    editorDocumentOf(idle).getMap('test').set('a', 'a')
    idle.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    expect(idle.lifecycle.guard()?.()).toBe(false)

    // A closed socket cannot carry it.
    idle.getProviderOptions()?.events.onClose(1006, 'network_lost')
    expect(idle.lifecycle.guard()?.()).toBe(true)
    idle.runTimer(1_000)
    await settleMicrotasks()
    await acknowledge(idle, idle.sentOnSocket(2)[0]!)
    expect(idle.lifecycle.guardCalls().at(-1)).toBeNull()

    // A durability failure keeps the local state; leaving would lose it.
    const failed = contentHarness(session('initial-token'))
    await failed.controller.start()
    editorDocumentOf(failed).getMap('test').set('a', 'a')
    failed.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    failed.getProviderOptions()?.events.onClose(4403, 'suggestion_policy_violation')
    expect(failed.lifecycle.guard()?.()).toBe(true)

    // More waiting batches than a page exit may send.
    const backlog = await pageExitHarness({ max_queued_frames: 8 }, 6)
    expect(backlog.harness.lifecycle.guard()?.()).toBe(true)

    // Decided when the browser asks, not at the last state change.
    const closing = await pageExitHarness({ max_queued_frames: 8 }, 1)
    expect(closing.harness.lifecycle.guard()?.()).toBe(false)
    closing.harness.socketState.open = false
    expect(closing.harness.lifecycle.guard()?.()).toBe(true)
  })

  it('keeps every unconfirmed update within the published limits across random page exits and reconnects', async () => {
    const limitSets: Array<{ bytes?: boolean; C: number; Q: number }> = [
      { C: 120, Q: 5 },
      { C: 120, Q: 8 },
      { C: 120, Q: 32 },
      { C: 20, Q: 32 },
      { C: 9, Q: 32 },
      { C: 200, Q: 128 },
      { C: 400, Q: 256 },
      { bytes: true, C: 120, Q: 64 },
    ]
    for (const [setIndex, limits] of limitSets.entries()) {
      for (let run = 0; run < 25; run += 1) {
        const label = `Q=${limits.Q} C=${limits.C}${limits.bytes ? ' bytes' : ''} run ${run}`
        const random = seededRandom(1_000 * setIndex + run + 1)
        const clock = { now: NOW_MS }
        const harness = contentHarness(() => sessionAt(clock.now, {
          access: 'suggest',
          initial_write_mode: 'suggest',
          max_frame_bytes: limits.bytes ? 64 * 1024 : 2 * 1024 * 1024,
          max_queued_bytes: limits.bytes ? 64 * 1024 : 8 * 1024 * 1024,
          max_queued_frames: limits.Q,
          update_rate_count: limits.C,
        }), { clock })
        await harness.controller.start()
        const seedUpdate = seedParagraphs(harness, [[{ text: 'Seed' }]])
        const text = editorText(harness, 0)
        const countCap = Math.max(1, Math.min(limits.Q - 4, limits.C - 8, 60))
        const byteCap = (limits.bytes ? 64 * 1024 : 2 * 1024 * 1024) - 16 * 1024
        const acknowledged = new Set<string>()
        let socketSeen = harness.socketNumber()
        let acknowledgedOnSocket = 0
        let exitedOnSocket = false
        let sentAtSocketStart = harness.sentUpdates.length
        for (let step = 0; step < 30; step += 1) {
          const roll = random()
          if (roll < 0.45) {
            const count = 1 + Math.floor(random() * 4)
            for (let key = 0; key < count; key += 1) {
              if (limits.bytes && random() < 0.3) {
                text.insert(text.length, 'L'.repeat(9 * 1024))
              } else if (random() < 0.5 || text.length === 0) {
                text.insert(Math.floor(random() * (text.length + 1)), 'x')
              } else {
                text.delete(Math.floor(random() * text.length), 1)
              }
            }
          } else if (roll < 0.55) {
            exitedOnSocket = true
            harness.lifecycle.exit('hidden')
          } else if (roll < 0.65) {
            exitedOnSocket = true
            harness.lifecycle.exit('pagehide')
          } else if (roll < 0.7) {
            harness.lifecycle.show(random() < 0.5)
          } else if (roll < 0.9) {
            const open = harness.sentUpdates.filter(({ socket, update }) => (
              socket === harness.socketNumber() && !acknowledged.has(contentHash(update))
            ))
            const acks = Math.floor(random() * 3)
            for (const { update } of open.slice(0, acks)) {
              acknowledged.add(contentHash(update))
              acknowledgedOnSocket += 1
              await acknowledge(harness, update)
            }
            harness.runTimersUpTo(1_000)
          } else {
            harness.getProviderOptions()?.events.onClose(1006, 'network_lost')
            harness.advance(30_000)
            harness.runTimersUpTo(30_000)
          }
          await settleMicrotasks()
          if (harness.socketNumber() !== socketSeen) {
            socketSeen = harness.socketNumber()
            acknowledgedOnSocket = 0
            exitedOnSocket = false
            sentAtSocketStart = harness.sentUpdates.findIndex(({ socket }) => socket === socketSeen)
            if (sentAtSocketStart < 0) sentAtSocketStart = harness.sentUpdates.length
          }
          const snapshot = harness.controller.getSnapshot()
          expect(snapshot.pendingHashes.length, label).toBeLessThanOrEqual(countCap)
          const pendingBytes = new Map(harness.sentUpdates
            .filter(({ update }) => snapshot.pendingHashes.includes(contentHash(update)))
            .map(({ update }) => [contentHash(update), update.length]))
          const bytes = [...pendingBytes.values()].reduce((total, size) => total + size, 0)
          if (pendingBytes.size > 1) expect(bytes, label).toBeLessThanOrEqual(byteCap)
          // Until its first acknowledgement a socket carries one update, the
          // resent one or a new one, unless the page was left meanwhile.
          if (acknowledgedOnSocket === 0 && !exitedOnSocket) {
            const frames = harness.sentUpdates
              .slice(sentAtSocketStart)
              .filter(({ socket }) => socket === socketSeen)
            expect(frames.length, label).toBeLessThanOrEqual(1)
          }
        }
        if (harness.controller.getSnapshot().connectionStatus === 'reconnecting') {
          harness.advance(30_000)
          harness.runTimersUpTo(30_000)
          await settleMicrotasks()
        }
        await drainSends(harness, acknowledged, label)
        const replica = replicaOf([seedUpdate, ...harness.sentUpdates.map(({ update }) => update)])
        expect(replica.store.pendingStructs, label).toBeNull()
        const replicaParagraph = replica.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0) as Y.XmlElement
        expect((replicaParagraph.get(0) as Y.XmlText).toString(), label).toBe(text.toString())
        replica.destroy()
        harness.controller.discardAfterRecoveryCapture()
      }
    }
  })
})

function structIds(update: Uint8Array): string[] {
  return Y.decodeUpdate(update).structs.flatMap((struct) => (
    struct instanceof Y.Item || struct instanceof Y.GC
      ? Array.from({ length: struct.length }, (_value, offset) => (
          `${struct.id.client}:${struct.id.clock + offset}`
        ))
      : []
  ))
}

describe('collaboration sending: durability across reconnects', () => {
  it('loses and duplicates no change across random reconnects and page exits', async () => {
    for (let seed = 1; seed <= 20; seed += 1) {
      const label = `seed ${seed}`
      const random = seededRandom(seed * 7_919)
      const clock = { now: NOW_MS }
      const harness = contentHarness(() => sessionAt(clock.now), { clock })
      await harness.controller.start()
      const seedUpdate = seedParagraphs(harness, [[{ text: 'Seed' }]])
      const map = editorDocumentOf(harness).getMap('test')
      const text = editorText(harness, 0)
      // The service: its document, one stored row per novel update, and the
      // frames it received and has not processed yet.
      const server = new Y.Doc()
      Y.applyUpdate(server, seedUpdate)
      const rows = new Map<string, string[]>()
      const queue: Array<{ hash: string; socket: number; update: Uint8Array }> = []
      const acknowledgedOnSocket = new Set<string>()
      let received = harness.sentUpdates.length
      let socketSeen = harness.socketNumber()
      let exitedOnSocket = false
      let maxInFlight = 0
      const receive = () => {
        while (received < harness.sentUpdates.length) {
          const frame = harness.sentUpdates[received]!
          received += 1
          queue.push({ hash: contentHash(frame.update), socket: frame.socket, update: frame.update })
        }
      }
      const processOne = (deliver: boolean) => {
        const frame = queue.shift()
        if (!frame) return
        const contained = Y.snapshotContainsUpdate(Y.snapshot(server), frame.update)
        if (!rows.has(frame.hash) && !contained) {
          Y.applyUpdate(server, frame.update)
          // Every update reaches the service after the updates it depends on.
          expect(server.store.pendingStructs, label).toBeNull()
          expect(server.store.pendingDs, label).toBeNull()
          rows.set(frame.hash, structIds(frame.update))
        }
        if (deliver && frame.socket === harness.socketNumber() && harness.socketState.open) {
          acknowledgedOnSocket.add(frame.hash)
          harness.advance(200)
          harness.ack(frame.hash)
        }
      }
      for (let step = 0; step < 60; step += 1) {
        const roll = random()
        if (roll < 0.35) {
          const edit = random()
          if (edit < 0.4) map.set(`k${Math.floor(random() * 5)}`, `v${step}`)
          else if (edit < 0.75 || text.length === 0) {
            text.insert(Math.floor(random() * (text.length + 1)), String.fromCharCode(97 + (step % 26)))
          } else text.delete(Math.floor(random() * text.length), 1)
        } else if (roll < 0.5) {
          harness.runTimersUpTo(1_000)
        } else if (roll < 0.72) {
          receive()
          processOne(true)
        } else if (roll < 0.8) {
          // The socket dies: the service still processes what it received,
          // but no acknowledgement reaches this page.
          receive()
          while (queue.length > 0) processOne(false)
          harness.getProviderOptions()?.events.onClose(1006, 'network_lost')
          harness.advance(30_000)
          harness.runTimersUpTo(30_000)
        } else if (roll < 0.88) {
          exitedOnSocket = true
          harness.lifecycle.exit(random() < 0.5 ? 'pagehide' : 'hidden')
        } else if (roll < 0.92) {
          harness.lifecycle.show(random() < 0.5)
        } else {
          harness.advance(200)
        }
        await settleMicrotasks()
        receive()
        if (harness.socketNumber() !== socketSeen) {
          socketSeen = harness.socketNumber()
          exitedOnSocket = false
          acknowledgedOnSocket.clear()
        }
        if (!exitedOnSocket) {
          const inFlight = harness.sentUpdates.filter(({ socket, update }) => (
            socket === socketSeen && !acknowledgedOnSocket.has(contentHash(update))
          )).length
          maxInFlight = Math.max(maxInFlight, inFlight)
        }
        expect(harness.controller.getSnapshot().pendingHashes.length, label).toBeLessThanOrEqual(28)
      }
      for (let round = 0; round < 100; round += 1) {
        if (!harness.controller.getSnapshot().hasUnconfirmedLocalChanges) break
        if (harness.controller.getSnapshot().connectionStatus === 'reconnecting') {
          harness.advance(30_000)
          harness.runTimersUpTo(30_000)
        }
        harness.advance(1_000)
        harness.runTimersUpTo(1_000)
        await settleMicrotasks()
        receive()
        while (queue.length > 0) processOne(true)
        await settleMicrotasks()
      }

      expect(harness.controller.getSnapshot().hasUnconfirmedLocalChanges, label).toBe(false)
      expect(maxInFlight, label).toBeLessThanOrEqual(1)
      expect(server.getMap('test').toJSON(), label).toEqual(map.toJSON())
      const serverParagraph = server.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0) as Y.XmlElement
      expect((serverParagraph.get(0) as Y.XmlText).toString(), label).toBe(text.toString())
      const owners = new Map<string, string>()
      for (const [hash, ids] of rows) {
        for (const id of ids) {
          expect(owners.get(id), `${label}: struct ${id} stored twice`).toBeUndefined()
          owners.set(id, hash)
        }
      }
      server.destroy()
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('shows the access-change notice before sending direct changes after a downgrade from edit to suggest', async () => {
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(suggestSession())
    const harness = createHarness(requestSession, vi.fn(async (update: Uint8Array) => contentHash(update)))
    await harness.controller.start()
    const map = editorDocumentOf(harness).getMap('test')
    map.set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    map.set('b', 'b')
    map.set('c', 'c')

    await harness.controller.refreshLease()

    expect(harness.controller.getSnapshot()).toMatchObject({
      connectionStatus: 'error',
      durabilityStatus: 'error',
      hasUnconfirmedLocalChanges: true,
    })
    expect(harness.controller.getSnapshot().error).toContain(
      'access changed before local updates were durably confirmed',
    )
    await acknowledge(harness, harness.sentOnSocket(1)[0]!)
    expect(harness.sentUpdates).toHaveLength(1)
  })

  it('keeps sending suggestion changes after a downgrade from edit to suggest', async () => {
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(suggestSession())
    const harness = createHarness(requestSession, vi.fn(async (update: Uint8Array) => contentHash(update)))
    await harness.controller.start()
    seedParagraphs(harness, [[{ text: 'Hello' }]])
    harness.controller.getSnapshot().markLocalWriteMode('suggest', {})
    editorText(harness, 0).insert(5, 'a', insertionMark())
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    editorText(harness, 0).insert(6, 'b', insertionMark())

    await harness.controller.refreshLease()

    expect(harness.controller.getSnapshot()).toMatchObject({
      access: 'suggest',
      connectionStatus: 'connected',
      durabilityStatus: 'pending',
    })
    await acknowledge(harness, harness.sentOnSocket(1)[0]!)
    expect(harness.sentOnSocket(1)).toHaveLength(2)
  })
})

describe('collaboration sending: pacing within the published limits', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  async function heldKey(options: { suggest: boolean }) {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const harness = contentHarness(() => sessionAt(Date.now(), options.suggest
      ? { access: 'suggest', initial_write_mode: 'suggest' }
      : {}), { globalTimers: true })
    await harness.controller.start()
    const seedUpdate = seedParagraphs(harness, [[{ text: 'x'.repeat(400) }]])
    const editorDocument = editorDocumentOf(harness)
    const text = editorText(harness, 0)
    const acknowledged = new Set<string>()
    let delayed = false
    const acknowledgeAll = () => {
      for (const { update } of harness.sentUpdates) {
        const hash = contentHash(update)
        if (acknowledged.has(hash)) continue
        acknowledged.add(hash)
        harness.ack(hash)
      }
    }
    for (let key = 0; key < 300; key += 1) {
      if (options.suggest) {
        // A deletion mark replaces a character: it deletes and inserts, so
        // it is its own update.
        editorDocument.transact(() => {
          text.delete(key, 1)
          text.insert(key, 'd', insertionMark())
        })
      } else {
        text.insert(text.length, 'k')
      }
      await vi.advanceTimersByTimeAsync(33)
      acknowledgeAll()
      delayed ||= harness.controller.getSnapshot().durabilityDelayed
    }
    for (let tick = 0; tick < 2_000; tick += 1) {
      if (!harness.controller.getSnapshot().hasUnconfirmedLocalChanges) break
      await vi.advanceTimersByTimeAsync(33)
      acknowledgeAll()
      delayed ||= harness.controller.getSnapshot().durabilityDelayed
    }
    expect(harness.controller.getSnapshot().hasUnconfirmedLocalChanges).toBe(false)
    const times = harness.sentUpdates.map(({ time }) => time)
    return { delayed, editorDocument, harness, seedUpdate, text, times }
  }

  function maxSendsPerWindow(times: readonly number[], windowMs: number): number {
    let max = 0
    for (let start = 0; start < times.length; start += 1) {
      let end = start
      while (end < times.length && times[end]! < times[start]! + windowMs) end += 1
      max = Math.max(max, end - start)
    }
    return max
  }

  it('a held key in suggest mode never sends faster than the published update allowance allows', async () => {
    const { delayed, harness, seedUpdate, text, times } = await heldKey({ suggest: true })

    for (let index = 1; index < times.length; index += 1) {
      expect(times[index]! - times[index - 1]!).toBeGreaterThanOrEqual(105)
    }
    expect(maxSendsPerWindow(times, 10_000)).toBeLessThanOrEqual(96)
    expect(times.length).toBe(300)
    expect(delayed).toBe(true)
    expect(replicaText([seedUpdate, ...harness.sentUpdates.map(({ update }) => update)]))
      .toBe(plainText(text))
  })

  it('a held key in edit mode never sends faster than the published update allowance allows', async () => {
    const { harness, seedUpdate, text, times } = await heldKey({ suggest: false })

    expect(maxSendsPerWindow(times, 10_000)).toBeLessThanOrEqual(96)
    for (let index = 1; index < times.length; index += 1) {
      expect(times[index]! - times[index - 1]!).toBeGreaterThanOrEqual(105)
    }
    expect(replicaText([seedUpdate, ...harness.sentUpdates.map(({ update }) => update)]))
      .toBe(plainText(text))
  })

  it('applies the update limits the session publishes, down to the smallest valid values', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const paced = async (limits: SessionLimits) => {
      const harness = contentHarness(() => sessionAt(Date.now(), limits), { globalTimers: true })
      await harness.controller.start()
      const map = editorDocumentOf(harness).getMap('test')
      map.set('a', 'a')
      await vi.advanceTimersByTimeAsync(COLLABORATION_UPDATE_BATCH_MS)
      map.set('b', 'b')
      harness.ack(contentHash(harness.sentUpdates[0]!.update))
      await vi.advanceTimersByTimeAsync(0)
      return harness
    }

    // 20 per 10 s: 16 sends per window, 625 ms apart.
    const twenty = await paced({ max_queued_frames: 4, update_rate_count: 20 })
    await vi.advanceTimersByTimeAsync(624)
    expect(twenty.sentUpdates).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(twenty.sentUpdates).toHaveLength(2)
    // A frame limit of 4 leaves no room for a page-exit burst.
    editorDocumentOf(twenty).getMap('test').set('c', 'c')
    twenty.lifecycle.exit('pagehide')
    expect(twenty.sentUpdates).toHaveLength(2)
    expect(twenty.lifecycle.guard()?.()).toBe(true)

    // 1 per 10 s: one send per window, and no page-exit burst.
    const one = await paced({ update_rate_count: 1 })
    await vi.advanceTimersByTimeAsync(9_999)
    expect(one.sentUpdates).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(one.sentUpdates).toHaveLength(2)
    editorDocumentOf(one).getMap('test').set('c', 'c')
    one.lifecycle.exit('pagehide')
    expect(one.sentUpdates).toHaveLength(2)

    // 9 per 10 s: 7 sends per window, 1429 ms apart; a page exit may send one
    // update, and none once an update went out in the window.
    const nine = contentHarness(() => sessionAt(Date.now(), { update_rate_count: 9 }), {
      globalTimers: true,
    })
    await nine.controller.start()
    editorDocumentOf(nine).getMap('test').set('a', 'a')
    nine.lifecycle.exit('pagehide')
    expect(nine.sentUpdates).toHaveLength(1)
    nine.ack(contentHash(nine.sentUpdates[0]!.update))
    await vi.advanceTimersByTimeAsync(0)
    editorDocumentOf(nine).getMap('test').set('b', 'b')
    nine.lifecycle.exit('pagehide')
    expect(nine.sentUpdates).toHaveLength(1)
    expect(nine.lifecycle.guard()?.()).toBe(true)
    await vi.advanceTimersByTimeAsync(1_428)
    expect(nine.sentUpdates).toHaveLength(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(nine.sentUpdates).toHaveLength(2)
  })

  it('refuses limits outside the ranges the service accepts', async () => {
    const invalid: Array<Partial<Record<keyof SessionLimits, number>>> = [
      { update_rate_count: 0 },
      { update_rate_count: 10_001 },
      { update_rate_count: 1.5 },
      { update_rate_window_seconds: 0 },
      { update_rate_window_seconds: 61 },
      { max_queued_frames: 0 },
      { max_queued_frames: 257 },
      { max_frame_bytes: 64 * 1024 - 1 },
      { max_frame_bytes: 16 * 1024 * 1024 + 1 },
      { max_queued_bytes: 64 * 1024 - 1 },
      { max_queued_bytes: 64 * 1024 * 1024 + 1 },
    ]
    for (const limits of invalid) {
      const harness = contentHarness({ ...session('initial-token'), ...limits })
      await harness.controller.start()
      const snapshot = harness.controller.getSnapshot()
      expect(snapshot.connectionStatus, JSON.stringify(limits)).toBe('incompatible')
      // The controller's own wording; a reword must be caught here.
      expect(snapshot.error).toBe('The collaboration limits are not compatible with this client.')
      expect(localizeEditorCollaborationNotice(snapshot.error, 'de')).toBe(
        'Die Grenzwerte des Kollaborationsdienstes sind mit dieser App-Version nicht kompatibel.',
      )
    }
  })

  it('uses the service defaults with one warning when the session publishes no limits', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    try {
      const withoutLimits = () => {
        const value: Partial<ConfiguredSession> = sessionAt(Date.now())
        delete value.max_frame_bytes
        delete value.max_queued_bytes
        delete value.max_queued_frames
        delete value.update_rate_count
        delete value.update_rate_window_seconds
        return value as ConfiguredSession
      }
      const harness = contentHarness(withoutLimits, { globalTimers: true })
      await harness.controller.start()
      const map = editorDocumentOf(harness).getMap('test')
      map.set('a', 'a')
      await vi.advanceTimersByTimeAsync(COLLABORATION_UPDATE_BATCH_MS)
      map.set('b', 'b')
      harness.ack(contentHash(harness.sentUpdates[0]!.update))
      await vi.advanceTimersByTimeAsync(104)
      expect(harness.sentUpdates).toHaveLength(1)
      await vi.advanceTimersByTimeAsync(1)
      expect(harness.sentUpdates).toHaveLength(2)
      await harness.controller.refreshLease()
      expect(harness.controller.getSnapshot().connectionStatus).toBe('connected')
      expect(warn).toHaveBeenCalledOnce()
    } finally {
      warn.mockRestore()
    }
  })

  it('raises the navigation failure only after a minute without any acknowledgement', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(NOW_MS)
    const harness = contentHarness(() => sessionAt(Date.now()), { globalTimers: true })
    await harness.controller.start()
    const map = editorDocumentOf(harness).getMap('test')
    map.set('a', 'a')
    await vi.advanceTimersByTimeAsync(COLLABORATION_UPDATE_BATCH_MS)
    map.set('b', 'b')

    harness.controller.release()
    await vi.advanceTimersByTimeAsync(50_000)
    harness.ack(contentHash(harness.sentUpdates[0]!.update))
    await vi.advanceTimersByTimeAsync(0)
    expect(harness.sentUpdates).toHaveLength(2)
    await vi.advanceTimersByTimeAsync(59_000)
    expect(harness.controller.getSnapshot().blockingFailure).toBeNull()
    await vi.advanceTimersByTimeAsync(1_000)
    expect(harness.controller.getSnapshot().blockingFailure).toContain('could not be confirmed')
  })
})

describe('collaboration timing and transport', () => {
  it('uses exponential reconnect backoff with bounded jitter', () => {
    expect([
      1, 2, 3, 4, 5, 6, 7,
    ].map((attempt) => collaborationReconnectDelayMs(attempt, 0.5))).toEqual([
      1_000,
      2_000,
      4_000,
      8_000,
      15_000,
      30_000,
      30_000,
    ])
    expect(collaborationReconnectDelayMs(1, 0)).toBe(850)
    expect(collaborationReconnectDelayMs(1, 1)).toBe(1_150)
    expect(collaborationReconnectDelayMs(6, 1)).toBe(30_000)
  })

  it('refreshes halfway through the lease and keeps websocket auth same-origin', () => {
    expect(leaseRefreshDelayMs(1_060, NOW_MS)).toBe(30_000)
    expect(collaborationWebSocketUrl('collaboration', {
      host: 'desk.test:5173',
      protocol: 'http:',
    })).toBe('ws://desk.test:5173/collaboration')
  })

  it('throttles OWN awareness state and never echoes received server states', () => {
    // Hocuspocus 4.3 re-broadcasts every changed awareness client — even
    // states just received from the server. The sidecar's identity gate
    // closes such connections with 4403 (invalid_request) as soon as a
    // second participant or stale clientID appears, so the adapter must
    // filter to the local clientID.
    const document = new Y.Doc()
    const adapter = createHocuspocusProvider({
      document,
      events: {
        onAuthenticated: () => undefined,
        onAuthenticationFailed: () => undefined,
        onClose: () => undefined,
        onStateless: () => undefined,
        onSynced: () => undefined,
      },
      getToken: () => 'lease-token',
      holdsUnsentLocalUpdates: () => false,
      mayAnswerSync: () => true,
      room: 'inqtrix-editor-v1:doc-1:g1',
      url: 'ws://localhost:9/collaboration',
    })
    const forwarded: Array<{ added: number[]; removed: number[]; updated: number[] }> = []
    adapter.provider.awarenessUpdateHandler = ((changes: {
      added: number[]
      removed: number[]
      updated: number[]
    }) => {
      forwarded.push(changes)
    }) as typeof adapter.provider.awarenessUpdateHandler
    vi.useFakeTimers()
    try {
      const awareness = adapter.provider.awareness
      expect(awareness).not.toBeNull()
      // A remote participant's state arriving from the server (its update
      // event names a FOREIGN clientID) must NOT be echoed back.
      awareness!.emit('update', [
        { added: [document.clientID + 1], removed: [], updated: [] },
        'server',
      ])
      expect(forwarded).toHaveLength(0)
      // An echoed state for our own clientID is still a server message and
      // must not be sent back.
      awareness!.emit('update', [
        { added: [], removed: [], updated: [document.clientID] },
        adapter.provider,
      ])
      expect(forwarded).toHaveLength(0)
      forwarded.length = 0
      // A burst of local cursor changes is coalesced below the sidecar's
      // awareness rate limit while preserving the latest state.
      for (let cursor = 0; cursor < 25; cursor += 1) {
        awareness!.setLocalStateField('cursor', cursor)
      }
      expect(forwarded).toHaveLength(0)
      vi.advanceTimersByTime(COLLABORATION_AWARENESS_THROTTLE_MS)
      expect(forwarded).toHaveLength(1)
      expect(forwarded[0].updated).toEqual([document.clientID])
      expect(awareness!.getLocalState()?.cursor).toBe(24)
    } finally {
      adapter.destroy()
      document.destroy()
      vi.useRealTimers()
    }
  })

  it('flushes the page exit before the browser asks whether to leave, and listens for the unload only while a guard is set', () => {
    const listeners = new Map<string, Set<(event: unknown) => void>>()
    const documentListeners = new Map<string, Set<(event: unknown) => void>>()
    const eventTarget = (registry: Map<string, Set<(event: unknown) => void>>) => ({
      addEventListener: (type: string, listener: (event: unknown) => void) => {
        const set = registry.get(type) ?? new Set()
        set.add(listener)
        registry.set(type, set)
      },
      removeEventListener: (type: string, listener: (event: unknown) => void) => {
        registry.get(type)?.delete(listener)
      },
    })
    const fakeDocument = { ...eventTarget(documentListeners), visibilityState: 'visible' }
    vi.stubGlobal('window', { ...eventTarget(listeners), document: fakeDocument })
    const dispatch = (
      registry: Map<string, Set<(event: unknown) => void>>,
      type: string,
      event: unknown,
    ) => {
      for (const listener of [...(registry.get(type) ?? [])]) listener(event)
    }
    const unloadEvent = () => ({ preventDefault: vi.fn(), returnValue: 'unset' })
    try {
      const calls: string[] = []
      const registration = observeBrowserPageLifecycle({
        onExit: (reason) => calls.push(`exit:${reason}`),
        onShow: (persisted) => calls.push(`show:${persisted}`),
      })
      // Without a guard the page stays eligible for the back/forward cache.
      expect(listeners.get('beforeunload')?.size ?? 0).toBe(0)

      registration.setLeaveGuard(() => {
        calls.push('guard')
        return false
      })
      expect(listeners.get('beforeunload')?.size).toBe(1)
      const quiet = unloadEvent()
      dispatch(listeners, 'beforeunload', quiet)
      expect(calls).toEqual(['exit:beforeunload', 'guard'])
      expect(quiet.preventDefault).not.toHaveBeenCalled()

      calls.length = 0
      registration.setLeaveGuard(() => {
        calls.push('guard')
        return true
      })
      expect(listeners.get('beforeunload')?.size).toBe(1)
      const asking = unloadEvent()
      dispatch(listeners, 'beforeunload', asking)
      expect(calls).toEqual(['exit:beforeunload', 'guard'])
      expect(asking.preventDefault).toHaveBeenCalledOnce()
      expect(asking.returnValue).toBe('')

      calls.length = 0
      dispatch(listeners, 'pagehide', {})
      fakeDocument.visibilityState = 'hidden'
      dispatch(documentListeners, 'visibilitychange', {})
      dispatch(listeners, 'pageshow', { persisted: true })
      expect(calls).toEqual(['exit:pagehide', 'exit:hidden', 'show:true'])

      registration.setLeaveGuard(null)
      expect(listeners.get('beforeunload')?.size ?? 0).toBe(0)
      registration.dispose()
      expect(listeners.get('pagehide')?.size ?? 0).toBe(0)
      expect(documentListeners.get('visibilitychange')?.size ?? 0).toBe(0)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('attaches the real provider to its external websocket transport', () => {
    // Hocuspocus 4.x only auto-attaches when it manages its own socket;
    // with the adapter's external websocketProvider a missing attach()
    // means the auth token is NEVER sent and every participant stays
    // read-only. The controller tests replace this factory with fakes, so
    // this regression test drives the real provider.
    const document = new Y.Doc()
    const adapter = createHocuspocusProvider({
      document,
      events: {
        onAuthenticated: () => undefined,
        onAuthenticationFailed: () => undefined,
        onClose: () => undefined,
        onStateless: () => undefined,
        onSynced: () => undefined,
      },
      getToken: () => 'lease-token',
      holdsUnsentLocalUpdates: () => false,
      mayAnswerSync: () => true,
      room: 'inqtrix-editor-v1:doc-1:g1',
      url: 'ws://localhost:9/collaboration',
    })
    try {
      expect(adapter.provider.isAttached).toBe(true)
    } finally {
      adapter.destroy()
      document.destroy()
    }
  })

  it('honors non-default server flush and refresh timing', async () => {
    const configured = session('configured-token', {
      provider_flush_ms: 23,
      refresh_after: (NOW_MS + 17_000) / 1_000,
    })
    const harness = createHarness(vi.fn().mockResolvedValue(configured))

    await harness.controller.start()
    harness.controller.getSnapshot().document?.getMap('test').set('title', 'Timed')

    expect(harness.scheduledDelays()).toContain(17_000)
    expect(harness.scheduledDelays()).toContain(23)
    harness.runTimer(23)
    expect(harness.transportUpdates).toHaveLength(1)
  })
})

const TRANSPORT_ROOM = 'inqtrix-editor-v1:document-1:g2'

/** A browser WebSocket whose opening, server frames and closing the test
 * drives step by step. It records every frame the client sends. */
class ScriptedWebSocket {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  static instances: ScriptedWebSocket[] = []

  binaryType = 'blob'
  readonly frames: Uint8Array[] = []
  readyState = ScriptedWebSocket.CONNECTING
  readonly url: string
  private readonly listeners = new Map<string, Set<(event: unknown) => void>>()

  constructor(url: string) {
    this.url = url
    ScriptedWebSocket.instances.push(this)
  }

  addEventListener(type: string, listener: (event: unknown) => void): void {
    const listeners = this.listeners.get(type) ?? new Set()
    listeners.add(listener)
    this.listeners.set(type, listeners)
  }

  removeEventListener(type: string, listener: (event: unknown) => void): void {
    this.listeners.get(type)?.delete(listener)
  }

  send(data: Uint8Array): void {
    if (this.readyState !== ScriptedWebSocket.OPEN) {
      throw new Error('A frame was sent on a socket that is not open.')
    }
    this.frames.push(Uint8Array.from(data))
  }

  close(): void {
    if (this.readyState === ScriptedWebSocket.CLOSED) return
    this.readyState = ScriptedWebSocket.CLOSING
  }

  open(): void {
    this.readyState = ScriptedWebSocket.OPEN
    this.dispatch('open', {})
  }

  receive(frame: Uint8Array): void {
    this.dispatch('message', { data: Uint8Array.from(frame).buffer })
  }

  /** The server began closing; no close event has reached the page yet. */
  startClose(): void {
    this.readyState = ScriptedWebSocket.CLOSING
  }

  finishClose(code: number, reason: string): void {
    this.readyState = ScriptedWebSocket.CLOSED
    this.dispatch('close', { code, reason })
  }

  private dispatch(type: string, event: unknown): void {
    for (const listener of [...(this.listeners.get(type) ?? [])]) listener(event)
  }
}

function transportFrame(type: number, payload: Uint8Array): Uint8Array {
  return concatBytes(varStringBytes(TRANSPORT_ROOM), varUintBytes(type), payload)
}

function authenticatedFrame(): Uint8Array {
  return transportFrame(2, concatBytes(varUintBytes(2), varStringBytes('read-write')))
}

/** A close the server sends in band, ahead of closing the socket itself. */
function closeMessageFrame(reason: string): Uint8Array {
  return transportFrame(7, varStringBytes(reason))
}

function syncStepTwoFrame(update: Uint8Array): Uint8Array {
  return transportFrame(0, concatBytes(varUintBytes(1), varUintBytes(update.byteLength), update))
}

/** A room update the service broadcasts: a sync message of type update. */
function syncUpdateFrame(update: Uint8Array): Uint8Array {
  return transportFrame(0, concatBytes(varUintBytes(2), varUintBytes(update.byteLength), update))
}

function varUintBytes(value: number): Uint8Array {
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

function varStringBytes(value: string): Uint8Array {
  const encoded = new TextEncoder().encode(value)
  return concatBytes(varUintBytes(encoded.byteLength), encoded)
}

function concatBytes(...parts: Uint8Array[]): Uint8Array {
  const result = new Uint8Array(parts.reduce((total, part) => total + part.byteLength, 0))
  let offset = 0
  for (const part of parts) {
    result.set(part, offset)
    offset += part.byteLength
  }
  return result
}

function syncStepOneFrame(stateVector: Uint8Array): Uint8Array {
  return transportFrame(0, concatBytes(
    varUintBytes(0),
    varUintBytes(stateVector.byteLength),
    stateVector,
  ))
}

/** A durable acknowledgement as the service sends it: a stateless frame. */
function acknowledgementFrame(hash: string, sequence: number): Uint8Array {
  return transportFrame(5, varStringBytes(JSON.stringify({
    hash,
    sequence,
    type: 'durable_ack',
  })))
}

type ClientFrame =
  | { kind: 'awareness'; states: Array<{ client: number; clock: number; state: unknown }> }
  | { kind: 'syncAnswer'; update: number[] }
  | { kind: 'token'; token: string }
  | { kind: 'update'; update: number[] }
  | { kind: 'other' }

/** Decodes the frames a socket carried, in order: document updates (sync
 * message type 2), sync answers (type 1), authentication tokens and
 * awareness states. */
function decodedClientFrames(socket: ScriptedWebSocket): ClientFrame[] {
  return socket.frames.map((frame): ClientFrame => {
    let offset = 0
    const uint = (): number => {
      let value = 0
      let shift = 0
      while (offset < frame.byteLength) {
        const byte = frame[offset++]!
        value += (byte & 0x7f) * (2 ** shift)
        if ((byte & 0x80) === 0) return value
        shift += 7
      }
      throw new Error('truncated client frame')
    }
    const bytes = (): Uint8Array => {
      const length = uint()
      const value = frame.slice(offset, offset + length)
      offset += length
      return value
    }
    if (frame.byteLength <= 1) return { kind: 'other' }
    bytes()
    const type = uint()
    if (type === 0) {
      const syncType = uint()
      if (syncType === 2) return { kind: 'update', update: [...bytes()] }
      if (syncType === 1) return { kind: 'syncAnswer', update: [...bytes()] }
      return { kind: 'other' }
    }
    if (type === 2 && uint() === 0) {
      return { kind: 'token', token: new TextDecoder().decode(bytes()) }
    }
    if (type === 1) {
      const update = bytes()
      let inner = 0
      const innerUint = (): number => {
        let value = 0
        let shift = 0
        while (inner < update.byteLength) {
          const byte = update[inner++]!
          value += (byte & 0x7f) * (2 ** shift)
          if ((byte & 0x80) === 0) return value
          shift += 7
        }
        throw new Error('truncated awareness update')
      }
      const count = innerUint()
      const states: Array<{ client: number; clock: number; state: unknown }> = []
      for (let index = 0; index < count; index += 1) {
        const client = innerUint()
        const clock = innerUint()
        const length = innerUint()
        const json = new TextDecoder().decode(update.slice(inner, inner + length))
        inner += length
        states.push({ client, clock, state: JSON.parse(json) })
      }
      return { kind: 'awareness', states }
    }
    return { kind: 'other' }
  })
}

function clientFrames(socket: ScriptedWebSocket): {
  syncAnswers: number[][]
  tokens: string[]
  updates: number[][]
} {
  const frames = decodedClientFrames(socket)
  return {
    syncAnswers: frames.flatMap((frame) => (frame.kind === 'syncAnswer' ? [frame.update] : [])),
    tokens: frames.flatMap((frame) => (frame.kind === 'token' ? [frame.token] : [])),
    updates: frames.flatMap((frame) => (frame.kind === 'update' ? [frame.update] : [])),
  }
}

function createTransportHarness(
  requestSession: (
    leaseToken?: string,
    rotationCommandId?: string,
    signal?: AbortSignal,
  ) => Promise<EditorCollaborationSession>,
) {
  const timers = new Map<number, { callback: () => void; delayMs: number }>()
  const localTransportUpdates: number[][] = []
  const clock = { now: NOW_MS }
  const lifecycle = createPageLifecycleFake()
  let nextTimerId = 1
  let adapterProvider: HocuspocusProvider | null = null
  const controller = new CollaborationDocumentController(
    {
      documentId: 'document-1',
      generation: 2,
      initialPersistedSequence: 0,
      requestSession,
      resolveWebSocketUrl: (path) => `ws://desk.test${path}`,
      schemaVersion: EDITOR_SCHEMA_VERSION,
    },
    {
      cancelTimer: (timer) => {
        timers.delete(timer)
      },
      createCommandId: () => '00000000-0000-4000-8000-000000000001',
      createDocument: () => new Y.Doc(),
      createProvider: (options) => {
        options.document.on('update', (update: Uint8Array, origin: unknown) => {
          if (origin !== adapterProvider) localTransportUpdates.push([...update])
        })
        const adapter = createHocuspocusProvider(options)
        adapterProvider = adapter.provider
        return adapter
      },
      hashUpdate: async (update) => contentHash(update),
      now: () => clock.now,
      observePageLifecycle: lifecycle.observe,
      random: () => 0.5,
      scheduleTimer: (callback, delayMs) => {
        const timer = nextTimerId
        nextTimerId += 1
        timers.set(timer, { callback, delayMs })
        return timer
      },
    },
  )

  function runTimer(delayMs: number): void {
    const match = [...timers.entries()].find(([, timer]) => timer.delayMs === delayMs)
    if (!match) throw new Error(`No ${delayMs}ms timer is scheduled.`)
    timers.delete(match[0])
    match[1].callback()
  }

  function runTimersUpTo(maxDelayMs: number): void {
    for (const [timerId, timer] of [...timers.entries()]) {
      if (timer.delayMs > maxDelayMs || !timers.has(timerId)) continue
      timers.delete(timerId)
      timer.callback()
    }
  }

  /** One local edit; returns the update it put on the transport, or null
   * when it waits in the controller's log. */
  async function edit(key: string): Promise<number[] | null> {
    const count = localTransportUpdates.length
    controller.getSnapshot().document?.getMap('test').set(key, key)
    runTimersUpTo(1_000)
    await settleMicrotasks()
    if (localTransportUpdates.length > count + 1) {
      throw new Error('The edit produced more than one transport update.')
    }
    return localTransportUpdates[count] ?? null
  }

  async function acknowledge(socket: ScriptedWebSocket, update: readonly number[]): Promise<void> {
    clock.now += 1_000
    socket.receive(acknowledgementFrame(contentHash(update), 5))
    await settleMicrotasks()
    runTimersUpTo(1_000)
    await settleMicrotasks()
  }

  /** Starts the controller on its first socket; the room holds `seed`. */
  async function connectedSocket(seed?: Uint8Array): Promise<ScriptedWebSocket> {
    const starting = controller.start()
    await vi.waitFor(() => expect(ScriptedWebSocket.instances).toHaveLength(1))
    const socket = ScriptedWebSocket.instances[0]!
    socket.open()
    await settleMicrotasks()
    socket.receive(authenticatedFrame())
    socket.receive(syncStepTwoFrame(seed ?? Y.encodeStateAsUpdate(new Y.Doc())))
    await starting
    await settleMicrotasks()
    expect(controller.getSnapshot()).toMatchObject({ canEdit: true, synced: true })
    return socket
  }

  /** Runs the reconnect timer and waits for the socket it creates, unopened. */
  async function nextConnectingSocket(delayMs = 1_000): Promise<ScriptedWebSocket> {
    const count = ScriptedWebSocket.instances.length
    runTimer(delayMs)
    await vi.waitFor(() => expect(ScriptedWebSocket.instances).toHaveLength(count + 1))
    return ScriptedWebSocket.instances[count]!
  }

  /** Runs the reconnect timer and opens the socket it creates. */
  async function nextSocket(delayMs = 1_000): Promise<ScriptedWebSocket> {
    const socket = await nextConnectingSocket(delayMs)
    socket.open()
    await settleMicrotasks()
    return socket
  }

  return {
    acknowledge,
    advance: (ms: number) => {
      clock.now += ms
    },
    connectedSocket,
    controller,
    edit,
    lifecycle,
    localTransportUpdates,
    nextConnectingSocket,
    nextSocket,
    runTimer,
    runTimersUpTo,
    scheduledDelays: () => [...timers.values()].map((timer) => timer.delayMs),
    /** The transport's websocket, the object the provider's liveness check
     * runs on. */
    websocket: (): HocuspocusProviderWebsocket => (
      controller.getSnapshot().provider!.configuration.websocketProvider
    ),
  }
}

/** The provider's liveness check as it runs once thirty seconds passed
 * without any server message: two close requests that a dead path never
 * answers with a close event, then its own close call. */
function closeForSilence(websocket: HocuspocusProviderWebsocket): void {
  websocket.lastMessageReceived = 1
  for (let check = 0; check < 3; check += 1) websocket.checkConnection()
}

/** The kinds of the frames a socket carried from `from` on. */
function frameKinds(socket: ScriptedWebSocket, from = 0): string[] {
  return decodedClientFrames(socket).slice(from).map((frame) => frame.kind)
}

/** The own presence states a socket carried, in order. */
function ownPresence(
  socket: ScriptedWebSocket,
  client: number,
): Array<{ clock: number; index: number; state: unknown }> {
  return decodedClientFrames(socket).flatMap((frame, index) => (
    frame.kind === 'awareness'
      ? frame.states
        .filter((entry) => entry.client === client)
        .map((entry) => ({ clock: entry.clock, index, state: entry.state }))
      : []
  ))
}

describe('collaboration send order over the real provider transport', () => {
  afterEach(() => {
    ScriptedWebSocket.instances = []
    vi.unstubAllGlobals()
    vi.useRealTimers()
  })

  it('never sends a local update ahead of the ordered replay on a new socket: a batch assembled while the socket closes', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token')))
    try {
      const first = await harness.connectedSocket()
      const e1 = await harness.edit('e1')
      expect(clientFrames(first).updates).toEqual([e1])

      first.startClose()
      expect(await harness.edit('e2')).toBeNull()
      first.finishClose(4429, 'rate_limited')
      expect(harness.controller.getSnapshot().connectionStatus).toBe('reconnecting')

      const second = await harness.nextSocket()
      second.receive(authenticatedFrame())
      await settleMicrotasks()
      expect(clientFrames(second).updates).toEqual([e1])

      await harness.acknowledge(second, e1!)
      const updates = clientFrames(second).updates
      expect(updates).toHaveLength(2)
      expect(updates[0]).toEqual(e1)
      expect(replicaOf(updates).getMap('test').toJSON()).toEqual({ e1: 'e1', e2: 'e2' })
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('never sends a local update ahead of the ordered replay on a new socket: a batch assembled before authentication', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token')))
    try {
      const first = await harness.connectedSocket()
      const e1 = await harness.edit('e1')
      expect(clientFrames(first).updates).toEqual([e1])
      first.finishClose(1006, 'network_lost')

      const second = await harness.nextSocket()
      expect(await harness.edit('e3')).toBeNull()
      expect(clientFrames(second).updates).toEqual([])
      second.receive(authenticatedFrame())
      await settleMicrotasks()
      expect(clientFrames(second).updates).toEqual([e1])

      await harness.acknowledge(second, e1!)
      const updates = clientFrames(second).updates
      expect(updates).toHaveLength(2)
      expect(replicaOf(updates).getMap('test').toJSON()).toEqual({ e1: 'e1', e3: 'e3' })
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('answers the sync on a new socket with no change that was not sent before it', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token')))
    try {
      const first = await harness.connectedSocket()
      const e1 = await harness.edit('e1')
      await harness.edit('x')
      first.finishClose(1006, 'network_lost')

      const second = await harness.nextSocket()
      second.receive(authenticatedFrame())
      second.receive(syncStepOneFrame(Y.encodeStateVector(new Y.Doc())))
      await settleMicrotasks()

      const [answer] = clientFrames(second).syncAnswers
      expect(answer).toBeDefined()
      expect(replicaOf([answer!]).getMap('test').toJSON()).toEqual({ e1: 'e1' })
      expect(clientFrames(second).updates).toEqual([e1])
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('does not answer the room\'s sync request with updates that still wait for their resend', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token')))
    try {
      const first = await harness.connectedSocket()
      const e1 = await harness.edit('e1')
      expect(await harness.edit('x')).toBeNull()
      // A hidden page sends the waiting update at once: two unconfirmed.
      harness.lifecycle.exit('hidden')
      const [, x] = clientFrames(first).updates
      expect(clientFrames(first).updates).toEqual([e1, x])
      first.finishClose(1006, 'network_lost')

      const second = await harness.nextSocket()
      second.receive(authenticatedFrame())
      await settleMicrotasks()
      expect(clientFrames(second).updates).toEqual([e1])
      // The room asks for what this editor holds before it processed the
      // first resend. Answered now, the answer would carry `x` merged with
      // `e1`, ahead of the resend of `x`, outside the grouping rules the
      // service enforces for every update.
      second.receive(syncStepOneFrame(Y.encodeStateVector(new Y.Doc())))
      await settleMicrotasks()
      expect(clientFrames(second).syncAnswers).toEqual([])

      await harness.acknowledge(second, e1!)
      expect(clientFrames(second).updates).toEqual([e1, x])
      expect(clientFrames(second).syncAnswers).toEqual([])
      expect(debug).toHaveBeenCalledOnce()
    } finally {
      harness.controller.discardAfterRecoveryCapture()
      debug.mockRestore()
    }
  })

  it('a page exit writes its frames to the open socket before the handler returns', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const harness = createTransportHarness(vi.fn().mockResolvedValue(session('initial-token')))
    try {
      const first = await harness.connectedSocket()
      const e1 = await harness.edit('e1')
      expect(await harness.edit('x')).toBeNull()

      harness.lifecycle.exit('pagehide')

      const updates = clientFrames(first).updates
      expect(updates).toHaveLength(2)
      expect(updates[0]).toEqual(e1)
      expect(replicaOf(updates).getMap('test').toJSON()).toEqual({ e1: 'e1', x: 'x' })
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('a page that is unloaded or hidden into the back-forward cache removes its own presence at once', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    vi.useFakeTimers()
    const harness = createTransportHarness(vi.fn().mockResolvedValue(session('initial-token')))
    try {
      const first = await harness.connectedSocket()
      const awareness = harness.controller.getSnapshot().provider!.awareness!
      const own = awareness.clientID
      const ownStates = () => decodedClientFrames(first).flatMap((frame) => (
        frame.kind === 'awareness'
          ? frame.states.filter((entry) => entry.client === own).map((entry) => entry.state)
          : []
      ))
      awareness.setLocalStateField('cursor', { anchor: 1 })
      await vi.advanceTimersByTimeAsync(COLLABORATION_AWARENESS_THROTTLE_MS)
      expect(ownStates().at(-1)).toMatchObject({ cursor: { anchor: 1 } })
      await harness.edit('e1')
      await harness.edit('x')

      harness.lifecycle.exit('hidden')
      expect(ownStates().at(-1)).not.toBeNull()
      await harness.edit('y')

      harness.lifecycle.exit('pagehide')

      const frames = decodedClientFrames(first)
      const last = frames.at(-1)
      expect(last?.kind).toBe('awareness')
      expect(ownStates().at(-1)).toBeNull()
      expect(frames.at(-2)?.kind).toBe('update')
      expect(clientFrames(first).updates).toHaveLength(3)

      harness.lifecycle.show(true)
      await vi.advanceTimersByTimeAsync(COLLABORATION_AWARENESS_THROTTLE_MS)
      expect(ownStates().at(-1)).toMatchObject({ cursor: { anchor: 1 } })
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('an editor sends its own presence only once the document synchronized, not among the opening frames of its socket', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    vi.useFakeTimers()
    const harness = createTransportHarness(vi.fn().mockResolvedValue(session('initial-token')))
    try {
      const starting = harness.controller.start()
      await vi.waitFor(() => expect(ScriptedWebSocket.instances).toHaveLength(1))
      const socket = ScriptedWebSocket.instances[0]!
      socket.open()
      await settleMicrotasks()
      socket.receive(authenticatedFrame())
      await settleMicrotasks()
      const awareness = harness.controller.getSnapshot().provider!.awareness!
      const own = awareness.clientID
      expect(awareness.getLocalState()).not.toBeNull()
      expect(ownPresence(socket, own)).toEqual([])

      for (let move = 0; move < 33; move += 1) {
        awareness.setLocalStateField('cursor', { anchor: move })
        await vi.advanceTimersByTimeAsync(30)
      }
      expect(ownPresence(socket, own)).toEqual([])

      socket.receive(syncStepTwoFrame(Y.encodeStateAsUpdate(new Y.Doc())))
      await starting
      const presence = ownPresence(socket, own)
      expect(presence).toHaveLength(1)
      expect(presence[0]!.state).toMatchObject({ cursor: { anchor: 32 } })
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('does not deliver a message produced while no socket was open on the next socket', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token')))
    try {
      const first = await harness.connectedSocket()
      first.finishClose(1006, 'network_lost')
      // A token message produced while no socket is open.
      await harness.controller.getSnapshot().provider!.sendToken()

      const second = await harness.nextSocket()
      second.receive(authenticatedFrame())
      await settleMicrotasks()

      expect(clientFrames(second).tokens).toEqual(['reconnect-token'])
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('a periodic lease refresh that races a close opens the next socket with its fresh lease, and no reconnect follows', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('periodic-token'))
    const harness = createTransportHarness(requestSession)
    try {
      const first = await harness.connectedSocket()
      first.finishClose(1006, 'network_lost')
      expect(harness.scheduledDelays()).toContain(1_000)

      await harness.controller.refreshLease('periodic')

      await vi.waitFor(() => expect(ScriptedWebSocket.instances).toHaveLength(2))
      expect(harness.scheduledDelays()).not.toContain(1_000)
      const second = ScriptedWebSocket.instances[1]!
      second.open()
      await settleMicrotasks()
      expect(clientFrames(second).tokens).toEqual(['periodic-token'])
      second.receive(authenticatedFrame())
      second.receive(syncStepTwoFrame(Y.encodeStateAsUpdate(new Y.Doc())))
      await settleMicrotasks()
      expect(harness.controller.getSnapshot()).toMatchObject({
        canEdit: true,
        connectionStatus: 'connected',
        nextReconnectAt: null,
      })

      harness.runTimersUpTo(1_000)
      await settleMicrotasks()
      expect(ScriptedWebSocket.instances).toHaveLength(2)
      expect(requestSession).toHaveBeenCalledTimes(2)
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('schedules the next reconnect when a reconnect attempt closes before authentication', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token')))
    try {
      const first = await harness.connectedSocket()
      first.finishClose(4429, 'rate_limited')
      const second = await harness.nextSocket()

      second.finishClose(1006, '')
      await settleMicrotasks()

      expect(harness.controller.getSnapshot()).toMatchObject({
        connectionStatus: 'reconnecting',
        nextReconnectAt: NOW_MS + 2_000,
        reconnectAttempt: 2,
      })
      expect(harness.scheduledDelays()).toContain(2_000)
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('opens a new socket when the previous one is still closing after the reconnect delay', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token')))
    try {
      const first = await harness.connectedSocket()
      // The server refuses an update in band. The raw close that follows
      // needs longer than the reconnect delay, as on a degraded network.
      first.receive(closeMessageFrame('causal_gap'))
      expect(first.readyState).toBe(ScriptedWebSocket.CLOSING)
      expect(harness.controller.getSnapshot().connectionStatus).toBe('reconnecting')

      const second = await harness.nextSocket()
      expect(second).not.toBe(first)
      // The late close of the first socket is not the second one's, and a
      // close of the second before authentication schedules the next attempt.
      first.finishClose(4412, 'causal_gap')
      second.finishClose(1006, '')
      await settleMicrotasks()

      expect(harness.controller.getSnapshot()).toMatchObject({
        connectionStatus: 'reconnecting',
        nextReconnectAt: NOW_MS + 2_000,
        reconnectAttempt: 2,
      })
      expect(harness.scheduledDelays()).toContain(2_000)
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })
})

/** An update that writes paragraphs of plain text into an empty room. */
function paragraphsUpdate(texts: readonly string[]): Uint8Array {
  const source = new Y.Doc()
  const root = source.getXmlFragment(EDITOR_YJS_FRAGMENT)
  source.transact(() => {
    for (const value of texts) {
      const paragraph = new Y.XmlElement('paragraph')
      root.push([paragraph])
      const text = new Y.XmlText()
      paragraph.insert(0, [text])
      text.insert(0, value)
    }
  })
  const update = Y.encodeStateAsUpdate(source)
  source.destroy()
  return update
}

function controllerText(controller: CollaborationDocumentController, index: number): Y.XmlText {
  const paragraph = controller.getSnapshot().document
    ?.getXmlFragment(EDITOR_YJS_FRAGMENT)
    .get(index)
  const text = paragraph instanceof Y.XmlElement ? paragraph.get(0) : null
  if (!(text instanceof Y.XmlText)) throw new Error(`Paragraph ${index} has no text.`)
  return text
}

describe('collaboration formatting cleanups over the real provider transport', () => {
  afterEach(() => {
    ScriptedWebSocket.instances = []
    vi.unstubAllGlobals()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('sends the transport\'s cleanup of a room update behind the unconfirmed update it met, and never a cleanup the room ran itself', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const harness = createTransportHarness(vi.fn().mockResolvedValue(session('initial-token')))
    const seed = paragraphsUpdate(['Hello world'])
    const socket = await harness.connectedSocket(seed)
    controllerText(harness.controller, 0).format(6, 5, { bold: true })
    harness.runTimersUpTo(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    const [bolded] = clientFrames(socket).updates
    // The room applied another writer's deletion of the word before the
    // bold run and broadcasts it now.
    const deleted = remoteChange([seed], (text) => text.delete(6, 5))
    socket.receive(syncUpdateFrame(deleted))
    await settleMicrotasks()
    expect(clientFrames(socket).updates).toHaveLength(1)

    await harness.acknowledge(socket, bolded!)
    const [, cleanup] = clientFrames(socket).updates
    expect(cleanup).toBeDefined()
    const typed = remoteChange(
      [seed, deleted, bolded!],
      (text) => text.insert(6, 'Y', { bold: true }),
    )
    socket.receive(syncUpdateFrame(typed))
    await harness.acknowledge(socket, cleanup!)
    expect(roomDelta([seed, deleted, bolded!, typed, cleanup!]))
      .toEqual(controllerText(harness.controller, 0).toDelta())

    // With nothing unconfirmed, the transport holds the room's state: the
    // cleanup of the next room update is the room's own and is not echoed.
    const sentBefore = clientFrames(socket).updates.length
    const editorDocument = harness.controller.getSnapshot().document!
    const deletedBefore = deletedItems(editorDocument).length
    const rebolded = remoteChange(
      [seed, deleted, bolded!, typed, cleanup!],
      (text) => text.format(0, 5, { bold: true }),
    )
    const unbolded = remoteChange(
      [seed, deleted, bolded!, typed, cleanup!],
      (text) => text.delete(0, 5),
    )
    socket.receive(syncUpdateFrame(rebolded))
    socket.receive(syncUpdateFrame(unbolded))
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    // Besides the five characters, the start of the bold run the deletion
    // emptied, which the transport and the room each removed.
    expect(deletedItems(editorDocument).length - deletedBefore).toBeGreaterThan(5)
    expect(clientFrames(socket).updates).toHaveLength(sentBefore)
    expect(harness.controller.getSnapshot().hasUnconfirmedLocalChanges).toBe(false)
  })

  // The room applies every update as a transaction of its own, each with the
  // cleanup Yjs attaches to it: a deletion alone gets only the cleanup of the
  // gap it leaves. A sync answer carries all of them merged into one update,
  // and the transport's cleanup of an update that brings format items reads
  // the whole text, so it can remove a format item the room keeps.

  /** The room after one writer bolded `a` in `abcdef` and another deleted
   * `abc` on a state without the bold run. The deletion's cleanup removed
   * the start of the bold run and kept its end, which sets nothing any more.
   * `broadcasts` holds every transaction the room ran after `seed`, its own
   * cleanups included, in order. */
  function roomWithRedundantBoldEnd(seed: Uint8Array): { broadcasts: Uint8Array[]; room: Y.Doc } {
    const room = new Y.Doc()
    Y.applyUpdate(room, seed)
    const broadcasts: Uint8Array[] = []
    room.on('update', (update: Uint8Array) => {
      broadcasts.push(update)
    })
    Y.applyUpdate(room, remoteChange([seed], (text) => text.format(0, 1, { bold: true })))
    Y.applyUpdate(room, remoteChange([seed], (text) => text.delete(0, 3)))
    return { broadcasts, room }
  }

  /** Starts the controller on its first socket, whose sync answer carries
   * `answer`; unlike connectedSocket it serves every access. */
  async function openedOn(
    harness: ReturnType<typeof createTransportHarness>,
    answer: Uint8Array,
  ): Promise<ScriptedWebSocket> {
    const starting = harness.controller.start()
    await vi.waitFor(() => expect(ScriptedWebSocket.instances).toHaveLength(1))
    const socket = ScriptedWebSocket.instances[0]!
    socket.open()
    await settleMicrotasks()
    socket.receive(authenticatedFrame())
    socket.receive(syncStepTwoFrame(answer))
    await starting
    await settleMicrotasks()
    expect(harness.controller.getSnapshot()).toMatchObject({ error: null, synced: true })
    return socket
  }

  /** Brings the transport the room's changes after `seed` the way `delivery`
   * names: `live`, each transaction the room ran as its own broadcast;
   * `reconnect`, a new socket's sync answer with every change the transport
   * lacks; `open`, the first socket's sync answer with the whole room.
   * Returns the socket that carried them. */
  async function deliverRoom(
    harness: ReturnType<typeof createTransportHarness>,
    delivery: 'live' | 'open' | 'reconnect',
    seed: Uint8Array,
    room: Y.Doc,
    broadcasts: readonly Uint8Array[],
  ): Promise<ScriptedWebSocket> {
    if (delivery === 'open') return openedOn(harness, Y.encodeStateAsUpdate(room))
    const socket = await openedOn(harness, seed)
    if (delivery === 'live') {
      for (const update of broadcasts) socket.receive(syncUpdateFrame(update))
      await settleMicrotasks()
      return socket
    }
    const transport = harness.controller.getSnapshot().provider!.document
    socket.finishClose(1006, 'network_lost')
    harness.runTimersUpTo(0)
    await settleMicrotasks()
    const next = await harness.nextSocket()
    next.receive(authenticatedFrame())
    await settleMicrotasks()
    next.receive(syncStepTwoFrame(Y.encodeStateAsUpdate(room, Y.encodeStateVector(transport))))
    await settleMicrotasks()
    return next
  }

  it.each([
    { access: 'edit', delivery: 'live', sends: 'nothing', sent: 0 },
    { access: 'edit', delivery: 'reconnect', sends: 'its cleanup', sent: 1 },
    { access: 'suggest', delivery: 'reconnect', sends: 'its cleanup', sent: 1 },
    // Yjs cleans up only texts the transport held before: a first sync
    // answer brings the text itself, and its format items stay as the room
    // holds them.
    { access: 'edit', delivery: 'open', sends: 'nothing', sent: 0 },
  ] as const)('reads a bold letter typed next to a format item the room left redundant the way the room does: $access access, room changes by $delivery, the transport sends $sends', async ({ access, delivery, sent }) => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    const harness = createTransportHarness(vi.fn().mockResolvedValue(
      access === 'suggest' ? suggestSession() : session('initial-token'),
    ))
    const seed = paragraphsUpdate(['abcdef'])
    const { broadcasts, room } = roomWithRedundantBoldEnd(seed)
    const socket = await deliverRoom(harness, delivery, seed, room, broadcasts)
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()

    // The room accepts what the socket carried, in order.
    const carried = clientFrames(socket).updates
    for (const update of carried) {
      expect(roomRefusesAsCausalGap(room, Uint8Array.from(update))).toBe(false)
      Y.applyUpdate(room, Uint8Array.from(update))
      await harness.acknowledge(socket, update)
    }
    // A third writer types a bold letter at the start, on the room's state.
    const typed = remoteChange([Y.encodeStateAsUpdate(room)], (text) => text.insert(0, 'Q', { bold: true }))
    Y.applyUpdate(room, typed)
    socket.receive(syncUpdateFrame(typed))
    await settleMicrotasks()
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()

    expect(firstText(room).toDelta()).toEqual([
      { attributes: { bold: true }, insert: 'Q' },
      { insert: 'def' },
    ])
    expect(controllerText(harness.controller, 0).toDelta()).toEqual(firstText(room).toDelta())
    // Only the cleanup the room did not run itself travels.
    expect(carried).toHaveLength(sent)
    expect(clientFrames(socket).updates).toHaveLength(sent)
    expect(harness.controller.getSnapshot()).toMatchObject({
      error: null,
      hasUnconfirmedLocalChanges: false,
    })
    room.destroy()
  })

  it.each(['comment', 'view'] as const)('never sends the cleanup of a sync answer under %s access, whose lease the service refuses every document update', async (access) => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    const harness = createTransportHarness(vi.fn().mockResolvedValue({
      ...session('initial-token'),
      access,
      initial_write_mode: access,
    }))
    const seed = paragraphsUpdate(['abcdef'])
    const { broadcasts, room } = roomWithRedundantBoldEnd(seed)
    const socket = await deliverRoom(harness, 'reconnect', seed, room, broadcasts)
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()

    expect(clientFrames(socket).updates).toEqual([])
    expect(harness.controller.getSnapshot()).toMatchObject({
      error: null,
      hasUnconfirmedLocalChanges: false,
    })
    // The reader's transport did run a cleanup of its own: the proof that
    // there was something to send.
    expect(debug).toHaveBeenCalledWith('A formatting cleanup of a remote collaboration update was not sent.')
    room.destroy()
  })

  // A reader keeps that cleanup, so its transport holds a deletion the room
  // lacks. The answer to a sync request carries every deletion of the
  // transport, and the service closes a view connection whose frame holds
  // anything the room lacks: every later socket of the reader would end so.
  it.each(['comment', 'view'] as const)('never answers the sync request of a later socket under %s access with a deletion the room lacks', async (access) => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    const harness = createTransportHarness(vi.fn().mockResolvedValue({
      ...session('initial-token'),
      access,
      initial_write_mode: access,
    }))
    const seed = paragraphsUpdate(['abcdef'])
    const { broadcasts, room } = roomWithRedundantBoldEnd(seed)
    const second = await deliverRoom(harness, 'reconnect', seed, room, broadcasts)
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    // What an answer would carry now: the service refuses it.
    const transport = harness.controller.getSnapshot().provider!.document
    expect(roomRefusesFromReader(
      room,
      Y.encodeStateAsUpdate(transport, Y.encodeStateVector(room)),
    )).toBe(true)

    second.finishClose(1006, 'network_lost')
    harness.runTimersUpTo(0)
    await settleMicrotasks()
    const third = await harness.nextSocket(collaborationReconnectDelayMs(
      harness.controller.getSnapshot().reconnectAttempt,
      0.5,
    ))
    third.receive(authenticatedFrame())
    await settleMicrotasks()
    third.receive(syncStepOneFrame(Y.encodeStateVector(room)))
    await settleMicrotasks()

    const refused = clientFrames(third).syncAnswers
      .filter((answer) => roomRefusesFromReader(room, Uint8Array.from(answer)))
    expect(refused).toEqual([])
    expect(clientFrames(third).updates).toEqual([])
    room.destroy()
  })
})

/** Whether the service refuses `update` from a view connection, decided the
 * way it screens every frame of one: a struct or a deletion the room does
 * not hold closes the connection. */
function roomRefusesFromReader(room: Y.Doc, update: Uint8Array): boolean {
  return !Y.snapshotContainsUpdate(Y.snapshot(room), update)
}

/** Whether the service refuses `update` as a causal gap, decided the way its
 * document coordinator decides it: the update is applied to a copy of the
 * causally complete room, and anything Yjs has to park as pending depends on
 * structs or deletions the room does not hold. */
function roomRefusesAsCausalGap(room: Y.Doc, update: Uint8Array): boolean {
  const copy = new Y.Doc()
  try {
    Y.applyUpdate(copy, Y.encodeStateAsUpdate(room))
    Y.applyUpdate(copy, update)
    return copy.store.pendingStructs !== null || copy.store.pendingDs !== null
  } finally {
    copy.destroy()
  }
}

/** Every clock of `client` the updates carry, once per update that carries
 * it, in ascending order. */
function clocksOf(updates: ReadonlyArray<readonly number[]>, client: number): number[] {
  return updates
    .flatMap((update) => structIds(Uint8Array.from(update)))
    .filter((id) => id.startsWith(`${client}:`))
    .map((id) => Number(id.slice(id.indexOf(':') + 1)))
    .sort((left, right) => left - right)
}

// Yjs emits a transaction that starts inside the cleanup of another one only
// after that cleanup. A batch applied to the transport document while the
// transport is still applying a room update therefore reaches the transport
// without reaching the socket, and the next update depends on changes the
// room never received.
describe('collaboration sending while a room update is applied', () => {
  afterEach(() => {
    ScriptedWebSocket.instances = []
    vi.unstubAllGlobals()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it.each([
    {
      change: 'a block deletion',
      limits: {},
      write: (document: Y.Doc) => {
        document.getXmlFragment(EDITOR_YJS_FRAGMENT).delete(2, 1)
      },
    },
    {
      change: 'a paste beyond the merge cap',
      // The smallest frame limit the service publishes: a merge stays within
      // 8 KiB.
      limits: { max_frame_bytes: 64 * 1024 },
      write: (document: Y.Doc) => {
        firstText(document).insert(0, 'P'.repeat(9_000))
      },
    },
  ])('sends every change of this editor exactly once and in order when $change seals the waiting batch while a room update is applied', async ({ limits, write }) => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const harness = createTransportHarness(vi.fn().mockResolvedValue({
      ...session('initial-token'),
      ...limits,
    }))
    const seed = paragraphsUpdate(['Zone of another writer', 'Own zone', 'Spare paragraph'])
    const socket = await harness.connectedSocket(seed)
    const editor = harness.controller.getSnapshot().document!
    // The service's room, and every update it accepted from this editor.
    const room = new Y.Doc()
    Y.applyUpdate(room, seed)
    const accepted: number[][] = []
    const deliverSent = async (): Promise<void> => {
      for (;;) {
        const next = clientFrames(socket).updates[accepted.length]
        if (!next) return
        const update = Uint8Array.from(next)
        expect(
          roomRefusesAsCausalGap(room, update),
          `update ${accepted.length + 1} is refused as a causal gap`,
        ).toBe(false)
        Y.applyUpdate(room, update)
        accepted.push(next)
        await harness.acknowledge(socket, next)
      }
    }
    // A confirmed change first: afterwards no update waits for its
    // acknowledgement, so a sealed batch goes out at once.
    controllerText(harness.controller, 1).insert(8, 'x')
    harness.runTimersUpTo(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    await deliverSent()
    expect(accepted).toHaveLength(1)

    // Typed and still waiting for the idle delay.
    controllerText(harness.controller, 1).insert(9, 'bc')
    // While the editor applies the room update, it makes a change of its
    // own that seals the waiting batch, the way an input the editor reads
    // while it renders the remote change does.
    let written = false
    firstText(editor).observe(() => {
      if (written) return
      written = true
      write(editor)
    })
    const remote = remoteChange([Y.encodeStateAsUpdate(room)], (text) => text.insert(0, 'R'))
    Y.applyUpdate(room, remote)
    socket.receive(syncUpdateFrame(remote))
    await settleMicrotasks()
    expect(written).toBe(true)

    // The next keystroke, and everything sent until nothing is unconfirmed.
    const own = controllerText(harness.controller, 1)
    own.insert(own.length, 'i')
    for (let round = 0; round < 20; round += 1) {
      harness.runTimersUpTo(10_000)
      await settleMicrotasks()
      await deliverSent()
      if (!harness.controller.getSnapshot().hasUnconfirmedLocalChanges) break
    }

    const ownClocks = clocksOf(accepted, editor.clientID)
    expect(ownClocks).toEqual(Array.from(
      { length: Y.getState(editor.store, editor.clientID) },
      (_value, clock) => clock,
    ))
    expect(Y.encodeStateVector(room)).toEqual(Y.encodeStateVector(editor))
    expect(room.getXmlFragment(EDITOR_YJS_FRAGMENT).toString())
      .toBe(editor.getXmlFragment(EDITOR_YJS_FRAGMENT).toString())
    expect(harness.controller.getSnapshot()).toMatchObject({
      durabilityStatus: 'saved',
      error: null,
      hasUnconfirmedLocalChanges: false,
    })
    room.destroy()
  })

  it.each([
    {
      batch: 'an insertion',
      during: 'a room update',
      expected: 'Own zonebci',
      type: (text: Y.XmlText) => text.insert(8, 'bc'),
    },
    {
      batch: 'a deletion',
      during: 'a room update',
      expected: ' zonei',
      type: (text: Y.XmlText) => text.delete(0, 3),
    },
    {
      batch: 'a deletion',
      during: 'a running transport transaction',
      expected: ' zonei',
      type: (text: Y.XmlText) => text.delete(0, 3),
    },
  ])('never drops $batch the transport took in without emitting it during $during: the editor stops sending, warns and keeps its state', async ({ during, expected, type }) => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const harness = createTransportHarness(vi.fn().mockResolvedValue(session('initial-token')))
    const seed = paragraphsUpdate(['Zone of another writer', 'Own zone'])
    const socket = await harness.connectedSocket(seed)
    const provider = harness.controller.getSnapshot().provider!
    const own = controllerText(harness.controller, 1)
    // Waiting for the idle delay.
    type(own)
    // The page exit is the one send path outside the pump. It applies the
    // waiting batch while the transport is still applying something else.
    if (during === 'a room update') {
      provider.document.on('update', (_update: Uint8Array, origin: unknown) => {
        if (origin === provider) harness.lifecycle.exit('hidden')
      })
      socket.receive(syncUpdateFrame(remoteChange([seed], (text) => text.insert(0, 'R'))))
    } else {
      provider.document.transact(() => harness.lifecycle.exit('hidden'))
    }
    await settleMicrotasks()
    own.insert(own.length, 'i')
    harness.runTimersUpTo(10_000)
    await settleMicrotasks()

    // No update left after the batch: it would miss what the batch carried.
    expect(clientFrames(socket).updates).toEqual([])
    expect(harness.controller.getSnapshot()).toMatchObject({
      durabilityStatus: 'error',
      error: COLLABORATION_NOTICES.causal_gap_unrecoverable,
      updateRejected: true,
    })
    expect(warn).toHaveBeenCalledWith(
      'A local collaboration batch changed the transport document without '
      + 'being emitted for sending; this editor stops sending and keeps its '
      + 'local state as a backup.',
    )
    expect(plainText(own)).toBe(expected)
  })
})

describe('collaboration connection lifecycle over the real provider transport', () => {
  afterEach(() => {
    ScriptedWebSocket.instances = []
    vi.unstubAllGlobals()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('a socket the provider closes for silence goes through the controller\'s reconnect and replay', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    vi.useFakeTimers()
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token')))
    try {
      const first = await harness.connectedSocket()
      const e1 = await harness.edit('e1')
      expect(clientFrames(first).updates).toEqual([e1])

      closeForSilence(harness.websocket())

      // The controller's own wording; a reword must be caught here.
      expect(harness.controller.getSnapshot()).toMatchObject({
        canEdit: false,
        connectionStatus: 'reconnecting',
        error: 'The collaboration connection was interrupted; reconnecting read-only.',
        nextReconnectAt: NOW_MS + 1_000,
      })
      const second = await harness.nextSocket()
      expect(clientFrames(second).tokens).toEqual(['reconnect-token'])
      second.receive(authenticatedFrame())
      await settleMicrotasks()
      expect(clientFrames(second).updates).toEqual([e1])
      second.receive(syncStepTwoFrame(Y.encodeStateAsUpdate(new Y.Doc())))
      await settleMicrotasks()
      expect(harness.controller.getSnapshot()).toMatchObject({
        canEdit: true,
        connectionStatus: 'connected',
        synced: true,
      })

      await harness.acknowledge(second, e1!)
      const later = await harness.edit('later')
      expect(clientFrames(second).updates).toEqual([e1, later])
      await harness.acknowledge(second, later!)
      expect(harness.controller.getSnapshot()).toMatchObject({
        durabilityStatus: 'saved',
        hasUnconfirmedLocalChanges: false,
      })
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('the provider never opens a socket on its own after it closed one for silence', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    vi.useFakeTimers()
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token')))
    try {
      await harness.connectedSocket()
      closeForSilence(harness.websocket())

      await vi.advanceTimersByTimeAsync(5_000)

      expect(ScriptedWebSocket.instances).toHaveLength(1)
      await harness.nextSocket()
      expect(ScriptedWebSocket.instances).toHaveLength(2)
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('the provider transport reports each end of a socket exactly once, however it ended', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const closes: Array<[number, string]> = []
    let reconnectOnClose = false
    const document = new Y.Doc()
    const adapter = createHocuspocusProvider({
      document,
      events: {
        onAuthenticated: () => undefined,
        onAuthenticationFailed: () => undefined,
        onClose: (code, reason) => {
          closes.push([code, reason])
          if (!reconnectOnClose) return
          reconnectOnClose = false
          void adapter.connect()
        },
        onStateless: () => undefined,
        onSynced: () => undefined,
      },
      getToken: () => 'lease-token',
      holdsUnsentLocalUpdates: () => false,
      mayAnswerSync: () => true,
      room: TRANSPORT_ROOM,
      url: 'ws://desk.test/collaboration',
    })
    try {
      // A close the service sends in band, then the raw close of the same
      // socket: one end.
      void adapter.connect()
      const first = ScriptedWebSocket.instances[0]!
      first.open()
      first.receive(authenticatedFrame())
      first.receive(closeMessageFrame('service_unavailable'))
      first.finishClose(1000, 'service_unavailable')
      expect(closes).toEqual([[1000, 'service_unavailable']])
      expect(adapter.hasSocket()).toBe(false)

      // The liveness check's own close, then a late close event of the socket
      // it released: one end.
      void adapter.connect()
      const second = ScriptedWebSocket.instances[1]!
      expect(adapter.socketId()).not.toBe(1)
      second.open()
      second.receive(authenticatedFrame())
      closeForSilence(adapter.provider.configuration.websocketProvider)
      second.finishClose(1006, '')
      expect(closes).toEqual([[1000, 'service_unavailable'], [4408, 'forced']])

      // A close the controller asked for is no end to report.
      void adapter.connect()
      ScriptedWebSocket.instances[2]!.open()
      adapter.disconnect()
      expect(closes).toHaveLength(2)
      expect(ScriptedWebSocket.instances).toHaveLength(3)

      // A socket opened while an end is handled is not taken for the one
      // that ended.
      void adapter.connect()
      const fourth = ScriptedWebSocket.instances[3]!
      fourth.open()
      fourth.receive(authenticatedFrame())
      reconnectOnClose = true
      fourth.finishClose(1006, 'network_lost')
      expect(ScriptedWebSocket.instances).toHaveLength(5)
      expect(closes).toEqual([
        [1000, 'service_unavailable'],
        [4408, 'forced'],
        [1006, 'network_lost'],
      ])
      expect(adapter.hasSocket()).toBe(true)
    } finally {
      adapter.destroy()
      document.destroy()
    }
  })

  it('a socket that authenticates without a reported close still replays first, and no sync answer overtakes the replay', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    const harness = createTransportHarness(vi.fn().mockResolvedValue(session('initial-token')))
    try {
      const first = await harness.connectedSocket()
      const e1 = await harness.edit('e1')
      expect(await harness.edit('x')).toBeNull()
      harness.lifecycle.exit('hidden')
      const [, x] = clientFrames(first).updates
      expect(clientFrames(first).updates).toEqual([e1, x])
      // A socket end that bypasses the transport's close path, and a new
      // socket: nothing reports the end to the controller.
      const websocket = harness.websocket()
      websocket.shouldConnect = false
      HocuspocusProviderWebsocket.prototype.onClose.call(websocket, {
        event: { code: 4408, reason: 'forced' },
      })
      void websocket.connect()
      await vi.waitFor(() => expect(ScriptedWebSocket.instances).toHaveLength(2))
      const second = ScriptedWebSocket.instances[1]!
      second.open()
      await settleMicrotasks()

      second.receive(authenticatedFrame())
      await settleMicrotasks()
      expect(clientFrames(second).updates).toEqual([e1])
      second.receive(syncStepOneFrame(Y.encodeStateVector(new Y.Doc())))
      await settleMicrotasks()
      expect(clientFrames(second).syncAnswers).toEqual([])

      await harness.acknowledge(second, e1!)
      expect(clientFrames(second).updates).toEqual([e1, x])
      expect(warn.mock.calls.flat().join(' ')).toContain('replaced without a reported close')
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('a socket that opens and never answers is given up after 35 seconds and replaced', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token'))
      .mockResolvedValueOnce(session('third-token')))
    try {
      const first = await harness.connectedSocket()
      first.finishClose(1006, 'network_lost')
      const second = await harness.nextSocket()

      harness.runTimer(35_000)

      expect(second.readyState).toBe(ScriptedWebSocket.CLOSING)
      expect(harness.controller.getSnapshot()).toMatchObject({
        connectionStatus: 'reconnecting',
        error: 'The collaboration connection was interrupted; reconnecting read-only.',
        nextReconnectAt: NOW_MS + 2_000,
        reconnectAttempt: 2,
      })
      const third = await harness.nextSocket(2_000)
      expect(clientFrames(third).tokens).toEqual(['third-token'])
      third.receive(authenticatedFrame())
      third.receive(syncStepTwoFrame(Y.encodeStateAsUpdate(new Y.Doc())))
      await settleMicrotasks()
      expect(harness.controller.getSnapshot()).toMatchObject({
        canEdit: true,
        connectionStatus: 'connected',
      })
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('a socket that never opens is given up after 35 seconds', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token')))
    try {
      const first = await harness.connectedSocket()
      first.finishClose(1006, 'network_lost')
      const second = await harness.nextConnectingSocket()
      expect(second.readyState).toBe(ScriptedWebSocket.CONNECTING)

      harness.runTimer(35_000)

      expect(second.readyState).toBe(ScriptedWebSocket.CLOSING)
      expect(harness.controller.getSnapshot()).toMatchObject({
        connectionStatus: 'reconnecting',
        nextReconnectAt: NOW_MS + 2_000,
        reconnectAttempt: 2,
      })
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('an answered connection keeps no connect deadline', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token')))
    try {
      const first = await harness.connectedSocket()
      expect(harness.scheduledDelays()).not.toContain(35_000)
      first.finishClose(1006, 'network_lost')
      const second = await harness.nextSocket()
      expect(harness.scheduledDelays()).toContain(35_000)

      second.receive(authenticatedFrame())
      await settleMicrotasks()

      expect(harness.scheduledDelays()).not.toContain(35_000)
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('a retry while a new socket stays silent opens another socket at once', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token'))
      .mockResolvedValueOnce(session('retry-token'))
    const harness = createTransportHarness(requestSession)
    try {
      const first = await harness.connectedSocket()
      first.finishClose(1006, 'network_lost')
      const second = await harness.nextSocket()
      harness.advance(10_000)

      await harness.controller.getSnapshot().retryConnection()

      await vi.waitFor(() => expect(ScriptedWebSocket.instances).toHaveLength(3))
      expect(second.readyState).toBe(ScriptedWebSocket.CLOSING)
      expect(requestSession).toHaveBeenCalledTimes(3)
      const third = ScriptedWebSocket.instances[2]!
      third.open()
      await settleMicrotasks()
      expect(clientFrames(third).tokens).toEqual(['retry-token'])
      third.receive(authenticatedFrame())
      third.receive(syncStepTwoFrame(Y.encodeStateAsUpdate(new Y.Doc())))
      await settleMicrotasks()
      expect(harness.controller.getSnapshot()).toMatchObject({
        canEdit: true,
        connectionStatus: 'connected',
      })
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('does not answer the room\'s sync request while suggestion changes still wait for their resend', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    const harness = createTransportHarness(vi.fn().mockResolvedValue(suggestSession()))
    try {
      const seed = paragraphsUpdate(['Hello'])
      const first = await harness.connectedSocket(seed)
      const text = controllerText(harness.controller, 0)
      text.insert(5, 'ab', insertionMark(1))
      harness.runTimersUpTo(1_000)
      await settleMicrotasks()
      // Deleting part of the own suggestion and inserting again travel apart;
      // the hidden page sends both at once, next to the one in flight.
      text.delete(6, 1)
      text.insert(6, 'c', insertionMark(2))
      await settleMicrotasks()
      harness.lifecycle.exit('hidden')
      const sent = clientFrames(first).updates
      expect(sent).toHaveLength(3)
      for (const update of sent) {
        expect(() => validateSuggestionYjsUpdate(Uint8Array.from(update))).not.toThrow()
      }
      const [insertion, deletion, reinsertion] = sent
      first.finishClose(1006, 'network_lost')

      const second = await harness.nextSocket()
      second.receive(authenticatedFrame())
      await settleMicrotasks()
      expect(clientFrames(second).updates).toEqual([insertion])
      // The room asks before it processed the first resend. Answered now,
      // the answer would carry the other two merged, which the suggestion
      // rules refuse.
      const roomStateVector = Y.encodeStateVector(replicaOf([seed]))
      const transport = harness.controller.getSnapshot().provider!.document
      expect(() => validateSuggestionYjsUpdate(
        Y.encodeStateAsUpdate(transport, roomStateVector),
      )).toThrow()
      second.receive(syncStepOneFrame(roomStateVector))
      await settleMicrotasks()
      expect(clientFrames(second).syncAnswers).toEqual([])

      await harness.acknowledge(second, insertion!)
      expect(clientFrames(second).updates).toEqual([insertion, deletion])
      await harness.acknowledge(second, deletion!)
      expect(clientFrames(second).updates).toEqual([insertion, deletion, reinsertion])
      await harness.acknowledge(second, reinsertion!)
      expect(clientFrames(second).syncAnswers).toEqual([])
      expect(harness.controller.getSnapshot()).toMatchObject({
        durabilityStatus: 'saved',
        hasUnconfirmedLocalChanges: false,
      })
      expect(replicaText([seed, insertion!, deletion!, reinsertion!])).toBe('Helloac')
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('a reconnected editor announces its presence with a new clock once synchronized', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    vi.useFakeTimers()
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token')))
    try {
      const first = await harness.connectedSocket()
      const awareness = harness.controller.getSnapshot().provider!.awareness!
      const own = awareness.clientID
      awareness.setLocalStateField('cursor', { anchor: 1 })
      await vi.advanceTimersByTimeAsync(COLLABORATION_AWARENESS_THROTTLE_MS)
      const lastClock = ownPresence(first, own).at(-1)!.clock
      first.finishClose(1006, 'network_lost')

      const second = await harness.nextSocket()
      second.receive(authenticatedFrame())
      await settleMicrotasks()
      expect(ownPresence(second, own)).toEqual([])
      second.receive(syncStepTwoFrame(Y.encodeStateAsUpdate(new Y.Doc())))
      await settleMicrotasks()

      const announced = ownPresence(second, own)
      expect(announced).toHaveLength(1)
      expect(announced[0]!.clock).toBeGreaterThan(lastClock)
      expect(announced[0]!.state).toMatchObject({ cursor: { anchor: 1 } })
      await vi.advanceTimersByTimeAsync(4 * COLLABORATION_AWARENESS_THROTTLE_MS)
      expect(ownPresence(second, own)).toHaveLength(1)
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('own presence never leaves before the local change it follows, and travels directly behind it', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    vi.useFakeTimers()
    const harness = createTransportHarness(vi.fn().mockResolvedValue(session('initial-token')))
    try {
      const first = await harness.connectedSocket()
      const awareness = harness.controller.getSnapshot().provider!.awareness!
      const own = awareness.clientID
      const e0 = await harness.edit('e0')
      expect(await harness.edit('e1')).toBeNull()
      const mark = first.frames.length

      awareness.setLocalStateField('cursor', { anchor: 2 })
      await vi.advanceTimersByTimeAsync(3 * COLLABORATION_AWARENESS_THROTTLE_MS)
      expect(frameKinds(first, mark)).toEqual([])

      await harness.acknowledge(first, e0!)
      expect(frameKinds(first, mark)).toEqual(['update', 'awareness'])
      expect(ownPresence(first, own).at(-1)!.state).toMatchObject({ cursor: { anchor: 2 } })
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('a removal of the own presence is never held back by unsent local changes', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    vi.useFakeTimers()
    const harness = createTransportHarness(vi.fn().mockResolvedValue(session('initial-token')))
    try {
      const first = await harness.connectedSocket()
      const awareness = harness.controller.getSnapshot().provider!.awareness!
      await harness.edit('e0')
      expect(await harness.edit('e1')).toBeNull()

      awareness.setLocalState(null)
      await vi.advanceTimersByTimeAsync(COLLABORATION_AWARENESS_THROTTLE_MS)

      expect(ownPresence(first, awareness.clientID).at(-1)?.state).toBeNull()
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('a socket that replaced another without a reported close does not inherit its stability watch', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token')))
    try {
      const first = await harness.connectedSocket()
      first.finishClose(1006, 'network_lost')
      const second = await harness.nextSocket()
      second.receive(authenticatedFrame())
      second.receive(syncStepTwoFrame(Y.encodeStateAsUpdate(new Y.Doc())))
      await settleMicrotasks()
      expect(harness.controller.getSnapshot().reconnectAttempt).toBe(1)
      expect(harness.scheduledDelays()).toContain(10_000)
      const websocket = harness.websocket()
      websocket.shouldConnect = false
      HocuspocusProviderWebsocket.prototype.onClose.call(websocket, {
        event: { code: 4408, reason: 'forced' },
      })
      void websocket.connect()
      await vi.waitFor(() => expect(ScriptedWebSocket.instances).toHaveLength(3))
      const third = ScriptedWebSocket.instances[2]!
      third.open()
      await settleMicrotasks()
      third.receive(authenticatedFrame())
      await settleMicrotasks()

      harness.runTimer(10_000)

      expect(harness.controller.getSnapshot().reconnectAttempt).toBe(1)
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('the own presence leaves at once when the page is unloaded, even while local changes remain unsent', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    vi.useFakeTimers()
    const harness = createTransportHarness(
      vi.fn().mockResolvedValue(suggestSession({ max_queued_frames: 8 })),
    )
    try {
      const first = await harness.connectedSocket(paragraphsUpdate(['Hello world']))
      const own = harness.controller.getSnapshot().provider!.awareness!.clientID
      const text = controllerText(harness.controller, 0)
      text.insert(11, 'a')
      harness.runTimersUpTo(1_000)
      await settleMicrotasks()
      typeAlternating(text, 6, [])
      await settleMicrotasks()

      harness.lifecycle.exit('pagehide')

      // Four updates wait at most (Q - 4): three of the six changes remain.
      expect(clientFrames(first).updates).toHaveLength(4)
      expect(harness.controller.getSnapshot().hasUnconfirmedLocalChanges).toBe(true)
      const frames = decodedClientFrames(first)
      expect(frames.at(-1)?.kind).toBe('awareness')
      expect(ownPresence(first, own).at(-1)?.state).toBeNull()
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })

  it('a reconnected editor sends no presence ahead of the replay and sends it behind the last resent change', async () => {
    vi.stubGlobal('WebSocket', ScriptedWebSocket)
    vi.useFakeTimers()
    const harness = createTransportHarness(vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockResolvedValueOnce(session('reconnect-token')))
    try {
      const first = await harness.connectedSocket()
      const awareness = harness.controller.getSnapshot().provider!.awareness!
      const own = awareness.clientID
      const e1 = await harness.edit('e1')
      // Two more changes, each sent at once by a hidden page.
      expect(await harness.edit('x')).toBeNull()
      harness.lifecycle.exit('hidden')
      expect(await harness.edit('y')).toBeNull()
      harness.lifecycle.exit('hidden')
      const [, x, y] = clientFrames(first).updates
      expect(clientFrames(first).updates).toEqual([e1, x, y])
      awareness.setLocalStateField('cursor', { anchor: 3 })
      await vi.advanceTimersByTimeAsync(COLLABORATION_AWARENESS_THROTTLE_MS)
      first.finishClose(1006, 'network_lost')

      const second = await harness.nextSocket()
      second.receive(authenticatedFrame())
      second.receive(syncStepTwoFrame(Y.encodeStateAsUpdate(new Y.Doc())))
      await settleMicrotasks()
      await vi.advanceTimersByTimeAsync(3 * COLLABORATION_AWARENESS_THROTTLE_MS)
      expect(clientFrames(second).updates).toEqual([e1])
      expect(ownPresence(second, own)).toEqual([])

      await harness.acknowledge(second, e1!)
      expect(ownPresence(second, own)).toEqual([])
      await harness.acknowledge(second, x!)
      expect(clientFrames(second).updates).toEqual([e1, x, y])
      const presence = ownPresence(second, own)
      expect(presence).toHaveLength(1)
      expect(presence[0]!.index).toBe(frameKinds(second).lastIndexOf('update') + 1)
      expect(presence[0]!.state).toMatchObject({ cursor: { anchor: 3 } })
    } finally {
      harness.controller.discardAfterRecoveryCapture()
    }
  })
})

describe('collaboration connection lifecycle', () => {
  it('a reconnect that joins a periodic lease refresh connects when that refresh ends', async () => {
    let resolvePeriodic!: (value: EditorCollaborationSession) => void
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolvePeriodic = resolve
      }))
    const harness = createHarness(requestSession, undefined, { acknowledgeTokenSync: false })
    await harness.controller.start()
    const periodic = harness.controller.refreshLease('periodic')
    harness.getProviderOptions()?.events.onClose(1006, 'network_lost')
    harness.runTimer(1_000)

    resolvePeriodic(session('periodic-token'))
    await periodic
    await settleMicrotasks()

    expect(requestSession).toHaveBeenCalledTimes(2)
    expect(harness.connect).toHaveBeenCalledTimes(2)
    expect(harness.getProviderOptions()?.getToken()).toBe('periodic-token')
    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: true,
      connectionStatus: 'connected',
    })
  })

  it('a periodic lease refresh that reopens the socket before the reconnect timer leaves the editor writable and rotates the lease once', async () => {
    let resolvePeriodic!: (value: EditorCollaborationSession) => void
    const requestSession = vi.fn()
      .mockResolvedValueOnce(session('initial-token'))
      .mockImplementationOnce(() => new Promise((resolve) => {
        resolvePeriodic = resolve
      }))
    const harness = createHarness(requestSession, undefined, { acknowledgeTokenSync: false })
    await harness.controller.start()
    const periodic = harness.controller.refreshLease('periodic')
    harness.getProviderOptions()?.events.onClose(1006, 'network_lost')
    expect(harness.scheduledDelays()).toContain(1_000)

    resolvePeriodic(session('periodic-token'))
    await periodic
    await settleMicrotasks()

    expect(harness.connect).toHaveBeenCalledTimes(2)
    expect(harness.scheduledDelays()).not.toContain(1_000)
    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: true,
      connectionStatus: 'connected',
      nextReconnectAt: null,
    })
    harness.runTimersUpTo(1_000)
    await settleMicrotasks()
    expect(requestSession).toHaveBeenCalledTimes(2)
    expect(harness.controller.getSnapshot()).toMatchObject({
      canEdit: true,
      connectionStatus: 'connected',
    })
  })

  it('abandons a lease refresh whose session request does not answer within twenty seconds, and retries', async () => {
    const signals: Array<AbortSignal | undefined> = []
    const requestSession = vi.fn((
      leaseToken?: string,
      rotationCommandId?: string,
      signal?: AbortSignal,
    ) => {
      void leaseToken
      void rotationCommandId
      signals.push(signal)
      return signals.length === 1
        ? Promise.resolve<EditorCollaborationSession>(session('initial-token'))
        : new Promise<EditorCollaborationSession>(() => undefined)
    })
    const harness = createHarness(requestSession)
    await harness.controller.start()
    harness.getProviderOptions()?.events.onClose(1006, 'network_lost')
    harness.runTimer(1_000)
    await settleMicrotasks()
    expect(harness.scheduledDelays()).toContain(20_000)
    expect(signals[1]?.aborted).toBe(false)

    harness.runTimer(20_000)
    await settleMicrotasks()

    expect(signals[1]?.aborted).toBe(true)
    // The controller's own wording; a reword must be caught here.
    expect(harness.controller.getSnapshot()).toMatchObject({
      connectionStatus: 'reconnecting',
      error: 'The collaboration lease could not be refreshed; reconnecting read-only.',
      nextReconnectAt: NOW_MS + 2_000,
      reconnectAttempt: 2,
    })
    expect(harness.scheduledDelays()).toContain(2_000)
  })

  it('abandons a session request on its own clock even when the request never observes its abort signal', async () => {
    // A request whose CSRF token was refused first waits for the token
    // recovery, which does not observe the signal.
    const requestSession = vi.fn(() => new Promise<EditorCollaborationSession>(() => undefined))
    const harness = createHarness(requestSession)
    const starting = harness.controller.start()
    await settleMicrotasks()
    expect(harness.scheduledDelays()).toContain(20_000)

    harness.runTimer(20_000)
    await starting

    expect(harness.controller.getSnapshot()).toMatchObject({
      connectionStatus: 'reconnecting',
      error: COLLABORATION_NOTICES.session_open_failed_retrying,
      nextReconnectAt: NOW_MS + 1_000,
      reconnectAttempt: 1,
    })
    expect(harness.scheduledDelays()).toContain(1_000)
  })

  it('a connect attempt that fails before any answer is retried on the reconnect ladder', async () => {
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    try {
      const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
      harness.connect.mockImplementationOnce(async () => {
        throw new Error('The socket could not be created.')
      })

      await harness.controller.start()
      await settleMicrotasks()

      expect(harness.controller.getSnapshot()).toMatchObject({
        connectionStatus: 'reconnecting',
        error: 'The collaboration connection was interrupted; reconnecting read-only.',
        reconnectAttempt: 1,
      })
      harness.runTimer(1_000)
      await settleMicrotasks()
      expect(harness.controller.getSnapshot()).toMatchObject({
        canEdit: true,
        connectionStatus: 'connected',
      })
    } finally {
      vi.restoreAllMocks()
    }
  })

  it('a close right after the sync keeps counting the reconnect ladder', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    await harness.controller.start()
    const delays: number[] = []

    for (let cycle = 0; cycle < 6; cycle += 1) {
      harness.getProviderOptions()?.events.onClose(1_000, 'invalid_request')
      const { nextReconnectAt } = harness.controller.getSnapshot()
      delays.push(nextReconnectAt! - NOW_MS)
      harness.runTimer(delays.at(-1)!)
      await settleMicrotasks()
      expect(harness.controller.getSnapshot()).toMatchObject({
        canEdit: true,
        connectionStatus: 'connected',
      })
    }

    expect(delays).toEqual([1_000, 2_000, 4_000, 8_000, 15_000, 30_000])
  })

  it('a connection that stays synchronized or confirms a change starts the reconnect ladder again from its first step', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    const reconnect = async () => {
      harness.getProviderOptions()?.events.onClose(1_000, 'invalid_request')
      harness.runTimer(harness.controller.getSnapshot().nextReconnectAt! - NOW_MS)
      await settleMicrotasks()
    }
    for (let cycle = 0; cycle < 3; cycle += 1) await reconnect()
    expect(harness.controller.getSnapshot().reconnectAttempt).toBe(3)
    expect(harness.scheduledDelays()).toContain(10_000)

    harness.runTimer(10_000)

    expect(harness.controller.getSnapshot().reconnectAttempt).toBe(0)
    await reconnect()
    await reconnect()
    expect(harness.controller.getSnapshot().reconnectAttempt).toBe(2)
    editorDocumentOf(harness).getMap('test').set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    await acknowledge(harness, harness.sentOnSocket(harness.socketNumber())[0]!)
    expect(harness.controller.getSnapshot().reconnectAttempt).toBe(0)
  })

  it('after an ingress overflow only a durable acknowledgement starts the reconnect ladder again, however long the socket stays synchronized', async () => {
    const harness = contentHarness(session('initial-token'))
    await harness.controller.start()
    editorDocumentOf(harness).getMap('test').set('a', 'a')
    harness.runTimer(COLLABORATION_UPDATE_BATCH_MS)
    await settleMicrotasks()
    const [a] = harness.sentOnSocket(1)
    harness.getProviderOptions()?.events.onClose(4429, 'ingress_overflow')
    harness.runTimer(1_000)
    await settleMicrotasks()
    expect(harness.controller.getSnapshot()).toMatchObject({ reconnectAttempt: 1, synced: true })

    // A synchronized socket that confirms nothing does not show that the
    // service keeps up with this editor again.
    harness.advance(COLLABORATION_STABLE_CONNECTION_MS)
    harness.runTimersUpTo(COLLABORATION_STABLE_CONNECTION_MS)
    expect(harness.controller.getSnapshot().reconnectAttempt).toBe(1)

    await acknowledge(harness, a!)
    expect(harness.controller.getSnapshot().reconnectAttempt).toBe(0)
  })

  it('a late failure of a connect attempt that already answered leaves its connection in place', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    try {
      const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
      const answer = harness.connect.getMockImplementation()!
      let failLate!: (error: Error) => void
      harness.connect.mockImplementationOnce(() => {
        void answer()
        return new Promise<void>((_, reject) => {
          failLate = reject
        })
      })
      await harness.controller.start()
      await settleMicrotasks()
      expect(harness.controller.getSnapshot()).toMatchObject({
        canEdit: true,
        connectionStatus: 'connected',
      })

      failLate(new Error('The connect attempt failed after its answer.'))
      await settleMicrotasks()

      expect(harness.disconnect).not.toHaveBeenCalled()
      expect(harness.controller.getSnapshot()).toMatchObject({
        canEdit: true,
        connectionStatus: 'connected',
        reconnectAttempt: 0,
      })
    } finally {
      warn.mockRestore()
    }
  })

  it('a late failure of an older connect attempt leaves the newer attempt in place', async () => {
    const harness = createHarness(vi.fn().mockResolvedValue(session('initial-token')))
    const answer = harness.connect.getMockImplementation()!
    let failFirst!: (error: Error) => void
    harness.connect.mockImplementationOnce(() => {
      void answer()
      return new Promise<void>((_, reject) => {
        failFirst = reject
      })
    })
    await harness.controller.start()
    harness.getProviderOptions()?.events.onClose(1006, 'network_lost')
    // The next attempt opens its socket and has not answered yet.
    harness.connect.mockImplementationOnce(async () => {
      harness.socketState.open = true
    })
    harness.runTimer(1_000)
    await settleMicrotasks()
    expect(harness.connect).toHaveBeenCalledTimes(2)
    expect(harness.scheduledDelays()).toContain(COLLABORATION_CONNECT_ANSWER_TIMEOUT_MS)
    const disconnects = harness.disconnect.mock.calls.length

    failFirst(new Error('The first connect attempt failed late.'))
    await settleMicrotasks()

    expect(harness.disconnect).toHaveBeenCalledTimes(disconnects)
    expect(harness.controller.getSnapshot()).toMatchObject({
      connectionStatus: 'reconnecting',
      nextReconnectAt: null,
      reconnectAttempt: 1,
    })
    expect(harness.scheduledDelays()).toContain(COLLABORATION_CONNECT_ANSWER_TIMEOUT_MS)
  })

  it('gives a connect attempt more time than the service may take to answer its authentication', () => {
    const config = readFileSync(
      new URL('../../../../collaboration-server/src/config.ts', import.meta.url),
      'utf8',
    )
    const bound = /'INQTRIX_COLLABORATION_HTTP_TIMEOUT_MS',\s*([\d_]+),\s*([\d_]+),\s*([\d_]+)\)/
      .exec(config)
    expect(bound).not.toBeNull()
    const serverMaximumMs = Number(bound![3]!.replaceAll('_', ''))
    expect(serverMaximumMs).toBeGreaterThan(Number(bound![1]!.replaceAll('_', '')))
    // The socket handshake and the gateway hop get five seconds on top.
    expect(COLLABORATION_CONNECT_ANSWER_TIMEOUT_MS).toBeGreaterThanOrEqual(serverMaximumMs + 5_000)
  })
})
