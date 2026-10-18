import process from 'node:process'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  VERIFICATION_PROFILES,
  type ContainerEngine,
  type VerificationBrowser,
  type VerificationProfile,
  type VerificationReport,
} from './model.ts'
import { runVerification } from './orchestrator.ts'
import { createRedactor } from './redaction.ts'
import {
  isVerificationProfile,
} from './run-context.ts'
import {
  PROFILE_DESCRIPTIONS,
  scenariosForProfile,
} from './scenario-inventory.ts'

export type CliOptions = {
  browserTarget: VerificationBrowser | null
  containerEngine: ContainerEngine | null
  fixturePath: string | null
  help: boolean
  json: boolean
  list: boolean
  preflightOnly: boolean
  profile: VerificationProfile | null
  runId: string | null
}

export function parseCliArguments(args: readonly string[]): CliOptions {
  const options: CliOptions = {
    browserTarget: null,
    containerEngine: null,
    fixturePath: null,
    help: false,
    json: false,
    list: false,
    preflightOnly: false,
    profile: null,
    runId: null,
  }
  for (let index = 0; index < args.length; index += 1) {
    const argument = args[index]
    if (argument === '--help' || argument === '-h') options.help = true
    else if (argument === '--json') options.json = true
    else if (argument === '--list') options.list = true
    else if (argument === '--preflight-only') options.preflightOnly = true
    else if (argument === '--profile') {
      const value = requiredValue(args, ++index, argument)
      if (!isVerificationProfile(value)) {
        throw new Error(
          `Unknown profile "${value}". Expected one of: ${VERIFICATION_PROFILES.join(', ')}.`,
        )
      }
      options.profile = value
    } else if (argument === '--fixture') {
      options.fixturePath = requiredValue(args, ++index, argument)
    } else if (argument === '--browser') {
      const value = requiredValue(args, ++index, argument)
      if (value !== 'chromium' && value !== 'firefox' && value !== 'webkit') {
        throw new Error('--browser must be exactly "chromium", "firefox", or "webkit".')
      }
      options.browserTarget = value
    } else if (argument === '--container-engine') {
      const value = requiredValue(args, ++index, argument)
      if (value !== 'podman' && value !== 'docker') {
        throw new Error('--container-engine must be exactly "podman" or "docker".')
      }
      options.containerEngine = value
    } else if (argument === '--run-id') {
      options.runId = requiredValue(args, ++index, argument)
    } else {
      throw new Error(`Unknown verification argument: ${argument}`)
    }
  }
  if (!options.help && !options.list && options.profile === null) {
    throw new Error('--profile is required unless --help or --list is used.')
  }
  if (options.preflightOnly && options.list) {
    throw new Error('--preflight-only and --list cannot be combined.')
  }
  if (!options.help && !options.list && options.profile === 'owner-setup') {
    if (options.browserTarget === null) {
      throw new Error('owner-setup requires one explicit --browser target.')
    }
  } else if (options.browserTarget !== null && options.profile !== 'owner-setup') {
    throw new Error('--browser is reserved for the owner-setup profile.')
  }
  if (
    (options.profile === 'load-smoke' || options.profile === 'load-soak')
    && options.fixturePath
  ) {
    throw new Error(
      `${options.profile} provisions its own Run-ID-bound fixture; remove --fixture.`,
    )
  }
  return options
}

export async function main(
  args: readonly string[] = process.argv.slice(2),
  environment: NodeJS.ProcessEnv = process.env,
): Promise<number> {
  const redactor = createRedactor(environment)
  try {
    const options = parseCliArguments(args)
    if (options.help) {
      printHelp()
      return 0
    }
    if (options.list) {
      printInventory(options.profile, options.json)
      return 0
    }

    const abortController = new AbortController()
    const interrupt = (): void => abortController.abort()
    process.once('SIGINT', interrupt)
    process.once('SIGTERM', interrupt)
    try {
      const report = await runVerification({
        abortSignal: abortController.signal,
        browserTarget: options.browserTarget,
        containerEngine: options.containerEngine,
        environment,
        fixturePath: options.fixturePath,
        preflightOnly: options.preflightOnly,
        profile: options.profile!,
        runId: options.runId ?? undefined,
      })
      printSummary(report, options.json)
      return exitCode(report)
    } finally {
      process.removeListener('SIGINT', interrupt)
      process.removeListener('SIGTERM', interrupt)
    }
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown verification error.'
    process.stderr.write(`Verification failed before completion: ${redactor.redactMessage(message)}\n`)
    return 1
  }
}

function printInventory(
  selectedProfile: VerificationProfile | null,
  json: boolean,
): void {
  const profiles = selectedProfile ? [selectedProfile] : VERIFICATION_PROFILES
  const inventory = profiles.map((profile) => ({
    description: PROFILE_DESCRIPTIONS[profile],
    profile,
    scenarios: scenariosForProfile(profile).map((scenario) => ({
      destructive: scenario.destructive,
      engine: scenario.engine,
      id: scenario.id,
      title: scenario.title,
    })),
  }))
  if (json) {
    process.stdout.write(`${JSON.stringify(inventory, null, 2)}\n`)
    return
  }
  for (const entry of inventory) {
    process.stdout.write(`${entry.profile}: ${entry.description}\n`)
    for (const scenario of entry.scenarios) {
      process.stdout.write(
        `  ${scenario.id} [${scenario.engine}]${scenario.destructive ? ' destructive' : ''}\n`,
      )
    }
  }
}

function printSummary(report: VerificationReport, json: boolean): void {
  if (json) {
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`)
    return
  }
  const failedPreflight = report.preflight.filter((check) => check.status === 'failed')
  process.stdout.write(`Verification ${report.runId}: ${report.status}\n`)
  process.stdout.write(`  Inqtrix version: ${report.inqtrixVersion}\n`)
  process.stdout.write(`  profile: ${report.profile}\n`)
  process.stdout.write(`  scenarios: ${report.scenarios.length}\n`)
  process.stdout.write(`  adapters: ${report.adapters.length}\n`)
  process.stdout.write(`  preflight failures: ${failedPreflight.length}\n`)
  process.stdout.write(`  cleanup failures: ${report.cleanup.failed}\n`)
  process.stdout.write(`  retained review resources: ${report.cleanup.retained}\n`)
  process.stdout.write(
    `  report: e2e/.results/verification/${report.runId}/report.json\n`,
  )
}

function printHelp(): void {
  process.stdout.write(
    'Usage: node --experimental-strip-types tests/verification/cli.ts '
    + '--profile PROFILE [--browser chromium|firefox|webkit] [--fixture PATH] '
    + '[--container-engine podman|docker] '
    + '[--run-id ID] [--preflight-only] [--json]\n',
  )
  process.stdout.write(
    '       node --experimental-strip-types tests/verification/cli.ts '
    + '--list [--profile PROFILE] [--json]\n\n',
  )
  process.stdout.write(`Profiles: ${VERIFICATION_PROFILES.join(', ')}\n`)
  process.stdout.write(
    'Generated fault-injection and edge-conformance require an explicit '
    + '--container-engine; neither auto-selects or falls back.\n',
  )
  process.stdout.write(
    'load-smoke and load-soak provision temporary accounts, one document, '
    + 'shares, and private leases; --fixture is reserved for external fixture profiles.\n',
  )
  process.stdout.write(
    'owner-setup requires exactly one explicit browser and an externally prepared fresh stack.\n',
  )
  process.stdout.write('The CLI accepts no Playwright selectors, reporter overrides, or capacity overrides.\n')
}

function exitCode(report: VerificationReport): number {
  if (report.status === 'passed' || report.status === 'preflight_passed') return 0
  if (report.status === 'blocked') return 2
  if (report.status === 'cleanup_failed') return 3
  if (report.status === 'interrupted') return 130
  return 1
}

function requiredValue(
  args: readonly string[],
  index: number,
  option: string,
): string {
  const value = args[index]
  if (!value || value.startsWith('--')) throw new Error(`${option} requires a value.`)
  return value
}

const modulePath = fileURLToPath(import.meta.url)
if (resolve(process.argv[1] ?? '') === modulePath) {
  process.exitCode = await main()
}
