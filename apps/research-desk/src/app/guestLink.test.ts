/**
 * A guest-link visitor has no account, so no account preference will ever
 * replace the first-render locale: the browser's language is the signal such
 * a visitor brings. Account pages keep the account-default rule.
 */
import { afterEach, describe, expect, it, vi } from 'vitest'

import { appFirstRenderLocale, guestLinkTokenFromPath } from './guestLink'

const KEY = 'inqtrix.researchDesk.locale'

function browser(languages: readonly string[], stored: string | null = null) {
  vi.stubGlobal('navigator', { language: languages[0] ?? '', languages })
  vi.stubGlobal('localStorage', {
    getItem: (key: string) => (key === KEY ? stored : null),
    setItem: () => undefined,
  })
}

describe('first-render locale by page', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('paints a guest link in German for a browser that asks for German', () => {
    browser(['de-DE', 'de', 'en-US'])
    expect(appFirstRenderLocale('/s/egl1.token')).toBe('de')
  })

  it('takes the first supported browser language for a guest link', () => {
    browser(['fr-FR', 'de-AT', 'en'])
    expect(appFirstRenderLocale('/s/egl1.token')).toBe('de')
    browser(['fr-FR', 'it'])
    expect(appFirstRenderLocale('/s/egl1.token')).toBe('en')
  })

  it('lets a language chosen on this device win on a guest link', () => {
    browser(['de-DE'], 'en')
    expect(appFirstRenderLocale('/s/egl1.token')).toBe('en')
  })

  it('keeps the account default for every page that is not a guest link', () => {
    browser(['de-DE'])
    expect(appFirstRenderLocale('/')).toBe('en')
    expect(appFirstRenderLocale('/s/')).toBe('en')
    expect(appFirstRenderLocale('/s/egl1.token/extra')).toBe('en')
  })

  it('recognizes only a single-segment guest path', () => {
    expect(guestLinkTokenFromPath('/s/egl1.a%2Fb')).toBe('egl1.a/b')
    expect(guestLinkTokenFromPath('/s/egl1.token/')).toBe('egl1.token')
    expect(guestLinkTokenFromPath('/s/%E0%A4%A')).toBeNull()
    expect(guestLinkTokenFromPath('/editor')).toBeNull()
  })
})
