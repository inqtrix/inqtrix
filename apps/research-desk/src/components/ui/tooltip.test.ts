import { describe, expect, it } from 'vitest'

import { TOOLTIP_APPEAR_DELAY_MS, TOOLTIP_WARMUP_SKIP_MS } from './tooltip'

describe('tooltip timing', () => {
  it('holds the deliberately calm dwell and never fires instantly', () => {
    // 1.5s is deliberately calmer than the common 500–1000ms desktop band.
    // The shared Tooltip wrapper owns this delay (provider runs at 0) and
    // the trigger cancels the pending reveal on leave/press/blur — Radix's
    // controlled mode swallows a close while the reveal is pending, which
    // would strand stacked orphan tooltips.
    expect(TOOLTIP_APPEAR_DELAY_MS).toBe(1_500)
    expect(TOOLTIP_APPEAR_DELAY_MS).toBeGreaterThanOrEqual(500)
  })

  it('applies the dwell to EVERY icon — no warm-up shortcut', () => {
    // Radix's provider warm state resets asynchronously on close, so fast
    // crossings between neighbors would reopen instantly regardless of this
    // value — which is why the wrapper enforces the dwell itself.
    expect(TOOLTIP_WARMUP_SKIP_MS).toBe(0)
  })
})
