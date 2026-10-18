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
  startedAt: string
  status: EngineStatus
}

export type ScenarioExecutionResult = {
  id: string
  status: 'passed' | 'failed'
}

export type ScenarioReportRecord = {
  engine: VerificationEngine
  id: string
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
  preflight: PreflightCheck[]
  profile: VerificationProfile
  runId: string
  runtime: {
    arch: string
    node: string
    platform: NodeJS.Platform
  }
  scenarios: ScenarioReportRecord[]
  schemaVersion: 4
  sourceDirty: boolean | null
  sourceRevision: string | null
  startedAt: string
  status: RunStatus
}
