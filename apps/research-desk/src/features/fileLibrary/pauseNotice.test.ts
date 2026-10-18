import { describe, expect, it } from 'vitest'

import type { AutoResumePlan } from './indexingTypes'
import {
  allowsRawRebuild,
  autoResumeNotice,
  pauseCause,
  soonestPlan,
} from './pauseNotice'

const NOW_MS = 1_791_740_000_000

function plan(overrides: Partial<AutoResumePlan> = {}): AutoResumePlan {
  return { attempts: 0, max: 5, next_at: NOW_MS / 1000 + 60, stopped: null, ...overrides }
}

describe('pauseCause', () => {
  it.each([
    ['contextualization_provider_timeout', 'contextualization'],
    ['contextualization_provider_rate_limited', 'contextualization'],
    ['contextualization_provider_unavailable', 'contextualization'],
    ['contextualization_provider_circuit_open', 'circuitOpen'],
    ['embedding_provider_timeout', 'embedding'],
    ['vector_store_unavailable', 'vectorStore'],
    ['quota_dependency_error', 'quota'],
    ['contextualization_circuit_state_unavailable', 'unknown'],
    ['dependency_error', 'unknown'],
    [null, 'unknown'],
  ])('names %s as %s', (errorType, cause) => {
    expect(pauseCause(errorType)).toBe(cause)
  })
})

describe('allowsRawRebuild', () => {
  it('offers a build without context only where context is what failed', () => {
    expect(allowsRawRebuild('paused_validation', null)).toBe(true)
    expect(allowsRawRebuild('paused_dependency', 'contextualization_provider_timeout')).toBe(true)
    expect(allowsRawRebuild('paused_dependency', 'contextualization_circuit_state_unavailable')).toBe(true)
    expect(allowsRawRebuild('paused_dependency', 'vector_store_unavailable')).toBe(false)
    expect(allowsRawRebuild('paused_dependency', 'embedding_provider_rate_limited')).toBe(false)
    expect(allowsRawRebuild('paused_dependency', 'quota_dependency_error')).toBe(false)
    expect(allowsRawRebuild('paused_dependency', null)).toBe(false)
  })
})

describe('autoResumeNotice', () => {
  it('names the next attempt and its count while the plan waits', () => {
    expect(autoResumeNotice('paused_dependency', plan({ attempts: 1 }), NOW_MS)).toEqual({
      at: new Date(NOW_MS + 60_000),
      attempt: 2,
      kind: 'planned',
      max: 5,
    })
  })

  it('never shows a time that has already passed', () => {
    expect(
      autoResumeNotice('paused_dependency', plan({ next_at: NOW_MS / 1000 - 1 }), NOW_MS),
    ).toEqual({ attempt: 1, kind: 'soon', max: 5 })
  })

  it('names why a streak ended', () => {
    expect(
      autoResumeNotice(
        'paused_dependency',
        plan({ attempts: 5, next_at: null, stopped: 'exhausted' }),
        NOW_MS,
      ),
    ).toEqual({ attempts: 5, kind: 'stopped', max: 5, reason: 'exhausted' })
  })

  it('shows nothing outside a dependency pause or without a plan', () => {
    expect(autoResumeNotice('paused_validation', plan(), NOW_MS)).toBeNull()
    expect(autoResumeNotice('completed', plan({ attempts: 2, next_at: null }), NOW_MS)).toBeNull()
    expect(autoResumeNotice('paused_dependency', null, NOW_MS)).toBeNull()
    // Resumed and waiting to run: no due time, no stop.
    expect(autoResumeNotice('paused_dependency', plan({ next_at: null }), NOW_MS)).toBeNull()
  })
})

describe('soonestPlan', () => {
  it('names the job a paused run hears from first', () => {
    const later = plan({ next_at: NOW_MS / 1000 + 240 })
    const sooner = plan({ attempts: 2, next_at: NOW_MS / 1000 + 30 })
    expect(soonestPlan([later, null, sooner])).toBe(sooner)
  })

  it('falls back to a stopped streak when nothing is planned', () => {
    const stopped = plan({ attempts: 5, next_at: null, stopped: 'exhausted' })
    expect(soonestPlan([undefined, stopped])).toBe(stopped)
    expect(soonestPlan([undefined, null])).toBeNull()
  })
})
