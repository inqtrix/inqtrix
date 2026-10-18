import type { ReactNode } from 'react'
import type { LucideIcon } from '@/components/icons'

/**
 * Shared building blocks for the assistant message action menu (the three-dots
 * "more" dropdown next to a copy button). Extracted from the chat view so the
 * Wissen view can render a byte-identical menu — one design-language source of
 * truth instead of two parallel implementations.
 */

/** Content width/shape used by every assistant message action menu. */
export const assistantMenuContentClassName =
  'w-60 max-w-[calc(100vw-2rem)] overflow-hidden rounded-lg p-0 shadow-lg'

/**
 * Menu header: a bold primary line (e.g. the message timestamp) over an optional
 * muted secondary line (e.g. the model that produced the answer).
 */
export function AssistantMenuHeader({
  primary,
  secondary,
}: {
  primary: string
  secondary?: string | null
}) {
  return (
    <div className="border-b border-border/70 px-2.5 py-1.5">
      <div className="truncate t-meta-sm font-semibold tabular-nums text-muted-foreground">{primary}</div>
      {secondary ? (
        <div className="mt-0.5 truncate t-hint text-muted-foreground/70">{secondary}</div>
      ) : null}
    </div>
  )
}

/** Leading icon slot of a menu item; brightens with the item's highlight state. */
export function AssistantMenuIcon({
  icon: Icon,
}: {
  icon: LucideIcon
}) {
  return (
    <span className="flex w-5 shrink-0 items-center justify-center text-foreground/85 group-data-[disabled]:text-muted-foreground/45 group-data-[highlighted]:text-foreground group-data-[state=open]:text-foreground group-focus:text-foreground">
      <Icon className="icon-sm" strokeWidth={2.35} />
    </span>
  )
}

/** Truncating label slot of a menu item. */
export function AssistantMenuLabel({ children }: { children: ReactNode }) {
  return <span className="min-w-0 flex-1 truncate t-list-regular text-foreground">{children}</span>
}
