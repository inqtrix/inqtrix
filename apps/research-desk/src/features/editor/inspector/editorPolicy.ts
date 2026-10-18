import type { Editor } from '@tiptap/react'

import {
  collaborationReviewPluginKey,
  type CollaborationReviewDisplay,
  type CollaborationReviewOverlayUpdate,
} from '../tiptap'
import type {
  EditorChangesView,
  EditorInspectorTab,
  EditorWriteMode,
  InspectorChange,
} from './model'

export type CollaborationEditorPolicyInput = {
  collaboration: boolean
  changesView: EditorChangesView
  display: CollaborationReviewDisplay
  documentId: string | null
  inspectorTab: EditorInspectorTab
  selectedChangeId: string | null
  visibleChanges: readonly InspectorChange[]
  writeAuthorId: string | null
  writeMode: EditorWriteMode
}

export function collaborationEditorPolicyUpdate({
  collaboration,
  changesView,
  display,
  documentId,
  inspectorTab,
  selectedChangeId,
  visibleChanges,
  writeAuthorId,
  writeMode,
}: CollaborationEditorPolicyInput): CollaborationReviewOverlayUpdate {
  const changesVisible = collaboration && inspectorTab === 'changes' && changesView === 'open'
  const selectedChange = visibleChanges.find((change) => change.id === selectedChangeId)
  return {
    collaboration,
    display: changesVisible ? display : 'final',
    documentId,
    enabled: collaboration,
    selectedSuggestionIds: changesVisible
      ? selectedChange?.suggestionIds ?? []
      : [],
    visibleSuggestionIds: changesVisible
      ? visibleChanges.flatMap((change) => change.suggestionIds)
      : undefined,
    writeAuthorId,
    writeMode,
  }
}

export function applyCollaborationEditorPolicy(
  editor: Editor | null,
  input: CollaborationEditorPolicyInput,
): void {
  if (!editor || editor.isDestroyed) return
  editor.view.dispatch(
    editor.state.tr.setMeta(collaborationReviewPluginKey, collaborationEditorPolicyUpdate(input)),
  )
}
