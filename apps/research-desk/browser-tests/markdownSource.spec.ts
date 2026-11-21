import { expect, test, type Page } from '@playwright/test'

// The Markdown source view of the editor, mounted alone: what it hands its
// host on each keystroke, how it takes a text another writer made, and that
// a switch of the interface language keeps its text and its undo history.
const STORED = 'Absatz eins.\n\nAbsatz zwei.'

async function openSource(page: Page) {
  await page.goto('/browser-tests/fixtures/markdown-source.html')
  const source = page.getByRole('textbox', { name: 'Markdown-Quelltext' })
  await expect(source).toBeVisible()
  return source
}

async function changes(page: Page) {
  return page.evaluate(() => window.__sourceChanges ?? [])
}

test('the source view hands over each text with the one it was typed on, and undoes only its own edits', async ({ page }) => {
  const source = await openSource(page)
  await source.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.type(' Eigen.')

  // One text per keystroke, each with the text shown before it.
  const typed = await changes(page)
  expect([typed[0]?.base, typed.at(-1)?.text]).toEqual([STORED, `${STORED} Eigen.`])
  expect(typed.slice(1).every((change, index) => change.base === typed[index].text)).toBe(true)

  // Another writer changes the first paragraph; the view takes that text,
  // and undo takes back the own edit only.
  await page.evaluate(() => window.__takeRecordText?.('Absatz eins, fremd.\n\nAbsatz zwei. Eigen.'))
  await expect(source).toContainText('Absatz eins, fremd.')
  await source.click()
  await page.keyboard.press('ControlOrMeta+z')

  expect((await changes(page)).at(-1)?.text).toBe('Absatz eins, fremd.\n\nAbsatz zwei.')
})

test('the source view keeps its text when the interface language switches', async ({ page }) => {
  const source = await openSource(page)
  await source.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.type(' Eigen.')

  const before = (await changes(page)).length

  await page.evaluate(() => window.__switchLanguage?.())
  const english = page.getByRole('textbox', { name: 'Markdown source' })
  await expect(english).toContainText('Absatz zwei. Eigen.')
  await english.click()
  await page.keyboard.press('ControlOrMeta+End')
  await page.keyboard.type(' Weiter.')

  // The first keystroke after the switch is typed on the text shown before
  // it, and nothing typed earlier is lost.
  const typed = await changes(page)
  expect([typed[before]?.base, typed.at(-1)?.text]).toEqual([`${STORED} Eigen.`, `${STORED} Eigen. Weiter.`])
})
