import { expect, test } from '@playwright/test'

import {
  CollaborationStack,
  WRITER_A,
  WRITER_B,
  closeWriters,
  editorText,
  expectSaved,
  expectWriterReady,
  openWriter,
  placeCaretAtBlockEnd,
  pressKeys,
  typeSlowly,
  writerText,
  type WriterPage,
} from './collaborationStack'

test('a change the service rejects keeps the local state as a read-only backup until the writer reloads the document, and the reload starts over from the service', async ({ browser }, testInfo) => {
  test.setTimeout(120_000)
  const documentId = 'rejected-change-doc'
  const seed = 'Shared paragraph'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `${seed}\n\nSecond paragraph` }],
  })
  const writers: WriterPage[] = []
  try {
    const suggester = await openWriter(browser, stack, WRITER_A, {
      access: 'suggest',
      documentId,
      seedText: seed,
    })
    writers.push(suggester)
    const editor = await openWriter(browser, stack, WRITER_B, {
      documentId,
      seedText: seed,
    })
    writers.push(editor)
    await expect(suggester.state).toHaveAttribute('data-write-mode', 'suggest')

    // A suggest lease may not change text directly. The editor's own rules
    // never produce such a change, so it is written into the live document
    // past them, as a faulty or outdated client would.
    await suggester.page.evaluate(() => {
      const append = (window as unknown as {
        __writerAppendPlainText?: (text: string) => void
      }).__writerAppendPlainText
      if (!append) throw new Error('The writer fixture offers no direct write')
      append('MARKE1')
    })

    await expect(suggester.state).toHaveAttribute('data-update-rejected', 'true', { timeout: 15_000 })
    expect(stack.rejections()).toEqual({ 'update:suggestion_policy_violation': 1 })
    await expect(suggester.state).toHaveAttribute('data-recoverability', 'reload')
    await expect(suggester.state).toHaveAttribute('data-can-edit', 'false')
    await expect(suggester.editor).toHaveAttribute('contenteditable', 'false')
    // The refused state stays readable as the backup, and the bar says so.
    await expect.poll(() => editorText(suggester)).toContain(`${seed}MARKE1`)
    const bar = suggester.page.locator('[data-collaboration-update-rejected]')
    await expect(bar).toHaveAttribute('role', 'alert')
    await expect(bar).toContainText('Ihr lokaler Stand bleibt als Sicherung erhalten, bis Sie das Dokument neu laden.')
    expect(stack.storeText(documentId, 'final')).not.toContain('MARKE1')
    await suggester.page.screenshot({ path: testInfo.outputPath('rejected-change-backup.png') })

    // The other writer's session is not affected: it keeps writing.
    await placeCaretAtBlockEnd(editor, 1)
    await typeSlowly(editor, ' MARKE2', 60)
    await expectSaved(editor)
    expect(stack.storeText(documentId, 'final')).toBe(`${seed}\n\nSecond paragraph MARKE2`)
    const refusedSockets = suggester.transports.length

    await bar.getByRole('button', { name: 'Neu laden' }).click()

    // A new session on a new socket, bound to the service's state: the
    // refused text is gone, the other writer's text is there, and the
    // writer can suggest again.
    await expectWriterReady(suggester, seed)
    await expect.poll(() => editorText(suggester)).toBe(`${seed}Second paragraph MARKE2`)
    await expect(suggester.state).toHaveAttribute('data-update-rejected', 'false')
    await expect(bar).toHaveCount(0)
    expect(suggester.transports.length).toBe(refusedSockets + 1)
    expect(await editorText(suggester)).not.toContain('MARKE1')
    expect(await writerText(suggester, 'final')).toBe(`${seed}\n\nSecond paragraph MARKE2`)
    await suggester.page.screenshot({ path: testInfo.outputPath('rejected-change-reloaded.png') })

    await placeCaretAtBlockEnd(suggester, 0)
    await typeSlowly(suggester, 'MARKE3', 60)
    await expectSaved(suggester)
    expect(stack.rejections()).toEqual({ 'update:suggestion_policy_violation': 1 })
    expect(stack.storeText(documentId, 'final')).toBe(`${seed}MARKE3\n\nSecond paragraph MARKE2`)
    expect(stack.storeText(documentId, 'original')).toBe(`${seed}\n\nSecond paragraph MARKE2`)
    for (const writer of writers) expect(writer.consoleErrors).toEqual([])
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

/** The updates a writer's page sent so far, over all its sockets. */
function sentUpdates(writer: WriterPage): number {
  return writer.transports.reduce((count, transport) => count + transport.updates().length, 0)
}

/** A refused local change: the refusal line says why, nothing went out
 * within several send windows, and the session stays writable. */
async function expectRefusedLocally(
  writer: WriterPage,
  sentBefore: number,
  reason: string,
): Promise<void> {
  const line = writer.page.locator('[data-collaboration-edit-refusal]')
  await expect(line).toHaveText(reason)
  await expect(line).toHaveAttribute('role', 'status')
  await writer.page.waitForTimeout(500)
  expect(sentUpdates(writer)).toBe(sentBefore)
  await expect(writer.state).toHaveAttribute('data-connection-status', 'connected')
  await expect(writer.state).toHaveAttribute('data-can-edit', 'true')
  await expect(writer.state).toHaveAttribute('data-has-unconfirmed', 'false')
  await expect(writer.editor).toHaveAttribute('contenteditable', 'true')
}

test('a suggest-mode Enter in the middle of a paragraph is refused in the editor with a German reason, sends nothing and leaves the session writable', async ({ browser }, testInfo) => {
  test.setTimeout(90_000)
  const documentId = 'refused-split-doc'
  const seed = 'Shared paragraph'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `${seed}\n\nSecond paragraph` }],
  })
  const writers: WriterPage[] = []
  try {
    const suggester = await openWriter(browser, stack, WRITER_A, {
      access: 'suggest',
      documentId,
      seedText: seed,
    })
    writers.push(suggester)
    await expect(suggester.state).toHaveAttribute('data-write-mode', 'suggest')

    await placeCaretAtBlockEnd(suggester, 0)
    const sentBefore = sentUpdates(suggester)
    await pressKeys(suggester, ['ArrowLeft', 'ArrowLeft', 'ArrowLeft', 'Enter'], 60)

    await expectRefusedLocally(
      suggester,
      sentBefore,
      'Im Modus „Vorschlagen“ kann ein neuer Absatz nur am Ende eines Absatzes beginnen. Setzen Sie den Cursor ans Absatzende.',
    )
    expect(await editorText(suggester)).toBe(`${seed}Second paragraph`)
    await suggester.page.screenshot({ path: testInfo.outputPath('refused-split.png') })

    // A change that goes through clears the reason and is stored.
    await placeCaretAtBlockEnd(suggester, 0)
    await typeSlowly(suggester, 'MARKE1', 60)
    await expect(suggester.page.locator('[data-collaboration-edit-refusal]')).toHaveCount(0)
    await expectSaved(suggester)
    expect(sentUpdates(suggester)).toBeGreaterThan(sentBefore)
    expect(stack.rejections()).toEqual({})
    expect(stack.storeText(documentId, 'final')).toBe(`${seed}MARKE1\n\nSecond paragraph`)
    expect(suggester.consoleErrors).toEqual([])
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test("typing in suggest mode directly behind another person's pending suggestion is refused in the editor with a German reason and leaves the session writable", async ({ browser }) => {
  test.setTimeout(90_000)
  const documentId = 'refused-adjacent-doc'
  const seed = 'Shared paragraph'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `${seed}\n\nSecond paragraph` }],
  })
  const writers: WriterPage[] = []
  try {
    const other = await openWriter(browser, stack, WRITER_B, {
      access: 'suggest',
      documentId,
      seedText: seed,
    })
    writers.push(other)
    const suggester = await openWriter(browser, stack, WRITER_A, {
      access: 'suggest',
      documentId,
      seedText: seed,
    })
    writers.push(suggester)

    await placeCaretAtBlockEnd(other, 0)
    await typeSlowly(other, 'MARKE2', 60)
    await expectSaved(other)
    await expect.poll(() => editorText(suggester)).toBe(`${seed}MARKE2Second paragraph`)

    await placeCaretAtBlockEnd(suggester, 0)
    const sentBefore = sentUpdates(suggester)
    await pressKeys(suggester, ['X'], 60)

    await expectRefusedLocally(
      suggester,
      sentBefore,
      'Diese Änderung würde einen offenen Vorschlag einer anderen Person verändern. Ändern Sie an anderer Stelle oder warten Sie, bis über den Vorschlag entschieden ist.',
    )
    expect(await editorText(suggester)).toBe(`${seed}MARKE2Second paragraph`)

    await placeCaretAtBlockEnd(suggester, 1)
    await typeSlowly(suggester, 'MARKE3', 60)
    await expect(suggester.page.locator('[data-collaboration-edit-refusal]')).toHaveCount(0)
    await expectSaved(suggester)
    expect(stack.rejections()).toEqual({})
    expect(stack.storeText(documentId, 'final')).toBe(`${seed}MARKE2\n\nSecond paragraphMARKE3`)
    for (const writer of writers) expect(writer.consoleErrors).toEqual([])
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})
