import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch } from 'react'

import {
  deleteKnowledgeSession,
  deleteKnowledgeSessionGroup,
  getKnowledgeSession,
  listKnowledgeSessionGroups,
  listKnowledgeSessions,
  saveKnowledgeSession,
  saveKnowledgeSessionGroup,
} from '@/api/inqtrixClient'
import type {
  KnowledgeSessionGroupRecord,
  KnowledgeSessionRecord,
  KnowledgeThreadItemRecord,
} from '@/features/project/types'
import {
  DEFAULT_KNOWLEDGE_SESSION_TITLE,
  legacyKnowledgeSessionIdReplacements,
} from '@/features/project/knowledgeSessionDefaults'
import {
  deleteTolerant404,
  deletedSinceSnapshot,
  syncCollection,
} from '@/features/project/syncCollection'
import {
  useProjectSyncLifecycle,
  useSyncScope,
  type SyncLifecycleToken,
} from '@/features/project/useProjectSyncLifecycle'
import type { ResearchDeskAction } from '@/features/researchDesk/state'
import { useSessionDeletionApi } from '@/features/project/useSessionDeletionApi'
import {
  fingerprintKnowledgeSession,
  groupRecordFromServer,
  itemsFromServerSession,
  serverKnowledgeSessionGroupPayload,
  serverKnowledgeSessionPayload,
  sessionRecordFromServer,
} from './knowledgeSessionSync'
import {
  decideKnowledgeSessionItemsLoadMerge,
  shouldSurfaceKnowledgeSessionItemsLoadResult,
} from './sessionLoadPolicy'
import { recentKnowledgeSessionsForPrefetch } from './sessionPrefetch'
import { messageFromError } from '@/api/requestErrorText'

const AUTOSAVE_DEBOUNCE_MS = 1_500

type UseKnowledgeSessionsApiOptions = {
  apiKey: string | undefined
  dispatch: Dispatch<ResearchDeskAction>
  items: Record<string, KnowledgeThreadItemRecord>
  itemOrder: string[]
  projectEpoch: number
  selectedSessionId: string | null
  sessionGroupMemberships: Record<string, string | null>
  sessionGroups: Record<string, KnowledgeSessionGroupRecord>
  sessions: Record<string, KnowledgeSessionRecord>
  sessionOrder: string[]
  syncActive: boolean
  workspaceId: string
}

type LoadSessionItemsOptions = {
  surfaceErrors: boolean
}

function isPristineBootstrapSession(
  session: KnowledgeSessionRecord,
  items: readonly KnowledgeThreadItemRecord[],
): boolean {
  return session.isBootstrapPlaceholder === true
    && session.title === DEFAULT_KNOWLEDGE_SESSION_TITLE
    && items.length === 0
}

export function useKnowledgeSessionsApi({
  apiKey,
  dispatch,
  items,
  itemOrder,
  projectEpoch,
  selectedSessionId,
  sessionGroupMemberships,
  sessionGroups,
  sessions,
  sessionOrder,
  syncActive,
  workspaceId,
}: UseKnowledgeSessionsApiOptions): {
  deleteSession: (sessionId: string) => Promise<void>
  error: string | null
  isSelectedSessionItemsLoading: boolean
  /** A deletion receipt poll stopped after repeated failed reads. */
  observationStopped: boolean
  prefetchSessionItems: (sessionId: string) => Promise<void>
  retrySessionDeletion: (sessionId: string) => Promise<void>
} {
  const [error, setError] = useState<string | null>(null)
  const [hydrated, setHydrated] = useState(false)
  // Sessions whose item load-on-open has completed (or errored). Drives the
  // "still fetching?" signal so the ask view shows a skeleton instead of the
  // empty-state hero while a session's items load.
  const [itemsLoadResolved, setItemsLoadResolved] = useState<ReadonlySet<string>>(() => new Set())

  const sessionsRef = useRef(sessions)
  sessionsRef.current = sessions
  const sessionOrderRef = useRef(sessionOrder)
  sessionOrderRef.current = sessionOrder
  const groupsRef = useRef(sessionGroups)
  groupsRef.current = sessionGroups
  const membershipsRef = useRef(sessionGroupMemberships)
  membershipsRef.current = sessionGroupMemberships
  const itemsRef = useRef(items)
  itemsRef.current = items
  const itemOrderRef = useRef(itemOrder)
  itemOrderRef.current = itemOrder
  const selectedSessionIdRef = useRef(selectedSessionId)
  selectedSessionIdRef.current = selectedSessionId
  const optionsRef = useRef({ apiKey, workspaceId })
  optionsRef.current = { apiKey, workspaceId }
  const syncActiveRef = useRef(syncActive)
  syncActiveRef.current = syncActive
  const syncIdentity = `${workspaceId}:${projectEpoch}`
  const bindSyncPass = useSyncScope(syncActive, syncIdentity)

  const syncedRef = useRef(new Map<string, string>())
  const syncedGroupsRef = useRef(new Map<string, string>())
  const serverKnownRef = useRef(new Set<string>())
  const loadedRef = useRef(new Set<string>())
  const loadingRef = useRef(new Map<string, number>())
  const surfaceLoadResultRef = useRef(new Set<string>())
  const nextLoadIdRef = useRef(0)
  const lifecycleEpochRef = useRef(0)
  const mountedRef = useRef(true)
  const flushingRef = useRef(false)
  const flushPendingRef = useRef(false)

  const {
    deleteSession,
    error: deletionError,
    observationStopped,
    retrySession: retrySessionDeletion,
  } = useSessionDeletionApi({
    enabled: syncActive,
    onComplete: (sessionId, operationId) => {
      syncedRef.current.delete(sessionId)
      serverKnownRef.current.delete(sessionId)
      loadedRef.current.delete(sessionId)
      dispatch({ operationId, sessionId, type: 'deleteKnowledgeSession' })
    },
    onState: (sessionId, deletion) => {
      dispatch({ deletion, sessionId, type: 'setKnowledgeSessionDeletionState' })
    },
    options: { apiKey, workspaceId },
    scopeKey: `${workspaceId}:${projectEpoch}:${syncActive ? 'on' : 'off'}`,
    sessions,
    start: deleteKnowledgeSession,
    targetKind: 'knowledge_session',
  })

  useEffect(() => {
    mountedRef.current = true
    return () => {
      mountedRef.current = false
    }
  }, [])

  const itemsForSession = useCallback((sessionId: string) => {
    return itemOrderRef.current
      .map((itemId) => itemsRef.current[itemId])
      .filter((item): item is KnowledgeThreadItemRecord => (
        Boolean(item) && item.sessionId === sessionId
      ))
  }, [])

  const markItemsLoadResolved = useCallback((sessionId: string) => {
    setItemsLoadResolved((prev) => (prev.has(sessionId) ? prev : new Set(prev).add(sessionId)))
  }, [])

  const reset = useCallback(() => {
    lifecycleEpochRef.current += 1
    syncedRef.current.clear()
    syncedGroupsRef.current.clear()
    serverKnownRef.current.clear()
    loadedRef.current.clear()
    loadingRef.current.clear()
    surfaceLoadResultRef.current.clear()
    setItemsLoadResolved(new Set())
    flushingRef.current = false
    flushPendingRef.current = false
    setHydrated(false)
  }, [])

  const hydrate = useCallback((token: SyncLifecycleToken) => {
    void (async () => {
      try {
        const serverGroups = await listKnowledgeSessionGroups(optionsRef.current)
        if (token.cancelled) return
        const groupRecords = serverGroups.map(groupRecordFromServer)
        if (groupRecords.length > 0) {
          dispatch({
            groups: groupRecords,
            type: 'upsertServerKnowledgeSessionGroups',
          })
        }
        for (const group of groupRecords) {
          syncedGroupsRef.current.set(group.id, group.updatedAt)
        }

        const serverSessions = await listKnowledgeSessions(optionsRef.current)
        if (token.cancelled) return
        const converted = serverSessions.map(sessionRecordFromServer)
        const records = converted.map(({ record }) => record)
        const serverIds = new Set(records.map((record) => record.id))
        const replacements = legacyKnowledgeSessionIdReplacements(
          sessionsRef.current,
          serverIds,
        )
        if (Object.keys(replacements).length > 0) {
          dispatch({ replacements, type: 'rekeyKnowledgeSessionIds' })
        }
        const memberships = Object.fromEntries(
          converted.map(({ groupId, record }) => [record.id, groupId]),
        )
        if (records.length > 0) {
          dispatch({
            memberships,
            sessions: records,
            type: 'upsertServerKnowledgeSessions',
          })
        }
        serverKnownRef.current = serverIds
        for (const record of records) {
          syncedRef.current.set(
            record.id,
            fingerprintKnowledgeSession(record, [], memberships[record.id] ?? null),
          )
        }
        // Server is authoritative for the seed bootstrap default only: drop
        // the pristine local placeholder when it is not on the server. A user-
        // created or renamed empty session is real intent and syncs below.
        dispatch({
          serverIds: [...serverKnownRef.current],
          type: 'pruneLocalPlaceholderKnowledgeSessions',
        })
        const selected = selectedSessionIdRef.current
        if (records.length > 0 && (!selected || !serverKnownRef.current.has(selected))) {
          dispatch({ sessionId: records[0].id, type: 'selectKnowledgeSession' })
        }
        setHydrated(true)
        setError(null)
      } catch (caught) {
        if (!token.cancelled) setError(messageFromError(caught))
      }
    })()
  }, [dispatch])

  useProjectSyncLifecycle({
    active: syncActive,
    hydrate,
    identity: syncIdentity,
    reset,
  })

  const loadSessionItems = useCallback(async (
    sessionId: string,
    { surfaceErrors }: LoadSessionItemsOptions,
  ) => {
    if (surfaceErrors) surfaceLoadResultRef.current.add(sessionId)
    if (!serverKnownRef.current.has(sessionId)) return
    if (loadedRef.current.has(sessionId)) {
      if (shouldSurfaceKnowledgeSessionItemsLoadResult({
        selectedSessionId: selectedSessionIdRef.current,
        sessionId,
        surfaceErrors,
      })) {
        setError(null)
      }
      surfaceLoadResultRef.current.delete(sessionId)
      return
    }
    if (loadingRef.current.has(sessionId)) return
    const loadId = nextLoadIdRef.current + 1
    nextLoadIdRef.current = loadId
    const lifecycleEpoch = lifecycleEpochRef.current
    loadingRef.current.set(sessionId, loadId)
    try {
      const serverSession = await getKnowledgeSession(sessionId, optionsRef.current)
      const canApplyResult = mountedRef.current
        && lifecycleEpochRef.current === lifecycleEpoch
        && serverKnownRef.current.has(sessionId)
      if (!canApplyResult) return
      const { groupId, record } = sessionRecordFromServer(serverSession)
      const local = sessionsRef.current[sessionId]
      const serverItems = itemsFromServerSession(serverSession)
      const mergeDecision = decideKnowledgeSessionItemsLoadMerge({
        localItemCount: itemsForSession(sessionId).length,
        localSession: local,
        serverItemCount: serverItems.length,
        serverSession: record,
      })
      if (!mergeDecision.markItemsLoadResolved) return
      syncedRef.current.set(
        sessionId,
        fingerprintKnowledgeSession(record, serverItems, groupId),
      )
      if (mergeDecision.applyServerState) {
        dispatch({
          memberships: { [record.id]: groupId },
          sessions: [record],
          type: 'upsertServerKnowledgeSessions',
        })
        dispatch({
          items: serverItems,
          sessionId,
          type: 'setServerKnowledgeSessionItems',
        })
      }
      if (mergeDecision.markItemsPayloadLoaded) loadedRef.current.add(sessionId)
      markItemsLoadResolved(sessionId)
      if (shouldSurfaceKnowledgeSessionItemsLoadResult({
        selectedSessionId: selectedSessionIdRef.current,
        sessionId,
        surfaceErrors: surfaceErrors || surfaceLoadResultRef.current.has(sessionId),
      })) {
        setError(null)
      }
    } catch (caught) {
      const canApplyError = mountedRef.current
        && lifecycleEpochRef.current === lifecycleEpoch
        && serverKnownRef.current.has(sessionId)
      if (canApplyError) {
        markItemsLoadResolved(sessionId)
        if (shouldSurfaceKnowledgeSessionItemsLoadResult({
          selectedSessionId: selectedSessionIdRef.current,
          sessionId,
          surfaceErrors: surfaceErrors || surfaceLoadResultRef.current.has(sessionId),
        })) {
          setError(messageFromError(caught))
        }
      }
    } finally {
      if (loadingRef.current.get(sessionId) === loadId) {
        loadingRef.current.delete(sessionId)
        surfaceLoadResultRef.current.delete(sessionId)
      }
    }
  }, [dispatch, itemsForSession, markItemsLoadResolved])

  useEffect(() => {
    if (!syncActive || !hydrated || !selectedSessionId) return
    void loadSessionItems(selectedSessionId, { surfaceErrors: true })
  }, [hydrated, loadSessionItems, selectedSessionId, syncActive])

  useEffect(() => {
    if (!syncActive || !hydrated) return
    for (const session of recentKnowledgeSessionsForPrefetch(
      sessionsRef.current,
      serverKnownRef.current,
    )) {
      void loadSessionItems(session.id, { surfaceErrors: false })
    }
  }, [hydrated, loadSessionItems, sessions, syncActive])

  const pushGroup = useCallback(async (group: KnowledgeSessionGroupRecord) => {
    await saveKnowledgeSessionGroup(
      group.id,
      serverKnowledgeSessionGroupPayload(group),
      optionsRef.current,
    )
  }, [])

  const pushSession = useCallback(async (
    session: KnowledgeSessionRecord,
    inScope: () => boolean = () => true,
  ) => {
    let itemsForPayload = itemsForSession(session.id)
    const groupId = membershipsRef.current[session.id] ?? null
    if (serverKnownRef.current.has(session.id) && !loadedRef.current.has(session.id)) {
      const serverSession = await getKnowledgeSession(session.id, optionsRef.current)
      if (!inScope()) return
      itemsForPayload = itemsFromServerSession(serverSession)
      loadedRef.current.add(session.id)
      dispatch({
        items: itemsForPayload,
        sessionId: session.id,
        type: 'setServerKnowledgeSessionItems',
      })
      markItemsLoadResolved(session.id)
    }
    await saveKnowledgeSession(
      session.id,
      serverKnowledgeSessionPayload(session, itemsForPayload, groupId),
      optionsRef.current,
    )
    if (!inScope()) return
    serverKnownRef.current.add(session.id)
    loadedRef.current.add(session.id)
    markItemsLoadResolved(session.id)
    syncedRef.current.set(
      session.id,
      fingerprintKnowledgeSession(session, itemsForPayload, groupId),
    )
  }, [dispatch, itemsForSession, markItemsLoadResolved])

  const flush = useCallback(async () => {
    if (!syncActiveRef.current || !hydrated) return
    if (flushingRef.current) {
      flushPendingRef.current = true
      return
    }
    flushingRef.current = true
    const inScope = bindSyncPass()
    try {
      await syncCollection<KnowledgeSessionGroupRecord, string>({
        inScope,
        current: groupsRef.current,
        synced: syncedGroupsRef.current,
        fingerprintOf: (group) => group.updatedAt,
        changed: (previous, current) => previous !== current,
        pushOne: pushGroup,
        deleteOne: (id) => deleteTolerant404(
          () => deleteKnowledgeSessionGroup(id, optionsRef.current),
        ),
      })

      const currentSessions = sessionsRef.current
      const known = [...syncedRef.current.keys()]
      for (const sessionId of sessionOrderRef.current) {
        const session = currentSessions[sessionId]
        if (!session) continue
        if (session.deletion) continue
        const itemsForFingerprint = itemsForSession(sessionId)
        const groupId = membershipsRef.current[sessionId] ?? null
        // Keep only the untouched seed placeholder local. Renamed or user-
        // created empty sessions are meaningful user state and sync as rows.
        if (
          isPristineBootstrapSession(session, itemsForFingerprint)
          && !serverKnownRef.current.has(sessionId)
        ) {
          continue
        }
        const fingerprint = fingerprintKnowledgeSession(session, itemsForFingerprint, groupId)
        if (syncedRef.current.get(sessionId) !== fingerprint) {
          if (!inScope()) return
          await pushSession(session, inScope)
        }
      }
      for (const sessionId of deletedSinceSnapshot(known, syncedRef.current, currentSessions)) {
        if (!inScope()) return
        await deleteTolerant404(
          async () => { await deleteKnowledgeSession(sessionId, optionsRef.current) },
        )
        if (!inScope()) return
        syncedRef.current.delete(sessionId)
        serverKnownRef.current.delete(sessionId)
        loadedRef.current.delete(sessionId)
      }
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
  }, [bindSyncPass, hydrated, itemsForSession, pushGroup, pushSession])

  useEffect(() => {
    if (!syncActive || !hydrated) return undefined
    const anyRunning = itemOrder.some(
      (itemId) => items[itemId]?.status === 'running',
    )
    if (!anyRunning) {
      // Everything has settled (an answer just arrived, a rename, a delete):
      // persist immediately so a reload right after cannot lose it. flush() is
      // fingerprint-guarded, so an eager call is a no-op when nothing changed.
      void flush()
      return undefined
    }
    // While an ask is still streaming, batch interim changes behind the debounce.
    const timer = window.setTimeout(() => {
      void flush()
    }, AUTOSAVE_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [
    flush,
    hydrated,
    itemOrder,
    items,
    sessionGroupMemberships,
    sessionGroups,
    sessionOrder,
    sessions,
    syncActive,
  ])

  useEffect(() => {
    if (!syncActive) return undefined
    // Backgrounding the tab is the reliable moment to persist before a likely
    // reload/close — same client path as the autosave, no separate beacon.
    // `flush` is in the deps, so the listener is rebound if it changes (no stale
    // closure), and flush() itself no-ops before hydrate completes.
    const onVisibilityChange = () => {
      if (document.visibilityState === 'hidden') void flush()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () => document.removeEventListener('visibilitychange', onVisibilityChange)
  }, [flush, syncActive])

  const isSelectedSessionItemsLoading = Boolean(
    syncActive
    && hydrated
    && selectedSessionId
    && serverKnownRef.current.has(selectedSessionId)
    && itemsForSession(selectedSessionId).length === 0
    && !itemsLoadResolved.has(selectedSessionId),
  )

  const prefetchSessionItems = useCallback(async (sessionId: string) => {
    await loadSessionItems(sessionId, { surfaceErrors: false })
  }, [loadSessionItems])

  return {
    deleteSession,
    error: deletionError ?? error,
    isSelectedSessionItemsLoading,
    observationStopped,
    prefetchSessionItems,
    retrySessionDeletion,
  }
}
