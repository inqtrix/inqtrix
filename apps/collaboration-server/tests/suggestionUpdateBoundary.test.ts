import { Editor, getSchema, type JSONContent } from '@tiptap/core'
import { Fragment, Slice, type Node as ProseMirrorNode } from '@tiptap/pm/model'
import { joinBackward, joinForward, setBlockType, splitBlock, wrapIn } from '@tiptap/pm/commands'
import { splitListItem, wrapInList } from '@tiptap/pm/schema-list'
import { EditorState, TextSelection, type Transaction } from '@tiptap/pm/state'
import { initProseMirrorDoc, updateYFragment } from '@tiptap/y-tiptap'
import {
  EDITOR_YJS_FRAGMENT,
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  UnsupportedSuggestionStructureError,
  assertDirectEditKeepsSuggestionRecords,
  createEditorSchemaExtensions,
  editorCollaborationRoom,
  editorJsonToYDoc,
  editorYDocToJson,
  getEditorSchemaFingerprint,
  parseEditorMarkdown,
  projectOriginalJson,
  structureSuggestionsLeftInapplicable,
  transformToInqtrixSuggestionTransaction,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import type { ConnectionContext, SidecarLogger } from '../src/contracts'
import * as changeSummaryModule from '../src/changeSummary'
import { resolvePatchDecision } from '../src/decisionResolver'
import { DocumentCoordinator } from '../src/documentCoordinator'
import { CollaborationError } from '../src/errors'
import { InstanceLeaseManager } from '../src/instanceLease'
import { SidecarMetrics } from '../src/metrics'
import { collectSuggestionRecords, assertSuggestionDeleteSet } from '../src/suggestPolicy'
import { FakeCollaborationApi, USER_ID, settings, silentLogger } from './helpers'

// Every case here is judged by the real acceptance path of the collaboration
// service: a client-shaped Yjs update goes through
// DocumentCoordinator.prepareClientUpdate against a room, with the access of
// the person who sends it. The suggest-only lease is the trust boundary: it
// may propose, but never change content directly, hide a change, or rewrite
// another person's suggestion.

const WRITER = USER_ID
const REVIEWER = '44444444-4444-4444-8444-444444444444'
const THIRD = '55555555-5555-4555-8555-555555555555'
const DOCUMENT_ID = 'ed_boundary'
const ROOM = editorCollaborationRoom(DOCUMENT_ID, 1)
const CREATED_AT = 1_784_112_000
const ZERO_WIDTH_SPACE = '\u200B'
const schema = getSchema(createEditorSchemaExtensions({ enableUndoRedo: false }))

const OWN_INSERTION = { patchId: 'aaaaaaaa-0000-4000-8000-0000000000f1', suggestionId: 'aaaaaaaa-0000-4000-8000-000000000001' }
const FOREIGN_INSERTION = { patchId: 'bbbbbbbb-0000-4000-8000-0000000000f1', suggestionId: 'bbbbbbbb-0000-4000-8000-000000000001' }
const FOREIGN_STRUCTURE = { patchId: 'cccccccc-0000-4000-8000-0000000000f1', suggestionId: 'cccccccc-0000-4000-8000-000000000001' }
const OWN_STRUCTURE = { patchId: 'dddddddd-0000-4000-8000-0000000000f1', suggestionId: 'dddddddd-0000-4000-8000-000000000001' }

let generated = 0
function uuid(): string {
  generated += 1
  return `eeeeeeee-0000-4000-8000-${generated.toString(16).padStart(12, '0')}`
}

type MarkJson = NonNullable<JSONContent['marks']>[number]

function insertionMark(authorId: string, ids: { patchId: string; suggestionId: string }): MarkJson {
  return {
    type: 'insertion',
    attrs: { authorId, createdAt: CREATED_AT, id: ids.suggestionId, kind: 'insertion', ...ids },
  }
}

function structure(
  authorId: string,
  action: string,
  ids: { patchId: string; suggestionId: string },
): Record<string, unknown> {
  return { action, authorId, createdAt: CREATED_AT, kind: 'structure', ...ids }
}

function text(value: string, marks?: MarkJson[]): JSONContent {
  return marks ? { type: 'text', text: value, marks } : { type: 'text', text: value }
}

function paragraph(content: JSONContent[], attrs?: Record<string, unknown>): JSONContent {
  return attrs ? { type: 'paragraph', attrs, content } : { type: 'paragraph', content }
}

function listItem(value: string): JSONContent {
  return { type: 'listItem', content: [paragraph([text(value)])] }
}

// A document with a pending suggestion of each shape the rules protect.
const MATRIX = Y.encodeStateAsUpdate(editorJsonToYDoc({
  type: 'doc',
  content: [
    paragraph([text('Plain ZONEPLAIN.')]),
    paragraph([text('Own ZONEOWN.'), text(' OWNTEXT', [insertionMark(WRITER, OWN_INSERTION)])]),
    paragraph([text('Structured ZONESTRUCT.')], {
      [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: structure(REVIEWER, 'heading2', FOREIGN_STRUCTURE),
    }),
    paragraph([text('Own structure ZONEOWNSTRUCT.')], {
      [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: structure(WRITER, 'heading3', OWN_STRUCTURE),
    }),
    paragraph([text('Foreign ZONEFOREIGN.'), text(' FOREIGNTEXT', [insertionMark(REVIEWER, FOREIGN_INSERTION)])]),
    { type: 'bulletList', content: [listItem('List one ZONELISTONE'), listItem('List two ZONELISTTWO')] },
    {
      type: 'taskList',
      content: [{ type: 'taskItem', attrs: { checked: false }, content: [paragraph([text('Task ZONETASK')])] }],
    },
    paragraph([text('Aligned ZONEALIGN.')], { textAlign: 'center' }),
    { type: 'heading', attrs: { level: 2 }, content: [text('Heading ZONEHEADING')] },
    paragraph([text('Last ZONELAST.')]),
  ],
}))

// The first and the last block carry another person's structure suggestion.
const EDGES = Y.encodeStateAsUpdate(editorJsonToYDoc({
  type: 'doc',
  content: [
    paragraph([text('First block ZONEFIRST')], {
      [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: structure(REVIEWER, 'heading2', FOREIGN_STRUCTURE),
    }),
    paragraph([text('Middle ZONEMIDDLE')]),
    paragraph([text('Last block ZONEEDGELAST')], {
      [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: structure(REVIEWER, 'heading3', {
        patchId: 'cccccccc-0000-4000-8000-0000000000f2',
        suggestionId: 'cccccccc-0000-4000-8000-000000000002',
      }),
    }),
  ],
}))

function documentFrom(state: Uint8Array, clientId: number): Y.Doc {
  const document = new Y.Doc()
  document.clientID = clientId
  Y.applyUpdate(document, state)
  return document
}

function fork(base: Y.Doc, clientId: number): Y.Doc {
  return documentFrom(Y.encodeStateAsUpdate(base), clientId)
}

/** A change the editor writes through y-tiptap, as a client update. */
function editorUpdate(
  base: Y.Doc,
  clientId: number,
  build: (state: EditorState) => Transaction,
): Uint8Array {
  const document = fork(base, clientId)
  const fragment = document.getXmlFragment(EDITOR_YJS_FRAGMENT)
  const initialized = initProseMirrorDoc(fragment, schema)
  const target = build(EditorState.create({ doc: initialized.doc, schema })).doc
  const vector = Y.encodeStateVector(document)
  document.transact(() => updateYFragment(document, fragment, target, initialized.meta))
  const update = Y.encodeStateAsUpdate(document, vector)
  document.destroy()
  return update
}

/** The same change made in suggest mode by the real suggestion transform. */
function suggestionUpdate(
  base: Y.Doc,
  clientId: number,
  authorId: string,
  build: (state: EditorState) => Transaction,
): Uint8Array {
  return editorUpdate(base, clientId, (state) => transformToInqtrixSuggestionTransaction(
    build(state),
    state,
    { authorId, createdAt: CREATED_AT + clientId, patchId: uuid() },
    uuid,
  ))
}

/** A change written directly into the shared types, as a modified client could. */
function rawUpdate(
  base: Y.Doc,
  clientId: number,
  mutate: (fragment: Y.XmlFragment) => void,
): Uint8Array {
  const document = fork(base, clientId)
  const vector = Y.encodeStateVector(document)
  document.transact(() => mutate(document.getXmlFragment(EDITOR_YJS_FRAGMENT)))
  const update = Y.encodeStateAsUpdate(document, vector)
  document.destroy()
  return update
}

function block(document: ProseMirrorNode, needle: string): { node: ProseMirrorNode; position: number } {
  let found: { node: ProseMirrorNode; position: number } | null = null
  document.descendants((node, position) => {
    if (found) return false
    if (node.isTextblock && node.textContent.includes(needle)) {
      found = { node, position }
      return false
    }
    return true
  })
  if (!found) throw new Error(`fixture has no text block containing ${needle}`)
  return found
}

function blockEnd(document: ProseMirrorNode, needle: string): number {
  const { node, position } = block(document, needle)
  return position + 1 + node.content.size
}

function topLevelIndex(fragment: Y.XmlFragment, needle: string): number {
  const index = fragment.toArray().findIndex((child) => child.toString().includes(needle))
  if (index < 0) throw new Error(`fixture has no top-level block containing ${needle}`)
  return index
}

function splitItem(itemType: 'listItem' | 'taskItem', needle: string) {
  return (state: EditorState): Transaction => {
    const type = schema.nodes[itemType]
    if (!type) throw new Error(`schema has no ${itemType}`)
    const atEnd = state.apply(state.tr.setSelection(
      TextSelection.create(state.doc, blockEnd(state.doc, needle)),
    ))
    let split: Transaction | null = null
    splitListItem(type)(atEnd, (transaction) => {
      split = transaction
    })
    if (!split) throw new Error(`the ${itemType} could not be split`)
    // Carry the selection step into a transaction on the original state.
    const combined = state.tr.setSelection(atEnd.selection)
    for (const step of (split as Transaction).steps) combined.step(step)
    return combined
  }
}

type Step = {
  access: 'edit' | 'suggest'
  update: (room: Y.Doc) => Uint8Array
  user: string
}

type Room = {
  api: FakeCollaborationApi
  close: () => Promise<void>
  coordinator: DocumentCoordinator
  decide: (decision: 'accept' | 'reject', patchIds: string[]) => Promise<string>
  document: Y.Doc
  submit: (step: Step) => Promise<string>
}

async function openRoom(initial: Uint8Array, logger: SidecarLogger = silentLogger): Promise<Room> {
  const api = new FakeCollaborationApi()
  const configured = settings()
  const metrics = new SidecarMetrics()
  const lease = new InstanceLeaseManager(api, configured, silentLogger, metrics, () => undefined)
  await lease.start()
  const coordinator = new DocumentCoordinator(api, lease, configured, logger, metrics)
  coordinator.initialize(ROOM, 0, { bytes: 0, updates: 0 })
  const document = documentFrom(initial, 1)
  const schemaHash = await getEditorSchemaFingerprint()
  let connections = 0
  let commands = 0
  return {
    api,
    close: () => lease.stop(),
    coordinator,
    decide: async (decision, patchIds) => {
      commands += 1
      try {
        await coordinator.applyServerMutation({
          actorKind: 'human',
          actorUserId: WRITER,
          changeKind: 'decision',
          commandId: `command-${commands}`,
          commandPayloadHash: `payload-${commands}`,
          decision,
          document,
          documentId: DOCUMENT_ID,
          expectedSequence: coordinator.getPersistedSequence(ROOM),
          generation: 1,
          mutate: (clone) => resolvePatchDecision(clone, { decision, patchIds }),
          requestedPatchIds: patchIds,
          room: ROOM,
          sequenceRule: 'not_ahead',
        })
        return 'accepted decision'
      } catch (error) {
        if (error instanceof CollaborationError) return `${error.code} ${error.reason}`
        throw error
      }
    },
    document,
    submit: async (step) => {
      connections += 1
      const connectionId = `connection-${connections}`
      const context: ConnectionContext = {
        access: step.access,
        documentId: DOCUMENT_ID,
        expiresAt: Date.now() / 1_000 + 600,
        generation: 1,
        leaseId: 'lease-1',
        policyCursor: 0,
        protocolVersion: configured.protocolVersion,
        schemaHash,
        schemaVersion: configured.schemaVersion,
        sessionId: connectionId,
        tenantId: 'tenant-1',
        user: { color: '#123456', id: step.user, name: 'Collaborator' },
      }
      const update = step.update(document)
      try {
        const result = await coordinator.prepareClientUpdate({
          allowNoop: false,
          connectionId,
          context,
          document,
          room: ROOM,
          update,
        })
        if (result !== 'pending') return result
        Y.applyUpdate(document, update)
        coordinator.finishClientUpdate(connectionId, document)
        return `accepted ${api.persisted.at(-1)?.changeKind ?? 'unknown'}`
      } catch (error) {
        if (error instanceof CollaborationError) return `${error.code} ${error.reason}`
        throw error
      }
    },
  }
}

async function judge(initial: Uint8Array, steps: Step[]): Promise<string[]> {
  const room = await openRoom(initial)
  try {
    const verdicts: string[] = []
    for (const step of steps) verdicts.push(await room.submit(step))
    return verdicts
  } finally {
    room.document.destroy()
    await room.close()
  }
}

/** Submits an edit and returns the verdict, the suggestions its row
 * rejected, and the structure data the room's target block ends with. */
async function editOfStructure(
  initial: Uint8Array,
  update: (room: Y.Doc) => Uint8Array,
  needle: string,
): Promise<{ data: unknown; rejected: string[]; verdict: string }> {
  const room = await openRoom(initial)
  try {
    const verdict = await room.submit({ access: 'edit', update, user: WRITER })
    const fragment = room.document.getXmlFragment(EDITOR_YJS_FRAGMENT)
    const element = fragment.get(topLevelIndex(fragment, needle))
    return {
      data: element instanceof Y.XmlElement
        ? element.getAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR) ?? null
        : null,
      rejected: room.api.persisted.at(-1)?.rejectedSuggestionIds ?? [],
      verdict,
    }
  } finally {
    room.document.destroy()
    await room.close()
  }
}

// A refusal names the layer that refused: the suggestion policy, or the
// document validation that runs before it for every writer.
const POLICY_REFUSAL = '4403 suggestion_policy_violation'
const SCHEMA_REFUSAL = '4409 invalid_schema'

describe('a new block in suggest mode', () => {
  const enterAtEnd = (needle: string) => (state: EditorState) => state.tr.split(blockEnd(state.doc, needle))

  it.each([
    ['a suggest-only lease', 'suggest'],
    ['an edit lease writing in suggest mode', 'edit'],
  ] as const)('is accepted as Enter at the end of a paragraph from %s', async (_name, access) => {
    expect(await judge(MATRIX, [{
      access,
      update: (room) => suggestionUpdate(room, 4001, WRITER, enterAtEnd('ZONEPLAIN')),
      user: WRITER,
    }])).toEqual(['accepted suggestion'])
  })

  it('is accepted when typed into after Enter, and after Enter twice', async () => {
    expect(await judge(MATRIX, [
      { access: 'suggest', update: (room) => suggestionUpdate(room, 4001, WRITER, enterAtEnd('ZONEPLAIN')), user: WRITER },
      {
        access: 'suggest',
        update: (room) => suggestionUpdate(room, 4001, WRITER, (state) => (
          state.tr.insertText('New', blockEnd(state.doc, 'ZONEPLAIN') + 3)
        )),
        user: WRITER,
      },
      {
        access: 'suggest',
        update: (room) => suggestionUpdate(room, 4001, WRITER, (state) => (
          state.tr.split(blockEnd(state.doc, 'New'))
        )),
        user: WRITER,
      },
    ])).toEqual(['accepted suggestion', 'accepted suggestion', 'accepted suggestion'])
  })

  it('is accepted at the end of a list item, a task item, a centered paragraph and after a heading', async () => {
    const paragraphType = schema.nodes.paragraph
    if (!paragraphType) throw new Error('schema has no paragraph')
    expect(await judge(MATRIX, [
      { access: 'suggest', update: (room) => suggestionUpdate(room, 4001, WRITER, splitItem('listItem', 'ZONELISTTWO')), user: WRITER },
      { access: 'suggest', update: (room) => suggestionUpdate(room, 4001, WRITER, splitItem('taskItem', 'ZONETASK')), user: WRITER },
      { access: 'suggest', update: (room) => suggestionUpdate(room, 4001, WRITER, enterAtEnd('ZONEALIGN')), user: WRITER },
      {
        access: 'suggest',
        update: (room) => suggestionUpdate(room, 4001, WRITER, (state) => (
          state.tr.split(blockEnd(state.doc, 'ZONEHEADING'), 1, [{ type: paragraphType }])
        )),
        user: WRITER,
      },
    ])).toEqual(['accepted suggestion', 'accepted suggestion', 'accepted suggestion', 'accepted suggestion'])
  })

  // The editor refuses a new block locally when the service would refuse it,
  // so no paste or Enter the editor lets through ends in a rejection.
  const pasteAtEnd = (needle: string, blocks: () => ProseMirrorNode[]) => (state: EditorState) => {
    const end = blockEnd(state.doc, needle)
    return state.tr.replace(end, end, new Slice(Fragment.from(blocks()), 1, 1))
  }
  const node = (name: string, attrs: Record<string, unknown> | null, value: string): ProseMirrorNode => {
    const type = schema.nodes[name]
    const paragraphType = schema.nodes.paragraph
    if (!type || !paragraphType) throw new Error(`schema has no ${name}`)
    return type.isTextblock
      ? type.create(attrs, schema.text(value))
      : type.create(attrs, paragraphType.create(null, schema.text(value)))
  }
  it.each([
    ['two paragraphs pasted at the end of a paragraph', 'delivered', pasteAtEnd('ZONEPLAIN', () => [
      node('paragraph', null, 'One'), node('paragraph', null, 'Two'),
    ])],
    ['a centered paragraph pasted at the end of a paragraph', 'delivered', pasteAtEnd('ZONEPLAIN', () => [
      node('paragraph', null, 'One'), node('paragraph', { textAlign: 'center' }, 'Two'),
    ])],
    ['two paragraphs pasted at the end of a list item', 'delivered', pasteAtEnd('ZONELISTTWO', () => [
      node('paragraph', null, 'One'), node('paragraph', null, 'Two'),
    ])],
    ['three paragraphs pasted at the end of a paragraph', 'node_marks_unsupported', pasteAtEnd('ZONEPLAIN', () => [
      node('paragraph', null, 'One'), node('paragraph', null, 'Two'), node('paragraph', null, 'Three'),
    ])],
    ['a whole paragraph pasted between two paragraphs', 'node_marks_unsupported', (state: EditorState) => {
      const { position } = block(state.doc, 'ZONEPLAIN')
      const after = position + (state.doc.nodeAt(position)?.nodeSize ?? 0)
      return state.tr.replace(after, after, new Slice(Fragment.from(node('paragraph', null, 'Between')), 0, 0))
    }],
    ['Enter at the end of a heading', 'delivered', (state: EditorState) => (
      state.tr.split(blockEnd(state.doc, 'ZONEHEADING'), 1, [{ type: schema.nodes.paragraph! }])
    )],
    ['two headings pasted at the end of a paragraph', 'proposed_block_unsupported', pasteAtEnd('ZONEPLAIN', () => [
      node('heading', { level: 2 }, 'One'), node('heading', { level: 2 }, 'Two'),
    ])],
    ['a paragraph and a heading pasted at the end of a paragraph', 'proposed_block_unsupported', pasteAtEnd('ZONEPLAIN', () => [
      node('paragraph', null, 'One'), node('heading', { level: 2 }, 'Two'),
    ])],
    ['a code block started at the end of a paragraph', 'proposed_block_unsupported', (state: EditorState) => (
      state.tr.split(blockEnd(state.doc, 'ZONEPLAIN'), 1, [{ type: schema.nodes.codeBlock! }])
    )],
    ['a checked task item started at the end of a task item', 'proposed_block_unsupported', (state: EditorState) => (
      state.tr.split(blockEnd(state.doc, 'ZONETASK'), 2, [{ type: schema.nodes.taskItem!, attrs: { checked: true } }, null])
    )],
  ] as const)('is refused by the editor exactly when the service refuses it: %s', async (_name, expected, build) => {
    let editor = 'delivered'
    let update: Uint8Array | null = null
    try {
      update = suggestionUpdate(documentFrom(MATRIX, 1), 4001, WRITER, build)
    } catch (error) {
      if (!(error instanceof UnsupportedSuggestionStructureError)) throw error
      editor = error.reason
    }
    const delivered = update
    const [service] = delivered
      ? await judge(MATRIX, [{ access: 'suggest', update: () => delivered, user: WRITER }])
      : ['not sent']
    expect({ editor, service }).toEqual({
      editor: expected,
      service: expected === 'delivered' ? 'accepted suggestion' : 'not sent',
    })
  })

  it('is refused when a block of the proposed subtree carries a structure suggestion', async () => {
    const [verdict] = await judge(MATRIX, [{
      access: 'suggest',
      user: WRITER,
      update: (room) => editorUpdate(room, 4001, (state) => {
        const ids = { patchId: uuid(), suggestionId: uuid() }
        const mark = schema.markFromJSON(insertionMark(WRITER, ids))
        const listItemType = schema.nodes.listItem
        const paragraphType = schema.nodes.paragraph
        if (!listItemType || !paragraphType) throw new Error('schema has no list item')
        const nested = paragraphType.create(
          {
            [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: structure(WRITER, 'paragraph', {
              patchId: ids.patchId,
              suggestionId: uuid(),
            }),
          },
          schema.text(`${ZERO_WIDTH_SPACE}x`, [mark]),
        )
        const { position } = block(state.doc, 'ZONELISTTWO')
        const itemEnd = state.doc.resolve(position).after(state.doc.resolve(position).depth)
        return state.tr
          .insert(itemEnd, listItemType.create(null, nested))
          .insert(blockEnd(state.doc, 'ZONELISTTWO'), schema.text(ZERO_WIDTH_SPACE, [mark]))
      }),
    }])
    expect(verdict).toBe(POLICY_REFUSAL)
  })

  // Text alignment outside the offered values is refused by the document
  // validation for every writer, before the policy judges the block.
  it.each([
    ['a checked task item', 'taskItem', { checked: true }, POLICY_REFUSAL],
    ['a paragraph with injected style text', 'paragraph', { textAlign: 'left; display: none' }, SCHEMA_REFUSAL],
  ] as const)('is refused as %s', async (_name, nodeName, attrs, refusal) => {
    const [verdict] = await judge(MATRIX, [{
      access: 'suggest',
      user: WRITER,
      update: (room) => editorUpdate(room, 4001, (state) => {
        const ids = { patchId: uuid(), suggestionId: uuid() }
        const mark = schema.markFromJSON(insertionMark(WRITER, ids))
        const paragraphType = schema.nodes.paragraph
        const itemType = schema.nodes.taskItem
        if (!paragraphType || !itemType) throw new Error('schema has no task item')
        const needle = nodeName === 'taskItem' ? 'ZONETASK' : 'ZONEPLAIN'
        const content = paragraphType.create(
          nodeName === 'paragraph' ? attrs : null,
          schema.text(`${ZERO_WIDTH_SPACE}proposed`, [mark]),
        )
        const { position } = block(state.doc, needle)
        const $position = state.doc.resolve(position)
        const insertAt = nodeName === 'taskItem' ? $position.after($position.depth) : position + $position.nodeAfter!.nodeSize
        return state.tr
          .insert(insertAt, nodeName === 'taskItem' ? itemType.create(attrs, content) : content)
          .insert(blockEnd(state.doc, needle), schema.text(ZERO_WIDTH_SPACE, [mark]))
      }),
    }])
    expect(verdict).toBe(refusal)
  })

  it.each([
    ['without a proposed break at the end of the block before it', 'no-anchor'],
    ['anchored to the start of the block after it', 'backward'],
    ['as another person', 'foreign-author'],
    ['as a heading', 'heading'],
    ['carrying original text of the block it splits', 'moves-text'],
  ] as const)('is refused %s', async (_name, shape) => {
    const [verdict] = await judge(MATRIX, [{
      access: 'suggest',
      user: WRITER,
      update: (room) => editorUpdate(room, 4001, (state) => {
        const ids = { patchId: uuid(), suggestionId: uuid() }
        const author = shape === 'foreign-author' ? REVIEWER : WRITER
        const mark = schema.markFromJSON(insertionMark(author, ids))
        const paragraphType = schema.nodes.paragraph
        const headingType = schema.nodes.heading
        if (!paragraphType || !headingType) throw new Error('schema has no paragraph')
        const end = blockEnd(state.doc, 'ZONEPLAIN')
        if (shape === 'moves-text') {
          const lastEnd = blockEnd(state.doc, 'ZONELAST')
          const splitAt = lastEnd - 'LAST.'.length
          return state.tr
            .split(splitAt)
            .insert(splitAt + 2, schema.text(ZERO_WIDTH_SPACE, [mark]))
            .insert(splitAt, schema.text(ZERO_WIDTH_SPACE, [mark]))
        }
        const content = schema.text(`${ZERO_WIDTH_SPACE}proposed`, [mark])
        const proposed = shape === 'heading'
          ? headingType.create({ level: 2 }, content)
          : paragraphType.create(null, content)
        if (shape === 'backward') {
          return state.tr.insert(1, schema.text(ZERO_WIDTH_SPACE, [mark])).insert(0, proposed)
        }
        const transaction = state.tr.insert(end + 1, proposed)
        return shape === 'no-anchor' ? transaction : transaction.insert(end, schema.text(ZERO_WIDTH_SPACE, [mark]))
      }),
    }])
    expect(verdict).toBe(POLICY_REFUSAL)
  })
})

describe('a block join in suggest mode', () => {
  const JOINS = Y.encodeStateAsUpdate(editorJsonToYDoc(parseEditorMarkdown([
    '# Heading ZONEJOINHEADING',
    '',
    'First ZONEJOINFIRST.',
    '',
    'Second ZONEJOINSECOND.',
    '',
    '- Bullet ZONEJOINBULLET',
    '- Bullet two ZONEJOINBULLETTWO',
    '  - Nested ZONEJOINNESTED',
    '',
    '1. Ordered ZONEJOINORDERED',
    '2. Ordered two',
  ].join('\n'))))

  // Backspace at the start and Delete at the end of a text block, as the
  // editor's keymap runs them.
  type KeyCommand = (state: EditorState, dispatch: (transaction: Transaction) => void) => boolean
  const key = (run: KeyCommand, needle: string, where: 'start' | 'end') => (state: EditorState): Transaction => {
    const { node, position } = block(state.doc, needle)
    const placed = state.apply(state.tr.setSelection(TextSelection.create(
      state.doc,
      where === 'start' ? position + 1 : position + 1 + node.content.size,
    )))
    let result: Transaction | null = null
    run(placed, (transaction) => {
      result = transaction
    })
    if (!result) throw new Error(`the key did nothing at ${needle}`)
    // Carry the selection step into a transaction on the original state, so
    // the transform sees the same document the key changed.
    const combined = state.tr.setSelection(placed.selection)
    for (const step of (result as Transaction).steps) combined.step(step)
    return combined
  }

  it.each([
    ['Backspace at the start of a paragraph after a paragraph', 'delivered', key(joinBackward, 'ZONEJOINSECOND', 'start')],
    ['Backspace at the start of a paragraph after a heading', 'delivered', key(joinBackward, 'ZONEJOINFIRST', 'start')],
    ['Delete at the end of a paragraph', 'delivered', key(joinForward, 'ZONEJOINFIRST', 'end')],
    ['Backspace that joins an ordered list to the bullet list before it', 'unsupported_structure', key(joinBackward, 'ZONEJOINORDERED', 'start')],
    ['Backspace at the start of a nested list item', 'unsupported_structure', key(joinBackward, 'ZONEJOINNESTED', 'start')],
    ['Delete at the end of a list item that holds a nested list', 'unsupported_structure', key(joinForward, 'ZONEJOINBULLETTWO', 'end')],
  ] as const)('is refused by the editor exactly when the service refuses it: %s', async (_name, expected, build) => {
    let editor = 'delivered'
    let update: Uint8Array | null = null
    try {
      update = suggestionUpdate(documentFrom(JOINS, 1), 4001, WRITER, build)
    } catch (error) {
      if (!(error instanceof UnsupportedSuggestionStructureError)) throw error
      editor = error.reason
    }
    const delivered = update
    const [service] = delivered
      ? await judge(JOINS, [{ access: 'suggest', update: () => delivered, user: WRITER }])
      : ['not sent']
    expect({ editor, service }).toEqual({
      editor: expected,
      service: expected === 'delivered' ? 'accepted suggestion' : 'not sent',
    })
  })

  it.each([
    ['directly after the join', 'adjacent_own_suggestion_conflict', (state: EditorState) => {
      const { position } = block(state.doc, 'ZONEJOINSECOND')
      // The second block starts with the join's deleted marker character.
      return state.tr.delete(position + 2, position + 5)
    }],
    ['directly before the join', 'adjacent_own_suggestion_conflict', (state: EditorState) => {
      const { node, position } = block(state.doc, 'ZONEJOINFIRST')
      // The first block ends with the join's deleted marker character.
      const end = position + node.content.size
      return state.tr.delete(end - 3, end)
    }],
    ['away from the join', 'delivered', (state: EditorState) => {
      const { position } = block(state.doc, 'ZONEJOINSECOND')
      return state.tr.delete(position + 9, position + 12)
    }],
    ['formatting that takes in the marker at the end of the first block', 'unsupported_structure', (state: EditorState) => {
      const { node, position } = block(state.doc, 'ZONEJOINFIRST')
      const end = position + 1 + node.content.size
      return state.tr.addMark(end - 8, end, schema.marks.bold!.create())
    }],
    ['formatting that ends right before that marker', 'adjacent_own_suggestion_conflict', (state: EditorState) => {
      const { node, position } = block(state.doc, 'ZONEJOINFIRST')
      const end = position + node.content.size
      return state.tr.addMark(end - 7, end, schema.marks.bold!.create())
    }],
  ] as const)('is followed by a change %s exactly as the service judges it', async (_name, expected, build) => {
    const room = await openRoom(JOINS)
    try {
      const joinUpdate = suggestionUpdate(room.document, 4001, WRITER, key(joinBackward, 'ZONEJOINSECOND', 'start'))
      expect(await room.submit({ access: 'suggest', update: () => joinUpdate, user: WRITER })).toBe('accepted suggestion')
      let editor = 'delivered'
      let update: Uint8Array | null = null
      try {
        update = suggestionUpdate(room.document, 4001, WRITER, build)
      } catch (error) {
        if (!(error instanceof UnsupportedSuggestionStructureError)) throw error
        editor = error.reason
      }
      const delivered = update
      const service = delivered
        ? await room.submit({ access: 'suggest', update: () => delivered, user: WRITER })
        : 'not sent'
      expect({ editor, service }).toEqual({
        editor: expected,
        service: expected === 'delivered' ? 'accepted suggestion' : 'not sent',
      })
    } finally {
      room.document.destroy()
      await room.close()
    }
  })

  it('is refused by the service in the shape the suggestion transform gives a list join', async () => {
    // The whole list item the transform adds for this join holds only a
    // deleted marker character; the editor must never send it.
    const [verdict] = await judge(JOINS, [{
      access: 'suggest',
      user: WRITER,
      update: (room) => editorUpdate(room, 4001, (state) => {
        const listItemType = schema.nodes.listItem
        const paragraphType = schema.nodes.paragraph
        if (!listItemType || !paragraphType) throw new Error('schema has no list item')
        const ids = { patchId: uuid(), suggestionId: uuid() }
        const marker = schema.text(ZERO_WIDTH_SPACE, [schema.markFromJSON({
          type: 'deletion',
          attrs: { authorId: WRITER, createdAt: CREATED_AT, id: ids.suggestionId, kind: 'deletion', ...ids },
        })])
        let bulletEnd = -1
        state.doc.forEach((node, offset) => {
          if (node.type.name === 'bulletList') bulletEnd = offset + node.nodeSize - 1
        })
        return state.tr.insert(bulletEnd, listItemType.create(null, paragraphType.create(null, marker)))
      }),
    }])
    expect(verdict).toBe(POLICY_REFUSAL)
  })
})

describe('the attributes of a proposed block', () => {
  // Attribute names are data. A name such as `__proto__` does not show up as
  // an own key of a plain object built from the attributes, so the document
  // validation and the allowlist read the attributes themselves. The
  // validation, which runs first for every writer, refuses both names.
  it.each(['__proto__', 'constructor'])('refuse an attribute named %s', async (name) => {
    const [verdict] = await judge(MATRIX, [{
      access: 'edit',
      user: WRITER,
      update: (room) => {
        const update = suggestionUpdate(room, 4001, WRITER, (state) => (
          state.tr.split(blockEnd(state.doc, 'ZONEPLAIN'))
        ))
        const client = fork(room, 4001)
        Y.applyUpdate(client, update)
        const vector = Y.encodeStateVector(room)
        const fragment = client.getXmlFragment(EDITOR_YJS_FRAGMENT)
        const proposed = fragment.get(topLevelIndex(fragment, 'ZONEPLAIN') + 1)
        if (!(proposed instanceof Y.XmlElement)) throw new Error('the split proposed no block')
        client.transact(() => proposed.setAttribute(name, 'left; display: none' as never))
        const combined = Y.encodeStateAsUpdate(client, vector)
        client.destroy()
        return combined
      },
    }])
    expect(verdict).toBe(SCHEMA_REFUSAL)
  })
})

describe('a new block chained backwards', () => {
  // The new block follows a known block that does not end with its
  // insertion, and the insertion continues at the start of the block after
  // it. Rejecting everything would join the two again, so the original
  // document comes back; only the chain to the block before refuses it.
  it('is refused behind a block that does not end with its insertion', async () => {
    const [verdict] = await judge(MATRIX, [{
      access: 'suggest',
      user: WRITER,
      update: (room) => rawUpdate(room, 4001, (fragment) => {
        const index = topLevelIndex(fragment, 'ZONELAST')
        const following = fragment.get(index)
        const followingText = following instanceof Y.XmlElement ? following.get(0) : null
        if (!(followingText instanceof Y.XmlText)) throw new Error('fixture has no text')
        const ids = { patchId: uuid(), suggestionId: uuid() }
        const insertion = { authorId: WRITER, createdAt: CREATED_AT, id: ids.suggestionId, kind: 'insertion', ...ids }
        const proposed = new Y.XmlElement('paragraph')
        const proposedText = new Y.XmlText()
        proposed.insert(0, [proposedText])
        fragment.insert(index, [proposed])
        proposedText.insert(0, 'x', { insertion })
        followingText.insert(0, ZERO_WIDTH_SPACE, { insertion })
      }),
    }])
    expect(verdict).toBe(POLICY_REFUSAL)
  })
})

describe("another person's structure suggestion", () => {
  function foreignStructureElement(fragment: Y.XmlFragment): Y.XmlElement {
    const element = fragment.get(topLevelIndex(fragment, 'ZONESTRUCT'))
    if (!(element instanceof Y.XmlElement)) throw new Error('fixture has no structure block')
    return element
  }

  function rewrite(change: Record<string, unknown>) {
    return (fragment: Y.XmlFragment): void => {
      const element = foreignStructureElement(fragment)
      const data = element.getAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR) as unknown as Record<string, unknown>
      element.setAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR, { ...data, ...change } as never)
    }
  }

  function withOwnInsertion(mutate: (fragment: Y.XmlFragment) => void, authorId: string) {
    return (fragment: Y.XmlFragment): void => {
      mutate(fragment)
      const target = fragment.get(topLevelIndex(fragment, 'ZONEPLAIN'))
      const content = target instanceof Y.XmlElement ? target.get(0) : null
      if (!(content instanceof Y.XmlText)) throw new Error('fixture has no text')
      const ids = { patchId: uuid(), suggestionId: uuid() }
      content.insert(content.length, ' legit', {
        insertion: { authorId, createdAt: CREATED_AT, id: ids.suggestionId, kind: 'insertion', ...ids },
      })
    }
  }

  it.each([
    ['its action rewritten by a suggest-only lease', withOwnInsertion(rewrite({ action: 'bulletList' }), THIRD)],
    ['a discarded command added by a suggest-only lease', withOwnInsertion(rewrite({ discardedCommand: { offset: 0, text: 'PLANTED ' } }), THIRD)],
  ] as const)('refuses %s', async (_name, mutate) => {
    const [verdict] = await judge(MATRIX, [{ access: 'suggest', update: (room) => rawUpdate(room, 4001, mutate), user: THIRD }])
    expect(verdict).toBe(POLICY_REFUSAL)
  })

  // An edit lease never rewrites another person's suggestion: the service
  // removes it instead, in the writer's own row.
  it.each([
    ['its action rewritten', rewrite({ action: 'codeBlock' })],
    ['a discarded command added', rewrite({ discardedCommand: { offset: 0, text: 'PLANTED ' } })],
  ] as const)('is removed, not rewritten, when an edit lease sends it with %s', async (_name, mutate) => {
    expect(await editOfStructure(MATRIX, (room) => rawUpdate(room, 4001, mutate), 'ZONESTRUCT')).toEqual({
      data: null,
      rejected: [FOREIGN_STRUCTURE.suggestionId],
      verdict: 'accepted direct',
    })
  })

  it.each([
    ['an inline formula', () => {
      const inlineMath = schema.nodes.inlineMath
      if (!inlineMath) throw new Error('schema has no inline math')
      return inlineMath.create({ latex: 'x' })
    }],
    ['a hard break', () => {
      const hardBreak = schema.nodes.hardBreak
      if (!hardBreak) throw new Error('schema has no hard break')
      return hardBreak.create()
    }],
  ] as const)('stays pending on its block when an edit lease inserts %s into the block', async (_name, atom) => {
    // A structure suggestion stands on its block: editing the text of the
    // block directly leaves the proposal as its author made it.
    expect(await editOfStructure(MATRIX, (room) => editorUpdate(room, 4001, (state) => (
      state.tr.insert(block(state.doc, 'ZONESTRUCT').position + 3, atom())
    )), 'ZONESTRUCT')).toEqual({
      data: structure(REVIEWER, 'heading2', FOREIGN_STRUCTURE),
      rejected: [],
      verdict: 'accepted direct',
    })
  })

  it('stays pending on its block when an edit lease deletes text in front of its discarded slash command', async () => {
    // The command goes back where it was typed, or at the end of the text
    // that is left; the original projection of the document stays defined.
    const discarded = { offset: 'Structured ZONESTRUCT.'.length, text: '/heading' }
    const withCommand = documentFrom(MATRIX, 1)
    withCommand.transact(() => {
      const fragment = withCommand.getXmlFragment(EDITOR_YJS_FRAGMENT)
      foreignStructureElement(fragment).setAttribute(
        INQTRIX_STRUCTURE_SUGGESTION_ATTR,
        { ...structure(REVIEWER, 'heading2', FOREIGN_STRUCTURE), discardedCommand: discarded } as never,
      )
    })
    const initial = Y.encodeStateAsUpdate(withCommand)
    withCommand.destroy()
    const room = await openRoom(initial)
    try {
      expect(await room.submit({
        access: 'edit',
        user: WRITER,
        update: (current) => editorUpdate(current, 4001, (state) => {
          const { position } = block(state.doc, 'ZONESTRUCT')
          return state.tr.delete(position + 1, position + 1 + 'Structured '.length)
        }),
      })).toBe('accepted direct')
      expect(room.api.persisted.at(-1)?.rejectedSuggestionIds).toEqual([])
      const document = editorYDocToJson(room.document)
      expect(collectSuggestionRecords(document).has(FOREIGN_STRUCTURE.suggestionId)).toBe(true)
      const original = projectOriginalJson(schema.nodeFromJSON(document))
      expect((original.content ?? []).map((node) => (node.content ?? []).map((child) => child.text ?? '').join('')))
        .toContain('ZONESTRUCT./heading')
    } finally {
      room.document.destroy()
      await room.close()
    }
  })

  it('is removed, not moved, when an edit lease places a copy after it and deletes the original', async () => {
    expect(await editOfStructure(MATRIX, (room) => rawUpdate(room, 4001, (fragment) => {
      const index = topLevelIndex(fragment, 'ZONESTRUCT')
      const original = foreignStructureElement(fragment)
      const copy = new Y.XmlElement('paragraph')
      for (const [key, value] of Object.entries(original.getAttributes())) {
        copy.setAttribute(key, value as never)
      }
      const content = new Y.XmlText()
      copy.insert(0, [content])
      fragment.insert(index + 1, [copy])
      const originalText = original.get(0)
      if (!(originalText instanceof Y.XmlText)) throw new Error('fixture has no text')
      content.applyDelta(originalText.toDelta())
      fragment.delete(index, 1)
    }), 'ZONESTRUCT')).toEqual({
      data: null,
      rejected: [FOREIGN_STRUCTURE.suggestionId],
      verdict: 'accepted direct',
    })
  })

  it('still accepts formatting inside it and inside an own structure suggestion', async () => {
    const bold = schema.marks.bold
    if (!bold) throw new Error('schema has no bold mark')
    expect(await judge(MATRIX, [
      {
        access: 'edit',
        user: WRITER,
        update: (room) => editorUpdate(room, 4001, (state) => {
          const { position } = block(state.doc, 'ZONESTRUCT')
          return state.tr.addMark(position + 1, position + 11, bold.create())
        }),
      },
      {
        access: 'edit',
        user: WRITER,
        update: (room) => editorUpdate(room, 4001, (state) => {
          const { position } = block(state.doc, 'ZONEOWNSTRUCT')
          return state.tr.addMark(position + 1, position + 4, bold.create())
        }),
      },
    ])).toEqual(['accepted direct', 'accepted direct'])
  })
})

describe('pending suggestions are followed by identity', () => {
  const paragraphWith = (value: string): ProseMirrorNode => {
    const paragraphType = schema.nodes.paragraph
    if (!paragraphType) throw new Error('schema has no paragraph')
    return paragraphType.create(null, schema.text(value))
  }

  it.each([
    ['before the first block', (state: EditorState) => state.tr.insert(0, paragraphWith('New first'))],
    ['after the first block', (state: EditorState) => state.tr.insert(state.doc.child(0).nodeSize, paragraphWith('New second'))],
    ['after the last block', (state: EditorState) => state.tr.insert(state.doc.content.size, paragraphWith('New last'))],
  ] as const)('so an edit lease may add a paragraph %s next to structure suggestions', async (_name, build) => {
    expect(await judge(EDGES, [{
      access: 'edit',
      update: (room) => editorUpdate(room, 4001, build),
      user: WRITER,
    }])).toEqual(['accepted direct'])
  })

  it('so an edit lease may type directly before its own pending insertion', async () => {
    expect(await judge(MATRIX, [{
      access: 'edit',
      user: WRITER,
      update: (room) => editorUpdate(room, 4001, (state) => (
        state.tr.insertText('x', blockEnd(state.doc, 'ZONEOWN') - ' OWNTEXT'.length)
      )),
    }])).toEqual(['accepted direct'])
  })

  it('so a copy of the first block placed after it does not take over its structure suggestion, which is removed', async () => {
    expect(await editOfStructure(EDGES, (room) => rawUpdate(room, 4001, (fragment) => {
      const original = fragment.get(0)
      if (!(original instanceof Y.XmlElement)) throw new Error('fixture has no first block')
      const copy = new Y.XmlElement('paragraph')
      for (const [key, value] of Object.entries(original.getAttributes())) {
        copy.setAttribute(key, value as never)
      }
      const content = new Y.XmlText()
      copy.insert(0, [content])
      fragment.insert(1, [copy])
      const originalText = original.get(0)
      if (!(originalText instanceof Y.XmlText)) throw new Error('fixture has no text')
      content.applyDelta(originalText.toDelta())
      fragment.delete(0, 1)
    }), 'ZONEFIRST')).toEqual({
      data: null,
      rejected: [FOREIGN_STRUCTURE.suggestionId],
      verdict: 'accepted direct',
    })
  })

  it('so a copy of the last block placed before it does not take over its structure suggestion, which is removed', async () => {
    expect(await editOfStructure(EDGES, (room) => rawUpdate(room, 4001, (fragment) => {
      const index = fragment.length - 1
      const original = fragment.get(index)
      if (!(original instanceof Y.XmlElement)) throw new Error('fixture has no last block')
      const copy = new Y.XmlElement('paragraph')
      for (const [key, value] of Object.entries(original.getAttributes())) {
        copy.setAttribute(key, value as never)
      }
      const content = new Y.XmlText()
      copy.insert(0, [content])
      fragment.insert(index, [copy])
      const originalText = original.get(0)
      if (!(originalText instanceof Y.XmlText)) throw new Error('fixture has no text')
      content.applyDelta(originalText.toDelta())
      fragment.delete(index + 1, 1)
    }), 'ZONEEDGELAST')).toEqual({
      data: null,
      rejected: ['cccccccc-0000-4000-8000-000000000002'],
      verdict: 'accepted direct',
    })
  })

  it('so two suggest-only writers can type at the end of one paragraph at the same time', async () => {
    expect(await judge(MATRIX, [
      {
        access: 'suggest',
        user: REVIEWER,
        update: () => suggestionUpdate(documentFrom(MATRIX, 1), 4002, REVIEWER, (state) => (
          state.tr.insertText(' first', blockEnd(state.doc, 'ZONEPLAIN'))
        )),
      },
      {
        access: 'suggest',
        user: WRITER,
        update: () => suggestionUpdate(documentFrom(MATRIX, 1), 4001, WRITER, (state) => (
          state.tr.insertText(' second', blockEnd(state.doc, 'ZONEPLAIN'))
        )),
      },
    ])).toEqual(['accepted suggestion', 'accepted suggestion'])
  })
})

describe('the deletions of a suggest-only update', () => {
  // Deletes one original character and creates the same character again,
  // unmarked, beside a legitimate proposal. The text reads the same, but the
  // original character is gone: the identity every comment anchor and every
  // other person's pending change refers to.
  function swapOriginalCharacter(authorId: string) {
    return (fragment: Y.XmlFragment): void => {
      const target = fragment.get(topLevelIndex(fragment, 'ZONELAST'))
      const content = target instanceof Y.XmlElement ? target.get(0) : null
      if (!(content instanceof Y.XmlText)) throw new Error('fixture has no text')
      const character = content.toString().charAt(2)
      content.delete(2, 1)
      content.insert(2, character, {})
      const ids = { patchId: uuid(), suggestionId: uuid() }
      content.insert(content.length, ' legit', {
        insertion: { authorId, createdAt: CREATED_AT, id: ids.suggestionId, kind: 'insertion', ...ids },
      })
    }
  }

  it('may not remove an original character, not even one written again', async () => {
    const [verdict] = await judge(MATRIX, [{
      access: 'suggest',
      update: (room) => rawUpdate(room, 4001, swapOriginalCharacter(WRITER)),
      user: WRITER,
    }])
    expect(verdict).toBe('4403 suggestion_policy_violation')
  })

  it('bind only a suggest-only lease; an edit lease may change content directly anyway', async () => {
    expect(await judge(MATRIX, [{
      access: 'edit',
      update: (room) => rawUpdate(room, 4001, swapOriginalCharacter(WRITER)),
      user: WRITER,
    }])).toEqual(['accepted suggestion'])
  })

  it('may remove characters of an own pending insertion', async () => {
    expect(await judge(MATRIX, [{
      access: 'suggest',
      user: WRITER,
      update: (room) => suggestionUpdate(room, 4001, WRITER, (state) => {
        const end = blockEnd(state.doc, 'ZONEOWN')
        return state.tr.delete(end - 2, end - 1)
      }),
    }])).toEqual(['accepted suggestion'])
  })

  it('are judged by the work of the update, not by the deletion history of the document', () => {
    // Two documents with the same content; one carries a long deletion
    // history in another paragraph. The rule judges only what the update
    // deletes, so its cost must not grow with that history.
    const fresh = documentFrom(MATRIX, 1)
    const aged = documentFrom(MATRIX, 1)
    const history = aged.getXmlFragment(EDITOR_YJS_FRAGMENT)
    const historyBlock = history.get(topLevelIndex(history, 'ZONEHEADING'))
    const historyText = historyBlock instanceof Y.XmlElement ? historyBlock.get(0) : null
    if (!(historyText instanceof Y.XmlText)) throw new Error('fixture has no text')
    aged.transact(() => {
      for (let index = 0; index < 100_000; index += 1) historyText.insert(0, 'h')
    })
    aged.transact(() => historyText.delete(0, 100_000))

    const measure = (base: Y.Doc): { judged: number; milliseconds: number } => {
      const client = fork(base, 4001)
      const fragment = client.getXmlFragment(EDITOR_YJS_FRAGMENT)
      const initialized = initProseMirrorDoc(fragment, schema)
      const state = EditorState.create({ doc: initialized.doc, schema })
      const end = blockEnd(state.doc, 'ZONEOWN')
      const target = transformToInqtrixSuggestionTransaction(
        state.tr.delete(end - 2, end - 1),
        state,
        { authorId: WRITER, createdAt: CREATED_AT, patchId: uuid() },
        uuid,
      ).doc
      const vector = Y.encodeStateVector(client)
      client.transact(() => updateYFragment(client, fragment, target, initialized.meta))
      const update = Y.encodeStateAsUpdate(client, vector)
      const after = fork(base, 1)
      let deleted: Y.Transaction['deleteSet'] | null = null
      after.on('afterTransaction', (transaction: Y.Transaction) => {
        deleted ??= transaction.deleteSet
      })
      Y.applyUpdate(after, update)
      if (!deleted) throw new Error('the update deleted nothing')
      const records = collectSuggestionRecords(editorYDocToJson(base))
      const samples: number[] = []
      let judged = 0
      for (let run = 0; run < 7; run += 1) {
        const started = performance.now()
        judged = assertSuggestionDeleteSet(
          { afterDocument: after, beforeDocument: base, deletedByUpdate: deleted },
          records,
          WRITER,
        ).judged
        samples.push(performance.now() - started)
      }
      samples.sort((left, right) => left - right)
      client.destroy()
      after.destroy()
      return { judged, milliseconds: samples[3]! }
    }

    const freshCost = measure(fresh)
    const agedCost = measure(aged)
    expect(agedCost.judged).toBe(freshCost.judged)
    expect(agedCost.judged).toBe(1)
    // Judging one deleted character takes microseconds on either document;
    // walking 100 000 deleted items takes more than a millisecond. The
    // bounds leave two orders of magnitude for a loaded host.
    expect(agedCost.milliseconds).toBeLessThan(freshCost.milliseconds * 10 + 0.05)
    expect(agedCost.milliseconds).toBeLessThan(0.5)
    fresh.destroy()
    aged.destroy()
  })

  it('read the text they come from once, not once per deleted character', async () => {
    // An own insertion made of many separate items, as typing at one
    // position or alternating formatting leaves it, is removed down to its
    // first character in one update. Finding the formatting of each deleted
    // character by walking to the left from it again would cost the square
    // of the insertion's length and stall the service for every room.
    const separateItems = 3_000
    const room = documentFrom(MATRIX, 1)
    const fragment = room.getXmlFragment(EDITOR_YJS_FRAGMENT)
    const owned = fragment.get(topLevelIndex(fragment, 'ZONEOWN'))
    const ownText = owned instanceof Y.XmlElement ? owned.get(0) : null
    if (!(ownText instanceof Y.XmlText)) throw new Error('fixture has no text')
    const ownPart = (ownText.toDelta() as Array<{ attributes?: Record<string, unknown>; insert: unknown }>)
      .find((part) => part.attributes?.insertion)
    if (!ownPart?.attributes) throw new Error('fixture has no own insertion')
    const insideOwn = 'Own ZONEOWN. '.length
    room.transact(() => {
      for (let index = 0; index < separateItems; index += 1) {
        ownText.insert(insideOwn, 'h', { ...ownPart.attributes })
      }
    })
    const deleteTyped = (current: Y.Doc) => rawUpdate(current, 4001, (clientFragment) => {
      const block = clientFragment.get(topLevelIndex(clientFragment, 'ZONEOWN'))
      const content = block instanceof Y.XmlElement ? block.get(0) : null
      if (!(content instanceof Y.XmlText)) throw new Error('fixture has no text')
      content.delete(insideOwn, separateItems)
    })

    const after = fork(room, 1)
    const deletions: Array<Y.Transaction['deleteSet']> = []
    after.on('afterTransaction', (transaction: Y.Transaction) => {
      deletions.push(transaction.deleteSet)
    })
    Y.applyUpdate(after, deleteTyped(room))
    const cost = assertSuggestionDeleteSet(
      { afterDocument: after, beforeDocument: room, deletedByUpdate: Y.mergeDeleteSets(deletions) },
      collectSuggestionRecords(editorYDocToJson(room)),
      WRITER,
    )
    after.destroy()
    expect(cost.judged).toBe(separateItems)
    // Each item of the text once, plus the one item each walk stops at.
    expect(cost.read).toBeLessThanOrEqual(2 * separateItems)

    const verdicts = await judge(Y.encodeStateAsUpdate(room), [
      { access: 'suggest', update: deleteTyped, user: WRITER },
    ])
    room.destroy()
    expect(verdicts).toEqual(['accepted suggestion'])
  })
})

describe('structure suggestions stay applicable', () => {
  // A code block holds plain text only, and a list item must start with a
  // paragraph. A structure suggestion that cannot be applied would make the
  // final projection of the document, and with it every later change
  // summary, fail for everyone in the room.
  const CODE_BLOCK = { patchId: 'ffffffff-0000-4000-8000-0000000000f1', suggestionId: 'ffffffff-0000-4000-8000-000000000001' }

  function plantCodeBlockOnListItem(fragment: Y.XmlFragment): void {
    const list = fragment.get(topLevelIndex(fragment, 'ZONELISTONE'))
    const item = list instanceof Y.XmlElement ? list.get(0) : null
    const itemParagraph = item instanceof Y.XmlElement ? item.get(0) : null
    if (!(itemParagraph instanceof Y.XmlElement)) throw new Error('fixture has no list item paragraph')
    itemParagraph.setAttribute(
      INQTRIX_STRUCTURE_SUGGESTION_ATTR,
      structure(WRITER, 'codeBlock', CODE_BLOCK) as never,
    )
  }

  it('so a suggestion that cannot be applied is refused', async () => {
    const [verdict] = await judge(MATRIX, [{
      access: 'suggest',
      update: (room) => rawUpdate(room, 4001, plantCodeBlockOnListItem),
      user: WRITER,
    }])
    expect(verdict).toBe('4403 suggestion_policy_violation')
  })

  it('so a direct edit that makes a pending suggestion inapplicable is refused', async () => {
    const bold = schema.marks.bold
    if (!bold) throw new Error('schema has no bold mark')
    const proposed = Y.encodeStateAsUpdate(editorJsonToYDoc({
      type: 'doc',
      content: [
        { type: 'heading', attrs: { level: 2, [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: structure(REVIEWER, 'codeBlock', CODE_BLOCK) }, content: [text('Proposed as code ZONECODE')] },
        paragraph([text('After ZONEAFTER.')]),
      ],
    }))
    expect(await judge(proposed, [
      {
        access: 'edit',
        user: WRITER,
        update: (room) => editorUpdate(room, 4001, (state) => {
          const { position } = block(state.doc, 'ZONECODE')
          return state.tr.addMark(position + 1, position + 9, bold.create())
        }),
      },
      {
        access: 'edit',
        user: WRITER,
        update: (room) => editorUpdate(room, 4001, (state) => (
          state.tr.insertText('!', blockEnd(state.doc, 'ZONEAFTER'))
        )),
      },
    ])).toEqual(['4403 suggestion_policy_violation', 'accepted direct'])
  })

  it.each([
    ['bold in edit mode inside a block another person proposed as a code block', 'edit', REVIEWER, 'ZONECODE', 'structure_suggestion_inapplicable'],
    ['bold in suggest mode inside a block the writer proposed as a code block', 'suggest', WRITER, 'ZONECODE', 'structure_suggestion_inapplicable'],
    ['bold in edit mode in a block without a proposal', 'edit', REVIEWER, 'ZONEAFTER', 'delivered'],
  ] as const)('so the editor refuses locally what the service refuses: %s', async (_name, mode, proposer, needle, expected) => {
    const bold = schema.marks.bold
    if (!bold) throw new Error('schema has no bold mark')
    const proposed = Y.encodeStateAsUpdate(editorJsonToYDoc({
      type: 'doc',
      content: [
        paragraph([text('Proposed as code ZONECODE')], { [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: structure(proposer, 'codeBlock', CODE_BLOCK) }),
        paragraph([text('After ZONEAFTER.')]),
      ],
    }))
    const addBold = (state: EditorState): Transaction => {
      const { position } = block(state.doc, needle)
      return state.tr.addMark(position + 1, position + 9, bold.create())
    }
    let editor = 'delivered'
    let update: Uint8Array | null = null
    try {
      update = mode === 'suggest'
        ? suggestionUpdate(documentFrom(proposed, 1), 4001, WRITER, addBold)
        : editorUpdate(documentFrom(proposed, 1), 4001, (state) => {
            const transaction = addBold(state)
            assertDirectEditKeepsSuggestionRecords(transaction, WRITER)
            return transaction
          })
    } catch (error) {
      if (!(error instanceof UnsupportedSuggestionStructureError)) throw error
      editor = error.reason
    }
    const delivered = update
    const [service] = delivered
      ? await judge(proposed, [{ access: mode, update: () => delivered, user: WRITER }])
      : ['not sent']
    expect({ editor, service }).toEqual({
      editor: expected,
      service: expected === 'delivered' ? 'accepted direct' : 'not sent',
    })
  })

  it('so a stored suggestion that cannot be applied refuses only new suggestions until it is rejected', async () => {
    const stored = documentFrom(MATRIX, 1)
    stored.transact(() => plantCodeBlockOnListItem(stored.getXmlFragment(EDITOR_YJS_FRAGMENT)))
    const room = await openRoom(Y.encodeStateAsUpdate(stored))
    stored.destroy()
    try {
      const directElsewhere = await room.submit({
        access: 'edit',
        user: WRITER,
        update: (current) => editorUpdate(current, 4001, (state) => (
          state.tr.insertText('!', blockEnd(state.doc, 'ZONELAST'))
        )),
      })
      const suggestionElsewhere = await room.submit({
        access: 'suggest',
        user: REVIEWER,
        update: (current) => suggestionUpdate(current, 4002, REVIEWER, (state) => (
          state.tr.insertText(' more', blockEnd(state.doc, 'ZONEPLAIN'))
        )),
      })
      expect(directElsewhere).toBe('accepted direct')
      // The change summary cannot project such a document; the update is
      // refused for its sender alone, and the room stays usable.
      expect(suggestionElsewhere).toBe('4409 invalid_schema')
      expect(room.coordinator.requiresReconstruction(ROOM)).toBe(false)
      expect(await room.decide('reject', [CODE_BLOCK.patchId])).toBe('accepted decision')
      expect(await room.submit({
        access: 'suggest',
        user: REVIEWER,
        update: (current) => suggestionUpdate(current, 4002, REVIEWER, (state) => (
          state.tr.insertText(' more', blockEnd(state.doc, 'ZONEPLAIN'))
        )),
      })).toBe('accepted suggestion')
    } finally {
      room.document.destroy()
      await room.close()
    }
  })
})

describe('a local failure before persistence', () => {
  function recordingLogger(): { logger: SidecarLogger; warnings: Array<[string, unknown]> } {
    const warnings: Array<[string, unknown]> = []
    return {
      logger: { ...silentLogger, warn: (event, fields) => warnings.push([event, fields]) },
      warnings,
    }
  }

  it('refuses a client update for its sender only, without rebuilding the room', async () => {
    const { logger, warnings } = recordingLogger()
    const room = await openRoom(MATRIX, logger)
    try {
      const summary = vi.spyOn(changeSummaryModule, 'summarizeEditorChange')
        .mockImplementationOnce(() => {
          throw new RangeError('summary failed')
        })
      const failed = await room.submit({
        access: 'suggest',
        user: REVIEWER,
        update: (current) => suggestionUpdate(current, 4002, REVIEWER, (state) => (
          state.tr.insertText(' more', blockEnd(state.doc, 'ZONEPLAIN'))
        )),
      })
      expect(failed).toBe('4409 invalid_schema')
      expect(room.api.persisted).toHaveLength(0)
      expect(room.coordinator.requiresReconstruction(ROOM)).toBe(false)
      expect(() => room.coordinator.assertJoinAllowed(ROOM)).not.toThrow()
      expect(warnings).toContainEqual([
        'change_summary_failed',
        expect.objectContaining({ cause: 'RangeError', path: 'client' }),
      ])
      expect(await room.submit({
        access: 'edit',
        user: WRITER,
        update: (current) => editorUpdate(current, 4001, (state) => (
          state.tr.insertText('!', blockEnd(state.doc, 'ZONELAST'))
        )),
      })).toBe('accepted direct')
      expect(summary).toHaveBeenCalledTimes(2)
    } finally {
      room.document.destroy()
      await room.close()
    }
  })

  it('refuses a decision without rebuilding the room', async () => {
    const { logger, warnings } = recordingLogger()
    const room = await openRoom(MATRIX, logger)
    try {
      vi.spyOn(changeSummaryModule, 'summarizeEditorChange').mockImplementationOnce(() => {
        throw new RangeError('summary failed')
      })
      expect(await room.decide('accept', [FOREIGN_INSERTION.patchId])).toBe('4409 decision_conflict')
      expect(room.api.persisted).toHaveLength(0)
      expect(room.coordinator.requiresReconstruction(ROOM)).toBe(false)
      expect(warnings).toContainEqual([
        'change_summary_failed',
        expect.objectContaining({ cause: 'RangeError', path: 'decision' }),
      ])
      expect(await room.decide('accept', [FOREIGN_INSERTION.patchId])).toBe('accepted decision')
    } finally {
      room.document.destroy()
      await room.close()
    }
  })
})

describe('the edit-mode guard', () => {
  // The editor never lets through an edit-mode change the service would
  // refuse, so no honest edit ends the session, and what it lets through the
  // service accepts as the editor saw it.
  const OWN_DELETION = { patchId: 'abababab-0000-4000-8000-0000000000f1', suggestionId: 'abababab-0000-4000-8000-000000000001' }
  const SINGLE = { patchId: 'abababab-0000-4000-8000-0000000000f2', suggestionId: 'abababab-0000-4000-8000-000000000002' }
  const deletionMark = (authorId: string, ids: { patchId: string; suggestionId: string }): MarkJson => ({
    type: 'deletion',
    attrs: { authorId, createdAt: CREATED_AT, id: ids.suggestionId, kind: 'deletion', ...ids },
  })
  const replacementAttrs = {
    authorId: REVIEWER,
    createdAt: CREATED_AT,
    id: 'abababab-0000-4000-8000-000000000003',
    kind: 'replacement',
    patchId: 'abababab-0000-4000-8000-0000000000f3',
    suggestionId: 'abababab-0000-4000-8000-000000000003',
  }
  const ownReplacementAttrs = {
    authorId: WRITER,
    createdAt: CREATED_AT,
    id: 'abababab-0000-4000-8000-000000000004',
    kind: 'replacement',
    patchId: 'abababab-0000-4000-8000-0000000000f4',
    suggestionId: 'abababab-0000-4000-8000-000000000004',
  }
  const OWN = Y.encodeStateAsUpdate(editorJsonToYDoc({
    type: 'doc',
    content: [
      paragraph([text('Before ZONEBEFORE.')]),
      paragraph([text('Structure ZONETARGET with text.')], {
        [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: structure(WRITER, 'heading2', OWN_STRUCTURE),
      }),
      paragraph([text('Following ZONEFOLLOW.')]),
      paragraph([
        text('With '),
        text('OWNINSERT', [insertionMark(WRITER, OWN_INSERTION)]),
        text(' and '),
        text('OWNDELETE', [deletionMark(WRITER, OWN_DELETION)]),
        text(' end.'),
      ]),
      paragraph([text('Foreign '), text('THEIRS', [insertionMark(REVIEWER, FOREIGN_INSERTION)]), text(' tail.')]),
      paragraph([text('Single ZONEX'), text('X', [insertionMark(WRITER, SINGLE)])]),
      paragraph([
        text('Replace '),
        text('OLDWORD', [{ type: 'deletion', attrs: { ...replacementAttrs } }]),
        text('NEWWORD', [{ type: 'insertion', attrs: { ...replacementAttrs } }]),
        text(' here.'),
      ]),
      paragraph([text('Foreign structure ZONEFOREIGNSTRUCT.')], {
        [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: structure(REVIEWER, 'heading3', FOREIGN_STRUCTURE),
      }),
      paragraph([
        text('Mine '),
        text('MINEOLD', [{ type: 'deletion', attrs: { ...ownReplacementAttrs } }]),
        text('MINENEW', [{ type: 'insertion', attrs: { ...ownReplacementAttrs } }]),
        text(' here.'),
      ]),
    ],
  }))

  function at(document: ProseMirrorNode, needle: string, where: 'start' | 'end' | 'in', offset = 0): number {
    const { node, position } = block(document, needle)
    if (where === 'start') return position + 1
    if (where === 'end') return position + 1 + node.content.size
    return position + 1 + node.textContent.indexOf(needle) + offset
  }

  type Command = (state: EditorState, dispatch: (transaction: Transaction) => void) => boolean
  const command = (run: Command) => (state: EditorState): Transaction => {
    let result: Transaction | null = null
    run(state, (transaction) => {
      result = transaction
    })
    if (!result) throw new Error('the command did not apply')
    return result
  }
  const typeText = (value: string) => (state: EditorState) => state.tr.insertText(value)
  // Typed text without any mark, as the editor inserts it inside another
  // person's suggestion in edit mode.
  const typePlain = (value: string) => (state: EditorState) => (
    state.tr.insert(state.selection.from, schema.text(value))
  )
  const backspace = (state: EditorState) => state.tr.delete(state.selection.from - 1, state.selection.from)
  const withMark = (name: string, length: number) => (state: EditorState) => {
    const mark = schema.marks[name]
    if (!mark) throw new Error(`schema has no ${name} mark`)
    return state.tr.addMark(state.selection.from, state.selection.from + length, mark.create())
  }
  const deleteNext = (length: number) => (state: EditorState) => (
    state.tr.delete(state.selection.from, state.selection.from + length)
  )
  const deleteBlockAround = (state: EditorState) => {
    const $from = state.selection.$from
    return state.tr.delete($from.before(1), $from.after(1))
  }

  const moveForeignStructureBlockFirst = (state: EditorState): Transaction => {
    let from = -1
    state.doc.forEach((node, offset) => {
      if (node.textContent.includes('ZONEFOREIGNSTRUCT')) from = offset
    })
    const node = state.doc.nodeAt(from)
    if (!node) throw new Error('fixture has no foreign structure block')
    return state.tr.delete(from, from + node.nodeSize).insert(0, node)
  }
  const moveForeignInsertion = (state: EditorState): Transaction => {
    const from = state.selection.from
    const slice = state.doc.slice(from, from + 'THEIRS'.length)
    const transaction = state.tr.delete(from, from + 'THEIRS'.length)
    return transaction.replace(at(transaction.doc, 'ZONEBEFORE', 'end'), undefined, slice)
  }

  // Moves and block commands as the editor sends them in edit mode:
  // content the change inserts never carries another person's suggestion,
  // neither a mark on its text nor the structure suggestion of a block.
  const withoutForeignSuggestions = (node: ProseMirrorNode): ProseMirrorNode => {
    const attrs = node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR]?.authorId === REVIEWER
      ? { ...node.attrs, [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: null }
      : node.attrs
    if (node.isText) {
      return node.mark(node.marks.filter((mark) => mark.attrs.authorId !== REVIEWER))
    }
    const content: ProseMirrorNode[] = []
    node.forEach((child) => content.push(withoutForeignSuggestions(child)))
    return node.type.create(attrs, content, node.marks)
  }
  const topBlock = (state: EditorState, needle: string): { from: number; node: ProseMirrorNode; to: number } => {
    let found: { from: number; node: ProseMirrorNode; to: number } | null = null
    state.doc.forEach((node, offset) => {
      if (!found && node.textContent.includes(needle)) found = { from: offset, node, to: offset + node.nodeSize }
    })
    if (!found) throw new Error(`fixture has no block containing ${needle}`)
    return found
  }
  const moveBlockAsSent = (needle: string, direction: 'first' | 'up') => (state: EditorState): Transaction => {
    const { from, node, to } = topBlock(state, needle)
    const target = direction === 'first' ? 0 : from - state.doc.resolve(from).nodeBefore!.nodeSize
    return state.tr.delete(from, to).insert(target, withoutForeignSuggestions(node))
  }
  const copyBlockAsSent = (needle: string) => (state: EditorState): Transaction => {
    const { node, to } = topBlock(state, needle)
    return state.tr.insert(to, withoutForeignSuggestions(node))
  }
  const moveForeignInsertionAsSent = (state: EditorState): Transaction => {
    const from = state.selection.from
    const moved = withoutForeignSuggestions(state.doc.cut(from, from + 'THEIRS'.length))
    const transaction = state.tr.delete(from, from + 'THEIRS'.length)
    return transaction.replace(at(transaction.doc, 'ZONEBEFORE', 'end'), undefined, new Slice(moved.content, 0, 0))
  }
  // The editor's block command keeps the block's attributes on the new
  // block, and the edit then takes another person's structure suggestion off
  // it.
  const headingAsSent = (state: EditorState): Transaction => {
    const { $from } = state.selection
    const attrs: Record<string, unknown> = { ...$from.parent.attrs, level: 1 }
    if ($from.parent.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR]?.authorId === REVIEWER) {
      attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR] = null
    }
    return command(setBlockType(schema.nodes.heading!, attrs))(state)
  }

  const replacement = replacementAttrs.suggestionId
  const ownReplacement = ownReplacementAttrs.suggestionId
  // A change that leaves the replacement in a form no pending suggestion may
  // take, as the editor sends it in edit mode: it removes the replacement as
  // a whole, its proposed text deleted and its struck text kept unmarked.
  const removingReplacementAsSent = (change: (state: EditorState) => Transaction) => (state: EditorState): Transaction => {
    const transaction = change(state)
    const proposed: Array<{ from: number; to: number }> = []
    transaction.doc.descendants((node, position) => {
      if (!node.isText) return true
      for (const mark of node.marks) {
        if (mark.attrs.suggestionId !== replacement) continue
        if (mark.type.name === 'insertion') proposed.push({ from: position, to: position + node.nodeSize })
        else transaction.removeMark(position, position + node.nodeSize, mark)
      }
      return false
    })
    for (const { from, to } of proposed.reverse()) transaction.delete(from, to)
    return transaction
  }
  // A change that leaves a structure suggestion impossible to apply, as the
  // editor sends it in edit mode: the suggestion is taken off its block.
  const removingInapplicableAsSent = (change: (state: EditorState) => Transaction) => (state: EditorState): Transaction => {
    const transaction = change(state)
    const inapplicable = new Set(structureSuggestionsLeftInapplicable(transaction.before, transaction.doc) ?? [])
    const blocks: number[] = []
    transaction.doc.descendants((node, position) => {
      if (inapplicable.has(node.attrs[INQTRIX_STRUCTURE_SUGGESTION_ATTR]?.suggestionId)) blocks.push(position)
      return !node.isTextblock
    })
    for (const position of blocks) transaction.setNodeAttribute(position, INQTRIX_STRUCTURE_SUGGESTION_ATTR, null)
    return transaction
  }
  const bulletList = command(wrapInList(schema.nodes.bulletList!))
  const noneRemoved: string[] = []
  // Each action with the guard's verdict, the service's verdict for the same
  // change, and what the service's row names as removed. Where the guard
  // allows a change, the service accepts it and removes exactly what the
  // guard reports. Where the guard refuses, the service refuses as well, or
  // it accepts the change with the suggestion the change left behind
  // resolved: typed text that took another person's mark loses it, and a
  // suggestion that cannot stay pending in its new form is removed.
  const actions: Array<[
    string,
    (document: ProseMirrorNode) => number,
    (state: EditorState) => Transaction,
    string,
    string,
    readonly string[],
  ]> = [
    ['typing in a paragraph without suggestions', (d) => at(d, 'ZONEBEFORE', 'end'), typeText('x'), 'allowed', 'accepted direct', noneRemoved],
    ['Enter at the end of a paragraph without suggestions', (d) => at(d, 'ZONEBEFORE', 'end'), command(splitBlock), 'allowed', 'accepted direct', noneRemoved],
    ['Enter at the end of a block with an own structure suggestion', (d) => at(d, 'ZONETARGET', 'end'), command(splitBlock), 'allowed', 'accepted direct', noneRemoved],
    // The split copies the block's attributes, and with them the structure
    // suggestion, onto both blocks: a form no suggestion may take, which the
    // service removes, the writer's own as well.
    ['Enter in the middle of that block', (d) => at(d, 'ZONETARGET', 'in', 4), command(splitBlock), 'own_suggestion_edit_unsupported', 'accepted direct', [OWN_STRUCTURE.suggestionId]],
    ['Enter at the start of that block', (d) => at(d, 'ZONETARGET', 'start'), command(splitBlock), 'own_suggestion_edit_unsupported', 'accepted direct', [OWN_STRUCTURE.suggestionId]],
    ['Backspace at the start of that block', (d) => at(d, 'ZONETARGET', 'start'), command(joinBackward), 'allowed', 'accepted direct', [OWN_STRUCTURE.suggestionId]],
    ['Backspace at the start of the block after it', (d) => at(d, 'ZONEFOLLOW', 'start'), command(joinBackward), 'allowed', 'accepted direct', noneRemoved],
    ['typing inside that block', (d) => at(d, 'ZONETARGET', 'in', 2), typeText('x'), 'allowed', 'accepted direct', noneRemoved],
    ['bold inside that block', (d) => at(d, 'ZONETARGET', 'in'), withMark('bold', 4), 'allowed', 'accepted direct', noneRemoved],
    ['typing inside an own insertion', (d) => at(d, 'OWNINSERT', 'in', 3), typeText('x'), 'allowed', 'accepted suggestion', noneRemoved],
    ['Backspace inside an own insertion', (d) => at(d, 'OWNINSERT', 'in', 3), backspace, 'allowed', 'accepted direct', noneRemoved],
    ['typing a different character directly before an own insertion', (d) => at(d, 'OWNINSERT', 'in'), typeText('x'), 'allowed', 'accepted direct', noneRemoved],
    ['typing inside an own deletion', (d) => at(d, 'OWNDELETE', 'in', 3), typeText('x'), 'own_suggestion_edit_unsupported', POLICY_REFUSAL, noneRemoved],
    ['code formatting over an own deletion', (d) => at(d, 'OWNDELETE', 'in'), withMark('code', 'OWNDELETE'.length), 'allowed', 'accepted direct', [OWN_DELETION.suggestionId]],
    ['Backspace on the last character of an own insertion', (d) => at(d, 'ZONEX', 'end'), backspace, 'allowed', 'accepted direct', [SINGLE.suggestionId]],
    ['typing inside a foreign insertion with the text taking its mark', (d) => at(d, 'THEIRS', 'in', 3), typeText('x'), 'foreign_suggestion_changed', 'accepted direct', noneRemoved],
    ['plain text typed inside a foreign insertion', (d) => at(d, 'THEIRS', 'in', 3), typePlain('x'), 'allowed', 'accepted direct', noneRemoved],
    ['typing directly after a foreign insertion', (d) => at(d, 'THEIRS', 'in', 6), typeText('x'), 'allowed', 'accepted direct', noneRemoved],
    ['typing between the halves of a foreign replacement', (d) => at(d, 'NEWWORD', 'in'), typeText('x'), 'foreign_suggestion_changed', 'accepted direct', [replacement]],
    ['moving a block that carries a foreign structure suggestion', (d) => at(d, 'ZONEBEFORE', 'start'), moveForeignStructureBlockFirst, 'foreign_suggestion_changed', 'accepted direct', [FOREIGN_STRUCTURE.suggestionId]],
    ['deleting a foreign insertion completely', (d) => at(d, 'THEIRS', 'in'), deleteNext('THEIRS'.length), 'allowed', 'accepted direct', [FOREIGN_INSERTION.suggestionId]],
    ['deleting the paragraph that holds a foreign insertion', (d) => at(d, 'THEIRS', 'in'), deleteBlockAround, 'allowed', 'accepted direct', [FOREIGN_INSERTION.suggestionId]],
    ['replacing a foreign insertion and the text around it', (d) => at(d, 'Foreign THEIRS', 'in'), (state) => (
      state.tr.insertText('New words', state.selection.from, state.selection.from + 'Foreign THEIRS tail'.length)
    ), 'allowed', 'accepted direct', [FOREIGN_INSERTION.suggestionId]],
    ['deleting both halves of a foreign replacement', (d) => at(d, 'OLDWORD', 'in'), deleteNext('OLDWORDNEWWORD'.length), 'allowed', 'accepted direct', [replacement]],
    ['deleting the block that carries a foreign structure suggestion', (d) => at(d, 'ZONEFOREIGNSTRUCT', 'in'), deleteBlockAround, 'allowed', 'accepted direct', [FOREIGN_STRUCTURE.suggestionId]],
    ['deleting part of a foreign insertion', (d) => at(d, 'THEIRS', 'in', 2), deleteNext(2), 'allowed', 'accepted direct', noneRemoved],
    ['bold over a foreign insertion', (d) => at(d, 'THEIRS', 'in'), withMark('bold', 'THEIRS'.length), 'allowed', 'accepted direct', noneRemoved],
    ['bold over both halves of a foreign replacement', (d) => at(d, 'OLDWORD', 'in'), withMark('bold', 'OLDWORDNEWWORD'.length), 'allowed', 'accepted direct', noneRemoved],
    ['code over a foreign insertion', (d) => at(d, 'THEIRS', 'in'), withMark('code', 'THEIRS'.length), 'foreign_suggestion_changed', 'accepted direct', [FOREIGN_INSERTION.suggestionId]],
    ['code over an own insertion', (d) => at(d, 'OWNINSERT', 'in'), withMark('code', 'OWNINSERT'.length), 'suggestion_would_disappear', POLICY_REFUSAL, noneRemoved],
    ['deleting only the proposed half of a foreign replacement', (d) => at(d, 'NEWWORD', 'in'), deleteNext('NEWWORD'.length), 'foreign_suggestion_changed', 'accepted direct', [replacement]],
    // The writer's own replacement left in such a form, sent as it is: the
    // service removes it the same way.
    ['typing between the halves of an own replacement', (d) => at(d, 'MINENEW', 'in'), typeText('x'), 'own_suggestion_edit_unsupported', 'accepted direct', [ownReplacement]],
    ['deleting only the proposed half of an own replacement', (d) => at(d, 'MINENEW', 'in'), deleteNext('MINENEW'.length), 'own_suggestion_edit_unsupported', 'accepted direct', [ownReplacement]],
    ['moving a foreign insertion to another paragraph', (d) => at(d, 'THEIRS', 'in'), moveForeignInsertion, 'foreign_suggestion_changed', 'accepted direct', [FOREIGN_INSERTION.suggestionId]],
    // Moves and block commands, as the editor sends them: the guard never
    // refuses what the service accepts.
    ['moving a foreign insertion to another paragraph, as the editor sends it', (d) => at(d, 'THEIRS', 'in'), moveForeignInsertionAsSent, 'allowed', 'accepted direct', [FOREIGN_INSERTION.suggestionId]],
    ['moving the paragraph that holds a foreign insertion to the top, as the editor sends it', (d) => at(d, 'ZONEBEFORE', 'start'), moveBlockAsSent('THEIRS', 'first'), 'allowed', 'accepted direct', [FOREIGN_INSERTION.suggestionId]],
    ['moving a block that carries a foreign structure suggestion to the top, as the editor sends it', (d) => at(d, 'ZONEBEFORE', 'start'), moveBlockAsSent('ZONEFOREIGNSTRUCT', 'first'), 'allowed', 'accepted direct', [FOREIGN_STRUCTURE.suggestionId]],
    ['moving that block up one place, as the editor sends it', (d) => at(d, 'ZONEBEFORE', 'start'), moveBlockAsSent('ZONEFOREIGNSTRUCT', 'up'), 'allowed', 'accepted direct', [FOREIGN_STRUCTURE.suggestionId]],
    ['copying that block, as the editor sends it', (d) => at(d, 'ZONEBEFORE', 'start'), copyBlockAsSent('ZONEFOREIGNSTRUCT'), 'allowed', 'accepted direct', noneRemoved],
    ['a heading of that block, as the editor sends it', (d) => at(d, 'ZONEFOREIGNSTRUCT', 'in'), headingAsSent, 'allowed', 'accepted direct', [FOREIGN_STRUCTURE.suggestionId]],
    ['a heading of the paragraph that holds a foreign insertion', (d) => at(d, 'THEIRS', 'in'), headingAsSent, 'allowed', 'accepted direct', noneRemoved],
    ['a code block of the paragraph that holds a foreign insertion', (d) => at(d, 'THEIRS', 'in'), command(setBlockType(schema.nodes.codeBlock!)), 'allowed', 'accepted direct', noneRemoved],
    ['a list around the paragraph that holds a foreign insertion', (d) => at(d, 'THEIRS', 'in'), command(wrapInList(schema.nodes.bulletList!)), 'allowed', 'accepted direct', noneRemoved],
    ['a quote around the paragraph that holds a foreign insertion', (d) => at(d, 'THEIRS', 'in'), command(wrapIn(schema.nodes.blockquote!)), 'allowed', 'accepted direct', noneRemoved],
    // A heading cannot stand in a list item: a list around a block with a
    // heading proposal leaves the proposal impossible to apply. As the editor
    // sends it, the change removes the proposal, another person's and the
    // writer's own alike.
    ['a list around the block that carries a foreign heading proposal', (d) => at(d, 'ZONEFOREIGNSTRUCT', 'in'), bulletList, 'structure_suggestion_inapplicable', 'accepted direct', [FOREIGN_STRUCTURE.suggestionId]],
    ['a list around the block that carries a foreign heading proposal, as the editor sends it', (d) => at(d, 'ZONEFOREIGNSTRUCT', 'in'), removingInapplicableAsSent(bulletList), 'allowed', 'accepted direct', [FOREIGN_STRUCTURE.suggestionId]],
    ['a list around the block that carries an own heading proposal', (d) => at(d, 'ZONETARGET', 'in'), bulletList, 'structure_suggestion_inapplicable', POLICY_REFUSAL, noneRemoved],
    ['a list around the block that carries an own heading proposal, as the editor sends it', (d) => at(d, 'ZONETARGET', 'in'), removingInapplicableAsSent(bulletList), 'allowed', 'accepted direct', [OWN_STRUCTURE.suggestionId]],
    // A replacement the change leaves in a form no pending suggestion may
    // take, as the editor sends it: removed as a whole in the same change.
    ['plain text typed between the halves of a foreign replacement, as the editor sends it', (d) => at(d, 'NEWWORD', 'in'), removingReplacementAsSent(typePlain('x')), 'allowed', 'accepted direct', [replacement]],
    ['plain text typed inside the proposed half of a foreign replacement, as the editor sends it', (d) => at(d, 'NEWWORD', 'in', 3), removingReplacementAsSent(typePlain('x')), 'allowed', 'accepted direct', [replacement]],
    ['deleting only the proposed half of a foreign replacement, as the editor sends it', (d) => at(d, 'NEWWORD', 'in'), removingReplacementAsSent(deleteNext('NEWWORD'.length)), 'allowed', 'accepted direct', [replacement]],
  ]

  it.each(actions)('agrees with the service on %s', async (_name, place, change, guardVerdict, serviceVerdict, removed) => {
    let guard = 'allowed'
    let reported: string[] | null = null
    const room = await openRoom(OWN)
    try {
      const server = await room.submit({
        access: 'edit',
        user: WRITER,
        update: (document) => editorUpdate(document, 4001, (state) => {
          const placed = state.apply(state.tr.setSelection(TextSelection.create(state.doc, place(state.doc))))
          const transaction = change(placed)
          try {
            reported = assertDirectEditKeepsSuggestionRecords(transaction, WRITER)
          } catch (error) {
            if (!(error instanceof UnsupportedSuggestionStructureError)) throw error
            guard = error.reason
          }
          return transaction
        }),
      })
      const accepted = server.startsWith('accepted')
      expect({
        guard,
        removed: accepted ? room.api.persisted.at(-1)?.rejectedSuggestionIds : null,
        server,
      }).toEqual({ guard: guardVerdict, removed: accepted ? removed : null, server: serviceVerdict })
      if (guard === 'allowed') expect(reported).toEqual(removed)
    } finally {
      room.document.destroy()
      await room.close()
    }
  })
})

describe('an edit lease and pending suggestions', () => {
  // A writer with edit rights may delete, format or type into text that
  // holds pending suggestions. A suggestion keeps the characters that are
  // left and stays pending in its author's name; the row is the writer's
  // direct edit and changes no patch. A suggestion whose every proposed
  // character the update deleted is removed by that writer: the update names
  // it, and its patch loses it. Text a removed suggestion proposed is gone,
  // never kept without its mark. A suggestion never gains a character or
  // moves; an update that writes in another person's name is refused.
  const THEIRS = { patchId: 'f0f0f0f0-0000-4000-8000-0000000000f1', suggestionId: 'f0f0f0f0-0000-4000-8000-000000000001' }
  const PULLED = { patchId: 'f0f0f0f0-0000-4000-8000-0000000000f2', suggestionId: 'f0f0f0f0-0000-4000-8000-000000000002' }
  const STRUCK = { patchId: 'f0f0f0f0-0000-4000-8000-0000000000f3', suggestionId: 'f0f0f0f0-0000-4000-8000-000000000003' }
  const MINE = { patchId: 'f0f0f0f0-0000-4000-8000-0000000000f4', suggestionId: 'f0f0f0f0-0000-4000-8000-000000000004' }
  const deletionMark = (authorId: string, ids: { patchId: string; suggestionId: string }): MarkJson => ({
    type: 'deletion',
    attrs: { authorId, createdAt: CREATED_AT, id: ids.suggestionId, kind: 'deletion', ...ids },
  })
  const REJECTABLE = Y.encodeStateAsUpdate(editorJsonToYDoc({
    type: 'doc',
    content: [
      paragraph([text('Keep ZONEKEEP.')]),
      paragraph([
        text('Foreign '),
        text('THEIRS', [insertionMark(REVIEWER, THEIRS)]),
        text(' tail ZONEFOREIGN.'),
      ]),
      paragraph([
        text('Pull '),
        text('WORD', [insertionMark(REVIEWER, PULLED)]),
        text('Dtail ZONEPULL.'),
      ]),
      paragraph([
        text('Deleted '),
        text('OLDTEXT', [deletionMark(REVIEWER, STRUCK)]),
        text(' rest ZONEDELETION.'),
      ]),
      paragraph([
        text('Own '),
        text('MINE', [insertionMark(WRITER, MINE)]),
        text(' ZONEOWN.'),
      ]),
      paragraph([text('Structured ZONESTRUCT.')], {
        [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: structure(REVIEWER, 'heading2', FOREIGN_STRUCTURE),
      }),
      paragraph([text('Last ZONELAST.')]),
    ],
  }))

  /** The text of the top-level paragraph holding `needle`, and where the
   * needle starts in it. */
  function textWith(fragment: Y.XmlFragment, needle: string): { index: number; text: Y.XmlText } {
    const element = fragment.get(topLevelIndex(fragment, needle))
    const content = element instanceof Y.XmlElement ? element.get(0) : null
    if (!(content instanceof Y.XmlText)) throw new Error(`fixture has no text with ${needle}`)
    const index = content.toString().replace(/<[^>]*>/g, '').indexOf(needle)
    if (index < 0) throw new Error(`fixture text has no ${needle}`)
    return { index, text: content }
  }

  /** Where a needle starts and ends in the editor document. */
  function range(document: ProseMirrorNode, needle: string): { from: number; to: number } {
    const { node, position } = block(document, needle)
    const from = position + 1 + node.textContent.indexOf(needle)
    return { from, to: from + needle.length }
  }

  const deleteBlock = (needle: string) => (state: EditorState): Transaction => {
    const { node, position } = block(state.doc, needle)
    return state.tr.delete(position, position + node.nodeSize)
  }

  const deleteText = (needle: string, start = 0, length = needle.length) => (state: EditorState): Transaction => {
    const { from } = range(state.doc, needle)
    return state.tr.delete(from + start, from + start + length)
  }

  const format = (name: string, needle: string, attrs?: Record<string, unknown>) => (state: EditorState): Transaction => {
    const mark = schema.marks[name]
    if (!mark) throw new Error(`schema has no ${name} mark`)
    const { from, to } = range(state.doc, needle)
    return state.tr.addMark(from, to, mark.create(attrs))
  }

  /** Plain text typed at an offset into a needle, without any mark: what the
   * editor inserts inside another person's suggestion in edit mode. */
  const typePlain = (needle: string, offset: number, value: string) => (state: EditorState): Transaction => (
    state.tr.insert(range(state.doc, needle).from + offset, schema.text(value))
  )

  async function submitted(steps: Step[]): Promise<{
    document: JSONContent
    persisted: FakeCollaborationApi['persisted']
    verdicts: string[]
  }> {
    const room = await openRoom(REJECTABLE)
    try {
      const verdicts: string[] = []
      for (const step of steps) verdicts.push(await room.submit(step))
      return { document: editorYDocToJson(room.document), persisted: [...room.api.persisted], verdicts }
    } finally {
      room.document.destroy()
      await room.close()
    }
  }

  /** The runs of the room's paragraph holding `needle`: each run's text,
   * followed by its mark names when it has marks. */
  function runsOf(document: JSONContent, needle: string): string[] {
    const found = (document.content ?? []).find((node) => (
      (node.content ?? []).map((child) => child.text ?? '').join('').includes(needle)
    ))
    if (!found) throw new Error(`the room has no paragraph with ${needle}`)
    return (found.content ?? []).map((child) => {
      const marks = (child.marks ?? []).map((mark) => mark.type).sort()
      return marks.length > 0 ? `${child.text}{${marks.join(',')}}` : child.text ?? ''
    })
  }

  /** The descriptor of a suggestion the room holds, or null. */
  function descriptorIn(document: JSONContent, suggestionId: string): unknown {
    const record = collectSuggestionRecords(document).get(suggestionId)
    return record
      ? { authorId: record.authorId, createdAt: record.createdAt, kind: record.kind, patchId: record.patchId }
      : null
  }

  /** The text of the room's paragraph holding `needle`, and whether any of
   * its characters still carries a suggestion mark. */
  function paragraphOf(document: JSONContent, needle: string): { marked: boolean; text: string } {
    const found = (document.content ?? []).find((node) => (
      (node.content ?? []).map((child) => child.text ?? '').join('').includes(needle)
    ))
    if (!found) throw new Error(`the room has no paragraph with ${needle}`)
    return {
      marked: (found.content ?? []).some((child) => (child.marks ?? []).some((mark) => (
        ['deletion', 'insertion', 'modification'].includes(mark.type)
      ))),
      text: (found.content ?? []).map((child) => child.text ?? '').join(''),
    }
  }

  const edit = (update: (room: Y.Doc) => Uint8Array): Step => ({ access: 'edit', update, user: WRITER })

  it.each([
    ['deleting the paragraph that holds another person\'s insertion', THEIRS, deleteBlock('ZONEFOREIGN')],
    ['deleting exactly another person\'s insertion', THEIRS, deleteText('THEIRS')],
    ['deleting another person\'s insertion with the text around it', THEIRS, (state: EditorState) => {
      const { from, to } = range(state.doc, 'Foreign THEIRS tail')
      return state.tr.insertText('New words', from, to)
    }],
    ['deleting the text another person proposed to delete', STRUCK, deleteText('OLDTEXT')],
    ['deleting the block that carries another person\'s structure suggestion', FOREIGN_STRUCTURE, deleteBlock('ZONESTRUCT')],
    ['deleting an own pending insertion', MINE, deleteText('MINE')],
  ] as const)('is accepted as a direct edit when %s, and names the suggestion as removed', async (_name, ids, build) => {
    const { persisted, verdicts } = await submitted([edit((room) => editorUpdate(room, 4001, build))])

    expect(verdicts).toEqual(['accepted direct'])
    const row = persisted.at(-1)!
    expect(row.rejectedSuggestionIds).toEqual([ids.suggestionId])
    expect(row.suggestions).toEqual([])
    expect(row.suggestionIds).toEqual([])
    expect(row.patches).toEqual([expect.objectContaining({
      activeSuggestionIds: [],
      patchId: ids.patchId,
    })])
  })

  it('names nothing as removed for an edit that leaves every suggestion alone', async () => {
    const { persisted, verdicts } = await submitted([edit((room) => editorUpdate(room, 4001, (state) => (
      state.tr.insertText(' more', range(state.doc, 'ZONEKEEP').to)
    )))])

    expect(verdicts).toEqual(['accepted direct'])
    expect(persisted.at(-1)).toMatchObject({ patches: [], rejectedSuggestionIds: [] })
  })

  it('accepts the removal of another person\'s deletion mark that keeps the text, as a removal', async () => {
    const { persisted, verdicts } = await submitted([edit((room) => rawUpdate(room, 4001, (fragment) => {
      const { index, text: content } = textWith(fragment, 'OLDTEXT')
      content.format(index, 'OLDTEXT'.length, { deletion: null })
    }))])

    expect(verdicts).toEqual(['accepted direct'])
    expect(persisted.at(-1)?.rejectedSuggestionIds).toEqual([STRUCK.suggestionId])
  })

  it('accepts the removal of another person\'s structure suggestion from its block, as a removal', async () => {
    const { persisted, verdicts } = await submitted([edit((room) => rawUpdate(room, 4001, (fragment) => {
      const element = fragment.get(topLevelIndex(fragment, 'ZONESTRUCT'))
      if (!(element instanceof Y.XmlElement)) throw new Error('fixture has no structure block')
      element.removeAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR)
    }))])

    expect(verdicts).toEqual(['accepted direct'])
    expect(persisted.at(-1)?.rejectedSuggestionIds).toEqual([FOREIGN_STRUCTURE.suggestionId])
  })

  it('removes an insertion another person proposed while the edit that deletes its paragraph was on its way', async () => {
    const base = documentFrom(REJECTABLE, 1)
    try {
      const { persisted, verdicts } = await submitted([
        {
          access: 'suggest',
          user: REVIEWER,
          update: () => suggestionUpdate(base, 4002, REVIEWER, (state) => (
            state.tr.insertText(' proposed', range(state.doc, 'ZONEKEEP').to)
          )),
        },
        edit(() => editorUpdate(base, 4001, deleteBlock('ZONEKEEP'))),
      ])

      expect(verdicts).toEqual(['accepted suggestion', 'accepted direct'])
      const proposed = persisted[0]!.suggestionIds
      expect(proposed).toHaveLength(1)
      expect(persisted[1]!.rejectedSuggestionIds).toEqual(proposed)
      expect(persisted[1]!.patches).toEqual([expect.objectContaining({
        activeSuggestionIds: [],
        authorId: REVIEWER,
        patchId: persisted[0]!.patches[0]!.patchId,
      })])
    } finally {
      base.destroy()
    }
  })

  // Editing a suggestion's text directly leaves the suggestion pending with
  // what is left of it, on its own characters, in its author's name. The
  // row is the writer's direct edit: it names no patch and removes nothing.
  it.each([
    ['deleting part of another person\'s insertion', THEIRS, deleteText('THEIRS', 2, 2), 'ZONEFOREIGN', ['Foreign ', 'THRS{insertion}', ' tail ZONEFOREIGN.']],
    ['deleting part of the writer\'s own insertion with the original text before it', MINE, deleteText('Own MINE', 0, 'Own MI'.length), 'ZONEOWN', ['NE{insertion}', ' ZONEOWN.']],
    ['formatting another person\'s insertion bold', THEIRS, format('bold', 'THEIRS'), 'ZONEFOREIGN', ['Foreign ', 'THEIRS{bold,insertion}', ' tail ZONEFOREIGN.']],
    ['linking another person\'s insertion with the text around it', THEIRS, format('link', 'Foreign THEIRS tail', { href: 'https://example.com' }), 'ZONEFOREIGN', ['Foreign {link}', 'THEIRS{insertion,link}', ' tail{link}', ' ZONEFOREIGN.']],
    ['typing plain text inside another person\'s insertion', THEIRS, typePlain('THEIRS', 3, 'x'), 'ZONEFOREIGN', ['Foreign ', 'THE{insertion}', 'x', 'IRS{insertion}', ' tail ZONEFOREIGN.']],
    ['deleting part of the text another person proposed to delete', STRUCK, deleteText('OLDTEXT', 1, 3), 'ZONEDELETION', ['Deleted ', 'OEXT{deletion}', ' rest ZONEDELETION.']],
    ['formatting the text another person proposed to delete bold', STRUCK, format('bold', 'OLDTEXT'), 'ZONEDELETION', ['Deleted ', 'OLDTEXT{bold,deletion}', ' rest ZONEDELETION.']],
    ['typing plain text inside the text another person proposed to delete', STRUCK, typePlain('OLDTEXT', 3, 'x'), 'ZONEDELETION', ['Deleted ', 'OLD{deletion}', 'x', 'TEXT{deletion}', ' rest ZONEDELETION.']],
  ] as const)('keeps a suggestion pending with what is left of it when %s', async (_name, ids, build, needle, runs) => {
    const initial = documentFrom(REJECTABLE, 1)
    const before = descriptorIn(editorYDocToJson(initial), ids.suggestionId)
    initial.destroy()
    const room = await openRoom(REJECTABLE)
    try {
      let sent: Uint8Array | null = null
      const verdict = await room.submit(edit((current) => {
        sent = editorUpdate(current, 4001, build)
        return sent
      }))

      expect(verdict).toBe('accepted direct')
      const row = room.api.persisted.at(-1)!
      expect(row).toMatchObject({ patches: [], rejectedSuggestionIds: [], suggestionIds: [], suggestions: [] })
      // The service accepted the writer's own bytes: nothing was resolved.
      expect(Buffer.compare(row.update, sent!)).toBe(0)
      const document = editorYDocToJson(room.document)
      expect(runsOf(document, needle)).toEqual(runs)
      expect(before).not.toBeNull()
      expect(descriptorIn(document, ids.suggestionId)).toEqual(before)
    } finally {
      room.document.destroy()
      await room.close()
    }
  })

  it('keeps text typed inside another person\'s insertion plain when it took the insertion\'s mark', async () => {
    // A binding that lets typed text take the mark of the text around it
    // would write it into another person's proposal; the service clears the
    // mark on it in the writer's row, and the insertion stays pending.
    const { document, persisted, verdicts } = await submitted([edit((room) => editorUpdate(room, 4001, (state) => (
      state.tr.insertText('x', range(state.doc, 'THEIRS').from + 3)
    )))])

    expect(verdicts).toEqual(['accepted direct'])
    expect(persisted.at(-1)).toMatchObject({ patches: [], rejectedSuggestionIds: [], suggestionIds: [] })
    expect(runsOf(document, 'ZONEFOREIGN')).toEqual(['Foreign ', 'THE{insertion}', 'x', 'IRS{insertion}', ' tail ZONEFOREIGN.'])
  })

  it('lets the writer\'s own insertion grow as the writer\'s proposal', async () => {
    const { document, persisted, verdicts } = await submitted([edit((room) => editorUpdate(room, 4001, (state) => (
      state.tr.insertText('x', range(state.doc, 'MINE').from + 2)
    )))])

    expect(verdicts).toEqual(['accepted suggestion'])
    expect(persisted.at(-1)).toMatchObject({ rejectedSuggestionIds: [], suggestionIds: [MINE.suggestionId] })
    expect(runsOf(document, 'ZONEOWN')).toEqual(['Own ', 'MIxNE{insertion}', ' ZONEOWN.'])
  })

  it.each([
    ['accept', 'Foreign THExIRS tail ZONEFOREIGN.'],
    ['reject', 'Foreign x tail ZONEFOREIGN.'],
  ] as const)('lets a decision to %s cover both parts of an insertion that plain typing split', async (decision, text) => {
    const room = await openRoom(REJECTABLE)
    try {
      expect(await room.submit(edit((current) => editorUpdate(current, 4001, typePlain('THEIRS', 3, 'x'))))).toBe('accepted direct')
      expect(await room.decide(decision, [THEIRS.patchId])).toBe('accepted decision')

      expect(paragraphOf(editorYDocToJson(room.document), 'ZONEFOREIGN')).toEqual({ marked: false, text })
    } finally {
      room.document.destroy()
      await room.close()
    }
  })

  // Known limits, kept red on purpose: each case must turn its test into a
  // failure once the service admits it, so the limit is taken off here.
  //
  // An edit that joins two paragraphs makes the binding write the second
  // paragraph's text anew. The service follows a suggestion written anew
  // only when the stretch between the nearest characters the edit kept
  // holds the same suggested text before and after. When the join also
  // shortened the suggestion, or deleted other suggested text in that
  // stretch, the service reads the change as a move and removes the
  // suggestion with its proposed text.
  it.fails('keeps another person\'s insertion pending with what is left of it when an edit deletes from the end of the paragraph before it into the insertion', async () => {
    const { document, persisted, verdicts } = await submitted([edit((room) => editorUpdate(room, 4001, (state) => (
      state.tr.delete(range(state.doc, 'ZONEKEEP.').to, range(state.doc, 'THEIRS').from + 3)
    )))])

    expect({
      rejected: persisted.at(-1)?.rejectedSuggestionIds,
      runs: runsOf(document, 'ZONEKEEP'),
      verdicts,
    }).toEqual({
      rejected: [],
      runs: ['Keep ZONEKEEP.', 'IRS{insertion}', ' tail ZONEFOREIGN.'],
      verdicts: ['accepted direct'],
    })
  })

  it.fails('keeps another person\'s insertion pending, untouched, in a paragraph an edit joins to the one before it while deleting part of an insertion there', async () => {
    const { document, persisted, verdicts } = await submitted([edit((room) => editorUpdate(room, 4001, (state) => (
      state.tr.delete(range(state.doc, 'THEIRS').from + 3, range(state.doc, 'Pull').from + 2)
    )))])

    expect({
      rejected: persisted.at(-1)?.rejectedSuggestionIds,
      runs: runsOf(document, 'ZONEPULL'),
      verdicts,
    }).toEqual({
      rejected: [],
      runs: ['Foreign ', 'THE{insertion}', 'll ', 'WORD{insertion}', 'Dtail ZONEPULL.'],
      verdicts: ['accepted direct'],
    })
  })

  it('refuses a character typed before another person\'s insertion that the binding pairs with its first character, rather than delete the typed character', async () => {
    // The plain binding keeps the insertion's first character for the typed
    // one and writes the insertion's character anew. Rejecting the insertion
    // would delete the room's character, which now holds what was typed.
    const { document, persisted, verdicts } = await submitted([edit((room) => editorUpdate(room, 4001, (state) => (
      state.tr.insertText('T', range(state.doc, 'THEIRS').from)
    )))])

    expect(verdicts).toEqual([POLICY_REFUSAL])
    expect(persisted).toEqual([])
    expect(paragraphOf(document, 'ZONEFOREIGN')).toEqual({ marked: true, text: 'Foreign THEIRS tail ZONEFOREIGN.' })
  })

  it('keeps a character written into another person\'s insertion with that person\'s mark plain', async () => {
    // The character takes the insertion's formatting without writing any:
    // the service clears the mark on it, and the insertion keeps its own
    // characters.
    const { document, persisted, verdicts } = await submitted([edit((room) => rawUpdate(room, 4001, (fragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      const attributes = content.toDelta().find((part: { attributes?: Record<string, unknown> }) => (
        part.attributes?.insertion
      )).attributes as Record<string, unknown>
      content.delete(index + 2, 1)
      content.insert(index + 2, 'E', attributes)
    }))])

    expect(verdicts).toEqual(['accepted direct'])
    expect(persisted.at(-1)).toMatchObject({ rejectedSuggestionIds: [], suggestionIds: [] })
    expect(runsOf(document, 'ZONEFOREIGN')).toEqual(['Foreign ', 'TH{insertion}', 'E', 'IRS{insertion}', ' tail ZONEFOREIGN.'])
  })

  it('removes another person\'s insertion whose text a modified client pasted elsewhere with its mark', async () => {
    // Characters written elsewhere are new: a suggestion never moves with a
    // copy of its text. What the copy proposes in that person's name goes,
    // and the original, whose characters were cut, is removed.
    const { document, persisted, verdicts } = await submitted([edit((room) => rawUpdate(room, 4001, (fragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      const attributes = content.toDelta().find((part: { attributes?: Record<string, unknown> }) => (
        part.attributes?.insertion
      )).attributes as Record<string, unknown>
      content.delete(index, 'THEIRS'.length)
      const last = textWith(fragment, 'ZONELAST')
      last.text.insert(last.text.length, ' THEIRS', attributes)
    }))])

    expect(verdicts).toEqual(['accepted direct'])
    expect(persisted.at(-1)).toMatchObject({
      rejectedSuggestionIds: [THEIRS.suggestionId],
      suggestionIds: [],
    })
    expect(paragraphOf(document, 'ZONEFOREIGN')).toEqual({ marked: false, text: 'Foreign  tail ZONEFOREIGN.' })
    expect(paragraphOf(document, 'ZONELAST')).toEqual({ marked: false, text: 'Last ZONELAST.' })
  })

  it('keeps text cut from another person\'s insertion and pasted plain as the writer\'s own text, and removes the insertion', async () => {
    const { document, persisted, verdicts } = await submitted([edit((room) => editorUpdate(room, 4001, (state) => {
      const { from, to } = range(state.doc, 'THEIRS')
      const cut = state.tr.delete(from, to)
      return cut.insert(range(cut.doc, 'ZONELAST').to, schema.text(' THEIRS'))
    }))])

    expect(verdicts).toEqual(['accepted direct'])
    expect(persisted.at(-1)).toMatchObject({ rejectedSuggestionIds: [THEIRS.suggestionId], suggestionIds: [] })
    expect(paragraphOf(document, 'ZONELAST')).toEqual({ marked: false, text: 'Last ZONELAST THEIRS.' })
  })

  it.each([
    ['strips the mark of another person\'s insertion and keeps its text', THEIRS, 'ZONEFOREIGN', 'Foreign  tail ZONEFOREIGN.', (fragment: Y.XmlFragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      content.format(index, 'THEIRS'.length, { insertion: null })
    }],
    ['strips the mark of part of another person\'s insertion and keeps its text', THEIRS, 'ZONEFOREIGN', 'Foreign  tail ZONEFOREIGN.', (fragment: Y.XmlFragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      content.format(index + 1, 2, { insertion: null })
    }],
    ['hands another person\'s insertion the equal character beside it in place of its own', PULLED, 'ZONEPULL', 'Pull Dtail ZONEPULL.', (fragment: Y.XmlFragment) => {
      const { index, text: content } = textWith(fragment, 'WORD')
      const attributes = content.toDelta().find((part: { attributes?: Record<string, unknown> }) => (
        part.attributes?.insertion
      )).attributes as Record<string, unknown>
      content.delete(index + 3, 1)
      content.format(index + 3, 1, attributes)
    }],
    ['moves another person\'s insertion onto other text', THEIRS, 'ZONEFOREIGN', 'Foreign  tail ZONEFOREIGN.', (fragment: Y.XmlFragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      const attributes = content.toDelta().find((part: { attributes?: Record<string, unknown> }) => (
        part.attributes?.insertion
      )).attributes as Record<string, unknown>
      content.format(index, 'THEIRS'.length, { insertion: null })
      content.format(index + 'THEIRS tail '.length, 'ZONEFOREIGN'.length, attributes)
    }],
    ['changes the time of part of another person\'s insertion', THEIRS, 'ZONEFOREIGN', 'Foreign  tail ZONEFOREIGN.', (fragment: Y.XmlFragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      const attributes = content.toDelta().find((part: { attributes?: Record<string, unknown> }) => (
        part.attributes?.insertion
      )).attributes.insertion as Record<string, unknown>
      content.format(index, 2, { insertion: { ...attributes, createdAt: CREATED_AT + 1 } })
    }],
    ['declares another person\'s insertion a deletion', THEIRS, 'ZONEFOREIGN', 'Foreign  tail ZONEFOREIGN.', (fragment: Y.XmlFragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      const attributes = content.toDelta().find((part: { attributes?: Record<string, unknown> }) => (
        part.attributes?.insertion
      )).attributes.insertion as Record<string, unknown>
      content.format(index, 'THEIRS'.length, { insertion: { ...attributes, kind: 'deletion' } })
    }],
    ['replaces another person\'s structure suggestion on its block with a rewritten one', FOREIGN_STRUCTURE, 'ZONESTRUCT', 'Structured ZONESTRUCT.', (fragment: Y.XmlFragment) => {
      const element = fragment.get(topLevelIndex(fragment, 'ZONESTRUCT'))
      if (!(element instanceof Y.XmlElement)) throw new Error('fixture has no structure block')
      const data = element.getAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR) as unknown as Record<string, unknown>
      element.setAttribute(INQTRIX_STRUCTURE_SUGGESTION_ATTR, { ...data, action: 'codeBlock' } as never)
    }],
  ] as const)('removes the suggestion as a whole when a modified client %s', async (_name, ids, needle, text, mutate) => {
    // A mark stripped from text the suggestion proposed, a mark given to
    // text the suggestion never stood on, a rewritten proposal: none of them
    // leaves the suggestion pending in a form its author made, and none
    // decides it. The service removes it the way a rejection removes it:
    // text the room held under the proposed insertion goes, text the mark
    // spread to stays unmarked.
    const { document, persisted, verdicts } = await submitted([edit((room) => rawUpdate(room, 4001, mutate))])

    expect(verdicts).toEqual(['accepted direct'])
    expect(persisted.at(-1)).toMatchObject({
      rejectedSuggestionIds: [ids.suggestionId],
      suggestionIds: [],
    })
    expect(paragraphOf(document, needle)).toEqual({ marked: false, text })
    expect(collectSuggestionRecords(document).has(ids.suggestionId)).toBe(false)
  })

  it.each([
    ['deletes the paragraph that holds another person\'s insertion', (room: Y.Doc) => editorUpdate(room, 4001, deleteBlock('ZONEFOREIGN'))],
    ['strips another person\'s deletion mark', (room: Y.Doc) => rawUpdate(room, 4001, (fragment) => {
      const { index, text: content } = textWith(fragment, 'OLDTEXT')
      content.format(index, 'OLDTEXT'.length, { deletion: null })
      const ids = { patchId: uuid(), suggestionId: uuid() }
      const last = textWith(fragment, 'ZONELAST')
      last.text.insert(last.text.length, ' legit', {
        insertion: { authorId: THIRD, createdAt: CREATED_AT, id: ids.suggestionId, kind: 'insertion', ...ids },
      })
    })],
  ] as const)('stays refused for a suggest-only lease that %s', async (_name, update) => {
    const { verdicts } = await submitted([{ access: 'suggest', update, user: THIRD }])

    expect(verdicts).toEqual([POLICY_REFUSAL])
  })

  /** The insertion attributes of `THEIRS`, as the room holds them. */
  const theirsAttributes = (content: Y.XmlText): Record<string, unknown> => (
    content.toDelta().find((part: { attributes?: Record<string, unknown> }) => (
      part.attributes?.insertion
    )).attributes.insertion as Record<string, unknown>
  )

  // Edit rights reject suggestions; they never write one in another
  // person's name. Each case below is a modified client under an edit lease.
  it.each([
    ['writes a new suggestion in another person\'s name', (fragment: Y.XmlFragment) => {
      const ids = { patchId: uuid(), suggestionId: uuid() }
      const last = textWith(fragment, 'ZONELAST')
      last.text.insert(last.text.length, ' forged', {
        insertion: { authorId: REVIEWER, createdAt: CREATED_AT, id: ids.suggestionId, kind: 'insertion', ...ids },
      })
    }],
    ['adds a new suggestion in another person\'s name to that person\'s pending patch', (fragment: Y.XmlFragment) => {
      const suggestionId = uuid()
      const last = textWith(fragment, 'ZONELAST')
      last.text.insert(last.text.length, ' forged', {
        insertion: {
          authorId: REVIEWER,
          createdAt: CREATED_AT,
          id: suggestionId,
          kind: 'insertion',
          patchId: THEIRS.patchId,
          suggestionId,
        },
      })
    }],
    ['gives another person\'s insertion to itself', (fragment: Y.XmlFragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      content.format(index, 'THEIRS'.length, {
        insertion: { ...theirsAttributes(content), authorId: WRITER },
      })
    }],
    ['moves another person\'s insertion into a patch that person never opened', (fragment: Y.XmlFragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      content.format(index, 'THEIRS'.length, {
        insertion: { ...theirsAttributes(content), patchId: uuid() },
      })
    }],
    ['changes the time of another person\'s insertion', (fragment: Y.XmlFragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      content.format(index, 'THEIRS'.length, {
        insertion: { ...theirsAttributes(content), createdAt: CREATED_AT + 1 },
      })
    }],
  ] as const)('is refused when a modified client under an edit lease %s', async (_name, mutate) => {
    const { persisted, verdicts } = await submitted([edit((room) => rawUpdate(room, 4001, mutate))])

    expect(verdicts).toEqual([POLICY_REFUSAL])
    expect(persisted).toEqual([])
  })

  it('removes another person\'s insertion, and keeps no suggestion in its place, when a modified client under an edit lease proposes half of it as its own suggestion', async () => {
    const { document, persisted, verdicts } = await submitted([edit((room) => rawUpdate(room, 4001, (fragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      const ids = { patchId: uuid(), suggestionId: uuid() }
      content.format(index + 3, 3, {
        insertion: { authorId: WRITER, createdAt: CREATED_AT, id: ids.suggestionId, kind: 'insertion', ...ids },
      })
    }))])

    expect(verdicts).toEqual(['accepted direct'])
    expect(persisted.at(-1)).toMatchObject({
      rejectedSuggestionIds: [THEIRS.suggestionId],
      suggestionIds: [],
      suggestions: [],
    })
    expect(paragraphOf(document, 'ZONEFOREIGN')).toEqual({ marked: false, text: 'Foreign  tail ZONEFOREIGN.' })
  })

  /** The author's extension of `THEIRS`, written into the insertion's own
   * formatting, as the binding writes typing at the end of a marked run. */
  const extendTheirs = (base: Y.Doc): Step => ({
    access: 'suggest',
    user: REVIEWER,
    update: () => rawUpdate(base, 4002, (fragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      content.insert(index + 'THEIRS'.length, 'X')
    }),
  })

  it('keeps another person\'s insertion pending with the extension its author wrote while an edit deleted part of it', async () => {
    const base = documentFrom(REJECTABLE, 1)
    try {
      const { document, persisted, verdicts } = await submitted([
        extendTheirs(base),
        edit(() => editorUpdate(base, 4001, deleteText('THEIRS', 0, 3))),
      ])

      expect(verdicts).toEqual(['accepted suggestion', 'accepted direct'])
      expect(persisted.at(-1)).toMatchObject({ patches: [], rejectedSuggestionIds: [], suggestionIds: [] })
      expect(runsOf(document, 'ZONEFOREIGN')).toEqual(['Foreign ', 'IRSX{insertion}', ' tail ZONEFOREIGN.'])
    } finally {
      base.destroy()
    }
  })

  it('removes another person\'s insertion with the extension its author wrote while an edit deleted every character it showed', async () => {
    // Deleting the whole run also deletes the formatting that marks it, so
    // the merge leaves the extension without its mark: kept, it would read
    // as accepted text. The insertion is removed with the extension.
    const base = documentFrom(REJECTABLE, 1)
    try {
      const { document, persisted, verdicts } = await submitted([
        extendTheirs(base),
        edit(() => editorUpdate(base, 4001, deleteText('THEIRS'))),
      ])

      expect(verdicts).toEqual(['accepted suggestion', 'accepted direct'])
      expect(persisted.at(-1)).toMatchObject({
        rejectedSuggestionIds: [THEIRS.suggestionId],
      })
      expect(paragraphOf(document, 'ZONEFOREIGN')).toEqual({ marked: false, text: 'Foreign  tail ZONEFOREIGN.' })
    } finally {
      base.destroy()
    }
  })

  it.each([
    ['deletes part of another person\'s insertion', (room: Y.Doc) => rawUpdate(room, 4001, (fragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      content.delete(index + 2, 2)
    })],
    ['formats another person\'s insertion bold', (room: Y.Doc) => rawUpdate(room, 4001, (fragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      content.format(index, 'THEIRS'.length, { bold: {} })
    })],
    ['moves another person\'s insertion onto other text', (room: Y.Doc) => rawUpdate(room, 4001, (fragment) => {
      const { index, text: content } = textWith(fragment, 'THEIRS')
      const attributes = theirsAttributes(content)
      content.format(index, 'THEIRS'.length, { insertion: null })
      content.format(index + 'THEIRS tail '.length, 'ZONEFOREIGN'.length, { insertion: attributes })
    })],
  ] as const)('stays refused for a suggest-only lease whose modified client %s', async (_name, update) => {
    const { persisted, verdicts } = await submitted([{ access: 'suggest', update, user: THIRD }])

    expect(verdicts).toEqual([POLICY_REFUSAL])
    expect(persisted).toEqual([])
  })
})

describe('block commands over pending suggestions', () => {
  // The binding writes a block whose type or place changes as a new copy of
  // the block with its text and marks. A suggestion inside it then stands on
  // new characters that repeat the old ones in the same place, which counts
  // as the same suggestion: a heading, a list, a quote or a code block over
  // pending suggestions is formatting, and they stay pending.
  const IDS = {
    deletion: { patchId: 'a1a1a1a1-0000-4000-8000-0000000000f1', suggestionId: 'a1a1a1a1-0000-4000-8000-000000000001' },
    item: { patchId: 'a1a1a1a1-0000-4000-8000-0000000000f2', suggestionId: 'a1a1a1a1-0000-4000-8000-000000000002' },
    shared: { patchId: 'a1a1a1a1-0000-4000-8000-0000000000f3', suggestionId: 'a1a1a1a1-0000-4000-8000-000000000003' },
    second: { patchId: 'a1a1a1a1-0000-4000-8000-0000000000f4', suggestionId: 'a1a1a1a1-0000-4000-8000-000000000004' },
    own: { patchId: 'a1a1a1a1-0000-4000-8000-0000000000f5', suggestionId: 'a1a1a1a1-0000-4000-8000-000000000005' },
  }
  const deletionMark = (authorId: string, ids: { patchId: string; suggestionId: string }): MarkJson => ({
    type: 'deletion',
    attrs: { authorId, createdAt: CREATED_AT, id: ids.suggestionId, kind: 'deletion', ...ids },
  })
  const BLOCKS = Y.encodeStateAsUpdate(editorJsonToYDoc({
    type: 'doc',
    content: [
      paragraph([text('Plain ZONEPLAIN words.')]),
      paragraph([text('Shared paragraph'), text(' PROPOSAL', [insertionMark(REVIEWER, IDS.shared)]), text(' text.')]),
      paragraph([text('Delete '), text('OLDWORD', [deletionMark(REVIEWER, IDS.deletion)]), text(' after.')]),
      paragraph([text('Second '), text('SECONDPROPOSAL', [insertionMark(REVIEWER, IDS.second)]), text(' after.')]),
      paragraph([text('Own '), text('OWNPROPOSAL', [insertionMark(WRITER, IDS.own)]), text(' after.')]),
      {
        type: 'bulletList',
        content: [{ type: 'listItem', content: [paragraph([text('Item '), text('ITEMPROPOSAL', [insertionMark(REVIEWER, IDS.item)]), text(' end.')])] }],
      },
      paragraph([text('Structure ZONESTRUCT words.')], {
        [INQTRIX_STRUCTURE_SUGGESTION_ATTR]: structure(REVIEWER, 'heading2', FOREIGN_STRUCTURE),
      }),
    ],
  }))

  /** Where a needle starts in the editor document. */
  function find(document: ProseMirrorNode, needle: string): number {
    let found = -1
    document.descendants((node, position) => {
      if (found >= 0) return false
      if (node.isText && node.text?.includes(needle)) {
        found = position + node.text.indexOf(needle)
        return false
      }
      return true
    })
    if (found < 0) throw new Error(`fixture has no text ${needle}`)
    return found
  }

  /** The document a real editor command leaves, over a selection. */
  const command = (
    select: (document: ProseMirrorNode) => { from: number; to: number },
    run: (editor: Editor) => boolean,
  ) => (state: EditorState): Transaction => {
    const editor = new Editor({
      content: state.doc.toJSON(),
      element: null,
      extensions: createEditorSchemaExtensions({ enableUndoRedo: false }),
    })
    try {
      const { from, to } = select(editor.state.doc)
      editor.view.dispatch(editor.state.tr.setSelection(TextSelection.create(editor.state.doc, from, to)))
      if (!run(editor)) throw new Error('the command changed nothing')
      const target = schema.nodeFromJSON(editor.state.doc.toJSON())
      return state.tr.replaceWith(0, state.doc.content.size, target.content)
    } finally {
      editor.destroy()
    }
  }
  const caretIn = (needle: string) => (document: ProseMirrorNode) => ({ from: find(document, needle) + 2, to: find(document, needle) + 2 })
  const between = (first: string, last: string) => (document: ProseMirrorNode) => ({ from: find(document, first), to: find(document, last) })

  /** Every pending suggestion the room holds, with its text. */
  function suggestionTexts(document: JSONContent): Record<string, string> {
    const texts: Record<string, string> = {}
    const visit = (node: JSONContent): void => {
      for (const mark of node.marks ?? []) {
        const id = (mark.attrs as { suggestionId?: string } | undefined)?.suggestionId
        if (id) texts[id] = (texts[id] ?? '') + (node.text ?? '')
      }
      const data = node.attrs?.[INQTRIX_STRUCTURE_SUGGESTION_ATTR] as { suggestionId?: string } | undefined
      if (data?.suggestionId) texts[data.suggestionId] = `structure:${JSON.stringify(data)}`
      for (const child of node.content ?? []) visit(child)
    }
    visit(document)
    return texts
  }

  it.each([
    ['a heading of the paragraph that holds another person\'s insertion', caretIn('Shared'), (editor: Editor) => editor.commands.toggleHeading({ level: 1 })],
    ['a heading of the paragraph that holds another person\'s proposed deletion', caretIn('Delete'), (editor: Editor) => editor.commands.toggleHeading({ level: 2 })],
    ['a list around the paragraph that holds another person\'s insertion', caretIn('Shared'), (editor: Editor) => editor.commands.toggleBulletList()],
    ['a quote around the paragraph that holds another person\'s insertion', caretIn('Shared'), (editor: Editor) => editor.commands.toggleBlockquote()],
    ['a code block of the paragraph that holds another person\'s insertion', caretIn('Shared'), (editor: Editor) => editor.commands.toggleCodeBlock()],
    ['lifting the list item that holds another person\'s insertion out of its list', caretIn('Item'), (editor: Editor) => editor.commands.liftListItem('listItem')],
    ['a list over several blocks that hold other people\'s suggestions', between('Shared', 'Second'), (editor: Editor) => editor.commands.toggleBulletList()],
  ] as const)('keeps every suggestion pending for %s', async (_name, select, run) => {
    expect(await commandOutcome(select, run)).toEqual(untouched())
  })

  // Known limits, kept red on purpose: each case must turn this test into a
  // failure once the service admits it, so the limit is taken off here.
  //
  // A list over a paragraph and an existing list nests that list, and the
  // binding writes the first new item into the old item's text: the old
  // item's characters are not written again in place, and the service
  // refuses the change.
  it.fails('keeps every suggestion pending for a list over several blocks that joins the list holding another person\'s insertion', async () => {
    expect(await commandOutcome(between('Own', 'Item'), (editor) => editor.commands.toggleBulletList())).toEqual(untouched())
  })

  // A structure suggestion stands on its block, and a quote around the block
  // makes the binding write the block anew: the service removes the
  // suggestion instead of following the block.
  it.fails('keeps every suggestion pending for a quote around a block another person proposed as a heading', async () => {
    expect(await commandOutcome(caretIn('ZONESTRUCT'), (editor) => editor.commands.toggleBlockquote())).toEqual(untouched())
  })

  function untouched(): { rejected: string[]; suggestions: Record<string, string>; verdict: string } {
    const initial = documentFrom(BLOCKS, 1)
    try {
      return { rejected: [], suggestions: suggestionTexts(editorYDocToJson(initial)), verdict: 'accepted direct' }
    } finally {
      initial.destroy()
    }
  }

  async function commandOutcome(
    select: (document: ProseMirrorNode) => { from: number; to: number },
    run: (editor: Editor) => boolean,
  ): Promise<{ rejected: string[] | null; suggestions: Record<string, string>; verdict: string }> {
    const room = await openRoom(BLOCKS)
    try {
      const verdict = await room.submit({ access: 'edit', update: (current) => editorUpdate(current, 4001, command(select, run)), user: WRITER })
      return {
        rejected: room.api.persisted.at(-1)?.rejectedSuggestionIds ?? null,
        suggestions: suggestionTexts(editorYDocToJson(room.document)),
        verdict,
      }
    } finally {
      room.document.destroy()
      await room.close()
    }
  }
})
