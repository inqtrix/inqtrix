import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ResearchRunEvent } from '@/features/researchRuns/types'
import { COALESCE_BACKSTOP_MS, createRunEventCoalescer } from './deltaCoalescer'

function delta(offset: number, text: string, sequence: number): ResearchRunEvent {
  return {
    type: 'inqtrix.output_text.delta',
    run_id: 'run-1',
    sequence,
    created_at: 1_700_000_000 + sequence,
    data: { artifact_id: 'answer-1', publication_id: 'pub-1', delta: text, offset },
  }
}

function harness() {
  const sunk: ResearchRunEvent[] = []
  const flushes: Array<() => void> = []
  const coalescer = createRunEventCoalescer(
    (event) => sunk.push(event),
    (flush) => flushes.push(flush),
  )
  return { coalescer, sunk, flushes, runFrame: () => flushes.splice(0).forEach((flush) => flush()) }
}

describe('createRunEventCoalescer', () => {
  it('merges a burst of contiguous deltas into one event per frame', () => {
    const { coalescer, sunk, flushes, runFrame } = harness()
    let offset = 0
    for (let index = 0; index < 50; index += 1) {
      const text = `w${index} `
      coalescer.push(delta(offset, text, index + 1))
      offset += new TextEncoder().encode(text).byteLength
    }
    expect(sunk).toHaveLength(0)
    expect(flushes).toHaveLength(1)
    runFrame()
    expect(sunk).toHaveLength(1)
    expect(sunk[0].data.offset).toBe(0)
    expect(sunk[0].data.delta).toBe(Array.from({ length: 50 }, (_, i) => `w${i} `).join(''))
    // The merged frame carries the LAST sequence so lastSequence advances.
    expect(sunk[0].sequence).toBe(50)
  })

  it('flushes the pending delta before any other event, keeping order', () => {
    const { coalescer, sunk } = harness()
    coalescer.push(delta(0, 'Hallo ', 1))
    coalescer.push({ type: 'inqtrix.answer.ready', run_id: 'run-1', sequence: 2, created_at: 2, data: { artifact_id: 'answer-1', publication_id: 'pub-1' } })
    expect(sunk.map((event) => event.type)).toEqual(['inqtrix.output_text.delta', 'inqtrix.answer.ready'])
  })

  it('never bridges a gap: a non-contiguous offset starts a new frame', () => {
    const { coalescer, sunk, runFrame } = harness()
    coalescer.push(delta(0, 'ab', 1))
    coalescer.push(delta(5, 'cd', 2))
    runFrame()
    expect(sunk).toHaveLength(2)
    expect(sunk.map((event) => event.data.offset)).toEqual([0, 5])
  })

  it('counts UTF-8 bytes, not characters, when chaining offsets', () => {
    const { coalescer, sunk, runFrame } = harness()
    coalescer.push(delta(0, 'ä', 1)) // 2 bytes
    coalescer.push(delta(2, 'b', 2))
    runFrame()
    expect(sunk).toHaveLength(1)
    expect(sunk[0].data.delta).toBe('äb')
  })

  it('keeps deltas of different publications apart', () => {
    const { coalescer, sunk, runFrame } = harness()
    coalescer.push(delta(0, 'a', 1))
    coalescer.push({ ...delta(1, 'b', 2), data: { ...delta(1, 'b', 2).data, publication_id: 'pub-2' } })
    runFrame()
    expect(sunk).toHaveLength(2)
  })

  it('flushes on the timer backstop when no animation frame runs (hidden tab)', () => {
    vi.useFakeTimers()
    const sunk: ResearchRunEvent[] = []
    const coalescer = createRunEventCoalescer((event) => sunk.push(event))
    coalescer.push(delta(0, 'a', 1))
    expect(sunk).toHaveLength(0)
    vi.advanceTimersByTime(COALESCE_BACKSTOP_MS + 1)
    expect(sunk).toHaveLength(1)
  })

  it('exposes flush so a history batch on another path can drain it first', () => {
    const { coalescer, sunk } = harness()
    coalescer.push(delta(0, 'a', 1))
    coalescer.flush()
    expect(sunk).toHaveLength(1)
    coalescer.flush()
    expect(sunk).toHaveLength(1)
  })
})

afterEach(() => {
  vi.useRealTimers()
})
