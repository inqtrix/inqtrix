import {
  listAdminAuditEvents,
  type AdminAuditEvent,
  type AdminAuditFilters,
} from '@/api/inqtrixClient'
import { useCallback, useEffect, useRef, useState } from 'react'
import { seedAuditLog } from './demo'

type AuditStatus = 'idle' | 'loading' | 'ready' | 'error'

export type AdminAuditState = {
  available: boolean
  demo: boolean
  events: AdminAuditEvent[]
  nextCursor: string | null
  status: AuditStatus
  error: string | null
}

/**
 * Cursor-paginated audit trail for the admin panel.
 *
 * Mirrors the useAdminUsers grammar: demo mode serves the seeded twin,
 * `enabled: false` stays idle/unavailable, and a generation guard drops
 * stale responses when filters change mid-flight. `loadMore` appends the
 * next keyset page.
 */
export function useAdminAudit({
  demo,
  enabled,
  filters,
}: {
  demo: boolean
  enabled: boolean
  filters: AdminAuditFilters
}): AdminAuditState & { reload: () => void; loadMore: () => void } {
  const [state, setState] = useState<AdminAuditState>({
    available: false,
    demo,
    events: [],
    nextCursor: null,
    status: 'idle',
    error: null,
  })
  const generationRef = useRef(0)
  const filterKey = JSON.stringify(filters)

  const load = useCallback(
    (cursor: string | null, append: boolean) => {
      const generation = ++generationRef.current
      if (demo) {
        const seeded = applyDemoFilters(seedAuditLog(), filters)
        setState({
          available: true,
          demo: true,
          events: seeded,
          nextCursor: null,
          status: 'ready',
          error: null,
        })
        return
      }
      if (!enabled) {
        setState((previous) => ({
          ...previous,
          available: false,
          demo: false,
          status: 'idle',
        }))
        return
      }
      setState((previous) => ({
        ...previous,
        demo: false,
        status: 'loading',
        error: null,
      }))
      void listAdminAuditEvents({
        ...filters,
        cursor: cursor ?? undefined,
      })
        .then((page) => {
          if (generationRef.current !== generation) return
          setState((previous) => ({
            available: true,
            demo: false,
            events: append
              ? [...previous.events, ...page.data]
              : page.data,
            nextCursor: page.next_cursor,
            status: 'ready',
            error: null,
          }))
        })
        .catch((error: unknown) => {
          if (generationRef.current !== generation) return
          setState((previous) => ({
            ...previous,
            status: 'error',
            error:
              error instanceof Error ? error.message : String(error),
          }))
        })
    },
    // filterKey is the serialized filter set, so it covers every filter
    // field without listing them individually.
    [demo, enabled, filterKey],
  )

  useEffect(() => {
    load(null, false)
  }, [load])

  const reload = useCallback(() => load(null, false), [load])
  const loadMore = useCallback(() => {
    if (state.nextCursor) load(state.nextCursor, true)
  }, [load, state.nextCursor])

  return { ...state, reload, loadMore }
}

function applyDemoFilters(
  rows: AdminAuditEvent[],
  filters: AdminAuditFilters,
): AdminAuditEvent[] {
  return rows.filter((row) => {
    if (filters.action && !row.action.startsWith(filters.action)) {
      return false
    }
    if (filters.actor && row.actor_pseudonym !== filters.actor) return false
    if (filters.outcome && row.outcome !== filters.outcome) return false
    return true
  })
}
