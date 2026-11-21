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
 *
 * The last report profile and Stufe follow the account: a row that carries
 * the two fields decides, an empty value included, so a pick another account
 * left on this device does not carry over. A server row without the fields,
 * or a project file without picks, leaves the current pick as it is.
 */

import type {
  AccountPreferencesPayload,
  ServerAccountPreferences,
} from '@/api/inqtrixClient'
import type { Locale } from '@/i18n/translations'
import { agentTierIdOf } from '@/features/agent/runSetupLabels'
import type { ProjectPreferences } from '@/features/project/types'
import { reportProfileOf } from '@/features/researchDesk/components/Composer'
import type {
  LastAgentTier,
  LastResearchProfile,
  ModelTierPreference,
} from '@/features/researchRuns/types'
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

/** A stored or received value as the last report profile: one of the
 * profiles the research composer offers, otherwise no pick. */
export function lastResearchProfileOf(value: unknown): LastResearchProfile {
  return reportProfileOf(value) ?? ''
}

/** A stored or received value as the last Stufe: one of the Stufen the
 * desk knows, otherwise no pick. */
export function lastAgentTierOf(value: unknown): LastAgentTier {
  return agentTierIdOf(value) ?? ''
}

/** A project file's preferences, keeping the current last picks where the file
 * names none: an older file, or one saved before any pick, must not undo
 * them. */
export function preferencesWithLastChoicesKept(
  incoming: ProjectPreferences,
  current: ProjectPreferences,
): ProjectPreferences {
  return {
    ...incoming,
    lastAgentTier: incoming.lastAgentTier || current.lastAgentTier,
    lastResearchProfile: incoming.lastResearchProfile || current.lastResearchProfile,
  }
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
    // The row decides for its account: an empty or unknown value is no
    // pick, so a pick another account left on this device does not carry
    // over. Only a row without the fields keeps the device's pick.
    lastAgentTier:
      server.last_agent_tier == null
        ? fallback.lastAgentTier
        : lastAgentTierOf(server.last_agent_tier),
    lastResearchProfile:
      server.last_research_profile == null
        ? fallback.lastResearchProfile
        : lastResearchProfileOf(server.last_research_profile),
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
    last_research_profile: preferences.lastResearchProfile,
    last_agent_tier: preferences.lastAgentTier,
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
    `profile:${preferences.lastResearchProfile}`,
    `stufe:${preferences.lastAgentTier}`,
  ].join('|')
}
