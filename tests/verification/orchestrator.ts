import type { VerificationAdapter } from './adapter.ts'
import { VERIFICATION_ADAPTERS } from './adapters/index.ts'
import { CleanupLedger } from './cleanup-ledger.ts'
import {
  inspectRunLogs,
  logInspectionIsMandatory,
  type LogSource,
} from './log-inspection.ts'
import type {
  EngineResult,
  RunStatus,
  VerificationEngine,
  VerificationReport,
} from './model.ts'
import { ReportWriter } from './report.ts'
import {
  createRunContext,
  type RunContextOptions,
} from './run-context.ts'
import {
  PROFILE_ENGINE_ORDER,
  scenariosForProfile,
} from './scenario-inventory.ts'

export type RunVerificationOptions = RunContextOptions

export async function runVerification(
  options: RunVerificationOptions,
  adapters: readonly VerificationAdapter[] = VERIFICATION_ADAPTERS,
  logSource?: LogSource,
): Promise<VerificationReport> {
  const context = await createRunContext(options)
  const selectedAdapters = selectAdapters(context.profile, adapters)
  const writer = new ReportWriter(context)
  const cleanupLedger = new CleanupLedger(context.reportDirectory, writer.redactor)
  const preflight = (
    await Promise.all(selectedAdapters.map(async (adapter) => {
      try {
        return await adapter.preflight(context)
      } catch {
        return [{
          engine: adapter.engine,
          id: 'adapter-preflight',
          message: 'The adapter preflight failed unexpectedly.',
          status: 'failed' as const,
        }]
      }
    }))
  ).flat()
  await writer.setPreflight(preflight)
  const blockedEngines = new Set(
    preflight
      .filter((check) => check.status === 'failed')
      .map((check) => check.engine),
  )
  if (blockedEngines.size > 0) {
    await writer.blockEngines([...blockedEngines])
  }

  if (context.abortSignal.aborted) {
    const cleanup = await cleanupLedger.cleanupAll()
    await writer.setCleanup(cleanup)
    return await writer.finish(
      cleanup.some((record) => record.status === 'failed')
        ? 'cleanup_failed'
        : 'interrupted',
    )
  }

  if (context.preflightOnly) {
    const cleanup = await cleanupLedger.cleanupAll()
    await writer.setCleanup(cleanup)
    return await writer.finish(
      cleanup.some((record) => record.status === 'failed')
        ? 'cleanup_failed'
        : blockedEngines.size > 0
          ? 'blocked'
          : 'preflight_passed',
    )
  }

  const runnableAdapters = selectedAdapters.filter(
    (adapter) => !blockedEngines.has(adapter.engine),
  )
  if (runnableAdapters.length === 0) {
    const cleanup = await cleanupLedger.cleanupAll()
    await writer.setCleanup(cleanup)
    return await writer.finish(
      cleanup.some((record) => record.status === 'failed')
        ? 'cleanup_failed'
        : 'blocked',
    )
  }

  await writer.setStatus('running')
  let failed = false
  let interrupted = false
  for (const [index, adapter] of runnableAdapters.entries()) {
    // The adapters of one run reuse run-scoped names, and a red adapter
    // leaves its open entries to this ledger: the next one starts only after
    // they are cleaned, and a failed cleanup ends the run as cleanup_failed.
    if (index > 0) {
      const cleanup = await cleanupLedger.cleanupAll()
      if (cleanup.some((record) => record.status === 'failed')) break
    }
    if (context.abortSignal.aborted) {
      interrupted = true
      break
    }
    const result = requireExplicitPassedScenarios(
      context.profile,
      await executeAdapter(adapter, context, cleanupLedger),
    )
    await writer.addAdapterResult(result)
    if (result.status === 'interrupted') {
      interrupted = true
      break
    }
    // A red adapter does not hide the ones after it.
    if (result.status !== 'passed') failed = true
  }

  const cleanup = await cleanupLedger.cleanupAll()
  await writer.setCleanup(cleanup)
  // Last, so that the window also holds what cleaning up logged.
  const logInspection = await inspectRunLogs(context, logSource)
  await writer.setLogInspection(logInspection)
  // A red adapter stays the verdict even when the run is interrupted later.
  const executionStatus: RunStatus = failed || logInspection.status === 'failed'
    ? 'failed'
    : interrupted
      ? 'interrupted'
      : blockedEngines.size > 0
          || (logInspection.status === 'not_run' && logInspectionIsMandatory(context))
        ? 'blocked'
        : 'passed'
  const status = cleanup.some((record) => record.status === 'failed')
    ? 'cleanup_failed'
    : executionStatus
  return await writer.finish(status)
}

function requireExplicitPassedScenarios(
  profile: RunVerificationOptions['profile'],
  result: EngineResult,
): EngineResult {
  if (result.status !== 'passed') return result
  const inventory = scenariosForProfile(profile)
    .filter((scenario) => scenario.engine === result.engine)
  const expected = inventory.map((scenario) => scenario.id)
  const skippable = new Set(
    inventory.filter((scenario) => scenario.mayBeSkipped).map((scenario) => scenario.id),
  )
  // A skip carries its reason (the reader refuses one without) and shows in
  // the report, but counts only for a scenario the inventory lets skip;
  // not_run means unmeasured and keeps a passing engine red.
  const passed = new Set(
    (result.scenarios ?? [])
      .filter((scenario) => scenario.status === 'passed'
        || (scenario.status === 'skipped' && skippable.has(scenario.id)))
      .map((scenario) => scenario.id),
  )
  if (expected.length === passed.size && expected.every((id) => passed.has(id))) {
    return result
  }
  // The engine exited 0, so nothing else says why it is red.
  const reported = new Map((result.scenarios ?? []).map((scenario) => [scenario.id, scenario]))
  const gap = expected
    .filter((id) => !passed.has(id))
    .map((id) => {
      const scenario = reported.get(id)
      if (!scenario) return `${id} not reported`
      const status = scenario.status === 'skipped' ? 'skipped, which it may not be' : scenario.status
      return scenario.reason ? `${id} ${status}: ${scenario.reason}` : `${id} ${status}`
    })
  return { ...result, scenarioGap: gap.join('; '), status: 'failed' }
}

export function selectAdapters(
  profile: RunVerificationOptions['profile'],
  adapters: readonly VerificationAdapter[] = VERIFICATION_ADAPTERS,
): VerificationAdapter[] {
  const byEngine = new Map<VerificationEngine, VerificationAdapter>()
  for (const adapter of adapters) {
    if (byEngine.has(adapter.engine)) {
      throw new Error(`Duplicate verification adapter: ${adapter.engine}`)
    }
    byEngine.set(adapter.engine, adapter)
  }
  return PROFILE_ENGINE_ORDER[profile].map((engine) => {
    const adapter = byEngine.get(engine)
    if (!adapter || !adapter.profiles.includes(profile)) {
      throw new Error(`Missing verification adapter ${engine} for profile ${profile}.`)
    }
    return adapter
  })
}

async function executeAdapter(
  adapter: VerificationAdapter,
  context: Awaited<ReturnType<typeof createRunContext>>,
  cleanupLedger: CleanupLedger,
): Promise<EngineResult> {
  const startedAt = new Date()
  try {
    return await adapter.execute(context, cleanupLedger)
  } catch {
    const finishedAt = new Date()
    return {
      durationMs: Math.max(0, finishedAt.getTime() - startedAt.getTime()),
      engine: adapter.engine,
      exitCode: null,
      finishedAt: finishedAt.toISOString(),
      signal: null,
      startedAt: startedAt.toISOString(),
      status: context.abortSignal.aborted ? 'interrupted' : 'failed',
    }
  }
}
