export type ScenarioResultStatus = 'passed' | 'failed' | 'not_run' | 'skipped'

export type ScenarioResultEntry = {
  id: string
  reason?: string
  status: ScenarioResultStatus
}

export const SCENARIO_RESULT_STATUSES: readonly ScenarioResultStatus[]

export function scenarioResultProblem(entry: unknown): string | null

export function writeScenarioSidecar(
  scenarios: readonly ScenarioResultEntry[],
  environment?: Readonly<Record<string, string | undefined>>,
): Promise<void>

export type ScenarioBook = {
  pass(id: string): void
  fail(id: string, reason?: unknown): void
  skip(id: string, reason: string): void
  status(id: string): ScenarioResultStatus
  allPassed(): boolean
  entries(): ScenarioResultEntry[]
}

export function createScenarioBook(ids: readonly string[]): ScenarioBook
