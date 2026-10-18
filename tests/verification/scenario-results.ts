import { readFile, stat } from 'node:fs/promises'
import { resolve } from 'node:path'

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

export async function readScenarioResults(
  context: RunContext,
  engine: VerificationEngine,
): Promise<ScenarioExecutionResult[]> {
  const path = scenarioResultsPath(context, engine)
  try {
    const metadata = await stat(path)
    if (!metadata.isFile() || metadata.size > MAX_RESULT_BYTES) return []
    const parsed = JSON.parse(await readFile(path, 'utf8')) as unknown
    if (!isResultEnvelope(parsed)) return []
    const allowed = new Set(
      scenariosForProfile(context.profile)
        .filter((scenario) => scenario.engine === engine)
        .map((scenario) => scenario.id),
    )
    const seen = new Set<string>()
    const results: ScenarioExecutionResult[] = []
    for (const result of parsed.scenarios) {
      if (
        !allowed.has(result.id)
        || seen.has(result.id)
        || (result.status !== 'passed' && result.status !== 'failed')
      ) {
        return []
      }
      seen.add(result.id)
      results.push({ id: result.id, status: result.status })
    }
    return results
  } catch {
    return []
  }
}

function isResultEnvelope(value: unknown): value is {
  scenarios: Array<{ id: string; status: 'passed' | 'failed' }>
  schemaVersion: 1
} {
  if (!value || typeof value !== 'object') return false
  const candidate = value as Record<string, unknown>
  return candidate.schemaVersion === 1 && Array.isArray(candidate.scenarios)
    && candidate.scenarios.every((entry) => (
      entry
      && typeof entry === 'object'
      && typeof (entry as Record<string, unknown>).id === 'string'
      && typeof (entry as Record<string, unknown>).status === 'string'
    ))
}
