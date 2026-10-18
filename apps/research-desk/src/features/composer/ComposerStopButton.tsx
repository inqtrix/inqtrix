import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'

type ComposerStopButtonProps = {
  className?: string
  disabled?: boolean
  label: string
  onClick: () => void
}

/**
 * Destructive primary control shown while a composer-owned generation is
 * running. It keeps the send button footprint, but uses a restrained stop
 * mark instead of a decorative badge.
 */
export function ComposerStopButton({
  className,
  disabled = false,
  label,
  onClick,
}: ComposerStopButtonProps) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={label}
          className={cn(
            'group/stop relative size-7 shrink-0 overflow-hidden rounded-md border border-destructive/25 bg-destructive text-white shadow-sm shadow-destructive/15 hover:bg-destructive/90 hover:text-white focus-visible:ring-1 focus-visible:ring-destructive/45 focus-visible:ring-offset-0',
            className,
          )}
          disabled={disabled}
          onClick={onClick}
          size="icon"
          type="button"
          variant="default"
        >
          <span
            aria-hidden="true"
            className="inqtrix-running-dot absolute inset-1 rounded-md bg-white/12"
          />
          <span className="relative z-10 grid size-4 place-items-center rounded-sm bg-white/15 transition-colors group-hover/stop:bg-white/20">
            <span className="block size-2.5 rounded-[2px] bg-current" />
          </span>
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}
