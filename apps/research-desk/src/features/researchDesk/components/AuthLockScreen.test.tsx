import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { type GatewayStateInfo, gatewayState } from '@/features/auth/authMode'
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

function gatewayMarkup(
  state: GatewayStateInfo | null,
  locale: 'de' | 'en' = 'de',
  isSubmitting = false,
): string {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale={locale}>
      <AuthLockScreen
        error={null}
        gatewayState={state}
        isSubmitting={isSubmitting}
        mode="gateway"
        onGatewayAction={() => undefined}
        onSubmit={() => undefined}
        onTokenChange={() => undefined}
        reduceMotion
        token=""
      />
    </LocaleProvider>,
  )
}

describe('AuthLockScreen in gateway mode', () => {
  const gateway = translations.de.authLock.gateway

  it('shows the state of the session start instead of any form', () => {
    const markup = gatewayMarkup({ state: 'notProvisioned', outcome: 'not_provisioned' })

    expect(markup).toContain(gateway.states.notProvisioned.title)
    expect(markup).toContain('not_provisioned')
    expect(markup).not.toContain('<form')
    expect(markup).not.toContain('id="inqtrix-auth-token"')
    expect(markup).not.toContain('id="inqtrix-auth-identifier"')
  })

  it('never falls back to the token form for a class this build does not know', () => {
    const markup = gatewayMarkup(gatewayState({ kind: 'rejected', outcome: 'a_new_class' }))

    expect(markup).toContain(gateway.states.signInFailed.title)
    expect(markup).toContain('a_new_class')
    expect(markup).not.toContain('id="inqtrix-auth-token"')
  })

  it('offers the action of the state, and no button where the state only informs', () => {
    expect(gatewayMarkup({ state: 'signInFailed', outcome: 'expired' })).toContain(gateway.actions.reload)
    expect(gatewayMarkup({ state: 'serviceUnavailable', outcome: 'keys_unavailable' })).toContain(gateway.actions.retry)
    expect(gatewayMarkup({ state: 'accountDisabled', outcome: 'account_disabled' })).not.toContain(
      'inqtrix-auth-action',
    )
    const signedOut = gatewayMarkup({ state: 'signedOut', outcome: null })
    expect(signedOut).toContain(gateway.actions.signInAgain)
    expect(signedOut).not.toContain(gateway.outcomeLabel)
  })

  it('speaks English as well', () => {
    const markup = gatewayMarkup({ state: 'emailUnverified', outcome: 'email_unverified' }, 'en')

    expect(markup).toContain(translations.en.authLock.gateway.states.emailUnverified.title)
  })
})

/** The opening tag of the element carrying *id*. */
function tagWithId(markup: string, id: string): string {
  const tag = markup.match(new RegExp(`<[a-z]+ [^>]*id="${id}"[^>]*>`))
  expect(tag, `an element with id ${id}`).not.toBeNull()
  return tag![0]
}

/** The markup of the element carrying *id*, up to the next action button. */
function stateBlock(markup: string): string {
  const start = markup.indexOf(tagWithId(markup, 'auth-lock-gateway-state'))
  const end = markup.indexOf('inqtrix-auth-action', start)
  return markup.slice(start, end === -1 ? undefined : end)
}

describe('AuthLockScreen announces a gateway state', () => {
  const gateway = translations.de.authLock.gateway

  it('announces a failed or refused start at once', () => {
    const markup = gatewayMarkup({ state: 'signInFailed', outcome: 'expired' })

    expect(tagWithId(markup, 'auth-lock-gateway-state')).toContain('role="alert"')
    expect(markup.split('role="alert"')).toHaveLength(2)
    expect(markup).not.toContain('role="status"')
  })

  it('announces a state with a plain next step politely', () => {
    const markup = gatewayMarkup({ state: 'signedOut', outcome: null })

    expect(tagWithId(markup, 'auth-lock-gateway-state')).toContain('role="status"')
    expect(markup).not.toContain('role="alert"')
  })

  it('describes the dialog by the state text, the class included', () => {
    const markup = gatewayMarkup({ state: 'signInFailed', outcome: 'expired' })
    const block = stateBlock(markup)

    expect(markup).toMatch(/<section [^>]*aria-describedby="auth-lock-gateway-state"/)
    expect(block).toContain(gateway.states.signInFailed.title)
    expect(block).toContain(gateway.states.signInFailed.description)
    expect(block).toContain('>expired<')
    expect(block).toContain('data-gateway-state="signInFailed"')
  })

  it('leaves the other modes without a state to describe them by', () => {
    expect(lockMarkup('sso')).not.toContain('aria-describedby="auth-lock-gateway-state"')
    expect(lockMarkup('sso')).not.toContain('id="auth-lock-gateway-state"')
  })

  it('shows that "Try again" runs and takes no second click meanwhile', () => {
    const markup = gatewayMarkup({ state: 'serviceUnavailable', outcome: 'keys_unavailable' }, 'de', true)
    const button = markup.match(/<button [^>]*inqtrix-auth-action[^>]*>.*?<\/button>/)?.[0] ?? ''

    expect(button).toContain('disabled=""')
    expect(button).toContain(translations.de.authLock.signingIn)
    expect(button).not.toContain(gateway.actions.retry)
  })

  it('keeps "Try again" ready while no probe runs, and a reload always ready', () => {
    const idle = gatewayMarkup({ state: 'serviceUnavailable', outcome: 'keys_unavailable' })
    const reload = gatewayMarkup({ state: 'signInFailed', outcome: 'expired' }, 'de', true)

    expect(idle).toContain(gateway.actions.retry)
    expect(idle).not.toContain('disabled=""')
    expect(reload).toContain(gateway.actions.reload)
    expect(reload).not.toContain('disabled=""')
  })

  it('says after a sign-out that the sign-in at the gateway lives on', () => {
    // On a shared computer the next person would otherwise sign in again as
    // the previous one, without a password.
    expect(gatewayMarkup({ state: 'signedOut', outcome: null })).toContain(
      'Ihre Anmeldung am Gateway endet damit nicht; solange sie besteht, meldet „Erneut anmelden“ Sie ohne Passwort an.',
    )
    expect(gatewayMarkup({ state: 'signedOut', outcome: null }, 'en')).toContain(
      'Your sign-in at the gateway does not end with this; while it lasts, &quot;Sign in again&quot; signs you in without a password.',
    )
  })

  it('says after a sign-out that went on to the gateway that it went on there, not that the sign-in lives on', () => {
    const de = gatewayMarkup({ state: 'signedOutAtGateway', outcome: null })
    const en = gatewayMarkup({ state: 'signedOutAtGateway', outcome: null }, 'en')

    expect(de).toContain('die Abmeldung wurde am Gateway fortgesetzt')
    expect(de).not.toContain('endet damit nicht')
    expect(de).toContain(gateway.actions.signInAgain)
    expect(en).toContain('the sign-out continued at the gateway')
    expect(en).not.toContain('does not end with this')
    expect(en).toContain(translations.en.authLock.gateway.actions.signInAgain)
  })
})
