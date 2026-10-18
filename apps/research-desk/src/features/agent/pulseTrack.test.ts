import { describe, expect, it } from 'vitest'

import {
  AGENT_KERNEL_STATIONS,
  AGENT_PHASE_STATIONS,
  agentPulseActiveIndex,
  agentStationsFor,
} from './model'

/** The stations the track paints as done, given the rule under test. */
function doneStations(
  station: Parameters<typeof agentPulseActiveIndex>[0],
  completed: boolean,
  stations: readonly string[] = AGENT_PHASE_STATIONS,
) {
  const activeIndex = agentPulseActiveIndex(station, completed, stations)
  return stations.filter((_s, index) => index < activeIndex)
}

describe('how far the pulse track is filled', () => {
  it('draws the kernel its own line, not the mission’s', () => {
    // The kernel engine reports exactly two phases, `execution` and
    // `done`. It has no discovery, no planning, no synthesis and no
    // critic, so drawing the mission's six stations for it would paint a
    // flow that never happens.
    expect(agentStationsFor('agent_kernel')).toEqual([
      'intake',
      'execution',
      'result',
    ])
    expect(agentStationsFor('workspace_agent')).toEqual([
      ...AGENT_PHASE_STATIONS,
    ])
    const done = doneStations('execution', true, AGENT_KERNEL_STATIONS)
    expect(done).toEqual(['intake', 'execution', 'result'])
    expect(done).not.toContain('discovery')
    expect(done).not.toContain('planning')
    expect(done).not.toContain('critic')
  })

  it('keeps the mission line when the engine is unknown', () => {
    // An older run without execution metadata must not lose stations it
    // may well have visited.
    expect(agentStationsFor(undefined)).toEqual([...AGENT_PHASE_STATIONS])
  })

  it('leaves the kernel result open while the run is still working', () => {
    expect(doneStations('execution', false, AGENT_KERNEL_STATIONS)).toEqual([
      'intake',
    ])
  })

  it('fills the whole line for a mission that really reached the end', () => {
    expect(doneStations('critic', true)).toEqual([...AGENT_PHASE_STATIONS])
  })

  it('keeps the reached station ACTIVE while the run is alive', () => {
    // Running: the station it is on must not already read as done.
    expect(agentPulseActiveIndex('planning', false)).toBe(2)
    expect(doneStations('planning', false)).toEqual(['intake', 'discovery'])
  })

  it('marks the reached station done once the run completes', () => {
    expect(doneStations('planning', true)).toEqual([
      'intake',
      'discovery',
      'planning',
    ])
  })

  it('never falls below the first station', () => {
    // An unknown station must not produce a negative index and paint the
    // line backwards.
    const unknown = 'nonsense' as Parameters<typeof agentPulseActiveIndex>[0]
    expect(agentPulseActiveIndex(unknown, false)).toBe(0)
    expect(agentPulseActiveIndex(unknown, true)).toBe(1)
  })
})
