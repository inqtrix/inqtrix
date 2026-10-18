import { expect, test, type Page } from '@playwright/test'

const fixturePath = '/browser-tests/fixtures/index-pause.html'

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => {
    throw error
  })
})

/** No API answers: the database view must work from its own state. */
async function serveNothing(page: Page) {
  await page.route(
    (url) => url.pathname.startsWith('/v1/') || url.pathname.startsWith('/api/'),
    (route) => route.fulfill({ json: {}, status: 404 }),
  )
}

const withoutKnowledge = [
  {
    mode: 'Knowledge base off',
    notice: 'The knowledge base is switched off in this deployment.',
    reason: 'operator_disabled',
  },
  {
    mode: 'Vector store unreachable',
    notice: 'The vector store is not answering right now.',
    reason: 'store_unreachable',
  },
] as const

for (const { mode, notice, reason } of withoutKnowledge) {
  test(`without a knowledge base (${reason}) indexes are read-only and say why`, async ({ page }) => {
    // The index already lives on the server and none of its three documents
    // is embedded: with knowledge working every build action would be on
    // offer, here none may be, and the reason has to be in view.
    await serveNothing(page)
    await page.goto(`${fixturePath}?knowledge=${reason}&emptyIndex=1`)

    const newIndex = page.getByRole('button', { name: /New vector index/ })
    await expect(newIndex).toBeDisabled()
    await expect(newIndex).toContainText(mode)

    await page.getByRole('button', { name: 'Verträge 3' }).click()
    await expect(page.getByText(notice)).toBeVisible()
    await expect(page.getByRole('button', { exact: true, name: 'Index 3' })).toBeDisabled()
    await expect(page.getByRole('button', { exact: true, name: 'Now' })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Add documents' })).toBeDisabled()
    await expect(page.getByRole('button', { name: 'Index this file' })).toHaveCount(0)

    // An index without documents offers its own "add" in the empty state.
    await page.getByRole('button', { name: 'Leer 0' }).click()
    const add = page.getByRole('button', { name: 'Add documents' })
    await expect(add).toHaveCount(2)
    for (const button of await add.all()) await expect(button).toBeDisabled()
  })
}

test('the demo still builds its index with the local simulator', async ({ page }) => {
  // The same page without a server and without a reason is the demo: every
  // action stays on offer and the simulator carries the build to the end.
  await serveNothing(page)
  await page.goto(`${fixturePath}?knowledge=demo`)

  await expect(page.getByRole('button', { name: /New vector index/ })).toBeEnabled()
  await page.getByRole('button', { name: 'Verträge 3' }).click()
  await expect(page.getByRole('button', { name: 'Index this file' })).toHaveCount(3)
  await page.getByRole('button', { exact: true, name: 'Index 3' }).click()

  await expect(page.getByRole('button', { exact: true, name: 'Refresh' })).toBeEnabled({
    timeout: 10_000,
  })
})
