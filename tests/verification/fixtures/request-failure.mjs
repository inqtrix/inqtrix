/**
 * A failed fixture request in words that carry no credential.
 *
 * Playwright's request errors append the call log with every header of the
 * request -- the session cookie and the CSRF token included. Only the kind of
 * failure leaves a fixture. The original error is not attached as a cause,
 * because printing an error prints its cause as well.
 */

const NETWORK_CODE = /\b(EAI_AGAIN|ECONNREFUSED|ECONNRESET|ENOTFOUND|EPIPE|ETIMEDOUT)\b/

/** How a request failed, without any of the request's own text. */
export function requestFailureKind(error) {
  const message = String(error?.message ?? '')
  if (/\bTimeout \d+ms exceeded\b/.test(message)) return 'timed out'
  const code = NETWORK_CODE.exec(message)?.[1]
  if (code) return `failed (${code})`
  // A name is a type, never request text; the plain `Error` says nothing.
  const name = typeof error?.name === 'string' && error.name !== 'Error' ? error.name : null
  return name ? `failed (${name})` : 'failed'
}
