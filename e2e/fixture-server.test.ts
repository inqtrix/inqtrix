import assert from 'node:assert/strict'
import { test } from 'node:test'

import { fixtureWebServer } from './fixture-server.ts'

test('a fixture run starts its own server and never takes over a running one', () => {
  const server = fixtureWebServer({}, '/app')

  assert.equal(server.reuseExistingServer, false)
  assert.equal(server.baseURL, 'http://127.0.0.1:5187')
  assert.match(server.command, /--port 5187 --strictPort$/)
  assert.equal(server.cwd, '/app')
})

test('a working copy runs its fixtures on its own port', () => {
  const server = fixtureWebServer({ INQTRIX_UI_FIXTURE_PORT: ' 5299 ' }, '/app')

  assert.equal(server.baseURL, 'http://127.0.0.1:5299')
  assert.match(server.command, /--port 5299 --strictPort$/)
  assert.equal(server.url, 'http://127.0.0.1:5299/browser-tests/fixtures/editor-lifecycle.html')
  assert.equal(server.reuseExistingServer, false)
})

test('a port setting that is no port stops the run before a browser starts', () => {
  for (const value of ['abc', '80', '70000', '5187.5']) {
    assert.throws(
      () => fixtureWebServer({ INQTRIX_UI_FIXTURE_PORT: value }, '/app'),
      /INQTRIX_UI_FIXTURE_PORT/,
    )
  }
})
