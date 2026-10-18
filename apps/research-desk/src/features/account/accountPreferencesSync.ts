/**
 * Pure conversion between the local ProjectPreferences and the server
 * account-preferences wire shape (account tier).
 *
 * Account preferences (theme / locale / contrast / bubble tone) follow the
 * USER across devices, so they live in their own per-user server row — separate
 * from the project, and deliberately NOT part of the project import. This module is the
 * tiny snake_case<->camelCase bridge; the enum normalization defends against a
 * malformed row (the server CHECK-constrains these, but the client stays
 * defensive like the other converters).
 */

import type {
  AccountPreferencesPayload,
  ServerAccountPreferences,
} from '@/api/inqtrixClient'
import type { Locale } from '@/i18n/translations'
import type { ProjectPreferences } from '@/features/project/types'
import type { ModelTierPreference } from '@/features/researchRuns/types'
import type {
  ContrastMode,
  ThemeMode,
  ThemePreset,
  UserBubbleTone,
} from '@/theme/ThemeProvider'

const VALID_CONTRAST: ReadonlySet<string> = new Set(['standard', 'high'])
const VALID_LOCALE: ReadonlySet<string> = new Set(['de', 'en'])
const VALID_THEME: ReadonlySet<string> = new Set(['light', 'dark', 'system'])
const VALID_PRESET: ReadonlySet<string> = new Set(['petrol', 'standard', 'slate', 'graphite', 'sage'])
const VALID_USER_BUBBLE_TONE: ReadonlySet<string> = new Set([
  'gray',
  'mint',
  'orange',
  'sky',
  'violet',
  'ink',
])
/** `''` is a legitimate value here — it means "no preference", which is what
 * the picker shows as its server-default entry. */
const VALID_MODEL_TIER: ReadonlySet<string> = new Set(['', 'high', 'mid', 'fast'])

function modelTierOrFallback(
  value: string | null | undefined,
  fallback: ModelTierPreference,
): ModelTierPreference {
  if (value === undefined || value === null) return fallback
  return VALID_MODEL_TIER.has(value) ? (value as ModelTierPreference) : fallback
}

/** One server row -> local preferences, falling back per field to the given
 * defaults when a value is out of domain (so a corrupt row never breaks the UI). */
export function preferencesFromServer(
  server: ServerAccountPreferences,
  fallback: ProjectPreferences,
): ProjectPreferences {
  return {
    // Boolean opt-in: an absent field (legacy row / old server) resolves to
    // the fallback, which is the privacy default OFF.
    agentMemoryEnabled:
      typeof server.enable_agent_memory === 'boolean'
        ? server.enable_agent_memory
        : fallback.agentMemoryEnabled,
    agentModelTier: modelTierOrFallback(server.agent_model_tier, fallback.agentModelTier),
    chatModelTier: modelTierOrFallback(server.chat_model_tier, fallback.chatModelTier),
    contrastMode: VALID_CONTRAST.has(server.contrast_mode)
      ? (server.contrast_mode as ContrastMode)
      : fallback.contrastMode,
    locale: VALID_LOCALE.has(server.locale) ? (server.locale as Locale) : fallback.locale,
    theme: VALID_THEME.has(server.theme) ? (server.theme as ThemeMode) : fallback.theme,
    themePreset: VALID_PRESET.has(server.theme_preset)
      ? (server.theme_preset as ThemePreset)
      : fallback.themePreset,
    userBubbleTone:
      server.user_bubble_tone && VALID_USER_BUBBLE_TONE.has(server.user_bubble_tone)
        ? (server.user_bubble_tone as UserBubbleTone)
        : fallback.userBubbleTone,
  }
}

/** Local preferences -> server payload. ``updatedAt`` is a unix-seconds sync
 * stamp the caller supplies (the row has no other lifecycle). */
export function serverAccountPreferencesPayload(
  preferences: ProjectPreferences,
  updatedAt: number,
): AccountPreferencesPayload {
  return {
    contrast_mode: preferences.contrastMode,
    locale: preferences.locale,
    theme: preferences.theme,
    theme_preset: preferences.themePreset,
    user_bubble_tone: preferences.userBubbleTone,
    // Whole-row upsert: the opt-in MUST ride every save or the server resets
    // it to the default OFF. The same holds for every field below.
    enable_agent_memory: preferences.agentMemoryEnabled,
    chat_model_tier: preferences.chatModelTier,
    agent_model_tier: preferences.agentModelTier,
    updated_at: updatedAt,
  }
}

/** Equality key for the autosave diff — the account preference fields.
 *
 * This is the ONLY thing that decides whether a change is pushed. A field
 * missing here syncs silently never: the user changes it, no request goes
 * out, nothing fails, and the value is gone after a reload. Every field in
 * the payload belongs here. */
export function preferencesFingerprint(preferences: ProjectPreferences): string {
  return [
    preferences.contrastMode,
    preferences.locale,
    preferences.theme,
    preferences.themePreset,
    preferences.userBubbleTone,
    preferences.agentMemoryEnabled ? 'mem:on' : 'mem:off',
    `chat:${preferences.chatModelTier}`,
    `agent:${preferences.agentModelTier}`,
  ].join('|')
}
