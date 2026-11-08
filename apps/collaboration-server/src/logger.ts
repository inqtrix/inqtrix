import { format } from 'node:util'

import { setCitationLabelsLeftEscapedReporter } from '@inqtrix/editor-schema'

import type { LogFields, LogFieldValue, SidecarLogger } from './contracts'
import { HookRejection } from './errors'

const SENSITIVE_KEY = /(body|comment|content|markdown|secret|token|update)/i

/** The threshold names the Python services use (``INQTRIX_LOG_LEVEL``). */
export const LOG_LEVELS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const
export type LogLevel = (typeof LOG_LEVELS)[number]

const SEVERITY: Record<LogLevel, number> = { DEBUG: 10, INFO: 20, WARNING: 30, ERROR: 40, CRITICAL: 50 }
const RECORD_LEVEL = { debug: 'DEBUG', error: 'ERROR', info: 'INFO', warn: 'WARNING' } as const
const LIBRARY_TEXT_LIMIT = 200
const LIBRARY_STACK_LINES = 10

type LogSink = Pick<Console, 'debug' | 'error' | 'info' | 'warn'>

/** One JSON object per record with the fields of the Python services (`ts`,
 * `level`, `service`) plus the event and its fields; records below
 * *threshold* are dropped. */
export function createJsonLogger(
  sink: LogSink = console,
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

/** The console's methods as they are now, for a logger that keeps writing to
 * the real console after {@link routeLibraryConsole} replaced them. */
export function consoleSink(target: LogSink = console): LogSink {
  return {
    debug: target.debug.bind(target),
    error: target.error.bind(target),
    info: target.info.bind(target),
    warn: target.warn.bind(target),
  }
}

/** Turn what a library prints past *logger* into one record per call. Hocuspocus
 * prints every connection a hook refuses; the hook has already written
 * `hook_refused` for those, so they add nothing. Anything else keeps its first
 * line and the frames of its error, which carry code paths and no content.
 * Installed once, at startup. */
export function routeLibraryConsole(
  logger: SidecarLogger,
  target: LogSink & Pick<Console, 'log'> = console,
): void {
  const record = (method: keyof SidecarLogger) => (...args: unknown[]): void => {
    if (args.some((arg) => arg instanceof HookRejection)) return
    const [firstLine = ''] = format(...args).split('\n', 1)
    const text = firstLine.slice(0, LIBRARY_TEXT_LIMIT)
    const error = args.find((arg): arg is Error => arg instanceof Error)
    if (!text && !error) return
    logger[method]('library_console', {
      text,
      ...(error
        ? {
            error_type: error.name,
            stack: (error.stack ?? '')
              .split('\n')
              .filter((line) => /^\s+at /.test(line))
              .slice(0, LIBRARY_STACK_LINES)
              .join('\n'),
          }
        : {}),
    })
  }
  target.debug = record('debug')
  target.error = record('error')
  target.info = record('info')
  target.log = record('info')
  target.warn = record('warn')
}

/** Report the shared editor schema's warnings through *logger*: a Markdown
 * projection that leaves citation labels escaped becomes a structured record
 * like every other sidecar event. Installed once, at startup. */
export function routeEditorSchemaWarnings(logger: SidecarLogger): void {
  setCitationLabelsLeftEscapedReporter(() => logger.warn('citation_labels_left_escaped', {
    reason: 'placeholder_character_in_document',
  }))
}
