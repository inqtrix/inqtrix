import { describe, expect, it } from 'vitest'

import type { AdminUser, WorkspaceMember } from '@/api/inqtrixClient'
import { candidateUsers, wouldOrphanLastOwner } from './workspaceModel'

function member(userId: string, role: WorkspaceMember['role']): WorkspaceMember {
  return { display_name: userId, email: `${userId}@example.com`, role, user_id: userId }
}

function user(id: string, displayName: string | null): AdminUser {
  return {
    disabled: false,
    display_name: displayName,
    email: `${id}@example.com`,
    instance_role: 'user',
    last_login_at: null,
    erasure_operation_id: null,
    id,
  }
}

describe('wouldOrphanLastOwner (last-owner guard)', () => {
  const soleOwner = [member('a', 'owner'), member('b', 'editor')]
  const twoOwners = [member('a', 'owner'), member('b', 'owner')]

  it('blocks removing or demoting the only owner', () => {
    // keepsOwner=false models both removal and a role change to a non-owner.
    expect(wouldOrphanLastOwner(soleOwner, 'a', false)).toBe(true)
  })

  it('allows a no-op owner->owner role change', () => {
    expect(wouldOrphanLastOwner(soleOwner, 'a', true)).toBe(false)
  })

  it('allows removing one of several owners', () => {
    expect(wouldOrphanLastOwner(twoOwners, 'a', false)).toBe(false)
  })

  it('never blocks a non-owner', () => {
    expect(wouldOrphanLastOwner(soleOwner, 'b', false)).toBe(false)
  })
})

describe('candidateUsers (add-member pool)', () => {
  const users = [
    user('u-charlie', 'Charlie'),
    user('u-alice', 'Alice'),
    user('u-bob', 'Bob'),
  ]

  it('excludes existing members and sorts by display name', () => {
    const result = candidateUsers(users, new Set(['u-bob']))
    expect(result.map((entry) => entry.id)).toEqual([
      'u-alice',
      'u-charlie',
    ])
  })

  it('excludes disabled users (the server assign endpoint 404s on them)', () => {
    const disabled = { ...user('u-dora', 'Dora'), disabled: true }
    const result = candidateUsers([...users, disabled], new Set())
    expect(result.some((entry) => entry.id === 'u-dora')).toBe(false)
  })

  it('excludes an account being erased (it is cut off and its memberships go)', () => {
    const erasing = { ...user('u-emil', 'Emil'), erasure_operation_id: 'del_1' }
    const result = candidateUsers([...users, erasing], new Set())
    expect(result.some((entry) => entry.id === 'u-emil')).toBe(false)
  })

  it('falls back to email then id when the name is missing', () => {
    const result = candidateUsers([user('u-zoe', null)], new Set())
    expect(result).toHaveLength(1)
  })

  it('filters by a case-insensitive query over name/email/id', () => {
    const byName = candidateUsers(users, new Set(), 'ALI')
    expect(byName.map((entry) => entry.id)).toEqual(['u-alice'])
    // Email match (u-bob@example.com) even when the name does not match.
    const byEmail = candidateUsers(users, new Set(), 'u-bob@')
    expect(byEmail.map((entry) => entry.id)).toEqual(['u-bob'])
    // An empty query returns the full (member/disabled-filtered) list.
    expect(candidateUsers(users, new Set(), '  ')).toHaveLength(3)
  })
})
