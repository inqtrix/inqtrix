/** Safe coercions for untyped values from the wire (SSE frames, JSON,
 * frontmatter). Defined once so every "read an untyped field" call site
 * shares the same empty-value semantics instead of re-deriving them. */

/** The value when it is a string, else ``undefined``. No trimming — an empty
 * or whitespace string is returned as-is (use {@link asNonEmptyString} to
 * treat blank as absent). */
export function asString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined
}

/** The value when it is a NON-BLANK string (something remains after trim),
 * else ``undefined``. The original (untrimmed) value is returned so callers
 * keep any intentional surrounding whitespace. */
export function asNonEmptyString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined
}

/** The value when it is a finite number, else ``undefined``. Guards against
 * ``NaN``/``Infinity`` that a malformed frame could carry. */
export function asFiniteNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

/** The string members of an array value; ``[]`` for a non-array or when no
 * member is a string (non-string members are dropped, never coerced). */
export function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return value.filter((item): item is string => typeof item === 'string')
}
