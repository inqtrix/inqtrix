import type { ResearchRunSnapshot } from '@/features/researchRuns/types'

/** Empty child-start snapshots carry identity only. They must never be
 * interpreted as a completed/answering research run. */
export function meaningfulResearchSnapshot(
  value: unknown,
): ResearchRunSnapshot | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined
  const snapshot = value as ResearchRunSnapshot
  return Object.keys(snapshot).length > 0 ? snapshot : undefined
}

export function mergeResearchSnapshot(
  current: ResearchRunSnapshot | undefined,
  incoming: unknown,
): ResearchRunSnapshot | undefined {
  const next = meaningfulResearchSnapshot(incoming)
  if (!next) return current
  return { ...(current ?? {}), ...next }
}
