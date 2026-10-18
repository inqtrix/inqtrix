import { describe, expect, it } from 'vitest'

import { defaultComposerFormState } from './components/Composer'
import {
  RESEARCH_DRAFT_RECOVERY_MAX_AGE_MS,
  RESEARCH_DRAFT_RECOVERY_KEY,
  saveResearchDraftRecovery,
  takeResearchDraftRecovery,
  type ResearchDraftStorage,
} from './researchDraftRecovery'

function createMemoryStorage(): ResearchDraftStorage & { size: () => number } {
  const values = new Map<string, string>()
  return {
    getItem: (key) => values.get(key) ?? null,
    removeItem: (key) => {
      values.delete(key)
    },
    setItem: (key, value) => {
      values.set(key, value)
    },
    size: () => values.size,
  }
}

describe('Research auth-recovery draft', () => {
  it('restores the full composer state once for the same authenticated user', () => {
    const storage = createMemoryStorage()
    const form = {
      ...defaultComposerFormState,
      confidenceStop: 9 as const,
      firstRoundQueries: 4 as const,
      maxRounds: 2 as const,
      minRounds: 2 as const,
      question: 'Long retained question 🧪',
      reportProfile: 'compact' as const,
    }

    expect(saveResearchDraftRecovery(storage, 'user-a', form, 1_000)).toBe(true)
    expect(takeResearchDraftRecovery(storage, 'user-a', 2_000)).toEqual(form)
    expect(takeResearchDraftRecovery(storage, 'user-a', 2_001)).toBeNull()
    expect(storage.size()).toBe(0)
  })

  it('discards a draft instead of exposing it to another account', () => {
    const storage = createMemoryStorage()
    saveResearchDraftRecovery(
      storage,
      'user-a',
      { ...defaultComposerFormState, question: 'Private user-a draft' },
      1_000,
    )

    expect(takeResearchDraftRecovery(storage, 'user-b', 2_000)).toBeNull()
    expect(storage.size()).toBe(0)
  })

  it('discards expired and malformed recovery data', () => {
    const storage = createMemoryStorage()
    saveResearchDraftRecovery(
      storage,
      'user-a',
      { ...defaultComposerFormState, question: 'Stale draft' },
      1_000,
    )

    expect(takeResearchDraftRecovery(
      storage,
      'user-a',
      1_000 + RESEARCH_DRAFT_RECOVERY_MAX_AGE_MS + 1,
    )).toBeNull()
    storage.setItem(RESEARCH_DRAFT_RECOVERY_KEY, '{broken')
    expect(takeResearchDraftRecovery(storage, 'user-a', 2_000)).toBeNull()
    storage.setItem(RESEARCH_DRAFT_RECOVERY_KEY, JSON.stringify({
      createdAt: 1_000,
      form: { ...defaultComposerFormState, confidenceStop: '9', question: 'Forged' },
      userId: 'user-a',
    }))
    expect(takeResearchDraftRecovery(storage, 'user-a', 2_000)).toBeNull()
    expect(storage.size()).toBe(0)
  })
})
