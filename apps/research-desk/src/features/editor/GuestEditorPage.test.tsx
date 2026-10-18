import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'

import { LocaleProvider } from '@/i18n/LocaleProvider'

import { GuestAccessEnded } from './GuestEditorPage'

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
