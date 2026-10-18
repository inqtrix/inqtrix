import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { defineConfig, devices } from '@playwright/test'

const port = 5187
const executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH
const appDirectory = path.dirname(fileURLToPath(import.meta.url))
const repositoryRoot = path.resolve(appDirectory, '../..')
const runId = process.env.INQTRIX_VERIFICATION_RUN_ID ?? 'developer'
const listOnly = process.argv.includes('--list')

export default defineConfig({
  fullyParallel: false,
  outputDir: path.join(
    repositoryRoot,
    'e2e',
    '.results',
    'playwright',
    runId,
    'ui-fixture',
  ),
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        launchOptions: executablePath ? { executablePath } : undefined,
      },
    },
    {
      name: 'firefox',
      use: { ...devices['Desktop Firefox'] },
    },
    {
      name: 'webkit',
      use: { ...devices['Desktop Safari'] },
    },
  ],
  reporter: listOnly
    ? [['list', { printSteps: true }]]
    : [
        ['list', { printSteps: true }],
        [path.join(repositoryRoot, 'e2e', 'ui-scenario-reporter.ts')],
      ],
  testDir: './browser-tests',
  use: {
    baseURL: `http://127.0.0.1:${port}`,
    headless: true,
  },
  webServer: {
    command: `npm exec -- vite --host 127.0.0.1 --port ${port} --strictPort`,
    cwd: appDirectory,
    reuseExistingServer: true,
    timeout: 120_000,
    url: `http://127.0.0.1:${port}/browser-tests/fixtures/editor-lifecycle.html`,
  },
})
