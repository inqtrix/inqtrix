import { afterEach, describe, expect, it, vi } from 'vitest'
import { retryServerDiscovery } from './useServerDiscovery'

afterEach(() => { vi.useRealTimers() })

describe('server discovery recovery', () => {
  it('retries a failed discovery and stops requesting after recovery', async () => {
    vi.useFakeTimers()
    const discover = vi.fn().mockResolvedValueOnce(false).mockResolvedValue(true)
    const stop = retryServerDiscovery(discover)
    await vi.advanceTimersByTimeAsync(4999)
    expect(discover).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(discover).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(60000)
    expect(discover).toHaveBeenCalledTimes(2)
    stop()
  })

  it('never overlaps a slow attempt and does not restart after teardown', async () => {
    vi.useFakeTimers()
    let resolve!: (success: boolean) => void
    const discover = vi.fn(() => new Promise<boolean>((done) => { resolve = done }))
    const stop = retryServerDiscovery(discover)
    await vi.advanceTimersByTimeAsync(30000)
    expect(discover).toHaveBeenCalledTimes(1)
    stop()
    resolve(false)
    await vi.advanceTimersByTimeAsync(30000)
    expect(discover).toHaveBeenCalledTimes(1)
  })
})
