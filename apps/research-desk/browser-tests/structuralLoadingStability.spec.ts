import { expect, test, type Locator, type Page, type TestInfo } from '@playwright/test'

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.clear()
    window.sessionStorage.clear()
    // These specs address the desk by its German labels, so they state the
    // language instead of inheriting the English first-render fallback.
    window.localStorage.setItem('inqtrix.researchDesk.locale', 'de')
  })
  await installBackendFreeDiscoveryRoutes(page)
  page.on('pageerror', (error) => {
    throw error
  })
})

test('warm and prefetched desk targets never flash a structural fallback', async ({ page }, testInfo) => {
  test.setTimeout(180_000)
  const consoleErrors: string[] = []
  const requestFailures: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  page.on('requestfailed', (request) => {
    requestFailures.push(`${request.method()} ${request.url()}: ${request.failure()?.errorText ?? 'failed'}`)
  })
  await page.setViewportSize({ height: 1_100, width: 1_440 })
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await enableDemoMode(page)

  const navigation = page.getByRole('navigation', { name: 'Ansichten' })
  const research = navigation.getByRole('button', { exact: true, name: 'Research Desk' })
  const chat = navigation.getByRole('button', { exact: true, name: 'Chat' })
  const editor = navigation.getByRole('button', { exact: true, name: 'Editor' })
  const database = navigation.getByRole('button', { exact: true, name: 'Datenbank' })

  await research.click()
  await expect(research).toHaveAttribute('aria-pressed', 'true')

  // First visit may still be a genuine structural cold start (for example
  // uncached Mermaid rendering in WebKit). Warm it before measuring the cache
  // contract; the second visit must not mount any fallback in any engine.
  await chat.click()
  await expect(chat).toHaveAttribute('aria-pressed', 'true')
  const chatRegion = page.locator('[data-structural-requested-identity^="chat:"]').first()
  await expect(chatRegion).toHaveAttribute('data-structural-state', 'ready', { timeout: 30_000 })
  await research.click()
  await observeStructuralFallbacks(page)
  await chat.click()
  await expect(chatRegion).toHaveAttribute('data-structural-state', 'ready', { timeout: 30_000 })
  await expect(chatRegion).toHaveAttribute('data-structural-blockers', '0')
  await expect(chatRegion.locator('[data-structural-fallback]')).toHaveCount(0)
  await expect(chatRegion.getByText('Diagramm wird erstellt …')).toHaveCount(0)
  await expect(chatRegion.locator('.inqtrix-mermaid svg')).toHaveCount(3)
  expect(await structuralFallbackCount(page)).toBe(0)
  const chatStability = await probeRegionStability(chatRegion)
  expect(chatStability.maxVerticalDelta).toBeLessThanOrEqual(2)
  expect(chatStability.scrollHeightRange).toBeLessThanOrEqual(1)
  expect(chatStability.scrollTopRange).toBeLessThanOrEqual(1)
  expect(chatStability.distanceFromBottom).toBeLessThanOrEqual(1)
  expect(chatStability.disconnectedElements).toBe(0)
  await captureFrame(page, testInfo, 'chat-warm-settled')

  await research.click()
  await editor.click()
  await expect(editor).toHaveAttribute('aria-pressed', 'true')
  const editorTextbox = page.getByRole('textbox', { name: 'Dokumentinhalt' })
  await expect(editorTextbox).toBeVisible({ timeout: 30_000 })

  await research.click()
  await resetStructuralFallbackCount(page)
  await editor.click()
  await expect(editor).toHaveAttribute('aria-pressed', 'true')
  await expect(editorTextbox).toBeVisible({ timeout: 30_000 })
  const editorRegion = editorTextbox.locator('xpath=ancestor::*[@data-structural-region][1]')
  await expect(editorRegion).toHaveAttribute('data-structural-state', 'ready')
  await expect(editorRegion).toHaveAttribute('data-structural-blockers', '0')
  await expect(editorRegion.locator('[data-structural-fallback]')).toHaveCount(0)
  expect(await structuralFallbackCount(page)).toBe(0)
  const editorStability = await probeRegionStability(editorRegion)
  expect(editorStability.maxVerticalDelta).toBeLessThanOrEqual(2)
  expect(editorStability.scrollHeightRange).toBeLessThanOrEqual(1)
  expect(editorStability.scrollTopRange).toBeLessThanOrEqual(1)
  expect(editorStability.disconnectedElements).toBe(0)
  await captureFrame(page, testInfo, 'editor-warm-settled')

  await chat.click()
  await editor.click()
  await database.click()
  await chat.click()
  await expect(chat).toHaveAttribute('aria-pressed', 'true')
  await expect(page.locator('[data-structural-requested-identity^="chat:"]').first()).toHaveAttribute(
    'data-structural-state',
    'ready',
    { timeout: 30_000 },
  )
  await expect(page.locator('[data-structural-fallback]')).toHaveCount(0)
  expect(await structuralFallbackCount(page)).toBe(0)
  const invalidMathSubscripts = await page.locator('msub').evaluateAll((elements) => (
    elements
      .filter((element) => element.children.length !== 2)
      .slice(0, 10)
      .map((element) => element.outerHTML)
  ))
  expect(invalidMathSubscripts).toEqual([])
  expect(
    consoleErrors,
    `invalid msub elements: ${JSON.stringify(invalidMathSubscripts)}`,
  ).toEqual([])
  expect(requestFailures).toEqual([])
})

test('report header and body publish as one identity without fallback', async ({ page }) => {
  test.setTimeout(120_000)
  await page.setViewportSize({ height: 720, width: 1_280 })
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await enableDemoMode(page)
  await observeStructuralFallbacks(page)

  const reportRegion = page.locator('[data-structural-requested-identity^="report:"]').first()
  await expect(reportRegion).toHaveAttribute('data-structural-state', /ready|refreshing/)
  const cards = page.getByRole('article')
  expect(await cards.count()).toBeGreaterThan(1)
  await startReportIdentityProbe(reportRegion)
  await cards.nth(1).click()
  await expect(reportRegion).toHaveAttribute('data-structural-state', /ready|refreshing/, {
    timeout: 30_000,
  })

  await reportRegion.evaluate(async () => {
    const startedAt = performance.now()
    while (performance.now() - startedAt < 1_000) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
    }
  })
  const identitySamples = await reportIdentityMismatches(page)
  expect(identitySamples).toEqual([])
  expect(await structuralFallbackCount(page)).toBe(0)
})

test('reduced motion keeps warm navigation free of fallback and motion', async ({ page }) => {
  test.setTimeout(120_000)
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.setViewportSize({ height: 720, width: 1_280 })
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await enableDemoMode(page)

  const navigation = page.getByRole('navigation', { name: 'Ansichten' })
  const chat = navigation.getByRole('button', { exact: true, name: 'Chat' })
  const research = navigation.getByRole('button', { exact: true, name: 'Research Desk' })
  // Reduced motion changes presentation, not the cold/warm distinction. Warm
  // the target first, then prove its repeat navigation stays fallback-free.
  await chat.click()
  const region = page.locator('[data-structural-requested-identity^="chat:"]').first()
  await expect(region).toHaveAttribute('data-structural-state', 'ready', { timeout: 30_000 })
  await research.click()
  await observeStructuralFallbacks(page)
  await chat.click()
  await expect(region).toHaveAttribute('data-structural-state', 'ready', { timeout: 30_000 })
  expect(await structuralFallbackCount(page)).toBe(0)
  expect(await page.evaluate(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches)).toBe(true)
})

async function observeStructuralFallbacks(page: Page): Promise<void> {
  await page.evaluate(() => {
    const observed: number[] = []
    const observer = new MutationObserver((records) => {
      for (const record of records) {
        for (const node of record.addedNodes) {
          if (!(node instanceof Element)) continue
          if (node.matches('[data-structural-fallback]') || node.querySelector('[data-structural-fallback]')) {
            observed.push(performance.now())
          }
        }
      }
    })
    observer.observe(document.documentElement, { childList: true, subtree: true })
    Object.assign(window, { __inqtrixStructuralFallbacks: observed })
  })
}

async function structuralFallbackCount(page: Page): Promise<number> {
  return await page.evaluate(() => (
    (window as typeof window & { __inqtrixStructuralFallbacks?: number[] })
      .__inqtrixStructuralFallbacks?.length ?? 0
  ))
}

async function resetStructuralFallbackCount(page: Page): Promise<void> {
  await page.evaluate(() => {
    const observed = (window as typeof window & {
      __inqtrixStructuralFallbacks?: number[]
    }).__inqtrixStructuralFallbacks
    if (observed) observed.length = 0
  })
}

async function startReportIdentityProbe(region: Locator): Promise<void> {
  await region.evaluate((root) => {
    const mismatches: Array<{ surface: string | null; visible: string | null }> = []
    const sample = () => {
      const visible = root.getAttribute('data-structural-visible-identity')
      const surface = root.querySelector<HTMLElement>(
        ':scope > [data-structural-layer="visible"] [data-report-surface-run-id]',
      )?.dataset.reportSurfaceRunId ?? null
      if (
        visible?.startsWith('report:')
        && surface !== null
        && !visible.startsWith(`report:${surface}:`)
      ) {
        mismatches.push({ surface, visible })
      }
      requestAnimationFrame(sample)
    }
    Object.assign(window, { __inqtrixReportIdentityMismatches: mismatches })
    requestAnimationFrame(sample)
  })
}

async function reportIdentityMismatches(
  page: Page,
): Promise<Array<{ surface: string | null; visible: string | null }>> {
  return await page.evaluate(() => (
    (window as typeof window & {
      __inqtrixReportIdentityMismatches?: Array<{
        surface: string | null
        visible: string | null
      }>
    }).__inqtrixReportIdentityMismatches ?? []
  ))
}

async function probeRegionStability(region: Locator): Promise<{
  disconnectedElements: number
  distanceFromBottom: number
  maxVerticalDelta: number
  scrollHeightRange: number
  scrollTopRange: number
}> {
  return await region.evaluate(async (root) => {
    const candidates = [...root.querySelectorAll<HTMLElement>('h1,h2,h3,p,li,tr,[contenteditable="true"]')]
    const rootRect = root.getBoundingClientRect()
    const visible = candidates.filter((element) => {
      const rect = element.getBoundingClientRect()
      return rect.bottom > rootRect.top && rect.top < rootRect.bottom && rect.width > 20
    })
    const initial = new Map(visible.map((element) => {
      const rect = element.getBoundingClientRect()
      return [element, { bottom: rect.bottom, top: rect.top }]
    }))
    const scrollables = [...root.querySelectorAll<HTMLElement>('*')]
      .filter((element) => element.scrollHeight > element.clientHeight + 1)
      .sort((left, right) => (
        right.clientHeight * right.clientWidth - left.clientHeight * left.clientWidth
      ))
    const viewport = scrollables[0] ?? root
    const scrollTops = [viewport.scrollTop]
    const scrollHeights = [viewport.scrollHeight]
    let disconnectedElements = 0
    let maxVerticalDelta = 0
    const startedAt = performance.now()

    while (performance.now() - startedAt < 1_000) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
      scrollTops.push(viewport.scrollTop)
      scrollHeights.push(viewport.scrollHeight)
      for (const [element, baseline] of initial) {
        if (!element.isConnected) {
          disconnectedElements += 1
          continue
        }
        const rect = element.getBoundingClientRect()
        maxVerticalDelta = Math.max(
          maxVerticalDelta,
          Math.abs(rect.top - baseline.top),
          Math.abs(rect.bottom - baseline.bottom),
        )
      }
    }

    return {
      disconnectedElements,
      distanceFromBottom: Math.abs(
        viewport.scrollHeight - viewport.clientHeight - viewport.scrollTop,
      ),
      maxVerticalDelta,
      scrollHeightRange: Math.max(...scrollHeights) - Math.min(...scrollHeights),
      scrollTopRange: Math.max(...scrollTops) - Math.min(...scrollTops),
    }
  })
}

async function captureFrame(page: Page, testInfo: TestInfo, name: string): Promise<void> {
  if (testInfo.project.name !== 'chromium') return
  const path = testInfo.outputPath(`${name}.png`)
  await page.screenshot({ path })
  await testInfo.attach(name, { contentType: 'image/png', path })
}

async function enableDemoMode(page: Page): Promise<void> {
  const settings = page.getByRole('button', { exact: true, name: 'Einstellungen' })
  await expect(settings).toBeVisible({ timeout: 30_000 })
  await settings.click()
  const demoMode = page.getByRole('switch', { name: 'Demo-Modus' })
  await expect(demoMode).not.toBeChecked()
  await demoMode.click()
  await expect(page.getByRole('button', { exact: true, name: 'Research Desk' })).toHaveAttribute(
    'aria-pressed',
    'true',
  )
}

async function installBackendFreeDiscoveryRoutes(page: Page): Promise<void> {
  const jsonResponse = (body: unknown) => ({
    body: JSON.stringify(body),
    contentType: 'application/json',
    status: 200,
  })
  await page.route('**/api/auth/config', (route) => route.fulfill(jsonResponse({
    auth_mode: 'none',
    auth_required: false,
    csrf_header: 'x-csrf-token',
    csrf_required: false,
    login_methods: [],
    pat_available: false,
    provider_name: null,
    registration: { needs_owner: false, self_service: false },
    supports_logout: false,
  })))
  await page.route('**/health', (route) => route.fulfill(jsonResponse({
    auth_mode: 'none',
    auth_required: false,
    llm: { provider: 'fixture', status: 'unavailable' },
    search: { provider: 'fixture', status: 'unavailable' },
    status: 'degraded',
  })))
  await page.route('**/v1/capabilities', (route) => route.fulfill(jsonResponse({
    algorithms: [],
    features: {
      embedding_provider: false,
      knowledge: false,
      multi_stack: false,
      openapi: false,
    },
    timeouts: {
      chat_wait_seconds: 3_630,
      editor_wait_seconds: 630,
      text_wait_seconds: 630,
    },
  })))
  await page.route('**/v1/runs?*', (route) => route.fulfill(jsonResponse({
    data: [],
    next_cursor: null,
  })))
}
