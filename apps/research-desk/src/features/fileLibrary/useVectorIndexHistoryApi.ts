/**
 * Vector-index server sync (project-persistence tier).
 *
 * The simplest of the file-library sync hooks: hydrate the FULL vector-index
 * records on mount (members + capped history travel with the record — there is
 * no heavy lazy body, so no load-on-use) and a debounced serialized autosave
 * that diffs them via the shared syncCollection helper. Persistence only.
 *
 * The autosave fingerprint is {updatedAt, status} and a record is pushed only
 * once it reaches a TERMINAL status: while a reindex runs (status ===
 * 'indexing') the push is deferred, so the server only ever holds a durable
 * status. The high-frequency live progress lives in the separate, non-
 * serialized ``indexingJobs`` map and never reaches this hook — vectorIndexes
 * only changes on start / terminal / create / rename / membership edits, so
 * the debounce never fires on a progress tick.
 *
 * It does NOT own the import button (the project-level useProjectServerImport
 * pushes vector indexes too), seeding its synced fingerprint to WHAT THE
 * SERVER HOLDS so a local-newer index is pushed up rather than stranded.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch } from 'react'

import {
  deleteVectorIndex,
  listVectorIndexes,
  saveVectorIndex,
} from '@/api/inqtrixClient'
import {
  autosaveDelayForVectorIndexes,
  serverVectorIndexPayload,
  vectorIndexChanged,
  vectorIndexFingerprint,
  vectorIndexRecordFromServer,
  type VectorIndexFingerprint,
} from '@/features/fileLibrary/vectorIndexSync'
import { deleteTolerant404, syncCollection } from '@/features/project/syncCollection'
import {
  useProjectSyncLifecycle,
  useSyncScope,
  type SyncLifecycleToken,
} from '@/features/project/useProjectSyncLifecycle'
import type { VectorIndexRecord } from '@/features/project/types'
import type { ResearchDeskAction } from '@/features/researchDesk/state'
import { messageFromError } from '@/api/requestErrorText'

const AUTOSAVE_DEBOUNCE_MS = 1_500
const PAGE_LIMIT = 200

type UseVectorIndexHistoryApiOptions = {
  apiKey: string | undefined
  dispatch: Dispatch<ResearchDeskAction>
  vectorIndexes: Record<string, VectorIndexRecord>
  /** In-session load counter (bumped on every wholesale project replace). Part
   * of the lifecycle identity so a switch to another synced project re-hydrates
   * from its own server state instead of inheriting this one's synced map. */
  projectEpoch: number
  /** ``serverSyncEnabled`` AND the durable capability AND not demo. */
  syncActive: boolean
  workspaceId: string
}

export type VectorIndexHistoryApiHandle = {
  /** Forget a record whose durable aggregate deletion already completed.
   * The following local reducer removal must not make generic autosave issue
   * a second DELETE for the same user action. */
  acknowledgeServerDeletion: (indexId: string) => void
  error: string | null
}

export function useVectorIndexHistoryApi({
  apiKey,
  dispatch,
  vectorIndexes,
  projectEpoch,
  syncActive,
  workspaceId,
}: UseVectorIndexHistoryApiOptions): VectorIndexHistoryApiHandle {
  const [error, setError] = useState<string | null>(null)
  const [hydrated, setHydrated] = useState(false)

  // Previous render's records for the membership-growth delay override.
  const autosavePrevIndexesRef = useRef(vectorIndexes)
  const indexesRef = useRef(vectorIndexes)
  indexesRef.current = vectorIndexes
  const optionsRef = useRef({ apiKey, workspaceId })
  optionsRef.current = { apiKey, workspaceId }

  const syncedRef = useRef(new Map<string, VectorIndexFingerprint>())
  const hydratedRef = useRef(false)
  const flushingRef = useRef(false)
  const flushPendingRef = useRef(false)
  const syncActiveRef = useRef(syncActive)
  syncActiveRef.current = syncActive
  const syncIdentity = `${workspaceId}:${projectEpoch}`
  const bindSyncPass = useSyncScope(syncActive, syncIdentity)

  const pushIndex = useCallback(async (index: VectorIndexRecord) => {
    await saveVectorIndex(index.id, serverVectorIndexPayload(index), optionsRef.current)
  }, [])

  const acknowledgeServerDeletion = useCallback((indexId: string) => {
    syncedRef.current.delete(indexId)
  }, [])

  const flush = useCallback(async () => {
    if (!syncActiveRef.current || !hydratedRef.current) return
    if (flushingRef.current) {
      flushPendingRef.current = true
      return
    }
    flushingRef.current = true
    const inScope = bindSyncPass()
    try {
      await syncCollection<VectorIndexRecord, VectorIndexFingerprint>({
        inScope,
        current: indexesRef.current,
        synced: syncedRef.current,
        fingerprintOf: vectorIndexFingerprint,
        changed: vectorIndexChanged,
        pushOne: pushIndex,
        deleteOne: (id) =>
          deleteTolerant404(
            async () => { await deleteVectorIndex(id, optionsRef.current) },
          ),
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
  }, [bindSyncPass, pushIndex])

  // -- reset + hydrate lifecycle (re-armed on project identity) ---------- #

  const reset = useCallback(() => {
    hydratedRef.current = false
    setHydrated(false)
    syncedRef.current.clear()
  }, [])

  const hydrate = useCallback((token: SyncLifecycleToken) => {
    void (async () => {
      try {
        const options = optionsRef.current
        const records: VectorIndexRecord[] = []
        let cursor: string | undefined
        do {
          const page = await listVectorIndexes({ ...options, cursor, limit: PAGE_LIMIT })
          for (const serverIndex of page.data) {
            records.push(vectorIndexRecordFromServer(serverIndex))
          }
          cursor = page.next_cursor ?? undefined
        } while (cursor)
        if (token.cancelled) return
        if (records.length > 0) {
          dispatch({ indexes: records, type: 'upsertServerVectorIndexes' })
        }
        // Seed each fingerprint to WHAT THE SERVER HOLDS; a local-newer index
        // then differs and the first autosave pushes it (unless mid-reindex,
        // which vectorIndexChanged defers until it reaches a terminal status).
        for (const record of records) {
          syncedRef.current.set(record.id, vectorIndexFingerprint(record))
        }
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

  // -- debounced autosave trigger --------------------------------------- #

  useEffect(() => {
    const previous = autosavePrevIndexesRef.current
    autosavePrevIndexesRef.current = vectorIndexes
    if (!syncActive || !hydrated) return
    const timer = setTimeout(() => {
      void flush()
    }, autosaveDelayForVectorIndexes(previous, vectorIndexes, AUTOSAVE_DEBOUNCE_MS))
    return () => clearTimeout(timer)
  }, [vectorIndexes, syncActive, hydrated, flush])

  return { acknowledgeServerDeletion, error }
}
