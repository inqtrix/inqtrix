import { describe, expect, it } from 'vitest'

import {
  enforceAwarenessIdentity,
  removeHocuspocusScratchAwarenessState,
} from '../src/awareness'
import type { ConnectionContext } from '../src/contracts'

const context: ConnectionContext = {
  access: 'edit',
  documentId: '11111111-1111-4111-8111-111111111111',
  expiresAt: 1_900_000_000,
  generation: 1,
  leaseId: '55555555-5555-4555-8555-555555555555',
  policyCursor: 0,
  protocolVersion: 1,
  schemaHash: 'a'.repeat(64),
  schemaVersion: 1,
  sessionId: '22222222-2222-4222-8222-222222222222',
  tenantId: '33333333-3333-4333-8333-333333333333',
  user: {
    color: '#2563EB',
    id: '44444444-4444-4444-8444-444444444444',
    name: 'Ada',
  },
}

describe('Hocuspocus awareness normalization', () => {
  it('removes only the leading empty scratch state', () => {
    const states = new Map<number, Record<string, unknown>>([
      [101, {}],
      [7, {
        cursor: { anchor: 3, head: 5 },
        user: { color: '#badbad', id: 'spoofed', name: 'Spoofed' },
      }],
    ])

    expect(removeHocuspocusScratchAwarenessState(states)).toBe(true)
    enforceAwarenessIdentity(states, context)

    expect(states).toEqual(new Map([
      [7, {
        cursor: { anchor: 3, head: 5 },
        user: context.user,
      }],
    ]))
  })

  it('normalizes a scratch-only removal update to an empty map', () => {
    const states = new Map<number, Record<string, unknown>>([[101, {}]])

    expect(removeHocuspocusScratchAwarenessState(states)).toBe(true)
    expect(states.size).toBe(0)
  })

  it('leaves an upstream-fixed awareness shape unchanged', () => {
    const state = { user: { id: 'client-user' } }
    const states = new Map<number, Record<string, unknown>>([[7, state]])

    expect(removeHocuspocusScratchAwarenessState(states)).toBe(false)
    expect(states.get(7)).toBe(state)
  })

  it('does not guess when the empty state is not the leading entry', () => {
    const states = new Map<number, Record<string, unknown>>([
      [7, { user: { id: 'client-user' } }],
      [101, {}],
    ])

    expect(removeHocuspocusScratchAwarenessState(states)).toBe(false)
    expect(() => enforceAwarenessIdentity(states, context)).toThrowError('invalid_request')
  })
})
