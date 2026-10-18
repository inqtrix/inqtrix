import { useAuthConfig } from '@/features/auth/useAuthConfig'
import { OwnerSetupScreen } from '@/features/onboarding/OwnerSetupScreen'
import { ResearchDesk } from '@/features/researchDesk/ResearchDesk'
import { LoadingWorkspace } from '@/features/researchDesk/components/LoadingWorkspace'
import { GuestEditorPage } from '@/features/editor/GuestEditorPage'
import { guestLinkTokenFromPath } from './guestLink'

export function App() {
  const guestToken = guestLinkToken()
  if (guestToken !== null) return <GuestEditorPage token={guestToken} />
  return <AuthenticatedApp />
}

function AuthenticatedApp() {
  // One pre-login discovery probe (GET /api/auth/config) at the root. It
  // both gates the first-run owner setup (registration.needs_owner, local
  // mode only) and is threaded into the desk for the SSO provider name and
  // the PAT availability flag — so there is no second /api/setup/status
  // probe and no duplicate fetch inside the desk.
  const auth = useAuthConfig(true)
  // Hold the real surfaces until the first probe answers so neither the setup
  // screen nor the desk flashes; a probe failure degrades open (config is
  // null, the desk renders and runs its own auth gate). Showing a skeleton
  // instead of nothing keeps the wait from reading as a broken page — the
  // probe is short locally but not on a remote connection.
  if (auth.status === 'pending') return <LoadingWorkspace />
  if (auth.config?.registration.needs_owner) {
    return <OwnerSetupScreen onCreated={() => void auth.refresh()} />
  }
  return <ResearchDesk authConfig={auth.config} />
}

function guestLinkToken(): string | null {
  if (typeof window === 'undefined') return null
  return guestLinkTokenFromPath(window.location.pathname)
}
