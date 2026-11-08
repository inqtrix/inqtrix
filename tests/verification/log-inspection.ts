import { spawn } from 'node:child_process'
import { createInterface } from 'node:readline'

import {
  listServiceContainers,
  type ServiceContainer,
} from './fixtures/fault-control-server.mjs'
import type {
  LogInspection,
  ServiceLogCounters,
  VerificationProfile,
} from './model.ts'
import type { RunContext } from './run-context.ts'

const INSPECTED_SERVICES = ['api', 'worker', 'web', 'collaboration'] as const
/** Every run talks to these two, so an empty log means the window is wrong. */
const ACTIVE_SERVICES = ['api', 'web'] as const
const REQUIRED_FIELDS = ['ts', 'level', 'service'] as const
const ERROR_LEVELS = new Set(['ERROR', 'CRITICAL', 'FATAL'])
const NOTABLE_LEVELS = new Set(['WARNING', 'WARN', ...ERROR_LEVELS])
const NOTABLE_LIMIT = 20
const MESSAGE_LIMIT = 150
const READ_TIMEOUT_MS = 120_000
/** A later first line means the log was rotated or the clocks differ. */
const WINDOW_START_TOLERANCE_MS = 120_000

/** Profiles that run against the regular stack and expect it to stay quiet. */
export const QUIET_PROFILES: readonly VerificationProfile[] = [
  'agent-desk',
  'chat-prompt',
  'load-smoke',
  'system-smoke',
]
/**
 * Profiles that provoke failures on purpose: only the log contract holds.
 * agent-recovery is not among them: it recreates api and worker while cleaning
 * up, so the logs of the run are gone before they could be read.
 */
export const FAULT_PROFILES: readonly VerificationProfile[] = [
  'fault-injection',
  'load-capacity',
  'load-ramp',
  'load-soak',
]

const MASKS: readonly (readonly [RegExp, string])[] = [
  [/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g, '<id>'],
  [/\b(run|sess|ws|sk|fsec|thread|doc|del|inqv)[_-][0-9a-zA-Z-]{6,}/g, '$1_<id>'],
  [/\b([a-z]{2,8})_[0-9a-f]{8,}\b/g, '$1_<id>'],
  [/editor-doc-[0-9a-zA-Z-]+/g, 'editor-doc-<id>'],
  [/\b[0-9a-f]{16,}\b/g, '<hex>'],
  [/\b\d{2,}\b/g, '<n>'],
]

/** Reduce a log message to its template: no identifiers, no numbers, bounded. */
export function maskLogMessage(message: string): string {
  let masked = message
  for (const [pattern, replacement] of MASKS) {
    masked = masked.replace(pattern, replacement)
  }
  return masked.slice(0, MESSAGE_LIMIT)
}

export class ServiceLogCounter {
  readonly #levels = new Map<string, number>()
  readonly #missing = new Map<string, number>()
  readonly #notable = new Map<string, { count: number; level: string; logger: string; message: string }>()
  #errors = 0
  #firstLineAt: string | null = null
  #lines = 0
  #notJson = 0

  add(line: string): void {
    if (!line.trim()) return
    this.#lines += 1
    let record: unknown
    try {
      record = JSON.parse(line)
    } catch {
      this.#notJson += 1
      return
    }
    if (typeof record !== 'object' || record === null || Array.isArray(record)) {
      this.#notJson += 1
      return
    }
    const fields = record as Record<string, unknown>
    for (const field of REQUIRED_FIELDS) {
      if (!(field in fields)) bump(this.#missing, field)
    }
    // The Python services name a logger and a message, the collaboration service an event.
    if (!('message' in fields) && !('event' in fields)) bump(this.#missing, 'message or event')
    if (this.#firstLineAt === null && typeof fields.ts === 'string') this.#firstLineAt = fields.ts
    const level = String(fields.level ?? 'MISSING').toUpperCase()
    bump(this.#levels, level)
    if (ERROR_LEVELS.has(level)) this.#errors += 1
    if (NOTABLE_LEVELS.has(level)) {
      const logger = String(fields.logger ?? '').slice(0, MESSAGE_LIMIT)
      const message = maskLogMessage(String(fields.message ?? fields.event ?? ''))
      const key = `${level}|${logger}|${message}`
      const entry = this.#notable.get(key)
      if (entry) entry.count += 1
      else this.#notable.set(key, { count: 1, level, logger, message })
    }
  }

  result(): ServiceLogCounters {
    const notable = [...this.#notable.values()].sort((first, second) => (
      Number(ERROR_LEVELS.has(second.level)) - Number(ERROR_LEVELS.has(first.level))
      || second.count - first.count
    ))
    return {
      errors: this.#errors,
      firstLineAt: this.#firstLineAt,
      levels: Object.fromEntries(this.#levels),
      lines: this.#lines,
      missingFields: Object.fromEntries(this.#missing),
      notable: notable.slice(0, NOTABLE_LIMIT),
      notableDropped: Math.max(0, notable.length - NOTABLE_LIMIT),
      notJson: this.#notJson,
    }
  }
}

function bump(counts: Map<string, number>, key: string): void {
  counts.set(key, (counts.get(key) ?? 0) + 1)
}

/** The contract holds in every inspected profile, silence only in the quiet ones. */
export function judgeServiceLogs(
  profile: VerificationProfile,
  services: Readonly<Record<string, ServiceLogCounters>>,
): 'passed' | 'failed' {
  const rows = Object.values(services)
  const contractHeld = rows.every(
    (row) => row.notJson === 0 && Object.keys(row.missingFields).length === 0,
  )
  const quiet = !QUIET_PROFILES.includes(profile) || rows.every((row) => row.errors === 0)
  return contractHeld && quiet ? 'passed' : 'failed'
}

/** Whether the counters can carry a verdict at all; the reason if they cannot. */
function missingFoundation(
  services: Readonly<Record<string, ServiceLogCounters>>,
  since: string,
): string | null {
  const unstructured = Object.entries(services)
    .filter(([, row]) => row.lines > 0 && row.notJson === row.lines)
    .map(([service]) => service)
  if (unstructured.length > 0) {
    return `The logs of ${unstructured.join(', ')} are not JSON: a text log format or the nginx edge.`
  }
  for (const service of ACTIVE_SERVICES) {
    const row = services[service]
    if (!row || row.lines === 0) return `The ${service} log is empty in the window of this run.`
    if (
      row.firstLineAt !== null
      && Date.parse(row.firstLineAt) - Date.parse(since) > WINDOW_START_TOLERANCE_MS
    ) {
      return `The ${service} log starts later than the run: it was rotated or the clocks differ.`
    }
  }
  return null
}

export type LogSource = {
  list(context: RunContext): Promise<readonly ServiceContainer[]>
  read(
    context: RunContext,
    container: ServiceContainer,
    onLine: (line: string) => void,
  ): Promise<void>
}

/** A failure whose message this module wrote itself, so it may reach the report. */
class LogReadError extends Error {}

const CONTAINER_LOGS: LogSource = {
  async list(context) {
    return await listServiceContainers({
      engine: context.containerEngine,
      repositoryRoot: context.repositoryRoot,
      services: INSPECTED_SERVICES,
    })
  },
  async read(context, container, onLine) {
    const child = spawn(
      context.containerEngine as string,
      ['--log-level=error', 'logs', '--since', context.startedAt, container.id],
      {
        cwd: context.repositoryRoot,
        env: context.environment,
        signal: AbortSignal.timeout(READ_TIMEOUT_MS),
        stdio: ['ignore', 'pipe', 'pipe'],
      },
    )
    const closed = new Promise<number | null>((resolve, reject) => {
      child.once('error', reject)
      child.once('close', resolve)
    })
    // Application logs reach the engine on either stream.
    const [exitCode] = await Promise.all([
      closed,
      ...[child.stdout, child.stderr].map(async (stream) => {
        for await (const line of createInterface({ input: stream, crlfDelay: Infinity })) {
          onLine(line)
        }
      }),
    ])
    if (exitCode !== 0) {
      throw new LogReadError(`Reading the ${container.service} logs ended with ${exitCode}.`)
    }
  },
}

/** A run that selected a container engine must not pass on an inspection it missed. */
export function logInspectionIsMandatory(context: RunContext): boolean {
  return context.containerEngine !== null && isInspected(context.profile)
}

function isInspected(profile: VerificationProfile): boolean {
  return QUIET_PROFILES.includes(profile) || FAULT_PROFILES.includes(profile)
}

/**
 * Count what the services logged in the time window of this run. Only redacted
 * counters leave this function: levels, missing fields and masked message
 * templates, never a log line. Lines already counted keep their verdict when a
 * later read fails; a run that cannot read the logs reports not_run, not a pass.
 */
export async function inspectRunLogs(
  context: RunContext,
  source: LogSource = CONTAINER_LOGS,
): Promise<LogInspection> {
  if (!isInspected(context.profile)) {
    return { reason: 'This profile does not produce an inspectable log window.', status: 'not_run' }
  }
  if (!context.containerEngine) {
    return { reason: 'Reading service logs requires --container-engine.', status: 'not_run' }
  }
  const counters = new Map<string, ServiceLogCounter>()
  const summarize = () => Object.fromEntries(
    [...counters].map(([service, counter]) => [service, counter.result()]),
  )
  const window = { runId: context.runId, since: context.startedAt }
  try {
    const containers = await source.list(context)
    for (const service of ACTIVE_SERVICES) {
      if (!containers.some((container) => container.service === service)) {
        return { reason: `No ${service} container of the canonical stack was found.`, status: 'not_run' }
      }
    }
    for (const container of containers) {
      const counter = counters.get(container.service) ?? new ServiceLogCounter()
      counters.set(container.service, counter)
      await source.read(context, container, (line) => counter.add(line))
    }
    const services = summarize()
    const stopped = containers.filter((container) => !container.running).map(({ service }) => service)
    if (stopped.length > 0 && QUIET_PROFILES.includes(context.profile)) {
      return {
        ...window,
        reason: `Stopped during the run: ${stopped.join(', ')}.`,
        services,
        status: 'failed',
      }
    }
    const missing = missingFoundation(services, context.startedAt)
    if (missing) {
      const noisy = Object.values(services).some((row) => row.errors > 0)
      return noisy && QUIET_PROFILES.includes(context.profile)
        ? { ...window, reason: missing, services, status: 'failed' }
        : { ...window, reason: missing, services, status: 'not_run' }
    }
    return { ...window, services, status: judgeServiceLogs(context.profile, services) }
  } catch (error) {
    const reason = error instanceof LogReadError
      ? error.message
      : error instanceof Error && error.message.startsWith('Log inspection: ')
        ? error.message
        : `Reading service logs failed: ${error instanceof Error ? error.name : 'Error'}.`
    const services = summarize()
    const noisy = Object.values(services).some((row) => row.errors > 0)
    return noisy && QUIET_PROFILES.includes(context.profile)
      ? { ...window, reason, services, status: 'failed' }
      : { ...window, reason, services, status: 'not_run' }
  }
}
