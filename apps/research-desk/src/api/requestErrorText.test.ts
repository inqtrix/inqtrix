import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { viewerErrorText } from '@/features/knowledge/viewerErrorText'
import { LocaleProvider } from '@/i18n/LocaleProvider'
import { translations } from '@/i18n/translations'
import { fetchServerFileInfo } from './inqtrixClient'
import {
  carriedErrorText,
  messageFromError,
  readerError,
  requestErrorText,
  setRequestErrorLocale,
} from './requestErrorText'

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    headers: { 'Content-Type': 'application/json' },
    status,
  })
}

// A request error reads in the reader's words. The server writes its errors
// in English for operators; what the interface shows comes from the type and
// the status, and the server's own sentence stays on the error for diagnosis.
describe('a failed request in the reader\'s words', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('names a missing item by its status, keeping the server sentence for diagnosis', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      { error: { message: 'Session not found', type: 'not_found' } },
      404,
    )))

    await expect(
      fetchServerFileInfo('file-1', { baseUrl: 'http://api.test' }),
    ).rejects.toMatchObject({
      message: 'What was requested could not be found.',
      name: 'not_found',
      serverMessage: 'Session not found',
      status: 404,
    })
  })

  it('names an unavailable service when a gateway answers without a body', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 502 })))

    await expect(
      fetchServerFileInfo('file-1', { baseUrl: 'http://api.test' }),
    ).rejects.toMatchObject({
      message: 'The service is unavailable right now. Please try again shortly.',
      status: 502,
    })
  })

  it('names a lost connection, and leaves the status unset as before', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('Failed to fetch')
    }))

    const failure = await fetchServerFileInfo('file-1', { baseUrl: 'http://api.test' })
      .then(() => null, (error: unknown) => error as Error & { status?: number })
    expect(failure).toMatchObject({
      message: 'No connection to the server. Please check the network connection.',
      name: 'network_error',
    })
    // A connection retry keys on the missing status (`status === undefined`).
    expect(failure?.status).toBeUndefined()
  })

  it('lets a cancelled request end as a cancellation', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new DOMException('The operation was aborted.', 'AbortError')
    }))

    await expect(
      fetchServerFileInfo('file-1', { baseUrl: 'http://api.test' }),
    ).rejects.toMatchObject({ name: 'AbortError' })
  })
})

describe('an error inside a sentence', () => {
  it('ends the sentence once when the error brings its own full stop', () => {
    const notice = translations.de.common.observationStopped(
      'Der Dienst ist gerade nicht erreichbar. Bitte gleich erneut versuchen.',
    )

    expect(notice).toContain('(Der Dienst ist gerade nicht erreichbar. Bitte gleich erneut versuchen)')
    expect(notice).not.toContain('.).')
  })
})

describe('the reader text of a failure', () => {
  afterEach(() => {
    setRequestErrorLocale('en')
    vi.restoreAllMocks()
  })

  it('reads a known type, else the status class, in both languages', () => {
    expect(requestErrorText({ status: 404, type: 'not_found' }, translations.de))
      .toBe('Das Gesuchte wurde nicht gefunden.')
    expect(requestErrorText({ type: 'network_error' }, translations.de))
      .toBe('Keine Verbindung zum Server. Bitte die Netzwerkverbindung prüfen.')
    expect(requestErrorText({ status: 503 }, translations.de))
      .toBe('Der Dienst ist gerade nicht erreichbar. Bitte gleich erneut versuchen.')
    expect(requestErrorText({ status: 500 }, translations.de))
      .toBe('Auf dem Server ist ein Fehler aufgetreten.')
    expect(requestErrorText({ status: 409, type: 'unmapped_conflict_type' }, translations.en))
      .toBe('This has changed in the meantime. Please reload and try again.')
  })

  it('says that it failed, with the status, when nothing names the failure, and tells the console once', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)

    expect(requestErrorText({ status: 418, type: 'teapot_without_text' }, translations.de))
      .toBe('Die Aktion ist fehlgeschlagen (HTTP 418).')
    expect(requestErrorText({ status: 418, type: 'teapot_without_text' }, translations.de))
      .toBe('Die Aktion ist fehlgeschlagen (HTTP 418).')
    expect(requestErrorText({ type: 'typeless_carrier_without_text' }, translations.en))
      .toBe('The action failed.')
    expect(warn).toHaveBeenCalledTimes(2)
  })

  it('takes the reader language from the locale provider while it renders', () => {
    renderToStaticMarkup(createElement(LocaleProvider, { children: null, initialLocale: 'de' }))
    expect(requestErrorText({ status: 404 })).toBe('Das Gesuchte wurde nicht gefunden.')
    expect(readerError('clipboardUnavailable').message).toBe('Zwischenablage nicht verfügbar')

    renderToStaticMarkup(createElement(LocaleProvider, { children: null, initialLocale: 'en' }))
    expect(requestErrorText({ status: 404 })).toBe('What was requested could not be found.')
    expect(readerError('clipboardUnavailable').message).toBe('Clipboard not available')
  })

  it('reads a server error carried as data by its type, else as the place\'s own failure', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    setRequestErrorLocale('de')

    expect(carriedErrorText({ message: 'upload checksum mismatch', type: 'upload_integrity_error' }, 'Upload fehlgeschlagen'))
      .toBe('Die Datei ist beim Hochladen beschädigt worden.')
    expect(carriedErrorText({ message: 'vector store said no', type: 'carried_type_without_text' }, 'Löschen fehlgeschlagen'))
      .toBe('Löschen fehlgeschlagen')
    expect(carriedErrorText(null, 'Löschen fehlgeschlagen')).toBeNull()
    expect(warn).toHaveBeenCalledTimes(1)
  })

  it('never shows a thrown value that is not an Error', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    setRequestErrorLocale('de')

    expect(messageFromError('raw thrown text')).toBe('Die Aktion ist fehlgeschlagen.')
    expect(messageFromError(new Error('Lesetext'))).toBe('Lesetext')
    expect(warn).toHaveBeenCalledTimes(1)
  })

  it('names a cited document that left its collection', () => {
    const gone = Object.assign(new Error('Das Gesuchte wurde nicht gefunden.'), { status: 404 })

    expect(viewerErrorText(gone, translations.de)).toBe('Dieses Dokument ist nicht mehr in der Sammlung.')
    expect(viewerErrorText(new Error('Keine Verbindung zum Server.'), translations.de))
      .toBe('Keine Verbindung zum Server.')
    expect(viewerErrorText('not an error', translations.de)).toBe('Dokument konnte nicht geladen werden.')
  })
})
