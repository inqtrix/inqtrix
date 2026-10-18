/**
 * Every notice the collaboration controller publishes, by code, with the
 * English text it publishes. The code is the contract with the surface,
 * which shows the text of the reader's language for it; the English text
 * also reaches logs and failed durability waiters. A notice outside this
 * table cannot be published: the controller's notice parameters and state
 * accept only these texts. Each text is one string literal, never a
 * concatenation, so that the notice type is the union of these exact texts
 * and not `string`.
 */
export const COLLABORATION_NOTICES = Object.freeze({
  access_changed_revalidating:
    'Collaboration access changed; revalidating read-only.',
  access_changed_with_unconfirmed_updates:
    'Collaboration access changed before local updates were durably confirmed. Your local document state is retained for recovery or backup.',
  access_unavailable:
    'Access to this collaboration document is unavailable.',
  ack_delayed:
    'The collaboration service has not confirmed the latest change for more than ten seconds. Your changes are kept in this editor.',
  authentication_failed:
    'Collaboration authentication failed; reconnecting read-only.',
  causal_gap_resending:
    'The collaboration service is missing earlier changes from this editor; they are being sent again in order.',
  causal_gap_unrecoverable:
    'Earlier changes from this editor could not be restored in order. Your local document state is kept as a backup until you reload the document.',
  connection_interrupted:
    'The collaboration connection was interrupted; reconnecting read-only.',
  durability_failed_before_navigation:
    'Collaboration durability failed before navigation.',
  flush_interval_incompatible:
    'The collaboration flush interval is not compatible with this client.',
  identity_changed:
    'Collaboration identity changed while refreshing access.',
  ingress_overflow_reconnecting:
    'The collaboration service received changes from this editor faster than it could process them; reconnecting and sending them again.',
  lease_refresh_failed:
    'The collaboration lease could not be refreshed; reconnecting read-only.',
  limits_incompatible:
    'The collaboration limits are not compatible with this client.',
  origin_rejected:
    'The collaboration server rejected this page origin. The address the browser uses does not match the configured public address of the server.',
  protocol_incompatible:
    'The collaboration protocol is not compatible with this client.',
  protocol_or_schema_incompatible:
    'The collaboration protocol or schema is not compatible.',
  refresh_interval_incompatible:
    'The collaboration refresh interval is not compatible with this client.',
  release_unconfirmed:
    'Collaboration changes could not be confirmed before navigation completed.',
  room_changed:
    'The collaboration room changed while refreshing its lease.',
  schema_unsupported:
    'The document schema is not supported by this client.',
  send_failed:
    'Local changes could not be sent to the collaboration service; reconnecting read-only.',
  // Every conflict that ends a session without a compatibility reason says
  // this, so it claims only what holds for each of them: the session ended and
  // will not resume with this state. Not "your state is outdated", which would
  // be wrong for a fenced instance, a tenant conflict, an invalid room or a
  // version skew between the service and the API.
  session_ended:
    'The collaboration service ended this session and will not resume it with the current state. Reload the page to continue.',
  session_expired:
    'The collaboration session expired. Sign in again to continue.',
  session_open_failed_retrying:
    'The collaboration session could not be opened; retrying.',
  session_open_refused:
    'The collaboration service refused to open this session.',
  update_rejected:
    'The collaboration service rejected a change from this editor. Your local document state is kept as a backup until you reload the document.',
  update_too_large:
    'The collaboration service rejected a change from this editor because it exceeds the supported size. Your local document state is kept as a backup until you reload the document.',
  update_untracked:
    'The collaboration update could not be tracked for durable storage.',
} as const)

export type CollaborationNoticeCode = keyof typeof COLLABORATION_NOTICES

/** A text of COLLABORATION_NOTICES: the only texts the controller publishes. */
export type CollaborationNotice = (typeof COLLABORATION_NOTICES)[CollaborationNoticeCode]

const NOTICE_CODES: ReadonlyMap<string, CollaborationNoticeCode> = new Map(
  (Object.entries(COLLABORATION_NOTICES) as Array<[CollaborationNoticeCode, string]>)
    .map(([code, text]) => [text, code]),
)

/** The code of a published notice, or null for a text that is none. */
export function collaborationNoticeCode(notice: string): CollaborationNoticeCode | null {
  return NOTICE_CODES.get(notice) ?? null
}
