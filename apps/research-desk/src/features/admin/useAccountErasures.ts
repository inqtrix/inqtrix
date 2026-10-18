import { useEffect, useRef, useState } from 'react'

import {
  type AdminUser,
  getAssetDeletionOperation,
  retryAssetDeletionOperation,
} from '@/api/inqtrixClient'
import { carriedErrorText, readerTexts } from '@/api/requestErrorText'
import { useStoppedObservations } from '@/features/project/observationRetry'
import {
  type AccountErasureObserver,
  type AccountErasureProgress,
  createAccountErasureObserver,
  MAX_FOLLOWED_ERASURES,
} from './accountErasure'
import { isErasing } from './adminModel'

/**
 * How far each listed account's erasure got. Reads run without a workspace:
 * an account erasure belongs to no workspace. `onErased` runs when an account
 * is gone, so the list can drop the row.
 */
export function useAccountErasures({
  enabled,
  onErased,
  users,
}: {
  enabled: boolean
  onErased: () => void
  users: readonly AdminUser[]
}): {
  /** A read loop stopped after repeated failed reads; it starts again once
   * the browser is online or the tab is visible again. */
  observationStopped: boolean
  progress: Readonly<Record<string, AccountErasureProgress>>
  retry: (userId: string) => Promise<void>
} {
  const [progress, setProgress] = useState<Record<string, AccountErasureProgress>>({})
  const { observationStopped, stoppedObservations } = useStoppedObservations()
  const onErasedRef = useRef(onErased)
  onErasedRef.current = onErased
  const observerRef = useRef<AccountErasureObserver | null>(null)
  if (observerRef.current === null) {
    observerRef.current = createAccountErasureObserver({
      failureText: (error) =>
        carriedErrorText(error, readerTexts().adminUsers.erasureFailed),
      onErased: (userId) => {
        setProgress((current) => {
          const next = { ...current }
          delete next[userId]
          return next
        })
        onErasedRef.current()
      },
      onProgress: (userId, next) =>
        setProgress((current) => ({ ...current, [userId]: next })),
      onResumed: (operationId) => stoppedObservations.drop(operationId),
      onStopped: (operationId, restart) =>
        stoppedObservations.stop(operationId, restart),
      read: (operationId, signal) =>
        getAssetDeletionOperation(operationId, { signal }),
      reopen: (operationId) => retryAssetDeletionOperation(operationId),
    })
  }
  const observer = observerRef.current

  useEffect(() => {
    if (!enabled) {
      observer.stopAll()
      return
    }
    const followed = users
      .filter(isErasing)
      .slice(0, MAX_FOLLOWED_ERASURES)
    for (const user of followed) {
      observer.observe(user.id, user.erasure_operation_id)
    }
    const kept = new Set(followed.map((user) => user.id))
    observer.retain(kept)
    setProgress((current) => {
      const stale = Object.keys(current).filter((userId) => !kept.has(userId))
      if (stale.length === 0) return current
      const next = { ...current }
      for (const userId of stale) delete next[userId]
      return next
    })
  }, [enabled, observer, users])

  useEffect(() => () => observer.stopAll(), [observer])

  return { observationStopped, progress, retry: observer.retry }
}
