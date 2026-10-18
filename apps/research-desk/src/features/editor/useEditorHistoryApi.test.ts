import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

import type {
  EditorCommentThreadRecord,
  EditorDocumentRecord,
} from '@/features/project/types'
import {
  canPersistEditorCommentsForDocument,
  editorCommentsForAutosave,
  editorDocumentDetailProvenanceKey,
  editorLocallyAuthoritativeDocumentIds,
  editorServerDocumentObservation,
  editorDocumentsForAutosave,
  planEditorCommentReconciliation,
  planEditorDocumentAutosave,
  planEditorOpenHydration,
  resolveEditorDocumentBodyHydration,
  shouldLoadLegacyEditorBody,
} from './useEditorHistoryApi'

const BASE_DOCUMENT: EditorDocumentRecord = {
  contentMarkdown: '# Draft',
  createdAt: '2026-07-15T08:00:00.000Z',
  folderId: null,
  id: 'document-1',
  revision: 3,
  source: 'blank',
  title: 'Draft',
  updatedAt: '2026-07-15T08:01:00.000Z',
}

function comment(
  id: string,
  updatedAt = '2026-07-15T08:01:00.000Z',
): EditorCommentThreadRecord {
  return {
    anchor: {
      from: 1,
      quoteAfter: '',
      quoteBefore: '',
      selectedText: 'Draft',
      to: 6,
    },
    commentMarkdown: id,
    createdAt: BASE_DOCUMENT.createdAt,
    documentId: BASE_DOCUMENT.id,
    id,
    kind: 'collect',
    status: 'open',
    updatedAt,
  }
}

describe('editor document autosave planning', () => {
  it('keeps legacy documents on the existing full-body lifecycle', () => {
    expect(planEditorDocumentAutosave(BASE_DOCUMENT)).toEqual({
      kind: 'legacy_body',
    })
  })

  it('limits owner collaboration autosave to metadata with its current CAS revision', () => {
    const plan = planEditorDocumentAutosave(
      {
        ...BASE_DOCUMENT,
        access: { mode: 'owner', permission: 'edit' },
        contentMode: 'collaboration',
        metadataRevision: 4,
      },
      7,
    )

    expect(plan).toEqual({
      kind: 'collaboration_metadata',
      payload: {
        expected_metadata_revision: 7,
        folder_id: null,
        title: 'Draft',
      },
    })
    expect(JSON.stringify(plan)).not.toContain('contentMarkdown')
    expect(JSON.stringify(plan)).not.toContain('# Draft')
  })

  it('seeds the activation metadata revision and synced fingerprint without claiming exact detail', () => {
    const activated = {
      ...BASE_DOCUMENT,
      access: { mode: 'owner' as const, permission: 'edit' as const },
      collaboration: {
        generation: 1,
        persistedSequence: 0,
        projectionSequence: 0,
        schemaVersion: 1,
      },
      contentMode: 'collaboration' as const,
      metadataRevision: 5,
    }

    expect(editorServerDocumentObservation(activated, 'metadata')).toEqual({
      exactDetailProvenanceKey: null,
      metadataRevision: 5,
      syncedFingerprint: activated.updatedAt,
    })
  })

  it('never persists collaboration metadata for shared access', () => {
    expect(planEditorDocumentAutosave({
      ...BASE_DOCUMENT,
      access: { mode: 'shared', permission: 'edit' },
      contentMode: 'collaboration',
      metadataRevision: 2,
    })).toEqual({ kind: 'none' })
  })

  it('suppresses shared document writes while persisting authorized private collaboration comments', () => {
    const ownerDocument = {
      ...BASE_DOCUMENT,
      id: 'owner-document',
    }
    const locallyMaskedSharedDocument = {
      ...BASE_DOCUMENT,
      id: 'shared-document',
    }
    const serverDocuments = new Map<string, EditorDocumentRecord>([[
      'shared-document',
      {
        ...locallyMaskedSharedDocument,
        access: { mode: 'shared', permission: 'edit' },
        contentMode: 'collaboration',
      },
    ]])
    const documents = {
      [ownerDocument.id]: ownerDocument,
      [locallyMaskedSharedDocument.id]: locallyMaskedSharedDocument,
    }
    const comments = {
      'owner-comment': {
        anchor: {
          from: 1,
          quoteAfter: '',
          quoteBefore: '# ',
          selectedText: 'Draft',
          to: 6,
        },
        commentMarkdown: 'Owner note',
        createdAt: BASE_DOCUMENT.createdAt,
        documentId: ownerDocument.id,
        id: 'owner-comment',
        kind: 'collect' as const,
        status: 'open' as const,
        updatedAt: BASE_DOCUMENT.updatedAt,
      },
      'shared-comment': {
        anchor: {
          from: 1,
          quoteAfter: '',
          quoteBefore: '# ',
          selectedText: 'Draft',
          to: 6,
        },
        commentMarkdown: 'Shared note',
        createdAt: BASE_DOCUMENT.createdAt,
        documentId: locallyMaskedSharedDocument.id,
        id: 'shared-comment',
        kind: 'collect' as const,
        status: 'open' as const,
        updatedAt: BASE_DOCUMENT.updatedAt,
      },
    }

    expect(Object.keys(editorDocumentsForAutosave(documents, serverDocuments))).toEqual([
      'owner-document',
    ])
    expect(Object.keys(editorCommentsForAutosave(
      comments,
      documents,
      serverDocuments,
    ))).toEqual(['owner-comment', 'shared-comment'])
  })

  it('keeps recovery copies and synchronously retired ids outside every autosave collection', () => {
    const ownerDocument = {
      ...BASE_DOCUMENT,
      id: 'owner-document',
    }
    const recoveryDocument = {
      ...BASE_DOCUMENT,
      id: 'recovery-document',
      recovery: {
        capturedAt: '2026-07-15T08:02:00.000Z',
        originalDocumentId: 'retired-document',
        reason: 'remote_deleted' as const,
      },
      revision: 0,
    }
    const retiredDocument = {
      ...BASE_DOCUMENT,
      id: 'retired-document',
      serverSynced: true,
    }
    const documents = {
      [ownerDocument.id]: ownerDocument,
      [recoveryDocument.id]: recoveryDocument,
      [retiredDocument.id]: retiredDocument,
    }
    const comments = Object.fromEntries(
      Object.values(documents).map((document) => {
        const record = {
          ...comment(`comment-${document.id}`),
          documentId: document.id,
        }
        return [record.id, record]
      }),
    )
    const retiredIds = new Set([retiredDocument.id])

    expect(Object.keys(editorDocumentsForAutosave(
      documents,
      undefined,
      retiredIds,
    ))).toEqual([ownerDocument.id])
    expect(Object.values(editorCommentsForAutosave(
      comments,
      documents,
      undefined,
      retiredIds,
    )).map((item) => item.documentId)).toEqual([ownerDocument.id])
  })

  it('keeps view-only and legacy shared comments out of the write collection', () => {
    expect(canPersistEditorCommentsForDocument({
      ...BASE_DOCUMENT,
      access: { mode: 'shared', permission: 'suggest' },
      contentMode: 'collaboration',
    })).toBe(true)
    expect(canPersistEditorCommentsForDocument({
      ...BASE_DOCUMENT,
      access: { mode: 'shared', permission: 'view' },
      contentMode: 'collaboration',
    })).toBe(false)
    expect(canPersistEditorCommentsForDocument({
      ...BASE_DOCUMENT,
      access: { mode: 'shared', permission: 'edit' },
      contentMode: 'markdown',
    })).toBe(false)
  })

  it('honors hydrated collaboration mode when a local-newer record masked it', () => {
    const serverDocument: EditorDocumentRecord = {
      ...BASE_DOCUMENT,
      access: { mode: 'owner', permission: 'edit' },
      contentMarkdown: '',
      contentMode: 'collaboration',
      metadataRevision: 5,
    }

    expect(planEditorDocumentAutosave(BASE_DOCUMENT, 5, serverDocument)).toEqual({
      kind: 'collaboration_metadata',
      payload: {
        expected_metadata_revision: 5,
        folder_id: null,
        title: 'Draft',
      },
    })
  })

  it('loads markdown bodies only for the legacy editor lifecycle', () => {
    expect(shouldLoadLegacyEditorBody(BASE_DOCUMENT)).toBe(true)
    expect(shouldLoadLegacyEditorBody({
      ...BASE_DOCUMENT,
      contentMode: 'collaboration',
    })).toBe(false)
  })

  it('loads private comments after an exact shared document detail was registered', () => {
    expect(planEditorOpenHydration({
      collaborationDocument: true,
      hasCommentSnapshot: false,
      hasExactDocumentDetail: true,
      hasLocalDocumentBody: true,
    })).toEqual({
      loadComments: true,
      loadDocumentDetail: false,
    })
  })

  it('does not treat a cached collaboration body as exact lifecycle detail', () => {
    expect(planEditorOpenHydration({
      collaborationDocument: true,
      hasCommentSnapshot: false,
      hasExactDocumentDetail: false,
      hasLocalDocumentBody: true,
    })).toEqual({
      loadComments: true,
      loadDocumentDetail: true,
    })
  })

  it('requires collaboration detail again after reset or a failed detail attempt', () => {
    const afterExactGet = planEditorOpenHydration({
      collaborationDocument: true,
      hasCommentSnapshot: true,
      hasExactDocumentDetail: true,
      hasLocalDocumentBody: true,
    })
    const afterLifecycleReset = planEditorOpenHydration({
      collaborationDocument: true,
      hasCommentSnapshot: false,
      hasExactDocumentDetail: false,
      hasLocalDocumentBody: true,
    })
    const afterDetailOutage = planEditorOpenHydration({
      collaborationDocument: true,
      hasCommentSnapshot: false,
      hasExactDocumentDetail: false,
      hasLocalDocumentBody: true,
    })

    expect(afterExactGet.loadDocumentDetail).toBe(false)
    expect(afterLifecycleReset.loadDocumentDetail).toBe(true)
    expect(afterDetailOutage.loadDocumentDetail).toBe(true)
  })

  it('invalidates exact detail provenance when the collaboration projection lifecycle advances', () => {
    const exactDetail = {
      ...BASE_DOCUMENT,
      collaboration: {
        generation: 2,
        persistedSequence: 8,
        projectionSequence: 8,
        schemaVersion: 1,
      },
      contentMode: 'collaboration' as const,
    }
    const newerMetadata = {
      ...exactDetail,
      collaboration: {
        ...exactDetail.collaboration,
        persistedSequence: 11,
        projectionSequence: 11,
      },
    }

    expect(editorDocumentDetailProvenanceKey(exactDetail))
      .not.toBe(editorDocumentDetailProvenanceKey(newerMetadata))
    expect(planEditorOpenHydration({
      collaborationDocument: true,
      hasCommentSnapshot: true,
      hasExactDocumentDetail: editorDocumentDetailProvenanceKey(exactDetail)
        === editorDocumentDetailProvenanceKey(newerMetadata),
      hasLocalDocumentBody: true,
    }).loadDocumentDetail).toBe(true)
  })
})

describe('editor document body readiness', () => {
  it('does not promote metadata-only rows to loaded bodies on background refresh', () => {
    expect([...editorLocallyAuthoritativeDocumentIds({
      alreadyHydrated: false,
      loadedDocumentIds: [],
      projectDocumentIds: ['local-body', 'metadata-row'],
    })]).toEqual(['local-body', 'metadata-row'])
    expect([...editorLocallyAuthoritativeDocumentIds({
      alreadyHydrated: true,
      loadedDocumentIds: ['local-body'],
      projectDocumentIds: ['local-body', 'metadata-row'],
    })]).toEqual(['local-body'])
  })

  it('renders local and already hydrated server bodies without a pending phase', () => {
    expect(resolveEditorDocumentBodyHydration({
      documentId: 'local-document',
      error: null,
      hasExactDocumentDetail: false,
      hasLoadedDocumentBody: false,
      hasServerDocument: false,
      requiresExactDocumentDetail: false,
    })).toEqual({
      documentId: 'local-document',
      error: null,
      phase: 'ready',
    })
    expect(resolveEditorDocumentBodyHydration({
      documentId: 'server-document',
      error: null,
      hasExactDocumentDetail: false,
      hasLoadedDocumentBody: true,
      hasServerDocument: true,
      requiresExactDocumentDetail: false,
    }).phase).toBe('ready')
  })

  it('keeps metadata-only server documents pending until their body is authoritative', () => {
    expect(resolveEditorDocumentBodyHydration({
      documentId: 'server-document',
      error: null,
      hasExactDocumentDetail: false,
      hasLoadedDocumentBody: false,
      hasServerDocument: true,
      requiresExactDocumentDetail: false,
    })).toEqual({
      documentId: 'server-document',
      error: null,
      phase: 'pending',
    })
  })

  it('requires exact lifecycle detail for collaboration documents', () => {
    const shared = {
      documentId: 'collaboration-document',
      error: null,
      hasLoadedDocumentBody: true,
      hasServerDocument: true,
      requiresExactDocumentDetail: true,
    }
    expect(resolveEditorDocumentBodyHydration({
      ...shared,
      hasExactDocumentDetail: false,
    }).phase).toBe('pending')
    expect(resolveEditorDocumentBodyHydration({
      ...shared,
      hasExactDocumentDetail: true,
    }).phase).toBe('ready')
  })

  it('makes a terminal body failure visible without leaving a pending skeleton', () => {
    expect(resolveEditorDocumentBodyHydration({
      documentId: 'server-document',
      error: 'Document body unavailable',
      hasExactDocumentDetail: false,
      hasLoadedDocumentBody: false,
      hasServerDocument: true,
      requiresExactDocumentDetail: false,
    })).toEqual({
      documentId: 'server-document',
      error: 'Document body unavailable',
      phase: 'error',
    })
  })

  it('uses an immediate empty phase when no document is selected', () => {
    expect(resolveEditorDocumentBodyHydration({
      documentId: null,
      error: 'ignored',
      hasExactDocumentDetail: false,
      hasLoadedDocumentBody: false,
      hasServerDocument: false,
      requiresExactDocumentDetail: false,
    })).toEqual({ documentId: null, error: null, phase: 'empty' })
  })
})

describe('authoritative editor comment reconciliation', () => {
  it('drops a stale cached private comment after reset and an authoritative empty list', () => {
    const local = comment('comment-1')
    const plan = planEditorCommentReconciliation(
      BASE_DOCUMENT.id,
      { [local.id]: local },
      [],
      {},
    )

    expect([...plan.preserveCommentIds]).toEqual([])
    expect(plan.serverComments).toEqual([])
  })

  it('preserves only comment ids explicitly present in the local outbox', () => {
    const staleCached = comment('stale-cached')
    const changedLocally = comment('changed-comment', '2026-07-15T08:03:00.000Z')
    const plan = planEditorCommentReconciliation(
      BASE_DOCUMENT.id,
      {
        [changedLocally.id]: changedLocally,
        [staleCached.id]: staleCached,
      },
      [comment('server-comment')],
      {
        [changedLocally.id]: {
          documentId: changedLocally.documentId,
          operation: 'upsert',
          updatedAt: changedLocally.updatedAt,
        },
      },
    )

    expect([...plan.preserveCommentIds]).toEqual(['changed-comment'])
    expect(plan.serverComments.map((item) => item.id)).toEqual(['server-comment'])
  })

  it('keeps a pending local deletion from being resurrected by a racing load', () => {
    const deleted = comment('deleted-comment')
    const plan = planEditorCommentReconciliation(
      BASE_DOCUMENT.id,
      {},
      [deleted],
      {
        [deleted.id]: {
          documentId: deleted.documentId,
          operation: 'delete',
        },
      },
    )

    expect([...plan.pendingDeletedCommentIds]).toEqual(['deleted-comment'])
    expect(plan.serverComments).toEqual([])
  })
})

describe('the share flush locks before it reads (source pins)', () => {
  const source = readFileSync(
    fileURLToPath(new URL('./useEditorHistoryApi.ts', import.meta.url)),
    'utf8',
  )
  const body = source.slice(source.indexOf('const flushDocumentForShare'))
    .slice(0, 2400)

  it('takes only a documentId — never a caller-frozen record', () => {
    expect(body).toContain('documentId: string')
    expect(body).not.toContain('document: EditorDocumentRecord')
  })

  it('waits for the in-flight flush, acquires the lock, THEN reads live', () => {
    const waits = body.indexOf('while (flushingRef.current)')
    const locks = body.indexOf('flushingRef.current = true')
    const reads = body.indexOf('documentsRef.current[documentId]')
    expect(waits).toBeGreaterThan(-1)
    expect(locks).toBeGreaterThan(waits)
    expect(reads).toBeGreaterThan(locks)
  })
})

describe('rebased-then-retry reaches saved (pure composition)', () => {
  // The pre-fix loop: a frozen snapshot re-sent revision N+1 forever.
  // The fix re-reads the live record — after the concurrent writer won
  // and the 409 path rebased the base, a retry MUST compute a payload
  // the server CAS accepts (current + 1).
  it('a snapshot payload stays stale; a live-read payload advances', async () => {
    const { researchDeskReducer } = await import('@/features/researchDesk/state')
    const { serverDocumentPayload } = await import('./editorSync')
    const { createEmptyProjectState } = await import('@/features/project/seedProject')

    const snapshot = { ...BASE_DOCUMENT, revision: 3 }
    let state = {
      ...createEmptyProjectState(),
      editorDocumentOrder: [snapshot.id],
      editorDocuments: { [snapshot.id]: snapshot },
    }
    // Concurrent autosave wins: server (and live record) move to 4.
    state = researchDeskReducer(state, {
      documentId: snapshot.id,
      revision: 4,
      type: 'adoptEditorDocumentRevision',
    })
    // The frozen snapshot would re-send base+1 = 4 against a server at 4
    // (base 4 requires payload 5) — exactly the never-succeeding retry.
    expect(serverDocumentPayload(snapshot).revision).toBe(4)
    // The 409 path rebases the LIVE record onto the server base ...
    state = researchDeskReducer(state, {
      contentMarkdown: '# Server-Stand',
      documentId: snapshot.id,
      pushedContentMarkdown: snapshot.contentMarkdown,
      revision: 4,
      type: 'rebaseServerEditorDocument',
    })
    // ... and a live re-read now computes the accepted payload.
    const live = state.editorDocuments[snapshot.id]
    expect(live.revision).toBe(4)
    expect(serverDocumentPayload(live).revision).toBe(5)
  })
})

describe('activation callbacks receive the LIVE record', () => {
  it('EditorWorkspace re-reads editorDocumentsRef before spreading', () => {
    const source = readFileSync(
      fileURLToPath(new URL('./EditorWorkspace.tsx', import.meta.url)),
      'utf8',
    )
    const handler = source.slice(
      source.indexOf('const handleEnableCollaboration'),
    ).slice(0, 4200)
    const liveRead = handler.indexOf(
      'editorDocumentsRef.current[document.id]',
    )
    const spread = handler.indexOf('...liveDocument')
    expect(liveRead).toBeGreaterThan(-1)
    expect(spread).toBeGreaterThan(liveRead)
    expect(handler).not.toContain('...document,')
  })
})
