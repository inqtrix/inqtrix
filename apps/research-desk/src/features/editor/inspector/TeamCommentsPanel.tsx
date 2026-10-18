import { useEffect, useMemo, useRef, useState } from 'react'

import {
  AtSign,
  ArrowUpDown,
  Check,
  ChevronLeft,
  ChevronRight,
  Link,
  LoaderCircle,
  ListFilter,
  MoreHorizontal,
  PencilLine,
  RotateCcw,
  Search,
  CornerDownLeft,
  Sparkles,
  Trash2,
} from '@/components/icons'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import type {
  EditorCollaborationCommentActor,
  EditorCollaborationCommentMessage,
  EditorCollaborationCommentThread,
} from '@/api/inqtrixClient'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import type { CollaborationCommentsHandle } from '../useCollaborationComments'

type TeamCommentsPanelProps = {
  assistantAvailable?: boolean
  canComment: boolean
  comments: CollaborationCommentsHandle
  currentUserId: string | null
  onSelectThread: (threadId: string | null) => void
  onUseWithAssistant: (thread: EditorCollaborationCommentThread) => void
  orphanedThreadIds: ReadonlySet<string>
  positionByThreadId: ReadonlyMap<string, number>
  selectedThreadId: string | null
}

const copy = {
  de: {
    allResolved: 'Keine erledigten Diskussionen.',
    allOpen: 'Keine offenen Diskussionen.',
    allFiltered: 'Keine Kommentare entsprechen diesem Filter.',
    comment: 'Kommentar',
    comments: 'Kommentare',
    actions: 'Kommentaraktionen',
    cancel: 'Abbrechen',
    deleted: 'Beitrag gelöscht',
    edit: 'Bearbeiten',
    edited: 'bearbeitet',
    filter: 'Kommentarfilter',
    mention: 'Person erwähnen',
    mentions: 'Erwähnungen',
    messageActions: 'Beitragsaktionen',
    noParticipants: 'Keine weiteren Teilnehmenden',
    next: 'Nächster Kommentar',
    loadMore: 'Weitere laden',
    newest: 'Neueste Aktivität',
    open: 'Offen',
    orphaned: 'Nicht mehr verankert',
    position: 'Dokumentposition',
    previous: 'Vorheriger Kommentar',
    reopen: 'Wieder öffnen',
    oneReply: 'Antwort',
    replies: 'Antworten',
    reply: 'Antworten …',
    resolve: 'Auflösen',
    resolved: 'Erledigt',
    search: 'Kommentare durchsuchen',
    sort: 'Sortierung',
    save: 'Speichern',
    send: 'Antwort senden',
    useWithAssistant: 'Mit Assistenz verwenden',
    viewOnly: 'Sie können diese Diskussionen lesen, aber nicht bearbeiten.',
    unread: 'Ungelesen',
    delete: 'Löschen',
    deleteThread: 'Kommentar löschen',
  },
  en: {
    allResolved: 'No resolved discussions.',
    allOpen: 'No open discussions.',
    allFiltered: 'No comments match this filter.',
    comment: 'comment',
    comments: 'Comments',
    actions: 'Comment actions',
    cancel: 'Cancel',
    deleted: 'Message deleted',
    edit: 'Edit',
    edited: 'edited',
    filter: 'Comment filters',
    mention: 'Mention a person',
    mentions: 'Mentions',
    messageActions: 'Message actions',
    noParticipants: 'No other participants',
    next: 'Next comment',
    loadMore: 'Load more',
    newest: 'Newest activity',
    open: 'Open',
    orphaned: 'No longer anchored',
    position: 'Document position',
    previous: 'Previous comment',
    reopen: 'Reopen',
    oneReply: 'reply',
    replies: 'Replies',
    reply: 'Reply…',
    resolve: 'Resolve',
    resolved: 'Resolved',
    search: 'Search comments',
    sort: 'Sort',
    save: 'Save',
    send: 'Send reply',
    useWithAssistant: 'Use with assistant',
    viewOnly: 'You can read these discussions but cannot change them.',
    unread: 'Unread',
    delete: 'Delete',
    deleteThread: 'Delete comment',
  },
} as const

export function TeamCommentsPanel({
  assistantAvailable = true,
  canComment,
  comments,
  currentUserId,
  onSelectThread,
  onUseWithAssistant,
  orphanedThreadIds,
  positionByThreadId,
  selectedThreadId,
}: TeamCommentsPanelProps) {
  const { locale } = useLocale()
  const labels = copy[locale]
  const [filter, setFilter] = useState<
    'mentions' | 'open' | 'orphaned' | 'resolved' | 'unread'
  >('open')
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState<'newest' | 'position'>('position')
  const [visibleCount, setVisibleCount] = useState(50)
  const counts = useMemo(() => ({
    mentions: comments.threads.filter((thread) => (
      currentUserId !== null
      && thread.messages.some((message) => (
        message.mentions.some((mention) => mention.id === currentUserId)
      ))
    )).length,
    open: comments.threads.filter((thread) => thread.status === 'open').length,
    orphaned: comments.threads.filter((thread) => orphanedThreadIds.has(thread.id)).length,
    resolved: comments.threads.filter((thread) => thread.status === 'resolved').length,
    unread: comments.threads.filter(
      (thread) => thread.revision > comments.lastReadRevision,
    ).length,
  }), [
    comments.lastReadRevision,
    comments.threads,
    currentUserId,
    orphanedThreadIds,
  ])
  const filtered = useMemo(
    () => {
      const normalizedQuery = query.trim().toLocaleLowerCase(locale)
      const matching = comments.threads.filter((thread) => {
        const inFilter = filter === 'open'
          ? thread.status === 'open'
          : filter === 'resolved'
            ? thread.status === 'resolved'
            : filter === 'unread'
              ? thread.revision > comments.lastReadRevision
              : filter === 'orphaned'
                ? orphanedThreadIds.has(thread.id)
                : currentUserId !== null
                  && thread.messages.some((message) => (
                    message.mentions.some((mention) => mention.id === currentUserId)
                  ))
        if (!inFilter) return false
        if (!normalizedQuery) return true
        return [
          thread.author.name,
          thread.quote,
          ...thread.messages.map((message) => (
            `${message.author.name} ${message.body_markdown ?? ''}`
          )),
        ].some((value) => value.toLocaleLowerCase(locale).includes(normalizedQuery))
      })
      return matching.sort((left, right) => {
        if (sort === 'newest') {
          return right.updated_at - left.updated_at || left.id.localeCompare(right.id)
        }
        const leftPosition = positionByThreadId.get(left.id) ?? Number.MAX_SAFE_INTEGER
        const rightPosition = positionByThreadId.get(right.id) ?? Number.MAX_SAFE_INTEGER
        return leftPosition - rightPosition
          || right.updated_at - left.updated_at
          || left.id.localeCompare(right.id)
      })
    },
    [
      comments.lastReadRevision,
      comments.threads,
      currentUserId,
      filter,
      locale,
      orphanedThreadIds,
      positionByThreadId,
      query,
      sort,
    ],
  )
  const visible = useMemo(() => {
    const page = filtered.slice(0, visibleCount)
    const selected = selectedThreadId
      ? filtered.find((thread) => thread.id === selectedThreadId)
      : undefined
    return selected && !page.some((thread) => thread.id === selected.id)
      ? [...page, selected]
      : page
  }, [filtered, selectedThreadId, visibleCount])
  const selectedIndex = filtered.findIndex(
    (thread) => thread.id === selectedThreadId,
  )
  const listRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => setVisibleCount(50), [filter, query, sort])

  useEffect(() => {
    if (!selectedThreadId) return
    listRef.current
      ?.querySelector<HTMLElement>(
        `[data-team-comment-id="${CSS.escape(selectedThreadId)}"]`,
      )
      ?.scrollIntoView({ block: 'nearest' })
  }, [selectedThreadId])

  const navigate = (direction: -1 | 1) => {
    if (filtered.length === 0) return
    const index = selectedIndex < 0
      ? direction > 0 ? 0 : filtered.length - 1
      : (selectedIndex + direction + filtered.length) % filtered.length
    onSelectThread(filtered[index]?.id ?? null)
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="space-y-2 border-b border-border px-3 py-2">
        <div className="flex min-w-0 items-center gap-1">
          <div className="grid min-w-0 flex-1 grid-cols-2 gap-0.5 rounded-md bg-muted/60 p-0.5">
            <FilterButton
              active={filter === 'open'}
              count={counts.open}
              label={labels.open}
              onClick={() => setFilter('open')}
            />
            <FilterButton
              active={filter === 'resolved'}
              count={counts.resolved}
              label={labels.resolved}
              onClick={() => setFilter('resolved')}
            />
          </div>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                aria-label={labels.filter}
                className={cn(
                  filter !== 'open' && filter !== 'resolved' && 'text-brand',
                )}
                size="icon"
                type="button"
                variant="ghost"
              >
                <ListFilter className="icon-sm" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {([
                ['unread', labels.unread],
                ['mentions', labels.mentions],
                ['orphaned', labels.orphaned],
              ] as const).map(([id, label]) => (
                <DropdownMenuItem key={id} onSelect={() => setFilter(id)}>
                  {filter === id ? <Check className="icon-sm" /> : <span className="icon-sm" />}
                  <span className="min-w-0 flex-1">{label}</span>
                  <span className="t-hint tabular-nums">{counts[id]}</span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        <div className="flex min-w-0 items-center gap-1">
          <div className="relative min-w-0 flex-1">
            <Search className="icon-sm pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <Input
              aria-label={labels.search}
              className="h-8 pl-7"
              onChange={(event) => setQuery(event.target.value)}
              placeholder={labels.search}
              value={query}
            />
          </div>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button aria-label={labels.sort} size="icon" type="button" variant="ghost">
                <ArrowUpDown className="icon-sm" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => setSort('position')}>
                {sort === 'position' ? <Check className="icon-sm" /> : <span className="icon-sm" />}
                {labels.position}
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => setSort('newest')}>
                {sort === 'newest' ? <Check className="icon-sm" /> : <span className="icon-sm" />}
                {labels.newest}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
        <div className="flex h-7 items-center justify-between">
          <span className="t-meta-sm text-muted-foreground">
            {filtered.length}{' '}
            {filtered.length === 1
              ? labels.comment.toLocaleLowerCase(locale)
              : labels.comments.toLocaleLowerCase(locale)}
          </span>
          <div className="flex items-center gap-0.5">
            <Button
              aria-label={labels.previous}
              disabled={filtered.length === 0}
              onClick={() => navigate(-1)}
              size="icon"
              type="button"
              variant="ghost"
            >
              <ChevronLeft className="icon-sm" />
            </Button>
            <Button
              aria-label={labels.next}
              disabled={filtered.length === 0}
              onClick={() => navigate(1)}
              size="icon"
              type="button"
              variant="ghost"
            >
              <ChevronRight className="icon-sm" />
            </Button>
          </div>
        </div>
        {!canComment ? (
          <p className="t-meta-sm text-muted-foreground">{labels.viewOnly}</p>
        ) : null}
        {comments.error ? (
          <p className="t-meta-sm text-destructive" role="alert">
            {comments.error}
          </p>
        ) : null}
      </div>

      <ScrollArea className="min-h-0 flex-1">
        <div className="space-y-2 p-3" ref={listRef}>
          {comments.isLoading && comments.threads.length === 0 ? (
            <div className="flex items-center gap-2 py-8 text-muted-foreground">
              <LoaderCircle className="icon-sm animate-spin" />
              <span className="t-meta">{labels.comments}</span>
            </div>
          ) : filtered.length === 0 ? (
            <p className="t-meta py-8 text-center text-muted-foreground">
              {query
                ? labels.allFiltered
                : filter === 'open'
                  ? labels.allOpen
                  : filter === 'resolved'
                    ? labels.allResolved
                    : labels.allFiltered}
            </p>
          ) : visible.map((thread) => (
            <ThreadCard
              canComment={canComment}
              comments={comments}
              isOrphaned={orphanedThreadIds.has(thread.id)}
              isSelected={thread.id === selectedThreadId}
              isUnread={thread.revision > comments.lastReadRevision}
              key={thread.id}
              labels={labels}
              locale={locale}
              onSelect={() => onSelectThread(thread.id)}
              onUseWithAssistant={() => onUseWithAssistant(thread)}
              showAssistantAction={assistantAvailable}
              thread={thread}
            />
          ))}
          {visibleCount < filtered.length || comments.hasMore ? (
            <Button
              className="w-full"
              disabled={comments.isLoadingMore}
              onClick={() => {
                setVisibleCount((current) => current + 50)
                if (comments.hasMore) void comments.loadMore()
              }}
              size="sm"
              type="button"
              variant="ghost"
            >
              {comments.isLoadingMore
                ? <LoaderCircle className="icon-sm animate-spin" />
                : null}
              {labels.loadMore}
              <span className="t-hint tabular-nums">
                {comments.hasMore
                  ? '50+'
                  : Math.min(50, filtered.length - visibleCount)}
              </span>
            </Button>
          ) : null}
        </div>
      </ScrollArea>
    </div>
  )
}

function ThreadCard({
  canComment,
  comments,
  isOrphaned,
  isSelected,
  isUnread,
  labels,
  locale,
  onSelect,
  onUseWithAssistant,
  showAssistantAction,
  thread,
}: {
  canComment: boolean
  comments: CollaborationCommentsHandle
  isOrphaned: boolean
  isSelected: boolean
  isUnread: boolean
  labels: typeof copy.de | typeof copy.en
  locale: 'de' | 'en'
  onSelect: () => void
  onUseWithAssistant: () => void
  showAssistantAction: boolean
  thread: EditorCollaborationCommentThread
}) {
  const [mentions, setMentions] = useState<string[]>([])
  const draft = comments.drafts[thread.id] ?? ''
  const pending = comments.pendingIds.has(thread.id)
  const canReply = canComment && thread.status === 'open'

  const submitReply = () => {
    const value = draft.trim()
    if (!value || pending) return
    void comments.reply(thread.id, value, mentions).then(() => {
      setMentions([])
    })
  }

  return (
    <article
      aria-current={isSelected ? 'true' : undefined}
      className={cn(
        'rounded-lg border transition-colors',
        isSelected
          ? 'border-brand/45 bg-brand-subtle/20'
          : 'border-border/70 bg-card hover:border-border',
      )}
      data-team-comment-id={thread.id}
      onClick={onSelect}
    >
      <div className={cn('px-2.5 py-2', isSelected ? 'space-y-2' : 'space-y-1')}>
        <div className="flex items-start gap-2">
          <Avatar actor={thread.author} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5">
              <span className="t-list truncate">{thread.author.name}</span>
              <span className="t-hint text-muted-foreground">
                {relativeTime(thread.created_at, locale)}
              </span>
              {isUnread ? (
                <span
                  aria-label={labels.unread}
                  className="size-1.5 shrink-0 rounded-full bg-brand"
                />
              ) : null}
            </div>
            {isOrphaned ? (
              <span className="t-meta-sm inline-flex items-center gap-1 text-warning">
                <Link className="icon-xs" />
                {labels.orphaned}
              </span>
            ) : null}
          </div>
          {showAssistantAction || thread.can_resolve || thread.can_delete ? <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                aria-label={labels.actions}
                onClick={(event) => event.stopPropagation()}
                size="icon"
                type="button"
                variant="ghost"
              >
                <MoreHorizontal className="icon-sm" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              {showAssistantAction ? (
                <DropdownMenuItem onSelect={onUseWithAssistant}>
                  <Sparkles className="icon-sm" />
                  {labels.useWithAssistant}
                </DropdownMenuItem>
              ) : null}
              {thread.can_resolve ? (
                <DropdownMenuItem
                  disabled={pending}
                  onSelect={() => void comments.setStatus(
                    thread.id,
                    thread.status === 'open' ? 'resolved' : 'open',
                  )}
                >
                  {thread.status === 'open'
                    ? <Check className="icon-sm" />
                    : <RotateCcw className="icon-sm" />}
                  {thread.status === 'open' ? labels.resolve : labels.reopen}
                </DropdownMenuItem>
              ) : null}
              {/* Resolving hides a discussion that happened; deleting is for
                  one that was never meant to exist — a card added by accident.
                  Only the author sees it: unlike resolving, it takes other
                  people's replies out of view. */}
              {thread.can_delete ? (
                <DropdownMenuItem
                  className="text-destructive"
                  disabled={pending}
                  onSelect={() => void comments.deleteThread(thread.id)}
                >
                  <Trash2 className="icon-sm" />
                  {labels.deleteThread}
                </DropdownMenuItem>
              ) : null}
            </DropdownMenuContent>
          </DropdownMenu> : null}
        </div>

        <blockquote className={cn(
          't-meta-sm border-l border-foreground/20 pl-2 text-muted-foreground',
          isSelected ? 'line-clamp-3' : 'line-clamp-1',
        )}>
          {thread.quote || '—'}
        </blockquote>

        <div className={cn(isSelected && 'space-y-2')}>
          {(isSelected ? thread.messages : thread.messages.slice(0, 1)).map((message, index) => (
            <CommentMessage
              comments={comments}
              isReply={index > 0}
              key={message.id}
              labels={labels}
              locale={locale}
              message={message}
              thread={thread}
              truncate={!isSelected}
            />
          ))}
          {!isSelected && thread.messages.length > 1 ? (
            <p className="t-hint mt-1 text-muted-foreground">
              {thread.messages.length - 1}{' '}
              {thread.messages.length === 2
                ? labels.oneReply.toLocaleLowerCase(locale)
                : labels.replies.toLocaleLowerCase(locale)}
            </p>
          ) : null}
        </div>

        {isSelected && canReply ? (
          <div className="space-y-1.5 border-t border-border pt-2">
            {/* One line that grows, with the return glyph stating what the
                key already does — the same shape as the inline composer. */}
            <div className="relative">
              <Textarea
                aria-label={labels.reply}
                className="t-body min-h-0 resize-none bg-background py-1.5 pr-16"
                onChange={(event) => comments.setDraft(thread.id, event.target.value)}
                onFocus={onSelect}
                onKeyDown={(event) => {
                  if (
                    event.key === 'Enter'
                    && !event.shiftKey
                    && !event.nativeEvent.isComposing
                  ) {
                    event.preventDefault()
                    submitReply()
                  }
                }}
                placeholder={labels.reply}
                rows={1}
                value={draft}
              />
              <div className="absolute inset-y-0 right-1.5 z-10 flex items-start gap-0.5 py-1">
                <MentionPicker
                  labels={labels}
                  onChange={setMentions}
                  participants={comments.participants}
                  selected={mentions}
                />
                <button
                  aria-label={labels.send}
                  className="inline-grid size-6 shrink-0 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-muted hover:text-foreground disabled:opacity-40 disabled:hover:bg-transparent"
                  disabled={!draft.trim() || pending}
                  onClick={submitReply}
                  type="button"
                >
                  {pending
                    ? <LoaderCircle className="icon-sm animate-spin" />
                    : <CornerDownLeft className="size-3.5" />}
                </button>
              </div>
            </div>
            {/* The chosen names only when there are any: an empty mention row
                would cost a whole line under a one-line box. */}
            {mentionNames(mentions, comments.participants) ? (
              <p className="t-hint truncate text-muted-foreground">
                {mentionNames(mentions, comments.participants)}
              </p>
            ) : null}
          </div>
        ) : null}
      </div>
    </article>
  )
}

function CommentMessage({
  comments,
  isReply,
  labels,
  locale,
  message,
  thread,
  truncate,
}: {
  comments: CollaborationCommentsHandle
  isReply: boolean
  labels: typeof copy.de | typeof copy.en
  locale: 'de' | 'en'
  message: EditorCollaborationCommentMessage
  thread: EditorCollaborationCommentThread
  truncate: boolean
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(message.body_markdown ?? '')
  const pending = comments.pendingIds.has(message.id)

  if (message.deleted_at !== null) {
    return (
      <div className={cn('t-meta-sm italic text-muted-foreground', isReply && 'ml-7')}>
        {labels.deleted}
      </div>
    )
  }

  return (
    <div className={cn('group/message space-y-1', isReply && 'ml-7 border-l border-border pl-2')}>
      {isReply ? (
        <div className="flex items-center gap-1.5">
          <Avatar actor={message.author} compact />
          <span className="t-meta-sm font-medium">{message.author.name}</span>
          <span className="t-hint text-muted-foreground">
            {relativeTime(message.created_at, locale)}
          </span>
        </div>
      ) : null}
      {editing ? (
        <div className="space-y-1.5">
          <Textarea
            autoFocus
            className="t-body min-h-16 resize-none"
            onChange={(event) => setDraft(event.target.value)}
            value={draft}
          />
          <div className="flex justify-end gap-1">
            <Button
              onClick={() => {
                setDraft(message.body_markdown ?? '')
                setEditing(false)
              }}
              size="sm"
              type="button"
              variant="ghost"
            >
              {labels.cancel}
            </Button>
            <Button
              disabled={!draft.trim() || pending}
              onClick={() => void comments.editMessage(
                thread.id,
                message.id,
                draft,
                message.mentions
                  .map((mention) => mention.id)
                  .filter((id): id is string => id !== null),
              ).then(() => setEditing(false))}
              size="sm"
              type="button"
            >
              {labels.save}
            </Button>
          </div>
        </div>
      ) : (
        <div className="space-y-1">
          <div className="flex items-start gap-1">
            <p className={cn(
              't-body min-w-0 flex-1 whitespace-pre-wrap break-words',
              truncate && 'line-clamp-2',
            )}>
              {message.body_markdown}
            </p>
            {/* Editing and deleting a message are shown outright rather than
                behind a trigger that only appears once the mouse crosses the
                message inside an already-opened thread: an action nobody can
                find is an action nobody has. The server decides WHO may —
                `can_edit`/`can_delete` are author-only. */}
            {!truncate && (message.can_edit || message.can_delete) ? (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    aria-label={labels.messageActions}
                    className="size-6 shrink-0 text-muted-foreground hover:text-foreground"
                    size="icon"
                    type="button"
                    variant="ghost"
                  >
                    <MoreHorizontal className="icon-sm" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end">
                  {message.can_edit ? (
                    <DropdownMenuItem onSelect={() => setEditing(true)}>
                      <PencilLine className="icon-sm" />
                      {labels.edit}
                    </DropdownMenuItem>
                  ) : null}
                  {message.can_delete ? (
                    <DropdownMenuItem
                      className="text-destructive"
                      disabled={pending}
                      onSelect={() => void comments.deleteMessage(
                        thread.id,
                        message.id,
                      )}
                    >
                      <Trash2 className="icon-sm" />
                      {labels.delete}
                    </DropdownMenuItem>
                  ) : null}
                </DropdownMenuContent>
              </DropdownMenu>
            ) : null}
          </div>
          {!truncate && message.mentions.length > 0 ? (
            <div className="flex flex-wrap gap-1" aria-label={labels.mention}>
              {message.mentions.map((mention) => (
                <span
                  className="t-hint rounded-full bg-brand-subtle px-1.5 py-0.5 text-brand"
                  key={mention.id ?? mention.name}
                >
                  @{mention.name}
                </span>
              ))}
            </div>
          ) : null}
        </div>
      )}
      {!truncate && message.edited_at !== null && !editing ? (
        <span className="t-hint text-muted-foreground">{labels.edited}</span>
      ) : null}
    </div>
  )
}

function MentionPicker({
  labels,
  onChange,
  participants,
  selected,
}: {
  labels: typeof copy.de | typeof copy.en
  onChange: (selected: string[]) => void
  participants: readonly EditorCollaborationCommentActor[]
  selected: readonly string[]
}) {
  return (
    <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            aria-label={labels.mention}
            className="size-6 shrink-0 text-muted-foreground hover:text-foreground"
            size="icon"
            type="button"
            variant="ghost"
          >
            <AtSign className="icon-sm" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          {participants.length === 0 ? (
            <DropdownMenuItem disabled>{labels.noParticipants}</DropdownMenuItem>
          ) : participants.map((participant) => {
            if (!participant.id) return null
            const active = selected.includes(participant.id)
            return (
              <DropdownMenuItem
                key={participant.id}
                onSelect={(event) => {
                  event.preventDefault()
                  onChange(active
                    ? selected.filter((id) => id !== participant.id)
                    : [...selected, participant.id!])
                }}
              >
                <Avatar actor={participant} compact />
                {participant.name}
                {active ? <Check className="ml-auto icon-sm" /> : null}
              </DropdownMenuItem>
            )
          })}
        </DropdownMenuContent>
      </DropdownMenu>
  )
}

/** The chosen mentions as one line, empty when nothing is chosen. */
function mentionNames(
  selected: readonly string[],
  participants: readonly EditorCollaborationCommentActor[],
): string {
  return selected
    .map((id) => participants.find((participant) => participant.id === id)?.name)
    .filter(Boolean)
    .map((name) => `@${name}`)
    .join(' ')
}

function Avatar({
  actor,
  compact = false,
}: {
  actor: EditorCollaborationCommentActor
  compact?: boolean
}) {
  return (
    <span
      aria-hidden
      className={cn(
        't-hint flex shrink-0 items-center justify-center rounded-full font-medium text-white',
        compact ? 'size-5' : 'size-7',
      )}
      style={{ backgroundColor: actorColor(actor.id ?? actor.name) }}
    >
      {initials(actor.name)}
    </span>
  )
}

function FilterButton({
  active,
  count,
  label,
  onClick,
}: {
  active: boolean
  count: number
  label: string
  onClick: () => void
}) {
  return (
    <button
      aria-pressed={active}
      className={cn(
        't-meta-sm flex h-7 items-center justify-center gap-1 rounded-[5px] px-2',
        active
          ? 'bg-background text-foreground shadow-sm'
          : 'text-muted-foreground hover:text-foreground',
      )}
      onClick={onClick}
      type="button"
    >
      {label}
      <span className="t-hint tabular-nums">{count}</span>
    </button>
  )
}

function relativeTime(timestamp: number, locale: 'de' | 'en'): string {
  const seconds = Math.round(timestamp - Date.now() / 1_000)
  const formatter = new Intl.RelativeTimeFormat(locale, { numeric: 'auto' })
  if (Math.abs(seconds) < 60) return formatter.format(seconds, 'second')
  const minutes = Math.round(seconds / 60)
  if (Math.abs(minutes) < 60) return formatter.format(minutes, 'minute')
  const hours = Math.round(minutes / 60)
  if (Math.abs(hours) < 24) return formatter.format(hours, 'hour')
  return formatter.format(Math.round(hours / 24), 'day')
}

function initials(name: string): string {
  return name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0]?.toLocaleUpperCase() ?? '')
    .join('') || '?'
}

function actorColor(value: string): string {
  let hash = 0
  for (const character of value) {
    hash = ((hash << 5) - hash + character.charCodeAt(0)) | 0
  }
  return `hsl(${Math.abs(hash) % 360} 48% 42%)`
}
