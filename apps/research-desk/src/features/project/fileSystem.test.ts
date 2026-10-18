import { describe, expect, it } from 'vitest'
import { loadProject, resolvePinnedExplorerFromManifest, resolveVectorIndexesFromManifest } from './fileSystem'
import { serializeEditorDocument, serializeProjectManifest } from './markdown'
import { createEmptyProjectState } from './seedProject'
import type {
  EditorCommentThreadRecord,
  EditorDocumentRecord,
  FileAssetRecord,
} from './types'

function makeAsset(id: string): FileAssetRecord {
  return {
    createdAt: '2026-01-01T00:00:00.000Z',
    extractedText: `${id} content`,
    fileName: `${id}.txt`,
    groupId: null,
    id,
    label: id,
    mimeType: 'text/plain',
    origin: 'library',
    pageCount: null,
    parseStatus: 'parsed',
    parseWarning: null,
    sectionId: 'file-section-temp',
    sizeBytes: 12,
    textTruncated: false,
    title: `${id}.txt`,
    updatedAt: '2026-01-01T00:00:00.000Z',
  }
}

describe('resolvePinnedExplorerFromManifest', () => {
  it('filters duplicate and stale pinned ids against known project ids', () => {
    const result = resolvePinnedExplorerFromManifest({
      chatThreadIds: ['ct-1', 'ct-1', 'missing'],
      editorDocumentIds: ['doc-1', 42, 'missing'],
      knowledgeSessionIds: ['ks-1', 'missing'],
    }, {
      chatThreadIds: ['ct-1'],
      editorDocumentIds: ['doc-1'],
      knowledgeSessionIds: ['ks-1'],
    })

    expect(result).toEqual({
      chatThreadIds: ['ct-1'],
      editorDocumentIds: ['doc-1'],
      knowledgeSessionIds: ['ks-1'],
      agentSessionIds: [],
    })
  })

  it('keeps knowledge pins when no valid server-session list is available yet', () => {
    const result = resolvePinnedExplorerFromManifest({
      chatThreadIds: ['missing'],
      editorDocumentIds: ['missing'],
      knowledgeSessionIds: ['ks-server'],
    }, {
      chatThreadIds: [],
      editorDocumentIds: [],
    })

    expect(result).toEqual({
      chatThreadIds: [],
      editorDocumentIds: [],
      knowledgeSessionIds: ['ks-server'],
      agentSessionIds: [],
    })
  })
})

describe('detached editor-document import', () => {
  it('preserves manifest order and semantic selection while replacing collaboration identities', async () => {
    const sourceState = createEmptyProjectState()
    const documents: EditorDocumentRecord[] = [
      makeCollaborationDocument('source-a', 'Marker A 🦋', '2026-03-03T00:00:00.000Z'),
      makeCollaborationDocument('source-b', 'Marker B 🦋', '2026-01-01T00:00:00.000Z'),
      makeCollaborationDocument('source-c', 'Marker C 🦋', '2026-02-02T00:00:00.000Z'),
    ]
    const selectedComment: EditorCommentThreadRecord = {
      anchor: {
        from: 2,
        quoteAfter: ' after',
        quoteBefore: 'before ',
        selectedText: 'Marker',
        to: 8,
      },
      commentMarkdown: 'Ausgewählter Kommentar 🧭',
      createdAt: '2026-03-04T00:00:00.000Z',
      documentId: 'source-c',
      id: 'source-comment-c',
      kind: 'collect',
      status: 'open',
      updatedAt: '2026-03-04T00:00:00.000Z',
    }
    sourceState.editorDocuments = Object.fromEntries(
      documents.map((document) => [document.id, document]),
    )
    sourceState.editorDocumentOrder = ['source-b', 'source-a', 'source-c']
    sourceState.editorComments = { [selectedComment.id]: selectedComment }
    sourceState.editorUi = {
      ...sourceState.editorUi,
      activeDocumentId: 'source-c',
      openDocumentIds: ['source-b', 'source-c'],
      selectedCommentId: selectedComment.id,
    }
    sourceState.ui = {
      ...sourceState.ui,
      pinnedExplorer: {
        ...sourceState.ui.pinnedExplorer,
        editorDocumentIds: ['source-a', 'source-c'],
      },
    }

    const manifest = serializeProjectManifest(sourceState).contents
    const documentFiles = documents.map((document) => markdownFileHandle(
      `${document.id}.md`,
      serializeEditorDocument(document, sourceState).contents,
    ))
    const root = projectDirectoryHandle(manifest, [
      documentFiles[2],
      documentFiles[0],
      documentFiles[1],
    ])
    const imported = await loadFromProjectDirectory(root)
    const idByMarker = Object.fromEntries(
      Object.values(imported.editorDocuments).map((document) => [
        document.contentMarkdown.trim(),
        document.id,
      ]),
    )
    const importedComment = Object.values(imported.editorComments).find(
      (comment) => comment.commentMarkdown === selectedComment.commentMarkdown,
    )

    expect(Object.keys(imported.editorDocuments)).toHaveLength(3)
    expect(Object.keys(imported.editorDocuments).some((importedId) => (
      documents.some((document) => document.id === importedId)
    ))).toBe(false)
    expect(imported.editorDocumentOrder).toEqual([
      idByMarker['Marker B 🦋'],
      idByMarker['Marker A 🦋'],
      idByMarker['Marker C 🦋'],
    ])
    expect(imported.editorUi.activeDocumentId).toBe(idByMarker['Marker C 🦋'])
    expect(imported.editorUi.openDocumentIds).toEqual([
      idByMarker['Marker B 🦋'],
      idByMarker['Marker C 🦋'],
    ])
    expect(imported.ui.pinnedExplorer.editorDocumentIds).toEqual([
      idByMarker['Marker A 🦋'],
      idByMarker['Marker C 🦋'],
    ])
    expect(importedComment?.id).not.toBe(selectedComment.id)
    expect(imported.editorUi.selectedCommentId).toBe(importedComment?.id)
  })

  it('falls back deterministically instead of guessing between duplicate source ids', async () => {
    const sourceState = createEmptyProjectState()
    const newer = makeCollaborationDocument(
      'duplicate-source',
      'Newer duplicate',
      '2026-03-03T00:00:00.000Z',
    )
    const older = makeCollaborationDocument(
      'duplicate-source',
      'Older duplicate',
      '2026-01-01T00:00:00.000Z',
    )
    sourceState.editorDocuments = { [newer.id]: newer }
    sourceState.editorDocumentOrder = [newer.id]
    sourceState.editorUi = {
      ...sourceState.editorUi,
      activeDocumentId: newer.id,
      openDocumentIds: [newer.id],
    }
    sourceState.ui = {
      ...sourceState.ui,
      pinnedExplorer: {
        ...sourceState.ui.pinnedExplorer,
        editorDocumentIds: [newer.id],
      },
    }

    const root = projectDirectoryHandle(
      serializeProjectManifest(sourceState).contents,
      [newer, older].map((document, index) => markdownFileHandle(
        `duplicate-${index}.md`,
        serializeEditorDocument(document, sourceState).contents,
      )),
    )
    const imported = await loadFromProjectDirectory(root)
    const idByMarker = Object.fromEntries(
      Object.values(imported.editorDocuments).map((document) => [
        document.contentMarkdown.trim(),
        document.id,
      ]),
    )

    expect(imported.editorDocumentOrder).toEqual([
      idByMarker['Newer duplicate'],
      idByMarker['Older duplicate'],
    ])
    expect(imported.editorUi.activeDocumentId).toBe(idByMarker['Newer duplicate'])
    expect(imported.editorUi.openDocumentIds).toEqual([idByMarker['Newer duplicate']])
    expect(imported.ui.pinnedExplorer.editorDocumentIds).toEqual([])
  })
})

describe('resolveVectorIndexesFromManifest', () => {
  const fileAssets: Record<string, FileAssetRecord> = { f1: makeAsset('f1'), f2: makeAsset('f2') }

  it('returns empty indexes for an old manifest without the vector keys', () => {
    const result = resolveVectorIndexesFromManifest({}, fileAssets)
    expect(result.vectorIndexOrder).toEqual([])
    expect(result.vectorIndexes).toEqual({})
  })

  it('drops members whose source asset is missing', () => {
    const manifest = {
      vector_index_order: ['idx1'],
      vector_indexes: [
        {
          createdAt: '2026-01-01T00:00:00.000Z',
          dims: 3072,
          handle: 'eu',
          id: 'idx1',
          members: [
            { fileId: 'f1', state: 'embedded' },
            { fileId: 'missing', state: 'pending' },
          ],
          model: 'text-embedding-3-large',
          status: 'ready',
          title: 'EU',
          updatedAt: '2026-01-01T00:00:00.000Z',
        },
      ],
    }
    const result = resolveVectorIndexesFromManifest(manifest, fileAssets)
    expect(result.vectorIndexOrder).toEqual(['idx1'])
    expect(result.vectorIndexes.idx1.members).toEqual([{ fileId: 'f1', state: 'embedded' }])
  })

  it('defaults an unknown model/status and reconstructs the order', () => {
    const manifest = {
      vector_indexes: [
        { createdAt: '', id: 'idx1', members: [], model: 'bogus', status: 'weird', title: 'A', updatedAt: '' },
      ],
    }
    const result = resolveVectorIndexesFromManifest(manifest, fileAssets)
    expect(result.vectorIndexOrder).toEqual(['idx1'])
    expect(result.vectorIndexes.idx1.model).toBe('text-embedding-3-large')
    expect(result.vectorIndexes.idx1.dims).toBe(3072)
    expect(result.vectorIndexes.idx1.status).toBe('ready')
  })

  it('reconciles a persisted "indexing" status on load (no run survives a reload)', () => {
    // A reindex that crashed mid-run leaves the record at "indexing" with no
    // live job (indexingJobs is never serialized). Loading must reconcile it
    // to the pre-run status so the UI is not wedged and its server
    // autosave is not deferred forever: stale if any member still pending...
    const stalish = resolveVectorIndexesFromManifest({
      vector_indexes: [{
        createdAt: '', id: 'idx1', model: 'text-embedding-3-large', status: 'indexing',
        title: 'A', updatedAt: '',
        members: [{ fileId: 'f1', state: 'embedded' }, { fileId: 'f2', state: 'pending' }],
      }],
    }, fileAssets)
    expect(stalish.vectorIndexes.idx1.status).toBe('stale')

    // ...ready when every member is already embedded.
    const readyish = resolveVectorIndexesFromManifest({
      vector_indexes: [{
        createdAt: '', id: 'idx1', model: 'text-embedding-3-large', status: 'indexing',
        title: 'A', updatedAt: '',
        members: [{ fileId: 'f1', state: 'embedded' }],
      }],
    }, fileAssets)
    expect(readyish.vectorIndexes.idx1.status).toBe('ready')
  })

  it('reconstructs history, lastError and serverCollectionId (Frozen-Rebuild guard)', () => {
    const manifest = {
      vector_index_order: ['idx1'],
      vector_indexes: [
        {
          createdAt: '2026-01-01T00:00:00.000Z',
          dims: 3072,
          handle: 'eu',
          history: [
            { documents: 4, durationMs: 47_000, finishedAt: '2026-06-15T08:41:00.000Z', result: 'ok', startedAt: '2026-06-15T08:40:13.000Z' },
            { documents: 0, durationMs: 6_000, error: 'backend down', finishedAt: '2026-06-10T11:20:00.000Z', result: 'error', startedAt: '2026-06-10T11:19:54.000Z' },
          ],
          id: 'idx1',
          lastError: 'backend down',
          members: [{ fileId: 'f1', state: 'embedded' }],
          model: 'text-embedding-3-large',
          serverCollectionId: 'kc_live',
          status: 'ready',
          title: 'EU',
          updatedAt: '2026-01-01T00:00:00.000Z',
        },
      ],
    }
    const idx = resolveVectorIndexesFromManifest(manifest, fileAssets).vectorIndexes.idx1
    expect(idx.serverCollectionId).toBe('kc_live')
    expect(idx.lastError).toBe('backend down')
    expect(idx.history).toHaveLength(2)
    expect(idx.history?.[0]).toMatchObject({ documents: 4, result: 'ok' })
    expect(idx.history?.[1]).toMatchObject({ error: 'backend down', result: 'error' })
  })
})

function makeCollaborationDocument(
  id: string,
  contentMarkdown: string,
  updatedAt: string,
): EditorDocumentRecord {
  return {
    contentMarkdown,
    contentMode: 'collaboration',
    createdAt: '2026-01-01T00:00:00.000Z',
    folderId: null,
    id,
    revision: 7,
    source: 'blank',
    title: 'Identischer Titel 🦋.md',
    updatedAt,
  }
}

function markdownFileHandle(name: string, contents: string): FileSystemFileHandle {
  return {
    getFile: async () => ({ text: async () => contents }) as File,
    kind: 'file',
    name,
  } as FileSystemFileHandle
}

function projectDirectoryHandle(
  manifest: string,
  documents: FileSystemFileHandle[],
): FileSystemDirectoryHandle {
  const documentsDirectory = {
    async *entries() {
      for (const document of documents) yield [document.name, document] as const
    },
    kind: 'directory',
    name: 'documents',
  } as unknown as FileSystemDirectoryHandle
  const manifestHandle = markdownFileHandle('project.md', manifest)

  return {
    getDirectoryHandle: async (name: string) => {
      if (name === 'documents') return documentsDirectory
      throw new Error(`Missing directory: ${name}`)
    },
    getFileHandle: async (name: string) => {
      if (name === 'project.md') return manifestHandle
      throw new Error(`Missing file: ${name}`)
    },
    kind: 'directory',
    name: 'Detached Import Fixture',
  } as FileSystemDirectoryHandle
}

async function loadFromProjectDirectory(directory: FileSystemDirectoryHandle) {
  const previousWindow = globalThis.window
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { showDirectoryPicker: async () => directory },
  })

  try {
    return await loadProject()
  } finally {
    if (previousWindow === undefined) {
      Reflect.deleteProperty(globalThis, 'window')
    } else {
      Object.defineProperty(globalThis, 'window', {
        configurable: true,
        value: previousWindow,
      })
    }
  }
}


describe('project theme preferences', () => {
  it.each(['petrol', 'standard', 'slate', 'graphite', 'sage'] as const)(
    'preserves %s through project export and import', async (preset) => {
      const state = createEmptyProjectState()
      state.preferences.themePreset = preset
      const manifest = serializeProjectManifest(state).contents
      const imported = await loadFromProjectDirectory(projectDirectoryHandle(manifest, []))
      expect(imported.preferences.themePreset).toBe(preset)
    },
  )

  it('starts a new project with Petrol', () => {
    expect(createEmptyProjectState().preferences.themePreset).toBe('petrol')
  })
})
