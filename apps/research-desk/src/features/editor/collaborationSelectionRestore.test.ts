import {
  EDITOR_YJS_FRAGMENT,
  createEditorSchemaExtensions,
} from '@inqtrix/editor-schema'
import { getSchema } from '@tiptap/core'
import { splitBlock } from '@tiptap/pm/commands'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { splitListItem } from '@tiptap/pm/schema-list'
import { EditorState, TextSelection, type PluginView, type Transaction } from '@tiptap/pm/state'
import type { EditorView } from '@tiptap/pm/view'
import { ySyncPlugin, ySyncPluginKey, type ProsemirrorBinding } from '@tiptap/y-tiptap'
import { afterEach, describe, expect, it } from 'vitest'
import * as Y from 'yjs'

import { installMarkAwareTextDiff } from './collaborationTextDiff'

/*
 * After every change another writer makes, the editor's Yjs binding rebuilds
 * the ProseMirror document and restores the local selection from positions
 * relative to Yjs items. These tests run the real binding over the editor's
 * schema, with the editor's own text update installed: the writer's caret
 * stays where it was, and the letters the writer types next land in the
 * writer's own paragraph, while another writer types, deletes and moves
 * blocks around it.
 */

const SCHEMA = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
const REMOTE = Symbol('remote')
const PARAGRAPHS = 30

function line(index: number): string {
  return `Line ${String(index).padStart(2, '0')} mark${index} end.`
}

type Caret = {
  /** Index of the top-level block that holds the caret. */
  block: number
  offset: number
  text: string
}

type Writer = {
  /** Position in front of top-level block `block`. */
  blockStart: (block: number) => number
  caret: () => Caret
  destroy: () => void
  dispatch: (transaction: Transaction) => void
  /** Presses Enter at the caret. */
  enter: () => void
  /** Presses Enter at the caret inside a list item. */
  enterInList: () => void
  /** Places the caret at `offset` in top-level block `block`. */
  placeCaret: (block: number, offset: number) => void
  placeCaretAt: (position: number) => void
  receive: (update: Uint8Array) => void
  /** Every update this writer produced since the last call, merged. */
  send: () => Uint8Array
  state: () => EditorState
  texts: () => string[]
  type: (text: string) => void
}

const writers: Writer[] = []

afterEach(() => {
  for (const writer of writers.splice(0)) writer.destroy()
})

/** An editor on its own replica: the binding's plugin with a minimal view
 * whose dispatch hands every local change to the binding, as the editor
 * view does. The view never has focus, so nothing scrolls. */
function writer(clientId: number): Writer {
  const document = new Y.Doc()
  document.clientID = clientId
  const plugin = ySyncPlugin(document.getXmlFragment(EDITOR_YJS_FRAGMENT))
  let state = EditorState.create({ plugins: [plugin], schema: SCHEMA })
  // The plugin view renders the replica while it is created, before it can
  // take updates.
  let pluginView: PluginView | null = null
  const view = {
    dispatch: (transaction: Transaction) => {
      const previous = state
      state = state.apply(transaction)
      pluginView?.update?.(view as unknown as EditorView, previous)
    },
    hasFocus: () => false,
    get state() {
      return state
    },
  }
  pluginView = plugin.spec.view!(view as unknown as EditorView)
  installMarkAwareTextDiff(ySyncPluginKey.getState(state).binding as ProsemirrorBinding)
  const outbox: Uint8Array[] = []
  document.on('update', (update: Uint8Array, origin: unknown) => {
    if (origin !== REMOTE) outbox.push(update)
  })
  const blockStart = (block: number): number => {
    let position = 0
    for (let index = 0; index < block; index += 1) position += state.doc.child(index).nodeSize
    return position
  }
  const created: Writer = {
    blockStart,
    caret: () => {
      const head = state.selection.$head
      return { block: head.index(0), offset: head.parentOffset, text: head.parent.textContent }
    },
    destroy: () => {
      pluginView?.destroy?.()
      document.destroy()
    },
    dispatch: view.dispatch,
    enter: () => {
      splitBlock(state, view.dispatch)
    },
    enterInList: () => {
      splitListItem(SCHEMA.nodes.listItem!)(state, view.dispatch)
    },
    placeCaret: (block, offset) => {
      view.dispatch(state.tr.setSelection(
        TextSelection.create(state.doc, blockStart(block) + 1 + offset),
      ))
    },
    placeCaretAt: (position) => {
      view.dispatch(state.tr.setSelection(TextSelection.create(state.doc, position)))
    },
    receive: (update) => {
      Y.applyUpdate(document, update, REMOTE)
    },
    send: () => Y.mergeUpdates(outbox.splice(0)),
    state: () => state,
    texts: () => {
      const texts: string[] = []
      state.doc.forEach((node) => texts.push(node.textContent))
      return texts
    },
    type: (text) => {
      view.dispatch(state.tr.insertText(text))
    },
  }
  writers.push(created)
  return created
}

/** Two writers on the same document; `other` wrote it. */
function twoWriters(blocks: readonly ProseMirrorNode[]): { local: Writer; other: Writer } {
  const other = writer(1)
  const local = writer(3)
  other.dispatch(other.state().tr.replaceWith(0, other.state().doc.content.size, blocks))
  local.receive(other.send())
  expect(local.texts()).toEqual(other.texts())
  return { local, other }
}

function paragraphs(): ProseMirrorNode[] {
  return Array.from({ length: PARAGRAPHS }, (_, index) => (
    SCHEMA.nodes.paragraph!.create(null, SCHEMA.text(line(index)))
  ))
}

/** The product's "move up" and "move down" of a block: the block is
 * deleted and a copy made from its JSON is inserted next to its neighbour,
 * in one transaction. */
function moveBlock(writer: Writer, block: number, direction: 'down' | 'up'): void {
  const doc = writer.state().doc
  const from = writer.blockStart(block)
  const node = doc.child(block)
  const target = direction === 'up'
    ? from - doc.child(block - 1).nodeSize
    : from + doc.child(block + 1).nodeSize
  const transaction = writer.state().tr.delete(from, from + node.nodeSize)
  transaction.insert(target, SCHEMA.nodeFromJSON(node.toJSON()))
  writer.dispatch(transaction)
}

/** A block dragged by its handle: the block is deleted and the same node is
 * inserted where it was dropped, before block `before` of the old document. */
function dragBlock(writer: Writer, block: number, before: number): void {
  const from = writer.blockStart(block)
  const node = writer.state().doc.child(block)
  const drop = writer.blockStart(before)
  const transaction = writer.state().tr.delete(from, from + node.nodeSize)
  transaction.insert(transaction.mapping.map(drop), node)
  writer.dispatch(transaction)
}

/** The top-level block whose text holds `marker`. */
function blockWith(writer: Writer, marker: string): string | undefined {
  return writer.texts().find((text) => text.includes(marker))
}

describe('the caret of a writer while another writer changes the document', () => {
  it('stays in place when another writer types in the caret\'s paragraph', () => {
    const { local, other } = twoWriters(paragraphs())
    local.placeCaret(5, 3)

    other.placeCaret(5, line(5).length)
    other.type('x')
    local.receive(other.send())

    expect(local.caret()).toEqual({ block: 5, offset: 3, text: `${line(5)}x` })
    local.type('QQ')
    expect(local.texts()[5]).toBe(`LinQQe 05 mark5 end.x`)
  })

  it('stays in place when another writer deletes the character before it', () => {
    const { local, other } = twoWriters(paragraphs())
    local.placeCaret(5, 1)

    const start = other.blockStart(5)
    other.dispatch(other.state().tr.delete(start + 1, start + 2))
    local.receive(other.send())

    expect(local.caret()).toEqual({ block: 5, offset: 0, text: line(5).slice(1) })
    local.type('QQ')
    expect(local.texts()[5]).toBe(`QQ${line(5).slice(1)}`)
  })

  it('stays in its own paragraph when another writer moves the paragraph below it up', () => {
    const { local, other } = twoWriters(paragraphs())
    local.placeCaret(4, 15)

    moveBlock(other, 5, 'up')
    local.receive(other.send())

    expect(local.texts().slice(4, 6)).toEqual([line(5), line(4)])
    expect(local.caret()).toMatchObject({ offset: 15, text: line(4) })
    local.type('QQ')
    expect(blockWith(local, 'QQ')).toBe('Line 04 mark4 eQQnd.')
  })

  it('stays in its own paragraph when another writer moves the paragraph above it down', () => {
    const { local, other } = twoWriters(paragraphs())
    local.placeCaret(5, 15)

    moveBlock(other, 4, 'down')
    local.receive(other.send())

    expect(local.texts().slice(4, 6)).toEqual([line(5), line(4)])
    expect(local.caret()).toMatchObject({ offset: 15, text: line(5) })
    local.type('QQ')
    expect(blockWith(local, 'QQ')).toBe('Line 05 mark5 eQQnd.')
  })

  it('stays in its own paragraph when another writer drags a paragraph from below to above it', () => {
    const { local, other } = twoWriters(paragraphs())
    local.placeCaret(5, 15)

    dragBlock(other, 10, 2)
    local.receive(other.send())

    expect(local.texts()[2]).toBe(line(10))
    expect(local.caret()).toMatchObject({ offset: 15, text: line(5) })
    local.type('QQ')
    expect(blockWith(local, 'QQ')).toBe('Line 05 mark5 eQQnd.')
  })

  it('stays in its own paragraph when another writer drags a paragraph from above to below it', () => {
    const { local, other } = twoWriters(paragraphs())
    local.placeCaret(5, 15)

    dragBlock(other, 2, 11)
    local.receive(other.send())

    expect(local.texts()[10]).toBe(line(2))
    expect(local.caret()).toMatchObject({ offset: 15, text: line(5) })
    local.type('QQ')
    expect(blockWith(local, 'QQ')).toBe('Line 05 mark5 eQQnd.')
  })

  // Known limit of @tiptap/y-tiptap up to 3.0.9. When the caret sits at the
  // start of a block, the binding checks the position it restored against a
  // search by content: "the n-th block of this type with this text". Every
  // empty paragraph has the same text, so a new empty paragraph above the
  // caret's own shifts the count by one, and the caret leaves its own block
  // for another one; the next letters land there. Reported upstream as a
  // selection restore that picks a same-text sibling block
  // (https://github.com/ueberdosis/y-tiptap/issues/51). When a release fixes
  // it, these tests fail: turn them into plain tests.
  it.fails('stays in a new empty paragraph when another writer presses Enter above it', () => {
    const { local, other } = twoWriters(paragraphs())
    local.placeCaret(27, line(27).length)
    local.enter()
    other.receive(local.send())

    other.placeCaret(2, line(2).length)
    other.enter()
    local.receive(other.send())

    expect(local.caret()).toEqual({ block: 29, offset: 0, text: '' })
    local.type('QQ')
    expect(local.texts()[29]).toBe('QQ')
  })

  it.fails('stays in a new empty list item when another writer presses Enter in an item above it', () => {
    const items = Array.from({ length: 6 }, (_, index) => SCHEMA.nodes.listItem!.create(
      null,
      SCHEMA.nodes.paragraph!.create(null, SCHEMA.text(`Item ${index} mark${index}`)),
    ))
    const { local, other } = twoWriters([
      SCHEMA.nodes.paragraph!.create(null, SCHEMA.text('Before the list')),
      SCHEMA.nodes.bulletList!.create(null, items),
      SCHEMA.nodes.paragraph!.create(null, SCHEMA.text('After the list')),
    ])
    const endOfItem = (writer: Writer, item: number): number => {
      const list = writer.state().doc.child(1)
      let position = writer.blockStart(1) + 1
      for (let index = 0; index < item; index += 1) position += list.child(index).nodeSize
      return position + list.child(item).nodeSize - 2
    }
    local.placeCaretAt(endOfItem(local, 4))
    local.enterInList()
    other.receive(local.send())

    other.placeCaretAt(endOfItem(other, 1))
    other.enterInList()
    local.receive(other.send())

    const head = local.state().selection.$head
    expect({ item: head.index(1), text: head.parent.textContent }).toEqual({ item: 6, text: '' })
    local.type('QQ')
    expect(local.state().doc.child(1).child(6).textContent).toBe('QQ')
  })
})
