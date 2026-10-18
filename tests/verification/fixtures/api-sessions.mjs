import { request } from '@playwright/test'

import { assertFixture } from './api.mjs'

export function createApiSessionFixtures({
  baseURL,
  ignoreHTTPSErrors,
  lifecycle,
  runId,
}) {
  const actors = new Set()

  async function loginActor(email, password, label, credential = 'user') {
    const api = await request.newContext({
      baseURL,
      ignoreHTTPSErrors,
    })
    let lifecycleSession
    try {
      const login = await api.post('/api/auth/login/local', {
        data: { email, password },
      })
      assertFixture(
        login.status() === 200,
        `${label} login returned HTTP ${login.status()}.`,
      )
      lifecycleSession = await lifecycle.registerSession(api, label)
      let session = await readSession(api, label)
      if (!session.project_namespace) {
        const candidate = `e2e-${runId}-${label}`
          .toLowerCase()
          .replaceAll(/[^a-z0-9_-]+/g, '-')
          .slice(0, 80)
        const adoption = await api.get('/api/auth/session', {
          headers: { 'X-Inqtrix-Workspace-Id': candidate },
        })
        assertFixture(
          adoption.status() === 200,
          `${label} workspace adoption failed.`,
        )
        session = await adoption.json()
      }
      assertFixture(
        typeof session.project_namespace === 'string'
          && session.project_namespace.length >= 8,
        `${label} has no canonical project namespace.`,
      )
      const actor = {
        api,
        context: { request: api },
        credential,
        csrf: session.csrf_token,
        email,
        label,
        lifecycleSession,
        user: session.user,
        workspaceId: session.project_namespace,
      }
      actors.add(actor)
      return actor
    } catch (error) {
      if (!lifecycleSession) {
        await logoutUnregisteredApiSession(api).catch(() => undefined)
      }
      await api.dispose().catch(() => undefined)
      throw error
    }
  }

  async function logoutActor(actor) {
    if (!actor || !actors.has(actor)) return
    try {
      const response = await actor.api.post('/api/auth/logout', {
        headers: { 'X-CSRF-Token': actor.csrf },
      })
      assertFixture(
        [200, 401].includes(response.status()),
        `${actor.label} logout returned HTTP ${response.status()}.`,
      )
      await lifecycle.completeSession(actor.lifecycleSession)
      actor.lifecycleSession = null
    } finally {
      actors.delete(actor)
      await actor.api.dispose()
    }
  }

  async function closeAll() {
    for (const actor of [...actors].reverse()) {
      await logoutActor(actor).catch(() => undefined)
    }
  }

  return {
    closeAll,
    loginActor,
    logoutActor,
  }
}

async function readSession(api, label) {
  const response = await api.get('/api/auth/session')
  assertFixture(
    response.status() === 200,
    `${label} session lookup returned HTTP ${response.status()}.`,
  )
  const session = await response.json()
  assertFixture(
    typeof session.csrf_token === 'string'
      && session.csrf_token.length > 0
      && typeof session.user?.id === 'string'
      && session.user.id.length > 0,
    `${label} session is incomplete.`,
  )
  return session
}

async function logoutUnregisteredApiSession(api) {
  const response = await api.get('/api/auth/session')
  if (response.status() !== 200) return
  const session = await response.json()
  if (typeof session.csrf_token !== 'string' || !session.csrf_token) return
  await api.post('/api/auth/logout', {
    headers: { 'X-CSRF-Token': session.csrf_token },
  })
}
