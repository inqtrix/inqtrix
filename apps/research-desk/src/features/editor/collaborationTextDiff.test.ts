import {
  EDITOR_YJS_FRAGMENT,
  createEditorSchemaExtensions,
} from '@inqtrix/editor-schema'
import { getExtensionField, getSchema } from '@tiptap/core'
import { EditorState, type Plugin, type Transaction } from '@tiptap/pm/state'
import type { EditorView } from '@tiptap/pm/view'
import {
  initProseMirrorDoc,
  prosemirrorJSONToYDoc,
  ySyncPlugin,
  ySyncPluginKey,
  type ProsemirrorBinding,
} from '@tiptap/y-tiptap'
import { describe, expect, it } from 'vitest'
import * as Y from 'yjs'

import {
  MarkAwareCollaborationTextDiff,
  installMarkAwareTextDiff,
} from './collaborationTextDiff'

const SCHEMA = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
const LOCAL_CLIENT = 10

function deletion(id: string) {
  return {
    attrs: {
      authorId: 'user-2',
      createdAt: 1,
      id,
      kind: 'deletion',
      patchId: `patch-${id}`,
      suggestionId: id,
    },
    type: 'deletion',
  }
}

function insertion(id: string) {
  return {
    attrs: {
      authorId: 'user-2',
      createdAt: 1,
      id,
      kind: 'insertion',
      patchId: `patch-${id}`,
      suggestionId: id,
    },
    type: 'insertion',
  }
}

/** The room's document, written by another writer's client. */
function roomDocument(content: ReadonlyArray<Record<string, unknown>>): Uint8Array {
  const document = prosemirrorJSONToYDoc(SCHEMA, {
    content: [{ content, type: 'paragraph' }],
    type: 'doc',
  }, EDITOR_YJS_FRAGMENT)
  expect(document.clientID).not.toBe(LOCAL_CLIENT)
  const update = Y.encodeStateAsUpdate(document)
  document.destroy()
  return update
}

type BoundEditor = {
  binding: ProsemirrorBinding
  document: Y.Doc
  /** Runs a local ProseMirror change through the binding, the way the
   * editor view hands it over after every dispatch. */
  change: (build: (transaction: Transaction) => void) => void
}

/** The editor's Yjs binding on a replica of the room, with a minimal view:
 * the binding reads only the view's state. `markAware` installs the text
 * update directly, or through the editor extension's plugin view, as the
 * editor does. */
function boundEditor(room: Uint8Array, markAware: boolean | 'extension'): BoundEditor {
  const document = new Y.Doc()
  document.clientID = LOCAL_CLIENT
  Y.applyUpdate(document, room)
  const fragment = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const { doc, mapping } = initProseMirrorDoc(fragment, SCHEMA)
  let state = EditorState.create({
    doc,
    plugins: [ySyncPlugin(fragment, { mapping })],
    schema: SCHEMA,
  })
  const binding = ySyncPluginKey.getState(state).binding as ProsemirrorBinding
  const view = { state }
  Reflect.set(binding, 'prosemirrorView', view)
  if (markAware === 'extension') {
    const context = {
      editor: {} as never,
      name: MarkAwareCollaborationTextDiff.name,
      options: MarkAwareCollaborationTextDiff.options,
      storage: {},
    }
    const [plugin] = getExtensionField<() => Plugin[]>(
      MarkAwareCollaborationTextDiff,
      'addProseMirrorPlugins',
      context,
    )()
    plugin!.spec.view!(view as unknown as EditorView)
  } else if (markAware) {
    installMarkAwareTextDiff(binding)
  }
  return {
    binding,
    change: (build) => {
      const transaction = state.tr
      build(transaction)
      state = state.apply(transaction)
      view.state = state
      binding._prosemirrorChanged(state.doc)
    },
    document,
  }
}

/** Each character of the first text with the client that wrote it and the
 * suggestion it belongs to. */
function characters(document: Y.Doc): string[] {
  const text = (document.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0) as Y.XmlElement).get(0) as Y.XmlText
  const result: string[] = []
  let suggestion: string | null = null
  for (let item = text._start; item !== null; item = item.right) {
    if (item.deleted) continue
    if (item.content instanceof Y.ContentFormat) {
      if (item.content.key === 'insertion') {
        const value = item.content.value as { suggestionId?: string } | null
        suggestion = value?.suggestionId ?? null
      }
      continue
    }
    if (!(item.content instanceof Y.ContentString)) continue
    for (const character of item.content.str) {
      const writer = item.id.client === LOCAL_CLIENT ? 'local' : 'room'
      result.push(`${character}:${writer}${suggestion ? `:${suggestion}` : ''}`)
    }
  }
  return result
}

/** The Yjs id of the live character at `index` of the first text. */
function characterId(document: Y.Doc, index: number): Y.ID {
  const text = (document.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0) as Y.XmlElement).get(0) as Y.XmlText
  let remaining = index
  for (let item = text._start; item !== null; item = item.right) {
    if (item.deleted || !(item.content instanceof Y.ContentString)) continue
    if (remaining < item.length) return Y.createID(item.id.client, item.id.clock + remaining)
    remaining -= item.length
  }
  throw new Error(`The text has no character ${index}.`)
}

/** Format items this editor wrote that carry another writer's suggestion. */
function ownFormatItemsWithForeignSuggestion(document: Y.Doc): number {
  const text = (document.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0) as Y.XmlElement).get(0) as Y.XmlText
  let count = 0
  for (let item = text._start; item !== null; item = item.right) {
    if (
      !item.deleted
      && item.id.client === LOCAL_CLIENT
      && item.content instanceof Y.ContentFormat
      && item.content.value !== null
      && item.content.key === 'insertion'
    ) count += 1
  }
  return count
}

const FOREIGN_INSERTION_AFTER_HELLO = [
  { text: 'Hello', type: 'text' },
  { marks: [insertion('s1')], text: ' foo', type: 'text' },
  { text: ' bar', type: 'text' },
] as const

describe('the editor binding writes typed text beside another writer\'s suggestion', () => {
  it('keeps the suggestion\'s own characters when the typed text starts with its first character (edit mode)', () => {
    const editor = boundEditor(roomDocument(FOREIGN_INSERTION_AFTER_HELLO), true)

    editor.change((transaction) => transaction.insertText(' b', 6))

    expect(characters(editor.document)).toEqual([
      'H:room', 'e:room', 'l:room', 'l:room', 'o:room',
      ' :local', 'b:local',
      ' :room:s1', 'f:room:s1', 'o:room:s1', 'o:room:s1',
      ' :room', 'b:room', 'a:room', 'r:room',
    ])
    expect(ownFormatItemsWithForeignSuggestion(editor.document)).toBe(0)
  })

  it('deletes the unmarked character, not the suggestion\'s equal first character, when the deletion ends before it', () => {
    const editor = boundEditor(roomDocument([
      { text: 'ab', type: 'text' },
      { marks: [insertion('s1')], text: 'b', type: 'text' },
      { text: 'c', type: 'text' },
    ]), true)

    const suggested = characterId(editor.document, 2)
    const plain = characterId(editor.document, 1)

    editor.change((transaction) => transaction.delete(2, 3))

    expect(characters(editor.document)).toEqual(['a:room', 'b:room:s1', 'c:room'])
    expect(characterId(editor.document, 1)).toEqual(suggested)
    expect(Y.getItem(editor.document.store, plain).deleted).toBe(true)
  })

  it('writes suggestion-mode typing as its own suggestion next to another writer\'s', () => {
    const editor = boundEditor(roomDocument(FOREIGN_INSERTION_AFTER_HELLO), true)
    const own = SCHEMA.marks.insertion!.create({
      authorId: 'user-1',
      createdAt: 2,
      id: 's9',
      kind: 'insertion',
      patchId: 'patch-s9',
      suggestionId: 's9',
    })

    editor.change((transaction) => transaction.insert(6, SCHEMA.text(' b', [own])))

    expect(characters(editor.document)).toEqual([
      'H:room', 'e:room', 'l:room', 'l:room', 'o:room',
      ' :local:s9', 'b:local:s9',
      ' :room:s1', 'f:room:s1', 'o:room:s1', 'o:room:s1',
      ' :room', 'b:room', 'a:room', 'r:room',
    ])
  })

  it('writes plain text typed right after another writer\'s suggestion without restating the suggestion', () => {
    const editor = boundEditor(roomDocument(FOREIGN_INSERTION_AFTER_HELLO), true)

    editor.change((transaction) => transaction.insertText('x', 10))

    expect(characters(editor.document).slice(5, 11)).toEqual([
      ' :room:s1', 'f:room:s1', 'o:room:s1', 'o:room:s1', 'x:local', ' :room',
    ])
    expect(ownFormatItemsWithForeignSuggestion(editor.document)).toBe(0)
  })

  it('changes formatting and suggestion marks in place, without writing any character anew', () => {
    const editor = boundEditor(roomDocument(FOREIGN_INSERTION_AFTER_HELLO), true)

    editor.change((transaction) => transaction.addMark(1, 6, SCHEMA.marks.bold!.create()))

    expect(characters(editor.document)).toEqual([
      'H:room', 'e:room', 'l:room', 'l:room', 'o:room',
      ' :room:s1', 'f:room:s1', 'o:room:s1', 'o:room:s1',
      ' :room', 'b:room', 'a:room', 'r:room',
    ])
  })

  it('never splits a surrogate pair when it moves the changed characters', () => {
    const editor = boundEditor(roomDocument([
      { text: 'Hi ', type: 'text' },
      { marks: [insertion('s1')], text: '😀😀', type: 'text' },
    ]), true)

    editor.change((transaction) => transaction.insertText('😀', 4))

    // Characters are read by code point: a split pair would read as two
    // lone halves.
    expect(characters(editor.document).slice(3)).toEqual([
      '😀:local', '😀:room:s1', '😀:room:s1',
    ])
  })

  it('is installed on the editor\'s binding by the editor extension', () => {
    const editor = boundEditor(roomDocument(FOREIGN_INSERTION_AFTER_HELLO), 'extension')

    editor.change((transaction) => transaction.insertText(' b', 6))

    expect(characters(editor.document).slice(5, 8)).toEqual([' :local', 'b:local', ' :room:s1'])
  })

  it('never moves a change into a surrogate pair, even where that would keep more suggestion marks', () => {
    // U+1F600 and U+10200 share their low surrogate: the reading that pairs
    // the suggestion's low half with the new character's would split the
    // suggestion's emoji, and the text would be written anew around it.
    const editor = boundEditor(roomDocument([
      { marks: [insertion('s1')], text: '\u{1F600}', type: 'text' },
    ]), true)

    editor.change((transaction) => transaction.replaceWith(1, 3, [
      SCHEMA.text('\u{1F600}'),
      SCHEMA.text('\u{10200}', [SCHEMA.marks.insertion!.create(insertion('s1').attrs)]),
    ]))

    // The room's character leaves the suggestion, so it is written anew;
    // the pair stays whole either way.
    expect(characters(editor.document)).toEqual(['\u{1F600}:local', '\u{10200}:local:s1'])
  })

  it('writes a change without suggestions exactly as the binding itself does', () => {
    const room = roomDocument([{ text: 'Hello  world, hello', type: 'text' }])
    const markAware = boundEditor(room, true)
    const own = boundEditor(room, false)

    for (const editor of [markAware, own]) {
      editor.change((transaction) => transaction.insertText(' ', 7))
      editor.change((transaction) => transaction.insertText('l', 4))
      editor.change((transaction) => transaction.delete(15, 17))
    }

    expect(Y.encodeStateAsUpdate(markAware.document)).toEqual(Y.encodeStateAsUpdate(own.document))
  })

  it('writes text typed over another writer\'s insertion anew, never on the insertion\'s characters', () => {
    const editor = boundEditor(roomDocument(FOREIGN_INSERTION_AFTER_HELLO), true)

    // Select " foo" and type " fox": the shortest diff would keep " fo" of
    // the suggestion as plain text, which reads as accepting it.
    editor.change((transaction) => transaction.insertText(' fox', 6, 10))

    expect(characters(editor.document)).toEqual([
      'H:room', 'e:room', 'l:room', 'l:room', 'o:room',
      ' :local', 'f:local', 'o:local', 'x:local',
      ' :room', 'b:room', 'a:room', 'r:room',
    ])
  })

  it('writes the same text typed over another writer\'s insertion anew, never on the insertion\'s characters', () => {
    const editor = boundEditor(roomDocument(FOREIGN_INSERTION_AFTER_HELLO), true)
    const suggested = characterId(editor.document, 5)

    editor.change((transaction) => transaction.insertText(' foo', 6, 10))

    expect(characters(editor.document)).toEqual([
      'H:room', 'e:room', 'l:room', 'l:room', 'o:room',
      ' :local', 'f:local', 'o:local', 'o:local',
      ' :room', 'b:room', 'a:room', 'r:room',
    ])
    expect(Y.getItem(editor.document.store, suggested).deleted).toBe(true)
  })

  it('keeps an unmarked character away from another writer\'s insertion when a plain character before it is deleted', () => {
    const editor = boundEditor(roomDocument([
      { text: 'xa', type: 'text' },
      { marks: [insertion('s1')], text: 'aa', type: 'text' },
    ]), true)

    editor.change((transaction) => transaction.delete(2, 3))

    expect(characters(editor.document)).toEqual(['x:room', 'a:room:s1', 'a:room:s1'])
  })

  it('writes text typed over another writer\'s insertion anew when the typed text ends with the insertion\'s characters', () => {
    const editor = boundEditor(roomDocument(FOREIGN_INSERTION_AFTER_HELLO), true)

    // " foo" replaced by "x foo": the shortest diff keeps " foo" as the
    // common end and would leave the suggestion's characters unmarked.
    editor.change((transaction) => transaction.insertText('x foo', 6, 10))

    expect(characters(editor.document)).toEqual([
      'H:room', 'e:room', 'l:room', 'l:room', 'o:room',
      'x:local', ' :local', 'f:local', 'o:local', 'o:local',
      ' :room', 'b:room', 'a:room', 'r:room',
    ])
  })

  it('keeps another writer\'s deletion on its own characters when the typed text starts with its first character', () => {
    const editor = boundEditor(roomDocument([
      { text: 'Hello', type: 'text' },
      { marks: [deletion('d1')], text: ' foo', type: 'text' },
      { text: ' bar', type: 'text' },
    ]), true)
    const struck = characterId(editor.document, 5)

    editor.change((transaction) => transaction.insertText(' b', 6))

    // The deletion's first character is still the room's own space: the
    // typed space is new.
    expect(characterId(editor.document, 7)).toEqual(struck)
    expect(Y.getItem(editor.document.store, struck).deleted).toBe(false)
  })

  it('never moves a change into a surrogate pair, even where that would keep more deletion marks', () => {
    const editor = boundEditor(roomDocument([
      { marks: [deletion('d1')], text: '\u{1F600}', type: 'text' },
    ]), true)
    const struck = SCHEMA.marks.deletion!.create(deletion('d1').attrs)

    editor.change((transaction) => transaction.replaceWith(1, 3, [
      SCHEMA.text('\u{1F600}'),
      SCHEMA.text('\u{10200}', [struck]),
    ]))

    expect(characters(editor.document)).toEqual(['\u{1F600}:room', '\u{10200}:local'])
  })

  it('control: the binding\'s own diff hands the suggestion\'s first character to the typed text', () => {
    const editor = boundEditor(roomDocument(FOREIGN_INSERTION_AFTER_HELLO), false)

    editor.change((transaction) => transaction.insertText(' b', 6))

    const written = characters(editor.document)
    expect(written.slice(5, 8)).toEqual([' :room', 'b:local', ' :local:s1'])
  })
})
