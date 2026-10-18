import { expect, test, type Page } from '@playwright/test'

const fixturePath = '/browser-tests/fixtures/index-pause.html'

/** One knowledge document as a collection lists it. `omitted` is the count
 * the server records when the content filter left chunks without context. */
type Doc = { fileId: string; id: string; omitted?: number; title: string }

type Server = {
  /** Documents each collection lists, read when the listing is requested. */
  documents: Record<string, () => Doc[]>
  failListing?: boolean
  /** Collection ids in the order their document listing was requested. */
  listings: string[]
  started: string[]
}

function documentInfo(collectionId: string, doc: Doc) {
  return {
    chunk_count: 12,
    collection_id: collectionId,
    created_at: 1_760_000_000,
    id: doc.id,
    metadata: {
      fileId: doc.fileId,
      ...(doc.omitted
        ? { _chunk_context_omitted: { chunks: doc.omitted, reason: 'content_filter' } }
        : {}),
    },
    title: doc.title,
  }
}

/** A document job that completed: job `ix_c` indexed `c.pdf` as `kd_c`. */
function completedJob(letter: string) {
  return {
    checkpoint: {},
    collection_id: 'kc_fix',
    completed_documents: 1,
    current_batch: 0,
    document_id: `kd_${letter}`,
    error: null,
    events_url: `/v1/knowledge/indexing-jobs/ix_${letter}/events`,
    job_id: `ix_${letter}`,
    operation_kind: 'document_revision',
    phase: 'publishing',
    revision_id: `rev_${letter}`,
    snapshot: {},
    status: 'completed',
    total_batches: 0,
    total_documents: 1,
  }
}

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => {
    throw error
  })
})

/** Routes the document listings and the indexing API. Every started document
 * job completes at once; everything else the workspace asks for is absent. */
async function serve(page: Page, server: Server) {
  await page.route(
    (url) => url.pathname.startsWith('/v1/') || url.pathname.startsWith('/api/'),
    async (route) => {
      const { pathname } = new URL(route.request().url())
      const method = route.request().method()
      const listing = pathname.match(/^\/v1\/knowledge\/collections\/([^/]+)\/documents$/)
      const events = pathname.match(/^\/v1\/knowledge\/indexing-jobs\/ix_([a-z])\/events$/)
      const single = pathname.match(/^\/v1\/knowledge\/indexing-jobs\/ix_([a-z])$/)
      if (listing && method === 'GET') {
        server.listings.push(listing[1])
        if (server.failListing) {
          await route.fulfill({ json: { detail: 'unavailable' }, status: 503 })
          return
        }
        const documents = (server.documents[listing[1]] ?? (() => []))()
        await route.fulfill({
          json: {
            data: documents.map((doc) => documentInfo(listing[1], doc)),
            next_cursor: null,
          },
        })
      } else if (pathname === '/v1/knowledge/indexing-jobs' && method === 'GET') {
        await route.fulfill({ json: { data: [] } })
      } else if (
        pathname === '/v1/knowledge/collections/kc_fix/document-revisions'
        && method === 'POST'
      ) {
        const { title } = route.request().postDataJSON() as { title: string }
        server.started.push(title)
        await route.fulfill({ json: completedJob(title.charAt(0)) })
      } else if (events) {
        await route.fulfill({
          body: '',
          headers: { 'content-type': 'text/event-stream' },
          status: 200,
        })
      } else if (single && method === 'GET') {
        await route.fulfill({ json: completedJob(single[1]) })
      } else {
        await route.fulfill({ json: {}, status: 404 })
      }
    },
  )
}

test('an indexed document names its chunks without context after a reload', async ({ page }) => {
  // The page loads with a and b indexed; the server recorded three chunks of
  // a without context. The row says so, b's row stays plain, and the view
  // lists the collection once.
  const server: Server = {
    documents: {
      kc_fix: () => [
        { fileId: 'file-a', id: 'kd_a', omitted: 3, title: 'a.pdf' },
        { fileId: 'file-b', id: 'kd_b', title: 'b.pdf' },
      ],
    },
    listings: [],
    started: [],
  }
  await serve(page, server)
  await page.goto(`${fixturePath}?embedded=file-a,file-b`)
  await page.getByRole('button', { name: 'Verträge 3' }).click()

  await expect(page.getByText('3 without context')).toBeVisible()
  await expect(page.getByText('without context')).toHaveCount(1)
  await page.getByText('3 without context').hover()
  await expect(page.getByRole('tooltip')).toContainText('content filter')
  expect(server.listings).toEqual(['kc_fix'])
})

test('a run that leaves chunks without context names them when it ends', async ({ page }) => {
  // c is indexed in this run and the server records two chunks without
  // context. The view lists the collection when it opens and again when the
  // run ends, never in between; c's row then names the count.
  const server: Server = {
    documents: {
      kc_fix: () => (
        server.started.length === 3
          ? [
              { fileId: 'file-a', id: 'kd_a', title: 'a.pdf' },
              { fileId: 'file-b', id: 'kd_b', title: 'b.pdf' },
              { fileId: 'file-c', id: 'kd_c', omitted: 2, title: 'c.pdf' },
            ]
          : []
      ),
    },
    listings: [],
    started: [],
  }
  await serve(page, server)
  await page.goto(fixturePath)
  await page.getByRole('button', { name: 'Verträge 3' }).click()
  await expect.poll(() => server.listings.length).toBe(1)

  await page.getByRole('button', { exact: true, name: 'Index 3' }).click()

  await expect(page.getByText('2 without context')).toBeVisible()
  await expect(page.getByText('without context')).toHaveCount(1)
  // Opening the view, the run's own check for documents already on the
  // server, and the end of the run.
  expect(server.listings).toEqual(['kc_fix', 'kc_fix', 'kc_fix'])
})

test('a shared collection names the chunks a document holds without context', async ({ page }) => {
  const server: Server = {
    documents: {
      kc_shared: () => [{ fileId: 'file-x', id: 'kd_x', omitted: 4, title: 'Vertrag.pdf' }],
    },
    listings: [],
    started: [],
  }
  await serve(page, server)
  await page.goto(`${fixturePath}?shared=1`)
  await page.getByRole('button', { name: /Geteilt/ }).click()

  await expect(page.getByText('Vertrag.pdf')).toBeVisible()
  await expect(page.getByText('4 without context')).toBeVisible()
  await page.getByText('4 without context').hover()
  await expect(page.getByRole('tooltip')).toContainText('content filter')
})

test('a listing that fails leaves the rows plain and says so in the console', async ({ page }) => {
  const warnings: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'warning') warnings.push(message.text())
  })
  const server: Server = {
    documents: {},
    failListing: true,
    listings: [],
    started: [],
  }
  await serve(page, server)
  await page.goto(`${fixturePath}?embedded=file-a`)
  await page.getByRole('button', { name: 'Verträge 3' }).click()

  await expect.poll(
    () => warnings.some((text) => text.includes('Context omission counts could not be loaded')),
  ).toBe(true)
  await expect(page.getByText('Indexed', { exact: true })).toHaveCount(1)
  await expect(page.getByText('without context')).toHaveCount(0)
})
