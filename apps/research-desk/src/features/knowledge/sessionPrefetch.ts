import type { KnowledgeSessionRecord } from '@/features/project/types'

export const RECENT_KNOWLEDGE_SESSION_PREFETCH_COUNT = 5

export function recentKnowledgeSessionsForPrefetch(
  sessions: Record<string, KnowledgeSessionRecord>,
  serverKnownIds: ReadonlySet<string>,
  limit = RECENT_KNOWLEDGE_SESSION_PREFETCH_COUNT,
): KnowledgeSessionRecord[] {
  return Object.values(sessions)
    .filter((session) => serverKnownIds.has(session.id) && !session.deletion)
    .sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : a.updatedAt > b.updatedAt ? -1 : 0))
    .slice(0, limit)
}
