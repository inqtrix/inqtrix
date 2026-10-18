import { COLLABORATION_CAUSAL_GAP_CLOSE } from '@inqtrix/editor-schema'

export const CloseCodes = Object.freeze({
  accessRevoked: 4403,
  causalGap: COLLABORATION_CAUSAL_GAP_CLOSE.code,
  incompatible: 4409,
  internalConsistency: 1011,
  leaseInvalid: 4401,
  messageTooLarge: 1009,
  rateLimited: 4429,
  restarting: 1012,
  serviceUnavailable: 4503,
})

export type CollaborationErrorReason =
  | 'access_revoked'
  | 'base_changed'
  | typeof COLLABORATION_CAUSAL_GAP_CLOSE.reason
  | 'connection_superseded'
  | 'decision_conflict'
  | 'document_too_large'
  | 'generation_mismatch'
  | 'instance_lease_lost'
  | 'internal_consistency'
  | 'invalid_lease'
  | 'invalid_request'
  | 'invalid_room'
  | 'invalid_schema'
  | 'message_too_large'
  | 'rate_limited'
  | 'restarting'
  | 'sequence_conflict'
  | 'service_unavailable'
  | 'suggestion_conflict'
  | 'suggestion_policy_violation'
  | 'unsupported_suggestion_structure'
  | 'update_required'
  | 'upstream_conflict'

export class CollaborationError extends Error {
  readonly code: number
  readonly httpStatus: number
  readonly reason: CollaborationErrorReason
  /** The reason the internal API gave, unchanged.
   *
   * The sidecar maps onto its own small vocabulary, because that vocabulary
   * selects the WebSocket close code. The mapping must not destroy the
   * original reason: without it, neither the log nor the user learns why a
   * request was really refused. */
  readonly upstreamReason: string | undefined
  /** Why a suggestion publication was refused, in the publisher's and the
   * suggestion library's own words (`node_marks_unsupported`,
   * `too_many_new_blocks`, ...).
   *
   * `reason` stays the small fixed vocabulary clients and metrics key on;
   * this names the cause behind it, so the log and the answer say which
   * rule refused. It is an open set and travels nowhere a client
   * classifies by. */
  readonly suggestionReason: string | undefined
  /** The text the socket is closed with and the refusal is logged under.
   *
   * It is the sidecar's reason, unless the mapping names the internal API's
   * reason in it as well. */
  readonly closeReason: string

  constructor(
    reason: CollaborationErrorReason,
    options: {
      closeCode: number
      closeReason?: string
      httpStatus?: number
      suggestionReason?: string
      upstreamReason?: string
    },
  ) {
    const closeReason = options.closeReason ?? reason
    super(closeReason)
    this.name = 'CollaborationError'
    this.code = options.closeCode
    this.closeReason = closeReason
    this.httpStatus = options.httpStatus ?? 500
    this.reason = reason
    this.suggestionReason = options.suggestionReason
    this.upstreamReason = options.upstreamReason
  }
}

export class ApiRequestError extends Error {
  readonly reason: string
  readonly status: number

  constructor(status: number, reason: string) {
    super(`Internal API request failed with status ${status}`)
    this.name = 'ApiRequestError'
    this.reason = reason
    this.status = status
  }
}

/** Whether the internal API refused the request because the store no
 * longer holds this instance's fencing lease: every later write under the
 * same lease is refused as well. */
export function isInstanceFenced(error: unknown): boolean {
  return error instanceof ApiRequestError
    && error.status === 409
    && error.reason === 'instance_fenced'
}

/** Whether the internal API answered that the document no longer exists:
 * it was deleted or purged. Unlike a refused lease or a lost permission, no
 * later request for it can succeed. */
export function isDocumentRemoved(error: unknown): boolean {
  return error instanceof ApiRequestError
    && error.status === 404
    && error.reason === 'not_found'
}

/** Whether the failure proves the persistence transaction did not write.
 *
 * A 4xx answer from the internal API is a decision, not an outage: the request
 * was understood, rejected, and rolled back. Callers use this to distinguish a
 * rejection they can recover from locally from an unknown outcome that leaves
 * the in-memory room possibly ahead of the store. Timeouts, transport errors
 * and 5xx are deliberately NOT deterministic — the write may have landed.
 */
export function isDeterministicRejection(error: unknown): boolean {
  return error instanceof ApiRequestError
    && error.status >= 400
    && error.status < 500
}

export function collaborationError(error: unknown): CollaborationError {
  if (error instanceof CollaborationError) return error
  if (error instanceof ApiRequestError) {
    // One status stands for several refusals here: a 401 for an unknown,
    // expired or rotated lease, a signed-out session or a lowered
    // permission; a 403 or 404 for lost access. The API's reason travels in
    // the close reason and the log text, after the sidecar's own. Clients
    // classify a close by the words its reason contains, and these reasons
    // are a closed set of lease and access codes that none of those words
    // occur in.
    if (error.status === 401) {
      return new CollaborationError('invalid_lease', {
        closeCode: CloseCodes.leaseInvalid,
        closeReason: withUpstreamReason('invalid_lease', error.reason),
        httpStatus: 401,
        upstreamReason: error.reason,
      })
    }
    if (error.status === 403 || error.status === 404) {
      return new CollaborationError('access_revoked', {
        closeCode: CloseCodes.accessRevoked,
        closeReason: withUpstreamReason('access_revoked', error.reason),
        httpStatus: error.status,
        upstreamReason: error.reason,
      })
    }
    if (isInstanceFenced(error)) {
      // The rooms of this instance end, not the user's session: the client
      // reconnects, as after any other loss of the instance lease.
      return new CollaborationError('instance_lease_lost', {
        closeCode: CloseCodes.serviceUnavailable,
        httpStatus: 503,
        upstreamReason: error.reason,
      })
    }
    if (error.status === 409) {
      // The internal API knows about forty conflict reasons; only those
      // with a handling of their own are mapped here. Everything else stays
      // a conflict without an invented cause: the real reason travels as
      // upstreamReason instead of turning into "your client is outdated".
      // It stays out of the close reason: the conflict reasons are an open
      // set, and one containing a word the client classifies by would
      // change how the client handles the close.
      const reason: CollaborationErrorReason =
        error.reason === 'sequence_conflict' || error.reason === 'command_conflict'
          ? 'sequence_conflict'
          : error.reason === 'generation_mismatch'
            ? 'generation_mismatch'
            : error.reason === 'update_required'
              ? 'update_required'
              : 'upstream_conflict'
      return new CollaborationError(reason, {
        closeCode: CloseCodes.incompatible,
        httpStatus: 409,
        upstreamReason: error.reason,
      })
    }
    if (error.status === 413) {
      return new CollaborationError('document_too_large', {
        closeCode: CloseCodes.messageTooLarge,
        httpStatus: 413,
      })
    }
    if (error.status === 429) {
      return new CollaborationError('rate_limited', {
        closeCode: CloseCodes.rateLimited,
        httpStatus: 429,
      })
    }
    if (error.status === 503) {
      return new CollaborationError('service_unavailable', {
        closeCode: CloseCodes.serviceUnavailable,
        httpStatus: 503,
      })
    }
  }
  // The catch-all: every answer without a branch of its own above, above
  // all a 400, ends here. Outwardly it stays internal_consistency, because
  // for the user it is an internal failure. The internal API's reason must
  // not get lost on the way: without it the sidecar reports
  // "internal_consistency" for a refusal whose reason it knows, and a
  // refused request reads like an inconsistent room. It stays out of the
  // close reason, as a conflict's does: a 400 names the refused field, and
  // invalid_schema_version would read as the sidecar's invalid_schema.
  return new CollaborationError('internal_consistency', {
    closeCode: CloseCodes.internalConsistency,
    ...(error instanceof ApiRequestError ? { upstreamReason: error.reason } : {}),
  })
}

/** The sidecar's reason followed by the internal API's, when that one says
 * something the sidecar's does not. The API client accepts a reason of at
 * most 80 characters, so the text stays within the 123 bytes a WebSocket
 * close reason may carry. */
function withUpstreamReason(reason: CollaborationErrorReason, upstreamReason: string): string {
  return upstreamReason === reason ? reason : `${reason}:${upstreamReason}`
}
