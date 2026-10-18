import { describe, expect, it } from 'vitest'
import {
  bytesToGbField,
  cellFraction,
  filterAdminSubjects,
  GIB,
  gbToBytes,
  parseStorageGb,
  QUOTA_DEFAULT_USER_ID,
  quotaAdminAvailable,
  quotaPeriodReset,
  quotaPeriodStart,
  seedQuotaAdminSnapshot,
  sortAdminSubjects,
  storageDraftAction,
  subjectStatus,
} from './admin'

describe('quotaAdminAvailable (instance-admin axis)', () => {
  // Availability follows the instance-admin role and
  // the quota capability, NEVER workspace ownership (ownership is not even an
  // input). A regression re-coupling availability to ownership would need to
  // add a parameter here, which this suite would surface.
  it('is true for an instance admin when the quota gate is enabled', () => {
    expect(
      quotaAdminAvailable({ demo: false, enabled: true }, true),
    ).toBe(true)
  })

  it('is false for a non-admin even when the gate is enabled', () => {
    expect(
      quotaAdminAvailable({ demo: false, enabled: true }, false),
    ).toBe(false)
  })

  it('is false when the quota capability is off, admin or not', () => {
    expect(quotaAdminAvailable({ demo: false, enabled: false }, true)).toBe(
      false,
    )
    expect(quotaAdminAvailable({ demo: true, enabled: false }, false)).toBe(
      false,
    )
  })

  it('is true in demo (enabled gate) regardless of the role', () => {
    expect(quotaAdminAvailable({ demo: true, enabled: true }, false)).toBe(
      true,
    )
  })
})

describe('seedQuotaAdminSnapshot (demo)', () => {
  const snapshot = seedQuotaAdminSnapshot(1_700_000_000)

  it('exposes every dimension with ceilings and env defaults', () => {
    for (const dim of ['runs', 'llm_tokens', 'embedding_tokens', 'stored_bytes']) {
      expect(snapshot.dimensions).toContain(dim)
      expect(snapshot.ceilings[dim]).toBeGreaterThan(0)
      expect(snapshot.env_defaults).toHaveProperty(dim)
    }
    expect(snapshot.stock_dimensions).toEqual(['stored_bytes'])
  })

  it('includes subjects with usage and at least one override', () => {
    expect(snapshot.subjects.length).toBeGreaterThanOrEqual(2)
    const withOverride = snapshot.subjects.find((s) =>
      Object.values(s.dimensions).some((cell) => cell.override != null),
    )
    expect(withOverride).toBeDefined()
    // Enriched display fields the admin table renders.
    expect(snapshot.subjects[0].display_name).toBeTruthy()
    expect(snapshot.subjects[0].email).toBeTruthy()
  })

  it('does not surface the tenant-default sentinel as a subject', () => {
    expect(snapshot.subjects.some((s) => s.user_id === QUOTA_DEFAULT_USER_ID)).toBe(
      false,
    )
  })
})

// 2024-06-13T00:00:00Z — a fixed flow-window start to assert period maths.
const JUNE = Math.floor(Date.UTC(2024, 5, 13) / 1000)

describe('quota period', () => {
  it('reads the active window from a flow cell', () => {
    const snapshot = seedQuotaAdminSnapshot(JUNE)
    expect(quotaPeriodStart(snapshot)).toBe(JUNE)
  })

  it('falls back to the current UTC month with no subjects', () => {
    const snapshot = { ...seedQuotaAdminSnapshot(JUNE), subjects: [] }
    const start = quotaPeriodStart(snapshot)
    const d = new Date(start * 1000)
    expect(d.getUTCDate()).toBe(1)
  })

  it('resets at the first of the following month', () => {
    const reset = quotaPeriodReset(JUNE)
    const d = new Date(reset * 1000)
    expect(d.getUTCFullYear()).toBe(2024)
    expect(d.getUTCMonth()).toBe(6) // July
    expect(d.getUTCDate()).toBe(1)
  })

  it('rolls the year over at December', () => {
    const dec = Math.floor(Date.UTC(2024, 11, 1) / 1000)
    const d = new Date(quotaPeriodReset(dec) * 1000)
    expect(d.getUTCFullYear()).toBe(2025)
    expect(d.getUTCMonth()).toBe(0)
  })
})

describe('subjectStatus', () => {
  const snapshot = seedQuotaAdminSnapshot(JUNE)
  const byName = (name: string) =>
    snapshot.subjects.find((s) => s.display_name === name)!

  it('flags an override as a custom limit', () => {
    expect(subjectStatus(byName('Rita Recipient'), snapshot).custom).toBe(true)
    expect(subjectStatus(byName('Olga Owner'), snapshot).custom).toBe(false)
  })

  it('flags a reached limit as exhausted', () => {
    expect(subjectStatus(byName('Stefan Schulz'), snapshot).exhausted).toBe(true)
    expect(subjectStatus(byName('Olga Owner'), snapshot).exhausted).toBe(false)
  })
})

describe('cellFraction', () => {
  it('is used/limit, null when unlimited or missing', () => {
    expect(cellFraction({ limit: 100, override: null, period_start: 0, used: 50 })).toBe(0.5)
    expect(cellFraction({ limit: 0, override: null, period_start: 0, used: 5 })).toBeNull()
    expect(cellFraction(undefined)).toBeNull()
  })
})

describe('storage GB codec', () => {
  it('formats whole and sub-GiB values without collapsing to 0', () => {
    expect(bytesToGbField(2 * GIB)).toBe('2')
    expect(bytesToGbField(null)).toBe('') // inherit
    expect(bytesToGbField(0)).toBe('0') // explicit unlimited
    // 500 MiB must NOT round to '0' (which would read as unlimited).
    expect(bytesToGbField(524_288_000)).toBe('0.49')
  })

  it('parses GB (decimals allowed) into bytes', () => {
    expect(parseStorageGb('2')).toBe(2 * GIB)
    expect(parseStorageGb('0.5')).toBe(gbToBytes(0.5))
    expect(parseStorageGb('0')).toBe(0)
    expect(parseStorageGb('')).toBeNull()
    expect(parseStorageGb('  ')).toBeNull()
    expect(parseStorageGb('abc')).toBeNull()
    expect(parseStorageGb('-1')).toBeNull()
  })

  it('drives the same clear/commit/noop semantics as the count field', () => {
    expect(storageDraftAction('', 2 * GIB)).toEqual({ kind: 'clear' })
    expect(storageDraftAction('', null)).toEqual({ kind: 'noop' })
    expect(storageDraftAction('2', 2 * GIB)).toEqual({ kind: 'noop' })
    expect(storageDraftAction('3', 2 * GIB)).toEqual({
      kind: 'commit',
      value: 3 * GIB,
    })
    // Unchanged sub-GiB draft (does not round-trip exactly) is a no-op,
    // not a spurious re-write.
    expect(storageDraftAction('0.49', 524_288_000)).toEqual({ kind: 'noop' })
    expect(storageDraftAction('abc', 2 * GIB)).toEqual({ kind: 'noop' })
  })
})

describe('filterAdminSubjects', () => {
  const snapshot = seedQuotaAdminSnapshot(JUNE)
  it('matches name, email and user id, blank returns all', () => {
    expect(filterAdminSubjects(snapshot.subjects, 'rita').map((s) => s.user_id)).toEqual([
      '00000000-0000-4000-8000-000000000002',
    ])
    expect(filterAdminSubjects(snapshot.subjects, 'example.com')).toHaveLength(3)
    expect(filterAdminSubjects(snapshot.subjects, '   ')).toHaveLength(3)
    expect(filterAdminSubjects(snapshot.subjects, 'nobody')).toHaveLength(0)
  })
})

describe('sortAdminSubjects', () => {
  const snapshot = seedQuotaAdminSnapshot(JUNE)
  const names = (sort: Parameters<typeof sortAdminSubjects>[1]) =>
    sortAdminSubjects(snapshot.subjects, sort).map((s) => s.display_name)

  it('sorts by name in both directions', () => {
    expect(names({ dir: 'asc', key: 'name' })).toEqual([
      'Olga Owner',
      'Rita Recipient',
      'Stefan Schulz',
    ])
    expect(names({ dir: 'desc', key: 'name' })).toEqual([
      'Stefan Schulz',
      'Rita Recipient',
      'Olga Owner',
    ])
  })

  it('sorts a metric column high to low by raw usage', () => {
    // runs used: Olga 142, Stefan 200, Rita 320
    expect(names({ dir: 'desc', key: 'runs' })).toEqual([
      'Rita Recipient',
      'Stefan Schulz',
      'Olga Owner',
    ])
    expect(names({ dir: 'asc', key: 'runs' })).toEqual([
      'Olga Owner',
      'Stefan Schulz',
      'Rita Recipient',
    ])
  })

  it('does not mutate the input array', () => {
    const before = snapshot.subjects.map((s) => s.user_id)
    sortAdminSubjects(snapshot.subjects, { dir: 'desc', key: 'runs' })
    expect(snapshot.subjects.map((s) => s.user_id)).toEqual(before)
  })
})
