import { expect, test, type Page } from '@playwright/test'

const fixturePath = '/browser-tests/fixtures/asset-body-load.html'
const ASSET_ID = 'asset-unprepared'
const NOW = 1_791_700_000

/** The three prepared areas every file library has (the hook loads nothing
 * until all three are there). */
const sections = (['temporary', 'library', 'project_sources'] as const).map((role) => ({
  created_at: NOW, id: `sec-${role}`, kind: role, semantic_role: role,
  title: role, updated_at: NOW,
}))

/** A server file whose canonical body the server cannot prepare (the
 * document parser is off): metadata lists it, the detail carries no
 * prepared provenance. */
const asset = {
  created_at: NOW, file_name: 'vertrag.pdf', group_id: null, id: ASSET_ID,
  label: 'vertrag', mime_type: 'application/pdf', origin: 'upload', page_count: null,
  parse_status: 'failed', parse_warning: null, prepared_at: null,
  prepared_content_hash: null, prepared_parser_id: null, section_id: 'sec-library',
  server_file_id: 'file-1', size_bytes: 1_024, text_truncated: false,
  title: 'Vertrag', updated_at: NOW,
}

test.beforeEach(async ({ page }) => {
  page.on('pageerror', (error) => {
    throw error
  })
})

/** Routes the fixture's API: the asset detail answers unprepared until the
 * server holds a prepared version (`server.version` 2), and counts every
 * detail request. */
async function serveAssets(page: Page, server: { detailRequests: number; version: number }) {
  await page.route(
    (url) => url.pathname.startsWith('/v1/') || url.pathname.startsWith('/api/'),
    async (route) => {
      const { pathname } = new URL(route.request().url())
      const method = route.request().method()
      if (pathname === '/v1/assets/sections' || pathname === '/v1/assets/default-sections') {
        await route.fulfill({ json: { data: sections } })
      } else if (pathname === '/v1/assets/groups') {
        await route.fulfill({ json: { data: [] } })
      } else if (pathname === '/v1/assets') {
        await route.fulfill({ json: { data: [current(server)], next_cursor: null } })
      } else if (pathname === `/v1/assets/${ASSET_ID}` && method === 'GET') {
        server.detailRequests += 1
        await route.fulfill({ json: detail(server) })
      } else if (pathname === '/v1/uploads') {
        await route.fulfill({ json: { data: [], object: 'list' } })
      } else {
        await route.fulfill({ json: {}, status: 404 })
      }
    },
  )
}

function current(server: { version: number }) {
  return { ...asset, updated_at: NOW + server.version }
}

function detail(server: { version: number }) {
  return server.version < 2
    ? { ...current(server), extracted_text: '', prepared_text: '' }
    : {
        ...current(server), extracted_text: 'Vertragstext', parse_status: 'parsed',
        prepared_at: NOW + 2, prepared_content_hash: 'sha-prepared', prepared_parser_id: 'markitdown',
        prepared_text: 'Vertragstext',
      }
}

test('an asset the server cannot prepare is asked for once, not on every render', async ({ page }) => {
  // The regression: every answer changed the store, the chat derived a new
  // list from it and asked again, about 29 requests a second until reload.
  const server = { detailRequests: 0, version: 1 }
  await serveAssets(page, server)
  await page.goto(fixturePath)
  await expect(page.getByTestId('asset-status')).toHaveAttribute('data-status', 'failed')
  // Long past the time a loop needs for dozens of requests.
  await page.waitForTimeout(1_500)
  expect(server.detailRequests).toBe(1)
})

test('an unprepared asset is asked for again once the server changed it', async ({ page }) => {
  // Re-enabling the parser prepares the file anew; the asset's new version
  // must reach the reader without a page reload.
  const server = { detailRequests: 0, version: 1 }
  await serveAssets(page, server)
  await page.goto(fixturePath)
  await expect(page.getByTestId('asset-status')).toHaveAttribute('data-status', 'failed')
  server.version = 2
  await page.getByRole('button', { name: 'Neu laden' }).click()
  await expect(page.getByTestId('asset-status')).toHaveAttribute('data-status', 'ready')
  await page.waitForTimeout(1_500)
  expect(server.detailRequests).toBe(2)
})
