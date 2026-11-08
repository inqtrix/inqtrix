import assert from 'node:assert/strict'
import test from 'node:test'

import {
  ensureTemporaryUsers,
  eraseTemporaryUser,
  requireAccountLocale,
  setAccountLocale,
  temporaryUserBelongsToRun,
  temporaryUserDescriptors,
} from './accounts.mjs'

test('temporary user provisioning derives default identities from its run', async () => {
  const runId = 'inqv-account-default-identities'
  const expectedDescriptors = temporaryUserDescriptors(runId)
  const registrations = []
  const requests = []
  const actor = {
    context: {
      request: {
        async fetch(path, options) {
          requests.push({ method: options.method, path })
          if (options.method === 'GET') return jsonResponse(200, { users: [] })
          return jsonResponse(201, {
            email: options.data.email,
            id: `user-${requests.length}`,
          })
        },
      },
    },
    csrf: 'csrf',
    label: 'fixture owner',
    user: { id: 'owner' },
    workspaceId: 'workspace',
  }
  const lifecycle = {
    async register(resource) {
      registrations.push(resource)
      return `cleanup-${registrations.length}`
    },
  }

  const users = await ensureTemporaryUsers({
    adminActor: actor,
    lifecycle,
    password: 'fixture password',
    runId,
  })

  assert.deepEqual(
    users.map(({ displayName, email }) => ({ displayName, email })),
    expectedDescriptors,
  )
  assert.deepEqual(
    registrations.map(({ email, id, kind }) => ({ email, id, kind })),
    expectedDescriptors.map(({ email }) => ({
      email,
      id: `${runId}:${email}`,
      kind: 'temporary_user',
    })),
  )
  assert.equal(requests.filter(({ method }) => method === 'GET').length, 1)
  assert.equal(requests.filter(({ method }) => method === 'POST').length, 4)
})

test('temporary user scope supports the 24-user soak cohort without widening runs', () => {
  const firstRun = 'inqv-account-soak-identities-a'
  const secondRun = 'inqv-account-soak-identities-b'
  const descriptors = temporaryUserDescriptors(firstRun, 24)
  assert.equal(descriptors.length, 24)
  assert.equal(new Set(descriptors.map(({ email }) => email)).size, 24)
  assert(descriptors.every(({ email }) => temporaryUserBelongsToRun(email, firstRun)))
  assert(descriptors.every(({ email }) => !temporaryUserBelongsToRun(email, secondRun)))
  assert.throws(() => temporaryUserDescriptors(firstRun, 25), /at most 24/)
})

/** An admin actor over a scripted server: the account erasure answers 202,
 * then each read of its operation takes the next state. */
function erasureWorld({ deleteStatus = 202, listed = [], reads = [] } = {}) {
  const requests = []
  const completed = []
  const actor = {
    context: {
      request: {
        async fetch(path, options) {
          requests.push({
            method: options.method,
            path,
            workspace: options.headers['X-Inqtrix-Workspace-Id'] ?? null,
          })
          if (options.method === 'DELETE') {
            return deleteStatus === 202
              ? jsonResponse(202, { operation_id: 'del_1', status: 'queued' })
              : jsonResponse(404, { error: { type: 'not_found' } })
          }
          if (path === '/v1/admin/users') return jsonResponse(200, { users: listed })
          return jsonResponse(200, { operation_id: 'del_1', ...reads.shift() })
        },
      },
    },
    csrf: 'csrf',
    label: 'fixture admin',
    user: { id: 'admin' },
    workspaceId: 'workspace',
  }
  const lifecycle = {
    async complete(handle) {
      completed.push([handle, requests.length])
    },
  }
  const user = { cleanupHandle: 'cleanup-1', id: 'user-1' }
  const erase = () =>
    eraseTemporaryUser(actor, user, lifecycle, { sleep: async () => undefined })
  return { completed, erase, requests }
}

test('a temporary account is erased and its cleanup completes once it is gone', async () => {
  const world = erasureWorld({ reads: [{ status: 'running' }, { status: 'deleted' }] })

  await world.erase()

  assert.deepEqual(world.requests, [
    { method: 'DELETE', path: '/v1/admin/users/user-1', workspace: 'workspace' },
    // The erasure belongs to no workspace; a read naming one would 404.
    { method: 'GET', path: '/v1/deletion-operations/del_1', workspace: null },
    { method: 'GET', path: '/v1/deletion-operations/del_1', workspace: null },
    { method: 'GET', path: '/v1/admin/users', workspace: 'workspace' },
  ])
  assert.deepEqual(world.completed, [['cleanup-1', 4]])
})

test('a failed erasure leaves the cleanup open and says only how it failed', async () => {
  const world = erasureWorld({
    reads: [{ error: { message: 'internal detail', type: 'server_error' }, status: 'delete_failed' }],
  })

  await assert.rejects(world.erase(), (error) => {
    assert.equal(error.message, 'Temporary account erasure failed (server_error).')
    return true
  })
  assert.deepEqual(world.completed, [])
})

test('an account that is already gone completes its cleanup once the listing confirms it', async () => {
  const world = erasureWorld({ deleteStatus: 404 })

  await world.erase()

  assert.equal(world.requests.length, 2)
  assert.deepEqual(world.completed, [['cleanup-1', 2]])
})

test('a 404 for an account that is still listed leaves the cleanup open', async () => {
  const world = erasureWorld({ deleteStatus: 404, listed: [{ id: 'user-1' }] })

  await assert.rejects(world.erase(), /still listed/)

  assert.deepEqual(world.completed, [])
})

function preferenceWorld(saved, { serverLocale } = {}) {
  const requests = []
  let row = saved
  const actor = {
    context: {
      request: {
        async fetch(path, options) {
          requests.push({ data: options.data, method: options.method, path })
          if (options.method === 'PUT') {
            row = { ...options.data, ...(serverLocale ? { locale: serverLocale } : {}) }
            return jsonResponse(200, row)
          }
          return row
            ? jsonResponse(200, row)
            : jsonResponse(404, { error: { type: 'not_found' } })
        },
      },
    },
    csrf: 'csrf',
    label: 'Fixture account',
    user: { id: 'user-1' },
    workspaceId: 'workspace',
  }
  return { actor, requests }
}

test('an account the run created gets the run language saved and read back', async () => {
  const world = preferenceWorld(null)

  await setAccountLocale(world.actor, 'de', { now: () => 1_700_000_000_500 })

  assert.deepEqual(world.requests, [
    {
      data: { locale: 'de', updated_at: 1_700_000_000 },
      method: 'PUT',
      path: '/v1/account/preferences',
    },
    { data: undefined, method: 'GET', path: '/v1/account/preferences' },
  ])

  const ignored = preferenceWorld(null, { serverLocale: 'en' })
  await assert.rejects(setAccountLocale(ignored.actor, 'de'), (error) => {
    assert.equal(
      error.message,
      "Fixture account kept the interface language 'en' after saving 'de'.",
    )
    return true
  })
})

test('a borrowed account is only read and must already show the run language', async () => {
  const matching = preferenceWorld({ locale: 'de', theme: 'dark' })
  await requireAccountLocale(matching.actor, 'de')
  assert.deepEqual(matching.requests.map(({ method }) => method), ['GET'])

  const other = preferenceWorld({ locale: 'en', theme: 'dark' })
  await assert.rejects(requireAccountLocale(other.actor, 'de'), (error) => {
    assert.equal(
      error.message,
      "Fixture account has the interface language 'en'; the run expects 'de'. Set it in that account's settings.",
    )
    return true
  })

  const unsaved = preferenceWorld(null)
  await assert.rejects(requireAccountLocale(unsaved.actor, 'de'), (error) => {
    assert.equal(
      error.message,
      "Fixture account has no saved interface language; the run expects 'de'. Set it in that account's settings.",
    )
    return true
  })
  assert.deepEqual(
    [...other.requests, ...unsaved.requests].map(({ method }) => method),
    ['GET', 'GET'],
  )
})

function jsonResponse(status, body) {
  return {
    status: () => status,
    text: async () => JSON.stringify(body),
  }
}
