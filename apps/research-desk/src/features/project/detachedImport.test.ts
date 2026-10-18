import { describe, expect, it } from 'vitest'

import { createEmptyProjectState } from './seedProject'
import { detachProjectResourceGraph, prepareProjectFileImport } from './detachedImport'
import type { ProjectState } from './types'

const NOW = '2026-07-15T12:00:00.000Z'

function factory(label: string) {
  let sequence = 0
  return (prefix: string) => `${prefix}-${label}-${++sequence}`
}

function sourceProject(): ProjectState {
  const base = createEmptyProjectState()
  const sectionId = base.fileLibrarySectionOrder[0]
  const knowledgeSessionId = base.knowledgeSessionOrder[0]
  return {
    ...base,
    agentSessionGroupOrder: ['ag-1'],
    agentSessionGroups: {
      'ag-1': { createdAt: NOW, id: 'ag-1', title: 'Agents', updatedAt: NOW },
    },
    agentSessionOrder: ['as-1'],
    agentSessions: {
      'as-1': {
        createdAt: NOW,
        groupId: 'ag-1',
        id: 'as-1',
        runIds: ['run-server-source'],
        sourcePolicy: { knowledge: 'available', web: 'disabled' },
        title: 'Agent session',
        updatedAt: NOW,
      },
    },
    chatRuleOrder: ['rule-1'],
    chatRules: {
      'rule-1': {
        access: { mode: 'shared', permission: 'view' },
        category: 'context',
        contentMarkdown: 'Use the file.',
        createdAt: NOW,
        id: 'rule-1',
        label: 'rule',
        linkedContextRefs: [{ fileId: 'file-1', kind: 'file-asset' }],
        serverComposedChars: 1_250,
        serverRevision: 4,
        serverTemplateId: 'pt-source',
        title: 'Rule',
        updatedAt: NOW,
      },
    },
    chatThreadGroupMemberships: { 'thread-1': 'chat-group-1', 'thread-2': null },
    chatThreadGroupOrder: ['chat-group-1'],
    chatThreadGroups: {
      'chat-group-1': {
        createdAt: NOW,
        id: 'chat-group-1',
        title: 'Chats',
        updatedAt: NOW,
      },
    },
    chatThreadOrder: ['thread-1', 'thread-2'],
    chatThreads: {
      'thread-1': {
        createdAt: NOW,
        id: 'thread-1',
        messages: [{
          attachments: [
            {
              attachedAt: NOW,
              contentMarkdown: 'file',
              fileId: 'file-1',
              kind: 'file-asset',
              label: 'file',
              pageCount: 1,
              sizeBytes: 4,
              title: 'File',
            },
            {
              attachedAt: NOW,
              contentMarkdown: 'rule',
              kind: 'chat-rule',
              label: 'rule',
              ruleId: 'rule-1',
              title: 'Rule',
            },
          ],
          contentMarkdown: 'Question',
          createdAt: NOW,
          id: 'message-shared-old-id',
          requestContext: { knowledgeCollectionIds: ['kc-source'] },
          role: 'user',
        }],
        preview: 'Question',
        source: 'imported',
        title: 'One',
        updatedAt: NOW,
      },
      'thread-2': {
        createdAt: NOW,
        id: 'thread-2',
        messages: [{
          contentMarkdown: 'Same old message id in another thread',
          createdAt: NOW,
          id: 'message-shared-old-id',
          role: 'user',
        }],
        preview: 'Same old message id',
        source: 'imported',
        title: 'Two',
        updatedAt: NOW,
      },
    },
    editorComments: {
      'comment-1': {
        anchor: {
          from: 0,
          quoteAfter: '',
          quoteBefore: '',
          selectedText: 'Text',
          to: 4,
        },
        commentMarkdown: 'Review',
        createdAt: NOW,
        documentId: 'document-1',
        id: 'comment-1',
        kind: 'collect',
        status: 'open',
        updatedAt: NOW,
      },
    },
    editorDocumentOrder: ['document-1'],
    editorDocuments: {
      'document-1': {
        access: { mode: 'shared', permission: 'view' },
        collaboration: {
          generation: 2,
          persistedSequence: 4,
          projectionSequence: 4,
          schemaVersion: 1,
        },
        contentMarkdown: 'Text',
        contentMode: 'collaboration',
        createdAt: NOW,
        folderId: 'folder-1',
        id: 'document-1',
        metadataRevision: 7,
        recovery: {
          capturedAt: NOW,
          originalDocumentId: 'deleted-document',
          reason: 'remote_deleted',
        },
        revision: 8,
        serverSynced: true,
        source: 'pasted',
        title: 'Document',
        updatedAt: NOW,
      },
    },
    editorFolderOrder: ['folder-1'],
    editorFolders: {
      'folder-1': { createdAt: NOW, id: 'folder-1', title: 'Folder', updatedAt: NOW },
    },
    fileAssetOrder: ['file-1'],
    fileAssets: {
      'file-1': {
        createdAt: NOW,
        extractedText: 'Text',
        fileName: 'file.txt',
        groupId: 'file-group-1',
        id: 'file-1',
        label: 'file',
        mimeType: 'text/plain',
        origin: 'library',
        pageCount: 1,
        parseStatus: 'parsed',
        parseWarning: null,
        deletionError: 'retry',
        deletionOperationId: 'del-source',
        deletionStage: 'delete_failed',
        lifecycleStatus: 'delete_failed',
        sectionId,
        serverFileId: 'fl-source',
        serverSynced: true,
        sizeBytes: 4,
        textTruncated: false,
        title: 'File',
        updatedAt: NOW,
      },
    },
    fileGroupOrder: ['file-group-1'],
    fileGroups: {
      'file-group-1': {
        createdAt: NOW,
        id: 'file-group-1',
        sectionId,
        title: 'Files',
        updatedAt: NOW,
      },
    },
    knowledgeSessionGroupMemberships: { [knowledgeSessionId]: 'kg-1' },
    knowledgeSessionGroupOrder: ['kg-1'],
    knowledgeSessionGroups: {
      'kg-1': { createdAt: NOW, id: 'kg-1', title: 'Knowledge', updatedAt: NOW },
    },
    selectedAgentSessionId: 'as-1',
    serverSyncEnabled: true,
    ui: {
      ...base.ui,
      pendingChatAttachmentRefs: [{ fileId: 'file-1', kind: 'file-asset' }],
      pinnedExplorer: {
        agentSessionIds: ['as-1'],
        chatThreadIds: ['thread-1'],
        editorDocumentIds: ['document-1'],
        knowledgeSessionIds: [knowledgeSessionId],
      },
      selectedAgentSessionId: 'as-1',
      selectedChatThreadId: 'thread-1',
    },
    vectorIndexOrder: ['index-1'],
    vectorIndexes: {
      'index-1': {
        createdAt: NOW,
        dims: 3072,
        handle: 'files',
        history: [{
          documents: 1,
          durationMs: 10,
          finishedAt: NOW,
          result: 'ok',
          startedAt: NOW,
        }],
        id: 'index-1',
        members: [{
          fileId: 'file-1',
          serverDocumentId: 'kd-source',
          state: 'embedded',
        }],
        model: 'text-embedding-3-large',
        serverCollectionId: 'kc-source',
        serverCollectionModel: 'text-embedding-3-large',
        status: 'ready',
        title: 'Index',
        updatedAt: NOW,
      },
    },
  }
}

describe('detachProjectResourceGraph', () => {
  it('allocates isolated ownership ids and rewrites the complete local graph', () => {
    const source = sourceProject()
    const cloneA = detachProjectResourceGraph(source, 'workspace-user-a', factory('a'))
    const cloneB = detachProjectResourceGraph(source, 'workspace-user-b', factory('b'))

    const threadA = cloneA.chatThreads[cloneA.chatThreadOrder[0]]
    const threadB = cloneB.chatThreads[cloneB.chatThreadOrder[0]]
    const secondThreadA = cloneA.chatThreads[cloneA.chatThreadOrder[1]]
    const assetA = cloneA.fileAssets[cloneA.fileAssetOrder[0]]
    const groupA = cloneA.fileGroups[cloneA.fileGroupOrder[0]]
    const documentA = cloneA.editorDocuments[cloneA.editorDocumentOrder[0]]
    const commentA = Object.values(cloneA.editorComments)[0]
    const indexA = cloneA.vectorIndexes[cloneA.vectorIndexOrder[0]]
    const sessionA = cloneA.agentSessions[cloneA.agentSessionOrder[0]]
    const knowledgeSessionA = cloneA.knowledgeSessions[cloneA.knowledgeSessionOrder[0]]

    expect(cloneA.chatThreadOrder).not.toEqual(cloneB.chatThreadOrder)
    expect(cloneA.fileAssetOrder).not.toEqual(cloneB.fileAssetOrder)
    expect(threadA.id).not.toBe('thread-1')
    expect(threadA.messages[0].id).not.toBe(secondThreadA.messages[0].id)
    expect(cloneA.chatThreadGroupMemberships[threadA.id]).toBe(
      cloneA.chatThreadGroupOrder[0],
    )
    expect(threadA.messages[0].attachments?.[0]).toMatchObject({
      fileId: assetA.id,
    })
    expect(threadA.messages[0].attachments?.[1]).toMatchObject({
      ruleId: cloneA.chatRuleOrder[0],
    })
    expect(threadA.messages[0].requestContext).toBeUndefined()
    expect(cloneA.chatRules[cloneA.chatRuleOrder[0]]).not.toHaveProperty(
      'serverTemplateId',
    )
    // A size the server measured belongs to its template, which the
    // detached copy no longer is.
    expect(cloneA.chatRules[cloneA.chatRuleOrder[0]]).not.toHaveProperty(
      'serverComposedChars',
    )
    expect(assetA.groupId).toBe(groupA.id)
    expect(groupA.sectionId).toBe(assetA.sectionId)
    expect(assetA.serverFileId).toBeNull()
    expect(assetA).toMatchObject({
      deletionError: null,
      deletionOperationId: null,
      deletionStage: null,
      lifecycleStatus: 'active',
      serverSynced: false,
    })
    expect(documentA.folderId).toBe(cloneA.editorFolderOrder[0])
    expect(documentA).toMatchObject({ contentMode: 'markdown', revision: 0 })
    expect(documentA).not.toHaveProperty('access')
    expect(documentA).not.toHaveProperty('collaboration')
    expect(documentA).not.toHaveProperty('recovery')
    expect(documentA).not.toHaveProperty('serverSynced')
    expect(commentA.documentId).toBe(documentA.id)
    expect(indexA.members).toEqual([{ fileId: assetA.id, state: 'pending' }])
    expect(indexA).toMatchObject({
      history: [],
      serverCollectionId: null,
      serverCollectionModel: null,
      status: 'stale',
    })
    expect(sessionA.groupId).toBe(cloneA.agentSessionGroupOrder[0])
    expect(sessionA.runIds).toEqual([])
    expect(cloneA.selectedAgentSessionId).toBe(sessionA.id)
    expect(cloneA.selectedKnowledgeSessionId).toBe(knowledgeSessionA.id)
    expect(cloneA.ui.pinnedExplorer.knowledgeSessionIds).toEqual([
      knowledgeSessionA.id,
    ])
    expect(cloneA.workspaceId).toBe('workspace-user-a')
    expect(cloneA.serverSyncEnabled).toBe(false)
    expect(cloneA.dirty).toBe(true)

    expect(source.chatThreadOrder).toEqual(['thread-1', 'thread-2'])
    expect(source.fileAssets['file-1'].serverFileId).toBe('fl-source')
    expect(threadB.id).not.toBe(threadA.id)
  })

  it('detaches account imports even when the workspace id matches', () => {
    const source = { ...sourceProject(), workspaceId: 'shared-workspace-id' }

    const imported = prepareProjectFileImport(
      source,
      'shared-workspace-id',
      true,
      factory('same-workspace'),
    )

    expect(imported.workspaceId).toBe(source.workspaceId)
    expect(imported.chatThreadOrder).not.toEqual(source.chatThreadOrder)
    expect(imported.fileAssetOrder).not.toEqual(source.fileAssetOrder)
    expect(imported.editorDocumentOrder).not.toEqual(source.editorDocumentOrder)
    expect(imported.serverSyncEnabled).toBe(false)
  })

  it('preserves exact file state when no account sync boundary exists', () => {
    const source = sourceProject()

    expect(prepareProjectFileImport(source, source.workspaceId, false)).toBe(source)
  })
})
