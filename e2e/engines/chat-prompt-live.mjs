// Chat/Prompt-Library live engine (profile: chat-prompt).
//
// Closes the remaining LIVE browser-matrix cells for the Chat and
// Prompt Library surfaces against the already-running canonical stack:
// one real completion per browser, reload turn ordering, duplicated-tab
// consistency, offline failure visibility with recovery, and the
// template CRUD/revision/search lifecycle — in Chromium, Firefox, and
// WebKit plus one live mobile-viewport cell. Every assertion point
// leaves a screenshot so a red cell can be adjudicated visually before
// it is ever called a product finding. Scenario results are written to
// the orchestrator sidecar file; resources register through the shared
// product-lifecycle IPC channel and are deleted through the visible UI
// (the central ledger pass stays 404-tolerant crash safety).

import { chmod, mkdir, rename, writeFile } from 'node:fs/promises'
import { join } from 'node:path'

import { chromium, firefox, webkit } from '@playwright/test'

import {
  assertFixture as assert,
  fetchActorJson as fetchJson,
  isExpectedBrowserRequestAbort,
} from '../../tests/verification/fixtures/api.mjs'
import {
  assertVerificationRunId,
} from '../../tests/verification/fixtures/run-scope.mjs'
import {
  createSessionFixtures,
} from '../../tests/verification/fixtures/sessions.mjs'
import {
  VerificationLifecycleClient,
} from '../../tests/verification/fixtures/lifecycle-client.mjs'

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

const lifecycle = new VerificationLifecycleClient({
  reportDirectory,
  runId: runKey,
})

// German UI anchors — loginActor pins locale de-DE for every context.
const COMPOSER_LABEL = 'Nachricht an das Modell... @ für Kontext'
const SEND_LABEL = 'Nachricht senden'
const MODEL_PICKER_LABEL = 'Chatmodell auswählen'
const NEW_CHAT_LABEL = 'Neuer Chat'
const DELETE_THREAD_LABEL = 'Gespräch löschen'
const SYNC_ERROR_LABEL = 'Server-Synchronisierung fehlgeschlagen'
const PROMPT_TITLE_PLACEHOLDER = 'Prägnanter Name'
const PROMPT_LABEL_PLACEHOLDER = 'kurzes-label'
const PROMPT_BODY_PLACEHOLDER =
  'Beschreiben Sie die Anweisung oder den Kontext...'
const PROMPT_SAVE_LABEL = 'Prompt speichern'
const PROMPT_DELETE_LABEL = 'Prompt löschen'
const PROMPT_SEARCH_PLACEHOLDER = 'Prompts suchen'
const PROMPT_NEW_LABEL = 'Neuer Prompt'
/** Client transport failures are rendered AND synced as assistant-styled
 * error turns — a "real answer" assertion must never match them, or an
 * offline failure silently counts as a recovery. */
const ASSISTANT_ERROR_PATTERN =
  /Chat-Anfrage fehlgeschlagen|Chat request failed|Failed to fetch/i

const BROWSERS = [
  { launcher: chromium, name: 'chromium', options: { executablePath } },
  { launcher: firefox, name: 'firefox', options: {} },
  { launcher: webkit, name: 'webkit', options: {} },
]

const SCENARIOS = [
  'chat.live-turn-roundtrip',
  'chat.reload-turn-order',
  'chat.duplicate-tab-consistency',
  'chat.network-recovery',
  'chat.cleanup-integrity',
  'prompt.live-owner-crud',
  'prompt.live-mobile-viewport',
  'prompt.cleanup-integrity',
  'chatprompt.console-network-clean',
]
// Every cell must pass in EVERY applicable browser; one missing browser
// keeps the scenario red instead of silently narrowing the matrix.
const cellFailures = new Map(SCENARIOS.map((id) => [id, []]))
const hygieneFindings = []
const passedCells = new Set()

let exitCode = 1

try {
  await mkdir(screenshotDirectory, { recursive: true })
  for (const target of BROWSERS) {
    await runBrowserPass(target)
  }
  exitCode = SCENARIOS.every((id) => cellFailures.get(id).length === 0) ? 0 : 1
} catch (error) {
  progress(`Engine-Fehler: ${sanitize(String(error?.message ?? error))}`)
  exitCode = 1
} finally {
  for (const id of SCENARIOS) {
    for (const failure of cellFailures.get(id)) {
      progress(`ROT ${id}: ${sanitize(failure)}`)
    }
  }
  await writeScenarioSidecar(
    SCENARIOS.map((id) => ({
      id,
      status: cellFailures.get(id).length === 0 ? 'passed' : 'failed',
    })),
  )
}

process.exit(exitCode)

async function runBrowserPass(target) {
  progress(`${target.name}: Browser starten`)
  const browser = await target.launcher.launch({
    headless: true,
    ...target.options,
  })
  let tester
  // Findings inside the deliberate offline window are expected and are
  // reported separately instead of failing the hygiene cell.
  const state = { offlineWindow: false }
  const expectedOffline = []
  let errorsBeforeOffline = 0
  let errorsAfterOffline = 0
  try {
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
      `ChatPrompt-${target.name}`,
      'user',
    )
    tester.page.on('response', (response) => {
      if (response.status() >= 500) {
        const finding = `http:${response.status()}:${new URL(response.url()).pathname}`
        if (state.offlineWindow) expectedOffline.push(finding)
        else hygieneFindings.push(`${target.name}:${finding}`)
      }
    })

    await runChatCells(target, tester, state, () => {
      errorsBeforeOffline = tester.errors.length
    }, () => {
      errorsAfterOffline = tester.errors.length
    })
    await runPromptCells(target, tester)

    // Hygiene: fixture-collected findings outside the offline window.
    // SPA navigations legitimately abort in-flight requests — those
    // browser-specific abort markers are measurement noise, not product
    // signals, and every remaining finding stays hard.
    const outside = tester.errors
      .filter((_, index) =>
        index < errorsBeforeOffline || index >= errorsAfterOffline)
      .filter((finding) =>
        // Request-abort markers only — per engine: Chromium
        // net::ERR_ABORTED, Firefox NS_BINDING_ABORTED, WebKit
        // "cancelled". Console findings are never filtered.
        !isExpectedBrowserRequestAbort(finding))
    for (const finding of outside) {
      hygieneFindings.push(`${target.name}:${finding}`)
    }
    if (errorsAfterOffline > errorsBeforeOffline || expectedOffline.length > 0) {
      progress(
        `${target.name}: ${
          errorsAfterOffline - errorsBeforeOffline + expectedOffline.length
        } erwartete Offline-Fenster-Befunde (nicht hygienerelevant)`,
      )
    }
    if (hygieneFindings.length === 0) {
      markPassed('chatprompt.console-network-clean', target.name)
    } else {
      markFailed(
        'chatprompt.console-network-clean',
        target.name,
        `${hygieneFindings.length} Befunde: ${hygieneFindings.slice(0, 3).join(' | ')}`,
      )
    }
  } catch (error) {
    // A pass-level crash reddens every cell this browser did not finish.
    const message = `${target.name}: ${String(error?.message ?? error)}`
    progress(`Durchlauf-Fehler: ${sanitize(message)}`)
    for (const id of SCENARIOS) {
      if (id === 'prompt.live-mobile-viewport' && target.name !== 'chromium') {
        continue
      }
      if (!cellPassed(id, target.name)) cellFailures.get(id).push(message)
    }
  } finally {
    await browser.close().catch(() => null)
  }
}

function cellPassed(id, browserName) {
  return passedCells.has(`${id}:${browserName}`)
}

function markPassed(id, browserName) {
  passedCells.add(`${id}:${browserName}`)
}

function markFailed(id, browserName, message) {
  cellFailures.get(id).push(`${browserName}: ${message}`)
}

async function runChatCells(target, tester, state, beforeOffline, afterOffline) {
  const { page } = tester
  const browserName = target.name
  const question =
    `${runKey} Chat-Browsermatrix ${browserName}: Antworte mit einem kurzen Satz.`
  const offlineQuestion =
    `${runKey} Chat-Offline ${browserName}: Antworte mit einem kurzen Satz.`
  let threadId = null

  const composer = page.locator(
    `[aria-label="${COMPOSER_LABEL}"]:visible`,
  )
  try {
    progress(`${browserName}: Chat öffnen`)
    await page.goto('/', { waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: 'Chat', exact: true }).click()
    // ALWAYS start a fresh thread: the shared tester account may carry a
    // server-synced thread from an earlier browser pass, and typing into
    // that foreign thread would corrupt both measurements.
    await page.getByRole('button', { name: NEW_CHAT_LABEL }).first()
      .click()
      .catch(() => null)
    await composer.first().waitFor({ state: 'visible', timeout: 20_000 })

    // Cost control: the cell pins the cheapest chat model explicitly. The
    // trigger's accessible name is dynamic ("<title>: <active model>"),
    // so the anchor matches on the stable title prefix.
    await page
      .locator(`[aria-label^="${MODEL_PICKER_LABEL}"]:visible`)
      .first()
      .click()
    const nano = page.getByRole('menuitem', { name: /nano/i })
      .or(page.getByRole('option', { name: /nano/i }))
      .or(page.getByRole('button', { name: /nano/i }))
    await nano.first().waitFor({ state: 'visible', timeout: 10_000 })
    await nano.first().click()
    await page.keyboard.press('Escape').catch(() => null)

    progress(`${browserName}: Frage senden`)
    await composer.first().fill(question)
    const completion = page.waitForResponse(
      (response) =>
        response.request().method() === 'POST'
        && new URL(response.url()).pathname === '/v1/chat/completions',
      { timeout: 60_000 },
    )
    await page.locator(`[aria-label="${SEND_LABEL}"]:visible`).first().click()
    const completionResponse = await completion
    assert(
      completionResponse.status() < 400,
      `Completion returned HTTP ${completionResponse.status()}.`,
    )
    const thread = await waitFor(
      async () => {
        const listing = await fetchJson(tester, 'GET', '/v1/chat/threads')
        return listing.data?.find(
          (candidate) =>
            typeof candidate.title === 'string'
            && candidate.title.startsWith(`${runKey} `)
            && candidate.title.includes(browserName),
        ) ?? null
      },
      30_000,
      'run-titled chat thread in the sync store',
    )
    threadId = thread.id
    await lifecycle.register({
      credential: 'user',
      id: threadId,
      kind: 'chat_thread',
      ownerEmail: testerEmail,
      title: question,
    })
    await waitForAssistantCount(tester, threadId, 1, 120_000)
    await screenshot(page, `chat-roundtrip-${browserName}.png`)
    markPassed('chat.live-turn-roundtrip', browserName)
  } catch (error) {
    markFailed('chat.live-turn-roundtrip', browserName, String(error?.message ?? error))
    await screenshot(page, `chat-roundtrip-${browserName}-failed.png`)
  }
  if (!threadId) {
    // Without the shared thread the dependent chat cells cannot run;
    // they stay red for this browser while the prompt cells continue.
    for (const id of [
      'chat.reload-turn-order',
      'chat.duplicate-tab-consistency',
      'chat.network-recovery',
      'chat.cleanup-integrity',
    ]) {
      markFailed(id, browserName, 'No shared chat thread (roundtrip failed).')
    }
    return
  }

  try {
    progress(`${browserName}: Reload und Reihenfolge`)
    await page.reload({ waitUntil: 'domcontentloaded' })
    // The SPA restores its default surface after a reload — anchor on the
    // Chat surface and OUR thread before asserting anything.
    await page.getByRole('button', { name: 'Chat', exact: true }).click()
    const ownRow = page.getByText(`Chat-Browsermatrix ${browserName}`).first()
    if (!(await page.getByText(question, { exact: false }).first()
      .isVisible().catch(() => false))) {
      await ownRow.waitFor({ state: 'visible', timeout: 20_000 })
      await ownRow.click()
    }
    await page.getByText(question, { exact: false }).first()
      .waitFor({ state: 'visible', timeout: 30_000 })
    const messages = await fetchJson(
      tester,
      'GET',
      `/v1/chat/threads/${threadId}/messages`,
    )
    const ordered = messages.data ?? []
    assert(ordered.length === 2, `Expected 2 persisted turns, got ${ordered.length}.`)
    // The keyset page is newest-first: assistant answer, then user turn.
    assert(ordered[0].role === 'assistant', `Newest role is ${ordered[0].role}.`)
    assert(
      ordered[0].content_markdown.trim().length > 0,
      'Assistant turn is empty after reload.',
    )
    assert(ordered[1].role === 'user', `Oldest role is ${ordered[1].role}.`)
    assert(
      ordered[1].content_markdown.includes(browserName),
      'User turn lost its content after reload.',
    )
    await screenshot(page, `chat-reload-${browserName}.png`)
    markPassed('chat.reload-turn-order', browserName)
  } catch (error) {
    markFailed('chat.reload-turn-order', browserName, String(error?.message ?? error))
    await screenshot(page, `chat-reload-${browserName}-failed.png`)
  }

  try {
    progress(`${browserName}: Duplizierter Tab`)
    const second = await tester.context.newPage()
    let duplicateSends = 0
    second.on('request', (request) => {
      if (
        request.method() === 'POST'
        && new URL(request.url()).pathname === '/v1/chat/completions'
      ) duplicateSends += 1
    })
    await second.goto('/', { waitUntil: 'domcontentloaded' })
    await second.getByRole('button', { name: 'Chat', exact: true }).click()
    await second.getByText(question.slice(0, 60), { exact: false }).first()
      .waitFor({ state: 'visible', timeout: 30_000 })
    assert(duplicateSends === 0, `Duplicated tab issued ${duplicateSends} sends.`)
    await screenshot(second, `chat-duplicate-tab-${browserName}.png`)
    await second.close()
    markPassed('chat.duplicate-tab-consistency', browserName)
  } catch (error) {
    markFailed(
      'chat.duplicate-tab-consistency',
      browserName,
      String(error?.message ?? error),
    )
  }

  try {
    progress(`${browserName}: Offline-Senden und Recovery`)
    // The reload resets the composer model to the server default, a
    // known defect. Re-pin the cheap model deliberately so the offline
    // attempt and its retry stay on Nano.
    await page
      .locator(`[aria-label^="${MODEL_PICKER_LABEL}"]:visible`)
      .first()
      .click()
    const nanoRepin = page.getByRole('menuitem', { name: /nano/i })
      .or(page.getByRole('option', { name: /nano/i }))
      .or(page.getByRole('button', { name: /nano/i }))
    await nanoRepin.first().waitFor({ state: 'visible', timeout: 10_000 })
    await nanoRepin.first().click()
    await page.keyboard.press('Escape').catch(() => null)
    beforeOffline()
    state.offlineWindow = true
    await tester.context.setOffline(true)
    await composer.first().fill(offlineQuestion)
    await page.locator(`[aria-label="${SEND_LABEL}"]:visible`).first().click()
    // The failure must be VISIBLE — a badge, alert, or retry affordance —
    // and the typed question must not be silently lost.
    const feedback = page.getByText(SYNC_ERROR_LABEL).first()
      .or(page.getByRole('alert').first())
      .or(page.getByText(/fehlgeschlagen|erneut versuchen/i).first())
    await feedback.first().waitFor({ state: 'visible', timeout: 20_000 })
    const inputPreserved =
      (await composer.first().inputValue().catch(() => '')).includes('Offline')
      || (await page.getByText(offlineQuestion.slice(0, 40), { exact: false })
        .first().isVisible().catch(() => false))
    assert(inputPreserved, 'The offline question disappeared without feedback.')
    await screenshot(page, `chat-offline-${browserName}.png`)
    await tester.context.setOffline(false)
    state.offlineWindow = false
    afterOffline()
    // Recovery through the product's own in-place affordance: the failed
    // assistant turn offers "Nachrichtenoptionen → Erneut versuchen".
    // The retried completion must produce a REAL answer — the persisted
    // error turn never satisfies the non-error assistant assertion.
    const errorTurn = page.getByText('Chat-Anfrage fehlgeschlagen')
      .first()
    await errorTurn.waitFor({ state: 'visible', timeout: 10_000 })
    await errorTurn.hover()
    const options = page
      .locator('[aria-label="Nachrichtenoptionen"]:visible')
      .last()
    // The row's action bar is hover-revealed: prove the affordance is
    // actually reachable before clicking into it.
    await options.waitFor({ state: 'visible', timeout: 10_000 })
    await options.scrollIntoViewIfNeeded().catch(() => null)
    const retried = await awaitResponseFor(
      page,
      (response) =>
        response.request().method() === 'POST'
        && new URL(response.url()).pathname === '/v1/chat/completions',
      async () => {
        await options.click()
        // Anchor on the OPEN menu before reaching into it: a click that
        // silently did not open anything must fail here, not 30 s later
        // inside a locator that was never going to resolve.
        const menu = page.locator('[role="menu"]').last()
        await menu.waitFor({ state: 'visible', timeout: 10_000 })
        // Submenu trigger and preset item carry the SAME label; only the
        // trigger has aria-haspopup, and it opens on hover (not click).
        const subTrigger = menu
          .locator('[role="menuitem"][aria-haspopup]')
          .filter({ hasText: 'Erneut versuchen' })
          .first()
        await subTrigger.waitFor({ state: 'visible', timeout: 10_000 })
          .catch(async () => {
            await screenshot(page, `chat-retry-menu-${browserName}-state.png`)
          })
        await subTrigger.hover()
        const preset = page
          .locator('[role="menuitem"]:not([aria-haspopup])')
          .filter({ hasText: 'Erneut versuchen' })
          .first()
        await preset.waitFor({ state: 'visible', timeout: 10_000 })
        await preset.click()
      },
      30_000,
    )
    assert(
      retried.status() < 400,
      `Retry completion returned HTTP ${retried.status()}.`,
    )
    await waitForAssistantCount(tester, threadId, 2, 120_000)
    await screenshot(page, `chat-recovered-${browserName}.png`)
    markPassed('chat.network-recovery', browserName)
  } catch (error) {
    state.offlineWindow = false
    afterOffline()
    await tester.context.setOffline(false).catch(() => null)
    markFailed('chat.network-recovery', browserName, String(error?.message ?? error))
    await screenshot(page, `chat-offline-${browserName}-failed.png`)
  }

  try {
    progress(`${browserName}: Sichtbare Löschung`)
    const trigger = page
      .locator(`[aria-label="${DELETE_THREAD_LABEL}"]:visible`)
      .first()
    // The affordance is disabled without a selected thread — prove the
    // precondition instead of letting the click time out anonymously.
    await trigger.waitFor({ state: 'visible', timeout: 20_000 })
    assert(
      await trigger.isEnabled(),
      'The visible delete affordance is disabled; no thread is selected.',
    )
    const deleted = await awaitResponseFor(
      page,
      (response) =>
        response.request().method() === 'DELETE'
        && new URL(response.url()).pathname === `/v1/chat/threads/${threadId}`,
      async () => {
        await trigger.click()
        await confirmDialogIfPresent(page)
      },
      // The deletion travels through the 1.5 s debounced autosave flush.
      20_000,
    )
    assert(
      [200, 204].includes(deleted.status()),
      `Thread deletion returned HTTP ${deleted.status()}.`,
    )
    await waitFor(
      async () => {
        const listing = await fetchJson(tester, 'GET', '/v1/chat/threads')
        // Scope to THIS browser's thread: a leftover from another failed
        // pass is that pass's red cell, not a false negative here.
        const residue = listing.data?.filter(
          (candidate) => candidate.title?.startsWith(`${runKey} `)
            && candidate.title.includes(browserName),
        ) ?? []
        return residue.length === 0 ? true : null
      },
      20_000,
      'zero run-bound chat threads after visible deletion',
    )
    await screenshot(page, `chat-deleted-${browserName}.png`)
    markPassed('chat.cleanup-integrity', browserName)
  } catch (error) {
    markFailed('chat.cleanup-integrity', browserName, String(error?.message ?? error))
    await screenshot(page, `chat-deleted-${browserName}-failed.png`)
  }
}

async function runPromptCells(target, tester) {
  const { page } = tester
  const browserName = target.name
  const suffix = runKey.replaceAll(/[^a-z0-9]/gi, '').slice(-6).toLowerCase()
  const title = `${runKey} Prompt-Browsermatrix ${browserName}`
  const shortcut = `r09-${browserName.slice(0, 2)}-${suffix}`
  const body =
    `${runKey} Prompt-Inhalt fuer ${browserName}: Antworte sachlich.`
  let templateId = null

  try {
    progress(`${browserName}: Prompt Library CRUD`)
    await page.getByRole('button', { name: 'Prompt Library', exact: true })
      .click()
    await page.getByRole('button', { name: PROMPT_NEW_LABEL }).first().click()
    await page.getByPlaceholder(PROMPT_TITLE_PLACEHOLDER).first().fill(title)
    await page.getByPlaceholder(PROMPT_LABEL_PLACEHOLDER).first().fill(shortcut)
    await page.getByPlaceholder(PROMPT_BODY_PLACEHOLDER).first().fill(body)
    await page.getByRole('button', { name: PROMPT_SAVE_LABEL }).first().click()
    const template = await waitFor(
      async () => {
        const listing = await fetchJson(tester, 'GET', '/v1/prompt-templates')
        return (listing.data ?? []).find(
          (candidate) => candidate.title === title,
        ) ?? null
      },
      30_000,
      'run-titled prompt template in the sync store',
    )
    templateId = template.id
    const firstRevision = template.revision
    await lifecycle.register({
      credential: 'user',
      id: templateId,
      kind: 'prompt_template',
      ownerEmail: testerEmail,
      title,
    })
    // Edit → the optimistic revision must advance on the server.
    await page.getByPlaceholder(PROMPT_BODY_PLACEHOLDER).first()
      .fill(`${body} V2`)
    await page.getByRole('button', { name: PROMPT_SAVE_LABEL }).first().click()
    await waitFor(
      async () => {
        const listing = await fetchJson(tester, 'GET', '/v1/prompt-templates')
        const current = (listing.data ?? []).find(
          (candidate) => candidate.id === templateId,
        )
        return current && current.revision > firstRevision
          && current.content_markdown.includes('V2')
          ? current
          : null
      },
      30_000,
      'advanced template revision after the visible edit',
    )
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.getByRole('button', { name: 'Prompt Library', exact: true })
      .click()
    await page.getByPlaceholder(PROMPT_SEARCH_PLACEHOLDER).first()
      .fill(runKey)
    await page.getByText(title, { exact: false }).first()
      .waitFor({ state: 'visible', timeout: 20_000 })
    await screenshot(page, `prompt-crud-${browserName}.png`)
    markPassed('prompt.live-owner-crud', browserName)
  } catch (error) {
    markFailed('prompt.live-owner-crud', browserName, String(error?.message ?? error))
    await screenshot(page, `prompt-crud-${browserName}-failed.png`)
  }

  if (browserName === 'chromium') {
    try {
      progress(`${browserName}: Mobiler Viewport`)
      await page.setViewportSize({ height: 844, width: 390 })
      await page.reload({ waitUntil: 'domcontentloaded' })
      await page.getByRole('button', { name: 'Prompt Library', exact: true })
        .click()
      const mobileTitle = `${runKey} Prompt-Mobil`
      await page.getByRole('button', { name: PROMPT_NEW_LABEL }).first()
        .click()
      await page.getByPlaceholder(PROMPT_TITLE_PLACEHOLDER).first()
        .fill(mobileTitle)
      await page.getByPlaceholder(PROMPT_LABEL_PLACEHOLDER).first()
        .fill(`r09-mo-${suffix}`)
      await page.getByPlaceholder(PROMPT_BODY_PLACEHOLDER).first()
        .fill(`${runKey} Mobiler Inhalt.`)
      await page.getByRole('button', { name: PROMPT_SAVE_LABEL }).first()
        .click()
      const mobileTemplate = await waitFor(
        async () => {
          const listing = await fetchJson(tester, 'GET', '/v1/prompt-templates')
          return (listing.data ?? []).find(
            (candidate) => candidate.title === mobileTitle,
          ) ?? null
        },
        30_000,
        'mobile-created template in the sync store',
      )
      await lifecycle.register({
        credential: 'user',
        id: mobileTemplate.id,
        kind: 'prompt_template',
        ownerEmail: testerEmail,
        title: mobileTitle,
      })
      await screenshot(page, 'prompt-mobile-created.png')
      await deleteVisibleTemplate(page, tester, mobileTemplate.id, mobileTitle)
      await screenshot(page, 'prompt-mobile-deleted.png')
      markPassed('prompt.live-mobile-viewport', browserName)
    } catch (error) {
      markFailed(
        'prompt.live-mobile-viewport',
        browserName,
        String(error?.message ?? error),
      )
      await screenshot(page, 'prompt-mobile-failed.png')
    } finally {
      await page.setViewportSize({ height: 1000, width: 1440 })
      await page.reload({ waitUntil: 'domcontentloaded' })
      await page.getByRole('button', { name: 'Prompt Library', exact: true })
        .click()
    }
  } else {
    markPassed('prompt.live-mobile-viewport', browserName)
  }

  try {
    if (!templateId) throw new Error('No template to delete.')
    progress(`${browserName}: Prompt sichtbar löschen`)
    await deleteVisibleTemplate(page, tester, templateId, title)
    await waitFor(
      async () => {
        const listing = await fetchJson(tester, 'GET', '/v1/prompt-templates')
        // Scope to THIS browser's template: a leftover from another
        // failed cell keeps its own cell red instead of this one.
        const residue = (listing.data ?? []).filter(
          (candidate) => candidate.title === title,
        )
        return residue.length === 0 ? true : null
      },
      20_000,
      'zero run-bound prompt templates after visible deletion',
    )
    await screenshot(page, `prompt-deleted-${browserName}.png`)
    markPassed('prompt.cleanup-integrity', browserName)
  } catch (error) {
    markFailed('prompt.cleanup-integrity', browserName, String(error?.message ?? error))
    await screenshot(page, `prompt-deleted-${browserName}-failed.png`)
  }

}

async function deleteVisibleTemplate(page, tester, templateId, title) {
  // On the mobile layout the open editor covers the list — return to the
  // list first so the row is genuinely visible before it is clicked.
  const back = page.getByRole('button', { name: 'Zurück' }).first()
  if (await back.isVisible().catch(() => false)) {
    await back.click()
  }
  const search = page.getByPlaceholder(PROMPT_SEARCH_PLACEHOLDER).first()
  if (await search.isVisible().catch(() => false)) {
    await search.fill(runKey)
  }
  // Match the EXACT template title: with desktop and mobile templates
  // present at once, a prefix match could delete the wrong one.
  const row = page.getByText(title).first()
  await row.waitFor({ state: 'visible', timeout: 20_000 })
  await row.click()
  const deleted = await awaitResponseFor(
    page,
    (response) =>
      response.request().method() === 'DELETE'
      && new URL(response.url()).pathname
        === `/v1/prompt-templates/${templateId}`,
    async () => {
      await page.getByRole('button', { name: PROMPT_DELETE_LABEL }).first()
        .click()
      await confirmDialogIfPresent(page)
    },
    20_000,
  )
  assert(
    [200, 204].includes(deleted.status()),
    `Template deletion returned HTTP ${deleted.status()}.`,
  )
}

/** Arm the response wait BEFORE the action, but never leave it dangling:
 * an action that throws (a disabled or missing affordance) must surface
 * its own error instead of crashing the process with an unhandled
 * rejection once the wait later times out. */
async function awaitResponseFor(page, predicate, action, timeoutMs) {
  const waiter = page.waitForResponse(predicate, { timeout: timeoutMs })
  // Attach the handler immediately — this is the whole point.
  const settled = waiter.then(
    (response) => ({ response }),
    (error) => ({ error }),
  )
  await action()
  const outcome = await settled
  if (outcome.error) throw outcome.error
  return outcome.response
}

async function confirmDialogIfPresent(page) {
  // Some destructive actions confirm through a dialog; absence is fine.
  const dialog = page.getByRole('dialog')
  const appeared = await dialog.first()
    .waitFor({ state: 'visible', timeout: 1_500 })
    .then(() => true)
    .catch(() => false)
  if (!appeared) return
  await dialog.getByRole('button', { name: /löschen|bestätigen|delete/i })
    .first()
    .click()
}

async function waitForAssistantCount(tester, threadId, minimum, timeoutMs) {
  await waitFor(
    async () => {
      const messages = await fetchJson(
        tester,
        'GET',
        `/v1/chat/threads/${threadId}/messages`,
      )
      const assistants = (messages.data ?? []).filter(
        (message) => message.role === 'assistant'
          && message.content_markdown.trim().length > 0
          && !ASSISTANT_ERROR_PATTERN.test(message.content_markdown),
      )
      return assistants.length >= minimum ? true : null
    },
    timeoutMs,
    `${minimum} persisted non-error assistant answer(s)`,
  )
}

async function screenshot(page, name) {
  await page.screenshot({
    fullPage: true,
    path: join(screenshotDirectory, name),
  }).catch(() => null)
}

function escapeRegExp(value) {
  return value.replaceAll(/[.*+?^${}()|[\]\\]/g, String.raw`\$&`)
}

function requiredEnvironment(name) {
  const value = process.env[name]?.trim()
  if (!value) throw new Error(`${name} is required.`)
  return value
}

function progress(message) {
  console.log(`[chat-prompt-live] ${sanitize(message)}`)
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
