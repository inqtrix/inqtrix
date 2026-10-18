import { setCitationLabelsLeftEscapedReporter } from '@inqtrix/editor-schema'

import type { LogFields, LogFieldValue, SidecarLogger } from './contracts'

const SENSITIVE_KEY = /(body|comment|content|markdown|secret|token|update)/i

export function createJsonLogger(
  sink: Pick<Console, 'debug' | 'error' | 'info' | 'warn'> = console,
): SidecarLogger {
  const emit = (
    level: 'debug' | 'error' | 'info' | 'warn',
    event: string,
    fields: LogFields = {},
  ): void => {
    const record: Record<string, LogFieldValue> = {
      component: 'collaboration-server',
      event,
      level,
      timestamp: new Date().toISOString(),
    }
    for (const [key, value] of Object.entries(fields)) {
      record[key] = SENSITIVE_KEY.test(key) ? '[redacted]' : value
    }
    sink[level](JSON.stringify(record))
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
