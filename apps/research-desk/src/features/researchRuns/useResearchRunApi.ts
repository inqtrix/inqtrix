import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from 'react'
import {
  cancelResearchRun,
  createResearchRun,
  deleteResearchRun,
  fetchRunEventsPage,
  fetchResearchRunResult,
  fetchResearchRunSummary,
  hasHttpStatus,
  listResearchRuns,
} from '@/api/inqtrixClient'
import {
  isParkedRunStatus,
  noteParkedTransition,
  subscribeRunEvents,
} from './runEventChannel'
import type {
  CreateResearchRunRequest,
  ResearchRunEvent,
  ResearchRunResult,
  ResearchRunSummary,
} from './types'
import { isTerminalRunStatus } from './utils'
import { messageFromError } from '@/api/requestErrorText'

/** Runs per keyset page during history hydration; the loop follows
 * `next_cursor` so the full working set still hydrates while each server
 * query stays bounded (matches the chat/asset/editor hydration idiom). */
const RUN_PAGE_LIMIT = 100

export type LiveRunCallbacks = {
  onEvent: (event: ResearchRunEvent) => void
  /** Catch-up delivery: replayed/missed events arrive as ONE batch (one
   * commit, rows render in place) instead of N live-looking onEvent calls.
   * Optional so per-run consumers without history handling keep the
   * per-event path. */
  onHistory?: (events: ResearchRunEvent[]) => void
  /** `finishedAt` is the run's finish on the server, when known. */
  onResult: (result: ResearchRunResult, finishedAt?: string) => void
  /** Loading a finished run's result failed. The run itself completed, so a
   * consumer that keeps completed runs records the failure without changing
   * the run. Optional: a consumer whose only surface is the result (the
   * per-run incognito ask) has nothing to keep and gets `onRunError`. */
  onResultError?: (runId: string, message: string) => void
  onRunError: (runId: string, message: string) => void
  /** The run answered 404/401 on the event channel: no longer available
   * to this session (deliberately non-disclosing -- revoked, deleted,
   * expired and foreign all look identical). Optional: without it the
   * channel still ends cleanly; the record just keeps its last state. */
  onRunUnavailable?: (runId: string) => void
  onSummary: (summary: ResearchRunSummary, options?: { select?: boolean }) => void
}

type PerRunCallbacks = Partial<LiveRunCallbacks>

type UseResearchRunApiOptions = LiveRunCallbacks & {
  apiKey?: string
  enabled: boolean
  /** Whether the caller is admitted to list/run (the single auth gate the
   * parent already resolves: anonymous `none`, `apikey` with a key, or a live
   * cookie session). Flipping false->true (e.g. an in-app local login)
   * re-runs run-list hydration without a remount; false aborts live streams. */
  canList: boolean
  /** Re-run the authoritative list after a user invalidation wake-up. */
  refreshToken?: number
  /** Replace every server-backed run after the complete paginated listing
   * succeeds. Failed/aborted listings never prune local state. */
  onReplace: (summaries: ResearchRunSummary[]) => void
  /** The workspace namespace every run operation scopes to -- the per-user
   * project namespace when authenticated, the browser-local id otherwise.
   * Resolved by the parent (after server discovery + the auth session), so a run
   * is created/listed/cancelled under the same namespace the project data uses
   * and reports follow the user across devices. Flips to the namespace in
   * lockstep with `canList`, so the first list/submit already uses it. */
  workspaceId: string
}

/**
 * Run operations (list/create/cancel/result/stream) for the research desk,
 * scoped to a workspace namespace the PARENT resolves. Server discovery
 * (health/capabilities/stacks) lives in {@link useServerDiscovery}; splitting it
 * out lets the parent resolve the auth session and the per-user namespace before
 * this hook runs, so the run scope is the namespace from the first list/submit
 * with no in-hook session re-probe and no browser-id window.
 */
export function useResearchRunApi({
  apiKey,
  canList,
  enabled,
  onEvent,
  onHistory,
  onResult,
  onResultError,
  onRunError,
  onRunUnavailable,
  onReplace,
  onSummary,
  refreshToken = 0,
  workspaceId,
}: UseResearchRunApiOptions) {
  const [lastError, setLastError] = useState<string | null>(null)
  // Initial run-list hydration has SETTLED (success or error): workspaces
  // render a loading skeleton instead of an empty state until this flips,
  // so a reload does not flash "no runs" while pages stream in.
  const [runsHydrated, setRunsHydrated] = useState(false)
  // Runs currently on the polling fallback — the timeline
  // shows a visible degradation hint for these.
  const [pollingRunIds, setPollingRunIds] = useState<string[]>([])
  const streamsRef = useRef(new Map<string, AbortController>())
  // Runs parked on a human decision. They produce no events, but an SSE
  // stream still holds one of the browser's six connections per origin
  // — with two agent runs open, approval gates, `/quota/usage` and even
  // `/api/auth/session` starved while the server answered in 27 ms.
  // A parked run polls instead; the wake signal is unchanged.
  const parkedRunsRef = useRef(new Set<string>())
  const replayedTerminalRunIdsRef = useRef(new Set<string>())
  // Finished runs whose result this view holds: a refresh pulse lists every
  // run again, and a finished result does not change.
  const loadedResultRunIdsRef = useRef(new Set<string>())
  // Result requests on their way: a refresh pulse during a slow load asked
  // for the same result again.
  const resultLoadsRef = useRef(new Map<string, Promise<void>>())
  // Finished agent runs wait here until their session is open, finished
  // knowledge asks until their question is in view.
  const replayQueueRef = useRef<DemandQueue>({ demanded: new Set(), pending: new Map() })
  const resultQueueRef = useRef<DemandQueue>({ demanded: new Set(), pending: new Map() })
  const perRunCallbacksRef = useRef(new Map<string, PerRunCallbacks>())
  const streamScopeRef = useRef<{
    apiKey: string | undefined
    workspaceId: string
  } | null>(null)
  const callbacksRef = useRef<LiveRunCallbacks>({
    onEvent,
    onHistory,
    onResult,
    onResultError,
    onRunError,
    onRunUnavailable,
    onSummary,
  })

  useEffect(() => {
    callbacksRef.current = {
      onEvent,
      onHistory,
      onResult,
      onResultError,
      onRunError,
      onRunUnavailable,
      onSummary,
    }
  }, [onEvent, onHistory, onResult, onResultError, onRunError, onRunUnavailable, onSummary])

  const loadResultOnce = useCallback(async (runId: string, finishedAt?: string) => {
    const callbacks = () => perRunCallbacksRef.current.get(runId) ?? callbacksRef.current
    try {
      await loadTerminalResult({
        fetchResult: (id) => fetchResearchRunResult(id, { apiKey, workspaceId }),
        finishedAt,
        loaded: loadedResultRunIdsRef.current,
        onError: (id, message) => { routeResultError(callbacks(), id, message) },
        onResult: (result, finished) => { callbacks().onResult?.(result, finished) },
        runId,
      })
    } finally {
      perRunCallbacksRef.current.delete(runId)
    }
  }, [apiKey, workspaceId])

  const loadResult = useCallback((runId: string, finishedAt?: string) => shareResultLoad(
    resultLoadsRef.current,
    runId,
    () => loadResultOnce(runId, finishedAt),
  ), [loadResultOnce])

  const replayTerminalAgentEvents = useCallback(async (
    summary: ResearchRunSummary,
  ) => {
    if (replayedTerminalRunIdsRef.current.has(summary.run_id)) return
    replayedTerminalRunIdsRef.current.add(summary.run_id)
    try {
      await replayTerminalEventPages({
        fetchPage: (afterSequence) => fetchRunEventsPage(
          summary.events_url,
          afterSequence,
          { apiKey, workspaceId },
        ),
        onEvent: (event) => {
          const callbacks = perRunCallbacksRef.current.get(summary.run_id)
            ?? callbacksRef.current
          callbacks.onEvent?.(event)
        },
        onHistory: (events) => {
          const callbacks = perRunCallbacksRef.current.get(summary.run_id)
            ?? callbacksRef.current
          if (callbacks.onHistory) callbacks.onHistory(events)
          else for (const event of events) callbacks.onEvent?.(event)
        },
      })
    } catch (error) {
      const callbacks = perRunCallbacksRef.current.get(summary.run_id)
        ?? callbacksRef.current
      callbacks.onRunError?.(summary.run_id, messageFromError(error))
    } finally {
      // The answer of an agent run is its answer artifact; its result payload
      // is not read anywhere.
      perRunCallbacksRef.current.delete(summary.run_id)
    }
  }, [apiKey, workspaceId])

  const requestRunResults = useCallback((runIds: readonly string[]) => {
    for (const summary of demandQueued(resultQueueRef.current, runIds)) {
      void loadResult(summary.run_id, isoFromSeconds(summary.finished_at))
    }
  }, [loadResult])

  const replayRunHistory = useCallback((runIds: readonly string[]) => {
    for (const summary of demandQueued(replayQueueRef.current, runIds)) {
      void replayTerminalAgentEvents(summary)
    }
  }, [replayTerminalAgentEvents])

  const startStream = useCallback((summary: ResearchRunSummary) => {
    // Child runs are projected through their parent stream. A child channel is
    // opened only when its work unit is explicitly inspected.
    if (summary.kind === 'agent_child') return
    if (streamsRef.current.has(summary.run_id)) return
    if (terminalStatus(summary.status)) {
      const action = terminalRunAction(
        summary,
        loadedResultRunIdsRef.current,
        perRunCallbacksRef.current.has(summary.run_id),
      )
      if (action === 'replay_events') {
        if (queueUntilDemanded(replayQueueRef.current, summary)) {
          void replayTerminalAgentEvents(summary)
        }
      } else if (action === 'load_result') {
        void loadResult(summary.run_id, isoFromSeconds(summary.finished_at))
      } else if (action === 'defer_result') {
        if (queueUntilDemanded(resultQueueRef.current, summary)) {
          void loadResult(summary.run_id, isoFromSeconds(summary.finished_at))
        }
      } else {
        perRunCallbacksRef.current.delete(summary.run_id)
      }
      return
    }

    const controller = new AbortController()
    streamsRef.current.set(summary.run_id, controller)
    // Seeded from the row so a reload onto a parked run never opens a
    // stream it would immediately have to give up.
    if (isParkedRunStatus(summary.status)) {
      parkedRunsRef.current.add(summary.run_id)
    } else {
      parkedRunsRef.current.delete(summary.run_id)
    }
    void subscribeRunEvents({
      eventsUrl: summary.events_url,
      options: { apiKey, workspaceId },
      preferPolling: () => parkedRunsRef.current.has(summary.run_id),
      signal: controller.signal,
      // Visible degradation: the timeline shows a hint
      // while the run is on the polling fallback; recovery clears it.
      onTransportChange: (transport) => {
        setPollingRunIds((current) => {
          const isPolling = transport === 'polling'
          if (current.includes(summary.run_id) === isPolling) return current
          return isPolling
            ? [...current, summary.run_id]
            : current.filter((id) => id !== summary.run_id)
        })
      },
      onEvent: (event) => {
        noteParkedTransition(parkedRunsRef.current, event)
        const callbacks = perRunCallbacksRef.current.get(event.run_id) ?? callbacksRef.current
        callbacks.onEvent?.(event)
        if (event.type === 'inqtrix.run.completed' && runResultIsUsed(summary)) {
          void loadResult(event.run_id, isoFromSeconds(event.created_at))
        } else if (event.type === 'inqtrix.run.completed') {
          perRunCallbacksRef.current.delete(event.run_id)
        } else if (event.type === 'inqtrix.run.failed' || event.type === 'inqtrix.run.cancelled') {
          perRunCallbacksRef.current.delete(event.run_id)
        }
      },
      onHistory: (events) => {
        // Catch-up batch (SSE replay before the boundary marker, or a first
        // polling page): one dispatch, rows render in place. The terminal
        // side effects still fire — a terminal event inside the batch means
        // the run settled while this client was away.
        for (const event of events) {
          noteParkedTransition(parkedRunsRef.current, event)
        }
        const callbacks = perRunCallbacksRef.current.get(summary.run_id)
          ?? callbacksRef.current
        if (callbacks.onHistory) callbacks.onHistory(events)
        else for (const event of events) callbacks.onEvent?.(event)
        for (const event of events) {
          if (event.type === 'inqtrix.run.completed' && runResultIsUsed(summary)) {
            void loadResult(event.run_id, isoFromSeconds(event.created_at))
          } else if (event.type === 'inqtrix.run.completed') {
            perRunCallbacksRef.current.delete(event.run_id)
          } else if (event.type === 'inqtrix.run.failed' || event.type === 'inqtrix.run.cancelled') {
            perRunCallbacksRef.current.delete(event.run_id)
          }
        }
      },
      onUnavailable: (status) => {
        const callbacks = perRunCallbacksRef.current.get(summary.run_id)
          ?? callbacksRef.current
        if (status === 401 || !callbacks.onRunUnavailable) {
          // 401 is an AUTH problem, never a calm lock: cookie mode
          // reloads globally before this fires, but apikey mode has no
          // reload -- a rotated key must surface loudly, not as
          // "no longer available". Consumers without the new callback
          // (per-run incognito surfaces) keep their pre-existing loud
          // path for the 404 too.
          callbacks.onRunError?.(
            summary.run_id,
            status === 401
              ? 'Anmeldung abgelaufen oder Zugriffsschlüssel ungültig.'
              : 'Dieser Lauf ist nicht mehr verfügbar.',
          )
        } else {
          // Calm terminal state, not an error: the server's 404 is
          // deliberately non-disclosing.
          callbacks.onRunUnavailable(summary.run_id)
        }
        perRunCallbacksRef.current.delete(summary.run_id)
      },
    }).catch((error) => {
      if (controller.signal.aborted) return
      const callbacks = perRunCallbacksRef.current.get(summary.run_id) ?? callbacksRef.current
      callbacks.onRunError?.(summary.run_id, messageFromError(error))
      perRunCallbacksRef.current.delete(summary.run_id)
    }).finally(() => {
      streamsRef.current.delete(summary.run_id)
      setPollingRunIds((current) =>
        current.includes(summary.run_id)
          ? current.filter((id) => id !== summary.run_id)
          : current)
    })
  }, [apiKey, loadResult, replayTerminalAgentEvents, workspaceId])

  const submitRun = useCallback(async (
    request: CreateResearchRunRequest,
    options?: {
      /** Select the new run in the research workspace (default true);
       * knowledge asks pass false — their surface is the Wissen thread. */
      select?: boolean
      /** Invoked with the accepted summary BEFORE the event stream
       * starts, so callers can register run-id-keyed state without
       * racing the first SSE event. */
      onCreated?: (summary: ResearchRunSummary) => void
      /** Per-run callback override for ephemeral surfaces such as incognito
       * Knowledge asks. Omitted callbacks are intentionally not filled from the
       * global store callbacks, so those runs stay out of persisted state. */
      callbacks?: PerRunCallbacks
      /** Preserve the typed rejection for a caller-owned visible recovery
       * flow. The hook still records lastError and returns null. */
      onRejected?: (error: unknown) => void
      /** Default true. Set false only when the caller visibly handles a 401. */
      reloadOnUnauthorized?: boolean
      /** Prevent the accepted summary from entering the global run store. */
      suppressSummary?: boolean
    },
  ): Promise<ResearchRunSummary | null> => {
    try {
      setLastError(null)
      const summary = await createResearchRun(request, {
        apiKey,
        reloadOnUnauthorized: options?.reloadOnUnauthorized,
        workspaceId,
      })
      if (options?.callbacks) {
        perRunCallbacksRef.current.set(summary.run_id, options.callbacks)
      }
      if (!options?.suppressSummary) {
        callbacksRef.current.onSummary(summary, { select: options?.select ?? true })
      } else {
        options?.callbacks?.onSummary?.(summary, { select: options?.select ?? true })
      }
      options?.onCreated?.(summary)
      startStream(summary)
      return summary
    } catch (error) {
      options?.onRejected?.(error)
      const message = messageFromError(error)
      setLastError(message)
      console.warn('Inqtrix run creation failed.', error)
      return null
    }
  }, [apiKey, startStream, workspaceId])

  const cancelRun = useCallback(async (runId: string) => {
    try {
      setLastError(null)
      const summary = await cancelResearchRun(runId, { apiKey, workspaceId })
      callbacksRef.current.onSummary(summary)
      startStream(summary)
    } catch (error) {
      const message = messageFromError(error)
      setLastError(message)
      throw new Error(message, { cause: error })
    }
  }, [apiKey, startStream, workspaceId])

  const deleteRun = useCallback(async (runId: string, options?: { cancelIfActive?: boolean }) => {
    // Stop any live stream first, then delete durably on the server. The
    // caller removes it from local state only after this resolves, so a
    // failed delete never leaves the UI claiming a run is gone while the
    // store still has it (it would re-appear on the next reload).
    const controller = streamsRef.current.get(runId)
    if (controller) {
      controller.abort()
      streamsRef.current.delete(runId)
    }
    perRunCallbacksRef.current.delete(runId)
    try {
      setLastError(null)
      await deleteResearchRun(runId, { apiKey, workspaceId })
    } catch (error) {
      // A 404 means the run is already absent server-side; for a delete that
      // is idempotent success, not a failure to surface. Anything else (e.g.
      // 409 while still active) is a real error the caller must see.
      if (hasHttpStatus(error, 404)) return
      if (options?.cancelIfActive && hasHttpStatus(error, 409)) {
        let cancelled: ResearchRunSummary | null = null
        try {
          try {
            cancelled = await cancelResearchRun(runId, { apiKey, workspaceId })
          } catch (cancelError) {
            if (!hasHttpStatus(cancelError, 404)) throw cancelError
            return
          }
          // Keep the card honest while we wait: the summary now carries
          // cancel_requested, which renders the "cancelling" badge.
          callbacksRef.current.onSummary(cancelled)
          if (!isTerminalRunStatus(cancelled.status)) {
            // A cancel of a RUNNING run is asynchronous (the worker stops
            // at its next checkpoint); only queued/waiting runs cancel to
            // a terminal state synchronously. Wait for the transition
            // instead of retrying the delete into another 409.
            let terminal: ResearchRunSummary | null
            try {
              terminal = await waitForRunTerminal({
                fetchSummary: () =>
                  fetchResearchRunSummary(runId, { apiKey, workspaceId }),
              })
            } catch (waitError) {
              // The run vanished while waiting (expired/foreign delete):
              // the goal state is reached.
              if (hasHttpStatus(waitError, 404)) return
              throw waitError
            }
            if (terminal === null) throw new RunStillCancellingError(runId)
          }
          try {
            await deleteResearchRun(runId, { apiKey, workspaceId })
            return
          } catch (deleteError) {
            if (hasHttpStatus(deleteError, 404)) return
            throw deleteError
          }
        } catch (flowError) {
          // The run survives a failed cancel-and-delete flow, but its live
          // stream was aborted above — reattach it so the card keeps
          // following the run (and flips to "cancelled" once the worker
          // stops) instead of freezing on the "cancelling" badge.
          if (cancelled !== null) startStream(cancelled)
          if (flowError instanceof RunStillCancellingError) {
            // Surfaced once by the caller on the run card; no banner.
            throw flowError
          }
          const message = messageFromError(flowError)
          setLastError(message)
          throw new Error(message, { cause: flowError })
        }
      }
      const message = messageFromError(error)
      setLastError(message)
      throw new Error(message, { cause: error })
    }
  }, [apiKey, startStream, workspaceId])

  useEffect(() => {
    if (!enabled || !canList) {
      // Not admitted (disabled, no apikey, or no/lost session): abort any live
      // streams and list nothing. Re-running with canList true re-hydrates.
      for (const controller of streamsRef.current.values()) {
        controller.abort()
      }
      streamsRef.current.clear()
      perRunCallbacksRef.current.clear()
      replayedTerminalRunIdsRef.current.clear()
      loadedResultRunIdsRef.current.clear()
      replayQueueRef.current = { demanded: new Set(), pending: new Map() }
      resultQueueRef.current = { demanded: new Set(), pending: new Map() }
      resultLoadsRef.current.clear()
      streamScopeRef.current = null
      setRunsHydrated(false)
      if (clearsLastErrorOnAdmissionLoss({ enabled, canList })) {
        setLastError(null)
      }
      return undefined
    }

    let ignore = false
    const listController = new AbortController()
    const previousScope = streamScopeRef.current
    const scopeChanged = previousScope === null
      || previousScope.apiKey !== apiKey
      || previousScope.workspaceId !== workspaceId
    streamScopeRef.current = { apiKey, workspaceId }
    if (scopeChanged) {
      for (const controller of streamsRef.current.values()) controller.abort()
      streamsRef.current.clear()
      perRunCallbacksRef.current.clear()
      replayedTerminalRunIdsRef.current.clear()
      loadedResultRunIdsRef.current.clear()
      replayQueueRef.current = { demanded: new Set(), pending: new Map() }
      resultQueueRef.current = { demanded: new Set(), pending: new Map() }
      resultLoadsRef.current.clear()
      setRunsHydrated(false)
    }

    async function hydrate() {
      try {
        let cursor: string | undefined
        const summaries: ResearchRunSummary[] = []
        do {
          const page = await listResearchRuns({
            apiKey,
            cursor,
            limit: RUN_PAGE_LIMIT,
            signal: listController.signal,
            workspaceId,
          })
          if (ignore) return
          summaries.push(...page.data)
          cursor = page.next_cursor ?? undefined
        } while (cursor)
        if (ignore) return
        onReplace(summaries)
        const visibleIds = new Set(summaries.map((summary) => summary.run_id))
        for (const [runId, controller] of streamsRef.current) {
          if (visibleIds.has(runId)) continue
          controller.abort()
          streamsRef.current.delete(runId)
          perRunCallbacksRef.current.delete(runId)
        }
        for (const summary of summaries) startStream(summary)
        setLastError(null)
      } catch (error) {
        if (!ignore && !listController.signal.aborted) {
          setLastError(messageFromError(error))
        }
      } finally {
        // "Settled", not "succeeded": a failed listing surfaces via
        // lastError — the loading state must still end either way.
        if (!ignore) setRunsHydrated(true)
      }
    }

    void hydrate()

    return () => {
      ignore = true
      listController.abort()
    }
  }, [apiKey, canList, enabled, onReplace, refreshToken, startStream, workspaceId])

  useEffect(() => {
    return () => {
      for (const controller of streamsRef.current.values()) {
        controller.abort()
      }
      streamsRef.current.clear()
      perRunCallbacksRef.current.clear()
      replayedTerminalRunIdsRef.current.clear()
      loadedResultRunIdsRef.current.clear()
      replayQueueRef.current = { demanded: new Set(), pending: new Map() }
      resultQueueRef.current = { demanded: new Set(), pending: new Map() }
      resultLoadsRef.current.clear()
    }
  }, [])

  return {
    cancelRun,
    deleteRun,
    lastError,
    pollingRunIds,
    /** Replay the event history of these finished agent runs (the runs of the
     * open session); runs listed later replay as soon as they arrive. */
    replayRunHistory,
    /** Load the results of these finished runs (the knowledge questions in
     * view); runs listed later load as soon as they arrive. */
    requestRunResults,
    /** Load a finished run's result again after a failed attempt. */
    retryResult: loadResult,
    runsHydrated,
    submitRun,
  }
}

function terminalStatus(status: ResearchRunSummary['status']) {
  return status === 'completed'
    || status === 'failed'
    || status === 'cancelled'
    || status === 'expired'
}

/** Terminal root-agent summaries need their durable event story replayed after
 * hydration. Child runs stay parent-projected; standard runs need only their
 * result payload. */
/** Whether losing admission should also drop the last error.
 *
 * Leaving the server for the demo drops every other piece of that session's
 * state, and the error must go too: otherwise the demo composer claims a
 * rejected request the demo never made, on two surfaces, for the whole demo
 * session — nothing in demo mode clears it. It could even appear with no
 * submission at all, when only the run list failed to load.
 *
 * Losing the session (`canList === false`) is the opposite case: a real run's
 * error is exactly what the user needs to see, so it stays.
 */
export function clearsLastErrorOnAdmissionLoss({
  enabled,
  canList,
}: {
  enabled: boolean
  canList: boolean
}): boolean {
  void canList
  return !enabled
}

export function shouldReplayTerminalAgentEvents(
  summary: Pick<ResearchRunSummary, 'kind' | 'mode' | 'status'>,
): boolean {
  return summary.kind !== 'agent_child'
    && (summary.mode === 'workspace_agent' || summary.mode === 'agent_kernel')
    && terminalStatus(summary.status)
}

/** What a listed finished run needs from this view: an agent run replays its
 * events, a completed run loads its result unless this view already holds it
 * and nobody waits on it, and any other run needs nothing. */
export function terminalRunAction(
  summary: Pick<ResearchRunSummary, 'kind' | 'mode' | 'run_id' | 'status'>,
  loaded: ReadonlySet<string>,
  waitedOn: boolean,
): 'replay_events' | 'load_result' | 'defer_result' | 'none' {
  if (shouldReplayTerminalAgentEvents(summary)) return 'replay_events'
  if (summary.status !== 'completed') return 'none'
  if (waitedOn) return 'load_result'
  if (loaded.has(summary.run_id)) return 'none'
  // A knowledge result attaches to its question: it loads once the question
  // is in view, not for every finished ask at start.
  return summary.mode === 'knowledge' ? 'defer_result' : 'load_result'
}

/** Fetch one finished run's result and hand it on with the run's finish. The
 * run joins `loaded` only once its result arrived, so a failed fetch is tried
 * again on the next refresh pulse. */
export async function loadTerminalResult({
  fetchResult,
  finishedAt,
  loaded,
  onError,
  onResult,
  runId,
}: {
  fetchResult: (runId: string) => Promise<ResearchRunResult>
  finishedAt?: string
  loaded: Set<string>
  onError: (runId: string, message: string) => void
  onResult: (result: ResearchRunResult, finishedAt?: string) => void
  runId: string
}): Promise<void> {
  try {
    const result = await fetchResult(runId)
    onResult(result, finishedAt)
    loaded.add(runId)
  } catch (error) {
    onError(runId, messageFromError(error))
  }
}

/** A failed result load is not a failed run: it goes to `onResultError` when
 * the consumer keeps completed runs, otherwise to `onRunError`. */
export function routeResultError(
  callbacks: Partial<LiveRunCallbacks>,
  runId: string,
  message: string,
): void {
  if (callbacks.onResultError) callbacks.onResultError(runId, message)
  else callbacks.onRunError?.(runId, message)
}

/** Whether a view reads a finished run's result. An agent run keeps its
 * answer as an artifact; its result payload was fetched and thrown away. */
export function runResultIsUsed(
  summary: Pick<ResearchRunSummary, 'kind' | 'mode'>,
): boolean {
  return summary.kind !== 'agent_child'
    && summary.mode !== 'workspace_agent'
    && summary.mode !== 'agent_kernel'
}

/** One request per result while it is on its way. */
export function shareResultLoad(
  loads: Map<string, Promise<void>>,
  runId: string,
  start: () => Promise<void>,
): Promise<void> {
  const running = loads.get(runId)
  if (running) return running
  const load = start().finally(() => {
    if (loads.get(runId) === load) loads.delete(runId)
  })
  loads.set(runId, load)
  return load
}

/** Work on finished runs that waits until a view asks for it (an agent run's
 * history replay, a knowledge answer's result): listed runs wait in
 * `pending`, runs a view asked for are `demanded` and go ahead as soon as
 * they are listed. */
export type DemandQueue = {
  demanded: Set<string>
  pending: Map<string, ResearchRunSummary>
}

/** Queue a listed finished run; true when a view already asked for it. */
export function queueUntilDemanded(
  queue: DemandQueue,
  summary: ResearchRunSummary,
): boolean {
  if (queue.demanded.has(summary.run_id)) return true
  queue.pending.set(summary.run_id, summary)
  return false
}

/** Mark runs as asked for and hand out those already listed. */
export function demandQueued(
  queue: DemandQueue,
  runIds: readonly string[],
): ResearchRunSummary[] {
  const ready: ResearchRunSummary[] = []
  for (const runId of runIds) {
    queue.demanded.add(runId)
    const summary = queue.pending.get(runId)
    if (summary) {
      queue.pending.delete(runId)
      ready.push(summary)
    }
  }
  return ready
}

export async function replayTerminalEventPages({
  fetchPage,
  onEvent,
  onHistory,
}: {
  fetchPage: (afterSequence: number | null) => Promise<{
    data: ResearchRunEvent[]
    terminal: boolean
  }>
  onEvent: (event: ResearchRunEvent) => void
  /** Every page of a TERMINAL replay is history by definition; given this,
   * each page lands as one batch instead of per-event deliveries. */
  onHistory?: (events: ResearchRunEvent[]) => void
}): Promise<void> {
  let afterSequence: number | null = null
  for (;;) {
    const page = await fetchPage(afterSequence)
    if (onHistory) onHistory(page.data)
    else for (const event of page.data) onEvent(event)
    if (page.terminal) return
    const latest = page.data.at(-1)?.sequence
    if (latest === undefined || latest === afterSequence) {
      throw new Error('Terminal agent event replay ended before a terminal page.')
    }
    afterSequence = latest
  }
}

/** Thrown by `deleteRun` when a cancelled run did not reach a terminal
 * state within the bounded wait. The message is diagnostic only: callers
 * detect the class and render their own localized copy. */
export class RunStillCancellingError extends Error {
  constructor(runId: string) {
    super(`run ${runId} is still cancelling; retry the delete once it stopped`)
    this.name = 'RunStillCancellingError'
  }
}

/** Poll one run's summary until it reaches a terminal status.
 *
 * Returns the terminal summary, or `null` once `maxWaitMs` is exhausted.
 * The bound caps only how long the UI auto-waits before surfacing a
 * visible "still cancelling" error — it never truncates a server-side
 * operation, so it intentionally needs no capabilities-derived budget
 * (unlike request timeouts, see researchRuns/clientTimeouts.ts). Expected
 * post-cancel latency is seconds; 30s covers a slow in-flight provider
 * attempt without leaving the user staring at a silent spinner forever.
 */
export async function waitForRunTerminal({
  fetchSummary,
  pollMs = 2000,
  maxWaitMs = 30000,
  sleep = (ms: number) =>
    new Promise<void>((resolve) => {
      window.setTimeout(resolve, ms)
    }),
}: {
  fetchSummary: () => Promise<ResearchRunSummary>
  pollMs?: number
  maxWaitMs?: number
  sleep?: (ms: number) => Promise<void>
}): Promise<ResearchRunSummary | null> {
  let waitedMs = 0
  for (;;) {
    const summary = await fetchSummary()
    if (isTerminalRunStatus(summary.status)) return summary
    if (waitedMs >= maxWaitMs) return null
    await sleep(pollMs)
    waitedMs += pollMs
  }
}

/** A server epoch-seconds timestamp as ISO text; `undefined` when absent. */
function isoFromSeconds(seconds: number | null | undefined): string | undefined {
  return typeof seconds === 'number' ? new Date(seconds * 1000).toISOString() : undefined
}
