/**
 * Wire<->record conversion for agent sessions (the knowledgeSessionSync
 * pattern). Agent sessions are light: turns are durable server runs, while
 * `items_json` carries only the small user-owned source policy.
 */

import type { AgentSessionGroupRecord, AgentSessionRecord } from './model'
import { DERIVED_AGENT_SESSION_UPDATED_AT } from './model'
import { titleWire } from '@/features/sessionTitle/model'
import type { ServerAgentSession, ServerAgentSessionGroup } from './types'
import {
  agentModelSelectionKey,
  normalizeAgentKnowledgeSwitch,
  normalizeAgentModelSelection,
  normalizeAgentSourcePolicy,
  type AgentKnowledgeSwitch,
  type AgentSessionModelSelection,
  type AgentSourcePolicy,
} from './executionPolicy'

export function agentSessionFingerprint(
  session: AgentSessionRecord,
): string {
  const sourcePolicy = normalizeAgentSourcePolicy(session.sourcePolicy)
  return [
    session.title,
    String(session.titleRevision ?? 0),
    session.titleSource ?? 'manual',
    session.groupId ?? '',
    session.updatedAt,
    sourcePolicy.web,
    sourcePolicy.knowledge,
    session.knowledgeSwitch ?? '',
    // Without this a model change never reaches the server: the autosave
    // diffs on this key alone, so an omitted field fails silently.
    agentModelSelectionKey(session.modelSelection),
  ].join('\u0000')
}

/** Fingerprint the authoritative list response without depending on a reducer
 * update having committed already. This preserves local-newer-wins: the sync
 * baseline describes the server row, while a newer local record remains
 * detectably different and is flushed afterwards. */
export function serverAgentSessionFingerprint(
  session: ServerAgentSession,
): string {
  const metadata = agentSessionMetadataFromJson(session.items_json)
  return [
    session.title,
    String(session.title_revision ?? 0),
    session.title_source ?? 'manual',
    session.group_id ?? '',
    new Date(session.updated_at * 1000).toISOString(),
    metadata.sourcePolicy.web,
    metadata.sourcePolicy.knowledge,
    metadata.knowledgeSwitch ?? '',
    agentModelSelectionKey(metadata.modelSelection),
  ].join('\u0000')
}

/** Ordered sessions admitted to the persistence API. Shared-run view sessions
 * stay entirely client-derived and are deliberately absent — and so is an
 * UNTOUCHED session record fabricated from a run summary, marked by the epoch
 * `updatedAt` sentinel: it represents nothing user-authored, and pushing it
 * would resurrect its question-derived title over the stored one. Any user
 * mutation stamps a real `updatedAt` and re-admits the row. */
export function persistableAgentSessionsInOrder(
  sessions: Readonly<Record<string, AgentSessionRecord>>,
  sessionOrder: readonly string[],
): AgentSessionRecord[] {
  return sessionOrder.flatMap((sessionId) => {
    const session = sessions[sessionId]
    return session
      && session.persistable !== false
      && session.updatedAt !== DERIVED_AGENT_SESSION_UPDATED_AT
      ? [session]
      : []
  })
}

/**
 * One autosave pass over the agent sessions: push every persistable session
 * whose fingerprint differs from its baseline in `synced`, then forget the
 * baseline of a derived view. It deletes nothing. A session leaves the server
 * only through an explicit user deletion (`useSessionDeletionApi`); one that is
 * missing from `sessions` may have been created while a push here was awaited
 * (`createSession` seeds its baseline before the store holds it), and deleting
 * by that difference removed every such session right after its creation.
 */
export async function pushAgentSessions({
  inScope = () => true,
  order,
  save,
  sessions,
  synced,
}: {
  /** Whether this pass's project is still open (`syncCollection`'s `inScope`). */
  inScope?: () => boolean
  order: readonly string[]
  save: (session: AgentSessionRecord) => Promise<void>
  sessions: Readonly<Record<string, AgentSessionRecord>>
  synced: Map<string, string>
}): Promise<void> {
  for (const session of persistableAgentSessionsInOrder(sessions, order)) {
    if (session.deletion) continue
    const fingerprint = agentSessionFingerprint(session)
    if (synced.get(session.id) !== fingerprint) {
      if (!inScope()) return
      await save(session)
      if (!inScope()) return
      synced.set(session.id, fingerprint)
    }
  }
  for (const sessionId of [...synced.keys()]) {
    // A derived view never issues CREATE/UPDATE/DELETE through the session
    // API, even if a stale local baseline used the same id.
    if (sessions[sessionId]?.persistable === false) synced.delete(sessionId)
  }
}

export function serverAgentSessionPayload(session: AgentSessionRecord): {
  title: string
  title_revision?: number
  title_source?: import('@/features/sessionTitle/model').TitleSource
  items_json: string
  group_id: string | null
  created_at: number
  updated_at: number
} {
  return {
    ...titleWire(session),
    items_json: JSON.stringify({
      source_policy: normalizeAgentSourcePolicy(session.sourcePolicy),
      // Whole-value writes like the policy above: a field must ride every
      // save or the stored value is dropped on the next one.
      knowledge_switch: session.knowledgeSwitch ?? null,
      model_selection: session.modelSelection ?? null,
    }),
    group_id: session.groupId,
    created_at: secondsFromIso(session.createdAt),
    updated_at: secondsFromIso(session.updatedAt),
  }
}

/** Parse only the metadata fields this client owns. Unknown and malformed
 * fields are ignored so older and newer server rows remain loadable. */
export function agentSessionMetadataFromJson(value: string | undefined): {
  sourcePolicy: AgentSourcePolicy
  modelSelection: AgentSessionModelSelection | null
  knowledgeSwitch: AgentKnowledgeSwitch | undefined
} {
  const empty = {
    knowledgeSwitch: undefined,
    modelSelection: null,
    sourcePolicy: normalizeAgentSourcePolicy(null),
  }
  if (!value) return empty
  try {
    const parsed = JSON.parse(value) as unknown
    const record = parsed && typeof parsed === 'object'
      ? (parsed as Record<string, unknown>)
      : null
    return {
      knowledgeSwitch: normalizeAgentKnowledgeSwitch(record?.knowledge_switch),
      modelSelection: normalizeAgentModelSelection(record?.model_selection),
      sourcePolicy: normalizeAgentSourcePolicy(record?.source_policy),
    }
  } catch {
    return empty
  }
}

export function serverAgentSessionGroupPayload(
  group: AgentSessionGroupRecord,
): { title: string; created_at: number; updated_at: number } {
  return {
    title: group.title,
    created_at: secondsFromIso(group.createdAt),
    updated_at: secondsFromIso(group.updatedAt),
  }
}

export function sessionsFromServer(
  sessions: ServerAgentSession[],
  groups: ServerAgentSessionGroup[],
): { sessions: ServerAgentSession[]; groups: ServerAgentSessionGroup[] } {
  return { groups, sessions }
}

function secondsFromIso(value: string): number {
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed / 1000 : Date.now() / 1000
}
