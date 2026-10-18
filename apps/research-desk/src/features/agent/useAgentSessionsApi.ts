import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch } from 'react'

import {
  deleteAgentSession,
  deleteAgentSessionGroup,
  getAgentSession,
  listAgentSessionGroups,
  listAgentSessions,
  saveAgentSession,
  saveAgentSessionGroup,
} from '@/api/inqtrixClient'
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
import { useSessionDeletionApi } from '@/features/project/useSessionDeletionApi'
import type { AgentSessionGroupRecord, AgentSessionRecord } from './model'
import {
  agentSessionFingerprint,
  pushAgentSessions,
  serverAgentSessionGroupPayload,
  serverAgentSessionFingerprint,
  serverAgentSessionPayload,
} from './agentSessionSync'

const AUTOSAVE_DEBOUNCE_MS = 1_500

/**
 * Server sync for agent sessions (clone of `useKnowledgeSessionsApi`,
 * simplified): sessions are metadata rows only — the turns are durable
 * server RUNS hydrated separately via the run list. Hydrates on activation
 * and pushes fingerprint-guarded changes debounced ({@link pushAgentSessions});
 * a session row is deleted only through the explicit deletion API, a session
 * group by the group diff.
 */
/** Hydration identities (workspace:epoch) whose first listing SUCCEEDED in
 * this document lifetime. See the `settled` note in the hook; a full reload
 * drops the set, so a genuine cold start still shows the loading skeleton
 * exactly once, and a failed listing never marks its identity as known. */
const settledHydrationIdentities = new Set<string>()

export function useAgentSessionsApi({
  apiKey,
  dispatch,
  projectEpoch,
  sessionGroups,
  sessionOrder,
  selectedSessionId,
  sessions,
  syncActive,
  workspaceId,
}: {
  apiKey: string | undefined
  dispatch: Dispatch<ResearchDeskAction>
  projectEpoch: number
  sessionGroups: Record<string, AgentSessionGroupRecord>
  sessionOrder: string[]
  selectedSessionId: string | null
  sessions: Record<string, AgentSessionRecord>
  syncActive: boolean
  workspaceId: string
}): {
  createSession: (session: AgentSessionRecord) => Promise<boolean>
  deleteSession: (sessionId: string) => Promise<void>
  error: string | null
  /** A deletion receipt poll stopped after repeated failed reads. */
  observationStopped: boolean
  retrySessionDeletion: (sessionId: string) => Promise<void>
  settled: boolean
} {
  const [error, setError] = useState<string | null>(null)
  const [hydrated, setHydrated] = useState(false)
  // "Settled", not "succeeded": the initial listing finished either way.
  // The workspace gates its loading skeleton on THIS (a failed listing
  // must end the skeleton and surface via `error`), while `hydrated`
  // stays success-only — flush must never push/delete against a
  // baseline that was never seeded (it would resurrect deleted rows).
  //
  // The state is remount-local, but the ANSWER it carries ("has the first
  // listing for this workspace+epoch finished?") is not: a view switch
  // remounts the desk while the store keeps every session, and re-arming
  // the skeleton over known data is exactly the center-column flash. The
  // module-level identity set makes remounts start settled; a project
  // epoch change mints a new identity and is genuinely unknown again.
  const hydrationIdentity = `${workspaceId}:${projectEpoch}`
  const [settled, setSettled] = useState(
    () => settledHydrationIdentities.has(hydrationIdentity),
  )
  const hydrationIdentityRef = useRef(hydrationIdentity)
  hydrationIdentityRef.current = hydrationIdentity
  const bindSyncPass = useSyncScope(syncActive, hydrationIdentity)

  const sessionsRef = useRef(sessions)
  sessionsRef.current = sessions
  const sessionOrderRef = useRef(sessionOrder)
  sessionOrderRef.current = sessionOrder
  const groupsRef = useRef(sessionGroups)
  groupsRef.current = sessionGroups
  const optionsRef = useRef({ apiKey, workspaceId })
  optionsRef.current = { apiKey, workspaceId }
  const syncActiveRef = useRef(syncActive)
  syncActiveRef.current = syncActive

  const syncedRef = useRef(new Map<string, string>())
  const syncedGroupsRef = useRef(new Map<string, string>())
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
      dispatch({ operationId, sessionId, type: 'deleteAgentSession' })
    },
    onState: (sessionId, deletion) => {
      dispatch({ deletion, sessionId, type: 'setAgentSessionDeletionState' })
    },
    options: { apiKey, workspaceId },
    scopeKey: `${workspaceId}:${projectEpoch}:${syncActive ? 'on' : 'off'}`,
    sessions,
    start: deleteAgentSession,
    targetKind: 'agent_session',
  })

  const reset = useCallback(() => {
    syncedRef.current.clear()
    syncedGroupsRef.current.clear()
    flushingRef.current = false
    flushPendingRef.current = false
    setHydrated(false)
    setSettled(settledHydrationIdentities.has(hydrationIdentityRef.current))
  }, [])

  const hydrate = useCallback(
    (token: SyncLifecycleToken) => {
      void (async () => {
        try {
          const [serverGroups, serverSessions] = await Promise.all([
            listAgentSessionGroups(optionsRef.current),
            listAgentSessions(optionsRef.current),
          ])
          if (token.cancelled) return
          dispatch({
            groups: serverGroups,
            sessions: serverSessions,
            type: 'upsertServerAgentSessions',
          })
          for (const group of serverGroups) {
            syncedGroupsRef.current.set(group.id, String(group.updated_at))
          }
          for (const wire of serverSessions) {
            syncedRef.current.set(
              wire.id,
              serverAgentSessionFingerprint(wire),
            )
          }
          setHydrated(true)
          settledHydrationIdentities.add(hydrationIdentityRef.current)
          setSettled(true)
          setError(null)
        } catch (caught) {
          if (!token.cancelled) {
            // Settles THIS mount (the skeleton must end and yield to the
            // error surface) but does NOT mark the identity as known: a
            // failed listing left the question unanswered, so the next
            // mount honestly starts loading again.
            setSettled(true)
            setError(
              caught instanceof Error ? caught.message : String(caught),
            )
          }
        }
      })()
    },
    [dispatch],
  )

  useProjectSyncLifecycle({
    active: syncActive,
    hydrate,
    identity: hydrationIdentity,
    reset,
  })

  const flush = useCallback(async () => {
    if (!syncActiveRef.current || !hydrated) return
    if (flushingRef.current) {
      flushPendingRef.current = true
      return
    }
    flushingRef.current = true
    const inScope = bindSyncPass()
    try {
      await syncCollection<AgentSessionGroupRecord, string>({
        current: groupsRef.current,
        synced: syncedGroupsRef.current,
        fingerprintOf: (group) => group.updatedAt,
        changed: (previous, current) => previous !== current,
        pushOne: async (group) => {
          await saveAgentSessionGroup(
            group.id,
            serverAgentSessionGroupPayload(group),
            optionsRef.current,
          )
        },
        deleteOne: (id) => deleteTolerant404(
          () => deleteAgentSessionGroup(id, optionsRef.current),
        ),
        inScope,
      })

      await pushAgentSessions({
        inScope,
        order: sessionOrderRef.current,
        save: async (session) => {
          const wire = await saveAgentSession(
            session.id,
            serverAgentSessionPayload(session),
            optionsRef.current,
          )
          dispatch({ type: 'reconcileSessionTitle', kind: 'agent', id: session.id,
            wire, submitted: session, projectEpoch })
        },
        sessions: sessionsRef.current,
        synced: syncedRef.current,
      })
      setError(null)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
    } finally {
      flushingRef.current = false
      if (flushPendingRef.current) {
        flushPendingRef.current = false
        void flush()
      }
    }
  }, [bindSyncPass, dispatch, hydrated, projectEpoch])

  /** Confirm a new row on the server before it becomes visible locally.
   * First-message submit uses the same path, so a failed create cannot leave
   * a phantom session id attached to a durable run. */
  const createSession = useCallback(async (session: AgentSessionRecord) => {
    if (!syncActiveRef.current || session.persistable === false) return false
    try {
      const wire = await saveAgentSession(
        session.id,
        serverAgentSessionPayload(session),
        optionsRef.current,
      )
      // Seed before dispatch: reducer state from the confirmed response must
      // not schedule an immediate duplicate autosave PUT.
      syncedRef.current.set(session.id, agentSessionFingerprint({
        ...session,
        title: wire.title,
        titleRevision: wire.title_revision, titleSource: wire.title_source,
        groupId: wire.group_id,
        createdAt: new Date(wire.created_at * 1000).toISOString(),
        updatedAt: new Date(wire.updated_at * 1000).toISOString(),
        persistable: true,
      }))
      dispatch({
        groups: [],
        selectSessionId: session.id,
        sessions: [wire],
        type: 'upsertServerAgentSessions',
      })
      setError(null)
      return true
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught))
      return false
    }
  }, [dispatch])

  useEffect(() => {
    if (!syncActive || !hydrated) return undefined
    if (!error && Object.values(sessions).some((session) => session.titleRequest
      && (!session.titleServerReady || (session.titlePending && session.titleSource === 'initial')))) {
      void flush()
      return undefined
    }
    const timer = window.setTimeout(() => {
      void flush()
    }, AUTOSAVE_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [error, flush, hydrated, sessionGroups, sessionOrder, sessions, syncActive])

  // Re-read the selected row, so its source policy is current before the
  // user submits even when the session was picked before the listing landed.
  useEffect(() => {
    if (!syncActive || !hydrated || !selectedSessionId) return undefined
    if (sessionsRef.current[selectedSessionId]?.deletion) return undefined
    if (sessionsRef.current[selectedSessionId]?.persistable === false) {
      return undefined
    }
    let cancelled = false
    void getAgentSession(selectedSessionId, optionsRef.current)
      .then((session) => {
        if (cancelled) return
        dispatch({ groups: [], sessions: [session], type: 'upsertServerAgentSessions' })
      })
      .catch((caught) => {
        if (!cancelled) {
          setError(caught instanceof Error ? caught.message : String(caught))
        }
      })
    return () => {
      cancelled = true
    }
  }, [
    dispatch,
    hydrated,
    selectedSessionId,
    sessions[selectedSessionId ?? '']?.persistable,
    syncActive,
  ])

  useEffect(() => {
    if (!syncActive) return undefined
    const onVisibilityChange = () => {
      if (document.visibilityState === 'hidden') void flush()
    }
    document.addEventListener('visibilitychange', onVisibilityChange)
    return () =>
      document.removeEventListener('visibilitychange', onVisibilityChange)
  }, [flush, syncActive])

  return {
    createSession,
    deleteSession,
    error: deletionError ?? error,
    observationStopped,
    retrySessionDeletion,
    settled,
  }
}
