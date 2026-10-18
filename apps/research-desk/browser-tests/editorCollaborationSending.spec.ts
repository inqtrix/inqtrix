import { expect, test } from '@playwright/test'
import { editorCollaborationRoom } from '@inqtrix/editor-schema'

import { reloadAcceptingLeaveQuestion } from '../../../e2e/leave-question'
import {
  CollaborationRelay,
  CollaborationStack,
  WRITER_A,
  WRITER_B,
  WRITER_C,
  closeWriters,
  describeUpdate,
  editorText,
  expectSaved,
  expectWriterReady,
  insertedText,
  observedStatusKinds,
  openWriter,
  placeCaretAtBlockEnd,
  pressKeys,
  projectionText,
  selectWord,
  settledDurability,
  suggestionsOf,
  typeSlowly,
  watchStatusKinds,
  writerDocument,
  writerText,
  type TransportRecord,
  type WriterPage,
} from './collaborationStack'

// Status kinds that mean the writer lost its connection, its right to write
// or its changes.
const FAILURE_KINDS = [
  'access_revoked',
  'error',
  'origin_rejected',
  'read_only',
  'reconnecting',
  'reload_required',
]

function onlyTransport(writer: WriterPage): TransportRecord {
  expect(writer.transports).toHaveLength(1)
  return writer.transports[0]!
}

/** The updates a writer sent and the service's warnings, for a failure
 * message. */
function describeWriter(stack: CollaborationStack, writer: WriterPage): string {
  const updates = writer.transports.flatMap((transport) => transport.updates())
    .map((update) => describeUpdate(update.payload!))
  return `updates sent: ${updates.join(' ')}; service warnings: ${JSON.stringify(stack.warnings)}`
}

function closesOf(writer: WriterPage): Array<{ code: number; reason: string }> {
  return writer.transports.flatMap((transport) => transport.closes)
}

/** Over the whole observation the page never showed a failure status and
 * logged no console error. */
async function expectNoFailureShown(writer: WriterPage): Promise<void> {
  const kinds = await observedStatusKinds(writer)
  expect(kinds.filter((kind) => FAILURE_KINDS.includes(kind))).toEqual([])
  expect(writer.consoleErrors).toEqual([])
}

/** The writer's live document and the store agree in both projections and
 * in every suggestion. */
async function expectStoreEqualsWriter(
  stack: CollaborationStack,
  writer: WriterPage,
  documentId: string,
): Promise<void> {
  const live = await writerDocument(writer)
  const stored = stack.storeDocument(documentId)
  try {
    expect(projectionText(stored, 'final')).toBe(projectionText(live, 'final'))
    expect(projectionText(stored, 'original')).toBe(projectionText(live, 'original'))
    expect(suggestionsOf(stored)).toEqual(suggestionsOf(live))
  } finally {
    live.destroy()
    stored.destroy()
  }
}

/** Milliseconds from each keystroke to the durable acknowledgement of the
 * update that carried it. The keystrokes insert text only, so the n-th typed
 * character travels in the update whose inserted text first reaches n. */
function keystrokeToAckMs(typedAt: readonly number[], transport: TransportRecord): number[] {
  const acks = new Map(transport.acks().map((ack) => [ack.hash, ack.at]))
  const latencies: number[] = []
  let carried = 0
  for (const update of transport.updates()) {
    const ackAt = acks.get(update.hash)
    if (ackAt === undefined) throw new Error('An update of the writer was never acknowledged')
    const characters = insertedText(update.payload!).length
    for (let index = carried; index < carried + characters && index < typedAt.length; index += 1) {
      latencies.push(ackAt - typedAt[index]!)
    }
    carried += characters
  }
  expect(carried).toBe(typedAt.length)
  return latencies
}

function percentile(values: readonly number[], fraction: number): number {
  const sorted = [...values].sort((left, right) => left - right)
  return sorted[Math.min(sorted.length - 1, Math.ceil(fraction * sorted.length) - 1)]!
}

test('a writer who keeps typing while the store is slow stays connected, keeps one update in flight and loses no character', async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'slow-store-doc'
  const seed = 'Seed paragraph for a slow store'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `${seed}\n\nSecond paragraph.` }],
  })
  const writers: WriterPage[] = []
  try {
    const writerA = await openWriter(browser, stack, WRITER_A, { documentId, seedText: seed })
    writers.push(writerA)
    const writerB = await openWriter(browser, stack, WRITER_B, { documentId, seedText: seed })
    writers.push(writerB)
    await watchStatusKinds(writerA)
    await placeCaretAtBlockEnd(writerA, 0)

    stack.holdPersistence()
    const typed = 'abcdefghijklmnopqrstuvwxy'.repeat(2)
    await typeSlowly(writerA, typed, 150)
    // Nothing was acknowledged yet: the first keystroke is the only update
    // on the wire, everything typed after it waits in the editor.
    const transportA = onlyTransport(writerA)
    expect(transportA.updates()).toHaveLength(1)
    stack.releasePersistence()

    await expectSaved(writerA)
    expect(await editorText(writerA)).toBe(`${seed}${typed}Second paragraph.`)
    expect(stack.storeText(documentId)).toBe(`${seed}${typed}\n\nSecond paragraph.`)
    await expect.poll(() => editorText(writerB)).toBe(`${seed}${typed}Second paragraph.`)
    expect(closesOf(writerA)).toEqual([])
    expect(closesOf(writerB)).toEqual([])
    expect(onlyTransport(writerA).maxUnacknowledged).toBe(1)
    await expectNoFailureShown(writerA)
    expect(stack.rejections()).toEqual({})
    expect(stack.metric('inqtrix_collaboration_ingress_deferred_discarded_total')).toBe(0)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test('three writers typing at human pace in one paragraph stay connected, converge and store every character', async ({ browser }) => {
  test.setTimeout(180_000)
  const documentId = 'three-writers-doc'
  const seed = 'Shared paragraph'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: seed }],
    persistDelayMs: 90,
  })
  const writers: WriterPage[] = []
  try {
    for (const writer of [WRITER_A, WRITER_B, WRITER_C]) {
      writers.push(await openWriter(browser, stack, writer, { documentId, seedText: seed }))
    }
    // Disjoint alphabets: each writer's characters can be read back from the
    // merged paragraph in the order that writer typed them.
    const alphabets = ['abcdefghij', 'KLMNOPQRST', '0123456789']
    const scripts = alphabets.map((alphabet) => alphabet.repeat(11))
    for (const writer of writers) {
      await watchStatusKinds(writer)
      await placeCaretAtBlockEnd(writer, 0)
    }
    const typedAt = await Promise.all(writers.map((writer, index) => (
      typeSlowly(writer, scripts[index]!, 150)
    )))
    for (const writer of writers) await expectSaved(writer)

    const stored = stack.storeText(documentId)
    for (const writer of writers) {
      await expect.poll(() => editorText(writer)).toBe(stored)
    }
    expect(stored.startsWith(seed)).toBe(true)
    const typed = stored.slice(seed.length)
    expect(typed.length).toBe(scripts.join('').length)
    alphabets.forEach((alphabet, index) => {
      const own = [...typed].filter((character) => alphabet.includes(character)).join('')
      expect(own).toBe(scripts[index])
    })
    const latencies: number[] = []
    writers.forEach((writer, index) => {
      const transport = onlyTransport(writer)
      expect(transport.closes).toEqual([])
      expect(transport.maxUnacknowledged).toBe(1)
      latencies.push(...keystrokeToAckMs(typedAt[index]!, transport))
    })
    for (const writer of writers) await expectNoFailureShown(writer)
    expect(stack.rejections()).toEqual({})
    const p95 = percentile(latencies, 0.95)
    test.info().annotations.push({
      description: `keystroke to acknowledgement over ${latencies.length} keystrokes: `
        + `p50 ${percentile(latencies, 0.5)} ms, p95 ${p95} ms, max ${Math.max(...latencies)} ms`,
      type: 'measurement',
    })
    expect(p95).toBeLessThan(1_000)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test('a suggest-mode writer who corrects at human pace while the store is slow stays connected and every accepted suggestion is stored', async ({ browser }) => {
  test.setTimeout(150_000)
  const documentId = 'suggest-corrections-doc'
  const stack = await CollaborationStack.start({
    documents: [{
      id: documentId,
      markdown: 'Seed paragraph for suggestions\n\nEditor paragraph\n\nOriginal text to delete.',
    }],
    persistDelayMs: 300,
  })
  const writers: WriterPage[] = []
  try {
    const suggester = await openWriter(browser, stack, WRITER_A, {
      access: 'suggest',
      documentId,
      seedText: 'Seed paragraph for suggestions',
    })
    writers.push(suggester)
    const editor = await openWriter(browser, stack, WRITER_B, {
      documentId,
      seedText: 'Seed paragraph for suggestions',
    })
    writers.push(editor)
    await expect(suggester.state).toHaveAttribute('data-write-mode', 'suggest')
    for (const writer of writers) await watchStatusKinds(writer)

    await placeCaretAtBlockEnd(editor, 1)
    const suggesting = (async () => {
      await placeCaretAtBlockEnd(suggester, 0)
      // Two correction runs over the writer's own fresh text.
      await pressKeys(suggester, [
        ...'Suggestid', 'Backspace', 'Backspace',
        ...'edWordnig', 'Backspace', 'Backspace', 'Backspace',
        ...'ing',
      ], 150)
      // A run of deletion marks on original text.
      await placeCaretAtBlockEnd(suggester, 2)
      await pressKeys(suggester, ['Backspace', 'Backspace', 'Backspace', 'Backspace'], 150)
    })()
    const editing = typeSlowly(editor, 'Direct', 150)
    await Promise.all([suggesting, editing])
    await settledDurability(suggester, 30_000)
    expect(stack.rejections(), describeWriter(stack, suggester)).toEqual({})
    for (const writer of writers) await expectSaved(writer, 30_000)

    for (const writer of writers) {
      expect(closesOf(writer)).toEqual([])
      await expectNoFailureShown(writer)
      await expectStoreEqualsWriter(stack, writer, documentId)
    }
    expect(stack.rejections()).toEqual({})
    // Read as a user would: the proposal and the marked deletion in the
    // final text, the untouched original underneath.
    expect(stack.storeText(documentId, 'final')).toBe(
      'Seed paragraph for suggestionsSuggestedWording\n\nEditor paragraphDirect\n\nOriginal text to del',
    )
    expect(stack.storeText(documentId, 'original')).toBe(
      'Seed paragraph for suggestions\n\nEditor paragraphDirect\n\nOriginal text to delete.',
    )
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test('an edit-access writer who switches modes and edits inside its own suggestion while an update waits is never rejected', async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'mode-switch-doc'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: 'First paragraph\n\nSecond paragraph' }],
  })
  const writers: WriterPage[] = []
  try {
    const writer = await openWriter(browser, stack, WRITER_A, {
      documentId,
      seedText: 'First paragraph',
    })
    writers.push(writer)
    await watchStatusKinds(writer)
    const suggestMode = writer.page.getByTestId('writer-mode-suggest')
    const editMode = writer.page.getByTestId('writer-mode-edit')

    stack.holdPersistence()
    await suggestMode.click()
    await expect(writer.state).toHaveAttribute('data-write-mode', 'suggest')
    await placeCaretAtBlockEnd(writer, 0)
    await typeSlowly(writer, 'abc', 120)
    // Edit mode: first a plain change elsewhere, then one inside the own
    // suggestion, then a plain change again, then suggest mode once more.
    await editMode.click()
    await expect(writer.state).toHaveAttribute('data-write-mode', 'edit')
    await placeCaretAtBlockEnd(writer, 1)
    await typeSlowly(writer, 'Y', 120)
    await placeCaretAtBlockEnd(writer, 0)
    await pressKeys(writer, ['ArrowLeft', 'ArrowLeft', 'X'], 120)
    await placeCaretAtBlockEnd(writer, 1)
    await typeSlowly(writer, 'W', 120)
    await suggestMode.click()
    await expect(writer.state).toHaveAttribute('data-write-mode', 'suggest')
    await typeSlowly(writer, 'Z', 120)
    expect(onlyTransport(writer).updates()).toHaveLength(1)
    stack.releasePersistence()

    await expectSaved(writer)
    expect(closesOf(writer)).toEqual([])
    await expectNoFailureShown(writer)
    expect(stack.rejections()).toEqual({})
    await expectStoreEqualsWriter(stack, writer, documentId)
    expect(stack.storeText(documentId, 'final')).toBe('First paragraphaXbc\n\nSecond paragraphYWZ')
    // The first keystroke, the rest of the suggestion, the plain change, the
    // change inside the suggestion, the second plain change and the last
    // suggestion: each group of one mode and one kind travels on its own.
    expect(onlyTransport(writer).updates()).toHaveLength(6)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test('an editor reopened in suggest mode on a lingering document merges its suggestion typing', async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'linger-doc'
  const seed = 'Linger paragraph'
  const stack = await CollaborationStack.start({ documents: [{ id: documentId, markdown: seed }] })
  const writers: WriterPage[] = []
  try {
    const writer = await openWriter(browser, stack, WRITER_A, { documentId, seedText: seed })
    writers.push(writer)
    await placeCaretAtBlockEnd(writer, 0)
    stack.holdPersistence()
    await typeSlowly(writer, 'pq', 120)
    stack.releasePersistence()
    await expectSaved(writer)

    await writer.page.getByTestId('writer-mode-suggest').click()
    await expect(writer.state).toHaveAttribute('data-write-mode', 'suggest')
    const toggle = writer.page.getByTestId('writer-toggle-pane')
    await toggle.click()
    await expect(writer.state).toHaveCount(0)
    await toggle.click()
    await expectWriterReady(writer, `${seed}pq`)
    await expect(writer.state).toHaveAttribute('data-write-mode', 'suggest')
    await watchStatusKinds(writer)
    // The lingering controller kept its socket.
    const transport = onlyTransport(writer)
    const before = transport.updates().length

    await placeCaretAtBlockEnd(writer, 0)
    stack.holdPersistence()
    await typeSlowly(writer, 'vwxyz', 120)
    stack.releasePersistence()
    await expectSaved(writer)

    // The first suggestion keystroke alone, the four after it in one update.
    expect(transport.updates().length - before).toBe(2)
    expect(stack.rejections()).toEqual({})
    await expectNoFailureShown(writer)
    await expectStoreEqualsWriter(stack, writer, documentId)
    expect(stack.storeText(documentId, 'final')).toBe(`${seed}pqvwxyz`)
    expect(stack.storeText(documentId, 'original')).toBe(`${seed}pq`)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test('an editor opened on another document in suggest mode merges its suggestion typing there', async ({ browser }) => {
  test.setTimeout(120_000)
  const first = 'first-writer-doc'
  const second = 'second-writer-doc'
  const stack = await CollaborationStack.start({
    documents: [
      { id: first, markdown: 'First document' },
      { id: second, markdown: 'Second document' },
    ],
  })
  const writers: WriterPage[] = []
  try {
    const writer = await openWriter(browser, stack, WRITER_A, {
      alternateDocumentId: second,
      documentId: first,
      seedText: 'First document',
    })
    writers.push(writer)
    await writer.page.getByTestId('writer-mode-suggest').click()
    await expect(writer.state).toHaveAttribute('data-write-mode', 'suggest')
    await writer.page.getByTestId('writer-switch-document').click()
    await expect(writer.state).toHaveAttribute('data-document-id', second)
    await expectWriterReady(writer, 'Second document')
    await expect(writer.state).toHaveAttribute('data-write-mode', 'suggest')
    await watchStatusKinds(writer)

    await placeCaretAtBlockEnd(writer, 0)
    stack.holdPersistence()
    await typeSlowly(writer, 'vwxyz', 120)
    stack.releasePersistence()
    await expectSaved(writer)

    const secondRoom = editorCollaborationRoom(second, 1)
    const updates = writer.transports.flatMap((transport) => transport.updates(secondRoom))
    // The first suggestion keystroke alone, the four after it in one update.
    expect(updates).toHaveLength(2)
    expect(stack.rejections()).toEqual({})
    await expectNoFailureShown(writer)
    await expectStoreEqualsWriter(stack, writer, second)
    expect(stack.storeText(second, 'final')).toBe('Second documentvwxyz')
    expect(stack.storeText(second, 'original')).toBe('Second document')
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test('an editor reopened in edit mode after suggest mode sends a plain change and a change inside its own suggestion apart', async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'reopen-edit-doc'
  const stack = await CollaborationStack.start({
    documents: [{
      id: documentId,
      markdown: 'Own suggestion paragraph\n\nPlain paragraph one\n\nPlain paragraph two',
    }],
  })
  const writers: WriterPage[] = []
  try {
    const writer = await openWriter(browser, stack, WRITER_A, {
      documentId,
      seedText: 'Own suggestion paragraph',
    })
    writers.push(writer)
    // Suggest mode entered on the mounted editor, through its policy switch.
    await writer.page.getByTestId('writer-mode-suggest').click()
    await expect(writer.state).toHaveAttribute('data-write-mode', 'suggest')
    await placeCaretAtBlockEnd(writer, 0)
    await typeSlowly(writer, 'abc', 120)
    await expectSaved(writer)

    // Edit mode chosen while the document is closed; the reopened editor
    // starts in it.
    const toggle = writer.page.getByTestId('writer-toggle-pane')
    await toggle.click()
    await expect(writer.state).toHaveCount(0)
    await writer.page.getByTestId('writer-mode-edit').click()
    await toggle.click()
    await expectWriterReady(writer, 'Own suggestion paragraphabc')
    await expect(writer.state).toHaveAttribute('data-write-mode', 'edit')
    await watchStatusKinds(writer)
    const transport = onlyTransport(writer)
    const before = transport.updates().length

    stack.holdPersistence()
    await placeCaretAtBlockEnd(writer, 1)
    await typeSlowly(writer, 'X', 120)
    await placeCaretAtBlockEnd(writer, 0)
    await pressKeys(writer, ['ArrowLeft', 'Q'], 120)
    await placeCaretAtBlockEnd(writer, 2)
    await typeSlowly(writer, 'R', 120)
    stack.releasePersistence()
    await settledDurability(writer)
    expect(stack.rejections(), describeWriter(stack, writer)).toEqual({})
    await expectSaved(writer)

    const updates = transport.updates().slice(before)
    expect(updates.map((update) => insertedText(update.payload!))).toEqual(['X', 'Q', 'R'])
    expect(stack.rejections()).toEqual({})
    await expectNoFailureShown(writer)
    await expectStoreEqualsWriter(stack, writer, documentId)
    expect(stack.storeText(documentId, 'final')).toBe(
      'Own suggestion paragraphabQc\n\nPlain paragraph oneX\n\nPlain paragraph twoR',
    )
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test('every keystroke typed within one acknowledgement cycle before a reload is stored', async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'reload-edit-doc'
  const seed = 'Reload paragraph'
  const stack = await CollaborationStack.start({ documents: [{ id: documentId, markdown: seed }] })
  const relay = await CollaborationRelay.start(stack)
  const writers: WriterPage[] = []
  try {
    const writer = await openWriter(browser, stack, WRITER_A, { documentId, relay, seedText: seed })
    writers.push(writer)
    await placeCaretAtBlockEnd(writer, 0)
    stack.holdPersistence()
    await typeSlowly(writer, 'abcdefghijkl', 80)
    await writer.page.waitForTimeout(200)
    const beforeReload = await writerText(writer)
    expect(beforeReload).toBe(`${seed}abcdefghijkl`)
    const firstSocket = relay.transports[0]!
    // One update waits for its acknowledgement; the rest is still in the page.
    expect(firstSocket.updates()).toHaveLength(1)

    await reloadAcceptingLeaveQuestion(writer.page, { expectQuestion: false })
    stack.releasePersistence()

    await expect.poll(() => stack.storeText(documentId), { timeout: 20_000 }).toBe(beforeReload)
    // The page sent what it had assembled at once when it was left.
    expect(firstSocket.updates()).toHaveLength(2)
    await expectWriterReady(writer, `${seed}abcdefghijkl`)
    expect(stack.rejections()).toEqual({})
    expect(stack.storedUpdateCount(documentId)).toBe(2)
    expect(writer.consoleErrors).toEqual([])
  } finally {
    await closeWriters(writers)
    await relay.stop()
    await stack.stop()
  }
})

test('every suggestion typed within one acknowledgement cycle before a reload is stored', async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'reload-suggest-doc'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: 'Reload paragraph\n\nOriginal words stay.' }],
  })
  const relay = await CollaborationRelay.start(stack)
  const writers: WriterPage[] = []
  try {
    const suggester = await openWriter(browser, stack, WRITER_A, {
      access: 'suggest',
      documentId,
      relay,
      seedText: 'Reload paragraph',
    })
    writers.push(suggester)
    await placeCaretAtBlockEnd(suggester, 0)
    stack.holdPersistence()
    await pressKeys(suggester, [
      ...'Planx', 'Backspace',
      ...'nedStepq', 'Backspace', 'Backspace',
      ...'ps',
    ], 80)
    await placeCaretAtBlockEnd(suggester, 1)
    await pressKeys(suggester, ['Backspace', 'Backspace', 'Backspace'], 80)
    await suggester.page.waitForTimeout(200)
    const live = await writerDocument(suggester)
    const beforeReload = {
      final: projectionText(live, 'final'),
      original: projectionText(live, 'original'),
      suggestions: suggestionsOf(live),
    }
    live.destroy()
    expect(beforeReload.final).toBe('Reload paragraphPlannedSteps\n\nOriginal words st')
    expect(beforeReload.original).toBe('Reload paragraph\n\nOriginal words stay.')

    await reloadAcceptingLeaveQuestion(suggester.page, { expectQuestion: false })
    stack.releasePersistence()

    await expect.poll(() => stack.storeText(documentId, 'final'), { timeout: 20_000 })
      .toBe(beforeReload.final)
    expect(stack.storeText(documentId, 'original')).toBe(beforeReload.original)
    const stored = stack.storeDocument(documentId)
    expect(suggestionsOf(stored)).toEqual(beforeReload.suggestions)
    stored.destroy()
    expect(stack.rejections()).toEqual({})
    await expectWriterReady(suggester, 'Reload paragraphPlannedSteps')
    expect(suggester.consoleErrors).toEqual([])
  } finally {
    await closeWriters(writers)
    await relay.stop()
    await stack.stop()
  }
})

test('a page that holds more changes than it may send at once asks before a reload and stores what it sent', async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'leave-question-doc'
  const original = 'Original sentence that the writer marks for deletion one character at a time.'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `Reload paragraph\n\n${original}` }],
  })
  const relay = await CollaborationRelay.start(stack)
  const writers: WriterPage[] = []
  try {
    const suggester = await openWriter(browser, stack, WRITER_A, {
      access: 'suggest',
      documentId,
      relay,
      seedText: 'Reload paragraph',
    })
    writers.push(suggester)
    await placeCaretAtBlockEnd(suggester, 1)
    stack.holdPersistence()
    // Every deletion mark on original text travels as its own update: the
    // first waits for its acknowledgement, the others wait in the page.
    await pressKeys(suggester, Array.from({ length: 32 }, () => 'Backspace'), 80)
    await suggester.page.waitForTimeout(200)
    // Counted in the page's own document, not in keys pressed: the page must
    // hold more marks than the one in flight and the 27 it may send at once.
    const paragraphs = (await writerText(suggester)).split('\n\n')
    expect(paragraphs).toHaveLength(2)
    const kept = paragraphs[1]!
    expect(original.startsWith(kept)).toBe(true)
    expect(original.length - kept.length).toBeGreaterThan(28)
    const firstSocket = relay.transports[0]!
    expect(firstSocket.updates()).toHaveLength(1)

    await reloadAcceptingLeaveQuestion(suggester.page, { expectQuestion: true })
    stack.releasePersistence()

    // The published frame limit of 32 less four control frames bounds the
    // updates that may wait for an acknowledgement: the one in flight and 27
    // more went out as the page left, the rest could not, and the page asked
    // first.
    await expect.poll(() => stack.storedUpdateCount(documentId), { timeout: 20_000 }).toBe(28)
    expect(firstSocket.updates()).toHaveLength(28)
    const stored = `Reload paragraph\n\n${original.slice(0, -28)}`
    expect(stack.storeText(documentId, 'final')).toBe(stored)
    expect(stack.storeText(documentId, 'original')).toBe(`Reload paragraph\n\n${original}`)
    expect(stack.rejections()).toEqual({})
    await expectWriterReady(suggester, 'Reload paragraph')
    await expect.poll(() => writerText(suggester)).toBe(stored)
    expect(suggester.consoleErrors).toEqual([])
  } finally {
    await closeWriters(writers)
    await relay.stop()
    await stack.stop()
  }
})

test('two writers who bold the same word at the start of a paragraph at the same time both keep writing', async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'concurrent-bold-doc'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: 'Hello world\n\nOther paragraph' }],
  })
  const writers: WriterPage[] = []
  try {
    for (const writer of [WRITER_A, WRITER_B, WRITER_C]) {
      writers.push(await openWriter(browser, stack, writer, { documentId, seedText: 'Hello world' }))
    }
    const [writerA, writerB, observer] = writers as [WriterPage, WriterPage, WriterPage]
    for (const writer of writers) await watchStatusKinds(writer)

    // The other writers' carets stay away from the word, so no caret label
    // sits inside the selection a writer extends.
    await placeCaretAtBlockEnd(writerB, 1)
    await placeCaretAtBlockEnd(observer, 1)
    stack.holdPersistence()
    expect(await selectWord(writerA, 0, 'Hello')).toBe('Hello')
    await writerA.page.keyboard.press('ControlOrMeta+b')
    await placeCaretAtBlockEnd(writerA, 1)
    expect(await selectWord(writerB, 0, 'Hello')).toBe('Hello')
    await writerB.page.keyboard.press('ControlOrMeta+b')
    // Both toggles left the base state before either was stored.
    await expect.poll(() => onlyTransport(writerA).updates().length).toBe(1)
    await expect.poll(() => onlyTransport(writerB).updates().length).toBe(1)
    stack.releasePersistence()

    for (const writer of [writerA, writerB]) await settledDurability(writer)
    expect(
      stack.rejections(),
      `${describeWriter(stack, writerA)} | ${describeWriter(stack, writerB)}`,
    ).toEqual({})
    for (const writer of [writerA, writerB]) await expectSaved(writer)
    const bold = '**Hello** world\n\nOther paragraph'
    await expect.poll(() => stack.storeText(documentId)).toBe(bold)
    for (const writer of writers) {
      await expect.poll(() => writerText(writer)).toBe(bold)
      await expect(writer.state).toHaveAttribute('data-can-edit', 'true')
      await expectNoFailureShown(writer)
      expect(closesOf(writer)).toEqual([])
    }
    expect(stack.rejections()).toEqual({})
    expect(stack.metric('inqtrix_collaboration_formatting_cleanups_accepted_total')).toBe(1)
    // The writer who only received both toggles sent nothing: its own copy
    // of the redundant-formatting cleanup stays local.
    expect(onlyTransport(observer).updates()).toEqual([])
    // Both writers keep writing.
    await placeCaretAtBlockEnd(writerB, 1)
    await typeSlowly(writerB, 'MARKE1', 60)
    await expectSaved(writerB)
    expect(stack.storeText(documentId)).toBe('**Hello** world\n\nOther paragraphMARKE1')
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})
