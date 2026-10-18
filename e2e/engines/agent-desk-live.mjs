// Agent-Desk live engine (profile: agent-desk).
//
// Drives a research conversation through the Agent Desk against the
// already-running canonical stack: submit a run-prefixed question,
// watch the live tool activity, assert the clickable-citation answer
// experience, collect console/network hygiene, and finalize resources through
// the shared cleanup or explicit review-retention contract. Results use the
// sidecar file (INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH); resources
// register through the shared product-lifecycle IPC channel.

import { access, chmod, mkdir, rename, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

import { chromium, firefox, webkit } from '@playwright/test'

import {
  assertFixture as assert,
  fetchActorJson as fetchJson,
} from '../../tests/verification/fixtures/api.mjs'
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

const baseURL = process.env.INQTRIX_E2E_BASE_URL ?? 'http://127.0.0.1:8080'
const ignoreHTTPSErrors =
  process.env.INQTRIX_E2E_IGNORE_HTTPS_ERRORS === '1'
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
]
// A scenario starts as NOT RUN, not as failed. Booking an unmeasured
// scenario red makes a permanently red column that carries no
// information: it hides whichever real failure happens next to it, and
// it counts default values as evidence. The exit code below still
// treats anything that is not 'passed' as a failure - it is the REPORT
// that must tell a measurement from a default.
const scenarioStatus = new Map(SCENARIOS.map((id) => [id, 'not_run']))
const networkFindings = []

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
  // Network hygiene on top of the shared pageerror/console collectors.
  tester.page.on('requestfailed', (request) => {
    const failure = request.failure()?.errorText ?? 'failed'
    if (failure.includes('net::ERR_ABORTED')) return
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
  runId = await submitTurn(question, 'R1', true)
  assert(typeof runId === 'string' && runId.length > 0, 'No run id returned.')

  scenarioStatus.set('agent.kernel-run-submits', 'passed')
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
    `The kernel run ended ${terminal.status}: ${terminal.error ?? ''}`,
  )
  // The run and every owned resource are terminal and registered before
  // answer presentation is inspected. A later UI assertion must not turn
  // this independently proven cleanup contract red.
  scenarioStatus.set('agent.cleanup-integrity', 'passed')
  if (activitySeen) {
    scenarioStatus.set('agent.tool-activity-visible', 'passed')
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
  scenarioStatus.set('agent.answer-citations-clickable', 'passed')

  // One table for every follow-up turn. `web` is the native source policy,
  // `cites` expects a rendered citation in the answer, `context` the document
  // pin the turn needs (`none` clears the pins), `scenario` the verdict this
  // turn decides and `requires` the earlier turns that verdict also stands on.
  const rounds = [
    { marker: 'R2', web: true, cites: true, prompt: 'Wie unterscheidet sich davon der aktuelle Stand der KI-Haftungsrichtlinie und der überarbeiteten Produkthaftungsrichtlinie? Recherchiere gezielt in offiziellen Quellen. Trenne beide Vorhaben und nenne den jeweiligen Stand; eine zurückgenommene Vorlage darf nicht wie geltendes Recht behandelt werden.' },
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
  const failedRounds = new Set()
  for (const { marker, prompt, web = false, cites = false, context = null, scenario = null, requires = [], tier = null, engine = null, autonomy = null } of rounds) {
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
      assert(state.status === 'completed', `${marker} ended ${state.status}: ${state.error ?? ''}`)
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
      if (scenario && !requires.some((earlier) => failedRounds.has(earlier))) scenarioStatus.set(scenario, 'passed')
    } catch (error) {
      failedRounds.add(marker)
      progress(`${marker} nicht bestanden: ${sanitize(String(error?.message ?? error))}`)
      await tester.page.screenshot({ fullPage: true, path: join(screenshotDirectory, `conversation-${marker}-failure.png`) })
        .catch(() => undefined)
    }
  }
  // Each engine gets its own verdict. With chained awaits a Firefox probe
  // error would leave WebKit unmeasured, so the engine that is supposed to
  // be the independent second opinion would never reach one. Collect both,
  // then fail on the first.
  const replayFailures = []
  for (const [name, browserType] of [['firefox', firefox], ['webkit', webkit]]) {
    try {
      await verifyPersistedConversation(name, browserType)
    } catch (error) {
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
      const summary = await fetchJson(tester, 'GET', `/v1/runs/${id}`, { expected: [200, 404] })
        .catch(() => null)
      if (summary && ['queued', 'running', 'waiting_for_approval', 'waiting_for_input', 'waiting_for_children'].includes(summary.status)) {
        await fetchJson(tester, 'POST', `/v1/runs/${id}/cancel`, {
          expected: [200, 202, 404, 409],
        }).catch(() => null)
        await waitFor(
          async () => {
            const current = await fetchJson(tester, 'GET', `/v1/runs/${id}`)
            return ['completed', 'failed', 'cancelled'].includes(current.status)
              ? current
              : null
          },
          120_000,
          'terminal state after cancel',
        ).catch(() => {
          scenarioStatus.set('agent.cleanup-integrity', 'failed')
        })
      }
    }
  } catch {
    scenarioStatus.set('agent.cleanup-integrity', 'failed')
  }
  if (tester) {
    const hygieneFindings = [
      ...tester.errors,
      ...networkFindings,
    ]
    scenarioStatus.set(
      'agent.console-network-clean',
      hygieneFindings.length === 0 ? 'passed' : 'failed',
    )
    if (hygieneFindings.length > 0) {
      progress(
        `Konsole/Netzwerk nicht sauber (${hygieneFindings.length} Befunde)`,
      )
    }
  }
  exitCode = [...scenarioStatus.values()].every((status) => status === 'passed')
    ? 0
    : 1
  await writeScenarioSidecar(
    SCENARIOS.map((id) => ({ id, status: scenarioStatus.get(id) ?? 'not_run' })),
  )
  await browser?.close().catch(() => null)
}

process.exit(exitCode)

async function submitTurn(prompt, marker, webEnabled, materialKind = null, { tier = null, engine = null, autonomy = null } = {}) {
  const web = tester.page.locator('[data-source="web"]')
  if (await web.getAttribute('aria-pressed') !== String(webEnabled)) await web.click()
  await tester.page.locator(`[data-source="web"][aria-pressed="${webEnabled}"]`)
    .waitFor({ state: 'visible' })
  let submitted
  if (manualConversation) {
    await writeFile(join(reportDirectory, 'manual-conversation-turn.json'),
      JSON.stringify({ session_id: activeSession.id, title: activeTitle, marker, prompt, webEnabled, tier, engine, autonomy,
        materialKind, collection: documentMaterial?.collection_name, editor: documentMaterial?.editor_title }, null, 2), { mode: 0o600 })
    progress(`Waiting for ${marker} through the in-app browser.`)
    submitted = await waitFor(async () => {
      const listing = await fetchJson(tester, 'GET', '/v1/runs?limit=200')
      return listing.data.find((row) => row.session_id === activeSession.id && !ownedRuns.includes(row.run_id)
        && row.question?.startsWith(prompt.slice(0, 120)))
    }, 300_000, `${marker} in-app submission`)
  } else {
    if (tier) await selectRunSetup(tester.page, 'Tempo & Tiefe', { schnell: 'Schnell', gruendlich: 'Gründlich', tief: 'Tief' }[tier])
    if (engine) await selectRunSetup(tester.page, 'Arbeitsweise', { mission: 'Missions-Maschine', kernel: 'Kernel' }[engine])
    if (autonomy) await selectRunSetup(tester.page, 'Berechtigung', { strict: 'Strikt', balanced: 'Standard', autonomous: 'Auto' }[autonomy])
    await tester.page.getByTestId('agent-composer-input').locator('[contenteditable="true"]').fill(prompt)
    const pending = tester.page.waitForResponse((response) =>
      response.request().method() === 'POST' && new URL(response.url()).pathname === '/v1/runs')
    await tester.page.getByTestId('agent-submit').click()
    const response = await pending
    assert([200, 202].includes(response.status()), 'Conversation submission failed.')
    submitted = await response.json()
    assert(response.request().postDataJSON()?.session_id === activeSession.id, 'The conversation turn targeted another session.')
    assert(response.request().postDataJSON()?.source_policy?.web === (webEnabled ? 'available' : 'disabled'),
      'The conversation turn changed the chosen web policy.')
  }
  ownedRuns.push(submitted.run_id)
  await lifecycle.register({ retainForReview, credential: 'user', id: submitted.run_id, kind: 'agent_run', ownerEmail: testerEmail, sessionTitle: activeTitle })
  const durable = await fetchJson(tester, 'GET', `/v1/runs/${submitted.run_id}`)
  assert((durable.question_full ?? durable.question) === prompt, 'The saved conversation question differs from the submitted prompt.')
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
  if (state.status === 'completed') await assertReviewMatchesTier(marker, state, events)
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
  // The run setup keeps its menu open on a choice; Escape closes it.
  await page.locator('.agent-run-setup-trigger').click()
  await page.getByRole('menuitem').filter({ hasText: row }).click()
  await page.getByRole('menuitem').filter({ hasText: new RegExp(`^${choice}`) }).click()
  await page.keyboard.press('Escape')
}

async function clearDocumentContext(page) {
  // Pinned chips stay after a submission by design; the history turn
  // must run without any document pin so nothing invites a re-read.
  for (const name of [/^Sammlung entfernen: /, /^Dokument-Patch: /]) {
    const chip = page.getByRole('button', { name })
    while (await chip.count() > 0) await chip.first().click()
  }
}

async function selectDocumentContext(page, kind) {
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
    await waitFor(async () => await surface.getByTestId('agent-answer').count() === conversation.length,
      30_000, `${name} persisted conversation answers`)
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
    scenarioStatus.set(`agent.conversation-${name}`, 'passed')
  } catch (error) {
    await reader?.page.screenshot({ fullPage: true, path: join(screenshotDirectory, `conversation-${name}-failure.png`) })
      .catch(() => undefined)
    throw error
  } finally {
    try { if (reader) await fixtures.logoutActor(reader) } finally { await otherBrowser.close() }
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

/** Sidecar contract of tests/verification/scenario-results.ts:
 * atomic write, mode 0600, {schemaVersion: 1, scenarios}. */
async function writeScenarioSidecar(scenarios) {
  const path = process.env.INQTRIX_VERIFICATION_SCENARIO_RESULTS_PATH
  if (!path) return
  const temporaryPath = `${path}.tmp`
  const payload = `${JSON.stringify({ scenarios, schemaVersion: 1 }, null, 2)}\n`
  await writeFile(temporaryPath, payload, { encoding: 'utf8', mode: 0o600 })
  await rename(temporaryPath, path)
  await chmod(path, 0o600)
}
