import type {
  CollaborationApi,
  CollaborationSettings,
  InstanceFence,
  IntervalScheduler,
  SidecarLogger,
} from './contracts'
import { ApiRequestError, CloseCodes, CollaborationError, isInstanceFenced } from './errors'
import { SidecarMetrics } from './metrics'

const scheduler: IntervalScheduler = {
  clear: (handle) => clearInterval(handle),
  every: (callback, intervalMs) => setInterval(callback, intervalMs),
}

/** Delay before a failed renewal is tried again. */
const RENEW_RETRY_MS = 1_000

/** How long before the stored lease expires the instance gives it up, so
 * that no room keeps accepting edits the store would refuse. */
const LEASE_EXPIRY_MARGIN_MS = 1_000

/** A renewal answer that does not continue this instance's current lease. */
class InstanceLeaseMismatch extends Error {}

/** The internal API as the rooms use it: an answer that the store fenced
 * this instance ends the lease the request was made under at once, instead
 * of when the next renewal finds out. */
export function reportingFencedAnswers(
  api: CollaborationApi,
  onFenced: (fence: InstanceFence) => void,
): CollaborationApi {
  const observe = <T>(fence: InstanceFence, request: Promise<T>): Promise<T> => request.catch(
    (error: unknown) => {
      if (isInstanceFenced(error)) onFenced(fence)
      throw error
    },
  )
  return {
    acquireInstance: (input) => api.acquireInstance(input),
    compactMaintenance: (input) => observe(input.fence, api.compactMaintenance(input)),
    introspectLease: (input) => observe(input.fence, api.introspectLease(input)),
    loadDocumentState: (input) => observe(input.fence, api.loadDocumentState(input)),
    lookupCommand: (input) => observe(input.fence, api.lookupCommand(input)),
    lookupUpdates: (input) => observe(input.fence, api.lookupUpdates(input)),
    persistUpdate: (input) => observe(input.fence, api.persistUpdate(input)),
    pollPolicyEvents: (input) => observe(input.fence, api.pollPolicyEvents(input)),
    renewInstance: (input) => observe(input.fence, api.renewInstance(input)),
    storeSnapshot: (input) => observe(input.fence, api.storeSnapshot(input)),
  }
}

/** Holds the single-writer fencing lease. A failed renewal does not end it:
 * the lease ends when it is about to expire, or at once when the store
 * reports the instance fenced. Whatever the instance believes, the store
 * checks the lease in every write transaction. */
export class InstanceLeaseManager {
  private expiryTimer: ReturnType<typeof setTimeout> | null = null
  private fence: InstanceFence | null = null
  private inFlight: Promise<void> | null = null
  private interval: ReturnType<IntervalScheduler['every']> | null = null
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private running = false

  constructor(
    private readonly api: CollaborationApi,
    private readonly settings: CollaborationSettings,
    private readonly logger: SidecarLogger,
    private readonly metrics: SidecarMetrics,
    private readonly onLeaseLost: () => void,
    private readonly intervals: IntervalScheduler = scheduler,
    private readonly now: () => number = () => Date.now(),
  ) {}

  async start(): Promise<void> {
    if (this.running) return
    this.running = true
    await this.renewNow()
    if (!this.running) return
    this.interval = this.intervals.every(() => {
      void this.renewNow()
    }, this.settings.instanceRenewSeconds * 1_000)
  }

  async stop(): Promise<void> {
    this.running = false
    if (this.interval) this.intervals.clear(this.interval)
    this.interval = null
    this.clearTimers()
    const inFlight = this.inFlight
    this.fence = null
    this.metrics.set('inqtrix_collaboration_instance_ready', 0)
    await inFlight
  }

  async renewNow(): Promise<void> {
    if (!this.running) return
    if (this.inFlight) return this.inFlight
    this.inFlight = this.tick().finally(() => {
      this.inFlight = null
    })
    return this.inFlight
  }

  isReady(): boolean {
    return this.fence !== null && this.fence.leaseExpiresAt > this.now() / 1_000
  }

  assertActive(): InstanceFence {
    if (!this.isReady() || !this.fence) {
      throw new CollaborationError('instance_lease_lost', {
        closeCode: CloseCodes.serviceUnavailable,
        httpStatus: 503,
      })
    }
    return this.fence
  }

  /** Ends the lease the given fence belongs to, because the store refused a
   * request made under it and refuses every later one too. A fence of an
   * earlier lease changes nothing. */
  loseLease(fence: InstanceFence): void {
    const current = this.fence
    if (
      !this.running
      || current === null
      || current.instanceId !== fence.instanceId
      || current.epoch !== fence.epoch
    ) return
    this.clearTimers()
    this.fence = null
    this.metrics.set('inqtrix_collaboration_instance_ready', 0)
    this.logger.warn('instance_lease_unavailable', {
      instance_id: this.settings.instanceId,
    })
    this.onLeaseLost()
  }

  private async tick(): Promise<void> {
    const held = this.fence
    try {
      const next = held
        ? await this.api.renewInstance({
            fence: held,
            leaseSeconds: this.settings.instanceLeaseSeconds,
          })
        : await this.api.acquireInstance({
            instanceId: this.settings.instanceId,
            leaseSeconds: this.settings.instanceLeaseSeconds,
            protocolVersion: this.settings.protocolVersion,
            schemaVersion: this.settings.schemaVersion,
          })
      if (!this.running) return
      // A renewal answered after its lease ended does not revive it; the
      // next tick acquires a lease of its own.
      if (this.fence !== held) return
      if (next.instanceId !== this.settings.instanceId) {
        throw new InstanceLeaseMismatch('Instance lease response referenced another instance')
      }
      if (held && next.epoch !== held.epoch) {
        throw new InstanceLeaseMismatch('Instance renewal changed the fencing epoch')
      }
      this.install(next)
      if (held === null) {
        this.logger.info('instance_lease_acquired', {
          epoch: next.epoch,
          instance_id: next.instanceId,
        })
      }
    } catch (error) {
      if (!this.running) return
      this.metrics.increment('inqtrix_collaboration_instance_renew_failures_total')
      if (held === null) {
        this.metrics.set('inqtrix_collaboration_instance_ready', 0)
        this.logger.warn('instance_lease_unavailable', {
          instance_id: this.settings.instanceId,
        })
        return
      }
      if (error instanceof InstanceLeaseMismatch || isInstanceFenced(error)) {
        this.loseLease(held)
        return
      }
      if (this.fence === held) this.retryRenewal(held, error)
    }
  }

  private install(next: InstanceFence): void {
    this.clearTimers()
    this.fence = next
    // Set for every lease, so a renewal that hangs past the expiry cannot
    // keep the rooms open longer than the store honours the lease.
    this.expiryTimer = setTimeout(() => {
      this.expiryTimer = null
      if (this.fence === next) this.loseLease(next)
    }, Math.max(0, next.leaseExpiresAt * 1_000 - LEASE_EXPIRY_MARGIN_MS - this.now()))
    this.expiryTimer.unref()
    this.metrics.set('inqtrix_collaboration_instance_ready', 1)
    this.metrics.set('inqtrix_collaboration_instance_epoch', next.epoch)
  }

  /** A renewal failed without the store refusing the lease (timeout,
   * transport, 5xx or another answer): keep the lease and try again soon. */
  private retryRenewal(held: InstanceFence, error: unknown): void {
    const expiresInMs = held.leaseExpiresAt * 1_000 - this.now()
    const retryMs = Math.max(0, Math.min(RENEW_RETRY_MS, expiresInMs - LEASE_EXPIRY_MARGIN_MS))
    this.logger.warn('instance_lease_renew_failed', {
      lease_expires_in_ms: Math.round(expiresInMs),
      reason: error instanceof ApiRequestError ? error.reason : 'unknown',
      retry_ms: retryMs,
    })
    if (this.retryTimer) clearTimeout(this.retryTimer)
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null
      void this.renewNow()
    }, retryMs)
    this.retryTimer.unref()
  }

  private clearTimers(): void {
    if (this.expiryTimer) clearTimeout(this.expiryTimer)
    if (this.retryTimer) clearTimeout(this.retryTimer)
    this.expiryTimer = null
    this.retryTimer = null
  }
}
