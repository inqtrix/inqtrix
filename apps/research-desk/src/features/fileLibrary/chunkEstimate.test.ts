import { describe, expect, it } from 'vitest'

import type { FileAssetRecord } from '@/features/project/types'
import { chunkEstimate, indexVectorCount } from './helpers'

function asset(sizeBytes: number, pageCount: number | null = null): FileAssetRecord {
  return { pageCount, sizeBytes } as FileAssetRecord
}

describe('chunkEstimate', () => {
  it('counts one vector for a small file, not a floor of 24', () => {
    // A file of 463 bytes was shown as "24 vectors (estimated)".
    expect(chunkEstimate(asset(463))).toBe(1)
    expect(chunkEstimate(asset(0))).toBe(1)
  })

  it('counts by the page for a short document, not a floor of 40', () => {
    expect(chunkEstimate(asset(128, 1))).toBe(4)
    expect(chunkEstimate(asset(128, 0))).toBe(1)
  })

  it('still scales with size and pages', () => {
    expect(chunkEstimate(asset(26_000))).toBe(10)
    expect(chunkEstimate(asset(128, 100))).toBe(430)
  })

  it('adds up the embedded members of an index only', () => {
    expect(indexVectorCount([
      { asset: asset(463), member: { state: 'embedded' } },
      { asset: asset(26_000), member: { state: 'embedded' } },
      { asset: asset(26_000), member: { state: 'pending' } },
    ] as Parameters<typeof indexVectorCount>[0])).toBe(11)
  })
})
