import { join } from 'node:path'

import { defineConfig, devices, type Project } from '@playwright/test'

import {
  collaborationE2EConfiguration,
  type CollaborationTransport,
} from './e2e/config'
import {
  CHROMIUM_ONLY_SCENARIO_TAG,
  COLLABORATION_BROWSER_TARGETS,
  MOBILE_ONLY_SCENARIO_TAG,
  MOBILE_SCENARIO_TAG,
  type CollaborationBrowserTarget,
} from './tests/verification/scenario-inventory'

const executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH
const listOnly = process.argv.includes('--list')
const projects = collaborationE2EConfiguration.selectedTransports
  .flatMap((transport) => (
  COLLABORATION_BROWSER_TARGETS.map((target) => project(transport, target))
  ))

export default defineConfig({
  expect: { timeout: 15_000 },
  forbidOnly: Boolean(process.env.CI) || collaborationE2EConfiguration.mode === 'strict',
  fullyParallel: false,
  outputDir: join(
    'e2e',
    '.results',
    'playwright',
    process.env.INQTRIX_VERIFICATION_RUN_ID ?? 'developer',
  ),
  projects,
  reporter: collaborationE2EConfiguration.mode === 'strict' && !listOnly
    ? [
        ['list', { printSteps: true }],
        ['./e2e/scenario-reporter.ts'],
      ]
    : [['list', { printSteps: true }]],
  retries: 0,
  testDir: './e2e/scenarios',
  testMatch: 'collaboration.system.spec.ts',
  timeout: 60_000,
  workers: 1,
})

function project(
  transport: CollaborationTransport,
  target: CollaborationBrowserTarget,
): Project {
  const endpoint = collaborationE2EConfiguration.stack?.transports[transport].baseURL
  const device = target.formFactor === 'mobile'
    ? devices['Pixel 7']
    : target.browser === 'firefox'
      ? devices['Desktop Firefox']
      : target.browser === 'webkit'
        ? devices['Desktop Safari']
        : devices['Desktop Chrome']
  const excludedTags = [
    ...(target.formFactor === 'desktop' ? [MOBILE_ONLY_SCENARIO_TAG] : []),
    ...(target.browser === 'chromium' ? [] : [CHROMIUM_ONLY_SCENARIO_TAG]),
  ]
  return {
    grep: target.formFactor === 'mobile'
      ? new RegExp(`${MOBILE_SCENARIO_TAG}|@layout`)
      : undefined,
    grepInvert: excludedTags.length > 0
      ? new RegExp(excludedTags.join('|'))
      : undefined,
    metadata: {
      browser: target.browser,
      formFactor: target.formFactor,
      transport,
    },
    name: `${transport}-${target.browser}-${target.formFactor}`,
    use: {
      ...device,
      baseURL: endpoint ?? 'http://127.0.0.1:9',
      browserName: target.browser,
      ignoreHTTPSErrors:
        process.env.INQTRIX_E2E_IGNORE_HTTPS_ERRORS === '1',
      launchOptions: executablePath && target.browser === 'chromium'
        ? { executablePath }
        : undefined,
      screenshot: 'only-on-failure',
      trace: 'off',
      video: 'off',
    },
  }
}
