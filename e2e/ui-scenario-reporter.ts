import type {
  FullResult,
  Reporter,
  TestCase,
  TestResult,
} from '@playwright/test/reporter'

import { writeScenarioResultFile } from './scenario-result-file.ts'
import {
  scenariosForProfile,
  uiScenarioForTestTitle,
} from '../tests/verification/scenario-inventory.ts'

export default class UiScenarioReporter implements Reporter {
  private readonly environment: NodeJS.ProcessEnv
  private readonly projects = ['chromium', 'firefox', 'webkit'] as const
  private readonly outcomes = new Map<string, Map<string, TestResult['status']>>()

  constructor(
    options: { environment?: NodeJS.ProcessEnv } = {},
  ) {
    // Playwright always passes the configured reporter options object as the
    // first constructor argument. Treating that argument as ProcessEnv drops
    // the real process environment when the config supplies no options, so
    // the scenario sidecar is never written even though every browser passes.
    this.environment = options.environment ?? process.env
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    const project = test.parent.project()?.name
    const scenario = uiScenarioForTestTitle(test.title)
    if (!project || !scenario) return
    const outcomes = this.outcomes.get(scenario.id) ?? new Map()
    outcomes.set(project, result.status)
    this.outcomes.set(scenario.id, outcomes)
  }

  async onEnd(
    _result: FullResult,
  ): Promise<{ status?: FullResult['status'] } | undefined> {
    const inventory = scenariosForProfile('ui-fixture')
    const evaluations = inventory.map((scenario) => {
      const outcomes = this.outcomes.get(scenario.id) ?? new Map()
      const expectedProjects = scenario.browsers ?? this.projects
      const passed = expectedProjects.length > 0
        && expectedProjects.every(
          (project) => outcomes.get(project) === 'passed',
        )
      return {
        id: scenario.id,
        attempted: outcomes.size > 0,
        status: passed ? 'passed' as const : 'failed' as const,
      }
    })
    const scenarios = evaluations
      .filter((scenario) => scenario.attempted)
      .map(({ attempted: _attempted, ...scenario }) => scenario)
    await writeScenarioResultFile(scenarios, this.environment)
    return evaluations.every((scenario) => scenario.status === 'passed')
      ? undefined
      : { status: 'failed' }
  }
}
