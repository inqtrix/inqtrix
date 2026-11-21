import { beforeEach, describe, expect, it, vi } from 'vitest'

import { applyEditorPatch, getEditorDocument } from '@/api/inqtrixClient'
import { createEmptyProjectState } from '@/features/project/seedProject'
import type {
  EditorDocumentRecord,
  EditorSuggestionRecord,
  ProjectState,
} from '@/features/project/types'
import { researchDeskReducer, type ResearchDeskAction } from '@/features/researchDesk/state'
import { useEditorSuggestions, type UseEditorSuggestionsArgs } from './useEditorSuggestions'

// The hook's handlers run for real on a stand-in for one render: no DOM in
// this lane, and the decision is a chain of awaited calls a render does not
// change.
vi.mock('react', async (original) => ({
  ...await original<typeof import('react')>(),
  useEffect: () => undefined,
  useMemo: <T,>(factory: () => T) => factory(),
  useRef: <T,>(value: T) => ({ current: value }),
  useState: <T,>(value: T | (() => T)) => [
    typeof value === 'function' ? (value as () => T)() : value,
    () => undefined,
  ],
}))
vi.mock('@/api/inqtrixClient', async (original) => ({
  ...await original<typeof import('@/api/inqtrixClient')>(),
  applyEditorPatch: vi.fn(),
  getEditorDocument: vi.fn(),
}))

function paragraphs(...lines: string[]): string {
  return `${lines.join('\n\n')}\n`
}

const ON_SERVER = paragraphs('# Plan', 'Der Bau dauert sechs Wochen.', 'Das Team trifft sich montags.')

function agentPatchPart(): EditorSuggestionRecord {
  return {
    anchor: { from: 9, quoteAfter: '', quoteBefore: '', selectedText: 'sechs', to: 14 },
    blockId: 'block-1',
    createdAt: '2026-03-04T08:00:00.000Z',
    documentId: 'doc-1',
    groupId: 'group-1',
    id: 'part-1',
    origin: { kind: 'global_run' },
    originalText: 'sechs',
    proposedText: 'sieben',
    serverPatch: { editId: 'edit-1', patchId: 'patch-1' },
    status: 'pending',
    updatedAt: '2026-03-04T08:00:00.000Z',
  }
}

beforeEach(() => {
  vi.mocked(applyEditorPatch).mockReset()
  vi.mocked(getEditorDocument).mockReset()
})

describe('accepting an agent patch without the immediate save', () => {
  it('merges the server text with the record\'s own base, so text the server never had is kept', async () => {
    // The project file is saved, but the change to the meeting day never
    // reached the server, and nothing tells which server text it was
    // written on.
    const own = ON_SERVER.replace('montags', 'dienstags')
    const document: EditorDocumentRecord = {
      contentMarkdown: own,
      createdAt: '2026-03-04T08:00:00.000Z',
      folderId: null,
      id: 'doc-1',
      revision: 7,
      serverSynced: true,
      source: 'blank',
      title: 'Plan.md',
      updatedAt: '2026-03-04T08:00:00.000Z',
    }
    const part = agentPatchPart()
    const empty = createEmptyProjectState()
    let state: ProjectState = {
      ...empty,
      dirty: false,
      editorDocumentOrder: ['doc-1'],
      editorDocuments: { 'doc-1': document },
      editorSuggestionGroups: {
        'group-1': {
          createdAt: part.createdAt,
          documentId: 'doc-1',
          id: 'group-1',
          origin: { kind: 'global_run' },
        },
      },
      editorSuggestions: { 'part-1': part },
      editorUi: { ...empty.editorUi, activeDocumentId: 'doc-1', openDocumentIds: ['doc-1'] },
    }
    const dispatch = (action: ResearchDeskAction) => {
      state = researchDeskReducer(state, action)
    }
    // The merge as the workspace's history hook dispatches it.
    const merged: Array<{ contentMarkdown: string; documentId: string; revision: number }> = []
    const mergeServerDocument: UseEditorSuggestionsArgs['mergeServerDocument'] = async (documentId, server) => {
      merged.push({ contentMarkdown: server.contentMarkdown, documentId, revision: server.revision })
      dispatch({
        at: '2026-03-04T09:00:00.000Z',
        conflictCopy: { id: 'doc-copy', title: 'Plan (Konfliktkopie).md' },
        documentId,
        observed: null,
        server: {
          contentMarkdown: server.contentMarkdown,
          folderId: server.folderId,
          revision: server.revision,
          title: server.title,
        },
        type: 'mergeServerEditorDocument',
      })
      return state.editorDocuments['doc-copy']
        ? { conflictCopyId: 'doc-copy', kind: 'conflict' }
        : { kind: 'merged' }
    }
    const patched = ON_SERVER.replace('sechs Wochen', 'sieben Wochen')
    vi.mocked(applyEditorPatch).mockResolvedValue({
      applied_edit_ids: ['edit-1'],
      document_id: 'doc-1',
      revision: 8,
    })
    vi.mocked(getEditorDocument).mockResolvedValue({
      content_markdown: patched,
      created_at: 1_772_611_200,
      diff_anchor_markdown: null,
      diff_anchor_updated_at: null,
      folder_id: null,
      id: 'doc-1',
      revision: 8,
      source: 'blank',
      source_run_id: null,
      title: 'Plan.md',
      updated_at: 1_772_614_800,
    })
    const controller = useEditorSuggestions({
      activeDocument: document,
      activeEditor: null,
      attachedCommentIds: [],
      attachedRefs: [],
      capabilities: null,
      collaboration: {} as UseEditorSuggestionsArgs['collaboration'],
      comments: [],
      dispatch,
      handedOverDiscussions: [],
      locale: 'de',
      mergeServerDocument,
      onGlobalSuccess: () => undefined,
      selectedModelTier: null,
      state,
    })

    await controller.handleAcceptSuggestion(part)

    expect(vi.mocked(applyEditorPatch).mock.calls.map((call) => call.slice(0, 2))).toEqual([['patch-1', 7]])
    expect(merged).toEqual([{ contentMarkdown: patched, documentId: 'doc-1', revision: 8 }])
    expect(state.editorDocuments['doc-1']).toMatchObject({ contentMarkdown: patched, revision: 8 })
    expect(state.editorDocuments['doc-copy']).toMatchObject({ contentMarkdown: own })
    expect(state.editorSuggestions['part-1'].status).toBe('accepted')
  })
})
