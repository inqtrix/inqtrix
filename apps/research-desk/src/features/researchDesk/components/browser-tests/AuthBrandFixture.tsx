import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource-variable/inter/index.css'

import { AuthLockScreen } from '../AuthLockScreen'
import type { GatewayState } from '@/features/auth/authMode'
import { OwnerSetupScreen } from '@/features/onboarding/OwnerSetupScreen'
import { LocaleProvider } from '@/i18n/LocaleProvider'
import { ThemeProvider } from '@/theme/ThemeProvider'
import '@/styles/globals.css'

// Standalone frontend test entry; never imported by the production application.
const params = new URLSearchParams(window.location.search)
for (const key of ['theme', 'themePreset', 'contrastMode']) {
  const value = params.get(key)
  if (value) localStorage.setItem(`inqtrix.researchDesk.${key}`, value)
  else localStorage.removeItem(`inqtrix.researchDesk.${key}`)
}
const locale = params.get('locale') === 'de' ? 'de' : 'en'
const modes = ['sso', 'apikey', 'gateway'] as const
const mode = modes.find((candidate) => candidate === params.get('mode')) ?? 'local'
const gatewayState = mode === 'gateway'
  ? { state: (params.get('state') ?? 'noGatewaySignIn') as GatewayState, outcome: params.get('outcome') || null }
  : null

function Fixture() {
  const [identifier, setIdentifier] = useState('')
  const [password, setPassword] = useState('')
  const [token, setToken] = useState('')
  const [submitting, setSubmitting] = useState(params.has('busy'))
  if (params.get('view') === 'setup') return <OwnerSetupScreen onCreated={() => undefined} />
  return <AuthLockScreen
    error={params.has('error') ? (locale === 'de' ? 'Anmeldedaten wurden nicht akzeptiert.' : 'Those credentials were not accepted.') : null}
    gatewayState={gatewayState} identifier={identifier} isSubmitting={submitting} mode={mode}
    onGatewayAction={() => setSubmitting(true)}
    onCredentialSubmit={() => setSubmitting(true)} onIdentifierChange={setIdentifier}
    onPasswordChange={setPassword} onSsoLogin={() => setSubmitting(true)}
    onSubmit={() => setSubmitting(true)} onTokenChange={setToken}
    password={password} providerName="Example SSO" reduceMotion token={token}
  />
}

createRoot(document.getElementById('root')!).render(
  <ThemeProvider><LocaleProvider initialLocale={locale}><Fixture /></LocaleProvider></ThemeProvider>,
)
