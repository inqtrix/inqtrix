import { Check, type LucideIcon } from '@/components/icons'
import { DropdownMenuItem } from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'

/**
 * Shared building blocks for "trigger → options with a check" dropdown pickers.
 * The chat composer model/effort pickers, the chat settings menu, and the
 * database sort & embedding-model pickers all compose these so every picker in
 * the app reads as one design ("aus einem Guss"). Each call site still owns its
 * own trigger and `DropdownMenu` wrapper; only the content shape, header and
 * option row are shared.
 */

/** Canonical content width/shape for an option-picker dropdown — pass it as the
 * `DropdownMenuContent` className. */
export const optionMenuContentClassName = 'w-max min-w-48 max-w-72 overflow-hidden rounded-xl p-0 shadow-lg'

/** Compact menu header: the title on the left, and the current value on the
 * right — or, when no value is given, the option count. */
export function OptionMenuHeader({ count, title, value }: { count: number; title: string; value?: string }) {
  return (
    <div className="flex items-center gap-1.5 border-b border-border px-2.5 py-1.5">
      <span className="t-meta-sm font-medium text-muted-foreground">{title}</span>
      <span className="ml-auto t-hint tabular-nums text-muted-foreground/50">{value ?? count}</span>
    </div>
  )
}

/** One picker option: a brand accent bar on the left (shown on hover/active), a
 * leading icon (brand when active), the label plus an optional description, and
 * a trailing check on the active option. */
export function OptionMenuItem({
  active,
  description,
  disabled = false,
  icon: Icon,
  label,
  onSelect,
  keepOpen = false,
  descriptionLines = 1,
}: {
  active: boolean
  description?: string
  /** Listed but not pickable; the description says why (a limit that is
   * legible before the click, never a click that does nothing). */
  disabled?: boolean
  icon: LucideIcon
  label: string
  onSelect: () => void
  /** Multi-setting popovers can keep the surface open between choices. */
  keepOpen?: boolean
  /** Long decision hints may use two lines; all other menu rows stay dense. */
  descriptionLines?: 1 | 2
}) {
  return (
    <DropdownMenuItem
      disabled={disabled}
      className={cn(
        'group relative items-center gap-2.5 rounded-none px-2.5 py-1.5 pr-1.5 hover:bg-accent/50 focus:bg-accent/80 data-[highlighted]:bg-accent/80',
        active && 'bg-accent',
      )}
      onSelect={(event) => {
        if (keepOpen) event.preventDefault()
        onSelect()
      }}
    >
      <span
        className={cn(
          'absolute inset-y-1 left-0 w-0.5 rounded-full bg-brand transition-opacity group-hover:opacity-100 group-focus:opacity-100 group-data-[highlighted]:opacity-100',
          active ? 'opacity-100' : 'opacity-0',
        )}
      />
      <Icon
        className={cn(
          'icon-md shrink-0 transition-colors',
          active
            ? 'text-brand'
            : 'text-muted-foreground/70 group-hover:text-brand group-focus:text-brand group-data-[highlighted]:text-brand',
        )}
      />
      <span className="min-w-0 flex-1 text-left">
        {/* A reason is read in full: an option that cannot be picked wraps
            where a pickable one stays dense. */}
        <span
          className={cn(
            'block t-list text-foreground',
            disabled ? 'whitespace-normal break-words' : 'truncate',
          )}
        >
          {label}
        </span>
        {description ? (
          <span
            className={cn(
              'block t-meta-sm text-muted-foreground',
              disabled
                ? 'whitespace-normal break-words'
                : descriptionLines === 2 ? 'line-clamp-2' : 'truncate',
            )}
          >
            {description}
          </span>
        ) : null}
      </span>
      <span className="flex size-4 shrink-0 items-center justify-center">
        {active ? <Check className="size-3.5 text-brand" /> : null}
      </span>
    </DropdownMenuItem>
  )
}
