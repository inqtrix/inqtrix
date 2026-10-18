import { describe, expect, it } from 'vitest'
import { formatBytes } from '@/lib/modelCard'
import { seedQuotaUsage } from './demo'
import {
  buildFooterSections,
  buildQuotaMeterModel,
  formatQuotaAmount,
  limitDraftAction,
  parseLimitValue,
  QUOTA_DIMENSION_ORDER,
  quotaBarFractionClass,
  quotaBarWidth,
  quotaFooter,
  quotaRefreshDue,
  type QuotaDimensionUsage,
} from './model'

function row(
  dimension: string,
  used: number,
  limit: number | null,
  resetAt = 1_000,
): QuotaDimensionUsage {
  return {
    dimension,
    limit,
    period_start: 0,
    remaining: limit == null ? null : Math.max(0, limit - used),
    reset_at: resetAt,
    used,
  }
}

describe('buildQuotaMeterModel', () => {
  it('always returns every dimension in canonical order', () => {
    const model = buildQuotaMeterModel([])
    expect(model.dimensions.map((d) => d.key)).toEqual([
      ...QUOTA_DIMENSION_ORDER,
    ])
    // Missing rows read as 0 / unlimited.
    expect(model.dimensions[0]).toMatchObject({ fraction: null, used: 0 })
    expect(model.threshold).toBe('unknown')
    expect(model.worstFraction).toBeNull()
  })

  it('drives the threshold from the most-constrained dimension', () => {
    const model = buildQuotaMeterModel([
      row('runs', 1, 50), // 2%
      row('llm_tokens', 920, 1000), // 92% -> critical
      row('embedding_tokens', 10, null), // unlimited -> no fraction
    ])
    expect(model.worstFraction).toBeCloseTo(0.92)
    expect(model.threshold).toBe('critical')
  })

  it('uses warning band between 75% and 90%', () => {
    const model = buildQuotaMeterModel([row('runs', 80, 100)])
    expect(model.threshold).toBe('warning')
  })

  it('treats a zero limit as unlimited (no fraction)', () => {
    const model = buildQuotaMeterModel([row('runs', 5, 0)])
    const runs = model.dimensions.find((d) => d.key === 'runs')
    expect(runs?.fraction).toBeNull()
    expect(model.threshold).toBe('unknown')
  })

  it('marks stored_bytes as the only stock dimension', () => {
    const model = buildQuotaMeterModel([])
    const stock = model.dimensions.filter((d) => d.isStock).map((d) => d.key)
    expect(stock).toEqual(['stored_bytes'])
  })

  it('carries the per-dimension reset timestamp through', () => {
    const model = buildQuotaMeterModel([row('runs', 1, 50, 4242)])
    const runs = model.dimensions.find((d) => d.key === 'runs')
    expect(runs?.resetAt).toBe(4242)
  })
})

describe('buildFooterSections', () => {
  it('picks the requested dimensions in caller order, regardless of row order', () => {
    const sections = buildFooterSections(
      [
        row('runs', 12, 50),
        { ...row('llm_tokens', 812_000, 1_000_000), period_start: 222 },
        { ...row('embedding_tokens', 900_000, 2_000_000), period_start: 111 },
      ],
      ['embedding_tokens', 'llm_tokens'],
    )
    expect(sections).toEqual([
      { dimension: 'embedding_tokens', limit: 2_000_000, periodStart: 111, used: 900_000 },
      { dimension: 'llm_tokens', limit: 1_000_000, periodStart: 222, used: 812_000 },
    ])
  })

  it('reads a missing row as an empty/unlimited account (used 0, no limit, no period)', () => {
    // This is the *content* of an empty account; a failed LOAD is a separate
    // concern the footer surfaces via state.status, so the two never collapse.
    expect(buildFooterSections([], ['llm_tokens'])).toEqual([
      { dimension: 'llm_tokens', limit: null, periodStart: 0, used: 0 },
    ])
  })
})

describe('quotaFooter', () => {
  it('reports exceeded when a dimension is at or over its limit', () => {
    const model = buildQuotaMeterModel([row('runs', 50, 50)])
    expect(quotaFooter(model)).toEqual({ kind: 'exceeded' })
  })

  it('picks the earliest flow-window reset, excluding stock', () => {
    const model = buildQuotaMeterModel([
      row('runs', 1, 50, 9000),
      row('llm_tokens', 1, 1000, 5000), // earliest
      { ...row('stored_bytes', 1, 100, 1), reset_at: 1 }, // stock, ignored
    ])
    expect(quotaFooter(model)).toEqual({ kind: 'reset', resetAt: 5000 })
  })

  it('falls back to the stock hint when nothing is limited or windowed', () => {
    const model = buildQuotaMeterModel([row('embedding_tokens', 10, null, 0)])
    expect(quotaFooter(model)).toEqual({ kind: 'stock' })
  })
})

describe('seedQuotaUsage (demo)', () => {
  it('lands in the documented visual states', () => {
    const model = buildQuotaMeterModel(seedQuotaUsage(1_700_000_000))
    // LLM tokens is the binding constraint, in the warning band.
    expect(model.threshold).toBe('warning')
    const llm = model.dimensions.find((d) => d.key === 'llm_tokens')
    expect(llm?.fraction).toBeGreaterThanOrEqual(0.75)
    expect(llm?.fraction).toBeLessThan(0.9)
    // Embedding is limited but healthy (below LLM, so the ring still tracks
    // LLM); storage is stock; never "exceeded" in demo.
    const embed = model.dimensions.find((d) => d.key === 'embedding_tokens')
    expect(embed?.fraction).toBeCloseTo(0.45)
    expect(embed?.fraction ?? 1).toBeLessThan(llm?.fraction ?? 0)
    expect(quotaFooter(model).kind).toBe('reset')
    expect(model.worstFraction).toBeLessThan(1)
  })
})

describe('parseLimitValue', () => {
  it('floors a valid non-negative number', () => {
    expect(parseLimitValue('0')).toBe(0)
    expect(parseLimitValue('12')).toBe(12)
    expect(parseLimitValue('12.9')).toBe(12)
    expect(parseLimitValue('1e3')).toBe(1_000)
  })
  it('accepts compact k/m/b suffixes used in admin limit fields', () => {
    expect(parseLimitValue('1.2k')).toBe(1_200)
    expect(parseLimitValue('5M')).toBe(5_000_000)
    expect(parseLimitValue('0.5b')).toBe(500_000_000)
  })
  it('rejects blank, non-numeric and negative as null', () => {
    expect(parseLimitValue('')).toBeNull()
    expect(parseLimitValue('   ')).toBeNull()
    expect(parseLimitValue('abc')).toBeNull()
    expect(parseLimitValue('-5')).toBeNull()
  })
})

describe('limitDraftAction', () => {
  it('blank clears an existing value, no-ops when none', () => {
    expect(limitDraftAction('', 50)).toEqual({ kind: 'clear' })
    expect(limitDraftAction('', null)).toEqual({ kind: 'noop' })
  })
  it('0 commits explicit unlimited (not a clear)', () => {
    expect(limitDraftAction('0', null)).toEqual({ kind: 'commit', value: 0 })
  })
  it('commits only on an actual change', () => {
    expect(limitDraftAction('50', 50)).toEqual({ kind: 'noop' })
    expect(limitDraftAction('60', 50)).toEqual({ kind: 'commit', value: 60 })
  })
  it('reverts invalid/negative drafts (no-op)', () => {
    expect(limitDraftAction('-5', 50)).toEqual({ kind: 'noop' })
    expect(limitDraftAction('abc', 50)).toEqual({ kind: 'noop' })
  })
})

describe('quotaBarFractionClass', () => {
  it('bands green/amber/red and treats unlimited as inert', () => {
    expect(quotaBarFractionClass(null)).toContain('muted')
    expect(quotaBarFractionClass(0.5)).toBe('bg-success')
    expect(quotaBarFractionClass(0.8)).toBe('bg-warning')
    expect(quotaBarFractionClass(0.95)).toBe('bg-destructive')
    expect(quotaBarFractionClass(1.2)).toBe('bg-destructive')
  })
})

describe('quotaBarWidth', () => {
  it('is 0 when unlimited, floors at 3%, ceils at 100%', () => {
    expect(quotaBarWidth(null)).toBe(0)
    expect(quotaBarWidth(0)).toBe(3) // floor keeps a sliver visible
    expect(quotaBarWidth(0.45)).toBe(45)
    expect(quotaBarWidth(1)).toBe(100)
    expect(quotaBarWidth(1.5)).toBe(100)
  })
})

describe('formatQuotaAmount', () => {
  it('formats bytes for stored_bytes and tokens otherwise', () => {
    expect(formatQuotaAmount('stored_bytes', 1024)).toBe(formatBytes(1024))
    expect(formatQuotaAmount('runs', 1200)).toBe('1.2k')
    expect(formatQuotaAmount('llm_tokens', 2_000_000)).toBe('2M')
  })
})

describe('formatBytes', () => {
  it('scales by 1024 with a unit step', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(1024)).toBe('1 KB')
    expect(formatBytes(47_185_920)).toBe('45 MB')
    expect(formatBytes(524_288_000)).toBe('500 MB')
  })
})

describe('quotaRefreshDue', () => {
  const refreshMs = 30_000

  it('loads at once when nothing was attempted yet', () => {
    expect(quotaRefreshDue({ inFlight: false, lastAttemptAt: null, now: 1_000, refreshMs })).toBe(true)
  })

  it('skips a refresh while the last attempt is younger than the interval', () => {
    // A tab that flips visibility every few seconds must not refetch each time.
    expect(quotaRefreshDue({ inFlight: false, lastAttemptAt: 1_000, now: 11_000, refreshMs })).toBe(false)
    expect(quotaRefreshDue({ inFlight: false, lastAttemptAt: 1_000, now: 30_999, refreshMs })).toBe(false)
  })

  it('refreshes once the last attempt is at least one interval old', () => {
    expect(quotaRefreshDue({ inFlight: false, lastAttemptAt: 1_000, now: 31_000, refreshMs })).toBe(true)
    expect(quotaRefreshDue({ inFlight: false, lastAttemptAt: 1_000, now: 3_600_000, refreshMs })).toBe(true)
  })

  it('never starts a second request while one is in flight', () => {
    expect(quotaRefreshDue({ inFlight: true, lastAttemptAt: null, now: 1_000, refreshMs })).toBe(false)
    expect(quotaRefreshDue({ inFlight: true, lastAttemptAt: 1_000, now: 3_600_000, refreshMs })).toBe(false)
  })
})
