import assert from 'node:assert/strict'
import { chmod, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { delimiter, join } from 'node:path'
import { describe, test } from 'node:test'

import type { VerificationAdapter } from './adapter.ts'
import {
  FAULT_PROFILES,
  inspectRunLogs,
  judgeServiceLogs,
  logInspectionIsMandatory,
  maskLogMessage,
  QUIET_PROFILES,
  ServiceLogCounter,
  type LogSource,
} from './log-inspection.ts'
import type { ServiceLogCounters } from './model.ts'
import { runVerification } from './orchestrator.ts'
import type { RunContext } from './run-context.ts'
import { scenariosForProfile } from './scenario-inventory.ts'

const RUN_START = '2026-10-30T23:00:00.000Z'

function line(fields: Record<string, unknown>): string {
  return JSON.stringify({
    level: 'INFO',
    logger: 'inqtrix.server',
    message: 'request finished',
    service: 'api',
    ts: '2026-10-30T23:00:01Z',
    ...fields,
  })
}

const QUIET_ROW: ServiceLogCounters = {
  errors: 0,
  firstLineAt: '2026-10-30T23:00:01Z',
  levels: { INFO: 1 },
  lines: 1,
  missingFields: {},
  notable: [],
  notableDropped: 0,
  notJson: 0,
}

/** The block Hocuspocus prints when a hook rejects an update. */
const REJECTION_BLOCK = [
  'closing connection 11111111-2222-3333-4444-555555555555 (while handling inqtrix-editor-v1:ed_abc) because of exception HookRejection',
  '    at Object.beforeSync (/app/src/errors.ts:278:13)',
  '  code: 4403,',
  "  reason: 'suggestion_policy_violation'",
  '}',
]

function context(overrides: Partial<RunContext> = {}): RunContext {
  return {
    containerEngine: 'podman',
    profile: 'system-smoke',
    runId: 'inqv-log-inspection',
    startedAt: RUN_START,
    ...overrides,
  } as RunContext
}

function source(
  logs: Readonly<Record<string, readonly string[]>>,
  stopped: readonly string[] = [],
): LogSource {
  return {
    async list() {
      return Object.keys(logs).map((id) => ({
        id,
        running: !stopped.includes(id),
        service: id.split('-')[0] ?? id,
      }))
    },
    async read(_context, container, onLine) {
      for (const row of logs[container.id] ?? []) onLine(row)
    },
  }
}

describe('service log counters', () => {
  test('count levels, broken lines and missing fields', () => {
    const counter = new ServiceLogCounter()
    counter.add(line({}))
    counter.add(line({ level: 'error', message: 'lost it' }))
    counter.add('Traceback (most recent call last):')
    counter.add('[1, 2]')
    counter.add('{"level": "INFO"}')
    counter.add('   ')

    const row = counter.result()

    assert.equal(row.lines, 5)
    assert.equal(row.notJson, 2)
    assert.equal(row.errors, 1)
    assert.equal(row.firstLineAt, '2026-10-30T23:00:01Z')
    assert.deepEqual(row.levels, { ERROR: 1, INFO: 2 })
    assert.deepEqual(row.missingFields, { 'message or event': 1, service: 1, ts: 1 })
  })

  test('keep only masked templates of notable lines, never other fields', () => {
    const counter = new ServiceLogCounter()
    counter.add(line({
      level: 'WARNING',
      message: 'run run_9cc6176c7bb640ea retried 12 times for 3f2b7c1e-0d6a-4c55-9e11-2a3b4c5d6e7f',
      prompt: 'the secret question',
    }))
    counter.add(line({
      level: 'WARNING',
      message: 'run run_00aa11bb22cc33dd retried 40 times for 11111111-2222-3333-4444-555555555555',
    }))

    const { notable } = counter.result()

    assert.deepEqual(notable, [{
      count: 2,
      level: 'WARNING',
      logger: 'inqtrix.server',
      message: 'run run_<id> retried <n> times for <id>',
    }])
    assert.equal(JSON.stringify(notable).includes('secret question'), false)
  })

  test('accept the record shape of the collaboration service', () => {
    const counter = new ServiceLogCounter()
    counter.add(JSON.stringify({
      event: 'connection_closed',
      level: 'warn',
      service: 'collaboration',
      ts: '2026-10-30T23:00:00Z',
    }))

    const row = counter.result()

    assert.deepEqual(row.missingFields, {})
    assert.equal(row.notable[0]?.message, 'connection_closed')
  })

  test('count every text line as a break of the log contract, the rejection block included', () => {
    const counter = new ServiceLogCounter()
    for (const text of REJECTION_BLOCK) counter.add(text)
    counter.add('Caught error during storeDocumentHooks. Document stays in memory to avoid data loss')

    assert.equal(counter.result().notJson, REJECTION_BLOCK.length + 1)
  })

  test('put errors first when the list of templates is cut, and say how much was cut', () => {
    const counter = new ServiceLogCounter()
    for (let index = 0; index < 25; index += 1) {
      counter.add(line({ level: 'WARNING', message: `warning kind ${String.fromCharCode(97 + index)}` }))
      counter.add(line({ level: 'WARNING', message: `warning kind ${String.fromCharCode(97 + index)}` }))
    }
    counter.add(line({ level: 'ERROR', message: 'the only error' }))

    const { notable, notableDropped } = counter.result()

    assert.equal(notable[0]?.message, 'the only error')
    assert.equal(notableDropped, 6)
  })

  test('mask identifiers behind any short prefix', () => {
    assert.equal(
      maskLogMessage('denied actor_ref=usr_72ac011ad560ce57 tenant_ref=ten_d2eb783e4dfb0f06'),
      'denied actor_ref=usr_<id> tenant_ref=ten_<id>',
    )
  })

  test('bound a template in length', () => {
    assert.equal(maskLogMessage('x'.repeat(500)).length, 150)
  })
})

describe('the verdict over the services', () => {
  test('a quiet profile fails on an error line, a fault profile does not', () => {
    const noisy = { api: { ...QUIET_ROW, errors: 1 } }

    assert.equal(judgeServiceLogs('system-smoke', noisy), 'failed')
    assert.equal(judgeServiceLogs('fault-injection', noisy), 'passed')
  })

  test('a broken log contract fails every profile', () => {
    assert.equal(judgeServiceLogs('fault-injection', { api: { ...QUIET_ROW, notJson: 1 } }), 'failed')
    assert.equal(
      judgeServiceLogs('fault-injection', { api: { ...QUIET_ROW, missingFields: { ts: 1 } } }),
      'failed',
    )
  })

  test('the profile lists stay as they are', () => {
    assert.deepEqual(
      [...QUIET_PROFILES],
      ['agent-desk', 'chat-prompt', 'load-smoke', 'system-smoke'],
    )
    assert.deepEqual(
      [...FAULT_PROFILES],
      ['fault-injection', 'load-capacity', 'load-ramp', 'load-soak'],
    )
  })
})

describe('the inspection of one run', () => {
  const quietStack = { 'api-1': [line({})], 'web-1': [line({})] }

  test('sums replicas of a service and binds the result to the run', async () => {
    const inspection = await inspectRunLogs(context(), source({
      ...quietStack,
      'worker-1': [line({})],
      'worker-2': [line({}), line({})],
    }))

    assert.equal(inspection.status, 'passed')
    assert.equal(inspection.runId, 'inqv-log-inspection')
    assert.equal(inspection.since, RUN_START)
    assert.equal(inspection.services?.worker?.lines, 3)
  })

  test('says not_run instead of passing when it cannot read the logs', async () => {
    const noEngine = await inspectRunLogs(context({ containerEngine: null }), source(quietStack))
    const noStack = await inspectRunLogs(context(), source({}))
    const notApplicable = await inspectRunLogs(context({ profile: 'ui-fixture' }), source(quietStack))
    const recreated = await inspectRunLogs(context({ profile: 'agent-recovery' }), source(quietStack))
    const broken = await inspectRunLogs(context(), {
      async list() {
        throw new TypeError('Cookie: session=abc')
      },
      async read() {},
    })

    for (const inspection of [noEngine, noStack, notApplicable, recreated, broken]) {
      assert.equal(inspection.status, 'not_run')
    }
    assert.equal(JSON.stringify(broken).includes('session=abc'), false)
  })

  test('an empty log or a missing active service carries no verdict', async () => {
    const empty = await inspectRunLogs(context(), source({ 'api-1': [], 'web-1': [line({})] }))
    const noWeb = await inspectRunLogs(context(), source({ 'api-1': [line({})] }))

    assert.equal(empty.status, 'not_run')
    assert.match(empty.reason ?? '', /api log is empty/)
    assert.equal(noWeb.status, 'not_run')
    assert.match(noWeb.reason ?? '', /web container/)
  })

  test('a log that starts long after the run began carries no verdict', async () => {
    const rotated = await inspectRunLogs(context(), source({
      'api-1': [line({ ts: '2026-10-30T23:10:00Z' })],
      'web-1': [line({})],
    }))

    assert.equal(rotated.status, 'not_run')
    assert.match(rotated.reason ?? '', /rotated or the clocks differ/)
  })

  test('logs in text format carry no verdict instead of staying red for good', async () => {
    const text = await inspectRunLogs(context(), source({
      'api-1': [line({})],
      'web-1': ['127.0.0.1 - GET / 200'],
    }))

    assert.equal(text.status, 'not_run')
    assert.match(text.reason ?? '', /web are not JSON/)
  })

  test('a service that stopped during a quiet run fails it', async () => {
    const stopped = await inspectRunLogs(
      context(),
      source({ ...quietStack, 'worker-1': [line({})] }, ['worker-1']),
    )
    const fault = await inspectRunLogs(
      context({ profile: 'fault-injection' }),
      source({ ...quietStack, 'worker-1': [line({})] }, ['worker-1']),
    )

    assert.equal(stopped.status, 'failed')
    assert.match(stopped.reason ?? '', /worker/)
    assert.equal(fault.status, 'passed')
  })

  test('error lines already counted survive a later read failure', async () => {
    const read = source({ 'api-1': [line({ level: 'ERROR' })], 'web-1': [] })
    const inspection = await inspectRunLogs(context(), {
      list: read.list,
      async read(run, container, onLine) {
        if (container.service === 'web') throw new Error('replaced')
        await read.read(run, container, onLine)
      },
    })

    assert.equal(inspection.status, 'failed')
    assert.equal(inspection.services?.api?.errors, 1)
  })

  test('only a run that selected an engine has to inspect', () => {
    assert.equal(logInspectionIsMandatory(context()), true)
    assert.equal(logInspectionIsMandatory(context({ containerEngine: null })), false)
    assert.equal(logInspectionIsMandatory(context({ profile: 'ui-fixture' })), false)
    assert.equal(logInspectionIsMandatory(context({ profile: 'agent-recovery' })), false)
  })

  test('the run verdict follows the inspection', async () => {
    const repositoryRoot = await mkdtemp(join(tmpdir(), 'inqtrix-log-inspection-'))
    const engine = 'chat-prompt-live'
    const scenarios = scenariosForProfile('chat-prompt')
      .filter((scenario) => scenario.engine === engine)
      .map((scenario) => ({ id: scenario.id, status: 'passed' as const }))
    const green: VerificationAdapter = {
      engine,
      profiles: ['chat-prompt'],
      async preflight() {
        return []
      },
      async execute() {
        const now = new Date().toISOString()
        return {
          durationMs: 1,
          engine,
          exitCode: 0,
          finishedAt: now,
          scenarios,
          signal: null,
          startedAt: now,
          status: 'passed',
        }
      },
    }
    try {
      const options = {
        containerEngine: 'podman' as const,
        profile: 'chat-prompt' as const,
        repositoryRoot,
      }
      const now = new Date().toISOString()
      const logs = (level: string) => ({
        'api-1': [line({ level, ts: now })],
        'web-1': [line({ ts: now })],
      })
      const clean = await runVerification(
        { ...options, runId: 'inqv-log-inspection-clean' },
        [green],
        source(logs('INFO')),
      )
      const noisy = await runVerification(
        { ...options, runId: 'inqv-log-inspection-noisy' },
        [green],
        source(logs('ERROR')),
      )
      const unread = await runVerification(
        { ...options, runId: 'inqv-log-inspection-unread' },
        [green],
        source({}),
      )

      assert.equal(clean.status, 'passed')
      assert.equal(noisy.status, 'failed')
      assert.equal(unread.status, 'blocked')
    } finally {
      await rm(repositoryRoot, { force: true, recursive: true })
    }
  })
})

describe('reading the logs through the container engine', () => {
  const API = 'aaaaaaaaaaaa'
  const WEB = 'bbbbbbbbbbbb'
  const WORKER = 'cccccccccccc'
  const BROKEN = 'dddddddddddd'

  async function fakeEngine(
    rows: readonly Record<string, unknown>[],
    run: (root: string, inspect: () => ReturnType<typeof inspectRunLogs>) => Promise<void>,
  ): Promise<void> {
    const root = await mkdtemp(join(tmpdir(), 'inqtrix-log-engine-'))
    const previousPath = process.env.PATH
    try {
      const bin = join(root, 'bin')
      await mkdir(bin)
      await writeFile(join(root, 'rows.json'), JSON.stringify(rows))
      await writeFile(join(bin, 'podman'), [
        '#!/bin/sh',
        `echo "$*" >> "${join(root, 'calls.txt')}"`,
        'if [ "$1" = "ps" ]; then',
        `  cat "${join(root, 'rows.json')}"`,
        '  exit 0',
        'fi',
        'last=""; for argument in "$@"; do last="$argument"; done',
        `if [ "$last" = "${BROKEN}" ]; then exit 125; fi`,
        `echo '${line({})}'`,
        `echo '${line({ level: 'WARNING', message: 'on stderr' })}' >&2`,
        '',
      ].join('\n'))
      await chmod(join(bin, 'podman'), 0o700)
      const path = `${bin}${delimiter}${previousPath ?? ''}`
      process.env.PATH = path
      await run(root, () => inspectRunLogs(context({
        environment: { PATH: path },
        repositoryRoot: root,
      } as Partial<RunContext>)))
    } finally {
      process.env.PATH = previousPath
      await rm(root, { force: true, recursive: true })
    }
  }

  function row(root: string, id: string, service: string, state = 'running', project = 'inqtrix') {
    return {
      Id: id,
      Labels: {
        'com.docker.compose.project': project,
        'com.docker.compose.project.config_files': join(root, 'deploy/compose/compose.stack.yaml'),
        'com.docker.compose.service': service,
      },
      State: state,
    }
  }

  async function withRows(
    make: (root: string) => readonly Record<string, unknown>[],
    check: (inspection: Awaited<ReturnType<typeof inspectRunLogs>>, calls: string) => void,
  ): Promise<void> {
    await fakeEngine([], async (root, inspect) => {
      await writeFile(join(root, 'rows.json'), JSON.stringify(make(root)))
      const inspection = await inspect()
      check(inspection, await readFile(join(root, 'calls.txt'), 'utf8'))
    })
  }

  test('asks for the window of the run and reads both streams', async () => {
    await withRows(
      (root) => [row(root, API, 'api'), row(root, WEB, 'web')],
      (inspection, calls) => {
        assert.equal(inspection.status, 'passed')
        assert.equal(inspection.services?.api?.lines, 2)
        assert.match(calls, new RegExp(`--log-level=error logs --since ${RUN_START} ${API}`))
      },
    )
  })

  test('reads a stopped container and fails the quiet run', async () => {
    await withRows(
      (root) => [row(root, API, 'api'), row(root, WEB, 'web'), row(root, WORKER, 'worker', 'exited')],
      (inspection, calls) => {
        assert.equal(inspection.status, 'failed')
        assert.match(inspection.reason ?? '', /worker/)
        assert.match(calls, new RegExp(WORKER))
      },
    )
  })

  test('reports a failing read as not_run with the service', async () => {
    await withRows(
      (root) => [row(root, API, 'api'), row(root, WEB, 'web'), row(root, BROKEN, 'worker')],
      (inspection) => {
        assert.equal(inspection.status, 'not_run')
        assert.match(inspection.reason ?? '', /worker logs ended with 125/)
      },
    )
  })

  test('refuses to guess between two projects on the same stack file', async () => {
    await withRows(
      (root) => [row(root, API, 'api'), row(root, WEB, 'web', 'running', 'inqtrix-gateway')],
      (inspection) => {
        assert.equal(inspection.status, 'not_run')
        assert.match(inspection.reason ?? '', /several Compose projects/)
      },
    )
  })

  test('ignores containers of another stack file', async () => {
    await withRows(
      (root) => [
        row(root, API, 'api'),
        row(root, WEB, 'web'),
        { ...row(root, WORKER, 'worker'), Labels: {
          'com.docker.compose.project': 'other',
          'com.docker.compose.project.config_files': '/elsewhere/compose.yaml',
          'com.docker.compose.service': 'worker',
        } },
      ],
      (inspection, calls) => {
        assert.equal(inspection.status, 'passed')
        assert.equal(calls.includes(WORKER), false)
      },
    )
  })
})
