import { hasHttpStatus, type ServerDeletionOperation } from '@/api/inqtrixClient'
import { observationRetryDelay } from '@/features/project/observationRetry'
import {
  DELETION_RETRY_BASE_MS,
  nextDeletionPollDelayMs,
} from '@/features/project/sessionDeletion'

/**
 * Following the erasure of accounts from the admin user list.
 *
 * An erasure is a deletion operation. The list reads its checkpoint with the
 * same cadence and the same rule for failed reads as every other deletion
 * receipt, and shows how far it got. Framework-free so the loop is testable
 * without a browser; `useAccountErasures` binds it to React.
 */

/** Erasures the list follows at the same time. A script that erases many
 * accounts while the list is open must not turn into a request storm: the
 * others show that they are being erased and are followed once a slot is
 * free. */
export const MAX_FOLLOWED_ERASURES = 10

/** The step an erasure shows. `running`: claimed, before its first
 * checkpoint. */
export type AccountErasurePhase =
  | 'queued'
  | 'running'
  | 'aggregates_removed'
  | 'rows_removed'
  | 'residuals_verified'

export type AccountErasureProgress = {
  operationId: string
  /** `running`: still erasing. `failed`: stopped; the administrator can try
   * again. `unknown`: the list still names the erasure, but it is readable
   * neither through its operation nor through the account -- it never got
   * an operation, or another administrator holds it; erasing the account
   * again resumes it or says who holds it. */
  state: 'running' | 'failed' | 'unknown'
  phase: AccountErasurePhase | null
  completed: number
  total: number
  /** The reader's text of the failure. */
  error: string | null
}

type FailureText = (error: ServerDeletionOperation['error']) => string | null

const CHECKPOINTS: ReadonlySet<string> = new Set([
  'aggregates_removed',
  'rows_removed',
  'residuals_verified',
])

/** How far *operation* got, or `'erased'` once the account is gone. */
export function erasureProgress(
  operation: ServerDeletionOperation,
  failureText: FailureText,
): AccountErasureProgress | 'erased' {
  if (operation.status === 'deleted') return 'erased'
  const failed = operation.status === 'delete_failed'
  let phase: AccountErasurePhase | null = null
  if (!failed) {
    phase = CHECKPOINTS.has(operation.stage)
      ? (operation.stage as AccountErasurePhase)
      : operation.status === 'running'
        ? 'running'
        : 'queued'
  }
  return {
    operationId: operation.operation_id,
    state: failed ? 'failed' : 'running',
    phase,
    completed: operation.completed_items,
    total: operation.total_items,
    error: failed ? failureText(operation.error) : null,
  }
}

function waitFor(delayMs: number, signal: AbortSignal): Promise<void> {
  if (signal.aborted) return Promise.reject(signal.reason)
  return new Promise((resolve, reject) => {
    const timer = setTimeout(resolve, delayMs)
    signal.addEventListener(
      'abort',
      () => {
        clearTimeout(timer)
        reject(signal.reason)
      },
      { once: true },
    )
  })
}

export type AccountErasureObserver = {
  /** Follow *operationId* for *userId*; following what is already followed
   * changes nothing, and an erasure the account path no longer found shows
   * as `unknown` instead of being read again. */
  observe: (userId: string, operationId: string) => void
  /** Stop following every account not in *userIds*. */
  retain: (userIds: ReadonlySet<string>) => void
  /** Reopen *userId*'s failed or unreadable erasure and follow it again. */
  retry: (userId: string) => Promise<void>
  /** Stop following every account. */
  stopAll: () => void
}

function unreadableProgress(operationId: string): AccountErasureProgress {
  return {
    completed: 0,
    error: null,
    operationId,
    phase: null,
    state: 'unknown',
    total: 0,
  }
}

export function createAccountErasureObserver({
  failureText,
  onErased,
  onProgress,
  onResumed,
  onStopped,
  read,
  readByUser,
  reopen,
  restartByUser,
  sleep = waitFor,
}: {
  failureText: FailureText
  /** The account is gone, or may be: its erasure ended, or the account path
   * no longer finds it. The list reloads and drops the account once it is
   * gone. */
  onErased: (userId: string) => void
  onProgress: (userId: string, progress: AccountErasureProgress) => void
  /** A stopped read loop is no longer stopped: it started again or ended. */
  onResumed: (operationId: string) => void
  /** A read loop stopped after repeated failed reads; `restart` follows the
   * erasure again. */
  onStopped: (operationId: string, restart: () => void) => void
  read: (
    operationId: string,
    signal: AbortSignal,
  ) => Promise<ServerDeletionOperation>
  /** Reads the erasure through the account when the operation is not
   * readable here: one provisioning started belongs to nobody, and every
   * active administrator follows it through the account. */
  readByUser: (userId: string, signal: AbortSignal) => Promise<ServerDeletionOperation>
  reopen: (operationId: string) => Promise<ServerDeletionOperation>
  /** Repeats an erasure read through the account: erasing the account
   * again resumes it. */
  restartByUser: (userId: string) => Promise<ServerDeletionOperation>
  sleep?: (delayMs: number, signal: AbortSignal) => Promise<void>
}): AccountErasureObserver {
  /** The erasure each followed account is in, kept after its loop ended so a
   * failed one can be tried again. */
  const operations = new Map<string, string>()
  const loops = new Map<string, AbortController>()
  /** Accounts whose erasure is read through the account. */
  const byUser = new Set<string>()
  /** Accounts whose erasure the account path did not find; reading it again
   * would only reload the list again. */
  const unreadable = new Set<string>()

  const end = (userId: string) => {
    loops.get(userId)?.abort()
    loops.delete(userId)
    const operationId = operations.get(userId)
    if (operationId) onResumed(operationId)
    operations.delete(userId)
    byUser.delete(userId)
    unreadable.delete(userId)
  }

  const follow = (userId: string, operationId: string) => {
    if (operations.get(userId) === operationId) {
      if (unreadable.has(userId)) {
        onProgress(userId, unreadableProgress(operationId))
        return
      }
      if (loops.has(userId)) return
    } else {
      byUser.delete(userId)
      unreadable.delete(userId)
    }
    loops.get(userId)?.abort()
    const controller = new AbortController()
    operations.set(userId, operationId)
    loops.set(userId, controller)
    onResumed(operationId)
    void loop(userId, operationId, controller)
  }

  const loop = async (
    userId: string,
    operationId: string,
    controller: AbortController,
  ) => {
    const { signal } = controller
    let delayMs = nextDeletionPollDelayMs(0)
    let completedPolls = 0
    let failures = 0
    try {
      while (!signal.aborted) {
        try {
          await sleep(delayMs, signal)
        } catch {
          return
        }
        try {
          const progress = erasureProgress(
            byUser.has(userId)
              ? await readByUser(userId, signal)
              : await read(operationId, signal),
            failureText,
          )
          if (signal.aborted) return
          if (progress === 'erased') {
            operations.delete(userId)
            onErased(userId)
            return
          }
          onProgress(userId, progress)
          if (progress.state === 'failed') return
          failures = 0
          completedPolls += 1
          delayMs = nextDeletionPollDelayMs(completedPolls)
        } catch (caught) {
          if (signal.aborted) return
          if (hasHttpStatus(caught, 404)) {
            if (!byUser.has(userId)) {
              byUser.add(userId)
              continue
            }
            // The account path never answers `deleted`: the erasure ends
            // with the account, and the path answers 404 -- as it does for
            // an erasure without an operation or one another administrator
            // holds. Only the list tells them apart.
            unreadable.add(userId)
            onErased(userId)
            return
          }
          failures += 1
          const retryDelay = observationRetryDelay(failures, DELETION_RETRY_BASE_MS)
          if (retryDelay === null) {
            onStopped(operationId, () => follow(userId, operationId))
            return
          }
          delayMs = retryDelay
        }
      }
    } finally {
      if (loops.get(userId) === controller) loops.delete(userId)
    }
  }

  return {
    observe: follow,
    retain: (userIds) => {
      for (const userId of [...operations.keys()]) {
        if (!userIds.has(userId)) end(userId)
      }
    },
    retry: async (userId) => {
      const operationId = operations.get(userId)
      if (!operationId) return
      const throughAccount = byUser.has(userId)
      const reopened = throughAccount
        ? await restartByUser(userId)
        : await reopen(operationId)
      const progress = erasureProgress(reopened, failureText)
      if (progress === 'erased') {
        end(userId)
        onErased(userId)
        return
      }
      onProgress(userId, progress)
      loops.get(userId)?.abort()
      loops.delete(userId)
      unreadable.delete(userId)
      follow(userId, reopened.operation_id)
      if (throughAccount) byUser.add(userId)
    },
    stopAll: () => {
      for (const userId of [...operations.keys()]) end(userId)
    },
  }
}
