import {
  EDITOR_SCHEMA_BEHAVIOR_INPUTS,
  type SerializedRelativePosition,
} from '@inqtrix/editor-schema'
import type { JobFilter, JobPhase, JobStatus, AppView } from '@/features/researchDesk/types'
import type { ExplorerSortState } from '@/features/project/explorerSort'
import type { AutoResumePlan } from '@/features/fileLibrary/indexingTypes'
import type { Locale } from '@/i18n/translations'
import { asFiniteNumber, asNonEmptyString } from '@/lib/coerce'
import type {
  ContrastMode,
  ThemeMode,
  ThemePreset,
  UserBubbleTone,
} from '@/theme/ThemeProvider'
import type { AgentSessionModelSelection } from '@/features/agent/executionPolicy'
import type {
  ResearchClaim,
  ResearchMetrics,
  ReportReference,
  ChatModelTier,
  ModelTierPreference,
  KnowledgeRetrievalDegradation,
  KnowledgeSearchWarning,
  ResearchRunEvent,
  ResearchRunMode,
  ResearchRunResult,
  ResearchRunAccess,
  ResearchRunSummary,
  ResearchRunSnapshot,
  ResearchSource,
} from '@/features/researchRuns/types'

export const PROJECT_SCHEMA_VERSION = 1

export type ProjectConnectionKind = 'demo' | 'directory' | 'download' | 'local'

export type ProjectConnection = {
  directoryHandle?: FileSystemDirectoryHandle
  directoryName?: string
  kind: ProjectConnectionKind
  writable: boolean
}

export type ProjectMetadata = {
  createdAt: string
  name: string
  schemaVersion: typeof PROJECT_SCHEMA_VERSION
  updatedAt: string
}

import type {
  AgentRunRecord,
  AgentSessionArtifactIndex,
  AgentSessionGroupRecord,
  AgentSessionRecord,
} from '@/features/agent/model'
import type { CanvasState } from '@/features/canvas/types'
import type { AgentPlanDraft } from '@/features/agent/plan/usePlanApproval'

export type PinnedExplorerState = {
  chatThreadIds: string[]
  editorDocumentIds: string[]
  knowledgeSessionIds: string[]
  agentSessionIds: string[]
}

export type ProjectPanelLayoutState = {
  chatHistory: number
  knowledgeHistory: number
  knowledgeSource: number
  researchReport: number
  agentSessions: number
  agentCanvas: number
  editorTree: number
  editorComments: number
}

export type ProjectUiState = {
  activeFilter: JobFilter
  activeView: AppView
  chatChainingEnabled: boolean
  expandedJobId: string | null
  isAgentSessionsVisible: boolean
  isChatHistoryVisible: boolean
  isKnowledgeHistoryVisible: boolean
  isComposerVisible: boolean
  isReportExpanded: boolean
  isReportVisible: boolean
  panelLayout: ProjectPanelLayoutState
  pendingChatAttachmentRefs: ChatContextReferenceRecord[]
  pendingChatReportRunId: string | null
  /** Per-desk sidebar sort mode (recent | name | manual); a drag in an
   * automatic mode switches the desk to manual visibly. */
  explorerSort: ExplorerSortState
  pinnedExplorer: PinnedExplorerState
  selectedChatModel: string | null
  selectedChatEffort: string | null
  selectedChatModelTier: ChatModelTier | null
  /** Agent-run model override. Deliberately SEPARATE from the
   * chat selection: agent runs have a different cost profile, so a
   * chat pick must never silently raise agent spend. */
  selectedAgentModel: string | null
  selectedAgentEffort: string | null
  selectedAgentModelTier: ChatModelTier | null
  selectedChatThreadId: string | null
  /** Persisted selection INTENT for the Agent Desk. Sessions are
   * server-hydrated, so load-time membership validation is impossible —
   * the workspace restores this id (or the most recent session) once
   * hydration lands, instead of showing the empty new-session state. */
  selectedAgentSessionId: string | null
  selectedJobId: string | null
  selectedStack: string
}

export type EditorDocumentSource = 'blank' | 'imported-research-report' | 'pasted' | 'agent-artifact'

export type EditorDocumentContentMode = 'collaboration' | 'markdown'

export type EditorDocumentAccess = {
  mode: 'owner' | 'shared'
  owner?: {
    id: string
    name: string
  }
  permission: 'edit' | 'suggest' | 'view'
}

export type EditorDocumentCollaboration = {
  commentRevision?: number
  generation: number
  persistedSequence: number
  projectionSequence: number
  projectionUpdatedAt?: string
  schemaVersion: number
}

export type EditorCollaborationConnectionStatus =
  | 'access_revoked'
  | 'connected'
  | 'connecting'
  | 'error'
  | 'inactive'
  | 'incompatible'
  // The server refused this page origin. Terminal like an incompatibility,
  // but a different remedy: the operator fixes an address, not a version.
  | 'origin_rejected'
  | 'read_only'
  | 'reconnecting'

export type EditorCollaborationDurabilityStatus =
  | 'error'
  | 'idle'
  | 'pending'
  | 'saved'

export type EditorCollaborationUser = {
  color: string
  id: string
  kind?: 'guest' | 'user'
  link_label?: string
  name: string
}

export type EditorFolderRecord = {
  createdAt: string
  id: string
  title: string
  updatedAt: string
}

export type EditorCommentStatus = 'open' | 'resolved' | 'stale'

/** Die Arten privater Notizen -- plus einen Maschinentraeger.
 *
 * `assistant_edit` ist keine Notiz des Nutzers, sondern die Zeile, an der ein
 * Assistentenlauf seinen privaten Entwurf befestigt. Nur ein solcher Entwurf
 * autorisiert spaeter die Veroeffentlichung genau dieses Vorschlags. Die
 * Notizliste und ihre Zaehler zeigen die Art nicht, und sie ist nicht erneut
 * ausfuehrbar -- sonst waere jeder Maschinen-Edit eine Aufgabe fuer den
 * Nutzer, die er nie gestellt hat. */
export type EditorCommentKind =
  | 'collect'
  | 'inline_edit'
  | 'evidence_review'
  | 'assistant_edit'

export type EditorCommentAnchorRecord = {
  blockId?: string
  from: number
  quoteAfter: string
  quoteBefore: string
  relativeFrom?: SerializedRelativePosition
  relativeTo?: SerializedRelativePosition
  relativeVersion?: typeof EDITOR_SCHEMA_BEHAVIOR_INPUTS.relativePositions
  selectedMarkdown?: string
  selectedText: string
  to: number
}

export type EditorCommentThreadRecord = {
  anchor: EditorCommentAnchorRecord
  commentMarkdown: string
  createdAt: string
  documentId: string
  id: string
  kind: EditorCommentKind
  suggestionDraft?: EditorPrivateSuggestionDraftRecord
  status: EditorCommentStatus
  updatedAt: string
}

export type EditorSuggestionStatus = 'pending' | 'accepted' | 'rejected' | 'stale'
export type EditorSuggestionEditPosition = 'replace' | 'before' | 'after' | 'append'
export type EditorSuggestionRevisionSource = 'llm_refine' | 'manual_edit'

export type EditorSuggestionRevisionRecord = {
  changeSummary?: string[]
  createdAt: string
  instruction?: string
  proposedText: string
  source: EditorSuggestionRevisionSource
  warnings?: string[]
}

export type EditorPrivateSuggestionDraftRecord = {
  anchorVersion: 1
  /** Der Suchtext, an dem dieser Vorschlag haengt.
   *
   * Beim Kommentarweg steht er im Kommentar selbst. Ein Assistentenlauf hat
   * keinen Kommentar des Nutzers, also muss der Entwurf ihn tragen: ohne ihn
   * findet der Uebernahmepfad nach einem Neuladen die Stelle nicht wieder. */
  anchorText?: string
  changeSummary?: string[]
  createdAt: string
  /** Ob ersetzt oder eingefuegt wird -- und wo.
   *
   * Der Kommentarweg kennt nur "ersetze den markierten Bereich" und brauchte
   * das Feld nie. Ein Assistentenlauf fuegt in drei von vier Faellen ein.
   * Fehlt die Angabe, baut der Uebernahmepfad den Vorschlag als Ersetzung neu
   * auf: aus "nach dem Anker einfuegen" wird "den Anker ersetzen", und der
   * Nutzer verliert stillschweigend Text. */
  editPosition?: EditorSuggestionEditPosition
  groupId: string
  patchId: string
  proposedText: string
  publicationCommandId: string
  revision: number
  revisionHistory?: EditorSuggestionRevisionRecord[]
  suggestionId: string
  updatedAt: string
  warnings?: string[]
}

/** Woher ein Vorschlag stammt.
 *
 * `assistant_edit` ist eigenstaendig und nicht mit `global_run` zu verwechseln:
 * `global_run` traegt auch dann eine `commentId`, wenn der Lauf aus einer
 * Sammel-Notiz des Nutzers kam. Wer Anweisungslaeufe an
 * `kind === 'global_run'` erkennen will, trifft darum auch die Vorschlaege zu
 * fremden Notizen -- und verwirft oder verdoppelt sie. */
export type EditorSuggestionOrigin =
  | { commentId?: string; kind: 'global_run' }
  | { commentId: string; kind: 'inline_edit' }
  | { commentId: string; kind: 'assistant_edit' }


export type EditorSuggestionCollaborationPublication = {
  commandId: string
  patchId: string
  sequence: number
  suggestionIds: string[]
}

export type EditorSuggestionRecord = {
  anchor: EditorCommentAnchorRecord
  anchorText?: string
  blockId: string
  changeSummary?: string[]
  collaborationPublication?: EditorSuggestionCollaborationPublication
  createdAt: string
  documentId: string
  editPosition?: EditorSuggestionEditPosition
  groupId: string
  id: string
  originalMarkdown?: string
  originalText: string
  origin: EditorSuggestionOrigin
  privateDraft?: {
    patchId: string
    publicationCommandId: string
    revision: number
  }
  /** Server-side agent patch this suggestion mirrors: decisions go
   * through the official patch endpoints (`:apply`/`:reject`, whole
   * patch), never the client-side edit path. Absent for editor-born
   * suggestions, which have no server row. */
  serverPatch?: {
    patchId: string
    editId: string
  }
  proposedText: string
  revision?: number
  revisionHistory?: EditorSuggestionRevisionRecord[]
  status: EditorSuggestionStatus
  updatedAt: string
  warnings?: string[]
}

export type EditorSuggestionGroupRecord = {
  assistantMessage?: string
  createdAt: string
  documentId: string
  id: string
  origin: EditorSuggestionOrigin
  warnings?: string[]
}

/** Why a local recovery copy of an editor document exists. */
export type EditorDocumentRecoveryReason = 'generation_replaced' | 'remote_deleted'

export type EditorDocumentRecord = {
  /** Authoritative caller relationship; absent on local/legacy project files. */
  access?: EditorDocumentAccess
  /** Server-owned Yjs room metadata; present only in collaboration mode. */
  collaboration?: EditorDocumentCollaboration
  contentMarkdown: string
  /** Absent on local project files and interpreted as legacy markdown mode. */
  contentMode?: EditorDocumentContentMode
  createdAt: string
  diffAnchorMarkdown?: string
  diffAnchorUpdatedAt?: string
  folderId: string | null
  id: string
  /** Independent compare-and-swap revision for title/folder changes. */
  metadataRevision?: number
  /**
   * Marks a local-only recovery copy created from work that had not reached
   * the server: the authoritative document disappeared (`remote_deleted`), or
   * it was rebuilt as a new collaboration generation while the service could
   * no longer confirm local changes of the old one (`generation_replaced`).
   * Recovery copies never reuse the server id and remain outside autosave
   * until the user explicitly saves them as a new document.
   */
  recovery?: {
    capturedAt: string
    originalDocumentId: string
    reason: EditorDocumentRecoveryReason
  }
  revision: number
  /**
   * Local provenance set only after this exact id was observed in a server
   * response or acknowledged by a successful save. It distinguishes a
   * remotely deleted document from a never-confirmed offline draft.
   */
  serverSynced?: boolean
  source: EditorDocumentSource
  sourceRunId?: string
  title: string
  updatedAt: string
}

export type EditorPanelTab = 'comments' | 'assistant'

export type EditorCommentOutboxEntry = {
  documentId: string
  operation: 'delete' | 'upsert'
  updatedAt?: string
}

export type EditorViewMode = 'live' | 'source'

export type EditorUiState = {
  activeDocumentId: string | null
  assistantDraft: string
  isAssistantVisible: boolean
  isCommentPanelVisible: boolean
  isDiffVisible: boolean
  isTreeVisible: boolean
  openDocumentIds: string[]
  panelTab: EditorPanelTab
  selectedCommentId: string | null
  viewMode: EditorViewMode
}

export type ProjectPreferences = {
  agentMemoryEnabled: boolean
  /** Tier a new agent run starts on. SEPARATE from the chat preference on
   * purpose: an agent run fans out over several thinking nodes while a chat
   * answer is a single call, so a chat pick must never raise agent spend.
   * Mirrors the same split on {@link ProjectUiState}. */
  agentModelTier: ModelTierPreference
  /** Tier a new chat starts on. */
  chatModelTier: ModelTierPreference
  contrastMode: ContrastMode
  locale: Locale
  theme: ThemeMode
  themePreset: ThemePreset
  userBubbleTone: UserBubbleTone
}

export type ResearchRunSource = 'api' | 'imported' | 'mock'

export type ResearchRunEventKind = 'progress' | 'system'

export type ResearchRunEventSeverity = 'error' | 'info' | 'success' | 'warning'

export type ResearchRunEventRecord = {
  active?: boolean
  /** True only for an event that arrived on the LIVE side of its stream —
   * after the server's replay/live boundary marker. Absent (history: SSE
   * replay after a reload, the first polling page, imported/persisted
   * records) the row renders in place; only live arrivals animate. */
  arrivedLive?: boolean
  createdAt: string
  id: string
  kind: ResearchRunEventKind
  phase?: JobPhase
  severity: ResearchRunEventSeverity
  title: string
}

export type ResearchRunPhaseState = {
  activePhase: JobPhase
  completedPhases: JobPhase[]
}

export type ResearchRunCardSummary = {
  queueNote?: string
  score?: string
  title: string
}

export type ResearchRunCardMetrics = {
  claims: number
  queries: number
  rounds: string
  sources: number
}

export type ResearchRunResultRecord = {
  markdown: string
  metrics?: Partial<ResearchMetrics>
  references: ReportReference[]
  topClaims: ResearchClaim[]
  topSources: ResearchSource[]
  usage?: ResearchRunResult['usage']
  /** Runtime receipt from an authorized API read; not a local Markdown cache. */
  webSearchLedger?: Record<string, unknown>
}

export type ResearchRunRecord = {
  /** Set by the event channel when the run answered 404/401: no longer
   * available to this session. Deliberately NOT a status -- the server
   * did not say why (non-disclosing 404), so the last known status stays
   * and the UI renders a calm lock instead of a claim. */
  unavailable?: boolean
  access?: ResearchRunAccess
  agentOverrides: Record<string, unknown>
  /** Transient server state: a cancel is pending on a still-running run.
   * Hydrated from the API summary only (never serialized to project files:
   * the pending state would be stale by the next load). */
  cancelRequested?: boolean
  createdAt: string
  /** Run-tree role; absent = plain standalone run (historical shape). */
  kind?: 'standard' | 'agent' | 'agent_child'
  parentRunId?: string
  sessionId?: string
  durationSeconds?: number
  error?: string
  /** Transient: the last request for this completed run's result failed.
   * Deliberately NOT a status -- the run itself completed on the server, so
   * the report surface offers a retry instead of a failure claim. Never
   * serialized to project files. */
  resultError?: string
  events: ResearchRunEventRecord[]
  finishedAt?: string
  /** Whether this run's report is offered in the `@research` mention
   * autocomplete. Absent/`true` = available (the historical, default-on shape);
   * only an explicit `false` hides it. Gated at the mention source only
   * (`mentionableReportOptions`), never on already-attached report chips. */
  includeInAutocomplete?: boolean
  metrics: ResearchRunCardMetrics
  mode?: ResearchRunMode
  phaseState: ResearchRunPhaseState
  queuePosition?: number | null
  result?: ResearchRunResultRecord
  runId: string
  snapshot?: ResearchRunSnapshot
  source: ResearchRunSource
  stack: string
  startedAt?: string
  status: JobStatus
  submittedAt: string
  summary: ResearchRunCardSummary
}

export type ChatRole = 'assistant' | 'user'

export type ChatRuleCategory = 'context' | 'function' | 'instruction'

export type ChatRuleVisibility = {
  /** Agent Desk: a rule can be attached to a mission's plan gate as a
   * result requirement. Off by default — an existing rule was written
   * for chat or the editor and should not start steering reports
   * without its owner saying so. */
  agent: boolean
  chat: boolean
  editor: boolean
}

export type ChatRuleRecord = {
  /** Canonical server authorization metadata; absent only for local rules. */
  access?: ResearchRunAccess
  category?: ChatRuleCategory
  contentMarkdown: string
  createdAt: string
  id: string
  includeInAutocomplete?: boolean
  label: string
  linkedContextRefs?: ChatContextReferenceRecord[]
  /** Server template id once synced; absent = browser-local rule. */
  serverTemplateId?: string
  /** Integer compare-and-swap revision loaded from the server. */
  serverRevision?: number
  /** Size of the block this rule composes into as an attached result
   * requirement, measured by the server for `serverRevision` (absent =
   * unknown: never synced, or a server that does not report it). */
  serverComposedChars?: number
  title: string
  updatedAt: string
  visibility?: ChatRuleVisibility
}

export type ChatThreadGroupRecord = {
  createdAt: string
  id: string
  title: string
  updatedAt: string
}

export type ResearchReportAttachmentRecord = {
  attachedAt: string
  contentMarkdown: string
  kind: 'research-report'
  label?: string
  runId: string
  title: string
}

export type ChatRuleAttachmentRecord = {
  attachedAt: string
  contentMarkdown: string
  kind: 'chat-rule'
  label: string
  ruleId: string
  title: string
}

export type FileParseStatus = 'parsed' | 'partial' | 'unsupported' | 'error'

export type FileAssetOrigin = 'chat' | 'editor' | 'library'

export type FileAssetLifecycleStatus = 'active' | 'deleting' | 'delete_failed'

/** Server-owned original-file upload lifecycle.  The intermediate values are
 * deliberately more precise than a boolean spinner: after a reload the UI can
 * distinguish bytes that have not moved yet, a queued dependency retry, and
 * finalisation of an already durable object. */
export type FileAssetUploadStatus =
  | 'awaiting_upload'
  | 'uploading'
  | 'retrying'
  | 'parsing'
  | 'finalizing'
  | 'ready'
  | 'failed'
  | 'cancelled'

export type FileAssetBodyLoadState = {
  error: string | null
  status: 'failed' | 'loading' | 'ready'
}

export type FileSectionKind = 'temporary' | 'custom'
export type FileSectionSemanticRole =
  | 'temporary'
  | 'library'
  | 'project_sources'
  | 'custom'

export type FileLibrarySectionRecord = {
  createdAt: string
  /** Server-owned aggregate deletion state. Kept on the section itself so an
   * empty-section operation remains visible and retryable after navigation or
   * a project reload; the durable operation feed refreshes these fields. */
  deletionError?: string | null
  deletionOperationId?: string | null
  deletionStage?: string | null
  id: string
  /** Transient/project-persisted marker for the untouched sections created
   * while the client is bootstrapping. Server records intentionally omit it:
   * once a section exists remotely it is authoritative user data. */
  isBootstrapPlaceholder?: boolean
  kind: FileSectionKind
  lifecycleStatus?: FileAssetLifecycleStatus
  /** Server-owned stable meaning. `null` identifies a row created before the
   * semantic-role contract; clients never infer a role from a translated
   * title. */
  semanticRole?: FileSectionSemanticRole | null
  /** Local provenance marker set only after this exact id was observed or
   * accepted by the persistence service. It is not deletion authority; it
   * only permits an exact retained server receipt to claim an unprojected row. */
  serverSynced?: boolean
  title: string
  updatedAt: string
}

export type FileGroupRecord = {
  createdAt: string
  deletionError?: string | null
  deletionOperationId?: string | null
  deletionStage?: string | null
  id: string
  lifecycleStatus?: FileAssetLifecycleStatus
  sectionId: string
  serverSynced?: boolean
  title: string
  updatedAt: string
}

export type FileAssetRecord = {
  createdAt: string
  extractedText: string
  fileName: string
  groupId: string | null
  id: string
  label: string
  mimeType: string
  origin: FileAssetOrigin
  pageCount: number | null
  parseStatus: FileParseStatus
  parseWarning: string | null
  sectionId: string
  sizeBytes: number
  textTruncated: boolean
  title: string
  updatedAt: string
  /** Server-owned aggregate lifecycle. A row remains addressable while its
   * blob, knowledge evidence, and vector memberships are being removed. */
  lifecycleStatus?: FileAssetLifecycleStatus
  deletionOperationId?: string | null
  deletionStage?: string | null
  deletionError?: string | null
  /**
   * Server file id (`fl_...`) when the original was uploaded to the
   * connected backend (`features.files`). `null`/absent = local-only
   * asset — every feature keeps working from `extractedText`.
   */
  serverFileId?: string | null
  /** Local provenance marker set only by server hydration/confirmation. A
   * missing asset-list row is never interpreted as deletion; an exact durable
   * deletion receipt may reconcile this row only when the marker is true. */
  serverSynced?: boolean
  /**
   * Which parser produced `extractedText`: `'markitdown'` (the server
   * parser ladder, used when `features.document_parser` is on) or
   * `'client'` (the in-browser parser). `null`/absent = unknown (e.g.
   * loaded from a local `.md` save, which omits this like `serverFileId`).
   * Display-only provenance for the file card badge.
   */
  parserId?: string | null
  /** Server-owned parser and immutable content identity for the canonical
   * body admitted to Chat/Editor model context. Metadata proves preparation;
   * preparedText itself is loaded only from the asset detail endpoint. */
  preparedParserId?: string | null
  preparedContentHash?: string | null
  preparedAt?: string | null
  preparedText?: string
  /**
   * Transient, client-only: a background server (MarkItDown) parse is in
   * flight, kicked off right after upload to upgrade the instant client parse.
   * Drives the "Parsing…" badge. Never persisted/synced (no server column);
   * absent after a reload, at which point the upgrade happens at index time.
   */
  parsePending?: boolean
  /** The original bytes are queued or in flight. Created locally before the
   * request and hydrated from the server-owned upload lifecycle afterwards,
   * so reloads and second tabs retain a truthful uploading state. */
  uploadPending?: boolean
  /** Durable server state for the original-file upload. Undefined is reserved
   * for deliberately local assets (for example incognito attachments). */
  uploadStatus?: FileAssetUploadStatus
  /** Stable operation identity once the multipart body has been fully spooled
   * and hashed. Reservations intentionally have no operation id yet. */
  uploadOperationId?: string | null
  /** Visible failure for the last original-file upload. Connected assets
   * receive this from the durable server lifecycle; local-only failures keep
   * the same field until an explicit retry succeeds. */
  uploadError?: string | null
}

/** Embedding model identifier. Open string: the authoritative catalog
 * comes from the backend (`GET /v1/capabilities` -> embedding_catalog)
 * when the knowledge engine is enabled; the literals in EMBED_MODELS
 * below are only the demo/offline fallback. */
export type EmbedModelId = string

export type EmbedModelDescriptor = {
  dims: number
  id: EmbedModelId
  label: string
  provider: string
}

/** FALLBACK embedding catalog for demo and offline modes only. Connected
 * deployments with the knowledge engine enabled replace this with the
 * server-provided catalog from `GET /v1/capabilities`; entries here are
 * never sent to a live backend. */
export const EMBED_MODELS: readonly EmbedModelDescriptor[] = [
  { dims: 3072, id: 'text-embedding-3-large', label: 'text-embedding-3-large', provider: 'OpenAI' },
  { dims: 1536, id: 'text-embedding-3-small', label: 'text-embedding-3-small', provider: 'OpenAI' },
  { dims: 1024, id: 'voyage-3-large', label: 'voyage-3-large', provider: 'Voyage' },
  { dims: 4096, id: 'e5-mistral-7b', label: 'e5-mistral-7b', provider: 'open' },
]

export const DEFAULT_EMBED_MODEL_ID: EmbedModelId = 'text-embedding-3-large'

export type VectorIndexStatus =
  | 'delete_failed'
  | 'deleting'
  | 'error'
  | 'indexing'
  | 'ready'
  | 'stale'

// 'skipped' is TERMINAL: the document carried no extractable text, so it can
// never embed — distinct from 'pending' (queued, will embed) so the UI stops
// prompting a futile re-index and the index reads 'ready' once nothing is
// genuinely pending.
export type VectorIndexMemberState = 'pending' | 'embedded' | 'skipped'

/** A document referenced by a vector index (n:m). The asset stays in its
 * collection; only `state` is persisted lifecycle data — chunk/vector counts
 * are derived, never stored. */
export type VectorIndexMemberRecord = {
  fileId: string
  state: VectorIndexMemberState
  /** The backend knowledge-document id this member was ingested as, once known.
   * Lets "remove from index" delete the exact document from the searchable
   * collection (no full rebuild). Members built before this was tracked must
   * be reconciled by stable source id; a server-backed member is never removed
   * locally while this identity is unresolved. */
  serverDocumentId?: string
}

/** Outcome of one finished reindex run, shown in the inline history. */
export type VectorIndexRunResult = 'cancelled' | 'error' | 'ok'

/** One past reindex run (durable; serialized with the index, capped at
 * {@link VECTOR_INDEX_HISTORY_LIMIT}). */
export type VectorIndexRunHistoryEntry = {
  documents: number
  durationMs: number
  error?: string | null
  finishedAt: string
  result: VectorIndexRunResult
  startedAt: string
}

/** Max retained history entries per index (newest first). */
export const VECTOR_INDEX_HISTORY_LIMIT = 10

/** Server-backed progress for one document inside a multi-document index run.
 * The document job is the authority for phase and batch counters; the client
 * only contributes the local queue state before that job is submitted. */
export type IndexingMemberLive = {
  currentBatch?: number
  /** The member's durable revision job; a paused one is resumed by it. */
  jobId?: string
  /** While the member's job is paused: what it waits for (the server's error
   * type) and the server's plan to resume it on its own. */
  errorType?: string | null
  autoResume?: AutoResumePlan | null
  phase?: string
  /** 1-based server queue position when the document job has been accepted
   * but not claimed. Client-side waiting has no position. */
  queuePosition?: number | null
  status:
    | 'queued'
    | 'running'
    | 'cancelling'
    | 'paused_dependency'
    | 'paused_validation'
  totalBatches?: number
}

/** Ephemeral live state of a running reindex — high-frequency progress
 * kept OUT of the serialized project (never marks the project dirty). */
export type IndexingJobLive = {
  /** Truthful server execution state. Paused jobs retain their checkpoint and
   * active generation; they are not treated as failed or completed. */
  status?:
    | 'queued'
    | 'running'
    | 'cancelling'
    | 'paused_dependency'
    | 'paused_validation'
  completedDocuments: number
  currentBatch?: number
  currentDocumentTitle?: string
  /** While paused: what the run waits for and its soonest automatic resume. */
  pauseErrorType?: string | null
  autoResume?: AutoResumePlan | null
  phase?: string
  totalBatches?: number
  /** The asset ids this run actually PROCESSES (its working set): the new
   * (pending) members for an incremental add, every member for a rebuild /
   * durable re-embed. A file row reads "läuft" only while its id is in here and
   * not yet confirmed — so indexing one file never makes the already-embedded
   * rows read "läuft" (they are outside the run). */
  runningFileIds: string[]
  /** Client-build live per-file progress (the durable server-job path leaves
   * these absent): asset ids the server has CONFIRMED embedded / skipped so
   * far this run, so each file row flips to its real outcome as it lands.
   * Ephemeral — the persisted member states take over on completion. */
  embeddedFileIds?: string[]
  skippedFileIds?: string[]
  /** Per-document queue and execution facts. This is intentionally ephemeral:
   * durable truth remains in each server indexing job and its event stream. */
  memberProgress?: Record<string, IndexingMemberLive>
  jobId: string
  /** 0..100 whole percent, derived from completed/total. */
  percent: number
  /** 1-based FIFO slot while the job waits for a free slot; `null`/absent
   * once it is running. Drives the "In Warteschlange" indicator, matching
   * the research-run queue display. */
  queuePosition?: number | null
  /** How the run is driven, and therefore how it cancels:
   * `demo` = local simulator; `build` = client-orchestrated first build
   * (no cancellable server job — cancels locally); `server` = durable
   * server job streamed over SSE (cancels server-side via the job id).
   * Cancellability is read off this fact, never parsed from the job id. */
  source: 'demo' | 'build' | 'server'
  startedAt: string
  totalDocuments: number
}

export type VectorIndexRecord = {
  createdAt: string
  dims: number
  handle: string
  /** Past reindex runs, newest first (capped). Absent until the first run. */
  history?: VectorIndexRunHistoryEntry[]
  id: string
  /** Visible failure message of the last server reindex attempt;
   * cleared when a new run starts (No-Silent-Fallbacks: a failed
   * embedding run must never look like a stale index). */
  lastError?: string | null
  members: VectorIndexMemberRecord[]
  model: EmbedModelId
  /** Backend knowledge-collection id once the index was embedded on a
   * connected server; null/absent for simulated (demo/offline) runs. */
  serverCollectionId?: string | null
  /** Embedding model the server collection was BUILT with. Lets a reindex
   * tell "documents added" (same model -> incremental ingest of the new
   * members) from "model changed" (different -> full rebuild with a fresh
   * dimension). Absent on indexes built before this field existed -> read as
   * a mismatch, so the next reindex heals via a full rebuild. */
  serverCollectionModel?: string | null
  status: VectorIndexStatus
  title: string
  updatedAt: string
}

export type FileAssetAttachmentRecord = {
  attachedAt: string
  contentMarkdown: string
  fileId: string
  kind: 'file-asset'
  label: string
  pageCount: number | null
  sizeBytes: number
  title: string
}

export type FileGroupAttachmentRecord = {
  attachedAt: string
  contentMarkdown: string
  fileId: string
  groupId: string
  groupLabel: string
  kind: 'file-group'
  label: string
  pageCount: number | null
  sizeBytes: number
  title: string
}

export type ChatMessageAttachmentRecord =
  | ChatRuleAttachmentRecord
  | FileAssetAttachmentRecord
  | FileGroupAttachmentRecord
  | ResearchReportAttachmentRecord

export type ChatContextReferenceRecord =
  | { kind: 'chat-rule'; ruleId: string }
  | { kind: 'research-report'; runId: string }
  | { fileId: string; kind: 'file-asset' }
  | { groupId: string; kind: 'file-group' }

export type ChatMessageModelResolutionRecord = {
  effort: string
  effortSource: string
  model: string
  modelSource: string
  requestedTier: string
  tier: string
}

export type ChatMessageRequestContextRecord = {
  knowledgeCollectionIds?: string[]
}

export type ChatChainStepStatus = 'ok' | 'error' | 'stopped'

export type ChatChainStepRecord = {
  label: string
  output: string
  status: ChatChainStepStatus
}

export type ChatMessageRecord = {
  attachments?: ChatMessageAttachmentRecord[]
  chainTrace?: ChatChainStepRecord[]
  contentMarkdown: string
  createdAt: string
  id: string
  modelResolution?: ChatMessageModelResolutionRecord
  requestContext?: ChatMessageRequestContextRecord
  role: ChatRole
}

/** A chat thread's server deletion is in flight, or it failed and waits for
 * an explicit retry. Deliberately NOT `SessionDeletionState`: a thread
 * delete is one owner-scoped row cascade, not a durable deletion operation,
 * so it has no operation id and no stage to report. */
export type ChatThreadDeletionState = {
  error: string | null
  status: 'deleting' | 'delete_failed'
}

export type ChatThreadRecord = import('@/features/sessionTitle/model').SessionTitleFields & {
  createdAt: string
  /** Present only while a server deletion is pending or has failed. The row
   * stays visible and inert until the server confirms; nothing local removes
   * it earlier, so a failed delete can never look like a completed one. */
  deletion?: ChatThreadDeletionState
  id: string
  messages: ChatMessageRecord[]
  /** The model picked inside THIS thread; absent = nothing picked, the
   * account preference seeds the composer. Persisted on the thread row
   * (server) and in the thread file (project export) — it is the object's
   * own property, not a second global store. */
  modelSelection?: AgentSessionModelSelection
  preview: string
  source: 'api' | 'imported' | 'mock'
  title: string
  updatedAt: string
}

// ---------------------------------------------------------------------------
// Knowledge workspace ("Wissen") — Q&A thread over knowledge collections.
// Session-scoped state: it lives in ProjectState so it survives view
// switches, but it is deliberately NOT written to project files (the
// thread references short-lived server runs).
// ---------------------------------------------------------------------------

/** Step kinds shown on the live knowledge run card, in pipeline order. */
export type KnowledgeStepKind =
  | 'queued'
  | 'context'
  | 'profile'
  | 'decompose'
  | 'vocabulary'
  | 'retrieval'
  | 'evidence'
  | 'gate'
  | 'gate-exhausted'
  | 'answer'
  | 'answer-retry'
  | 'grounding'

/** Numeric/string facts captured from one knowledge SSE event. The
 * i18n step-line builder turns these into German/English lines, so the
 * record itself stays language-free. */
export type KnowledgeStepFacts = {
  autoSelected?: boolean
  candidateCount?: number
  /** Total documents in the searched collection scope (all are eligible) —
   * confirms coverage in the retrieval step line. */
  collectionDocumentCount?: number
  contextMarker?: string
  /** Parse/validation marker returned by the evidence gate. A fallback marker
   * means the gate could not be evaluated and must remain visible even though
   * the answer pipeline continues with the retrieved evidence. */
  gateMarker?: string
  degradedStages?: string[]
  dropped?: number
  kept?: number
  profile?: string
  quotesTotal?: number
  quotesVerified?: number
  /** Unverified-quote count that triggered the single visible answer
   * regeneration (answer-retry step). */
  quotesUnverified?: number
  /** Backend parse/verification marker. A fallback marker is a visible
   * degradation even when no quote rows could be produced. */
  groundingMarker?: string
  /** Technical retrieval shortfalls retained from the event stream. */
  retrievalDegradations?: KnowledgeRetrievalDegradation[]
  /** Source-integrity exclusions retained through the shared warning shape. */
  retrievalWarnings?: KnowledgeSearchWarning[]
  rewritten?: boolean
  round?: number
  roundsTotal?: number
  subQueryCount?: number
  sufficient?: boolean
  topK?: number
  finalK?: number
  finalKOverridden?: boolean
}

export type KnowledgeRunStepRecord = {
  id: string
  kind: KnowledgeStepKind
  status: 'running' | 'done'
  facts: KnowledgeStepFacts
}

/** Resolved run-plan facts from `inqtrix.knowledge.profile.resolved`;
 * needed to number gate rounds and to know which steps will appear. */
export type KnowledgeRunPlanRecord = {
  autoReason?: string | null
  autoSelected: boolean
  decompose: boolean
  degradedStages: string[]
  gateRounds: number
  grounding: boolean
  profile: string
  requestedProfile?: string | null
  vocabularyBridge: boolean
}

export type KnowledgeRunProgressRecord = {
  plan?: KnowledgeRunPlanRecord
  steps: KnowledgeRunStepRecord[]
}

export type KnowledgeQuoteRecord = {
  label: string
  text: string
  verified: boolean
}

export type KnowledgeReferenceRecord = {
  /** Citation label exactly as used in the answer text, e.g. `K1`. */
  label: string
  url: string
  tier: string
  title?: string
  /** The cited document id — the explicit backend field when present
   * (reliable open), else parsed from the citation URL; null only when
   * neither is available. */
  documentId?: string | null
  chunkIndex?: number | null
  /** The exact retrieved chunk text the answer was grounded in — shown as the
   * highlighted "Beleg" passage when the citation is opened. */
  excerpt?: string | null
  /** The chunk's original source text (sans contextualization prefix), used to
   * locate/verify the quoted span. */
  sourceText?: string | null
  /** Best-effort 1-based source page of the cited chunk (PDF sources only);
   * null when unmapped. Enables a page-level "open PDF at page N" jump. */
  pageNumber?: number | null
}

export type KnowledgeAnswerRecord = {
  answerMarkdown: string
  /** True for the honest no-evidence answer — rendered in a quiet style. */
  refusal: boolean
  references: KnowledgeReferenceRecord[]
  quotes: KnowledgeQuoteRecord[]
  profileId?: string | null
  autoSelected?: boolean
  degradedStages: string[]
  /** Technical retrieval boundaries that affected this answer. */
  retrievalDegradations: KnowledgeRetrievalDegradation[]
  /** Source-integrity exclusions that affected this answer. */
  retrievalWarnings?: KnowledgeSearchWarning[]
  gate?: { sufficient: boolean; roundsUsed: number; maxRounds: number } | null
  grounding?: { total: number; verified: number; degraded: boolean } | null
  candidateCount?: number | null
  evidenceUsed?: number | null
}

export type KnowledgeItemStatus = 'running' | 'completed' | 'failed' | 'cancelled'

export type KnowledgeThreadItemRecord = {
  answer?: KnowledgeAnswerRecord
  /** Local selection keys for the collections used by this ask. Stored so an
   * in-place rerun can address the same RAG scope even after the composer moved
   * on. Older items may omit it and fall back to title matching. */
  collectionIds?: string[]
  collectionTitles: string[]
  /** When the answer arrived. `createdAt` remains the user question time. */
  completedAt?: string
  createdAt: string
  error?: string
  id: string
  progress: KnowledgeRunProgressRecord
  question: string
  requestedProfile: string | null
  /** Server run id once the native run was accepted; demo items carry a
   * synthetic id so the same event pipeline addresses them. */
  runId: string | null
  sessionId: string
  status: KnowledgeItemStatus
  /** Per-run retrieval breadth used by the ask. Null means server default. */
  topK?: number | null
  /** Per-run surfaced-evidence override (`final_k`). Null = profile factor. */
  finalK?: number | null
}

export type KnowledgeSessionRecord = {
  createdAt: string
  id: string
  /** Local-only marker for the untouched startup session; never sent over the wire. */
  isBootstrapPlaceholder?: boolean
  title: string
  updatedAt: string
  /** Durable server-owned deletion lifecycle. */
  deletion?: import('./sessionDeletion').SessionDeletionState
}

export type KnowledgeSessionGroupRecord = {
  createdAt: string
  id: string
  title: string
  updatedAt: string
}

export type ProjectState = {
  chatRuleOrder: string[]
  chatRules: Record<string, ChatRuleRecord>
  chatThreadGroupMemberships: Record<string, string | null>
  chatThreadGroupOrder: string[]
  chatThreadGroups: Record<string, ChatThreadGroupRecord>
  chatThreadOrder: string[]
  chatThreads: Record<string, ChatThreadRecord>
  connection: ProjectConnection
  dirty: boolean
  /** Ephemeral per-user comment writes. Project serialization intentionally
   * omits this map; a sync lifecycle reset starts from an empty outbox. */
  editorCommentOutbox?: Record<string, EditorCommentOutboxEntry>
  editorComments: Record<string, EditorCommentThreadRecord>
  editorDocumentOrder: string[]
  editorDocuments: Record<string, EditorDocumentRecord>
  editorFolderOrder: string[]
  editorFolders: Record<string, EditorFolderRecord>
  editorSuggestionGroups: Record<string, EditorSuggestionGroupRecord>
  editorSuggestions: Record<string, EditorSuggestionRecord>
  editorUi: EditorUiState
  fileAssetOrder: string[]
  fileAssets: Record<string, FileAssetRecord>
  fileGroupOrder: string[]
  fileGroups: Record<string, FileGroupRecord>
  fileLibrarySectionOrder: string[]
  fileLibrarySections: Record<string, FileLibrarySectionRecord>
  /** Live reindex progress per vector-index id. Ephemeral: never
   * serialized, never marks the project dirty (high-frequency updates). */
  indexingJobs: Record<string, IndexingJobLive>
  knowledgeItemOrder: string[]
  knowledgeItems: Record<string, KnowledgeThreadItemRecord>
  knowledgeSessionGroupMemberships: Record<string, string | null>
  knowledgeSessionGroupOrder: string[]
  knowledgeSessionGroups: Record<string, KnowledgeSessionGroupRecord>
  knowledgeSessionOrder: string[]
  knowledgeSessions: Record<string, KnowledgeSessionRecord>
  selectedKnowledgeSessionId: string | null
  /** Ephemeral terminal receipts that reject stale session hydration. */
  knowledgeSessionDeletionReceipts?: Record<string, string>
  /** Agent Desk records. Session-scoped like the knowledge thread: runs
   * reference short-lived server rows, so none of this is part of
   * project files — server hydration rebuilds it. */
  agentRuns: Record<string, AgentRunRecord>
  /** Anchor-independent artifact index per session: meta rows from
   * GET /v1/agent-sessions/{id}/artifacts, carrying each artifact's
   * CURRENT run anchor. Server-derived like agentRuns — never part of
   * project files. */
  agentSessionArtifacts: Record<string, AgentSessionArtifactIndex>
  agentSessionGroupOrder: string[]
  agentSessionGroups: Record<string, AgentSessionGroupRecord>
  agentSessionOrder: string[]
  agentSessions: Record<string, AgentSessionRecord>
  selectedAgentSessionId: string | null
  /** Ephemeral terminal receipts that reject late run/list responses after a
   * server-confirmed aggregate deletion. Never serialized with the project. */
  agentSessionDeletionReceipts?: Record<
    string,
    { operationId: string; runIds: string[] }
  >
  /** Polymorphic canvas state (base view + overlay stack). Ephemeral:
   * never serialized, never marks the project dirty. */
  agentCanvas: CanvasState
  /** Per-run plan edit drafts — ONE draft shared by the timeline card and
   * the canvas plan view. Ephemeral like the canvas. */
  agentPlanDrafts: Record<string, AgentPlanDraft>
  localRunCounter: number
  preferences: ProjectPreferences
  project: ProjectMetadata
  researchRunOrder: string[]
  researchRuns: Record<string, ResearchRunRecord>
  /** Opt-in to the server-persistence tier. `false` keeps the
   * project local-first; the explicit "move to server" import sets it
   * `true`, after which a server with the `project_persistence`
   * capability hydrates and autosaves the project (chat first; editor
   * and assets follow). Persisted in the project manifest so a re-opened
   * server project re-hydrates. Never forces a non-durable server. */
  serverSyncEnabled: boolean
  /** Monotonic in-session counter, bumped every time the whole project state
   * is replaced (project load, demo toggle). It is the identity signal the
   * project-scoped server-sync hooks re-arm on: a switch to a DIFFERENT project
   * that is also server-synced keeps ``serverSyncEnabled`` (and possibly the
   * ``workspaceId``) unchanged, so the boolean alone cannot tell the hooks the
   * underlying project changed -- they would keep the prior project's ``synced``
   * fingerprint map and delete its server rows on the next autosave. Bumping
   * this on every wholesale replace forces each project to re-hydrate from its
   * OWN server state. Deliberately EPHEMERAL: never serialized to the manifest
   * (a restored counter would defeat the purpose), so it resets to 0 on reload
   * and is overwritten by the reducer on hydrate. */
  projectEpoch: number
  ui: ProjectUiState
  vectorIndexOrder: string[]
  vectorIndexes: Record<string, VectorIndexRecord>
  workspaceId: string
}

export type ProjectWriteResult = {
  connection: ProjectConnection
  savedAt: string
}

export function fromRunSummary(
  summary: ResearchRunSummary,
  fallbackStack: string,
): ResearchRunRecord {
  const now = new Date().toISOString()
  const submittedAt = toIsoString(summary.created_at) ?? now
  const startedAt = toIsoString(summary.started_at)
  const finishedAt = toIsoString(summary.finished_at)
  const snapshot = summary.snapshot
  const maxRounds = snapshot.max_rounds ?? asFiniteNumber(summary.agent_overrides.max_rounds)
  const currentRounds = snapshot.active_round ?? snapshot.completed_rounds ?? 0
  const title = summary.question.trim() || summary.run_id

  return {
    access: summary.access,
    agentOverrides: summary.agent_overrides,
    cancelRequested: summary.cancel_requested === true ? true : undefined,
    createdAt: submittedAt,
    kind: summary.kind,
    parentRunId: summary.parent_run_id,
    sessionId: summary.session_id,
    durationSeconds: terminalStatus(summary.status)
      ? summary.elapsed_seconds ?? undefined
      : undefined,
    error: summary.error?.message,
    events: [],
    finishedAt: finishedAt ?? undefined,
    metrics: {
      claims: snapshot.consolidated_claim_count ?? 0,
      queries: snapshot.total_queries ?? 0,
      rounds: maxRounds ? `${currentRounds} / ${maxRounds}` : String(currentRounds),
      sources: snapshot.total_sources ?? snapshot.total_citations ?? 0,
    },
    mode: summary.mode,
    phaseState: {
      activePhase: nodeToPhase(snapshot.current_node),
      completedPhases: completedPhasesFromNode(snapshot.current_node),
    },
    queuePosition: summary.queue_position,
    runId: summary.run_id,
    snapshot,
    source: 'api',
    stack: summary.stack || fallbackStack,
    startedAt: startedAt ?? undefined,
    status: normalizeRunStatus(summary.status),
    submittedAt,
    summary: {
      queueNote: queueNoteFromSummary(summary),
      score: confidenceScore(snapshot.confidence),
      title,
    },
  }
}

export function mergeRunSummary(
  current: ResearchRunRecord | undefined,
  summary: ResearchRunSummary,
  fallbackStack: string,
): ResearchRunRecord {
  const next = fromRunSummary(summary, fallbackStack)
  if (!current) return next

  return {
    ...next,
    events: current.events,
    // The API run summary carries no mention-availability flag, so preserve the
    // local choice; otherwise a run-list refresh would silently re-enable a
    // report the user hid from the @-autocomplete (frozen-rebuild field loss).
    includeInAutocomplete: current.includeInAutocomplete ?? next.includeInAutocomplete,
    result: current.result ?? next.result,
    summary: {
      ...next.summary,
      score: next.summary.score ?? current.summary.score,
    },
  }
}

/**
 * Ring cap on a run's stored event timeline. The whole (filtered) array is
 * rendered in the report log, so this bounds the DOM for a pathological run;
 * generous enough that no real run (a 20-round DEEP run is ~400 events) is
 * ever trimmed, so phase-visit counting and the cancel marker are unaffected.
 */
export const RUN_EVENT_LOG_CAP = 500

function clearTailActive(
  events: ResearchRunEventRecord[],
): ResearchRunEventRecord[] {
  const lastIndex = events.length - 1
  // Invariant (maintained by this module): AT MOST the tail is active, so
  // clearing the tail clears every active flag. When it is already inactive
  // the array is returned untouched -- the head event OBJECTS keep their
  // identity, so memoised timeline rows do not re-render.
  if (lastIndex < 0 || !events[lastIndex].active) return events
  const next = events.slice()
  next[lastIndex] = { ...next[lastIndex], active: false }
  return next
}

function capHead(
  events: ResearchRunEventRecord[],
  cap: number,
): ResearchRunEventRecord[] {
  // Trim the OLDEST when over cap; the active event is always the tail, so
  // dropping from the head never removes it.
  return events.length > cap ? events.slice(events.length - cap) : events
}

/**
 * Append one event record to a run's timeline, preserving the invariant that
 * at most the tail event is `active`.
 *
 * Replaces the previous O(n) `map(active:false)` + O(n^2) `filter(findIndex)`
 * dedup that ran on every SSE frame and stuttered late in long DEEP runs.
 * Here only the previous tail's active flag is cleared (head objects are
 * shared, so memoised rows stay put); dedup keeps the FIRST occurrence by id
 * -- matching the old semantics for BOTH reconnect replays (`{run}-{seq}` ids
 * replayed on SSE resume) and node-stable model-resolution ids
 * (`{run}-model-{node}` refired each round). A model re-fire can sit far from
 * the tail, so the membership test is the one unavoidable O(n) scan; the
 * quadratic dedup -- the actual stutter -- is gone.
 */
export function appendRunEventRecord(
  events: ResearchRunEventRecord[],
  next: ResearchRunEventRecord,
  options?: { cap?: number },
): ResearchRunEventRecord[] {
  const cap = options?.cap ?? RUN_EVENT_LOG_CAP
  const base = clearTailActive(events)
  // Duplicate id -> keep the existing row at its stable position, drop next.
  // The array is already dedup-free (this helper maintains it), so a single
  // membership scan suffices.
  if (base.some((item) => item.id === next.id)) return capHead(base, cap)
  return capHead([...base, next], cap)
}

export function applyRunEvent(
  record: ResearchRunRecord,
  event: ResearchRunEvent,
  options?: { arrivedLive?: boolean },
): ResearchRunRecord {
  const snapshot = snapshotFromEvent(event) ?? record.snapshot
  const nextStatus = statusFromEvent(event) ?? record.status
  const eventRecord = eventRecordFromRunEvent(event, options?.arrivedLive ?? true)
  const events = eventRecord
    ? appendRunEventRecord(record.events, eventRecord)
    : record.events
  const updated = applySnapshotToRecord(record, snapshot)

  return {
    ...updated,
    error: errorFromEvent(event) ?? updated.error,
    events,
    finishedAt: terminalStatus(nextStatus) && !updated.finishedAt
      ? toIsoString(event.created_at) ?? new Date().toISOString()
      : updated.finishedAt,
    // The create summary is `queued` (started_at null); `started_at` is only
    // set server-side at claim and never re-sent on the event stream. Without
    // this, `status` flips to `running` from an event while `startedAt` stays
    // undefined, so the live runtime sticks at 00:00:00. Derive it from the
    // running-transition event's timestamp (mirrors `finishedAt` above); never
    // overwrite an authoritative summary `startedAt` a hydrated run already has.
    startedAt: nextStatus === 'running' && !updated.startedAt
      ? toIsoString(event.created_at) ?? updated.startedAt
      : updated.startedAt,
    status: nextStatus,
    summary: {
      ...updated.summary,
      queueNote: nextStatus === 'queued' ? updated.summary.queueNote : undefined,
      score: updated.summary.score,
    },
  }
}

export function attachRunResult(
  record: ResearchRunRecord,
  result: ResearchRunResult,
): ResearchRunRecord {
  const maxRounds = record.snapshot?.max_rounds
    ?? asFiniteNumber(record.agentOverrides.max_rounds)
    ?? result.metrics.rounds

  return {
    ...record,
    durationSeconds: result.metrics.elapsed_seconds,
    finishedAt: new Date().toISOString(),
    metrics: {
      // "Claims found" = total consolidated claims, summed across the status
      // buckets so a completed card matches the live snapshot's
      // consolidated_claim_count semantics.
      claims: Object.values(result.metrics.claims.status_counts).reduce((sum, count) => sum + count, 0),
      queries: result.metrics.total_queries,
      rounds: `${result.metrics.rounds} / ${maxRounds}`,
      sources: result.metrics.total_citations,
    },
    phaseState: {
      activePhase: 'answer',
      completedPhases: ['analysis', 'planning', 'search', 'evaluation', 'answer'],
    },
    result: {
      markdown: result.answer,
      metrics: result.metrics,
      references: result.references ?? [],
      topClaims: result.top_claims,
      topSources: result.top_sources,
      usage: result.usage,
      ...(result.web_search_ledger ? { webSearchLedger: result.web_search_ledger } : {}),
    },
    status: 'completed',
    summary: {
      ...record.summary,
      queueNote: undefined,
      score: `${result.metrics.confidence.toFixed(1)} / 10`,
    },
  }
}

function normalizeRunStatus(status: ResearchRunSummary['status']): JobStatus {
  return status
}

function toIsoString(timestamp: number | null) {
  if (timestamp === null) return null
  return new Date(timestamp * 1000).toISOString()
}

function nodeToPhase(node?: string): JobPhase {
  return phaseFromNode(node) ?? 'analysis'
}

function phaseFromNode(node?: string): JobPhase | undefined {
  if (node === 'classify') return 'analysis'
  if (node === 'plan') return 'planning'
  if (node === 'search') return 'search'
  if (node === 'evaluate') return 'evaluation'
  if (node === 'answer' || node === 'direct_llm') return 'answer'
  return undefined
}

function completedPhasesFromNode(node?: string): JobPhase[] {
  if (node === 'plan') return ['analysis']
  if (node === 'search') return ['analysis', 'planning']
  if (node === 'evaluate') return ['analysis', 'planning', 'search']
  if (node === 'answer') return ['analysis', 'planning', 'search', 'evaluation']
  if (node === 'direct_llm') return ['analysis']
  return []
}

function applySnapshotToRecord(
  record: ResearchRunRecord,
  snapshot: ResearchRunSnapshot | undefined,
): ResearchRunRecord {
  if (!snapshot) return record

  const maxRounds = snapshot.max_rounds
    ?? record.snapshot?.max_rounds
    ?? asFiniteNumber(record.agentOverrides.max_rounds)
  const currentRounds = snapshot.active_round
    ?? snapshot.completed_rounds
    ?? record.snapshot?.active_round
    ?? record.snapshot?.completed_rounds
    ?? 0
  const confidence = snapshot.confidence ?? record.snapshot?.confidence

  return {
    ...record,
    metrics: {
      claims: snapshot.consolidated_claim_count ?? record.metrics.claims,
      queries: snapshot.total_queries ?? record.metrics.queries,
      rounds: maxRounds ? `${currentRounds} / ${maxRounds}` : String(currentRounds),
      sources: snapshot.total_sources ?? snapshot.total_citations ?? record.metrics.sources,
    },
    phaseState: {
      activePhase: nodeToPhase(snapshot.current_node),
      completedPhases: completedPhasesFromNode(snapshot.current_node),
    },
    snapshot,
    summary: {
      ...record.summary,
      queueNote: record.status === 'queued' ? record.summary.queueNote : undefined,
      score: confidenceScore(confidence) ?? record.summary.score,
    },
  }
}

function snapshotFromEvent(event: ResearchRunEvent): ResearchRunSnapshot | undefined {
  const snapshot = event.data.snapshot
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) return undefined
  return snapshot as ResearchRunSnapshot
}

function statusFromEvent(event: ResearchRunEvent): JobStatus | undefined {
  if (event.type === 'inqtrix.run.queued') return 'queued'
  if (event.type === 'inqtrix.run.started' || event.type === 'inqtrix.run.resumed') return 'running'
  if (event.type === 'inqtrix.run.completed') return 'completed'
  if (event.type === 'inqtrix.run.failed') return 'failed'
  if (event.type === 'inqtrix.run.cancelled') return 'cancelled'

  // Only the run lifecycle owns ResearchRunRecord.status. Node, query, and
  // tool events may use the same status vocabulary for their own operation;
  // treating those payloads as run state can create a false terminal card.
  if (event.type !== 'inqtrix.run.snapshot') return undefined

  const status = event.data.status
  if (
    status === 'queued'
    || status === 'running'
    || status === 'completed'
    || status === 'failed'
    || status === 'cancelled'
    || status === 'expired'
  ) {
    return status
  }
  return undefined
}

function terminalStatus(status: JobStatus) {
  return status === 'completed'
    || status === 'failed'
    || status === 'cancelled'
    || status === 'expired'
}

function eventRecordFromRunEvent(
  event: ResearchRunEvent,
  arrivedLive: boolean,
): ResearchRunEventRecord | null {
  if (!isVisibleProtocolEvent(event)) return null
  const phase = phaseFromRunEvent(event)

  return {
    active: !terminalStatus(statusFromEvent(event) ?? 'running') || undefined,
    ...(arrivedLive ? { arrivedLive } : {}),
    createdAt: toIsoString(event.created_at) ?? new Date().toISOString(),
    // Model-resolution events fire once per node per round but the model is
    // stable per node, so give them a node-stable id; the dedup below then
    // keeps one row per node instead of one per round (no timeline spam).
    id: event.type === 'inqtrix.node.model_resolution'
      ? `${event.run_id}-model-${asNonEmptyString(event.data.node) ?? event.sequence}`
      : `${event.run_id}-${event.sequence}`,
    kind: eventKindFromRunEvent(event),
    ...(phase ? { phase } : {}),
    severity: eventSeverityFromRunEvent(event),
    title: titleFromRunEvent(event),
  }
}

function phaseFromRunEvent(event: ResearchRunEvent): JobPhase | undefined {
  const explicitPhase = phaseFromUnknown(event.data.phase)
  if (explicitPhase) return explicitPhase

  const snapshotPhase = phaseFromNode(snapshotFromEvent(event)?.current_node)
  if (snapshotPhase) return snapshotPhase

  return phaseFromNode(asNonEmptyString(event.data.node))
}

function phaseFromUnknown(value: unknown): JobPhase | undefined {
  if (
    value === 'analysis'
    || value === 'planning'
    || value === 'search'
    || value === 'evaluation'
    || value === 'answer'
  ) {
    return value
  }
  return phaseFromNode(asNonEmptyString(value))
}

function isVisibleProtocolEvent(event: ResearchRunEvent) {
  if (event.type === 'inqtrix.progress.message') {
    const message = asNonEmptyString(event.data.message)
    return Boolean(message && message !== 'done' && !message.startsWith('chat_only.'))
  }
  if (
    event.type === 'inqtrix.run.snapshot'
    || event.type === 'inqtrix.run.started'
    || event.type === 'inqtrix.node.started'
    || event.type === 'inqtrix.node.finished'
    || event.type === 'inqtrix.output_text.delta'
  ) {
    return false
  }
  return event.type === 'inqtrix.run.completed'
    || event.type === 'inqtrix.run.cancel_requested'
    || event.type === 'inqtrix.run.cancelled'
    || event.type === 'inqtrix.run.failed'
    || event.type === 'inqtrix.node.failed'
    || event.type === 'inqtrix.node.model_resolution'
}

function eventKindFromRunEvent(event: ResearchRunEvent): ResearchRunEventKind {
  return event.type === 'inqtrix.progress.message' ? 'progress' : 'system'
}

function eventSeverityFromRunEvent(event: ResearchRunEvent): ResearchRunEventSeverity {
  const severity = event.data.severity
  if (severity === 'warning' || severity === 'error' || severity === 'success') {
    return severity
  }
  if (event.type === 'inqtrix.run.completed') return 'success'
  if (event.type === 'inqtrix.run.failed' || event.type === 'inqtrix.node.failed') return 'error'
  if (event.type === 'inqtrix.run.cancel_requested' || event.type === 'inqtrix.run.cancelled') return 'warning'

  const message = asNonEmptyString(event.data.message) ?? titleFromRunEvent(event)
  return warningLikeMessage(message) ? 'warning' : 'info'
}

function titleFromRunEvent(event: ResearchRunEvent) {
  const message = asNonEmptyString(event.data.message)
  if (message) return message

  const node = asNonEmptyString(event.data.node)
  if (event.type === 'inqtrix.node.model_resolution' && node) {
    const model = asNonEmptyString(event.data.model) || '(default)'
    const source = asNonEmptyString(event.data.model_source) ?? ''
    const effort = asNonEmptyString(event.data.effort) ?? ''
    const sourceLabel = source.startsWith('tier:')
      ? `${source.slice(5)} tier`
      : source === 'reasoning_model_default'
        ? 'reasoning_model default'
        : source === 'per_node_override'
          ? 'per-node override'
          : source
    const effortSuffix = effort && effort !== 'none' ? `, effort ${effort}` : ''
    return `${node}: ${model} (${sourceLabel}${effortSuffix})`
  }
  if (event.type === 'inqtrix.node.failed' && node) return `Failed ${node}`
  if (event.type === 'inqtrix.run.queued') return 'Queued'
  if (event.type === 'inqtrix.run.completed') return 'Run completed'
  if (event.type === 'inqtrix.run.cancel_requested') return 'Cancellation requested'
  if (event.type === 'inqtrix.run.cancelled') return 'Run cancelled'
  if (event.type === 'inqtrix.run.failed') return errorFromEvent(event) ?? 'Run failed'
  return event.type.replace(/^inqtrix\./, '').replace(/\./g, ' ')
}

function warningLikeMessage(message: string) {
  return /\b(ALGO-FAIL|Warnung|Warning|failed|fehlgeschlagen|violated|verletzt|unvollstaendig|unvollständig|Fallback|fallback|Kontextfenster|context window)\b/i.test(message)
}

function errorFromEvent(event: ResearchRunEvent) {
  const error = event.data.error
  if (!error || typeof error !== 'object' || Array.isArray(error)) return undefined
  return asNonEmptyString((error as { message?: unknown }).message)
}

function queueNoteFromSummary(summary: ResearchRunSummary) {
  if (summary.error?.message) return summary.error.message
  if (summary.queue_position) return `Queue position ${summary.queue_position}`
  return undefined
}

function confidenceScore(confidence: number | undefined) {
  return confidence ? `${confidence.toFixed(1)} / 10` : undefined
}
