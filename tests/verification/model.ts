import type { ScenarioResultEntry } from './fixtures/scenario-sidecar.mjs'

export const VERIFICATION_PROFILES = [
  'ui-fixture',
  'owner-setup',
  'system-smoke',
  'agent-desk',
  'agent-recovery',
  'chat-prompt',
  'fault-injection',
  'load-smoke',
  'load-soak',
  'load-ramp',
  'load-capacity',
  'run-load',
  'edge-conformance',
] as const

export const VERIFICATION_ENGINES = [
  'ui-fixture-playwright',
  'owner-setup-live',
  'collaboration-playwright',
  'editor-system-live',
  'agent-desk-live',
  'chat-prompt-live',
  'collaboration-load',
  'run-load',
  'web-edge-containers',
] as const

export const RUN_STATUSES = [
  'created',
  'preflight_passed',
  'blocked',
  'running',
  'passed',
  'failed',
  'cleanup_failed',
  'interrupted',
] as const

export const ENGINE_STATUSES = [
  'passed',
  'failed',
  'interrupted',
] as const

export const SCENARIO_STATUSES = [
  'passed',
  'failed',
  'skipped',
  'blocked',
  'not_applicable',
  'not_run',
] as const

export type VerificationProfile = typeof VERIFICATION_PROFILES[number]
export type VerificationEngine = typeof VERIFICATION_ENGINES[number]
export type VerificationBrowser = 'chromium' | 'firefox' | 'webkit'
export type ContainerEngine = 'docker' | 'podman'
export type RunStatus = typeof RUN_STATUSES[number]
export type EngineStatus = typeof ENGINE_STATUSES[number]
export type ScenarioStatus = typeof SCENARIO_STATUSES[number]
export type PreflightStatus = 'passed' | 'failed'
export type CleanupStatus = 'registered' | 'running' | 'cleaned' | 'retained' | 'failed'
export type CleanupDisposition = void | 'retained'

export type PreflightCheck = {
  engine: VerificationEngine
  id: string
  message: string
  status: PreflightStatus
}

export type EngineResult = {
  durationMs: number
  engine: VerificationEngine
  exitCode: number | null
  finishedAt: string
  signal: NodeJS.Signals | null
  scenarios?: ScenarioExecutionResult[]
  /** Why the engine's result file was discarded, when it was: its scenarios
   * then stay not_run, and this names the cause at the engine. */
  scenarioResultsProblem?: string
  /** Why an engine that exited 0 counts as failed: the scenarios of its
   * inventory it did not pass, each with its status and reason. */
  scenarioGap?: string
  startedAt: string
  status: EngineStatus
}

// What an engine reports per scenario: measured (passed, failed), not
// measured (not_run), or left out for a stated reason (skipped).
export type ScenarioExecutionResult = ScenarioResultEntry

export type ScenarioReportRecord = {
  engine: VerificationEngine
  id: string
  reason?: string
  status: ScenarioStatus
}

export type CleanupRecord = {
  completedAt: string | null
  id: string
  kind: 'process' | 'resource'
  label: string
  registeredAt: string
  status: CleanupStatus
}

export type ServiceLogCounters = {
  errors: number
  firstLineAt: string | null
  levels: Record<string, number>
  lines: number
  missingFields: Record<string, number>
  notable: { count: number; level: string; logger: string; message: string }[]
  notableDropped: number
  notJson: number
}

export type LogInspection = {
  reason?: string
  runId?: string
  services?: Record<string, ServiceLogCounters>
  since?: string
  status: 'passed' | 'failed' | 'not_run'
}

export type VerificationReport = {
  adapters: EngineResult[]
  cleanup: {
    failed: number
    retained: number
    records: CleanupRecord[]
    status: 'clean' | 'retained' | 'failed'
  }
  engines: VerificationEngine[]
  finishedAt: string | null
  inqtrixVersion: string
  logInspection: LogInspection
  preflight: PreflightCheck[]
  profile: VerificationProfile
  runId: string
  runtime: {
    arch: string
    node: string
    platform: NodeJS.Platform
  }
  scenarios: ScenarioReportRecord[]
  schemaVersion: 6
  sourceDirty: boolean | null
  sourceRevision: string | null
  startedAt: string
  status: RunStatus
}
