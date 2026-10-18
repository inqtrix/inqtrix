import { mkdir, unlink } from 'node:fs/promises'
import { resolve } from 'node:path'

import type { VerificationAdapter } from '../adapter.ts'
import { runCommand } from '../command-runner.ts'
import { containerEnginePreflight } from '../container-engine.ts'
import {
  environmentCheck,
  executableCheck,
  failed,
  passed,
  repositoryFileCheck,
} from '../preflight.ts'
import {
  normalizeSystemSmokeBaseURL,
} from '../fixtures/collaboration-system-smoke.mjs'
import {
  playwrightGrep,
} from '../scenario-inventory.ts'
import {
  loadCollaborationE2EConfiguration,
  strictPreflightReasons,
} from '../../../e2e/config.ts'
import {
  playwrightPreflight,
  resolvePlaywrightCli,
} from './shared.ts'

const ENGINE = 'collaboration-playwright' as const

export const collaborationPlaywrightAdapter: VerificationAdapter = {
  engine: ENGINE,
  profiles: ['system-smoke', 'fault-injection'],
  async preflight(context) {
    if (usesGeneratedSystemFixture(context)) {
      return generatedSystemPreflight(context)
    }
    if (usesGeneratedFaultFixture(context)) {
      return generatedFaultPreflight(context)
    }
    const environment = collaborationEnvironment(context)
    const executable = context.environment.PLAYWRIGHT_EXECUTABLE_PATH
    const configuration = loadCollaborationE2EConfiguration(
      environment,
      context.repositoryRoot,
    )
    const reasons = strictPreflightReasons(
      configuration,
      context.profile,
      environment,
    )
    return [
      repositoryFileCheck(
        context,
        ENGINE,
        'playwright-config',
        'playwright.config.ts',
      ),
      repositoryFileCheck(
        context,
        ENGINE,
        'scenario-spec',
        'e2e/scenarios/collaboration.system.spec.ts',
      ),
      ...playwrightPreflight(
        ENGINE,
        executable ? ['firefox', 'webkit'] : ['chromium', 'firefox', 'webkit'],
      ),
      executableCheck(context, ENGINE, 'browser-executable', executable),
      ...(reasons.length === 0
        ? [passed(ENGINE, 'external-fixture', 'The external collaboration fixture satisfies the selected profile.')]
        : reasons.map((reason, index) => failed(
            ENGINE,
            `external-fixture-${String(index + 1).padStart(2, '0')}`,
            reason,
          ))),
    ]
  },
  async execute(context, cleanupLedger) {
    const playwrightCli = resolvePlaywrightCli()
    if (!playwrightCli) {
      throw new Error('Playwright CLI became unavailable after preflight.')
    }
    if (usesGeneratedSystemFixture(context)) {
      const privateDirectory = resolve(
        context.reportDirectory,
        '.cleanup-secrets',
      )
      const generatedFixture = resolve(
        privateDirectory,
        'collaboration-system-smoke-fixture.json',
      )
      await mkdir(privateDirectory, { recursive: true, mode: 0o700 })
      const fixtureCleanup = await cleanupLedger.register(
        'resource',
        'private collaboration system-smoke fixture for current run',
        async () => await unlink(generatedFixture).catch(
          (error: NodeJS.ErrnoException) => {
            if (error.code !== 'ENOENT') throw error
          },
        ),
      )
      try {
        return await runCommand(context, cleanupLedger, {
          args: [
            'tests/verification/engines/collaboration-system-smoke.mjs',
          ],
          command: process.execPath,
          engine: ENGINE,
          environment: {
            INQTRIX_E2E_FIXTURE: generatedFixture,
            INQTRIX_E2E_MODE: 'strict',
            INQTRIX_E2E_PLAYWRIGHT_CLI: playwrightCli,
            INQTRIX_E2E_PLAYWRIGHT_GREP: playwrightGrep(context.profile),
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
    if (usesGeneratedFaultFixture(context)) {
      const privateDirectory = resolve(
        context.reportDirectory,
        '.cleanup-secrets',
      )
      const generatedFixture = resolve(
        privateDirectory,
        'collaboration-fault-injection-fixture.json',
      )
      await mkdir(privateDirectory, { recursive: true, mode: 0o700 })
      const fixtureCleanup = await cleanupLedger.register(
        'resource',
        'private collaboration fault-injection fixture for current run',
        async () => await unlink(generatedFixture).catch(
          (error: NodeJS.ErrnoException) => {
            if (error.code !== 'ENOENT') throw error
          },
        ),
      )
      try {
        return await runCommand(context, cleanupLedger, {
          args: [
            'tests/verification/engines/collaboration-fault-injection.mjs',
          ],
          command: process.execPath,
          engine: ENGINE,
          environment: {
            INQTRIX_E2E_CONTAINER_ENGINE: context.containerEngine!,
            INQTRIX_E2E_FIXTURE: generatedFixture,
            INQTRIX_E2E_MODE: 'strict',
            INQTRIX_E2E_PLAYWRIGHT_CLI: playwrightCli,
            INQTRIX_E2E_PLAYWRIGHT_GREP: playwrightGrep(context.profile),
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
    return await runCommand(context, cleanupLedger, {
      args: [
        playwrightCli,
        'test',
        '--config',
        'playwright.config.ts',
        '--grep',
        playwrightGrep(context.profile),
      ],
      command: process.execPath,
      engine: ENGINE,
      environment: collaborationEnvironment(context),
    })
  },
}

function collaborationEnvironment(
  context: Parameters<VerificationAdapter['preflight']>[0],
): NodeJS.ProcessEnv {
  return {
    ...context.environment,
    INQTRIX_E2E_FIXTURE: context.fixturePath
      ?? context.environment.INQTRIX_E2E_FIXTURE,
    INQTRIX_E2E_MODE: 'strict',
  }
}

function configuredFixture(
  context: Parameters<VerificationAdapter['preflight']>[0],
): string | null {
  const value = context.fixturePath
    ?? context.environment.INQTRIX_E2E_FIXTURE
  return value?.trim() || null
}

function usesGeneratedSystemFixture(
  context: Parameters<VerificationAdapter['preflight']>[0],
): boolean {
  return context.profile === 'system-smoke' && configuredFixture(context) === null
}

function usesGeneratedFaultFixture(
  context: Parameters<VerificationAdapter['preflight']>[0],
): boolean {
  return context.profile === 'fault-injection' && configuredFixture(context) === null
}

function generatedSystemPreflight(
  context: Parameters<VerificationAdapter['preflight']>[0],
) {
  const executable = context.environment.PLAYWRIGHT_EXECUTABLE_PATH
  let baseURLCheck
  try {
    normalizeSystemSmokeBaseURL(
      context.environment.INQTRIX_E2E_BASE_URL
        ?? 'http://127.0.0.1:8080',
    )
    baseURLCheck = passed(
      ENGINE,
      'base-url',
      'The system-smoke base URL is structurally valid.',
    )
  } catch {
    baseURLCheck = failed(
      ENGINE,
      'base-url',
      'INQTRIX_E2E_BASE_URL must be a credential-free HTTP(S) URL.',
    )
  }
  return [
    repositoryFileCheck(
      context,
      ENGINE,
      'playwright-config',
      'playwright.config.ts',
    ),
    repositoryFileCheck(
      context,
      ENGINE,
      'scenario-spec',
      'e2e/scenarios/collaboration.system.spec.ts',
    ),
    repositoryFileCheck(
      context,
      ENGINE,
      'system-fixture-engine',
      'tests/verification/engines/collaboration-system-smoke.mjs',
    ),
    repositoryFileCheck(
      context,
      ENGINE,
      'system-fixture-builder',
      'tests/verification/fixtures/collaboration-system-smoke.mjs',
    ),
    ...playwrightPreflight(
      ENGINE,
      executable ? ['firefox', 'webkit'] : ['chromium', 'firefox', 'webkit'],
    ),
    executableCheck(context, ENGINE, 'browser-executable', executable),
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
    passed(
      ENGINE,
      'generated-system-fixture',
      'system-smoke will provision its private active-transport fixture in the run directory.',
    ),
    baseURLCheck,
  ]
}

function generatedFaultPreflight(
  context: Parameters<VerificationAdapter['preflight']>[0],
) {
  const executable = context.environment.PLAYWRIGHT_EXECUTABLE_PATH
  let baseURLCheck
  try {
    normalizeSystemSmokeBaseURL(
      context.environment.INQTRIX_E2E_BASE_URL
        ?? 'http://127.0.0.1:8080',
    )
    baseURLCheck = passed(
      ENGINE,
      'base-url',
      'The fault-injection base URL is structurally valid.',
    )
  } catch {
    baseURLCheck = failed(
      ENGINE,
      'base-url',
      'INQTRIX_E2E_BASE_URL must be a credential-free HTTP(S) URL.',
    )
  }
  return [
    repositoryFileCheck(
      context,
      ENGINE,
      'playwright-config',
      'playwright.config.ts',
    ),
    repositoryFileCheck(
      context,
      ENGINE,
      'scenario-spec',
      'e2e/scenarios/collaboration.system.spec.ts',
    ),
    repositoryFileCheck(
      context,
      ENGINE,
      'fault-fixture-engine',
      'tests/verification/engines/collaboration-fault-injection.mjs',
    ),
    repositoryFileCheck(
      context,
      ENGINE,
      'fault-fixture-builder',
      'tests/verification/fixtures/collaboration-fault-injection.mjs',
    ),
    repositoryFileCheck(
      context,
      ENGINE,
      'fault-controller',
      'tests/verification/fixtures/fault-control-server.mjs',
    ),
    ...playwrightPreflight(
      ENGINE,
      executable ? ['firefox', 'webkit'] : ['chromium', 'firefox', 'webkit'],
    ),
    executableCheck(context, ENGINE, 'browser-executable', executable),
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
    ...containerEnginePreflight(context, ENGINE),
    passed(
      ENGINE,
      'generated-fault-fixture',
      'fault-injection will provision a private active-transport fixture and loopback control plane in the run directory.',
    ),
    baseURLCheck,
  ]
}
