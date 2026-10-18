import { expect, test } from '@playwright/test'

import {
  CollaborationStack,
  WRITER_A,
  WRITER_B,
  closeWriters,
  expectSaved,
  openWriter,
  placeCaretAfter,
  typeSlowly,
  type WriterPage,
} from './collaborationStack'

// A GFM table cell is one line, so the editor writes a line break inside a
// cell as `<br>`. The collaboration service reads stored Markdown without a
// DOM when it activates a document; every writer sees the line break, never
// the text `<br>`, and an edit stores the cell the way the editor wrote it.

const INTRO = 'Intro MARKE0 text'

/** The body cells of the table a writer's editor shows: each cell's text
 * without other writers' caret labels, and the line breaks it shows. */
async function bodyCells(writer: WriterPage): Promise<Array<{ breaks: number; text: string }>> {
  return writer.editor.locator('td').evaluateAll((cells) => cells.map((cell) => {
    const clone = cell.cloneNode(true) as HTMLElement
    for (const widget of clone.querySelectorAll('[class*="collaboration-caret"]')) widget.remove()
    return {
      breaks: clone.querySelectorAll('br:not(.ProseMirror-trailingBreak)').length,
      text: clone.textContent ?? '',
    }
  }))
}

test('a line break inside a table cell reads as a line break for every writer of an activated document, and the store keeps it after an edit', async ({ browser }) => {
  test.setTimeout(120_000)
  const documentId = 'table-cell-line-break-doc'
  const stack = await CollaborationStack.start({
    documents: [{
      id: documentId,
      markdown: `${INTRO}\n\n| Quelle | Wert |\n| --- | --- |\n| Bericht | first MARKE1<br>second MARKE2 |`,
    }],
  })
  const writers: WriterPage[] = []
  try {
    const reader = await openWriter(browser, stack, WRITER_A, { documentId, seedText: INTRO })
    writers.push(reader)
    const editor = await openWriter(browser, stack, WRITER_B, { documentId, seedText: INTRO })
    writers.push(editor)
    for (const writer of writers) {
      expect(await bodyCells(writer), writer.writer.name).toEqual([
        { breaks: 0, text: 'Bericht' },
        { breaks: 1, text: 'first MARKE1second MARKE2' },
      ])
    }

    // A browser's edit reaches the store with the document around it, and
    // the stored Markdown keeps the cell as the editor writes it.
    const rowsBefore = stack.storedRows(documentId).length
    await placeCaretAfter(editor, 0, 'text', '')
    await typeSlowly(editor, ' MARKE3', 40)
    await expectSaved(editor)

    expect(stack.storedRows(documentId).length).toBeGreaterThan(rowsBefore)
    for (const writer of writers) {
      await expect(writer.editor).toContainText(`${INTRO} MARKE3`)
      expect(await bodyCells(writer), writer.writer.name).toEqual([
        { breaks: 0, text: 'Bericht' },
        { breaks: 1, text: 'first MARKE1second MARKE2' },
      ])
    }
    expect(stack.storeText(documentId, 'final').split('\n').filter((line) => line !== '')).toEqual([
      `${INTRO} MARKE3`,
      '| Quelle  | Wert                          |',
      '| ------- | ----------------------------- |',
      '| Bericht | first MARKE1<br>second MARKE2 |',
    ])
    for (const writer of writers) expect(writer.consoleErrors, writer.writer.name).toEqual([])
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})
