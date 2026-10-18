import type { IntervalScheduler, TimerHandle } from '../src/contracts'
import { ApiRequestError } from '../src/errors'
import { InstanceLeaseManager, reportingFencedAnswers } from '../src/instanceLease'
import { SidecarMetrics } from '../src/metrics'
import { FakeCollaborationApi, deferred, settings, silentLogger } from './helpers'

describe('single-replica instance fencing', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('becomes unready and invokes the close callback when the store fences the instance', async () => {
    const api = new FakeCollaborationApi()
    const lost = vi.fn()
    let scheduled: (() => void) | null = null
    const handle = {} as TimerHandle
    const intervals: IntervalScheduler = {
      clear: vi.fn(),
      every: (callback) => {
        scheduled = callback
        return handle
      },
    }
    const manager = new InstanceLeaseManager(
      api,
      settings(),
      silentLogger,
      new SidecarMetrics(),
      lost,
      intervals,
    )

    await manager.start()
    expect(manager.isReady()).toBe(true)
    expect(scheduled).not.toBeNull()
    api.renewError = new ApiRequestError(409, 'instance_fenced')
    await manager.renewNow()

    expect(manager.isReady()).toBe(false)
    expect(lost).toHaveBeenCalledOnce()
    expect(() => manager.assertActive()).toThrowError('instance_lease_lost')
    await manager.stop()
  })

  it('invalidates the local fence before an in-flight renewal can finish shutdown', async () => {
    const api = new FakeCollaborationApi()
    const renewal = deferred<typeof api.fence>()
    const manager = new InstanceLeaseManager(
      api,
      settings(),
      silentLogger,
      new SidecarMetrics(),
      () => undefined,
    )
    await manager.start()
    api.renewInstance = async () => renewal.promise
    const renewing = manager.renewNow()
    const stopping = manager.stop()

    expect(manager.isReady()).toBe(false)
    expect(() => manager.assertActive()).toThrowError('instance_lease_lost')
    renewal.resolve(api.fence)
    await Promise.all([renewing, stopping])
    expect(manager.isReady()).toBe(false)
  })
  it('keeps the lease through a failed renewal and renews again a second later', async () => {
    vi.useFakeTimers()
    const api = new FakeCollaborationApi()
    api.fence = { epoch: 1, instanceId: 'test-instance', leaseExpiresAt: Date.now() / 1_000 + 15 }
    const lost = vi.fn()
    const renewals = vi.fn()
    const manager = new InstanceLeaseManager(
      api,
      settings(),
      silentLogger,
      new SidecarMetrics(),
      lost,
    )
    await manager.start()
    const renew = api.renewInstance.bind(api)
    api.renewInstance = async () => {
      renewals()
      return renew()
    }

    api.renewError = new ApiRequestError(503, 'internal_api_timeout')
    await manager.renewNow()
    expect(lost).not.toHaveBeenCalled()
    expect(manager.isReady()).toBe(true)
    expect(() => manager.assertActive()).not.toThrow()

    api.renewError = null
    await vi.advanceTimersByTimeAsync(999)
    expect(renewals).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(renewals).toHaveBeenCalledTimes(2)
    expect(manager.isReady()).toBe(true)
    expect(lost).not.toHaveBeenCalled()
    await manager.stop()
  })

  it('ends a lease whose renewals keep failing one second before it expires', async () => {
    vi.useFakeTimers()
    const api = new FakeCollaborationApi()
    const expiresAt = Date.now() + 15_000
    api.fence = { epoch: 1, instanceId: 'test-instance', leaseExpiresAt: expiresAt / 1_000 }
    const lost = vi.fn()
    const manager = new InstanceLeaseManager(
      api,
      settings(),
      silentLogger,
      new SidecarMetrics(),
      lost,
    )
    await manager.start()
    api.renewError = new ApiRequestError(503, 'internal_api_unreachable')
    api.acquireInstance = async () => {
      throw new ApiRequestError(503, 'internal_api_unreachable')
    }
    await manager.renewNow()

    await vi.advanceTimersByTimeAsync(expiresAt - 1_100 - Date.now())
    expect(lost).not.toHaveBeenCalled()
    expect(manager.isReady()).toBe(true)
    await vi.advanceTimersByTimeAsync(200)
    expect(lost).toHaveBeenCalledOnce()
    expect(manager.isReady()).toBe(false)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(lost).toHaveBeenCalledOnce()
    await manager.stop()
  })

  it('ends the lease at once when a renewal answers with another fencing epoch', async () => {
    const api = new FakeCollaborationApi()
    const lost = vi.fn()
    const manager = new InstanceLeaseManager(
      api,
      settings(),
      silentLogger,
      new SidecarMetrics(),
      lost,
    )
    await manager.start()
    api.renewInstance = async () => ({ ...api.fence, epoch: api.fence.epoch + 1 })
    await manager.renewNow()

    expect(manager.isReady()).toBe(false)
    expect(lost).toHaveBeenCalledOnce()
    await manager.stop()
  })

  it('ends the lease before it expires while a renewal hangs and ignores the late answer', async () => {
    vi.useFakeTimers()
    const api = new FakeCollaborationApi()
    const expiresAt = Date.now() + 15_000
    api.fence = { epoch: 1, instanceId: 'test-instance', leaseExpiresAt: expiresAt / 1_000 }
    const lost = vi.fn()
    const manager = new InstanceLeaseManager(
      api,
      settings(),
      silentLogger,
      new SidecarMetrics(),
      lost,
    )
    await manager.start()
    const renewal = deferred<typeof api.fence>()
    api.renewInstance = async () => renewal.promise
    const renewing = manager.renewNow()

    await vi.advanceTimersByTimeAsync(expiresAt - 900 - Date.now())
    expect(lost).toHaveBeenCalledOnce()
    expect(manager.isReady()).toBe(false)

    renewal.resolve({ ...api.fence, leaseExpiresAt: Date.now() / 1_000 + 15 })
    await renewing
    expect(manager.isReady()).toBe(false)
    expect(() => manager.assertActive()).toThrowError('instance_lease_lost')
    expect(lost).toHaveBeenCalledOnce()
    await manager.stop()
  })

  it('leaves the current lease alone when a request made under an earlier lease is fenced', async () => {
    const api = new FakeCollaborationApi()
    api.fence = { epoch: 1, instanceId: 'test-instance', leaseExpiresAt: Date.now() / 1_000 + 15 }
    const lost = vi.fn()
    const manager = new InstanceLeaseManager(
      api,
      settings(),
      silentLogger,
      new SidecarMetrics(),
      lost,
    )
    const rooms = reportingFencedAnswers(api, (fence) => manager.loseLease(fence))
    await manager.start()
    const earlier = manager.assertActive()
    api.renewError = new ApiRequestError(409, 'instance_fenced')
    await manager.renewNow()
    expect(lost).toHaveBeenCalledOnce()

    api.renewError = null
    api.fence = { epoch: 2, instanceId: 'test-instance', leaseExpiresAt: Date.now() / 1_000 + 15 }
    await manager.renewNow()
    expect(manager.assertActive().epoch).toBe(2)

    // A request still in flight from the earlier lease is answered fenced
    // only after the new lease is held.
    api.introspectLease = async () => {
      throw new ApiRequestError(409, 'instance_fenced')
    }
    await expect(
      rooms.introspectLease({ fence: earlier, room: 'room', token: 'token' }),
    ).rejects.toThrowError()

    expect(manager.isReady()).toBe(true)
    expect(manager.assertActive().epoch).toBe(2)
    expect(lost).toHaveBeenCalledOnce()
    await manager.stop()
  })
})
