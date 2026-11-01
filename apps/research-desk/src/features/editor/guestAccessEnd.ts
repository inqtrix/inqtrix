import type { InqtrixRequestError } from '@/api/inqtrixClient'

import { COLLABORATION_NOTICES } from './collaborationNotices'
import type { CollaborationDocumentHandle } from './useCollaborationDocument'

/**
 * What the guest page does with the collaboration state it sees.
 *
 * `ended`: the service has refused the guest for good (the session is gone
 * after a revocation, an expiry or a new password; or the link or the
 * document no longer exists), or `unavailable`: the link is paused while its
 * owner is not active and may work again. The page removes the document and
 * its comments and shows one notice with a way back to the link.
 *
 * `verify`: access changed while local changes were unconfirmed. The
 * controller then stops without asking the service whether access remains,
 * so the page asks once; a refused answer ends access.
 */
export type GuestAccessEnd =
  | { kind: 'continue' }
  | { kind: 'ended'; unavailable: boolean; unsavedChanges: boolean }
  | { kind: 'verify'; unsavedChanges: boolean }

export function guestAccessEnd(
  snapshot: Pick<
    CollaborationDocumentHandle,
    'connectionStatus' | 'durabilityStatus' | 'error' | 'hasUnconfirmedLocalChanges' | 'recoverability'
  >,
): GuestAccessEnd {
  // Read at the moment access ends, on every path: a lease renewal that
  // meets the ended session takes the 401 path without looking at local
  // changes, and the notice must still say they are lost.
  const unsavedChanges = snapshot.hasUnconfirmedLocalChanges
    || snapshot.durabilityStatus === 'error'
  // A guest cannot sign in, so "sign in again" means access has ended.
  if (snapshot.connectionStatus === 'access_revoked' || snapshot.recoverability === 'login') {
    return {
      kind: 'ended',
      unavailable: snapshot.error === COLLABORATION_NOTICES.link_unavailable,
      unsavedChanges,
    }
  }
  if (snapshot.error === COLLABORATION_NOTICES.access_changed_with_unconfirmed_updates) {
    return { kind: 'verify', unsavedChanges }
  }
  return { kind: 'continue' }
}

/** Whether the service refused the guest session itself, for good or while
 * the link is paused (423). An outage (5xx, 429, no answer) says nothing
 * about access and is never read as its end. */
export function guestSessionIsGone(error: unknown): boolean {
  const status = requestStatus(error)
  return status === 401 || status === 403 || status === 404 || status === 423
}

/** Whether the service paused the link: its owner is not active, and it may
 * work again later. */
export function guestLinkUnavailable(error: unknown): boolean {
  return requestStatus(error) === 423
}

/** Why the landing page cannot open a link. Only the service answering that
 * it does not know the link (404) makes it invalid; a paused link (423) is
 * apart; an outage, a rate limit or a lost connection says nothing about
 * the link, so it is temporarily unavailable. */
export type GuestLandingFailure = 'invalid' | 'paused' | 'unavailable'

export function guestLandingFailure(error: unknown): GuestLandingFailure {
  const status = requestStatus(error)
  if (status === 404) return 'invalid'
  if (status === 423) return 'paused'
  return 'unavailable'
}

function requestStatus(error: unknown): number | undefined {
  return error instanceof Error
    ? (error as InqtrixRequestError).status
    : undefined
}
