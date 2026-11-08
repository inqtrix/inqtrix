import assert from 'node:assert/strict'
import test from 'node:test'

import { attemptCleanup, ignoreMissing } from './cleanup-step.mjs'

test('a failed cleanup step is reported without its message and does not throw', async () => {
  const lines = []
  const ok = await attemptCleanup(
    'document fixture',
    async () => {
      throw new Error('Cookie: session=abc returned HTTP 503 for the document.')
    },
    (line) => lines.push(line),
  )

  assert.equal(ok, false)
  assert.deepEqual(lines, [
    '[cleanup] document fixture failed: Error, HTTP 503\n',
  ])
})

test('a successful cleanup step stays silent', async () => {
  const lines = []
  const ok = await attemptCleanup('logout', async () => undefined, (line) => lines.push(line))

  assert.equal(ok, true)
  assert.deepEqual(lines, [])
})

test('only a missing file counts as removed', () => {
  assert.doesNotThrow(() => ignoreMissing(Object.assign(new Error('gone'), { code: 'ENOENT' })))
  assert.throws(
    () => ignoreMissing(Object.assign(new Error('denied'), { code: 'EACCES' })),
    /denied/,
  )
})
