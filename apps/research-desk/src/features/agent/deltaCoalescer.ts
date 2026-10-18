import type { ResearchRunEvent } from '@/features/researchRuns/types'

const DELTA_EVENT = 'inqtrix.output_text.delta'

type Flush = () => void

/** Upper bound on how long a merged delta may wait when animation frames
 * do not run (a hidden tab pauses requestAnimationFrame): the backstop
 * timer flushes so the pending text can never fall behind a reconnect
 * catch-up that arrives on the direct history path. */
export const COALESCE_BACKSTOP_MS = 120

/** Schedule one flush per animation frame with a timer backstop; a test may
 * pass its own scheduler. Whichever fires first flushes, the other is a no-op. */
function frameScheduler(flush: Flush): void {
  let done = false
  const once = () => {
    if (done) return
    done = true
    flush()
  }
  if (typeof requestAnimationFrame === 'function') requestAnimationFrame(once)
  setTimeout(once, COALESCE_BACKSTOP_MS)
}

/**
 * Coalesce consecutive answer deltas of ONE publication into a single
 * event per animation frame.
 *
 * The server replays a finished answer as a burst of small deltas, and a
 * live answer arrives token by token; each one was a reducer dispatch and
 * a Markdown re-parse. Merging contiguous deltas keeps the reducer's own
 * gap check intact (the merged frame starts at the first offset and its
 * byte length is the sum, so a real gap still surfaces) and never
 * reorders: any other event flushes the pending delta first, and a caller
 * that dispatches a history batch on another path calls `flush()` before
 * it (the channel's replay cursor already counts the pending deltas).
 */
export function createRunEventCoalescer(
  sink: (event: ResearchRunEvent) => void,
  schedule: (flush: Flush) => void = frameScheduler,
): { push: (event: ResearchRunEvent) => void; flush: Flush } {
  let pending: ResearchRunEvent | null = null
  let pendingNextOffset = 0
  let scheduled = false
  const flush: Flush = () => {
    scheduled = false
    if (!pending) return
    const event = pending
    pending = null
    sink(event)
  }
  const push = (event: ResearchRunEvent): void => {
    if (event.type !== DELTA_EVENT) {
      flush()
      sink(event)
      return
    }
    const delta = typeof event.data.delta === 'string' ? event.data.delta : null
    const offset = typeof event.data.offset === 'number' ? event.data.offset : null
    if (delta === null || offset === null) {
      flush()
      sink(event)
      return
    }
    const bytes = new TextEncoder().encode(delta).byteLength
    if (
      pending
      && pending.run_id === event.run_id
      && pending.data.artifact_id === event.data.artifact_id
      && pending.data.publication_id === event.data.publication_id
      && offset === pendingNextOffset
    ) {
      pending = {
        ...event,
        data: { ...pending.data, delta: `${pending.data.delta as string}${delta}` },
      }
      pendingNextOffset += bytes
      return
    }
    flush()
    pending = { ...event, data: { ...event.data } }
    pendingNextOffset = offset + bytes
    if (!scheduled) {
      scheduled = true
      schedule(flush)
    }
  }
  return { push, flush }
}
