import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { join } from 'node:path'

import { formatClockTime, formatCount, formatMessageTimestamp, formatTimeOfDay, intlLocale } from './time'

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

describe('dates, times and numbers follow the interface language', () => {
  // A German desk in an English browser must not show "10:59 PM": every
  // date, time and number follows the interface language.
  const moment = new Date(2026, 10, 6, 22, 59, 7)

  it('formats a clock time in the desk language, not the browser\'s', () => {
    expect(formatTimeOfDay(moment, 'de')).toBe('22:59')
    expect(formatTimeOfDay(moment, 'en')).toMatch(/^10:59\sPM$/)
    expect(formatTimeOfDay(moment.getTime(), 'de', { seconds: true })).toBe('22:59:07')
    expect(formatTimeOfDay('not a date', 'de')).toBe('not a date')
  })

  it('formats a count in the desk language', () => {
    expect(formatCount(12_345, 'de')).toBe('12.345')
    expect(formatCount(12_345, 'en')).toBe('12,345')
    expect(intlLocale('de')).toBe('de-DE')
    expect(intlLocale('en')).toBe('en-US')
  })

  it('leaves no formatter in the desk that asks the browser for its language', () => {
    // Every date, time and number goes through intlLocale(locale). A call
    // without a locale, or with [] or undefined, takes the browser's.
    const blind = [
      /\.toLocale(?:Time|Date)?String\(\s*(?:\[\]|undefined)?\s*[,)]/,
      /new Intl\.(?:DateTimeFormat|NumberFormat|RelativeTimeFormat)\(\s*(?:\[\]|undefined)?\s*[,)]/,
      /new Intl\.(?:DateTimeFormat|NumberFormat)\(\s*'(?:de-DE|en-US)'/,
    ]
    // Anchored on this file, not on the working directory: started from
    // elsewhere, a relative root would scan another tree and pass empty.
    const root = fileURLToPath(new URL('..', import.meta.url))
    const mappingPoint = fileURLToPath(new URL('./time.ts', import.meta.url))
    const offenders: string[] = []
    let scanned = 0
    const walk = (dir: string) => {
      for (const name of readdirSync(dir)) {
        const path = join(dir, name)
        if (statSync(path).isDirectory()) {
          if (name !== 'browser-tests') walk(path)
          continue
        }
        if (!/\.(?:ts|tsx)$/.test(name) || /\.test\.tsx?$/.test(name) || path === mappingPoint) continue
        scanned += 1
        const lines = readFileSync(path, 'utf8').split('\n')
        lines.forEach((line, index) => {
          if (blind.some((pattern) => pattern.test(line))) offenders.push(`${path}:${index + 1}`)
        })
      }
    }
    walk(root)
    expect(scanned).toBeGreaterThan(200)
    expect(offenders).toEqual([])
  })
})
