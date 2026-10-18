import type {
  FullConfig,
  FullResult,
  Reporter,
  Suite,
  TestCase,
  TestResult,
} from '@playwright/test/reporter'

import {
  collaborationE2EConfiguration,
  type CollaborationTransport,
} from './config.ts'
import { writeScenarioResultFile } from './scenario-result-file.ts'
import {
  COLLABORATION_BROWSER_TARGETS,
  collaborationScenarioForTags,
  requiredPlaywrightTags,
  scenariosForProfile,
} from '../tests/verification/scenario-inventory.ts'
import type { VerificationProfile } from '../tests/verification/model.ts'

export default class CollaborationScenarioReporter implements Reporter {
  private preflightFailures: string[] = []
  private readonly environment: NodeJS.ProcessEnv
  private readonly outcomes = new Map<string, Map<string, TestResult['status']>>()
  private skippedTests: string[] = []
  private readonly profile: VerificationProfile
  private readonly transports: readonly CollaborationTransport[]

  constructor(
    options: {
      environment?: NodeJS.ProcessEnv
      profile?: VerificationProfile
      transports?: readonly CollaborationTransport[]
    } = {},
  ) {
    // Playwright constructs custom reporters with their configured options
    // object. Resolve defaults from the process only after receiving that
    // object; otherwise an injected empty object is mistaken for an
    // environment/profile and the result sidecar contract silently disappears.
    this.environment = options.environment ?? process.env
    this.profile = options.profile ?? selectedProfile(this.environment)
    this.transports = options.transports
      ?? collaborationE2EConfiguration.selectedTransports
  }

  onBegin(_config: FullConfig, suite: Suite): void {
    const testsByProject = new Map<string, TestCase[]>()
    for (const test of suite.allTests()) {
      const projectName = test.parent.project()?.name
      if (!projectName) continue
      const tests = testsByProject.get(projectName) ?? []
      tests.push(test)
      testsByProject.set(projectName, tests)
    }

    for (const transport of this.transports) {
      for (const target of COLLABORATION_BROWSER_TARGETS) {
        const projectName = projectId(transport, target)
        const tests = testsByProject.get(projectName) ?? []
        if (tests.length === 0) {
          this.preflightFailures.push(`${projectName} selected no tests`)
          continue
        }
        const required = requiredPlaywrightTags(
          this.profile,
          target.formFactor,
          target.browser,
        )
        for (const tag of required) {
          if (!tests.some((test) => test.tags.includes(tag))) {
            this.preflightFailures.push(`${projectName} is missing required scenario ${tag}`)
          }
        }
      }
    }
  }

  onTestEnd(test: TestCase, result: TestResult): void {
    const scenario = collaborationScenarioForTags(this.profile, test.tags)
    const projectName = test.parent.project()?.name ?? 'unknown-project'
    if (scenario) {
      const outcomes = this.outcomes.get(scenario.id) ?? new Map()
      outcomes.set(projectName, result.status)
      this.outcomes.set(scenario.id, outcomes)
    }
    if (result.status !== 'skipped') return
    this.skippedTests.push(`${projectName}: ${test.title}`)
  }

  async onEnd(
    result: FullResult,
  ): Promise<{ status?: FullResult['status'] } | undefined> {
    const failures = [
      ...this.preflightFailures,
      ...this.skippedTests.map((test) => `required test skipped: ${test}`),
    ]
    const evaluations = scenariosForProfile(this.profile)
      .filter((scenario) => scenario.engine === 'collaboration-playwright')
      .map((scenario) => {
        const expectedProjects = this.transports.flatMap((transport) => (
          COLLABORATION_BROWSER_TARGETS
            .filter((target) => (
              !scenario.formFactors
              || scenario.formFactors.includes(target.formFactor)
            ))
            .filter((target) => (
              !scenario.browsers
              || scenario.browsers.includes(target.browser)
            ))
            .map((target) => projectId(transport, target))
        ))
        const outcomes = this.outcomes.get(scenario.id) ?? new Map()
        const passed = expectedProjects.length > 0
          && expectedProjects.every((project) => outcomes.get(project) === 'passed')
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
    if (
      failures.length === 0
      && evaluations.every((scenario) => scenario.status === 'passed')
    ) {
      return undefined
    }
    const allFailures = failures.length > 0
      ? failures
      : ['one or more required scenario/browser executions did not pass']
    process.stderr.write(
      `Collaboration ${this.profile} run failed:\n- ${allFailures.join('\n- ')}\n`,
    )
    if (result.status === 'passed') return { status: 'failed' }
    return undefined
  }
}

function projectId(
  transport: CollaborationTransport,
  target: typeof COLLABORATION_BROWSER_TARGETS[number],
): string {
  return `${transport}-${target.browser}-${target.formFactor}`
}

function selectedProfile(environment: NodeJS.ProcessEnv): VerificationProfile {
  const profile = environment.INQTRIX_VERIFICATION_PROFILE
  if (profile === 'system-smoke' || profile === 'fault-injection') return profile
  throw new Error(
    'Scenario reporter requires system-smoke or fault-injection profile.',
  )
}
