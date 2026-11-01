import { setCitationLabelsLeftEscapedReporter } from '@inqtrix/editor-schema'

import type { LogFields, LogFieldValue, SidecarLogger } from './contracts'

const SENSITIVE_KEY = /(body|comment|content|markdown|secret|token|update)/i

/** The threshold names the Python services use (``INQTRIX_LOG_LEVEL``). */
export const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const
export type LogLevel = (typeof LOG_LEVELS)[number]

const SEVERITY: Record<LogLevel, number> = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 }
const RECORD_LEVEL = { debug: 'DEBUG', error: 'ERROR', info: 'INFO', warn: 'WARNING' } as const

/** One JSON object per record with the fields of the Python services (`ts`,
 * `level`, `service`) plus the event and its fields; records below
 * *threshold* are dropped. */
export function createJsonLogger(
  sink: Pick<Console, 'debug' | 'error' | 'info' | 'warn'> = console,
  threshold: LogLevel = 'INFO',
): SidecarLogger {
  const emit = (
    method: 'debug' | 'error' | 'info' | 'warn',
    event: string,
    fields: LogFields = {},
  ): void => {
    const level = RECORD_LEVEL[method]
    if (SEVERITY[level] < SEVERITY[threshold]) return
    const record: Record<string, LogFieldValue> = {
      ts: new Date().toISOString(),
      level,
      service: 'collaboration',
      event,
    }
    for (const [key, value] of Object.entries(fields)) {
      record[key] = SENSITIVE_KEY.test(key) ? '[redacted]' : value
    }
    sink[method](JSON.stringify(record))
  }

  return {
    debug: (event, fields) => emit('debug', event, fields),
    error: (event, fields) => emit('error', event, fields),
    info: (event, fields) => emit('info', event, fields),
    warn: (event, fields) => emit('warn', event, fields),
  }
}

export const nullLogger: SidecarLogger = {
  debug: () => undefined,
  error: () => undefined,
  info: () => undefined,
  warn: () => undefined,
}

/** Report the shared editor schema's warnings through *logger*: a Markdown
 * projection that leaves citation labels escaped becomes a structured record
 * like every other sidecar event. Installed once, at startup. */
export function routeEditorSchemaWarnings(logger: SidecarLogger): void {
  setCitationLabelsLeftEscapedReporter(() => logger.warn('citation_labels_left_escaped', {
    reason: 'placeholder_character_in_document',
  }))
}
