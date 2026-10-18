import {
  Download,
  FolderOpen,
  LoaderCircle,
  Menu,
  Monitor,
  Moon,
  Save,
  Server,
  Sun,
  Upload,
  X,
  type LucideIcon,
} from '@/components/icons'
import { AnimatePresence, motion, useReducedMotion } from 'motion/react'
import type { ReactNode } from 'react'
import { BrandMark } from '@/components/BrandMark'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import type { ProjectConnection } from '@/features/project/types'
import type { AppView } from '@/features/researchDesk/types'
import { useLocale } from '@/i18n/LocaleProvider'
import type { Locale, TranslationDictionary } from '@/i18n/translations'
import { useTheme, type ThemeMode } from '@/theme/ThemeProvider'
import { cn } from '@/lib/utils'
import { appMotion } from '@/motion/transitions'

type TopbarProps = {
  activeView: AppView
  /** The server supports durable project persistence: shows the
   * "move to server" import action. */
  canPersistProject: boolean
  dirty: boolean
  /** A server import is in flight (the import button shows a spinner). */
  importPending: boolean
  isProjectActionPending: boolean
  onDismissProjectActionError: () => void
  onExportProject: () => void
  /** Push this project's chat to the server and opt into ongoing sync. */
  onImportProjectToServer: () => void
  onLoadProject: () => void
  onSaveProject: () => void
  projectActionError: string | null
  projectConnection: ProjectConnection
  projectName: string
  /** This project is opted into the server-persistence tier. */
  serverSyncEnabled: boolean
  /** Last background chat-sync failure, surfaced as a status (never silent). */
  serverSyncError: string | null
}

export function Topbar({
  activeView,
  canPersistProject,
  dirty,
  importPending,
  isProjectActionPending,
  onDismissProjectActionError,
  onExportProject,
  onImportProjectToServer,
  onLoadProject,
  onSaveProject,
  projectActionError,
  projectConnection,
  projectName,
  serverSyncEnabled,
  serverSyncError,
}: TopbarProps) {
  const { t } = useLocale()
  const reduceMotion = useReducedMotion()
  const status = projectStatus(projectConnection, dirty, t)
  const activeModeLabel = viewLabel(activeView, t)
  const projectActionsProps = {
    canPersistProject,
    dirty,
    importPending,
    isProjectActionPending,
    onExportProject,
    onImportProjectToServer,
    onLoadProject,
    onSaveProject,
    projectConnection,
    serverSyncEnabled,
  }

  return (
    <header className="sticky top-0 z-30 border-b border-border bg-background/95 backdrop-blur">
      <div className="flex h-[var(--header-h)] min-h-[var(--header-h)] w-full items-center gap-2 overflow-hidden pr-2 md:pr-4 xl:pr-6">
        <div className="flex min-w-0 items-center">
          <div className="flex w-[var(--header-h)] shrink-0 items-center justify-center">
            <BrandMark className="size-7 shrink-0" />
          </div>
          <span className="text-base font-semibold tracking-normal text-foreground">
            {t.appName}
          </span>
          <span aria-hidden className="mx-2 hidden h-4 w-px bg-border sm:block" />
          <motion.span
            aria-live="polite"
            className="hidden min-w-0 items-center overflow-hidden text-sm font-medium text-muted-foreground sm:inline-grid"
          >
            <AnimatePresence initial={false} mode="popLayout">
              <motion.span
                animate={reduceMotion
                  ? { opacity: 1 }
                  : { opacity: 1, y: 0 }}
                className="col-start-1 row-start-1 inline-flex whitespace-nowrap"
                exit={reduceMotion
                  ? { opacity: 0 }
                  : { opacity: 0, y: -2 }}
                initial={reduceMotion
                  ? { opacity: 0 }
                  : { opacity: 0, y: 2 }}
                key={activeModeLabel}
                transition={{
                  duration: reduceMotion ? 0.01 : 0.065,
                  ease: [0.22, 1, 0.36, 1],
                }}
              >
                {activeModeLabel}
              </motion.span>
            </AnimatePresence>
          </motion.span>
          {activeView === 'agent' && (
            <Badge
              className="t-hint ml-2 hidden h-5 shrink-0 rounded-full border-border/70 bg-background px-1.5 sm:inline-flex"
              variant="outline"
            >
              {t.navigation.agentBeta}
            </Badge>
          )}
        </div>

        <div className="ml-auto flex min-w-0 shrink-0 items-center justify-end gap-1.5">
          {!serverSyncEnabled ? (
            <ProjectStatusBadge projectName={projectName} status={status} />
          ) : null}
          <div className="hidden md:block">
            <ProjectActionsGroup {...projectActionsProps} />
          </div>
          {serverSyncEnabled ? (
            <div className="hidden md:block">
              <ServerSyncBadge error={serverSyncError} projectName={projectName} t={t} />
            </div>
          ) : null}
          <div className="hidden lg:block">
            <ThemeToggle />
          </div>
          <div className="hidden md:block">
            <LanguageToggle />
          </div>
          <TopbarOverflowMenu
            projectActions={projectActionsProps}
            serverSyncBadge={serverSyncEnabled
              ? <ServerSyncBadge error={serverSyncError} projectName={projectName} t={t} />
              : null}
          />
        </div>
      </div>
      {projectActionError ? (
        <div
          className="flex items-center gap-2 border-t border-border bg-destructive/10 px-4 py-2 text-sm text-destructive md:px-5 xl:px-8"
          role="alert"
        >
          <span className="min-w-0 flex-1 truncate" title={projectActionError}>
            {projectActionError}
          </span>
          <button
            aria-label={t.common.close}
            className="shrink-0 rounded-[6px] p-1 hover:bg-destructive/15"
            onClick={onDismissProjectActionError}
            type="button"
          >
            <X className="size-4" />
          </button>
        </div>
      ) : null}
    </header>
  )
}

function viewLabel(view: AppView, t: TranslationDictionary) {
  if (view === 'prompt-library') return t.navigation.promptLibrary
  return t.navigation[view]
}

function ProjectStatusBadge({
  projectName,
  status,
}: {
  projectName: string
  status: { label: string; tone: 'success' | 'warning' }
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div
          aria-label={`${status.label}: ${projectName}`}
          className={cn(
            'inline-flex h-8 max-w-40 items-center gap-1.5 rounded-md px-2 t-meta-sm font-semibold text-foreground',
            status.tone === 'warning' && 'text-brand',
          )}
          role="status"
          tabIndex={0}
        >
          <span className={cn(
            'size-2 shrink-0 rounded-full bg-success shadow-[0_0_0_4px_var(--success-subtle)]',
            status.tone === 'warning' && 'bg-brand shadow-[0_0_0_4px_var(--brand-subtle)]',
          )} />
          <span className="truncate">{status.label}</span>
        </div>
      </TooltipTrigger>
      <TooltipContent className="max-w-72">
        <span className="block font-semibold">{projectName}</span>
        <span className="block opacity-80">{status.label}</span>
      </TooltipContent>
    </Tooltip>
  )
}

type ProjectActionsGroupProps = {
  canPersistProject: boolean
  dirty: boolean
  importPending: boolean
  isProjectActionPending: boolean
  onExportProject: () => void
  onImportProjectToServer: () => void
  onLoadProject: () => void
  onSaveProject: () => void
  projectConnection: ProjectConnection
  serverSyncEnabled: boolean
}

function ProjectActionsGroup({
  canPersistProject,
  dirty,
  importPending,
  isProjectActionPending,
  onExportProject,
  onImportProjectToServer,
  onLoadProject,
  onSaveProject,
  projectConnection,
  serverSyncEnabled,
}: ProjectActionsGroupProps) {
  const { t } = useLocale()

  return (
    <div
      aria-label={t.topbar.projectActions}
      className="inline-flex h-8 items-center rounded-md border border-border bg-card p-0.5 shadow-[0_1px_2px_var(--shadow-hairline)]"
      role="group"
    >
      {serverSyncEnabled ? (
        <>
          <ProjectActionButton
            disabled={isProjectActionPending}
            icon={FolderOpen}
            label={t.topbar.importFile}
            onClick={onLoadProject}
          />
          <ProjectActionButton
            disabled={isProjectActionPending}
            icon={isProjectActionPending ? LoaderCircle : Download}
            label={t.topbar.exportBackup}
            onClick={onExportProject}
          />
        </>
      ) : (
        <>
          <ProjectActionButton
            disabled={isProjectActionPending}
            icon={FolderOpen}
            label={t.topbar.loadProject}
            onClick={onLoadProject}
          />
          <ProjectActionButton
            disabled={isProjectActionPending}
            icon={isProjectActionPending ? LoaderCircle : Download}
            label={t.topbar.exportProject}
            onClick={onExportProject}
          />
          <ProjectActionButton
            disabled={isProjectActionPending || (!dirty && projectConnection.kind !== 'directory')}
            icon={isProjectActionPending ? LoaderCircle : Save}
            label={t.topbar.saveProject}
            onClick={onSaveProject}
            spin={isProjectActionPending}
          />
          {canPersistProject ? (
            <ProjectActionButton
              disabled={isProjectActionPending || importPending}
              icon={importPending ? LoaderCircle : Upload}
              label={t.topbar.importToServer}
              onClick={onImportProjectToServer}
              spin={importPending}
            />
          ) : null}
        </>
      )}
    </div>
  )
}

function ProjectActionButton({
  disabled = false,
  icon: Icon,
  label,
  onClick,
  spin = false,
}: {
  disabled?: boolean
  icon: LucideIcon
  label: string
  onClick: () => void
  spin?: boolean
}) {
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <Button
          aria-label={label}
          className="size-7 rounded-[6px] px-0 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          disabled={disabled}
          onClick={onClick}
          type="button"
          variant="ghost"
        >
          <Icon className={cn('icon-sm', spin && 'animate-spin')} />
        </Button>
      </TooltipTrigger>
      <TooltipContent>{label}</TooltipContent>
    </Tooltip>
  )
}

function TopbarOverflowMenu({
  projectActions,
  serverSyncBadge,
}: {
  projectActions: ProjectActionsGroupProps
  serverSyncBadge: ReactNode
}) {
  const { locale, setLocale, t } = useLocale()
  const { setTheme, theme } = useTheme()
  const themeOptions: Array<{
    icon: LucideIcon
    label: string
    value: ThemeMode
  }> = [
    { icon: Sun, label: t.common.light, value: 'light' },
    { icon: Moon, label: t.common.dark, value: 'dark' },
    { icon: Monitor, label: t.common.system, value: 'system' },
  ]
  const languageOptions: Array<{ label: string; value: Locale }> = [
    { label: 'DE', value: 'de' },
    { label: 'EN', value: 'en' },
  ]

  return (
    <DropdownMenu modal={false}>
      <DropdownMenuTrigger asChild>
        <Button
          aria-label={t.common.menu}
          className="size-8 shrink-0 text-muted-foreground hover:bg-accent/70 hover:text-foreground lg:hidden"
          size="icon"
          title={t.common.menu}
          type="button"
          variant="ghost"
        >
          <Menu className="icon-md" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56 rounded-lg p-1 shadow-lg" sideOffset={8}>
        <div className="md:hidden">
          <TopbarMenuLabel>{t.topbar.projectActions}</TopbarMenuLabel>
          <ProjectActionMenuItem
            disabled={projectActions.isProjectActionPending}
            icon={FolderOpen}
            label={projectActions.serverSyncEnabled ? t.topbar.importFile : t.topbar.loadProject}
            onSelect={projectActions.onLoadProject}
          />
          <ProjectActionMenuItem
            disabled={projectActions.isProjectActionPending}
            icon={Download}
            label={projectActions.serverSyncEnabled ? t.topbar.exportBackup : t.topbar.exportProject}
            onSelect={projectActions.onExportProject}
          />
          {!projectActions.serverSyncEnabled ? (
            <ProjectActionMenuItem
              disabled={projectActions.isProjectActionPending || (!projectActions.dirty && projectActions.projectConnection.kind !== 'directory')}
              icon={projectActions.isProjectActionPending ? LoaderCircle : Save}
              label={t.topbar.saveProject}
              onSelect={projectActions.onSaveProject}
              spin={projectActions.isProjectActionPending}
            />
          ) : null}
          {!projectActions.serverSyncEnabled && projectActions.canPersistProject ? (
            <ProjectActionMenuItem
              disabled={projectActions.isProjectActionPending || projectActions.importPending}
              icon={projectActions.importPending ? LoaderCircle : Upload}
              label={t.topbar.importToServer}
              onSelect={projectActions.onImportProjectToServer}
              spin={projectActions.importPending}
            />
          ) : null}
        </div>
        {serverSyncBadge ? (
          <div className="md:hidden">
            <DropdownMenuSeparator />
            <div className="px-1 py-1">{serverSyncBadge}</div>
          </div>
        ) : null}
        <div className="lg:hidden">
          <TopbarMenuSeparator className="md:hidden" />
          <TopbarMenuLabel>{t.common.theme}</TopbarMenuLabel>
          {themeOptions.map((option) => {
            const Icon = option.icon
            return (
              <DropdownMenuItem
                className={cn(
                  topbarMenuItemClassName,
                  theme === option.value && 'bg-brand-subtle text-brand focus:bg-brand-subtle focus:text-brand',
                )}
                key={option.value}
                onSelect={() => setTheme(option.value)}
              >
                <Icon className="icon-sm" />
                <span className={topbarMenuTextClassName}>{option.label}</span>
              </DropdownMenuItem>
            )
          })}
        </div>
        <div className="md:hidden">
          <DropdownMenuSeparator />
          <TopbarMenuLabel>{t.common.language}</TopbarMenuLabel>
          {languageOptions.map((option) => (
            <DropdownMenuItem
              className={cn(
                topbarMenuItemClassName,
                locale === option.value && 'bg-brand-subtle text-brand focus:bg-brand-subtle focus:text-brand',
              )}
              key={option.value}
              onSelect={() => setLocale(option.value)}
            >
              <span className="w-4 text-xs font-semibold">{option.label}</span>
              <span className={topbarMenuTextClassName}>{option.value === 'de' ? 'Deutsch' : 'English'}</span>
            </DropdownMenuItem>
          ))}
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

const topbarMenuItemClassName = 'h-8 gap-2 rounded-md px-2 py-1'
const topbarMenuTextClassName = 'min-w-0 truncate text-xs font-medium'

function TopbarMenuLabel({ children }: { children: ReactNode }) {
  return (
    <DropdownMenuLabel className="px-2 py-1">
      <span className="t-caption text-muted-foreground">{children}</span>
    </DropdownMenuLabel>
  )
}

function TopbarMenuSeparator({ className }: { className?: string }) {
  return <DropdownMenuSeparator className={className} />
}

function ProjectActionMenuItem({
  disabled = false,
  icon: Icon,
  label,
  onSelect,
  spin = false,
}: {
  disabled?: boolean
  icon: LucideIcon
  label: string
  onSelect: () => void
  spin?: boolean
}) {
  return (
    <DropdownMenuItem
      className={topbarMenuItemClassName}
      disabled={disabled}
      onSelect={(event) => {
        if (disabled) {
          event.preventDefault()
          return
        }
        onSelect()
      }}
    >
      <Icon className={cn('icon-sm', spin && 'animate-spin')} />
      <span className={topbarMenuTextClassName}>{label}</span>
    </DropdownMenuItem>
  )
}

function ServerSyncBadge({
  error,
  projectName,
  t,
}: {
  error: string | null
  projectName: string
  t: TranslationDictionary
}) {
  const label = error ? t.topbar.serverSyncError : t.topbar.serverSyncedShort
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <div
          aria-label={`${label}: ${projectName}`}
          className={cn(
            'inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-card px-2.5 t-meta-sm font-medium shadow-[0_1px_2px_var(--shadow-hairline)]',
            error ? 'text-destructive' : 'text-muted-foreground',
          )}
          role="status"
          tabIndex={0}
        >
          <Server
            className={cn('icon-sm shrink-0', error ? 'text-destructive' : 'text-success')}
          />
          <span className="hidden sm:inline">{label}</span>
        </div>
      </TooltipTrigger>
      <TooltipContent className="max-w-72">
        <span className="block font-semibold">{projectName}</span>
        <span className="block opacity-80">{error ?? t.topbar.serverSyncedHint}</span>
      </TooltipContent>
    </Tooltip>
  )
}

function projectStatus(
  connection: ProjectConnection,
  dirty: boolean,
  t: TranslationDictionary,
) {
  if (dirty) return { label: t.topbar.unsavedProject, tone: 'warning' as const }
  if (connection.kind === 'directory') {
    return { label: t.topbar.connectedProject, tone: 'success' as const }
  }
  if (connection.kind === 'download') {
    return { label: t.topbar.downloadFallback, tone: 'warning' as const }
  }
  if (connection.kind === 'demo') {
    return { label: t.common.demoMode, tone: 'success' as const }
  }
  return { label: t.topbar.localProject, tone: 'success' as const }
}

function ThemeToggle() {
  const { setTheme, theme } = useTheme()
  const { t } = useLocale()

  const options: Array<{
    icon: LucideIcon
    label: string
    value: ThemeMode
  }> = [
    { icon: Sun, label: t.common.light, value: 'light' },
    { icon: Moon, label: t.common.dark, value: 'dark' },
    { icon: Monitor, label: t.common.system, value: 'system' },
  ]

  return (
    <div
      aria-label={t.common.theme}
      className="inline-flex h-8 items-center rounded-md border border-border bg-card p-0.5 shadow-[0_1px_2px_var(--shadow-hairline)]"
      role="group"
    >
      {options.map((option) => {
        const Icon = option.icon
        return (
          <button
            aria-label={option.label}
            className={cn(
              'inline-flex size-7 items-center justify-center rounded-[6px] text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground',
              theme === option.value && 'bg-brand-subtle text-brand hover:bg-brand-subtle hover:text-brand',
            )}
            key={option.value}
            onClick={() => setTheme(option.value)}
            type="button"
          >
            <Icon className="icon-sm" />
          </button>
        )
      })}
    </div>
  )
}

function LanguageToggle() {
  const { locale, setLocale, t } = useLocale()
  const reduceMotion = useReducedMotion()

  const options: Array<{ label: string; value: Locale }> = [
    { label: 'DE', value: 'de' },
    { label: 'EN', value: 'en' },
  ]

  return (
    <div
      aria-label={t.common.language}
      className="inline-flex h-8 items-center rounded-md border border-border bg-card p-0.5 shadow-[0_1px_2px_var(--shadow-hairline)]"
      role="group"
    >
      {options.map((option) => {
        const isActive = locale === option.value

        return (
          <button
            aria-label={option.label}
            aria-pressed={isActive}
            className={cn(
              'relative inline-flex h-7 items-center justify-center rounded-[6px] px-2.5 text-xs font-semibold transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              isActive ? 'text-brand' : 'text-muted-foreground hover:text-foreground',
            )}
            key={option.value}
            onClick={() => setLocale(option.value)}
            type="button"
          >
            {isActive ? (
              <motion.span
                aria-hidden
                className="absolute inset-0 rounded-[6px] bg-brand-subtle"
                layoutId="topbar-locale-active"
                transition={reduceMotion ? { duration: 0 } : appMotion.list}
              />
            ) : null}
            <span className="relative z-10">{option.label}</span>
          </button>
        )
      })}
    </div>
  )
}
