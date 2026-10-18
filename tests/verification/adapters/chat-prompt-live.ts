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

const ENGINE = 'chat-prompt-live' as const

export const chatPromptLiveAdapter: VerificationAdapter = {
  engine: ENGINE,
  profiles: ['chat-prompt'],
  async preflight(context) {
    const baseURL = context.environment.INQTRIX_E2E_BASE_URL
      ?? 'http://127.0.0.1:8080'
    let baseURLCheck
    let origin: string | null = null
    try {
      const parsed = new URL(baseURL)
      const valid = ['http:', 'https:'].includes(parsed.protocol)
        && !parsed.username
        && !parsed.password
        && !parsed.search
        && !parsed.hash
      origin = valid ? parsed.origin : null
      baseURLCheck = valid
        ? passed(ENGINE, 'base-url', 'The system base URL is structurally valid.')
        : failed(ENGINE, 'base-url', 'The system base URL must be a credential-free HTTP(S) URL.')
    } catch {
      baseURLCheck = failed(ENGINE, 'base-url', 'The system base URL is invalid.')
    }
    // Strict live profile: never start product services here — an
    // unreachable stack blocks instead of failing scenarios.
    let reachabilityCheck
    if (!origin) {
      reachabilityCheck = failed(
        ENGINE,
        'live-stack-reachable',
        'Stack health not checked: invalid base URL.',
      )
    } else {
      try {
        const response = await fetch(`${origin}/health`, {
          signal: AbortSignal.timeout(10_000),
        })
        reachabilityCheck = response.ok
          ? passed(ENGINE, 'live-stack-reachable', 'The live stack answers its health endpoint.')
          : failed(
              ENGINE,
              'live-stack-reachable',
              `The stack health endpoint returned HTTP ${response.status}.`,
            )
      } catch {
        reachabilityCheck = failed(
          ENGINE,
          'live-stack-reachable',
          'The live stack is unreachable — start the canonical stack first.',
        )
      }
    }
    const executable = context.environment.PLAYWRIGHT_EXECUTABLE_PATH
    // Tester-only by design: one authenticated account creates and
    // deletes every resource through owner APIs; no admin credential.
    return [
      repositoryFileCheck(
        context,
        ENGINE,
        'engine-entrypoint',
        'e2e/engines/chat-prompt-live.mjs',
      ),
      // Firefox and WebKit always run from the Playwright-managed
      // runtimes; the executable override only replaces Chromium.
      ...playwrightPreflight(
        ENGINE,
        executable ? ['firefox', 'webkit'] : ['chromium', 'firefox', 'webkit'],
      ),
      executableCheck(context, ENGINE, 'browser-executable', executable),
      environmentCheck(context, ENGINE, 'tester-email', 'INQTRIX_E2E_TESTER_EMAIL'),
      environmentCheck(context, ENGINE, 'user-password', 'INQTRIX_E2E_USER_PASSWORD'),
      baseURLCheck,
      reachabilityCheck,
    ]
  },
  async execute(context, cleanupLedger) {
    // The engine writes the per-scenario sidecar file itself; runCommand
    // reads it back, so no scenarios are synthesized here.
    return await runCommand(context, cleanupLedger, {
      args: ['e2e/engines/chat-prompt-live.mjs'],
      command: process.execPath,
      engine: ENGINE,
      productLifecycle: true,
    })
  },
}
