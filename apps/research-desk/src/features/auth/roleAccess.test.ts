import { describe, expect, it } from 'vitest'

import { isAdminRole } from './roleAccess'

describe('isAdminRole', () => {
  it('grants the surface only for the exact admin role', () => {
    expect(isAdminRole('admin')).toBe(true)
  })

  it('is default-closed for user, unknown, missing, and empty roles', () => {
    expect(isAdminRole('user')).toBe(false)
    expect(isAdminRole(null)).toBe(false)
    expect(isAdminRole(undefined)).toBe(false)
    expect(isAdminRole('')).toBe(false)
    expect(isAdminRole('Admin')).toBe(false) // case-sensitive on purpose
    expect(isAdminRole('owner')).toBe(false) // workspace role, not instance
  })
})
