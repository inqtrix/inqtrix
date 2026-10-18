import { ListTree, ScrollText } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

type PanelToggleProps = {
  /** Which side panel this controls; picks the semantic glyph and tooltip side. */
  side: 'left' | 'right'
  /** True while the panel is open (drives aria-expanded and the active control state). */
  expanded: boolean
  /** Receives the desired next state (`!expanded`). */
  onToggle: (next: boolean) => void
  /** Tooltip + aria-label while collapsed (the "open it" affordance). */
  expandLabel: string
  /** Tooltip + aria-label while open (the "collapse it" affordance). */
  collapseLabel: string
  className?: string
  controlsId?: string
}

/**
 * A single, position-stable control that collapses and expands one side panel
 * with a semantic glyph: left panels are explorers/navigation, right panels are
 * supporting details. It lives at the panel/content boundary inside the
 * persistent column header, replacing the former always-on restore rail and the
 * panel's own in-header close button -- so the affordance never moves and the
 * collapsed state leaves no leftover strip. Always visible (never hover-only)
 * so a collapsed panel stays discoverable; the tooltip carries the meaning.
 */
export function PanelToggle({
  side,
  expanded,
  onToggle,
  expandLabel,
  collapseLabel,
  className,
  controlsId,
}: PanelToggleProps) {
  const label = expanded ? collapseLabel : expandLabel
  const Icon = side === 'left' ? ListTree : ScrollText
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-controls={controlsId}
          aria-expanded={expanded}
          aria-label={label}
          className={cn(
            'size-7 shrink-0 text-foreground/75 hover:text-foreground',
            expanded && 'bg-brand-subtle text-brand hover:bg-brand-subtle/80 hover:text-brand',
            className,
          )}
          onClick={() => onToggle(!expanded)}
          size="icon"
          type="button"
          variant="ghost"
        >
          <Icon className="size-4" />
        </Button>
      </TooltipTrigger>
      <TooltipContent side={side === 'left' ? 'right' : 'left'}>{label}</TooltipContent>
    </Tooltip>
  )
}
