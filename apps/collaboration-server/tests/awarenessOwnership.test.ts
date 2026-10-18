import {
  AwarenessOwnership,
  awarenessPrincipal,
  type AwarenessClaimant,
} from '../src/awarenessOwnership'

type Room = { name: string }
type Connection = { label: string }

const ADA = awarenessPrincipal({ tenantId: 'tenant-1', user: { id: 'ada' } })
const GRACE = awarenessPrincipal({ tenantId: 'tenant-1', user: { id: 'grace' } })

function claimant(
  label: string,
  principal: string,
  authenticationSequence: number,
): AwarenessClaimant<Connection> {
  return { authenticationSequence, connection: { label }, principal }
}

describe('awareness ownership decisions', () => {
  it('binds an id to the first connection that publishes it, and accepts its later states', () => {
    const ownership = new AwarenessOwnership<Room, Connection>()
    const room = { name: 'room' }
    const first = claimant('first', ADA, 1)

    expect(ownership.admit(room, 41, first)).toEqual({ kind: 'claim' })
    expect(ownership.admit(room, 41, first)).toEqual({ kind: 'accept' })
    expect(ownership.owns(room, 41, first.connection)).toBe(true)
  })

  it('refuses a second id from one connection', () => {
    const ownership = new AwarenessOwnership<Room, Connection>()
    const room = { name: 'room' }
    const writer = claimant('writer', ADA, 1)
    ownership.admit(room, 41, writer)

    expect(ownership.admit(room, 42, writer)).toEqual({
      kind: 'drop',
      reason: 'second_client_id',
      supersede: false,
    })
    expect(ownership.admit(room, 41, writer)).toEqual({ kind: 'accept' })
  })

  it('never hands an id to another principal, whether its owner is present or away', () => {
    const ownership = new AwarenessOwnership<Room, Connection>()
    const room = { name: 'room' }
    const owner = claimant('owner', ADA, 1)
    const intruder = claimant('intruder', GRACE, 2)
    ownership.admit(room, 41, owner)

    const refused = { kind: 'drop', reason: 'foreign_client_id', supersede: false }
    expect(ownership.admit(room, 41, intruder)).toEqual(refused)
    ownership.release(owner.connection)
    expect(ownership.owns(room, 41, owner.connection)).toBe(false)
    expect(ownership.admit(room, 41, intruder)).toEqual(refused)
    expect(ownership.admit(room, 41, claimant('later intruder', GRACE, 9))).toEqual(refused)
  })

  it('gives an id back to its principal once the owner left', () => {
    const ownership = new AwarenessOwnership<Room, Connection>()
    const room = { name: 'room' }
    const first = claimant('first', ADA, 1)
    ownership.admit(room, 41, first)
    ownership.release(first.connection)
    const second = claimant('second', ADA, 2)

    expect(ownership.admit(room, 41, second)).toEqual({ kind: 'reclaim' })
    expect(ownership.owns(room, 41, second.connection)).toBe(true)
    expect(ownership.hasClaim(first.connection)).toBe(false)
  })

  it('lets a later authentication of the same principal take the id over from a live owner', () => {
    const ownership = new AwarenessOwnership<Room, Connection>()
    const room = { name: 'room' }
    const predecessor = claimant('predecessor', ADA, 3)
    const successor = claimant('successor', ADA, 7)
    ownership.admit(room, 41, predecessor)

    expect(ownership.admit(room, 41, successor)).toEqual({
      kind: 'takeover',
      superseded: predecessor.connection,
    })
    expect(ownership.owns(room, 41, successor.connection)).toBe(true)
    // The superseded connection leaving later changes nothing for its
    // successor.
    ownership.release(predecessor.connection)
    expect(ownership.owns(room, 41, successor.connection)).toBe(true)
    expect(ownership.admit(room, 41, successor)).toEqual({ kind: 'accept' })
  })

  it('decides by authentication order, not by the order in which frames arrive', () => {
    const ownership = new AwarenessOwnership<Room, Connection>()
    const room = { name: 'room' }
    const current = claimant('current', ADA, 7)
    const stale = claimant('stale', ADA, 3)
    ownership.admit(room, 41, current)

    expect(ownership.admit(room, 41, stale)).toEqual({
      kind: 'drop',
      reason: 'superseded_connection',
      supersede: true,
    })
    expect(ownership.owns(room, 41, current.connection)).toBe(true)
  })

  it('keeps the rooms apart', () => {
    const ownership = new AwarenessOwnership<Room, Connection>()
    const first = { name: 'first' }
    const second = { name: 'second' }
    ownership.admit(first, 41, claimant('ada', ADA, 1))

    expect(ownership.admit(second, 41, claimant('grace', GRACE, 2))).toEqual({ kind: 'claim' })
  })
})

describe('awareness principal', () => {
  it('is the tenant and the user identity', () => {
    const user = { id: 'same-id' }
    expect(awarenessPrincipal({ tenantId: 'tenant-1', user }))
      .toBe(awarenessPrincipal({ tenantId: 'tenant-1', user: { ...user, kind: 'user' } }))
    expect(awarenessPrincipal({ tenantId: 'tenant-1', user }))
      .not.toBe(awarenessPrincipal({ tenantId: 'tenant-2', user }))
    expect(awarenessPrincipal({ tenantId: 'tenant-1', user }))
      .not.toBe(awarenessPrincipal({ tenantId: 'tenant-1', user: { ...user, kind: 'guest' } }))
    expect(awarenessPrincipal({ tenantId: 'tenant-1', user }))
      .not.toBe(awarenessPrincipal({ tenantId: 'tenant-1', user: { id: 'other-id' } }))
  })
})
