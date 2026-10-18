import type { VerificationAdapter } from '../adapter.ts'
import { runCommand } from '../command-runner.ts'
import {
  environmentCheck,
  executableCheck,
  failed,
  passed,
  repositoryFileCheck,
} from '../preflight.ts'
import { playwrightPreflight } from './shared.ts'

const ENGINE = 'editor-system-live' as const

export const editorSystemLiveAdapter: VerificationAdapter = {
  engine: ENGINE,
  profiles: ['system-smoke'],
  async preflight(context) {
    const baseURL = context.environment.INQTRIX_E2E_BASE_URL
      ?? 'http://127.0.0.1:8080'
    let baseURLCheck
    try {
      const parsed = new URL(baseURL)
      baseURLCheck = ['http:', 'https:'].includes(parsed.protocol)
        && !parsed.username
        && !parsed.password
        && !parsed.search
        && !parsed.hash
        ? passed(ENGINE, 'base-url', 'The system base URL is structurally valid.')
        : failed(ENGINE, 'base-url', 'The system base URL must be a credential-free HTTP(S) URL.')
    } catch {
      baseURLCheck = failed(ENGINE, 'base-url', 'The system base URL is invalid.')
    }
    const executable = context.environment.PLAYWRIGHT_EXECUTABLE_PATH
    const adminEmail = context.environment.INQTRIX_E2E_ADMIN_EMAIL?.trim()
    const testerEmail = context.environment.INQTRIX_E2E_TESTER_EMAIL?.trim()
    const distinctSeedAccounts = (
      adminEmail
      && testerEmail
      && adminEmail.toLowerCase() !== testerEmail.toLowerCase()
    )
      ? passed(
          ENGINE,
          'seed-account-separation',
          'The admin and tester identities are explicitly distinct.',
        )
      : failed(
          ENGINE,
          'seed-account-separation',
          'INQTRIX_E2E_ADMIN_EMAIL and INQTRIX_E2E_TESTER_EMAIL must identify distinct accounts.',
        )
    return [
      repositoryFileCheck(
        context,
        ENGINE,
        'engine-entrypoint',
        'e2e/engines/editor-system-live.mjs',
      ),
      ...playwrightPreflight(ENGINE, executable ? [] : ['chromium']),
      executableCheck(context, ENGINE, 'browser-executable', executable),
      environmentCheck(context, ENGINE, 'admin-email', 'INQTRIX_E2E_ADMIN_EMAIL'),
      environmentCheck(context, ENGINE, 'tester-email', 'INQTRIX_E2E_TESTER_EMAIL'),
      environmentCheck(context, ENGINE, 'admin-password', 'INQTRIX_E2E_ADMIN_PASSWORD'),
      environmentCheck(context, ENGINE, 'user-password', 'INQTRIX_E2E_USER_PASSWORD'),
      distinctSeedAccounts,
      baseURLCheck,
    ]
  },
  async execute(context, cleanupLedger) {
    const result = await runCommand(context, cleanupLedger, {
      args: ['e2e/engines/editor-system-live.mjs'],
      command: process.execPath,
      engine: ENGINE,
      productLifecycle: true,
    })
    return {
      ...result,
      scenarios: [{
        id: 'system.multiuser-live-matrix',
        status: result.status === 'passed' ? 'passed' : 'failed',
      }],
    }
  },
}
