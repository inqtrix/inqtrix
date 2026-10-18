import { isCookieSessionMode, type AuthMode } from '@/features/auth/authMode'
import type { InqtrixCapabilities } from '@/features/researchRuns/types'

/**
 * Whether the sharing surface is available: demo simulates it from seeded
 * data, otherwise the backend must advertise `features.sharing` AND there must
 * be a real cookie-session identity to share with or as (oidc/local,
 * authenticated). The none/apikey single-operator modes stay byte-identical
 * (no scoped identity). Defined once so the desk gate, the settings panel, and
 * the nav badge never drift.
 */
export function isSharingEnabled(params: {
  capabilities: InqtrixCapabilities | null | undefined
  authMode: AuthMode | undefined
  sessionStatus: string | undefined
  isDemo: boolean
}): boolean {
  const { authMode, capabilities, isDemo, sessionStatus } = params
  return (
    isDemo
    || (capabilities?.features.sharing === true
      && isCookieSessionMode(authMode)
      && sessionStatus === 'authenticated')
  )
}
