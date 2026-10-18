import type { VerificationAdapter } from './adapter.ts'
import { VERIFICATION_ADAPTERS } from './adapters/index.ts'
import { CleanupLedger } from './cleanup-ledger.ts'
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
  let executionStatus: RunStatus = blockedEngines.size > 0
    ? 'blocked'
    : 'passed'
  for (const adapter of runnableAdapters) {
    if (context.abortSignal.aborted) {
      executionStatus = 'interrupted'
      break
    }
    const result = requireExplicitPassedScenarios(
      context.profile,
      await executeAdapter(adapter, context, cleanupLedger),
    )
    await writer.addAdapterResult(result)
    if (result.status === 'passed') continue
    executionStatus = result.status === 'interrupted' ? 'interrupted' : 'failed'
    break
  }

  const cleanup = await cleanupLedger.cleanupAll()
  await writer.setCleanup(cleanup)
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
  const expected = scenariosForProfile(profile)
    .filter((scenario) => scenario.engine === result.engine)
    .map((scenario) => scenario.id)
  const passed = new Set(
    (result.scenarios ?? [])
      .filter((scenario) => scenario.status === 'passed')
      .map((scenario) => scenario.id),
  )
  if (expected.length === passed.size && expected.every((id) => passed.has(id))) {
    return result
  }
  return { ...result, status: 'failed' }
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
