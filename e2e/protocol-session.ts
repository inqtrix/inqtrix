export type CollaborationProtocolSession = {
  access: 'edit' | 'suggest' | 'view'
  initialWriteMode: 'edit' | 'suggest' | 'view'
  leaseToken: string
  room: string
  userId: string
  websocketPath: string
}

export function parseCollaborationProtocolSession(
  payload: unknown,
): CollaborationProtocolSession {
  const session = isRecord(payload) ? payload : null
  const user = isRecord(session?.user) ? session.user : null
  if (
    !isAccess(session?.access)
    || !isAccess(session?.initial_write_mode)
    || session.access !== session.initial_write_mode
    || typeof session?.lease_token !== 'string'
    || session.lease_token.length === 0
    || typeof session.room !== 'string'
    || session.room.length === 0
    || typeof user?.id !== 'string'
    || user.id.length === 0
    || typeof session.websocket_path !== 'string'
    || session.websocket_path.length === 0
  ) {
    throw new Error(
      'Collaboration session omitted lease, authority, identity, or raw protocol fields.',
    )
  }
  return {
    access: session.access,
    initialWriteMode: session.initial_write_mode,
    leaseToken: session.lease_token,
    room: session.room,
    userId: user.id,
    websocketPath: session.websocket_path,
  }
}

function isAccess(value: unknown): value is CollaborationProtocolSession['access'] {
  return value === 'edit' || value === 'suggest' || value === 'view'
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}
