import { describe, expect, it, vi } from 'vitest'

import {
  noteParkedTransition,
  subscribeRunEventsWithFallback,
  type RunEventChannelDeps,
  type RunEventTransport,
} from './runEventChannel'
import type { ResearchRunEvent } from './types'

function event(sequence: number, type = 'inqtrix.agent.activity'): ResearchRunEvent {
  return {
    type,
    run_id: 'run_1',
    sequence,
    created_at: sequence,
    data: {},
  } as ResearchRunEvent
}

const instantSleep: NonNullable<RunEventChannelDeps['sleep']> = () =>
  Promise.resolve()

describe('subscribeRunEventsWithFallback', () => {
  it('stays on SSE when the stream ends with a terminal event', async () => {
    const seen: number[] = []
    const transports: RunEventTransport[] = []
    await subscribeRunEventsWithFallback({
      streamSse: async (_after, deliver) => {
        deliver(event(1))
        deliver(event(2, 'inqtrix.run.completed'))
        return { sawTerminal: true }
      },
      pollOnce: async () => {
        throw new Error('must not poll')
      },
      onEvent: (e) => seen.push(e.sequence),
      onTransportChange: (t) => transports.push(t),
      signal: new AbortController().signal,
      sleep: instantSleep,
    })
    expect(seen).toEqual([1, 2])
    expect(transports).toEqual(['sse'])
  })

  it('degrades to polling after an SSE failure and resumes at the last sequence', async () => {
    const seen: number[] = []
    const transports: RunEventTransport[] = []
    const polledAfter: (number | null)[] = []
    let sseAttempts = 0
    await subscribeRunEventsWithFallback({
      streamSse: async (_after, deliver) => {
        sseAttempts += 1
        deliver(event(1))
        deliver(event(2))
        throw new Error('proxy cut the stream')
      },
      pollOnce: async (after) => {
        polledAfter.push(after)
        return {
          events: [event(3), event(4, 'inqtrix.run.completed')],
          terminal: true,
        }
      },
      onEvent: (e) => seen.push(e.sequence),
      onTransportChange: (t) => transports.push(t),
      signal: new AbortController().signal,
      sleep: instantSleep,
    })
    expect(sseAttempts).toBe(1)
    // No event lost, no duplicate needed: the poll starts AFTER the
    // last delivered sequence.
    expect(polledAfter).toEqual([2])
    expect(seen).toEqual([1, 2, 3, 4])
    expect(transports).toEqual(['sse', 'polling'])
  })

  it('retries SSE after pollsPerSseRetry polls and switches back on success', async () => {
    const transports: RunEventTransport[] = []
    const seen: number[] = []
    let sseAttempts = 0
    let polls = 0
    await subscribeRunEventsWithFallback({
      streamSse: async (_after, deliver) => {
        sseAttempts += 1
        if (sseAttempts === 1) throw new Error('down')
        deliver(event(10, 'inqtrix.run.completed'))
        return { sawTerminal: true }
      },
      pollOnce: async () => {
        polls += 1
        return { events: [event(polls)], terminal: false }
      },
      onEvent: (e) => seen.push(e.sequence),
      onTransportChange: (t) => transports.push(t),
      signal: new AbortController().signal,
      pollsPerSseRetry: 2,
      sleep: instantSleep,
    })
    expect(sseAttempts).toBe(2)
    expect(polls).toBe(2)
    expect(seen).toEqual([1, 2, 10])
    expect(transports).toEqual(['polling', 'sse'])
  })

  it('treats a clean SSE end WITHOUT terminal as a transport cut', async () => {
    const transports: RunEventTransport[] = []
    await subscribeRunEventsWithFallback({
      streamSse: async () => ({ sawTerminal: false }),
      pollOnce: async () => ({
        events: [event(1, 'inqtrix.run.completed')],
        terminal: true,
      }),
      onEvent: () => undefined,
      onTransportChange: (t) => transports.push(t),
      signal: new AbortController().signal,
      sleep: instantSleep,
    })
    expect(transports).toEqual(['polling'])
  })

  it('aborts a stream that never produces activity and falls back to polling', async () => {
    const transports: RunEventTransport[] = []
    let attemptAborted = false
    await subscribeRunEventsWithFallback({
      streamSse: async (_after, _deliver, attempt) =>
        new Promise((_resolve, reject) => {
          attempt.signal.addEventListener('abort', () => {
            attemptAborted = true
            reject(attempt.signal.reason)
          }, { once: true })
        }),
      pollOnce: async () => ({
        events: [event(1, 'inqtrix.run.completed')],
        terminal: true,
      }),
      onEvent: () => undefined,
      onTransportChange: (transport) => transports.push(transport),
      signal: new AbortController().signal,
      firstActivityTimeoutMs: 1,
      sleep: instantSleep,
    })
    expect(attemptAborted).toBe(true)
    expect(transports).toEqual(['polling'])
  })

  it('falls back after an active stream stalls and polls after the last sequence', async () => {
    const transports: RunEventTransport[] = []
    const polledAfter: Array<number | null> = []
    await subscribeRunEventsWithFallback({
      streamSse: async (_after, deliver, attempt) => {
        deliver(event(7))
        return new Promise((_resolve, reject) => {
          attempt.signal.addEventListener(
            'abort',
            () => reject(attempt.signal.reason),
            { once: true },
          )
        })
      },
      pollOnce: async (after) => {
        polledAfter.push(after)
        return {
          events: [event(8, 'inqtrix.run.completed')],
          terminal: true,
        }
      },
      onEvent: () => undefined,
      onTransportChange: (transport) => transports.push(transport),
      signal: new AbortController().signal,
      inactivityTimeoutMs: 1,
      sleep: instantSleep,
    })
    expect(polledAfter).toEqual([7])
    expect(transports).toEqual(['sse', 'polling'])
  })

  it('treats a comment heartbeat as real SSE activity', async () => {
    const transports: RunEventTransport[] = []
    await subscribeRunEventsWithFallback({
      streamSse: async (_after, _deliver, attempt) => {
        attempt.onActivity()
        return new Promise((_resolve, reject) => {
          attempt.signal.addEventListener(
            'abort',
            () => reject(attempt.signal.reason),
            { once: true },
          )
        })
      },
      pollOnce: async () => ({
        events: [event(1, 'inqtrix.run.completed')],
        terminal: true,
      }),
      onEvent: () => undefined,
      onTransportChange: (transport) => transports.push(transport),
      signal: new AbortController().signal,
      inactivityTimeoutMs: 1,
      sleep: instantSleep,
    })
    expect(transports).toEqual(['sse', 'polling'])
  })

  it('keeps reporting polling while a reconnect has no activity', async () => {
    const transports: RunEventTransport[] = []
    let attempts = 0
    let polls = 0
    await subscribeRunEventsWithFallback({
      streamSse: async (_after, _deliver, attempt) => {
        attempts += 1
        if (attempts === 1) throw new Error('down')
        return new Promise((_resolve, reject) => {
          attempt.signal.addEventListener(
            'abort',
            () => reject(attempt.signal.reason),
            { once: true },
          )
        })
      },
      pollOnce: async () => {
        polls += 1
        return {
          events: polls === 2 ? [event(2, 'inqtrix.run.completed')] : [],
          terminal: polls === 2,
        }
      },
      onEvent: () => undefined,
      onTransportChange: (transport) => transports.push(transport),
      signal: new AbortController().signal,
      firstActivityTimeoutMs: 1,
      pollsPerSseRetry: 1,
      sleep: instantSleep,
    })
    expect(attempts).toBe(2)
    expect(transports).toEqual(['polling'])
  })

  it('stops promptly on abort while polling', async () => {
    const controller = new AbortController()
    let polls = 0
    await subscribeRunEventsWithFallback({
      streamSse: async () => {
        throw new Error('down')
      },
      pollOnce: async () => {
        polls += 1
        controller.abort()
        return { events: [], terminal: false }
      },
      onEvent: () => undefined,
      signal: controller.signal,
      sleep: instantSleep,
    })
    expect(polls).toBe(1)
  })

  it('does not open a stream when the parent signal is already aborted', async () => {
    const controller = new AbortController()
    controller.abort()
    let attempts = 0
    await subscribeRunEventsWithFallback({
      streamSse: async () => {
        attempts += 1
        return { sawTerminal: false }
      },
      pollOnce: async () => {
        throw new Error('must not poll')
      },
      onEvent: () => undefined,
      signal: controller.signal,
      sleep: instantSleep,
    })
    expect(attempts).toBe(0)
  })
})

describe('replay/live boundary (history batching)', () => {
  it('buffers frames before the inqtrix.stream.live marker as ONE history batch', async () => {
    const history: number[][] = []
    const live: number[] = []
    await subscribeRunEventsWithFallback({
      streamSse: async (_after, deliver) => {
        deliver(event(1))
        deliver(event(2))
        deliver(event(3, 'inqtrix.stream.live'))
        deliver(event(4))
        deliver(event(5, 'inqtrix.run.completed'))
        return { sawTerminal: true }
      },
      pollOnce: async () => {
        throw new Error('must not poll')
      },
      onEvent: (e) => live.push(e.sequence),
      onHistory: (events) => history.push(events.map((e) => e.sequence)),
      signal: new AbortController().signal,
      sleep: instantSleep,
    })
    // The marker itself is transport state and reaches neither channel.
    expect(history).toEqual([[1, 2]])
    expect(live).toEqual([4, 5])
  })

  it('flushes an unfinished catch-up as history when the stream ends before a marker', async () => {
    // Terminal runs end right after their replay and never emit the marker —
    // everything the attempt carried was replay by definition.
    const history: number[][] = []
    const live: number[] = []
    await subscribeRunEventsWithFallback({
      streamSse: async (_after, deliver) => {
        deliver(event(1))
        deliver(event(2, 'inqtrix.run.completed'))
        return { sawTerminal: true }
      },
      pollOnce: async () => {
        throw new Error('must not poll')
      },
      onEvent: (e) => live.push(e.sequence),
      onHistory: (events) => history.push(events.map((e) => e.sequence)),
      signal: new AbortController().signal,
      sleep: instantSleep,
    })
    expect(history).toEqual([[1, 2]])
    expect(live).toEqual([])
  })

  it('treats the first polling page as history and later pages as live', async () => {
    const history: number[][] = []
    const live: number[] = []
    let polls = 0
    await subscribeRunEventsWithFallback({
      streamSse: async () => {
        throw new Error('kein Stream')
      },
      pollOnce: async () => {
        polls += 1
        if (polls === 1) return { events: [event(1), event(2)], terminal: false }
        return { events: [event(3, 'inqtrix.run.completed')], terminal: true }
      },
      onEvent: (e) => live.push(e.sequence),
      onHistory: (events) => history.push(events.map((e) => e.sequence)),
      signal: new AbortController().signal,
      sleep: instantSleep,
    })
    expect(history).toEqual([[1, 2]])
    expect(live).toEqual([3])
  })

  it('resumes AFTER buffered history sequences (no re-fetch of flushed replay)', async () => {
    const polledAfter: (number | null)[] = []
    await subscribeRunEventsWithFallback({
      streamSse: async (_after, deliver) => {
        deliver(event(7))
        throw new Error('Stream riss nach dem Replay ab')
      },
      pollOnce: async (after) => {
        polledAfter.push(after)
        return { events: [event(8, 'inqtrix.run.completed')], terminal: true }
      },
      onEvent: () => undefined,
      onHistory: () => undefined,
      signal: new AbortController().signal,
      sleep: instantSleep,
    })
    // The buffered (then flushed) event 7 advanced the cursor even though it
    // never went through the live path.
    expect(polledAfter).toEqual([7])
  })

  it('falls back to per-event delivery when no onHistory is provided', async () => {
    const seen: number[] = []
    await subscribeRunEventsWithFallback({
      streamSse: async (_after, deliver) => {
        deliver(event(1))
        deliver(event(2, 'inqtrix.stream.live'))
        deliver(event(3, 'inqtrix.run.completed'))
        return { sawTerminal: true }
      },
      pollOnce: async () => {
        throw new Error('must not poll')
      },
      onEvent: (e) => seen.push(e.sequence),
      signal: new AbortController().signal,
      sleep: instantSleep,
    })
    // Old consumers keep the flat stream; the marker still never leaks.
    expect(seen).toEqual([1, 3])
  })
})

describe('unavailable disposition (share revoked / run deleted)', () => {
  function httpError(status: number): Error {
    const error = new Error(`HTTP ${status}`)
    ;(error as Error & { status: number }).status = status
    return error
  }

  it('ends polling AND the SSE retry on a 404 poll and never rejects', async () => {
    // The revocation shape: the stream dies without a terminal event,
    // the fallback poll answers 404. Retrying would hammer a
    // deliberate, non-disclosing answer.
    let polls = 0
    let sseAttempts = 0
    const unavailable: number[] = []
    await subscribeRunEventsWithFallback({
      streamSse: async () => {
        sseAttempts += 1
        return { sawTerminal: false }
      },
      pollOnce: async () => {
        polls += 1
        throw httpError(404)
      },
      onEvent: () => undefined,
      onUnavailable: (status) => unavailable.push(status),
      signal: new AbortController().signal,
      pollsPerSseRetry: 1,
      sleep: instantSleep,
    })
    expect(unavailable).toEqual([404])
    expect(polls).toBe(1)
    expect(sseAttempts).toBe(1)
  })

  it('treats a 401 poll the same way (global reload already handled it)', async () => {
    const unavailable: number[] = []
    await subscribeRunEventsWithFallback({
      streamSse: async () => ({ sawTerminal: false }),
      pollOnce: async () => {
        throw httpError(401)
      },
      onEvent: () => undefined,
      onUnavailable: (status) => unavailable.push(status),
      signal: new AbortController().signal,
      sleep: instantSleep,
    })
    expect(unavailable).toEqual([401])
  })

  it('still rejects loudly on a non-availability poll failure', async () => {
    // A genuine outage (500, network) keeps the documented loud path:
    // reclassifying it as "unavailable" would hide a real incident.
    await expect(subscribeRunEventsWithFallback({
      streamSse: async () => ({ sawTerminal: false }),
      pollOnce: async () => {
        throw httpError(500)
      },
      onEvent: () => undefined,
      onUnavailable: () => {
        throw new Error('must not classify a 500 as unavailable')
      },
      signal: new AbortController().signal,
      sleep: instantSleep,
    })).rejects.toThrow('HTTP 500')
  })
})

describe('parked runs poll instead of holding a stream', () => {
  it('never opens a stream while the run is parked', async () => {
    // The regression: a parked run's SSE stream held one of the
    // browser's six connections per origin. With two agent runs open,
    // approval gates never rendered and even /api/auth/session hung.
    const streamSse = vi.fn()
    const pollOnce = vi
      .fn()
      .mockResolvedValue({ events: [], terminal: true })
    await subscribeRunEventsWithFallback({
      onEvent: () => undefined,
      pollOnce,
      preferPolling: () => true,
      signal: new AbortController().signal,
      streamSse,
    })
    expect(streamSse).not.toHaveBeenCalled()
    expect(pollOnce).toHaveBeenCalled()
  })

  it('returns to the stream as soon as the run wakes', async () => {
    let parked = true
    const streamSse = vi
      .fn()
      .mockResolvedValue({ sawTerminal: true })
    const pollOnce = vi.fn().mockImplementation(async () => {
      parked = false
      return { events: [], terminal: false }
    })
    await subscribeRunEventsWithFallback({
      onEvent: () => undefined,
      parkedPollIntervalMs: 0,
      pollOnce,
      preferPolling: () => parked,
      signal: new AbortController().signal,
      sleep: async () => undefined,
      streamSse,
    })
    // One poll noticed the wake-up; the stream took over immediately
    // instead of waiting out the outage retry budget.
    expect(pollOnce).toHaveBeenCalledTimes(1)
    expect(streamSse).toHaveBeenCalledTimes(1)
  })

  it('keeps the full retry budget when polling after an OUTAGE', async () => {
    // Not parked: a flaky network must not be hammered with reconnects.
    const streamSse = vi
      .fn()
      .mockRejectedValueOnce(new Error('stream tot'))
      .mockResolvedValue({ sawTerminal: true })
    let polls = 0
    const pollOnce = vi.fn().mockImplementation(async () => {
      polls += 1
      return { events: [], terminal: polls > 12 }
    })
    await subscribeRunEventsWithFallback({
      onEvent: () => undefined,
      pollOnce,
      pollsPerSseRetry: 3,
      preferPolling: () => false,
      signal: new AbortController().signal,
      sleep: async () => undefined,
      streamSse,
    })
    expect(pollOnce).toHaveBeenCalledTimes(3)
  })
})

describe('noteParkedTransition', () => {
  const waiting = (status: string): ResearchRunEvent => ({
    created_at: 0,
    data: { status },
    run_id: 'run_1',
    sequence: 1,
    type: 'inqtrix.run.waiting',
  })

  it('parks a run waiting on a human decision', () => {
    const parked = new Set<string>()
    noteParkedTransition(parked, waiting('waiting_for_approval'))
    expect(parked.has('run_1')).toBe(true)
    parked.clear()
    noteParkedTransition(parked, waiting('waiting_for_input'))
    expect(parked.has('run_1')).toBe(true)
  })

  it('does NOT park a parent waiting for its children', () => {
    // Its children stream THROUGH this connection: closing it would
    // blind the surface exactly while a delegated run does the work.
    const parked = new Set<string>()
    noteParkedTransition(parked, waiting('waiting_for_children'))
    expect(parked.has('run_1')).toBe(false)
  })

  it('unparks the moment the run wakes', () => {
    const parked = new Set(['run_1'])
    noteParkedTransition(parked, {
      data: {},
      run_id: 'run_1',
      type: 'inqtrix.run.resumed',
    })
    expect(parked.has('run_1')).toBe(false)
  })

  it('unparks on every terminal end', () => {
    for (const type of [
      'inqtrix.run.completed',
      'inqtrix.run.failed',
      'inqtrix.run.cancelled',
    ]) {
      const parked = new Set(['run_1'])
      noteParkedTransition(parked, {
        data: {},
        run_id: 'run_1',
        type,
      })
      expect(parked.has('run_1'), type).toBe(false)
    }
  })

  it('ignores ordinary progress events', () => {
    const parked = new Set(['run_1'])
    noteParkedTransition(parked, event(4))
    expect(parked.has('run_1')).toBe(true)
  })
})
