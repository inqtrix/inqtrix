import assert from 'node:assert/strict'
import { test } from 'node:test'

import { fetchActorJson, isExpectedBrowserRequestAbort } from './api.mjs'

function actorWith(fetch) {
  return {
    context: { request: { fetch } },
    csrf: 'csrf-header-value',
    label: 'fixture admin',
    user: { id: '00000000-0000-4000-8000-000000000001' },
    workspaceId: 'workspace-1',
  }
}

test('a failed request names only the request and how it failed', async () => {
  // Playwright appends the call log with every request header.
  const callLog = [
    'apiRequestContext.fetch: Timeout 30000ms exceeded.',
    'Call log:',
    '  - → DELETE http://127.0.0.1:8080/v1/admin/users/account-1',
    '  -   x-csrf-token: csrf-header-value',
    '  -   cookie: inqtrix_session=session-cookie-value; inqtrix_csrf=csrf-cookie-value',
  ].join('\n')
  const actor = actorWith(async () => {
    throw new Error(callLog)
  })

  const failure = await fetchActorJson(actor, 'DELETE', '/v1/admin/users/account-1').then(
    () => null,
    (error) => error,
  )

  assert.equal(failure.message, 'fixture admin DELETE /v1/admin/users/account-1 timed out')
  assert.equal(failure.cause, undefined)
  assert.doesNotMatch(
    String(failure.stack),
    /session-cookie-value|csrf-header-value|csrf-cookie-value/,
  )
})

test('a resource that belongs to no workspace is read without one', async () => {
  const sent = []
  const actor = actorWith(async (_path, options) => {
    sent.push(options.headers)
    return { status: () => 200, text: async () => '{}' }
  })

  await fetchActorJson(actor, 'GET', '/v1/deletion-operations/del_1', { workspace: false })
  await fetchActorJson(actor, 'GET', '/v1/assets')

  assert.equal('X-Inqtrix-Workspace-Id' in sent[0], false)
  assert.equal(sent[1]['X-Inqtrix-Workspace-Id'], 'workspace-1')
})

test('browser navigation cancellation codes have one shared meaning', () => {
  for (const code of ['net::ERR_ABORTED', 'ERR_ABORTED', 'NS_BINDING_ABORTED', 'cancelled', 'Load request cancelled']) {
    assert.equal(isExpectedBrowserRequestAbort(code), true)
    assert.equal(isExpectedBrowserRequestAbort(`request:/v1/runs:${code}`), true)
  }
})

test('network failures and timeouts remain findings', () => {
  for (const code of ['NS_ERROR_CONNECTION_REFUSED', 'net::ERR_CONNECTION_RESET', 'Timeout', 'net::ERR_FAILED']) {
    assert.equal(isExpectedBrowserRequestAbort(code), false)
    assert.equal(isExpectedBrowserRequestAbort(`request:/v1/runs:${code}`), false)
  }
})

test('cancellation words in console, HTTP errors or paths are not an abort', () => {
  for (const finding of ['console:cancelled', 'response:500:/cancelled',
    'request:/NS_BINDING_ABORTED:net::ERR_FAILED', 'Unexpected operation cancelled']) {
    assert.equal(isExpectedBrowserRequestAbort(finding), false)
  }
})
