import { expect, test, type Page } from '@playwright/test'

import { enableDemoMode, installBackendFreeDiscoveryRoutes } from './demoApp'

// Incognito in the real app (demo mode, no backend): a screen reader hears
// that a message is not saved, and the empty conversation states what the
// server keeps of an incognito chat or Knowledge Desk ask.
const PROMISE =
  'Inhalte werden nicht gespeichert und nicht protokolliert; Nutzungsmengen werden gezählt.'

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.clear()
    window.sessionStorage.clear()
    // These specs address the UI by its German labels.
    window.localStorage.setItem('inqtrix.researchDesk.locale', 'de')
  })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await installBackendFreeDiscoveryRoutes(page)
})

async function showWorkspace(page: Page, name: string) {
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await enableDemoMode(page)
  const navigation = page.getByRole('button', { exact: true, name })
  await navigation.click()
  await expect(navigation).toHaveAttribute('aria-pressed', 'true')
}

test('an incognito chat names its composer for what is kept and says so', async ({ page }) => {
  await showWorkspace(page, 'Chat')
  await expect(page.getByRole('textbox', { name: 'Nachricht an das Modell... @ für Kontext' })).toBeVisible()
  await expect(page.getByText(PROMISE)).toHaveCount(0)

  await page.getByRole('button', { exact: true, name: 'Inkognito' }).click()

  await expect(
    page.getByRole('textbox', { name: 'Nachricht (wird nicht gespeichert)... @ für Kontext' }),
  ).toBeVisible()
  await expect(page.getByText(PROMISE)).toBeVisible()
  // The header badge keeps the promise at hand once the conversation runs.
  await expect(page.locator(`[title="${PROMISE}"]`)).toHaveCount(1)
})

test('an incognito Knowledge Desk ask says what the server keeps', async ({ page }) => {
  await showWorkspace(page, 'Knowledge Desk')
  await expect(page.getByText(PROMISE)).toHaveCount(0)

  await page.getByRole('button', { exact: true, name: 'Inkognito fragen' }).click()

  await expect(page.getByText(PROMISE)).toBeVisible()
  await expect(page.locator(`[title="${PROMISE}"]`)).toHaveCount(1)
})
