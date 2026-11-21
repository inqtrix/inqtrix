import { describe, expect, it } from 'vitest'

import type { AgentTierCapability } from '@/features/researchRuns/types'

import { DEMO_AGENT_TIERS } from './demo'
import { effectiveAgentTier } from './runSetupLabels'

function published(...ids: AgentTierCapability['id'][]): AgentTierCapability[] {
  return ids.map((id) => ({
    clarification_rounds: 0,
    id,
    verify: 'standard',
  }))
}

const SERVER = published('schnell', 'gruendlich', 'tief')

describe('the Stufe the agent composer starts on', () => {
  it('takes a pick made on this page before everything else', () => {
    expect(effectiveAgentTier({
      defaultTier: 'gruendlich', last: 'schnell', selected: 'tief', tiers: SERVER,
    })).toBe('tief')
  })

  it('starts on the last pick when the server still publishes it', () => {
    expect(effectiveAgentTier({
      defaultTier: 'gruendlich', last: 'schnell', selected: null, tiers: SERVER,
    })).toBe('schnell')
  })

  it('starts on the published default when the last pick is no longer offered', () => {
    expect(effectiveAgentTier({
      defaultTier: 'tief', last: 'schnell', selected: null, tiers: published('gruendlich', 'tief'),
    })).toBe('tief')
  })

  it('reads a value outside the ladder as no pick', () => {
    expect(effectiveAgentTier({
      defaultTier: 'gruendlich', last: 'standard', selected: null, tiers: SERVER,
    })).toBe('gruendlich')
  })

  it('starts on the published default without a pick', () => {
    expect(effectiveAgentTier({
      defaultTier: 'tief', last: '', selected: null, tiers: SERVER,
    })).toBe('tief')
  })

  it('starts on Gründlich when the server names no default', () => {
    expect(effectiveAgentTier({
      defaultTier: undefined, last: '', selected: null, tiers: SERVER,
    })).toBe('gruendlich')
  })

  it('starts on the last pick in the demo, which publishes its own ladder', () => {
    expect(effectiveAgentTier({
      defaultTier: undefined, last: 'tief', selected: null, tiers: DEMO_AGENT_TIERS,
    })).toBe('tief')
  })

  it('offers no Stufe where the server publishes no ladder', () => {
    expect(effectiveAgentTier({
      defaultTier: 'gruendlich', last: 'schnell', selected: 'tief', tiers: null,
    })).toBeNull()
  })
})
