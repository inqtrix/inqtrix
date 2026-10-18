import type { ServerKnowledgeSession, ServerKnowledgeSessionGroup } from '@/api/inqtrixClient'
import type {
  KnowledgeSessionGroupRecord,
  KnowledgeSessionRecord,
  KnowledgeThreadItemRecord,
} from '@/features/project/types'
import { sessionDeletionFromWire } from '@/features/project/sessionDeletion'
import {
  knowledgeRetrievalDegradations,
  knowledgeRetrievalWarnings,
} from './retrievalDegradation'

type ServerKnowledgeThreadItemPayload =
  Omit<KnowledgeThreadItemRecord, 'sessionId'>
  & Partial<Pick<KnowledgeThreadItemRecord, 'sessionId'>>

export function epochToIso(seconds: number): string {
  return new Date(seconds * 1000).toISOString()
}

export function isoToEpoch(iso: string): number {
  const millis = Date.parse(iso)
  return Number.isFinite(millis) ? millis / 1000 : Date.now() / 1000
}

export function sessionRecordFromServer(
  session: ServerKnowledgeSession,
): { groupId: string | null; record: KnowledgeSessionRecord } {
  const deletion = sessionDeletionFromWire(session)
  return {
    groupId: session.group_id ?? null,
    record: {
      createdAt: epochToIso(session.created_at),
      id: session.id,
      title: session.title,
      updatedAt: epochToIso(session.updated_at),
      ...(deletion ? { deletion } : {}),
    },
  }
}

export function groupRecordFromServer(
  group: ServerKnowledgeSessionGroup,
): KnowledgeSessionGroupRecord {
  return {
    createdAt: epochToIso(group.created_at),
    id: group.id,
    title: group.title,
    updatedAt: epochToIso(group.updated_at),
  }
}

export function itemsFromServerSession(
  session: ServerKnowledgeSession,
): KnowledgeThreadItemRecord[] {
  if (!session.items_json) return []
  try {
    const parsed = JSON.parse(session.items_json)
    if (!Array.isArray(parsed)) return []
    return parsed.flatMap((item) => (
      isKnowledgeThreadItem(item)
        ? [{
          ...item,
          ...(item.answer
            ? {
              answer: {
                ...item.answer,
                retrievalDegradations: knowledgeRetrievalDegradations(
                  item.answer.retrievalDegradations,
                ),
                retrievalWarnings: knowledgeRetrievalWarnings(
                  item.answer.retrievalWarnings,
                ),
              },
            }
            : {}),
          sessionId: session.id,
        }]
        : []
    ))
  } catch {
    return []
  }
}

export function serverKnowledgeSessionPayload(
  session: KnowledgeSessionRecord,
  items: readonly KnowledgeThreadItemRecord[],
  groupId: string | null,
) {
  return {
    created_at: isoToEpoch(session.createdAt),
    group_id: groupId,
    items_json: JSON.stringify(items),
    title: session.title,
    updated_at: isoToEpoch(session.updatedAt),
  }
}

export function serverKnowledgeSessionGroupPayload(group: KnowledgeSessionGroupRecord) {
  return {
    created_at: isoToEpoch(group.createdAt),
    title: group.title,
    updated_at: isoToEpoch(group.updatedAt),
  }
}

export function fingerprintKnowledgeSession(
  session: KnowledgeSessionRecord,
  items: readonly KnowledgeThreadItemRecord[],
  groupId: string | null,
): string {
  return JSON.stringify({
    groupId,
    items,
    title: session.title,
    updatedAt: session.updatedAt,
  })
}

function isKnowledgeThreadItem(value: unknown): value is ServerKnowledgeThreadItemPayload {
  if (!value || typeof value !== 'object') return false
  const item = value as Partial<KnowledgeThreadItemRecord>
  return typeof item.id === 'string'
    && typeof item.question === 'string'
    && typeof item.createdAt === 'string'
    && (
      item.status === 'running'
      || item.status === 'completed'
      || item.status === 'failed'
      || item.status === 'cancelled'
    )
    && typeof item.progress === 'object'
}
