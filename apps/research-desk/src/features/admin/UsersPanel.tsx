import { type FormEvent, useEffect, useMemo, useState } from 'react'

import { AlertTriangle, Check, Copy, KeyRound, Plus, Trash2 } from '@/components/icons'
import {
  type AdminErasurePreview,
  type AdminUser,
  eraseAdminUser,
  hasHttpStatus,
  previewAdminUserErasure,
} from '@/api/inqtrixClient'
import { messageFromError } from '@/api/requestErrorText'
import { InitialsAvatar } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import {
  Table,
  TableBody,
  TableCell,
  TableEmpty,
  TableHead,
  TableHeader,
  TableRow,
  TableSkeleton,
} from '@/components/ui/table'
import type { AuthMode } from '@/features/auth/authMode'
import {
  MIN_PASSWORD_LENGTH,
  isPasswordAcceptable,
} from '@/features/auth/passwordPolicy'
import { StatusBadge } from '@/features/settings/parts'
import { useLocale } from '@/i18n/LocaleProvider'
import { countLabel } from '@/i18n/plural'
import type { AccountErasureProgress } from './accountErasure'
import { canDisable, canSetRole, isErasing, isSelf, sortUsers } from './adminModel'
import { useAccountErasures } from './useAccountErasures'
import type { useAdminUsers } from './useAdminUsers'
import { copyTextToClipboard } from '@/lib/clipboard'

function formatDate(seconds: number | null, locale: string): string | null {
  if (seconds == null) return null
  return new Date(seconds * 1000).toLocaleDateString(locale, {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
  })
}

/** A cryptographically-random, policy-passing initial password suggestion. */
function suggestPassword(): string {
  const alphabet =
    'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_'
  const bytes = new Uint32Array(20)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, (value) => alphabet[value % alphabet.length]).join('')
}

export function UsersPanel({
  admin,
  mode,
  sessionUserId,
}: {
  admin: ReturnType<typeof useAdminUsers>
  mode: AuthMode
  sessionUserId: string | null
}) {
  const { locale, t } = useLocale()
  const { createUser, resetPassword, setDisabled, setRole, state } = admin
  const [createOpen, setCreateOpen] = useState(false)
  const [resetTarget, setResetTarget] = useState<AdminUser | null>(null)
  const [eraseTarget, setEraseTarget] = useState<AdminUser | null>(null)
  const [retryError, setRetryError] = useState<string | null>(null)
  const rows = useMemo(() => sortUsers(state.users), [state.users])
  // Rows in their list order, so the erasures followed first are the ones
  // the administrator sees first.
  const erasures = useAccountErasures({
    enabled: state.available && !state.demo,
    onErased: () => void admin.reload(),
    users: rows,
  })
  const retryErasure = (userId: string) => {
    setRetryError(null)
    erasures.retry(userId).catch((error: unknown) => {
      setRetryError(messageFromError(error))
    })
  }

  return (
    <div className="flex min-w-0 flex-col gap-4">
      <div className="flex items-center justify-between gap-3">
        <p className="t-meta max-w-xl text-muted-foreground">
          {t.adminUsers.description}
        </p>
        {mode === 'local' || state.demo ? (
          <Button
            className="shrink-0 gap-1.5 bg-brand text-brand-foreground hover:bg-brand/90"
            onClick={() => setCreateOpen(true)}
            size="sm"
          >
            <Plus className="size-4" />
            {t.adminUsers.createButton}
          </Button>
        ) : null}
      </div>

      {state.mutationError ? (
        <p className="flex gap-1.5 text-xs font-medium text-destructive" role="alert">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span>{t.adminUsers.mutationFailed}</span>
        </p>
      ) : null}

      {/* A refresh that failed says so. Without this the panel would keep
          the rows it had and look perfectly current — the quieter half of
          the lie an empty state tells when it hides a failed load. */}
      {state.status === 'error' ? (
        <p className="flex gap-1.5 text-xs font-medium text-destructive" role="alert">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span>{t.adminUsers.loadFailed}</span>
        </p>
      ) : null}

      {retryError ? (
        <p className="flex gap-1.5 text-xs font-medium text-destructive" role="alert">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span>{retryError}</span>
        </p>
      ) : null}

      {erasures.observationStopped ? (
        <p className="t-meta text-muted-foreground" role="status">
          {t.common.observationStopped(null)}
        </p>
      ) : null}

      <Table variant="fluid">
        <TableHeader>
          <TableRow>
            <TableHead>{t.adminUsers.colUser}</TableHead>
            <TableHead>{t.adminUsers.colRole}</TableHead>
            <TableHead>{t.adminUsers.colStatus}</TableHead>
            <TableHead>{t.adminUsers.colLastLogin}</TableHead>
            <TableHead className="text-right">{t.adminUsers.colActions}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {/* The skeleton belongs to the FIRST load only. Every mutation
              refetches the list, and swapping a filled table for a skeleton
              collapses the page height -- the scroll position falls to the
              top and the row just toggled is off screen. Rows stay put while
              a refresh runs; they are replaced when the new list arrives. */}
          {state.status === 'loading' && rows.length === 0 ? (
            <TableSkeleton colSpan={5} />
          ) : rows.length === 0 ? (
            <TableEmpty colSpan={5} title={t.adminUsers.empty} />
          ) : (
            rows.map((user) => {
              const self = isSelf(user, sessionUserId)
              // An account being erased is cut off already; nothing about it
              // can change any more, so every control on its row is off.
              const erasing = isErasing(user)
              const roleGuardUser = canSetRole(rows, user, sessionUserId, 'user')
              const disableGuard = canDisable(rows, user, sessionUserId)
              const lockReason = (
                guard: ReturnType<typeof canDisable>,
              ): string | undefined =>
                guard.allowed
                  ? undefined
                  : guard.reason === 'self'
                    ? t.adminUsers.lockedSelf
                    : t.adminUsers.lockedLastAdmin
              return (
                <TableRow key={user.id}>
                  <TableCell>
                    <div className="flex items-center gap-2.5">
                      <InitialsAvatar
                        displayName={user.display_name}
                        email={user.email}
                      />
                      <div className="min-w-0">
                        <div className="flex items-center gap-1.5">
                          <span className="t-list truncate text-foreground">
                            {user.display_name ?? user.email}
                          </span>
                          {self ? (
                            <StatusBadge
                              density="table"
                              label={t.adminUsers.you}
                              tone="brand"
                            />
                          ) : null}
                        </div>
                        <span className="t-meta-sm block truncate text-muted-foreground">
                          {user.email}
                        </span>
                      </div>
                    </div>
                  </TableCell>
                  <TableCell>
                    <Select
                      disabled={
                        erasing ||
                        (!roleGuardUser.allowed && user.instance_role === 'admin')
                      }
                      onValueChange={(value) =>
                        void setRole(user.id, value as 'admin' | 'user')
                      }
                      value={user.instance_role}
                    >
                      <SelectTrigger
                        className="w-32 border-border bg-background shadow-none disabled:bg-surface/60 disabled:text-muted-foreground disabled:opacity-100"
                        density="table"
                        title={lockReason(roleGuardUser)}
                      >
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="admin">
                          {t.adminUsers.roleAdmin}
                        </SelectItem>
                        <SelectItem value="user">
                          {t.adminUsers.roleUser}
                        </SelectItem>
                      </SelectContent>
                    </Select>
                  </TableCell>
                  <TableCell>
                    {erasing ? (
                      <ErasureStatus
                        onRetry={() => retryErasure(user.id)}
                        progress={erasures.progress[user.id]}
                      />
                    ) : (
                      <StatusBadge
                        density="table"
                        label={
                          user.disabled
                            ? t.adminUsers.statusDisabled
                            : t.adminUsers.statusActive
                        }
                        tone={user.disabled ? 'destructive' : 'success'}
                      />
                    )}
                  </TableCell>
                  <TableCell>
                    <span className="t-mono tabular-nums text-muted-foreground">
                      {formatDate(user.last_login_at, locale) ??
                        t.adminUsers.neverLoggedIn}
                    </span>
                  </TableCell>
                  <TableCell className="text-right">
                    {/* The Status column already shows active/disabled; the
                        switch's aria-label carries the action verb, so no
                        (easily-inverted) inline text label is needed. */}
                    <div className="flex items-center justify-end gap-1">
                      {mode === 'local' || state.demo ? (
                        <Button
                          aria-label={t.adminUsers.resetButton}
                          className="size-7 text-muted-foreground"
                          disabled={erasing}
                          onClick={() => setResetTarget(user)}
                          size="icon"
                          title={t.adminUsers.resetButton}
                          variant="ghost"
                        >
                          <KeyRound className="icon-sm" />
                        </Button>
                      ) : null}
                      <Button
                        aria-label={t.adminUsers.eraseButton}
                        className="size-7 text-muted-foreground hover:text-destructive"
                        disabled={self || erasing}
                        onClick={() => setEraseTarget(user)}
                        size="icon"
                        title={t.adminUsers.eraseButton}
                        variant="ghost"
                      >
                        <Trash2 className="icon-sm" />
                      </Button>
                      <Switch
                        aria-label={
                          user.disabled
                            ? t.adminUsers.enable
                            : t.adminUsers.disable
                        }
                        checked={!user.disabled && !erasing}
                        disabled={
                          erasing || (!user.disabled && !disableGuard.allowed)
                        }
                        onCheckedChange={(checked) =>
                          void setDisabled(user.id, !checked)
                        }
                        density="table"
                        title={
                          user.disabled ? undefined : lockReason(disableGuard)
                        }
                      />
                    </div>
                  </TableCell>
                </TableRow>
              )
            })
          )}
        </TableBody>
      </Table>

      {eraseTarget ? (
        <EraseUserDialog
          onClose={() => setEraseTarget(null)}
          onErased={() => {
            setEraseTarget(null)
            void admin.reload()
          }}
          user={eraseTarget}
        />
      ) : null}

      {createOpen ? (
        <CreateUserDialog
          createUser={createUser}
          onClose={() => setCreateOpen(false)}
        />
      ) : null}

      {resetTarget ? (
        <ResetPasswordDialog
          onClose={() => setResetTarget(null)}
          resetPassword={resetPassword}
          user={resetTarget}
        />
      ) : null}
    </div>
  )
}

function ResetPasswordDialog({
  onClose,
  resetPassword,
  user,
}: {
  onClose: () => void
  resetPassword: ReturnType<typeof useAdminUsers>['resetPassword']
  user: AdminUser
}) {
  const { t } = useLocale()
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [revealed, setRevealed] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await resetPassword(user.id, password)
      setRevealed(password)
    } catch {
      setError(t.adminUsers.resetFailed)
      setSubmitting(false)
    }
  }

  if (revealed) {
    return (
      <Dialog
        closeLabel={t.adminUsers.done}
        description={user.email ?? undefined}
        dismissable={false}
        footer={
          <Button onClick={onClose} size="sm">
            {t.adminUsers.done}
          </Button>
        }
        onClose={onClose}
        open
        title={t.adminUsers.resetCreatedTitle}
      >
        <div className="rounded-md border border-warning/25 bg-warning/10 p-3">
          <p className="t-label text-foreground">
            {t.adminUsers.resetCreatedHint}
          </p>
          <div className="mt-2 flex items-center gap-2">
            <code className="t-mono flex-1 select-all break-all rounded bg-background px-2 py-1.5 text-foreground">
              {revealed}
            </code>
            <Button
              className="shrink-0 gap-1.5"
              onClick={() => {
                void copyTextToClipboard(revealed)
                setCopied(true)
              }}
              size="sm"
              variant="outline"
            >
              {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
              {copied ? t.adminUsers.copied : t.adminUsers.copyPassword}
            </Button>
          </div>
        </div>
      </Dialog>
    )
  }

  const canSubmit = isPasswordAcceptable(password) && !submitting
  return (
    <Dialog
      closeLabel={t.adminTokens.cancel}
      description={t.adminUsers.resetDescription}
      onClose={onClose}
      open
      title={t.adminUsers.resetTitle(user.display_name ?? user.email ?? '')}
    >
      <form className="space-y-4" onSubmit={handleSubmit}>
        <div>
          <label className="t-label mb-1.5 block text-foreground" htmlFor="reset-password">
            {t.adminUsers.fieldPassword}
          </label>
          <div className="flex items-center gap-2">
            <Input
              autoComplete="new-password"
              autoFocus
              id="reset-password"
              onChange={(event) => setPassword(event.target.value)}
              type="text"
              value={password}
            />
            <Button
              className="shrink-0"
              onClick={() => setPassword(suggestPassword())}
              size="sm"
              type="button"
              variant="outline"
            >
              {t.adminUsers.generatePassword}
            </Button>
          </div>
          {password && !isPasswordAcceptable(password) ? (
            <p className="t-hint mt-1 text-warning">{`min. ${MIN_PASSWORD_LENGTH}`}</p>
          ) : null}
        </div>
        {error ? (
          <p className="flex gap-1.5 text-xs font-medium text-destructive" role="alert">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
            <span>{error}</span>
          </p>
        ) : null}
        <div className="flex items-center justify-end gap-2 pt-1">
          <Button onClick={onClose} size="sm" type="button" variant="ghost">
            {t.adminTokens.cancel}
          </Button>
          <Button
            className="bg-brand text-brand-foreground hover:bg-brand/90"
            disabled={!canSubmit}
            size="sm"
            type="submit"
          >
            {submitting ? t.adminUsers.resetting : t.adminUsers.resetSubmit}
          </Button>
        </div>
      </form>
    </Dialog>
  )
}

function CreateUserDialog({
  createUser,
  onClose,
}: {
  createUser: ReturnType<typeof useAdminUsers>['createUser']
  onClose: () => void
}) {
  const { t } = useLocale()
  const [email, setEmail] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState<'admin' | 'user'>('user')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [created, setCreated] = useState<{ email: string; password: string } | null>(null)
  const [copied, setCopied] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await createUser({
        email: email.trim(),
        password,
        instanceRole: role,
        displayName: displayName.trim() || undefined,
      })
      setCreated({ email: email.trim(), password })
    } catch (caught) {
      setError(
        hasHttpStatus(caught, 409)
          ? t.adminUsers.duplicateEmail
          : t.adminUsers.createFailed,
      )
      setSubmitting(false)
    }
  }

  if (created) {
    return (
      <Dialog
        closeLabel={t.adminUsers.done}
        dismissable={false}
        footer={
          <Button onClick={onClose} size="sm">
            {t.adminUsers.done}
          </Button>
        }
        onClose={onClose}
        open
        title={t.adminUsers.createdTitle}
      >
        <p className="t-meta text-muted-foreground">{created.email}</p>
        <div className="mt-3 rounded-md border border-warning/25 bg-warning/10 p-3">
          <p className="t-label text-foreground">{t.adminUsers.createdHint}</p>
          <div className="mt-2 flex items-center gap-2">
            <code className="t-mono flex-1 select-all break-all rounded bg-background px-2 py-1.5 text-foreground">
              {created.password}
            </code>
            <Button
              className="shrink-0 gap-1.5"
              onClick={() => {
                void copyTextToClipboard(created.password)
                setCopied(true)
              }}
              size="sm"
              variant="outline"
            >
              {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
              {copied ? t.adminUsers.copied : t.adminUsers.copyPassword}
            </Button>
          </div>
        </div>
      </Dialog>
    )
  }

  const canSubmit =
    email.includes('@') && isPasswordAcceptable(password) && !submitting

  return (
    <Dialog
      closeLabel={t.adminTokens.cancel}
      description={t.adminUsers.createDescription}
      onClose={onClose}
      open
      title={t.adminUsers.createTitle}
    >
      <form className="space-y-4" onSubmit={handleSubmit}>
        <div>
          <label className="t-label mb-1.5 block text-foreground" htmlFor="admin-create-email">
            {t.adminUsers.fieldEmail}
          </label>
          <Input
            autoFocus
            id="admin-create-email"
            onChange={(event) => setEmail(event.target.value)}
            type="email"
            value={email}
          />
        </div>
        <div>
          <label className="t-label mb-1.5 block text-foreground" htmlFor="admin-create-name">
            {t.adminUsers.fieldDisplayName}
          </label>
          <Input
            id="admin-create-name"
            onChange={(event) => setDisplayName(event.target.value)}
            value={displayName}
          />
          <p className="t-hint mt-1 text-muted-foreground">
            {t.adminUsers.fieldDisplayNameHint}
          </p>
        </div>
        <div>
          <label className="t-label mb-1.5 block text-foreground" htmlFor="admin-create-password">
            {t.adminUsers.fieldPassword}
          </label>
          <div className="flex items-center gap-2">
            <Input
              autoComplete="new-password"
              id="admin-create-password"
              onChange={(event) => setPassword(event.target.value)}
              type="text"
              value={password}
            />
            <Button
              className="shrink-0"
              onClick={() => setPassword(suggestPassword())}
              size="sm"
              type="button"
              variant="outline"
            >
              {t.adminUsers.generatePassword}
            </Button>
          </div>
          {password && !isPasswordAcceptable(password) ? (
            <p className="t-hint mt-1 text-warning">
              {`min. ${MIN_PASSWORD_LENGTH}`}
            </p>
          ) : null}
        </div>
        <div>
          <label className="t-label mb-1.5 block text-foreground" htmlFor="admin-create-role">
            {t.adminUsers.fieldRole}
          </label>
          <Select onValueChange={(value) => setRole(value as 'admin' | 'user')} value={role}>
            <SelectTrigger className="w-full" id="admin-create-role">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="user">{t.adminUsers.roleUser}</SelectItem>
              <SelectItem value="admin">{t.adminUsers.roleAdmin}</SelectItem>
            </SelectContent>
          </Select>
        </div>
        {error ? (
          <p className="flex gap-1.5 text-xs font-medium text-destructive" role="alert">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
            <span>{error}</span>
          </p>
        ) : null}
        <div className="flex items-center justify-end gap-2 pt-1">
          <Button onClick={onClose} size="sm" type="button" variant="ghost">
            {t.adminTokens.cancel}
          </Button>
          <Button
            className="bg-brand text-brand-foreground hover:bg-brand/90"
            disabled={!canSubmit}
            size="sm"
            type="submit"
          >
            {submitting ? t.adminUsers.creating : t.adminUsers.createSubmit}
          </Button>
        </div>
      </form>
    </Dialog>
  )
}

/**
 * The confirmation for the one irreversible action in user management.
 *
 * It counts BEFORE it asks. An administrator deleting an account cannot
 * see what hangs off it — documents, tokens, shares, and the replies
 * colleagues wrote inside those documents — and a dialog that only says
 * "are you sure?" asks them to confirm something nobody can see. The
 * preview walks the same plan the erasure executes, so the number here
 * and the work there cannot drift apart.
 *
 * Two refusals are not styling: a preview that failed leaves the button
 * disabled (deleting on unknown scope is exactly the mistake this dialog
 * exists to prevent), and a blocked account shows the server's reasons
 * instead of a generic error, because each one names something a person
 * has to settle first.
 */
function EraseUserDialog({
  onClose,
  onErased,
  user,
}: {
  onClose: () => void
  onErased: () => void
  user: AdminUser
}) {
  const { t } = useLocale()
  const [preview, setPreview] = useState<AdminErasurePreview | null>(null)
  const [previewFailed, setPreviewFailed] = useState(false)
  const [typed, setTyped] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let current = true
    setPreview(null)
    setPreviewFailed(false)
    previewAdminUserErasure(user.id)
      .then((result) => { if (current) setPreview(result) })
      .catch(() => { if (current) setPreviewFailed(true) })
    return () => { current = false }
  }, [user.id])

  const blocked = (preview?.blockers.length ?? 0) > 0
  // A mirrored identity can carry no address; the id is then the only
  // thing that identifies it, and confirming by id is still a deliberate
  // act rather than a reflex click.
  const confirmPhrase = (user.email ?? user.id).trim()
  const confirmed =
    typed.trim().toLowerCase() === confirmPhrase.toLowerCase()
  const canSubmit = Boolean(preview) && !blocked && confirmed && !busy

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!canSubmit) return
    setBusy(true)
    setError(null)
    try {
      await eraseAdminUser(user.id)
      onErased()
    } catch (cause) {
      setError(
        cause instanceof Error && cause.message
          ? cause.message
          : t.adminUsers.eraseFailed,
      )
      setBusy(false)
    }
  }

  return (
    <Dialog
      // Mid-erasure the dialog must not be dismissable: the request is
      // already in flight and closing would hide its outcome.
      dismissable={!busy}
      onClose={() => { if (!busy) onClose() }}
      open
      title={t.adminUsers.eraseTitle(
        user.display_name || user.email || user.id,
      )}
    >
      <form className="space-y-3" onSubmit={submit}>
        <p className="t-body text-muted-foreground">{t.adminUsers.eraseIntro}</p>

        {preview === null && !previewFailed ? (
          <p className="t-meta text-muted-foreground" role="status">
            {t.adminUsers.eraseCounting}
          </p>
        ) : null}

        {previewFailed ? (
          <p className="flex gap-1.5 t-meta font-medium text-destructive" role="alert">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
            <span>{t.adminUsers.eraseCountFailed}</span>
          </p>
        ) : null}

        {preview && !blocked ? <ErasePreviewSummary preview={preview} /> : null}

        {blocked ? (
          <div className="rounded-md border border-warning/25 bg-warning/10 p-3" role="alert">
            <p className="t-meta font-medium text-foreground">{t.adminUsers.eraseBlocked}</p>
            <ul className="mt-1 list-disc space-y-1 pl-4">
              {preview?.blockers.map((reason) => (
                <li className="t-meta text-muted-foreground" key={reason}>{reason}</li>
              ))}
            </ul>
          </div>
        ) : null}

        {preview && !blocked ? (
          <label className="block space-y-1">
            <span className="t-meta text-muted-foreground">
              {t.adminUsers.eraseConfirmLabel(confirmPhrase)}
            </span>
            <Input
              autoComplete="off"
              onChange={(event) => setTyped(event.target.value)}
              spellCheck={false}
              value={typed}
            />
          </label>
        ) : null}

        {error ? (
          <p className="flex gap-1.5 t-meta font-medium text-destructive" role="alert">
            <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
            <span>{error}</span>
          </p>
        ) : null}

        <div className="flex justify-end gap-2 pt-1">
          <Button
            disabled={busy}
            onClick={onClose}
            size="sm"
            type="button"
            variant="ghost"
          >
            {t.adminTokens.cancel}
          </Button>
          <Button
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            disabled={!canSubmit}
            size="sm"
            type="submit"
          >
            {busy ? t.adminUsers.erasing : t.adminUsers.eraseSubmit}
          </Button>
        </div>
      </form>
    </Dialog>
  )
}

/**
 * What the erasure reaches, in the confirmation: the rows it deletes and
 * clears, what goes through its own deletion together with the data it holds
 * outside the database, what stays in other people's documents under the
 * deleted-account placeholder, and the replies other people lose.
 */
export function ErasePreviewSummary({ preview }: { preview: AdminErasurePreview }) {
  const { t } = useLocale()
  return (
    <div className="rounded-md border border-warning/25 bg-warning/10 p-3 space-y-1.5">
      <p className="t-meta text-foreground">
        {t.adminUsers.eraseSummary
          .replace('{deleted}', countLabel(preview.deleted_rows, t.adminUsers.eraseDeletedRowsOne, t.adminUsers.eraseDeletedRows))
          .replace('{cleared}', countLabel(preview.cleared_rows, t.adminUsers.eraseClearedRowsOne, t.adminUsers.eraseClearedRows))}
      </p>
      {preview.delegated_rows > 0 ? (
        <p className="t-meta text-foreground" data-testid="erase-delegated">
          {countLabel(
            preview.delegated_rows,
            t.adminUsers.eraseDelegatedOne,
            t.adminUsers.eraseDelegated,
          )}
        </p>
      ) : null}
      {preview.reassigned_rows > 0 ? (
        <p className="t-meta text-foreground" data-testid="erase-reassigned">
          {countLabel(
            preview.reassigned_rows,
            t.adminUsers.eraseReassignedOne,
            t.adminUsers.eraseReassigned,
          )}
        </p>
      ) : null}
      {preview.foreign_rows > 0 ? (
        <p className="t-meta font-medium text-foreground" data-testid="erase-foreign-warning">
          {countLabel(
            preview.foreign_rows,
            t.adminUsers.eraseForeignWarningOne,
            t.adminUsers.eraseForeignWarning,
          )}
        </p>
      ) : null}
      <p className="t-hint text-muted-foreground">{t.adminUsers.eraseRetained}</p>
      <p className="t-hint text-muted-foreground">{t.adminUsers.eraseAsync}</p>
    </div>
  )
}

/**
 * The status of an account being erased: how far its erasure got, or why it
 * stopped with a way to try again. Without a readable operation (another
 * administrator started it) the row says only that the account is being
 * erased.
 */
export function ErasureStatus({
  onRetry,
  progress,
}: {
  onRetry: () => void
  progress: AccountErasureProgress | undefined
}) {
  const { t } = useLocale()
  if (progress?.state === 'failed') {
    return (
      <div className="flex flex-col items-start gap-1">
        <StatusBadge
          density="table"
          label={t.adminUsers.erasureFailed}
          tone="destructive"
        />
        {progress.error ? (
          <span className="t-meta-sm text-muted-foreground">{progress.error}</span>
        ) : null}
        <Button className="h-7 px-2" onClick={onRetry} size="sm" variant="outline">
          {t.adminUsers.erasureRetry}
        </Button>
      </div>
    )
  }
  const detail =
    progress?.state === 'running' && progress.phase
      ? t.adminUsers.erasureProgress(
          t.adminUsers.erasureStages[progress.phase],
          progress.completed,
          progress.total,
        )
      : null
  return (
    <div className="flex flex-col items-start gap-0.5">
      <StatusBadge density="table" label={t.adminUsers.erasing} tone="warning" />
      {detail ? (
        <span className="t-meta-sm tabular-nums text-muted-foreground">{detail}</span>
      ) : null}
    </div>
  )
}
