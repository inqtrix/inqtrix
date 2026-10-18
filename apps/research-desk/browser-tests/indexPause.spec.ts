import { expect, test, type Page } from '@playwright/test'

const fixturePath = '/browser-tests/fixtures/index-pause.html'

type Job = {
  checkpoint?: Record<string, unknown>
  document_id: string
  error?: { message: string; type?: string } | null
  job_id: string
  status: string
  title: string
}

type Server = {
  cancelled?: string[]
  /** Paused jobs the server lists on load (a reload of the page). */
  listed: Job[]
  /** Jobs by document title, as the run starts them. */
  jobs: Job[]
  /** A rebuild of the whole collection and the event stream it sends. */
  rebuild?: { events: string }
  /** Raw summaries the server lists on load besides the document jobs. */
  rawListed?: Array<Record<string, unknown>>
  resumed: string[]
  started: string[]
}

function summary(job: Job) {
  return {
    checkpoint: job.checkpoint ?? {},
    collection_id: 'kc_fix',
    completed_documents: job.status === 'completed' ? 1 : 0,
    current_batch: 4,
    document_id: job.document_id,
    error: job.error ?? null,
    events_url: `/v1/knowledge/indexing-jobs/${job.job_id}/events`,
    job_id: job.job_id,
    operation_kind: 'document_revision',
    phase: 'contextualization',
    revision_id: `rev_${job.job_id}`,
    snapshot: {},
    status: job.status,
    total_batches: 11,
    total_documents: 1,
  }
}

const rebuildSummary = {
  checkpoint: {},
  collection_id: 'kc_fix',
  completed_documents: 0,
  current_batch: 0,
  document_id: null,
  error: null,
  events_url: '/v1/knowledge/indexing-jobs/ix_rebuild/events',
  generation_id: 'gen_rebuild',
  job_id: 'ix_rebuild',
  operation_kind: 'collection_generation',
  phase: 'queued',
  revision_id: null,
  snapshot: {},
  status: 'queued',
  total_batches: 0,
  total_documents: 3,
}

/** The rebuild's event stream, framed as the server frames it. */
function rebuildEvents(frames: Array<{ data: Record<string, unknown>; type: string }>): string {
  return frames.map((frame, index) => {
    const event = {
      created_at: 1_791_740_000,
      data: frame.data,
      job_id: rebuildSummary.job_id,
      sequence: index + 1,
      type: `inqtrix.index.${frame.type}`,
    }
    return `event: ${event.type}\ndata: ${JSON.stringify(event)}\n\n`
  }).join('')
}

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => {
    throw error
  })
})

/** Routes the indexing API of one collection. Document jobs start as the
 * server state says, resuming completes a job, and every start and resume is
 * counted. Everything else the workspace asks for is absent (404). */
async function serveIndexing(page: Page, server: Server) {
  const byId = (jobId: string) => (
    server.jobs.find((job) => job.job_id === jobId)
    ?? server.listed.find((job) => job.job_id === jobId)
  )
  await page.route(
    (url) => url.pathname.startsWith('/v1/') || url.pathname.startsWith('/api/'),
    async (route) => {
      const { pathname } = new URL(route.request().url())
      const method = route.request().method()
      const resume = pathname.match(/^\/v1\/knowledge\/indexing-jobs\/([^/]+)\/resume$/)
      const cancel = pathname.match(/^\/v1\/knowledge\/indexing-jobs\/([^/]+)\/cancel$/)
      const events = pathname.match(/^\/v1\/knowledge\/indexing-jobs\/([^/]+)\/events$/)
      const single = pathname.match(/^\/v1\/knowledge\/indexing-jobs\/([^/]+)$/)
      if (pathname === '/v1/knowledge/indexing-jobs' && method === 'GET') {
        await route.fulfill({
          json: { data: [...server.listed.map(summary), ...(server.rawListed ?? [])] },
        })
      } else if (
        pathname === '/v1/knowledge/collections/kc_fix/reindex'
        && method === 'POST'
        && server.rebuild
      ) {
        await route.fulfill({ json: rebuildSummary })
      } else if (events && events[1] === rebuildSummary.job_id && server.rebuild) {
        await route.fulfill({
          body: server.rebuild.events,
          headers: { 'content-type': 'text/event-stream' },
          status: 200,
        })
      } else if (pathname === '/v1/knowledge/collections/kc_fix/documents') {
        await route.fulfill({ json: { data: [], next_cursor: null } })
      } else if (
        pathname === '/v1/knowledge/collections/kc_fix/document-revisions'
        && method === 'POST'
      ) {
        const { title } = route.request().postDataJSON() as { title: string }
        server.started.push(title)
        const job = server.jobs.find((candidate) => candidate.title === title)!
        await route.fulfill({ json: summary(job) })
      } else if (resume && method === 'POST') {
        server.resumed.push(resume[1])
        const job = byId(resume[1])!
        job.status = 'completed'
        job.error = null
        await route.fulfill({ json: summary(job) })
      } else if (cancel && method === 'POST') {
        server.cancelled?.push(cancel[1])
        const job = byId(cancel[1])!
        job.status = 'cancelled'
        await route.fulfill({ json: summary(job) })
      } else if (events) {
        await route.fulfill({
          body: '',
          headers: { 'content-type': 'text/event-stream' },
          status: 200,
        })
      } else if (single && method === 'GET') {
        await route.fulfill({ json: summary(byId(single[1])!) })
      } else {
        await route.fulfill({ json: {}, status: 404 })
      }
    },
  )
}

const paused = (title: string, jobId: string, documentId: string): Job => ({
  document_id: documentId,
  error: { message: 'Context enrichment needs review.' },
  job_id: jobId,
  status: 'paused_validation',
  title,
})

test('every paused document shows at once and one click continues them all', async ({ page }) => {
  // Two of three documents pause: both rows must say so, and one action must
  // continue both paused jobs (resumed, never started again).
  const server: Server = {
    jobs: [
      paused('a.pdf', 'ix_a', 'kd_a'),
      paused('b.pdf', 'ix_b', 'kd_b'),
      { document_id: 'kd_c', job_id: 'ix_c', status: 'completed', title: 'c.pdf' },
    ],
    listed: [],
    resumed: [],
    started: [],
  }
  await serveIndexing(page, server)
  await page.goto(fixturePath)
  await page.getByRole('button', { name: 'Verträge 3' }).click()
  await page.getByRole('button', { exact: true, name: 'Index 3' }).click()

  await expect(page.getByText('Review needed')).toHaveCount(2)
  await page.getByRole('button', { name: 'Resume all (2)' }).click()

  await expect.poll(() => [...server.resumed].sort()).toEqual(['ix_a', 'ix_b'])
  expect([...server.started].sort()).toEqual(['a.pdf', 'b.pdf', 'c.pdf'])
  await expect(page.getByText('Review needed')).toHaveCount(0)
})

test('paused documents found after a reload continue with one click', async ({ page }) => {
  // The page is reloaded while two documents wait: both are listed by the
  // server and must come back as one paused run, not as one job at a time.
  // The same click also starts c, which the stopped run never reached.
  const server: Server = {
    jobs: [{ document_id: 'kd_c', job_id: 'ix_c', status: 'completed', title: 'c.pdf' }],
    listed: [paused('a.pdf', 'ix_a', 'kd_a'), paused('b.pdf', 'ix_b', 'kd_b')],
    resumed: [],
    started: [],
  }
  await serveIndexing(page, server)
  await page.goto(fixturePath)
  await page.getByRole('button', { name: 'Verträge 3' }).click()

  await expect(page.getByText('Review needed')).toHaveCount(2)
  await page.getByRole('button', { name: 'Resume all (2)' }).click()

  await expect.poll(() => [...server.resumed].sort()).toEqual(['ix_a', 'ix_b'])
  await expect.poll(() => server.started).toEqual(['c.pdf'])
})

test('cancelling a paused run cancels every paused job on the server', async ({ page }) => {
  // Left paused on the server, the jobs would come back after a reload. One
  // document waits on the provider, so the run reads as a dependency pause.
  const server: Server = {
    cancelled: [],
    jobs: [
      { ...paused('a.pdf', 'ix_a', 'kd_a'), status: 'paused_dependency' },
      paused('b.pdf', 'ix_b', 'kd_b'),
      { document_id: 'kd_c', job_id: 'ix_c', status: 'completed', title: 'c.pdf' },
    ],
    listed: [],
    resumed: [],
    started: [],
  }
  await serveIndexing(page, server)
  await page.goto(fixturePath)
  await page.getByRole('button', { name: 'Verträge 3' }).click()
  await page.getByRole('button', { exact: true, name: 'Index 3' }).click()

  await expect(page.getByText('Paused · dependency')).toBeVisible()
  await page.getByRole('button', { name: 'Cancel indexing' }).click()

  await expect.poll(() => [...server.cancelled!].sort()).toEqual(['ix_a', 'ix_b'])
  await expect(page.getByText('Paused · dependency')).toHaveCount(0)
})

test('a dependency pause names what it waits for and the next automatic attempt', async ({ page }) => {
  const now = Math.floor(Date.now() / 1000)
  const sooner = now + 3600
  const waiting = (title: string, jobId: string, documentId: string, attempts: number, nextAt: number): Job => ({
    checkpoint: { auto_resume: { attempts, max: 5, next_at: nextAt, stopped: null } },
    document_id: documentId,
    error: { message: 'vector store down', type: 'vector_store_unavailable' },
    job_id: jobId,
    status: 'paused_dependency',
    title,
  })
  const server: Server = {
    jobs: [],
    listed: [
      waiting('a.pdf', 'ix_a', 'kd_a', 0, now + 7200),
      waiting('b.pdf', 'ix_b', 'kd_b', 1, sooner),
    ],
    resumed: [],
    started: [],
  }
  await serveIndexing(page, server)
  await page.goto(fixturePath)

  await page.getByRole('button', { name: 'Verträge 3' }).click()

  // The run names the soonest automatic resume among its paused documents, in
  // the viewer's own clock time; the server's own message stays out of view.
  const time = await page.evaluate(
    (seconds) => new Date(seconds * 1000).toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' }),
    sooner,
  )
  await expect(
    page.getByText('The vector store is temporarily unreachable. (2 documents paused)'),
  ).toBeVisible()
  await expect(page.getByText(`Next automatic attempt at ${time} (2/5)`)).toBeVisible()
  await expect(page.getByRole('button', { name: 'Resume all (2)' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Rebuild without context' })).toHaveCount(0)
  await expect(page.getByText('vector store down')).toHaveCount(0)
})

test('a rebuild that pauses shows its rows paused and its next automatic attempt', async ({ page }) => {
  const nextAt = Math.floor(Date.now() / 1000) + 3600
  const snapshot = { completed_documents: 0, current_batch: 1, phase: 'embedding', total_batches: 1, total_documents: 3 }
  const server: Server = {
    jobs: [],
    listed: [],
    rebuild: {
      events: rebuildEvents([
        { data: { snapshot, status: 'running' }, type: 'started' },
        { data: { document_id: 'kd_a' }, type: 'document_started' },
        {
          data: { current_batch: 1, document_id: 'kd_a', phase: 'embedding', total_batches: 1 },
          type: 'document_progress',
        },
        {
          data: {
            auto_resume: { attempts: 0, max: 5, next_at: nextAt, stopped: null },
            error: { message: 'vector store down', type: 'vector_store_unavailable' },
            snapshot,
            status: 'paused_dependency',
          },
          type: 'paused_dependency',
        },
      ]),
    },
    resumed: [],
    started: [],
  }
  await serveIndexing(page, server)
  await page.goto(`${fixturePath}?embedded=file-a,file-b,file-c`)

  await page.getByRole('button', { name: 'Verträge 3' }).click()
  await page.getByRole('button', { exact: true, name: 'Refresh' }).click()

  const time = await page.evaluate(
    (seconds) => new Date(seconds * 1000).toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' }),
    nextAt,
  )
  await expect(page.getByText('The vector store is temporarily unreachable.')).toBeVisible()
  await expect(page.getByText(`Next automatic attempt at ${time} (1/5)`)).toBeVisible()
  // Every document of the paused rebuild waits with it; none reads running.
  await expect(page.getByText('Paused', { exact: true })).toHaveCount(3)
  await expect(page.getByText('Embedding 1/1')).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Rebuild without context' })).toHaveCount(0)
})

test('a paused rebuild found on load keeps its embedding row paused', async ({ page }) => {
  // Reattaching to a paused rebuild replays its per-document checkpoint, which
  // names the document it was embedding; that row must still read paused.
  const nextAt = Math.floor(Date.now() / 1000) + 3600
  const server: Server = {
    jobs: [],
    listed: [],
    rawListed: [{
      ...rebuildSummary,
      checkpoint: {
        auto_resume: { attempts: 1, max: 5, next_at: nextAt, stopped: null },
        document_progress: { kd_a: { current_batch: 1, phase: 'embedding', total_batches: 1 } },
      },
      current_batch: 1,
      error: { message: 'vector store down', type: 'vector_store_unavailable' },
      phase: 'embedding',
      status: 'paused_dependency',
      total_batches: 1,
    }],
    resumed: [],
    started: [],
  }
  await serveIndexing(page, server)
  await page.goto(`${fixturePath}?embedded=file-a,file-b,file-c`)

  await page.getByRole('button', { name: 'Verträge 3' }).click()

  const time = await page.evaluate(
    (seconds) => new Date(seconds * 1000).toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit' }),
    nextAt,
  )
  await expect(page.getByText('The vector store is temporarily unreachable.')).toBeVisible()
  await expect(page.getByText(`Next automatic attempt at ${time} (2/5)`)).toBeVisible()
  // The rebuild queues every document; the one it was embedding and the ones
  // still waiting all wait with it, and none reads running.
  await expect(page.getByText('Paused', { exact: true })).toHaveCount(3)
  await expect(page.getByText('Embedding 1/1')).toHaveCount(0)
})
