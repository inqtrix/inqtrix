import { SnapshotRetryController } from '../src/snapshotRetry'

describe('snapshot retry controller', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('retries with capped exponential backoff until persistence succeeds', async () => {
    vi.useFakeTimers()
    let failuresRemaining = 3
    const task = vi.fn(async () => {
      if (failuresRemaining > 0) {
        failuresRemaining -= 1
        throw new Error('snapshot unavailable')
      }
    })
    const onFailure = vi.fn()
    const onSuccess = vi.fn()
    const retries = new SnapshotRetryController(100, 250, {
      isEligible: () => true,
      onFailure,
      onSuccess,
    })

    retries.schedule('room-1', task)
    await vi.advanceTimersByTimeAsync(99)
    expect(task).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(1)
    expect(task).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(200)
    expect(task).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(250)
    expect(task).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(250)

    expect(task).toHaveBeenCalledTimes(4)
    expect(onFailure).toHaveBeenCalledTimes(3)
    expect(onSuccess).toHaveBeenCalledWith('room-1')
    expect(retries.isPending('room-1')).toBe(false)
  })

  it('cancels pending work and stops when a room is no longer eligible', async () => {
    vi.useFakeTimers()
    let eligible = true
    const task = vi.fn(async () => undefined)
    const retries = new SnapshotRetryController(100, 250, {
      isEligible: () => eligible,
      onFailure: () => undefined,
      onSuccess: () => undefined,
    })

    retries.schedule('room-1', task)
    retries.cancel('room-1')
    await vi.advanceTimersByTimeAsync(100)
    expect(task).not.toHaveBeenCalled()

    retries.schedule('room-1', task)
    eligible = false
    await vi.advanceTimersByTimeAsync(100)
    expect(task).not.toHaveBeenCalled()
    expect(retries.isPending('room-1')).toBe(false)
  })

  it('keeps one retry slot while an attempt is in flight', async () => {
    vi.useFakeTimers()
    let resolveAttempt = (): void => undefined
    const first = vi.fn(() => new Promise<void>((resolve) => {
      resolveAttempt = resolve
    }))
    const replacement = vi.fn(async () => undefined)
    const retries = new SnapshotRetryController(100, 250, {
      isEligible: () => true,
      onFailure: () => undefined,
      onSuccess: () => undefined,
    })

    retries.schedule('room-1', first)
    await vi.advanceTimersByTimeAsync(100)
    expect(first).toHaveBeenCalledTimes(1)
    expect(retries.isPending('room-1')).toBe(true)

    retries.schedule('room-1', replacement)
    await vi.advanceTimersByTimeAsync(1_000)
    expect(replacement).not.toHaveBeenCalled()
    resolveAttempt()
    await vi.advanceTimersByTimeAsync(0)
    expect(retries.isPending('room-1')).toBe(false)
  })
})
