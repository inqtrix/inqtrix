/** Which sentence a failed guest unlock earns.
 *
 * The unlock endpoint distinguishes five outcomes and only ONE of them
 * is about the password: 401 for a wrong one, 403 when the request
 * origin is outside the deployment's allowlist, 404 for a link that is
 * gone, 429 for too many attempts, 400 for a malformed body. Answering
 * all of them with "check the link and password" sends the reader
 * hunting a credential the server never examined -- a 403
 * `Disallowed origin.` would reach the screen as "Passwort pruefen".
 */
import { hasHttpStatus } from '@/api/inqtrixClient'

export type GuestUnlockErrorKey =
  | 'invalid'
  | 'unlockBlocked'
  | 'unlockFailed'
  | 'unlockRateLimited'
  | 'unlockUnavailable'

/** Status codes the server answers with, each with its own sentence.
 * Anything else -- a network failure, a proxy, a 5xx -- is not a
 * statement about this link and must not be dressed up as one. */
const BY_STATUS: ReadonlyArray<readonly [number, GuestUnlockErrorKey]> = [
  [403, 'unlockBlocked'],
  [429, 'unlockRateLimited'],
  [401, 'unlockFailed'],
  [400, 'unlockFailed'],
  [404, 'invalid'],
]

export function guestUnlockErrorKey(cause: unknown): GuestUnlockErrorKey {
  for (const [status, key] of BY_STATUS) {
    if (hasHttpStatus(cause, status)) return key
  }
  return 'unlockUnavailable'
}
