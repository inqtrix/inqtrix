import { formatBytes, formatTokens } from '@/lib/modelCard'

/** Display order of the metered dimensions (mirrors the backend enum). */
export const QUOTA_DIMENSION_ORDER = [
  'runs',
  'llm_tokens',
  'embedding_tokens',
  'stored_bytes',
] as const

export type QuotaDimensionKey = (typeof QUOTA_DIMENSION_ORDER)[number]

/** One row from `GET /v1/quota/usage` (the caller's own meter). */
export type QuotaDimensionUsage = {
  dimension: string
  used: number
  /** Effective limit, or `null` for unlimited. */
  limit: number | null
  /** Amount left, or `null` for unlimited. */
  remaining: number | null
  period_start: number
  /** Window reset (unix seconds); `0` for the stock dimension. */
  reset_at: number
}

export type QuotaMeterThreshold = 'ok' | 'warning' | 'critical' | 'unknown'

export type QuotaMeterDimension = {
  key: QuotaDimensionKey
  used: number
  limit: number | null
  /** `used / limit`, or `null` when unlimited. */
  fraction: number | null
  resetAt: number
  /** Stored bytes is a stock level (no monthly reset). */
  isStock: boolean
}

export type QuotaMeterModel = {
  /** Worst (most-constrained) dimension's threshold; drives the ring hue. */
  threshold: QuotaMeterThreshold
  /** The binding dimension's `used / limit`, or `null` when nothing is limited. */
  worstFraction: number | null
  dimensions: QuotaMeterDimension[]
}

const WARNING_AT = 0.75
const CRITICAL_AT = 0.9

const STOCK_KEYS: ReadonlySet<QuotaDimensionKey> = new Set(['stored_bytes'])

/** Build the meter view-model from the raw usage rows.

 * Pure and order-stable: every dimension gets a row (missing ones read as
 * 0 / unlimited). The ring/threshold follow the single most-constrained
 * dimension — the one that actually decides whether the next action is
 * blocked — so a healthy meter stays calm even when one quota is tight.
 */
export function buildQuotaMeterModel(
  rows: readonly QuotaDimensionUsage[],
): QuotaMeterModel {
  const byKey = new Map(rows.map((row) => [row.dimension, row]))
  const dimensions = QUOTA_DIMENSION_ORDER.map((key): QuotaMeterDimension => {
    const row = byKey.get(key)
    const used = row?.used ?? 0
    const limit = row?.limit ?? null
    return {
      key,
      used,
      limit,
      fraction: limit != null && limit > 0 ? used / limit : null,
      resetAt: row?.reset_at ?? 0,
      isStock: STOCK_KEYS.has(key),
    }
  })
  const fractions = dimensions
    .map((dimension) => dimension.fraction)
    .filter((fraction): fraction is number => fraction != null)
  const worstFraction = fractions.length ? Math.max(...fractions) : null
  return {
    dimensions,
    threshold: thresholdFor(worstFraction),
    worstFraction,
  }
}

/** One picked dimension for the persistent rail usage footer (the quiet
 * Database-style glance reused across Knowledge/Chat/Editor). Generic over the
 * key so the caller's narrow dimension union survives into the result (the
 * footer maps each key to a label/icon and must keep the narrow type). */
export type FooterSection<K extends QuotaDimensionKey = QuotaDimensionKey> = {
  dimension: K
  used: number
  limit: number | null
  /** Flow-window start (unix seconds); ``0`` -> no month label. */
  periodStart: number
}

/** Pick the requested dimensions out of the raw usage rows, in the caller's
 * order, for the rail usage footer.
 *
 * Pure (no locale, no DOM): a missing row reads as ``used = 0`` /
 * ``limit = null`` (unlimited), exactly like {@link buildQuotaMeterModel}, so
 * the *content* of an empty account is well-defined. A failed LOAD is a
 * separate concern the footer handles via ``state.status`` — it must NOT reuse
 * this empty-rows path, or a failed fetch would masquerade as a healthy
 * unlimited account (no silent fallback). ``fraction`` /
 * ``exhausted`` are deliberately NOT computed here: the bar's threshold/banding
 * has a single owner downstream (the section primitive over
 * {@link quotaBarFractionClass}/{@link quotaBarWidth}).
 */
export function buildFooterSections<K extends QuotaDimensionKey>(
  rows: readonly QuotaDimensionUsage[],
  dimensions: readonly K[],
): FooterSection<K>[] {
  const byKey = new Map(rows.map((row) => [row.dimension, row]))
  return dimensions.map((dimension): FooterSection<K> => {
    const row = byKey.get(dimension)
    return {
      dimension,
      limit: row?.limit ?? null,
      periodStart: row?.period_start ?? 0,
      used: row?.used ?? 0,
    }
  })
}

function thresholdFor(fraction: number | null): QuotaMeterThreshold {
  if (fraction == null) return 'unknown'
  if (fraction >= CRITICAL_AT) return 'critical'
  if (fraction >= WARNING_AT) return 'warning'
  return 'ok'
}

/** A utilisation bar's fill colour by its own fraction — the one place the
 * green/amber/red banding lives, shared by the meter, the admin table and
 * the index bar. ``null`` (unlimited) reads as inert. */
export function quotaBarFractionClass(fraction: number | null): string {
  if (fraction == null) return 'bg-muted-foreground/40'
  if (fraction >= CRITICAL_AT) return 'bg-destructive'
  if (fraction >= WARNING_AT) return 'bg-warning'
  return 'bg-success'
}

/** A utilisation bar's fill WIDTH in percent — the colour's inseparable
 * partner (same three call sites). ``null`` (unlimited) -> 0; otherwise a 3%
 * floor keeps a sliver visible and 100% is the ceiling. */
export function quotaBarWidth(fraction: number | null): number {
  return fraction == null ? 0 : Math.max(3, Math.min(100, fraction * 100))
}

export type QuotaFooter =
  | { kind: 'exceeded' }
  | { kind: 'reset'; resetAt: number }
  | { kind: 'stock' }

/** The meter's footer line, decided purely so it is unit-testable.

 * Precedence: a crossed limit (worst fraction at/over 1) is the headline;
 * otherwise the earliest upcoming flow-window reset; otherwise (only stock
 * or unlimited dimensions) the stock hint.
 */
export function quotaFooter(model: QuotaMeterModel): QuotaFooter {
  if (model.worstFraction != null && model.worstFraction >= 1) {
    return { kind: 'exceeded' }
  }
  const nextReset = model.dimensions
    .filter((dimension) => !dimension.isStock && dimension.resetAt > 0)
    .map((dimension) => dimension.resetAt)
    .sort((a, b) => a - b)[0]
  return nextReset ? { kind: 'reset', resetAt: nextReset } : { kind: 'stock' }
}

/** Format a dimension's amount in its natural unit (count / tokens / bytes). */
export function formatQuotaAmount(key: QuotaDimensionKey, amount: number): string {
  if (key === 'stored_bytes') return formatBytes(amount)
  return formatTokens(amount)
}

const LIMIT_SUFFIX_MULTIPLIERS: Readonly<Record<string, number>> = {
  b: 1_000_000_000,
  k: 1_000,
  m: 1_000_000,
}

/** Parse a limit-field draft into a non-negative integer, or ``null``.

 * ``null`` means "not a valid limit" — blank, non-numeric, or negative. A
 * valid value is floored (limits are whole units; ``0`` is the explicit
 * "unlimited" the backend accepts). Plain numeric drafts keep the previous
 * `Number(...)` behavior; compact token suffixes (`k`, `m`, `b`) are accepted
 * because the admin table displays large inherited defaults in that format.
 * One parser for both limit-entry surfaces (the inline editor and the
 * add-override field) so they agree.
 */
export function parseLimitValue(raw: string): number | null {
  const trimmed = raw.trim()
  if (trimmed === '') return null
  const numeric = Number(trimmed)
  if (Number.isFinite(numeric) && numeric >= 0) return Math.floor(numeric)
  const match = trimmed.match(/^(\d+(?:\.\d+)?)([kmb])?$/i)
  if (!match) return null
  const [, amount, suffix] = match
  if (!amount) return null
  const multiplier = suffix
    ? (LIMIT_SUFFIX_MULTIPLIERS[suffix.toLowerCase()] ?? null)
    : 1
  if (multiplier == null) return null
  const parsed = Number(amount) * multiplier
  if (!Number.isFinite(parsed) || parsed < 0) return null
  return Math.floor(parsed)
}

export type LimitDraftAction =
  | { kind: 'clear' }
  | { kind: 'commit'; value: number }
  | { kind: 'noop' }

/** Decide what an inline limit edit should do, given the committed draft
 * and the *current* stored value.

 * Blank clears an existing override (or no-ops when none was set); an
 * invalid/negative draft no-ops (the field reverts); a valid value
 * commits only when it actually changed.
 */
export function limitDraftAction(
  draft: string,
  current: number | null,
): LimitDraftAction {
  if (draft.trim() === '') {
    return current != null ? { kind: 'clear' } : { kind: 'noop' }
  }
  const parsed = parseLimitValue(draft)
  if (parsed == null) return { kind: 'noop' }
  return parsed === current ? { kind: 'noop' } : { kind: 'commit', value: parsed }
}

/** Whether the meter may fetch again now (one rule for every automatic trigger).

 * The meter refreshes on a slow cadence and when its tab becomes visible
 * again. Both triggers ask this rule, so two automatic requests of one tab
 * are always at least *refreshMs* apart however often the tab flips
 * visibility, and never overlap. The interval counts from the last
 * ATTEMPT, not the last success, so a failing endpoint is retried on the
 * same slow cadence instead of on every tab switch.
 */
export function quotaRefreshDue({
  inFlight,
  lastAttemptAt,
  now,
  refreshMs,
}: {
  inFlight: boolean
  lastAttemptAt: number | null
  now: number
  refreshMs: number
}): boolean {
  if (inFlight) return false
  return lastAttemptAt === null || now - lastAttemptAt >= refreshMs
}
