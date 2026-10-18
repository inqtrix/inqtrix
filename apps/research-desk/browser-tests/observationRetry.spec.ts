import { expect, test, type Page, type Route } from '@playwright/test'

const fixturePath = '/browser-tests/fixtures/observation-retry.html'
const NOW = 1_791_700_000
const ASSET_ID = 'asset-uploading'

type Answer = 'fail' | 'running'

/** A status endpoint that holds every read until the test answers it, so
 * the page clock moves only while nothing is in flight. Reads past the
 * planned answers fail, as they do while the server is unreachable. */
type StatusServer = {
  answers: Answer[]
  held: Array<() => Promise<void>>
  reads: number
}

function statusServer(answers: Answer[]): StatusServer {
  return { answers, held: [], reads: 0 }
}

async function holdRead(route: Route, server: StatusServer, running: unknown) {
  server.reads += 1
  const read = server.reads
  const answer = server.answers[read - 1] ?? 'fail'
  await new Promise<void>((resolve) => {
    server.held.push(async () => {
      await (answer === 'running'
        ? route.fulfill({ json: running })
        : route.fulfill({
            json: {
              error: {
                message: `status read ${read} failed`,
                type: read % 2 === 1 ? 'service_unavailable' : 'server_error',
              },
            },
            status: read % 2 === 1 ? 503 : 500,
          }))
      resolve()
    })
  })
}

/** What the page shows for a failed read, in the reader's words: odd reads
 * fail with 503, even reads with 500, so two failures in a row read
 * differently and each answer is seen in the page. */
function failedRead(read: number): string {
  return read % 2 === 1
    ? 'The service is unavailable right now. Please try again shortly.'
    : 'An error occurred on the server.'
}

/** Routes the fixture's API: the status path answers through the server,
 * the other paths with fixed bodies, everything else 404. */
async function serve(
  page: Page,
  server: StatusServer,
  statusPath: string,
  running: unknown,
  bodies: Record<string, unknown> = {},
) {
  await page.route(
    (url) => url.pathname.startsWith('/v1/') || url.pathname.startsWith('/api/'),
    async (route) => {
      const { pathname } = new URL(route.request().url())
      if (pathname === statusPath) {
        await holdRead(route, server, running)
      } else if (pathname in bodies) {
        await route.fulfill({ json: bodies[pathname] })
      } else {
        await route.fulfill({ json: {}, status: 404 })
      }
    },
  )
}

function sentReads(page: Page, statusPath: string): Promise<number> {
  return page.evaluate(
    (path) => (window.__requestPaths ?? []).filter((sent) => sent === path).length,
    statusPath,
  )
}

/** Opens the fixture with the page clock stopped and moves the clock in
 * small steps until the page sent its first status read. */
async function openLoop(page: Page, loop: string, statusPath: string) {
  await page.clock.install({ time: NOW * 1_000 })
  await page.clock.pauseAt(NOW * 1_000 + 1_000)
  await page.goto(`${fixturePath}?loop=${loop}`)
  await expect.poll(async () => {
    await page.clock.runFor(100)
    return sentReads(page, statusPath)
  }).toBe(1)
}

/** Answers the read the page waits on, then waits until the hook shows the
 * answer: from then on the page has its next read scheduled. */
async function answer(page: Page, server: StatusServer, shown: string) {
  await expect.poll(() => server.held.length).toBe(1)
  await server.held.shift()!()
  await expect(page.getByTestId('observation')).toHaveText(shown)
}

/** The next read goes out exactly `delayMs` after the last answer: not a
 * millisecond earlier, and at that millisecond. */
async function expectReadAfter(page: Page, statusPath: string, delayMs: number) {
  const sent = await sentReads(page, statusPath)
  await page.clock.runFor(delayMs - 1)
  expect(await sentReads(page, statusPath), `no read before ${delayMs} ms`).toBe(sent)
  await page.clock.runFor(1)
  expect(await sentReads(page, statusPath), `a read at ${delayMs} ms`).toBe(sent + 1)
}

async function expectReads(
  page: Page,
  server: StatusServer,
  statusPath: string,
  reads: ReadonlyArray<readonly [delayMs: number, shown: string]>,
) {
  for (const [delayMs, shown] of reads) {
    await expectReadAfter(page, statusPath, delayMs)
    await answer(page, server, shown)
  }
}

/** Ten minutes on, a stopped poll has not read again. */
async function expectStoppedAt(page: Page, statusPath: string, reads: number) {
  await expect(page.getByTestId('observation')).toHaveAttribute('data-stopped', 'true')
  await page.clock.runFor(10 * 60_000)
  expect(await sentReads(page, statusPath)).toBe(reads)
}

/** The waits between ten failed reads in a row, from the poll's base. */
const UPLOAD_FAILURE_WAITS = [1_500, 3_000, 6_000, 12_000, 24_000, 30_000, 30_000, 30_000, 30_000]
const RECEIPT_FAILURE_WAITS = [750, 1_500, 3_000, 6_000, 12_000, 24_000, 30_000, 30_000, 30_000]

/** Fails the first read and the nine after it, until the poll stops. */
async function failUntilStopped(
  page: Page,
  server: StatusServer,
  statusPath: string,
  waits: readonly number[],
) {
  await answer(page, server, failedRead(1))
  await expectReads(
    page,
    server,
    statusPath,
    waits.map((delayMs, index) => [delayMs, failedRead(index + 2)] as const),
  )
  await expectStoppedAt(page, statusPath, waits.length + 1)
}

/** Clicks without waiting for animation frames, which a stopped page clock
 * holds back. */
async function press(page: Page, name: string) {
  await page.getByRole('button', { name }).dispatchEvent('click')
}

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => {
    throw error
  })
})

const sections = (['temporary', 'library', 'project_sources'] as const).map((role) => ({
  created_at: NOW, id: `sec-${role}`, kind: role, semantic_role: role,
  title: role, updated_at: NOW,
}))

const asset = {
  created_at: NOW, file_name: 'vertrag.pdf', group_id: null, id: ASSET_ID,
  label: 'vertrag', mime_type: 'application/pdf', origin: 'upload', page_count: null,
  parse_status: 'failed', parse_warning: null, prepared_at: null,
  prepared_content_hash: null, prepared_parser_id: null, section_id: 'sec-library',
  server_file_id: 'file-1', size_bytes: 1_024, text_truncated: false,
  title: 'Vertrag', updated_at: NOW,
}

/** The hydrated library; the autosave writes the sections back once. */
const uploadLibrary = {
  '/v1/assets': { data: [asset], next_cursor: null },
  '/v1/assets/default-sections': { data: sections },
  '/v1/assets/groups': { data: [] },
  '/v1/assets/sections': { data: sections },
  [`/v1/assets/${ASSET_ID}`]: { ...asset, extracted_text: '', prepared_text: '' },
  ...Object.fromEntries(sections.map((section) => [`/v1/assets/sections/${section.id}`, section])),
}

const runningUploads = {
  data: [{
    asset_id: ASSET_ID, attempt: 1, created_at: NOW, error: null, file_id: 'file-1',
    finished_at: null, operation_id: 'upload-1', requires_bytes: false, retryable: true,
    stage: 'parsing', started_at: NOW, status: 'running',
  }],
  object: 'list',
}

test('an upload status poll that keeps failing waits longer each time and stops after ten failures in a row', async ({ page }) => {
  // The regression: a failed read retried every 1.5 s for as long as the
  // server stayed away, in every open tab.
  const uploads = '/v1/uploads'
  const server = statusServer(['fail', 'fail', 'fail', 'fail', 'running'])
  await serve(page, server, uploads, runningUploads, uploadLibrary)
  await openLoop(page, 'uploads', uploads)
  await answer(page, server, failedRead(1))

  await expectReads(page, server, uploads, [
    [1_500, failedRead(2)],
    [3_000, failedRead(3)],
    [6_000, failedRead(4)],
    [12_000, ''], // the upload is still running
    [1_500, failedRead(6)], // the normal cadence
    [1_500, failedRead(7)], // the count started over
    [3_000, failedRead(8)],
    [6_000, failedRead(9)],
    [12_000, failedRead(10)],
    [24_000, failedRead(11)],
    [30_000, failedRead(12)],
    [30_000, failedRead(13)],
    [30_000, failedRead(14)],
    [30_000, failedRead(15)], // the tenth failure in a row
  ])
  await expectStoppedAt(page, uploads, 15)

  // The browser is online again: the poll reads at once, with a fresh count.
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await expect(page.getByTestId('observation')).toHaveAttribute('data-stopped', 'false')
  await answer(page, server, failedRead(16))
  await expectReads(page, server, uploads, [[1_500, failedRead(17)]])
})

test('a stopped upload status poll starts again with a fresh count once an upload begins', async ({ page }) => {
  const uploads = '/v1/uploads'
  const server = statusServer([])
  const library: Record<string, unknown> = { ...uploadLibrary }
  await serve(page, server, uploads, runningUploads, library)
  await openLoop(page, 'uploads', uploads)
  await failUntilStopped(page, server, uploads, UPLOAD_FAILURE_WAITS)

  // The server lists the file as parsing: an upload runs, and its status
  // poll reads at once instead of waiting for a restart cue.
  library['/v1/assets'] = { data: [{ ...asset, upload_status: 'parsing' }], next_cursor: null }
  await press(page, 'Neu laden')
  await expect(page.getByTestId('observation')).toHaveAttribute('data-stopped', 'false')
  await answer(page, server, failedRead(11))
  await expectReads(page, server, uploads, [[1_500, failedRead(12)]])
})

const runningAssetDeletion = {
  asset_ids: ['asset-1'], error: null, operation_id: 'op-asset', stage: 'queued',
  status: 'running', target_id: 'asset-1', target_kind: 'asset',
}

const assetDeletionFeed = {
  '/v1/assets/deletion-operations': { data: [runningAssetDeletion], next_cursor: null },
}

test('an asset deletion receipt poll that keeps failing waits longer each time and stops after ten failures in a row', async ({ page }) => {
  const receipt = '/v1/deletion-operations/op-asset'
  const server = statusServer(['fail', 'fail', 'fail', 'running'])
  await serve(page, server, receipt, runningAssetDeletion, assetDeletionFeed)
  await openLoop(page, 'asset-deletion', receipt)
  await answer(page, server, failedRead(1))

  await expectReads(page, server, receipt, [
    [750, failedRead(2)],
    [1_500, failedRead(3)],
    [3_000, ''], // the deletion is still running
    [300, failedRead(5)], // the normal cadence
    [750, failedRead(6)], // the count started over
    [1_500, failedRead(7)],
    [3_000, failedRead(8)],
    [6_000, failedRead(9)],
    [12_000, failedRead(10)],
    [24_000, failedRead(11)],
    [30_000, failedRead(12)],
    [30_000, failedRead(13)],
    [30_000, failedRead(14)], // the tenth failure in a row
  ])
  await expectStoppedAt(page, receipt, 14)

  // The tab is visible again: the poll starts over with a fresh count.
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))
  await expect(page.getByTestId('observation')).toHaveAttribute('data-stopped', 'false')
  await expectReads(page, server, receipt, [
    [300, failedRead(15)],
    [750, failedRead(16)],
  ])
})

test('a stopped asset deletion receipt poll starts again with a fresh count once the rows change', async ({ page }) => {
  const receipt = '/v1/deletion-operations/op-asset'
  const server = statusServer([])
  await serve(page, server, receipt, runningAssetDeletion, assetDeletionFeed)
  await openLoop(page, 'asset-deletion', receipt)
  await failUntilStopped(page, server, receipt, RECEIPT_FAILURE_WAITS)

  // Another deletion changes the rows: the receipt poll starts over.
  await press(page, 'Zeilen ändern')
  await expect(page.getByTestId('observation')).toHaveAttribute('data-stopped', 'false')
  await expectReads(page, server, receipt, [
    [300, failedRead(11)],
    [750, failedRead(12)],
  ])
})

test('a stopped asset deletion receipt poll ends quietly once the deletion finished elsewhere', async ({ page }) => {
  const receipt = '/v1/deletion-operations/op-asset'
  const server = statusServer([])
  const feed: Record<string, unknown> = { ...assetDeletionFeed }
  await serve(page, server, receipt, runningAssetDeletion, feed)
  await openLoop(page, 'asset-deletion', receipt)
  await failUntilStopped(page, server, receipt, RECEIPT_FAILURE_WAITS)

  // The server is back, and its notice makes the view read the retained
  // receipts: the deletion is done, so nothing is stopped and nothing reads.
  feed['/v1/assets/deletion-operations'] = {
    data: [{ ...runningAssetDeletion, stage: 'blobs_removed', status: 'deleted' }],
    next_cursor: null,
  }
  await press(page, 'Löschstatus neu laden')
  await expect(page.getByTestId('observation')).toHaveAttribute('data-stopped', 'false')
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await page.clock.runFor(10 * 60_000)
  expect(await sentReads(page, receipt)).toBe(10)
})

test('a project switch forgets a stopped asset deletion receipt poll', async ({ page }) => {
  const receipt = '/v1/deletion-operations/op-asset'
  const server = statusServer([])
  const feed: Record<string, unknown> = { ...assetDeletionFeed }
  await serve(page, server, receipt, runningAssetDeletion, feed)
  await openLoop(page, 'asset-deletion', receipt)
  await failUntilStopped(page, server, receipt, RECEIPT_FAILURE_WAITS)

  // The other project has no deletion running: nothing is stopped there,
  // and the old project's poll never comes back.
  feed['/v1/assets/deletion-operations'] = { data: [], next_cursor: null }
  await press(page, 'Projekt wechseln')
  await expect(page.getByTestId('observation')).toHaveAttribute('data-stopped', 'false')
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await page.clock.runFor(10 * 60_000)
  expect(await sentReads(page, receipt)).toBe(10)
})

const runningSessionDeletion = {
  asset_ids: [], error: null, operation_id: 'op-session', stage: 'queued',
  status: 'running', target_id: 'session-1', target_kind: 'agent_session',
}

test('a session deletion receipt poll that keeps failing waits longer each time and stops after ten failures in a row', async ({ page }) => {
  const receipt = '/v1/deletion-operations/op-session'
  const server = statusServer(['fail', 'fail', 'running'])
  await serve(page, server, receipt, runningSessionDeletion)
  await openLoop(page, 'session-deletion', receipt)
  await answer(page, server, failedRead(1))

  await expectReads(page, server, receipt, [
    [750, failedRead(2)],
    [1_500, ''], // the deletion is still running
    [300, failedRead(4)], // the normal cadence
    [750, failedRead(5)], // the count started over
    [1_500, failedRead(6)],
    [3_000, failedRead(7)],
    [6_000, failedRead(8)],
    [12_000, failedRead(9)],
    [24_000, failedRead(10)],
    [30_000, failedRead(11)],
    [30_000, failedRead(12)],
    [30_000, failedRead(13)], // the tenth failure in a row
  ])
  await expectStoppedAt(page, receipt, 13)

  // The browser is online again: the poll starts over with a fresh count.
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await expect(page.getByTestId('observation')).toHaveAttribute('data-stopped', 'false')
  await expectReads(page, server, receipt, [
    [300, failedRead(14)],
    [750, failedRead(15)],
  ])
})

test('a stopped session deletion receipt poll starts again with a fresh count once the sessions change', async ({ page }) => {
  const receipt = '/v1/deletion-operations/op-session'
  const server = statusServer([])
  await serve(page, server, receipt, runningSessionDeletion)
  await openLoop(page, 'session-deletion', receipt)
  await failUntilStopped(page, server, receipt, RECEIPT_FAILURE_WAITS)

  // A session edit changes the list: the receipt poll starts over.
  await press(page, 'Sitzungen ändern')
  await expect(page.getByTestId('observation')).toHaveAttribute('data-stopped', 'false')
  await expectReads(page, server, receipt, [
    [300, failedRead(11)],
    [750, failedRead(12)],
  ])
})

test('a project switch forgets a stopped session deletion receipt poll', async ({ page }) => {
  const receipt = '/v1/deletion-operations/op-session'
  const server = statusServer([])
  await serve(page, server, receipt, runningSessionDeletion)
  await openLoop(page, 'session-deletion', receipt)
  await failUntilStopped(page, server, receipt, RECEIPT_FAILURE_WAITS)

  // The other project has no session being deleted: nothing is stopped
  // there, and the old project's poll never comes back.
  await press(page, 'Projekt wechseln')
  await expect(page.getByTestId('observation')).toHaveAttribute('data-stopped', 'false')
  await page.evaluate(() => window.dispatchEvent(new Event('online')))
  await page.clock.runFor(10 * 60_000)
  expect(await sentReads(page, receipt)).toBe(10)
})
