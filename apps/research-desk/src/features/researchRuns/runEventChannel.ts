/**
 * Run-event transport with a polling fallback.
 *
 * The app is fetch-first with wake signals (events flip stale
 * flags, rows are truth), so the CONTENT never depends on streaming —
 * only the wake channel does. Primary transport is SSE; when the stream
 * fails to open or dies without a terminal event (SSE-buffering
 * proxies, flaky networks), the channel degrades VISIBLY to keyset
 * polling over the same replay buffer (`?format=json&after=N`) and
 * periodically retries SSE in the background, switching back on
 * success. Sequence numbers make the handoff lossless in both
 * directions (events are applied idempotently downstream).
 *
 * The core is dependency-injected so the switching contract is
 * unit-testable in the node-only vitest setup.
 */

import {
  fetchRunEventsPage,
  streamResearchRunEvents,
  type ClientOptions,
} from '@/api/inqtrixClient'
import type { ResearchRunEvent } from './types'

export type RunEventTransport = 'sse' | 'polling'

export type RunEventsPage = {
  events: ResearchRunEvent[]
  terminal: boolean
}

export type RunEventChannelDeps = {
  /** Open the SSE stream after *afterSequence*, delivering every event
   * through *deliver*; resolves when the stream ENDS (`sawTerminal`
   * distinguishes an ordered end from a transport cut); throws when it
   * cannot open / dies hard. */
  streamSse: (
    afterSequence: number | null,
    deliver: (event: ResearchRunEvent) => void,
    attempt: { signal: AbortSignal; onActivity: () => void },
  ) => Promise<{ sawTerminal: boolean }>
  /** One polling page over the same replay buffer. */
  pollOnce: (afterSequence: number | null) => Promise<RunEventsPage>
  /** Whether this run should currently POLL instead of holding a stream.
   *
   * A parked run produces no events, but its SSE stream still occupies
   * one of the browser's six connections per origin — and the gateway
   * speaks HTTP/1.1. With two agent runs open, everything else starved:
   * approval gates never rendered, `/quota/usage` and even
   * `/api/auth/session` hung while the server answered curl in 27 ms.
   *
   * Polling costs one SHORT request per cycle and hands the connection
   * back between them. The wake signal is unchanged — same replay
   * buffer, same sequence numbers, applied idempotently downstream. */
  preferPolling?: () => boolean
  /** Poll cadence while parked. Slower than the outage cadence on
   * purpose: a parked run has nothing to report. */
  parkedPollIntervalMs?: number
  onEvent: (event: ResearchRunEvent) => void
  /** HISTORY delivery: events the transport replayed as catch-up (SSE frames
   * before the server's `inqtrix.stream.live` boundary marker, or the first
   * polling page after a (re)start) arrive here as ONE batch instead of N
   * `onEvent` calls. History renders in place — only what arrives after the
   * boundary is genuinely new and may animate. Omitted: history falls back
   * to per-event `onEvent` (the pre-marker behaviour). */
  onHistory?: (events: ResearchRunEvent[]) => void
  /** Visible degradation hook (No Silent Fallbacks): fired only after
   * the transport is proven active or polling takes over. */
  onTransportChange?: (transport: RunEventTransport) => void
  /** Terminal disposition: the poll answered 404 (this run is no longer
   * available to this session) or 401 (credentials gone). Polling AND
   * the SSE retry end for good; the promise resolves instead of
   * rejecting. The 404 is deliberately non-disclosing on the server
   * (revoked share, deleted run, expired retention, foreign workspace
   * and unknown id all answer identically), so the callback carries no
   * reason beyond the status -- the UI must say "no longer available",
   * never "access revoked". The channel only decides that RETRYING is
   * pointless for both statuses; how each surface PRESENTS them (calm
   * lock for 404, loud auth error for 401 -- cookie mode reloads
   * globally, apikey mode has no reload) is the consumer's call. */
  onUnavailable?: (status: 401 | 404) => void
  signal: AbortSignal
  /** Poll cadence while degraded (default 3000 ms). */
  pollIntervalMs?: number
  /** Polls between background SSE retry attempts (default 10). */
  pollsPerSseRetry?: number
  /** Maximum wait for the first SSE bytes (default 15000 ms). */
  firstActivityTimeoutMs?: number
  /** Maximum quiet period after SSE became live (default 30000 ms). */
  inactivityTimeoutMs?: number
  /** Injectable for tests. */
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>
}

function defaultSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const timer = window.setTimeout(done, ms)
    function done() {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }
    function onAbort() {
      window.clearTimeout(timer)
      done()
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
}

/**
 * Subscribe until a terminal event or abort. Never rejects after the
 * first successful delivery path — transport errors degrade instead.
 * The initial SSE open failure ALSO degrades (the poller then reports
 * a genuine outage loudly by throwing once polling fails too).
 */
export async function subscribeRunEventsWithFallback(
  deps: RunEventChannelDeps,
): Promise<void> {
  const {
    onEvent,
    onHistory,
    onTransportChange,
    signal,
    pollIntervalMs = 3000,
    pollsPerSseRetry = 10,
    firstActivityTimeoutMs = 15_000,
    inactivityTimeoutMs = 30_000,
    parkedPollIntervalMs = 15_000,
    sleep = defaultSleep,
  } = deps
  /** A parked run polls; everything else streams. */
  const shouldStream = () => !deps.preferPolling?.()
  let lastSequence: number | null = null
  let reportedTransport: RunEventTransport | null = null
  const reportTransport = (transport: RunEventTransport) => {
    if (reportedTransport === transport) return
    reportedTransport = transport
    onTransportChange?.(transport)
  }
  const noteSequence = (event: ResearchRunEvent) => {
    if (typeof event.sequence === 'number') {
      lastSequence
        = lastSequence === null
          ? event.sequence
          : Math.max(lastSequence, event.sequence)
    }
  }
  const deliver = (event: ResearchRunEvent) => {
    noteSequence(event)
    onEvent(event)
  }
  const deliverHistory = (events: ResearchRunEvent[]) => {
    if (events.length === 0) return
    for (const event of events) noteSequence(event)
    if (onHistory) onHistory(events)
    else for (const event of events) onEvent(event)
  }

  const trySse = async (): Promise<'terminal' | 'ended' | 'failed'> => {
    if (signal.aborted) return 'terminal'
    const attemptController = new AbortController()
    let attemptActive = true
    // Catch-up phase of THIS attempt: frames before the server's boundary
    // marker are buffered history. `null` = the marker arrived, we are live.
    // A stream that ends or dies before the marker (terminal runs end right
    // after their replay; transport cuts) flushes the buffer in the finally —
    // everything it carried was replay by definition.
    let catchUp: ResearchRunEvent[] | null = []
    const flushCatchUp = () => {
      if (!catchUp) return
      const buffered = catchUp
      catchUp = null
      deliverHistory(buffered)
    }
    let activitySeen = false
    let watchdog: number | null = null
    let rejectInterruption: (reason: unknown) => void = () => undefined
    const interruption = new Promise<never>((_resolve, reject) => {
      rejectInterruption = reject
    })
    const clearWatchdog = () => {
      if (watchdog === null) return
      globalThis.clearTimeout(watchdog)
      watchdog = null
    }
    const interrupt = (message: string) => {
      if (!attemptActive) return
      const reason = new DOMException(message, 'TimeoutError')
      attemptController.abort(reason)
      rejectInterruption(reason)
    }
    const armWatchdog = (timeoutMs: number, message: string) => {
      clearWatchdog()
      watchdog = globalThis.setTimeout(
        () => interrupt(message),
        timeoutMs,
      ) as unknown as number
    }
    const onActivity = () => {
      if (!attemptActive) return
      if (!activitySeen) {
        activitySeen = true
        reportTransport('sse')
      }
      armWatchdog(
        inactivityTimeoutMs,
        'Inqtrix run event stream became inactive.',
      )
    }
    const onParentAbort = () => {
      if (!attemptActive) return
      const reason = new DOMException(
        'Inqtrix run event subscription was aborted.',
        'AbortError',
      )
      attemptController.abort(reason)
      rejectInterruption(reason)
    }
    signal.addEventListener('abort', onParentAbort, { once: true })
    armWatchdog(
      firstActivityTimeoutMs,
      'Inqtrix run event stream produced no initial activity.',
    )
    try {
      const { sawTerminal } = await Promise.race([
        deps.streamSse(
          lastSequence,
          (event) => {
            if (!attemptActive) return
            onActivity()
            if (event.type === STREAM_LIVE_MARKER_TYPE) {
              // Transport state, never a run event: consume it here — it must
              // not reach consumers and carries no sequence to track.
              flushCatchUp()
              return
            }
            if (catchUp) {
              catchUp.push(event)
              return
            }
            deliver(event)
          },
          { signal: attemptController.signal, onActivity },
        ),
        interruption,
      ])
      return sawTerminal ? 'terminal' : 'ended'
    } catch (error) {
      if (signal.aborted) return 'terminal'
      console.warn('Inqtrix run event stream failed; polling instead.', error)
      return 'failed'
    } finally {
      attemptActive = false
      flushCatchUp()
      clearWatchdog()
      signal.removeEventListener('abort', onParentAbort)
      if (!attemptController.signal.aborted) attemptController.abort()
    }
  }

  let outcome: 'terminal' | 'ended' | 'failed' | 'parked'
    = shouldStream() ? await trySse() : 'parked'
  while (outcome !== 'terminal' && !signal.aborted) {
    reportTransport('polling')
    // WHY this episode polls decides how it ends. Parked: back to the
    // stream the moment the run wakes. Transport outage: keep the full
    // retry budget, so a flaky network is not hammered with reconnects.
    const parked = outcome === 'parked'
    let pollsSinceRetry = 0
    // First page of each polling episode = catch-up (everything missed while
    // the stream was down); later pages arrive on cadence and count as live.
    // Decided here in the transport — no timestamp heuristics, no clock skew.
    let firstPollPage = true
    for (;;) {
      if (signal.aborted) return
      let page: RunEventsPage
      try {
        page = await deps.pollOnce(lastSequence)
      } catch (error) {
        if (signal.aborted) return
        const status = (error as { status?: number }).status
        if (status === 401 || status === 404) {
          // The run is gone for this session -- not a transport outage.
          // Retrying (polling or SSE) would hammer a deliberate answer.
          deps.onUnavailable?.(status)
          return
        }
        throw error
      }
      if (firstPollPage) {
        firstPollPage = false
        deliverHistory(page.events)
      } else {
        for (const event of page.events) deliver(event)
      }
      if (page.terminal) return
      pollsSinceRetry += 1
      if (parked ? shouldStream() : pollsSinceRetry >= pollsPerSseRetry) break
      await sleep(parked ? parkedPollIntervalMs : pollIntervalMs, signal)
    }
    if (signal.aborted) return
    outcome = shouldStream() ? await trySse() : 'parked'
  }
}

/** The server's replay/live boundary on the SSE stream: everything before
 * this frame was served from the buffer (history), everything after is live.
 * Emitted by the runs router; consumed HERE — it never reaches consumers. */
const STREAM_LIVE_MARKER_TYPE = 'inqtrix.stream.live'

const TERMINAL_EVENT_TYPES = new Set([
  'inqtrix.run.completed',
  'inqtrix.run.failed',
  'inqtrix.run.cancelled',
])

/** The client-bound channel: SSE via the shared reader, polling via the
 * `?format=json` page — the ONE subscription entry point for run event
 * consumers. */
export function subscribeRunEvents(params: {
  eventsUrl: string
  options: ClientOptions
  signal: AbortSignal
  /** See RunEventChannelDeps.preferPolling. */
  preferPolling?: () => boolean
  onEvent: (event: ResearchRunEvent) => void
  /** Catch-up batches (see RunEventChannelDeps.onHistory). */
  onHistory?: (events: ResearchRunEvent[]) => void
  onTransportChange?: (transport: RunEventTransport) => void
  /** See RunEventChannelDeps.onUnavailable. */
  onUnavailable?: (status: 401 | 404) => void
}): Promise<void> {
  const separator = params.eventsUrl.includes('?') ? '&' : '?'
  return subscribeRunEventsWithFallback({
    streamSse: async (afterSequence, deliver, attempt) => {
      let sawTerminal = false
      const url
        = afterSequence === null
          ? params.eventsUrl
          : `${params.eventsUrl}${separator}after=${afterSequence}`
      await streamResearchRunEvents(url, {
        ...params.options,
        signal: attempt.signal,
        onActivity: attempt.onActivity,
        onEvent: (event) => {
          if (TERMINAL_EVENT_TYPES.has(event.type)) sawTerminal = true
          deliver(event)
        },
      })
      return { sawTerminal }
    },
    pollOnce: async (afterSequence) => {
      const page = await fetchRunEventsPage(params.eventsUrl, afterSequence, {
        ...params.options,
        signal: params.signal,
      })
      return { events: page.data, terminal: page.terminal }
    },
    onEvent: params.onEvent,
    onHistory: params.onHistory,
    onTransportChange: params.onTransportChange,
    onUnavailable: params.onUnavailable,
    preferPolling: params.preferPolling,
    signal: params.signal,
  })
}


/** Statuses in which a run waits for a HUMAN and emits nothing. */
const PARKED_RUN_STATUSES = new Set([
  'waiting_for_approval',
  'waiting_for_input',
])

export function isParkedRunStatus(status: string): boolean {
  return PARKED_RUN_STATUSES.has(status)
}

/**
 * Keep the parked set in step with the run's own lifecycle events.
 *
 * `run.waiting` carries the status it parked in — a children-wait is
 * NOT parked: the parent is idle but its children stream through it, so
 * closing that stream would blind the surface exactly while a delegated
 * run works. Anything that resumes, completes or dies clears the flag,
 * so a run never stays on the slow poll after it woke.
 */
export function noteParkedTransition(
  parked: Set<string>,
  event: Pick<ResearchRunEvent, 'run_id' | 'type' | 'data'>,
): void {
  if (event.type === 'inqtrix.run.waiting') {
    const status = (event.data as { status?: unknown } | undefined)?.status
    if (typeof status === 'string' && isParkedRunStatus(status)) {
      parked.add(event.run_id)
      return
    }
    parked.delete(event.run_id)
    return
  }
  if (
    event.type === 'inqtrix.run.resumed'
    || event.type === 'inqtrix.run.started'
    || event.type === 'inqtrix.run.completed'
    || event.type === 'inqtrix.run.failed'
    || event.type === 'inqtrix.run.cancelled'
  ) {
    parked.delete(event.run_id)
  }
}
