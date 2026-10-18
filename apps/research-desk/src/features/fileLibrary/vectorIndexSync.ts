/**
 * Pure conversion + push helpers between the local ProjectState vector-index
 * records and the server wire shape (project-persistence tier).
 *
 * Like assetSync, the client speaks the verbatim server shape (snake_case,
 * unix-seconds floats); this maps it to/from the ISO-timestamped
 * VectorIndexRecord the reducer uses. Unlike assets there is NO heavy lazy
 * body — the record (its members + capped history) travels whole, so the list
 * returns full records and there is no load-on-use.
 *
 * The autosave fingerprint is the structured {updatedAt, status}: a record is
 * pushed only once it reaches a TERMINAL status (ready / stale / error). While
 * a reindex is in flight (status === 'indexing') the push is deferred, so the
 * server only ever holds a durable status — a cross-device observer never sees
 * a frozen "indexing" spinner and a crashed run never strands one. The live
 * reindex progress (``indexingJobs``) is a separate, non-serialized map and is
 * never synced at all.
 */

import type { ClientOptions } from '@/api/inqtrixClient'
import {
  saveVectorIndex,
  type ServerVectorIndex,
  type ServerVectorIndexHistoryEntry,
  type ServerVectorIndexMember,
} from '@/api/inqtrixClient'
import type {
  EmbedModelId,
  VectorIndexMemberRecord,
  VectorIndexMemberState,
  VectorIndexRecord,
  VectorIndexRunHistoryEntry,
  VectorIndexRunResult,
  VectorIndexStatus,
} from '@/features/project/types'
import { isoFromUnixSeconds, unixSecondsFromIso } from '@/lib/time'

const VALID_STATUS: ReadonlySet<string> = new Set([
  'delete_failed', 'deleting', 'error', 'indexing', 'ready', 'stale',
])
const VALID_MEMBER_STATE: ReadonlySet<string> = new Set(['pending', 'embedded', 'skipped'])
const VALID_RESULT: ReadonlySet<string> = new Set(['cancelled', 'error', 'ok'])

function normalizeStatus(status: string): VectorIndexStatus {
  return VALID_STATUS.has(status) ? (status as VectorIndexStatus) : 'stale'
}

function normalizeMemberState(state: string): VectorIndexMemberState {
  return VALID_MEMBER_STATE.has(state) ? (state as VectorIndexMemberState) : 'pending'
}

function normalizeResult(result: string): VectorIndexRunResult {
  return VALID_RESULT.has(result) ? (result as VectorIndexRunResult) : 'ok'
}

// -- server -> record ------------------------------------------------------ #

function memberFromServer(member: ServerVectorIndexMember): VectorIndexMemberRecord {
  return {
    fileId: member.file_id,
    state: normalizeMemberState(member.state),
    ...(member.server_document_id
      ? { serverDocumentId: member.server_document_id }
      : {}),
  }
}

function historyEntryFromServer(
  entry: ServerVectorIndexHistoryEntry,
): VectorIndexRunHistoryEntry {
  return {
    documents: entry.documents,
    durationMs: entry.duration_ms,
    error: entry.error,
    finishedAt: isoFromUnixSeconds(entry.finished_at),
    result: normalizeResult(entry.result),
    startedAt: isoFromUnixSeconds(entry.started_at),
  }
}

export function vectorIndexRecordFromServer(index: ServerVectorIndex): VectorIndexRecord {
  return {
    createdAt: isoFromUnixSeconds(index.created_at),
    dims: index.dims,
    handle: index.handle,
    id: index.id,
    lastError: index.last_error,
    members: index.members.map(memberFromServer),
    model: index.model as EmbedModelId,
    serverCollectionId: index.server_collection_id,
    serverCollectionModel: index.server_collection_model,
    status: normalizeStatus(index.status),
    title: index.title,
    updatedAt: isoFromUnixSeconds(index.updated_at),
    ...(index.history.length > 0
      ? { history: index.history.map(historyEntryFromServer) }
      : {}),
  }
}

// -- record -> server payload ---------------------------------------------- #

function serverMemberPayload(member: VectorIndexMemberRecord): ServerVectorIndexMember {
  return {
    file_id: member.fileId,
    state: member.state,
    ...(member.serverDocumentId
      ? { server_document_id: member.serverDocumentId }
      : {}),
  }
}

function serverHistoryPayload(
  entry: VectorIndexRunHistoryEntry,
): ServerVectorIndexHistoryEntry {
  return {
    documents: entry.documents,
    duration_ms: entry.durationMs,
    error: entry.error ?? null,
    finished_at: unixSecondsFromIso(entry.finishedAt),
    result: entry.result,
    started_at: unixSecondsFromIso(entry.startedAt),
  }
}

export function serverVectorIndexPayload(record: VectorIndexRecord): {
  created_at: number
  dims: number
  handle: string
  history: ServerVectorIndexHistoryEntry[]
  last_error: string | null
  members: ServerVectorIndexMember[]
  model: string
  server_collection_id: string | null
  server_collection_model: string | null
  status: string
  title: string
  updated_at: number
} {
  return {
    created_at: unixSecondsFromIso(record.createdAt),
    dims: record.dims,
    handle: record.handle,
    history: (record.history ?? []).map(serverHistoryPayload),
    last_error: record.lastError ?? null,
    members: record.members.map(serverMemberPayload),
    model: record.model,
    server_collection_id: record.serverCollectionId ?? null,
    server_collection_model: record.serverCollectionModel ?? null,
    status: record.status,
    title: record.title,
    updated_at: unixSecondsFromIso(record.updatedAt),
  }
}

// -- autosave fingerprint -------------------------------------------------- #

/** {updatedAt, status}: a record is pushed only on a content change AND while
 * it is NOT mid-reindex, so a transient 'indexing' status is never persisted. */
export type VectorIndexFingerprint = { status: string; updatedAt: string }

export function vectorIndexFingerprint(record: VectorIndexRecord): VectorIndexFingerprint {
  return { status: record.status, updatedAt: record.updatedAt }
}

export function vectorIndexChanged(
  previous: VectorIndexFingerprint | undefined,
  current: VectorIndexFingerprint,
): boolean {
  // Never push an in-flight reindex (defer until it reaches a terminal status);
  // otherwise push on any updatedAt change (or a first sight).
  if (current.status === 'indexing' || current.status === 'deleting' || current.status === 'delete_failed') return false
  return previous === undefined || previous.updatedAt !== current.updatedAt
}

/** Debounce override for the vector-index autosave: membership GROWTH flushes
 * immediately (delay 0), everything else keeps the regular debounce. New
 * members are a structural change whose loss window should be as small as the
 * upload's — a reload right after adding documents must find them on the
 * server. Pure so the trigger is unit-testable. */
export function autosaveDelayForVectorIndexes(
  previous: Record<string, VectorIndexRecord>,
  next: Record<string, VectorIndexRecord>,
  debounceMs: number,
): number {
  for (const [id, record] of Object.entries(next)) {
    const before = previous[id]
    if (before ? record.members.length > before.members.length : record.members.length > 0) {
      return 0
    }
  }
  return debounceMs
}

// -- whole-project push (the explicit import) ------------------------------ #

/** Push every TERMINAL local vector index to the server (the one-time import).
 * Mid-reindex records are skipped — the autosave pushes them once they finish,
 * matching the deferral rule above. Idempotent server upserts make a re-run
 * safe. */
export async function pushAllVectorIndexEntities(
  indexes: Record<string, VectorIndexRecord>,
  options: ClientOptions,
): Promise<void> {
  for (const index of Object.values(indexes)) {
    if (index.status === 'indexing') continue
    await saveVectorIndex(index.id, serverVectorIndexPayload(index), options)
  }
}
