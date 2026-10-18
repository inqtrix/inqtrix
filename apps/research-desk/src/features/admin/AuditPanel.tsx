import { useState } from 'react'

import {
  adminAuditExportUrl,
  type AdminAuditEvent,
  type AdminAuditFilters,
} from '@/api/inqtrixClient'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  auditDateInputValue,
  auditEpochFromDateInput,
} from './adminModel'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableEmpty,
  TableHead,
  TableHeader,
  TableRow,
  TableSkeleton,
} from '@/components/ui/table'
import { StatusBadge } from '@/features/settings/parts'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import { AuditRunDrawer } from './AuditRunDrawer'
import type { useAdminAudit } from './useAdminAudit'

const ACTION_GROUPS = [
  { value: 'all', prefix: '' },
  { value: 'runs', prefix: 'run.' },
  { value: 'chat', prefix: 'chat.' },
  { value: 'indexing', prefix: 'indexing.' },
  { value: 'auth', prefix: 'auth.' },
  { value: 'deletion', prefix: 'asset.delete' },
  { value: 'files', prefix: 'file.' },
  { value: 'exports', prefix: 'export.' },
] as const

type ActionGroup = (typeof ACTION_GROUPS)[number]['value']

function formatTime(seconds: number, locale: string): string {
  return new Date(seconds * 1000).toLocaleString(locale, {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

function outcomeTone(
  outcome: AdminAuditEvent['outcome'],
): 'success' | 'destructive' | 'warning' {
  if (outcome === 'success') return 'success'
  if (outcome === 'denied') return 'warning'
  return 'destructive'
}

/**
 * Instance-admin audit trail: filter bar, newest-first table,
 * keyset load-more, NDJSON/CSV export, and the run drawer drill-down for
 * rows that carry a run correlation.
 */
export function AuditPanel({
  audit,
  demo,
  filters,
  onFiltersChange,
  traceUiConfigured,
}: {
  audit: ReturnType<typeof useAdminAudit>
  demo: boolean
  filters: AdminAuditFilters
  onFiltersChange: (filters: AdminAuditFilters) => void
  traceUiConfigured: boolean
}) {
  const { locale, t } = useLocale()
  const [drawerRunId, setDrawerRunId] = useState<string | null>(null)
  const activeGroup: ActionGroup =
    ACTION_GROUPS.find((group) => group.prefix === (filters.action ?? ''))
      ?.value ?? 'all'

  const rows = audit.events
  const busy = audit.status === 'loading' && rows.length === 0

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <div className="flex flex-wrap items-center gap-2 px-3">
        <Select
          value={activeGroup}
          onValueChange={(value) => {
            const group = ACTION_GROUPS.find(
              (candidate) => candidate.value === value,
            )
            onFiltersChange({
              ...filters,
              action: group?.prefix || undefined,
            })
          }}
        >
          <SelectTrigger className="w-44">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {ACTION_GROUPS.map((group) => (
              <SelectItem key={group.value} value={group.value}>
                {t.adminAudit.actionGroups[group.value]}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={filters.outcome ?? 'all'}
          onValueChange={(value) =>
            onFiltersChange({
              ...filters,
              outcome:
                value === 'all'
                  ? undefined
                  : (value as AdminAuditFilters['outcome']),
            })
          }
        >
          <SelectTrigger className="w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">
              {t.adminAudit.outcomeAll}
            </SelectItem>
            <SelectItem value="success">
              {t.adminAudit.outcomeSuccess}
            </SelectItem>
            <SelectItem value="failure">
              {t.adminAudit.outcomeFailure}
            </SelectItem>
            <SelectItem value="denied">
              {t.adminAudit.outcomeDenied}
            </SelectItem>
          </SelectContent>
        </Select>
        <Input
          className="w-56 font-mono"
          placeholder={t.adminAudit.actorFilterPlaceholder}
          value={filters.actor ?? ''}
          onChange={(event) =>
            onFiltersChange({
              ...filters,
              actor: event.target.value.trim() || undefined,
            })
          }
        />
        <Input
          type="date"
          className="w-40"
          aria-label={t.adminAudit.fromLabel}
          value={auditDateInputValue(filters.from)}
          onChange={(event) =>
            onFiltersChange({
              ...filters,
              from: auditEpochFromDateInput(event.target.value),
            })
          }
        />
        <Input
          type="date"
          className="w-40"
          aria-label={t.adminAudit.toLabel}
          value={auditDateInputValue(filters.to)}
          onChange={(event) =>
            onFiltersChange({
              // Exclusive upper bound: a picked day must INCLUDE that
              // day, so shift to the following midnight.
              ...filters,
              to: auditEpochFromDateInput(event.target.value, true),
            })
          }
        />
        <span className="ml-auto flex items-center gap-2">
          <span className="t-caption text-muted-foreground">
            {countLabel(rows.length, t.adminAudit.rowCountOne, t.adminAudit.rowCount)}
          </span>
          <Button
            size="sm"
            variant="outline"
            disabled={demo}
            onClick={() =>
              window.open(
                adminAuditExportUrl('ndjson', filters),
                '_blank',
              )
            }
          >
            NDJSON
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={demo}
            onClick={() =>
              window.open(adminAuditExportUrl('csv', filters), '_blank')
            }
          >
            CSV
          </Button>
        </span>
      </div>

      <Table variant="fluid">
        <TableHeader>
          <TableRow>
            <TableHead className="w-40">{t.adminAudit.colTime}</TableHead>
            <TableHead>{t.adminAudit.colAction}</TableHead>
            <TableHead className="w-24">
              {t.adminAudit.colOutcome}
            </TableHead>
            <TableHead className="w-48">{t.adminAudit.colActor}</TableHead>
            <TableHead>{t.adminAudit.colResource}</TableHead>
            <TableHead className="w-28" />
          </TableRow>
        </TableHeader>
        <TableBody>
          {busy ? (
            <TableSkeleton colSpan={6} rows={6} />
          ) : rows.length === 0 ? (
            <TableEmpty
              colSpan={6}
              title={
                audit.status === 'error'
                  ? (audit.error ?? t.adminAudit.loadError)
                  : t.adminAudit.empty
              }
            />
          ) : (
            rows.map((row) => {
              const runId = row.correlation.run_id
              return (
                <TableRow key={row.id}>
                  <TableCell className="t-mono tabular-nums text-muted-foreground">
                    {formatTime(row.occurred_at, locale)}
                  </TableCell>
                  <TableCell>
                    <span className="t-list text-foreground">
                      {row.action}
                    </span>
                  </TableCell>
                  <TableCell>
                    <StatusBadge
                      density="table"
                      label={t.adminAudit.outcomes[row.outcome]}
                      tone={outcomeTone(row.outcome)}
                    />
                  </TableCell>
                  <TableCell className="t-mono text-muted-foreground">
                    {row.actor_pseudonym ?? t.adminAudit.anonymous}
                  </TableCell>
                  <TableCell>
                    <span className="t-meta text-muted-foreground">
                      {row.resource_type} · {row.resource_id}
                    </span>
                  </TableCell>
                  <TableCell className="text-right">
                    {runId ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => setDrawerRunId(runId)}
                      >
                        {t.adminAudit.openDrawer}
                      </Button>
                    ) : null}
                  </TableCell>
                </TableRow>
              )
            })
          )}
        </TableBody>
      </Table>

      {audit.nextCursor ? (
        <div className="px-3 pb-2">
          <Button
            size="sm"
            variant="outline"
            disabled={audit.status === 'loading'}
            onClick={audit.loadMore}
          >
            {t.adminAudit.loadMore}
          </Button>
        </div>
      ) : null}

      {drawerRunId ? (
        <AuditRunDrawer
          demo={demo}
          onClose={() => setDrawerRunId(null)}
          row={
            rows.find(
              (candidate) => candidate.correlation.run_id === drawerRunId,
            ) ?? null
          }
          runId={drawerRunId}
          traceUiConfigured={traceUiConfigured}
        />
      ) : null}
    </div>
  )
}
