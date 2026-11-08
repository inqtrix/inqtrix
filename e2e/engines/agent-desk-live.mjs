// Agent-Desk live engine (profile: agent-desk).
//
// Drives a research conversation through the Agent Desk against the
// already-running canonical stack: submit a run-prefixed question,
// watch the live tool activity, assert the clickable-citation answer
// experience, collect console/network hygiene, and finalize resources through
// the shared cleanup or explicit review-retention contract. Results go
// through the shared scenario result writer; resources register through the
// shared product-lifecycle IPC channel.

import { access, mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

import { chromium, firefox, webkit } from '@playwright/test'

import {
  assertFixture as assert,
  fetchActorJson as fetchJson,
} from '../../tests/verification/fixtures/api.mjs'
import { attemptCleanup } from '../../tests/verification/fixtures/cleanup-step.mjs'
import { createScenarioBook, writeScenarioSidecar } from '../../tests/verification/fixtures/scenario-sidecar.mjs'
import {
  requireAccountLocale,
} from '../../tests/verification/fixtures/accounts.mjs'
import {
  agentSessionTitleForRun,
  assertVerificationRunId,
} from '../../tests/verification/fixtures/run-scope.mjs'
import {
  createSessionFixtures,
} from '../../tests/verification/fixtures/sessions.mjs'
import {
  VerificationLifecycleClient,
} from '../../tests/verification/fixtures/lifecycle-client.mjs'
import { createAgentDocumentMaterial } from '../../tests/verification/fixtures/agent-document-material.mjs'
import { httpsErrorsIgnored } from '../../tests/verification/fixtures/https-errors.mjs'

const baseURL = process.env.INQTRIX_E2E_BASE_URL ?? 'http://localhost:8080'
const ignoreHTTPSErrors = httpsErrorsIgnored()
const testerEmail = requiredEnvironment('INQTRIX_E2E_TESTER_EMAIL')
const userPassword = requiredEnvironment('INQTRIX_E2E_USER_PASSWORD')
const executablePath = process.env.PLAYWRIGHT_EXECUTABLE_PATH
const runKey = assertVerificationRunId(
  requiredEnvironment('INQTRIX_VERIFICATION_RUN_ID'),
)
const reportDirectory = requiredEnvironment('INQTRIX_VERIFICATION_REPORT_DIR')
const screenshotDirectory = join(reportDirectory, 'screenshots')
const manualConversation = process.env.INQTRIX_E2E_MANUAL_CONVERSATION === '1'
const retainForReview = process.env.INQTRIX_E2E_RETAIN_REVIEW_DATA === '1'
const reviewSeconds = Number(process.env.INQTRIX_E2E_VISUAL_REVIEW_SECONDS ?? 0)

const lifecycle = new VerificationLifecycleClient({
  reportDirectory,
  runId: runKey,
})

if (process.env.INQTRIX_VERIFICATION_PROFILE === 'agent-recovery') {
  const { runAgentRecovery } = await import('../../tests/verification/fixtures/agent-recovery.mjs')
  const exitCode = await runAgentRecovery({
    baseURL, ignoreHTTPSErrors, executablePath, lifecycle, reportDirectory,
    runKey, testerEmail, userPassword, screenshotDirectory, writeScenarioSidecar,
  })
  process.exit(exitCode)
}

const SCENARIOS = [
  'agent.kernel-run-submits',
  'agent.tool-activity-visible',
  'agent.answer-citations-clickable',
  'agent.console-network-clean',
  'agent.cleanup-integrity',
  'agent.review-matches-tier',
  'agent.conversation-followups',
  'agent.conversation-history-synthesis',
  'agent.conversation-history-canvas',
  'agent.conversation-firefox',
  'agent.conversation-webkit',
  'agent.conversation-knowledge-document',
  'agent.conversation-editor-document',
  'agent.conversation-history-material',
  'agent.conversation-file-attachment',
  'agent.conversation-review-role',
  'agent.conversation-mission-review',
  'agent.conversation-stop-continue',
  'agent.conversation-manual-title',
  'agent.conversation-canvas-copy',
]
// Every scenario starts as not measured. A measured step that fails marks
// its own scenario failed with the reason, so a run that dies early shows one
// red verdict where it broke, and the rest stay not run. The exit code treats
// anything that is not passed as a failure.
const verdicts = createScenarioBook(SCENARIOS)
// The scenario the main flow measures right now: a failure that reaches the
// main catch counts against it. Setup before the first question measures none.
let measuring = null
const networkFindings = []
let abortedWrites = 0
// The review a Stufe owes is judged per completed turn in a scenario of its
// own: an unusable review stays visible as red, and it does not hide the
// turns after it.
const reviewMismatches = []
let reviewedTurns = 0

let browser
let tester
let runId = null
let exitCode = 1
const ownedRuns = []
const conversation = []
let activeSession
let activeTitle
let documentMaterial
let publishedTiers = []
// Answers the stopped turn rendered before it ended; the replay counts them.
let stoppedTurnAnswers = 0
// A stop that did not hold may have left an answer of its own, uncounted.
let stopTurnUncertain = false
let stopTurnRunId = null

// The reason of a failure reaches the report: credentials never do.
function failScenario(id, reason) {
  verdicts.fail(id, sanitize(String(reason)))
}

try {
  assert(Number.isInteger(reviewSeconds) && reviewSeconds >= 0 && reviewSeconds <= 600,
    'Visual review hold must be between 0 and 600 seconds.')
  progress('Browser starten')
  await mkdir(screenshotDirectory, { recursive: true })
  browser = await chromium.launch({ executablePath, headless: true })
  const sessionFixtures = createSessionFixtures({
    baseURL,
    browser,
    ignoreHTTPSErrors,
    lifecycle,
    parseCollaborationFrame: () => null,
    runId: runKey,
    screenshotDirectory,
  })
  tester = await sessionFixtures.loginActor(
    testerEmail,
    userPassword,
    'AgentTester',
    'user',
  )
  // The run reads German labels; the page shows the tester's saved language.
  await requireAccountLocale(tester, 'de')
  // Network hygiene on top of the shared pageerror/console collectors.
  tester.page.on('requestfailed', (request) => {
    const failure = request.failure()?.errorText ?? 'failed'
    if (failure.includes('net::ERR_ABORTED')) {
      // Chromium reports a write as aborted when the app does not read its 204.
      // Nothing here proves that the server answered, so the count stays visible.
      if (!['GET', 'HEAD'].includes(request.method())) abortedWrites += 1
      return
    }
    networkFindings.push(`requestfailed:${failure}`)
  })
  tester.page.on('response', (response) => {
    if (response.status() >= 500) {
      networkFindings.push(`http:${response.status()}`)
    }
  })

  const capabilities = await fetchJson(tester, 'GET', '/v1/capabilities')
  assert(
    capabilities.features?.agent_kernel === true,
    'The stack does not enable the agent kernel (features.agent_kernel).',
  )
  // The kernel ships registered, so the bare boolean above does not
  // distinguish a healthy stack from a missed gate the operator never
  // saw. Read the reason the registration path published instead.
  const kernelStatus = capabilities.feature_status?.agent_kernel
  assert(
    kernelStatus?.state === 'enabled' && kernelStatus?.reason_code === null,
    'The agent kernel is not cleanly registered '
      + `(state=${kernelStatus?.state ?? 'unset'}, `
      + `reason=${kernelStatus?.reason_code ?? 'none'}).`,
  )
  // Published == enforced: the tier the composer preselects promises a
  // report-quality level, and the rounds below check that the engine
  // delivers exactly what this table says.
  publishedTiers = capabilities.agent?.tiers ?? []
  const defaultTier = publishedTiers.find(
    (tier) => tier.id === capabilities.agent?.default_tier,
  )
  assert(
    defaultTier?.verify && defaultTier.verify !== 'labels',
    'The published default tier claims no verification '
      + `(${defaultTier?.verify ?? 'unset'}); the review rounds below would `
      + 'pass vacuously.',
  )
  // The composer submits the published default engine — this profile
  // verifies the KERNEL answer experience, so a workspace_agent default
  // would silently test the wrong engine.
  assert(
    capabilities.agent?.default_mode === 'agent_kernel',
    'The stack default engine is not agent_kernel '
      + `(${capabilities.agent?.default_mode ?? 'unset'}).`,
  )
  progress('Prepare owned document originals')
  documentMaterial = await createAgentDocumentMaterial({
    actor: tester, lifecycle, runId: runKey, retainForReview, waitFor,
    schemaVersion: capabilities.collaboration?.schema_version,
  })
  await writeFile(join(reportDirectory, 'document-originals.json'), JSON.stringify(documentMaterial, null, 2), { mode: 0o600 })

  const sessionTitle = agentSessionTitleForRun(runKey)
  const session = await fetchJson(tester, 'PUT', `/v1/agent-sessions/agent-session-${runKey}`, {
    data: {
      title: sessionTitle,
      title_revision: 0,
      title_source: 'manual',
      items_json: JSON.stringify({ source_policy: { web: 'available', knowledge: 'disabled' } }),
      created_at: Date.now() / 1000,
      updated_at: Date.now() / 1000,
    },
  })
  await lifecycle.register({
    retainForReview,
    credential: 'user',
    id: session.id,
    kind: 'agent_session',
    ownerEmail: testerEmail,
    title: sessionTitle,
  })
  assert(session.title === sessionTitle, 'The test session title was not persisted.')
  activeSession = session
  activeTitle = sessionTitle

  progress('Agent Desk oeffnen')
  await tester.page.goto('/', { waitUntil: 'domcontentloaded' })
  await tester.page.getByRole('button', { name: 'Agent Desk' }).click()
  await tester.page.locator(`[data-agent-session-id="${session.id}"]`)
    .getByText(sessionTitle, { exact: true }).click()
  await tester.page.getByRole('heading', { name: sessionTitle, exact: true })
    .waitFor({ state: 'visible' })
  const composer = tester.page.getByTestId('agent-composer-input')
    .locator('[contenteditable="true"]')
  await composer.waitFor({ state: 'visible', timeout: 20_000 })
  await tester.page.locator('[data-source="web"][aria-pressed="true"]')
    .waitFor({ state: 'visible', timeout: 20_000 })

  // Research executes without intermediate approval clicks. The approval
  // workflow is covered separately by the recovery profile.
  await tester.page
    .getByRole('button', { name: 'Ausführung', exact: true })
    .click()
  await tester.page
    .getByRole('menuitem')
    .filter({ hasText: 'Zwischenfreigaben' })
    .first()
    .click()
  await tester.page.keyboard.press('Escape')

  const question = 'Wir planen einen internen KI-Arbeitsassistenten '
    + 'für ein fiktives deutsches Unternehmen mit 140 Beschäftigten. Er hilft bei Recherche und Dokumenten, '
    + 'trifft aber keine Personalentscheidungen. Für Schulung und Einführung stehen zunächst 18.000 EUR bereit; '
    + 'die IT-Leitung verantwortet die Umsetzung, die Datenschutzbeauftragte wird beteiligt. '
    + 'Recherchiere den aktuellen Stand des EU AI Act aus offiziellen Quellen. Welche Pflichten und Termine '
    + 'sind für uns relevant, was ist beschlossen und was nur vorgeschlagen? Antworte zunächst im Chat mit Quellen.'

  progress('Kernel-Lauf einreichen')
  measuring = 'agent.kernel-run-submits'
  runId = await submitTurn(question, 'R1', true)
  assert(typeof runId === 'string' && runId.length > 0, 'No run id returned.')

  verdicts.pass('agent.kernel-run-submits')
  // From here on the opening turn decides whether an answer reaches the user.
  measuring = 'agent.answer-citations-clickable'
  progress('Lauf laeuft — Aktivitaet beobachten')

  // Live tool activity: at least one activity row must appear WHILE the
  // run is active and carry a non-empty detail (the literal query).
  let activitySeen = false
  const activityProbe = (async () => {
    const row = tester.page.getByTestId('agent-activity-item').first()
    try {
      await row.waitFor({ state: 'visible', timeout: 180_000 })
      const text = ((await row.textContent()) ?? '').trim()
      activitySeen = text.length > 0
    } catch {
      activitySeen = false
    }
  })()

  const terminal = await waitFor(
    async () => {
      const summary = await fetchJson(tester, 'GET', `/v1/runs/${runId}`)
      return ['completed', 'failed', 'cancelled'].includes(summary.status)
        ? summary
        : null
    },
    600_000,
    'terminal kernel run state',
  )
  await activityProbe
  await captureTurn('R1', question, terminal)
  assert(
    terminal.status === 'completed',
    `The kernel run ended ${terminal.status}: ${runErrorText(terminal.error)}`,
  )
  // The run and every owned resource are terminal and registered before
  // answer presentation is inspected. A later UI assertion must not turn
  // this independently proven cleanup contract red.
  verdicts.pass('agent.cleanup-integrity')
  if (activitySeen) {
    verdicts.pass('agent.tool-activity-visible')
  } else {
    failScenario('agent.tool-activity-visible', 'no activity row with a detail appeared while the run was active')
  }

  progress('Antwort-Erlebnis pruefen')
  const answer = tester.page.getByTestId('agent-answer')
  await answer.waitFor({ state: 'visible', timeout: 60_000 })
  // The answer container exists from answer.started onwards. References
  // arrive with the canonical artifact-detail reconciliation after
  // answer.ready, so the source owner — not the already-visible container —
  // is the anchored readiness signal for citation assertions.
  const sources = tester.page.getByTestId('agent-sources')
  await sources.waitFor({ state: 'visible', timeout: 30_000 })
  const citationAnchors = answer.locator('a[href^="#kref-"]')
  const citationCount = await citationAnchors.count()
  assert(citationCount > 0, 'The answer rendered no citation chips.')
  await tester.page.screenshot({
    fullPage: true,
    path: join(screenshotDirectory, 'agent-answer-ready.png'),
  })
  // Clicking a citation must react visibly: a web ref opens a popup, a
  // knowledge ref opens the evidence view (canvas panel).
  const popupPromise = tester.context
    .waitForEvent('page', { timeout: 5_000 })
    .catch(() => null)
  await citationAnchors.first().click()
  const popup = await popupPromise
  if (popup) {
    await popup.close()
  } else {
    await tester.page
      .getByText(/Beleg [KW]\d+/)
      .first()
      .waitFor({ state: 'visible', timeout: 10_000 })
  }
  verdicts.pass('agent.answer-citations-clickable')
  measuring = null

  // Two session paths every later turn stands on. The user renames the
  // session by hand, and stops a running turn; the next turn (R2) then has
  // to complete in the same session under the manual title. The new title
  // keeps the run prefix, so the cleanup ledger still recognises the session.
  const failedRounds = new Set()
  try {
    progress('Sitzung von Hand umbenennen')
    await renameSession(`${sessionTitle} umbenannt`)
  } catch (error) {
    failedRounds.add('TITLE')
    failScenario('agent.conversation-manual-title', `the rename failed: ${error?.message ?? error}`)
    progress(`Umbenennen nicht bestanden: ${sanitize(String(error?.message ?? error))}`)
    await tester.page.keyboard.press('Escape').catch(() => undefined)
    await tester.page.screenshot({ fullPage: true, path: join(screenshotDirectory, 'session-rename-failure.png') })
      .catch(() => undefined)
  }
  try {
    progress('Laufenden Auftrag stoppen')
    await stopRunningTurn()
  } catch (error) {
    failedRounds.add('RS')
    stopTurnUncertain = true
    failScenario('agent.conversation-stop-continue', `the stop failed: ${error?.message ?? error}`)
    progress(`Stoppen nicht bestanden: ${sanitize(String(error?.message ?? error))}`)
    await tester.page.screenshot({ fullPage: true, path: join(screenshotDirectory, 'conversation-RS-failure.png') })
      .catch(() => undefined)
    // The next turn goes into the same session: a turn that is still running
    // must end first, or R2 would be judged next to it.
    if (stopTurnRunId) await endRun(stopTurnRunId).catch(() => undefined)
  }

  // One table for every follow-up turn. `web` is the native source policy,
  // `cites` expects a rendered citation in the answer, `context` the document
  // pin the turn needs (`none` clears the pins), `scenario` the verdict this
  // turn decides and `requires` the earlier turns that verdict also stands on.
  const rounds = [
    // R2 is the turn after the stopped one: it decides whether work goes on in the same session.
    { marker: 'R2', web: true, cites: true, scenario: 'agent.conversation-stop-continue', requires: ['RS'], prompt: 'Wie unterscheidet sich davon der aktuelle Stand der KI-Haftungsrichtlinie und der überarbeiteten Produkthaftungsrichtlinie? Recherchiere gezielt in offiziellen Quellen. Trenne beide Vorhaben und nenne den jeweiligen Stand; eine zurückgenommene Vorlage darf nicht wie geltendes Recht behandelt werden.' },
    { marker: 'R3', web: true, cites: true, scenario: 'agent.conversation-followups', requires: ['R2'], prompt: 'Was bedeutet die Pflicht zur KI-Kompetenz konkret für die Einführung unseres beschriebenen Assistenten? Prüfe die aktuellen offiziellen Hinweise. Unterscheide belegte Anforderungen von deinen praktischen Empfehlungen. Berücksichtige unsere Rollenverteilung.' },
    { marker: 'R4', scenario: 'agent.conversation-history-synthesis', prompt: 'Eine Korrektur für unsere weitere Planung: Das Einführungsbudget beträgt jetzt 12.500 EUR statt des bisherigen Betrags. Stelle die bisherigen Erkenntnisse ausschließlich aus diesem Gespräch zusammen, ohne weitere Websuche. Nenne unsere Ausgangslage, die verschiedenen Regelwerke und deren Status, die Termine, Zuständigkeiten und offene Fragen. Lies bei Bedarf frühere Rechercheergebnisse nach; erfinde keine fehlenden Details.' },
    { marker: 'R5', scenario: 'agent.conversation-history-canvas', prompt: 'Erstelle jetzt nachträglich einen eigenständigen Managementbericht im Canvas aus den Informationen unseres gesamten Gesprächs. Keine neue Websuche. Erhalte die Quellen und die Unterscheidung zwischen geltendem Recht, Vorschlägen und Empfehlungen. Übernimm die korrigierte Planung, ergänze eine übersichtliche Tabelle mit Aufgaben, Zuständigkeiten und offenen Entscheidungen. Der Bericht soll ohne den Chat verständlich sein.' },
    { marker: 'R6', context: 'knowledge', scenario: 'agent.conversation-knowledge-document', prompt: 'Ich habe jetzt unsere freigegebenen Einführungsunterlagen als Wissenssammlung ausgewählt. Lies den vollständigen Einführungsleitfaden, bevor du antwortest. Welche verbindlichen Sonderregeln gelten für den Pilotbetrieb zu Kosten, Freigabe, Betriebszeiten und späteren Erweiterungen? Trenne diesen Sachkostenrahmen vom zuvor besprochenen Einführungsbudget. Nenne die maßgebliche Fundstelle. Keine Websuche und noch keine Änderungen.' },
    { marker: 'R7', context: 'editor', scenario: 'agent.conversation-editor-document', prompt: 'Als Bearbeitungsziel ist jetzt die Arbeitsfassung des Einführungsleitfadens ausgewählt. Lies auch diese Fassung vollständig und vergleiche sie mit den eben ermittelten freigegebenen Sonderregeln. Wo weicht sie ab? Gib die abweichenden Textstellen wörtlich wieder und erkläre die nötige Korrektur. Prüfe auch mit einer gezielten Suche im Dokument. Nichts ändern und keine neue Websuche.' },
    // The materials both document turns transferred are now part of the
    // saved conversation: a later turn must reach them through the
    // conversation originals, re-authorized against current rights, without
    // opening the documents again. The question asks for body text that
    // no published answer quotes: a fact the answers of the document turns
    // already quote is found there first, and the saved originals of those
    // turns are never reached.
    { marker: 'R8', context: 'none', scenario: 'agent.conversation-history-material', prompt: 'Ohne die Dokumente erneut zu öffnen: Welche Prüfschritte nennt der freigegebene Einführungsleitfaden im Abschnitt Archivierung, und wer benennt dort eine Vertretung? Nutze nur unseren Gesprächsverlauf, gib die Stelle wörtlich wieder und nenne, aus welchem früheren Auftrag sie stammt.' },
    // The collection also holds a PDF that reached it through the file
    // library: its text is the canonical server extraction, and one of its
    // pages has no text layer. The pages do not number themselves, so
    // only the extraction facts of the reader can reveal the missing page.
    { marker: 'R9', context: 'knowledge', scenario: 'agent.conversation-file-attachment', prompt: 'In der ausgewählten Wissenssammlung liegt zusätzlich die Anlage „Prüfprotokoll Pilotbetrieb“, eine hochgeladene PDF-Datei. Lies diese Anlage vollständig und fasse sie Seite für Seite zusammen. Keine Websuche und keine Änderungen.' },
    // Deep ends with the review ROLE: a bounded child run of the kernel that
    // re-reads what this turn read and hands back a typed verdict. Both
    // documents are in the selected collection; nothing needs research.
    { marker: 'R10', context: 'knowledge', tier: 'tief', scenario: 'agent.conversation-review-role', prompt: 'Jetzt bitte besonders gründlich: Erstelle eine geprüfte Kurzübersicht zum Pilotbetrieb aus der ausgewählten Wissenssammlung. Nenne den Sachkostenrahmen und die Prüfregeln aus der abschließenden Sonderfreigabe des freigegebenen Einführungsleitfadens sowie das Abnahmeergebnis und die Nacharbeiten aus der Anlage „Prüfprotokoll Pilotbetrieb“. Dafür ist keine Recherche nötig; lies nur den Leitfaden und die Anlage. Belege jede Aussage mit der Fundstelle und nenne ausdrücklich, was sich nicht prüfen lässt. Keine Websuche und keine Änderungen.' },
    // The mission engine shares the review pass with the kernel: a typed
    // verdict that settles the plan's success criteria, anchored edits instead
    // of a rewrite, one review per synthesis. Small material is reviewed in one
    // call, large material by the review role; the turn says which it was.
    { marker: 'R11', context: 'knowledge', tier: 'gruendlich', engine: 'mission', autonomy: 'autonomous', scenario: 'agent.conversation-mission-review', prompt: 'Erstelle jetzt ein eigenständiges Memo als Entscheidungsvorlage zum Pilotbetrieb, ausschließlich aus der ausgewählten Wissenssammlung: Sachkostenrahmen und Freigaberegeln, Betriebszeiten, das Abnahmeergebnis mit den offenen Nacharbeiten und die Folgen für spätere Erweiterungen. Belege jede Aussage mit der Fundstelle und nenne offen, was die Unterlagen nicht hergeben. Keine Websuche und keine Änderungen an Dokumenten.' },
  ]
  for (const { marker, prompt, web = false, cites = false, context = null, scenario = null, requires = [], tier = null, engine = null, autonomy = null } of rounds) {
    measuring = scenario
    if (!manualConversation && context) {
      if (context === 'none') await clearDocumentContext(tester.page)
      else await selectDocumentContext(tester.page, context)
    }
    // A turn that cannot be submitted or never ends stops the conversation.
    runId = await submitTurn(prompt, marker, web, context === 'none' ? 'history' : context, { tier, engine, autonomy })
    // A Deep turn adds a review child and one revision to the turn itself.
    const state = await waitFor(async () => {
      const current = await fetchJson(tester, 'GET', `/v1/runs/${runId}`)
      return ['completed', 'failed', 'cancelled'].includes(current.status) ? current : null
    }, tier === 'tief' ? 1_200_000 : 600_000, `${marker} terminal conversation turn`)
    // A failed expectation of one turn fails its scenario and the run, but
    // it must not hide the turns after it: they are judged on their own.
    try {
      await captureTurn(marker, prompt, state)
      assert(state.status === 'completed', `${marker} ended ${state.status}: ${runErrorText(state.error)}`)
      // The conversation has several answers. An already visible older answer
      // cannot establish that this newly completed turn rendered correctly.
      const turn = answeredTurn(tester.page, state)
      await turn.getByTestId('agent-answer').waitFor({ state: 'visible', timeout: 30_000 })
      if (cites) {
        assert(await turn.getByTestId('agent-answer').locator('a[href^="#kref-"]').count() > 0,
          `${marker} answered without citing a source.`)
      }
      // The timeline is an inner scroll container: a full-page capture shows
      // whatever is scrolled into view, so the turn under review is scrolled
      // there first. The image is evidence only once it shows this turn.
      await turn.scrollIntoViewIfNeeded()
      await tester.page.screenshot({ fullPage: true, path: join(screenshotDirectory, `conversation-${marker}.png`) })
      if (marker === 'R5') {
        const artifacts = conversation.at(-1).artifacts
        assert(artifacts.some((item) => ['memo', 'deliverable'].includes(item.kind) && item.status === 'ready'),
          'The conversation produced no ready Canvas report.')
        // Whether the report CARRIES what the conversation established is a
        // content check, not a browser check - it lived in the cross-browser
        // replay, where it read rendered text in one engine. Read the stored
        // bytes instead: they are what every engine renders, and the one
        // content defect this area ever found was a model omission in exactly
        // these bytes.
        const report = conversation.at(-1).artifact_details
          .find((item) => ['memo', 'deliverable'].includes(item.kind))
        assert(report?.content_markdown, 'The saved report has no stored text.')
        assert(/12[.,\s]?500/.test(report.content_markdown),
          'R5 report omitted the revised budget from the conversation.')
        assert(/140/.test(report.content_markdown),
          'R5 report omitted the headcount from the conversation.')
      }
      const failedEarlier = requires.filter((earlier) => failedRounds.has(earlier))
      if (scenario && failedEarlier.length > 0) {
        failScenario(scenario, `stands on ${failedEarlier.join(', ')}, which failed`)
      } else if (scenario) {
        verdicts.pass(scenario)
      }
    } catch (error) {
      failedRounds.add(marker)
      if (scenario) failScenario(scenario, `${marker}: ${error?.message ?? error}`)
      progress(`${marker} nicht bestanden: ${sanitize(String(error?.message ?? error))}`)
      await tester.page.screenshot({ fullPage: true, path: join(screenshotDirectory, `conversation-${marker}-failure.png`) })
        .catch(() => undefined)
    }
    // The follow-up has ended, whatever its answer: did the manual title survive it?
    if (marker === 'R2' && !failedRounds.has('TITLE')) await judgeManualTitle()
    // The report exists once R5 ended, whatever its review said; copying it
    // right away keeps that verdict from depending on the turns after it.
    // Opening a report in the Canvas does not attach it to the next turn.
    if (marker === 'R5') await judgeCanvasCopy()
  }
  measuring = null
  // Each engine gets its own verdict. With chained awaits a Firefox probe
  // error would leave WebKit unmeasured, so the engine that is supposed to
  // be the independent second opinion would never reach one. Collect both,
  // then fail on the first.
  const replayFailures = []
  for (const [name, browserType] of [['firefox', firefox], ['webkit', webkit]]) {
    try {
      await verifyPersistedConversation(name, browserType)
    } catch (error) {
      failScenario(`agent.conversation-${name}`, error?.message ?? error)
      replayFailures.push(`${name}: ${error?.message ?? error}`)
    }
  }
  if (replayFailures.length) {
    throw new Error(`Persisted-conversation replay failed - ${replayFailures.join(' | ')}`)
  }

} catch (error) {
  await tester?.page.screenshot({
    fullPage: true,
    path: join(screenshotDirectory, 'agent-failure.png'),
  }).catch(() => undefined)
  progress(`Engine-Fehler: ${sanitize(String(error?.message ?? error))}`)
  if (measuring) failScenario(measuring, error?.message ?? error)
  exitCode = 1
} finally {
  // Keep both successes and failures inspectable before the shared owner
  // cleanup. Structural scenario verdicts never replace the content review.
  const until = Date.now() + (Number.isInteger(reviewSeconds) ? Math.min(600, Math.max(0, reviewSeconds)) : 0) * 1000
  if (Date.now() < until) progress('Results retained for in-app visual and trace review.')
  while (Date.now() < until) {
    try { await access(join(reportDirectory, 'visual-review.done')); break } catch { /* bounded review hold */ }
    await new Promise((resolveWait) => setTimeout(resolveWait, 500))
  }
  try {
    for (const id of ownedRuns) {
      // Never leave a live run behind: cancel and await the terminal
      // state so the ledger's DELETE cannot race a running row.
      await endRun(id).catch(() => {
        failScenario('agent.cleanup-integrity', `run ${id} did not end after its cancel`)
      })
    }
  } catch {
    failScenario('agent.cleanup-integrity', 'the owned runs could not be ended')
  }
  if (reviewMismatches.length > 0) {
    failScenario('agent.review-matches-tier', reviewMismatches.join(' | '))
  } else if (reviewedTurns > 0) {
    verdicts.pass('agent.review-matches-tier')
  }
  if (tester) {
    const hygieneFindings = [
      ...tester.errors,
      ...networkFindings,
    ]
    if (hygieneFindings.length === 0) verdicts.pass('agent.console-network-clean')
    else failScenario('agent.console-network-clean', `${hygieneFindings.length} console or network findings`)
    if (hygieneFindings.length > 0) {
      progress(
        `Konsole/Netzwerk nicht sauber (${hygieneFindings.length} Befunde)`,
      )
    }
  }
  exitCode = verdicts.allPassed()
    ? 0
    : 1
  progress(`Abgebrochene Schreibanfragen ohne gelesene Antwort: ${abortedWrites}`)
  // Every scenario with its status: a failure or a verdict that stands on a
  // failed step with its reason, one the run never reached as not_run.
  try {
    await writeScenarioSidecar(verdicts.entries())
  } catch (error) {
    exitCode = 1
    progress(`Ergebnisdatei nicht geschrieben: ${sanitize(String(error?.message ?? error))}`)
  }
  await browser?.close().catch(() => null)
}

process.exit(exitCode)

/** Cancel a run that is still open and wait until it has ended; an ended run is left alone. */
async function endRun(id) {
  let summary = null
  await attemptCleanup('run state', async () => {
    summary = await fetchJson(tester, 'GET', `/v1/runs/${id}`, { expected: [200, 404] })
  })
  if (!summary || !['queued', 'running', 'waiting_for_approval', 'waiting_for_input', 'waiting_for_children'].includes(summary.status)) return
  await attemptCleanup('run cancel', () => fetchJson(tester, 'POST', `/v1/runs/${id}/cancel`, {
    expected: [200, 202, 404, 409],
  }))
  await waitFor(
    async () => {
      const current = await fetchJson(tester, 'GET', `/v1/runs/${id}`)
      return ['completed', 'failed', 'cancelled'].includes(current.status) ? current : null
    },
    120_000,
    'terminal state after cancel',
  )
}

async function submitTurn(prompt, marker, webEnabled, materialKind = null, { tier = null, engine = null, autonomy = null } = {}) {
  const web = tester.page.locator('[data-source="web"]')
  if (await web.getAttribute('aria-pressed') !== String(webEnabled)) await web.click()
  await tester.page.locator(`[data-source="web"][aria-pressed="${webEnabled}"]`)
    .waitFor({ state: 'visible' })
  // A document chip in the input is the material of the task: the composer
  // spells it in the reader's quotes where it stands (`composerTextFromDoc`),
  // so the saved question names the document. Picked before typing, as the
  // scripted path does, it stands ahead of the sentence; added through the
  // "+" menu after typing, it stands at the end. A collection chip is a
  // setting and stays out of the text. The run requires the German
  // interface, hence the German quotes.
  const expectedQuestions = materialKind === 'editor'
    ? [`„${documentMaterial.editor_title}“ ${prompt}`, `${prompt} „${documentMaterial.editor_title}“`]
    : [prompt]
  let submitted
  if (manualConversation) {
    await writeFile(join(reportDirectory, 'manual-conversation-turn.json'),
      JSON.stringify({ session_id: activeSession.id, title: activeTitle, marker, prompt, webEnabled, tier, engine, autonomy,
        materialKind, collection: documentMaterial?.collection_name, editor: documentMaterial?.editor_title }, null, 2), { mode: 0o600 })
    progress(`Waiting for ${marker} through the in-app browser.`)
    submitted = await waitFor(async () => {
      const listing = await fetchJson(tester, 'GET', '/v1/runs?limit=200')
      return listing.data.find((row) => row.session_id === activeSession.id && !ownedRuns.includes(row.run_id)
        && expectedQuestions.some((expected) => row.question?.startsWith(expected.slice(0, 120))))
    }, 300_000, `${marker} in-app submission`)
  } else {
    if (tier) await selectRunSetup(tester.page, 'Tempo & Tiefe', { schnell: 'Schnell', gruendlich: 'Gründlich', tief: 'Tief' }[tier])
    if (engine) await selectRunSetup(tester.page, 'Arbeitsweise', { mission: 'Missions-Maschine', kernel: 'Automatisch' }[engine])
    if (autonomy) await selectRunSetup(tester.page, 'Berechtigung', { strict: 'Strikt', balanced: 'Standard', autonomous: 'Auto' }[autonomy])
    // The question is typed after what the input already holds: the editing
    // target stands in the input as a chip, and replacing the whole text
    // (`fill`) would delete it and with it the target.
    await tester.page.getByTestId('agent-composer-input').locator('[contenteditable="true"]').click()
    await tester.page.keyboard.press('Control+End')
    await tester.page.keyboard.insertText(prompt)
    const pending = tester.page.waitForResponse((response) =>
      response.request().method() === 'POST' && new URL(response.url()).pathname === '/v1/runs')
    await tester.page.getByTestId('agent-submit').click()
    const response = await pending
    assert([200, 202].includes(response.status()), 'Conversation submission failed.')
    submitted = await response.json()
    const body = response.request().postDataJSON()
    assert(body?.session_id === activeSession.id, 'The conversation turn targeted another session.')
    assert(body?.source_policy?.web === (webEnabled ? 'available' : 'disabled'),
      'The conversation turn changed the chosen web policy.')
    // The editing target travels with exactly the turn that chose it.
    // No target may travel as a missing field or as null.
    const target = materialKind === 'editor' ? documentMaterial.editor_id : null
    assert((body?.document_id ?? null) === target,
      `${marker} was sent with editing target ${body?.document_id ?? 'none'} instead of ${target ?? 'none'}.`)
  }
  ownedRuns.push(submitted.run_id)
  await lifecycle.register({ retainForReview, credential: 'user', id: submitted.run_id, kind: 'agent_run', ownerEmail: testerEmail, sessionTitle: activeTitle })
  const durable = await fetchJson(tester, 'GET', `/v1/runs/${submitted.run_id}`)
  assert(expectedQuestions.includes(durable.question_full ?? durable.question), 'The saved conversation question differs from the submitted prompt.')
  if (tier) {
    assert(durable.agent_overrides?.agent_tier === tier,
      `${marker} was submitted in tier ${durable.agent_overrides?.agent_tier ?? 'default'} instead of ${tier}.`)
  }
  if (engine) {
    const mode = { mission: 'workspace_agent', kernel: 'agent_kernel' }[engine]
    assert(durable.mode === mode, `${marker} was submitted as ${durable.mode} instead of ${mode}.`)
  }
  return submitted.run_id
}

async function captureTurn(marker, question, state) {
  const children = await fetchJson(tester, 'GET', `/v1/runs/${runId}/children`)
  for (const child of children.data ?? []) {
    if (!ownedRuns.includes(child.run_id)) {
      ownedRuns.push(child.run_id)
      await lifecycle.register({ retainForReview, credential: 'user', id: child.run_id, kind: 'agent_run', ownerEmail: testerEmail, sessionTitle: activeTitle })
    }
  }
  const artifacts = await fetchJson(tester, 'GET', `/v1/runs/${runId}/artifacts`)
  const events = await fetchJson(tester, 'GET', `/v1/runs/${runId}/events?format=json`)
  await writeFile(join(reportDirectory, `events-${marker}.json`), JSON.stringify(events, null, 2), { mode: 0o600 })
  const details = []
  for (const item of artifacts.data ?? []) {
    details.push(await fetchJson(tester, 'GET', `/v1/runs/${runId}/artifacts/${item.artifact_id}`))
  }
  conversation.push({ case: marker, run_id: runId, session_id: activeSession.id, question, state, artifacts: artifacts.data ?? [], artifact_details: details,
    trace_id: events.data?.findLast((item) => item.type === 'inqtrix.run.trace')?.data?.trace_id })
  await writeFile(join(reportDirectory, 'conversation-runs.json'), JSON.stringify(conversation, null, 2), { mode: 0o600 })
  const started = events.data.filter((item) => item.type === 'inqtrix.agent.tool.started')
    .map((item) => item.data.tool)
  const policy = state.snapshot?.execution?.source_policy?.web
  assert(policy === (['R1', 'R2', 'R3'].includes(marker) ? 'available' : 'disabled'),
    `${marker} did not retain the intended native web policy.`)
  if (state.status === 'completed' && ['R1', 'R2', 'R3'].includes(marker)) {
    const researched = state.snapshot?.execution?.tool_use_counts?.web > 0 && started.some((tool) =>
      ['web_instant', 'run_web_research', 'delegate_batch', 'run_deep_mission'].includes(tool))
    // R1 and R2 ask for research in so many words. R3 asks to check the
    // current official guidance: re-reading what this conversation already
    // researched meets that as well as a new search does, so the
    // expectation is a consulted source, not a route.
    const reread = marker === 'R3' && started.some((tool) => ['read_evidence', 'read_context'].includes(tool))
    assert(researched || reread, marker === 'R3'
      ? 'R3 consulted neither new research nor the evidence of this conversation.'
      : `${marker} completed without the requested new research.`)
  }
  if (['R4', 'R5', 'R6', 'R7', 'R9', 'R11'].includes(marker)) {
    assert(state.snapshot?.execution?.tool_use_counts?.web === 0,
      `${marker} recorded new web usage with web disabled.`)
    assert(!started.some((tool) => ['web_instant', 'run_web_research'].includes(tool)),
      `${marker} started new research with web disabled.`)
  }
  if (state.status === 'completed' && marker === 'R6') {
    assert(started.includes('read_project_document'), 'The requested knowledge original was not read.')
  }
  if (state.status === 'completed' && marker === 'R7') {
    assert(started.includes('read_editor_document') && started.includes('search_editor_document'),
      'The requested editor read and anchor search were not executed.')
  }
  if (state.status === 'completed' && marker === 'R8') {
    // Materials of earlier turns reached this turn through the conversation
    // originals: a saved original of a document turn was searched
    // and read, and no document was opened again. The kernel transcript and
    // an offloaded tool-result archive of such a turn both hold the
    // transferred text and both pass the material check; the passage may be
    // read from either.
    assert(!started.some((tool) => ['read_project_document', 'read_editor_document', 'search_editor_document', 'search_project_knowledge'].includes(tool)),
      'R8 opened a document instead of the saved conversation.')
    assert(started.includes('search_context') && started.includes('read_context'),
      'R8 did not search and read the conversation originals.')
    const documentTurns = new Set(conversation.filter((turn) => ['R6', 'R7'].includes(turn.case)).map((turn) => turn.run_id))
    const coverage = state.snapshot?.execution?.source_coverage ?? []
    const carriers = coverage.filter((row) => {
      const [kind, originRun] = sourceReference(row.source_id)
      return row.kind === 'conversation_source' && row.read_ranges.length > 0
        && ['transcript', 'archive'].includes(kind) && documentTurns.has(originRun)
    })
    assert(carriers.length > 0, 'R8 read no saved original of a document turn.')
  }
  if (state.status === 'completed' && marker === 'R10') await assertReviewRole(state, events, started)
  if (state.status === 'completed' && marker === 'R11') await assertMissionReview(state, events, details)
  if (state.status === 'completed') {
    try {
      await assertReviewMatchesTier(marker, state, events)
      reviewedTurns += 1
    } catch (error) {
      reviewMismatches.push(String(error?.message ?? error))
    }
  }
  // The question of each document turn asks for the COMPLETE source. The
  // kernel publishes what was actually read per source version
  // (execution.source_coverage); a search excerpt alone is not that read.
  if (state.status === 'completed' && ['R6', 'R7', 'R9'].includes(marker)) {
    const coverage = state.snapshot?.execution?.source_coverage
    assert(Array.isArray(coverage), `${marker} published no source coverage.`)
    const expected = {
      R6: { kind: 'knowledge_document', id: documentMaterial.document_id, label: 'knowledge original' },
      R7: { kind: 'editor_document', id: documentMaterial.editor_id, label: 'editor working version' },
      R9: { kind: 'knowledge_document', id: documentMaterial.attachment.document_id, label: 'file attachment' },
    }[marker]
    const entry = coverage.find((row) => row.kind === expected.kind && row.source_id === expected.id)
    assert(entry, `${marker} never read the selected ${expected.label}.`)
    assert(entry.complete === true && entry.continuation_open === false,
      `${marker} did not read the ${expected.label} completely: ${JSON.stringify(entry.read_ranges)} of ${entry.total}.`)
    if (marker === 'R7') {
      assert(entry.scanned_ranges.some(([start, end]) => start === 0 && end === entry.total),
        'The anchor search did not scan the whole editor working version.')
    }
  }
}

/**
 * Published == enforced, for every finished turn of this conversation.
 *
 * The Stufe table publishes a report-quality level per Stufe, and the
 * engines gate their review on it. A turn's outcome record therefore
 * states how its review ended, and WHICH state follows from the level the
 * composer's Stufe published — not from the engine that happened to run,
 * and not from the depth flag the Stufe maps onto: `completed` where the
 * level owes a review, `waived` where the Stufe skips it, and `not_run`
 * for a request without a Stufe whose depth owes none.
 */
async function assertReviewMatchesTier(marker, state, events) {
  if (state.snapshot?.execution?.effective_mode !== 'agent_kernel') return
  // A redelivered segment repeats its record; the last one is the run's end.
  const record = outcomeRecord(events)
  assert(record, `${marker} sent no outcome record.`)
  assert(record.review !== 'unavailable', `${marker} owed a review that did not run.`)
  // The Stufe of record is the one the run was SUBMITTED with.
  const durable = await fetchJson(tester, 'GET', `/v1/runs/${state.run_id ?? runId}`)
  const tier = durable.agent_overrides?.agent_tier ?? ''
  const level = publishedVerifyLevel(tier, durable.agent_overrides?.depth ?? durable.depth ?? '')
  const expected = level !== 'labels' ? 'completed' : tier ? 'waived' : 'not_run'
  assert(record.review === expected,
    `${marker} ran at verify level ${level} and records its review as ${record.review}.`)
}

/** The run's outcome record (`inqtrix.agent.outcome`): the last one sent. */
function outcomeRecord(events) {
  return events.data.filter((item) => item.type === 'inqtrix.agent.outcome').at(-1)?.data
}

/** A run's `error` as text: the API sends an object with a `message`. */
function runErrorText(error) {
  if (error == null) return ''
  if (typeof error === 'object') return String(error.message ?? JSON.stringify(error))
  return String(error)
}

/** The level a turn owes: its Stufe's published one, else legacy depth. */
function publishedVerifyLevel(tier, depth) {
  if (!tier) return depth === 'deep' ? 'standard' : 'labels'
  const entry = (publishedTiers ?? []).find((item) => item.id === tier)
  assert(entry?.verify, `The stack publishes no verify level for Stufe ${tier}.`)
  return entry.verify
}

async function assertReviewRole(state, events, started) {
  // The Stufe decides WHETHER this turn is reviewed, the size of its
  // material HOW. Read how the review ran from the parent's own rows and
  // hold it to its contract, instead of assuming the role: a Deep turn
  // whose material fits the budget is reviewed in one call and has no
  // child, and asserting the child would make this round fail on a run
  // that did exactly the right thing.
  const record = outcomeRecord(events)
  assert(record?.review === 'completed' && typeof record.summary === 'string' && record.summary,
    `R10 records its review as ${JSON.stringify(record ? [record.review, record.summary] : null)}.`)
  const byRole = events.data.some((item) => item.type === 'inqtrix.agent.tool.started' && item.data.tool === 'review_role')
  const children = (await fetchJson(tester, 'GET', `/v1/runs/${runId}/children`)).data ?? []
  const roles = children.filter((child) => child.work_profile)
  assert(roles.length === (byRole ? 1 : 0),
    `R10 has ${roles.length} review role children for ${byRole ? 'a role review' : 'a one-call review'}.`)
  assert(!started.some((tool) => ['web_instant', 'run_web_research', 'run_deep_mission', 'delegate_batch'].includes(tool)),
    'R10 researched although both documents were in the selected collection.')
  assert(state.snapshot?.execution?.work_profile === '', 'The parent turn carries a work profile.')
  if (!byRole) return
  // The role is ONE child run of the kernel with a work profile: bounded
  // tools, a typed handover in, a typed result out, no answer of its own.
  assert(roles[0].work_profile === 'review' && roles[0].origin_key === 'role-review'
    && roles[0].mode === 'agent_kernel', `Unexpected review role child: ${JSON.stringify(roles.map((c) => [c.work_profile, c.origin_key, c.mode]))}.`)
  const role = roles[0]
  assert(role.status === 'completed', `The review role ended ${role.status}.`)
  const result = await fetchJson(tester, 'GET', `/v1/runs/${role.run_id}/result`)
  const work = result.work_result
  assert(work?.profile === 'review' && work.status === 'completed' && work.value,
    `The review role stored no typed verdict: ${JSON.stringify(work ?? null).slice(0, 300)}`)
  assert(typeof work.value.grounded === 'boolean' && Array.isArray(work.value.findings) && Array.isArray(work.value.not_checked),
    'The typed verdict lacks its contract fields.')
  assert(Array.isArray(work.read_before_result) && work.read_before_result.length > 0,
    'The review role judged without reading anything before its verdict.')
  assert(result.answer === '' && result.execution?.work_profile === 'review', 'The role run published an answer or lost its profile.')
  const roleArtifacts = (await fetchJson(tester, 'GET', `/v1/runs/${role.run_id}/artifacts`)).data ?? []
  assert(!roleArtifacts.some((item) => item.kind === 'answer'), 'The role run created an answer artifact.')
  const roleEvents = await fetchJson(tester, 'GET', `/v1/runs/${role.run_id}/events?format=json`)
  await writeFile(join(reportDirectory, 'events-R10-review-role.json'), JSON.stringify(roleEvents, null, 2), { mode: 0o600 })
  assert(!roleEvents.data.some((item) => item.type.includes('answer')), 'The role run emitted answer events.')
  const profileTools = ['read_evidence', 'read_canvas', 'read_project_document', 'read_editor_document',
    'search_editor_document', 'read_context', 'write_todos', 'DeepReviewVerdict']
  const used = roleEvents.data.filter((item) => item.type === 'inqtrix.agent.tool.started').map((item) => item.data.tool)
  const foreign = used.filter((tool) => !profileTools.includes(tool))
  assert(foreign.length === 0, `The review role used tools outside its profile: ${foreign.join(', ')}.`)
  // It re-read what the turn read: the documents of this turn, by address.
  const reread = work.read_before_result.map(([kind]) => kind)
  assert(reread.includes('knowledge_document'), `The review role did not re-read a document of the turn: ${JSON.stringify(work.read_before_result)}.`)
  // On the parent: one delegation row for the role.
  const rows = events.data.filter((item) => item.type.startsWith('inqtrix.agent.tool.') && item.data.tool === 'review_role')
    .map((item) => `${item.type.split('.').at(-1)}:${item.data.invocation_id}:${item.data.status ?? ''}`)
  assert(JSON.stringify(rows) === JSON.stringify(['started:role-review:', 'finished:role-review:success']),
    `The parent shows the review role as ${JSON.stringify(rows)}.`)
}

async function assertMissionReview(state, events, details) {
  // The turn ran on the mission engine, free of interim approvals.
  const execution = state.snapshot?.execution
  assert(execution?.effective_mode === 'workspace_agent' && execution?.consent_reason === 'autonomous_policy',
    `R11 ran as ${execution?.effective_mode} under ${execution?.consent_reason}.`)
  // How the review ended is the outcome record's to say; the decisions
  // only say why the mission went on or stopped.
  const record = outcomeRecord(events)
  assert(record?.review === 'completed' && typeof record.summary === 'string' && record.summary,
    `The mission records its review as ${JSON.stringify(record ? [record.review, record.summary] : null)}.`)
  const decisions = events.data.filter((item) => item.type === 'inqtrix.agent.activity'
    && ['critic_revise', 'critic_research', 'critic_unavailable'].includes(item.data?.kind))
    .map((item) => item.data.kind)
  assert(!decisions.includes('critic_unavailable'), `The mission review was unavailable: ${JSON.stringify(decisions)}.`)
  // One review per synthesis; a research round in between buys another
  // one, and each role review is one child under its round's origin key.
  const children = (await fetchJson(tester, 'GET', `/v1/runs/${runId}/children`)).data ?? []
  const roles = children.filter((child) => child.work_profile)
  assert(roles.length <= record.round, `The mission has ${roles.length} review role children for ${record.round} review rounds.`)
  for (const role of roles) {
    assert(role.work_profile === 'review' && role.mode === 'agent_kernel' && /^role-review:\d+$/.test(role.origin_key)
      && role.status === 'completed', `Unexpected review role child: ${JSON.stringify([role.work_profile, role.mode, role.origin_key, role.status])}.`)
    const rows = events.data.filter((item) => item.type.startsWith('inqtrix.agent.tool.') && item.data.invocation_id === role.origin_key)
      .map((item) => item.type.split('.').at(-1))
    assert(JSON.stringify(rows) === JSON.stringify(['started', 'finished']), `The role ${role.origin_key} shows as ${JSON.stringify(rows)}.`)
  }
  // The stored report: the decision, every criterion settled by its number,
  // the round the record names, and a role child exactly when its
  // transport says so.
  const report = details.find((item) => item.kind === 'critic_report')?.payload
  assert(report && ['pass', 'revise', 'research', 'open'].includes(report.verdict), `The mission stored no review report: ${JSON.stringify(report ?? null).slice(0, 200)}`)
  assert(report.round === record.round, `The stored report names round ${report.round}, the record round ${record.round}.`)
  assert(['prompt', 'role'].includes(report.transport), `The last review names no transport: ${JSON.stringify(report.transport)}.`)
  const lastRole = roles.some((role) => role.origin_key === `role-review:${report.round}`)
  assert(lastRole === (report.transport === 'role'),
    `The last review ran as ${report.transport}, and its role child is ${lastRole ? 'present' : 'absent'}.`)
  const plan = await fetchJson(tester, 'GET', `/v1/runs/${runId}/plan`)
  const criteria = plan.success_criteria ?? []
  const settled = (report.review?.criteria ?? []).map((item) => item.index).sort((a, b) => a - b)
  assert(criteria.length > 0 && JSON.stringify(settled) === JSON.stringify(criteria.map((_, index) => index + 1)),
    `The verdict settles criteria ${JSON.stringify(settled)} of ${criteria.length} planned.`)
  assert(report.criteria_covered.length + report.criteria_uncovered.length <= criteria.length, 'The report counts more criteria than the plan has.')
  // An applied revision replaces passages of the memo and cites only its own references.
  const memo = details.find((item) => item.kind === 'memo')
  assert(memo?.content_markdown, 'The mission produced no memo.')
  const known = new Set((memo.refs ?? []).map((ref) => ref.label))
  const cited = [...memo.content_markdown.matchAll(/\[([KW]\d+)\]/g)].map((match) => match[1])
  const foreign = cited.filter((label) => !known.has(label))
  assert(foreign.length === 0, `The memo cites labels outside its references: ${[...new Set(foreign)].join(', ')}.`)
  const revised = decisions.includes('critic_revise')
    && !events.data.some((item) => item.data?.kind === 'critic_revision_discarded')
  if (revised) {
    assert(memo.revision > 1, 'A revised memo kept its first revision.')
    assert(events.data.some((item) => item.data?.kind === 'deep_review_findings'), 'A revision without visible findings.')
  }
}

function sourceReference(sourceId) {
  // The conversation source reference is base64url JSON [kind, run, local, version].
  try {
    return JSON.parse(Buffer.from(String(sourceId).replace(/^src_/, ''), 'base64url').toString('utf8'))
  } catch {
    return []
  }
}

function answeredTurn(page, state) {
  // The bubble shows the question of the run row, which the server clips at
  // its column limit (the full text stays one click away). A turn is found
  // by the text the surface shows, never by text it cannot show.
  return page.getByTestId('agent-timeline-panel').locator('div.space-y-2').filter({
    has: page.getByText(state.question, { exact: true }),
  }).filter({ has: page.getByTestId('agent-answer') }).last()
}

async function selectRunSetup(page, row, choice) {
  // The run setup keeps its menu open on a choice; Escape closes it. Some
  // rows are sections whose choices stand in the menu itself (Berechtigung,
  // Arbeitsweise); others open a submenu (Tempo & Tiefe). A choice is the one
  // option whose label reads exactly so ("Auto" is not "Automatisch"); a row
  // that opens a submenu shows its current value and is never the choice.
  // The menu closes with an animation. A trigger click before it is gone is
  // swallowed, and a lookup at that moment reads the menu that is leaving:
  // the trigger's own expanded state says the menu opened, and each call
  // ends only once its menu is gone.
  await page.locator('.agent-run-setup-trigger').click()
  await page.locator('.agent-run-setup-trigger[aria-expanded="true"]').waitFor({ state: 'visible' })
  const option = page.locator('[role="menuitem"]:not([aria-haspopup])')
    .filter({ has: page.getByText(choice, { exact: true }) })
  if (await option.count() === 0) {
    await page.locator('[role="menuitem"][aria-haspopup]').filter({ hasText: row }).click()
  }
  await option.click()
  await page.keyboard.press('Escape')
  await page.getByRole('menu').first().waitFor({ state: 'detached', timeout: 10_000 })
}

/** Rename the open session in the session list, as a user does: double-click, type, Enter. */
async function renameSession(title) {
  assert(title.startsWith(`${runKey} `), 'A renamed verification session must keep its run prefix.')
  await tester.page.locator(`[data-agent-session-id="${activeSession.id}"]`)
    .getByText(activeTitle, { exact: true }).dblclick()
  const input = tester.page.getByRole('textbox', { name: 'Sitzung umbenennen', exact: true })
  await input.waitFor({ state: 'visible', timeout: 10_000 })
  await input.fill(title)
  // The session list saves on its own schedule; the save that counts carries the new title.
  const saved = tester.page.waitForResponse((response) => response.request().method() === 'PUT'
    && new URL(response.url()).pathname === `/v1/agent-sessions/${activeSession.id}`
    && jsonBody(response.request())?.title === title, { timeout: 30_000 })
  await input.press('Enter')
  const response = await saved
  assert(response.status() === 200, `Saving the new title returned HTTP ${response.status()}.`)
  const stored = await fetchJson(tester, 'GET', `/v1/agent-sessions/${activeSession.id}`)
  assert(stored.title === title && stored.title_source === 'manual',
    `The rename stored ${JSON.stringify([stored.title, stored.title_source])}.`)
  // The server holds the new title now: the replay looks the session up by it,
  // even when the page below fails to follow.
  activeTitle = title
  await tester.page.getByRole('heading', { name: title, exact: true }).waitFor({ state: 'visible', timeout: 10_000 })
}

/** Judged after the follow-up: the server keeps the manual title, and the page still shows it. */
async function judgeManualTitle() {
  try {
    const stored = await fetchJson(tester, 'GET', `/v1/agent-sessions/${activeSession.id}`)
    assert(stored.title === activeTitle && stored.title_source === 'manual',
      `After the follow-up the session is titled ${JSON.stringify([stored.title, stored.title_source])}.`)
    await tester.page.getByRole('heading', { name: activeTitle, exact: true }).waitFor({ state: 'visible', timeout: 10_000 })
    await tester.page.locator(`[data-agent-session-id="${activeSession.id}"]`)
      .getByText(activeTitle, { exact: true }).waitFor({ state: 'visible', timeout: 10_000 })
    verdicts.pass('agent.conversation-manual-title')
  } catch (error) {
    failScenario('agent.conversation-manual-title', error?.message ?? error)
    progress(`Manueller Titel nicht bestanden: ${sanitize(String(error?.message ?? error))}`)
  }
}

/** A user stops a turn that is researching, through the visible Stop control. */
async function stopRunningTurn() {
  const prompt = 'Recherchiere zusätzlich gründlich in offiziellen Quellen, welche Behörden in Deutschland '
    + 'die Aufsicht nach dem AI Act übernehmen sollen und wie weit das nationale Durchführungsgesetz ist.'
  const id = await submitTurn(prompt, 'RS', true)
  stopTurnRunId = id
  // Stopped only once the turn really works: a queued run would test
  // admission, not stopping.
  await waitFor(async () => {
    const events = await fetchJson(tester, 'GET', `/v1/runs/${id}/events?format=json`)
    return events.data.some((item) => item.type === 'inqtrix.agent.tool.started')
  }, 180_000, 'a started tool in the turn to stop')
  await tester.page.locator('.agent-composer-actions').getByRole('button', { name: 'Stoppen', exact: true }).click()
  const state = await waitFor(async () => {
    const current = await fetchJson(tester, 'GET', `/v1/runs/${id}`)
    return ['completed', 'failed', 'cancelled'].includes(current.status) ? current : null
  }, 120_000, 'the stopped turn to end')
  // Work the turn had delegated ends with it and stays owned by this run.
  const children = (await fetchJson(tester, 'GET', `/v1/runs/${id}/children`)).data ?? []
  for (const child of children) {
    if (ownedRuns.includes(child.run_id)) continue
    ownedRuns.push(child.run_id)
    await lifecycle.register({ retainForReview, credential: 'user', id: child.run_id, kind: 'agent_run', ownerEmail: testerEmail, sessionTitle: activeTitle })
  }
  const events = await fetchJson(tester, 'GET', `/v1/runs/${id}/events?format=json`)
  await writeFile(join(reportDirectory, 'events-RS.json'), JSON.stringify(events, null, 2), { mode: 0o600 })
  assert(state.status === 'cancelled', `The stopped turn ended ${state.status}.`)
  const ended = await waitFor(async () => {
    const current = (await fetchJson(tester, 'GET', `/v1/runs/${id}/children`)).data ?? []
    return current.every((child) => ['completed', 'failed', 'cancelled'].includes(child.status)) ? current : null
  }, 60_000, 'the delegated work of the stopped turn to end')
  // What was still running when the Stop arrived ends cancelled with the
  // turn; only work that had finished before keeps its own outcome.
  const stopAt = events.data.find((item) => ['inqtrix.run.cancel_requested', 'inqtrix.run.cancelled'].includes(item.type))?.created_at
  assert(typeof stopAt === 'number', 'The stopped turn recorded no cancel request.')
  const wentOn = ended.filter((child) => child.status !== 'cancelled' && !(child.finished_at <= stopAt))
  assert(wentOn.length === 0, `Delegated work went on after the Stop: ${wentOn.map((child) => `${child.run_id} ${child.status}`).join(', ')}.`)
  // Where the turn stands it says that it was stopped, and the composer takes the next question.
  const turn = tester.page.getByTestId('agent-timeline-panel').locator('div.space-y-2')
    .filter({ has: tester.page.getByText(state.question, { exact: true }) }).last()
  await turn.getByText('Abgebrochen', { exact: true }).waitFor({ state: 'visible', timeout: 15_000 })
  await tester.page.getByTestId('agent-submit').waitFor({ state: 'visible', timeout: 15_000 })
  stoppedTurnAnswers = await turn.getByTestId('agent-answer').count()
  await turn.scrollIntoViewIfNeeded()
  await tester.page.screenshot({ fullPage: true, path: join(screenshotDirectory, 'conversation-RS.png') })
}

async function judgeCanvasCopy() {
  try {
    progress('Canvas-Bericht als Markdown kopieren')
    await copyCanvasReport()
    verdicts.pass('agent.conversation-canvas-copy')
  } catch (error) {
    failScenario('agent.conversation-canvas-copy', error?.message ?? error)
    progress(`Canvas-Kopie nicht bestanden: ${sanitize(String(error?.message ?? error))}`)
    await tester.page.screenshot({ fullPage: true, path: join(screenshotDirectory, 'canvas-copy-failure.png') })
      .catch(() => undefined)
  }
}

/** The Canvas report leaves the desk as Markdown with the AI notice: the stored report, unchanged. */
async function copyCanvasReport() {
  const reportTurn = conversation.find((turn) => turn.case === 'R5')
  assert(reportTurn, 'There is no Canvas report turn to copy from.')
  const report = reportTurn.artifact_details.find((item) => ['memo', 'deliverable'].includes(item.kind))
  assert(report?.content_markdown, 'The saved report has no stored text.')
  const title = report.content_markdown.match(/^# (.+)$/m)?.[1]
  assert(title, 'The saved report has no document heading.')
  await tester.context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(baseURL).origin })
  await answeredTurn(tester.page, reportTurn.state).getByTestId('agent-answer')
    .getByRole('button').filter({ hasText: /\.md/ }).first().click()
  const canvas = tester.page.getByTestId('agent-canvas-panel')
  await canvas.getByRole('heading', { name: title, exact: true }).waitFor({ state: 'visible' })
  await tester.page.evaluate(() => navigator.clipboard.writeText(''))
  await canvas.getByRole('button', { name: 'Markdown kopieren', exact: true }).click()
  const copied = await waitFor(async () => (await tester.page.evaluate(() => navigator.clipboard.readText())) || null,
    10_000, 'the copied Canvas report')
  await writeFile(join(reportDirectory, 'canvas-copy.md'), copied, { mode: 0o600 })
  assert(copied.startsWith(report.content_markdown.trimEnd()), 'The copied Markdown differs from the stored report.')
  assert(copied.trimEnd().endsWith('> Dieser Text wurde von einem KI-System erzeugt (Inqtrix).'),
    'The copied Markdown does not end with the AI notice.')
  await tester.page.screenshot({ fullPage: true, path: join(screenshotDirectory, 'canvas-copy.png') })
}

async function clearDocumentContext(page) {
  // Pinned chips stay after a submission by design; the history turn
  // must run without any document pin so nothing invites a re-read. A
  // collection chip has its own remove control. The editing target stands
  // in the input as a chip that goes like a word: select it, Backspace.
  const collections = page.getByRole('button', { name: /^Sammlung entfernen: / })
  while (await collections.count() > 0) await collections.first().click()
  const target = editingTargetChip(page)
  for (let attempt = 0; attempt < 3 && await target.count() > 0; attempt += 1) {
    await target.first().click()
    await page.keyboard.press('Backspace')
  }
  assert(await target.count() === 0, 'The editing target could not be removed from the input.')
}

function editingTargetChip(page) {
  return page.getByTestId('agent-composer-input').getByText(documentMaterial.editor_title)
}

async function selectDocumentContext(page, kind) {
  // A choice that already stands is not made again: the menu offers a
  // collection only while it is not pinned, and one editing target a turn.
  const standing = kind === 'knowledge'
    ? page.getByRole('button', { name: `Sammlung entfernen: ${documentMaterial.collection_name}`, exact: true })
    : editingTargetChip(page)
  if (await standing.count() > 0) return
  await page.getByRole('button', { name: 'Kontext oder Bearbeitungsziel hinzufügen', exact: true }).click()
  await page.getByRole('menuitem').filter({ hasText: kind === 'knowledge' ? 'Sammlungen auswählen' : 'Bearbeitungsziel' }).click()
  await page.getByRole('menuitem').filter({ hasText: kind === 'knowledge' ? documentMaterial.collection_name : documentMaterial.editor_title }).click()
  await page.keyboard.press('Escape')
}

async function verifyPersistedConversation(name, browserType) {
  const otherBrowser = await browserType.launch({ headless: true })
  const fixtures = createSessionFixtures({
    baseURL, browser: otherBrowser, ignoreHTTPSErrors, lifecycle, runId: runKey,
    screenshotDirectory, parseCollaborationFrame: () => null,
  })
  let reader
  try {
    reader = await fixtures.loginActor(testerEmail, userPassword, `Conversation-${name}`, 'user')
    await reader.page.goto('/', { waitUntil: 'domcontentloaded' })
    await reader.page.getByRole('button', { name: 'Agent Desk', exact: true }).click()
    await reader.page.locator(`[data-agent-session-id="${activeSession.id}"]`)
      .getByText(activeTitle, { exact: true }).click()
    const surface = reader.page.getByTestId('agent-timeline-panel')
    // A stop that did not hold may have left one answer of its own.
    const expected = conversation.length + stoppedTurnAnswers
    await waitFor(async () => {
      const count = await surface.getByTestId('agent-answer').count()
      return count === expected || (stopTurnUncertain && count === expected + 1)
    }, 30_000, `${name} persisted conversation answers`)
    const synthesis = answeredTurn(reader.page, conversation.find((turn) => turn.case === 'R4').state)
    assert(/12[.,\s]?500/.test(await synthesis.getByTestId('agent-answer').innerText()),
      `${name} synthesis omitted the revised budget.`)
    assert((await synthesis.getByTestId('agent-run-settings').innerText()).includes('Web aus'),
      `${name} synthesis did not display its disabled web policy.`)
    // The conversation holds more than one document by now. "Bericht ansehen"
    // opens the latest one; the report under review is the file of ITS turn.
    const reportTurn = answeredTurn(reader.page, conversation.find((turn) => turn.case === 'R5').state)
    await reportTurn.getByTestId('agent-answer').getByRole('button').filter({ hasText: /\.md/ }).first().click()
    const canvas = reader.page.getByTestId('agent-canvas-panel')
    const artifact = conversation.find((turn) => turn.case === 'R5').artifact_details.find((item) => ['memo', 'deliverable'].includes(item.kind))
    const title = artifact.content_markdown.match(/^# (.+)$/m)?.[1]
    assert(title, 'The saved management report has no document heading.')
    await canvas.getByRole('heading', { name: title, exact: true }).waitFor({ state: 'visible' })
    // The CONTENT of this report is checked against its stored bytes in the
    // R5 round. What this engine adds is that the stored document renders at
    // all here: its heading resolved above, and its body is not empty.
    const body = await canvas.innerText()
    assert(body.trim().length > 0, `${name} rendered the saved report as an empty Canvas.`)
    await reader.page.screenshot({ fullPage: true, path: join(screenshotDirectory, `conversation-${name}.png`) })
    assert(reader.browserObservations.responses > 0, `${name} recorded no browser traffic.`)
    assert(reader.errors.length === 0, `${name} recorded browser errors.`)
    verdicts.pass(`agent.conversation-${name}`)
  } catch (error) {
    await reader?.page.screenshot({ fullPage: true, path: join(screenshotDirectory, `conversation-${name}-failure.png`) })
      .catch(() => undefined)
    throw error
  } finally {
    try { if (reader) await fixtures.logoutActor(reader) } finally { await otherBrowser.close() }
  }
}

function jsonBody(request) {
  try {
    return request.postDataJSON()
  } catch {
    return null
  }
}

function requiredEnvironment(name) {
  const value = process.env[name]?.trim()
  if (!value) throw new Error(`${name} is required.`)
  return value
}

function progress(message) {
  console.log(`[agent-desk-live] ${sanitize(message)}`)
}

/** Engine output may echo answer fragments; keep it structural. The
 * orchestrator report additionally redacts UUIDs — deliberately not
 * bypassed here. */
function sanitize(value) {
  return value.replaceAll(/https?:\/\/\S+/g, '[url]').slice(0, 400)
}

async function waitFor(probe, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs
  let lastError = null
  while (Date.now() < deadline) {
    try {
      const value = await probe()
      if (value) return value
    } catch (error) {
      lastError = error
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000))
  }
  throw new Error(
    `Timed out waiting for ${label}${lastError ? `: ${lastError}` : ''}`,
  )
}

