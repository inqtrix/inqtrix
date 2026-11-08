import { spawn } from 'node:child_process'
import process from 'node:process'

import {
  eraseTemporaryUser,
  ensureTemporaryUsers,
  requireAccountLocale,
  setAccountLocale,
  temporaryUserDescriptors,
} from '../fixtures/accounts.mjs'
import { assertFixture, fetchActorJson } from '../fixtures/api.mjs'
import {
  buildGeneratedSystemSmokeFixture,
  normalizeSystemSmokeBaseURL,
  SYSTEM_SMOKE_LOCALE,
  writeGeneratedSystemSmokeFixture,
} from '../fixtures/collaboration-system-smoke.mjs'
import {
  buildLargeCollaborationDocumentSeed,
} from '../fixtures/collaboration-document-state.mjs'
import {
  createCollaborationDocument,
  deleteCollaborationDocument,
} from '../fixtures/documents.mjs'
import { attemptCleanup } from '../fixtures/cleanup-step.mjs'
import { createApiSessionFixtures } from '../fixtures/api-sessions.mjs'
import {
  VerificationLifecycleClient,
} from '../fixtures/lifecycle-client.mjs'
import {
  cleanupOwnedProjectDocuments,
} from '../fixtures/project-documents.mjs'
import { assertVerificationRunId } from '../fixtures/run-scope.mjs'
import { grantAndAccept } from '../fixtures/shares.mjs'
import { httpsErrorsIgnored } from '../fixtures/https-errors.mjs'

const runId = requiredEnvironment('INQTRIX_VERIFICATION_RUN_ID')
const reportDirectory = requiredEnvironment('INQTRIX_VERIFICATION_REPORT_DIR')
const fixturePath = requiredEnvironment('INQTRIX_E2E_FIXTURE')
const playwrightCli = requiredEnvironment('INQTRIX_E2E_PLAYWRIGHT_CLI')
const playwrightGrep = requiredEnvironment('INQTRIX_E2E_PLAYWRIGHT_GREP')
const adminEmail = requiredEnvironment('INQTRIX_E2E_ADMIN_EMAIL')
const adminPassword = requiredEnvironment('INQTRIX_E2E_ADMIN_PASSWORD')
const userPassword = requiredEnvironment('INQTRIX_E2E_USER_PASSWORD')
const baseURL = normalizeSystemSmokeBaseURL(
  process.env.INQTRIX_E2E_BASE_URL ?? 'http://localhost:8080',
)
const ignoreHTTPSErrors = httpsErrorsIgnored()

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
const documents = []

try {
  progress('Authenticating the provisioning owner.')
  admin = await sessions.loginActor(
    adminEmail,
    adminPassword,
    'System smoke owner',
    'admin',
  )
  const capabilities = await fetchActorJson(admin, 'GET', '/v1/capabilities')
  assertFixture(
    capabilities.features?.sharing === true
      && capabilities.features?.collaboration === true
      && capabilities.feature_status?.collaboration?.state === 'enabled',
    'System-smoke requires enabled sharing and collaboration capabilities.',
  )
  await requireAccountLocale(admin, SYSTEM_SMOKE_LOCALE)

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
    'System-smoke identities resolved to the same account.',
  )
  await setAccountLocale(collaborator, SYSTEM_SMOKE_LOCALE)
  collaboratorProjectCleanup = await lifecycle.register({
    email: temporaryUser.email,
    id: `${runId}:${temporaryUser.email}:project`,
    kind: 'temporary_user_project',
  })
  await clearOwnedProjectDocuments(collaborator)

  const schemaVersion = capabilities.collaboration.schema_version
  const largeDocumentSeed = buildLargeCollaborationDocumentSeed({ runId })
  const documentDefinitions = [
    {
      key: 'detachedTransfer',
      markdown: `# Projekttransfer\n\nRun ${runId}.`,
      permission: 'edit',
      title: 'System Smoke Detached Transfer',
    },
    {
      key: 'directEdit',
      markdown: `# Direkte Bearbeitung\n\nRun ${runId}.`,
      permission: 'edit',
      title: 'System Smoke Direct Edit',
    },
    {
      key: 'concurrent',
      markdown: `# Gleichzeitige Bearbeitung\n\nRun ${runId}.`,
      permission: 'edit',
      title: 'System Smoke Concurrent Edit',
    },
    {
      // Eigenes Dokument: dieses Szenario VEROEFFENTLICHT einen Vorschlag
      // und veraendert das Dokument damit dauerhaft. Der Satz ist woertlich
      // bekannt, weil der gestubbte Vorschlag ihn als Anker sucht.
      key: 'aiSuggestion',
      markdown: `# KI-Vorschlag\n\nDer Assistent ersetzt hier ein Wort. Run ${runId}.`,
      permission: 'edit',
      title: 'System Smoke AI Suggestion',
    },
    {
      // Eigenes Dokument, bewusst von keinem anderen Szenario benutzt: der
      // Stabilitaetstest haelt seine beiden Sitzungen eine halbe Minute lang
      // offen und behauptet, dass keine davon abbricht. Teilte er sich das
      // Dokument mit einem Szenario, das die Seite absichtlich neu laedt,
      // liefe er in den Deckel von fuenf gleichzeitigen Sitzungen je Nutzer
      // und Dokument -- und meldete den Ratenschutz als Verbindungsabbruch.
      key: 'staysConnected',
      markdown: `# Dauerhafte Verbindung\n\nRun ${runId}.`,
      permission: 'edit',
      title: 'System Smoke Stays Connected',
    },
    {
      key: 'remotePresence',
      markdown: `# Remote-Präsenz\n\nRun ${runId}.`,
      permission: 'edit',
      title: 'System Smoke Remote Presence',
    },
    {
      key: 'revocation',
      markdown: `# Live-Widerruf\n\nRun ${runId}.`,
      permission: 'edit',
      title: 'System Smoke Revocation',
    },
    {
      key: 'suggestion',
      markdown: `# Vorschläge\n\nRun ${runId}.`,
      permission: 'suggest',
      title: 'System Smoke Suggestions',
    },
    {
      key: 'suggestionUndo',
      markdown: `# Vorschlag zurücknehmen\n\nRun ${runId}.`,
      permission: 'edit',
      title: 'System Smoke Suggestion Undo',
    },
    {
      key: 'ime',
      markdown: `# IME-Vorschlag\n\nRun ${runId}.`,
      permission: 'suggest',
      title: 'System Smoke IME',
    },
    {
      key: 'largeState',
      markdown: largeDocumentSeed.markdown,
      permission: 'edit',
      title: 'System Smoke Large State',
    },
    {
      key: 'sourceReadonly',
      markdown: `# Source-Ansicht\n\nRun ${runId}.`,
      permission: 'edit',
      title: 'System Smoke Source Readonly',
    },
    {
      key: 'layout',
      markdown: `# Editor-Layout\n\nRun ${runId}.`,
      permission: 'suggest',
      title: 'System Smoke Layout',
    },
    {
      key: 'mobileDrawers',
      markdown: `# Mobile Drawer\n\nRun ${runId}.`,
      permission: 'edit',
      title: 'System Smoke Mobile Drawers',
    },
  ]
  const fixtureDocuments = {}
  for (const definition of documentDefinitions) {
    const document = await createCollaborationDocument({
      lifecycle,
      markdown: definition.markdown,
      owner: admin,
      runId,
      schemaVersion,
      title: definition.title,
    })
    documents.push(document)
    fixtureDocuments[definition.key] = document
    await grantAndAccept({
      document,
      lifecycle,
      owner: admin,
      recipients: [[collaborator, definition.permission]],
    })
  }

  const fixture = buildGeneratedSystemSmokeFixture({
    baseURL,
    collaborator: {
      displayName: temporaryUser.displayName,
      storageState: collaborator.lifecycleSession.storageStatePath,
      userId: collaborator.user.id,
    },
    documents: fixtureDocuments,
    owner: {
      displayName: admin.label,
      storageState: admin.lifecycleSession.storageStatePath,
      userId: admin.user.id,
    },
    runId,
  })
  await writeGeneratedSystemSmokeFixture(fixturePath, fixture)

  progress('Running the active gateway across the mandatory browser matrix.')
  await runPlaywright(playwrightCli, playwrightGrep)
  progress('Cross-browser system-smoke passed.')
} finally {
  await new Promise((resolve) => setTimeout(resolve, 500))
  if (admin) {
    for (const document of [...documents].reverse()) {
      await attemptCleanup('document', () => deleteCollaborationDocument({
        document,
        lifecycle,
        owner: admin,
      }))
    }
  }
  if (collaborator && collaboratorProjectCleanup) {
    // On failure both the project and user handles stay live. The parent
    // orchestrator can re-authenticate the still-enabled Run-ID account after
    // this child exits and finish cleanup even after a browser or engine failure.
    await attemptCleanup('collaborator project', async () => {
      await clearOwnedProjectDocuments(collaborator)
      await lifecycle.complete(collaboratorProjectCleanup)
      collaboratorProjectCleanup = null
    })
  }
  if (collaborator) {
    await attemptCleanup('collaborator logout', () => sessions.logoutActor(collaborator))
  }
  if (admin && temporaryUser && !collaboratorProjectCleanup) {
    await attemptCleanup('temporary account', () => eraseTemporaryUser(admin, temporaryUser, lifecycle))
  }
  if (admin) await attemptCleanup('admin logout', () => sessions.logoutActor(admin))
  await sessions.closeAll()
  lifecycle.close()
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

async function runPlaywright(cli, grep) {
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
      env: process.env,
      stdio: 'inherit',
    },
  )
  const forwardSignal = (signal) => {
    if (child.exitCode === null && child.signalCode === null) {
      child.kill(signal)
    }
  }
  const terminate = () => forwardSignal('SIGTERM')
  const interrupt = () => forwardSignal('SIGINT')
  process.once('SIGTERM', terminate)
  process.once('SIGINT', interrupt)
  try {
    const outcome = await new Promise((resolve, reject) => {
      child.once('error', reject)
      child.once('close', (exitCode, signal) => {
        resolve({ exitCode, signal })
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

function requiredEnvironment(name) {
  const value = process.env[name]?.trim()
  if (!value) throw new Error(`${name} is required.`)
  return value
}

function progress(message) {
  process.stderr.write(`[system-fixture] ${message}\n`)
}
