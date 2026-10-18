import { expect, test } from '@playwright/test'

import {
  CollaborationStack,
  WRITER_A,
  WRITER_B,
  WRITER_D,
  closeWriters,
  editorText,
  expectSaved,
  openWriter,
  placeCaretAtBlockEnd,
  type FixtureWriter,
  type WriterPage,
} from './collaborationStack'

// How another writer's caret looks to an observer: the caret and its name
// label follow the typing without vanishing, a resting label costs no work,
// and the label text stays readable on the writer's colour.

/**
 * Starts recording, on the observer's page, the caret of `writer` in every
 * painted frame and every caret element added or removed. A frame is sampled
 * after the page's own animation-frame work, so a caret that is gone for one
 * frame and back in the next is seen.
 */
async function recordCaret(observer: WriterPage, writer: FixtureWriter): Promise<void> {
  await observer.editor.evaluate((root, name) => {
    const carets = () => [...root.querySelectorAll<HTMLElement>('.inqtrix-collaboration-caret')]
      .filter((caret) => caret.querySelector('.inqtrix-collaboration-caret-label')?.textContent === name)
    const record = {
      added: 0,
      elements: [] as HTMLElement[],
      framesWithout: 0,
      frames: 0,
      removed: 0,
      running: true,
      styleWrites: 0,
    }
    const isWritersCaret = (node: Node): boolean => (
      node instanceof HTMLElement
      && [node, ...node.querySelectorAll<HTMLElement>('.inqtrix-collaboration-caret')].some((element) => (
        element.classList.contains('inqtrix-collaboration-caret')
        && element.querySelector('.inqtrix-collaboration-caret-label')?.textContent === name
      ))
    )
    new MutationObserver((mutations) => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) if (isWritersCaret(node)) record.added += 1
        for (const node of mutation.removedNodes) if (isWritersCaret(node)) record.removed += 1
        if (
          mutation.type === 'attributes'
          && mutation.target instanceof HTMLElement
          && mutation.target.closest('.inqtrix-collaboration-caret') !== null
        ) record.styleWrites += 1
      }
    }).observe(root, { attributeFilter: ['style'], attributes: true, childList: true, subtree: true })
    const sample = () => {
      if (!record.running) return
      record.frames += 1
      const current = carets()
      if (current.length === 0) record.framesWithout += 1
      for (const caret of current) {
        if (!record.elements.includes(caret)) record.elements.push(caret)
      }
      requestAnimationFrame(() => setTimeout(sample, 0))
    }
    requestAnimationFrame(() => setTimeout(sample, 0))
    ;(window as unknown as { __caretRecord?: typeof record }).__caretRecord = record
  }, writer.name)
}

type CaretRecord = {
  added: number
  distinct: number
  frames: number
  framesWithout: number
  removed: number
  styleWrites: number
}

async function caretRecord(observer: WriterPage, reset = false): Promise<CaretRecord> {
  const value = await observer.page.evaluate((clear) => {
    const record = (window as unknown as {
      __caretRecord?: {
        added: number
        elements: unknown[]
        frames: number
        framesWithout: number
        removed: number
        styleWrites: number
      }
    }).__caretRecord
    if (!record) return null
    const snapshot = {
      added: record.added,
      distinct: record.elements.length,
      frames: record.frames,
      framesWithout: record.framesWithout,
      removed: record.removed,
      styleWrites: record.styleWrites,
    }
    if (clear) {
      record.added = 0
      record.frames = 0
      record.framesWithout = 0
      record.removed = 0
      record.styleWrites = 0
    }
    return snapshot
  }, reset)
  if (value === null) throw new Error('The caret recorder is not running')
  return value
}

/** Irregular human typing: pauses between 35 and 320 ms, several shorter
 * than the 75 ms in which a presence change may follow the last. */
const TYPING_PAUSES_MS = [
  140, 45, 210, 60, 95, 320, 38, 150, 70, 260,
  52, 180, 40, 110, 300, 65, 125, 35, 240, 80,
  160, 48, 200, 90, 58, 280, 42, 135, 75, 190,
]

async function typeIrregularly(writer: WriterPage, text: string): Promise<void> {
  let index = 0
  for (const character of text) {
    await writer.page.keyboard.type(character)
    await writer.page.waitForTimeout(TYPING_PAUSES_MS[index % TYPING_PAUSES_MS.length]!)
    index += 1
  }
}

test("another writer's caret and its name stay on screen, as one element, in every frame while that writer types at an irregular pace against a slow store", async ({ browser }, testInfo) => {
  test.setTimeout(120_000)
  const documentId = 'caret-presence-doc'
  const seed = 'Caret paragraph'
  // A store answering after 150 ms keeps every update waiting for its
  // acknowledgement while the next keys arrive, the order in which a
  // presence change could overtake the text it points into.
  const stack = await CollaborationStack.start({
    // The writer types in the second paragraph, where the name label above
    // the caret stays inside the editor's frame.
    documents: [{ id: documentId, markdown: `Observer paragraph\n\n${seed}` }],
    persistDelayMs: 150,
  })
  const writers: WriterPage[] = []
  try {
    const observer = await openWriter(browser, stack, WRITER_A, { documentId, seedText: seed })
    writers.push(observer)
    const typist = await openWriter(browser, stack, WRITER_B, { documentId, seedText: seed })
    writers.push(typist)
    await placeCaretAtBlockEnd(observer, 0)
    await placeCaretAtBlockEnd(typist, 1)
    await expect.poll(() => observer.editor.evaluate((root, name) => (
      [...root.querySelectorAll('.inqtrix-collaboration-caret-label')]
        .filter((label) => label.textContent === name).length
    ), WRITER_B.name)).toBe(1)
    await recordCaret(observer, WRITER_B)

    const typed = 'abcdefghijklmnopqrstuvwxyzABCD'
    await typeIrregularly(typist, typed)
    await expectSaved(typist)
    await expect.poll(() => editorText(observer)).toBe(`Observer paragraph${seed}${typed}`)
    await observer.page.waitForTimeout(300)

    const record = await caretRecord(observer)
    testInfo.annotations.push({
      description: `frames ${record.frames}, frames without the caret ${record.framesWithout}, caret elements ${record.distinct}, removed ${record.removed}`,
      type: 'measurement',
    })
    // The recorder saw the caret during the whole typing run.
    expect(record.frames).toBeGreaterThan(60)
    expect(record.framesWithout).toBe(0)
    expect(record.removed).toBe(0)
    expect(record.distinct).toBe(1)
    await observer.page.screenshot({ path: testInfo.outputPath('caret-after-typing.png') })
    expect(stack.rejections()).toEqual({})
    for (const writer of writers) expect(writer.consoleErrors).toEqual([])
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})

test("a resting caret label writes no style, and a writer with a bright colour gets dark label text", async ({ browser }, testInfo) => {
  test.setTimeout(120_000)
  const documentId = 'caret-label-doc'
  const seed = 'Label paragraph'
  const stack = await CollaborationStack.start({
    documents: [{ id: documentId, markdown: `Observer paragraph\n\n${seed}` }],
  })
  const writers: WriterPage[] = []
  try {
    const observer = await openWriter(browser, stack, WRITER_A, { documentId, seedText: seed })
    writers.push(observer)
    const bright = await openWriter(browser, stack, WRITER_D, { documentId, seedText: seed })
    writers.push(bright)
    await placeCaretAtBlockEnd(observer, 0)
    await placeCaretAtBlockEnd(bright, 1)
    await bright.page.keyboard.type('MARKE1')
    await expectSaved(bright)
    await expect.poll(() => editorText(observer)).toBe(`Observer paragraph${seed}MARKE1`)
    const label = observer.editor.locator('.inqtrix-collaboration-caret-label', { hasText: WRITER_D.name })
    await expect(label).toHaveCount(1)
    await observer.page.waitForTimeout(500)

    await recordCaret(observer, WRITER_D)
    // Sixty painted frames with nobody typing.
    await observer.page.evaluate(() => new Promise<void>((resolve) => {
      let frames = 0
      const next = () => {
        frames += 1
        if (frames >= 60) resolve()
        else requestAnimationFrame(next)
      }
      requestAnimationFrame(next)
    }))
    const resting = await caretRecord(observer)
    expect(resting.frames).toBeGreaterThan(30)
    expect(resting.framesWithout).toBe(0)
    expect(resting.styleWrites).toBe(0)

    // The label text is readable on the bright colour after every layout
    // the page ran, not only as first rendered.
    await expect(label).toHaveCSS('background-color', 'rgb(250, 204, 21)')
    await expect(label).toHaveCSS('color', 'rgb(17, 24, 39)')
    await label.screenshot({ path: testInfo.outputPath('bright-caret-label.png') })
    for (const writer of writers) expect(writer.consoleErrors).toEqual([])
  } finally {
    await closeWriters(writers)
    await stack.stop()
  }
})
