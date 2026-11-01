import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { LocaleProvider } from '@/i18n/LocaleProvider'

import { GuestAccessEnded, GuestLandingError } from './GuestEditorPage'

function endedMarkup(locale: 'de' | 'en', unsavedChanges: boolean): string {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale={locale}>
      <GuestAccessEnded onReopen={() => undefined} unsavedChanges={unsavedChanges} />
    </LocaleProvider>,
  )
}

function count(markup: string, needle: string): number {
  return markup.split(needle).length - 1
}

describe('the notice a guest sees once access through the link has ended', () => {
  it('is one notice with a way back to the link, and nothing of the document', () => {
    const markup = endedMarkup('de', false)

    expect(count(markup, 'role="alert"')).toBe(1)
    expect(markup).toContain('Kein Zugriff mehr')
    // Revoked, expired, a new password or a deleted document all end here.
    expect(markup).toContain('widerrufen')
    expect(markup).toContain('gelöscht')
    expect(markup).toContain('Link erneut öffnen')
    expect(markup).not.toContain('verloren')
    expect(markup).not.toContain('ProseMirror')
  })

  it('names the latest changes as lost when they were not saved', () => {
    expect(endedMarkup('de', true)).toContain(
      'Ihre letzten Änderungen konnten nicht mehr gespeichert werden und sind verloren.',
    )
  })

  it('speaks the reader\'s language', () => {
    const markup = endedMarkup('en', true)

    expect(markup).toContain('You no longer have access')
    expect(markup).toContain('Your latest changes could not be saved and are lost.')
    expect(markup).toContain('Open the link again')
  })
})

describe('the notice a guest sees while the link is paused', () => {
  it('says the link is currently unavailable, not that access ended', () => {
    const markup = renderToStaticMarkup(
      <LocaleProvider initialLocale="de">
        <GuestAccessEnded onReopen={() => undefined} unavailable unsavedChanges={false} />
      </LocaleProvider>,
    )

    expect(markup).toContain('Link derzeit nicht verfügbar')
    expect(markup).toContain('kann später wieder genutzt werden')
    expect(markup).not.toContain('Kein Zugriff mehr')
    expect(markup).toContain('Link erneut öffnen')
  })

  it('promises nothing about the link staying valid', () => {
    // A link also pauses while its owner's account is being erased, and
    // then it never works again.
    const markup = (locale: 'de' | 'en') => renderToStaticMarkup(
      <LocaleProvider initialLocale={locale}>
        <GuestAccessEnded onReopen={() => undefined} unavailable unsavedChanges={false} />
      </LocaleProvider>,
    )

    expect(markup('de')).not.toContain('Er gilt weiter')
    expect(markup('en')).not.toContain('stays valid')
    expect(markup('en')).toContain('Link currently unavailable')
  })
})

function landingMarkup(failure: 'invalid' | 'paused' | 'unavailable', locale: 'de' | 'en' = 'de'): string {
  return renderToStaticMarkup(
    <LocaleProvider initialLocale={locale}>
      <GuestLandingError failure={failure} />
    </LocaleProvider>,
  )
}

describe('the landing page of a link that cannot be opened', () => {
  it('calls the link invalid when the service does not know it', () => {
    expect(landingMarkup('invalid')).toContain('ungültig, abgelaufen oder wurde widerrufen')
  })

  it('says a paused link is currently unavailable', () => {
    const markup = landingMarkup('paused')

    expect(markup).toContain('Link derzeit nicht verfügbar')
    expect(markup).not.toContain('ungültig')
  })

  it('calls the link temporarily unavailable when the service could not answer, never invalid', () => {
    const de = landingMarkup('unavailable')
    const en = landingMarkup('unavailable', 'en')

    expect(de).toContain('Der Link kann gerade nicht geprüft werden. Bitte versuchen Sie es später erneut.')
    expect(de).not.toContain('ungültig')
    expect(en).toContain('The link cannot be checked right now. Please try again later.')
    expect(en).not.toContain('invalid')
  })
})
