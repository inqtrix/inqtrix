import { useEffect, useState } from 'react'

import { CircleUserRound } from '@/components/icons'
import { InitialsAvatar } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import { type AuthMode, isCookieSessionMode } from '@/features/auth/authMode'
import type { AuthSessionState } from '@/features/auth/useAuthSession'
import { DEMO_OWNER } from '@/features/sharing/demoShares'

type ProfileAvatarProps = {
  /** Resolved auth mode; the avatar exists for cookie-session modes. */
  authMode: AuthMode
  /** Demo mode shows a seeded identity so the multi-user surface is visible. */
  isDemo?: boolean
  session: AuthSessionState
  onLogin: () => void
  onLogout: () => void
  /** Open the settings view focused on the security section. */
  onOpenSecuritySettings: () => void
}

/**
 * Round identity element at the bottom of the app rail.
 *
 * Renders nothing outside oidc mode and while the session state is
 * still `unknown` (no sign-in flash on reload); anonymous shows a
 * quiet sign-in affordance, authenticated shows the initials with a
 * small account menu (settings deep-link, sign-out).
 */
export function ProfileAvatar({
  authMode,
  isDemo = false,
  session,
  onLogin,
  onLogout,
  onOpenSecuritySettings,
}: ProfileAvatarProps) {
  const { t } = useLocale()
  const [menuOpen, setMenuOpen] = useState(false)
  const effectiveSessionStatus = isDemo ? 'authenticated' : session.status

  useEffect(() => {
    if (effectiveSessionStatus !== 'authenticated') {
      setMenuOpen(false)
    }
  }, [effectiveSessionStatus])

  if (!isDemo && (!isCookieSessionMode(authMode) || session.status === 'unknown')) {
    return null
  }
  // Demo shows the seeded workspace owner as a signed-in identity.
  const effectiveSession: AuthSessionState = isDemo
    ? {
        projectNamespace: null,
        status: 'authenticated',
        user: {
          displayName: DEMO_OWNER.displayName,
          email: DEMO_OWNER.email,
          id: DEMO_OWNER.userId,
          role: 'admin',
        },
      }
    : session

  if (effectiveSession.status === 'anonymous') {
    return (
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            aria-label={t.profile.signIn}
            className="size-9 rounded-full text-muted-foreground"
            onClick={onLogin}
            size="icon"
            type="button"
            variant="ghost"
          >
            <CircleUserRound className="size-4" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="right">{t.profile.signIn}</TooltipContent>
      </Tooltip>
    )
  }

  return (
    <DropdownMenu onOpenChange={setMenuOpen} open={menuOpen}>
      <Tooltip>
        <TooltipTrigger asChild>
          <DropdownMenuTrigger asChild>
            <Button
              aria-label={t.profile.signedInAs}
              className="size-9 rounded-full"
              size="icon"
              type="button"
              variant="ghost"
            >
              <InitialsAvatar
                displayName={effectiveSession.user?.displayName ?? null}
                email={effectiveSession.user?.email ?? null}
              />
            </Button>
          </DropdownMenuTrigger>
        </TooltipTrigger>
        {menuOpen ? null : (
          <TooltipContent side="right">
            <div className="flex flex-col gap-0.5">
              <span>{effectiveSession.user?.displayName ?? t.profile.signedInAs}</span>
              {effectiveSession.user?.email ? (
                <span className="text-muted-foreground">{effectiveSession.user.email}</span>
              ) : null}
            </div>
          </TooltipContent>
        )}
      </Tooltip>
      <DropdownMenuContent align="start" side="right">
        <div className="px-2 py-1.5">
          <p className="t-list text-foreground">
            {effectiveSession.user?.displayName ?? t.profile.signedInAs}
          </p>
          {effectiveSession.user?.email ? (
            <p className="t-meta text-muted-foreground">{effectiveSession.user.email}</p>
          ) : null}
        </div>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={onOpenSecuritySettings}>
          {t.profile.settings}
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={onLogout}>
          {t.profile.signOut}
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
