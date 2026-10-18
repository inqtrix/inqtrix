import type { CleanupLedger } from './cleanup-ledger.ts'
import type {
  EngineResult,
  PreflightCheck,
  VerificationEngine,
  VerificationProfile,
} from './model.ts'
import type { RunContext } from './run-context.ts'

export type VerificationAdapter = {
  engine: VerificationEngine
  execute(
    context: RunContext,
    cleanupLedger: CleanupLedger,
  ): Promise<EngineResult>
  preflight(context: RunContext): Promise<PreflightCheck[]>
  profiles: readonly VerificationProfile[]
}
