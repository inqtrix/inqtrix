import { EMPTY_CANVAS_STATE } from '@/features/canvas/types'
import type {
  ChatContextReferenceRecord,
  ChatMessageAttachmentRecord,
  EditorSuggestionOrigin,
  ProjectState,
} from './types'
import { createProjectEntityId } from './entityId'

type IdFactory = (prefix: string) => string
type IdMaps = {
  agentGroups: Map<string, string>
  agentSessions: Map<string, string>
  chatGroups: Map<string, string>
  chatMessages: Map<string, string>
  chatRules: Map<string, string>
  chatThreads: Map<string, string>
  editorComments: Map<string, string>
  editorDocuments: Map<string, string>
  editorFolders: Map<string, string>
  editorSuggestionGroups: Map<string, string>
  editorSuggestions: Map<string, string>
  fileAssets: Map<string, string>
  fileGroups: Map<string, string>
  fileSections: Map<string, string>
  knowledgeGroups: Map<string, string>
  knowledgeItems: Map<string, string>
  knowledgeSessions: Map<string, string>
  vectorIndexes: Map<string, string>
}

function idMap(
  ids: readonly string[],
  prefix: string,
  createId: IdFactory,
): Map<string, string> {
  return new Map(ids.map((id) => [id, createId(prefix)]))
}

function replace(map: ReadonlyMap<string, string>, id: string): string {
  return map.get(id) ?? id
}

function replaceNullable(
  map: ReadonlyMap<string, string>,
  id: string | null,
): string | null {
  return id === null ? null : replace(map, id)
}

function contextReference(
  reference: ChatContextReferenceRecord,
  maps: IdMaps,
): ChatContextReferenceRecord {
  if (reference.kind === 'chat-rule') {
    return { ...reference, ruleId: replace(maps.chatRules, reference.ruleId) }
  }
  if (reference.kind === 'file-asset') {
    return { ...reference, fileId: replace(maps.fileAssets, reference.fileId) }
  }
  if (reference.kind === 'file-group') {
    return { ...reference, groupId: replace(maps.fileGroups, reference.groupId) }
  }
  return reference
}

function messageAttachment(
  attachment: ChatMessageAttachmentRecord,
  maps: IdMaps,
): ChatMessageAttachmentRecord {
  if (attachment.kind === 'chat-rule') {
    return { ...attachment, ruleId: replace(maps.chatRules, attachment.ruleId) }
  }
  if (attachment.kind === 'file-asset') {
    return { ...attachment, fileId: replace(maps.fileAssets, attachment.fileId) }
  }
  if (attachment.kind === 'file-group') {
    return {
      ...attachment,
      fileId: replace(maps.fileAssets, attachment.fileId),
      groupId: replace(maps.fileGroups, attachment.groupId),
    }
  }
  return attachment
}

function suggestionOrigin(
  origin: EditorSuggestionOrigin,
  maps: IdMaps,
): EditorSuggestionOrigin {
  if (!('commentId' in origin) || !origin.commentId) return origin
  return {
    ...origin,
    commentId: replace(maps.editorComments, origin.commentId),
  }
}

/**
 * Clone every client-owned resource id before importing a project to a user.
 *
 * Server-owned references are deliberately not promoted into the clone:
 * collaboration metadata, prompt-template ids, file ids, knowledge collection
 * and document ids, agent runs, shares, and index jobs belong to the source
 * deployment. Local report run ids remain only as import-source references;
 * the run-import endpoint replaces them with newly allocated server ids.
 */
export function detachProjectResourceGraph(
  state: ProjectState,
  targetWorkspaceId: string,
  createId: IdFactory = createProjectEntityId,
): ProjectState {
  const persistableAgentSessionIds = Object.values(state.agentSessions)
    .filter((session) => session.persistable !== false)
    .map((session) => session.id)
  const maps: IdMaps = {
    agentGroups: idMap(Object.keys(state.agentSessionGroups), 'agent-session-group', createId),
    agentSessions: idMap(persistableAgentSessionIds, 'agent-session', createId),
    chatGroups: idMap(Object.keys(state.chatThreadGroups), 'chat-group', createId),
    chatMessages: idMap(
      Object.values(state.chatThreads).flatMap((thread) => (
        thread.messages.map((message) => `${thread.id}\u0000${message.id}`)
      )),
      'chat-message',
      createId,
    ),
    chatRules: idMap(Object.keys(state.chatRules), 'chat-rule', createId),
    chatThreads: idMap(Object.keys(state.chatThreads), 'chat-thread', createId),
    editorComments: idMap(Object.keys(state.editorComments), 'editor-comment', createId),
    editorDocuments: idMap(Object.keys(state.editorDocuments), 'editor-document', createId),
    editorFolders: idMap(Object.keys(state.editorFolders), 'editor-folder', createId),
    editorSuggestionGroups: idMap(
      Object.keys(state.editorSuggestionGroups),
      'editor-suggestion-group',
      createId,
    ),
    editorSuggestions: idMap(
      Object.keys(state.editorSuggestions),
      'editor-suggestion',
      createId,
    ),
    fileAssets: idMap(Object.keys(state.fileAssets), 'file', createId),
    fileGroups: idMap(Object.keys(state.fileGroups), 'file-group', createId),
    fileSections: idMap(Object.keys(state.fileLibrarySections), 'file-section', createId),
    knowledgeGroups: idMap(
      Object.keys(state.knowledgeSessionGroups),
      'knowledge-session-group',
      createId,
    ),
    knowledgeItems: idMap(Object.keys(state.knowledgeItems), 'knowledge-item', createId),
    knowledgeSessions: idMap(Object.keys(state.knowledgeSessions), 'ks', createId),
    vectorIndexes: idMap(Object.keys(state.vectorIndexes), 'vector-index', createId),
  }

  const chatRules = Object.fromEntries(Object.values(state.chatRules).map((rule) => {
    const detached = {
      ...rule,
      id: replace(maps.chatRules, rule.id),
      linkedContextRefs: rule.linkedContextRefs?.map((reference) => (
        contextReference(reference, maps)
      )),
    }
    delete detached.access
    delete detached.serverComposedChars
    delete detached.serverRevision
    delete detached.serverTemplateId
    return [detached.id, detached]
  }))
  const chatThreads = Object.fromEntries(Object.values(state.chatThreads).map((thread) => {
    const id = replace(maps.chatThreads, thread.id)
    return [id, {
      ...thread,
      id,
      messages: thread.messages.map((message) => ({
        ...message,
        attachments: message.attachments?.map((attachment) => (
          messageAttachment(attachment, maps)
        )),
        id: replace(maps.chatMessages, `${thread.id}\u0000${message.id}`),
        requestContext: undefined,
      })),
    }]
  }))
  const editorDocuments = Object.fromEntries(
    Object.values(state.editorDocuments).map((document) => {
      const detached = {
        ...document,
        contentMode: 'markdown' as const,
        folderId: replaceNullable(maps.editorFolders, document.folderId),
        id: replace(maps.editorDocuments, document.id),
        revision: 0,
      }
      delete detached.access
      delete detached.collaboration
      delete detached.metadataRevision
      delete detached.recovery
      delete detached.serverSynced
      return [detached.id, detached]
    }),
  )
  const editorComments = Object.fromEntries(
    Object.values(state.editorComments).map((comment) => {
      const id = replace(maps.editorComments, comment.id)
      return [id, {
        ...comment,
        documentId: replace(maps.editorDocuments, comment.documentId),
        id,
      }]
    }),
  )
  const editorSuggestionGroups = Object.fromEntries(
    Object.values(state.editorSuggestionGroups).map((group) => {
      const id = replace(maps.editorSuggestionGroups, group.id)
      return [id, {
        ...group,
        documentId: replace(maps.editorDocuments, group.documentId),
        id,
        origin: suggestionOrigin(group.origin, maps),
      }]
    }),
  )
  const editorSuggestions = Object.fromEntries(
    Object.values(state.editorSuggestions).map((suggestion) => {
      const id = replace(maps.editorSuggestions, suggestion.id)
      return [id, {
        ...suggestion,
        collaborationPublication: undefined,
        documentId: replace(maps.editorDocuments, suggestion.documentId),
        groupId: replace(maps.editorSuggestionGroups, suggestion.groupId),
        id,
        origin: suggestionOrigin(suggestion.origin, maps),
      }]
    }),
  )
  const fileAssets = Object.fromEntries(Object.values(state.fileAssets).map((asset) => {
    const id = replace(maps.fileAssets, asset.id)
    return [id, {
      ...asset,
      deletionError: null,
      deletionOperationId: null,
      deletionStage: null,
      groupId: replaceNullable(maps.fileGroups, asset.groupId),
      id,
      lifecycleStatus: 'active' as const,
      sectionId: replace(maps.fileSections, asset.sectionId),
      serverFileId: null,
      serverSynced: false,
    }]
  }))
  const vectorIndexes = Object.fromEntries(
    Object.values(state.vectorIndexes).map((index) => {
      const id = replace(maps.vectorIndexes, index.id)
      const members = index.members.map((member) => ({
        fileId: replace(maps.fileAssets, member.fileId),
        state: member.state === 'skipped' ? 'skipped' as const : 'pending' as const,
      }))
      return [id, {
        ...index,
        history: [],
        id,
        lastError: null,
        members,
        serverCollectionId: null,
        serverCollectionModel: null,
        status: members.some((member) => member.state === 'pending')
          ? 'stale' as const
          : 'ready' as const,
      }]
    }),
  )
  const knowledgeItems = Object.fromEntries(
    Object.values(state.knowledgeItems).map((item) => {
      const id = replace(maps.knowledgeItems, item.id)
      return [id, {
        ...item,
        collectionIds: undefined,
        id,
        runId: null,
        sessionId: replace(maps.knowledgeSessions, item.sessionId),
        status: item.status === 'running' ? 'cancelled' as const : item.status,
      }]
    }),
  )
  const agentSessions = Object.fromEntries(
    Object.values(state.agentSessions)
      .filter((session) => session.persistable !== false)
      .map((session) => {
        const id = replace(maps.agentSessions, session.id)
        return [id, {
          ...session,
          groupId: replaceNullable(maps.agentGroups, session.groupId),
          id,
          persistable: true,
          runIds: [],
        }]
      }),
  )

  return {
    ...state,
    agentCanvas: EMPTY_CANVAS_STATE,
    agentPlanDrafts: {},
    agentRuns: {},
    agentSessionArtifacts: {},
    agentSessionGroupOrder: state.agentSessionGroupOrder.map((id) => (
      replace(maps.agentGroups, id)
    )),
    agentSessionGroups: Object.fromEntries(
      Object.values(state.agentSessionGroups).map((group) => {
        const id = replace(maps.agentGroups, group.id)
        return [id, { ...group, id }]
      }),
    ),
    agentSessionOrder: state.agentSessionOrder
      .filter((id) => maps.agentSessions.has(id))
      .map((id) => replace(maps.agentSessions, id)),
    agentSessions,
    chatRuleOrder: state.chatRuleOrder.map((id) => replace(maps.chatRules, id)),
    chatRules,
    chatThreadGroupMemberships: Object.fromEntries(
      Object.entries(state.chatThreadGroupMemberships).map(([threadId, groupId]) => [
        replace(maps.chatThreads, threadId),
        replaceNullable(maps.chatGroups, groupId),
      ]),
    ),
    chatThreadGroupOrder: state.chatThreadGroupOrder.map((id) => (
      replace(maps.chatGroups, id)
    )),
    chatThreadGroups: Object.fromEntries(
      Object.values(state.chatThreadGroups).map((group) => {
        const id = replace(maps.chatGroups, group.id)
        return [id, { ...group, id }]
      }),
    ),
    chatThreadOrder: state.chatThreadOrder.map((id) => replace(maps.chatThreads, id)),
    chatThreads,
    dirty: true,
    editorCommentOutbox: {},
    editorComments,
    editorDocumentOrder: state.editorDocumentOrder.map((id) => (
      replace(maps.editorDocuments, id)
    )),
    editorDocuments,
    editorFolderOrder: state.editorFolderOrder.map((id) => replace(maps.editorFolders, id)),
    editorFolders: Object.fromEntries(Object.values(state.editorFolders).map((folder) => {
      const id = replace(maps.editorFolders, folder.id)
      return [id, { ...folder, id }]
    })),
    editorSuggestionGroups,
    editorSuggestions,
    editorUi: {
      ...state.editorUi,
      activeDocumentId: replaceNullable(
        maps.editorDocuments,
        state.editorUi.activeDocumentId,
      ),
      openDocumentIds: state.editorUi.openDocumentIds.map((id) => (
        replace(maps.editorDocuments, id)
      )),
      selectedCommentId: replaceNullable(
        maps.editorComments,
        state.editorUi.selectedCommentId,
      ),
    },
    fileAssetOrder: state.fileAssetOrder.map((id) => replace(maps.fileAssets, id)),
    fileAssets,
    fileGroupOrder: state.fileGroupOrder.map((id) => replace(maps.fileGroups, id)),
    fileGroups: Object.fromEntries(Object.values(state.fileGroups).map((group) => {
      const id = replace(maps.fileGroups, group.id)
      return [id, {
        ...group,
        id,
        sectionId: replace(maps.fileSections, group.sectionId),
      }]
    })),
    fileLibrarySectionOrder: state.fileLibrarySectionOrder.map((id) => (
      replace(maps.fileSections, id)
    )),
    fileLibrarySections: Object.fromEntries(
      Object.values(state.fileLibrarySections).map((section) => {
        const id = replace(maps.fileSections, section.id)
        return [id, {
          ...section,
          deletionError: null,
          deletionOperationId: null,
          deletionStage: null,
          id,
          lifecycleStatus: 'active' as const,
          serverSynced: false,
        }]
      }),
    ),
    indexingJobs: {},
    knowledgeItemOrder: state.knowledgeItemOrder.map((id) => (
      replace(maps.knowledgeItems, id)
    )),
    knowledgeItems,
    knowledgeSessionGroupMemberships: Object.fromEntries(
      Object.entries(state.knowledgeSessionGroupMemberships).map(([sessionId, groupId]) => [
        replace(maps.knowledgeSessions, sessionId),
        replaceNullable(maps.knowledgeGroups, groupId),
      ]),
    ),
    knowledgeSessionGroupOrder: state.knowledgeSessionGroupOrder.map((id) => (
      replace(maps.knowledgeGroups, id)
    )),
    knowledgeSessionGroups: Object.fromEntries(
      Object.values(state.knowledgeSessionGroups).map((group) => {
        const id = replace(maps.knowledgeGroups, group.id)
        return [id, { ...group, id }]
      }),
    ),
    knowledgeSessionOrder: state.knowledgeSessionOrder.map((id) => (
      replace(maps.knowledgeSessions, id)
    )),
    knowledgeSessions: Object.fromEntries(
      Object.values(state.knowledgeSessions).map((session) => {
        const id = replace(maps.knowledgeSessions, session.id)
        return [id, { ...session, id, isBootstrapPlaceholder: false }]
      }),
    ),
    researchRuns: Object.fromEntries(Object.entries(state.researchRuns).map(([id, run]) => {
      const detached = { ...run, source: 'imported' as const }
      delete detached.access
      return [id, detached]
    })),
    selectedAgentSessionId: state.selectedAgentSessionId
      ? maps.agentSessions.get(state.selectedAgentSessionId) ?? null
      : null,
    selectedKnowledgeSessionId: replaceNullable(
      maps.knowledgeSessions,
      state.selectedKnowledgeSessionId,
    ),
    serverSyncEnabled: false,
    ui: {
      ...state.ui,
      pendingChatAttachmentRefs: state.ui.pendingChatAttachmentRefs.map((reference) => (
        contextReference(reference, maps)
      )),
      pinnedExplorer: {
        agentSessionIds: state.ui.pinnedExplorer.agentSessionIds
          .filter((id) => maps.agentSessions.has(id))
          .map((id) => replace(maps.agentSessions, id)),
        chatThreadIds: state.ui.pinnedExplorer.chatThreadIds.map((id) => (
          replace(maps.chatThreads, id)
        )),
        editorDocumentIds: state.ui.pinnedExplorer.editorDocumentIds.map((id) => (
          replace(maps.editorDocuments, id)
        )),
        knowledgeSessionIds: state.ui.pinnedExplorer.knowledgeSessionIds.map((id) => (
          replace(maps.knowledgeSessions, id)
        )),
      },
      selectedAgentSessionId: state.ui.selectedAgentSessionId
        ? maps.agentSessions.get(state.ui.selectedAgentSessionId) ?? null
        : null,
      selectedChatThreadId: replaceNullable(
        maps.chatThreads,
        state.ui.selectedChatThreadId,
      ),
    },
    vectorIndexOrder: state.vectorIndexOrder.map((id) => (
      replace(maps.vectorIndexes, id)
    )),
    vectorIndexes,
    workspaceId: targetWorkspaceId,
  }
}

/**
 * Apply the ownership policy for a project file selected by the user.
 *
 * A workspace id is routing context, not proof that the file's resources
 * belong to the authenticated principal. Account-synced imports therefore
 * always become detached clones, even when source and target workspace ids
 * happen to match. Offline/local-first loading retains the historical exact
 * file state because no server ownership boundary is crossed.
 */
export function prepareProjectFileImport(
  state: ProjectState,
  targetWorkspaceId: string,
  accountSyncActive: boolean,
  createId: IdFactory = createProjectEntityId,
): ProjectState {
  if (!accountSyncActive) return state
  return detachProjectResourceGraph(state, targetWorkspaceId, createId)
}
