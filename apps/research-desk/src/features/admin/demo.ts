import type {
  AdminAuditEvent,
  AdminSystemRuntime,
  AccessToken,
  AdminUser,
  AdminWorkspace,
  WorkspaceMember,
  WorkspaceRoleValue,
} from '@/api/inqtrixClient'
import type {
  InqtrixCapabilities,
  InqtrixHealth,
} from '@/features/researchRuns/types'
import { DEMO_OWNER } from '@/features/sharing/demoShares'

// 2026-01-01T00:00:00Z — the neutral fixed instant used across the demo
// digital twin (anonymised demo data; never Date.now()).
const DEMO_TS = 1_767_225_600
const DAY = 86_400
const DEMO_USER_IDS = {
  ines: '00000000-0000-4000-8000-000000000011',
  mara: '00000000-0000-4000-8000-000000000012',
  paul: '00000000-0000-4000-8000-000000000014',
  tomas: '00000000-0000-4000-8000-000000000013',
} as const

/**
 * Seeded instance users for the demo admin surface (digital twin). The
 * owner row IS `DEMO_OWNER` so the "you" badge + self-disable lock line up
 * with the demo session; the mix (a second admin, a disabled account)
 * exercises every cell state and guard the Users panel renders.
 */
export function seedAdminUsers(): AdminUser[] {
  return [
    {
      id: DEMO_OWNER.userId,
      email: DEMO_OWNER.email,
      display_name: DEMO_OWNER.displayName,
      instance_role: 'admin',
      disabled: false,
      last_login_at: DEMO_TS - 2 * DAY,
      erasure_operation_id: null,
    },
    {
      id: DEMO_USER_IDS.ines,
      email: 'ines.adesina@example.com',
      display_name: 'Ines Adesina',
      instance_role: 'admin',
      disabled: false,
      last_login_at: DEMO_TS - 1 * DAY,
      erasure_operation_id: null,
    },
    {
      id: DEMO_USER_IDS.mara,
      email: 'mara.lindqvist@example.com',
      display_name: 'Mara Lindqvist',
      instance_role: 'user',
      disabled: false,
      last_login_at: DEMO_TS - 5 * DAY,
      erasure_operation_id: null,
    },
    {
      id: DEMO_USER_IDS.tomas,
      email: 'tomas.berg@example.com',
      display_name: 'Tomas Berg',
      instance_role: 'user',
      disabled: false,
      last_login_at: DEMO_TS - 11 * DAY,
      erasure_operation_id: null,
    },
    {
      id: DEMO_USER_IDS.paul,
      email: 'paul.henning@example.com',
      display_name: 'Paul Henning',
      instance_role: 'user',
      disabled: true,
      last_login_at: DEMO_TS - 64 * DAY,
      erasure_operation_id: null,
    },
  ]
}

/** One seeded workspace with its members (the demo workspace admin twin). */
export type DemoWorkspaceSeed = {
  workspace: AdminWorkspace
  members: WorkspaceMember[]
}

/**
 * Seeded workspaces for the demo workspace-admin surface. Built over the SAME
 * five seeded users (`seedAdminUsers`) so the member rows enrich with real
 * names/emails and the cross-references line up: every role cell is exercised,
 * an unassigned user exists (the add-member pool is non-empty), and two
 * single-owner workspaces show the last-owner guard.
 */
export function seedAdminWorkspaces(): DemoWorkspaceSeed[] {
  const users = seedAdminUsers()
  const member = (userId: string, role: WorkspaceRoleValue): WorkspaceMember => {
    const user = users.find((candidate) => candidate.id === userId)
    return {
      display_name: user?.display_name ?? null,
      email: user?.email ?? null,
      role,
      user_id: userId,
    }
  }
  const make = (
    workspaceId: string,
    name: string,
    createdBy: string,
    roster: Array<[string, WorkspaceRoleValue]>,
  ): DemoWorkspaceSeed => ({
    members: roster.map(([userId, role]) => member(userId, role)),
    workspace: {
      created_by_user_id: createdBy,
      member_count: roster.length,
      name,
      workspace_id: workspaceId,
    },
  })
  const owner = DEMO_OWNER.userId
  return [
    make('ws-default', 'Default workspace', owner, [
      [owner, 'owner'],
      [DEMO_USER_IDS.ines, 'editor'],
    ]),
    make('ws-research', 'Research Team', owner, [
      [owner, 'owner'],
      [DEMO_USER_IDS.mara, 'editor'],
      [DEMO_USER_IDS.tomas, 'commenter'],
      [DEMO_USER_IDS.paul, 'viewer'],
    ]),
    make('ws-legal', 'Legal', DEMO_USER_IDS.ines, [
      [DEMO_USER_IDS.ines, 'owner'],
      [DEMO_USER_IDS.mara, 'viewer'],
    ]),
  ]
}

/**
 * Seeded personal access tokens for the demo. One scoped + expiring, one
 * read-only + non-expiring — enough to show the list, scopes, and the
 * revoke affordance without a backend.
 */
export function seedAccessTokens(): AccessToken[] {
  return [
    {
      token_id: 'demo-pat-ci',
      name: 'CI pipeline',
      created_at: DEMO_TS - 30 * DAY,
      expires_at: DEMO_TS + 60 * DAY,
      last_used_at: DEMO_TS - 1 * DAY,
      scopes: ['runs:read', 'runs:write'],
    },
    {
      token_id: 'demo-pat-notebook',
      name: 'Analysis notebook',
      created_at: DEMO_TS - 7 * DAY,
      expires_at: null,
      last_used_at: DEMO_TS - 3 * DAY,
      scopes: ['runs:read'],
    },
  ]
}

/**
 * Seeded identity for the demo System panel: a plausible local-mode deployment
 * stack so the read-only panel shows real-looking provider/model identity
 * offline (the live panel reads /health). Only the fields the panel reads
 * are meaningful; the rest satisfy the type.
 */
export function seedSystemHealth(): InqtrixHealth {
  return {
    auth_mode: 'local',
    auth_required: true,
    llm: { provider: 'anthropic', status: 'ready' },
    search: { provider: 'perplexity', status: 'ready' },
    chat_model_options: [
      {
        effort: 'medium',
        effort_source: 'demo',
        model: 'claude-opus-4-8',
        model_source: 'demo',
        requested_tier: 'high',
        tier: 'high',
      },
      {
        effort: 'medium',
        effort_source: 'demo',
        model: 'claude-sonnet-4-6',
        model_source: 'demo',
        requested_tier: 'mid',
        tier: 'mid',
      },
      {
        effort: 'none',
        effort_source: 'demo',
        model: 'claude-haiku-4-5',
        model_source: 'demo',
        requested_tier: 'fast',
        tier: 'fast',
      },
    ],
    models_catalog: [
      {
        card: {
          capabilities: ['analysis', 'long-context', 'reasoning'],
          category: 'high',
          context_window_tokens: 200_000,
          description: 'Deep analysis model for demanding research answers.',
          display_name: 'Claude Opus 4.8',
          id: 'claude-opus-4-8',
          input_modalities: ['text', 'image'],
          knowledge_cutoff: '2026-01',
          max_output_tokens: 32_000,
          pricing: { input_per_mtok: 15, output_per_mtok: 75 },
          reasoning_levels: ['minimal', 'medium', 'high'],
          speed: 'langsam',
          vendor: 'Anthropic',
        },
        model_id: 'claude-opus-4-8',
      },
      {
        card: {
          capabilities: ['analysis', 'chat', 'coding'],
          category: 'mid',
          context_window_tokens: 200_000,
          description: 'Balanced default model for everyday chat and research.',
          display_name: 'Claude Sonnet 4.6',
          id: 'claude-sonnet-4-6',
          input_modalities: ['text', 'image'],
          knowledge_cutoff: '2026-01',
          max_output_tokens: 32_000,
          pricing: { input_per_mtok: 3, output_per_mtok: 15 },
          reasoning_levels: ['minimal', 'medium', 'high'],
          speed: 'mittel',
          vendor: 'Anthropic',
        },
        model_id: 'claude-sonnet-4-6',
      },
      {
        card: {
          capabilities: ['chat', 'summarization'],
          category: 'fast',
          context_window_tokens: 200_000,
          description: 'Fast, low-latency model for compact responses.',
          display_name: 'Claude Haiku 4.5',
          id: 'claude-haiku-4-5',
          input_modalities: ['text'],
          knowledge_cutoff: '2026-01',
          max_output_tokens: 16_000,
          pricing: { input_per_mtok: 0.8, output_per_mtok: 4 },
          reasoning_levels: [],
          speed: 'schnell',
          vendor: 'Anthropic',
        },
        model_id: 'claude-haiku-4-5',
      },
    ],
    node_models: {
      direct_chat: {
        effort: 'medium',
        effort_source: 'demo',
        model: 'claude-sonnet-4-6',
        model_source: 'demo',
        node: 'direct_chat',
        requested_tier: 'mid',
        tier: 'mid',
      },
    },
    status: 'ok',
  }
}

/** Seeded feature matrix for the demo System panel (open features map). */
export function seedSystemCapabilities(): InqtrixCapabilities {
  return {
    algorithms: [],
    features: {
      contextual_retrieval: false,
      document_parser: true,
      embedding_provider: true,
      files: true,
      hybrid_retrieval: true,
      knowledge: true,
    multi_stack: false,
      openapi: true,
      prompt_templates: true,
      quota: true,
      reranker: false,
      sharing: true,
    },
    // Default-derived waits so the demo System Status panel shows the
    // same timeouts shape the real /v1/capabilities publishes. The demo editor/chat
    // client-abort path still uses the fixed fallbacks: discovery is disabled in
    // demo mode, so the run paths receive capabilities = null, not this seed.
    timeouts: {
      editor_wait_seconds: 630,
      chat_wait_seconds: 3630,
      text_wait_seconds: 630,
      reasoning_operation_seconds: 600,
      editor_operation_seconds: 600,
      search_operation_seconds: 600,
      claim_extract_operation_seconds: 600,
      research_run_seconds: 3600,
      max_attempts: 3,
    },
  }
}

/** Seeded runtime manifest for the demo System panel. */
export function seedAdminSystemRuntime(): AdminSystemRuntime {
  return {
    api: {
      openapi: true,
      chat_max_concurrent: 100,
      stream_reader_workers: 128,
    },
    files: {
      blob_storage: 'volume',
      document_parser: 'markitdown',
      enabled: true,
      max_file_bytes: 52_428_800,
      object_store: 'local',
      object_store_available: true,
    },
    knowledge: {
      contextual_retrieval: false,
      default_top_k: 8,
      embedding_model: 'text-embedding-3-large',
      embedding_provider: 'azure',
      enabled: true,
      hybrid_retrieval: true,
      reranker: 'none',
      sparse: 'bm25_german',
      vector_store: 'qdrant',
      vector_store_available: true,
    },
    runs: {
      execution: 'in_process',
      queue: 'memory',
      queue_available: true,
      queue_consumers: null,
      queue_depth: null,
      store: 'postgres',
      worker_dispatch: false,
      admission_max_concurrent: 100,
      queue_max_size: 100,
    },
    agents: {
      checkpointer_pool_size: 4,
    },
    storage: {
      backend: 'postgres',
      durable: true,
    },
    observability: {
      tracing: 'otlp',
      tracing_active: true,
      content_capture: false,
      sample_rate: 1,
      spool: false,
      retention_enforced: false,
      retention_days: 30,
      ui_link_configured: true,
    },
  }
}

/**
 * Seeded audit trail for the demo digital twin: one page covering every
 * audit catalog family (service starts, AuthN, deletion lifecycle, export)
 * so the audit panel shows each badge/outcome/correlation state.
 */
export function seedAuditLog(): AdminAuditEvent[] {
  const actor = 'usr_8fd3cc408accc745'
  const rows: AdminAuditEvent[] = [
    {
      id: 12,
      occurred_at: DEMO_TS + 4 * 3600,
      action: 'export.trace',
      resource_type: 'run',
      resource_id: 'run_demo_research_1',
      actor_pseudonym: actor,
      actor_type: 'user',
      outcome: 'success',
      workspace_id: null,
      detail: { source: 'langfuse' },
      origin: { auth_method: 'oidc_session', ip: '203.0.113.7' },
      correlation: {
        request_id: 'req-demo-12',
        run_id: 'run_demo_research_1',
        trace_id: '4374a53e1ce2ac54051985e27e6ac9f7',
      },
    },
    {
      id: 11,
      occurred_at: DEMO_TS + 3 * 3600 + 240,
      action: 'run.completed',
      resource_type: 'run',
      resource_id: 'run_demo_research_1',
      actor_pseudonym: actor,
      actor_type: 'user',
      outcome: 'success',
      workspace_id: null,
      detail: {
        mode: 'research',
        duration_s: 184.2,
        prompt_tokens: 48213,
        completion_tokens: 9877,
      },
      origin: {},
      correlation: {
        run_id: 'run_demo_research_1',
        trace_id: '4374a53e1ce2ac54051985e27e6ac9f7',
      },
    },
    {
      id: 10,
      occurred_at: DEMO_TS + 3 * 3600,
      action: 'chat.completed',
      resource_type: 'chat',
      resource_id: 'req-demo-10',
      actor_pseudonym: actor,
      actor_type: 'user',
      outcome: 'success',
      workspace_id: null,
      detail: { streamed: 'true', prompt_tokens: 921, completion_tokens: 305 },
      origin: { auth_method: 'oidc_session' },
      correlation: { request_id: 'req-demo-10' },
    },
    {
      id: 9,
      occurred_at: DEMO_TS + 2 * 3600 + 1800,
      action: 'run.failed',
      resource_type: 'run',
      resource_id: 'run_demo_agent_7',
      actor_pseudonym: 'usr_1f22ab90cd34ef56',
      actor_type: 'user',
      outcome: 'failure',
      workspace_id: null,
      detail: { mode: 'agent_kernel', duration_s: 42.7, error_type: 'timeout' },
      origin: {},
      correlation: {
        run_id: 'run_demo_agent_7',
        trace_id: 'daa36bb122975f423450706c96a0ee4b',
      },
    },
    {
      id: 8,
      occurred_at: DEMO_TS + 2 * 3600,
      action: 'asset.delete_completed',
      resource_type: 'asset',
      resource_id: 'asset_demo_31',
      actor_pseudonym: actor,
      actor_type: 'user',
      outcome: 'success',
      workspace_id: null,
      detail: { stage: 'deleted' },
      origin: { auth_method: 'oidc_session' },
      correlation: { run_id: 'delop_demo_5' },
    },
    {
      id: 7,
      occurred_at: DEMO_TS + 2 * 3600 - 90,
      action: 'asset.delete_requested',
      resource_type: 'asset',
      resource_id: 'asset_demo_31',
      actor_pseudonym: actor,
      actor_type: 'user',
      outcome: 'success',
      workspace_id: null,
      detail: { manifest_items: 3 },
      origin: { auth_method: 'oidc_session', ip: '203.0.113.7' },
      correlation: { request_id: 'req-demo-7', run_id: 'delop_demo_5' },
    },
    {
      id: 6,
      occurred_at: DEMO_TS + 90 * 60,
      action: 'file.uploaded',
      resource_type: 'file',
      resource_id: 'file_demo_88',
      actor_pseudonym: 'usr_1f22ab90cd34ef56',
      actor_type: 'user',
      outcome: 'success',
      workspace_id: null,
      detail: { size_bytes: '1048576', mime: 'application/pdf' },
      origin: { auth_method: 'oidc_session' },
      correlation: { request_id: 'req-demo-6', run_id: 'upop_demo_2' },
    },
    {
      id: 5,
      occurred_at: DEMO_TS + 3600,
      action: 'indexing.completed',
      resource_type: 'knowledge_collection',
      resource_id: 'col_demo_energie',
      actor_pseudonym: actor,
      actor_type: 'user',
      outcome: 'success',
      workspace_id: null,
      detail: {},
      origin: {},
      correlation: { run_id: 'rix_demo_4' },
    },
    {
      id: 4,
      occurred_at: DEMO_TS + 2700,
      action: 'guest_link.accessed',
      resource_type: 'guest_link',
      resource_id: 'lnk_demo_2',
      actor_pseudonym: null,
      actor_type: 'user',
      outcome: 'success',
      workspace_id: null,
      detail: { document_id: 'doc_demo_9' },
      origin: { auth_method: 'guest_link', ip: '198.51.100.23' },
      correlation: { request_id: 'req-demo-4' },
    },
    {
      id: 3,
      occurred_at: DEMO_TS + 1800,
      action: 'auth.lockout',
      resource_type: 'account',
      resource_id: 'mara@example.com',
      actor_pseudonym: null,
      actor_type: 'user',
      outcome: 'denied',
      workspace_id: null,
      detail: {},
      origin: { auth_method: 'local', ip: '198.51.100.44' },
      correlation: { request_id: 'req-demo-3' },
    },
    {
      id: 2,
      occurred_at: DEMO_TS + 1795,
      action: 'auth.login_failed',
      resource_type: 'account',
      resource_id: 'mara@example.com',
      actor_pseudonym: null,
      actor_type: 'user',
      outcome: 'failure',
      workspace_id: null,
      detail: { reason: 'invalid_credentials' },
      origin: { auth_method: 'local', ip: '198.51.100.44' },
      correlation: { request_id: 'req-demo-2' },
    },
    {
      id: 1,
      occurred_at: DEMO_TS + 600,
      action: 'auth.logout',
      resource_type: 'session',
      resource_id: 'ses_demo_1',
      actor_pseudonym: 'usr_1f22ab90cd34ef56',
      actor_type: 'user',
      outcome: 'success',
      workspace_id: null,
      detail: {},
      origin: { auth_method: 'oidc_session' },
      correlation: { request_id: 'req-demo-1' },
    },
  ]
  return rows
}

/** Step events behind the demo run drawer (run_demo_research_1). */
export function seedAdminRunEvents(runId: string) {
  if (runId !== 'run_demo_research_1') return []
  const base = DEMO_TS + 3 * 3600
  return [
    {
      type: 'inqtrix.run.trace',
      run_id: runId,
      sequence: 1,
      created_at: base + 1,
      data: { trace_id: '4374a53e1ce2ac54051985e27e6ac9f7' },
    },
    {
      type: 'phase',
      run_id: runId,
      sequence: 2,
      created_at: base + 2,
      data: { status: 'running', node: 'classify' },
    },
    {
      type: 'phase',
      run_id: runId,
      sequence: 3,
      created_at: base + 31,
      data: { status: 'running', node: 'search', round: 1 },
    },
    {
      type: 'phase',
      run_id: runId,
      sequence: 4,
      created_at: base + 122,
      data: { status: 'running', node: 'evaluate', confidence: 7.5 },
    },
    {
      type: 'inqtrix.run.completed',
      run_id: runId,
      sequence: 5,
      created_at: base + 184,
      data: { status: 'completed' },
    },
  ]
}
