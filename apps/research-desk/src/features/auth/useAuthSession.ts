import { useCallback, useEffect, useRef, useState } from 'react'
import {
  buildLoginUrl,
  fetchAuthSession,
  logoutSession,
  type AuthSessionInfo,
} from '@/api/inqtrixClient'

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

/** Full reload is the account-switch boundary for all browser-held stores. */
export function reloadApplication() {
  window.location.reload()
}

/** Destroy the server session before crossing the hard reload boundary. */
export async function logoutAndReload(
  destroySession: () => Promise<unknown> = logoutSession,
  reload: () => void = reloadApplication,
  onError?: (error: unknown) => void,
  awaitDurability?: () => Promise<void>,
): Promise<boolean> {
  try {
    await awaitDurability?.()
    await destroySession()
  } catch (error) {
    // A failed server-side logout must leave the confirmed session intact.
    console.warn('Logout failed; the current session remains active.', error)
    onError?.(error)
    return false
  }
  reload()
  return true
}

/**
 * Cookie-session state for the SPA. Probes `GET /api/auth/session` only
 * when the server reports a cookie-session mode (`oidc`/`local`)
 * — in `none`/`apikey` deployments (and demo mode) the hook stays inert
 * and reports `anonymous` without any network traffic. `login` is the
 * OIDC full-page redirect (local uses the credential form); successful
 * credential login and logout reload the document so no prior account's
 * reducer or hook state can survive an identity transition.
 */
export function useAuthSession(
  active: boolean,
  workspaceId?: string,
  awaitLogoutDurability?: () => Promise<void>,
) {
  const [session, setSession] = useState<AuthSessionState>(
    active ? { ...ANONYMOUS, status: 'unknown' } : ANONYMOUS,
  )
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
    try {
      // Send the browser's namespace as the CANDIDATE: on a first authenticated
      // boot the server adopts it as the user's canonical project namespace and
      // returns it in `project_namespace`; thereafter it returns the already-
      // adopted value (the same on every device, so the data follows the user).
      const info: AuthSessionInfo = await fetchAuthSession({
        signal: controller.signal,
        workspaceId,
      })
      if (controller.signal.aborted || generation !== generationRef.current) return
      setSession(
        info.authenticated
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
      )
    } catch (error) {
      if (controller.signal.aborted || generation !== generationRef.current) return
      // Server unreachable: treat as anonymous; the existing health
      // banner already surfaces connectivity problems.
      console.warn('Authentication session probe failed.', error)
      setSession(ANONYMOUS)
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null
    }
  }, [active, workspaceId])

  useEffect(() => {
    if (!active) {
      generationRef.current += 1
      controllerRef.current?.abort()
      controllerRef.current = null
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
    }
  }, [active, refresh])

  const login = useCallback(() => {
    window.location.assign(
      buildLoginUrl(undefined, window.location.pathname || '/'),
    )
  }, [])

  const logout = useCallback(async () => {
    setLogoutError(null)
    return logoutAndReload(
      logoutSession,
      reloadApplication,
      (error) => {
        setLogoutError(error instanceof Error ? error.message : String(error))
      },
      awaitLogoutDurability,
    )
  }, [awaitLogoutDurability])

  return { session, login, logout, logoutError, refresh }
}
