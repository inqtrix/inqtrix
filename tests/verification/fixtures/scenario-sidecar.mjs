import { chmod, rename, unlink, writeFile } from 'node:fs/promises'

import { ignoreMissing } from './cleanup-step.mjs'

// The one contract for the result file an engine hands the orchestrator
// (tests/verification/scenario-results.ts reads it with the same rule). A
// scenario is measured (passed, failed), not measured (not_run), or left out
// for a stated reason (skipped). A skip without its reason is refused here,
// at the engine, so a part that did not run can never count as covered.
export const SCENARIO_RESULT_STATUSES = Object.freeze([
  'passed',
  'failed',
  'not_run',
  'skipped',
])

// A reason is one line of a report. A longer one is cut, and the cut says how
// much it left out, so a reader never takes a part for the whole.
export const SCENARIO_REASON_LIMIT = 240

function boundedReason(text) {
  if (text.length <= SCENARIO_REASON_LIMIT) return text
  const left = text.length - SCENARIO_REASON_LIMIT
  return `${text.slice(0, SCENARIO_REASON_LIMIT)} [... ${left} more characters]`
}

export function scenarioResultProblem(entry) {
  if (!entry || typeof entry !== 'object') return 'an entry is not an object'
  if (typeof entry.id !== 'string' || entry.id.length === 0) return 'an entry has no id'
  if (!SCENARIO_RESULT_STATUSES.includes(entry.status)) {
    return `${entry.id} has the unknown status ${String(entry.status)}`
  }
  if (
    entry.reason !== undefined
    && (typeof entry.reason !== 'string' || entry.reason.trim() === '')
  ) {
    return `${entry.id} has an empty reason`
  }
  if (entry.status === 'skipped' && entry.reason === undefined) {
    return `${entry.id} is skipped without a reason`
  }
  return null
}

export async function writeScenarioSidecar(scenarios, environment = process.env) {
  const path = environment.INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH
  if (!path) return
  const seen = new Set()
  for (const entry of scenarios) {
    const problem = scenarioResultProblem(entry)
    if (problem) throw new Error(`Scenario results not written: ${problem}.`)
    if (seen.has(entry.id)) {
      throw new Error(`Scenario results not written: ${entry.id} appears twice.`)
    }
    seen.add(entry.id)
  }
  const payload = `${JSON.stringify({
    scenarios: scenarios.map(({ id, reason, status }) => (
      reason === undefined ? { id, status } : { id, reason, status }
    )),
    schemaVersion: 1,
  }, null, 2)}\n`
  const temporaryPath = `${path}.tmp`
  try {
    await writeFile(temporaryPath, payload, { encoding: 'utf8', mode: 0o600 })
    await rename(temporaryPath, path)
    await chmod(path, 0o600)
  } catch (error) {
    await unlink(temporaryPath).catch(ignoreMissing)
    throw error
  }
}

// The verdicts of one engine run, kept in the order of its scenarios. Every
// scenario starts not measured; a failed step fails it with the first
// reason given, and a later pass never replaces a failure. A pass may carry
// a note (a warning band it stayed inside, with target and measured value),
// which the report shows as the scenario's reason. The entries are what
// writeScenarioSidecar takes.
export function createScenarioBook(ids) {
  const statuses = new Map(ids.map((id) => [id, 'not_run']))
  const reasons = new Map()
  const known = (id) => {
    if (!statuses.has(id)) throw new Error(`Unknown scenario ${id}.`)
  }
  const note = (id, reason) => {
    if (reason !== undefined && reason !== null && !reasons.has(id)) {
      const text = String(reason).trim()
      if (text) reasons.set(id, boundedReason(text))
    }
  }
  return {
    pass(id, noteText) {
      known(id)
      if (statuses.get(id) === 'failed') return
      statuses.set(id, 'passed')
      note(id, noteText)
    },
    fail(id, reason) {
      known(id)
      // The first failure's reason stands; the note of an earlier pass does not.
      if (statuses.get(id) !== 'failed') reasons.delete(id)
      statuses.set(id, 'failed')
      note(id, reason)
    },
    skip(id, reason) {
      known(id)
      if (statuses.get(id) === 'failed') return
      if (!String(reason ?? '').trim()) throw new Error(`Skipping ${id} needs a reason.`)
      statuses.set(id, 'skipped')
      reasons.set(id, boundedReason(String(reason).trim()))
    },
    status(id) {
      known(id)
      return statuses.get(id)
    },
    allPassed() {
      return [...statuses.values()].every((status) => status === 'passed' || status === 'skipped')
    },
    entries() {
      return ids.map((id) => (reasons.has(id)
        ? { id, reason: reasons.get(id), status: statuses.get(id) }
        : { id, status: statuses.get(id) }))
    },
  }
}
