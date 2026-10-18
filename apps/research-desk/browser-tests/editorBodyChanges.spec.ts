import { expect, test, type Page } from '@playwright/test'
import type { Editor } from '@tiptap/core'

import { CANVAS_SAVE_DEBOUNCE_MS } from '../src/features/agent/canvas/saveGate'

const fixturePath = '/browser-tests/fixtures/editor-body-changes.html'

/** Every kind of last block the editor normalizes with a trailing
 * paragraph, and the paragraph that needs none. */
const BODY_KINDS = [
  'blockMath',
  'blockquote',
  'bulletList',
  'codeBlock',
  'heading',
  'horizontalRule',
  'orderedList',
  'table',
  'taskList',
  'paragraph',
] as const

// Longer than every frame, timer and effect the editor schedules after it
// mounted or after a click; a body the surface hands its host by then is
// handed over for good.
const SETTLE_MS = 400
// Past the canvas's save debounce: a queued save has gone out by then.
const CANVAS_SAVE_WINDOW_MS = CANVAS_SAVE_DEBOUNCE_MS + SETTLE_MS
// One page load with its two settle waits and a click, with ample margin.
const OPEN_BUDGET_MS = 5_000

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => {
    throw error
  })
})

async function openSurface(page: Page, query: string) {
  await page.goto(`${fixturePath}?${query}`)
  await expect(page.getByTestId('body-change-surface')).toHaveAttribute('data-ready', 'true')
  await page.waitForTimeout(SETTLE_MS)
}

function bodyChanges(page: Page): Promise<string[]> {
  return page.evaluate(() => window.__bodyChanges ?? [])
}

function lastBlock(page: Page): Promise<string | null> {
  return page.evaluate(() => window.__bodyEditor?.lastBlock() ?? null)
}

function storedBody(page: Page): Promise<string> {
  return page.evaluate(() => window.__bodyEditor!.storedBody)
}

function editorTextbox(page: Page) {
  return page.getByRole('textbox', { name: /^(Document content|Dokumentinhalt)$/ })
}

/** A click into the first paragraph, then the caret moved to its end. */
async function clickToFirstParagraphEnd(page: Page) {
  await editorTextbox(page).locator('p').first().click()
  await page.keyboard.press('End')
}

/** The same, once the surface editor's own selection is there too: a
 * keystroke before that lands at the DOM caret while undo restores the
 * editor's older selection. */
async function placeCaretAfterFirstParagraph(page: Page) {
  await clickToFirstParagraphEnd(page)
  await expect.poll(() => page.evaluate(() => window.__bodyEditor!.caretAtFirstBlockEnd()))
    .toBe(true)
}

test('opening, focusing and clicking the live editor never hands its host a body', async ({ page }) => {
  test.setTimeout(BODY_KINDS.length * 2 * OPEN_BUDGET_MS)
  for (const kind of BODY_KINDS) {
    for (const form of ['stored', 'fixpoint'] as const) {
      const label = `${kind} (${form})`
      await openSurface(page, `body=${kind}&form=${form}`)
      // The editor did normalize the document: a trailing paragraph after
      // every other kind of last block. The absence of a body is therefore
      // not the absence of a document change.
      expect(await lastBlock(page), label).toBe('paragraph')
      expect(await bodyChanges(page), `${label} after mount`).toEqual([])

      await placeCaretAfterFirstParagraph(page)
      await page.waitForTimeout(SETTLE_MS)
      expect(await bodyChanges(page), `${label} after the first click`).toEqual([])
    }
  }
})

test('a trailing paragraph appended after mount is no content change', async ({ page }) => {
  // The editor appends the paragraph to the first transaction that finds it
  // missing. While a document opens, that is a step-less transaction the
  // surface dispatches itself, so the document is complete before the user
  // can act. Here the paragraph is taken away once the surface holds its
  // baseline, and the same kind of transaction brings it back.
  await openSurface(page, 'body=table')
  expect(await page.evaluate(() => window.__bodyEditor!.removeTrailingParagraph())).toBe('table')
  expect(await page.evaluate(() => window.__bodyEditor!.tellEditability())).toBe('paragraph')
  await page.waitForTimeout(SETTLE_MS)
  expect(await bodyChanges(page)).toEqual([])

  await placeCaretAfterFirstParagraph(page)
  await page.waitForTimeout(SETTLE_MS)

  expect(await lastBlock(page)).toBe('paragraph')
  expect(await bodyChanges(page)).toEqual([])
})

test('typing, pasting and composing text each hand the host the new body', async ({ page }) => {
  await openSurface(page, 'body=table')
  await placeCaretAfterFirstParagraph(page)

  await page.keyboard.type(' getippt')
  await expect.poll(async () => (await bodyChanges(page)).at(-1) ?? '').toContain('getippt')

  // The view's own paste entry point runs the paste handling a paste event
  // does (the surface's paste rule included); a constructed paste event
  // carries no clipboard data in every engine.
  expect(await page.evaluate(() => window.__bodyEditor!.pasteText(' eingefuegt'))).toBe(true)
  await expect.poll(async () => (await bodyChanges(page)).at(-1) ?? '').toContain('getippt eingefuegt')

  // A composition session the way an input method drives it: preedit text
  // written into the DOM between composition events, replaced, committed.
  await editorTextbox(page).evaluate(async (target) => {
    const selection = window.getSelection()!
    const text = selection.focusNode as Text
    const offset = selection.focusOffset
    if (text.nodeType !== Node.TEXT_NODE) throw new Error('The caret is not in a text node.')
    const compose = (data: string) => {
      target.dispatchEvent(new CompositionEvent('compositionupdate', { bubbles: true, data }))
      target.dispatchEvent(new InputEvent('input', {
        bubbles: true,
        data,
        inputType: 'insertCompositionText',
        isComposing: true,
      }))
    }
    target.dispatchEvent(new CompositionEvent('compositionstart', { bubbles: true, data: '' }))
    text.insertData(offset, ' u')
    compose(' u')
    await new Promise((resolve) => window.setTimeout(resolve, 50))
    text.replaceData(offset, 2, ' ü')
    compose(' ü')
    target.dispatchEvent(new CompositionEvent('compositionend', { bubbles: true, data: ' ü' }))
    selection.collapse(text, offset + 2)
  })
  await expect.poll(async () => (await bodyChanges(page)).at(-1) ?? '').toContain('eingefuegt ü')
})

test('undo back to the loaded text hands the host its stored bytes and keeps the caret', async ({
  page,
}) => {
  // The host takes every body as its own, as the editor desk does: the
  // stored bytes coming back must not make the surface reload them.
  await openSurface(page, 'body=table&echo=host')
  const stored = await storedBody(page)
  await placeCaretAfterFirstParagraph(page)

  await page.keyboard.type('x')
  await expect.poll(async () => (await bodyChanges(page)).at(-1) ?? '').toContain('[K2, W7].x')

  await page.keyboard.press('ControlOrMeta+z')
  await expect.poll(async () => (await bodyChanges(page)).at(-1)).toBe(stored)
  await page.waitForTimeout(SETTLE_MS)

  await page.keyboard.type('y')
  await expect(editorTextbox(page).locator('p').first())
    .toHaveText('Einleitung mit Beleg [K1] und Gruppe [K2, W7].y')
  await expect.poll(async () => (await bodyChanges(page)).at(-1) ?? '').toContain('[K2, W7].y')
})

test('text typed right after opening is undone on its own and keeps the caret', async ({ page }) => {
  // The surface creates the editor from the stored body and takes those
  // bytes as its own from the start. Loading them a second time would put
  // the load into the undo history: the first typed text would join it, and
  // one Ctrl+Z would take both back and the caret with them to the start.
  // A paused clock gives every step the same time, so the history joins any
  // change that touches the one before.
  await page.clock.install()
  await page.goto(`${fixturePath}?body=table&echo=host`)
  await expect(page.getByTestId('body-change-surface')).toHaveAttribute('data-ready', 'true')
  await page.clock.pauseAt(await page.evaluate(() => Date.now() + 50))
  await placeCaretAfterFirstParagraph(page)

  await page.keyboard.type('x')
  await page.keyboard.press('ControlOrMeta+z')

  await expect(editorTextbox(page).locator('p').first())
    .toHaveText('Einleitung mit Beleg [K1] und Gruppe [K2, W7].')
  expect(await page.evaluate(() => window.__bodyEditor!.caretAtFirstBlockEnd())).toBe(true)
})

function canvasSaves(page: Page): Promise<{ expectedRevision: number; markdown: string }[]> {
  return page.evaluate(() => window.__canvasSaves ?? [])
}

async function renderedCitations(page: Page): Promise<string[]> {
  const hrefs = await page.locator('a[href^="#kref-"]').evaluateAll(
    (anchors) => anchors.map((anchor) => anchor.getAttribute('href') ?? ''),
  )
  return [...new Set(hrefs)].sort()
}

async function openCanvasEditor(page: Page, body: string, answer: 'held' | 'prompt' = 'prompt') {
  await page.goto(`${fixturePath}?scenario=canvas&body=${body}&answer=${answer}`)
  await expect(page.getByRole('button', { name: /^(Memo bearbeiten|Edit memo)$/ })).toBeVisible()
}

function firstParagraph(page: Page) {
  return editorTextbox(page).locator('p').first()
}

/** The canvas's caret placed after the first paragraph, once the editor's
 * own selection is there too (Tiptap keeps its editor on the editable
 * element): see placeCaretAfterFirstParagraph. */
async function placeCanvasCaretAfterFirstParagraph(page: Page) {
  await clickToFirstParagraphEnd(page)
  await expect.poll(() => editorTextbox(page).evaluate((element) => {
    const { doc, selection } = (element as HTMLElement & { editor: Editor }).editor.state
    return selection.empty && selection.from === 1 + (doc.firstChild?.content.size ?? 0)
  })).toBe(true)
}

/** Answer every save the canvas sent once one holds *text*. */
async function answerSavesOnceSent(page: Page, text: string) {
  await expect.poll(async () => (await canvasSaves(page)).some((save) => save.markdown.includes(text)), {
    timeout: 5_000,
  }).toBe(true)
  await page.evaluate(() => window.__answerCanvasSaves!())
}

test('the canvas saves a real edit once, never on open or undo, and keeps its citations', async ({
  page,
}) => {
  await openCanvasEditor(page, 'table')
  const citedBefore = await renderedCitations(page)
  expect(citedBefore).toEqual(['#kref-K1', '#kref-K2', '#kref-W7'])

  await page.getByRole('button', { name: /^(Memo bearbeiten|Edit memo)$/ }).click()
  await expect(editorTextbox(page)).toBeVisible()
  await page.waitForTimeout(CANVAS_SAVE_WINDOW_MS)
  expect(await canvasSaves(page), 'after opening').toEqual([])

  await clickToFirstParagraphEnd(page)
  await page.waitForTimeout(CANVAS_SAVE_WINDOW_MS)
  expect(await canvasSaves(page), 'after the first click').toEqual([])

  // An edit undone inside the debounce window leaves nothing to save.
  await page.keyboard.type('x')
  await page.keyboard.press('ControlOrMeta+z')
  await page.waitForTimeout(CANVAS_SAVE_WINDOW_MS)
  expect(await canvasSaves(page), 'after an undone edit').toEqual([])

  await page.keyboard.type(' Ergaenzt.')
  await expect.poll(async () => (await canvasSaves(page)).length, {
    timeout: 5_000,
  }).toBe(1)
  const [saved] = await canvasSaves(page)
  expect(saved.expectedRevision).toBe(1)
  expect(saved.markdown).toContain('Einleitung mit Beleg [K1] und Gruppe [K2, W7]. Ergaenzt.')
  expect(saved.markdown).not.toContain('\\[')

  await page.getByRole('button', { name: /^(Zur Leseansicht|Return to reading view)$/ }).click()
  await expect(page.getByText('Ergaenzt.', { exact: false })).toBeVisible()
  expect(await renderedCitations(page)).toEqual(citedBefore)
  expect(await canvasSaves(page)).toHaveLength(1)
})

test('a canvas edit before every kind of last block saves that edit and nothing else', async ({ page }) => {
  test.setTimeout(BODY_KINDS.length * (OPEN_BUDGET_MS + CANVAS_SAVE_WINDOW_MS))
  for (const kind of BODY_KINDS) {
    // The body is the editor's own fixpoint, so the only change a save may
    // carry is the typed text: not the paragraph the editor appends after a
    // last block of another kind.
    await page.goto(`${fixturePath}?scenario=canvas&body=${kind}&form=fixpoint`)
    await page.getByRole('button', { name: /^(Memo bearbeiten|Edit memo)$/ }).click()
    await expect(editorTextbox(page), kind).toBeVisible()
    await placeCanvasCaretAfterFirstParagraph(page)
    await page.keyboard.type(' Ergaenzt.')
    await expect.poll(async () => (await canvasSaves(page)).length, { message: kind, timeout: 5_000 }).toBe(1)

    const stored = await page.evaluate(() => window.__canvasStoredBody!)
    const firstBreak = stored.indexOf('\n\n')
    const [saved] = await canvasSaves(page)
    expect(saved.markdown, kind).toBe(`${stored.slice(0, firstBreak)} Ergaenzt.${stored.slice(firstBreak)}`)
  }
})

test('editing a canvas shows a chip for every citation the reading view links, and a chip opens its evidence', async ({ page }) => {
  await openCanvasEditor(page, 'table')
  const linked = await page.locator('a[href^="#kref-"]').evaluateAll(
    (anchors) => anchors.map((anchor) => (anchor.getAttribute('href') ?? '').slice('#kref-'.length)),
  )
  expect(linked).toEqual(['K1', 'K2', 'W7', 'K1', 'W7'])

  await page.getByRole('button', { name: /^(Memo bearbeiten|Edit memo)$/ }).click()
  await expect(editorTextbox(page)).toBeVisible()
  const chips = editorTextbox(page).locator('[data-citation-label]')
  await expect(chips).toHaveCount(linked.length)
  expect(await chips.evaluateAll(
    (elements) => elements.map((element) => element.getAttribute('data-citation-label')),
  )).toEqual(linked)

  await chips.nth(2).click()
  expect(await page.evaluate(() => window.__canvasViews ?? [])).toEqual([
    { artifactId: 'fixture-memo', label: 'W7', runId: 'fixture-run', view: 'evidence' },
  ])
  await page.waitForTimeout(CANVAS_SAVE_WINDOW_MS)
  expect(await canvasSaves(page), 'opening a source is no edit').toEqual([])
})

/** The caret at *offset* within the first `[K1]` of the canvas editor, once
 * the editor holds the focus (Tiptap focuses on the next frame) and it. */
async function placeCaretInFirstK1(page: Page, offset: number) {
  const caret = await editorTextbox(page).evaluate((element, within) => {
    const { editor } = element as HTMLElement & { editor: Editor }
    let position = -1
    editor.state.doc.descendants((node, at) => {
      const index = node.isText ? (node.text ?? '').indexOf('[K1]') : -1
      if (position < 0 && index >= 0) position = at + index + within
    })
    editor.chain().focus().setTextSelection(position).run()
    return position
  }, offset)
  await expect.poll(() => editorTextbox(page).evaluate((element, expected) => {
    const { editor } = element as HTMLElement & { editor: Editor }
    return element.contains(document.activeElement) && editor.state.selection.from === expected
  }, caret)).toBe(true)
}

test('Enter inside a chip opens its evidence, and the caret moves through a chip as through text', async ({ page }) => {
  await openCanvasEditor(page, 'table')
  await page.getByRole('button', { name: /^(Memo bearbeiten|Edit memo)$/ }).click()
  await expect(editorTextbox(page).locator('[data-citation-label]').first()).toBeVisible()

  // At the chip's end (after "K1", before "]") Enter is Enter.
  await placeCaretInFirstK1(page, 3)
  await page.keyboard.press('Enter')
  expect(await editorTextbox(page).locator('p').count()).toBeGreaterThan(1)
  await page.keyboard.press('ControlOrMeta+z')
  expect(await page.evaluate(() => window.__canvasViews ?? [])).toEqual([])

  // Between "K" and "1" it opens the chip's evidence.
  await placeCaretInFirstK1(page, 2)
  await page.keyboard.press('Enter')
  expect(await page.evaluate(() => window.__canvasViews ?? [])).toEqual([
    { artifactId: 'fixture-memo', label: 'K1', runId: 'fixture-run', view: 'evidence' },
  ])

  await page.keyboard.press('ArrowRight')
  await page.keyboard.press('ArrowRight')
  await page.keyboard.type('x')
  await expect.poll(async () => (await canvasSaves(page)).length, { timeout: 5_000 }).toBe(1)
  const [saved] = await canvasSaves(page)
  expect(saved.markdown).toContain('Einleitung mit Beleg [K1]x und Gruppe [K2, W7].')
})

test('a canvas stored with escaped labels heals on the next edit, never on open', async ({
  page,
}) => {
  await openCanvasEditor(page, 'escapedTable')
  // Escaped labels are literal text to every reader.
  expect(await renderedCitations(page)).toEqual([])

  await page.getByRole('button', { name: /^(Memo bearbeiten|Edit memo)$/ }).click()
  await expect(editorTextbox(page)).toBeVisible()
  await clickToFirstParagraphEnd(page)
  await page.waitForTimeout(CANVAS_SAVE_WINDOW_MS)
  expect(await canvasSaves(page), 'after opening and clicking').toEqual([])

  await page.keyboard.type(' Ergaenzt.')
  await expect.poll(async () => (await canvasSaves(page)).length, { timeout: 5_000 }).toBe(1)
  const [saved] = await canvasSaves(page)
  expect(saved.markdown).toContain('Einleitung mit Beleg [K1] und Gruppe [K2, W7]. Ergaenzt.')

  await page.getByRole('button', { name: /^(Zur Leseansicht|Return to reading view)$/ }).click()
  await expect(page.getByText('Ergaenzt.', { exact: false })).toBeVisible()
  expect(await renderedCitations(page)).toEqual(['#kref-K1', '#kref-K2', '#kref-W7'])
})

test('a save answered after further typing keeps the newer text and the caret', async ({ page }) => {
  // The server's answer reloads the saved body into the canvas; typed on
  // since, the editor holds newer text that the answer must not replace.
  await openCanvasEditor(page, 'paragraph', 'held')
  await page.getByRole('button', { name: /^(Memo bearbeiten|Edit memo)$/ }).click()
  await placeCanvasCaretAfterFirstParagraph(page)

  await page.keyboard.type(' eins')
  await expect.poll(async () => (await canvasSaves(page)).length, { timeout: 5_000 }).toBe(1)
  await page.keyboard.type(' zwei')
  await page.evaluate(() => window.__answerCanvasSaves!())
  await page.waitForTimeout(SETTLE_MS)
  await expect(firstParagraph(page)).toHaveText('Einleitung mit Beleg [K1]. eins zwei')

  // The caret stayed where the user typed.
  await page.keyboard.type(' drei')
  await expect(firstParagraph(page)).toHaveText('Einleitung mit Beleg [K1]. eins zwei drei')
  await expect(editorTextbox(page).locator('p').nth(1)).toHaveText('Schluss mit Gruppe [K2, W7].')
  await answerSavesOnceSent(page, 'eins zwei drei')
  await page.waitForTimeout(CANVAS_SAVE_WINDOW_MS)
  expect((await canvasSaves(page)).at(-1)?.markdown).toBe(
    'Einleitung mit Beleg [K1]. eins zwei drei\n\nSchluss mit Gruppe [K2, W7].',
  )
})

test('a save answered after an undo keeps the loaded text', async ({ page }) => {
  await openCanvasEditor(page, 'paragraph', 'held')
  await page.getByRole('button', { name: /^(Memo bearbeiten|Edit memo)$/ }).click()
  await placeCanvasCaretAfterFirstParagraph(page)

  await page.keyboard.type('x')
  await expect.poll(async () => (await canvasSaves(page)).length, { timeout: 5_000 }).toBe(1)
  await page.keyboard.press('ControlOrMeta+z')
  await expect(firstParagraph(page)).toHaveText('Einleitung mit Beleg [K1].')
  // The answer reloads the body with the `x` the user took back.
  await page.evaluate(() => window.__answerCanvasSaves!())
  await page.waitForTimeout(SETTLE_MS)
  await expect(firstParagraph(page)).toHaveText('Einleitung mit Beleg [K1].')

  // The loaded text is saved back, and the caret stayed where it was.
  await answerSavesOnceSent(page, 'Einleitung mit Beleg [K1].\n\n')
  await page.waitForTimeout(CANVAS_SAVE_WINDOW_MS)
  expect((await canvasSaves(page)).map((save) => save.markdown)).toEqual([
    'Einleitung mit Beleg [K1].x\n\nSchluss mit Gruppe [K2, W7].',
    'Einleitung mit Beleg [K1].\n\nSchluss mit Gruppe [K2, W7].',
  ])
  await page.keyboard.type('y')
  await expect(firstParagraph(page)).toHaveText('Einleitung mit Beleg [K1].y')
})

test('the code block language picker follows the right to edit', async ({ page }) => {
  // A reconnect or a session refresh grants or withdraws the right to edit a
  // collaborative document while the editor stays mounted.
  await page.goto(`${fixturePath}?scenario=collaboration`)
  await expect(page.getByTestId('body-change-surface')).toHaveAttribute('data-ready', 'true')
  await page.waitForTimeout(SETTLE_MS)
  const picker = page.getByRole('combobox', { name: 'Code block language' })
  await expect(picker).toBeEnabled()

  for (const canEdit of [false, true, false, true]) {
    await page.evaluate((value) => window.__setCollaborationCanEdit!(value), canEdit)
    await expect(editorTextbox(page)).toHaveAttribute('aria-readonly', String(!canEdit))
    // A read of the settled state, not a wait for it: a later, unrelated
    // transaction must not be what brings the picker up to date.
    await page.waitForTimeout(SETTLE_MS)
    expect(await picker.isDisabled(), `can edit: ${String(canEdit)}`).toBe(!canEdit)
  }
  // A collaborative document is persisted by its room, never as a body.
  expect(await bodyChanges(page)).toEqual([])
})
