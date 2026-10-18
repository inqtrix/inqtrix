import { describe, expect, it } from 'vitest'

import { appMotion } from './transitions'

describe('appMotion vocabulary', () => {
  it('uses the fastest shared curve for a settled-region reveal', () => {
    expect(appMotion.reveal.duration).toBe(0.15)
    expect(appMotion.reveal.duration).toBeLessThan(appMotion.panel.duration)
    expect(appMotion.reveal.ease).toEqual(appMotion.panel.ease)
  })
})
