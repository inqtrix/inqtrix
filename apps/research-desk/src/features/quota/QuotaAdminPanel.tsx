import {
  ArrowUpDown,
  CalendarDays,
  ChevronDown,
  ChevronUp,
  Gauge,
  RotateCcw,
  Search,
  Shield,
  X,
} from '@/components/icons'
import { type CSSProperties, useEffect, useRef, useState } from 'react'
import { searchUsers } from '@/api/inqtrixClient'
import { InitialsAvatar } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  OptionMenuHeader,
  OptionMenuItem,
  optionMenuContentClassName,
} from '@/components/ui/option-menu'
import type { UserSearchResult } from '@/features/sharing/types'
import { personLabel } from '@/features/sharing/shareModel'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { cn } from '@/lib/utils'
import {
  bytesToGbField,
  cellFraction,
  filterAdminSubjects,
  parseStorageGb,
  QUOTA_DEFAULT_USER_ID,
  quotaPeriodReset,
  quotaPeriodStart,
  sortAdminSubjects,
  storageDraftAction,
  subjectStatus,
  type QuotaAdminCell,
  type QuotaAdminSnapshot,
  type QuotaAdminSort,
  type QuotaAdminSortKey,
  type QuotaAdminSubject,
} from './admin'
import {
  formatQuotaAmount,
  limitDraftAction,
  parseLimitValue,
  quotaBarFractionClass,
  quotaBarWidth,
  type LimitDraftAction,
  type QuotaDimensionKey,
} from './model'
import { useQuotaAdmin } from './useQuotaAdmin'

const SEARCH_DEBOUNCE_MS = 250
const MIN_QUERY = 2

/** Storage is the one dimension whose fields read in whole GB (bytes are
 * unusable in a form); every other dimension is a raw count of tokens/runs. */
const isStorage = (dim: QuotaDimensionKey): boolean => dim === 'stored_bytes'

const limitFieldFormatters: Record<QuotaDimensionKey, (value: number) => string> = {
  embedding_tokens: (value) => formatQuotaAmount('embedding_tokens', value),
  llm_tokens: (value) => formatQuotaAmount('llm_tokens', value),
  runs: (value) => formatQuotaAmount('runs', value),
  stored_bytes: bytesToGbField,
}

/** Owner-only quota administration, built for scale: a compact member list
 * where a row expands inline to edit that member's limits. The "for all"
 * defaults live as the first dense row of the same grid, so the page reads as
 * one management surface instead of separate setting cards. The owned-workspace
 * + snapshot resolution lives in useQuotaAdmin, lifted to the Settings view so
 * the nav gate and the panel share one instance. */
export function QuotaAdminPanel({
  admin,
}: {
  admin: ReturnType<typeof useQuotaAdmin>
}) {
  const { t, locale } = useLocale()
  const { state } = admin
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState<QuotaAdminSort>({ dir: 'asc', key: 'name' })
  const [expanded, setExpanded] = useState<string | null>(null)

  if (!state.available) {
    return (
      <p className="t-meta text-muted-foreground">{t.quotaAdmin.notAdmin}</p>
    )
  }
  if (state.status === 'error') {
    return <p className="t-meta text-destructive">{t.quota.loadFailed}</p>
  }
  if (!state.snapshot) {
    return <p className="t-meta text-muted-foreground">…</p>
  }

  const snapshot = state.snapshot
  const dims = snapshot.dimensions as QuotaDimensionKey[]
  const dimLabel: Record<string, string> = {
    embedding_tokens: t.quota.dimEmbeddingTokens,
    llm_tokens: t.quota.dimLlmTokens,
    runs: t.quota.dimRuns,
    stored_bytes: t.quota.dimStoredBytes,
  }
  const periodStart = quotaPeriodStart(snapshot)
  const periodReset = quotaPeriodReset(periodStart)
  // period_start/reset are UTC-aligned month boundaries — render them in UTC
  // so a user west of UTC does not see the previous month / wrong reset day.
  const monthLabel = new Date(periodStart * 1000).toLocaleDateString(locale, {
    month: 'long',
    timeZone: 'UTC',
    year: 'numeric',
  })
  const resetLabel = new Date(periodReset * 1000).toLocaleDateString(locale, {
    day: 'numeric',
    month: 'long',
    timeZone: 'UTC',
  })

  const visible = sortAdminSubjects(
    filterAdminSubjects(snapshot.subjects, query),
    sort,
  )
  // One grid template shared by the header and every row so columns align.
  const gridStyle: CSSProperties = {
    gridTemplateColumns: `minmax(8.5rem,1.35fr) minmax(6.5rem,0.7fr) repeat(${dims.length}, minmax(5rem,0.85fr)) 2rem`,
  }

  const toggleSort = (key: QuotaAdminSortKey) =>
    setSort((prev) =>
      prev.key === key
        ? { dir: prev.dir === 'desc' ? 'asc' : 'desc', key }
        : { dir: key === 'name' ? 'asc' : 'desc', key },
    )

  return (
    <div className="grid min-w-0 gap-4">
      <p className="t-meta max-w-2xl text-muted-foreground">
        {t.quotaAdmin.intro}
      </p>

      {state.demo ? (
        <p className="t-meta-sm text-muted-foreground">
          {t.quotaAdmin.demoNote}
        </p>
      ) : null}
      {state.mutationError ? (
        <p className="rounded-md border border-destructive/25 bg-destructive-subtle px-3 py-2 t-meta-sm font-medium text-destructive">
          {t.quotaAdmin.saveFailed}
        </p>
      ) : null}

      <section className="min-w-0">
        <div className="flex flex-wrap items-center gap-2 border-b border-border/70 py-2">
          <div className="relative min-w-0 flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
            <input
              aria-label={t.quotaAdmin.searchUser}
              className="h-8 w-full rounded-md border border-border bg-background pl-8 pr-3 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
              onChange={(event) => setQuery(event.target.value)}
              placeholder={t.quotaAdmin.searchUser}
              type="text"
              value={query}
            />
          </div>
          {state.demo ? (
            <span className="inline-flex h-8 shrink-0 items-center rounded-md border border-brand/30 bg-brand-subtle px-2.5 text-xs font-medium text-brand">
              {t.common.demoMode}
            </span>
          ) : null}
          <span className="inline-flex h-8 shrink-0 items-center rounded-md border border-border bg-surface px-2.5 text-xs text-muted-foreground">
            <CalendarDays className="mr-1.5 icon-sm text-muted-foreground/70" />
            <span className="capitalize">{monthLabel}</span>
          </span>
          <span className="inline-flex h-8 shrink-0 items-center rounded-md border border-border bg-surface px-2.5 text-xs text-muted-foreground">
            {t.quotaAdmin.resetPrefix} {resetLabel}
          </span>
          <span className="ml-auto shrink-0 t-meta-sm text-muted-foreground">
            {countLabel(visible.length, t.quotaAdmin.memberCountOne, t.quotaAdmin.memberCount)}
          </span>
        </div>

        <div className="overflow-x-auto">
          <div className="min-w-[48rem]">
            <div
              className="grid items-center gap-x-2 border-b border-border/70 bg-surface/45 px-3 py-1.5"
              style={gridStyle}
            >
              <SortButton
                active={sort.key === 'name'}
                ariaLabel={sortAriaLabel(t, t.quotaAdmin.colMember, sort.key === 'name', sort.dir)}
                dir={sort.dir}
                label={t.quotaAdmin.colMember}
                onClick={() => toggleSort('name')}
              />
              <span className="t-caption text-muted-foreground">
                {t.adminUsers.colStatus}
              </span>
              {dims.map((dim) => (
                <SortButton
                  active={sort.key === dim}
                  ariaLabel={sortAriaLabel(t, dimLabel[dim], sort.key === dim, sort.dir)}
                  dir={sort.dir}
                  key={dim}
                  label={dimLabel[dim]}
                  onClick={() => toggleSort(dim)}
                />
              ))}
              <span aria-hidden="true" />
            </div>

            <DefaultLimitRow
              admin={admin}
              demo={state.demo}
              dimLabel={dimLabel}
              dims={dims}
              gridStyle={gridStyle}
              snapshot={snapshot}
            />

            {visible.length === 0 ? (
              <p className="px-3 py-6 text-center t-meta text-muted-foreground">
                {query.trim()
                  ? t.quotaAdmin.noMatches
                  : t.quotaAdmin.noSubjects}
              </p>
            ) : (
              <ul className="grid gap-0.5 py-1">
                {visible.map((subject) => (
                  <MemberRow
                    admin={admin}
                    demo={state.demo}
                    dimLabel={dimLabel}
                    dims={dims}
                    expanded={expanded === subject.user_id}
                    gridStyle={gridStyle}
                    key={subject.user_id}
                    onToggle={() =>
                      setExpanded((current) =>
                        current === subject.user_id ? null : subject.user_id,
                      )
                    }
                    snapshot={snapshot}
                    subject={subject}
                  />
                ))}
              </ul>
            )}
          </div>
        </div>

        {!state.demo ? (
          <div className="border-t border-border/70 bg-surface/30 px-3 py-2">
            <AddOverride
              dimLabel={dimLabel}
              dims={dims}
              onPick={(sub, dim, value) => void admin.setLimit(sub, dim, value)}
            />
          </div>
        ) : null}
      </section>

      <EnvCeilingFooter
        dimLabel={dimLabel}
        dims={dims}
        snapshot={snapshot}
      />
    </div>
  )
}

/** The accessible name for a sort header: the column plus its current sort
 * state (the direction is otherwise glyph-only — invisible to AT). */
function sortAriaLabel(
  t: ReturnType<typeof useLocale>['t'],
  label: string,
  active: boolean,
  dir: 'asc' | 'desc',
): string {
  const state = !active
    ? t.quotaAdmin.sortNone
    : dir === 'desc'
      ? t.quotaAdmin.sortDesc
      : t.quotaAdmin.sortAsc
  return `${label}, ${state}`
}

/** A sortable column header: label in the eyebrow role, a direction glyph
 * (aria-hidden) that only colours up when this column drives the sort; the
 * sort state lives in the button's aria-label for AT. */
function SortButton({
  active,
  ariaLabel,
  dir,
  label,
  onClick,
}: {
  active: boolean
  ariaLabel: string
  dir: 'asc' | 'desc'
  label: string
  onClick: () => void
}) {
  return (
    <button
      aria-label={ariaLabel}
      className={cn(
        'flex min-w-0 items-center gap-1 t-caption hover:text-foreground',
        active ? 'text-foreground' : 'text-muted-foreground',
      )}
      onClick={onClick}
      type="button"
    >
      <span className="truncate">{label}</span>
      {active ? (
        dir === 'desc' ? (
          <ChevronDown className="size-3 shrink-0" />
        ) : (
          <ChevronUp className="size-3 shrink-0" />
        )
      ) : (
        <ArrowUpDown className="size-3 shrink-0 opacity-40" />
      )}
    </button>
  )
}

/** One tenant-default row. Storage edits in GB; everything else raw. */
function DefaultLimitRow({
  admin,
  demo,
  dimLabel,
  dims,
  gridStyle,
  snapshot,
}: {
  admin: ReturnType<typeof useQuotaAdmin>
  demo: boolean
  dimLabel: Record<string, string>
  dims: QuotaDimensionKey[]
  gridStyle: CSSProperties
  snapshot: QuotaAdminSnapshot
}) {
  const { t } = useLocale()
  return (
    <div
      className="grid items-center gap-x-2 bg-surface/30 px-3 py-2"
      style={gridStyle}
    >
      <div className="min-w-0">
        <span className="t-list text-foreground">
          {t.quotaAdmin.defaultTitle}
        </span>
        <p className="truncate t-meta-sm text-muted-foreground">
          {t.quotaAdmin.defaultHint}
        </p>
      </div>
      <StatusChip tone="neutral">{t.quotaAdmin.standardLabel}</StatusChip>
      {dims.map((dim) => {
        const storage = isStorage(dim)
        const ceiling = snapshot.ceilings[dim] ?? 0
        const envDefault = snapshot.env_defaults[dim] ?? 0
        const tenantDefault = snapshot.tenant_default[dim] ?? null
        return (
          <div className="min-w-0" key={dim}>
            <LimitInput
              ariaLabel={`${dimLabel[dim]} ${t.quotaAdmin.defaultTitle}`}
              compact
              disabled={demo}
              format={limitFieldFormatters[dim]}
              onClear={() => void admin.clearLimit(QUOTA_DEFAULT_USER_ID, dim)}
              onCommit={(value) =>
                void admin.setLimit(QUOTA_DEFAULT_USER_ID, dim, value)
              }
              parse={storage ? storageDraftAction : limitDraftAction}
              placeholder={
                storage
                  ? bytesToGbField(envDefault)
                  : formatQuotaAmount(dim, envDefault)
              }
              value={tenantDefault}
            />
            <p className="mt-1 truncate t-hint text-muted-foreground/70">
              {ceiling > 0 ? (
                <>
                  {t.quotaAdmin.ceiling}{' '}
                  <span className="t-mono">
                    {formatQuotaAmount(dim, ceiling)}
                  </span>
                </>
              ) : (
                t.quotaAdmin.ceilingNone
              )}
            </p>
          </div>
        )
      })}
      <span aria-hidden="true" />
    </div>
  )
}

function MemberRow({
  admin,
  demo,
  dimLabel,
  dims,
  expanded,
  gridStyle,
  onToggle,
  snapshot,
  subject,
}: {
  admin: ReturnType<typeof useQuotaAdmin>
  demo: boolean
  dimLabel: Record<string, string>
  dims: QuotaDimensionKey[]
  expanded: boolean
  gridStyle: CSSProperties
  onToggle: () => void
  snapshot: QuotaAdminSnapshot
  subject: QuotaAdminSubject
}) {
  const { t } = useLocale()
  const status = subjectStatus(subject, snapshot)
  const flowDims = dims.filter((dim) => !snapshot.stock_dimensions.includes(dim))

  return (
    <li>
      <div
        aria-expanded={expanded}
        className={cn(
          'grid cursor-pointer items-center gap-x-2 rounded-md px-3 py-2 text-left hover:bg-accent/40',
          expanded && 'bg-accent/40',
        )}
        onClick={onToggle}
        onKeyDown={(event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault()
            onToggle()
          }
        }}
        role="button"
        style={gridStyle}
        tabIndex={0}
      >
        <div className="flex min-w-0 items-center gap-2">
          <InitialsAvatar
            displayName={subject.display_name ?? null}
            email={subject.email ?? null}
            size="sm"
          />
          <div className="min-w-0">
            <span className="block truncate t-list text-foreground">
              {personLabel(subject.display_name, subject.email, subject.user_id)}
            </span>
            {subject.email ? (
              <p className="truncate t-meta-sm text-muted-foreground">
                {subject.email}
              </p>
            ) : null}
          </div>
        </div>

        <div className="min-w-0">
          {status.exhausted ? (
            <StatusChip tone="destructive">
              {t.quotaAdmin.badgeExhausted}
            </StatusChip>
          ) : status.custom ? (
            <StatusChip tone="brand">{t.quotaAdmin.badgeCustom}</StatusChip>
          ) : (
            <StatusChip tone="neutral">{t.quotaAdmin.standardLabel}</StatusChip>
          )}
        </div>

        {dims.map((dim) => (
          <MetricCell
            cell={subject.dimensions[dim]}
            dim={dim}
            key={dim}
            unlimitedLabel={t.quota.unlimited}
          />
        ))}

        <div className="flex items-center justify-end">
          <ChevronDown
            className={cn(
              'icon-sm shrink-0 text-muted-foreground transition-transform',
              expanded && 'rotate-180',
            )}
          />
        </div>
      </div>

      {expanded ? (
        <InlineEditor
          ceilings={snapshot.ceilings}
          demo={demo}
          dimLabel={dimLabel}
          dims={dims}
          envDefaults={snapshot.env_defaults}
          flowDims={flowDims}
          onClose={onToggle}
          onResetUsage={() => void admin.resetUsage(subject.user_id, flowDims)}
          onSave={(changes) => void admin.applyOverrides(subject.user_id, changes)}
          subject={subject}
          tenantDefault={snapshot.tenant_default}
        />
      ) : null}
    </li>
  )
}

function StatusChip({
  children,
  tone,
}: {
  children: string
  tone: 'brand' | 'destructive' | 'neutral'
}) {
  return (
    <span
      className={cn(
        'inline-flex h-5 max-w-full justify-self-start rounded-md border px-1.5 t-hint font-medium',
        tone === 'destructive'
          ? 'border-destructive/25 bg-destructive-subtle text-destructive'
          : tone === 'brand'
            ? 'border-brand/25 bg-brand-subtle text-brand'
            : 'border-border bg-background text-muted-foreground',
      )}
    >
      <span className="truncate">{children}</span>
    </span>
  )
}

/** One member×dimension cell: "used / limit" over a thin utilisation bar. */
function MetricCell({
  cell,
  dim,
  unlimitedLabel,
}: {
  cell: QuotaAdminCell | undefined
  dim: QuotaDimensionKey
  unlimitedLabel: string
}) {
  const used = cell?.used ?? 0
  const limit = cell?.limit ?? null
  const fraction = cellFraction(cell)
  const exhausted = fraction != null && fraction >= 1
  const unlimited = limit == null || limit <= 0
  const width = quotaBarWidth(fraction)
  return (
    <div className="min-w-0">
      <p
        className={cn(
          'truncate t-mono',
          exhausted ? 'text-destructive' : 'text-foreground',
        )}
        title={
          unlimited
            ? `${formatQuotaAmount(dim, used)} · ${unlimitedLabel}`
            : `${formatQuotaAmount(dim, used)} / ${formatQuotaAmount(dim, limit)}`
        }
      >
        {formatQuotaAmount(dim, used)}
        <span className="font-normal text-muted-foreground/60">
          {unlimited ? ' · ∞' : ` / ${formatQuotaAmount(dim, limit)}`}
        </span>
      </p>
      <span className="mt-1 block h-1 w-full overflow-hidden rounded-full bg-muted">
        {fraction != null ? (
          <span
            className={cn(
              'block h-full rounded-full',
              quotaBarFractionClass(fraction),
            )}
            style={{ width: `${width}%` }}
          />
        ) : null}
      </span>
    </div>
  )
}

/** The editor's per-dimension draft strings, seeded from the current
 * overrides in display units (GB for storage, raw count otherwise). */
function initialDrafts(
  subject: QuotaAdminSubject,
  dims: QuotaDimensionKey[],
): Record<string, string> {
  const fields: Record<string, string> = {}
  for (const dim of dims) {
    const override = subject.dimensions[dim]?.override ?? null
    fields[dim] = isStorage(dim)
      ? bytesToGbField(override)
      : override == null
        ? ''
        : String(override)
  }
  return fields
}

/** The inline edit panel a row expands into: a draft per dimension, saved
 * as one batch (so the snapshot reloads once), with the effective standard
 * and the operator ceiling stated as the bounds. */
function InlineEditor({
  ceilings,
  demo,
  dimLabel,
  dims,
  envDefaults,
  flowDims,
  onClose,
  onResetUsage,
  onSave,
  subject,
  tenantDefault,
}: {
  ceilings: Record<string, number>
  demo: boolean
  dimLabel: Record<string, string>
  dims: QuotaDimensionKey[]
  envDefaults: Record<string, number>
  flowDims: QuotaDimensionKey[]
  onClose: () => void
  onResetUsage: () => void
  onSave: (
    changes: ReadonlyArray<{ dimension: string; value: number | null }>,
  ) => void
  subject: QuotaAdminSubject
  tenantDefault: Record<string, number | null>
}) {
  const { t } = useLocale()
  // Drafts per dimension, in display units (GB for storage). Re-synced when
  // the subject changes (e.g. after a reload), so the editor never edits a
  // stale value and a freshly opened row starts from server truth.
  const [drafts, setDrafts] = useState<Record<string, string>>(() =>
    initialDrafts(subject, dims),
  )
  useEffect(() => {
    setDrafts(initialDrafts(subject, dims))
  }, [dims, subject])
  const hasOverride = dims.some((dim) => subject.dimensions[dim]?.override != null)

  const save = () => {
    const changes: { dimension: string; value: number | null }[] = []
    for (const dim of dims) {
      // limitDraftAction / storageDraftAction both work in API units
      // (bytes for storage), so action.value is committed directly.
      const override = subject.dimensions[dim]?.override ?? null
      const action: LimitDraftAction = isStorage(dim)
        ? storageDraftAction(drafts[dim] ?? '', override)
        : limitDraftAction(drafts[dim] ?? '', override)
      if (action.kind === 'clear') changes.push({ dimension: dim, value: null })
      else if (action.kind === 'commit')
        changes.push({ dimension: dim, value: action.value })
    }
    if (changes.length > 0) onSave(changes)
    onClose()
  }

  const resetToDefault = () => {
    const changes = dims
      .filter((dim) => subject.dimensions[dim]?.override != null)
      .map((dim) => ({ dimension: dim, value: null as number | null }))
    if (changes.length > 0) onSave(changes)
    onClose()
  }

  return (
    <div className="rounded-md bg-surface/70 px-3 py-3">
      <div className="flex items-center justify-between gap-2">
        <p className="t-label text-foreground">
          {t.quotaAdmin.editTitle(
            personLabel(subject.display_name, subject.email, subject.user_id),
          )}
        </p>
        {!demo && flowDims.length > 0 ? (
          <Button
            className="h-7 gap-1.5 px-2 text-muted-foreground hover:text-foreground"
            onClick={onResetUsage}
            size="sm"
            type="button"
            variant="ghost"
          >
            <RotateCcw className="size-3.5" />
            {t.quotaAdmin.resetUsage}
          </Button>
        ) : null}
      </div>

      <div className="mt-2 grid gap-x-4 gap-y-2.5 sm:grid-cols-2">
        {dims.map((dim) => {
          const storage = isStorage(dim)
          const standard = tenantDefault[dim] ?? envDefaults[dim] ?? 0
          const ceiling = ceilings[dim] ?? 0
          return (
            <div className="grid gap-1" key={dim}>
              <label
                className="t-meta-sm text-muted-foreground"
                htmlFor={`limit-${subject.user_id}-${dim}`}
              >
                {dimLabel[dim]}
                {storage ? ` ${t.quotaAdmin.storageUnit}` : ''}
              </label>
              <input
                className="h-8 w-full rounded-md border border-border bg-background px-2.5 text-sm tabular-nums text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
                disabled={demo}
                id={`limit-${subject.user_id}-${dim}`}
                inputMode="numeric"
                onChange={(event) =>
                  setDrafts((current) => ({
                    ...current,
                    [dim]: event.target.value,
                  }))
                }
                placeholder={t.quotaAdmin.standardPlaceholder(
                  storage ? bytesToGbField(standard) : String(standard),
                )}
                value={drafts[dim] ?? ''}
              />
              <p className="t-hint text-muted-foreground/70">
                {t.quotaAdmin.standardLabel}{' '}
                <span className="t-mono">{formatQuotaAmount(dim, standard)}</span>
                {ceiling > 0 ? (
                  <>
                    {' · '}
                    {t.quotaAdmin.ceiling}{' '}
                    <span className="t-mono">
                      {formatQuotaAmount(dim, ceiling)}
                    </span>
                  </>
                ) : null}
              </p>
            </div>
          )
        })}
      </div>

      <div className="mt-3 flex items-center gap-2">
        <Button
          disabled={demo}
          onClick={save}
          size="sm"
          type="button"
          variant="default"
        >
          {t.quotaAdmin.save}
        </Button>
        <Button onClick={onClose} size="sm" type="button" variant="ghost">
          {t.quotaAdmin.cancel}
        </Button>
        {hasOverride ? (
          <Button
            className="ml-auto text-muted-foreground hover:text-foreground"
            disabled={demo}
            onClick={resetToDefault}
            size="sm"
            type="button"
            variant="ghost"
          >
            {t.quotaAdmin.resetToDefault}
          </Button>
        ) : null}
      </div>
    </div>
  )
}

/** The operator ceiling line: the hard bound above which nothing is settable. */
function EnvCeilingFooter({
  dimLabel,
  dims,
  snapshot,
}: {
  dimLabel: Record<string, string>
  dims: QuotaDimensionKey[]
  snapshot: QuotaAdminSnapshot
}) {
  const { t } = useLocale()
  const entries = dims
    .filter((dim) => (snapshot.ceilings[dim] ?? 0) > 0)
    .map((dim) =>
      isStorage(dim)
        ? formatQuotaAmount(dim, snapshot.ceilings[dim])
        : `${formatQuotaAmount(dim, snapshot.ceilings[dim])} ${dimLabel[dim]}`,
    )
  if (entries.length === 0) return null
  return (
    <p className="flex items-start gap-1.5 px-1 t-hint text-muted-foreground/70">
      <Shield className="mt-px size-3 shrink-0" />
      <span>
        <span className="font-medium">{t.quotaAdmin.envCeilingLabel}:</span>{' '}
        {entries.join(' · ')} — {t.quotaAdmin.envCeilingNote}
      </span>
    </p>
  )
}

/** Number field for one limit (in API units): 0 = unlimited; blank shows the
 * inherited value as placeholder. Commits on blur/Enter; a clear button drops
 * the row so it falls back to the next layer. The format/parse pair is the
 * unit codec — default is a raw count; storage passes the GB codec so the
 * GB<->bytes conversion lives in exactly one place. Both
 * must be STABLE references (module-level), since they drive the reset effect. */
function LimitInput({
  ariaLabel,
  compact = false,
  disabled,
  format = String,
  fullWidth = false,
  onClear,
  onCommit,
  parse = limitDraftAction,
  placeholder,
  value,
}: {
  ariaLabel: string
  compact?: boolean
  disabled: boolean
  format?: (value: number) => string
  fullWidth?: boolean
  onClear: () => void
  onCommit: (value: number) => void
  parse?: (draft: string, current: number | null) => LimitDraftAction
  placeholder: string
  value: number | null
}) {
  const { t } = useLocale()
  const [draft, setDraft] = useState(value == null ? '' : format(value))
  useEffect(() => {
    setDraft(value == null ? '' : format(value))
  }, [format, value])

  const commit = () => {
    const action = parse(draft, value)
    if (action.kind === 'clear') onClear()
    else if (action.kind === 'commit') onCommit(action.value)
    else setDraft(value == null ? '' : format(value)) // revert invalid draft
  }

  return (
    <div className={cn('flex items-center gap-1', fullWidth && 'w-full')}>
      <input
        aria-label={ariaLabel}
        className={cn(
          'rounded-md border border-border bg-background text-right tabular-nums text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50',
          compact
            ? 'h-7 w-16 px-1.5 text-xs disabled:border-border/55 disabled:bg-surface/55 disabled:text-muted-foreground disabled:opacity-100'
            : 'h-8 px-2 text-sm',
          fullWidth && !compact ? 'min-w-0 flex-1' : null,
          !fullWidth && !compact ? 'w-24' : null,
        )}
        disabled={disabled}
        inputMode="numeric"
        onBlur={commit}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Enter') event.currentTarget.blur()
        }}
        placeholder={placeholder}
        value={draft}
      />
      {value != null ? (
        <button
          aria-label={t.quotaAdmin.clearAria}
          className={cn(
            'grid shrink-0 place-items-center rounded text-muted-foreground hover:text-foreground disabled:opacity-50',
            compact ? 'size-4' : 'size-5',
          )}
          disabled={disabled}
          onClick={onClear}
          type="button"
        >
          <X className="size-3" />
        </button>
      ) : compact ? null : (
        <span className="size-5 shrink-0" aria-hidden="true" />
      )}
    </div>
  )
}

/** User typeahead that adds a per-person override (reuses the share search).
 * Storage values are entered in GB to match the rest of the panel. */
function AddOverride({
  dimLabel,
  dims,
  onPick,
}: {
  dimLabel: Record<string, string>
  dims: QuotaDimensionKey[]
  onPick: (userId: string, dimension: string, value: number) => void
}) {
  const { t } = useLocale()
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<readonly UserSearchResult[]>([])
  const [picked, setPicked] = useState<UserSearchResult | null>(null)
  const [dimension, setDimension] = useState<QuotaDimensionKey>(dims[0])
  const [value, setValue] = useState('')
  const generationRef = useRef(0)

  useEffect(() => {
    const trimmed = query.trim()
    if (trimmed.length < MIN_QUERY) {
      setResults([])
      return
    }
    const generation = ++generationRef.current
    const timer = window.setTimeout(() => {
      void searchUsers(trimmed)
        .then((found) => {
          if (generationRef.current === generation) setResults(found)
        })
        .catch(() => setResults([]))
    }, SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [query])

  if (picked) {
    const storage = isStorage(dimension)
    // Storage is entered in GB (decimals allowed) -> bytes; everything else
    // is a raw integer. parsedValue is already in API units.
    const parsedValue = storage ? parseStorageGb(value) : parseLimitValue(value)
    return (
      <div className="flex flex-wrap items-center gap-2">
        <span className="inline-flex items-center gap-1.5 rounded-full bg-brand-subtle px-2 py-0.5 t-meta-sm text-brand">
          {personLabel(picked.display_name, picked.email, picked.id)}
          <button
            aria-label={t.quotaAdmin.removePerson}
            onClick={() => setPicked(null)}
            type="button"
          >
            <X className="size-3" />
          </button>
        </span>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button className="gap-1.5" size="sm" type="button" variant="outline">
              <span>
                {dimLabel[dimension]}
                {storage ? ` ${t.quotaAdmin.storageUnit}` : ''}
              </span>
              <ChevronDown className="text-muted-foreground" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent
            align="start"
            className={optionMenuContentClassName}
            sideOffset={6}
          >
            <OptionMenuHeader count={dims.length} title={t.quotaAdmin.dimension} />
            <div className="py-1">
              {dims.map((dim) => (
                <OptionMenuItem
                  active={dim === dimension}
                  icon={Gauge}
                  key={dim}
                  label={`${dimLabel[dim]}${isStorage(dim) ? ` ${t.quotaAdmin.storageUnit}` : ''}`}
                  onSelect={() => setDimension(dim)}
                />
              ))}
            </div>
          </DropdownMenuContent>
        </DropdownMenu>
        <input
          aria-label={t.quotaAdmin.limitValue}
          className="h-8 w-20 rounded-md border border-border bg-background px-2 text-right text-sm tabular-nums text-foreground"
          inputMode="numeric"
          onChange={(event) => setValue(event.target.value)}
          placeholder="0"
          value={value}
        />
        <Button
          disabled={parsedValue == null}
          onClick={() => {
            if (parsedValue == null) return
            onPick(picked.id, dimension, parsedValue)
            setPicked(null)
            setValue('')
            setQuery('')
          }}
          size="sm"
          type="button"
          variant="outline"
        >
          {t.quotaAdmin.addOverride}
        </Button>
      </div>
    )
  }

  return (
    <div>
      <div className="relative">
        <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
        <input
          aria-label={t.quotaAdmin.addPerson}
          className="h-8 w-full rounded-md border border-border bg-background pl-8 pr-3 text-sm text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring"
          onChange={(event) => setQuery(event.target.value)}
          placeholder={t.quotaAdmin.addPerson}
          type="text"
          value={query}
        />
      </div>
      {results.length > 0 ? (
        <ul className="mt-1.5 max-h-44 overflow-y-auto rounded-md border border-border">
          {results.map((user) => (
            <li key={user.id}>
              <button
                className="flex w-full items-center gap-2 px-2.5 py-1.5 text-left hover:bg-accent"
                onMouseDown={(event) => {
                  event.preventDefault()
                  setPicked(user)
                  setResults([])
                }}
                type="button"
              >
                <InitialsAvatar
                  displayName={user.display_name}
                  email={user.email}
                  size="sm"
                />
                <span className="min-w-0 flex-1 truncate t-list text-foreground">
                  {personLabel(user.display_name, user.email, user.id)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
