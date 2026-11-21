import {
  createContext,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import {
  lastAgentTierOf,
  lastResearchProfileOf,
} from '@/features/account/accountPreferencesSync'
import type {
  LastAgentTier,
  LastResearchProfile,
  ModelTierPreference,
} from '@/features/researchRuns/types'

export type ThemeMode = 'light' | 'dark' | 'system'
export type ThemePreset = 'petrol' | 'standard' | 'slate' | 'graphite' | 'sage'
export type ContrastMode = 'standard' | 'high'
export type UserBubbleTone = 'gray' | 'mint' | 'orange' | 'sky' | 'violet' | 'ink'

type ThemeContextValue = {
  // Long-term agent memory opt-in. NOT a visual preference, but it rides the
  // same synced account-preferences bag (ThemeProvider + locale) that
  // useAccountPreferences reads/writes; the server row is authoritative and
  // wins on login. localStorage is only the device cache/default (OFF).
  agentMemoryEnabled: boolean
  // Preferred model tier per surface, riding the same bag for the same
  // reason. Two values, never one: an agent run fans out over several
  // thinking nodes while a chat answer is a single call, so a chat pick must
  // not raise agent spend. `''` means no preference.
  agentModelTier: ModelTierPreference
  chatModelTier: ModelTierPreference
  contrastMode: ContrastMode
  // The last Stufe and report profile picked in the two composers that start
  // with one; they ride the same bag so each composer starts with the user's
  // habit on any device. `''` means no pick yet.
  lastAgentTier: LastAgentTier
  lastResearchProfile: LastResearchProfile
  preset: ThemePreset
  resolvedTheme: 'light' | 'dark'
  setAgentMemoryEnabled: (enabled: boolean) => void
  setAgentModelTier: (tier: ModelTierPreference) => void
  setChatModelTier: (tier: ModelTierPreference) => void
  setContrastMode: (mode: ContrastMode) => void
  setLastAgentTier: (tier: LastAgentTier) => void
  setLastResearchProfile: (profile: LastResearchProfile) => void
  setPreset: (preset: ThemePreset) => void
  setTheme: (theme: ThemeMode) => void
  setUserBubbleTone: (tone: UserBubbleTone) => void
  theme: ThemeMode
  userBubbleTone: UserBubbleTone
}

const THEME_STORAGE_KEY = 'inqtrix.researchDesk.theme'
const THEME_PRESET_STORAGE_KEY = 'inqtrix.researchDesk.themePreset'
const CONTRAST_MODE_STORAGE_KEY = 'inqtrix.researchDesk.contrastMode'
const USER_BUBBLE_TONE_STORAGE_KEY = 'inqtrix.researchDesk.userBubbleTone'
const AGENT_MEMORY_ENABLED_STORAGE_KEY = 'inqtrix.researchDesk.agentMemoryEnabled'
const CHAT_MODEL_TIER_STORAGE_KEY = 'inqtrix.researchDesk.chatModelTier'
const AGENT_MODEL_TIER_STORAGE_KEY = 'inqtrix.researchDesk.agentModelTier'
const LAST_RESEARCH_PROFILE_STORAGE_KEY = 'inqtrix.researchDesk.lastResearchProfile'
const LAST_AGENT_TIER_STORAGE_KEY = 'inqtrix.researchDesk.lastAgentTier'
const THEME_TRANSITION_SUPPRESSION_ATTR = 'data-theme-transition-suppressed'
const ThemeContext = createContext<ThemeContextValue | null>(null)

/** One preference another tab of this browser saved, already checked. */
export type ThemePreferenceChange =
  | { field: 'agentMemoryEnabled'; value: boolean }
  | { field: 'agentModelTier'; value: ModelTierPreference }
  | { field: 'chatModelTier'; value: ModelTierPreference }
  | { field: 'contrastMode'; value: ContrastMode }
  | { field: 'lastAgentTier'; value: LastAgentTier }
  | { field: 'lastResearchProfile'; value: LastResearchProfile }
  | { field: 'preset'; value: ThemePreset }
  | { field: 'theme'; value: ThemeMode }
  | { field: 'userBubbleTone'; value: UserBubbleTone }

type ThemeProviderProps = {
  children: ReactNode
}

export function ThemeProvider({ children }: ThemeProviderProps) {
  const [theme, setThemeState] = useState<ThemeMode>(() => readStoredTheme())
  const [preset, setPresetState] = useState<ThemePreset>(() => readStoredPreset())
  const [contrastMode, setContrastModeState] = useState<ContrastMode>(() =>
    readStoredContrastMode(),
  )
  const [userBubbleTone, setUserBubbleToneState] = useState<UserBubbleTone>(() =>
    readStoredUserBubbleTone(),
  )
  const [agentMemoryEnabled, setAgentMemoryEnabledState] = useState<boolean>(() =>
    readStoredAgentMemoryEnabled(),
  )
  const [chatModelTier, setChatModelTierState] = useState<ModelTierPreference>(() =>
    readStoredModelTier(CHAT_MODEL_TIER_STORAGE_KEY),
  )
  const [agentModelTier, setAgentModelTierState] = useState<ModelTierPreference>(() =>
    readStoredModelTier(AGENT_MODEL_TIER_STORAGE_KEY),
  )
  const [lastResearchProfile, setLastResearchProfileState] = useState<LastResearchProfile>(() =>
    lastResearchProfileOf(readStoredValue(LAST_RESEARCH_PROFILE_STORAGE_KEY)),
  )
  const [lastAgentTier, setLastAgentTierState] = useState<LastAgentTier>(() =>
    lastAgentTierOf(readStoredValue(LAST_AGENT_TIER_STORAGE_KEY)),
  )
  const [systemTheme, setSystemTheme] = useState<'light' | 'dark'>(() =>
    getSystemTheme(),
  )

  const resolvedTheme = theme === 'system' ? systemTheme : theme

  useEffect(() => {
    const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
    const handleChange = () => {
      setSystemTheme(mediaQuery.matches ? 'dark' : 'light')
    }

    handleChange()
    mediaQuery.addEventListener('change', handleChange)
    return () => mediaQuery.removeEventListener('change', handleChange)
  }, [])

  // Another tab of this browser saved a preference: take it over, so this
  // tab never sends an older value back with the whole row it saves next.
  useEffect(() => {
    const followers: {
      [Field in ThemePreferenceChange['field']]: (
        value: Extract<ThemePreferenceChange, { field: Field }>['value'],
      ) => void
    } = {
      agentMemoryEnabled: setAgentMemoryEnabledState,
      agentModelTier: setAgentModelTierState,
      chatModelTier: setChatModelTierState,
      contrastMode: setContrastModeState,
      lastAgentTier: setLastAgentTierState,
      lastResearchProfile: setLastResearchProfileState,
      preset: setPresetState,
      theme: setThemeState,
      userBubbleTone: setUserBubbleToneState,
    }
    const follow = (event: StorageEvent) => {
      const change = preferenceChangeFromStorage(event.key, event.newValue)
      if (!change) return
      const apply = followers[change.field] as (value: ThemePreferenceChange['value']) => void
      apply(change.value)
    }
    window.addEventListener('storage', follow)
    return () => window.removeEventListener('storage', follow)
  }, [])

  useLayoutEffect(() => {
    const root = document.documentElement
    root.setAttribute(THEME_TRANSITION_SUPPRESSION_ATTR, '')
    root.classList.toggle('dark', resolvedTheme === 'dark')
    root.dataset.contrastMode = contrastMode
    root.dataset.themePreset = preset
    root.dataset.userBubbleTone = userBubbleTone
    root.style.colorScheme = resolvedTheme

    // Commit the final token values while transitions are disabled.
    root.getBoundingClientRect()
    return scheduleThemeTransitionRestore(root)
  }, [contrastMode, preset, resolvedTheme, userBubbleTone])

  // Each setter applies the choice first and remembers it on the device
  // afterwards: a storage that refuses to keep it (private mode, blocked site
  // data, a full quota) costs only that memory, never the choice.
  const value = useMemo<ThemeContextValue>(
    () => ({
      agentMemoryEnabled,
      agentModelTier,
      chatModelTier,
      contrastMode,
      lastAgentTier,
      lastResearchProfile,
      preset,
      resolvedTheme,
      setAgentMemoryEnabled(nextEnabled) {
        setAgentMemoryEnabledState(nextEnabled)
        rememberStoredValue(AGENT_MEMORY_ENABLED_STORAGE_KEY, nextEnabled ? 'true' : 'false')
      },
      setAgentModelTier(nextTier) {
        setAgentModelTierState(nextTier)
        rememberStoredValue(AGENT_MODEL_TIER_STORAGE_KEY, nextTier)
      },
      setChatModelTier(nextTier) {
        setChatModelTierState(nextTier)
        rememberStoredValue(CHAT_MODEL_TIER_STORAGE_KEY, nextTier)
      },
      setContrastMode(nextMode) {
        setContrastModeState(nextMode)
        rememberStoredValue(CONTRAST_MODE_STORAGE_KEY, nextMode)
      },
      setLastAgentTier(nextTier) {
        setLastAgentTierState(nextTier)
        rememberStoredValue(LAST_AGENT_TIER_STORAGE_KEY, nextTier)
      },
      setLastResearchProfile(nextProfile) {
        setLastResearchProfileState(nextProfile)
        rememberStoredValue(LAST_RESEARCH_PROFILE_STORAGE_KEY, nextProfile)
      },
      setPreset(nextPreset) {
        setPresetState(nextPreset)
        rememberStoredValue(THEME_PRESET_STORAGE_KEY, nextPreset)
      },
      setTheme(nextTheme) {
        setThemeState(nextTheme)
        rememberStoredValue(THEME_STORAGE_KEY, nextTheme)
      },
      setUserBubbleTone(nextTone) {
        setUserBubbleToneState(nextTone)
        rememberStoredValue(USER_BUBBLE_TONE_STORAGE_KEY, nextTone)
      },
      theme,
      userBubbleTone,
    }),
    [
      agentMemoryEnabled,
      agentModelTier,
      chatModelTier,
      contrastMode,
      lastAgentTier,
      lastResearchProfile,
      preset,
      resolvedTheme,
      theme,
      userBubbleTone,
    ],
  )

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function useTheme() {
  const context = useContext(ThemeContext)
  if (!context) {
    throw new Error('useTheme must be used within ThemeProvider.')
  }
  return context
}

/** The preference another tab saved under `key`, checked like a stored value,
 * or null for a key this provider does not own and for a value it would not
 * read. A last pick is never taken back to no pick: `''` changes nothing. */
export function preferenceChangeFromStorage(
  key: string | null,
  value: string | null,
): ThemePreferenceChange | null {
  switch (key) {
    case THEME_STORAGE_KEY: {
      const theme = themeOf(value)
      return theme ? { field: 'theme', value: theme } : null
    }
    case THEME_PRESET_STORAGE_KEY: {
      const preset = presetOf(value)
      return preset ? { field: 'preset', value: preset } : null
    }
    case CONTRAST_MODE_STORAGE_KEY: {
      const mode = contrastModeOf(value)
      return mode ? { field: 'contrastMode', value: mode } : null
    }
    case USER_BUBBLE_TONE_STORAGE_KEY: {
      const tone = userBubbleToneOf(value)
      return tone ? { field: 'userBubbleTone', value: tone } : null
    }
    case AGENT_MEMORY_ENABLED_STORAGE_KEY: {
      const enabled = agentMemoryEnabledOf(value)
      return enabled === null ? null : { field: 'agentMemoryEnabled', value: enabled }
    }
    case CHAT_MODEL_TIER_STORAGE_KEY: {
      const tier = modelTierOf(value)
      return tier === null ? null : { field: 'chatModelTier', value: tier }
    }
    case AGENT_MODEL_TIER_STORAGE_KEY: {
      const tier = modelTierOf(value)
      return tier === null ? null : { field: 'agentModelTier', value: tier }
    }
    case LAST_RESEARCH_PROFILE_STORAGE_KEY: {
      const profile = lastResearchProfileOf(value)
      return profile ? { field: 'lastResearchProfile', value: profile } : null
    }
    case LAST_AGENT_TIER_STORAGE_KEY: {
      const tier = lastAgentTierOf(value)
      return tier ? { field: 'lastAgentTier', value: tier } : null
    }
    default:
      return null
  }
}

/** A value this device stored, or null when there is none or the storage
 * refuses access (private mode, blocked site data). */
function readStoredValue(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function rememberStoredValue(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    // The choice holds on this page; only this device's memory of it is lost.
  }
}

function themeOf(value: string | null): ThemeMode | null {
  return value === 'light' || value === 'dark' || value === 'system' ? value : null
}

function presetOf(value: string | null): ThemePreset | null {
  return value === 'petrol'
    || value === 'standard'
    || value === 'slate'
    || value === 'graphite'
    || value === 'sage'
    ? value
    : null
}

function contrastModeOf(value: string | null): ContrastMode | null {
  return value === 'standard' || value === 'high' ? value : null
}

function userBubbleToneOf(value: string | null): UserBubbleTone | null {
  return value === 'gray'
    || value === 'mint'
    || value === 'orange'
    || value === 'sky'
    || value === 'violet'
    || value === 'ink'
    ? value
    : null
}

function agentMemoryEnabledOf(value: string | null): boolean | null {
  if (value === 'true') return true
  if (value === 'false') return false
  return null
}

/** `''` is a value of its own here: no preference, the deployment default. */
function modelTierOf(value: string | null): ModelTierPreference | null {
  return value === '' || value === 'high' || value === 'mid' || value === 'fast' ? value : null
}

function readStoredTheme(): ThemeMode {
  return themeOf(readStoredValue(THEME_STORAGE_KEY)) ?? 'system'
}

function readStoredPreset(): ThemePreset {
  return presetOf(readStoredValue(THEME_PRESET_STORAGE_KEY)) ?? 'petrol'
}

function readStoredContrastMode(): ContrastMode {
  return contrastModeOf(readStoredValue(CONTRAST_MODE_STORAGE_KEY)) ?? 'standard'
}

/** Device cache for a model-tier preference; the account row still wins on
 * login. An unreadable or unknown value resolves to `''` (no preference), so
 * the deployment default applies rather than an invented tier. */
function readStoredModelTier(key: string): ModelTierPreference {
  return modelTierOf(readStoredValue(key)) ?? ''
}

function readStoredUserBubbleTone(): UserBubbleTone {
  return userBubbleToneOf(readStoredValue(USER_BUBBLE_TONE_STORAGE_KEY)) ?? 'gray'
}

function readStoredAgentMemoryEnabled(): boolean {
  // Privacy default OFF; the server row wins on login (account-preferences sync).
  return agentMemoryEnabledOf(readStoredValue(AGENT_MEMORY_ENABLED_STORAGE_KEY)) ?? false
}

function getSystemTheme(): 'light' | 'dark' {
  if (typeof window === 'undefined') return 'light'
  return window.matchMedia('(prefers-color-scheme: dark)').matches
    ? 'dark'
    : 'light'
}

function scheduleThemeTransitionRestore(root: HTMLElement): () => void {
  let restored = false
  let frameId: number | null = null
  let timeoutId: number | null = null

  const restore = () => {
    if (restored) return
    restored = true
    root.removeAttribute(THEME_TRANSITION_SUPPRESSION_ATTR)
  }

  if (typeof window.requestAnimationFrame === 'function') {
    frameId = window.requestAnimationFrame(() => {
      timeoutId = window.setTimeout(restore, 0)
    })
  } else {
    timeoutId = window.setTimeout(restore, 0)
  }

  return () => {
    if (frameId !== null) window.cancelAnimationFrame(frameId)
    if (timeoutId !== null) window.clearTimeout(timeoutId)
    restore()
  }
}
