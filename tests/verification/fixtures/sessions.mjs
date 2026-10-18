import { join } from 'node:path'

import { assertFixture, isExpectedBrowserRequestAbort, redactPath } from './api.mjs'

export function createSessionFixtures({
  baseURL,
  browser,
  ignoreHTTPSErrors,
  lifecycle,
  parseCollaborationFrame,
  runId,
  screenshotDirectory,
}) {
  const guestContexts = []

  async function loginActor(email, password, label, credential = 'user') {
    const context = await browser.newContext({
      baseURL,
      ignoreHTTPSErrors,
      locale: 'de-DE',
      viewport: { width: 1440, height: 1000 },
    })
    let lifecycleSession
    try {
      const login = await context.request.post('/api/auth/login/local', {
        data: { email, password },
      })
      assertFixture(
        login.status() === 200,
        `${label} login returned HTTP ${login.status()}.`,
      )
      const sessionResponse = await context.request.get('/api/auth/session')
      assertFixture(
        sessionResponse.status() === 200,
        `${label} session lookup failed.`,
      )
      let session = await sessionResponse.json()
      if (!session.project_namespace) {
        const candidate = `e2e-${runId}-${label}`
          .toLowerCase()
          .replaceAll(/[^a-z0-9_-]+/g, '-')
          .slice(0, 80)
        const adoptionResponse = await context.request.get('/api/auth/session', {
          headers: { 'X-Inqtrix-Workspace-Id': candidate },
        })
        assertFixture(
          adoptionResponse.status() === 200,
          `${label} workspace adoption failed.`,
        )
        session = await adoptionResponse.json()
      }
      assertFixture(
        typeof session.project_namespace === 'string'
          && session.project_namespace.length >= 8,
        `${label} has no canonical project namespace.`,
      )
      lifecycleSession = await lifecycle.registerSession(context, label)
      const page = await context.newPage()
      const errors = []
      const browserObservations = { responses: 0 }
      const durabilityTrace = {
        acknowledgements: [],
        sentUpdates: [],
        updatePayloads: new Map(),
      }
      page.on('websocket', (socket) => {
        socket.on('framesent', ({ payload }) => {
          const parsed = parseCollaborationFrame(payload)
          if (parsed?.kind === 'update') {
            durabilityTrace.sentUpdates.push(parsed.hash)
            durabilityTrace.updatePayloads.set(parsed.hash, parsed.update)
          }
        })
        socket.on('framereceived', ({ payload }) => {
          const parsed = parseCollaborationFrame(payload)
          if (parsed?.kind === 'durable_ack') {
            durabilityTrace.acknowledgements.push(parsed.hash)
          }
        })
      })
      page.on('pageerror', (error) => errors.push(`pageerror:${error.message}`))
      page.on('console', (message) => {
        const value = message.text()
        if (
          message.type() === 'error'
          && !value.startsWith(
            'Failed to load resource: the server responded with a status of 404',
          )
        ) {
          errors.push(`console:${value}`)
        }
      })
      page.on('requestfailed', (request) => {
        const failure = request.failure()?.errorText ?? ''
        if (!isExpectedBrowserRequestAbort(failure)) {
          errors.push(`request:${new URL(request.url()).pathname}:${failure}`)
        }
      })
      page.on('response', (response) => {
        browserObservations.responses += 1
        if (response.status() >= 500) {
          errors.push(
            `response:${response.status()}:${new URL(response.url()).pathname}`,
          )
        }
      })
      return {
        context,
        credential,
        csrf: session.csrf_token,
        browserObservations,
        durabilityTrace,
        email,
        errors,
        label,
        lifecycleSession,
        page,
        user: session.user,
        workspaceId: session.project_namespace,
      }
    } catch (error) {
      if (lifecycleSession) {
        await logoutContext(context, lifecycleSession).catch(() => undefined)
      }
      await context.close().catch(() => undefined)
      throw error
    }
  }

  async function logoutActor(actor) {
    if (!actor?.context || !actor.lifecycleSession) return
    await logoutContext(actor.context, actor.lifecycleSession, actor.csrf)
    actor.lifecycleSession = null
  }

  async function logoutContext(context, lifecycleSession, csrf = '') {
    let csrfToken = csrf
    if (!csrfToken) {
      const session = await context.request.get('/api/auth/session')
      if (session.status() === 200) {
        csrfToken = (await session.json()).csrf_token ?? ''
      }
    }
    if (csrfToken) {
      const response = await context.request.post('/api/auth/logout', {
        headers: { 'X-CSRF-Token': csrfToken },
      })
      assertFixture(
        [200, 401].includes(response.status()),
        `Account logout returned HTTP ${response.status()}.`,
      )
    }
    await lifecycle.completeSession(lifecycleSession)
  }

  async function newGuestContext() {
    const context = await browser.newContext({
      baseURL,
      ignoreHTTPSErrors,
      locale: 'de-DE',
      viewport: { width: 1440, height: 1000 },
    })
    guestContexts.push(context)
    return context
  }

  async function openGuestLink(link, displayName) {
    const context = await newGuestContext()
    const page = await context.newPage()
    const aiRequests = []
    const browserErrors = []
    const failedRequests = []
    const endpointResponses = []
    const sockets = []
    const result = {
      aiRequests,
      browserErrors,
      context,
      csrf: '',
      endpointResponses,
      failedRequests,
      headersVerified: false,
      link,
      page,
      secureCookies: false,
      socketClosed: false,
      sockets,
    }
    page.on('request', (request) => {
      const path = new URL(request.url()).pathname
      if (/\/v1\/(?:chat|agent|runs)/.test(path)) aiRequests.push(path)
    })
    page.on('requestfailed', (request) => {
      failedRequests.push({
        error: sanitizeGuestDiagnostic(
          request.failure()?.errorText ?? 'request failed',
        ),
        method: request.method(),
        path: redactPath(new URL(request.url()).pathname),
      })
    })
    page.on('response', (response) => {
      const path = new URL(response.url()).pathname
      if (response.status() >= 400 || path.startsWith('/v1/editor/guest/')) {
        endpointResponses.push({
          path: redactPath(path),
          status: response.status(),
        })
      }
    })
    page.on('console', (message) => {
      if (!['error', 'warning'].includes(message.type())) return
      browserErrors.push(
        sanitizeGuestDiagnostic(`${message.type()}: ${message.text()}`),
      )
    })
    page.on('pageerror', (error) => {
      browserErrors.push(
        sanitizeGuestDiagnostic(`pageerror: ${error.stack ?? error.message}`),
      )
    })
    page.on('websocket', (socket) => {
      if (new URL(socket.url()).pathname !== '/collaboration') return
      sockets.push(socket)
      socket.on('close', () => {
        result.socketClosed = true
      })
    })
    const navigation = await page.goto(link.url, {
      waitUntil: 'domcontentloaded',
    })
    assertFixture(
      navigation?.status() === 200,
      'Guest page did not return HTTP 200.',
    )
    assertGuestSecurityHeaders(navigation.headers())
    result.headersVerified = true
    if (displayName !== null) {
      await page.getByLabel('Ihr Anzeigename').fill(displayName)
    }
    await page.getByLabel('Link-Passwort').fill(link.password)
    await page.getByRole('button', { name: 'Dokument öffnen' }).click()
    try {
      await page.locator('.editor-prose').first().waitFor({
        state: 'visible',
        timeout: 30_000,
      })
    } catch (error) {
      const screenshot = join(
        screenshotDirectory,
        `${runId}-guest-${link.permission}-failure.png`,
      )
      await page.screenshot({ fullPage: true, path: screenshot })
      const dom = await page.evaluate(() => ({
        bodyText: document.body.innerText.slice(0, 1_500),
        contentEditable: Array.from(
          document.querySelectorAll('[contenteditable]'),
        ).map((element) => ({
          className: element.className,
          contentEditable: element.contentEditable,
          tagName: element.tagName,
        })),
        title: document.title,
      }))
      throw new Error(
        `Guest ${link.permission} workspace did not render: ${JSON.stringify({
          browserErrors,
          dom,
          endpointResponses,
          failedRequests,
          screenshot,
          sockets: sockets.length,
        })}`,
        { cause: error },
      )
    }
    await page.getByText('Verbunden', { exact: true }).waitFor({
      state: 'visible',
      timeout: 30_000,
    })
    await waitUntil(
      () => sockets.length > 0,
      10_000,
      `guest ${link.permission} WebSocket`,
    )
    const cookies = await context.cookies()
    const sessionCookie = cookies.find(
      (cookie) => cookie.name === 'inqtrix_editor_guest',
    )
    const csrfCookie = cookies.find(
      (cookie) => cookie.name === 'inqtrix_editor_guest_csrf',
    )
    assertFixture(
      sessionCookie?.secure === true
        && sessionCookie.httpOnly === true
        && csrfCookie?.secure === true
        && csrfCookie.httpOnly === false,
      `Guest ${link.permission} cookies do not satisfy the Secure/HttpOnly contract.`,
    )
    result.csrf = csrfCookie.value
    result.secureCookies = true
    return result
  }

  async function guestFetch(context, method, path, options = {}) {
    const response = await context.request.fetch(path, {
      data: options.data,
      headers: options.headers,
      method,
    })
    const text = await response.text()
    let body = null
    if (text) {
      try {
        body = JSON.parse(text)
      } catch {
        body = { text: text.slice(0, 500) }
      }
    }
    const expected = options.expected ?? [200]
    if (!expected.includes(response.status())) {
      throw new Error(
        `Guest ${method} ${redactPath(path)} returned HTTP `
        + `${response.status()}: ${JSON.stringify(body)}`,
      )
    }
    return {
      body,
      headers: response.headers(),
      status: response.status(),
    }
  }

  async function guestCsrf(context) {
    const cookie = (await context.cookies()).find(
      (candidate) => candidate.name === 'inqtrix_editor_guest_csrf',
    )
    assertFixture(cookie?.value, 'Guest CSRF cookie is missing.')
    return cookie.value
  }

  async function closeGuestContexts() {
    await Promise.allSettled(
      guestContexts.map((context) => context.close()),
    )
  }

  return {
    closeGuestContexts,
    guestCsrf,
    guestFetch,
    loginActor,
    logoutActor,
    newGuestContext,
    openGuestLink,
  }
}

export function sanitizeGuestDiagnostic(value) {
  return String(value)
    .replace(/egl1\.[A-Za-z0-9._~-]+/g, '[REDACTED]')
    .replace(/\/s\/[^/?#\s]+/g, '/s/[REDACTED]')
    .slice(0, 4_000)
}

export function assertGuestSecurityHeaders(headers) {
  assertFixture(
    headers['cache-control'] === 'no-store'
      && headers['referrer-policy'] === 'no-referrer'
      && headers['x-content-type-options'] === 'nosniff',
    `Guest response is missing mandatory security headers: `
    + `${JSON.stringify({
      cacheControl: headers['cache-control'],
      contentTypeOptions: headers['x-content-type-options'],
      referrerPolicy: headers['referrer-policy'],
    })}`,
  )
}

async function waitUntil(predicate, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await predicate()) return
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error(`Timed out waiting for ${label}.`)
}
