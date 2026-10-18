import { describe, expect, it } from 'vitest'

import type { ServerAccountPreferences } from '@/api/inqtrixClient'
import type { ProjectPreferences } from '@/features/project/types'
import {
  preferencesFingerprint,
  preferencesFromServer,
  serverAccountPreferencesPayload,
} from './accountPreferencesSync'

const fallback: ProjectPreferences = {
  agentMemoryEnabled: false,
  agentModelTier: '',
  chatModelTier: '',
  contrastMode: 'standard',
  locale: 'en',
  theme: 'system',
  themePreset: 'standard',
  userBubbleTone: 'gray',
}

describe('accountPreferencesSync', () => {
  it.each(['petrol', 'standard', 'slate', 'graphite', 'sage'] as const)(
    'round-trips %s without replacing an existing account choice', (preset) => {
      const local = { ...fallback, themePreset: preset }
      const server = serverAccountPreferencesPayload(local, 42)
      // The device's own preset must differ from the row, or a dropped
      // server value would read back as the expected one.
      const device = { ...fallback, themePreset: preset === 'petrol' ? 'standard' : 'petrol' } as const
      expect(preferencesFromServer(server, device)).toEqual(local)
    },
  )

  it('round-trips a server row to preferences and back', () => {
    const server: ServerAccountPreferences = {
      contrast_mode: 'high', locale: 'de', theme: 'dark', theme_preset: 'sage',
      user_bubble_tone: 'mint',
      enable_agent_memory: true,
      chat_model_tier: 'mid',
      agent_model_tier: 'fast',
      updated_at: 1_700_000_000,
    }
    const prefs = preferencesFromServer(server, fallback)
    expect(prefs).toEqual({
      agentMemoryEnabled: true,
      agentModelTier: 'fast',
      chatModelTier: 'mid',
      contrastMode: 'high',
      locale: 'de',
      theme: 'dark',
      themePreset: 'sage',
      userBubbleTone: 'mint',
    })

    const payload = serverAccountPreferencesPayload(prefs, 42)
    expect(payload).toEqual({
      contrast_mode: 'high',
      locale: 'de',
      theme: 'dark',
      theme_preset: 'sage',
      user_bubble_tone: 'mint',
      enable_agent_memory: true,
      chat_model_tier: 'mid',
      agent_model_tier: 'fast',
      updated_at: 42,
    })
  })

  it('falls back per field on an out-of-domain server value', () => {
    const prefs = preferencesFromServer(
      {
        contrast_mode: 'ultra',
        locale: 'fr',
        theme: 'neon',
        theme_preset: 'gold',
        user_bubble_tone: 'rainbow',
        updated_at: 1,
      },
      fallback,
    )
    expect(prefs).toEqual(fallback)
  })

  it('falls back to the local bubble tone when an old server row omits it', () => {
    const prefs = preferencesFromServer(
      { contrast_mode: 'high', locale: 'de', theme: 'dark', theme_preset: 'sage', updated_at: 1 },
      { ...fallback, userBubbleTone: 'sky' },
    )
    expect(prefs.userBubbleTone).toBe('sky')
  })

  it('keeps the local opt-in when an old server row omits enable_agent_memory', () => {
    // Legacy row / old server: an absent boolean must not silently enable
    // memory — it resolves to the local (privacy default OFF) value.
    const prefs = preferencesFromServer(
      { contrast_mode: 'high', locale: 'de', theme: 'dark', theme_preset: 'sage', updated_at: 1 },
      { ...fallback, agentMemoryEnabled: false },
    )
    expect(prefs.agentMemoryEnabled).toBe(false)
  })

  it('fingerprint changes only when a preference field changes', () => {
    const a = preferencesFingerprint(fallback)
    expect(preferencesFingerprint({ ...fallback })).toBe(a)
    expect(preferencesFingerprint({ ...fallback, theme: 'dark' })).not.toBe(a)
    expect(preferencesFingerprint({ ...fallback, userBubbleTone: 'orange' })).not.toBe(a)
    expect(preferencesFingerprint({ ...fallback, agentMemoryEnabled: true })).not.toBe(a)
  })

  it('a changed model tier is visible to the autosave diff', () => {
    // The fingerprint is the ONLY thing that decides whether a change is
    // pushed. A field missing from it syncs silently never: the user picks a
    // tier, no request goes out, nothing fails, and the value is gone after a
    // reload.
    const a = preferencesFingerprint(fallback)
    expect(preferencesFingerprint({ ...fallback, chatModelTier: 'high' })).not.toBe(a)
    expect(preferencesFingerprint({ ...fallback, agentModelTier: 'high' })).not.toBe(a)
    // ...and the two surfaces are distinguishable from each other, so setting
    // one does not read as having set the other.
    expect(preferencesFingerprint({ ...fallback, chatModelTier: 'high' })).not.toBe(
      preferencesFingerprint({ ...fallback, agentModelTier: 'high' }),
    )
  })

  it('a chat tier from the server never lands on the agent', () => {
    // An agent run fans out over several thinking nodes while a chat answer is
    // a single call. If the mapping merged the two, a chat pick would raise
    // agent spend the moment it synced.
    const prefs = preferencesFromServer(
      {
        contrast_mode: 'standard', locale: 'en', theme: 'system',
        theme_preset: 'standard', chat_model_tier: 'high', updated_at: 1,
      },
      fallback,
    )
    expect(prefs.chatModelTier).toBe('high')
    expect(prefs.agentModelTier).toBe('')
  })

  it('keeps the local tiers when an old server row omits them', () => {
    const prefs = preferencesFromServer(
      {
        contrast_mode: 'high', locale: 'de', theme: 'dark',
        theme_preset: 'sage', updated_at: 1,
      },
      { ...fallback, chatModelTier: 'mid', agentModelTier: 'fast' },
    )
    expect(prefs.chatModelTier).toBe('mid')
    expect(prefs.agentModelTier).toBe('fast')
  })

  it('rejects an out-of-domain tier but accepts the empty no-preference value', () => {
    const corrupt = preferencesFromServer(
      {
        contrast_mode: 'standard', locale: 'en', theme: 'system',
        theme_preset: 'standard', chat_model_tier: 'turbo', updated_at: 1,
      },
      { ...fallback, chatModelTier: 'mid' },
    )
    expect(corrupt.chatModelTier).toBe('mid')

    // '' is a legitimate stored value, not a corrupt one: it means the user
    // cleared their preference, which must survive the round trip.
    const cleared = preferencesFromServer(
      {
        contrast_mode: 'standard', locale: 'en', theme: 'system',
        theme_preset: 'standard', chat_model_tier: '', updated_at: 1,
      },
      { ...fallback, chatModelTier: 'mid' },
    )
    expect(cleared.chatModelTier).toBe('')
  })
})
