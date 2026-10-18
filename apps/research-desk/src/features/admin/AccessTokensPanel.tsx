import { type FormEvent, useReducer, useState } from 'react'

import { AlertTriangle, Check, Copy, Plus, Trash2 } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
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
import { useLocale } from '@/i18n/LocaleProvider'
import { patRevealReducer } from './adminModel'
import type { usePatTokens } from './usePatTokens'
import { copyTextToClipboard } from '@/lib/clipboard'

function formatDate(seconds: number | null, locale: string): string | null {
  if (seconds == null) return null
  return new Date(seconds * 1000).toLocaleDateString(locale, {
    day: '2-digit',
    month: '2-digit',
    year: 'numeric',
  })
}

export function AccessTokensPanel({
  tokens: pat,
}: {
  tokens: ReturnType<typeof usePatTokens>
}) {
  const { locale, t } = useLocale()
  const { createToken, revokeToken, state } = pat
  const [createOpen, setCreateOpen] = useState(false)
  const [revokeTarget, setRevokeTarget] = useState<{ id: string; name: string } | null>(null)
  const [reveal, dispatchReveal] = useReducer(patRevealReducer, { phase: 'idle' })
  const [copied, setCopied] = useState(false)

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center justify-between gap-3">
        <p className="t-meta max-w-xl text-muted-foreground">
          {t.adminTokens.description}
        </p>
        <Button
          className="shrink-0 gap-1.5 bg-brand text-brand-foreground hover:bg-brand/90"
          onClick={() => setCreateOpen(true)}
          size="sm"
        >
          <Plus className="size-4" />
          {t.adminTokens.createButton}
        </Button>
      </div>

      {state.mutationError ? (
        <p className="flex gap-1.5 text-xs font-medium text-destructive" role="alert">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span>{t.adminTokens.mutationFailed}</span>
        </p>
      ) : null}

      {/* The rows stay while a refresh fails, so the failure needs a voice
          of its own — otherwise the table looks current and is not. */}
      {state.status === 'error' ? (
        <p className="flex gap-1.5 text-xs font-medium text-destructive" role="alert">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          <span>{t.adminTokens.loadFailed}</span>
        </p>
      ) : null}

      <Table variant="fluid">
        <TableHeader>
          <TableRow>
            <TableHead>{t.adminTokens.colName}</TableHead>
            <TableHead>{t.adminTokens.colScopes}</TableHead>
            <TableHead>{t.adminTokens.colLastUsed}</TableHead>
            <TableHead>{t.adminTokens.colExpires}</TableHead>
            <TableHead className="text-right">{t.adminUsers.colActions}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {/* First load only: a refresh after a mutation must not swap a
              filled table for a skeleton, or the page collapses and the
              scroll position is lost. */}
          {state.status === 'loading' && state.tokens.length === 0 ? (
            <TableSkeleton colSpan={5} />
          ) : state.tokens.length === 0 ? (
            <TableEmpty colSpan={5} title={t.adminTokens.empty} />
          ) : (
            state.tokens.map((token) => (
              <TableRow key={token.token_id}>
                <TableCell>
                  <span className="t-list text-foreground">{token.name}</span>
                </TableCell>
                <TableCell>
                  {token.scopes.length === 0 ? (
                    <span className="t-meta text-muted-foreground">
                      {t.adminTokens.scopesAll}
                    </span>
                  ) : (
                    <div className="flex flex-wrap gap-1">
                      {token.scopes.map((scope) => (
                        <span
                          className="t-mono rounded border border-border bg-surface px-1.5 py-0.5 text-muted-foreground"
                          key={scope}
                        >
                          {scope}
                        </span>
                      ))}
                    </div>
                  )}
                </TableCell>
                <TableCell>
                  <span className="t-mono tabular-nums text-muted-foreground">
                    {formatDate(token.last_used_at, locale) ?? t.adminTokens.neverUsed}
                  </span>
                </TableCell>
                <TableCell>
                  <span className="t-mono tabular-nums text-muted-foreground">
                    {formatDate(token.expires_at, locale) ?? t.adminTokens.noExpiry}
                  </span>
                </TableCell>
                <TableCell className="text-right">
                  <Button
                    aria-label={t.adminTokens.revoke}
                    className="text-muted-foreground hover:text-destructive"
                    onClick={() =>
                      setRevokeTarget({ id: token.token_id, name: token.name })
                    }
                    size="icon"
                    variant="ghost"
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </TableCell>
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>

      {createOpen ? (
        <CreateTokenDialog
          createToken={createToken}
          onClose={() => setCreateOpen(false)}
          onCreated={(name, token, tokenId) => {
            setCreateOpen(false)
            setCopied(false)
            dispatchReveal({ name, token, tokenId, type: 'reveal' })
          }}
        />
      ) : null}

      {reveal.phase === 'revealed' ? (
        <Dialog
          closeLabel={t.adminTokens.done}
          description={reveal.name}
          dismissable={false}
          footer={
            <Button onClick={() => dispatchReveal({ type: 'dismiss' })} size="sm">
              {t.adminTokens.done}
            </Button>
          }
          onClose={() => dispatchReveal({ type: 'dismiss' })}
          open
          title={t.adminTokens.createdTitle}
        >
          <div className="rounded-md border border-warning/25 bg-warning/10 p-3">
            <p className="t-label text-foreground">{t.adminTokens.createdHint}</p>
            <div className="mt-2 flex items-center gap-2">
              <code className="t-mono flex-1 select-all break-all rounded bg-background px-2 py-1.5 text-foreground">
                {reveal.token}
              </code>
              <Button
                className="shrink-0 gap-1.5"
                onClick={() => {
                  void copyTextToClipboard(reveal.token)
                  setCopied(true)
                }}
                size="sm"
                variant="outline"
              >
                {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
                {copied ? t.adminTokens.copied : t.adminTokens.copyToken}
              </Button>
            </div>
          </div>
        </Dialog>
      ) : null}

      {revokeTarget ? (
        <Dialog
          closeLabel={t.adminTokens.cancel}
          description={t.adminTokens.revokeHint(revokeTarget.name)}
          footer={
            <>
              <Button onClick={() => setRevokeTarget(null)} size="sm" variant="ghost">
                {t.adminTokens.cancel}
              </Button>
              <Button
                onClick={() => {
                  void revokeToken(revokeTarget.id)
                  setRevokeTarget(null)
                }}
                size="sm"
                variant="destructive"
              >
                {t.adminTokens.confirmRevoke}
              </Button>
            </>
          }
          onClose={() => setRevokeTarget(null)}
          open
          title={t.adminTokens.revokeTitle}
        >
          <p className="t-body text-muted-foreground">
            {t.adminTokens.revokeHint(revokeTarget.name)}
          </p>
        </Dialog>
      ) : null}
    </div>
  )
}

function CreateTokenDialog({
  createToken,
  onClose,
  onCreated,
}: {
  createToken: ReturnType<typeof usePatTokens>['createToken']
  onClose: () => void
  onCreated: (name: string, token: string, tokenId: string) => void
}) {
  const { t } = useLocale()
  const [name, setName] = useState('')
  const [expiryDays, setExpiryDays] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      const days = expiryDays.trim() ? Number(expiryDays) : undefined
      const { token, tokenId } = await createToken({
        name: name.trim(),
        expiresInDays: days && days > 0 ? days : undefined,
      })
      onCreated(name.trim(), token, tokenId)
    } catch {
      setError(t.adminTokens.createFailed)
      setSubmitting(false)
    }
  }

  return (
    <Dialog
      closeLabel={t.adminTokens.cancel}
      onClose={onClose}
      open
      title={t.adminTokens.createTitle}
    >
      <form className="space-y-4" onSubmit={handleSubmit}>
        <div>
          <label className="t-label mb-1.5 block text-foreground" htmlFor="pat-name">
            {t.adminTokens.nameLabel}
          </label>
          <Input
            autoFocus
            id="pat-name"
            onChange={(event) => setName(event.target.value)}
            placeholder={t.adminTokens.namePlaceholder}
            value={name}
          />
        </div>
        <div>
          <label className="t-label mb-1.5 block text-foreground" htmlFor="pat-expiry">
            {t.adminTokens.expiryLabel}
          </label>
          <Input
            id="pat-expiry"
            inputMode="numeric"
            onChange={(event) =>
              setExpiryDays(event.target.value.replace(/[^0-9]/g, ''))
            }
            placeholder={t.adminTokens.expiryPlaceholder}
            value={expiryDays}
          />
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
            disabled={!name.trim() || submitting}
            size="sm"
            type="submit"
          >
            {submitting ? t.adminTokens.creating : t.adminTokens.createSubmit}
          </Button>
        </div>
      </form>
    </Dialog>
  )
}
