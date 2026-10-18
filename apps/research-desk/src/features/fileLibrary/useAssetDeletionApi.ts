import { useCallback, useEffect, useRef, useState, type Dispatch } from 'react'

import {
  deleteAsset,
  deleteAssetGroup,
  deleteAssets,
  deleteAssetSection,
  deleteKnowledgeCollection,
  deleteKnowledgeDocument,
  getAssetDeletionOperation,
  listAssetDeletionOperations,
  retryAssetDeletionOperation,
  type ClientOptions,
  type ServerDeletionOperation,
} from '@/api/inqtrixClient'
import { observationRetryDelay, useStoppedObservations } from '@/features/project/observationRetry'
import {
  DELETION_RETRY_BASE_MS,
  nextDeletionPollDelayMs,
} from '@/features/project/sessionDeletion'
import type {
  FileAssetRecord,
  FileGroupRecord,
  FileLibrarySectionRecord,
} from '@/features/project/types'
import type { ResearchDeskAction } from '@/features/researchDesk/state'
import { deleteVectorIndexAggregate } from './vectorIndexDeletion'
import { carriedErrorText, readerError, readerTexts } from '@/api/requestErrorText'

const OPERATION_PAGE_LIMIT = 200

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Abort-aware polling delay. The listener is removed on both timeout and
 * abort, so a long-lived operation does not accumulate one listener per poll.
 */
export function waitForAssetDeletionPoll(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve()
      return
    }
    let timer: ReturnType<typeof setTimeout> | null = null
    let settled = false
    const finish = () => {
      if (settled) return
      settled = true
      if (timer !== null) clearTimeout(timer)
      signal.removeEventListener('abort', finish)
      resolve()
    }
    timer = setTimeout(finish, ms)
    signal.addEventListener('abort', finish, { once: true })
  })
}

type ScopedController = {
  controller: AbortController
  scopeKey: string
}

/**
 * Owns poll cancellation and scope fencing independently from React renders.
 * A project/principal/backend switch aborts every request from the old scope;
 * late completions are additionally rejected by `isCurrent`.
 */
export function createAssetDeletionPollingScope() {
  let activeScopeKey: string | null = null
  const controllers = new Map<string, ScopedController>()

  const reset = (scopeKey: string | null) => {
    controllers.forEach(({ controller }) => controller.abort())
    controllers.clear()
    activeScopeKey = scopeKey
  }

  return {
    isCurrent: (scopeKey: string) => activeScopeKey === scopeKey,
    open: (operationId: string, scopeKey: string): AbortController | null => {
      if (activeScopeKey !== scopeKey || controllers.has(operationId)) return null
      const controller = new AbortController()
      controllers.set(operationId, { controller, scopeKey })
      return controller
    },
    release: (operationId: string, controller: AbortController) => {
      if (controllers.get(operationId)?.controller === controller) {
        controllers.delete(operationId)
      }
    },
    reset,
    stop: (operationId: string, scopeKey?: string) => {
      const entry = controllers.get(operationId)
      if (!entry || (scopeKey !== undefined && entry.scopeKey !== scopeKey)) return
      entry.controller.abort()
      controllers.delete(operationId)
    },
  }
}

export type AssetDeletionApi = {
  error: string | null
  /** A receipt poll stopped after repeated failed reads; it starts again
   * once the browser is online or the tab is visible again. */
  observationStopped: boolean
  operations: Readonly<Record<string, ServerDeletionOperation>>
  retry: (operationId: string) => Promise<void>
  startAssets: (assetIds: readonly string[]) => Promise<void>
  startGroup: (groupId: string) => Promise<void>
  startKnowledgeCollection: (collectionId: string) => Promise<void>
  startKnowledgeDocument: (documentId: string) => Promise<void>
  startSection: (sectionId: string, assetIds: readonly string[]) => Promise<void>
  startVectorIndex: (indexId: string, collectionId?: string | null) => Promise<void>
}

export type DeletionTransportOptions = {
  asset: ClientOptions | null
  knowledge: ClientOptions | null
  operations: ClientOptions | null
}

/**
 * Asset persistence and Knowledge persistence are independent capabilities.
 * Local-only assets must stay local, while a real Knowledge backend still
 * owns its document, collection and vector-index deletion operations.
 */
export function resolveDeletionTransportOptions(
  assetOptions: ClientOptions | null,
  knowledgeOptions?: ClientOptions | null,
): DeletionTransportOptions {
  const knowledge = knowledgeOptions === undefined ? assetOptions : knowledgeOptions
  return {
    asset: assetOptions,
    knowledge,
    operations: knowledge ?? assetOptions,
  }
}

/**
 * Client projection of the durable server deletion state machine.
 *
 * Polling has no business timeout: dependency timeouts and failures are
 * terminal server states and remain visible. A failed read slows the next
 * one, and ten in a row stop the poll until the browser is online or the tab
 * visible again; it never turns an unknown outcome into success.
 * `scopeKey` is the stable project/principal/backend identity supplied by the
 * caller; changing it fences every request and clears the old projection.
 */
export function useAssetDeletionApi({
  assets,
  dispatch,
  groups,
  knowledgeOptions,
  options,
  refreshToken = 0,
  scopeKey,
  sections,
}: {
  assets: Readonly<Record<string, FileAssetRecord>>
  dispatch: Dispatch<ResearchDeskAction>
  groups: Readonly<Record<string, FileGroupRecord>>
  knowledgeOptions?: ClientOptions | null
  options: ClientOptions | null
  refreshToken?: number
  scopeKey: string
  sections: Readonly<Record<string, FileLibrarySectionRecord>>
}): AssetDeletionApi {
  const [error, setError] = useState<string | null>(null)
  const [operations, setOperations] = useState<Record<string, ServerDeletionOperation>>({})
  const { observationStopped, stoppedObservations } = useStoppedObservations()
  const pollingRef = useRef<ReturnType<typeof createAssetDeletionPollingScope> | null>(null)
  if (pollingRef.current === null) pollingRef.current = createAssetDeletionPollingScope()
  const polling = pollingRef.current
  const transports = resolveDeletionTransportOptions(options, knowledgeOptions)
  const assetOptionsRef = useRef(transports.asset)
  assetOptionsRef.current = transports.asset
  const knowledgeOptionsRef = useRef(transports.knowledge)
  knowledgeOptionsRef.current = transports.knowledge
  const operationOptionsRef = useRef(transports.operations)
  operationOptionsRef.current = transports.operations
  const assetsRef = useRef(assets)
  assetsRef.current = assets

  // Derive the transport scope too, so an accidentally under-specified caller
  // key still cannot carry work across a workspace, backend, or offline flip.
  const requestScopeKey = [
    scopeKey,
    transports.asset ? 'asset-server' : 'asset-local',
    transports.asset?.baseUrl ?? '',
    transports.asset?.workspaceId ?? '',
    transports.knowledge ? 'knowledge-server' : 'knowledge-local',
    transports.knowledge?.baseUrl ?? '',
    transports.knowledge?.workspaceId ?? '',
  ].join('\u001f')
  const renderedScopeRef = useRef(requestScopeKey)
  renderedScopeRef.current = requestScopeKey

  const stopPolling = useCallback((operationId: string, expectedScopeKey?: string) => {
    polling.stop(operationId, expectedScopeKey)
    stoppedObservations.drop(operationId)
  }, [polling, stoppedObservations])

  const applyOperation = useCallback((
    operation: ServerDeletionOperation,
    expectedScopeKey: string,
  ): boolean => {
    if (renderedScopeRef.current !== expectedScopeKey) return false
    setOperations((current) => (
      current[operation.operation_id] === operation
        ? current
        : { ...current, [operation.operation_id]: operation }
    ))
    const message = carriedErrorText(operation.error, readerTexts().fileLibrary.deletionFailed)
    const projectedStatus = operation.status === 'delete_failed'
      ? 'delete_failed'
      : operation.status === 'queued'
        ? 'queued'
        : 'running'

    // Project the receipt before applying its terminal transition. Reducer
    // guards bind it to the same operation and only let retained receipts
    // claim rows with explicit server-synced provenance.
    dispatch({
      error: message,
      fileIds: operation.asset_ids,
      operationId: operation.operation_id,
      stage: operation.stage,
      status: projectedStatus,
      type: 'setFileAssetDeletionState',
    })
    if (operation.target_kind === 'section') {
      dispatch({
        error: message,
        operationId: operation.operation_id,
        sectionId: operation.target_id,
        stage: operation.stage,
        status: projectedStatus,
        type: 'setFileLibrarySectionDeletionState',
      })
    }
    if (operation.target_kind === 'group') {
      dispatch({
        error: message,
        groupId: operation.target_id,
        operationId: operation.operation_id,
        stage: operation.stage,
        status: projectedStatus,
        type: 'setFileGroupDeletionState',
      })
    }

    if (operation.status === 'deleted') {
      stopPolling(operation.operation_id, expectedScopeKey)
      dispatch({
        fileIds: operation.asset_ids,
        operationId: operation.operation_id,
        type: 'completeFileAssetDeletion',
      })
      if (operation.target_kind === 'section') {
        dispatch({
          operationId: operation.operation_id,
          sectionId: operation.target_id,
          type: 'completeFileLibrarySectionDeletion',
        })
      }
      if (operation.target_kind === 'group') {
        dispatch({
          groupId: operation.target_id,
          operationId: operation.operation_id,
          type: 'completeFileGroupDeletion',
        })
      }
      return true
    }
    if (operation.status === 'delete_failed') {
      stopPolling(operation.operation_id, expectedScopeKey)
    }
    return true
  }, [dispatch, stopPolling])

  const poll = useCallback((operationId: string, expectedScopeKey: string) => {
    const activeOptions = operationOptionsRef.current
    if (!activeOptions) return
    const controller = polling.open(operationId, expectedScopeKey)
    if (!controller) return
    stoppedObservations.drop(operationId)
    void (async () => {
      let delay = nextDeletionPollDelayMs(0)
      let completedPolls = 0
      let failures = 0
      try {
        while (!controller.signal.aborted) {
          await waitForAssetDeletionPoll(delay, controller.signal)
          if (controller.signal.aborted || !polling.isCurrent(expectedScopeKey)) return
          try {
            const currentOptions = operationOptionsRef.current
            if (!currentOptions) return
            const operation = await getAssetDeletionOperation(operationId, {
              ...currentOptions,
              signal: controller.signal,
            })
            if (!applyOperation(operation, expectedScopeKey)) return
            setError(null)
            if (operation.status === 'deleted' || operation.status === 'delete_failed') return
            failures = 0
            completedPolls += 1
            delay = nextDeletionPollDelayMs(completedPolls)
          } catch (caught) {
            if (controller.signal.aborted || !polling.isCurrent(expectedScopeKey)) return
            setError(errorMessage(caught))
            failures += 1
            const retryDelay = observationRetryDelay(failures, DELETION_RETRY_BASE_MS)
            if (retryDelay === null) {
              stoppedObservations.stop(operationId, () => poll(operationId, expectedScopeKey))
              return
            }
            delay = retryDelay
          }
        }
      } finally {
        polling.release(operationId, controller)
      }
    })()
  }, [applyOperation, polling, stoppedObservations])

  const track = useCallback((
    operation: ServerDeletionOperation,
    expectedScopeKey: string,
  ) => {
    if (!applyOperation(operation, expectedScopeKey)) return
    if (operation.status === 'queued' || operation.status === 'running') {
      poll(operation.operation_id, expectedScopeKey)
    }
  }, [applyOperation, poll])

  const startAssets = useCallback(async (assetIds: readonly string[]) => {
    const ids = [...new Set(assetIds)].filter((assetId) => Boolean(assetsRef.current[assetId]))
    if (ids.length === 0) return
    const activeOptions = assetOptionsRef.current
    if (!activeOptions) {
      dispatch({ fileIds: ids, type: 'deleteFileAssets' })
      return
    }
    const expectedScopeKey = renderedScopeRef.current
    try {
      const operation = ids.length === 1
        ? await deleteAsset(ids[0], activeOptions)
        : await deleteAssets(ids, activeOptions)
      if (renderedScopeRef.current !== expectedScopeKey) return
      setError(null)
      track(operation, expectedScopeKey)
    } catch (caught) {
      if (renderedScopeRef.current !== expectedScopeKey) return
      setError(errorMessage(caught))
      throw caught
    }
  }, [dispatch, track])

  const startSection = useCallback(async (
    sectionId: string,
    assetIds: readonly string[],
  ) => {
    const activeOptions = assetOptionsRef.current
    if (!activeOptions) {
      dispatch({ sectionId, type: 'deleteFileLibrarySection' })
      return
    }
    const expectedScopeKey = renderedScopeRef.current
    try {
      const operation = await deleteAssetSection(sectionId, activeOptions)
      if (renderedScopeRef.current !== expectedScopeKey) return
      setError(null)
      // Compatibility with an older server response that omitted the manifest
      // projection. The durable feed always returns the canonical asset ids.
      track({
        ...operation,
        asset_ids: operation.asset_ids.length > 0 ? operation.asset_ids : [...assetIds],
      }, expectedScopeKey)
    } catch (caught) {
      if (renderedScopeRef.current !== expectedScopeKey) return
      setError(errorMessage(caught))
      throw caught
    }
  }, [dispatch, track])

  const startGroup = useCallback(async (groupId: string) => {
    if (!groups[groupId]) return
    const activeOptions = assetOptionsRef.current
    if (!activeOptions) {
      dispatch({ groupId, type: 'deleteFileGroup' })
      return
    }
    const expectedScopeKey = renderedScopeRef.current
    try {
      const operation = await deleteAssetGroup(groupId, activeOptions)
      if (renderedScopeRef.current !== expectedScopeKey) return
      setError(null)
      track(operation, expectedScopeKey)
    } catch (caught) {
      if (renderedScopeRef.current !== expectedScopeKey) return
      setError(errorMessage(caught))
      throw caught
    }
  }, [dispatch, groups, track])

  const startVectorIndex = useCallback(async (
    indexId: string,
    collectionId?: string | null,
  ) => {
    const activeOptions = knowledgeOptionsRef.current
    if (!activeOptions) {
      throw readerError('deletionNeedsServer')
    }
    const expectedScopeKey = renderedScopeRef.current
    try {
      const operation = await deleteVectorIndexAggregate({
        collectionId,
        indexId,
        options: activeOptions,
      })
      if (renderedScopeRef.current !== expectedScopeKey) return
      setError(null)
      track(operation, expectedScopeKey)
    } catch (caught) {
      if (renderedScopeRef.current !== expectedScopeKey) return
      setError(errorMessage(caught))
      throw caught
    }
  }, [track])

  const startKnowledgeCollection = useCallback(async (collectionId: string) => {
    const activeOptions = knowledgeOptionsRef.current
    if (!activeOptions) {
      throw readerError('deletionNeedsServer')
    }
    const expectedScopeKey = renderedScopeRef.current
    try {
      const operation = await deleteKnowledgeCollection(collectionId, activeOptions)
      if (renderedScopeRef.current !== expectedScopeKey) return
      setError(null)
      track(operation, expectedScopeKey)
    } catch (caught) {
      if (renderedScopeRef.current !== expectedScopeKey) return
      setError(errorMessage(caught))
      throw caught
    }
  }, [track])

  const startKnowledgeDocument = useCallback(async (documentId: string) => {
    const activeOptions = knowledgeOptionsRef.current
    if (!activeOptions) {
      throw readerError('deletionNeedsServer')
    }
    const expectedScopeKey = renderedScopeRef.current
    try {
      const operation = await deleteKnowledgeDocument(documentId, activeOptions)
      if (renderedScopeRef.current !== expectedScopeKey) return
      setError(null)
      track(operation, expectedScopeKey)
    } catch (caught) {
      if (renderedScopeRef.current !== expectedScopeKey) return
      setError(errorMessage(caught))
      throw caught
    }
  }, [track])

  const retry = useCallback(async (operationId: string) => {
    const activeOptions = operationOptionsRef.current
    if (!activeOptions) {
      throw readerError('deletionNeedsServer')
    }
    const expectedScopeKey = renderedScopeRef.current
    try {
      const operation = await retryAssetDeletionOperation(operationId, activeOptions)
      if (renderedScopeRef.current !== expectedScopeKey) return
      setError(null)
      track(operation, expectedScopeKey)
    } catch (caught) {
      if (renderedScopeRef.current !== expectedScopeKey) return
      setError(errorMessage(caught))
      throw caught
    }
  }, [track])

  // Scope changes invalidate old operations immediately and abort their
  // network work before hydration for the new project begins.
  useEffect(() => {
    polling.reset(requestScopeKey)
    setOperations({})
    setError(null)
    return () => {
      polling.reset(null)
      stoppedObservations.clear()
    }
  }, [polling, requestScopeKey, stoppedObservations])

  // Resume in-flight operations already projected on rows even if the feed is
  // temporarily unavailable. Failed operations remain still until retry.
  useEffect(() => {
    if (!operationOptionsRef.current || !polling.isCurrent(requestScopeKey)) return
    const operationIds = new Set([
      ...Object.values(assets)
        .filter((asset) => asset.lifecycleStatus === 'deleting')
        .map((asset) => asset.deletionOperationId),
      ...Object.values(sections)
        .filter((section) => section.lifecycleStatus === 'deleting')
        .map((section) => section.deletionOperationId),
      ...Object.values(groups)
        .filter((group) => group.lifecycleStatus === 'deleting')
        .map((group) => group.deletionOperationId),
    ].filter((value): value is string => Boolean(value)))
    operationIds.forEach((operationId) => poll(operationId, requestScopeKey))
  }, [assets, groups, poll, polling, requestScopeKey, sections])

  // Hydration requests and the operation feed are independent. A list
  // response captured just before server cleanup may therefore arrive after a
  // terminal receipt and temporarily re-add the old row. Re-project retained
  // receipts whenever those slices change; provenance + exact-operation
  // guards keep local-only rows outside this server-owned lifecycle.
  useEffect(() => {
    if (!polling.isCurrent(requestScopeKey)) return
    for (const operation of Object.values(operations)) {
      const hasVisibleAsset = operation.asset_ids.some((assetId) => Boolean(assets[assetId]))
      const hasVisibleSection = operation.target_kind === 'section'
        && Boolean(sections[operation.target_id])
      const hasVisibleGroup = operation.target_kind === 'group'
        && Boolean(groups[operation.target_id])
      if (hasVisibleAsset || hasVisibleSection || hasVisibleGroup) {
        applyOperation(operation, requestScopeKey)
      }
    }
  }, [applyOperation, assets, groups, operations, polling, requestScopeKey, sections])

  // The retained operation feed is the deletion-side hydration contract. It
  // reconciles rows removed by another tab and discovers empty-section jobs,
  // neither of which can be inferred from the additive asset list.
  useEffect(() => {
    const activeOptions = operationOptionsRef.current
    if (!activeOptions || !polling.isCurrent(requestScopeKey)) return
    const controller = new AbortController()
    void (async () => {
      let cursor: string | undefined
      const seenCursors = new Set<string>()
      try {
        do {
          const page = await listAssetDeletionOperations({
            ...activeOptions,
            cursor,
            limit: OPERATION_PAGE_LIMIT,
            signal: controller.signal,
          })
          if (controller.signal.aborted || !polling.isCurrent(requestScopeKey)) return
          for (const operation of page.data) track(operation, requestScopeKey)
          const nextCursor = page.next_cursor ?? undefined
          if (!nextCursor || seenCursors.has(nextCursor)) break
          seenCursors.add(nextCursor)
          cursor = nextCursor
        } while (cursor)
        setError(null)
      } catch (caught) {
        if (!controller.signal.aborted && polling.isCurrent(requestScopeKey)) {
          setError(errorMessage(caught))
        }
      }
    })()
    return () => controller.abort()
  }, [polling, refreshToken, requestScopeKey, track])

  return {
    error,
    observationStopped,
    operations,
    retry,
    startAssets,
    startGroup,
    startKnowledgeCollection,
    startKnowledgeDocument,
    startSection,
    startVectorIndex,
  }
}
