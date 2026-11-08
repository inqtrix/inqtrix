import { readFile, stat } from 'node:fs/promises'
import { resolve } from 'node:path'

import { scenarioResultProblem } from './fixtures/scenario-sidecar.mjs'
import type {
  ScenarioExecutionResult,
  VerificationEngine,
} from './model.ts'
import type { RunContext } from './run-context.ts'
import { scenariosForProfile } from './scenario-inventory.ts'

const MAX_RESULT_BYTES = 1_000_000

export function scenarioResultsPath(
  context: RunContext,
  engine: VerificationEngine,
): string {
  return resolve(context.reportDirectory, `${engine}-scenarios.json`)
}

/** What the reader made of an engine's result file. A discarded file keeps
 * every scenario of its engine at not_run; `problem` says why, so the report
 * and the summary can name it at the engine instead of leaving a red run
 * without a hint. */
export type ScenarioResultsRead = {
  problem: string | null
  scenarios: ScenarioExecutionResult[]
}

export async function readScenarioResults(
  context: RunContext,
  engine: VerificationEngine,
): Promise<ScenarioResultsRead> {
  const discarded = (problem: string): ScenarioResultsRead => ({ problem, scenarios: [] })
  const path = scenarioResultsPath(context, engine)
  let text: string
  try {
    const metadata = await stat(path)
    if (!metadata.isFile()) return discarded('the result file is not a regular file')
    if (metadata.size > MAX_RESULT_BYTES) {
      return discarded(`the result file is larger than ${MAX_RESULT_BYTES} bytes`)
    }
    text = await readFile(path, 'utf8')
  } catch {
    return discarded('the engine wrote no result file')
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch {
    return discarded('the result file is not valid JSON')
  }
  if (!isResultEnvelope(parsed)) {
    return discarded('the result file is no schemaVersion 1 envelope with a scenarios list')
  }
  const allowed = new Set(
    scenariosForProfile(context.profile)
      .filter((scenario) => scenario.engine === engine)
      .map((scenario) => scenario.id),
  )
  const seen = new Set<string>()
  const results: ScenarioExecutionResult[] = []
  // The writer's own rule decides an entry; a file with one entry that
  // breaks it, names a foreign scenario or repeats one counts as unreadable.
  for (const result of parsed.scenarios) {
    const problem = scenarioResultProblem(result)
    if (problem !== null) return discarded(problem)
    if (!allowed.has(result.id)) {
      return discarded(`${result.id} is no scenario of this engine in this profile`)
    }
    if (seen.has(result.id)) return discarded(`${result.id} is listed twice`)
    seen.add(result.id)
    results.push(
      result.reason === undefined
        ? { id: result.id, status: result.status }
        : { id: result.id, reason: result.reason, status: result.status },
    )
  }
  return { problem: null, scenarios: results }
}

function isResultEnvelope(value: unknown): value is {
  scenarios: ScenarioExecutionResult[]
  schemaVersion: 1
} {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Record<string, unknown>
  return candidate.schemaVersion === 1 && Array.isArray(candidate.scenarios)
}
