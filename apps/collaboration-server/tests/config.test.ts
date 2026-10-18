import { settingsFromEnv } from '../src/config'

const base = {
  INQTRIX_API_INTERNAL_URL: 'http://api.internal:5100',
  INQTRIX_COLLABORATION_SECRET: '0123456789abcdef0123456789abcdef',
}

describe('settings environment bridge', () => {
  it('uses the shared Python/deployment collaboration vocabulary', () => {
    const configured = settingsFromEnv({
      ...base,
      INQTRIX_COLLABORATION_AWARENESS_RATE_PER_SECOND: '17',
      INQTRIX_COLLABORATION_MAX_DOCUMENT_BYTES: '2097152',
      INQTRIX_COLLABORATION_MAX_FRAME_BYTES: '131072',
      INQTRIX_COLLABORATION_MAX_SESSIONS_PER_USER_DOCUMENT: '4',
      INQTRIX_COLLABORATION_MAINTENANCE_INTERVAL_SECONDS: '45',
      INQTRIX_COLLABORATION_MAX_QUEUED_BYTES: '262144',
      INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES: '12',
      INQTRIX_COLLABORATION_SNAPSHOT_IDLE_SECONDS: '7',
      INQTRIX_COLLABORATION_SNAPSHOT_RETRY_BASE_MS: '250',
      INQTRIX_COLLABORATION_SNAPSHOT_RETRY_MAX_MS: '4000',
      INQTRIX_COLLABORATION_SNAPSHOT_TAIL_BYTES: '131072',
      INQTRIX_COLLABORATION_SNAPSHOT_UPDATE_COUNT: '90',
      INQTRIX_COLLABORATION_TENANT_ID: 'tenant-primary',
      INQTRIX_COLLABORATION_SOCKET_BACKPRESSURE_BYTES: '196608',
      INQTRIX_COLLABORATION_UPDATE_RATE_COUNT: '75',
      INQTRIX_COLLABORATION_UPDATE_RATE_WINDOW_SECONDS: '12',
    }, 'instance-1')

    expect(configured).toMatchObject({
      awarenessRateLimit: 17,
      awarenessRateWindowMs: 1_000,
      documentLimitBytes: 2_097_152,
      frameLimitBytes: 131_072,
      maintenanceIntervalMs: 45_000,
      maxQueuedBytes: 262_144,
      maxQueuedFrames: 12,
      maxSessionsPerUserDocument: 4,
      snapshotIdleMs: 7_000,
      snapshotMaxUpdates: 90,
      snapshotRetryBaseMs: 250,
      snapshotRetryMaxMs: 4_000,
      snapshotTailBytes: 131_072,
      socketBackpressureBytes: 196_608,
      tenantId: 'tenant-primary',
      updateRateLimit: 75,
      updateRateWindowMs: 12_000,
    })
  })

  it('rejects an inverted snapshot retry window', () => {
    expect(() => settingsFromEnv({
      ...base,
      INQTRIX_COLLABORATION_SNAPSHOT_RETRY_BASE_MS: '5000',
      INQTRIX_COLLABORATION_SNAPSHOT_RETRY_MAX_MS: '1000',
    }, 'instance-1')).toThrowError(
      'INQTRIX_COLLABORATION_SNAPSHOT_RETRY_MAX_MS must be greater than or equal to INQTRIX_COLLABORATION_SNAPSHOT_RETRY_BASE_MS',
    )
  })

  it.each([
    ['5', '5'],
    ['6', '5'],
  ])('requires instance renewal %s to be lower than lease %s', (renew, lease) => {
    expect(() => settingsFromEnv({
      ...base,
      INQTRIX_COLLABORATION_INSTANCE_LEASE_SECONDS: lease,
      INQTRIX_COLLABORATION_INSTANCE_RENEW_SECONDS: renew,
    }, 'instance-1')).toThrowError(
      'INQTRIX_COLLABORATION_INSTANCE_RENEW_SECONDS must be lower than INQTRIX_COLLABORATION_INSTANCE_LEASE_SECONDS',
    )
  })

  it('does not retain unreleased near-duplicate aliases', () => {
    const configured = settingsFromEnv({
      ...base,
      INQTRIX_COLLABORATION_AWARENESS_RATE_LIMIT: '1',
      INQTRIX_COLLABORATION_DOCUMENT_LIMIT_BYTES: '1048576',
      INQTRIX_COLLABORATION_FRAME_LIMIT_BYTES: '65536',
      INQTRIX_COLLABORATION_MAX_USER_DOCUMENT_SESSIONS: '1',
      INQTRIX_COLLABORATION_AWARENESS_RATE_WINDOW_MS: '1',
      INQTRIX_COLLABORATION_SNAPSHOT_IDLE_MS: '1000',
      INQTRIX_COLLABORATION_SNAPSHOT_MAX_UPDATES: '1',
      INQTRIX_COLLABORATION_UPDATE_RATE_LIMIT: '1',
      INQTRIX_COLLABORATION_UPDATE_RATE_WINDOW_MS: '1',
    }, 'instance-1')

    expect(configured).toMatchObject({
      awarenessRateLimit: 20,
      documentLimitBytes: 10 * 1024 * 1024,
      frameLimitBytes: 2 * 1024 * 1024,
      maxSessionsPerUserDocument: 5,
      snapshotIdleMs: 5_000,
      snapshotMaxUpdates: 256,
      updateRateLimit: 120,
      updateRateWindowMs: 10_000,
    })
  })
})
