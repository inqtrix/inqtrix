import { expect, test, type Page } from '@playwright/test'

import { enableDemoMode, installBackendFreeDiscoveryRoutes } from './demoApp'

// The research composer and the Agent Desk start with the report profile and
// the Stufe the user picked last. In the demo there is no account, so the
// device keeps the picks; this spec reloads and therefore never clears the
// storage on load the way the other demo specs do. Every test starts in a
// fresh browser context, so it starts without a pick all the same.

async function prepare(page: Page) {
  await page.addInitScript(() => {
    // These specs address the UI by its German labels.
    window.localStorage.setItem('inqtrix.researchDesk.locale', 'de')
  })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await installBackendFreeDiscoveryRoutes(page)
}

async function openDemo(page: Page) {
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  // The demo mode is project state: a reload ends it, so it is switched on
  // again after every load.
  await enableDemoMode(page)
}

async function showWorkspace(page: Page, name: string) {
  const navigation = page.getByRole('button', { exact: true, name })
  await navigation.click()
  await expect(navigation).toHaveAttribute('aria-pressed', 'true')
}

function reportProfile(page: Page, label: string) {
  return page.getByRole('button', { exact: true, name: `Report-Profil: ${label}` })
}

async function pickReportProfile(page: Page, from: string, label: string) {
  await reportProfile(page, from).click()
  await page.getByRole('menuitem').filter({ has: page.getByText(label, { exact: true }) }).click()
  await expect(reportProfile(page, label)).toBeVisible()
}

function runSetup(page: Page) {
  return page.locator('.agent-run-setup-trigger')
}

async function pickStufe(page: Page, label: string) {
  // The run setup keeps its menu open on a choice; Escape closes it.
  await runSetup(page).click()
  await expect(runSetup(page)).toHaveAttribute('aria-expanded', 'true')
  await page.locator('[role="menuitem"][aria-haspopup]').filter({ hasText: 'Tempo & Tiefe' }).click()
  await page.locator('[role="menuitem"]:not([aria-haspopup])')
    .filter({ has: page.getByText(label, { exact: true }) })
    .click()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('menu')).toHaveCount(0)
  await expect(runSetup(page)).toContainText(label)
}

test.beforeEach(async ({ page }) => {
  await prepare(page)
})

test('both composers start with the last profile and Stufe after a reload', async ({ page }) => {
  // Two loads of the whole app, each with the demo switched on.
  test.setTimeout(120_000)
  await openDemo(page)
  await showWorkspace(page, 'Research Desk')
  // Without a pick: the composer defaults, Tief and Gründlich.
  await pickReportProfile(page, 'Tief', 'Kompakt')
  await expect(page.getByRole('button', { exact: true, name: 'Max. Runden: 2' })).toBeVisible()
  // A view switch rebuilds the composer; the profile stays.
  await showWorkspace(page, 'Chat')
  await showWorkspace(page, 'Research Desk')
  await expect(reportProfile(page, 'Kompakt')).toBeVisible()

  await showWorkspace(page, 'Agent Desk')
  await expect(runSetup(page)).toContainText('Gründlich')
  await pickStufe(page, 'Schnell')

  await page.reload({ waitUntil: 'domcontentloaded' })
  await enableDemoMode(page)
  await showWorkspace(page, 'Research Desk')
  // The profile comes back with its preset.
  await expect(reportProfile(page, 'Kompakt')).toBeVisible()
  await expect(page.getByRole('button', { exact: true, name: 'Max. Runden: 2' })).toBeVisible()
  await showWorkspace(page, 'Agent Desk')
  await expect(runSetup(page)).toContainText('Schnell')
})

test('another tab follows the picks while nothing is changed there', async ({ context, page }) => {
  // The desk saves the whole preference row: a tab that kept an older pick
  // would send it back with its next save.
  test.setTimeout(120_000)
  await openDemo(page)
  const other = await context.newPage()
  await prepare(other)
  await openDemo(other)
  await showWorkspace(other, 'Research Desk')
  await expect(reportProfile(other, 'Tief')).toBeVisible()

  await showWorkspace(page, 'Research Desk')
  await pickReportProfile(page, 'Tief', 'Schnell')
  await expect(reportProfile(other, 'Schnell')).toBeVisible()
  await expect(other.getByRole('button', { exact: true, name: 'Max. Runden: 1' })).toBeVisible()

  await showWorkspace(page, 'Agent Desk')
  await pickStufe(page, 'Tief')
  await showWorkspace(other, 'Agent Desk')
  await expect(runSetup(other)).toContainText('Tief')

  // The language rides the same row, so it is taken over the same way.
  await expect(other.locator('html')).toHaveAttribute('lang', 'de')
  await page.getByRole('group', { name: 'Sprache' }).getByRole('button', { exact: true, name: 'EN' }).click()
  await expect(other.locator('html')).toHaveAttribute('lang', 'en')
  await other.close()
})
