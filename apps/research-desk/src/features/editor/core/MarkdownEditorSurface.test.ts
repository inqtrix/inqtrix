import { readFileSync } from 'node:fs'
import path from 'node:path'
import type { HocuspocusProvider } from '@hocuspocus/provider'
import {
  EDITOR_SCHEMA_BEHAVIOR_INPUTS,
  createEditorSchemaExtensions,
  parseEditorMarkdown,
} from '@inqtrix/editor-schema'
import { getSchema, type Extensions } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { EditorState, type Transaction } from '@tiptap/pm/state'
import { describe, expect, it, vi } from 'vitest'
import * as Y from 'yjs'
import type { EditorSuggestionRecord } from '@/features/project/types'

vi.mock('../tiptap', () => ({
  InqtrixCollaborationCaret: {
    configure: vi.fn(() => ({ name: 'collaborationCaret' })),
  },
  collaborationCaretOptions: {
    render: vi.fn(),
    selectionRender: vi.fn(),
  },
  commentDecorationPluginKey: {},
  createEditorExtensions: () => [],
  normalizeEditorMarkdownForTiptap: (markdown: string) => markdown,
  serializeEditorFinalProjectionMarkdown: () => '',
  serializeEditorMarkdown: () => '',
  suggestionDecorationPluginKey: {},
}))

import {
  collaborationBindingForEditorDocument,
  collaborationSurfaceAcceptsLocalEdits,
  configureEditorExtensionsForCollaboration,
  editorSuggestionDecorationSignature,
  editorSuggestionShouldAutoStaleOnMissingTarget,
  editorBodyChangeDecision,
  editorBodyFromHost,
  editorSurfaceInitialContent,
  editorSurfaceLifecyclePolicy,
  INITIAL_EDITOR_BODY_CHANGE_STATE,
  isCollaborationSurfaceSynced,
  type EditorBodyChangeState,
} from './MarkdownEditorSurface'

describe('private suggestion decoration identity', () => {
  it('rebuilds a stable suggestion when its materialized anchor moves', () => {
    const suggestion: EditorSuggestionRecord = {
      anchor: {
        from: 5,
        quoteAfter: '',
        quoteBefore: '',
        selectedText: 'target',
        to: 11,
      },
      anchorText: 'target',
      blockId: 'block-1',
      createdAt: '2026-07-31T00:00:00.000Z',
      documentId: 'document-1',
      editPosition: 'replace',
      groupId: 'group-1',
      id: 'editor-suggestion-stable',
      originalText: 'target',
      origin: { kind: 'global_run' },
      proposedText: 'proposal',
      revision: 1,
      status: 'pending',
      updatedAt: '2026-07-31T00:00:00.000Z',
    }

    const before = editorSuggestionDecorationSignature([suggestion])
    const after = editorSuggestionDecorationSignature([{
      ...suggestion,
      anchor: { ...suggestion.anchor, from: 12, to: 18 },
    }])

    expect(after).not.toBe(before)
    expect(editorSuggestionDecorationSignature([{
      ...suggestion,
      anchor: { ...suggestion.anchor },
    }])).toBe(before)
  })

  it('leaves relative collaboration anchors pending when a decoration target is missing', () => {
    const legacySuggestion: EditorSuggestionRecord = {
      anchor: {
        from: 1,
        quoteAfter: '',
        quoteBefore: '',
        selectedText: 'target',
        to: 7,
      },
      anchorText: 'target',
      blockId: 'block-1',
      createdAt: '2026-07-31T00:00:00.000Z',
      documentId: 'document-1',
      editPosition: 'replace',
      groupId: 'group-1',
      id: 'editor-suggestion-transient',
      originalText: 'target',
      origin: { kind: 'global_run' },
      proposedText: 'proposal',
      revision: 1,
      status: 'pending',
      updatedAt: '2026-07-31T00:00:00.000Z',
    }
    const relativeSuggestion: EditorSuggestionRecord = {
      ...legacySuggestion,
      anchor: {
        ...legacySuggestion.anchor,
        relativeFrom: 'relative-from',
        relativeTo: 'relative-to',
        relativeVersion: EDITOR_SCHEMA_BEHAVIOR_INPUTS.relativePositions,
      } as EditorSuggestionRecord['anchor'],
    }

    expect(editorSuggestionShouldAutoStaleOnMissingTarget(legacySuggestion)).toBe(true)
    expect(editorSuggestionShouldAutoStaleOnMissingTarget(relativeSuggestion)).toBe(false)
  })
})

describe('MarkdownEditorSurface lifecycle policy', () => {
  it('preserves the complete legacy markdown lifecycle', () => {
    expect(editorSurfaceLifecyclePolicy({
      collaborationCanEdit: false,
      collaborationMode: false,
      collaborationReady: false,
      mode: 'source',
    })).toEqual({
      applyExternalContent: true,
      editable: false,
      emitFullBodyChanges: true,
      renderSourceEditor: true,
    })
  })

  it('never applies or emits full markdown bodies after collaboration join', () => {
    expect(editorSurfaceLifecyclePolicy({
      collaborationCanEdit: true,
      collaborationMode: true,
      collaborationReady: true,
      mode: 'live',
    })).toEqual({
      applyExternalContent: false,
      editable: true,
      emitFullBodyChanges: false,
      renderSourceEditor: false,
    })
  })

  it('keeps collaboration read-only until lifecycle sync is complete', () => {
    expect(editorSurfaceLifecyclePolicy({
      collaborationCanEdit: true,
      collaborationMode: true,
      collaborationReady: false,
      mode: 'live',
    }).editable).toBe(false)
  })

  it('does not treat provider presence as lifecycle readiness', () => {
    expect(isCollaborationSurfaceSynced({
      provider: {} as HocuspocusProvider,
      synced: false,
    } as never)).toBe(false)
    expect(isCollaborationSurfaceSynced({
      provider: {} as HocuspocusProvider,
      synced: true,
    } as never)).toBe(true)
  })

  it('does not bind document A collaboration under document B during a switch', () => {
    const yDocument = new Y.Doc()
    const documentB = {
      collaboration: {
        generation: 3,
        persistedSequence: 0,
        projectionSequence: 0,
        schemaVersion: 1,
      },
      contentMarkdown: '# B',
      contentMode: 'collaboration',
      createdAt: '2026-07-15T10:00:00.000Z',
      folderId: null,
      id: 'document-b',
      revision: 1,
      source: 'blank',
      title: 'B',
      updatedAt: '2026-07-15T10:00:00.000Z',
    } as const

    expect(collaborationBindingForEditorDocument(documentB, {
      canEdit: true,
      document: yDocument,
      documentId: 'document-a',
      generation: 2,
      lifecycleKey: 'document-a:g2',
      liveDocumentReady: true,
      provider: {} as HocuspocusProvider,
      synced: true,
      user: { color: '#2563EB', id: 'user-1', name: 'Ada' },
    } as never)).toBeNull()
    yDocument.destroy()
  })

  it('binds the live document from its first sync on, independent of later sync state', () => {
    const yDocument = new Y.Doc()
    const document = {
      collaboration: {
        generation: 2,
        persistedSequence: 0,
        projectionSequence: 0,
        schemaVersion: 1,
      },
      contentMarkdown: '# Projection',
      contentMode: 'collaboration',
      createdAt: '2026-07-15T10:00:00.000Z',
      folderId: null,
      id: 'document-a',
      revision: 1,
      source: 'blank',
      title: 'A',
      updatedAt: '2026-07-15T10:00:00.000Z',
    } as const
    const markLocalWriteMode = vi.fn()
    const endLocalWriteMode = vi.fn()
    const handle = (overrides: Record<string, unknown>) => ({
      access: 'edit',
      canEdit: true,
      document: yDocument,
      documentId: 'document-a',
      endLocalWriteMode,
      generation: 2,
      lifecycleKey: 'document-a:g2:7',
      liveDocumentReady: true,
      markLocalWriteMode,
      provider: {} as HocuspocusProvider,
      synced: true,
      user: { color: '#2563EB', id: 'user-1', name: 'Ada' },
      ...overrides,
    }) as never

    // Reconnecting: not synced, read-only, but the same live document, and
    // the write-mode report still reaches the controller of that document.
    const reconnecting = collaborationBindingForEditorDocument(
      document,
      handle({ canEdit: false, synced: false }),
    )
    expect(reconnecting).toMatchObject({ canEdit: false, lifecycleKey: 'document-a:g2:7' })
    expect(reconnecting?.markLocalWriteMode).toBe(markLocalWriteMode)
    expect(reconnecting?.endLocalWriteMode).toBe(endLocalWriteMode)
    // Released or revoked: the live document is no longer shown.
    expect(collaborationBindingForEditorDocument(
      document,
      handle({ liveDocumentReady: false }),
    )).toBeNull()
    // A viewer is bound as well and stays read-only.
    const viewBinding = collaborationBindingForEditorDocument(
      document,
      handle({ access: 'view', canEdit: false }),
    )
    expect(viewBinding).not.toBeNull()
    expect(editorSurfaceLifecyclePolicy({
      collaborationCanEdit: false,
      collaborationMode: true,
      collaborationReady: viewBinding !== null,
      mode: 'live',
    }).editable).toBe(false)
    yDocument.destroy()
  })

  it('admits local edits behind a read-only surface only while a bound surface reconnects', () => {
    const handle = (overrides: Record<string, unknown>) => ({
      access: 'edit',
      canEdit: false,
      connectionStatus: 'reconnecting',
      liveDocumentReady: true,
      ...overrides,
    }) as never

    expect(collaborationSurfaceAcceptsLocalEdits(handle({
      canEdit: true,
      connectionStatus: 'connected',
    }))).toBe(true)
    // Restoring the connection: the surface is the input gate.
    expect(collaborationSurfaceAcceptsLocalEdits(handle({}))).toBe(true)
    expect(collaborationSurfaceAcceptsLocalEdits(handle({ connectionStatus: 'connecting' }))).toBe(true)
    // Nothing is bound before the first sync or after a revocation.
    expect(collaborationSurfaceAcceptsLocalEdits(handle({ liveDocumentReady: false }))).toBe(false)
    // A failure, a revocation or a view-only lease is not a reconnect.
    for (const connectionStatus of ['access_revoked', 'error', 'incompatible', 'read_only']) {
      expect(collaborationSurfaceAcceptsLocalEdits(handle({ connectionStatus }))).toBe(false)
    }
  })

  it('derives the review write mode of the workspace from the reconnect-aware rule', () => {
    // The toolbar keeps following `canEdit`; only the transaction gate behind
    // the surface uses the reconnect-aware rule. A reword of these lines must
    // be caught here.
    const workspace = readFileSync(path.resolve(__dirname, '../EditorWorkspace.tsx'), 'utf-8')
    expect(workspace).toContain(
      'const collaborationAcceptsLocalEdits = collaborationSurfaceAcceptsLocalEdits(collaboration)',
    )
    expect(workspace).toMatch(
      /const reviewWriteMode = [^;]*?effectiveEditorWriteMode\(\s*collaboration\.access,\s*collaborationAcceptsLocalEdits,/,
    )
    const policy = /const collaborationEditorPolicy = useMemo[\s\S]*?\]\)/.exec(workspace)?.[0] ?? ''
    expect(policy).toContain('writeMode: reviewWriteMode,')
  })

  it('shows the last projected markdown read-only while collaboration is unavailable', () => {
    const policy = editorSurfaceLifecyclePolicy({
      collaborationCanEdit: false,
      collaborationMode: true,
      collaborationReady: false,
      mode: 'live',
    })

    expect(editorSurfaceInitialContent({
      collaborationMode: true,
      collaborationReady: false,
      contentMarkdown: '# Last durable projection',
    })).toEqual({
      content: '# Last durable projection',
      contentType: 'markdown',
    })
    expect(policy).toMatchObject({
      applyExternalContent: true,
      editable: false,
      emitFullBodyChanges: false,
    })
  })

  it('leaves content initialization to Yjs once collaboration is ready', () => {
    expect(editorSurfaceInitialContent({
      collaborationMode: true,
      collaborationReady: true,
      contentMarkdown: '# Projection must not enter Yjs',
    })).toEqual({})
  })
})

describe('MarkdownEditorSurface collaboration extensions', () => {
  it('leaves legacy extensions untouched', () => {
    const configure = vi.fn()
    const extensions = [{ configure, name: 'starterKit' }] as unknown as Extensions

    expect(configureEditorExtensionsForCollaboration(extensions, null)).toBe(extensions)
    expect(configure).not.toHaveBeenCalled()
  })

  it('disables normal undo while a collaboration provider is still joining', () => {
    const configuredStarterKit = { name: 'starterKit' }
    const configure = vi.fn(() => configuredStarterKit)
    const extensions = [{ configure, name: 'starterKit' }] as unknown as Extensions

    const configured = configureEditorExtensionsForCollaboration(
      extensions,
      null,
      true,
    )

    expect(configure).toHaveBeenCalledWith({ undoRedo: false })
    expect(configured).toEqual([configuredStarterKit])
  })

  it('disables normal undo only when Yjs collaboration extensions are attached', () => {
    const configuredStarterKit = { name: 'starterKit' }
    const configure = vi.fn(() => configuredStarterKit)
    const extensions = [
      { configure, name: 'starterKit' },
      { name: 'paragraph' },
    ] as unknown as Extensions
    const document = new Y.Doc()

    const configured = configureEditorExtensionsForCollaboration(extensions, {
      canEdit: true,
      document,
      endLocalWriteMode: () => undefined,
      lifecycleKey: 'document-1:g1',
      markLocalWriteMode: () => undefined,
      provider: {} as HocuspocusProvider,
      user: { color: '#2563EB', id: 'user-1', name: 'Ada' },
    })

    expect(configure).toHaveBeenCalledWith({ undoRedo: false })
    expect(configured.map((extension) => extension.name)).toEqual([
      'starterKit',
      'paragraph',
      'collaboration',
      'markAwareCollaborationTextDiff',
      'collaborationCaret',
    ])
    expect(configuredStarterKit).toBe(configured[0])
    document.destroy()
  })
})

const bodySchema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
// Stored as an agent writes it: not the serializer's own output.
const STORED = '| A | B |\n|---|---|\n| x | y |'

function loadedDocument(): ProseMirrorNode {
  return bodySchema.nodeFromJSON(parseEditorMarkdown(STORED))
}

/** One editor update: the root transaction and the document after it (the
 * root's result, or what an appended transaction made of it). */
function update(
  root: Transaction,
  doc: ProseMirrorNode = root.doc,
  emittable = true,
) {
  return {
    before: root.before,
    doc,
    emittable,
    rootChangedDocument: root.docChanged,
    storedMarkdown: STORED,
  }
}

/** The step-less root the editor dispatches on mount, focus or click, and
 * the document after the trailing paragraph was appended to it. */
function normalization(doc: ProseMirrorNode) {
  const root = EditorState.create({ doc, schema: bodySchema }).tr.setMeta('viewport', true)
  const appended = EditorState.create({ doc, schema: bodySchema }).tr
    .insert(doc.content.size, bodySchema.nodes.paragraph.create())
  return update(root, appended.doc)
}

function typed(doc: ProseMirrorNode, text: string) {
  return update(EditorState.create({ doc, schema: bodySchema }).tr.insertText(text, doc.content.size - 1))
}

/** A step-bearing root back to *target* (an undo). */
function reverted(doc: ProseMirrorNode, target: ProseMirrorNode) {
  return update(
    EditorState.create({ doc, schema: bodySchema }).tr.replaceWith(0, doc.content.size, target.content),
  )
}

const serializeFresh = () => 'fresh serialization'

function apply(
  state: EditorBodyChangeState,
  input: ReturnType<typeof update>,
): { emit: string | null; state: EditorBodyChangeState } {
  return editorBodyChangeDecision(state, input, serializeFresh)
}

describe('full-body changes reach the host only after a content edit', () => {
  it('folds the normalization of the loaded document into the baseline and emits nothing', () => {
    const loaded = loadedDocument()
    const opened = apply(INITIAL_EDITOR_BODY_CHANGE_STATE, normalization(loaded))

    expect(opened.emit).toBeNull()
    expect(opened.state.baseline?.markdown).toBe(STORED)
    expect(opened.state.baseline?.doc.lastChild?.type.name).toBe('paragraph')
    expect(opened.state.baseline?.doc.eq(loaded)).toBe(false)
  })

  it('emits nothing for an update with an empty transaction and an unchanged document', () => {
    const loaded = loadedDocument()
    const editable = update(EditorState.create({ doc: loaded, schema: bodySchema }).tr)

    const first = apply(INITIAL_EDITOR_BODY_CHANGE_STATE, editable)
    expect(first.emit).toBeNull()
    expect(apply(first.state, editable).emit).toBeNull()
  })

  it('folds a normalization that arrives with the first click as well', () => {
    // A later Tiptap appends the trailing paragraph on the first transaction
    // after mount, a click's selection, instead of inside the constructor.
    const loaded = loadedDocument()
    const setEditable = update(EditorState.create({ doc: loaded, schema: bodySchema }).tr)
    const afterMount = apply(INITIAL_EDITOR_BODY_CHANGE_STATE, setEditable).state
    const click = apply(afterMount, normalization(loaded))

    expect(click.emit).toBeNull()
    expect(click.state.baseline?.doc.lastChild?.type.name).toBe('paragraph')
  })

  it('emits an edit as a fresh serialization', () => {
    const opened = apply(INITIAL_EDITOR_BODY_CHANGE_STATE, normalization(loadedDocument()))
    const edit = apply(opened.state, typed(opened.state.baseline!.doc, 'x'))

    expect(edit.emit).toBe('fresh serialization')
    expect(edit.state.ownBodies).toEqual([STORED, 'fresh serialization'])
  })

  it('emits the stored bytes when an edit returns to the loaded document', () => {
    const opened = apply(INITIAL_EDITOR_BODY_CHANGE_STATE, normalization(loadedDocument()))
    const baseline = opened.state.baseline!.doc
    const edit = apply(opened.state, typed(baseline, 'x'))
    const undo = apply(edit.state, reverted(edit.state.emitted!, baseline))

    expect(undo.emit).toBe(STORED)
    expect(undo.state.ownBodies).toEqual([STORED, 'fresh serialization', STORED])
  })

  it('takes the document before the first edit as the baseline when nothing normalized it', () => {
    const loaded = loadedDocument()
    const edit = apply(INITIAL_EDITOR_BODY_CHANGE_STATE, typed(loaded, 'x'))
    const undo = apply(edit.state, reverted(edit.state.emitted!, loaded))

    expect(edit.emit).toBe('fresh serialization')
    expect(undo.emit).toBe(STORED)
  })

  it('skips an edit whose document the host already holds', () => {
    const opened = apply(INITIAL_EDITOR_BODY_CHANGE_STATE, normalization(loadedDocument()))
    const edit = apply(opened.state, typed(opened.state.baseline!.doc, 'x'))
    const same = apply(edit.state, reverted(edit.state.emitted!, edit.state.emitted!))

    expect(same.emit).toBeNull()
  })

  it('emits a step-less change after an edit only when the document moved on', () => {
    const opened = apply(INITIAL_EDITOR_BODY_CHANGE_STATE, normalization(loadedDocument()))
    const edit = apply(opened.state, typed(opened.state.baseline!.doc, 'x'))
    const emitted = edit.state.emitted!
    const unchanged = apply(
      edit.state,
      update(EditorState.create({ doc: emitted, schema: bodySchema }).tr.setMeta('decorations', true)),
    )
    const appended = apply(edit.state, normalization(emitted))

    expect(unchanged.emit).toBeNull()
    expect(appended.emit).toBe('fresh serialization')
  })

  it('holds an edit back while the host may not receive it and sends the next one', () => {
    const opened = apply(INITIAL_EDITOR_BODY_CHANGE_STATE, normalization(loadedDocument()))
    const blocked = apply(opened.state, update(
      EditorState.create({ doc: opened.state.baseline!.doc, schema: bodySchema }).tr.insertText('x', 1),
      undefined,
      false,
    ))
    const next = apply(blocked.state, typed(blocked.state.baseline!.doc, 'y'))

    expect(blocked.emit).toBeNull()
    expect(blocked.state.ownBodies).toEqual([STORED])
    expect(next.emit).toBe('fresh serialization')
  })

  it('knows every body it handed the host until the host hands one back', () => {
    expect(editorBodyFromHost(INITIAL_EDITOR_BODY_CHANGE_STATE, STORED).own).toBe(false)
    const opened = apply(INITIAL_EDITOR_BODY_CHANGE_STATE, normalization(loadedDocument()))
    expect(editorBodyFromHost(opened.state, STORED).own).toBe(true)

    const edit = editorBodyChangeDecision(
      opened.state,
      typed(opened.state.baseline!.doc, 'x'),
      () => 'first edit',
    )
    const later = editorBodyChangeDecision(
      edit.state,
      typed(edit.state.emitted!, 'y'),
      () => 'second edit',
    )
    // A save of the first edit answered after the second one: an older
    // body, still the surface's own.
    const answered = editorBodyFromHost(later.state, 'first edit')
    expect(answered.own).toBe(true)
    expect(editorBodyFromHost(later.state, STORED).own).toBe(true)
    expect(editorBodyFromHost(later.state, 'foreign body').own).toBe(false)
    expect(editorBodyFromHost(later.state, 'foreign body').state).toBe(later.state)

    // The host moved past what it handed back: older bodies are no longer
    // its own, newer ones still are.
    expect(answered.state.ownBodies).toEqual(['first edit', 'second edit'])
    expect(editorBodyFromHost(answered.state, STORED).own).toBe(false)
    expect(editorBodyFromHost(answered.state, 'second edit').own).toBe(true)
  })
})

// Source pins for wiring the DOM-less lane cannot mount; the browser lane
// (`browser-tests/editorBodyChanges.spec.ts`) drives the mounted surface.
describe('MarkdownEditorSurface body-change wiring', () => {
  const source = readFileSync(path.resolve(__dirname, './MarkdownEditorSurface.tsx'), 'utf-8')

  it('decides on the full-body policy before any update reaches the host', () => {
    // Collaborative documents persist through Yjs only: a remote Yjs
    // transaction is step-bearing and must never become a full body.
    const handler = source.slice(source.indexOf('onUpdate: ({ editor: currentEditor, transaction }) => {'))
    const firstStatement = handler.slice(handler.indexOf('\n') + 1, handler.indexOf('\n', handler.indexOf('\n') + 1))
    expect(firstStatement.trim()).toBe('if (!lifecyclePolicy.emitFullBodyChanges) return')
  })

  it('switches editability without an update', () => {
    expect(source).toContain('editor.setEditable(editable, false)')
    // No call may omit the `false` that suppresses the empty update.
    expect(source).not.toMatch(/setEditable\([^,)]*\)/)
  })
})
