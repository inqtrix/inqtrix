import { describe, expect, it } from 'vitest'

import { createEmptyProjectState } from '@/features/project/seedProject'
import type {
  EditorCommentThreadRecord,
  EditorDocumentRecord,
  EditorFolderRecord,
  EditorSuggestionRecord,
  ProjectState,
} from '@/features/project/types'
import { researchDeskReducer } from '@/features/researchDesk/state'
import {
  planEditorCommentReconciliation,
  planEditorDocumentAutosave,
} from './useEditorHistoryApi'

function doc(
  id: string,
  overrides: Partial<EditorDocumentRecord> = {},
): EditorDocumentRecord {
  return {
    contentMarkdown: '',
    createdAt: '2026-01-01T00:00:00.000Z',
    folderId: null,
    id,
    revision: 1,
    source: 'blank',
    title: id,
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

function comment(id: string, documentId: string): EditorCommentThreadRecord {
  return {
    anchor: { from: 0, to: 1, selectedText: 'x', quoteBefore: '', quoteAfter: '' },
    commentMarkdown: id,
    createdAt: '2026-01-01T00:00:00.000Z',
    documentId,
    id,
    kind: 'collect',
    status: 'open',
    updatedAt: '2026-01-01T00:00:00.000Z',
  }
}

function withDoc(local: EditorDocumentRecord): ProjectState {
  const base = createEmptyProjectState()
  return {
    ...base,
    dirty: false,
    editorDocumentOrder: [local.id],
    editorDocuments: { [local.id]: local },
  }
}

describe('server editor hydration', () => {
  it('adds server documents with empty body + order, WITHOUT dirtying', () => {
    const next = researchDeskReducer(createEmptyProjectState(), {
      documents: [doc('ed_1', { title: 'From server', folderId: 'edf_1' })],
      type: 'upsertServerEditorDocuments',
    })
    expect(next.editorDocuments.ed_1.title).toBe('From server')
    expect(next.editorDocuments.ed_1.contentMarkdown).toBe('') // body loads on open
    expect(next.editorDocuments.ed_1.folderId).toBe('edf_1')
    expect(next.editorDocumentOrder).toContain('ed_1')
    expect(next.dirty).toBe(false)
  })

  it('keeps a newer local document body + metadata over an older server version', () => {
    const local = doc('ed_1', {
      contentMarkdown: 'local body',
      title: 'Local edit',
      updatedAt: '2026-02-01T00:00:00.000Z',
    })
    const next = researchDeskReducer(withDoc(local), {
      documents: [doc('ed_1', { title: 'Stale server', updatedAt: '2026-01-01T00:00:00.000Z' })],
      type: 'upsertServerEditorDocuments',
    })
    expect(next.editorDocuments.ed_1.title).toBe('Local edit')
    expect(next.editorDocuments.ed_1.contentMarkdown).toBe('local body')
  })

  it('takes newer server metadata but KEEPS the local (loaded) body', () => {
    const local = doc('ed_1', {
      contentMarkdown: 'loaded body',
      title: 'old',
      updatedAt: '2026-01-01T00:00:00.000Z',
    })
    const next = researchDeskReducer(withDoc(local), {
      documents: [doc('ed_1', { title: 'newer server', updatedAt: '2026-03-01T00:00:00.000Z' })],
      type: 'upsertServerEditorDocuments',
    })
    expect(next.editorDocuments.ed_1.title).toBe('newer server')
    expect(next.editorDocuments.ed_1.contentMarkdown).toBe('loaded body') // body kept
  })

  it('removes revoked shared documents while preserving local owner work', () => {
    const revoked = doc('ed_revoked', {
      access: {
        mode: 'shared',
        owner: { id: 'user_admin', name: 'Admin' },
        permission: 'edit',
      },
    })
    const kept = doc('ed_kept', {
      access: {
        mode: 'shared',
        owner: { id: 'user_admin', name: 'Admin' },
        permission: 'suggest',
      },
    })
    const local = doc('ed_local', {
      access: { mode: 'owner', permission: 'edit' },
      contentMarkdown: 'offline owner work',
    })
    const base = createEmptyProjectState()
    const state: ProjectState = {
      ...base,
      dirty: false,
      editorCommentOutbox: {
        revoked_comment: {
          documentId: revoked.id,
          operation: 'upsert',
          updatedAt: '2026-01-01T00:00:00.000Z',
        },
      },
      editorComments: {
        kept_comment: comment('kept_comment', kept.id),
        revoked_comment: comment('revoked_comment', revoked.id),
      },
      editorDocumentOrder: [revoked.id, kept.id, local.id],
      editorDocuments: {
        [kept.id]: kept,
        [local.id]: local,
        [revoked.id]: revoked,
      },
      editorSuggestionGroups: {
        revoked_group: {
          createdAt: '2026-01-01T00:00:00.000Z',
          documentId: revoked.id,
          id: 'revoked_group',
          origin: { kind: 'global_run' },
        },
      },
      editorSuggestions: {
        revoked_suggestion: {
          anchor: {
            from: 0,
            quoteAfter: '',
            quoteBefore: '',
            selectedText: 'x',
            to: 1,
          },
          blockId: 'block-1',
          createdAt: '2026-01-01T00:00:00.000Z',
          documentId: revoked.id,
          groupId: 'revoked_group',
          id: 'revoked_suggestion',
          originalText: 'x',
          origin: { kind: 'global_run' },
          proposedText: 'y',
          status: 'pending',
          updatedAt: '2026-01-01T00:00:00.000Z',
        },
      },
      editorUi: {
        ...base.editorUi,
        activeDocumentId: revoked.id,
        openDocumentIds: [revoked.id, kept.id],
        selectedCommentId: 'revoked_comment',
      },
      ui: {
        ...base.ui,
        pinnedExplorer: {
          ...base.ui.pinnedExplorer,
          editorDocumentIds: [revoked.id, kept.id],
        },
      },
    }

    const next = researchDeskReducer(state, {
      documents: [{
        ...kept,
        title: 'Kept from server',
        updatedAt: '2026-01-02T00:00:00.000Z',
      }],
      type: 'reconcileServerEditorDocuments',
    })

    expect(next.editorDocuments.ed_revoked).toBeUndefined()
    expect(next.editorDocuments.ed_kept.title).toBe('Kept from server')
    expect(next.editorDocuments.ed_local.contentMarkdown).toBe('offline owner work')
    expect(next.editorDocumentOrder).toEqual(['ed_kept', 'ed_local'])
    expect(next.editorComments.revoked_comment).toBeUndefined()
    expect(next.editorComments.kept_comment).toBeDefined()
    expect(next.editorCommentOutbox?.revoked_comment).toBeUndefined()
    expect(next.editorSuggestionGroups.revoked_group).toBeUndefined()
    expect(next.editorSuggestions.revoked_suggestion).toBeUndefined()
    expect(next.editorUi).toMatchObject({
      activeDocumentId: 'ed_kept',
      openDocumentIds: ['ed_kept'],
      selectedCommentId: null,
    })
    expect(next.ui.pinnedExplorer.editorDocumentIds).toEqual(['ed_kept'])
    expect(next.dirty).toBe(false)
  })

  it('removes a missing server owner document while preserving a never-synced local draft', () => {
    const removedOwner = doc('ed_removed_owner', {
      access: { mode: 'owner', permission: 'edit' },
      contentMarkdown: 'server-confirmed body',
      serverSynced: true,
    })
    const localDraft = doc('ed_local_draft', {
      contentMarkdown: 'never-synced local work',
      revision: 0,
    })
    const retainedServerDocument = doc('ed_retained', {
      access: { mode: 'owner', permission: 'edit' },
      serverSynced: true,
    })
    const base = createEmptyProjectState()
    const state: ProjectState = {
      ...base,
      dirty: false,
      editorCommentOutbox: {
        removed_comment: {
          documentId: removedOwner.id,
          operation: 'upsert',
          updatedAt: '2026-01-01T00:00:00.000Z',
        },
      },
      editorComments: {
        removed_comment: comment('removed_comment', removedOwner.id),
      },
      editorDocumentOrder: [
        removedOwner.id,
        localDraft.id,
        retainedServerDocument.id,
      ],
      editorDocuments: {
        [localDraft.id]: localDraft,
        [removedOwner.id]: removedOwner,
        [retainedServerDocument.id]: retainedServerDocument,
      },
      editorSuggestionGroups: {
        removed_group: {
          createdAt: '2026-01-01T00:00:00.000Z',
          documentId: removedOwner.id,
          id: 'removed_group',
          origin: { kind: 'global_run' },
        },
      },
      editorSuggestions: {
        removed_suggestion: {
          anchor: {
            from: 0,
            quoteAfter: '',
            quoteBefore: '',
            selectedText: 'server-confirmed body',
            to: 4,
          },
          blockId: 'block-removed',
          createdAt: '2026-01-01T00:00:00.000Z',
          documentId: removedOwner.id,
          groupId: 'removed_group',
          id: 'removed_suggestion',
          originalText: 'server',
          origin: { kind: 'global_run' },
          proposedText: 'local',
          status: 'pending',
          updatedAt: '2026-01-01T00:00:00.000Z',
        },
      },
      editorUi: {
        ...base.editorUi,
        activeDocumentId: removedOwner.id,
        openDocumentIds: [removedOwner.id, retainedServerDocument.id],
        selectedCommentId: 'removed_comment',
      },
      ui: {
        ...base.ui,
        pinnedExplorer: {
          ...base.ui.pinnedExplorer,
          editorDocumentIds: [removedOwner.id],
        },
      },
    }

    const next = researchDeskReducer(state, {
      documents: [retainedServerDocument],
      type: 'reconcileServerEditorDocuments',
    })

    expect(next.editorDocuments.ed_removed_owner).toBeUndefined()
    expect(next.editorDocuments.ed_local_draft).toMatchObject({
      contentMarkdown: 'never-synced local work',
      revision: 0,
    })
    expect(next.editorComments.removed_comment).toBeUndefined()
    expect(next.editorCommentOutbox?.removed_comment).toBeUndefined()
    expect(next.editorSuggestionGroups.removed_group).toBeUndefined()
    expect(next.editorSuggestions.removed_suggestion).toBeUndefined()
    expect(next.editorUi).toMatchObject({
      activeDocumentId: retainedServerDocument.id,
      openDocumentIds: [retainedServerDocument.id],
      selectedCommentId: null,
    })
    expect(next.ui.pinnedExplorer.editorDocumentIds).toEqual([])
    expect(next.dirty).toBe(false)
  })

  it('moves only unconfirmed local work into a separate recovery document', () => {
    const removedOwner = doc('ed_removed_owner', {
      access: { mode: 'owner', permission: 'edit' },
      collaboration: {
        generation: 3,
        persistedSequence: 12,
        projectionSequence: 12,
        schemaVersion: 1,
      },
      contentMarkdown: 'last confirmed projection',
      contentMode: 'collaboration',
      metadataRevision: 4,
      serverSynced: true,
    })
    const pendingComment = comment('pending_comment', removedOwner.id)
    pendingComment.suggestionDraft = {
      anchorVersion: 1,
      createdAt: '2026-01-01T00:00:00.000Z',
      groupId: 'pending_group',
      patchId: '00000000-0000-4000-8000-000000000003',
      proposedText: 'recovered',
      publicationCommandId: '00000000-0000-4000-8000-000000000002',
      revision: 1,
      suggestionId: 'pending_suggestion',
      updatedAt: '2026-01-01T00:00:00.000Z',
    }
    const confirmedComment = comment('confirmed_comment', removedOwner.id)
    const base = createEmptyProjectState()
    const state: ProjectState = {
      ...base,
      dirty: false,
      editorCommentOutbox: {
        [pendingComment.id]: {
          documentId: removedOwner.id,
          operation: 'upsert',
          updatedAt: pendingComment.updatedAt,
        },
      },
      editorComments: {
        [confirmedComment.id]: confirmedComment,
        [pendingComment.id]: pendingComment,
      },
      editorDocumentOrder: [removedOwner.id],
      editorDocuments: { [removedOwner.id]: removedOwner },
      editorSuggestionGroups: {
        pending_group: {
          createdAt: '2026-01-01T00:00:00.000Z',
          documentId: removedOwner.id,
          id: 'pending_group',
          origin: {
            commentId: pendingComment.id,
            kind: 'inline_edit',
          },
        },
      },
      editorSuggestions: {
        pending_suggestion: {
          anchor: pendingComment.anchor,
          blockId: 'pending-block',
          collaborationPublication: {
            commandId: 'old-command',
            patchId: 'old-patch',
            sequence: 13,
            suggestionIds: ['pending_suggestion'],
          },
          createdAt: '2026-01-01T00:00:00.000Z',
          documentId: removedOwner.id,
          groupId: 'pending_group',
          id: 'pending_suggestion',
          originalText: 'server',
          origin: {
            commentId: pendingComment.id,
            kind: 'inline_edit',
          },
          privateDraft: {
            patchId: '00000000-0000-4000-8000-000000000003',
            publicationCommandId: '00000000-0000-4000-8000-000000000002',
            revision: 1,
          },
          proposedText: 'recovered',
          status: 'pending',
          updatedAt: '2026-01-01T00:00:00.000Z',
        },
      },
      editorUi: {
        ...base.editorUi,
        activeDocumentId: removedOwner.id,
        openDocumentIds: [removedOwner.id],
        selectedCommentId: pendingComment.id,
      },
    }

    const recovered = researchDeskReducer(state, {
      documents: [],
      recoveryCaptures: [{
        capturedAt: '2026-01-02T00:00:00.000Z',
        contentMarkdown: 'unconfirmed local document state',
        documentId: removedOwner.id,
      }],
      type: 'reconcileServerEditorDocuments',
    })
    const recoveryId = recovered.editorDocumentOrder[0]
    const recovery = recovered.editorDocuments[recoveryId]
    const recoveredComments = Object.values(recovered.editorComments)

    expect(recoveryId).not.toBe(removedOwner.id)
    expect(recovery).toMatchObject({
      contentMarkdown: 'unconfirmed local document state',
      recovery: {
        capturedAt: '2026-01-02T00:00:00.000Z',
        originalDocumentId: removedOwner.id,
        reason: 'remote_deleted',
      },
      revision: 0,
      title: removedOwner.title,
    })
    expect(recovery.access).toBeUndefined()
    expect(recovery.collaboration).toBeUndefined()
    expect(recovery.contentMode).toBeUndefined()
    expect(recovery.metadataRevision).toBeUndefined()
    expect(recovery.serverSynced).toBeUndefined()
    expect(recoveredComments).toHaveLength(1)
    expect(recoveredComments[0]).toMatchObject({
      commentMarkdown: pendingComment.commentMarkdown,
      documentId: recoveryId,
    })
    expect(recoveredComments[0].id).not.toBe(pendingComment.id)
    expect(recoveredComments[0].suggestionDraft).toBeUndefined()
    expect(recovered.editorCommentOutbox).toEqual({})
    const recoveredSuggestionGroups = Object.values(
      recovered.editorSuggestionGroups,
    )
    const recoveredSuggestions = Object.values(recovered.editorSuggestions)
    expect(recoveredSuggestionGroups).toHaveLength(1)
    expect(recoveredSuggestionGroups[0]).toMatchObject({
      documentId: recoveryId,
      origin: {
        commentId: recoveredComments[0].id,
        kind: 'inline_edit',
      },
    })
    expect(recoveredSuggestions).toHaveLength(1)
    expect(recoveredSuggestions[0]).toMatchObject({
      documentId: recoveryId,
      groupId: recoveredSuggestionGroups[0].id,
      origin: {
        commentId: recoveredComments[0].id,
        kind: 'inline_edit',
      },
    })
    expect(recoveredSuggestions[0]).not.toHaveProperty(
      'collaborationPublication',
    )
    expect(recoveredSuggestions[0]).not.toHaveProperty('privateDraft')
    expect(recovered.editorUi).toMatchObject({
      activeDocumentId: recoveryId,
      openDocumentIds: [recoveryId],
      selectedCommentId: recoveredComments[0].id,
    })
    expect(recovered.dirty).toBe(true)

    const promoted = researchDeskReducer(recovered, {
      documentId: recoveryId,
      type: 'promoteEditorRecoveryDocument',
    })

    expect(promoted.editorDocuments[recoveryId].recovery).toBeUndefined()
    expect(promoted.editorDocuments[recoveryId].revision).toBe(0)
    expect(promoted.editorCommentOutbox?.[recoveredComments[0].id]).toMatchObject({
      documentId: recoveryId,
      operation: 'upsert',
    })
  })

  it('sets a document body on load-on-open WITHOUT dirtying or bumping updatedAt', () => {
    const local = doc('ed_1', { updatedAt: '2026-01-01T00:00:00.000Z' })
    const next = researchDeskReducer(withDoc(local), {
      contentMarkdown: 'fetched body',
      documentId: 'ed_1',
      type: 'setServerEditorDocumentBody',
    })
    expect(next.editorDocuments.ed_1.contentMarkdown).toBe('fetched body')
    expect(next.editorDocuments.ed_1.updatedAt).toBe('2026-01-01T00:00:00.000Z')
    expect(next.dirty).toBe(false)
  })

  it('replaces a collaboration metadata stub with the exact projection detail', () => {
    const local = doc('ed_1', {
      contentMarkdown: '',
      contentMode: 'collaboration',
      updatedAt: '2026-01-01T00:00:00.000Z',
    })
    const detail = doc('ed_1', {
      collaboration: {
        generation: 1,
        persistedSequence: 8,
        projectionSequence: 8,
        projectionUpdatedAt: '2026-01-02T00:00:00.000Z',
        schemaVersion: 1,
      },
      contentMarkdown: '# Exact projection',
      contentMode: 'collaboration',
      updatedAt: '2026-01-02T00:00:00.000Z',
    })

    const next = researchDeskReducer(withDoc(local), {
      document: detail,
      type: 'setServerEditorDocumentDetail',
    })

    expect(next.editorDocuments.ed_1).toEqual(detail)
    expect(next.dirty).toBe(false)
  })

  it('authoritatively removes deleted comments while preserving explicit local work', () => {
    const base = withDoc(doc('ed_1'))
    const state: ProjectState = {
      ...base,
      editorComments: {
        'local-comment': comment('local-comment', 'ed_1'),
        'server-deleted': comment('server-deleted', 'ed_1'),
        'server-kept': comment('server-kept', 'ed_1'),
      },
    }
    const refreshed = {
      ...comment('server-kept', 'ed_1'),
      commentMarkdown: 'server version',
      updatedAt: '2026-01-02T00:00:00.000Z',
    }

    const next = researchDeskReducer(state, {
      comments: [refreshed],
      documentId: 'ed_1',
      preserveCommentIds: ['local-comment'],
      type: 'reconcileServerEditorComments',
    })

    expect(next.editorComments['local-comment']).toBe(state.editorComments['local-comment'])
    expect(next.editorComments['server-deleted']).toBeUndefined()
    expect(next.editorComments['server-kept']).toEqual(refreshed)
    expect(next.dirty).toBe(false)
  })

  it('materializes and clears a persisted private suggestion draft during server reconciliation', () => {
    const base = withDoc(doc('ed_1', { contentMode: 'collaboration' }))
    const privateComment: EditorCommentThreadRecord = {
      ...comment('private-comment', 'ed_1'),
      kind: 'inline_edit',
      suggestionDraft: {
        anchorVersion: 1,
        changeSummary: ['Clearer wording'],
        createdAt: '2026-01-01T00:00:01.000Z',
        groupId: 'private-group',
        patchId: '00000000-0000-4000-8000-000000000003',
        proposedText: 'replacement',
        publicationCommandId: '00000000-0000-4000-8000-000000000002',
        revision: 2,
        suggestionId: 'private-suggestion',
        updatedAt: '2026-01-01T00:00:02.000Z',
      },
    }

    const hydrated = researchDeskReducer(base, {
      comments: [privateComment],
      documentId: 'ed_1',
      preserveCommentIds: [],
      type: 'reconcileServerEditorComments',
    })

    expect(hydrated.editorSuggestionGroups['private-group']).toMatchObject({
      documentId: 'ed_1',
      origin: { commentId: 'private-comment', kind: 'inline_edit' },
    })
    expect(hydrated.editorSuggestions['private-suggestion']).toMatchObject({
      documentId: 'ed_1',
      privateDraft: {
        patchId: '00000000-0000-4000-8000-000000000003',
        publicationCommandId: '00000000-0000-4000-8000-000000000002',
        revision: 2,
      },
      proposedText: 'replacement',
      status: 'pending',
    })
    expect(hydrated.dirty).toBe(false)

    const accepted = researchDeskReducer(hydrated, {
      collaborationPublication: {
        commandId: privateComment.suggestionDraft!.publicationCommandId,
        patchId: privateComment.suggestionDraft!.patchId,
        sequence: 9,
        suggestionIds: ['shared-suggestion'],
      },
      suggestionId: 'private-suggestion',
      type: 'acceptEditorSuggestion',
    })
    const staleReplay = researchDeskReducer(accepted, {
      comments: [privateComment],
      documentId: 'ed_1',
      preserveCommentIds: [],
      type: 'reconcileServerEditorComments',
    })
    expect(staleReplay.editorSuggestions['private-suggestion'].status).toBe('accepted')
    expect(staleReplay.editorSuggestions['private-suggestion'].privateDraft).toBeUndefined()
    expect(staleReplay.editorComments['private-comment'].suggestionDraft).toBeUndefined()

    const cleared = researchDeskReducer(hydrated, {
      comments: [{ ...privateComment, suggestionDraft: undefined }],
      documentId: 'ed_1',
      preserveCommentIds: [],
      type: 'reconcileServerEditorComments',
    })

    expect(cleared.editorComments['private-comment'].suggestionDraft).toBeUndefined()
    expect(cleared.editorSuggestionGroups['private-group']).toBeUndefined()
    expect(cleared.editorSuggestions['private-suggestion']).toBeUndefined()
    expect(cleared.dirty).toBe(false)
  })

  it('adopts the server-confirmed relative anchor with a private suggestion draft', () => {
    const base = withDoc(doc('ed_1', { contentMode: 'collaboration' }))
    const privateComment: EditorCommentThreadRecord = {
      ...comment('private-comment', 'ed_1'),
      kind: 'inline_edit',
    }
    const relativeAnchor: EditorCommentThreadRecord['anchor'] = {
      ...privateComment.anchor,
      relativeFrom: 'relative-from',
      relativeTo: 'relative-to',
      relativeVersion: 'yjs-relative-position-base64-v1',
    }
    const state: ProjectState = {
      ...base,
      editorComments: { [privateComment.id]: privateComment },
    }

    const next = researchDeskReducer(state, {
      anchor: relativeAnchor,
      commentId: privateComment.id,
      suggestionDraft: {
        anchorVersion: 1,
        createdAt: '2026-01-01T00:00:01.000Z',
        groupId: 'private-group',
        patchId: '00000000-0000-4000-8000-000000000003',
        proposedText: 'replacement',
        publicationCommandId: '00000000-0000-4000-8000-000000000002',
        revision: 1,
        suggestionId: 'private-suggestion',
        updatedAt: '2026-01-01T00:00:02.000Z',
      },
      type: 'adoptEditorCommentSuggestionDraft',
    })

    expect(next.editorComments[privateComment.id].anchor).toEqual(relativeAnchor)
    expect(next.editorSuggestions['private-suggestion'].anchor).toEqual(relativeAnchor)
    expect(next.dirty).toBe(false)
  })

  it('merges server folders + comments WITHOUT dirtying', () => {
    const folder: EditorFolderRecord = {
      createdAt: '2026-01-01T00:00:00.000Z',
      id: 'edf_1',
      title: 'F',
      updatedAt: '2026-01-01T00:00:00.000Z',
    }
    const afterFolders = researchDeskReducer(createEmptyProjectState(), {
      folders: [folder],
      type: 'upsertServerEditorFolders',
    })
    expect(afterFolders.editorFolders.edf_1.title).toBe('F')
    expect(afterFolders.editorFolderOrder).toContain('edf_1')
    expect(afterFolders.dirty).toBe(false)

    const afterComments = researchDeskReducer(afterFolders, {
      comments: [comment('edc_1', 'ed_1')],
      type: 'upsertServerEditorComments',
    })
    expect(afterComments.editorComments.edc_1.documentId).toBe('ed_1')
    expect(afterComments.dirty).toBe(false)
  })
})

describe('recovery copy of a replaced collaboration generation', () => {
  it('keeps the captured state as a local recovery copy right before the document, which stays', () => {
    const original = doc('live-document', {
      collaboration: {
        generation: 3,
        persistedSequence: 2,
        projectionSequence: 2,
        projectionUpdatedAt: '2026-01-01T00:00:00.000Z',
        schemaVersion: 1,
      },
      contentMode: 'collaboration',
      serverSynced: true,
      title: 'Quarterly.md',
    })
    const other = doc('other-document')
    const state: ProjectState = {
      ...withDoc(original),
      editorDocumentOrder: [other.id, original.id],
      editorDocuments: { [original.id]: original, [other.id]: other },
    }

    const next = researchDeskReducer(state, {
      capture: {
        capturedAt: '2026-01-02T00:00:00.000Z',
        contentMarkdown: 'Refused local state',
        documentId: original.id,
      },
      reason: 'generation_replaced',
      source: { source: 'blank', title: 'Quarterly (old).md' },
      type: 'addEditorRecoveryDocument',
    })

    const recoveryId = next.editorDocumentOrder[1]!
    expect(next.editorDocumentOrder).toEqual([other.id, recoveryId, original.id])
    expect(next.editorDocuments[original.id]).toBe(original)
    expect(next.editorDocuments[recoveryId]).toMatchObject({
      contentMarkdown: 'Refused local state',
      folderId: null,
      recovery: {
        capturedAt: '2026-01-02T00:00:00.000Z',
        originalDocumentId: original.id,
        reason: 'generation_replaced',
      },
      revision: 0,
      title: 'Quarterly (old).md',
    })
    expect(next.editorDocuments[recoveryId]!.collaboration).toBeUndefined()
    expect(next.editorDocuments[recoveryId]!.serverSynced).toBeUndefined()
    expect(next.dirty).toBe(true)
  })

  it('keeps the recovery copy when the document left the list meanwhile', () => {
    const next = researchDeskReducer(createEmptyProjectState(), {
      capture: {
        capturedAt: '2026-01-02T00:00:00.000Z',
        contentMarkdown: 'Refused local state',
        documentId: 'gone-document',
      },
      reason: 'generation_replaced',
      source: { source: 'blank', title: 'Gone.md' },
      type: 'addEditorRecoveryDocument',
    })

    const [recoveryId] = next.editorDocumentOrder
    expect(next.editorDocuments[recoveryId!]).toMatchObject({
      contentMarkdown: 'Refused local state',
      recovery: { originalDocumentId: 'gone-document', reason: 'generation_replaced' },
      title: 'Gone.md',
    })
  })
})

describe('collaboration metadata CAS adoption', () => {
  function collaborationDocument(metadataRevision: number): EditorDocumentRecord {
    return doc('ed_1', {
      access: { mode: 'owner', permission: 'edit' },
      collaboration: {
        generation: 1,
        persistedSequence: 0,
        projectionSequence: 0,
        schemaVersion: 1,
      },
      contentMode: 'collaboration',
      metadataRevision,
    })
  }

  it('authoritatively enters collaboration despite a future local timestamp', () => {
    const legacy = doc('ed_1', {
      access: { mode: 'owner', permission: 'edit' },
      contentMarkdown: '# Writable legacy body',
      metadataRevision: 4,
      updatedAt: '2099-01-01T00:00:00.000Z',
    })

    const activated = researchDeskReducer(withDoc(legacy), {
      collaboration: {
        generation: 3,
        persistedSequence: 0,
        projectionSequence: 0,
        schemaVersion: 1,
      },
      documentId: legacy.id,
      metadataRevision: 5,
      type: 'activateEditorDocumentCollaboration',
    })

    expect(activated.editorDocuments.ed_1).toMatchObject({
      collaboration: { generation: 3 },
      contentMarkdown: '# Writable legacy body',
      contentMode: 'collaboration',
      metadataRevision: 5,
      updatedAt: '2099-01-01T00:00:00.000Z',
    })
    expect(planEditorDocumentAutosave(activated.editorDocuments.ed_1)).toMatchObject({
      kind: 'collaboration_metadata',
      payload: { expected_metadata_revision: 5 },
    })
    expect(activated.dirty).toBe(false)
  })

  it('advances activation, autosave, and anchor PATCH bases monotonically', () => {
    let state = withDoc(collaborationDocument(4))
    state = researchDeskReducer(state, {
      documentId: 'ed_1',
      metadataRevision: 5,
      type: 'adoptEditorDocumentMetadataRevision',
    })
    expect(planEditorDocumentAutosave(state.editorDocuments.ed_1)).toMatchObject({
      payload: { expected_metadata_revision: 5 },
    })

    state = researchDeskReducer(state, {
      documentId: 'ed_1',
      metadataRevision: 6,
      type: 'adoptEditorDocumentMetadataRevision',
    })
    expect(planEditorDocumentAutosave(state.editorDocuments.ed_1)).toMatchObject({
      payload: { expected_metadata_revision: 6 },
    })

    state = researchDeskReducer(state, {
      documentId: 'ed_1',
      metadataRevision: 7,
      type: 'adoptEditorDocumentMetadataRevision',
    })
    expect(state.editorDocuments.ed_1.metadataRevision).toBe(7)
  })

  it('keeps rename and anchor CAS on the newest response despite a late older adoption', () => {
    let state = withDoc(collaborationDocument(7))
    state = researchDeskReducer(state, {
      documentId: 'ed_1',
      title: 'Renamed',
      type: 'renameEditorDocument',
    })
    expect(planEditorDocumentAutosave(state.editorDocuments.ed_1)).toMatchObject({
      payload: { expected_metadata_revision: 7, title: 'Renamed.md' },
    })

    state = researchDeskReducer(state, {
      documentId: 'ed_1',
      metadataRevision: 9,
      type: 'adoptEditorDocumentMetadataRevision',
    })
    state = researchDeskReducer(state, {
      documentId: 'ed_1',
      metadataRevision: 8,
      type: 'adoptEditorDocumentMetadataRevision',
    })

    expect(state.editorDocuments.ed_1.metadataRevision).toBe(9)
    expect(planEditorDocumentAutosave(state.editorDocuments.ed_1)).toMatchObject({
      payload: { expected_metadata_revision: 9, title: 'Renamed.md' },
    })
  })
})

describe('private comment outbox', () => {
  it('tracks explicit local work and ignores acknowledgements for an older mutation', () => {
    const localComment = comment('local-comment', 'ed_1')
    let state = researchDeskReducer(withDoc(doc('ed_1')), {
      comment: localComment,
      type: 'createEditorComment',
    })
    expect(state.editorCommentOutbox?.[localComment.id]).toEqual({
      documentId: 'ed_1',
      operation: 'upsert',
      updatedAt: localComment.updatedAt,
    })

    state = researchDeskReducer(state, {
      acknowledgements: [{
        commentId: localComment.id,
        operation: 'upsert',
        updatedAt: '2025-12-31T23:59:59.000Z',
      }],
      type: 'acknowledgeEditorCommentOutbox',
    })
    expect(state.editorCommentOutbox?.[localComment.id]).toBeDefined()

    state = researchDeskReducer(state, {
      acknowledgements: [{
        commentId: localComment.id,
        operation: 'upsert',
        updatedAt: localComment.updatedAt,
      }],
      type: 'acknowledgeEditorCommentOutbox',
    })
    expect(state.editorCommentOutbox?.[localComment.id]).toBeUndefined()
  })

  it('drops stale cache after reset while preserving only explicit local mutations', () => {
    const staleComment = comment('stale-comment', 'ed_1')
    const localComment = {
      ...comment('local-comment', 'ed_1'),
      updatedAt: '2026-01-02T00:00:00.000Z',
    }
    const state = {
      ...withDoc(doc('ed_1')),
      editorCommentOutbox: {
        [localComment.id]: {
          documentId: 'ed_1',
          operation: 'upsert' as const,
          updatedAt: localComment.updatedAt,
        },
      },
      editorComments: {
        [localComment.id]: localComment,
        [staleComment.id]: staleComment,
      },
    }
    const plan = planEditorCommentReconciliation(
      'ed_1',
      state.editorComments,
      [],
      state.editorCommentOutbox,
    )
    const reconciled = researchDeskReducer(state, {
      comments: plan.serverComments,
      documentId: 'ed_1',
      preserveCommentIds: [...plan.preserveCommentIds],
      type: 'reconcileServerEditorComments',
    })

    expect(reconciled.editorCommentOutbox).toEqual(state.editorCommentOutbox)
    expect(reconciled.editorComments).toEqual({ [localComment.id]: localComment })
  })
})

describe('private collaboration suggestion publication', () => {
  it('stores durable shared command identifiers when acceptance completes', () => {
    const base = withDoc(doc('ed_1'))
    const suggestion: EditorSuggestionRecord = {
      anchor: { from: 0, quoteAfter: '', quoteBefore: '', selectedText: 'x', to: 1 },
      blockId: 'block-1',
      createdAt: '2026-01-01T00:00:00.000Z',
      documentId: 'ed_1',
      groupId: 'group-1',
      id: 'suggestion-1',
      originalText: 'x',
      origin: { kind: 'global_run' },
      proposedText: 'y',
      status: 'pending',
      updatedAt: '2026-01-01T00:00:00.000Z',
    }
    const state = {
      ...base,
      editorSuggestions: { [suggestion.id]: suggestion },
    }
    const collaborationPublication = {
      commandId: 'command-1',
      patchId: 'patch-1',
      sequence: 9,
      suggestionIds: ['shared-suggestion-1'],
    }

    const next = researchDeskReducer(state, {
      collaborationPublication,
      suggestionId: suggestion.id,
      type: 'acceptEditorSuggestion',
    })

    expect(next.editorSuggestions[suggestion.id]).toMatchObject({
      collaborationPublication,
      status: 'accepted',
    })
  })
})

describe('Anweisungslauf: welche Vorschlaege ein neuer Lauf ablöst', () => {
  // Ein neuer Anweisungslauf loest die Vorschlaege des VORIGEN ab. Die Regel
  // schluesselte einmal auf `origin.kind === 'global_run'` -- und traf damit
  // auch Vorschlaege, die zu einer Sammel-Notiz des Nutzers gehoeren, denn
  // die tragen dieselbe Kennung. Ergebnis waere gewesen: der Nutzer laesst
  // sich eine Notiz beantworten, tippt danach eine Anweisung, und seine
  // offene Notiz-Antwort ist stillschweigend weg.
  function vorschlag(
    id: string,
    origin: EditorSuggestionRecord['origin'],
  ): EditorSuggestionRecord {
    return {
      anchor: { from: 0, to: 1, selectedText: 'x', quoteBefore: '', quoteAfter: '' },
      blockId: '',
      createdAt: '2026-01-01T00:00:00.000Z',
      documentId: 'ed_1',
      groupId: `group-${id}`,
      id,
      originalMarkdown: 'alt',
      originalText: 'alt',
      origin,
      proposedText: 'neu',
      revision: 1,
      status: 'pending',
      updatedAt: '2026-01-01T00:00:00.000Z',
    }
  }

  function nachNeuemLauf(vorhandene: EditorSuggestionRecord[]) {
    const basis: ProjectState = {
      ...withDoc(doc('ed_1')),
      editorSuggestions: Object.fromEntries(vorhandene.map((s) => [s.id, s])),
    }
    return researchDeskReducer(basis, {
      group: {
        createdAt: '2026-01-02T00:00:00.000Z',
        documentId: 'ed_1',
        id: 'group-neu',
        origin: { kind: 'global_run' },
      },
      suggestions: [vorschlag('neu-1', { commentId: 'c-neu', kind: 'assistant_edit' })],
      type: 'createEditorSuggestionGroup',
    })
  }

  it('laesst die Vorschlaege zu Notizen des Nutzers unberuehrt', () => {
    const ausNotiz = vorschlag('aus-notiz', { commentId: 'c-notiz', kind: 'global_run' })

    const next = nachNeuemLauf([ausNotiz])

    expect(next.editorSuggestions['aus-notiz'].status).toBe('pending')
  })

  it('loest den vorigen Anweisungslauf ab -- im Kollaborationsmodus', () => {
    const vorigerLauf = vorschlag('vorher', { commentId: 'c-traeger', kind: 'assistant_edit' })

    const next = nachNeuemLauf([vorigerLauf])

    expect(next.editorSuggestions.vorher.status).toBe('rejected')
  })

  it('loest den vorigen Anweisungslauf ab -- im Markdown-Modus', () => {
    const vorigerLauf = vorschlag('vorher-md', { kind: 'global_run' })

    const next = nachNeuemLauf([vorigerLauf])

    expect(next.editorSuggestions['vorher-md'].status).toBe('rejected')
  })
})

describe('rejecting a whole group of suggestions', () => {
  // "Reject all" acts on every card of the group: on an open suggestion and
  // on one whose passage changed alike, as each card's own reject button does.
  function suggestion(id: string, status: EditorSuggestionRecord['status']): EditorSuggestionRecord {
    return {
      anchor: { from: 0, to: 1, selectedText: 'x', quoteBefore: '', quoteAfter: '' },
      blockId: '',
      createdAt: '2026-01-01T00:00:00.000Z',
      documentId: 'ed_1',
      groupId: 'group-1',
      id,
      originalText: 'x',
      origin: { kind: 'global_run' },
      proposedText: 'y',
      status,
      updatedAt: '2026-01-01T00:00:00.000Z',
    }
  }

  it('rejects the open and the stale suggestions of the group', () => {
    const state: ProjectState = {
      ...withDoc(doc('ed_1')),
      editorSuggestions: {
        open: suggestion('open', 'pending'),
        stale: suggestion('stale', 'stale'),
      },
    }

    const next = researchDeskReducer(state, { groupId: 'group-1', type: 'rejectEditorSuggestionGroup' })

    expect(next.editorSuggestions.open.status).toBe('rejected')
    expect(next.editorSuggestions.stale.status).toBe('rejected')
  })
})
