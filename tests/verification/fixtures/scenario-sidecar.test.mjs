import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'

import { createScenarioBook, scenarioResultProblem, writeScenarioSidecar } from './scenario-sidecar.mjs'

function withResultsPath(run) {
  const directory = mkdtempSync(join(tmpdir(), 'inqtrix-scenario-sidecar-'))
  const path = join(directory, 'scenarios.json')
  return Promise.resolve(run(path, { INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH: path }))
    .finally(() => rmSync(directory, { force: true, recursive: true }))
}

test('measured, unmeasured and skipped scenarios are written with their reasons', () => withResultsPath(async (path, environment) => {
  await writeScenarioSidecar([
    { id: 'a.measured', status: 'passed' },
    { id: 'a.red', status: 'failed' },
    { id: 'a.later', status: 'not_run' },
    { id: 'a.left-out', reason: 'Guest links need HTTPS.', status: 'skipped' },
  ], environment)

  assert.deepEqual(JSON.parse(readFileSync(path, 'utf8')), {
    scenarios: [
      { id: 'a.measured', status: 'passed' },
      { id: 'a.red', status: 'failed' },
      { id: 'a.later', status: 'not_run' },
      { id: 'a.left-out', reason: 'Guest links need HTTPS.', status: 'skipped' },
    ],
    schemaVersion: 1,
  })
  assert.equal(statSync(path).mode & 0o777, 0o600)
}))

test('a skip without its reason is refused at the engine', () => withResultsPath(async (path, environment) => {
  await assert.rejects(
    writeScenarioSidecar([{ id: 'a.left-out', status: 'skipped' }], environment),
    /a\.left-out is skipped without a reason/,
  )
  assert.throws(() => readFileSync(path), { code: 'ENOENT' })
}))

test('an unknown status, an empty reason and a repeated id are refused', async () => {
  assert.equal(scenarioResultProblem({ id: 'a', status: 'pending' }), 'a has the unknown status pending')
  assert.equal(scenarioResultProblem({ id: 'a', reason: ' ', status: 'skipped' }), 'a has an empty reason')
  assert.equal(scenarioResultProblem({ status: 'passed' }), 'an entry has no id')
  assert.equal(scenarioResultProblem({ id: 'a', status: 'not_run' }), null)
  await withResultsPath(async (_path, environment) => {
    await assert.rejects(
      writeScenarioSidecar([
        { id: 'a', status: 'passed' },
        { id: 'a', status: 'failed' },
      ], environment),
      /a appears twice/,
    )
  })
})

test('without a results path nothing is written', async () => {
  await writeScenarioSidecar([{ id: 'a', status: 'passed' }], {})
})

test('a failure is never replaced by a later pass and keeps its first reason', () => {
  const book = createScenarioBook(['a', 'b', 'c'])
  book.fail('a', 'R2 answered without a source')
  book.fail('a', 'a second reason')
  book.pass('a')
  book.pass('b')
  assert.deepEqual(book.entries(), [
    { id: 'a', reason: 'R2 answered without a source', status: 'failed' },
    { id: 'b', status: 'passed' },
    { id: 'c', status: 'not_run' },
  ])
  assert.equal(book.allPassed(), false)
})

test('a skip needs its reason, does not hide a failure, and counts as covered only with one', () => {
  const book = createScenarioBook(['guest', 'matrix'])
  assert.throws(() => book.skip('guest', ' '), /needs a reason/)
  book.skip('guest', 'Guest links need HTTPS.')
  book.pass('matrix')
  assert.equal(book.allPassed(), true)
  assert.deepEqual(book.entries()[0], { id: 'guest', reason: 'Guest links need HTTPS.', status: 'skipped' })
  book.fail('matrix', 'browser errors')
  book.skip('matrix', 'too late')
  assert.equal(book.status('matrix'), 'failed')
  assert.throws(() => book.pass('unknown'), /Unknown scenario unknown/)
})
