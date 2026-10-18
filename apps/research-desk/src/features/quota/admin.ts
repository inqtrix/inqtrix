import type { LimitDraftAction, QuotaDimensionKey } from './model'

/** One workspace membership row from `GET /v1/workspaces`. */
export type WorkspaceMembership = {
  workspace_id: string
  name: string
  role: 'viewer' | 'commenter' | 'editor' | 'owner'
}

/** Per-dimension cell of an admin subject row (backend admin_snapshot). */
export type QuotaAdminCell = {
  used: number
  /** Raw per-user override (``null`` = none set). */
  override: number | null
  /** Resolved effective limit (``null`` = unlimited). */
  limit: number | null
  period_start: number
}

export type QuotaAdminSubject = {
  user_id: string
  display_name?: string | null
  email?: string | null
  dimensions: Record<string, QuotaAdminCell>
}

/** `GET /v1/admin/quota` — the instance-admin overview. */
export type QuotaAdminSnapshot = {
  dimensions: string[]
  stock_dimensions: string[]
  /** Operator ceiling per dimension (``0`` = no ceiling). */
  ceilings: Record<string, number>
  env_defaults: Record<string, number>
  /** Admin-set tenant default per dimension (``null`` = unset). */
  tenant_default: Record<string, number | null>
  subjects: QuotaAdminSubject[]
}

/** Whether the caller may administer quotas.
 *
 * The single decoupling rule (kept here as a pure, testable function): quota
 * administration is tenant-wide platform administration, so availability is
 * the instance-admin role gated by the quota capability — never workspace
 * ownership. ``gate`` carries the shared meter gate (``enabled`` = oidc +
 * ``capabilities.quota`` + session; ``demo``). Workspace ownership is
 * deliberately NOT an input.
 */
export function quotaAdminAvailable(
  gate: { enabled: boolean; demo: boolean },
  instanceAdmin: boolean,
): boolean {
  return gate.enabled && (gate.demo || instanceAdmin)
}

/** Sentinel the admin API uses for the tenant-wide "for all" default. */
export const QUOTA_DEFAULT_USER_ID = 'default'

/** Bytes per GiB — the storage fields edit in GB, the API stores bytes. */
export const GIB = 1024 ** 3

/** The active flow window (unix seconds) the snapshot reports, or the
 * current UTC month when there are no metered subjects yet. */
export function quotaPeriodStart(snapshot: QuotaAdminSnapshot): number {
  const flow = snapshot.dimensions.filter(
    (dim) => !snapshot.stock_dimensions.includes(dim),
  )
  for (const subject of snapshot.subjects) {
    for (const dim of flow) {
      const start = subject.dimensions[dim]?.period_start
      if (start && start > 0) return start
    }
  }
  const now = new Date()
  return Math.floor(
    Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), 1) / 1000,
  )
}

/** Start of the month after *periodStart* (the flow-window reset moment). */
export function quotaPeriodReset(periodStart: number): number {
  const start = new Date(periodStart * 1000)
  return Math.floor(
    Date.UTC(start.getUTCFullYear(), start.getUTCMonth() + 1, 1) / 1000,
  )
}

/** Row-level flags: has a per-user override, and/or has hit a limit. */
export function subjectStatus(
  subject: QuotaAdminSubject,
  snapshot: QuotaAdminSnapshot,
): { custom: boolean; exhausted: boolean } {
  let custom = false
  let exhausted = false
  for (const dim of snapshot.dimensions) {
    const cell = subject.dimensions[dim]
    if (!cell) continue
    if (cell.override != null) custom = true
    if (cell.limit != null && cell.limit > 0 && cell.used >= cell.limit) {
      exhausted = true
    }
  }
  return { custom, exhausted }
}

/** Utilisation 0..1+ for sorting/colouring a cell (``null`` = unlimited). */
export function cellFraction(cell: QuotaAdminCell | undefined): number | null {
  if (!cell || cell.limit == null || cell.limit <= 0) return null
  return cell.used / cell.limit
}

/** The storage fields edit in GB; the API speaks bytes. The field shows up
 * to two decimals so a sub-GiB cap never collapses to ``0`` (which is the
 * "unlimited" sentinel) — i.e. a real limit is never displayed as unlimited.
 * ``null`` -> '' (inherit), ``0`` -> '0' (explicit unlimited). */
export function bytesToGbField(bytes: number | null): string {
  if (bytes == null) return ''
  if (bytes <= 0) return '0'
  return String(Math.round((bytes / GIB) * 100) / 100)
}

export function gbToBytes(gb: number): number {
  return gb <= 0 ? 0 : Math.round(gb * GIB)
}

/** Parse a GB field draft into bytes (the storage analog of
 * {@link parseLimitValue}): blank/non-numeric/negative -> ``null``; decimals
 * allowed; ``0`` -> ``0`` (explicit unlimited). */
export function parseStorageGb(raw: string): number | null {
  const trimmed = raw.trim()
  if (!trimmed) return null
  const gb = Number(trimmed)
  if (!Number.isFinite(gb) || gb < 0) return null
  return gbToBytes(gb)
}

/** The storage analog of {@link import('./model').limitDraftAction}, in bytes.
 * The unchanged-string short-circuit avoids a spurious re-write when a value
 * that does not round-trip exactly (e.g. 500 MiB -> "0.49") is left untouched. */
export function storageDraftAction(
  draft: string,
  currentBytes: number | null,
): LimitDraftAction {
  const trimmed = draft.trim()
  if (trimmed === '') return currentBytes == null ? { kind: 'noop' } : { kind: 'clear' }
  if (trimmed === bytesToGbField(currentBytes)) return { kind: 'noop' }
  const value = parseStorageGb(trimmed)
  if (value == null) return { kind: 'noop' }
  return value === currentBytes ? { kind: 'noop' } : { kind: 'commit', value }
}

/** The label a row sorts/searches by — display name, else email, else user id. */
function subjectLabel(subject: QuotaAdminSubject): string {
  return (subject.display_name || subject.email || subject.user_id).toLowerCase()
}

/** Case-insensitive substring filter over name, email and canonical user id. */
export function filterAdminSubjects(
  subjects: ReadonlyArray<QuotaAdminSubject>,
  query: string,
): QuotaAdminSubject[] {
  const needle = query.trim().toLowerCase()
  if (!needle) return [...subjects]
  return subjects.filter(
    (subject) =>
      subjectLabel(subject).includes(needle) ||
      (subject.email ?? '').toLowerCase().includes(needle) ||
      subject.user_id.toLowerCase().includes(needle),
  )
}

export type QuotaAdminSortKey = 'name' | QuotaDimensionKey
export type QuotaAdminSort = { key: QuotaAdminSortKey; dir: 'asc' | 'desc' }

/** Stable sort by member name or by a column's raw usage (the visible left
 * number) — the "high to low per category" the admin asked for. Returns a
 * new array; the input order is preserved as the tie-breaker. */
export function sortAdminSubjects(
  subjects: ReadonlyArray<QuotaAdminSubject>,
  sort: QuotaAdminSort,
): QuotaAdminSubject[] {
  const sign = sort.dir === 'asc' ? 1 : -1
  return subjects
    .map((subject, index) => ({ index, subject }))
    .sort((a, b) => {
      let delta: number
      if (sort.key === 'name') {
        delta = subjectLabel(a.subject).localeCompare(subjectLabel(b.subject))
      } else {
        const used = (s: QuotaAdminSubject) => s.dimensions[sort.key]?.used ?? 0
        delta = used(a.subject) - used(b.subject)
      }
      return delta !== 0 ? delta * sign : a.index - b.index
    })
    .map((entry) => entry.subject)
}

/** A plausible admin snapshot for demo mode (no backend) — exercises the
 * standard / custom-limit / exhausted row states the panel renders. */
export function seedQuotaAdminSnapshot(nowSeconds: number): QuotaAdminSnapshot {
  const month = nowSeconds
  const cell = (
    used: number,
    override: number | null,
    limit: number | null,
  ): QuotaAdminCell => ({ limit, override, period_start: month, used })
  const dims: QuotaDimensionKey[] = [
    'runs',
    'llm_tokens',
    'embedding_tokens',
    'stored_bytes',
  ]
  // env operator ceiling >= tenant default (these mirror the screenshot).
  const def = { embedding_tokens: 2_000_000, llm_tokens: 5_000_000, runs: 200, stored_bytes: 2 * GIB }
  return {
    ceilings: {
      embedding_tokens: 5_000_000,
      llm_tokens: 10_000_000,
      runs: 500,
      stored_bytes: 10 * GIB,
    },
    dimensions: dims,
    env_defaults: def,
    stock_dimensions: ['stored_bytes'],
    subjects: [
      {
        // Owner, all standard limits, healthy.
        dimensions: {
          embedding_tokens: cell(900_000, null, def.embedding_tokens),
          llm_tokens: cell(3_100_000, null, def.llm_tokens),
          runs: cell(142, null, def.runs),
          stored_bytes: cell(640_000_000, null, def.stored_bytes),
        },
        display_name: 'Olga Owner',
        email: 'olga@example.com',
        user_id: '00000000-0000-4000-8000-000000000001',
      },
      {
        // Custom run/storage limit, runs in the warning band.
        dimensions: {
          embedding_tokens: cell(1_200_000, null, def.embedding_tokens),
          llm_tokens: cell(4_600_000, null, def.llm_tokens),
          runs: cell(320, 400, 400),
          stored_bytes: cell(1_400_000_000, 3 * GIB, 3 * GIB),
        },
        display_name: 'Rita Recipient',
        email: 'rita@example.com',
        user_id: '00000000-0000-4000-8000-000000000002',
      },
      {
        // Runs exhausted.
        dimensions: {
          embedding_tokens: cell(1_800_000, null, def.embedding_tokens),
          llm_tokens: cell(2_700_000, null, def.llm_tokens),
          runs: cell(200, null, def.runs),
          stored_bytes: cell(420_000_000, null, def.stored_bytes),
        },
        display_name: 'Stefan Schulz',
        email: 'stefan@example.com',
        user_id: '00000000-0000-4000-8000-000000000003',
      },
    ],
    tenant_default: {
      embedding_tokens: null,
      llm_tokens: null,
      runs: null,
      stored_bytes: null,
    },
  }
}
