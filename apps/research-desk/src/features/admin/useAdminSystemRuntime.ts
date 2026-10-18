import { useCallback, useEffect, useRef, useState } from 'react'

import {
  type AdminSystemRuntime,
  fetchAdminSystemRuntime,
} from '@/api/inqtrixClient'
import { seedAdminSystemRuntime } from './demo'

type AdminSystemRuntimeStatus = 'idle' | 'loading' | 'ready' | 'error'

export type AdminSystemRuntimeState = {
  available: boolean
  demo: boolean
  error: string | null
  runtime: AdminSystemRuntime | null
  status: AdminSystemRuntimeStatus
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** Read-only runtime manifest for the instance-admin System page. */
export function useAdminSystemRuntime({
  demo,
  enabled,
}: {
  demo: boolean
  enabled: boolean
}) {
  const [state, setState] = useState<AdminSystemRuntimeState>({
    available: false,
    demo,
    error: null,
    runtime: null,
    status: 'idle',
  })
  const generationRef = useRef(0)

  const reload = useCallback(async () => {
    const generation = ++generationRef.current
    if (!enabled) {
      setState({
        available: false,
        demo,
        error: null,
        runtime: null,
        status: 'idle',
      })
      return
    }
    if (demo) {
      setState({
        available: true,
        demo,
        error: null,
        runtime: seedAdminSystemRuntime(),
        status: 'ready',
      })
      return
    }
    setState((current) => ({ ...current, available: true, status: 'loading' }))
    try {
      const runtime = await fetchAdminSystemRuntime()
      if (generationRef.current !== generation) return
      setState({
        available: true,
        demo,
        error: null,
        runtime,
        status: 'ready',
      })
    } catch (error) {
      if (generationRef.current !== generation) return
      setState({
        available: true,
        demo,
        error: messageOf(error),
        runtime: null,
        status: 'error',
      })
    }
  }, [demo, enabled])

  useEffect(() => {
    void reload()
  }, [reload])

  return { reload, state }
}
