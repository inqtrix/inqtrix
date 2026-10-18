import { describe, expect, it } from 'vitest'
import { formatClockTime, formatMessageTimestamp } from './time'

describe('formatClockTime', () => {
  it('names the local clock time of a server timestamp in the reader\'s language', () => {
    // Unix seconds, the server wire format; built from a LOCAL date so the
    // expectation holds in every time zone the suite runs in.
    const seconds = new Date(2026, 9, 10, 17, 49, 30).getTime() / 1000

    expect(formatClockTime(seconds, 'de')).toBe('17:49')
    expect(formatClockTime(seconds, 'en')).toMatch(/^5:49\sPM$/)
  })
})

describe('formatMessageTimestamp', () => {
  it('formats German chat-style timestamps with date and local time', () => {
    const label = formatMessageTimestamp('2026-06-27T12:34:00.000Z', 'de')

    expect(label).toMatch(/^27\.06\.2026 · \d{2}:\d{2}$/)
  })

  it('formats English chat-style timestamps with date and local time', () => {
    const label = formatMessageTimestamp('2026-06-27T12:34:00.000Z', 'en')

    expect(label).toMatch(/^Jun 27, 2026 · \d{1,2}:34 (AM|PM)$/)
  })

  it('keeps locales apart when called alternately', () => {
    // The formatters are cached per locale. A cache keyed carelessly —
    // or shared across locales — would leak the first caller's format
    // into every later one, and a chat list renders both orders.
    const first = formatMessageTimestamp('2026-06-27T12:34:00.000Z', 'de')
    const second = formatMessageTimestamp('2026-06-27T12:34:00.000Z', 'en')
    const third = formatMessageTimestamp('2026-06-27T12:34:00.000Z', 'de')

    expect(first).toMatch(/^27\.06\.2026 · /)
    expect(second).toMatch(/^Jun 27, 2026 · /)
    expect(third).toBe(first)
  })

  it('returns identical labels for repeated calls with the same input', () => {
    const once = formatMessageTimestamp('2026-01-02T03:04:00.000Z', 'de')
    const twice = formatMessageTimestamp('2026-01-02T03:04:00.000Z', 'de')

    expect(twice).toBe(once)
  })
})
