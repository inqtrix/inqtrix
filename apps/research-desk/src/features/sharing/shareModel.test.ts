import { describe, expect, it } from 'vitest'
import {
  canCancelWithAccess,
  outgoingShareCounts,
  partitionJobsByAccess,
  personLabel,
  selectableSearchResults,
  sharePermissionLabel,
  sharePermissionsForResource,
  sharedResourceDestination,
  toggleSelectedUser,
} from './shareModel'
import type { UserSearchResult } from './types'

const user = (id: string): UserSearchResult => ({
  display_name: null,
  email: `${id}@example.com`,
  id,
})

describe('partitionJobsByAccess', () => {
  it('splits shared-in jobs out while preserving order', () => {
    const jobs = [
      { access: undefined, id: 'a' },
      { access: { mode: 'shared' as const, permission: 'view' as const }, id: 'b' },
      { access: { mode: 'owner' as const }, id: 'c' },
    ]
    const { own, shared } = partitionJobsByAccess(jobs)
    expect(own.map((job) => job.id)).toEqual(['a', 'c'])
    expect(shared.map((job) => job.id)).toEqual(['b'])
  })

  it('returns empty shared group when nothing is shared in', () => {
    expect(partitionJobsByAccess([{ access: undefined, id: 'a' }]).shared).toEqual([])
  })
})

describe('canCancelWithAccess', () => {
  it('keeps owned runs cancellable', () => {
    expect(canCancelWithAccess(undefined)).toBe(true)
  })

  it('mirrors the server rule: view cannot cancel, edit can', () => {
    expect(canCancelWithAccess({ mode: 'shared', permission: 'view' })).toBe(false)
    expect(canCancelWithAccess({ mode: 'shared', permission: 'edit' })).toBe(true)
    expect(canCancelWithAccess({ mode: 'owner' })).toBe(true)
  })
})

describe('toggleSelectedUser', () => {
  it('adds unknown users and removes already-selected ones', () => {
    const once = toggleSelectedUser([], user('u1'))
    expect(once.map((entry) => entry.id)).toEqual(['u1'])
    expect(toggleSelectedUser(once, user('u1'))).toEqual([])
  })
})

describe('selectableSearchResults', () => {
  it('hides users who already hold a share or are picked', () => {
    const results = [user('u1'), user('u2')]
    expect(
      selectableSearchResults(results, new Set(['u1'])).map((entry) => entry.id),
    ).toEqual(['u2'])
  })
})

describe('personLabel', () => {
  it('prefers the display name, then email, then the fallback', () => {
    expect(personLabel('Alice B', 'a@example.com', 'sub')).toBe('Alice B')
    expect(personLabel('  ', 'a@example.com', 'sub')).toBe('a@example.com')
    expect(personLabel(null, null, 'sub')).toBe('sub')
  })
})

describe('outgoingShareCounts', () => {
  it('derives active counts for one resource type from /shares/mine rows', () => {
    expect(outgoingShareCounts([
      {
        pending_count: 1,
        resource_id: 'run_1',
        resource_title: 'Run',
        resource_type: 'run',
        share_count: 2,
      },
      {
        pending_count: 0,
        resource_id: 'skill_1',
        resource_title: 'Skill',
        resource_type: 'skill_template',
        share_count: 1,
      },
    ], 'run')).toEqual({ run_1: 2 })
  })
})

describe('resource-specific sharing policy', () => {
  it('offers suggest only for editor documents', () => {
    expect(sharePermissionsForResource('editor_document')).toEqual([
      'view',
      'suggest',
      'edit',
    ])
    for (const resourceType of [
      'run',
      'knowledge_collection',
      'prompt_template',
      'skill_template',
    ]) {
      expect(sharePermissionsForResource(resourceType)).toEqual(['view', 'edit'])
    }
  })

  it('labels suggest and routes accepted editor shares into the editor', () => {
    expect(sharePermissionLabel('suggest', 'en', {
      edit: 'Edit',
      view: 'View',
    })).toBe('Suggest')
    expect(sharedResourceDestination('editor_document')).toBe('editor')
    expect(sharedResourceDestination('prompt_template')).toBe('prompt-library')
  })
})
