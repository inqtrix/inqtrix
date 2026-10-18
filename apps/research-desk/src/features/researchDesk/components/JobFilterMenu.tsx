import type { ReactNode } from 'react'

import { ChevronDown, ListFilter } from '@/components/icons'
import { Chip } from '@/components/ui/chip'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useLocale } from '@/i18n/LocaleProvider'
import type { JobFilter, ResearchJob } from '../types'

type JobFilterMenuProps = {
  activeFilter: JobFilter
  jobs: ResearchJob[]
  onActiveFilterChange: (filter: JobFilter) => void
  /** Right-pinned header slot (the report panel toggle), kept outside the
   * horizontally scrolling chip row so it never scrolls out of reach. */
  trailing?: ReactNode
}

export function JobFilterMenu({
  activeFilter,
  jobs,
  onActiveFilterChange,
  trailing,
}: JobFilterMenuProps) {
  const { t } = useLocale()
  const filters = buildFilterOptions(jobs, t.home.tabs)
  const active = filters.find((filter) => filter.key === activeFilter) ?? filters[0]

  return (
    <div className="flex inqtrix-panel-header items-center gap-1 border-b border-border px-3">
      <div className="hidden min-w-0 flex-1 items-center gap-1 overflow-x-auto [scrollbar-width:none] sm:flex">
        <ListFilter className="size-3.5 shrink-0 text-muted-foreground" />
        {filters.map((filter) => (
          <Chip
            active={activeFilter === filter.key}
            count={filter.count}
            key={filter.key}
            onClick={() => onActiveFilterChange(filter.key)}
          >
            {filter.label}
          </Chip>
        ))}
      </div>
      <DropdownMenu modal={false}>
        <DropdownMenuTrigger asChild>
          <button
            className="inline-flex h-7 min-w-0 flex-1 items-center gap-1.5 rounded-md border border-border bg-card px-2 text-xs font-medium text-foreground shadow-[0_1px_2px_var(--shadow-hairline)] transition-colors hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring sm:hidden"
            type="button"
          >
            <ListFilter className="icon-sm shrink-0 text-muted-foreground" />
            <span className="min-w-0 truncate">{active.label}</span>
            <span className="shrink-0 text-muted-foreground tabular-nums">{active.count}</span>
            <ChevronDown className="icon-xs ml-auto shrink-0 text-muted-foreground" />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-56 rounded-xl p-1.5 shadow-lg" sideOffset={6}>
          {filters.map((filter) => (
            <DropdownMenuItem
              className={activeFilter === filter.key ? 'bg-brand-subtle text-brand focus:bg-brand-subtle focus:text-brand' : undefined}
              key={filter.key}
              onSelect={() => onActiveFilterChange(filter.key)}
            >
              <span className="min-w-0 flex-1 truncate">{filter.label}</span>
              <span className="t-hint tabular-nums text-muted-foreground">{filter.count}</span>
            </DropdownMenuItem>
          ))}
        </DropdownMenuContent>
      </DropdownMenu>
      {trailing ? <div className="ml-auto flex shrink-0 items-center pl-2">{trailing}</div> : null}
    </div>
  )
}

function buildFilterOptions(
  jobs: ResearchJob[],
  labels: Record<JobFilter, string>,
) {
  return [
    { count: jobs.length, key: 'all', label: labels.all },
    {
      // Unavailable runs retract their status (calm lock): they count
      // only under "all", mirroring visibleResearchJobs.
      count: jobs.filter((job) => job.status === 'running' && !job.unavailable).length,
      key: 'running',
      label: labels.running,
    },
    {
      count: jobs.filter((job) => job.status === 'queued' && !job.unavailable).length,
      key: 'queued',
      label: labels.queued,
    },
    {
      count: jobs.filter((job) => job.status === 'cancelled').length,
      key: 'cancelled',
      label: labels.cancelled,
    },
    {
      count: jobs.filter((job) => job.status === 'completed').length,
      key: 'completed',
      label: labels.completed,
    },
  ] satisfies Array<{
    count: number
    key: JobFilter
    label: string
  }>
}
