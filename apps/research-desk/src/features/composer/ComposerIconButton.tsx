import type { LucideIcon } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

/**
 * Single source of truth for the icon-button optics shared by every composer
 * footer (editor, chat, research). Apply this class directly to dropdown/select
 * triggers, or use the `ComposerIconButton` component for plain tooltip buttons.
 *
 * `[&_svg]:size-3.5` is load-bearing: the `Button` base sets `[&_svg]:size-4`,
 * so without this override every footer icon renders at 16px regardless of the
 * size class on the icon element. `text-muted-foreground` keeps the icons grey
 * instead of the dark `text-foreground` a bare ghost button inherits.
 */
export const composerIconButtonClassName =
  'h-7 w-7 shrink-0 rounded-md border border-transparent bg-transparent p-0 text-muted-foreground shadow-none [&_svg]:size-3.5 hover:bg-accent hover:text-foreground focus-visible:ring-1 data-[state=open]:bg-accent data-[state=open]:text-foreground'

type ComposerIconButtonProps = {
  active?: boolean
  className?: string
  disabled?: boolean
  icon: LucideIcon
  label: string
  onClick?: () => void
  type?: 'button' | 'submit'
}

/**
 * Tooltip + ghost icon button used for the secondary actions in a composer
 * footer (hide, attach, toggles). Size and colour are fixed by
 * `composerIconButtonClassName`; `active` paints the brand-subtle toggle state.
 */
export function ComposerIconButton({
  active = false,
  className,
  disabled = false,
  icon: Icon,
  label,
  onClick,
  type = 'button',
}: ComposerIconButtonProps) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={label}
          aria-pressed={active || undefined}
          className={cn(
            composerIconButtonClassName,
            active && 'bg-brand-subtle text-brand hover:bg-brand-subtle hover:text-brand',
            className,
          )}
          disabled={disabled}
          onClick={onClick}
          type={type}
          variant="ghost"
        >
          <Icon />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}
