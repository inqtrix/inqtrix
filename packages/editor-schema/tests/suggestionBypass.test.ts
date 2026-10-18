import {
  applySuggestion,
  revertSuggestion,
  suggestChangesKey,
} from '@handlewithcare/prosemirror-suggest-changes'
import { getSchema } from '@tiptap/core'
import { EditorState, type Command, type Transaction } from '@tiptap/pm/state'
import {
  initProseMirrorDoc,
  ySyncPlugin,
  ySyncPluginKey,
} from '@tiptap/y-tiptap'
import * as Y from 'yjs'
import { describe, expect, it } from 'vitest'
import {
  createEditorSchemaExtensions,
  editorJsonToYDoc,
  isYjsUndoRedoTransaction,
  parseEditorMarkdown,
  shouldBypassSuggestionTransform,
  suggestionDescriptors,
  transformToInqtrixSuggestionTransaction,
} from '../src/index'

const SUGGESTION_ID = 'suggestion-command'

function trackedInsertionState(): EditorState {
  const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
  const document = schema.node('doc', null, [schema.node('paragraph', null, schema.text('Hello'))])
  const state = EditorState.create({ schema, doc: document })
  const tracked = transformToInqtrixSuggestionTransaction(
    state.tr.insertText('!', 6),
    state,
    { authorId: 'user-command', createdAt: 20, patchId: 'patch-command' },
    () => SUGGESTION_ID,
  )
  return state.apply(tracked)
}

function executeCommand(command: Command, state: EditorState): Transaction {
  let dispatched: Transaction | null = null
  const handled = command(state, (transaction) => {
    dispatched = transaction
  })
  if (!handled || !dispatched) throw new Error('Suggestion command did not dispatch')
  return dispatched
}

describe('suggestion transaction bypass', () => {
  it.each([
    ['accept', applySuggestion(SUGGESTION_ID), 'Hello!'],
    ['reject', revertSuggestion(SUGGESTION_ID), 'Hello'],
  ] as const)('bypasses the real %s command transaction', (_name, command, expectedText) => {
    const state = trackedInsertionState()
    const transaction = executeCommand(command, state)

    expect(transaction.getMeta(suggestChangesKey)).toMatchObject({ skip: true })
    expect(shouldBypassSuggestionTransform(transaction)).toBe(true)
    expect(transformToInqtrixSuggestionTransaction(
      transaction,
      state,
      { authorId: 'user-command', createdAt: 21, patchId: 'patch-command-result' },
      () => 'must-not-be-used',
    )).toBe(transaction)

    const decided = state.apply(transaction).doc
    expect(decided.textContent).toBe(expectedText)
    expect(suggestionDescriptors(decided)).toEqual([])
  })

  it('bypasses a real y-tiptap transaction emitted by Yjs undo', () => {
    const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
    const yDocument = editorJsonToYDoc(parseEditorMarkdown('Hello'))
    const fragment = yDocument.getXmlFragment('content')
    const initialized = initProseMirrorDoc(fragment, schema)
    let state = EditorState.create({
      schema,
      doc: initialized.doc,
      plugins: [ySyncPlugin(fragment, { mapping: initialized.mapping })],
    })
    const pluginState = ySyncPluginKey.getState(state) as {
      binding: {
        destroy: () => void
        initView: (view: unknown) => void
      }
    }
    const dispatched: Transaction[] = []
    const view = {
      dispatch(transaction: Transaction) {
        dispatched.push(transaction)
        state = state.apply(transaction)
      },
      hasFocus: () => false,
      get state() {
        return state
      },
    }
    pluginState.binding.initView(view)

    const paragraph = fragment.get(0)
    const text = paragraph instanceof Y.XmlElement ? paragraph.get(0) : null
    if (!(text instanceof Y.XmlText)) throw new Error('Expected y-tiptap text content')
    const localOrigin = { source: 'test' }
    const undoManager = new Y.UndoManager(fragment, {
      trackedOrigins: new Set([localOrigin]),
    })

    try {
      yDocument.transact(() => text.insert(text.length, '!'), localOrigin)
      expect(state.doc.textContent).toBe('Hello!')
      dispatched.length = 0

      undoManager.undo()

      const undoTransaction = dispatched.at(-1)
      if (!undoTransaction) throw new Error('Yjs undo did not dispatch a ProseMirror transaction')
      expect(undoTransaction.getMeta(ySyncPluginKey)).toMatchObject({
        isChangeOrigin: true,
        isUndoRedoOperation: true,
      })
      expect(isYjsUndoRedoTransaction(undoTransaction)).toBe(true)
      expect(shouldBypassSuggestionTransform(undoTransaction)).toBe(true)
      expect(transformToInqtrixSuggestionTransaction(
        undoTransaction,
        state,
        { authorId: 'user-undo', createdAt: 22, patchId: 'patch-undo' },
        () => 'must-not-be-used',
      )).toBe(undoTransaction)
      expect(state.doc.textContent).toBe('Hello')
    } finally {
      undoManager.destroy()
      pluginState.binding.destroy()
    }
  })
})
