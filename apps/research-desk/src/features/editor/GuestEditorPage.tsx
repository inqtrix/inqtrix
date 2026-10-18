import { EDITOR_SCHEMA_VERSION } from '@inqtrix/editor-schema'
import {
  useEditorState,
  type Editor,
} from '@tiptap/react'
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import {
  describeEditorGuestLink,
  getEditorGuestSession,
  unlockEditorGuestLink,
  type EditorGuestAccessSession,
  type EditorGuestLinkDescription,
} from '@/api/inqtrixClient'
import { BrandMark } from '@/components/BrandMark'
import {
  AlertTriangle,
  Clock3,
  Eye,
  LoaderCircle,
  LockKeyhole,
  MessageSquareText,
  PencilLine,
  Redo2,
  RefreshCw,
  Undo2,
  Users,
} from '@/components/icons'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import type { EditorDocumentRecord } from '@/features/project/types'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import { COLLABORATION_NOTICES } from './collaborationNotices'
import {
  CollaborationEditRefusalLine,
  CollaborationRejectionBar,
  downloadCollaborationBackup,
  useCollaborationEditRefusal,
} from './CollaborationNoticeBars'
import { editorCopy } from './editorCopy'
import { guestAccessEnd, guestSessionIsGone } from './guestAccessEnd'
import { guestUnlockErrorKey } from './guestUnlockErrors'
import {
  localizeCollaborationEditRefusal,
  localizeEditorCollaborationNotice,
} from './inspector/model'
import { TeamCommentsPanel } from './inspector/TeamCommentsPanel'
import { MarkdownEditorSurface } from './core/MarkdownEditorSurface'
import {
  canRunEditorHistoryCommand,
  runEditorHistoryCommand,
} from './core/editorHistoryCommands'
import {
  clearCollaborationCommentDrafts,
  clearOtherGuestsCommentDrafts,
  useCollaborationComments,
} from './useCollaborationComments'
import {
  discardGuestCollaborationLifecycle,
  useGuestCollaborationDocument,
} from './useCollaborationDocument'

type GuestEditorPageProps = {
  token: string
}

export function GuestEditorPage({ token }: GuestEditorPageProps) {
  const { locale } = useLocale()
  const labels = guestCopy[locale]
  const [description, setDescription] = useState<EditorGuestLinkDescription | null>(null)
  const [access, setAccess] = useState<EditorGuestAccessSession | null>(null)
  const [status, setStatus] = useState<'loading' | 'locked' | 'ready' | 'error' | 'ended'>('loading')
  const [error, setError] = useState<string | null>(null)
  const [unsavedChangesLost, setUnsavedChangesLost] = useState(false)
  // Bumped by "open the link again": the link is opened from the start, as
  // on first arrival, and never reconnected in place.
  const [openRevision, setOpenRevision] = useState(0)

  useEffect(() => {
    let active = true
    setStatus('loading')
    setError(null)
    void Promise.allSettled([
      describeEditorGuestLink(token),
      getEditorGuestSession(),
    ]).then(([descriptionResult, sessionResult]) => {
      if (!active) return
      if (descriptionResult.status === 'rejected') {
        setStatus('error')
        setError(labels.invalid)
        return
      }
      setDescription(descriptionResult.value)
      if (
        sessionResult.status === 'fulfilled'
        && sessionResult.value.guest.link_label === descriptionResult.value.label
      ) {
        setAccess(sessionResult.value)
        setStatus('ready')
        return
      }
      setStatus('locked')
    })
    return () => {
      active = false
    }
  }, [labels.invalid, openRevision, token])

  const endAccess = useCallback(({
    access: ended,
    unsavedChanges,
  }: {
    access: EditorGuestAccessSession
    unsavedChanges: boolean
  }) => {
    discardGuestCollaborationLifecycle({
      documentId: ended.document.id,
      generation: ended.document.generation,
      guestId: ended.guest.id,
    })
    // Unsent comments stay behind on this browser only while access lasts.
    clearCollaborationCommentDrafts({
      documentId: ended.document.id,
      generation: ended.document.generation,
      guestId: ended.guest.id,
    })
    // The session carried the document's text; nothing of it stays.
    setAccess(null)
    setDescription(null)
    setUnsavedChangesLost(unsavedChanges)
    setStatus('ended')
  }, [])

  if (status === 'ended') {
    return (
      <GuestShell>
        <GuestAccessEnded
          onReopen={() => {
            setUnsavedChangesLost(false)
            setStatus('loading')
            setOpenRevision((revision) => revision + 1)
          }}
          unsavedChanges={unsavedChangesLost}
        />
      </GuestShell>
    )
  }
  if (status === 'loading') {
    return <GuestShell><GuestLoading label={labels.loading} /></GuestShell>
  }
  if (status === 'error' || description === null) {
    return (
      <GuestShell>
        <GuestError
          description={error ?? labels.invalid}
          title={labels.unavailable}
        />
      </GuestShell>
    )
  }
  if (status !== 'ready' || access === null) {
    return (
      <GuestShell>
        <GuestUnlock
          description={description}
          labels={labels}
          onUnlocked={(next) => {
            setAccess(next)
            setStatus('ready')
          }}
          token={token}
        />
      </GuestShell>
    )
  }
  return <GuestWorkspace access={access} onAccessEnded={endAccess} />
}

function GuestUnlock({
  description,
  labels,
  onUnlocked,
  token,
}: {
  description: EditorGuestLinkDescription
  labels: typeof guestCopy.de | typeof guestCopy.en
  onUnlocked: (access: EditorGuestAccessSession) => void
  token: string
}) {
  const [password, setPassword] = useState('')
  const [displayName, setDisplayName] = useState('')
  const [pending, setPending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const nameRequired = description.permission !== 'view'

  return (
    <main className="grid min-h-svh place-items-center bg-background px-4 py-10">
      <section className="w-full max-w-md rounded-xl border border-border bg-surface p-6 shadow-sm">
        <div className="mb-6 flex items-start gap-3">
          <div className="grid size-10 shrink-0 place-items-center rounded-lg bg-brand-subtle text-brand">
            <LockKeyhole className="size-5" />
          </div>
          <div className="min-w-0">
            <p className="t-meta-sm text-muted-foreground">{labels.sharedDocument}</p>
            <h1 className="t-title mt-0.5 truncate">{description.document_title}</h1>
            <p className="t-meta mt-1 text-muted-foreground">
              {permissionLabel(description.permission, labels)}
              {' · '}
              {labels.expires} {formatDate(description.expires_at)}
            </p>
          </div>
        </div>
        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault()
            if (!password || (nameRequired && !displayName.trim()) || pending) return
            setPending(true)
            setError(null)
            void unlockEditorGuestLink(token, {
              password,
              ...(displayName.trim() ? { display_name: displayName.trim() } : {}),
            }).then(onUnlocked).catch((cause: unknown) => {
              setError(labels[guestUnlockErrorKey(cause)])
            }).finally(() => setPending(false))
          }}
        >
          {nameRequired ? (
            <label className="block space-y-1.5">
              <span className="t-meta font-medium">{labels.displayName}</span>
              <Input
                autoComplete="name"
                autoFocus
                maxLength={80}
                onChange={(event) => setDisplayName(event.target.value)}
                placeholder={labels.displayNamePlaceholder}
                value={displayName}
              />
              <span className="t-hint text-muted-foreground">{labels.nameHint}</span>
            </label>
          ) : null}
          <label className="block space-y-1.5">
            <span className="t-meta font-medium">{labels.password}</span>
            <Input
              autoComplete="current-password"
              autoFocus={!nameRequired}
              onChange={(event) => setPassword(event.target.value)}
              placeholder={labels.passwordPlaceholder}
              type="password"
              value={password}
            />
          </label>
          {error ? (
            <p className="t-meta rounded-md border border-destructive/25 bg-destructive/5 px-3 py-2 text-destructive" role="alert">
              {error}
            </p>
          ) : null}
          <Button
            className="w-full"
            disabled={!password || (nameRequired && !displayName.trim()) || pending}
            type="submit"
          >
            {pending ? <LoaderCircle className="icon-sm animate-spin" /> : <LockKeyhole className="icon-sm" />}
            {labels.open}
          </Button>
        </form>
        <p className="t-hint mt-5 border-t border-border pt-4 text-muted-foreground">
          {labels.security}
        </p>
      </section>
    </main>
  )
}

function GuestWorkspace({
  access,
  onAccessEnded,
}: {
  access: EditorGuestAccessSession
  onAccessEnded: (ended: { access: EditorGuestAccessSession; unsavedChanges: boolean }) => void
}) {
  const { locale } = useLocale()
  const labels = guestCopy[locale]
  const copy = editorCopy[locale]
  const collaboration = useGuestCollaborationDocument({ access, active: true })
  const accessEnd = guestAccessEnd(collaboration)
  const endKind = accessEnd.kind
  const endUnsavedChanges = accessEnd.kind === 'continue' ? false : accessEnd.unsavedChanges
  const endReportedRef = useRef(false)
  const verificationRef = useRef<AbortController | null>(null)

  // The service is the source of truth: access ends on its refusal, never on
  // a guess. The state is published again on every change, so the end is
  // reported once and the one verification request is started once.
  useEffect(() => {
    if (endReportedRef.current) return
    if (endKind === 'ended') {
      endReportedRef.current = true
      onAccessEnded({ access, unsavedChanges: endUnsavedChanges })
      return
    }
    if (endKind !== 'verify' || verificationRef.current !== null) return
    const verification = new AbortController()
    verificationRef.current = verification
    void getEditorGuestSession({ signal: verification.signal }).then(
      // Access remains: the read-only view stays as it is.
      () => undefined,
      (cause: unknown) => {
        // An outage says nothing about access; the read-only view stays.
        if (verification.signal.aborted || endReportedRef.current) return
        if (!guestSessionIsGone(cause)) return
        endReportedRef.current = true
        onAccessEnded({ access, unsavedChanges: endUnsavedChanges })
      },
    )
  }, [access, endKind, endUnsavedChanges, onAccessEnded])
  useEffect(() => () => verificationRef.current?.abort(), [])
  // This guest session holds the browser now; no earlier guest returns here.
  useEffect(() => {
    clearOtherGuestsCommentDrafts(access.guest.id)
  }, [access.guest.id])
  const comments = useCollaborationComments({
    active: true,
    apiKey: undefined,
    documentId: access.document.id,
    eventVersion: collaboration.commentEventVersion,
    generation: access.document.generation,
    guestId: access.guest.id,
    initialRevision: access.document.comment_revision,
    locale,
    mentionEventVersion: collaboration.commentMentionEventVersion,
    workspaceId: 'guest',
  })
  const [selectedThreadId, setSelectedThreadId] = useState<string | null>(null)
  const [editor, setEditor] = useState<Editor | null>(null)
  const editRefusal = localizeCollaborationEditRefusal(
    useCollaborationEditRefusal(editor),
    locale,
  )
  const history = useEditorState({
    editor,
    selector: ({ editor: currentEditor }) => ({
      canRedo: canRunEditorHistoryCommand(currentEditor, 'redo'),
      canUndo: canRunEditorHistoryCommand(currentEditor, 'undo'),
    }),
  })
  const canComment = access.permission !== 'view'
  const writeMode = access.permission === 'comment'
    ? 'comment'
    : access.permission
  const document = useMemo<EditorDocumentRecord>(() => ({
    access: {
      mode: 'shared',
      permission: access.permission === 'comment' ? 'view' : access.permission,
    },
    collaboration: {
      commentRevision: access.document.comment_revision,
      generation: access.document.generation,
      persistedSequence: access.document.persisted_sequence,
      projectionSequence: access.document.projection_sequence,
      schemaVersion: EDITOR_SCHEMA_VERSION,
    },
    contentMarkdown: access.document.content_markdown,
    contentMode: 'collaboration',
    createdAt: new Date(0).toISOString(),
    folderId: null,
    id: access.document.id,
    metadataRevision: 1,
    revision: 1,
    source: 'blank',
    title: access.document.title,
    updatedAt: new Date(
      access.document.projection_sequence * 1_000,
    ).toISOString(),
  }), [access])
  const positions = useMemo(() => new Map(
    comments.threads.map((thread) => [
      thread.id,
      typeof thread.anchor.from === 'number'
        ? thread.anchor.from
        : Number.MAX_SAFE_INTEGER,
    ]),
  ), [comments.threads])
  const orphaned = useMemo(() => new Set(
    comments.threads
      .filter((thread) => typeof thread.anchor.from !== 'number')
      .map((thread) => thread.id),
  ), [comments.threads])

  useEffect(() => {
    if (comments.revision <= comments.lastReadRevision) return
    const timer = window.setTimeout(() => void comments.markRead(), 600)
    return () => window.clearTimeout(timer)
  }, [comments.lastReadRevision, comments.markRead, comments.revision])

  return (
    <div className="flex h-svh min-h-0 flex-col bg-background">
      <header className="flex h-14 shrink-0 items-center gap-3 border-b border-border px-3 sm:px-4">
        <div className="flex min-w-0 flex-1 items-center gap-2.5">
          <BrandMark className="size-6 shrink-0" />
          <div className="min-w-0">
            <h1 className="t-list truncate">{access.document.title}</h1>
            <p className="t-hint truncate text-muted-foreground">
              {labels.guestAs} {access.guest.display_name ?? labels.anonymous}
              {' · '}
              {permissionLabel(access.permission, labels)}
            </p>
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <Button
            aria-label={labels.undo}
            disabled={!history?.canUndo}
            onClick={() => runEditorHistoryCommand(editor, 'undo')}
            size="icon"
            type="button"
            variant="ghost"
          >
            <Undo2 className="icon-sm" />
          </Button>
          <Button
            aria-label={labels.redo}
            disabled={!history?.canRedo}
            onClick={() => runEditorHistoryCommand(editor, 'redo')}
            size="icon"
            type="button"
            variant="ghost"
          >
            <Redo2 className="icon-sm" />
          </Button>
          <ConnectionChip
            collaboration={collaboration}
            labels={labels}
          />
        </div>
      </header>
      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <main className="flex min-h-[55vh] min-w-0 flex-col">
          <div className="flex h-9 shrink-0 items-center justify-between border-b border-border px-3">
            <span className="t-meta inline-flex items-center gap-1.5 text-muted-foreground">
              {permissionIcon(access.permission)}
              {permissionLabel(access.permission, labels)}
            </span>
            <span className="t-hint inline-flex items-center gap-1 text-muted-foreground">
              <Clock3 className="icon-xs" />
              {labels.expires} {formatDate(access.expires_at)}
            </span>
          </div>
          {collaboration.updateRejected ? (
            <CollaborationRejectionBar
              locale={locale}
              notice={localizeEditorCollaborationNotice(
                collaboration.error ?? COLLABORATION_NOTICES.update_rejected,
                locale,
              ) ?? COLLABORATION_NOTICES.update_rejected}
              onDownloadBackup={editor && !editor.isDestroyed
                ? () => downloadCollaborationBackup(editor, access.document.title, locale)
                : null}
              onReload={collaboration.reloadDocument}
            />
          ) : editRefusal ? (
            <CollaborationEditRefusalLine message={editRefusal} />
          ) : null}
          <MarkdownEditorSurface
            collaboration={collaboration}
            collaborationReviewPolicy={{
              collaboration: true,
              display: 'final',
              documentId: access.document.id,
              enabled: true,
              selectedSuggestionIds: [],
              writeAuthorId: collaboration.user?.id ?? null,
              writeMode,
            }}
            comments={[]}
            copy={copy}
            diffAnchorMarkdown={null}
            document={document}
            isDiffVisible={false}
            mode="live"
            onAcceptSuggestion={() => undefined}
            onChange={() => undefined}
            onCreateComment={() => undefined}
            onCreateTeamComment={canComment
              ? (input) => {
                  void comments.createThread(input).then((thread) => {
                    setSelectedThreadId(thread.id)
                  })
                }
              : undefined}
            onEditSuggestion={() => undefined}
            onEditorReady={setEditor}
            onMarkSuggestionStale={() => undefined}
            onRefineSuggestion={async () => undefined}
            onRejectSuggestion={() => undefined}
            onSelectComment={() => undefined}
            onSelectTeamComment={setSelectedThreadId}
            onStopSuggestion={() => undefined}
            onTeamCommentDraftChange={(value) => comments.setDraft('new', value)}
            privateCommentsEnabled={false}
            runningSuggestionIds={[]}
            selectedCommentId={null}
            selectedTeamCommentId={selectedThreadId}
            suggestionErrors={{}}
            suggestions={[]}
            teamCommentDraft={comments.drafts.new ?? ''}
            teamCommentParticipants={comments.participants}
            teamComments={comments.threads}
            textImprovement={{
              enabled: false,
              workspaceId: 'guest',
            }}
          />
        </main>
        <aside className="min-h-[24rem] border-t border-border lg:min-h-0 lg:border-l lg:border-t-0">
          <div className="flex h-10 items-center gap-2 border-b border-border px-3">
            <MessageSquareText className="icon-sm text-muted-foreground" />
            <span className="t-list">{labels.comments}</span>
            <span className="t-hint ml-auto tabular-nums text-muted-foreground">
              {comments.threads.length}
            </span>
          </div>
          <div className="h-[calc(100%-2.5rem)] min-h-0">
            <TeamCommentsPanel
              assistantAvailable={false}
              canComment={canComment}
              comments={comments}
              currentUserId={access.guest.id}
              onSelectThread={setSelectedThreadId}
              onUseWithAssistant={() => undefined}
              orphanedThreadIds={orphaned}
              positionByThreadId={positions}
              selectedThreadId={selectedThreadId}
            />
          </div>
        </aside>
      </div>
    </div>
  )
}

/**
 * What a guest sees once access through the link has ended: one notice,
 * nothing of the document or its comments, and a way back to the link. The
 * link opens from the start again, so a link with a new password asks for
 * it and a revoked link says it is no longer valid. No copy of the document
 * is offered for download: after a revocation none may leave the service.
 */
export function GuestAccessEnded({
  onReopen,
  unsavedChanges,
}: {
  onReopen: () => void
  unsavedChanges: boolean
}) {
  const { locale } = useLocale()
  const labels = guestCopy[locale]
  return (
    <main className="grid min-h-svh place-items-center px-4">
      <section
        className="max-w-md rounded-xl border border-border bg-surface p-6 text-center"
        role="alert"
      >
        <LockKeyhole className="mx-auto size-8 text-warning" />
        <h1 className="t-title mt-3">{labels.accessEndedTitle}</h1>
        <p className="t-body mt-2 text-muted-foreground">{labels.accessEnded}</p>
        {unsavedChanges ? (
          <p className="t-body mt-2 font-medium">{labels.accessEndedUnsaved}</p>
        ) : null}
        <Button autoFocus className="mt-5" onClick={onReopen} type="button">
          <RefreshCw className="icon-sm" />
          {labels.reopenLink}
        </Button>
      </section>
    </main>
  )
}

function ConnectionChip({
  collaboration,
  labels,
}: {
  collaboration: ReturnType<typeof useGuestCollaborationDocument>
  labels: typeof guestCopy.de | typeof guestCopy.en
}) {
  const healthy = collaboration.connectionStatus === 'connected'
    || collaboration.connectionStatus === 'read_only'
  const delayed = healthy && collaboration.durabilityDelayed
  const label = healthy
    ? delayed ? labels.savingDelayed : labels.connected
    : collaboration.connectionStatus === 'connecting'
        || collaboration.connectionStatus === 'reconnecting'
      ? labels.connecting
      : labels.connectionError
  return (
    <Button
      className={cn(
        'h-7 gap-1.5 rounded-full px-2.5 t-meta-sm',
        healthy && !delayed && 'text-success',
        delayed && 'text-warning',
        !healthy && 'text-muted-foreground',
      )}
      disabled={healthy || collaboration.recoverability !== 'retry'}
      onClick={() => void collaboration.retryConnection()}
      size="sm"
      type="button"
      variant="ghost"
    >
      {healthy ? (
        <span className={cn('size-1.5 rounded-full', delayed ? 'bg-warning' : 'bg-success')} />
      ) : collaboration.connectionStatus === 'connecting'
        || collaboration.connectionStatus === 'reconnecting' ? (
          <LoaderCircle className="icon-xs animate-spin" />
        ) : (
          <RefreshCw className="icon-xs" />
        )}
      <span className="hidden sm:inline">{label}</span>
    </Button>
  )
}

function permissionIcon(permission: EditorGuestAccessSession['permission']) {
  if (permission === 'view') return <Eye className="icon-sm" />
  if (permission === 'comment') return <MessageSquareText className="icon-sm" />
  if (permission === 'suggest') return <PencilLine className="icon-sm" />
  return <Users className="icon-sm" />
}

function permissionLabel(
  permission: EditorGuestAccessSession['permission'],
  labels: typeof guestCopy.de | typeof guestCopy.en,
) {
  return labels.permissions[permission]
}

function GuestShell({ children }: { children: ReactNode }) {
  return <div className="min-h-svh bg-background">{children}</div>
}

function GuestLoading({ label }: { label: string }) {
  return (
    <div className="grid min-h-svh place-items-center">
      <div className="flex items-center gap-2 text-muted-foreground">
        <LoaderCircle className="icon-sm animate-spin" />
        <span className="t-meta">{label}</span>
      </div>
    </div>
  )
}

function GuestError({
  description,
  title,
}: {
  description: string
  title: string
}) {
  return (
    <main className="grid min-h-svh place-items-center px-4">
      <section className="max-w-md rounded-xl border border-border bg-surface p-6 text-center">
        <AlertTriangle className="mx-auto size-8 text-warning" />
        <h1 className="t-title mt-3">{title}</h1>
        <p className="t-body mt-2 text-muted-foreground">{description}</p>
      </section>
    </main>
  )
}

function formatDate(epochSeconds: number) {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(epochSeconds * 1_000))
}

const guestCopy = {
  de: {
    accessEnded: 'Der Zugriff über diesen Link ist beendet. Der Link wurde widerrufen, ist abgelaufen oder sein Passwort wurde geändert, oder das Dokument wurde gelöscht. Wenn Sie weiter mitarbeiten möchten, bitten Sie die Person, die das Dokument mit Ihnen geteilt hat, um einen neuen Link oder das neue Passwort.',
    accessEndedTitle: 'Kein Zugriff mehr',
    accessEndedUnsaved: 'Ihre letzten Änderungen konnten nicht mehr gespeichert werden und sind verloren.',
    anonymous: 'Gast',
    comments: 'Kommentare',
    connected: 'Verbunden',
    connecting: 'Verbinden …',
    connectionError: 'Neu verbinden',
    savingDelayed: 'Speichern verzögert',
    displayName: 'Ihr Anzeigename',
    displayNamePlaceholder: 'z. B. Maria',
    expires: 'Gültig bis',
    guestAs: 'Gast:',
    invalid: 'Dieser Freigabelink ist ungültig, abgelaufen oder wurde widerrufen.',
    loading: 'Freigabe wird geprüft …',
    nameHint: 'Der Name erscheint bei Kommentaren und Änderungen.',
    open: 'Dokument öffnen',
    password: 'Link-Passwort',
    passwordPlaceholder: 'Passwort eingeben',
    permissions: {
      comment: 'Kommentieren',
      edit: 'Bearbeiten',
      suggest: 'Vorschlagen',
      view: 'Lesen',
    },
    redo: 'Wiederholen',
    reopenLink: 'Link erneut öffnen',
    security: 'Diese Sitzung ist ausschließlich auf dieses Dokument begrenzt.',
    sharedDocument: 'Mit Ihnen geteiltes Dokument',
    unavailable: 'Dokument nicht verfügbar',
    undo: 'Rückgängig',
    unlockFailed: 'Link oder Passwort ist ungültig. Bitte prüfen Sie beide Angaben.',
    unlockBlocked: 'Dieser Zugang wurde abgelehnt, bevor Ihr Passwort geprüft wurde — der Link wird über eine Adresse geöffnet, die dieser Server nicht zulässt. Bitte die Adresse aus der Einladung unverändert verwenden.',
    unlockRateLimited: 'Zu viele Versuche. Bitte warten Sie einen Moment.',
    unlockUnavailable: 'Der Server hat nicht geantwortet. Bitte später erneut versuchen.',
  },
  en: {
    accessEnded: 'Access through this link has ended. The link was revoked, has expired or its password was changed, or the document was deleted. To keep working on it, ask the person who shared the document with you for a new link or the new password.',
    accessEndedTitle: 'You no longer have access',
    accessEndedUnsaved: 'Your latest changes could not be saved and are lost.',
    anonymous: 'Guest',
    comments: 'Comments',
    connected: 'Connected',
    connecting: 'Connecting…',
    connectionError: 'Reconnect',
    savingDelayed: 'Saving delayed',
    displayName: 'Your display name',
    displayNamePlaceholder: 'e.g. Maria',
    expires: 'Expires',
    guestAs: 'Guest:',
    invalid: 'This shared link is invalid, expired, or has been revoked.',
    loading: 'Checking shared link…',
    nameHint: 'This name appears next to comments and changes.',
    open: 'Open document',
    password: 'Link password',
    passwordPlaceholder: 'Enter password',
    permissions: {
      comment: 'Comment',
      edit: 'Edit',
      suggest: 'Suggest',
      view: 'View',
    },
    redo: 'Redo',
    reopenLink: 'Open the link again',
    security: 'This session is strictly scoped to this document.',
    sharedDocument: 'Document shared with you',
    unavailable: 'Document unavailable',
    undo: 'Undo',
    unlockFailed: 'The link or password is invalid. Check both values.',
    unlockBlocked: 'This access was refused before your password was checked — the link is being opened from an address this server does not allow. Use the address from the invitation unchanged.',
    unlockRateLimited: 'Too many attempts. Please wait a moment.',
    unlockUnavailable: 'The server did not respond. Please try again later.',
  },
} as const
