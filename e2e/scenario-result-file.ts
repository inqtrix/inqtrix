import { chmod, rename, writeFile } from 'node:fs/promises'

import type { ScenarioExecutionResult } from '../tests/verification/model.ts'

export async function writeScenarioResultFile(
  scenarios: readonly ScenarioExecutionResult[],
  environment: NodeJS.ProcessEnv = process.env,
): Promise<void> {
  const path = environment.INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH
  if (!path) return
  const temporaryPath = `${path}.tmp`
  const payload = `${JSON.stringify({
    scenarios,
    schemaVersion: 1,
  }, null, 2)}\n`
  await writeFile(temporaryPath, payload, { encoding: 'utf8', mode: 0o600 })
  await rename(temporaryPath, path)
  await chmod(path, 0o600)
}
