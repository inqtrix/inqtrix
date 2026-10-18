import { readFileSync } from 'node:fs'

import {
  EDITOR_YJS_FRAGMENT,
  createEditorSchemaExtensions,
} from '@inqtrix/editor-schema'
import { getSchema } from '@tiptap/core'
import { prosemirrorJSONToYDoc } from '@tiptap/y-tiptap'
import { describe, expect, it, vi } from 'vitest'
import * as Y from 'yjs'

import type { EditorDocumentRecord } from '@/features/project/types'
import {
  collaborationDocumentMarkdown,
  workspaceRecoveryStore,
} from './collaborationRecovery'

const SCHEMA = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

function suggestion(kind: 'deletion' | 'insertion', id: string) {
  return {
    attrs: {
      authorId: 'user-2',
      createdAt: 1,
      id,
      kind,
      patchId: `patch-${id}`,
      suggestionId: id,
    },
    type: kind,
  }
}

function documentWithSuggestions(): Y.Doc {
  return prosemirrorJSONToYDoc(SCHEMA, {
    content: [
      {
        content: [
          { text: 'Hello ', type: 'text' },
          { marks: [suggestion('insertion', 's1')], text: 'inserted', type: 'text' },
          { marks: [suggestion('deletion', 's2')], text: ' deleted', type: 'text' },
          { marks: [{ type: 'bold' }], text: ' bold', type: 'text' },
        ],
        type: 'paragraph',
      },
    ],
    type: 'doc',
  }, EDITOR_YJS_FRAGMENT)
}

function editorDocument(id: string, overrides: Partial<EditorDocumentRecord> = {}): EditorDocumentRecord {
  return {
    contentMarkdown: '',
    createdAt: '2026-01-01T00:00:00.000Z',
    folderId: null,
    id,
    revision: 1,
    source: 'blank',
    title: 'Quarterly.md',
    updatedAt: '2026-01-01T00:00:00.000Z',
    ...overrides,
  }
}

describe('collaboration document markdown for recovery', () => {
  it('keeps every text the editor showed, pending suggestions included, like the backup download', () => {
    expect(collaborationDocumentMarkdown(documentWithSuggestions()))
      .toBe('Hello inserted deleted **bold**')
  })

  it('leaves out what the schema refuses without changing the document it reads', () => {
    const document = documentWithSuggestions()
    document.getXmlFragment(EDITOR_YJS_FRAGMENT).push([new Y.XmlElement('unknownBlock')])
    const before = Y.encodeStateAsUpdate(document)

    expect(collaborationDocumentMarkdown(document)).toBe('Hello inserted deleted **bold**')
    expect(Y.encodeStateAsUpdate(document)).toEqual(before)
  })
})

describe('workspace recovery store for replaced generations', () => {
  it('adds a recovery copy of a listed workspace document and takes the state', () => {
    const dispatch = vi.fn()
    const store = workspaceRecoveryStore({
      dispatch,
      documents: () => ({ 'doc-1': editorDocument('doc-1', { sourceRunId: 'run-1' }) }),
      now: () => new Date('2026-02-03T04:05:06.000Z'),
      workspaceId: 'workspace-1',
    })

    expect(store({
      document: documentWithSuggestions(),
      documentId: 'doc-1',
      generation: 2,
      scope: 'workspace-1',
    })).toBe(true)
    expect(dispatch).toHaveBeenCalledExactlyOnceWith({
      capture: {
        capturedAt: '2026-02-03T04:05:06.000Z',
        contentMarkdown: 'Hello inserted deleted **bold**',
        documentId: 'doc-1',
      },
      reason: 'generation_replaced',
      source: { source: 'blank', sourceRunId: 'run-1', title: 'Quarterly.md' },
      type: 'addEditorRecoveryDocument',
    })
  })

  it('leaves another workspace\'s or a guest\'s lifecycle, and an unlisted document, with their lifecycle', () => {
    const dispatch = vi.fn()
    const store = workspaceRecoveryStore({
      dispatch,
      documents: () => ({ 'doc-1': editorDocument('doc-1') }),
      workspaceId: 'workspace-1',
    })

    for (const [scope, documentId] of [
      ['workspace-2', 'doc-1'],
      ['guest:guest-1', 'doc-1'],
      ['workspace-1', 'doc-2'],
    ] as const) {
      expect(store({ document: new Y.Doc(), documentId, generation: 2, scope })).toBe(false)
    }
    expect(dispatch).not.toHaveBeenCalled()
  })
})

describe('the desk installs the recovery store', () => {
  it('under the workspace the editor keys its collaboration lifecycles by', () => {
    const desk = readFileSync(
      new URL('../researchDesk/ResearchDesk.tsx', import.meta.url),
      'utf8',
    )
    const installed = /useReplacedGenerationRecovery\(\{\s*dispatch,\s*documents: state\.editorDocuments,\s*workspaceId: (\w+),\s*\}\)/
      .exec(desk)
    expect(installed).not.toBeNull()
    const editor = /<EditorWorkspace[\s\S]*?\n\s*workspaceId=\{(\w+)\}/.exec(desk)
    expect(editor).not.toBeNull()
    expect(installed![1]).toBe(editor![1])
  })
})
