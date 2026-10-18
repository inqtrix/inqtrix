import { describe, expect, it } from 'vitest'

import { SidecarMetrics } from '../src/metrics'

function bucketFor(rendered: string, name: string, le: string): number {
  const line = rendered
    .split('\n')
    .find((entry) => entry.startsWith(`${name}_bucket{le="${le}"}`))
  if (!line) throw new Error(`missing bucket ${le} for ${name}`)
  return Number(line.split(' ').at(-1))
}

describe('SidecarMetrics histogram buckets', () => {
  it('resolves the range the large-state latency actually lands in', () => {
    // Under the accepted large-state latency risk the latency sits between
    // roughly 490ms and 750ms. With buckets jumping 500 -> 1000 every one
    // of those samples falls into the same bucket, so a regression from
    // 500ms to 950ms is invisible — the histogram cannot answer the
    // question it exists for.
    const metrics = new SidecarMetrics()

    metrics.observeMilliseconds('inqtrix_collaboration_probe_seconds', 600)
    metrics.observeMilliseconds('inqtrix_collaboration_probe_seconds', 900)

    const rendered = metrics.render()
    // Cumulative: each bucket counts every sample at or below its bound.
    expect(bucketFor(rendered, 'inqtrix_collaboration_probe_seconds', '0.5')).toBe(0)
    // 600ms separates here — the bucket that did not exist before.
    expect(bucketFor(rendered, 'inqtrix_collaboration_probe_seconds', '0.75')).toBe(1)
    expect(bucketFor(rendered, 'inqtrix_collaboration_probe_seconds', '1')).toBe(2)
    expect(bucketFor(rendered, 'inqtrix_collaboration_probe_seconds', '1.5')).toBe(2)
  })

  it('keeps buckets monotonically non-decreasing', () => {
    // A cumulative histogram is only readable if every wider bucket
    // contains at least what the narrower one did.
    const metrics = new SidecarMetrics()
    for (const value of [3, 30, 300, 600, 900, 3_000]) {
      metrics.observeMilliseconds('inqtrix_collaboration_probe_seconds', value)
    }

    const rendered = metrics.render()
    const counts = rendered
      .split('\n')
      .filter((line) => line.startsWith('inqtrix_collaboration_probe_seconds_bucket'))
      .map((line) => Number(line.split(' ').at(-1)))

    expect(counts.length).toBeGreaterThan(0)
    for (let index = 1; index < counts.length; index += 1) {
      expect(counts[index]).toBeGreaterThanOrEqual(counts[index - 1] as number)
    }
    expect(counts.at(-1)).toBe(6)
  })
})
