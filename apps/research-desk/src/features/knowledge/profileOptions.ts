import type { KnowledgeProfileManifestEntry } from '@/features/researchRuns/types'

/**
 * Picker model for one selectable retrieval profile.
 *
 * Built EXCLUSIVELY from the capability manifest — the picker never
 * hardcodes which profiles a deployment offers, it only attaches
 * known-id labels/latency hints at render time and falls back to the
 * raw id for unknown entries.
 */
export type KnowledgeProfileOption = {
  id: string
  /** True for the delegating `auto` entry (no own stages). */
  isAuto: boolean
  delegatesTo: string[]
  /** Multiplier on the request `top_k` for the effective `final_k` (1.0 unless
   * the profile widens it, currently only `tief`). Defaults to 1. */
  finalKFactor: number
  stages: {
    decompose: boolean
    gateRounds: number
    grounding: boolean
    rerank: boolean
    report: boolean
    vocabularyBridge: boolean
  } | null
  /** Stage names the deployment ceiling reduced for this profile;
   * shown as a muted hint, never hidden. */
  degraded: string[]
}

export function knowledgeProfileOptionsFromManifest(
  entries: readonly KnowledgeProfileManifestEntry[] | undefined,
): KnowledgeProfileOption[] {
  if (!entries) return []
  return entries
    .filter((entry) => typeof entry.id === 'string' && entry.id.trim() !== '')
    .map((entry) => {
      const isAuto = Array.isArray(entry.delegates_to) && !entry.stages
      return {
        degraded: Array.isArray(entry.degraded)
          ? entry.degraded.filter((stage): stage is string => typeof stage === 'string')
          : [],
        delegatesTo: Array.isArray(entry.delegates_to)
          ? entry.delegates_to.filter((id): id is string => typeof id === 'string')
          : [],
        finalKFactor: typeof entry.final_k_factor === 'number' ? entry.final_k_factor : 1,
        id: entry.id,
        isAuto,
        stages: entry.stages
          ? {
            decompose: entry.stages.decompose === true,
            gateRounds: typeof entry.stages.gate_rounds === 'number' ? entry.stages.gate_rounds : 0,
            grounding: entry.stages.grounding === true,
            rerank: entry.stages.rerank === true,
            report: entry.stages.report === true,
            vocabularyBridge: entry.stages.vocabulary_bridge === true,
          }
          : null,
      }
    })
}

export function resolveKnowledgeDefaultProfileId(
  profileOptions: readonly KnowledgeProfileOption[],
  serverDefaultProfileId: string | null | undefined,
): string | null {
  if (profileOptions.length === 0) return null
  const ids = new Set(profileOptions.map((option) => option.id))
  if (ids.has('tief')) return 'tief'
  if (serverDefaultProfileId && ids.has(serverDefaultProfileId)) return serverDefaultProfileId
  return profileOptions[0]?.id ?? null
}
