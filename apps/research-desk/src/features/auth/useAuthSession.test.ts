import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  type AuthSessionState,
  GATEWAY_RELOADED_AT_KEY,
  GATEWAY_SIGNED_OUT_KEY,
  clearGatewaySignedOut,
  gatewayLogoutTarget,
  gatewaySignOutStored,
  leaveGatewaySession,
  logoutAndReload,
  markGatewaySignedOut,
  noteGatewayReload,
  probeSession,
  lastShownUserId,
  probeSwitchesUser,
  readGatewayReloadedAt,
  readGatewaySignedOut,
  reloadUnlessBraked,
  storedGatewaySignOut,
} from './useAuthSession'

describe('logoutAndReload', () => {
  afterEach(() => vi.restoreAllMocks())

  it('reloads only after the server confirms logout', async () => {
    const destroySession = vi.fn(async () => undefined)
    const reload = vi.fn()
    const awaitDurability = vi.fn(async () => undefined)

    await expect(
      logoutAndReload(destroySession, reload, undefined, awaitDurability),
    ).resolves.toBe(true)
    expect(awaitDurability).toHaveBeenCalledOnce()
    expect(awaitDurability.mock.invocationCallOrder[0]).toBeLessThan(
      destroySession.mock.invocationCallOrder[0],
    )
    expect(destroySession).toHaveBeenCalledOnce()
    expect(reload).toHaveBeenCalledOnce()
  })

  it('preserves the session when collaboration durability cannot be reached', async () => {
    const failure = new Error('collaboration durability unavailable')
    const awaitDurability = vi.fn(async () => {
      throw failure
    })
    const destroySession = vi.fn(async () => undefined)
    const reload = vi.fn()
    const onError = vi.fn()
    vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    await expect(
      logoutAndReload(destroySession, reload, onError, awaitDurability),
    ).resolves.toBe(false)
    expect(destroySession).not.toHaveBeenCalled()
    expect(reload).not.toHaveBeenCalled()
    expect(onError).toHaveBeenCalledWith(failure)
  })

  it('preserves the rendered session when server logout fails', async () => {
    const failure = new Error('offline')
    const destroySession = vi.fn(async () => {
      throw failure
    })
    const reload = vi.fn()
    const onError = vi.fn()
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    await expect(logoutAndReload(destroySession, reload, onError)).resolves.toBe(false)
    expect(reload).not.toHaveBeenCalled()
    expect(warning).toHaveBeenCalledWith(
      'Logout failed; the current session remains active.',
      failure,
    )
    expect(onError).toHaveBeenCalledWith(failure)
  })
})

function memoryStorage() {
  const values = new Map<string, string>()
  return {
    getItem: (key: string) => values.get(key) ?? null,
    removeItem: (key: string) => void values.delete(key),
    setItem: (key: string, value: string) => void values.set(key, value),
  }
}

function brokenStorage() {
  const fail = () => {
    throw new Error('storage blocked')
  }
  return { getItem: fail, removeItem: fail, setItem: fail }
}

function typedError(name: string, status: number): Error {
  const error = new Error('message') as Error & { status?: number }
  error.name = name
  error.status = status
  return error
}

describe('probeSession in gateway mode', () => {
  afterEach(() => vi.restoreAllMocks())

  it('never asks the server after a sign-out, which would start the next session', async () => {
    const fetchSession = vi.fn(async () => ({ authenticated: false as const }))

    const result = await probeSession({ gateway: true }, fetchSession, () => ({ state: 'signedOut', outcome: null }))

    expect(fetchSession).not.toHaveBeenCalled()
    expect(result.gatewayState).toEqual({ state: 'signedOut', outcome: null })
    expect(result.session.status).toBe('anonymous')
  })

  it('shows the signed-out state the stored mark stands for', async () => {
    const fetchSession = vi.fn(async () => ({ authenticated: false as const }))

    const result = await probeSession(
      { gateway: true },
      fetchSession,
      () => ({ state: 'signedOutAtGateway', outcome: null }),
    )

    expect(fetchSession).not.toHaveBeenCalled()
    expect(result.gatewayState).toEqual({ state: 'signedOutAtGateway', outcome: null })
  })

  it('shows the state of a session start that signed nobody in', async () => {
    const fetchSession = vi.fn(async () => ({ authenticated: false as const, reason: 'assertion_missing' }))

    const result = await probeSession({ gateway: true }, fetchSession, () => null)

    expect(result.gatewayState).toEqual({ state: 'noGatewaySignIn', outcome: 'assertion_missing' })
    expect(result.session.status).toBe('anonymous')
  })

  it('shows the class of a refused start and reads an untyped failure as the gateway answering', async () => {
    const refused = await probeSession(
      { gateway: true },
      vi.fn(async () => {
        throw typedError('not_provisioned', 403)
      }),
      () => null,
    )
    const foreign = await probeSession(
      { gateway: true },
      vi.fn(async () => {
        throw new SyntaxError('Unexpected token <')
      }),
      () => null,
    )

    expect(refused.gatewayState).toEqual({ state: 'notProvisioned', outcome: 'not_provisioned' })
    expect(foreign.gatewayState).toEqual({ state: 'renewGatewaySignIn', outcome: null })
  })

  it('signs the user in without a gateway state', async () => {
    const fetchSession = vi.fn(async () => ({
      authenticated: true as const,
      user: { display_name: 'Ada', email: null, id: 'user-a', role: 'user' },
    }))

    const result = await probeSession({ gateway: true }, fetchSession, () => null)

    expect(result.gatewayState).toBeNull()
    expect(result.session.status).toBe('authenticated')
    expect(result.session.user?.id).toBe('user-a')
  })

  it('leaves the other modes as they were: no mark, no state', async () => {
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const fetchSession = vi.fn(async () => {
      throw typedError('server_error', 500)
    })

    const result = await probeSession({ gateway: false }, fetchSession, () => ({ state: 'signedOut', outcome: null }))

    expect(fetchSession).toHaveBeenCalledOnce()
    expect(result).toEqual({
      gatewayState: null,
      session: { projectNamespace: null, status: 'anonymous', user: null },
    })
    expect(warning).toHaveBeenCalled()
  })

  it('throws only for an aborted probe', async () => {
    const controller = new AbortController()
    controller.abort()
    const aborted = new DOMException('aborted', 'AbortError')

    await expect(
      probeSession(
        { gateway: true, signal: controller.signal },
        vi.fn(async () => {
          throw aborted
        }),
        () => null,
      ),
    ).rejects.toBe(aborted)
  })
})

describe('leaving a gateway session', () => {
  afterEach(() => vi.restoreAllMocks())

  it('hands the server answer to the leaving step', async () => {
    const answer = { logged_out: true, redirect_url: 'https://gateway.example/sign_out' }
    const reload = vi.fn()

    await logoutAndReload(vi.fn(async () => answer), reload)

    expect(reload).toHaveBeenCalledWith(answer)
  })

  it('marks the sign-out before it follows the gateway sign-out URL', () => {
    // The other tabs of this browser must switch to the signed-out page at
    // once: while the gateway signs out (or asks to confirm it), a tab that
    // probes its session would otherwise start the next one.
    const steps: string[] = []
    const navigate = vi.fn((url: string) => steps.push(`navigate ${url}`))
    const markSignedOut = vi.fn((atGateway: boolean) => {
      steps.push(`mark atGateway=${atGateway}`)
      return true
    })
    const reload = vi.fn()

    const outcome = leaveGatewaySession(
      { logged_out: true, redirect_url: 'https://gateway.example/oauth2/sign_out' },
      { markSignedOut, navigate, reload, showSignedOut: vi.fn() },
    )

    expect(outcome).toBe('navigated')
    expect(steps).toEqual(['mark atGateway=true', 'navigate https://gateway.example/oauth2/sign_out'])
    expect(reload).not.toHaveBeenCalled()
  })

  it('follows the gateway sign-out URL without storage too, and says what the other tabs may do', () => {
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const navigate = vi.fn()

    const outcome = leaveGatewaySession(
      { logged_out: true, redirect_url: '/oauth2/sign_out' },
      { markSignedOut: () => false, navigate, reload: vi.fn(), showSignedOut: vi.fn() },
    )

    expect(outcome).toBe('navigated')
    expect(navigate).toHaveBeenCalledWith('/oauth2/sign_out')
    expect(warning).toHaveBeenCalledWith(expect.stringContaining('other tabs'))
  })

  it('without a URL marks the sign-out and reloads into the signed-out page', () => {
    const navigate = vi.fn()
    const markSignedOut = vi.fn(() => true)
    const reload = vi.fn()

    const outcome = leaveGatewaySession(
      { logged_out: true, redirect_url: null },
      { markSignedOut, navigate, reload, showSignedOut: vi.fn() },
    )

    expect(outcome).toBe('reloaded')
    expect(markSignedOut).toHaveBeenCalledExactlyOnceWith(false)
    expect(reload).toHaveBeenCalledOnce()
    expect(navigate).not.toHaveBeenCalled()
  })

  it('without storage shows the signed-out page in place and says why', () => {
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const reload = vi.fn()
    const showSignedOut = vi.fn()

    const outcome = leaveGatewaySession(
      { logged_out: true },
      { markSignedOut: () => false, navigate: vi.fn(), reload, showSignedOut },
    )

    expect(outcome).toBe('shown')
    expect(showSignedOut).toHaveBeenCalledOnce()
    expect(reload).not.toHaveBeenCalled()
    expect(warning).toHaveBeenCalled()
  })

  it('follows only an http(s) URL or a same-origin path', () => {
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    expect(gatewayLogoutTarget({ redirect_url: 'https://gateway.example/sign_out' })).toBe(
      'https://gateway.example/sign_out',
    )
    expect(gatewayLogoutTarget({ redirect_url: '/oauth2/sign_out' })).toBe('/oauth2/sign_out')
    expect(gatewayLogoutTarget({ redirect_url: '//elsewhere.example/' })).toBeNull()
    expect(gatewayLogoutTarget({ redirect_url: 'javascript:alert(1)' })).toBeNull()
    expect(warning).toHaveBeenCalledTimes(2)
    expect(gatewayLogoutTarget({ redirect_url: null })).toBeNull()
    expect(gatewayLogoutTarget(undefined)).toBeNull()
  })
})

describe('the gateway sign-out mark', () => {
  it('holds until it is cleared', () => {
    const storage = memoryStorage()

    expect(readGatewaySignedOut(storage)).toBe(false)
    expect(markGatewaySignedOut(storage)).toBe(true)
    expect(readGatewaySignedOut(storage)).toBe(true)
    clearGatewaySignedOut(storage)
    expect(readGatewaySignedOut(storage)).toBe(false)
  })

  it('keeps whether the sign-out went on to the gateway', () => {
    const storage = memoryStorage()

    expect(storedGatewaySignOut(storage)).toBeNull()
    expect(markGatewaySignedOut(storage, true)).toBe(true)
    expect(storedGatewaySignOut(storage)).toEqual({ state: 'signedOutAtGateway', outcome: null })
    expect(readGatewaySignedOut(storage)).toBe(true)
    expect(markGatewaySignedOut(storage)).toBe(true)
    expect(storedGatewaySignOut(storage)).toEqual({ state: 'signedOut', outcome: null })
    clearGatewaySignedOut(storage)
    expect(storedGatewaySignOut(storage)).toBeNull()
  })

  it('reports a blocked or missing storage instead of throwing', () => {
    expect(markGatewaySignedOut(brokenStorage())).toBe(false)
    expect(readGatewaySignedOut(brokenStorage())).toBe(false)
    expect(storedGatewaySignOut(brokenStorage())).toBeNull()
    expect(() => clearGatewaySignedOut(brokenStorage())).not.toThrow()
    expect(markGatewaySignedOut(null)).toBe(false)
  })
})

/** A storage that also records each write as the storage event other tabs
 * receive for it. */
function recordingStorage() {
  const storage = memoryStorage()
  const events: Array<{ key: string; newValue: string | null }> = []
  return {
    events,
    getItem: storage.getItem,
    removeItem: (key: string) => {
      storage.removeItem(key)
      events.push({ key, newValue: null })
    },
    setItem: (key: string, value: string) => {
      storage.setItem(key, value)
      events.push({ key, newValue: value })
    },
  }
}

describe('a gateway sign-out in another tab', () => {
  it('is seen as soon as that tab stores the mark, whether the sign-out went on to the gateway or not', () => {
    const storage = recordingStorage()

    markGatewaySignedOut(storage)
    markGatewaySignedOut(storage, true)

    expect(storage.events).toHaveLength(2)
    expect(gatewaySignOutStored(storage.events[0]!)).toBe(true)
    expect(gatewaySignOutStored(storage.events[1]!)).toBe(true)
  })

  it('is not seen for a cleared mark, another key or a cleared storage', () => {
    const storage = recordingStorage()
    markGatewaySignedOut(storage)
    clearGatewaySignedOut(storage)

    expect(gatewaySignOutStored(storage.events[1]!)).toBe(false)
    expect(gatewaySignOutStored({ key: GATEWAY_SIGNED_OUT_KEY, newValue: 'other' })).toBe(false)
    expect(gatewaySignOutStored({ key: 'inqtrix.researchDesk.theme', newValue: '1' })).toBe(false)
    expect(gatewaySignOutStored({ key: null, newValue: null })).toBe(false)
  })
})

describe('the brake on automatic reloads in gateway mode', () => {
  afterEach(() => vi.restoreAllMocks())

  const unauthorized = typedError('unauthorized', 401)

  it('reloads after a 401 and notes when, so the next page life can hold back', () => {
    const storage = memoryStorage()
    const page = { reloadStarted: false }
    const reload = vi.fn()
    const hold = vi.fn()

    const step = reloadUnlessBraked(unauthorized, { hold, now: () => 50_000, page, reload, storage })

    expect(step).toBe('reload')
    expect(reload).toHaveBeenCalledOnce()
    expect(hold).not.toHaveBeenCalled()
    expect(readGatewayReloadedAt(storage)).toBe(50_000)
    expect(page.reloadStarted).toBe(true)
  })

  it('holds a reload within 30 seconds of the last one and shows the failed sign-in with the class', () => {
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const storage = memoryStorage()
    noteGatewayReload(50_000, storage)
    const reload = vi.fn()
    const hold = vi.fn()

    const step = reloadUnlessBraked(unauthorized, {
      hold,
      now: () => 65_000,
      page: { reloadStarted: false },
      reload,
      storage,
    })

    expect(step).toBe('hold')
    expect(reload).not.toHaveBeenCalled()
    expect(hold).toHaveBeenCalledWith({ outcome: 'unauthorized', state: 'signInFailed' })
    expect(warning).toHaveBeenCalledOnce()
  })

  it('reloads again once the pause has passed', () => {
    const storage = memoryStorage()
    noteGatewayReload(50_000, storage)
    const reload = vi.fn()

    const step = reloadUnlessBraked(unauthorized, {
      hold: vi.fn(),
      now: () => 80_000,
      page: { reloadStarted: false },
      reload,
      storage,
    })

    expect(step).toBe('reload')
    expect(reload).toHaveBeenCalledOnce()
    expect(readGatewayReloadedAt(storage)).toBe(80_000)
  })

  it('leaves every further 401 alone while its reload is underway', () => {
    // Several requests fail at once: one reload is enough, and the page must
    // not show the failed state in the moment before it leaves.
    const page = { reloadStarted: false }
    const reload = vi.fn()
    const hold = vi.fn()
    const options = { hold, now: () => 50_000, page, reload, storage: memoryStorage() }

    expect(reloadUnlessBraked(unauthorized, options)).toBe('reload')
    expect(reloadUnlessBraked(unauthorized, options)).toBe('underway')
    expect(reload).toHaveBeenCalledOnce()
    expect(hold).not.toHaveBeenCalled()
  })

  it('without storage reloads at most once per page life and says so', () => {
    const warning = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const page = { reloadStarted: false }
    const reload = vi.fn()
    const options = { hold: vi.fn(), now: () => 50_000, page, reload, storage: brokenStorage() }

    expect(reloadUnlessBraked(unauthorized, options)).toBe('reload')
    expect(reloadUnlessBraked(unauthorized, options)).toBe('underway')
    expect(reload).toHaveBeenCalledOnce()
    expect(warning).toHaveBeenCalledOnce()
  })

  it('reads a time it cannot use as none and never throws on a blocked storage', () => {
    const storage = memoryStorage()
    storage.setItem(GATEWAY_RELOADED_AT_KEY, 'soon')

    expect(readGatewayReloadedAt(storage)).toBeNull()
    expect(readGatewayReloadedAt(brokenStorage())).toBeNull()
    expect(readGatewayReloadedAt(null)).toBeNull()
    expect(noteGatewayReload(1, brokenStorage())).toBe(false)
    expect(noteGatewayReload(1, null)).toBe(false)
  })
})

describe('a session probe of the running page', () => {
  const anonymous: AuthSessionState = { projectNamespace: null, status: 'anonymous', user: null }
  const signedIn = (id: string): AuthSessionState => ({
    projectNamespace: null,
    status: 'authenticated',
    user: { displayName: null, email: null, id, role: 'user' },
  })

  it('reloads instead of adopting another user than the one shown last', () => {
    // The page still holds the shown account's runs, drafts and composer
    // text; adopting the new user would carry them over to that account.
    expect(probeSwitchesUser('user-a', signedIn('user-b'))).toBe(true)
  })

  it('adopts the same user, a first sign-in and an answer that signs nobody in', () => {
    expect(probeSwitchesUser('user-a', signedIn('user-a'))).toBe(false)
    expect(probeSwitchesUser(null, signedIn('user-b'))).toBe(false)
    expect(probeSwitchesUser('user-a', anonymous)).toBe(false)
    expect(probeSwitchesUser('user-a', { ...anonymous, status: 'unknown' })).toBe(false)
  })

  it('keeps the user shown last while the page lost its session in place', () => {
    // A refused start or an unreachable sign-in service ends the session in
    // place, and "Try again" probes again: the page still holds what it
    // showed of that user.
    let shown = lastShownUserId(null, { ...anonymous, status: 'unknown' })
    expect(shown).toBeNull()
    shown = lastShownUserId(shown, signedIn('user-a'))
    shown = lastShownUserId(shown, anonymous)
    expect(shown).toBe('user-a')
    expect(probeSwitchesUser(shown, signedIn('user-b'))).toBe(true)
    expect(lastShownUserId(shown, signedIn('user-b'))).toBe('user-b')
  })
})
