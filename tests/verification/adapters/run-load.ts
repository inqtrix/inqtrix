import { createHash } from 'node:crypto'
import { setTimeout as delay } from 'node:timers/promises'

import { request } from '@playwright/test'

import type { VerificationAdapter } from '../adapter.ts'
import { runCommand } from '../command-runner.ts'
import { containerEnginePreflight } from '../container-engine.ts'
import { normalizeLoadSmokeBaseURL } from '../fixtures/load-smoke.mjs'
import { httpsErrorsIgnored } from '../fixtures/https-errors.mjs'
import {
  eraseTemporaryAccount,
  type CleanupActor,
} from '../fixtures/product-resource.ts'
import type { PreflightCheck } from '../model.ts'
import {
  configuredFileCheck,
  environmentCheck,
  failed,
  passed,
  repositoryFileCheck,
} from '../preflight.ts'
import type { RunContext } from '../run-context.ts'

const ENGINE = 'run-load' as const
const STAGES = ['smoke', 'l05', 'l1', 'l2'] as const
const ACCOUNT_SUFFIX = /^\d{1,4}@example\.invalid$/
const ERASURE_CONCURRENCY = 8
// The product refuses to erase an account while one of its runs still stops.
const ERASURE_DEADLINE_MS = 120_000
const ERASURE_RETRY_MS = 5_000
// Long enough to cancel the active runs, erase every account and write the report.
const TERMINATION_GRACE_MS = 240_000
const COMPOSE_PROJECT = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/

/**
 * Everything before the number of a run-load account's address. It is computed
 * here, in the orchestrator, from the Run ID alone: the digest binds an address
 * to exactly one run, and no value of the engine can widen what gets erased.
 */
export function runLoadAccountPrefix(runId: string): string {
  const fragment = runId
    .replace(/^inqv-/, '')
    .replaceAll(/[^a-z0-9-]+/g, '-')
    .slice(0, 28)
  const digest = createHash('sha256').update(`run-load:${runId}`).digest('hex').slice(0, 16)
  return `inqv-rl-${fragment}-${digest}-u`
}

export function runLoadAccountBelongsToRun(email: unknown, runId: string): boolean {
  if (typeof email !== 'string') return false
  const prefix = runLoadAccountPrefix(runId)
  return email.startsWith(prefix) && ACCOUNT_SUFFIX.test(email.slice(prefix.length))
}

async function listAccountEmails(actor: CleanupActor): Promise<unknown[]> {
  let response
  try {
    response = await actor.api.fetch('/v1/admin/users', {
      headers: {
        'X-Inqtrix-Expected-User-Id': actor.userId,
        'X-Inqtrix-Workspace-Id': actor.workspaceId,
      },
      method: 'GET',
    })
  } catch {
    // The library's error text lists the request headers, the cookie included.
    throw new Error('Listing the accounts failed.')
  }
  if (response.status() !== 200) {
    throw new Error(`Listing the accounts returned HTTP ${response.status()}.`)
  }
  const listing = await response.json() as { users?: { email?: unknown }[] }
  if (!Array.isArray(listing.users)) throw new Error('The account listing is invalid.')
  return listing.users.map((user) => user.email)
}

/**
 * Erase what is left of this run's accounts and prove that nothing remains. Only
 * addresses of exactly this run qualify. A refused erasure is tried again until the
 * deadline, because a run of the account may still be stopping; what is left then
 * makes this throw, and the ledger ends the whole run as `cleanup_failed`.
 */
export async function eraseAccountsOfRun(
  actor: CleanupActor,
  runId: string,
  {
    concurrency = ERASURE_CONCURRENCY,
    deadlineMs = ERASURE_DEADLINE_MS,
    retryMs = ERASURE_RETRY_MS,
  }: { concurrency?: number, deadlineMs?: number, retryMs?: number } = {},
): Promise<number> {
  const deadline = Date.now() + deadlineMs
  const ownAccounts = async (): Promise<string[]> => (await listAccountEmails(actor)).filter(
    (email): email is string => runLoadAccountBelongsToRun(email, runId),
  )
  const found = await ownAccounts()
  let owned = found
  for (;;) {
    let failures = 0
    let next = 0
    const pending = owned
    const worker = async (): Promise<void> => {
      while (next < pending.length) {
        const email = pending[next] as string
        next += 1
        try {
          await eraseTemporaryAccount(actor, email)
        } catch {
          failures += 1
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(concurrency, pending.length) }, worker))
    owned = await ownAccounts()
    if (owned.length === 0 && failures === 0) return found.length
    if (Date.now() >= deadline) {
      process.stderr.write(
        `[cleanup] run-load accounts: ${failures} erasures failed, ${owned.length} accounts remain\n`,
      )
      throw new Error(`Run-load account erasure left ${owned.length} accounts.`)
    }
    await delay(retryMs)
  }
}

async function eraseRunLoadAccounts(context: RunContext): Promise<void> {
  const baseURL = normalizeLoadSmokeBaseURL(
    context.environment.INQTRIX_E2E_BASE_URL ?? 'http://localhost:8080',
  )
  const adminEmail = context.environment.INQTRIX_E2E_ADMIN_EMAIL?.trim()
  const adminPassword = context.environment.INQTRIX_E2E_ADMIN_PASSWORD?.trim()
  if (!adminEmail || !adminPassword) {
    throw new Error('Erasing the run-load accounts requires the administrator credentials.')
  }
  const api = await request.newContext({
    baseURL,
    ignoreHTTPSErrors: httpsErrorsIgnored(context.environment),
  })
  let csrf = ''
  try {
    // A stack without an owner holds no account this run could have created.
    const setup = await api.get('/api/setup/status')
    if (setup.status() === 200 && (await setup.json() as { needs_owner?: unknown }).needs_owner === true) {
      return
    }
    const login = await api.post('/api/auth/login/local', {
      data: { email: adminEmail, password: adminPassword },
    })
    if (login.status() !== 200) throw new Error(`Cleanup login returned HTTP ${login.status()}.`)
    const read = async (headers?: Record<string, string>) => {
      const response = await api.get('/api/auth/session', { headers })
      if (response.status() !== 200) {
        throw new Error(`Cleanup session lookup returned HTTP ${response.status()}.`)
      }
      return await response.json() as {
        csrf_token?: string
        project_namespace?: string | null
        user?: { id?: string }
      }
    }
    let session = await read()
    if (!session.project_namespace) {
      session = await read({
        'X-Inqtrix-Workspace-Id': `e2e-${context.runId}-rl-owner`.slice(0, 80),
      })
    }
    if (!session.csrf_token || !session.project_namespace || !session.user?.id) {
      throw new Error('Cleanup session is incomplete.')
    }
    csrf = session.csrf_token
    await eraseAccountsOfRun({
      api,
      csrf,
      userId: session.user.id,
      workspaceId: session.project_namespace,
    }, context.runId)
  } finally {
    if (csrf) {
      await api.post('/api/auth/logout', { headers: { 'X-CSRF-Token': csrf } }).catch(() => {
        process.stderr.write('[cleanup] run-load owner logout failed\n')
      })
    }
    await api.dispose()
  }
}

async function liveStackCheck(origin: string | null): Promise<PreflightCheck> {
  if (origin === null) {
    return failed(ENGINE, 'live-stack-reachable', 'Stack health not checked: invalid base URL.')
  }
  try {
    const response = await fetch(`${origin}/health`, { signal: AbortSignal.timeout(10_000) })
    return response.ok
      ? passed(ENGINE, 'live-stack-reachable', 'The stack answers its health endpoint.')
      : failed(ENGINE, 'live-stack-reachable', `The stack health endpoint returned HTTP ${response.status}.`)
  } catch {
    return failed(
      ENGINE,
      'live-stack-reachable',
      'The stack is unreachable; start the load stack first. This profile starts nothing.',
    )
  }
}

export const runLoadAdapter: VerificationAdapter = {
  engine: ENGINE,
  profiles: ['run-load'],
  async preflight(context) {
    let origin: string | null = null
    let baseURLCheck: PreflightCheck
    try {
      origin = normalizeLoadSmokeBaseURL(
        context.environment.INQTRIX_E2E_BASE_URL ?? 'http://localhost:8080',
      )
      baseURLCheck = passed(ENGINE, 'base-url', 'The base URL is structurally valid.')
    } catch {
      baseURLCheck = failed(ENGINE, 'base-url', 'INQTRIX_E2E_BASE_URL must be a credential-free HTTP(S) URL.')
    }
    const stage = context.environment.INQTRIX_RUN_LOAD_STAGE?.trim() ?? ''
    const project = context.environment.INQTRIX_RUN_LOAD_PROJECT?.trim()
    const baseline = context.environment.INQTRIX_RUN_LOAD_BASELINE?.trim()
    const fault = context.environment.INQTRIX_RUN_LOAD_FAULT?.trim()
    return [
      repositoryFileCheck(context, ENGINE, 'engine-entrypoint', 'tests/load/run-load/run-load.mjs'),
      repositoryFileCheck(context, ENGINE, 'engine-library', 'tests/load/run-load/run-load-lib.mjs'),
      repositoryFileCheck(context, ENGINE, 'engine-scenarios', 'tests/load/run-load/scenarios.json'),
      environmentCheck(context, ENGINE, 'admin-email', 'INQTRIX_E2E_ADMIN_EMAIL'),
      environmentCheck(context, ENGINE, 'admin-password', 'INQTRIX_E2E_ADMIN_PASSWORD'),
      environmentCheck(context, ENGINE, 'user-password', 'INQTRIX_E2E_USER_PASSWORD'),
      (STAGES as readonly string[]).includes(stage)
        ? passed(ENGINE, 'stage', `The stage ${stage} is known.`)
        : failed(ENGINE, 'stage', `INQTRIX_RUN_LOAD_STAGE must be one of: ${STAGES.join(', ')}.`),
      project === undefined || COMPOSE_PROJECT.test(project)
        ? passed(ENGINE, 'compose-project', 'The Compose project name is valid.')
        : failed(ENGINE, 'compose-project', 'INQTRIX_RUN_LOAD_PROJECT is not a Compose project name.'),
      baseline
        ? configuredFileCheck(context, ENGINE, 'baseline-report', baseline, 'INQTRIX_RUN_LOAD_BASELINE')
        : passed(ENGINE, 'baseline-report', 'No base report is set; the baseline scenario stays not_run.'),
      !fault || fault === 'worker-stop'
        ? passed(ENGINE, 'fault', fault ? 'One worker is stopped half way through the plateau.' : 'No fault is set; its scenario stays not_run.')
        : failed(ENGINE, 'fault', 'INQTRIX_RUN_LOAD_FAULT must be worker-stop.'),
      fault && baseline
        ? failed(ENGINE, 'fault-without-base', 'A run with a fault says nothing about the base; set only one of the two.')
        : passed(ENGINE, 'fault-without-base', 'A fault and a base are not combined.'),
      context.fixturePath
        ? failed(ENGINE, 'generated-accounts', 'run-load provisions its own accounts and does not accept --fixture.')
        : passed(ENGINE, 'generated-accounts', 'run-load provisions its own Run-ID-bound accounts.'),
      ...containerEnginePreflight(context, ENGINE),
      context.containerEngine === 'podman'
        ? passed(ENGINE, 'run-load-podman', 'run-load reads its counters through Podman.')
        : failed(ENGINE, 'run-load-podman', 'run-load requires --container-engine podman for its samplers.'),
      baseURLCheck,
      await liveStackCheck(origin),
    ]
  },
  async execute(context, cleanupLedger) {
    // Before the engine can create anything, so that no interruption can leave
    // an account without a way to remove it. The entry stays open and runs last:
    // after a normal run it finds nothing, which is what it then has proven.
    await cleanupLedger.register(
      'resource',
      'run-load temporary accounts for current run',
      async () => await eraseRunLoadAccounts(context),
    )
    return await runCommand(context, cleanupLedger, {
      args: ['tests/load/run-load/run-load.mjs'],
      command: process.execPath,
      engine: ENGINE,
      environment: {
        INQTRIX_RUN_LOAD_ACCOUNT_PREFIX: runLoadAccountPrefix(context.runId),
        INQTRIX_RUN_LOAD_ENGINE: context.containerEngine ?? '',
      },
      productLifecycle: true,
      terminationGraceMs: TERMINATION_GRACE_MS,
    })
  },
}
