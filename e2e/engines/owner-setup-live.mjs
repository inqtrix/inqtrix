// Fresh local-owner live engine (profile: owner-setup).
//
// Drives exactly one explicitly selected browser against an externally
// prepared empty local-auth stack. The stack lifecycle stays outside this
// engine so raw Compose remains the canonical operator boundary. Evidence is
// limited to masked screenshots and structural request metadata: auth bodies,
// cookies, CSRF values, and passwords are never persisted.

import { chmod, mkdir, rename, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

import { chromium, firefox, webkit } from '@playwright/test'

import {
  assertVerificationRunId,
} from '../../tests/verification/fixtures/run-scope.mjs'

const baseURL = process.env.INQTRIX_E2E_BASE_URL ?? 'http://127.0.0.1:8080'
const ownerEmail = requiredEnvironment('INQTRIX_E2E_ADMIN_EMAIL')
const ownerPassword = requiredEnvironment('INQTRIX_E2E_ADMIN_PASSWORD')
const userEmail = requiredEnvironment('INQTRIX_E2E_TESTER_EMAIL')
const userPassword = requiredEnvironment('INQTRIX_E2E_USER_PASSWORD')
const browserTarget = requiredBrowserTarget(
  requiredEnvironment('INQTRIX_VERIFICATION_BROWSER_TARGET'),
)
const runId = assertVerificationRunId(
  requiredEnvironment('INQTRIX_VERIFICATION_RUN_ID'),
)
const reportDirectory = requiredEnvironment('INQTRIX_VERIFICATION_REPORT_DIR')
const screenshotDirectory = join(reportDirectory, 'screenshots')
const ignoreHTTPSErrors = process.env.INQTRIX_E2E_IGNORE_HTTPS_ERRORS === '1'

const BROWSER_TYPES = { chromium, firefox, webkit }
const SCENARIOS = [
  'auth.owner-setup-visible',
  'auth.initial-server-mutation',
  'auth.admin-user-create',
  'auth.pointer-logout',
  'auth.console-network-clean',
]
// Same rule as the other engines: an unmeasured scenario is NOT RUN. A
// pre-cooked failure turns a campaign that died early into a wall of red
// verdicts for checks that never executed, and the report layer can no
// longer point at the one that really broke.
const scenarioStatus = new Map(SCENARIOS.map((id) => [id, 'not_run']))
const consoleErrors = []
const consoleWarnings = []
const networkFindings = []
const requestFailures = []
const routeSummary = new Map()
const sensitiveValues = [ownerEmail, ownerPassword, userEmail, userPassword]

let browser
let context
let page
let logoutInitiatedByPointer = false
let logoutRequestCount = 0
let exitCode = 1

try {
  progress(`Browser starten: ${browserTarget}`)
  await mkdir(screenshotDirectory, { recursive: true, mode: 0o700 })
  await chmod(screenshotDirectory, 0o700)
  browser = await BROWSER_TYPES[browserTarget].launch({ headless: true })
  context = await browser.newContext({
    ignoreHTTPSErrors,
    locale: 'de-DE',
    viewport: { height: 900, width: 1_440 },
  })
  page = await context.newPage()
  installObservers(page)

  progress('Frische Ersteinrichtung verankern')
  await page.goto(baseURL, { waitUntil: 'domcontentloaded' })
  const setupDialog = page.getByRole('dialog', { name: 'Owner-Konto anlegen' })
  await setupDialog.waitFor({ state: 'visible', timeout: 30_000 })
  await setupDialog.getByRole('button', { name: 'DE', exact: true }).click()
  await captureScreenshot('01-owner-setup-empty.png')

  await setupDialog.locator('#owner-email').fill(ownerEmail)
  await setupDialog.locator('#owner-display-name').fill('Level 3 Owner')
  await setupDialog.locator('#owner-password').fill(ownerPassword)
  await setupDialog.locator('#owner-confirm').fill(ownerPassword)

  const setupResponsePromise = waitForResponse('POST', '/api/setup/owner', 30_000)
  const initialMutationPromise = waitForResponse(
    'PUT',
    '/v1/assets/default-sections',
    60_000,
  )
  await setupDialog.locator('button[type="submit"]').click()
  const setupResponse = await setupResponsePromise
  assert(
    setupResponse.status() === 201,
    `Owner setup returned HTTP ${setupResponse.status()}.`,
  )

  await setupDialog.waitFor({ state: 'hidden', timeout: 30_000 })
  await page.getByRole('button', { exact: true, name: 'Research Desk' })
    .waitFor({ state: 'visible', timeout: 30_000 })
  await page.getByRole('button', { exact: true, name: 'Angemeldet' })
    .waitFor({ state: 'visible', timeout: 30_000 })
  scenarioStatus.set('auth.owner-setup-visible', 'passed')

  const initialMutation = await initialMutationPromise
  assert(
    initialMutation.status() === 200,
    `Initial project mutation returned HTTP ${initialMutation.status()}.`,
  )
  const syncStatus = page.locator(
    '[role="status"][aria-label^="Synchronisiert:"]:visible',
  )
  await syncStatus.waitFor({ state: 'visible', timeout: 30_000 })
  assert(
    await page.locator(
      '[role="status"][aria-label^="Server-Synchronisierung fehlgeschlagen:"]:visible',
    ).count() === 0,
    'The state-owning project badge reports a synchronization error.',
  )
  scenarioStatus.set('auth.initial-server-mutation', 'passed')
  await captureScreenshot('02-owner-shell-synchronized.png')

  progress('Echte-Daten- und Benutzerverwaltung pruefen')
  await page.getByRole('button', { exact: true, name: 'Einstellungen' }).click()
  await page.getByRole('heading', { exact: true, name: 'Einstellungen' })
    .waitFor({ state: 'visible', timeout: 20_000 })
  const demoSwitch = page.getByRole('switch', { name: 'Demo-Modus' })
  await demoSwitch.waitFor({ state: 'visible' })
  assert(
    await demoSwitch.getAttribute('aria-checked') === 'false',
    'Demo mode is unexpectedly enabled in the live owner flow.',
  )
  await page.locator('aside footer')
    .getByText('Echte Daten', { exact: true })
    .waitFor({ state: 'visible' })
  await captureScreenshot('03-settings-real-data.png')

  const settingsNavigation = page.getByRole('navigation', {
    name: 'Einstellungsbereiche',
  })
  await settingsNavigation
    .getByRole('button', { exact: true, name: 'Benutzer' })
    .click()
  await page.getByRole('heading', { exact: true, name: 'Benutzer' })
    .waitFor({ state: 'visible', timeout: 20_000 })
  await page.getByRole('button', { exact: true, name: 'Benutzer anlegen' })
    .click()

  const createDialog = page.getByRole('dialog', {
    name: 'Lokalen Benutzer anlegen',
  })
  await createDialog.waitFor({ state: 'visible' })
  await createDialog.locator('#admin-create-email').fill(userEmail)
  await createDialog.locator('#admin-create-name').fill('Level 3 User')
  await createDialog.locator('#admin-create-password').fill(userPassword)
  const createResponsePromise = waitForResponse('POST', '/v1/admin/users', 30_000)
  await createDialog.getByRole('button', { exact: true, name: 'Anlegen' }).click()
  const createResponse = await createResponsePromise
  assert(
    createResponse.status() === 201,
    `Admin user creation returned HTTP ${createResponse.status()}.`,
  )

  const createdDialog = page.getByRole('dialog', { name: 'Benutzer angelegt' })
  await createdDialog.waitFor({ state: 'visible' })
  await captureScreenshot(
    '04-admin-user-created-masked.png',
    [createdDialog.locator('code')],
  )
  await createdDialog
    .getByRole('button', { exact: true, name: 'Fertig' })
    .filter({ hasText: 'Fertig' })
    .click()
  const createdRow = page.getByRole('row').filter({ hasText: userEmail })
  await createdRow.waitFor({ state: 'visible', timeout: 20_000 })
  await createdRow.getByText('Aktiv', { exact: true }).waitFor({ state: 'visible' })
  scenarioStatus.set('auth.admin-user-create', 'passed')
  await captureScreenshot('05-admin-user-table.png')

  progress('Pointer-Logout und Sitzungsende pruefen')
  const profileTrigger = page.getByRole('button', {
    exact: true,
    name: 'Angemeldet',
  })
  await pointerClick(profileTrigger)
  const profileMenu = page.getByRole('menu')
  await profileMenu.waitFor({ state: 'visible' })
  assert(
    await visibleCount(page.getByRole('tooltip')) === 0,
    'A tooltip overlays the open profile menu.',
  )
  const logoutItem = profileMenu.getByRole('menuitem', {
    exact: true,
    name: 'Abmelden',
  })
  await logoutItem.waitFor({ state: 'visible' })
  await captureScreenshot('06-profile-menu-pointer.png')

  const logoutResponsePromise = waitForResponse('POST', '/api/auth/logout', 30_000)
  logoutInitiatedByPointer = true
  await pointerClick(logoutItem)
  const logoutResponse = await logoutResponsePromise
  assert(
    logoutResponse.status() === 200,
    `Logout returned HTTP ${logoutResponse.status()}.`,
  )
  await page.getByRole('heading', { exact: true, name: 'Willkommen bei Inqtrix' })
    .waitFor({ state: 'visible', timeout: 30_000 })
  assert(logoutRequestCount === 1, 'Pointer logout did not send exactly one request.')
  assert(
    await page.getByRole('button', { exact: true, name: 'Angemeldet' }).count() === 0,
    'The authenticated profile trigger remains after logout.',
  )
  assert(
    await page.getByRole('heading', { exact: true, name: 'Owner-Konto anlegen' }).count() === 0,
    'Logout incorrectly returned to first-owner setup.',
  )
  const sessionState = await page.evaluate(async () => {
    const response = await fetch('/api/auth/session')
    if (!response.ok) return { authenticated: null, status: response.status }
    const payload = await response.json()
    return {
      authenticated: payload?.authenticated === true,
      status: response.status,
    }
  })
  assert(
    sessionState.status === 200 && sessionState.authenticated === false,
    'The server still reports an authenticated session after logout.',
  )
  scenarioStatus.set('auth.pointer-logout', 'passed')
  await captureScreenshot('07-logout-anonymous.png')

} catch (error) {
  progress(`Engine-Fehler: ${sanitize(error instanceof Error ? error.message : error)}`)
} finally {
  const classifiedConsole = classifyConsoleErrors()
  const classifiedWarnings = classifyConsoleWarnings()
  const classifiedRequestFailures = classifyRequestFailures()
  const hygieneFindings = [
    ...classifiedConsole.actionable,
    ...classifiedWarnings.actionable,
    ...classifiedRequestFailures.actionable,
    ...networkFindings,
  ]
  scenarioStatus.set(
    'auth.console-network-clean',
    hygieneFindings.length === 0 ? 'passed' : 'failed',
  )
  if (hygieneFindings.length > 0) {
    progress(`Konsole/Netzwerk nicht sauber (${hygieneFindings.length} Befunde)`)
  }
  await writeStructuralEvidence().catch(() => undefined)
  exitCode = [...scenarioStatus.values()].every((status) => status === 'passed')
    ? 0
    : 1
  await writeScenarioSidecar(
    SCENARIOS.map((id) => ({ id, status: scenarioStatus.get(id) ?? 'not_run' })),
  )
  await context?.close().catch(() => undefined)
  await browser?.close().catch(() => undefined)
}

process.exit(exitCode)

function installObservers(activePage) {
  activePage.on('console', (message) => {
    if (message.type() === 'error') {
      consoleErrors.push(`console:${sanitize(message.text())}`)
    } else if (message.type() === 'warning') {
      consoleWarnings.push({
        duringPointerLogout: logoutInitiatedByPointer,
        text: sanitize(message.text()),
      })
    }
  })
  activePage.on('pageerror', (error) => {
    consoleErrors.push(`pageerror:${sanitize(error.message)}`)
  })
  activePage.on('requestfailed', (request) => {
    const failure = request.failure()?.errorText ?? 'failed'
    const path = safePath(request.url())
    requestFailures.push({
      failure: sanitize(failure),
      method: request.method(),
      path,
    })
  })
  activePage.on('response', (response) => {
    const request = response.request()
    const path = safePath(response.url())
    const key = `${request.method()} ${path} ${response.status()}`
    if (isEvidenceRoute(path)) {
      routeSummary.set(key, (routeSummary.get(key) ?? 0) + 1)
    }
    if (request.method() === 'POST' && path === '/api/auth/logout') {
      logoutRequestCount += 1
    }
    const expectedPreferenceAbsence = (
      request.method() === 'GET'
      && path === '/v1/account/preferences'
      && response.status() === 404
    )
    if (response.status() >= 400 && !expectedPreferenceAbsence) {
      networkFindings.push(`http:${response.status()}:${request.method()}:${path}`)
    }
  })
}

function waitForResponse(method, path, timeout) {
  return page.waitForResponse(
    (response) => {
      const request = response.request()
      return request.method() === method && safePath(response.url()) === path
    },
    { timeout },
  )
}

async function pointerClick(locator) {
  await locator.scrollIntoViewIfNeeded()
  const box = await locator.boundingBox()
  assert(box && box.width > 0 && box.height > 0, 'Pointer target has no visible box.')
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.click(box.x + box.width / 2, box.y + box.height / 2)
}

async function visibleCount(locator) {
  let count = 0
  for (const candidate of await locator.all()) {
    if (await candidate.isVisible()) count += 1
  }
  return count
}

async function captureScreenshot(name, additionalMasks = []) {
  const path = join(screenshotDirectory, name)
  const masks = [
    page.getByText(ownerEmail, { exact: true }),
    page.getByText(userEmail, { exact: true }),
    ...additionalMasks,
  ]
  await page.screenshot({
    animations: 'disabled',
    fullPage: true,
    mask: masks,
    maskColor: '#20242b',
    path,
  })
  await chmod(path, 0o600)
}

async function writeStructuralEvidence() {
  const classifiedConsole = classifyConsoleErrors()
  const classifiedWarnings = classifyConsoleWarnings()
  const classifiedRequestFailures = classifyRequestFailures()
  const path = join(reportDirectory, 'owner-setup-evidence.json')
  const temporaryPath = `${path}.tmp`
  const payload = `${JSON.stringify({
    browser: browserTarget,
    consoleErrors: classifiedConsole.actionable.map(sanitize),
    consoleWarnings: classifiedWarnings.actionable.map(sanitize),
    explainedConsoleErrors: classifiedConsole.explained,
    explainedConsoleWarnings: classifiedWarnings.explained,
    explainedNetworkFailures: classifiedRequestFailures.explained,
    logoutRequestCount,
    networkFindings: [
      ...networkFindings,
      ...classifiedRequestFailures.actionable,
    ].map(sanitize),
    routes: Object.fromEntries([...routeSummary.entries()].sort()),
    runId,
    schemaVersion: 1,
  }, null, 2)}\n`
  await writeFile(temporaryPath, payload, { encoding: 'utf8', mode: 0o600 })
  await rename(temporaryPath, path)
  await chmod(path, 0o600)
}

async function writeScenarioSidecar(scenarios) {
  const path = process.env.INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH
  if (!path) return
  const temporaryPath = `${path}.tmp`
  const payload = `${JSON.stringify({ scenarios, schemaVersion: 1 }, null, 2)}\n`
  await writeFile(temporaryPath, payload, { encoding: 'utf8', mode: 0o600 })
  await rename(temporaryPath, path)
  await chmod(path, 0o600)
}

function isEvidenceRoute(path) {
  return [
    '/api/auth/config',
    '/api/auth/logout',
    '/api/auth/session',
    '/api/setup/owner',
    '/v1/account/preferences',
    '/v1/admin/users',
    '/v1/assets/default-sections',
    '/v1/user/events',
  ].includes(path)
}

function classifyConsoleErrors() {
  let expectedPreferenceAbsence = routeSummary.get(
    'GET /v1/account/preferences 404',
  ) ?? 0
  let explained = 0
  const actionable = []
  for (const finding of consoleErrors) {
    if (
      expectedPreferenceAbsence > 0
      && finding === 'console:Failed to load resource: the server responded with a status of 404 (Not Found)'
    ) {
      expectedPreferenceAbsence -= 1
      explained += 1
    } else {
      actionable.push(finding)
    }
  }
  return { actionable, explained }
}

function classifyConsoleWarnings() {
  const logoutStreamTerminationIsAnchored = (
    scenarioStatus.get('auth.pointer-logout') === 'passed'
    && logoutRequestCount === 1
    && (routeSummary.get('POST /api/auth/logout 200') ?? 0) === 1
    && (routeSummary.get('GET /v1/user/events 200') ?? 0) >= 1
  )
  let expectedLogoutStreamWarning = logoutStreamTerminationIsAnchored ? 1 : 0
  let explained = 0
  const actionable = []
  for (const warning of consoleWarnings) {
    if (
      expectedLogoutStreamWarning > 0
      && warning.duringPointerLogout
      && warning.text.startsWith('User invalidation stream failed; reconnecting.')
    ) {
      expectedLogoutStreamWarning -= 1
      explained += 1
    } else {
      actionable.push(warning.text)
    }
  }
  return { actionable, explained }
}

function classifyRequestFailures() {
  let explained = 0
  const actionable = []
  for (const failure of requestFailures) {
    const normalized = failure.failure.toLowerCase()
    const expectedReadCancellation = (
      failure.method === 'GET'
      && (normalized.includes('aborted') || normalized.includes('cancelled'))
    )
    if (expectedReadCancellation) {
      explained += 1
    } else {
      actionable.push(
        `requestfailed:${failure.failure}:${failure.method}:${failure.path}`,
      )
    }
  }
  return { actionable, explained }
}

function safePath(value) {
  try {
    return new URL(value).pathname
  } catch {
    return '[invalid-path]'
  }
}

function requiredEnvironment(name) {
  const value = process.env[name]?.trim()
  if (!value) throw new Error(`${name} is required.`)
  return value
}

function requiredBrowserTarget(value) {
  if (value !== 'chromium' && value !== 'firefox' && value !== 'webkit') {
    throw new Error('The browser target must be chromium, firefox, or webkit.')
  }
  return value
}

function assert(condition, message) {
  if (!condition) throw new Error(message)
}

function progress(message) {
  console.log(`[owner-setup-live] ${sanitize(message)}`)
}

function sanitize(value) {
  let output = String(value)
    .replaceAll(/https?:\/\/\S+/g, '[url]')
    .replaceAll(/[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+/gi, '[email]')
    .replaceAll(/\b[0-9a-f]{8}-[0-9a-f-]{27,}\b/gi, '[id]')
  for (const sensitive of sensitiveValues) {
    output = output.replaceAll(sensitive, '[redacted]')
  }
  return output.slice(0, 400)
}
