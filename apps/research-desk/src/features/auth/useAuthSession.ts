import { useCallback, useEffect, useRef, useState } from 'react'
import {
  buildLoginUrl,
  fetchAuthSession,
  logoutSession,
  setUnauthorizedReload,
  type AuthSessionInfo,
} from '@/api/inqtrixClient'
import {
  automaticReloadStep,
  gatewayAnswerFromError,
  gatewaySessionAnswer,
  gatewayState,
  heldReloadState,
  type GatewayStateInfo,
} from './authMode'

export type AuthSessionState = {
  /** `unknown` until the first probe answers; the UI shows neither
   * login nor logout while unknown to avoid a button flash. */
  status: 'anonymous' | 'authenticated' | 'unknown'
  /** Canonical authenticated user. External issuer subjects never enter the
   * SPA authorization state. */
  user: {
    displayName: string | null
    email: string | null
    id: string
    role: string
  } | null
  /** The user's canonical project namespace (cross-device), resolved
   * server-side from the session; `null` while anonymous/unknown or before a
   * namespace has been adopted. The desk scopes the project to this (not the
   * browser-local id) when authenticated. */
  projectNamespace: string | null
}

const ANONYMOUS: AuthSessionState = {
  status: 'anonymous',
  user: null,
  projectNamespace: null,
}

const SIGNED_OUT: GatewayStateInfo = { state: 'signedOut', outcome: null }

/** Full reload is the account-switch boundary for all browser-held stores. */
export function reloadApplication() {
  window.location.reload()
}

/** Destroy the server session before crossing the hard reload boundary.
 * `reload` receives the server's answer (the gateway mode reads its
 * sign-out URL from it). */
export async function logoutAndReload(
  destroySession: () => Promise<unknown> = logoutSession,
  reload: (result: unknown) => void = reloadApplication,
  onError?: (error: unknown) => void,
  awaitDurability?: () => Promise<void>,
): Promise<boolean> {
  let result: unknown
  try {
    await awaitDurability?.()
    result = await destroySession()
  } catch (error) {
    // A failed server-side logout must leave the confirmed session intact.
    console.warn('Logout failed; the current session remains active.', error)
    onError?.(error)
    return false
  }
  reload(result)
  return true
}

/** Marks that this browser signed out of a gateway session. The gateway
 * may still assert the user, so any session probe would start the next
 * session at once; the mark keeps the signed-out page across reloads and
 * tabs until the user signs in again. Its value says whether the sign-out
 * went on to the gateway's own sign-out. */
export const GATEWAY_SIGNED_OUT_KEY = 'inqtrix.gateway.signedOut'
const MARK_SIGNED_OUT = '1'
const MARK_SIGNED_OUT_AT_GATEWAY = 'gateway'

type MarkStorage = Pick<Storage, 'getItem' | 'removeItem' | 'setItem'>

function browserStorage(): MarkStorage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    return null
  }
}

export function readGatewaySignedOut(storage: MarkStorage | null = browserStorage()): boolean {
  return storedGatewaySignOut(storage) !== null
}

/** The signed-out state the stored mark stands for, `null` without one. */
export function storedGatewaySignOut(storage: MarkStorage | null = browserStorage()): GatewayStateInfo | null {
  let value: string | null | undefined
  try {
    value = storage?.getItem(GATEWAY_SIGNED_OUT_KEY)
  } catch {
    return null
  }
  if (value === MARK_SIGNED_OUT) return gatewayState({ kind: 'signedOut', atGateway: false })
  if (value === MARK_SIGNED_OUT_AT_GATEWAY) return gatewayState({ kind: 'signedOut', atGateway: true })
  return null
}

/** Whether the mark is stored; without storage the caller keeps the
 * signed-out page in memory instead. */
export function markGatewaySignedOut(
  storage: MarkStorage | null = browserStorage(),
  atGateway = false,
): boolean {
  const value = atGateway ? MARK_SIGNED_OUT_AT_GATEWAY : MARK_SIGNED_OUT
  try {
    if (!storage) return false
    storage.setItem(GATEWAY_SIGNED_OUT_KEY, value)
    return storage.getItem(GATEWAY_SIGNED_OUT_KEY) === value
  } catch {
    return false
  }
}

export function clearGatewaySignedOut(storage: MarkStorage | null = browserStorage()) {
  try {
    storage?.removeItem(GATEWAY_SIGNED_OUT_KEY)
  } catch {
    // Nothing stored, nothing to clear.
  }
}

/** Whether a storage event tells this tab that another tab signed out of
 * the gateway session; this tab then reloads into the signed-out page
 * before any probe of it starts the next session. */
export function gatewaySignOutStored(event: { key: string | null; newValue: string | null }): boolean {
  return (
    event.key === GATEWAY_SIGNED_OUT_KEY &&
    (event.newValue === MARK_SIGNED_OUT || event.newValue === MARK_SIGNED_OUT_AT_GATEWAY)
  )
}

/** When this tab last reloaded by itself after a 401 in gateway mode. Kept
 * per tab, across its reloads, so the next page life can hold back. */
export const GATEWAY_RELOADED_AT_KEY = 'inqtrix.gateway.reloadedAt'

function tabStorage(): MarkStorage | null {
  try {
    return typeof window === 'undefined' ? null : window.sessionStorage
  } catch {
    return null
  }
}

/** The time of this tab's last automatic reload, `null` when none is known
 * or none can be read. */
export function readGatewayReloadedAt(storage: MarkStorage | null = tabStorage()): number | null {
  try {
    const stored = storage?.getItem(GATEWAY_RELOADED_AT_KEY)
    if (!stored) return null
    const at = Number(stored)
    return Number.isFinite(at) ? at : null
  } catch {
    return null
  }
}

/** Whether the time is stored; without storage this page life is the only
 * record of the reload. */
export function noteGatewayReload(at: number, storage: MarkStorage | null = tabStorage()): boolean {
  try {
    if (!storage) return false
    storage.setItem(GATEWAY_RELOADED_AT_KEY, String(at))
    return true
  } catch {
    return false
  }
}

/** This page life: whether it started an automatic reload already. */
const PAGE_LIFE = { reloadStarted: false }

/**
 * Reload after a 401 that took the identity the page shows, unless the
 * brake holds it back (`automaticReloadStep`): then `hold` shows the failed
 * gateway sign-in with the class of the 401. Without storage the page
 * reloads at most once per page life.
 */
export function reloadUnlessBraked(
  error: unknown,
  {
    hold,
    now = Date.now,
    page = PAGE_LIFE,
    reload = reloadApplication,
    storage = tabStorage(),
  }: {
    hold: (state: GatewayStateInfo) => void
    now?: () => number
    page?: { reloadStarted: boolean }
    reload?: () => void
    storage?: MarkStorage | null
  },
): 'reload' | 'hold' | 'underway' {
  const at = now()
  const step = automaticReloadStep({
    lastReloadAt: readGatewayReloadedAt(storage),
    now: at,
    reloadStarted: page.reloadStarted,
  })
  if (step === 'hold') {
    console.warn('The page reloaded by itself moments ago and lost its session again; the reload is held back.')
    hold(heldReloadState(error))
  } else if (step === 'reload') {
    page.reloadStarted = true
    if (!noteGatewayReload(at, storage)) {
      console.warn('The automatic reload cannot be noted (no browser storage); a 401 after it reloads the page again.')
    }
    reload()
  }
  return step
}

/** The gateway's sign-out URL from a logout answer, or `null`. Only an
 * http(s) URL or a same-origin path is followed. */
export function gatewayLogoutTarget(result: unknown): string | null {
  const url = typeof result === 'object' && result !== null
    ? (result as { redirect_url?: unknown }).redirect_url
    : null
  if (typeof url !== 'string' || url === '') return null
  if (/^https?:\/\//i.test(url) || (url.startsWith('/') && !url.startsWith('//'))) return url
  console.warn('The gateway logout URL is neither an http(s) URL nor a path; the signed-out page is shown instead.')
  return null
}

/** Leave after a gateway logout. The sign-out is marked first in every
 * case, with whether it goes on to the gateway: the other tabs of this
 * browser switch to the signed-out page at once and start no session while
 * the gateway signs out, even when it asks to confirm. Then follow the
 * gateway's sign-out URL, or show the signed-out page without starting a
 * new session. */
export function leaveGatewaySession(
  result: unknown,
  {
    markSignedOut = (atGateway: boolean) => markGatewaySignedOut(browserStorage(), atGateway),
    navigate = (url: string) => window.location.assign(url),
    reload = reloadApplication,
    showSignedOut,
  }: {
    markSignedOut?: (atGateway: boolean) => boolean
    navigate?: (url: string) => void
    reload?: () => void
    showSignedOut: () => void
  },
): 'navigated' | 'reloaded' | 'shown' {
  const target = gatewayLogoutTarget(result)
  const marked = markSignedOut(target !== null)
  if (target !== null) {
    if (!marked) {
      console.warn('The sign-out cannot be marked (no browser storage); other tabs of this browser may start a new session while the gateway signs out.')
    }
    navigate(target)
    return 'navigated'
  }
  if (marked) {
    reload()
    return 'reloaded'
  }
  console.warn('The signed-out state cannot be kept across reloads (no browser storage); it holds until this page reloads.')
  showSignedOut()
  return 'shown'
}

type ProbeResult = {
  session: AuthSessionState
  gatewayState: GatewayStateInfo | null
}

/**
 * One probe of `GET /api/auth/session`. In gateway mode every answer that
 * signs nobody in becomes the state of the session start, and a sign-out
 * mark answers without asking the server, which would start the next
 * session at once. Only an aborted probe throws.
 */
export async function probeSession(
  {
    gateway,
    signal,
    workspaceId,
  }: { gateway: boolean; signal?: AbortSignal; workspaceId?: string },
  fetchSession: typeof fetchAuthSession = fetchAuthSession,
  signedOut: () => GatewayStateInfo | null = storedGatewaySignOut,
): Promise<ProbeResult> {
  if (gateway) {
    const stored = signedOut()
    if (stored !== null) return { session: ANONYMOUS, gatewayState: stored }
  }
  try {
    // Send the browser's namespace as the CANDIDATE: on a first authenticated
    // boot the server adopts it as the user's canonical project namespace and
    // returns it in `project_namespace`; thereafter it returns the already-
    // adopted value (the same on every device, so the data follows the user).
    const info: AuthSessionInfo = await fetchSession({ signal, workspaceId })
    if (gateway) {
      const answer = gatewaySessionAnswer(info)
      if (answer !== null) return { session: ANONYMOUS, gatewayState: gatewayState(answer) }
    }
    return {
      session: info.authenticated
        ? {
            status: 'authenticated',
            user: {
              displayName: info.user.display_name ?? null,
              email: info.user.email ?? null,
              id: info.user.id,
              role: info.user.role,
            },
            projectNamespace: info.project_namespace ?? null,
          }
        : ANONYMOUS,
      gatewayState: null,
    }
  } catch (error) {
    if (signal?.aborted) throw error
    if (gateway) {
      // Every failed start shows its state: the reason stays on the page.
      return { session: ANONYMOUS, gatewayState: gatewayState(gatewayAnswerFromError(error)) }
    }
    // Server unreachable: treat as anonymous; the existing health
    // banner already surfaces connectivity problems.
    console.warn('Authentication session probe failed.', error)
    return { session: ANONYMOUS, gatewayState: null }
  }
}

/** The user the running page showed last. A probe that signs nobody in, or
 * a session the page lost in place, keeps it: the page still holds what it
 * showed of that user. */
export function lastShownUserId(previous: string | null, session: AuthSessionState): string | null {
  return session.status === 'authenticated' && session.user !== null ? session.user.id : previous
}

/** Whether a probe of the running page names another signed-in user than
 * the one it showed last, also after it lost that session in place. The
 * page then reloads instead of adopting that user: what it holds of the
 * shown account (an open run, a report, composer text) must not carry over
 * to the next. */
export function probeSwitchesUser(lastShown: string | null, probed: AuthSessionState): boolean {
  return lastShown !== null
    && probed.status === 'authenticated'
    && probed.user !== null
    && probed.user.id !== lastShown
}

/**
 * Cookie-session state for the SPA. Probes `GET /api/auth/session` only
 * when the server reports a cookie-session mode (`oidc`/`local`/`gateway`)
 * — in `none`/`apikey` deployments (and demo mode) the hook stays inert
 * and reports `anonymous` without any network traffic. `login` is the
 * OIDC full-page redirect (local uses the credential form); successful
 * credential login and logout reload the document so no prior account's
 * reducer or hook state can survive an identity transition, and so does a
 * probe of the running page that names another signed-in user than the one
 * it showed last. In gateway mode every answer of the session start that
 * signs nobody in becomes one `gatewayState`, `login` signs in again after a
 * logout, and the reload after a 401 that took the shown identity runs
 * through a brake. `refreshing` is true while a probe started by `refresh`
 * runs.
 */
export function useAuthSession(
  active: boolean,
  workspaceId?: string,
  awaitLogoutDurability?: () => Promise<void>,
  gateway = false,
) {
  const [session, setSession] = useState<AuthSessionState>(
    active ? { ...ANONYMOUS, status: 'unknown' } : ANONYMOUS,
  )
  const lastShownUserRef = useRef<string | null>(null)
  lastShownUserRef.current = lastShownUserId(lastShownUserRef.current, session)
  const [gatewayStateInfo, setGatewayStateInfo] = useState<GatewayStateInfo | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const generationRef = useRef(0)
  const controllerRef = useRef<AbortController | null>(null)
  const [logoutError, setLogoutError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!active) return
    controllerRef.current?.abort()
    const controller = new AbortController()
    controllerRef.current = controller
    const generation = generationRef.current + 1
    generationRef.current = generation
    setRefreshing(true)
    try {
      const result = await probeSession({
        gateway,
        signal: controller.signal,
        workspaceId,
      })
      if (controller.signal.aborted || generation !== generationRef.current) return
      if (probeSwitchesUser(lastShownUserRef.current, result.session)) {
        reloadApplication()
        return
      }
      setGatewayStateInfo(result.gatewayState)
      setSession(result.session)
    } catch (error) {
      // Only an aborted probe throws; the newer probe owns the state.
      if (!controller.signal.aborted) console.warn('Authentication session probe failed.', error)
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null
        setRefreshing(false)
      }
    }
  }, [active, gateway, workspaceId])

  useEffect(() => {
    if (!active || !gateway) return
    setUnauthorizedReload((error) => {
      reloadUnlessBraked(error, {
        hold: (state) => {
          generationRef.current += 1
          setGatewayStateInfo(state)
          setSession(ANONYMOUS)
        },
      })
    })
    return () => setUnauthorizedReload(null)
  }, [active, gateway])

  useEffect(() => {
    if (!active) {
      generationRef.current += 1
      controllerRef.current?.abort()
      controllerRef.current = null
      setRefreshing(false)
      setGatewayStateInfo(null)
      setSession(ANONYMOUS)
      return
    }
    // `active` (= cookie-session mode) usually flips true only AFTER health
    // resolves post-mount, so the mount-time initializer cannot seed
    // `unknown`. Seed it here, before the probe, so the desk withholds the
    // auth-lock form while the session is being checked (no lock flash for an
    // already-authenticated user reloading the page).
    setSession((current) =>
      current.status === 'authenticated'
        ? current
        : { ...ANONYMOUS, status: 'unknown' },
    )
    void refresh()
    return () => {
      generationRef.current += 1
      controllerRef.current?.abort()
      controllerRef.current = null
      setRefreshing(false)
    }
  }, [active, refresh])

  const login = useCallback(() => {
    if (gateway) {
      // The gateway signs the user in; a reload starts the session from its
      // assertion and crosses the account-switch boundary.
      clearGatewaySignedOut()
      reloadApplication()
      return
    }
    window.location.assign(
      buildLoginUrl(undefined, window.location.pathname || '/'),
    )
  }, [gateway])

  const logout = useCallback(async () => {
    setLogoutError(null)
    return logoutAndReload(
      logoutSession,
      gateway
        ? (result) => {
            leaveGatewaySession(result, {
              showSignedOut: () => {
                generationRef.current += 1
                setGatewayStateInfo(SIGNED_OUT)
                setSession(ANONYMOUS)
              },
            })
          }
        : reloadApplication,
      (error) => {
        setLogoutError(error instanceof Error ? error.message : String(error))
      },
      awaitLogoutDurability,
    )
  }, [awaitLogoutDurability, gateway])

  return { session, gatewayState: gatewayStateInfo, login, logout, logoutError, refresh, refreshing }
}
