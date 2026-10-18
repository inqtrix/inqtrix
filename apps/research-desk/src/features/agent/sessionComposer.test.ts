import { describe, expect, it } from 'vitest'

import {
  agentComposerAvailability,
  carryCollectionsToNewSession,
  collectionIdsForSession,
  startExclusive,
  withSessionCollectionIds,
} from './sessionComposer'

describe('the agent composer while a session stages', () => {
  it('keeps the composer usable and lets only the send wait', () => {
    // The whole composer was inert while the selected session staged: text
    // typed in that window was dropped without a trace.
    expect(agentComposerAvailability({
      agentAvailable: true,
      runningRunLocked: false,
      surfaceTransitioning: true,
    })).toEqual({ disabled: false, sendWaits: true })
    expect(agentComposerAvailability({
      agentAvailable: true,
      runningRunLocked: false,
      surfaceTransitioning: false,
    })).toEqual({ disabled: false, sendWaits: false })
  })

  it('still locks the composer when the agent is unavailable or a run holds it', () => {
    expect(agentComposerAvailability({
      agentAvailable: false,
      runningRunLocked: false,
      surfaceTransitioning: false,
    }).disabled).toBe(true)
    expect(agentComposerAvailability({
      agentAvailable: true,
      runningRunLocked: true,
      surfaceTransitioning: false,
    }).disabled).toBe(true)
  })
})

describe('creating a session', () => {
  it('starts one creation at a time and reports it as busy until it settled', async () => {
    const flag = { current: false }
    const busy: boolean[] = []
    let finish: () => void = () => undefined
    let started = 0
    const start = () => {
      started += 1
      return new Promise<void>((resolve) => { finish = resolve })
    }
    expect(startExclusive(flag, start, (value) => { busy.push(value) })).toBe(true)
    expect(startExclusive(flag, start, (value) => { busy.push(value) })).toBe(false)
    expect(started).toBe(1)
    finish()
    await Promise.resolve()
    await Promise.resolve()
    expect(busy).toEqual([true, false])
    expect(startExclusive(flag, async () => undefined, () => undefined)).toBe(true)
  })
})

describe('collection chips of an agent session', () => {
  it('keeps each session to its own chips', () => {
    // The chips were one list for the whole desk: a collection attached in
    // one session rode along into the next question of another session.
    const withA = withSessionCollectionIds({}, 'session-a', ['col-1'])
    expect(collectionIdsForSession(withA, 'session-a')).toEqual(['col-1'])
    expect(collectionIdsForSession(withA, 'session-b')).toEqual([])
    const cleared = withSessionCollectionIds(withA, 'session-a', [])
    expect(collectionIdsForSession(cleared, 'session-a')).toEqual([])
  })

  it('hands the chips of an unsaved first question to the session it creates', () => {
    const unsaved = withSessionCollectionIds({}, null, ['col-1'])
    const carried = carryCollectionsToNewSession(unsaved, null, 'session-new')
    expect(collectionIdsForSession(carried, 'session-new')).toEqual(['col-1'])
    expect(collectionIdsForSession(carried, null)).toEqual([])
    // An existing session keeps its own chips.
    const existing = withSessionCollectionIds(unsaved, 'session-old', ['col-2'])
    expect(collectionIdsForSession(
      carryCollectionsToNewSession(existing, null, 'session-old'),
      'session-old',
    )).toEqual(['col-2'])
  })
})
