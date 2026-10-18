/**
 * Which connection of a room may publish the presence state of an awareness
 * client id.
 *
 * An awareness id is chosen by the client, so the only trusted input is the
 * authenticated context of the connection that sends it. The first connection
 * that publishes a state for an id binds the id to its principal (tenant and
 * user identity; a guest's identity is its user id) for the lifetime of the
 * room. From then on:
 *
 * - the id's current owner publishes freely;
 * - another connection of the same principal takes the id over when it
 *   authenticated later than the owner (a reconnect of the same editor whose
 *   previous connection may still look open to the service), or reclaims it
 *   when the owner has left the room;
 * - a connection of the same principal that authenticated earlier than the
 *   owner is stale: its state is dropped, and the caller closes it;
 * - another principal never changes the id's state, whether the owner is
 *   present or away;
 * - one connection publishes at most one id.
 *
 * Nothing here closes a connection or throws: the caller turns a verdict into
 * effects. The bookkeeping is kept per room object, so it ends with the room.
 */

export type AwarenessDropReason =
  | 'foreign_client_id'
  | 'second_client_id'
  | 'superseded_connection'

export type AwarenessClaimant<Connection extends object> = {
  /** Order of authentication across the service; a higher value
   * authenticated later. */
  authenticationSequence: number
  connection: Connection
  principal: string
}

export type AwarenessVerdict<Connection extends object> =
  /** The connection owns the id already. */
  | { kind: 'accept' }
  /** The id had no owner in this room; the connection owns it now. */
  | { kind: 'claim' }
  /** The id's owner left the room; the same principal owns it again. */
  | { kind: 'reclaim' }
  /** The same principal authenticated again while its previous connection
   * still owned the id; the new connection owns it now and the previous one
   * is superseded. */
  | { kind: 'takeover'; superseded: Connection }
  /** The state is not published. `supersede` names a stale connection of the
   * owner's principal that authenticated before the owner. */
  | { kind: 'drop'; reason: AwarenessDropReason; supersede: boolean }

type OwnerRecord<Connection extends object> = {
  /** Null while the principal is away: the id stays bound to it. */
  owner: AwarenessClaimant<Connection> | null
  principal: string
}

export class AwarenessOwnership<Room extends object, Connection extends object> {
  private readonly rooms = new WeakMap<Room, Map<number, OwnerRecord<Connection>>>()
  private readonly claims = new WeakMap<Connection, { clientId: number; room: Room }>()

  /** Decides whether `claimant` may publish a state for `clientId` in `room`
   * and records the outcome. */
  admit(
    room: Room,
    clientId: number,
    claimant: AwarenessClaimant<Connection>,
  ): AwarenessVerdict<Connection> {
    const claimed = this.claims.get(claimant.connection)
    if (claimed && claimed.clientId !== clientId) {
      return { kind: 'drop', reason: 'second_client_id', supersede: false }
    }
    const records = this.records(room)
    const record = records.get(clientId)
    if (!record) {
      records.set(clientId, { owner: claimant, principal: claimant.principal })
      this.claims.set(claimant.connection, { clientId, room })
      return { kind: 'claim' }
    }
    if (record.owner?.connection === claimant.connection) return { kind: 'accept' }
    if (record.principal !== claimant.principal) {
      return { kind: 'drop', reason: 'foreign_client_id', supersede: false }
    }
    const owner = record.owner
    if (!owner) {
      record.owner = claimant
      this.claims.set(claimant.connection, { clientId, room })
      return { kind: 'reclaim' }
    }
    if (claimant.authenticationSequence > owner.authenticationSequence) {
      record.owner = claimant
      this.claims.delete(owner.connection)
      this.claims.set(claimant.connection, { clientId, room })
      return { kind: 'takeover', superseded: owner.connection }
    }
    return { kind: 'drop', reason: 'superseded_connection', supersede: true }
  }

  /** Whether `connection` currently owns `clientId` in `room`. */
  owns(room: Room, clientId: number, connection: Connection): boolean {
    return this.rooms.get(room)?.get(clientId)?.owner?.connection === connection
  }

  /** The principal `clientId` is bound to in `room`, or null while nobody
   * published it. */
  principalOf(room: Room, clientId: number): string | null {
    return this.rooms.get(room)?.get(clientId)?.principal ?? null
  }

  /** Whether `connection` owns an id at all; a connection is released once
   * it left its room. */
  hasClaim(connection: Connection): boolean {
    return this.claims.has(connection)
  }

  /** The connection left its room: its id stays bound to its principal,
   * without an owner, and the connection is no longer referenced. Only the
   * current owner holds a claim; a takeover moves it. */
  release(connection: Connection): void {
    const claim = this.claims.get(connection)
    if (!claim) return
    this.claims.delete(connection)
    const record = this.rooms.get(claim.room)?.get(claim.clientId)
    if (record) record.owner = null
  }

  private records(room: Room): Map<number, OwnerRecord<Connection>> {
    const existing = this.rooms.get(room)
    if (existing) return existing
    const created = new Map<number, OwnerRecord<Connection>>()
    this.rooms.set(room, created)
    return created
  }
}

/** The principal an awareness id is bound to: tenant and user identity. A
 * guest's identity is carried in its user id. The login session is not part
 * of it: signing in again in another tab keeps the same editor, and with it
 * the same awareness id. */
export function awarenessPrincipal(context: {
  tenantId: string
  user: { id: string; kind?: 'guest' | 'user' }
}): string {
  return `${context.tenantId}\0${context.user.kind ?? 'user'}\0${context.user.id}`
}
