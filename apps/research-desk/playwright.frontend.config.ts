import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { defineConfig, devices } from '@playwright/test'

import { fixtureWebServer } from '../../e2e/fixture-server.ts'
import { fixtureWorkers } from '../../e2e/fixture-workers.ts'

const executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH
const appDirectory = path.dirname(fileURLToPath(import.meta.url))
const { baseURL, ...webServer } = fixtureWebServer(process.env, appDirectory)
const repositoryRoot = path.resolve(appDirectory, '../..')
const runId = process.env.INQTRIX_VERIFICATION_RUN_ID ?? 'developer'
const listOnly = process.argv.includes('--list')

export default defineConfig({
  fullyParallel: false,
  workers: fixtureWorkers(os.totalmem(), os.cpus().length),
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
    baseURL,
    headless: true,
  },
  webServer,
})
