import { useCallback, useEffect, useRef, useState } from 'react'

import {
  getAssetDeletionOperation,
  retryAssetDeletionOperation,
  type ClientOptions,
  type ServerDeletionOperation,
} from '@/api/inqtrixClient'
import { observationRetryDelay, useStoppedObservations } from './observationRetry'
import {
  assertSessionDeletionOperation,
  DELETION_RETRY_BASE_MS,
  nextDeletionPollDelayMs,
  SessionDeletionContractError,
  type SessionDeletionState,
} from './sessionDeletion'
import { carriedErrorText, readerTexts } from '@/api/requestErrorText'

type SessionRow = { deletion?: SessionDeletionState }

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : String(value)
}

function wait(delayMs: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(signal.reason)
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(resolve, delayMs)
    signal.addEventListener('abort', () => {
      window.clearTimeout(timer)
      reject(signal.reason)
    }, { once: true })
  })
}

/** Shared Agent/Knowledge session-deletion client over the aggregate deletion
 * ledger. It projects durable tombstones, resumes polling after reload, and
 * only removes a row after the server's terminal receipt. */
export function useSessionDeletionApi({
  enabled,
  onComplete,
  onState,
  options,
  scopeKey,
  sessions,
  start,
  targetKind,
}: {
  enabled: boolean
  onComplete: (sessionId: string, operationId: string | null) => void
  onState: (sessionId: string, state: SessionDeletionState) => void
  options: ClientOptions
  scopeKey: string
  sessions: Readonly<Record<string, SessionRow>>
  start: (
    sessionId: string,
    options: ClientOptions,
  ) => Promise<ServerDeletionOperation | null>
  targetKind: 'agent_session' | 'knowledge_session'
}): {
  deleteSession: (sessionId: string) => Promise<void>
  error: string | null
  /** A receipt poll stopped after repeated failed reads; it starts again
   * once the browser is online or the tab is visible again. */
  observationStopped: boolean
  retrySession: (sessionId: string) => Promise<void>
} {
  const [error, setError] = useState<string | null>(null)
  const { observationStopped, stoppedObservations } = useStoppedObservations()
  const controllersRef = useRef(new Map<string, AbortController>())
  const requestsRef = useRef(new Set<string>())
  const scopeRef = useRef(scopeKey)
  const callbacksRef = useRef({ onComplete, onState, options, start })
  callbacksRef.current = { onComplete, onState, options, start }

  const stop = useCallback((operationId: string) => {
    controllersRef.current.get(operationId)?.abort()
    controllersRef.current.delete(operationId)
    stoppedObservations.drop(operationId)
  }, [stoppedObservations])

  const apply = useCallback((
    operation: ServerDeletionOperation,
    expectedSessionId: string,
    scope: string,
  ) => {
    if (scopeRef.current !== scope) return false
    assertSessionDeletionOperation(operation, targetKind, expectedSessionId)
    if (operation.status === 'deleted') {
      stop(operation.operation_id)
      callbacksRef.current.onComplete(expectedSessionId, operation.operation_id)
      return true
    }
    callbacksRef.current.onState(expectedSessionId, {
      error: carriedErrorText(operation.error, readerTexts().fileLibrary.deletionFailed),
      operationId: operation.operation_id,
      stage: operation.stage,
      status: operation.status === 'delete_failed' ? 'delete_failed' : 'deleting',
    })
    if (operation.status === 'delete_failed') stop(operation.operation_id)
    return true
  }, [stop, targetKind])

  const poll = useCallback((
    operationId: string,
    expectedSessionId: string,
    scope: string,
  ) => {
    if (
      !enabled
      || scopeRef.current !== scope
      || controllersRef.current.has(operationId)
    ) return
    const controller = new AbortController()
    controllersRef.current.set(operationId, controller)
    stoppedObservations.drop(operationId)
    void (async () => {
      let delayMs = nextDeletionPollDelayMs(0)
      let completedPolls = 0
      let failures = 0
      try {
        while (!controller.signal.aborted && scopeRef.current === scope) {
          await wait(delayMs, controller.signal)
          try {
            const operation = await getAssetDeletionOperation(operationId, {
              ...callbacksRef.current.options,
              signal: controller.signal,
            })
            if (!apply(operation, expectedSessionId, scope)) return
            setError(null)
            if (operation.status === 'deleted' || operation.status === 'delete_failed') return
            failures = 0
            completedPolls += 1
            delayMs = nextDeletionPollDelayMs(completedPolls)
          } catch (caught) {
            if (controller.signal.aborted || scopeRef.current !== scope) return
            setError(errorMessage(caught))
            if (caught instanceof SessionDeletionContractError) return
            failures += 1
            const retryDelay = observationRetryDelay(failures, DELETION_RETRY_BASE_MS)
            if (retryDelay === null) {
              stoppedObservations.stop(operationId, () => poll(operationId, expectedSessionId, scope))
              return
            }
            delayMs = retryDelay
          }
        }
      } catch {
        // Only the abort-aware wait rejects out here: the poll was stopped.
      } finally {
        if (controllersRef.current.get(operationId) === controller) {
          controllersRef.current.delete(operationId)
        }
      }
    })()
  }, [apply, enabled, stoppedObservations])

  const track = useCallback((
    operation: ServerDeletionOperation,
    expectedSessionId: string,
  ) => {
    const scope = scopeRef.current
    if (!apply(operation, expectedSessionId, scope)) return
    if (operation.status === 'queued' || operation.status === 'running') {
      poll(operation.operation_id, expectedSessionId, scope)
    }
  }, [apply, poll])

  const deleteSession = useCallback(async (sessionId: string) => {
    if (requestsRef.current.has(sessionId)) return
    if (!enabled) {
      callbacksRef.current.onComplete(sessionId, null)
      return
    }
    requestsRef.current.add(sessionId)
    try {
      const operation = await callbacksRef.current.start(
        sessionId,
        callbacksRef.current.options,
      )
      if (operation === null) {
        callbacksRef.current.onComplete(sessionId, null)
        return
      }
      setError(null)
      track(operation, sessionId)
    } catch (caught) {
      setError(errorMessage(caught))
      throw caught
    } finally {
      requestsRef.current.delete(sessionId)
    }
  }, [enabled, track])

  const retrySession = useCallback(async (sessionId: string) => {
    const operationId = sessions[sessionId]?.deletion?.operationId
    if (!enabled || !operationId || requestsRef.current.has(sessionId)) return
    requestsRef.current.add(sessionId)
    try {
      const operation = await retryAssetDeletionOperation(
        operationId,
        callbacksRef.current.options,
      )
      setError(null)
      track(operation, sessionId)
    } catch (caught) {
      setError(errorMessage(caught))
      throw caught
    } finally {
      requestsRef.current.delete(sessionId)
    }
  }, [enabled, sessions, track])

  useEffect(() => {
    const scope = scopeKey
    scopeRef.current = scope
    for (const controller of controllersRef.current.values()) controller.abort()
    controllersRef.current.clear()
    requestsRef.current.clear()
    setError(null)
    return () => {
      for (const controller of controllersRef.current.values()) controller.abort()
      controllersRef.current.clear()
      stoppedObservations.clear()
      requestsRef.current.clear()
    }
  }, [scopeKey, stoppedObservations])

  useEffect(() => {
    if (!enabled) return
    const scope = scopeRef.current
    for (const [sessionId, session] of Object.entries(sessions)) {
      if (session.deletion?.status === 'deleting') {
        poll(session.deletion.operationId, sessionId, scope)
      }
    }
  }, [enabled, poll, sessions])

  return { deleteSession, error, observationStopped, retrySession }
}
