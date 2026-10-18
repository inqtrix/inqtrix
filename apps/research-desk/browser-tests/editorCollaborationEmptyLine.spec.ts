import { expect, test, type TestInfo } from '@playwright/test'

import {
  CollaborationStack,
  WRITER_A,
  WRITER_B,
  closeWriters,
  editorText,
  observedNotices,
  observedStatusKinds,
  openWriter,
  placeCaretAtBlockEnd,
  settledDurability,
  typeSlowly,
  watchNotices,
  watchStatusKinds,
  type WriterPage,
} from './collaborationStack'

// Two people type into the same empty line at the same time: a stored empty
// line both click into, or a line one of them has just created with Enter.
// Every empty line holds one empty text node from the update that created it,
// so both writers' characters go into that one node, as in a line that
// already holds text. Normal collaborative work shows no notice and loses
// nothing: both writers stay connected and writable, every word stands in the
// line once and whole, and both editors and the store agree.

const FIRST_LINE = 'Erster Absatz.'
const LAST_LINE = 'Letzter Absatz.'
const EMPTY_LINE_DOCUMENT = {
  content: [
    { content: [{ text: FIRST_LINE, type: 'text' }], type: 'paragraph' },
    { type: 'paragraph' },
    { content: [{ text: LAST_LINE, type: 'text' }], type: 'paragraph' },
  ],
  type: 'doc',
}
const TWO_LINE_DOCUMENT = {
  content: [
    { content: [{ text: FIRST_LINE, type: 'text' }], type: 'paragraph' },
    { content: [{ text: LAST_LINE, type: 'text' }], type: 'paragraph' },
  ],
  type: 'doc',
}

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

/** The updates a writer's page sent so far, over all its sockets. */
function updatesSent(writer: WriterPage): number {
  return writer.transports.reduce((count, transport) => count + transport.updates().length, 0)
}

/** What each writer and the store ended with, once every page stored its
 * changes or stopped with a failure. */
async function outcome(
  stack: CollaborationStack,
  documentId: string,
  writers: readonly WriterPage[],
  words: readonly string[],
  testInfo: TestInfo,
) {
  const durability: string[] = []
  for (const writer of writers) durability.push(await settledDurability(writer))
  const stored = stack.storeText(documentId).split('\n\n')
  const line = stored[1] ?? ''
  const joined = [words.join(''), [...words].reverse().join('')]
  const notices = []
  for (const writer of writers) {
    const observed = await observedNotices(writer)
    // An absence is only read from an observer that saw the editor change.
    expect(observed.editorChanges).toBeGreaterThan(0)
    notices.push(observed.shown)
  }
  const failures = []
  for (const writer of writers) {
    failures.push((await observedStatusKinds(writer)).filter((kind) => FAILURE_KINDS.includes(kind)))
  }
  const editors = []
  for (const [index, writer] of writers.entries()) {
    editors.push(await editorText(writer))
    await writer.page.screenshot({ path: testInfo.outputPath(`writer-${index + 1}.png`) })
  }
  return {
    closes: writers.map((writer) => writer.transports.flatMap((transport) => transport.closes)),
    consoleErrors: writers.map((writer) => writer.consoleErrors),
    durability,
    editorsShowTheStore: editors.every((text) => text === stored.join('')),
    failures,
    line: joined.includes(line) ? 'both words once, each whole' : line,
    notices,
    otherLines: [stored[0], stored[2], stored.length],
    rejections: stack.rejections(),
  }
}

const SETTLED = {
  closes: [[], []],
  consoleErrors: [[], []],
  durability: ['saved', 'saved'],
  editorsShowTheStore: true,
  failures: [[], []],
  line: 'both words once, each whole',
  notices: [[], []],
  otherLines: [FIRST_LINE, LAST_LINE, 3],
  rejections: {},
}

test('two writers who click into the same empty line and type at the same time stay connected, see no notice and keep both words', async ({ browser }, testInfo) => {
  test.setTimeout(120_000)
  const documentId = 'empty-line-doc'
  const stack = await CollaborationStack.start({
    documents: [{ content: EMPTY_LINE_DOCUMENT, id: documentId }],
  })
  const writers: WriterPage[] = []
  try {
    writers.push(await openWriter(browser, stack, WRITER_A, { documentId, seedText: FIRST_LINE }))
    writers.push(await openWriter(browser, stack, WRITER_B, { documentId, seedText: FIRST_LINE }))
    const [first, second] = writers as [WriterPage, WriterPage]
    for (const writer of writers) {
      await watchStatusKinds(writer)
      await watchNotices(writer)
      await placeCaretAtBlockEnd(writer, 1)
    }

    // The store takes a moment. The first writer's first letter is judged
    // and waits for its row while the second writer types: neither editor
    // has the other's letters when it writes its own.
    stack.holdPersistence()
    await typeSlowly(first, 'MARKE1', 60)
    await typeSlowly(second, 'MARKE2', 60)
    await expect.poll(() => writers.map(updatesSent)).toEqual([1, 1])
    stack.releasePersistence()

    expect(await outcome(stack, documentId, writers, ['MARKE1', 'MARKE2'], testInfo)).toEqual(SETTLED)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test('two writers who keep typing words into the same empty line at human pace stay connected, see no notice and keep every word whole', async ({ browser }, testInfo) => {
  test.setTimeout(120_000)
  const documentId = 'empty-line-words-doc'
  const stack = await CollaborationStack.start({
    documents: [{ content: EMPTY_LINE_DOCUMENT, id: documentId }],
    persistDelayMs: 90,
  })
  const writers: WriterPage[] = []
  try {
    writers.push(await openWriter(browser, stack, WRITER_A, { documentId, seedText: FIRST_LINE }))
    writers.push(await openWriter(browser, stack, WRITER_B, { documentId, seedText: FIRST_LINE }))
    const [first, second] = writers as [WriterPage, WriterPage]
    for (const writer of writers) {
      await watchStatusKinds(writer)
      await watchNotices(writer)
      await placeCaretAtBlockEnd(writer, 1)
    }

    // Both start in the same instant: each first letter leaves its page
    // before the other's reaches it. Then both type on while the other's
    // letters, and whatever their editors write in answer, are on their way.
    stack.holdPersistence()
    await Promise.all([
      typeSlowly(first, 'M', 0),
      typeSlowly(second, 'M', 0),
    ])
    await expect.poll(() => writers.map(updatesSent)).toEqual([1, 1])
    stack.releasePersistence()
    await Promise.all([
      typeSlowly(first, 'ARKE1MARKE3', 120),
      typeSlowly(second, 'ARKE2MARKE4', 120),
    ])

    expect(await outcome(stack, documentId, writers, ['MARKE1MARKE3', 'MARKE2MARKE4'], testInfo)).toEqual(SETTLED)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test('a writer who presses Enter and a writer who clicks into the new line and both type words at human pace stay connected, see no notice and keep every word whole', async ({ browser }, testInfo) => {
  test.setTimeout(120_000)
  const documentId = 'enter-line-words-doc'
  const stack = await CollaborationStack.start({
    documents: [{ content: TWO_LINE_DOCUMENT, id: documentId }],
    persistDelayMs: 90,
  })
  const writers: WriterPage[] = []
  try {
    writers.push(await openWriter(browser, stack, WRITER_A, { documentId, seedText: FIRST_LINE }))
    writers.push(await openWriter(browser, stack, WRITER_B, { documentId, seedText: FIRST_LINE }))
    const [first, second] = writers as [WriterPage, WriterPage]
    for (const writer of writers) {
      await watchStatusKinds(writer)
      await watchNotices(writer)
    }

    // The first writer presses Enter at the end of the first line; the
    // second sees the new empty line and clicks into it.
    await placeCaretAtBlockEnd(first, 0)
    await first.page.keyboard.press('Enter')
    await expect.poll(() => second.editor.locator(':scope > *').count()).toBe(3)
    await placeCaretAtBlockEnd(second, 1)

    // Both type words into that line at the same time, each first letter
    // leaving its page before the other's reaches it.
    stack.holdPersistence()
    await Promise.all([
      typeSlowly(first, 'M', 0),
      typeSlowly(second, 'M', 0),
    ])
    await expect.poll(() => writers.map(updatesSent)).toEqual([2, 1])
    stack.releasePersistence()
    await Promise.all([
      typeSlowly(first, 'ARKE1MARKE3', 120),
      typeSlowly(second, 'ARKE2MARKE4', 120),
    ])

    expect(await outcome(stack, documentId, writers, ['MARKE1MARKE3', 'MARKE2MARKE4'], testInfo)).toEqual(SETTLED)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})
