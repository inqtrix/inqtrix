import { describe, expect, it } from 'vitest'

import type {
  ServerEditorComment,
  ServerEditorDocument,
  ServerEditorFolder,
} from '@/api/inqtrixClient'
import {
  commentRecordFromServer,
  documentRecordFromServer,
  editorEntitiesForServerImport,
  folderRecordFromServer,
  isCollaborationDocument,
  serverCommentPayload,
  serverDocumentPayload,
  serverFolderPayload,
} from './editorSync'

describe('editorSync converters', () => {
  it('round-trips a document with body, diff anchor, and source run', () => {
    const server: ServerEditorDocument = {
      id: 'ed_1',
      title: 'My doc',
      content_markdown: '# Heavy body',
      folder_id: 'edf_1',
      source: 'imported-research-report',
      source_run_id: 'run_1',
      revision: 7,
      diff_anchor_markdown: '# old',
      diff_anchor_updated_at: 1_700_000_000,
      created_at: 1_699_000_000,
      updated_at: 1_700_000_500,
    }
    const record = documentRecordFromServer(server)
    expect(record.contentMarkdown).toBe('# Heavy body')
    expect(record.folderId).toBe('edf_1')
    expect(record.source).toBe('imported-research-report')
    expect(record.sourceRunId).toBe('run_1')
    expect(record.revision).toBe(7)
    expect(record.diffAnchorMarkdown).toBe('# old')

    const payload = serverDocumentPayload(record)
    expect(payload.content_markdown).toBe('# Heavy body')
    expect(payload.folder_id).toBe('edf_1')
    expect(payload.source).toBe('imported-research-report')
    expect(payload.source_run_id).toBe('run_1')
    // The record's revision is the last-synced server base (7); the save
    // creates base+1 (8), which the store CAS accepts only while stored == 7.
    expect(payload.revision).toBe(8)
    expect(payload.created_at).toBe(server.created_at)
    expect(payload.updated_at).toBe(server.updated_at)
    expect(payload.diff_anchor_updated_at).toBe(server.diff_anchor_updated_at)
  })

  it('treats a list row (no content_markdown) as an empty body', () => {
    const record = documentRecordFromServer({
      id: 'ed_2',
      title: 'Listed',
      folder_id: null,
      source: 'blank',
      source_run_id: null,
      revision: 1,
      diff_anchor_markdown: null,
      diff_anchor_updated_at: null,
      created_at: 1,
      updated_at: 1,
    })
    expect(record.contentMarkdown).toBe('')
    expect(record.contentMode).toBe('markdown')
    expect(record.metadataRevision).toBe(1)
    expect(record.serverSynced).toBe(true)
    expect(record.access).toEqual({ mode: 'owner', permission: 'edit' })
    expect('sourceRunId' in record).toBe(false)
    expect('diffAnchorMarkdown' in record).toBe(false)
    expect(serverDocumentPayload(record).source_run_id).toBe(null)
  })

  it('maps collaboration access and durable projection metadata', () => {
    const record = documentRecordFromServer({
      access: {
        mode: 'shared',
        owner: { id: 'user-owner', name: 'Olga Owner' },
        permission: 'suggest',
      },
      collaboration: {
        generation: 2,
        persisted_sequence: 19,
        projection_sequence: 17,
        projection_updated_at: 1_700_000_000,
        schema_version: 1,
      },
      content_mode: 'collaboration',
      created_at: 1,
      diff_anchor_markdown: null,
      diff_anchor_updated_at: null,
      folder_id: null,
      id: 'ed_shared',
      metadata_revision: 4,
      revision: 8,
      source: 'blank',
      source_run_id: null,
      title: 'Shared draft',
      updated_at: 2,
    })

    expect(isCollaborationDocument(record)).toBe(true)
    expect(record.access).toEqual({
      mode: 'shared',
      owner: { id: 'user-owner', name: 'Olga Owner' },
      permission: 'suggest',
    })
    expect(record.metadataRevision).toBe(4)
    expect(record.collaboration).toEqual({
      commentRevision: 0,
      generation: 2,
      persistedSequence: 19,
      projectionSequence: 17,
      projectionUpdatedAt: '2023-11-14T22:13:20.000Z',
      schemaVersion: 1,
    })
  })

  it('normalizes an unknown document source', () => {
    const record = documentRecordFromServer({
      id: 'ed_3', title: 'X', folder_id: null, source: 'nope',
      source_run_id: null, revision: 1, diff_anchor_markdown: null,
      diff_anchor_updated_at: null, created_at: 1, updated_at: 1,
    })
    expect(record.source).toBe('blank')
  })

  it('keeps recovery and shared projections outside an explicit server import', () => {
    const owned = documentRecordFromServer({
      created_at: 1,
      diff_anchor_markdown: null,
      diff_anchor_updated_at: null,
      folder_id: null,
      id: 'owned',
      revision: 1,
      source: 'blank',
      source_run_id: null,
      title: 'Owned',
      updated_at: 1,
    })
    const recovery = {
      ...owned,
      access: undefined,
      id: 'recovery',
      recovery: {
        capturedAt: '2026-01-01T00:00:00.000Z',
        originalDocumentId: 'deleted',
        reason: 'remote_deleted' as const,
      },
      revision: 0,
      serverSynced: undefined,
    }
    const shared = {
      ...owned,
      access: { mode: 'shared' as const, permission: 'view' as const },
      id: 'shared',
    }
    const comments = Object.fromEntries(
      [owned, recovery, shared].map((document) => {
        const record = commentRecordFromServer({
          anchor: { from: 0, selectedText: 'x', to: 1 },
          comment_markdown: document.id,
          created_at: 1,
          document_id: document.id,
          evidence_preset: null,
          id: `comment-${document.id}`,
          kind: 'collect',
          status: 'open',
          updated_at: 1,
        })
        return [record.id, record]
      }),
    )

    const projection = editorEntitiesForServerImport({
      comments,
      documents: {
        [owned.id]: owned,
        [recovery.id]: recovery,
        [shared.id]: shared,
      },
    })

    expect(Object.keys(projection.documents)).toEqual([owned.id])
    expect(Object.values(projection.comments).map((item) => item.documentId))
      .toEqual([owned.id])
  })

  it('round-trips a comment with its verbatim anchor', () => {
    const server: ServerEditorComment = {
      id: 'edc_1',
      document_id: 'ed_1',
      comment_markdown: 'check this',
      anchor: { from: 3, to: 9, selectedText: 'hello', quoteBefore: 'a', quoteAfter: 'b' },
      kind: 'evidence_review',
      status: 'open',
      evidence_preset: 'fact_check',
      created_at: 10,
      updated_at: 20,
    }
    const record = commentRecordFromServer(server)
    expect(record.anchor).toEqual(server.anchor)
    // The legacy kind survives the round trip — rows are not migrated.
    expect(record.kind).toBe('evidence_review')

    const payload = serverCommentPayload(record)
    expect(payload.anchor).toEqual(server.anchor)
    // Evidence presets never reach a model. The column stays, and the
    // client does not write it: a stored preset is not re-sent, so it
    // cannot come back through an autosave.
    expect(payload.evidence_preset).toBeNull()
    expect(payload.created_at).toBe(10)
    expect(payload.updated_at).toBe(20)
  })

  it('hydrates a creator-private suggestion draft without sending it through comment autosave', () => {
    const record = commentRecordFromServer({
      anchor: { from: 3, quoteAfter: 'b', quoteBefore: 'a', selectedText: 'old', to: 6 },
      comment_markdown: 'Rewrite this',
      created_at: 10,
      document_id: 'ed_1',
      evidence_preset: null,
      id: 'edc_private',
      kind: 'inline_edit',
      status: 'open',
      suggestion_draft: {
        anchor_version: 1,
        change_summary: ['Clearer wording'],
        created_at: 11,
        evidence: null,
        group_id: 'editor-suggestion-group-private',
        patch_id: '00000000-0000-4000-8000-000000000003',
        proposed_text: 'new',
        publication_command_id: '00000000-0000-4000-8000-000000000002',
        revision: 2,
        revision_history: [{
          change_summary: [],
          created_at: 12,
          instruction: 'Shorter',
          proposed_text: 'newer',
          source: 'llm_refine',
          warnings: [],
        }],
        suggestion_id: 'editor-suggestion-private',
        updated_at: 13,
        warnings: ['Review terminology'],
      },
      updated_at: 10,
    })

    expect(record.suggestionDraft).toEqual({
      anchorVersion: 1,
      changeSummary: ['Clearer wording'],
      createdAt: '1970-01-01T00:00:11.000Z',
      groupId: 'editor-suggestion-group-private',
      patchId: '00000000-0000-4000-8000-000000000003',
      proposedText: 'new',
      publicationCommandId: '00000000-0000-4000-8000-000000000002',
      revision: 2,
      revisionHistory: [{
        changeSummary: [],
        createdAt: '1970-01-01T00:00:12.000Z',
        instruction: 'Shorter',
        proposedText: 'newer',
        source: 'llm_refine',
        warnings: [],
      }],
      suggestionId: 'editor-suggestion-private',
      updatedAt: '1970-01-01T00:00:13.000Z',
      warnings: ['Review terminology'],
    })
    expect(serverCommentPayload(record)).not.toHaveProperty('suggestion_draft')
  })

  it('omits an absent evidence preset on a comment', () => {
    const record = commentRecordFromServer({
      id: 'edc_2', document_id: 'ed_1', comment_markdown: 'x',
      anchor: { from: 0, to: 1, selectedText: 'h' }, kind: 'collect',
      status: 'open', evidence_preset: null, created_at: 1, updated_at: 1,
    })
    expect('evidencePreset' in record).toBe(false)
    expect(serverCommentPayload(record).evidence_preset).toBe(null)
  })

  it('round-trips a folder', () => {
    const server: ServerEditorFolder = {
      id: 'edf_1', title: 'F', created_at: 100, updated_at: 200,
    }
    const record = folderRecordFromServer(server)
    const payload = serverFolderPayload(record)
    expect(payload.created_at).toBe(100)
    expect(payload.updated_at).toBe(200)
    expect(payload.title).toBe('F')
  })
})
