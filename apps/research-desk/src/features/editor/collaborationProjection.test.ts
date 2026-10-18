import { describe, expect, it, vi } from 'vitest'

import type { EditorCollaborationProjection } from '@/api/inqtrixClient'
import type { EditorDocumentRecord } from '@/features/project/types'
import type { CollaborationLiveAuthority } from './collaborationAuthority'
import {
  collaborationProjectionController,
  confirmedProjectionFallback,
  decideCollaborationPatchesAfterBarrier,
  flushCollaborationProjectionBarrier,
  setAuthoritativeCollaborationSequence,
  type CollaborationProjectionController,
} from './collaborationProjection'

function controller(
  order: string[],
  localSequence = 7,
): CollaborationProjectionController & { setAuthoritativeSequence: ReturnType<typeof vi.fn> } {
  return {
    flushAndAwaitDurable: vi.fn(async () => {
      order.push('local')
      return localSequence
    }),
    readAuthority: () => writableAuthority(),
    setAuthoritativeSequence: vi.fn((sequence: number) => {
      order.push(`set:${sequence}`)
    }),
  }
}

function writableAuthority(
  overrides: Partial<CollaborationLiveAuthority> = {},
): CollaborationLiveAuthority {
  return {
    access: 'edit',
    blockingFailure: null,
    canEdit: true,
    connectionStatus: 'connected',
    documentId: 'doc-1',
    generation: 1,
    lifecycleStatus: 'saved',
    revision: 0,
    synced: true,
    ...overrides,
  }
}

describe('collaboration projection barrier', () => {
  it('does not register a retained A controller for requested document B', () => {
    const flushAndAwaitDurable = vi.fn(async () => 4)
    const setAuthoritativeSequence = vi.fn()
    const readAuthority = vi.fn(() => writableAuthority({ documentId: 'doc-a', generation: 2 }))
    const handle = {
      documentId: 'doc-a',
      flushAndAwaitDurable,
      generation: 2,
      lifecycleStatus: 'saved',
      readAuthority,
      setAuthoritativeSequence,
    } as never

    expect(collaborationProjectionController(handle, 'doc-b', 3)).toBeNull()
    expect(collaborationProjectionController(handle, 'doc-a', 2)).toEqual({
      flushAndAwaitDurable,
      readAuthority,
      setAuthoritativeSequence,
    })
  })

  it('drains local durability before flushing and adopts peer advancement', async () => {
    const order: string[] = []
    const local = controller(order)

    const result = await flushCollaborationProjectionBarrier({
      clientOptions: { workspaceId: 'ws-1' },
      controller: local,
      documentId: 'doc-1',
      generation: 1,
      flushProjection: vi.fn(async () => {
        order.push('server')
        return {
          authoritative_sequence: 11,
          content_markdown: '# Durable',
          generation: 1,
          projection_hash: 'hash',
          sequence: 11,
        }
      }),
      now: () => new Date('2026-07-15T10:00:00.000Z'),
    })

    expect(order).toEqual(['local', 'server', 'set:11'])
    expect(result).toEqual({
      confirmedAt: '2026-07-15T10:00:00.000Z',
      markdown: '# Durable',
      sequence: 11,
    })
  })

  it('rejects an in-flight projection from another generation before watermark adoption', async () => {
    const local = controller([], 7)
    let resolveProjection!: (projection: EditorCollaborationProjection) => void
    const projectionResponse = new Promise<EditorCollaborationProjection>((resolve) => {
      resolveProjection = resolve
    })
    const flushProjection = vi.fn(() => projectionResponse)

    const pending = flushCollaborationProjectionBarrier({
      clientOptions: {},
      controller: local,
      documentId: 'doc-1',
      generation: 1,
      flushProjection,
    })
    await vi.waitFor(() => expect(flushProjection).toHaveBeenCalledOnce())
    resolveProjection({
      authoritative_sequence: 8,
      content_markdown: '# Recreated document',
      generation: 2,
      projection_hash: 'generation-2-hash',
      sequence: 8,
    })

    await expect(pending).rejects.toMatchObject({
      code: 'projection_generation_mismatch',
    })
    expect(local.setAuthoritativeSequence).not.toHaveBeenCalled()
  })

  it('rejects a projection behind a durable local acknowledgement', async () => {
    const local = controller([], 12)

    await expect(flushCollaborationProjectionBarrier({
      clientOptions: {},
      controller: local,
      documentId: 'doc-1',
      generation: 1,
      flushProjection: async () => ({
        authoritative_sequence: 11,
        content_markdown: '# Old',
        generation: 1,
        projection_hash: 'hash',
        sequence: 11,
      }),
    })).rejects.toMatchObject({
      code: 'projection_behind_local',
    })
    expect(local.setAuthoritativeSequence).not.toHaveBeenCalled()
  })

  it('requires the browser barrier for live callers but permits inactive export flushes', async () => {
    const flushProjection = vi.fn(async () => ({
      authoritative_sequence: 4,
      content_markdown: '# Server',
      generation: 1,
      projection_hash: 'hash',
      sequence: 4,
    }))

    await expect(flushCollaborationProjectionBarrier({
      clientOptions: {},
      controller: null,
      documentId: 'doc-1',
      generation: 1,
      flushProjection,
    })).rejects.toMatchObject({ code: 'local_barrier_unavailable' })
    expect(flushProjection).not.toHaveBeenCalled()

    await expect(flushCollaborationProjectionBarrier({
      clientOptions: {},
      controller: null,
      documentId: 'doc-1',
      generation: 1,
      flushProjection,
      requireLocal: false,
    })).resolves.toMatchObject({ markdown: '# Server', sequence: 4 })
  })

  it('blocks an edit-authorized session while it is reconnecting before any barrier call', async () => {
    const flushAndAwaitDurable = vi.fn(async () => 7)
    const flushProjection = vi.fn()
    const local: CollaborationProjectionController = {
      flushAndAwaitDurable,
      readAuthority: () => writableAuthority({
        canEdit: false,
        connectionStatus: 'reconnecting',
        lifecycleStatus: 'reconnecting',
        synced: false,
      }),
      setAuthoritativeSequence: vi.fn(),
    }

    await expect(flushCollaborationProjectionBarrier({
      clientOptions: {},
      controller: local,
      documentId: 'doc-1',
      generation: 1,
      flushProjection,
    })).rejects.toThrow('reconnecting')
    expect(flushAndAwaitDurable).not.toHaveBeenCalled()
    expect(flushProjection).not.toHaveBeenCalled()
  })

  it('does not call projection or decision after authority changes during the local barrier', async () => {
    let authority = writableAuthority()
    const flushProjection = vi.fn()
    const decide = vi.fn()
    const local: CollaborationProjectionController = {
      flushAndAwaitDurable: vi.fn(async () => {
        authority = writableAuthority({
          access: 'view',
          canEdit: false,
          connectionStatus: 'read_only',
          lifecycleStatus: 'read_only',
          revision: 1,
        })
        return 7
      }),
      readAuthority: () => authority,
      setAuthoritativeSequence: vi.fn(),
    }

    await expect(decideCollaborationPatchesAfterBarrier({
      clientOptions: {},
      controller: local,
      decide,
      decision: 'accept',
      decisionId: 'decision-downgrade',
      documentId: 'doc-1',
      generation: 1,
      flushProjection,
      patchIds: ['patch-1'],
    })).rejects.toThrow('Only editors')
    expect(flushProjection).not.toHaveBeenCalled()
    expect(decide).not.toHaveBeenCalled()
    expect(local.setAuthoritativeSequence).not.toHaveBeenCalled()
  })

  it('feeds each durable command sequence into the next local watermark', () => {
    const local = controller([])

    setAuthoritativeCollaborationSequence(local, 20)
    setAuthoritativeCollaborationSequence(local, 24)

    expect(local.setAuthoritativeSequence.mock.calls).toEqual([[20], [24]])
  })

  it('rejects lagging markdown instead of relabeling it with a newer authoritative sequence', async () => {
    const order: string[] = []
    const local = controller(order, 12)
    const decide = vi.fn(async (_documentId, payload) => ({
      decision_id: payload.decision_id,
      sequence: payload.expected_sequence + 1,
      suggestion_ids: ['suggestion-1'],
    }))

    await expect(decideCollaborationPatchesAfterBarrier({
      clientOptions: {},
      controller: local,
      decide,
      decision: 'accept',
      decisionId: 'decision-1',
      documentId: 'doc-1',
      generation: 1,
      flushProjection: async () => ({
        authoritative_sequence: 15,
        content_markdown: '# Projected at eleven',
        generation: 1,
        projection_hash: 'hash',
        sequence: 11,
      }),
      patchIds: ['patch-1'],
    })).rejects.toMatchObject({ code: 'projection_not_authoritative' })

    expect(decide).not.toHaveBeenCalled()
    expect(local.setAuthoritativeSequence).not.toHaveBeenCalled()
  })

  it('does not dispatch a decision for a projection from another generation', async () => {
    const local = controller([], 7)
    const decide = vi.fn()

    await expect(decideCollaborationPatchesAfterBarrier({
      clientOptions: {},
      controller: local,
      decide,
      decision: 'accept',
      decisionId: 'decision-generation-race',
      documentId: 'doc-1',
      generation: 1,
      flushProjection: async () => ({
        authoritative_sequence: 8,
        content_markdown: '# Recreated document',
        generation: 2,
        projection_hash: 'generation-2-hash',
        sequence: 8,
      }),
      patchIds: ['patch-1'],
    })).rejects.toMatchObject({ code: 'projection_generation_mismatch' })

    expect(decide).not.toHaveBeenCalled()
    expect(local.setAuthoritativeSequence).not.toHaveBeenCalled()
  })

  it('uses an equal projected and authoritative sequence for decision CAS', async () => {
    const local = controller([], 12)
    const decide = vi.fn(async (_documentId, payload) => ({
      decision_id: payload.decision_id,
      sequence: payload.expected_sequence + 1,
      suggestion_ids: ['suggestion-1'],
    }))

    await decideCollaborationPatchesAfterBarrier({
      clientOptions: {},
      controller: local,
      decide,
      decision: 'accept',
      decisionId: 'decision-equal',
      documentId: 'doc-1',
      generation: 1,
      flushProjection: async () => ({
        authoritative_sequence: 15,
        content_markdown: '# Projected at fifteen',
        generation: 1,
        projection_hash: 'hash',
        sequence: 15,
      }),
      patchIds: ['patch-1'],
    })

    expect(decide).toHaveBeenCalledWith(
      'doc-1',
      expect.objectContaining({ expected_sequence: 15 }),
      {},
    )
    expect(local.setAuthoritativeSequence.mock.calls).toEqual([[15], [16]])
  })

  it('uses fresh exact sequences for consecutive decisions and peer advancement', async () => {
    let watermark = 5
    const local: CollaborationProjectionController = {
      flushAndAwaitDurable: vi.fn(async () => watermark),
      readAuthority: () => writableAuthority(),
      setAuthoritativeSequence: vi.fn((sequence: number) => {
        watermark = Math.max(watermark, sequence)
      }),
    }
    const serverSequences = [7, 12]
    const expectedSequences: number[] = []
    const flushProjection = vi.fn(async () => ({
      authoritative_sequence: serverSequences[0],
      content_markdown: '# Durable',
      generation: 1,
      projection_hash: 'hash',
      sequence: serverSequences.shift()!,
    }))
    const decide = vi.fn(async (_documentId, payload) => {
      expectedSequences.push(payload.expected_sequence)
      return {
        decision_id: payload.decision_id,
        sequence: payload.expected_sequence + 1,
        suggestion_ids: ['suggestion-1'],
      }
    })

    await decideCollaborationPatchesAfterBarrier({
      clientOptions: {},
      controller: local,
      decide,
      decision: 'accept',
      decisionId: 'decision-1',
      documentId: 'doc-1',
      generation: 1,
      flushProjection,
      patchIds: ['patch-1'],
    })
    expect(watermark).toBe(8)
    await decideCollaborationPatchesAfterBarrier({
      clientOptions: {},
      controller: local,
      decide,
      decision: 'reject',
      decisionId: 'decision-2',
      documentId: 'doc-1',
      generation: 1,
      flushProjection,
      patchIds: ['patch-2'],
    })

    expect(expectedSequences).toEqual([7, 12])
    expect(watermark).toBe(13)
  })

  it('does not advance the watermark for an unconfirmed decision response', async () => {
    const local = controller([], 4)

    await expect(decideCollaborationPatchesAfterBarrier({
      clientOptions: {},
      controller: local,
      decide: async () => ({
        decision_id: 'wrong-decision',
        sequence: 6,
        suggestion_ids: [],
      }),
      decision: 'accept',
      decisionId: 'decision-1',
      documentId: 'doc-1',
      generation: 1,
      flushProjection: async () => ({
        authoritative_sequence: 5,
        content_markdown: '# Durable',
        generation: 1,
        projection_hash: 'hash',
        sequence: 5,
      }),
      patchIds: ['patch-1'],
    })).rejects.toMatchObject({ code: 'command_response_invalid' })
    expect(local.setAuthoritativeSequence).toHaveBeenCalledTimes(1)
    expect(local.setAuthoritativeSequence).toHaveBeenLastCalledWith(5)
  })
})

describe('confirmed collaboration projection fallback', () => {
  const document: EditorDocumentRecord = {
    access: { mode: 'owner', permission: 'edit' },
    collaboration: {
      generation: 1,
      persistedSequence: 4,
      projectionSequence: 4,
      projectionUpdatedAt: '2026-07-15T09:30:00.000Z',
      schemaVersion: 1,
    },
    contentMarkdown: '# Confirmed',
    contentMode: 'collaboration',
    createdAt: '2026-07-15T09:00:00.000Z',
    folderId: null,
    id: 'doc-1',
    revision: 1,
    source: 'blank',
    title: 'Document',
    updatedAt: '2026-07-15T09:30:00.000Z',
  }

  it('returns only timestamped collaboration projections', () => {
    expect(confirmedProjectionFallback(document)).toEqual({
      confirmedAt: '2026-07-15T09:30:00.000Z',
      markdown: '# Confirmed',
    })
    expect(confirmedProjectionFallback({
      ...document,
      collaboration: { ...document.collaboration!, projectionUpdatedAt: undefined },
    })).toBeNull()
  })

  it('refuses an empty body even when a confirmation timestamp is present', () => {
    // A server document that was never opened in this session is hydrated from
    // metadata only and carries an empty body next to a plausible timestamp.
    // Accepting it here writes an empty file into the backup archive.
    expect(confirmedProjectionFallback({
      ...document,
      contentMarkdown: '',
    })).toBeNull()
  })
})
