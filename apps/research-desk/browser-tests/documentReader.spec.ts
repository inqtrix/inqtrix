import { expect, test } from '@playwright/test'

const fixturePath = '/browser-tests/fixtures/document-reader.html'

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => {
    throw error
  })
})

test('a second citation of the same document keeps the document readable', async ({ page }) => {
  // The regression: switching from one citation to another of the SAME
  // document emptied the Document tab, and no document request went out.
  // The fixture runs in the browser's default (English) interface.
  await page.goto(fixturePath)
  const reader = page.getByTestId('reader')

  await reader.getByRole('button', { exact: true, name: 'Document' }).click()
  await expect(reader.getByText('Anhang III nennt die Bereiche.')).toBeVisible()

  await page.getByRole('button', { name: 'Beleg K3' }).click()
  await reader.getByRole('button', { exact: true, name: 'Document' }).click()
  await expect(reader.getByText('Anhang III nennt die Bereiche.')).toBeVisible()

  // One document, one text request: the second citation reuses it.
  await expect(page.locator('body')).toHaveAttribute('data-document-loads', '1')
})
