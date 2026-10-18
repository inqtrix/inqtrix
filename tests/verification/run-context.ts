import { randomUUID } from 'node:crypto'
import { mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  VERIFICATION_PROFILES,
  type ContainerEngine,
  type VerificationBrowser,
  type VerificationProfile,
} from './model.ts'

const MODULE_DIRECTORY = dirname(fileURLToPath(import.meta.url))
const RUN_ID_PATTERN = /^inqv-[a-z0-9][a-z0-9-]{7,75}$/

export type RunContext = {
  abortSignal: AbortSignal
  browserTarget: VerificationBrowser | null
  containerEngine: ContainerEngine | null
  environment: NodeJS.ProcessEnv
  fixturePath: string | null
  preflightOnly: boolean
  profile: VerificationProfile
  reportDirectory: string
  reportPath: string
  repositoryRoot: string
  runId: string
  startedAt: string
}

export type RunContextOptions = {
  abortSignal?: AbortSignal
  browserTarget?: VerificationBrowser | null
  containerEngine?: ContainerEngine | null
  environment?: NodeJS.ProcessEnv
  fixturePath?: string | null
  preflightOnly?: boolean
  profile: VerificationProfile
  repositoryRoot?: string
  runId?: string
}

export function isVerificationProfile(value: string): value is VerificationProfile {
  return (VERIFICATION_PROFILES as readonly string[]).includes(value)
}

export function createRunId(now = new Date(), uuid = randomUUID()): string {
  const timestamp = now.toISOString()
    .replaceAll(/[-:]/g, '')
    .replace(/\.\d{3}Z$/, 'z')
    .toLowerCase()
  return `inqv-${timestamp}-${uuid.slice(0, 8).toLowerCase()}`
}

export function validateRunId(value: string): string {
  if (!RUN_ID_PATTERN.test(value)) {
    throw new Error(
      'Run ID must start with "inqv-" and contain only lowercase letters, digits, and hyphens.',
    )
  }
  return value
}

export async function createRunContext(options: RunContextOptions): Promise<RunContext> {
  const repositoryRoot = options.repositoryRoot
    ? resolve(options.repositoryRoot)
    : resolve(MODULE_DIRECTORY, '../..')
  const runId = validateRunId(options.runId ?? createRunId())
  const reportRoot = resolve(
    repositoryRoot,
    'e2e',
    '.results',
    'verification',
  )
  const reportDirectory = resolve(
    reportRoot,
    runId,
  )
  await mkdir(reportRoot, { recursive: true, mode: 0o700 })
  try {
    await mkdir(reportDirectory, { recursive: false, mode: 0o700 })
  } catch (error) {
    if (
      error
      && typeof error === 'object'
      && 'code' in error
      && error.code === 'EEXIST'
    ) {
      throw new Error('Verification Run ID already has a report directory.')
    }
    throw error
  }
  return {
    abortSignal: options.abortSignal ?? new AbortController().signal,
    browserTarget: options.browserTarget ?? null,
    containerEngine: options.containerEngine ?? null,
    environment: { ...(options.environment ?? process.env) },
    fixturePath: options.fixturePath ?? null,
    preflightOnly: options.preflightOnly ?? false,
    profile: options.profile,
    reportDirectory,
    reportPath: resolve(reportDirectory, 'report.json'),
    repositoryRoot,
    runId,
    startedAt: new Date().toISOString(),
  }
}
