import type { ReactNode } from 'react'

import { BookOpenCheck, Database, FileText, Globe2, Library, MessagesSquare, Settings, Waypoints, type LucideIcon } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import type { AppView } from '../types'

type AppRailProps = {
  activeView: AppView
  onViewChange: (view: AppView) => void
  /** Capability gate for the knowledge workspace entry (true when the
   * backend advertises `features.knowledge`, or in demo mode). */
  showKnowledge?: boolean
  /** Capability gate for the Agent Desk entry (true when the backend
   * advertises `features.workspace_agent`, or in demo mode). */
  showAgent?: boolean
  /** Pending share invitations awaiting consent — a count chip on the
   * settings entry so the inbox is discoverable from anywhere. 0 hides it. */
  settingsBadgeCount?: number
  /** Identity slot rendered below the settings entry (the profile
   * avatar in oidc mode; absent otherwise). */
  profileSlot?: ReactNode
}

type RailItem = {
  badge?: number
  icon: LucideIcon
  label: string
  value: AppView
}

export function AppRail({ activeView, onViewChange, settingsBadgeCount = 0, showAgent = false, showKnowledge = false, profileSlot }: AppRailProps) {
  const { t } = useLocale()
  const deskItems: RailItem[] = [
    { icon: Globe2, label: t.navigation.research, value: 'research' },
    ...(showKnowledge
      ? [{ icon: BookOpenCheck, label: t.navigation.knowledge, value: 'knowledge' as AppView }]
      : []),
  ]
  const toolItems: RailItem[] = [
    { icon: MessagesSquare, label: t.navigation.chat, value: 'chat' },
    { icon: FileText, label: t.navigation.editor, value: 'editor' },
  ]
  // The Agent Desk sits in its OWN group below the editor, set off by a
  // divider — it is the acting-agent surface, distinct from the read tools.
  const agentItems: RailItem[] = showAgent
    ? [{ icon: Waypoints, label: t.navigation.agent, value: 'agent' }]
    : []
  const settingsItem = {
    badge: settingsBadgeCount,
    icon: Settings,
    label: t.navigation.settings,
    value: 'settings',
  } satisfies RailItem
  const databaseItem = {
    icon: Database,
    label: t.navigation.database,
    value: 'database',
  } satisfies RailItem
  const promptLibraryItem = {
    icon: Library,
    label: t.navigation.promptLibrary,
    value: 'prompt-library',
  } satisfies RailItem

  return (
    // The shell row is fully capped (`overflow-hidden`, never scrolls), so the
    // rail must NOT be sticky: a sticky rail reacts to the row's transient
    // scroll extent and gets dragged when a sibling workspace mounts with a
    // vertical entry transform (e.g. Settings' `y: 8`), producing a visible
    // up/down jitter. `relative` keeps the same box position and honours z-20.
    <nav
      aria-label={t.navigation.label}
      className="relative z-20 flex h-full min-h-0 w-[var(--header-h)] shrink-0 flex-col items-center border-r border-border bg-background/90 px-0 pb-3 pt-1 backdrop-blur"
    >
      <div className="flex flex-col items-center gap-1">
        {deskItems.map((item) => (
          <RailButton activeView={activeView} item={item} key={item.value} onViewChange={onViewChange} />
        ))}
        <span aria-hidden className="my-0.5 h-px w-5 rounded-full bg-border/70" />
        {toolItems.map((item) => (
          <RailButton activeView={activeView} item={item} key={item.value} onViewChange={onViewChange} />
        ))}
        {agentItems.length > 0 && (
          <span aria-hidden className="my-0.5 h-px w-5 rounded-full bg-border/70" />
        )}
        {agentItems.map((item) => (
          <RailButton activeView={activeView} item={item} key={item.value} onViewChange={onViewChange} />
        ))}
      </div>
      <div className="mt-auto flex flex-col items-center gap-1">
        <span aria-hidden className="mb-0.5 h-px w-5 rounded-full bg-border/70" />
        <RailButton activeView={activeView} item={promptLibraryItem} onViewChange={onViewChange} />
        <RailButton activeView={activeView} item={databaseItem} onViewChange={onViewChange} />
        <RailButton activeView={activeView} item={settingsItem} onViewChange={onViewChange} />
        {profileSlot}
      </div>
    </nav>
  )
}

function RailButton({
  activeView,
  item,
  onViewChange,
}: {
  activeView: AppView
  item: RailItem
  onViewChange: (view: AppView) => void
}) {
  const Icon = item.icon
  const isActive = activeView === item.value
  const badge = item.badge ?? 0

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={item.label}
          aria-pressed={isActive}
          className={cn(
            'relative size-9 rounded-md text-muted-foreground',
            isActive && 'bg-brand-subtle text-brand shadow-none hover:bg-brand-subtle hover:text-brand',
          )}
          onClick={() => onViewChange(item.value)}
          size="icon"
          type="button"
          variant={isActive ? 'default' : 'ghost'}
        >
          <Icon className="size-4" />
          {badge > 0 ? (
            <span className="absolute -right-0.5 -top-0.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-brand px-1 t-hint font-semibold tabular-nums text-brand-foreground">
              {badge > 9 ? '9+' : badge}
            </span>
          ) : null}
        </Button>
      </TooltipTrigger>
      <TooltipContent side="right">{item.label}</TooltipContent>
    </Tooltip>
  )
}
