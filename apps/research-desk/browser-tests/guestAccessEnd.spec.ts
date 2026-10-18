import { expect, test, type Page, type Route } from '@playwright/test'

// The real guest page and its real collaboration controller; every API
// request it makes is answered here, so a revocation, an expiry, a password
// change and a deleted document can be played without a server.
const fixture = '/browser-tests/fixtures/guest-editor.html'
const TITLE = 'Projektbericht zur Frist'
const COMMENT = 'Bitte die Frist bis Freitag prüfen.'
const ENDED_TITLE = 'Kein Zugriff mehr'
const REOPEN = 'Link erneut öffnen'
const REPLY = 'Antworten …'
const DRAFT = 'Mein Entwurf zur Frist, noch nicht gesendet.'
const DRAFT_PREFIX = 'inqtrix:collaboration-comment-drafts:v1:'
const FIRST_GUEST = '77777777-7777-4777-8777-777777777777'
const NEXT_GUEST = '66666666-6666-4666-8666-666666666666'

type GuestApi = {
  /** Answers the held collaboration session requests, and every later one,
   * with this status. */
  answerCollaboration: (status: number) => void
  guestRequestsSince: (since: number) => string[]
  setDescribeStatus: (status: number) => void
  /** The guest identity the session answers with from now on: another
   * guest has opened a link in this browser. */
  setGuestId: (id: string) => void
  setSessionStatus: (status: number) => void
}

async function guestApi(page: Page): Promise<GuestApi> {
  const now = Math.floor(Date.now() / 1_000)
  const description = {
    document_title: TITLE,
    expires_at: now + 3_600,
    label: 'Link A',
    password_required: true,
    permission: 'edit',
  }
  const access = {
    document: {
      comment_revision: 1,
      content_markdown: '## Abgabe\n\nDie Abgabe endet am Freitag.',
      generation: 1,
      id: 'doc-guest-fixture',
      persisted_sequence: 1,
      projection_sequence: 1,
      title: TITLE,
    },
    expires_at: now + 3_600,
    guest: { display_name: 'Maria', id: FIRST_GUEST, link_label: 'Link A' },
    permission: 'edit',
  }
  const author = { id: '88888888-8888-4888-8888-888888888888', kind: 'user', name: 'Anna' }
  const thread = {
    anchor: { from: 4, quoteAfter: '', quoteBefore: '', selectedText: 'Abgabe', to: 10 },
    author,
    can_delete: false,
    can_resolve: false,
    created_at: now - 60,
    deleted_at: null,
    document_id: access.document.id,
    generation: 1,
    id: '99999999-9999-4999-8999-999999999999',
    messages: [{
      author,
      body_markdown: COMMENT,
      can_delete: false,
      can_edit: false,
      created_at: now - 60,
      deleted_at: null,
      edited_at: null,
      id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa',
      mentions: [],
      revision: 1,
    }],
    quote: 'Abgabe',
    resolved_at: null,
    resolved_by: null,
    revision: 1,
    status: 'open',
    updated_at: now - 60,
  }
  let describeStatus = 200
  let sessionStatus = 200
  let collaborationStatus: number | null = null
  const held: Route[] = []
  const guestRequests: Array<{ at: number; url: string }> = []
  page.on('request', (request) => {
    if (request.url().includes('/v1/editor/guest/')) {
      guestRequests.push({ at: Date.now(), url: request.url() })
    }
  })
  const refusal = (status: number) => ({
    json: { error: { message: 'Refused by the fixture.', reason: 'fixture_refusal', type: 'fixture' } },
    status,
  })
  const answerCollaboration = async (route: Route, status: number) => {
    try {
      await route.fulfill(refusal(status))
    } catch {
      // The page dropped the request (an effect cleanup aborted it).
    }
  }
  await page.route('**/v1/editor/share-links/**', (route) => (
    describeStatus === 200 ? route.fulfill({ json: description }) : route.fulfill(refusal(describeStatus))
  ))
  await page.route('**/v1/editor/guest/session', (route) => (
    sessionStatus === 200 ? route.fulfill({ json: access }) : route.fulfill(refusal(sessionStatus))
  ))
  await page.route('**/v1/editor/guest/collaboration/session', async (route) => {
    if (collaborationStatus === null) {
      held.push(route)
      return
    }
    await answerCollaboration(route, collaborationStatus)
  })
  await page.route('**/v1/editor/guest/collaboration/comments**', (route) => route.fulfill({
    json: {
      current_revision: 1,
      data: [thread],
      has_more: false,
      last_read_revision: 1,
      object: 'list',
      participants: [author],
      revision: 1,
    },
  }))
  return {
    answerCollaboration: (status) => {
      collaborationStatus = status
      for (const route of held.splice(0)) void answerCollaboration(route, status)
    },
    guestRequestsSince: (since) => guestRequests.filter((entry) => entry.at > since).map((entry) => entry.url),
    setDescribeStatus: (status) => {
      describeStatus = status
    },
    setGuestId: (id) => {
      access.guest.id = id
    },
    setSessionStatus: (status) => {
      sessionStatus = status
    },
  }
}

async function openGuestDocument(page: Page): Promise<GuestApi> {
  const api = await guestApi(page)
  await page.goto(fixture)
  // The first load compiles the page; give it room once.
  await expect(page.getByRole('heading', { name: TITLE })).toBeVisible({ timeout: 30_000 })
  await expect(page.getByText(COMMENT)).toBeVisible()
  return api
}

/** Every unsent comment draft this browser keeps, as [key, value]. */
async function storedCommentDrafts(page: Page): Promise<Array<[string, string | null]>> {
  return page.evaluate((prefix) => (
    Object.keys(window.localStorage)
      .filter((key) => key.startsWith(prefix))
      .map((key): [string, string | null] => [key, window.localStorage.getItem(key)])
  ), DRAFT_PREFIX)
}

/** Types an unsent reply into the thread and waits until it is stored. */
async function typeUnsentReply(page: Page): Promise<void> {
  await page.getByText(COMMENT).click()
  await page.getByRole('textbox', { name: REPLY }).fill(DRAFT)
  await expect.poll(async () => (
    (await storedCommentDrafts(page)).map(([, value]) => value ?? '').join('\n')
  )).toContain(DRAFT)
}

async function expectAccessEnded(page: Page): Promise<void> {
  const alert = page.getByRole('alert')
  await expect(alert).toHaveCount(1, { timeout: 10_000 })
  await expect(alert).toContainText(ENDED_TITLE)
  await expect(page.getByText(TITLE)).toHaveCount(0)
  await expect(page.getByText(COMMENT)).toHaveCount(0)
  await expect(page.locator('.ProseMirror')).toHaveCount(0)
  await expect(page.getByRole('button', { name: REOPEN })).toBeVisible()
}

for (const [label, status] of [
  ['the guest session is gone (revoked link, expiry or a new password)', 401],
  ['the link was revoked or the document deleted', 404],
] as const) {
  test(`removes the document and its comments and shows one notice when ${label}`, async ({ page }, testInfo) => {
    const api = await openGuestDocument(page)

    api.answerCollaboration(status)

    await expectAccessEnded(page)
    const endedAt = Date.now()
    // Nothing of the document is requested once access has ended.
    await page.waitForTimeout(3_000)
    expect(api.guestRequestsSince(endedAt + 250)).toEqual([])
    if (testInfo.project.name === 'chromium') {
      await page.screenshot({ path: testInfo.outputPath(`guest-access-ended-${status}.png`), fullPage: true })
    }
  })
}

test('reopening a link that still exists leads to the password form', async ({ page }) => {
  const api = await openGuestDocument(page)
  api.answerCollaboration(401)
  await expectAccessEnded(page)

  api.setSessionStatus(401)
  await page.getByRole('button', { name: REOPEN }).click()

  await expect(page.getByPlaceholder('Passwort eingeben')).toBeVisible()
  await expect(page.getByRole('heading', { name: TITLE })).toBeVisible()
})

test('reopening a revoked link shows the invalid-link page', async ({ page }) => {
  const api = await openGuestDocument(page)
  api.answerCollaboration(404)
  await expectAccessEnded(page)

  api.setDescribeStatus(404)
  api.setSessionStatus(401)
  await page.getByRole('button', { name: REOPEN }).click()

  await expect(page.getByText('Dieser Freigabelink ist ungültig, abgelaufen oder wurde widerrufen.')).toBeVisible()
  await expect(page.getByText(ENDED_TITLE)).toHaveCount(0)
})

test('keeps the document while the connection only fails transiently', async ({ page }) => {
  const api = await openGuestDocument(page)

  api.answerCollaboration(503)

  await page.waitForTimeout(4_000)
  await expect(page.getByText(ENDED_TITLE)).toHaveCount(0)
  await expect(page.getByRole('heading', { name: TITLE })).toBeVisible()
  await expect(page.getByText(COMMENT)).toBeVisible()
})

test('keeps a guest\'s unsent reply across a reload of the same session', async ({ page }) => {
  await openGuestDocument(page)
  await typeUnsentReply(page)

  await page.reload()

  await expect(page.getByText(COMMENT)).toBeVisible({ timeout: 30_000 })
  await page.getByText(COMMENT).click()
  await expect(page.getByRole('textbox', { name: REPLY })).toHaveValue(DRAFT)
})

test('deletes a guest\'s unsent reply from this browser when access ends', async ({ page }) => {
  const api = await openGuestDocument(page)
  await typeUnsentReply(page)

  api.answerCollaboration(401)

  await expectAccessEnded(page)
  expect(await storedCommentDrafts(page)).toEqual([])
})

test('never shows a guest\'s unsent reply to the next guest in this browser', async ({ page }) => {
  const api = await openGuestDocument(page)
  await typeUnsentReply(page)

  // The page closed without seeing the end, and another guest opened a link
  // of the same document in this browser: the session is now theirs.
  api.setGuestId(NEXT_GUEST)
  await page.reload()

  await expect(page.getByText(COMMENT)).toBeVisible({ timeout: 30_000 })
  await page.getByText(COMMENT).click()
  await expect(page.getByRole('textbox', { name: REPLY })).toHaveValue('')
  expect(await storedCommentDrafts(page)).toEqual([])
})
