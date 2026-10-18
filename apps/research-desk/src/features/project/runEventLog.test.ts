import { describe, expect, it } from 'vitest'

import {
  appendRunEventRecord,
  RUN_EVENT_LOG_CAP,
  type ResearchRunEventRecord,
} from './types'

function ev(
  id: string,
  overrides: Partial<ResearchRunEventRecord> = {},
): ResearchRunEventRecord {
  return {
    active: true,
    createdAt: '2026-07-04T00:00:00.000Z',
    id,
    kind: 'progress',
    severity: 'info',
    title: id,
    ...overrides,
  }
}

describe('appendRunEventRecord', () => {
  it('flips the previous tail inactive and appends the new active tail', () => {
    const start = [ev('a')]
    const next = appendRunEventRecord(start, ev('b'))

    expect(next.map((e) => e.id)).toEqual(['a', 'b'])
    expect(next[0].active).toBe(false)
    expect(next[1].active).toBe(true)
    // Input is not mutated (pure).
    expect(start[0].active).toBe(true)
  })

  it('keeps head event object identity (no per-row re-render churn)', () => {
    const a = ev('a', { active: false })
    const b = ev('b', { active: false })
    const c = ev('c')
    const next = appendRunEventRecord([a, b, c], ev('d'))

    // The already-inactive head events are the SAME references; only the
    // prior active tail (c) is replaced.
    expect(next[0]).toBe(a)
    expect(next[1]).toBe(b)
    expect(next[2]).not.toBe(c)
    expect(next[2].active).toBe(false)
    expect(next[3].active).toBe(true)
  })

  it('drops a duplicate id (reconnect replay), keeping the first position', () => {
    const start = [ev('run-1', { active: false }), ev('run-2')]
    const next = appendRunEventRecord(start, ev('run-1'))

    // The replayed id is not re-appended; order is stable; nothing is active
    // (the prior tail was cleared and the replay was dropped).
    expect(next.map((e) => e.id)).toEqual(['run-1', 'run-2'])
    expect(next.some((e) => e.active)).toBe(false)
  })

  it('dedups a node-stable model id that sits far from the tail', () => {
    // Model-resolution events refire across rounds with the same id but are
    // separated by a whole round of other events -> the whole-array membership
    // test (not a trailing window) must still collapse them.
    let events: ResearchRunEventRecord[] = [
      ev('run-model-plan', { active: false }),
    ]
    for (let i = 0; i < 40; i += 1) {
      events = appendRunEventRecord(events, ev(`run-${i}`))
    }
    const before = events.length
    events = appendRunEventRecord(events, ev('run-model-plan'))

    expect(events.length).toBe(before) // refire dropped, not appended
    expect(events.filter((e) => e.id === 'run-model-plan')).toHaveLength(1)
  })

  it('maintains at-most-one-active across a long sequence', () => {
    let events: ResearchRunEventRecord[] = []
    for (let i = 0; i < 50; i += 1) {
      const terminal = i === 49
      events = appendRunEventRecord(
        events,
        ev(`e-${i}`, { active: terminal ? undefined : true }),
      )
      expect(events.filter((e) => e.active).length).toBeLessThanOrEqual(1)
    }
    // A terminal (inactive) tail leaves zero active.
    expect(events.some((e) => e.active)).toBe(false)
    expect(events[events.length - 1].id).toBe('e-49')
  })

  it('ring-caps the oldest events but never trims the active tail', () => {
    let events: ResearchRunEventRecord[] = []
    for (let i = 0; i < RUN_EVENT_LOG_CAP + 25; i += 1) {
      events = appendRunEventRecord(events, ev(`e-${i}`), { cap: RUN_EVENT_LOG_CAP })
    }
    expect(events.length).toBe(RUN_EVENT_LOG_CAP)
    // Oldest dropped, newest kept and active.
    expect(events[0].id).toBe('e-25')
    const last = events[events.length - 1]
    expect(last.id).toBe(`e-${RUN_EVENT_LOG_CAP + 24}`)
    expect(last.active).toBe(true)
  })

  it('appends onto an empty timeline', () => {
    const next = appendRunEventRecord([], ev('first'))
    expect(next).toHaveLength(1)
    expect(next[0].id).toBe('first')
  })
})
