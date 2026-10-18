import type { ConnectionContext } from './contracts'
import { CloseCodes, CollaborationError } from './errors'

const MAX_AWARENESS_STATE_BYTES = 16 * 1024

export function removeHocuspocusScratchAwarenessState(
  states: Map<number, Record<string, unknown>>,
): boolean {
  const first = states.entries().next()
  if (first.done) return false

  const [clientId, state] = first.value
  if (!isPlainEmptyRecord(state)) return false

  // @hocuspocus/server 4.3 and 4.4 decode inbound awareness updates through
  // a scratch Awareness instance. Its constructor inserts this leading empty
  // local state before the real client states. Restrict the workaround to that
  // exact shape so a changed upstream representation fails closed below.
  states.delete(clientId)
  return true
}

export function enforceAwarenessIdentity(
  states: Map<number, Record<string, unknown>>,
  context: ConnectionContext,
): void {
  if (states.size > 1) throw invalidAwareness()
  for (const [clientId, state] of states) {
    const sanitized: Record<string, unknown> = {
      user: {
        color: context.user.color,
        id: context.user.id,
        name: context.user.name,
      },
    }
    if (state.cursor !== undefined && state.cursor !== null) {
      sanitized.cursor = jsonClone(state.cursor)
    }
    if (Buffer.byteLength(JSON.stringify(sanitized), 'utf8') > MAX_AWARENESS_STATE_BYTES) {
      throw invalidAwareness()
    }
    states.set(clientId, sanitized)
  }
}

function isPlainEmptyRecord(value: unknown): value is Record<string, never> {
  return (
    typeof value === 'object'
    && value !== null
    && !Array.isArray(value)
    && Object.getPrototypeOf(value) === Object.prototype
    && Object.keys(value).length === 0
  )
}

function jsonClone(value: unknown): unknown {
  try {
    return JSON.parse(JSON.stringify(value)) as unknown
  } catch {
    throw invalidAwareness()
  }
}

function invalidAwareness(): CollaborationError {
  return new CollaborationError('invalid_request', {
    closeCode: CloseCodes.accessRevoked,
    httpStatus: 400,
  })
}
