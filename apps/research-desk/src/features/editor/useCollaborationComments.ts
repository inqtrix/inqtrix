import { createSecureUuid } from '@inqtrix/editor-schema'
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'

import {
  createEditorCollaborationComment,
  createGuestEditorCollaborationComment,
  deleteEditorCollaborationCommentMessage,
  deleteGuestEditorCollaborationCommentMessage,
  listEditorCollaborationComments,
  listGuestEditorCollaborationComments,
  markEditorCollaborationCommentsRead,
  markGuestEditorCollaborationCommentsRead,
  replyToEditorCollaborationComment,
  replyToGuestEditorCollaborationComment,
  updateEditorCollaborationCommentMessage,
  updateGuestEditorCollaborationCommentMessage,
  deleteEditorCollaborationCommentThread,
  updateEditorCollaborationCommentThread,
  updateGuestEditorCollaborationCommentThread,
  type ClientOptions,
  type EditorCollaborationCommentActor,
  type EditorCollaborationCommentMutation,
  type EditorCollaborationCommentThread,
  type InqtrixRequestError,
} from '@/api/inqtrixClient'

const COMMENT_PAGE_SIZE = 50
const COMMENT_PAGE_LIMIT = 20
export const COMMENT_REFRESH_DEBOUNCE_MS = 500
const COMMENT_DRAFT_PREFIX = 'inqtrix:collaboration-comment-drafts:v1'
const GUEST_COMMENT_DRAFT_PREFIX = `${COMMENT_DRAFT_PREFIX}:guest:`

/** Whose unsent comment drafts of one document generation are meant: a
 * signed-in user's, per workspace, or one guest's, per guest identity. */
export type CollaborationCommentDraftScope = {
  documentId: string
  generation: number
} & ({ guestId: string } | { workspaceId: string })

/** A guest's drafts are keyed by the guest identity, never by the document
 * alone: several guests can open the same document in one browser, one after
 * another, and none may read what another left unsent. */
export function collaborationCommentDraftDocumentKey(
  scope: CollaborationCommentDraftScope,
): string {
  const owner = 'guestId' in scope ? `guest:${scope.guestId}` : scope.workspaceId
  return `${owner}:${scope.documentId}:g${scope.generation}`
}

export function clearCollaborationCommentDrafts(
  scope: CollaborationCommentDraftScope,
): void {
  try {
    globalThis.localStorage?.removeItem(
      `${COMMENT_DRAFT_PREFIX}:${collaborationCommentDraftDocumentKey(scope)}`,
    )
  } catch {
    // Best-effort local recovery hygiene; the in-memory surface is retired
    // independently and must not depend on storage availability.
  }
}

/**
 * Deletes the unsent comment drafts of every guest but `currentGuestId`.
 *
 * A browser holds one guest session at a time, and opening a link replaces
 * it; a new session is a new guest identity. Once a guest session holds this
 * browser, any other guest's drafts belong to an access that has ended here,
 * also one whose end the page never saw (closed before the link expired or
 * was revoked). Drafts under a key without a guest identity are swept the
 * same way.
 */
export function clearOtherGuestsCommentDrafts(currentGuestId: string): void {
  try {
    const store = globalThis.localStorage
    if (!store) return
    const current = `${GUEST_COMMENT_DRAFT_PREFIX}${currentGuestId}:`
    const ended: string[] = []
    for (let index = 0; index < store.length; index += 1) {
      const key = store.key(index)
      if (key?.startsWith(GUEST_COMMENT_DRAFT_PREFIX) && !key.startsWith(current)) {
        ended.push(key)
      }
    }
    for (const key of ended) store.removeItem(key)
  } catch {
    // Storage that refuses to work holds no drafts to delete.
  }
}

/**
 * Where the incremental comment cursor stands after one of our own comment
 * changes committed at `mutationRevision`.
 *
 * Every comment change takes the document's next comment revision, so ours
 * directly after the cursor proves that nothing else committed in between,
 * and the cursor moves to it. A larger revision means somebody else's change
 * committed first, for instance a thread another person started while we
 * wrote ours: the cursor stays, because every later incremental page starts
 * after it and would never carry that change, and `missed` asks the caller
 * to load from the cursor. An older revision, the answer to a replayed
 * command, leaves the cursor where it is.
 */
export function commentCursorAfterMutation(
  cursor: number,
  mutationRevision: number,
): { cursor: number; missed: boolean } {
  if (mutationRevision === cursor + 1) {
    return { cursor: mutationRevision, missed: false }
  }
  return { cursor, missed: mutationRevision > cursor + 1 }
}

export type CollaborationCommentDrafts = Record<string, string>

export type CollaborationCommentsHandle = {
  createThread: (input: {
    anchor: Record<string, unknown>
    bodyMarkdown: string
    mentionUserIds?: string[]
    quote: string
  }) => Promise<EditorCollaborationCommentThread>
  deleteMessage: (threadId: string, messageId: string) => Promise<void>
  drafts: CollaborationCommentDrafts
  editMessage: (
    threadId: string,
    messageId: string,
    bodyMarkdown: string,
    mentionUserIds?: string[],
  ) => Promise<void>
  error: string | null
  hasMore: boolean
  isLoading: boolean
  isLoadingMore: boolean
  lastReadRevision: number
  loadMore: () => Promise<void>
  markRead: () => Promise<void>
  mentionEventVersion: number
  participants: readonly EditorCollaborationCommentActor[]
  pendingIds: ReadonlySet<string>
  reply: (
    threadId: string,
    bodyMarkdown: string,
    mentionUserIds?: string[],
  ) => Promise<void>
  revision: number
  setDraft: (key: string, value: string) => void
  deleteThread: (threadId: string) => Promise<void>
  setStatus: (
    threadId: string,
    status: 'open' | 'resolved',
  ) => Promise<void>
  threads: readonly EditorCollaborationCommentThread[]
  unreadCount: number
}

export type UseCollaborationCommentsOptions = {
  active: boolean
  apiKey: string | undefined
  documentId: string | null
  eventVersion: number
  generation: number | null
  /** The guest identity of a guest session; absent for a signed-in user. */
  guestId?: string
  initialRevision: number
  locale: 'de' | 'en'
  mentionEventVersion: number
  workspaceId: string
}

export function useCollaborationComments({
  active,
  apiKey,
  documentId,
  eventVersion,
  generation,
  guestId,
  initialRevision,
  locale,
  mentionEventVersion,
  workspaceId,
}: UseCollaborationCommentsOptions): CollaborationCommentsHandle {
  const guest = guestId !== undefined
  const clientOptions = useMemo<ClientOptions>(
    () => (guest ? {} : { apiKey, workspaceId }),
    [apiKey, guest, workspaceId],
  )
  const [threads, setThreads] = useState<
    EditorCollaborationCommentThread[]
  >([])
  const [participants, setParticipants] = useState<
    EditorCollaborationCommentActor[]
  >([])
  const [revision, setRevision] = useState(0)
  const [lastReadRevision, setLastReadRevision] = useState(0)
  const [isLoading, setIsLoading] = useState(false)
  const [isLoadingMore, setIsLoadingMore] = useState(false)
  const [hasMore, setHasMore] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pendingIds, setPendingIds] = useState<ReadonlySet<string>>(
    () => new Set(),
  )
  const [drafts, setDrafts] = useState<CollaborationCommentDrafts>({})
  const revisionRef = useRef(0)
  const pageCursorRef = useRef(0)
  const threadsRef = useRef<EditorCollaborationCommentThread[]>([])
  const loadInFlightRef = useRef<Promise<void> | null>(null)
  const loadMoreInFlightRef = useRef<Promise<void> | null>(null)
  const refreshRequestedRef = useRef(false)
  const refreshTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const loadedDocumentKeyRef = useRef<string | null>(null)
  const activeRef = useRef(active)
  activeRef.current = active
  const documentKey = active && documentId && generation !== null
    ? collaborationCommentDraftDocumentKey(
        guestId !== undefined
          ? { documentId, generation, guestId }
          : { documentId, generation, workspaceId },
      )
    : null

  const publishThreads = useCallback((
    next: EditorCollaborationCommentThread[],
  ) => {
    const ordered = [...next].sort((left, right) => (
      right.updated_at - left.updated_at || left.id.localeCompare(right.id)
    ))
    threadsRef.current = ordered
    setThreads(ordered)
  }, [])

  const mergeThreads = useCallback((
    changed: readonly EditorCollaborationCommentThread[],
    replace = false,
  ) => {
    const byId = new Map(
      (replace ? [] : threadsRef.current).map((thread) => [thread.id, thread]),
    )
    for (const thread of changed) {
      // A retired thread reaches us exactly once, on the incremental page
      // that follows its deletion — that page is the only chance to drop a
      // card we already hold, so it removes rather than merges. This is the
      // single place that decides it, for the poll and for our own mutation
      // alike.
      if (thread.deleted_at !== null) byId.delete(thread.id)
      else byId.set(thread.id, thread)
    }
    publishThreads([...byId.values()])
  }, [publishThreads])

  const fetchComments = useCallback(async (replace: boolean) => {
    if (!activeRef.current || !documentId || generation === null) return
    let cursor = replace ? 0 : revisionRef.current
    let pageCount = 0
    do {
      const query = {
        limit: COMMENT_PAGE_SIZE,
        sinceRevision: cursor,
        status: 'all' as const,
      }
      const page = guest
        ? await listGuestEditorCollaborationComments(query, clientOptions)
        : await listEditorCollaborationComments(
            documentId,
            query,
            clientOptions,
          )
      if (!activeRef.current) return
      mergeThreads(page.data, replace && pageCount === 0)
      setParticipants(page.participants ?? [])
      setLastReadRevision(page.last_read_revision)
      const currentRevision = page.current_revision ?? page.revision
      revisionRef.current = currentRevision
      setRevision(currentRevision)
      pageCount += 1
      if (replace) {
        pageCursorRef.current = page.revision
        setHasMore(page.has_more === true)
        break
      }
      if (page.revision <= cursor || page.has_more !== true) break
      cursor = page.revision
    } while (pageCount < COMMENT_PAGE_LIMIT)
  }, [clientOptions, documentId, generation, guest, mergeThreads])

  const requestRefresh = useCallback((replace = false): Promise<void> => {
    refreshRequestedRef.current = true
    if (replace) revisionRef.current = 0
    if (loadInFlightRef.current) return loadInFlightRef.current
    const load = (async () => {
      setIsLoading(true)
      try {
        let replaceNext = replace
        while (refreshRequestedRef.current && activeRef.current) {
          refreshRequestedRef.current = false
          await fetchComments(replaceNext)
          replaceNext = false
        }
        setError(null)
      } catch (loadError) {
        setError(commentErrorMessage(loadError, locale))
      } finally {
        setIsLoading(false)
        loadInFlightRef.current = null
      }
    })()
    loadInFlightRef.current = load
    return load
  }, [fetchComments, locale])

  useEffect(() => {
    if (refreshTimerRef.current !== null) {
      globalThis.clearTimeout(refreshTimerRef.current)
      refreshTimerRef.current = null
    }
    activeRef.current = Boolean(documentKey)
    if (!documentKey) {
      loadedDocumentKeyRef.current = null
      revisionRef.current = 0
      pageCursorRef.current = 0
      threadsRef.current = []
      setThreads([])
      setParticipants([])
      setRevision(0)
      setHasMore(false)
      setLastReadRevision(0)
      setError(null)
      setDrafts({})
      return
    }
    if (loadedDocumentKeyRef.current === documentKey) return
    loadedDocumentKeyRef.current = documentKey
    revisionRef.current = 0
    pageCursorRef.current = 0
    threadsRef.current = []
    setThreads([])
    setParticipants([])
    setRevision(initialRevision)
    setHasMore(false)
    setDrafts(readDrafts(documentKey))
    void requestRefresh(true)
  }, [documentKey, initialRevision, requestRefresh])

  useEffect(() => {
    if (!documentKey || loadedDocumentKeyRef.current !== documentKey) return
    if (eventVersion === 0 && revisionRef.current === 0) return
    if (refreshTimerRef.current !== null) return
    refreshTimerRef.current = globalThis.setTimeout(() => {
      refreshTimerRef.current = null
      void requestRefresh(false)
    }, COMMENT_REFRESH_DEBOUNCE_MS)
  }, [documentKey, eventVersion, requestRefresh])

  useEffect(() => () => {
    if (refreshTimerRef.current !== null) {
      globalThis.clearTimeout(refreshTimerRef.current)
      refreshTimerRef.current = null
    }
  }, [])

  const loadMore = useCallback((): Promise<void> => {
    if (
      !activeRef.current
      || !documentId
      || generation === null
      || !hasMore
    ) return Promise.resolve()
    if (loadMoreInFlightRef.current) return loadMoreInFlightRef.current
    const load = (async () => {
      setIsLoadingMore(true)
      setError(null)
      try {
        const query = {
          limit: COMMENT_PAGE_SIZE,
          sinceRevision: pageCursorRef.current,
          status: 'all' as const,
        }
        const page = guest
          ? await listGuestEditorCollaborationComments(query, clientOptions)
          : await listEditorCollaborationComments(
              documentId,
              query,
              clientOptions,
            )
        if (!activeRef.current) return
        mergeThreads(page.data)
        setParticipants(page.participants ?? [])
        setLastReadRevision(page.last_read_revision)
        pageCursorRef.current = Math.max(
          pageCursorRef.current,
          page.revision,
        )
        const currentRevision = page.current_revision ?? page.revision
        revisionRef.current = Math.max(
          revisionRef.current,
          currentRevision,
        )
        setRevision(revisionRef.current)
        setHasMore(page.has_more === true)
      } catch (loadError) {
        setError(commentErrorMessage(loadError, locale))
      } finally {
        setIsLoadingMore(false)
        loadMoreInFlightRef.current = null
      }
    })()
    loadMoreInFlightRef.current = load
    return load
  }, [
    clientOptions,
    documentId,
    generation,
    guest,
    hasMore,
    locale,
    mergeThreads,
  ])

  const setDraft = useCallback((key: string, value: string) => {
    if (!documentKey) return
    setDrafts((current) => {
      const next = { ...current }
      if (value) next[key] = value
      else delete next[key]
      writeDrafts(documentKey, next)
      return next
    })
  }, [documentKey])

  const withPending = useCallback(async <T,>(
    key: string,
    operation: () => Promise<T>,
  ): Promise<T> => {
    setPendingIds((current) => new Set(current).add(key))
    setError(null)
    try {
      return await operation()
    } catch (mutationError) {
      if (requestStatus(mutationError) === 409) {
        await requestRefresh(true)
      }
      const message = commentErrorMessage(mutationError, locale)
      setError(message)
      throw new Error(message, { cause: mutationError })
    } finally {
      setPendingIds((current) => {
        const next = new Set(current)
        next.delete(key)
        return next
      })
    }
  }, [locale, requestRefresh])

  const adoptMutation = useCallback((
    mutation: EditorCollaborationCommentMutation,
  ) => {
    mergeThreads([mutation.thread])
    const next = commentCursorAfterMutation(
      revisionRef.current,
      mutation.revision,
    )
    revisionRef.current = next.cursor
    setRevision(revisionRef.current)
    if (next.missed) void requestRefresh(false)
  }, [mergeThreads, requestRefresh])

  const createThread = useCallback(async ({
    anchor,
    bodyMarkdown,
    mentionUserIds = [],
    quote,
  }: {
    anchor: Record<string, unknown>
    bodyMarkdown: string
    mentionUserIds?: string[]
    quote: string
  }) => {
    if (!documentId || generation === null) {
      throw new Error(commentErrorCopy[locale].unavailable)
    }
    const threadId = createSecureUuid()
    return withPending(threadId, async () => {
      const command = {
        anchor,
        body_markdown: bodyMarkdown,
        command_id: createSecureUuid(),
        expected_revision: revisionRef.current,
        generation,
        mention_user_ids: mentionUserIds,
        message_id: createSecureUuid(),
        quote,
        thread_id: threadId,
      }
      const mutation = await retryLostResponse(() => (
        guest
          ? createGuestEditorCollaborationComment(command, clientOptions)
          : createEditorCollaborationComment(
              documentId,
              command,
              clientOptions,
            )
      ))
      adoptMutation(mutation)
      setDraft('new', '')
      return mutation.thread
    })
  }, [
    adoptMutation,
    clientOptions,
    documentId,
    generation,
    guest,
    locale,
    setDraft,
    withPending,
  ])

  const reply = useCallback(async (
    threadId: string,
    bodyMarkdown: string,
    mentionUserIds: string[] = [],
  ) => {
    if (!documentId || generation === null) {
      throw new Error(commentErrorCopy[locale].unavailable)
    }
    const thread = threadsRef.current.find((item) => item.id === threadId)
    if (!thread) throw new Error(commentErrorCopy[locale].threadUnavailable)
    await withPending(threadId, async () => {
      const command = {
        body_markdown: bodyMarkdown,
        command_id: createSecureUuid(),
        expected_revision: thread.revision,
        generation,
        mention_user_ids: mentionUserIds,
        message_id: createSecureUuid(),
      }
      const mutation = await retryLostResponse(() => (
        guest
          ? replyToGuestEditorCollaborationComment(
              threadId,
              command,
              clientOptions,
            )
          : replyToEditorCollaborationComment(
              documentId,
              threadId,
              command,
              clientOptions,
            )
      ))
      adoptMutation(mutation)
      setDraft(threadId, '')
    })
  }, [
    adoptMutation,
    clientOptions,
    documentId,
    generation,
    guest,
    locale,
    setDraft,
    withPending,
  ])

  const editMessage = useCallback(async (
    threadId: string,
    messageId: string,
    bodyMarkdown: string,
    mentionUserIds: string[] = [],
  ) => {
    if (!documentId || generation === null) {
      throw new Error(commentErrorCopy[locale].unavailable)
    }
    const thread = threadsRef.current.find((item) => item.id === threadId)
    if (!thread) throw new Error(commentErrorCopy[locale].threadUnavailable)
    await withPending(messageId, async () => {
      const command = {
        body_markdown: bodyMarkdown,
        command_id: createSecureUuid(),
        expected_revision: thread.revision,
        generation,
        mention_user_ids: mentionUserIds,
      }
      const mutation = await retryLostResponse(() => (
        guest
          ? updateGuestEditorCollaborationCommentMessage(
              threadId,
              messageId,
              command,
              clientOptions,
            )
          : updateEditorCollaborationCommentMessage(
              documentId,
              threadId,
              messageId,
              command,
              clientOptions,
            )
      ))
      adoptMutation(mutation)
    })
  }, [adoptMutation, clientOptions, documentId, generation, guest, locale, withPending])

  const deleteMessage = useCallback(async (
    threadId: string,
    messageId: string,
  ) => {
    if (!documentId || generation === null) {
      throw new Error(commentErrorCopy[locale].unavailable)
    }
    const thread = threadsRef.current.find((item) => item.id === threadId)
    if (!thread) throw new Error(commentErrorCopy[locale].threadUnavailable)
    await withPending(messageId, async () => {
      const command = {
        command_id: createSecureUuid(),
        expected_revision: thread.revision,
        generation,
      }
      const mutation = await retryLostResponse(() => (
        guest
          ? deleteGuestEditorCollaborationCommentMessage(
              threadId,
              messageId,
              command,
              clientOptions,
            )
          : deleteEditorCollaborationCommentMessage(
              documentId,
              threadId,
              messageId,
              command,
              clientOptions,
            )
      ))
      adoptMutation(mutation)
    })
  }, [adoptMutation, clientOptions, documentId, generation, guest, locale, withPending])

  const setStatus = useCallback(async (
    threadId: string,
    status: 'open' | 'resolved',
  ) => {
    if (!documentId || generation === null) {
      throw new Error(commentErrorCopy[locale].unavailable)
    }
    const thread = threadsRef.current.find((item) => item.id === threadId)
    if (!thread) throw new Error(commentErrorCopy[locale].threadUnavailable)
    await withPending(threadId, async () => {
      const command = {
        command_id: createSecureUuid(),
        expected_revision: thread.revision,
        generation,
        status,
      }
      const mutation = await retryLostResponse(() => (
        guest
          ? updateGuestEditorCollaborationCommentThread(
              threadId,
              command,
              clientOptions,
            )
          : updateEditorCollaborationCommentThread(
              documentId,
              threadId,
              command,
              clientOptions,
            )
      ))
      adoptMutation(mutation)
    })
  }, [adoptMutation, clientOptions, documentId, generation, guest, locale, withPending])

  /** Retire a whole thread the current user started by accident. Resolving
   * hides a discussion that happened; this is for one that never should have
   * existed. The server admits only the author. */
  const deleteThread = useCallback(async (threadId: string) => {
    if (!documentId || generation === null) {
      throw new Error(commentErrorCopy[locale].unavailable)
    }
    const thread = threadsRef.current.find((item) => item.id === threadId)
    if (!thread) throw new Error(commentErrorCopy[locale].threadUnavailable)
    await withPending(threadId, async () => {
      const mutation = await retryLostResponse(() => (
        deleteEditorCollaborationCommentThread(
          documentId,
          threadId,
          {
            command_id: createSecureUuid(),
            expected_revision: thread.revision,
            generation,
          },
          clientOptions,
        )
      ))
      adoptMutation(mutation)
    })
  }, [adoptMutation, clientOptions, documentId, generation, locale, withPending])

  const markRead = useCallback(async () => {
    if (
      !documentId
      || generation === null
      || revisionRef.current <= lastReadRevision
    ) return
    try {
      const result = guest
        ? await markGuestEditorCollaborationCommentsRead(
            revisionRef.current,
            clientOptions,
          )
        : await markEditorCollaborationCommentsRead(
            documentId,
            { generation, revision: revisionRef.current },
            clientOptions,
          )
      setLastReadRevision(result.last_read_revision)
    } catch (markError) {
      setError(commentErrorMessage(markError, locale))
    }
  }, [
    clientOptions,
    documentId,
    generation,
    guest,
    lastReadRevision,
    locale,
  ])

  const unreadCount = threads.reduce(
    (count, thread) => count + Number(thread.revision > lastReadRevision),
    0,
  )

  return {
    createThread,
    deleteMessage,
    drafts,
    editMessage,
    error,
    hasMore,
    isLoading,
    isLoadingMore,
    deleteThread,
    lastReadRevision,
    loadMore,
    markRead,
    mentionEventVersion,
    participants,
    pendingIds,
    reply,
    revision,
    setDraft,
    setStatus,
    threads,
    unreadCount,
  }
}

async function retryLostResponse<T>(operation: () => Promise<T>): Promise<T> {
  try {
    return await operation()
  } catch (error) {
    if (requestStatus(error) !== undefined) throw error
    return operation()
  }
}

function requestStatus(error: unknown): number | undefined {
  return error instanceof Error
    ? (error as InqtrixRequestError).status
    : undefined
}

const commentErrorCopy = {
  de: {
    conflict: 'Diese Diskussion wurde zwischenzeitlich geändert und neu geladen.',
    forbidden: 'Sie dürfen diesen Kommentar nicht ändern.',
    generic: 'Die Team-Kommentare konnten nicht aktualisiert werden.',
    threadUnavailable: 'Diese Diskussion ist nicht mehr verfügbar.',
    unavailable: 'Team-Kommentare sind momentan nicht verfügbar.',
  },
  en: {
    conflict: 'This discussion changed elsewhere and was refreshed.',
    forbidden: 'You do not have permission to change this comment.',
    generic: 'Shared comments could not be updated.',
    threadUnavailable: 'The comment thread is no longer available.',
    unavailable: 'Shared comments are currently unavailable.',
  },
} as const

function commentErrorMessage(error: unknown, locale: 'de' | 'en'): string {
  const labels = commentErrorCopy[locale]
  if (!(error instanceof Error)) return labels.generic
  const status = requestStatus(error)
  if (status === 403) return labels.forbidden
  if (status === 409) return labels.conflict
  return error.message || labels.generic
}

function readDrafts(documentKey: string): CollaborationCommentDrafts {
  try {
    const raw = globalThis.localStorage?.getItem(
      `${COMMENT_DRAFT_PREFIX}:${documentKey}`,
    )
    if (!raw) return {}
    const parsed = JSON.parse(raw) as unknown
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    return Object.fromEntries(
      Object.entries(parsed).filter((entry): entry is [string, string] => (
        typeof entry[1] === 'string'
      )),
    )
  } catch {
    return {}
  }
}

function writeDrafts(
  documentKey: string,
  drafts: CollaborationCommentDrafts,
): void {
  try {
    const storageKey = `${COMMENT_DRAFT_PREFIX}:${documentKey}`
    if (Object.keys(drafts).length === 0) {
      globalThis.localStorage?.removeItem(storageKey)
      return
    }
    globalThis.localStorage?.setItem(storageKey, JSON.stringify(drafts))
  } catch {
    // Storage is best-effort. The in-memory draft remains available.
  }
}
