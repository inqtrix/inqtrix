import type { ComponentType } from 'react'
import * as React from 'react'

import { cn } from '@/lib/utils'

type TableVariant = 'card' | 'fluid'

const TableVariantContext = React.createContext<TableVariant>('card')

/**
 * Dense data table for settings/admin surfaces. Card is the default historical
 * container; fluid is for management pages where the table should read as part
 * of the page/workspace rather than a nested card. Header eyebrows are
 * `.t-caption`; the fluid variant keeps only subtle row separators so the
 * column header, alignment, and hover state carry most of the structure. The
 * BODY-cell text role is the caller's choice — a row title is `.t-list`, a
 * number is `.t-mono tabular-nums` — so the cell itself only owns
 * padding/alignment.
 */
function Table({
  className,
  variant = 'card',
  ...props
}: React.ComponentProps<'table'> & { variant?: TableVariant }) {
  return (
    <TableVariantContext.Provider value={variant}>
      <div
        className={cn(
          variant === 'card'
            ? 'min-w-0 overflow-hidden rounded-lg border border-border bg-card shadow-[0_1px_2px_var(--shadow-hairline)]'
            : 'min-w-0 overflow-x-auto bg-transparent',
        )}
      >
        <table
          className={cn('w-full border-collapse text-left', className)}
          {...props}
        />
      </div>
    </TableVariantContext.Provider>
  )
}

function TableHeader({ className, ...props }: React.ComponentProps<'thead'>) {
  const variant = React.useContext(TableVariantContext)

  return (
    <thead
      className={cn(
        variant === 'fluid'
          ? 'border-b border-border/70 bg-surface/45'
          : 'border-b border-border/70 bg-surface/55',
        className,
      )}
      {...props}
    />
  )
}

function TableBody({ className, ...props }: React.ComponentProps<'tbody'>) {
  const variant = React.useContext(TableVariantContext)

  return (
    <tbody
      className={cn(
        variant === 'card' && 'divide-y divide-border/55',
        className,
      )}
      {...props}
    />
  )
}

function TableRow({ className, ...props }: React.ComponentProps<'tr'>) {
  return (
    <tr
      className={cn('group transition-colors hover:bg-surface/45', className)}
      {...props}
    />
  )
}

/** Header cell — owns the `.t-caption` eyebrow role so callers never repeat it. */
function TableHead({ className, ...props }: React.ComponentProps<'th'>) {
  return (
    <th
      className={cn(
        't-caption whitespace-nowrap px-3 py-2 text-muted-foreground',
        className,
      )}
      {...props}
    />
  )
}

/** Body cell — owns padding/alignment only; the caller sets the text role. */
function TableCell({ className, ...props }: React.ComponentProps<'td'>) {
  return <td className={cn('px-3 py-2 align-middle', className)} {...props} />
}

/** Full-width empty state inside the table body (the one empty-state layout every table shares). */
function TableEmpty({
  colSpan,
  hint,
  icon: Icon,
  title,
}: {
  colSpan: number
  hint?: string
  icon?: ComponentType<{ className?: string }>
  title: string
}) {
  return (
    <tr>
      <td className="px-3 py-10 text-center" colSpan={colSpan}>
        <div className="mx-auto flex max-w-xs flex-col items-center gap-2">
          {Icon ? (
            <span className="flex size-9 items-center justify-center rounded-full bg-surface text-muted-foreground">
              <Icon className="icon-md" />
            </span>
          ) : null}
          <p className="t-section text-foreground">{title}</p>
          {hint ? <p className="t-meta text-muted-foreground">{hint}</p> : null}
        </div>
      </td>
    </tr>
  )
}

/** Loading rows; the shimmer loops only while loading, never on real content. */
function TableSkeleton({ colSpan, rows = 3 }: { colSpan: number; rows?: number }) {
  return (
    <>
      {Array.from({ length: rows }).map((_, index) => (
        <tr key={index}>
          <td className="px-3 py-2" colSpan={colSpan}>
            <div className="h-5 w-full animate-pulse rounded bg-surface" />
          </td>
        </tr>
      ))}
    </>
  )
}

export {
  Table,
  TableBody,
  TableCell,
  TableEmpty,
  TableHead,
  TableHeader,
  TableRow,
  TableSkeleton,
}
