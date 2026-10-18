import type { QuotaDimensionUsage } from './model'

/** Plausible seed so the meter showcases the feature in demo mode.

 * LLM tokens is deliberately the binding constraint (warning band, the
 * worst fraction the ring follows); embedding tokens is limited but
 * comfortably below it (so the Database index bar shows a healthy green
 * fraction); stored bytes is a stock level — enough to exercise every
 * visual state without a backend.
 */
export function seedQuotaUsage(nowSeconds: number): QuotaDimensionUsage[] {
  const nextMonth = nowSeconds + 18 * 24 * 3600
  return [
    {
      dimension: 'runs',
      used: 12,
      limit: 50,
      remaining: 38,
      period_start: nowSeconds,
      reset_at: nextMonth,
    },
    {
      dimension: 'llm_tokens',
      used: 812_000,
      limit: 1_000_000,
      remaining: 188_000,
      period_start: nowSeconds,
      reset_at: nextMonth,
    },
    {
      dimension: 'embedding_tokens',
      used: 900_000,
      limit: 2_000_000,
      remaining: 1_100_000,
      period_start: nowSeconds,
      reset_at: nextMonth,
    },
    {
      dimension: 'stored_bytes',
      used: 47_185_920,
      limit: 524_288_000,
      remaining: 477_102_080,
      period_start: 0,
      reset_at: 0,
    },
  ]
}
