import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { setRequestErrorLocale } from '@/api/requestErrorText'
import { translations, type Locale, type TranslationDictionary } from './translations'

type LocaleContextValue = {
  locale: Locale
  setLocale: (locale: Locale) => void
  t: TranslationDictionary
}

const LOCALE_STORAGE_KEY = 'inqtrix.researchDesk.locale'
const LocaleContext = createContext<LocaleContextValue | null>(null)

type LocaleProviderProps = {
  children: ReactNode
  /** Seeds the FIRST render only; a later `setLocale` still wins.
   *
   * The app passes the rule of the page it opens (account or guest-link
   * visitor); without it the account rule applies. Tests that assert German
   * copy pass this so their language is stated,
   * not inherited from whatever the fallback happens to be — an implicit
   * dependency breaks silently the day the fallback changes. */
  initialLocale?: Locale
}

export function LocaleProvider({
  children,
  initialLocale,
}: LocaleProviderProps) {
  const [locale, setLocaleState] = useState<Locale>(
    () => initialLocale ?? readStoredLocale(),
  )
  // While rendering, not in an effect: children's effects run before this
  // provider's, and the requests they start fail in the reader's language.
  setRequestErrorLocale(locale)

  useEffect(() => {
    document.documentElement.lang = locale
  }, [locale])

  const value = useMemo<LocaleContextValue>(
    () => ({
      locale,
      setLocale(nextLocale) {
        localStorage.setItem(LOCALE_STORAGE_KEY, nextLocale)
        setLocaleState(nextLocale)
      },
      t: translations[locale],
    }),
    [locale],
  )

  return (
    <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>
  )
}

export function useLocale() {
  const context = useContext(LocaleContext)
  if (!context) {
    throw new Error('useLocale must be used within LocaleProvider.')
  }
  return context
}

/** The locale for the FIRST render only.
 *
 * The durable setting is the per-user account preference on the server
 * (``account_preferences.locale``, server default ``en``); the project load
 * applies it through ``setLocale``, which also writes it here. This function
 * therefore only answers "what do we paint before that arrives?" — and its
 * answer has to agree with the server default, or a user whose stored
 * preference is English sees a flash of German on every fresh browser.
 */
export function readStoredLocale(): Locale {
  return storedLocale() ?? 'en'
}

/** The locale of a visitor without an account, such as a guest-link page.
 *
 * No account preference will ever arrive for such a visitor, so this is not a
 * first paint but the page's language: a locale stored on this device wins,
 * otherwise the first of the browser's preferred languages the app supports,
 * otherwise English. The browser language is the one signal a visitor
 * without an account brings. Account pages never use it: there the account
 * preference decides.
 */
export function readVisitorLocale(): Locale {
  const stored = storedLocale()
  if (stored) return stored
  if (typeof navigator === 'undefined') return 'en'
  const preferred = navigator.languages?.length ? navigator.languages : [navigator.language]
  for (const tag of preferred) {
    const language = String(tag ?? '').toLowerCase().split('-')[0]
    if (language === 'de' || language === 'en') return language
  }
  return 'en'
}

function storedLocale(): Locale | null {
  if (typeof localStorage === 'undefined') return null
  const stored = localStorage.getItem(LOCALE_STORAGE_KEY)
  return stored === 'en' || stored === 'de' ? stored : null
}
