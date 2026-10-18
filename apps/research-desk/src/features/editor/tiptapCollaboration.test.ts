import {
  EDITOR_YJS_FRAGMENT,
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  createEditorSchemaExtensions,
  editorJsonToYDoc,
  transformToInqtrixSuggestionTransaction,
} from '@inqtrix/editor-schema'
import { getExtensionField, getSchema } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { EditorState, Plugin, TextSelection, type Transaction } from '@tiptap/pm/state'
import { findWrapping } from '@tiptap/pm/transform'
import type { EditorView } from '@tiptap/pm/view'
import { ySyncPlugin, ySyncPluginKey, yUndoPlugin } from '@tiptap/y-tiptap'
import { afterEach, describe, expect, it, vi } from 'vitest'
import * as Y from 'yjs'

import {
  CollaborationReviewExtension,
  CollaborationSuggestionGroupingCoordinator,
  CollaborationSuggestionUndoHistory,
  collaborationReviewAllowsTransaction,
  collaborationEditRefusal,
  collaborationReviewPluginKey,
  collaborationSuggestionCollisionEvent,
  collaborationSuggestionErrorEvent,
  detectForeignSuggestionCollision,
  suggestModeDeletionRange,
} from './tiptap'

describe('collaboration transaction policy', () => {
  it('blocks local document mutations for view and comment modes', () => {
    for (const writeMode of ['view', 'comment'] as const) {
      expect(collaborationReviewAllowsTransaction({
        collaboration: true,
        docChanged: true,
        remote: false,
        writeMode,
      })).toBe(false)
    }
  })

  it('allows selection-only, remote, edit, and suggestion transactions', () => {
    expect(collaborationReviewAllowsTransaction({
      collaboration: true,
      docChanged: false,
      remote: false,
      writeMode: 'comment',
    })).toBe(true)
    expect(collaborationReviewAllowsTransaction({
      collaboration: true,
      docChanged: true,
      remote: true,
      writeMode: 'view',
    })).toBe(true)
    for (const writeMode of ['edit', 'suggest'] as const) {
      expect(collaborationReviewAllowsTransaction({
        collaboration: true,
        docChanged: true,
        remote: false,
        writeMode,
      })).toBe(true)
    }
  })

  it('admits a document change a plugin appended only to an own direct edit', () => {
    // Suggest mode: the suggestion transform judged only the dispatched
    // change; an appended one would reach the service untransformed.
    for (const appendedTo of ['own_edit', 'other'] as const) {
      expect(collaborationReviewAllowsTransaction({
        appendedTo,
        collaboration: true,
        docChanged: true,
        remote: false,
        writeMode: 'suggest',
      })).toBe(false)
    }
    // Opening a document, receiving a change or moving the selection never
    // writes.
    expect(collaborationReviewAllowsTransaction({
      appendedTo: 'other',
      collaboration: true,
      docChanged: true,
      remote: false,
      writeMode: 'edit',
    })).toBe(false)
    expect(collaborationReviewAllowsTransaction({
      appendedTo: 'own_edit',
      collaboration: true,
      docChanged: true,
      remote: false,
      writeMode: 'edit',
    })).toBe(true)
    expect(collaborationReviewAllowsTransaction({
      appendedTo: 'other',
      collaboration: false,
      docChanged: true,
      remote: false,
      writeMode: 'suggest',
    })).toBe(true)
    expect(collaborationReviewAllowsTransaction({
      appendedTo: 'own_edit',
      collaboration: true,
      docChanged: false,
      remote: false,
      writeMode: 'suggest',
    })).toBe(true)
  })
})

describe('collaboration suggest grouping', () => {
  it('reuses the exact patch id and creation time until five seconds of inactivity', () => {
    let now = 1_000
    let nextPatch = 1
    const grouping = new CollaborationSuggestionGroupingCoordinator({
      createPatchId: () => `patch-${nextPatch++}`,
      now: () => now,
    })
    grouping.observeContext({
      authorId: 'user-1',
      documentId: 'doc-1',
      writeMode: 'suggest',
    })

    const first = grouping.metadata()
    now += 4_999
    const continuous = grouping.metadata()
    now += 5_000
    const afterIdle = grouping.metadata()

    expect(continuous).toEqual(first)
    expect(afterIdle).toEqual({
      authorId: 'user-1',
      createdAt: 10_999,
      patchId: 'patch-2',
    })
  })

  it('resets on selection, mode, author, document, and remote boundaries', () => {
    let nextPatch = 1
    const grouping = new CollaborationSuggestionGroupingCoordinator({
      createPatchId: () => `patch-${nextPatch++}`,
      now: () => 100,
    })
    const context = {
      authorId: 'user-1',
      documentId: 'doc-1',
      writeMode: 'suggest' as const,
    }
    grouping.observeContext(context)
    expect(grouping.metadata().patchId).toBe('patch-1')

    grouping.reset()
    expect(grouping.metadata().patchId).toBe('patch-2')
    grouping.observeContext({ ...context, writeMode: 'edit' })
    grouping.observeContext(context)
    expect(grouping.metadata().patchId).toBe('patch-3')
    grouping.observeContext({ ...context, authorId: 'user-2' })
    expect(grouping.metadata().patchId).toBe('patch-4')
    grouping.observeContext({ ...context, documentId: 'doc-2' })
    expect(grouping.metadata().patchId).toBe('patch-5')
  })

  it('never allocates suggestion identity in comment-only mode', () => {
    const grouping = new CollaborationSuggestionGroupingCoordinator()
    grouping.observeContext({
      authorId: 'user-1',
      documentId: 'doc-1',
      writeMode: 'comment',
    })

    expect(() => grouping.metadata()).toThrowError(
      'Suggestion grouping requires an active document and author.',
    )
  })
})

describe('collaboration suggestion undo history', () => {
  it('offers only session-local open patches and serializes a pending decision', () => {
    const history = new CollaborationSuggestionUndoHistory()
    history.record('patch-1')
    history.record('patch-1')
    history.record('patch-2')
    const bothOpen = new Set(['patch-1', 'patch-2'])

    expect(history.current(bothOpen)).toBe('patch-2')
    expect(history.begin('patch-2', bothOpen)).toBe(true)
    expect(history.current(bothOpen)).toBeNull()

    history.fail('patch-2')
    expect(history.current(bothOpen)).toBe('patch-2')
  })

  it('drops an authoritatively resolved patch without exposing reloaded history', () => {
    const history = new CollaborationSuggestionUndoHistory()
    history.record('patch-1')
    history.record('patch-2')
    expect(history.begin('patch-2', new Set(['patch-1', 'patch-2']))).toBe(true)

    history.reconcile(new Set(['patch-1']))

    expect(history.current(new Set(['patch-1']))).toBe('patch-1')
    history.reconcile(new Set())
    expect(history.current(new Set())).toBeNull()
    expect(new CollaborationSuggestionUndoHistory().current(new Set(['patch-1']))).toBeNull()
  })
})

describe('foreign collaboration suggestion collision', () => {
  const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
  const initialDocument = schema.node('doc', null, [
    schema.node('paragraph', null, schema.text('Hello world')),
  ])
  const initialState = EditorState.create({ schema, doc: initialDocument })
  const foreignInsertion = initialState.tr
    .setSelection(TextSelection.create(initialState.doc, 6))
    .insertText(' shared')
  const tracked = transformToInqtrixSuggestionTransaction(
    foreignInsertion,
    initialState,
    { authorId: 'peer', createdAt: 10, patchId: 'patch-peer' },
    () => 'suggestion-peer',
  )
  const trackedState = initialState.apply(tracked)

  it('returns the authoritative patch and suggestion before an overlapping mutation', () => {
    const overlap = trackedState.tr.insertText('X', 7, 8)

    expect(detectForeignSuggestionCollision(overlap, 'local-user')).toEqual({
      patchId: 'patch-peer',
      suggestionId: 'suggestion-peer',
    })
  })

  it('allows the suggestion author and non-overlapping local edits', () => {
    const overlap = trackedState.tr.insertText('X', 7, 8)
    const separate = trackedState.tr.insertText('Y', 1, 2)

    expect(detectForeignSuggestionCollision(overlap, 'peer')).toBeNull()
    expect(detectForeignSuggestionCollision(separate, 'local-user')).toBeNull()
  })
})

describe('foreign collaboration suggestion collision through formatting and replacement gaps', () => {
  const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
  const bold = schema.marks.bold!
  const code = schema.marks.code!
  const markedDocument = schema.nodeFromJSON({
    type: 'doc',
    content: [
      {
        type: 'paragraph',
        content: [
          { type: 'text', text: 'Foreign ' },
          { type: 'text', text: 'THEIRS', marks: [{ type: 'insertion', attrs: refusalMetadata(REFUSAL_PEER, 'foreign-insertion', 'insertion') }] },
          { type: 'text', text: ' and ' },
          { type: 'text', text: 'LOUD', marks: [{ type: 'bold' }, { type: 'insertion', attrs: refusalMetadata(REFUSAL_PEER, 'foreign-bold', 'insertion') }] },
          { type: 'text', text: ' tail.' },
        ],
      },
      {
        type: 'paragraph',
        content: [
          { type: 'text', text: 'Replace ' },
          { type: 'text', text: 'OLDWORD', marks: [{ type: 'deletion', attrs: refusalMetadata(REFUSAL_PEER, 'foreign-replacement', 'replacement') }] },
          { type: 'text', text: 'NEWWORD', marks: [{ type: 'insertion', attrs: refusalMetadata(REFUSAL_PEER, 'foreign-replacement', 'replacement') }] },
          { type: 'text', text: ' here ' },
          { type: 'text', text: 'MINE', marks: [{ type: 'insertion', attrs: refusalMetadata(REFUSAL_ACTOR, 'own-insertion', 'insertion') }] },
          { type: 'text', text: ' end.' },
        ],
      },
      {
        type: 'paragraph',
        attrs: {
          [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: {
            action: 'heading2',
            authorId: REFUSAL_PEER,
            createdAt: 1_784_112_000,
            kind: 'structure',
            patchId: 'patch-foreign-structure',
            suggestionId: 'foreign-structure',
          },
        },
        content: [{ type: 'text', text: 'Proposed ZONEFOREIGNSTRUCT.' }],
      },
    ],
  })
  const state = EditorState.create({ doc: markedDocument, schema })
  const at = (needle: string, offset = 0) => refusalPosition(state.doc, needle, 'in', offset)
  const foreignInsertion = { patchId: 'patch-foreign-insertion', suggestionId: 'foreign-insertion' }

  it('collides when formatting spans another person\'s insertion', () => {
    expect(detectForeignSuggestionCollision(
      state.tr.addMark(at('Foreign'), at('THEIRS', 'THEIRS'.length), bold.create()),
      REFUSAL_ACTOR,
    )).toEqual(foreignInsertion)
  })

  it('collides when inline code spans another person\'s insertion', () => {
    expect(detectForeignSuggestionCollision(
      state.tr.addMark(at('THEIRS', 2), at('THEIRS', 'THEIRS'.length + 3), code.create()),
      REFUSAL_ACTOR,
    )).toEqual(foreignInsertion)
  })

  it('collides when formatting is removed inside another person\'s insertion', () => {
    expect(detectForeignSuggestionCollision(
      state.tr.removeMark(at('LOUD', 1), at('LOUD', 3), bold),
      REFUSAL_ACTOR,
    )).toEqual({ patchId: 'patch-foreign-bold', suggestionId: 'foreign-bold' })
  })

  it('collides when text is typed between the halves of another person\'s replacement', () => {
    expect(detectForeignSuggestionCollision(
      state.tr.insertText('x', at('NEWWORD')),
      REFUSAL_ACTOR,
    )).toEqual({ patchId: 'patch-foreign-replacement', suggestionId: 'foreign-replacement' })
  })

  it('lets formatting of the own insertion and of original text through', () => {
    expect(detectForeignSuggestionCollision(
      state.tr.addMark(at('MINE'), at('MINE', 'MINE'.length), bold.create()),
      REFUSAL_ACTOR,
    )).toBeNull()
    expect(detectForeignSuggestionCollision(
      state.tr.addMark(at('Foreign'), at('THEIRS'), bold.create()),
      REFUSAL_ACTOR,
    )).toBeNull()
  })

  it('collides when an attribute of a block with another person\'s structural suggestion changes', () => {
    const block = refusalPosition(state.doc, 'ZONEFOREIGNSTRUCT', 'start') - 1
    expect(detectForeignSuggestionCollision(
      state.tr.setNodeAttribute(block, 'textAlign', 'center'),
      REFUSAL_ACTOR,
    )).toEqual({ patchId: 'patch-foreign-structure', suggestionId: 'foreign-structure' })
  })

  it('lets an attribute change of a block that only contains another person\'s insertion through', () => {
    const block = refusalPosition(state.doc, 'THEIRS', 'start') - 1
    expect(detectForeignSuggestionCollision(
      state.tr.setNodeAttribute(block, 'textAlign', 'center'),
      REFUSAL_ACTOR,
    )).toBeNull()
  })

  it('lets typing right next to another person\'s insertion through', () => {
    expect(detectForeignSuggestionCollision(
      state.tr.insertText('x', at('THEIRS', 'THEIRS'.length)),
      REFUSAL_ACTOR,
    )).toBeNull()
    expect(detectForeignSuggestionCollision(
      state.tr.insertText('x', at('THEIRS')),
      REFUSAL_ACTOR,
    )).toBeNull()
  })
})

/** The review extension as an editor builds it, without a DOM: its storage,
 * its plugins (built once, as for one editor) and its destroy hook. */
function reviewExtension(initialMode: 'edit' | 'suggest') {
  const calls: Array<{ kind: 'end' | 'report'; mode?: string; reporter: object }> = []
  const extension = CollaborationReviewExtension.configure({
    initialPolicy: {
      collaboration: true,
      display: 'final',
      enabled: true,
      writeAuthorId: '11111111-1111-4111-8111-111111111111',
      writeMode: initialMode,
    },
    onLocalWriteMode: (mode, reporter) => {
      calls.push({ kind: 'report', mode, reporter })
    },
    onLocalWriteModeEnd: (reporter) => {
      calls.push({ kind: 'end', reporter })
    },
  })
  const storage = getExtensionField<() => Record<string, unknown>>(
    extension,
    'addStorage',
    { name: extension.name, options: extension.options },
  )()
  const context = {
    editor: {} as never,
    name: extension.name,
    options: extension.options,
    storage,
    type: null,
  }
  const plugins = getExtensionField<() => Plugin[]>(
    extension,
    'addProseMirrorPlugins',
    context,
  )()
  const schema = getSchema(createEditorSchemaExtensions())
  return {
    calls,
    destroy: () => getExtensionField<() => void>(extension, 'onDestroy', context)(),
    plugins,
    state: EditorState.create({ plugins, schema }),
  }
}

function withWriteMode(state: EditorState, writeMode: 'edit' | 'suggest'): EditorState {
  return state.apply(state.tr.setMeta(collaborationReviewPluginKey, {
    collaboration: true,
    display: 'final',
    enabled: true,
    writeMode,
  }))
}

describe('collaboration write-mode report', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('the review extension reports every change of the enforced write mode once', () => {
    const editor = reviewExtension('edit')
    let state = editor.state

    state = withWriteMode(state, 'suggest')
    // Reported while the state is applied, before any plugin view could
    // update and write into the collaboration document.
    expect(editor.calls.map((call) => call.mode)).toEqual(['edit', 'suggest'])
    state = withWriteMode(state, 'suggest')
    state = withWriteMode(state, 'edit')

    expect(editor.calls.map((call) => [call.kind, call.mode])).toEqual([
      ['report', 'edit'],
      ['report', 'suggest'],
      ['report', 'edit'],
    ])
    expect(new Set(editor.calls.map((call) => call.reporter)).size).toBe(1)
    void state
  })

  it('an editor created with a suggest policy reports suggest before any plugin view exists and ends the report only when it is destroyed', () => {
    const editor = reviewExtension('suggest')

    // The plugins exist, no plugin view was built yet.
    expect(editor.calls).toEqual([
      { kind: 'report', mode: 'suggest', reporter: editor.calls[0]!.reporter },
    ])

    editor.destroy()
    editor.destroy()

    expect(editor.calls).toHaveLength(2)
    expect(editor.calls[1]).toEqual({ kind: 'end', reporter: editor.calls[0]!.reporter })
  })

  it('registering and unregistering plugins while the editor lives keeps the write-mode report', () => {
    vi.stubGlobal('MutationObserver', class {
      disconnect() {}
      observe() {}
    })
    vi.stubGlobal('ResizeObserver', class {
      disconnect() {}
      observe() {}
    })
    vi.stubGlobal('requestAnimationFrame', () => 1)
    vi.stubGlobal('cancelAnimationFrame', () => undefined)
    vi.stubGlobal('window', {
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
    })
    const first = reviewExtension('suggest')
    const view = {
      dom: {
        addEventListener: () => undefined,
        querySelectorAll: () => [],
        removeEventListener: () => undefined,
      },
    } as unknown as EditorView
    const reviewPlugin = first.plugins.find(
      (plugin) => plugin.spec.key === collaborationReviewPluginKey,
    )!

    // Registering a plugin rebuilds every plugin view and keeps the plugin
    // state; removing one does the same.
    const pluginView = reviewPlugin.spec.view!(view)
    pluginView.destroy?.()
    const extra = new Plugin({})
    let state = first.state.reconfigure({ plugins: [...first.state.plugins, extra] })
    reviewPlugin.spec.view!(view).destroy?.()
    state = state.reconfigure({ plugins: state.plugins.filter((plugin) => plugin !== extra) })
    reviewPlugin.spec.view!(view).destroy?.()

    expect(first.calls.map((call) => call.kind)).toEqual(['report'])
    expect(collaborationReviewPluginKey.getState(state)?.writeMode).toBe('suggest')

    // A second editor reports under its own reporter; the first editor's
    // later destroy ends only its own report.
    const second = reviewExtension('edit')
    expect(second.calls[0]!.reporter).not.toBe(first.calls[0]!.reporter)
    first.destroy()
    expect(first.calls.at(-1)).toEqual({ kind: 'end', reporter: first.calls[0]!.reporter })
    expect(second.calls.map((call) => call.kind)).toEqual(['report'])
  })
})

const REFUSAL_ACTOR = '11111111-1111-4111-8111-111111111111'
const REFUSAL_PEER = '22222222-2222-4222-8222-222222222222'

function refusalMetadata(authorId: string, suggestionId: string, kind: string) {
  return {
    authorId,
    createdAt: 1_784_112_000,
    id: suggestionId,
    kind,
    patchId: `patch-${suggestionId}`,
    suggestionId,
  }
}

const REFUSAL_DOCUMENT = {
  type: 'doc',
  content: [
    { type: 'paragraph', content: [{ type: 'text', text: 'Plain ZONEPLAIN text.' }] },
    {
      type: 'paragraph',
      attrs: {
        [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: {
          action: 'heading2',
          authorId: REFUSAL_ACTOR,
          createdAt: 1_784_112_000,
          kind: 'structure',
          patchId: 'patch-structure',
          suggestionId: 'structure',
        },
      },
      content: [{ type: 'text', text: 'Structure ZONETARGET with text.' }],
    },
    {
      type: 'paragraph',
      content: [
        { type: 'text', text: 'With ' },
        { type: 'text', text: 'OWNINSERT', marks: [{ type: 'insertion', attrs: refusalMetadata(REFUSAL_ACTOR, 'own-insertion', 'insertion') }] },
        { type: 'text', text: ' end.' },
      ],
    },
    {
      type: 'paragraph',
      content: [
        { type: 'text', text: 'Foreign ' },
        { type: 'text', text: 'THEIRS', marks: [{ type: 'insertion', attrs: refusalMetadata(REFUSAL_PEER, 'foreign-insertion', 'insertion') }] },
        { type: 'text', text: ' tail.' },
      ],
    },
    {
      type: 'paragraph',
      content: [
        { type: 'text', text: 'Replace ' },
        { type: 'text', text: 'OLDWORD', marks: [{ type: 'deletion', attrs: refusalMetadata(REFUSAL_PEER, 'foreign-replacement', 'replacement') }] },
        { type: 'text', text: 'NEWWORD', marks: [{ type: 'insertion', attrs: refusalMetadata(REFUSAL_PEER, 'foreign-replacement', 'replacement') }] },
        { type: 'text', text: ' here.' },
      ],
    },
    {
      type: 'paragraph',
      content: [
        { type: 'text', text: 'Struck ' },
        { type: 'text', text: 'STRUCKOUT', marks: [{ type: 'deletion', attrs: refusalMetadata(REFUSAL_PEER, 'foreign-deletion', 'deletion') }] },
        { type: 'text', text: ' end.' },
      ],
    },
  ],
}

function refusalPosition(
  document: ProseMirrorNode,
  needle: string,
  where: 'end' | 'in' | 'start',
  offset = 0,
): number {
  let found = -1
  document.descendants((node, position) => {
    if (found >= 0) return false
    if (node.isTextblock && node.textContent.includes(needle)) {
      if (where === 'start') found = position + 1
      else if (where === 'end') found = position + 1 + node.content.size
      else found = position + 1 + node.textContent.indexOf(needle) + offset
      return false
    }
    return true
  })
  if (found < 0) throw new Error(`fixture has no text block containing ${needle}`)
  return found
}

/** A collaboration editor without a DOM: the review extension's own dispatch
 * over a view whose element collects the refusal and collision events the
 * editor emits. */
function collaborationEditor(
  writeMode: 'edit' | 'suggest',
  writeAuthorId: string | null = REFUSAL_ACTOR,
  onSuggestionUndo?: (patchId: string) => Promise<void>,
  extraPlugins: readonly Plugin[] = [],
  content: Record<string, unknown> = REFUSAL_DOCUMENT,
) {
  const extension = CollaborationReviewExtension.configure({
    onSuggestionUndo,
    initialPolicy: {
      collaboration: true,
      display: 'final',
      documentId: 'document-1',
      enabled: true,
      writeAuthorId,
      writeMode,
    },
  })
  const storage = getExtensionField<() => Record<string, unknown>>(
    extension,
    'addStorage',
    { name: extension.name, options: extension.options },
  )()
  const dom = new EventTarget()
  const refusals: unknown[] = []
  dom.addEventListener(collaborationSuggestionErrorEvent, (event) => {
    refusals.push((event as CustomEvent<unknown>).detail)
  })
  const collisions: unknown[] = []
  dom.addEventListener(collaborationSuggestionCollisionEvent, (event) => {
    const detail = (event as CustomEvent<unknown>).detail
    if (detail !== null) collisions.push(detail)
  })
  const view = { dom, state: null as unknown as EditorState }
  const context = {
    editor: { view } as never,
    name: extension.name,
    options: extension.options,
    storage,
    type: null,
  }
  const plugins = getExtensionField<() => Plugin[]>(
    extension,
    'addProseMirrorPlugins',
    context,
  )()
  const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
  view.state = EditorState.create({
    doc: schema.nodeFromJSON(content),
    plugins: [...plugins, ...extraPlugins],
    schema,
  })
  const dispatchTransaction = getExtensionField<(props: {
    next: (transaction: Transaction) => void
    transaction: Transaction
  }) => void>(extension, 'dispatchTransaction', context)
  const delivered: Transaction[] = []
  const commands = getExtensionField<() => Record<string, () => (props: {
    dispatch?: () => void
    state: EditorState
    tr: Transaction
  }) => boolean>>(extension, 'addCommands', context)()
  return {
    collisions,
    delivered,
    dispatch(transaction: Transaction) {
      dispatchTransaction({
        next: (next) => {
          delivered.push(next)
          view.state = view.state.apply(next)
        },
        transaction,
      })
    },
    refusals,
    schema,
    state: () => view.state,
    undo: () => commands.undo!()({
      dispatch: () => undefined,
      state: view.state,
      tr: view.state.tr,
    }),
  }
}

function lastRefusal(refusals: readonly unknown[]): unknown {
  return refusals.filter((refusal) => refusal !== null).at(-1) ?? null
}

describe('collaboration edit refusals', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('refuses a suggest-mode Enter in the middle of a paragraph with its reason code', () => {
    const editor = collaborationEditor('suggest')
    const position = refusalPosition(editor.state().doc, 'ZONEPLAIN', 'in', 3)

    editor.dispatch(editor.state().tr.split(position))

    expect(editor.delivered).toHaveLength(0)
    expect(lastRefusal(editor.refusals)).toEqual({
      message: 'In suggest mode a new block can only start at the end of a block.',
      reason: 'split_moves_content',
    })
  })

  it('refuses suggest-mode formatting across original text and an own insertion with a formatting reason', () => {
    const editor = collaborationEditor('suggest')
    const document = editor.state().doc
    const from = refusalPosition(document, 'OWNINSERT', 'start')
    const to = refusalPosition(document, 'OWNINSERT', 'in', 'OWNINSERT'.length)
    const italic = editor.schema.marks.italic!

    editor.dispatch(editor.state().tr.addMark(from, to, italic.create()))

    expect(editor.delivered).toHaveLength(0)
    expect(lastRefusal(editor.refusals)).toMatchObject({
      reason: 'format_overlaps_own_suggestion',
    })
  })

  it('refuses an edit-mode Enter in the middle of a block with an own structure suggestion', () => {
    const editor = collaborationEditor('edit')
    const position = refusalPosition(editor.state().doc, 'ZONETARGET', 'in', 4)

    editor.dispatch(editor.state().tr.split(position))

    expect(editor.delivered).toHaveLength(0)
    expect(lastRefusal(editor.refusals)).toMatchObject({
      reason: 'own_suggestion_edit_unsupported',
    })
  })

  it('delivers an edit-mode join that removes an own structure suggestion', () => {
    const editor = collaborationEditor('edit')
    const position = refusalPosition(editor.state().doc, 'ZONETARGET', 'start')

    editor.dispatch(editor.state().tr.join(position - 1))

    expect(editor.delivered).toHaveLength(1)
    expect(lastRefusal(editor.refusals)).toBeNull()
  })

  it('refuses a suggest-mode deletion of another person\'s whole insertion as a collision', () => {
    const editor = collaborationEditor('suggest')
    const from = refusalPosition(editor.state().doc, 'THEIRS', 'in')

    editor.dispatch(editor.state().tr.delete(from, from + 'THEIRS'.length))

    expect(editor.delivered).toHaveLength(0)
    expect(lastRefusal(editor.refusals)).toMatchObject({ reason: 'foreign_collision' })
    expect(editor.collisions).toEqual([{ patchId: 'patch-foreign-insertion', suggestionId: 'foreign-insertion' }])
  })

  it('delivers an edit-mode Enter at the end of a block with an own structure suggestion', () => {
    const editor = collaborationEditor('edit')
    const position = refusalPosition(editor.state().doc, 'ZONETARGET', 'end')
    const paragraph = editor.schema.nodes.paragraph!

    editor.dispatch(editor.state().tr.split(position, 1, [{ type: paragraph }]))

    expect(editor.delivered).toHaveLength(1)
    expect(lastRefusal(editor.refusals)).toBeNull()
  })

  it('delivers edit-mode typing in a paragraph without suggestions', () => {
    const editor = collaborationEditor('edit')
    const position = refusalPosition(editor.state().doc, 'ZONEPLAIN', 'end')

    editor.dispatch(editor.state().tr.insertText('x', position))

    expect(editor.delivered).toHaveLength(1)
    expect(lastRefusal(editor.refusals)).toBeNull()
  })

  it('refuses every local change without a verified collaborator', () => {
    const editor = collaborationEditor('edit', null)
    const position = refusalPosition(editor.state().doc, 'ZONEPLAIN', 'end')

    editor.dispatch(editor.state().tr.insertText('x', position))

    expect(editor.delivered).toHaveLength(0)
    expect(lastRefusal(editor.refusals)).toMatchObject({ reason: 'collaborator_required' })
  })

  it('names a failed withdrawal of the own last suggestion', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const editor = collaborationEditor('suggest', REFUSAL_ACTOR, async () => {
      throw new Error('The decision request failed.')
    })
    editor.dispatch(editor.state().tr.insertText(
      'x',
      refusalPosition(editor.state().doc, 'ZONEPLAIN', 'end'),
    ))
    expect(editor.delivered).toHaveLength(1)

    expect(editor.undo()).toBe(true)
    await new Promise((resolve) => setTimeout(resolve, 0))

    expect(lastRefusal(editor.refusals)).toEqual({
      message: 'The decision request failed.',
      reason: 'suggestion_undo_failed',
    })
    expect(warn).toHaveBeenCalled()
  })

  it('reports a refusal it cannot classify as unknown and says so in the log', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => undefined)
    const refusal = collaborationEditRefusal(new RangeError('Position 99 out of range'))

    expect(refusal).toEqual({ message: 'Position 99 out of range', reason: 'unknown' })
    expect(warn).toHaveBeenCalledOnce()
  })
})

/** The text runs of the block that holds a word, with their mark names. */
function runsOfBlock(document: ProseMirrorNode, word: string): Array<{ marks: string[]; text: string }> {
  const runs: Array<{ marks: string[]; text: string }> = []
  let found = false
  document.descendants((node) => {
    if (found) return false
    if (!node.isTextblock) return true
    if (!node.textContent.includes(word)) return false
    found = true
    node.forEach((child) => {
      runs.push({ marks: child.marks.map((mark) => mark.type.name), text: child.text ?? '' })
    })
    return false
  })
  return runs
}

/** The text runs of the block that holds a word, each with the suggestion
 * it carries: its mark kind, id and author, or null. */
function suggestionRunsOfBlock(document: ProseMirrorNode, word: string): Array<{
  suggestion: { authorId: unknown; kind: string; suggestionId: unknown } | null
  text: string
}> {
  const runs: ReturnType<typeof suggestionRunsOfBlock> = []
  let found = false
  document.descendants((node) => {
    if (found) return false
    if (!node.isTextblock) return true
    if (!node.textContent.includes(word)) return false
    found = true
    node.forEach((child) => {
      const mark = child.marks.find((candidate) => (
        ['deletion', 'insertion', 'modification'].includes(candidate.type.name)
      ))
      runs.push({
        suggestion: mark
          ? { authorId: mark.attrs.authorId, kind: mark.type.name, suggestionId: mark.attrs.suggestionId }
          : null,
        text: child.text ?? '',
      })
    })
    return false
  })
  return runs
}

/** The collaboration binding's plugin over a shared copy of a document, as
 * the collaboration extension installs it. */
function bindingOver(content: Record<string, unknown>): { document: Y.Doc; plugin: Plugin } {
  const shared = editorJsonToYDoc(content as never)
  return {
    document: shared,
    plugin: ySyncPlugin(shared.getXmlFragment(EDITOR_YJS_FRAGMENT)) as Plugin,
  }
}

const THEIRS = { authorId: REFUSAL_PEER, kind: 'insertion', suggestionId: 'foreign-insertion' }
const STRUCK = { authorId: REFUSAL_PEER, kind: 'deletion', suggestionId: 'foreign-deletion' }
const OWN = { authorId: REFUSAL_ACTOR, kind: 'insertion', suggestionId: 'own-insertion' }

/** No refusal line and nothing selected for review: what normal work in
 * edit mode shows. */
function expectNothingShown(editor: ReturnType<typeof collaborationEditor>): void {
  expect(lastRefusal(editor.refusals)).toBeNull()
  expect(editor.collisions).toEqual([])
}

describe('edit mode and pending suggestions', () => {
  // A writer with edit rights works on text that holds pending suggestions
  // like on any other text. A suggestion keeps the characters that are
  // left, under its author; formatting applies to it; text typed or pasted
  // into it is the writer's own; and one whose proposed characters are all
  // deleted is removed. Nothing of this shows a line or selects anything.
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('delivers the deletion of part of another person\'s insertion, which keeps its id and author for what is left', () => {
    const editor = collaborationEditor('edit')
    const from = refusalPosition(editor.state().doc, 'THEIRS', 'in', 2)

    editor.dispatch(editor.state().tr.delete(from, from + 2))

    expect(editor.delivered).toHaveLength(1)
    expect(suggestionRunsOfBlock(editor.state().doc, 'Foreign')).toEqual([
      { suggestion: null, text: 'Foreign ' },
      { suggestion: THEIRS, text: 'THRS' },
      { suggestion: null, text: ' tail.' },
    ])
    expectNothingShown(editor)
  })

  it('delivers the deletion of another person\'s whole insertion, which removes it', () => {
    const editor = collaborationEditor('edit')
    const from = refusalPosition(editor.state().doc, 'THEIRS', 'in')

    editor.dispatch(editor.state().tr.delete(from, from + 'THEIRS'.length))

    expect(editor.delivered).toHaveLength(1)
    expect(suggestionRunsOfBlock(editor.state().doc, 'Foreign')).toEqual([
      { suggestion: null, text: 'Foreign  tail.' },
    ])
    expectNothingShown(editor)
  })

  it('delivers the deletion of a sentence that holds the writer\'s own insertion, which removes it', () => {
    const editor = collaborationEditor('edit')
    const from = refusalPosition(editor.state().doc, 'With', 'start')
    const to = refusalPosition(editor.state().doc, 'With', 'end')

    editor.dispatch(editor.state().tr.delete(from, to))

    expect(editor.delivered).toHaveLength(1)
    expect(editor.state().doc.textContent).not.toContain('OWNINSERT')
    expectNothingShown(editor)
  })

  it('delivers bold across another person\'s insertion, which stays pending under its author, bold', () => {
    const editor = collaborationEditor('edit')
    const document = editor.state().doc
    const from = refusalPosition(document, 'Foreign', 'start')
    const to = refusalPosition(document, 'Foreign', 'end')

    editor.dispatch(editor.state().tr.addMark(from, to, editor.schema.marks.bold!.create()))

    expect(editor.delivered).toHaveLength(1)
    expect(runsOfBlock(editor.state().doc, 'THEIRS')).toEqual([
      { marks: ['bold'], text: 'Foreign ' },
      { marks: ['bold', 'insertion'], text: 'THEIRS' },
      { marks: ['bold'], text: ' tail.' },
    ])
    expect(suggestionRunsOfBlock(editor.state().doc, 'THEIRS')[1]).toEqual({ suggestion: THEIRS, text: 'THEIRS' })
    expectNothingShown(editor)
  })

  it('keeps inline code off the characters of pending suggestions, another person\'s and the writer\'s own, and formats the rest', () => {
    const editor = collaborationEditor('edit')
    const code = editor.schema.marks.code!
    const document = editor.state().doc

    editor.dispatch(editor.state().tr
      .addMark(refusalPosition(document, 'Foreign', 'start'), refusalPosition(document, 'Foreign', 'end'), code.create())
      .addMark(refusalPosition(document, 'With', 'start'), refusalPosition(document, 'With', 'end'), code.create()))

    expect(editor.delivered).toHaveLength(1)
    expect(runsOfBlock(editor.state().doc, 'THEIRS')).toEqual([
      { marks: ['code'], text: 'Foreign ' },
      { marks: ['insertion'], text: 'THEIRS' },
      { marks: ['code'], text: ' tail.' },
    ])
    expect(runsOfBlock(editor.state().doc, 'OWNINSERT')).toEqual([
      { marks: ['code'], text: 'With ' },
      { marks: ['insertion'], text: 'OWNINSERT' },
      { marks: ['code'], text: ' end.' },
    ])
    expect(suggestionRunsOfBlock(editor.state().doc, 'THEIRS')[1]!.suggestion).toEqual(THEIRS)
    expect(suggestionRunsOfBlock(editor.state().doc, 'OWNINSERT')[1]!.suggestion).toEqual(OWN)
    expectNothingShown(editor)
  })

  it('leaves another person\'s insertion and its bold as they were when inline code covers nothing else, without a word', () => {
    const content = {
      type: 'doc',
      content: [{
        type: 'paragraph',
        content: [
          { type: 'text', text: 'Code ' },
          { type: 'text', text: 'BOLDTHEIRS', marks: [{ type: 'bold' }, { type: 'insertion', attrs: refusalMetadata(REFUSAL_PEER, 'foreign-bold', 'insertion') }] },
          { type: 'text', text: ' tail.' },
        ],
      }],
    }
    const editor = collaborationEditor('edit', REFUSAL_ACTOR, undefined, [], content)
    const before = editor.state().doc
    const from = refusalPosition(before, 'BOLDTHEIRS', 'in')

    editor.dispatch(editor.state().tr.addMark(from, from + 'BOLDTHEIRS'.length, editor.schema.marks.code!.create()))

    expect(editor.state().doc.eq(before)).toBe(true)
    expectNothingShown(editor)
  })

  it('types plain text inside another person\'s insertion, which keeps one id over both of its parts', () => {
    const editor = collaborationEditor('edit')
    const position = refusalPosition(editor.state().doc, 'THEIRS', 'in', 3)

    // Typing inside a text node takes that node's marks, as the browser's
    // input does.
    editor.dispatch(editor.state().tr.insertText('x', position))

    expect(editor.delivered).toHaveLength(1)
    expect(suggestionRunsOfBlock(editor.state().doc, 'Foreign')).toEqual([
      { suggestion: null, text: 'Foreign ' },
      { suggestion: THEIRS, text: 'THE' },
      { suggestion: null, text: 'x' },
      { suggestion: THEIRS, text: 'IRS' },
      { suggestion: null, text: ' tail.' },
    ])
    expectNothingShown(editor)
  })

  it('types plain text over part of another person\'s insertion without selecting it for review', () => {
    const editor = collaborationEditor('edit')
    const position = refusalPosition(editor.state().doc, 'THEIRS', 'in', 2)

    editor.dispatch(editor.state().tr.insertText('x', position, position + 1))

    expect(editor.delivered).toHaveLength(1)
    expect(suggestionRunsOfBlock(editor.state().doc, 'Foreign')).toEqual([
      { suggestion: null, text: 'Foreign ' },
      { suggestion: THEIRS, text: 'TH' },
      { suggestion: null, text: 'x' },
      { suggestion: THEIRS, text: 'IRS' },
      { suggestion: null, text: ' tail.' },
    ])
    expectNothingShown(editor)
  })

  it('types plain text inside another person\'s proposed deletion, which keeps one id over both of its parts', () => {
    const editor = collaborationEditor('edit')
    const position = refusalPosition(editor.state().doc, 'STRUCKOUT', 'in', 6)

    editor.dispatch(editor.state().tr.insertText('x', position))

    expect(editor.delivered).toHaveLength(1)
    expect(suggestionRunsOfBlock(editor.state().doc, 'Struck')).toEqual([
      { suggestion: null, text: 'Struck ' },
      { suggestion: STRUCK, text: 'STRUCK' },
      { suggestion: null, text: 'x' },
      { suggestion: STRUCK, text: 'OUT' },
      { suggestion: null, text: ' end.' },
    ])
    expectNothingShown(editor)
  })

  it('extends the writer\'s own insertion with text typed inside it', () => {
    const editor = collaborationEditor('edit')
    const position = refusalPosition(editor.state().doc, 'OWNINSERT', 'in', 3)

    editor.dispatch(editor.state().tr.insertText('x', position))

    expect(editor.delivered).toHaveLength(1)
    expect(suggestionRunsOfBlock(editor.state().doc, 'With')).toEqual([
      { suggestion: null, text: 'With ' },
      { suggestion: OWN, text: 'OWNxINSERT' },
      { suggestion: null, text: ' end.' },
    ])
    expectNothingShown(editor)
  })

  it('pastes text copied from another person\'s insertion as the writer\'s own, and leaves the insertion as it was', () => {
    const editor = collaborationEditor('edit')
    const document = editor.state().doc
    const from = refusalPosition(document, 'THEIRS', 'in')
    const copied = document.slice(from, from + 'THEIRS'.length)

    editor.dispatch(editor.state().tr.replace(refusalPosition(document, 'ZONEPLAIN', 'end'), undefined, copied))

    expect(editor.delivered).toHaveLength(1)
    expect(suggestionRunsOfBlock(editor.state().doc, 'ZONEPLAIN')).toEqual([
      { suggestion: null, text: 'Plain ZONEPLAIN text.THEIRS' },
    ])
    expect(suggestionRunsOfBlock(editor.state().doc, 'Foreign')[1]).toEqual({ suggestion: THEIRS, text: 'THEIRS' })
    expectNothingShown(editor)
  })

  it('delivers a heading of a paragraph that holds another person\'s insertion, which stays pending', () => {
    const binding = bindingOver(REFUSAL_DOCUMENT)
    try {
      const editor = collaborationEditor('edit', REFUSAL_ACTOR, undefined, [binding.plugin])
      const at = refusalPosition(editor.state().doc, 'THEIRS', 'in')

      editor.dispatch(editor.state().tr.setBlockType(at, at, editor.schema.nodes.heading!, { level: 2 }))

      expect(editor.delivered).toHaveLength(1)
      expect(suggestionRunsOfBlock(editor.state().doc, 'Foreign')[1]).toEqual({ suggestion: THEIRS, text: 'THEIRS' })
      expectNothingShown(editor)
    } finally {
      binding.document.destroy()
    }
  })

  it('keeps the writer\'s undo history after an edit that removed a suggestion', () => {
    const binding = bindingOver(REFUSAL_DOCUMENT)
    try {
      const fragment = binding.document.getXmlFragment(EDITOR_YJS_FRAGMENT)
      const undoManager = new Y.UndoManager(fragment)
      // An earlier edit of the writer's own that undo can take back.
      fragment.insert(fragment.length, [new Y.XmlElement('paragraph')])
      expect(undoManager.undoStack).toHaveLength(1)
      const editor = collaborationEditor('edit', REFUSAL_ACTOR, undefined, [
        binding.plugin,
        yUndoPlugin({ undoManager }) as Plugin,
      ])
      const from = refusalPosition(editor.state().doc, 'THEIRS', 'in')

      editor.dispatch(editor.state().tr.delete(from, from + 'THEIRS'.length))

      expect(editor.delivered).toHaveLength(1)
      expect(undoManager.undoStack).toHaveLength(1)
      expectNothingShown(editor)
    } finally {
      binding.document.destroy()
    }
  })

  describe('moving and changing a block that carries another person\'s structure suggestion', () => {
    // A block moved, copied or pasted is content the edit inserts, and
    // inserted content is the writer's own: it never carries another
    // person's suggestion. A structure suggestion stands on its block, so
    // it goes with the block the edit deletes and stays on a block the edit
    // keeps.
    const FOREIGN_STRUCTURE = {
      action: 'heading2',
      authorId: REFUSAL_PEER,
      createdAt: 1_784_112_000,
      kind: 'structure',
      patchId: 'patch-foreign-structure',
      suggestionId: 'foreign-structure',
    }
    const STRUCTURE_DOCUMENT = {
      type: 'doc',
      content: [
        { type: 'paragraph', content: [{ type: 'text', text: 'First ZONEFIRST text.' }] },
        {
          type: 'paragraph',
          attrs: { [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: FOREIGN_STRUCTURE },
          content: [{ type: 'text', text: 'Proposed ZONEFOREIGNSTRUCT.' }],
        },
        { type: 'paragraph', content: [{ type: 'text', text: 'Last ZONELAST text.' }] },
      ],
    }

    /** Each block's text with the id of the structure suggestion it carries. */
    function blocks(document: ProseMirrorNode): Array<[string, unknown]> {
      const found: Array<[string, unknown]> = []
      document.forEach((node) => {
        const data = node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR] as { suggestionId?: unknown } | null
        found.push([node.textContent, data?.suggestionId ?? null])
      })
      return found
    }

    function blockAt(document: ProseMirrorNode, needle: string): { from: number; node: ProseMirrorNode; to: number } {
      const from = refusalPosition(document, needle, 'start') - 1
      const node = document.nodeAt(from)!
      return { from, node, to: from + node.nodeSize }
    }

    it.each([
      ['up', (state: EditorState) => {
        // The block handle's "move up": delete the block, insert it in front
        // of the block before it.
        const { from, node, to } = blockAt(state.doc, 'ZONEFOREIGNSTRUCT')
        const before = state.doc.resolve(from).nodeBefore!
        return state.tr.delete(from, to).insert(from - before.nodeSize, node)
      }, ['Proposed ZONEFOREIGNSTRUCT.', 'First ZONEFIRST text.', 'Last ZONELAST text.']],
      ['down', (state: EditorState) => {
        const { from, node, to } = blockAt(state.doc, 'ZONEFOREIGNSTRUCT')
        const after = state.doc.resolve(to).nodeAfter!
        return state.tr.delete(from, to).insert(from + after.nodeSize, node)
      }, ['First ZONEFIRST text.', 'Last ZONELAST text.', 'Proposed ZONEFOREIGNSTRUCT.']],
      ['by dragging it below the last block', (state: EditorState) => {
        // A drop moves the dragged slice: the selection is deleted and the
        // slice is placed at the drop position.
        const { from, to } = blockAt(state.doc, 'ZONEFOREIGNSTRUCT')
        const slice = state.doc.slice(from, to)
        const transaction = state.tr.delete(from, to)
        const drop = transaction.doc.content.size
        return transaction.replace(drop, drop, slice)
      }, ['First ZONEFIRST text.', 'Last ZONELAST text.', 'Proposed ZONEFOREIGNSTRUCT.']],
    ] as const)('delivers a move %s, which removes the suggestion instead of moving it', (_name, move, order) => {
      const editor = collaborationEditor('edit', REFUSAL_ACTOR, undefined, [], STRUCTURE_DOCUMENT)

      editor.dispatch(move(editor.state()))

      expect(editor.delivered).toHaveLength(1)
      expect(blocks(editor.state().doc)).toEqual(order.map((text) => [text, null]))
      expectNothingShown(editor)
    })

    it('delivers a copy of the block, and the suggestion stays on the original only', () => {
      const editor = collaborationEditor('edit', REFUSAL_ACTOR, undefined, [], STRUCTURE_DOCUMENT)
      const { node, to } = blockAt(editor.state().doc, 'ZONEFOREIGNSTRUCT')

      // The block handle's "duplicate".
      editor.dispatch(editor.state().tr.insert(to, node))

      expect(editor.delivered).toHaveLength(1)
      expect(blocks(editor.state().doc)).toEqual([
        ['First ZONEFIRST text.', null],
        ['Proposed ZONEFOREIGNSTRUCT.', 'foreign-structure'],
        ['Proposed ZONEFOREIGNSTRUCT.', null],
        ['Last ZONELAST text.', null],
      ])
      expectNothingShown(editor)
    })

    it('delivers a heading of the block made the way the editor\'s command makes it, which removes the suggestion', () => {
      const editor = collaborationEditor('edit', REFUSAL_ACTOR, undefined, [], STRUCTURE_DOCUMENT)
      const { from, node } = blockAt(editor.state().doc, 'ZONEFOREIGNSTRUCT')

      // The editor's block command keeps the block's attributes on the new
      // block, the structure suggestion included.
      editor.dispatch(editor.state().tr.setBlockType(from + 1, from + 1, editor.schema.nodes.heading!, {
        ...node.attrs,
        level: 1,
      }))

      expect(editor.delivered).toHaveLength(1)
      expect(editor.state().doc.child(1).type.name).toBe('heading')
      expect(blocks(editor.state().doc)).toEqual([
        ['First ZONEFIRST text.', null],
        ['Proposed ZONEFOREIGNSTRUCT.', null],
        ['Last ZONELAST text.', null],
      ])
      expectNothingShown(editor)
    })

    it('delivers text typed and pasted inside the block, which keeps the suggestion pending on it', () => {
      const editor = collaborationEditor('edit', REFUSAL_ACTOR, undefined, [], STRUCTURE_DOCUMENT)
      const at = refusalPosition(editor.state().doc, 'ZONEFOREIGNSTRUCT', 'in')

      editor.dispatch(editor.state().tr.insertText('typed ', at))
      const pasted = editor.state().doc.slice(
        refusalPosition(editor.state().doc, 'ZONEFIRST', 'start'),
        refusalPosition(editor.state().doc, 'ZONEFIRST', 'end'),
      )
      editor.dispatch(editor.state().tr.replace(at, at, pasted))

      expect(editor.delivered).toHaveLength(2)
      expect(blocks(editor.state().doc)[1]).toEqual([
        'Proposed First ZONEFIRST text.typed ZONEFOREIGNSTRUCT.',
        'foreign-structure',
      ])
      expectNothingShown(editor)
    })

    it('delivers bold inside the block, which keeps the suggestion pending on it', () => {
      const editor = collaborationEditor('edit', REFUSAL_ACTOR, undefined, [], STRUCTURE_DOCUMENT)
      const at = refusalPosition(editor.state().doc, 'ZONEFOREIGNSTRUCT', 'in')

      editor.dispatch(editor.state().tr.addMark(at, at + 4, editor.schema.marks.bold!.create()))

      expect(editor.delivered).toHaveLength(1)
      expect(blocks(editor.state().doc)[1]).toEqual(['Proposed ZONEFOREIGNSTRUCT.', 'foreign-structure'])
      expectNothingShown(editor)
    })
  })

  describe('a structure suggestion the edit leaves impossible to apply', () => {
    // A block proposed as a heading cannot become one inside a list item,
    // and a block proposed as a code block cannot hold formatting. Such a
    // suggestion cannot stay pending in its new place; the edit removes it,
    // the way a rejection removes it, another person's and the writer's own
    // alike, and nothing is shown. One that can still be applied stays, and
    // so does every other proposal.
    function proposal(authorId: string, action: string): Record<string, unknown> {
      return {
        type: 'doc',
        content: [
          { type: 'paragraph', content: [{ type: 'text', text: 'First ZONEFIRST text.' }] },
          {
            type: 'paragraph',
            attrs: {
              [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: {
                action,
                authorId,
                createdAt: 1_784_112_000,
                kind: 'structure',
                patchId: 'patch-proposed-block',
                suggestionId: 'proposed-block',
              },
            },
            content: [{ type: 'text', text: 'Proposed ZONEPROPOSED text.' }],
          },
          {
            type: 'paragraph',
            attrs: {
              [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: {
                action: 'heading3',
                authorId: REFUSAL_PEER,
                createdAt: 1_784_112_000,
                kind: 'structure',
                patchId: 'patch-other-block',
                suggestionId: 'other-block',
              },
            },
            content: [{ type: 'text', text: 'Other ZONEOTHER text.' }],
          },
        ],
      }
    }

    /** The ids of the structure suggestions on the document's blocks. */
    function structureIds(document: ProseMirrorNode): unknown[] {
      const ids: unknown[] = []
      document.descendants((node) => {
        const data = node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR] as { suggestionId?: unknown } | null
        if (data) ids.push(data.suggestionId)
        return !node.isTextblock
      })
      return ids
    }

    const wrapInto = (editor: ReturnType<typeof collaborationEditor>, type: 'blockquote' | 'bulletList'): Transaction => {
      const state = editor.state()
      const range = state.doc.resolve(refusalPosition(state.doc, 'ZONEPROPOSED', 'in')).blockRange()!
      return state.tr.wrap(range, findWrapping(range, editor.schema.nodes[type]!)!)
    }

    it.each([
      ['another person\'s', REFUSAL_PEER],
      ['the writer\'s own', REFUSAL_ACTOR],
    ] as const)('delivers a list around a block %s heading proposal stands on, which removes the proposal', (_author, author) => {
      const editor = collaborationEditor('edit', REFUSAL_ACTOR, undefined, [], proposal(author, 'heading2'))

      editor.dispatch(wrapInto(editor, 'bulletList'))

      expect(editor.delivered).toHaveLength(1)
      expect(editor.state().doc.child(1).type.name).toBe('bulletList')
      expect(editor.state().doc.child(1).textContent).toBe('Proposed ZONEPROPOSED text.')
      expect(structureIds(editor.state().doc)).toEqual(['other-block'])
      expectNothingShown(editor)
    })

    it('delivers bold inside a block another person proposed as a code block, which removes the proposal', () => {
      const editor = collaborationEditor('edit', REFUSAL_ACTOR, undefined, [], proposal(REFUSAL_PEER, 'codeBlock'))
      const at = refusalPosition(editor.state().doc, 'ZONEPROPOSED', 'in')

      editor.dispatch(editor.state().tr.addMark(at, at + 4, editor.schema.marks.bold!.create()))

      expect(editor.delivered).toHaveLength(1)
      expect(runsOfBlock(editor.state().doc, 'ZONEPROPOSED')).toEqual([
        { marks: [], text: 'Proposed ' },
        { marks: ['bold'], text: 'ZONE' },
        { marks: [], text: 'PROPOSED text.' },
      ])
      expect(structureIds(editor.state().doc)).toEqual(['other-block'])
      expectNothingShown(editor)
    })

    it('delivers a quote around a block another person proposed as a heading, which keeps the proposal pending on it', () => {
      const editor = collaborationEditor('edit', REFUSAL_ACTOR, undefined, [], proposal(REFUSAL_PEER, 'heading2'))

      editor.dispatch(wrapInto(editor, 'blockquote'))

      expect(editor.delivered).toHaveLength(1)
      expect(editor.state().doc.child(1).type.name).toBe('blockquote')
      expect(structureIds(editor.state().doc)).toEqual(['proposed-block', 'other-block'])
      expectNothingShown(editor)
    })
  })

  describe('a replacement, which is one proposed deletion directly followed by one proposed insertion', () => {
    // Text typed between or inside its halves, or one half deleted
    // completely, leaves it in a form no pending suggestion may take. The
    // edit then removes it as a whole, the way a rejection removes it: the
    // text it proposed goes, the text it proposed to delete stays, and
    // nothing is shown. What the edit itself wrote stays.
    const OWN_REPLACEMENT_DOCUMENT = {
      type: 'doc',
      content: [{
        type: 'paragraph',
        content: [
          { type: 'text', text: 'Mine ' },
          { type: 'text', text: 'MYOLD', marks: [{ type: 'deletion', attrs: refusalMetadata(REFUSAL_ACTOR, 'own-replacement', 'replacement') }] },
          { type: 'text', text: 'MYNEW', marks: [{ type: 'insertion', attrs: refusalMetadata(REFUSAL_ACTOR, 'own-replacement', 'replacement') }] },
          { type: 'text', text: ' here.' },
        ],
      }],
    }

    it('delivers text typed inside the proposed half of another person\'s replacement, which removes the replacement', () => {
      const editor = collaborationEditor('edit')
      const position = refusalPosition(editor.state().doc, 'NEWWORD', 'in', 3)

      editor.dispatch(editor.state().tr.insertText('x', position))

      expect(editor.delivered).toHaveLength(1)
      expect(suggestionRunsOfBlock(editor.state().doc, 'Replace')).toEqual([
        { suggestion: null, text: 'Replace OLDWORDx here.' },
      ])
      expectNothingShown(editor)
    })

    it('delivers text typed between the halves of another person\'s replacement, which removes the replacement', () => {
      const editor = collaborationEditor('edit')
      const position = refusalPosition(editor.state().doc, 'NEWWORD', 'in')

      editor.dispatch(editor.state().tr.insertText('x', position))

      expect(editor.delivered).toHaveLength(1)
      expect(suggestionRunsOfBlock(editor.state().doc, 'Replace')).toEqual([
        { suggestion: null, text: 'Replace OLDWORDx here.' },
      ])
      expectNothingShown(editor)
    })

    it('delivers the deletion of the text another person\'s replacement proposes, which removes the replacement', () => {
      const editor = collaborationEditor('edit')
      const from = refusalPosition(editor.state().doc, 'NEWWORD', 'in')

      editor.dispatch(editor.state().tr.delete(from, from + 'NEWWORD'.length))

      expect(editor.delivered).toHaveLength(1)
      expect(suggestionRunsOfBlock(editor.state().doc, 'Replace')).toEqual([
        { suggestion: null, text: 'Replace OLDWORD here.' },
      ])
      expectNothingShown(editor)
    })

    it('delivers the deletion of the text the writer\'s own replacement proposes, which withdraws the replacement', () => {
      const editor = collaborationEditor('edit', REFUSAL_ACTOR, undefined, [], OWN_REPLACEMENT_DOCUMENT)
      const from = refusalPosition(editor.state().doc, 'MYNEW', 'in')

      editor.dispatch(editor.state().tr.delete(from, from + 'MYNEW'.length))

      expect(editor.delivered).toHaveLength(1)
      expect(suggestionRunsOfBlock(editor.state().doc, 'Mine')).toEqual([
        { suggestion: null, text: 'Mine MYOLD here.' },
      ])
      expectNothingShown(editor)
    })

    it('delivers the deletion of part of the text another person\'s replacement proposes, which keeps the replacement pending', () => {
      const editor = collaborationEditor('edit')
      const from = refusalPosition(editor.state().doc, 'NEWWORD', 'in', 3)

      editor.dispatch(editor.state().tr.delete(from, from + 2))

      expect(editor.delivered).toHaveLength(1)
      const replacement = { authorId: REFUSAL_PEER, suggestionId: 'foreign-replacement' }
      expect(suggestionRunsOfBlock(editor.state().doc, 'Replace')).toEqual([
        { suggestion: null, text: 'Replace ' },
        { suggestion: { ...replacement, kind: 'deletion' }, text: 'OLDWORD' },
        { suggestion: { ...replacement, kind: 'insertion' }, text: 'NEWRD' },
        { suggestion: null, text: ' here.' },
      ])
      expectNothingShown(editor)
    })
  })
})

/** Like the editor's trailing-node plugin: after any transaction it appends
 * an empty paragraph when the document does not end in one. */
function appendsTrailingParagraph(): Plugin {
  return new Plugin({
    appendTransaction(_transactions, _before, state) {
      if (state.doc.lastChild?.type.name === 'paragraph' && state.doc.lastChild.content.size === 0) {
        return null
      }
      return state.tr.insert(state.doc.content.size, state.schema.nodes.paragraph!.create())
    },
  })
}

function endsInEmptyParagraph(state: EditorState): boolean {
  return state.doc.lastChild?.type.name === 'paragraph' && state.doc.lastChild.content.size === 0
}

describe('document changes plugins append to a dispatched change', () => {
  it('are dropped in suggest mode, where only the suggestion transform may change the document', () => {
    const editor = collaborationEditor('suggest', REFUSAL_ACTOR, undefined, [appendsTrailingParagraph()])
    const position = refusalPosition(editor.state().doc, 'ZONEPLAIN', 'end')
    editor.dispatch(editor.state().tr.insertText('MARKE1', position))

    expect(editor.state().doc.textContent).toContain('MARKE1')
    expect(endsInEmptyParagraph(editor.state())).toBe(false)
  })

  it('are dropped behind a received change in every write mode, so that opening or receiving never writes', () => {
    for (const writeMode of ['edit', 'suggest'] as const) {
      const editor = collaborationEditor(writeMode, REFUSAL_ACTOR, undefined, [appendsTrailingParagraph()])
      const received = editor.state().tr
        .insertText('MARKE2', refusalPosition(editor.state().doc, 'ZONEPLAIN', 'end'))
        .setMeta(ySyncPluginKey, { isChangeOrigin: true })
      editor.dispatch(received)

      expect(editor.state().doc.textContent, writeMode).toContain('MARKE2')
      expect(endsInEmptyParagraph(editor.state()), writeMode).toBe(false)
    }
  })

  it('are dropped behind a change that only moves the selection, in every write mode', () => {
    for (const writeMode of ['edit', 'suggest'] as const) {
      const editor = collaborationEditor(writeMode, REFUSAL_ACTOR, undefined, [appendsTrailingParagraph()])
      // A plugin whose state wants a trailing paragraph appends on any later
      // transaction, also one that changes nothing, as the trailing-node
      // plugin does once a received change ended the document in a list.
      const received = editor.state().tr
        .insertText('MARKE4', refusalPosition(editor.state().doc, 'ZONEPLAIN', 'end'))
        .setMeta(ySyncPluginKey, { isChangeOrigin: true })
      editor.dispatch(received)
      editor.dispatch(editor.state().tr.setSelection(
        TextSelection.create(editor.state().doc, refusalPosition(editor.state().doc, 'ZONEPLAIN', 'start')),
      ))

      expect(editor.state().doc.textContent, writeMode).toContain('MARKE4')
      expect(endsInEmptyParagraph(editor.state()), writeMode).toBe(false)
    }
  })

  it('stay behind an own direct edit in edit mode', () => {
    const editor = collaborationEditor('edit', REFUSAL_ACTOR, undefined, [appendsTrailingParagraph()])
    editor.dispatch(editor.state().tr.insertText('MARKE3', refusalPosition(editor.state().doc, 'ZONEPLAIN', 'end')))

    expect(editor.state().doc.textContent).toContain('MARKE3')
    expect(endsInEmptyParagraph(editor.state())).toBe(true)
  })
})

describe('suggest-mode deletion at the caret', () => {
  const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
  const deletion = (id: string) => ({
    type: 'deletion',
    attrs: refusalMetadata(REFUSAL_ACTOR, id, 'deletion'),
  })
  const own = { type: 'insertion', attrs: refusalMetadata(REFUSAL_ACTOR, 'own', 'insertion') }
  function stateWith(content: unknown[], caret: number): EditorState {
    const doc = schema.nodeFromJSON({ type: 'doc', content: [{ type: 'paragraph', content }] })
    const state = EditorState.create({ doc, schema })
    return state.apply(state.tr.setSelection(TextSelection.create(doc, caret)))
  }
  // "abcd" + "E" (suggested deletion) + " tail": positions 1..5 are "abcd",
  // 5..6 is "E", 6.. is " tail".
  const marked = [
    { type: 'text', text: 'abcd' },
    { type: 'text', text: 'E', marks: [deletion('d1')] },
    { type: 'text', text: ' tail' },
  ]

  it('removes the character before the caret, past text that already carries a deletion suggestion', () => {
    expect(suggestModeDeletionRange(stateWith(marked, 5), 'backward')).toEqual({ from: 4, to: 5 })
    expect(suggestModeDeletionRange(stateWith(marked, 6), 'backward')).toEqual({ from: 4, to: 5 })
  })

  it('removes the character after the caret, past text that already carries a deletion suggestion', () => {
    expect(suggestModeDeletionRange(stateWith(marked, 5), 'forward')).toEqual({ from: 6, to: 7 })
    expect(suggestModeDeletionRange(stateWith(marked, 4), 'forward')).toEqual({ from: 4, to: 5 })
  })

  it('keeps a grapheme cluster whole', () => {
    const emoji = '\u{1F44D}\u{1F3FD}'
    const content = [{ type: 'text', text: `ab${emoji}c` }]
    expect(suggestModeDeletionRange(stateWith(content, 3 + emoji.length), 'backward'))
      .toEqual({ from: 3, to: 3 + emoji.length })
    expect(suggestModeDeletionRange(stateWith(content, 3), 'forward'))
      .toEqual({ from: 3, to: 3 + emoji.length })
  })

  it("removes the writer's own suggested text like any other text", () => {
    const content = [{ type: 'text', text: 'ab' }, { type: 'text', text: 'XY', marks: [own] }]
    expect(suggestModeDeletionRange(stateWith(content, 5), 'backward')).toEqual({ from: 4, to: 5 })
  })

  it('leaves the block edges, inline elements and selections to the default handling', () => {
    expect(suggestModeDeletionRange(stateWith(marked, 1), 'backward')).toBeNull()
    expect(suggestModeDeletionRange(stateWith(marked, 11), 'forward')).toBeNull()
    const allDeleted = [{ type: 'text', text: 'ab', marks: [deletion('d2')] }, { type: 'text', text: 'c' }]
    expect(suggestModeDeletionRange(stateWith(allDeleted, 3), 'backward')).toBeNull()
    const withBreak = [{ type: 'text', text: 'ab' }, { type: 'hardBreak' }, { type: 'text', text: 'cd' }]
    expect(suggestModeDeletionRange(stateWith(withBreak, 4), 'backward')).toBeNull()
    expect(suggestModeDeletionRange(stateWith(withBreak, 3), 'forward')).toBeNull()
    const state = stateWith(marked, 2)
    const selected = state.apply(state.tr.setSelection(TextSelection.create(state.doc, 2, 4)))
    expect(suggestModeDeletionRange(selected, 'backward')).toBeNull()
  })
})
