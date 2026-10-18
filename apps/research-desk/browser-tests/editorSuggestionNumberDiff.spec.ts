import { expect, test } from '@playwright/test'

const fixturePath = '/browser-tests/fixtures/editor-lifecycle.html'

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => {
    throw error
  })
})

test('an AI proposal that changes an amount strikes and inserts whole numbers', async ({ page }) => {
  await page.goto(`${fixturePath}?scenario=suggestion-number-diff`)

  // Each changed amount is one struck and one inserted span: a fragmented
  // diff ("18" "000" / "21" "500") read like "1821.000500", and "1,5"
  // became a struck "1" before "2,5".
  await expect(page.locator('.suggestion-delete')).toHaveText(['18.000', '1,5'])
  await expect(page.locator('.suggestion-insert')).toHaveText(['21.500', '2,5'])
  await expect(page.locator('.ProseMirror p').first()).toHaveText(
    'Budget 18.00021.500 Euro für 1,52,5 Jahre',
  )
})
