import { getSchema, type JSONContent } from '@tiptap/core'
import type { Node as ProseMirrorNode } from '@tiptap/pm/model'
import { EditorState, type Transaction } from '@tiptap/pm/state'
import {
  defaultDeleteFilter,
  defaultProtectedNodes,
  initProseMirrorDoc,
  updateYFragment,
  ySyncPluginKey,
} from '@tiptap/y-tiptap'
import * as Y from 'yjs'
import { describe, expect, it } from 'vitest'
import {
  EDITOR_YJS_FRAGMENT,
  EDITOR_YJS_TEXTBLOCK_NAMES,
  completeCreatedTextblocks,
  completeTextblocksCreatedSince,
  createEditorSchemaExtensions,
  editorJsonToYDoc,
  editorYDocToJson,
  isTextblockCompletion,
  validateEditorYDoc,
} from '../src/index'

// Whoever creates a textblock gives it its one empty text node in the same
// update, so that the first characters two writers type into it go into one
// text node. These tests drive the completion the way the editor does: from
// the document's afterTransaction event, after writes shaped like the Yjs
// binding's (updateYFragment over a ProseMirror document).

const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))
const REMOTE = Symbol('remote')

function paragraph(text?: string): JSONContent {
  return text ? { content: [{ text, type: 'text' }], type: 'paragraph' } : { type: 'paragraph' }
}

/** A document that runs the completion after every transaction and records
 * what it wrote. The editor skips the transactions it applies from the room
 * before it gets here; the completion must hold without that. */
function writerDocument(initial: Uint8Array, clientId: number) {
  const document = new Y.Doc()
  document.clientID = clientId
  Y.applyUpdate(document, initial, REMOTE)
  const updates: Array<{ completion: boolean; origin: unknown; update: Uint8Array }> = []
  const completedBy: boolean[] = []
  const transactions: Y.Transaction[] = []
  document.on('afterTransaction', (transaction: Y.Transaction) => {
    transactions.push(transaction)
    completedBy.push(completeCreatedTextblocks(transaction))
  })
  document.on('update', (update: Uint8Array, origin: unknown, _doc: Y.Doc, transaction: Y.Transaction) => {
    updates.push({ completion: isTextblockCompletion(transaction), origin, update })
  })
  return { completedBy, document, transactions, updates }
}

/** Writes a ProseMirror change into the document the way the binding does. */
function write(
  document: Y.Doc,
  build: (state: EditorState) => Transaction,
  origin: unknown = ySyncPluginKey,
  meta: Record<string, unknown> = {},
): ProseMirrorNode {
  const fragment = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, schema)
  const state = EditorState.create({ doc: initialized.doc, schema })
  const target = build(state).doc
  document.transact((transaction) => {
    for (const [key, value] of Object.entries(meta)) transaction.meta.set(key, value)
    updateYFragment(document, fragment, target, initialized.meta)
  }, origin)
  return target
}

/** Every textblock in document order with its children: `text:` and the
 * text of a text node (`text:!` when it carries attributes), or the name of
 * an element. */
function textblockChildren(document: Y.Doc): Array<[string, string[]]> {
  const found: Array<[string, string[]]> = []
  const visit = (element: Y.XmlElement): void => {
    if (['codeBlock', 'heading', 'paragraph'].includes(element.nodeName)) {
      found.push([element.nodeName, element.toArray().map((child) => (
        child instanceof Y.XmlText
          ? `text:${Object.keys(child.getAttributes()).length > 0 ? '!' : ''}${child.toString()}`
          : (child as Y.XmlElement).nodeName
      ))])
      return
    }
    for (const child of element.toArray()) {
      if (child instanceof Y.XmlElement) visit(child)
    }
  }
  for (const child of document.getXmlFragment(EDITOR_YJS_FRAGMENT).toArray()) {
    if (child instanceof Y.XmlElement) visit(child)
  }
  return found
}

/** A stored document whose second line is an empty paragraph without a text
 * node, as documents written before the completion hold it. */
function documentWithTextlessLine(): Uint8Array {
  const built = editorJsonToYDoc({ content: [paragraph('Oben.'), paragraph(), paragraph('Unten.')], type: 'doc' })
  const line = built.getXmlFragment(EDITOR_YJS_FRAGMENT).get(1) as Y.XmlElement
  line.delete(0, line.length)
  const state = Y.encodeStateAsUpdate(built)
  built.destroy()
  return state
}

const INITIAL = Y.encodeStateAsUpdate(editorJsonToYDoc({
  content: [paragraph('Oben.'), paragraph('Unten.')],
  type: 'doc',
}))

describe('completing the textblocks a transaction created', () => {
  it('gives every textblock a local transaction created without content one empty text node, in the update of that transaction', () => {
    const writer = writerDocument(INITIAL, 101)
    const nodes = schema.nodes
    const target = write(writer.document, (state) => state.tr.insert(
      state.doc.child(0).nodeSize,
      [
        nodes.paragraph!.create(),
        nodes.heading!.create({ level: 2 }),
        nodes.codeBlock!.create(),
        nodes.bulletList!.create(null, nodes.listItem!.create(null, nodes.paragraph!.create())),
        nodes.taskList!.create(null, nodes.taskItem!.create(null, nodes.paragraph!.create())),
        nodes.table!.create(null, nodes.tableRow!.create(null, [
          nodes.tableHeader!.create(null, nodes.paragraph!.create()),
          nodes.tableCell!.create(null, nodes.paragraph!.create()),
        ])),
      ],
    ))

    const expected: Array<[string, string[]]> = [
      ['paragraph', ['text:Oben.']],
      ['paragraph', ['text:']],
      ['heading', ['text:']],
      ['codeBlock', ['text:']],
      ['paragraph', ['text:']],
      ['paragraph', ['text:']],
      ['paragraph', ['text:']],
      ['paragraph', ['text:']],
      ['paragraph', ['text:Unten.']],
    ]
    expect(textblockChildren(writer.document)).toEqual(expected)
    // One completion wrote (reading the document runs empty transactions).
    expect(writer.completedBy.filter(Boolean)).toHaveLength(1)
    expect(validateEditorYDoc(writer.document)).toEqual(target.toJSON())

    // The creating transaction's own update already carries the text nodes:
    // a replica that receives only that update holds the same blocks. The
    // completion's update follows with the same origin and repeats them.
    expect(writer.updates.map(({ completion, origin }) => [completion, origin])).toEqual([
      [false, ySyncPluginKey],
      [true, ySyncPluginKey],
    ])
    const replica = new Y.Doc()
    Y.applyUpdate(replica, INITIAL)
    Y.applyUpdate(replica, writer.updates[0]!.update)
    expect(textblockChildren(replica)).toEqual(expected)
    const completionOnly = Y.decodeUpdate(writer.updates[1]!.update).structs
    const creating = new Set(Y.decodeUpdate(writer.updates[0]!.update).structs
      .map((struct) => `${struct.id.client}:${struct.id.clock}`))
    expect(completionOnly.every((struct) => creating.has(`${struct.id.client}:${struct.id.clock}`)))
      .toBe(true)
    replica.destroy()
    writer.document.destroy()
  })

  it('leaves a textblock with text, one holding only an inline node, one that existed before, and every block that is not a textblock, as they are', () => {
    const writer = writerDocument(documentWithTextlessLine(), 102)
    const nodes = schema.nodes
    write(writer.document, (state) => {
      const transaction = state.tr.insertText('!', state.doc.child(0).nodeSize - 1)
      return transaction.insert(transaction.doc.content.size, [
        nodes.paragraph!.create(null, schema.text('Neu.')),
        nodes.paragraph!.create(null, nodes.hardBreak!.create()),
        nodes.horizontalRule!.create(),
        nodes.blockMath!.create({ latex: 'x^2' }),
      ])
    })
    const leaves = writer.document.getXmlFragment(EDITOR_YJS_FRAGMENT).toArray().slice(-2)
    expect(leaves.map((leaf) => [(leaf as Y.XmlElement).nodeName, (leaf as Y.XmlElement).length]))
      .toEqual([['horizontalRule', 0], ['blockMath', 0]])

    expect(textblockChildren(writer.document)).toEqual([
      ['paragraph', ['text:Oben.!']],
      ['paragraph', []],
      ['paragraph', ['text:Unten.']],
      ['paragraph', ['text:Neu.']],
      ['paragraph', ['hardBreak']],
    ])
    // Reading a text runs an empty transaction of its own; none wrote.
    expect(writer.completedBy.some(Boolean)).toBe(false)
    expect(writer.updates.map(({ completion }) => completion)).toEqual([false])
    writer.document.destroy()
  })

  it('never completes a textblock another client created, even one carrying this document\'s own client id', () => {
    // A remote update writes an empty paragraph without a text node, as an
    // editor without the completion does. The receiver never completes it:
    // only the creator writes the text node, so two receivers never add two.
    for (const senderId of [201, 202]) {
      const receiver = writerDocument(INITIAL, 202)
      const sender = new Y.Doc()
      sender.clientID = senderId
      Y.applyUpdate(sender, INITIAL)
      const vector = Y.encodeStateVector(sender)
      sender.transact(() => {
        sender.getXmlFragment(EDITOR_YJS_FRAGMENT).insert(1, [new Y.XmlElement('paragraph')])
      })
      // The sender with id 202 shares the receiver's client id: its items
      // then fall into the receiver's own clock range, and only the
      // transaction being remote keeps the receiver from completing them.
      Y.applyUpdate(receiver.document, Y.encodeStateAsUpdate(sender, vector), REMOTE)

      expect(textblockChildren(receiver.document)[1]).toEqual(['paragraph', []])
      expect(receiver.completedBy.some(Boolean)).toBe(false)
      expect(receiver.updates.map(({ origin }) => origin)).toEqual([REMOTE])
      receiver.document.destroy()
      sender.destroy()
    }
  })

  it('writes with the origin and meta of the transaction it completes, so one undo removes both and redo restores one text node', () => {
    const writer = writerDocument(INITIAL, 103)
    const fragment = writer.document.getXmlFragment(EDITOR_YJS_FRAGMENT)
    // Configured as the editor's undo plugin configures it. Its delete filter
    // lets an undo remove a paragraph it inserted only while that paragraph
    // is empty: the text node must belong to the same undo step, or undoing
    // an Enter would leave the new line in place.
    const undo = new Y.UndoManager(fragment, {
      captureTransaction: (transaction) => transaction.meta.get('addToHistory') !== false,
      deleteFilter: (item) => defaultDeleteFilter(item, defaultProtectedNodes),
      trackedOrigins: new Set([ySyncPluginKey]),
    })
    write(writer.document, (state) => state.tr.split(state.doc.child(0).nodeSize - 1), ySyncPluginKey, {
      addToHistory: true,
    })
    const [creating, completion] = writer.transactions.slice(-2)
    expect(isTextblockCompletion(creating!)).toBe(false)
    expect(isTextblockCompletion(completion!)).toBe(true)
    expect(completion!.origin).toBe(ySyncPluginKey)
    expect(completion!.meta.get('addToHistory')).toBe(true)
    expect(textblockChildren(writer.document).map(([, children]) => children))
      .toEqual([['text:Oben.'], ['text:'], ['text:Unten.']])
    expect(undo.undoStack).toHaveLength(1)

    undo.undo()
    expect(textblockChildren(writer.document).map(([, children]) => children))
      .toEqual([['text:Oben.'], ['text:Unten.']])
    undo.redo()
    expect(textblockChildren(writer.document).map(([, children]) => children))
      .toEqual([['text:Oben.'], ['text:'], ['text:Unten.']])
    expect(undo.undoStack).toHaveLength(1)
    expect(undo.redoStack).toHaveLength(0)

    // A change the editor keeps out of the history keeps its completion out
    // too: nothing is left for an undo step of its own.
    undo.clear()
    write(writer.document, (state) => state.tr.split(state.doc.child(0).nodeSize - 1), ySyncPluginKey, {
      addToHistory: false,
    })
    expect(textblockChildren(writer.document)[1]).toEqual(['paragraph', ['text:']])
    expect(undo.undoStack).toHaveLength(0)
    undo.destroy()
    writer.document.destroy()
  })

  it('reads only the items the transaction wrote, not the document', () => {
    // A document of many blocks with text: a walk over the document visits
    // every block and finds nothing to complete. The completion of one new
    // block must cost a small fraction of such a walk.
    const blocks = 30_000
    const big = new Y.Doc()
    big.transact(() => {
      big.getXmlFragment(EDITOR_YJS_FRAGMENT).insert(0, Array.from({ length: blocks }, (_, index) => {
        const element = new Y.XmlElement('paragraph')
        const text = new Y.XmlText()
        text.insert(0, `Absatz ${index}.`)
        element.insert(0, [text])
        return element
      }))
    })
    const writer = writerDocument(Y.encodeStateAsUpdate(big), 104)
    big.destroy()
    const fragment = writer.document.getXmlFragment(EDITOR_YJS_FRAGMENT)
    const walk = (): number => {
      let visited = 0
      const visit = (element: Y.XmlElement): void => {
        visited += 1
        if (EDITOR_YJS_TEXTBLOCK_NAMES.has(element.nodeName) && element.length === 0) return
        for (const child of element.toArray()) {
          if (child instanceof Y.XmlElement) visit(child)
        }
      }
      for (const child of fragment.toArray()) {
        if (child instanceof Y.XmlElement) visit(child)
      }
      return visited
    }
    const completionMs: number[] = []
    const walkMs: number[] = []
    for (let round = 0; round < 5; round += 1) {
      const started = performance.now()
      expect(walk()).toBe(blocks + round)
      walkMs.push(performance.now() - started)
      let measured = 0
      const measure = (transaction: Y.Transaction): void => {
        if (isTextblockCompletion(transaction)) return
        // The writer's own handler already ran for this transaction; a
        // second call on the completed state writes nothing but reads the
        // same items, which is what is measured here.
        const begin = performance.now()
        completeCreatedTextblocks(transaction)
        measured = performance.now() - begin
      }
      writer.document.on('afterTransaction', measure)
      writer.document.transact(() => {
        fragment.insert(round, [new Y.XmlElement('paragraph')])
      }, ySyncPluginKey)
      writer.document.off('afterTransaction', measure)
      completionMs.push(measured)
    }
    const fastestCompletion = Math.min(...completionMs)
    const fastestWalk = Math.min(...walkMs)
    expect(writer.completedBy.filter(Boolean)).toHaveLength(5)
    expect(fastestCompletion * 50).toBeLessThan(fastestWalk)
    writer.document.destroy()
  })
})

describe('completing the textblocks a service writer created', () => {
  it('completes only the textblocks written from the given clock on', () => {
    const document = new Y.Doc()
    document.clientID = 301
    Y.applyUpdate(document, documentWithTextlessLine())
    const fragment = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
    // An earlier write of this same client left a line without a text node.
    // Only the clock keeps it out of the completion, not the client id.
    fragment.insert(0, [new Y.XmlElement('paragraph')])
    const since = Y.getState(document.store, document.clientID)
    expect(since).toBe(1)
    const initialized = initProseMirrorDoc(fragment, schema)
    const state = EditorState.create({ doc: initialized.doc, schema })
    const target = state.apply(state.tr.insert(state.doc.content.size, [
      schema.nodes.heading!.create({ level: 1 }),
      schema.nodes.paragraph!.create(null, schema.text('Text')),
    ])).doc
    updateYFragment(document, fragment, target, initialized.meta)

    expect(completeTextblocksCreatedSince(document, since)).toBe(true)
    expect(textblockChildren(document)).toEqual([
      ['paragraph', []],
      ['paragraph', ['text:Oben.']],
      ['paragraph', []],
      ['paragraph', ['text:Unten.']],
      ['heading', ['text:']],
      ['paragraph', ['text:Text']],
    ])
    expect(completeTextblocksCreatedSince(document, since)).toBe(false)
    expect(editorYDocToJson(document)).toEqual(target.toJSON())
    document.destroy()
  })
})
