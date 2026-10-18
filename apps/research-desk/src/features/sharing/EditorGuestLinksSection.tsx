import { useCallback, useEffect, useState } from 'react'
import {
  Copy,
  KeyRound,
  Link,
  Plus,
  Repeat2,
  RotateCcw,
  Trash2,
} from '@/components/icons'
import {
  createEditorShareLink,
  listEditorShareLinks,
  revokeEditorShareLink,
  rotateEditorShareLinkPassword,
  rotateEditorShareLinkToken,
  updateEditorShareLink,
} from '@/api/inqtrixClient'
import { Button } from '@/components/ui/button'
import { collaborationCommandId } from '@/features/editor/collaborationCrypto'
import { cn } from '@/lib/utils'
import type {
  EditorShareLink,
  EditorShareLinkPermission,
} from './types'
import { copyTextToClipboard } from '@/lib/clipboard'
import { readerError } from '@/api/requestErrorText'

const PERMISSIONS: EditorShareLinkPermission[] = [
  'view',
  'comment',
  'suggest',
  'edit',
]

const EXPIRIES = [
  { days: 0, labelDe: '1 Stunde', labelEn: '1 hour', seconds: 3600 },
  { days: 1, labelDe: '1 Tag', labelEn: '1 day', seconds: 86400 },
  { days: 7, labelDe: '7 Tage', labelEn: '7 days', seconds: 604800 },
  { days: 30, labelDe: '30 Tage', labelEn: '30 days', seconds: 2592000 },
]

/** A freshly minted secret, shown exactly once.
 *
 * Creating yields both halves, rotating one yields that half alone — one
 * state instead of three, so the "nothing copied yet" guard has a single
 * source of truth to report upwards.
 */
export type PendingSecret = {
  label: string
  password?: string
  url?: string
}

export type SecretPart = 'password' | 'url'

/** Which halves of a one-time secret the owner has NOT captured yet. */
export function uncopiedParts(
  secret: PendingSecret,
  copied: ReadonlySet<SecretPart>,
): SecretPart[] {
  const parts: SecretPart[] = []
  if (secret.url !== undefined && !copied.has('url')) parts.push('url')
  if (secret.password !== undefined && !copied.has('password')) {
    parts.push('password')
  }
  return parts
}

/** One click captures the whole one-time moment.
 *
 * The card also advises sending the two halves separately, and a single
 * clipboard entry works against that — so the reminder travels WITH the
 * text: a careless paste still carries it.
 */
export function secretClipboardText(
  secret: PendingSecret,
  copy: { copyHint: string; password: string; url: string },
): string {
  return [
    `${copy.url}: ${secret.url}`,
    '',
    `${copy.password}: ${secret.password}`,
    '',
    copy.copyHint,
  ].join('\n')
}

type EditorGuestLinksSectionProps = {
  documentId: string
  generation: number
  locale: 'de' | 'en'
  onChanged?: () => void
  /** Reports whether a one-time secret is on screen that nobody copied.
   * The dialog asks before closing over it; the value is irreversible. */
  onUncopiedSecretChange?: (uncopied: boolean) => void
}

export function EditorGuestLinksSection({
  documentId,
  generation,
  locale,
  onChanged,
  onUncopiedSecretChange,
}: EditorGuestLinksSectionProps) {
  const [links, setLinks] = useState<EditorShareLink[]>([])
  const [secret, setSecret] = useState<PendingSecret | null>(null)
  const [copiedParts, setCopiedParts] = useState<Set<SecretPart>>(
    () => new Set(),
  )
  const [permission, setPermission] =
    useState<EditorShareLinkPermission>('view')
  const [ttlSeconds, setTtlSeconds] = useState(604800)
  const [status, setStatus] =
    useState<'creating' | 'error' | 'loading' | 'ready'>('loading')
  const [busyId, setBusyId] = useState<string | null>(null)
  const [copied, setCopied] = useState<'both' | SecretPart | null>(null)

  const hasUncopiedSecret =
    secret !== null && uncopiedParts(secret, copiedParts).length > 0
  useEffect(() => {
    onUncopiedSecretChange?.(hasUncopiedSecret)
  }, [hasUncopiedSecret, onUncopiedSecretChange])

  const copy = locale === 'de'
    ? {
        add: 'Link erstellen',
        copied: 'Kopiert',
        copyBoth: 'Link und Passwort kopieren',
        copyHint: 'Link und Passwort getrennt übermitteln.',
        description:
          'Zugriff ohne Inqtrix-Konto. Link und Passwort getrennt übermitteln.',
        empty: 'Noch keine Gastlinks.',
        error: 'Gastlinks konnten nicht geladen oder geändert werden.',
        expires: 'Läuft ab',
        opens: 'Öffnungen',
        password: 'Passwort',
        bothOnce: 'Link und Passwort werden nur jetzt angezeigt.',
        linkOnce: 'Der Link wird nur jetzt angezeigt.',
        passwordOnce: 'Das Passwort wird nur jetzt angezeigt.',
        revoke: 'Link beenden',
        rotate: 'Passwort neu erzeugen',
        rotateToken: 'Link neu erzeugen (alter Link wird ungültig)',
        sessions: 'Sitzungen',
        title: 'Gastlinks',
        url: 'Link',
      }
    : {
        add: 'Create link',
        copied: 'Copied',
        copyBoth: 'Copy link and password',
        copyHint: 'Send link and password separately.',
        description:
          'Access without an Inqtrix account. Send link and password separately.',
        empty: 'No guest links yet.',
        error: 'Guest links could not be loaded or changed.',
        expires: 'Expires',
        opens: 'Opens',
        password: 'Password',
        bothOnce: 'Link and password are shown only now.',
        linkOnce: 'The link is shown only now.',
        passwordOnce: 'The password is shown only now.',
        revoke: 'End link',
        rotate: 'Generate new password',
        rotateToken: 'Generate new link (the old one stops working)',
        sessions: 'Sessions',
        title: 'Guest links',
        url: 'Link',
      }

  const permissionLabel = (value: EditorShareLinkPermission) => {
    if (locale === 'de') {
      return {
        comment: 'Kommentieren',
        edit: 'Bearbeiten',
        suggest: 'Vorschlagen',
        view: 'Lesen',
      }[value]
    }
    return {
      comment: 'Comment',
      edit: 'Edit',
      suggest: 'Suggest',
      view: 'Read',
    }[value]
  }

  const reload = useCallback(async () => {
    setStatus('loading')
    try {
      setLinks(await listEditorShareLinks(documentId))
      setStatus('ready')
    } catch {
      setStatus('error')
    }
  }, [documentId])

  useEffect(() => {
    void reload()
  }, [reload])

  const create = async () => {
    if (status === 'creating') return
    setStatus('creating')
    try {
      const next = await createEditorShareLink(documentId, {
        commandId: collaborationCommandId(),
        generation,
        permission,
        ttlSeconds,
      })
      showSecret({
        label: next.label,
        password: next.password,
        url: next.url,
      })
      setLinks((current) => [
        next,
        ...current.filter((link) => link.id !== next.id),
      ])
      setStatus('ready')
      onChanged?.()
    } catch {
      setStatus('error')
    }
  }

  const mutate = async (
    link: EditorShareLink,
    operation:
      | 'revoke'
      | 'rotate'
      | 'rotate-token'
      | EditorShareLinkPermission,
  ) => {
    setBusyId(link.id)
    try {
      if (operation === 'revoke') {
        const next = await revokeEditorShareLink(documentId, link.id, {
          commandId: collaborationCommandId(),
          expectedRevision: link.revision,
        })
        setLinks((current) =>
          current.map((item) => (item.id === next.id ? next : item)))
        // A secret for a link that no longer exists is worth nothing, and
        // warning about it before closing would be a nag about a dead value.
        if (secret?.label === next.label) setSecret(null)
      } else if (operation === 'rotate') {
        const next = await rotateEditorShareLinkPassword(documentId, link.id, {
          commandId: collaborationCommandId(),
          expectedRevision: link.revision,
        })
        setLinks((current) =>
          current.map((item) => (item.id === next.id ? next : item)))
        showSecret({ label: next.label, password: next.password })
      } else if (operation === 'rotate-token') {
        const next = await rotateEditorShareLinkToken(documentId, link.id, {
          commandId: collaborationCommandId(),
          expectedRevision: link.revision,
        })
        setLinks((current) =>
          current.map((item) => (item.id === next.id ? next : item)))
        showSecret({ label: next.label, url: next.url })
      } else {
        const next = await updateEditorShareLink(documentId, link.id, {
          commandId: collaborationCommandId(),
          expectedRevision: link.revision,
          permission: operation,
        })
        setLinks((current) =>
          current.map((item) => (item.id === next.id ? next : item)))
      }
      setStatus('ready')
      onChanged?.()
    } catch {
      // Order matters: `reload` ends in 'ready', so setting the error first
      // would erase it and a failed rotation would look like nothing
      // happened — which is how a 500 once left this panel silent.
      await reload()
      setStatus('error')
    } finally {
      setBusyId(null)
    }
  }

  /** Replaces whatever card is on screen and restarts the copy tracking. */
  const showSecret = (next: PendingSecret) => {
    setSecret(next)
    setCopiedParts(new Set())
    setCopied(null)
  }

  const copyValue = async (
    kind: 'both' | SecretPart,
    value: string,
    parts: SecretPart[],
  ) => {
    try {
      if (!(await copyTextToClipboard(value))) {
        throw readerError('clipboardUnavailable')
      }
      setCopiedParts((current) => {
        const next = new Set(current)
        for (const part of parts) next.add(part)
        return next
      })
      setCopied(kind)
      window.setTimeout(() => setCopied(null), 1500)
    } catch {
      setCopied(null)
    }
  }

  const copyText = (kind: SecretPart, value: string) =>
    copyValue(kind, value, [kind])

  const copyBoth = (current: PendingSecret) => copyValue(
    'both',
    secretClipboardText(current, copy),
    ['password', 'url'],
  )

  const visibleLinks = links.filter((link) => link.revoked_at === null)

  return (
    <section className="space-y-2.5 border-t border-border pt-3">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h3 className="flex items-center gap-1.5 t-caption text-foreground">
            <Link className="size-3.5 text-muted-foreground" />
            {copy.title}
          </h3>
          <p className="mt-0.5 t-meta-sm text-muted-foreground">
            {copy.description}
          </p>
        </div>
        <span className="rounded-full bg-surface px-1.5 py-0.5 t-meta-sm text-muted-foreground">
          {visibleLinks.length}
        </span>
      </div>

      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] gap-1.5">
        <select
          aria-label={locale === 'de' ? 'Berechtigung' : 'Permission'}
          className="h-8 min-w-0 rounded-md border border-border bg-background px-2 text-xs"
          onChange={(event) =>
            setPermission(event.target.value as EditorShareLinkPermission)}
          value={permission}
        >
          {PERMISSIONS.map((value) => (
            <option key={value} value={value}>{permissionLabel(value)}</option>
          ))}
        </select>
        <select
          aria-label={locale === 'de' ? 'Ablauf' : 'Expiry'}
          className="h-8 min-w-0 rounded-md border border-border bg-background px-2 text-xs"
          onChange={(event) => setTtlSeconds(Number(event.target.value))}
          value={ttlSeconds}
        >
          {EXPIRIES.map((expiry) => (
            <option key={expiry.seconds} value={expiry.seconds}>
              {locale === 'de' ? expiry.labelDe : expiry.labelEn}
            </option>
          ))}
        </select>
        <Button
          className="h-8 bg-brand px-2.5 text-xs text-brand-foreground hover:bg-brand/90"
          disabled={status === 'creating'}
          onClick={() => void create()}
          size="sm"
        >
          <Plus className="size-3.5" />
          {copy.add}
        </Button>
      </div>

      {secret ? (
        <SecretCard
          copy={copy}
          copied={copied}
          label={`Link ${secret.label}`}
          onCopy={copyText}
          onCopyBoth={
            secret.password !== undefined && secret.url !== undefined
              ? () => void copyBoth(secret)
              : undefined
          }
          password={secret.password}
          url={secret.url}
        />
      ) : null}

      {status === 'error' ? (
        <p className="rounded-md border border-destructive/20 bg-destructive/5 px-2.5 py-2 t-meta text-destructive">
          {copy.error}
        </p>
      ) : null}

      <div className="divide-y divide-border overflow-hidden rounded-md border border-border">
        {status === 'loading' ? (
          <p className="px-3 py-3 t-meta text-muted-foreground">…</p>
        ) : visibleLinks.length === 0 ? (
          <p className="px-3 py-3 t-meta text-muted-foreground">{copy.empty}</p>
        ) : visibleLinks.map((link) => (
          <div className="group px-2.5 py-2" key={link.id}>
            <div className="flex items-center gap-2">
              <span className="grid size-7 shrink-0 place-items-center rounded-md bg-surface text-muted-foreground">
                <Link className="size-3.5" />
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-1.5">
                  <span className="t-list text-foreground">Link {link.label}</span>
                  <span className="rounded border border-border px-1 py-0.5 t-meta-sm text-muted-foreground">
                    {permissionLabel(link.permission)}
                  </span>
                </div>
                <p className="truncate t-meta-sm text-muted-foreground">
                  {copy.expires} {new Intl.DateTimeFormat(locale, {
                    dateStyle: 'medium',
                    timeStyle: 'short',
                  }).format(new Date(link.expires_at * 1000))}
                  {' · '}
                  {copy.opens} {link.successful_open_count}
                  {' · '}
                  {copy.sessions} {link.session_count}
                </p>
              </div>
              <select
                aria-label={locale === 'de' ? 'Berechtigung ändern' : 'Change permission'}
                className="h-7 max-w-28 rounded-md border border-border bg-background px-1.5 text-xs opacity-70 focus:opacity-100 group-hover:opacity-100"
                disabled={busyId === link.id}
                onChange={(event) => void mutate(
                  link,
                  event.target.value as EditorShareLinkPermission,
                )}
                value={link.permission}
              >
                {PERMISSIONS.map((value) => (
                  <option key={value} value={value}>{permissionLabel(value)}</option>
                ))}
              </select>
              <button
                aria-label={copy.rotate}
                className="grid size-7 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
                disabled={busyId === link.id}
                onClick={() => void mutate(link, 'rotate')}
                title={copy.rotate}
                type="button"
              >
                <RotateCcw className="size-3.5" />
              </button>
              <button
                aria-label={copy.rotateToken}
                className="grid size-7 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-foreground"
                disabled={busyId === link.id}
                onClick={() => void mutate(link, 'rotate-token')}
                title={copy.rotateToken}
                type="button"
              >
                <Repeat2 className="size-3.5" />
              </button>
              <button
                aria-label={copy.revoke}
                className="grid size-7 place-items-center rounded-md text-muted-foreground hover:bg-accent hover:text-destructive"
                disabled={busyId === link.id}
                onClick={() => void mutate(link, 'revoke')}
                title={copy.revoke}
                type="button"
              >
                <Trash2 className="size-3.5" />
              </button>
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

function SecretCard({
  copy,
  copied,
  label,
  onCopy,
  onCopyBoth,
  password,
  url,
}: {
  copy: {
    bothOnce: string
    copied: string
    copyBoth: string
    linkOnce: string
    password: string
    passwordOnce: string
    url: string
  }
  copied: 'both' | SecretPart | null
  label: string
  onCopy: (kind: SecretPart, value: string) => Promise<void>
  /** Only offered when the card carries BOTH halves. */
  onCopyBoth?: () => void
  password?: string
  url?: string
}) {
  return (
    <div className="rounded-md border border-brand/25 bg-brand-subtle/45 p-2.5">
      <div className="flex items-center gap-1.5">
        <KeyRound className="size-3.5 text-brand" />
        <p className="t-caption text-foreground">{label}</p>
        <span className="ml-auto t-meta-sm text-brand">
          {password !== undefined && url !== undefined
            ? copy.bothOnce
            : password !== undefined
              ? copy.passwordOnce
              : copy.linkOnce}
        </span>
      </div>
      <div className="mt-2 space-y-1">
        {url ? (
          <SecretRow
            copied={copied === 'url'}
            copiedLabel={copy.copied}
            label={copy.url}
            onCopy={() => void onCopy('url', url)}
            value={url}
          />
        ) : null}
        {password !== undefined ? (
          <SecretRow
            copied={copied === 'password'}
            copiedLabel={copy.copied}
            label={copy.password}
            onCopy={() => void onCopy('password', password)}
            value={password}
          />
        ) : null}
      </div>
      {onCopyBoth ? (
        <button
          className="mt-2 flex w-full items-center justify-center gap-1.5 rounded bg-background/75 px-2 py-1.5 t-meta text-foreground hover:bg-background"
          onClick={onCopyBoth}
          type="button"
        >
          <Copy className="size-3.5" />
          {copied === 'both' ? copy.copied : copy.copyBoth}
        </button>
      ) : null}
    </div>
  )
}

function SecretRow({
  copied,
  copiedLabel,
  label,
  onCopy,
  value,
}: {
  copied: boolean
  copiedLabel: string
  label: string
  onCopy: () => void
  value: string
}) {
  return (
    <div className="grid grid-cols-[4rem_minmax(0,1fr)_auto] items-center gap-2 rounded bg-background/75 px-2 py-1.5">
      <span className="t-meta-sm text-muted-foreground">{label}</span>
      <code className="truncate text-xs text-foreground">{value}</code>
      <button
        className={cn(
          'grid size-6 place-items-center rounded text-muted-foreground hover:bg-accent hover:text-foreground',
          copied && 'text-brand',
        )}
        onClick={onCopy}
        title={copied ? copiedLabel : label}
        type="button"
      >
        <Copy className="size-3.5" />
      </button>
    </div>
  )
}
