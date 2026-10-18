import { describe, expect, it } from 'vitest'

import {
  isCookieSessionMode,
  isKnownAuthMode,
  isPasswordMode,
} from './authMode'

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
