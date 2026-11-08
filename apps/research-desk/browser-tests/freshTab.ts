import type { BrowserContext, Page } from '@playwright/test'

/**
 * Run one round of a reload loop in its own tab. A Chromium tab stops loading
 * after about 5,000 requests (net::ERR_INSUFFICIENT_RESOURCES), and one load of
 * a fixture served by the dev server is several hundred modules.
 */
export async function inFreshTab(
  context: BrowserContext,
  round: (page: Page) => Promise<void>,
): Promise<void> {
  const page = await context.newPage()
  page.on('pageerror', (error) => {
    throw error
  })
  try {
    await round(page)
  } finally {
    await page.close()
  }
}
