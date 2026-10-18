import type { Editor } from '@tiptap/react'

export type EditorSurfaceIdentity = {
  documentId: string
  generation: number | null
}

export type EditorSurfaceRegistration = EditorSurfaceIdentity & {
  editor: Editor
}

export function editorForSurfaceIdentity(
  registration: EditorSurfaceRegistration | null,
  identity: EditorSurfaceIdentity | null,
): Editor | null {
  if (
    !registration
    || !identity
    || registration.documentId !== identity.documentId
    || registration.generation !== identity.generation
  ) return null
  return registration.editor
}

/** A late cleanup from surface A must not erase a newer B registration. */
export function updateEditorSurfaceRegistration(
  registration: EditorSurfaceRegistration | null,
  identity: EditorSurfaceIdentity,
  editor: Editor | null,
): EditorSurfaceRegistration | null {
  if (editor) return { ...identity, editor }
  if (
    registration?.documentId === identity.documentId
    && registration.generation === identity.generation
  ) return null
  return registration
}
