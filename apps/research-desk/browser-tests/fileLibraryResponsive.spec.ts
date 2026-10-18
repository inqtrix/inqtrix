import { expect, test } from '@playwright/test'

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    // This spec addresses the desk by its German labels, so it states the
    // language instead of inheriting the English first-render fallback.
    window.localStorage.setItem('inqtrix.researchDesk.locale', 'de')
  })
  page.on('pageerror', (error) => {
    throw error
  })
})

test('mobile file library keeps its primary upload action fully visible', async ({ page }) => {
  const consoleErrors: string[] = []
  const consoleWarnings: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
    if (message.type() === 'warning') consoleWarnings.push(message.text())
  })
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

  await page.setViewportSize({ height: 844, width: 390 })
  await page.goto('/')

  await page.getByRole('button', { exact: true, name: 'Einstellungen' }).click()
  const demoMode = page.getByRole('switch', { name: 'Demo-Modus' })
  await expect(demoMode).not.toBeChecked()
  await demoMode.click()

  // The mobile topbar deliberately omits the desktop project status. Re-open
  // the state-owning Settings control so this cannot pass on a merely seeded
  // or otherwise similar-looking database screen.
  await page.getByRole('button', { exact: true, name: 'Einstellungen' }).click()
  await expect(page.getByRole('switch', { name: 'Demo-Modus' })).toBeChecked()
  await page.getByRole('button', { exact: true, name: 'Datenbank' }).click()
  await page.getByRole('button', { name: 'Alle Sammlungen 59' }).click()

  const primaryUpload = () => page
    .getByRole('button', { exact: true, name: 'Hochladen' })
    .filter({ hasText: 'Hochladen' })

  for (const width of [320, 390, 607, 768, 1_280]) {
    await page.setViewportSize({ height: 844, width })
    const upload = primaryUpload()
    await expect(upload).toHaveCount(1)
    await expect(upload).toBeVisible()

    const geometry = await upload.evaluate((element) => {
      const rect = element.getBoundingClientRect()
      let clippingAncestor: HTMLElement | null = element.parentElement
      while (clippingAncestor) {
        const overflowX = getComputedStyle(clippingAncestor).overflowX
        if (overflowX === 'hidden' || overflowX === 'clip') break
        clippingAncestor = clippingAncestor.parentElement
      }
      if (!clippingAncestor) {
        throw new Error('The file-library action has no bounded workspace ancestor')
      }
      return {
        action: {
          bottom: rect.bottom,
          left: rect.left,
          right: rect.right,
          top: rect.top,
          width: rect.width,
        },
        workspace: {
          clientWidth: clippingAncestor.clientWidth,
          scrollWidth: clippingAncestor.scrollWidth,
        },
      }
    })

    expect(geometry.action.left).toBeGreaterThanOrEqual(0)
    expect(geometry.action.right).toBeLessThanOrEqual(width)
    expect(geometry.action.top).toBeGreaterThanOrEqual(0)
    expect(geometry.action.bottom).toBeLessThanOrEqual(844)
    expect(geometry.action.width).toBeGreaterThanOrEqual(44)
    expect(geometry.workspace.scrollWidth).toBeLessThanOrEqual(
      geometry.workspace.clientWidth,
    )
  }

  await page.setViewportSize({ height: 844, width: 390 })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  expect(await page.evaluate(() => (
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  ))).toBe(true)
  await primaryUpload().focus()
  await expect(primaryUpload()).toBeFocused()
  const fileChooser = page.waitForEvent('filechooser')
  await primaryUpload().press('Enter')
  await fileChooser
  expect(consoleErrors).toEqual([])
  expect(consoleWarnings).toEqual([])
})
