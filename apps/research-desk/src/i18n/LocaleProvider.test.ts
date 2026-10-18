/**
 * The first-render locale must agree with the server's account default.
 *
 * The durable setting lives in `account_preferences.locale` (server default
 * `en`) and reaches the app through `setLocale`, which also writes it to
 * localStorage. `readStoredLocale` only answers "what do we paint before
 * that arrives?" — if its answer disagrees with the server default, a user
 * whose preference is English sees a flash of German on every fresh browser,
 * and a new international user reads German until the project load lands.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { readStoredLocale } from './LocaleProvider'

const KEY = 'inqtrix.researchDesk.locale'

function withStorage(value: string | null) {
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => (key === KEY ? value : null),
    setItem: () => undefined,
  })
}

describe('readStoredLocale', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('falls back to English, matching the server account default', () => {
    withStorage(null)
    expect(readStoredLocale()).toBe('en')
  })

  it('falls back to English when there is no storage at all', () => {
    // The server-render / node path: every component test renders here, so
    // this branch decides what a test sees when it states no locale.
    vi.stubGlobal('localStorage', undefined)
    expect(readStoredLocale()).toBe('en')
  })

  it('a stored choice always wins over the fallback', () => {
    withStorage('de')
    expect(readStoredLocale()).toBe('de')
    withStorage('en')
    expect(readStoredLocale()).toBe('en')
  })

  it('an unreadable stored value resolves to English, never to German', () => {
    withStorage('klingon')
    expect(readStoredLocale()).toBe('en')
  })
})
