import {
  EDITOR_YJS_FRAGMENT,
  createEditorSchemaExtensions,
  serializeWithRawCitationLabels,
} from '@inqtrix/editor-schema'
import { getSchema } from '@tiptap/core'
import { MarkdownManager } from '@tiptap/markdown'
import { yXmlFragmentToProseMirrorRootNode } from '@tiptap/y-tiptap'
import { useEffect, useRef, type Dispatch } from 'react'
import * as Y from 'yjs'

import type { EditorDocumentRecord } from '@/features/project/types'
import type { ResearchDeskAction } from '@/features/researchDesk/state'
import {
  noteCollaborationDocumentGeneration,
  registerReplacedGenerationRecovery,
  type ReplacedGenerationRecoveryStore,
} from './useCollaborationDocument'

// The editor's own schema and Markdown settings: the editor serializes with
// the Markdown extension of these extensions.
const RECOVERY_EXTENSIONS = createEditorSchemaExtensions({ enableUndoRedo: false })
const RECOVERY_SCHEMA = getSchema(RECOVERY_EXTENSIONS)
let recoveryMarkdown: MarkdownManager | null = null

/**
 * The local state of a collaboration editor document as Markdown, the way
 * the editor shows it and its backup download saves it: every text with its
 * formatting, the text of pending suggestions without their marks.
 */
export function collaborationDocumentMarkdown(document: Y.Doc): string {
  recoveryMarkdown ??= new MarkdownManager({
    extensions: RECOVERY_EXTENSIONS,
    markedOptions: { gfm: true },
  })
  const manager = recoveryMarkdown
  // The editor's reader leaves out what the schema refuses and deletes it
  // from the document it reads, so it reads a copy.
  const copy = new Y.Doc()
  try {
    Y.applyUpdate(copy, Y.encodeStateAsUpdate(document))
    const root = yXmlFragmentToProseMirrorRootNode(
      copy.getXmlFragment(EDITOR_YJS_FRAGMENT),
      RECOVERY_SCHEMA,
    )
    return serializeWithRawCitationLabels(
      root.toJSON(),
      RECOVERY_SCHEMA,
      (json) => manager.serialize(json),
    )
  } finally {
    copy.destroy()
  }
}

/**
 * Where the local state of a replaced generation of a workspace document
 * goes: a local recovery copy next to the document, the same kind of copy a
 * remotely deleted document leaves behind. Takes only documents of
 * `workspaceId` that are still listed; everything else stays with its
 * lifecycle.
 */
export function workspaceRecoveryStore({
  dispatch,
  documents,
  now = () => new Date(),
  workspaceId,
}: {
  dispatch: Dispatch<ResearchDeskAction>
  documents: () => Readonly<Record<string, EditorDocumentRecord>>
  now?: () => Date
  workspaceId: string
}): ReplacedGenerationRecoveryStore {
  return (recovery) => {
    if (recovery.scope !== workspaceId) return false
    const original = documents()[recovery.documentId]
    if (!original) return false
    dispatch({
      capture: {
        capturedAt: now().toISOString(),
        contentMarkdown: collaborationDocumentMarkdown(recovery.document),
        documentId: recovery.documentId,
      },
      reason: 'generation_replaced',
      source: {
        source: original.source,
        ...(original.sourceRunId ? { sourceRunId: original.sourceRunId } : {}),
        title: original.title,
      },
      type: 'addEditorRecoveryDocument',
    })
    return true
  }
}

/**
 * Keeps the local state of a replaced collaboration generation of this
 * workspace's documents as a recovery copy (see
 * registerReplacedGenerationRecovery), and tells the lifecycle registry
 * which generation each listed document has now.
 */
export function useReplacedGenerationRecovery({
  dispatch,
  documents,
  workspaceId,
}: {
  dispatch: Dispatch<ResearchDeskAction>
  documents: Readonly<Record<string, EditorDocumentRecord>>
  workspaceId: string
}): void {
  const documentsRef = useRef(documents)
  useEffect(() => {
    documentsRef.current = documents
  }, [documents])
  useEffect(() => registerReplacedGenerationRecovery(workspaceRecoveryStore({
    dispatch,
    documents: () => documentsRef.current,
    workspaceId,
  })), [dispatch, workspaceId])
  useEffect(() => {
    for (const document of Object.values(documents)) {
      const generation = document.collaboration?.generation
      if (typeof generation !== 'number') continue
      noteCollaborationDocumentGeneration({
        documentId: document.id,
        generation,
        workspaceId,
      })
    }
  }, [documents, workspaceId])
}
