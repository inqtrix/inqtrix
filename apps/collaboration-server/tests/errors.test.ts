import { describe, expect, it } from 'vitest'

import { ApiRequestError, collaborationError } from '../src/errors.js'

describe('collaborationError: a reason belongs to whoever produced it', () => {
  // The sidecar maps only a few of the about forty reasons the internal API
  // can send. If it turned everything else into "update_required", a claim
  // about an outdated client, the real reason would be destroyed: it would
  // appear neither in the sidecar log nor in the answer to the user, and the
  // operator could not find out why a request was refused.
  it('carries an unknown 409 reason on instead of claiming an outdated client', () => {
    const error = collaborationError(new ApiRequestError(409, 'projection_not_current'))

    expect(error.upstreamReason).toBe('projection_not_current')
    expect(error.reason).not.toBe('update_required')
  })

  it('turns a fenced instance into a lost instance lease the client reconnects from', () => {
    // The store refuses every write of an instance whose lease it no longer
    // holds. That ends this instance's rooms, not the user's session: the
    // client reconnects, possibly to the instance that holds the lease now.
    const error = collaborationError(new ApiRequestError(409, 'instance_fenced'))

    expect(error.reason).toBe('instance_lease_lost')
    expect(error.code).toBe(4503)
    expect(error.closeReason).toBe('instance_lease_lost')
    expect(error.httpStatus).toBe(503)
    expect(error.upstreamReason).toBe('instance_fenced')
  })

  it('invents nothing for a reason it does not know either', () => {
    const error = collaborationError(new ApiRequestError(409, 'patch_decision_incomplete'))

    expect(error.upstreamReason).toBe('patch_decision_incomplete')
    expect(error.reason).not.toBe('update_required')
  })

  it('keeps the known mappings', () => {
    // Counter-check: the three mapped reasons must not change; they select
    // the WebSocket close code.
    expect(collaborationError(new ApiRequestError(409, 'sequence_conflict')).reason)
      .toBe('sequence_conflict')
    expect(collaborationError(new ApiRequestError(409, 'command_conflict')).reason)
      .toBe('sequence_conflict')
    expect(collaborationError(new ApiRequestError(409, 'generation_mismatch')).reason)
      .toBe('generation_mismatch')
  })

  it('names update_required only when the internal API says so itself', () => {
    const error = collaborationError(new ApiRequestError(409, 'update_required'))
    expect(error.reason).toBe('update_required')
    expect(error.upstreamReason).toBe('update_required')
  })

  // A 400 has no branch of its own and ends in the catch-all, which calls it
  // "internal_consistency": a room state that never existed. The sidecar
  // closes with 1011 and the browser reconnects, again and again, unless the
  // real reason of the refusal travels with it.
  it('keeps the reason of a 400 refusal instead of swallowing it', () => {
    const error = collaborationError(new ApiRequestError(400, 'update_hash_mismatch'))

    expect(error.upstreamReason).toBe('update_hash_mismatch')
  })

  it('keeps the reason of a status it does not handle otherwise', () => {
    const error = collaborationError(new ApiRequestError(418, 'teapot'))

    expect(error.upstreamReason).toBe('teapot')
  })
})

describe('collaborationError: a refused lease or access names why', () => {
  // The internal API refuses a lease with 401 for several distinct causes
  // and access with 403 or 404. The sidecar maps each status onto one reason
  // of its own, which selects the close code; the API's reason must travel
  // with it into the error, the log text and the close reason, or a signed-
  // out user, a lowered permission and a rotated lease all read the same.
  it.each([
    ['lease_invalid'],
    ['session_invalid'],
    ['permission_denied'],
  ])('carries the API reason %s of a 401 into the close reason and the log text', (reason) => {
    const error = collaborationError(new ApiRequestError(401, reason))

    expect(error.reason).toBe('invalid_lease')
    expect(error.code).toBe(4401)
    expect(error.httpStatus).toBe(401)
    expect(error.upstreamReason).toBe(reason)
    expect(error.closeReason).toBe(`invalid_lease:${reason}`)
    expect(error.message).toBe(`invalid_lease:${reason}`)
  })

  it('carries the API reason of a 404 into the close reason and the log text', () => {
    const error = collaborationError(new ApiRequestError(404, 'not_found'))

    expect(error.reason).toBe('access_revoked')
    expect(error.code).toBe(4403)
    expect(error.httpStatus).toBe(404)
    expect(error.upstreamReason).toBe('not_found')
    expect(error.closeReason).toBe('access_revoked:not_found')
    expect(error.message).toBe('access_revoked:not_found')
  })

  it.each([
    [401, 'invalid_lease', 4401],
    [403, 'access_revoked', 4403],
  ] as const)('does not repeat an API reason of a %i that equals its own', (status, reason, code) => {
    const error = collaborationError(new ApiRequestError(status, reason))

    expect(error.reason).toBe(reason)
    expect(error.code).toBe(code)
    expect(error.httpStatus).toBe(status)
    expect(error.upstreamReason).toBe(reason)
    expect(error.closeReason).toBe(reason)
    expect(error.message).toBe(reason)
  })

  // Counter-check of the scope: a 409 or a 400 reason is not appended to the
  // close reason. Those reasons are open-ended (a 400 names the refused
  // field, for example invalid_schema_version), and the browser classifies
  // a close by the words it contains, so an appended invalid_schema_version
  // would read as the sidecar's own invalid_schema.
  it.each([
    [409, 'projection_not_current', 'upstream_conflict', 4409],
    [400, 'invalid_schema_version', 'internal_consistency', 1011],
  ] as const)('keeps the close reason of a %i to the sidecar reason', (status, upstream, reason, code) => {
    const error = collaborationError(new ApiRequestError(status, upstream))

    expect(error.reason).toBe(reason)
    expect(error.code).toBe(code)
    expect(error.upstreamReason).toBe(upstream)
    expect(error.closeReason).toBe(reason)
  })
})
