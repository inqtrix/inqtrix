import { describe, expect, it } from 'vitest'

import {
  GATEWAY_ACTIONS,
  GATEWAY_STATE_ROLES,
  type GatewayAction,
  type GatewayAnswer,
  type GatewayState,
  automaticReloadStep,
  deskAuthMode,
  gatewayAnswerFromError,
  gatewaySessionAnswer,
  gatewayState,
  heldReloadState,
  isCookieSessionMode,
  isGatewayMode,
  isKnownAuthMode,
  isPasswordMode,
} from './authMode'

function requestError(name: string, status?: number): Error {
  const error = new Error('message') as Error & { status?: number }
  error.name = name
  if (status !== undefined) error.status = status
  return error
}

// These predicates gate the whole cookie-session surface: the SPA only probes
// /api/auth/session, and only offers sharing, quota and admin, for a mode that
// answers true here. A mode that silently drops out of the set loses those
// features without an error anywhere -- so each mode is pinned individually
// rather than through a loop over the union.
describe('isCookieSessionMode', () => {
  it('accepts every mode whose identity rides a server-side cookie session', () => {
    expect(isCookieSessionMode('local')).toBe(true)
    expect(isCookieSessionMode('oidc')).toBe(true)
  })

  it('rejects the modes that carry no session', () => {
    expect(isCookieSessionMode('none')).toBe(false)
    expect(isCookieSessionMode('apikey')).toBe(false)
  })

  it('rejects an unknown mode instead of defaulting to a session', () => {
    // The server may answer before the SPA knows the mode. Guessing "true"
    // here would render sharing and admin against a principal that does not
    // exist yet.
    expect(isCookieSessionMode(undefined)).toBe(false)
    expect(isCookieSessionMode(null)).toBe(false)
  })
})

describe('isPasswordMode', () => {
  it('accepts the mode that authenticates through the credential form', () => {
    expect(isPasswordMode('local')).toBe(true)
  })

  it('rejects modes that do not render the credential form', () => {
    expect(isPasswordMode('oidc')).toBe(false)
    expect(isPasswordMode('none')).toBe(false)
    expect(isPasswordMode('apikey')).toBe(false)
    expect(isPasswordMode(undefined)).toBe(false)
    expect(isPasswordMode(null)).toBe(false)
  })
})

describe('the auth mode of the desk', () => {
  it('takes the mode the server reports, whatever this browser stored', () => {
    expect(deskAuthMode({ auth_mode: 'local' }, { demo: false, gatewaySignedOut: true })).toBe('local')
    expect(deskAuthMode({ auth_mode: 'gateway' }, { demo: false, gatewaySignedOut: false })).toBe('gateway')
    expect(deskAuthMode({ auth_required: true }, { demo: false, gatewaySignedOut: false })).toBe('apikey')
  })

  it('runs open without a server answer', () => {
    expect(deskAuthMode(null, { demo: false, gatewaySignedOut: false })).toBe('none')
    expect(deskAuthMode(undefined, { demo: false, gatewaySignedOut: false })).toBe('none')
  })

  it('stays in gateway mode without a server answer in a browser that signed out there', () => {
    // The gateway hides the server once the person signed out at it; the
    // tab must show the signed-out page, never an open desk.
    expect(deskAuthMode(null, { demo: false, gatewaySignedOut: true })).toBe('gateway')
  })

  it('runs the demo open in any case', () => {
    expect(deskAuthMode({ auth_mode: 'gateway' }, { demo: true, gatewaySignedOut: true })).toBe('none')
    expect(deskAuthMode(null, { demo: true, gatewaySignedOut: true })).toBe('none')
  })
})

describe('isKnownAuthMode', () => {
  it('accepts every mode this build renders', () => {
    for (const mode of ['none', 'apikey', 'oidc', 'local']) {
      expect(isKnownAuthMode(mode)).toBe(true)
    }
  })

  it('rejects a mode the server may report but this build cannot render', () => {
    // A frontend and backend on different versions is a documented topology
    // (split-origin deployment), so this is reachable, not hypothetical.
    expect(isKnownAuthMode('ldap')).toBe(false)
    expect(isKnownAuthMode('saml')).toBe(false)
    expect(isKnownAuthMode(undefined)).toBe(false)
    expect(isKnownAuthMode(null)).toBe(false)
  })
})

describe('the gateway mode', () => {
  it('is a known cookie-session mode without a password form', () => {
    // Without it in the set, the SPA would lock a gateway deployment behind
    // the token form, which cannot unlock it.
    expect(isKnownAuthMode('gateway')).toBe(true)
    expect(isCookieSessionMode('gateway')).toBe(true)
    expect(isPasswordMode('gateway')).toBe(false)
    expect(isGatewayMode('gateway')).toBe(true)
    expect(isGatewayMode('oidc')).toBe(false)
  })
})

// One row per kind of answer of the session start: each shows exactly its
// state and offers exactly its action.
const GATEWAY_TABLE: ReadonlyArray<[string, GatewayAnswer, GatewayState, GatewayAction]> = [
  ['no assertion (S1)', { kind: 'unauthenticated', reason: 'assertion_missing' }, 'noGatewaySignIn', 'reload'],
  ['an answer that is not from Inqtrix', { kind: 'foreign' }, 'renewGatewaySignIn', 'reload'],
  ['a logout without a gateway sign-out URL', { kind: 'signedOut', atGateway: false }, 'signedOut', 'signInAgain'],
  ['a logout that went on to the gateway sign-out', { kind: 'signedOut', atGateway: true }, 'signedOutAtGateway', 'signInAgain'],
  ['a disabled account', { kind: 'rejected', outcome: 'account_disabled' }, 'accountDisabled', null],
  ['a person the identity system has not provisioned', { kind: 'rejected', outcome: 'not_provisioned' }, 'notProvisioned', null],
  ['a refused registration', { kind: 'rejected', outcome: 'registration_denied' }, 'accessNotGranted', null],
  ['a refused admission', { kind: 'rejected', outcome: 'admission_denied' }, 'accessNotGranted', null],
  ['an unconfirmed email', { kind: 'rejected', outcome: 'email_unverified' }, 'emailUnverified', null],
  ['an expired assertion', { kind: 'rejected', outcome: 'expired' }, 'signInFailed', 'reload'],
  ['a proof from an untrusted peer', { kind: 'rejected', outcome: 'untrusted_source' }, 'signInFailed', 'reload'],
  ['an email as subject', { kind: 'rejected', outcome: 'subject_is_email' }, 'signInFailed', 'reload'],
  ['unreachable keys', { kind: 'rejected', outcome: 'keys_unavailable' }, 'serviceUnavailable', 'retry'],
  ['a failing resolver of its own', { kind: 'rejected', outcome: 'resolver_failed' }, 'serviceUnavailable', 'retry'],
  ['a class this build does not know', { kind: 'rejected', outcome: 'a_new_class' }, 'signInFailed', 'reload'],
  ['an unauthenticated answer with an unknown reason', { kind: 'unauthenticated', reason: 'other' }, 'signInFailed', 'reload'],
]

describe('gatewayState', () => {
  it.each(GATEWAY_TABLE)('maps %s to its one state and action', (_case, answer, state, action) => {
    const info = gatewayState(answer)

    expect(info.state).toBe(state)
    expect(GATEWAY_ACTIONS[info.state]).toBe(action)
  })

  it('keeps the class as the identifier the page shows', () => {
    expect(gatewayState({ kind: 'rejected', outcome: 'expired' }).outcome).toBe('expired')
    expect(gatewayState({ kind: 'unauthenticated', reason: 'assertion_missing' }).outcome).toBe('assertion_missing')
    expect(gatewayState({ kind: 'foreign' }).outcome).toBeNull()
  })
})

describe('gatewaySessionAnswer', () => {
  it('reads the reason of a session start that signed nobody in', () => {
    expect(gatewaySessionAnswer({ authenticated: false, reason: 'assertion_missing' })).toEqual({
      kind: 'unauthenticated',
      reason: 'assertion_missing',
    })
  })

  it('has no answer for a signed-in user', () => {
    expect(gatewaySessionAnswer({ authenticated: true, user: { id: 'u' } })).toBeNull()
  })

  it('reads a payload without the session shape as an answer from in front of Inqtrix', () => {
    for (const payload of [null, 'text', {}, { authenticated: 'yes' }]) {
      expect(gatewaySessionAnswer(payload)).toEqual({ kind: 'foreign' })
    }
  })
})

describe('gatewayAnswerFromError', () => {
  it('reads the class of a typed Inqtrix error', () => {
    expect(gatewayAnswerFromError(requestError('not_provisioned', 403))).toEqual({
      kind: 'rejected',
      outcome: 'not_provisioned',
    })
  })

  it('reads a status without a typed body, a network error and a body that is no JSON as foreign', () => {
    expect(gatewayAnswerFromError(requestError('InqtrixRequestError', 401))).toEqual({ kind: 'foreign' })
    expect(gatewayAnswerFromError(requestError('network_error'))).toEqual({ kind: 'foreign' })
    expect(gatewayAnswerFromError(new SyntaxError('Unexpected token <'))).toEqual({ kind: 'foreign' })
    expect(gatewayAnswerFromError('boom')).toEqual({ kind: 'foreign' })
  })

  it('takes a class only in the form Inqtrix names its classes, and reads any other type as foreign', () => {
    // A gateway answering in its own error format names its own type; shown
    // as the class of a failed sign-in, it would send the administration
    // after an outcome Inqtrix never reported.
    for (const type of ['Unauthorized', 'invalid-token', 'not provisioned', '1st_class', `a${'b'.repeat(64)}`]) {
      expect(gatewayAnswerFromError(requestError(type, 401))).toEqual({ kind: 'foreign' })
    }
    const longest = `a${'b'.repeat(63)}`
    expect(gatewayAnswerFromError(requestError(longest, 401))).toEqual({ kind: 'rejected', outcome: longest })
    expect(gatewayAnswerFromError(requestError('principal_mismatch', 401))).toEqual({
      kind: 'rejected',
      outcome: 'principal_mismatch',
    })
  })
})

// A failure or a refusal is announced at once; a state with a plain next
// step is announced politely. Every state has exactly one of the two.
const ANNOUNCED: ReadonlyArray<[GatewayState, 'alert' | 'status']> = [
  ['noGatewaySignIn', 'status'],
  ['renewGatewaySignIn', 'status'],
  ['signedOut', 'status'],
  ['signedOutAtGateway', 'status'],
  ['accountDisabled', 'alert'],
  ['notProvisioned', 'alert'],
  ['accessNotGranted', 'alert'],
  ['emailUnverified', 'alert'],
  ['signInFailed', 'alert'],
  ['serviceUnavailable', 'alert'],
]

describe('how the sign-in surface announces a gateway state', () => {
  it.each(ANNOUNCED)('announces %s as %s', (state, role) => {
    expect(GATEWAY_STATE_ROLES[state]).toBe(role)
  })

  it('knows no state the table leaves out', () => {
    expect(Object.keys(GATEWAY_STATE_ROLES).sort()).toEqual(ANNOUNCED.map(([state]) => state).sort())
  })
})

describe('the brake on automatic reloads after a 401 in gateway mode', () => {
  const now = 1_000_000

  it('reloads when no automatic reload of this tab is known', () => {
    expect(automaticReloadStep({ lastReloadAt: null, now, reloadStarted: false })).toBe('reload')
  })

  it('holds the next automatic reload back for 30 seconds after the last one', () => {
    for (const elapsed of [0, 1, 15_000, 29_999]) {
      expect(automaticReloadStep({ lastReloadAt: now - elapsed, now, reloadStarted: false })).toBe('hold')
    }
    expect(automaticReloadStep({ lastReloadAt: now - 30_000, now, reloadStarted: false })).toBe('reload')
    expect(automaticReloadStep({ lastReloadAt: now - 600_000, now, reloadStarted: false })).toBe('reload')
  })

  it('reloads after the clock moved back instead of holding until it catches up', () => {
    expect(automaticReloadStep({ lastReloadAt: now + 60_000, now, reloadStarted: false })).toBe('reload')
  })

  it('starts at most one automatic reload per page life', () => {
    expect(automaticReloadStep({ lastReloadAt: null, now, reloadStarted: true })).toBe('underway')
    expect(automaticReloadStep({ lastReloadAt: now - 600_000, now, reloadStarted: true })).toBe('underway')
  })
})

describe('the state of a held reload', () => {
  it('is a failed gateway sign-in with the class of the 401', () => {
    expect(heldReloadState(requestError('unauthorized', 401))).toEqual({
      outcome: 'unauthorized',
      state: 'signInFailed',
    })
    // The session start succeeded; what failed is the request after it, so
    // even a class with a state of its own reads as the failed sign-in.
    expect(heldReloadState(requestError('assertion_missing', 401))).toEqual({
      outcome: 'assertion_missing',
      state: 'signInFailed',
    })
  })

  it('names no class for a 401 that did not come from Inqtrix', () => {
    expect(heldReloadState(requestError('InqtrixRequestError', 401))).toEqual({
      outcome: null,
      state: 'signInFailed',
    })
    expect(heldReloadState(requestError('Unauthorized', 401))).toEqual({ outcome: null, state: 'signInFailed' })
  })
})
