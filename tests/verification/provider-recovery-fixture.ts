import { writeFile, unlink } from 'node:fs/promises'
import { resolve } from 'node:path'

import type { CleanupLedger } from './cleanup-ledger.ts'
import { requireContainerCommand, runContainerCommand } from './container-engine.ts'
import type { RunContext } from './run-context.ts'

/** Temporary SDK seam in the already running canonical API and worker. */
export async function prepareProviderRecoveryFixture(
  context: RunContext,
  cleanupLedger: CleanupLedger,
): Promise<void> {
  if (context.containerEngine !== 'podman') {
    throw new Error('Provider recovery verification requires explicit local Podman control.')
  }
  const secretsFile = context.environment.INQTRIX_E2E_STACK_SECRETS_FILE
  const configFile = context.environment.INQTRIX_E2E_STACK_CONFIG_FILE
  if (!secretsFile || !configFile) throw new Error('Explicit opaque stack environment-file paths are required.')
  const base = [
    'compose', '-f', 'deploy/compose/compose.stack.yaml',
    '--env-file', secretsFile, '--env-file', configFile,
  ]
  const overlay = resolve(context.reportDirectory, 'provider-fixture.override.yaml')
  const fixture = resolve(context.repositoryRoot, 'tests/verification/fixtures/provider-faults.py')
  let activationStarted = false
  const compose = async (args: string[], label: string) => {
    const result = await runContainerCommand('podman', args, {
      cwd: context.repositoryRoot,
      environment: context.environment,
      abortSignal: AbortSignal.timeout(120_000),
    })
    await writeFile(resolve(context.reportDirectory, `${label}.log`),
      `${result.stdout}\n${result.stderr}`, { mode: 0o600 })
    requireContainerCommand(result, label)
  }
  await cleanupLedger.register('resource', 'temporary provider verification command override', async () => {
    if (activationStarted) {
      await compose([...base, 'up', '-d', '--no-deps', '--force-recreate', 'api', 'worker'],
        'provider-fixture-restore')
      for (const service of ['api', 'worker']) {
        const restored = await runContainerCommand('podman', [
          'exec', `inqtrix-${service}-1`, 'python', '-c',
          'import os; raise SystemExit(int("INQTRIX_VERIFICATION_RUN_ID" in os.environ))',
        ], { cwd: context.repositoryRoot, environment: context.environment,
          abortSignal: AbortSignal.timeout(10_000) })
        requireContainerCommand(restored, 'Provider fixture removal check')
      }
    }
    await unlink(overlay).catch((error: NodeJS.ErrnoException) => {
      if (error.code !== 'ENOENT') throw error
    })
  })
  // JSON is a YAML subset; no shell interpolation or secret copying is used.
  const service = (role: string) => ({
    command: ['python', '/verification/provider-faults.py', role],
    environment: { INQTRIX_VERIFICATION_RUN_ID: context.runId },
    volumes: [`${fixture}:/verification/provider-faults.py:ro`],
  })
  await writeFile(overlay, JSON.stringify({ services: { api: service('api'), worker: service('worker') } }),
    { mode: 0o600 })
  activationStarted = true
  await compose([...base, '-f', overlay, 'up', '-d', '--no-deps', '--force-recreate', 'api', 'worker'],
    'provider-fixture-activate')
  const baseURL = context.environment.INQTRIX_E2E_BASE_URL ?? 'http://127.0.0.1:8080'
  const deadline = Date.now() + 60_000
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`${baseURL}/v1/capabilities`, { signal: AbortSignal.timeout(3_000) })
      if (response.ok || response.status === 401 || response.status === 403) {
        const markers = await providerFixtureLogLines(context)
        if (['api', 'worker'].every((service) => markers.some((item) =>
          item.fixtureService === service && item.verification_provider_ready === context.runId))) return
      }
    } catch { /* The API is being replaced; the bounded readiness probe continues. */ }
    await new Promise((resolveWait) => setTimeout(resolveWait, 500))
  }
  throw new Error('The canonical API did not recover after fixture activation.')
}

async function providerFixtureLogLines(context: RunContext): Promise<Array<Record<string, any>>> {
  const rows: Array<Record<string, any>> = []
  for (const service of ['api', 'worker']) {
    const result = await runContainerCommand('podman', [
      'logs', `inqtrix-${service}-1`,
    ], { cwd: context.repositoryRoot, environment: context.environment,
      abortSignal: AbortSignal.timeout(10_000) })
    requireContainerCommand(result, 'Provider fixture log read')
    await writeFile(resolve(context.reportDirectory, 'runtime-' + service + '.log'),
      result.stdout + '\n' + result.stderr, { mode: 0o600 })
    for (const line of `${result.stdout}\n${result.stderr}`.split('\n')) {
      try {
        const row = JSON.parse(line)
        if (row.verification_provider?.run === context.runId || row.verification_provider_ready === context.runId) rows.push({ ...row, fixtureService: service })
      } catch { /* Only exact structured fixture records are selected. */ }
    }
  }
  return rows
}

export async function verifyProviderFaultCalls(context: RunContext, validate = true): Promise<void> {
  const calls = (await providerFixtureLogLines(context)).flatMap((row) =>
    row.verification_provider ? [row.verification_provider] : [])
  await writeFile(resolve(context.reportDirectory, 'provider-fault-calls.json'), JSON.stringify(calls, null, 2), { mode: 0o600 })
  // A failed browser campaign still needs its evidence before the fixture
  // containers are restored. Only already-failed campaigns omit final gates.
  if (!validate) return
  const expected: Record<string, number> = {
    output_limit: 1, arguments_exhausted: 3, transport: 3,
    access_denied: 1, unknown: 1, cancel: 1, context_unreducible: 1,
  }
  for (const [name, count] of Object.entries(expected)) {
    if (calls.filter((row) => row.case === name).length !== count) {
      throw new Error(`Provider fault case ${name} violated its SDK attempt count.`)
    }
  }
  const transport = calls.filter((row) => row.case === 'transport')
  if (new Set(transport.map((row) => row.request_sha256)).size !== 1) {
    throw new Error('Transport retry changed the model messages or tool schema.')
  }
  if (!calls.some((row) => row.case === 'context' && row.purpose === 'agent_context_projection')) {
    throw new Error('Context recovery never reached the real projection model.')
  }
  for (const [name, minimum] of Object.entries({ archive_window: 3, archive_catalogue: 4, history_seed: 4, bulk_handover: 2 })) {
    if (calls.filter((row) => row.case === name).length < minimum) {
      throw new Error('Archive case ' + name + ' never reached its real answer generation.')
    }
  }
}
