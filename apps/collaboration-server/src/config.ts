import { Buffer } from 'node:buffer'

import {
  EDITOR_COLLABORATION_PROTOCOL_VERSION,
  EDITOR_SCHEMA_VERSION,
} from '@inqtrix/editor-schema'

import type { CollaborationSettings } from './contracts'

const MEBIBYTE = 1024 * 1024

export function settingsFromEnv(
  env: Readonly<Record<string, string | undefined>>,
  instanceId: string,
): CollaborationSettings {
  const secret = required(env, 'INQTRIX_COLLABORATION_SECRET')
  if (Buffer.byteLength(secret, 'utf8') < 32) {
    throw new Error('INQTRIX_COLLABORATION_SECRET must contain at least 32 UTF-8 bytes')
  }

  const apiBaseUrl = parseApiUrl(required(env, 'INQTRIX_API_INTERNAL_URL'))
  const snapshotRetryBaseMs = integer(
    env,
    'INQTRIX_COLLABORATION_SNAPSHOT_RETRY_BASE_MS',
    1_000,
    100,
    60_000,
  )
  const snapshotRetryMaxMs = integer(
    env,
    'INQTRIX_COLLABORATION_SNAPSHOT_RETRY_MAX_MS',
    30_000,
    100,
    300_000,
  )
  if (snapshotRetryMaxMs < snapshotRetryBaseMs) {
    throw new Error(
      'INQTRIX_COLLABORATION_SNAPSHOT_RETRY_MAX_MS must be greater than or equal to INQTRIX_COLLABORATION_SNAPSHOT_RETRY_BASE_MS',
    )
  }
  const instanceLeaseSeconds = integer(
    env,
    'INQTRIX_COLLABORATION_INSTANCE_LEASE_SECONDS',
    15,
    5,
    300,
  )
  const instanceRenewSeconds = integer(
    env,
    'INQTRIX_COLLABORATION_INSTANCE_RENEW_SECONDS',
    5,
    1,
    60,
  )
  if (instanceRenewSeconds >= instanceLeaseSeconds) {
    throw new Error(
      'INQTRIX_COLLABORATION_INSTANCE_RENEW_SECONDS must be lower than INQTRIX_COLLABORATION_INSTANCE_LEASE_SECONDS',
    )
  }

  return {
    apiBaseUrl,
    apiTimeoutMs: integer(env, 'INQTRIX_COLLABORATION_HTTP_TIMEOUT_MS', 5_000, 100, 30_000),
    awarenessRateLimit: integer(
      env,
      'INQTRIX_COLLABORATION_AWARENESS_RATE_PER_SECOND',
      20,
      1,
      200,
    ),
    awarenessRateWindowMs: 1_000,
    bindAddress: env.INQTRIX_COLLABORATION_BIND_ADDRESS?.trim() || '0.0.0.0',
    documentLimitBytes: integer(env, 'INQTRIX_COLLABORATION_MAX_DOCUMENT_BYTES', 10 * MEBIBYTE, MEBIBYTE, 100 * MEBIBYTE),
    frameLimitBytes: integer(env, 'INQTRIX_COLLABORATION_MAX_FRAME_BYTES', 2 * MEBIBYTE, 64 * 1024, 16 * MEBIBYTE),
    instanceId,
    instanceLeaseSeconds,
    instanceRenewSeconds,
    maintenanceIntervalMs: integer(
      env,
      'INQTRIX_COLLABORATION_MAINTENANCE_INTERVAL_SECONDS',
      60,
      5,
      86_400,
    ) * 1_000,
    maxQueuedBytes: integer(
      env,
      'INQTRIX_COLLABORATION_MAX_QUEUED_BYTES',
      8 * MEBIBYTE,
      64 * 1024,
      64 * MEBIBYTE,
    ),
    maxQueuedFrames: integer(
      env,
      'INQTRIX_COLLABORATION_MAX_QUEUED_FRAMES',
      32,
      1,
      256,
    ),
    maxSessionsPerUserDocument: integer(
      env,
      'INQTRIX_COLLABORATION_MAX_SESSIONS_PER_USER_DOCUMENT',
      5,
      1,
      50,
    ),
    port: integer(env, 'INQTRIX_COLLABORATION_PORT', 1234, 1, 65_535),
    policyPollMs: integer(env, 'INQTRIX_COLLABORATION_POLICY_POLL_MS', 2_000, 250, 60_000),
    policyRevalidationTimeoutMs: integer(
      env,
      'INQTRIX_COLLABORATION_POLICY_REVALIDATION_TIMEOUT_MS',
      7_500,
      1_000,
      60_000,
    ),
    protocolVersion: EDITOR_COLLABORATION_PROTOCOL_VERSION,
    schemaVersion: EDITOR_SCHEMA_VERSION,
    secret,
    snapshotIdleMs: integer(
      env,
      'INQTRIX_COLLABORATION_SNAPSHOT_IDLE_SECONDS',
      5,
      1,
      300,
    ) * 1_000,
    snapshotMaxUpdates: integer(
      env,
      'INQTRIX_COLLABORATION_SNAPSHOT_UPDATE_COUNT',
      256,
      1,
      100_000,
    ),
    snapshotRetryBaseMs,
    snapshotRetryMaxMs,
    snapshotTailBytes: integer(env, 'INQTRIX_COLLABORATION_SNAPSHOT_TAIL_BYTES', MEBIBYTE, 64 * 1024, 100 * MEBIBYTE),
    socketBackpressureBytes: integer(
      env,
      'INQTRIX_COLLABORATION_SOCKET_BACKPRESSURE_BYTES',
      4 * MEBIBYTE,
      64 * 1024,
      64 * MEBIBYTE,
    ),
    tenantId: tenantId(env),
    updateRateLimit: integer(
      env,
      'INQTRIX_COLLABORATION_UPDATE_RATE_COUNT',
      120,
      1,
      10_000,
    ),
    updateRateWindowMs: integer(
      env,
      'INQTRIX_COLLABORATION_UPDATE_RATE_WINDOW_SECONDS',
      10,
      1,
      60,
    ) * 1_000,
    websocketPath: '/collaboration',
  }
}

function tenantId(env: Readonly<Record<string, string | undefined>>): string {
  const value = env.INQTRIX_COLLABORATION_TENANT_ID?.trim() || 'default'
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/.test(value)) {
    throw new Error('INQTRIX_COLLABORATION_TENANT_ID must be a valid tenant identifier')
  }
  return value
}

function required(
  env: Readonly<Record<string, string | undefined>>,
  name: string,
): string {
  const value = env[name]?.trim()
  if (!value) throw new Error(`${name} is required`)
  return value
}

function integer(
  env: Readonly<Record<string, string | undefined>>,
  name: string,
  defaultValue: number,
  minimum: number,
  maximum: number,
): number {
  const raw = env[name]
  if (raw === undefined || raw.trim() === '') return defaultValue
  if (!/^[0-9]+$/.test(raw.trim())) throw new Error(`${name} must be an integer`)
  const value = Number(raw)
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${name} must be between ${minimum} and ${maximum}`)
  }
  return value
}

function parseApiUrl(value: string): string {
  const url = new URL(value)
  if ((url.protocol !== 'http:' && url.protocol !== 'https:') || url.username || url.password) {
    throw new Error('INQTRIX_API_INTERNAL_URL must be an HTTP(S) URL without credentials')
  }
  url.pathname = url.pathname.replace(/\/$/, '')
  url.search = ''
  url.hash = ''
  return url.toString().replace(/\/$/, '')
}
