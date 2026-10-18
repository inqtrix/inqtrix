/**
 * File-library server sync (project-persistence tier).
 *
 * The file-library counterpart of useEditorHistoryApi, sharing the same
 * shape: hydrate section + group + asset METADATA on mount and a debounced
 * serialized autosave that diffs three collections via the shared
 * syncCollection helper. Persistence only — it never calls a model.
 *
 * The one structural difference from the editor: an asset's heavy
 * ``extractedText`` has no per-asset "open" event to hang a load on (the
 * library has no document viewer). Instead the body loads ON USE via the
 * exposed ``ensureAssetBodiesLoaded`` primitive: connected Chat and Editor
 * receive only the operation-fenced ``prepared_text`` while autosave still
 * preserves the separately editable ``extracted_text``. Incognito is the sole
 * local-body exception.
 *
 * It does NOT own the import button: the explicit opt-in push is the
 * project-level useProjectServerImport. This hook only hydrates + autosaves
 * once the project is opted in (syncActive), seeding its synced fingerprint
 * to WHAT THE SERVER HOLDS so a local-newer entity is pushed up rather than
 * stranded.
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { Dispatch } from 'react'

import {
  ensureDefaultAssetSections,
  getAsset,
  listUploadOperations,
  retryUploadOperation,
  listAssetGroups,
  listAssetSections,
  listAssets,
  saveAsset,
  saveAssetGroup,
  saveAssetSection,
} from '@/api/inqtrixClient'
import {
  assetAutosaveFingerprint,
  assetRecordFromServer,
  groupRecordFromServer,
  isAssetSettledForSync,
  sectionRecordFromServer,
  serverAssetPayload,
  serverGroupPayload,
  serverSectionPayload,
  visibleServerAssetSections,
} from '@/features/fileLibrary/assetSync'
import { observationRetryDelay, useStoppedObservations } from '@/features/project/observationRetry'
import type {
  FileAssetBodyLoadState,
  FileAssetRecord,
  FileGroupRecord,
  FileLibrarySectionRecord,
} from '@/features/project/types'
import { syncCollection } from '@/features/project/syncCollection'
import {
  useProjectSyncLifecycle,
  useSyncScope,
  type SyncLifecycleToken,
} from '@/features/project/useProjectSyncLifecycle'
import type { ResearchDeskAction } from '@/features/researchDesk/state'
import {
  defaultFileSectionIdReplacements,
  legacyFileSectionIdReplacements,
} from '@/features/files/sections'
import { carriedErrorText, messageFromError, readerError, readerTexts } from '@/api/requestErrorText'

const AUTOSAVE_DEBOUNCE_MS = 1_500
const PAGE_LIMIT = 200
const UPLOAD_POLL_INTERVAL_MS = 1_500

type UseAssetHistoryApiOptions = {
  apiKey: string | undefined
  dispatch: Dispatch<ResearchDeskAction>
  fileAssets: Record<string, FileAssetRecord>
  fileGroups: Record<string, FileGroupRecord>
  fileLibrarySections: Record<string, FileLibrarySectionRecord>
  /** In-session load counter (bumped on every wholesale project replace). Part
   * of the lifecycle identity so a switch to another synced project re-hydrates
   * from its own server state instead of inheriting this one's synced map. */
  projectEpoch: number
  /** ``serverSyncEnabled`` AND the durable capability AND not demo. */
  syncActive: boolean
  workspaceId: string
}

export type AssetHistoryApiHandle = {
  /** Truthful load-on-use projection for metadata-only server assets. */
  bodyLoadStates: Readonly<Record<string, FileAssetBodyLoadState>>
  error: string | null
  /** Resolve canonical server-prepared bodies on demand (deduped), keyed by
   * asset id. Local-only rows return extractedText solely so the caller's
   * explicit incognito policy can admit them. */
  ensureAssetBodiesLoaded: (assetIds: readonly string[]) => Promise<Map<string, string>>
  /** Record that the server just created/refreshed this asset OUTSIDE the
   * autosave (a bound upload's 201 carried the asset object). Seeds the
   * synced fingerprint so the regular diff owns the record from here on:
   * a later local delete issues the server DELETE (even if the row never
   * settled), a later settle pushes the changed record. Without this, a row
   * deleted mid-flight would resurrect from the server on the next hydrate. */
  noteServerAssetRecord: (assetId: string) => void
  /** The upload status poll stopped after repeated failed reads; it starts
   * again once the browser is online or the tab is visible again. */
  observationStopped: boolean
  /** Resume a durable dependency failure without resending bytes. If the
   * operation requires browser bytes, the typed API error is left intact so
   * the caller can ask the user to reselect the file. */
  retryUpload: (assetId: string) => Promise<void>
}

type LoadedAssetBodies = {
  extractedText: string
  preparedAt: string | null
  preparedContentHash: string | null
  preparedParserId: string | null
  preparedText: string
}

export function useAssetHistoryApi({
  apiKey,
  dispatch,
  fileAssets,
  fileGroups,
  fileLibrarySections,
  projectEpoch,
  syncActive,
  workspaceId,
}: UseAssetHistoryApiOptions): AssetHistoryApiHandle {
  const [error, setError] = useState<string | null>(null)
  const [uploadObservationError, setUploadObservationError] = useState<string | null>(null)
  const { observationStopped, stoppedObservations } = useStoppedObservations()
  const [bodyLoadStates, setBodyLoadStates] = useState<Record<string, FileAssetBodyLoadState>>({})
  const [hydrated, setHydrated] = useState(false)

  const assetsRef = useRef(fileAssets)
  assetsRef.current = fileAssets
  const groupsRef = useRef(fileGroups)
  groupsRef.current = fileGroups
  const sectionsRef = useRef(fileLibrarySections)
  sectionsRef.current = fileLibrarySections
  const optionsRef = useRef({ apiKey, workspaceId })
  optionsRef.current = { apiKey, workspaceId }

  const syncedSectionsRef = useRef(new Map<string, string>())
  const syncedGroupsRef = useRef(new Map<string, string>())
  const syncedAssetsRef = useRef(new Map<string, string>())
  // Assets whose extractedText is authoritative locally (loaded on use,
  // locally-present at hydrate, or just pushed) — never to be overwritten on
  // a later body fetch, and safe to PUT without first reading the server body.
  const loadedAssetsRef = useRef(new Set<string>())
  // Canonical prepared bodies are independently cached against their
  // server-owned content hash. A locally editable/extracted body never
  // satisfies this provenance boundary.
  const loadedPreparedAssetsRef = useRef(new Map<string, string>())
  // Server assets whose canonical body the server could not prepare (no
  // parser, a failed parse), by the server version that answered so. Asking
  // again before the asset changes gets the same answer; a caller that derives
  // its id list from the store would otherwise ask after every body it loaded.
  // A changed asset (a re-prepare bumps its version) is asked for again.
  const unpreparedAssetsRef = useRef(new Map<string, string>())
  // De-dupe concurrent body fetches per asset id (prefetch + send-await +
  // autosave guard can all want the same body at once).
  const inFlightBodyRef = useRef(new Map<string, Promise<LoadedAssetBodies>>())
  const hydratedRef = useRef(false)
  const flushingRef = useRef(false)
  const flushPendingRef = useRef(false)
  const syncActiveRef = useRef(syncActive)
  syncActiveRef.current = syncActive
  const syncIdentity = `${workspaceId}:${projectEpoch}`
  const bindSyncPass = useSyncScope(syncActive, syncIdentity)

  // -- body load-on-use (deduped) --------------------------------------- #

  const loadBody = useCallback((assetId: string): Promise<LoadedAssetBodies> => {
    const existing = inFlightBodyRef.current.get(assetId)
    if (existing) return existing
    setBodyLoadStates((current) => ({
      ...current,
      [assetId]: { error: null, status: 'loading' },
    }))
    const promise = (async () => {
      try {
        const detail = await getAsset(assetId, optionsRef.current)
        const record = assetRecordFromServer(detail)
        const bodies: LoadedAssetBodies = {
          extractedText: record.extractedText,
          preparedAt: record.preparedAt ?? null,
          preparedContentHash: record.preparedContentHash ?? null,
          preparedParserId: record.preparedParserId ?? null,
          preparedText: record.preparedText ?? '',
        }
        dispatch({ assetId, ...bodies, type: 'setServerAssetBody' })
        loadedAssetsRef.current.add(assetId)
        const hasPreparedProvenance = Boolean(
          bodies.preparedAt
          && bodies.preparedContentHash
          && bodies.preparedParserId,
        )
        if (hasPreparedProvenance && bodies.preparedContentHash) {
          loadedPreparedAssetsRef.current.set(
            assetId,
            bodies.preparedContentHash,
          )
        } else {
          loadedPreparedAssetsRef.current.delete(assetId)
        }
        setBodyLoadStates((current) => ({
          ...current,
          [assetId]: hasPreparedProvenance && bodies.preparedText.trim()
            ? { error: null, status: 'ready' }
            : {
                error: hasPreparedProvenance
                  ? 'Die serverseitig vorbereitete Dokumentquelle ist leer.'
                  : 'Die serverseitig vorbereitete Dokumentquelle ist nicht verfügbar.',
                status: 'failed',
              },
        }))
        return bodies
      } catch (error) {
        setBodyLoadStates((current) => ({
          ...current,
          [assetId]: { error: messageFromError(error), status: 'failed' },
        }))
        throw error
      }
    })()
    inFlightBodyRef.current.set(assetId, promise)
    void promise.finally(() => {
      if (inFlightBodyRef.current.get(assetId) === promise) {
        inFlightBodyRef.current.delete(assetId)
      }
    })
    return promise
  }, [dispatch])

  const ensureAssetBodiesLoaded = useCallback(
    async (assetIds: readonly string[]): Promise<Map<string, string>> => {
      const result = new Map<string, string>()
      await Promise.all(
        [...new Set(assetIds)].map(async (id) => {
          const asset = assetsRef.current[id]
          if (!asset) return
          // Local-only content is admitted only by the caller's explicit
          // incognito policy. For a server asset, only a prepared body loaded
          // under the current canonical hash can satisfy this method.
          if (!syncedAssetsRef.current.has(id)) {
            result.set(id, asset.extractedText)
            setBodyLoadStates((current) => ({
              ...current,
              [id]: asset.extractedText.trim()
                ? { error: null, status: 'ready' }
                : { error: null, status: 'failed' },
            }))
            return
          }
          if (
            asset.preparedContentHash
            && loadedPreparedAssetsRef.current.get(id)
              === asset.preparedContentHash
          ) {
            result.set(id, asset.preparedText ?? '')
            return
          }
          if (unpreparedAssetsRef.current.get(id) === asset.updatedAt) {
            result.set(id, asset.preparedText ?? '')
            return
          }
          const bodies = await loadBody(id)
          if (loadedPreparedAssetsRef.current.has(id)) {
            unpreparedAssetsRef.current.delete(id)
          } else {
            unpreparedAssetsRef.current.set(id, asset.updatedAt)
          }
          result.set(id, bodies.preparedText)
        }),
      )
      return result
    },
    [loadBody],
  )

  // -- pushing one entity ----------------------------------------------- #

  const pushAsset = useCallback(async (asset: FileAssetRecord) => {
    let record = asset
    if (
      syncedAssetsRef.current.has(asset.id) &&
      !loadedAssetsRef.current.has(asset.id)
    ) {
      // A metadata-only edit (group-orphan on group delete, section move)
      // bumped this server-held asset's updatedAt while its body was never
      // loaded (still ""). The PUT is a full-record upsert, so sending
      // extractedText="" would ERASE the server body. Fetch it first and push
      // the merged record (new metadata + kept body).
      const bodies = await loadBody(asset.id)
      record = { ...asset, extractedText: bodies.extractedText }
    }
    await saveAsset(record.id, serverAssetPayload(record), optionsRef.current)
    dispatch({ assetId: record.id, type: 'markFileAssetServerSynced' })
    loadedAssetsRef.current.add(record.id)
  }, [dispatch, loadBody])

  const pushSection = useCallback(async (section: FileLibrarySectionRecord) => {
    await saveAssetSection(section.id, serverSectionPayload(section), optionsRef.current)
    dispatch({ sectionId: section.id, type: 'markFileLibrarySectionServerSynced' })
  }, [dispatch])

  const pushGroup = useCallback(async (group: FileGroupRecord) => {
    await saveAssetGroup(group.id, serverGroupPayload(group), optionsRef.current)
    dispatch({ groupId: group.id, type: 'markFileGroupServerSynced' })
  }, [dispatch])

  // -- autosave flush (debounced, serialized) --------------------------- #

  const flush = useCallback(async () => {
    if (!syncActiveRef.current || !hydratedRef.current) return
    if (flushingRef.current) {
      flushPendingRef.current = true
      return
    }
    flushingRef.current = true
    const inScope = bindSyncPass()
    try {
      // Parent-first so a freshly created section exists before its groups and
      // a group before its assets (the FK order). On delete the order is
      // harmless: a section delete cascades its groups + assets server-side,
      // and the child passes below tolerate the resulting 404s.
      await syncCollection<FileLibrarySectionRecord, string>({
        inScope,
        current: sectionsRef.current,
        synced: syncedSectionsRef.current,
        fingerprintOf: (section) => section.updatedAt,
        changed: (previous, current) => previous !== current,
        pushOne: pushSection,
        // Destructive section cleanup is an explicit durable operation. A
        // local diff is not authority to launch it after the UI row vanished.
        deleteOne: async () => undefined,
      })
      await syncCollection<FileGroupRecord, string>({
        inScope,
        current: groupsRef.current,
        synced: syncedGroupsRef.current,
        fingerprintOf: (group) => group.updatedAt,
        changed: (previous, current) => previous !== current,
        pushOne: pushGroup,
        // Group removal is an explicit durable deletion operation. The row
        // remains addressable until the operation feed confirms `deleted`.
        deleteOne: async () => undefined,
      })
      await syncCollection<FileAssetRecord, string>({
        inScope,
        // NEW, still-unsettled rows (mid-upload/mid-parse, unknown to the
        // synced map) stay out of the diff: their server state is owned by
        // the bound upload + settle actions, and a premature PUT would race
        // the pre-flight/binding with an empty body. An id the synced map
        // ALREADY knows must stay in `current` even while transiently
        // pending again (retry, hydrate-mid-batch, bound upload) — dropping
        // it would read as a local delete and trigger a real server DELETE.
        // Its fingerprint remains at the confirmed server value until both
        // transient flags clear, so retaining it for presence cannot also
        // authorize a half-built full-record PUT. The kept invariant: an id
        // filtered out here is never in `synced`, so the delete loop cannot
        // see it.
        current: Object.fromEntries(
          Object.entries(assetsRef.current).filter(
            ([, asset]) => isAssetSettledForSync(asset) || syncedAssetsRef.current.has(asset.id),
          ),
        ),
        synced: syncedAssetsRef.current,
        fingerprintOf: (asset) => assetAutosaveFingerprint(
          asset,
          syncedAssetsRef.current.get(asset.id),
        ),
        changed: (previous, current) => previous !== current,
        pushOne: pushAsset,
        // The explicit deletion coordinator removes server aggregates first;
        // this callback only retires local hydration caches after terminal
        // confirmation. Absence from client state is never deletion authority.
        deleteOne: async (id) => {
          loadedAssetsRef.current.delete(id)
          loadedPreparedAssetsRef.current.delete(id)
          unpreparedAssetsRef.current.delete(id)
          inFlightBodyRef.current.delete(id)
        },
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
  }, [bindSyncPass, pushAsset, pushGroup, pushSection])

  // -- reset + hydrate lifecycle (re-armed on project identity) ---------- #

  const reset = useCallback(() => {
    hydratedRef.current = false
    setHydrated(false)
    setUploadObservationError(null)
    syncedSectionsRef.current.clear()
    syncedGroupsRef.current.clear()
    syncedAssetsRef.current.clear()
    loadedAssetsRef.current.clear()
    loadedPreparedAssetsRef.current.clear()
    unpreparedAssetsRef.current.clear()
    inFlightBodyRef.current.clear()
    setBodyLoadStates({})
  }, [])

  const hydrate = useCallback((token: SyncLifecycleToken) => {
    void (async () => {
      try {
        const options = optionsRef.current
        // This server-owned idempotency point runs before the generic list.
        // Concurrent first tabs receive the same role IDs instead of
        // autosaving their independent opaque bootstrap IDs.
        let sectionRecords: FileLibrarySectionRecord[] = []
        for (let attempt = 0; attempt < 2; attempt += 1) {
          await ensureDefaultAssetSections(options)
          sectionRecords = (await listAssetSections(options)).map(sectionRecordFromServer)
          const observedRoles = new Set(
            sectionRecords.map((record) => record.semanticRole),
          )
          if (
            observedRoles.has('temporary')
            && observedRoles.has('library')
            && observedRoles.has('project_sources')
          ) break
          if (attempt === 1) {
            throw readerError('sectionsInconsistent')
          }
        }
        // Use only the later list observation for rekeying. The ensure
        // response could already be stale if another tab renamed (and thereby
        // released) a prepared role between the two requests.
        const canonicalSectionRecords = sectionRecords.filter(
          (record) => record.semanticRole === 'temporary'
            || record.semanticRole === 'library'
            || record.semanticRole === 'project_sources',
        )
        const groupRecords = (await listAssetGroups(options)).map(groupRecordFromServer)
        const assetRecords: FileAssetRecord[] = []
        let cursor: string | undefined
        do {
          const page = await listAssets({ ...options, cursor, limit: PAGE_LIMIT })
          for (const serverAsset of page.data) {
            assetRecords.push(assetRecordFromServer(serverAsset))
          }
          cursor = page.next_cursor ?? undefined
        } while (cursor)
        if (token.cancelled) return
        const visibleSectionRecords = visibleServerAssetSections(
          sectionRecords,
          groupRecords,
          assetRecords,
        )
        const visibleSectionIds = new Set(
          visibleSectionRecords.map((record) => record.id),
        )
        const hiddenServerIds = sectionRecords
          .filter((record) => !visibleSectionIds.has(record.id))
          .map((record) => record.id)
        const sectionReplacements = {
          ...legacyFileSectionIdReplacements(
            sectionsRef.current,
            new Set(sectionRecords.map((record) => record.id)),
          ),
          ...defaultFileSectionIdReplacements(
            sectionsRef.current,
            canonicalSectionRecords,
          ),
        }
        if (Object.keys(sectionReplacements).length > 0) {
          dispatch({ replacements: sectionReplacements, type: 'rekeyFileLibrarySectionIds' })
        }
        // Assets already in the local project carry an authoritative body
        // (loaded from the markdown). Captured BEFORE the merge dispatch so a
        // server-only asset (hydrated with extractedText="") is distinguishable.
        const localAssetsBeforeHydrate = assetsRef.current
        const locallyPresentIds = new Set(Object.keys(localAssetsBeforeHydrate))
        if (visibleSectionRecords.length > 0) {
          dispatch({
            sections: visibleSectionRecords,
            type: 'upsertServerAssetSections',
          })
        }
        if (sectionRecords.length > 0) {
          dispatch({
            hiddenServerIds,
            serverHasTemporarySection: sectionRecords.some(
              (record) => record.kind === 'temporary',
            ),
            serverIds: sectionRecords.map((record) => record.id),
            type: 'pruneLocalBootstrapFileSections',
          })
        }
        if (groupRecords.length > 0) {
          dispatch({ groups: groupRecords, type: 'upsertServerAssetGroups' })
        }
        if (assetRecords.length > 0) {
          dispatch({ assets: assetRecords, type: 'upsertServerAssetMetadata' })
        }
        // Seed each fingerprint to WHAT THE SERVER HOLDS (its updatedAt); a
        // local-newer entity then differs and the first autosave pushes it.
        // Intentionally track only the visible projection. Historical,
        // unreferenced bootstrap duplicates are absent from both ProjectState
        // and this synced map, so the generic diff neither PUTs nor DELETEs
        // them. A browser-side delete would race another client and the
        // endpoint cascades children; persisted cleanup therefore needs a
        // future atomic server-side "delete only while still empty" operation.
        for (const record of visibleSectionRecords) {
          syncedSectionsRef.current.set(record.id, record.updatedAt)
        }
        for (const record of groupRecords) {
          syncedGroupsRef.current.set(record.id, record.updatedAt)
        }
        for (const record of assetRecords) {
          syncedAssetsRef.current.set(record.id, record.updatedAt)
          if (locallyPresentIds.has(record.id)) {
            // Its body is authoritative locally: never overwrite it on a later
            // fetch, and it may be pushed without first reading the server body.
            loadedAssetsRef.current.add(record.id)
            const local = localAssetsBeforeHydrate[record.id]
            if (
              record.preparedContentHash
              && local?.preparedContentHash === record.preparedContentHash
              && local.preparedText !== undefined
            ) {
              loadedPreparedAssetsRef.current.set(
                record.id,
                record.preparedContentHash,
              )
            }
          }
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

  // -- durable upload convergence -------------------------------------- #

  const uploadPollKey = Object.values(fileAssets)
    .filter((asset) => asset.serverSynced === true && (
      asset.uploadPending
      || asset.uploadStatus === 'awaiting_upload'
      || asset.uploadStatus === 'retrying'
      || asset.uploadStatus === 'parsing'
      || asset.uploadStatus === 'finalizing'
    ))
    .map((asset) => `${asset.id}:${asset.uploadOperationId ?? ''}:${asset.uploadStatus ?? ''}`)
    .sort()
    .join('|')

  useEffect(() => {
    if (!syncActive || !hydrated) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | null = null
    let failures = 0
    let polling = false

    const poll = async () => {
      if (polling || controller.signal.aborted) return
      polling = true
      try {
        const options = { ...optionsRef.current, signal: controller.signal }
        const feed = await listUploadOperations(options)
        if (controller.signal.aborted) return
        const local = assetsRef.current
        const ids = new Set<string>()
        for (const asset of Object.values(local)) {
          if (asset.serverSynced === true && asset.uploadPending) ids.add(asset.id)
        }
        for (const operation of feed.data) {
          if (local[operation.asset_id]) ids.add(operation.asset_id)
        }
        const settled = await Promise.allSettled(
          [...ids].map((id) => getAsset(id, options)),
        )
        if (controller.signal.aborted) return
        const records = settled.flatMap((result) => (
          result.status === 'fulfilled' ? [assetRecordFromServer(result.value)] : []
        ))
        if (records.length > 0) {
          dispatch({ assets: records, type: 'upsertServerAssetMetadata' })
          for (const record of records) {
            const preparedAt = record.preparedAt ?? null
            const preparedContentHash = record.preparedContentHash ?? null
            const preparedParserId = record.preparedParserId ?? null
            const preparedText = record.preparedText ?? ''
            dispatch({
              assetId: record.id,
              extractedText: record.extractedText,
              preparedAt,
              preparedContentHash,
              preparedParserId,
              preparedText,
              type: 'setServerAssetBody',
            })
            loadedAssetsRef.current.add(record.id)
            const hasPreparedProvenance = Boolean(
              preparedAt && preparedContentHash && preparedParserId,
            )
            if (hasPreparedProvenance && preparedContentHash) {
              loadedPreparedAssetsRef.current.set(
                record.id,
                preparedContentHash,
              )
            } else {
              loadedPreparedAssetsRef.current.delete(record.id)
            }
            setBodyLoadStates((current) => ({
              ...current,
              [record.id]: hasPreparedProvenance && preparedText.trim()
                ? { error: null, status: 'ready' }
                : {
                    error: hasPreparedProvenance
                      ? 'Die serverseitig vorbereitete Dokumentquelle ist leer.'
                      : 'Die serverseitig vorbereitete Dokumentquelle ist nicht verfügbar.',
                    status: 'failed',
                  },
            }))
            if (
              record.uploadStatus === 'ready'
              && hasPreparedProvenance
            ) {
              dispatch({
                assetId: record.id,
                pending: false,
                type: 'setFileAssetParsePending',
              })
            }
          }
        }
        const stillRunning = records.some((record) => (
          record.uploadPending && record.uploadStatus !== 'awaiting_upload'
        ))
          || feed.data.some((operation) => (
            Boolean(local[operation.asset_id])
            && (operation.status === 'running' || operation.status === 'queued')
          ))
        failures = 0
        setUploadObservationError(null)
        if (stillRunning && !controller.signal.aborted) {
          timer = setTimeout(() => void poll(), UPLOAD_POLL_INTERVAL_MS)
        }
      } catch (caught) {
        if (controller.signal.aborted) return
        setUploadObservationError(messageFromError(caught))
        // A failed status read is not an upload failure. Keep the operation
        // visible and retry observation; never rewrite it to a local success.
        failures += 1
        const delay = observationRetryDelay(failures, UPLOAD_POLL_INTERVAL_MS)
        if (delay === null) {
          stoppedObservations.stop('uploads', () => {
            failures = 0
            void poll()
          })
        } else {
          timer = setTimeout(() => void poll(), delay)
        }
      } finally {
        polling = false
      }
    }

    void poll()
    return () => {
      controller.abort()
      if (timer !== null) clearTimeout(timer)
      stoppedObservations.clear()
    }
  }, [dispatch, hydrated, projectEpoch, stoppedObservations, syncActive, uploadPollKey])

  // -- bound-upload bookkeeping ------------------------------------------ #

  const noteServerAssetRecord = useCallback((assetId: string) => {
    const asset = assetsRef.current[assetId]
    if (asset) {
      // The server record mirrors the local placeholder (the binding carried
      // its timestamps, the body is empty on both sides): seed the synced
      // fingerprint to the local updatedAt so nothing re-pushes until a real
      // change, and mark the body authoritative locally.
      syncedAssetsRef.current.set(assetId, asset.updatedAt)
      loadedAssetsRef.current.add(assetId)
      return
    }
    // The row was deleted while its upload was in flight: the server now
    // holds a record the user already removed. Seeding an impossible
    // fingerprint puts the id into the delete diff — the next flush issues
    // the compensating server DELETE.
    syncedAssetsRef.current.set(assetId, 'bound-upload-orphan')
    void flush()
  }, [flush])

  const retryUpload = useCallback(async (assetId: string) => {
    const asset = assetsRef.current[assetId]
    if (!asset?.uploadOperationId) {
      throw readerError('uploadNotResumable')
    }
    const operation = await retryUploadOperation(
      asset.uploadOperationId,
      optionsRef.current,
    )
    dispatch({
      assetId,
      error: carriedErrorText(operation.error, readerTexts().fileLibrary.uploadFailedLabel),
      operationId: operation.operation_id,
      serverFileId: asset.serverFileId ?? null,
      status: operation.status === 'ready' ? 'ready' : 'retrying',
      type: 'adoptFileAssetUploadLifecycle',
    })
  }, [dispatch])

  // -- debounced autosave trigger --------------------------------------- #

  useEffect(() => {
    if (!syncActive || !hydrated) return
    // A just-settled, never-synced asset (fresh upload whose parse landed)
    // flushes immediately: its body should reach the server promptly rather
    // than wait out a debounce that every pipeline dispatch keeps resetting.
    const hasFreshSettledAsset = Object.values(fileAssets).some(
      (asset) => isAssetSettledForSync(asset) && !syncedAssetsRef.current.has(asset.id),
    )
    const timer = setTimeout(() => {
      void flush()
    }, hasFreshSettledAsset ? 0 : AUTOSAVE_DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [fileLibrarySections, fileGroups, fileAssets, syncActive, hydrated, flush])

  return {
    bodyLoadStates,
    error: uploadObservationError ?? error,
    ensureAssetBodiesLoaded,
    noteServerAssetRecord,
    observationStopped,
    retryUpload,
  }
}
