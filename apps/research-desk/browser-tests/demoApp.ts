import { expect, type Page } from '@playwright/test'

/**
 * The app without a backend: discovery answers for an anonymous, degraded
 * server, so the demo mode can carry every workspace and nothing reaches a
 * real server.
 */
export async function installBackendFreeDiscoveryRoutes(page: Page): Promise<void> {
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

export async function enableDemoMode(page: Page): Promise<void> {
  const settings = page.getByRole('button', { exact: true, name: 'Einstellungen' })
  await expect(settings).toBeVisible({ timeout: 30_000 })
  await settings.click()

  const switchControl = page.getByRole('switch', { name: 'Demo-Modus' })
  await expect(switchControl).not.toBeChecked()
  await switchControl.click()

  // Enabling demo mode replaces the workspace and returns to Research Desk.
  // Re-open Settings and assert the control that owns the mode state.
  await settings.click()
  await expect(page.getByRole('switch', { name: 'Demo-Modus' })).toBeChecked()
  await expect(page.getByRole('button', { exact: true, name: 'Knowledge Desk' })).toBeVisible()
  await expect(page.getByRole('button', { exact: true, name: 'Agent Desk' })).toBeVisible()
}

export async function settleLayout(page: Page): Promise<void> {
  await page.evaluate(async () => {
    await document.fonts.ready
    await new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
    })
  })
}
