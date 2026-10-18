import { access, mkdir, writeFile } from 'node:fs/promises'
import { join } from 'node:path'
import { createHash } from 'node:crypto'

import { chromium, firefox, webkit } from '@playwright/test'

import { assertFixture as assert, fetchActorJson as fetchJson } from './api.mjs'
import { createSessionFixtures } from './sessions.mjs'

const CASES = [
  { id: 'context', marker: 'MARKE1', status: 'completed', text: '73' },
  { id: 'output-limit', marker: 'MARKE2', status: 'failed', code: 'model_output_limit', text: 'maximale Ausgabegröße' },
  { id: 'arguments', marker: 'MARKE3', status: 'completed', text: 'Aufgabenliste abgeschlossen' },
  { id: 'arguments-exhausted', marker: 'MARKE4', status: 'failed', code: 'model_tool_arguments', text: 'ungültige Werkzeugargumente' },
  { id: 'transport', marker: 'MARKE5', status: 'completed', text: 'erfolgreich fortgesetzt' },
  { id: 'access-denied', marker: 'MARKE6', status: 'failed', code: 'provider_access_denied', text: 'Zugriff' },
  { id: 'unknown', marker: 'MARKE7', status: 'failed', code: 'server_error', text: 'interner Fehler' },
  { id: 'cancel', marker: 'MARKE8', status: 'cancelled', text: 'abgebrochen' },
  { id: 'context-unreducible', marker: 'MARKE9', status: 'failed', code: 'model_context_overflow', text: 'Modellkontext' },
  { id: 'archive-window', marker: 'MARKE11', status: 'completed', text: '83' },
  { id: 'archive-catalogue', marker: 'MARKE12', status: 'completed', text: '47' },
  { id: 'conversation-originals', marker: 'MARKE14', status: 'completed', text: '180' },
  { id: 'bulk-handover', marker: 'MARKE15', status: 'completed', text: '93' },
  { id: 'normal-memo', marker: 'MARKE10', status: 'completed', text: 'abnahmememo' },
]

export async function runAgentRecovery(options) {
  const { baseURL, ignoreHTTPSErrors, executablePath, lifecycle, reportDirectory,
    runKey, testerEmail, userPassword, screenshotDirectory, writeScenarioSidecar } = options
  const results = [
    ...CASES.map((item) => ({ id: `agent.recovery.${item.id}`, status: null })),
    { id: 'agent.recovery.browser-hygiene', status: null },
    { id: 'agent.recovery.firefox', status: null },
    { id: 'agent.recovery.webkit', status: null },
  ]
  const evidence = []
  const ownedRuns = []
  let browser
  let actor
  let completed = false
  let activeSession
  let activeCase
  const hold = Number(process.env.INQTRIX_E2E_VISUAL_REVIEW_SECONDS ?? 0)
  try {
    assert(Number.isInteger(hold) && hold >= 0 && hold <= 600, 'Visual review hold must be between 0 and 600 seconds.')
    await mkdir(screenshotDirectory, { recursive: true })
    browser = await chromium.launch({ executablePath, headless: true })
    const fixtures = createSessionFixtures({
      baseURL, browser, ignoreHTTPSErrors, lifecycle, runId: runKey,
      screenshotDirectory, parseCollaborationFrame: () => null,
    })
    actor = await fixtures.loginActor(testerEmail, userPassword, 'RecoveryTester', 'user')
    for (const [index, scenario] of CASES.entries()) {
      results[index].status = 'failed'
      const title = `${runKey} ${scenario.marker}`
      const session = await fetchJson(actor, 'PUT', `/v1/agent-sessions/agent-session-${runKey}-${index}`, {
        data: {
          title, title_revision: 0, title_source: 'manual',
          items_json: JSON.stringify({ source_policy: { web: scenario.id === 'bulk-handover' ? 'available' : 'disabled', knowledge: 'disabled' } }),
          created_at: Date.now() / 1000, updated_at: Date.now() / 1000,
        },
      })
      activeSession = session.id
      activeCase = scenario.id
      session.verificationWebEnabled = scenario.id === 'bulk-handover'
      await lifecycle.register({ credential: 'user', id: session.id, kind: 'agent_session', ownerEmail: testerEmail, title })
      console.log(`[agent-recovery] Starting ${scenario.id}`)
      if (scenario.id === 'context') {
        const seed = await submit(session, title, 'MARKE0', 'Erstelle das vorgesehene Quelldokument. Keine Websuche.')
        await terminal(seed.run_id)
        const listing = await fetchJson(actor, 'GET', `/v1/runs/${seed.run_id}/artifacts`)
        const source = listing.data.find((row) => row.title === 'Verification source')
        assert(source, 'The seeded source document was not created.')
        const body = '# Quellmaterial\n\n' + 'Neutrales Testmaterial ohne weitere Aussagen. '.repeat(180)
          + '\n\nDer gesuchte Wert am Ende lautet MARKEZIEL=73.\n'
        await fetchJson(actor, 'PUT', `/v1/runs/${seed.run_id}/artifacts/${source.artifact_id}`, {
          data: { content_markdown: body, expected_revision: source.revision },
        })
      }
      if (scenario.id === 'conversation-originals') {
        const original = 'Projekt Polar: verbindliche Ausgangsnotiz. '
          + 'Neutrale Arbeitsnotiz ohne weitere Vorgaben. '.repeat(240)
          + '\nVerbindliche Vorgaben: Verarbeitung ausschließlich innerhalb der EU; '
          + 'maximal 180 Millisekunden Antwortzeit; Monatsbudget 1.250 EUR. '
          + 'Bestätige diese Notiz nur kurz, ohne die Werte zu wiederholen.'
        for (let note = 0; note < 4; note += 1) {
          const seed = await submit(session, title, 'MARKE13', note === 0 ? original : 'Bestätige diese weitere neutrale Notiz kurz.')
          assert((await terminal(seed.run_id)).status === 'completed', 'A saved history seed did not complete.')
        }
        await writeFile(join(reportDirectory, 'conversation-original.json'), JSON.stringify({ text: original }), { mode: 0o600 })
      }
      const question = scenario.id === 'context'
        ? 'Lies das Quelldokument vollständig und nenne den Wert MARKEZIEL an dessen Ende. Keine Websuche.'
        : scenario.id === 'archive-window'
          ? 'Nenne den Wert MARKEZIEL am Ende des vorbereiteten Archivs. Sage ausdrücklich, ob nur ausgewählte Bereiche oder der vollständige Text gelesen wurden. Keine Websuche und keine Unteraufträge.'
        : scenario.id === 'archive-catalogue'
          ? 'Lies den Eintrag Gamma aus der vorbereiteten Archivübersicht und nenne seinen Wert MARKEKATALOG. Keine Websuche und keine Unteraufträge.'
        : scenario.id === 'conversation-originals'
          ? 'Welche drei verbindlichen Vorgaben habe ich am Anfang dieser Sitzung für Projekt Polar festgelegt? Prüfe die Originalstelle und nenne Region, maximale Antwortzeit und Monatsbudget. Keine Vermutungen, keine Websuche und keine Unteraufträge.'
        : scenario.id === 'bulk-handover'
          ? 'Nutze genau eine kurze Web-Abfrage zum fiktiven Abnahmedatensatz. Nenne den abschließenden Prüfwert aus dessen langem Ergebnis. Keine Unteraufträge.'
        : scenario.id === 'normal-memo'
          ? 'Erstelle ein kurzes Entscheidungsmemo im Canvas mit dem Titel Abnahmememo (Dateiname abnahmememo.md). Nutze ausschließlich diese fiktiven Testdaten: Anbieter Nord kostet 1.200 EUR pro Monat und verarbeitet alle Daten innerhalb der EU. Anbieter Süd kostet 900 EUR pro Monat und verarbeitet Daten in den USA. Harte Vorgabe ist eine Verarbeitung ausschließlich innerhalb der EU. Das Monatsbudget beträgt 1.500 EUR. Empfiehl einen Anbieter, berechne seine Jahreskosten und das verbleibende Monatsbudget. Erkläre auch, warum der günstigere Anbieter ausscheidet. Erfinde keine weiteren Eigenschaften. Keine Websuche und keine Unteraufträge.'
        : scenario.id === 'arguments'
          ? 'Erstelle mit write_todos eine Aufgabenliste mit dem Eintrag MARKEAUFGABE, markiere ihn als completed und antworte danach: Aufgabenliste abgeschlossen. Keine Websuche.'
          : 'Beantworte diese Diagnoseanfrage kurz. Keine Websuche und keine anderen Unteraufträge.'
      const manual = (scenario.id === 'normal-memo' && process.env.INQTRIX_E2E_MANUAL_NORMAL_CASE === '1')
        || (['conversation-originals', 'bulk-handover'].includes(scenario.id) && process.env.INQTRIX_E2E_MANUAL_SOURCE_CASES === '1')
      const submitted = manual
        ? await manualSubmit(session, title, scenario.marker, question)
        : await submit(session, title, scenario.marker, question)
      const entry = { case: scenario.id, run_id: submitted.run_id, session_id: session.id, status: 'running' }
      let cancellationStarted
      evidence.push(entry)
      await writeFile(join(reportDirectory, 'recovery-runs.json'), JSON.stringify(evidence, null, 2), { mode: 0o600 })
      if (scenario.id === 'cancel') {
        // A retry's technical detail can be folded in the timeline. Its
        // native event proves that the provider is waiting; the user's
        // visible Stop control remains the sole cancellation action.
        await waitFor(async () => {
          const events = await fetchJson(actor, 'GET', `/v1/runs/${submitted.run_id}/events?format=json`)
          return events.data.some((event) => event.type === 'inqtrix.agent.activity'
            && event.data?.retry?.failed_attempt === 1
            && event.data.retry.delay_seconds >= 10)
        }, 8_000, 'provider retry before cancellation')
        cancellationStarted = performance.now()
        await actor.page.locator('.agent-composer-actions').getByRole('button', { name: 'Stoppen', exact: true }).click()
      }
      const summary = await terminal(submitted.run_id)
      if (cancellationStarted !== undefined) entry.cancel_terminal_ms = Math.round(performance.now() - cancellationStarted)
      const events = await fetchJson(actor, 'GET', `/v1/runs/${submitted.run_id}/events?format=json`)
      await writeFile(join(reportDirectory, `events-${scenario.id}.json`), JSON.stringify(events, null, 2), { mode: 0o600 })
      if (scenario.id === 'arguments') {
        assert(events.data.some((event) => event.type === 'inqtrix.agent.todo.updated'
          && event.data.todos?.some((item) => item.content === 'MARKEAUFGABE' && item.status === 'completed')),
        'The corrected call did not actually complete the requested todo.')
      }
      if (scenario.id === 'arguments-exhausted') {
        assert(!events.data.some((event) => event.type === 'inqtrix.agent.tool.started'),
          'A tool executed despite the rejected argument batches.')
      }
      assert(summary.status === scenario.status, `${scenario.id} ended ${summary.status}.`)
      if (scenario.id === 'archive-window') {
        const previews = events.data.filter((event) => event.type === 'inqtrix.agent.tool.started'
          && event.data.tool === 'read_canvas').map((event) => event.data.args_preview)
        assert(previews[0] === 'Archivierter Text' && previews[1] === 'Archivierter Text ab Zeichen 77.915',
          'Archive window activity still exposes technical arguments instead of a readable position.')
        const listing = await fetchJson(actor, 'GET', '/v1/runs/' + submitted.run_id + '/artifacts')
        const parts = listing.data.filter((row) => row.kind === 'context_archive')
        assert(parts.length > 3, 'The archive source did not span enough storage parts.')
        const originals = []
        for (const part of parts) {
          originals.push(await fetchJson(actor, 'GET',
            '/v1/runs/' + submitted.run_id + '/artifacts/' + part.artifact_id))
        }
        originals.sort((a, b) => a.payload.context_archive.index - b.payload.context_archive.index)
        const head = originals[0]
        const source = originals.map((part) => part.content_markdown).join('')
        assert(source.length > 60_000 && source.endsWith('\nMARKEZIEL=83\n'),
          'The persisted original does not contain the complete tail fact.')
        assert(createHash('sha256').update(source).digest('hex') === head.payload.context_archive.content_hash,
          'The reassembled original differs from its source fingerprint.')
        const path = '/v1/runs/' + submitted.run_id + '/artifacts/' + head.artifact_id
        const refused = await fetchJson(actor, 'PUT', path, {
          data: { content_markdown: 'Attempted edit', expected_revision: 1 }, expected: [409],
        })
        assert(refused.error?.type === 'archive_immutable', 'The archive edit has the wrong refusal type.')
        const unchanged = await fetchJson(actor, 'GET', path)
        assert(unchanged.revision === 1 && unchanged.content_markdown === originals[0].content_markdown,
          'A refused edit modified the captured original.')
        await fetchJson(actor, 'GET',
          '/v1/runs/' + ownedRuns[0] + '/artifacts/' + head.artifact_id, { expected: [404] })
        await writeFile(join(reportDirectory, 'archive-originals.json'), JSON.stringify(originals, null, 2), { mode: 0o600 })
      }
      if (scenario.id === 'conversation-originals') {
        const used = events.data.filter((event) => event.type === 'inqtrix.agent.tool.finished').map((event) => event.data.tool)
        assert(used.includes('search_context') && used.includes('read_context'),
          'The real model did not search and read conversation originals.')
        const answer = await fetchJson(actor, 'GET', `/v1/runs/${submitted.run_id}/result`)
        assert(/EU/.test(answer.answer) && /180/.test(answer.answer) && /1[.,\s]?250/.test(answer.answer),
          'The retrieved original requirements are incomplete or incorrect.')
        await writeFile(join(reportDirectory, 'conversation-answer.json'), JSON.stringify(answer), { mode: 0o600 })
      }
      if (scenario.id === 'normal-memo') {
        const listing = await fetchJson(actor, 'GET', `/v1/runs/${submitted.run_id}/artifacts`)
        const memo = listing.data.find((row) => row.title === 'Abnahmememo')
        assert(memo, 'The ordinary run did not create the requested memo.')
        const stored = await fetchJson(actor, 'GET', `/v1/runs/${submitted.run_id}/artifacts/${memo.artifact_id}`)
        const body = stored.content_markdown
        assert(typeof body === 'string' && /Nord/.test(body) && /Süd/.test(body)
          && /EU/.test(body) && /14[.\s]?400/.test(body) && /\b300\b/.test(body),
        'The ordinary memo is missing required decision facts or calculations.')
        await writeFile(join(reportDirectory, 'normal-memo.json'), JSON.stringify(stored, null, 2), { mode: 0o600 })
      }
      if (scenario.code) assert(summary.error?.type === scenario.code, `${scenario.id} has the wrong error type.`)
      const surface = actor.page.getByTestId('agent-timeline-panel')
      if (scenario.id === 'archive-window' || scenario.id === 'archive-catalogue') {
        await surface.getByRole('button', { name: /^\d+ Schritte$/ }).click()
        await surface.getByRole('button').filter({ hasText: scenario.id === 'archive-window'
          ? 'Archivierter Text ab Zeichen 77.915' : 'Archivübersicht fortsetzen' })
          .waitFor({ state: 'visible', timeout: 10_000 })
      }
      if (scenario.status === 'completed') {
        await surface.getByTestId('agent-answer').last().waitFor({ state: 'visible', timeout: 30_000 })
        await waitFor(async () => (await surface.getByTestId('agent-answer').last().innerText())
          .toLowerCase().includes(scenario.text.toLowerCase()), 30_000, 'completed answer content')
      } else {
        await surface.getByText(new RegExp(scenario.text, 'i')).first()
          .waitFor({ state: 'visible', timeout: 30_000 })
      }
      // A backend terminal state and an answer can precede the browser's
      // terminal event. Require the owning live-status/Stop controls to settle
      // before any navigation, so reload cannot conceal broken completion.
      await surface.getByTestId('agent-run-status').waitFor({ state: 'hidden', timeout: 30_000 })
      await actor.page.locator('.agent-composer-actions').getByRole('button', { name: 'Stoppen', exact: true })
        .waitFor({ state: 'hidden', timeout: 30_000 })
      if (cancellationStarted !== undefined) entry.cancel_visible_ms = Math.round(performance.now() - cancellationStarted)
      await actor.page.screenshot({ fullPage: true, path: join(screenshotDirectory, `${scenario.id}.png`) })
      // A separate reload check proves persisted error typing; it is not used
      // to obtain a passing live failure/recovery display above.
      await openSession(session, title)
      if (scenario.code) {
        await actor.page.getByTestId('agent-timeline-panel').getByText(new RegExp(scenario.text, 'i')).first()
          .waitFor({ state: 'visible', timeout: 30_000 })
      }
      Object.assign(entry, {
        status: summary.status, error_type: summary.error?.type ?? null,
        trace_id: events.data.findLast((event) => event.type === 'inqtrix.run.trace')?.data?.trace_id ?? null,
      })
      await writeFile(join(reportDirectory, 'recovery-runs.json'), JSON.stringify(evidence, null, 2), { mode: 0o600 })
      results[index].status = 'passed'
      console.log(`[agent-recovery] Passed ${scenario.id}`)
    }
    for (const [name, browserType] of Object.entries({ firefox, webkit })) {
      const result = results.find((item) => item.id === `agent.recovery.${name}`)
      result.status = 'failed'
      await verifyPersistedSurfaces(name, browserType)
      result.status = 'passed'
      console.log(`[agent-recovery] Passed ${name} persisted surfaces`)
    }
    results[CASES.length].status = 'failed'
    assert(actor.browserObservations.responses > 0, 'The browser response observer recorded no traffic.')
    assert(actor.errors.length === 0, 'The recovery campaign recorded browser errors.')
    results[CASES.length].status = 'passed'
    await visualReviewHold()
    completed = true
  } catch (error) {
    if (actor) {
      await writeFile(join(reportDirectory, 'browser-failure.json'), JSON.stringify({
        url: actor.page.url(), errors: actor.errors, session_id: activeSession, case: activeCase,
        observedResponses: actor.browserObservations.responses,
      }, null, 2), { mode: 0o600 })
    }
    await actor?.page.screenshot({ fullPage: true, path: join(screenshotDirectory, 'recovery-failure.png') })
      .catch(() => undefined)
    console.error(`[agent-recovery] ${String(error?.message ?? error).slice(0, 500)}`)
    await visualReviewHold()
  } finally {
    if (actor) {
      for (const id of ownedRuns) {
        const summary = await fetchJson(actor, 'GET', `/v1/runs/${id}`, { expected: [200, 404] }).catch(() => null)
        if (summary && ['queued', 'running', 'waiting_for_approval', 'waiting_for_input', 'waiting_for_children'].includes(summary.status)) {
          await fetchJson(actor, 'POST', `/v1/runs/${id}/cancel`, { expected: [200, 202, 404, 409] })
          await terminal(id)
        }
      }
    }
    await writeScenarioSidecar(results.filter((item) => item.status !== null))
    await browser?.close()
  }
  return completed && results.every((item) => item.status === 'passed') ? 0 : 1

  async function visualReviewHold() {
    if (!Number.isInteger(hold) || hold < 0 || hold > 600) return
    const until = Date.now() + hold * 1000
    if (hold) console.log('[agent-recovery] Results retained for in-app visual and trace review.')
    while (Date.now() < until) {
      try { await access(join(reportDirectory, 'visual-review.done')); break } catch { /* bounded review hold */ }
      await new Promise((resolveWait) => setTimeout(resolveWait, 500))
    }
  }

  async function openSession(session, title) {
    await actor.page.goto('/', { waitUntil: 'domcontentloaded' })
    await actor.page.getByRole('button', { name: 'Agent Desk', exact: true }).click()
    await actor.page.locator(`[data-agent-session-id="${session.id}"]`).getByText(title, { exact: true }).click()
    await actor.page.getByRole('heading', { name: title, exact: true }).waitFor({ state: 'visible' })
    // The heading transitions before the selected row and its separately
    // fetched source policy. Wait for the controls that own those states.
    await waitFor(async () =>
      await actor.page.locator(`[data-agent-session-id="${session.id}"] [aria-pressed="true"]`).count() === 1
      && await actor.page.locator('[data-source="web"]').getAttribute('aria-pressed') === String(Boolean(session.verificationWebEnabled)),
    20_000, 'selected session and hydrated source policy')
  }

  async function submit(session, title, marker, question) {
    await openSession(session, title)
    assert(await actor.page.locator('[data-source="web"]').getAttribute('aria-pressed') === String(Boolean(session.verificationWebEnabled)), 'The test session did not apply its intended source policy.')
    await actor.page.getByRole('button', { name: 'Ausführung', exact: true }).click()
    await actor.page.getByRole('menuitem').filter({ hasText: 'Zwischenfreigaben' }).first().click()
    await actor.page.keyboard.press('Escape')
    await actor.page.getByTestId('agent-composer-input').locator('[contenteditable="true"]')
      .fill(`${runKey} ${marker} ${question}`)
    const pending = actor.page.waitForResponse((response) =>
      response.request().method() === 'POST' && new URL(response.url()).pathname === '/v1/runs')
    await actor.page.getByTestId('agent-submit').click()
    const response = await pending
    assert([200, 202].includes(response.status()), 'Recovery run submission failed.')
    const result = await response.json()
    ownedRuns.push(result.run_id)
    await lifecycle.register({ credential: 'user', id: result.run_id, kind: 'agent_run', ownerEmail: testerEmail, sessionTitle: title })
    assert(response.request().postDataJSON().session_id === session.id, 'A recovery run targeted another session.')
    assert(response.request().postDataJSON().source_policy?.web === (session.verificationWebEnabled ? 'available' : 'disabled'),
      'The submitted recovery run did not preserve its source policy.')
    return result
  }

  async function terminal(id) {
    return await waitFor(async () => {
      const summary = await fetchJson(actor, 'GET', `/v1/runs/${id}`)
      return ['completed', 'failed', 'cancelled'].includes(summary.status) ? summary : null
    }, 180_000, 'terminal recovery run')
  }

  async function manualSubmit(session, title, marker, question) {
    const prompt = `${runKey} ${marker} ${question}`
    await openSession(session, title)
    await writeFile(join(reportDirectory, 'manual-normal-case.json'),
      JSON.stringify({ session_id: session.id, title, prompt }, null, 2), { mode: 0o600 })
    console.log('[agent-recovery] Waiting for reviewed request ' + marker + ' through the in-app browser.')
    const submitted = await waitFor(async () => {
      const listing = await fetchJson(actor, 'GET', '/v1/runs?limit=200')
      return listing.data.find((row) => row.session_id === session.id && row.question?.startsWith(`${runKey} ${marker} `))
    }, 180_000, 'manual in-app memo submission', 1_000)
    ownedRuns.push(submitted.run_id)
    await lifecycle.register({ credential: 'user', id: submitted.run_id, kind: 'agent_run',
      ownerEmail: testerEmail, sessionTitle: title })
    const durable = await fetchJson(actor, 'GET', `/v1/runs/${submitted.run_id}`)
    assert((durable.question_full ?? durable.question) === prompt,
      'The manual request differs from the reviewed test prompt.')
    return submitted
  }

  async function verifyPersistedSurfaces(name, browserType) {
    const otherBrowser = await browserType.launch({ headless: true })
    const fixtures = createSessionFixtures({
      baseURL, browser: otherBrowser, ignoreHTTPSErrors, lifecycle, runId: runKey,
      screenshotDirectory, parseCollaborationFrame: () => null,
    })
    let reader
    try {
      reader = await fixtures.loginActor(testerEmail, userPassword, `Recovery-${name}`, 'user')
      await reader.page.goto('/', { waitUntil: 'domcontentloaded' })
      await reader.page.getByRole('button', { name: 'Agent Desk', exact: true }).click()
      for (const id of ['context', 'archive-window', 'archive-catalogue', 'cancel', 'output-limit']) {
        const scenario = CASES.find((item) => item.id === id)
        const entry = evidence.find((item) => item.case === id)
        const title = `${runKey} ${scenario.marker}`
        const persisted = await fetchJson(reader, 'GET', `/v1/runs/${entry.run_id}`)
        assert(persisted.status === scenario.status, `${name} read an unexpected native state.`)
        await reader.page.locator(`[data-agent-session-id="${entry.session_id}"]`)
          .getByText(title, { exact: true }).click()
        await reader.page.getByRole('heading', { name: title, exact: true }).waitFor({ state: 'visible' })
        const surface = reader.page.getByTestId('agent-timeline-panel')
        const content = scenario.status === 'completed'
          ? surface.getByTestId('agent-answer').last()
          : surface.getByText(new RegExp(scenario.text, 'i')).first()
        await content.waitFor({ state: 'visible', timeout: 30_000 })
        assert((await content.innerText()).toLowerCase().includes(scenario.text.toLowerCase()),
          `${name} did not render the persisted ${id} result.`)
        await reader.page.screenshot({ fullPage: true, path: join(screenshotDirectory, `${name}-${id}.png`) })
      }
      await reader.page.setViewportSize({ width: 390, height: 844 })
      const warning = reader.page.getByTestId('agent-timeline-panel')
        .getByText(/maximale Ausgabegröße/).first()
      let layout
      await waitFor(async () => {
        const viewport = await reader.page.evaluate(() => ({
          width: document.documentElement.clientWidth,
          scrollWidth: document.documentElement.scrollWidth,
        }))
        const box = await warning.boundingBox()
        layout = { ...viewport, warning: box }
        return viewport.width === 390 && viewport.scrollWidth <= 391
          && box && box.x >= 0 && box.x + box.width <= 391
      }, 5_000, `${name} narrow warning layout`)
      const details = reader.page.getByRole('button', { name: '2 Schritte', exact: true })
      await details.press('Enter')
      await waitFor(async () => await details.getAttribute('aria-expanded') === 'true',
        3_000, `${name} keyboard-opened details`)
      await details.press('Enter')
      await waitFor(async () => await details.getAttribute('aria-expanded') === 'false',
        3_000, `${name} keyboard-closed details`)
      await writeFile(join(reportDirectory, `${name}-narrow-layout.json`), JSON.stringify(layout, null, 2), { mode: 0o600 })
      await reader.page.screenshot({ fullPage: true, path: join(screenshotDirectory, `${name}-narrow-output-limit.png`) })
      assert(reader.browserObservations.responses > 0, `${name} response observer recorded no traffic.`)
      assert(reader.errors.length === 0, `${name} recorded browser errors: ${reader.errors.join('; ')}`)
    } catch (error) {
      await reader?.page.screenshot({ fullPage: true, path: join(screenshotDirectory, `${name}-failure.png`) })
        .catch(() => undefined)
      throw error
    } finally {
      try { if (reader) await fixtures.logoutActor(reader) } finally { await otherBrowser.close() }
    }
  }
}

async function waitFor(probe, timeout, label, intervalMs = 250) {
  const until = Date.now() + timeout
  while (Date.now() < until) {
    const value = await probe()
    if (value) return value
    await new Promise((resolveWait) => setTimeout(resolveWait, intervalMs))
  }
  throw new Error(`Timed out waiting for ${label}.`)
}
