import { describe, expect, it } from 'vitest'

import {
  distanceFromBottom,
  isNearBottom,
  scrollFollowModeForUpdate,
} from './scrollMetrics'

describe('scroll metrics', () => {
  it('handles fractional bottom distances with the near-bottom threshold', () => {
    const metrics = { clientHeight: 808, scrollHeight: 3640, scrollTop: 2808.5 }

    expect(distanceFromBottom(metrics)).toBe(23.5)
    expect(isNearBottom(metrics)).toBe(true)
    expect(isNearBottom(metrics, 1)).toBe(false)
  })

  it('forces an instant restore for a conversation switch', () => {
    expect(scrollFollowModeForUpdate({
      hasActiveContent: false,
      keyChanged: true,
      nearBottom: false,
      reduceMotion: false,
    })).toBe('auto')
  })

  it('only smooth-follows same-conversation updates when the user is near the bottom', () => {
    expect(scrollFollowModeForUpdate({
      hasActiveContent: false,
      keyChanged: false,
      nearBottom: true,
      reduceMotion: false,
    })).toBe('smooth')
    expect(scrollFollowModeForUpdate({
      hasActiveContent: false,
      keyChanged: false,
      nearBottom: false,
      reduceMotion: false,
    })).toBe('none')
    expect(scrollFollowModeForUpdate({
      hasActiveContent: true,
      keyChanged: false,
      nearBottom: true,
      reduceMotion: false,
    })).toBe('auto')
  })
})
