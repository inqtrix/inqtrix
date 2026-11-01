import { Buffer } from 'node:buffer'

import {
  EDITOR_COLLABORATION_PROTOCOL_VERSION,
  EDITOR_SCHEMA_VERSION,
} from '@inqtrix/editor-schema'

import type { CollaborationSettings, LogFields } from './contracts'
import { LOG_LEVELS, type LogLevel } from './logger'

const MEBIBYTE = 1024 * 1024

/** A setting the operator has to fix. Its message names the variable and
 * the allowed values, never the value itself, so a failed start may say it. */
export class ConfigError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'ConfigError'
  }
}

/** ``INQTRIX_LOG_LEVEL`` as the Python services read it: any case, empty is
 * ``INFO``. */
export function logLevelFromEnv(env: Readonly<Record<string, string | undefined>>): LogLevel {
  const raw = env.INQTRIX_LOG_LEVEL?.trim().toUpperCase() ?? ''
  if (!raw) return 'INFO'
  const level = LOG_LEVELS.find((candidate) => candidate === raw)
  if (!level) throw new ConfigError(`INQTRIX_LOG_LEVEL must be one of ${LOG_LEVELS.join(', ')}`)
  return level
}

/** The shape the Python services accept as a stable exception code. */
const STABLE_ERROR_CODE = /^[A-Za-z0-9_.:+-]{1,120}$/

function typeAndCode(prefix: 'cause' | 'error', error: unknown): LogFields {
  const fields: LogFields = {
    [`${prefix}_type`]: error instanceof Error ? error.name : typeof error,
  }
  const code = error !== null && typeof error === 'object' ? (error as { code?: unknown }).code : undefined
  if ((typeof code === 'string' || typeof code === 'number') && STABLE_ERROR_CODE.test(String(code))) {
    fields[`${prefix}_code`] = String(code)
  }
  return fields
}

/** What the start failure line may say: a configuration error by its
 * message, anything else by the type and stable code of the error and of its
 * direct cause, never by a text. So a taken port (``EADDRINUSE``) and a refused
 * connection (``ECONNREFUSED`` behind ``fetch failed``) read differently. */
export function startFailureFields(error: unknown): LogFields {
  if (error instanceof ConfigError) return { reason: error.message }
  const cause = error instanceof Error ? error.cause : undefined
  return { ...typeAndCode('error', error), ...(cause === undefined ? {} : typeAndCode('cause', cause)) }
}

export function settingsFromEnv(
  env: Readonly<Record<string, string | undefined>>,
  instanceId: string,
): CollaborationSettings {
  const secret = required(env, 'INQTRIX_COLLABORATION_SECRET')
  if (Buffer.byteLength(secret, 'utf8') < 32) {
    throw new ConfigError('INQTRIX_COLLABORATION_SECRET must contain at least 32 UTF-8 bytes')
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
    throw new ConfigError(
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
    throw new ConfigError(
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
    throw new ConfigError('INQTRIX_COLLABORATION_TENANT_ID must be a valid tenant identifier')
  }
  return value
}

function required(
  env: Readonly<Record<string, string | undefined>>,
  name: string,
): string {
  const value = env[name]?.trim()
  if (!value) throw new ConfigError(`${name} is required`)
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
  if (!/^[0-9]+$/.test(raw.trim())) throw new ConfigError(`${name} must be an integer`)
  const value = Number(raw)
  if (!Number.isSafeInteger(value) || value < minimum || value > maximum) {
    throw new ConfigError(`${name} must be between ${minimum} and ${maximum}`)
  }
  return value
}

function parseApiUrl(value: string): string {
  const invalid = new ConfigError('INQTRIX_API_INTERNAL_URL must be an HTTP(S) URL without credentials')
  let url: URL
  try {
    url = new URL(value)
  } catch {
    // The URL error carries the rejected value as ``input``.
    throw invalid
  }
  if ((url.protocol !== 'http:' && url.protocol !== 'https:') || url.username || url.password) {
    throw invalid
  }
  url.pathname = url.pathname.replace(/\/$/, '')
  url.search = ''
  url.hash = ''
  return url.toString().replace(/\/$/, '')
}
