/** Time-formatting helpers shared across features. */

/** Unix-seconds (the server wire format) -> ISO-8601 string (the format
 * every ProjectState record uses). Shared so the wire<->record timestamp
 * boundary is defined once. */
export function isoFromUnixSeconds(seconds: number): string {
  return new Date(seconds * 1000).toISOString()
}

/** ISO-8601 string -> unix-seconds (float, preserving sub-second precision)
 * for sending a record timestamp back to the server. */
export function unixSecondsFromIso(iso: string): number {
  return new Date(iso).getTime() / 1000
}

/** Zero-padded ``HH:MM:SS`` elapsed time from a seconds count. Used by the
 * research-run timer and anywhere a stopwatch-style duration is shown. */
export function formatDuration(seconds: number): string {
  const wholeSeconds = Math.max(0, Math.round(seconds))
  const hours = Math.floor(wholeSeconds / 3600)
  const minutes = Math.floor((wholeSeconds % 3600) / 60)
  const remainingSeconds = wholeSeconds % 60

  return [hours, minutes, remainingSeconds]
    .map((part) => part.toString().padStart(2, '0'))
    .join(':')
}

/** Compact elapsed form for inline history rows: ``m:ss`` under an hour,
 * ``h:mm:ss`` above. Input is milliseconds. */
export function formatDurationMsShort(ms: number): string {
  const wholeSeconds = Math.max(0, Math.round(ms / 1000))
  const hours = Math.floor(wholeSeconds / 3600)
  const minutes = Math.floor((wholeSeconds % 3600) / 60)
  const seconds = wholeSeconds % 60
  const ss = seconds.toString().padStart(2, '0')
  if (hours > 0) return `${hours}:${minutes.toString().padStart(2, '0')}:${ss}`
  return `${minutes}:${ss}`
}

/** Chat-style message timestamp used anywhere a conversational entry needs the
 * same date + time surface. It intentionally uses the viewer's local timezone,
 * matching the previous chat-only formatter. */
// `Intl.DateTimeFormat` construction is the expensive part; formatting
// with an existing instance is cheap. A chat list rebuilt two of them
// per message on every render, so the cost scaled with conversation
// length for a result that only ever depends on the locale. Keyed by
// locale so alternating languages in one list cannot bleed into each
// other, and module-level so the instances survive re-renders.
const messageTimestampFormatters = new Map<
  string,
  { date: Intl.DateTimeFormat; time: Intl.DateTimeFormat }
>()

function messageTimestampFormatter(locale: string): {
  date: Intl.DateTimeFormat
  time: Intl.DateTimeFormat
} {
  const cached = messageTimestampFormatters.get(locale)
  if (cached) return cached
  const formatterLocale = locale === 'de' ? 'de-DE' : 'en-US'
  const created = {
    date: new Intl.DateTimeFormat(formatterLocale, locale === 'de'
      ? {
        day: '2-digit',
        month: '2-digit',
        year: 'numeric',
      }
      : {
        day: 'numeric',
        month: 'short',
        year: 'numeric',
      }),
    time: new Intl.DateTimeFormat(formatterLocale, {
      hour: locale === 'de' ? '2-digit' : 'numeric',
      minute: '2-digit',
    }),
  }
  messageTimestampFormatters.set(locale, created)
  return created
}

export function formatMessageTimestamp(iso: string, locale: string): string {
  const date = new Date(iso)
  const formatter = messageTimestampFormatter(locale)
  return `${formatter.date.format(date)} · ${formatter.time.format(date)}`
}

/** The viewer's local clock time of a server timestamp (unix seconds, the
 * wire format), in the same time format as {@link formatMessageTimestamp}:
 * ``17:49`` in German, ``5:49 PM`` in English. A start time never goes
 * stale the way a duration sampled from the last update does. */
export function formatClockTime(seconds: number, locale: string): string {
  return messageTimestampFormatter(locale).time.format(new Date(seconds * 1000))
}
