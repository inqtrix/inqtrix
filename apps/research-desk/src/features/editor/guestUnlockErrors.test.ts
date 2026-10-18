import { describe, expect, it } from 'vitest'

import {
  guestUnlockErrorKey,
  type GuestUnlockErrorKey,
} from './guestUnlockErrors'

/** An error exactly as the client throws it for an HTTP failure. */
function httpError(status: number): Error {
  return Object.assign(new Error(`HTTP ${status}`), { status })
}

describe('a failed guest unlock names the cause it was actually given', () => {
  it.each<[number, GuestUnlockErrorKey]>([
    [403, 'unlockBlocked'],
    [429, 'unlockRateLimited'],
    [401, 'unlockFailed'],
    [400, 'unlockFailed'],
    [404, 'invalid'],
  ])('maps %i to its own sentence', (status, key) => {
    expect(guestUnlockErrorKey(httpError(status))).toBe(key)
  })

  it('does not blame the password for a refused origin', () => {
    // The regression: the server answers 403 `Disallowed origin.` -- the
    // deployment's address allowlist, the password never examined -- so
    // the screen must not say to check it.
    expect(guestUnlockErrorKey(httpError(403))).not.toBe('unlockFailed')
  })

  it('does not claim a link is wrong when the server never answered', () => {
    expect(guestUnlockErrorKey(new TypeError('Failed to fetch')))
      .toBe('unlockUnavailable')
    expect(guestUnlockErrorKey(httpError(502))).toBe('unlockUnavailable')
    expect(guestUnlockErrorKey(undefined)).toBe('unlockUnavailable')
  })

  it('separates the five outcomes into four distinct sentences', () => {
    const keys = new Set(
      [400, 401, 403, 404, 429].map(s => guestUnlockErrorKey(httpError(s))),
    )
    expect(keys.size).toBe(4)
  })
})
