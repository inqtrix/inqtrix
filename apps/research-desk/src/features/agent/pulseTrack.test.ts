import { describe, expect, it } from 'vitest'

import {
  AGENT_KERNEL_STATIONS,
  agentPhaseStation,
  agentPulseActiveIndex,
} from './model'

/** The stations the track paints as done, given the rule under test. */
function doneStations(
  station: Parameters<typeof agentPulseActiveIndex>[0],
  completed: boolean,
) {
  const activeIndex = agentPulseActiveIndex(station, completed)
  return AGENT_KERNEL_STATIONS.filter((_s, index) => index < activeIndex)
}

describe('how far the pulse track is filled', () => {
  it('draws the kernel line: the run is taken in, executed, and holds a result', () => {
    expect([...AGENT_KERNEL_STATIONS]).toEqual(['intake', 'execution', 'result'])
  })

  it('fills the whole line once the run completed', () => {
    expect(doneStations('execution', true)).toEqual(['intake', 'execution', 'result'])
    // Also for a run whose last reported phase was its first.
    expect(doneStations('intake', true)).toEqual(['intake', 'execution', 'result'])
  })

  it('leaves the result open while the run is still working', () => {
    expect(agentPulseActiveIndex('execution', false)).toBe(1)
    expect(doneStations('execution', false)).toEqual(['intake'])
  })

  it('keeps the station a run stopped at', () => {
    expect(agentPulseActiveIndex('intake', false)).toBe(0)
    expect(doneStations('intake', false)).toEqual([])
  })
})

describe('the station a phase reaches', () => {
  it('reaches the kernel stations by their phases', () => {
    expect(agentPhaseStation('intake')).toBe('intake')
    expect(agentPhaseStation('execution')).toBe('execution')
  })

  it('keeps the previous station for every other phase', () => {
    // `done` completes the run; a stored mission run's own phases are off
    // the kernel line and must never bounce the track back.
    for (const phase of ['done', 'cancelled', 'discovery', 'planning', 'evidence', 'synthesis', 'critic']) {
      expect(agentPhaseStation(phase), phase).toBeNull()
    }
  })
})
