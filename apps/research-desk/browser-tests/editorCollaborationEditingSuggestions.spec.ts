import { expect, test } from '@playwright/test'
import type { JSONContent } from '@tiptap/core'
import {
  INQTRIX_STRUCTURE_SUGGESTION_ATTR,
  editorYDocToJson,
} from '@inqtrix/editor-schema'
import type * as Y from 'yjs'

import {
  CollaborationStack,
  WRITER_A,
  WRITER_B,
  WRITER_C,
  closeWriters,
  editorText,
  expectSaved,
  liveRuns,
  observedNotices,
  observedStatusKinds,
  openWriter,
  placeCaretAfter,
  placeCaretAtBlockEnd,
  pressKeys,
  proposeInTheMiddle,
  selectWord,
  storedProposals,
  storedRuns,
  typeSlowly,
  watchNotices,
  watchStatusKinds,
  writerDocument,
  type Run,
  type WriterPage,
} from './collaborationStack'

// A writer with edit rights deletes and types in text that holds pending
// suggestions like in any other text. A suggestion keeps the characters
// that are left, under its author; text typed into it is the editor's own;
// a suggestion whose proposed characters are all deleted is removed, and
// the history names who removed whose suggestion. Undo takes back the
// editor's own steps and never brings back a removed suggestion. Normal
// work shows no line, banner or hint to anybody.

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

async function watch(writers: readonly WriterPage[]): Promise<void> {
  for (const writer of writers) {
    await watchStatusKinds(writer)
    await watchNotices(writer)
  }
}

/** The one patch a writer's suggestion rows stored. */
function proposedPatch(stack: CollaborationStack, documentId: string, authorId: string): string {
  const patchIds = new Set(stack.storedRows(documentId).flatMap(({ input }) => (
    input.changeKind === 'suggestion' && input.actorUserId === authorId
      ? input.patches.map((patch) => patch.patchId)
      : []
  )))
  expect(patchIds.size).toBe(1)
  return [...patchIds][0]!
}

/** Selects the last `length` characters of a block that ends in
 * `lastWord`, as a user does with Shift and the arrow keys from the
 * block's end. */
async function selectBlockEnd(writer: WriterPage, block: number, lastWord: string, length: number): Promise<void> {
  await placeCaretAfter(writer, block, lastWord, '')
  await pressKeys(writer, Array.from({ length }, () => 'Shift+ArrowLeft'), 20)
  await expect.poll(() => writer.page.evaluate(() => window.getSelection()?.toString() ?? ''))
    .toHaveLength(length)
}

test("an edit that deletes part of another writer's pending insertion leaves the rest pending under its author, and undo gives the characters back as the editor's own text", async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'edit-part-of-suggestion-doc'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `${SEED}\n\nSecond paragraph` }],
  })
  const writers: WriterPage[] = []
  try {
    const author = await openWriter(browser, stack, WRITER_A, { access: 'suggest', documentId, seedText: SEED })
    writers.push(author)
    const editor = await openWriter(browser, stack, WRITER_B, { documentId, seedText: SEED })
    writers.push(editor)

    await proposeInTheMiddle(author, 'MARKE2 MARKE3')
    await expect.poll(() => editorText(editor)).toContain('Shared paragraph MARKE2 MARKE3 text')
    const proposed = storedProposals(stack, documentId).suggestions
    expect(proposed).toEqual([expect.stringMatching(new RegExp(`^insertion:.+:${WRITER_A.id}$`))])
    await watch(writers)

    expect(await selectWord(editor, 0, 'MARKE3')).toBe('MARKE3')
    await pressKeys(editor, ['Backspace'], 0)

    const shrunk: Run[] = [
      { marks: [], text: 'Shared paragraph' },
      { marks: ['insertion'], text: ' MARKE2 ' },
      { marks: [], text: ' text' },
    ]
    await expectSaved(editor)
    for (const writer of writers) await expect.poll(() => liveRuns(writer)).toEqual(shrunk)
    await expect.poll(() => storedRuns(stack, documentId)).toEqual(shrunk)
    // The editor's direct edit: no patch changed and nothing was removed;
    // what is left is still the author's pending suggestion.
    expect(stack.rejections()).toEqual({})
    expect(stack.storedRows(documentId).at(-1)!.input).toMatchObject({
      changeKind: 'direct',
      patches: [],
      rejectedSuggestionIds: [],
    })
    expect(storedProposals(stack, documentId).suggestions).toEqual(proposed)

    // Undo gives the deleted characters back, as the editor's own text; the
    // rest of the suggestion stays pending.
    await pressKeys(editor, ['ControlOrMeta+z'], 0)
    const restored: Run[] = [
      { marks: [], text: 'Shared paragraph' },
      { marks: ['insertion'], text: ' MARKE2 ' },
      { marks: [], text: 'MARKE3 text' },
    ]
    for (const writer of writers) await expect.poll(() => liveRuns(writer)).toEqual(restored)
    await expect.poll(() => storedRuns(stack, documentId)).toEqual(restored)
    expect(storedProposals(stack, documentId).suggestions).toEqual(proposed)

    await expectNothingShown(writers)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test("text an edit types inside another writer's pending insertion is the editor's own, and a third writer who accepts the insertion applies both of its parts", async ({ browser }) => {
  test.setTimeout(150_000)
  const documentId = 'edit-inside-suggestion-doc'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `${SEED}\n\nSecond paragraph` }],
  })
  const writers: WriterPage[] = []
  try {
    const author = await openWriter(browser, stack, WRITER_A, { access: 'suggest', documentId, seedText: SEED })
    writers.push(author)
    const editor = await openWriter(browser, stack, WRITER_B, { documentId, seedText: SEED })
    writers.push(editor)
    const decider = await openWriter(browser, stack, WRITER_C, { documentId, seedText: SEED })
    writers.push(decider)

    await proposeInTheMiddle(author, 'MARKE4 MARKE5')
    await expect.poll(() => editorText(editor)).toContain('Shared paragraph MARKE4 MARKE5 text')
    const proposed = storedProposals(stack, documentId).suggestions
    expect(proposed).toHaveLength(1)
    await watch(writers)

    await placeCaretAfter(editor, 0, 'MARKE4', ' MARKE5 text')
    await typeSlowly(editor, 'MARKE9', 40)

    const split: Run[] = [
      { marks: [], text: 'Shared paragraph' },
      { marks: ['insertion'], text: ' MARKE4' },
      { marks: [], text: 'MARKE9' },
      { marks: ['insertion'], text: ' MARKE5' },
      { marks: [], text: ' text' },
    ]
    await expectSaved(editor)
    for (const writer of writers) await expect.poll(() => liveRuns(writer)).toEqual(split)
    await expect.poll(() => storedRuns(stack, documentId)).toEqual(split)
    // Both parts are the one suggestion of its author.
    expect(storedProposals(stack, documentId).suggestions).toEqual(proposed)
    expect(stack.rejections()).toEqual({})

    // The third writer accepts the author's patch: both parts become text.
    await stack.decide(documentId, WRITER_C, 'accept', [proposedPatch(stack, documentId, WRITER_A.id)])
    const accepted: Run[] = [{ marks: [], text: 'Shared paragraph MARKE4MARKE9 MARKE5 text' }]
    for (const writer of writers) await expect.poll(() => liveRuns(writer)).toEqual(accepted)
    await expect.poll(() => storedRuns(stack, documentId)).toEqual(accepted)
    expect(storedProposals(stack, documentId).suggestions).toEqual([])
    for (const writer of writers) await expect(writer.editor.locator('ins, del')).toHaveCount(0)

    await expectNothingShown(writers)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

// A known limit, kept red on purpose: Enter inside a suggestion moves the
// part behind the caret into a new block, which the binding writes as new
// characters, and the service keeps a pending suggestion only on characters
// that stay where they were: it removes the suggestion with every character
// it proposed. This test turns into a failure once both parts stay pending.
// Expected to fail, it stays outside the scenario inventory, whose profile
// would otherwise read red on every run.
test.fail("Enter inside another writer's pending insertion keeps both of its parts pending", async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'edit-enter-inside-suggestion-doc'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `${SEED}\n\nSecond paragraph` }],
  })
  const writers: WriterPage[] = []
  try {
    const author = await openWriter(browser, stack, WRITER_A, { access: 'suggest', documentId, seedText: SEED })
    writers.push(author)
    const editor = await openWriter(browser, stack, WRITER_B, { documentId, seedText: SEED })
    writers.push(editor)
    await proposeInTheMiddle(author, 'MARKE4 MARKE5')
    await expect.poll(() => editorText(editor)).toContain('Shared paragraph MARKE4 MARKE5 text')
    const proposed = storedProposals(stack, documentId).suggestions
    await watch(writers)

    await placeCaretAfter(editor, 0, 'MARKE4', ' MARKE5 text')
    await pressKeys(editor, ['Enter'], 0)
    await expectSaved(editor)

    for (const writer of writers) {
      await expect.poll(() => liveRuns(writer, 0)).toEqual([
        { marks: [], text: 'Shared paragraph' },
        { marks: ['insertion'], text: ' MARKE4' },
      ])
      await expect.poll(() => liveRuns(writer, 1)).toEqual([
        { marks: ['insertion'], text: ' MARKE5' },
        { marks: [], text: ' text' },
      ])
    }
    expect(storedProposals(stack, documentId).suggestions).toEqual(proposed)
    await expectNothingShown(writers)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test("an edit that deletes all of another writer's pending insertion removes it, both writers read in the history who removed whose suggestion, and undo keeps the removal and the editor's earlier steps", async ({ browser }, testInfo) => {
  test.setTimeout(150_000)
  const documentId = 'edit-removes-suggestion-doc'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `${SEED}\n\nSecond paragraph` }],
  })
  const writers: WriterPage[] = []
  try {
    const author = await openWriter(browser, stack, WRITER_A, {
      access: 'suggest',
      documentId,
      history: true,
      seedText: SEED,
    })
    writers.push(author)
    const editor = await openWriter(browser, stack, WRITER_B, { documentId, history: true, seedText: SEED })
    writers.push(editor)

    // An earlier step of the editor's own, which undo must still reach.
    await placeCaretAtBlockEnd(editor, 1)
    await typeSlowly(editor, ' MARKE6', 40)
    await expectSaved(editor)
    await placeCaretAtBlockEnd(author, 0)
    await typeSlowly(author, ' MARKE1', 40)
    await expectSaved(author)
    await expect.poll(() => editorText(editor)).toContain(`${SEED} MARKE1`)
    const proposed = storedProposals(stack, documentId).suggestions
    expect(proposed).toHaveLength(1)
    const proposedIds = stack.storedRows(documentId).flatMap(({ input }) => input.suggestionIds)
    expect(new Set(proposedIds).size).toBe(1)
    await watch(writers)

    await selectBlockEnd(editor, 0, 'MARKE1', ' MARKE1'.length)
    await pressKeys(editor, ['Backspace'], 0)
    await expectSaved(editor)

    // The edit went through whole: its row names the suggestion as removed
    // and empties the suggestion's patch.
    for (const writer of writers) {
      await expect.poll(() => editorText(writer)).toBe(`${SEED}Second paragraph MARKE6`)
    }
    expect(stack.rejections()).toEqual({})
    expect(stack.storedRows(documentId).at(-1)!.input).toMatchObject({
      actorKind: 'human',
      changeKind: 'direct',
      patches: [expect.objectContaining({ activeSuggestionIds: [] })],
      rejectedSuggestionIds: [proposedIds[0]],
      suggestionIds: [],
    })
    expect(storedProposals(stack, documentId).suggestions).toEqual([])

    // Both writers read the same quiet entry in the history.
    for (const writer of writers) {
      const history = writer.page.getByTestId('writer-history')
      await expect(history).toContainText('Lin hat einen Vorschlag von Ada entfernt')
      await expect(history).not.toContainText('abgelehnt')
      await history.screenshot({ path: testInfo.outputPath(`history-${writer.writer.key}.png`) })
    }

    // Undo cannot bring the removed suggestion back, and the editor's
    // earlier step is still undoable.
    await pressKeys(editor, ['ControlOrMeta+z'], 0)
    await expectSaved(editor)
    for (const writer of writers) {
      await expect.poll(() => editorText(writer)).toBe(`${SEED}Second paragraph MARKE6`)
    }
    expect(storedProposals(stack, documentId).suggestions).toEqual([])
    await pressKeys(editor, ['ControlOrMeta+z'], 0)
    for (const writer of writers) {
      await expect.poll(() => editorText(writer)).toBe(`${SEED}Second paragraph`)
    }
    await expect.poll(() => stack.storeText(documentId, 'final')).toBe(`${SEED}\n\nSecond paragraph`)
    expect(stack.rejections()).toEqual({})

    await expectNothingShown(writers)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

// A replacement is one proposed deletion directly followed by one proposed
// insertion; text typed inside it leaves a form no pending suggestion may
// take, so the edit removes it as a whole, the way a rejection removes it.
test("an edit that types inside another writer's proposed replacement removes the replacement without a word, both writers read in the history who removed whose suggestion, and undo keeps the removal", async ({ browser }, testInfo) => {
  test.setTimeout(150_000)
  const documentId = 'edit-inside-replacement-doc'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `${SEED}\n\nSecond paragraph` }],
  })
  const writers: WriterPage[] = []
  try {
    const author = await openWriter(browser, stack, WRITER_A, {
      access: 'suggest',
      documentId,
      history: true,
      seedText: SEED,
    })
    writers.push(author)
    const editor = await openWriter(browser, stack, WRITER_B, { documentId, history: true, seedText: SEED })
    writers.push(editor)

    // The author replaces a word: the old word proposed for deletion, the
    // new one proposed for insertion, under one suggestion.
    expect(await selectWord(author, 0, 'paragraph')).toBe('paragraph')
    await typeSlowly(author, 'MARKE7', 40)
    await expectSaved(author)
    await placeCaretAfter(author, 1, 'paragraph', '')
    const replaced: Run[] = [
      { marks: [], text: 'Shared ' },
      { marks: ['deletion'], text: 'paragraph' },
      { marks: ['insertion'], text: 'MARKE7' },
      { marks: [], text: ' text' },
    ]
    await expect.poll(() => liveRuns(editor)).toEqual(replaced)
    const proposed = storedProposals(stack, documentId).suggestions
    expect(proposed).toEqual([
      expect.stringMatching(new RegExp(`^deletion:.+:${WRITER_A.id}$`)),
      expect.stringMatching(new RegExp(`^insertion:.+:${WRITER_A.id}$`)),
    ])
    const proposedId = proposed[0]!.split(':')[1]
    expect(proposed[1]!.split(':')[1]).toBe(proposedId)
    await watch(writers)

    await placeCaretAfter(editor, 0, 'MARKE', '7 text')
    await typeSlowly(editor, 'MARKE8', 40)
    await expectSaved(editor)

    // The proposed word is gone, the old word stays unmarked, and what the
    // editor typed stays.
    const removed: Run[] = [{ marks: [], text: 'Shared paragraphMARKE8 text' }]
    for (const writer of writers) await expect.poll(() => liveRuns(writer)).toEqual(removed)
    await expect.poll(() => storedRuns(stack, documentId)).toEqual(removed)
    expect(storedProposals(stack, documentId).suggestions).toEqual([])
    expect(stack.rejections()).toEqual({})
    const removal = stack.storedRows(documentId).find(({ input }) => input.rejectedSuggestionIds.length > 0)
    expect(removal?.input).toMatchObject({
      actorUserId: WRITER_B.id,
      changeKind: 'direct',
      rejectedSuggestionIds: [proposedId],
      suggestionIds: [],
    })
    for (const writer of writers) {
      const history = writer.page.getByTestId('writer-history')
      await expect(history).toContainText('Lin hat einen Vorschlag von Ada entfernt')
      await history.screenshot({ path: testInfo.outputPath(`history-${writer.writer.key}.png`) })
    }

    // Undo takes back the typing; the replacement stays removed.
    await pressKeys(editor, ['ControlOrMeta+z'], 0)
    await expectSaved(editor)
    const undone: Run[] = [{ marks: [], text: SEED }]
    for (const writer of writers) await expect.poll(() => liveRuns(writer)).toEqual(undone)
    await expect.poll(() => storedRuns(stack, documentId)).toEqual(undone)
    expect(storedProposals(stack, documentId).suggestions).toEqual([])
    expect(stack.rejections()).toEqual({})

    await expectNothingShown(writers)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

/**
 * The editor proposes text at the end of the second block in suggest mode,
 * switches to edit mode and deletes that block's text, own proposal and
 * all, which withdraws the proposal; `then` runs against the result.
 */
async function withdrawOwnSuggestionByEdit(
  browser: Parameters<typeof openWriter>[0],
  documentId: string,
  then: (stack: CollaborationStack, writers: { author: WriterPage; editor: WriterPage }) => Promise<void>,
): Promise<void> {
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `${SEED}\n\nSecond paragraph` }],
  })
  const writers: WriterPage[] = []
  try {
    const author = await openWriter(browser, stack, WRITER_A, { documentId, history: true, seedText: SEED })
    writers.push(author)
    const editor = await openWriter(browser, stack, WRITER_B, { documentId, history: true, seedText: SEED })
    writers.push(editor)

    await editor.page.getByTestId('writer-mode-suggest').click()
    await expect(editor.state).toHaveAttribute('data-write-mode', 'suggest')
    await placeCaretAtBlockEnd(editor, 1)
    await typeSlowly(editor, ' MARKE5', 40)
    await expectSaved(editor)
    const proposed = storedProposals(stack, documentId).suggestions
    expect(proposed).toEqual([expect.stringMatching(new RegExp(`^insertion:.+:${WRITER_B.id}$`))])
    await editor.page.getByTestId('writer-mode-edit').click()
    await expect(editor.state).toHaveAttribute('data-write-mode', 'edit')
    await expect.poll(() => editorText(author)).toContain('Second paragraph MARKE5')
    await watch(writers)

    await selectBlockEnd(editor, 1, 'MARKE5', 'Second paragraph MARKE5'.length)
    await pressKeys(editor, ['Backspace'], 0)
    await expectSaved(editor)

    for (const writer of writers) await expect.poll(() => editorText(writer)).toBe(SEED)
    expect(stack.rejections()).toEqual({})
    expect(stack.storedRows(documentId).at(-1)!.input).toMatchObject({
      changeKind: 'direct',
      rejectedSuggestionIds: [expect.any(String)],
    })
    expect(storedProposals(stack, documentId).suggestions).toEqual([])
    for (const writer of writers) {
      await expect(writer.page.getByTestId('writer-history')).toContainText('Lin hat einen Vorschlag zurückgezogen')
    }

    await then(stack, { author, editor })
    await expectNothingShown(writers)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
}

test("an edit that deletes a sentence holding the editor's own pending insertion withdraws it without a word", async ({ browser }) => {
  test.setTimeout(120_000)
  await withdrawOwnSuggestionByEdit(browser, 'edit-own-suggestion-doc', async () => undefined)
})

// Undo writes the withdrawn proposal back in the editor's own name; the
// room knows it as removed, so it stays removed, while the original text the
// same edit deleted comes back.
test("undo after an edit that withdrew the editor's own pending insertion brings back the deleted text and keeps the withdrawal without a word", async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'edit-own-suggestion-undo-doc'
  await withdrawOwnSuggestionByEdit(browser, documentId, async (stack, { author, editor }) => {
    await pressKeys(editor, ['ControlOrMeta+z'], 0)
    await expectSaved(editor)
    const restored: Run[] = [{ marks: [], text: 'Second paragraph' }]
    for (const writer of [author, editor]) {
      await expect.poll(() => editorText(writer)).toBe(`${SEED}Second paragraph`)
      await expect.poll(() => liveRuns(writer, 1)).toEqual(restored)
    }
    await expect.poll(() => stack.storeText(documentId, 'final')).toBe(`${SEED}\n\nSecond paragraph`)
    expect(storedProposals(stack, documentId).suggestions).toEqual([])
    expect(stack.rejections()).toEqual({})
  })
})

/** Each top-level block of a document: its type, its text and the id and
 * author of the structure suggestion it carries. */
function blocksOf(document: Y.Doc): Array<{ structure: string | null; text: string; type: string }> {
  const textOf = (node: JSONContent): string => node.text ?? (node.content ?? []).map(textOf).join('')
  return (editorYDocToJson(document).content ?? []).map((node) => {
    const data = node.attrs?.[INQTRIX_STRUCTURE_SUGGESTION_ATTR] as { authorId?: string; suggestionId?: string } | null | undefined
    return {
      structure: data ? `${data.suggestionId}:${data.authorId}` : null,
      text: textOf(node),
      type: String(node.type),
    }
  })
}

async function liveBlocks(writer: WriterPage): Promise<ReturnType<typeof blocksOf>> {
  const document = await writerDocument(writer)
  try {
    return blocksOf(document)
  } finally {
    document.destroy()
  }
}

function storedBlocks(stack: CollaborationStack, documentId: string): ReturnType<typeof blocksOf> {
  const document = stack.storeDocument(documentId)
  try {
    return blocksOf(document)
  } finally {
    document.destroy()
  }
}

/** Picks an entry of a block's menu the way a user does: point at the
 * block, click the handle that appears beside it, then the entry (and the
 * entry of its submenu, when there is one). */
async function chooseFromBlockMenu(writer: WriterPage, block: number, ...entries: readonly string[]): Promise<void> {
  await writer.editor.locator(':scope > *').nth(block).hover()
  const handle = writer.page.getByRole('button', { name: 'Block options' })
  await expect(handle).toBeVisible()
  await handle.click()
  for (const entry of entries) await writer.page.getByRole('menuitem', { name: entry, exact: true }).click()
}

test("an edit that moves a block carrying another writer's proposed heading removes the proposal without a word, and both writers read in the history who removed whose suggestion", async ({ browser }, testInfo) => {
  test.setTimeout(150_000)
  const documentId = 'edit-moves-structure-doc'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: 'First MARKE1 block\n\nProposed MARKE2 block\n\nLast MARKE3 block' }],
  })
  const writers: WriterPage[] = []
  try {
    const author = await openWriter(browser, stack, WRITER_A, {
      access: 'suggest',
      documentId,
      history: true,
      seedText: 'First MARKE1 block',
    })
    writers.push(author)
    const editor = await openWriter(browser, stack, WRITER_B, { documentId, history: true, seedText: 'First MARKE1 block' })
    writers.push(editor)

    // The author proposes the second block as a heading.
    await chooseFromBlockMenu(author, 1, 'Turn into', 'Heading 2')
    await expectSaved(author)
    const [proposed] = stack.storedRows(documentId).flatMap(({ input }) => (
      input.changeKind === 'suggestion' ? input.suggestionIds : []
    ))
    expect(proposed).toBeDefined()
    const pending = [
      { structure: null, text: 'First MARKE1 block', type: 'paragraph' },
      { structure: `${proposed}:${WRITER_A.id}`, text: 'Proposed MARKE2 block', type: 'paragraph' },
      { structure: null, text: 'Last MARKE3 block', type: 'paragraph' },
    ]
    expect(storedBlocks(stack, documentId)).toEqual(pending)
    await expect.poll(() => liveBlocks(editor)).toEqual(pending)
    await watch(writers)

    // The editor moves that block up. A block moved is new content, which
    // never carries another person's suggestion: the proposal is removed
    // with the block the move deleted.
    await chooseFromBlockMenu(editor, 1, 'Move up')
    await expectSaved(editor)

    const moved = [
      { structure: null, text: 'Proposed MARKE2 block', type: 'paragraph' },
      { structure: null, text: 'First MARKE1 block', type: 'paragraph' },
      { structure: null, text: 'Last MARKE3 block', type: 'paragraph' },
    ]
    for (const writer of writers) await expect.poll(() => liveBlocks(writer)).toEqual(moved)
    expect(storedBlocks(stack, documentId)).toEqual(moved)
    expect(stack.rejections()).toEqual({})
    expect(stack.storedRows(documentId).at(-1)!.input).toMatchObject({
      actorKind: 'human',
      actorUserId: WRITER_B.id,
      changeKind: 'direct',
      rejectedSuggestionIds: [proposed],
    })
    for (const writer of writers) {
      const history = writer.page.getByTestId('writer-history')
      await expect(history).toContainText('Lin hat einen Vorschlag von Ada entfernt')
      await history.screenshot({ path: testInfo.outputPath(`history-${writer.writer.key}.png`) })
    }

    await expectNothingShown(writers)
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})
