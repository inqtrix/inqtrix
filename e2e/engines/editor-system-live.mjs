import { createHash, randomUUID } from 'node:crypto'
import { mkdir } from 'node:fs/promises'
import { join } from 'node:path'

import { chromium } from '@playwright/test'
import * as Y from 'yjs'

import {
  eraseTemporaryUser,
  ensureTemporaryUsers,
} from '../../tests/verification/fixtures/accounts.mjs'
import {
  assertFixture as assert,
  fetchActorJson as fetchJson,
} from '../../tests/verification/fixtures/api.mjs'
import {
  createCollaborationDocument,
  deleteCollaborationDocument,
} from '../../tests/verification/fixtures/documents.mjs'
import {
  assertVerificationRunId,
} from '../../tests/verification/fixtures/run-scope.mjs'
import {
  guestDescribePath,
  guestUnlockPath,
  createGuestLink,
  grantAndAccept,
  revokeGuestLink,
  rotateGuestPassword,
  updateGuestLink,
} from '../../tests/verification/fixtures/shares.mjs'
import {
  assertGuestSecurityHeaders,
  createSessionFixtures,
  sanitizeGuestDiagnostic,
} from '../../tests/verification/fixtures/sessions.mjs'
import {
  VerificationLifecycleClient,
} from '../../tests/verification/fixtures/lifecycle-client.mjs'
import { guestLinkGateReason } from '../config.ts'

const baseURL = process.env.INQTRIX_E2E_BASE_URL ?? 'http://127.0.0.1:8080'
const publicOrigin = new URL(baseURL).origin
const ignoreHTTPSErrors =
  process.env.INQTRIX_E2E_IGNORE_HTTPS_ERRORS === '1'
const adminEmail = requiredEnvironment('INQTRIX_E2E_ADMIN_EMAIL')
const testerEmail = requiredEnvironment('INQTRIX_E2E_TESTER_EMAIL')
const adminPassword = requiredEnvironment('INQTRIX_E2E_ADMIN_PASSWORD')
const userPassword = requiredEnvironment('INQTRIX_E2E_USER_PASSWORD')
if (adminEmail.toLowerCase() === testerEmail.toLowerCase()) {
  throw new Error('Admin and tester must be distinct accounts.')
}
const executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH
const runKey = requiredEnvironment('INQTRIX_VERIFICATION_RUN_ID')
assertVerificationRunId(runKey, 'INQTRIX_VERIFICATION_RUN_ID')
const screenshotDir = join(
  process.cwd(),
  'e2e',
  '.results',
  'verification',
  runKey,
  'editor-system-live',
)
const lifecycle = new VerificationLifecycleClient({
  reportDirectory: requiredEnvironment('INQTRIX_VERIFICATION_REPORT_DIR'),
  runId: runKey,
})
const liveSlashActions = [
  { id: 'paragraph', label: 'Absatz', selector: 'p' },
  { id: 'heading1', label: 'Überschrift 1', selector: 'h1' },
  { id: 'heading2', label: 'Überschrift 2', selector: 'h2' },
  { id: 'heading3', label: 'Überschrift 3', selector: 'h3' },
  { id: 'bulletList', label: 'Aufzählung', selector: 'ul:not([data-type="taskList"])' },
  { id: 'orderedList', label: 'Nummerierte Liste', selector: 'ol' },
  { id: 'taskList', label: 'Aufgabenliste', selector: 'ul[data-type="taskList"]' },
  { id: 'blockquote', label: 'Zitat', selector: 'blockquote' },
  { id: 'codeBlock', label: 'Codeblock', selector: 'pre' },
  { id: 'table', label: 'Tabelle', selector: 'table' },
  { id: 'divider', label: 'Trennlinie', selector: 'hr' },
]

const report = {
  apiCommentP95Ms: null,
  commentSelectionMs: null,
  consoleErrors: [],
  documentIsolationChecks: 0,
  firstCommentPageMs: null,
  guestGate: {
    reason: 'editor guest links are not enabled',
    status: 'skipped',
  },
  runKey,
  screenshots: [],
  slashMatrix: {
    edit: [],
    suggest: [],
    suggestBlocked: [],
  },
  suggestionUndo: {
    closeEvents: null,
    outboundUndoUpdates: null,
    status: 'not_run',
  },
  users: 6,
  viewports: [],
}

let browser
let admin
let tester
let temporaryUsers = []
let actors = []
let documents = []
let sessionFixtures

try {
  progress('Browser starten')
  await mkdir(screenshotDir, { recursive: true })
  browser = await chromium.launch({ executablePath, headless: true })
  sessionFixtures = createSessionFixtures({
    baseURL,
    browser,
    ignoreHTTPSErrors,
    lifecycle,
    parseCollaborationFrame,
    runId: runKey,
    screenshotDirectory: screenshotDir,
  })
  progress('Admin und Tester anmelden')
  admin = await sessionFixtures.loginActor(
    adminEmail,
    adminPassword,
    'Admin',
    'admin',
  )
  tester = await sessionFixtures.loginActor(
    testerEmail,
    userPassword,
    'Tester',
    'user',
  )
  assert(
    admin.user.id !== tester.user.id,
    'Admin and tester credentials resolved to the same authenticated identity.',
  )

  const capabilities = await fetchJson(admin, 'GET', '/v1/capabilities')
  assert(capabilities.features?.sharing === true, 'Sharing capability is not enabled.')
  assert(capabilities.features?.collaboration === true, 'Collaboration capability is not enabled.')
  assert(
    capabilities.feature_status?.collaboration?.state === 'enabled',
    'Collaboration status is not enabled.',
  )

  temporaryUsers = await ensureTemporaryUsers({
    adminActor: admin,
    lifecycle,
    password: userPassword,
    runId: runKey,
  })
  const temporaryActors = []
  for (const user of temporaryUsers) {
    temporaryActors.push(
      await sessionFixtures.loginActor(
        user.email,
        userPassword,
        user.displayName,
        'user',
      ),
    )
  }
  actors = [admin, tester, ...temporaryActors]
  assert(
    new Set(actors.map((actor) => actor.user.id)).size === actors.length,
    'The six account contexts did not resolve to six distinct identities.',
  )
  progress('Sechs getrennte Browserkontexte bereit')
  const schemaVersion = capabilities.collaboration.schema_version
  const strategy = await createCollaborationDocument({
    lifecycle,
    markdown: '# Strategie\n\nGemeinsame Mehrnutzer-Planung.',
    owner: admin,
    runId: runKey,
    schemaVersion,
    title: 'Strategie',
  })
  documents.push(strategy)
  const budget = await createCollaborationDocument({
    lifecycle,
    markdown: '# Budget\n\nVertrauliche Budgetplanung.',
    owner: admin,
    runId: runKey,
    schemaVersion,
    title: 'Budget',
  })
  documents.push(budget)
  const roadmap = await createCollaborationDocument({
    lifecycle,
    markdown: '# Roadmap\n\nProdukt-Roadmap.',
    owner: admin,
    runId: runKey,
    schemaVersion,
    title: 'Roadmap',
  })
  documents.push(roadmap)
  const testerStatus = await createCollaborationDocument({
    lifecycle,
    markdown: '# Status\n\nStatus des Tester-Teams.',
    owner: tester,
    runId: runKey,
    schemaVersion,
    title: 'Status',
  })
  documents.push(testerStatus)
  const user3Status = await createCollaborationDocument({
    lifecycle,
    markdown: '# Status\n\nStatus des dritten Teams.',
    owner: temporaryActors[0],
    runId: runKey,
    schemaVersion,
    title: 'Status',
  })
  documents.push(user3Status)
  const privateDocument = await createCollaborationDocument({
    lifecycle,
    markdown: '# Privat\n\nNicht freigegeben.',
    owner: admin,
    runId: runKey,
    schemaVersion,
    title: 'Privat',
  })
  documents.push(privateDocument)
  const liveDocument = await createCollaborationDocument({
    lifecycle,
    markdown: [
      '# System Live',
      'Alle Rollen arbeiten in diesem Dokument.',
      'Slot 1:',
      'Slot 2:',
      'Slot 3:',
      'Slot 4:',
      'Slot 5:',
      'Gemeinsamer Slot:',
    ].join('\n\n'),
    owner: admin,
    runId: runKey,
    schemaVersion,
    title: 'System Live',
  })
  documents.push(liveDocument)

  await grantAndAccept({
    document: strategy,
    lifecycle,
    owner: admin,
    recipients: [
      [tester, 'edit'],
      [temporaryActors[0], 'suggest'],
    ],
  })
  await grantAndAccept({
    document: budget,
    lifecycle,
    owner: admin,
    recipients: [[temporaryActors[1], 'view']],
  })
  await grantAndAccept({
    document: roadmap,
    lifecycle,
    owner: admin,
    recipients: [[temporaryActors[2], 'edit']],
  })
  await grantAndAccept({
    document: testerStatus,
    lifecycle,
    owner: tester,
    recipients: [
      [admin, 'suggest'],
      [temporaryActors[1], 'edit'],
    ],
  })
  await grantAndAccept({
    document: user3Status,
    lifecycle,
    owner: temporaryActors[0],
    recipients: [[temporaryActors[2], 'view']],
  })
  const liveRecipients = [
    [tester, 'edit'],
    [temporaryActors[0], 'edit'],
    [temporaryActors[1], 'suggest'],
    [temporaryActors[2], 'suggest'],
    [temporaryActors[3], 'view'],
  ]
  await grantAndAccept({
    document: liveDocument,
    lifecycle,
    owner: admin,
    recipients: liveRecipients,
  })

  progress('Mehrdokument-/Rechtematrix prüfen')
  await verifyDocumentMatrix({
    admin,
    budget,
    privateDocument,
    roadmap,
    strategy,
    temporaryActors,
    tester,
    testerStatus,
    user3Status,
  })

  progress('Account-Freigabe → Vorschlagen → Eingabe → Undo → Reload prüfen')
  await exerciseAccountSuggestionUndo(admin, tester, strategy)

  progress('Alle elf Slash-Aktionen live im Bearbeiten-Modus prüfen')
  await exerciseLiveSlashMatrix(actors, schemaVersion)

  progress('Live-Dokument in sechs Sitzungen öffnen')
  for (const actor of actors) {
    progress(`Editor öffnen: ${actor.label}`)
    await openDocument(actor, liveDocument)
  }
  progress('Rollen und gleichzeitige Änderungen prüfen')
  await assertRolePresentation(actors)
  await exerciseConcurrentEditing(actors)

  progress('Echtzeit-Kommentare und Berechtigungen prüfen')
  let commentRevision = 0
  const liveThread = randomUUID()
  const firstComment = await createComment(admin, liveDocument, {
    body: `Live-Kommentar ${runKey}`,
    expectedRevision: commentRevision,
    mentions: [tester.user.id],
    threadId: liveThread,
  })
  commentRevision = firstComment.revision
  await openComments(tester.page)
  await tester.page.getByText(`Live-Kommentar ${runKey}`, { exact: true }).waitFor()
  const firstReply = await replyComment(tester, liveDocument, {
    body: `Live-Antwort ${runKey}`,
    expectedRevision: commentRevision,
    threadId: liveThread,
  })
  commentRevision = firstReply.revision
  await openComments(admin.page)
  const liveThreadCard = admin.page.locator(
    `[data-team-comment-id="${liveThread}"]`,
  )
  await liveThreadCard.waitFor({ state: 'visible' })
  await liveThreadCard.click()
  await liveThreadCard
    .getByText(`Live-Antwort ${runKey}`, { exact: true })
    .waitFor()
  const secondReply = await replyComment(temporaryActors[0], liveDocument, {
    body: `Zweite Live-Antwort ${runKey}`,
    expectedRevision: commentRevision,
    threadId: liveThread,
  })
  commentRevision = secondReply.revision

  const denied = await fetchJson(temporaryActors[3], 'POST', commentPath(liveDocument.id), {
    data: commentCommand(liveDocument, {
      body: 'View-only must never persist this.',
      expectedRevision: commentRevision,
    }),
    expected: [404],
  })
  assert(denied.error?.type === 'not_found', 'View-only comment denial did not stay hidden.')

  const threadIds = [liveThread]
  const threadRevisions = new Map([[liveThread, secondReply.revision]])
  for (let index = 1; index < 60; index += 1) {
    const creator = actors[index % 5]
    const firstReplier = actors[(index + 1) % 5]
    const secondReplier = actors[(index + 2) % 5]
    const threadId = randomUUID()
    threadIds.push(threadId)
    const created = await createComment(creator, liveDocument, {
      body: `Thread ${String(index + 1).padStart(2, '0')} · ${'Langer Prüfinhalt '.repeat((index % 4) + 1).trim()}`,
      expectedRevision: commentRevision,
      mentions: index % 3 === 0 ? [temporaryActors[2].user.id] : [],
      orphaned: index % 10 === 0,
      threadId,
    })
    commentRevision = created.revision
    const replied = await replyComment(firstReplier, liveDocument, {
      body: `Antwort A auf Thread ${index + 1}`,
      expectedRevision: commentRevision,
      threadId,
    })
    commentRevision = replied.revision
    const repliedAgain = await replyComment(secondReplier, liveDocument, {
      body: `Antwort B auf Thread ${index + 1}`,
      expectedRevision: commentRevision,
      threadId,
    })
    commentRevision = repliedAgain.revision
    threadRevisions.set(threadId, repliedAgain.revision)
    if (index % 12 === 0) {
      const messageId = repliedAgain.thread.messages.at(-1).id
      const tombstoned = await fetchJson(
        secondReplier,
        'DELETE',
        `${commentPath(liveDocument.id)}/${threadId}/messages/${messageId}`,
        {
          data: mutationCommand(liveDocument, commentRevision),
        },
      )
      commentRevision = tombstoned.revision
      threadRevisions.set(threadId, tombstoned.revision)
    }
  }

  progress('60 Threads / 180 Nachrichten im Inspector prüfen')
  await measureCommentApi(tester, liveDocument.id)
  await verifyCommentInspector(tester.page)

  for (let index = 4; index < threadIds.length; index += 4) {
    const threadRevision = threadRevisions.get(threadIds[index])
    assert(threadRevision !== undefined, 'Comment fixture lost a thread revision.')
    const resolved = await fetchJson(
      admin,
      'PATCH',
      `${commentPath(liveDocument.id)}/${threadIds[index]}`,
      {
        data: {
          ...mutationCommand(liveDocument, threadRevision),
          status: 'resolved',
        },
      },
    )
    commentRevision = resolved.revision
    threadRevisions.set(threadIds[index], resolved.revision)
  }
  await verifyResolvedFilter(tester.page)

  progress('Dokumentdetails und responsive Geometrie prüfen')
  await verifyDocumentDetails(admin.page, tester.page, liveDocument.id)
  await verifyResponsiveLayout(tester.page)

  // Decide the precondition BEFORE entering: an environment that cannot
  // serve guest links would otherwise surface as a crash here, after the
  // whole six-user matrix had already passed — taking every green scenario
  // down with it. The section is skipped with its reason stated, never
  // counted as covered.
  const guestGateSkipReason = guestLinkGateReason(
    baseURL,
    capabilities.features?.editor_guest_links === true,
  )
  if (guestGateSkipReason) {
    // The opt-in switch keeps a mandatory gate mandatory: where guest
    // links MUST be proven, an unmet precondition still fails loudly
    // instead of quietly downgrading to a skip.
    if (process.env.INQTRIX_E2E_REQUIRE_GUEST_LINKS === '1') {
      throw new Error(`Guest-link gate is required but cannot run: ${guestGateSkipReason}`)
    }
    progress(`Gastlinkmatrix übersprungen: ${guestGateSkipReason}`)
    report.guestGate = { reason: guestGateSkipReason, status: 'skipped' }
  } else {
    progress('HTTPS-Gastlinkmatrix mit zusätzlichen Gastbrowsern prüfen')
    report.guestGate = await exerciseGuestLinkGate({
      accountActors: actors,
      adminActor: admin,
      document: liveDocument,
      privateDocument,
      protocolVersion: capabilities.collaboration.protocol_version,
      schemaVersion: capabilities.collaboration.schema_version,
    })
  }

  for (const actor of actors) {
    assert(
      actor.errors.length === 0,
      `${actor.label} browser reported errors: ${actor.errors.join(' | ')}`,
    )
  }

  progress('System-Szenarien bestanden')
  process.stdout.write(
    `${JSON.stringify(sanitizeSystemReport({ status: 'passed', ...report }), null, 2)}\n`,
  )
} finally {
  const pages = new Set(
    [admin, tester, ...actors]
      .map((actor) => actor?.page)
      .filter(Boolean),
  )
  await Promise.allSettled([...pages].map((page) => page.close()))
  if (sessionFixtures) await sessionFixtures.closeGuestContexts()
  // Give WebSocket/SSE disconnects and any cancelled reads one event-loop
  // window to settle before deleting their authoritative document fixtures.
  await new Promise((resolve) => setTimeout(resolve, 500))
  if (admin?.context) {
    for (const document of [...documents].reverse()) {
      const owner = actors.find((actor) => actor.user.id === document.ownerId)
      if (!owner) continue
      await deleteCollaborationDocument({
        document,
        lifecycle,
        owner,
      }).catch(() => undefined)
    }
    for (const user of temporaryUsers) {
      await eraseTemporaryUser(admin, user, lifecycle)
        .catch(() => undefined)
    }
  }
  if (sessionFixtures) {
    const accountActors = new Set([admin, tester, ...actors].filter(Boolean))
    for (const actor of accountActors) {
      await sessionFixtures.logoutActor(actor).catch(() => undefined)
    }
  }
  const contexts = new Set(
    [admin, tester, ...actors]
      .map((actor) => actor?.context)
      .filter(Boolean),
  )
  await Promise.allSettled([...contexts].map((context) => context.close()))
  if (browser) await browser.close().catch(() => undefined)
  lifecycle.close()
}

function requiredEnvironment(name) {
  const value = process.env[name]?.trim()
  if (!value) throw new Error(`${name} is required.`)
  return value
}

function progress(message) {
  process.stdout.write(`[system-e2e] ${message}\n`)
}

function sanitizeSystemReport(value, key = '') {
  if (/(?:authorization|cookie|credential|csrf|lease|password|secret|token)/i.test(key)) {
    return '[REDACTED]'
  }
  if (typeof value === 'string') {
    return [adminPassword, userPassword]
      .filter((candidate) => candidate.length >= 4)
      .reduce(
        (output, candidate) => output.replaceAll(candidate, '[REDACTED]'),
        sanitizeGuestDiagnostic(value)
          .replace(/\bBearer\s+[A-Za-z0-9._~+/=-]+/gi, 'Bearer [REDACTED]'),
      )
  }
  if (Array.isArray(value)) {
    return value.map((entry) => sanitizeSystemReport(entry))
  }
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).map(([entryKey, entryValue]) => [
        entryKey,
        sanitizeSystemReport(entryValue, entryKey),
      ]),
    )
  }
  return value
}

async function exerciseGuestLinkGate({
  accountActors,
  adminActor,
  document,
  privateDocument,
  protocolVersion,
  schemaVersion,
}) {
  // Kept as a backstop, not as the decision point: the caller now
  // resolves this precondition via guestLinkGateReason before entering,
  // so reaching this line means someone bypassed that path.
  assert(
    guestLinkGateReason(baseURL, true) === null,
    'Guest-link gate requires an HTTPS base URL.',
  )
  assert(
    accountActors.length === 6
      && accountActors.every((actor) => !actor.page.isClosed()),
    'Guest-link gate must run while all six account-user pages are active.',
  )

  const links = new Map()
  const openedGuests = new Map()
  const permissions = ['view', 'comment', 'suggest', 'edit']
  const reportFragment = {
    accountBrowsers: accountActors.length,
    commentMutationDenied: false,
    guestBrowsers: 0,
    guestCommentVisibleToOwner: false,
    guestMutationWithoutNameDenied: false,
    headers: false,
    noAccountSurface: false,
    passwordRateLimit: false,
    passwordRotation: false,
    permissionDowngrade: false,
    permissionDowngradeSocketClosed: false,
    permissions: [],
    revokedSocketClosed: false,
    secureCookies: false,
    sentinelRequests: 0,
    status: 'passed',
    undoRedoConverged: false,
  }

  try {
    for (const permission of permissions) {
      links.set(
        permission,
        await createGuestLink({
          document,
          lifecycle,
          owner: adminActor,
          permission,
        }),
      )
    }
    links.set(
      'rate-limit',
      await createGuestLink({
        document,
        lifecycle,
        owner: adminActor,
        permission: 'view',
      }),
    )

    const namelessContext = await sessionFixtures.newGuestContext()
    const namelessLink = links.get('comment')
    const namelessUnlock = await sessionFixtures.guestFetch(
      namelessContext,
      'POST',
      guestUnlockPath(namelessLink),
      {
        data: { password: namelessLink.password },
        expected: [200],
        headers: { Origin: publicOrigin },
      },
    )
    assertGuestSecurityHeaders(namelessUnlock.headers)
    const namelessCsrf = await sessionFixtures.guestCsrf(namelessContext)
    const namelessMutation = await sessionFixtures.guestFetch(
      namelessContext,
      'POST',
      '/v1/editor/guest/collaboration/session',
      {
        data: {
          current_lease_token: null,
          protocol_version: protocolVersion,
          rotation_command_id: null,
          schema_version: schemaVersion,
        },
        expected: [400],
        headers: {
          Origin: publicOrigin,
          'X-Inqtrix-Guest-Csrf': namelessCsrf,
        },
      },
    )
    assert(
      namelessMutation.body?.error?.message === 'display_name_required',
      'A mutating guest obtained a collaboration session without a display name.',
    )
    reportFragment.guestMutationWithoutNameDenied = true
    await namelessContext.close()

    const rateContext = await sessionFixtures.newGuestContext()
    const rateLink = links.get('rate-limit')
    const rateStatuses = []
    for (let attempt = 0; attempt < 6; attempt += 1) {
      const denied = await sessionFixtures.guestFetch(
        rateContext,
        'POST',
        guestUnlockPath(rateLink),
        {
          data: { password: `wrong-${attempt}` },
          expected: [401, 429],
          headers: { Origin: publicOrigin },
        },
      )
      assertGuestSecurityHeaders(denied.headers)
      rateStatuses.push(denied.status)
    }
    assert(
      rateStatuses.slice(0, 5).every((status) => status === 401)
        && rateStatuses[5] === 429,
      `Guest password limiter returned the wrong sequence: ${rateStatuses.join(',')}`,
    )
    reportFragment.passwordRateLimit = true
    await rateContext.close()

    const roleGuests = await Promise.all(
      permissions.map(async (permission) => {
        const opened = await sessionFixtures.openGuestLink(
          links.get(permission),
          permission === 'view' ? null : `E2E Gast ${permission}`,
        )
        openedGuests.set(permission, opened)
        return opened
      }),
    )
    reportFragment.guestBrowsers = roleGuests.length
    reportFragment.permissions = permissions
    reportFragment.headers = roleGuests.every((guest) => guest.headersVerified)
    reportFragment.secureCookies = roleGuests.every(
      (guest) => guest.secureCookies,
    )

    for (const permission of permissions) {
      const guest = openedGuests.get(permission)
      const editable = await guest.page
        .locator('.editor-prose')
        .first()
        .getAttribute('contenteditable')
      assert(
        editable === (permission === 'view' ? 'false' : 'true'),
        `Guest permission ${permission} produced contenteditable=${editable}.`,
      )
    }

    const commentGuest = openedGuests.get('comment')
    const commentSurface = commentGuest.page.locator('.editor-prose').first()
    const deniedCommentMarker = `comment-edit-denied-${runKey}`
    await focusEnd(commentSurface.locator('p').first())
    await commentGuest.page.keyboard.type(` ${deniedCommentMarker}`, { delay: 4 })
    await commentGuest.page.waitForTimeout(500)
    assert(
      !(await commentSurface.textContent())?.includes(deniedCommentMarker),
      'Comment-only guest changed the local editor document.',
    )
    await openDocument(adminActor, document)
    assert(
      !(await adminActor.page.locator('.editor-prose').first().textContent())
        ?.includes(deniedCommentMarker),
      'Comment-only guest mutation reached the owner document.',
    )
    reportFragment.commentMutationDenied = true

    const missingCsrf = await sessionFixtures.guestFetch(
      commentGuest.context,
      'POST',
      '/v1/editor/guest/collaboration/session',
      {
        data: {},
        expected: [403],
        headers: { Origin: publicOrigin },
      },
    )
    assert(
      missingCsrf.body?.error?.message === 'CSRF-Prüfung fehlgeschlagen.',
      'Guest mutation without CSRF was not rejected.',
    )
    const wrongOrigin = await sessionFixtures.guestFetch(
      commentGuest.context,
      'POST',
      '/v1/editor/guest/collaboration/session',
      {
        data: {},
        expected: [403],
        headers: {
          Origin: 'https://untrusted.example.invalid',
          'X-Inqtrix-Guest-Csrf': commentGuest.csrf,
        },
      },
    )
    assert(
      wrongOrigin.body?.error?.message === 'Unzulässiger Ursprung.',
      'Guest mutation from an untrusted Origin was not rejected.',
    )

    const accountList = await sessionFixtures.guestFetch(
      commentGuest.context,
      'GET',
      '/v1/editor/documents?limit=200&scope=all',
      { expected: [401] },
    )
    const privateRead = await sessionFixtures.guestFetch(
      commentGuest.context,
      'GET',
      `/v1/editor/documents/${privateDocument.id}`,
      { expected: [401, 404] },
    )
    assert(
      !JSON.stringify(accountList.body).includes(privateDocument.title)
        && !JSON.stringify(privateRead.body).includes(privateDocument.title),
      'A guest response exposed account or private-document content.',
    )
    assert(
      await commentGuest.page.getByRole('button', {
        name: /Assistent|Assistant/,
      }).count() === 0,
      'The guest surface exposed an assistant action.',
    )
    reportFragment.noAccountSurface = true

    const commentSession = await sessionFixtures.guestFetch(
      commentGuest.context,
      'GET',
      '/v1/editor/guest/session',
      { expected: [200] },
    )
    const guestCommentBody = `HTTPS-Gastkommentar ${runKey}`
    const guestComment = await sessionFixtures.guestFetch(
      commentGuest.context,
      'POST',
      '/v1/editor/guest/collaboration/comments',
      {
        data: {
          anchor: {
            from: 1,
            quoteAfter: '',
            quoteBefore: '',
            relativeFrom: null,
            relativeTo: null,
            relativeVersion: 'yjs-relative-position-base64-v1',
            selectedText: 'System',
            to: 7,
          },
          body_markdown: guestCommentBody,
          command_id: randomUUID(),
          expected_revision:
            commentSession.body.document.comment_revision,
          mention_user_ids: [],
          message_id: randomUUID(),
          quote: 'System',
          thread_id: randomUUID(),
        },
        expected: [200],
        headers: {
          Origin: publicOrigin,
          'X-Inqtrix-Guest-Csrf': commentGuest.csrf,
        },
      },
    )
    assert(
      Number.isInteger(guestComment.body?.revision),
      'Guest comment did not return a durable revision.',
    )
    const ownerComments = await fetchJson(
      adminActor,
      'GET',
      `${commentPath(document.id)}?since_revision=0&status=all&limit=200`,
    )
    assert(
      JSON.stringify(ownerComments).includes(guestCommentBody),
      'The owner could not observe the guest-authored comment.',
    )
    assert(
      commentGuest.aiRequests.length === 0,
      'Guest comment activity was transmitted to an AI endpoint.',
    )
    reportFragment.guestCommentVisibleToOwner = true

    await openDocument(adminActor, document)
    const editGuest = openedGuests.get('edit')
    const editSurface = editGuest.page.locator('.editor-prose').first()
    const guestMarker = `guest-edit-${runKey}`
    await focusEnd(
      editSurface.locator('p').filter({ hasText: 'Gemeinsamer Slot:' }).first(),
    )
    await editGuest.page.keyboard.type(` ${guestMarker}`, { delay: 4 })
    await adminActor.page.waitForFunction(
      (marker) => document.querySelector('.editor-prose')?.textContent?.includes(marker),
      guestMarker,
      { timeout: 30_000 },
    )
    const undoButton = editGuest.page
      .getByRole('button', { name: /^(Rückgängig|Undo)$/ })
      .first()
    await waitUntil(
      () => undoButton.isEnabled(),
      10_000,
      'guest Undo control to become available',
    )
    await undoButton.click()
    await adminActor.page.waitForFunction(
      (marker) => !document.querySelector('.editor-prose')?.textContent?.includes(marker),
      guestMarker,
      { timeout: 30_000 },
    )
    const redoButton = editGuest.page
      .getByRole('button', { name: /^(Wiederholen|Redo)$/ })
      .first()
    await waitUntil(
      () => redoButton.isEnabled(),
      10_000,
      'guest Redo control to become available',
    )
    await redoButton.click()
    await adminActor.page.waitForFunction(
      (marker) => document.querySelector('.editor-prose')?.textContent?.includes(marker),
      guestMarker,
      { timeout: 30_000 },
    )
    reportFragment.undoRedoConverged = true

    const updatedEditLink = await updateGuestLink({
      document,
      link: links.get('edit'),
      owner: adminActor,
      permission: 'view',
    })
    links.set('edit', updatedEditLink)
    await waitUntil(
      () => editGuest.socketClosed,
      15_000,
      'downgraded guest WebSocket to close',
    )
    reportFragment.permissionDowngradeSocketClosed = true
    const downgradedSession = await sessionFixtures.guestFetch(
      editGuest.context,
      'GET',
      '/v1/editor/guest/session',
      { expected: [200] },
    )
    assert(
      downgradedSession.body?.permission === 'view',
      'Guest permission downgrade was not visible to the active session.',
    )
    const downgradedLease = await sessionFixtures.guestFetch(
      editGuest.context,
      'POST',
      '/v1/editor/guest/collaboration/session',
      {
        data: {
          current_lease_token: null,
          display_name: 'E2E Gast edit',
          protocol_version: protocolVersion,
          rotation_command_id: null,
          schema_version: schemaVersion,
        },
        expected: [200],
        headers: {
          Origin: publicOrigin,
          'X-Inqtrix-Guest-Csrf': editGuest.csrf,
        },
      },
    )
    assert(
      downgradedLease.body?.access === 'view'
        && downgradedLease.body?.initial_write_mode === 'view',
      'A downgraded guest received a write-capable replacement lease.',
    )
    reportFragment.permissionDowngrade = true

    const viewGuest = openedGuests.get('view')
    const rotated = await rotateGuestPassword({
      document,
      link: links.get('view'),
      owner: adminActor,
    })
    links.set('view', rotated)
    await sessionFixtures.guestFetch(
      viewGuest.context,
      'GET',
      '/v1/editor/guest/session',
      { expected: [401] },
    )
    const rotationContext = await sessionFixtures.newGuestContext()
    await sessionFixtures.guestFetch(
      rotationContext,
      'POST',
      guestUnlockPath(rotated),
      {
        data: { password: viewGuest.link.password },
        expected: [401],
        headers: { Origin: publicOrigin },
      },
    )
    const rotatedUnlock = await sessionFixtures.guestFetch(
      rotationContext,
      'POST',
      guestUnlockPath(rotated),
      {
        data: { password: rotated.password },
        expected: [200],
        headers: { Origin: publicOrigin },
      },
    )
    assertGuestSecurityHeaders(rotatedUnlock.headers)
    reportFragment.passwordRotation = true
    await rotationContext.close()

    const suggestGuest = openedGuests.get('suggest')
    const revoked = await revokeGuestLink({
      document,
      lifecycle,
      link: links.get('suggest'),
      owner: adminActor,
    })
    links.set('suggest', revoked)
    await sessionFixtures.guestFetch(
      suggestGuest.context,
      'GET',
      '/v1/editor/guest/session',
      { expected: [401] },
    )
    await waitUntil(
      () => suggestGuest.socketClosed,
      15_000,
      'revoked guest WebSocket to close',
    )
    const revokedDescription = await sessionFixtures.guestFetch(
      suggestGuest.context,
      'GET',
      guestDescribePath(suggestGuest.link),
      { expected: [404] },
    )
    assertGuestSecurityHeaders(revokedDescription.headers)
    reportFragment.revokedSocketClosed = true

    const sentinel = 'inqtrix-e2e-guest-token-sentinel'
    await sessionFixtures.guestFetch(
      commentGuest.context,
      'GET',
      `/s/${sentinel}`,
      { expected: [200] },
    )
    await sessionFixtures.guestFetch(
      commentGuest.context,
      'GET',
      `/v1/editor/share-links/${sentinel}`,
      { expected: [404] },
    )
    reportFragment.sentinelRequests = 2

    const summary = await fetchJson(
      adminActor,
      'GET',
      `/v1/editor/documents/${document.id}/access-summary?window=7d`,
    )
    assert(
      summary.guest_link_count >= 4
        && summary.guest_open_count >= 5
        && summary.guest_session_count >= 5,
      'Guest access statistics did not reflect the live sessions.',
    )
    return reportFragment
  } finally {
    for (const guest of openedGuests.values()) {
      await guest.page.close().catch(() => undefined)
    }
    for (const [key, link] of [...links.entries()].reverse()) {
      if (link.revoked_at !== null) continue
      await revokeGuestLink({
        document,
        lifecycle,
        link,
        owner: adminActor,
      })
        .then((revoked) => links.set(key, revoked))
        .catch(() => undefined)
    }
  }
}

async function waitUntil(predicate, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs
  while (Date.now() < deadline) {
    if (await predicate()) return
    await new Promise((resolve) => setTimeout(resolve, 100))
  }
  throw new Error(`Timed out waiting for ${label}.`)
}

async function verifyDocumentMatrix(matrix) {
  const expected = new Map([
    [matrix.admin, [matrix.strategy.id, matrix.budget.id, matrix.roadmap.id, matrix.testerStatus.id, matrix.privateDocument.id]],
    [matrix.tester, [matrix.strategy.id, matrix.testerStatus.id]],
    [matrix.temporaryActors[0], [matrix.strategy.id, matrix.user3Status.id]],
    [matrix.temporaryActors[1], [matrix.budget.id, matrix.testerStatus.id]],
    [matrix.temporaryActors[2], [matrix.roadmap.id, matrix.user3Status.id]],
    [matrix.temporaryActors[3], []],
  ])
  const matrixIds = new Set([
    matrix.strategy.id,
    matrix.budget.id,
    matrix.roadmap.id,
    matrix.testerStatus.id,
    matrix.user3Status.id,
    matrix.privateDocument.id,
  ])
  for (const [actor, allowedIds] of expected) {
    const listing = await fetchJson(actor, 'GET', '/v1/editor/documents?limit=200&scope=all')
    const visibleTestIds = listing.data
      .map((document) => document.id)
      .filter((id) => matrixIds.has(id))
      .sort()
    assert(
      JSON.stringify(visibleTestIds) === JSON.stringify([...allowedIds].sort()),
      `${actor.label} saw the wrong document matrix: ${visibleTestIds.join(', ')}`,
    )
    for (const id of matrixIds) {
      const shouldSee = allowedIds.includes(id)
      await fetchJson(actor, 'GET', `/v1/editor/documents/${id}`, {
        expected: shouldSee ? [200] : [404],
      })
      report.documentIsolationChecks += 1
    }
  }
  const testerDetail = await fetchJson(matrix.tester, 'GET', `/v1/editor/documents/${matrix.strategy.id}`)
  assert(testerDetail.access.mode === 'shared', 'Shared strategy document lost its access mode.')
  assert(testerDetail.access.owner?.id === matrix.admin.user.id, 'Shared strategy document lost owner metadata.')
  assert(testerDetail.access.permission === 'edit', 'Strategy permission is not edit for Tester.')
  const user3Detail = await fetchJson(
    matrix.temporaryActors[0],
    'GET',
    `/v1/editor/documents/${matrix.strategy.id}`,
  )
  assert(user3Detail.access.permission === 'suggest', 'Strategy permission is not suggest for Nutzer 3.')
}

async function openDocument(actor, document) {
  await actor.page.goto('/', { waitUntil: 'domcontentloaded' })
  const editorButton = actor.page.getByRole('button', { name: 'Editor', exact: true })
  await editorButton.waitFor({ state: 'visible', timeout: 20_000 })
  await editorButton.click()
  const search = actor.page.getByRole('searchbox', {
    name: /^(Dokumente suchen|Search documents)$/,
  })
  await search.waitFor({ state: 'visible', timeout: 20_000 })
  await search.fill(document.title)
  const title = actor.page.getByText(document.title, { exact: true }).first()
  try {
    await title.waitFor({ state: 'visible', timeout: 20_000 })
  } catch (error) {
    const availableTitles = await actor.page
      .locator('[data-editor-document-id] [class*="truncate"]')
      .allTextContents()
    const bodyText = (await actor.page.locator('body').innerText())
      .replaceAll(/\s+/g, ' ')
      .slice(0, 1_500)
    const serverListing = await fetchJson(
      actor,
      'GET',
      '/v1/editor/documents?limit=200&scope=all',
    )
    const serverTitles = serverListing.data
      .filter((candidate) => candidate.id.includes(runKey))
      .map((candidate) => `${candidate.title}:${candidate.id}`)
    throw new Error(
      `${actor.label} cannot see ${document.title} in the editor tree. `
      + `Visible results: ${availableTitles.join(', ') || 'none'}. `
      + `Server results: ${serverTitles.join(', ') || 'none'}. `
      + `Page text: ${bodyText}. `
      + `Browser errors: ${actor.errors.join(' | ') || 'none'}. `
      + `Cause: ${error.message}`,
    )
  }
  await title.click()
  await actor.page.locator('.editor-prose').first().waitFor({ state: 'visible', timeout: 20_000 })
  const healthyStatus = actor.page
    .locator('[data-editor-status-label]')
    .filter({ hasText: /^(Gespeichert|Saved|Schreibgeschützt|Read-only)$/ })
    .first()
  try {
    await healthyStatus.waitFor({ state: 'visible', timeout: 30_000 })
  } catch (error) {
    const labels = await actor.page.locator('[data-editor-status-label]').allTextContents()
    throw new Error(
      `${actor.label} did not reach a healthy collaboration state. `
      + `Status labels: ${labels.join(', ') || 'none'}. `
      + `Browser errors: ${actor.errors.join(' | ') || 'none'}. `
      + `Cause: ${error.message}`,
    )
  }
}

async function exerciseAccountSuggestionUndo(owner, collaborator, documentRecord) {
  const collaborationSockets = []
  let collaborationSocketCloseEvents = 0
  const trackSocket = (socket) => {
    if (new URL(socket.url()).pathname !== '/collaboration') return
    collaborationSockets.push(socket)
    socket.on('close', () => {
      collaborationSocketCloseEvents += 1
    })
  }
  collaborator.page.on('websocket', trackSocket)
  try {
    await Promise.all([
      openDocument(owner, documentRecord),
      openDocument(collaborator, documentRecord),
    ])
    await selectMode(collaborator.page, /^(Vorschlagen|Suggest)$/)
    await waitUntil(
      () => collaborationSockets.length > 0,
      10_000,
      'the collaborator account WebSocket',
    )
    const activeSocket = collaborationSockets.at(-1)
    assert(activeSocket, 'The collaborator account opened no collaboration WebSocket.')
    assert(!activeSocket.isClosed(), 'The collaborator WebSocket closed before Suggest undo.')
    const closeEventsBeforeUndo = collaborationSocketCloseEvents
    const browserErrorsBeforeUndo = collaborator.errors.length
    const surface = collaborator.page.locator('.editor-prose').first()
    const ownerSurface = owner.page.locator('.editor-prose').first()
    const target = surface
      .locator('p')
      .filter({ hasText: 'Gemeinsame Mehrnutzer-Planung.' })
      .first()
    const marker = `suggest-undo-${runKey}`
    const sentBeforeTyping = collaborator.durabilityTrace.sentUpdates.length

    await focusEnd(target)
    await collaborator.page.keyboard.type(` ${marker}`, { delay: 4 })
    await waitForDurableBrowserUpdate(
      collaborator,
      sentBeforeTyping,
      'account Suggest typing',
    )
    await surface
      .locator('ins[data-suggestion-id]')
      .filter({ hasText: marker })
      .first()
      .waitFor({ state: 'visible', timeout: 20_000 })
    await ownerSurface
      .locator('ins[data-suggestion-id]')
      .filter({ hasText: marker })
      .first()
      .waitFor({ state: 'visible', timeout: 20_000 })

    const sentBeforeUndo = collaborator.durabilityTrace.sentUpdates.length
    const undoButton = collaborator.page.getByRole('button', {
      name: /^(Rückgängig|Undo)$/,
    }).first()
    await waitUntil(
      () => undoButton.isEnabled(),
      10_000,
      'the account Suggest Undo control',
    )
    const undoResponsePromise = collaborator.page.waitForResponse(
      (response) => (
        response.url().includes('/patches:decide')
        && response.request().method() === 'POST'
      ),
      { timeout: 20_000 },
    )
    await undoButton.click()
    const undoResponse = await undoResponsePromise
    assert(
      undoResponse.status() === 200,
      `Account Suggest undo returned HTTP ${undoResponse.status()}.`,
    )

    for (const page of [owner.page, collaborator.page]) {
      await page.waitForFunction(
        (needle) => !document
          .querySelector('.editor-prose')
          ?.textContent
          ?.includes(needle),
        marker,
        { timeout: 30_000 },
      )
      await page
        .locator('[data-editor-status-label]')
        .filter({ hasText: /^(Gespeichert|Saved)$/ })
        .first()
        .waitFor({ state: 'visible', timeout: 30_000 })
    }

    const outboundUndoUpdates =
      collaborator.durabilityTrace.sentUpdates.length - sentBeforeUndo
    assert(
      outboundUndoUpdates === 0,
      `Account Suggest undo emitted ${outboundUndoUpdates} raw Yjs update(s).`,
    )
    assert(
      collaborationSocketCloseEvents === closeEventsBeforeUndo
        && !activeSocket.isClosed(),
      'Account Suggest undo closed or replaced the collaboration WebSocket.',
    )
    const undoCloseEvents =
      collaborationSocketCloseEvents - closeEventsBeforeUndo
    assert(
      collaborator.errors.length === browserErrorsBeforeUndo,
      `Account Suggest undo produced browser errors: ${
        collaborator.errors.slice(browserErrorsBeforeUndo).join(' | ')
      }`,
    )
    const detail = await fetchJson(
      owner,
      'GET',
      `/v1/editor/documents/${documentRecord.id}`,
    )
    assert(
      !detail.content_markdown.includes(marker),
      'The authoritative document projection retained the rejected Undo marker.',
    )

    await collaborator.page.reload({ waitUntil: 'domcontentloaded' })
    await openDocument(collaborator, documentRecord)
    assert(
      !(await collaborator.page.locator('.editor-prose').first().innerText())
        .includes(marker),
      'The rejected Undo marker returned after collaborator reload.',
    )
    assert(
      !(await ownerSurface.innerText()).includes(marker),
      'The rejected Undo marker returned in the owner session.',
    )
    report.suggestionUndo = {
      closeEvents: undoCloseEvents,
      outboundUndoUpdates,
      status: 'passed',
    }
  } finally {
    collaborator.page.off('websocket', trackSocket)
  }
}

async function assertRolePresentation(allActors) {
  for (const actor of allActors.slice(0, 3)) {
    await selectMode(actor.page, /^(Bearbeiten|Edit)$/)
    assert(
      await actor.page.locator('.editor-prose').first().getAttribute('contenteditable') === 'true',
      `${actor.label} edit surface is not editable.`,
    )
  }
  for (const actor of allActors.slice(3, 5)) {
    await selectMode(actor.page, /^(Vorschlagen|Suggest)$/)
  }
  assert(
    await allActors[5].page.locator('.editor-prose').first().getAttribute('contenteditable') === 'false',
    'View-only actor received an editable surface.',
  )
}

async function selectMode(page, label) {
  const button = page.getByRole('button', { name: label, exact: true }).first()
  await button.waitFor({ state: 'visible' })
  if (await button.isEnabled()) await button.click()
  await page.waitForFunction(
    (pattern) => Array.from(document.querySelectorAll('button')).some(
      (candidate) => new RegExp(pattern).test(candidate.textContent?.trim() ?? '')
        && candidate.getAttribute('aria-pressed') === 'true',
    ),
    label.source,
  )
}

async function exerciseLiveSlashMatrix(matrixActors, schemaVersion) {
  const requestedAction = process.env.INQTRIX_E2E_SLASH_ACTION?.trim()
  const selectedActions = requestedAction
    ? liveSlashActions.filter((action) => action.id === requestedAction)
    : liveSlashActions
  assert(
    selectedActions.length > 0,
    `Unknown INQTRIX_E2E_SLASH_ACTION: ${requestedAction}`,
  )
  assert(matrixActors.length > 0, 'Slash matrix requires at least one actor.')
  for (const [actionIndex, action] of selectedActions.entries()) {
    // Spread the synthetic document-open burst over the isolated users. A
    // single actor would otherwise exceed the production session-issuance
    // limit before the full command matrix reaches its recovery assertions.
    const actor = matrixActors[actionIndex % matrixActors.length]
    progress(`Slash Bearbeiten: ${action.label} (${actor.label})`)
    const document = await createCollaborationDocument({
      lifecycle,
      markdown: `# Slash Edit ${action.label}\n\nSlash target`,
      owner: actor,
      runId: runKey,
      schemaVersion,
      title: `Slash Edit ${action.id}`,
    })
    documents.push(document)
    await openDocument(actor, document)
    await selectMode(actor.page, /^(Bearbeiten|Edit)$/)
    const surface = actor.page.locator('.editor-prose').first()
    const target = surface.locator('p').filter({ hasText: 'Slash target' }).first()
    const beforeCount = await surface.locator(action.selector).count()
    const beforeSent = actor.durabilityTrace.sentUpdates.length
    await focusEnd(target)
    await actor.page.keyboard.press('Enter')

    if (action.id === 'heading2') {
      await actor.page.keyboard.type('/h2')
      const menu = actor.page.locator('[data-editor-command-menu]')
      await menu.waitFor({ state: 'visible' })
      const searched = menu.getByRole('button', {
        name: action.label,
      })
      await searched.waitFor({ state: 'visible' })
      await actor.page.keyboard.press('Enter')
    } else {
      await actor.page.keyboard.type('/')
      const menu = actor.page.locator('[data-editor-command-menu]')
      await menu.waitFor({ state: 'visible' })
      const item = menu.getByRole('button', {
        name: action.label,
      })
      await item.waitFor({ state: 'visible' })
      if (action.id === 'paragraph') {
        for (const menuAction of liveSlashActions) {
          const menuItem = menu.getByRole('button', {
            name: menuAction.label,
          })
          assert(
            await menuItem.count() === 1 && await menuItem.isEnabled(),
            `${menuAction.label} is missing or disabled in the live edit menu.`,
          )
        }
        await actor.page.keyboard.press('Escape')
        await item.waitFor({ state: 'hidden' })
        await actor.page.keyboard.press('Backspace')
        await actor.page.keyboard.type('/')
        await item.waitFor({ state: 'visible' })
      }
      assert(await item.isEnabled(), `${action.label} is disabled in live edit mode.`)
      await item.click()
    }

    const focusState = await actor.page.evaluate(() => {
      const selection = window.getSelection()
      const node = selection?.anchorNode
      const parent = node instanceof Element ? node : node?.parentElement
      return {
        activeClass: document.activeElement?.getAttribute('class'),
        activeTag: document.activeElement?.tagName,
        selectionParent: parent?.tagName,
      }
    })
    progress(`Slash-Fokus ${action.id}: ${JSON.stringify(focusState)}`)
    const marker = `slash-${action.id}-${runKey}`
    await actor.page.keyboard.type(` ${marker}`, { delay: 2 })
    const typedState = await actor.page.evaluate(() => {
      const surface = document.querySelector('.editor-prose')
      const selection = window.getSelection()
      const node = selection?.anchorNode
      const parent = node instanceof Element ? node : node?.parentElement
      return {
        anchorConnected: node?.isConnected,
        anchorOffset: selection?.anchorOffset,
        contentEditable: surface?.getAttribute('contenteditable'),
        parentHtml: parent?.outerHTML.slice(0, 500),
        tail: surface?.textContent?.slice(-300),
      }
    })
    progress(`Slash-Eingabe ${action.id}: ${JSON.stringify(typedState)}`)
    await actor.page.waitForFunction(
      (needle) => document.querySelector('.editor-prose')?.textContent?.includes(needle),
      marker,
      { timeout: 20_000 },
    )
    const minimumCount = beforeCount + 1
    await actor.page.waitForFunction(
      ({ minimum, selector }) => (
        document.querySelector('.editor-prose')?.querySelectorAll(selector).length ?? 0
      ) >= minimum,
      { minimum: minimumCount, selector: action.selector },
      { timeout: 20_000 },
    )
    await waitForDurableBrowserUpdate(actor, beforeSent, `slash edit ${action.id}`)

    await openDocument(actor, document)
    const reloadedSurface = actor.page.locator('.editor-prose').first()
    assert(
      await reloadedSurface.locator(action.selector).count() >= minimumCount,
      `${action.label} did not survive an authoritative reload.`,
    )
    await reloadedSurface.getByText(marker, { exact: false }).waitFor({
      state: 'visible',
      timeout: 20_000,
    })
    report.slashMatrix.edit.push(action.id)
  }

  progress('Neun reversible Slash-Strukturvorschläge live annehmen und ablehnen')
  const suggestActions = selectedActions.filter(
    (candidate) => !['table', 'divider'].includes(candidate.id),
  )
  for (const [actionIndex, action] of suggestActions.entries()) {
    const actor = matrixActors[(actionIndex + selectedActions.length) % matrixActors.length]
    const source = action.id === 'paragraph'
      ? `# Slash Suggest ${action.label}\n\n## Suggest target`
      : `# Slash Suggest ${action.label}\n\nSuggest target`
    const document = await createCollaborationDocument({
      lifecycle,
      markdown: source,
      owner: actor,
      runId: runKey,
      schemaVersion,
      title: `Slash Suggest ${action.id}`,
    })
    documents.push(document)
    await openDocument(actor, document)
    await selectMode(actor.page, /^(Vorschlagen|Suggest)$/)
    const surface = actor.page.locator('.editor-prose').first()
    const originalSelector = action.id === 'paragraph' ? 'h2' : 'p'
    const finalBaseline = await surface.locator(action.selector).count()

    await createLiveStructureSuggestion(actor, action, originalSelector)
    await decideOnlyLiveChange(actor, 'reject')
    await actor.page.waitForFunction(
      ({ actionId, original }) => (
        document.querySelector(
          `.editor-prose [data-review-structure-action="${actionId}"]`,
        ) === null
        && document.querySelector(`.editor-prose ${original}`) !== null
      ),
      { actionId: action.id, original: originalSelector },
      { timeout: 20_000 },
    )

    await createLiveStructureSuggestion(actor, action, originalSelector)
    await decideOnlyLiveChange(actor, 'accept')
    await actor.page.waitForFunction(
      ({ minimum, selector }) => (
        document.querySelector('.editor-prose')?.querySelectorAll(selector).length ?? 0
      ) > minimum,
      { minimum: finalBaseline, selector: action.selector },
      { timeout: 20_000 },
    )
    await openDocument(actor, document)
    assert(
      await actor.page.locator('.editor-prose').first().locator(action.selector).count()
        > finalBaseline,
      `${action.label} accept did not survive an authoritative reload.`,
    )
    report.slashMatrix.suggest.push({
      decisions: ['reject', 'accept'],
      id: action.id,
    })
  }

  const actor = matrixActors[0]
  const guardDocument = await createCollaborationDocument({
    lifecycle,
    markdown: '# Slash Suggest Guards\n\nGuard target',
    owner: actor,
    runId: runKey,
    schemaVersion,
    title: 'Slash Suggest Guards',
  })
  documents.push(guardDocument)
  await openDocument(actor, guardDocument)
  await selectMode(actor.page, /^(Vorschlagen|Suggest)$/)
  await focusStart(
    actor.page
      .locator('.editor-prose')
      .first()
      .locator('p')
      .filter({ hasText: 'Guard target' })
      .first(),
  )
  await actor.page.keyboard.type('/')
  const menu = actor.page.locator('[data-editor-command-menu]')
  await menu.waitFor({ state: 'visible' })
  for (const action of liveSlashActions) {
    const item = action.id === 'table' || action.id === 'divider'
      ? menu.getByRole('button', { name: new RegExp(`^${action.label}`) })
      : menu.getByRole('button', { name: action.label })
    assert(await item.count() === 1, `${action.label} is missing in live suggest mode.`)
    const blocked = action.id === 'table' || action.id === 'divider'
    assert(
      await item.isEnabled() === !blocked,
      `${action.label} has the wrong live suggest-mode availability.`,
    )
    if (blocked) {
      assert(
        (await item.innerText()).includes('Nur im Modus Bearbeiten verfügbar'),
        `${action.label} does not explain its suggest-mode restriction.`,
      )
      report.slashMatrix.suggestBlocked.push(action.id)
    }
  }
  await actor.page.keyboard.press('Escape')
}

async function createLiveStructureSuggestion(actor, action, sourceSelector) {
  const surface = actor.page.locator('.editor-prose').first()
  const target = surface
    .locator(sourceSelector)
    .filter({ hasText: 'Suggest target' })
    .first()
  const beforeSent = actor.durabilityTrace.sentUpdates.length
  await focusStart(target)
  await actor.page.keyboard.type('/')
  const menu = actor.page.locator('[data-editor-command-menu]')
  await menu.waitFor({ state: 'visible' })
  const item = menu.getByRole('button', {
    name: action.label,
  })
  await item.waitFor({ state: 'visible' })
  assert(await item.count() === 1, `${action.label} is ambiguous in live suggest mode.`)
  assert(await item.isEnabled(), `${action.label} is disabled in live suggest mode.`)
  await item.dispatchEvent('mousedown', {
    button: 0,
    buttons: 1,
  })
  await surface
    .locator(`[data-review-structure-action="${action.id}"]`)
    .waitFor({ state: 'visible', timeout: 20_000 })
  await waitForDurableBrowserUpdate(
    actor,
    beforeSent,
    `slash suggestion ${action.id}`,
  )
}

async function decideOnlyLiveChange(actor, decision) {
  const page = actor.page
  const changes = page.getByRole('tab', { name: /^(Änderungen|Changes)/ }).first()
  await changes.click()
  const card = page.locator('[data-inspector-change-id]').first()
  await card.waitFor({ state: 'visible', timeout: 20_000 })
  const cardId = await card.getAttribute('data-inspector-change-id')
  assert(cardId, 'The live slash suggestion has no stable change id.')
  await card.locator('button').first().click()
  const action = card.getByRole('button', {
    name: decision === 'accept'
      ? /^(Annehmen|Accept)$/
      : /^(Ablehnen|Reject)$/,
  })
  await action.waitFor({ state: 'visible', timeout: 20_000 })
  await page.waitForFunction(
    ({ cardId: expectedCardId, decisionKind }) => {
      const current = Array.from(
        document.querySelectorAll('[data-inspector-change-id]'),
      ).find((candidate) => (
        candidate.getAttribute('data-inspector-change-id') === expectedCardId
      ))
      const label = decisionKind === 'accept' ? 'Annehmen' : 'Ablehnen'
      const fallback = decisionKind === 'accept' ? 'Accept' : 'Reject'
      const button = current?.querySelector(
        `button[aria-label="${label}"], button[aria-label="${fallback}"]`,
      )
      return button instanceof HTMLButtonElement && !button.disabled
    },
    { cardId, decisionKind: decision },
    { timeout: 20_000 },
  )
  const [response] = await Promise.all([
    page.waitForResponse((candidate) => (
      candidate.url().includes('/patches:decide')
      && candidate.request().method() === 'POST'
    )),
    action.click(),
  ])
  assert(
    response.status() === 200,
    `Live slash ${decision} returned HTTP ${response.status()}.`,
  )
  await page.waitForFunction(
    (expectedCardId) => !Array.from(
      document.querySelectorAll('[data-inspector-change-id]'),
    ).some((candidate) => (
      candidate.getAttribute('data-inspector-change-id') === expectedCardId
    )),
    cardId,
    { timeout: 20_000 },
  )
}

async function waitForDurableBrowserUpdate(actor, beforeSent, context) {
  const deadline = Date.now() + 20_000
  let observedLength = actor.durabilityTrace.sentUpdates.length
  let stableSince = Date.now()
  while (Date.now() < deadline) {
    const currentLength = actor.durabilityTrace.sentUpdates.length
    if (currentLength !== observedLength) {
      observedLength = currentLength
      stableSince = Date.now()
    }
    if (currentLength > beforeSent && Date.now() - stableSince >= 250) break
    await new Promise((resolve) => setTimeout(resolve, 25))
  }
  assert(
    actor.durabilityTrace.sentUpdates.length > beforeSent,
    `${context} emitted no collaboration update.`,
  )
  const updates = actor.durabilityTrace.sentUpdates.slice(beforeSent)
  while (
    !updates.every((hash) => actor.durabilityTrace.acknowledgements.includes(hash))
    && Date.now() < deadline
  ) {
    await new Promise((resolve) => setTimeout(resolve, 25))
  }
  if (!updates.every(
    (hash) => actor.durabilityTrace.acknowledgements.includes(hash),
  )) {
    const rejected = updates
      .filter((hash) => !actor.durabilityTrace.acknowledgements.includes(hash))
      .map((hash) => ({
        hash: hash.slice(0, 12),
        update: summarizeYjsUpdate(actor.durabilityTrace.updatePayloads.get(hash)),
      }))
    throw new Error(
      `${context} did not receive a durable acknowledgement: `
      + JSON.stringify(rejected),
    )
  }
}

function summarizeYjsUpdate(update) {
  if (!(update instanceof Uint8Array)) return { error: 'missing_payload' }
  try {
    const decoded = Y.decodeUpdate(update)
    return {
      deleteSet: [...decoded.ds.clients].map(([client, ranges]) => ({
        client,
        ranges: ranges.map(({ clock, len }) => ({ clock, len })),
      })),
      structs: decoded.structs.slice(-24).map((struct) => ({
        anyValueKeys: struct.content instanceof Y.ContentAny
          ? struct.content.getContent().map((value) => (
              value && typeof value === 'object' && !Array.isArray(value)
                ? Object.keys(value).sort()
                : typeof value
            ))
          : undefined,
        client: struct.id.client,
        clock: struct.id.clock,
        content: struct.content.constructor.name,
        formatKey: struct.content instanceof Y.ContentFormat
          ? struct.content.key
          : undefined,
        length: struct.length,
        parentKind: typeof struct.parent,
        parentSub: struct instanceof Y.Item ? struct.parentSub : undefined,
      })),
    }
  } catch (error) {
    return {
      error: error instanceof Error ? error.message : 'decode_failed',
      length: update.byteLength,
    }
  }
}

async function exerciseConcurrentEditing(allActors) {
  const writers = allActors.slice(0, 5)
  const markers = writers.map((actor, index) => `${runKey}-${index + 1}-${actor.label.replaceAll(' ', '-')}`)
  await Promise.all(writers.map(async (actor, index) => {
    const surface = actor.page.locator('.editor-prose').first()
    const slot = surface.locator('p').filter({ hasText: `Slot ${index + 1}:` }).first()
    await slot.waitFor({ state: 'visible' })
    await focusEnd(slot)
    await actor.page.keyboard.type(` ${markers[index]}`, { delay: 4 })
  }))
  for (const actor of allActors) {
    try {
      await actor.page.waitForFunction(
        (needles) => needles.every((needle) => document.querySelector('.editor-prose')?.textContent?.includes(needle)),
        markers,
        { timeout: 30_000 },
      )
    } catch (error) {
      const text = (await actor.page.locator('.editor-prose').first().innerText())
        .replaceAll(/\s+/g, ' ')
        .slice(-1_500)
      const missing = markers.filter((marker) => !text.includes(marker))
      const statuses = await actor.page.locator('[data-editor-status-label]').allTextContents()
      throw new Error(
        `${actor.label} did not converge after concurrent editing. `
        + `Missing markers: ${missing.join(', ') || 'none'}. `
        + `Statuses: ${statuses.join(', ') || 'none'}. `
        + `Document tail: ${text}. `
        + `Browser errors: ${actor.errors.join(' | ') || 'none'}. `
        + `Cause: ${error.message}`,
      )
    }
  }

  const samePositionEditors = allActors.slice(0, 3)
  const samePositionMarkers = ['①', '②', '③']
  await Promise.all(samePositionEditors.map(async (actor) => {
    const sharedSlot = actor.page
      .locator('.editor-prose')
      .first()
      .locator('p')
      .filter({ hasText: 'Gemeinsamer Slot:' })
      .first()
    await focusEnd(sharedSlot)
  }))
  await Promise.all(samePositionEditors.map((actor, index) => (
    actor.page.keyboard.insertText(samePositionMarkers[index])
  )))
  for (const actor of allActors) {
    await actor.page.waitForFunction(
      (needles) => needles.every(
        (needle) => document.querySelector('.editor-prose')?.textContent?.includes(needle),
      ),
      samePositionMarkers,
      { timeout: 30_000 },
    )
  }

  const owner = allActors[0]
  const changes = owner.page.getByRole('tab', { name: /^(Änderungen|Changes)/ }).first()
  await changes.click()
  const firstChange = owner.page.locator('[data-inspector-change-id]').first()
  await firstChange.waitFor({ state: 'visible', timeout: 20_000 })
  const firstChangeId = await firstChange.getAttribute('data-inspector-change-id')
  assert(firstChangeId, 'The first suggestion card has no stable change id.')
  await firstChange.locator('button').first().click()
  const accept = firstChange.getByRole('button', {
    name: /^(Annehmen|Accept)$/,
  })
  await accept.waitFor({
    state: 'visible',
    timeout: 20_000,
  })
  try {
    await owner.page.waitForFunction(
      (changeId) => {
        const card = Array.from(
          document.querySelectorAll('[data-inspector-change-id]'),
        ).find((candidate) => (
          candidate.getAttribute('data-inspector-change-id') === changeId
        ))
        const button = card?.querySelector(
          'button[aria-label="Annehmen"], button[aria-label="Accept"]',
        )
        return button instanceof HTMLButtonElement && !button.disabled
      },
      firstChangeId,
      { timeout: 20_000 },
    )
  } catch (error) {
    const status = await owner.page.locator(
      '[data-editor-status-label]',
    ).allTextContents()
    const cardText = await firstChange.innerText()
    throw new Error(
      `Accept decision stayed disabled. Status: ${status.join(' | ') || 'none'}. `
      + `Card: ${cardText.replaceAll(/\s+/g, ' ')}. Cause: ${error.message}`,
    )
  }
  const decisionRequests = []
  const trackDecisionRequest = (request) => {
    const path = new URL(request.url()).pathname
    if (
      path.includes('/collaboration')
      || path.includes('/projection')
      || path.includes('/patches')
    ) {
      decisionRequests.push(`${request.method()} ${path}`)
    }
  }
  owner.page.on('request', trackDecisionRequest)
  const acceptResponsePromise = owner.page.waitForResponse(
    (response) => (
      response.url().includes('/patches:decide')
      && response.request().method() === 'POST'
    ),
    { timeout: 10_000 },
  ).catch(() => null)
  await accept.click()
  const acceptResponse = await acceptResponsePromise
  owner.page.off('request', trackDecisionRequest)
  if (!acceptResponse) {
    const acceptEnabledAfterClick = await accept.isEnabled()
    const status = await owner.page.locator(
      '[data-editor-status-label]',
    ).allTextContents()
    const inspectorText = await owner.page
      .locator('.inqtrix-contained-panel')
      .last()
      .innerText()
      .catch(() => '')
    throw new Error(
      `Accept decision emitted no server command. `
      + `Accept enabled after click: ${acceptEnabledAfterClick}. `
      + `Status: ${status.join(' | ') || 'none'}. `
      + `Requests: ${decisionRequests.join(' | ') || 'none'}. `
      + `Durability: ${summarizeDurabilityTrace(owner)}. `
      + `Inspector: ${inspectorText.replaceAll(/\s+/g, ' ').slice(0, 2_000) || 'none'}. `
      + `Browser errors: ${owner.errors.join(' | ') || 'none'}.`,
    )
  }
  assert(
    acceptResponse.status() === 200,
    `Accept decision returned ${acceptResponse.status()}.`,
  )
  await owner.page.waitForFunction(
    (changeId) => !Array.from(document.querySelectorAll('[data-inspector-change-id]')).some(
      (candidate) => candidate.getAttribute('data-inspector-change-id') === changeId,
    ),
    firstChangeId,
    { timeout: 30_000 },
  )
  const nextChange = owner.page.locator('[data-inspector-change-id]').first()
  await nextChange.waitFor({ state: 'visible', timeout: 20_000 })
  const nextChangeId = await nextChange.getAttribute('data-inspector-change-id')
  assert(nextChangeId, 'The next suggestion card has no stable change id.')
  await nextChange.locator('button').first().click()
  const reject = nextChange.getByRole('button', {
    name: /^(Ablehnen|Reject)$/,
  })
  await reject.waitFor({ state: 'visible' })
  const [rejectResponse] = await Promise.all([
    owner.page.waitForResponse((response) => (
      response.url().includes('/patches:decide')
      && response.request().method() === 'POST'
    )),
    reject.click(),
  ])
  assert(
    rejectResponse.status() === 200,
    `Reject decision returned ${rejectResponse.status()}.`,
  )
  await owner.page.waitForFunction(
    (changeId) => !Array.from(document.querySelectorAll('[data-inspector-change-id]')).some(
      (candidate) => candidate.getAttribute('data-inspector-change-id') === changeId,
    ),
    nextChangeId,
    { timeout: 30_000 },
  )
  const displayControl = owner.page.locator(
    '[aria-label="Anzeige"], [aria-label="Display"]',
  )
  assert(
    await displayControl.count() === 1,
    'The tracked-changes display selector is not uniquely available.',
  )
  for (const mode of [
    /^(Einfach|Simple)$/,
    /^(Alle|All)$/,
    /^(Final)$/,
    /^(Original)$/,
  ]) {
    const button = displayControl.getByRole('button', { name: mode })
    assert(
      await button.count() === 1,
      `Review display mode ${mode.source} is not uniquely available.`,
    )
    await button.click()
    assert(
      await button.getAttribute('aria-pressed') === 'true',
      `Review display mode ${mode.source} did not become active.`,
    )
  }

  const headerActions = await owner.page.locator(
    '[data-editor-topbar-actions]',
  ).evaluate((actions) => {
    const buttons = Array.from(actions.querySelectorAll('button')).filter((button) => {
      const style = getComputedStyle(button)
      const box = button.getBoundingClientRect()
      return style.visibility !== 'hidden'
        && style.display !== 'none'
        && box.width > 1
        && box.height > 1
    })
    return {
      count: buttons.length,
      controls: buttons.map((button) => button.getAttribute('aria-controls')),
      overflow: buttons.map((button) => button.hasAttribute('data-editor-topbar-overflow')),
      positions: buttons.map((button) => button.getBoundingClientRect().left),
    }
  })
  assert(
    headerActions.count === 2
      && headerActions.overflow[0] === true
      && headerActions.controls[1] === 'editor-comments-panel'
      && headerActions.positions[0] < headerActions.positions[1],
    `Editor actions must contain only the overflow menu immediately left of `
      + `the inspector toggle: ${JSON.stringify(headerActions)}`,
  )

  const overlap = await owner.page.locator('[data-editor-topbar]').evaluate((topbar) => {
    const controls = Array.from(topbar.querySelectorAll('button')).filter((button) => {
      const style = getComputedStyle(button)
      const box = button.getBoundingClientRect()
      return style.visibility !== 'hidden' && style.display !== 'none' && box.width > 1 && box.height > 1
    })
    for (let left = 0; left < controls.length; left += 1) {
      const a = controls[left].getBoundingClientRect()
      for (let right = left + 1; right < controls.length; right += 1) {
        const b = controls[right].getBoundingClientRect()
        if (
          Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1
          && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1
        ) return true
      }
    }
    return false
  })
  assert(!overlap, 'Editor topbar controls overlap at 1440px.')
}

function parseCollaborationFrame(payload) {
  if (typeof payload === 'string') return null
  try {
    const bytes = new Uint8Array(payload)
    const cursor = { offset: 0 }
    readFrameString(bytes, cursor)
    const messageType = readFrameVarUint(bytes, cursor)
    if (messageType === 0) {
      const syncType = readFrameVarUint(bytes, cursor)
      if (syncType !== 2) return null
      const update = readFrameBytes(bytes, cursor)
      return {
        hash: createHash('sha256').update(update).digest('hex'),
        kind: 'update',
        update,
      }
    }
    if (messageType !== 5) return null
    const value = JSON.parse(readFrameString(bytes, cursor))
    if (
      value?.type === 'durable_ack'
      && typeof value.hash === 'string'
    ) {
      return { hash: value.hash, kind: 'durable_ack' }
    }
  } catch {
    return null
  }
  return null
}

function readFrameBytes(bytes, cursor) {
  const length = readFrameVarUint(bytes, cursor)
  const end = cursor.offset + length
  if (end > bytes.length) throw new Error('Invalid collaboration frame.')
  const value = bytes.slice(cursor.offset, end)
  cursor.offset = end
  return value
}

function readFrameString(bytes, cursor) {
  return new TextDecoder().decode(readFrameBytes(bytes, cursor))
}

function readFrameVarUint(bytes, cursor) {
  let value = 0
  let shift = 0
  while (cursor.offset < bytes.length && shift <= 49) {
    const byte = bytes[cursor.offset]
    cursor.offset += 1
    value += (byte & 0x7f) * (2 ** shift)
    if ((byte & 0x80) === 0) return value
    shift += 7
  }
  throw new Error('Invalid collaboration frame.')
}

function summarizeDurabilityTrace(actor) {
  const sent = actor.durabilityTrace.sentUpdates
  const acknowledged = actor.durabilityTrace.acknowledgements
  const remaining = [...sent]
  for (const hash of acknowledged) {
    const index = remaining.indexOf(hash)
    if (index >= 0) remaining.splice(index, 1)
  }
  return [
    `sent=${sent.length}`,
    `acked=${acknowledged.length}`,
    `unmatched=${remaining.length}`,
    `sent_tail=${sent.slice(-4).map((hash) => hash.slice(0, 10)).join(',') || 'none'}`,
    `ack_tail=${acknowledged.slice(-4).map((hash) => hash.slice(0, 10)).join(',') || 'none'}`,
  ].join(' ')
}

async function focusEnd(locator) {
  await locator.evaluate((element) => {
    const range = document.createRange()
    range.selectNodeContents(element)
    range.collapse(false)
    const selection = window.getSelection()
    selection?.removeAllRanges()
    selection?.addRange(range)
    element.focus()
    document.dispatchEvent(new Event('selectionchange'))
  })
}

async function focusStart(locator) {
  await locator.evaluate((element) => {
    const range = document.createRange()
    range.selectNodeContents(element)
    range.collapse(true)
    const selection = window.getSelection()
    selection?.removeAllRanges()
    selection?.addRange(range)
    element.focus()
    document.dispatchEvent(new Event('selectionchange'))
  })
}

function commentPath(documentId) {
  return `/v1/editor/documents/${documentId}/collaboration/comments`
}

function mutationCommand(document, expectedRevision) {
  return {
    command_id: randomUUID(),
    expected_revision: expectedRevision,
    generation: document.generation,
  }
}

function commentCommand(document, {
  body,
  expectedRevision,
  mentions = [],
  orphaned = false,
  threadId = randomUUID(),
}) {
  return {
    anchor: {
      from: orphaned ? 999_999 : 1,
      quoteAfter: '',
      quoteBefore: '',
      relativeFrom: orphaned ? 'orphaned-relative-from' : null,
      relativeTo: orphaned ? 'orphaned-relative-to' : null,
      relativeVersion: 'yjs-relative-position-base64-v1',
      selectedText: orphaned ? 'Nicht mehr vorhandener Text' : 'System',
      to: orphaned ? 1_000_010 : 7,
    },
    body_markdown: body,
    command_id: randomUUID(),
    expected_revision: expectedRevision,
    generation: document.generation,
    mention_user_ids: mentions,
    message_id: randomUUID(),
    quote: orphaned ? 'Nicht mehr vorhandener Text' : 'System',
    thread_id: threadId,
  }
}

async function createComment(actor, document, options) {
  return fetchJson(actor, 'POST', commentPath(document.id), {
    data: commentCommand(document, options),
  })
}

async function replyComment(actor, document, {
  body,
  expectedRevision,
  threadId,
}) {
  return fetchJson(actor, 'POST', `${commentPath(document.id)}/${threadId}/replies`, {
    data: {
      ...mutationCommand(document, expectedRevision),
      body_markdown: body,
      mention_user_ids: [],
      message_id: randomUUID(),
    },
  })
}

async function openComments(page) {
  let tab = page.getByRole('tab', { name: /^(Kommentare|Comments)/ }).first()
  if (!await tab.isVisible().catch(() => false)) {
    const show = page.getByRole('button', {
      name: /^(Inspector einblenden|Show inspector)$/,
    }).first()
    if (await show.isVisible().catch(() => false)) await show.click()
    tab = page.getByRole('tab', { name: /^(Kommentare|Comments)/ }).first()
  }
  await tab.waitFor({ state: 'visible' })
  await tab.click()
}

async function measureCommentApi(actor, documentId) {
  // Let the final 500 ms room-event refresh and 600 ms read-state debounce
  // complete before measuring the steady-state list path. Otherwise a
  // deliberately scheduled final sync is randomly counted as API latency.
  await new Promise((resolve) => setTimeout(resolve, 1_200))
  const samples = []
  for (let index = 0; index < 20; index += 1) {
    const started = performance.now()
    const response = await fetchJson(
      actor,
      'GET',
      `${commentPath(documentId)}?since_revision=0&status=all&limit=50`,
    )
    samples.push(performance.now() - started)
    assert(response.data.length === 50, `Comment API returned ${response.data.length}, expected 50.`)
    const threadIds = response.data.map((thread) => thread.id)
    assert(
      threadIds.every((id) => typeof id === 'string' && id.length > 0),
      'Comment API returned a thread without its public identity.',
    )
    assert(
      new Set(threadIds).size === 50,
      'Comment API returned duplicate public thread identities.',
    )
    assert(response.has_more === true, 'Comment API did not advertise the second page.')
  }
  samples.sort((left, right) => left - right)
  report.apiCommentP95Ms = samples[Math.ceil(samples.length * 0.95) - 1]
  assert(report.apiCommentP95Ms < 250, `Comment API p95 is ${report.apiCommentP95Ms.toFixed(1)}ms.`)
  progress(`Kommentar-API p95: ${report.apiCommentP95Ms.toFixed(1)} ms`)
}

async function verifyCommentInspector(page) {
  const started = performance.now()
  await openComments(page)
  await page.waitForFunction(
    () => document.querySelectorAll('[data-team-comment-id]').length === 50,
    null,
    { timeout: 20_000 },
  )
  report.firstCommentPageMs = performance.now() - started
  assert(
    report.firstCommentPageMs < 750,
    `First comment page took ${report.firstCommentPageMs.toFixed(1)}ms.`,
  )
  assert(
    await page.locator('[data-team-comment-id]').count() === 50,
    'The first inspector page did not stay bounded to 50 threads.',
  )

  const selectionTarget = page.locator('[data-team-comment-id]').nth(20)
  report.commentSelectionMs = await selectionTarget.evaluate(
    async (element) => {
      const startedAt = performance.now()
      element.click()
      await new Promise((resolve) => requestAnimationFrame(() => resolve()))
      return performance.now() - startedAt
    },
  )
  assert(
    report.commentSelectionMs < 100,
    `Comment selection took ${report.commentSelectionMs.toFixed(1)}ms.`,
  )
  assert(
    await page.getByRole('textbox', { name: /^(Antworten|Reply)/ }).count() === 1,
    'The active thread must mount exactly one reply composer.',
  )
  assert(
    await page.locator(
      '.editor-team-comment-marker[data-editor-comment-count="55"]',
    ).count() === 1,
    'Colocated comment anchors were not condensed into one count marker.',
  )
  await page.getByRole('button', { name: /^(Weitere laden|Load more)/ }).click()
  await page.waitForFunction(
    () => document.querySelectorAll('[data-team-comment-id]').length === 60,
    null,
    { timeout: 20_000 },
  )
}

async function verifyResolvedFilter(page) {
  await page.getByRole('button', { name: /^(Erledigt|Resolved)/ }).first().click()
  await page.waitForFunction(
    () => document.querySelectorAll('[data-team-comment-id]').length >= 10,
    null,
    { timeout: 20_000 },
  )
  await page.getByRole('button', { name: /^(Offen|Open)/ }).first().click()
}

async function verifyDocumentDetails(ownerPage, recipientPage, documentId) {
  await ownerPage.locator(`[data-editor-document-id="${documentId}"]`).first().click()
  await ownerPage.locator('[data-editor-topbar-overflow]').click()
  await ownerPage.getByRole('menuitem', {
    name: /^(Dokument teilen|Share document)$/,
  }).click()
  const ownerDialog = ownerPage.getByRole('dialog', {
    name: /^(Dokumentdetails|Document details)$/,
  })
  await ownerDialog.waitFor()
  const ownerSearch = ownerPage.getByRole('textbox', {
    name: /^(Personen suchen|Search people)(?:\s|$)/,
  })
  await ownerPage.waitForFunction(
    (element) => document.activeElement === element,
    await ownerSearch.elementHandle(),
  )
  for (const tab of [/^(Übersicht|Overview)$/, /^(Zugriff|Access)$/, /^(Aktivität|Activity)$/]) {
    const control = ownerPage.getByRole('tab', { name: tab })
    await control.click()
  }
  const ownerShot = join(screenshotDir, `${runKey}-document-details-1440.png`)
  await ownerPage.screenshot({ fullPage: true, path: ownerShot })
  report.screenshots.push(ownerShot)
  await ownerPage.getByRole('button', { name: /^(Schließen|Close)$/ }).last().click()

  const row = recipientPage.locator(`[data-editor-document-id="${documentId}"]`).first()
  const details = row.getByRole('button', {
    name: /^(Dokumentdetails|Document details):/,
  })
  await details.focus()
  await details.press('Enter')
  const recipientDialog = recipientPage.getByRole('dialog', {
    name: /^(Dokumentdetails|Document details)$/,
  })
  await recipientDialog.waitFor()
  const overviewTab = recipientDialog.getByRole('tab', {
    name: /^(Übersicht|Overview)$/,
  })
  await recipientPage.waitForFunction(
    (element) => document.activeElement === element,
    await overviewTab.elementHandle(),
  )
  await recipientDialog.getByRole('tab', {
    name: /^(Zugriff|Access)$/,
  }).click()
  await recipientDialog.getByRole('button', {
    name: /^(Freigabe verlassen|Leave share)$/,
  }).waitFor()
  const recipientShot = join(
    screenshotDir,
    `${runKey}-recipient-document-details-1440.png`,
  )
  await recipientPage.screenshot({ fullPage: true, path: recipientShot })
  report.screenshots.push(recipientShot)

  const modalFocusables = recipientDialog.locator(
    'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])',
  )
  const firstFocusable = modalFocusables.first()
  const lastFocusable = modalFocusables.last()
  await lastFocusable.focus()
  await lastFocusable.press('Tab')
  assert(
    await firstFocusable.evaluate((element) => document.activeElement === element),
    'Tab from the final document-details control escaped the modal.',
  )
  await firstFocusable.press('Shift+Tab')
  assert(
    await lastFocusable.evaluate((element) => document.activeElement === element),
    'Shift+Tab from the first document-details control escaped the modal.',
  )

  await recipientPage.keyboard.press('Escape')
  await recipientDialog.waitFor({ state: 'detached' })
  assert(
    await details.evaluate((element) => document.activeElement === element),
    'Escape did not restore focus to the document-details trigger.',
  )

  await details.press('Enter')
  await recipientDialog.waitFor()
  await recipientPage.mouse.click(10, 10)
  await recipientDialog.waitFor({ state: 'detached' })
  const detailsHandle = await details.elementHandle()
  assert(detailsHandle, 'The document-details trigger disappeared after click-outside.')
  await recipientPage.waitForFunction(
    (element) => document.activeElement === element,
    detailsHandle,
    { timeout: 2_000 },
  )
  assert(
    await details.evaluate((element) => document.activeElement === element),
    'Click-outside did not restore focus to the document-details trigger.',
  )
}

async function verifyResponsiveLayout(page) {
  for (const width of [1920, 1440, 1280, 1024, 720]) {
    await page.setViewportSize({ width, height: 900 })
    if (width <= 720) await openComments(page)
    const geometry = await page.evaluate(() => {
      const root = document.documentElement
      const panel = document.querySelector('#editor-comments-panel')
      const topbar = document.querySelector('[data-editor-topbar]')
      return {
        documentOverflow: root.scrollWidth - root.clientWidth,
        panelOverflow: panel ? panel.scrollWidth - panel.clientWidth : 0,
        topbarOverflow: topbar ? topbar.scrollWidth - topbar.clientWidth : 0,
      }
    })
    assert(
      geometry.documentOverflow <= 1
        && geometry.panelOverflow <= 1
        && geometry.topbarOverflow <= 1,
      `Horizontal overflow at ${width}px: ${JSON.stringify(geometry)}`,
    )
    report.viewports.push({ width, ...geometry })
  }
  const mobileShot = join(screenshotDir, `${runKey}-comments-720.png`)
  await page.screenshot({ fullPage: true, path: mobileShot })
  report.screenshots.push(mobileShot)
}
