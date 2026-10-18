import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { LocaleProvider } from '@/i18n/LocaleProvider'
import { translations } from '@/i18n/translations'
import { AuthLockScreen } from './AuthLockScreen'

function lockMarkup(mode: 'local' | 'sso' | 'apikey' = 'local'): string {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale="de">
      <AuthLockScreen
        error={null}
        identifier=""
        isSubmitting={false}
        mode={mode}
        onCredentialSubmit={() => undefined}
        onIdentifierChange={() => undefined}
        onPasswordChange={() => undefined}
        onSubmit={() => undefined}
        onTokenChange={() => undefined}
        password=""
        reduceMotion
        token=""
      />
    </LocaleProvider>,
  )
}

describe('AuthLockScreen', () => {
  it('announces itself as a modal dialog', () => {
    // role alone is not enough: without aria-modal, assistive technology
    // keeps offering the covered application behind the lock screen.
    const markup = lockMarkup()

    expect(markup).toContain('role="dialog"')
    expect(markup).toContain('aria-modal="true"')
    expect(markup).toContain('aria-labelledby="auth-lock-title"')
  })

  it('labels the dialog with a rendered element', () => {
    // aria-labelledby pointing at nothing announces an unnamed dialog.
    expect(lockMarkup()).toContain('id="auth-lock-title"')
  })
})

describe('AuthLockScreen credential form', () => {
  // The existing assertions above all live OUTSIDE the isCredentialMode
  // branch: if the form stopped rendering, they would all stay green. These
  // three pin the form itself, so removing a password mode cannot take the
  // local login with it unnoticed.
  it('renders the identifier and password fields in local mode', () => {
    const markup = lockMarkup()

    expect(markup).toContain('id="inqtrix-auth-identifier"')
    expect(markup).toContain('id="inqtrix-auth-password"')
  })

  it('asks for an email address, not a username', () => {
    // local authenticates on an email. A username label or a plain-text field
    // here means the identifier ternary fell through to a branch that does
    // not apply to local accounts.
    const markup = lockMarkup()

    expect(markup).toContain('type="email"')
    expect(markup).toContain('E-Mail-Adresse')
    expect(markup).toContain('name@beispiel.de')
    // The password manager fills an email field differently from a username
    // one, so the hint is user-visible and belongs in the pin.
    expect(markup).toContain('autoComplete="email"')
  })

  it('labels the identifier field with the rendered label element', () => {
    const markup = lockMarkup()

    expect(markup).toContain('for="inqtrix-auth-identifier"')
  })
})

describe('AuthLockScreen footer', () => {
  // Reversing part of an earlier removal on purpose: the
  // usage notices belong here after all — this is the one surface a new
  // user sees BEFORE using an AI system. What stays removed are the source
  // / documentation / license links and the copyright line; they live in
  // Settings → Licensing and would turn a sign-in screen into a landing
  // page.
  it.each(['local', 'sso', 'apikey'] as const)(
    'renders the usage notices but no footer links in %s mode',
    (mode) => {
      const markup = lockMarkup(mode)

      expect(markup).toContain('Nutzungshinweise')
      expect(markup).toContain('Inqtrix ist ein KI-System.')
      // Acknowledgement, NOT consent: Apache-2.0 grants rights and is not
      // agreed to by signing in, and the software makes no claim about
      // terms its operator may or may not have.
      expect(markup).toContain('bestätigen Sie, dass Sie diese Hinweise gelesen haben')

      expect(markup).not.toContain('Copyright (c)')
      expect(markup).not.toContain(`href="${translations.de.authLock.licenseUrl}"`)
      expect(markup).not.toContain(`href="${translations.de.authLock.documentationUrl}"`)
      expect(markup).not.toContain('>Quellcode<')
    },
  )

  it('does not call the software "under active development"', () => {
    // The phrase adds nothing — experimental is the point, the
    // development state is not.
    const markup = lockMarkup()
    expect(markup).toContain('Experimentelle Software.')
    expect(markup).not.toContain('in aktiver Entwicklung')
  })

  it('keeps the open-source badge in the header', () => {
    expect(lockMarkup()).toContain('Open Source')
  })
})
