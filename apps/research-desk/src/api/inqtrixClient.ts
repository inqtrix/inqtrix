import type { ReferenceDoc } from '@/features/files/referenceBlocks'
import type {
  IndexingJobEvent,
  IndexingJobSummary,
} from '@/features/fileLibrary/indexingTypes'
import type {
  PromptTemplateInfo,
  PromptTemplatePayload,
} from '@/features/promptLibrary/templateSync'
import type {
  SkillInfo,
  SkillPayload,
} from '@/features/skills/skillLibrary'
import type {
  QuotaAdminSnapshot,
  WorkspaceMembership,
} from '@/features/quota/admin'
import type { QuotaDimensionUsage } from '@/features/quota/model'
import type {
  CreatedEditorShareLink,
  EditorAccessSummary,
  EditorShareLink,
  EditorShareLinkPermission,
  OutgoingShare,
  ShareInvitee,
  SharePermissionValue,
  ShareRecordInfo,
  SharingInbox,
  UserSearchResult,
} from '@/features/sharing/types'
import { requestErrorText } from './requestErrorText'
import type {
  AgentApprovalDecisionRequest,
  AgentApprovalWire,
  AgentPatchWire,
  AgentArtifactDetailWire,
  AgentArtifactMetaWire,
  AgentTaskCancelWire,
  AgentTaskResultWire,
  AgentClarificationAnswerRequest,
  AgentClarificationWire,
  AgentPlanWire,
  ServerAgentSession,
  ServerAgentSessionGroup,
} from '@/features/agent/types'
import type {
  AgentOverrides,
  CreateResearchRunRequest,
  InqtrixCapabilities,
  InqtrixError,
  InqtrixHealth,
  InqtrixStackList,
  KnowledgeChatFilters,
  KnowledgeCollectionInfo,
  KnowledgeDocumentInfo,
  KnowledgeDocumentText,
  KnowledgeSearchResponse,
  NodeModelResolution,
  ResearchRunMode,
  ResearchRunEvent,
  ResearchRunResult,
  ResearchRunSummary,
} from '@/features/researchRuns/types'

export type ClientOptions = {
  apiKey?: string
  baseUrl?: string
  /** Resume cursor for fetch-based SSE reconnects. Ignored by ordinary
   * JSON endpoints; event-stream callers send it as `Last-Event-ID`. */
  lastEventId?: string
  /** Keep a caller-owned recovery flow in control of an expected HTTP 401.
   * Ordinary authenticated requests still hard-reload on an unexpected
   * principal loss. */
  reloadOnUnauthorized?: boolean
  signal?: AbortSignal
  workspaceId?: string
}

type StreamRunEventsOptions = ClientOptions & {
  /** Fires for every received SSE byte chunk, including comment
   * heartbeats that intentionally contain no data event. */
  onActivity?: () => void
  onEvent: (event: ResearchRunEvent) => void
}

export type ChatCompletionMessage = {
  content: string
  role: 'assistant' | 'system' | 'user'
}

export type ChatCompletionRequest = {
  agentOverrides?: AgentOverrides
  /** Keep the turn's content out of the server's traces (incognito chat). */
  incognito?: boolean
  includeProgress?: boolean
  /** Knowledge-retrieval scope; only meaningful with `mode: 'knowledge'`. */
  knowledgeFilters?: KnowledgeChatFilters
  messages: ChatCompletionMessage[]
  mode?: ResearchRunMode
  model?: string
  stack?: string
  stream?: boolean
  /** Interface language of a `mode: 'knowledge'` ask: the answer language
   * where the question leaves its own language open. */
  uiLanguage?: 'de' | 'en'
}

export type ChatCompletionResponse = {
  choices: Array<{
    finish_reason?: string | null
    index: number
    message: {
      content: string
      role: 'assistant'
    }
  }>
  created: number
  id: string
  inqtrix?: {
    model_resolution?: NodeModelResolution
  }
  model: string
  object: 'chat.completion'
  usage?: {
    completion_tokens: number
    prompt_tokens: number
    total_tokens: number
  }
}

export type TextImprovementContext = 'chat_input' | 'prompt_template'

export type TextImprovementRequest = {
  context: TextImprovementContext
  guidance?: string
  locale: 'de' | 'en'
  stack?: string
  text: string
}

export type TextImprovementResponse = {
  change_summary: string[]
  clarification_questions: string[]
  improved_text: string
  needs_clarification: boolean
  warnings: string[]
}

type ChatCompletionChunk = {
  choices?: Array<{
    delta?: {
      content?: string
      role?: 'assistant'
    }
    finish_reason?: string | null
    index?: number
  }>
  inqtrix?: {
    model_resolution?: NodeModelResolution
  }
}

type StreamChatCompletionOptions = ClientOptions & {
  onDone?: () => void
  onDelta: (delta: string) => void
  onModelResolution?: (resolution: NodeModelResolution) => void
}

const DEFAULT_BASE_URL = import.meta.env.VITE_INQTRIX_API_BASE_URL ?? ''
const CHAT_MODEL_NAME = 'research-agent'
const EXPECTED_USER_ID_HEADER = 'X-Inqtrix-Expected-User-Id'

let expectedUserIdentity: string | null = null
let csrfRefreshInFlight: Promise<void> | null = null
let sessionCsrfToken: string | null = null

/** Bind subsequent cookie-session requests to the SPA's rendered principal.
 * The value is a consistency generation, never an authentication credential. */
export function setExpectedUserIdentity(userId: string | null) {
  const nextIdentity = userId?.trim() || null
  if (
    expectedUserIdentity !== null
    && expectedUserIdentity !== nextIdentity
  ) {
    sessionCsrfToken = null
  }
  expectedUserIdentity = nextIdentity
}

export type InqtrixRequestError = Error & {
  status?: number
  /** Full error payload beyond message/type — 409 conflicts carry extras
   * like `current_revision` / `locked_by` the caller branches on. */
  detail?: Record<string, unknown>
  /** What the server (or the browser) said, for diagnosis; `message` is the
   * reader's sentence (see `requestErrorText`). */
  serverMessage?: string
}

export function hasHttpStatus(error: unknown, status: number) {
  return error instanceof Error && (error as InqtrixRequestError).status === status
}

export async function fetchHealth(options: ClientOptions = {}) {
  return requestJson<InqtrixHealth>('/health', options)
}

export async function fetchStacks(options: ClientOptions = {}) {
  return requestJson<InqtrixStackList>('/v1/stacks', options)
}

export async function fetchCapabilities(options: ClientOptions = {}) {
  return requestJson<InqtrixCapabilities>('/v1/capabilities', options)
}

export async function fetchQuotaUsage(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: QuotaDimensionUsage[] }>(
    '/v1/quota/usage',
    options,
  )
  return payload.data
}

export async function listWorkspaces(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: WorkspaceMembership[] }>(
    '/v1/workspaces',
    options,
  )
  return payload.data
}

// Quota administration is instance-admin power (tenant-wide), so it lives
// under /v1/admin/quota* and carries no workspace context — the server gates
// it on the session's instance_role.
export async function fetchQuotaAdmin(options: ClientOptions = {}) {
  return requestJson<QuotaAdminSnapshot>('/v1/admin/quota', options)
}

export async function setQuotaLimit(
  body: { user_id: string; dimension: string; value: number },
  options: ClientOptions = {},
) {
  await request('/v1/admin/quota/limits', {
    ...options,
    body,
    method: 'PUT',
  })
}

export async function clearQuotaLimit(
  userId: string,
  dimension: string,
  options: ClientOptions = {},
) {
  const query = new URLSearchParams({ dimension, user_id: userId })
  await request(`/v1/admin/quota/limits?${query}`, {
    ...options,
    method: 'DELETE',
  })
}

export async function resetQuota(
  body: { user_id: string; dimension: string },
  options: ClientOptions = {},
) {
  await request('/v1/admin/quota/reset', {
    ...options,
    body,
    method: 'POST',
  })
}

export async function listKnowledgeCollections(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: KnowledgeCollectionInfo[] }>(
    '/v1/knowledge/collections',
    options,
  )
  return payload.data
}

/** One authoritative page of documents in a visible server collection. */
export async function listKnowledgeDocuments(
  collectionId: string,
  options: PageOptions = {},
) {
  return requestJson<{ data: KnowledgeDocumentInfo[]; next_cursor: string | null }>(
    `/v1/knowledge/collections/${collectionId}/documents${pageQuery(options)}`,
    options,
  )
}

/** Resolve a member persisted before server_document_id via its stable source.
 * This endpoint is read-only and intentionally returns no extracted text. */
export async function resolveKnowledgeDocumentBySource(
  collectionId: string,
  sourceId: string,
  options: ClientOptions = {},
) {
  const query = new URLSearchParams({ source_id: sourceId })
  return requestJson<KnowledgeDocumentInfo>(
    `/v1/knowledge/collections/${collectionId}/documents/by-source?${query}`,
    options,
  )
}

export async function createKnowledgeCollection(
  request: { embeddingModel?: string; name: string },
  options: ClientOptions = {},
) {
  return requestJson<KnowledgeCollectionInfo>('/v1/knowledge/collections', {
    ...options,
    body: {
      embedding_model: request.embeddingModel,
      name: request.name,
    },
    method: 'POST',
  })
}

export async function deleteKnowledgeCollection(
  collectionId: string,
  options: ClientOptions = {},
) {
  return requestJson<ServerDeletionOperation>(
    `/v1/knowledge/collections/${collectionId}`,
    {
    ...options,
    method: 'DELETE',
    },
  )
}

/** Delete a single knowledge document from its collection (Postgres + vectors).
 * Used when a member is removed from a vector index so it leaves the searchable
 * collection immediately, without a full rebuild. */
export async function deleteKnowledgeDocument(
  documentId: string,
  options: ClientOptions = {},
) {
  return requestJson<ServerDeletionOperation>(
    `/v1/knowledge/documents/${documentId}`,
    {
    ...options,
    method: 'DELETE',
    },
  )
}

export type ServerFileInfo = {
  content_type: string
  created_at: number
  file_name: string
  id: string
  sha256: string
  size_bytes: number
  workspace_id: string | null
  /** Present only on a BOUND upload: the section-bound asset record the
   * server created together with the file. Its absence after a binding was
   * requested means the server predates upload binding — the caller falls
   * back to the regular asset autosave. */
  asset?: ServerAsset
}

export type ServerUploadOperationStatus =
  | 'running'
  | 'queued'
  | 'awaiting_bytes'
  | 'upload_failed'
  | 'ready'

export type ServerUploadOperationStage =
  | 'prepared'
  | 'object_stored'
  | 'file_registered'
  | 'asset_bound'
  | 'parsing'
  | 'parse_finished'
  | 'quota_booked'
  | 'ready'

/** Durable, redacted progress receipt for one bound original-file upload. */
export type ServerUploadOperation = {
  asset_id: string
  attempt: number
  created_at: number
  error: { message: string; type: string } | null
  file_id: string
  finished_at: number | null
  operation_id: string
  requires_bytes: boolean
  retryable: boolean
  stage: ServerUploadOperationStage
  started_at: number | null
  status: ServerUploadOperationStatus
}

/** A bound multipart either reaches ready synchronously or is durably queued
 * for dependency recovery. Both responses carry the same authoritative asset
 * and operation projections; callers must not infer success from HTTP 2xx. */
export type ServerBoundFileUpload =
  | (ServerFileInfo & {
      asset: ServerAsset
      upload_operation: ServerUploadOperation
    })
  | {
      asset: ServerAsset
      object: 'upload_operation'
      upload_operation: ServerUploadOperation
    }

/** Wire form fields for a bound upload — the file's target placement,
 * persisted by the server in the same request as the bytes. Snake_case +
 * unix-seconds like every asset DTO; conversion from records lives in
 * features/fileLibrary/assetSync.ts. */
export type ServerFileUploadBinding = {
  asset_id: string
  section_id: string
  group_id: string | null
  title: string
  label: string
  origin: string
  created_at: number
  updated_at: number
}

/** Persist the stable asset identity before the multipart body is sent. */
export async function reserveServerFileUpload(
  file: File,
  binding: ServerFileUploadBinding,
  options: ClientOptions = {},
) {
  return requestJson<ServerAsset>(
    `/v1/assets/${binding.asset_id}/upload-reservation`,
    {
      ...options,
      body: {
        section_id: binding.section_id,
        group_id: binding.group_id,
        title: binding.title,
        label: binding.label,
        file_name: file.name,
        mime_type: file.type || 'application/octet-stream',
        origin: binding.origin,
        page_count: null,
        parse_status: 'parsed',
        parse_warning: null,
        text_truncated: false,
        size_bytes: file.size,
        parser_id: null,
        created_at: binding.created_at,
        updated_at: binding.updated_at,
      },
      method: 'POST',
    },
  )
}

/**
 * Upload one original file to the server file store (`POST /v1/files`).
 * Multipart: the browser sets the boundary header itself, so this goes
 * through `fetch` directly instead of the JSON helper. With `binding`, the
 * request also carries the target placement. Normal project flows call
 * {@link reserveServerFileUpload} first; this request then finalises that
 * reservation through a lifecycle CAS.
 */
export async function uploadServerFile(
  file: File,
  options: ClientOptions = {},
  binding?: ServerFileUploadBinding,
) {
  const send = async (csrfRetryAttempted: boolean): Promise<Response> => {
    const headers = new Headers()
    if (options.apiKey) headers.set('Authorization', `Bearer ${options.apiKey}`)
    if (options.workspaceId) headers.set('X-Inqtrix-Workspace-Id', options.workspaceId)
    attachExpectedUserIdentity(headers)
    attachCsrfHeader(headers, 'POST')
    // A FormData body is one-shot from the recovery layer's perspective.
    // Rebuild it for the sole permitted retry instead of attempting to reuse
    // a body a browser may already have consumed.
    const body = new FormData()
    body.append('file', file, file.name)
    if (binding) {
      for (const [key, value] of Object.entries(binding)) {
        if (value === null || value === undefined) continue
        body.append(key, String(value))
      }
    }

    const response = await fetchReadable(resolveUrl('/v1/files', options.baseUrl), {
      method: 'POST',
      headers,
      body,
      signal: options.signal,
      credentials: 'include',
    })
    if (response.ok) return response

    const error = await requestError(response)
    if (canRecoverCsrf({
      csrfRetryAttempted,
      error,
      method: 'POST',
      options,
      path: '/v1/files',
    })) {
      await refreshSessionCsrf(options)
      return send(true)
    }
    throwParsedRequestError(
      error,
      response.status,
      options.reloadOnUnauthorized !== false,
    )
  }

  const response = await send(false)
  return (await response.json()) as ServerBoundFileUpload
}

export async function getUploadOperation(
  operationId: string,
  options: ClientOptions = {},
) {
  return requestJson<ServerUploadOperation>(
    `/v1/uploads/${encodeURIComponent(operationId)}`,
    options,
  )
}

export async function listUploadOperations(options: ClientOptions = {}) {
  return requestJson<{ data: ServerUploadOperation[]; object: 'list' }>(
    '/v1/uploads',
    options,
  )
}

export async function retryUploadOperation(
  operationId: string,
  options: ClientOptions = {},
) {
  return requestJson<ServerUploadOperation>(
    `/v1/uploads/${encodeURIComponent(operationId)}/retry`,
    { ...options, method: 'POST' },
  )
}

/** Metadata-only access probe for an original file. A 404 means the current
 * principal cannot access it (the same indistinguishable response as absence). */
export async function fetchServerFileInfo(
  fileId: string,
  options: ClientOptions = {},
) {
  return requestJson<ServerFileInfo>(`/v1/files/${fileId}`, options)
}

/**
 * Ingest a registered server file into a knowledge collection — the
 * server fetches, parses (MarkItDown by default) and indexes it.
 */
export async function ingestKnowledgeFile(
  collectionId: string,
  payload: { fileId: string; metadata?: Record<string, unknown>; title?: string },
  options: ClientOptions = {},
) {
  return requestJson<KnowledgeDocumentInfo>(
    `/v1/knowledge/collections/${collectionId}/documents`,
    {
      ...options,
      body: {
        file_id: payload.fileId,
        metadata: payload.metadata,
        title: payload.title,
      },
      method: 'POST',
    },
  )
}

export async function addKnowledgeDocument(
  collectionId: string,
  document: { metadata?: Record<string, unknown>; text: string; title: string },
  options: ClientOptions = {},
) {
  return requestJson<KnowledgeDocumentInfo>(
    `/v1/knowledge/collections/${collectionId}/documents`,
    {
      ...options,
      body: document,
      method: 'POST',
    },
  )
}

/** Synchronous retrieval search (`POST /v1/knowledge/search`) — the
 * "Finden" mode of the knowledge workspace. */
export async function searchKnowledge(
  request: { query: string; collectionIds?: string[]; topK?: number },
  options: ClientOptions = {},
) {
  const payload = await requestJson<KnowledgeSearchResponse>(
    '/v1/knowledge/search',
    {
      ...options,
      body: {
        query: request.query,
        ...(request.collectionIds && request.collectionIds.length > 0
          ? { collection_ids: request.collectionIds }
          : {}),
        ...(request.topK ? { top_k: request.topK } : {}),
      },
      method: 'POST',
    },
  )
  return {
    data: payload.data,
    warnings: Array.isArray(payload.warnings) ? payload.warnings : [],
  }
}

/** Full extracted text of one knowledge document (document reader). */
export async function fetchKnowledgeDocumentText(
  documentId: string,
  options: ClientOptions = {},
) {
  return requestJson<KnowledgeDocumentText>(
    `/v1/knowledge/documents/${documentId}/text`,
    options,
  )
}

/**
 * Download one original server file as a Blob (`GET /v1/files/{id}/content`).
 * Returned as blob + content type so callers can build an object URL —
 * needed because an `<iframe src>` cannot carry Bearer auth headers.
 */
export async function fetchServerFileContent(
  fileId: string,
  options: ClientOptions = {},
) {
  const response = await request(`/v1/files/${fileId}/content`, options)
  const blob = await response.blob()
  return {
    blob,
    contentType: response.headers.get('content-type') ?? blob.type ?? '',
  }
}

/** All prompt templates visible to the caller, newest first. */
export async function listPromptTemplates(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: PromptTemplateInfo[] }>(
    '/v1/prompt-templates',
    options,
  )
  return payload.data
}

/** Create one prompt template on the server. */
export async function createPromptTemplate(
  payload: PromptTemplatePayload,
  options: ClientOptions = {},
) {
  return requestJson<PromptTemplateInfo>('/v1/prompt-templates', {
    ...options,
    body: payload,
    method: 'POST',
  })
}

/** Replace one template using its mandatory compare-and-swap revision. */
export async function updatePromptTemplate(
  templateId: string,
  payload: PromptTemplatePayload & { expected_revision: number },
  options: ClientOptions = {},
) {
  return requestJson<PromptTemplateInfo>(
    `/v1/prompt-templates/${templateId}`,
    { ...options, body: payload, method: 'PUT' },
  )
}

/** Delete one template (owner-only; revokes its shares server-side). */
export async function deletePromptTemplate(
  templateId: string,
  options: ClientOptions = {},
) {
  await request(`/v1/prompt-templates/${templateId}`, {
    ...options,
    method: 'DELETE',
  })
}

// --- Skills: thin wire wrappers over /v1/skills ----------------------------

/** List the caller's visible skills, newest first. */
export async function listSkills(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: SkillInfo[] }>(
    '/v1/skills',
    options,
  )
  return payload.data
}

/** Create one skill on the server. */
export async function createSkill(
  payload: SkillPayload,
  options: ClientOptions = {},
) {
  return requestJson<SkillInfo>('/v1/skills', {
    ...options,
    body: payload,
    method: 'POST',
  })
}

/** Replace one skill using its mandatory compare-and-swap revision. */
export async function updateSkill(
  skillId: string,
  payload: SkillPayload & { expected_revision: number },
  options: ClientOptions = {},
) {
  return requestJson<SkillInfo>(`/v1/skills/${skillId}`, {
    ...options,
    body: payload,
    method: 'PUT',
  })
}

/** Delete one skill (owner-only; revokes its shares server-side). */
export async function deleteSkill(
  skillId: string,
  options: ClientOptions = {},
) {
  await request(`/v1/skills/${skillId}`, { ...options, method: 'DELETE' })
}

/** One skill as its SKILL.md document (text, server-serialized). */
export async function exportSkillMarkdown(
  skillId: string,
  options: ClientOptions = {},
) {
  const response = await request(`/v1/skills/${skillId}/markdown`, options)
  return response.text()
}

/** Create one skill from a SKILL.md document (server-parsed + validated). */
export async function importSkillMarkdown(
  markdown: string,
  options: ClientOptions = {},
) {
  return requestJson<SkillInfo>('/v1/skills/import', {
    ...options,
    body: { markdown },
    method: 'POST',
  })
}

// --- Chat-history persistence (project tier) -------------------------------
//
// Thin wire wrappers over /v1/chat/threads(+/messages) and
// /v1/chat/thread-groups. They speak the SERVER shape verbatim: snake_case
// keys and unix-seconds FLOAT timestamps. The DTO<->ProjectState-record
// conversion (epoch<->ISO, message metadata pack/unpack, group membership)
// lives in features/chat/chatHistorySync.ts, the way fromRunSummary converts
// the run wire shape — the client stays a transport layer.

/** One chat thread as the server stores it (timestamps = unix seconds). */
export type ServerChatThread = import('@/features/sessionTitle/model').TitleWire & {
  id: string
  title: string
  preview: string
  source: string
  group_id: string | null
  created_at: number
  updated_at: number
  /** Client-owned JSON carrying the thread's picked model; '' or absent
   * (older server) means nothing was picked here. */
  model_selection?: string
}

/** One chat message as the server stores it. ``metadata`` holds the verbatim
 * optional record fields (attachments / chainTrace / modelResolution). */
export type ServerChatMessage = {
  id: string
  thread_id: string
  role: string
  content_markdown: string
  metadata?: Record<string, unknown>
  created_at: number
}

/** One chat-thread group as the server stores it. */
export type ServerChatThreadGroup = {
  id: string
  title: string
  created_at: number
  updated_at: number
}

type PageOptions = ClientOptions & { cursor?: string; limit?: number }

export type EditorDocumentListOptions = PageOptions & {
  /** Omitted preserves the server's owned-only default. */
  scope?: 'all' | 'owned'
}

function pageQuery(options: PageOptions): string {
  const params = new URLSearchParams()
  if (options.cursor) params.set('cursor', options.cursor)
  if (options.limit !== undefined) params.set('limit', String(options.limit))
  const query = params.toString()
  return query ? `?${query}` : ''
}

function editorDocumentListQuery(options: EditorDocumentListOptions): string {
  const params = new URLSearchParams()
  if (options.cursor) params.set('cursor', options.cursor)
  if (options.limit !== undefined) params.set('limit', String(options.limit))
  if (options.scope) params.set('scope', options.scope)
  const query = params.toString()
  return query ? `?${query}` : ''
}

/** One keyset page of the caller's chat threads (newest first). */
export async function listChatThreads(options: PageOptions = {}) {
  return requestJson<{ data: ServerChatThread[]; next_cursor: string | null }>(
    `/v1/chat/threads${pageQuery(options)}`,
    options,
  )
}

/** Create or idempotently update one thread (the autosave upsert). */
export async function saveChatThread(
  threadId: string,
  payload: {
    title: string
    title_revision?: number
    title_source?: import('@/features/sessionTitle/model').TitleSource
    preview: string
    source: string
    group_id: string | null
    created_at: number
    updated_at: number
    /** Whole-row PUT: must ride every save or the server resets it to ''. */
    model_selection: string
  },
  options: ClientOptions = {},
) {
  return requestJson<ServerChatThread>(`/v1/chat/threads/${threadId}`, {
    ...options,
    body: payload,
    method: 'PUT',
  })
}

/** Delete one thread and its messages (owner-only; idempotent). */
export async function deleteChatThread(
  threadId: string,
  options: ClientOptions = {},
) {
  await request(`/v1/chat/threads/${threadId}`, {
    ...options,
    method: 'DELETE',
  })
}

/** One keyset page of a thread's messages (newest first). */
export async function listChatMessages(
  threadId: string,
  options: PageOptions = {},
) {
  return requestJson<{ data: ServerChatMessage[]; next_cursor: string | null }>(
    `/v1/chat/threads/${threadId}/messages${pageQuery(options)}`,
    options,
  )
}

/** Append/idempotently upsert messages into a thread. */
export async function appendChatMessages(
  threadId: string,
  messages: Array<{
    id: string
    role: string
    content_markdown: string
    metadata?: Record<string, unknown>
    created_at: number
  }>,
  options: ClientOptions = {},
) {
  return requestJson<{ data: ServerChatMessage[] }>(
    `/v1/chat/threads/${threadId}/messages`,
    { ...options, body: { messages }, method: 'POST' },
  )
}

/** Delete one message from a thread (edit access; idempotent — a missing
 * message is a quiet 204, so an autosave re-issue never wedges the loop). */
export async function deleteChatMessage(
  threadId: string,
  messageId: string,
  options: ClientOptions = {},
) {
  await request(`/v1/chat/threads/${threadId}/messages/${messageId}`, {
    ...options,
    method: 'DELETE',
  })
}

/** All of the caller's chat-thread groups (newest first). */
export async function listChatThreadGroups(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: ServerChatThreadGroup[] }>(
    '/v1/chat/thread-groups',
    options,
  )
  return payload.data
}

/** Create or idempotently update one thread group. */
export async function saveChatThreadGroup(
  groupId: string,
  payload: { title: string; created_at: number; updated_at: number },
  options: ClientOptions = {},
) {
  return requestJson<ServerChatThreadGroup>(
    `/v1/chat/thread-groups/${groupId}`,
    { ...options, body: payload, method: 'PUT' },
  )
}

/** Delete one thread group (its threads orphan to ungrouped). */
export async function deleteChatThreadGroup(
  groupId: string,
  options: ClientOptions = {},
) {
  await request(`/v1/chat/thread-groups/${groupId}`, {
    ...options,
    method: 'DELETE',
  })
}

// --- Knowledge-session persistence ---------------------------------------

export type ServerKnowledgeSession = {
  id: string
  title: string
  group_id: string | null
  created_at: number
  updated_at: number
  items_json?: string | null
  lifecycle_status?: 'active' | 'deleting' | 'delete_failed'
  deletion_operation_id?: string | null
  deletion_stage?: string | null
  deletion_error?: string | null
}

export type ServerKnowledgeSessionGroup = {
  id: string
  title: string
  created_at: number
  updated_at: number
}

export async function listKnowledgeSessions(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: ServerKnowledgeSession[] }>(
    '/v1/knowledge-sessions',
    options,
  )
  return payload.data
}

export async function getKnowledgeSession(
  sessionId: string,
  options: ClientOptions = {},
) {
  return requestJson<ServerKnowledgeSession>(
    `/v1/knowledge-sessions/${sessionId}`,
    options,
  )
}

export async function saveKnowledgeSession(
  sessionId: string,
  payload: {
    title: string
    items_json: string
    group_id: string | null
    created_at: number
    updated_at: number
  },
  options: ClientOptions = {},
) {
  return requestJson<ServerKnowledgeSession>(`/v1/knowledge-sessions/${sessionId}`, {
    ...options,
    body: payload,
    method: 'PUT',
  })
}

export async function listKnowledgeSessionGroups(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: ServerKnowledgeSessionGroup[] }>(
    '/v1/knowledge-session-groups',
    options,
  )
  return payload.data
}

export async function saveKnowledgeSessionGroup(
  groupId: string,
  payload: {
    title: string
    created_at: number
    updated_at: number
  },
  options: ClientOptions = {},
) {
  return requestJson<ServerKnowledgeSessionGroup>(`/v1/knowledge-session-groups/${groupId}`, {
    ...options,
    body: payload,
    method: 'PUT',
  })
}

export async function deleteKnowledgeSessionGroup(
  groupId: string,
  options: ClientOptions = {},
) {
  await request(`/v1/knowledge-session-groups/${groupId}`, {
    ...options,
    method: 'DELETE',
  })
}

export async function deleteKnowledgeSession(
  sessionId: string,
  options: ClientOptions = {},
) {
  const response = await request(`/v1/knowledge-sessions/${sessionId}`, {
    ...options,
    method: 'DELETE',
  })
  if (response.status === 204) return null
  return (await response.json()) as ServerDeletionOperation
}

// --- Editor-history persistence (project tier) -----------------------------
//
// Thin wire wrappers over /v1/editor/documents(+/comments) and
// /v1/editor/folders. Same conventions as the chat client (snake_case,
// unix-seconds float timestamps); the DTO<->record conversion lives in
// features/editor/editorSync.ts. The document body is excluded from the
// list shape and present only on getEditorDocument (load-on-open).

/** One editor document as the server stores it. ``content_markdown`` is the
 * heavy body — present on getEditorDocument, ABSENT on the list. */
export type ServerEditorAccess = {
  mode: 'owner' | 'shared'
  owner?: {
    id: string
    name: string
  }
  permission: 'edit' | 'suggest' | 'view'
}

export type ServerEditorCollaboration = {
  comment_revision?: number
  generation: number
  persisted_sequence: number
  projection_sequence: number
  projection_updated_at: number | null
  schema_version: number
}

export type ServerEditorDocument = {
  id: string
  title: string
  content_markdown?: string
  folder_id: string | null
  source: string
  source_run_id: string | null
  revision: number
  diff_anchor_markdown: string | null
  diff_anchor_updated_at: number | null
  created_at: number
  updated_at: number
  /** Added by collaboration-capable servers; absent means legacy markdown. */
  content_mode?: 'collaboration' | 'markdown'
  /** Independent compare-and-swap revision for title/folder metadata. */
  metadata_revision?: number
  /** Live caller relationship; absent on pre-collaboration servers. */
  access?: ServerEditorAccess
  collaboration?: ServerEditorCollaboration | null
}

export type EditorCollaborationUser = {
  color: string
  id: string
  kind?: 'guest' | 'user'
  link_label?: string
  name: string
}

export type EditorCollaborationSession = {
  access: 'comment' | 'edit' | 'suggest' | 'view'
  expires_at: number
  initial_write_mode: 'comment' | 'edit' | 'suggest' | 'view'
  lease_token: string
  /** Largest WebSocket frame the collaboration service accepts, in bytes. */
  max_frame_bytes?: number
  /** Inbound bytes the service holds for one socket. */
  max_queued_bytes?: number
  /** Inbound frames the service queues for one socket. */
  max_queued_frames?: number
  provider_flush_ms?: number
  protocol_version: number
  refresh_after?: number
  room: string
  schema_version: number
  /** Update messages one connection may send per rate window. */
  update_rate_count?: number
  update_rate_window_seconds?: number
  user: EditorCollaborationUser
  websocket_path: string
}

export type EditorCollaborationActivity = {
  actor: EditorCollaborationCommentActor
  actor_kind: 'assistant' | 'agent' | 'guest' | 'human' | 'system'
  command_id: string | null
  comment_action?: 'created' | 'message_deleted' | 'message_edited' | 'reopened' | 'replied' | 'resolved'
  created_at: number
  from_sequence: number
  outcome?: 'accepted' | 'rejected' | null
  summary?: {
    edits: Array<{
      after: string
      before: string
      kind: 'deletion' | 'direct' | 'format' | 'insertion' | 'replacement' | 'structure'
      position: number
    }>
    omitted_edit_count: number
  }
  suggestion_ids: string[]
  /** How many pending suggestions this edit removed by deleting the text
   * they proposed; the list names up to twenty of them with their authors. */
  rejected_suggestion_count?: number
  rejected_suggestions?: EditorCollaborationRejectedSuggestion[]
  to_sequence: number
  type: 'comment' | 'decision' | 'direct' | 'suggestion' | 'system'
  update_count?: number
}

export type EditorCollaborationRejectedSuggestion = {
  author: EditorCollaborationCommentActor
  patch_id: string
  suggestion_id: string
}

export type EditorCollaborationCommentActor = {
  id: string | null
  /** `deleted`: the placeholder an erased account's work now belongs to;
   * its stored name is not shown, the interface names it. */
  kind?: 'deleted' | 'guest' | 'user'
  link_label?: string | null
  name: string
}

export type EditorGuestLinkDescription = {
  document_title: string
  expires_at: number
  label: string
  password_required: true
  permission: EditorShareLinkPermission
}

export type EditorGuestAccessSession = {
  document: {
    comment_revision: number
    content_markdown: string
    generation: number
    id: string
    persisted_sequence: number
    projection_sequence: number
    title: string
  }
  expires_at: number
  guest: {
    display_name: string | null
    id: string
    link_label: string
  }
  permission: EditorShareLinkPermission
}

export type EditorCollaborationCommentMessage = {
  author: EditorCollaborationCommentActor
  body_markdown: string | null
  can_delete: boolean
  can_edit: boolean
  created_at: number
  deleted_at: number | null
  edited_at: number | null
  id: string
  mentions: EditorCollaborationCommentActor[]
  revision: number
}

export type EditorCollaborationCommentThread = {
  anchor: Record<string, unknown>
  author: EditorCollaborationCommentActor
  /** Only the author may retire a whole thread: unlike resolving, it takes
   * other people's replies out of view. */
  can_delete: boolean
  can_resolve: boolean
  created_at: number
  /** Set once the thread was retired. A fresh load never carries such a
   * thread; an incremental page carries it exactly once, so a client holding
   * the card knows to drop it. */
  deleted_at: number | null
  document_id: string
  generation: number
  id: string
  messages: EditorCollaborationCommentMessage[]
  quote: string
  resolved_at: number | null
  resolved_by: EditorCollaborationCommentActor | null
  revision: number
  status: 'open' | 'resolved'
  updated_at: number
}

export type EditorCollaborationCommentList = {
  current_revision?: number
  data: EditorCollaborationCommentThread[]
  has_more?: boolean
  last_read_revision: number
  object: 'list'
  participants: EditorCollaborationCommentActor[]
  revision: number
}

export type EditorCollaborationCommentMutation = {
  revision: number
  thread: EditorCollaborationCommentThread
}

export type EditorCollaborationCommentCommand = {
  command_id: string
  expected_revision: number
  generation: number
}

export type EditorCollaborationCommentMessageCommand =
  EditorCollaborationCommentCommand & {
    body_markdown: string
    mention_user_ids: string[]
  }

export type EditorCollaborationCommentCreateCommand =
  EditorCollaborationCommentMessageCommand & {
    anchor: Record<string, unknown>
    message_id: string
    quote: string
    thread_id: string
  }

export type EditorDocumentMetadataPatch = {
  diff_anchor_markdown?: string | null
  diff_anchor_updated_at?: number | null
  expected_metadata_revision: number
  folder_id?: string | null
  title?: string
}

export type EditorCollaborationEnableRequest = {
  expected_metadata_revision: number
  expected_revision: number
  schema_version: number
}

export type EditorCollaborationEnableResponse = {
  content_mode: 'collaboration'
  generation: number
  persisted_sequence: number
  projection_sequence: number
  schema_hash: string
  schema_version: number
}

export type EditorCollaborationSessionRequest = {
  /** Omitted for initial join; supplied only to rotate the same lease. */
  lease_token?: string
  protocol_version: number
  /** Stable across retries so a lost rotation response can be reconstructed. */
  rotation_command_id?: string
  schema_version: number
}

export type EditorCollaborationProjection = {
  authoritative_sequence?: number
  content_markdown: string
  generation: number
  projection_hash: string
  sequence: number
}

export type EditorCollaborationDecisionRequest = {
  decision: 'accept' | 'reject'
  decision_id: string
  expected_sequence: number
  patch_ids: string[]
}

export type EditorCollaborationDecisionResponse = {
  decision_id: string
  sequence: number
  suggestion_ids: string[]
}

export type EditorCollaborationSuggestionPublishRequest = {
  actor_kind: 'assistant'
  /** The projection `target_markdown` was built on, at `expected_sequence`.
   * The collaboration service places the AI change onto the document as it
   * is now and refuses with `base_changed` only when another writer changed
   * that very passage in the meantime. */
  base_markdown: string
  command_id: string
  expected_sequence: number
  patch_id: string
  target_markdown: string
}

export type EditorCollaborationSuggestionPublishResponse = {
  command_id: string
  patch_id: string
  sequence: number
  suggestion_ids: string[]
}

export type EditorSuggestionDraftRevisionWire = {
  change_summary: string[]
  created_at: number
  instruction: string | null
  proposed_text: string
  source: 'llm_refine' | 'manual_edit'
  warnings: string[]
}

export type EditorPrivateSuggestionDraftWire = {
  anchor_text?: string | null
  anchor_version: 1
  change_summary: string[]
  created_at: number
  edit_position?: 'replace' | 'before' | 'after' | 'append' | null
  evidence: {
    mode: 'add_sources' | 'fact_check' | 'verify_citations'
    sources: Array<{ title: string; url: string }>
  } | null
  group_id: string
  patch_id: string
  proposed_text: string
  publication_command_id: string
  revision: number
  revision_history: EditorSuggestionDraftRevisionWire[]
  suggestion_id: string
  updated_at: number
  warnings: string[]
}

export type EditorSuggestionDraftCreateWire = {
  // Nur bei der ERSTANLAGE gesetzt: Ankerstelle und Einfuegeart gehoeren zur
  // Identitaet des Vorschlags und duerfen von einer Revision nicht wandern.
  anchor_text?: string
  anchor_version: 1
  change_summary: string[]
  edit_position?: 'replace' | 'before' | 'after' | 'append'
  evidence: EditorPrivateSuggestionDraftWire['evidence']
  group_id: string
  patch_id: string
  proposed_text: string
  publication_command_id: string
  suggestion_id: string
  warnings: string[]
}

export type EditorSuggestionDraftRevisionRequestWire = {
  change_summary: string[]
  evidence?: EditorPrivateSuggestionDraftWire['evidence']
  instruction?: string
  proposed_text: string
  revision_source: 'llm_refine' | 'manual_edit'
  warnings: string[]
}

/** One editor folder as the server stores it. */
export type ServerEditorFolder = {
  id: string
  title: string
  created_at: number
  updated_at: number
}

/** One editor comment as the server stores it. */
export type ServerEditorComment = {
  id: string
  document_id: string
  comment_markdown: string
  anchor: Record<string, unknown>
  kind: string
  status: string
  suggestion_draft?: EditorPrivateSuggestionDraftWire | null
  evidence_preset: string | null
  created_at: number
  updated_at: number
}

/** One keyset page of documents (owned-only by default, METADATA only). */
export async function listEditorDocuments(options: EditorDocumentListOptions = {}) {
  return requestJson<{ data: ServerEditorDocument[]; next_cursor: string | null }>(
    `/v1/editor/documents${editorDocumentListQuery(options)}`,
    options,
  )
}

/** One document WITH its body (load-on-open). */
export async function getEditorDocument(
  documentId: string,
  options: ClientOptions = {},
) {
  return requestJson<ServerEditorDocument>(
    `/v1/editor/documents/${documentId}`,
    options,
  )
}

/** Create or idempotently update one document (autosave upsert, with body). */
export async function saveEditorDocument(
  documentId: string,
  payload: {
    title: string
    content_markdown: string
    folder_id: string | null
    source: string
    source_run_id: string | null
    revision: number
    diff_anchor_markdown: string | null
    diff_anchor_updated_at: number | null
    created_at: number
    updated_at: number
  },
  options: ClientOptions = {},
) {
  return requestJson<ServerEditorDocument>(`/v1/editor/documents/${documentId}`, {
    ...options,
    body: payload,
    method: 'PUT',
  })
}

/** Update title/folder metadata without touching the authoritative body. */
export async function patchEditorDocumentMetadata(
  documentId: string,
  payload: EditorDocumentMetadataPatch,
  options: ClientOptions = {},
) {
  return requestJson<ServerEditorDocument>(`/v1/editor/documents/${documentId}`, {
    ...options,
    body: payload,
    method: 'PATCH',
  })
}

/** Atomically convert an owner-only markdown document to Yjs collaboration. */
export async function enableEditorDocumentCollaboration(
  documentId: string,
  payload: EditorCollaborationEnableRequest,
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationEnableResponse>(
    `/v1/editor/documents/${documentId}/collaboration:enable`,
    {
      ...options,
      body: payload,
      method: 'POST',
    },
  )
}

/** Issue or rotate the current browser tab's collaboration lease. */
export async function createEditorCollaborationSession(
  documentId: string,
  payload: EditorCollaborationSessionRequest,
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationSession>(
    `/v1/editor/documents/${documentId}/collaboration/session`,
    {
      ...options,
      body: payload,
      method: 'POST',
      reloadOnUnauthorized: false,
    },
  )
}

/** Public, account-less guest-link metadata. The raw token is never persisted. */
export async function describeEditorGuestLink(
  token: string,
  options: ClientOptions = {},
) {
  return requestJson<EditorGuestLinkDescription>(
    `/v1/editor/share-links/${encodeURIComponent(token)}`,
    { ...options, reloadOnUnauthorized: false },
  )
}

/** Unlock one guest link and establish its scoped HttpOnly session cookie. */
export async function unlockEditorGuestLink(
  token: string,
  payload: { display_name?: string; password: string },
  options: ClientOptions = {},
) {
  return requestJson<EditorGuestAccessSession>(
    `/v1/editor/share-links/${encodeURIComponent(token)}:unlock`,
    {
      ...options,
      body: payload,
      method: 'POST',
      reloadOnUnauthorized: false,
    },
  )
}

/** Restore a still-valid scoped guest session without touching account auth. */
export async function getEditorGuestSession(options: ClientOptions = {}) {
  return requestJson<EditorGuestAccessSession>(
    '/v1/editor/guest/session',
    { ...options, reloadOnUnauthorized: false },
  )
}

/** Issue or rotate the guest's link-bound collaboration lease. */
export async function createGuestEditorCollaborationSession(
  payload: EditorCollaborationSessionRequest & { display_name?: string },
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationSession>(
    '/v1/editor/guest/collaboration/session',
    {
      ...options,
      body: payload,
      method: 'POST',
      reloadOnUnauthorized: false,
    },
  )
}

export async function listGuestEditorCollaborationComments(
  query: {
    limit?: number
    sinceRevision?: number
    status?: 'all' | 'open' | 'resolved'
  } = {},
  options: ClientOptions = {},
) {
  const params = new URLSearchParams({
    limit: String(query.limit ?? 50),
    since_revision: String(query.sinceRevision ?? 0),
    status: query.status ?? 'all',
  })
  return requestJson<EditorCollaborationCommentList>(
    `/v1/editor/guest/collaboration/comments?${params.toString()}`,
    { ...options, reloadOnUnauthorized: false },
  )
}

export async function createGuestEditorCollaborationComment(
  payload: EditorCollaborationCommentCreateCommand,
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationCommentMutation>(
    '/v1/editor/guest/collaboration/comments',
    {
      ...options,
      body: payload,
      method: 'POST',
      reloadOnUnauthorized: false,
    },
  )
}

export async function replyToGuestEditorCollaborationComment(
  threadId: string,
  payload: EditorCollaborationCommentMessageCommand & { message_id: string },
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationCommentMutation>(
    `/v1/editor/guest/collaboration/comments/${threadId}/replies`,
    {
      ...options,
      body: payload,
      method: 'POST',
      reloadOnUnauthorized: false,
    },
  )
}

export async function updateGuestEditorCollaborationCommentThread(
  threadId: string,
  payload: EditorCollaborationCommentCommand & {
    status: 'open' | 'resolved'
  },
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationCommentMutation>(
    `/v1/editor/guest/collaboration/comments/${threadId}`,
    {
      ...options,
      body: payload,
      method: 'PATCH',
      reloadOnUnauthorized: false,
    },
  )
}

export async function updateGuestEditorCollaborationCommentMessage(
  threadId: string,
  messageId: string,
  payload: EditorCollaborationCommentMessageCommand,
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationCommentMutation>(
    `/v1/editor/guest/collaboration/comments/${threadId}/messages/${messageId}`,
    {
      ...options,
      body: payload,
      method: 'PATCH',
      reloadOnUnauthorized: false,
    },
  )
}

export async function deleteGuestEditorCollaborationCommentMessage(
  threadId: string,
  messageId: string,
  payload: EditorCollaborationCommentCommand,
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationCommentMutation>(
    `/v1/editor/guest/collaboration/comments/${threadId}/messages/${messageId}`,
    {
      ...options,
      body: payload,
      method: 'DELETE',
      reloadOnUnauthorized: false,
    },
  )
}

export async function markGuestEditorCollaborationCommentsRead(
  revision: number,
  options: ClientOptions = {},
) {
  return requestJson<{ last_read_revision: number }>(
    '/v1/editor/guest/collaboration/comments/read',
    {
      ...options,
      body: { revision },
      method: 'POST',
      reloadOnUnauthorized: false,
    },
  )
}

/** Compact, content-bounded durable activity for a future inspector surface. */
export async function listEditorCollaborationActivity(
  documentId: string,
  options: PageOptions = {},
) {
  return requestJson<{
    data: EditorCollaborationActivity[]
    next_cursor: string | null
    object: 'list'
  }>(`/v1/editor/documents/${documentId}/activity${pageQuery(options)}`, options)
}

/** Incrementally load durable, document-shared discussion threads. */
export async function listEditorCollaborationComments(
  documentId: string,
  query: {
    limit?: number
    sinceRevision?: number
    status?: 'all' | 'open' | 'resolved'
  } = {},
  options: ClientOptions = {},
) {
  const params = new URLSearchParams({
    limit: String(query.limit ?? 100),
    since_revision: String(query.sinceRevision ?? 0),
    status: query.status ?? 'all',
  })
  return requestJson<EditorCollaborationCommentList>(
    `/v1/editor/documents/${documentId}/collaboration/comments?${params}`,
    options,
  )
}

/** Create one shared thread anchored in the current Yjs generation. */
export async function createEditorCollaborationComment(
  documentId: string,
  payload: EditorCollaborationCommentCreateCommand,
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationCommentMutation>(
    `/v1/editor/documents/${documentId}/collaboration/comments`,
    { ...options, body: payload, method: 'POST' },
  )
}

/** Add one real-time reply to a shared thread. */
export async function replyToEditorCollaborationComment(
  documentId: string,
  threadId: string,
  payload: EditorCollaborationCommentMessageCommand & { message_id: string },
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationCommentMutation>(
    `/v1/editor/documents/${documentId}/collaboration/comments/${threadId}/replies`,
    { ...options, body: payload, method: 'POST' },
  )
}

/** Edit one own shared-comment contribution. */
export async function updateEditorCollaborationCommentMessage(
  documentId: string,
  threadId: string,
  messageId: string,
  payload: EditorCollaborationCommentMessageCommand,
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationCommentMutation>(
    `/v1/editor/documents/${documentId}/collaboration/comments/${threadId}/messages/${messageId}`,
    { ...options, body: payload, method: 'PATCH' },
  )
}

/** Delete one own contribution while retaining its audit tombstone. */
export async function deleteEditorCollaborationCommentMessage(
  documentId: string,
  threadId: string,
  messageId: string,
  payload: EditorCollaborationCommentCommand,
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationCommentMutation>(
    `/v1/editor/documents/${documentId}/collaboration/comments/${threadId}/messages/${messageId}`,
    { ...options, body: payload, method: 'DELETE' },
  )
}

/** Resolve or reopen one shared discussion. */
export async function updateEditorCollaborationCommentThread(
  documentId: string,
  threadId: string,
  payload: EditorCollaborationCommentCommand & {
    status: 'open' | 'resolved'
  },
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationCommentMutation>(
    `/v1/editor/documents/${documentId}/collaboration/comments/${threadId}`,
    { ...options, body: payload, method: 'PATCH' },
  )
}

/** Retire a whole discussion its author started by accident.
 *
 * Resolving hides a discussion that happened; this is for one that was never
 * meant to exist. The server keeps the row for the audit and stops returning
 * it, so the card disappears for everyone in the document. Author only. */
export async function deleteEditorCollaborationCommentThread(
  documentId: string,
  threadId: string,
  payload: EditorCollaborationCommentCommand,
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationCommentMutation>(
    `/v1/editor/documents/${documentId}/collaboration/comments/${threadId}`,
    { ...options, body: payload, method: 'DELETE' },
  )
}

/** Persist the personal document-scoped comment read coordinate. */
export async function markEditorCollaborationCommentsRead(
  documentId: string,
  payload: { generation: number; revision: number },
  options: ClientOptions = {},
) {
  return requestJson<{ last_read_revision: number }>(
    `/v1/editor/documents/${documentId}/collaboration/comments/read`,
    { ...options, body: payload, method: 'POST' },
  )
}

/** Drain durable updates and publish the canonical markdown projection. */
export async function flushEditorCollaborationProjection(
  documentId: string,
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationProjection>(
    `/v1/editor/documents/${documentId}/collaboration/projection:flush`,
    {
      ...options,
      method: 'POST',
    },
  )
}

/** Publish one private assistant result as an attributable shared suggestion. */
export async function publishEditorCollaborationSuggestion(
  documentId: string,
  payload: EditorCollaborationSuggestionPublishRequest,
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationSuggestionPublishResponse>(
    `/v1/editor/documents/${documentId}/suggestions:publish`,
    {
      ...options,
      body: payload,
      method: 'POST',
    },
  )
}

/** Apply one idempotent batch decision through the authoritative Yjs room. */
export async function decideEditorCollaborationPatches(
  documentId: string,
  payload: EditorCollaborationDecisionRequest,
  options: ClientOptions = {},
) {
  return requestJson<EditorCollaborationDecisionResponse>(
    `/v1/editor/documents/${documentId}/patches:decide`,
    {
      ...options,
      body: payload,
      method: 'POST',
    },
  )
}

/** Delete one document and its comments (owner-only; idempotent). */
export async function deleteEditorDocument(
  documentId: string,
  options: ClientOptions = {},
) {
  await request(`/v1/editor/documents/${documentId}`, {
    ...options,
    method: 'DELETE',
  })
}

/** One keyset page of a document's comments (newest first). */
export async function listEditorComments(
  documentId: string,
  options: PageOptions = {},
) {
  return requestJson<{ data: ServerEditorComment[]; next_cursor: string | null }>(
    `/v1/editor/documents/${documentId}/comments${pageQuery(options)}`,
    options,
  )
}

/** Upsert comments into a document. */
export async function saveEditorComments(
  documentId: string,
  comments: Array<{
    id: string
    comment_markdown: string
    anchor: Record<string, unknown>
    kind: string
    status: string
    evidence_preset: string | null
    created_at: number
    updated_at: number
  }>,
  options: ClientOptions = {},
) {
  return requestJson<{ data: ServerEditorComment[] }>(
    `/v1/editor/documents/${documentId}/comments`,
    { ...options, body: { comments }, method: 'POST' },
  )
}

/** Delete one comment from a document. */
export async function deleteEditorComment(
  documentId: string,
  commentId: string,
  options: ClientOptions = {},
) {
  await request(
    `/v1/editor/documents/${documentId}/comments/${commentId}`,
    { ...options, method: 'DELETE' },
  )
}

/** Create or revise one unpublished suggestion visible only to its comment creator. */
export async function saveEditorCommentSuggestionDraft(
  documentId: string,
  commentId: string,
  payload: {
    draft: EditorSuggestionDraftCreateWire | EditorSuggestionDraftRevisionRequestWire
    expected_revision: number
  },
  options: ClientOptions = {},
) {
  return requestJson<{ suggestion_draft: EditorPrivateSuggestionDraftWire }>(
    `/v1/editor/documents/${documentId}/comments/${commentId}/suggestion-draft`,
    { ...options, body: payload, method: 'PUT' },
  )
}

/** Discard one creator-private draft only when both revision and patch match. */
export async function deleteEditorCommentSuggestionDraft(
  documentId: string,
  commentId: string,
  payload: { expected_revision: number; patch_id: string },
  options: ClientOptions = {},
) {
  await request(
    `/v1/editor/documents/${documentId}/comments/${commentId}/suggestion-draft`,
    { ...options, body: payload, method: 'DELETE' },
  )
}

/** All of the caller's editor folders (newest first). */
export async function listEditorFolders(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: ServerEditorFolder[] }>(
    '/v1/editor/folders',
    options,
  )
  return payload.data
}

/** Create or idempotently update one editor folder. */
export async function saveEditorFolder(
  folderId: string,
  payload: { title: string; created_at: number; updated_at: number },
  options: ClientOptions = {},
) {
  return requestJson<ServerEditorFolder>(`/v1/editor/folders/${folderId}`, {
    ...options,
    body: payload,
    method: 'PUT',
  })
}

/** Delete one editor folder (its documents orphan to ungrouped). */
export async function deleteEditorFolder(
  folderId: string,
  options: ClientOptions = {},
) {
  await request(`/v1/editor/folders/${folderId}`, {
    ...options,
    method: 'DELETE',
  })
}

// -- file-asset records ----------------------------------------------------
//
// Thin wire wrappers over /v1/assets/sections, /v1/assets/groups and
// /v1/assets(+/{id}). Same conventions as the editor client (snake_case,
// unix-seconds float timestamps); the DTO<->record conversion lives in
// features/fileLibrary/assetSync.ts. The heavy extracted_text is excluded
// from the asset LIST and present only on getAsset (load-on-use).

/** One file-library section as the server stores it. */
export type ServerAssetSection = {
  id: string
  kind: string
  title: string
  semantic_role: 'temporary' | 'library' | 'project_sources' | 'custom' | null
  created_at: number
  updated_at: number
}

/** One file-library group as the server stores it. */
export type ServerAssetGroup = {
  id: string
  section_id: string
  title: string
  created_at: number
  updated_at: number
}

/** One file-asset record as the server stores it. Heavy editable and canonical
 * prepared bodies are present on getAsset and absent on list metadata rows. */
export type ServerAsset = {
  id: string
  section_id: string
  group_id: string | null
  title: string
  label: string
  file_name: string
  mime_type: string
  origin: string
  page_count: number | null
  parse_status: string
  parse_warning: string | null
  text_truncated: boolean
  size_bytes: number
  server_file_id: string | null
  // Optional on the wire: a server predating the provenance field omits it.
  parser_id?: string | null
  prepared_parser_id?: string | null
  prepared_content_hash?: string | null
  prepared_at?: number | null
  lifecycle_status?: 'active' | 'deleting' | 'delete_failed'
  deletion_operation_id?: string | null
  deletion_stage?: string | null
  deletion_error?: string | null
  upload_status?:
    | 'awaiting_upload'
    | 'uploading'
    | 'retrying'
    | 'parsing'
    | 'finalizing'
    | 'ready'
    | 'failed'
    | 'cancelled'
  upload_error?: string | null
  upload_operation_id?: string | null
  extracted_text?: string
  prepared_text?: string
  created_at: number
  updated_at: number
}

/** All of the caller's file-library sections. */
export async function listAssetSections(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: ServerAssetSection[] }>(
    '/v1/assets/sections',
    options,
  )
  return payload.data
}

/** Converge concurrent first-load clients on the scope's prepared sections. */
export async function ensureDefaultAssetSections(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: ServerAssetSection[] }>(
    '/v1/assets/default-sections',
    { ...options, method: 'PUT' },
  )
  return payload.data
}

/** Create or idempotently update one section. */
export async function saveAssetSection(
  sectionId: string,
  payload: { kind: string; title: string; created_at: number; updated_at: number },
  options: ClientOptions = {},
) {
  return requestJson<ServerAssetSection>(`/v1/assets/sections/${sectionId}`, {
    ...options,
    body: payload,
    method: 'PUT',
  })
}

export type ServerDeletionOperation = {
  operation_id: string
  target_kind:
    | 'asset'
    | 'bulk'
    | 'group'
    | 'section'
    | 'vector_index'
    | 'knowledge_collection'
    | 'knowledge_document'
    | 'agent_session'
    | 'knowledge_session'
    | 'user'
  target_id: string
  asset_ids: string[]
  status: 'queued' | 'running' | 'delete_failed' | 'deleted'
  stage:
    | 'queued'
    | 'aggregates_removed'
    | 'rows_removed'
    | 'vector_index_detached'
    | 'indexing_cancelled'
    | 'search_detached'
    | 'vectors_removed'
    | 'knowledge_removed'
    | 'blobs_removed'
    | 'metadata_removed'
    | 'session_data_removed'
    | 'residuals_verified'
    | 'delete_failed'
    | 'deleted'
  completed_items: number
  total_items: number
  attempt: number
  created_at: number
  started_at: number | null
  finished_at: number | null
  error: { message: string; type: string } | null
  retryable: boolean
}

/** Start the server-owned cleanup of a section and all contained assets. */
export async function deleteAssetSection(sectionId: string, options: ClientOptions = {}) {
  return requestJson<ServerDeletionOperation>(`/v1/assets/sections/${sectionId}`, {
    ...options,
    method: 'DELETE',
  })
}

/** All of the caller's file-library groups. */
export async function listAssetGroups(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: ServerAssetGroup[] }>(
    '/v1/assets/groups',
    options,
  )
  return payload.data
}

/** Create or idempotently update one group. */
export async function saveAssetGroup(
  groupId: string,
  payload: { section_id: string; title: string; created_at: number; updated_at: number },
  options: ClientOptions = {},
) {
  return requestJson<ServerAssetGroup>(`/v1/assets/groups/${groupId}`, {
    ...options,
    body: payload,
    method: 'PUT',
  })
}

/** Delete one group (its assets orphan to ungrouped server-side). */
export async function deleteAssetGroup(groupId: string, options: ClientOptions = {}) {
  return requestJson<ServerDeletionOperation>(`/v1/assets/groups/${groupId}`, {
    ...options,
    method: 'DELETE',
  })
}

/** One keyset page of the caller's assets (newest first, METADATA only). */
export async function listAssets(options: PageOptions = {}) {
  return requestJson<{ data: ServerAsset[]; next_cursor: string | null }>(
    `/v1/assets${pageQuery(options)}`,
    options,
  )
}

/** One asset WITH its extracted text (load-on-use). */
export async function getAsset(assetId: string, options: ClientOptions = {}) {
  return requestJson<ServerAsset>(`/v1/assets/${assetId}`, options)
}

/** Create or idempotently update one asset (with its extracted text). */
export async function saveAsset(
  assetId: string,
  payload: {
    section_id: string
    group_id: string | null
    title: string
    label: string
    file_name: string
    mime_type: string
    origin: string
    page_count: number | null
    parse_status: string
    parse_warning: string | null
    text_truncated: boolean
    size_bytes: number
    server_file_id: string | null
    parser_id: string | null
    extracted_text: string
    created_at: number
    updated_at: number
  },
  options: ClientOptions = {},
) {
  return requestJson<ServerAsset>(`/v1/assets/${assetId}`, {
    ...options,
    body: payload,
    method: 'PUT',
  })
}

/** Start one idempotent aggregate asset deletion. */
export async function deleteAsset(assetId: string, options: ClientOptions = {}) {
  return requestJson<ServerDeletionOperation>(`/v1/assets/${assetId}`, {
    ...options,
    method: 'DELETE',
  })
}

/** Start one aggregate deletion for a stable set of assets. */
export async function deleteAssets(assetIds: readonly string[], options: ClientOptions = {}) {
  return requestJson<ServerDeletionOperation>('/v1/assets/deletion-operations', {
    ...options,
    body: { asset_ids: assetIds },
    method: 'POST',
  })
}

/**
 * Read retained aggregate-deletion checkpoints for the current principal and
 * workspace. The feed includes active/failed operations and retained deleted
 * receipts: asset-list hydration alone cannot reveal an already removed row,
 * and empty-section operations have no child asset from which to resume.
 */
export async function listAssetDeletionOperations(options: PageOptions = {}) {
  return requestJson<{ data: ServerDeletionOperation[]; next_cursor: string | null }>(
    `/v1/assets/deletion-operations${pageQuery(options)}`,
    options,
  )
}

/** Read the authoritative checkpoint of a deletion operation. */
export async function getAssetDeletionOperation(
  operationId: string,
  options: ClientOptions = {},
) {
  return requestJson<ServerDeletionOperation>(
    `/v1/deletion-operations/${operationId}`,
    options,
  )
}

/** Retry the same failed operation and manifest; no replacement id is made. */
export async function retryAssetDeletionOperation(
  operationId: string,
  options: ClientOptions = {},
) {
  return requestJson<ServerDeletionOperation>(
    `/v1/deletion-operations/${operationId}/retry`,
    { ...options, method: 'POST' },
  )
}

// -- vector-index records ---------------------------------------------------
//
// Thin wire wrappers over /v1/vector-indexes(+/{id}). Same conventions as the
// asset client (snake_case, unix-seconds float timestamps); the DTO<->record
// conversion lives in features/fileLibrary/vectorIndexSync.ts. A vector index
// has no heavy lazy body — the list returns FULL records (members + history).

/** One vector-index member (referenced document) as the server stores it. */
export type ServerVectorIndexMember = {
  file_id: string
  state: string
  /** Backend knowledge-document id this member was ingested as, once known;
   * absent/null for members ingested before this was tracked. Lets "remove
   * from index" delete the exact document without a full rebuild. */
  server_document_id?: string | null
}

/** One past reindex run as the server stores it (newest first). */
export type ServerVectorIndexHistoryEntry = {
  result: string
  documents: number
  duration_ms: number
  error: string | null
  started_at: number
  finished_at: number
}

/** One vector index as the server stores it (full record incl. children). */
export type ServerVectorIndex = {
  id: string
  title: string
  handle: string
  model: string
  dims: number
  status: string
  server_collection_id: string | null
  server_collection_model: string | null
  last_error: string | null
  members: ServerVectorIndexMember[]
  history: ServerVectorIndexHistoryEntry[]
  created_at: number
  updated_at: number
}

/** One keyset page of the caller's vector indexes (newest first, FULL records). */
export async function listVectorIndexes(options: PageOptions = {}) {
  return requestJson<{ data: ServerVectorIndex[]; next_cursor: string | null }>(
    `/v1/vector-indexes${pageQuery(options)}`,
    options,
  )
}

/** Create or idempotently update one vector index (replaces its members +
 * history wholesale server-side). */
export async function saveVectorIndex(
  indexId: string,
  payload: {
    title: string
    handle: string
    model: string
    dims: number
    status: string
    server_collection_id: string | null
    server_collection_model: string | null
    last_error: string | null
    members: ServerVectorIndexMember[]
    history: ServerVectorIndexHistoryEntry[]
    created_at: number
    updated_at: number
  },
  options: ClientOptions = {},
) {
  return requestJson<ServerVectorIndex>(`/v1/vector-indexes/${indexId}`, {
    ...options,
    body: payload,
    method: 'PUT',
  })
}

/** Delete one vector index and its complete server aggregate (owner-only;
 * idempotent). A client-retained collection id lets the server recover a
 * binding whose preceding terminal autosave was interrupted; the server
 * re-authorizes that collection before including it in the durable operation. */
export async function deleteVectorIndex(
  indexId: string,
  options: ClientOptions = {},
  serverCollectionId?: string | null,
) {
  return requestJson<ServerDeletionOperation>(`/v1/vector-indexes/${indexId}`, {
    ...options,
    ...(serverCollectionId
      ? { body: { server_collection_id: serverCollectionId } }
      : {}),
    method: 'DELETE',
  })
}

// -- account preferences ----------------------------------------------------
//
// A single per-user settings row (theme/locale/contrast/bubble tone). NOT project data and
// NOT part of the project import — it follows the user across devices. GET
// returns 404 when the user has never saved; getAccountPreferences maps that
// to null so the caller keeps its own default (the defaults are a frontend
// SSOT, never fabricated server-side).

/** Account preferences as the server stores them.
 *
 * Fields added after the first release are OPTIONAL here on purpose: a row
 * written by an older server has no such key, and the reader falls back per
 * field rather than failing. The write side ({@link AccountPreferencesPayload})
 * is deliberately strict instead — see there. */
export type ServerAccountPreferences = {
  contrast_mode: string
  locale: string
  theme: string
  theme_preset: string
  user_bubble_tone?: string
  enable_agent_memory?: boolean
  chat_model_tier?: string
  agent_model_tier?: string
  updated_at: number
}

/** What a save must send — every field, always.
 *
 * The endpoint knows no PATCH: it replaces the whole row, so an omitted field
 * is reset to its server-side default. Making every field required is what
 * turns that silent reset into a compile error. */
export type AccountPreferencesPayload = {
  contrast_mode: string
  locale: string
  theme: string
  theme_preset: string
  user_bubble_tone: string
  enable_agent_memory: boolean
  chat_model_tier: string
  agent_model_tier: string
  updated_at: number
}

/** The caller's account preferences, or null when never saved (HTTP 404). */
export async function getAccountPreferences(
  options: ClientOptions = {},
): Promise<ServerAccountPreferences | null> {
  try {
    return await requestJson<ServerAccountPreferences>('/v1/account/preferences', options)
  } catch (error) {
    if (hasHttpStatus(error, 404)) return null
    throw error
  }
}

/** Create or idempotently update the caller's account preferences. */
export async function saveAccountPreferences(
  payload: AccountPreferencesPayload,
  options: ClientOptions = {},
) {
  return requestJson<ServerAccountPreferences>('/v1/account/preferences', {
    ...options,
    body: payload,
    method: 'PUT',
  })
}

/** Prefix typeahead over the tenant's user mirror (min 2 chars). */
export async function searchUsers(query: string, options: ClientOptions = {}) {
  const payload = await requestJson<{ data: UserSearchResult[] }>(
    `/v1/users/search?q=${encodeURIComponent(query)}`,
    options,
  )
  return payload.data
}

/** Active shares on one owned resource (profile-enriched rows). */
export async function listShares(
  resourceType: string,
  resourceId: string,
  options: ClientOptions = {},
) {
  const params = new URLSearchParams({
    resource_id: resourceId,
    resource_type: resourceType,
  })
  const payload = await requestJson<{ data: ShareRecordInfo[] }>(
    `/v1/shares?${params.toString()}`,
    options,
  )
  return payload.data
}

/** Grant shares to one or more users in a single batch. */
export async function createShares(
  resourceType: string,
  resourceId: string,
  invitees: ShareInvitee[],
  options: ClientOptions = {},
) {
  const payload = await requestJson<{ data: ShareRecordInfo[] }>('/v1/shares', {
    ...options,
    body: {
      invitees: invitees.map((invitee) => ({
        permission: invitee.permission,
        user_id: invitee.userId,
      })),
      resource_id: resourceId,
      resource_type: resourceType,
    },
    method: 'POST',
  })
  return payload.data
}

/** Revoke one share by id (404s are indistinct from absence). */
export async function revokeShare(shareId: string, options: ClientOptions = {}) {
  await request(`/v1/shares/${shareId}`, { ...options, method: 'DELETE' })
}

/** Compare-and-swap one active share's permission. Acceptance is retained. */
export async function updateShare(
  shareId: string,
  requestBody: { expectedRevision: number; permission: SharePermissionValue },
  options: ClientOptions = {},
) {
  const payload = await requestJson<{ data: ShareRecordInfo }>(`/v1/shares/${shareId}`, {
    ...options,
    body: {
      expected_revision: requestBody.expectedRevision,
      permission: requestBody.permission,
    },
    method: 'PATCH',
  })
  return payload.data
}

/** Owner-only HTTPS guest links for one collaboration document. */
export async function listEditorShareLinks(
  documentId: string,
  options: ClientOptions = {},
) {
  const payload = await requestJson<{ data: EditorShareLink[] }>(
    `/v1/editor/documents/${encodeURIComponent(documentId)}/share-links`,
    options,
  )
  return payload.data
}

export async function createEditorShareLink(
  documentId: string,
  requestBody: {
    commandId: string
    generation: number
    permission: EditorShareLinkPermission
    ttlSeconds: number
  },
  options: ClientOptions = {},
) {
  const payload = await requestJson<{ data: CreatedEditorShareLink }>(
    `/v1/editor/documents/${encodeURIComponent(documentId)}/share-links`,
    {
      ...options,
      body: {
        command_id: requestBody.commandId,
        generation: requestBody.generation,
        permission: requestBody.permission,
        ttl_seconds: requestBody.ttlSeconds,
      },
      method: 'POST',
    },
  )
  return payload.data
}

export async function updateEditorShareLink(
  documentId: string,
  linkId: string,
  requestBody: {
    commandId: string
    expectedRevision: number
    permission?: EditorShareLinkPermission
    ttlSeconds?: number
  },
  options: ClientOptions = {},
) {
  const payload = await requestJson<{ data: EditorShareLink }>(
    `/v1/editor/documents/${encodeURIComponent(documentId)}/share-links/${encodeURIComponent(linkId)}`,
    {
      ...options,
      body: {
        command_id: requestBody.commandId,
        expected_revision: requestBody.expectedRevision,
        ...(requestBody.permission
          ? { permission: requestBody.permission }
          : {}),
        ...(requestBody.ttlSeconds
          ? { ttl_seconds: requestBody.ttlSeconds }
          : {}),
      },
      method: 'PATCH',
    },
  )
  return payload.data
}

export async function revokeEditorShareLink(
  documentId: string,
  linkId: string,
  requestBody: { commandId: string; expectedRevision: number },
  options: ClientOptions = {},
) {
  const payload = await requestJson<{ data: EditorShareLink }>(
    `/v1/editor/documents/${encodeURIComponent(documentId)}/share-links/${encodeURIComponent(linkId)}`,
    {
      ...options,
      body: {
        command_id: requestBody.commandId,
        expected_revision: requestBody.expectedRevision,
      },
      method: 'DELETE',
    },
  )
  return payload.data
}

export async function rotateEditorShareLinkPassword(
  documentId: string,
  linkId: string,
  requestBody: { commandId: string; expectedRevision: number },
  options: ClientOptions = {},
) {
  const payload = await requestJson<{
    data: EditorShareLink & { password: string }
  }>(
    `/v1/editor/documents/${encodeURIComponent(documentId)}/share-links/${encodeURIComponent(linkId)}:rotate-password`,
    {
      ...options,
      body: {
        command_id: requestBody.commandId,
        expected_revision: requestBody.expectedRevision,
      },
      method: 'POST',
    },
  )
  return payload.data
}

export async function rotateEditorShareLinkToken(
  documentId: string,
  linkId: string,
  requestBody: { commandId: string; expectedRevision: number },
  options: ClientOptions = {},
) {
  const payload = await requestJson<{
    data: EditorShareLink & { url: string }
  }>(
    `/v1/editor/documents/${encodeURIComponent(documentId)}/share-links/${encodeURIComponent(linkId)}:rotate-token`,
    {
      ...options,
      body: {
        command_id: requestBody.commandId,
        expected_revision: requestBody.expectedRevision,
      },
      method: 'POST',
    },
  )
  return payload.data
}

export async function getEditorAccessSummary(
  documentId: string,
  window: '7d' | '30d',
  options: ClientOptions = {},
) {
  return requestJson<EditorAccessSummary>(
    `/v1/editor/documents/${encodeURIComponent(documentId)}/access-summary?window=${window}`,
    options,
  )
}

/** The caller's incoming shares, split into pending (consent queue) and
 * accepted (shared with me) — the sharing settings panel's source. */
export async function fetchSharingInbox(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: SharingInbox }>(
    '/v1/shares/inbox',
    options,
  )
  return payload.data
}

/** The resources the caller has shared out, grouped per resource. */
export async function fetchMyShares(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: OutgoingShare[] }>(
    '/v1/shares/mine',
    options,
  )
  return payload.data
}

/** Accept one pending incoming share (the recipient's consent). */
export async function acceptShare(shareId: string, options: ClientOptions = {}) {
  await request(`/v1/shares/${shareId}/accept`, { ...options, method: 'POST' })
}

/** One keyset page of visible research runs (newest first). */
export async function listResearchRuns(options: PageOptions = {}) {
  return requestJson<{ data: ResearchRunSummary[]; next_cursor: string | null }>(
    `/v1/runs${pageQuery(options)}`,
    options,
  )
}

/** One run's current summary. Unlike the list, this carries
 * `question_full` when the stored question was clipped. */
export async function fetchResearchRun(
  runId: string,
  options: ClientOptions = {},
) {
  return requestJson<ResearchRunSummary>(
    `/v1/runs/${encodeURIComponent(runId)}`,
    options,
  )
}

export async function createResearchRun(
  request: CreateResearchRunRequest,
  options: ClientOptions = {},
) {
  return requestJson<ResearchRunSummary>('/v1/runs', {
    ...options,
    method: 'POST',
    body: {
      question: request.question,
      messages: request.messages,
      stack: request.stack,
      mode: request.mode,
      workspace_id: options.workspaceId,
      agent_overrides: serializeOverrides(request.agentOverrides),
      knowledge_filters: serializeKnowledgeFilters(request.knowledgeFilters),
      autonomy: request.autonomy,
      session_id: request.sessionId,
      incognito: request.incognito ? true : undefined,
      ui_language: request.uiLanguage,
      document_id: request.documentId,
      response_form: request.responseForm,
      skill_ids: request.skillIds,
      tool_directives: request.toolDirectives,
      source_policy: request.sourcePolicy,
      canvas_context: request.canvasContext
        ? {
          artifact_id: request.canvasContext.artifactId,
          comments: request.canvasContext.comments.map((comment) => ({
            artifact_id: comment.artifactId,
            comment: comment.comment,
            quote: comment.quote,
            quote_after: comment.quoteAfter,
            quote_before: comment.quoteBefore,
            revision: comment.revision,
          })),
          revision: request.canvasContext.revision,
        }
        : undefined,
      // Ids only: the server resolves name, completion and visibility
      // from the caller's own runs.
      report_ids: request.reportIds?.length ? request.reportIds : undefined,
      // Ids only, and only when there is something to say: the server
      // resolves each rule from the caller's own catalog and refuses the
      // field on non-agent modes rather than dropping it.
      report_rule_ids: request.reportRuleIds?.length
        ? request.reportRuleIds
        : undefined,
    },
  })
}

export async function cancelResearchRun(
  runId: string,
  options: ClientOptions = {},
) {
  return requestJson<ResearchRunSummary>(`/v1/runs/${runId}/cancel`, {
    ...options,
    method: 'POST',
  })
}

/** Current summary of one run. Used to poll for the terminal transition
 * after a cancel (a cancel of a RUNNING run is asynchronous: the summary
 * stays `running` with `cancel_requested: true` until the worker stops). */
export async function fetchResearchRunSummary(
  runId: string,
  options: ClientOptions = {},
) {
  return requestJson<ResearchRunSummary>(`/v1/runs/${runId}`, options)
}

/** Permanently delete one terminal run from the durable store (owner-only).
 * A run removed locally alone re-hydrates on the next list; this is what makes
 * deletion survive a reload. 409 when the run is still active (cancel first). */
export async function deleteResearchRun(
  runId: string,
  options: ClientOptions = {},
) {
  await request(`/v1/runs/${runId}`, {
    ...options,
    method: 'DELETE',
  })
}

/** A completed report carried in from a loaded project file, pushed to the
 * durable run tier so it survives reload + follows the user. `result` is the
 * server result payload (answer/metrics/top_sources/top_claims/references/
 * usage) WITHOUT run_id/status (the result endpoint adds those on read). */
export type ImportResearchRunPayload = {
  /** Stable id from the imported project. The server always allocates the
   * canonical public run id and returns it in the summary. */
  source_run_id: string
  question: string
  stack: string
  mode?: string
  status?: string
  /** The report's ORIGINAL date (display/ordering); retention starts at import. */
  created_at?: number
  agent_overrides?: Record<string, unknown>
  snapshot?: Record<string, unknown>
  result?: Record<string, unknown>
}

export async function importResearchRun(
  payload: ImportResearchRunPayload,
  options: ClientOptions = {},
) {
  return requestJson<ResearchRunSummary>('/v1/runs/import', {
    ...options,
    method: 'POST',
    body: { ...payload, workspace_id: options.workspaceId },
  })
}

export async function fetchResearchRunResult(
  runId: string,
  options: ClientOptions = {},
) {
  return requestJson<ResearchRunResult>(`/v1/runs/${runId}/result`, options)
}

/** One knowledge chunk + optional neighbour context (canvas evidence view). */
export type KnowledgeChunkDetail = {
  chunk_id: string
  document_id: string
  chunk_index: number
  /** Canonical document text only; retrieval scaffolding is never exposed. */
  excerpt: string
  page_number: number | null
  source_span: {
    start: number
    end: number
    offset_unit: 'utf8_byte'
    document_content_hash: string
  }
  revision_id: string | null
  generation_id: string | null
  provenance_status: 'verified_span'
  neighbors?: {
    chunk_index: number
    excerpt: string
    source_span: KnowledgeChunkDetail['source_span']
    revision_id: string | null
    generation_id: string | null
    provenance_status: 'verified_span'
  }[]
}

export async function getKnowledgeChunk(
  documentId: string,
  chunkIndex: number,
  context: number,
  options: ClientOptions = {},
) {
  const query = context > 0 ? `?context=${context}` : ''
  return requestJson<KnowledgeChunkDetail>(
    `/v1/knowledge/documents/${documentId}/chunks/${chunkIndex}${query}`,
    options,
  )
}

// --- Workspace-agent control surfaces ---------------------------------------
// Rows are the truth: SSE events are signals to (re-)fetch these.

export async function listRunChildren(
  runId: string,
  options: ClientOptions = {},
) {
  const payload = await requestJson<{ data: ResearchRunSummary[] }>(
    `/v1/runs/${runId}/children`,
    options,
  )
  return payload.data
}

export async function getAgentRunPlan(
  runId: string,
  version: number | undefined,
  options: ClientOptions = {},
) {
  const query = version === undefined ? '' : `?version=${version}`
  return requestJson<AgentPlanWire>(`/v1/runs/${runId}/plan${query}`, options)
}

export async function getAgentRunTaskResult(
  runId: string,
  taskId: string,
  options: ClientOptions = {},
) {
  return requestJson<AgentTaskResultWire>(
    `/v1/runs/${runId}/tasks/${taskId}/result`,
    options,
  )
}

export async function cancelAgentRunTask(
  runId: string,
  taskId: string,
  options: ClientOptions = {},
) {
  return requestJson<AgentTaskCancelWire>(
    `/v1/runs/${runId}/tasks/${taskId}/cancel`,
    { ...options, method: 'POST' },
  )
}

export async function listAgentRunApprovals(
  runId: string,
  options: ClientOptions = {},
) {
  const payload = await requestJson<{ data: AgentApprovalWire[] }>(
    `/v1/runs/${runId}/approvals`,
    options,
  )
  return payload.data
}

/** Decide a pending approval. The response embeds the RESUMED run summary
 * under `run` — callers must upsert it (status flips waiting -> queued). */
export async function decideAgentRunApproval(
  runId: string,
  approvalId: string,
  decision: AgentApprovalDecisionRequest,
  options: ClientOptions = {},
) {
  return requestJson<AgentApprovalWire & { run: ResearchRunSummary }>(
    `/v1/runs/${runId}/approvals/${approvalId}`,
    { ...options, method: 'POST', body: decision },
  )
}

export async function listAgentRunClarifications(
  runId: string,
  options: ClientOptions = {},
) {
  const payload = await requestJson<{ data: AgentClarificationWire[] }>(
    `/v1/runs/${runId}/clarifications`,
    options,
  )
  return payload.data
}

/** Answer a clarification: whole-round free text, a preset option, or the
 * structured per-question `answers` map (exactly one of the three). */
export async function answerAgentRunClarification(
  runId: string,
  clarificationId: string,
  answer: AgentClarificationAnswerRequest,
  options: ClientOptions = {},
) {
  return requestJson<AgentClarificationWire & { run: ResearchRunSummary }>(
    `/v1/runs/${runId}/clarifications/${clarificationId}`,
    { ...options, method: 'POST', body: answer },
  )
}

export async function listAgentRunArtifacts(
  runId: string,
  options: ClientOptions = {},
) {
  const payload = await requestJson<{ data: AgentArtifactMetaWire[] }>(
    `/v1/runs/${runId}/artifacts`,
    options,
  )
  return payload.data
}

export async function listAgentSessionArtifacts(
  sessionId: string,
  options: ClientOptions = {},
) {
  return requestJson<{ data: AgentArtifactMetaWire[] }>(
    `/v1/agent-sessions/${sessionId}/artifacts`,
    options,
  )
}

export async function getAgentRunArtifact(
  runId: string,
  artifactId: string,
  revision: number | undefined,
  options: ClientOptions = {},
) {
  const query = revision === undefined ? '' : `?revision=${revision}`
  return requestJson<AgentArtifactDetailWire>(
    `/v1/runs/${runId}/artifacts/${artifactId}${query}`,
    options,
  )
}

/** Optimistic-concurrency canvas edit. 409 `conflict` carries either
 * `current_revision` (stale edit) or `locked_by: 'agent'` (agent writing);
 * callers branch on those extras via the thrown error's payload. */
export async function updateAgentRunArtifact(
  runId: string,
  artifactId: string,
  body: { content_markdown: string; expected_revision: number },
  options: ClientOptions = {},
) {
  return requestJson<{ id: string; revision: number; updated_by: string }>(
    `/v1/runs/${runId}/artifacts/${artifactId}`,
    { ...options, method: 'PUT', body },
  )
}

/** Metadata-only rename: title changes, revision does not — the
 * content history never records a rename. 409 `locked_by: 'agent'`
 * while the agent is writing. */
export async function renameAgentRunArtifact(
  runId: string,
  artifactId: string,
  body: { title: string },
  options: ClientOptions = {},
) {
  return requestJson<{
    id: string
    revision: number
    title: string
    updated_by: string
  }>(
    `/v1/runs/${runId}/artifacts/${artifactId}`,
    { ...options, method: 'PATCH', body },
  )
}

export async function exportAgentRunArtifact(
  runId: string,
  artifactId: string,
  body: { target?: 'editor_document'; title?: string; folder_id?: string },
  options: ClientOptions = {},
) {
  return requestJson<Record<string, unknown>>(
    `/v1/runs/${runId}/artifacts/${artifactId}/export`,
    {
      ...options,
      method: 'POST',
      body: { ...body, workspace_id: options.workspaceId },
    },
  )
}

// --- Editor patches ----------------------------------------------------------

export async function getEditorPatch(
  patchId: string,
  options: ClientOptions = {},
) {
  return requestJson<AgentPatchWire>(
    `/v1/editor/patches/${patchId}`,
    options,
  )
}

/** Patch METADATA of one document (no edit bodies), newest first.
 * `status` filters to one lifecycle state; bodies come from
 * `getEditorPatch`. */
export async function listEditorDocumentPatches(
  documentId: string,
  status: 'pending' | 'accepted' | 'rejected' | null = null,
  options: ClientOptions = {},
) {
  const query = status ? `?status=${status}` : ''
  return requestJson<{
    data: Array<{
      patch_id: string
      source: 'suggest' | 'instruct' | 'agent'
      status: 'pending' | 'accepted' | 'rejected'
    }>
  }>(`/v1/editor/documents/${documentId}/patches${query}`, options)
}

/** Apply a pending patch server-side. 409 `conflict` carries
 * `current_revision`/`revision_before` on a stale precondition. */
export async function applyEditorPatch(
  patchId: string,
  expectedRevision: number,
  options: ClientOptions = {},
) {
  return requestJson<{
    document_id: string
    revision: number
    applied_edit_ids: string[]
  }>(`/v1/editor/patches/${patchId}:apply`, {
    ...options,
    method: 'POST',
    body: { expected_revision: expectedRevision },
  })
}

export async function rejectEditorPatch(
  patchId: string,
  note: string,
  options: ClientOptions = {},
) {
  return requestJson<AgentPatchWire>(
    `/v1/editor/patches/${patchId}:reject`,
    { ...options, method: 'POST', body: note ? { note } : {} },
  )
}

// --- Agent sessions (shape-identical clone of knowledge sessions) ----------

export async function listAgentSessions(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: ServerAgentSession[] }>(
    '/v1/agent-sessions',
    options,
  )
  return payload.data
}

export async function getAgentSession(
  sessionId: string,
  options: ClientOptions = {},
) {
  return requestJson<ServerAgentSession>(
    `/v1/agent-sessions/${sessionId}`,
    options,
  )
}

export async function saveAgentSession(
  sessionId: string,
  payload: {
    title: string
    title_revision?: number
    title_source?: import('@/features/sessionTitle/model').TitleSource
    items_json: string
    group_id: string | null
    created_at: number
    updated_at: number
  },
  options: ClientOptions = {},
) {
  return requestJson<ServerAgentSession>(`/v1/agent-sessions/${sessionId}`, {
    ...options,
    body: payload,
    method: 'PUT',
  })
}

export type SessionTitleResponse = {
  trace_id?: string | null
  title: string | null
  reason?: string | null
  model?: string | null
  usage?: { prompt_tokens: number; completion_tokens: number }
  current?: import('@/features/sessionTitle/model').TitleWire
}

export async function generateSessionTitle(
  payload: { question: string; language: string; stack?: string;
    target?: { kind: 'chat' | 'agent'; id: string; revision: number } },
  options: ClientOptions = {},
): Promise<SessionTitleResponse> {
  return requestJson<SessionTitleResponse>('/v1/session-titles', { ...options, body: payload, method: 'POST' })
}

export async function deleteAgentSession(
  sessionId: string,
  options: ClientOptions = {},
) {
  const response = await request(`/v1/agent-sessions/${sessionId}`, {
    ...options,
    method: 'DELETE',
  })
  if (response.status === 204) return null
  return (await response.json()) as ServerDeletionOperation
}

export async function listAgentSessionGroups(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: ServerAgentSessionGroup[] }>(
    '/v1/agent-session-groups',
    options,
  )
  return payload.data
}

export async function saveAgentSessionGroup(
  groupId: string,
  payload: { title: string; created_at: number; updated_at: number },
  options: ClientOptions = {},
) {
  return requestJson<ServerAgentSessionGroup>(
    `/v1/agent-session-groups/${groupId}`,
    { ...options, body: payload, method: 'PUT' },
  )
}

export async function deleteAgentSessionGroup(
  groupId: string,
  options: ClientOptions = {},
) {
  await request(`/v1/agent-session-groups/${groupId}`, {
    ...options,
    method: 'DELETE',
  })
}

export type AgentMemoryStatus = {
  available: boolean
  degraded_reason?: string
  durable: boolean
  effective_mode?: string
  mode: string
  principal_eligible: boolean
  provider: string
}

export type AgentMemoryWire = {
  category: string
  confidence: number
  content: string
  created_at: string
  id: string
  metadata: Record<string, unknown>
  scope: string
  source_run_id: string
  updated_at: string
}

export type AgentMemoryCandidateWire = {
  category: string
  confidence: number
  content: string
  created_at: number
  id: string
  memory_id: string
  reason: string
  scope: string
  source_run_id: string
  status: 'accepted' | 'pending' | 'rejected'
  updated_at: number
}

export type AgentFeedbackWire = {
  created_at: number
  feedback: 'positive' | 'negative' | 'neutral'
  id: string
  memory_id: string
  reason: string
  run_id: string
}

export type AgentMemoryListFilters = {
  limit?: number
  q?: string
  scope?: 'user' | 'workspace' | 'project' | 'agent'
}

export type AgentFeedbackListFilters = {
  limit?: number
  runId?: string
}

function agentMemoryQuery(filters: AgentMemoryListFilters = {}): string {
  const params = new URLSearchParams()
  if (filters.q?.trim()) params.set('q', filters.q.trim())
  if (filters.scope) params.set('scope', filters.scope)
  if (filters.limit !== undefined) params.set('limit', String(filters.limit))
  const query = params.toString()
  return query ? `?${query}` : ''
}

function agentFeedbackQuery(filters: AgentFeedbackListFilters = {}): string {
  const params = new URLSearchParams()
  if (filters.runId?.trim()) params.set('run_id', filters.runId.trim())
  if (filters.limit !== undefined) params.set('limit', String(filters.limit))
  const query = params.toString()
  return query ? `?${query}` : ''
}

export async function listAgentMemories(
  options: ClientOptions = {},
  filters: AgentMemoryListFilters = {},
) {
  return requestJson<{
    data: AgentMemoryWire[]
    object: 'list'
    status: AgentMemoryStatus
  }>(`/v1/agent/memory${agentMemoryQuery(filters)}`, options)
}

export async function updateAgentMemory(
  memoryId: string,
  body: { category: string; content: string; scope: string },
  options: ClientOptions = {},
) {
  return requestJson<AgentMemoryWire>(
    `/v1/agent/memory/${encodeURIComponent(memoryId)}`,
    { ...options, method: 'PATCH', body },
  )
}

export async function deleteAgentMemory(
  memoryId: string,
  options: ClientOptions = {},
) {
  return requestJson<{ deleted: boolean }>(
    `/v1/agent/memory/${encodeURIComponent(memoryId)}`,
    { ...options, method: 'DELETE' },
  )
}

export async function clearAgentMemories(options: ClientOptions = {}) {
  return requestJson<{ deleted: number }>('/v1/agent/memory:clear', {
    ...options,
    body: {},
    method: 'POST',
  })
}

export async function listAgentMemoryCandidates(options: ClientOptions = {}) {
  return requestJson<{
    data: AgentMemoryCandidateWire[]
    object: 'list'
    status: AgentMemoryStatus
  }>('/v1/agent/memory/candidates', options)
}

export async function acceptAgentMemoryCandidate(
  candidateId: string,
  body: { content?: string } = {},
  options: ClientOptions = {},
) {
  return requestJson<AgentMemoryCandidateWire>(
    `/v1/agent/memory/candidates/${encodeURIComponent(candidateId)}:accept`,
    { ...options, body, method: 'POST' },
  )
}

export async function rejectAgentMemoryCandidate(
  candidateId: string,
  options: ClientOptions = {},
) {
  return requestJson<AgentMemoryCandidateWire>(
    `/v1/agent/memory/candidates/${encodeURIComponent(candidateId)}:reject`,
    { ...options, body: {}, method: 'POST' },
  )
}

export async function listAgentMemoryFeedback(
  options: ClientOptions = {},
  filters: AgentFeedbackListFilters = {},
) {
  return requestJson<{
    data: AgentFeedbackWire[]
    object: 'list'
  }>(`/v1/agent/memory/feedback${agentFeedbackQuery(filters)}`, options)
}

export async function submitAgentRunFeedback(
  runId: string,
  body: {
    feedback: 'positive' | 'negative' | 'neutral'
    memory_id?: string
    reason?: string
  },
  options: ClientOptions = {},
) {
  return requestJson<AgentFeedbackWire>(
    `/v1/agent/runs/${encodeURIComponent(runId)}/feedback`,
    { ...options, body, method: 'POST' },
  )
}

export async function createChatCompletion(
  chatRequest: ChatCompletionRequest,
  options: ClientOptions = {},
) {
  return requestJson<ChatCompletionResponse>('/v1/chat/completions', {
    ...options,
    method: 'POST',
    body: serializeChatCompletionRequest(chatRequest, false),
  })
}

export async function improveText(
  textRequest: TextImprovementRequest,
  options: ClientOptions = {},
) {
  return requestJson<TextImprovementResponse>('/v1/text/improvements', {
    ...options,
    method: 'POST',
    body: {
      context: textRequest.context,
      guidance: textRequest.guidance,
      locale: textRequest.locale,
      stack: textRequest.stack,
      text: textRequest.text,
      workspace_id: options.workspaceId,
    },
  })
}

/**
 * Map attached reference documents to the additive snake_case `attachments`
 * wire field shared by both editor endpoints. Returns `undefined` for no
 * attachments so the request body stays byte-identical to the pre-attachment
 * shape (the backend treats a missing field as "no attachments").
 */
function attachmentsPayload(docs: readonly ReferenceDoc[] | undefined) {
  if (!docs || docs.length === 0) return undefined
  return docs.map((doc) => ({
    content: doc.content,
    label: doc.label,
    page_count: doc.pageCount ?? null,
    size_bytes: doc.sizeBytes ?? null,
  }))
}

export type EditorSuggestRequest = {
  attachments?: ReferenceDoc[]
  background?: string
  blockMarkdown?: string
  blockText: string
  /** The document text directly before and after the passage, so the
   * rewrite fits the sentence it replaces a part of. Sent untrimmed. */
  contextAfter?: string
  contextBefore?: string
  currentSuggestionMarkdown?: string
  refinementInstruction?: string
  globalInstruction?: string
  instruction?: string
  locale: 'de' | 'en'
  modelTier?: 'high' | 'mid' | 'fast' | null
  model?: string | null
  effort?: string | null
  snippet?: string
  stack?: string
}

export type EditorSuggestResponse = {
  change_summary: string[]
  improved_text: string
  warnings: string[]
}

export async function suggestEditorBlock(
  suggestRequest: EditorSuggestRequest,
  options: ClientOptions = {},
) {
  return requestJson<EditorSuggestResponse>('/v1/editor/suggest', {
    ...options,
    method: 'POST',
    body: {
      agent_overrides: editorAgentOverrides(suggestRequest),
      attachments: attachmentsPayload(suggestRequest.attachments),
      background: suggestRequest.background,
      block_markdown: suggestRequest.blockMarkdown,
      block_text: suggestRequest.blockText,
      context_after: suggestRequest.contextAfter,
      context_before: suggestRequest.contextBefore,
      current_suggestion_markdown: suggestRequest.currentSuggestionMarkdown,
      global_instruction: suggestRequest.globalInstruction,
      instruction: suggestRequest.instruction,
      locale: suggestRequest.locale,
      refinement_instruction: suggestRequest.refinementInstruction,
      snippet: suggestRequest.snippet,
      stack: suggestRequest.stack,
      workspace_id: options.workspaceId,
    },
  })
}

export type EditorInstructEdit = {
  find: string
  note: string
  position: 'replace' | 'before' | 'after' | 'append'
  quote_after: string
  quote_before: string
  text: string
}

export type EditorInstructRequest = {
  attachments?: ReferenceDoc[]
  documentMarkdown: string
  instruction: string
  locale: 'de' | 'en'
  modelTier?: 'high' | 'mid' | 'fast' | null
  model?: string | null
  effort?: string | null
  stack?: string
}

export type EditorInstructResponse = {
  assistant_message: string
  edits: EditorInstructEdit[]
  warnings: string[]
}

export async function instructEditorDocument(
  instructRequest: EditorInstructRequest,
  options: ClientOptions = {},
) {
  return requestJson<EditorInstructResponse>('/v1/editor/instruct', {
    ...options,
    method: 'POST',
    body: {
      agent_overrides: editorAgentOverrides(instructRequest),
      attachments: attachmentsPayload(instructRequest.attachments),
      document_markdown: instructRequest.documentMarkdown,
      instruction: instructRequest.instruction,
      locale: instructRequest.locale,
      stack: instructRequest.stack,
      workspace_id: options.workspaceId,
    },
  })
}

export async function streamChatCompletion(
  chatRequest: ChatCompletionRequest,
  options: StreamChatCompletionOptions,
) {
  const response = await request('/v1/chat/completions', {
    ...options,
    method: 'POST',
    body: serializeChatCompletionRequest(chatRequest, true),
  })
  if (!response.body) {
    throw readableRequestError({
      serverMessage: 'Inqtrix chat stream did not return a response body.',
      type: 'stream_unavailable',
    })
  }
  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.includes('text/event-stream')) {
    throw readableRequestError({
      serverMessage: 'Inqtrix chat streaming is not available for this response.',
      type: 'stream_unavailable',
    })
  }

  const reader = response.body
    .pipeThrough(new TextDecoderStream())
    .getReader()
  let buffer = ''
  let doneEmitted = false

  function emitDone() {
    if (doneEmitted) return
    doneEmitted = true
    options.onDone?.()
  }

  for (;;) {
    const { value, done } = await readStream(reader, options.signal)
    if (done) break
    buffer += value
    const frames = buffer.split('\n\n')
    buffer = frames.pop() ?? ''

    for (const frame of frames) {
      const data = parseSseData(frame)
      if (!data) continue
      if (data === '[DONE]') {
        emitDone()
        return
      }

      const chunk = JSON.parse(data) as ChatCompletionChunk
      const modelResolution = chunk.inqtrix?.model_resolution
      if (modelResolution) options.onModelResolution?.(modelResolution)
      const choice = chunk.choices?.[0]
      const delta = choice?.delta?.content
      if (delta) options.onDelta(delta)
      if (choice?.finish_reason) emitDone()
    }
  }

  emitDone()
}

export type ServerSentEventMetadata = {
  event: string | null
  id: string | null
}

type StreamEventsOptions<T> = ClientOptions & {
  /** Transport liveness, including comment-only heartbeat frames. */
  onActivity?: () => void
  onEvent: (event: T, metadata: ServerSentEventMetadata) => void
}

/**
 * Generic Server-Sent-Events reader: open *eventsUrl*, split on the
 * SSE frame boundary, parse each frame's data line, and hand the parsed
 * object to ``onEvent``. Both the research-run and the reindex-job
 * streams are thin typed wrappers over this one reader.
 */
export async function streamServerSentEvents<T>(
  eventsUrl: string,
  options: StreamEventsOptions<T>,
) {
  const response = await request(eventsUrl, options)
  if (!response.body) {
    throw readableRequestError({
      serverMessage: 'Inqtrix event stream did not return a response body.',
      type: 'stream_unavailable',
    })
  }

  const reader = response.body
    .pipeThrough(new TextDecoderStream())
    .getReader()
  let buffer = ''

  for (;;) {
    const { value, done } = await readStream(reader, options.signal)
    if (done) break
    options.onActivity?.()
    buffer += value
    const frames = buffer.split('\n\n')
    buffer = frames.pop() ?? ''

    for (const frame of frames) {
      const parsed = parseSseFrame(frame)
      if (!parsed.data) continue
      options.onEvent(JSON.parse(parsed.data) as T, {
        event: parsed.event,
        id: parsed.id,
      })
    }
  }
}

export type UserEvent =
  | {
    data: { cursor: string; user_id: string }
    id: string | null
    type: 'ready'
  }
  | {
    data: {
      resource_id?: string
      resource_type?: string
      scope: string
    }
    id: string | null
    type: 'invalidate'
  }
  | {
    data: Record<string, never>
    id: string | null
    type: 'reset'
  }
  | {
    data: Record<string, unknown>
    id: string | null
    type: 'unknown'
  }

type StreamUserEventsOptions = ClientOptions & {
  onEvent: (event: UserEvent) => void
}

/**
 * User-scoped invalidation stream. Payloads deliberately remain hints: callers
 * wake their authoritative list endpoints instead of applying entity patches
 * from this channel.
 */
export async function streamUserEvents(options: StreamUserEventsOptions) {
  return streamServerSentEvents<Record<string, unknown>>('/v1/user/events', {
    ...options,
    onEvent: (data, metadata) => {
      const type = metadata.event
      if (type === 'ready') {
        options.onEvent({
          data: data as { cursor: string; user_id: string },
          id: metadata.id,
          type,
        })
      } else if (type === 'invalidate') {
        options.onEvent({
          data: data as {
            resource_id?: string
            resource_type?: string
            scope: string
          },
          id: metadata.id,
          type,
        })
      } else if (type === 'reset') {
        options.onEvent({
          data: {},
          id: metadata.id,
          type,
        })
      } else {
        options.onEvent({ data, id: metadata.id, type: 'unknown' })
      }
    },
  })
}

export async function streamResearchRunEvents(
  eventsUrl: string,
  options: StreamRunEventsOptions,
) {
  return streamServerSentEvents<ResearchRunEvent>(eventsUrl, options)
}

/** One keyset page of buffered run events (the polling fallback):
 * the SAME replay buffer as the SSE route, returned immediately as
 * JSON. `terminal` tells the poller the run is settled. */
export async function fetchRunEventsPage(
  eventsUrl: string,
  afterSequence: number | null,
  options: ClientOptions = {},
) {
  const separator = eventsUrl.includes('?') ? '&' : '?'
  const after
    = afterSequence === null ? '' : `&after=${encodeURIComponent(afterSequence)}`
  return requestJson<{ data: ResearchRunEvent[]; terminal: boolean }>(
    `${eventsUrl}${separator}format=json${after}`,
    options,
  )
}

export async function startIndexingJob(
  collectionId: string,
  request: { indexId?: string },
  options: ClientOptions = {},
) {
  return requestJson<IndexingJobSummary>(
    `/v1/knowledge/collections/${collectionId}/reindex`,
    {
      ...options,
      method: 'POST',
      body: { index_id: request.indexId, workspace_id: options.workspaceId },
    },
  )
}

export async function startDocumentRevisionJob(
  collectionId: string,
  request: {
    assetId?: string
    metadata?: Record<string, unknown>
    text?: string
    title: string
  },
  options: ClientOptions = {},
) {
  return requestJson<IndexingJobSummary>(
    `/v1/knowledge/collections/${collectionId}/document-revisions`,
    {
      ...options,
      method: 'POST',
      body: {
        asset_id: request.assetId,
        metadata: request.metadata,
        text: request.text,
        title: request.title,
        workspace_id: options.workspaceId,
      },
    },
  )
}

export async function getIndexingJob(
  jobId: string,
  options: ClientOptions = {},
) {
  return requestJson<IndexingJobSummary>(
    `/v1/knowledge/indexing-jobs/${jobId}`,
    options,
  )
}

export async function cancelIndexingJob(
  jobId: string,
  options: ClientOptions = {},
) {
  return requestJson<IndexingJobSummary>(
    `/v1/knowledge/indexing-jobs/${jobId}/cancel`,
    { ...options, method: 'POST' },
  )
}

export async function resumeIndexingJob(
  jobId: string,
  options: ClientOptions = {},
) {
  return requestJson<IndexingJobSummary>(
    `/v1/knowledge/indexing-jobs/${jobId}/resume`,
    { ...options, method: 'POST' },
  )
}

export async function resumeIndexingJobWithoutContext(
  jobId: string,
  options: ClientOptions = {},
) {
  return requestJson<IndexingJobSummary>(
    `/v1/knowledge/indexing-jobs/${jobId}/resume-raw`,
    { ...options, method: 'POST' },
  )
}

export async function listIndexingJobs(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: IndexingJobSummary[] }>(
    '/v1/knowledge/indexing-jobs',
    options,
  )
  return payload.data
}

export async function streamIndexingJobEvents(
  eventsUrl: string,
  options: StreamEventsOptions<IndexingJobEvent>,
) {
  return streamServerSentEvents<IndexingJobEvent>(eventsUrl, options)
}

type RequestJsonOptions = ClientOptions & {
  body?: unknown
  method?: 'DELETE' | 'GET' | 'PATCH' | 'POST' | 'PUT'
}

async function requestJson<T>(path: string, options: RequestJsonOptions = {}) {
  const response = await request(path, options)
  return (await response.json()) as T
}

async function request(path: string, options: RequestJsonOptions = {}) {
  const method = options.method ?? 'GET'
  const serializedBody = options.body === undefined
    ? undefined
    : JSON.stringify(options.body)

  const send = async (csrfRetryAttempted: boolean): Promise<Response> => {
    const headers = new Headers()
    if (serializedBody !== undefined) {
      headers.set('Content-Type', 'application/json')
    }
    if (options.apiKey) {
      headers.set('Authorization', `Bearer ${options.apiKey}`)
    }
    if (options.workspaceId) {
      headers.set('X-Inqtrix-Workspace-Id', options.workspaceId)
    }
    if (options.lastEventId) {
      headers.set('Last-Event-ID', options.lastEventId)
    }
    attachExpectedUserIdentity(headers)
    attachCsrfHeader(headers, method)

    const response = await fetchReadable(resolveUrl(path, options.baseUrl), {
      method,
      headers,
      body: serializedBody,
      signal: options.signal,
      credentials: 'include',
    })

    if (response.ok) return response
    const error = await requestError(response)
    if (canRecoverCsrf({
      csrfRetryAttempted,
      error,
      method,
      options,
      path,
    })) {
      await refreshSessionCsrf(options)
      return send(true)
    }
    throwParsedRequestError(
      error,
      response.status,
      options.reloadOnUnauthorized !== false,
    )
  }

  return send(false)
}

function canRecoverCsrf({
  csrfRetryAttempted,
  error,
  method,
  options,
  path,
}: {
  csrfRetryAttempted: boolean
  error: InqtrixRequestError
  method: string
  options: ClientOptions
  path: string
}): boolean {
  if (csrfRetryAttempted || error.status !== 403 || error.name !== 'csrf_error') {
    return false
  }
  if (method === 'GET' || method === 'HEAD' || options.apiKey) return false
  // Guest double-submit tokens and login/setup endpoints have different
  // authorities. A session bootstrap must never be used to retry them.
  if (path.startsWith('/v1/editor/guest/')) return false
  return !path.startsWith('/api/auth/login')
    && !path.startsWith('/api/setup/')
    && path !== '/api/auth/session'
}

/** One process-wide recovery flight. It intentionally has no caller AbortSignal:
 * one cancelled mutation must not abort the cookie repair awaited by other
 * requests. The original request keeps its own signal for the retry. */
async function refreshSessionCsrf(options: ClientOptions): Promise<void> {
  if (csrfRefreshInFlight) return csrfRefreshInFlight
  csrfRefreshInFlight = (async () => {
    const headers = new Headers()
    if (options.workspaceId) {
      headers.set('X-Inqtrix-Workspace-Id', options.workspaceId)
    }
    attachExpectedUserIdentity(headers)
    const response = await fetchReadable(resolveUrl('/api/auth/session', options.baseUrl), {
      credentials: 'include',
      headers,
      method: 'GET',
    })
    if (!response.ok) {
      await throwRequestError(response, false)
    }
    const session = await response.json() as AuthSessionInfo
    adoptSessionCsrfToken(session)
    if (!session.authenticated) {
      throw readableRequestError({
        serverMessage: 'The authenticated session is no longer available.',
        status: 401,
        type: 'authentication_error',
      })
    }
  })().finally(() => {
    csrfRefreshInFlight = null
  })
  return csrfRefreshInFlight
}

/**
 * Attach the OIDC double-submit CSRF token on unsafe methods. The
 * authoritative token is adopted from the no-store session bootstrap and
 * kept only in this page's process memory. The readable cookie remains the
 * pre-bootstrap fallback required by OWASP signed double-submit. No
 * bootstrap token or cookie means no OIDC session (apikey/none modes), and
 * nothing is sent.
 */
function attachCsrfHeader(headers: Headers, method: string) {
  if (method === 'GET' || method === 'HEAD') return
  const token = sessionCsrfToken ?? readCsrfCookie()
  if (token) headers.set('X-CSRF-Token', token)
  const guestToken = readCookie('inqtrix_editor_guest_csrf')
  if (guestToken) headers.set('X-Inqtrix-Guest-CSRF', guestToken)
}

function adoptSessionCsrfToken(session: AuthSessionInfo) {
  const token = session.authenticated ? session.csrf_token?.trim() : null
  sessionCsrfToken = token || null
}

function attachExpectedUserIdentity(headers: Headers) {
  if (expectedUserIdentity) {
    headers.set(EXPECTED_USER_ID_HEADER, expectedUserIdentity)
  }
}

function readCsrfCookie(): string | null {
  if (typeof document === 'undefined') return null
  for (const name of ['__Host-inqtrix_csrf', 'inqtrix_csrf']) {
    const match = document.cookie
      .split('; ')
      .find((entry) => entry.startsWith(`${name}=`))
    if (match) return decodeURIComponent(match.slice(name.length + 1))
  }
  return null
}

function readCookie(name: string): string | null {
  if (typeof document === 'undefined') return null
  const match = document.cookie
    .split('; ')
    .find((entry) => entry.startsWith(`${name}=`))
  return match ? decodeURIComponent(match.slice(name.length + 1)) : null
}

type AuthSessionBase = {
  csrf_token?: string
  /** The user's canonical project namespace (a `ws_...` string), resolved
   * server-side: on the first authenticated boot the server ADOPTS the browser
   * namespace carried in the `X-Inqtrix-Workspace-Id` request header and returns
   * it here; thereafter it returns the same adopted value on every device, so the
   * project follows the user. `null`/absent until a namespace has been adopted
   * (or in non-cookie modes), in which case the desk keeps the browser-local id. */
  project_namespace?: string | null
}

/** Session facts the SPA bootstraps from `GET /api/auth/session`. */
export type AuthSessionInfo = AuthSessionBase & (
  | { authenticated: false }
  | {
    authenticated: true
    user: {
      display_name: string | null
      email: string | null
      id: string
      role: string
    }
  }
)

/**
 * Fetch the current OIDC session state (cookie-driven, never 401s).
 *
 * Pass `options.workspaceId` (the browser's namespace) so the probe carries it as
 * the adoption candidate: a first authenticated boot adopts it as the user's
 * canonical `project_namespace`; later boots ignore it and return the already-
 * adopted value.
 */
export async function fetchAuthSession(options: ClientOptions = {}) {
  const session = await requestJson<AuthSessionInfo>('/api/auth/session', options)
  adoptSessionCsrfToken(session)
  return session
}

/** Destroy the server-side OIDC session using the bootstrap/cookie CSRF token. */
export async function logoutSession(options: ClientOptions = {}) {
  const result = await requestJson<{ logged_out: boolean }>('/api/auth/logout', {
    ...options,
    body: {},
    method: 'POST',
  })
  sessionCsrfToken = null
  return result
}

/**
 * Full-page navigation target that starts the OIDC login redirect.
 * A plain link/location change on purpose — the authorization flow is
 * a top-level browser navigation, never an XHR.
 */
export function buildLoginUrl(baseUrl?: string, next: string = '/') {
  const query = next && next !== '/' ? `?next=${encodeURIComponent(next)}` : ''
  return resolveUrl(`/api/auth/login${query}`, baseUrl)
}

/** One login method the SPA can render (an ordered list per deployment). */
export type AuthLoginMethod = {
  kind: 'sso' | 'password' | 'apikey'
  label: string
  image_url?: string
  identifier?: 'email' | 'username'
}

/**
 * Pre-login auth capabilities from `GET /api/auth/config` — unauthenticated
 * and always mounted, so the SPA can label the SSO button with the real
 * provider name and learn the session contract before authenticating.
 */
export type AuthConfig = {
  auth_mode: 'none' | 'apikey' | 'oidc' | 'local'
  auth_required: boolean
  login_methods: AuthLoginMethod[]
  provider_name: string | null
  registration: { self_service: boolean; needs_owner: boolean }
  pat_available: boolean
  supports_logout: boolean
  csrf_required: boolean
  csrf_header: string
}

/** Pre-login discovery (presentation hints only; degrade open on failure). */
export async function fetchAuthConfig(options: ClientOptions = {}) {
  return requestJson<AuthConfig>('/api/auth/config', options)
}

// --- Native local auth + first-run owner setup ---------------------------
// All cookie-driven through the BFF session machinery: no token is
// threaded by the caller and CSRF rides the cookie on unsafe methods.

/** Create the first owner exactly once; the server logs them straight in. */
export async function createOwner(
  input: { email: string; password: string; displayName?: string },
  options: ClientOptions = {},
) {
  return requestJson<{ authenticated: boolean }>('/api/setup/owner', {
    ...options,
    method: 'POST',
    body: {
      email: input.email,
      password: input.password,
      display_name: input.displayName,
    },
  })
}

/** Email/password login → the same session+CSRF cookies as OIDC. */
export async function loginLocal(
  input: { identifier: string; password: string },
  options: ClientOptions = {},
) {
  return requestJson<{ authenticated: boolean }>('/api/auth/login/local', {
    ...options,
    method: 'POST',
    body: { identifier: input.identifier, password: input.password },
  })
}

/** Self-service password change (local accounts; re-verifies the current). */
export async function changePassword(
  input: { currentPassword: string; newPassword: string },
  options: ClientOptions = {},
) {
  return requestJson<{ changed: boolean }>('/api/auth/password', {
    ...options,
    method: 'POST',
    body: {
      current_password: input.currentPassword,
      new_password: input.newPassword,
    },
  })
}

// --- Instance administration (/v1/admin/*) -------------------------------
// Session-only + admin-gated server-side; denial is an indistinguishable
// 404. A PAT can never administer users.

export type AdminSystemRuntime = {
  api: {
    openapi: boolean
    /** Effective concurrent-chat ceiling of THIS api process.
     * Optional: an older api image does not publish it, and the row
     * must disappear rather than render "undefined". */
    chat_max_concurrent?: number
    /** Threads reserved for event-stream readers in this api process. */
    stream_reader_workers?: number
  }
  files: {
    blob_storage: string
    /** The parser that prepares uploads, independent of knowledge. */
    document_parser: string
    enabled: boolean
    max_file_bytes: number | null
    object_store: string
    object_store_available: boolean
  }
  knowledge: {
    contextual_retrieval: boolean
    default_top_k: number | null
    embedding_model: string | null
    embedding_provider: string | null
    enabled: boolean
    hybrid_retrieval: boolean
    reranker: string
    sparse: string | null
    vector_store: string
    vector_store_available: boolean
  }
  runs: {
    execution: string
    queue: string
    queue_available: boolean
    /** Recently active stream consumers; null when the probe cannot tell. */
    queue_consumers: number | null
    /** Messages in the dispatch stream, in-flight ones included; null when the probe cannot tell. */
    queue_depth: number | null
    store: string
    worker_dispatch: boolean
    /** Admission cap of this api process; with worker_dispatch the
     * execution capacity belongs to the worker fleet and is not claimed.
     * Optional like the api block's concurrency fields: version skew. */
    admission_max_concurrent?: number
    queue_max_size?: number
  }
  /** Optional as a whole: an older api does not publish the block. */
  agents?: {
    /** Ceiling of the agent checkpointer's own psycopg pool in THIS
     * process. */
    checkpointer_pool_size?: number | null
  }
  storage: {
    backend: string
    durable: boolean
  }
  observability: {
    tracing: string
    tracing_active: boolean
    content_capture: boolean
    sample_rate: number
    spool: boolean
    /** Whether ANY process runs the prune jobs (all three live in the worker). */
    retention_enforced: boolean
    retention_days: number | null
    ui_link_configured: boolean
  }
}

/** Sanitized runtime categories for the instance-admin System panel. */
export async function fetchAdminSystemRuntime(options: ClientOptions = {}) {
  return requestJson<AdminSystemRuntime>('/v1/admin/system/runtime', options)
}

/** One row of the instance audit trail (OCSF-oriented read model). */
export type AdminAuditEvent = {
  id: number
  occurred_at: number
  action: string
  resource_type: string
  resource_id: string
  actor_pseudonym: string | null
  actor_type: string
  outcome: 'success' | 'failure' | 'denied'
  workspace_id: string | null
  detail: Record<string, unknown>
  origin: Record<string, string>
  correlation: Record<string, string>
}

export type AdminAuditFilters = {
  /** Action prefix, e.g. "run." or "auth.login_failed". */
  action?: string
  /** Stable actor pseudonym (usr_<hex16>). */
  actor?: string
  outcome?: 'success' | 'failure' | 'denied'
  /** Inclusive lower bound, epoch seconds (matches occurred_at). */
  from?: number
  /** Exclusive upper bound, epoch seconds. */
  to?: number
  cursor?: string
  limit?: number
}

function adminAuditQuery(filters: AdminAuditFilters): string {
  const params = new URLSearchParams()
  if (filters.action) params.set('action', filters.action)
  if (filters.actor) params.set('actor', filters.actor)
  if (filters.outcome) params.set('outcome', filters.outcome)
  if (filters.from !== undefined) params.set('from', String(filters.from))
  if (filters.to !== undefined) params.set('to', String(filters.to))
  if (filters.cursor) params.set('cursor', filters.cursor)
  if (filters.limit) params.set('limit', String(filters.limit))
  const query = params.toString()
  return query ? `?${query}` : ''
}

/** Newest-first audit page (cursor = opaque id keyset). */
export async function listAdminAuditEvents(
  filters: AdminAuditFilters = {},
  options: ClientOptions = {},
) {
  return requestJson<{
    object: 'list'
    data: AdminAuditEvent[]
    next_cursor: string | null
  }>(`/v1/admin/audit${adminAuditQuery(filters)}`, options)
}

/** Durable step events of one run for the admin drawer. */
export async function listAdminRunEvents(
  runId: string,
  options: ClientOptions = {},
) {
  return requestJson<{
    object: 'list'
    data: Array<{
      type: string
      run_id: string
      sequence: number
      created_at: number
      data: Record<string, unknown>
    }>
  }>(`/v1/admin/runs/${encodeURIComponent(runId)}/events`, options)
}

/** Full trace document of one run (Langfuse or spool source). */
export async function fetchAdminRunTraceExport(
  runId: string,
  options: ClientOptions = {},
) {
  return requestJson<{
    run_id: string
    trace_id: string
    source: 'langfuse' | 'spool'
    payload: Record<string, unknown>
    html_path?: string
    ui_url?: string
  }>(`/v1/admin/runs/${encodeURIComponent(runId)}/trace/export`, options)
}

/** Browser URL for the streamed audit export (NDJSON/CSV download).

 * Resolved against the configured API base: in split-origin setups
 * (desk served separately from the API) a relative URL would hit the
 * FRONTEND origin and 404. */
export function adminAuditExportUrl(
  format: 'ndjson' | 'csv',
  filters: AdminAuditFilters = {},
  baseUrl?: string,
): string {
  const query = adminAuditQuery({ ...filters, cursor: undefined })
  const separator = query ? '&' : '?'
  return resolveUrl(
    `/v1/admin/audit/export${query}${separator}format=${format}`,
    baseUrl,
  )
}

/** One row of the instance user list. */
export type AdminUser = {
  id: string
  email: string | null
  display_name: string | null
  instance_role: 'admin' | 'user'
  disabled: boolean
  last_login_at: number | null
  /** The deletion operation erasing this account, or null. While set the
   * account is cut off and can no longer be changed. */
  erasure_operation_id: string | null
}

/** Every mirrored identity in the tenant (admin listing). */
export async function listAdminUsers(options: ClientOptions = {}) {
  return requestJson<{ users: AdminUser[] }>('/v1/admin/users', options)
}

/** Change a user's instance role (last-admin demotion is refused, 409). */
export async function setAdminUserRole(
  userId: string,
  instanceRole: 'admin' | 'user',
  options: ClientOptions = {},
) {
  return requestJson<AdminUser>(
    `/v1/admin/users/${encodeURIComponent(userId)}`,
    { ...options, method: 'PATCH', body: { instance_role: instanceRole } },
  )
}

/** Disable or enable a user (disable cascades sessions + tokens). */
export async function setAdminUserDisabled(
  userId: string,
  disabled: boolean,
  options: ClientOptions = {},
) {
  const action = disabled ? 'disable' : 'enable'
  return requestJson<AdminUser>(
    `/v1/admin/users/${encodeURIComponent(userId)}:${action}`,
    { ...options, method: 'POST', body: {} },
  )
}

/** One line of an erasure preview: a table, what happens to it, and why. */
export type AdminErasureLine = {
  table: string
  column: string
  disposition: 'delegate' | 'delete_row' | 'clear_reference' | 'reassign' | 'retain' | 'refuse'
  reason: string
  rows: number
  aggregate?: string
}

/** What erasing one account would reach, counted before it happens. */
export type AdminErasurePreview = {
  user_id: string
  deleted_rows: number
  cleared_rows: number
  /** Files, collections, sessions, runs and indexes that go through their
   * own deletion together with the data they hold outside the database. */
  delegated_rows: number
  /** Edits and resolutions in OTHER people's documents that stay and move
   * to the deleted-account placeholder. */
  reassigned_rows: number
  /** Replies OTHER people wrote inside documents that would go with the
   * account. The one loss an administrator inflicts on somebody who was
   * never asked, so it is shown on its own. */
  foreign_rows: number
  /** Reasons the erasure would refuse; empty means it can proceed. */
  blockers: string[]
  lines: AdminErasureLine[]
}

/** Count what erasing this account would destroy. Reads only. */
export async function previewAdminUserErasure(
  userId: string,
  options: ClientOptions = {},
) {
  return requestJson<AdminErasurePreview>(
    `/v1/admin/users/${encodeURIComponent(userId)}/erasure-preview`,
    options,
  )
}

/** Start erasing one account (202). The answer is the deletion operation
 * that erases it; 409 when something must be settled first. Irreversible. */
export async function eraseAdminUser(
  userId: string,
  options: ClientOptions = {},
) {
  return requestJson<ServerDeletionOperation>(
    `/v1/admin/users/${encodeURIComponent(userId)}`,
    { ...options, method: 'DELETE' },
  )
}

/** Create a local account (local mode only); the admin sets the password. */
export async function createAdminUser(
  input: {
    email: string
    password: string
    instanceRole?: 'admin' | 'user'
    displayName?: string
  },
  options: ClientOptions = {},
) {
  return requestJson<AdminUser>('/v1/admin/users', {
    ...options,
    method: 'POST',
    body: {
      email: input.email,
      password: input.password,
      instance_role: input.instanceRole,
      display_name: input.displayName,
    },
  })
}

/** Admin password reset for a local account (forgotten-password recovery). */
export async function resetUserPassword(
  userId: string,
  password: string,
  options: ClientOptions = {},
) {
  return requestJson<{ reset: boolean }>(
    `/v1/admin/users/${encodeURIComponent(userId)}:reset-password`,
    { ...options, method: 'POST', body: { password } },
  )
}

// --- Admin workspace management (/v1/admin/workspaces) --------------------
// Instance-admin surface: create workspaces and position users into them.
// The collaboration role (viewer..owner) lives entirely inside a workspace.

export type WorkspaceRoleValue = 'viewer' | 'commenter' | 'editor' | 'owner'

/** One workspace row in the admin overview. */
export type AdminWorkspace = {
  workspace_id: string
  name: string
  created_by_user_id: string
  member_count: number
}

/** One member row of a workspace (enriched with the mirror profile). */
export type WorkspaceMember = {
  user_id: string
  role: WorkspaceRoleValue
  display_name: string | null
  email: string | null
}

/** Every workspace in the tenant with its member count. */
export async function listAdminWorkspaces(options: ClientOptions = {}) {
  const payload = await requestJson<{ data: AdminWorkspace[] }>(
    '/v1/admin/workspaces',
    options,
  )
  return payload.data
}

/** Create a workspace; the calling admin becomes its OWNER. */
export async function createAdminWorkspace(
  name: string,
  options: ClientOptions = {},
) {
  return requestJson<{ workspace_id: string; name: string }>(
    '/v1/admin/workspaces',
    { ...options, method: 'POST', body: { name } },
  )
}

/** Rename a workspace. */
export async function renameAdminWorkspace(
  workspaceId: string,
  name: string,
  options: ClientOptions = {},
) {
  return requestJson<{ workspace_id: string; name: string }>(
    `/v1/admin/workspaces/${encodeURIComponent(workspaceId)}`,
    { ...options, method: 'PATCH', body: { name } },
  )
}

/** Delete a workspace and cascade its memberships. */
export async function deleteAdminWorkspace(
  workspaceId: string,
  options: ClientOptions = {},
) {
  await request(`/v1/admin/workspaces/${encodeURIComponent(workspaceId)}`, {
    ...options,
    method: 'DELETE',
  })
}

/** Members of one workspace. */
export async function listWorkspaceMembers(
  workspaceId: string,
  options: ClientOptions = {},
) {
  const payload = await requestJson<{ data: WorkspaceMember[] }>(
    `/v1/admin/workspaces/${encodeURIComponent(workspaceId)}/members`,
    options,
  )
  return payload.data
}

/** Assign a user to a workspace at a role (adds them as a member). */
export async function addWorkspaceMember(
  workspaceId: string,
  userId: string,
  role: WorkspaceRoleValue,
  options: ClientOptions = {},
) {
  return requestJson<{ user_id: string; role: WorkspaceRoleValue }>(
    `/v1/admin/workspaces/${encodeURIComponent(workspaceId)}/members`,
    { ...options, method: 'POST', body: { user_id: userId, role } },
  )
}

/** Change an existing member's role (last-owner demotion is refused, 409). */
export async function setWorkspaceMemberRole(
  workspaceId: string,
  userId: string,
  role: WorkspaceRoleValue,
  options: ClientOptions = {},
) {
  return requestJson<{ user_id: string; role: WorkspaceRoleValue }>(
    `/v1/admin/workspaces/${encodeURIComponent(workspaceId)}/members/${encodeURIComponent(userId)}`,
    { ...options, method: 'PATCH', body: { role } },
  )
}

/** Remove a member (last-owner removal is refused, 409). */
export async function removeWorkspaceMember(
  workspaceId: string,
  userId: string,
  options: ClientOptions = {},
) {
  await request(
    `/v1/admin/workspaces/${encodeURIComponent(workspaceId)}/members/${encodeURIComponent(userId)}`,
    { ...options, method: 'DELETE' },
  )
}

// --- Personal access tokens (/api/auth/tokens) ---------------------------
// Session-only management; the plaintext secret is emitted exactly once at
// creation and never retrievable again.

/** One PAT row (never carries the hash or the plaintext). */
export type AccessToken = {
  token_id: string
  name: string
  created_at: number
  expires_at: number | null
  last_used_at: number | null
  scopes: string[]
}

/** The current session owner's tokens. */
export async function listAccessTokens(options: ClientOptions = {}) {
  return requestJson<{ tokens: AccessToken[] }>('/api/auth/tokens', options)
}

/** Mint a token; the response's one-time `token` field is the plaintext. */
export async function createAccessToken(
  input: { name: string; expiresInDays?: number },
  options: ClientOptions = {},
) {
  return requestJson<AccessToken & { token: string }>('/api/auth/tokens', {
    ...options,
    method: 'POST',
    body: { name: input.name, expires_in_days: input.expiresInDays },
  })
}

/** Revoke a token (foreign/unknown ids are an indistinguishable 404). */
export async function revokeAccessToken(
  tokenId: string,
  options: ClientOptions = {},
) {
  return requestJson<{ revoked: boolean }>(
    `/api/auth/tokens/${encodeURIComponent(tokenId)}`,
    { ...options, method: 'DELETE' },
  )
}

async function requestError(response: Response) {
  try {
    const payload = await response.json() as {
      detail?: { error?: InqtrixError }
      error?: InqtrixError
    }
    // Application errors use the top-level shape; FastAPI dependency
    // failures (including the session CSRF guard) wrap the same typed error
    // under `detail`. Normalize both before any recovery decision.
    const error = payload.error ?? payload.detail?.error
    if (error?.message) {
      return readableRequestError({
        detail: error as unknown as Record<string, unknown>,
        serverMessage: error.message,
        status: response.status,
        type: error.type || undefined,
      })
    }
  } catch {
    // The server may return an empty body for infrastructure failures.
  }
  return readableRequestError({
    serverMessage: `HTTP ${response.status}`,
    status: response.status,
  })
}

/** An error in the reader's words that keeps what callers branch on: the
 * type as `name`, the status, the payload, and the server's own sentence. */
function readableRequestError(facts: {
  detail?: Record<string, unknown>
  serverMessage: string
  status?: number
  type?: string
}): InqtrixRequestError {
  const error = new Error(
    requestErrorText({ status: facts.status, type: facts.type }),
  ) as InqtrixRequestError
  error.name = facts.type || 'InqtrixRequestError'
  error.serverMessage = facts.serverMessage
  if (facts.status !== undefined) error.status = facts.status
  if (facts.detail) error.detail = facts.detail
  return error
}

/** A lost connection reads like any failed request. A cancelled request
 * keeps its own reason (an `AbortError`, or the reason a caller aborted
 * with), and the status stays unset: a connection retry keys on that. */
function readableConnectionError(
  error: unknown,
  signal: AbortSignal | null | undefined,
): unknown {
  if (signal?.aborted || (error instanceof Error && error.name === 'AbortError')) return error
  return readableRequestError({
    serverMessage: error instanceof Error ? error.message : String(error),
    type: 'network_error',
  })
}

async function fetchReadable(input: string, init: RequestInit): Promise<Response> {
  try {
    return await fetch(input, init)
  } catch (error) {
    throw readableConnectionError(error, init.signal)
  }
}

async function readStream(
  reader: ReadableStreamDefaultReader<string>,
  signal: AbortSignal | null | undefined,
): Promise<ReadableStreamReadResult<string>> {
  try {
    return await reader.read()
  } catch (error) {
    throw readableConnectionError(error, signal)
  }
}

async function throwRequestError(
  response: Response,
  reloadOnUnauthorized = true,
): Promise<never> {
  const error = await requestError(response)
  return throwParsedRequestError(error, response.status, reloadOnUnauthorized)
}

function throwParsedRequestError(
  error: InqtrixRequestError,
  status: number,
  reloadOnUnauthorized = true,
): never {
  if (
    (error.name === 'principal_changed'
      || (
        reloadOnUnauthorized
        && status === 401
        && expectedUserIdentity !== null
      ))
    && typeof window !== 'undefined'
    && typeof window.location?.reload === 'function'
  ) {
    window.location.reload()
  }
  throw error
}

function resolveUrl(path: string, baseUrl = DEFAULT_BASE_URL) {
  if (/^https?:\/\//.test(path)) return path
  const normalizedBase = baseUrl.replace(/\/$/, '')
  return `${normalizedBase}${path}`
}

function parseSseData(frame: string) {
  const dataLines = frame
    .split(/\r?\n/)
    .filter((line) => line.startsWith('data:'))
    .map((line) => line.slice(5).trimStart())
  return dataLines.length > 0 ? dataLines.join('\n') : null
}

function parseSseFrame(frame: string): {
  data: string | null
  event: string | null
  id: string | null
} {
  let event: string | null = null
  let id: string | null = null
  for (const line of frame.split(/\r?\n/)) {
    if (line.startsWith('event:')) event = line.slice(6).trimStart()
    if (line.startsWith('id:')) id = line.slice(3).trimStart()
  }
  return { data: parseSseData(frame), event, id }
}

function serializeOverrides(overrides?: AgentOverrides) {
  if (!overrides) return undefined
  return {
    max_rounds: overrides.maxRounds,
    min_rounds: overrides.minRounds,
    confidence_stop: overrides.confidenceStop,
    report_profile: overrides.reportProfile,
    max_total_seconds: overrides.maxTotalSeconds,
    first_round_queries: overrides.firstRoundQueries,
    skip_search: overrides.skipSearch,
    model_tier: overrides.modelTier,
    model: overrides.model,
    effort: overrides.effort,
    depth: overrides.depth,
    agent_tier: overrides.agentTier,
  }
}

/** Build the editor ``agent_overrides`` slice from picker selections.
 *  An explicit model/effort (UI picker) takes precedence; modelTier remains
 *  the fallback. Returns undefined when nothing is selected. */
function editorAgentOverrides(req: {
  modelTier?: 'high' | 'mid' | 'fast' | null
  model?: string | null
  effort?: string | null
}) {
  const overrides: Record<string, string> = {}
  if (req.model) overrides.model = req.model
  if (req.effort) overrides.effort = req.effort
  if (req.modelTier) overrides.model_tier = req.modelTier
  return Object.keys(overrides).length > 0 ? overrides : undefined
}

function serializeKnowledgeFilters(filters?: KnowledgeChatFilters) {
  if (!filters) return undefined
  if (filters.collectionIds.length === 0 && filters.scope !== 'all') return undefined
  return {
    // Chips pin; the session switch asks for the whole visible boundary.
    ...(filters.collectionIds.length > 0
      ? { collection_ids: filters.collectionIds }
      : { scope: 'all' as const }),
    ...(filters.topK ? { top_k: filters.topK } : {}),
    ...(filters.finalK ? { final_k: filters.finalK } : {}),
    ...(filters.profile ? { profile: filters.profile } : {}),
  }
}

function serializeChatCompletionRequest(
  request: ChatCompletionRequest,
  stream: boolean,
) {
  return {
    ...(request.incognito ? { incognito: true } : {}),
    include_progress: request.includeProgress ?? false,
    knowledge_filters: serializeKnowledgeFilters(request.knowledgeFilters),
    messages: request.messages,
    mode: request.mode ?? 'direct_llm',
    model: request.model ?? CHAT_MODEL_NAME,
    stack: request.stack,
    stream,
    ui_language: request.uiLanguage,
    agent_overrides: serializeOverrides(request.agentOverrides),
  }
}
