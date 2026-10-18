import { mkdir, unlink } from 'node:fs/promises'
import { resolve } from 'node:path'

import type { VerificationAdapter } from '../adapter.ts'
import { runCommand } from '../command-runner.ts'
import {
  containerEnginePreflight,
  runContainerCommand,
} from '../container-engine.ts'
import type { PreflightCheck } from '../model.ts'
import {
  configuredPrivateFileCheck,
  environmentCheck,
  failed,
  passed,
  repositoryFileCheck,
} from '../preflight.ts'
import { normalizeLoadSmokeBaseURL } from '../fixtures/load-smoke.mjs'
import { playwrightPreflight } from './shared.ts'

const ENGINE = 'collaboration-load' as const
const MAX_LOAD_CLOCK_SKEW_SECONDS = 5

export function clockSkewOutsideWindowSeconds(
  remoteEpochSeconds: number,
  hostBeforeEpochMs: number,
  hostAfterEpochMs: number,
): number {
  if (
    !Number.isFinite(remoteEpochSeconds)
    || !Number.isFinite(hostBeforeEpochMs)
    || !Number.isFinite(hostAfterEpochMs)
    || hostBeforeEpochMs > hostAfterEpochMs
  ) {
    throw new Error('Clock samples must be finite and ordered.')
  }
  const hostBeforeEpochSeconds = hostBeforeEpochMs / 1_000
  const hostAfterEpochSeconds = hostAfterEpochMs / 1_000
  if (remoteEpochSeconds < hostBeforeEpochSeconds) {
    return hostBeforeEpochSeconds - remoteEpochSeconds
  }
  if (remoteEpochSeconds > hostAfterEpochSeconds) {
    return remoteEpochSeconds - hostAfterEpochSeconds
  }
  return 0
}

async function podmanClockPreflight(
  context: Parameters<VerificationAdapter['preflight']>[0],
): Promise<PreflightCheck> {
  const hostBeforeEpochMs = Date.now()
  try {
    const result = await runContainerCommand(
      'podman',
      ['machine', 'ssh', 'date', '-u', '+%s.%N'],
      {
        abortSignal: context.abortSignal,
        cwd: context.repositoryRoot,
        environment: context.environment,
      },
    )
    const hostAfterEpochMs = Date.now()
    const output = result.stdout.trim()
    if (
      result.exitCode !== 0
      || !/^\d+(?:\.\d+)?$/.test(output)
    ) {
      return failed(
        ENGINE,
        'load-podman-clock',
        'The Podman VM clock could not be measured reliably.',
      )
    }
    const skewSeconds = clockSkewOutsideWindowSeconds(
      Number(output),
      hostBeforeEpochMs,
      hostAfterEpochMs,
    )
    return skewSeconds <= MAX_LOAD_CLOCK_SKEW_SECONDS
      ? passed(
          ENGINE,
          'load-podman-clock',
          'The Podman VM clock is aligned with the host clock.',
        )
      : failed(
          ENGINE,
          'load-podman-clock',
          'The Podman VM clock differs from the host by more than five seconds; synchronize the VM clock before provisioning collaboration load resources.',
        )
  } catch {
    return failed(
      ENGINE,
      'load-podman-clock',
      'The Podman VM clock could not be measured reliably.',
    )
  }
}

export const collaborationLoadAdapter: VerificationAdapter = {
  engine: ENGINE,
  profiles: ['load-smoke', 'load-soak', 'load-ramp', 'load-capacity'],
  async preflight(context) {
    const common = [
      repositoryFileCheck(
        context,
        ENGINE,
        'engine-entrypoint',
        'tests/load/collaboration-load.mjs',
      ),
      repositoryFileCheck(
        context,
        ENGINE,
        'engine-library',
        'tests/load/collaboration-load-lib.mjs',
      ),
    ]
    if (context.profile === 'load-capacity') {
      const fixture = context.fixturePath
        ?? context.environment.INQTRIX_LOAD_SESSION_FIXTURE
      return [
        ...common,
        configuredPrivateFileCheck(
          context,
          ENGINE,
          'session-fixture',
          fixture,
          'INQTRIX_LOAD_SESSION_FIXTURE',
        ),
      ]
    }
    let baseURLCheck
    try {
      normalizeLoadSmokeBaseURL(
        context.environment.INQTRIX_E2E_BASE_URL
          ?? 'http://127.0.0.1:8080',
      )
      baseURLCheck = passed(
        ENGINE,
        'base-url',
        `The ${context.profile} base URL is structurally valid.`,
      )
    } catch {
      baseURLCheck = failed(
        ENGINE,
        'base-url',
        'INQTRIX_E2E_BASE_URL must be a credential-free HTTP(S) URL.',
      )
    }
    const loadRuntimeChecks: PreflightCheck[] = []
    const checksSelectedPodman = context.profile === 'load-smoke'
      && context.containerEngine === 'podman'
    // The ramp gates every rung on container resource headroom, so it
    // needs the same Podman inspection path the soak uses.
    const requiresPodman = context.profile === 'load-soak'
      || context.profile === 'load-ramp'
    if (requiresPodman || checksSelectedPodman) {
      const containerChecks = containerEnginePreflight(context, ENGINE)
      loadRuntimeChecks.push(...containerChecks)
      if (context.containerEngine !== 'podman') {
        loadRuntimeChecks.push(
          failed(
            ENGINE,
            'load-soak-podman',
            `${context.profile} requires --container-engine podman for scoped container inspection.`,
          ),
        )
      } else {
        if (requiresPodman) {
          loadRuntimeChecks.push(
            passed(
              ENGINE,
              'load-soak-podman',
              `${context.profile} uses the selected Podman VM namespace.`,
            ),
          )
        }
        loadRuntimeChecks.push(
          containerChecks.some(
            (check) => check.id === 'container-engine-daemon'
              && check.status === 'passed',
          )
              ? await podmanClockPreflight(context)
              : failed(
                  ENGINE,
                  'load-podman-clock',
                  'The Podman VM clock requires a reachable Podman engine before collaboration load provisioning.',
                ),
        )
      }
    }
    return [
      ...common,
      repositoryFileCheck(
        context,
        ENGINE,
        'smoke-provisioner',
        'tests/verification/engines/collaboration-load-smoke.mjs',
      ),
      repositoryFileCheck(
        context,
        ENGINE,
        'smoke-fixture-builder',
        'tests/verification/fixtures/load-smoke.mjs',
      ),
      ...playwrightPreflight(ENGINE, []),
      environmentCheck(
        context,
        ENGINE,
        'admin-email',
        'INQTRIX_E2E_ADMIN_EMAIL',
      ),
      environmentCheck(
        context,
        ENGINE,
        'admin-password',
        'INQTRIX_E2E_ADMIN_PASSWORD',
      ),
      environmentCheck(
        context,
        ENGINE,
        'user-password',
        'INQTRIX_E2E_USER_PASSWORD',
      ),
      context.fixturePath
        ? failed(
            ENGINE,
            'generated-session-fixture',
            `${context.profile} provisions its own fixture and does not accept --fixture.`,
          )
        : passed(
            ENGINE,
            'generated-session-fixture',
            `${context.profile} will provision its private fixture in the run directory.`,
          ),
      baseURLCheck,
      ...loadRuntimeChecks,
    ]
  },
  async execute(context, cleanupLedger) {
    if (
      context.profile === 'load-smoke'
      || context.profile === 'load-soak'
      || context.profile === 'load-ramp'
    ) {
      const privateDirectory = resolve(
        context.reportDirectory,
        '.cleanup-secrets',
      )
      const generatedFixture = resolve(
        privateDirectory,
        `${context.profile}-session-fixture.json`,
      )
      await mkdir(privateDirectory, { recursive: true, mode: 0o700 })
      const fixtureCleanup = await cleanupLedger.register(
        'resource',
        `private ${context.profile} session fixture for current run`,
        async () => await unlink(generatedFixture).catch(
          (error: NodeJS.ErrnoException) => {
            if (error.code !== 'ENOENT') throw error
          },
        ),
      )
      try {
        return await runCommand(context, cleanupLedger, {
          args: [
            'tests/verification/engines/collaboration-load-smoke.mjs',
          ],
          command: process.execPath,
          engine: ENGINE,
          environment: {
            INQTRIX_E2E_CONTAINER_ENGINE: context.containerEngine ?? '',
            INQTRIX_LOAD_SESSION_FIXTURE: generatedFixture,
            INQTRIX_LOAD_PROFILE: context.profile,
          },
          productLifecycle: true,
        })
      } finally {
        await unlink(generatedFixture).catch(
          (error: NodeJS.ErrnoException) => {
            if (error.code !== 'ENOENT') throw error
          },
        )
        await cleanupLedger.complete(fixtureCleanup)
      }
    }
    const fixture = context.fixturePath
      ?? context.environment.INQTRIX_LOAD_SESSION_FIXTURE
    if (!fixture) throw new Error('Load fixture became unavailable after preflight.')
    return await runCommand(context, cleanupLedger, {
      args: [
        'tests/load/collaboration-load.mjs',
        '--mode',
        'capacity',
        '--fixture',
        fixture,
        '--json',
      ],
      command: process.execPath,
      engine: ENGINE,
    })
  },
}
