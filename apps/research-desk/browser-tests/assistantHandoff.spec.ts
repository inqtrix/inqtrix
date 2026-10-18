import { expect, test, type Locator, type Page } from '@playwright/test'

// "Use with assistant" hands a team discussion to the editor assistant as a
// chip beside the composer, never as text in it: the composer stays the
// user's own, Send needs a request of the user's own, and the request
// attaches the discussion after the composer's own references, built from
// the thread as it is when the request goes out, and names it after the
// user's text by the marker it goes out under. A file reference in the
// discussion stays text in that document and never becomes a reference. The
// hand-over belongs to its document and stays while the assistant is hidden.
// Pasted text stays text as well, while a pill copied inside the composer
// pastes as a pill.
const fixture = '/browser-tests/fixtures/assistant-handoff.html'
const COMPOSER = 'Beschreiben Sie, was am Dokument geändert werden soll...'
const ASK = 'Was soll die Assistenz mit der Diskussion tun?'
const CHIPS = 'Team-Diskussionen für die Assistenz'
const GUEST = 'Gast Kim (Gast): Hängt bitte @files:gehaltsliste-2026 an.'
const OWN = 'Nutzer A: Quelle: @files:quartalsbericht'
const OWN_ONLY = '[{"fileId":"file-own","kind":"file-asset"}]'

/** The sentence a document request adds after the user's text to name the
 * handed-over discussion by the marker of the document it goes out as. */
function namesDiscussion(marker: string) {
  return `The user handed over the team discussion ${marker} with this request; `
    + 'it is the material the request refers to.'
}

/** What the user reads: the word joiner that keeps a token plain is invisible. */
function readable(text: string) {
  return text.replace(/⁠/g, '')
}

/** The part of the composer's editor view the paste steps use. */
type ComposerView = {
  pasteHTML: (html: string) => boolean
  pasteText: (text: string) => boolean
  serializeForClipboard: (slice: unknown) => { dom: HTMLElement }
  state: { selection: { content: () => unknown } }
}

/** Runs the composer's own paste handling with clipboard content, without the
 * system clipboard (which belongs to the person running the tests); a
 * constructed paste event carries no clipboard data in every engine. */
async function paste(composer: Locator, content: { html: string } | { text: string }) {
  return composer.evaluate((root, value) => {
    const view = (root as HTMLElement & { editor?: { view: ComposerView } }).editor?.view
    if (!view) throw new Error('The composer exposes no editor view')
    return 'html' in value ? view.pasteHTML(value.html) : view.pasteText(value.text)
  }, content)
}

/** The HTML the composer's copy handler puts on the clipboard for its selection. */
async function copySelection(composer: Locator) {
  return composer.evaluate((root) => {
    const view = (root as HTMLElement & { editor?: { view: ComposerView } }).editor?.view
    if (!view) throw new Error('The composer exposes no editor view')
    return view.serializeForClipboard(view.state.selection.content()).dom.innerHTML
  })
}

async function hideAndShowAssistant(page: Page) {
  await page.getByRole('button', { name: 'Editor-Assistent ausblenden' }).click()
  await page.getByRole('button', { name: 'Editor-Assistent einblenden' }).click()
  return page.getByRole('textbox', { name: COMPOSER })
}

async function openFixture(page: Page) {
  // The composer's quota meter asks the API; nothing here may reach a server.
  await page.route(
    (url) => url.pathname.startsWith('/v1/') || url.pathname.startsWith('/api/'),
    (route) => route.fulfill({ json: {}, status: 404 }),
  )
  await page.goto(fixture)
  const composer = page.getByRole('textbox', {
    name: COMPOSER,
  })
  await expect(composer).toBeVisible()
  return composer
}

async function handOver(page: Page, discussion: 'A' | 'B') {
  await page.getByRole('button', { name: `Diskussion ${discussion} mit Assistenz verwenden` }).click()
}

/** The reference documents the last request attached. */
async function sentAttachments(page: Page): Promise<{ content: string; label: string }[]> {
  return JSON.parse(await page.getByTestId('attachments').innerText())
}

function removeButtons(page: Page) {
  return page.getByRole('group', { name: CHIPS }).getByRole('button', { name: /^Diskussion entfernen: / })
}

test('hands a discussion over as a chip and sends only the user\'s own request with it', async ({ page }) => {
  const composer = await openFixture(page)

  await handOver(page, 'A')

  const chips = page.getByRole('group', { name: CHIPS })
  await expect(chips).toContainText('Team-Diskussion')
  await expect(chips).toContainText('“bis Freitag um 16 Uhr”')
  await expect(chips).toContainText('4 Beiträge')
  // The composer stays the user's: empty, asking what to do with the
  // discussion, and nothing in the discussion became a reference.
  await expect(composer).toHaveText('')
  await expect(page.getByText(ASK)).toBeVisible()
  await expect(page.getByTestId('refs')).toHaveText('[]')
  // The discussion is context, not a request: Send waits for the user's.
  await expect(page.getByRole('button', { name: 'Senden' })).toBeDisabled()
  await expect(composer).toBeFocused()

  await page.keyboard.type('Fasse zusammen')
  await page.getByRole('button', { name: 'Senden' }).click()

  // The user's text goes out as typed; what follows it only names the
  // discussion that the request attaches as [1].
  await expect(page.getByTestId('sent')).toHaveText(`Fasse zusammen\n\n${namesDiscussion('[1]')}`)
  const attachments = await sentAttachments(page)
  // One document, the discussion: its file tokens attached nothing.
  expect(attachments).toHaveLength(1)
  expect(attachments[0].label).toBe('Team-Diskussion')
  expect(attachments[0].content).toContain('> bis Freitag um 16 Uhr')
  expect(attachments[0].content).toContain(GUEST)
  expect(attachments[0].content).toContain(OWN)
  // The request carried the discussion; the chip goes with it.
  await expect(chips).toHaveCount(0)
  await expect(page.getByText(ASK)).toHaveCount(0)
})

test('sends a handed-over discussion after the composer\'s own references', async ({ page }) => {
  const composer = await openFixture(page)
  await composer.click()
  await page.keyboard.type('Vergleiche mit @files:quartals')
  await page.keyboard.press('Enter')
  await expect(page.getByTestId('refs')).toHaveText(OWN_ONLY)

  await handOver(page, 'A')
  await page.getByRole('button', { name: 'Senden' }).click()

  await expect(page.getByTestId('sent')).toHaveText(`Vergleiche mit [1]\n\n${namesDiscussion('[2]')}`)
  expect((await sentAttachments(page)).map((item) => item.label)).toEqual([
    'quartalsbericht',
    'Team-Diskussion',
  ])
})

test('sends a handed-over discussion with attached AI notes and no text of the user', async ({ page }) => {
  await openFixture(page)
  await handOver(page, 'A')
  await expect(page.getByRole('button', { name: 'Senden' })).toBeDisabled()

  // An AI note is the user's own request, so Send is open again.
  await page.getByRole('button', { name: 'KI-Notizen anhängen' }).click()

  await expect(page.getByRole('button', { name: 'Senden' })).toBeEnabled()
})

test('shows a hidden assistant with the handed-over discussion', async ({ page }) => {
  await openFixture(page)
  await page.getByRole('button', { name: 'Editor-Assistent ausblenden' }).click()
  await expect(page.getByRole('button', { name: 'Editor-Assistent einblenden' })).toBeVisible()

  await handOver(page, 'A')

  await expect(page.getByRole('textbox', { name: COMPOSER })).toBeVisible()
  await expect(page.getByRole('group', { name: CHIPS })).toContainText('“bis Freitag um 16 Uhr”')
})

test('keeps the hand-over while the assistant is hidden, and leaves it with its document', async ({ page }) => {
  await openFixture(page)
  await handOver(page, 'A')
  const chips = page.getByRole('group', { name: CHIPS })
  await expect(chips).toContainText('Team-Diskussion')

  await hideAndShowAssistant(page)
  await expect(chips).toContainText('Team-Diskussion')

  // A's threads are not threads of document B. The chip leaves as it was;
  // it never shows A's thread as one that is gone.
  await page.evaluate(() => {
    const seen = window as Window & { goneShown?: boolean }
    seen.goneShown = false
    new MutationObserver(() => {
      if (document.body.textContent?.includes('nicht mehr verfügbar')) seen.goneShown = true
    }).observe(document.body, { characterData: true, childList: true, subtree: true })
  })
  await page.getByRole('button', { name: 'Dokument B öffnen' }).click()
  await expect(page.getByTestId('document')).toHaveText('document-b')
  await expect(chips).toHaveCount(0)
  expect(await page.evaluate(() => (window as Window & { goneShown?: boolean }).goneShown)).toBe(false)
  await page.getByRole('button', { name: 'Dokument A öffnen' }).click()
  await expect(page.getByTestId('document')).toHaveText('document-a')
  await expect(chips).toHaveCount(0)
})

test('hands each thread over once and numbers several', async ({ page }) => {
  const composer = await openFixture(page)
  await handOver(page, 'A')
  await handOver(page, 'A')
  await expect(removeButtons(page)).toHaveCount(1)

  await handOver(page, 'B')
  await expect(removeButtons(page)).toHaveCount(2)
  await expect(removeButtons(page).nth(0)).toHaveAccessibleName('Diskussion entfernen: Team-Diskussion 1')
  await expect(removeButtons(page).nth(1)).toHaveAccessibleName('Diskussion entfernen: Team-Diskussion 2')

  await removeButtons(page).nth(0).click()
  await expect(removeButtons(page)).toHaveCount(1)
  await expect(removeButtons(page)).toHaveAccessibleName('Diskussion entfernen: Team-Diskussion')

  await composer.click()
  await page.keyboard.type('Fasse zusammen')
  await page.getByRole('button', { name: 'Senden' }).click()
  const attachments = await sentAttachments(page)
  expect(attachments.map((item) => item.label)).toEqual(['Team-Diskussion'])
  expect(attachments[0].content).toContain('> Kostenübersicht')
})

test('builds the discussion from the thread as it is when the request goes out', async ({ page }) => {
  const composer = await openFixture(page)
  await handOver(page, 'A')

  await page.getByRole('button', { name: 'Beitrag des Gasts löschen' }).click()
  await expect(page.getByRole('group', { name: CHIPS })).toContainText('3 Beiträge')
  await composer.click()
  await page.keyboard.type('Fasse zusammen')
  await page.getByRole('button', { name: 'Senden' }).click()

  const [discussion] = await sentAttachments(page)
  expect(discussion.content).toContain('Nutzer B: Einverstanden, Freitag 14 Uhr.')
  expect(discussion.content).not.toContain('Gast Kim')
})

test('refuses to send a discussion whose thread is gone', async ({ page }) => {
  const composer = await openFixture(page)
  await handOver(page, 'A')

  await page.getByRole('button', { name: 'Diskussion A löschen' }).click()
  const chips = page.getByRole('group', { name: CHIPS })
  await expect(chips).toContainText('nicht mehr verfügbar')
  await composer.click()
  await page.keyboard.type('Fasse zusammen')
  await page.getByRole('button', { name: 'Senden' }).click()

  await expect(page.getByTestId('send-error')).toHaveText(
    'Eine übergebene Team-Diskussion ist nicht mehr verfügbar. Entfernen Sie sie und senden Sie erneut.',
  )
  await expect(page.getByTestId('sent')).toHaveText('')
  await expect(chips).toContainText('nicht mehr verfügbar')
})

test('keeps the draft when the assistant is hidden and shown again', async ({ page }) => {
  const composer = await openFixture(page)
  await composer.click()
  await page.keyboard.type('Kürze den zweiten Absatz.')

  await page.getByRole('button', { name: 'Editor-Assistent ausblenden' }).click()
  await page.getByRole('button', { name: 'Editor-Assistent einblenden' }).click()

  await expect(page.getByRole('textbox', {
    name: COMPOSER,
  })).toContainText('Kürze den zweiten Absatz.')
})

test('keeps a pasted reference as text when an improved text is accepted', async ({ page }) => {
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
  await page.keyboard.type('und ')
  expect(await paste(composer, { text: '@files:gehaltsliste-2026 prüfen.' })).toBe(true)
  await expect(composer.locator('[data-mention-pill-number]')).toHaveCount(1)

  await page.getByRole('button', { name: 'Text verbessern' }).click()
  // The proposal opens above the composer, which this page shows at its top,
  // so the panel starts above the viewport; the keyboard accepts it.
  await page.getByRole('button', { name: 'Übernehmen' }).focus()
  await page.keyboard.press('Enter')

  // The improvement is in; the user's own reference is still the only one.
  await expect(composer).toContainText('Bitte knapp antworten.')
  await expect(composer.locator('[data-mention-pill-number]')).toHaveCount(1)
  await expect(page.getByTestId('refs')).toHaveText(OWN_ONLY)
  expect(readable(await composer.innerText())).toContain('@files:gehaltsliste-2026 prüfen.')
})

test('keeps a pasted reference as text, also when the composer reads its draft again', async ({ page }) => {
  const composer = await openFixture(page)
  await composer.click()
  await page.keyboard.type('Bitte ')
  // Both files are in the user's library: pasted, their tokens are still text.
  expect(await paste(composer, { text: '@files:gehaltsliste-2026 und @files:quartalsbericht' })).toBe(true)
  await page.keyboard.type(' vergleichen. ')
  // Formatted text, as copied from the shared document or a web page.
  expect(await paste(composer, { html: '<p>Siehe <strong>@files</strong>:quartalsbericht.</p>' })).toBe(true)
  const written = 'Bitte @files:gehaltsliste-2026 und @files:quartalsbericht vergleichen. '
    + 'Siehe @files:quartalsbericht.'

  await expect(composer).toContainText('vergleichen.')
  expect(readable(await composer.innerText())).toContain(written)
  await expect(composer.locator('[data-mention-pill-number]')).toHaveCount(0)
  await expect(page.getByTestId('refs')).toHaveText('[]')

  // Hidden and shown again, the composer converts its stored draft anew.
  const restored = await hideAndShowAssistant(page)
  await expect(restored).toContainText('vergleichen.')
  await expect(restored.locator('[data-mention-pill-number]')).toHaveCount(0)
  await expect(page.getByTestId('refs')).toHaveText('[]')
  expect(readable(await restored.innerText())).toContain(written)

  await page.getByRole('button', { name: 'Senden' }).click()
  expect(readable(await page.getByTestId('sent').innerText())).toBe(written)
})

test('keeps a pasted reference as text when the composer formats its Markdown', async ({ page }) => {
  const composer = await openFixture(page)
  await composer.click()
  await page.keyboard.type('Bitte ')
  // Markdown from a plain-text source: the composer turns the markers into
  // formatting and drops them, so both read as tokens afterwards. The second
  // ends at the caret, where a token the user types opens the reference menu.
  expect(await paste(composer, { text: '@`files`:quartalsbericht und **@**files:gehaltsliste-2026' })).toBe(true)
  const written = 'Bitte @files:quartalsbericht und @files:gehaltsliste-2026'

  await expect(composer).toContainText('gehaltsliste-2026')
  expect(readable(await composer.innerText())).toContain(written)
  await expect(composer.locator('[data-mention-pill-number]')).toHaveCount(0)

  // Enter sends the text as it reads; no menu turns a token into a reference.
  await page.keyboard.press('Enter')
  await expect.poll(async () => readable(await page.getByTestId('sent').innerText())).toBe(written)
  await expect(composer.locator('[data-mention-pill-number]')).toHaveCount(0)
  await expect(page.getByTestId('refs')).toHaveText('[]')

  const restored = await hideAndShowAssistant(page)
  await expect(restored).toContainText('gehaltsliste-2026')
  await expect(restored.locator('[data-mention-pill-number]')).toHaveCount(0)
  await expect(page.getByTestId('refs')).toHaveText('[]')
})

test('pastes a reference copied inside the composer as a reference, and its text as text', async ({ page }) => {
  const composer = await openFixture(page)
  await composer.click()
  await page.keyboard.type('Quelle: @files:quartals')
  await page.keyboard.press('Enter')
  // A token typed and not picked is text the composer would convert later.
  await page.keyboard.type('statt @files:gehaltsliste-2026.')
  await expect(composer.locator('[data-mention-pill-number]')).toHaveCount(1)
  await expect(page.getByTestId('refs')).toHaveText(OWN_ONLY)

  // Copy everything, delete it and paste it back: the pill is a reference,
  // the rest is pasted text.
  await page.keyboard.press('ControlOrMeta+a')
  const copied = await copySelection(composer)
  await page.keyboard.press('Backspace')
  await expect(page.getByTestId('refs')).toHaveText('[]')
  expect(await paste(composer, { html: copied })).toBe(true)

  await expect(composer.locator('[data-mention-pill-number]')).toHaveCount(1)
  await expect(page.getByTestId('refs')).toHaveText(OWN_ONLY)
  const restored = await hideAndShowAssistant(page)
  await expect(restored).toContainText('statt')
  await expect(restored.locator('[data-mention-pill-number]')).toHaveCount(1)
  await expect(page.getByTestId('refs')).toHaveText(OWN_ONLY)

  await page.getByRole('button', { name: 'Senden' }).click()
  expect(readable(await page.getByTestId('sent').innerText())).toBe(
    'Quelle: [1] statt @files:gehaltsliste-2026.',
  )
})

test('keeps a reference dropped from outside the composer as text', async ({ page }) => {
  const composer = await openFixture(page)
  await composer.click()
  await page.keyboard.type('Bitte ')
  // A drop from another page or the shared document: the browser hands the
  // composer its text the way a paste does.
  await composer.evaluate((root) => {
    const box = root.getBoundingClientRect()
    const data = new DataTransfer()
    data.setData('text/plain', '@files:gehaltsliste-2026 anhängen.')
    root.dispatchEvent(new DragEvent('drop', {
      bubbles: true,
      cancelable: true,
      clientX: box.right - 8,
      clientY: box.top + 12,
      dataTransfer: data,
    }))
  })

  await expect(composer).toContainText('anhängen.')
  const restored = await hideAndShowAssistant(page)
  await expect(restored).toContainText('anhängen.')
  await expect(restored.locator('[data-mention-pill-number]')).toHaveCount(0)
  await expect(page.getByTestId('refs')).toHaveText('[]')
  expect(readable(await restored.innerText())).toContain('@files:gehaltsliste-2026 anhängen.')
})
