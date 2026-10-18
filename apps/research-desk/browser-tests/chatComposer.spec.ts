import { expect, test, type Locator, type Page } from '@playwright/test'

// The chat composer turns `@files:` tokens into references to the user's own
// library. Text pasted into it is not the user's own writing and stays text,
// also when the composer reads its draft again (the chat shown again after a
// view switch) and when an improved text written by a model is accepted.
const fixture = '/browser-tests/fixtures/chat-composer.html'
const COMPOSER = 'Nachricht an das Modell... @ für Kontext'
const OWN_ONLY = '[{"fileId":"file-own","kind":"file-asset"}]'

/** What the user reads: the word joiner that keeps a token plain is invisible. */
function readable(text: string) {
  return text.replace(/\u2060/g, '')
}

/** The part of the composer's editor view the paste step uses. */
type ComposerView = { pasteText: (text: string) => boolean }

/** Runs the composer's own paste handling with clipboard text, without the
 * system clipboard (which belongs to the person running the tests); a
 * constructed paste event carries no clipboard data in every engine. */
async function pasteText(composer: Locator, text: string) {
  return composer.evaluate((root, value) => {
    const view = (root as HTMLElement & { editor?: { view: ComposerView } }).editor?.view
    if (!view) throw new Error('The composer exposes no editor view')
    return view.pasteText(value)
  }, text)
}

async function openFixture(page: Page) {
  // The composer's quota meter asks the API; nothing here may reach a server.
  await page.route(
    (url) => url.pathname.startsWith('/v1/') || url.pathname.startsWith('/api/'),
    (route) => route.fulfill({ json: {}, status: 404 }),
  )
  await page.goto(fixture)
  const composer = page.getByRole('textbox', { name: COMPOSER })
  await expect(composer).toBeVisible()
  return composer
}

test('keeps a pasted reference as text in the chat composer, also when the chat is shown again', async ({ page }) => {
  const composer = await openFixture(page)
  await composer.click()
  await page.keyboard.type('Bitte ')
  // Both files are in the user's library: pasted, their tokens are still text,
  // also the one the composer only forms by formatting the Markdown around it.
  expect(await pasteText(composer, '@files:gehaltsliste-2026 und @`files`:quartalsbericht')).toBe(true)
  await page.keyboard.type(' vergleichen.')
  const written = 'Bitte @files:gehaltsliste-2026 und @files:quartalsbericht vergleichen.'
  await expect(composer).toContainText('vergleichen.')
  await expect(composer.locator('[data-mention-pill-number]')).toHaveCount(0)

  // The desk unmounts the chat on a view switch; shown again, the composer
  // converts the session draft anew.
  await page.getByRole('button', { name: 'Chat ausblenden' }).click()
  await page.getByRole('button', { name: 'Chat einblenden' }).click()
  const restored = page.getByRole('textbox', { name: COMPOSER })
  await expect(restored).toContainText('vergleichen.')
  await expect(restored.locator('[data-mention-pill-number]')).toHaveCount(0)
  expect(readable(await restored.innerText())).toContain(written)

  await page.getByRole('button', { name: 'Nachricht senden' }).click()
  await expect(page.getByTestId('sent-refs')).toHaveText('[]')
  expect(readable(await page.getByTestId('sent').textContent() ?? '')).toBe(written)
})

test('keeps a pasted reference as text when an improved chat text is accepted, and the user\'s own as a reference', async ({ page }) => {
  const composer = await openFixture(page)
  // The improvement comes from a model, which may drop the invisible word
  // joiner that keeps the pasted token plain; this one drops it.
  await page.route(
    (url) => url.pathname === '/v1/text/improvements',
    async (route) => {
      const { text } = route.request().postDataJSON() as { text: string }
      await route.fulfill({
        json: {
          change_summary: [],
          clarification_questions: [],
          improved_text: `${readable(text)}\nBitte knapp antworten.`,
          needs_clarification: false,
          warnings: [],
        },
      })
    },
  )
  await composer.click()
  await page.keyboard.type('Quelle: @files:quartals')
  await page.keyboard.press('Enter')
  await expect(composer.locator('[data-mention-pill-number]')).toHaveCount(1)
  // The picked reference is followed by a space already.
  await page.keyboard.type('und ')
  expect(await pasteText(composer, '@files:gehaltsliste-2026')).toBe(true)
  await expect(composer).toContainText('gehaltsliste-2026')

  await page.getByRole('button', { name: 'Text verbessern' }).click()
  await page.getByRole('button', { name: 'Übernehmen' }).focus()
  await page.keyboard.press('Enter')

  // The improvement is in; the user's own reference is still the only one.
  await expect(composer).toContainText('Bitte knapp antworten.')
  await expect(composer.locator('[data-mention-pill-number]')).toHaveCount(1)
  expect(readable(await composer.innerText())).toContain('und @files:gehaltsliste-2026')

  await page.getByRole('button', { name: 'Nachricht senden' }).click()
  await expect(page.getByTestId('sent-refs')).toHaveText(OWN_ONLY)
  expect(readable(await page.getByTestId('sent').textContent() ?? '')).toBe(
    'Quelle: [1] und @files:gehaltsliste-2026\nBitte knapp antworten.',
  )
})
