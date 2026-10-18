import '@fontsource-variable/inter/index.css'
import '@/styles/globals.css'

import { useReducer } from 'react'
import { createRoot } from 'react-dom/client'

import { AppProviders } from '@/app/AppProviders'
import type { FileAssetRecord } from '@/features/project/types'
import type { KnowledgeCollectionInfo } from '@/features/researchRuns/types'
import {
  initializeResearchDeskState,
  researchDeskReducer,
  type ResearchDeskState,
} from '@/features/researchDesk/state'
import { FileLibraryWorkspace } from '../FileLibraryWorkspace'

function asset(id: string, title: string): FileAssetRecord {
  return {
    createdAt: '2026-10-01T00:00:00.000Z',
    extractedText: `${title} text`,
    fileName: title,
    groupId: null,
    id,
    label: title,
    mimeType: 'application/pdf',
    origin: 'library',
    pageCount: 3,
    parseStatus: 'parsed',
    parseWarning: null,
    sectionId: 'file-section-temp',
    serverFileId: `srv-${id}`,
    sizeBytes: 1_000,
    textTruncated: false,
    title,
    updatedAt: '2026-10-01T00:00:00.000Z',
  }
}

const query = new URLSearchParams(window.location.search)
/** Members a page load finds already indexed (`?embedded=file-a,file-b`). */
const embeddedOnLoad = new Set((query.get('embedded') ?? '').split(',').filter(Boolean))

/** A collection shared with the user (`?shared=1`), opened from the rail. */
const sharedCollections: KnowledgeCollectionInfo[] = query.get('shared')
  ? [{
      access: { mode: 'shared', permission: 'view' },
      created_at: 1_760_000_000,
      document_count: 1,
      embedding_dim: 3072,
      embedding_model: 'text-embedding-3-large',
      id: 'kc_shared',
      name: 'Geteilt',
    }]
  : []

/** A vector index ("Verträge") whose server collection exists, with three
 * server files not yet in it: "Index 3" runs the incremental client pool
 * against the routed indexing API. a and b carry their server document ids so
 * a reload can map paused jobs back onto their rows. */
function seededState(): ResearchDeskState {
  let state = researchDeskReducer(initializeResearchDeskState(), {
    assets: [asset('file-a', 'a.pdf'), asset('file-b', 'b.pdf'), asset('file-c', 'c.pdf')],
    type: 'ingestFileAssets',
  })
  state = researchDeskReducer(state, {
    fileIds: ['file-a', 'file-b', 'file-c'],
    title: 'Verträge',
    type: 'createVectorIndex',
  })
  const id = state.vectorIndexOrder[0]
  const index = state.vectorIndexes[id]
  const documentIds: Record<string, string> = { 'file-a': 'kd_a', 'file-b': 'kd_b' }
  const seeded: ResearchDeskState = {
    ...state,
    vectorIndexes: {
      ...state.vectorIndexes,
      [id]: {
        ...index,
        members: index.members.map((member) => ({
          ...member,
          ...(documentIds[member.fileId]
            ? { serverDocumentId: documentIds[member.fileId] }
            : {}),
          ...(embeddedOnLoad.has(member.fileId) ? { state: 'embedded' as const } : {}),
        })),
        serverCollectionId: 'kc_fix',
        serverCollectionModel: index.model,
      },
    },
  }
  // A second index without documents (`?emptyIndex=1`).
  return query.get('emptyIndex')
    ? researchDeskReducer(seeded, { fileIds: [], title: 'Leer', type: 'createVectorIndex' })
    : seeded
}

/** The deployment the page plays (`?knowledge=`): absent means knowledge
 * works, `demo` means no server at all (the local simulator), and a server
 * reason (`operator_disabled`, `store_unreachable`) means a real deployment
 * without a knowledge base. */
const knowledgeMode = query.get('knowledge')

/** One object for the page's life, as the desk memoizes it: a fresh object per
 * render would re-run every effect keyed on the connection. */
const knowledgeSync = knowledgeMode === null ? { workspaceId: 'ws-fixture' } : null
const knowledgeUnavailableReason =
  knowledgeMode === null || knowledgeMode === 'demo' ? null : knowledgeMode

function IndexPauseFixture() {
  const [state, dispatch] = useReducer(researchDeskReducer, undefined, seededState)
  return (
    <FileLibraryWorkspace
      assetDeletionApiOptions={null}
      deletionScopeKey="fixture"
      dispatch={dispatch}
      embedModels={[
        { dims: 3072, id: 'text-embedding-3-large', label: 'text-embedding-3-large', provider: 'OpenAI' },
        { dims: 1536, id: 'text-embedding-3-small', label: 'text-embedding-3-small', provider: 'OpenAI' },
      ]}
      fileApiOptions={null}
      knowledgeSync={knowledgeSync}
      knowledgeUnavailableReason={knowledgeUnavailableReason}
      serverCollections={sharedCollections}
      state={state}
    />
  )
}

createRoot(document.getElementById('root')!).render(
  <AppProviders>
    <IndexPauseFixture />
  </AppProviders>,
)
