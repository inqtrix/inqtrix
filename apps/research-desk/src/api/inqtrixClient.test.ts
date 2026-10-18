import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  acceptAgentMemoryCandidate,
  clearAgentMemories,
  createChatCompletion,
  createResearchRun,
  createEditorCollaborationSession,
  createShares,
  decideEditorCollaborationPatches,
  deleteEditorCommentSuggestionDraft,
  deleteAgentMemory,
  enableEditorDocumentCollaboration,
  fetchAuthSession,
  fetchMyShares,
  fetchServerFileInfo,
  fetchSharingInbox,
  flushEditorCollaborationProjection,
  getEditorAccessSummary,
  listAgentMemories,
  listAgentMemoryCandidates,
  listAgentMemoryFeedback,
  listAssetDeletionOperations,
  listEditorCollaborationActivity,
  listEditorDocuments,
  listKnowledgeDocuments,
  listUploadOperations,
  logoutSession,
  markGuestEditorCollaborationCommentsRead,
  patchEditorDocumentMetadata,
  publishEditorCollaborationSuggestion,
  rejectAgentMemoryCandidate,
  reserveServerFileUpload,
  resolveKnowledgeDocumentBySource,
  retryUploadOperation,
  resumeIndexingJob,
  resumeIndexingJobWithoutContext,
  searchKnowledge,
  saveEditorCommentSuggestionDraft,
  setExpectedUserIdentity,
  submitAgentRunFeedback,
  streamChatCompletion,
  streamServerSentEvents,
  streamUserEvents,
  updateAgentMemory,
  updatePromptTemplate,
  updateShare,
  updateSkill,
  uploadServerFile,
} from './inqtrixClient'

describe('knowledge evidence wire contract', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('preserves the original-source excerpt and never expects retrieval text', async () => {
    const hit = {
      chunk_id: 'kch_1',
      chunk_index: 2,
      collection_id: 'kc_1',
      document_id: 'kd_1',
      document_title: 'Vertrag.pdf',
      excerpt: 'Die Haftung ist begrenzt.',
      generation_id: 'gen_1',
      page_number: 4,
      provenance_status: 'verified_span',
      rank: 1,
      reference_id: 'K1',
      revision_id: 'rev_1',
      score: 0.91,
      source_span: {
        document_content_hash: 'abc123',
        end: 28,
        offset_unit: 'utf8_byte',
        start: 0,
      },
    }
    const warnings = [
      {
        candidate_cap: 64,
        code: 'vector_overfetch_cap',
        final_evidence_complete: true,
        final_top_k: 1,
        message: 'candidate pool limited; final evidence complete',
        reason: 'vector_overfetch_cap',
        requested_candidate_pool: 10,
        requested_top_k: 1,
        retrieval_mode: 'hybrid',
        returned_candidate_pool: 4,
        returned_hits: 1,
        stage: 'vector_candidate_pool',
      },
      {
        candidate_cap: 64,
        code: 'vector_overfetch_cap',
        final_evidence_complete: false,
        final_top_k: 5,
        message: 'candidate pool limited; final evidence incomplete',
        reason: 'vector_overfetch_cap',
        requested_candidate_pool: 50,
        requested_top_k: 5,
        retrieval_mode: 'hybrid',
        returned_candidate_pool: 4,
        returned_hits: 4,
        stage: 'vector_candidate_pool',
      },
      {
        candidate_cap: null,
        code: 'vector_candidate_stalled',
        final_evidence_complete: true,
        final_top_k: 1,
        message: 'candidate pool stalled; final evidence complete',
        reason: 'vector_candidate_stalled',
        requested_candidate_pool: 10,
        requested_top_k: 1,
        retrieval_mode: 'dense',
        returned_candidate_pool: 4,
        returned_hits: 1,
        stage: 'vector_candidate_pool',
      },
      {
        candidate_cap: null,
        code: 'vector_candidate_stalled',
        final_evidence_complete: false,
        final_top_k: 5,
        message: 'candidate pool stalled; final evidence incomplete',
        reason: 'vector_candidate_stalled',
        requested_candidate_pool: 50,
        requested_top_k: 5,
        retrieval_mode: 'dense',
        returned_candidate_pool: 4,
        returned_hits: 4,
        stage: 'vector_candidate_pool',
      },
    ]
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse({ data: [hit], warnings })))

    const results = await searchKnowledge(
      { collectionIds: ['kc_1'], query: 'Haftung', topK: 5 },
      { baseUrl: 'http://api.test' },
    )

    expect(results).toEqual({ data: [hit], warnings })
    expect(results.data[0]).not.toHaveProperty('text')
  })

  it('resumes a paused indexing job through the explicit server action', async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) => {
      void _input
      void _init
      return jsonResponse({
        collection_id: 'kc_1',
        job_id: 'ix_1',
        status: 'queued',
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    await resumeIndexingJob('ix_1', {
      baseUrl: 'http://api.test',
      workspaceId: 'workspace-1',
    })

    expect(String(fetchMock.mock.calls[0]?.[0])).toContain(
      '/v1/knowledge/indexing-jobs/ix_1/resume',
    )
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('POST')
  })

  it('rebuilds a paused indexing generation raw only by explicit action', async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) => {
      void _input
      void _init
      return jsonResponse({
        collection_id: 'kc_1',
        job_id: 'ix_1',
        status: 'queued',
      })
    })
    vi.stubGlobal('fetch', fetchMock)

    await resumeIndexingJobWithoutContext('ix_1', {
      baseUrl: 'http://api.test',
      workspaceId: 'workspace-1',
    })

    expect(String(fetchMock.mock.calls[0]?.[0])).toContain(
      '/v1/knowledge/indexing-jobs/ix_1/resume-raw',
    )
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('POST')
  })
})

describe('private editor suggestion draft client', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('uses the nested creator-private draft endpoint with explicit revision guards', async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      init?: RequestInit,
    ) => init?.method === 'DELETE'
      ? new Response(null, { status: 204 })
      : jsonResponse({ suggestion_draft: {} }))
    vi.stubGlobal('fetch', fetchMock)
    const options = { baseUrl: 'http://api.test' }
    const patchId = '00000000-0000-4000-8000-000000000003'

    await saveEditorCommentSuggestionDraft(
      'doc-1',
      'comment-1',
      {
        draft: {
          anchor_version: 1,
          change_summary: [],
          evidence: null,
          group_id: 'group-1',
          patch_id: patchId,
          proposed_text: 'Private provider result',
          publication_command_id: '00000000-0000-4000-8000-000000000002',
          suggestion_id: 'suggestion-1',
          warnings: [],
        },
        expected_revision: 0,
      },
      options,
    )
    await deleteEditorCommentSuggestionDraft(
      'doc-1',
      'comment-1',
      { expected_revision: 1, patch_id: patchId },
      options,
    )

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://api.test/v1/editor/documents/doc-1/comments/comment-1/suggestion-draft',
      expect.objectContaining({
        body: JSON.stringify({
          draft: {
            anchor_version: 1,
            change_summary: [],
            evidence: null,
            group_id: 'group-1',
            patch_id: patchId,
            proposed_text: 'Private provider result',
            publication_command_id: '00000000-0000-4000-8000-000000000002',
            suggestion_id: 'suggestion-1',
            warnings: [],
          },
          expected_revision: 0,
        }),
        method: 'PUT',
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'http://api.test/v1/editor/documents/doc-1/comments/comment-1/suggestion-draft',
      expect.objectContaining({
        body: JSON.stringify({ expected_revision: 1, patch_id: patchId }),
        method: 'DELETE',
      }),
    )
  })
})

describe('browser principal generation', () => {
  afterEach(() => {
    setExpectedUserIdentity(null)
    vi.unstubAllGlobals()
  })

  it('binds API requests to the authenticated user rendered by the SPA', async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) => {
      void _input
      void _init
      return jsonResponse({ id: 'file-1' })
    })
    vi.stubGlobal('fetch', fetchMock)
    setExpectedUserIdentity('00000000-0000-4000-8000-000000000001')

    await fetchServerFileInfo('file-1', { baseUrl: 'http://api.test' })

    const headers = fetchMock.mock.calls[0][1]?.headers as Headers
    expect(headers.get('X-Inqtrix-Expected-User-Id')).toBe(
      '00000000-0000-4000-8000-000000000001',
    )
  })

  it('surfaces the stable principal-changed response to non-browser callers', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      {
        error: {
          message: 'session changed',
          type: 'principal_changed',
        },
      },
      409,
    )))
    setExpectedUserIdentity('00000000-0000-4000-8000-000000000001')

    await expect(
      fetchServerFileInfo('file-1', { baseUrl: 'http://api.test' }),
    ).rejects.toMatchObject({ name: 'principal_changed', status: 409 })
  })

  it('leaves an expired collaboration lease to the controller recovery flow', async () => {
    const reload = vi.fn()
    vi.stubGlobal('window', { location: { reload } })
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      {
        error: {
          message: 'collaboration lease expired',
          type: 'authentication_error',
        },
      },
      401,
    )))
    setExpectedUserIdentity('00000000-0000-4000-8000-000000000001')

    await expect(createEditorCollaborationSession(
      'doc-1',
      {
        lease_token: 'expired-private-token',
        protocol_version: 1,
        rotation_command_id: '00000000-0000-4000-8000-000000000004',
        schema_version: 1,
      },
      { baseUrl: 'http://api.test' },
    )).rejects.toMatchObject({ status: 401 })

    expect(reload).not.toHaveBeenCalled()
  })

  it('leaves an expired research submit to a visible caller recovery flow', async () => {
    const reload = vi.fn()
    vi.stubGlobal('window', { location: { reload } })
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(
      {
        error: {
          message: 'session expired',
          type: 'authentication_error',
        },
      },
      401,
    )))
    setExpectedUserIdentity('00000000-0000-4000-8000-000000000001')

    await expect(createResearchRun(
      { mode: 'research', question: 'Retain this draft' },
      { baseUrl: 'http://api.test', reloadOnUnauthorized: false },
    )).rejects.toMatchObject({ status: 401 })

    expect(reload).not.toHaveBeenCalled()
  })
})

describe('cookie-session CSRF recovery', () => {
  beforeEach(() => {
    setExpectedUserIdentity('user-1')
  })

  afterEach(() => {
    setExpectedUserIdentity(null)
    vi.unstubAllGlobals()
  })

  it('uses the authenticated session bootstrap token for the first mutation', async () => {
    vi.stubGlobal('document', { cookie: '' })
    const mutationHeaders: Array<string | null> = []
    const fetchMock = vi.fn(async (
      input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
      if (String(input).endsWith('/api/auth/session')) {
        return jsonResponse({
          authenticated: true,
          csrf_token: 'fresh-token',
          user: { display_name: 'Ada', email: 'ada@example.de', id: 'user-1', role: 'user' },
        })
      }
      const token = new Headers(init?.headers).get('X-CSRF-Token')
      mutationHeaders.push(token)
      return token === 'fresh-token'
        ? jsonResponse({ id: 'one', revision: 2 })
        : jsonResponse({
          detail: { error: { message: 'CSRF failed', type: 'csrf_error' } },
        }, 403)
    })
    vi.stubGlobal('fetch', fetchMock)

    await fetchAuthSession({ baseUrl: 'http://api.test' })
    await updatePromptTemplate(
      'one',
      {
        category: null,
        content_markdown: 'a',
        expected_revision: 1,
        include_in_autocomplete: true,
        label: 'one',
        title: 'One',
        visibility: { agent: false, chat: true, editor: false },
      },
      { baseUrl: 'http://api.test' },
    )

    expect(mutationHeaders).toEqual(['fresh-token'])
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('clears the in-memory token when the session bootstrap becomes anonymous', async () => {
    vi.stubGlobal('document', { cookie: '' })
    let sessionCalls = 0
    const mutationHeaders: Array<string | null> = []
    const fetchMock = vi.fn(async (
      input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
      if (String(input).endsWith('/api/auth/session')) {
        sessionCalls += 1
        return sessionCalls === 1
          ? jsonResponse({
            authenticated: true,
            csrf_token: 'fresh-token',
            user: { display_name: 'Ada', email: 'ada@example.de', id: 'user-1', role: 'user' },
          })
          : jsonResponse({ authenticated: false })
      }
      mutationHeaders.push(new Headers(init?.headers).get('X-CSRF-Token'))
      return jsonResponse({
        error: { message: 'Forbidden', type: 'authorization_error' },
      }, 403)
    })
    vi.stubGlobal('fetch', fetchMock)

    await fetchAuthSession({ baseUrl: 'http://api.test' })
    await fetchAuthSession({ baseUrl: 'http://api.test' })
    await expect(updatePromptTemplate(
      'one',
      {
        category: null,
        content_markdown: 'a',
        expected_revision: 1,
        include_in_autocomplete: true,
        label: 'one',
        title: 'One',
        visibility: { agent: false, chat: true, editor: false },
      },
      { baseUrl: 'http://api.test' },
    )).rejects.toMatchObject({ name: 'authorization_error', status: 403 })

    expect(mutationHeaders).toEqual([null])
  })

  it('does not carry a bootstrap token across an identity transition', async () => {
    vi.stubGlobal('document', { cookie: '' })
    const mutationHeaders: Array<string | null> = []
    const fetchMock = vi.fn(async (
      input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
      if (String(input).endsWith('/api/auth/session')) {
        return jsonResponse({
          authenticated: true,
          csrf_token: 'user-one-token',
          user: { display_name: 'Ada', email: 'ada@example.de', id: 'user-1', role: 'user' },
        })
      }
      mutationHeaders.push(new Headers(init?.headers).get('X-CSRF-Token'))
      return jsonResponse({
        error: { message: 'Forbidden', type: 'authorization_error' },
      }, 403)
    })
    vi.stubGlobal('fetch', fetchMock)

    await fetchAuthSession({ baseUrl: 'http://api.test' })
    setExpectedUserIdentity('user-2')
    await expect(updatePromptTemplate(
      'one',
      {
        category: null,
        content_markdown: 'a',
        expected_revision: 1,
        include_in_autocomplete: true,
        label: 'one',
        title: 'One',
        visibility: { agent: false, chat: true, editor: false },
      },
      { baseUrl: 'http://api.test' },
    )).rejects.toMatchObject({ name: 'authorization_error', status: 403 })

    expect(mutationHeaders).toEqual([null])
  })

  it('clears the in-memory token only after logout is confirmed', async () => {
    vi.stubGlobal('document', { cookie: '' })
    const logoutHeaders: Array<string | null> = []
    const mutationHeaders: Array<string | null> = []
    const fetchMock = vi.fn(async (
      input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
      const url = String(input)
      if (url.endsWith('/api/auth/session')) {
        return jsonResponse({
          authenticated: true,
          csrf_token: 'fresh-token',
          user: { display_name: 'Ada', email: 'ada@example.de', id: 'user-1', role: 'user' },
        })
      }
      if (url.endsWith('/api/auth/logout')) {
        logoutHeaders.push(new Headers(init?.headers).get('X-CSRF-Token'))
        return jsonResponse({ logged_out: true })
      }
      mutationHeaders.push(new Headers(init?.headers).get('X-CSRF-Token'))
      return jsonResponse({
        error: { message: 'Forbidden', type: 'authorization_error' },
      }, 403)
    })
    vi.stubGlobal('fetch', fetchMock)

    await fetchAuthSession({ baseUrl: 'http://api.test' })
    await logoutSession({ baseUrl: 'http://api.test' })
    await expect(updatePromptTemplate(
      'one',
      {
        category: null,
        content_markdown: 'a',
        expected_revision: 1,
        include_in_autocomplete: true,
        label: 'one',
        title: 'One',
        visibility: { agent: false, chat: true, editor: false },
      },
      { baseUrl: 'http://api.test' },
    )).rejects.toMatchObject({ name: 'authorization_error', status: 403 })

    expect(logoutHeaders).toEqual(['fresh-token'])
    expect(mutationHeaders).toEqual([null])
  })

  it('refreshes once for concurrent typed CSRF failures and retries each mutation once', async () => {
    vi.stubGlobal('document', { cookie: 'inqtrix_csrf=stale-token' })
    let refreshCalls = 0
    const attempts = new Map<string, number>()
    const mutationHeaders = new Map<string, Array<string | null>>()
    const fetchMock = vi.fn(async (
      input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
      const url = String(input)
      if (url.endsWith('/api/auth/session')) {
        refreshCalls += 1
        await Promise.resolve()
        return jsonResponse({
          authenticated: true,
          csrf_token: 'fresh-token',
          user: { display_name: 'Ada', email: 'ada@example.de', id: 'user-1', role: 'user' },
        })
      }
      const count = (attempts.get(url) ?? 0) + 1
      attempts.set(url, count)
      const token = new Headers(init?.headers).get('X-CSRF-Token')
      mutationHeaders.set(url, [...mutationHeaders.get(url) ?? [], token])
      return count === 1 || token !== 'fresh-token'
        ? jsonResponse({
          detail: { error: { message: 'CSRF failed', type: 'csrf_error' } },
        }, 403)
        : jsonResponse({ id: url, revision: 2 })
    })
    vi.stubGlobal('fetch', fetchMock)

    await Promise.all([
      updatePromptTemplate(
        'one',
        {
          category: null,
          content_markdown: 'a',
          expected_revision: 1,
          include_in_autocomplete: true,
          label: 'one',
          title: 'One',
          visibility: { agent: false, chat: true, editor: false },
        },
        { baseUrl: 'http://api.test' },
      ),
      updatePromptTemplate(
        'two',
        {
          category: null,
          content_markdown: 'b',
          expected_revision: 1,
          include_in_autocomplete: true,
          label: 'two',
          title: 'Two',
          visibility: { agent: false, chat: true, editor: false },
        },
        { baseUrl: 'http://api.test' },
      ),
    ])

    expect(refreshCalls).toBe(1)
    expect(attempts.get('http://api.test/v1/prompt-templates/one')).toBe(2)
    expect(attempts.get('http://api.test/v1/prompt-templates/two')).toBe(2)
    expect(mutationHeaders.get('http://api.test/v1/prompt-templates/one')).toEqual([
      'stale-token',
      'fresh-token',
    ])
    expect(mutationHeaders.get('http://api.test/v1/prompt-templates/two')).toEqual([
      'stale-token',
      'fresh-token',
    ])
  })

  it('repairs a missing cookie once and never loops when the retry still fails CSRF', async () => {
    vi.stubGlobal('document', { cookie: '' })
    const mutationHeaders: Array<string | null> = []
    const fetchMock = vi.fn(async (
      input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
      if (String(input).endsWith('/api/auth/session')) {
        return jsonResponse({
          authenticated: true,
          csrf_token: 'fresh-token',
          user: { display_name: null, email: null, id: 'user-1', role: 'user' },
        })
      }
      mutationHeaders.push(new Headers(init?.headers).get('X-CSRF-Token'))
      return jsonResponse({
        detail: { error: { message: 'CSRF failed', type: 'csrf_error' } },
      }, 403)
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(updatePromptTemplate(
      'one',
      {
        category: null,
        content_markdown: 'a',
        expected_revision: 1,
        include_in_autocomplete: true,
        label: 'one',
        title: 'One',
        visibility: { agent: false, chat: true, editor: false },
      },
      { baseUrl: 'http://api.test' },
    )).rejects.toMatchObject({ name: 'csrf_error', status: 403 })

    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(mutationHeaders).toEqual([null, 'fresh-token'])
  })

  it('does not refresh PAT requests or ordinary forbidden responses', async () => {
    vi.stubGlobal('document', { cookie: 'inqtrix_csrf=stale-token' })
    const fetchMock = vi.fn(async () => jsonResponse({
      error: { message: 'Forbidden', type: 'authorization_error' },
    }, 403))
    vi.stubGlobal('fetch', fetchMock)

    await expect(updatePromptTemplate(
      'one',
      {
        category: null,
        content_markdown: 'a',
        expected_revision: 1,
        include_in_autocomplete: true,
        label: 'one',
        title: 'One',
        visibility: { agent: false, chat: true, editor: false },
      },
      { apiKey: 'pat', baseUrl: 'http://api.test' },
    )).rejects.toMatchObject({ name: 'authorization_error', status: 403 })

    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('does not use an account-session bootstrap for guest CSRF failures', async () => {
    vi.stubGlobal('document', { cookie: 'inqtrix_editor_guest_csrf=guest-token' })
    const fetchMock = vi.fn(async () => jsonResponse({
      detail: { error: { message: 'Guest CSRF failed', type: 'csrf_error' } },
    }, 403))
    vi.stubGlobal('fetch', fetchMock)

    await expect(markGuestEditorCollaborationCommentsRead(
      7,
      { baseUrl: 'http://api.test' },
    )).rejects.toMatchObject({ name: 'csrf_error', status: 403 })

    expect(fetchMock).toHaveBeenCalledTimes(1)
  })
})

describe('bound file upload reservation', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('reserves the stable asset id before multipart bytes move', async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) => {
      void _input
      void _init
      return jsonResponse({
        id: 'file-a',
        section_id: 'section-a',
        upload_status: 'uploading',
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const file = new File(['hello'], 'A.txt', { type: 'text/plain' })

    await reserveServerFileUpload(file, {
      asset_id: 'file-a',
      created_at: 10,
      group_id: null,
      label: 'a',
      origin: 'editor',
      section_id: 'section-a',
      title: 'A.txt',
      updated_at: 11,
    }, { baseUrl: 'http://api.test', workspaceId: 'workspace-a' })

    expect(fetchMock).toHaveBeenCalledWith(
      'http://api.test/v1/assets/file-a/upload-reservation',
      expect.objectContaining({ method: 'POST' }),
    )
    const init = fetchMock.mock.calls[0]?.[1] as RequestInit
    expect(JSON.parse(String(init.body))).toMatchObject({
      file_name: 'A.txt',
      mime_type: 'text/plain',
      section_id: 'section-a',
      size_bytes: 5,
    })
  })

  it('preserves a 202 durable retry instead of treating it as a ready file', async () => {
    const response = {
      asset: {
        id: 'file-a',
        server_file_id: null,
        upload_operation_id: 'up_1',
        upload_status: 'retrying',
      },
      object: 'upload_operation',
      upload_operation: {
        asset_id: 'file-a',
        attempt: 1,
        created_at: 1,
        error: { message: 'storage unavailable', type: 'dependency_error' },
        file_id: 'fl_1',
        finished_at: null,
        operation_id: 'up_1',
        requires_bytes: false,
        retryable: true,
        stage: 'object_stored',
        started_at: 2,
        status: 'queued',
      },
    }
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(response, 202)))

    const result = await uploadServerFile(
      new File(['hello'], 'A.txt', { type: 'text/plain' }),
      { baseUrl: 'http://api.test' },
    )

    expect(result).toMatchObject({
      object: 'upload_operation',
      upload_operation: { operation_id: 'up_1', status: 'queued' },
    })
  })

  it('uses the canonical operation feed and retry endpoints', async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) => {
      void _input
      void _init
      return jsonResponse({ data: [], object: 'list' })
    })
    vi.stubGlobal('fetch', fetchMock)

    await listUploadOperations({ baseUrl: 'http://api.test' })
    await retryUploadOperation('up 1', { baseUrl: 'http://api.test' })

    expect(fetchMock.mock.calls[0]?.[0]).toBe('http://api.test/v1/uploads')
    expect(fetchMock.mock.calls[1]?.[0]).toBe('http://api.test/v1/uploads/up%201/retry')
    expect(fetchMock.mock.calls[1]?.[1]).toEqual(expect.objectContaining({ method: 'POST' }))
  })
})

describe('editor collaboration client contract', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('omits the lease on initial join and rotates it only in the refresh body', async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) => {
      void _input
      void _init
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    const options = { baseUrl: 'http://api.test' }

    await createEditorCollaborationSession(
      'doc-1',
      { protocol_version: 1, schema_version: 1 },
      options,
    )
    await createEditorCollaborationSession(
      'doc-1',
      {
        lease_token: 'current-private-token',
        protocol_version: 1,
        rotation_command_id: '00000000-0000-4000-8000-000000000004',
        schema_version: 1,
      },
      options,
    )

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://api.test/v1/editor/documents/doc-1/collaboration/session',
      expect.objectContaining({
        body: JSON.stringify({ protocol_version: 1, schema_version: 1 }),
        method: 'POST',
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'http://api.test/v1/editor/documents/doc-1/collaboration/session',
      expect.objectContaining({
        body: JSON.stringify({
          lease_token: 'current-private-token',
          protocol_version: 1,
          rotation_command_id: '00000000-0000-4000-8000-000000000004',
          schema_version: 1,
        }),
        method: 'POST',
      }),
    )
    for (const [url] of fetchMock.mock.calls) {
      expect(String(url)).not.toContain('current-private-token')
    }
  })

  it('keeps document lists owned-only by default and opts history hydration into all scope', async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) => {
      void _input
      void _init
      return jsonResponse({ data: [], next_cursor: null })
    })
    vi.stubGlobal('fetch', fetchMock)
    const options = {
      baseUrl: 'http://api.test',
      workspaceId: 'workspace-1',
    }

    await listEditorDocuments(options)
    await listEditorDocuments({ ...options, limit: 200, scope: 'all' })

    expect(fetchMock.mock.calls[0][0]).toBe('http://api.test/v1/editor/documents')
    expect(fetchMock.mock.calls[1][0]).toBe(
      'http://api.test/v1/editor/documents?limit=200&scope=all',
    )
    const headers = fetchMock.mock.calls[1][1]?.headers as Headers
    expect(headers.get('X-Inqtrix-Workspace-Id')).toBe('workspace-1')
  })

  it('uses the additive metadata and collaboration endpoints without body PUTs', async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) => {
      void _input
      void _init
      return jsonResponse({ data: [], next_cursor: null })
    })
    vi.stubGlobal('fetch', fetchMock)
    const options = { baseUrl: 'http://api.test' }

    await patchEditorDocumentMetadata(
      'doc-1',
      {
        diff_anchor_markdown: '# Projected anchor',
        diff_anchor_updated_at: 123.5,
        expected_metadata_revision: 3,
        folder_id: null,
        title: 'Shared notes',
      },
      options,
    )
    await enableEditorDocumentCollaboration(
      'doc-1',
      { expected_metadata_revision: 3, expected_revision: 7, schema_version: 1 },
      options,
    )
    await listEditorCollaborationActivity(
      'doc-1',
      { ...options, cursor: '12', limit: 25 },
    )
    await flushEditorCollaborationProjection('doc-1', options)
    await publishEditorCollaborationSuggestion(
      'doc-1',
      {
        actor_kind: 'assistant',
        base_markdown: '# Shared',
        command_id: '00000000-0000-4000-8000-000000000002',
        expected_sequence: 12,
        patch_id: '00000000-0000-4000-8000-000000000003',
        target_markdown: '# Shared suggestion',
      },
      options,
    )
    await decideEditorCollaborationPatches(
      'doc-1',
      {
        decision: 'accept',
        decision_id: '00000000-0000-4000-8000-000000000001',
        expected_sequence: 12,
        patch_ids: ['00000000-0000-4000-8000-000000000005'],
      },
      options,
    )

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://api.test/v1/editor/documents/doc-1',
      expect.objectContaining({
        body: JSON.stringify({
          diff_anchor_markdown: '# Projected anchor',
          diff_anchor_updated_at: 123.5,
          expected_metadata_revision: 3,
          folder_id: null,
          title: 'Shared notes',
        }),
        method: 'PATCH',
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'http://api.test/v1/editor/documents/doc-1/collaboration:enable',
      expect.objectContaining({
        body: JSON.stringify({
          expected_metadata_revision: 3,
          expected_revision: 7,
          schema_version: 1,
        }),
        method: 'POST',
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      'http://api.test/v1/editor/documents/doc-1/activity?cursor=12&limit=25',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      'http://api.test/v1/editor/documents/doc-1/collaboration/projection:flush',
      expect.objectContaining({ method: 'POST' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      'http://api.test/v1/editor/documents/doc-1/suggestions:publish',
      expect.objectContaining({
        body: JSON.stringify({
          actor_kind: 'assistant',
          base_markdown: '# Shared',
          command_id: '00000000-0000-4000-8000-000000000002',
          expected_sequence: 12,
          patch_id: '00000000-0000-4000-8000-000000000003',
          target_markdown: '# Shared suggestion',
        }),
        method: 'POST',
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      6,
      'http://api.test/v1/editor/documents/doc-1/patches:decide',
      expect.objectContaining({
        body: JSON.stringify({
          decision: 'accept',
          decision_id: '00000000-0000-4000-8000-000000000001',
          expected_sequence: 12,
          patch_ids: ['00000000-0000-4000-8000-000000000005'],
        }),
        method: 'POST',
      }),
    )
    expect(fetchMock.mock.calls.map(([, init]) => init?.method)).not.toContain('PUT')
  })
})

describe('agent memory client', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('sends memory search through the existing list endpoint', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ data: [], object: 'list', status: {} }))
    vi.stubGlobal('fetch', fetchMock)

    await listAgentMemories(
      { baseUrl: 'http://api.test' },
      { limit: 5, q: 'decision table', scope: 'user' },
    )

    expect(fetchMock).toHaveBeenCalledWith(
      'http://api.test/v1/agent/memory?q=decision+table&scope=user&limit=5',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('reads feedback history and posts run feedback without owner fields', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ data: [], object: 'list' }))
    vi.stubGlobal('fetch', fetchMock)

    await listAgentMemoryFeedback(
      { baseUrl: 'http://api.test' },
      { limit: 10, runId: 'run_1' },
    )
    await submitAgentRunFeedback(
      'run_1',
      { feedback: 'positive', memory_id: 'mem_1', reason: 'useful' },
      { baseUrl: 'http://api.test' },
    )

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://api.test/v1/agent/memory/feedback?run_id=run_1&limit=10',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'http://api.test/v1/agent/runs/run_1/feedback',
      expect.objectContaining({
        body: JSON.stringify({
          feedback: 'positive',
          memory_id: 'mem_1',
          reason: 'useful',
        }),
        method: 'POST',
      }),
    )
  })

  it('wires the memory mutation endpoints to their verbs and paths', async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) => {
      void _input
      void _init
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    const options = { baseUrl: 'http://api.test' }

    await updateAgentMemory(
      'mem 1',
      { category: 'preference', content: 'x', scope: 'user' },
      options,
    )
    await deleteAgentMemory('mem 1', options)
    await clearAgentMemories(options)
    await listAgentMemoryCandidates(options)
    await acceptAgentMemoryCandidate('cand 1', {}, options)
    await rejectAgentMemoryCandidate('cand 1', options)

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://api.test/v1/agent/memory/mem%201',
      expect.objectContaining({
        body: JSON.stringify({
          category: 'preference',
          content: 'x',
          scope: 'user',
        }),
        method: 'PATCH',
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'http://api.test/v1/agent/memory/mem%201',
      expect.objectContaining({ method: 'DELETE' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      'http://api.test/v1/agent/memory:clear',
      expect.objectContaining({ body: JSON.stringify({}), method: 'POST' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      4,
      'http://api.test/v1/agent/memory/candidates',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      5,
      'http://api.test/v1/agent/memory/candidates/cand%201:accept',
      expect.objectContaining({ body: JSON.stringify({}), method: 'POST' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      6,
      'http://api.test/v1/agent/memory/candidates/cand%201:reject',
      expect.objectContaining({ body: JSON.stringify({}), method: 'POST' }),
    )
  })
})

describe('sharing client contract', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('uses only inbox and mine for sharing lifecycle listings', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({
      data: [],
      object: 'list',
    }))
    vi.stubGlobal('fetch', fetchMock)

    await fetchSharingInbox({ baseUrl: 'http://api.test' })
    await fetchMyShares({ baseUrl: 'http://api.test' })

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://api.test/v1/shares/inbox',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'http://api.test/v1/shares/mine',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('loads bounded editor access metrics for the selected owner window', async () => {
    const payload = {
      direct_share_count: 3,
      guest_link_count: 2,
      guest_open_count: 8,
      guest_session_count: 5,
      last_guest_accessed_at: 123,
      object: 'editor_access_summary',
      share_links: [],
      window: '30d',
    }
    const fetchMock = vi.fn(async () => jsonResponse(payload))
    vi.stubGlobal('fetch', fetchMock)

    await expect(getEditorAccessSummary(
      'doc / 1',
      '30d',
      { baseUrl: 'http://api.test' },
    )).resolves.toEqual(payload)
    expect(fetchMock).toHaveBeenCalledWith(
      'http://api.test/v1/editor/documents/doc%20%2F%201/access-summary?window=30d',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('grants by canonical user id without subject aliases', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({
      data: [],
      object: 'list',
    }))
    vi.stubGlobal('fetch', fetchMock)

    await createShares(
      'run',
      'run_1',
      [{ permission: 'edit', userId: '00000000-0000-4000-8000-000000000002' }],
      { baseUrl: 'http://api.test' },
    )

    expect(fetchMock).toHaveBeenCalledWith(
      'http://api.test/v1/shares',
      expect.objectContaining({
        body: JSON.stringify({
          invitees: [{
            permission: 'edit',
            user_id: '00000000-0000-4000-8000-000000000002',
          }],
          resource_id: 'run_1',
          resource_type: 'run',
        }),
        method: 'POST',
      }),
    )
  })

  it('updates a share with its exact optimistic revision', async () => {
    const updated = {
      accepted_at: 1,
      created_at: 1,
      display_name: null,
      email: null,
      granted_by_user_id: '00000000-0000-4000-8000-000000000001',
      id: 'share-1',
      permission: 'edit',
      recipient_user_id: '00000000-0000-4000-8000-000000000002',
      resource_id: 'run_1',
      resource_type: 'run',
      revision: 4,
    }
    const fetchMock = vi.fn(async () => jsonResponse({ data: updated, object: 'share' }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(updateShare(
      'share-1',
      { expectedRevision: 3, permission: 'edit' },
      { baseUrl: 'http://api.test' },
    )).resolves.toEqual(updated)
    expect(fetchMock).toHaveBeenCalledWith(
      'http://api.test/v1/shares/share-1',
      expect.objectContaining({
        body: JSON.stringify({ expected_revision: 3, permission: 'edit' }),
        method: 'PATCH',
      }),
    )
  })
})

describe('knowledge collection client contract', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('pages documents by collection id and probes original access without downloading it', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ data: [], next_cursor: null }))
    vi.stubGlobal('fetch', fetchMock)

    await listKnowledgeDocuments('kc 1', {
      baseUrl: 'http://api.test',
      cursor: 'next row',
      limit: 50,
    })
    await fetchServerFileInfo('fl 1', { baseUrl: 'http://api.test' })

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://api.test/v1/knowledge/collections/kc 1/documents?cursor=next+row&limit=50',
      expect.objectContaining({ method: 'GET' }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'http://api.test/v1/files/fl 1',
      expect.objectContaining({ method: 'GET' }),
    )
  })

  it('resolves a legacy index member through its encoded stable source id', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({
      chunk_count: 1,
      collection_id: 'kc-1',
      created_at: 1,
      id: 'kd-1',
      metadata: {},
      title: 'Legacy',
    }))
    vi.stubGlobal('fetch', fetchMock)

    await resolveKnowledgeDocumentBySource(
      'kc-1',
      'asset:file with spaces',
      { baseUrl: 'http://api.test' },
    )

    expect(fetchMock).toHaveBeenCalledWith(
      'http://api.test/v1/knowledge/collections/kc-1/documents/by-source?source_id=asset%3Afile+with+spaces',
      expect.objectContaining({ method: 'GET' }),
    )
  })
})

describe('asset deletion operation client contract', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('reads the retained scoped feed with keyset pagination', async () => {
    const payload = { data: [], next_cursor: null }
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) => {
      void _input
      void _init
      return jsonResponse(payload)
    })
    vi.stubGlobal('fetch', fetchMock)

    await expect(listAssetDeletionOperations({
      baseUrl: 'http://api.test',
      cursor: 'next operation',
      limit: 200,
      workspaceId: 'workspace-1',
    })).resolves.toEqual(payload)

    expect(fetchMock).toHaveBeenCalledWith(
      'http://api.test/v1/assets/deletion-operations?cursor=next+operation&limit=200',
      expect.objectContaining({ method: 'GET' }),
    )
    const headers = fetchMock.mock.calls[0]?.[1]?.headers as Headers
    expect(headers.get('X-Inqtrix-Workspace-Id')).toBe('workspace-1')
  })
})

describe('shared content optimistic-concurrency contract', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('sends integer revisions for prompt-template and skill updates', async () => {
    const fetchMock = vi.fn(async (
      _input: RequestInfo | URL,
      _init?: RequestInit,
    ) => {
      void _input
      void _init
      return jsonResponse({})
    })
    vi.stubGlobal('fetch', fetchMock)
    const options = { baseUrl: 'http://api.test' }

    await updatePromptTemplate('pt 1', {
      category: null,
      content_markdown: 'Summarize.',
      expected_revision: 7,
      include_in_autocomplete: true,
      label: 'summary',
      title: 'Summary',
      visibility: { agent: false, chat: true, editor: false },
    }, options)
    await updateSkill('sk 1', {
      allowed_tools: [],
      argument_hint: '',
      clarification_points: [],
      deliverable: '',
      description: 'Summarize a document.',
      effort: '',
      expected_revision: 11,
      include_in_autocomplete: true,
      instructions_markdown: 'Summarize.',
      invocation: 'user_only',
      label: 'summary',
      model_tier: '',
      requires_plan: 'auto',
      title: 'Summary',
      when_to_use: 'When a summary is requested.',
    }, options)

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      'http://api.test/v1/prompt-templates/pt 1',
      expect.objectContaining({
        body: expect.stringContaining('"expected_revision":7'),
        method: 'PUT',
      }),
    )
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      'http://api.test/v1/skills/sk 1',
      expect.objectContaining({
        body: expect.stringContaining('"expected_revision":11'),
        method: 'PUT',
      }),
    )
    for (const call of fetchMock.mock.calls) {
      expect(String(call[1]?.body)).not.toContain('expected_updated_at')
    }
  })
})

describe('SSE liveness', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('reports comment-only heartbeat bytes as transport activity', async () => {
    const fetchMock = vi.fn(async () => new Response(': keepalive\n\n', {
      headers: { 'Content-Type': 'text/event-stream' },
      status: 200,
    }))
    vi.stubGlobal('fetch', fetchMock)
    const onActivity = vi.fn()
    const onEvent = vi.fn()

    await streamServerSentEvents('/v1/runs/run_1/events', {
      baseUrl: 'http://api.test',
      onActivity,
      onEvent,
    })

    expect(onActivity).toHaveBeenCalled()
    expect(onEvent).not.toHaveBeenCalled()
  })

  it('parses named user events and resumes with Last-Event-ID', async () => {
    const body = [
      'event: ready\ndata: {"user_id":"user-1","cursor":"40"}',
      'id: 41\nevent: invalidate\ndata: {"scope":"sharing","resource_type":"run"}',
      'event: reset\ndata: {}',
      '',
    ].join('\n\n')
    const fetchMock = vi.fn(async (
      input: RequestInfo | URL,
      init?: RequestInit,
    ) => {
      void input
      void init
      return new Response(body, {
        headers: { 'Content-Type': 'text/event-stream' },
        status: 200,
      })
    })
    vi.stubGlobal('fetch', fetchMock)
    const events: unknown[] = []

    await streamUserEvents({
      baseUrl: 'http://api.test',
      lastEventId: '40',
      onEvent: (event) => events.push(event),
    })

    expect(events).toEqual([
      {
        data: { cursor: '40', user_id: 'user-1' },
        id: null,
        type: 'ready',
      },
      {
        data: { resource_type: 'run', scope: 'sharing' },
        id: '41',
        type: 'invalidate',
      },
      { data: {}, id: null, type: 'reset' },
    ])
    const request = fetchMock.mock.calls[0]?.[1] as RequestInit
    expect((request.headers as Headers).get('Last-Event-ID')).toBe('40')
  })
})

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    headers: { 'Content-Type': 'application/json' },
    status,
  })
}

describe('attaching research reports to an agent run', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  async function bodyOf(request: Parameters<typeof createResearchRun>[0]) {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ run_id: 'run_x', status: 'queued' }, 202))
    vi.stubGlobal('fetch', fetchMock)
    await createResearchRun(request, { baseUrl: 'http://api.test' })
    const call = fetchMock.mock.calls.at(0) as unknown as [
      string,
      { body?: string },
    ]
    return JSON.parse(String(call[1]?.body)) as Record<string, unknown>
  }

  it('sends ids only — never the report text', async () => {
    // A real report has a median of ~54k characters. The server resolves
    // name and visibility; the kernel fetches the body on demand.
    const body = await bodyOf({
      mode: 'agent_kernel',
      question: 'Schreibe einen Sprechzettel.',
      reportIds: ['run_a', 'run_b'],
    })
    expect(body.report_ids).toEqual(['run_a', 'run_b'])
    expect(JSON.stringify(body)).not.toContain('markdown')
  })

  it('says nothing when no report is attached', async () => {
    const body = await bodyOf({
      mode: 'agent_kernel',
      question: 'Ohne Anhang.',
    })
    expect('report_ids' in body).toBe(false)
  })

  it('treats an empty list as no attachment', async () => {
    const body = await bodyOf({
      mode: 'agent_kernel',
      question: 'Leere Liste.',
      reportIds: [],
    })
    expect(body.report_ids).toBeUndefined()
  })
})

describe('attaching rules to an agent run', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  async function bodyOf(request: Parameters<typeof createResearchRun>[0]) {
    const fetchMock = vi.fn(async () =>
      jsonResponse({ run_id: 'run_x', status: 'queued' }, 202))
    vi.stubGlobal('fetch', fetchMock)
    await createResearchRun(request, { baseUrl: 'http://api.test' })
    const call = fetchMock.mock.calls.at(0) as unknown as [
      string,
      { body?: string },
    ]
    return JSON.parse(String(call[1]?.body)) as Record<string, unknown>
  }

  it('sends rule ids only, in attachment order', async () => {
    // The server resolves label, revision and text from the caller's own
    // catalog; a client that sent text would be an unchecked channel
    // into the writing prompts.
    const body = await bodyOf({
      mode: 'agent_kernel',
      question: 'Halte dich an Executive Briefing.',
      reportRuleIds: ['pt_brief', 'pt_lektor'],
    })
    expect(body.report_rule_ids).toEqual(['pt_brief', 'pt_lektor'])
    expect('report_guidance' in body).toBe(false)
    expect('report_rules' in body).toBe(false)
  })

  it('says nothing when no rule is attached', async () => {
    const none = await bodyOf({ mode: 'agent_kernel', question: 'Ohne Regel.' })
    expect('report_rule_ids' in none).toBe(false)
    const empty = await bodyOf({ mode: 'agent_kernel', question: 'Leer.', reportRuleIds: [] })
    expect(empty.report_rule_ids).toBeUndefined()
  })
})

describe('incognito flag on outgoing requests', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('asks the server to keep an incognito turn out of traces on both transports', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      void _input
      const body = JSON.parse(String(init?.body)) as { stream?: boolean }
      return body.stream
        ? new Response('data: [DONE]\n\n', {
            headers: { 'Content-Type': 'text/event-stream' },
          })
        : jsonResponse({ choices: [], created: 0, id: 'chatcmpl-1' })
    })
    vi.stubGlobal('fetch', fetchMock)
    const messages = [{ content: 'Hallo', role: 'user' as const }]

    await createChatCompletion({ incognito: true, messages }, { baseUrl: 'http://api.test' })
    await streamChatCompletion(
      { incognito: true, messages },
      { baseUrl: 'http://api.test', onDelta: () => undefined },
    )
    await createChatCompletion({ messages }, { baseUrl: 'http://api.test' })

    const [blocking, streamed, normal] = fetchMock.mock.calls.map(
      (call) => JSON.parse(String(call[1]?.body)) as Record<string, unknown>,
    )
    expect(blocking).toMatchObject({ incognito: true, stream: false })
    expect(streamed).toMatchObject({ incognito: true, stream: true })
    expect(normal).not.toHaveProperty('incognito')
  })

  it('asks the server to keep an incognito knowledge run private', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ run_id: 'run_x', status: 'queued' }, 202))
    vi.stubGlobal('fetch', fetchMock)
    const question = 'Wie ist die Haftung geregelt?'

    await createResearchRun({ incognito: true, mode: 'knowledge', question }, { baseUrl: 'http://api.test' })
    await createResearchRun(
      { mode: 'knowledge', question, sessionId: 'ks_1' },
      { baseUrl: 'http://api.test' },
    )

    const [incognito, ordinary] = fetchMock.mock.calls.map(
      (call) =>
        JSON.parse(String((call as unknown as [string, { body?: string }])[1]?.body)) as Record<
          string,
          unknown
        >,
    )
    expect(incognito).toMatchObject({ incognito: true, mode: 'knowledge' })
    expect(incognito).not.toHaveProperty('session_id')
    expect(ordinary).toMatchObject({ session_id: 'ks_1' })
    expect(ordinary).not.toHaveProperty('incognito')
  })

  it('sends the interface language of a knowledge ask, from the desk and from the chat', async () => {
    const fetchMock = vi.fn(async () => jsonResponse({ run_id: 'run_x', status: 'queued' }, 202))
    vi.stubGlobal('fetch', fetchMock)

    await createResearchRun(
      { mode: 'knowledge', question: 'Was ist AI?', uiLanguage: 'de' },
      { baseUrl: 'http://api.test' },
    )
    await createChatCompletion(
      { messages: [{ content: 'Was ist AI?', role: 'user' }], mode: 'knowledge', uiLanguage: 'de' },
      { baseUrl: 'http://api.test' },
    ).catch(() => undefined)

    const [run, chat] = fetchMock.mock.calls.map(
      (call) =>
        JSON.parse(String((call as unknown as [string, { body?: string }])[1]?.body)) as Record<
          string,
          unknown
        >,
    )
    expect(run).toMatchObject({ mode: 'knowledge', ui_language: 'de' })
    expect(chat).toMatchObject({ mode: 'knowledge', ui_language: 'de' })
  })
})
