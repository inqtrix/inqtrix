import { spawn, type ChildProcess } from 'node:child_process'
import { unlink } from 'node:fs/promises'

import type { CleanupLedger } from './cleanup-ledger.ts'
import type {
  EngineResult,
  VerificationEngine,
} from './model.ts'
import type { RunContext } from './run-context.ts'
import {
  ProductResourceController,
  type ProductCleanup,
} from './fixtures/product-resource.ts'
import { createRedactor, type Redactor } from './redaction.ts'
import {
  readScenarioResults,
  scenarioResultsPath,
} from './scenario-results.ts'

export type CommandSpec = {
  args: readonly string[]
  command: string
  engine: VerificationEngine
  environment?: NodeJS.ProcessEnv
  output?: {
    stderr: NodeJS.WritableStream
    stdout: NodeJS.WritableStream
  }
  productCleanup?: ProductCleanup
  productLifecycle?: boolean
}

export async function runCommand(
  context: RunContext,
  cleanupLedger: CleanupLedger,
  spec: CommandSpec,
): Promise<EngineResult> {
  const startedAt = new Date()
  const resultsPath = scenarioResultsPath(context, spec.engine)
  await unlink(resultsPath).catch(() => undefined)
  let child: ChildProcess | null = null
  const productResources = spec.productLifecycle
    ? new ProductResourceController(
        context,
        cleanupLedger,
        spec.productCleanup,
      )
    : null
  const cleanupHandle = await cleanupLedger.register(
    'process',
    `${spec.engine} child process`,
    async () => {
      if (!child || child.exitCode !== null || child.signalCode !== null) return
      child.kill('SIGTERM')
      await waitForExit(child, 5_000)
      if (child.exitCode === null && child.signalCode === null) {
        child.kill('SIGKILL')
        await waitForExit(child, 1_000)
      }
      if (child.exitCode === null && child.signalCode === null) {
        throw new Error('Child process did not terminate during cleanup.')
      }
    },
  )

  if (context.abortSignal.aborted) {
    await cleanupLedger.complete(cleanupHandle)
    return result(spec.engine, startedAt, 'interrupted', null, 'SIGTERM', [])
  }

  return await new Promise<EngineResult>((resolveResult) => {
    let forceKillTimer: ReturnType<typeof setTimeout> | null = null
    let settled = false
    const settle = async (
      status: EngineResult['status'],
      exitCode: number | null,
      signal: NodeJS.Signals | null,
    ): Promise<void> => {
      if (settled) return
      settled = true
      if (forceKillTimer) clearTimeout(forceKillTimer)
      context.abortSignal.removeEventListener('abort', abort)
      await cleanupLedger.complete(cleanupHandle)
      const scenarios = await readScenarioResults(context, spec.engine)
      resolveResult(
        result(spec.engine, startedAt, status, exitCode, signal, scenarios),
      )
    }
    const abort = (): void => {
      if (!child || child.exitCode !== null || child.signalCode !== null) return
      child.kill('SIGTERM')
      forceKillTimer = setTimeout(() => {
        if (child && child.exitCode === null && child.signalCode === null) {
          child.kill('SIGKILL')
        }
      }, 5_000)
      forceKillTimer.unref()
    }

    try {
      child = spawn(spec.command, [...spec.args], {
        cwd: context.repositoryRoot,
        env: {
          ...context.environment,
          ...spec.environment,
          INQTRIX_VERIFICATION_PROFILE: context.profile,
          INQTRIX_VERIFICATION_REPORT_DIR: context.reportDirectory,
          INQTRIX_VERIFICATION_RUN_ID: context.runId,
          INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH: resultsPath,
        },
        stdio: spec.productLifecycle
          ? ['ignore', 'pipe', 'pipe', 'ipc']
          : ['ignore', 'pipe', 'pipe'],
      })
      forwardRedactedOutput(
        child.stdout,
        spec.output?.stdout ?? process.stdout,
        createRedactor(context.environment),
      )
      forwardRedactedOutput(
        child.stderr,
        spec.output?.stderr ?? process.stderr,
        createRedactor(context.environment),
      )
    } catch {
      void settle('failed', null, null)
      return
    }

    if (productResources) {
      child.on('message', (message) => {
        void productResources.handle(child!, message)
      })
    }
    context.abortSignal.addEventListener('abort', abort, { once: true })
    child.once('error', () => {
      void settle('failed', null, null)
    })
    child.once('close', (exitCode, signal) => {
      const status = context.abortSignal.aborted
        ? 'interrupted'
        : exitCode === 0
          ? 'passed'
          : 'failed'
      void settle(status, exitCode, signal)
    })
  })
}

function result(
  engine: VerificationEngine,
  startedAt: Date,
  status: EngineResult['status'],
  exitCode: number | null,
  signal: NodeJS.Signals | null,
  scenarios: EngineResult['scenarios'],
): EngineResult {
  const finishedAt = new Date()
  return {
    durationMs: Math.max(0, finishedAt.getTime() - startedAt.getTime()),
    engine,
    exitCode,
    finishedAt: finishedAt.toISOString(),
    scenarios,
    signal,
    startedAt: startedAt.toISOString(),
    status,
  }
}

async function waitForExit(child: ChildProcess, timeoutMs: number): Promise<void> {
  if (child.exitCode !== null || child.signalCode !== null) return
  await Promise.race([
    new Promise<void>((resolve) => child.once('exit', () => resolve())),
    new Promise<void>((resolve) => setTimeout(resolve, timeoutMs)),
  ])
}

const MAX_CHILD_OUTPUT_LINE_BYTES = 64 * 1024
const MAX_CHILD_OUTPUT_BYTES = 2_000_000

function forwardRedactedOutput(
  source: NodeJS.ReadableStream | null,
  destination: NodeJS.WritableStream,
  redactor: Redactor,
): void {
  if (!source) return
  let buffered = ''
  let forwardedBytes = 0
  let outputSuppressed = false
  let oversizedLine = false
  const write = (value: string): void => {
    if (outputSuppressed) return
    const size = Buffer.byteLength(value, 'utf8')
    if (forwardedBytes + size > MAX_CHILD_OUTPUT_BYTES) {
      outputSuppressed = true
      destination.write(
        '[verification child output omitted: total output limit reached]\n',
      )
      return
    }
    forwardedBytes += size
    if (!destination.write(value)) {
      source.pause()
      destination.once('drain', () => source.resume())
    }
  }
  source.setEncoding('utf8')
  source.on('data', (chunk: string) => {
    buffered += chunk
    let newline = buffered.indexOf('\n')
    while (newline >= 0) {
      const line = buffered.slice(0, newline + 1)
      buffered = buffered.slice(newline + 1)
      if (oversizedLine || Buffer.byteLength(line, 'utf8') > MAX_CHILD_OUTPUT_LINE_BYTES) {
        write('[verification child output omitted: oversized line]\n')
      } else {
        const redacted = redactor.redactMessage(line)
        write(redacted.endsWith('\n') ? redacted : `${redacted}\n`)
      }
      oversizedLine = false
      newline = buffered.indexOf('\n')
    }
    if (Buffer.byteLength(buffered, 'utf8') > MAX_CHILD_OUTPUT_LINE_BYTES) {
      buffered = ''
      oversizedLine = true
    }
  })
  source.on('end', () => {
    if (oversizedLine) {
      write('[verification child output omitted: oversized line]\n')
    } else if (buffered) {
      write(redactor.redactMessage(buffered))
    }
  })
}
