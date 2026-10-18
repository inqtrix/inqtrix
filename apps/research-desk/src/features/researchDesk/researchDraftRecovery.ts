import type { ComposerFormState } from './components/Composer'

export const RESEARCH_DRAFT_RECOVERY_MAX_AGE_MS = 30 * 60 * 1_000
export const RESEARCH_DRAFT_RECOVERY_KEY = 'inqtrix.research.auth-recovery.v1'

export type ResearchDraftStorage = Pick<Storage, 'getItem' | 'removeItem' | 'setItem'>

type StoredResearchDraft = {
  createdAt: number
  form: ComposerFormState
  userId: string
}

export function browserResearchDraftStorage(): ResearchDraftStorage | null {
  try {
    return globalThis.sessionStorage ?? null
  } catch {
    return null
  }
}

export function saveResearchDraftRecovery(
  storage: ResearchDraftStorage,
  userId: string,
  form: ComposerFormState,
  now = Date.now(),
): boolean {
  const normalizedUserId = userId.trim()
  if (!normalizedUserId || !isComposerFormState(form)) return false
  try {
    storage.setItem(RESEARCH_DRAFT_RECOVERY_KEY, JSON.stringify({
      createdAt: now,
      form,
      userId: normalizedUserId,
    } satisfies StoredResearchDraft))
    return true
  } catch {
    return false
  }
}

/** Read-once recovery. Invalid, expired, or differently owned data is removed
 * before returning so an account switch can never expose another user's text. */
export function takeResearchDraftRecovery(
  storage: ResearchDraftStorage,
  userId: string,
  now = Date.now(),
): ComposerFormState | null {
  let raw: string | null
  try {
    raw = storage.getItem(RESEARCH_DRAFT_RECOVERY_KEY)
    storage.removeItem(RESEARCH_DRAFT_RECOVERY_KEY)
  } catch {
    return null
  }
  if (!raw) return null

  try {
    const value = JSON.parse(raw) as unknown
    if (!isStoredResearchDraft(value)) return null
    const ageMs = now - value.createdAt
    if (
      value.userId !== userId.trim()
      || ageMs < 0
      || ageMs > RESEARCH_DRAFT_RECOVERY_MAX_AGE_MS
    ) return null
    return value.form
  } catch {
    return null
  }
}

function isStoredResearchDraft(value: unknown): value is StoredResearchDraft {
  return isRecord(value)
    && typeof value.createdAt === 'number'
    && Number.isFinite(value.createdAt)
    && typeof value.userId === 'string'
    && isComposerFormState(value.form)
}

function isComposerFormState(value: unknown): value is ComposerFormState {
  if (!isRecord(value)) return false
  return typeof value.confidenceStop === 'number'
    && [6, 7, 8, 9].includes(value.confidenceStop)
    && typeof value.firstRoundQueries === 'number'
    && [4, 6, 8].includes(value.firstRoundQueries)
    && typeof value.maxRounds === 'number'
    && [1, 2, 3, 4].includes(value.maxRounds)
    && typeof value.minRounds === 'number'
    && [1, 2].includes(value.minRounds)
    && value.minRounds <= value.maxRounds
    && typeof value.question === 'string'
    && typeof value.reportProfile === 'string'
    && ['schnell', 'compact', 'deep'].includes(value.reportProfile)
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
