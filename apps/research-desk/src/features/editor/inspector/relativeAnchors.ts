import {
  createRelativePositionAdapter,
  EDITOR_SCHEMA_BEHAVIOR_INPUTS,
  EDITOR_YJS_FRAGMENT,
  type EditorRelativePositionAdapter,
  type ProseMirrorMapping,
  type SerializedRelativePosition,
} from '@inqtrix/editor-schema'
import { ySyncPluginKey } from '@tiptap/y-tiptap'
import type { Editor } from '@tiptap/react'
import type * as Y from 'yjs'

import type {
  EditorCommentAnchorRecord,
  EditorCommentThreadRecord,
} from '@/features/project/types'

const RELATIVE_ANCHOR_VERSION = EDITOR_SCHEMA_BEHAVIOR_INPUTS.relativePositions

export type CollaborationRelativeAnchor = EditorCommentAnchorRecord & {
  relativeFrom: SerializedRelativePosition
  relativeTo: SerializedRelativePosition
  relativeVersion: typeof RELATIVE_ANCHOR_VERSION
}

export type CollaborationCommentAnchorResolution = {
  comment: EditorCommentThreadRecord
  reason?: 'adapter_unavailable' | 'relative_unresolved'
  status: 'degraded' | 'legacy' | 'relative'
}

export function serializeCollaborationAnchor(
  anchor: EditorCommentAnchorRecord,
  adapter: EditorRelativePositionAdapter,
): CollaborationRelativeAnchor {
  return {
    ...anchor,
    relativeFrom: adapter.fromProseMirrorPosition(anchor.from),
    relativeTo: adapter.fromProseMirrorPosition(anchor.to),
    relativeVersion: RELATIVE_ANCHOR_VERSION,
  }
}

export function resolveCollaborationAnchor(
  anchor: EditorCommentAnchorRecord,
  adapter: EditorRelativePositionAdapter,
): { anchor: EditorCommentAnchorRecord; source: 'quote' | 'relative' } {
  if (!hasCollaborationRelativeAnchor(anchor)) return { anchor, source: 'quote' }
  try {
    const from = adapter.toProseMirrorPosition(anchor.relativeFrom)
    const to = adapter.toProseMirrorPosition(anchor.relativeTo)
    if (from === null || to === null || from < 0 || to < from) {
      return { anchor, source: 'quote' }
    }
    return {
      anchor: { ...anchor, from, to },
      source: 'relative',
    }
  } catch {
    return { anchor, source: 'quote' }
  }
}

export function serializeCollaborationCommentAnchor(
  editor: Editor,
  document: Y.Doc,
  comment: EditorCommentThreadRecord,
): EditorCommentThreadRecord {
  return {
    ...comment,
    anchor: serializeCollaborationAnchor(comment.anchor, editorRelativePositionAdapter(editor, document)),
  }
}

export function serializeEditorCollaborationAnchor(
  editor: Editor,
  document: Y.Doc,
  anchor: EditorCommentAnchorRecord,
): CollaborationRelativeAnchor {
  return serializeCollaborationAnchor(
    anchor,
    editorRelativePositionAdapter(editor, document),
  )
}

export function resolveEditorCollaborationAnchor(
  editor: Editor,
  document: Y.Doc,
  anchor: EditorCommentAnchorRecord,
): {
  anchor: EditorCommentAnchorRecord
  status: 'degraded' | 'legacy' | 'relative'
} {
  if (!hasCollaborationRelativeAnchor(anchor)) {
    return { anchor, status: 'legacy' }
  }
  try {
    const resolved = resolveCollaborationAnchor(
      anchor,
      editorRelativePositionAdapter(editor, document),
    )
    return resolved.source === 'relative'
      ? { anchor: resolved.anchor, status: 'relative' }
      : { anchor, status: 'degraded' }
  } catch {
    return { anchor, status: 'degraded' }
  }
}

export function resolveCollaborationCommentAnchorWithStatus(
  editor: Editor,
  document: Y.Doc,
  comment: EditorCommentThreadRecord,
): CollaborationCommentAnchorResolution {
  if (!hasCollaborationRelativeAnchor(comment.anchor)) {
    return { comment, status: 'legacy' }
  }
  try {
    const resolved = resolveCollaborationAnchor(
      comment.anchor,
      editorRelativePositionAdapter(editor, document),
    )
    return resolved.source === 'relative'
      ? { comment: { ...comment, anchor: resolved.anchor }, status: 'relative' }
      : { comment, reason: 'relative_unresolved', status: 'degraded' }
  } catch {
    return { comment, reason: 'adapter_unavailable', status: 'degraded' }
  }
}

export function hasCollaborationRelativeAnchor(
  anchor: EditorCommentAnchorRecord,
): anchor is CollaborationRelativeAnchor {
  const candidate = anchor as EditorCommentAnchorRecord & Partial<CollaborationRelativeAnchor>
  return candidate.relativeVersion === RELATIVE_ANCHOR_VERSION
    && typeof candidate.relativeFrom === 'string'
    && typeof candidate.relativeTo === 'string'
}

function editorRelativePositionAdapter(
  editor: Editor,
  document: Y.Doc,
): EditorRelativePositionAdapter {
  const state = ySyncPluginKey.getState(editor.state) as {
    binding?: { mapping?: ProseMirrorMapping }
  } | undefined
  const mapping = state?.binding?.mapping
  if (!(mapping instanceof Map)) {
    throw new Error('The collaboration editor mapping is not available.')
  }
  return createRelativePositionAdapter(
    document,
    document.getXmlFragment(EDITOR_YJS_FRAGMENT),
    mapping,
  )
}
