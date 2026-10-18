import { Button } from '@/components/ui/button'
import {
  AlertTriangle,
  BookOpen,
  Check,
  ChevronDown,
  CircleUserRound,
  Database,
  ExternalLink,
  Gauge,
  Github,
  KeyRound,
  LayoutGrid,
  Monitor,
  Moon,
  Palette,
  Scale,
  ScrollText,
  Search,
  Server,
  Settings,
  Share2,
  Shield,
  SlidersHorizontal,
  Save,
  Sun,
  ThumbsDown,
  ThumbsUp,
  Trash2,
  Users,
  X,
  type LucideIcon,
} from '@/components/icons'
import {
  type FormEvent,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from 'react'
import {
  changePassword,
  hasHttpStatus,
  type AgentMemoryWire,
} from '@/api/inqtrixClient'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { useAgentMemory } from '@/features/agent/useAgentMemory'
import type { ModelTierPreference } from '@/features/researchRuns/types'
import {
  agentMemoryModeLabel,
  pendingAgentMemoryCandidates,
  visibleAgentFeedback,
} from '@/features/agent/memoryModel'
import { isPasswordAcceptable } from '@/features/auth/passwordPolicy'
import { AccessTokensPanel } from '@/features/admin/AccessTokensPanel'
import { AdminAuditSection } from '@/features/admin/AdminAuditSection'
import { SystemStatusPanel } from '@/features/admin/SystemStatusPanel'
import { AdminWorkspacesPanel } from '@/features/admin/AdminWorkspacesPanel'
import { UsersPanel } from '@/features/admin/UsersPanel'
import { seedSystemCapabilities, seedSystemHealth } from '@/features/admin/demo'
import { useAdminSystemRuntime } from '@/features/admin/useAdminSystemRuntime'
import { useAdminUsers } from '@/features/admin/useAdminUsers'
import { useAdminWorkspaces } from '@/features/admin/useAdminWorkspaces'
import { usePatTokens } from '@/features/admin/usePatTokens'
import { type AuthMode, isCookieSessionMode } from '@/features/auth/authMode'
import { isAdminRole } from '@/features/auth/roleAccess'
import { QuotaAdminPanel } from '@/features/quota/QuotaAdminPanel'
import { useQuotaAdmin } from '@/features/quota/useQuotaAdmin'
import { DEMO_OWNER } from '@/features/sharing/demoShares'
import { SharingPanel } from '@/features/sharing/SharingPanel'
import type { InboxShare } from '@/features/sharing/types'
import type { SharingInboxHandle } from '@/features/sharing/useSharingInbox'
import {
  SettingsRow,
  SettingsRowBlock,
  SettingsSection,
  StatusBadge,
} from '@/features/settings/parts'
import { useLocale } from '@/i18n/LocaleProvider'
import type {
  InqtrixCapabilities,
  InqtrixHealth,
  StackDiscoveryStatus,
} from '@/features/researchRuns/types'
import { cn } from '@/lib/utils'
import {
  useTheme,
  type ThemeMode,
  type ThemePreset,
  type UserBubbleTone,
} from '@/theme/ThemeProvider'

type SettingsWorkspaceProps = {
  apiCapabilities?: InqtrixCapabilities | null
  apiError: string | null
  apiHealth: InqtrixHealth | null
  apiKey: string
  /** Active auth mode resolved by the desk (demo forces `none`). */
  authMode?: AuthMode
  /** Cookie-session facts; canonical `user.id`/`role` drive account surfaces. */
  authSession?: {
    status: string
    user: {
      displayName: string | null
      email: string | null
      id: string
      role: string
    } | null
  }
  isDemoMode: boolean
  logoutError?: string | null
  /** Whether the server offers personal access tokens (auth-config
   * discovery). `false` hides the access-tokens panel; `undefined`
   * degrades open (treated as available). */
  patAvailable?: boolean
  onApiKeyChange: (apiKey: string) => void
  onDemoModeChange: (enabled: boolean) => void
  onSsoLogin?: () => void
  onSsoLogout?: () => void
  onStackChange: (stack: string) => void
  /** Navigate to a shared resource's home view from the sharing panel
   * (run -> research, collection -> database, template -> prompt library). */
  onOpenSharedResource?: (share: InboxShare) => void
  /** Shared sharing-inbox state (resolved once in the desk so the nav badge
   * and the panel agree). `null` hides the sharing section — sharing is off
   * (none/apikey, no capability, or not authenticated). */
  sharing?: SharingInboxHandle | null
  /** Global authoritative-resource revision used by an open share dialog. */
  sharingRefreshToken: number
  /** One-shot deep link: when set, the workspace focuses this
   * section on mount/changes (the avatar menu's settings entry). */
  requestedSection?: 'security' | null
  selectedStack: string
  stackDiscoveryStatus: StackDiscoveryStatus
  stackOptions: string[]
}

type SettingsSectionId =
  | 'access-tokens'
  | 'admin-audit'
  | 'admin-system'
  | 'admin-users'
  | 'admin-workspaces'
  | 'agent-memory'
  | 'appearance'
  | 'connection'
  | 'licensing'
  | 'preferences'
  | 'quotas'
  | 'security'
  | 'sharing'

export function shouldReloadPatTokensOnSectionChange(
  previousSection: SettingsSectionId,
  activeSection: SettingsSectionId,
): boolean {
  return previousSection !== 'access-tokens' && activeSection === 'access-tokens'
}

type SettingsNavItem = {
  /** Optional count chip (e.g. pending share invitations); hidden when 0. */
  badge?: number
  description: string
  icon: LucideIcon
  id: SettingsSectionId
  label: string
}

type SettingsNavGroup = {
  icon: LucideIcon
  id: 'account' | 'application' | 'admin'
  items: SettingsNavItem[]
  label: string
}

const ADMIN_DATA_SECTION_IDS = new Set<SettingsSectionId>([
  'admin-audit',
  'admin-system',
  'admin-users',
  'admin-workspaces',
  'quotas',
])

const SETTINGS_NAV_RAIL_CENTER_CLASS = 'lg:[--settings-rail-x:0.9375rem]'

export default function SettingsWorkspace({
  apiCapabilities,
  apiError,
  apiHealth,
  apiKey,
  authMode = 'none',
  authSession,
  isDemoMode,
  logoutError = null,
  patAvailable,
  onApiKeyChange,
  onDemoModeChange,
  onSsoLogin,
  onSsoLogout,
  onStackChange,
  onOpenSharedResource,
  requestedSection = null,
  selectedStack,
  sharing = null,
  sharingRefreshToken,
  stackDiscoveryStatus,
  stackOptions,
}: SettingsWorkspaceProps) {
  const {
    contrastMode,
    preset,
    setContrastMode,
    setPreset,
    setTheme,
    setUserBubbleTone,
    theme,
    userBubbleTone,
  } = useTheme()
  const { t } = useLocale()
  // Instance administration is gated on the instance role (default-closed,
  // [[roleAccess]]) or demo — the single platform-admin axis.
  const instanceAdmin = isAdminRole(authSession?.user?.role) || isDemoMode
  // Quota administration is instance-admin power (tenant-wide); resolved once
  // here so the nav gate and the panel share one hook instance.
  const quotaAdmin = useQuotaAdmin({ instanceAdmin })
  // Personal access tokens are per-user (session-scoped server-side), so they
  // are available to ANY authenticated cookie session (or demo), independent
  // of the admin role — not gated behind instance administration.
  const tokensAvailable =
    isDemoMode
    || (authSession?.status === 'authenticated' && patAvailable !== false)
  // The self row in demo is the seeded owner (the real session is anonymous
  // in demo); otherwise the canonical live-session user id.
  const sessionUserId = isDemoMode ? DEMO_OWNER.userId : authSession?.user?.id ?? null
  const adminSystemRuntime = useAdminSystemRuntime({
    demo: isDemoMode,
    enabled: instanceAdmin,
  })
  const adminUsers = useAdminUsers({ demo: isDemoMode, enabled: instanceAdmin })
  const adminWorkspaces = useAdminWorkspaces({
    demo: isDemoMode,
    enabled: instanceAdmin,
  })
  const patTokens = usePatTokens({ demo: isDemoMode, enabled: tokensAvailable })
  const [activeSection, setActiveSection] = useState<SettingsSectionId>('preferences')
  const previousActiveSection = useRef(activeSection)
  useEffect(() => {
    if (requestedSection) setActiveSection(requestedSection)
  }, [requestedSection])
  useEffect(() => {
    const previousSection = previousActiveSection.current
    previousActiveSection.current = activeSection
    if (shouldReloadPatTokensOnSectionChange(previousSection, activeSection)) {
      void patTokens.reload()
    }
  }, [activeSection, patTokens.reload])
  const modeOptions: Array<{
    icon: LucideIcon
    label: string
    value: ThemeMode
  }> = [
    { icon: Sun, label: t.common.light, value: 'light' },
    { icon: Moon, label: t.common.dark, value: 'dark' },
    { icon: Monitor, label: t.common.system, value: 'system' },
  ]
  const presetOptions: Array<{
    accent: string
    description: string
    label: string
    surface: string
    value: ThemePreset
  }> = [
    {
      accent: '#08686D',
      description: t.settings.petrolDescription,
      label: t.settings.petrol,
      surface: '#F0F5F5',
      value: 'petrol',
    },
    {
      accent: 'oklch(0.5 0.18 262)',
      description: t.settings.standardDescription,
      label: t.settings.standard,
      surface: 'oklch(0.971 0.006 255)',
      value: 'standard',
    },
    {
      accent: 'oklch(0.48 0.14 245)',
      description: t.settings.slateDescription,
      label: t.settings.slate,
      surface: 'oklch(0.972 0.008 245)',
      value: 'slate',
    },
    {
      accent: 'oklch(0.42 0.025 260)',
      description: t.settings.graphiteDescription,
      label: t.settings.graphite,
      surface: 'oklch(0.968 0.002 260)',
      value: 'graphite',
    },
    {
      accent: 'oklch(0.48 0.11 155)',
      description: t.settings.sageDescription,
      label: t.settings.sage,
      surface: 'oklch(0.973 0.008 150)',
      value: 'sage',
    },
  ]
  const userBubbleToneOptions: Array<{
    description: string
    label: string
    value: UserBubbleTone
  }> = [
    {
      description: t.settings.userBubbleGrayDescription,
      label: t.settings.userBubbleGray,
      value: 'gray',
    },
    {
      description: t.settings.userBubbleMintDescription,
      label: t.settings.userBubbleMint,
      value: 'mint',
    },
    {
      description: t.settings.userBubbleOrangeDescription,
      label: t.settings.userBubbleOrange,
      value: 'orange',
    },
    {
      description: t.settings.userBubbleSkyDescription,
      label: t.settings.userBubbleSky,
      value: 'sky',
    },
    {
      description: t.settings.userBubbleVioletDescription,
      label: t.settings.userBubbleViolet,
      value: 'violet',
    },
    {
      description: t.settings.userBubbleInkDescription,
      label: t.settings.userBubbleInk,
      value: 'ink',
    },
  ]

  const apiRequestBaseUrl = import.meta.env.VITE_INQTRIX_API_BASE_URL || undefined
  const apiBaseUrl = apiRequestBaseUrl || t.settings.sameOriginApi
  const legal = apiHealth?.legal
  // Server build wins once connected; the SPA build constant covers demo/offline.
  const appVersion = apiHealth?.version ?? __APP_VERSION__
  const hasMultiStackSelection = stackDiscoveryStatus === 'available' && stackOptions.length > 1
  const projectSourceUrl = legal?.source_url ?? t.authLock.repositoryUrl
  const stackModeLabel = stackDiscoveryStatus === 'available'
    ? t.settings.multiStackServer
    : stackDiscoveryStatus === 'unsupported'
      ? t.settings.singleStackServer
      : t.settings.stackDiscoveryPending
  const navGroups: SettingsNavGroup[] = [
    {
      icon: CircleUserRound,
      id: 'account',
      label: t.settings.account,
      items: [
        {
          description: t.settings.preferencesDescription,
          icon: SlidersHorizontal,
          id: 'preferences',
          label: t.settings.preferences,
        },
        {
          description: t.settings.securityDescription,
          icon: Shield,
          id: 'security',
          label: t.settings.security,
        },
        {
          description: t.agentMemory.navDescription,
          icon: Database,
          id: 'agent-memory',
          label: t.agentMemory.navLabel,
        },
        // Sharing management is per-user (your incoming invitations + what you
        // shared), so it lives in the account group. Only present when the
        // sharing surface is enabled (container mode, authenticated, or demo);
        // the badge counts pending invitations awaiting consent.
        ...(sharing
          ? [
              {
                badge: sharing.pendingCount,
                description: t.sharingManagement.navDescription,
                icon: Share2,
                id: 'sharing' as const,
                label: t.sharingManagement.navLabel,
              },
            ]
          : []),
        // Personal access tokens are per-user (session-scoped server-side),
        // not an admin feature: any authenticated cookie-session user manages
        // their OWN tokens, so this lives in the account group.
        ...(tokensAvailable
          ? [
              {
                description: t.adminTokens.navDescription,
                icon: KeyRound,
                id: 'access-tokens' as const,
                label: t.adminTokens.navLabel,
              },
            ]
          : []),
      ],
    },
    {
      icon: Settings,
      id: 'application',
      label: t.settings.application,
      items: [
        {
          description: t.settings.appearanceDescription,
          icon: Palette,
          id: 'appearance',
          label: t.settings.appearance,
        },
        {
          description: t.settings.connectionDescription,
          icon: Server,
          id: 'connection',
          label: t.settings.connection,
        },
      ],
    },
    // Admin section: the single platform-admin axis (instance role, or demo).
    // Each item is independently gated — Users/System on the admin role,
    // Quotas additionally on the quota capability being enabled — so a
    // non-admin never sees the group, and an admin without quotas enabled
    // sees Users/System but not Quotas.
    ...(instanceAdmin
      ? [
          {
            icon: Shield,
            id: 'admin' as const,
            label: t.settings.admin,
            items: [
              ...(instanceAdmin
                ? [
                    {
                      description: t.adminUsers.navDescription,
                      icon: Users,
                      id: 'admin-users' as const,
                      label: t.adminUsers.navLabel,
                    },
                    {
                      description: t.adminWorkspaces.navDescription,
                      icon: LayoutGrid,
                      id: 'admin-workspaces' as const,
                      label: t.adminWorkspaces.navLabel,
                    },
                  ]
                : []),
              ...(quotaAdmin.state.available
                ? [
                    {
                      description: t.quotaAdmin.navDescription,
                      icon: Gauge,
                      id: 'quotas' as const,
                      label: t.quotaAdmin.navLabel,
                    },
                  ]
                : []),
              ...(instanceAdmin
                ? [
                    {
                      description: t.adminSystem.navDescription,
                      icon: Server,
                      id: 'admin-system' as const,
                      label: t.adminSystem.navLabel,
                    },
                    {
                      description: t.adminSystem.auditNavDescription,
                      icon: ScrollText,
                      id: 'admin-audit' as const,
                      label: t.adminSystem.auditNavLabel,
                    },
                  ]
                : []),
            ],
          },
        ]
      : []),
  ]
  const standaloneItems: SettingsNavItem[] = [
    {
      description: t.settings.licensingDescription,
      icon: Scale,
      id: 'licensing',
      label: t.settings.licensing,
    },
  ]
  const activeItem =
    [...navGroups.flatMap((group) => group.items), ...standaloneItems].find(
      (item) => item.id === activeSection,
    ) ?? navGroups[0].items[0]

  return (
    <SettingsShell>
      <SettingsSidebar
        activeSection={activeSection}
        groups={navGroups}
        isDemoMode={isDemoMode}
        onSectionChange={setActiveSection}
        standaloneItems={standaloneItems}
      />
      <SettingsPanel
        activeItem={activeItem}
        adminUsers={adminUsers}
        adminSystemRuntime={adminSystemRuntime}
        adminWorkspaces={adminWorkspaces}
        apiBaseUrl={apiBaseUrl}
        apiRequestBaseUrl={apiRequestBaseUrl}
        apiCapabilities={apiCapabilities ?? null}
        apiError={apiError}
        apiHealth={apiHealth}
        apiKey={apiKey}
        appVersion={appVersion}
        authMode={authMode}
        authSession={authSession}
        onSsoLogin={onSsoLogin}
        onSsoLogout={onSsoLogout}
        contrastMode={contrastMode}
        hasMultiStackSelection={hasMultiStackSelection}
        isDemoMode={isDemoMode}
        logoutError={logoutError}
        legal={legal}
        modeOptions={modeOptions}
        onApiKeyChange={onApiKeyChange}
        onDemoModeChange={onDemoModeChange}
        onStackChange={onStackChange}
        patTokens={patTokens}
        preset={preset}
        presetOptions={presetOptions}
        projectSourceUrl={projectSourceUrl}
        quotaAdmin={quotaAdmin}
        selectedStack={selectedStack}
        sessionUserId={sessionUserId}
        sharing={sharing}
        sharingRefreshToken={sharingRefreshToken}
        onOpenSharedResource={onOpenSharedResource}
        setContrastMode={setContrastMode}
        setPreset={setPreset}
        setTheme={setTheme}
        setUserBubbleTone={setUserBubbleTone}
        stackDiscoveryStatus={stackDiscoveryStatus}
        stackModeLabel={stackModeLabel}
        stackOptions={stackOptions}
        theme={theme}
        userBubbleTone={userBubbleTone}
        userBubbleToneOptions={userBubbleToneOptions}
      />
    </SettingsShell>
  )
}

function SettingsShell({ children }: { children: ReactNode }) {
  return (
    // Settings has no structural wait, so its complete shell swaps directly.
    // Mount-wide motion is reserved for bounded regions that are genuinely
    // waiting on data or layout.
    <div className="flex min-h-0 w-full bg-canvas lg:h-full">
      <section className="grid min-h-[calc(100svh-var(--header-h))] w-full grid-rows-[auto_minmax(0,1fr)] lg:h-full lg:min-h-0 lg:grid-cols-[224px_minmax(0,1fr)] lg:grid-rows-1">
        {children}
      </section>
    </div>
  )
}

function SettingsSidebar({
  activeSection,
  groups,
  isDemoMode,
  onSectionChange,
  standaloneItems,
}: {
  activeSection: SettingsSectionId
  groups: SettingsNavGroup[]
  standaloneItems: SettingsNavItem[]
  isDemoMode: boolean
  onSectionChange: (section: SettingsSectionId) => void
}) {
  const { t } = useLocale()
  const allItems = [...groups.flatMap((group) => group.items), ...standaloneItems]
  const activeItem = allItems.find((item) => item.id === activeSection) ?? allItems[0]
  const ActiveIcon = activeItem?.icon ?? Settings

  return (
    <aside className="flex min-w-0 flex-col border-b border-border bg-surface/50 backdrop-blur lg:border-b-0 lg:border-r">
      <div className="h-[60px] border-b border-border px-2">
        <div className="flex h-full min-w-0 items-center gap-2 border-l-2 border-transparent px-2">
          <Settings className="icon-sm shrink-0 text-foreground/80" />
          <h1 className="truncate t-section text-foreground">{t.settings.title}</h1>
        </div>
      </div>
      <div className="border-b border-border px-3 py-2 lg:hidden">
        <DropdownMenu modal={false}>
          <DropdownMenuTrigger asChild>
            <Button
              aria-label={t.settings.sectionsLabel}
              className="h-8 w-full justify-between gap-2 px-2"
              size="sm"
              type="button"
              variant="outline"
            >
              <span className="flex min-w-0 items-center gap-2">
                <ActiveIcon className="icon-sm shrink-0" />
                <span className="truncate">{activeItem?.label ?? t.settings.sectionsLabel}</span>
              </span>
              <ChevronDown className="icon-sm shrink-0 text-muted-foreground" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-72">
            {groups.map((group) => {
              const GroupIcon = group.icon

              return (
                <div key={group.id}>
                  <DropdownMenuLabel className="flex items-center gap-2 t-caption text-muted-foreground">
                    <GroupIcon className="icon-sm" />
                    {group.label}
                  </DropdownMenuLabel>
                  {group.items.map((item) => {
                    const ItemIcon = item.icon

                    return (
                      <DropdownMenuItem
                        className={cn(activeSection === item.id && 'bg-brand-subtle text-brand focus:bg-brand-subtle focus:text-brand')}
                        key={item.id}
                        onSelect={() => onSectionChange(item.id)}
                      >
                        <ItemIcon className="icon-sm" />
                        {item.label}
                      </DropdownMenuItem>
                    )
                  })}
                  <DropdownMenuSeparator className="last:hidden" />
                </div>
              )
            })}
            {standaloneItems.length > 0 ? (
              <>
                {groups.length > 0 ? <DropdownMenuSeparator /> : null}
                {standaloneItems.map((item) => {
                  const ItemIcon = item.icon

                  return (
                    <DropdownMenuItem
                      className={cn(activeSection === item.id && 'bg-brand-subtle text-brand focus:bg-brand-subtle focus:text-brand')}
                      key={item.id}
                      onSelect={() => onSectionChange(item.id)}
                    >
                      <ItemIcon className="icon-sm" />
                      {item.label}
                    </DropdownMenuItem>
                  )
                })}
              </>
            ) : null}
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
      <nav
        aria-label={t.settings.sectionsLabel}
        className="hidden [scrollbar-width:none] lg:block lg:min-h-0 lg:flex-1 lg:space-y-3 lg:overflow-y-auto lg:px-2 lg:pb-3 [&::-webkit-scrollbar]:hidden"
      >
        {groups.map((group) => {
          const GroupIcon = group.icon

          return (
            <div
              className={cn(
                'flex shrink-0 items-center gap-1.5 lg:block',
                SETTINGS_NAV_RAIL_CENTER_CLASS,
              )}
              key={group.id}
            >
              <p className="hidden h-6 items-center gap-1.5 px-2 t-caption text-foreground lg:flex">
                <GroupIcon className="icon-sm text-foreground/70" />
                {group.label}
              </p>
              <div className="flex gap-1.5 lg:relative lg:flex-col lg:gap-0.5 lg:pl-6 lg:before:absolute lg:before:bottom-1 lg:before:left-[var(--settings-rail-x)] lg:before:top-1 lg:before:w-px lg:before:-translate-x-1/2 lg:before:bg-muted-foreground/25 lg:before:content-['']">
                {group.items.map((item) => (
                  <SettingsNavButton
                    item={item}
                    isActive={activeSection === item.id}
                    key={item.id}
                    nested
                    onClick={() => onSectionChange(item.id)}
                  />
                ))}
              </div>
            </div>
          )
        })}
        {standaloneItems.length > 0 ? (
          <div className="flex shrink-0 items-center gap-1.5 lg:block">
            <div className="flex gap-1.5 lg:flex-col lg:gap-0.5">
              {standaloneItems.map((item) => (
                <SettingsNavButton
                  item={item}
                  isActive={activeSection === item.id}
                  key={item.id}
                  onClick={() => onSectionChange(item.id)}
                />
              ))}
            </div>
          </div>
        ) : null}
      </nav>
      <footer className="hidden border-t border-border px-3 py-2 lg:flex lg:items-center lg:justify-between lg:gap-2">
        <span className="t-caption text-muted-foreground">{t.settings.mode}</span>
        <StatusBadge
          density="table"
          label={isDemoMode ? t.common.demoMode : t.settings.localWorkspace}
          tone={isDemoMode ? 'brand' : 'neutral'}
        />
      </footer>
    </aside>
  )
}

function SettingsNavButton({
  isActive,
  item,
  nested = false,
  onClick,
}: {
  isActive: boolean
  item: SettingsNavItem
  nested?: boolean
  onClick: () => void
}) {
  const Icon = item.icon

  return (
    <button
      aria-current={isActive ? 'page' : undefined}
      className={cn(
        'relative flex h-8 shrink-0 items-center gap-2 rounded-md border-l-2 border-transparent px-2 text-muted-foreground transition-colors hover:bg-accent/70 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring lg:h-7 lg:w-full lg:justify-start',
        nested && 'lg:-ml-6 lg:w-[calc(100%+1.5rem)] lg:pl-8',
        isActive && 'border-brand bg-brand-subtle text-brand hover:bg-brand-subtle hover:text-brand',
      )}
      onClick={onClick}
      title={item.description}
      type="button"
    >
      <Icon className="icon-sm shrink-0" />
      <span className="t-list whitespace-nowrap">{item.label}</span>
      {item.badge && item.badge > 0 ? (
        <span className="ml-auto inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-brand px-1 t-hint font-semibold tabular-nums text-brand-foreground">
          {item.badge > 9 ? '9+' : item.badge}
        </span>
      ) : null}
    </button>
  )
}

function SettingsPanel({
  activeItem,
  adminUsers,
  adminSystemRuntime,
  adminWorkspaces,
  apiBaseUrl,
  apiRequestBaseUrl,
  apiCapabilities,
  apiError,
  apiHealth,
  apiKey,
  appVersion,
  authMode,
  authSession,
  onSsoLogin,
  onSsoLogout,
  contrastMode,
  hasMultiStackSelection,
  isDemoMode,
  logoutError,
  legal,
  modeOptions,
  onApiKeyChange,
  onDemoModeChange,
  onStackChange,
  patTokens,
  preset,
  presetOptions,
  projectSourceUrl,
  quotaAdmin,
  selectedStack,
  sessionUserId,
  setContrastMode,
  setPreset,
  setTheme,
  setUserBubbleTone,
  sharing,
  sharingRefreshToken,
  onOpenSharedResource,
  stackDiscoveryStatus,
  stackModeLabel,
  stackOptions,
  theme,
  userBubbleTone,
  userBubbleToneOptions,
}: {
  activeItem: SettingsNavItem
  adminUsers: ReturnType<typeof useAdminUsers>
  adminSystemRuntime: ReturnType<typeof useAdminSystemRuntime>
  adminWorkspaces: ReturnType<typeof useAdminWorkspaces>
  apiBaseUrl: string
  apiRequestBaseUrl?: string
  apiCapabilities: InqtrixCapabilities | null
  apiError: string | null
  apiHealth: InqtrixHealth | null
  apiKey: string
  appVersion: string
  authMode: AuthMode
  authSession?: {
    status: string
    user: {
      displayName: string | null
      email: string | null
      id: string
      role: string
    } | null
  }
  onSsoLogin?: () => void
  onSsoLogout?: () => void
  contrastMode: 'high' | 'standard'
  hasMultiStackSelection: boolean
  isDemoMode: boolean
  logoutError: string | null
  legal: InqtrixHealth['legal'] | undefined
  modeOptions: Array<{
    icon: LucideIcon
    label: string
    value: ThemeMode
  }>
  onApiKeyChange: (apiKey: string) => void
  onDemoModeChange: (enabled: boolean) => void
  onStackChange: (stack: string) => void
  patTokens: ReturnType<typeof usePatTokens>
  preset: ThemePreset
  presetOptions: Array<{
    accent: string
    description: string
    label: string
    surface: string
    value: ThemePreset
  }>
  projectSourceUrl: string
  quotaAdmin: ReturnType<typeof useQuotaAdmin>
  selectedStack: string
  sessionUserId: string | null
  setContrastMode: (mode: 'high' | 'standard') => void
  setPreset: (preset: ThemePreset) => void
  setTheme: (theme: ThemeMode) => void
  setUserBubbleTone: (tone: UserBubbleTone) => void
  sharing: SharingInboxHandle | null
  sharingRefreshToken: number
  onOpenSharedResource?: (share: InboxShare) => void
  stackDiscoveryStatus: StackDiscoveryStatus
  stackModeLabel: string
  stackOptions: string[]
  theme: ThemeMode
  userBubbleTone: UserBubbleTone
  userBubbleToneOptions: Array<{
    description: string
    label: string
    value: UserBubbleTone
  }>
}) {
  const isAdminDataSection = ADMIN_DATA_SECTION_IDS.has(activeItem.id)

  return (
    <main className="min-h-0 overflow-y-auto overscroll-contain px-4 py-4 [scrollbar-gutter:stable] [scrollbar-width:thin] md:px-6 lg:px-8 lg:py-0 xl:px-10">
      <div
        className={cn(
          'flex w-full flex-col gap-4 pb-8',
          isAdminDataSection ? 'max-w-none' : 'max-w-[920px]',
        )}
      >
        <SettingsPanelHeader item={activeItem} />
        {activeItem.id === 'preferences' ? (
          <PreferencesPanel
            isDemoMode={isDemoMode}
            onDemoModeChange={onDemoModeChange}
          />
        ) : activeItem.id === 'security' ? (
          <SecurityPanel
            apiHealth={apiHealth}
            apiKey={apiKey}
            authMode={authMode}
            authSession={authSession}
            logoutError={logoutError}
            onApiKeyChange={onApiKeyChange}
            onSsoLogin={onSsoLogin}
            onSsoLogout={onSsoLogout}
          />
        ) : activeItem.id === 'appearance' ? (
          <AppearancePanel
            contrastMode={contrastMode}
            modeOptions={modeOptions}
            preset={preset}
            presetOptions={presetOptions}
            setContrastMode={setContrastMode}
            setPreset={setPreset}
            setTheme={setTheme}
            theme={theme}
            userBubbleTone={userBubbleTone}
            userBubbleToneOptions={userBubbleToneOptions}
            setUserBubbleTone={setUserBubbleTone}
          />
        ) : activeItem.id === 'connection' ? (
          <ConnectionPanel
            apiBaseUrl={apiBaseUrl}
            apiError={apiError}
            apiHealth={apiHealth}
            authMode={authMode}
            hasMultiStackSelection={hasMultiStackSelection}
            onStackChange={onStackChange}
            selectedStack={selectedStack}
            stackDiscoveryStatus={stackDiscoveryStatus}
            stackModeLabel={stackModeLabel}
            stackOptions={stackOptions}
          />
        ) : activeItem.id === 'agent-memory' ? (
          <AgentMemoryPanel
            apiKey={apiKey}
            apiRequestBaseUrl={apiRequestBaseUrl}
          />
        ) : activeItem.id === 'admin-users' ? (
          <UsersPanel
            admin={adminUsers}
            mode={authMode ?? 'none'}
            sessionUserId={sessionUserId}
          />
        ) : activeItem.id === 'admin-workspaces' ? (
          <AdminWorkspacesPanel
            admin={adminWorkspaces}
            users={adminUsers.state.users}
          />
        ) : activeItem.id === 'access-tokens' ? (
          <AccessTokensPanel tokens={patTokens} />
        ) : activeItem.id === 'admin-system' ? (
          <SystemStatusPanel
            capabilities={isDemoMode ? seedSystemCapabilities() : apiCapabilities}
            health={isDemoMode ? seedSystemHealth() : apiHealth}
            runtime={adminSystemRuntime.state.runtime}
            runtimeError={adminSystemRuntime.state.error}
            runtimeStatus={adminSystemRuntime.state.status}
          />
        ) : activeItem.id === 'admin-audit' ? (
          <AdminAuditSection
            demo={isDemoMode}
            enabled={isAdminRole(authSession?.user?.role) || isDemoMode}
            traceUiConfigured={
              adminSystemRuntime.state.runtime?.observability
                ?.ui_link_configured ?? false
            }
          />
        ) : activeItem.id === 'quotas' ? (
          <QuotaAdminPanel admin={quotaAdmin} />
        ) : activeItem.id === 'sharing' && sharing ? (
          <SharingPanel
            demo={isDemoMode}
            onOpen={onOpenSharedResource ?? (() => {})}
            ownerEmail={isDemoMode ? DEMO_OWNER.email : authSession?.user?.email ?? null}
            ownerName={
              isDemoMode ? DEMO_OWNER.displayName : authSession?.user?.displayName ?? null
            }
            refreshToken={sharingRefreshToken}
            sharing={sharing}
          />
        ) : (
          <LicensingPanel
            appVersion={appVersion}
            legal={legal}
            projectSourceUrl={projectSourceUrl}
          />
        )}
      </div>
    </main>
  )
}

function SettingsPanelHeader({ item }: { item: SettingsNavItem }) {
  return (
    <header className="flex h-[60px] min-w-0 flex-col justify-center border-b border-border">
      <h2 className="t-section text-foreground">{item.label}</h2>
      <p className="mt-0.5 max-w-2xl truncate t-meta text-muted-foreground">
        {item.description}
      </p>
    </header>
  )
}

function PreferencesPanel({
  isDemoMode,
  onDemoModeChange,
}: {
  isDemoMode: boolean
  onDemoModeChange: (enabled: boolean) => void
}) {
  const { locale, setLocale, t } = useLocale()
  const { agentModelTier, chatModelTier, setAgentModelTier, setChatModelTier } = useTheme()
  // Two rows, never one. An agent run fans out over several thinking nodes
  // while a chat answer is a single call, so a chat pick must not raise agent
  // spend — the same split the composer state keeps.
  const modelTierOptions: Array<{ label: string; value: ModelTierPreference }> = [
    { label: t.settings.modelTierServerDefault, value: '' },
    { label: t.settings.modelTierHigh, value: 'high' },
    { label: t.settings.modelTierMid, value: 'mid' },
    { label: t.settings.modelTierFast, value: 'fast' },
  ]

  return (
    <SettingsSection>
      <div className="flex flex-col gap-2.5 px-3 py-3 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
        <div className="min-w-0 sm:flex-1">
          <h4 className="t-list text-foreground">{t.settings.demoMode}</h4>
          <p className="mt-0.5 t-meta text-muted-foreground">{t.settings.demoModeDescription}</p>
          <div
            className="mt-2 flex gap-2 rounded-md border border-warning/25 bg-warning-subtle/35 p-2.5 t-meta text-foreground"
            id="demo-mode-warning"
          >
            <AlertTriangle className="mt-0.5 icon-sm shrink-0 text-warning" />
            <p className="min-w-0">{t.settings.demoModeWarning}</p>
          </div>
        </div>
        <div className="shrink-0 sm:mt-0.5">
          <Switch
            aria-describedby="demo-mode-warning"
            aria-label={t.settings.demoMode}
            checked={isDemoMode}
            onCheckedChange={onDemoModeChange}
          />
        </div>
      </div>
      <SettingsRow
        description={t.settings.languageDescription}
        title={t.common.language}
      >
        <SettingsSegmented
          ariaLabel={t.common.language}
          onChange={setLocale}
          options={[
            { label: 'DE', value: 'de' },
            { label: 'EN', value: 'en' },
          ]}
          value={locale}
        />
      </SettingsRow>
      <SettingsRow
        description={t.settings.chatModelTierDescription}
        title={t.settings.chatModelTier}
      >
        <SettingsSegmented
          ariaLabel={t.settings.chatModelTier}
          onChange={setChatModelTier}
          options={modelTierOptions}
          value={chatModelTier}
          wrap
        />
      </SettingsRow>
      <SettingsRow
        description={t.settings.agentModelTierDescription}
        title={t.settings.agentModelTier}
      >
        <SettingsSegmented
          ariaLabel={t.settings.agentModelTier}
          onChange={setAgentModelTier}
          options={modelTierOptions}
          value={agentModelTier}
          wrap
        />
      </SettingsRow>
      <SettingsRow
        description={t.settings.workspaceStateDescription}
        title={t.settings.workspaceState}
      >
        <StatusBadge
          label={isDemoMode ? t.common.demoMode : t.settings.localWorkspace}
          tone={isDemoMode ? 'brand' : 'neutral'}
        />
      </SettingsRow>
    </SettingsSection>
  )
}

function AgentMemoryPanel({
  apiKey,
  apiRequestBaseUrl,
}: {
  apiKey: string
  apiRequestBaseUrl?: string
}) {
  const { t } = useLocale()
  const { agentMemoryEnabled, setAgentMemoryEnabled } = useTheme()
  const memory = useAgentMemory({ apiKey, baseUrl: apiRequestBaseUrl })
  const [drafts, setDrafts] = useState<Record<string, AgentMemoryWire>>({})
  const [busyId, setBusyId] = useState<string | null>(null)

  useEffect(() => {
    setDrafts(
      Object.fromEntries(
        memory.memories.map((item) => [item.id, { ...item }]),
      ),
    )
  }, [memory.memories])

  const status = memory.status
  const unavailable = !status || !status.available
  const pendingCandidates = pendingAgentMemoryCandidates(memory.candidates)
  const feedbackRows = visibleAgentFeedback(memory.feedback)

  async function runAction(id: string, action: () => Promise<void>) {
    setBusyId(id)
    try {
      await action()
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="flex flex-col gap-5">
      <SettingsSection
        description={t.agentMemory.enableSectionDescription}
        title={t.agentMemory.enableSectionTitle}
      >
        <SettingsRow
          description={t.agentMemory.enableDescription}
          descriptionId="agent-memory-enable-description"
          title={t.agentMemory.enableTitle}
        >
          <Switch
            aria-describedby="agent-memory-enable-description"
            aria-label={t.agentMemory.enableTitle}
            checked={agentMemoryEnabled}
            onCheckedChange={setAgentMemoryEnabled}
          />
        </SettingsRow>
      </SettingsSection>
      <SettingsSection
        description={t.agentMemory.statusDescription}
        title={t.agentMemory.statusTitle}
      >
        <SettingsRow
          description={
            status?.principal_eligible === false
              ? t.agentMemory.authRequired
              : t.agentMemory.statusHelper
          }
          title={t.agentMemory.provider}
        >
          <div className="flex flex-wrap justify-end gap-2">
            <StatusBadge
              label={
                unavailable
                  ? t.agentMemory.unavailable
                  : t.agentMemory.available
              }
              tone={unavailable ? 'warning' : 'success'}
            />
            <StatusBadge
              label={agentMemoryModeLabel(status)}
              tone="neutral"
            />
            {status?.degraded_reason ? (
              <StatusBadge label={t.agentMemory.degraded} tone="warning" />
            ) : null}
          </div>
        </SettingsRow>
        {memory.error ? (
          <div className="mx-3 rounded-md border border-warning/25 bg-warning-subtle/35 p-2.5 t-meta text-foreground">
            {memory.error}
          </div>
        ) : null}
      </SettingsSection>

      <SettingsSection
        description={t.agentMemory.candidatesDescription}
        title={t.agentMemory.candidatesTitle}
      >
        {pendingCandidates.length === 0 ? (
          <SettingsRow
            description={t.agentMemory.noCandidatesDescription}
            title={t.agentMemory.noCandidates}
          >
            <StatusBadge label={t.agentMemory.empty} tone="neutral" />
          </SettingsRow>
        ) : (
          pendingCandidates.map((candidate) => (
            <div
              className="grid gap-2 rounded-md px-3 py-2.5 transition-colors hover:bg-surface/45"
              key={candidate.id}
            >
              <div className="flex min-w-0 flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <h4 className="t-list text-foreground">{candidate.content}</h4>
                  <p className="mt-0.5 t-meta text-muted-foreground">
                    {candidate.scope} · {candidate.category} · {candidate.reason}
                  </p>
                </div>
                <div className="flex shrink-0 gap-1.5">
                  <Button
                    disabled={busyId === candidate.id || unavailable}
                    onClick={() =>
                      runAction(candidate.id, () =>
                        memory.acceptCandidate(candidate.id),
                      )
                    }
                    size="sm"
                  >
                    <Check className="icon-sm" />
                    {t.agentMemory.accept}
                  </Button>
                  <Button
                    disabled={busyId === candidate.id}
                    onClick={() =>
                      runAction(candidate.id, () =>
                        memory.rejectCandidate(candidate.id),
                      )
                    }
                    size="sm"
                    variant="outline"
                  >
                    <X className="icon-sm" />
                    {t.agentMemory.reject}
                  </Button>
                </div>
              </div>
            </div>
          ))
        )}
      </SettingsSection>

      <SettingsSection
        description={t.agentMemory.memoriesDescription}
        title={t.agentMemory.memoriesTitle}
      >
        <SettingsRow
          description={t.agentMemory.searchDescription}
          title={t.agentMemory.searchTitle}
        >
          <div className="flex w-full max-w-sm items-center justify-end gap-1.5">
            <div className="relative min-w-0 flex-1">
              <Search className="pointer-events-none absolute left-2.5 top-1/2 icon-sm -translate-y-1/2 text-muted-foreground" />
              <Input
                aria-label={t.agentMemory.searchTitle}
                className="pl-8"
                disabled={unavailable}
                onChange={(event) => memory.setSearchQuery(event.target.value)}
                placeholder={t.agentMemory.searchPlaceholder}
                value={memory.searchQuery}
              />
            </div>
            <Button
              aria-label={t.agentMemory.clearSearch}
              disabled={!memory.searchQuery}
              onClick={() => memory.setSearchQuery('')}
              size="icon"
              type="button"
              variant="outline"
            >
              <X className="icon-sm" />
            </Button>
          </div>
        </SettingsRow>
        {memory.memories.length === 0 ? (
          <SettingsRow
            description={t.agentMemory.noMemoriesDescription}
            title={t.agentMemory.noMemories}
          >
            <StatusBadge label={t.agentMemory.empty} tone="neutral" />
          </SettingsRow>
        ) : (
          memory.memories.map((item) => {
            const draft = drafts[item.id] ?? item

            return (
              <div
                className="grid gap-2 rounded-md px-3 py-2.5 transition-colors hover:bg-surface/45"
                key={item.id}
              >
                <div className="flex min-w-0 items-center justify-between gap-3">
                  <div className="min-w-0">
                    <h4 className="t-list text-foreground">
                      {draft.scope} · {draft.category}
                    </h4>
                    <p className="mt-0.5 truncate t-mono text-muted-foreground">
                      {item.id}
                    </p>
                  </div>
                  <div className="flex shrink-0 gap-1.5">
                    <Button
                      aria-label={t.agentMemory.feedbackGivePositive}
                      disabled={
                        busyId === item.id ||
                        unavailable ||
                        !item.source_run_id
                      }
                      onClick={() =>
                        runAction(item.id, () =>
                          memory.submitFeedback(item, 'positive'),
                        )
                      }
                      size="sm"
                      title={t.agentMemory.feedbackGivePositive}
                      variant="ghost"
                    >
                      <ThumbsUp className="icon-sm" />
                    </Button>
                    <Button
                      aria-label={t.agentMemory.feedbackGiveNegative}
                      disabled={
                        busyId === item.id ||
                        unavailable ||
                        !item.source_run_id
                      }
                      onClick={() =>
                        runAction(item.id, () =>
                          memory.submitFeedback(item, 'negative'),
                        )
                      }
                      size="sm"
                      title={t.agentMemory.feedbackGiveNegative}
                      variant="ghost"
                    >
                      <ThumbsDown className="icon-sm" />
                    </Button>
                    <Button
                      disabled={busyId === item.id || unavailable}
                      onClick={() =>
                        runAction(item.id, () => memory.updateMemory(draft))
                      }
                      size="sm"
                      variant="outline"
                    >
                      <Save className="icon-sm" />
                      {t.common.save}
                    </Button>
                    <Button
                      disabled={busyId === item.id}
                      onClick={() =>
                        runAction(item.id, () => memory.deleteMemory(item.id))
                      }
                      size="sm"
                      variant="outline"
                    >
                      <Trash2 className="icon-sm" />
                      {t.common.delete}
                    </Button>
                  </div>
                </div>
                <textarea
                  aria-label={t.agentMemory.memoryContent}
                  className="min-h-20 resize-y rounded-md border border-border bg-background px-3 py-2 text-sm text-foreground outline-none transition focus-visible:ring-2 focus-visible:ring-ring"
                  onChange={(event) =>
                    setDrafts((current) => ({
                      ...current,
                      [item.id]: { ...draft, content: event.target.value },
                    }))
                  }
                  value={draft.content}
                />
              </div>
            )
          })
        )}
        <SettingsRow
          description={t.agentMemory.clearDescription}
          title={t.agentMemory.clearTitle}
        >
          <Button
            disabled={memory.memories.length === 0 || busyId === 'clear'}
            onClick={() => runAction('clear', memory.clearAll)}
            size="sm"
            variant="outline"
          >
            <Trash2 className="icon-sm" />
            {t.agentMemory.clearAll}
          </Button>
        </SettingsRow>
      </SettingsSection>

      <SettingsSection
        description={t.agentMemory.feedbackDescription}
        title={t.agentMemory.feedbackTitle}
      >
        {feedbackRows.length === 0 ? (
          <SettingsRow
            description={t.agentMemory.noFeedbackDescription}
            title={t.agentMemory.noFeedback}
          >
            <StatusBadge label={t.agentMemory.empty} tone="neutral" />
          </SettingsRow>
        ) : (
          feedbackRows.map((row) => (
            <div
              className="grid gap-1 rounded-md px-3 py-2.5 transition-colors hover:bg-surface/45"
              key={row.id}
            >
              <div className="flex min-w-0 items-start justify-between gap-3">
                <div className="min-w-0">
                  <h4 className="t-list text-foreground">
                    {row.feedback === 'positive'
                      ? t.agentMemory.feedbackPositive
                      : row.feedback === 'negative'
                        ? t.agentMemory.feedbackNegative
                        : t.agentMemory.feedbackNeutral}
                  </h4>
                  <p className="mt-0.5 truncate t-mono text-muted-foreground">
                    {row.run_id}
                  </p>
                </div>
                <p className="shrink-0 t-meta text-muted-foreground">
                  {new Date(row.created_at * 1000).toLocaleString()}
                </p>
              </div>
              {row.reason ? (
                <p className="t-meta text-muted-foreground">{row.reason}</p>
              ) : null}
            </div>
          ))
        )}
      </SettingsSection>
    </div>
  )
}

function SecurityPanel({
  apiHealth,
  apiKey,
  authMode = 'none',
  authSession,
  logoutError,
  onApiKeyChange,
  onSsoLogin,
  onSsoLogout,
}: {
  apiHealth: InqtrixHealth | null
  apiKey: string
  authMode?: AuthMode
  authSession?: {
    status: string
    user: { displayName: string | null; email: string | null } | null
  }
  logoutError?: string | null
  onApiKeyChange: (apiKey: string) => void
  onSsoLogin?: () => void
  onSsoLogout?: () => void
}) {
  const { t } = useLocale()
  const shouldShowTokenInput = apiHealth?.auth_required || apiKey

  // Every cookie-session mode (oidc/local) shows the identity + a
  // sign-out row, NOT the inert Bearer-token input — auth rides the session
  // cookie there, so a token field would do nothing and offer no logout.
  if (isCookieSessionMode(authMode)) {
    const signedIn = authSession?.status === 'authenticated'
    return (
      <div className="flex flex-col gap-6">
        <SettingsSection>
          <SettingsRow
            description={
              signedIn
                ? authSession?.user?.email ?? t.settings.ssoSignedInDescription
                : t.settings.ssoSignedOutDescription
            }
            title={
              signedIn
                ? t.settings.ssoSignedInAs.replace(
                    '{name}',
                    authSession?.user?.displayName ?? authSession?.user?.email ?? '?',
                  )
                : t.settings.ssoTitle
            }
          >
            {signedIn ? (
              <Button onClick={onSsoLogout} size="sm" variant="outline">
                {t.settings.ssoLogout}
              </Button>
            ) : (
              <Button onClick={onSsoLogin} size="sm">
                {t.settings.ssoLogin}
              </Button>
            )}
          </SettingsRow>
          {logoutError ? (
            <p className="mt-2 t-meta text-destructive">
              {t.settings.ssoLogoutFailed}: {logoutError}
            </p>
          ) : null}
        </SettingsSection>
        {/* Only local accounts have a password Inqtrix owns; oidc
            passwords live upstream. */}
        {authMode === 'local' && signedIn ? <ChangePasswordSection /> : null}
      </div>
    )
  }

  return (
    <SettingsSection>
      {shouldShowTokenInput ? (
        <SettingsRow
          description={t.settings.runtimeTokenDescription}
          title={t.settings.apiToken}
        >
          <input
            aria-label={t.settings.apiToken}
            autoComplete="off"
            className="h-9 w-full rounded-md border border-border bg-background px-3 text-left text-sm text-foreground shadow-[0_1px_2px_var(--shadow-hairline)] outline-none transition focus-visible:ring-2 focus-visible:ring-ring sm:w-72"
            onChange={(event) => onApiKeyChange(event.target.value)}
            placeholder={t.settings.apiTokenPlaceholder}
            type="password"
            value={apiKey}
          />
        </SettingsRow>
      ) : (
        <SettingsRow
          description={t.settings.noBearerRequiredDescription}
          title={t.settings.noBearerRequired}
        >
          <StatusBadge label={t.settings.notConnected} tone="neutral" />
        </SettingsRow>
      )}
    </SettingsSection>
  )
}

function ChangePasswordSection() {
  const { t } = useLocale()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [status, setStatus] = useState<'idle' | 'saving' | 'done'>('idle')
  const [error, setError] = useState<string | null>(null)

  const canSubmit =
    current.length > 0 &&
    isPasswordAcceptable(next) &&
    next === confirm &&
    status !== 'saving'

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    if (next !== confirm) {
      setError(t.changePassword.mismatch)
      return
    }
    setStatus('saving')
    try {
      await changePassword({ currentPassword: current, newPassword: next })
      setStatus('done')
      setCurrent('')
      setNext('')
      setConfirm('')
    } catch (caught) {
      setError(
        hasHttpStatus(caught, 401)
          ? t.changePassword.wrongCurrent
          : t.changePassword.failed,
      )
      setStatus('idle')
    }
  }

  return (
    <SettingsSection
      description={t.changePassword.description}
      title={t.changePassword.title}
    >
      <form className="space-y-4 px-3 py-3" onSubmit={handleSubmit}>
        <PasswordField
          autoComplete="current-password"
          id="cp-current"
          label={t.changePassword.current}
          onChange={(value) => {
            setCurrent(value)
            setStatus('idle')
          }}
          value={current}
        />
        <PasswordField
          autoComplete="new-password"
          id="cp-next"
          label={t.changePassword.next}
          onChange={setNext}
          value={next}
        />
        <PasswordField
          autoComplete="new-password"
          id="cp-confirm"
          label={t.changePassword.confirm}
          onChange={setConfirm}
          value={confirm}
        />
        {error ? (
          <p className="t-meta text-destructive" role="alert">
            {error}
          </p>
        ) : null}
        {status === 'done' ? (
          <p className="t-meta text-success" role="status">
            {t.changePassword.success}
          </p>
        ) : null}
        <div className="flex justify-end">
          <Button
            className="bg-brand text-brand-foreground hover:bg-brand/90"
            disabled={!canSubmit}
            size="sm"
            type="submit"
          >
            {status === 'saving'
              ? t.changePassword.submitting
              : t.changePassword.submit}
          </Button>
        </div>
      </form>
    </SettingsSection>
  )
}

function PasswordField({
  autoComplete,
  id,
  label,
  onChange,
  value,
}: {
  autoComplete: string
  id: string
  label: string
  onChange: (value: string) => void
  value: string
}) {
  return (
    <div>
      <label className="t-label mb-1.5 block text-foreground" htmlFor={id}>
        {label}
      </label>
      <Input
        autoComplete={autoComplete}
        className="sm:max-w-sm"
        id={id}
        onChange={(event) => onChange(event.target.value)}
        type="password"
        value={value}
      />
    </div>
  )
}

function AppearancePanel({
  contrastMode,
  modeOptions,
  preset,
  presetOptions,
  setContrastMode,
  setPreset,
  setTheme,
  setUserBubbleTone,
  theme,
  userBubbleTone,
  userBubbleToneOptions,
}: {
  contrastMode: 'high' | 'standard'
  modeOptions: Array<{
    icon: LucideIcon
    label: string
    value: ThemeMode
  }>
  preset: ThemePreset
  presetOptions: Array<{
    accent: string
    description: string
    label: string
    surface: string
    value: ThemePreset
  }>
  setContrastMode: (mode: 'high' | 'standard') => void
  setPreset: (preset: ThemePreset) => void
  setTheme: (theme: ThemeMode) => void
  setUserBubbleTone: (tone: UserBubbleTone) => void
  theme: ThemeMode
  userBubbleTone: UserBubbleTone
  userBubbleToneOptions: Array<{
    description: string
    label: string
    value: UserBubbleTone
  }>
}) {
  const { t } = useLocale()

  return (
    <SettingsSection>
      <SettingsRow description={t.settings.modeDescription} title={t.settings.mode}>
        <SettingsSegmented
          ariaLabel={t.settings.mode}
          onChange={setTheme}
          options={modeOptions}
          value={theme}
        />
      </SettingsRow>
      <SettingsRowBlock description={t.settings.themeDescription} title={t.settings.theme}>
        <div className="grid grid-cols-1 gap-1.5 md:grid-cols-2">
          {presetOptions.map((option) => {
            const isActive = preset === option.value

            return (
              <AppearanceChoiceButton
                active={isActive}
                description={option.description}
                key={option.value}
                label={option.label}
                onSelect={setPreset}
                swatch={
                  <ThemePresetSwatch
                    accent={option.accent}
                    surface={option.surface}
                  />
                }
                value={option.value}
              />
            )
          })}
        </div>
      </SettingsRowBlock>
      <SettingsRowBlock
        description={t.settings.userBubbleToneDescription}
        title={t.settings.userBubbleTone}
      >
        <div className="grid grid-cols-1 gap-1.5 md:grid-cols-2">
          {userBubbleToneOptions.map((option) => {
            const isActive = userBubbleTone === option.value

            return (
              <AppearanceChoiceButton
                active={isActive}
                description={option.description}
                key={option.value}
                label={option.label}
                onSelect={setUserBubbleTone}
                swatch={<UserBubbleToneSwatch tone={option.value} />}
                value={option.value}
              />
            )
          })}
        </div>
      </SettingsRowBlock>
      <SettingsRow
        description={t.settings.highContrastDescription}
        descriptionId="high-contrast-description"
        title={t.settings.highContrast}
      >
        <Switch
          aria-describedby="high-contrast-description"
          aria-label={t.settings.highContrast}
          checked={contrastMode === 'high'}
          onCheckedChange={(checked) => setContrastMode(checked ? 'high' : 'standard')}
        />
      </SettingsRow>
    </SettingsSection>
  )
}

function AppearanceChoiceButton<T extends string>({
  active,
  description,
  label,
  onSelect,
  swatch,
  value,
}: {
  active: boolean
  description: string
  label: string
  onSelect: (value: T) => void
  swatch: ReactNode
  value: T
}) {
  return (
    <button
      aria-pressed={active}
      className={cn(
        'group flex min-h-11 items-center gap-2 rounded-md border border-border bg-background px-2 py-1.5 text-left transition-colors hover:border-brand/40 hover:bg-surface/45 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
        active && 'border-brand/45 bg-brand-subtle hover:bg-brand-subtle',
      )}
      onClick={() => onSelect(value)}
      type="button"
    >
      {swatch}
      <span className="min-w-0 flex-1">
        <span className={cn('block truncate t-list', active ? 'text-brand' : 'text-foreground')}>
          {label}
        </span>
        <span
          className={cn(
            'mt-0.5 block truncate t-meta-sm',
            active ? 'text-brand' : 'text-muted-foreground',
          )}
        >
          {description}
        </span>
      </span>
      <span
        aria-hidden
        className={cn(
          'ml-auto inline-flex size-4 shrink-0 items-center justify-center rounded-full border',
          active
            ? 'border-brand bg-brand text-brand-foreground'
            : 'border-transparent text-transparent',
        )}
      >
        {active ? <Check className="icon-xs" /> : null}
      </span>
    </button>
  )
}

function ThemePresetSwatch({
  accent,
  surface,
}: {
  accent: string
  surface: string
}) {
  return (
    <span
      aria-hidden
      className="flex h-8 w-14 shrink-0 items-center justify-center rounded-md border border-border bg-card p-1 shadow-[0_1px_2px_var(--shadow-hairline)]"
    >
      <span
        className="flex h-full w-full items-center gap-1 overflow-hidden rounded-[5px] px-1.5"
        style={{ background: surface }}
      >
        <span className="h-1.5 w-6 rounded-full" style={{ background: accent }} />
        <span className="h-1.5 w-3 rounded-full opacity-40" style={{ background: accent }} />
      </span>
    </span>
  )
}

function UserBubbleToneSwatch({ tone }: { tone: UserBubbleTone }) {
  return (
    <span
      aria-hidden
      className="inqtrix-user-bubble-tone-preview flex h-8 w-14 shrink-0 items-center justify-center rounded-md border border-border bg-card p-1 shadow-[0_1px_2px_var(--shadow-hairline)]"
      data-user-bubble-tone={tone}
    >
      <span className="inqtrix-user-bubble h-3 w-9 rounded-full border shadow-[0_1px_2px_var(--shadow-hairline)]" />
    </span>
  )
}

function ConnectionPanel({
  apiBaseUrl,
  apiError,
  apiHealth,
  authMode,
  hasMultiStackSelection,
  onStackChange,
  selectedStack,
  stackDiscoveryStatus,
  stackModeLabel,
  stackOptions,
}: {
  apiBaseUrl: string
  apiError: string | null
  apiHealth: InqtrixHealth | null
  authMode: AuthMode
  hasMultiStackSelection: boolean
  onStackChange: (stack: string) => void
  selectedStack: string
  stackDiscoveryStatus: StackDiscoveryStatus
  stackModeLabel: string
  stackOptions: string[]
}) {
  const { t } = useLocale()
  const modelRows = apiHealth
    ? [
        { label: t.settings.classifyModel, value: apiHealth.classify_model },
        { label: t.settings.evaluateModel, value: apiHealth.evaluate_model },
        { label: t.settings.searchModel, value: apiHealth.search_model },
        { label: t.settings.summarizeModel, value: apiHealth.summarize_model },
        { label: t.settings.reasoningModel, value: apiHealth.reasoning_model },
      ].filter((row) => Boolean(row.value))
    : []
  const hasModelInfo =
    modelRows.length > 0
    || Boolean(apiHealth?.report_profile)
    || Boolean(apiHealth?.model_tier)
    || Boolean(apiHealth?.testing_mode)

  return (
    <>
      <SettingsSection
        description={t.settings.apiStatusDescription}
        title={t.settings.apiConnection}
      >
        <div className="grid gap-2 px-3 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge
              label={apiHealth?.status ?? t.settings.notConnected}
              tone={apiHealth?.status === 'ok' ? 'success' : 'neutral'}
            />
            {apiHealth?.auth_required ? (
              <StatusBadge
                label={
                  authMode === 'apikey'
                    ? t.settings.authRequired
                    : isCookieSessionMode(authMode)
                      ? t.settings.sessionAuthEnabled
                      : t.settings.authenticationRequired
                }
                tone={isCookieSessionMode(authMode) ? 'neutral' : 'warning'}
              />
            ) : null}
            {apiHealth ? <StatusBadge label={stackModeLabel} tone="neutral" /> : null}
          </div>
          <p className="break-words t-meta text-muted-foreground">
            <span className="font-medium text-foreground">{t.settings.baseUrl}:</span> {apiBaseUrl}
          </p>
          {apiError ? (
            <p className="t-meta text-destructive">{apiError}</p>
          ) : null}
        </div>
      </SettingsSection>
      {apiHealth ? (
        <SettingsSection
          description={t.settings.providerMetadataDescription}
          title={t.settings.providerMetadata}
        >
          <div className="grid gap-1 px-3 py-3 t-meta text-muted-foreground sm:grid-cols-2">
            <span className="min-w-0 truncate">
              {t.settings.llmProvider}: <strong className="font-semibold text-foreground">{apiHealth.llm.provider}</strong>
            </span>
            <span className="min-w-0 truncate">
              {t.settings.searchProvider}: <strong className="font-semibold text-foreground">{apiHealth.search.provider}</strong>
            </span>
          </div>
          <SettingsRow description={t.settings.stackDescription} title={t.settings.currentStack}>
            {hasMultiStackSelection ? (
              <SettingsSegmented
                ariaLabel={t.common.stack}
                onChange={onStackChange}
                options={stackOptions.map((stack) => ({ label: stack, value: stack }))}
                value={selectedStack}
                wrap
              />
            ) : (
              <div className="grid gap-1">
                <div className="flex min-w-0 flex-wrap items-center gap-2 sm:justify-end">
                  <StatusBadge label={stackModeLabel} tone="neutral" />
                  <span className="min-w-0 truncate t-card text-foreground">
                    {selectedStack}
                  </span>
                </div>
                {stackDiscoveryStatus === 'unsupported' ? (
                  <p className="t-meta text-muted-foreground sm:text-right">
                    {t.settings.singleStackDescription}
                  </p>
                ) : null}
              </div>
            )}
          </SettingsRow>
        </SettingsSection>
      ) : null}
      {apiHealth && hasModelInfo ? (
        <SettingsSection
          description={t.settings.activeModelsDescription}
          title={t.settings.activeModels}
        >
          <div className="grid gap-x-6 gap-y-1.5 px-3 py-3 t-meta sm:grid-cols-2">
            {modelRows.map((row) => (
              <span className="min-w-0 truncate text-muted-foreground" key={row.label}>
                {row.label}: <strong className="font-semibold text-foreground">{row.value}</strong>
              </span>
            ))}
            {apiHealth.report_profile || apiHealth.model_tier || apiHealth.testing_mode ? (
              <div className="flex flex-wrap items-center gap-2 pt-1 sm:col-span-2">
                {apiHealth.report_profile ? (
                  <span className="text-muted-foreground">
                    {t.settings.reportProfile}: <strong className="font-semibold text-foreground">{apiHealth.report_profile}</strong>
                  </span>
                ) : null}
                {apiHealth.model_tier ? (
                  <StatusBadge label={`${t.settings.modelTier}: ${apiHealth.model_tier}`} tone="neutral" />
                ) : null}
                {apiHealth.testing_mode ? (
                  <StatusBadge label={t.settings.testingMode} tone="warning" />
                ) : null}
              </div>
            ) : null}
          </div>
        </SettingsSection>
      ) : null}
    </>
  )
}

export function LicensingPanel({
  appVersion,
  legal,
  projectSourceUrl,
}: {
  appVersion: string
  legal: InqtrixHealth['legal'] | undefined
  projectSourceUrl: string
}) {
  const { t } = useLocale()

  return (
    <SettingsSection>
      <LicensingInfoRow title={legal?.project ?? t.appName}>
        <div className="flex flex-wrap items-center gap-2 sm:justify-end">
          <StatusBadge
            label={legal?.license ?? t.authLock.licenseLabel}
            tone="brand"
          />
          <StatusBadge label={`v${appVersion}`} tone="neutral" />
        </div>
      </LicensingInfoRow>

      <div className="bg-surface/30 px-3 py-3">
        <div className="flex items-center gap-1.5 t-caption text-foreground">
          <Scale className="icon-sm" />
          {t.authLock.noticeTitle}
        </div>
        <ul className="mt-2 grid gap-x-5 gap-y-1.5 sm:grid-cols-2">
          {t.authLock.notices.map((notice) => (
            <li className="flex gap-2 t-meta-sm text-muted-foreground" key={notice}>
              <span className="mt-1.5 size-1 shrink-0 rounded-full bg-muted-foreground/60" />
              <span>{notice}</span>
            </li>
          ))}
        </ul>
      </div>

      <footer className="bg-surface/45 px-3 py-3">
        <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <h3 className="t-list text-foreground">{t.settings.legalResources}</h3>
            <p className="mt-0.5 max-w-xl t-meta text-muted-foreground">
              {t.settings.legalResourcesDescription}
            </p>
          </div>
          <div className="flex flex-wrap gap-2 sm:justify-end">
            <SettingsLink
              href={projectSourceUrl}
              icon={Github}
              label={t.authLock.repositoryLabel}
            />
            <SettingsLink
              href={t.authLock.documentationUrl}
              icon={BookOpen}
              label={t.authLock.documentationLabel}
            />
            <SettingsLink
              href={t.authLock.licenseUrl}
              icon={Scale}
              label={t.authLock.licenseLabel}
            />
          </div>
        </div>
      </footer>
    </SettingsSection>
  )
}

function LicensingInfoRow({
  children,
  title,
}: {
  children: ReactNode
  title: string
}) {
  return (
    <div className="grid gap-2 px-3 py-3 sm:grid-cols-[220px_minmax(0,1fr)] sm:gap-6">
      <div className="min-w-0">
        <h3 className="t-list text-foreground">{title}</h3>
      </div>
      <div className="flex min-w-0 items-start sm:justify-end">
        <div className="min-w-0 sm:text-right">{children}</div>
      </div>
    </div>
  )
}

function SettingsSegmented<T extends string>({
  ariaLabel,
  onChange,
  options,
  value,
  wrap,
}: {
  ariaLabel: string
  onChange: (value: T) => void
  options: Array<{ icon?: LucideIcon; label: string; value: T }>
  value: T
  wrap?: boolean
}) {
  return (
    <div
      aria-label={ariaLabel}
      className={cn(
        'inline-flex h-9 items-center rounded-md border border-border bg-card p-0.5 shadow-[0_1px_2px_var(--shadow-hairline)]',
        wrap && 'h-auto flex-wrap gap-0.5',
      )}
      role="group"
    >
      {options.map((option) => {
        const Icon = option.icon
        const isActive = value === option.value

        return (
          <button
            aria-pressed={isActive}
            className={cn(
              'inline-flex h-8 items-center gap-1.5 rounded-[6px] px-2.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-accent-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
              isActive && 'bg-brand-subtle text-brand hover:bg-brand-subtle hover:text-brand',
            )}
            key={option.value}
            onClick={() => onChange(option.value)}
            type="button"
          >
            {Icon ? <Icon className="icon-md shrink-0" /> : null}
            <span className={cn(wrap && 'max-w-[12rem] truncate')}>{option.label}</span>
          </button>
        )
      })}
    </div>
  )
}


function SettingsLink({
  href,
  icon: Icon,
  label,
}: {
  href: string
  icon: LucideIcon
  label: string
}) {
  return (
    <a
      className="inline-flex h-8 items-center gap-1.5 rounded-md border border-border bg-background px-2 text-xs font-medium text-muted-foreground transition hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      href={href}
      rel="noreferrer"
      target="_blank"
    >
      <Icon className="icon-sm" />
      <span>{label}</span>
      <ExternalLink className="icon-xs" />
    </a>
  )
}
