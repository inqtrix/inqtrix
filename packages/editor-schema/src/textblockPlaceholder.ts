import { getSchema } from '@tiptap/core'
import * as Y from 'yjs'

import { createEditorSchemaExtensions } from './extensions.js'

// A textblock without text holds exactly one empty text node in shared
// state, from the update that creates it on.
//
// The Yjs binding writes the first character of a textblock that has no child
// into a new text node. Two writers who type into the same such block before
// either has received the other's character therefore write two text nodes
// side by side, which the schema refuses, and every way to join them
// afterwards deletes a text node a writer may still be typing into or copies
// its text a second time. With one empty text node in place, both writers'
// characters go into that node as ordinary concurrent insertions, exactly as
// in a block that already holds text.
//
// It is the shape the binding itself keeps for a textblock whose text was
// deleted (it empties the text node instead of removing it, so that remote
// changes land on the same node), and the only extra child the canonical
// comparison accepts. The binding keeps it, shows it as nothing and types
// into it. Whoever creates a textblock writes it, in the same update: the
// editor after each of its own transactions, and every service writer that
// builds or rewrites content. A textblock someone else created is never
// completed, so two writers never add a text node to the same block.

/** Names of the schema's textblocks: the nodes whose content is inline. */
export const EDITOR_YJS_TEXTBLOCK_NAMES: ReadonlySet<string> = new Set(
  Object.values(getSchema(createEditorSchemaExtensions({ enableUndoRedo: false })).nodes)
    .filter((node) => node.isTextblock)
    .map((node) => node.name),
)

const completions = new WeakSet<Y.Transaction>()

/**
 * Gives every textblock `transaction` created and left without children its
 * empty text node. Returns whether it wrote.
 *
 * Call it from the document's `afterTransaction` event, never inside an open
 * transaction. Only a local transaction is completed, and only the blocks the
 * document's own client created in it: the items of its own clock range, so
 * the cost follows the transaction, not the document. A block another client
 * created, or one that existed before, is left as it is.
 *
 * The text nodes are written in a transaction of their own, which Yjs runs
 * right after `transaction`, with its origin and meta so that an undo manager
 * treats both as one change. Yjs encodes an update from every struct written
 * since its transaction began, so the update of `transaction` already carries
 * the text nodes; the follow-up's own update repeats them
 * (`isTextblockCompletion`).
 */
export function completeCreatedTextblocks(transaction: Y.Transaction): boolean {
  if (!transaction.local) return false
  const document = transaction.doc
  const client = document.clientID
  const created = textlessTextblocksCreated(
    document,
    client,
    transaction.beforeState.get(client) ?? 0,
    transaction.afterState.get(client) ?? 0,
  )
  if (created.length === 0) return false
  document.transact((completion) => {
    completions.add(completion)
    for (const [key, value] of transaction.meta) completion.meta.set(key, value)
    appendEmptyText(created)
  }, transaction.origin)
  return true
}

/** Whether `transaction` is the follow-up in which completeCreatedTextblocks
 * wrote the text nodes of the blocks another transaction created. */
export function isTextblockCompletion(transaction: Y.Transaction): boolean {
  return completions.has(transaction)
}

/**
 * Gives every textblock the document's own client created from clock `since`
 * on, and left without children, its empty text node. Returns whether it
 * wrote.
 *
 * For a writer that builds or rewrites content on a document of its own and
 * sends everything it wrote as one update: pass the client's clock from
 * before the write (0 for a document built from scratch).
 */
export function completeTextblocksCreatedSince(document: Y.Doc, since: number): boolean {
  const client = document.clientID
  const created = textlessTextblocksCreated(
    document,
    client,
    since,
    Y.getState(document.store, client),
  )
  if (created.length === 0) return false
  document.transact(() => appendEmptyText(created))
  return true
}

function appendEmptyText(textblocks: readonly Y.XmlElement[]): void {
  for (const textblock of textblocks) textblock.insert(0, [new Y.XmlText()])
}

/** The live textblocks without children among the items `client` wrote
 * from clock `from` up to `to`. */
function textlessTextblocksCreated(
  document: Y.Doc,
  client: number,
  from: number,
  to: number,
): Y.XmlElement[] {
  if (to <= from) return []
  const structs = document.store.clients.get(client)
  if (!structs) return []
  const created: Y.XmlElement[] = []
  for (let index = Y.findIndexSS(structs, from); index < structs.length; index += 1) {
    const struct = structs[index]!
    if (struct.id.clock >= to) break
    if (!(struct instanceof Y.Item) || struct.deleted) continue
    if (!(struct.content instanceof Y.ContentType)) continue
    const type = struct.content.type
    if (
      type instanceof Y.XmlElement
      && EDITOR_YJS_TEXTBLOCK_NAMES.has(type.nodeName)
      && type.length === 0
    ) created.push(type)
  }
  return created
}
