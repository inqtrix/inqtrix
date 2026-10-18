import { readStoredLocale, readVisitorLocale } from '@/i18n/LocaleProvider'
import type { Locale } from '@/i18n/translations'

/** The guest-link token of a `/s/<token>` page, or null for every other path. */
export function guestLinkTokenFromPath(pathname: string): string | null {
  const match = pathname.match(/^\/s\/([^/]+)\/?$/)
  if (!match?.[1]) return null
  try {
    return decodeURIComponent(match[1])
  } catch {
    return null
  }
}

/** The locale the app paints first on this path: a guest link follows the
 * visitor rule, every other page the account rule. */
export function appFirstRenderLocale(pathname: string): Locale {
  return guestLinkTokenFromPath(pathname) !== null
    ? readVisitorLocale()
    : readStoredLocale()
}
