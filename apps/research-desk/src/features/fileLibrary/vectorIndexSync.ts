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
  FileAssetRecord,
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

/** Whether an index write may name a member yet.
 *
 * The server refuses a whole index write that names a file it does not hold
 * ("At least one file is no longer available; the index was not changed").
 * A file uploaded INTO an index is such a file until its upload reservation
 * returns: the reducer shows it as a pending member at once, but the server
 * learns of it only with the reservation. The write therefore leaves that
 * member out and names it as soon as the reservation is confirmed. An upload
 * that failed or was cancelled before its reservation returned leaves the
 * file unknown to the server just the same, until a retry confirms it. Only
 * such a positive "this upload never reached the server" leaves a member
 * out: a file this client does not hold, or holds from an earlier session,
 * is left to the server to judge, so no write ever drops a member the server
 * keeps. */
export type MemberOnServer = (fileId: string) => boolean

const EVERY_MEMBER: MemberOnServer = () => true

export function memberOnServerFor(
  assets: Readonly<Record<string, FileAssetRecord>>,
): MemberOnServer {
  return (fileId) => {
    const asset = assets[fileId]
    if (!asset || asset.serverSynced === true) return true
    return !(
      asset.uploadPending === true
      || asset.uploadStatus === 'failed'
      || asset.uploadStatus === 'cancelled'
    )
  }
}

/** The members no index write may name yet, as one stable key. The autosave
 * runs again when it changes, so a member reaches the server the moment its
 * reservation is confirmed instead of waiting for the next unrelated edit. */
export function unconfirmedMemberKey(
  indexes: Readonly<Record<string, VectorIndexRecord>>,
  onServer: MemberOnServer,
): string {
  const waiting: string[] = []
  for (const [indexId, record] of Object.entries(indexes)) {
    for (const member of record.members) {
      if (!onServer(member.fileId)) waiting.push(`${indexId}\u0000${member.fileId}`)
    }
  }
  return waiting.sort().join('\u0001')
}

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

export function serverVectorIndexPayload(
  record: VectorIndexRecord,
  onServer: MemberOnServer = EVERY_MEMBER,
): {
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
    members: record.members
      .filter((member) => onServer(member.fileId))
      .map(serverMemberPayload),
    model: record.model,
    server_collection_id: record.serverCollectionId ?? null,
    server_collection_model: record.serverCollectionModel ?? null,
    // A run's status is transient: a reload that finds the run sets it again.
    status: record.status === 'indexing'
      ? record.members.some((member) => member.state === 'pending') ? 'stale' : 'ready'
      : record.status,
    title: record.title,
    updated_at: unixSecondsFromIso(record.updatedAt),
  }
}

// -- autosave fingerprint -------------------------------------------------- #

/** A record is pushed on a content change, but mid-reindex only for its server
 * identity: a reload has to find the collection and the documents of a run that
 * nobody watches any more. */
export type VectorIndexFingerprint = {
  identity: string
  /** The members the last write named; a member confirmed later changes it. */
  members: string
  status: string
  updatedAt: string
}

function serverIdentity(record: VectorIndexRecord): string {
  return JSON.stringify([
    record.serverCollectionId ?? null,
    record.members.map((member) => member.serverDocumentId ?? null),
  ])
}

export function vectorIndexFingerprint(
  record: VectorIndexRecord,
  onServer: MemberOnServer = EVERY_MEMBER,
): VectorIndexFingerprint {
  return {
    identity: serverIdentity(record),
    members: JSON.stringify(
      record.members
        .filter((member) => onServer(member.fileId))
        .map((member) => member.fileId),
    ),
    status: record.status,
    updatedAt: record.updatedAt,
  }
}

export function vectorIndexChanged(
  previous: VectorIndexFingerprint | undefined,
  current: VectorIndexFingerprint,
): boolean {
  // Defer an in-flight reindex until it reaches a terminal status, except for a
  // server identity it just found; otherwise push on any updatedAt change (or a
  // first sight).
  if (current.status === 'indexing') {
    return previous !== undefined && previous.identity !== current.identity
  }
  if (current.status === 'deleting' || current.status === 'delete_failed') return false
  return previous === undefined
    || previous.updatedAt !== current.updatedAt
    || previous.members !== current.members
}

function gainedServerIdentity(before: VectorIndexRecord, record: VectorIndexRecord): boolean {
  if (record.serverCollectionId && record.serverCollectionId !== before.serverCollectionId) return true
  const known = new Map(before.members.map((member) => [member.fileId, member.serverDocumentId]))
  return record.members.some((member) => (
    member.serverDocumentId && member.serverDocumentId !== known.get(member.fileId)
  ))
}

/** Debounce override for the vector-index autosave: membership GROWTH and a new
 * server identity flush immediately (delay 0), everything else keeps the regular
 * debounce. Their loss window should be as small as the upload's — a reload right
 * after adding documents or starting a run must find them on the server. Pure so
 * the trigger is unit-testable. */
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
    if (before && gainedServerIdentity(before, record)) return 0
  }
  return debounceMs
}

/** {@link autosaveDelayForVectorIndexes}, plus: a member whose reservation
 * was just confirmed flushes at once (delay 0), because its loss window is
 * the upload's own. */
export function autosaveDelayAfterConfirmation(
  previous: Record<string, VectorIndexRecord>,
  next: Record<string, VectorIndexRecord>,
  previousUnconfirmed: string,
  nextUnconfirmed: string,
  debounceMs: number,
): number {
  if (previousUnconfirmed !== nextUnconfirmed) return 0
  return autosaveDelayForVectorIndexes(previous, next, debounceMs)
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
