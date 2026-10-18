const SENSITIVE_KEY =
  /(?:authorization|cookie|credential|csrf|email|lease|owner.?id|password|recipient.?id|secret|storage.?state|token|user.?id)/i
const BEARER_VALUE = /\bBearer\s+[A-Za-z0-9._~+/=-]+/gi
/** Header lines that carry a credential, as a request's call log prints
 * them (Playwright lists every request header on a failed request). */
const CREDENTIAL_HEADER_VALUE =
  /(\b(?:set-cookie|cookie|x-csrf-token|authorization|proxy-authorization)\s*:\s*)[^\r\n]*/gi
const EMAIL_VALUE =
  /[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+/gi
const GUEST_LINK_VALUE = /\begl1\.[A-Za-z0-9._~-]+/g
const GUEST_PATH = /\/s\/[^/?#\s]+/g
const INQTRIX_COOKIE_VALUE =
  /((?:inqtrix_(?:editor_guest_csrf|editor_guest|session|csrf))=)[^;\s'",]+/gi
const SENSITIVE_JSON_VALUE =
  /("(?:authorization|cookie|credential|csrf|email|lease|owner.?id|password|recipient.?id|secret|storage.?state|token|user.?id)"\s*:\s*)("(?:\\.|[^"\\])*"|[^,}\s]+)/gi
const SENSITIVE_QUERY = /([?&](?:access_token|api_key|password|secret|token)=)[^&#\s]*/gi
const UUID_VALUE =
  /\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b/gi

export type Redactor = {
  redact<T>(value: T): T
  redactMessage(value: unknown): string
}

export function createRedactor(environment: NodeJS.ProcessEnv = process.env): Redactor {
  const environmentSecrets = Object.entries(environment)
    .filter(([name, value]) => SENSITIVE_KEY.test(name) && Boolean(value))
    .map(([, value]) => value!)
    .sort((left, right) => right.length - left.length)

  const redactMessage = (value: unknown): string => {
    let output = String(value)
      .replace(CREDENTIAL_HEADER_VALUE, '$1[REDACTED]')
      .replace(INQTRIX_COOKIE_VALUE, '$1[REDACTED]')
      .replace(BEARER_VALUE, 'Bearer [REDACTED]')
      .replace(SENSITIVE_JSON_VALUE, '$1"[REDACTED]"')
      .replace(GUEST_LINK_VALUE, '[REDACTED]')
      .replace(GUEST_PATH, '/s/[REDACTED]')
      .replace(SENSITIVE_QUERY, '$1[REDACTED]')
      .replace(EMAIL_VALUE, '[REDACTED]')
      .replace(UUID_VALUE, '[REDACTED]')
    for (const secret of environmentSecrets) {
      output = output.replaceAll(secret, '[REDACTED]')
    }
    try {
      const parsed = new URL(output)
      if (parsed.username || parsed.password) {
        parsed.username = ''
        parsed.password = ''
        output = parsed.toString()
      }
    } catch {
      // Most diagnostics are not URLs.
    }
    return output.slice(0, 4_000)
  }

  const redact = <T>(value: T): T => redactUnknown(value, redactMessage) as T
  return { redact, redactMessage }
}

function redactUnknown(
  value: unknown,
  redactMessage: (value: unknown) => string,
  key = '',
): unknown {
  if (SENSITIVE_KEY.test(key)) return '[REDACTED]'
  if (typeof value === 'string') return redactMessage(value)
  if (Array.isArray(value)) {
    return value.map((entry) => redactUnknown(entry, redactMessage))
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([entryKey, entryValue]) => [
        entryKey,
        redactUnknown(entryValue, redactMessage, entryKey),
      ]),
    )
  }
  return value
}
