import { Users } from '@/components/icons'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'

/**
 * Quiet sharing indicator — muted, never a status colour (sharing is
 * identity, not state). Owner variant carries the recipient count and
 * opens the dialog; recipient state comes directly from the resource DTO.
 */
export function SharedBadge({
  count,
  isSharedWithMe = false,
  onClick,
}: {
  count?: number
  isSharedWithMe?: boolean
  onClick?: () => void
}) {
  const { t } = useLocale()
  if (!isSharedWithMe && (count === undefined || count === 0)) return null
  const tooltip = isSharedWithMe
    ? t.sharing.sharedBadge
    : countLabel(count ?? 0, t.sharing.sharedCountOne, t.sharing.sharedCount)

  const body = (
    <span aria-hidden="true" className="inline-flex items-center gap-1 text-muted-foreground">
      <Users className="size-3" />
      {!isSharedWithMe && (
        <span className="t-hint tabular-nums">{count}</span>
      )}
    </span>
  )

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {onClick ? (
          <button
            aria-label={tooltip}
            className="grid h-6 place-items-center rounded-md px-1 hover:bg-accent"
            onClick={(event) => {
              event.stopPropagation()
              onClick()
            }}
            type="button"
          >
            {body}
          </button>
        ) : (
          <span className="grid h-6 place-items-center px-1">
            {body}
            <span className="sr-only">{tooltip}</span>
          </span>
        )}
      </TooltipTrigger>
      <TooltipContent>{tooltip}</TooltipContent>
    </Tooltip>
  )
}
