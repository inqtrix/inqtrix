import assert from 'node:assert/strict'
import { test } from 'node:test'

import { fixtureWorkers } from './fixture-workers.ts'

test('fixture workers follow the memory before the cores', () => {
  const gib = 1024 ** 3

  assert.equal(fixtureWorkers(19 * gib, 10), 3)
  assert.equal(fixtureWorkers(64 * gib, 10), 5)
  assert.equal(fixtureWorkers(4 * gib, 10), 1)
  assert.equal(fixtureWorkers(64 * gib, 1), 1)
})
