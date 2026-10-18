import { afterEach, describe, expect, it } from 'vitest'
import {
  acceptDemoShare,
  demoOutgoingShares,
  demoSharingInbox,
  dropDemoInboxShare,
  resetDemoShares,
} from './demoShares'

afterEach(() => {
  // Each test starts from the freshly seeded demo state.
  resetDemoShares()
})

describe('demo sharing inbox', () => {
  it('seeds pending and accepted incoming shares', () => {
    const inbox = demoSharingInbox()
    expect(inbox.pending.length).toBeGreaterThan(0)
    expect(inbox.accepted.length).toBeGreaterThan(0)
    expect(inbox.pending.every((item) => item.accepted_at === null)).toBe(true)
    expect(inbox.accepted.every((item) => item.accepted_at !== null)).toBe(true)
  })

  it('accept moves a pending invitation into accepted', () => {
    const before = demoSharingInbox()
    const target = before.pending[0]
    acceptDemoShare(target.id)
    const after = demoSharingInbox()
    expect(after.pending.find((item) => item.id === target.id)).toBeUndefined()
    const moved = after.accepted.find((item) => item.id === target.id)
    expect(moved).toBeDefined()
    expect(moved?.accepted_at).not.toBeNull()
    expect(after.pending.length).toBe(before.pending.length - 1)
    expect(after.accepted.length).toBe(before.accepted.length + 1)
  })

  it('drop removes an invitation from either section', () => {
    const before = demoSharingInbox()
    const pendingTarget = before.pending[0]
    const acceptedTarget = before.accepted[0]
    dropDemoInboxShare(pendingTarget.id)
    dropDemoInboxShare(acceptedTarget.id)
    const after = demoSharingInbox()
    expect(after.pending.some((item) => item.id === pendingTarget.id)).toBe(false)
    expect(after.accepted.some((item) => item.id === acceptedTarget.id)).toBe(false)
  })

  it('outgoing listing derives a titled row with a recipient count', () => {
    const outgoing = demoOutgoingShares()
    expect(outgoing.length).toBeGreaterThan(0)
    const row = outgoing[0]
    expect(row.resource_title.length).toBeGreaterThan(0)
    expect(row.share_count).toBeGreaterThan(0)
    expect(row.pending_count).toBeLessThanOrEqual(row.share_count)
  })
})
