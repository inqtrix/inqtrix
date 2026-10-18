import type { KnowledgeThreadItemRecord } from '@/features/project/types'

export type KnowledgeComposerSessionContext = {
  collectionIds: string[]
  finalK: number | null
  profileId: string | null
  sourceItemId: string
  topK: number | null
}

function boundedPositiveInteger(value: unknown, maximum: number): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  return Math.min(maximum, Math.max(1, Math.round(value)))
}

/**
 * Recover the last submitted Knowledge composer context for one saved
 * session. The item is already the durable source of truth for what the user
 * requested; current collection/profile inventories remain authoritative for
 * what may be selected now.
 */
export function knowledgeComposerContextForSession({
  availableCollectionIds,
  availableProfileIds,
  evidenceKMax,
  itemOrder,
  items,
  sessionId,
}: {
  availableCollectionIds: readonly string[]
  availableProfileIds: readonly string[]
  evidenceKMax: number
  itemOrder: readonly string[]
  items: Readonly<Record<string, KnowledgeThreadItemRecord>>
  sessionId: string
}): KnowledgeComposerSessionContext | null {
  let latest: KnowledgeThreadItemRecord | null = null
  for (let index = itemOrder.length - 1; index >= 0; index -= 1) {
    const item = items[itemOrder[index]]
    if (item?.sessionId === sessionId) {
      latest = item
      break
    }
  }
  if (!latest) return null

  const allowedCollections = new Set(availableCollectionIds)
  const allowedProfiles = new Set(availableProfileIds)
  const requestedProfile = latest.requestedProfile?.trim() || null

  return {
    collectionIds: (latest.collectionIds ?? []).filter((id) => allowedCollections.has(id)),
    finalK: boundedPositiveInteger(latest.finalK, Math.max(1, evidenceKMax)),
    profileId: requestedProfile && allowedProfiles.has(requestedProfile)
      ? requestedProfile
      : null,
    sourceItemId: latest.id,
    topK: boundedPositiveInteger(latest.topK, 50),
  }
}
