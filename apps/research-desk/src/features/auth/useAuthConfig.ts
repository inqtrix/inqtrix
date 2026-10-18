import { useCallback, useEffect, useState } from 'react'

import { fetchAuthConfig, type AuthConfig } from '@/api/inqtrixClient'

export type AuthConfigState = {
  /** `pending` until the first probe answers; `ready` once decided. */
  status: 'pending' | 'ready'
  /** The discovered config, or `null` when disabled or the probe failed. */
  config: AuthConfig | null
}

/**
 * Pre-login auth discovery. Probes `GET /api/auth/config` once when
 * *enabled*; the endpoint is unauthenticated and always mounted, so this
 * only supplies presentation hints (today: the SSO provider name for the
 * lock-screen button). A probe failure DEGRADES OPEN — the app keeps the
 * `auth_mode` it already reads from `/health`, so discovery can never lock
 * anyone out.
 */
export function useAuthConfig(enabled: boolean) {
  const [state, setState] = useState<AuthConfigState>({
    status: 'pending',
    config: null,
  })

  const refresh = useCallback(async () => {
    try {
      setState({ status: 'ready', config: await fetchAuthConfig() })
    } catch {
      setState({ status: 'ready', config: null })
    }
  }, [])

  useEffect(() => {
    if (!enabled) {
      setState({ status: 'ready', config: null })
      return
    }
    void refresh()
  }, [enabled, refresh])

  return { ...state, refresh }
}
