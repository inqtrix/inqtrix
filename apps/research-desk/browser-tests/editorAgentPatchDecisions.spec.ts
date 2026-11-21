import { expect, test, type Page } from '@playwright/test'

import { deskDocument, serveDesk, type DeskServer } from './editorDeskServer'

// An agent patch reaches a Markdown document as one row on the server. The
// editor mirrors its edits as the suggestions of one group, and a decision on
// any of them is the decision on the whole patch, which the server takes.
// Each test opens the document against a stand-in of the patch endpoints on
// the page and counts what reaches them.

const fixturePath = '/browser-tests/fixtures/editor-lifecycle.html?scenario=agent-patch-review'
const DOCUMENT_ID = 'agent-patch-document'
const PATCH_ID = 'pch_agent_review'
// The body the page opens with, as the server holds it.
const BODY = [
  '# Projektplan',
  '',
  'Der Bau dauert ca. sechs Wochen.',
  '',
  'Die Kosten liegen bei 18.000 Euro.',
  '',
  'Das Team trifft sich montags.',
].join('\n')
type PatchEdit = { find: string; id: string; note: string; text: string }

const EDITS: PatchEdit[] = [
  { find: 'ca. sechs Wochen', id: 'ed_weeks', note: 'Dauer präzisiert', text: 'etwa sechs Wochen' },
  { find: '18.000 Euro', id: 'ed_costs', note: 'Kosten aktualisiert', text: '21.500 Euro' },
  { find: 'montags', id: 'ed_day', note: 'Termin verschoben', text: 'dienstags' },
]
// The same patch, but only its first passage is still in the document, as
// when the others changed after the agent proposed them: the server applies
// the first edit and cannot place the other two.
const PARTLY_PLACEABLE_EDITS: PatchEdit[] = [
  { find: 'ca. sechs Wochen', id: 'ed_weeks', note: 'Dauer präzisiert', text: 'etwa sechs Wochen' },
  { find: '19.000 Euro', id: 'ed_costs', note: 'Kosten aktualisiert', text: '21.500 Euro' },
  { find: 'mittwochs', id: 'ed_day', note: 'Termin verschoben', text: 'dienstags' },
]
// A second agent patch on the same document, as a run that proposes twice
// leaves it: a row of its own, with edits of its own.
const TITLE_PATCH_ID = 'pch_agent_title'
const TITLE_EDITS: PatchEdit[] = [
  { find: 'Projektplan', id: 'ed_title', note: 'Titel geändert', text: 'Bauplan' },
]
// The three parts of the patch the page opens with, all open.
const OPEN_PARTS = 'pch_agent_review/ed_weeks:pending,pch_agent_review/ed_costs:pending,pch_agent_review/ed_day:pending'

type PatchStatus = 'accepted' | 'pending' | 'rejected'

/** An agent patch the server holds for the document. */
type ServedPatch = {
  /** The edits the server applied when it accepted the patch. */
  appliedEditIds: string[] | null
  edits: PatchEdit[]
  id: string
  status: PatchStatus
}

type PatchServer = {
  /** The bodies of the apply requests, in order. */
  applyBodies: unknown[]
  body: string
  /** Every decision that reached the server, in order: `apply` or `reject`. */
  decisions: string[]
  /** Answer every save with a server error. */
  failSaves: boolean
  /** While set, a request for the document's pending patches waits for it
   * before it is answered. */
  heldListing: Promise<void> | null
  /** The requests for the document's pending patches that arrived, answered
   * or not. */
  listRequests: number
  /** The patch ids each answered request for the document's pending patches
   * got. */
  listings: string[][]
  /** The agent patches the server holds for the document, newest first, as
   * its list returns them. */
  patches: ServedPatch[]
  revision: number
  /** The bodies the server took from saves of the document, in order. */
  saves: string[]
  /** Every writing request, in order: `save`, `apply` or `reject`. */
  writes: string[]
}

/** A pending agent patch on the document. */
function agentPatch(id = PATCH_ID, edits: PatchEdit[] = EDITS): ServedPatch {
  return { appliedEditIds: null, edits, id, status: 'pending' }
}

function serverDocument(server: PatchServer) {
  return {
    content_markdown: server.body,
    content_mode: 'markdown',
    created_at: 1_760_000_000,
    diff_anchor_markdown: null,
    diff_anchor_updated_at: null,
    folder_id: null,
    id: DOCUMENT_ID,
    metadata_revision: 1,
    revision: server.revision,
    source: 'blank',
    source_run_id: null,
    title: 'Projektplan.md',
    updated_at: 1_760_000_100,
  }
}

function patchWire(server: PatchServer, patch: ServedPatch) {
  return {
    applied_edit_ids: patch.appliedEditIds,
    applied_revision: patch.status === 'accepted' ? server.revision : null,
    created_at: 1_760_000_000,
    decided_at: patch.status === 'pending' ? null : 1_760_000_100,
    document_id: DOCUMENT_ID,
    document_revision: server.revision,
    edit_count: patch.edits.length,
    edits: patch.edits.map(({ find, id, note, text }) => ({
      find,
      id,
      note,
      position: 'replace',
      quote_after: '',
      quote_before: '',
      text,
    })),
    note: '',
    patch_id: patch.id,
    revision_before: 7,
    run_id: 'run_agent_review',
    source: 'agent',
    status: patch.status,
    summary: 'Projektplan aktualisiert',
    warnings: [],
  }
}

function decidedAnswer(status: PatchStatus) {
  return {
    json: {
      error: {
        message: 'The patch already has a different decision.',
        status,
        type: 'conflict',
      },
    },
    status: 409,
  }
}

// A patch, its apply and its reject: `/v1/editor/patches/{id}`, with
// `:apply` or `:reject` after the id.
const PATCH_PATH = /^\/v1\/editor\/patches\/([^/:]+)(:apply|:reject)?$/

/** Answers the patch endpoints of the document the way the service does,
 * for the agent patches given, newest first, and everything else on the API
 * with 404: nothing reaches a real server. */
async function servePatchEndpoints(
  page: Page,
  patches: ServedPatch[],
): Promise<PatchServer> {
  const server: PatchServer = {
    applyBodies: [],
    body: BODY,
    decisions: [],
    failSaves: false,
    heldListing: null,
    listRequests: 0,
    listings: [],
    patches,
    revision: 7,
    saves: [],
    writes: [],
  }
  await page.route(
    (url) => url.pathname.startsWith('/v1/') || url.pathname.startsWith('/api/'),
    (route) => route.fulfill({ json: {}, status: 404 }),
  )
  await page.route(
    (url) => url.pathname === `/v1/editor/documents/${DOCUMENT_ID}/patches`,
    async (route) => {
      server.listRequests += 1
      await server.heldListing
      const asked = new URL(route.request().url()).searchParams.get('status')
      const listed = server.patches.filter((patch) => asked === null || asked === patch.status)
      server.listings.push(listed.map((patch) => patch.id))
      await route.fulfill({
        json: { data: listed.map((patch) => ({ patch_id: patch.id, source: 'agent', status: patch.status })) },
      })
    },
  )
  await page.route(
    (url) => PATCH_PATH.test(url.pathname),
    async (route) => {
      const [, patchId, decision] = PATCH_PATH.exec(new URL(route.request().url()).pathname)!
      if (decision) {
        server.decisions.push(decision.slice(1))
        server.writes.push(decision.slice(1))
      }
      const patch = server.patches.find((item) => item.id === patchId)
      if (!patch) {
        await route.fulfill({ json: {}, status: 404 })
        return
      }
      if (decision === ':apply') {
        const body = route.request().postDataJSON() as { expected_revision?: number }
        server.applyBodies.push(body)
        if (patch.status !== 'pending') {
          await route.fulfill(decidedAnswer(patch.status))
          return
        }
        if (body.expected_revision !== server.revision) {
          await route.fulfill({
            json: { error: { current_revision: server.revision, message: 'Revision conflict.', type: 'conflict' } },
            status: 409,
          })
          return
        }
        // As the service applies a patch: every edit whose passage it finds.
        const applied: string[] = []
        for (const edit of patch.edits) {
          if (!server.body.includes(edit.find)) continue
          server.body = server.body.replace(edit.find, edit.text)
          applied.push(edit.id)
        }
        patch.appliedEditIds = applied
        server.revision += 1
        patch.status = 'accepted'
        await route.fulfill({
          json: { applied_edit_ids: applied, document_id: DOCUMENT_ID, revision: server.revision },
        })
        return
      }
      if (decision === ':reject') {
        if (patch.status !== 'pending' && patch.status !== 'rejected') {
          await route.fulfill(decidedAnswer(patch.status))
          return
        }
        patch.status = 'rejected'
        await route.fulfill({ json: patchWire(server, patch) })
        return
      }
      await route.fulfill({ json: patchWire(server, patch) })
    },
  )
  await page.route(
    (url) => url.pathname === `/v1/editor/documents/${DOCUMENT_ID}`,
    async (route) => {
      if (route.request().method() !== 'PUT') {
        await route.fulfill({ json: serverDocument(server) })
        return
      }
      // A save, as the service takes it: the save names the revision it
      // creates, which the service accepts only right after the one it holds.
      server.writes.push('save')
      const body = route.request().postDataJSON() as { content_markdown: string; revision: number }
      if (server.failSaves) {
        await route.fulfill({
          json: { error: { message: 'The document could not be stored.', type: 'server_error' } },
          status: 500,
        })
        return
      }
      if (body.revision !== server.revision + 1) {
        await route.fulfill({
          json: { error: { current_revision: server.revision, message: 'Revision conflict.', type: 'conflict' } },
          status: 409,
        })
        return
      }
      server.body = body.content_markdown
      server.revision = body.revision
      server.saves.push(server.body)
      await route.fulfill({ json: serverDocument(server) })
    },
  )
  return server
}

/** Holds the answers to the requests for the document's pending patches
 * until the function returned is called. */
function holdListing(server: PatchServer): () => void {
  let release = () => {}
  server.heldListing = new Promise<void>((resolve) => {
    release = resolve
  })
  return () => {
    server.heldListing = null
    release()
  }
}

/** Answers the editor assistant's instruction requests in order, one edit
 * each: the passage it finds and the text it proposes. */
async function serveInstructionAnswers(
  page: Page,
  edits: Array<{ find: string; text: string }>,
): Promise<{ requests: number }> {
  const served = { requests: 0 }
  await page.route(
    (url) => url.pathname === '/v1/editor/instruct',
    async (route) => {
      const edit = edits[served.requests]
      served.requests += 1
      if (!edit) {
        await route.fulfill({ json: {}, status: 404 })
        return
      }
      await route.fulfill({
        json: {
          assistant_message: 'Die Änderung ist vorbereitet.',
          edits: [{ ...edit, note: 'Geändert', position: 'replace', quote_after: '', quote_before: '' }],
          warnings: [],
        },
      })
    },
  )
  return served
}

/** The group of the document changes that holds the part proposing `text`. */
function groupOf(page: Page, text: string) {
  return page.getByTestId('review-changes')
    .locator('[role="button"]', { hasText: text })
    .locator('xpath=ancestor::div[.//button[normalize-space()="Alle übernehmen"]][1]')
}

function watchPageErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('pageerror', (error) => errors.push(error.message))
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(message.text())
  })
  return errors
}

/** Opens the document and waits until its agent patch shows as one group of
 * three suggestions, the last of them on a passage that changed since. */
async function openWithChangedPart(page: Page): Promise<void> {
  await page.goto(fixturePath)
  const statuses = page.getByTestId('suggestion-statuses')
  await expect(statuses).toHaveText(OPEN_PARTS)
  await page.getByTestId('suggestion-mark-stale').click()
  await expect(statuses).toHaveText('pch_agent_review/ed_weeks:pending,pch_agent_review/ed_costs:pending,pch_agent_review/ed_day:stale')
  await expect(page.getByTestId('review-changes').locator('[data-suggestion-stale]')).toHaveCount(1)
}

/** Opens the document again, as a reload does, and waits for the server's
 * answer on its pending patches. The page starts from its own copy of the
 * document; what counts is which patches the server still lists. */
async function reopen(page: Page, server: PatchServer): Promise<void> {
  const listings = server.listings.length
  await page.reload()
  await expect.poll(() => server.listings.length).toBeGreaterThan(listings)
}

type AcceptWay = 'all at once' | 'through one part'

/** Accepts the agent patch on its card: all parts at once, or through its
 * first part. */
async function acceptPatch(page: Page, way: AcceptWay): Promise<void> {
  const changes = page.getByTestId('review-changes')
  if (way === 'all at once') {
    await changes.getByRole('button', { exact: true, name: 'Alle übernehmen' }).click()
    return
  }
  await changes
    .locator('[role="button"]', { hasText: 'etwa sechs Wochen' })
    .getByRole('button', { exact: true, name: 'Übernehmen' })
    .click()
}

/** Opens the document, waits for the agent patch's three open parts, and
 * types at the end of the heading, a passage no edit of the patch touches.
 * Nothing saves what was typed. */
async function openAndType(page: Page, path = fixturePath): Promise<void> {
  await page.goto(path)
  await expect(page.getByTestId('suggestion-statuses')).toHaveText(OPEN_PARTS)
  const editor = page.getByTestId('review-surface').locator('.ProseMirror')
  await editor.locator('h1').click()
  await page.keyboard.press('End')
  await page.keyboard.type(' 2026')
  await expect(editor).toContainText('Projektplan 2026')
}

/** The console errors besides the browser's own report of a request the
 * test refused with `status`. */
function errorsBesidesRefusal(errors: string[], status: number): string[] {
  return errors.filter((message) => !new RegExp(`\\b${status}\\b`).test(message))
}

test.beforeEach(async ({ context }) => {
  // The page reads its labels in German; the texts of failed requests follow
  // the app language, which this states instead of inheriting the English
  // first-render fallback.
  await context.addInitScript(() => {
    window.localStorage.setItem('inqtrix.researchDesk.locale', 'de')
  })
})

test('accepting all parts of an agent patch applies the patch once on the server, and the editor applies none of them itself', async ({ page }, testInfo) => {
  const errors = watchPageErrors(page)
  const patch = agentPatch()
  const server = await servePatchEndpoints(page, [patch])
  await openWithChangedPart(page)
  const statuses = page.getByTestId('suggestion-statuses')
  const editor = page.getByTestId('review-surface').locator('.ProseMirror')
  const bodyChanges = page.getByTestId('editor-body-changes')
  const ownBodies = await bodyChanges.textContent()

  await page.getByTestId('review-changes').getByRole('button', { exact: true, name: 'Alle übernehmen' }).click()

  // The server applied the patch as a whole, also the passage of the changed
  // part, which changed only on this page: every part is closed as applied.
  await expect(statuses).toHaveText('pch_agent_review/ed_weeks:accepted,pch_agent_review/ed_costs:accepted,pch_agent_review/ed_day:accepted')
  // The document was saved first, and the patch applied on the revision that
  // save returned.
  expect(server.writes).toEqual(['save', 'apply'])
  expect(server.applyBodies).toEqual([{ expected_revision: 8 }])
  expect(patch.appliedEditIds).toEqual(['ed_weeks', 'ed_costs', 'ed_day'])
  // No part is left to decide, so no reject can reach the decided patch.
  await expect(page.getByTestId('review-changes').getByRole('button')).toHaveCount(0)
  // The body is the one the server applied, at its revision.
  await expect(page.getByTestId('document-revision')).toHaveText('9')
  for (const line of [
    'Der Bau dauert etwa sechs Wochen.',
    'Die Kosten liegen bei 21.500 Euro.',
    'Das Team trifft sich dienstags.',
  ]) {
    await expect(editor).toContainText(line)
  }
  // The editor applied no part itself.
  await expect(bodyChanges).toHaveText(ownBodies ?? '')
  await expect(page.locator('[data-suggestion-error]')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('agent-patch-accepted.png') })

  // The server row is decided: opening the document again brings no group back.
  expect(patch.status).toBe('accepted')
  await reopen(page, server)
  expect(server.listings.at(-1)).toEqual([])
  await expect(statuses).toHaveText('')
  expect(server.decisions).toEqual(['apply'])
  expect(errors).toEqual([])
})

for (const way of ['all at once', 'through one part'] as const) {
  test(`accepting an agent patch ${way} closes the parts the server could not place as rejected, the changed part included`, async ({ page }) => {
    const errors = watchPageErrors(page)
    const patch = agentPatch(PATCH_ID, PARTLY_PLACEABLE_EDITS)
    const server = await servePatchEndpoints(page, [patch])
    await openWithChangedPart(page)
    const statuses = page.getByTestId('suggestion-statuses')
    const changes = page.getByTestId('review-changes')

    await acceptPatch(page, way)

    // Only the part the server placed is accepted; the open and the changed
    // part it could not place end with the patch.
    await expect(statuses).toHaveText('pch_agent_review/ed_weeks:accepted,pch_agent_review/ed_costs:rejected,pch_agent_review/ed_day:rejected')
    expect(server.writes).toEqual(['save', 'apply'])
    expect(patch.appliedEditIds).toEqual(['ed_weeks'])
    // No part is left to decide, so no reject can reach the decided patch.
    await expect(changes.getByRole('button')).toHaveCount(0)
    await expect(page.getByTestId('document-revision')).toHaveText('9')
    const editor = page.getByTestId('review-surface').locator('.ProseMirror')
    await expect(editor).toContainText('Der Bau dauert etwa sechs Wochen.')
    await expect(editor).toContainText('Die Kosten liegen bei 18.000 Euro.')
    await expect(editor).toContainText('Das Team trifft sich montags.')
    await expect(page.locator('[data-suggestion-error]')).toHaveCount(0)
    expect(server.decisions).toEqual(['apply'])
    expect(errors).toEqual([])
  })
}

// What was typed before an agent patch is accepted is not on the server yet,
// and the server applies the patch to what it holds. The document is saved
// first, and the patch applied on the revision that save returned.

for (const way of ['all at once', 'through one part'] as const) {
  test(`accepting an agent patch ${way} saves what was typed first and applies the patch on top of it, once`, async ({ page }) => {
    const errors = watchPageErrors(page)
    const server = await servePatchEndpoints(page, [agentPatch()])
    await openAndType(page)

    await acceptPatch(page, way)

    await expect(page.getByTestId('suggestion-statuses'))
      .toHaveText('pch_agent_review/ed_weeks:accepted,pch_agent_review/ed_costs:accepted,pch_agent_review/ed_day:accepted')
    // One save of what was typed, then one apply on the revision it returned.
    expect(server.writes).toEqual(['save', 'apply'])
    expect(server.saves).toHaveLength(1)
    expect(server.saves[0]).toContain('# Projektplan 2026')
    expect(server.applyBodies).toEqual([{ expected_revision: 8 }])
    // What was typed and what the patch changed are both in the text.
    await expect(page.getByTestId('document-revision')).toHaveText('9')
    const editor = page.getByTestId('review-surface').locator('.ProseMirror')
    for (const line of [
      'Projektplan 2026',
      'Der Bau dauert etwa sechs Wochen.',
      'Die Kosten liegen bei 21.500 Euro.',
      'Das Team trifft sich dienstags.',
    ]) {
      await expect(editor).toContainText(line)
    }
    expect(server.body).toContain('# Projektplan 2026')
    await expect(page.locator('[data-suggestion-error]')).toHaveCount(0)
    expect(errors).toEqual([])
  })
}

// The desk's own sync saves the document before an agent patch is applied
// and merges the service's text after it. These cases run the real sync
// hook over a stand-in of the editor endpoints: another writer, typing while
// the save or the apply runs, a conversion elsewhere.

const DESK_PATH = '/browser-tests/fixtures/editor-concurrent-writers.html'
const DESK_DOCUMENT_ID = 'doc-plan'
const DESK_TEXT = '# Plan\n\nAbsatz eins.\n\nAbsatz zwei.\n\nAbsatz drei.'
const DESK_EDIT: PatchEdit = {
  find: 'Absatz drei.',
  id: 'ed_three',
  note: 'Satz ergänzt',
  text: 'Absatz drei, vom Agenten.',
}
const DESK_PATCH_PUT = `PUT /v1/editor/documents/${DESK_DOCUMENT_ID}`

/** Opens the desk on a document with one pending agent patch. */
async function openDeskWithPatch(page: Page): Promise<DeskServer> {
  const server = await serveDesk(page, [deskDocument(DESK_DOCUMENT_ID, 'Plan.md', DESK_TEXT)])
  server.patches.push({
    appliedEditIds: null,
    documentId: DESK_DOCUMENT_ID,
    edits: [DESK_EDIT],
    id: 'pch_desk',
    status: 'pending',
  })
  await page.goto(DESK_PATH)
  await page.locator('[data-testid="tree-item"]').first().click()
  await expect(page.getByTestId('suggestion-statuses')).toHaveText('ed_three:pending')
  return server
}

function deskEditor(page: Page) {
  return page.getByTestId('surface').locator('.ProseMirror')
}

async function typeInDesk(page: Page, paragraph: string, text: string): Promise<void> {
  await deskEditor(page).locator('p', { hasText: paragraph }).click()
  await page.keyboard.press('End')
  await page.keyboard.type(text)
}

async function acceptDeskPatch(page: Page): Promise<void> {
  await page.getByTestId('review-changes').getByRole('button', { exact: true, name: 'Alle übernehmen' }).click()
}

test('an agent patch is applied on top of another writer\'s version when the save before it lost, and what was typed stays', async ({ page }, testInfo) => {
  const errors = watchPageErrors(page)
  const server = await openDeskWithPatch(page)
  await typeInDesk(page, 'Absatz eins.', ' hier')
  // Another writer saved the document meanwhile, in another paragraph.
  server.writeElsewhere(DESK_DOCUMENT_ID, DESK_TEXT.replace('Absatz zwei.', 'Absatz zwei, dort.'))

  await acceptDeskPatch(page)

  await expect(page.getByTestId('suggestion-statuses')).toHaveText('ed_three:accepted')
  // The first save lost, the merged record was saved once more, and the
  // patch was applied on the revision that save returned.
  expect(server.refusals).toEqual([`${DESK_DOCUMENT_ID}: revision`])
  expect(server.saves.map((save) => save.revision)).toEqual([3])
  expect(server.requestsOf('POST')).toEqual(['POST /v1/editor/patches/pch_desk:apply'])
  const stored = server.documents.get(DESK_DOCUMENT_ID)!
  expect(stored.revision).toBe(4)
  for (const line of ['Absatz eins. hier', 'Absatz zwei, dort.', 'Absatz drei, vom Agenten.']) {
    expect(stored.text).toContain(line)
    await expect(deskEditor(page)).toContainText(line)
  }
  await expect(page.locator('[data-suggestion-error]')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('agent-patch-after-other-writer.png') })
  expect(errorsBesidesRefusal(errors, 409)).toEqual([])
})

test('typing while the save before an agent patch runs keeps the typing and the patch', async ({ page }) => {
  const server = await openDeskWithPatch(page)
  await typeInDesk(page, 'Absatz eins.', ' a')
  const release = server.hold((request) => request === DESK_PATCH_PUT)

  await acceptDeskPatch(page)
  await expect.poll(() => server.requestsOf('PUT').length).toBe(1)
  // A second click on the same spot within half a second is a double
  // click to the editor, which selects a word instead of placing the caret.
  await page.waitForTimeout(600)
  await typeInDesk(page, 'Absatz eins. a', 'b')
  await expect(deskEditor(page)).toContainText('Absatz eins. ab')
  release()

  await expect(page.getByTestId('suggestion-statuses')).toHaveText('ed_three:accepted')
  await expect(deskEditor(page)).toContainText('Absatz eins. ab')
  await expect(deskEditor(page)).toContainText('Absatz drei, vom Agenten.')
  // What was typed during the save is saved on top of the patch.
  await expect.poll(() => server.documents.get(DESK_DOCUMENT_ID)?.text).toContain('Absatz eins. ab')
  expect(server.documents.get(DESK_DOCUMENT_ID)?.text).toContain('Absatz drei, vom Agenten.')
  expect(server.requestsOf('DELETE')).toEqual([])
})

test('typing in another paragraph while the service applies an agent patch keeps both', async ({ page }) => {
  const server = await openDeskWithPatch(page)
  const release = server.hold((request) => request.endsWith(':apply'))

  await acceptDeskPatch(page)
  await expect.poll(() => server.requestsOf('POST').length).toBe(1)
  await typeInDesk(page, 'Absatz eins.', ' hier')
  release()

  await expect(page.getByTestId('suggestion-statuses')).toHaveText('ed_three:accepted')
  await expect(deskEditor(page)).toContainText('Absatz eins. hier')
  await expect(deskEditor(page)).toContainText('Absatz drei, vom Agenten.')
  await expect.poll(() => server.documents.get(DESK_DOCUMENT_ID)?.text).toContain('Absatz eins. hier')
  expect(server.documents.get(DESK_DOCUMENT_ID)?.text).toContain('Absatz drei, vom Agenten.')
  await expect(page.getByTestId('tree-item')).toHaveCount(1)
})

test('typing in the patched paragraph while the service applies an agent patch keeps the typing in a copy, and the part reads accepted', async ({ page }) => {
  const server = await openDeskWithPatch(page)
  const release = server.hold((request) => request.endsWith(':apply'))

  await acceptDeskPatch(page)
  await expect.poll(() => server.requestsOf('POST').length).toBe(1)
  await typeInDesk(page, 'Absatz drei.', ' x')
  release()

  await expect(page.getByTestId('suggestion-statuses')).toHaveText('ed_three:accepted')
  await expect.poll(() => server.saves.filter((save) => save.id !== DESK_DOCUMENT_ID).length).toBe(1)
  const copy = server.saves.find((save) => save.id !== DESK_DOCUMENT_ID)!
  expect(copy.text).toContain('Absatz drei. x')
  await expect(deskEditor(page)).toContainText('Absatz drei, vom Agenten.')
  await expect(deskEditor(page)).not.toContainText('Absatz drei. x')
  expect(server.documents.get(DESK_DOCUMENT_ID)?.text).toContain('Absatz drei, vom Agenten.')
  await expect(page.locator('[data-editor-conflict-copy="original"]')).toBeVisible()
  expect(server.requestsOf('DELETE')).toEqual([])
})

test('accepting an agent patch of a document converted elsewhere says so and applies nothing', async ({ page }) => {
  const errors = watchPageErrors(page)
  const server = await openDeskWithPatch(page)
  server.convertElsewhere(DESK_DOCUMENT_ID)

  await acceptDeskPatch(page)

  await expect(page.getByTestId('instruction-feedback')).toHaveText(
    'Das Dokument ist inzwischen im Modus Zusammenarbeit. Der Vorschlag wurde nicht übernommen.',
  )
  await expect(page.getByTestId('document-mode')).toHaveText('collaboration')
  expect(server.requestsOf('POST')).toEqual([])
  expect(server.patches[0].status).toBe('pending')
  expect(errorsBesidesRefusal(errors, 409)).toEqual([])
})

test('an agent patch is not applied when the save before it fails, and what was typed stays', async ({ page }) => {
  const errors = watchPageErrors(page)
  const server = await servePatchEndpoints(page, [agentPatch()])
  await openAndType(page)
  server.failSaves = true

  await acceptPatch(page, 'through one part')

  const refusals = page.getByTestId('review-changes').locator('[data-suggestion-error]')
  await expect(refusals).toHaveCount(3)
  await expect(refusals.first()).toHaveText('Auf dem Server ist ein Fehler aufgetreten.')
  expect(server.writes).toEqual(['save'])
  await expect(page.getByTestId('suggestion-statuses')).toHaveText(OPEN_PARTS)
  await expect(page.getByTestId('review-surface').locator('.ProseMirror')).toContainText('Projektplan 2026')
  await expect(page.getByTestId('document-revision')).toHaveText('7')
  expect(errorsBesidesRefusal(errors, 500)).toEqual([])
})

// Without project sync the desk has no immediate save. As for sharing, a
// patch is then accepted on the server only while the project has nothing
// unsaved.

test('without the immediate save, an agent patch is not applied while the project has unsaved changes, and what was typed stays', async ({ page }, testInfo) => {
  const errors = watchPageErrors(page)
  const server = await servePatchEndpoints(page, [agentPatch()])
  await openAndType(page, `${fixturePath}&save=none`)
  await expect(page.getByTestId('project-dirty')).toHaveText('dirty')

  await acceptPatch(page, 'all at once')

  const refusals = page.getByTestId('review-changes').locator('[data-suggestion-error]')
  await expect(refusals).toHaveCount(3)
  await expect(refusals.first()).toHaveText('Speichern Sie das Dokument, bevor Sie den Vorschlag übernehmen.')
  expect(server.writes).toEqual([])
  await expect(page.getByTestId('suggestion-statuses')).toHaveText(OPEN_PARTS)
  await expect(page.getByTestId('review-surface').locator('.ProseMirror')).toContainText('Projektplan 2026')
  await expect(page.getByTestId('document-revision')).toHaveText('7')
  await page.screenshot({ path: testInfo.outputPath('agent-patch-save-first.png') })
  expect(errors).toEqual([])
})

test('without the immediate save, an agent patch of a saved project is applied on the server as before', async ({ page }) => {
  const errors = watchPageErrors(page)
  const server = await servePatchEndpoints(page, [agentPatch()])
  await page.goto(`${fixturePath}&save=none`)
  const statuses = page.getByTestId('suggestion-statuses')
  await expect(statuses).toHaveText(OPEN_PARTS)
  await page.getByTestId('project-save').click()
  await expect(page.getByTestId('project-dirty')).toHaveText('clean')

  await acceptPatch(page, 'all at once')

  await expect(statuses).toHaveText('pch_agent_review/ed_weeks:accepted,pch_agent_review/ed_costs:accepted,pch_agent_review/ed_day:accepted')
  expect(server.writes).toEqual(['apply'])
  expect(server.applyBodies).toEqual([{ expected_revision: 7 }])
  await expect(page.getByTestId('document-revision')).toHaveText('8')
  await expect(page.locator('[data-suggestion-error]')).toHaveCount(0)
  expect(errors).toEqual([])
})

test('rejecting all parts of an agent patch rejects the patch once on the server, its changed part included', async ({ page }, testInfo) => {
  const errors = watchPageErrors(page)
  const patch = agentPatch()
  const server = await servePatchEndpoints(page, [patch])
  await openWithChangedPart(page)
  const statuses = page.getByTestId('suggestion-statuses')
  const changes = page.getByTestId('review-changes')

  await changes.getByRole('button', { exact: true, name: 'Alle verwerfen' }).click()

  await expect(statuses).toHaveText('pch_agent_review/ed_weeks:rejected,pch_agent_review/ed_costs:rejected,pch_agent_review/ed_day:rejected')
  expect(server.decisions).toEqual(['reject'])
  await expect(changes.getByRole('button', { exact: true, name: 'Verwerfen' })).toHaveCount(0)
  // No part reached the body.
  await expect(page.getByTestId('document-revision')).toHaveText('7')
  const editor = page.getByTestId('review-surface').locator('.ProseMirror')
  await expect(editor).toContainText('Der Bau dauert ca. sechs Wochen.')
  await expect(editor).toContainText('Das Team trifft sich montags.')
  await expect(page.locator('[data-suggestion-error]')).toHaveCount(0)
  await page.screenshot({ path: testInfo.outputPath('agent-patch-rejected.png') })

  // The server row is decided: opening the document again brings no group back.
  expect(patch.status).toBe('rejected')
  await reopen(page, server)
  expect(server.listings.at(-1)).toEqual([])
  await expect(statuses).toHaveText('')
  expect(server.decisions).toEqual(['reject'])
  expect(errors).toEqual([])
})

test('rejecting the changed part of an agent patch rejects the whole patch once on the server', async ({ page }) => {
  const errors = watchPageErrors(page)
  const patch = agentPatch()
  const server = await servePatchEndpoints(page, [patch])
  await openWithChangedPart(page)
  const statuses = page.getByTestId('suggestion-statuses')
  const changedPart = page.getByTestId('review-changes').locator('[role="button"]', { hasText: 'dienstags' })
  await expect(changedPart.locator('[data-suggestion-stale]')).toBeVisible()

  await changedPart.getByRole('button', { exact: true, name: 'Verwerfen' }).click()

  await expect(statuses).toHaveText('pch_agent_review/ed_weeks:rejected,pch_agent_review/ed_costs:rejected,pch_agent_review/ed_day:rejected')
  expect(server.decisions).toEqual(['reject'])
  expect(patch.status).toBe('rejected')
  await expect(page.getByTestId('document-revision')).toHaveText('7')
  await expect(page.locator('[data-suggestion-error]')).toHaveCount(0)
  expect(errors).toEqual([])
})

test('the assistant\'s own suggestions are still accepted all at once in the editor, without a patch request', async ({ page }) => {
  const errors = watchPageErrors(page)
  const server = await servePatchEndpoints(page, [])
  await page.goto(fixturePath)
  // The document asked the server, which holds no agent patch for it.
  await expect.poll(() => server.listings).toEqual([[]])
  const statuses = page.getByTestId('suggestion-statuses')
  await page.getByTestId('assistant-seed').click()
  await expect(statuses).toHaveText('assistant:pending,assistant:pending')
  const bodyChanges = page.getByTestId('editor-body-changes')
  const ownBodies = Number(await bodyChanges.textContent())

  await page.getByTestId('review-changes').getByRole('button', { exact: true, name: 'Alle übernehmen' }).click()

  await expect(statuses).toHaveText('assistant:accepted,assistant:accepted')
  const editor = page.getByTestId('review-surface').locator('.ProseMirror')
  await expect(editor).toContainText('Bauplan')
  await expect(editor).toContainText('Das Team trifft sich freitags.')
  // Applied in the editor, at the revision the document had.
  expect(Number(await bodyChanges.textContent())).toBeGreaterThan(ownBodies)
  await expect(page.getByTestId('document-revision')).toHaveText('7')
  expect(server.decisions).toEqual([])
  expect(errors).toEqual([])
})

test('the assistant\'s own suggestions are still rejected all at once, a changed one included, without a patch request', async ({ page }) => {
  const errors = watchPageErrors(page)
  const server = await servePatchEndpoints(page, [])
  await page.goto(fixturePath)
  await expect.poll(() => server.listings).toEqual([[]])
  const statuses = page.getByTestId('suggestion-statuses')
  await page.getByTestId('assistant-seed').click()
  await expect(statuses).toHaveText('assistant:pending,assistant:pending')
  await page.getByTestId('suggestion-mark-stale').click()
  await expect(statuses).toHaveText('assistant:pending,assistant:stale')
  const changes = page.getByTestId('review-changes')

  await changes.getByRole('button', { exact: true, name: 'Alle verwerfen' }).click()

  await expect(statuses).toHaveText('assistant:rejected,assistant:rejected')
  await expect(changes.getByRole('button', { exact: true, name: 'Verwerfen' })).toHaveCount(0)
  const editor = page.getByTestId('review-surface').locator('.ProseMirror')
  await expect(editor).toContainText('Projektplan')
  await expect(editor).toContainText('Das Team trifft sich montags.')
  expect(server.decisions).toEqual([])
  expect(errors).toEqual([])
})

// A run that proposes twice leaves two agent patches on the document, and
// the editor's own instruction runs add suggestions beside them. Each agent
// patch stays a group of its own until the server decides it; a new
// instruction replaces only the open suggestions of the instruction run
// before it.

test('two open agent patches of a document show as two groups, and each is decided on the server on its own', async ({ page }, testInfo) => {
  const errors = watchPageErrors(page)
  const older = agentPatch()
  const newer = agentPatch(TITLE_PATCH_ID, TITLE_EDITS)
  // The server lists the newest patch first.
  const server = await servePatchEndpoints(page, [newer, older])
  await page.goto(fixturePath)
  const statuses = page.getByTestId('suggestion-statuses')
  const changes = page.getByTestId('review-changes')

  await expect(statuses).toHaveText(`pch_agent_title/ed_title:pending,${OPEN_PARTS}`)
  await expect(changes.getByRole('button', { exact: true, name: 'Alle übernehmen' })).toHaveCount(2)
  await changes.screenshot({ path: testInfo.outputPath('agent-patches-two-groups.png') })

  await groupOf(page, 'etwa sechs Wochen').getByRole('button', { exact: true, name: 'Alle übernehmen' }).click()
  await expect(statuses).toHaveText(
    'pch_agent_title/ed_title:pending,'
    + 'pch_agent_review/ed_weeks:accepted,pch_agent_review/ed_costs:accepted,pch_agent_review/ed_day:accepted',
  )
  await groupOf(page, 'Bauplan').getByRole('button', { exact: true, name: 'Alle verwerfen' }).click()

  await expect(statuses).toHaveText(
    'pch_agent_title/ed_title:rejected,'
    + 'pch_agent_review/ed_weeks:accepted,pch_agent_review/ed_costs:accepted,pch_agent_review/ed_day:accepted',
  )
  // Each patch was decided once, on its own row.
  expect(server.writes).toEqual(['save', 'apply', 'reject'])
  expect(older.status).toBe('accepted')
  expect(older.appliedEditIds).toEqual(['ed_weeks', 'ed_costs', 'ed_day'])
  expect(newer.status).toBe('rejected')
  await expect(changes.getByRole('button')).toHaveCount(0)
  const editor = page.getByTestId('review-surface').locator('.ProseMirror')
  await expect(editor).toContainText('Projektplan')
  await expect(editor).toContainText('Der Bau dauert etwa sechs Wochen.')
  await expect(page.locator('[data-suggestion-error]')).toHaveCount(0)

  // Both rows are decided: opening the document again brings no group back.
  await reopen(page, server)
  expect(server.listings.at(-1)).toEqual([])
  await expect(statuses).toHaveText('')
  expect(errors).toEqual([])
})

test('an instruction replaces the open suggestions of the instruction before it and leaves an open agent patch as it is', async ({ page }, testInfo) => {
  const errors = watchPageErrors(page)
  const patch = agentPatch()
  const server = await servePatchEndpoints(page, [patch])
  const instructions = await serveInstructionAnswers(page, [
    { find: 'Projektplan', text: 'Bauplan' },
    { find: 'Das Team', text: 'Die Gruppe' },
  ])
  await page.goto(fixturePath)
  const statuses = page.getByTestId('suggestion-statuses')
  const replaceable = page.getByTestId('replaceable-count')
  await expect(statuses).toHaveText(OPEN_PARTS)

  await page.getByTestId('instruction-run').click()
  await expect(statuses).toHaveText(`${OPEN_PARTS},assistant:pending`)
  await expect(replaceable).toHaveText('1')

  await page.getByTestId('instruction-run').click()

  // Of the two instructions, only the last one's suggestion is open; the
  // agent patch's three parts are open as before.
  await expect(statuses).toHaveText(`${OPEN_PARTS},assistant:rejected,assistant:pending`)
  await expect(replaceable).toHaveText('1')
  expect(instructions.requests).toBe(2)
  // Nothing reached the agent patch's row.
  expect(server.writes).toEqual([])
  expect(patch.status).toBe('pending')
  const changes = page.getByTestId('review-changes')
  await expect(changes.getByRole('button', { exact: true, name: 'Alle übernehmen' })).toHaveCount(2)
  await changes.screenshot({ path: testInfo.outputPath('agent-patch-beside-instruction.png') })
  expect(errors).toEqual([])
})

test('an agent patch mirrored after an instruction leaves the instruction\'s open suggestions as they are', async ({ page }) => {
  const errors = watchPageErrors(page)
  const server = await servePatchEndpoints(page, [agentPatch()])
  const releaseListing = holdListing(server)
  await page.goto(fixturePath)
  // The document asked for its pending patches, and the answer waits.
  await expect.poll(() => server.listRequests).toBe(1)
  expect(server.listings).toEqual([])
  const statuses = page.getByTestId('suggestion-statuses')
  await page.getByTestId('assistant-seed').click()
  await expect(statuses).toHaveText('assistant:pending,assistant:pending')

  releaseListing()

  await expect(statuses).toHaveText(`assistant:pending,assistant:pending,${OPEN_PARTS}`)
  expect(server.listings).toEqual([[PATCH_ID]])
  await expect(page.getByTestId('review-changes').getByRole('button', { exact: true, name: 'Alle übernehmen' }))
    .toHaveCount(2)
  expect(errors).toEqual([])
})

test('the count of what the next instruction replaces leaves an open agent patch out', async ({ page }) => {
  const errors = watchPageErrors(page)
  await servePatchEndpoints(page, [agentPatch()])
  await page.goto(fixturePath)
  const statuses = page.getByTestId('suggestion-statuses')
  const replaceable = page.getByTestId('replaceable-count')
  await expect(statuses).toHaveText(OPEN_PARTS)
  await expect(replaceable).toHaveText('0')

  await page.getByTestId('assistant-seed').click()

  await expect(statuses).toHaveText(`${OPEN_PARTS},assistant:pending,assistant:pending`)
  await expect(replaceable).toHaveText('2')
  expect(errors).toEqual([])
})
