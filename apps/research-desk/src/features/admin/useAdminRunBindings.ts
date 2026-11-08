import { useCallback, useEffect, useRef, useState } from 'react'

import {
  type AdminRunBindings,
  type AdminRunBindingsCancel,
  cancelAdminRunBindings,
  fetchAdminRunBindings,
} from '@/api/inqtrixClient'
import { seedAdminRunBindings } from './demo'

type AdminRunBindingsStatus = 'idle' | 'loading' | 'ready' | 'error'

export type AdminRunBindingsState = {
  bindings: AdminRunBindings | null
  error: string | null
  status: AdminRunBindingsStatus
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

/** Open runs by binding for the instance-admin System page, and their cancel. */
export function useAdminRunBindings({
  demo,
  enabled,
}: {
  demo: boolean
  enabled: boolean
}) {
  const [state, setState] = useState<AdminRunBindingsState>({
    bindings: null,
    error: null,
    status: 'idle',
  })
  const generationRef = useRef(0)

  const reload = useCallback(async () => {
    const generation = ++generationRef.current
    if (!enabled) {
      setState({ bindings: null, error: null, status: 'idle' })
      return
    }
    if (demo) {
      setState({ bindings: seedAdminRunBindings(), error: null, status: 'ready' })
      return
    }
    setState((current) => ({ ...current, status: 'loading' }))
    try {
      const bindings = await fetchAdminRunBindings()
      if (generationRef.current !== generation) return
      setState({ bindings, error: null, status: 'ready' })
    } catch (error) {
      if (generationRef.current !== generation) return
      setState({ bindings: null, error: messageOf(error), status: 'error' })
    }
  }, [demo, enabled])

  /** Cancels the mode's runs this server cannot run, then reads the counts again. */
  const cancel = useCallback(
    async (mode: string): Promise<AdminRunBindingsCancel> => {
      try {
        return await cancelAdminRunBindings(mode)
      } finally {
        await reload()
      }
    },
    [reload],
  )

  useEffect(() => {
    void reload()
  }, [reload])

  return { cancel, reload, state }
}
