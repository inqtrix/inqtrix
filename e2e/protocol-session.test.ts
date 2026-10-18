import assert from 'node:assert/strict'
import { describe, test } from 'node:test'

import { parseCollaborationProtocolSession } from './protocol-session.ts'

describe('collaboration protocol session parsing', () => {
  test('preserves a real suggest lease authority and principal identity', () => {
    assert.deepEqual(parseCollaborationProtocolSession({
      access: 'suggest',
      initial_write_mode: 'suggest',
      lease_token: 'sensitive-token-not-printed',
      room: 'tenant:document:generation',
      user: { id: '00000000-0000-4000-8000-000000000002' },
      websocket_path: '/collaboration',
    }), {
      access: 'suggest',
      initialWriteMode: 'suggest',
      leaseToken: 'sensitive-token-not-printed',
      room: 'tenant:document:generation',
      userId: '00000000-0000-4000-8000-000000000002',
      websocketPath: '/collaboration',
    })
  })

  test('rejects a lease that discards permission or user identity fields', () => {
    const incomplete = {
      lease_token: 'sensitive-token-not-printed',
      room: 'tenant:document:generation',
      websocket_path: '/collaboration',
    }
    assert.throws(
      () => parseCollaborationProtocolSession(incomplete),
      /omitted lease, authority, identity, or raw protocol fields/,
    )
  })

  test('rejects mismatched or unsupported write modes', () => {
    assert.throws(() => parseCollaborationProtocolSession({
      access: 'suggest',
      initial_write_mode: 'edit',
      lease_token: 'sensitive-token-not-printed',
      room: 'tenant:document:generation',
      user: { id: '00000000-0000-4000-8000-000000000002' },
      websocket_path: '/collaboration',
    }), /omitted lease, authority, identity, or raw protocol fields/)
  })
})
