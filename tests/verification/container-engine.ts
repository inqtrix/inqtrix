import {
  spawn,
  spawnSync,
  type ChildProcess,
} from 'node:child_process'

import type {
  ContainerEngine,
  PreflightCheck,
  VerificationEngine,
} from './model.ts'
import {
  failed,
  passed,
} from './preflight.ts'
import type { RunContext } from './run-context.ts'

const MAX_CAPTURE_BYTES = 2_000_000
const PROCESS_STOP_TIMEOUT_MS = 5_000

export type ContainerCommandResult = {
  exitCode: number | null
  signal: NodeJS.Signals | null
  stderr: string
  stdout: string
}

export type ContainerCommandOptions = {
  abortSignal?: AbortSignal
  cwd: string
  environment?: NodeJS.ProcessEnv
}

export type ContainerResourceNames = {
  backendContainer: string
  label: string
  network: string
  nginxContainer: string
  nginxImage: string
  pythonContainer: string
  pythonImage: string
}

export function containerResourceNames(runId: string): ContainerResourceNames {
  const suffix = runId.replace(/^inqv-/, '')
  const prefix = `inqtrix-edge-${suffix}`
  return {
    backendContainer: `${prefix}-backend`,
    label: `io.inqtrix.verification.run=${runId}`,
    network: `${prefix}-network`,
    nginxContainer: `${prefix}-nginx`,
    nginxImage: `inqtrix-edge-nginx:${suffix}`,
    pythonContainer: `${prefix}-python`,
    pythonImage: `inqtrix-edge-python:${suffix}`,
  }
}

export function containerEnginePreflight(
  context: RunContext,
  verificationEngine: VerificationEngine,
): PreflightCheck[] {
  const selected = context.containerEngine
  if (!selected) {
    return [
      failed(
        verificationEngine,
        'container-engine-selected',
        'Container-backed verification is opt-in and requires an explicit --container-engine podman|docker.',
      ),
    ]
  }
  const executable = spawnSync(selected, ['version'], {
    cwd: context.repositoryRoot,
    encoding: 'utf8',
    env: context.environment,
    stdio: 'ignore',
    timeout: 10_000,
  })
  if (executable.error || executable.status !== 0) {
    return [
      passed(
        verificationEngine,
        'container-engine-selected',
        `The ${selected} engine was selected explicitly.`,
      ),
      failed(
        verificationEngine,
        'container-engine-executable',
        `The selected ${selected} executable is unavailable or unusable.`,
      ),
    ]
  }
  const daemon = spawnSync(selected, ['info'], {
    cwd: context.repositoryRoot,
    encoding: 'utf8',
    env: context.environment,
    stdio: 'ignore',
    timeout: 15_000,
  })
  return [
    passed(
      verificationEngine,
      'container-engine-selected',
      `The ${selected} engine was selected explicitly.`,
    ),
    passed(
      verificationEngine,
      'container-engine-executable',
      `The selected ${selected} executable is available.`,
    ),
    daemon.error || daemon.status !== 0
      ? failed(
          verificationEngine,
          'container-engine-daemon',
          `The selected ${selected} engine is not reachable.`,
        )
      : passed(
          verificationEngine,
          'container-engine-daemon',
          `The selected ${selected} engine is reachable.`,
        ),
  ]
}

export async function runContainerCommand(
  engine: ContainerEngine,
  args: readonly string[],
  options: ContainerCommandOptions,
): Promise<ContainerCommandResult> {
  let child: ChildProcess | null = null
  let forceKillTimer: ReturnType<typeof setTimeout> | null = null
  let stdout = ''
  let stderr = ''
  const append = (current: string, chunk: Buffer): string => {
    if (current.length >= MAX_CAPTURE_BYTES) return current
    return `${current}${chunk.toString('utf8')}`.slice(0, MAX_CAPTURE_BYTES)
  }

  return await new Promise<ContainerCommandResult>((resolveResult, reject) => {
    const finish = (
      exitCode: number | null,
      signal: NodeJS.Signals | null,
    ): void => {
      if (forceKillTimer) clearTimeout(forceKillTimer)
      options.abortSignal?.removeEventListener('abort', abort)
      resolveResult({ exitCode, signal, stderr, stdout })
    }
    const abort = (): void => {
      if (!child || child.exitCode !== null || child.signalCode !== null) return
      child.kill('SIGTERM')
      forceKillTimer = setTimeout(() => {
        if (child && child.exitCode === null && child.signalCode === null) {
          child.kill('SIGKILL')
        }
      }, PROCESS_STOP_TIMEOUT_MS)
      forceKillTimer.unref()
    }

    try {
      child = spawn(engine, [...args], {
        cwd: options.cwd,
        env: options.environment ?? {},
        shell: false,
        stdio: ['ignore', 'pipe', 'pipe'],
      })
    } catch (error) {
      reject(error)
      return
    }
    child.stdout?.on('data', (chunk: Buffer) => {
      stdout = append(stdout, chunk)
    })
    child.stderr?.on('data', (chunk: Buffer) => {
      stderr = append(stderr, chunk)
    })
    options.abortSignal?.addEventListener('abort', abort, { once: true })
    if (options.abortSignal?.aborted) abort()
    child.once('error', reject)
    child.once('exit', finish)
  })
}

export function requireContainerCommand(
  result: ContainerCommandResult,
  operation: string,
): void {
  if (result.exitCode === 0) return
  throw new Error(`${operation} failed in the selected container engine.`)
}
