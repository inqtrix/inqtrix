import { DEEP_RESEARCH_MAX_ROUNDS } from '@/features/researchRuns/types'
import type {
  ChatModelTier,
  CreateResearchRunRequest,
  ResearchRunEvent,
  ResearchRunResult,
  ResearchRunSummary,
} from '@/features/researchRuns/types'
import { createEmptyProjectState, createSeedProjectState } from '@/features/project/seedProject'
import { normalizeChatRule } from '@/features/project/chatRules'
import { adoptVisibleExplorerOrder } from '@/features/project/explorerSort'
import type { ExplorerSortDesk, ExplorerSortMode } from '@/features/project/explorerSort'
import type { AutoResumePlan } from '@/features/fileLibrary/indexingTypes'
import { soonestPlan } from '@/features/fileLibrary/pauseNotice'
import { renderChatRuleAttachmentContent } from '@/features/project/chatRuleRendering'
import type {
  ChatChainStepRecord,
  ChatContextReferenceRecord,
  ChatMessageRecord,
  ChatMessageAttachmentRecord,
  ChatMessageModelResolutionRecord,
  ChatMessageRequestContextRecord,
  ChatRuleRecord,
  ChatThreadDeletionState,
  ChatThreadGroupRecord,
  ChatThreadRecord,
  EditorCommentOutboxEntry,
  EditorCommentStatus,
  EditorCommentThreadRecord,
  EditorDocumentRecord,
  EditorDocumentRecoveryReason,
  EditorFolderRecord,
  EditorPanelTab,
  EditorPrivateSuggestionDraftRecord,
  EditorSuggestionGroupRecord,
  EditorSuggestionOrigin,
  EditorSuggestionRecord,
  EditorSuggestionRevisionSource,
  EditorViewMode,
  EmbedModelId,
  FileAssetRecord,
  FileGroupRecord,
  FileLibrarySectionRecord,
  FileParseStatus,
  IndexingJobLive,
  IndexingMemberLive,
  KnowledgeAnswerRecord,
  KnowledgeSessionGroupRecord,
  KnowledgeSessionRecord,
  KnowledgeThreadItemRecord,
  ProjectConnection,
  ProjectPreferences,
  ProjectState,
  ResearchRunRecord,
  VectorIndexMemberRecord,
  VectorIndexRecord,
  VectorIndexRunHistoryEntry,
  VectorIndexStatus,
} from '@/features/project/types'
import { DEFAULT_KNOWLEDGE_SESSION_TITLE } from '@/features/project/knowledgeSessionDefaults'
import type { SessionDeletionState } from '@/features/project/sessionDeletion'
import { sessionDeletionFromWire } from '@/features/project/sessionDeletion'
import { createProjectEntityId as createId } from '@/features/project/entityId'
import { isPristineDefaultFileSection } from '@/features/files/sections'
import { stripServerUploadFailureWarning } from '@/features/files/ingest'
import {
  clampPanelLayoutSize,
  type ProjectPanelLayoutKey,
} from '@/features/project/panelLayout'
import {
  appendRunEventRecord,
  applyRunEvent,
  attachRunResult,
  DEFAULT_EMBED_MODEL_ID,
  EMBED_MODELS,
  mergeRunSummary,
  VECTOR_INDEX_HISTORY_LIMIT,
} from '@/features/project/types'
import {
  acknowledgeAgentTaskCancellation,
  applyAgentRunEvent,
} from '@/features/agent/events'
import type { AgentPlanDraft as AgentPlanDraftState } from '@/features/agent/plan/usePlanApproval'
import {
  agentApprovalFromWire,
  sessionArtifactMetaFromWire,
  mergeAgentRunApprovals,
  mergeAgentRunClarifications,
  agentArtifactFromWire,
  agentClarificationFromWire,
  agentPatchFromWire,
  agentPlanFromWire,
  DERIVED_AGENT_SESSION_UPDATED_AT,
  isAgentRunSummary,
  mergeAgentRunSummary,
  type AgentRunRecord,
  type AgentSessionArtifactMeta,
  type AgentSessionGroupRecord,
  type AgentSessionRecord,
} from '@/features/agent/model'
import { agentSessionMetadataFromJson } from '@/features/agent/agentSessionSync'
import {
  agentModelSelectionKey,
  DEFAULT_AGENT_SOURCE_POLICY,
  type AgentKnowledgeSwitch,
  type AgentSourcePolicy,
} from '@/features/agent/executionPolicy'
import type {
  AgentApprovalWire,
  AgentArtifactDetailWire,
  AgentArtifactMetaWire,
  AgentClarificationWire,
  AgentPatchWire,
  AgentPlanWire,
  ServerAgentSession,
  ServerAgentSessionGroup,
} from '@/features/agent/types'
import {
  activateCanvasTab,
  closeCanvasTab,
  openCanvasTab,
  pinCanvasTab,
} from '@/features/canvas/tabs'
import {
  EMPTY_CANVAS_STATE,
  type CanvasOpenSource,
  type CanvasState,
  type CanvasViewDescriptor,
} from '@/features/canvas/types'
import { moveItem } from '@/features/composer/reorder'
import {
  knowledgeAnswerFromRunResult,
  knowledgeAnswerWithRunProgress,
} from '@/features/knowledge/answer'
import { applyKnowledgeRunEvent } from '@/features/knowledge/runSteps'
import type { AppView, JobFilter, ResearchJob } from './types'

export const stackOptions = [
  'anthropic_perplexity',
  'azure_web_search',
  'azure_openai_web_search',
]

export type ResearchDeskState = ProjectState

export type EditorDocumentRecoveryCapture = {
  capturedAt: string
  contentMarkdown: string
  documentId: string
}

import { initialSessionTitle, reconcileTitle, titleFromWire, titleWire, type TitleKind, type TitleWire, type TitledRecord } from '@/features/sessionTitle/model'

export type ResearchDeskAction =
  | { type: 'restoreSessionTitleDraft'; kind: TitleKind; id: string; title: string; projectEpoch: number }
  | { type: 'requestSessionTitle'; kind: TitleKind; id: string; question: string; language?: string; stack?: string | null }
  | { type: 'reconcileSessionTitle'; kind: TitleKind; id: string; wire: TitleWire; submitted?: TitledRecord; projectEpoch: number }
  | { type: 'finishSessionTitle'; kind: TitleKind; id: string; requestId: string; projectEpoch: number; title: string | null; reason?: string | null; current?: TitleWire }
  | { ref: ChatContextReferenceRecord; type: 'attachChatContextToDraft' }
  | { type: 'attachReportToChatDraft'; runId: string }
  | { type: 'attachReportToNewChat'; runId: string }
  | { type: 'setResearchRunAutocomplete'; runId: string; includeInAutocomplete: boolean }
  | { ref: ChatContextReferenceRecord; type: 'removeChatContextFromDraft' }
  | { fromIndex: number; toIndex: number; type: 'reorderChatContextInDraft' }
  | { type: 'clearChatDraftAttachment' }
  | {
    groupId?: string | null
    /** The account's preferred tier, applied to the fresh thread. Passed in
     * rather than read here so the reducer stays free of the theme layer. */
    modelTier?: ChatModelTier | null
    preview: string
    title: string
    type: 'createChatThread'
  }
  | { messageId: string; threadId: string; type: 'branchChatThreadFromMessage' }
  | { type: 'createLocalRun'; request: CreateResearchRunRequest }
  | { type: 'cancelLocalRun'; runId: string }
  | { emptyPreview: string; type: 'clearChatThread'; threadId: string }
  | { emptyPreview: string; messageIds: string[]; threadId: string; type: 'deleteChatMessages' }
  | { title: string; type: 'createChatThreadGroup' }
  | { ruleId: string; type: 'deleteChatRule' }
  | { groupId: string; type: 'deleteChatThreadGroup' }
  | { type: 'deleteChatThread'; threadId: string }
  | {
    deletion: ChatThreadDeletionState | null
    threadId: string
    type: 'setChatThreadDeletion'
  }
  | { threadId: string; type: 'togglePinnedChatThread' }
  | { desk: ExplorerSortDesk; mode: ExplorerSortMode; type: 'setExplorerSortMode' }
  | {
    desk: 'agent' | 'chat' | 'editor' | 'knowledge'
    folderIds: string[]
    itemIds: string[]
    type: 'adoptExplorerOrder'
  }
  | { type: 'deleteJob'; jobId: string }
  | { folderId?: string | null; type: 'createEditorDocument' }
  | { title: string; type: 'createEditorFolder' }
  | { documentId: string; type: 'deleteEditorDocument' }
  | { documentId: string; type: 'promoteEditorRecoveryDocument' }
  | {
      capture: EditorDocumentRecoveryCapture
      reason: EditorDocumentRecoveryReason
      /** The original document as it was when its state was captured. */
      source: Pick<EditorDocumentRecord, 'source' | 'sourceRunId' | 'title'>
      type: 'addEditorRecoveryDocument'
    }
  | { documentId: string; type: 'togglePinnedEditorDocument' }
  | { folderId: string; type: 'deleteEditorFolder' }
  | { documentId: string; type: 'openEditorDocument' }
  | { documentId: string; type: 'closeEditorDocumentTab' }
  | { documentId: string; type: 'setActiveEditorDocument' }
  | { documentId: string; title: string; type: 'renameEditorDocument' }
  | { folderId: string; title: string; type: 'renameEditorFolder' }
  | { folderId: string; targetIndex: number; type: 'moveEditorFolder' }
  | { documentId: string; folderId: string | null; targetIndex: number; type: 'moveEditorDocumentToFolder' }
  | { comment: EditorCommentThreadRecord; type: 'createEditorComment' }
  | { comment: EditorCommentThreadRecord; type: 'adoptEditorCarrierComment' }
  | { commentId: string; type: 'resolveEditorComment' }
  | { commentId: string; type: 'deleteEditorComment' }
  | { commentId: string; contentMarkdown: string; type: 'updateEditorCommentText' }
  | { commentId: string; status: EditorCommentStatus; type: 'setEditorCommentStatus' }
  | {
      acknowledgements: Array<{
        commentId: string
        operation: 'delete' | 'upsert'
        updatedAt?: string
      }>
      type: 'acknowledgeEditorCommentOutbox'
    }
  | { group: EditorSuggestionGroupRecord; suggestions: EditorSuggestionRecord[]; type: 'createEditorSuggestionGroup' }
  | {
      anchor: EditorCommentThreadRecord['anchor']
      commentId: string
      suggestionDraft: EditorPrivateSuggestionDraftRecord
      type: 'adoptEditorCommentSuggestionDraft'
    }
  | {
      collaborationPublication?: EditorSuggestionRecord['collaborationPublication']
      suggestionId: string
      type: 'acceptEditorSuggestion'
    }
  | { suggestionId: string; type: 'rejectEditorSuggestion' }
  | { suggestionId: string; type: 'markEditorSuggestionStale' }
  | {
    changeSummary?: string[]
    instruction?: string
    proposedText: string
    source: EditorSuggestionRevisionSource
    suggestionId: string
    type: 'updateEditorSuggestionProposal'
    warnings?: string[]
  }
  | { groupId: string; type: 'acceptEditorSuggestionGroup' }
  | { groupId: string; type: 'rejectEditorSuggestionGroup' }
  | { commentId: string | null; type: 'selectEditorComment' }
  | { documentId: string; contentMarkdown: string; type: 'updateEditorDocumentMarkdown' }
  | { isVisible: boolean; type: 'setEditorAssistantVisible' }
  | { isVisible: boolean; type: 'setEditorCommentPanelVisible' }
  | { isVisible: boolean; type: 'setEditorTreeVisible' }
  | { draft: string; type: 'setEditorAssistantDraft' }
  | { tab: EditorPanelTab; type: 'setEditorPanelTab' }
  | { mode: EditorViewMode; type: 'setEditorViewMode' }
  | { contentMarkdown: string; documentId: string; type: 'setEditorDiffAnchor' }
  | {
      confirmedAt: string
      contentMarkdown: string
      documentId: string
      sequence: number
      type: 'confirmEditorCollaborationProjection'
    }
  | { isVisible: boolean; type: 'setEditorDiffVisible' }
  | { runId: string; type: 'importResearchReportToEditor' }
  | { contentMarkdown: string; messageId: string; threadId: string; type: 'editChatUserMessage' }
  | { type: 'hydrateProject'; state: ProjectState }
  | {
      type: 'appendApiRunEvent'
      event: ResearchRunEvent
      /** Client time (s) the live frame arrived: the run clock's offset. */
      receivedAt?: number
    }
  | { type: 'appendApiRunEvents'; events: ResearchRunEvent[] }
  | { select?: boolean; summary: ResearchRunSummary; type: 'upsertAgentRunSummary' }
  | { summary: ResearchRunSummary; type: 'upsertInspectedAgentChild' }
  | { plan: AgentPlanWire | null; runId: string; type: 'setAgentRunPlan' }
  | { runId: string; type: 'markAgentRunPlanStale' }
  | {
    childRunId: string | null
    runId: string
    status: 'cancel_requested' | 'cancelled'
    taskId: string
    type: 'ackAgentTaskCancel'
  }
  | { approvals: AgentApprovalWire[]; runId: string; type: 'setAgentRunApprovals' }
  | { clarifications: AgentClarificationWire[]; runId: string; type: 'setAgentRunClarifications' }
  | {
    approvals?: AgentApprovalWire[]
    childRunId: string
    clarifications?: AgentClarificationWire[]
    question?: string
    runId: string
    type: 'setAgentChildGates'
  }
  | { artifacts: AgentArtifactMetaWire[]; runId: string; type: 'setAgentRunArtifacts' }
  | { artifacts: AgentArtifactMetaWire[]; sessionId: string; type: 'setAgentSessionArtifacts' }
  | { message: string; sessionId: string; type: 'markAgentSessionArtifactsError' }
  | { artifact: AgentArtifactDetailWire; runId: string; type: 'setAgentRunArtifactDetail' }
  | { patch: AgentPatchWire; runId: string; type: 'setAgentRunPatch' }
  | { childRunId?: string; message: string; runId: string; surface?: 'plan' | 'approvals' | 'clarifications' | 'artifacts' | 'answer' | 'patch' | 'child_gates'; type: 'markAgentRunError' }
  | { session: AgentSessionRecord; type: 'createAgentSession' }
  | { sessionId: string; sourcePolicy: AgentSourcePolicy; type: 'setAgentSessionSourcePolicy' }
  | { sessionId: string; knowledgeSwitch: AgentKnowledgeSwitch; type: 'setAgentSessionKnowledgeSwitch' }
  | { sessionId: string; title: string; type: 'renameAgentSession' }
  | { operationId?: string | null; sessionId: string; type: 'deleteAgentSession' }
  | { deletion: SessionDeletionState; sessionId: string; type: 'setAgentSessionDeletionState' }
  | { sessionId: string | null; type: 'selectAgentSession' }
  | { sessionId: string; type: 'togglePinnedAgentSession' }
  | { groupId: string | null; sessionId: string; targetIndex: number; type: 'moveAgentSessionToGroup' }
  | { groupId: string; targetIndex: number; type: 'moveAgentSessionGroup' }
  | { title: string; type: 'createAgentSessionGroup' }
  | { groupId: string; title: string; type: 'renameAgentSessionGroup' }
  | { groupId: string; type: 'deleteAgentSessionGroup' }
  | {
      groups: ServerAgentSessionGroup[]
      selectSessionId?: string
      sessions: ServerAgentSession[]
      type: 'upsertServerAgentSessions'
    }
  | { draft: AgentPlanDraftState | null; runId: string; type: 'setAgentPlanDraft' }
  | { descriptor: CanvasViewDescriptor; source: CanvasOpenSource; type: 'openAgentCanvasView' }
  | { key: string; type: 'activateAgentCanvasTab' }
  | { key: string; type: 'closeAgentCanvasTab' }
  | { key: string; type: 'pinAgentCanvasTab' }
  | { pinned: boolean; type: 'setAgentCanvasPinned' }
  | { type: 'toggleAgentCanvasFocus' }
  | { type: 'closeAgentCanvas' }
  | { type: 'toggleAgentSessionsVisible' }
  | {
      type: 'attachApiRunResult'
      result: ResearchRunResult
      /** The run's finish on the server (`finished_at` of the run list),
       * when the result came for an already finished run. */
      finishedAt?: string
    }
  | { type: 'markApiRunError'; message: string; runId: string }
  | { type: 'markApiRunResultError'; message: string; runId: string }
  | { type: 'markApiRunUnavailable'; runId: string }
  | { hydration?: boolean; type: 'upsertApiRunSummary'; select?: boolean; summary: ResearchRunSummary }
  | {
      sourceRunId: string
      summary: ResearchRunSummary
      type: 'adoptImportedApiRun'
    }
  | { summaries: ResearchRunSummary[]; type: 'replaceApiRunSummaries' }
  | {
    /** Append new ids to the END of the order (older load-more pages) rather
     * than prepending (the page-1 hydrate / newest). */
    append?: boolean
    memberships: Record<string, string | null>
    threads: ChatThreadRecord[]
    type: 'upsertServerChatThreads'
  }
  | { messages: ChatMessageRecord[]; threadId: string; type: 'upsertServerChatMessages' }
  | { groups: ChatThreadGroupRecord[]; type: 'upsertServerChatThreadGroups' }
  | { enabled: boolean; persistLocal?: boolean; type: 'setServerSyncEnabled' }
  | { documents: EditorDocumentRecord[]; type: 'upsertServerEditorDocuments' }
  | {
      documents: EditorDocumentRecord[]
      recoveryCaptures?: EditorDocumentRecoveryCapture[]
      /** Synchronously retired by the sync lifecycle before this reducer
       * transition so autosave cannot race the React render. */
      retiredDocumentIds?: string[]
      type: 'reconcileServerEditorDocuments'
    }
  | {
      collaboration: NonNullable<EditorDocumentRecord['collaboration']>
      documentId: string
      metadataRevision: number
      type: 'activateEditorDocumentCollaboration'
    }
  | { document: EditorDocumentRecord; type: 'setServerEditorDocumentDetail' }
  | { contentMarkdown: string; documentId: string; type: 'setServerEditorDocumentBody' }
  | { documentId: string; revision: number; type: 'adoptEditorDocumentRevision' }
  | {
      documentId: string
      metadataRevision: number
      type: 'adoptEditorDocumentMetadataRevision'
    }
  | {
      contentMarkdown: string
      documentId: string
      pushedContentMarkdown: string
      revision: number
      type: 'rebaseServerEditorDocument'
    }
  | { folders: EditorFolderRecord[]; type: 'upsertServerEditorFolders' }
  | { comments: EditorCommentThreadRecord[]; type: 'upsertServerEditorComments' }
  | {
      comments: EditorCommentThreadRecord[]
      documentId: string
      preserveCommentIds: string[]
      type: 'reconcileServerEditorComments'
    }
  | { sections: FileLibrarySectionRecord[]; type: 'upsertServerAssetSections' }
  | { sectionId: string; type: 'markFileLibrarySectionServerSynced' }
  | {
      hiddenServerIds: string[]
      serverHasTemporarySection: boolean
      serverIds: string[]
      type: 'pruneLocalBootstrapFileSections'
    }
  | { replacements: Record<string, string>; type: 'rekeyFileLibrarySectionIds' }
  | { groups: FileGroupRecord[]; type: 'upsertServerAssetGroups' }
  | { groupId: string; type: 'markFileGroupServerSynced' }
  | { assets: FileAssetRecord[]; type: 'upsertServerAssetMetadata' }
  | { assetId: string; type: 'markFileAssetServerSynced' }
  | {
      assetId: string
      extractedText: string
      preparedAt: string | null
      preparedContentHash: string | null
      preparedParserId: string | null
      preparedText: string
      type: 'setServerAssetBody'
    }
  | { assetId: string; pending: boolean; type: 'setFileAssetParsePending' }
  | { assetId: string; pending: boolean; type: 'setFileAssetUploadPending' }
  | {
      assetId: string
      error: string | null
      operationId: string | null
      serverFileId: string | null
      status: FileAssetRecord['uploadStatus']
      type: 'adoptFileAssetUploadLifecycle'
    }
  | { assetId: string; serverFileId: string; type: 'completeFileAssetUpload' }
  | { assetId: string; message: string; type: 'failFileAssetUpload' }
  | {
      fileIds: string[]
      operationId: string
      stage: string
      status: 'queued' | 'running' | 'delete_failed'
      error: string | null
      type: 'setFileAssetDeletionState'
    }
  | { fileIds: string[]; operationId: string; type: 'completeFileAssetDeletion' }
  | {
      error: string | null
      groupId: string
      operationId: string
      stage: string
      status: 'queued' | 'running' | 'delete_failed'
      type: 'setFileGroupDeletionState'
    }
  | {
      groupId: string
      operationId: string
      type: 'completeFileGroupDeletion'
    }
  | {
      error: string | null
      operationId: string
      sectionId: string
      stage: string
      status: 'queued' | 'running' | 'delete_failed'
      type: 'setFileLibrarySectionDeletionState'
    }
  | {
      operationId: string
      sectionId: string
      type: 'completeFileLibrarySectionDeletion'
    }
  | {
      assetId: string
      clearParsePending: boolean
      extractedText: string
      pageCount: number | null
      parseStatus: FileParseStatus
      parseWarning: string | null
      textTruncated: boolean
      type: 'applyFileAssetClientParse'
    }
  | { indexes: VectorIndexRecord[]; type: 'upsertServerVectorIndexes' }
  | {
    assistantMessageId: string
    contentMarkdown: string
    createdAt: string
    attachmentRefs?: ChatContextReferenceRecord[]
    modelResolution?: ChatMessageModelResolutionRecord
    requestContext?: ChatMessageRequestContextRecord
    threadId: string
    type: 'startChatExchange'
    titleLanguage?: string
    titleStack?: string | null
    userMessageId: string
  }
  | {
    assistantMessageId: string
    createdAt: string
    modelResolution?: ChatMessageModelResolutionRecord
    requestContext?: ChatMessageRequestContextRecord
    threadId: string
    type: 'startChatAssistantResponse'
    userMessageId: string
  }
  | {
    assistantMessageId: string
    createdAt: string
    modelResolution?: ChatMessageModelResolutionRecord
    requestContext?: ChatMessageRequestContextRecord
    replacedAssistantMessageId: string
    threadId: string
    type: 'startChatAssistantRetry'
  }
  | {
    assistantMessageId: string
    chainTrace?: ChatChainStepRecord[]
    contentMarkdown: string
    modelResolution?: ChatMessageModelResolutionRecord
    threadId: string
    type: 'setChatAssistantMessageContent'
  }
  | { type: 'setDemoMode'; enabled: boolean }
  | { type: 'markProjectSaved'; connection: ProjectConnection; preferences?: ProjectPreferences; savedAt: string }
  | { groupId: string; targetIndex: number; type: 'moveChatThreadGroup' }
  | { groupId: string | null; targetIndex: number; threadId: string; type: 'moveChatThreadToGroup' }
  | { groupId: string; title: string; type: 'renameChatThreadGroup' }
  | { title: string; threadId: string; type: 'renameChatThread' }
  | { type: 'selectChatThread'; threadId: string }
  | { type: 'selectJob'; jobId: string }
  | { type: 'setActiveFilter'; filter: JobFilter }
  | { type: 'setActiveView'; view: AppView }
  | { type: 'setComposerVisible'; isVisible: boolean }
  | { type: 'setReportExpanded'; isExpanded: boolean }
  | { type: 'setReportVisible'; isVisible: boolean }
  | { type: 'setChatHistoryVisible'; isVisible: boolean }
  | { type: 'setKnowledgeHistoryVisible'; isVisible: boolean }
  | { key: ProjectPanelLayoutKey; size: number; type: 'setPanelLayoutSize' }
  | { enabled: boolean; type: 'setChatChainingEnabled' }
  | { type: 'setSelectedChatModelTier'; tier: ChatModelTier | null }
  // Apply the account preference to the working value ONLY — never to the
  // thread (the agent seeding rule; writing it there would look like a user
  // pick and shadow the thread's own stored one).
  | { type: 'seedChatModelTierFromPreference'; tier: ChatModelTier }
  | { type: 'setSelectedChatModel'; model: string | null }
  | { type: 'setSelectedChatEffort'; effort: string | null }
  | { type: 'setSelectedAgentModelTier'; tier: ChatModelTier | null }
  // Apply the account preference to the working value ONLY. It is not a user
  // pick, so it must never touch the session: writing it there would bump
  // updatedAt, make the local copy look newer than the server row, and block
  // the session's own stored pick from ever loading.
  | { type: 'seedAgentModelTierFromPreference'; tier: ChatModelTier }
  | { type: 'setSelectedAgentModel'; model: string | null }
  | { type: 'setSelectedAgentEffort'; effort: string | null }
  | { type: 'setSelectedStack'; stack: string }
  | { rule: ChatRuleRecord; type: 'upsertChatRule' }
  | { type: 'toggleJob'; jobId: string }
  | { assets: FileAssetRecord[]; type: 'ingestFileAssets' }
  | { fileId: string; label: string; type: 'renameFileAsset' }
  | { fileId: string; groupId: string | null; sectionId: string; type: 'moveFileAsset' }
  | { fileId: string; type: 'deleteFileAsset' }
  | { fileIds: string[]; type: 'deleteFileAssets' }
  | { fileIds: string[]; groupId: string | null; sectionId: string; type: 'moveFileAssets' }
  | { sectionId: string; title: string; type: 'createFileGroup' }
  | { groupId: string; title: string; type: 'renameFileGroup' }
  | { groupId: string; type: 'deleteFileGroup' }
  | { sectionId: string; title: string; type: 'renameFileLibrarySection' }
  | { sectionId: string; title: string; type: 'createFileLibrarySection' }
  | { sectionId: string; type: 'deleteFileLibrarySection' }
  | { dims?: number; fileIds: string[]; model?: EmbedModelId; title: string; type: 'createVectorIndex' }
  | { indexId: string; title: string; type: 'renameVectorIndex' }
  | { indexId: string; type: 'deleteVectorIndex' }
  | { dims?: number; indexId: string; model: EmbedModelId; type: 'setVectorIndexModel' }
  | { fileIds: string[]; indexId: string; type: 'addDocsToVectorIndex' }
  | { fileId: string; indexId: string; type: 'removeDocFromVectorIndex' }
  | { fileId: string; indexId: string; serverDocumentId: string; type: 'reconcileVectorIndexMemberDocument' }
  | { indexId: string; jobId: string; queuedFileIds?: string[]; runningFileIds?: string[]; source: IndexingJobLive['source']; status?: IndexingJobLive['status']; totalDocuments: number; type: 'startVectorIndexReindex' }
  | { indexId: string; queuePosition: number | null; type: 'markVectorIndexQueued' }
  | { autoResume?: AutoResumePlan | null; currentBatch?: number; errorType?: string | null; fileId: string; indexId: string; jobId?: string; phase?: string; queuePosition?: number | null; status: IndexingMemberLive['status']; totalBatches?: number; type: 'markVectorIndexMemberProgress' }
  | { autoResume: AutoResumePlan | null; completedDocuments: number; currentBatch: number; errorType: string | null; indexId: string; phase: string; status: 'paused_dependency' | 'paused_validation'; totalBatches: number; totalDocuments: number; type: 'markVectorIndexPaused' }
  | { indexId: string; status: 'paused_dependency' | 'paused_validation'; type: 'markVectorIndexRunPaused' }
  | { indexId: string; totalDocuments: number; type: 'markVectorIndexResumed' }
  | { indexId: string; type: 'markVectorIndexSuperseded' }
  | { completedDocuments: number; currentDocumentTitle?: string; embedded?: boolean; fileId?: string; indexId: string; runningFileIds?: string[]; totalDocuments: number; type: 'markVectorIndexProgress' }
  | { indexId: string; serverDocumentId: string; type: 'markVectorIndexDocumentEmbedded' }
  | { embeddedFileIds: string[]; skippedFileIds: string[]; indexId: string; serverCollectionId: string; serverCollectionModel: string; serverDocumentIds: Record<string, string>; type: 'adoptVectorIndexPartialResult' }
  | { embeddedFileIds?: string[]; skippedFileIds?: string[]; indexId: string; result?: 'cancelled' | 'ok'; serverCollectionId?: string; serverCollectionModel?: string; serverDocumentIds?: Record<string, string>; type: 'completeVectorIndexReindex' }
  | { indexId: string; message: string; serverCollectionId?: string; serverCollectionModel?: string; type: 'markVectorIndexError' }
  | { indexId: string; type: 'markVectorIndexCancelled' }
  | { title: string; type: 'createKnowledgeSessionGroup' }
  | { groupId: string; type: 'deleteKnowledgeSessionGroup' }
  | { groupId: string; targetIndex: number; type: 'moveKnowledgeSessionGroup' }
  | { groupId: string; title: string; type: 'renameKnowledgeSessionGroup' }
  | { groupId: string | null; sessionId: string; targetIndex: number; type: 'moveKnowledgeSessionToGroup' }
  | { session: KnowledgeSessionRecord; type: 'createKnowledgeSession' }
  | { operationId?: string | null; type: 'deleteKnowledgeSession'; sessionId: string }
  | { deletion: SessionDeletionState; sessionId: string; type: 'setKnowledgeSessionDeletionState' }
  | { sessionId: string; type: 'togglePinnedKnowledgeSession' }
  | { title: string; sessionId: string; type: 'renameKnowledgeSession' }
  | { type: 'selectKnowledgeSession'; sessionId: string }
  | { groups: KnowledgeSessionGroupRecord[]; type: 'upsertServerKnowledgeSessionGroups' }
  | { memberships: Record<string, string | null>; sessions: KnowledgeSessionRecord[]; type: 'upsertServerKnowledgeSessions' }
  | { replacements: Record<string, string>; type: 'rekeyKnowledgeSessionIds' }
  | { serverIds: string[]; type: 'pruneLocalPlaceholderKnowledgeSessions' }
  | { items: KnowledgeThreadItemRecord[]; sessionId: string; type: 'setServerKnowledgeSessionItems' }
  | { item: KnowledgeThreadItemRecord; type: 'startKnowledgeAsk' }
  | { itemIds: string[]; type: 'deleteKnowledgeItems' }
  | { sessionId: string; type: 'clearKnowledgeSession' }
  | { item: KnowledgeThreadItemRecord; replacedItemId: string; type: 'restartKnowledgeAsk' }
  | { answer: KnowledgeAnswerRecord; runId: string; type: 'completeKnowledgeItem' }

export function initializeResearchDeskState(): ResearchDeskState {
  return createEmptyProjectState()
}

function toggleExplorerPin(ids: readonly string[], id: string) {
  // Appending keeps the pinned section stable: the first pin stays on
  // top, a new pin attaches below and never reshuffles the existing
  // ones. Automatic sort modes render this array order; manual mode
  // keeps master order.
  return ids.includes(id)
    ? ids.filter((currentId) => currentId !== id)
    : [...ids, id]
}

function removeExplorerPin(ids: readonly string[], id: string) {
  return ids.filter((currentId) => currentId !== id)
}

export function researchDeskReducer(
  state: ResearchDeskState,
  action: ResearchDeskAction,
): ResearchDeskState {
  if (action.type === 'requestSessionTitle' || action.type === 'reconcileSessionTitle' || action.type === 'finishSessionTitle' || action.type === 'restoreSessionTitleDraft') {
    const record = action.kind === 'chat' ? state.chatThreads[action.id] : state.agentSessions[action.id]
    if (!record || record.deletion) return state
    if ('projectEpoch' in action && action.projectEpoch !== state.projectEpoch) return state
    let fields: Partial<TitledRecord>
    if (action.type === 'restoreSessionTitleDraft') {
      if (record.titleConflict || record.titlePending) return state
      fields = { titleConflict: action.title }
    } else if (action.type === 'requestSessionTitle') {
      if ((record.titleSource ?? 'manual') !== 'initial' || record.titleRequest) return state
      const title = initialSessionTitle(action.kind, action.question)
      fields = { title, titleRequest: { id: createId('title'), question: action.question, language: action.language, stack: action.stack }, titleGenerationReason: undefined,
        ...(title !== record.title ? { titlePending: true, titleEditId: createId('title-edit') } : {}) }
    } else if (action.type === 'reconcileSessionTitle') {
      fields = reconcileTitle(record, action.wire, action.submitted)
    } else {
      if (record.titleRequest?.id !== action.requestId) return state
      const wire = action.current ?? (action.title !== null
        ? { title: action.title, title_revision: (record.titleRevision ?? 0) + 1, title_source: 'generated' as const }
        : null)
      fields = { ...(wire ? reconcileTitle(record, wire, action.current ? undefined : record) : {}),
        titleRequest: undefined, titleGenerationReason: action.reason ?? undefined }
    }
    if (Object.entries(fields).every(([key, value]) => record[key as keyof TitledRecord] === value)) return state
    const dirty = state.dirty || (action.type === 'finishSessionTitle' && !action.current
      && fields.title !== undefined && fields.title !== record.title)
    return action.kind === 'chat'
      ? { ...state, dirty, chatThreads: { ...state.chatThreads, [action.id]: { ...state.chatThreads[action.id], ...fields } } }
      : { ...state, dirty, agentSessions: { ...state.agentSessions, [action.id]: { ...state.agentSessions[action.id], ...fields } } }
  }
  if (action.type === 'hydrateProject') {
    // Bump the load epoch relative to the OUTGOING state (not the loaded one),
    // so the project-scoped server-sync hooks see a changed identity and
    // re-hydrate from this project's own server state -- never carrying the
    // previous project's synced fingerprints into a delete. The loaded state's
    // own (ephemeral, unserialized) epoch is discarded on purpose.
    return { ...action.state, projectEpoch: state.projectEpoch + 1 }
  }
  if (action.type === 'rekeyFileLibrarySectionIds') {
    const replacements = Object.entries(action.replacements).filter(
      ([from, to]) => from !== to && Boolean(state.fileLibrarySections[from]),
    )
    if (replacements.length === 0) return state
    const replaceId = (id: string) => action.replacements[id] ?? id
    const fileLibrarySections = { ...state.fileLibrarySections }
    for (const [from, to] of replacements) {
      const section = fileLibrarySections[from]
      delete fileLibrarySections[from]
      // A server-canonical row may already have arrived through another
      // observation. Never overwrite it with this tab's bootstrap placeholder;
      // only redirect local child references to its identity.
      if (!fileLibrarySections[to]) {
        fileLibrarySections[to] = { ...section, id: to }
      }
    }
    const fileLibrarySectionOrder = [...new Set(
      state.fileLibrarySectionOrder.map(replaceId),
    )]
    return {
      ...state,
      dirty: true,
      fileAssets: Object.fromEntries(Object.entries(state.fileAssets).map(([id, asset]) => [
        id,
        { ...asset, sectionId: replaceId(asset.sectionId) },
      ])),
      fileGroups: Object.fromEntries(Object.entries(state.fileGroups).map(([id, group]) => [
        id,
        { ...group, sectionId: replaceId(group.sectionId) },
      ])),
      fileLibrarySectionOrder,
      fileLibrarySections,
    }
  }
  if (action.type === 'rekeyKnowledgeSessionIds') {
    const replacements = Object.entries(action.replacements).filter(
      ([from, to]) => from !== to && Boolean(state.knowledgeSessions[from]),
    )
    if (replacements.length === 0) return state
    const replaceId = (id: string) => action.replacements[id] ?? id
    const knowledgeSessions = { ...state.knowledgeSessions }
    const knowledgeSessionGroupMemberships = { ...state.knowledgeSessionGroupMemberships }
    for (const [from, to] of replacements) {
      const session = knowledgeSessions[from]
      delete knowledgeSessions[from]
      knowledgeSessions[to] = { ...session, id: to }
      const groupId = knowledgeSessionGroupMemberships[from]
      delete knowledgeSessionGroupMemberships[from]
      knowledgeSessionGroupMemberships[to] = groupId ?? null
    }
    return {
      ...state,
      dirty: true,
      knowledgeItems: Object.fromEntries(Object.entries(state.knowledgeItems).map(([id, item]) => [
        id,
        { ...item, sessionId: replaceId(item.sessionId) },
      ])),
      knowledgeSessionGroupMemberships,
      knowledgeSessionOrder: state.knowledgeSessionOrder.map(replaceId),
      knowledgeSessions,
      selectedKnowledgeSessionId: state.selectedKnowledgeSessionId
        ? replaceId(state.selectedKnowledgeSessionId)
        : null,
      ui: {
        ...state.ui,
        pinnedExplorer: {
          ...state.ui.pinnedExplorer,
          knowledgeSessionIds: state.ui.pinnedExplorer.knowledgeSessionIds.map(replaceId),
        },
      },
    }
  }
  if (action.type === 'setDemoMode') {
    const base = action.enabled ? createSeedProjectState() : createEmptyProjectState()
    return { ...base, projectEpoch: state.projectEpoch + 1 }
  }
  if (action.type === 'setExplorerSortMode') {
    if (state.ui.explorerSort[action.desk] === action.mode) return state
    return {
      ...state,
      dirty: true,
      ui: {
        ...state.ui,
        explorerSort: { ...state.ui.explorerSort, [action.desk]: action.mode },
      },
    }
  }
  if (action.type === 'adoptExplorerOrder') {
    // A drag inside an automatic sort mode adopts the visible order as
    // the new explicit order and switches the desk to manual — the list
    // must not jump at the moment of the switch.
    const adopted = {
      ...state,
      dirty: true,
      ui: {
        ...state.ui,
        explorerSort: { ...state.ui.explorerSort, [action.desk]: 'manual' as const },
      },
    }
    if (action.desk === 'chat') {
      return {
        ...adopted,
        chatThreadGroupOrder: adoptVisibleExplorerOrder(state.chatThreadGroupOrder, action.folderIds),
        chatThreadOrder: adoptVisibleExplorerOrder(state.chatThreadOrder, action.itemIds),
      }
    }
    if (action.desk === 'knowledge') {
      return {
        ...adopted,
        knowledgeSessionGroupOrder: adoptVisibleExplorerOrder(state.knowledgeSessionGroupOrder, action.folderIds),
        knowledgeSessionOrder: adoptVisibleExplorerOrder(state.knowledgeSessionOrder, action.itemIds),
      }
    }
    if (action.desk === 'agent') {
      return {
        ...adopted,
        agentSessionGroupOrder: adoptVisibleExplorerOrder(state.agentSessionGroupOrder, action.folderIds),
        agentSessionOrder: adoptVisibleExplorerOrder(state.agentSessionOrder, action.itemIds),
      }
    }
    return {
      ...adopted,
      editorDocumentOrder: adoptVisibleExplorerOrder(state.editorDocumentOrder, action.itemIds),
      editorFolderOrder: adoptVisibleExplorerOrder(state.editorFolderOrder, action.folderIds),
    }
  }
  if (action.type === 'togglePinnedChatThread') {
    if (!state.chatThreads[action.threadId]) return state
    return {
      ...state,
      dirty: true,
      ui: {
        ...state.ui,
        pinnedExplorer: {
          ...state.ui.pinnedExplorer,
          chatThreadIds: toggleExplorerPin(state.ui.pinnedExplorer.chatThreadIds, action.threadId),
        },
      },
    }
  }
  if (action.type === 'togglePinnedEditorDocument') {
    if (!state.editorDocuments[action.documentId]) return state
    return {
      ...state,
      dirty: true,
      ui: {
        ...state.ui,
        pinnedExplorer: {
          ...state.ui.pinnedExplorer,
          editorDocumentIds: toggleExplorerPin(state.ui.pinnedExplorer.editorDocumentIds, action.documentId),
        },
      },
    }
  }
  if (action.type === 'togglePinnedKnowledgeSession') {
    if (!state.knowledgeSessions[action.sessionId]) return state
    return {
      ...state,
      dirty: true,
      ui: {
        ...state.ui,
        pinnedExplorer: {
          ...state.ui.pinnedExplorer,
          knowledgeSessionIds: toggleExplorerPin(
            state.ui.pinnedExplorer.knowledgeSessionIds,
            action.sessionId,
          ),
        },
      },
    }
  }
  if (action.type === 'markProjectSaved') {
    return {
      ...state,
      connection: action.connection,
      dirty: false,
      preferences: action.preferences ?? state.preferences,
      project: {
        ...state.project,
        updatedAt: action.savedAt,
      },
    }
  }
  if (action.type === 'setActiveView') {
    return { ...state, ui: { ...state.ui, activeView: action.view } }
  }
  if (action.type === 'setActiveFilter') {
    const selectedJobId = resolveVisibleSelection(
      state.researchRunOrder,
      state.researchRuns,
      action.filter,
      state.ui.selectedJobId,
    )

    return {
      ...state,
      ui: {
        ...state.ui,
        activeFilter: action.filter,
        expandedJobId: selectedJobId,
        selectedJobId,
      },
    }
  }
  if (action.type === 'setSelectedStack') {
    return {
      ...state,
      dirty: true,
      ui: { ...state.ui, selectedStack: action.stack },
    }
  }
  if (action.type === 'setSelectedChatModelTier') {
    // Picking a tier (the fallback picker) clears any explicit model choice.
    return withChatThreadModelSelection(state, {
      ...state.ui,
      selectedChatModelTier: action.tier,
      selectedChatModel: null,
      selectedChatEffort: null,
    })
  }
  if (action.type === 'setSelectedChatModel') {
    // Picking a concrete model clears the tier and resets effort to the
    // model's provider default (the reasoning control re-sets it explicitly).
    return withChatThreadModelSelection(state, {
      ...state.ui,
      selectedChatModel: action.model,
      selectedChatModelTier: null,
      selectedChatEffort: null,
    })
  }
  if (action.type === 'setSelectedChatEffort') {
    return withChatThreadModelSelection(state, {
      ...state.ui,
      selectedChatEffort: action.effort,
    })
  }
  if (action.type === 'seedChatModelTierFromPreference') {
    if (
      state.ui.selectedChatModelTier !== null
      || state.ui.selectedChatModel !== null
    ) return state
    // ui only, no dirty: a preference echo is not a project change, and the
    // thread is deliberately untouched.
    return { ...state, ui: { ...state.ui, selectedChatModelTier: action.tier } }
  }
  if (action.type === 'setSelectedAgentModelTier') {
    // Same exclusivity contract as the chat picker: a tier pick clears
    // the explicit model AND the effort (effort is model-dependent).
    return withAgentSessionModelSelection(state, {
      ...state.ui,
      selectedAgentModelTier: action.tier,
      selectedAgentModel: null,
      selectedAgentEffort: null,
    })
  }
  if (action.type === 'setSelectedAgentModel') {
    return withAgentSessionModelSelection(state, {
      ...state.ui,
      selectedAgentModel: action.model,
      selectedAgentModelTier: null,
      selectedAgentEffort: null,
    })
  }
  if (action.type === 'setSelectedAgentEffort') {
    return withAgentSessionModelSelection(state, {
      ...state.ui,
      selectedAgentEffort: action.effort,
    })
  }
  if (action.type === 'seedAgentModelTierFromPreference') {
    if (
      state.ui.selectedAgentModelTier !== null
      || state.ui.selectedAgentModel !== null
    ) return state
    // ui only, no dirty: a preference echo is not a project change, and the
    // session is deliberately untouched.
    return { ...state, ui: { ...state.ui, selectedAgentModelTier: action.tier } }
  }
  if (action.type === 'setChatChainingEnabled') {
    return {
      ...state,
      dirty: true,
      ui: { ...state.ui, chatChainingEnabled: action.enabled },
    }
  }
  if (action.type === 'setReportExpanded') {
    return { ...state, ui: { ...state.ui, isReportExpanded: action.isExpanded } }
  }
  if (action.type === 'setReportVisible') {
    // Collapse state is a persisted ui field (markdown.ts/fileSystem.ts); mark
    // dirty so a pure toggle is durably saved/pushed and survives reload, like
    // chatChainingEnabled above (not opportunistically left to the next save).
    return { ...state, dirty: true, ui: { ...state.ui, isReportVisible: action.isVisible } }
  }
  if (action.type === 'setChatHistoryVisible') {
    return { ...state, dirty: true, ui: { ...state.ui, isChatHistoryVisible: action.isVisible } }
  }
  if (action.type === 'setKnowledgeHistoryVisible') {
    return { ...state, dirty: true, ui: { ...state.ui, isKnowledgeHistoryVisible: action.isVisible } }
  }
  if (action.type === 'setPanelLayoutSize') {
    const size = clampPanelLayoutSize(action.key, action.size)
    if (state.ui.panelLayout[action.key] === size) return state
    return {
      ...state,
      dirty: true,
      ui: {
        ...state.ui,
        panelLayout: {
          ...state.ui.panelLayout,
          [action.key]: size,
        },
      },
    }
  }
  if (action.type === 'setComposerVisible') {
    return { ...state, ui: { ...state.ui, isComposerVisible: action.isVisible } }
  }
  if (action.type === 'selectJob') {
    return {
      ...state,
      ui: {
        ...state.ui,
        expandedJobId: action.jobId,
        selectedJobId: action.jobId,
      },
    }
  }
  if (action.type === 'toggleJob') {
    return {
      ...state,
      ui: {
        ...state.ui,
        expandedJobId: state.ui.expandedJobId === action.jobId ? null : action.jobId,
        selectedJobId: action.jobId,
      },
    }
  }
  if (action.type === 'deleteJob') {
    if (!state.researchRuns[action.jobId]) return state
    const researchRuns = { ...state.researchRuns }
    delete researchRuns[action.jobId]
    const researchRunOrder = state.researchRunOrder.filter((runId) => runId !== action.jobId)
    const selectedJobId = resolveVisibleSelection(
      researchRunOrder,
      researchRuns,
      state.ui.activeFilter,
      state.ui.selectedJobId === action.jobId ? null : state.ui.selectedJobId,
    )

    return {
      ...state,
      dirty: true,
      researchRunOrder,
      researchRuns,
      ui: {
        ...state.ui,
        expandedJobId: selectedJobId,
        pendingChatReportRunId: state.ui.pendingChatReportRunId === action.jobId
          ? null
          : state.ui.pendingChatReportRunId,
        selectedJobId,
      },
    }
  }
  if (action.type === 'createEditorFolder') {
    const now = new Date().toISOString()
    const folder: EditorFolderRecord = {
      createdAt: now,
      id: createId('editor-folder'),
      title: action.title.trim() || 'New folder',
      updatedAt: now,
    }
    return {
      ...state,
      dirty: true,
      editorFolderOrder: [...state.editorFolderOrder, folder.id],
      editorFolders: {
        ...state.editorFolders,
        [folder.id]: folder,
      },
    }
  }
  if (action.type === 'renameEditorFolder') {
    const folder = state.editorFolders[action.folderId]
    const title = action.title.trim()
    if (!folder || !title || folder.title === title) return state
    return {
      ...state,
      dirty: true,
      editorFolders: {
        ...state.editorFolders,
        [folder.id]: {
          ...folder,
          title,
          updatedAt: new Date().toISOString(),
        },
      },
    }
  }
  if (action.type === 'deleteEditorFolder') {
    if (!state.editorFolders[action.folderId]) return state
    const editorFolders = { ...state.editorFolders }
    delete editorFolders[action.folderId]
    const updatedAt = new Date().toISOString()
    return {
      ...state,
      dirty: true,
      editorDocumentOrder: moveSectionDocumentIds(
        state.editorDocumentOrder,
        state.editorDocuments,
        action.folderId,
        null,
        state.editorDocumentOrder.length,
      ),
      editorDocuments: Object.fromEntries(
        Object.entries(state.editorDocuments).map(([documentId, document]) => [
          documentId,
          document.folderId === action.folderId
            // Dirtying edit bumps updatedAt (the autosave trigger), NOT the
            // revision: revision is the last-synced server base, and the save
            // sends base+1 (see updateEditorDocumentMarkdown / editorSync).
            ? { ...document, folderId: null, updatedAt }
            : document,
        ]),
      ),
      editorFolderOrder: state.editorFolderOrder.filter((folderId) => folderId !== action.folderId),
      editorFolders,
    }
  }
  if (action.type === 'moveEditorFolder') {
    return moveEditorFolder(state, action.folderId, action.targetIndex)
  }
  if (action.type === 'moveEditorDocumentToFolder') {
    return moveEditorDocumentToFolder(state, action.documentId, action.folderId, action.targetIndex)
  }
  if (action.type === 'createEditorDocument') {
    const document = createEditorDocument({
      folderId: action.folderId ?? null,
      source: 'blank',
    })
    return withEditorDocument(state, document, { activeView: 'editor' })
  }
  if (action.type === 'importResearchReportToEditor') {
    const report = reportFromRun(state, action.runId)
    if (!report) return state
    const document = createEditorDocument({
      contentMarkdown: report.contentMarkdown,
      source: 'imported-research-report',
      sourceRunId: report.runId,
      title: `${report.title}.md`,
    })
    return withEditorDocument(state, document, { activeView: 'editor' })
  }
  if (action.type === 'openEditorDocument') {
    if (!state.editorDocuments[action.documentId]) return state
    return {
      ...state,
      ui: { ...state.ui, activeView: 'editor' },
      editorUi: {
        ...state.editorUi,
        activeDocumentId: action.documentId,
        openDocumentIds: addOpenEditorDocumentId(
          state.editorUi.openDocumentIds,
          action.documentId,
        ),
      },
    }
  }
  if (action.type === 'setActiveEditorDocument') {
    if (!state.editorDocuments[action.documentId]) return state
    return {
      ...state,
      editorUi: {
        ...state.editorUi,
        activeDocumentId: action.documentId,
        openDocumentIds: addOpenEditorDocumentId(
          state.editorUi.openDocumentIds,
          action.documentId,
        ),
      },
    }
  }
  if (action.type === 'closeEditorDocumentTab') {
    const openDocumentIds = state.editorUi.openDocumentIds.filter((id) => id !== action.documentId)
    const activeDocumentId = state.editorUi.activeDocumentId === action.documentId
      ? openDocumentIds[openDocumentIds.length - 1] ?? null
      : state.editorUi.activeDocumentId
    return {
      ...state,
      editorUi: {
        ...state.editorUi,
        activeDocumentId,
        openDocumentIds,
        selectedCommentId: activeDocumentId ? state.editorUi.selectedCommentId : null,
      },
    }
  }
  if (action.type === 'deleteEditorDocument') {
    if (!state.editorDocuments[action.documentId]) return state
    const editorDocuments = { ...state.editorDocuments }
    delete editorDocuments[action.documentId]
    const editorComments = Object.fromEntries(
      Object.entries(state.editorComments).filter(([, comment]) => comment.documentId !== action.documentId),
    )
    const editorCommentOutbox = Object.fromEntries(
      Object.entries(state.editorCommentOutbox ?? {}).filter(([, entry]) => (
        entry.documentId !== action.documentId
      )),
    )
    const editorDocumentOrder = state.editorDocumentOrder.filter((id) => id !== action.documentId)
    const openDocumentIds = state.editorUi.openDocumentIds.filter((id) => id !== action.documentId)
    const activeDocumentId = state.editorUi.activeDocumentId === action.documentId
      ? openDocumentIds[openDocumentIds.length - 1] ?? editorDocumentOrder[0] ?? null
      : state.editorUi.activeDocumentId
    return {
      ...state,
      dirty: true,
      editorCommentOutbox,
      editorComments,
      editorDocumentOrder,
      editorDocuments,
      ui: {
        ...state.ui,
        pinnedExplorer: {
          ...state.ui.pinnedExplorer,
          editorDocumentIds: removeExplorerPin(state.ui.pinnedExplorer.editorDocumentIds, action.documentId),
        },
      },
      editorUi: {
        ...state.editorUi,
        activeDocumentId,
        openDocumentIds: activeDocumentId
          ? addOpenEditorDocumentId(openDocumentIds, activeDocumentId)
          : openDocumentIds,
        selectedCommentId: null,
      },
    }
  }
  if (action.type === 'addEditorRecoveryDocument') {
    // The original stays; its local copy stands right before it.
    const recovery = createEditorRecoveryDocument(
      { ...action.source, id: action.capture.documentId },
      action.capture,
      action.reason,
    )
    const originalIndex = state.editorDocumentOrder.indexOf(action.capture.documentId)
    const editorDocumentOrder = [...state.editorDocumentOrder]
    editorDocumentOrder.splice(Math.max(0, originalIndex), 0, recovery.id)
    return {
      ...state,
      dirty: true,
      editorDocumentOrder,
      editorDocuments: {
        ...state.editorDocuments,
        [recovery.id]: recovery,
      },
    }
  }
  if (action.type === 'promoteEditorRecoveryDocument') {
    const document = state.editorDocuments[action.documentId]
    if (!document) return state
    const { recovery, ...localDocument } = document
    if (!recovery) return state
    const updatedAt = new Date().toISOString()
    const editorCommentOutbox = { ...(state.editorCommentOutbox ?? {}) }
    for (const comment of Object.values(state.editorComments)) {
      if (comment.documentId !== document.id) continue
      editorCommentOutbox[comment.id] = {
        documentId: document.id,
        operation: 'upsert',
        updatedAt: comment.updatedAt,
      }
    }
    return {
      ...state,
      dirty: true,
      editorCommentOutbox,
      editorDocuments: {
        ...state.editorDocuments,
        [document.id]: {
          ...localDocument,
          updatedAt,
        },
      },
    }
  }
  if (action.type === 'renameEditorDocument') {
    const document = state.editorDocuments[action.documentId]
    const title = normalizeEditorDocumentTitle(action.title)
    if (!document || document.title === title) return state
    return {
      ...state,
      dirty: true,
      editorDocuments: {
        ...state.editorDocuments,
        [document.id]: {
          ...document,
          title,
          // Only updatedAt moves (the autosave trigger); revision stays the
          // last-synced server base. The save sends base+1, so a rename off
          // the current base still CAS-matches -- no revision bump needed.
          updatedAt: new Date().toISOString(),
        },
      },
    }
  }
  if (action.type === 'updateEditorDocumentMarkdown') {
    const document = state.editorDocuments[action.documentId]
    if (!document || document.contentMarkdown === action.contentMarkdown) return state
    const updatedAt = new Date().toISOString()
    return {
      ...state,
      dirty: true,
      editorDocuments: {
        ...state.editorDocuments,
        [document.id]: {
          ...document,
          contentMarkdown: action.contentMarkdown,
          // revision is the last-synced server base and stays put across
          // local edits; the save sends base+1 and the CAS is against that
          // base, so a stale writer whose base is behind the server 409s
          // (real optimistic concurrency, not a monotonic counter). updatedAt
          // is the local-edit signal the autosave debounce reads.
          updatedAt,
        },
      },
    }
  }
  if (action.type === 'acknowledgeEditorCommentOutbox') {
    const current = state.editorCommentOutbox ?? {}
    let changed = false
    const editorCommentOutbox = { ...current }
    for (const acknowledgement of action.acknowledgements) {
      const pending = editorCommentOutbox[acknowledgement.commentId]
      if (
        !pending
        || pending.operation !== acknowledgement.operation
        || pending.updatedAt !== acknowledgement.updatedAt
      ) continue
      delete editorCommentOutbox[acknowledgement.commentId]
      changed = true
    }
    return changed ? { ...state, editorCommentOutbox } : state
  }
  if (action.type === 'createEditorComment') {
    if (!state.editorDocuments[action.comment.documentId]) return state
    return markEditorCommentOutbox({
      ...state,
      dirty: true,
      editorComments: {
        ...state.editorComments,
        [action.comment.id]: action.comment,
      },
      editorUi: {
        ...state.editorUi,
        isCommentPanelVisible: true,
        panelTab: 'comments',
        selectedCommentId: action.comment.id,
      },
    }, action.comment, 'upsert')
  }
  if (action.type === 'adoptEditorCarrierComment') {
    // Die Maschinenzeile eines Assistentenlaufs. Sie ist bereits
    // servergespeichert -- der Entwurf haengt daran -, also KEIN
    // Outbox-Eintrag. Und sie ist keine Notiz des Nutzers, also weder
    // Panel oeffnen noch auswaehlen: der Zustand braucht sie nur, damit
    // "Verfeinern" und "Bearbeiten" ihren Kommentar wiederfinden.
    if (!state.editorDocuments[action.comment.documentId]) return state
    return {
      ...state,
      editorComments: {
        ...state.editorComments,
        [action.comment.id]: action.comment,
      },
    }
  }
  if (action.type === 'resolveEditorComment') {
    const comment = state.editorComments[action.commentId]
    if (!comment || comment.status === 'resolved') return state
    const now = new Date().toISOString()
    return markEditorCommentOutbox({
      ...state,
      dirty: true,
      editorComments: {
        ...state.editorComments,
        [comment.id]: {
          ...comment,
          status: 'resolved',
          updatedAt: now,
        },
      },
      editorSuggestions: retireActiveEditorSuggestionsForComments(
        state.editorSuggestions,
        new Set([comment.id]),
        now,
      ),
    }, { ...comment, status: 'resolved', updatedAt: now }, 'upsert')
  }
  if (action.type === 'setEditorCommentStatus') {
    const comment = state.editorComments[action.commentId]
    if (!comment || comment.status === action.status) return state
    const now = new Date().toISOString()
    const updatedComment = { ...comment, status: action.status, updatedAt: now }
    return markEditorCommentOutbox({
      ...state,
      dirty: true,
      editorComments: {
        ...state.editorComments,
        [comment.id]: updatedComment,
      },
      editorSuggestions: action.status === 'resolved'
        ? retireActiveEditorSuggestionsForComments(state.editorSuggestions, new Set([comment.id]), now)
        : state.editorSuggestions,
    }, updatedComment, 'upsert')
  }
  if (action.type === 'deleteEditorComment') {
    const comment = state.editorComments[action.commentId]
    if (!comment) return state
    const editorComments = { ...state.editorComments }
    delete editorComments[action.commentId]
    const editorSuggestions = Object.fromEntries(
      Object.entries(state.editorSuggestions).filter(([, suggestion]) =>
        suggestion.origin.commentId !== action.commentId),
    )
    return markEditorCommentOutbox({
      ...state,
      dirty: true,
      editorComments,
      editorSuggestions,
      editorUi: {
        ...state.editorUi,
        selectedCommentId: state.editorUi.selectedCommentId === action.commentId
          ? null
          : state.editorUi.selectedCommentId,
      },
    }, comment, 'delete')
  }
  if (action.type === 'updateEditorCommentText') {
    const comment = state.editorComments[action.commentId]
    const text = action.contentMarkdown.trim()
    if (!comment || !text || comment.commentMarkdown === text) return state
    const now = new Date().toISOString()
    const updatedComment = { ...comment, commentMarkdown: text, updatedAt: now }
    return markEditorCommentOutbox({
      ...state,
      dirty: true,
      editorComments: {
        ...state.editorComments,
        [comment.id]: updatedComment,
      },
      editorSuggestions: retireActiveEditorSuggestionsForComments(
        state.editorSuggestions,
        new Set([comment.id]),
        now,
      ),
    }, updatedComment, 'upsert')
  }
  if (action.type === 'adoptEditorCommentSuggestionDraft') {
    const comment = state.editorComments[action.commentId]
    if (!comment) return state
    const updatedComment = {
      ...comment,
      anchor: action.anchor,
      suggestionDraft: action.suggestionDraft,
    }
    return reconcilePrivateSuggestionDraftRecords({
      ...state,
      editorComments: {
        ...state.editorComments,
        [comment.id]: updatedComment,
      },
    }, [updatedComment])
  }
  if (action.type === 'createEditorSuggestionGroup') {
    const now = new Date().toISOString()
    const commentIds = new Set(
      action.suggestions
        .map((suggestion) => suggestion.origin.commentId)
        .filter((commentId): commentId is string => Boolean(commentId)),
    )
    let editorSuggestions = retireActiveEditorSuggestionsForComments(
      state.editorSuggestions,
      commentIds,
      now,
    )
    if (action.group.origin.kind === 'global_run' && !action.group.origin.commentId) {
      editorSuggestions = retireActiveDocumentInstructionSuggestions(
        editorSuggestions,
        action.group.documentId,
        now,
      )
    }
    // Spread into a fresh object before adding the new suggestions. When nothing was
    // retired, the retire helpers return the input map by reference, so mutating it here
    // would keep `state.editorSuggestions` referentially equal across the update. Consumers
    // memoize on that reference (e.g. documentSuggestions in useEditorSuggestions), so an
    // in-place mutation leaves the new suggestion invisible until an unrelated re-render --
    // the "first suggestion only shows after a second action" bug in production builds.
    const nextEditorSuggestions = { ...editorSuggestions }
    for (const suggestion of action.suggestions) {
      nextEditorSuggestions[suggestion.id] = suggestion
    }
    return {
      ...state,
      dirty: true,
      editorSuggestionGroups: { ...state.editorSuggestionGroups, [action.group.id]: action.group },
      editorSuggestions: nextEditorSuggestions,
    }
  }
  if (
    action.type === 'acceptEditorSuggestion'
    || action.type === 'rejectEditorSuggestion'
    || action.type === 'markEditorSuggestionStale'
  ) {
    const suggestion = state.editorSuggestions[action.suggestionId]
    if (!suggestion) return state
    const status = action.type === 'acceptEditorSuggestion'
      ? 'accepted'
      : action.type === 'rejectEditorSuggestion'
        ? 'rejected'
        : 'stale'
    if (suggestion.status === status) return state
    let editorComments = state.editorComments
    const privateComment = suggestion.origin.commentId
      ? state.editorComments[suggestion.origin.commentId]
      : undefined
    if (
      action.type !== 'markEditorSuggestionStale'
      && suggestion.privateDraft
      && privateComment?.suggestionDraft?.patchId === suggestion.privateDraft.patchId
    ) {
      const { suggestionDraft, ...commentWithoutDraft } = privateComment
      void suggestionDraft
      editorComments = {
        ...state.editorComments,
        [privateComment.id]: commentWithoutDraft,
      }
    }
    const updatedSuggestion: EditorSuggestionRecord = {
      ...suggestion,
      ...(action.type === 'acceptEditorSuggestion' && action.collaborationPublication
        ? { collaborationPublication: action.collaborationPublication }
        : {}),
      status,
      updatedAt: new Date().toISOString(),
    }
    if (action.type !== 'markEditorSuggestionStale') {
      delete updatedSuggestion.privateDraft
    }
    const nextState: ProjectState = {
      ...state,
      dirty: true,
      editorComments,
      editorSuggestions: {
        ...state.editorSuggestions,
        [suggestion.id]: updatedSuggestion,
      },
    }
    if (action.type !== 'acceptEditorSuggestion' || !suggestion.origin.commentId) {
      return nextState
    }
    const comment = nextState.editorComments[suggestion.origin.commentId]
    if (!comment || comment.status === 'resolved') return nextState
    const updatedComment = { ...comment, status: 'resolved' as const, updatedAt: new Date().toISOString() }
    return markEditorCommentOutbox({
      ...nextState,
      editorComments: {
        ...nextState.editorComments,
        [comment.id]: updatedComment,
      },
      editorUi: {
        ...nextState.editorUi,
        selectedCommentId: nextOpenEditorCommentId(nextState, comment.id),
      },
    }, updatedComment, 'upsert')
  }
  if (action.type === 'updateEditorSuggestionProposal') {
    const suggestion = state.editorSuggestions[action.suggestionId]
    const proposedText = action.proposedText
    if (!suggestion || suggestion.status !== 'pending' || !proposedText.trim()) return state
    const now = new Date().toISOString()
    const previousRevision = suggestion.revision ?? 1
    const historyEntry = {
      changeSummary: suggestion.changeSummary,
      createdAt: now,
      instruction: action.instruction,
      proposedText: suggestion.proposedText,
      source: action.source,
      warnings: suggestion.warnings,
    }
    return {
      ...state,
      dirty: true,
      editorSuggestions: {
        ...state.editorSuggestions,
        [suggestion.id]: {
          ...suggestion,
          changeSummary: action.changeSummary?.length ? action.changeSummary : undefined,
          proposedText,
          revision: previousRevision + 1,
          revisionHistory: [...(suggestion.revisionHistory ?? []), historyEntry],
          updatedAt: now,
          warnings: action.warnings?.length ? action.warnings : undefined,
        },
      },
    }
  }
  if (action.type === 'acceptEditorSuggestionGroup' || action.type === 'rejectEditorSuggestionGroup') {
    const status = action.type === 'acceptEditorSuggestionGroup' ? 'accepted' : 'rejected'
    const now = new Date().toISOString()
    const editorSuggestions = { ...state.editorSuggestions }
    let changed = false
    for (const suggestion of Object.values(state.editorSuggestions)) {
      // A suggestion whose passage changed can still be rejected, never accepted.
      const decidable = suggestion.status === 'pending'
        || (status === 'rejected' && suggestion.status === 'stale')
      if (suggestion.groupId !== action.groupId || !decidable) continue
      editorSuggestions[suggestion.id] = { ...suggestion, status, updatedAt: now }
      changed = true
    }
    if (!changed) return state
    return { ...state, dirty: true, editorSuggestions }
  }
  if (action.type === 'selectEditorComment') {
    return {
      ...state,
      editorUi: {
        ...state.editorUi,
        isCommentPanelVisible: action.commentId ? true : state.editorUi.isCommentPanelVisible,
        panelTab: action.commentId ? 'comments' : state.editorUi.panelTab,
        selectedCommentId: action.commentId,
      },
    }
  }
  if (action.type === 'setEditorAssistantVisible') {
    return {
      ...state,
      editorUi: { ...state.editorUi, isAssistantVisible: action.isVisible },
    }
  }
  if (action.type === 'setEditorCommentPanelVisible') {
    return {
      ...state,
      dirty: true,
      editorUi: { ...state.editorUi, isCommentPanelVisible: action.isVisible },
    }
  }
  if (action.type === 'setEditorTreeVisible') {
    return {
      ...state,
      dirty: true,
      editorUi: { ...state.editorUi, isTreeVisible: action.isVisible },
    }
  }
  if (action.type === 'setEditorAssistantDraft') {
    return {
      ...state,
      dirty: true,
      editorUi: { ...state.editorUi, assistantDraft: action.draft },
    }
  }
  if (action.type === 'setEditorPanelTab') {
    return {
      ...state,
      editorUi: { ...state.editorUi, panelTab: action.tab },
    }
  }
  if (action.type === 'setEditorViewMode') {
    return {
      ...state,
      editorUi: { ...state.editorUi, viewMode: action.mode },
    }
  }
  if (action.type === 'setEditorDiffAnchor') {
    const document = state.editorDocuments[action.documentId]
    if (!document) return state
    const now = new Date().toISOString()
    return {
      ...state,
      dirty: true,
      editorDocuments: {
        ...state.editorDocuments,
        [document.id]: {
          ...document,
          diffAnchorMarkdown: action.contentMarkdown,
          diffAnchorUpdatedAt: now,
        },
      },
    }
  }
  if (action.type === 'confirmEditorCollaborationProjection') {
    const document = state.editorDocuments[action.documentId]
    if (!document || !document.collaboration) return state
    return {
      ...state,
      editorDocuments: {
        ...state.editorDocuments,
        [document.id]: {
          ...document,
          collaboration: {
            ...document.collaboration,
            persistedSequence: action.sequence,
            projectionSequence: action.sequence,
            projectionUpdatedAt: action.confirmedAt,
          },
          contentMarkdown: action.contentMarkdown,
        },
      },
    }
  }
  if (action.type === 'setEditorDiffVisible') {
    return {
      ...state,
      editorUi: {
        ...state.editorUi,
        isDiffVisible: action.isVisible,
        viewMode: action.isVisible ? 'live' : state.editorUi.viewMode,
      },
    }
  }
  if (action.type === 'createLocalRun') {
    const run = createLocalResearchRun(
      action.request,
      state.localRunCounter,
      state.ui.selectedStack,
    )
    return {
      ...state,
      dirty: true,
      localRunCounter: state.localRunCounter + 1,
      researchRunOrder: [run.runId, ...state.researchRunOrder],
      researchRuns: {
        ...state.researchRuns,
        [run.runId]: run,
      },
      ui: {
        ...state.ui,
        activeFilter: 'all',
        expandedJobId: run.runId,
        selectedJobId: run.runId,
      },
    }
  }
  if (action.type === 'replaceApiRunSummaries') {
    return replaceApiRunSummaries(state, action.summaries)
  }
  if (action.type === 'adoptImportedApiRun') {
    return adoptImportedApiRun(state, action.sourceRunId, action.summary)
  }
  if (action.type === 'upsertApiRunSummary') {
    // Knowledge-mode runs are owned by the Knowledge thread. Keeping them out of
    // the global run store prevents deleted/incognito Q&A from reappearing via
    // run-list hydration or project export.
    if (action.summary.mode === 'knowledge') return state
    // Agent runs live on the Agent Desk (own record model); child runs are
    // internal to their parent and never surface as standalone cards.
    if (isAgentRunSummary(action.summary)) {
      return withAgentRunSummary(
        state,
        action.summary,
        Boolean(action.select),
        // Hydration replays history newest-first — only a LIVE submit may
        // hand a placeholder session its title, or a replay drifts it to
        // whichever question happens to arrive first.
        { retitle: !action.hydration },
      )
    }
    if (action.summary.kind === 'agent_child') return state
    const current = state.researchRuns[action.summary.run_id]
    const run = mergeRunSummary(current, action.summary, state.ui.selectedStack)
    const researchRunOrder = state.researchRunOrder.includes(run.runId)
      ? state.researchRunOrder
      : [run.runId, ...state.researchRunOrder]
    // Knowledge-mode runs are surfaced by the Wissen thread, never as
    // the selected research job (they are filtered from the job list).
    const shouldSelect = run.mode !== 'knowledge'
      && (action.select || state.ui.selectedJobId === null)
    const selectedJobId = shouldSelect ? run.runId : state.ui.selectedJobId

    return {
      ...state,
      dirty: true,
      researchRunOrder,
      researchRuns: {
        ...state.researchRuns,
        [run.runId]: run,
      },
      ui: {
        ...state.ui,
        activeFilter: shouldSelect ? 'all' : state.ui.activeFilter,
        expandedJobId: shouldSelect ? run.runId : state.ui.expandedJobId,
        selectedJobId,
      },
    }
  }
  if (action.type === 'upsertAgentRunSummary') {
    // Server-derived rows: never dirty (session-scoped, not project files).
    return withAgentRunSummary(state, action.summary, Boolean(action.select), {
      retitle: true,
    })
  }
  if (action.type === 'upsertInspectedAgentChild') {
    const summary = action.summary
    if (summary.kind !== 'agent_child') return state
    const owner = summary.root_run_id ?? summary.parent_run_id
    if (!owner || !state.agentRuns[owner]) return state
    return {
      ...state,
      agentRuns: {
        ...state.agentRuns,
        [summary.run_id]: mergeAgentRunSummary(state.agentRuns[summary.run_id], summary),
      },
    }
  }
  if (action.type === 'setAgentRunPlan') {
    // `plan: null` = the server has no plan yet (pre-planning 404); the
    // stale flag still clears so the fetch effect cannot loop.
    return updateAgentRun(state, action.runId, (run) => ({
      ...run,
      plan: action.plan ? agentPlanFromWire(action.plan) : run.plan,
      planStale: false,
    }))
  }
  if (action.type === 'markAgentRunPlanStale') {
    // View-open re-flags the plan through the ONE background fetch path
    // (useAgentControlApi); idempotent while already flagged, and never
    // `dirty` (server-derived state).
    return updateAgentRun(state, action.runId, (run) =>
      run.planStale ? run : { ...run, planStale: true })
  }
  if (action.type === 'ackAgentTaskCancel') {
    return updateAgentRun(state, action.runId, (run) => (
      acknowledgeAgentTaskCancellation(
        run,
        action.taskId,
        action.status,
        action.childRunId,
      )
    ))
  }
  if (action.type === 'setAgentRunApprovals') {
    // Reconcile instead of replace: a stale full-list refetch (racing the
    // decide commit) must not regress a locally decided approval back to
    // pending — that re-opened the composer gate tray after the decision.
    return updateAgentRun(state, action.runId, (run) => ({
      ...run,
      approvals: mergeAgentRunApprovals(
        run.approvals,
        action.approvals.map(agentApprovalFromWire),
      ),
      approvalsStale: false,
    }))
  }
  if (action.type === 'setAgentRunClarifications') {
    // Same reconcile-not-replace contract as approvals: a stale
    // full-list refetch must never regress an answered round back to
    // pending (that re-opened the gate tray after the answer).
    return updateAgentRun(state, action.runId, (run) => ({
      ...run,
      clarifications: mergeAgentRunClarifications(
        run.clarifications,
        action.clarifications.map(agentClarificationFromWire),
      ),
      clarificationsStale: false,
    }))
  }
  if (action.type === 'setAgentChildGates') {
    // Same reconcile-not-replace contract as the root surfaces, applied to
    // one parked child's rows on the PARENT record (children never become
    // desk records). A decide seeds its single fresh row; the fetch brings
    // the full lists — neither may regress a decided row to pending.
    return updateAgentRun(state, action.runId, (run) => {
      const gates = run.childGates[action.childRunId]
      return {
        ...run,
        childGates: {
          ...run.childGates,
          [action.childRunId]: {
            approvals: mergeAgentRunApprovals(
              gates?.approvals ?? [],
              (action.approvals ?? []).map(agentApprovalFromWire),
            ),
            clarifications: mergeAgentRunClarifications(
              gates?.clarifications ?? [],
              (action.clarifications ?? []).map(agentClarificationFromWire),
            ),
            question: action.question ?? gates?.question,
            stale: false,
          },
        },
      }
    })
  }
  if (action.type === 'setAgentSessionArtifacts') {
    // Anchor-independent index: the rows carry each artifact's
    // CURRENT run anchor; server-derived, never dirty — except a body
    // held for a child-anchored artifact, kept while it is not older
    // than the refreshed row (the run-scoped rule below, mirrored).
    const previousIndex = state.agentSessionArtifacts[action.sessionId]
    const byId: Record<string, AgentSessionArtifactMeta> = {}
    const order: string[] = []
    for (const wire of action.artifacts) {
      const meta = sessionArtifactMetaFromWire(wire)
      const held = previousIndex?.byId[meta.artifactId]?.record
      byId[meta.artifactId] =
        held && held.revision >= meta.revision ? { ...meta, record: held } : meta
      order.push(meta.artifactId)
    }
    return {
      ...state,
      agentSessionArtifacts: {
        ...state.agentSessionArtifacts,
        [action.sessionId]: { byId, order, stale: false },
      },
    }
  }
  if (action.type === 'markAgentSessionArtifactsError') {
    // Same no-tight-loop contract as the run surfaces: the failure
    // clears the stale flag and stays VISIBLE on the index (rendered
    // where the index feeds a menu); the next SSE signal re-flags.
    const index = state.agentSessionArtifacts[action.sessionId]
    return {
      ...state,
      agentSessionArtifacts: {
        ...state.agentSessionArtifacts,
        [action.sessionId]: {
          byId: index?.byId ?? {},
          order: index?.order ?? [],
          error: action.message,
          stale: false,
        },
      },
    }
  }
  if (action.type === 'setAgentRunArtifacts') {
    return updateAgentRun(state, action.runId, (run) => {
      const artifacts = { ...run.artifacts }
      const artifactOrder: string[] = []
      for (const wire of action.artifacts) {
        const record = agentArtifactFromWire(wire)
        const existing = artifacts[record.artifactId]
        if (existing?.status === 'writing' && existing.publicationId) {
          // An SSE answer publication owns the body until ready/interrupted.
          // A list request triggered just before `answer.started` may return
          // the already-materialized full artifact; applying it here would
          // replace the incremental Markdown with a one-frame jump.
          artifactOrder.push(record.artifactId)
          continue
        }
        // The list row carries no body — keep a fetched body while it is
        // not older than the row's revision. A body that a publication
        // just streamed is kept as well: the publisher finalizes the SAME
        // string it streamed, only under a newer revision, and the detail
        // fetch (still triggered by `publicationNeedsReconcile`) replaces
        // references and status. Dropping the streamed body here blanked
        // the answer for one round trip at the very moment it settled.
        // A body with a delta gap is NOT the finalized text: it is dropped
        // (the old, honest behaviour) and the detail fetch brings the whole
        // answer.
        const keepBody =
          existing?.contentMarkdown !== undefined
          && existing.publicationGap !== true
          && (
            existing.revision >= record.revision
            || existing.publicationNeedsReconcile === true
          )
        artifacts[record.artifactId] = keepBody
          ? {
            ...record,
            contentMarkdown: existing.contentMarkdown,
            payload: existing.payload,
            refs: existing.refs,
            revisions: existing.revisions,
            publicationNeedsReconcile: existing.publicationNeedsReconcile,
            // The announced labels keep the citations linked until the
            // detail fetch delivers the real references.
            publicationRefLabels: existing.publicationRefLabels,
          }
          : record
        artifactOrder.push(record.artifactId)
      }
      // A list request can start before `answer.started` and complete after
      // the SSE publication already created a local writing artifact. That
      // snapshot legitimately lacks the new id; retain every live publication
      // in render order until a later authoritative row marks it terminal.
      for (const artifactId of run.artifactOrder) {
        const existing = artifacts[artifactId]
        if (
          !artifactOrder.includes(artifactId)
          && existing?.status === 'writing'
          && existing.publicationId
        ) artifactOrder.push(artifactId)
      }
      return { ...run, artifacts, artifactOrder, artifactsStale: false }
    })
  }
  if (action.type === 'setAgentRunArtifactDetail') {
    if (!state.agentRuns[action.runId]) {
      // A delegated child is never a desk record (see the run-listing
      // rule above): its body lands on the session index row the
      // resolver already serves, or is dropped when no row knows it.
      const artifactId = action.artifact.artifact_id
      const hit = Object.entries(state.agentSessionArtifacts).find(
        ([, index]) => index.byId[artifactId],
      )
      if (!hit) return state
      const [sessionId, index] = hit
      return {
        ...state,
        agentSessionArtifacts: {
          ...state.agentSessionArtifacts,
          [sessionId]: {
            ...index,
            byId: {
              ...index.byId,
              [artifactId]: {
                ...index.byId[artifactId],
                record: agentArtifactFromWire(action.artifact),
              },
            },
          },
        },
      }
    }
    return updateAgentRun(state, action.runId, (run) => {
      const record = agentArtifactFromWire(action.artifact)
      const existing = run.artifacts[record.artifactId]
      if (existing?.status === 'writing' && existing.publicationId) return run
      return {
        ...run,
        artifacts: { ...run.artifacts, [record.artifactId]: record },
        artifactOrder: run.artifactOrder.includes(record.artifactId)
          ? run.artifactOrder
          : [...run.artifactOrder, record.artifactId],
      }
    })
  }
  if (action.type === 'setAgentRunPatch') {
    return updateAgentRun(state, action.runId, (run) => ({
      ...run,
      patch: agentPatchFromWire(action.patch),
      patchId: action.patch.patch_id,
      patchStale: false,
    }))
  }
  if (action.type === 'markAgentRunError') {
    // A failed control fetch clears its stale flag: retrying on the next
    // render would hammer the endpoint in a tight loop — the next SSE
    // signal re-flags the surface, which is the honest retry moment.
    return updateAgentRun(state, action.runId, (run) => ({
      ...run,
      error: action.message,
      ...(action.surface === 'plan' ? { planStale: false } : {}),
      ...(action.surface === 'approvals' ? { approvalsStale: false } : {}),
      ...(action.surface === 'clarifications'
        ? { clarificationsStale: false }
        : {}),
      ...(action.surface === 'artifacts' ? { artifactsStale: false } : {}),
      ...(action.surface === 'patch' ? { patchStale: false } : {}),
      ...(action.surface === 'child_gates'
        && action.childRunId
        && run.childGates[action.childRunId]
        ? {
          childGates: {
            ...run.childGates,
            [action.childRunId]: {
              ...run.childGates[action.childRunId],
              stale: false,
            },
          },
        }
        : {}),
    }))
  }
  if (action.type === 'createAgentSession') {
    const exists = state.agentSessions[action.session.id]
    return {
      ...state,
      dirty: true,
      // Creating (and selecting) a new session clears the canvas so a
      // pinned tab from the prior session does not leak (root: the reset
      // is bound to the selection transition, not to one action).
      agentCanvas: agentCanvasForSelection(state, action.session.id),
      agentSessionOrder: exists
        ? state.agentSessionOrder
        : [action.session.id, ...state.agentSessionOrder],
      agentSessions: {
        ...state.agentSessions,
        // A locally created session is fully known — there is no pending
        // detail fetch whose absence should hold back preference seeding.
        [action.session.id]: { ...action.session, metadataHydrated: true },
      },
      ...withSelectedAgentSession(state, action.session.id),
    }
  }
  if (action.type === 'renameAgentSession') {
    const session = state.agentSessions[action.sessionId]
    if (!session) return state
    const title = action.title.trim()
    if (!title || (title === session.title && session.titleSource === 'manual' && !session.titleConflict)) return state
    return {
      ...state,
      dirty: true,
      agentSessions: {
        ...state.agentSessions,
        [action.sessionId]: {
          ...session,
          title,
          titleSource: 'manual', titlePending: true, titleEditId: createId('title-edit'),
          titleRequest: undefined, titleGenerationReason: undefined, titleConflict: undefined,
          updatedAt: new Date().toISOString(),
        },
      },
    }
  }
  if (action.type === 'setAgentSessionKnowledgeSwitch') {
    const session = state.agentSessions[action.sessionId]
    if (!session) return state
    if (session.knowledgeSwitch === action.knowledgeSwitch) return state
    return {
      ...state,
      dirty: true,
      agentSessions: {
        ...state.agentSessions,
        [action.sessionId]: {
          ...session,
          knowledgeSwitch: action.knowledgeSwitch,
          updatedAt: new Date().toISOString(),
        },
      },
    }
  }
  if (action.type === 'setAgentSessionSourcePolicy') {
    const session = state.agentSessions[action.sessionId]
    if (!session) return state
    if (
      session.sourcePolicy?.web === action.sourcePolicy.web
      && session.sourcePolicy?.knowledge === action.sourcePolicy.knowledge
    ) return state
    return {
      ...state,
      dirty: true,
      agentSessions: {
        ...state.agentSessions,
        [action.sessionId]: {
          ...session,
          sourcePolicy: action.sourcePolicy,
          updatedAt: new Date().toISOString(),
        },
      },
    }
  }
  if (action.type === 'deleteAgentSession') {
    const session = state.agentSessions[action.sessionId]
    if (!session && !action.operationId) return state
    const removedRunIds = agentRunIdsForDeletedSession(
      state.agentRuns,
      action.sessionId,
      session?.runIds ?? [],
    )
    const agentRuns = Object.fromEntries(
      Object.entries(state.agentRuns).filter(([runId]) => !removedRunIds.has(runId)),
    )
    const agentSessions = { ...state.agentSessions }
    delete agentSessions[action.sessionId]
    for (const [sessionId, candidate] of Object.entries(agentSessions)) {
      const runIds = candidate.runIds.filter((runId) => !removedRunIds.has(runId))
      if (
        runIds.length === 0
        && (
          candidate.persistable === false
          || removedRunIds.has(sessionId)
          || state.agentRuns[sessionId] !== undefined
        )
      ) {
        delete agentSessions[sessionId]
      } else if (runIds.length !== candidate.runIds.length) {
        agentSessions[sessionId] = { ...candidate, runIds }
      }
    }
    const agentSessionOrder = state.agentSessionOrder.filter((id) => (
      id !== action.sessionId && agentSessions[id] !== undefined
    ))
    const nextSelected =
      state.selectedAgentSessionId === action.sessionId
        ? agentSessionOrder[0] ?? null
        : state.selectedAgentSessionId
    const selection = withSelectedAgentSession(state, nextSelected)
    return {
      ...state,
      dirty: true,
      // Deleting the ACTIVE session reassigns the selection — clear the
      // canvas so it does not keep the deleted session's tabs.
      agentCanvas: agentCanvasForSelection(state, nextSelected),
      agentSessionDeletionReceipts: action.operationId
        ? {
          ...state.agentSessionDeletionReceipts,
          [action.sessionId]: {
            operationId: action.operationId,
            runIds: [...removedRunIds],
          },
        }
        : state.agentSessionDeletionReceipts,
      agentRuns,
      agentSessionOrder,
      agentSessions,
      ...selection,
      ui: {
        ...selection.ui,
        pinnedExplorer: {
          ...state.ui.pinnedExplorer,
          agentSessionIds: state.ui.pinnedExplorer.agentSessionIds.filter(
            (sessionId) => agentSessions[sessionId] !== undefined,
          ),
        },
      },
    }
  }
  if (action.type === 'setAgentSessionDeletionState') {
    const session = state.agentSessions[action.sessionId]
    if (!session) return state
    const nextSelected = state.selectedAgentSessionId === action.sessionId
      ? state.agentSessionOrder.find((id) => (
        id !== action.sessionId && !state.agentSessions[id]?.deletion
      )) ?? null
      : state.selectedAgentSessionId
    const selection = withSelectedAgentSession(state, nextSelected)
    return {
      ...state,
      agentCanvas: agentCanvasForSelection(state, nextSelected),
      agentSessions: {
        ...state.agentSessions,
        [action.sessionId]: { ...session, deletion: action.deletion },
      },
      ...selection,
    }
  }
  if (action.type === 'selectAgentSession') {
    if (action.sessionId && state.agentSessions[action.sessionId]?.deletion) return state
    if (state.selectedAgentSessionId === action.sessionId) return state
    // A session switch clears the canvas (see agentCanvasForSelection).
    return {
      ...state,
      agentCanvas: agentCanvasForSelection(state, action.sessionId),
      ...withSelectedAgentSession(state, action.sessionId),
    }
  }
  if (action.type === 'togglePinnedAgentSession') {
    return {
      ...state,
      dirty: true,
      ui: {
        ...state.ui,
        pinnedExplorer: {
          ...state.ui.pinnedExplorer,
          agentSessionIds: toggleExplorerPin(
            state.ui.pinnedExplorer.agentSessionIds,
            action.sessionId,
          ),
        },
      },
    }
  }
  if (action.type === 'moveAgentSessionToGroup') {
    const session = state.agentSessions[action.sessionId]
    if (!session) return state
    const groupChanged = (session.groupId ?? null) !== action.groupId
    if (!groupChanged) {
      // Same-position no-op guard (chat parity): a threshold-crossing
      // wiggle released on the row's own slot must not dirty the project.
      const pinnedIds = new Set(state.ui.pinnedExplorer.agentSessionIds)
      const groupOf = (id: string) => {
        const candidate = state.agentSessions[id]?.groupId
        return candidate && state.agentSessionGroups[candidate] ? candidate : null
      }
      const sectionIds = state.agentSessionOrder.filter((id) =>
        id !== action.sessionId && !pinnedIds.has(id) && groupOf(id) === action.groupId)
      const currentIds = state.agentSessionOrder.filter((id) =>
        !pinnedIds.has(id) && groupOf(id) === action.groupId)
      const currentIndex = currentIds.indexOf(action.sessionId)
      const boundedTargetIndex = Math.max(0, Math.min(sectionIds.length, action.targetIndex))
      if (currentIndex === boundedTargetIndex) return state
    }
    return {
      ...state,
      dirty: true,
      agentSessionOrder: insertAgentSessionIntoSection(
        state,
        action.sessionId,
        action.groupId,
        action.targetIndex,
      ),
      agentSessions: {
        ...state.agentSessions,
        [action.sessionId]: groupChanged
          ? {
            ...session,
            groupId: action.groupId,
            // The bump flushes the sync fingerprint so the server learns
            // the new group; a pure position change stays local.
            updatedAt: new Date().toISOString(),
          }
          : session,
      },
    }
  }
  if (action.type === 'moveAgentSessionGroup') {
    if (!state.agentSessionGroups[action.groupId]) return state
    const withoutGroup = state.agentSessionGroupOrder.filter((id) => id !== action.groupId)
    const boundedIndex = Math.max(0, Math.min(withoutGroup.length, action.targetIndex))
    if (state.agentSessionGroupOrder[boundedIndex] === action.groupId
      && withoutGroup.length + 1 === state.agentSessionGroupOrder.length) {
      const current = state.agentSessionGroupOrder.indexOf(action.groupId)
      if (current === boundedIndex) return state
    }
    const nextOrder = [...withoutGroup]
    nextOrder.splice(boundedIndex, 0, action.groupId)
    return { ...state, agentSessionGroupOrder: nextOrder, dirty: true }
  }
  if (action.type === 'createAgentSessionGroup') {
    const now = new Date().toISOString()
    const group: AgentSessionGroupRecord = {
      createdAt: now,
      id: createId('agent-session-group'),
      title: action.title.trim() || 'Neuer Ordner',
      updatedAt: now,
    }
    return {
      ...state,
      dirty: true,
      agentSessionGroupOrder: [group.id, ...state.agentSessionGroupOrder],
      agentSessionGroups: {
        ...state.agentSessionGroups,
        [group.id]: group,
      },
    }
  }
  if (action.type === 'renameAgentSessionGroup') {
    const group = state.agentSessionGroups[action.groupId]
    if (!group) return state
    const title = action.title.trim()
    if (!title || title === group.title) return state
    return {
      ...state,
      dirty: true,
      agentSessionGroups: {
        ...state.agentSessionGroups,
        [action.groupId]: {
          ...group,
          title,
          updatedAt: new Date().toISOString(),
        },
      },
    }
  }
  if (action.type === 'deleteAgentSessionGroup') {
    if (!state.agentSessionGroups[action.groupId]) return state
    const agentSessionGroups = { ...state.agentSessionGroups }
    delete agentSessionGroups[action.groupId]
    const agentSessions = { ...state.agentSessions }
    let sessionsChanged = false
    for (const [id, session] of Object.entries(agentSessions)) {
      if (session.groupId === action.groupId) {
        agentSessions[id] = { ...session, groupId: null }
        sessionsChanged = true
      }
    }
    return {
      ...state,
      dirty: true,
      agentSessionGroupOrder: state.agentSessionGroupOrder.filter(
        (id) => id !== action.groupId,
      ),
      agentSessionGroups,
      agentSessions: sessionsChanged ? agentSessions : state.agentSessions,
    }
  }
  if (action.type === 'upsertServerAgentSessions') {
    // Server hydrate: additive, local-newer-wins, NEVER dirty (re-save loop).
    if (action.sessions.length === 0 && action.groups.length === 0) {
      return state
    }
    const agentSessions = { ...state.agentSessions }
    let agentSessionOrder = state.agentSessionOrder
    for (const wire of action.sessions) {
      if (state.agentSessionDeletionReceipts?.[wire.id]) continue
      const existing = agentSessions[wire.id]
      const serverUpdatedAt = new Date(wire.updated_at * 1000).toISOString()
      const deletion = sessionDeletionFromWire(wire)
      if (!existing) {
        const metadata = agentSessionMetadataFromJson(wire.items_json)
        agentSessions[wire.id] = {
          id: wire.id,
          ...titleFromWire(wire),
          groupId: wire.group_id,
          createdAt: new Date(wire.created_at * 1000).toISOString(),
          updatedAt: serverUpdatedAt,
          runIds: [],
          sourcePolicy: metadata.sourcePolicy,
          knowledgeSwitch: metadata.knowledgeSwitch,
          modelSelection: metadata.modelSelection ?? undefined,
          // Only a response that carried items_json proves the stored pick
          // is actually known.
          metadataHydrated: wire.items_json !== undefined,
          persistable: true,
          ...(deletion ? { deletion } : {}),
        }
        if (!agentSessionOrder.includes(wire.id)) {
          agentSessionOrder = [...agentSessionOrder, wire.id]
        }
      } else if (deletion || existing.updatedAt <= serverUpdatedAt) {
        const metadata = wire.items_json === undefined
          ? null
          : agentSessionMetadataFromJson(wire.items_json)
        agentSessions[wire.id] = {
          ...existing,
          title: wire.title,
          groupId: wire.group_id,
          updatedAt: serverUpdatedAt,
          sourcePolicy:
            metadata?.sourcePolicy
            ?? existing.sourcePolicy
            ?? { ...DEFAULT_AGENT_SOURCE_POLICY },
          // Same local-newer-wins rule as the policy: only a row that
          // actually carried metadata may replace the local pick.
          modelSelection:
            metadata === null
              ? existing.modelSelection
              : metadata.modelSelection ?? undefined,
          knowledgeSwitch:
            metadata === null ? existing.knowledgeSwitch : metadata.knowledgeSwitch,
          metadataHydrated:
            wire.items_json !== undefined ? true : existing.metadataHydrated,
          persistable: true,
          ...(deletion ? { deletion } : { deletion: undefined }),
        }
      } else if (existing.persistable === false) {
        // A real server session wins over an identically keyed derived shell.
        agentSessions[wire.id] = { ...existing, persistable: true }
      }
      if (existing && wire.title_revision !== undefined
        && (existing.titleRevision !== undefined || existing.updatedAt <= serverUpdatedAt)) {
        agentSessions[wire.id] = { ...agentSessions[wire.id], ...reconcileTitle(existing, wire) }
      }
    }
    const agentSessionGroups = { ...state.agentSessionGroups }
    let agentSessionGroupOrder = state.agentSessionGroupOrder
    for (const wire of action.groups) {
      agentSessionGroups[wire.id] = {
        id: wire.id,
        title: wire.title,
        createdAt: new Date(wire.created_at * 1000).toISOString(),
        updatedAt: new Date(wire.updated_at * 1000).toISOString(),
      }
      if (!agentSessionGroupOrder.includes(wire.id)) {
        agentSessionGroupOrder = [...agentSessionGroupOrder, wire.id]
      }
    }
    const selectSessionId = action.selectSessionId
      && agentSessions[action.selectSessionId]
      && !agentSessions[action.selectSessionId].deletion
      ? action.selectSessionId
      : null
    const selection = selectSessionId
      ? withSelectedAgentSession(state, selectSessionId, agentSessions)
      : null
    // Project a freshly hydrated stored pick into an UNTOUCHED working value.
    // A value the user (or a prior projection) already set stays: a fresh
    // user pick made while the detail fetch was in flight must win.
    const baseUi = selection ? selection.ui : state.ui
    const activeId = selection ? selectSessionId : baseUi.selectedAgentSessionId
    const storedPick = activeId ? agentSessions[activeId]?.modelSelection : undefined
    const uiUntouched
      = baseUi.selectedAgentModelTier === null && baseUi.selectedAgentModel === null
    const ui = storedPick && uiUntouched
      ? {
        ...baseUi,
        selectedAgentEffort: storedPick.effort,
        selectedAgentModel: storedPick.model,
        selectedAgentModelTier: storedPick.tier,
      }
      : baseUi
    return {
      ...state,
      ...(selection ?? {}),
      ...(selection
        ? { agentCanvas: agentCanvasForSelection(state, selectSessionId) }
        : {}),
      agentSessionGroupOrder,
      agentSessionGroups,
      agentSessionOrder,
      agentSessions,
      ui,
    }
  }
  if (action.type === 'setAgentPlanDraft') {
    const drafts = { ...state.agentPlanDrafts }
    if (action.draft === null) delete drafts[action.runId]
    else drafts[action.runId] = action.draft
    return { ...state, agentPlanDrafts: drafts }
  }
  if (action.type === 'openAgentCanvasView') {
    // Tab semantics live in the pure helpers (features/canvas/tabs.ts):
    // user opens create/focus pinned tabs, agent opens drive the one
    // preview tab under the follow rules.
    const next = openCanvasTab(
      state.agentCanvas,
      action.descriptor,
      action.source,
    )
    if (next === state.agentCanvas) return state
    return { ...state, agentCanvas: next }
  }
  if (action.type === 'activateAgentCanvasTab') {
    const next = activateCanvasTab(state.agentCanvas, action.key)
    if (next === state.agentCanvas) return state
    return { ...state, agentCanvas: next }
  }
  if (action.type === 'closeAgentCanvasTab') {
    const next = closeCanvasTab(state.agentCanvas, action.key)
    if (next === state.agentCanvas) return state
    return { ...state, agentCanvas: next }
  }
  if (action.type === 'pinAgentCanvasTab') {
    const next = pinCanvasTab(state.agentCanvas, action.key)
    if (next === state.agentCanvas) return state
    return { ...state, agentCanvas: next }
  }
  if (action.type === 'setAgentCanvasPinned') {
    if (state.agentCanvas.pinned === action.pinned) return state
    return {
      ...state,
      agentCanvas: { ...state.agentCanvas, pinned: action.pinned },
    }
  }
  if (action.type === 'toggleAgentCanvasFocus') {
    if (!state.agentCanvas.open) return state
    return {
      ...state,
      agentCanvas: {
        ...state.agentCanvas,
        focus: !state.agentCanvas.focus,
      },
    }
  }
  if (action.type === 'closeAgentCanvas') {
    // Tabs survive a panel close: reopening restores the row as left.
    if (!state.agentCanvas.open) return state
    return {
      ...state,
      agentCanvas: {
        ...state.agentCanvas,
        open: false,
        focus: false,
        pinned: false,
      },
    }
  }
  if (action.type === 'toggleAgentSessionsVisible') {
    return {
      ...state,
      ui: {
        ...state.ui,
        isAgentSessionsVisible: !state.ui.isAgentSessionsVisible,
      },
    }
  }
  if (action.type === 'upsertServerChatThreads') {
    // Hydrate thread METADATA from the server. Additive + local-
    // newer-wins (never clobber unpushed local edits) + NEVER sets dirty
    // (server-pushed state must not trigger a re-save loop). Messages are
    // loaded separately on thread open; a freshly hydrated thread starts
    // with an empty message list.
    if (action.threads.length === 0) return state
    const chatThreads = { ...state.chatThreads }
    const chatThreadGroupMemberships = { ...state.chatThreadGroupMemberships }
    const newIds: string[] = []
    for (const incoming of action.threads) {
      const local = chatThreads[incoming.id]
      if (local) {
        if (incoming.updatedAt > local.updatedAt) {
          chatThreads[incoming.id] = {
            ...local,
            title: incoming.title,
            preview: incoming.preview,
            source: incoming.source,
            createdAt: incoming.createdAt,
            updatedAt: incoming.updatedAt,
            // Server-newer-wins like the fields above; an absent value on a
            // newer row means the pick was cleared elsewhere.
            modelSelection: incoming.modelSelection,
          }
          chatThreadGroupMemberships[incoming.id] =
            action.memberships[incoming.id] ?? null
        }
        if (incoming.titleRevision !== undefined
          && (local.titleRevision !== undefined || incoming.updatedAt >= local.updatedAt)) {
          chatThreads[incoming.id] = { ...chatThreads[incoming.id], ...reconcileTitle(local, titleWire(incoming)) }
        }
      } else {
        chatThreads[incoming.id] = { ...incoming, messages: [] }
        chatThreadGroupMemberships[incoming.id] =
          action.memberships[incoming.id] ?? null
        newIds.push(incoming.id)
      }
    }
    // Preserve the SERVER's keyset order (created_at desc) within each batch:
    // newIds already follows action.threads, which the hook fills straight from
    // the server page. Re-sorting THIS ARRAY by another key would make the
    // merged list inconsistent across page boundaries -- the server paginates by
    // created_at, so keeping its order makes chatThreadOrder a faithful prefix
    // of the canonical pagination across every load-more. DISPLAY recency is a
    // separate concern: projectChatHistorySections sorts the RENDERED sections
    // by activity in the automatic modes while this array stays the untouched
    // pagination prefix. Honest limit: recency is ranked
    // within the loaded created_at window only -- an old thread that became
    // active again is not in the window until 'load older' reaches it.
    // Page-1 / newest hydrate prepends; an older load-more page appends to the
    // end so the displayed order stays newest-first across paginated loads.
    const chatThreadOrder = newIds.length === 0
      ? state.chatThreadOrder
      : action.append
        ? [...state.chatThreadOrder, ...newIds]
        : [...newIds, ...state.chatThreadOrder]
    // Project a freshly hydrated stored pick into an UNTOUCHED working value
    // (the agent-hydrate rule): a pick made while the page was loading wins.
    const activeThread = state.ui.selectedChatThreadId
      ? chatThreads[state.ui.selectedChatThreadId]
      : undefined
    const storedPick = activeThread?.modelSelection
    const uiUntouched
      = state.ui.selectedChatModelTier === null && state.ui.selectedChatModel === null
    const ui = storedPick && uiUntouched
      ? {
        ...state.ui,
        selectedChatEffort: storedPick.effort,
        selectedChatModel: storedPick.model,
        selectedChatModelTier: storedPick.tier,
      }
      : state.ui
    return {
      ...state,
      chatThreadGroupMemberships,
      chatThreadOrder,
      chatThreads,
      ui,
    }
  }
  if (action.type === 'upsertServerChatMessages') {
    // Load-on-open: fill a thread's messages from the server, merged by id
    // and ordered chronologically. Never sets dirty. Preview stays as the
    // metadata-hydrated value.
    const thread = state.chatThreads[action.threadId]
    if (!thread || action.messages.length === 0) return state
    const byId = new Map(thread.messages.map((message) => [message.id, message]))
    for (const message of action.messages) byId.set(message.id, message)
    const messages = [...byId.values()].sort(compareChatMessagesChronologically)
    return {
      ...state,
      chatThreads: {
        ...state.chatThreads,
        [action.threadId]: { ...thread, messages },
      },
    }
  }
  if (action.type === 'upsertServerChatThreadGroups') {
    if (action.groups.length === 0) return state
    const chatThreadGroups = { ...state.chatThreadGroups }
    const newIds: string[] = []
    for (const incoming of action.groups) {
      const local = chatThreadGroups[incoming.id]
      if (local) {
        if (incoming.updatedAt > local.updatedAt) chatThreadGroups[incoming.id] = incoming
      } else {
        chatThreadGroups[incoming.id] = incoming
        newIds.push(incoming.id)
      }
    }
    const sortedNew = newIds.sort((a, b) =>
      chatThreadGroups[b].updatedAt.localeCompare(chatThreadGroups[a].updatedAt),
    )
    const chatThreadGroupOrder = sortedNew.length > 0
      ? [...sortedNew, ...state.chatThreadGroupOrder]
      : state.chatThreadGroupOrder
    return { ...state, chatThreadGroupOrder, chatThreadGroups }
  }
  if (action.type === 'setServerSyncEnabled') {
    if (state.serverSyncEnabled === action.enabled) return state
    // A genuine user opt-in (persistLocal omitted/true) marks the project dirty
    // so the flag persists to the local manifest. The AUTO path
    // (persistLocal:false) derives the flag from the authenticated session on
    // every boot, so it must NOT dirty the project (nothing local to save, and
    // dirtying would prompt a spurious "unsaved changes" save loop).
    const dirty = action.persistLocal === false ? state.dirty : true
    return { ...state, dirty, serverSyncEnabled: action.enabled }
  }
  if (action.type === 'reconcileServerEditorDocuments') {
    // The paginated scope=all response is authoritative for remote access.
    // A record observed or acknowledged by the server must not survive a
    // missing authoritative row. Never-confirmed local drafts carry no server
    // provenance and remain available as offline work.
    const authoritativeIds = new Set(action.documents.map((document) => document.id))
    const retiredDocumentIds = new Set([
      ...(action.retiredDocumentIds ?? []),
      ...Object.values(state.editorDocuments)
        .filter((document) => (
          (document.serverSynced === true || document.access?.mode === 'shared')
          && !authoritativeIds.has(document.id)
        ))
        .map((document) => document.id),
    ])
    const recoveryCaptures = new Map(
      (action.recoveryCaptures ?? []).map((capture) => [
        capture.documentId,
        capture,
      ]),
    )
    const recoveryByOriginalId = new Map<string, EditorDocumentRecord>()
    for (const documentId of retiredDocumentIds) {
      const source = state.editorDocuments[documentId]
      const capture = recoveryCaptures.get(documentId)
      if (!source || !capture) continue
      recoveryByOriginalId.set(
        documentId,
        createEditorRecoveryDocument(source, capture, 'remote_deleted'),
      )
    }
    const editorDocuments: Record<string, EditorDocumentRecord> = Object.fromEntries(
      Object.entries(state.editorDocuments)
        .filter(([documentId]) => !retiredDocumentIds.has(documentId)),
    )
    for (const recovery of recoveryByOriginalId.values()) {
      editorDocuments[recovery.id] = recovery
    }
    const retainedDocumentOrder = state.editorDocumentOrder.flatMap((documentId) => {
      if (!retiredDocumentIds.has(documentId)) return [documentId]
      const recovery = recoveryByOriginalId.get(documentId)
      return recovery ? [recovery.id] : []
    })
    for (const recovery of recoveryByOriginalId.values()) {
      if (!retainedDocumentOrder.includes(recovery.id)) {
        retainedDocumentOrder.unshift(recovery.id)
      }
    }
    const newIds: string[] = []
    for (const incoming of action.documents) {
      const local = editorDocuments[incoming.id]
      if (local) {
        editorDocuments[incoming.id] = mergeServerEditorDocumentMetadata(
          local,
          incoming,
        )
      } else {
        editorDocuments[incoming.id] = {
          ...incoming,
          serverSynced: true,
        }
        newIds.push(incoming.id)
      }
    }
    const sortedNew = newIds.sort((a, b) =>
      editorDocuments[b].updatedAt.localeCompare(editorDocuments[a].updatedAt),
    )
    const editorDocumentOrder = sortedNew.length > 0
      ? [...sortedNew, ...retainedDocumentOrder]
      : retainedDocumentOrder
    const editorComments: Record<string, EditorCommentThreadRecord> = Object.fromEntries(
      Object.entries(state.editorComments).filter(([, comment]) => (
        !retiredDocumentIds.has(comment.documentId)
      )),
    )
    const recoveryCommentIdByOriginalId = new Map<string, string>()
    for (const [commentId, outboxEntry] of Object.entries(
      state.editorCommentOutbox ?? {},
    )) {
      if (outboxEntry.operation !== 'upsert') continue
      const recovery = recoveryByOriginalId.get(outboxEntry.documentId)
      const comment = state.editorComments[commentId]
      if (!recovery || !comment) continue
      const recoveryCommentId = createId('editor-comment')
      recoveryCommentIdByOriginalId.set(commentId, recoveryCommentId)
      const { suggestionDraft, ...localComment } = comment
      void suggestionDraft
      editorComments[recoveryCommentId] = {
        ...localComment,
        documentId: recovery.id,
        id: recoveryCommentId,
      }
    }
    const editorCommentOutbox = Object.fromEntries(
      Object.entries(state.editorCommentOutbox ?? {}).filter(([, entry]) => (
        !retiredDocumentIds.has(entry.documentId)
      )),
    )
    const editorSuggestionGroups: Record<string, EditorSuggestionGroupRecord> = Object.fromEntries(
      Object.entries(state.editorSuggestionGroups).filter(([, group]) => (
        !retiredDocumentIds.has(group.documentId)
      )),
    )
    const editorSuggestions: Record<string, EditorSuggestionRecord> = Object.fromEntries(
      Object.entries(state.editorSuggestions).filter(([, suggestion]) => (
        !retiredDocumentIds.has(suggestion.documentId)
      )),
    )
    const recoveryGroupIdByOriginalId = new Map<string, string>()
    for (const suggestion of Object.values(state.editorSuggestions)) {
      if (suggestion.status !== 'pending') continue
      const recovery = recoveryByOriginalId.get(suggestion.documentId)
      const sourceGroup = state.editorSuggestionGroups[suggestion.groupId]
      if (!recovery || !sourceGroup) continue
      const origin = remapRecoverySuggestionOrigin(
        suggestion.origin,
        recoveryCommentIdByOriginalId,
      )
      const groupOrigin = remapRecoverySuggestionOrigin(
        sourceGroup.origin,
        recoveryCommentIdByOriginalId,
      )
      if (!origin || !groupOrigin) continue
      let recoveryGroupId = recoveryGroupIdByOriginalId.get(sourceGroup.id)
      if (!recoveryGroupId) {
        recoveryGroupId = createId('editor-suggestion-group')
        recoveryGroupIdByOriginalId.set(sourceGroup.id, recoveryGroupId)
        editorSuggestionGroups[recoveryGroupId] = {
          ...sourceGroup,
          documentId: recovery.id,
          id: recoveryGroupId,
          origin: groupOrigin,
        }
      }
      const recoverySuggestionId = createId('editor-suggestion')
      const localSuggestion = { ...suggestion }
      delete localSuggestion.collaborationPublication
      delete localSuggestion.privateDraft
      editorSuggestions[recoverySuggestionId] = {
        ...localSuggestion,
        documentId: recovery.id,
        groupId: recoveryGroupId,
        id: recoverySuggestionId,
        origin,
      }
    }
    const openDocumentIds = state.editorUi.openDocumentIds.flatMap((documentId) => {
      if (!retiredDocumentIds.has(documentId)) return [documentId]
      const recovery = recoveryByOriginalId.get(documentId)
      return recovery ? [recovery.id] : []
    })
    const previousActiveDocumentId = state.editorUi.activeDocumentId
    const activeRecovery = previousActiveDocumentId
      ? recoveryByOriginalId.get(previousActiveDocumentId)
      : undefined
    const activeDocumentId = (
      previousActiveDocumentId
      && !retiredDocumentIds.has(previousActiveDocumentId)
    )
      ? previousActiveDocumentId
      : activeRecovery?.id
        ?? openDocumentIds[openDocumentIds.length - 1]
        ?? editorDocumentOrder[0]
        ?? null
    const selectedCommentId = state.editorUi.selectedCommentId
      ? recoveryCommentIdByOriginalId.get(state.editorUi.selectedCommentId)
        ?? (
          editorComments[state.editorUi.selectedCommentId]
            ? state.editorUi.selectedCommentId
            : null
        )
      : null
    return {
      ...state,
      dirty: recoveryByOriginalId.size > 0 ? true : state.dirty,
      editorCommentOutbox,
      editorComments,
      editorDocumentOrder,
      editorDocuments,
      editorSuggestionGroups,
      editorSuggestions,
      editorUi: {
        ...state.editorUi,
        activeDocumentId,
        openDocumentIds: activeDocumentId
          ? addOpenEditorDocumentId(openDocumentIds, activeDocumentId)
          : openDocumentIds,
        selectedCommentId,
      },
      ui: {
        ...state.ui,
        pinnedExplorer: {
          ...state.ui.pinnedExplorer,
          editorDocumentIds: state.ui.pinnedExplorer.editorDocumentIds
            .filter((documentId) => !retiredDocumentIds.has(documentId)),
        },
      },
    }
  }
  if (action.type === 'upsertServerEditorDocuments') {
    // Hydrate document METADATA from the server. Additive + local-
    // newer-wins + KEEPS the local body (the body loads separately via
    // setServerEditorDocumentBody on open) + never sets dirty.
    if (action.documents.length === 0) return state
    const editorDocuments = { ...state.editorDocuments }
    const newIds: string[] = []
    for (const incoming of action.documents) {
      const local = editorDocuments[incoming.id]
      if (local) {
        editorDocuments[incoming.id] = mergeServerEditorDocumentMetadata(
          local,
          incoming,
        )
      } else {
        editorDocuments[incoming.id] = {
          ...incoming,
          serverSynced: true,
        }
        newIds.push(incoming.id)
      }
    }
    const sortedNew = newIds.sort((a, b) =>
      editorDocuments[b].updatedAt.localeCompare(editorDocuments[a].updatedAt),
    )
    const editorDocumentOrder = sortedNew.length > 0
      ? [...sortedNew, ...state.editorDocumentOrder]
      : state.editorDocumentOrder
    return { ...state, editorDocumentOrder, editorDocuments }
  }
  if (action.type === 'activateEditorDocumentCollaboration') {
    const document = state.editorDocuments[action.documentId]
    if (!document) return state
    const metadataRevision = Number.isSafeInteger(action.metadataRevision)
      ? Math.max(document.metadataRevision ?? 0, action.metadataRevision)
      : document.metadataRevision
    return {
      ...state,
      editorDocuments: {
        ...state.editorDocuments,
        [document.id]: {
          ...document,
          collaboration: action.collaboration,
          contentMode: 'collaboration',
          ...(metadataRevision === undefined ? {} : { metadataRevision }),
          serverSynced: true,
        },
      },
    }
  }
  if (action.type === 'setServerEditorDocumentBody') {
    // Load-on-open: fill a document's body from the server. Never changes
    // updatedAt (so the autosave does not read it back as a local edit)
    // and never sets dirty.
    const document = state.editorDocuments[action.documentId]
    if (!document) return state
    return {
      ...state,
      editorDocuments: {
        ...state.editorDocuments,
        [action.documentId]: {
          ...document,
          contentMarkdown: action.contentMarkdown,
        },
      },
    }
  }
  if (action.type === 'adoptEditorDocumentRevision') {
    // A successful save advanced the server to this revision; adopt it as the
    // new base. Revision-only — never touches content/updatedAt/dirty, so a
    // live keystroke made during the save keeps its newer body and the flush
    // fingerprint (updatedAt) stays put. No-op if the revision did not move
    // forward (a late/duplicate adopt must not rewind a fresher base).
    const document = state.editorDocuments[action.documentId]
    if (
      !document
      || action.revision < document.revision
      || (
        action.revision === document.revision
        && document.serverSynced === true
      )
    ) return state
    return {
      ...state,
      editorDocuments: {
        ...state.editorDocuments,
        [action.documentId]: {
          ...document,
          revision: Math.max(document.revision, action.revision),
          serverSynced: true,
        },
      },
    }
  }
  if (action.type === 'adoptEditorDocumentMetadataRevision') {
    const document = state.editorDocuments[action.documentId]
    const currentRevision = document?.metadataRevision ?? 0
    if (
      !document
      || !Number.isSafeInteger(action.metadataRevision)
      || action.metadataRevision < currentRevision
      || (
        action.metadataRevision === currentRevision
        && document.serverSynced === true
      )
    ) return state
    return {
      ...state,
      editorDocuments: {
        ...state.editorDocuments,
        [action.documentId]: {
          ...document,
          metadataRevision: Math.max(currentRevision, action.metadataRevision),
          serverSynced: true,
        },
      },
    }
  }
  if (action.type === 'rebaseServerEditorDocument') {
    // Autosave lost the server's revision guard (a concurrent writer —
    // typically an agent patch apply — advanced the document). Two cases,
    // distinguished by whether the user kept typing during the PUT->GET
    // window (pushedContentMarkdown is what the failed PUT carried):
    const document = state.editorDocuments[action.documentId]
    if (!document) return state
    if (document.contentMarkdown === action.pushedContentMarkdown) {
      // No local edit since the failed push: adopt the server body +
      // revision. Never touches updatedAt / dirty (mirror of
      // setServerEditorDocumentBody) — this window is now in sync.
      return {
        ...state,
        editorDocuments: {
          ...state.editorDocuments,
          [action.documentId]: {
            ...document,
            contentMarkdown: action.contentMarkdown,
            revision: action.revision,
            serverSynced: true,
          },
        },
      }
    }
    // The user typed during the window: those keystrokes are a genuinely
    // newer local edit. KEEP them and rebase the revision onto the server
    // (the base), so the next flush re-pushes the live edit as base+1 — the
    // interactive user wins over the concurrent writer for this narrow
    // overlap, and nothing is silently discarded.
    return {
      ...state,
      dirty: true,
      editorDocuments: {
        ...state.editorDocuments,
        [action.documentId]: {
          ...document,
          revision: action.revision,
          serverSynced: true,
        },
      },
    }
  }
  if (action.type === 'upsertServerEditorFolders') {
    if (action.folders.length === 0) return state
    const editorFolders = { ...state.editorFolders }
    const newIds: string[] = []
    for (const incoming of action.folders) {
      const local = editorFolders[incoming.id]
      if (local) {
        if (incoming.updatedAt > local.updatedAt) editorFolders[incoming.id] = incoming
      } else {
        editorFolders[incoming.id] = incoming
        newIds.push(incoming.id)
      }
    }
    const sortedNew = newIds.sort((a, b) =>
      editorFolders[b].updatedAt.localeCompare(editorFolders[a].updatedAt),
    )
    const editorFolderOrder = sortedNew.length > 0
      ? [...sortedNew, ...state.editorFolderOrder]
      : state.editorFolderOrder
    return { ...state, editorFolderOrder, editorFolders }
  }
  if (action.type === 'setServerEditorDocumentDetail') {
    const existing = state.editorDocuments[action.document.id]
    if (!existing) return state
    return {
      ...state,
      editorDocuments: {
        ...state.editorDocuments,
        [action.document.id]: action.document,
      },
    }
  }
  if (action.type === 'upsertServerEditorComments') {
    // Load-on-open: merge a document's comments from the server, by id,
    // local-newer-wins, never dirty. (Comments have no order array — the
    // editor selectors sort them by anchor position.)
    if (action.comments.length === 0) return state
    const editorComments = { ...state.editorComments }
    for (const incoming of action.comments) {
      const local = editorComments[incoming.id]
      if (!local || incoming.updatedAt > local.updatedAt) {
        editorComments[incoming.id] = incoming
      }
    }
    return reconcilePrivateSuggestionDraftRecords(
      { ...state, editorComments },
      action.comments,
    )
  }
  if (action.type === 'reconcileServerEditorComments') {
    const preserveCommentIds = new Set(action.preserveCommentIds)
    const incomingById = new Map(
      action.comments
        .filter((comment) => comment.documentId === action.documentId)
        .map((comment) => [comment.id, comment]),
    )
    const editorComments = { ...state.editorComments }
    const removedCommentIds = new Set<string>()
    for (const comment of Object.values(state.editorComments)) {
      if (
        comment.documentId !== action.documentId
        || preserveCommentIds.has(comment.id)
      ) continue
      if (!incomingById.has(comment.id)) {
        delete editorComments[comment.id]
        removedCommentIds.add(comment.id)
      }
    }
    for (const incoming of incomingById.values()) {
      if (preserveCommentIds.has(incoming.id) && editorComments[incoming.id]) continue
      editorComments[incoming.id] = incoming
    }
    const editorSuggestions = removedCommentIds.size === 0
      ? state.editorSuggestions
      : Object.fromEntries(Object.entries(state.editorSuggestions).filter(([, suggestion]) => (
          !suggestion.origin.commentId
          || !removedCommentIds.has(suggestion.origin.commentId)
        )))
    return reconcilePrivateSuggestionDraftRecords({
      ...state,
      editorComments,
      editorSuggestions,
      editorUi: state.editorUi.selectedCommentId
        && removedCommentIds.has(state.editorUi.selectedCommentId)
        ? { ...state.editorUi, selectedCommentId: null }
        : state.editorUi,
    }, action.comments)
  }
  if (action.type === 'upsertServerAssetSections') {
    // Hydrate file-library section metadata. Additive + local-newer-
    // wins + never dirty. Mirror of upsertServerEditorFolders.
    if (action.sections.length === 0) return state
    const fileLibrarySections = { ...state.fileLibrarySections }
    const newIds: string[] = []
    for (const incoming of action.sections) {
      const local = fileLibrarySections[incoming.id]
      if (local) {
        if (incoming.updatedAt > local.updatedAt) fileLibrarySections[incoming.id] = incoming
        else if (incoming.serverSynced === true && local.serverSynced !== true) {
          fileLibrarySections[incoming.id] = { ...local, serverSynced: true }
        }
      } else {
        fileLibrarySections[incoming.id] = incoming
        newIds.push(incoming.id)
      }
    }
    const sortedNew = newIds.sort((a, b) =>
      fileLibrarySections[b].updatedAt.localeCompare(fileLibrarySections[a].updatedAt),
    )
    const fileLibrarySectionOrder = sortedNew.length > 0
      ? [...sortedNew, ...state.fileLibrarySectionOrder]
      : state.fileLibrarySectionOrder
    return { ...state, fileLibrarySectionOrder, fileLibrarySections }
  }
  if (action.type === 'markFileLibrarySectionServerSynced') {
    const section = state.fileLibrarySections[action.sectionId]
    if (!section || section.serverSynced === true) return state
    return {
      ...state,
      fileLibrarySections: {
        ...state.fileLibrarySections,
        [section.id]: { ...section, serverSynced: true },
      },
    }
  }
  if (action.type === 'pruneLocalBootstrapFileSections') {
    // A fresh client state contains three prepared sections with new opaque
    // IDs. Once this owner/workspace already has server sections, those local
    // placeholders must not be autosaved as another pair on every reload.
    // Keep anything the user has touched/referenced, and retain a temporary
    // section if the server scope has none so the upload invariant survives.
    const serverIds = new Set(action.serverIds)
    const hiddenServerIds = new Set(action.hiddenServerIds)
    const referencedIds = new Set([
      ...Object.values(state.fileGroups).map((group) => group.sectionId),
      ...Object.values(state.fileAssets).map((asset) => asset.sectionId),
    ])
    const keep = (sectionId: string) => {
      const section = state.fileLibrarySections[sectionId]
      if (!section) return false
      if (
        hiddenServerIds.has(sectionId)
        && !referencedIds.has(sectionId)
        && isPristineDefaultFileSection(section)
      ) return false
      if (
        serverIds.has(sectionId)
        || referencedIds.has(sectionId)
        || section.isBootstrapPlaceholder !== true
      ) return true
      return section.kind === 'temporary' && !action.serverHasTemporarySection
    }
    const removedIds = state.fileLibrarySectionOrder.filter((id) => !keep(id))
    if (removedIds.length === 0) return state
    const fileLibrarySections = { ...state.fileLibrarySections }
    for (const id of removedIds) delete fileLibrarySections[id]
    return {
      ...state,
      fileLibrarySectionOrder: state.fileLibrarySectionOrder.filter(keep),
      fileLibrarySections,
    }
  }
  if (action.type === 'upsertServerAssetGroups') {
    if (action.groups.length === 0) return state
    const fileGroups = { ...state.fileGroups }
    const newIds: string[] = []
    for (const incoming of action.groups) {
      const local = fileGroups[incoming.id]
      if (local) {
        if (incoming.updatedAt > local.updatedAt) {
          fileGroups[incoming.id] = {
            ...incoming,
            deletionError: local.deletionError,
            deletionOperationId: local.deletionOperationId,
            deletionStage: local.deletionStage,
            lifecycleStatus: local.lifecycleStatus,
          }
        } else if (incoming.serverSynced === true && local.serverSynced !== true) {
          fileGroups[incoming.id] = {
            ...local,
            serverSynced: true,
          }
        }
      } else {
        fileGroups[incoming.id] = incoming
        newIds.push(incoming.id)
      }
    }
    const sortedNew = newIds.sort((a, b) =>
      fileGroups[b].updatedAt.localeCompare(fileGroups[a].updatedAt),
    )
    const fileGroupOrder = sortedNew.length > 0
      ? [...sortedNew, ...state.fileGroupOrder]
      : state.fileGroupOrder
    return { ...state, fileGroupOrder, fileGroups }
  }
  if (action.type === 'markFileGroupServerSynced') {
    const group = state.fileGroups[action.groupId]
    if (!group || group.serverSynced === true) return state
    return {
      ...state,
      fileGroups: {
        ...state.fileGroups,
        [group.id]: { ...group, serverSynced: true },
      },
    }
  }
  if (action.type === 'upsertServerAssetMetadata') {
    // Hydrate asset METADATA from the server. Additive + local-newer-
    // wins + KEEPS the local extractedText (the body loads separately via
    // setServerAssetBody on use) + never sets dirty. Mirror of
    // upsertServerEditorDocuments.
    if (action.assets.length === 0) return state
    const fileAssets = { ...state.fileAssets }
    const newIds: string[] = []
    for (const incoming of action.assets) {
      const local = fileAssets[incoming.id]
      if (local) {
        if (incoming.updatedAt > local.updatedAt) {
          const samePreparedBody = Boolean(
            incoming.preparedContentHash
            && incoming.preparedContentHash === local.preparedContentHash,
          )
          fileAssets[incoming.id] = {
            ...incoming,
            extractedText: local.extractedText,
            preparedText: incoming.preparedText?.trim()
              ? incoming.preparedText
              : samePreparedBody
                ? local.preparedText
                : '',
          }
        } else if (
          incoming.lifecycleStatus !== local.lifecycleStatus
          || incoming.deletionOperationId !== local.deletionOperationId
          || incoming.deletionStage !== local.deletionStage
          || incoming.deletionError !== local.deletionError
          || incoming.uploadPending !== local.uploadPending
          || incoming.uploadError !== local.uploadError
          || incoming.uploadStatus !== local.uploadStatus
          || incoming.uploadOperationId !== local.uploadOperationId
          || incoming.serverFileId !== local.serverFileId
          || incoming.preparedParserId !== local.preparedParserId
          || incoming.preparedContentHash !== local.preparedContentHash
          || incoming.preparedAt !== local.preparedAt
          || (incoming.serverSynced === true && local.serverSynced !== true)
          || (
            incoming.uploadStatus === 'ready'
            && Boolean(incoming.parserId)
            && incoming.parserId !== 'client'
            && (
              incoming.parserId !== local.parserId
              || incoming.parseStatus !== local.parseStatus
              || incoming.parseWarning !== local.parseWarning
              || incoming.textTruncated !== local.textTruncated
            )
          )
        ) {
          // Destructive lifecycle fields are server-owned and must converge
          // even when the local user edited ordinary metadata more recently.
          // Otherwise a second tab can keep rendering an active row and never
          // resume polling a durable deletion operation.
          fileAssets[incoming.id] = {
            ...local,
            deletionError: incoming.deletionError,
            deletionOperationId: incoming.deletionOperationId,
            deletionStage: incoming.deletionStage,
            lifecycleStatus: incoming.lifecycleStatus,
            uploadError: incoming.uploadError,
            uploadOperationId: incoming.uploadOperationId,
            uploadPending: incoming.uploadPending,
            uploadStatus: incoming.uploadStatus,
            serverFileId: incoming.serverFileId,
            serverSynced: incoming.serverSynced === true || local.serverSynced === true,
            preparedAt: incoming.preparedAt,
            preparedContentHash: incoming.preparedContentHash,
            preparedParserId: incoming.preparedParserId,
            preparedText: incoming.preparedText?.trim()
              ? incoming.preparedText
              : incoming.preparedContentHash
                  && incoming.preparedContentHash === local.preparedContentHash
                ? local.preparedText
                : '',
            ...(
              incoming.uploadStatus === 'ready'
              && Boolean(incoming.parserId)
              && incoming.parserId !== 'client'
                ? {
                    parserId: incoming.parserId,
                    parseStatus: incoming.parseStatus,
                    parseWarning: incoming.parseWarning,
                    textTruncated: incoming.textTruncated,
                  }
                : {}
            ),
          }
        }
      } else {
        fileAssets[incoming.id] = incoming
        newIds.push(incoming.id)
      }
    }
    const sortedNew = newIds.sort((a, b) =>
      fileAssets[b].updatedAt.localeCompare(fileAssets[a].updatedAt),
    )
    const fileAssetOrder = sortedNew.length > 0
      ? [...sortedNew, ...state.fileAssetOrder]
      : state.fileAssetOrder
    return { ...state, fileAssetOrder, fileAssets }
  }
  if (action.type === 'markFileAssetServerSynced') {
    const asset = state.fileAssets[action.assetId]
    if (!asset || asset.serverSynced === true) return state
    return {
      ...state,
      fileAssets: {
        ...state.fileAssets,
        [asset.id]: { ...asset, serverSynced: true },
      },
    }
  }
  if (action.type === 'setServerAssetBody') {
    // Load-on-use: keep the editable body separate from the server-fenced
    // canonical body used by Chat/Editor. Never changes updatedAt (so the
    // autosave does not read it back as a local edit) and never sets dirty.
    const asset = state.fileAssets[action.assetId]
    if (!asset) return state
    return {
      ...state,
      fileAssets: {
        ...state.fileAssets,
        [action.assetId]: {
          ...asset,
          extractedText: action.extractedText,
          preparedAt: action.preparedAt,
          preparedContentHash: action.preparedContentHash,
          preparedParserId: action.preparedParserId,
          preparedText: action.preparedText,
        },
      },
    }
  }
  if (action.type === 'setFileAssetParsePending') {
    // Transient background-parse flag (drives the "Parsing…" badge). Never
    // dirty/persisted — it is a client-only in-flight marker.
    const asset = state.fileAssets[action.assetId]
    if (!asset || Boolean(asset.parsePending) === action.pending) return state
    return {
      ...state,
      fileAssets: {
        ...state.fileAssets,
        [asset.id]: { ...asset, parsePending: action.pending },
      },
    }
  }
  if (action.type === 'setFileAssetUploadPending') {
    // Transient upload-in-flight flag (drives the "Wird hochgeladen…"
    // badge). Never dirty/persisted. Entering pending also clears a
    // previous upload error — this is the retry entry point.
    const asset = state.fileAssets[action.assetId]
    if (
      !asset
      || (Boolean(asset.uploadPending) === action.pending
        && (!action.pending || asset.uploadError == null))
    ) {
      return state
    }
    return {
      ...state,
      fileAssets: {
        ...state.fileAssets,
        [asset.id]: {
          ...asset,
          uploadPending: action.pending,
          ...(action.pending ? { uploadError: null } : {}),
          ...(action.pending
            ? { uploadStatus: asset.uploadOperationId ? 'retrying' : 'uploading' }
            : {}),
        },
      },
    }
  }
  if (action.type === 'adoptFileAssetUploadLifecycle') {
    const asset = state.fileAssets[action.assetId]
    if (!asset || (asset.lifecycleStatus ?? 'active') !== 'active' || !action.status) {
      return state
    }
    const pending = action.status === 'awaiting_upload'
      || action.status === 'uploading'
      || action.status === 'retrying'
      || action.status === 'parsing'
      || action.status === 'finalizing'
    const ready = action.status === 'ready' && action.serverFileId !== null
    return {
      ...state,
      ...(ready ? { dirty: true } : {}),
      fileAssets: {
        ...state.fileAssets,
        [asset.id]: {
          ...asset,
          ...(ready
            ? {
                parseWarning: stripServerUploadFailureWarning(asset.parseWarning),
                serverFileId: action.serverFileId,
                serverSynced: true,
                updatedAt: new Date().toISOString(),
              }
            : {}),
          uploadError: action.error,
          uploadOperationId: action.operationId,
          uploadPending: pending,
          uploadStatus: action.status,
        },
      },
    }
  }
  if (action.type === 'completeFileAssetUpload') {
    // The original bytes reached the server. serverFileId is persisted
    // data (enables server parse + knowledge ingestion), so this is a
    // real edit: bumps updatedAt + dirty. Also settles the transient
    // upload state and retracts a persisted earlier-failure warning —
    // "Datei bleibt lokal" is false once the retry succeeded.
    const asset = state.fileAssets[action.assetId]
    if (!asset || (asset.lifecycleStatus ?? 'active') !== 'active') return state
    return {
      ...state,
      dirty: true,
      fileAssets: {
        ...state.fileAssets,
        [asset.id]: {
          ...asset,
          parseWarning: stripServerUploadFailureWarning(asset.parseWarning),
          serverFileId: action.serverFileId,
          serverSynced: true,
          updatedAt: new Date().toISOString(),
          uploadError: null,
          uploadStatus: 'ready',
          uploadPending: false,
        },
      },
    }
  }
  if (action.type === 'failFileAssetUpload') {
    // Upload failed: the transient error drives the badge + retry; the
    // persisted trace goes into parseWarning (same wording contract as
    // the batch ingest path), so a reload shows the same local-only
    // warning. Bumps updatedAt + dirty for the warning only.
    const asset = state.fileAssets[action.assetId]
    if (!asset || (asset.lifecycleStatus ?? 'active') !== 'active') return state
    const warning = asset.parseWarning
      ? asset.parseWarning.includes(action.message)
        ? asset.parseWarning
        : `${asset.parseWarning} ${action.message}`
      : action.message
    return {
      ...state,
      dirty: true,
      fileAssets: {
        ...state.fileAssets,
        [asset.id]: {
          ...asset,
          parseWarning: warning,
          updatedAt: new Date().toISOString(),
          uploadError: action.message,
          uploadStatus: 'failed',
          uploadPending: false,
        },
      },
    }
  }
  if (action.type === 'applyFileAssetClientParse') {
    // The deferred client parse settled onto a placeholder row. If the
    // higher-fidelity server (MarkItDown) parse already landed, only
    // backfill the page count — never downgrade its text or status.
    // clearParsePending stays false while a server parse is still in
    // flight for this asset, so the "Parsing…" badge hands over instead
    // of flickering off and on.
    const asset = state.fileAssets[action.assetId]
    if (!asset || (asset.lifecycleStatus ?? 'active') !== 'active') return state
    if (asset.parserId === 'markitdown') {
      if (asset.pageCount != null || action.pageCount == null) {
        if (!action.clearParsePending || !asset.parsePending) return state
        return {
          ...state,
          fileAssets: {
            ...state.fileAssets,
            [asset.id]: { ...asset, parsePending: false },
          },
        }
      }
      return {
        ...state,
        dirty: true,
        fileAssets: {
          ...state.fileAssets,
          [asset.id]: {
            ...asset,
            pageCount: action.pageCount,
            updatedAt: new Date().toISOString(),
            ...(action.clearParsePending ? { parsePending: false } : {}),
          },
        },
      }
    }
    const warning = asset.parseWarning && action.parseWarning
      ? `${action.parseWarning} ${asset.parseWarning}`
      : action.parseWarning ?? asset.parseWarning
    return {
      ...state,
      dirty: true,
      fileAssets: {
        ...state.fileAssets,
        [asset.id]: {
          ...asset,
          extractedText: action.extractedText,
          pageCount: action.pageCount,
          parserId: 'client',
          parseStatus: action.parseStatus,
          parseWarning: warning,
          textTruncated: action.textTruncated,
          updatedAt: new Date().toISOString(),
          ...(action.clearParsePending ? { parsePending: false } : {}),
        },
      },
    }
  }
  if (action.type === 'upsertServerVectorIndexes') {
    // Hydrate vector-index records from the server. Additive + local-
    // newer-wins + never dirty. Full records (members + history travel with
    // them); the ephemeral indexingJobs map is a SEPARATE field and is left
    // untouched so a live reindex on this device is never clobbered.
    if (action.indexes.length === 0) return state
    const vectorIndexes = { ...state.vectorIndexes }
    const newIds: string[] = []
    for (const incoming of action.indexes) {
      const local = vectorIndexes[incoming.id]
      if (local) {
        if (incoming.updatedAt > local.updatedAt) vectorIndexes[incoming.id] = incoming
      } else {
        vectorIndexes[incoming.id] = incoming
        newIds.push(incoming.id)
      }
    }
    const sortedNew = newIds.sort((a, b) =>
      vectorIndexes[b].updatedAt.localeCompare(vectorIndexes[a].updatedAt),
    )
    const vectorIndexOrder = sortedNew.length > 0
      ? [...sortedNew, ...state.vectorIndexOrder]
      : state.vectorIndexOrder
    return { ...state, vectorIndexOrder, vectorIndexes }
  }
  if (action.type === 'appendApiRunEvent') {
    return appendApiRunEventToState(state, action.event, {
      arrivedLive: true,
      receivedAt: action.receivedAt,
    })
  }
  if (action.type === 'appendApiRunEvents') {
    // History batch: the transport delivered these as replay/catch-up (SSE
    // frames before the server's `inqtrix.stream.live` boundary, or the
    // first polling page), so they land in ONE commit — no render per
    // replayed frame — and without `arrivedLive`, so no row animates.
    // Semantics are pinned to the singular path: Batch(N) ≡ N × Einzeln,
    // bis auf `arrivedLive` (state.test.ts).
    return action.events.reduce(
      (accumulated, event) =>
        appendApiRunEventToState(accumulated, event, { arrivedLive: false }),
      state,
    )
  }
  if (action.type === 'attachApiRunResult') {
    const knowledgeItem = knowledgeItemByRunId(state, action.result.run_id)
    const withKnowledge = knowledgeItem
      ? attachKnowledgeResult(state, knowledgeItem, action.result, action.finishedAt)
      : state
    const current = withKnowledge.researchRuns[action.result.run_id]
    if (!current) return withKnowledge
    const run = { ...attachRunResult(current, action.result), resultError: undefined }

    return {
      ...withKnowledge,
      dirty: true,
      researchRuns: {
        ...withKnowledge.researchRuns,
        [run.runId]: run,
      },
    }
  }
  if (action.type === 'markApiRunUnavailable') {
    // Knowledge asks never live in researchRuns; their surface is the
    // thread item, which would otherwise spin at "running" forever with
    // no path out (the ask gate blocks and cancel also 404s). The item
    // settles terminally with the unavailable prose -- project data, so
    // this branch IS dirty.
    const knowledgeItem = knowledgeItemByRunId(state, action.runId)
    if (knowledgeItem) {
      return touchKnowledgeSessions(
        updateKnowledgeItem({ ...state, dirty: true }, knowledgeItem.id, (item) => (
          item.status === 'completed'
            ? item
            : {
                ...item,
                error: 'Dieser Lauf ist nicht mehr verfügbar.',
                status: 'failed',
              }
        )),
        [knowledgeItem.sessionId],
        new Date().toISOString(),
      )
    }
    const current = state.researchRuns[action.runId]
    if (!current || current.unavailable === true) return state
    // Calm lock, NOT an error and NOT a status claim: the server's 404
    // is deliberately non-disclosing (revoked/deleted/expired/foreign
    // all identical), so the last known status stays and only the flag
    // moves. The next run-list hydration prunes the record entirely.
    // NOT dirty: the flag is server-derived, never serialized into a
    // project file -- dirtying it would show "Unsaved changes" for a
    // change no save artifact can capture.
    return {
      ...state,
      researchRuns: {
        ...state.researchRuns,
        [action.runId]: { ...current, unavailable: true },
      },
    }
  }
  if (action.type === 'markApiRunResultError') {
    // Loading a finished result failed (timeout, network, server error). The
    // run completed on the server, so its status stays; only an ask that never
    // received its answer settles as failed, because its thread item has no
    // other way to leave "running". A completed ask is left untouched, its
    // session time included.
    const knowledgeItem = knowledgeItemByRunId(state, action.runId)
    const withKnowledge = knowledgeItem && knowledgeItem.status !== 'completed'
      ? touchKnowledgeSessions(
        updateKnowledgeItem({ ...state, dirty: true }, knowledgeItem.id, (item) => (
          { ...item, error: action.message, status: 'failed' }
        )),
        [knowledgeItem.sessionId],
        new Date().toISOString(),
      )
      : state
    const current = withKnowledge.researchRuns[action.runId]
    if (!current) return withKnowledge
    return {
      ...withKnowledge,
      researchRuns: {
        ...withKnowledge.researchRuns,
        [action.runId]: { ...current, resultError: action.message },
      },
    }
  }
  if (action.type === 'markApiRunError') {
    const knowledgeItem = knowledgeItemByRunId(state, action.runId)
    const updatedAt = new Date().toISOString()
    const withKnowledge = knowledgeItem
      ? touchKnowledgeSessions(
        updateKnowledgeItem({ ...state, dirty: true }, knowledgeItem.id, (item) => (
          item.status === 'completed'
            ? item
            : { ...item, error: action.message, status: 'failed' }
        )),
        [knowledgeItem.sessionId],
        updatedAt,
      )
      : state
    const current = withKnowledge.researchRuns[action.runId]
    if (!current) return withKnowledge

    return {
      ...withKnowledge,
      dirty: true,
      researchRuns: {
        ...withKnowledge.researchRuns,
        [action.runId]: {
          ...current,
          error: action.message,
          status: 'failed',
          summary: {
            ...current.summary,
            queueNote: action.message,
          },
        },
      },
    }
  }
  if (action.type === 'createKnowledgeSession') {
    const exists = state.knowledgeSessions[action.session.id]
    return {
      ...state,
      dirty: true,
      knowledgeSessionOrder: exists
        ? state.knowledgeSessionOrder
        : [action.session.id, ...state.knowledgeSessionOrder],
      knowledgeSessions: {
        ...state.knowledgeSessions,
        [action.session.id]: action.session,
      },
      selectedKnowledgeSessionId: action.session.id,
    }
  }
  if (action.type === 'createKnowledgeSessionGroup') {
    const now = new Date().toISOString()
    const group: KnowledgeSessionGroupRecord = {
      createdAt: now,
      id: createId('knowledge-session-group'),
      title: action.title.trim() || 'New folder',
      updatedAt: now,
    }
    return {
      ...state,
      dirty: true,
      knowledgeSessionGroupOrder: [group.id, ...(state.knowledgeSessionGroupOrder ?? [])],
      knowledgeSessionGroups: {
        ...(state.knowledgeSessionGroups ?? {}),
        [group.id]: group,
      },
    }
  }
  if (action.type === 'renameKnowledgeSessionGroup') {
    const group = state.knowledgeSessionGroups[action.groupId]
    const title = action.title.trim()
    if (!group || !title || group.title === title) return state
    return {
      ...state,
      dirty: true,
      knowledgeSessionGroups: {
        ...state.knowledgeSessionGroups,
        [group.id]: { ...group, title, updatedAt: new Date().toISOString() },
      },
    }
  }
  if (action.type === 'deleteKnowledgeSessionGroup') {
    if (!state.knowledgeSessionGroups[action.groupId]) return state
    const knowledgeSessionGroups = { ...state.knowledgeSessionGroups }
    delete knowledgeSessionGroups[action.groupId]
    const knowledgeSessionGroupMemberships = Object.fromEntries(
      Object.entries(state.knowledgeSessionGroupMemberships ?? {})
        .filter(([, groupId]) => groupId !== action.groupId),
    )
    return {
      ...state,
      dirty: true,
      knowledgeSessionGroupMemberships,
      knowledgeSessionGroupOrder: (state.knowledgeSessionGroupOrder ?? [])
        .filter((groupId) => groupId !== action.groupId),
      knowledgeSessionGroups,
    }
  }
  if (action.type === 'moveKnowledgeSessionGroup') {
    return moveKnowledgeSessionGroup(state, action.groupId, action.targetIndex)
  }
  if (action.type === 'moveKnowledgeSessionToGroup') {
    return moveKnowledgeSessionToGroup(state, action.sessionId, action.groupId, action.targetIndex)
  }
  if (action.type === 'selectKnowledgeSession') {
    if (!state.knowledgeSessions[action.sessionId]
      || state.knowledgeSessions[action.sessionId]?.deletion) return state
    return { ...state, selectedKnowledgeSessionId: action.sessionId }
  }
  if (action.type === 'renameKnowledgeSession') {
    const session = state.knowledgeSessions[action.sessionId]
    const title = action.title.trim()
    if (!session || !title || session.title === title) return state
    return {
      ...state,
      dirty: true,
      knowledgeSessions: {
        ...state.knowledgeSessions,
        [session.id]: {
          ...session,
          isBootstrapPlaceholder: false,
          title,
          updatedAt: new Date().toISOString(),
        },
      },
    }
  }
  if (action.type === 'deleteKnowledgeSession') {
    // The last remaining session is deletable too: the empty state shows the
    // composer, and the next ask creates a fresh session (startKnowledgeAsk).
    if (!state.knowledgeSessions[action.sessionId] && !action.operationId) return state
    const knowledgeSessions = { ...state.knowledgeSessions }
    delete knowledgeSessions[action.sessionId]
    const knowledgeSessionOrder = state.knowledgeSessionOrder.filter((id) => id !== action.sessionId)
    const knowledgeItems = { ...state.knowledgeItems }
    const knowledgeItemOrder = state.knowledgeItemOrder.filter((itemId) => {
      const item = knowledgeItems[itemId]
      const keep = !item || item.sessionId !== action.sessionId
      if (!keep) delete knowledgeItems[itemId]
      return keep
    })
    const selectedKnowledgeSessionId = state.selectedKnowledgeSessionId === action.sessionId
      ? knowledgeSessionOrder[0] ?? null
      : state.selectedKnowledgeSessionId
    const knowledgeSessionGroupMemberships = { ...(state.knowledgeSessionGroupMemberships ?? {}) }
    delete knowledgeSessionGroupMemberships[action.sessionId]
    return {
      ...state,
      dirty: true,
      knowledgeItemOrder,
      knowledgeItems,
      knowledgeSessionGroupMemberships,
      knowledgeSessionOrder,
      knowledgeSessions,
      knowledgeSessionDeletionReceipts: action.operationId
        ? {
          ...state.knowledgeSessionDeletionReceipts,
          [action.sessionId]: action.operationId,
        }
        : state.knowledgeSessionDeletionReceipts,
      selectedKnowledgeSessionId,
      ui: {
        ...state.ui,
        pinnedExplorer: {
          ...state.ui.pinnedExplorer,
          knowledgeSessionIds: removeExplorerPin(state.ui.pinnedExplorer.knowledgeSessionIds, action.sessionId),
        },
      },
    }
  }
  if (action.type === 'setKnowledgeSessionDeletionState') {
    const session = state.knowledgeSessions[action.sessionId]
    if (!session) return state
    const selectedKnowledgeSessionId = state.selectedKnowledgeSessionId === action.sessionId
      ? state.knowledgeSessionOrder.find((id) => (
        id !== action.sessionId && !state.knowledgeSessions[id]?.deletion
      )) ?? null
      : state.selectedKnowledgeSessionId
    return {
      ...state,
      knowledgeSessions: {
        ...state.knowledgeSessions,
        [action.sessionId]: { ...session, deletion: action.deletion },
      },
      selectedKnowledgeSessionId,
    }
  }
  if (action.type === 'upsertServerKnowledgeSessionGroups') {
    if (action.groups.length === 0) return state
    const knowledgeSessionGroups = { ...state.knowledgeSessionGroups }
    const newIds: string[] = []
    for (const incoming of action.groups) {
      const local = knowledgeSessionGroups[incoming.id]
      if (local) {
        if (incoming.updatedAt > local.updatedAt) knowledgeSessionGroups[incoming.id] = incoming
      } else {
        knowledgeSessionGroups[incoming.id] = incoming
        newIds.push(incoming.id)
      }
    }
    const sortedNew = newIds.sort((a, b) =>
      knowledgeSessionGroups[b].updatedAt.localeCompare(knowledgeSessionGroups[a].updatedAt),
    )
    const knowledgeSessionGroupOrder = sortedNew.length > 0
      ? [...sortedNew, ...state.knowledgeSessionGroupOrder]
      : state.knowledgeSessionGroupOrder
    return { ...state, knowledgeSessionGroupOrder, knowledgeSessionGroups }
  }
  if (action.type === 'upsertServerKnowledgeSessions') {
    if (action.sessions.length === 0) return state
    const knowledgeSessions = { ...state.knowledgeSessions }
    const knowledgeSessionGroupMemberships = { ...state.knowledgeSessionGroupMemberships }
    const newIds: string[] = []
    for (const incoming of action.sessions) {
      if (state.knowledgeSessionDeletionReceipts?.[incoming.id]) continue
      const local = knowledgeSessions[incoming.id]
      if (local) {
        if (incoming.deletion || incoming.updatedAt > local.updatedAt) {
          knowledgeSessions[incoming.id] = incoming
          knowledgeSessionGroupMemberships[incoming.id] =
            action.memberships[incoming.id] ?? null
        }
      } else {
        knowledgeSessions[incoming.id] = incoming
        knowledgeSessionGroupMemberships[incoming.id] =
          action.memberships[incoming.id] ?? null
        newIds.push(incoming.id)
      }
    }
    const knowledgeSessionOrder = newIds.length === 0
      ? state.knowledgeSessionOrder
      : [...newIds, ...state.knowledgeSessionOrder]
    const selectedKnowledgeSessionId =
      state.selectedKnowledgeSessionId
        && knowledgeSessions[state.selectedKnowledgeSessionId]
        && !knowledgeSessions[state.selectedKnowledgeSessionId].deletion
        ? state.selectedKnowledgeSessionId
        : knowledgeSessionOrder.find((id) => !knowledgeSessions[id]?.deletion) ?? null
    return {
      ...state,
      knowledgeSessionGroupMemberships,
      knowledgeSessionOrder,
      knowledgeSessions,
      selectedKnowledgeSessionId,
    }
  }
  if (action.type === 'pruneLocalPlaceholderKnowledgeSessions') {
    // The server is authoritative for the untouched seed placeholder: drop
    // only that pristine local bootstrap session when it is not on the server.
    // A renamed or user-created empty session is user intent and must sync.
    const serverIds = new Set(action.serverIds)
    const sessionHasItems = (sessionId: string) =>
      state.knowledgeItemOrder.some(
        (itemId) => state.knowledgeItems[itemId]?.sessionId === sessionId,
      )
    const isPristineBootstrap = (sessionId: string) => {
      const session = state.knowledgeSessions[sessionId]
      return Boolean(session)
        && session.isBootstrapPlaceholder === true
        && session.title === DEFAULT_KNOWLEDGE_SESSION_TITLE
        && !sessionHasItems(sessionId)
    }
    const keep = (sessionId: string) => {
      if (serverIds.has(sessionId) || sessionHasItems(sessionId)) return true
      if (!state.knowledgeSessions[sessionId]) return false
      return !isPristineBootstrap(sessionId)
    }
    const removedIds = state.knowledgeSessionOrder.filter((id) => !keep(id))
    if (removedIds.length === 0) return state
    const knowledgeSessions = { ...state.knowledgeSessions }
    for (const id of removedIds) delete knowledgeSessions[id]
    const knowledgeSessionOrder = state.knowledgeSessionOrder.filter(keep)
    const knowledgeSessionGroupMemberships = { ...(state.knowledgeSessionGroupMemberships ?? {}) }
    for (const id of removedIds) delete knowledgeSessionGroupMemberships[id]
    const selectedKnowledgeSessionId =
      state.selectedKnowledgeSessionId && knowledgeSessions[state.selectedKnowledgeSessionId]
        ? state.selectedKnowledgeSessionId
        : knowledgeSessionOrder[0] ?? null
    // No dirty flag: this runs during hydrate (a server read), not a user edit,
    // so it must not mark the project dirty or trigger a save.
    return {
      ...state,
      knowledgeSessionGroupMemberships,
      knowledgeSessionOrder,
      knowledgeSessions,
      selectedKnowledgeSessionId,
    }
  }
  if (action.type === 'setServerKnowledgeSessionItems') {
    if (state.knowledgeSessionDeletionReceipts?.[action.sessionId]) return state
    const session = state.knowledgeSessions[action.sessionId]
    if (!session) return state
    const existingOtherIds = state.knowledgeItemOrder.filter((itemId) =>
      state.knowledgeItems[itemId]?.sessionId !== action.sessionId)
    const incomingItems = action.items.map((item) => ({ ...item, sessionId: action.sessionId }))
    return {
      ...state,
      knowledgeItemOrder: [...existingOtherIds, ...incomingItems.map((item) => item.id)],
      knowledgeItems: {
        ...Object.fromEntries(existingOtherIds.flatMap((itemId) => {
          const item = state.knowledgeItems[itemId]
          return item ? [[itemId, item] as const] : []
        })),
        ...Object.fromEntries(incomingItems.map((item) => [item.id, item])),
      },
    }
  }
  if (action.type === 'deleteKnowledgeItems') {
    const itemIds = new Set(action.itemIds)
    const existingIds = action.itemIds.filter((itemId) => Boolean(state.knowledgeItems[itemId]))
    if (existingIds.length === 0) return state
    const sessionIds = new Set(existingIds.map((itemId) => state.knowledgeItems[itemId].sessionId))
    const knowledgeItems = { ...state.knowledgeItems }
    for (const itemId of existingIds) delete knowledgeItems[itemId]
    const updatedAt = new Date().toISOString()
    return touchKnowledgeSessions({
      ...state,
      dirty: true,
      knowledgeItemOrder: state.knowledgeItemOrder.filter((itemId) => !itemIds.has(itemId)),
      knowledgeItems,
    }, sessionIds, updatedAt)
  }
  if (action.type === 'clearKnowledgeSession') {
    if (!state.knowledgeSessions[action.sessionId]) return state
    const itemIds = state.knowledgeItemOrder.filter((itemId) =>
      state.knowledgeItems[itemId]?.sessionId === action.sessionId)
    if (itemIds.length === 0) return state
    const itemIdSet = new Set(itemIds)
    const knowledgeItems = { ...state.knowledgeItems }
    for (const itemId of itemIds) delete knowledgeItems[itemId]
    const updatedAt = new Date().toISOString()
    return touchKnowledgeSessions({
      ...state,
      dirty: true,
      knowledgeItemOrder: state.knowledgeItemOrder.filter((itemId) => !itemIdSet.has(itemId)),
      knowledgeItems,
    }, [action.sessionId], updatedAt)
  }
  if (action.type === 'startKnowledgeAsk') {
    const session = state.knowledgeSessions[action.item.sessionId]
    const sessionHasItems = state.knowledgeItemOrder.some((itemId) =>
      state.knowledgeItems[itemId]?.sessionId === action.item.sessionId)
    const updatedSession = session
      ? {
        ...session,
        isBootstrapPlaceholder: false,
        title: !sessionHasItems && isPlaceholderKnowledgeSessionTitle(session.title)
          ? knowledgeSessionTitleFromQuestion(action.item.question)
          : session.title,
        updatedAt: action.item.createdAt,
      }
      : {
        createdAt: action.item.createdAt,
        id: action.item.sessionId,
        title: knowledgeSessionTitleFromQuestion(action.item.question),
        updatedAt: action.item.createdAt,
      }
    const knowledgeSessionOrder = state.knowledgeSessionOrder.includes(updatedSession.id)
      ? state.knowledgeSessionOrder
      : [updatedSession.id, ...state.knowledgeSessionOrder]
    return {
      ...state,
      dirty: true,
      knowledgeItemOrder: [...state.knowledgeItemOrder, action.item.id],
      knowledgeItems: {
        ...state.knowledgeItems,
        [action.item.id]: action.item,
      },
      knowledgeSessionOrder,
      knowledgeSessions: {
        ...state.knowledgeSessions,
        [updatedSession.id]: updatedSession,
      },
      selectedKnowledgeSessionId: updatedSession.id,
    }
  }
  if (action.type === 'restartKnowledgeAsk') {
    const current = state.knowledgeItems[action.replacedItemId]
    if (!current) return state
    const restarted: KnowledgeThreadItemRecord = {
      collectionTitles: action.item.collectionTitles,
      createdAt: action.item.createdAt,
      id: current.id,
      progress: { steps: [] },
      question: action.item.question,
      requestedProfile: action.item.requestedProfile,
      runId: action.item.runId,
      sessionId: current.sessionId,
      status: 'running',
    }
    if (action.item.collectionIds) restarted.collectionIds = action.item.collectionIds
    if (action.item.topK !== undefined) restarted.topK = action.item.topK
    if (action.item.finalK !== undefined) restarted.finalK = action.item.finalK
    return touchKnowledgeSessions({
      ...state,
      dirty: true,
      knowledgeItems: {
        ...state.knowledgeItems,
        [current.id]: restarted,
      },
      selectedKnowledgeSessionId: current.sessionId,
    }, [current.sessionId], action.item.createdAt)
  }
  if (action.type === 'completeKnowledgeItem') {
    const item = knowledgeItemByRunId(state, action.runId)
    if (!item) return state
    const completedAt = new Date().toISOString()
    return touchKnowledgeSessions(
      updateKnowledgeItem({
        ...state,
        dirty: true,
      }, item.id, (current) => ({
        ...current,
        answer: action.answer,
        completedAt,
        progress: {
          ...current.progress,
          steps: current.progress.steps.map((step) => (
            step.status === 'done' ? step : { ...step, status: 'done' as const }
          )),
        },
        status: 'completed',
      })),
      [item.sessionId],
      completedAt,
    )
  }
  if (action.type === 'cancelLocalRun') {
    const current = state.researchRuns[action.runId]
    if (!current || (current.status !== 'running' && current.status !== 'queued')) return state
    const now = new Date().toISOString()
    const cancelRequested = current.status === 'running'
      ? [{
        active: false,
        createdAt: now,
        id: createId('event'),
        kind: 'system' as const,
        severity: 'warning' as const,
        title: 'Cancellation requested',
      }]
      : []
    const cancelledEvent = {
      createdAt: now,
      id: createId('event'),
      kind: 'system' as const,
      severity: 'warning' as const,
      title: 'Run cancelled',
    }

    return {
      ...state,
      dirty: true,
      researchRuns: {
        ...state.researchRuns,
        [action.runId]: {
          ...current,
          // Shared O(1)-tail-flip append: clears the previous live step
          // and appends the cancel markers without re-spreading every event.
          events: [...cancelRequested, cancelledEvent].reduce(
            (events, event) => appendRunEventRecord(events, event),
            current.events,
          ),
          finishedAt: now,
          queuePosition: null,
          status: 'cancelled',
          summary: {
            ...current.summary,
            queueNote: undefined,
          },
        },
      },
    }
  }
  if (action.type === 'selectChatThread') {
    if (state.ui.selectedChatThreadId === action.threadId) return state
    // Project the target thread's own pick into the working value — ui only.
    // A thread without a stored pick clears it so the preference may seed.
    const selection = state.chatThreads[action.threadId]?.modelSelection ?? null
    return {
      ...state,
      ui: {
        ...state.ui,
        selectedChatThreadId: action.threadId,
        selectedChatEffort: selection?.effort ?? null,
        selectedChatModel: selection?.model ?? null,
        selectedChatModelTier: selection?.tier ?? null,
      },
    }
  }
  if (action.type === 'setResearchRunAutocomplete') {
    const current = state.researchRuns[action.runId]
    if (!current || current.includeInAutocomplete === action.includeInAutocomplete) return state

    return {
      ...state,
      dirty: true,
      researchRuns: {
        ...state.researchRuns,
        [action.runId]: { ...current, includeInAutocomplete: action.includeInAutocomplete },
      },
    }
  }
  if (action.type === 'attachReportToChatDraft') {
    if (!reportFromRun(state, action.runId)) return state

    return {
      ...state,
      ui: {
        ...state.ui,
        pendingChatAttachmentRefs: addPendingAttachmentRef(
          state.ui.pendingChatAttachmentRefs,
          { kind: 'research-report', runId: action.runId },
        ),
        pendingChatReportRunId: action.runId,
      },
    }
  }
  if (action.type === 'attachChatContextToDraft') {
    if (!contextRefExists(state, action.ref)) return state
    return {
      ...state,
      ui: {
        ...state.ui,
        pendingChatAttachmentRefs: addPendingAttachmentRef(
          state.ui.pendingChatAttachmentRefs,
          action.ref,
        ),
        pendingChatReportRunId: action.ref.kind === 'research-report'
          ? action.ref.runId
          : state.ui.pendingChatReportRunId,
      },
    }
  }
  if (action.type === 'removeChatContextFromDraft') {
    const pendingChatAttachmentRefs = state.ui.pendingChatAttachmentRefs.filter((ref) => (
      chatContextRefKey(ref) !== chatContextRefKey(action.ref)
    ))
    return {
      ...state,
      ui: {
        ...state.ui,
        pendingChatAttachmentRefs,
        pendingChatReportRunId: pendingChatAttachmentRefs.find((ref) => ref.kind === 'research-report')?.runId ?? null,
      },
    }
  }
  if (action.type === 'reorderChatContextInDraft') {
    if (
      action.fromIndex === action.toIndex
      || action.fromIndex < 0
      || action.toIndex < 0
      || action.fromIndex >= state.ui.pendingChatAttachmentRefs.length
      || action.toIndex >= state.ui.pendingChatAttachmentRefs.length
    ) {
      return state
    }

    const pendingChatAttachmentRefs = moveItem(
      state.ui.pendingChatAttachmentRefs,
      action.fromIndex,
      action.toIndex,
    )
    return {
      ...state,
      dirty: true,
      ui: {
        ...state.ui,
        pendingChatAttachmentRefs,
        pendingChatReportRunId: pendingChatAttachmentRefs.find((ref) => ref.kind === 'research-report')?.runId ?? null,
      },
    }
  }
  if (action.type === 'attachReportToNewChat') {
    const report = reportFromRun(state, action.runId)
    if (!report) return state

    const thread = createChatThread({
      preview: `Report attached: ${report.title}`,
      title: `Chat: ${report.title}`,
    })

    return {
      ...state,
      chatThreadOrder: [thread.id, ...state.chatThreadOrder],
      chatThreads: {
        ...state.chatThreads,
        [thread.id]: thread,
      },
      dirty: true,
      ui: {
        ...state.ui,
        pendingChatAttachmentRefs: [{ kind: 'research-report', runId: action.runId }],
        activeView: 'chat',
        pendingChatReportRunId: action.runId,
        selectedChatThreadId: thread.id,
      },
    }
  }
  if (action.type === 'clearChatDraftAttachment') {
    return {
      ...state,
      ui: {
        ...state.ui,
        pendingChatAttachmentRefs: [],
        pendingChatReportRunId: null,
      },
    }
  }
  if (action.type === 'ingestFileAssets') {
    const newIds = action.assets
      .filter((asset) => !state.fileAssets[asset.id])
      .map((asset) => asset.id)
    if (action.assets.length === 0) return state
    const fileAssets = { ...state.fileAssets }
    for (const asset of action.assets) {
      fileAssets[asset.id] = asset
    }
    return {
      ...state,
      dirty: true,
      fileAssetOrder: [...newIds, ...state.fileAssetOrder],
      fileAssets,
    }
  }
  if (action.type === 'renameFileAsset') {
    const asset = state.fileAssets[action.fileId]
    const label = action.label.trim()
    if (
      !asset
      || (asset.lifecycleStatus ?? 'active') !== 'active'
      || !label
      || asset.label === label
    ) return state
    return {
      ...state,
      dirty: true,
      fileAssets: {
        ...state.fileAssets,
        [asset.id]: { ...asset, label, updatedAt: new Date().toISOString() },
      },
    }
  }
  if (action.type === 'moveFileAsset') {
    // The single move is the batch of one — one implementation, no drift.
    return researchDeskReducer(state, {
      fileIds: [action.fileId], groupId: action.groupId,
      sectionId: action.sectionId, type: 'moveFileAssets',
    })
  }
  if (action.type === 'moveFileAssets') {
    // Bulk move (selection mode): ONE state update regardless of count.
    if (!state.fileLibrarySections[action.sectionId]) return state
    const targetGroup = action.groupId ? state.fileGroups[action.groupId] : null
    const groupId = targetGroup && targetGroup.sectionId === action.sectionId ? action.groupId : null
    const now = new Date().toISOString()
    let changed = false
    const fileAssets = { ...state.fileAssets }
    for (const fileId of action.fileIds) {
      const asset = fileAssets[fileId]
      if (
        !asset
        || (asset.lifecycleStatus ?? 'active') !== 'active'
        || (asset.sectionId === action.sectionId && asset.groupId === groupId)
      ) continue
      fileAssets[fileId] = { ...asset, groupId, sectionId: action.sectionId, updatedAt: now }
      changed = true
    }
    if (!changed) return state
    return { ...state, dirty: true, fileAssets }
  }
  if (action.type === 'setFileAssetDeletionState') {
    let changed = false
    const fileAssets = { ...state.fileAssets }
    for (const fileId of action.fileIds) {
      const asset = fileAssets[fileId]
      if (!asset) continue
      if (
        (
          asset.deletionOperationId
          && asset.deletionOperationId !== action.operationId
        )
        || (!asset.deletionOperationId && asset.serverSynced !== true)
      ) continue
      const lifecycleStatus = action.status === 'delete_failed' ? 'delete_failed' : 'deleting'
      if (
        asset.lifecycleStatus === lifecycleStatus
        && asset.deletionOperationId === action.operationId
        && asset.deletionStage === action.stage
        && asset.deletionError === action.error
      ) continue
      fileAssets[fileId] = {
        ...asset,
        lifecycleStatus,
        deletionError: action.error,
        deletionOperationId: action.operationId,
        deletionStage: action.stage,
        parsePending: false,
        uploadPending: false,
      }
      changed = true
    }
    return changed ? { ...state, fileAssets } : state
  }
  if (action.type === 'completeFileAssetDeletion') {
    const ownedIds = action.fileIds.filter((fileId) => {
      const asset = state.fileAssets[fileId]
      return Boolean(
        asset
        && (
          asset.lifecycleStatus === 'deleting'
          || asset.lifecycleStatus === 'delete_failed'
        )
        && asset.deletionOperationId === action.operationId,
      )
    })
    return researchDeskReducer(state, {
      fileIds: ownedIds,
      type: 'deleteFileAssets',
    })
  }
  if (action.type === 'setFileGroupDeletionState') {
    const group = state.fileGroups[action.groupId]
    if (!group) return state
    if (
      (
        group.deletionOperationId
        && group.deletionOperationId !== action.operationId
      )
      || (!group.deletionOperationId && group.serverSynced !== true)
    ) return state
    const lifecycleStatus = action.status === 'delete_failed' ? 'delete_failed' : 'deleting'
    if (
      group.lifecycleStatus === lifecycleStatus
      && group.deletionOperationId === action.operationId
      && group.deletionStage === action.stage
      && group.deletionError === action.error
    ) return state
    return {
      ...state,
      fileGroups: {
        ...state.fileGroups,
        [group.id]: {
          ...group,
          deletionError: action.error,
          deletionOperationId: action.operationId,
          deletionStage: action.stage,
          lifecycleStatus,
        },
      },
    }
  }
  if (action.type === 'completeFileGroupDeletion') {
    const group = state.fileGroups[action.groupId]
    if (
      !group
      || (
        group.lifecycleStatus !== 'deleting'
        && group.lifecycleStatus !== 'delete_failed'
      )
      || group.deletionOperationId !== action.operationId
    ) return state
    return researchDeskReducer(state, {
      groupId: action.groupId,
      type: 'deleteFileGroup',
    })
  }
  if (action.type === 'setFileLibrarySectionDeletionState') {
    const section = state.fileLibrarySections[action.sectionId]
    if (!section || section.kind === 'temporary') return state
    if (
      (
        section.deletionOperationId
        && section.deletionOperationId !== action.operationId
      )
      || (!section.deletionOperationId && section.serverSynced !== true)
    ) return state
    const lifecycleStatus = action.status === 'delete_failed' ? 'delete_failed' : 'deleting'
    if (
      section.lifecycleStatus === lifecycleStatus
      && section.deletionOperationId === action.operationId
      && section.deletionStage === action.stage
      && section.deletionError === action.error
    ) return state
    return {
      ...state,
      fileLibrarySections: {
        ...state.fileLibrarySections,
        [section.id]: {
          ...section,
          deletionError: action.error,
          deletionOperationId: action.operationId,
          deletionStage: action.stage,
          lifecycleStatus,
        },
      },
    }
  }
  if (action.type === 'completeFileLibrarySectionDeletion') {
    const section = state.fileLibrarySections[action.sectionId]
    if (
      !section
      || (
        section.lifecycleStatus !== 'deleting'
        && section.lifecycleStatus !== 'delete_failed'
      )
      || section.deletionOperationId !== action.operationId
    ) return state
    return researchDeskReducer(state, {
      sectionId: action.sectionId,
      type: 'deleteFileLibrarySection',
    })
  }
  if (action.type === 'deleteFileAsset') {
    // The single delete is the batch of one — one implementation, no drift.
    return researchDeskReducer(state, { fileIds: [action.fileId], type: 'deleteFileAssets' })
  }
  if (action.type === 'deleteFileAssets') {
    // Bulk delete (selection mode): ONE state update regardless of count, so
    // deleting hundreds of files costs one render + one autosave diff.
    const removed = new Set(action.fileIds.filter((fileId) => state.fileAssets[fileId]))
    if (removed.size === 0) return state
    const fileAssets = { ...state.fileAssets }
    for (const fileId of removed) delete fileAssets[fileId]
    const keepRef = (ref: ChatContextReferenceRecord) => ref.kind !== 'file-asset' || !removed.has(ref.fileId)
    const { vectorIndexes } = dropFilesFromVectorIndexes(
      state.vectorIndexes,
      removed,
      new Date().toISOString(),
    )
    return {
      ...state,
      dirty: true,
      fileAssetOrder: state.fileAssetOrder.filter((fileId) => !removed.has(fileId)),
      fileAssets,
      ui: {
        ...state.ui,
        pendingChatAttachmentRefs: state.ui.pendingChatAttachmentRefs.filter(keepRef),
      },
      vectorIndexes,
    }
  }
  if (action.type === 'createFileGroup') {
    if (!state.fileLibrarySections[action.sectionId]) return state
    const now = new Date().toISOString()
    const group: FileGroupRecord = {
      createdAt: now,
      id: createId('file-group'),
      sectionId: action.sectionId,
      title: action.title.trim() || 'Neue Gruppe',
      updatedAt: now,
    }
    return {
      ...state,
      dirty: true,
      fileGroupOrder: [group.id, ...state.fileGroupOrder],
      fileGroups: { ...state.fileGroups, [group.id]: group },
    }
  }
  if (action.type === 'renameFileGroup') {
    const group = state.fileGroups[action.groupId]
    const title = action.title.trim()
    if (!group || !title || group.title === title) return state
    return {
      ...state,
      dirty: true,
      fileGroups: {
        ...state.fileGroups,
        [group.id]: { ...group, title, updatedAt: new Date().toISOString() },
      },
    }
  }
  if (action.type === 'deleteFileGroup') {
    if (!state.fileGroups[action.groupId]) return state
    const fileGroups = { ...state.fileGroups }
    delete fileGroups[action.groupId]
    const now = new Date().toISOString()
    const fileAssets = { ...state.fileAssets }
    for (const asset of Object.values(state.fileAssets)) {
      if (asset.groupId === action.groupId) {
        fileAssets[asset.id] = { ...asset, groupId: null, updatedAt: now }
      }
    }
    return {
      ...state,
      dirty: true,
      fileAssets,
      fileGroupOrder: state.fileGroupOrder.filter((groupId) => groupId !== action.groupId),
      fileGroups,
    }
  }
  if (action.type === 'renameFileLibrarySection') {
    const section = state.fileLibrarySections[action.sectionId]
    const title = action.title.trim()
    if (!section || !title || section.title === title) return state
    return {
      ...state,
      dirty: true,
      fileLibrarySections: {
        ...state.fileLibrarySections,
        [section.id]: {
          ...section,
          isBootstrapPlaceholder: false,
          semanticRole: 'custom',
          title,
          updatedAt: new Date().toISOString(),
        },
      },
    }
  }
  if (action.type === 'createFileLibrarySection') {
    const now = new Date().toISOString()
    const section: FileLibrarySectionRecord = {
      createdAt: now,
      id: createId('file-section'),
      kind: 'custom',
      semanticRole: 'custom',
      title: action.title.trim() || 'Neue Sammlung',
      updatedAt: now,
    }
    return {
      ...state,
      dirty: true,
      fileLibrarySectionOrder: [...state.fileLibrarySectionOrder, section.id],
      fileLibrarySections: { ...state.fileLibrarySections, [section.id]: section },
    }
  }
  if (action.type === 'deleteFileLibrarySection') {
    const section = state.fileLibrarySections[action.sectionId]
    if (!section || section.kind === 'temporary') return state
    const now = new Date().toISOString()
    const removedFileIds = new Set(
      Object.values(state.fileAssets)
        .filter((asset) => asset.sectionId === action.sectionId)
        .map((asset) => asset.id),
    )
    const removedGroupIds = new Set(
      Object.values(state.fileGroups)
        .filter((group) => group.sectionId === action.sectionId)
        .map((group) => group.id),
    )
    const fileAssets = { ...state.fileAssets }
    for (const fileId of removedFileIds) delete fileAssets[fileId]
    const fileGroups = { ...state.fileGroups }
    for (const groupId of removedGroupIds) delete fileGroups[groupId]
    const fileLibrarySections = { ...state.fileLibrarySections }
    delete fileLibrarySections[action.sectionId]
    const { vectorIndexes } = dropFilesFromVectorIndexes(state.vectorIndexes, removedFileIds, now)
    const keepRef = (ref: ChatContextReferenceRecord) => {
      if (ref.kind === 'file-asset') return !removedFileIds.has(ref.fileId)
      if (ref.kind === 'file-group') return !removedGroupIds.has(ref.groupId)
      return true
    }
    return {
      ...state,
      dirty: true,
      fileAssetOrder: state.fileAssetOrder.filter((fileId) => !removedFileIds.has(fileId)),
      fileAssets,
      fileGroupOrder: state.fileGroupOrder.filter((groupId) => !removedGroupIds.has(groupId)),
      fileGroups,
      fileLibrarySectionOrder: state.fileLibrarySectionOrder.filter((sectionId) => sectionId !== action.sectionId),
      fileLibrarySections,
      ui: {
        ...state.ui,
        pendingChatAttachmentRefs: state.ui.pendingChatAttachmentRefs.filter(keepRef),
      },
      vectorIndexes,
    }
  }
  if (action.type === 'createVectorIndex') {
    const now = new Date().toISOString()
    const seen = new Set<string>()
    const members: VectorIndexMemberRecord[] = []
    for (const fileId of action.fileIds) {
      if (seen.has(fileId) || !state.fileAssets[fileId]) continue
      seen.add(fileId)
      members.push({ fileId, state: 'pending' })
    }
    const id = createId('vector-index')
    const model = action.model ?? DEFAULT_EMBED_MODEL_ID
    const index: VectorIndexRecord = {
      createdAt: now,
      dims: action.dims ?? dimsForEmbedModel(model),
      handle: uniqueVectorHandle(slugifyVectorHandle(action.title), state),
      id,
      members,
      model,
      status: members.length > 0 ? 'stale' : 'ready',
      title: action.title.trim() || 'Neuer Vektor-Index',
      updatedAt: now,
    }
    return {
      ...state,
      dirty: true,
      vectorIndexOrder: [id, ...state.vectorIndexOrder],
      vectorIndexes: { ...state.vectorIndexes, [id]: index },
    }
  }
  if (action.type === 'renameVectorIndex') {
    const index = state.vectorIndexes[action.indexId]
    const title = action.title.trim()
    if (!index || !title || index.title === title) return state
    return writeVectorIndex(state, {
      ...index,
      handle: uniqueVectorHandle(slugifyVectorHandle(title), state, index.id),
      title,
      updatedAt: new Date().toISOString(),
    })
  }
  if (action.type === 'deleteVectorIndex') {
    if (!state.vectorIndexes[action.indexId]) return state
    const vectorIndexes = { ...state.vectorIndexes }
    delete vectorIndexes[action.indexId]
    return {
      ...state,
      dirty: true,
      vectorIndexOrder: state.vectorIndexOrder.filter((indexId) => indexId !== action.indexId),
      vectorIndexes,
    }
  }
  if (action.type === 'setVectorIndexModel') {
    const index = state.vectorIndexes[action.indexId]
    if (!index || index.model === action.model) return state
    const hasMembers = index.members.length > 0
    return writeVectorIndex(state, {
      ...index,
      dims: action.dims ?? dimsForEmbedModel(action.model),
      members: hasMembers
        ? index.members.map((member): VectorIndexMemberRecord => ({ ...member, state: 'pending' }))
        : index.members,
      model: action.model,
      status: hasMembers ? 'stale' : 'ready',
      updatedAt: new Date().toISOString(),
    })
  }
  if (action.type === 'addDocsToVectorIndex') {
    const index = state.vectorIndexes[action.indexId]
    if (!index) return state
    const have = new Set(index.members.map((member) => member.fileId))
    const additions: VectorIndexMemberRecord[] = []
    for (const fileId of action.fileIds) {
      if (have.has(fileId) || !state.fileAssets[fileId]) continue
      have.add(fileId)
      additions.push({ fileId, state: 'pending' })
    }
    if (additions.length === 0) return state
    return writeVectorIndex(state, {
      ...index,
      members: [...index.members, ...additions],
      status: 'stale',
      updatedAt: new Date().toISOString(),
    })
  }
  if (action.type === 'removeDocFromVectorIndex') {
    const index = state.vectorIndexes[action.indexId]
    if (!index || !index.members.some((member) => member.fileId === action.fileId)) return state
    const members = index.members.filter((member) => member.fileId !== action.fileId)
    const status = members.length === 0
      ? 'ready'
      : members.some((member) => member.state === 'pending')
        ? 'stale'
        : index.status
    return writeVectorIndex(state, {
      ...index,
      members,
      status,
      updatedAt: new Date().toISOString(),
    })
  }
  if (action.type === 'reconcileVectorIndexMemberDocument') {
    const index = state.vectorIndexes[action.indexId]
    if (!index) return state
    const member = index.members.find((entry) => entry.fileId === action.fileId)
    if (
      !member
      || member.serverDocumentId === action.serverDocumentId
      || !action.serverDocumentId
    ) return state
    return writeVectorIndex(state, {
      ...index,
      members: index.members.map((entry) => (
        entry.fileId === action.fileId
          ? { ...entry, serverDocumentId: action.serverDocumentId }
          : entry
      )),
      updatedAt: new Date().toISOString(),
    })
  }
  if (action.type === 'startVectorIndexReindex') {
    const index = state.vectorIndexes[action.indexId]
    if (!index) return state
    const existing = state.indexingJobs[action.indexId]
    if (existing?.jobId === action.jobId) {
      return {
        ...state,
        indexingJobs: {
          ...state.indexingJobs,
          [action.indexId]: {
            ...existing,
            status: action.status ?? existing.status,
            totalDocuments: action.totalDocuments,
          },
        },
      }
    }
    const now = new Date().toISOString()
    const next = writeVectorIndex(state, {
      ...index,
      lastError: null,
      status: 'indexing',
      updatedAt: now,
    })
    return {
      ...next,
      indexingJobs: {
        ...next.indexingJobs,
        [action.indexId]: {
          completedDocuments: 0,
          jobId: action.jobId,
          memberProgress: Object.fromEntries(
            (action.queuedFileIds ?? []).map((fileId) => [
              fileId,
              { status: 'queued' as const },
            ]),
          ),
          percent: 0,
          // The run's working set. A client subset (incremental add) is passed
          // explicitly; the durable server re-embed omits it (the worker, not
          // the client, enumerates the docs) — default to every member, since a
          // re-embed re-vectorizes the whole collection.
          runningFileIds: action.runningFileIds ?? index.members.map((member) => member.fileId),
          source: action.source,
          startedAt: now,
          status: action.status ?? 'running',
          totalDocuments: action.totalDocuments,
        },
      },
    }
  }
  if (action.type === 'markVectorIndexMemberProgress') {
    const live = state.indexingJobs[action.indexId]
    if (!live) return state
    const previous = live.memberProgress?.[action.fileId]
    return {
      ...state,
      indexingJobs: {
        ...state.indexingJobs,
        [action.indexId]: {
          ...live,
          memberProgress: {
            ...live.memberProgress,
            [action.fileId]: {
              ...previous,
              autoResume: action.autoResume,
              currentBatch: action.currentBatch,
              errorType: action.errorType,
              jobId: action.jobId,
              phase: action.phase,
              queuePosition: action.queuePosition,
              status: action.status,
              totalBatches: action.totalBatches,
            },
          },
        },
      },
    }
  }
  if (action.type === 'markVectorIndexQueued') {
    // The server job is waiting for a free slot — surface its FIFO
    // position. Ephemeral, like progress (no writeVectorIndex / dirty).
    const live = state.indexingJobs[action.indexId]
    if (!live) return state
    return {
      ...state,
      indexingJobs: {
        ...state.indexingJobs,
        [action.indexId]: {
          ...live,
          queuePosition: action.queuePosition,
          status: 'queued',
        },
      },
    }
  }
  if (action.type === 'markVectorIndexPaused') {
    const live = state.indexingJobs[action.indexId]
    if (!live || live.source !== 'server') return state
    const percent = action.totalDocuments > 0
      ? Math.round((action.completedDocuments / action.totalDocuments) * 100)
      : live.percent
    return {
      ...state,
      indexingJobs: {
        ...state.indexingJobs,
        [action.indexId]: {
          ...live,
          completedDocuments: action.completedDocuments,
          currentBatch: action.currentBatch,
          memberProgress: Object.fromEntries(
            Object.entries(live.memberProgress ?? {}).concat(
              live.runningFileIds
                .filter((fileId) => !live.memberProgress?.[fileId])
                .map((fileId) => [fileId, { status: action.status }]),
            ).map(([fileId, progress]) => [
              fileId,
              // A rebuild of the whole index has no working set: its rows run
              // from document progress and pause with the one server job.
              live.runningFileIds.includes(fileId)
                || progress.status === 'running'
                || progress.status === 'queued'
                ? {
                    ...progress,
                    currentBatch: action.currentBatch,
                    phase: action.phase,
                    queuePosition: null,
                    status: action.status,
                    totalBatches: action.totalBatches,
                  }
                : progress,
            ]),
          ),
          autoResume: action.autoResume,
          pauseErrorType: action.errorType,
          percent,
          phase: action.phase,
          queuePosition: null,
          status: action.status,
          totalBatches: action.totalBatches,
          totalDocuments: action.totalDocuments,
        },
      },
    }
  }
  if (action.type === 'markVectorIndexRunPaused') {
    // A client-driven run ended with documents waiting: it stays open as one
    // paused run. Paused rows keep their job for "Alle fortsetzen"; members
    // the stopped run never reached leave the queue, since nothing runs them
    // until the user continues.
    const live = state.indexingJobs[action.indexId]
    if (!live) return state
    const pausedRows = Object.entries(live.memberProgress ?? {}).filter(([, progress]) => (
      progress.status === 'paused_dependency'
      || progress.status === 'paused_validation'
    ))
    return {
      ...state,
      indexingJobs: {
        ...state.indexingJobs,
        [action.indexId]: {
          ...live,
          // The run waits for what its first document of the run's own kind of
          // pause waits for, and hears first from the soonest automatic resume.
          autoResume: soonestPlan(pausedRows.map(([, progress]) => progress.autoResume)),
          memberProgress: Object.fromEntries(pausedRows),
          pauseErrorType: (
            pausedRows.find(([, progress]) => progress.status === action.status)
            ?? pausedRows[0]
          )?.[1].errorType ?? null,
          queuePosition: null,
          runningFileIds: [],
          status: action.status,
        },
      },
    }
  }
  if (action.type === 'markVectorIndexResumed') {
    const live = state.indexingJobs[action.indexId]
    if (!live || live.source !== 'server') return state
    return {
      ...state,
      indexingJobs: {
        ...state.indexingJobs,
        [action.indexId]: {
          ...live,
          currentBatch: undefined,
          memberProgress: Object.fromEntries(
            Object.entries(live.memberProgress ?? {}).map(([fileId, progress]) => [
              fileId,
              progress.status === 'paused_dependency'
                || progress.status === 'paused_validation'
                ? {
                    ...progress,
                    autoResume: undefined,
                    currentBatch: undefined,
                    errorType: undefined,
                    queuePosition: null,
                    status: 'running' as const,
                    totalBatches: undefined,
                  }
                : progress,
            ]),
          ),
          autoResume: undefined,
          pauseErrorType: undefined,
          phase: undefined,
          status: 'running',
          totalBatches: undefined,
          totalDocuments: action.totalDocuments,
        },
      },
    }
  }
  if (action.type === 'markVectorIndexSuperseded') {
    const index = state.vectorIndexes[action.indexId]
    if (!index || index.status !== 'indexing') return state
    const status: VectorIndexStatus = index.members.some(
      (member) => member.state === 'pending',
    ) ? 'stale' : 'ready'
    const next = writeVectorIndex(state, {
      ...index,
      status,
      updatedAt: new Date().toISOString(),
    })
    return clearIndexingJob(next, action.indexId)
  }
  if (action.type === 'markVectorIndexProgress') {
    // Hot path: update ONLY the ephemeral live entry — no writeVectorIndex,
    // so the project is never marked dirty by streaming progress.
    const live = state.indexingJobs[action.indexId]
    if (!live) return state
    const percent =
      action.totalDocuments > 0
        ? Math.round((action.completedDocuments / action.totalDocuments) * 100)
        : live.percent
    // Client-build path reports the just-confirmed member (fileId) so the file
    // list can flip that row to its real outcome live. The durable server-job
    // path omits fileId (it streams only counts) — then leave the sets as-is.
    // A member is sorted into a set ONLY on an explicit outcome: an event that
    // merely announces which document started (title + counts, no outcome)
    // must never mark that member skipped.
    const embeddedFileIds =
      action.fileId && action.embedded === true
        ? [...(live.embeddedFileIds ?? []), action.fileId]
        : live.embeddedFileIds
    const skippedFileIds =
      action.fileId && action.embedded === false
        ? [...(live.skippedFileIds ?? []), action.fileId]
        : live.skippedFileIds
    return {
      ...state,
      indexingJobs: {
        ...state.indexingJobs,
        [action.indexId]: {
          ...live,
          completedDocuments: action.completedDocuments,
          currentDocumentTitle: action.currentDocumentTitle,
          currentBatch: undefined,
          embeddedFileIds,
          // The client-driven run reports which members are ACTUALLY in
          // flight; only those may read "läuft". A queued member keeps its
          // pending state instead of pulsing for the whole run.
          ...(action.runningFileIds ? { runningFileIds: action.runningFileIds } : {}),
          percent,
          // Progress means a slot freed up and the job is running now —
          // clear any queued position so the UI leaves the waiting state.
          queuePosition: null,
          autoResume: undefined,
          pauseErrorType: undefined,
          phase: undefined,
          skippedFileIds,
          status: 'running',
          totalBatches: undefined,
          totalDocuments: action.totalDocuments,
        },
      },
    }
  }
  if (action.type === 'markVectorIndexDocumentEmbedded') {
    // The durable server re-embed confirms documents by their backend id (the
    // SSE per-document event). Resolve it to the local file via the member's
    // serverDocumentId and flip just that row live — same ephemeral
    // `embeddedFileIds` channel the client-build path uses, so no dirty.
    const live = state.indexingJobs[action.indexId]
    const index = state.vectorIndexes[action.indexId]
    if (!live || !index) return state
    const matched = index.members.find(
      (member) => member.serverDocumentId === action.serverDocumentId,
    )
    // No tracked id (older index) → cannot map; the row flips at completion.
    if (!matched || live.embeddedFileIds?.includes(matched.fileId)) return state
    return {
      ...state,
      indexingJobs: {
        ...state.indexingJobs,
        [action.indexId]: {
          ...live,
          embeddedFileIds: [...(live.embeddedFileIds ?? []), matched.fileId],
        },
      },
    }
  }
  if (action.type === 'adoptVectorIndexPartialResult') {
    const index = state.vectorIndexes[action.indexId]
    if (!index || index.status !== 'indexing') return state
    const members = reconcileVectorIndexMembers(
      index.members,
      action.embeddedFileIds,
      action.skippedFileIds,
      action.serverDocumentIds,
    )
    return writeVectorIndex(state, {
      ...index,
      members,
      serverCollectionId: action.serverCollectionId,
      serverCollectionModel: action.serverCollectionModel,
      updatedAt: new Date().toISOString(),
    })
  }
  if (action.type === 'completeVectorIndexReindex') {
    const index = state.vectorIndexes[action.indexId]
    if (!index || index.status !== 'indexing') return state
    const now = new Date().toISOString()
    const live = state.indexingJobs[action.indexId]
    // `embeddedFileIds` is the COMPLETE set of members now in the collection
    // (client-driven build/incremental): members in it are embedded. Members in
    // `skippedFileIds` carried no extractable text — TERMINAL 'skipped' (can
    // never embed), distinct from genuinely-not-yet-ingested members which stay
    // 'pending'. The index is honestly 'ready' once nothing is 'pending'
    // (skipped is an accepted terminal outcome, never a false "ready" for a doc
    // that still owes vectors). When `embeddedFileIds` is absent (durable
    // re-embed of an existing collection) every member was re-embedded.
    const embeddedSet = action.embeddedFileIds ? new Set(action.embeddedFileIds) : null
    const members = action.embeddedFileIds
      ? reconcileVectorIndexMembers(
          index.members,
          action.embeddedFileIds,
          action.skippedFileIds ?? [],
          action.serverDocumentIds ?? {},
        )
      : index.members.map((member): VectorIndexMemberRecord => ({
          ...member,
          serverDocumentId:
            action.serverDocumentIds?.[member.fileId]
            ?? member.serverDocumentId,
          // Durable re-embed re-vectorizes existing documents in place; a
          // no-text 'skipped' member has no document to re-embed, so it stays
          // skipped rather than falsely flipping to embedded.
          state: member.state === 'skipped' ? 'skipped' : 'embedded',
        }))
    // Ready once nothing is genuinely pending; embedded + skipped are both
    // terminal (matches the manifest's vectorIndexStatusOrDefault rule).
    const nothingPending = members.every((member) => member.state !== 'pending')
    const next = writeVectorIndex(state, {
      ...index,
      history: appendVectorIndexHistory(index, {
        documents: embeddedSet ? embeddedSet.size : (live?.totalDocuments ?? index.members.length),
        durationMs: runDurationMs(live, now),
        finishedAt: now,
        // A cancelled run reconciles through here too: what embedded is real
        // and is adopted, but the history must say the run was stopped.
        result: action.result ?? 'ok',
        startedAt: live?.startedAt ?? now,
      }),
      lastError: null,
      members,
      serverCollectionId: action.serverCollectionId ?? index.serverCollectionId ?? null,
      serverCollectionModel:
        action.serverCollectionModel ?? index.serverCollectionModel ?? null,
      status: nothingPending ? 'ready' : 'stale',
      updatedAt: now,
    })
    return clearIndexingJob(next, action.indexId)
  }
  if (action.type === 'markVectorIndexError') {
    const index = state.vectorIndexes[action.indexId]
    // Same precondition as completion: a terminal transition is only valid
    // while a run is in flight. Without it, a late/duplicate terminal
    // callback (resume race, double cancel) would append a garbage history
    // row and flip a finished index back to error.
    if (!index || index.status !== 'indexing') return state
    const now = new Date().toISOString()
    const live = state.indexingJobs[action.indexId]
    const next = writeVectorIndex(state, {
      ...index,
      history: appendVectorIndexHistory(index, {
        documents: live?.completedDocuments ?? 0,
        durationMs: runDurationMs(live, now),
        error: action.message,
        finishedAt: now,
        result: 'error',
        startedAt: live?.startedAt ?? now,
      }),
      lastError: action.message,
      serverCollectionId:
        action.serverCollectionId ?? index.serverCollectionId ?? null,
      serverCollectionModel:
        action.serverCollectionModel ?? index.serverCollectionModel ?? null,
      status: 'error',
      updatedAt: now,
    })
    return clearIndexingJob(next, action.indexId)
  }
  if (action.type === 'markVectorIndexCancelled') {
    const index = state.vectorIndexes[action.indexId]
    if (!index || index.status !== 'indexing') return state
    const now = new Date().toISOString()
    const live = state.indexingJobs[action.indexId]
    // Restore the pre-reindex status: stale if any member still needs
    // embedding, else ready. The record's member states were untouched
    // during the run, so this reflects reality without storing it.
    const status: VectorIndexStatus = index.members.some(
      (member) => member.state === 'pending',
    )
      ? 'stale'
      : 'ready'
    const next = writeVectorIndex(state, {
      ...index,
      history: appendVectorIndexHistory(index, {
        documents: live?.completedDocuments ?? 0,
        durationMs: runDurationMs(live, now),
        finishedAt: now,
        result: 'cancelled',
        startedAt: live?.startedAt ?? now,
      }),
      status,
      updatedAt: now,
    })
    return clearIndexingJob(next, action.indexId)
  }
  if (action.type === 'upsertChatRule') {
    const now = new Date().toISOString()
    const existing = state.chatRules[action.rule.id]
    const rule = normalizeChatRule({
      ...action.rule,
      createdAt: existing?.createdAt ?? action.rule.createdAt,
      updatedAt: action.rule.updatedAt || now,
    })
    const chatRuleOrder = state.chatRuleOrder.includes(rule.id)
      ? state.chatRuleOrder
      : [rule.id, ...state.chatRuleOrder]
    return {
      ...state,
      chatRuleOrder,
      chatRules: {
        ...state.chatRules,
        [rule.id]: rule,
      },
      dirty: true,
    }
  }
  if (action.type === 'deleteChatRule') {
    if (!state.chatRules[action.ruleId]) return state
    const chatRules = { ...state.chatRules }
    delete chatRules[action.ruleId]
    return {
      ...state,
      chatRuleOrder: state.chatRuleOrder.filter((ruleId) => ruleId !== action.ruleId),
      chatRules,
      dirty: true,
      ui: {
        ...state.ui,
        pendingChatAttachmentRefs: state.ui.pendingChatAttachmentRefs.filter((ref) => (
          ref.kind !== 'chat-rule' || ref.ruleId !== action.ruleId
        )),
      },
    }
  }
  if (action.type === 'createChatThreadGroup') {
    const now = new Date().toISOString()
    const group: ChatThreadGroupRecord = {
      createdAt: now,
      id: createId('chat-group'),
      title: action.title.trim() || 'New group',
      updatedAt: now,
    }
    return {
      ...state,
      chatThreadGroupOrder: [group.id, ...state.chatThreadGroupOrder],
      chatThreadGroups: {
        ...state.chatThreadGroups,
        [group.id]: group,
      },
      dirty: true,
    }
  }
  if (action.type === 'renameChatThreadGroup') {
    const group = state.chatThreadGroups[action.groupId]
    const title = action.title.trim()
    if (!group || !title || group.title === title) return state
    return {
      ...state,
      chatThreadGroups: {
        ...state.chatThreadGroups,
        [group.id]: {
          ...group,
          title,
          updatedAt: new Date().toISOString(),
        },
      },
      dirty: true,
    }
  }
  if (action.type === 'deleteChatThreadGroup') {
    if (!state.chatThreadGroups[action.groupId]) return state
    const chatThreadGroups = { ...state.chatThreadGroups }
    delete chatThreadGroups[action.groupId]
    const chatThreadGroupMemberships = Object.fromEntries(
      Object.entries(state.chatThreadGroupMemberships).filter(([, groupId]) => groupId !== action.groupId),
    )
    return {
      ...state,
      chatThreadGroupMemberships,
      chatThreadGroupOrder: state.chatThreadGroupOrder.filter((groupId) => groupId !== action.groupId),
      chatThreadGroups,
      dirty: true,
    }
  }
  if (action.type === 'moveChatThreadGroup') {
    return moveChatThreadGroup(state, action.groupId, action.targetIndex)
  }
  if (action.type === 'moveChatThreadToGroup') {
    return moveChatThreadToGroup(state, action.threadId, action.groupId, action.targetIndex)
  }
  if (action.type === 'createChatThread') {
    const thread = createChatThread({
      preview: action.preview,
      title: action.title,
    })
    const groupId = action.groupId && state.chatThreadGroups[action.groupId]
      ? action.groupId
      : null
    const chatThreadGroupMemberships = groupId
      ? { ...state.chatThreadGroupMemberships, [thread.id]: groupId }
      : state.chatThreadGroupMemberships
    const chatThreadOrder = groupId
      ? insertThreadIntoSection(state, chatThreadGroupMemberships, thread.id, groupId, 0)
      : [thread.id, ...state.chatThreadOrder]

    return {
      ...state,
      chatThreadGroupMemberships,
      chatThreadOrder,
      chatThreads: {
        ...state.chatThreads,
        [thread.id]: thread,
      },
      dirty: true,
      ui: {
        ...state.ui,
        // A new chat starts on the account preference, not on whatever the
        // previous chat was switched to. Clearing model and effort alongside
        // keeps the exclusivity contract the pickers rely on.
        selectedChatEffort: null,
        selectedChatModel: null,
        selectedChatModelTier: action.modelTier ?? null,
        selectedChatThreadId: thread.id,
      },
    }
  }
  if (action.type === 'renameChatThread') {
    const thread = state.chatThreads[action.threadId]
    const title = action.title.trim()
    if (!thread || !title || (thread.title === title && thread.titleSource === 'manual' && !thread.titleConflict)) return state

    const updatedAt = new Date().toISOString()
    return {
      ...state,
      chatThreads: {
        ...state.chatThreads,
        [thread.id]: {
          ...thread,
          title,
          titleSource: 'manual', titlePending: true, titleEditId: createId('title-edit'),
          titleRequest: undefined, titleGenerationReason: undefined, titleConflict: undefined,
          updatedAt,
        },
      },
      dirty: true,
    }
  }
  if (action.type === 'clearChatThread') {
    const thread = state.chatThreads[action.threadId]
    if (!thread) return state
    const updatedAt = new Date().toISOString()
    return {
      ...state,
      chatThreads: {
        ...state.chatThreads,
        [thread.id]: {
          ...thread,
          messages: [],
          preview: action.emptyPreview,
          updatedAt,
        },
      },
      dirty: true,
      ui: {
        ...state.ui,
        pendingChatAttachmentRefs: [],
        pendingChatReportRunId: null,
      },
    }
  }
  if (action.type === 'deleteChatMessages') {
    return deleteChatMessages(
      state,
      action.threadId,
      action.messageIds,
      action.emptyPreview,
    )
  }
  if (action.type === 'editChatUserMessage') {
    return editChatUserMessage(state, action.threadId, action.messageId, action.contentMarkdown)
  }
  if (action.type === 'branchChatThreadFromMessage') {
    return branchChatThreadFromMessage(state, action.threadId, action.messageId)
  }
  if (action.type === 'startChatExchange') {
    return startChatExchange(state, action)
  }
  if (action.type === 'startChatAssistantResponse') {
    return startChatAssistantResponse(state, action)
  }
  if (action.type === 'startChatAssistantRetry') {
    return startChatAssistantRetry(state, action)
  }
  if (action.type === 'setChatAssistantMessageContent') {
    return setChatAssistantMessageContent(state, action)
  }
  if (action.type === 'setChatThreadDeletion') {
    const thread = state.chatThreads[action.threadId]
    if (!thread) return state
    // Deliberately leaves `updatedAt` untouched: the autosave fingerprint is
    // (groupId, updatedAt), so marking a row as deleting must not look like
    // an edit that needs pushing.
    const next = { ...thread, deletion: action.deletion ?? undefined }
    if (!action.deletion) delete next.deletion
    return {
      ...state,
      chatThreads: { ...state.chatThreads, [action.threadId]: next },
    }
  }

  if (action.type === 'deleteChatThread') {
    const chatThreads = { ...state.chatThreads }
    delete chatThreads[action.threadId]
    const chatThreadGroupMemberships = { ...state.chatThreadGroupMemberships }
    delete chatThreadGroupMemberships[action.threadId]
    const chatThreadOrder = state.chatThreadOrder.filter((threadId) => threadId !== action.threadId)
    const selectedChatThreadId = state.ui.selectedChatThreadId === action.threadId
      ? chatThreadOrder[0] ?? null
      : state.ui.selectedChatThreadId

    return {
      ...state,
      chatThreadGroupMemberships,
      chatThreadOrder,
      chatThreads,
      dirty: true,
      ui: {
        ...state.ui,
        pendingChatAttachmentRefs: [],
        pendingChatReportRunId: null,
        pinnedExplorer: {
          ...state.ui.pinnedExplorer,
          chatThreadIds: removeExplorerPin(state.ui.pinnedExplorer.chatThreadIds, action.threadId),
        },
        selectedChatThreadId,
      },
    }
  }

  return state
}

function deleteChatMessages(
  state: ResearchDeskState,
  threadId: string,
  messageIds: readonly string[],
  emptyPreview: string,
): ResearchDeskState {
  const thread = state.chatThreads[threadId]
  if (!thread || messageIds.length === 0) return state
  const messageIdSet = new Set(messageIds)
  const messages = thread.messages.filter((message) => !messageIdSet.has(message.id))
  if (messages.length === thread.messages.length) return state

  const updatedAt = new Date().toISOString()
  return {
    ...state,
    chatThreads: {
      ...state.chatThreads,
      [thread.id]: threadWithMessages(thread, messages, updatedAt, emptyPreview),
    },
    dirty: true,
  }
}

function editChatUserMessage(
  state: ResearchDeskState,
  threadId: string,
  messageId: string,
  contentMarkdown: string,
): ResearchDeskState {
  const thread = state.chatThreads[threadId]
  const nextContent = contentMarkdown.trim()
  if (!thread || !nextContent) return state
  const currentMessage = thread.messages.find((message) => message.id === messageId)
  if (!currentMessage || currentMessage.role !== 'user' || currentMessage.contentMarkdown === nextContent) {
    return state
  }

  const updatedAt = new Date().toISOString()
  const messages = thread.messages.map((message) => (
    message.id === messageId
      ? { ...message, contentMarkdown: nextContent }
      : message
  ))
  const autoTitle = titleFromMessage(currentMessage.contentMarkdown)
  const initialQuestionEdited = thread.messages.find((message) => message.role === 'user')?.id === messageId
    && thread.titleSource === 'initial'
  const nextTitle = initialQuestionEdited && thread.title === autoTitle
    ? titleFromMessage(nextContent)
    : thread.title

  return {
    ...state,
    chatThreads: {
      ...state.chatThreads,
      [thread.id]: {
        ...threadWithMessages(thread, messages, updatedAt),
        title: nextTitle,
        ...(initialQuestionEdited ? { titleSource: 'manual' as const, titlePending: true,
          titleEditId: createId('title-edit'), titleRequest: undefined } : {}),
      },
    },
    dirty: true,
  }
}

function branchChatThreadFromMessage(
  state: ResearchDeskState,
  threadId: string,
  messageId: string,
): ResearchDeskState {
  const sourceThread = state.chatThreads[threadId]
  if (!sourceThread) return state
  const messageIndex = sourceThread.messages.findIndex((message) => message.id === messageId)
  if (messageIndex === -1) return state

  const messages = sourceThread.messages.slice(0, messageIndex + 1).map(cloneChatMessage)
  const now = new Date().toISOString()
  const firstUserMessage = messages.find((message) => message.role === 'user')
  const thread: ChatThreadRecord = threadWithMessages(
    {
      createdAt: now,
      id: createId('chat'),
      messages,
      preview: sourceThread.preview,
      source: 'api',
      title: firstUserMessage && sourceThread.titleSource === 'initial'
        ? titleFromMessage(firstUserMessage.contentMarkdown)
        : sourceThread.title,
      titleSource: sourceThread.titleSource ?? 'manual', titleRevision: 0,
      updatedAt: now,
    },
    messages,
    now,
  )

  return {
    ...state,
    chatThreadOrder: [thread.id, ...state.chatThreadOrder],
    chatThreads: {
      ...state.chatThreads,
      [thread.id]: thread,
    },
    dirty: true,
    ui: {
      ...state.ui,
      selectedChatThreadId: thread.id,
    },
  }
}

function cloneChatMessage(message: ChatMessageRecord): ChatMessageRecord {
  return {
    ...message,
    attachments: message.attachments ? message.attachments.map((attachment) => ({ ...attachment })) : undefined,
    id: createId('msg'),
    requestContext: message.requestContext
      ? { knowledgeCollectionIds: message.requestContext.knowledgeCollectionIds?.slice() }
      : undefined,
  }
}

function threadWithMessages(
  thread: ChatThreadRecord,
  messages: ChatMessageRecord[],
  updatedAt: string,
  emptyPreview = thread.preview,
): ChatThreadRecord {
  return {
    ...thread,
    messages,
    preview: chatPreviewFromMessages(messages, emptyPreview),
    updatedAt,
  }
}

function chatPreviewFromMessages(
  messages: readonly ChatMessageRecord[],
  emptyPreview: string,
) {
  return [...messages].reverse().find((message) => message.role === 'user')?.contentMarkdown ?? emptyPreview
}

function moveKnowledgeSessionGroup(
  state: ResearchDeskState,
  groupId: string,
  targetIndex: number,
): ResearchDeskState {
  if (!state.knowledgeSessionGroups[groupId]) return state
  const orderWithoutGroup = state.knowledgeSessionGroupOrder.filter((candidateId) => candidateId !== groupId)
  const boundedTargetIndex = Math.max(0, Math.min(orderWithoutGroup.length, targetIndex))
  const knowledgeSessionGroupOrder = [...orderWithoutGroup]
  knowledgeSessionGroupOrder.splice(boundedTargetIndex, 0, groupId)

  if (arraysEqual(knowledgeSessionGroupOrder, state.knowledgeSessionGroupOrder)) return state

  return {
    ...state,
    dirty: true,
    knowledgeSessionGroupOrder,
  }
}

function moveKnowledgeSessionToGroup(
  state: ResearchDeskState,
  sessionId: string,
  requestedGroupId: string | null,
  targetIndex: number,
): ResearchDeskState {
  if (!state.knowledgeSessions[sessionId]) return state
  const groupId = requestedGroupId && state.knowledgeSessionGroups[requestedGroupId]
    ? requestedGroupId
    : null
  const currentGroupId = normalizedKnowledgeSessionGroupId(state, sessionId)
  const currentSectionSessionIds = knowledgeSessionIdsForGroup(state, currentGroupId)
  const currentIndex = currentSectionSessionIds.indexOf(sessionId)
  const targetSessionIds = knowledgeSessionIdsForGroup(state, groupId).filter((id) => id !== sessionId)
  const boundedTargetIndex = Math.max(0, Math.min(targetSessionIds.length, targetIndex))
  if (currentGroupId === groupId && currentIndex === boundedTargetIndex) return state

  const knowledgeSessionGroupMemberships = { ...(state.knowledgeSessionGroupMemberships ?? {}) }
  if (groupId) {
    knowledgeSessionGroupMemberships[sessionId] = groupId
  } else {
    delete knowledgeSessionGroupMemberships[sessionId]
  }
  const knowledgeSessions = currentGroupId === groupId
    ? state.knowledgeSessions
    : {
      ...state.knowledgeSessions,
      [sessionId]: {
        ...state.knowledgeSessions[sessionId],
        updatedAt: new Date().toISOString(),
      },
    }

  return {
    ...state,
    dirty: true,
    knowledgeSessionGroupMemberships,
    knowledgeSessionOrder: insertKnowledgeSessionIntoSection(
      state,
      knowledgeSessionGroupMemberships,
      sessionId,
      groupId,
      boundedTargetIndex,
    ),
    knowledgeSessions,
  }
}

function insertKnowledgeSessionIntoSection(
  state: ResearchDeskState,
  memberships: Record<string, string | null>,
  sessionId: string,
  groupId: string | null,
  targetIndex: number,
) {
  const orderWithoutSession = state.knowledgeSessionOrder.filter((id) => id !== sessionId)
  // Same pinned-row exclusion as insertThreadIntoSection: the DOM drop
  // index never counts pinned rows.
  const pinnedIds = new Set(state.ui.pinnedExplorer.knowledgeSessionIds)
  const targetSessionIds = orderWithoutSession.filter((id) =>
    !pinnedIds.has(id)
    && normalizedKnowledgeSessionGroupId(state, id, memberships) === groupId)
  const beforeSessionId = targetSessionIds[targetIndex]
  if (beforeSessionId) {
    return insertBefore(orderWithoutSession, sessionId, beforeSessionId)
  }

  const previousSessionId = targetSessionIds[targetIndex - 1]
  if (previousSessionId) {
    return insertAfter(orderWithoutSession, sessionId, previousSessionId)
  }

  const sectionInsertionIndex = emptyKnowledgeSessionSectionInsertionIndex(
    state,
    orderWithoutSession,
    memberships,
    groupId,
  )
  const nextOrder = [...orderWithoutSession]
  nextOrder.splice(sectionInsertionIndex, 0, sessionId)
  return nextOrder
}

function emptyKnowledgeSessionSectionInsertionIndex(
  state: ResearchDeskState,
  orderWithoutSession: string[],
  memberships: Record<string, string | null>,
  groupId: string | null,
) {
  const sectionKeys = [
    ...state.knowledgeSessionGroupOrder.filter((candidateId) => Boolean(state.knowledgeSessionGroups[candidateId])),
    null,
  ]
  const targetSectionIndex = Math.max(0, sectionKeys.findIndex((candidateId) => candidateId === groupId))
  for (const nextGroupId of sectionKeys.slice(targetSectionIndex + 1)) {
    const nextSessionId = orderWithoutSession.find((sessionId) => (
      normalizedKnowledgeSessionGroupId(state, sessionId, memberships) === nextGroupId
    ))
    if (nextSessionId) return orderWithoutSession.indexOf(nextSessionId)
  }

  for (const previousGroupId of sectionKeys.slice(0, targetSectionIndex).reverse()) {
    const previousSessionId = [...orderWithoutSession].reverse().find((sessionId) => (
      normalizedKnowledgeSessionGroupId(state, sessionId, memberships) === previousGroupId
    ))
    if (previousSessionId) return orderWithoutSession.indexOf(previousSessionId) + 1
  }

  return orderWithoutSession.length
}

function knowledgeSessionIdsForGroup(
  state: ResearchDeskState,
  groupId: string | null,
) {
  return state.knowledgeSessionOrder.filter((sessionId) => (
    normalizedKnowledgeSessionGroupId(state, sessionId) === groupId
  ))
}

function normalizedKnowledgeSessionGroupId(
  state: ResearchDeskState,
  sessionId: string,
  memberships = state.knowledgeSessionGroupMemberships,
) {
  const groupId = memberships[sessionId]
  return groupId && state.knowledgeSessionGroups[groupId] ? groupId : null
}

function moveChatThreadGroup(
  state: ResearchDeskState,
  groupId: string,
  targetIndex: number,
): ResearchDeskState {
  if (!state.chatThreadGroups[groupId]) return state
  const orderWithoutGroup = state.chatThreadGroupOrder.filter((candidateId) => candidateId !== groupId)
  const boundedTargetIndex = Math.max(0, Math.min(orderWithoutGroup.length, targetIndex))
  const chatThreadGroupOrder = [...orderWithoutGroup]
  chatThreadGroupOrder.splice(boundedTargetIndex, 0, groupId)

  if (arraysEqual(chatThreadGroupOrder, state.chatThreadGroupOrder)) return state

  return {
    ...state,
    chatThreadGroupOrder,
    dirty: true,
  }
}

function moveChatThreadToGroup(
  state: ResearchDeskState,
  threadId: string,
  requestedGroupId: string | null,
  targetIndex: number,
): ResearchDeskState {
  if (!state.chatThreads[threadId]) return state
  const groupId = requestedGroupId && state.chatThreadGroups[requestedGroupId]
    ? requestedGroupId
    : null
  const currentGroupId = normalizedThreadGroupId(state, threadId)
  const currentSectionThreadIds = threadIdsForGroup(state, currentGroupId)
  const currentIndex = currentSectionThreadIds.indexOf(threadId)
  const targetThreadIds = threadIdsForGroup(state, groupId).filter((id) => id !== threadId)
  const boundedTargetIndex = Math.max(0, Math.min(targetThreadIds.length, targetIndex))
  if (currentGroupId === groupId && currentIndex === boundedTargetIndex) return state

  const chatThreadGroupMemberships = { ...state.chatThreadGroupMemberships }
  if (groupId) {
    chatThreadGroupMemberships[threadId] = groupId
  } else {
    delete chatThreadGroupMemberships[threadId]
  }

  return {
    ...state,
    chatThreadGroupMemberships,
    chatThreadOrder: insertThreadIntoSection(
      state,
      chatThreadGroupMemberships,
      threadId,
      groupId,
      boundedTargetIndex,
    ),
    dirty: true,
  }
}

function insertThreadIntoSection(
  state: ResearchDeskState,
  memberships: Record<string, string | null>,
  threadId: string,
  groupId: string | null,
  targetIndex: number,
) {
  const orderWithoutThread = state.chatThreadOrder.filter((id) => id !== threadId)
  // Pinned rows render in the lifted pinned block, so the DOM drop index
  // counts a section WITHOUT them — anchor against the same set or every
  // drop lands off by the pinned count.
  const pinnedIds = new Set(state.ui.pinnedExplorer.chatThreadIds)
  const targetThreadIds = orderWithoutThread.filter((id) =>
    !pinnedIds.has(id)
    && normalizedThreadGroupId(state, id, memberships) === groupId)
  const beforeThreadId = targetThreadIds[targetIndex]
  if (beforeThreadId) {
    return insertBefore(orderWithoutThread, threadId, beforeThreadId)
  }

  const previousThreadId = targetThreadIds[targetIndex - 1]
  if (previousThreadId) {
    return insertAfter(orderWithoutThread, threadId, previousThreadId)
  }

  const sectionInsertionIndex = emptySectionInsertionIndex(state, orderWithoutThread, memberships, groupId)
  const nextOrder = [...orderWithoutThread]
  nextOrder.splice(sectionInsertionIndex, 0, threadId)
  return nextOrder
}

function emptySectionInsertionIndex(
  state: ResearchDeskState,
  orderWithoutThread: string[],
  memberships: Record<string, string | null>,
  groupId: string | null,
) {
  const sectionKeys = [
    ...state.chatThreadGroupOrder.filter((candidateId) => Boolean(state.chatThreadGroups[candidateId])),
    null,
  ]
  const targetSectionIndex = Math.max(0, sectionKeys.findIndex((candidateId) => candidateId === groupId))
  for (const nextGroupId of sectionKeys.slice(targetSectionIndex + 1)) {
    const nextThreadId = orderWithoutThread.find((threadId) => (
      normalizedThreadGroupId(state, threadId, memberships) === nextGroupId
    ))
    if (nextThreadId) return orderWithoutThread.indexOf(nextThreadId)
  }

  for (const previousGroupId of sectionKeys.slice(0, targetSectionIndex).reverse()) {
    const previousThreadId = [...orderWithoutThread].reverse().find((threadId) => (
      normalizedThreadGroupId(state, threadId, memberships) === previousGroupId
    ))
    if (previousThreadId) return orderWithoutThread.indexOf(previousThreadId) + 1
  }

  return orderWithoutThread.length
}

/** Agent twin of insertThreadIntoSection: sessions carry groupId on the
 * record (no membership map), pinned rows are excluded from the visible
 * index just like every other rail. */
function insertAgentSessionIntoSection(
  state: ResearchDeskState,
  sessionId: string,
  groupId: string | null,
  targetIndex: number,
) {
  const orderWithoutSession = state.agentSessionOrder.filter((id) => id !== sessionId)
  const pinnedIds = new Set(state.ui.pinnedExplorer.agentSessionIds)
  const groupOf = (id: string) => {
    const candidate = state.agentSessions[id]?.groupId
    return candidate && state.agentSessionGroups[candidate] ? candidate : null
  }
  const targetSessionIds = orderWithoutSession.filter((id) =>
    !pinnedIds.has(id) && groupOf(id) === groupId)
  const beforeSessionId = targetSessionIds[targetIndex]
  if (beforeSessionId) {
    return insertBefore(orderWithoutSession, sessionId, beforeSessionId)
  }
  const previousSessionId = targetSessionIds[targetIndex - 1]
  if (previousSessionId) {
    return insertAfter(orderWithoutSession, sessionId, previousSessionId)
  }
  const sectionKeys: (string | null)[] = [
    ...state.agentSessionGroupOrder.filter((candidateId) => Boolean(state.agentSessionGroups[candidateId])),
    null,
  ]
  const targetSectionIndex = Math.max(0, sectionKeys.findIndex((candidateId) => candidateId === groupId))
  for (const nextGroupId of sectionKeys.slice(targetSectionIndex + 1)) {
    const nextSessionId = orderWithoutSession.find((id) => groupOf(id) === nextGroupId)
    if (nextSessionId) {
      const nextOrder = [...orderWithoutSession]
      nextOrder.splice(orderWithoutSession.indexOf(nextSessionId), 0, sessionId)
      return nextOrder
    }
  }
  for (const previousGroupId of sectionKeys.slice(0, targetSectionIndex).reverse()) {
    const previousSessionId = [...orderWithoutSession].reverse().find((id) => groupOf(id) === previousGroupId)
    if (previousSessionId) {
      const nextOrder = [...orderWithoutSession]
      nextOrder.splice(orderWithoutSession.indexOf(previousSessionId) + 1, 0, sessionId)
      return nextOrder
    }
  }
  return [...orderWithoutSession, sessionId]
}

function threadIdsForGroup(
  state: ResearchDeskState,
  groupId: string | null,
) {
  return state.chatThreadOrder.filter((threadId) => (
    normalizedThreadGroupId(state, threadId) === groupId
  ))
}

function normalizedThreadGroupId(
  state: ResearchDeskState,
  threadId: string,
  memberships = state.chatThreadGroupMemberships,
) {
  const groupId = memberships[threadId]
  return groupId && state.chatThreadGroups[groupId] ? groupId : null
}

function insertBefore(items: string[], item: string, beforeItem: string) {
  const next = [...items]
  const index = next.indexOf(beforeItem)
  next.splice(index < 0 ? next.length : index, 0, item)
  return next
}

function insertAfter(items: string[], item: string, afterItem: string) {
  const next = [...items]
  const index = next.indexOf(afterItem)
  next.splice(index < 0 ? next.length : index + 1, 0, item)
  return next
}

function arraysEqual(left: readonly string[], right: readonly string[]) {
  return left.length === right.length && left.every((item, index) => item === right[index])
}

function createLocalResearchRun(
  request: CreateResearchRunRequest,
  counter: number,
  fallbackStack: string,
): ResearchRunRecord {
  const now = new Date().toISOString()
  const maxRounds = request.agentOverrides?.maxRounds ?? DEEP_RESEARCH_MAX_ROUNDS
  const firstRoundQueries = request.agentOverrides?.firstRoundQueries ?? 0
  const question = request.question.trim()

  return {
    agentOverrides: request.agentOverrides ?? {},
    createdAt: now,
    events: [],
    metrics: {
      claims: 0,
      queries: firstRoundQueries,
      rounds: `0 / ${maxRounds}`,
      sources: 0,
    },
    phaseState: {
      activePhase: 'analysis',
      completedPhases: [],
    },
    runId: `RO-${counter.toString().padStart(4, '0')}`,
    source: 'mock',
    stack: request.stack ?? fallbackStack,
    status: 'queued',
    submittedAt: now,
    summary: {
      queueNote: 'Local draft',
      title: question,
    },
  }
}

function createChatThread(options: {
  id?: string
  preview: string
  source?: ChatThreadRecord['source']
  title: string
}): ChatThreadRecord {
  const now = new Date().toISOString()

  return {
    createdAt: now,
    id: options.id ?? createId('chat'),
    messages: [],
    preview: options.preview,
    source: options.source ?? 'api',
    title: options.title,
    titleSource: 'initial', titleRevision: 0,
    updatedAt: now,
  }
}

/** Keep persisted turns semantic when an older client stored both roles at
 * one instant; the database id is only a pagination tiebreaker. */
function compareChatMessagesChronologically(
  left: ChatMessageRecord,
  right: ChatMessageRecord,
): number {
  const createdOrder = left.createdAt.localeCompare(right.createdAt)
  if (createdOrder !== 0) return createdOrder
  if (left.role !== right.role) return left.role === 'user' ? -1 : 1
  return left.id.localeCompare(right.id)
}

/** Give the assistant placeholder its own stable ordering instant. Its
 * streamed content may change later, but its position in the turn must not. */
function assistantTimestampAfter(userCreatedAt: string): string {
  const userTimestamp = Date.parse(userCreatedAt)
  if (!Number.isFinite(userTimestamp)) {
    throw new RangeError('Chat message createdAt must be a valid ISO timestamp')
  }
  return new Date(userTimestamp + 1).toISOString()
}

function startChatExchange(
  state: ResearchDeskState,
  action: Extract<ResearchDeskAction, { type: 'startChatExchange' }>,
): ResearchDeskState {
  const trimmedContent = action.contentMarkdown.trim()
  if (!trimmedContent) return state

  const thread = state.chatThreads[action.threadId] ?? createChatThread({
    id: action.threadId,
    preview: trimmedContent,
    source: 'api',
    title: titleFromMessage(trimmedContent),
  })
  const isNewThread = !state.chatThreads[action.threadId]
  const report = state.ui.pendingChatReportRunId
    ? reportFromRun(state, state.ui.pendingChatReportRunId)
    : null
  const attachmentRefs = dedupePendingAttachmentRefs([
    ...state.ui.pendingChatAttachmentRefs,
    ...(action.attachmentRefs ?? []),
  ])
  const attachments = createMessageAttachments(state, attachmentRefs, action.createdAt)
  const legacyAttachments = attachments.length > 0
    ? attachments
    : report
      ? [createReportAttachment(report, action.createdAt)]
      : undefined
  const userMessage = {
    attachments: legacyAttachments,
    contentMarkdown: trimmedContent,
    createdAt: action.createdAt,
    id: action.userMessageId,
    role: 'user' as const,
  }
  const assistantMessage = {
    contentMarkdown: '',
    createdAt: assistantTimestampAfter(action.createdAt),
    id: action.assistantMessageId,
    modelResolution: action.modelResolution,
    requestContext: action.requestContext,
    role: 'assistant' as const,
  }
  const nextThread: ChatThreadRecord = {
    ...thread,
    messages: [...thread.messages, userMessage, assistantMessage],
    preview: trimmedContent,
    source: 'api',
    title: thread.messages.some((message) => message.role === 'user') || thread.titleSource === 'manual'
      ? thread.title : titleFromMessage(trimmedContent),
    ...(!thread.messages.some((message) => message.role === 'user') && thread.titleSource === 'initial'
      ? { titleRequest: { id: createId('title'), question: trimmedContent, language: action.titleLanguage, stack: action.titleStack },
          ...(thread.title !== titleFromMessage(trimmedContent)
            ? { titlePending: true, titleEditId: createId('title-edit') } : {}) }
      : {}),
    updatedAt: action.createdAt,
  }

  return {
    ...state,
    chatThreadOrder: isNewThread
      ? [nextThread.id, ...state.chatThreadOrder]
      : state.chatThreadOrder,
    chatThreads: {
      ...state.chatThreads,
      [nextThread.id]: nextThread,
    },
    dirty: true,
    ui: {
      ...state.ui,
      pendingChatAttachmentRefs: [],
      pendingChatReportRunId: null,
      selectedChatThreadId: nextThread.id,
    },
  }
}

function startChatAssistantResponse(
  state: ResearchDeskState,
  action: Extract<ResearchDeskAction, { type: 'startChatAssistantResponse' }>,
): ResearchDeskState {
  const thread = state.chatThreads[action.threadId]
  const lastMessage = thread?.messages[thread.messages.length - 1]
  if (!thread || !lastMessage || lastMessage.id !== action.userMessageId || lastMessage.role !== 'user') {
    return state
  }

  const assistantMessage: ChatMessageRecord = {
    contentMarkdown: '',
    createdAt: action.createdAt,
    id: action.assistantMessageId,
    modelResolution: action.modelResolution,
    requestContext: action.requestContext,
    role: 'assistant',
  }

  return {
    ...state,
    chatThreads: {
      ...state.chatThreads,
      [thread.id]: threadWithMessages(thread, [...thread.messages, assistantMessage], action.createdAt),
    },
    dirty: true,
  }
}

function startChatAssistantRetry(
  state: ResearchDeskState,
  action: Extract<ResearchDeskAction, { type: 'startChatAssistantRetry' }>,
): ResearchDeskState {
  const thread = state.chatThreads[action.threadId]
  if (!thread) return state

  const assistantIndex = thread.messages.findIndex((message) => (
    message.id === action.replacedAssistantMessageId
  ))
  const assistantMessage = assistantIndex >= 0 ? thread.messages[assistantIndex] : undefined
  const userMessage = assistantIndex > 0 ? thread.messages[assistantIndex - 1] : undefined
  if (
    !assistantMessage
    || assistantMessage.role !== 'assistant'
    || !userMessage
    || userMessage.role !== 'user'
    || !userMessage.contentMarkdown.trim()
  ) {
    return state
  }

  const retryMessage: ChatMessageRecord = {
    contentMarkdown: '',
    createdAt: action.createdAt,
    id: action.assistantMessageId,
    modelResolution: action.modelResolution,
    requestContext: action.requestContext,
    role: 'assistant',
  }
  const messages = [...thread.messages.slice(0, assistantIndex), retryMessage]

  return {
    ...state,
    chatThreads: {
      ...state.chatThreads,
      [thread.id]: threadWithMessages(thread, messages, action.createdAt),
    },
    dirty: true,
  }
}

function setChatAssistantMessageContent(
  state: ResearchDeskState,
  action: Extract<ResearchDeskAction, { type: 'setChatAssistantMessageContent' }>,
): ResearchDeskState {
  const thread = state.chatThreads[action.threadId]
  if (!thread) return state
  // A thread awaiting its server deletion accepts no further content. The
  // abort of its in-flight generation lands here with the "generation
  // stopped" text, and writing it would bump `updatedAt` — the autosave
  // fingerprint — so the next flush would push the thread back to a server
  // that just deleted it.
  if (thread.deletion) return state

  const messageIndex = thread.messages.findIndex((message) => message.id === action.assistantMessageId)
  if (messageIndex === -1) return state

  const messages = thread.messages.map((message) => (
    message.id === action.assistantMessageId
      ? {
        ...message,
        chainTrace: action.chainTrace ?? message.chainTrace,
        contentMarkdown: action.contentMarkdown,
        modelResolution: action.modelResolution ?? message.modelResolution,
      }
      : message
  ))
  const updatedAt = new Date().toISOString()

  return {
    ...state,
    chatThreads: {
      ...state.chatThreads,
      [thread.id]: {
        ...thread,
        messages,
        updatedAt,
      },
    },
    dirty: true,
  }
}

function createEditorDocument(options: {
  contentMarkdown?: string
  folderId?: string | null
  source: EditorDocumentRecord['source']
  sourceRunId?: string
  title?: string
}): EditorDocumentRecord {
  const now = new Date().toISOString()
  return {
    contentMarkdown: options.contentMarkdown ?? '',
    createdAt: now,
    folderId: options.folderId ?? null,
    id: createId('editor-doc'),
    // Base 0 = never synced to the server; the first save sends base+1 and
    // INSERTs at revision 1. Revision then tracks the server, not local edits.
    revision: 0,
    source: options.source,
    sourceRunId: options.sourceRunId,
    title: normalizeEditorDocumentTitle(options.title ?? 'Untitled.md'),
    updatedAt: now,
  }
}

function createEditorRecoveryDocument(
  source: Pick<EditorDocumentRecord, 'id' | 'source' | 'sourceRunId' | 'title'>,
  capture: EditorDocumentRecoveryCapture,
  reason: EditorDocumentRecoveryReason,
): EditorDocumentRecord {
  return {
    contentMarkdown: capture.contentMarkdown,
    createdAt: capture.capturedAt,
    folderId: null,
    id: createId('editor-recovery'),
    recovery: {
      capturedAt: capture.capturedAt,
      originalDocumentId: source.id,
      reason,
    },
    revision: 0,
    source: source.source,
    ...(source.sourceRunId ? { sourceRunId: source.sourceRunId } : {}),
    title: source.title,
    updatedAt: capture.capturedAt,
  }
}

function remapRecoverySuggestionOrigin(
  origin: EditorSuggestionOrigin,
  commentIds: ReadonlyMap<string, string>,
): EditorSuggestionOrigin | null {
  if (!origin.commentId) return origin
  const commentId = commentIds.get(origin.commentId)
  if (!commentId) {
    return origin.kind === 'global_run' ? { kind: 'global_run' } : null
  }
  return { ...origin, commentId }
}

function mergeServerEditorDocumentMetadata(
  local: EditorDocumentRecord,
  incoming: EditorDocumentRecord,
): EditorDocumentRecord {
  if (incoming.updatedAt > local.updatedAt) {
    return {
      ...incoming,
      contentMarkdown: local.contentMarkdown,
      serverSynced: true,
    }
  }
  return {
    ...incoming,
    ...local,
    access: incoming.access,
    collaboration: incoming.collaboration,
    contentMode: incoming.contentMode,
    metadataRevision: incoming.metadataRevision,
    serverSynced: true,
  }
}

function withEditorDocument(
  state: ResearchDeskState,
  document: EditorDocumentRecord,
  options: { activeView?: AppView } = {},
): ResearchDeskState {
  return {
    ...state,
    dirty: true,
    editorDocumentOrder: [document.id, ...state.editorDocumentOrder],
    editorDocuments: {
      ...state.editorDocuments,
      [document.id]: document,
    },
    editorUi: {
      ...state.editorUi,
      activeDocumentId: document.id,
      openDocumentIds: addOpenEditorDocumentId(state.editorUi.openDocumentIds, document.id),
      viewMode: 'live',
    },
    ui: {
      ...state.ui,
      activeView: options.activeView ?? state.ui.activeView,
    },
  }
}

function moveEditorFolder(
  state: ResearchDeskState,
  folderId: string,
  targetIndex: number,
): ResearchDeskState {
  if (!state.editorFolders[folderId]) return state
  const orderWithoutFolder = state.editorFolderOrder.filter((candidateId) => candidateId !== folderId)
  const boundedTargetIndex = Math.max(0, Math.min(orderWithoutFolder.length, targetIndex))
  const editorFolderOrder = [...orderWithoutFolder]
  editorFolderOrder.splice(boundedTargetIndex, 0, folderId)

  if (arraysEqual(editorFolderOrder, state.editorFolderOrder)) return state

  return {
    ...state,
    dirty: true,
    editorFolderOrder,
  }
}

function moveEditorDocumentToFolder(
  state: ResearchDeskState,
  documentId: string,
  requestedFolderId: string | null,
  targetIndex: number,
): ResearchDeskState {
  const document = state.editorDocuments[documentId]
  if (!document) return state
  const folderId = requestedFolderId && state.editorFolders[requestedFolderId]
    ? requestedFolderId
    : null
  const currentFolderId = document.folderId && state.editorFolders[document.folderId]
    ? document.folderId
    : null
  // Visible-index math excludes pinned documents (they render in the
  // lifted Pinned section, never inside their folder).
  const pinnedDocumentIds = new Set(state.ui.pinnedExplorer.editorDocumentIds)
  const currentSectionDocumentIds = documentIdsForEditorFolder(state.editorDocumentOrder, state.editorDocuments, currentFolderId)
    .filter((id) => !pinnedDocumentIds.has(id))
  const currentIndex = currentSectionDocumentIds.indexOf(documentId)
  const targetDocumentIds = documentIdsForEditorFolder(state.editorDocumentOrder, state.editorDocuments, folderId)
    .filter((id) => id !== documentId && !pinnedDocumentIds.has(id))
  const boundedTargetIndex = Math.max(0, Math.min(targetDocumentIds.length, targetIndex))
  if (currentFolderId === folderId && currentIndex === boundedTargetIndex) return state

  return {
    ...state,
    dirty: true,
    editorDocumentOrder: moveSectionDocumentIds(
      state.editorDocumentOrder,
      state.editorDocuments,
      currentFolderId,
      folderId,
      boundedTargetIndex,
      documentId,
      state.ui.pinnedExplorer.editorDocumentIds,
    ),
    editorDocuments: {
      ...state.editorDocuments,
      [document.id]: {
        ...document,
        folderId,
        // updatedAt only (autosave trigger); revision stays the server base.
        updatedAt: new Date().toISOString(),
      },
    },
  }
}

function moveSectionDocumentIds(
  documentOrder: readonly string[],
  documents: ResearchDeskState['editorDocuments'],
  currentFolderId: string | null,
  targetFolderId: string | null,
  targetIndex: number,
  movedDocumentId?: string,
  pinnedDocumentIds: readonly string[] = [],
): string[] {
  const movedIds = movedDocumentId
    ? [movedDocumentId]
    : documentOrder.filter((documentId) => documents[documentId]?.folderId === currentFolderId)
  const orderWithoutMovedIds = documentOrder.filter((documentId) => !movedIds.includes(documentId))
  // Pinned documents render in the lifted Pinned section but keep their
  // folderId — exclude them so the DOM drop index (which never counts
  // them) anchors against the same visible set.
  const pinnedIds = new Set(pinnedDocumentIds)
  const targetSectionIds = orderWithoutMovedIds.filter((documentId) => {
    const document = documents[documentId]
    return Boolean(document)
      && !pinnedIds.has(documentId)
      && (document.folderId ?? null) === targetFolderId
  })
  const boundedTargetIndex = Math.max(0, Math.min(targetSectionIds.length, targetIndex))
  const anchorId = targetSectionIds[boundedTargetIndex] ?? null
  const insertionIndex = anchorId
    ? orderWithoutMovedIds.indexOf(anchorId)
    : lastSectionInsertionIndex(orderWithoutMovedIds, documents, targetFolderId)
  const nextOrder = [...orderWithoutMovedIds]
  nextOrder.splice(insertionIndex, 0, ...movedIds)
  return nextOrder
}

function lastSectionInsertionIndex(
  documentOrder: readonly string[],
  documents: ResearchDeskState['editorDocuments'],
  folderId: string | null,
) {
  const sectionIndexes = documentOrder.flatMap((documentId, index) => (
    (documents[documentId]?.folderId ?? null) === folderId ? [index] : []
  ))
  return sectionIndexes.length > 0
    ? Math.max(...sectionIndexes) + 1
    : documentOrder.length
}

function documentIdsForEditorFolder(
  documentOrder: readonly string[],
  documents: ResearchDeskState['editorDocuments'],
  folderId: string | null,
) {
  return documentOrder.filter((documentId) => (documents[documentId]?.folderId ?? null) === folderId)
}

function addOpenEditorDocumentId(openDocumentIds: readonly string[], documentId: string) {
  return openDocumentIds.includes(documentId)
    ? [...openDocumentIds]
    : [...openDocumentIds, documentId]
}

function normalizeEditorDocumentTitle(value: string) {
  const normalized = value.replace(/\s+/g, ' ').trim() || 'Untitled'
  return normalized.endsWith('.md') ? normalized : `${normalized}.md`
}

function reportFromRun(state: ResearchDeskState, runId: string) {
  const run = state.researchRuns[runId]
  const markdown = run?.result?.markdown
  if (!run || run.status !== 'completed' || !markdown) return null

  return {
    contentMarkdown: markdown,
    label: reportLabel(run.summary.title, run.runId),
    runId: run.runId,
    title: run.summary.title,
  }
}

function createReportAttachment(
  report: {
    contentMarkdown: string
    label?: string
    runId: string
    title: string
  },
  attachedAt: string,
): ChatMessageAttachmentRecord {
  return {
    attachedAt,
    contentMarkdown: report.contentMarkdown,
    kind: 'research-report',
    label: report.label,
    runId: report.runId,
    title: report.title,
  }
}

function createRuleAttachment(
  rule: ChatRuleRecord,
  attachedAt: string,
  contentMarkdown: string,
): ChatMessageAttachmentRecord {
  return {
    attachedAt,
    contentMarkdown,
    kind: 'chat-rule',
    label: rule.label,
    ruleId: rule.id,
    title: rule.title,
  }
}

function createMessageAttachments(
  state: ResearchDeskState,
  refs: ChatContextReferenceRecord[],
  attachedAt: string,
): ChatMessageAttachmentRecord[] {
  const seen = new Set<string>()
  return refs.flatMap<ChatMessageAttachmentRecord>((ref) => {
    if (ref.kind === 'file-group') {
      const group = state.fileGroups[ref.groupId]
      if (!group) return []
      const members = state.fileAssetOrder
        .map((fileId) => state.fileAssets[fileId])
        .filter((asset): asset is FileAssetRecord => Boolean(asset))
        .filter((asset) => asset.groupId === ref.groupId)
      return members.flatMap<ChatMessageAttachmentRecord>((asset) => {
        const memberKey = `file-asset:${asset.id}`
        if (seen.has(memberKey)) return []
        seen.add(memberKey)
        return [{
          attachedAt,
          contentMarkdown: asset.extractedText,
          fileId: asset.id,
          groupId: group.id,
          groupLabel: group.title,
          kind: 'file-group' as const,
          label: asset.label,
          pageCount: asset.pageCount,
          sizeBytes: asset.sizeBytes,
          title: asset.title,
        }]
      })
    }
    const key = chatContextRefKey(ref)
    if (seen.has(key)) return []
    seen.add(key)
    if (ref.kind === 'research-report') {
      const report = reportFromRun(state, ref.runId)
      return report ? [createReportAttachment(report, attachedAt)] : []
    }
    if (ref.kind === 'file-asset') {
      const asset = state.fileAssets[ref.fileId]
      if (!asset) return []
      return [{
        attachedAt,
        contentMarkdown: asset.extractedText,
        fileId: asset.id,
        kind: 'file-asset' as const,
        label: asset.label,
        pageCount: asset.pageCount,
        sizeBytes: asset.sizeBytes,
        title: asset.title,
      }]
    }
    const rule = state.chatRules[ref.ruleId]
    return rule
      ? [createRuleAttachment(
        normalizeChatRule(rule),
        attachedAt,
        renderChatRuleAttachmentContent(state, rule, attachedAt),
      )]
      : []
  })
}

function contextRefExists(
  state: ResearchDeskState,
  ref: ChatContextReferenceRecord,
) {
  switch (ref.kind) {
    case 'research-report':
      return Boolean(reportFromRun(state, ref.runId))
    case 'file-asset':
      return Boolean(state.fileAssets[ref.fileId])
    case 'file-group':
      return Boolean(state.fileGroups[ref.groupId])
    case 'chat-rule':
      return Boolean(state.chatRules[ref.ruleId])
  }
}

function addPendingAttachmentRef(
  current: ChatContextReferenceRecord[],
  ref: ChatContextReferenceRecord,
) {
  return dedupePendingAttachmentRefs([...current, ref])
}

function dedupePendingAttachmentRefs(refs: ChatContextReferenceRecord[]) {
  const seen = new Set<string>()
  return refs.filter((ref) => {
    const key = chatContextRefKey(ref)
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function chatContextRefKey(ref: ChatContextReferenceRecord) {
  switch (ref.kind) {
    case 'research-report':
      return `research-report:${ref.runId}`
    case 'file-asset':
      return `file-asset:${ref.fileId}`
    case 'file-group':
      return `file-group:${ref.groupId}`
    case 'chat-rule':
      return `chat-rule:${ref.ruleId}`
  }
}

function titleFromMessage(contentMarkdown: string) {
  return initialSessionTitle('chat', contentMarkdown)
}

function reportLabel(title: string, runId: string) {
  const normalized = title
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48)
  if (normalized) return normalized
  return runId.toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 48) || 'report'
}

function knowledgeSessionTitleFromQuestion(question: string): string {
  const title = question.trim().replace(/\s+/g, ' ').slice(0, 48)
  return title || 'Knowledge session'
}

function isPlaceholderAgentSessionTitle(title: string): boolean {
  return ['Neue Sitzung', 'New session', ''].includes(title.trim())
}

function isPlaceholderKnowledgeSessionTitle(title: string): boolean {
  return [
    'Knowledge session',
    'New knowledge session',
    'New session',
    'Neue Sitzung',
    DEFAULT_KNOWLEDGE_SESSION_TITLE,
  ].includes(title)
}

function slugifyVectorHandle(value: string): string {
  const normalized = value
    .normalize('NFKD')
    .toLowerCase()
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 48)
  return normalized || 'index'
}

function uniqueVectorHandle(base: string, state: ProjectState, exceptId?: string): string {
  const taken = new Set(
    Object.values(state.vectorIndexes)
      .filter((index) => index.id !== exceptId)
      .map((index) => index.handle),
  )
  if (!taken.has(base)) return base
  let suffix = 2
  while (taken.has(`${base}-${suffix}`)) suffix += 1
  return `${base}-${suffix}`
}

function dimsForEmbedModel(model: EmbedModelId): number {
  return EMBED_MODELS.find((entry) => entry.id === model)?.dims ?? 3072
}

function knowledgeItemByRunId(
  state: ProjectState,
  runId: string,
): KnowledgeThreadItemRecord | null {
  for (const itemId of state.knowledgeItemOrder) {
    const item = state.knowledgeItems[itemId]
    if (item && item.runId === runId) return item
  }
  return null
}

function updateKnowledgeItem(
  state: ProjectState,
  itemId: string,
  update: (item: KnowledgeThreadItemRecord) => KnowledgeThreadItemRecord,
): ProjectState {
  const current = state.knowledgeItems[itemId]
  if (!current) return state
  const next = update(current)
  if (next === current) return state
  return {
    ...state,
    knowledgeItems: {
      ...state.knowledgeItems,
      [itemId]: next,
    },
  }
}

/**
 * Attach a run's result to its knowledge ask. Every refresh pulse reloads the
 * results of finished runs, so attaching is idempotent: a completed ask keeps
 * its answer time and its session stays untouched. The first completion takes
 * the run's finish (`finishedAt`, the server's time; the live moment when it
 * is unknown). An answer time that differs from the run's finish is corrected
 * once — earlier clients stamped every reload as the answer time — and so is
 * the session time that the same stamp set; a session changed later keeps its
 * time.
 */
function attachKnowledgeResult(
  state: ProjectState,
  item: KnowledgeThreadItemRecord,
  result: ResearchRunResult,
  finishedAt: string | undefined,
): ProjectState {
  if (item.status === 'completed' && item.answer !== undefined) {
    if (finishedAt === undefined || item.completedAt === finishedAt) return state
    const stamped = item.completedAt
    const corrected = updateKnowledgeItem({ ...state, dirty: true }, item.id, (current) => ({
      ...current,
      completedAt: finishedAt,
    }))
    const session = corrected.knowledgeSessions[item.sessionId]
    return session && stamped !== undefined && session.updatedAt === stamped
      ? touchKnowledgeSessions(
        corrected,
        [item.sessionId],
        latestKnowledgeActivity(corrected, item.sessionId),
      )
      : corrected
  }
  const completedAt = finishedAt ?? new Date().toISOString()
  const session = state.knowledgeSessions[item.sessionId]
  return touchKnowledgeSessions(
    updateKnowledgeItem({ ...state, dirty: true }, item.id, (current) => ({
      ...current,
      answer: knowledgeAnswerWithRunProgress(
        knowledgeAnswerFromRunResult(result),
        current.progress,
      ),
      completedAt,
      progress: {
        ...current.progress,
        steps: current.progress.steps.map((step) => (
          step.status === 'done' ? step : { ...step, status: 'done' as const }
        )),
      },
      status: 'completed',
    })),
    [item.sessionId],
    session && Date.parse(session.updatedAt) > Date.parse(completedAt)
      ? session.updatedAt
      : completedAt,
  )
}

/** The latest real activity of a knowledge session: the newest answer time,
 * or question time for an ask without an answer. */
function latestKnowledgeActivity(state: ProjectState, sessionId: string): string {
  let latest = ''
  for (const itemId of state.knowledgeItemOrder) {
    const item = state.knowledgeItems[itemId]
    if (!item || item.sessionId !== sessionId) continue
    const time = item.completedAt ?? item.createdAt
    if (!latest || Date.parse(time) > Date.parse(latest)) latest = time
  }
  return latest || state.knowledgeSessions[sessionId].updatedAt
}

function touchKnowledgeSessions(
  state: ProjectState,
  sessionIds: Iterable<string>,
  updatedAt: string,
): ProjectState {
  const existingIds = [...new Set(sessionIds)].filter((sessionId) => Boolean(state.knowledgeSessions[sessionId]))
  if (existingIds.length === 0) return state

  const knowledgeSessions = { ...state.knowledgeSessions }
  for (const sessionId of existingIds) {
    const session = knowledgeSessions[sessionId]
    knowledgeSessions[sessionId] = { ...session, updatedAt }
  }
  return { ...state, knowledgeSessions }
}

/** Route one run event into the matching knowledge thread item (live
 * runs and demo asks share this path); terminal events also close the
 * item so the card never poses as still running. */
/** The ONE application path for a run event, shared by the live single
 * dispatch (arrivedLive: true) and the history batch (arrivedLive: false) —
 * the two may never drift apart in anything but that flag. */
function appendApiRunEventToState(
  state: ProjectState,
  event: ResearchRunEvent,
  options: { arrivedLive: boolean; receivedAt?: number },
): ProjectState {
  // Knowledge thread items consume the same event stream as the run
  // records: demo asks have no run record, live asks have both.
  const withKnowledge = applyEventToKnowledgeItem(state, event)
  // Agent runs own their events entirely (separate record model);
  // server-derived state never marks the project dirty.
  const agentRun = withKnowledge.agentRuns[event.run_id]
  if (agentRun) {
    const applied = applyAgentRunEvent(agentRun, event, options)
    if (applied === agentRun) return withKnowledge
    // The session artifact index invalidates alongside the run's
    // artifact surface: an update may have RE-ANCHORED a session
    // artifact, and only the session listing sees that move.
    const sessionId = applied.sessionId
    const flipSession =
      sessionId !== undefined
      && applied.artifactsStale
      && !agentRun.artifactsStale
    const index = flipSession
      ? withKnowledge.agentSessionArtifacts[sessionId]
      : undefined
    return {
      ...withKnowledge,
      agentRuns: {
        ...withKnowledge.agentRuns,
        [applied.runId]: applied,
      },
      ...(flipSession
        ? {
          agentSessionArtifacts: {
            ...withKnowledge.agentSessionArtifacts,
            [sessionId]: {
              byId: index?.byId ?? {},
              order: index?.order ?? [],
              stale: true,
            },
          },
        }
        : {}),
    }
  }
  const current = withKnowledge.researchRuns[event.run_id]
  if (!current) return withKnowledge
  const run = applyRunEvent(current, event, options)

  return {
    ...withKnowledge,
    dirty: true,
    researchRuns: {
      ...withKnowledge.researchRuns,
      [run.runId]: run,
    },
  }
}

function applyEventToKnowledgeItem(
  state: ProjectState,
  event: ResearchRunEvent,
): ProjectState {
  const item = knowledgeItemByRunId(state, event.run_id)
  if (!item || item.status !== 'running') return state
  const updatedAt = new Date().toISOString()
  const terminalExit = event.type === 'inqtrix.run.failed' || event.type === 'inqtrix.run.cancelled'
  const terminalStatus = event.type === 'inqtrix.run.cancelled' ? 'cancelled' : 'failed'
  const updated = updateKnowledgeItem(terminalExit ? { ...state, dirty: true } : state, item.id, (current) => {
    const progress = applyKnowledgeRunEvent(current.progress, event)
    if (terminalExit) {
      const error = typeof (event.data.error as { message?: unknown } | undefined)?.message === 'string'
        ? String((event.data.error as { message?: unknown }).message)
        : undefined
      return {
        ...current,
        error: error ?? current.error,
        progress,
        status: terminalStatus,
      }
    }
    if (progress === current.progress) return current
    return { ...current, progress }
  })
  return terminalExit
    ? touchKnowledgeSessions(updated, [item.sessionId], updatedAt)
    : updated
}

function writeVectorIndex(state: ProjectState, index: VectorIndexRecord): ProjectState {
  return {
    ...state,
    dirty: true,
    vectorIndexes: { ...state.vectorIndexes, [index.id]: index },
  }
}

/** Prepend one finished run to the index history, newest first, capped. */
function appendVectorIndexHistory(
  index: VectorIndexRecord,
  entry: VectorIndexRunHistoryEntry,
): VectorIndexRunHistoryEntry[] {
  return [entry, ...(index.history ?? [])].slice(0, VECTOR_INDEX_HISTORY_LIMIT)
}

/** Elapsed run time from the live entry's start to *now* (ms, never negative). */
function runDurationMs(live: IndexingJobLive | undefined, now: string): number {
  if (!live) return 0
  return Math.max(0, new Date(now).getTime() - new Date(live.startedAt).getTime())
}

function reconcileVectorIndexMembers(
  members: readonly VectorIndexMemberRecord[],
  embeddedFileIds: readonly string[],
  skippedFileIds: readonly string[],
  serverDocumentIds: Readonly<Record<string, string>>,
): VectorIndexMemberRecord[] {
  const embedded = new Set(embeddedFileIds)
  const skipped = new Set(skippedFileIds)
  return members.map((member): VectorIndexMemberRecord => ({
    ...member,
    serverDocumentId:
      serverDocumentIds[member.fileId]
      ?? member.serverDocumentId,
    state: embedded.has(member.fileId)
      ? 'embedded'
      : skipped.has(member.fileId)
        ? 'skipped'
        : member.state,
  }))
}

/** Drop the ephemeral live-progress entry for an index (run finished). */
function clearIndexingJob(state: ProjectState, indexId: string): ProjectState {
  if (!(indexId in state.indexingJobs)) return state
  const indexingJobs = { ...state.indexingJobs }
  delete indexingJobs[indexId]
  return { ...state, indexingJobs }
}

/** Removes the given file ids from every vector index's membership and
 * recomputes status. Used by file- and section-deletion cascades; an
 * in-flight `indexing` status is left untouched so a running simulation
 * is not disturbed. */
function dropFilesFromVectorIndexes(
  vectorIndexes: ProjectState['vectorIndexes'],
  removedFileIds: ReadonlySet<string>,
  updatedAt: string,
): { changed: boolean; vectorIndexes: ProjectState['vectorIndexes'] } {
  let changed = false
  const next: ProjectState['vectorIndexes'] = {}
  for (const [id, index] of Object.entries(vectorIndexes)) {
    const members = index.members.filter((member) => !removedFileIds.has(member.fileId))
    if (members.length === index.members.length) {
      next[id] = index
      continue
    }
    changed = true
    next[id] = {
      ...index,
      members,
      status: members.length === 0 && index.status !== 'indexing' ? 'ready' : index.status,
      updatedAt,
    }
  }
  return changed ? { changed, vectorIndexes: next } : { changed, vectorIndexes }
}

function nextOpenEditorCommentId(state: ProjectState, currentCommentId: string) {
  const current = state.editorComments[currentCommentId]
  if (!current) return null
  const ordered = Object.values(state.editorComments)
    .filter((comment) => comment.documentId === current.documentId && comment.status !== 'resolved')
    .sort((a, b) => a.anchor.from - b.anchor.from || a.createdAt.localeCompare(b.createdAt))
  const currentIndex = ordered.findIndex((comment) => comment.id === currentCommentId)
  if (currentIndex < 0) return ordered[0]?.id ?? null
  return ordered[currentIndex + 1]?.id ?? ordered[currentIndex - 1]?.id ?? null
}

function markEditorCommentOutbox(
  state: ProjectState,
  comment: EditorCommentThreadRecord,
  operation: EditorCommentOutboxEntry['operation'],
): ProjectState {
  return {
    ...state,
    editorCommentOutbox: {
      ...(state.editorCommentOutbox ?? {}),
      [comment.id]: {
        documentId: comment.documentId,
        operation,
        ...(operation === 'upsert' ? { updatedAt: comment.updatedAt } : {}),
      },
    },
  }
}

function retireActiveEditorSuggestionsForComments(
  suggestions: ProjectState['editorSuggestions'],
  commentIds: Set<string>,
  updatedAt: string,
): ProjectState['editorSuggestions'] {
  if (commentIds.size === 0) return suggestions
  let changed = false
  const next = Object.fromEntries(
    Object.entries(suggestions).map(([suggestionId, suggestion]) => {
      const commentId = suggestion.origin.commentId
      const shouldRetire = commentId
        && commentIds.has(commentId)
        && (suggestion.status === 'pending' || suggestion.status === 'stale')
      if (!shouldRetire) return [suggestionId, suggestion]
      changed = true
      return [suggestionId, { ...suggestion, status: 'rejected' as const, updatedAt }]
    }),
  )
  return changed ? next : suggestions
}

function privateSuggestionOriginForComment(
  comment: EditorCommentThreadRecord,
): EditorSuggestionOrigin {
  if (comment.kind === 'inline_edit') {
    return { commentId: comment.id, kind: 'inline_edit' }
  }
  if (comment.kind === 'assistant_edit') {
    // Eigene Herkunft, nicht 'global_run': eine Sammel-Notiz des Nutzers
    // faellt ebenfalls in den Zweig darunter und traegt dann dieselbe
    // Kennung. Wer Anweisungslaeufe daran erkennen will, traefe beide.
    return { commentId: comment.id, kind: 'assistant_edit' }
  }
  return { commentId: comment.id, kind: 'global_run' }
}

/** Materialize server-confirmed private drafts into the existing review model.
 * The resulting records are a view of the nested comment authority, never a
 * second persistence source. */
function reconcilePrivateSuggestionDraftRecords(
  state: ProjectState,
  comments: readonly EditorCommentThreadRecord[],
): ProjectState {
  if (comments.length === 0) return state
  const editorSuggestionGroups = { ...state.editorSuggestionGroups }
  const editorSuggestions = { ...state.editorSuggestions }
  let editorComments = state.editorComments
  const groupCleanupCandidates = new Set<string>()
  let changed = false

  for (const comment of comments) {
    const draft = comment.suggestionDraft
    for (const suggestion of Object.values(editorSuggestions)) {
      if (suggestion.origin.commentId !== comment.id) continue
      const replacedByServerDraft = Boolean(
        draft
        && suggestion.id !== draft.suggestionId
        && (suggestion.status === 'pending' || suggestion.status === 'stale'),
      )
      if (!suggestion.privateDraft && !replacedByServerDraft) continue
      if (draft && suggestion.id === draft.suggestionId) continue
      delete editorSuggestions[suggestion.id]
      groupCleanupCandidates.add(suggestion.groupId)
      changed = true
    }
    if (!draft) continue

    const existingTerminal = editorSuggestions[draft.suggestionId]
    if (
      existingTerminal
      && (existingTerminal.status === 'accepted' || existingTerminal.status === 'rejected')
      && existingTerminal.updatedAt >= draft.updatedAt
    ) {
      const currentComment = editorComments[comment.id]
      if (currentComment?.suggestionDraft?.patchId === draft.patchId) {
        const { suggestionDraft, ...commentWithoutStaleDraft } = currentComment
        void suggestionDraft
        editorComments = {
          ...editorComments,
          [comment.id]: commentWithoutStaleDraft,
        }
        changed = true
      }
      continue
    }

    const origin = privateSuggestionOriginForComment(comment)
    const group: EditorSuggestionGroupRecord = {
      createdAt: draft.createdAt,
      documentId: comment.documentId,
      id: draft.groupId,
      origin,
      ...(draft.warnings?.length ? { warnings: [...draft.warnings] } : {}),
    }
    const suggestion: EditorSuggestionRecord = {
      anchor: comment.anchor,
      blockId: comment.anchor.blockId ?? '',
      createdAt: draft.createdAt,
      documentId: comment.documentId,
      groupId: draft.groupId,
      id: draft.suggestionId,
      originalMarkdown: comment.anchor.selectedMarkdown,
      originalText: comment.anchor.selectedText,
      origin,
      privateDraft: {
        patchId: draft.patchId,
        publicationCommandId: draft.publicationCommandId,
        revision: draft.revision,
      },
      proposedText: draft.proposedText,
      revision: draft.revision,
      status: 'pending',
      updatedAt: draft.updatedAt,
      // Without these two lines the suggestion falls back to 'replace' on
      // rebuild (resolveSuggestionTarget: editPosition ?? 'replace'). An
      // "insert after the anchor" would then REPLACE the anchor and the user
      // would silently lose text. Stored drafts without the fields keep the
      // 'replace' behaviour.
      ...(draft.editPosition ? { editPosition: draft.editPosition } : {}),
      ...(draft.anchorText ? { anchorText: draft.anchorText } : {}),
      ...(draft.changeSummary?.length
        ? { changeSummary: [...draft.changeSummary] }
        : {}),
      ...(draft.revisionHistory?.length
        ? { revisionHistory: [...draft.revisionHistory] }
        : {}),
      ...(draft.warnings?.length ? { warnings: [...draft.warnings] } : {}),
    }
    if (
      editorSuggestionGroups[group.id] !== group
      || editorSuggestions[suggestion.id] !== suggestion
    ) {
      editorSuggestionGroups[group.id] = group
      editorSuggestions[suggestion.id] = suggestion
      changed = true
    }
  }

  for (const groupId of groupCleanupCandidates) {
    if (Object.values(editorSuggestions).some((suggestion) => suggestion.groupId === groupId)) {
      continue
    }
    delete editorSuggestionGroups[groupId]
  }
  return changed
    ? { ...state, editorComments, editorSuggestionGroups, editorSuggestions }
    : state
}

/** Whether the next instruction run on *documentId* replaces *suggestion*.
 * The editor assistant names these before it is asked again. */
export function isReplacedByNextInstructionRun(
  suggestion: EditorSuggestionRecord,
  documentId: string,
): boolean {
  // Genau die Vorschlaege des VORIGEN Anweisungslaufs, nicht mehr.
  // 'global_run' ohne Traeger ist der Markdown-Modus; 'assistant_edit'
  // ist der Kollaborationsmodus. Ein 'global_run' MIT commentId stammt
  // dagegen aus einer Sammel-Notiz des Nutzers und geht diesen Lauf
  // nichts an -- ihn mitzuverwerfen hiesse, offene Arbeit des Nutzers
  // stillschweigend wegzuraeumen.
  const fromInstructionRun = suggestion.origin.kind === 'assistant_edit'
    || (suggestion.origin.kind === 'global_run' && !suggestion.origin.commentId)
  return suggestion.documentId === documentId
    && fromInstructionRun
    && (suggestion.status === 'pending' || suggestion.status === 'stale')
}

function retireActiveDocumentInstructionSuggestions(
  suggestions: ProjectState['editorSuggestions'],
  documentId: string,
  updatedAt: string,
): ProjectState['editorSuggestions'] {
  let changed = false
  const next = Object.fromEntries(
    Object.entries(suggestions).map(([suggestionId, suggestion]) => {
      if (!isReplacedByNextInstructionRun(suggestion, documentId)) return [suggestionId, suggestion]
      changed = true
      return [suggestionId, { ...suggestion, status: 'rejected' as const, updatedAt }]
    }),
  )
  return changed ? next : suggestions
}

export function visibleResearchJobs(
  jobs: ResearchJob[],
  activeFilter: JobFilter,
) {
  return activeFilter === 'all'
    ? jobs
    // An unavailable run's card visibly retracts its last status (calm
    // lock), so it must not appear under -- or count toward -- that
    // status bucket; it stays reachable under "all" until hydration
    // prunes it.
    : jobs.filter((job) => job.status === activeFilter && !job.unavailable)
}

export function selectedResearchJob(
  jobs: ResearchJob[],
  selectedJobId: string | null,
) {
  return jobs.find((job) => job.id === selectedJobId) ?? null
}

export function resolveVisibleSelectionFromJobs(
  jobs: ResearchJob[],
  activeFilter: JobFilter,
  selectedJobId: string | null,
) {
  const visibleJobs = visibleResearchJobs(jobs, activeFilter)
  const selectedJobIsVisible = visibleJobs.some((job) => job.id === selectedJobId)

  return selectedJobIsVisible ? selectedJobId : visibleJobs[0]?.id ?? null
}

function resolveVisibleSelection(
  researchRunOrder: string[],
  researchRuns: Record<string, ResearchRunRecord>,
  activeFilter: JobFilter,
  selectedJobId: string | null,
) {
  const visibleRunIds = activeFilter === 'all'
    ? researchRunOrder
    : researchRunOrder.filter((runId) => researchRuns[runId]?.status === activeFilter)
  const selectedJobIsVisible = visibleRunIds.some((runId) => runId === selectedJobId)

  return selectedJobIsVisible ? selectedJobId : visibleRunIds[0] ?? null
}

/**
 * Atomically reconcile the complete run listing. Server-backed rows absent
 * from the successful listing are removed (including revoked shares), while
 * imported/mock rows remain local. Incoming summaries still merge onto live
 * event/result state through the normal reducer path.
 */
export function replaceApiRunSummaries(
  state: ProjectState,
  summaries: readonly ResearchRunSummary[],
): ProjectState {
  let merged = state
  for (const summary of summaries) {
    merged = researchDeskReducer(merged, {
      hydration: true,
      summary,
      type: 'upsertApiRunSummary',
    })
  }

  const researchServerOrder: string[] = []
  const visibleResearchIds = new Set<string>()
  const visibleAgentIds = new Set<string>()
  for (const summary of summaries) {
    if (summary.mode === 'knowledge' || summary.kind === 'agent_child') continue
    if (isAgentRunSummary(summary)) {
      visibleAgentIds.add(summary.run_id)
    } else if (!visibleResearchIds.has(summary.run_id)) {
      visibleResearchIds.add(summary.run_id)
      researchServerOrder.push(summary.run_id)
    }
  }

  const researchRuns = Object.fromEntries(
    Object.entries(merged.researchRuns).filter(
      ([runId, run]) => run.source !== 'api' || visibleResearchIds.has(runId),
    ),
  )
  const localResearchOrder = merged.researchRunOrder.filter((runId) => {
    const run = researchRuns[runId]
    return run !== undefined && run.source !== 'api'
  })
  const researchRunOrder = [...researchServerOrder, ...localResearchOrder]
  const selectedJobId = resolveVisibleSelection(
    researchRunOrder,
    researchRuns,
    merged.ui.activeFilter,
    merged.ui.selectedJobId,
  )

  // The paginated root listing intentionally does not hydrate child summaries.
  // Keep already-loaded descendants while their root remains visible; if the
  // root disappears (delete/share revoke), prune every descendant with it.
  const retainedAgentIds = retainedAgentTreeIds(merged.agentRuns, visibleAgentIds)
  const agentRuns = Object.fromEntries(
    Object.entries(merged.agentRuns).filter(([runId]) => retainedAgentIds.has(runId)),
  )
  const agentSessions = Object.fromEntries(
    merged.agentSessionOrder.flatMap((sessionId) => {
      const session = merged.agentSessions[sessionId]
      if (!session) return []
      const runIds = session.runIds.filter((runId) => agentRuns[runId] !== undefined)
      // Derived shared-run views and run-id-keyed transient shells disappear
      // with their last run; real empty server sessions remain available.
      if (runIds.length === 0 && (
        session.persistable === false
        || (
          visibleAgentIds.has(sessionId) === false
          && merged.agentRuns[sessionId] !== undefined
        )
      )) {
        return []
      }
      return [[sessionId, { ...session, runIds }]]
    }),
  ) as Record<string, AgentSessionRecord>
  const agentSessionOrder = merged.agentSessionOrder.filter(
    (sessionId) => agentSessions[sessionId] !== undefined,
  )
  const selectedAgentSessionId = merged.selectedAgentSessionId
    && agentSessions[merged.selectedAgentSessionId]
    ? merged.selectedAgentSessionId
    : agentSessionOrder[0] ?? null

  return {
    ...merged,
    agentCanvas: agentCanvasForSelection(merged, selectedAgentSessionId),
    agentRuns,
    agentSessionOrder,
    agentSessions,
    researchRunOrder,
    researchRuns,
    selectedAgentSessionId,
    ui: {
      ...merged.ui,
      expandedJobId: merged.ui.expandedJobId
        && researchRuns[merged.ui.expandedJobId]
        ? merged.ui.expandedJobId
        : selectedJobId,
      pendingChatReportRunId: merged.ui.pendingChatReportRunId
        && researchRuns[merged.ui.pendingChatReportRunId]
        ? merged.ui.pendingChatReportRunId
        : null,
      pinnedExplorer: {
        ...merged.ui.pinnedExplorer,
        agentSessionIds: merged.ui.pinnedExplorer.agentSessionIds.filter(
          (sessionId) => agentSessions[sessionId] !== undefined,
        ),
      },
      selectedAgentSessionId,
      selectedJobId,
    },
  }
}

/**
 * Replace one imported project-local run id with the canonical id allocated by
 * ``POST /v1/runs/import``. The report body and event history stay intact while
 * every in-project reference follows the new id atomically, so a later delete,
 * share, selection, or attachment always addresses the durable resource.
 */
export function adoptImportedApiRun(
  state: ProjectState,
  sourceRunId: string,
  summary: ResearchRunSummary,
): ProjectState {
  const imported = state.researchRuns[sourceRunId]
  if (!imported || imported.source !== 'imported') return state
  const canonicalRunId = summary.run_id
  const run = mergeRunSummary(imported, summary, state.ui.selectedStack)
  const researchRuns = { ...state.researchRuns }
  delete researchRuns[sourceRunId]
  researchRuns[canonicalRunId] = run
  const researchRunOrder = state.researchRunOrder
    .map((runId) => runId === sourceRunId ? canonicalRunId : runId)
    .filter((runId, index, ids) => ids.indexOf(runId) === index)
  const replaceRunId = (runId: string | null) =>
    runId === sourceRunId ? canonicalRunId : runId
  const pendingChatAttachmentRefs = state.ui.pendingChatAttachmentRefs.map((ref) =>
    ref.kind === 'research-report' && ref.runId === sourceRunId
      ? { ...ref, runId: canonicalRunId }
      : ref)
  const editorDocuments = Object.fromEntries(
    Object.entries(state.editorDocuments).map(([documentId, document]) => [
      documentId,
      document.sourceRunId === sourceRunId
        ? { ...document, sourceRunId: canonicalRunId }
        : document,
    ]),
  )
  const chatThreads = Object.fromEntries(
    Object.entries(state.chatThreads).map(([threadId, thread]) => [
      threadId,
      {
        ...thread,
        messages: thread.messages.map((message) => ({
          ...message,
          attachments: message.attachments?.map((attachment) =>
            attachment.kind === 'research-report' && attachment.runId === sourceRunId
              ? { ...attachment, runId: canonicalRunId }
              : attachment),
        })),
      },
    ]),
  )
  return {
    ...state,
    chatThreads,
    editorDocuments,
    researchRunOrder,
    researchRuns,
    ui: {
      ...state.ui,
      expandedJobId: replaceRunId(state.ui.expandedJobId),
      pendingChatAttachmentRefs,
      pendingChatReportRunId: replaceRunId(state.ui.pendingChatReportRunId),
      selectedJobId: replaceRunId(state.ui.selectedJobId),
    },
  }
}

function retainedAgentTreeIds(
  agentRuns: Readonly<Record<string, AgentRunRecord>>,
  visibleRootIds: ReadonlySet<string>,
): Set<string> {
  const retained = new Set(visibleRootIds)
  let changed = true
  while (changed) {
    changed = false
    for (const [runId, run] of Object.entries(agentRuns)) {
      if (retained.has(runId) || run.kind !== 'agent_child') continue
      const rootVisible = run.rootRunId !== undefined
        && visibleRootIds.has(run.rootRunId)
      const parentRetained = run.parentRunId !== undefined
        && retained.has(run.parentRunId)
      if (rootVisible || parentRetained) {
        retained.add(runId)
        changed = true
      }
    }
  }
  return retained
}

function agentRunIdsForDeletedSession(
  agentRuns: Readonly<Record<string, AgentRunRecord>>,
  sessionId: string,
  directRunIds: readonly string[],
): Set<string> {
  const removed = new Set(directRunIds)
  for (const [runId, run] of Object.entries(agentRuns)) {
    if (run.sessionId === sessionId) removed.add(runId)
  }
  let changed = true
  while (changed) {
    changed = false
    for (const [runId, run] of Object.entries(agentRuns)) {
      if (removed.has(runId)) continue
      if (
        (run.rootRunId !== undefined && removed.has(run.rootRunId))
        || (run.parentRunId !== undefined && removed.has(run.parentRunId))
      ) {
        removed.add(runId)
        changed = true
      }
    }
  }
  return removed
}

function matchesCompletedAgentSessionDeletion(
  state: ProjectState,
  summary: ResearchRunSummary,
): boolean {
  const receipts = state.agentSessionDeletionReceipts
  if (!receipts) return false
  if (summary.session_id && receipts[summary.session_id]) return true
  const candidates = [summary.run_id, summary.root_run_id, summary.parent_run_id]
    .filter((value): value is string => Boolean(value))
  return Object.values(receipts).some((receipt) => {
    const removed = new Set(receipt.runIds)
    return candidates.some((runId) => removed.has(runId))
  })
}

/** Merge an agent-run summary and keep its session's turn list in sync.
 * Runs without a session id get a synthetic one keyed by the run id so
 * they stay visible. Never sets `dirty` (server-derived rows). */
/**
 * The canvas slice to carry when the selected agent session becomes
 * *nextSessionId*. The canvas is a workspace-global slice whose tabs
 * address ONE session's runs (plan/run/document by runId); the timeline
 * is strictly per-session. So EVERY transition that changes the selected
 * session to a different value must clear the canvas — otherwise a
 * pinned tab from the previous session keeps rendering its content
 * beside the new session's timeline (follow is inert while pinned).
 * Binding the reset to this state transition (not to one action) covers
 * selectAgentSession, createAgentSession, run-summary select, and the
 * delete-reassignment uniformly.
 */
function agentCanvasForSelection(
  state: ProjectState,
  nextSessionId: string | null,
): CanvasState {
  return nextSessionId === state.selectedAgentSessionId
    ? state.agentCanvas
    : EMPTY_CANVAS_STATE
}

/**
 * Every selection write mirrors into `ui.selectedAgentSessionId` — the
 * persisted intent the workspace restores after a reload. One helper on
 * ALL write paths (create, select, delete-reassign, run-summary select),
 * bound to the state transition rather than to one action, so no path
 * can leak a stale persisted selection.
 */
function withSelectedAgentSession(
  state: ProjectState,
  sessionId: string | null,
  sessions: ProjectState['agentSessions'] = state.agentSessions,
): Pick<ProjectState, 'selectedAgentSessionId' | 'ui'> {
  if (state.ui.selectedAgentSessionId === sessionId) {
    return { selectedAgentSessionId: sessionId, ui: state.ui }
  }
  // Project the target session's own pick into the working value — ui only,
  // never a write back into the session. A session without a stored pick
  // clears the value so the account preference may seed it.
  const selection = sessionId ? sessions[sessionId]?.modelSelection ?? null : null
  return {
    selectedAgentSessionId: sessionId,
    ui: {
      ...state.ui,
      selectedAgentSessionId: sessionId,
      selectedAgentEffort: selection?.effort ?? null,
      selectedAgentModel: selection?.model ?? null,
      selectedAgentModelTier: selection?.tier ?? null,
    },
  }
}

/** The chat twin of withAgentSessionModelSelection: apply a picker change to
 * the working state AND to the thread it belongs to. Guards make sure only a
 * real chat-thread context persists: the editor dispatches the very same chat
 * set-actions, so only `activeView === 'chat'` may write a pick through, and
 * the incognito thread never appears in `chatThreads`, so it can never be
 * written. */
function withChatThreadModelSelection(
  state: ProjectState,
  ui: ProjectState['ui'],
): ProjectState {
  const threadId = ui.selectedChatThreadId
  const thread = threadId ? state.chatThreads[threadId] : null
  if (!thread || !threadId || ui.activeView !== 'chat') {
    return { ...state, dirty: true, ui }
  }
  const cleared = ui.selectedChatEffort === null
    && ui.selectedChatModel === null
    && ui.selectedChatModelTier === null
  const next = cleared
    ? undefined
    : {
      effort: ui.selectedChatEffort,
      model: ui.selectedChatModel,
      tier: ui.selectedChatModelTier,
    }
  if (agentModelSelectionKey(thread.modelSelection) === agentModelSelectionKey(next)) {
    return { ...state, dirty: true, ui }
  }
  return {
    ...state,
    dirty: true,
    chatThreads: {
      ...state.chatThreads,
      [threadId]: {
        ...thread,
        modelSelection: next,
        updatedAt: new Date().toISOString(),
      },
    },
    ui,
  }
}

/** Apply a picker change to the working state AND to the session it belongs
 * to, so a reload returns to the model this session ran with. The session
 * copy is the durable one; `ui` stays the working value the composer reads.
 * Without a writable active session only `ui` changes — the very first pick
 * can happen before any session exists. Clearing everything (the picker's
 * "Auto" row) REMOVES the stored pick, which is what lets the preference
 * seed again: Auto means "follow my default", not "pin null". */
function withAgentSessionModelSelection(
  state: ProjectState,
  ui: ProjectState['ui'],
): ProjectState {
  const sessionId = ui.selectedAgentSessionId
  const session = sessionId ? state.agentSessions[sessionId] : null
  if (!session || !sessionId || session.deletion || session.persistable === false) {
    return { ...state, dirty: true, ui }
  }
  const cleared = ui.selectedAgentEffort === null
    && ui.selectedAgentModel === null
    && ui.selectedAgentModelTier === null
  const next = cleared
    ? undefined
    : {
      effort: ui.selectedAgentEffort,
      model: ui.selectedAgentModel,
      tier: ui.selectedAgentModelTier,
    }
  // No-op guard: an unchanged pick must not bump updatedAt, or the autosave
  // would push on every render.
  if (agentModelSelectionKey(session.modelSelection) === agentModelSelectionKey(next)) {
    return { ...state, dirty: true, ui }
  }
  return {
    ...state,
    dirty: true,
    agentSessions: {
      ...state.agentSessions,
      [sessionId]: {
        ...session,
        modelSelection: next,
        updatedAt: new Date().toISOString(),
      },
    },
    ui,
  }
}

function withAgentRunSummary(
  state: ProjectState,
  summary: ResearchRunSummary,
  select: boolean,
  { retitle = true }: { retitle?: boolean } = {},
): ProjectState {
  if (matchesCompletedAgentSessionDeletion(state, summary)) return state
  const current = state.agentRuns[summary.run_id]
  const run = mergeAgentRunSummary(current, summary)
  const sessionId = run.sessionId || run.runId
  // First sighting of a session seeds its artifact index STALE, so the
  // control loop fetches the anchor-independent listing once;
  // afterwards the event wrapper re-flags it alongside artifactsStale.
  const agentSessionArtifacts = run.sessionId
    && state.agentSessionArtifacts[run.sessionId] === undefined
    ? {
      ...state.agentSessionArtifacts,
      [run.sessionId]: { byId: {}, order: [], stale: true },
    }
    : state.agentSessionArtifacts
  let agentSessions = state.agentSessions
  let agentSessionOrder = state.agentSessionOrder
  const existing = agentSessions[sessionId]
  if (!existing) {
    const session: AgentSessionRecord = {
      id: sessionId,
      title: run.question.trim().slice(0, 80) || sessionId,
      groupId: null,
      createdAt: run.createdAt,
      // FABRICATION, not user state: summaries hydrate newest-first
      // and may beat the session listing — the epoch stamp makes the
      // real server row win the merge and keeps the autosave from ever
      // pushing this derived title over the stored one. Without it a
      // reload retitles the session to the LATEST question and syncs
      // that derived title back durably.
      updatedAt: DERIVED_AGENT_SESSION_UPDATED_AT,
      runIds: [run.runId],
      sourcePolicy: { ...DEFAULT_AGENT_SOURCE_POLICY },
      // Not a server row, so never ask the server for it. Two shells
      // land here: a shared-run view, and — keyed by the RUN id, see
      // `sessionId` above — a run that carries no session at all. Fetched
      // like a real session, the second one would ask
      // `GET /v1/agent-sessions/run_…`, get a 404, and show a red error in
      // the rail over a run that is perfectly fine.
      // `upsertServerAgentSessions` still upgrades the shell if a
      // real session with that id ever arrives.
      ...(run.access?.mode === 'shared' || !run.sessionId
        ? { persistable: false }
        : {}),
    }
    agentSessions = { ...agentSessions, [sessionId]: session }
    agentSessionOrder = [sessionId, ...agentSessionOrder]
  } else if (!existing.runIds.includes(run.runId)) {
    // Run membership never stamps updatedAt (server-derived). The ONE
    // exception is the placeholder retitle on a LIVE submit: it must
    // stamp — with the run's createdAt, not "now" — so the title syncs
    // exactly once and later reloads see a REAL title instead of
    // re-deriving one from whichever run hydrates first (newest-first,
    // which drifts the title to the latest question). A genuine
    // user rename carries a later updatedAt and still wins the merge.
    const retitled =
      retitle
      && existing.titleSource !== 'manual'
      && existing.runIds.length === 0
      && isPlaceholderAgentSessionTitle(existing.title)
      && Boolean(run.question.trim())
    // A still-untouched fabrication converges on the OLDEST run seen:
    // newest-first hydration would otherwise display the latest
    // question until the session listing lands. A real row or any
    // user mutation carries a non-epoch updatedAt and is never touched.
    const rederived =
      !retitled
      && existing.updatedAt === DERIVED_AGENT_SESSION_UPDATED_AT
      && run.createdAt < existing.createdAt
      && Boolean(run.question.trim())
    agentSessions = {
      ...agentSessions,
      [sessionId]: {
        ...existing,
        runIds: [...existing.runIds, run.runId],
        ...(retitled
          ? {
            title: run.question.trim().slice(0, 80),
            titlePending: true, titleEditId: createId('title-edit'),
            updatedAt: run.createdAt,
          }
          : {}),
        ...(rederived
          ? {
            createdAt: run.createdAt,
            title: run.question.trim().slice(0, 80),
          }
          : {}),
      },
    }
  }
  // Membership is EXCLUSIVE: a summary that (re)attributes the run sweeps
  // it out of every other session. Without this, a run first seen without
  // session_id lives on in its runId-keyed phantom session after the real
  // session hydrates — and its parked gate then surfaces under the wrong
  // selected session. An emptied phantom shell (id === runId) is dropped
  // outright; if it was selected, selection follows the run.
  let selectedOverride: string | null = null
  for (const [id, session] of Object.entries(agentSessions)) {
    if (id === sessionId || !session.runIds.includes(run.runId)) continue
    const runIds = session.runIds.filter((item) => item !== run.runId)
    if (id === run.runId && runIds.length === 0) {
      agentSessions = Object.fromEntries(
        Object.entries(agentSessions).filter(([key]) => key !== id),
      )
      agentSessionOrder = agentSessionOrder.filter((item) => item !== id)
      if (state.selectedAgentSessionId === id) selectedOverride = sessionId
    } else {
      agentSessions = { ...agentSessions, [id]: { ...session, runIds } }
    }
  }
  return {
    ...state,
    // A select=true summary switches the active session (run creation /
    // demo) — clear the canvas on the transition like every other path.
    agentCanvas: select
      ? agentCanvasForSelection(state, sessionId)
      : state.agentCanvas,
    agentRuns: { ...state.agentRuns, [run.runId]: run },
    agentSessionArtifacts,
    agentSessionOrder,
    agentSessions,
    ...(select || selectedOverride
      ? withSelectedAgentSession(state, selectedOverride ?? sessionId)
      : {}),
  }
}

function updateAgentRun(
  state: ProjectState,
  runId: string,
  update: (run: AgentRunRecord) => AgentRunRecord,
): ProjectState {
  const current = state.agentRuns[runId]
  if (!current) return state
  const next = update(current)
  if (next === current) return state
  return { ...state, agentRuns: { ...state.agentRuns, [runId]: next } }
}
