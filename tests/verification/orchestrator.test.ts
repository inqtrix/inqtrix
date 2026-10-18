import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  statSync,
  writeFileSync,
} from 'node:fs'
import { tmpdir } from 'node:os'
import { delimiter, join } from 'node:path'
import { Writable } from 'node:stream'
import { describe, test } from 'node:test'

import type { VerificationAdapter } from './adapter.ts'
import {
  clockSkewOutsideWindowSeconds,
  collaborationLoadAdapter,
} from './adapters/collaboration-load.ts'
import { collaborationPlaywrightAdapter } from './adapters/collaboration-playwright.ts'
import { editorSystemLiveAdapter } from './adapters/editor-system-live.ts'
import { ownerSetupLiveAdapter } from './adapters/owner-setup-live.ts'
import { playwrightPreflight } from './adapters/shared.ts'
import { CleanupLedger } from './cleanup-ledger.ts'
import { parseCliArguments } from './cli.ts'
import { runCommand } from './command-runner.ts'
import {
  VERIFICATION_ENGINES,
  VERIFICATION_PROFILES,
} from './model.ts'
import {
  runVerification,
  selectAdapters,
} from './orchestrator.ts'
import { createRedactor } from './redaction.ts'
import { readInqtrixVersion, ReportWriter } from './report.ts'
import {
  createRunId,
  createRunContext,
  validateRunId,
} from './run-context.ts'
import {
  COLLABORATION_BROWSER_TARGETS,
  PROFILE_ENGINE_ORDER,
  SCENARIO_INVENTORY,
  requiredPlaywrightTags,
  scenariosForProfile,
} from './scenario-inventory.ts'
import {
  documentBelongsToRun,
  temporaryUserBelongsToRun,
  temporaryUserDescriptors,
} from './fixtures/run-scope.mjs'
import {
  buildLoadDocumentSeed,
  buildLoadSoakFixture,
  buildLoadSmokeFixture,
  LOAD_SOAK_COMMENTERS,
  LOAD_SOAK_CONNECTIONS,
  LOAD_SOAK_FEATURE_ACTORS,
  LOAD_SOAK_READERS,
  LOAD_SOAK_WRITERS,
  LOAD_SMOKE_CONNECTIONS,
  writePrivateLoadSmokeFixture,
} from './fixtures/load-smoke.mjs'
import {
  buildLargeCollaborationDocumentSeed,
  LARGE_COLLABORATION_DOCUMENT_PARAGRAPHS,
} from './fixtures/collaboration-document-state.mjs'
import {
  buildGeneratedSystemSmokeFixture,
  fixtureIsInsidePrivateRunDirectory,
  writeGeneratedSystemSmokeFixture,
} from './fixtures/collaboration-system-smoke.mjs'
import {
  buildGeneratedFaultInjectionFixture,
  GENERATED_FAULT_INJECTION_PRIVATE_ANCHOR_TARGETS,
  writeGeneratedFaultInjectionFixture,
} from './fixtures/collaboration-fault-injection.mjs'
import {
  cleanupOwnedProjectDocuments,
} from './fixtures/project-documents.mjs'
import {
  eraseTemporaryAccount,
  ProductResourceController,
  retainsReviewResource,
  type CleanupActor,
  type ProductResource,
} from './fixtures/product-resource.ts'

const LIFECYCLE_CLIENT_URL = new URL(
  './fixtures/lifecycle-client.mjs',
  import.meta.url,
).href

describe('verification inventory', () => {
  test('keeps mutating private-anchor fixtures aligned with the browser matrix', () => {
    assert.deepEqual(
      COLLABORATION_BROWSER_TARGETS.map(
        ({ browser, formFactor }) => `${browser}-${formFactor}`,
      ),
      [...GENERATED_FAULT_INJECTION_PRIVATE_ANCHOR_TARGETS],
    )
  })

  test('defines twelve profiles over exactly eight reusable engines', () => {
    assert.deepEqual(VERIFICATION_PROFILES, [
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
    ])
    assert.deepEqual(VERIFICATION_ENGINES, [
      'ui-fixture-playwright',
      'owner-setup-live',
      'collaboration-playwright',
      'editor-system-live',
      'agent-desk-live',
      'chat-prompt-live',
      'collaboration-load',
      'web-edge-containers',
    ])
    for (const profile of VERIFICATION_PROFILES) {
      assert(scenariosForProfile(profile).length > 0)
      for (const engine of PROFILE_ENGINE_ORDER[profile]) {
        assert(
          scenariosForProfile(profile).some((scenario) => scenario.engine === engine),
          `${profile} must inventory ${engine}`,
        )
      }
    }
  })

  test('accepts only Run-ID-bound chat-thread and prompt-template cleanup registrations', async () => {
    const reportDirectory = mkdtempSync(join(tmpdir(), 'inqv-resource-'))
    try {
      const runId = createRunId(new Date('2026-08-06T00:00:00Z'))
      const redactor = createRedactor({})
      const ledger = new CleanupLedger(reportDirectory, redactor)
      const context = {
        environment: {},
        reportDirectory,
        runId,
      } as unknown as ConstructorParameters<typeof ProductResourceController>[0]
      const cleaned: string[] = []
      const controller = new ProductResourceController(
        context,
        ledger,
        async (_context, resource) => {
          cleaned.push(`${resource.kind}:${resource.id}`)
        },
      )
      const replies: unknown[] = []
      const child = {
        send(message: unknown) {
          replies.push(message)
          return true
        },
      }
      const register = (resource: Record<string, unknown>, requestId: string) =>
        controller.handle(child as never, {
          protocol: 'inqtrix-verification-resource-v1',
          requestId,
          resource,
          runId,
          type: 'register',
        })
      await register({
        credential: 'user',
        id: 'ct_r09_thread',
        kind: 'chat_thread',
        ownerEmail: 'tester@example.invalid',
        title: `${runId} Chat-Browsermatrix Frage`,
      }, 'req-1')
      await register({
        credential: 'user',
        id: 'pt_r09_template',
        kind: 'prompt_template',
        ownerEmail: 'tester@example.invalid',
        title: `${runId} Prompt-Browsermatrix`,
      }, 'req-2')
      // Unbound titles must be rejected — cleanup may never reach
      // resources of a foreign run.
      await register({
        credential: 'user',
        id: 'ct_foreign',
        kind: 'chat_thread',
        ownerEmail: 'tester@example.invalid',
        title: 'Fremder Chat ohne Run-Bindung',
      }, 'req-3')
      await register({
        credential: 'user',
        id: 'pt_foreign',
        kind: 'prompt_template',
        ownerEmail: 'tester@example.invalid',
        title: 'Fremder Prompt ohne Run-Bindung',
      }, 'req-4')
      const byRequest = new Map(
        replies.map((reply) => [
          (reply as { requestId: string }).requestId,
          (reply as { type: string }).type,
        ]),
      )
      assert.equal(byRequest.get('req-1'), 'ack')
      assert.equal(byRequest.get('req-2'), 'ack')
      assert.equal(byRequest.get('req-3'), 'error')
      assert.equal(byRequest.get('req-4'), 'error')
      const records = await ledger.cleanupAll()
      assert.equal(records.length, 2)
      assert.deepEqual(cleaned.sort(), [
        'chat_thread:ct_r09_thread',
        'prompt_template:pt_r09_template',
      ])
    } finally {
      rmSync(reportDirectory, { force: true, recursive: true })
    }
  })

  test('keeps system and fault Playwright selections disjoint', () => {
    const system = new Set(requiredPlaywrightTags('system-smoke', 'desktop'))
    const faults = new Set(requiredPlaywrightTags('fault-injection', 'desktop'))
    assert(system.size > 0)
    assert(faults.size > 0)
    assert(system.has('@detached-transfer'))
    assert(!faults.has('@detached-transfer'))
    assert.deepEqual([...system].filter((tag) => faults.has(tag)), [])
  })

  test('maps every UI title and collaboration tag to exactly one inventory target', () => {
    const uiSource = [
      '../../apps/research-desk/browser-tests/accessibilityDemo.spec.ts',
      '../../apps/research-desk/browser-tests/brandTheme.spec.ts',
      '../../apps/research-desk/browser-tests/editorBodyChanges.spec.ts',
      '../../apps/research-desk/browser-tests/editorCollaborationAiPublication.spec.ts',
      '../../apps/research-desk/browser-tests/editorCollaborationEditingSuggestions.spec.ts',
      '../../apps/research-desk/browser-tests/editorCollaborationEmptyLine.spec.ts',
      '../../apps/research-desk/browser-tests/editorCollaborationFormatting.spec.ts',
      '../../apps/research-desk/browser-tests/editorCollaborationLifecycle.spec.ts',
      '../../apps/research-desk/browser-tests/editorCollaborationPresence.spec.ts',
      '../../apps/research-desk/browser-tests/editorCollaborationReconnect.spec.ts',
      '../../apps/research-desk/browser-tests/editorCollaborationRejection.spec.ts',
      '../../apps/research-desk/browser-tests/editorCollaborationSending.spec.ts',
      '../../apps/research-desk/browser-tests/editorCollaborationSuggest.spec.ts',
      '../../apps/research-desk/browser-tests/editorCollaborationTables.spec.ts',
      '../../apps/research-desk/browser-tests/fileLibraryResponsive.spec.ts',
      '../../apps/research-desk/browser-tests/markdownRendering.spec.ts',
    ].map((path) => readFileSync(new URL(path, import.meta.url), 'utf8')).join('\n')
    const collaborationSource = readFileSync(
      new URL(
        '../../e2e/scenarios/collaboration.system.spec.ts',
        import.meta.url,
      ),
      'utf8',
    )
    const uiTitles = testTitles(uiSource)
    const collaborationTitles = testTitles(collaborationSource)
    const uiInventory = SCENARIO_INVENTORY.filter(
      (scenario) => scenario.engine === 'ui-fixture-playwright',
    )
    const collaborationInventory = SCENARIO_INVENTORY.filter(
      (scenario) => scenario.engine === 'collaboration-playwright',
    )
    assert.deepEqual(
      new Set(uiInventory.map((scenario) => scenario.testTitle)),
      new Set(uiTitles),
    )
    for (const scenario of collaborationInventory) {
      assert.equal(
        collaborationTitles.filter((title) => (
          title.split(/\s+/).includes(scenario.selectorTag ?? '')
        )).length,
        1,
        `${scenario.id} must map to exactly one Playwright test`,
      )
    }
    for (const title of collaborationTitles) {
      const tags = title.split(/\s+/).filter((part) => part.startsWith('@'))
      assert.equal(
        collaborationInventory.filter((scenario) => (
          scenario.selectorTag && tags.includes(scenario.selectorTag)
        )).length,
        1,
        `${title} must map back to exactly one inventory scenario`,
      )
    }
  })

  test('exposes only the profile orchestrator and current engine entrypoints', () => {
    const packageJson = JSON.parse(
      readFileSync(new URL('../../package.json', import.meta.url), 'utf8'),
    ) as { scripts?: Record<string, string> }
    assert.deepEqual(
      Object.keys(packageJson.scripts ?? {}).filter(
        (name) => /(?:release|enterprise|^e2e(?::|$))/.test(name),
      ),
      [],
    )
    for (const path of [
      '../../e2e/collaboration.spec.ts',
      '../../e2e/enterprise-editor-live.mjs',
      '../../e2e/release-command.ts',
      '../../e2e/release-contract.ts',
      '../../e2e/release-reporter.ts',
    ]) {
      assert.equal(existsSync(new URL(path, import.meta.url)), false, path)
    }
  })

  test('binds generated documents and temporary users to exactly one Run ID', () => {
    const firstRun = 'inqv-scope-first-0001'
    const secondRun = 'inqv-scope-second-0002'
    const firstUsers = temporaryUserDescriptors(firstRun)
    const secondUsers = temporaryUserDescriptors(secondRun)
    assert.equal(firstUsers.length, 4)
    assert.equal(secondUsers.length, 4)
    assert.deepEqual(
      new Set([...firstUsers, ...secondUsers].map((user) => user.email)).size,
      8,
    )
    assert(documentBelongsToRun(`ed_${firstRun}_document`, firstRun))
    assert(!documentBelongsToRun(`ed_${firstRun}_document`, secondRun))
    const soakUsers = temporaryUserDescriptors(firstRun, 24)
    assert.equal(soakUsers.length, 24)
    assert.equal(new Set(soakUsers.map((user) => user.email)).size, 24)
    assert(soakUsers.every((user) => temporaryUserBelongsToRun(user.email, firstRun)))
    assert(soakUsers.every((user) => !temporaryUserBelongsToRun(user.email, secondRun)))
  })

  test('requires explicit seed-account identities and both passwords', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-live-preflight-'))
    try {
      const context = await createRunContext({
        environment: {},
        profile: 'system-smoke',
        repositoryRoot,
        runId: 'inqv-live-preflight-01',
      })
      const checks = await editorSystemLiveAdapter.preflight(context)
      for (const id of [
        'admin-email',
        'tester-email',
        'admin-password',
        'user-password',
        'seed-account-separation',
      ]) {
        assert.equal(
          checks.find((check) => check.id === id)?.status,
          'failed',
          `${id} must be an explicit failed preflight without an environment value`,
        )
      }
      const configured = await createRunContext({
        environment: {
          INQTRIX_E2E_ADMIN_EMAIL: 'owner@example.invalid',
          INQTRIX_E2E_ADMIN_PASSWORD: 'explicit-admin-password',
          INQTRIX_E2E_TESTER_EMAIL: 'collaborator@example.invalid',
          INQTRIX_E2E_USER_PASSWORD: 'explicit-user-password',
        },
        profile: 'system-smoke',
        repositoryRoot,
        runId: 'inqv-live-preflight-02',
      })
      const configuredChecks = await editorSystemLiveAdapter.preflight(configured)
      for (const id of [
        'admin-email',
        'tester-email',
        'admin-password',
        'user-password',
        'seed-account-separation',
      ]) {
        assert.equal(
          configuredChecks.find((check) => check.id === id)?.status,
          'passed',
        )
      }
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('requires a fresh stack, one browser, and distinct owner-setup accounts', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-owner-setup-'))
    try {
      mkdirSync(join(repositoryRoot, 'e2e', 'engines'), { recursive: true })
      writeFileSync(
        join(repositoryRoot, 'e2e', 'engines', 'owner-setup-live.mjs'),
        '',
      )
      const context = await createRunContext({
        browserTarget: 'chromium',
        environment: {
          INQTRIX_E2E_ADMIN_EMAIL: 'owner@example.invalid',
          INQTRIX_E2E_ADMIN_PASSWORD: 'explicit-admin-password',
          INQTRIX_E2E_BASE_URL: 'http://example.invalid',
          INQTRIX_E2E_TESTER_EMAIL: 'user@example.invalid',
          INQTRIX_E2E_USER_PASSWORD: 'explicit-user-password',
        },
        profile: 'owner-setup',
        repositoryRoot,
        runId: 'inqv-owner-setup-preflight-01',
      })
      const checks = await ownerSetupLiveAdapter.preflight(context)
      for (const id of [
        'browser-target',
        'admin-email',
        'tester-email',
        'admin-password',
        'user-password',
        'account-separation',
      ]) {
        assert.equal(
          checks.find((check) => check.id === id)?.status,
          'passed',
          `${id} must pass when explicitly configured`,
        )
      }
      assert.equal(
        checks.find((check) => check.id === 'base-url')?.status,
        'failed',
      )
      assert.equal(
        checks.find((check) => check.id === 'fresh-owner-contract')?.status,
        'failed',
      )
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('accepts an explicit executable without requiring a managed browser', () => {
    assert.deepEqual(
      playwrightPreflight('ui-fixture-playwright', [])
        .map((check) => check.id),
      ['playwright-cli'],
    )
    assert(
      playwrightPreflight('ui-fixture-playwright', ['chromium'])
        .some((check) => check.id === 'playwright-browser-chromium'),
    )
  })

  test('keeps an external private fixture mandatory for load-capacity', async () => {
    if (process.platform === 'win32') return
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-load-mode-'))
    const fixture = join(repositoryRoot, 'load-fixture.json')
    try {
      writeFileSync(fixture, '{}', { mode: 0o640 })
      const unsafe = await createRunContext({
        fixturePath: fixture,
        profile: 'load-capacity',
        repositoryRoot,
        runId: 'inqv-load-mode-unsafe-01',
      })
      assert.equal(
        (await collaborationLoadAdapter.preflight(unsafe))
          .find((check) => check.id === 'session-fixture')?.status,
        'failed',
      )

      chmodSync(fixture, 0o600)
      const safe = await createRunContext({
        fixturePath: fixture,
        profile: 'load-capacity',
        repositoryRoot,
        runId: 'inqv-load-mode-safe-0001',
      })
      assert.equal(
        (await collaborationLoadAdapter.preflight(safe))
          .find((check) => check.id === 'session-fixture')?.status,
        'passed',
      )
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('load-smoke preflight requires provisioning credentials, not a fixture', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-load-smoke-'))
    try {
      const missing = await createRunContext({
        environment: {},
        profile: 'load-smoke',
        repositoryRoot,
        runId: 'inqv-load-smoke-missing-01',
      })
      const missingChecks = await collaborationLoadAdapter.preflight(missing)
      assert.equal(
        missingChecks.some((check) => check.id === 'session-fixture'),
        false,
      )
      for (const id of ['admin-email', 'admin-password', 'user-password']) {
        assert.equal(
          missingChecks.find((check) => check.id === id)?.status,
          'failed',
        )
      }

      const configured = await createRunContext({
        environment: {
          INQTRIX_E2E_ADMIN_EMAIL: 'owner@example.invalid',
          INQTRIX_E2E_ADMIN_PASSWORD: 'admin-private-value',
          INQTRIX_E2E_USER_PASSWORD: 'temporary-user-private-value',
        },
        profile: 'load-smoke',
        repositoryRoot,
        runId: 'inqv-load-smoke-configured-01',
      })
      const configuredChecks = await collaborationLoadAdapter.preflight(
        configured,
      )
      for (const id of [
        'admin-email',
        'admin-password',
        'user-password',
        'generated-session-fixture',
        'base-url',
      ]) {
        assert.equal(
          configuredChecks.find((check) => check.id === id)?.status,
          'passed',
        )
      }
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('explicit Podman load-smoke rejects a drifted VM clock before provisioning', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-load-smoke-clock-'))
    try {
      const binaryDirectory = join(repositoryRoot, 'bin')
      const podmanPath = join(binaryDirectory, 'podman')
      mkdirSync(binaryDirectory)
      writeFileSync(
        podmanPath,
        [
          '#!/bin/sh',
          'case "$1" in',
          '  version|info) exit 0 ;;',
          '  machine) printf "%s\\n" "$INQTRIX_FAKE_VM_EPOCH"; exit 0 ;;',
          '  *) exit 23 ;;',
          'esac',
          '',
        ].join('\n'),
        { mode: 0o700 },
      )
      chmodSync(podmanPath, 0o700)
      const context = await createRunContext({
        containerEngine: 'podman',
        environment: {
          INQTRIX_E2E_ADMIN_EMAIL: 'owner@example.invalid',
          INQTRIX_E2E_ADMIN_PASSWORD: 'admin-private-value',
          INQTRIX_E2E_USER_PASSWORD: 'temporary-user-private-value',
          INQTRIX_FAKE_VM_EPOCH: String((Date.now() / 1_000) - 824),
          PATH: `${binaryDirectory}${delimiter}${process.env.PATH ?? ''}`,
        },
        profile: 'load-smoke',
        repositoryRoot,
        runId: 'inqv-load-smoke-clock-01',
      })

      const checks = await collaborationLoadAdapter.preflight(context)

      assert.equal(
        checks.find((check) => check.id === 'load-podman-clock')?.status,
        'failed',
      )

      const alignedContext = await createRunContext({
        containerEngine: 'podman',
        environment: {
          INQTRIX_E2E_ADMIN_EMAIL: 'owner@example.invalid',
          INQTRIX_E2E_ADMIN_PASSWORD: 'admin-private-value',
          INQTRIX_E2E_USER_PASSWORD: 'temporary-user-private-value',
          INQTRIX_FAKE_VM_EPOCH: String(Date.now() / 1_000),
          PATH: `${binaryDirectory}${delimiter}${process.env.PATH ?? ''}`,
        },
        profile: 'load-smoke',
        repositoryRoot,
        runId: 'inqv-load-smoke-clock-02',
      })
      const alignedChecks = await collaborationLoadAdapter.preflight(
        alignedContext,
      )
      assert.equal(
        alignedChecks.find((check) => check.id === 'load-podman-clock')?.status,
        'passed',
      )
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('load-soak preflight requires credentials and an explicit container engine', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-load-soak-'))
    try {
      const binaryDirectory = join(repositoryRoot, 'bin')
      const podmanPath = join(binaryDirectory, 'podman')
      mkdirSync(binaryDirectory)
      writeFileSync(
        podmanPath,
        [
          '#!/bin/sh',
          'case "$1" in',
          '  version|info) exit 0 ;;',
          '  machine) printf "%s\\n" "$INQTRIX_FAKE_VM_EPOCH"; exit 0 ;;',
          '  *) exit 23 ;;',
          'esac',
          '',
        ].join('\n'),
        { mode: 0o700 },
      )
      chmodSync(podmanPath, 0o700)
      const context = await createRunContext({
        containerEngine: 'podman',
        environment: {
          INQTRIX_E2E_ADMIN_EMAIL: 'owner@example.invalid',
          INQTRIX_E2E_ADMIN_PASSWORD: 'admin-private-value',
          INQTRIX_E2E_USER_PASSWORD: 'temporary-user-private-value',
          INQTRIX_FAKE_VM_EPOCH: String(Date.now() / 1_000),
          PATH: `${binaryDirectory}${delimiter}${process.env.PATH ?? ''}`,
        },
        profile: 'load-soak',
        repositoryRoot,
        runId: 'inqv-load-soak-configured-01',
      })
      const checks = await collaborationLoadAdapter.preflight(context)
      for (const id of [
        'admin-email',
        'admin-password',
        'user-password',
        'generated-session-fixture',
        'base-url',
      ]) {
        assert.equal(checks.find((check) => check.id === id)?.status, 'passed')
      }
      assert.equal(
        checks.find((check) => check.id === 'container-engine-selected')?.status,
        'passed',
      )
      assert.equal(
        checks.find((check) => check.id === 'load-soak-podman')?.status,
        'passed',
      )
      assert.equal(
        checks.find((check) => check.id === 'load-podman-clock')?.status,
        'passed',
      )

      const driftedContext = await createRunContext({
        containerEngine: 'podman',
        environment: {
          INQTRIX_E2E_ADMIN_EMAIL: 'owner@example.invalid',
          INQTRIX_E2E_ADMIN_PASSWORD: 'admin-private-value',
          INQTRIX_E2E_USER_PASSWORD: 'temporary-user-private-value',
          INQTRIX_FAKE_VM_EPOCH: String((Date.now() / 1_000) - 824),
          PATH: `${binaryDirectory}${delimiter}${process.env.PATH ?? ''}`,
        },
        profile: 'load-soak',
        repositoryRoot,
        runId: 'inqv-load-soak-drifted-01',
      })
      const driftedChecks = await collaborationLoadAdapter.preflight(
        driftedContext,
      )
      assert.equal(
        driftedChecks.find((check) => check.id === 'load-podman-clock')?.status,
        'failed',
      )
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('measures clock skew only outside the host command window', () => {
    assert.equal(clockSkewOutsideWindowSeconds(100.5, 100_000, 101_000), 0)
    assert.equal(clockSkewOutsideWindowSeconds(95, 100_000, 101_000), 5)
    assert.equal(clockSkewOutsideWindowSeconds(106, 100_000, 101_000), 5)
    assert.throws(
      () => clockSkewOutsideWindowSeconds(100, 101_000, 100_000),
      /finite and ordered/,
    )
  })

  test('system-smoke provisions its collaboration fixture when no external fixture is supplied', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-system-fixture-'))
    try {
      const context = await createRunContext({
        environment: {
          INQTRIX_E2E_ADMIN_EMAIL: 'owner@example.invalid',
          INQTRIX_E2E_ADMIN_PASSWORD: 'admin-private-value',
          INQTRIX_E2E_BASE_URL: 'https://127.0.0.1:8080',
          INQTRIX_E2E_USER_PASSWORD: 'temporary-user-private-value',
        },
        profile: 'system-smoke',
        repositoryRoot,
        runId: 'inqv-system-fixture-preflight-01',
      })

      const checks = await collaborationPlaywrightAdapter.preflight(context)

      for (const id of [
        'admin-email',
        'admin-password',
        'user-password',
        'generated-system-fixture',
        'base-url',
      ]) {
        assert.equal(
          checks.find((check) => check.id === id)?.status,
          'passed',
          id,
        )
      }
      assert.equal(
        checks.some((check) => check.id.startsWith('external-fixture')),
        false,
      )
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('fault-injection provisions a scoped controller when no external fixture is supplied', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-fault-fixture-'))
    try {
      const context = await createRunContext({
        environment: {
          INQTRIX_E2E_ADMIN_EMAIL: 'owner@example.invalid',
          INQTRIX_E2E_ADMIN_PASSWORD: 'admin-private-value',
          INQTRIX_E2E_BASE_URL: 'https://127.0.0.1:8080',
          INQTRIX_E2E_USER_PASSWORD: 'temporary-user-private-value',
        },
        profile: 'fault-injection',
        repositoryRoot,
        runId: 'inqv-fault-fixture-preflight-01',
      })

      const checks = await collaborationPlaywrightAdapter.preflight(context)

      for (const id of [
        'admin-email',
        'admin-password',
        'user-password',
        'generated-fault-fixture',
        'base-url',
      ]) {
        assert.equal(
          checks.find((check) => check.id === id)?.status,
          'passed',
          id,
        )
      }
      assert.equal(
        checks.find((check) => check.id === 'container-engine-selected')?.status,
        'failed',
      )
      assert.equal(
        checks.some((check) => check.id.startsWith('external-fixture')),
        false,
      )
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('builds a private minimized active-transport system fixture', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-system-builder-'))
    const reportDirectory = join(directory, 'report')
    const fixturePath = join(
      reportDirectory,
      '.cleanup-secrets',
      'system-fixture.json',
    )
    try {
      const fixture = buildGeneratedSystemSmokeFixture({
        baseURL: 'https://127.0.0.1:8080/path',
        collaborator: {
          displayName: 'Collaborator',
          storageState: join(directory, 'collaborator.json'),
          userId: '00000000-0000-4000-8000-000000000002',
        },
        documents: {
          concurrent: { id: 'concurrent-document' },
          detachedTransfer: { id: 'detached-transfer-document' },
          directEdit: { id: 'direct-document' },
          ime: { id: 'ime-document' },
          largeState: { id: 'large-state-document' },
          layout: { id: 'layout-document' },
          mobileDrawers: { id: 'mobile-drawers-document' },
          remotePresence: { id: 'remote-presence-document' },
          revocation: { id: 'revocation-document' },
          sourceReadonly: { id: 'source-readonly-document' },
          staysConnected: { id: 'stays-connected-document' },
          aiSuggestion: { id: 'ai-suggestion-document' },
          suggestion: { id: 'suggestion-document' },
          suggestionUndo: { id: 'suggestion-undo-document' },
        },
        owner: {
          displayName: 'Owner',
          storageState: join(directory, 'owner.json'),
          userId: '00000000-0000-4000-8000-000000000001',
        },
        runId: 'inqv-system-builder-01',
      })

      assert.deepEqual(Object.keys(fixture.transports), ['python-gateway'])
      assert.equal(
        fixture.transports['python-gateway'].baseURL,
        'https://127.0.0.1:8080',
      )
      assert.equal(
        fixture.documents.suggestion.expectedAuthorId,
        fixture.users.collaborator.userId,
      )
      assert.equal(
        fixture.documents.detachedTransfer,
        'detached-transfer-document',
      )
      assert.equal(
        new Set([
          fixture.documents.concurrent,
          fixture.documents.detachedTransfer,
          fixture.documents.directEdit,
          fixture.documents.ime,
          fixture.documents.largeState,
          fixture.documents.layout,
          fixture.documents.mobileDrawers,
          fixture.documents.remotePresence,
          fixture.documents.revocation,
          fixture.documents.sourceReadonly,
          fixture.documents.suggestion.documentId,
          fixture.documents.suggestionUndo,
        ]).size,
        12,
      )
      await writeGeneratedSystemSmokeFixture(fixturePath, fixture)
      assert.equal(statSync(fixturePath).mode & 0o077, 0)
      assert.equal(
        fixtureIsInsidePrivateRunDirectory(fixturePath, reportDirectory),
        true,
      )
      assert.equal(
        fixtureIsInsidePrivateRunDirectory(
          join(directory, 'external.json'),
          reportDirectory,
        ),
        false,
      )
      assert.throws(
        () => buildGeneratedSystemSmokeFixture({
          baseURL: 'https://127.0.0.1:8080',
          collaborator: fixture.users.collaborator,
          documents: {
            concurrent: 'duplicate-document',
            detachedTransfer: 'detached-transfer-document',
            directEdit: 'duplicate-document',
            ime: 'ime-document',
            largeState: 'large-state-document',
            layout: 'layout-document',
            mobileDrawers: 'mobile-drawers-document',
            remotePresence: 'remote-presence-document',
            revocation: 'revocation-document',
            sourceReadonly: 'source-readonly-document',
            staysConnected: 'stays-connected-document',
            aiSuggestion: 'ai-suggestion-document',
            suggestion: 'suggestion-document',
            suggestionUndo: 'suggestion-undo-document',
          },
          owner: fixture.users.owner,
          runId: 'inqv-system-builder-01',
        }),
        /documents must be distinct/,
      )
    } finally {
      rmSync(directory, { force: true, recursive: true })
    }
  })

  test('builds a private Run-ID-bound active-transport fault fixture', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-fault-builder-'))
    const reportDirectory = join(directory, 'report')
    const fixturePath = join(
      reportDirectory,
      '.cleanup-secrets',
      'fault-fixture.json',
    )
    const actor = (name: string, id: string) => ({
      displayName: name,
      storageState: join(directory, `${name}.json`),
      userId: id,
    })
    const anchor = (prefix: string) => ({
      aiAnchorText: `${prefix}-ai-anchor`,
      aiInstructionText: `${prefix}-ai-instruction`,
      aiText: `${prefix}-ai-proposal`,
      commentAnchorText: `${prefix}-comment-anchor`,
      commentText: `${prefix}-comment`,
    })
    try {
      const fixture: any = buildGeneratedFaultInjectionFixture({
        baseURL: 'https://127.0.0.1:8080/path',
        collaborator: actor(
          'collaborator',
          '00000000-0000-4000-8000-000000000002',
        ),
        controls: {
          authorizationEnv: 'INQTRIX_E2E_CONTROL_TOKEN',
          baseURL: 'http://127.0.0.1:43123',
          paths: {
            armGatewayOutage: '/faults/gateway-outage/arm',
            armLostAck: '/faults/lost-ack/arm',
            armOutage: '/faults/sidecar-outage/arm',
            operationStatus: '/faults/operation/status',
            restart: '/faults/restart',
            restore: '/faults/restore',
          },
        },
        documents: {
          directEdit: 'direct-document',
          downgrade: 'downgrade-document',
          gatewayOutage: 'gateway-document',
          outage: 'outage-document',
          protocol: 'protocol-document',
          reconciliation: 'reconciliation-document',
          revocation: 'revocation-document',
          suggestion: 'suggestion-document',
        },
        owner: actor('owner', '00000000-0000-4000-8000-000000000001'),
        privateAnchors: {
          collaborator: anchor('collaborator'),
          documents: {
            'chromium-desktop': 'private-anchor-chromium-desktop',
            'chromium-mobile': 'private-anchor-chromium-mobile',
            'firefox-desktop': 'private-anchor-firefox-desktop',
            'webkit-desktop': 'private-anchor-webkit-desktop',
          },
          owner: anchor('owner'),
        },
        runId: 'inqv-fault-builder-01',
      })

      assert.deepEqual(Object.keys(fixture.transports), ['python-gateway'])
      assert.equal(fixture.controls.runId, 'inqv-fault-builder-01')
      assert.equal(fixture.controls.baseURL, 'http://127.0.0.1:43123')
      assert.equal(
        fixture.privateAnchors.owner.aiInstructionText,
        'owner-ai-instruction',
      )
      assert.deepEqual(
        Object.keys(fixture.privateAnchors.documents),
        [...GENERATED_FAULT_INJECTION_PRIVATE_ANCHOR_TARGETS],
      )
      assert.equal(
        new Set([
          fixture.documents.directEdit,
          fixture.documents.downgrade,
          fixture.documents.gatewayOutage,
          fixture.documents.outage,
          fixture.documents.protocol,
          fixture.documents.reconciliation,
          fixture.documents.revocation,
          fixture.documents.suggestion.documentId,
          ...Object.values(fixture.privateAnchors.documents),
        ]).size,
        12,
      )
      await writeGeneratedFaultInjectionFixture(fixturePath, fixture)
      assert.equal(statSync(fixturePath).mode & 0o077, 0)
    } finally {
      rmSync(directory, { force: true, recursive: true })
    }
  })

  test('builds a minimized private 20-session smoke fixture', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-load-fixture-'))
    const fixturePath = join(directory, 'private', 'fixture.json')
    const runId = 'inqv-load-fixture-builder-01'
    try {
      const sessions = Array.from(
        { length: LOAD_SMOKE_CONNECTIONS },
        (_, index) => ({
          access: 'edit' as const,
          expires_at: 4_102_444_800,
          ignored_display_name: 'must not persist',
          initial_write_mode: 'edit' as const,
          lease_token: `private-lease-${index}`,
          protocol_version: 1,
          refresh_after: 4_102_444_700,
          room: 'inqtrix-editor-v1:load:g1',
          schema_version: 1,
          user: {
            id: `user-${index % 4}`,
            name: 'must not persist',
          },
          websocket_path: '/collaboration',
        }),
      )
      const fixture = buildLoadSmokeFixture({
        baseURL: 'http://127.0.0.1:8080/path',
        runId,
        sessions,
      })
      assert.equal(fixture.sessions.length, 20)
      assert.equal(new Set(fixture.sessions.map((row) => row.user.id)).size, 4)
      assert.deepEqual(
        [...fixture.sessions.reduce((counts, row) => {
          counts.set(row.user.id, (counts.get(row.user.id) ?? 0) + 1)
          return counts
        }, new Map<string, number>()).values()].sort(),
        [5, 5, 5, 5],
      )
      assert.equal(
        new Set(fixture.sessions.slice(0, 5).map((row) => row.user.id)).size,
        4,
      )
      assert.equal(fixture.base_url, 'http://127.0.0.1:8080')
      assert.equal(
        fixture.sessions.every(
          (row) => Object.keys(row.user).join(',') === 'id',
        ),
        true,
      )
      assert.equal(
        new Set(fixture.sessions.map((row) => row.reissue_id)).size,
        20,
      )

      await writePrivateLoadSmokeFixture(fixturePath, fixture)
      if (process.platform !== 'win32') {
        assert.equal(statSync(fixturePath).mode & 0o077, 0)
      }
      const serialized = readFileSync(fixturePath, 'utf8')
      assert.doesNotMatch(serialized, /must not persist/)
      assert.match(serialized, /private-lease-0/)
    } finally {
      rmSync(directory, { force: true, recursive: true })
    }
  })

  test('builds deterministic load document states only for the smoke profile', () => {
    const runId = 'inqv-load-document-seed-01'
    const standard = buildLoadDocumentSeed({
      loadProfile: 'load-smoke',
      runId,
    })
    assert.deepEqual(standard, {
      characterCount: standard.markdown.length,
      markdown: `# System\n\nRun ${runId}. Synthetische Lastdaten.`,
      paragraphCount: 1,
      profile: 'standard',
    })
    assert.deepEqual(
      buildLoadDocumentSeed({ loadProfile: 'load-soak', runId }),
      standard,
    )

    const largeSeed = buildLargeCollaborationDocumentSeed({ runId })
    const large = buildLoadDocumentSeed({
      loadProfile: 'load-smoke',
      requestedProfile: 'large-state',
      runId,
    })
    assert.deepEqual(large, { ...largeSeed, profile: 'large-state' })
    assert.equal(large.profile, 'large-state')
    assert.equal(
      large.paragraphCount,
      LARGE_COLLABORATION_DOCUMENT_PARAGRAPHS,
    )
    assert.equal(large.characterCount, large.markdown.length)
    assert.match(large.markdown, /inqtrix-load-seed-inqv-load-document-seed-01-0001/)
    assert.match(large.markdown, /inqtrix-load-seed-inqv-load-document-seed-01-1500/)
    assert(large.characterCount >= 110_000 && large.characterCount <= 140_000)

    assert.throws(
      () => buildLoadDocumentSeed({
        loadProfile: 'load-smoke',
        requestedProfile: 'unknown',
        runId,
      }),
      /must be standard or large-state/,
    )
    assert.throws(
      () => buildLoadDocumentSeed({
        loadProfile: 'load-soak',
        requestedProfile: 'large-state',
        runId,
      }),
      /supported by load-smoke only/,
    )
  })

  test('builds one ordered session for each of 25 soak identities', () => {
    const runId = 'inqv-load-soak-builder-01'
    const accesses = [
      ...Array.from({ length: LOAD_SOAK_WRITERS }, () => 'edit' as const),
      ...Array.from({ length: LOAD_SOAK_COMMENTERS }, () => 'suggest' as const),
      ...Array.from({ length: LOAD_SOAK_READERS }, () => 'view' as const),
      ...Array.from({ length: LOAD_SOAK_FEATURE_ACTORS }, () => 'view' as const),
    ]
    assert.equal(accesses.length, LOAD_SOAK_CONNECTIONS)
    const sessions = accesses.map((access, index) => ({
      access,
      expires_at: 4_102_444_800,
      initial_write_mode: access,
      lease_token: `private-soak-lease-${index}`,
      protocol_version: 1,
      refresh_after: 4_102_444_700,
      room: 'inqtrix-editor-v1:soak:g1',
      schema_version: 1,
      user: { id: `soak-user-${index}` },
      websocket_path: '/collaboration',
    }))
    const fixture = buildLoadSoakFixture({
      baseURL: 'http://127.0.0.1:8080/path',
      controls: {
        authorizationEnv: 'INQTRIX_LOAD_CONTROL_TOKEN',
        baseURL: 'http://127.0.0.1:43123',
        networkPath: '/control/network-phase',
        reissuePath: '/control/session-reissue',
      },
      runId,
      sessions,
    })
    assert.equal(fixture.sessions.length, 25)
    assert.equal(new Set(fixture.sessions.map((row) => row.user.id)).size, 25)
    assert.deepEqual(
      fixture.sessions.map((row) => row.access),
      accesses,
    )
    assert.equal(new Set(fixture.sessions.map((row) => row.reissue_id)).size, 25)
    assert.equal(fixture.base_url, 'http://127.0.0.1:8080')
    assert.equal(fixture.network_control.run_id, runId)
    assert.equal(fixture.session_reissue.run_id, runId)
    assert.equal(
      fixture.network_control.url,
      'http://127.0.0.1:43123/control/network-phase',
    )
    assert.equal(
      fixture.session_reissue.url,
      'http://127.0.0.1:43123/control/session-reissue',
    )
  })
})

describe('run context and CLI', () => {
  test('reads the report product version from the Python package source', () => {
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-version-source-'))
    const sourcePath = join(directory, '__init__.py')
    try {
      writeFileSync(sourcePath, '__version__ = "9.8.7"\n', 'utf8')
      assert.equal(readInqtrixVersion(sourcePath), '9.8.7')
      writeFileSync(sourcePath, 'VERSION = "9.8.7"\n', 'utf8')
      assert.throws(
        () => readInqtrixVersion(sourcePath),
        /Unable to read the Inqtrix version/,
      )
    } finally {
      rmSync(directory, { force: true, recursive: true })
    }
  })

  test('creates stable machine-readable run IDs and rejects path-like values', () => {
    const runId = createRunId(
      new Date('2026-07-19T10:11:12.345Z'),
      '12345678-1234-4000-8000-123456789abc',
    )
    assert.equal(runId, 'inqv-20260719t101112z-12345678')
    assert.equal(validateRunId(runId), runId)
    for (const invalid of ['../escape', 'INQV-UPPERCASE', 'inqv-short', 'other-valid-name']) {
      assert.throws(() => validateRunId(invalid))
    }
  })

  test('rejects report-directory reuse for an existing Run ID', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-run-collision-'))
    try {
      const options = {
        profile: 'ui-fixture' as const,
        repositoryRoot,
        runId: 'inqv-collision-run-0001',
      }
      await createRunContext(options)
      await assert.rejects(
        () => createRunContext(options),
        /already has a report directory/,
      )
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('accepts only orchestrator controls and rejects runner bypass arguments', () => {
    const options = parseCliArguments([
      '--profile',
      'fault-injection',
      '--fixture',
      '/tmp/fixture.json',
      '--preflight-only',
    ])
    assert.equal(options.profile, 'fault-injection')
    assert.equal(options.preflightOnly, true)
    assert.equal(options.browserTarget, null)
    assert.equal(options.containerEngine, null)
    assert.equal(
      parseCliArguments([
        '--profile',
        'owner-setup',
        '--browser',
        'firefox',
      ]).browserTarget,
      'firefox',
    )
    assert.throws(
      () => parseCliArguments(['--profile', 'owner-setup']),
      /requires one explicit --browser/,
    )
    assert.throws(
      () => parseCliArguments([
        '--profile',
        'owner-setup',
        '--browser',
        'safari',
      ]),
      /must be exactly/,
    )
    assert.throws(
      () => parseCliArguments([
        '--profile',
        'system-smoke',
        '--browser',
        'chromium',
      ]),
      /reserved for the owner-setup profile/,
    )
    assert.equal(
      parseCliArguments([
        '--profile',
        'edge-conformance',
        '--container-engine',
        'podman',
      ]).containerEngine,
      'podman',
    )
    assert.throws(
      () => parseCliArguments([
        '--profile',
        'edge-conformance',
        '--container-engine',
        'auto',
      ]),
      /must be exactly/,
    )
    assert.throws(
      () => parseCliArguments([
        '--profile',
        'load-smoke',
        '--fixture',
        '/tmp/private-load-fixture.json',
      ]),
      /provisions its own/,
    )
    assert.throws(
      () => parseCliArguments([
        '--profile',
        'load-soak',
        '--fixture',
        '/tmp/private-load-fixture.json',
      ]),
      /provisions its own/,
    )
    for (const argument of [
      '--grep',
      '--project',
      '--reporter',
      '--workers',
      '--connections',
    ]) {
      assert.throws(
        () => parseCliArguments(['--profile', 'system-smoke', argument, 'value']),
        /Unknown verification argument/,
      )
    }
  })
})

describe('report redaction and cleanup', () => {
  test('redacts key-based, URL, bearer, guest-link, and environment secrets', () => {
    const redactor = createRedactor({
      INQTRIX_E2E_ADMIN_EMAIL: 'owner@example.invalid',
      INQTRIX_TEST_PASSWORD: 'test-password-value',
    })
    const redacted = redactor.redact({
      console: [
        'Bearer abc.def test-password-value owner@example.invalid',
        'unconfigured@example.invalid',
        '00000000-0000-4000-8000-000000000099',
        '/s/secret-link',
        'Cookie: inqtrix_session=session-cookie-value; inqtrix_csrf=csrf-cookie-value',
        'Set-Cookie: inqtrix_editor_guest=guest-cookie-value; Path=/',
        'inqtrix_editor_guest_csrf=guest-csrf-cookie-value diagnostic-retained',
      ].join('\n'),
      nested: {
        lease_token: 'lease-value',
        ownerEmail: 'another-owner@example.invalid',
        url: 'https://user:password@example.test/path?token=query-value',
        user_id: 'account-identifier',
      },
    })
    const serialized = JSON.stringify(redacted)
    assert.doesNotMatch(
      serialized,
      /abc\.def|test-password-value|secret-link|owner@example\.invalid|unconfigured@example\.invalid|session-cookie-value|csrf-cookie-value|guest-cookie-value|guest-csrf-cookie-value/,
    )
    assert.doesNotMatch(
      serialized,
      /another-owner@example\.invalid|account-identifier/,
    )
    assert.doesNotMatch(serialized, /lease-value|query-value|user:password/)
    assert.doesNotMatch(
      serialized,
      /00000000-0000-4000-8000-000000000099/,
    )
    assert.match(serialized, /REDACTED/)
    assert.match(serialized, /diagnostic-retained/)
  })

  test('redacts the credential headers a failed request lists in its call log', () => {
    const redacted = createRedactor({}).redactMessage([
      'apiRequestContext.fetch: Timeout 30000ms exceeded.',
      '  -   x-csrf-token: csrf-header-value',
      '  -   authorization: Basic basic-header-value',
      '  -   accept: application/json',
    ].join('\n'))

    assert.doesNotMatch(redacted, /csrf-header-value|basic-header-value/)
    assert.match(redacted, /x-csrf-token: \[REDACTED\]/)
    assert.match(redacted, /accept: application\/json/)
  })

  test('redacts child stdout and stderr before forwarding either stream', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-child-output-'))
    let output = ''
    const sink = new Writable({
      write(chunk, _encoding, callback) {
        output += String(chunk)
        callback()
      },
    })
    try {
      const context = await createRunContext({
        environment: {
          INQTRIX_E2E_ADMIN_EMAIL: 'configured@example.invalid',
          TEST_SECRET: 'child-private-value',
        },
        profile: 'ui-fixture',
        repositoryRoot,
        runId: 'inqv-child-output-0001',
      })
      const ledger = new CleanupLedger(
        context.reportDirectory,
        createRedactor(context.environment),
      )
      const result = await runCommand(context, ledger, {
        args: [
          '-e',
          [
            'process.stdout.write("configured@example.invalid child-private-value\\\\n")',
            'process.stdout.write("Cookie: inqtrix_session=child-session-value; inqtrix_csrf=child-csrf-value\\\\n")',
            'process.stderr.write("unconfigured@example.invalid '
              + '00000000-0000-4000-8000-000000000099\\\\n")',
            'process.stderr.write("inqtrix_editor_guest=child-guest-value diagnostic-retained\\\\n")',
          ].join(';'),
        ],
        command: process.execPath,
        engine: 'ui-fixture-playwright',
        output: {
          stderr: sink,
          stdout: sink,
        },
      })
      await ledger.cleanupAll()
      assert.equal(result.status, 'passed')
      assert.doesNotMatch(
        output,
        /configured@example|unconfigured@example|child-private-value|child-session-value|child-csrf-value|child-guest-value|00000000-0000-4000-8000-000000000099/,
      )
      assert.match(output, /REDACTED/)
      assert.match(output, /diagnostic-retained/)
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('cleans registered resources in reverse order and persists no secret labels', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-cleanup-ledger-'))
    try {
      const redactor = createRedactor({ TEST_TOKEN: 'private-control-value' })
      const ledger = new CleanupLedger(directory, redactor)
      const order: string[] = []
      await ledger.register('resource', 'first private-control-value', async () => {
        order.push('first')
      })
      await ledger.register('resource', 'second', async () => {
        order.push('second')
      })
      const records = await ledger.cleanupAll()
      assert.deepEqual(order, ['second', 'first'])
      assert(records.every((record) => record.status === 'cleaned'))
      const payload = readFileSync(join(directory, 'cleanup-ledger.json'), 'utf8')
      assert.doesNotMatch(payload, /private-control-value/)
    } finally {
      rmSync(directory, { force: true, recursive: true })
    }
  })

  test('persists an empty cleanup ledger for a run with no registered resources', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-empty-cleanup-ledger-'))
    try {
      const ledger = new CleanupLedger(directory, createRedactor({}))
      assert.deepEqual(await ledger.cleanupAll(), [])
      assert.equal(
        readFileSync(join(directory, 'cleanup-ledger.json'), 'utf8'),
        '[]\n',
      )
    } finally {
      rmSync(directory, { force: true, recursive: true })
    }
  })

  test('blocks before execution when preflight fails and redacts the report', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-orchestrator-'))
    let executions = 0
    const adapter: VerificationAdapter = {
      engine: 'ui-fixture-playwright',
      profiles: ['ui-fixture'],
      async preflight() {
        return [{
          engine: 'ui-fixture-playwright',
          id: 'fixture-token',
          message: 'fixture rejected secret-report-value',
          status: 'failed',
        }]
      },
      async execute() {
        executions += 1
        throw new Error('must not execute')
      },
    }
    try {
      const report = await runVerification({
        environment: { TEST_SECRET: 'secret-report-value' },
        profile: 'ui-fixture',
        repositoryRoot,
        runId: 'inqv-test-run-0001',
      }, [adapter])
      assert.equal(report.status, 'blocked')
      assert.equal(report.schemaVersion, 4)
      assert.equal(report.inqtrixVersion, '0.2.0')
      assert.equal(executions, 0)
      assert(
        report.scenarios
          .filter((scenario) => scenario.engine === 'ui-fixture-playwright')
          .every((scenario) => scenario.status === 'blocked'),
      )
      assert(
        report.scenarios
          .filter((scenario) => scenario.engine !== 'ui-fixture-playwright')
          .every((scenario) => scenario.status === 'not_applicable'),
      )
      const payload = readFileSync(
        join(
          repositoryRoot,
          'e2e',
          '.results',
          'verification',
          'inqv-test-run-0001',
          'report.json',
        ),
        'utf8',
      )
      assert.doesNotMatch(payload, /secret-report-value/)
      assert.match(payload, /REDACTED/)
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('runs an independent ready adapter while preserving a blocked profile', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-partial-preflight-'))
    let blockedExecutions = 0
    let readyExecutions = 0
    let readyFails = false
    const blocked: VerificationAdapter = {
      engine: 'collaboration-playwright',
      profiles: ['system-smoke'],
      async preflight() {
        return [{
          engine: 'collaboration-playwright',
          id: 'external-fixture',
          message: 'fixture unavailable',
          status: 'failed',
        }]
      },
      async execute() {
        blockedExecutions += 1
        throw new Error('blocked adapter must not execute')
      },
    }
    const ready: VerificationAdapter = {
      engine: 'editor-system-live',
      profiles: ['system-smoke'],
      async preflight() {
        return [{
          engine: 'editor-system-live',
          id: 'ready',
          message: 'ready',
          status: 'passed',
        }]
      },
      async execute() {
        readyExecutions += 1
        const now = new Date().toISOString()
        return {
          durationMs: 1,
          engine: 'editor-system-live',
          exitCode: readyFails ? 1 : 0,
          finishedAt: now,
          scenarios: [{
            id: 'system.multiuser-live-matrix',
            status: readyFails ? 'failed' : 'passed',
          }],
          signal: null,
          startedAt: now,
          status: readyFails ? 'failed' : 'passed',
        }
      },
    }
    try {
      const report = await runVerification({
        profile: 'system-smoke',
        repositoryRoot,
        runId: 'inqv-partial-preflight-01',
      }, [blocked, ready])
      assert.equal(report.status, 'blocked')
      assert.equal(blockedExecutions, 0)
      assert.equal(readyExecutions, 1)
      assert.deepEqual(
        report.adapters.map((adapter) => adapter.engine),
        ['editor-system-live'],
      )
      assert(
        report.scenarios
          .filter((scenario) => (
            scenario.engine === 'collaboration-playwright'
            && scenario.status !== 'not_applicable'
          ))
          .every((scenario) => scenario.status === 'blocked'),
      )
      assert.equal(
        report.scenarios.find((scenario) => (
          scenario.id === 'system.multiuser-live-matrix'
        ))?.status,
        'passed',
      )
      assert.equal(report.cleanup.status, 'clean')

      const preflightOnly = await runVerification({
        preflightOnly: true,
        profile: 'system-smoke',
        repositoryRoot,
        runId: 'inqv-partial-preflight-02',
      }, [blocked, ready])
      assert.equal(preflightOnly.status, 'blocked')
      assert.equal(readyExecutions, 1)
      assert.equal(preflightOnly.adapters.length, 0)

      readyFails = true
      const failed = await runVerification({
        profile: 'system-smoke',
        repositoryRoot,
        runId: 'inqv-partial-preflight-03',
      }, [blocked, ready])
      assert.equal(failed.status, 'failed')
      assert.equal(blockedExecutions, 0)
      assert.equal(readyExecutions, 2)
      assert.equal(
        failed.scenarios.find((scenario) => (
          scenario.id === 'system.multiuser-live-matrix'
        ))?.status,
        'failed',
      )
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('turns an unexpected adapter preflight exception into a blocked report', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-preflight-error-'))
    const adapter: VerificationAdapter = {
      engine: 'ui-fixture-playwright',
      profiles: ['ui-fixture'],
      async preflight() {
        throw new Error('private preflight detail')
      },
      async execute() {
        throw new Error('must not execute')
      },
    }
    try {
      const report = await runVerification({
        profile: 'ui-fixture',
        repositoryRoot,
        runId: 'inqv-test-run-0002',
      }, [adapter])
      assert.equal(report.status, 'blocked')
      assert.deepEqual(report.preflight, [{
        engine: 'ui-fixture-playwright',
        id: 'adapter-preflight',
        message: 'The adapter preflight failed unexpectedly.',
        status: 'failed',
      }])
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('interrupts a running adapter and records clean process cleanup', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-interrupt-'))
    const abortController = new AbortController()
    const adapter: VerificationAdapter = {
      engine: 'ui-fixture-playwright',
      profiles: ['ui-fixture'],
      async preflight() {
        return [{
          engine: 'ui-fixture-playwright',
          id: 'ready',
          message: 'ready',
          status: 'passed',
        }]
      },
      async execute(context, cleanupLedger) {
        return await runCommand(context, cleanupLedger, {
          args: ['-e', 'setInterval(() => {}, 1000)'],
          command: process.execPath,
          engine: 'ui-fixture-playwright',
        })
      },
    }
    const timer = setTimeout(() => abortController.abort(), 100)
    try {
      const report = await runVerification({
        abortSignal: abortController.signal,
        profile: 'ui-fixture',
        repositoryRoot,
        runId: 'inqv-test-run-0003',
      }, [adapter])
      assert.equal(report.status, 'interrupted')
      assert.equal(report.adapters[0]?.status, 'interrupted')
      assert.equal(
        report.scenarios.find((scenario) => (
          scenario.id === 'ui.lifecycle-isolation'
        ))?.status,
        'failed',
      )
      assert(
        report.scenarios
          .filter((scenario) => (
            scenario.engine === 'ui-fixture-playwright'
            && scenario.id !== 'ui.lifecycle-isolation'
          ))
          .every((scenario) => scenario.status === 'not_run'),
      )
      assert.equal(report.cleanup.status, 'clean')
      assert(report.cleanup.records.every((record) => record.status === 'cleaned'))
    } finally {
      clearTimeout(timer)
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('preserves explicit scenario outcomes and leaves later adapters not run', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-scenario-status-'))
    let liveExecutions = 0
    const collaboration: VerificationAdapter = {
      engine: 'collaboration-playwright',
      profiles: ['system-smoke'],
      async preflight() {
        return [{
          engine: 'collaboration-playwright',
          id: 'ready',
          message: 'ready',
          status: 'passed',
        }]
      },
      async execute() {
        const now = new Date().toISOString()
        return {
          durationMs: 1,
          engine: 'collaboration-playwright',
          exitCode: 1,
          finishedAt: now,
          scenarios: [
            { id: 'system.transport-fingerprint', status: 'passed' },
            { id: 'system.direct-edit', status: 'failed' },
          ],
          signal: null,
          startedAt: now,
          status: 'failed',
        }
      },
    }
    const live: VerificationAdapter = {
      engine: 'editor-system-live',
      profiles: ['system-smoke'],
      async preflight() {
        return [{
          engine: 'editor-system-live',
          id: 'ready',
          message: 'ready',
          status: 'passed',
        }]
      },
      async execute() {
        liveExecutions += 1
        throw new Error('must not execute')
      },
    }
    try {
      const report = await runVerification({
        profile: 'system-smoke',
        repositoryRoot,
        runId: 'inqv-test-run-0004',
      }, [collaboration, live])
      const statuses = new Map(
        report.scenarios.map((scenario) => [scenario.id, scenario.status]),
      )
      assert.equal(report.status, 'failed')
      assert.equal(statuses.get('system.transport-fingerprint'), 'passed')
      assert.equal(statuses.get('system.direct-edit'), 'failed')
      assert.equal(statuses.get('system.concurrent-edits'), 'not_run')
      assert.equal(statuses.get('system.multiuser-live-matrix'), 'not_run')
      assert.equal(statuses.get('fault.revocation'), 'not_applicable')
      assert.equal(liveExecutions, 0)
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('cleans every persisted product resource after partial setup failure', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-product-partial-'))
    const cleaned: string[] = []
    const runId = 'inqv-product-partial-01'
    const temporaryUserEmail = temporaryUserDescriptors(runId)[0]?.email
    assert(temporaryUserEmail)
    const adapter: VerificationAdapter = {
      engine: 'ui-fixture-playwright',
      profiles: ['ui-fixture'],
      async preflight() {
        return [{
          engine: 'ui-fixture-playwright',
          id: 'ready',
          message: 'ready',
          status: 'passed',
        }]
      },
      async execute(context, cleanupLedger) {
        return await runCommand(context, cleanupLedger, {
          args: productLifecycleChild(`
            await lifecycle.register({
              credential: 'admin',
              id: \`ed_\${runId}_partial\`,
              kind: 'document',
              ownerEmail: 'owner@example.invalid',
            })
            await lifecycle.register({
              credential: 'admin',
              id: 'kc-current-run',
              kind: 'knowledge_collection',
              name: runId + ' knowledge fixture',
              ownerEmail: 'owner@example.invalid',
            })
            await lifecycle.register({
              credential: 'admin',
              id: 'run-current-run',
              kind: 'research_run',
              ownerEmail: 'owner@example.invalid',
              question: runId + ' research fixture',
            })
            await lifecycle.register({
              email: ${JSON.stringify(temporaryUserEmail)},
              id: runId + ':' + ${JSON.stringify(temporaryUserEmail)},
              kind: 'temporary_user',
            })
            await lifecycle.register({
              email: ${JSON.stringify(temporaryUserEmail)},
              id: runId + ':' + ${JSON.stringify(temporaryUserEmail)} + ':project',
              kind: 'temporary_user_project',
            })
            process.exit(1)
          `),
          command: process.execPath,
          engine: 'ui-fixture-playwright',
          productCleanup: async (_run, resource) => {
            cleaned.push(resource.kind)
          },
          productLifecycle: true,
        })
      },
    }
    try {
      const report = await runVerification({
        profile: 'ui-fixture',
        repositoryRoot,
        runId,
      }, [adapter])
      assert.equal(report.status, 'failed')
      assert.deepEqual(cleaned, [
        'temporary_user_project',
        'temporary_user',
        'research_run',
        'knowledge_collection',
        'document',
      ])
      assert.equal(
        report.cleanup.records.filter((record) => record.kind === 'resource').length,
        5,
      )
      assert(
        report.cleanup.records.every((record) => record.status === 'cleaned'),
      )
      const persisted = readFileSync(
        join(
          repositoryRoot,
          'e2e',
          '.results',
          'verification',
          runId,
          'cleanup-ledger.json',
        ),
        'utf8',
      )
      assert.doesNotMatch(persisted, /owner@example\.invalid/)
      assert(!persisted.includes(temporaryUserEmail))
      assert.doesNotMatch(persisted, /ed_inqv-product-partial-01_partial/)
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('cleans a temporary owner feature resource before disabling its owner', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-product-owner-order-'))
    const cleaned: string[] = []
    const runId = 'inqv-product-owner-order-01'
    const temporaryUserEmail = temporaryUserDescriptors(runId)[0]?.email
    assert(temporaryUserEmail)
    const adapter: VerificationAdapter = {
      engine: 'ui-fixture-playwright',
      profiles: ['ui-fixture'],
      async preflight() {
        return [{
          engine: 'ui-fixture-playwright',
          id: 'ready',
          message: 'ready',
          status: 'passed',
        }]
      },
      async execute(context, cleanupLedger) {
        return await runCommand(context, cleanupLedger, {
          args: productLifecycleChild(`
            await lifecycle.register({
              email: ${JSON.stringify(temporaryUserEmail)},
              id: runId + ':' + ${JSON.stringify(temporaryUserEmail)},
              kind: 'temporary_user',
            })
            await lifecycle.register({
              credential: 'user',
              id: 'kc-current-run',
              kind: 'knowledge_collection',
              name: runId + ' knowledge fixture',
              ownerEmail: ${JSON.stringify(temporaryUserEmail)},
            })
            process.exit(1)
          `),
          command: process.execPath,
          engine: 'ui-fixture-playwright',
          productCleanup: async (_run, resource) => {
            cleaned.push(resource.kind)
          },
          productLifecycle: true,
        })
      },
    }
    try {
      const report = await runVerification({
        profile: 'ui-fixture',
        repositoryRoot,
        runId,
      }, [adapter])
      assert.equal(report.status, 'failed')
      assert.deepEqual(cleaned, ['knowledge_collection', 'temporary_user'])
      assert.equal(report.cleanup.status, 'clean')
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('rejects cleanup registrations that belong to another verification run', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-product-scope-'))
    const foreignRun = 'inqv-product-foreign-02'
    const foreignUserEmail = temporaryUserDescriptors(foreignRun)[0]?.email
    assert(foreignUserEmail)
    const cleaned: string[] = []
    const adapter: VerificationAdapter = {
      engine: 'ui-fixture-playwright',
      profiles: ['ui-fixture'],
      async preflight() {
        return [{
          engine: 'ui-fixture-playwright',
          id: 'ready',
          message: 'ready',
          status: 'passed',
        }]
      },
      async execute(context, cleanupLedger) {
        return await runCommand(context, cleanupLedger, {
          args: productLifecycleChild(`
            const networkContainer = '${'a'.repeat(64)}'
            let rejected = 0
            for (const resource of [
              {
                credential: 'admin',
                id: 'ed_${foreignRun}_document',
                kind: 'document',
                ownerEmail: 'owner@example.invalid',
              },
              {
                email: ${JSON.stringify(foreignUserEmail)},
                id: runId + ':' + ${JSON.stringify(foreignUserEmail)},
                kind: 'temporary_user',
              },
              {
                email: ${JSON.stringify(foreignUserEmail)},
                id: runId + ':' + ${JSON.stringify(foreignUserEmail)} + ':project',
                kind: 'temporary_user_project',
              },
              {
                composeProject: 'inqtrix',
                containerId: networkContainer,
                engine: 'docker',
                id: runId + ':network-qdisc:' + networkContainer,
                kind: 'network_qdisc',
              },
              {
                composeProject: '../foreign',
                containerId: networkContainer,
                engine: 'podman',
                id: runId + ':network-qdisc:' + networkContainer,
                kind: 'network_qdisc',
              },
              {
                composeProject: 'inqtrix',
                containerId: networkContainer,
                engine: 'podman',
                id: ${JSON.stringify(foreignRun)} + ':network-qdisc:' + networkContainer,
                kind: 'network_qdisc',
              },
              {
                credential: 'user',
                id: 'asset-current-run',
                kind: 'asset',
                ownerEmail: 'owner@example.invalid',
                title: ${JSON.stringify(foreignRun)} + ' Anlage',
              },
            ]) {
              try {
                await lifecycle.register(resource)
              } catch {
                rejected += 1
              }
            }
            process.exit(rejected === 7 ? 0 : 2)
          `),
          command: process.execPath,
          engine: 'ui-fixture-playwright',
          productCleanup: async (_run, resource) => {
            cleaned.push(resource.kind)
          },
          productLifecycle: true,
        })
      },
    }
    try {
      const report = await runVerification({
        profile: 'ui-fixture',
        repositoryRoot,
        runId: 'inqv-product-current-01',
      }, [adapter])
      assert.equal(report.adapters[0]?.exitCode, 0)
      assert.deepEqual(cleaned, [])
      assert.equal(
        report.cleanup.records.filter((record) => record.kind === 'resource').length,
        0,
      )
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('cleans an acknowledged product resource after interrupt', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-product-interrupt-'))
    const cleaned: string[] = []
    const abortController = new AbortController()
    const adapter: VerificationAdapter = {
      engine: 'ui-fixture-playwright',
      profiles: ['ui-fixture'],
      async preflight() {
        return [{
          engine: 'ui-fixture-playwright',
          id: 'ready',
          message: 'ready',
          status: 'passed',
        }]
      },
      async execute(context, cleanupLedger) {
        return await runCommand(context, cleanupLedger, {
          args: productLifecycleChild(`
            await lifecycle.register({
              credential: 'admin',
              id: \`ed_\${runId}_interrupt\`,
              kind: 'document',
              ownerEmail: 'owner@example.invalid',
            })
            await new Promise(() => {})
          `),
          command: process.execPath,
          engine: 'ui-fixture-playwright',
          productCleanup: async (_run, resource) => {
            cleaned.push(resource.kind)
          },
          productLifecycle: true,
        })
      },
    }
    const timer = setTimeout(() => abortController.abort(), 500)
    try {
      const report = await runVerification({
        abortSignal: abortController.signal,
        profile: 'ui-fixture',
        repositoryRoot,
        runId: 'inqv-product-interrupt-01',
      }, [adapter])
      assert.equal(report.status, 'interrupted')
      assert.deepEqual(cleaned, ['document'])
      assert(
        report.cleanup.records.every((record) => record.status === 'cleaned'),
      )
    } finally {
      clearTimeout(timer)
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('cleans an acknowledged product resource after child SIGKILL', {
    skip: process.platform === 'win32',
  }, async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-product-sigkill-'))
    const cleaned: string[] = []
    const runId = 'inqv-product-sigkill-01'
    const temporaryUserEmail = temporaryUserDescriptors(runId)[0]?.email
    assert(temporaryUserEmail)
    const adapter: VerificationAdapter = {
      engine: 'ui-fixture-playwright',
      profiles: ['ui-fixture'],
      async preflight() {
        return [{
          engine: 'ui-fixture-playwright',
          id: 'ready',
          message: 'ready',
          status: 'passed',
        }]
      },
      async execute(context, cleanupLedger) {
        return await runCommand(context, cleanupLedger, {
          args: productLifecycleChild(`
            await lifecycle.register({
              email: ${JSON.stringify(temporaryUserEmail)},
              id: runId + ':' + ${JSON.stringify(temporaryUserEmail)} + ':project',
              kind: 'temporary_user_project',
            })
            process.kill(process.pid, 'SIGKILL')
          `),
          command: process.execPath,
          engine: 'ui-fixture-playwright',
          productCleanup: async (_run, resource) => {
            cleaned.push(resource.kind)
          },
          productLifecycle: true,
        })
      },
    }
    try {
      const report = await runVerification({
        profile: 'ui-fixture',
        repositoryRoot,
        runId,
      }, [adapter])
      assert.equal(report.status, 'failed')
      assert.equal(report.adapters[0]?.signal, 'SIGKILL')
      assert.deepEqual(cleaned, ['temporary_user_project'])
      assert(
        report.cleanup.records.every((record) => record.status === 'cleaned'),
      )
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('cleans a registered network qdisc after child SIGKILL', {
    skip: process.platform === 'win32',
  }, async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-network-sigkill-'))
    const cleaned: string[] = []
    const runId = 'inqv-network-sigkill-01'
    const containerId = 'a'.repeat(64)
    const adapter: VerificationAdapter = {
      engine: 'ui-fixture-playwright',
      profiles: ['ui-fixture'],
      async preflight() {
        return [{
          engine: 'ui-fixture-playwright',
          id: 'ready',
          message: 'ready',
          status: 'passed',
        }]
      },
      async execute(context, cleanupLedger) {
        return await runCommand(context, cleanupLedger, {
          args: productLifecycleChild(`
            const containerId = ${JSON.stringify(containerId)}
            await lifecycle.register({
              composeProject: 'inqtrix',
              containerId,
              engine: 'podman',
              id: runId + ':network-qdisc:' + containerId,
              kind: 'network_qdisc',
            })
            process.kill(process.pid, 'SIGKILL')
          `),
          command: process.execPath,
          engine: 'ui-fixture-playwright',
          productCleanup: async (_run, resource) => {
            cleaned.push(resource.kind)
          },
          productLifecycle: true,
        })
      },
    }
    try {
      const report = await runVerification({
        profile: 'ui-fixture',
        repositoryRoot,
        runId,
      }, [adapter])
      assert.equal(report.status, 'failed')
      assert.equal(report.adapters[0]?.signal, 'SIGKILL')
      assert.deepEqual(cleaned, ['network_qdisc'])
      assert(report.cleanup.records.every((record) => record.status === 'cleaned'))
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('paginates before deleting every document in a run-private project', async () => {
    const cursors: Array<string | null> = []
    const deleted: string[] = []
    const pages = new Map<string | null, unknown>([
      [null, {
        data: [{ id: 'import-1' }, { id: 'import-2' }],
        next_cursor: 'second-page',
      }],
      ['second-page', {
        data: [{ id: 'import-3' }],
        next_cursor: null,
      }],
    ])

    const count = await cleanupOwnedProjectDocuments({
      async deleteDocument(documentId) {
        deleted.push(documentId)
      },
      async fetchPage(cursor) {
        cursors.push(cursor)
        return pages.get(cursor)
      },
    })

    assert.equal(count, 3)
    assert.deepEqual(cursors, [null, 'second-page'])
    assert.deepEqual(deleted, ['import-1', 'import-2', 'import-3'])
  })

  test('does not infer passed scenarios from a successful adapter exit', async () => {
    const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-scenario-missing-'))
    const adapter: VerificationAdapter = {
      engine: 'ui-fixture-playwright',
      profiles: ['ui-fixture'],
      async preflight() {
        return [{
          engine: 'ui-fixture-playwright',
          id: 'ready',
          message: 'ready',
          status: 'passed',
        }]
      },
      async execute() {
        const now = new Date().toISOString()
        return {
          durationMs: 1,
          engine: 'ui-fixture-playwright',
          exitCode: 0,
          finishedAt: now,
          signal: null,
          startedAt: now,
          status: 'passed',
        }
      },
    }
    try {
      const report = await runVerification({
        profile: 'ui-fixture',
        repositoryRoot,
        runId: 'inqv-scenario-missing-01',
      }, [adapter])
      assert.equal(report.status, 'failed')
      assert.equal(report.adapters[0]?.status, 'failed')
      assert.equal(report.scenarios.find((scenario) => scenario.engine === adapter.engine)?.status, 'failed')
      assert(
        report.scenarios
          .filter((scenario) => scenario.engine === 'ui-fixture-playwright')
          .slice(1)
          .every((scenario) => scenario.status === 'not_run'),
      )
    } finally {
      rmSync(repositoryRoot, { force: true, recursive: true })
    }
  })

  test('selectAdapters rejects missing and duplicate engine registrations', () => {
    const adapter: VerificationAdapter = {
      engine: 'ui-fixture-playwright',
      profiles: ['ui-fixture'],
      async execute() {
        throw new Error('not executed')
      },
      async preflight() {
        return []
      },
    }
    assert.deepEqual(selectAdapters('ui-fixture', [adapter]), [adapter])
    assert.throws(
      () => selectAdapters('ui-fixture', [adapter, adapter]),
      /Duplicate verification adapter/,
    )
    assert.throws(() => selectAdapters('system-smoke', [adapter]), /Missing verification adapter/)
  })
})

function productLifecycleChild(source: string): string[] {
  return [
    '--input-type=module',
    '-e',
    `
      const { VerificationLifecycleClient } = await import(
        ${JSON.stringify(LIFECYCLE_CLIENT_URL)}
      )
      const runId = process.env.INQTRIX_VERIFICATION_RUN_ID
      const lifecycle = new VerificationLifecycleClient({
        reportDirectory: process.env.INQTRIX_VERIFICATION_REPORT_DIR,
        runId,
      })
      ${source}
    `,
  ]
}

function testTitles(source: string): string[] {
  // A title that contains an apostrophe is written in double quotes.
  return [...source.matchAll(/\btest\((?:'([^']+)'|"([^"]+)")/g)]
    .map((match) => match[1] ?? match[2])
    .filter((title): title is string => title !== undefined)
}

describe('review-data retention', () => {
  test('keeps explicitly selected data and reports it separately from cleanup', async () => {
    const fixture = await retentionFixture({ retain: true, active: true })
    try {
      const records = await fixture.ledger.cleanupAll()
      assert.deepEqual(records.map((row) => row.status), ['retained', 'retained'])
      assert(fixture.requests.includes('POST /v1/runs/fixture-run/cancel'))
      assert(!fixture.requests.some((request) => request.startsWith('DELETE ')))
      assert.equal(fixture.requests.filter((request) => request === 'POST /api/auth/logout').length, 2)
      const count = fixture.requests.length
      await fixture.ledger.cleanupAll()
      assert.equal(fixture.requests.length, count)
      const writer = new ReportWriter(fixture.context)
      await writer.setCleanup(records)
      assert.equal(writer.report.cleanup.status, 'retained')
      assert.equal(writer.report.cleanup.retained, 2)
      assert.equal(writer.report.cleanup.failed, 0)
    } finally {
      await fixture.close()
    }
  })

  test('deletes the same fixtures when retention is not requested', async () => {
    const fixture = await retentionFixture({ retain: false })
    try {
      const records = await fixture.ledger.cleanupAll()
      assert(records.every((row) => row.status === 'cleaned'))
      assert.deepEqual(fixture.requests.filter((request) => request.startsWith('DELETE ')), [
        'DELETE /v1/runs/fixture-run', 'DELETE /v1/agent-sessions/fixture-session',
      ])
    } finally {
      await fixture.close()
    }
  })

  test('refuses to cancel or delete a run outside the registered verification namespace', async () => {
    const fixture = await retentionFixture({ retain: false, active: true, wrongSession: true })
    try {
      const records = await fixture.ledger.cleanupAll()
      assert.deepEqual(records.map((record) => record.status), ['cleaned', 'failed'])
      assert(!fixture.requests.includes('POST /v1/runs/fixture-run/cancel'))
      assert(!fixture.requests.includes('DELETE /v1/runs/fixture-run'))
    } finally {
      await fixture.close()
    }
  })

  test('does not claim that a vanished run was retained', async () => {
    const fixture = await retentionFixture({ retain: true, active: true, vanish: true })
    try {
      const records = await fixture.ledger.cleanupAll()
      assert.deepEqual(records.map((row) => row.status), ['retained', 'cleaned'])
    } finally {
      await fixture.close()
    }
  })

  test('requires host opt-in and a marked product resource, never authentication state', async () => {
    const context: Parameters<typeof retainsReviewResource>[0] = { profile: 'agent-desk', environment: { INQTRIX_E2E_RETAIN_REVIEW_DATA: '1' } }
    const resource: ProductResource = { kind: 'agent_session', id: 'fixture-session', title: 'inqv-retention-contract Session', ownerEmail: 'review@example.test', credential: 'user', retainForReview: true }
    assert.equal(retainsReviewResource(context, resource), true)
    assert.equal(retainsReviewResource({ ...context, environment: {} }, resource), false)
    assert.equal(retainsReviewResource({ ...context, profile: 'agent-recovery' }, resource), false)
    assert.equal(retainsReviewResource(context, { ...resource, retainForReview: false }), false)
    assert.equal(retainsReviewResource(context, { kind: 'session', id: 'session-test', storageStatePath: '/private/secret', retainForReview: true }), false)
    assert.equal(retainsReviewResource(context, { kind: 'temporary_user', id: 'user-test', email: 'review@example.test', retainForReview: true }), false)
  })

  test('processes cannot masquerade as retained product data', async () => {
    const directory = mkdtempSync(join(tmpdir(), 'inqtrix-retention-process-'))
    try {
      const ledger = new CleanupLedger(directory, createRedactor({}))
      await ledger.register('process', 'child process', async () => 'retained')
      assert.deepEqual((await ledger.cleanupAll()).map((record) => record.status), ['failed'])
    } finally {
      rmSync(directory, { recursive: true, force: true })
    }
  })

  test('retains a library asset only while its title proves the run, and deletes it otherwise', async () => {
    const retained = await retentionFixture({ retain: true, asset: true })
    try {
      assert.deepEqual((await retained.ledger.cleanupAll()).map((record) => record.status), ['retained', 'retained', 'retained'])
      assert(retained.requests.includes('GET /v1/assets/fixture-asset'))
      assert(!retained.requests.some((request) => request.startsWith('DELETE ')))
    } finally {
      await retained.close()
    }
    const renamed = await retentionFixture({ retain: true, asset: true, foreignAssetTitle: true })
    try {
      assert.deepEqual((await renamed.ledger.cleanupAll()).map((record) => record.status), ['retained', 'retained', 'failed'])
    } finally {
      await renamed.close()
    }
    const deleted = await retentionFixture({ retain: false, asset: true })
    try {
      assert((await deleted.ledger.cleanupAll()).every((record) => record.status === 'cleaned'))
      assert.equal(deleted.requests.filter((request) => request.startsWith('DELETE '))[0], 'DELETE /v1/assets/fixture-asset')
    } finally {
      await deleted.close()
    }
  })

  test('verifies retained collections through the real list contract', async () => {
    const fixture = await retentionFixture({ retain: true, collection: true })
    try {
      assert.deepEqual((await fixture.ledger.cleanupAll()).map((record) => record.status), ['retained', 'retained', 'retained'])
      assert(fixture.requests.includes('GET /v1/knowledge/collections'))
      assert(!fixture.requests.includes('GET /v1/knowledge/collections/fixture-collection'))
      assert(!fixture.requests.some((request) => request.startsWith('DELETE ')))
    } finally {
      await fixture.close()
    }
  })
})

async function retentionFixture(options: { retain: boolean; active?: boolean; wrongSession?: boolean; vanish?: boolean; collection?: boolean; asset?: boolean; foreignAssetTitle?: boolean }) {
  const repositoryRoot = mkdtempSync(join(tmpdir(), 'inqtrix-retention-'))
  const runId = 'inqv-review-retention-contract'
  const title = `${runId} Agent-Desk-Verifikation`
  const requests: string[] = []
  let reads = 0
  const server = createServer((request, response) => {
    const path = new URL(request.url ?? '/', 'http://localhost').pathname
    requests.push(`${request.method} ${path}`)
    response.setHeader('Content-Type', 'application/json')
    if (path.startsWith('/v1/') && request.headers['x-inqtrix-workspace-id'] !== 'review-workspace') {
      response.writeHead(403).end('{}')
    } else if (path === '/api/auth/session') {
      response.end(JSON.stringify({ csrf_token: 'fixture-csrf', project_namespace: 'review-workspace', user: { id: 'fixture-owner' } }))
    } else if (path === '/v1/runs/fixture-run' && request.method === 'GET') {
      reads += 1
      if (options.vanish && reads > 1) response.writeHead(404).end('{}')
      // Bound the negative case independently of cancellation: a mutation
      // that removes POST /cancel must fail its assertion instead of hang.
      else response.end(JSON.stringify({ status: options.active && reads === 1 ? 'running' : 'completed', session_id: options.wrongSession ? 'unrelated' : 'fixture-session' }))
    } else if (path.startsWith('/v1/agent-sessions/') && request.method === 'GET') {
      response.end(JSON.stringify({ id: 'fixture-session', title: path.endsWith('/unrelated') ? 'Personal conversation' : title }))
    } else if (path === '/v1/knowledge/collections' && request.method === 'GET') {
      response.end(JSON.stringify({ data: [{ id: 'fixture-collection', name: `${runId} Knowledge` }] }))
    } else if (path === '/v1/assets/fixture-asset' && request.method === 'GET') {
      response.end(JSON.stringify({ id: 'fixture-asset', title: options.foreignAssetTitle ? 'Personal file' : `${runId} Anlage` }))
    } else if (request.method === 'DELETE') {
      response.writeHead(204).end()
    } else if (path.startsWith('/api/auth/') || path === '/v1/runs/fixture-run/cancel') {
      response.end('{}')
    } else {
      response.writeHead(404).end('{}')
    }
  })
  await new Promise<void>((resolveListen, rejectListen) => {
    server.once('error', rejectListen)
    server.listen(0, '127.0.0.1', resolveListen)
  })
  const address = server.address()
  assert(address && typeof address !== 'string')
  const context = await createRunContext({ repositoryRoot, runId, profile: 'agent-desk', environment: {
    INQTRIX_E2E_BASE_URL: `http://127.0.0.1:${address.port}`,
    INQTRIX_E2E_USER_PASSWORD: 'fixture-password',
    INQTRIX_E2E_RETAIN_REVIEW_DATA: options.retain ? '1' : '0',
  } })
  const ledger = new CleanupLedger(context.reportDirectory, createRedactor(context.environment))
  const controller = new ProductResourceController(context, ledger)
  const replies: Array<{ type: string }> = []
  const child = { send: (message: { type: string }) => replies.push(message) } as unknown as Parameters<typeof controller.handle>[0]
  for (const resource of [
    { kind: 'agent_session', id: 'fixture-session', title },
    { kind: 'agent_run', id: 'fixture-run', sessionTitle: title },
    ...(options.collection ? [{ kind: 'knowledge_collection', id: 'fixture-collection', name: `${runId} Knowledge` }] : []),
    ...(options.asset ? [{ kind: 'asset', id: 'fixture-asset', title: `${runId} Anlage` }] : []),
  ]) {
    await controller.handle(child, { protocol: 'inqtrix-verification-resource-v1', requestId: resource.id, runId, type: 'register', resource: {
      ...resource, credential: 'user', ownerEmail: 'review@example.test', retainForReview: true,
    } })
  }
  assert.deepEqual(replies.map((reply) => reply.type), options.collection || options.asset ? ['ack', 'ack', 'ack'] : ['ack', 'ack'])
  return { context, ledger, requests, close: async () => {
    server.closeAllConnections()
    await new Promise<void>((resolveClose) => server.close(() => resolveClose()))
    rmSync(repositoryRoot, { recursive: true, force: true })
  } }
}

describe('temporary account cleanup', () => {
  function cleanupActor(
    fetch: (path: string, options: { headers: Record<string, string>; method: string }) => Promise<unknown>,
  ): CleanupActor {
    return {
      api: { fetch },
      csrf: 'csrf',
      userId: 'admin',
      workspaceId: 'workspace',
    } as unknown as CleanupActor
  }

  function scriptedResponse(status: number, body: unknown) {
    return { json: async () => body, status: () => status }
  }

  test('erases the account and waits for its erasure without a workspace', async () => {
    const requests: Array<{ method: string; path: string; workspace: string | null }> = []
    const reads = [{ status: 'running' }, { status: 'deleted' }]
    const actor = cleanupActor(async (path, options) => {
      requests.push({
        method: options.method,
        path,
        workspace: options.headers['X-Inqtrix-Workspace-Id'] ?? null,
      })
      if (path === '/v1/admin/users') {
        return scriptedResponse(200, {
          users: [{ email: 'temporary@example.invalid', id: 'account-1' }],
        })
      }
      if (options.method === 'DELETE') {
        return scriptedResponse(202, { operation_id: 'del_1' })
      }
      return scriptedResponse(200, reads.shift())
    })

    await eraseTemporaryAccount(actor, 'temporary@example.invalid')

    assert.deepEqual(requests, [
      { method: 'GET', path: '/v1/admin/users', workspace: 'workspace' },
      { method: 'DELETE', path: '/v1/admin/users/account-1', workspace: 'workspace' },
      // An erasure belongs to no workspace; a read naming one answers 404,
      // which would read as finished.
      { method: 'GET', path: '/v1/deletion-operations/del_1', workspace: null },
      { method: 'GET', path: '/v1/deletion-operations/del_1', workspace: null },
    ])
  })

  test('a failed request reports how it failed and no header', async () => {
    const actor = cleanupActor(async () => {
      throw new Error(
        'apiRequestContext.fetch: Timeout 15000ms exceeded.\n'
        + '  -   cookie: inqtrix_session=session-cookie-value',
      )
    })

    await assert.rejects(eraseTemporaryAccount(actor, 'temporary@example.invalid'), (error: Error) => {
      assert.equal(error.message, 'Product cleanup request timed out.')
      return true
    })
  })
})
