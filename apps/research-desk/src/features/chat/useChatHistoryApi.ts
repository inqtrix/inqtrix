/**
 * Chat-history server sync (project-persistence tier).
 *
 * The chat analogue of useResearchRunApi (resume-on-mount) + useTemplateSync
 * (local<->server reconciliation), but persistence-only: it never calls a
 * model. The LLM submission path in ResearchDesk is untouched — this hook
 * is a PARALLEL layer that keeps the conversation record on the server in
 * step with the local reducer, exactly the way template sync runs beside
 * how rules are used.
 *
 * Three behaviours, all gated on the durable ``project_persistence``
 * capability AND ``serverSyncEnabled`` (automatic for an authenticated
 * cookie-session user — derived from the session in ResearchDesk; the import
 * button only sets it for the apikey / local-first tiers):
 *   1. Hydrate on mount: list groups + threads, merge their metadata into
 *      the reducer (server-pushed, never marks the project dirty). Messages
 *      load lazily, per thread, on open.
 *   2. Load-on-open: when a thread without loaded messages is selected,
 *      fetch one newest-first page and merge it in.
 *   3. Autosave: a debounced diff (via the shared syncCollection helper)
 *      pushes new/changed threads + their messages, new/changed groups, and
 *      DELETEs whatever vanished locally. Idempotent server upserts make a
 *      retry or a coalesced burst safe.
 *
 * It does NOT own the import button: the explicit opt-in push is the
 * project-level useProjectServerImport (which pushes chat AND editor in one
 * flow). This hook only hydrates + autosaves once the project is opted in
 * (syncActive), seeding its synced fingerprint to WHAT THE SERVER HOLDS so a
 * local-newer entity is pushed up rather than stranded.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch } from 'react'

import {
  appendChatMessages,
  deleteChatMessage,
  deleteChatThread,
  deleteChatThreadGroup,
  listChatMessages,
  listChatThreadGroups,
  listChatThreads,
  saveChatThread,
  saveChatThreadGroup,
} from '@/api/inqtrixClient'
import {
  fingerprintThread,
  groupRecordFromServer,
  messageRecordFromServer,
  pushThreadMessages,
  serverGroupPayload,
  serverMessagePayload,
  serverThreadPayload,
  shouldLoadServerChatMessages,
  syncChatThreads,
  threadRecordFromServer,
  type ThreadFingerprint,
} from '@/features/chat/chatHistorySync'
import {
  chatThreadHistoryPending,
  selectedChatMessageLoadState,
  shareChatMessageLoad,
  type SharedChatMessageLoad,
  updateChatMessageLoadError,
} from '@/features/chat/chatMessageLoad'
import type {
  ChatThreadGroupRecord,
  ChatThreadRecord,
} from '@/features/project/types'
import {
  deleteTolerant404,
  syncCollection,
} from '@/features/project/syncCollection'
import {
  useProjectSyncLifecycle,
  useSyncScope,
  type SyncLifecycleToken,
} from '@/features/project/useProjectSyncLifecycle'
import type { ResearchDeskAction } from '@/features/researchDesk/state'
import { deletionSyncExclusions } from './deletionSyncExclusions'
import { createMutationLane } from './mutationLane'
import { messageFromError } from '@/api/requestErrorText'

const AUTOSAVE_DEBOUNCE_MS = 1_500
// Threads load on-demand (page one fast, then cursor-based load-more in the
// history sidebar), so the page is sidebar-sized rather than a bulk walk.
const THREAD_PAGE_LIMIT = 50
const MESSAGE_PAGE_LIMIT = 200
/** How many most-recently-updated threads to warm at startup so opening a recent
 * conversation shows real messages instantly (the skeleton then only covers
 * older threads). A handful of first-page fetches — cheap. */
const PREFETCH_RECENT_THREAD_COUNT = 5

type UseChatHistoryApiOptions = {
  apiKey: string | undefined
  chatThreadGroupMemberships: Record<string, string | null>
  chatThreadGroups: Record<string, ChatThreadGroupRecord>
  chatThreads: Record<string, ChatThreadRecord>
  dispatch: Dispatch<ResearchDeskAction>
  /** In-session load counter (bumped on every wholesale project replace). Part
   * of the lifecycle identity so a switch to another synced project re-hydrates
   * from its own server state instead of inheriting this one's synced map. */
  projectEpoch: number
  selectedThreadId: string | null
  /** The user opted this project into server sync (``serverSyncEnabled``
   * AND the durable capability AND not demo): hydrate + load-on-open +
   * autosave run. The explicit import push is project-level
   * (useProjectServerImport), not this hook. */
  syncActive: boolean
  workspaceId: string
}

export type ChatHistoryApiHandle = {
  error: string | null
  /** Whether the server has older thread pages not yet loaded. */
  hasMoreThreads: boolean
  /** A load-older page request is in flight (drives the button's busy state). */
  isLoadingMore: boolean
  /** True while the SELECTED server thread's messages are still being fetched
   * (lazy load-on-open). Lets the message view show a skeleton instead of the
   * empty-state hero during the gap. False for local/demo threads and once the
   * fetch settles. */
  isSelectedThreadMessagesLoading: boolean
  /** True while the selected thread is a listed server thread whose history
   * has neither loaded nor failed; a send into it waits for the history. */
  isSelectedThreadHistoryPending: boolean
  /** Terminal lazy-hydration failure for the selected thread. This is distinct
   * from a successfully loaded empty conversation. */
  selectedThreadMessagesError: string | null
  /** Load the next page of older threads (cursor-based; appends to the list). */
  loadMoreThreads: () => Promise<void>
  /** Warm one thread from navigation intent. Uses the same in-flight/cache
   * guard as selection, so hover followed by click never duplicates work. */
  prefetchThreadMessages: (threadId: string) => Promise<void>
  /** Clear the selected failure and start a fresh, visibly surfaced load. */
  retrySelectedThreadMessages: () => Promise<void>
  /** Delete one thread and only then remove it locally. The row stays
   * visible in a `deleting` state until the server confirms; a failure
   * leaves it visible as `delete_failed` and waits for {@link deleteThread}
   * to be called again from the row's retry action — nothing retries on its
   * own, so a server that keeps refusing can never be hammered. */
  deleteThread: (threadId: string) => Promise<void>
}

export function useChatHistoryApi({
  apiKey,
  chatThreadGroupMemberships,
  chatThreadGroups,
  chatThreads,
  dispatch,
  projectEpoch,
  selectedThreadId,
  syncActive,
  workspaceId,
}: UseChatHistoryApiOptions): ChatHistoryApiHandle {
  const [error, setError] = useState<string | null>(null)
  // Mirrors hydratedRef into render state so the load-on-open and autosave
  // effects RE-RUN once hydration completes (a ref mutation alone would
  // not), while hydratedRef keeps the value readable from the debounced
  // flush callback outside React's render cycle.
  const [hydrated, setHydrated] = useState(false)
  // Threads whose load-on-open has completed with an authoritative payload
  // (fetched or confirmed-empty). Failures stay separate so they can never be
  // mistaken for an empty conversation.
  const [messageLoadResolved, setMessageLoadResolved] = useState<ReadonlySet<string>>(() => new Set())
  const [messageLoadErrors, setMessageLoadErrors] = useState<ReadonlyMap<string, string>>(
    () => new Map(),
  )

  // Latest state read by the async flush without stale closures.
  const threadsRef = useRef(chatThreads)
  threadsRef.current = chatThreads
  const groupsRef = useRef(chatThreadGroups)
  groupsRef.current = chatThreadGroups
  const membershipsRef = useRef(chatThreadGroupMemberships)
  membershipsRef.current = chatThreadGroupMemberships

  const optionsRef = useRef({ apiKey, workspaceId })
  optionsRef.current = { apiKey, workspaceId }

  // "What the server has", seeded by hydrate/import and advanced by every
  // successful push, so the diff only writes genuinely-changed entities.
  const syncedThreadsRef = useRef(new Map<string, ThreadFingerprint>())
  const syncedGroupsRef = useRef(new Map<string, string>())
  // The per-thread baseline of message ids the server is known to hold —
  // the thread-fingerprint baseline extended to message granularity. The
  // append push only upserts, so without this a locally-deleted message
  // lingers on the server and a reload resurrects it; the push diffs this
  // baseline against the current messages to delete the vanished ones by
  // id. Seeded on load-on-open, advanced on every push, cleared on reset.
  const syncedMessagesRef = useRef(new Map<string, Set<string>>())
  const loadedThreadsRef = useRef(new Set<string>())
  const messageLoadsRef = useRef(new Map<string, SharedChatMessageLoad>())
  // The next-page cursor for on-demand thread loading; undefined = no more.
  const threadCursorRef = useRef<string | undefined>(undefined)
  const loadingMoreRef = useRef(false)
  const [isLoadingMore, setIsLoadingMore] = useState(false)
  const [hasMoreThreads, setHasMoreThreads] = useState(false)
  const hydratedRef = useRef(false)
  const flushingRef = useRef(false)
  const flushPendingRef = useRef(false)
  const syncActiveRef = useRef(syncActive)
  syncActiveRef.current = syncActive
  const syncIdentity = `${workspaceId}:${projectEpoch}`
  const bindSyncPass = useSyncScope(syncActive, syncIdentity)
  /** Threads whose server deletion is in flight — a second click (or a
   * retry while the first attempt still runs) must not issue a second
   * request. */
  const deletingThreadsRef = useRef(new Set<string>())
  /** Ids the server has already deleted while hydration was still in
   * flight. Hydration lists the threads BEFORE that delete lands, so its
   * payload would re-insert the row. This holds only until the running
   * hydration has been filtered; it is not a general tombstone. */
  const deletedDuringHydrationRef = useRef(new Set<string>())
  /** Ids whose server DELETE succeeded but whose local removal may not have
   * committed yet. A flush queued behind the deletion runs before that
   * commit and would otherwise re-push the stale record (pruned baseline =
   * "new"), resurrecting the deleted thread. Purged per flush pass once the
   * id has left the collection — see deletionSyncExclusions. */
  const recentlyDeletedThreadsRef = useRef(new Set<string>())

  // -- one server-mutation lane ------------------------------------------ #

  /** Serializes every server mutation this hook performs: the debounced
   * autosave writes a thread with an UPSERT, so an autosave overlapping a
   * deletion could land after the DELETE and bring the thread — and its
   * messages — back. */
  const mutationLaneRef = useRef(createMutationLane())
  const runExclusive = useCallback(
    <Result,>(task: () => Promise<Result>): Promise<Result> => mutationLaneRef.current.run(task),
    [],
  )

  // -- pushing one entity (syncCollection advances the synced fingerprint) #

  const fetchServerMessageIds = useCallback(async (threadId: string) => {
    const ids = new Set<string>()
    let cursor: string | undefined
    do {
      const page = await listChatMessages(threadId, {
        ...optionsRef.current,
        cursor,
        limit: MESSAGE_PAGE_LIMIT,
      })
      for (const serverMessage of page.data) {
        ids.add(serverMessage.id)
      }
      cursor = page.next_cursor ?? undefined
    } while (cursor)
    return ids
  }, [])

  const ensureMessageBaseline = useCallback(async (threadId: string) => {
    const existing = syncedMessagesRef.current.get(threadId)
    if (existing) return existing

    const fetched = await fetchServerMessageIds(threadId)
    const advancedWhileFetching = syncedMessagesRef.current.get(threadId)
    if (advancedWhileFetching) {
      const merged = new Set([...fetched, ...advancedWhileFetching])
      syncedMessagesRef.current.set(threadId, merged)
      return merged
    }

    syncedMessagesRef.current.set(threadId, fetched)
    return fetched
  }, [fetchServerMessageIds])

  const pushThread = useCallback(async (
    thread: ChatThreadRecord,
    knownAtSnapshot: ReadonlySet<string> | undefined,
  ) => {
    const options = optionsRef.current
    const groupId = membershipsRef.current[thread.id] ?? null
    const wire = await saveChatThread(thread.id, serverThreadPayload(thread, groupId), options)
    dispatch({ type: 'reconcileSessionTitle', kind: 'chat', id: thread.id,
      wire, submitted: thread, projectEpoch })
    // Delete the messages that vanished locally before re-upserting the rest,
    // against the baseline of this pass's snapshot. For a local thread that
    // already has messages but no loaded baseline, the server ids are fetched
    // first so a destructive local retry can delete the replaced answer/tail in
    // the same push instead of letting old server-only messages resurrect on
    // reload. A metadata-only push of an un-opened thread (no baseline, no
    // messages) records no baseline: an empty one would later read its
    // un-fetched server messages as deletions.
    const baseline = await pushThreadMessages({
      appendMessages: async (messages) => {
        await appendChatMessages(thread.id, messages.map(serverMessagePayload), options)
      },
      deleteMessage: (id) => deleteTolerant404(
        () => deleteChatMessage(thread.id, id, options),
      ),
      fetchBaseline: () => ensureMessageBaseline(thread.id),
      knownAtSnapshot,
      messages: thread.messages,
    })
    if (baseline) syncedMessagesRef.current.set(thread.id, baseline)
  }, [dispatch, ensureMessageBaseline, projectEpoch])

  const pushGroup = useCallback(async (group: ChatThreadGroupRecord) => {
    await saveChatThreadGroup(
      group.id,
      serverGroupPayload(group),
      optionsRef.current,
    )
  }, [])

  // -- autosave flush (debounced, serialized) ---------------------------- #

  const flush = useCallback(async () => {
    if (!syncActiveRef.current || !hydratedRef.current) return
    if (flushingRef.current) {
      flushPendingRef.current = true
      return
    }
    flushingRef.current = true
    const inScope = bindSyncPass()
    try {
      await runExclusive(async () => {
        const memberships = membershipsRef.current
        // Threads owned by a confirmed deletion are off-limits for this diff
        // in both directions; tombstones whose removal has committed have
        // done their job and are dropped.
        const { exclude, settled } = deletionSyncExclusions(
          threadsRef.current,
          recentlyDeletedThreadsRef.current,
        )
        for (const id of settled) recentlyDeletedThreadsRef.current.delete(id)
        await syncChatThreads({
          inScope,
          threads: threadsRef.current,
          exclude,
          syncedThreads: syncedThreadsRef.current,
          syncedMessages: syncedMessagesRef.current,
          fingerprintOf: (thread) =>
            fingerprintThread(thread, memberships[thread.id] ?? null),
          pushThread,
          deleteThread: async (id) => {
            await deleteTolerant404(
              () => deleteChatThread(id, optionsRef.current),
            )
            loadedThreadsRef.current.delete(id)
          },
        })
        await syncCollection<ChatThreadGroupRecord, string>({
          inScope,
          current: groupsRef.current,
          synced: syncedGroupsRef.current,
          fingerprintOf: (group) => group.updatedAt,
          changed: (previous, current) => previous !== current,
          pushOne: pushGroup,
          deleteOne: (id) => deleteTolerant404(
            () => deleteChatThreadGroup(id, optionsRef.current),
          ),
        })
      })
      setError(null)
    } catch (caught) {
      setError(messageFromError(caught))
    } finally {
      flushingRef.current = false
      if (flushPendingRef.current) {
        flushPendingRef.current = false
        void flush()
      }
    }
  }, [bindSyncPass, pushGroup, pushThread, runExclusive])

  // -- confirmed thread deletion ----------------------------------------- #

  const deleteThread = useCallback(async (threadId: string) => {
    if (deletingThreadsRef.current.has(threadId)) return
    // No server transport at all (local-only project, demo, no durable
    // capability): the local removal IS the deletion. Hydration state is
    // deliberately NOT part of this test — a synced project whose hydration
    // has not landed yet still owns a server row, and removing it locally
    // would look done while the row survives the next reload.
    if (!syncActiveRef.current) {
      dispatch({ threadId, type: 'deleteChatThread' })
      return
    }
    const wasHydrated = hydratedRef.current
    // Bound at click time, not at lane-execution time: if the user switches
    // projects while this deletion waits in the lane, the live options ref
    // would aim the DELETE at the NEW workspace, where the id 404s and the
    // tolerant delete would silently report success for a row that still
    // exists in the workspace the user actually deleted it from.
    const options = optionsRef.current
    deletingThreadsRef.current.add(threadId)
    dispatch({
      deletion: { error: null, status: 'deleting' },
      threadId,
      type: 'setChatThreadDeletion',
    })
    try {
      await runExclusive(async () => {
        await deleteTolerant404(
          () => deleteChatThread(threadId, options),
        )
        // The server no longer holds the thread: drop every baseline that
        // could make a later flush re-push or re-delete it. The tombstone
        // covers the gap until the local removal commits — a flush queued
        // behind this task still sees the thread in its state snapshot.
        recentlyDeletedThreadsRef.current.add(threadId)
        syncedThreadsRef.current.delete(threadId)
        syncedMessagesRef.current.delete(threadId)
        loadedThreadsRef.current.delete(threadId)
        messageLoadsRef.current.delete(threadId)
        if (!wasHydrated || !hydratedRef.current) {
          deletedDuringHydrationRef.current.add(threadId)
        }
      })
      dispatch({ threadId, type: 'deleteChatThread' })
    } catch (caught) {
      // Stays visible and inert; only the row's retry action calls again.
      dispatch({
        deletion: { error: messageFromError(caught), status: 'delete_failed' },
        threadId,
        type: 'setChatThreadDeletion',
      })
    } finally {
      deletingThreadsRef.current.delete(threadId)
    }
  }, [dispatch, runExclusive])

  // -- reset + hydrate lifecycle (re-armed on project identity) ---------- #

  // Drop the prior project's sync state. Runs before every (re)hydrate and on
  // leaving sync, so a switch to another synced project never carries this
  // project's synced fingerprints into a cross-project delete.
  const reset = useCallback(() => {
    hydratedRef.current = false
    setHydrated(false)
    syncedThreadsRef.current.clear()
    syncedGroupsRef.current.clear()
    syncedMessagesRef.current.clear()
    loadedThreadsRef.current.clear()
    messageLoadsRef.current.clear()
    setMessageLoadResolved(new Set())
    setMessageLoadErrors(new Map())
    threadCursorRef.current = undefined
    loadingMoreRef.current = false
    setIsLoadingMore(false)
    setHasMoreThreads(false)
    deletedDuringHydrationRef.current.clear()
    recentlyDeletedThreadsRef.current.clear()
  }, [])

  const hydrate = useCallback((token: SyncLifecycleToken) => {
    void (async () => {
      try {
        const options = optionsRef.current
        const serverGroups = await listChatThreadGroups(options)
        const groupRecords = serverGroups.map(groupRecordFromServer)
        const threadRecords: ChatThreadRecord[] = []
        const memberships: Record<string, string | null> = {}
        // Load only the first (newest) page; older pages load on demand via
        // loadMoreThreads. The autosave stays safe because synced is seeded
        // only for loaded threads (below), so delete-detection never touches an
        // un-loaded thread.
        const page = await listChatThreads({ ...options, limit: THREAD_PAGE_LIMIT })
        for (const serverThread of page.data) {
          const { groupId, record } = threadRecordFromServer(serverThread)
          // This listing was taken before a deletion that has since been
          // confirmed; re-inserting the row would resurrect it on screen.
          if (deletedDuringHydrationRef.current.has(record.id)) continue
          threadRecords.push(record)
          memberships[record.id] = groupId
        }
        deletedDuringHydrationRef.current.clear()
        if (token.cancelled) return
        if (groupRecords.length > 0) {
          dispatch({ groups: groupRecords, type: 'upsertServerChatThreadGroups' })
        }
        if (threadRecords.length > 0) {
          dispatch({
            memberships,
            threads: threadRecords,
            type: 'upsertServerChatThreads',
          })
        }
        // Seed each fingerprint to WHAT THE SERVER HOLDS (the server
        // record), never the local value. When the local copy is newer the
        // reducer keeps it, so local != seeded-server and the next autosave
        // pushes the newer local thread UP (local-newer-wins on the server,
        // not merely locally). When the server is newer or the thread is
        // new, local == server after the merge, so no spurious re-push.
        for (const record of threadRecords) {
          syncedThreadsRef.current.set(
            record.id,
            fingerprintThread(record, memberships[record.id] ?? null),
          )
        }
        for (const record of groupRecords) {
          syncedGroupsRef.current.set(record.id, record.updatedAt)
        }
        threadCursorRef.current = page.next_cursor ?? undefined
        setHasMoreThreads(Boolean(page.next_cursor))
        hydratedRef.current = true
        setHydrated(true)
        setError(null)
      } catch (caught) {
        if (!token.cancelled) setError(messageFromError(caught))
      }
    })()
  }, [dispatch])

  useProjectSyncLifecycle({
    active: syncActive,
    identity: syncIdentity,
    reset,
    hydrate,
  })

  // -- load a thread's messages on open ---------------------------------- #

  // Fetch (and cache) one thread's messages if not already loaded. Selection and
  // prefetch share one flight; selection promotes a running silent prefetch to
  // visible error handling instead of treating its unfinished request as a warm
  // cache hit. `loadedThreadsRef` records successful payloads only.
  const loadThreadMessages = useCallback(
    (threadId: string, { surfaceErrors }: { surfaceErrors: boolean }) => {
      const thread = threadsRef.current[threadId]
      const markResolved = () =>
        setMessageLoadResolved((prev) => (prev.has(threadId) ? prev : new Set(prev).add(threadId)))
      const serverThreadKnown = syncedThreadsRef.current.has(threadId)
      if (!shouldLoadServerChatMessages(
        thread,
        serverThreadKnown,
        loadedThreadsRef.current.has(threadId),
      )) {
        if (thread && !serverThreadKnown && thread.messages.length === 0) {
          markResolved()
          setMessageLoadErrors((current) =>
            updateChatMessageLoadError(current, threadId, null))
        }
        return Promise.resolve()
      }
      if (surfaceErrors) {
        setMessageLoadErrors((current) =>
          updateChatMessageLoadError(current, threadId, null))
      }
      return shareChatMessageLoad(
        messageLoadsRef.current,
        threadId,
        surfaceErrors,
        async (load) => {
          try {
            const messages: ReturnType<typeof messageRecordFromServer>[] = []
            let cursor: string | undefined
            do {
              const page = await listChatMessages(threadId, {
                ...optionsRef.current,
                cursor,
                limit: MESSAGE_PAGE_LIMIT,
              })
              for (const serverMessage of page.data) {
                messages.push(messageRecordFromServer(serverMessage))
              }
              cursor = page.next_cursor ?? undefined
            } while (cursor)
            if (messages.length > 0) {
              dispatch({ messages, threadId, type: 'upsertServerChatMessages' })
            }
            // Seed the per-thread message baseline with the server set just fetched,
            // UNIONED with any baseline a concurrent push already advanced (e.g. a
            // message sent during this very first open). Both subsets are
            // server-confirmed, so the union can never invent a phantom id (no
            // spurious delete), whereas a plain replace would clobber the pushed id
            // and let a later delete of it be lost, resurrecting it on reload. An
            // empty union still marks the thread as baseline-known.
            const seededIds = syncedMessagesRef.current.get(threadId)
            syncedMessagesRef.current.set(
              threadId,
              new Set([...(seededIds ?? []), ...messages.map((message) => message.id)]),
            )
            loadedThreadsRef.current.add(threadId)
            markResolved()
            setMessageLoadErrors((current) =>
              updateChatMessageLoadError(current, threadId, null))
            if (load.surfaceErrors) setError(null)
          } catch (caught) {
            // A failed request is neither a warm cache entry nor authoritative
            // empty content. Leave it unresolved and retryable; a selected load
            // (including a promoted prefetch) exposes the failure.
            loadedThreadsRef.current.delete(threadId)
            if (load.surfaceErrors) {
              const message = messageFromError(caught)
              setMessageLoadErrors((current) =>
                updateChatMessageLoadError(current, threadId, message))
              setError(message)
            }
          }
        },
      )
    },
    [dispatch],
  )

  useEffect(() => {
    // Gate on the `hydrated` STATE (not the ref) so this re-runs when an async
    // hydration completes AFTER the selected thread was restored from the
    // manifest — otherwise the landed thread would stay visibly empty.
    if (!syncActive || !hydrated || !selectedThreadId) return
    void loadThreadMessages(selectedThreadId, { surfaceErrors: true })
  }, [syncActive, hydrated, selectedThreadId, loadThreadMessages])

  // Warm the most-recently-updated threads at startup so opening a recent
  // conversation shows real messages instantly (the skeleton then only appears
  // for older, un-warmed threads). Deduped by the same loaded-guard as
  // load-on-open; errors stay silent (background work).
  useEffect(() => {
    if (!syncActive || !hydrated) return
    const recent = Object.values(threadsRef.current)
      .sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : a.updatedAt > b.updatedAt ? -1 : 0))
      .slice(0, PREFETCH_RECENT_THREAD_COUNT)
    for (const thread of recent) {
      void loadThreadMessages(thread.id, { surfaceErrors: false })
    }
  }, [syncActive, hydrated, loadThreadMessages])

  // -- debounced autosave trigger ---------------------------------------- #

  // Depends on `hydrated` too, so the FIRST flush runs right after hydration
  // even when no local edit followed — that reconciling flush pushes any
  // local-newer-than-server threads up (paired with the server-seeded
  // fingerprint above).
  useEffect(() => {
    if (!syncActive || !hydrated) return
    if (!error && Object.values(chatThreads).some((thread) => thread.titleRequest
      && (!thread.titleServerReady || (thread.titlePending && thread.titleSource === 'initial')))) {
      void flush()
      return
    }
    const timer = setTimeout(() => {
      void flush()
    }, AUTOSAVE_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [
    chatThreads,
    chatThreadGroups,
    chatThreadGroupMemberships,
    error,
    syncActive,
    hydrated,
    flush,
  ])

  // -- load older threads on demand ------------------------------------- #

  const loadMoreThreads = useCallback(async () => {
    if (!syncActiveRef.current || !hydratedRef.current) return
    if (loadingMoreRef.current) return
    const cursor = threadCursorRef.current
    if (!cursor) return
    loadingMoreRef.current = true
    setIsLoadingMore(true)
    try {
      const page = await listChatThreads({
        ...optionsRef.current,
        cursor,
        limit: THREAD_PAGE_LIMIT,
      })
      // The sync session may have been torn down while the page was in flight
      // (project closed / left server-sync). Mirror the hydrate effect's
      // cancellation discipline: do not dispatch or re-seed into a session the
      // re-arm effect has already cleared, which would re-pollute synced state.
      if (!syncActiveRef.current || !hydratedRef.current) return
      const threadRecords: ChatThreadRecord[] = []
      const memberships: Record<string, string | null> = {}
      for (const serverThread of page.data) {
        const { groupId, record } = threadRecordFromServer(serverThread)
        threadRecords.push(record)
        memberships[record.id] = groupId
      }
      if (threadRecords.length > 0) {
        // append: older page goes to the END of the order (stays newest-first).
        dispatch({ append: true, memberships, threads: threadRecords, type: 'upsertServerChatThreads' })
        // Seed synced ONLY for these newly loaded threads (delete-detection
        // stays scoped to loaded threads — never an un-loaded one).
        for (const record of threadRecords) {
          syncedThreadsRef.current.set(
            record.id,
            fingerprintThread(record, memberships[record.id] ?? null),
          )
        }
      }
      threadCursorRef.current = page.next_cursor ?? undefined
      setHasMoreThreads(Boolean(page.next_cursor))
      setError(null)
    } catch (caught) {
      setError(messageFromError(caught))
    } finally {
      loadingMoreRef.current = false
      setIsLoadingMore(false)
    }
  }, [dispatch])

  const selectedThreadRecord = selectedThreadId ? chatThreads[selectedThreadId] : undefined
  const selectedMessageLoadState = selectedChatMessageLoadState({
    errorByThreadId: messageLoadErrors,
    expectsServerMessages: Boolean(
      syncActive
      && hydrated
      && selectedThreadId
      && selectedThreadRecord
      && selectedThreadRecord.source === 'api'
      && selectedThreadRecord.messages.length === 0,
    ),
    resolvedThreadIds: messageLoadResolved,
    selectedThreadId,
  })
  const selectedThreadHistoryPending = Boolean(
    selectedThreadId
    && selectedThreadRecord
    && chatThreadHistoryPending({
      failed: messageLoadErrors.has(selectedThreadId),
      localMessageCount: selectedThreadRecord.messages.length,
      resolved: messageLoadResolved.has(selectedThreadId),
      serverThreadKnown: syncActive && syncedThreadsRef.current.has(selectedThreadId),
    }),
  )

  const prefetchThreadMessages = useCallback(async (threadId: string) => {
    await loadThreadMessages(threadId, { surfaceErrors: false })
  }, [loadThreadMessages])

  const retrySelectedThreadMessages = useCallback(async () => {
    if (!selectedThreadId) return
    setMessageLoadErrors((current) =>
      updateChatMessageLoadError(current, selectedThreadId, null),
    )
    await loadThreadMessages(selectedThreadId, { surfaceErrors: true })
  }, [loadThreadMessages, selectedThreadId])

  return {
    deleteThread,
    error,
    hasMoreThreads,
    isLoadingMore,
    isSelectedThreadHistoryPending: selectedThreadHistoryPending,
    isSelectedThreadMessagesLoading: selectedMessageLoadState.loading,
    loadMoreThreads,
    prefetchThreadMessages,
    retrySelectedThreadMessages,
    selectedThreadMessagesError: selectedMessageLoadState.error,
  }
}
