import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import type {
  FullConfig,
  FullResult,
  Suite,
  TestCase,
  TestResult,
} from '@playwright/test/reporter'

import {
  requiredPlaywrightTags,
} from '../tests/verification/scenario-inventory.ts'
import CollaborationScenarioReporter from './scenario-reporter.ts'
import UiScenarioReporter from './ui-scenario-reporter.ts'

test('profile inventory mandates critical system and fault behavior', () => {
  for (const tag of [
    '@concurrent-edits',
    '@detached-transfer',
    '@direct-edit',
    '@layout',
    '@remote-caret',
    '@remote-selection',
    '@suggestions',
    '@source-readonly',
    '@transport-fingerprint',
  ]) {
    assert(requiredPlaywrightTags('system-smoke', 'desktop').includes(tag), `${tag} must be required on desktop`)
    if (tag !== '@remote-caret' && tag !== '@remote-selection') {
      assert(requiredPlaywrightTags('system-smoke', 'mobile').includes(tag), `${tag} must be required on mobile`)
    }
  }
  assert(requiredPlaywrightTags('system-smoke', 'desktop', 'chromium').includes('@ime'))
  assert(!requiredPlaywrightTags('system-smoke', 'desktop', 'firefox').includes('@ime'))
  assert(!requiredPlaywrightTags('system-smoke', 'desktop', 'webkit').includes('@ime'))
  assert(requiredPlaywrightTags('system-smoke', 'desktop', 'chromium').includes('@large-state-latency'))
  assert(!requiredPlaywrightTags('system-smoke', 'desktop', 'firefox').includes('@large-state-latency'))
  assert(!requiredPlaywrightTags('system-smoke', 'mobile', 'chromium').includes('@large-state-latency'))
  for (const tag of [
    '@gateway-outage',
    '@permission-downgrade',
    '@revocation',
  ]) {
    assert(requiredPlaywrightTags('fault-injection', 'desktop').includes(tag), `${tag} must be required on desktop`)
    assert(requiredPlaywrightTags('fault-injection', 'mobile').includes(tag), `${tag} must be required on mobile`)
  }
})

test('scenario reporter converts any required runtime skip into a failed run', async () => {
  const reporter = new CollaborationScenarioReporter({
    profile: 'fault-injection',
  })
  reporter.onTestEnd({
    parent: { project: () => ({ name: 'vite-mobile' }) },
    tags: ['@outage', '@mobile'],
    title: '@outage @mobile sidecar outage',
  } as unknown as TestCase, {
    status: 'skipped',
  } as TestResult)

  const originalWrite = process.stderr.write
  let output = ''
  process.stderr.write = ((chunk: string | Uint8Array) => {
    output += String(chunk)
    return true
  }) as typeof process.stderr.write
  try {
    const result = await reporter.onEnd({
      duration: 1,
      startTime: new Date(0),
      status: 'passed',
    } satisfies FullResult)
    assert.deepEqual(result, { status: 'failed' })
    assert.match(output, /required test skipped: vite-mobile/)
  } finally {
    process.stderr.write = originalWrite
  }
})

test('scenario reporter fails when a transport project loses required scenarios', async () => {
  const reporter = new CollaborationScenarioReporter({
    profile: 'system-smoke',
  })
  reporter.onBegin({} as FullConfig, {
    allTests: () => [],
  } as unknown as Suite)

  const originalWrite = process.stderr.write
  let output = ''
  process.stderr.write = ((chunk: string | Uint8Array) => {
    output += String(chunk)
    return true
  }) as typeof process.stderr.write
  try {
    const result = await reporter.onEnd({
      duration: 1,
      startTime: new Date(0),
      status: 'passed',
    } satisfies FullResult)
    assert.deepEqual(result, { status: 'failed' })
    assert.match(output, /vite-chromium-desktop selected no tests/)
    assert.match(output, /python-gateway-chromium-mobile selected no tests/)
  } finally {
    process.stderr.write = originalWrite
  }
})

test('generated system reporter requires only the declared active transport projects', async () => {
  const reporter = new CollaborationScenarioReporter({
    profile: 'system-smoke',
    transports: ['python-gateway'],
  })
  reporter.onBegin({} as FullConfig, {
    allTests: () => [],
  } as unknown as Suite)

  const originalWrite = process.stderr.write
  let output = ''
  process.stderr.write = ((chunk: string | Uint8Array) => {
    output += String(chunk)
    return true
  }) as typeof process.stderr.write
  try {
    await reporter.onEnd({
      duration: 1,
      startTime: new Date(0),
      status: 'passed',
    } satisfies FullResult)
    assert.match(output, /python-gateway-chromium-desktop selected no tests/)
    assert.doesNotMatch(output, /vite-/)
    assert.doesNotMatch(output, /nginx-/)
  } finally {
    process.stderr.write = originalWrite
  }
})

test('scenario reporter persists only attempted scenarios after an early failure', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'inqtrix-scenario-sidecar-'))
  const path = join(directory, 'scenarios.json')
  const reporter = new CollaborationScenarioReporter({
    environment: {
      INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH: path,
    },
    profile: 'system-smoke',
  })
  reporter.onTestEnd({
    parent: { project: () => ({ name: 'vite-chromium-desktop' }) },
    tags: ['@direct-edit'],
    title: '@direct-edit',
  } as unknown as TestCase, {
    status: 'failed',
  } as TestResult)
  const originalWrite = process.stderr.write
  process.stderr.write = (() => true) as typeof process.stderr.write
  try {
    await reporter.onEnd({
      duration: 1,
      startTime: new Date(0),
      status: 'failed',
    } satisfies FullResult)
    const sidecar = JSON.parse(readFileSync(path, 'utf8'))
    assert.deepEqual(sidecar.scenarios, [{
      id: 'system.direct-edit',
      status: 'failed',
    }])
  } finally {
    process.stderr.write = originalWrite
    rmSync(directory, { force: true, recursive: true })
  }
})

test('UI scenario reporter requires Chromium, Firefox, and WebKit outcomes', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'inqtrix-ui-sidecar-'))
  const path = join(directory, 'scenarios.json')
  const reporter = new UiScenarioReporter({
    environment: {
      INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH: path,
    },
  })
  reporter.onTestEnd({
    parent: { project: () => ({ name: 'chromium' }) },
    title: 'activation success closes the writable body window before delayed exact hydration',
  } as unknown as TestCase, {
    status: 'passed',
  } as TestResult)
  try {
    const result = await reporter.onEnd({
      duration: 1,
      startTime: new Date(0),
      status: 'passed',
    } satisfies FullResult)
    assert.deepEqual(result, { status: 'failed' })
    const sidecar = JSON.parse(readFileSync(path, 'utf8'))
    assert.deepEqual(sidecar.scenarios, [{
      id: 'ui.activation-hydration',
      status: 'failed',
    }])
  } finally {
    rmSync(directory, { force: true, recursive: true })
  }
})

test('UI scenario reporter requires only the engines a scenario declares', async () => {
  const title = 'an IME composition that spans a reconnect is committed once and keeps focus'
  for (const [chromium, expected] of [['passed', 'passed'], ['skipped', 'failed']] as const) {
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-ui-engines-'))
    const path = join(directory, 'scenarios.json')
    const reporter = new UiScenarioReporter({
      environment: {
        INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH: path,
      },
    })
    for (const [project, status] of [
      ['chromium', chromium],
      ['firefox', 'skipped'],
      ['webkit', 'skipped'],
    ] as const) {
      reporter.onTestEnd({
        parent: { project: () => ({ name: project }) },
        title,
      } as unknown as TestCase, {
        status,
      } as TestResult)
    }
    try {
      await reporter.onEnd({
        duration: 1,
        startTime: new Date(0),
        status: 'passed',
      } satisfies FullResult)
      const sidecar = JSON.parse(readFileSync(path, 'utf8'))
      assert.deepEqual(sidecar.scenarios, [{
        id: 'ui.editor-reconnect-composition',
        status: expected,
      }])
    } finally {
      rmSync(directory, { force: true, recursive: true })
    }
  }
})

test('Playwright empty reporter options preserve the process result path', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'inqtrix-ui-process-env-'))
  const path = join(directory, 'scenarios.json')
  const previous = process.env.INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH
  process.env.INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH = path
  try {
    // This is the constructor shape Playwright uses for a reporter configured
    // without explicit options.
    const reporter = new UiScenarioReporter({})
    for (const project of ['chromium', 'firefox', 'webkit']) {
      reporter.onTestEnd({
        parent: { project: () => ({ name: project }) },
        title: 'activation success closes the writable body window before delayed exact hydration',
      } as unknown as TestCase, {
        status: 'passed',
      } as TestResult)
    }
    await reporter.onEnd({
      duration: 1,
      startTime: new Date(0),
      status: 'passed',
    } satisfies FullResult)
    const sidecar = JSON.parse(readFileSync(path, 'utf8'))
    assert.deepEqual(sidecar.scenarios, [{
      id: 'ui.activation-hydration',
      status: 'passed',
    }])
  } finally {
    if (previous === undefined) {
      delete process.env.INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH
    } else {
      process.env.INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH = previous
    }
    rmSync(directory, { force: true, recursive: true })
  }
})
