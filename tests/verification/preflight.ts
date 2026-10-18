import {
  accessSync,
  constants,
  statSync,
} from 'node:fs'
import { isAbsolute, resolve } from 'node:path'

import type {
  PreflightCheck,
  VerificationEngine,
} from './model.ts'
import type { RunContext } from './run-context.ts'

export function passed(
  engine: VerificationEngine,
  id: string,
  message: string,
): PreflightCheck {
  return { engine, id, message, status: 'passed' }
}

export function failed(
  engine: VerificationEngine,
  id: string,
  message: string,
): PreflightCheck {
  return { engine, id, message, status: 'failed' }
}

export function repositoryFileCheck(
  context: RunContext,
  engine: VerificationEngine,
  id: string,
  repositoryPath: string,
): PreflightCheck {
  const absolutePath = resolve(context.repositoryRoot, repositoryPath)
  return readableFile(absolutePath)
    ? passed(engine, id, `${repositoryPath} is available.`)
    : failed(engine, id, `${repositoryPath} is missing.`)
}

export function configuredFileCheck(
  context: RunContext,
  engine: VerificationEngine,
  id: string,
  value: string | null | undefined,
  variableName: string,
): PreflightCheck {
  if (!value?.trim()) {
    return failed(engine, id, `${variableName} is not configured.`)
  }
  const path = isAbsolute(value) ? value : resolve(context.repositoryRoot, value)
  return readableFile(path)
    ? passed(engine, id, `${variableName} points to an available file.`)
    : failed(engine, id, `${variableName} points to a missing or unreadable file.`)
}

export function configuredPrivateFileCheck(
  context: RunContext,
  engine: VerificationEngine,
  id: string,
  value: string | null | undefined,
  variableName: string,
): PreflightCheck {
  const available = configuredFileCheck(
    context,
    engine,
    id,
    value,
    variableName,
  )
  if (available.status === 'failed' || process.platform === 'win32') {
    return available
  }
  const path = isAbsolute(value!) ? value! : resolve(context.repositoryRoot, value!)
  try {
    return (statSync(path).mode & 0o077) === 0
      ? passed(engine, id, `${variableName} points to a private readable file.`)
      : failed(
          engine,
          id,
          `${variableName} must not be accessible by group or other users.`,
        )
  } catch {
    return failed(engine, id, `${variableName} points to a missing or unreadable file.`)
  }
}

export function environmentCheck(
  context: RunContext,
  engine: VerificationEngine,
  id: string,
  variableName: string,
): PreflightCheck {
  return context.environment[variableName]?.trim()
    ? passed(engine, id, `${variableName} is configured.`)
    : failed(engine, id, `${variableName} is not configured.`)
}

export function executableCheck(
  context: RunContext,
  engine: VerificationEngine,
  id: string,
  value: string | undefined,
): PreflightCheck {
  if (!value?.trim()) {
    return passed(engine, id, 'Playwright-managed browser resolution is selected.')
  }
  const path = isAbsolute(value) ? value : resolve(context.repositoryRoot, value)
  return executableFile(path)
    ? passed(engine, id, 'The configured browser executable is available.')
    : failed(engine, id, 'The configured browser executable is missing or not executable.')
}

function readableFile(path: string): boolean {
  try {
    accessSync(path, constants.R_OK)
    return statSync(path).isFile()
  } catch {
    return false
  }
}

function executableFile(path: string): boolean {
  try {
    accessSync(path, constants.X_OK)
    return statSync(path).isFile()
  } catch {
    return false
  }
}
