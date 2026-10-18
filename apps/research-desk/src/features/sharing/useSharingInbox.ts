import { useCallback, useEffect, useRef, useState } from 'react'
import {
  acceptShare,
  fetchMyShares,
  fetchSharingInbox,
  revokeShare,
} from '@/api/inqtrixClient'
import {
  acceptDemoShare,
  demoOutgoingShares,
  demoSharingInbox,
  dropDemoInboxShare,
} from './demoShares'
import type { InboxShare, OutgoingShare } from './types'

type SharingStatus = 'idle' | 'loading' | 'ready' | 'error'

export type SharingInboxState = {
  accepted: InboxShare[]
  error: string | null
  mutationError: string | null
  outgoing: OutgoingShare[]
  pending: InboxShare[]
  status: SharingStatus
}

export type SharingInboxHandle = {
  accept: (shareId: string) => Promise<void>
  drop: (shareId: string) => Promise<void>
  pendingCount: number
  reload: () => Promise<void>
  state: SharingInboxState
}

const EMPTY: SharingInboxState = {
  accepted: [],
  error: null,
  mutationError: null,
  outgoing: [],
  pending: [],
  status: 'idle',
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/**
 * Server-truth state for the sharing settings panel and the nav badge: the
 * recipient inbox (pending consent queue + accepted) and the caller's outgoing
 * shares, plus the recipient mutations — accept a pending invite, drop one's
 * own share (decline if pending, leave if accepted; same DELETE either way).
 * Every mutation re-reads, so the panel and the badge can never disagree with
 * the server. Disabled (gate off) resolves to the empty state without a fetch.
 */
export function useSharingInbox({
  demo,
  enabled,
  onResourcesChanged,
  refreshToken = 0,
}: {
  demo: boolean
  enabled: boolean
  onResourcesChanged?: () => void
  refreshToken?: number
}): SharingInboxHandle {
  const [state, setState] = useState<SharingInboxState>(EMPTY)
  const controllerRef = useRef<AbortController | null>(null)
  const generationRef = useRef(0)

  const reload = useCallback(async () => {
    controllerRef.current?.abort()
    controllerRef.current = null
    const generation = generationRef.current + 1
    generationRef.current = generation
    if (!enabled) {
      setState(EMPTY)
      return
    }
    if (demo) {
      const inbox = demoSharingInbox()
      setState({
        accepted: inbox.accepted,
        error: null,
        mutationError: null,
        outgoing: demoOutgoingShares(),
        pending: inbox.pending,
        status: 'ready',
      })
      return
    }
    setState((prev) => ({
      ...prev,
      status: prev.status === 'ready' ? 'ready' : 'loading',
    }))
    const controller = new AbortController()
    controllerRef.current = controller
    try {
      const [inbox, outgoing] = await Promise.all([
        fetchSharingInbox({ signal: controller.signal }),
        fetchMyShares({ signal: controller.signal }),
      ])
      if (controller.signal.aborted || generation !== generationRef.current) return
      setState({
        accepted: inbox.accepted,
        error: null,
        mutationError: null,
        outgoing,
        pending: inbox.pending,
        status: 'ready',
      })
    } catch (error) {
      if (controller.signal.aborted || generation !== generationRef.current) return
      setState((prev) => ({ ...prev, error: errorText(error), status: 'error' }))
    } finally {
      if (controllerRef.current === controller) controllerRef.current = null
    }
  }, [demo, enabled])

  useEffect(() => {
    void reload()
    return () => controllerRef.current?.abort()
  }, [refreshToken, reload])

  const accept = useCallback(
    async (shareId: string) => {
      setState((prev) => ({ ...prev, mutationError: null }))
      try {
        if (demo) acceptDemoShare(shareId)
        else await acceptShare(shareId)
        onResourcesChanged?.()
        await reload()
      } catch (error) {
        setState((prev) => ({ ...prev, mutationError: errorText(error) }))
        throw error
      }
    },
    [demo, onResourcesChanged, reload],
  )

  const drop = useCallback(
    async (shareId: string) => {
      setState((prev) => ({ ...prev, mutationError: null }))
      try {
        if (demo) dropDemoInboxShare(shareId)
        else await revokeShare(shareId)
        onResourcesChanged?.()
        await reload()
      } catch (error) {
        setState((prev) => ({ ...prev, mutationError: errorText(error) }))
        throw error
      }
    },
    [demo, onResourcesChanged, reload],
  )

  return { accept, drop, pendingCount: state.pending.length, reload, state }
}
