export type ReadyGateOutcome = 'cancelled' | 'failed' | 'ready'

export type ReadyGateState = {
  failed: boolean
  /** What the send belongs to: the open thread or session. */
  key: string | null
  pending: boolean
}

/**
 * Lets a send wait until what it belongs to is ready on screen (a chat
 * thread's history has loaded, an agent session is the one shown). The view
 * feeds every committed state into `update`; a waiting send resolves with
 * `ready` once the same key is no longer pending, `failed` when its load
 * failed, or `cancelled` when another key was opened or a newer send took its
 * place. Resolving from the committed state means the send continues with
 * handlers that already see the ready state.
 */
export function createReadyGate(): {
  update: (state: ReadyGateState) => void
  wait: () => Promise<ReadyGateOutcome>
} {
  let current: ReadyGateState = { failed: false, key: null, pending: false }
  let waiter: { key: string | null; resolve: (outcome: ReadyGateOutcome) => void } | null = null
  const settle = () => {
    if (!waiter) return
    let outcome: ReadyGateOutcome | null = null
    if (current.key !== waiter.key) outcome = 'cancelled'
    else if (current.failed) outcome = 'failed'
    else if (!current.pending) outcome = 'ready'
    if (outcome === null) return
    const { resolve } = waiter
    waiter = null
    resolve(outcome)
  }
  return {
    update(state) {
      current = state
      settle()
    },
    wait() {
      waiter?.resolve('cancelled')
      return new Promise<ReadyGateOutcome>((resolve) => {
        waiter = { key: current.key, resolve }
        settle()
      })
    },
  }
}
