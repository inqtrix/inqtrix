import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

/**
 * Shared Settings layout primitives. Settings surfaces use the quiet structured
 * list pattern from the database/file views: section headers and hoverable dense
 * rows carry the hierarchy, without enclosing every group in nested table chrome.
 * Mechanical extraction from SettingsWorkspace — no behaviour change beyond
 * StatusBadge gaining the `destructive` tone for the admin surface.
 */

/** Fluid section with an optional header and quiet structured rows. */
export function SettingsSection({
  children,
  description,
  title,
}: {
  children: ReactNode
  description?: string
  title?: string
}) {
  return (
    <section className="min-w-0 bg-transparent">
      {title ? (
        <div className="border-b border-border/65 px-3 pb-2 pt-1">
          <h3 className="t-section text-foreground">{title}</h3>
          {description ? (
            <p className="mt-0.5 t-meta text-muted-foreground">{description}</p>
          ) : null}
        </div>
      ) : null}
      <div className="grid gap-1 py-1">{children}</div>
    </section>
  )
}

/** Left label/description, right control — the standard settings row. */
export function SettingsRow({
  children,
  description,
  descriptionId,
  title,
}: {
  children: ReactNode
  description?: string
  descriptionId?: string
  title: string
}) {
  return (
    <div className="flex flex-col gap-2.5 rounded-md px-3 py-2.5 transition-colors hover:bg-surface/45 sm:flex-row sm:items-center sm:justify-between sm:gap-6">
      <div className="min-w-0 sm:flex-1">
        <h4 className="t-list text-foreground">{title}</h4>
        {description ? (
          <p className="mt-0.5 t-meta text-muted-foreground" id={descriptionId}>
            {description}
          </p>
        ) : null}
      </div>
      <div className="min-w-0 shrink-0 sm:max-w-[60%] sm:text-right">
        {children}
      </div>
    </div>
  )
}

/** Title/description above a full-width content block. */
export function SettingsRowBlock({
  children,
  description,
  title,
}: {
  children: ReactNode
  description?: string
  title: string
}) {
  return (
    <div className="rounded-md px-3 py-2.5 transition-colors hover:bg-surface/45">
      <h4 className="t-list text-foreground">{title}</h4>
      {description ? (
        <p className="mt-0.5 t-meta text-muted-foreground">{description}</p>
      ) : null}
      <div className="mt-3">{children}</div>
    </div>
  )
}

export type StatusTone =
  | 'brand'
  | 'neutral'
  | 'success'
  | 'warning'
  | 'destructive'

/** `*-subtle` surface + short label — the one semantic status pill shared by every view. */
export function StatusBadge({
  className,
  density = 'default',
  label,
  tone,
}: {
  className?: string
  density?: 'default' | 'table'
  label: string
  tone: StatusTone
}) {
  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center rounded-md border',
        density === 'default' && 'h-7 px-2 text-xs font-semibold',
        density === 'table' && 'h-5 px-1.5 t-hint font-medium',
        tone === 'brand' && 'border-brand/25 bg-brand-subtle text-brand',
        tone === 'neutral' && 'border-border bg-background text-muted-foreground',
        tone === 'success' && 'border-success/20 bg-success-subtle text-success',
        tone === 'warning' && 'border-warning/25 bg-warning/10 text-warning',
        tone === 'destructive' &&
          'border-destructive/25 bg-destructive/10 text-destructive',
        className,
      )}
    >
      {label}
    </span>
  )
}
