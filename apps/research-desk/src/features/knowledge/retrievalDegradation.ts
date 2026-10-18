import type {
  KnowledgeRetrievalDegradation,
  KnowledgeSearchWarning,
} from '@/features/researchRuns/types'
import type { TranslationDictionary } from '@/i18n/translations'
import { countLabel } from '@/i18n/plural'
import { asFiniteNumber, asNonEmptyString } from '@/lib/coerce'

type KnowledgeStrings = TranslationDictionary['knowledge']

export type KnowledgeSearchWarningNotice = {
  message: string
  tone: 'informational' | 'warning'
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null
}

function parseDegradation(value: unknown): KnowledgeRetrievalDegradation | null {
  const raw = asRecord(value)
  if (!raw) return null
  const reason = asNonEmptyString(raw.reason)
  const retrievalMode = asNonEmptyString(raw.retrieval_mode)
  const requestedTopK = asFiniteNumber(raw.requested_top_k)
  const returnedHits = asFiniteNumber(raw.returned_hits)
  const candidateCap = raw.candidate_cap === null
    ? null
    : asFiniteNumber(raw.candidate_cap)
  if (!reason || !retrievalMode || requestedTopK === undefined || returnedHits === undefined) {
    return null
  }
  const finalTopK = asFiniteNumber(raw.final_top_k) ?? requestedTopK
  const requestedCandidatePool = asFiniteNumber(raw.requested_candidate_pool) ?? requestedTopK
  const returnedCandidatePool = asFiniteNumber(raw.returned_candidate_pool) ?? returnedHits
  return {
    candidate_cap: candidateCap ?? null,
    final_evidence_complete: typeof raw.final_evidence_complete === 'boolean'
      ? raw.final_evidence_complete
      : returnedHits >= finalTopK,
    final_top_k: finalTopK,
    reason,
    requested_candidate_pool: requestedCandidatePool,
    requested_top_k: requestedTopK,
    retrieval_mode: retrievalMode,
    returned_candidate_pool: returnedCandidatePool,
    returned_hits: returnedHits,
    stage: asNonEmptyString(raw.stage) ?? 'vector_candidate_pool',
  }
}

function degradationKey(degradation: KnowledgeRetrievalDegradation): string {
  return [
    degradation.reason,
    degradation.retrieval_mode,
    degradation.stage,
    degradation.requested_candidate_pool,
    degradation.returned_candidate_pool,
    degradation.final_top_k,
    degradation.final_evidence_complete ? 'complete' : 'incomplete',
    degradation.requested_top_k,
    degradation.returned_hits,
    degradation.candidate_cap ?? '',
  ].join('\u0000')
}

/** Parse and deduplicate the untyped SSE/result-state payload. */
export function knowledgeRetrievalDegradations(value: unknown): KnowledgeRetrievalDegradation[] {
  if (!Array.isArray(value)) return []
  return mergeKnowledgeRetrievalDegradations(
    value.flatMap((entry) => {
      const parsed = parseDegradation(entry)
      return parsed ? [parsed] : []
    }),
  )
}

/** Keep event-level and completion-summary copies idempotent. */
export function mergeKnowledgeRetrievalDegradations(
  ...groups: readonly (readonly KnowledgeRetrievalDegradation[])[]
): KnowledgeRetrievalDegradation[] {
  const seen = new Set<string>()
  const merged: KnowledgeRetrievalDegradation[] = []
  for (const degradation of groups.flat()) {
    const key = degradationKey(degradation)
    if (seen.has(key)) continue
    seen.add(key)
    merged.push(degradation)
  }
  return merged
}

function parseWarning(value: unknown): KnowledgeSearchWarning | null {
  const raw = asRecord(value)
  if (!raw) return null
  const code = asNonEmptyString(raw.code)
  if (!code) return null
  const count = asFiniteNumber(raw.count)
  const reason = asNonEmptyString(raw.reason)
  const stage = asNonEmptyString(raw.stage)
  const recommendedAction = asNonEmptyString(raw.recommended_action)
  return {
    code,
    message: asNonEmptyString(raw.message) ?? '',
    ...(count !== undefined && count >= 0 ? { count } : {}),
    ...(reason ? { reason } : {}),
    ...(stage ? { stage } : {}),
    ...(raw.recommended_action === null
      ? { recommended_action: null }
      : recommendedAction
        ? { recommended_action: recommendedAction }
        : {}),
  }
}

function warningKey(warning: KnowledgeSearchWarning): string {
  return [
    warning.code,
    warning.reason ?? '',
    warning.stage ?? '',
    warning.recommended_action ?? '',
    ...(warning.filtered_ids ?? []),
  ].join('\u0000')
}

/** Parse the source-integrity warnings persisted on a native Knowledge run. */
export function knowledgeRetrievalWarnings(value: unknown): KnowledgeSearchWarning[] {
  if (!Array.isArray(value)) return []
  return mergeKnowledgeRetrievalWarnings(
    value.flatMap((entry) => {
      const parsed = parseWarning(entry)
      return parsed ? [parsed] : []
    }),
  )
}

/** Merge cumulative event/result snapshots without double-counting replays. */
export function mergeKnowledgeRetrievalWarnings(
  ...groups: readonly (readonly KnowledgeSearchWarning[])[]
): KnowledgeSearchWarning[] {
  const positions = new Map<string, number>()
  const merged: KnowledgeSearchWarning[] = []
  for (const warning of groups.flat()) {
    const key = warningKey(warning)
    const position = positions.get(key)
    if (position === undefined) {
      positions.set(key, merged.length)
      merged.push(warning)
      continue
    }
    const existing = merged[position]
    merged[position] = {
      ...existing,
      ...warning,
      count: Math.max(existing.count ?? 0, warning.count ?? 0),
      message: warning.message?.trim() || existing.message,
    }
  }
  return merged
}

export function knowledgeRetrievalDegradationText(
  degradation: KnowledgeRetrievalDegradation,
  t: KnowledgeStrings,
): string {
  const pool = countLabel(
    degradation.requested_candidate_pool,
    t.retrievalDegradedCandidatePoolOne,
    t.retrievalDegradedCandidatePool,
  ).replace('{returned}', String(degradation.returned_candidate_pool))
  const finalOutcome = (degradation.final_evidence_complete
    ? countLabel(degradation.final_top_k, t.retrievalDegradedFinalCompleteOne, t.retrievalDegradedFinalComplete)
    : countLabel(degradation.final_top_k, t.retrievalDegradedFinalIncompleteOne, t.retrievalDegradedFinalIncomplete))
    .replace('{returned}', String(degradation.returned_hits))
  const boundary = degradation.reason === 'vector_candidate_stalled'
    ? t.retrievalDegradedStalledDetail
    : degradation.candidate_cap === null
      ? ''
      : t.retrievalDegradedCapDetail.replace('{cap}', String(degradation.candidate_cap))
  return [pool, finalOutcome, boundary].filter(Boolean).join(' ')
}

/** Localize known search-envelope warnings. A code this client does not know
 * reads as one general localized line, never as the server's English message
 * or the raw code, and still shows, so no future warning is silently
 * discarded. */
export function knowledgeSearchWarningText(
  warning: KnowledgeSearchWarning,
  t: KnowledgeStrings,
): string {
  if (
    warning.code === 'vector_overfetch_cap'
    || warning.code === 'vector_candidate_stalled'
  ) {
    const degradation = parseDegradation({
      ...warning,
      reason: warning.reason ?? warning.code,
    })
    if (degradation) return knowledgeRetrievalDegradationText(degradation, t)
  }
  if (warning.code === 'collections_filtered') return t.findWarningCollectionsFiltered
  if (warning.code === 'duplicate_documents_collapsed') {
    return countLabel(warning.count ?? 0, t.findWarningDuplicate, t.findWarningDuplicates)
  }
  if (warning.code === 'chunks_require_reindex') {
    return countLabel(warning.count ?? 0, t.findWarningReindexOne, t.findWarningReindex)
  }
  if (warning.code === 'chunks_pending_reconciliation') {
    return countLabel(warning.count ?? 0, t.findWarningReconcileOne, t.findWarningReconcile)
  }
  return t.retrievalDegradedUnknown
}

/** A duplicate collapse protects evidence diversity and is informative rather
 * than a technical retrieval degradation. All other additive warning codes
 * remain warnings until their semantics are explicitly classified. */
export function knowledgeSearchWarningNotice(
  warning: KnowledgeSearchWarning,
  t: KnowledgeStrings,
): KnowledgeSearchWarningNotice {
  return {
    message: knowledgeSearchWarningText(warning, t),
    tone: warning.code === 'duplicate_documents_collapsed'
      ? 'informational'
      : 'warning',
  }
}
