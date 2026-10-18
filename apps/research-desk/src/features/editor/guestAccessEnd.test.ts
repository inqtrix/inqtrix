import { describe, expect, it } from 'vitest'

import { COLLABORATION_NOTICES } from './collaborationNotices'
import { guestAccessEnd, guestSessionIsGone } from './guestAccessEnd'
import type { CollaborationDocumentHandle } from './useCollaborationDocument'

type Snapshot = Parameters<typeof guestAccessEnd>[0]

function snapshot(patch: Partial<Snapshot>): Snapshot {
  return {
    connectionStatus: 'connected',
    durabilityStatus: 'saved',
    error: null,
    hasUnconfirmedLocalChanges: false,
    recoverability: 'none',
    ...patch,
  } satisfies Pick<CollaborationDocumentHandle, keyof Snapshot>
}

function httpError(status: number): Error {
  return Object.assign(new Error(`HTTP ${status}`), { status })
}

describe('when a guest\'s access through a link has ended', () => {
  // The guest page kept showing the document and its comments after the link
  // was revoked, with nothing but a disabled "Reconnect" in the header.
  it('ends access when the guest session is gone', () => {
    expect(guestAccessEnd(snapshot({
      connectionStatus: 'error',
      error: COLLABORATION_NOTICES.session_expired,
      recoverability: 'login',
    }))).toEqual({ kind: 'ended', unsavedChanges: false })
  })

  it('ends access when the link was revoked or the document deleted', () => {
    expect(guestAccessEnd(snapshot({
      connectionStatus: 'access_revoked',
      error: COLLABORATION_NOTICES.access_unavailable,
    }))).toEqual({ kind: 'ended', unsavedChanges: false })
  })

  // A lease renewal that meets the ended session takes the 401 path without
  // looking at local changes; whether any were lost is read from the
  // controller at that moment, on every path.
  it('states lost changes whenever changes were still unconfirmed', () => {
    expect(guestAccessEnd(snapshot({
      connectionStatus: 'error',
      hasUnconfirmedLocalChanges: true,
      recoverability: 'login',
    }))).toEqual({ kind: 'ended', unsavedChanges: true })
    expect(guestAccessEnd(snapshot({
      connectionStatus: 'access_revoked',
      durabilityStatus: 'error',
    }))).toEqual({ kind: 'ended', unsavedChanges: true })
  })

  // With changes still unconfirmed the controller does not ask the service
  // whether access remains; the page asks once instead of guessing.
  it('asks the service once when access changed while changes were unconfirmed', () => {
    expect(guestAccessEnd(snapshot({
      connectionStatus: 'error',
      durabilityStatus: 'error',
      error: COLLABORATION_NOTICES.access_changed_with_unconfirmed_updates,
      hasUnconfirmedLocalChanges: true,
      recoverability: 'none',
    }))).toEqual({ kind: 'verify', unsavedChanges: true })
  })

  it('keeps the document while the connection only reconnects or fails for another reason', () => {
    for (const state of [
      snapshot({ connectionStatus: 'reconnecting', recoverability: 'retry' }),
      snapshot({ connectionStatus: 'connecting' }),
      snapshot({ connectionStatus: 'error', recoverability: 'retry' }),
      snapshot({ connectionStatus: 'incompatible', recoverability: 'reload' }),
      snapshot({ connectionStatus: 'error', durabilityStatus: 'error', recoverability: 'reload' }),
      snapshot({ connectionStatus: 'read_only' }),
    ]) {
      expect(guestAccessEnd(state)).toEqual({ kind: 'continue' })
    }
  })

  it('counts only a refused session as gone, never an outage', () => {
    expect([401, 403, 404].map((status) => guestSessionIsGone(httpError(status))))
      .toEqual([true, true, true])
    expect([429, 500, 502, 503].map((status) => guestSessionIsGone(httpError(status))))
      .toEqual([false, false, false, false])
    expect(guestSessionIsGone(new TypeError('Failed to fetch'))).toBe(false)
  })
})
