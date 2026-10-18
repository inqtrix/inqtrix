/**
 * The time a downloaded file of the editor was made, as its name carries it:
 * compact `YYYYMMDD-HHMMSS` on the reader's clock, so the name matches the
 * time the reader saw. Kept apart from the Word export, which is loaded
 * lazily, so that the other downloads do not pull it in.
 */
export function fileNameTimestamp(date: Date): string {
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}`
    + `-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
}
