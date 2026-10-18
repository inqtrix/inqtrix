import { Buffer } from 'node:buffer'

import { FastApiCollaborationClient } from '../src/apiClient'
import { SidecarMetrics } from '../src/metrics'
import { settings, silentLogger, USER_ID } from './helpers'

const PATCH_ID = '22222222-2222-4222-8222-222222222222'
const SUGGESTION_ID = '33333333-3333-4333-8333-333333333333'

describe('FastAPI collaboration client contracts', () => {
  it('sends the suggestions a direct edit rejected, with their patches and no descriptors', async () => {
    let requestInit: RequestInit | undefined
    const fetchImplementation: typeof fetch = async (_input, init) => {
      requestInit = init
      return new Response(JSON.stringify({
        duplicate: false,
        persisted_sequence: 9,
        sequence: 9,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    const client = new FastApiCollaborationClient(
      settings(),
      silentLogger,
      new SidecarMetrics(),
      fetchImplementation,
    )
    const reviewer = '55555555-5555-4555-8555-555555555555'

    await client.persistUpdate({
      actorKind: 'guest',
      actorUserId: USER_ID,
      changeKind: 'direct',
      changeSummary: { changesDocument: true, edits: [], omittedEditCount: 0 },
      commandPayloadHash: null,
      decision: null,
      decisionOutcome: null,
      documentId: 'ed_test',
      fence: { epoch: 7, instanceId: 'test-instance', leaseExpiresAt: 999 },
      generation: 2,
      hash: 'a'.repeat(64),
      leaseId: '66666666-6666-4666-8666-666666666666',
      patches: [{
        activeSuggestionIds: [],
        authorId: reviewer,
        createdAt: 1_784_112_000,
        kinds: [],
        patchId: PATCH_ID,
        supersededSuggestionIds: [],
      }],
      rejectedSuggestionIds: [SUGGESTION_ID],
      suggestions: [],
      suggestionIds: [],
      update: new Uint8Array([1, 2, 3]),
    })

    const body = JSON.parse(String(requestInit?.body)) as Record<string, unknown>
    expect(body).toMatchObject({
      actor_kind: 'guest',
      change_kind: 'direct',
      patches: [{ active_suggestion_ids: [], author_id: reviewer, patch_id: PATCH_ID }],
      rejected_suggestion_ids: [SUGGESTION_ID],
      suggestion_ids: [],
      suggestions: [],
    })
  })

  it.each([true, false])('sends whether a change altered the document a reader sees (%s)', async (changesDocument) => {
    let requestInit: RequestInit | undefined
    const fetchImplementation: typeof fetch = async (_input, init) => {
      requestInit = init
      return new Response(JSON.stringify({
        duplicate: false,
        persisted_sequence: 3,
        sequence: 3,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    const client = new FastApiCollaborationClient(settings(), silentLogger, new SidecarMetrics(), fetchImplementation)

    await client.persistUpdate({
      actorKind: 'human',
      actorUserId: USER_ID,
      changeKind: 'direct',
      changeSummary: { changesDocument, edits: [], omittedEditCount: changesDocument ? 1 : 0 },
      commandPayloadHash: null,
      decision: null,
      decisionOutcome: null,
      documentId: 'ed_test',
      fence: { epoch: 7, instanceId: 'test-instance', leaseExpiresAt: 999 },
      generation: 2,
      hash: 'a'.repeat(64),
      leaseId: '66666666-6666-4666-8666-666666666666',
      patches: [],
      rejectedSuggestionIds: [],
      suggestions: [],
      suggestionIds: [],
      update: new Uint8Array([1, 2, 3]),
    })

    const body = JSON.parse(String(requestInit?.body)) as Record<string, unknown>
    expect(body.change_summary).toEqual({
      changes_document: changesDocument,
      edits: [],
      omitted_edit_count: changesDocument ? 1 : 0,
    })
  })

  it('serializes decision CAS and preserves duplicate original/current sequences', async () => {
    let requestUrl = ''
    let requestInit: RequestInit | undefined
    const fetchImplementation: typeof fetch = async (input, init) => {
      requestUrl = String(input)
      requestInit = init
      return new Response(JSON.stringify({
        duplicate: true,
        persisted_sequence: 42,
        sequence: 7,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } })
    }
    const configured = settings()
    const client = new FastApiCollaborationClient(
      configured,
      silentLogger,
      new SidecarMetrics(),
      fetchImplementation,
    )

    const result = await client.persistUpdate({
      actorKind: 'human',
      actorUserId: USER_ID,
      changeKind: 'decision',
      changeSummary: {
        changesDocument: true,
        edits: [{ after: 'new', before: 'old', kind: 'replacement', position: 4 }],
        omittedEditCount: 0,
      },
      commandId: '44444444-4444-4444-8444-444444444444',
      commandPayloadHash: 'b'.repeat(64),
      decision: 'accept',
      decisionOutcome: 'accepted',
      documentId: 'ed_test',
      expectedSequence: 41,
      fence: { epoch: 7, instanceId: 'test-instance', leaseExpiresAt: 999 },
      generation: 2,
      hash: 'a'.repeat(64),
      leaseId: null,
      patches: [{
        activeSuggestionIds: [],
        authorId: USER_ID,
        createdAt: 1_784_112_000,
        kinds: [],
        patchId: PATCH_ID,
        supersededSuggestionIds: [],
      }],
      rejectedSuggestionIds: [],
      suggestions: [{
        authorId: USER_ID,
        createdAt: 1_784_112_000,
        kind: 'insertion',
        patchId: PATCH_ID,
        suggestionId: SUGGESTION_ID,
      }],
      suggestionIds: [SUGGESTION_ID],
      update: new Uint8Array([1, 2, 3]),
    })

    expect(result).toEqual({
      duplicate: true,
      persistedSequence: 42,
      sequence: 7,
    })

    expect(requestUrl).toBe(
      'http://fastapi.internal/internal/collaboration/documents/ed_test/updates',
    )
    expect(requestInit?.headers).toMatchObject({
      Authorization: `Bearer ${configured.secret}`,
      'Content-Type': 'application/json',
    })
    expect(JSON.parse(String(requestInit?.body))).toEqual({
      actor_kind: 'human',
      actor_user_id: USER_ID,
      change_kind: 'decision',
      change_summary: {
        changes_document: true,
        edits: [{
          after: 'new',
          before: 'old',
          kind: 'replacement',
          position: 4,
        }],
        omitted_edit_count: 0,
      },
      command_id: '44444444-4444-4444-8444-444444444444',
      command_payload_hash: 'b'.repeat(64),
      decision: 'accept',
      decision_outcome: 'accepted',
      epoch: 7,
      expected_sequence: 41,
      generation: 2,
      instance_id: 'test-instance',
      lease_id: null,
      patches: [{
        active_suggestion_ids: [],
        author_id: USER_ID,
        created_at: 1_784_112_000,
        kinds: [],
        patch_id: PATCH_ID,
        superseded_suggestion_ids: [],
      }],
      rejected_suggestion_ids: [],
      suggestion_ids: [SUGGESTION_ID],
      suggestions: [{
        author_id: USER_ID,
        created_at: 1_784_112_000,
        kind: 'insertion',
        patch_id: PATCH_ID,
        suggestion_id: SUGGESTION_ID,
      }],
      tenant_id: configured.tenantId,
      update_base64: 'AQID',
      update_hash: 'a'.repeat(64),
    })
  })

  it('consumes the exact global policy feed cursor contract', async () => {
    let requested = ''
    const client = new FastApiCollaborationClient(
      settings(),
      silentLogger,
      new SidecarMetrics(),
      async (input) => {
        requested = String(input)
        return new Response(JSON.stringify({
          cursor: 12,
          events: [{
            id: 12,
            resource_id: 'ed_test',
            resource_type: 'editor_document',
            scope: 'share:revoked',
            target_user_id: USER_ID,
          }],
          reset_required: false,
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      },
    )

    await expect(client.pollPolicyEvents({
      afterId: 11,
      fence: { epoch: 1, instanceId: 'test-instance', leaseExpiresAt: 999 },
      limit: 500,
    })).resolves.toEqual({
      cursor: 12,
      events: [{
        id: 12,
        resourceId: 'ed_test',
        resourceType: 'editor_document',
        scope: 'share:revoked',
        targetUserId: USER_ID,
      }],
      resetRequired: false,
    })
    expect(requested).toBe(
      'http://fastapi.internal/internal/collaboration/policy-events?after_id=11&limit=500&tenant_id=tenant-1',
    )
  })

  it('carries the policy cursor captured by lease introspection', async () => {
    const configured = settings()
    let includePolicyCursor = true
    const client = new FastApiCollaborationClient(
      configured,
      silentLogger,
      new SidecarMetrics(),
      async () => new Response(JSON.stringify({
        document_id: 'ed_test',
        expires_at: 1_900_000_000,
        generation: 1,
        lease_id: 'lease-1',
        permission: 'edit',
        protocol_version: configured.protocolVersion,
        schema_hash: 'a'.repeat(64),
        schema_version: configured.schemaVersion,
        session_id: 'session-1',
        tenant_id: configured.tenantId,
        user: {
          color: '#2563EB',
          id: USER_ID,
          name: 'Ada',
        },
        valid: true,
        ...(includePolicyCursor ? { policy_cursor: 41 } : {}),
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )

    await expect(client.introspectLease({
      fence: { epoch: 1, instanceId: 'test-instance', leaseExpiresAt: 999 },
      room: 'editor:ed_test:1',
      token: 'lease-token',
    })).resolves.toMatchObject({
      policyCursor: 41,
    })

    includePolicyCursor = false
    await expect(client.introspectLease({
      fence: { epoch: 1, instanceId: 'test-instance', leaseExpiresAt: 999 },
      room: 'editor:ed_test:1',
      token: 'lease-token',
    })).resolves.toMatchObject({
      policyCursor: 0,
    })
  })

  it.each(['comment', 'owner'])(
    'refuses a lease introspection that names the unknown access %s',
    async (permission) => {
      // The service knows edit, suggest and view access only; a comment-only
      // principal holds a view lease. Any other answer is refused.
      const configured = settings()
      const client = new FastApiCollaborationClient(
        configured,
        silentLogger,
        new SidecarMetrics(),
        async () => new Response(JSON.stringify({
          document_id: 'ed_test',
          expires_at: 1_900_000_000,
          generation: 1,
          lease_id: 'lease-1',
          permission,
          policy_cursor: 0,
          protocol_version: configured.protocolVersion,
          schema_hash: 'a'.repeat(64),
          schema_version: configured.schemaVersion,
          session_id: 'session-1',
          tenant_id: configured.tenantId,
          user: {
            color: '#2563EB',
            id: USER_ID,
            name: 'Ada',
          },
          valid: true,
        }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
      )

      await expect(client.introspectLease({
        fence: { epoch: 1, instanceId: 'test-instance', leaseExpiresAt: 999 },
        room: 'editor:ed_test:1',
        token: 'lease-token',
      })).rejects.toMatchObject({
        reason: 'invalid_internal_response',
        status: 503,
      })
    },
  )

  it('looks up a durable command before mutating current document state', async () => {
    let body: unknown
    const client = new FastApiCollaborationClient(
      settings(),
      silentLogger,
      new SidecarMetrics(),
      async (_input, init) => {
        body = JSON.parse(String(init?.body))
        return new Response(JSON.stringify({
          actor_kind: 'assistant',
          actor_user_id: USER_ID,
          change_kind: 'suggestion',
          command_id: '44444444-4444-4444-8444-444444444444',
          command_payload_hash: 'b'.repeat(64),
          decision: null,
          found: true,
          generation: 1,
          patch_ids: [PATCH_ID],
          sequence: 9,
          suggestion_ids: [SUGGESTION_ID],
          update_hash: 'a'.repeat(64),
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      },
    )

    await expect(client.lookupCommand({
      commandId: '44444444-4444-4444-8444-444444444444',
      commandPayloadHash: 'b'.repeat(64),
      documentId: 'ed_test',
      fence: { epoch: 7, instanceId: 'test-instance', leaseExpiresAt: 999 },
      generation: 1,
    })).resolves.toMatchObject({
      commandId: '44444444-4444-4444-8444-444444444444',
      patchIds: [PATCH_ID],
      sequence: 9,
      suggestionIds: [SUGGESTION_ID],
    })
    expect(body).toEqual({
      command_id: '44444444-4444-4444-8444-444444444444',
      command_payload_hash: 'b'.repeat(64),
      epoch: 7,
      generation: 1,
      instance_id: 'test-instance',
      tenant_id: 'tenant-1',
    })
  })

  it('stores the bounded projection atomically with a snapshot', async () => {
    let body: unknown
    const client = new FastApiCollaborationClient(
      settings(),
      silentLogger,
      new SidecarMetrics(),
      async (_input, init) => {
        body = JSON.parse(String(init?.body))
        return new Response(null, { status: 204 })
      },
    )

    await client.storeSnapshot({
      coveredSequence: 8,
      documentId: 'ed_test',
      fence: { epoch: 7, instanceId: 'test-instance', leaseExpiresAt: 999 },
      generation: 1,
      projectionHash: 'a'.repeat(64),
      projectionMarkdown: '# Durable projection\n',
      schemaHash: 'b'.repeat(64),
      schemaVersion: 1,
      stateHash: 'c'.repeat(64),
      stateUpdate: new Uint8Array([1, 2]),
      stateVector: new Uint8Array([3, 4]),
    })

    expect(body).toEqual({
      covered_sequence: 8,
      epoch: 7,
      generation: 1,
      instance_id: 'test-instance',
      projection_hash: 'a'.repeat(64),
      projection_markdown: '# Durable projection\n',
      schema_hash: 'b'.repeat(64),
      schema_version: 1,
      state_hash: 'c'.repeat(64),
      state_update_base64: 'AQI=',
      state_vector_base64: 'AwQ=',
      tenant_id: 'tenant-1',
    })
  })

  it('calls fenced maintenance with the exact prune-count contract', async () => {
    let requested = ''
    let body: unknown
    const client = new FastApiCollaborationClient(
      settings(),
      silentLogger,
      new SidecarMetrics(),
      async (input, init) => {
        requested = String(input)
        body = JSON.parse(String(init?.body))
        return new Response(JSON.stringify({
          metadata_pruned: 3,
          payloads_pruned: 7,
          tombstones_purged: 2,
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      },
    )

    await expect(client.compactMaintenance({
      documentId: 'ed_test',
      fence: { epoch: 7, instanceId: 'test-instance', leaseExpiresAt: 999 },
      generation: 1,
    })).resolves.toEqual({
      metadataPruned: 3,
      payloadsPruned: 7,
      tombstonesPurged: 2,
    })
    expect(requested).toBe(
      'http://fastapi.internal/internal/collaboration/maintenance:compact',
    )
    expect(body).toEqual({
      document_id: 'ed_test',
      epoch: 7,
      generation: 1,
      instance_id: 'test-instance',
      tenant_id: 'tenant-1',
    })
  })

  it('parses two ordered snapshot candidates with their corresponding tails', async () => {
    let requested = ''
    const client = new FastApiCollaborationClient(
      settings(),
      silentLogger,
      new SidecarMetrics(),
      async (input) => {
        requested = String(input)
        const snapshot = {
          covered_sequence: 2,
          state_hash: 'a'.repeat(64),
          state_update_base64: 'AQ==',
          state_vector_base64: 'Ag==',
        }
        return new Response(JSON.stringify({
          document_id: 'ed_test',
          generation: 1,
          persisted_sequence: 2,
          schema_hash: 'b'.repeat(64),
          schema_version: 1,
          snapshot,
          snapshot_candidates: [
            { ...snapshot, updates: [] },
            {
              covered_sequence: 1,
              state_hash: 'c'.repeat(64),
              state_update_base64: 'Aw==',
              state_vector_base64: 'BA==',
              updates: [{
                sequence: 2,
                update_base64: 'BQ==',
                update_hash: 'd'.repeat(64),
              }],
            },
          ],
          updates: [],
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      },
    )

    await expect(client.loadDocumentState({
      documentId: 'ed_test',
      fence: { epoch: 7, instanceId: 'test-instance', leaseExpiresAt: 999 },
      generation: 1,
    })).resolves.toMatchObject({
      snapshotCandidates: [
        { snapshot: { coveredSequence: 2 }, updates: [] },
        {
          snapshot: { coveredSequence: 1 },
          updates: [{ hash: 'd'.repeat(64), sequence: 2 }],
        },
      ],
    })
    expect(requested).toBe(
      'http://fastapi.internal/internal/collaboration/documents/ed_test/state?epoch=7&generation=1&instance_id=test-instance&tenant_id=tenant-1',
    )
  })

  it('loads snapshots larger than the generic internal string limit', async () => {
    const stateUpdate = new Uint8Array(728).fill(0x5a)
    const encodedState = Buffer.from(stateUpdate).toString('base64')
    expect(encodedState.length).toBeGreaterThan(512)
    const snapshot = {
      covered_sequence: 0,
      state_hash: 'a'.repeat(64),
      state_update_base64: encodedState,
      state_vector_base64: 'AQ==',
    }
    const client = new FastApiCollaborationClient(
      settings(),
      silentLogger,
      new SidecarMetrics(),
      async () => new Response(JSON.stringify({
        document_id: 'ed_test',
        generation: 1,
        persisted_sequence: 0,
        schema_hash: 'b'.repeat(64),
        schema_version: 1,
        snapshot,
        snapshot_candidates: [{ ...snapshot, updates: [] }],
        updates: [],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )

    await expect(client.loadDocumentState({
      documentId: 'ed_test',
      fence: { epoch: 7, instanceId: 'test-instance', leaseExpiresAt: 999 },
      generation: 1,
    })).resolves.toMatchObject({
      snapshot: {
        stateUpdate,
      },
      snapshotCandidates: [{
        snapshot: {
          stateUpdate,
        },
      }],
    })
  })

  it('rejects snapshot payloads beyond the configured document limit', async () => {
    const oversizedState = Buffer.from(new Uint8Array(9).fill(0x5a)).toString('base64')
    const snapshot = {
      covered_sequence: 0,
      state_hash: 'a'.repeat(64),
      state_update_base64: oversizedState,
      state_vector_base64: 'AQ==',
    }
    const client = new FastApiCollaborationClient(
      settings({ documentLimitBytes: 8 }),
      silentLogger,
      new SidecarMetrics(),
      async () => new Response(JSON.stringify({
        document_id: 'ed_test',
        generation: 1,
        persisted_sequence: 0,
        schema_hash: 'b'.repeat(64),
        schema_version: 1,
        snapshot,
        updates: [],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )

    await expect(client.loadDocumentState({
      documentId: 'ed_test',
      fence: { epoch: 7, instanceId: 'test-instance', leaseExpiresAt: 999 },
      generation: 1,
    })).rejects.toMatchObject({
      reason: 'invalid_snapshot_state_update_base64',
      status: 503,
    })
  })

  it('rejects a loaded tail that omits its authoritative update hash', async () => {
    const client = new FastApiCollaborationClient(
      settings(),
      silentLogger,
      new SidecarMetrics(),
      async () => new Response(JSON.stringify({
        document_id: 'ed_test',
        generation: 1,
        persisted_sequence: 1,
        schema_hash: 'b'.repeat(64),
        schema_version: 1,
        snapshot: null,
        updates: [{ sequence: 1, update_base64: 'AQ==' }],
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    )

    await expect(client.loadDocumentState({
      documentId: 'ed_test',
      fence: { epoch: 7, instanceId: 'test-instance', leaseExpiresAt: 999 },
      generation: 1,
    })).rejects.toMatchObject({
      reason: 'invalid_internal_response',
      status: 503,
    })
  })

  it('looks up only requested durable hashes under the configured tenant', async () => {
    let body: unknown
    const first = 'a'.repeat(64)
    const missing = 'b'.repeat(64)
    const client = new FastApiCollaborationClient(
      settings(),
      silentLogger,
      new SidecarMetrics(),
      async (_input, init) => {
        body = JSON.parse(String(init?.body))
        return new Response(JSON.stringify({
          updates: [{ hash: first, sequence: 9 }],
        }), { status: 200, headers: { 'Content-Type': 'application/json' } })
      },
    )

    await expect(client.lookupUpdates({
      documentId: 'ed_test',
      fence: { epoch: 7, instanceId: 'test-instance', leaseExpiresAt: 999 },
      generation: 1,
      hashes: [first, missing],
    })).resolves.toEqual([{ hash: first, sequence: 9 }])
    expect(body).toEqual({
      epoch: 7,
      generation: 1,
      hashes: [first, missing],
      instance_id: 'test-instance',
      tenant_id: 'tenant-1',
    })
  })

  it.each([
    ['an unrequested hash', [{ hash: 'c'.repeat(64), sequence: 9 }]],
    ['a duplicate hash', [
      { hash: 'a'.repeat(64), sequence: 9 },
      { hash: 'a'.repeat(64), sequence: 9 },
    ]],
  ])('rejects %s in a durable lookup response', async (_label, updates) => {
    const client = new FastApiCollaborationClient(
      settings(),
      silentLogger,
      new SidecarMetrics(),
      async () => new Response(JSON.stringify({ updates }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    await expect(client.lookupUpdates({
      documentId: 'ed_test',
      fence: { epoch: 7, instanceId: 'test-instance', leaseExpiresAt: 999 },
      generation: 1,
      hashes: ['a'.repeat(64), 'b'.repeat(64)],
    })).rejects.toMatchObject({
      reason: 'invalid_internal_response',
      status: 503,
    })
  })
})
