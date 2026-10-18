import { createProjectEntityId } from './entityId'
import type { KnowledgeSessionRecord } from './types'

export const LEGACY_DEFAULT_KNOWLEDGE_SESSION_ID = 'knowledge-session-default'
export const DEFAULT_KNOWLEDGE_SESSION_TITLE = 'Neue Wissens-Sitzung'

export function createBootstrapKnowledgeSession(
  createdAt: string,
): KnowledgeSessionRecord {
  return {
    createdAt,
    id: createProjectEntityId('ks'),
    isBootstrapPlaceholder: true,
    title: DEFAULT_KNOWLEDGE_SESSION_TITLE,
    updatedAt: createdAt,
  }
}

export function legacyKnowledgeSessionIdReplacements(
  sessions: Record<string, KnowledgeSessionRecord>,
  serverIds: ReadonlySet<string>,
): Record<string, string> {
  const legacy = sessions[LEGACY_DEFAULT_KNOWLEDGE_SESSION_ID]
  if (!legacy || serverIds.has(LEGACY_DEFAULT_KNOWLEDGE_SESSION_ID)) return {}
  const occupied = new Set([...Object.keys(sessions), ...serverIds])
  let replacement = createProjectEntityId('ks')
  while (occupied.has(replacement)) replacement = createProjectEntityId('ks')
  return { [LEGACY_DEFAULT_KNOWLEDGE_SESSION_ID]: replacement }
}
