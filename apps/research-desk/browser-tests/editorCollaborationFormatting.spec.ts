import { expect, test } from '@playwright/test'

import {
  CollaborationStack,
  WRITER_A,
  WRITER_B,
  closeWriters,
  editorText,
  expectSaved,
  liveProposals,
  liveRuns,
  observedNotices,
  observedStatusKinds,
  openWriter,
  placeCaretAfter,
  proposeInTheMiddle,
  selectFromTo,
  shownText,
  storedProposals,
  storedRuns,
  watchNotices,
  watchStatusKinds,
  type Run,
  type WriterPage,
} from './collaborationStack'

// A writer with edit rights formats text that holds another writer's
// pending insertion like any other text: the formatting applies to the
// proposed characters too, and the insertion stays pending under its
// author. Only a mark that would take the suggestion mark off its
// characters (inline code) leaves them out. Normal work shows no line,
// banner or hint to either writer.

const SEED = 'Shared paragraph text'

// Status kinds that mean the writer lost its connection, its right to write
// or its changes.
const FAILURE_KINDS = ['access_revoked', 'error', 'origin_rejected', 'read_only', 'reconnecting', 'reload_required']

/** Every writer saw its editor change and was shown nothing: no alert,
 * dialog, toast, refusal line or status message, and no failure status;
 * each ends with nothing waiting to be saved and reads "Gespeichert". */
async function expectNothingShown(writers: readonly WriterPage[]): Promise<void> {
  for (const writer of writers) {
    const notices = await observedNotices(writer)
    expect(notices.editorChanges, writer.writer.name).toBeGreaterThan(0)
    expect(notices.shown, writer.writer.name).toEqual([])
    expect((await observedStatusKinds(writer)).filter((kind) => FAILURE_KINDS.includes(kind))).toEqual([])
    await expect(writer.state).toHaveAttribute('data-has-unconfirmed', 'false')
    await expect(writer.state).toHaveAttribute('data-durability-status', /^(idle|saved)$/)
    await expect(writer.status.locator('[data-editor-status-kind]')).toHaveAttribute('data-editor-status-kind', 'saved')
    await expect(writer.state).toHaveAttribute('data-update-rejected', 'false')
    expect(writer.consoleErrors).toEqual([])
  }
}

async function openPair(stack: CollaborationStack, browser: Parameters<typeof openWriter>[0], documentId: string): Promise<{
  author: WriterPage
  editor: WriterPage
}> {
  const author = await openWriter(browser, stack, WRITER_A, { access: 'suggest', documentId, seedText: SEED })
  const editor = await openWriter(browser, stack, WRITER_B, { documentId, seedText: SEED })
  await expect(author.state).toHaveAttribute('data-write-mode', 'suggest')
  await expect(editor.state).toHaveAttribute('data-write-mode', 'edit')
  return { author, editor }
}

test("bold in edit mode across another writer's pending insertion formats all of it, and the insertion stays pending under its author for both writers", async ({ browser }, testInfo) => {
  test.setTimeout(120_000)
  const documentId = 'edit-formatting-across-doc'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `${SEED}\n\nSecond paragraph` }],
  })
  const writers: WriterPage[] = []
  try {
    const { author, editor } = await openPair(stack, browser, documentId)
    writers.push(author, editor)

    await proposeInTheMiddle(author, 'MARKE7')
    await expect.poll(() => editorText(editor)).toContain('Shared paragraph MARKE7 text')
    const proposed = storedProposals(stack, documentId)
    expect(proposed.suggestions).toEqual([expect.stringMatching(new RegExp(`^insertion:.+:${WRITER_A.id}$`))])
    await expect.poll(() => liveProposals(editor)).toEqual(proposed.proposals)
    const rowsBefore = stack.storedRows(documentId).length
    for (const writer of writers) {
      await watchStatusKinds(writer)
      await watchNotices(writer)
    }

    // From the word before the proposal to the word after it.
    await selectFromTo(editor, 0, 'paragraph', 'text', 'paragraph MARKE7 text')
    await editor.page.keyboard.press('ControlOrMeta+b')

    const formatted: Run[] = [
      { marks: [], text: 'Shared ' },
      { marks: ['bold'], text: 'paragraph' },
      { marks: ['bold', 'insertion'], text: ' MARKE7' },
      { marks: ['bold'], text: ' text' },
    ]
    await expectSaved(editor)
    await expect.poll(() => liveRuns(editor)).toEqual(formatted)
    await expect.poll(() => storedRuns(stack, documentId)).toEqual(formatted)
    await expect.poll(() => liveRuns(author)).toEqual(formatted)
    // The service took the change as the editor's direct edit: no patch
    // changed, nothing was removed, and the proposal is still its author's.
    expect(stack.rejections()).toEqual({})
    const rows = stack.storedRows(documentId)
    expect(rows.length).toBe(rowsBefore + 1)
    expect(rows.at(-1)!.input).toMatchObject({
      actorKind: 'human',
      changeKind: 'direct',
      patches: [],
      rejectedSuggestionIds: [],
      suggestionIds: [],
    })
    expect(storedProposals(stack, documentId).suggestions).toEqual(proposed.suggestions)
    for (const writer of writers) {
      // On screen: the proposal is bold and still shown as proposed.
      expect(await shownText(writer, 'ins[data-suggestion-id]')).toEqual([' MARKE7'])
      await expect(writer.editor.locator('ins strong, strong ins')).toHaveCount(1)
    }
    await editor.page.getByTestId(editor.testIds.surface).screenshot({ path: testInfo.outputPath('editor-bold.png') })
    await author.page.getByTestId(author.testIds.surface).screenshot({ path: testInfo.outputPath('author-bold.png') })

    await expectNothingShown(writers)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test("inline code in edit mode across another writer's pending insertion leaves the insertion out without a word, and a heading of its paragraph keeps it pending", async ({ browser }, testInfo) => {
  test.setTimeout(120_000)
  const documentId = 'edit-code-heading-doc'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `${SEED}\n\nSecond paragraph` }],
  })
  const writers: WriterPage[] = []
  try {
    const { author, editor } = await openPair(stack, browser, documentId)
    writers.push(author, editor)

    await proposeInTheMiddle(author, 'MARKE8')
    await expect.poll(() => editorText(editor)).toContain('Shared paragraph MARKE8 text')
    const proposed = storedProposals(stack, documentId)
    expect(proposed.suggestions).toHaveLength(1)
    await expect.poll(() => liveProposals(editor)).toEqual(proposed.proposals)
    for (const writer of writers) {
      await watchStatusKinds(writer)
      await watchNotices(writer)
    }

    // Inline code would take the proposal's mark off its characters: it
    // formats the original text around the proposal only.
    await selectFromTo(editor, 0, 'paragraph', 'text', 'paragraph MARKE8 text')
    await editor.page.keyboard.press('ControlOrMeta+e')
    const coded: Run[] = [
      { marks: [], text: 'Shared ' },
      { marks: ['code'], text: 'paragraph' },
      { marks: ['insertion'], text: ' MARKE8' },
      { marks: ['code'], text: ' text' },
    ]
    await expectSaved(editor)
    await expect.poll(() => liveRuns(editor)).toEqual(coded)
    await expect.poll(() => storedRuns(stack, documentId)).toEqual(coded)
    await expect.poll(() => liveRuns(author)).toEqual(coded)
    expect(storedProposals(stack, documentId)).toEqual(proposed)
    await editor.page.getByTestId(editor.testIds.surface).screenshot({ path: testInfo.outputPath('editor-code.png') })

    // A heading of the paragraph that holds the proposal: the proposal
    // stays pending, for both writers.
    await placeCaretAfter(editor, 0, 'Shared', ' paragraph MARKE8 text')
    await editor.page.keyboard.press('ControlOrMeta+Alt+2')
    await expect(editor.editor.locator('h2')).toContainText('MARKE8')
    await expectSaved(editor)
    await expect(author.editor.locator('h2')).toContainText('MARKE8')
    expect(stack.rejections()).toEqual({})
    expect(stack.storedRows(documentId).at(-1)!.input).toMatchObject({
      changeKind: 'direct',
      patches: [],
      rejectedSuggestionIds: [],
    })
    expect(storedProposals(stack, documentId).suggestions).toEqual(proposed.suggestions)
    for (const writer of writers) {
      await expect.poll(() => liveProposals(writer)).toEqual(storedProposals(stack, documentId).proposals)
      expect(await shownText(writer, 'ins[data-suggestion-id]')).toEqual([' MARKE8'])
    }
    await author.page.getByTestId(author.testIds.surface).screenshot({ path: testInfo.outputPath('author-heading.png') })

    await expectNothingShown(writers)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})
