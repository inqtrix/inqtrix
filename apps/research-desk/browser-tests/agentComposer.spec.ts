import { expect, test, type Locator, type Page } from '@playwright/test'

import { enableDemoMode, installBackendFreeDiscoveryRoutes } from './demoApp'

// The Agent Desk composer in the real app (demo mode, no backend). The desk
// unmounts a workspace on every view switch, so whatever the agent draft
// carries must live outside the workspace to come back: the sentence with its
// capsules at their places, every attachment and the run setup.
const COMPOSER = /^Frage stellen oder Auftrag beschreiben/

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
  const navigation = page.getByRole('button', { exact: true, name })
  await navigation.click()
  await expect(navigation).toHaveAttribute('aria-pressed', 'true')
}

async function openAgentDesk(page: Page): Promise<Locator> {
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await enableDemoMode(page)
  await showWorkspace(page, 'Agent Desk')
  const composer = page.getByRole('textbox', { name: COMPOSER })
  await expect(composer).toBeVisible()
  return composer
}

/** Types a menu token and takes the row the menu offers for it. */
async function pick(page: Page, typed: string, row: RegExp) {
  await page.keyboard.type(typed)
  await expect(page.getByRole('button', { name: row }).first()).toBeVisible()
  await page.keyboard.press('Enter')
}

/** What the draft carries: its capsules by kind and title, in sentence
 * order, and the reports attached to it (as chip or capsule). */
async function draftAttachments(page: Page, composer: Locator) {
  return {
    pills: await composer.locator('[data-command-pill-kind]').evaluateAll((nodes) => nodes.map(
      (node) => `${node.getAttribute('data-command-pill-kind')}:${node.getAttribute('data-command-pill-label')}`,
    )),
    reports: await page.locator('[aria-label^="Recherche-Bericht: "]').count(),
  }
}

test('a view switch keeps the agent draft with its capsules, attachments and run setup', async ({ page }) => {
  const composer = await openAgentDesk(page)
  await composer.click()
  await page.keyboard.type('Bitte prüfe den Bericht ')
  await pick(page, '@research:Kleiderschrank', /digitalen Kleiderschrank/)
  await page.keyboard.type(' nach ')
  await pick(page, '@rules:brief', /Executive Briefing/)
  await page.keyboard.type('und nutze ')
  await pick(page, '/sprechz', /\/sprechzettel/)
  await page.keyboard.type('zusammen, ')
  await pick(page, '/canv', /\/canvas/)
  await page.keyboard.type('mit ')
  await pick(page, '/wiss', /\/wissen/)
  // The run setup belongs to the draft too: the engine and the web search.
  await page.getByRole('button', { exact: true, name: 'Ausführung' }).click()
  await page.getByRole('menuitem', { name: /^Missions-Maschine/ }).click()
  await page.keyboard.press('Escape')
  const setup = page.getByRole('button', { exact: true, name: 'Ausführung' })
  await expect(setup).toContainText('Missions-Maschine')
  const web = page.getByRole('button', { exact: true, name: 'Web' })
  await expect(web).toHaveAttribute('aria-pressed', 'true')
  await web.click()
  await expect(web).toHaveAttribute('aria-pressed', 'false')

  const before = await draftAttachments(page, composer)
  expect(before.pills).toEqual(expect.arrayContaining([
    'rule:Executive Briefing',
    'skill:sprechzettel',
    'responseForm:Canvas',
    'knowledge:Wissen',
  ]))
  expect(before.reports).toBe(1)
  const sentence = await composer.textContent()

  await showWorkspace(page, 'Prompt Library')
  await showWorkspace(page, 'Agent Desk')

  const restored = page.getByRole('textbox', { name: COMPOSER })
  await expect(restored).toBeVisible()
  expect(await draftAttachments(page, restored)).toEqual(before)
  expect(await restored.textContent()).toBe(sentence)
  await expect(setup).toContainText('Missions-Maschine')
  await expect(web).toHaveAttribute('aria-pressed', 'false')
})

test('opening the prompt library from the composer keeps the attached rule', async ({ page }) => {
  const composer = await openAgentDesk(page)
  await composer.click()
  await page.keyboard.type('Bitte nach ')
  await pick(page, '@rules:brief', /Executive Briefing/)
  // Every approved rule is attached: the empty row is the way to the library.
  await page.keyboard.type('@rules:')
  await expect(page.getByRole('button', { name: /Alle freigegebenen Regeln sind angehängt/ })).toBeVisible()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { exact: true, name: 'Prompt Library' }))
    .toHaveAttribute('aria-pressed', 'true')

  await showWorkspace(page, 'Agent Desk')
  const restored = page.getByRole('textbox', { name: COMPOSER })
  await expect(restored).toBeVisible()
  expect((await draftAttachments(page, restored)).pills).toEqual(['rule:Executive Briefing'])
})

const WELCOME = 'Was möchtest du wissen oder erarbeiten?'

test('a report picked with "@" sits in the sentence as a capsule and leaves with Backspace', async ({ page }) => {
  const composer = await openAgentDesk(page)
  await composer.click()
  await page.keyboard.type('Vergleiche ')
  await pick(page, '@research:Kleiderschrank', /digitalen Kleiderschrank/)
  const capsule = composer.locator('[data-command-pill-kind="report"]')
  await expect(capsule).toHaveCount(1)
  // Named by its whole title, never a raw id; shortened on screen only.
  await expect(capsule).toHaveAttribute(
    'data-command-pill-label',
    /^Gibt es ein Tool für einen digitalen Kleiderschrank/,
  )
  // No chip above the input: the capsule is the attachment.
  await expect(page.locator('button[aria-label^="Recherche-Bericht: "]')).toHaveCount(0)
  // It stands where "@" was typed: what the user writes next follows it.
  await page.keyboard.type('mit Fokus')
  const sentence = (await composer.textContent()) ?? ''
  expect(sentence.indexOf('Gibt es ein Tool')).toBeGreaterThan(sentence.indexOf('Vergleiche'))
  expect(sentence.indexOf('Gibt es ein Tool')).toBeLessThan(sentence.indexOf('mit Fokus'))
  // A word of the sentence: the typed words and the space after it, then
  // the capsule itself.
  for (let index = 0; index < 'mit Fokus'.length + 1; index += 1) await page.keyboard.press('Backspace')
  await expect(capsule).toHaveCount(1)
  await page.keyboard.press('Backspace')
  await expect(capsule).toHaveCount(0)
})

test('the sent question names each document in quotes, however it was attached', async ({ page }) => {
  const composer = await openAgentDesk(page)
  await composer.click()
  await page.keyboard.type('Vergleiche ')
  await pick(page, '@research:Kleiderschrank', /digitalen Kleiderschrank/)
  await page.keyboard.type('kurz mit')
  // A second report through "+": a capsule at the end of the draft.
  await page.getByRole('button', { name: 'Kontext oder Bearbeitungsziel hinzufügen' }).click()
  await page.getByRole('menuitem', { name: /^Recherche-Berichte/ }).click()
  await page.getByRole('menuitem', { name: /Gemini/ }).click()
  await page.keyboard.press('Escape')
  await expect(composer.locator('[data-command-pill-kind="report"]')).toHaveCount(2)
  await composer.press('End')
  await composer.press('Enter')
  // The question names both reports; it also titles the session.
  const sent = page.getByText(/^Vergleiche „Gibt es ein Tool für einen digitalen Kleiderschrank[^“]*“ kurz mit „Ich habe gehört[^“]*“$/)
  await expect(sent.first()).toBeVisible()
})

test('Enter on a menu row with nothing to pick sends nothing', async ({ page }) => {
  const composer = await openAgentDesk(page)
  await composer.click()
  await page.keyboard.type('Bitte @rules:zzz')
  await expect(page.getByRole('button', { name: /Kein Treffer für „zzz“/ })).toBeVisible()
  await page.keyboard.press('Enter')
  await expect(page.getByRole('button', { name: /Kein Treffer für „zzz“/ })).toHaveCount(0)
  await expect(composer).toContainText('Bitte @rules:zzz')
  // No run started: the desk still greets.
  await expect(page.getByRole('heading', { name: WELCOME })).toBeVisible()
})

test('the editing target stays for the next task', async ({ page }) => {
  const composer = await openAgentDesk(page)
  await composer.click()
  await page.keyboard.type('Überarbeite @')
  await page.getByRole('button', { name: /^Bearbeitungsziel/ }).click()
  await page.keyboard.press('Enter')
  const target = composer.locator('[data-command-pill-kind="editTarget"]')
  await expect(target).toHaveCount(1)
  const title = await target.getAttribute('data-command-pill-label')
  await page.keyboard.type('nach den Kommentaren.')
  await page.keyboard.press('Enter')
  // The question names the target; it also titles the session.
  await expect(page.getByText(`Überarbeite „${title}“ nach den Kommentaren.`, { exact: true }).first())
    .toBeVisible()
  // The draft keeps the target, as a capsule at its start, and nothing else.
  await expect(target).toHaveCount(1)
  await expect(composer).toHaveText(title ?? '')
  // The kept target holds the one slot: the menu says so in its own words.
  await composer.press('End')
  await page.keyboard.type('@')
  await expect(page.getByRole('button', { name: /^Bearbeitungsziel/ }))
    .toContainText('Höchstens ein Bearbeitungsziel je Auftrag')
})

test('a report the engine refuses holds the send until the kernel is back', async ({ page }) => {
  const composer = await openAgentDesk(page)
  await composer.click()
  await page.keyboard.type('Fasse ')
  await pick(page, '@research:Kleiderschrank', /digitalen Kleiderschrank/)
  await page.keyboard.type('zusammen.')
  const send = page.getByTestId('agent-submit')
  await expect(send).toBeEnabled()
  const setup = page.getByRole('button', { exact: true, name: 'Ausführung' })
  const engine = async (name: RegExp) => {
    await setup.click()
    await page.getByRole('menuitem', { name }).click()
    await page.keyboard.press('Escape')
    // The menu fades out first; a click during the fade only ends it.
    await expect(page.getByRole('menu')).toHaveCount(0)
  }
  // The mission engine takes no report: the server would answer the send
  // with 400, so the send waits and the notice says how to go on.
  await engine(/^Missions-Maschine/)
  await expect(page.getByTestId('agent-engine-refusal')).toContainText('um zu senden')
  await expect(send).toBeDisabled()
  await composer.press('End')
  await composer.press('Enter')
  await expect(composer).toContainText('zusammen.')
  await expect(page.getByRole('heading', { name: WELCOME })).toBeVisible()
  // Back on the kernel the same draft may go.
  await engine(/^Automatisch/)
  await expect(page.getByTestId('agent-engine-refusal')).toHaveCount(0)
  await expect(send).toBeEnabled()
})

test('"+" leads to the prompt library, as in the chat', async ({ page }) => {
  await openAgentDesk(page)
  await page.getByRole('button', { name: 'Kontext oder Bearbeitungsziel hinzufügen' }).click()
  await page.getByRole('menuitem', { name: /^Prompt Library öffnen/ }).click()
  await expect(page.getByRole('button', { exact: true, name: 'Prompt Library' }))
    .toHaveAttribute('aria-pressed', 'true')
})
