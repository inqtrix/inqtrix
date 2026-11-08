import assert from 'node:assert/strict'
import { test } from 'node:test'

import { cancelOwnedRuns, finishRecoveryRun } from './agent-recovery.mjs'

/** Cleanup steps that record which ones failed instead of writing to stderr. */
function recordingAttempt(failed) {
  return async (label, step) => {
    try {
      await step()
      return true
    } catch {
      failed.push(label)
      return false
    }
  }
}

test('a failed cancel leaves the other runs and the cleanup after it standing', async () => {
  const handled = []
  const failed = []

  await cancelOwnedRuns(['run_a', 'run_b', 'run_c'], {
    cancel: async (id) => {
      handled.push(`cancel ${id}`)
      if (id === 'run_a') throw new Error('HTTP 500')
    },
    read: async (id) => ({ status: id === 'run_c' ? 'completed' : 'running' }),
    terminal: async (id) => {
      handled.push(`terminal ${id}`)
    },
  }, recordingAttempt(failed))

  assert.deepEqual(handled, ['cancel run_a', 'cancel run_b', 'terminal run_b'])
  assert.deepEqual(failed, ['run cancel'])
})

test('a run whose state cannot be read is not cancelled blind', async () => {
  const handled = []
  const failed = []

  await cancelOwnedRuns(['run_a'], {
    cancel: async (id) => {
      handled.push(`cancel ${id}`)
    },
    read: async () => {
      throw new Error('HTTP 503')
    },
    terminal: async () => undefined,
  }, recordingAttempt(failed))

  assert.deepEqual(handled, [])
  assert.deepEqual(failed, ['run state'])
})

test('the end of a recovery run hands over its results and closes the browser when a cancel fails', async () => {
  // A cancel that throws must not cost the result file or leave the browser
  // open.
  const steps = []
  const results = [
    { id: 'agent.recovery.reload', status: 'passed' },
    { id: 'agent.recovery.webkit', status: null },
  ]
  await assert.rejects(finishRecoveryRun({
    actor: {},
    browser: { close: async () => { steps.push('close') } },
    ownedRuns: ['run_a'],
    results,
  }, {
    cancelRuns: async () => {
      steps.push('cancel')
      throw new Error('HTTP 500')
    },
    writeScenarioSidecar: async (written) => { steps.push(`write ${written.map((item) => item.id).join(',')}`) },
  }), /HTTP 500/)
  assert.deepEqual(steps, ['cancel', 'write agent.recovery.reload', 'close'])
})

test('a recovery run without an actor cancels nothing and still closes the browser after a failed write', async () => {
  const steps = []
  await assert.rejects(finishRecoveryRun({
    actor: null,
    browser: { close: async () => { steps.push('close') } },
    ownedRuns: ['run_a'],
    results: [],
  }, {
    cancelRuns: async () => { steps.push('cancel') },
    writeScenarioSidecar: async () => {
      steps.push('write')
      throw new Error('disk full')
    },
  }), /disk full/)
  assert.deepEqual(steps, ['write', 'close'])
})
