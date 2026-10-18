/**
 * Header sort control shared by every desk sidebar: one trigger icon
 * next to the folder/new buttons, three modes rendered in the house
 * option-menu language. The trigger reflects the active mode in its
 * tooltip so a drag-induced switch to "manual" is visible, never
 * silent.
 */

import { ArrowDownAZ, ArrowUpDown, CalendarClock, Hand, type LucideIcon } from '@/components/icons'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import {
  OptionMenuHeader,
  OptionMenuItem,
  optionMenuContentClassName,
} from '@/components/ui/option-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { EXPLORER_SORT_MODES } from '@/features/project/explorerSort'
import type { ExplorerSortMode } from '@/features/project/explorerSort'
import { useLocale } from '@/i18n/LocaleProvider'

const MODE_ICONS: Record<ExplorerSortMode, LucideIcon> = {
  manual: Hand,
  name: ArrowDownAZ,
  recent: CalendarClock,
}

export function ExplorerSortMenu({
  mode,
  onChangeMode,
}: {
  mode: ExplorerSortMode
  onChangeMode: (mode: ExplorerSortMode) => void
}) {
  const { t } = useLocale()
  const labels: Record<ExplorerSortMode, string> = {
    manual: t.explorerSort.manual,
    name: t.explorerSort.name,
    recent: t.explorerSort.recent,
  }
  const descriptions: Record<ExplorerSortMode, string> = {
    manual: t.explorerSort.manualHint,
    name: t.explorerSort.nameHint,
    recent: t.explorerSort.recentHint,
  }
  return (
    <DropdownMenu>
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button
              aria-label={`${t.explorerSort.title}: ${labels[mode]}`}
              className="size-7 shrink-0 rounded-md"
              size="icon"
              type="button"
              variant="ghost"
            >
              <ArrowUpDown className="size-4 text-foreground/85" />
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        <TooltipContent>{`${t.explorerSort.title}: ${labels[mode]}`}</TooltipContent>
      </Tooltip>
      <DropdownMenuContent align="end" className={optionMenuContentClassName}>
        <OptionMenuHeader
          count={EXPLORER_SORT_MODES.length}
          title={t.explorerSort.title}
          value={labels[mode]}
        />
        {EXPLORER_SORT_MODES.map((candidate) => (
          <OptionMenuItem
            active={candidate === mode}
            description={descriptions[candidate]}
            icon={MODE_ICONS[candidate]}
            key={candidate}
            label={labels[candidate]}
            onSelect={() => onChangeMode(candidate)}
          />
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
