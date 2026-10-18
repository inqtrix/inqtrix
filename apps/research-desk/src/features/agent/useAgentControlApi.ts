import { useCallback, useEffect, useMemo, useRef } from 'react'
import type { Dispatch } from 'react'

import {
  answerAgentRunClarification,
  applyEditorPatch,
  cancelAgentRunTask,
  decideAgentRunApproval,
  fetchResearchRunSummary,
  exportAgentRunArtifact,
  getAgentRunArtifact,
  getAgentRunPlan,
  getAgentRunTaskResult,
  getEditorDocument,
  getEditorPatch,
  hasHttpStatus,
  listAgentRunApprovals,
  listAgentRunArtifacts,
  listAgentRunClarifications,
  listAgentSessionArtifacts,
  renameAgentRunArtifact,
  rejectEditorPatch,
  updateAgentRunArtifact,
  type ClientOptions,
  type InqtrixRequestError,
} from '@/api/inqtrixClient'
import type { ResearchDeskAction } from '@/features/researchDesk/state'
import {
  isParkedRunStatus,
  noteParkedTransition,
  subscribeRunEvents,
  type RunEventTransport,
} from '@/features/researchRuns/runEventChannel'
import type {
  AgentApprovalDecisionRequest,
  AgentClarificationAnswerRequest,
  AgentTaskResultWire,
} from './types'
import {
  TERMINAL_AGENT_TASK_STATUSES,
  canEditAgentRun,
  type AgentRunRecord,
  type AgentSessionArtifactIndex,
} from './model'
import { readerError } from '@/api/requestErrorText'

export type ArtifactSaveResult =
  | { kind: 'saved'; revision: number }
  | { kind: 'conflict'; currentRevision: number | null }
  | { kind: 'locked' }

export type ArtifactRenameResult =
  | { kind: 'renamed' }
  | { kind: 'locked' }

const TASK_RESULT_CACHE_MAX = 50

/**
 * Control-surface data layer of the Agent Desk. Rows are the truth: SSE
 * events only flip `*Stale` flags on the run records — this hook
 * turns every stale flag into ONE fetch and stores the rows via the
 * reducer (which clears the flag, so the effect cannot loop). Decisions
 * and answers POST through here; each response embeds the RESUMED run
 * summary, which is upserted immediately (status waiting -> queued).
 */
export function useAgentControlApi({
  apiKey,
  dispatch,
  enabled,
  onEditorDocumentsChanged,
  runs,
  selectedSessionId,
  sessionArtifacts = {},
  workspaceId,
}: {
  apiKey: string | undefined
  dispatch: Dispatch<ResearchDeskAction>
  enabled: boolean
  onEditorDocumentsChanged: () => void
  runs: Record<string, AgentRunRecord>
  /** The open session: only its runs (and inspected child missions) and its
   * artifact index load their rows. Every other session loads when opened. */
  selectedSessionId: string | null
  /** Anchor-independent per-session artifact index. */
  sessionArtifacts?: Record<string, AgentSessionArtifactIndex>
  workspaceId: string
}) {
  const optionsRef = useRef({ apiKey, workspaceId })
  optionsRef.current = { apiKey, workspaceId }
  const inFlightRef = useRef(new Set<string>())
  // A failed fetch is a terminal readiness signal for the current row
  // revision. Without this guard, a missing answer body retried on every
  // render and could keep a structural loading boundary alive forever.
  const failedRef = useRef(new Set<string>())
  const runsRef = useRef(runs)
  runsRef.current = runs
  const enabledRef = useRef(enabled)
  enabledRef.current = enabled
  const requireEditableRun = useCallback((runId: string) => {
    if (!canEditAgentRun(runsRef.current[runId])) {
      throw readerError('agentRunReadOnly')
    }
  }, [])
  // Cache entries remember the live attempt they were fetched under so a
  // re-run of the SAME terminal status still invalidates (see loadTaskResult).
  const taskResultCacheRef = useRef(
    new Map<string, { result: AgentTaskResultWire; attempt?: number }>(),
  )
  const taskResultInFlightRef = useRef(
    new Map<string, Promise<AgentTaskResultWire>>(),
  )
  // Keys whose PREFETCH failed once: hover/idle must not hammer a
  // failing endpoint on every event; the on-open fetch stays untouched
  // and re-tries loudly.
  const taskResultPrefetchFailedRef = useRef(new Set<string>())
  // Artifact bodies on their way, and pinned revisions already loaded (a
  // revision never changes): a re-render or a run event asks no second time.
  const artifactLoadsRef = useRef(
    new Map<string, ReturnType<typeof getAgentRunArtifact>>(),
  )

  useEffect(() => {
    failedRef.current.clear()
    artifactLoadsRef.current.clear()
  }, [apiKey, workspaceId])

  useEffect(() => {
    if (!enabled) return
    // A reducer success/error action settles the current stale cycle. Drop
    // only failures whose fetch condition is no longer active; if a later SSE
    // event re-flags the same constant surface key, it starts unblocked. A
    // still-active failed condition remains suppressed, so renders cannot
    // create a tight retry loop.
    failedRef.current = currentAgentControlFetchFailures(
      runs,
      failedRef.current,
    )
    const scope = agentControlScope(runs, selectedSessionId)
    for (const run of Object.values(runs)) {
      if (!hasAgentControlRows(run) || !scope.has(run.runId)) continue
      if (run.planStale) {
        void refetch(`${run.runId}:plan`, async (options) => {
          try {
            const plan = await getAgentRunPlan(
              run.runId,
              undefined,
              options,
            )
            dispatch({ plan, runId: run.runId, type: 'setAgentRunPlan' })
          } catch (error) {
            if (hasHttpStatus(error, 404)) {
              // No plan yet (pre-planning) — clear the flag, keep nothing.
              dispatch({ plan: null, runId: run.runId, type: 'setAgentRunPlan' })
              return
            }
            throw error
          }
        })
      }
      if (run.approvalsStale) {
        void refetch(`${run.runId}:approvals`, async (options) => {
          const approvals = await listAgentRunApprovals(
            run.runId,
            options,
          )
          dispatch({
            approvals,
            runId: run.runId,
            type: 'setAgentRunApprovals',
          })
        })
      }
      if (run.clarificationsStale) {
        void refetch(`${run.runId}:clarifications`, async (options) => {
          const clarifications = await listAgentRunClarifications(
            run.runId,
            options,
          )
          dispatch({
            clarifications,
            runId: run.runId,
            type: 'setAgentRunClarifications',
          })
        })
      }
      // Gate rows of parked CHILDREN: their approvals/clarifications live
      // under the child run id (owner-visible), the parent's child.progress
      // stream flags them. Fetched here so the composer tray can offer the
      // child's gate.
      for (const [childRunId, gates] of Object.entries(run.childGates)) {
        if (!gates.stale) continue
        void refetch(childGateFetchKey(run.runId, childRunId), async (options) => {
          const [approvals, clarifications, childSummary] = await Promise.all([
            listAgentRunApprovals(childRunId, options),
            listAgentRunClarifications(childRunId, options),
            // The child's run row carries the FULL delegated question —
            // the gate context must be completely readable (every other
            // parent-side source is a truncated preview).
            fetchResearchRunSummary(childRunId, options),
          ])
          dispatch({
            approvals,
            childRunId,
            clarifications,
            question: childSummary.question,
            runId: run.runId,
            type: 'setAgentChildGates',
          })
        })
      }
      if (run.patchStale && run.patchId) {
        const patchId = run.patchId
        void refetch(`${run.runId}:patch`, async (options) => {
          const patch = await getEditorPatch(patchId, options)
          dispatch({ patch, runId: run.runId, type: 'setAgentRunPatch' })
        })
      }
      if (run.artifactsStale) {
        void refetch(`${run.runId}:artifacts`, async (options) => {
          const artifacts = await listAgentRunArtifacts(
            run.runId,
            options,
          )
          dispatch({
            artifacts,
            runId: run.runId,
            type: 'setAgentRunArtifacts',
          })
        })
      }
      // Chat answers render INLINE in the transcript, so their body is
      // auto-fetched (unlike canvas artifacts, which load on tab open).
      // Loop-safe: the reducer drops a stale body when the list shows a
      // newer revision — `contentMarkdown === undefined` is therefore
      // exactly "detail missing or outdated", and the fetched detail
      // makes it defined again.
      if (!run.artifactsStale) {
        const answer = run.artifactOrder
          .map((id) => run.artifacts[id])
          .find((artifact) => artifact?.kind === 'answer')
        if (
          answer
          && answer.status !== 'writing'
          && (
            answer.contentMarkdown === undefined
            || answer.publicationNeedsReconcile === true
          )
        ) {
          void refetch(answerFetchKey(run, answer), async (options) => {
            const artifact = await getAgentRunArtifact(
              run.runId,
              answer.artifactId,
              undefined,
              options,
            )
            dispatch({
              artifact,
              runId: run.runId,
              type: 'setAgentRunArtifactDetail',
            })
          })
        }
      }
    }

    // Anchor-independent session artifact index: fetched when its
    // stale flag is set. Success and failure BOTH clear the flag (the
    // failure stays visible on the index), so renders cannot loop — the
    // next SSE artifact signal is the honest retry moment.
    for (const [sessionId, index] of Object.entries(sessionArtifacts)) {
      if (!index.stale || sessionId !== selectedSessionId) continue
      const sessionKey = `${sessionId}:session_artifacts`
      if (inFlightRef.current.has(sessionKey)) continue
      inFlightRef.current.add(sessionKey)
      void listAgentSessionArtifacts(sessionId, optionsRef.current)
        .then((result) => {
          dispatch({
            artifacts: result.data,
            sessionId,
            type: 'setAgentSessionArtifacts',
          })
        })
        .catch((error: unknown) => {
          dispatch({
            message: error instanceof Error ? error.message : String(error),
            sessionId,
            type: 'markAgentSessionArtifactsError',
          })
        })
        .finally(() => {
          inFlightRef.current.delete(sessionKey)
        })
    }

    function refetch(key: string, fetcher: (options: ClientOptions) => Promise<void>) {
      if (inFlightRef.current.has(key) || failedRef.current.has(key)) {
        return Promise.resolve()
      }
      inFlightRef.current.add(key)
      // A request past its deadline is cancelled, not abandoned: left running
      // it would keep one of the browser's connections and could be asked
      // again by the next cycle while still open.
      const controller = new AbortController()
      return withControlFetchDeadline(
        fetcher({ ...optionsRef.current, signal: controller.signal }),
        // The reader needs to know it is not their agent that stalled.
        'Die Entscheidung konnte nicht geladen werden (Zeitüberschreitung). '
        + 'Seite neu laden.',
        CONTROL_FETCH_TIMEOUT_MS,
        setTimeout,
        clearTimeout as never,
        () => controller.abort(),
      )
        .then(() => {
          failedRef.current.delete(key)
        })
        .catch((error: unknown) => {
          // The condition may have settled while the request was in flight.
          // In that case retaining its failure would suppress a future cycle
          // even though this request no longer belongs to the visible state.
          if (agentControlFetchKeys(runsRef.current).has(key)) {
            failedRef.current.add(key)
          }
          // Loud, never silent: the run card shows the fetch failure. The
          // surface's stale flag clears with it (no tight retry loop).
          const [runId, surface, childRunId] = key.split(':')
          dispatch({
            ...(surface === 'child_gates' && childRunId
              ? { childRunId }
              : {}),
            message: error instanceof Error ? error.message : String(error),
            runId,
            surface: surface as
              | 'plan'
              | 'approvals'
              | 'clarifications'
              | 'artifacts'
              | 'answer'
              | 'patch'
              | 'child_gates',
            type: 'markAgentRunError',
          })
        })
        .finally(() => {
          inFlightRef.current.delete(key)
        })
    }
  }, [dispatch, enabled, runs, selectedSessionId, sessionArtifacts])

  const decideApproval = useCallback(
    async (
      runId: string,
      approvalId: string,
      decision: AgentApprovalDecisionRequest,
    ) => {
      requireEditableRun(runId)
      const result = await decideAgentRunApproval(
        runId,
        approvalId,
        decision,
        optionsRef.current,
      )
      const { run, ...approval } = result
      dispatch({ summary: run, type: 'upsertAgentRunSummary' })
      dispatch({ approvals: [approval], runId, type: 'setAgentRunApprovals' })
      await refreshPlanAfterApprovalDecision({
        kind: approval.kind,
        load: () => getAgentRunPlan(
          runId,
          undefined,
          optionsRef.current,
        ),
        onLoaded: (plan) => {
          dispatch({ plan, runId, type: 'setAgentRunPlan' })
        },
        onError: (message) => {
          dispatch({
            message,
            runId,
            surface: 'plan',
            type: 'markAgentRunError',
          })
        },
      })
      // The full list (older approvals) refreshes via the decided event;
      // seeding the fresh row keeps the card responsive meanwhile.
      return approval
    },
    [dispatch, requireEditableRun],
  )

  const answerClarification = useCallback(
    async (
      runId: string,
      clarificationId: string,
      answer: AgentClarificationAnswerRequest,
    ) => {
      requireEditableRun(runId)
      const result = await answerAgentRunClarification(
        runId,
        clarificationId,
        answer,
        optionsRef.current,
      )
      const { run, ...clarification } = result
      dispatch({ summary: run, type: 'upsertAgentRunSummary' })
      dispatch({
        clarifications: [clarification],
        runId,
        type: 'setAgentRunClarifications',
      })
      return clarification
    },
    [dispatch, requireEditableRun],
  )

  // Child-gate decisions target the CHILD's run id (its rows are
  // owner-scoped and directly decidable), but every local echo lands on
  // the PARENT record — the child's own summary in the response is
  // deliberately discarded because agent_child runs never become desk
  // records. Editability is judged on the parent: the tray only offers
  // child gates there, and a share recipient cannot reach the child's
  // URLs anyway: a child is readable only through the parent's children
  // route, its own run URLs stay owner-scoped.
  const decideChildApproval = useCallback(
    async (
      parentRunId: string,
      childRunId: string,
      approvalId: string,
      decision: AgentApprovalDecisionRequest,
    ) => {
      requireEditableRun(parentRunId)
      const result = await decideAgentRunApproval(
        childRunId,
        approvalId,
        decision,
        optionsRef.current,
      )
      const { run: childSummary, ...approval } = result
      void childSummary
      // Seeding the fresh row dismisses the card; the child.progress
      // resume event is the durable confirmation.
      dispatch({
        approvals: [approval],
        childRunId,
        runId: parentRunId,
        type: 'setAgentChildGates',
      })
      return approval
    },
    [dispatch, requireEditableRun],
  )

  const answerChildClarification = useCallback(
    async (
      parentRunId: string,
      childRunId: string,
      clarificationId: string,
      answer: AgentClarificationAnswerRequest,
    ) => {
      requireEditableRun(parentRunId)
      const result = await answerAgentRunClarification(
        childRunId,
        clarificationId,
        answer,
        optionsRef.current,
      )
      const { run: childSummary, ...clarification } = result
      void childSummary
      dispatch({
        childRunId,
        clarifications: [clarification],
        runId: parentRunId,
        type: 'setAgentChildGates',
      })
      return clarification
    },
    [dispatch, requireEditableRun],
  )

  const loadArtifact = useCallback(
    (runId: string, artifactId: string, revision?: number) => shareArtifactLoad(
      artifactLoadsRef.current,
      runId,
      artifactId,
      revision,
      async () => {
        const artifact = await getAgentRunArtifact(
          runId,
          artifactId,
          revision,
          optionsRef.current,
        )
        // Pinned historical revisions are view-only; only the LATEST body
        // may enter the record (the canvas edits against it).
        if (revision === undefined) {
          dispatch({ artifact, runId, type: 'setAgentRunArtifactDetail' })
        }
        return artifact
      },
    ),
    [dispatch],
  )

  const loadTaskResult = useCallback(
    (runId: string, taskId: string): Promise<AgentTaskResultWire> => {
      const key = `${runId}:${taskId}`
      const cached = taskResultCacheRef.current.get(key)
      const liveTask = runsRef.current[runId]?.taskStates[taskId]
      // Serve the cache only while the live task still carries the status
      // AND attempt the result was fetched under — a retried task drops
      // its entry even when it lands on the same terminal status again.
      const cacheValid =
        cached !== undefined
        && (liveTask === undefined
          || (liveTask.status === cached.result.status
            && liveTask.attempt === cached.attempt))
      if (cached && cacheValid) {
        return Promise.resolve(cached.result)
      }
      if (cached) taskResultCacheRef.current.delete(key)
      const inFlight = taskResultInFlightRef.current.get(key)
      if (inFlight) return inFlight
      // Attempt at REQUEST time: a retry racing this fetch must not tag
      // the previous attempt's result with the new attempt number.
      const requestAttempt = liveTask?.attempt
      const request = getAgentRunTaskResult(runId, taskId, optionsRef.current)
        .then((result) => {
          // Success re-arms hover/idle prefetch for this task (the flag
          // cannot clear inside prefetchTaskResult — it early-returns).
          taskResultPrefetchFailedRef.current.delete(key)
          if (TERMINAL_AGENT_TASK_STATUSES.has(result.status)) {
            const cache = taskResultCacheRef.current
            cache.set(key, {
              attempt: requestAttempt,
              result,
            })
            if (cache.size > TASK_RESULT_CACHE_MAX) {
              const oldest = cache.keys().next().value
              if (oldest !== undefined) cache.delete(oldest)
            }
          }
          return result
        })
        .finally(() => {
          taskResultInFlightRef.current.delete(key)
        })
      taskResultInFlightRef.current.set(key, request)
      return request
    },
    [],
  )

  const prefetchTaskResult = useCallback(
    (runId: string, taskId: string) => {
      // Demo/offline: the canvas renders demo runs, but there is no
      // server to prefetch from (the detail view guards the same way
      // via clientOptions).
      if (!enabledRef.current) return
      const key = `${runId}:${taskId}`
      if (taskResultPrefetchFailedRef.current.has(key)) return
      void loadTaskResult(runId, taskId)
        .catch((error: unknown) => {
          // Visible, but not load-bearing: the on-open fetch in the task
          // detail view stays the loud error path and still retries.
          taskResultPrefetchFailedRef.current.add(key)
          console.warn(
            `Task result prefetch failed (${key}):`,
            error instanceof Error ? error.message : error,
          )
        })
    },
    [loadTaskResult],
  )

  const cancelTask = useCallback(
    async (runId: string, taskId: string) => {
      requireEditableRun(runId)
      const result = await cancelAgentRunTask(
        runId,
        taskId,
        optionsRef.current,
      )
      dispatch({
        childRunId: result.child_run_id,
        runId,
        status: result.status,
        taskId,
        type: 'ackAgentTaskCancel',
      })
      try {
        const plan = await getAgentRunPlan(
          runId,
          undefined,
          optionsRef.current,
        )
        dispatch({ plan, runId, type: 'setAgentRunPlan' })
      } catch (error) {
        dispatch({
          message: error instanceof Error ? error.message : String(error),
          runId,
          surface: 'plan',
          type: 'markAgentRunError',
        })
      }
      return result
    },
    [dispatch, requireEditableRun],
  )

  const saveArtifact = useCallback(
    async (
      runId: string,
      artifactId: string,
      contentMarkdown: string,
      expectedRevision: number,
    ): Promise<ArtifactSaveResult> => {
      requireEditableRun(runId)
      try {
        const result = await updateAgentRunArtifact(
          runId,
          artifactId,
          {
            content_markdown: contentMarkdown,
            expected_revision: expectedRevision,
          },
          optionsRef.current,
        )
        await loadArtifact(runId, artifactId)
        return { kind: 'saved', revision: result.revision }
      } catch (error) {
        if (hasHttpStatus(error, 409)) {
          const detail = (error as InqtrixRequestError).detail ?? {}
          if (detail.locked_by === 'agent') return { kind: 'locked' }
          await loadArtifact(runId, artifactId)
          const current = detail.current_revision
          return {
            kind: 'conflict',
            currentRevision: typeof current === 'number' ? current : null,
          }
        }
        throw error
      }
    },
    [loadArtifact, requireEditableRun],
  )

  const renameArtifact = useCallback(
    async (
      runId: string,
      artifactId: string,
      title: string,
      sessionId: string | null,
    ): Promise<ArtifactRenameResult> => {
      requireEditableRun(runId)
      try {
        await renameAgentRunArtifact(
          runId,
          artifactId,
          { title },
          optionsRef.current,
        )
      } catch (error) {
        if (hasHttpStatus(error, 409)) return { kind: 'locked' }
        throw error
      }
      // The rename event reaches only LIVE streams; a settled run has
      // none — refresh the row and the session name index directly so
      // chips, tabs and the registry follow without a reload.
      await loadArtifact(runId, artifactId)
      if (sessionId) {
        const result = await listAgentSessionArtifacts(
          sessionId,
          optionsRef.current,
        )
        dispatch({
          artifacts: result.data,
          sessionId,
          type: 'setAgentSessionArtifacts',
        })
      }
      return { kind: 'renamed' }
    },
    [dispatch, loadArtifact, requireEditableRun],
  )

  const exportArtifact = useCallback(
    async (runId: string, artifactId: string, title?: string) => {
      const result = await exportAgentRunArtifact(
        runId,
        artifactId,
        { target: 'editor_document', title },
        optionsRef.current,
      )
      // Reuse authoritative hydration, including the editor's autosave
      // baseline. A metadata-only local insert must not become an empty save.
      onEditorDocumentsChanged()
      return result
    },
    [onEditorDocumentsChanged],
  )

  const applyPatch = useCallback(
    async (
      runId: string,
      patchId: string,
      expectedRevision: number,
    ): Promise<
      | { kind: 'applied'; revision: number; appliedEditIds: string[] }
      | { kind: 'conflict'; currentRevision: number | null }
    > => {
      requireEditableRun(runId)
      const refreshAfterApply = async () => {
        const patch = await getEditorPatch(patchId, optionsRef.current)
        dispatch({ patch, runId, type: 'setAgentRunPatch' })
        // The SERVER document just changed under the local editor copy —
        // pull the fresh body in (never dirties, never bumps updatedAt),
        // or the applied patch stays invisible until reload and the next
        // autosave of the stale copy would silently undo it.
        const document = await getEditorDocument(
          patch.document_id,
          optionsRef.current,
        )
        dispatch({
          contentMarkdown: document.content_markdown ?? '',
          documentId: patch.document_id,
          type: 'setServerEditorDocumentBody',
        })
        return patch
      }
      try {
        const result = await applyEditorPatch(
          patchId,
          expectedRevision,
          optionsRef.current,
        )
        await refreshAfterApply()
        return {
          kind: 'applied',
          revision: result.revision,
          appliedEditIds: result.applied_edit_ids,
        }
      } catch (error) {
        if (hasHttpStatus(error, 409)) {
          const detail = (error as InqtrixRequestError).detail ?? {}
          if (detail.status === 'accepted') {
            // Already applied (other tab / replayed decide) — the stored
            // outcome IS the success, not a conflict.
            const patch = await refreshAfterApply()
            return {
              kind: 'applied',
              revision: patch.applied_revision ?? patch.document_revision,
              appliedEditIds: patch.applied_edit_ids ?? [],
            }
          }
          const patch = await getEditorPatch(patchId, optionsRef.current)
          dispatch({ patch, runId, type: 'setAgentRunPatch' })
          const current = detail.current_revision
          return {
            kind: 'conflict',
            currentRevision:
              typeof current === 'number' ? current : null,
          }
        }
        throw error
      }
    },
    [dispatch, requireEditableRun],
  )

  const rejectPatch = useCallback(
    async (runId: string, patchId: string, note: string) => {
      requireEditableRun(runId)
      const patch = await rejectEditorPatch(
        patchId,
        note,
        optionsRef.current,
      )
      dispatch({ patch, runId, type: 'setAgentRunPatch' })
      return patch
    },
    [dispatch, requireEditableRun],
  )

  const isTranscriptHydrated = useCallback((runId: string): boolean => {
    // Demo/local runs carry their rows in reducer actions; there is no control
    // API whose initial stale defaults could ever settle.
    if (!enabledRef.current) return true
    const run = runsRef.current[runId]
    if (!run) return true
    return agentTranscriptHydrated(run, failedRef.current)
  }, [])

  const inspectChildRun = useCallback(
    (runId: string, signal: AbortSignal, onTransport: (value: RunEventTransport) => void) =>
      inspectAgentChild(runId, { ...optionsRef.current, signal }, dispatch, onTransport),
    [dispatch],
  )

  // One object per set of callbacks: a new object on every render made each
  // consumer (canvas context, timeline actions, open canvas views) treat a
  // re-render as a change and fetch artifact bodies again.
  return useMemo(() => ({
    answerChildClarification,
    answerClarification,
    applyPatch,
    cancelTask,
    decideApproval,
    decideChildApproval,
    exportArtifact,
    loadArtifact,
    loadTaskResult,
    isTranscriptHydrated,
    inspectChildRun,
    prefetchTaskResult,
    rejectPatch,
    renameArtifact,
    saveArtifact,
  }), [
    answerChildClarification,
    answerClarification,
    applyPatch,
    cancelTask,
    decideApproval,
    decideChildApproval,
    exportArtifact,
    loadArtifact,
    loadTaskResult,
    isTranscriptHydrated,
    inspectChildRun,
    prefetchTaskResult,
    rejectPatch,
    renameArtifact,
    saveArtifact,
  ])
}

/** One request per artifact body while it is on its way. A pinned revision
 * never changes, so its body stays for the page; the latest body is asked
 * again next time, because it can change. A failed request is asked again. */
export function shareArtifactLoad<T>(
  loads: Map<string, Promise<T>>,
  runId: string,
  artifactId: string,
  revision: number | undefined,
  start: () => Promise<T>,
): Promise<T> {
  const key = `${runId}\u0000${artifactId}\u0000${revision ?? 'latest'}`
  const running = loads.get(key)
  if (running) return running
  const load = start()
  loads.set(key, load)
  load.then(
    () => { if (revision === undefined && loads.get(key) === load) loads.delete(key) },
    () => { if (loads.get(key) === load) loads.delete(key) },
  )
  return load
}

/** A child channel exists only for the user-opened detail, under caller scope. */
export async function inspectAgentChild(
  runId: string,
  options: ClientOptions & { signal: AbortSignal },
  dispatch: Dispatch<ResearchDeskAction>,
  onTransport: (value: RunEventTransport) => void,
): Promise<void> {
  const summary = await fetchResearchRunSummary(runId, options)
  if (options.signal.aborted) return
  if (summary.kind !== 'agent_child') throw readerError('agentRunNotDelegated')
  dispatch({ type: 'upsertInspectedAgentChild', summary })
  let unavailable = false
  const parked = new Set(isParkedRunStatus(summary.status) ? [runId] : [])
  await subscribeRunEvents({
    eventsUrl: summary.events_url,
    options,
    signal: options.signal,
    preferPolling: () => parked.has(runId),
    onEvent: (event) => {
      noteParkedTransition(parked, event)
      dispatch({ type: 'appendApiRunEvent', event })
    },
    onHistory: (events) => {
      for (const event of events) noteParkedTransition(parked, event)
      dispatch({ type: 'appendApiRunEvents', events })
    },
    onTransportChange: onTransport,
    onUnavailable: () => { unavailable = true },
  })
  if (unavailable) throw readerError('agentRunUnavailable')
}

function answerFetchKey(
  run: Pick<AgentRunRecord, 'runId'>,
  answer: Pick<AgentRunRecord['artifacts'][string], 'artifactId' | 'revision'>,
): string {
  return `${run.runId}:answer:${answer.artifactId}:${answer.revision}`
}

function childGateFetchKey(runId: string, childRunId: string): string {
  return `${runId}:child_gates:${childRunId}`
}

/** Deadline for one control-surface fetch. */
export const CONTROL_FETCH_TIMEOUT_MS = 20_000

/**
 * Turn an endless wait into a stated failure.
 *
 * The failure path here is already loud — the run card shows it. But a
 * request that never resolves AND never rejects is neither: it stays
 * marked in flight forever, so nothing is shown and nothing retries.
 * That is exactly what happened when the browser's connection pool ran
 * dry with two agent runs open: the surface said "waiting for your
 * approval" while the fetch that would have rendered the decision hung
 * silently, for minutes.
 *
 * A deadline converts that into the failure the surface already knows
 * how to report.
 */
export function withControlFetchDeadline<T>(
  work: Promise<T>,
  message: string,
  timeoutMs: number = CONTROL_FETCH_TIMEOUT_MS,
  schedule: (fn: () => void, ms: number) => unknown = setTimeout,
  cancel: (handle: never) => void = clearTimeout as never,
  onTimeout?: () => void,
): Promise<T> {
  let timer: unknown
  return Promise.race([
    work,
    new Promise<never>((_resolve, reject) => {
      timer = schedule(() => {
        onTimeout?.()
        reject(new Error(message))
      }, timeoutMs)
    }),
  ]).finally(() => {
    cancel(timer as never)
  })
}

/** The runs whose control rows load now: those of the open session, and every
 * child mission (a child is in the store only once someone inspects it). The
 * rows of other sessions load when the user opens them; loading them all at
 * once queued the open session behind every run of every session. */
export function agentControlScope(
  runs: Record<string, AgentRunRecord>,
  selectedSessionId: string | null,
): Set<string> {
  const scope = new Set<string>()
  for (const run of Object.values(runs)) {
    if (run.kind === 'agent_child' || (selectedSessionId !== null && run.sessionId === selectedSessionId)) {
      scope.add(run.runId)
    }
  }
  return scope
}

/** Inspected child missions use the same durable control rows as root missions. */
function hasAgentControlRows(run: AgentRunRecord): boolean {
  return run.kind === 'agent' || run.kind === 'agent_child'
}

function agentControlFetchKeys(
  runs: Record<string, AgentRunRecord>,
): Set<string> {
  const keys = new Set<string>()
  for (const run of Object.values(runs)) {
    if (!hasAgentControlRows(run)) continue
    if (run.planStale) keys.add(`${run.runId}:plan`)
    if (run.approvalsStale) keys.add(`${run.runId}:approvals`)
    if (run.clarificationsStale) keys.add(`${run.runId}:clarifications`)
    if (run.patchStale && run.patchId) keys.add(`${run.runId}:patch`)
    for (const [childRunId, gates] of Object.entries(run.childGates)) {
      if (gates.stale) keys.add(childGateFetchKey(run.runId, childRunId))
    }
    if (run.artifactsStale) {
      keys.add(`${run.runId}:artifacts`)
      continue
    }
    const answer = run.artifactOrder
      .map((id) => run.artifacts[id])
      .find((artifact) => artifact?.kind === 'answer')
    if (
      answer
      && answer.status !== 'writing'
      && (
        answer.contentMarkdown === undefined
        || answer.publicationNeedsReconcile === true
      )
    ) {
      keys.add(answerFetchKey(run, answer))
    }
  }
  return keys
}

/** Retain terminal failures only while their exact stale/revision cycle is
 * still requested. Once that condition settles, the same surface key is
 * available to a later invalidation without permitting render-driven retries. */
export function currentAgentControlFetchFailures(
  runs: Record<string, AgentRunRecord>,
  failures: ReadonlySet<string>,
): Set<string> {
  const requested = agentControlFetchKeys(runs)
  return new Set([...failures].filter((key) => requested.has(key)))
}

/** Readiness of the transcript's row-backed geometry. Failed requests are
 * terminal too: the visible run error replaces an endless loading state. */
export function agentTranscriptHydrated(
  run: AgentRunRecord,
  terminalFetches: ReadonlySet<string> = new Set(),
): boolean {
  const runId = run.runId
  if (run.planStale && !terminalFetches.has(`${runId}:plan`)) return false
  if (
    run.approvalsStale
    && !terminalFetches.has(`${runId}:approvals`)
  ) return false
  if (
    run.clarificationsStale
    && !terminalFetches.has(`${runId}:clarifications`)
  ) return false
  if (
    run.artifactsStale
    && !terminalFetches.has(`${runId}:artifacts`)
  ) return false
  if (run.artifactsStale) return true
  const answer = run.artifactOrder
    .map((id) => run.artifacts[id])
    .find((artifact) => artifact?.kind === 'answer')
  if (
    answer
    && answer.status !== 'writing'
    && (
      answer.contentMarkdown === undefined
      || answer.publicationNeedsReconcile === true
    )
    && !terminalFetches.has(answerFetchKey(run, answer))
  ) return false
  return true
}

export async function refreshPlanAfterApprovalDecision<Plan>({
  kind,
  load,
  onError,
  onLoaded,
}: {
  kind: string
  load: () => Promise<Plan>
  onError: (message: string) => void
  onLoaded: (plan: Plan) => void
}): Promise<void> {
  if (kind !== 'plan' && kind !== 'replan') return
  try {
    onLoaded(await load())
  } catch (error) {
    onError(error instanceof Error ? error.message : String(error))
  }
}
