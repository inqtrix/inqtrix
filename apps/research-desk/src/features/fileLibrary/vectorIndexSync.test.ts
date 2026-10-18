import { describe, expect, it } from 'vitest'

import type { ServerVectorIndex } from '@/api/inqtrixClient'
import type { VectorIndexRecord } from '@/features/project/types'
import {
  autosaveDelayForVectorIndexes,
  serverVectorIndexPayload,
  vectorIndexChanged,
  vectorIndexFingerprint,
  vectorIndexRecordFromServer,
} from './vectorIndexSync'

describe('vectorIndexSync converters', () => {
  it('round-trips a record with members and history', () => {
    const server: ServerVectorIndex = {
      id: 'vector-index-1', title: 'My index', handle: 'my-index',
      model: 'text-embedding-3-large', dims: 3072, status: 'ready',
      server_collection_id: 'kc_9',
      server_collection_model: 'text-embedding-3-large', last_error: null,
      members: [
        { file_id: 'fa_1', state: 'embedded' },
        { file_id: 'fa_2', state: 'pending' },
      ],
      history: [
        { result: 'ok', documents: 2, duration_ms: 1500, error: null, started_at: 1, finished_at: 2.5 },
        { result: 'error', documents: 0, duration_ms: 9, error: 'boom', started_at: 0, finished_at: 0.4 },
      ],
      created_at: 1_699_000_000, updated_at: 1_700_000_000,
    }
    const record = vectorIndexRecordFromServer(server)
    expect(record.serverCollectionId).toBe('kc_9')
    expect(record.serverCollectionModel).toBe('text-embedding-3-large')
    expect(record.members).toEqual([
      { fileId: 'fa_1', state: 'embedded' },
      { fileId: 'fa_2', state: 'pending' },
    ])
    expect(record.history).toHaveLength(2)
    expect(record.history?.[0]).toMatchObject({ result: 'ok', documents: 2, durationMs: 1500 })
    expect(record.history?.[1]).toMatchObject({ result: 'error', error: 'boom' })

    const payload = serverVectorIndexPayload(record)
    expect(payload.server_collection_id).toBe('kc_9')
    expect(payload.members).toEqual(server.members)
    expect(payload.history[0]).toMatchObject({ result: 'ok', duration_ms: 1500, started_at: 1, finished_at: 2.5 })
    expect(payload.created_at).toBe(server.created_at)
    expect(payload.updated_at).toBe(server.updated_at)
  })

  it('round-trips serverDocumentId and the terminal skipped state on members', () => {
    const server: ServerVectorIndex = {
      id: 'vector-index-9', title: 't', handle: 'h',
      model: 'text-embedding-3-large', dims: 3072, status: 'ready',
      server_collection_id: 'kc_9', server_collection_model: 'text-embedding-3-large',
      last_error: null,
      members: [
        { file_id: 'fa_1', state: 'embedded', server_document_id: 'kd_1' },
        { file_id: 'fa_2', state: 'skipped' },
      ],
      history: [],
      created_at: 1, updated_at: 1,
    }
    const record = vectorIndexRecordFromServer(server)
    // The backend doc id survives so "remove from index" stays exact after reload.
    expect(record.members[0]).toEqual({ fileId: 'fa_1', state: 'embedded', serverDocumentId: 'kd_1' })
    // A no-text member keeps its terminal 'skipped' state (not collapsed to pending).
    expect(record.members[1]).toEqual({ fileId: 'fa_2', state: 'skipped' })

    const payload = serverVectorIndexPayload(record)
    expect(payload.members[0]).toMatchObject({ file_id: 'fa_1', state: 'embedded', server_document_id: 'kd_1' })
    expect(payload.members[1]).toMatchObject({ file_id: 'fa_2', state: 'skipped' })
    // No tracked id -> the key is omitted, never sent as null.
    expect('server_document_id' in payload.members[1]).toBe(false)
  })

  it('omits an empty history array on the record', () => {
    const record = vectorIndexRecordFromServer({
      id: 'vector-index-2', title: 't', handle: 'h', model: 'm', dims: 1, status: 'stale',
      server_collection_id: null, server_collection_model: null,
      last_error: null, members: [], history: [],
      created_at: 1, updated_at: 1,
    })
    expect('history' in record).toBe(false)
    expect(serverVectorIndexPayload(record).history).toEqual([])
    expect(serverVectorIndexPayload(record).server_collection_id).toBe(null)
  })

  it('normalizes unknown enum values', () => {
    const record = vectorIndexRecordFromServer({
      id: 'vector-index-3', title: 't', handle: 'h', model: 'm', dims: 1, status: 'weird',
      server_collection_id: null, server_collection_model: null, last_error: null,
      members: [{ file_id: 'fa_1', state: 'bogus' }],
      history: [{ result: 'nope', documents: 1, duration_ms: 1, error: null, started_at: 1, finished_at: 2 }],
      created_at: 1, updated_at: 1,
    })
    expect(record.status).toBe('stale')
    expect(record.members[0].state).toBe('pending')
    expect(record.history?.[0].result).toBe('ok')
  })
})

describe('vectorIndexChanged (defer-while-indexing)', () => {
  const base: VectorIndexRecord = {
    createdAt: '2026-01-01T00:00:00.000Z', dims: 3072, handle: 'h', id: 'vector-index-1',
    members: [], model: 'm', status: 'ready', title: 't',
    updatedAt: '2026-01-01T00:00:00.000Z',
  }

  it('pushes a new terminal record', () => {
    expect(vectorIndexChanged(undefined, vectorIndexFingerprint(base))).toBe(true)
  })

  it('does NOT push while a reindex is in flight', () => {
    const indexing = { ...base, status: 'indexing' as const, updatedAt: '2026-01-02T00:00:00.000Z' }
    // Even though updatedAt advanced (start bumps it), an indexing record is deferred.
    expect(
      vectorIndexChanged(vectorIndexFingerprint(base), vectorIndexFingerprint(indexing)),
    ).toBe(false)
  })

  it('pushes once the run reaches a terminal status', () => {
    const done = { ...base, status: 'ready' as const, updatedAt: '2026-01-03T00:00:00.000Z' }
    expect(
      vectorIndexChanged(vectorIndexFingerprint(base), vectorIndexFingerprint(done)),
    ).toBe(true)
  })

  it('does not re-push an unchanged terminal record', () => {
    expect(
      vectorIndexChanged(vectorIndexFingerprint(base), vectorIndexFingerprint(base)),
    ).toBe(false)
  })
})

describe('autosaveDelayForVectorIndexes (membership growth flushes immediately)', () => {
  const record = (id: string, memberCount: number): VectorIndexRecord => ({
    createdAt: '2026-01-01T00:00:00.000Z', dims: 3072, handle: id, id,
    members: Array.from({ length: memberCount }, (_, n) => ({
      fileId: `fa_${n}`, state: 'pending' as const,
    })),
    model: 'm', status: 'stale', title: id,
    updatedAt: '2026-01-01T00:00:00.000Z',
  })

  it('returns 0 when an index gained members', () => {
    expect(autosaveDelayForVectorIndexes(
      { i1: record('i1', 1) }, { i1: record('i1', 3) }, 1500,
    )).toBe(0)
  })

  it('returns 0 for a brand-new index that already has members', () => {
    expect(autosaveDelayForVectorIndexes({}, { i1: record('i1', 2) }, 1500)).toBe(0)
  })

  it('keeps the debounce for edits, removals, and empty new indexes', () => {
    expect(autosaveDelayForVectorIndexes(
      { i1: record('i1', 3) }, { i1: record('i1', 2) }, 1500,
    )).toBe(1500)
    expect(autosaveDelayForVectorIndexes(
      { i1: record('i1', 2) }, { i1: { ...record('i1', 2), title: 'renamed' } }, 1500,
    )).toBe(1500)
    expect(autosaveDelayForVectorIndexes({}, { i1: record('i1', 0) }, 1500)).toBe(1500)
  })
})
