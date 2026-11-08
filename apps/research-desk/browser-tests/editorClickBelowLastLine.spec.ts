import { expect, test } from '@playwright/test'

import { enableDemoMode, installBackendFreeDiscoveryRoutes } from './demoApp'

// A click into the empty page below the last line puts the cursor at the end
// of the document, as Docs and Word do. The editable element ends with the
// last line, so without this the click lands on the page around it and leaves
// the focus on the body.

test('a click below the last line places the cursor at the end of the document', async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.setItem('inqtrix.researchDesk.locale', 'de')
  })
  await installBackendFreeDiscoveryRoutes(page)
  await page.setViewportSize({ height: 1_100, width: 1_440 })
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await enableDemoMode(page)

  const navigation = page.getByRole('navigation', { name: 'Ansichten' })
  await navigation.getByRole('button', { exact: true, name: 'Editor' }).click()
  const textbox = page.getByRole('textbox', { name: 'Dokumentinhalt' })
  await expect(textbox).toBeVisible({ timeout: 30_000 })
  await expect(textbox).not.toBeFocused()

  // Bring the end of the document into view, then click into the page just
  // below the last line, in the column of the text.
  await textbox.evaluate((element) => element.lastElementChild?.scrollIntoView({ block: 'center' }))
  const box = await textbox.boundingBox()
  expect(box).not.toBeNull()
  await page.mouse.click(box!.x + 24, box!.y + box!.height + 12)

  await expect(textbox).toBeFocused()
  // What is typed now lands at the end of the document: the editor's own
  // text, read from its state, ends with it.
  await page.keyboard.type('§')
  const documentText = await textbox.evaluate((element) => {
    const editor = (element as HTMLElement & {
      editor?: { state: { doc: { content: { size: number }; textBetween: (from: number, to: number, separator: string) => string } } }
    }).editor
    if (!editor) return null
    const { doc } = editor.state
    return doc.textBetween(0, doc.content.size, '\n')
  })
  expect(documentText?.trimEnd().endsWith('§')).toBe(true)
})
