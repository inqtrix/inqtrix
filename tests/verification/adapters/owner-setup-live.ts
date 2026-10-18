import type { VerificationAdapter } from '../adapter.ts'
import { runCommand } from '../command-runner.ts'
import {
  environmentCheck,
  failed,
  passed,
  repositoryFileCheck,
} from '../preflight.ts'
import { playwrightPreflight } from './shared.ts'

const ENGINE = 'owner-setup-live' as const

export const ownerSetupLiveAdapter: VerificationAdapter = {
  engine: ENGINE,
  profiles: ['owner-setup'],
  async preflight(context) {
    const baseURL = context.environment.INQTRIX_E2E_BASE_URL
      ?? 'http://127.0.0.1:8080'
    const parsedBaseURL = parseLoopbackBaseURL(baseURL)
    const baseURLCheck = parsedBaseURL
      ? passed(
          ENGINE,
          'base-url',
          'The system base URL is a credential-free loopback HTTP(S) origin.',
        )
      : failed(
          ENGINE,
          'base-url',
          'The owner-setup base URL must be a credential-free loopback HTTP(S) origin.',
        )
    const browserCheck = context.browserTarget
      ? passed(
          ENGINE,
          'browser-target',
          `The run selects exactly one ${context.browserTarget} browser.`,
        )
      : failed(
          ENGINE,
          'browser-target',
          'The owner-setup profile requires one explicit browser target.',
        )
    const adminEmail = context.environment.INQTRIX_E2E_ADMIN_EMAIL?.trim()
    const testerEmail = context.environment.INQTRIX_E2E_TESTER_EMAIL?.trim()
    const distinctAccounts = (
      adminEmail
      && testerEmail
      && adminEmail.toLowerCase() !== testerEmail.toLowerCase()
    )
      ? passed(
          ENGINE,
          'account-separation',
          'The owner and created-user identities are explicitly distinct.',
        )
      : failed(
          ENGINE,
          'account-separation',
          'INQTRIX_E2E_ADMIN_EMAIL and INQTRIX_E2E_TESTER_EMAIL must identify distinct accounts.',
        )

    return [
      repositoryFileCheck(
        context,
        ENGINE,
        'engine-entrypoint',
        'e2e/engines/owner-setup-live.mjs',
      ),
      ...playwrightPreflight(
        ENGINE,
        context.browserTarget ? [context.browserTarget] : [],
      ),
      browserCheck,
      environmentCheck(context, ENGINE, 'admin-email', 'INQTRIX_E2E_ADMIN_EMAIL'),
      environmentCheck(context, ENGINE, 'tester-email', 'INQTRIX_E2E_TESTER_EMAIL'),
      environmentCheck(context, ENGINE, 'admin-password', 'INQTRIX_E2E_ADMIN_PASSWORD'),
      environmentCheck(context, ENGINE, 'user-password', 'INQTRIX_E2E_USER_PASSWORD'),
      distinctAccounts,
      baseURLCheck,
      await freshOwnerSetupCheck(parsedBaseURL),
    ]
  },
  async execute(context, cleanupLedger) {
    if (!context.browserTarget) {
      throw new Error('The owner-setup browser target was not selected.')
    }
    return await runCommand(context, cleanupLedger, {
      args: ['e2e/engines/owner-setup-live.mjs'],
      command: process.execPath,
      engine: ENGINE,
      environment: {
        INQTRIX_VERIFICATION_BROWSER_TARGET: context.browserTarget,
      },
    })
  },
}

function parseLoopbackBaseURL(value: string): URL | null {
  try {
    const parsed = new URL(value)
    const loopback = new Set(['127.0.0.1', '[::1]', '::1', 'localhost'])
    return (
      (parsed.protocol === 'http:' || parsed.protocol === 'https:')
      && loopback.has(parsed.hostname.toLowerCase())
      && !parsed.username
      && !parsed.password
      && !parsed.search
      && !parsed.hash
      && (parsed.pathname === '/' || parsed.pathname === '')
    )
      ? parsed
      : null
  } catch {
    return null
  }
}

async function freshOwnerSetupCheck(baseURL: URL | null) {
  if (!baseURL) {
    return failed(
      ENGINE,
      'fresh-owner-contract',
      'The fresh owner contract was not checked because the base URL is invalid.',
    )
  }
  try {
    const response = await fetch(`${baseURL.origin}/api/auth/config`, {
      signal: AbortSignal.timeout(10_000),
    })
    if (!response.ok) {
      return failed(
        ENGINE,
        'fresh-owner-contract',
        `The authentication configuration returned HTTP ${response.status}.`,
      )
    }
    const config = await response.json() as {
      auth_mode?: unknown
      auth_required?: unknown
      csrf_required?: unknown
      registration?: { needs_owner?: unknown }
      supports_logout?: unknown
    }
    return (
      config.auth_mode === 'local'
      && config.auth_required === true
      && config.csrf_required === true
      && config.registration?.needs_owner === true
      && config.supports_logout === true
    )
      ? passed(
          ENGINE,
          'fresh-owner-contract',
          'The reachable local-auth stack requires a first owner and publishes CSRF/logout support.',
        )
      : failed(
          ENGINE,
          'fresh-owner-contract',
          'The stack is not a fresh local-auth deployment with CSRF and logout support.',
        )
  } catch {
    return failed(
      ENGINE,
      'fresh-owner-contract',
      'The fresh local-auth stack is unreachable; start the isolated stack first.',
    )
  }
}
