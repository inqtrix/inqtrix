import { describe, expect, it } from 'vitest'

import {
  agentStepScrollDecision,
  agentStepScrollTop,
  isAgentStepScrollKey,
} from './agentStepScroll'

const geometry = {
  containerHeight: 300,
  stepHeight: 48,
  stepTop: 720,
}

describe('agent step scrolling', () => {
  it('aligns the current step to the lower viewport edge and clamps short logs to zero', () => {
    expect(agentStepScrollTop(geometry)).toBe(486)
    expect(agentStepScrollTop({
      containerHeight: 300,
      stepHeight: 40,
      stepTop: 12,
    })).toBe(0)
  })

  it('initializes every newly selected run synchronously even after user navigation', () => {
    expect(agentStepScrollDecision({
      autoFollow: false,
      geometry,
      positionedRunId: 'run-a',
      reducedMotion: false,
      runId: 'run-b',
    })).toEqual({
      behavior: 'auto',
      initializesRun: true,
      top: 486,
    })
  })

  it('smoothly follows later events only while auto-follow remains active', () => {
    expect(agentStepScrollDecision({
      autoFollow: true,
      geometry,
      positionedRunId: 'run-a',
      reducedMotion: false,
      runId: 'run-a',
    })).toEqual({
      behavior: 'smooth',
      initializesRun: false,
      top: 486,
    })
    expect(agentStepScrollDecision({
      autoFollow: false,
      geometry,
      positionedRunId: 'run-a',
      reducedMotion: false,
      runId: 'run-a',
    })).toBeNull()
  })

  it('disables smooth auto-follow when reduced motion is requested', () => {
    expect(agentStepScrollDecision({
      autoFollow: true,
      geometry,
      positionedRunId: 'run-a',
      reducedMotion: true,
      runId: 'run-a',
    })?.behavior).toBe('auto')
  })

  it('recognizes keyboard scrolling without treating unrelated keys as navigation', () => {
    expect([' ', 'ArrowDown', 'ArrowUp', 'End', 'Home', 'PageDown', 'PageUp'].every(
      isAgentStepScrollKey,
    )).toBe(true)
    expect(isAgentStepScrollKey('Tab')).toBe(false)
    expect(isAgentStepScrollKey('Enter')).toBe(false)
  })
})
