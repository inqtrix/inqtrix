import {
  AlertTriangle,
  ChevronRight,
  Database,
  Github,
  Info,
  KeyRound,
  Languages,
  Layers,
  LockKeyhole,
  PencilLine,
  Scale,
  SearchCheck,
  ShieldCheck,
  type LucideIcon,
} from '@/components/icons'
import { motion } from 'motion/react'
import { type FormEvent } from 'react'
import { BrandMark } from '@/components/BrandMark'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import { appMotion } from '@/motion/transitions'

type AuthLockScreenProps = {
  error: string | null
  isSubmitting: boolean
  /** `apikey` renders the token form (historical behaviour); `sso` swaps
   * it for the OIDC login redirect; `local` renders the credential
   * form (identifier + password) — same screen chrome throughout. */
  mode?: 'apikey' | 'sso' | 'local'
  /** SSO provider display name from `/api/auth/config`; labels the button
   * (e.g. "Okta") when set, else the generic localized SSO label. */
  providerName?: string | null
  onSsoLogin?: () => void
  onSubmit: (token: string) => void
  onTokenChange: (token: string) => void
  reduceMotion: boolean | null
  token: string
  /** Credential-mode (local) field state, owned by the parent. */
  identifier?: string
  password?: string
  onIdentifierChange?: (value: string) => void
  onPasswordChange?: (value: string) => void
  onCredentialSubmit?: () => void
}

export function AuthLockScreen({
  error,
  isSubmitting,
  mode = 'apikey',
  providerName,
  onSsoLogin,
  onSubmit,
  onTokenChange,
  reduceMotion,
  token,
  identifier = '',
  password = '',
  onIdentifierChange,
  onPasswordChange,
  onCredentialSubmit,
}: AuthLockScreenProps) {
  const { locale, setLocale, t } = useLocale()
  const isCredentialMode = mode === 'local'

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    onSubmit(token)
  }

  function handleCredentialSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    onCredentialSubmit?.()
  }

  return (
    <div className="inqtrix-auth-lock fixed inset-0 z-[100] flex min-h-svh items-start justify-center overflow-y-auto bg-background px-4 py-6 text-foreground sm:py-8">
      <div className="inqtrix-auth-fade pointer-events-none absolute inset-0" />
      <motion.section
        animate={{ opacity: 1, scale: 1, y: 0 }}
        aria-labelledby="auth-lock-title"
        // The lock screen covers the whole application, so it must also
        // CLAIM to: without aria-modal, assistive technology keeps
        // offering the covered shell as if it were still available. The
        // shell itself is made inert by the caller.
        aria-modal="true"
        className="relative w-full max-w-5xl overflow-hidden rounded-lg border border-border bg-card shadow-[0_24px_80px_var(--shadow-soft)]"
        initial={reduceMotion ? false : { opacity: 0, scale: 0.98, y: 10 }}
        role="dialog"
        transition={appMotion.panel}
      >
        <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-5 py-3.5">
          <div className="flex items-center gap-2.5">
            <BrandMark className="size-8 shrink-0" variant="matte" />
            <span className="text-sm font-semibold tracking-tight text-foreground">Inqtrix</span>
            <span className="inline-flex items-center gap-1 rounded-md border border-brand/20 bg-brand-subtle px-1.5 py-0.5 t-caption text-brand">
              <Github className="size-3" />
              {t.authLock.openSourceBadge}
            </span>
          </div>
          <div
            aria-label={t.common.language}
            className="inline-flex rounded-md border border-border bg-background/80 p-0.5 text-xs font-semibold shadow-[0_1px_2px_var(--shadow-hairline)] backdrop-blur"
            role="group"
          >
            <Languages className="mx-1.5 my-1 size-3.5 text-muted-foreground" />
            {(['de', 'en'] as const).map((nextLocale) => (
              <button
                aria-pressed={locale === nextLocale}
                className={cn(
                  'rounded px-2 py-1 text-muted-foreground transition hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  locale === nextLocale && 'bg-brand text-brand-foreground shadow-sm hover:text-brand-foreground',
                )}
                key={nextLocale}
                onClick={() => setLocale(nextLocale)}
                type="button"
              >
                {nextLocale.toUpperCase()}
              </button>
            ))}
          </div>
        </div>

        <div className="grid gap-0 lg:grid-cols-[minmax(0,1.1fr)_minmax(320px,0.9fr)]">
          <div className="border-b border-border bg-surface/55 p-5 sm:p-7 lg:border-b-0 lg:border-r">
            <p className="t-caption text-muted-foreground">{t.authLock.eyebrow}</p>
            <h1 className="t-display mt-2 max-w-lg text-foreground" id="auth-lock-title">
              {t.authLock.title}
            </h1>
            <p className="t-body mt-2.5 max-w-md text-muted-foreground">
              {t.authLock.description}
            </p>

            <div
              aria-hidden="true"
              className="mt-3.5 flex flex-wrap items-center gap-x-1.5 gap-y-1"
            >
              {t.authLock.flowSteps.map((step, index) => (
                <span className="inline-flex items-center gap-1.5" key={step}>
                  {index > 0 ? (
                    <ChevronRight className="size-3 text-muted-foreground/40" />
                  ) : null}
                  <span className="t-meta-sm text-muted-foreground">{step}</span>
                </span>
              ))}
            </div>

            <div className="mt-5 grid gap-0.5 border-t border-border pt-4">
              <LockBenefit icon={SearchCheck} label={t.authLock.benefitResearch} />
              <LockBenefit icon={Database} label={t.authLock.benefitKnowledge} />
              <LockBenefit icon={PencilLine} label={t.authLock.benefitEditor} />
              <LockBenefit icon={Layers} label={t.authLock.benefitPrompts} />
            </div>
          </div>

          <div className="flex flex-col justify-center p-5 sm:p-7">
            <div>
              <div className="flex items-center gap-2">
                <ShieldCheck className="size-4 shrink-0 text-brand" />
                <p className="t-section text-foreground">
                  {isCredentialMode
                    ? t.authLock.credentialTitle
                    : mode === 'sso'
                      ? t.authLock.ssoTitle
                      : t.authLock.accessTitle}
                </p>
              </div>
              <p className="t-meta mt-1.5 max-w-sm text-muted-foreground">
                {mode === 'sso'
                  ? t.authLock.ssoDescription
                  : mode === 'local'
                    ? t.authLock.credentialDescriptionLocal
                    : t.authLock.accessDescription}
              </p>
            </div>

            {isCredentialMode ? (
              <form className="mt-6 space-y-3" onSubmit={handleCredentialSubmit}>
                <div>
                  <label
                    className="block t-label text-foreground"
                    htmlFor="inqtrix-auth-identifier"
                  >
                    {t.authLock.emailLabel}
                  </label>
                  <Input
                    aria-invalid={Boolean(error)}
                    autoComplete="email"
                    autoFocus
                    className="mt-1.5 h-11"
                    id="inqtrix-auth-identifier"
                    onChange={(event) => onIdentifierChange?.(event.target.value)}
                    placeholder={t.authLock.emailPlaceholder}
                    type="email"
                    value={identifier}
                  />
                </div>
                <div>
                  <label
                    className="block t-label text-foreground"
                    htmlFor="inqtrix-auth-password"
                  >
                    {t.authLock.passwordLabel}
                  </label>
                  <Input
                    aria-invalid={Boolean(error)}
                    autoComplete="current-password"
                    className="mt-1.5 h-11"
                    id="inqtrix-auth-password"
                    onChange={(event) => onPasswordChange?.(event.target.value)}
                    placeholder={t.authLock.passwordPlaceholder}
                    type="password"
                    value={password}
                  />
                </div>
                {error && (
                  <p className="flex gap-1.5 text-xs font-medium leading-5 text-destructive" role="alert">
                    <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
                    <span>{error}</span>
                  </p>
                )}
                <Button
                  className="inqtrix-auth-action h-11 w-full gap-2"
                  disabled={isSubmitting}
                  type="submit"
                >
                  <LockKeyhole className="size-4" />
                  {isSubmitting ? t.authLock.signingIn : t.authLock.signIn}
                </Button>
              </form>
            ) : mode === 'sso' ? (
              <div className="mt-6 space-y-3">
                {error && (
                  <p className="flex gap-1.5 text-xs font-medium leading-5 text-destructive" role="alert">
                    <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
                    <span>{error}</span>
                  </p>
                )}
                <Button
                  className="inqtrix-auth-action h-11 w-full gap-2"
                  onClick={onSsoLogin}
                  type="button"
                >
                  <LockKeyhole className="size-4" />
                  {providerName || t.authLock.ssoButton}
                </Button>
              </div>
            ) : (
              <form className="mt-6 space-y-3" onSubmit={handleSubmit}>
                <label className="block t-label text-foreground" htmlFor="inqtrix-auth-token">
                  {t.authLock.tokenLabel}
                </label>
                <div className="relative">
                  <KeyRound className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
                  <input
                    aria-describedby="inqtrix-auth-help"
                    aria-invalid={Boolean(error)}
                    autoComplete="off"
                    autoFocus
                    className={cn(
                      'h-11 w-full rounded-md border border-input bg-card px-9 text-sm text-foreground shadow-[0_1px_2px_var(--shadow-hairline)] outline-none transition placeholder:text-muted-foreground focus-visible:ring-2 focus-visible:ring-ring',
                      error && 'border-destructive/50 focus-visible:ring-destructive/35',
                    )}
                    id="inqtrix-auth-token"
                    inputMode="text"
                    onChange={(event) => onTokenChange(event.target.value)}
                    placeholder={t.authLock.tokenPlaceholder}
                    spellCheck={false}
                    type="password"
                    value={token}
                  />
                </div>
                {error && (
                  <p className="flex gap-1.5 text-xs font-medium leading-5 text-destructive" role="alert">
                    <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
                    <span>{error}</span>
                  </p>
                )}
                <Button className="inqtrix-auth-action h-11 w-full gap-2" disabled={isSubmitting} type="submit">
                  <LockKeyhole className="size-4" />
                  {isSubmitting ? t.authLock.submitting : t.authLock.submit}
                </Button>
              </form>
            )}

            <div className="mt-6 space-y-2">
              <p className="flex items-start gap-2 t-meta-sm text-muted-foreground" id="inqtrix-auth-help">
                <LockKeyhole className="mt-0.5 size-3.5 shrink-0" />
                <span>{t.authLock.memoryOnly}</span>
              </p>
              <p className="flex items-start gap-2 t-meta-sm text-muted-foreground">
                <Info className="mt-0.5 size-3.5 shrink-0" />
                <span>{t.authLock.experimentalNote}</span>
              </p>
            </div>
          </div>
        </div>

        {/* Full width UNDER both columns — the notices are a footer of the
            card, not an appendix of the sign-in column; in that narrow column
            the two-column list has nowhere to breathe. Same list the
            licensing page renders (`t.authLock.notices`): one text, two
            surfaces. No source links here — this screen signs the user in,
            the sources belong in the settings. The closing line is an
            acknowledgement, not a consent: Apache-2.0 grants rights and is not
            agreed to by signing in, and this installation makes no claim about
            terms its operator may or may not have. */}
        <div className="border-t border-border px-5 py-4 sm:px-7">
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
          <p className="mt-3 t-meta-sm text-muted-foreground">
            {t.authLock.signInAcknowledgement}
          </p>
        </div>

      </motion.section>
    </div>
  )
}

function LockBenefit({
  icon: Icon,
  label,
}: {
  icon: LucideIcon
  label: string
}) {
  return (
    <div className="flex items-start gap-2.5 py-1.5">
      <span className="flex size-7 shrink-0 items-center justify-center rounded-md bg-brand-subtle text-brand">
        <Icon className="size-4" />
      </span>
      <span className="t-body text-foreground">{label}</span>
    </div>
  )
}
