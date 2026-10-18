import { translations, type Locale, type TranslationDictionary } from '@/i18n/translations'

/**
 * A failed request in the reader's words.
 *
 * The server writes its errors in English for operators; the interface
 * shows a sentence chosen by the error's type, else by its HTTP status
 * class, else a general one. The server's own sentence never reaches the
 * reader — it stays on the error (`serverMessage`) for diagnosis.
 *
 * The client builds its errors outside React, so the reader's language is
 * held here; the locale provider sets it while it renders, before any
 * request it triggers can fail.
 */
let readerLocale: Locale = 'en'
const reportedFallbacks = new Set<string>()

export function setRequestErrorLocale(locale: Locale): void {
  readerLocale = locale
}

export type RequestErrorFacts = {
  /** HTTP status; absent for a lost connection and for errors carried as data. */
  status?: number
  /** The server's error type (`error.type`), or a client one such as `network_error`. */
  type?: string | null
}

export function requestErrorText(
  facts: RequestErrorFacts,
  t: TranslationDictionary = translations[readerLocale],
): string {
  const texts = t.requestErrors
  const typed = facts.type
    ? (texts.types as Record<string, string | undefined>)[facts.type]
    : undefined
  if (typed) return typed
  const byStatus = statusText(facts.status, texts.status)
  if (byStatus) return byStatus
  // Neither the type nor the status names the failure: the reader still
  // sees that it failed, and the console says once what was not named.
  const key = `${facts.type ?? ''}:${facts.status ?? ''}`
  if (!reportedFallbacks.has(key)) {
    reportedFallbacks.add(key)
    console.warn('Request error without a reader text.', facts)
  }
  return facts.status
    ? texts.genericWithStatus.replace('{status}', String(facts.status))
    : texts.generic
}

function statusText(
  status: number | undefined,
  texts: TranslationDictionary['requestErrors']['status'],
): string | null {
  if (status === undefined) return null
  if (status === 400) return texts.badRequest
  if (status === 401) return texts.unauthorized
  if (status === 403) return texts.forbidden
  if (status === 404) return texts.notFound
  if (status === 409) return texts.conflict
  if (status === 413) return texts.tooLarge
  if (status === 422) return texts.invalid
  if (status === 429) return texts.rateLimited
  if (status === 502 || status === 503 || status === 504) return texts.unavailable
  if (status >= 500 && status < 600) return texts.serverError
  return null
}

/** The reader's dictionary, for code outside React that words a failure. */
export function readerTexts(): TranslationDictionary {
  return translations[readerLocale]
}

/** A failure the client detects itself, in the reader's words. */
export function readerError(key: keyof TranslationDictionary['clientErrors']): Error {
  return new Error(readerTexts().clientErrors[key])
}

/** The reader's text of a server error carried as data — the `error` of an
 * operation or a job — or null when there is none. A type without a reader
 * text reads as the place's own `fallback` (a failed deletion says that the
 * deletion failed), and the console names the type once. */
export function carriedErrorText(
  error: { message?: string | null; type?: string | null } | null | undefined,
  fallback: string = readerTexts().requestErrors.generic,
): string | null {
  if (!error) return null
  const typed = error.type
    ? (readerTexts().requestErrors.types as Record<string, string | undefined>)[error.type]
    : undefined
  if (typed) return typed
  const key = `carried:${error.type ?? ''}`
  if (!reportedFallbacks.has(key)) {
    reportedFallbacks.add(key)
    console.warn('Carried error without a reader text.', error)
  }
  return fallback
}

/** The reader's text of any failure: an `Error` already carries it (request
 * errors are built in the reader's words); anything else thrown is not a
 * sentence for the reader, so it reads as the general failure and the
 * console keeps the value. */
export function messageFromError(error: unknown): string {
  if (error instanceof Error) return error.message
  console.warn('A failure without an Error object reached the interface.', error)
  return translations[readerLocale].requestErrors.generic
}
