import { existsSync } from 'node:fs'
import { createRequire } from 'node:module'

import type { PreflightCheck, VerificationEngine } from '../model.ts'
import {
  failed,
  passed,
} from '../preflight.ts'

export function resolvePlaywrightCli(): string | null {
  try {
    return createRequire(import.meta.url).resolve('@playwright/test/cli')
  } catch {
    return null
  }
}

export function playwrightPreflight(
  engine: VerificationEngine,
  managedBrowsers: readonly PlaywrightBrowser[] = ['chromium'],
): PreflightCheck[] {
  const checks = [
    resolvePlaywrightCli()
      ? passed(engine, 'playwright-cli', 'The local Playwright CLI is available.')
      : failed(engine, 'playwright-cli', 'The local Playwright CLI is unavailable.'),
  ]
  for (const browser of managedBrowsers) {
    const browserPath = resolvePlaywrightExecutable(browser)
    checks.push(
      browserPath && existsSync(browserPath)
        ? passed(
            engine,
            `playwright-browser-${browser}`,
            `The Playwright-managed ${browser} runtime is available.`,
          )
        : failed(
            engine,
            `playwright-browser-${browser}`,
            `The Playwright-managed ${browser} runtime is unavailable; install the pinned browser before running this profile.`,
          ),
    )
  }
  return checks
}

type PlaywrightBrowser = 'chromium' | 'firefox' | 'webkit'

function resolvePlaywrightExecutable(browser: PlaywrightBrowser): string | null {
  try {
    const playwright = createRequire(import.meta.url)('@playwright/test') as {
      chromium?: { executablePath(): string }
      firefox?: { executablePath(): string }
      webkit?: { executablePath(): string }
    }
    return playwright[browser]?.executablePath() ?? null
  } catch {
    return null
  }
}
