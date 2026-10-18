import { type FormEvent, type ReactNode, useState } from 'react'

import { AlertTriangle, Check, Circle, Languages, ShieldCheck } from '@/components/icons'
import { createOwner, hasHttpStatus } from '@/api/inqtrixClient'
import { BrandMark } from '@/components/BrandMark'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  type PasswordCheckId,
  type PasswordStrength,
  isPasswordAcceptable,
  passwordChecks,
  passwordStrength,
  passwordsMatch,
} from '@/features/auth/passwordPolicy'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'

const CHECK_LABEL: Record<
  PasswordCheckId,
  'checklistLength' | 'checklistLower' | 'checklistUpper' | 'checklistDigit'
> = {
  length: 'checklistLength',
  lower: 'checklistLower',
  upper: 'checklistUpper',
  digit: 'checklistDigit',
}

const STRENGTH: Record<
  Exclude<PasswordStrength, 'empty'>,
  { bars: number; key: 'strengthWeak' | 'strengthFair' | 'strengthStrong'; tone: string }
> = {
  weak: { bars: 1, key: 'strengthWeak', tone: 'bg-destructive' },
  fair: { bars: 2, key: 'strengthFair', tone: 'bg-warning' },
  strong: { bars: 3, key: 'strengthStrong', tone: 'bg-success' },
}

/**
 * First-run owner setup. Shown by the app-root gate when the server
 * reports `needs_owner` (local mode, no owner yet). Creating the owner
 * logs them straight in server-side, so the parent re-probes and the app
 * renders unlocked. Password rules mirror `passwordPolicy` (length is the
 * only gate — server-aligned, no silent over-restriction).
 */
export function OwnerSetupScreen({ onCreated }: { onCreated: () => void }) {
  const { locale, setLocale, t } = useLocale()
  const [email, setEmail] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  const checks = passwordChecks(password)
  const strength = passwordStrength(password)
  const strengthMeta = strength === 'empty' ? null : STRENGTH[strength]
  const canSubmit =
    email.includes('@') &&
    isPasswordAcceptable(password) &&
    passwordsMatch(password, confirm) &&
    !submitting

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    if (!isPasswordAcceptable(password)) {
      setError(t.onboarding.weakPassword)
      return
    }
    if (!passwordsMatch(password, confirm)) {
      setError(t.onboarding.mismatch)
      return
    }
    setSubmitting(true)
    try {
      await createOwner({
        email: email.trim(),
        password,
        displayName: displayName.trim() || undefined,
      })
      onCreated()
    } catch (caught) {
      setError(
        hasHttpStatus(caught, 409)
          ? t.onboarding.lockedAlready
          : t.onboarding.failed,
      )
      setSubmitting(false)
    }
  }

  return (
    <div className="fixed inset-0 z-[100] flex min-h-svh items-start justify-center overflow-y-auto bg-background px-4 py-8 text-foreground sm:py-12">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_50%_0%,color-mix(in_oklch,var(--brand)_18%,transparent),transparent_44%)]" />
      <section
        aria-labelledby="owner-setup-title"
        className="relative w-full max-w-md overflow-hidden rounded-lg border border-border bg-card shadow-[0_24px_80px_var(--shadow-soft)]"
        role="dialog"
      >
        <div className="flex items-center justify-between gap-3 border-b border-border bg-surface/70 px-5 py-4">
          <div className="flex items-center gap-2">
            <BrandMark className="size-6 shrink-0" />
            <div className="inline-flex h-7 items-center gap-1.5 rounded-md border border-brand/20 bg-brand-subtle px-2 text-xs font-semibold text-brand">
              <ShieldCheck className="size-3.5" />
              {t.onboarding.eyebrow}
            </div>
          </div>
          <div
            aria-label={t.common.language}
            className="inline-flex rounded-md border border-border bg-background/80 p-0.5 text-xs font-semibold"
            role="group"
          >
            <Languages className="mx-1.5 my-1 size-3.5 text-muted-foreground" />
            {(['de', 'en'] as const).map((next) => (
              <button
                aria-pressed={locale === next}
                className={cn(
                  'rounded px-2 py-1 text-muted-foreground transition hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring',
                  locale === next &&
                    'bg-brand text-brand-foreground shadow-sm hover:text-brand-foreground',
                )}
                key={next}
                onClick={() => setLocale(next)}
                type="button"
              >
                {next.toUpperCase()}
              </button>
            ))}
          </div>
        </div>

        <div className="p-5 sm:p-6">
          <h1 className="t-display text-foreground" id="owner-setup-title">
            {t.onboarding.title}
          </h1>
          <p className="mt-2 text-sm leading-6 text-muted-foreground">
            {t.onboarding.description}
          </p>

          <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
            <Field htmlFor="owner-email" label={t.onboarding.emailLabel}>
              <Input
                autoComplete="email"
                autoFocus
                id="owner-email"
                onChange={(event) => setEmail(event.target.value)}
                placeholder={t.onboarding.emailPlaceholder}
                type="email"
                value={email}
              />
            </Field>
            <Field
              hint={t.onboarding.displayNameHint}
              htmlFor="owner-display-name"
              label={t.onboarding.displayNameLabel}
            >
              <Input
                autoComplete="name"
                id="owner-display-name"
                onChange={(event) => setDisplayName(event.target.value)}
                placeholder={t.onboarding.displayNamePlaceholder}
                value={displayName}
              />
            </Field>
            <Field htmlFor="owner-password" label={t.onboarding.passwordLabel}>
              <Input
                autoComplete="new-password"
                id="owner-password"
                onChange={(event) => setPassword(event.target.value)}
                placeholder={t.onboarding.passwordPlaceholder}
                type="password"
                value={password}
              />
              {strengthMeta ? (
                <div className="mt-2 flex items-center gap-2">
                  <div className="flex flex-1 gap-1">
                    {[0, 1, 2].map((index) => (
                      <span
                        className={cn(
                          'h-1 flex-1 rounded-full',
                          index < strengthMeta.bars
                            ? strengthMeta.tone
                            : 'bg-border',
                        )}
                        key={index}
                      />
                    ))}
                  </div>
                  <span className="t-hint text-muted-foreground">
                    {t.onboarding[strengthMeta.key]}
                  </span>
                </div>
              ) : null}
              <ul className="mt-2 grid gap-1">
                {checks.map((check) => (
                  <li
                    className={cn(
                      't-hint flex items-center gap-1.5',
                      check.met ? 'text-success' : 'text-muted-foreground',
                    )}
                    key={check.id}
                  >
                    {check.met ? (
                      <Check className="size-3" />
                    ) : (
                      <Circle className="size-3" />
                    )}
                    {t.onboarding[CHECK_LABEL[check.id]]}
                  </li>
                ))}
              </ul>
            </Field>
            <Field htmlFor="owner-confirm" label={t.onboarding.confirmLabel}>
              <Input
                autoComplete="new-password"
                id="owner-confirm"
                onChange={(event) => setConfirm(event.target.value)}
                placeholder={t.onboarding.confirmPlaceholder}
                type="password"
                value={confirm}
              />
            </Field>

            {error ? (
              <p className="flex gap-1.5 text-xs font-medium leading-5 text-destructive" role="alert">
                <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
                <span>{error}</span>
              </p>
            ) : null}

            <Button
              className="h-10 w-full gap-2 bg-brand text-brand-foreground hover:bg-brand/90"
              disabled={!canSubmit}
              type="submit"
            >
              <ShieldCheck className="size-4" />
              {submitting ? t.onboarding.submitting : t.onboarding.submit}
            </Button>
          </form>
        </div>
      </section>
    </div>
  )
}

function Field({
  children,
  hint,
  htmlFor,
  label,
}: {
  children: ReactNode
  hint?: string
  htmlFor: string
  label: string
}) {
  return (
    <div>
      <label
        className="t-label mb-1.5 block text-foreground"
        htmlFor={htmlFor}
      >
        {label}
      </label>
      {children}
      {hint ? <p className="t-hint mt-1 text-muted-foreground">{hint}</p> : null}
    </div>
  )
}
