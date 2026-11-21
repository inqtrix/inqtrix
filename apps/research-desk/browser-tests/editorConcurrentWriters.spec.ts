import { expect, test, type Page } from '@playwright/test'

import { deskDetail, deskDocument, serveDesk, type DeskServer } from './editorDeskServer'

// A Markdown document with a second writer: another tab saves, or converts
// the document to collaboration, while this page has text of its own. The
// page runs the desk's real sync hook and editor over a stand-in of the
// service; each test says what reached the service and what the page shows.
// Nothing written here or there may get lost without a visible notice.

const fixturePath = '/browser-tests/fixtures/editor-concurrent-writers.html'
const DOC = 'doc-plan'
const OTHER = 'doc-other'
const SAVED = '# Plan\n\nAbsatz eins.\n\nAbsatz zwei.\n\nAbsatz drei.'
const OTHER_TEXT = '# Notizen\n\nNichts weiter.'
const COPY_TITLE = /^Plan \(Konfliktkopie \d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}\)\.md$/

function editor(page: Page) {
  return page.getByTestId('surface').locator('.ProseMirror')
}

function treeItem(page: Page, id: string) {
  return page.locator(`[data-testid="tree-item"][data-document-id="${id}"]`)
}

function watchPageErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text())
  })
  return errors
}

/** The console errors besides the browser's own report of a 409 the
 * service answered on purpose. */
function errorsBesidesConflicts(errors: string[]): string[] {
  return errors.filter((message) => !/\b409\b/.test(message))
}

/** Opens the desk over a service that holds `documents`, and the first one. */
async function openDesk(
  page: Page,
  documents = [deskDocument(DOC, 'Plan.md', SAVED)],
): Promise<DeskServer> {
  const server = await serveDesk(page, documents)
  await page.goto(fixturePath)
  await expect(page.getByTestId('tree-item')).toHaveCount(documents.length)
  await openDocument(page, documents[0].id, documents[0].text.split('\n')[0].replace(/^#\s*/, ''))
  return server
}

async function openDocument(page: Page, id: string, firstLine: string): Promise<void> {
  await treeItem(page, id).click()
  await expect(page.getByTestId('active-document')).toHaveText(id)
  await expect(editor(page)).toContainText(firstLine)
}

/** Puts the caret at the end of the paragraph that reads `paragraph`. */
async function caretAtEndOf(page: Page, paragraph: string): Promise<void> {
  await editor(page).locator('p', { hasText: paragraph }).click()
  await page.keyboard.press('End')
}

async function typeAtEndOf(page: Page, paragraph: string, text: string): Promise<void> {
  await caretAtEndOf(page, paragraph)
  await page.keyboard.type(text)
  await expect(editor(page)).toContainText(`${paragraph}${text}`)
}

/** The saves of documents other than `ids`: the conflict copies. */
function copySaves(server: DeskServer, ...ids: string[]) {
  return server.saves.filter((save) => !ids.includes(save.id))
}

/** Focuses the window: the desk refreshes its lists, as it does on focus,
 * when the page comes back online and after a reconnect. */
async function refresh(page: Page): Promise<void> {
  await page.evaluate(() => window.dispatchEvent(new Event('focus')))
}

function noticeAtOriginal(page: Page) {
  return page.locator('[data-editor-conflict-copy="original"]')
}

function barAtCopy(page: Page) {
  return page.locator('[data-editor-conflict-copy="copy"]')
}

test.beforeEach(async ({ context }) => {
  await context.addInitScript(() => {
    window.localStorage.setItem('inqtrix.researchDesk.locale', 'de')
  })
})

test('a save that lost against another writer keeps both changes in different paragraphs and saves them on the new revision', async ({ page }) => {
  const errors = watchPageErrors(page)
  const server = await openDesk(page)
  await typeAtEndOf(page, 'Absatz eins.', ' hier')
  server.writeElsewhere(DOC, SAVED.replace('Absatz drei.', 'Absatz drei, dort.'))

  await expect.poll(() => server.saves.length).toBe(1)

  expect(server.refusals).toEqual([`${DOC}: revision`])
  expect(server.saves[0]).toMatchObject({ id: DOC, revision: 3 })
  expect(server.saves[0].text).toContain('Absatz eins. hier')
  expect(server.saves[0].text).toContain('Absatz drei, dort.')
  await expect(editor(page)).toContainText('Absatz drei, dort.')
  await expect(editor(page)).toContainText('Absatz eins. hier')
  await expect(page.getByTestId('document-revision')).toHaveText('3')
  await expect(page.getByTestId('tree-item')).toHaveCount(1)
  await expect(noticeAtOriginal(page)).toHaveCount(0)
  expect(server.requestsOf('DELETE')).toEqual([])
  expect(errorsBesidesConflicts(errors)).toEqual([])
})

test('a save that lost against another writer in the same paragraph keeps the own version in a copy saved at once, and nothing is deleted', async ({ page }, testInfo) => {
  const errors = watchPageErrors(page)
  const server = await openDesk(page)
  await typeAtEndOf(page, 'Absatz zwei.', ' hier')
  server.writeElsewhere(DOC, SAVED.replace('Absatz zwei.', 'Absatz zwei, dort.'))

  // The copy reached the service, under an id of its own.
  await expect.poll(() => copySaves(server, DOC).length).toBe(1)
  const [copy] = copySaves(server, DOC)
  expect(copy.revision).toBe(1)
  expect(copy.text).toContain('Absatz zwei. hier')
  expect(copy.text).not.toContain('dort')
  expect(server.documents.get(copy.id)?.title).toMatch(COPY_TITLE)
  expect(server.refusals).toEqual([`${DOC}: revision`])

  // The original shows the version saved elsewhere, and says where the own
  // version went; the copy stands right before it in the tree.
  await expect(editor(page)).toContainText('Absatz zwei, dort.')
  await expect(editor(page)).not.toContainText('hier')
  await expect(page.getByTestId('document-revision')).toHaveText('2')
  await expect(page.getByTestId('tree-item')).toHaveCount(2)
  await expect(page.getByTestId('tree-item').first()).toHaveAttribute('data-document-id', copy.id)
  await expect(page.getByTestId('conflict-copies')).toHaveText(`${DOC}>concurrent_change:saved`)
  const notice = noticeAtOriginal(page)
  await expect(notice).toContainText('Ihre Fassung ließ sich nicht mit einer inzwischen gespeicherten Änderung zusammenführen.')
  await expect(notice).toContainText(server.documents.get(copy.id)!.title)
  await page.screenshot({ path: testInfo.outputPath('conflict-original.png') })

  // The notice opens the copy; its bar compares it with the original's
  // current text.
  await notice.getByRole('button', { exact: true, name: 'Kopie öffnen' }).click()
  await expect(page.getByTestId('active-document')).toHaveText(copy.id)
  const bar = barAtCopy(page)
  await expect(bar).toContainText('Diese Kopie enthält Ihre Fassung von „Plan.md“')
  await expect(editor(page)).toContainText('Absatz zwei. hier')
  await bar.getByRole('button', { exact: true, name: 'Mit Original vergleichen' }).click()
  const comparison = page.locator('.editor-document-diff')
  await expect(comparison).toBeVisible()
  await expect(comparison.locator('.editor-document-diff-delete, del, [data-diff-delete]').first()).toBeVisible()
  await expect(comparison).toContainText('dort')
  await expect(comparison).toContainText('hier')
  await page.screenshot({ path: testInfo.outputPath('conflict-copy-comparison.png') })
  await bar.getByRole('button', { exact: true, name: 'Vergleich schließen' }).click()
  await expect(comparison).toHaveCount(0)
  await expect(editor(page)).toContainText('Absatz zwei. hier')

  // Nothing of either version was deleted, and the copy outlives the page.
  await page.waitForTimeout(2_000)
  expect(server.requestsOf('DELETE')).toEqual([])
  expect(server.documents.get(DOC)?.text).toContain('Absatz zwei, dort.')
  await page.reload()
  await expect(page.getByTestId('tree-item')).toHaveCount(2)
  await expect(treeItem(page, copy.id)).toHaveText(COPY_TITLE)
  expect(errorsBesidesConflicts(errors)).toEqual([])
})

test('deleting a conflict copy from its bar deletes that copy only', async ({ page }) => {
  const server = await openDesk(page)
  await typeAtEndOf(page, 'Absatz zwei.', ' hier')
  server.writeElsewhere(DOC, SAVED.replace('Absatz zwei.', 'Absatz zwei, dort.'))
  await expect.poll(() => copySaves(server, DOC).length).toBe(1)
  const [copy] = copySaves(server, DOC)
  await noticeAtOriginal(page).getByRole('button', { exact: true, name: 'Kopie öffnen' }).click()

  await barAtCopy(page).getByRole('button', { exact: true, name: 'Kopie löschen' }).click()

  await expect(page.getByTestId('tree-item')).toHaveCount(1)
  await expect.poll(() => server.requestsOf('DELETE')).toEqual([`DELETE /v1/editor/documents/${copy.id}`])
  expect(server.documents.get(DOC)?.text).toContain('Absatz zwei, dort.')
  await openDocument(page, DOC, 'Plan')
  await expect(noticeAtOriginal(page)).toHaveCount(0)
})

test('a conflict at a document that is not open shows the copy in the tree and the notice once the original is opened', async ({ page }) => {
  const server = await openDesk(page, [
    deskDocument(DOC, 'Plan.md', SAVED),
    deskDocument(OTHER, 'Notizen.md', OTHER_TEXT),
  ])
  await typeAtEndOf(page, 'Absatz zwei.', ' hier')
  await openDocument(page, OTHER, 'Notizen')
  server.writeElsewhere(DOC, SAVED.replace('Absatz zwei.', 'Absatz zwei, dort.'))

  await expect.poll(() => copySaves(server, DOC, OTHER).length).toBe(1)
  const [copy] = copySaves(server, DOC, OTHER)
  await expect(treeItem(page, copy.id)).toHaveText(COPY_TITLE)
  await expect(noticeAtOriginal(page)).toHaveCount(0)
  await expect(barAtCopy(page)).toHaveCount(0)

  await openDocument(page, DOC, 'Plan')
  await expect(editor(page)).toContainText('Absatz zwei, dort.')
  await expect(noticeAtOriginal(page)).toContainText(server.documents.get(copy.id)!.title)
  expect(server.requestsOf('DELETE')).toEqual([])
})

test('a refresh brings a version saved elsewhere into the open document without a reload, and saves nothing', async ({ page }) => {
  const server = await openDesk(page)
  server.writeElsewhere(DOC, SAVED.replace('Absatz drei.', 'Absatz drei, dort.'))

  await refresh(page)

  await expect(editor(page)).toContainText('Absatz drei, dort.')
  await expect(page.getByTestId('document-revision')).toHaveText('2')
  expect(server.requestsOf('GET').filter((request) => request.endsWith(`/documents/${DOC}`)).length)
    .toBeGreaterThanOrEqual(2)
  await page.waitForTimeout(2_000)
  expect(server.saves).toEqual([])
  expect(server.refusals).toEqual([])
})

test('a refresh merges a version saved elsewhere with own unsaved text, and the next save writes both', async ({ page }) => {
  const server = await openDesk(page)
  await typeAtEndOf(page, 'Absatz eins.', ' hier')
  server.writeElsewhere(DOC, SAVED.replace('Absatz drei.', 'Absatz drei, dort.'))

  await refresh(page)

  await expect(editor(page)).toContainText('Absatz drei, dort.')
  await expect(editor(page)).toContainText('Absatz eins. hier')
  await expect.poll(() => server.saves.length).toBe(1)
  expect(server.saves[0]).toMatchObject({ id: DOC, revision: 3 })
  expect(server.saves[0].text).toContain('Absatz eins. hier')
  expect(server.saves[0].text).toContain('Absatz drei, dort.')
  expect(server.refusals).toEqual([])
})

test('a document saved elsewhere after the list is opened with its revision, so the first save is not refused', async ({ page }) => {
  // The newest document opens first; the other one is opened later.
  const server = await serveDesk(page, [
    deskDocument(DOC, 'Plan.md', SAVED),
    deskDocument(OTHER, 'Notizen.md', OTHER_TEXT),
  ])
  await page.goto(fixturePath)
  await expect(page.getByTestId('active-document')).toHaveText(OTHER)
  await expect(editor(page)).toContainText('Notizen')
  expect(server.requestsOf('GET')).not.toContain(`GET /v1/editor/documents/${DOC}`)
  server.writeElsewhere(DOC, SAVED.replace('Absatz drei.', 'Absatz drei, dort.'))

  await openDocument(page, DOC, 'Plan')
  await expect(editor(page)).toContainText('Absatz drei, dort.')
  await typeAtEndOf(page, 'Absatz eins.', ' hier')

  await expect.poll(() => server.saves.length).toBe(1)
  expect(server.saves[0]).toMatchObject({ id: DOC, revision: 3 })
  expect(server.refusals).toEqual([])
})

test('a save refused because the document was converted elsewhere keeps the own text in a copy, and the original switches to collaboration', async ({ page }, testInfo) => {
  const errors = watchPageErrors(page)
  const server = await openDesk(page)
  await typeAtEndOf(page, 'Absatz eins.', ' hier')
  server.convertElsewhere(DOC)

  await expect.poll(() => copySaves(server, DOC).length).toBe(1)
  const [copy] = copySaves(server, DOC)
  expect(copy.text).toContain('Absatz eins. hier')
  expect(server.refusals).toEqual([`${DOC}: content_mode_changed`])
  await expect(page.getByTestId('document-mode')).toHaveText('collaboration')
  await expect(page.getByTestId('conflict-copies')).toHaveText(`${DOC}>content_mode_changed:saved`)
  await expect(noticeAtOriginal(page)).toContainText(
    'Das Dokument wurde inzwischen in den Modus Zusammenarbeit umgestellt. Ihre Fassung von vor der Umstellung liegt in der Kopie',
  )
  await page.screenshot({ path: testInfo.outputPath('converted-original.png') })
  expect(server.requestsOf('DELETE')).toEqual([])
  expect(errorsBesidesConflicts(errors)).toEqual([])
})

test('a conversion elsewhere that a refresh shows first keeps the own text in a copy before the original switches', async ({ page }) => {
  const server = await openDesk(page)
  await typeAtEndOf(page, 'Absatz eins.', ' hier')
  server.convertElsewhere(DOC)

  // Before the autosave runs: the refresh sees the conversion first.
  await refresh(page)

  await expect.poll(() => copySaves(server, DOC).length).toBe(1)
  const [copy] = copySaves(server, DOC)
  expect(copy.text).toContain('Absatz eins. hier')
  await expect(page.getByTestId('document-mode')).toHaveText('collaboration')
  await expect(page.getByTestId('conflict-copies')).toHaveText(`${DOC}>content_mode_changed:saved`)
  // No save of the original was refused: the copy came from the refresh.
  expect(server.refusals).toEqual([])
  expect(server.requestsOf('DELETE')).toEqual([])
})

test('the caret stays and undo takes back only own typing after another writer\'s version was merged', async ({ page }) => {
  const server = await openDesk(page)
  await typeAtEndOf(page, 'Absatz eins.', ' hier')
  server.writeElsewhere(DOC, SAVED.replace('Absatz drei.', 'Absatz drei, dort.'))

  await refresh(page)
  await expect(editor(page)).toContainText('Absatz drei, dort.')

  // The caret is still where the user typed.
  await page.keyboard.type('X')
  await expect(editor(page)).toContainText('Absatz eins. hierX')
  // Undo takes back the own typing, never the other writer's change.
  for (let step = 0; step < 4; step += 1) {
    await page.keyboard.press('ControlOrMeta+z')
  }
  await expect(editor(page)).toContainText('Absatz eins.')
  await expect(editor(page)).not.toContainText('hier')
  await expect(editor(page)).toContainText('Absatz drei, dort.')
  await expect(page.getByTestId('record-markdown')).toContainText('Absatz drei, dort.')
})

test('a keystroke sent on the text before a merge, in another paragraph, keeps both', async ({ page }) => {
  const server = await openDesk(page)
  await caretAtEndOf(page, 'Absatz eins.')
  server.writeElsewhere(DOC, SAVED.replace('Absatz drei.', 'Absatz drei, dort.'))

  // Merge and keystroke in one task: the editor still shows the text before.
  await page.evaluate(([detail, text]) => window.__mergeAndType!(detail, text), [deskDetail(server, DOC), 'X'] as const)

  await expect(page.getByTestId('record-markdown')).toContainText('Absatz eins.X')
  await expect(page.getByTestId('record-markdown')).toContainText('Absatz drei, dort.')
  await expect(editor(page)).toContainText('Absatz eins.X')
  await expect(editor(page)).toContainText('Absatz drei, dort.')
  await expect.poll(() => server.saves.length).toBe(1)
  expect(server.saves[0]).toMatchObject({ id: DOC, revision: 3 })
  expect(server.saves[0].text).toContain('Absatz eins.X')
  expect(server.saves[0].text).toContain('Absatz drei, dort.')
  await expect(page.getByTestId('tree-item')).toHaveCount(1)
})

test('a keystroke sent on the text before a merge, in the same paragraph, goes to a copy and the record keeps the merged version', async ({ page }) => {
  const server = await openDesk(page)
  await caretAtEndOf(page, 'Absatz drei.')
  server.writeElsewhere(DOC, SAVED.replace('Absatz drei.', 'Absatz drei, dort.'))

  await page.evaluate(([detail, text]) => window.__mergeAndType!(detail, text), [deskDetail(server, DOC), 'X'] as const)

  await expect.poll(() => copySaves(server, DOC).length).toBe(1)
  const [copy] = copySaves(server, DOC)
  expect(copy.text).toContain('Absatz drei.X')
  expect(copy.text).not.toContain('dort')
  await expect(page.getByTestId('record-markdown')).toContainText('Absatz drei, dort.')
  await expect(page.getByTestId('record-markdown')).not.toContainText('Absatz drei.X')
  await expect(editor(page)).toContainText('Absatz drei, dort.')
  await expect(page.getByTestId('conflict-copies')).toHaveText(`${DOC}>concurrent_change:saved`)
  await expect(noticeAtOriginal(page)).toBeVisible()
  await page.waitForTimeout(2_000)
  // The original holds the merged version; nothing saved it over.
  expect(server.saves.filter((save) => save.id === DOC)).toEqual([])
  expect(server.documents.get(DOC)?.text).toContain('Absatz drei, dort.')
})

/** Whether leaving the page now would ask the user first. */
async function leavingAsks(page: Page): Promise<boolean> {
  return page.evaluate(() => {
    const event = new Event('beforeunload', { cancelable: true })
    window.dispatchEvent(event)
    return event.defaultPrevented
  })
}

test('leaving the page asks until the first save of a conflict copy is confirmed', async ({ page }) => {
  const server = await openDesk(page)
  // The copy's first save waits on the service.
  const release = server.hold((request) => (
    request.startsWith('PUT ') && !request.endsWith(`/documents/${DOC}`)
  ))
  expect(await leavingAsks(page)).toBe(false)
  await typeAtEndOf(page, 'Absatz zwei.', ' hier')
  server.writeElsewhere(DOC, SAVED.replace('Absatz zwei.', 'Absatz zwei, dort.'))

  await expect(page.getByTestId('conflict-copies')).toHaveText(`${DOC}>concurrent_change:unsaved`)
  expect(await leavingAsks(page)).toBe(true)

  release()
  await expect(page.getByTestId('conflict-copies')).toHaveText(`${DOC}>concurrent_change:saved`)
  expect(await leavingAsks(page)).toBe(false)
})
