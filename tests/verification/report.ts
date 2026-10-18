import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import { chmod, rename, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import type {
  CleanupRecord,
  EngineResult,
  PreflightCheck,
  RunStatus,
  ScenarioReportRecord,
  VerificationReport,
} from './model.ts'
import { createRedactor, type Redactor } from './redaction.ts'
import type { RunContext } from './run-context.ts'
import {
  PROFILE_ENGINE_ORDER,
  SCENARIO_INVENTORY,
} from './scenario-inventory.ts'

const MODULE_DIRECTORY = dirname(fileURLToPath(import.meta.url))
const VERSION_SOURCE = resolve(
  MODULE_DIRECTORY,
  '../../src/inqtrix/__init__.py',
)

export class ReportWriter {
  readonly report: VerificationReport
  readonly redactor: Redactor
  private readonly context: RunContext

  constructor(context: RunContext) {
    this.context = context
    this.redactor = createRedactor(context.environment)
    this.report = {
      adapters: [],
      cleanup: { failed: 0, retained: 0, records: [], status: 'clean' },
      engines: [...PROFILE_ENGINE_ORDER[context.profile]],
      finishedAt: null,
      inqtrixVersion: readInqtrixVersion(),
      preflight: [],
      profile: context.profile,
      runId: context.runId,
      runtime: {
        arch: process.arch,
        node: process.version,
        platform: process.platform,
      },
      scenarios: SCENARIO_INVENTORY.map((scenario): ScenarioReportRecord => ({
        engine: scenario.engine,
        id: scenario.id,
        status: scenario.profiles.includes(context.profile)
          ? 'not_run'
          : 'not_applicable',
      })),
      schemaVersion: 4,
      sourceDirty: sourceDirty(context.repositoryRoot),
      sourceRevision: sourceRevision(context.repositoryRoot),
      startedAt: context.startedAt,
      status: 'created',
    }
  }

  async setPreflight(checks: readonly PreflightCheck[]): Promise<void> {
    this.report.preflight = this.redactor.redact([...checks])
    await this.flush()
  }

  async setStatus(status: RunStatus): Promise<void> {
    this.report.status = status
    await this.flush()
  }

  async addAdapterResult(result: EngineResult): Promise<void> {
    this.report.adapters.push(this.redactor.redact(result))
    this.applyScenarioResult(result)
    await this.flush()
  }

  async blockEngines(engines: readonly EngineResult['engine'][]): Promise<void> {
    const blocked = new Set(engines)
    for (const scenario of this.report.scenarios) {
      if (scenario.status === 'not_run' && blocked.has(scenario.engine)) {
        scenario.status = 'blocked'
      }
    }
    await this.flush()
  }

  async setCleanup(records: readonly CleanupRecord[]): Promise<void> {
    const sanitized = this.redactor.redact([...records])
    const failed = sanitized.filter((record) => record.status === 'failed').length
    const retained = sanitized.filter((record) => record.status === 'retained').length
    this.report.cleanup = {
      failed,
      retained,
      records: sanitized,
      status: failed > 0 ? 'failed' : retained > 0 ? 'retained' : 'clean',
    }
    await this.flush()
  }

  async finish(status: RunStatus): Promise<VerificationReport> {
    this.report.finishedAt = new Date().toISOString()
    this.report.status = status
    await this.flush()
    return this.redactor.redact(structuredClone(this.report))
  }

  async flush(): Promise<void> {
    const temporaryPath = resolve(this.context.reportDirectory, '.report.json.tmp')
    const payload = `${JSON.stringify(this.redactor.redact(this.report), null, 2)}\n`
    await writeFile(temporaryPath, payload, { encoding: 'utf8', mode: 0o600 })
    await rename(temporaryPath, this.context.reportPath)
    await chmod(this.context.reportPath, 0o600)
  }

  private applyScenarioResult(result: EngineResult): void {
    const selected = this.report.scenarios.filter((scenario) => (
      scenario.engine === result.engine && scenario.status === 'not_run'
    ))
    const explicit = new Map(
      (result.scenarios ?? []).map((scenario) => [scenario.id, scenario.status]),
    )
    for (const scenario of selected) {
      const status = explicit.get(scenario.id)
      if (status) scenario.status = status
    }
    const unresolved = selected.filter((scenario) => scenario.status === 'not_run')
    if (
      result.status !== 'passed' &&
      !selected.some((scenario) => scenario.status === 'failed')
      && unresolved.length > 0
    ) {
      const firstUnresolved = unresolved[0]
      if (firstUnresolved) firstUnresolved.status = 'failed'
    }
  }
}

export function readInqtrixVersion(sourcePath = VERSION_SOURCE): string {
  const source = readFileSync(sourcePath, 'utf8')
  const match = source.match(
    /^__version__\s*=\s*(['"])([^'"]+)\1\s*$/m,
  )
  const version = match?.[2]?.trim()
  if (!version) {
    throw new Error(`Unable to read the Inqtrix version from ${sourcePath}.`)
  }
  return version
}

function sourceRevision(repositoryRoot: string): string | null {
  const result = spawnSync('git', ['rev-parse', 'HEAD'], {
    cwd: repositoryRoot,
    encoding: 'utf8',
    stdio: ['ignore', 'pipe', 'ignore'],
  })
  const value = result.status === 0 ? result.stdout.trim() : ''
  return /^[0-9a-f]{40,64}$/i.test(value) ? value.toLowerCase() : null
}

function sourceDirty(repositoryRoot: string): boolean | null {
  const result = spawnSync(
    'git',
    ['status', '--porcelain', '--untracked-files=normal'],
    {
      cwd: repositoryRoot,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'ignore'],
    },
  )
  return result.status === 0 ? result.stdout.trim().length > 0 : null
}
