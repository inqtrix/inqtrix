import type { VerificationAdapter } from '../adapter.ts'
import { runCommand } from '../command-runner.ts'
import {
  executableCheck,
  repositoryFileCheck,
} from '../preflight.ts'
import {
  playwrightPreflight,
  resolvePlaywrightCli,
} from './shared.ts'

const ENGINE = 'ui-fixture-playwright' as const

export const uiFixtureAdapter: VerificationAdapter = {
  engine: ENGINE,
  profiles: ['ui-fixture'],
  async preflight(context) {
    const executable = context.environment.PLAYWRIGHT_EXECUTABLE_PATH
    return [
      repositoryFileCheck(
        context,
        ENGINE,
        'playwright-config',
        'apps/research-desk/playwright.frontend.config.ts',
      ),
      repositoryFileCheck(
        context,
        ENGINE,
        'fixture-spec',
        'apps/research-desk/browser-tests/editorCollaborationLifecycle.spec.ts',
      ),
      repositoryFileCheck(
        context,
        ENGINE,
        'accessibility-spec',
        'apps/research-desk/browser-tests/accessibilityDemo.spec.ts',
      ),
      ...playwrightPreflight(
        ENGINE,
        executable ? ['firefox', 'webkit'] : ['chromium', 'firefox', 'webkit'],
      ),
      executableCheck(context, ENGINE, 'browser-executable', executable),
    ]
  },
  async execute(context, cleanupLedger) {
    const playwrightCli = resolvePlaywrightCli()
    if (!playwrightCli) {
      throw new Error('Playwright CLI became unavailable after preflight.')
    }
    return await runCommand(context, cleanupLedger, {
      args: [
        playwrightCli,
        'test',
        '--config',
        'apps/research-desk/playwright.frontend.config.ts',
      ],
      command: process.execPath,
      engine: ENGINE,
    })
  },
}
