/**
 * Wire types for the agent run control surfaces.
 *
 * Snake_case mirrors the HTTP payloads verbatim (the client returns them
 * unconverted); record conversion lives in the feature layer, matching the
 * `chatHistorySync`/`knowledgeSessionSync` convention. Rows are the truth
 * here: SSE events only signal that one of these should be
 * (re-)fetched.
 */

/** One approval row (`GET /v1/runs/{id}/approvals`). */
export type AgentApprovalWire = {
  approval_id: string
  run_id: string
  /** `tool` for the kernel's gates. A stored run of the retired mission
   * engine also carries `plan`, `replan`, `discovery` and `patch` rows;
   * they are read for its transcript only. */
  kind: string
  status: 'pending' | 'approved' | 'rejected' | 'edited'
  subject_type: string
  subject_id: string
  payload: Record<string, unknown>
  decision: string
  /** Decision-scoped keys (edited actions, `approval_scope: 'run'` for a
   * run-wide tool grant; a stored mission plan gate's report requirement).
   * Older servers omit it. */
  decision_payload?: Record<string, unknown>
  note: string
  decided_by_user_id: string | null
  created_at: number
  decided_at: number | null
}

/** POST decision body for an approval. `actions` only with
 * `decision: 'edit'` on a `tool` gate (exactly one action, args-only
 * change — the backend re-validates the edited args against the live tool
 * schema). */
export type AgentApprovalDecisionRequest = {
  decision: 'approve' | 'reject' | 'edit'
  note?: string
  actions?: { tool: string; args: Record<string, unknown> }[]
  /** Only with `decision: 'approve'` on a `tool` gate: `'run'` grants
   * the gated tools for the rest of the run (balanced mode only —
   * strict stays per-call and `propose_editor_patch` is never
   * grantable); `'once'`/omitted approves this call alone. */
  approval_scope?: 'once' | 'run'
}

/** One pickable option of a structured clarification question. */
export type AgentClarificationOptionWire = {
  id: string
  label: string
  /** Optional one-line explanation (older rows omit it). */
  description?: string
}

/** One structured question of a clarification gate round. */
export type AgentClarificationQuestionWire = {
  id: string
  prompt: string
  options: AgentClarificationOptionWire[]
  multi_select: boolean
}

/** Per-question structured answer (`answers` map values). */
export type AgentClarificationAnswerEntryWire = {
  option_ids: string[]
  text: string
}

/** POST body for answering a clarification — exactly ONE of the three:
 * whole-round free text, a legacy single option, or the structured
 * per-question map (which must resolve EVERY question of the round). */
export type AgentClarificationAnswerRequest = {
  answer?: string
  option_id?: string
  answers?: Record<string, AgentClarificationAnswerEntryWire>
}

/** One clarification row (`GET /v1/runs/{id}/clarifications`). */
export type AgentClarificationWire = {
  clarification_id: string
  run_id: string
  question: string
  options: AgentClarificationOptionWire[]
  /** Structured round payload; empty for legacy single-text rounds
   * (older servers omit the field entirely). */
  questions?: AgentClarificationQuestionWire[]
  /** Structured answers by question id (empty until answered). */
  answers?: Record<string, AgentClarificationAnswerEntryWire>
  default_assumption: string
  status: 'pending' | 'answered'
  answer: string
  option_id: string
  answered_by_user_id: string | null
  created_at: number
  answered_at: number | null
}

/** Artifact metadata (list endpoint — never carries the body). */
export type AgentArtifactMetaWire = {
  artifact_id: string
  run_id: string
  session_id: string | null
  kind:
    | 'memo'
    | 'evidence_bundle'
    | 'critic_report'
    | 'editor_patch'
    | 'answer'
    | 'deliverable'
    | 'context_archive'
  title: string
  status: 'writing' | 'ready'
  revision: number
  updated_by: 'agent' | 'user'
  refs_count: number
  created_at: number
  updated_at: number
}

/** Artifact detail (`GET .../artifacts/{id}?revision=`). */
export type AgentArtifactDetailWire = AgentArtifactMetaWire & {
  content_markdown: string
  /** Kind-specific, bounded artifact data. Evidence bundles use this field
   * for their typed source-read lineage; older servers may omit it. */
  payload?: Record<string, unknown>
  refs: Record<string, unknown>[]
  revisions: { revision: number; created_by: string; created_at: number; status: string }[]
}

/** Agent-session rows (shape-identical to knowledge sessions). */
export type ServerAgentSession = import('@/features/sessionTitle/model').TitleWire & {
  id: string
  title: string
  group_id: string | null
  created_at: number
  updated_at: number
  items_json?: string
  lifecycle_status?: 'active' | 'deleting' | 'delete_failed'
  deletion_operation_id?: string | null
  deletion_stage?: string | null
  deletion_error?: string | null
}

export type ServerAgentSessionGroup = {
  id: string
  title: string
  created_at: number
  updated_at: number
}

/** One anchored edit of an editor patch (the instruct edit shape). */
export type AgentPatchEditWire = {
  id: string
  find: string
  quote_before: string
  quote_after: string
  position: 'replace' | 'before' | 'after' | 'append'
  text: string
  note: string
}

/** Patch detail (`GET /v1/editor/patches/{id}`). */
export type AgentPatchWire = {
  patch_id: string
  document_id: string
  run_id: string | null
  source: 'suggest' | 'instruct' | 'agent'
  status: 'pending' | 'accepted' | 'rejected'
  edit_count: number
  summary: string
  revision_before: number
  applied_revision: number | null
  created_at: number
  decided_at: number | null
  edits: AgentPatchEditWire[]
  warnings: string[]
  applied_edit_ids: string[] | null
  note: string
  /** CURRENT document revision — the FE applies against fresh state. */
  document_revision: number
}
