import { getSchema } from '@tiptap/core'
import { setBlockType } from '@tiptap/pm/commands'
import { EditorState, TextSelection, type Transaction } from '@tiptap/pm/state'
import { initProseMirrorDoc, updateYFragment } from '@tiptap/y-tiptap'
import * as Y from 'yjs'
import { describe, expect, it } from 'vitest'
import {
  EDITOR_YJS_FRAGMENT,
  INQTRIX_STRUCTURE_COMMAND_META,
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  collaborationMergeByteCap,
  completeCreatedTextblocks,
  createEditorSchemaExtensions,
  describeLocalEditorTransaction,
  describeLocalEditorUpdate,
  editorJsonToYDoc,
  isFormattingCleanupTransaction,
  localBatchSealReason,
  mergeLocalBatchFacts,
  parseEditorMarkdown,
  transformToInqtrixSuggestionTransaction,
  type CollaborationWriteStamp,
  type LocalBatchFacts,
  type LocalEditorTransactionFacts,
  type LocalEditorUpdateFacts,
} from '../src/index'

const AUTHOR_ID = '11111111-1111-4111-8111-111111111111'
const PATCH_ID = '22222222-2222-4222-8222-222222222222'
const SUGGESTION_ID = '33333333-3333-4333-8333-333333333333'
const STRUCTURE_SUGGESTION_ID = '44444444-4444-4444-8444-444444444444'
const LOCAL_EDIT = Object.freeze({ kind: 'local-edit' })
const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

function stateOf(document: Y.Doc): {
  meta: ReturnType<typeof initProseMirrorDoc>['meta']
  state: EditorState
} {
  const initialized = initProseMirrorDoc(document.getXmlFragment(EDITOR_YJS_FRAGMENT), schema)
  return {
    meta: initialized.meta,
    state: EditorState.create({ doc: initialized.doc, schema }),
  }
}

// Writes an editor transaction into the document the way the editor's Yjs
// binding does, in suggest mode through the suggestion transform.
function write(
  document: Y.Doc,
  build: (state: EditorState) => Transaction,
  options: { origin?: unknown; suggest?: boolean; suggestionId?: string } = {},
): void {
  const { meta, state } = stateOf(document)
  let transaction = build(state)
  if (options.suggest) {
    transaction = transformToInqtrixSuggestionTransaction(
      transaction,
      state,
      { authorId: AUTHOR_ID, createdAt: 1_784_112_000, patchId: PATCH_ID },
      () => options.suggestionId ?? SUGGESTION_ID,
    )
  }
  const target = transaction.doc
  document.transact(() => {
    updateYFragment(document, document.getXmlFragment(EDITOR_YJS_FRAGMENT), target, meta)
  }, options.origin ?? LOCAL_EDIT)
}

function textblockRange(state: EditorState, block: number): [number, number] {
  const ranges: Array<[number, number]> = []
  state.doc.descendants((node, position) => {
    if (node.isTextblock) ranges.push([position + 1, position + 1 + node.content.size])
    return !node.isTextblock
  })
  const range = ranges[block]
  if (!range) throw new Error(`the document has no text block ${block}`)
  return range
}

/** The editor's change of block type for the textblock `block`. */
function blockTypeChange(
  state: EditorState,
  block: number,
  type: string,
  attrs: Record<string, unknown> = {},
): Transaction {
  const nodeType = schema.nodes[type]
  if (!nodeType) throw new Error(`the schema has no ${type}`)
  const selected = state.apply(state.tr.setSelection(
    TextSelection.create(state.doc, textblockRange(state, block)[0]),
  ))
  let changed: Transaction | null = null
  setBlockType(nodeType, attrs)(selected, (transaction) => {
    changed = transaction
  })
  if (!changed) throw new Error(`the block cannot become a ${type}`)
  return changed
}

/** Proposes the textblock `block`, an empty line, as a heading in suggest
 * mode: a slash command typed into it, replaced by the structure suggestion. */
function proposeHeading(document: Y.Doc, block: number): void {
  let position = 0
  write(document, (state) => {
    position = textblockRange(state, block)[0]
    return state.tr.insertText('/', position)
  }, { suggest: true })
  const heading = schema.nodes.heading
  if (!heading) throw new Error('the schema has no heading')
  write(document, (state) => state.tr
    .delete(position, position + 1)
    .setNodeMarkup(position - 1, heading, { level: 2 })
    .setMeta(INQTRIX_STRUCTURE_COMMAND_META, {
      action: 'heading2',
      commandRange: { from: position, to: position + 1 },
    }), { suggest: true, suggestionId: STRUCTURE_SUGGESTION_ID })
  const proposed = document.getXmlFragment(EDITOR_YJS_FRAGMENT).get(block) as Y.XmlElement
  if (proposed.getAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR) == null) {
    throw new Error('the empty line carries no structure suggestion')
  }
}

// Records the facts of every local editor transaction and update, wired the
// way the browser wires them: transaction facts in afterTransaction, followed
// by the completion of the textblocks the transaction created, and update
// facts in the update event of the same transaction.
function recordLocalUpdates(
  document: Y.Doc,
  access: 'edit' | 'suggest',
  stamp: CollaborationWriteStamp,
): LocalEditorUpdateFacts[] {
  const transactionFacts = new WeakMap<Y.Transaction, LocalEditorTransactionFacts>()
  const recorded: LocalEditorUpdateFacts[] = []
  document.on('afterTransaction', (transaction: Y.Transaction) => {
    if (transaction.origin === LOCAL_EDIT) {
      transactionFacts.set(transaction, describeLocalEditorTransaction(transaction))
      completeCreatedTextblocks(transaction)
    }
  })
  document.on(
    'update',
    (update: Uint8Array, origin: unknown, _document: Y.Doc, transaction: Y.Transaction) => {
      if (origin !== LOCAL_EDIT) return
      recorded.push(describeLocalEditorUpdate({
        access,
        stamp,
        transaction: transactionFacts.get(transaction) ?? null,
        update,
      }))
    },
  )
  return recorded
}

function documentWith(markdown: string): Y.Doc {
  const document = new Y.Doc()
  Y.applyUpdate(document, Y.encodeStateAsUpdate(editorJsonToYDoc(parseEditorMarkdown(markdown))))
  return document
}

function boldUpdate(base: Uint8Array, clientId: number, from: number, to: number): Uint8Array {
  const writer = new Y.Doc()
  writer.clientID = clientId
  Y.applyUpdate(writer, base)
  const vector = Y.encodeStateVector(writer)
  const bold = schema.marks.bold
  if (!bold) throw new Error('the schema has no bold mark')
  write(writer, (state) => state.tr.addMark(from, to, bold.create()), { origin: null })
  return Y.encodeStateAsUpdate(writer, vector)
}

describe('formatting cleanup transactions', () => {
  // Two writers bold the first word of a paragraph from the same base. When
  // the second bold reaches a document that holds the first, Yjs removes the
  // now redundant format item in a transaction of its own.
  function concurrentBolds(): { base: Uint8Array; first: Uint8Array; second: Uint8Array } {
    const base = Y.encodeStateAsUpdate(editorJsonToYDoc(parseEditorMarkdown('Hello world')))
    return {
      base,
      first: boldUpdate(base, 101, 1, 6),
      second: boldUpdate(base, 102, 1, 6),
    }
  }

  function observeTransactions(document: Y.Doc, action: () => void): {
    cleanups: boolean[]
    deletedContent: string[][]
    inserted: boolean[]
    locals: boolean[]
    updates: Uint8Array[]
  } {
    const cleanups: boolean[] = []
    const deletedContent: string[][] = []
    const inserted: boolean[] = []
    const locals: boolean[] = []
    const updates: Uint8Array[] = []
    const onTransaction = (transaction: Y.Transaction): void => {
      cleanups.push(isFormattingCleanupTransaction(transaction))
      locals.push(transaction.local)
      inserted.push([...transaction.afterState].some(
        ([client, clock]) => (transaction.beforeState.get(client) ?? 0) !== clock,
      ))
      const kinds: string[] = []
      Y.iterateDeletedStructs(transaction, transaction.deleteSet, (struct) => {
        kinds.push(struct instanceof Y.Item ? struct.content.constructor.name : 'GC')
      })
      deletedContent.push(kinds)
    }
    const onUpdate = (update: Uint8Array): void => {
      updates.push(update)
    }
    document.on('afterTransaction', onTransaction)
    document.on('update', onUpdate)
    try {
      action()
    } finally {
      document.off('afterTransaction', onTransaction)
      document.off('update', onUpdate)
    }
    return { cleanups, deletedContent, inserted, locals, updates }
  }

  it('recognizes the cleanup Yjs runs after a concurrent identical format and nothing else', () => {
    const { base, first, second } = concurrentBolds()
    const room = new Y.Doc()
    Y.applyUpdate(room, base)
    Y.applyUpdate(room, first)

    const applied = observeTransactions(room, () => Y.applyUpdate(room, second))
    expect(applied.locals).toEqual([false, true])
    expect(applied.cleanups).toEqual([false, true])
    expect(applied.updates).toHaveLength(2)

    const paragraph = room.getXmlFragment(EDITOR_YJS_FRAGMENT).get(0) as Y.XmlElement
    const text = paragraph.get(0) as Y.XmlText
    const inserted = observeTransactions(room, () => room.transact(() => text.insert(0, 'x')))
    expect(inserted.cleanups).toEqual([false])
    const deleted = observeTransactions(room, () => room.transact(() => text.delete(0, 1)))
    expect(deleted.inserted).toEqual([false])
    expect(deleted.deletedContent).toEqual([['ContentString']])
    expect(deleted.cleanups).toEqual([false])
    const formatted = observeTransactions(room, () => room.transact(() => {
      text.format(0, 3, { italic: {} })
    }))
    expect(formatted.cleanups).toEqual([false])
    // Extending the bold inserts format items and deletes the old closing
    // one: only format items are deleted, but structs are inserted.
    const extended = observeTransactions(room, () => room.transact(() => {
      text.format(0, 8, { bold: {} })
    }))
    expect(extended.inserted).toEqual([true])
    expect(extended.deletedContent[0]?.length).toBeGreaterThan(0)
    expect(new Set(extended.deletedContent[0])).toEqual(new Set(['ContentFormat']))
    expect(extended.cleanups).toEqual([false])
    // The check is about shape: a writer removing a whole format run deletes
    // only format items and inserts nothing, too. Callers therefore apply it
    // only to the transactions that follow a remote change.
    const unbolded = observeTransactions(room, () => room.transact(() => {
      text.format(0, 11, { bold: null })
    }))
    expect(unbolded.inserted).toEqual([false])
    expect(unbolded.cleanups).toEqual([true])
    const unchanged = observeTransactions(room, () => room.transact(() => undefined))
    expect(unchanged.cleanups).toEqual([false])

    // The same deletion of a format item arriving from another client is a
    // remote change, not a cleanup of this document.
    const [, cleanupUpdate] = applied.updates
    if (!cleanupUpdate) throw new Error('the cleanup produced no update')
    const loaded = new Y.Doc()
    Y.applyUpdate(loaded, Y.mergeUpdates([base, first, second]))
    const remote = observeTransactions(loaded, () => Y.applyUpdate(loaded, cleanupUpdate))
    expect(remote.locals[0]).toBe(false)
    expect(remote.inserted[0]).toBe(false)
    expect(remote.deletedContent[0]).toEqual(['ContentFormat'])
    expect(remote.cleanups[0]).toBe(false)
  })
})

describe('local editor update facts', () => {
  it('classifies plain edit-mode typing as direct and an edit next to a suggestion as isolated', () => {
    const document = documentWith('Hello\n\nWorld\n\nThird')
    write(document, (state) => state.tr.insertText(' there', textblockRange(state, 0)[1]), {
      suggest: true,
    })
    const recorded = recordLocalUpdates(document, 'edit', 'edit')

    write(document, (state) => state.tr.insertText('!', textblockRange(state, 1)[1]))
    write(document, (state) => state.tr.insertText('Q', textblockRange(state, 0)[0] + 7))
    expect(recorded.map((facts) => facts.class)).toEqual(['direct', 'isolated'])
    expect(recorded[0]).toMatchObject({
      boundary: false,
      deletes: false,
      inserts: true,
      structure: false,
    })
  })

  it('isolates an edit-mode deletion that removes the last suggestion mark of a paragraph', () => {
    // The mark's format items are deleted in the same transaction; only the
    // transaction, before Yjs discards the deleted content, still shows them.
    const document = documentWith('Hello\n\nWorld')
    write(document, (state) => state.tr.insertText(' there', textblockRange(state, 0)[1]), {
      suggest: true,
    })
    const recorded = recordLocalUpdates(document, 'edit', 'edit')
    write(document, (state) => {
      const [, end] = textblockRange(state, 0)
      return state.tr.delete(end - 6, end)
    })
    expect(stateOf(document).state.doc.child(0).textContent).toBe('Hello')
    expect(recorded.map((facts) => facts.class)).toEqual(['isolated'])
    expect(recorded[0]).toMatchObject({ deletes: true })
  })

  it('isolates an edit-mode change that deletes a block', () => {
    const document = documentWith('Hello\n\nWorld')
    const recorded = recordLocalUpdates(document, 'edit', 'edit')
    write(document, (state) => state.tr.delete(
      textblockRange(state, 0)[1],
      textblockRange(state, 1)[0],
    ))
    expect(stateOf(document).state.doc.childCount).toBe(1)
    expect(recorded.map((facts) => facts.class)).toEqual(['isolated'])
  })

  it('isolates an edit-mode change inside a block that carries a structure suggestion', () => {
    const document = documentWith('## Suggest target\n\nWorld')
    write(document, (state) => state.tr.insertText('/', 1), { suggest: true })
    const paragraph = schema.nodes.paragraph
    if (!paragraph) throw new Error('the schema has no paragraph')
    write(document, (state) => state.tr
      .delete(1, 2)
      .setNodeMarkup(0, paragraph, { textAlign: null })
      .setMeta(INQTRIX_STRUCTURE_COMMAND_META, {
        action: 'paragraph',
        commandRange: { from: 1, to: 2 },
      }), { suggest: true, suggestionId: STRUCTURE_SUGGESTION_ID })
    const recorded = recordLocalUpdates(document, 'edit', 'edit')
    write(document, (state) => state.tr.insertText('X', textblockRange(state, 0)[0] + 3))
    write(document, (state) => state.tr.insertText('Y', textblockRange(state, 1)[1]))
    expect(recorded.map((facts) => facts.class)).toEqual(['isolated', 'direct'])
  })

  it('isolates an edit-mode change to a block that carries a structure suggestion, also when it removes the suggestion', () => {
    const document = documentWith('## Suggest target\n\nWorld')
    write(document, (state) => state.tr.insertText('/', 1), { suggest: true })
    const paragraph = schema.nodes.paragraph
    if (!paragraph) throw new Error('the schema has no paragraph')
    write(document, (state) => state.tr
      .delete(1, 2)
      .setNodeMarkup(0, paragraph, { textAlign: null })
      .setMeta(INQTRIX_STRUCTURE_COMMAND_META, {
        action: 'paragraph',
        commandRange: { from: 1, to: 2 },
      }), { suggest: true, suggestionId: STRUCTURE_SUGGESTION_ID })
    const recorded = recordLocalUpdates(document, 'edit', 'edit')
    // Only the block's own attributes change; its text stays as it is.
    write(document, (state) => state.tr.setNodeAttribute(0, 'textAlign', 'center'))
    // The block keeps its text and loses the structure suggestion.
    write(document, (state) => state.tr.setNodeAttribute(0, INQTRIX_STRUCTURE_SUGGESTION_ATTR, null))
    expect(stateOf(document).state.doc.child(0).attrs).toMatchObject({
      [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: null,
      textAlign: 'center',
    })
    expect(recorded.map((facts) => facts.class)).toEqual(['isolated', 'isolated'])
    expect(recorded.map((facts) => facts.boundary)).toEqual([false, false])
  })

  it('marks every update under suggest access or in suggest mode as a suggestion change', () => {
    const suggestAccess = documentWith('Hello')
    const underSuggestAccess = recordLocalUpdates(suggestAccess, 'suggest', 'edit')
    write(suggestAccess, (state) => state.tr.insertText('!', textblockRange(state, 0)[1]), {
      suggest: true,
    })
    const suggestMode = documentWith('Hello')
    const inSuggestMode = recordLocalUpdates(suggestMode, 'edit', 'suggest')
    write(suggestMode, (state) => state.tr.insertText('!', textblockRange(state, 0)[1]), {
      suggest: true,
    })
    expect(underSuggestAccess.map((facts) => facts.class)).toEqual(['suggestion'])
    expect(inSuggestMode.map((facts) => facts.class)).toEqual(['suggestion'])
    expect(underSuggestAccess[0]).toMatchObject({ boundary: true, inserts: true })
  })

  it('isolates an update whose transaction facts are missing', () => {
    const facts = describeLocalEditorUpdate({
      access: 'suggest',
      stamp: 'suggest',
      transaction: null,
      update: Y.encodeStateAsUpdate(documentWith('Hello')),
    })
    expect(facts).toMatchObject({ class: 'isolated', completion: false, deletes: true, inserts: true })
  })

  // Pressing Enter at the end of a block, turning an empty line into a
  // heading, and turning an empty line that carries a structure suggestion
  // into a code block each create a textblock without content. The update
  // that completes it follows and joins the batch of the creating update,
  // whatever that update's class.
  it.each([
    ['Enter at the end of a block', 'Hello\n\nWorld', 'direct', (state: EditorState) => (
      state.tr.split(textblockRange(state, 0)[1])
    ), false],
    ['an empty line turned into a heading', 'Hello\n\n\n\nWorld', 'isolated', (state: EditorState) => (
      blockTypeChange(state, 1, 'heading', { level: 2 })
    ), false],
    ['an empty line with a structure suggestion turned into a code block', 'Hello\n\n\n\nWorld', 'isolated', (state: EditorState) => (
      blockTypeChange(state, 1, 'codeBlock')
    ), true],
  ] as const)('marks the completion after %s, which joins the creating update', (_label, markdown, creatingClass, build, proposed) => {
    const document = documentWith(markdown)
    if (proposed) proposeHeading(document, 1)
    const recorded = recordLocalUpdates(document, 'edit', 'edit')
    write(document, build)

    expect(recorded.map((facts) => [facts.class, facts.completion])).toEqual([
      [creatingClass, false],
      ['direct', true],
    ])
    const [creating, completion] = recorded as [LocalEditorUpdateFacts, LocalEditorUpdateFacts]
    expect(localBatchSealReason(mergeLocalBatchFacts(null, creating), completion, 1_000)).toBeNull()
    // The block was created without content and holds its text node.
    const created = document.getXmlFragment(EDITOR_YJS_FRAGMENT).get(1) as Y.XmlElement
    expect(created.toArray().map((child) => child instanceof Y.XmlText && child.length === 0))
      .toEqual([true])
  })

  it('marks no completion for typing, deleting, or a block created with content', () => {
    const document = documentWith('Hello\n\nWorld')
    const recorded = recordLocalUpdates(document, 'edit', 'edit')
    write(document, (state) => state.tr.insertText('!', textblockRange(state, 0)[1]))
    write(document, (state) => state.tr.delete(textblockRange(state, 1)[0], textblockRange(state, 1)[0] + 1))
    write(document, (state) => state.tr.split(textblockRange(state, 0)[1] - 2))
    expect(recorded.map((facts) => facts.completion)).toEqual([false, false, false])
  })
})

describe('local batch sealing', () => {
  const CAP = 1_000

  function update(overrides: Partial<LocalEditorUpdateFacts>): LocalEditorUpdateFacts {
    return {
      boundary: false,
      bytes: 10,
      class: 'direct',
      completion: false,
      deletes: false,
      inserts: true,
      structure: false,
      ...overrides,
    }
  }

  function batch(...members: LocalEditorUpdateFacts[]): LocalBatchFacts {
    let facts: LocalBatchFacts | null = null
    for (const member of members) facts = mergeLocalBatchFacts(facts, member)
    if (!facts) throw new Error('a batch needs a member')
    return facts
  }

  it('never seals before the first member of a batch', () => {
    expect(localBatchSealReason(null, update({ class: 'isolated', structure: true }), CAP))
      .toBeNull()
  })

  it('seals where direct and suggestion changes meet', () => {
    expect(localBatchSealReason(batch(update({})), update({ class: 'suggestion' }), CAP))
      .toBe('write_mode')
    expect(localBatchSealReason(batch(update({ class: 'suggestion' })), update({}), CAP))
      .toBe('write_mode')
  })

  it('seals before a structure suggestion and before the first suggestion boundary', () => {
    const suggestion = update({ boundary: true, class: 'suggestion' })
    expect(localBatchSealReason(
      batch(suggestion),
      update({ boundary: true, class: 'suggestion', structure: true }),
      CAP,
    )).toBe('structure_suggestion')
    expect(localBatchSealReason(
      batch(update({ class: 'suggestion' })),
      update({ boundary: true, class: 'suggestion' }),
      CAP,
    )).toBe('first_suggestion_boundary')
    expect(localBatchSealReason(batch(suggestion), suggestion, CAP)).toBeNull()
  })

  it('keeps a suggestion batch insertion-only or deletion-only', () => {
    const inserting = update({ class: 'suggestion' })
    const deleting = update({ class: 'suggestion', deletes: true, inserts: false })
    const both = update({ class: 'suggestion', deletes: true })
    expect(localBatchSealReason(batch(inserting), deleting, CAP)).toBe('suggestion_direction')
    expect(localBatchSealReason(batch(deleting), inserting, CAP)).toBe('suggestion_direction')
    expect(localBatchSealReason(batch(deleting), both, CAP)).toBe('suggestion_direction')
    expect(localBatchSealReason(batch(both), deleting, CAP)).toBe('suggestion_direction')
    expect(localBatchSealReason(batch(inserting), inserting, CAP)).toBeNull()
    expect(localBatchSealReason(batch(deleting), deleting, CAP)).toBeNull()
    // Direct batches may mix insertions and deletions.
    expect(localBatchSealReason(batch(update({})), update({ deletes: true, inserts: false }), CAP))
      .toBeNull()
  })

  it('sends an isolated update alone', () => {
    expect(localBatchSealReason(batch(update({})), update({ class: 'isolated' }), CAP))
      .toBe('isolated_update')
    expect(localBatchSealReason(batch(update({ class: 'isolated' })), update({}), CAP))
      .toBe('isolated_update')
  })

  it('lets a completion join the batch of the update it completes, whatever that batch is', () => {
    const completion = update({ bytes: 12, completion: true })
    const isolated = batch(update({ class: 'isolated', deletes: true }))
    expect(localBatchSealReason(isolated, completion, CAP)).toBeNull()
    expect(localBatchSealReason(isolated, { ...completion, class: 'isolated' }, CAP)).toBeNull()
    const deletingSuggestion = batch(update({ class: 'suggestion', deletes: true, inserts: false }))
    expect(localBatchSealReason(deletingSuggestion, { ...completion, class: 'suggestion' }, CAP))
      .toBeNull()
    expect(localBatchSealReason(batch(update({ bytes: 995 })), completion, CAP)).toBeNull()
    // The batch keeps the completed update's class: what follows it seals.
    const completed = batch(update({ class: 'isolated', deletes: true }), completion)
    expect(completed.class).toBe('isolated')
    expect(localBatchSealReason(completed, update({}), CAP)).toBe('isolated_update')
  })

  it('seals before the merged bytes would exceed the cap', () => {
    const members = [update({ bytes: 400 }), update({ bytes: 400 })]
    expect(batch(...members).bytes).toBe(800)
    expect(localBatchSealReason(batch(...members), update({ bytes: 200 }), CAP)).toBeNull()
    expect(localBatchSealReason(batch(...members), update({ bytes: 201 }), CAP)).toBe('byte_cap')
  })

  it('derives the merge byte cap from the published frame limit', () => {
    expect(collaborationMergeByteCap(64 * 1024)).toBe(8 * 1024)
    expect(collaborationMergeByteCap(1024 * 1024)).toBe(128 * 1024)
    expect(collaborationMergeByteCap(2 * 1024 * 1024)).toBe(256 * 1024)
    expect(collaborationMergeByteCap(64 * 1024 * 1024)).toBe(256 * 1024)
    for (const invalid of [0, -1, 1.5, Number.NaN]) {
      expect(() => collaborationMergeByteCap(invalid)).toThrow(RangeError)
    }
  })
})
