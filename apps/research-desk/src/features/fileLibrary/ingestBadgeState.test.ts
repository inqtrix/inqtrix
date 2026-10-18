import { describe, expect, it } from 'vitest'
import { ingestBadgeState } from './helpers'

describe('ingestBadgeState', () => {
  it('shows the upload badge while the upload is queued or in flight', () => {
    expect(ingestBadgeState({ parsePending: true, uploadError: null, uploadPending: true })).toBe('uploading')
  })

  it('shows the retryable upload error once the upload settled failed', () => {
    expect(ingestBadgeState({ parsePending: true, uploadError: 'kaputt', uploadPending: false })).toBe('upload-error')
  })

  it('shows the parsing badge only after the upload settled cleanly', () => {
    expect(ingestBadgeState({ parsePending: true, uploadError: null, uploadPending: false })).toBe('parsing')
  })

  it('is null for a fully settled row (provenance badge takes over)', () => {
    expect(ingestBadgeState({ parsePending: false, uploadError: null, uploadPending: false })).toBeNull()
    expect(ingestBadgeState({})).toBeNull()
  })
})
