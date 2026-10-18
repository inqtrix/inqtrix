import { randomBytes, randomUUID } from 'node:crypto'
import { spawn } from 'node:child_process'
import process from 'node:process'

import {
  eraseTemporaryUser,
  ensureTemporaryUsers,
  temporaryUserDescriptors,
} from '../fixtures/accounts.mjs'
import { assertFixture, fetchActorJson } from '../fixtures/api.mjs'
import { createApiSessionFixtures } from '../fixtures/api-sessions.mjs'
import {
  buildGeneratedFaultInjectionFixture,
  GENERATED_FAULT_INJECTION_PRIVATE_ANCHOR_TARGETS,
  writeGeneratedFaultInjectionFixture,
} from '../fixtures/collaboration-fault-injection.mjs'
import {
  normalizeSystemSmokeBaseURL,
} from '../fixtures/collaboration-system-smoke.mjs'
import {
  ContainerFaultDriver,
  resolveFaultControlContainers,
  startFaultControlServer,
} from '../fixtures/fault-control-server.mjs'
import {
  createCollaborationDocument,
  deleteCollaborationDocument,
} from '../fixtures/documents.mjs'
import {
  VerificationLifecycleClient,
} from '../fixtures/lifecycle-client.mjs'
import {
  cleanupOwnedProjectDocuments,
} from '../fixtures/project-documents.mjs'
import { assertVerificationRunId } from '../fixtures/run-scope.mjs'
import { grantAndAccept } from '../fixtures/shares.mjs'

const runId = requiredEnvironment('INQTRIX_VERIFICATION_RUN_ID')
const reportDirectory = requiredEnvironment('INQTRIX_VERIFICATION_REPORT_DIR')
const fixturePath = requiredEnvironment('INQTRIX_E2E_FIXTURE')
const playwrightCli = requiredEnvironment('INQTRIX_E2E_PLAYWRIGHT_CLI')
const playwrightGrep = requiredEnvironment('INQTRIX_E2E_PLAYWRIGHT_GREP')
const adminEmail = requiredEnvironment('INQTRIX_E2E_ADMIN_EMAIL')
const adminPassword = requiredEnvironment('INQTRIX_E2E_ADMIN_PASSWORD')
const userPassword = requiredEnvironment('INQTRIX_E2E_USER_PASSWORD')
const containerEngine = requiredContainerEngine(
  requiredEnvironment('INQTRIX_E2E_CONTAINER_ENGINE'),
)
const baseURL = normalizeSystemSmokeBaseURL(
  process.env.INQTRIX_E2E_BASE_URL ?? 'http://127.0.0.1:8080',
)
const ignoreHTTPSErrors =
  process.env.INQTRIX_E2E_IGNORE_HTTPS_ERRORS === '1'

assertVerificationRunId(runId)

const lifecycle = new VerificationLifecycleClient({
  reportDirectory,
  runId,
})
const sessions = createApiSessionFixtures({
  baseURL,
  ignoreHTTPSErrors,
  lifecycle,
  runId,
})

let admin
let collaborator
let collaboratorProjectCleanup
let temporaryUser
let controlServer
let outcomeError = null
const cleanupErrors = []
const documents = []

try {
  progress('Authenticating the provisioning owner.')
  admin = await sessions.loginActor(
    adminEmail,
    adminPassword,
    'Fault-injection owner',
    'admin',
  )
  const capabilities = await fetchActorJson(admin, 'GET', '/v1/capabilities')
  assertFixture(
    capabilities.features?.sharing === true
      && capabilities.features?.collaboration === true
      && capabilities.feature_status?.collaboration?.state === 'enabled',
    'Fault-injection requires enabled sharing and collaboration capabilities.',
  )

  progress('Creating one Run-ID-bound collaborator identity.')
  const descriptor = temporaryUserDescriptors(runId)[0]
  assertFixture(descriptor, 'No temporary collaborator descriptor is available.')
  ;[temporaryUser] = await ensureTemporaryUsers({
    adminActor: admin,
    descriptors: [descriptor],
    lifecycle,
    password: userPassword,
    runId,
  })
  collaborator = await sessions.loginActor(
    temporaryUser.email,
    userPassword,
    temporaryUser.displayName,
    'user',
  )
  assertFixture(
    admin.user.id !== collaborator.user.id,
    'Fault-injection identities resolved to the same account.',
  )
  collaboratorProjectCleanup = await lifecycle.register({
    email: temporaryUser.email,
    id: `${runId}:${temporaryUser.email}:project`,
    kind: 'temporary_user_project',
  })
  await clearOwnedProjectDocuments(collaborator)

  const schemaVersion = capabilities.collaboration.schema_version
  const anchors = privateAnchorDescriptors(runId)
  const privateMarkdown = privateAnchorMarkdown(anchors)
  const documentDefinitions = [
    definition('directEdit', 'Fault Direct Edit', 'Direkte Bearbeitung'),
    definition('downgrade', 'Fault Permission Downgrade', 'Rechte-Downgrade'),
    definition('gatewayOutage', 'Fault Gateway Outage', 'Gateway-Ausfall'),
    definition('outage', 'Fault Sidecar Outage', 'Sidecar-Ausfall'),
    definition('protocol', 'Fault Protocol Rejection', 'Protokollablehnung'),
    definition('reconciliation', 'Fault Lost ACK', 'Verlorene Bestätigung'),
    definition('revocation', 'Fault Revocation', 'Live-Widerruf'),
    definition('suggestion', 'Fault Suggestion Principal', 'Vorschlagsrolle', 'suggest'),
    ...GENERATED_FAULT_INJECTION_PRIVATE_ANCHOR_TARGETS.map((target) => ({
      key: privateAnchorDocumentKey(target),
      markdown: privateMarkdown,
      permission: 'edit',
      title: `Fault Private Anchors ${privateAnchorTargetTitle(target)}`,
    })),
  ]
  const fixtureDocuments = {}
  for (const item of documentDefinitions) {
    const document = await createCollaborationDocument({
      lifecycle,
      markdown: item.markdown,
      owner: admin,
      runId,
      schemaVersion,
      title: item.title,
    })
    documents.push(document)
    fixtureDocuments[item.key] = document
    await grantAndAccept({
      document,
      lifecycle,
      owner: admin,
      recipients: [[collaborator, item.permission]],
    })
  }

  for (const [index, target] of GENERATED_FAULT_INJECTION_PRIVATE_ANCHOR_TARGETS.entries()) {
    const document = fixtureDocuments[privateAnchorDocumentKey(target)]
    await seedPrivateAiInstruction({
      actor: admin,
      descriptor: anchors.owner,
      document,
      markdown: privateMarkdown,
      sequence: index * 2 + 1,
    })
    await seedPrivateAiInstruction({
      actor: collaborator,
      descriptor: anchors.collaborator,
      document,
      markdown: privateMarkdown,
      sequence: index * 2 + 2,
    })
  }

  progress('Starting the loopback-only, Run-ID-scoped fault controller.')
  const targets = await resolveFaultControlContainers({
    engine: containerEngine,
    repositoryRoot: process.cwd(),
  })
  const driver = new ContainerFaultDriver({
    ...targets,
    engine: containerEngine,
    repositoryRoot: process.cwd(),
  })
  await driver.initialize()
  const controlToken = randomBytes(32).toString('base64url')
  controlServer = await startFaultControlServer({
    allowedDocuments: Object.fromEntries(
      Object.values(fixtureDocuments).map((document) => [
        document.id,
        [admin.user.id, collaborator.user.id],
      ]),
    ),
    driver,
    runId,
    token: controlToken,
  })

  const fixture = buildGeneratedFaultInjectionFixture({
    baseURL,
    collaborator: {
      displayName: temporaryUser.displayName,
      storageState: collaborator.lifecycleSession.storageStatePath,
      userId: collaborator.user.id,
    },
    controls: {
      authorizationEnv: 'INQTRIX_E2E_CONTROL_TOKEN',
      baseURL: controlServer.baseURL,
      paths: controlServer.paths,
    },
    documents: fixtureDocuments,
    owner: {
      displayName: admin.label,
      storageState: admin.lifecycleSession.storageStatePath,
      userId: admin.user.id,
    },
    privateAnchors: {
      collaborator: anchors.collaborator,
      documents: Object.fromEntries(
        GENERATED_FAULT_INJECTION_PRIVATE_ANCHOR_TARGETS.map((target) => [
          target,
          fixtureDocuments[privateAnchorDocumentKey(target)],
        ]),
      ),
      owner: anchors.owner,
    },
    runId,
  })
  await writeGeneratedFaultInjectionFixture(fixturePath, fixture)

  progress('Running controlled recovery scenarios across the browser matrix.')
  await runPlaywright(playwrightCli, playwrightGrep, {
    INQTRIX_E2E_CONTROL_TOKEN: controlToken,
  })
  progress('Cross-browser fault-injection passed.')
} catch (error) {
  outcomeError = error
} finally {
  if (controlServer) {
    await controlServer.close().catch((error) => cleanupErrors.push(error))
  }
  if (admin) {
    for (const document of [...documents].reverse()) {
      await deleteCollaborationDocument({
        document,
        lifecycle,
        owner: admin,
      }).catch((error) => cleanupErrors.push(error))
    }
  }
  if (collaborator && collaboratorProjectCleanup) {
    try {
      await clearOwnedProjectDocuments(collaborator)
      await lifecycle.complete(collaboratorProjectCleanup)
      collaboratorProjectCleanup = null
    } catch (error) {
      cleanupErrors.push(error)
    }
  }
  if (collaborator) {
    await sessions.logoutActor(collaborator)
      .catch((error) => cleanupErrors.push(error))
  }
  if (admin && temporaryUser && !collaboratorProjectCleanup) {
    await eraseTemporaryUser(admin, temporaryUser, lifecycle)
      .catch((error) => cleanupErrors.push(error))
  }
  if (admin) {
    await sessions.logoutActor(admin)
      .catch((error) => cleanupErrors.push(error))
  }
  await sessions.closeAll().catch((error) => cleanupErrors.push(error))
  lifecycle.close()
}

if (cleanupErrors.length > 0) {
  const cleanupError = new AggregateError(
    cleanupErrors,
    'Fault-injection cleanup did not complete.',
  )
  if (!outcomeError) outcomeError = cleanupError
}
if (outcomeError) throw outcomeError

function definition(key, title, heading, permission = 'edit') {
  return {
    key,
    markdown: `# ${heading}\n\nRun ${runId}.`,
    permission,
    title,
  }
}

function privateAnchorDescriptors(value) {
  const suffix = value.replaceAll(/[^a-z0-9]+/g, '-').slice(-30)
  return {
    collaborator: {
      aiAnchorText: `collaborator-ai-anchor-${suffix}`,
      aiInstructionText: `Rewrite collaborator AI anchor ${suffix}`,
      aiText: `collaborator-ai-proposal-${suffix}`,
      commentAnchorText: `collaborator-comment-anchor-${suffix}`,
      commentText: `collaborator-private-comment-${suffix}`,
    },
    owner: {
      aiAnchorText: `owner-ai-anchor-${suffix}`,
      aiInstructionText: `Rewrite owner AI anchor ${suffix}`,
      aiText: `owner-ai-proposal-${suffix}`,
      commentAnchorText: `owner-comment-anchor-${suffix}`,
      commentText: `owner-private-comment-${suffix}`,
    },
  }
}

function privateAnchorDocumentKey(target) {
  return `privateAnchors:${target}`
}

function privateAnchorTargetTitle(target) {
  return target
    .split('-')
    .map((part) => `${part.slice(0, 1).toUpperCase()}${part.slice(1)}`)
    .join(' ')
}

function privateAnchorMarkdown(anchors) {
  return `# ${[
    anchors.owner.aiAnchorText,
    anchors.owner.commentAnchorText,
    anchors.collaborator.aiAnchorText,
    anchors.collaborator.commentAnchorText,
  ].join(' ')}\n\nRun ${runId}.`
}

async function seedPrivateAiInstruction({
  actor,
  descriptor,
  document,
  markdown,
  sequence,
}) {
  const now = Date.now() / 1000 + sequence / 1000
  const aiAnchor = textAnchor(markdown, descriptor.aiAnchorText)
  const response = await fetchActorJson(
    actor,
    'POST',
    `/v1/editor/documents/${encodeURIComponent(document.id)}/comments`,
    {
      data: {
        comments: [privateComment({
          anchor: aiAnchor,
          body: descriptor.aiInstructionText,
          id: `edc_${runId}_${sequence}_ai`,
          kind: 'inline_edit',
          now,
        })],
      },
      expected: [201],
    },
  )
  assertFixture(
    response?.data?.length === 1
      && response.data.every(
        (comment) => comment.created_by_user_id === actor.user.id,
      ),
    `Private anchor creation failed for ${actor.label}.`,
  )
}

function textAnchor(markdown, text) {
  const markdownIndex = markdown.indexOf(text)
  assertFixture(markdownIndex >= 2, 'Private anchor text is absent from its document.')
  const from = markdownIndex - 1
  return {
    from,
    quoteAfter: markdown.slice(markdownIndex + text.length, markdownIndex + text.length + 32),
    quoteBefore: markdown.slice(Math.max(2, markdownIndex - 32), markdownIndex),
    selectedMarkdown: text,
    selectedText: text,
    to: from + text.length,
  }
}

function privateComment({ anchor, body, id, kind, now }) {
  return {
    anchor,
    comment_markdown: body,
    created_at: now,
    evidence_preset: null,
    id,
    kind,
    status: 'open',
    updated_at: now,
  }
}

async function clearOwnedProjectDocuments(actor) {
  return await cleanupOwnedProjectDocuments({
    async deleteDocument(documentId) {
      await fetchActorJson(
        actor,
        'DELETE',
        `/v1/editor/documents/${encodeURIComponent(documentId)}`,
        { expected: [204, 404] },
      )
    },
    async fetchPage(cursor) {
      const parameters = new URLSearchParams({ limit: '200', scope: 'owned' })
      if (cursor) parameters.set('cursor', cursor)
      return await fetchActorJson(
        actor,
        'GET',
        `/v1/editor/documents?${parameters.toString()}`,
      )
    },
  })
}

async function runPlaywright(cli, grep, environment) {
  const child = spawn(
    process.execPath,
    [
      cli,
      'test',
      '--config',
      'playwright.config.ts',
      '--grep',
      grep,
    ],
    {
      cwd: process.cwd(),
      env: { ...process.env, ...environment },
      stdio: 'inherit',
    },
  )
  const forwardSignal = (signal) => {
    if (child.exitCode === null && child.signalCode === null) child.kill(signal)
  }
  const terminate = () => forwardSignal('SIGTERM')
  const interrupt = () => forwardSignal('SIGINT')
  process.once('SIGTERM', terminate)
  process.once('SIGINT', interrupt)
  try {
    const outcome = await new Promise((resolveOutcome, reject) => {
      child.once('error', reject)
      child.once('close', (exitCode, signal) => {
        resolveOutcome({ exitCode, signal })
      })
    })
    if (outcome.exitCode !== 0) {
      throw new Error(
        `Collaboration Playwright exited with ${
          outcome.signal ?? `code ${outcome.exitCode}`
        }.`,
      )
    }
  } finally {
    process.removeListener('SIGTERM', terminate)
    process.removeListener('SIGINT', interrupt)
  }
}

function requiredContainerEngine(value) {
  if (value !== 'podman' && value !== 'docker') {
    throw new Error('INQTRIX_E2E_CONTAINER_ENGINE must be podman or docker.')
  }
  return value
}

function requiredEnvironment(name) {
  const value = process.env[name]?.trim()
  if (!value) throw new Error(`${name} is required.`)
  return value
}

function progress(message) {
  process.stderr.write(`[fault-fixture] ${message}\n`)
}
