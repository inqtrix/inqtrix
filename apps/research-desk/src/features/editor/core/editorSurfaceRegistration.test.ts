import type { Editor } from '@tiptap/react'
import { describe, expect, it } from 'vitest'

import {
  editorForSurfaceIdentity,
  updateEditorSurfaceRegistration,
} from './editorSurfaceRegistration'

describe('editor surface registration', () => {
  it('never exposes surface A as B and ignores A cleanup after B registers', () => {
    const editorA = { id: 'editor-a' } as unknown as Editor
    const editorB = { id: 'editor-b' } as unknown as Editor
    const identityA = { documentId: 'doc-a', generation: 2 }
    const identityB = { documentId: 'doc-b', generation: 3 }
    let registration = updateEditorSurfaceRegistration(null, identityA, editorA)

    expect(editorForSurfaceIdentity(registration, identityA)).toBe(editorA)
    expect(editorForSurfaceIdentity(registration, identityB)).toBeNull()

    registration = updateEditorSurfaceRegistration(registration, identityB, editorB)
    registration = updateEditorSurfaceRegistration(registration, identityA, null)

    expect(editorForSurfaceIdentity(registration, identityB)).toBe(editorB)
  })
})
