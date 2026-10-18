import { useEffect, useState } from 'react'
import {
  fetchAuthSession,
  hasHttpStatus,
  streamUserEvents,
  type AuthSessionInfo,
  type UserEvent,
} from '@/api/inqtrixClient'

export type UserEventAction = 'consume' | 'ignore' | 'refetch' | 'reload'

const COLLABORATION_TRANSPORT_SCOPES = new Set([
  'collaboration_comment_changed',
  'collaboration_comment_mention',
])

/**
 * Translate the content-free user event into a shell action. The ready frame
 * is also the cross-tab/account boundary: a stream for another user must never
 * wake stores under the currently rendered identity.
 */
export function userEventAction(
  event: UserEvent,
  expectedUserId: string,
): UserEventAction {
  if (event.type === 'ready') {
    return event.data.user_id === expectedUserId ? 'refetch' : 'reload'
  }
  if (
    event.type === 'invalidate'
    && COLLABORATION_TRANSPORT_SCOPES.has(event.data.scope)
  ) return 'consume'
  if (event.type === 'invalidate' || event.type === 'reset') return 'refetch'
  return 'ignore'
}

/** Scopes whose lists refresh on their own. Every other scope, a reconnect,
 * a reset, focus and coming online refresh everything: a missed or unknown
 * change must not leave a list stale. */
const SCOPED_REFRESH = new Set([
  'collaboration_guest_policy',
  'deletion_operations',
  'editor_documents',
  'indexing',
  'knowledge_collections',
  'prompt_templates',
  'runs',
  'sharing',
  'skills',
])

/** How often each list was told to refresh: `all` counts refreshes of every
 * list, `byScope` those of one scope. */
export type RefreshState = {
  all: number
  byScope: Readonly<Record<string, number>>
}

/** Events folded into the next refresh. */
export type PendingWake = { all: boolean; scopes: Set<string> }

/** Fold one event into the next refresh; `null` stands for "everything". */
export function noteWake(pending: PendingWake, scope: string | null): void {
  if (scope !== null && SCOPED_REFRESH.has(scope)) pending.scopes.add(scope)
  else pending.all = true
}

export function applyWake(state: RefreshState, pending: PendingWake): RefreshState {
  if (pending.all) return { ...state, all: state.all + 1 }
  if (pending.scopes.size === 0) return state
  const byScope = { ...state.byScope }
  for (const scope of pending.scopes) byScope[scope] = (byScope[scope] ?? 0) + 1
  return { ...state, byScope }
}

/** The refresh token of a list that depends on these scopes. */
export function refreshTokenFor(state: RefreshState, scopes: readonly string[]): number {
  let token = state.all
  for (const scope of scopes) token += state.byScope[scope] ?? 0
  return token
}

/** Bounded reconnect delay; exported so the transport policy stays testable. */
export function userEventReconnectDelay(attempt: number): number {
  return Math.min(30_000, 1_000 * 2 ** Math.min(Math.max(attempt, 0), 5))
}

export type IdentityVerification = {
  action: 'refetch' | 'reload' | 'retain'
  error: string | null
}

/** Advance the replay cursor only after the current identity was confirmed. */
export function confirmedUserEventCursor(
  current: string | undefined,
  event: UserEvent,
  verification: IdentityVerification,
): string | undefined {
  if (verification.action !== 'refetch') return current
  if (event.type === 'reset') return undefined
  return event.id ?? current
}

const UNRECOGNIZED_SESSION_ANSWER =
  'The session probe answered without a recognizable identity.'

/**
 * Resolve one canonical-session probe only if it is still the latest probe.
 * Only a confirmed identity change reloads the page: an anonymous session,
 * another user, a 401 or 403 answer, or the server naming another principal
 * for this browser. Everything that proves nothing about the identity keeps
 * the rendered page but authorizes no resource read: network failures,
 * timeouts, rate limits, server errors, other HTTP failures, an answer that
 * is not JSON (a proxy's error page) and a JSON answer without a
 * recognizable identity.
 */
export async function verifyLatestUserIdentity({
  expectedUserId,
  isCurrent,
  probe,
}: {
  expectedUserId: string
  isCurrent: () => boolean
  probe: () => Promise<AuthSessionInfo>
}): Promise<IdentityVerification | null> {
  let session: AuthSessionInfo
  try {
    session = await probe()
  } catch (error) {
    if (!isCurrent()) return null
    if (confirmsIdentityChange(error)) return { action: 'reload', error: null }
    return {
      action: 'retain',
      error: error instanceof Error && error.message
        ? error.message
        : 'User identity verification failed.',
    }
  }
  if (!isCurrent()) return null
  const identity = sessionIdentity(session)
  if (identity === undefined) {
    return { action: 'retain', error: UNRECOGNIZED_SESSION_ANSWER }
  }
  return identity === expectedUserId
    ? { action: 'refetch', error: null }
    : { action: 'reload', error: null }
}

function confirmsIdentityChange(error: unknown): boolean {
  if (!(error instanceof Error)) return false
  if (error.name === 'principal_changed') return true
  const status = (error as { status?: unknown }).status
  return status === 401 || status === 403
}

/** The user the session names, null for an anonymous session, or undefined
 * when the answer names neither. */
function sessionIdentity(session: unknown): string | null | undefined {
  if (typeof session !== 'object' || session === null) return undefined
  const { authenticated, user } = session as { authenticated?: unknown; user?: unknown }
  if (authenticated === false) return null
  if (authenticated !== true || typeof user !== 'object' || user === null) return undefined
  const id = (user as { id?: unknown }).id
  return typeof id === 'string' && id.length > 0 ? id : undefined
}

export type UserIdentityVerifier = {
  /** Forgets probes in flight and any scheduled retry: a ready frame of the
   * stream confirmed the identity itself. */
  reset: () => void
  stop: () => void
  /** Probes the session now. A newer call supersedes an older one still in
   * flight. */
  verify: () => Promise<void>
}

/**
 * Probes the canonical session and acts on the answer. A confirmed identity
 * change reloads the page. An answer that proves nothing keeps the page,
 * reports why and probes again after a bounded, growing delay until the
 * identity is confirmed or changed; nothing is read meanwhile.
 */
export function createUserIdentityVerifier({
  cancelTimer,
  expectedUserId,
  onConfirmed,
  onUnverified,
  probe,
  reload,
  scheduleTimer,
}: {
  cancelTimer: (timer: ReturnType<typeof setTimeout>) => void
  expectedUserId: string
  onConfirmed: (verification: IdentityVerification) => void
  onUnverified: (error: string) => void
  probe: () => Promise<AuthSessionInfo>
  reload: () => void
  scheduleTimer: (callback: () => void, delayMs: number) => ReturnType<typeof setTimeout>
}): UserIdentityVerifier {
  let generation = 0
  let retryAttempt = 0
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let stopped = false

  const clearRetry = () => {
    if (retryTimer === null) return
    cancelTimer(retryTimer)
    retryTimer = null
  }

  const verify = async (): Promise<void> => {
    if (stopped) return
    clearRetry()
    generation += 1
    const current = generation
    const verification = await verifyLatestUserIdentity({
      expectedUserId,
      isCurrent: () => !stopped && current === generation,
      probe,
    })
    if (verification === null) return
    if (verification.action === 'reload') {
      stopped = true
      reload()
      return
    }
    if (verification.action === 'retain') {
      onUnverified(verification.error ?? 'User identity verification failed.')
      const delay = userEventReconnectDelay(retryAttempt)
      retryAttempt += 1
      retryTimer = scheduleTimer(() => {
        retryTimer = null
        void verify()
      }, delay)
      return
    }
    retryAttempt = 0
    onConfirmed(verification)
  }

  return {
    reset: () => {
      generation += 1
      retryAttempt = 0
      clearRetry()
    },
    stop: () => {
      stopped = true
      generation += 1
      clearRetry()
    },
    verify,
  }
}

/**
 * Maintain the one user-scoped invalidation stream. Its only output is how
 * often each list was told to refresh (by scope, or everything): consumers
 * re-read their authoritative list endpoints. No resource content is ever
 * trusted or patched from SSE.
 */
export function useUserInvalidationEvents({
  enabled,
  userId,
}: {
  enabled: boolean
  userId: string | null
}): { error: string | null; refresh: RefreshState } {
  const [refresh, setRefresh] = useState<RefreshState>({ all: 0, byScope: {} })
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!enabled || !userId) {
      setError(null)
      return
    }

    const controller = new AbortController()
    let lastEventId: string | undefined
    let reconnectAttempt = 0
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null
    let wakeTimer: ReturnType<typeof setTimeout> | null = null
    let stopped = false
    let pendingInvalidation: UserEvent | null = null
    // Scopes of the invalidations waiting for the identity check; empty when
    // the check came from focus or coming online.
    let pendingScopes: Array<string | null> = []
    let pendingWake: PendingWake = { all: false, scopes: new Set() }

    const scheduleWake = (scopes: ReadonlyArray<string | null>) => {
      for (const scope of scopes) noteWake(pendingWake, scope)
      if (wakeTimer !== null) clearTimeout(wakeTimer)
      wakeTimer = setTimeout(() => {
        wakeTimer = null
        const wake = pendingWake
        pendingWake = { all: false, scopes: new Set() }
        if (!stopped) setRefresh((current) => applyWake(current, wake))
      }, 75)
    }

    const identity = createUserIdentityVerifier({
      cancelTimer: (timer) => clearTimeout(timer),
      expectedUserId: userId,
      onConfirmed: (verification) => {
        if (pendingInvalidation !== null) {
          lastEventId = confirmedUserEventCursor(
            lastEventId,
            pendingInvalidation,
            verification,
          )
          pendingInvalidation = null
        }
        setError(null)
        const scopes = pendingScopes.length > 0 ? pendingScopes : [null]
        pendingScopes = []
        scheduleWake(scopes)
      },
      onUnverified: (reason) => {
        setError(reason)
        console.warn(
          'User identity could not be verified; resource refresh was withheld.',
          reason,
        )
      },
      probe: () => fetchAuthSession({ signal: controller.signal }),
      reload: () => {
        stopped = true
        controller.abort()
        window.location.reload()
      },
      scheduleTimer: (callback, delayMs) => setTimeout(callback, delayMs),
    })
    const verifyIdentityAndWake = () => identity.verify()

    const handleFocus = () => {
      if (document.visibilityState === 'visible') void verifyIdentityAndWake()
    }
    const handleOnline = () => void verifyIdentityAndWake()
    window.addEventListener('focus', handleFocus)
    window.addEventListener('online', handleOnline)

    const connect = async () => {
      if (stopped || controller.signal.aborted) return
      try {
        await streamUserEvents({
          lastEventId,
          signal: controller.signal,
          onEvent: (event) => {
            if (stopped || controller.signal.aborted) return
            const action = userEventAction(event, userId)
            if (action === 'reload') {
              stopped = true
              controller.abort()
              window.location.reload()
              return
            }
            if (action === 'consume') {
              // Collaboration comment events are already delivered to the
              // document room by the sidecar. Consuming the duplicate
              // user-outbox coordinate here prevents one comment from
              // reloading runs, skills, knowledge, folders, documents and
              // auth state in every open participant tab.
              lastEventId = event.id ?? lastEventId
              return
            }
            if (event.type === 'ready') {
              identity.reset()
              pendingInvalidation = null
              pendingScopes = []
              lastEventId = event.id ?? event.data.cursor
            }
            if (action === 'refetch') {
              reconnectAttempt = 0
              if (event.type === 'ready') {
                setError(null)
                scheduleWake([null])
              } else {
                // The browser cookie may have changed in another tab after
                // this stream was opened. Re-probe before any list request so
                // another account can never merge into the rendered stores.
                pendingInvalidation = event
                pendingScopes.push(event.type === 'invalidate' ? event.data.scope : null)
                void verifyIdentityAndWake()
              }
            } else {
              console.warn('Unknown user invalidation event ignored.', event)
            }
          },
        })
        if (!stopped && !controller.signal.aborted) {
          setError('User invalidation stream closed; reconnecting.')
          console.warn('User invalidation stream closed; reconnecting.')
        }
      } catch (error) {
        if (stopped || controller.signal.aborted) return
        if (hasHttpStatus(error, 401) || hasHttpStatus(error, 403)) {
          stopped = true
          controller.abort()
          window.location.reload()
          return
        }
        setError(error instanceof Error ? error.message : String(error))
        console.warn('User invalidation stream failed; reconnecting.', error)
      }

      if (stopped || controller.signal.aborted) return
      const delay = userEventReconnectDelay(reconnectAttempt)
      reconnectAttempt += 1
      reconnectTimer = setTimeout(() => void connect(), delay)
    }

    void connect()
    return () => {
      stopped = true
      controller.abort()
      if (reconnectTimer !== null) clearTimeout(reconnectTimer)
      if (wakeTimer !== null) clearTimeout(wakeTimer)
      identity.stop()
      window.removeEventListener('focus', handleFocus)
      window.removeEventListener('online', handleOnline)
    }
  }, [enabled, userId])

  return { error, refresh }
}
