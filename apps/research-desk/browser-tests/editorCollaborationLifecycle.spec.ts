import { expect, test, type Route } from '@playwright/test'
import {
  EDITOR_SCHEMA_VERSION,
  editorYDocToJson,
  validateCanonicalYjsV1Update,
  validateEditorYDoc,
  validateSuggestionYjsUpdate,
} from '@inqtrix/editor-schema'
import * as Y from 'yjs'

import { validateSuggestionUpdate } from '../../collaboration-server/src/suggestPolicy'

const fixturePath = '/browser-tests/fixtures/editor-lifecycle.html'

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    // Where a fixture follows the app language, these specs assert its German
    // copy, so they state the language instead of inheriting the English
    // first-render fallback.
    window.localStorage.setItem('inqtrix.researchDesk.locale', 'de')
  })
  page.on('pageerror', (error) => {
    throw error
  })
})

test('modal focus wins the launcher teardown race and restores on Escape', async ({ page }) => {
  await page.goto(`${fixturePath}?scenario=modal-focus-race`)
  const launcher = page.getByTestId('modal-launcher')
  const preferred = page.getByRole('textbox', { name: 'Preferred modal control' })

  await launcher.click()
  await expect(preferred).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('dialog', { name: 'Focus race dialog' })).toBeHidden()
  await expect(launcher).toBeFocused()
})

test('editor share menu returns focus after Escape and click-outside', async ({ page }) => {
  await page.goto(`${fixturePath}?scenario=share-menu-focus`)
  const trigger = page.getByRole('button', {
    name: /^(Weitere Dokumentaktionen|More document actions)$/,
  })
  const shareMenuItem = page.getByRole('menuitem', {
    name: /^(Dokument teilen|Share document)$/,
  })
  const dialog = page.getByRole('dialog', {
    name: /^(Dokumentdetails|Document details)$/,
  })
  const search = page.getByRole('textbox', {
    name: /^(Personen suchen|Search people)/,
  })

  await trigger.click()
  await shareMenuItem.click()
  await expect(search).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(trigger).toBeFocused()

  await trigger.click()
  await shareMenuItem.click()
  await expect(search).toBeFocused()
  await page.mouse.click(10, 10)
  await expect(dialog).toBeHidden()
  await expect(trigger).toBeFocused()
})

test('explorer action tooltip yields while its modal owns interaction', async ({ page }) => {
  const consoleErrors: string[] = []
  const failedRequests: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text())
  })
  page.on('requestfailed', (request) => {
    failedRequests.push(`${request.method()} ${new URL(request.url()).pathname}`)
  })
  await page.setViewportSize({ height: 720, width: 1_200 })
  await page.goto(`${fixturePath}?scenario=explorer-action-modal`)
  const trigger = page.getByRole('button', {
    name: 'Dokumentdetails: Geteiltes Dokument.md',
  })
  const dialog = page.getByRole('dialog', { name: 'Dokumentdetails' })
  const detailsTooltip = page.getByRole('tooltip', { name: 'Dokumentdetails' })

  // The modal can open before the provider's 250ms tooltip delay expires.
  // A pending trigger timer must not surface after modal focus has moved.
  await trigger.focus()
  await trigger.press('Enter')
  await expect(dialog).toBeVisible()
  await expect(page.getByRole('tab', { name: 'Übersicht' })).toBeFocused()
  expect(await detailsTooltip.isVisible()).toBe(false)
  await page.waitForTimeout(350)
  await expect(detailsTooltip).toBeHidden()
  await page.keyboard.press('Escape')
  await expect(dialog).toBeHidden()
  await expect(trigger).toBeFocused()

  await page.mouse.move(600, 360)
  await trigger.hover()
  await expect(detailsTooltip).toBeVisible()
  await trigger.click()
  await expect(dialog).toBeVisible()
  expect(await detailsTooltip.isVisible()).toBe(false)
  await expect(detailsTooltip).toBeHidden()
  await page.mouse.click(10, 10)
  await expect(dialog).toBeHidden()
  await expect(trigger).toBeFocused()

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.goto(`${fixturePath}?scenario=explorer-action-modal`)
  await trigger.focus()
  await trigger.press('Enter')
  await expect(dialog).toBeVisible()
  expect(await detailsTooltip.isVisible()).toBe(false)
  await page.waitForTimeout(350)
  await expect(detailsTooltip).toBeHidden()
  await page.keyboard.press('Escape')

  const pinTrigger = page.getByRole('button', {
    name: 'Anheften: Lokales Dokument.md',
  })
  const pinTooltip = page.getByRole('tooltip', { name: 'Anheften' })
  await pinTrigger.focus()
  await expect(detailsTooltip).toBeHidden()
  await expect(pinTooltip).toBeVisible()
  await pinTrigger.press('Enter')
  await expect(page.getByTestId('explorer-pin-count')).toHaveText('1')
  expect(await pinTooltip.isVisible()).toBe(false)
  await expect(pinTrigger).toBeFocused()
  await page.mouse.move(600, 360)
  await pinTrigger.hover()
  await expect(pinTooltip).toBeVisible()
  expect(consoleErrors).toEqual([])
  expect(failedRequests).toEqual([])
})

test('long collaboration labels stay inside the mobile editor at both edges', async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ height: 844, width: 390 })
  await page.goto(`${fixturePath}?scenario=presence-label`)
  await page.evaluate(() => {
    localStorage.setItem('inqtrix.researchDesk.theme', 'light')
  })
  await page.reload()
  await expect(page.locator('html')).not.toHaveClass(/dark/)

  const verifyLayout = async (width: number, screenshotName: string) => {
    await expect(page.getByTestId('presence-demo-state')).toHaveText(
      'Demo-Modus eingeschaltet',
    )
    const boundary = page.getByTestId('presence-label-boundary')
    await expect(boundary).toHaveAttribute('data-labels-mounted', 'true')
    await expect(boundary.locator('[data-presence-sample]')).toHaveCount(4)

    await page.screenshot({
      animations: 'disabled',
      path: testInfo.outputPath(screenshotName),
    })

    const geometry = await boundary.evaluate((element) => {
      const boundaryRect = element.getBoundingClientRect()
      return {
        boundary: { left: boundaryRect.left, right: boundaryRect.right },
        documentScrollWidth: document.documentElement.scrollWidth,
        labels: [...element.querySelectorAll<HTMLElement>(
          '.inqtrix-collaboration-caret-label, .collaboration-carets__label',
        )].map((label) => {
          const rect = label.getBoundingClientRect()
          return {
            clientWidth: label.clientWidth,
            left: rect.left,
            right: rect.right,
            sample: label.parentElement?.dataset.presenceSample ?? 'unknown',
            scrollWidth: label.scrollWidth,
            side: label.dataset.collaborationLabelSide ?? 'unset',
          }
        }),
      }
    })

    expect(geometry.documentScrollWidth).toBeLessThanOrEqual(width)
    expect(geometry.labels).toHaveLength(4)
    for (const label of geometry.labels) {
      expect(label.left, `${width}px ${label.sample} left edge`).toBeGreaterThanOrEqual(
        geometry.boundary.left - 0.5,
      )
      expect(label.right, `${width}px ${label.sample} right edge`).toBeLessThanOrEqual(
        geometry.boundary.right + 0.5,
      )
      expect(label.scrollWidth, `${width}px ${label.sample} truncation`).toBeGreaterThan(
        label.clientWidth,
      )
      expect(label.side, `${width}px ${label.sample} alignment`).toBe(
        label.sample.endsWith('-right') ? 'right' : 'left',
      )
    }

    const stylesBeforeSettling = await boundary
      .locator('[data-presence-sample]')
      .evaluateAll((carets) => carets.map((caret) => caret.innerHTML))
    await page.evaluate(() => new Promise<void>((resolve) => {
      requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
    }))
    expect(
      await boundary
        .locator('[data-presence-sample]')
        .evaluateAll((carets) => carets.map((caret) => caret.innerHTML)),
    ).toEqual(stylesBeforeSettling)
  }

  for (const width of [390, 320, 195]) {
    await page.setViewportSize({ height: 844, width })
    if (width === 320) await page.emulateMedia({ reducedMotion: 'reduce' })
    await verifyLayout(width, `presence-label-${width}.png`)
  }

  await page.setViewportSize({ height: 844, width: 390 })
  await page.evaluate(() => {
    localStorage.setItem('inqtrix.researchDesk.theme', 'dark')
  })
  await page.reload()
  await expect(page.locator('html')).toHaveClass(/dark/)
  await verifyLayout(390, 'presence-label-390-dark.png')
})

test('profile menu suppresses its trigger tooltip while actions are open', async ({ page }) => {
  await page.setViewportSize({ height: 720, width: 1_200 })
  await page.goto(`${fixturePath}?scenario=profile-menu`)
  const trigger = page.getByRole('button', { name: 'Angemeldet' })

  await trigger.click()
  await expect(page.getByRole('menu', { name: 'Angemeldet' })).toBeVisible()
  // A delayed pointer move can land after the dropdown takes modal ownership.
  await page.evaluate(() => {
    const profileTrigger = document.querySelector<HTMLButtonElement>(
      'button[aria-label="Angemeldet"]',
    )
    if (!profileTrigger) throw new Error('Authenticated profile trigger is missing')
    profileTrigger.dispatchEvent(new PointerEvent('pointerleave', {
      bubbles: true,
      pointerType: 'mouse',
    }))
    profileTrigger.dispatchEvent(new PointerEvent('pointermove', {
      bubbles: true,
      pointerType: 'mouse',
    }))
  })
  await page.waitForTimeout(350)
  await expect(page.getByRole('tooltip')).toBeHidden()

  await page.getByRole('menuitem', { name: 'Abmelden' }).click()
  await expect(page.getByTestId('profile-logout-count')).toHaveText('1')
  await expect(trigger).toBeFocused()

  await page.mouse.move(600, 360)
  await trigger.hover()
  await expect(page.getByRole('tooltip')).toBeVisible()
})

test('A to B rerender never exposes A through B surface or controller registration', async ({ page }) => {
  const pendingSessions = new Map<string, Route>()
  await page.route('**/v1/editor/documents/*/collaboration/session', async (route) => {
    const match = new URL(route.request().url()).pathname.match(/documents\/([^/]+)\/collaboration/)
    if (!match) return route.abort()
    pendingSessions.set(match[1], route)
  })

  await page.goto(`${fixturePath}?scenario=switch`)
  const state = page.getByTestId('switch-state')
  const surface = page.getByTestId('switch-surface')
  await expect(state).toHaveAttribute('data-requested-document-id', 'doc-a')
  await expect(surface.locator('.ProseMirror')).toContainText('Projection DOC-A')
  await expect(surface.locator('.ProseMirror')).toHaveAttribute('contenteditable', 'false')
  await expect.poll(() => pendingSessions.has('doc-a')).toBe(true)

  await page.getByTestId('switch-document').click()
  await expect(state).toHaveAttribute('data-requested-document-id', 'doc-b')
  await expect(state).toHaveAttribute('data-handle-document-id', 'doc-b')
  await expect(surface.locator('.ProseMirror')).toContainText('Projection DOC-B')
  await expect(surface.locator('.ProseMirror')).not.toContainText('Projection DOC-A')
  await expect(surface.locator('.ProseMirror')).toHaveAttribute('contenteditable', 'false')
  await expect(state).toHaveAttribute('data-binding-document-id', 'none')
  await expect(state).toHaveAttribute('data-registered-editor-id', 'doc-b')
  await expect.poll(() => pendingSessions.has('doc-b')).toBe(true)

  const snapshots = await page.evaluate(() => window.__collaborationRenderSnapshots ?? [])
  expect(snapshots).toContainEqual(expect.objectContaining({
    controllerRegistered: false,
    handleDocumentId: 'doc-b',
    lifecycleStatus: 'inactive',
    requestedDocumentId: 'doc-b',
  }))
  expect(snapshots.some((snapshot) => (
    snapshot.requestedDocumentId === 'doc-b' && snapshot.handleDocumentId === 'doc-a'
  ))).toBe(false)

  await pendingSessions.get('doc-a')!.fulfill({
    contentType: 'application/json',
    json: {
      access: 'edit',
      expires_at: Math.floor(Date.now() / 1000) + 3600,
      initial_write_mode: 'edit',
      lease_token: 'late-a-lease',
      protocol_version: 1,
      room: 'browser-fixture:doc-a:g1',
      schema_version: EDITOR_SCHEMA_VERSION,
      user: { color: '#2563EB', id: 'test-user', name: 'Test User' },
      websocket_path: '/collaboration',
    },
  })
  await page.waitForTimeout(50)
  await expect(state).toHaveAttribute('data-requested-document-id', 'doc-b')
  await expect(state).toHaveAttribute('data-handle-document-id', 'doc-b')
  await expect(state).toHaveAttribute('data-registered-editor-id', 'doc-b')
  await expect(surface.locator('.ProseMirror')).not.toContainText('Projection DOC-A')
})

test('activation success closes the writable body window before delayed exact hydration', async ({ page }) => {
  await page.goto(`${fixturePath}?scenario=activation`)
  const state = page.getByTestId('activation-state')
  const editor = page.getByTestId('activation-surface').locator('.ProseMirror')

  await expect(state).toHaveAttribute('data-content-mode', 'markdown')
  await expect(state).toHaveAttribute('data-autosave-kind', 'legacy_body')
  await expect(editor).toHaveAttribute('contenteditable', 'true')
  await expect(editor).toContainText('Writable legacy body')

  await page.getByTestId('activation-success').click()
  await expect(state).toHaveAttribute('data-content-mode', 'collaboration')
  await expect(state).toHaveAttribute('data-autosave-kind', 'collaboration_metadata')
  await expect(state).toHaveAttribute('data-document-updated-at', '2099-01-01T00:00:00.000Z')
  await expect(editor).toHaveAttribute('contenteditable', 'false')
  await expect(editor).toContainText('Writable legacy body')
  const postActivationChanges = await state.getAttribute('data-body-change-count')
  await editor.click()
  await page.keyboard.type(' must not persist')
  await expect(state).toHaveAttribute('data-body-change-count', postActivationChanges!)
  await expect(state).toHaveAttribute('data-autosave-kind', 'collaboration_metadata')
  await expect(editor).not.toContainText('must not persist')

  await page.getByTestId('detail-success').click()
  await expect(state).toHaveAttribute('data-document-updated-at', '2026-07-15T08:05:00.000Z')
  await expect(editor).toHaveAttribute('contenteditable', 'false')
  await expect(editor).toContainText('Exact hydrated projection')
  await expect(editor).not.toContainText('Writable legacy body')
})

test('narrow topbar keeps long title and four-participant status in separate bounded tracks', async ({ page }) => {
  await page.setViewportSize({ height: 800, width: 360 })
  await page.goto(`${fixturePath}?scenario=topbar`)

  const status = page.getByRole('status')
  await expect(status).toHaveAccessibleName(/4 Teilnehmende: Ada Lovelace, Lin Chen, Max Weber, Zoe Smith/)
  await expect(status.locator('[data-participant-id]')).toHaveCount(3)
  await expect(status).toContainText('+1')

  const geometry = await page.evaluate(() => {
    const rect = (selector: string) => {
      const value = document.querySelector<HTMLElement>(selector)!.getBoundingClientRect()
      return { left: value.left, right: value.right, width: value.width }
    }
    const title = document.querySelector<HTMLElement>('[data-testid="long-title"]')!
    return {
      actions: rect('[data-editor-topbar-actions]'),
      header: rect('[data-editor-topbar]'),
      leading: rect('[data-editor-topbar-leading]'),
      titleClipped: title.scrollWidth > title.clientWidth,
      toolbar: rect('[data-editor-topbar-toolbar]'),
    }
  })

  expect(geometry.header.left).toBeGreaterThanOrEqual(0)
  expect(geometry.header.right).toBeLessThanOrEqual(360)
  expect(geometry.leading.left).toBeGreaterThanOrEqual(geometry.header.left)
  expect(geometry.leading.right).toBeLessThanOrEqual(geometry.toolbar.left + 0.5)
  expect(geometry.toolbar.right).toBeLessThanOrEqual(geometry.actions.left + 0.5)
  expect(geometry.actions.right).toBeLessThanOrEqual(geometry.header.right)
  expect(geometry.titleClipped).toBe(true)
})

test('save status suppresses acknowledgement flicker and keeps its label track stable', async ({ page }) => {
  type LabelRecord = { at: number; text: string }
  type RecordingWindow = typeof window & { __statusLabels?: LabelRecord[] }
  await page.goto(`${fixturePath}?scenario=status-hysteresis`)
  const label = page.locator('[data-editor-status-label]')
  await expect(label).toHaveText('Gespeichert')
  const savedWidth = await label.evaluate((element) => element.getBoundingClientRect().width)
  await page.evaluate(() => {
    const element = document.querySelector('[data-editor-status-label]')
    const labels = [{ at: performance.now(), text: element?.textContent ?? '' }]
    ;(window as RecordingWindow).__statusLabels = labels
    // React rewrites a lone text child in place, which is a characterData
    // mutation; without it the observer would miss every label change.
    new MutationObserver(() => labels.push({
      at: performance.now(),
      text: element?.textContent ?? '',
    })).observe(element!, {
      characterData: true,
      childList: true,
      subtree: true,
    })
  })
  const labelTexts = async () => (
    (await page.evaluate(() => (window as RecordingWindow).__statusLabels ?? []))
      .map((record) => record.text)
  )

  await page.getByTestId('quick-save-pulse').click()
  await page.waitForTimeout(800)
  await expect(label).toHaveText('Gespeichert')
  expect(await labelTexts()).toEqual(['Gespeichert'])

  // Five seconds of typing whose every change is confirmed after 300 ms:
  // the label never changes.
  await page.getByTestId('typing-save-pulses').click()
  await page.waitForTimeout(5_600)
  await expect(label).toHaveText('Gespeichert')
  expect(await labelTexts()).toEqual(['Gespeichert'])

  // A change that waits 2.6 s: "Wird gespeichert" appears once it has
  // waited 2 s, and "Gespeichert" returns with its confirmation.
  const clickedAt = await page.evaluate(() => performance.now())
  await page.getByTestId('slow-save-pulse').click()
  await expect(label).toHaveText('Wird gespeichert', { timeout: 3_000 })
  const savingWidth = await label.evaluate((element) => element.getBoundingClientRect().width)
  expect(savingWidth).toBe(savedWidth)
  await expect(label).toHaveText('Gespeichert', { timeout: 2_000 })
  const records = await page.evaluate(() => (window as RecordingWindow).__statusLabels ?? [])
  expect(records.map((record) => record.text))
    .toEqual(['Gespeichert', 'Wird gespeichert', 'Gespeichert'])
  expect(records[1]!.at - clickedAt).toBeGreaterThanOrEqual(1_900)
})

test('the unsaved state of a replaced generation is announced on its document and opens as a recovery copy', async ({ page }) => {
  await page.goto(`${fixturePath}?scenario=recovery-copy-notice`)
  const active = page.getByTestId('active-document')
  const notice = page.locator('[data-collaboration-recovery-copy]')
  await expect(active).toHaveAttribute('data-document-id', 'recovery-original')
  await expect(notice).toHaveCount(0)

  await page.getByTestId('capture-recovery').click()
  await expect(notice).toHaveAttribute('role', 'status')
  await expect(notice).toContainText(
    'Ihr nicht gespeicherter Stand wurde als Wiederherstellungskopie abgelegt.',
  )
  // The original stays active and unchanged; the notice fits its column.
  await expect(active).toHaveAttribute('data-document-id', 'recovery-original')
  await expect(page.getByTestId('active-markdown')).toContainText('Bestätigter Stand.')
  expect(await notice.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true)

  await notice.getByRole('button', { name: 'Kopie öffnen' }).click()
  await expect(active).not.toHaveAttribute('data-document-id', 'recovery-original')
  await expect(page.locator('[data-editor-recovery-artifact="generation_replaced"]')).toBeVisible()
  await expect(page.getByTestId('active-markdown')).toContainText('Unbestätigter lokaler Satz.')
  await expect(notice).toHaveCount(0)

  // Once the copy is decided, the original no longer points to it.
  await page.getByRole('button', { name: 'Verwerfen' }).click()
  await page.getByTestId('open-original').click()
  await expect(active).toHaveAttribute('data-document-id', 'recovery-original')
  await expect(notice).toHaveCount(0)
})

test('initial and same-event Suggest policies govern the first document transaction', async ({ page }) => {
  await page.goto(`${fixturePath}?scenario=initial-suggest-policy`)

  const initial = page.getByTestId('initial-suggest-editor')
  await expect(initial.locator('ins[data-suggestion-id]')).toContainText('immediate')

  await page.goto(`${fixturePath}?scenario=view-policy`)
  const viewOnly = page.getByTestId('initial-view-editor').locator('.ProseMirror')
  await expect(viewOnly).toContainText('View')
  await expect(viewOnly).not.toContainText('blocked')

  await page.goto(`${fixturePath}?scenario=mode-switch-policy`)
  const switched = page.getByTestId('mode-switch-editor')
  await expect(switched.locator('ins[data-suggestion-id]')).toHaveCount(0)
  await page.getByTestId('switch-to-suggest').click()
  await expect(switched.locator('ins[data-suggestion-id]')).toContainText('switched')
})

test('rejected Suggest undo emits no Yjs mutation and remains retryable', async ({ page }) => {
  await page.goto(`${fixturePath}?scenario=suggestion-undo-policy`)
  const state = page.getByTestId('suggestion-undo-state')
  const editor = page.getByTestId('suggestion-undo-editor').locator('.ProseMirror')
  await editor.click()
  await page.keyboard.press('Control+End')
  await page.keyboard.type(' tracked')
  await expect(editor.locator('ins[data-suggestion-id]')).toContainText('tracked')
  await expect(state).not.toHaveAttribute('data-update-count', '0')
  const updatesBeforeUndo = await state.getAttribute('data-update-count')

  const shortcut = await page.evaluate(() => (
    /Mac|iPhone|iPad|iPod/.test(navigator.platform) ? 'Meta+z' : 'Control+z'
  ))
  await page.keyboard.press(shortcut)

  await expect(state).toHaveAttribute('data-undo-attempts', '1')
  await expect(state).not.toHaveAttribute('data-undo-patch-id', 'none')
  await expect(editor.locator('ins[data-suggestion-id]')).toContainText('tracked')
  await expect(state).toHaveAttribute('data-update-count', updatesBeforeUndo!)

  await page.waitForTimeout(50)
  await page.keyboard.press(shortcut)
  await expect(state).toHaveAttribute('data-undo-attempts', '2')
  await expect(editor.locator('ins[data-suggestion-id]')).toContainText('tracked')
  await expect(state).toHaveAttribute('data-update-count', updatesBeforeUndo!)
})

test('Simple Markup reveals every active compound suggestion and hides other deletions', async ({ page }) => {
  await page.goto(`${fixturePath}?scenario=simple-markup`)
  const markup = page.getByTestId('simple-markup-editor')
  const activeDeletion = markup.locator('[data-review-suggestion-id="delete-active"]')
  const activeInsertion = markup.locator('[data-review-suggestion-id="insert-active"]')
  const inactiveDeletion = markup.locator('[data-review-suggestion-id="delete-inactive"]')

  await expect(activeDeletion).toHaveAttribute('data-review-active', 'true')
  await expect(activeInsertion).toHaveAttribute('data-review-active', 'true')
  await expect(activeDeletion).not.toHaveCSS('display', 'none')
  await expect(inactiveDeletion).not.toHaveAttribute('data-review-active', 'true')
  await expect(inactiveDeletion).toHaveCSS('display', 'none')

  await expect(page.getByTestId('canonical-source')).toHaveText('new')
  const canonicalDiff = page.getByTestId('canonical-diff')
  await expect(canonicalDiff.locator('.editor-document-diff-token-delete')).toHaveText('old')
  await expect(canonicalDiff.locator('.editor-document-diff-token-insert')).toHaveText('new')
})

test('slash heading creates a server-valid collaboration document', async ({ page }) => {
  await page.goto(`${fixturePath}?scenario=slash-collaboration`)
  const editor = page.getByTestId('slash-collaboration-editor').locator('.ProseMirror')
  await expect(editor).toContainText('Before')

  const initialEncoded = await page.evaluate(() => window.__slashCollaborationUpdate ?? [])
  await editor.click()
  await page.keyboard.press('Control+End')
  await page.keyboard.press('Enter')
  await page.keyboard.type('/')
  await expect(page.getByRole('button', { name: 'Überschrift 1' })).toBeVisible()

  const materializeDocument = async () => {
    const encoded = await page.evaluate(() => window.__slashCollaborationUpdate ?? [])
    const document = new Y.Doc()
    Y.applyUpdate(document, Uint8Array.from(encoded))
    return document
  }
  const before = await materializeDocument()
  expect(() => validateEditorYDoc(before)).not.toThrow()
  before.destroy()

  await page.getByRole('button', { name: 'Überschrift 1' }).click()
  await expect(editor.locator('h1')).toBeVisible()
  const after = await materializeDocument()
  expect(() => validateEditorYDoc(after)).not.toThrow()
  after.destroy()

  const serverDocument = new Y.Doc()
  Y.applyUpdate(serverDocument, Uint8Array.from(initialEncoded))
  const incrementalUpdates = await page.evaluate(
    () => window.__slashCollaborationUpdates ?? [],
  )
  expect(incrementalUpdates.length).toBeGreaterThan(0)
  for (const encoded of incrementalUpdates) {
    const update = Uint8Array.from(encoded)
    expect(() => validateCanonicalYjsV1Update(update)).not.toThrow()
    Y.applyUpdate(serverDocument, update)
    expect(() => validateEditorYDoc(serverDocument)).not.toThrow()
  }
  serverDocument.destroy()
})

test('all eleven slash actions keep the collaboration document schema-valid', async ({ page }) => {
  const matrix = [
    { label: 'Absatz', selector: 'p', minimum: 2 },
    { label: 'Überschrift 1', selector: 'h1' },
    { label: 'Überschrift 2', selector: 'h2' },
    { label: 'Überschrift 3', selector: 'h3' },
    { label: 'Aufzählung', selector: 'ul:not([data-type="taskList"])' },
    { label: 'Nummerierte Liste', selector: 'ol' },
    { label: 'Aufgabenliste', selector: 'ul[data-type="taskList"]' },
    { label: 'Zitat', selector: 'blockquote' },
    { label: 'Codeblock', selector: 'pre' },
    { label: 'Tabelle', selector: 'table' },
    { label: 'Trennlinie', selector: 'hr' },
  ]

  for (const action of matrix) {
    await page.goto(`${fixturePath}?scenario=slash-collaboration`)
    const editor = page.getByTestId('slash-collaboration-editor').locator('.ProseMirror')
    await editor.click()
    await page.keyboard.press('Control+End')
    await page.keyboard.press('Enter')
    await page.keyboard.type('/')
    const item = page.getByRole('button', { name: action.label })
    await expect(item).toBeEnabled()
    await item.click()
    await expect(editor.locator(action.selector)).toHaveCount(
      action.minimum ?? 1,
    )

    const encoded = await page.evaluate(
      () => window.__slashCollaborationUpdate ?? [],
    )
    const document = new Y.Doc()
    Y.applyUpdate(document, Uint8Array.from(encoded))
    expect(() => validateEditorYDoc(document)).not.toThrow()
    document.destroy()
  }
})

test('slash search, keyboard selection, escape, and suggest guards are coherent', async ({ page }) => {
  await page.goto(`${fixturePath}?scenario=slash-collaboration`)
  const editor = page.getByTestId('slash-collaboration-editor').locator('.ProseMirror')
  await editor.click()
  await page.keyboard.press('Control+End')
  await page.keyboard.press('Enter')
  await page.keyboard.type('/h2')
  await expect(page.getByRole('button', { name: 'Überschrift 2' })).toBeVisible()
  await page.keyboard.press('Enter')
  await expect(editor.locator('h2')).toBeVisible()

  await page.keyboard.press('Control+End')
  await page.keyboard.press('Enter')
  await page.keyboard.type('/')
  await expect(page.getByRole('button', { name: 'Absatz' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('button', { name: 'Absatz' })).not.toBeVisible()

  await page.goto(`${fixturePath}?scenario=slash-collaboration&mode=suggest`)
  const suggestEditor = page
    .getByTestId('slash-collaboration-editor')
    .locator('.ProseMirror')
  await suggestEditor.click()
  await page.keyboard.press('Control+End')
  await page.keyboard.press('Enter')
  await page.keyboard.type('/')
  const table = page.getByRole('button', { name: 'Tabelle' })
  const divider = page.getByRole('button', { name: 'Trennlinie' })
  await expect(table).toBeDisabled()
  await expect(divider).toBeDisabled()
  await expect(table).toContainText('Nur im Modus Bearbeiten verfügbar')
  await expect(divider).toContainText('Nur im Modus Bearbeiten verfügbar')
  await expect(page.getByRole('button', { name: 'Überschrift 1' })).toBeEnabled()
})

test('all reversible slash suggestions emit complete server-valid Yjs V1 updates', async ({ page }) => {
  const matrix = [
    'Absatz',
    'Überschrift 1',
    'Überschrift 2',
    'Überschrift 3',
    'Aufzählung',
    'Nummerierte Liste',
    'Aufgabenliste',
    'Zitat',
    'Codeblock',
  ]

  for (const label of matrix) {
    const sourceKind = label === 'Absatz' ? 'heading' : 'paragraph'
    await page.goto(
      `${fixturePath}?scenario=slash-collaboration&mode=suggest&source=${sourceKind}`,
    )
    const editor = page
      .getByTestId('slash-collaboration-editor')
      .locator('.ProseMirror')
    const sourceSelector = label === 'Absatz' ? 'h2' : 'p'
    const target = editor.locator(sourceSelector).filter({ hasText: 'Suggest target' })
    await expect(target).toBeVisible()
    const initialEncoded = await page.evaluate(
      () => window.__slashCollaborationServerUpdate ?? [],
    )
    const serverDocument = new Y.Doc()
    Y.applyUpdate(serverDocument, Uint8Array.from(initialEncoded))
    const sourceElement = serverDocument.getXmlFragment('content').get(1)
    if (!(sourceElement instanceof Y.XmlElement)) {
      throw new Error('Slash suggestion fixture has no source element')
    }
    const sourceText = sourceElement.toArray().find((item) => item instanceof Y.XmlText)
    if (!(sourceText instanceof Y.XmlText)) {
      throw new Error('Slash suggestion fixture has no source text anchor')
    }
    const sourceAnchor = Y.createRelativePositionFromTypeIndex(sourceText, 1)

    await target.evaluate((element) => {
      const range = document.createRange()
      range.selectNodeContents(element)
      range.collapse(true)
      const selection = window.getSelection()
      selection?.removeAllRanges()
      selection?.addRange(range)
      ;(element as HTMLElement).focus()
      document.dispatchEvent(new Event('selectionchange'))
    })
    await page.keyboard.type('/')
    const item = page.getByRole('button', { name: label })
    await expect(item).toBeEnabled()
    await item.dispatchEvent('mousedown', { button: 0, buttons: 1 })
    await expect(item).not.toBeVisible()

    const incrementalUpdates = await page.evaluate(
      () => window.__slashCollaborationUpdates ?? [],
    )
    expect(incrementalUpdates.length).toBeGreaterThan(0)
    const updates = incrementalUpdates.map((encoded) => Uint8Array.from(encoded))
    const structureDetections = await page.evaluate(
      () => window.__slashStructureUpdateDetections ?? [],
    )
    const suggestionBoundaries = await page.evaluate(
      () => window.__slashSuggestionBoundaryDetections ?? [],
    )
    expect(structureDetections).toHaveLength(updates.length)
    expect(suggestionBoundaries).toHaveLength(updates.length)
    expect(structureDetections).toContain(true)
    for (const [index, update] of updates.entries()) {
      expect(() => validateCanonicalYjsV1Update(update)).not.toThrow()
      expect(() => validateSuggestionYjsUpdate(update)).not.toThrow()
      const beforeDocument = new Y.Doc()
      Y.applyUpdate(beforeDocument, Y.encodeStateAsUpdate(serverDocument))
      const afterDocument = new Y.Doc()
      Y.applyUpdate(afterDocument, Y.encodeStateAsUpdate(serverDocument))
      Y.applyUpdate(afterDocument, update)
      expect(
        () => validateSuggestionUpdate(
          editorYDocToJson(beforeDocument),
          editorYDocToJson(afterDocument),
          'edit',
          '11111111-1111-4111-8111-111111111111',
          { afterDocument, beforeDocument },
        ),
        `raw browser update ${index + 1} (structure=${String(structureDetections[index])})`,
      ).not.toThrow()
      Y.applyUpdate(serverDocument, update)
      beforeDocument.destroy()
      afterDocument.destroy()
    }

    const batches: Uint8Array[][] = []
    let pending: Uint8Array[] = []
    let pendingHasSuggestionBoundary = false
    for (const [index, update] of updates.entries()) {
      if (
        pending.length > 0
        && (
          structureDetections[index]
          || (
            suggestionBoundaries[index]
            && !pendingHasSuggestionBoundary
          )
        )
      ) {
        batches.push(pending)
        pending = []
        pendingHasSuggestionBoundary = false
      }
      pending.push(update)
      pendingHasSuggestionBoundary = (
        pendingHasSuggestionBoundary
        || suggestionBoundaries[index] === true
      )
    }
    if (pending.length > 0) batches.push(pending)

    const transportDocument = new Y.Doc()
    const policyDocument = new Y.Doc()
    Y.applyUpdate(transportDocument, Uint8Array.from(initialEncoded))
    Y.applyUpdate(policyDocument, Uint8Array.from(initialEncoded))
    const localTransportOrigin = Symbol('local-transport')
    for (const batch of batches) {
      let transportedUpdate: Uint8Array | null = null
      const captureTransportUpdate = (update: Uint8Array, origin: unknown) => {
        if (origin === localTransportOrigin) transportedUpdate = Uint8Array.from(update)
      }
      transportDocument.on('update', captureTransportUpdate)
      try {
        Y.applyUpdate(
          transportDocument,
          Y.mergeUpdates(batch),
          localTransportOrigin,
        )
      } finally {
        transportDocument.off('update', captureTransportUpdate)
      }
      if (!transportedUpdate) continue
      const beforeDocument = new Y.Doc()
      const afterDocument = new Y.Doc()
      Y.applyUpdate(beforeDocument, Y.encodeStateAsUpdate(policyDocument))
      Y.applyUpdate(afterDocument, Y.encodeStateAsUpdate(policyDocument))
      Y.applyUpdate(afterDocument, transportedUpdate)
      expect(() => validateSuggestionUpdate(
        editorYDocToJson(beforeDocument),
        editorYDocToJson(afterDocument),
        'edit',
        '11111111-1111-4111-8111-111111111111',
        { afterDocument, beforeDocument },
      )).not.toThrow()
      Y.applyUpdate(policyDocument, transportedUpdate)
      beforeDocument.destroy()
      afterDocument.destroy()
    }
    expect(editorYDocToJson(policyDocument)).toEqual(editorYDocToJson(serverDocument))
    expect(Y.createAbsolutePositionFromRelativePosition(
      sourceAnchor,
      policyDocument,
    )).not.toBeNull()
    expect(Y.createAbsolutePositionFromRelativePosition(
      sourceAnchor,
      serverDocument,
    )).not.toBeNull()
    expect(() => validateEditorYDoc(serverDocument)).not.toThrow()
    transportDocument.destroy()
    policyDocument.destroy()
    serverDocument.destroy()
  }
})

test('60-thread inspector stays compact, progressive, and single-composer', async ({ page }) => {
  await page.setViewportSize({ height: 900, width: 400 })
  await page.goto(`${fixturePath}?scenario=comment-scale`)
  const panel = page.getByTestId('comment-scale-panel')
  await expect(panel.locator('[data-team-comment-id]')).toHaveCount(50)
  await expect.poll(
    () => page.evaluate(() => window.__commentScaleFirstInteractiveMs),
  ).toBeLessThan(750)
  await expect(panel.locator('textarea')).toHaveCount(0)

  await panel.locator('[data-team-comment-id]').nth(20).click()
  await expect(panel.locator('textarea')).toHaveCount(1)
  await expect.poll(
    () => page.evaluate(() => window.__commentScaleSelectionMs),
  ).toBeLessThan(100)

  await panel.getByRole('button', { name: /Weitere laden|Load more/ }).click()
  await expect(panel.locator('[data-team-comment-id]')).toHaveCount(55)
  await expect(panel.locator('textarea')).toHaveCount(1)

  for (const width of [400, 360, 320]) {
    await page.setViewportSize({ height: 900, width })
    const geometry = await panel.evaluate((element) => ({
      clientWidth: element.clientWidth,
      scrollWidth: element.scrollWidth,
    }))
    expect(geometry.scrollWidth).toBeLessThanOrEqual(geometry.clientWidth)
  }
})

test('view-only assistant controls explain the restriction and never dispatch work', async ({ page }) => {
  await page.goto(`${fixturePath}?scenario=view-ai`)

  const privateControls = page.getByTestId('private-suggestion-controls')
  const batchInspector = page.getByTestId('batch-inspector')
  const accept = privateControls.getByRole('button', { exact: true, name: 'Accept' })
  const acceptAll = privateControls.getByRole('button', { exact: true, name: 'Accept all' })
  const reject = privateControls.getByRole('button', { exact: true, name: 'Reject' })
  const rejectAll = privateControls.getByRole('button', { exact: true, name: 'Reject all' })
  await expect(accept).toBeEnabled()
  await expect(acceptAll).toBeEnabled()
  await expect(reject).toBeEnabled()
  await expect(rejectAll).toBeEnabled()
  const replaced = page.getByText('The open suggestion will be replaced by the new one.', { exact: true })
  await expect(replaced).toBeVisible()
  await page.locator('[data-editor-topbar-overflow]').click()
  await expect(page.getByRole('menuitem', { exact: true, name: 'Set comparison anchor' }))
    .toBeEnabled()
  await page.keyboard.press('Escape')

  await batchInspector.getByRole('button', { name: /^(Accept all|Alle annehmen)$/ }).click()
  const batchDialog = page.getByRole('dialog')
  await expect(batchDialog).toBeVisible()
  await expect(batchDialog.getByRole('button', { name: /^(Accept all|Alle annehmen)$/ })).toBeEnabled()

  await page.getByTestId('downgrade-ai-access').evaluate((button: HTMLButtonElement) => {
    button.click()
  })
  await expect(batchDialog).toBeHidden()
  await expect(page.getByRole('note')).toContainText('AI editing is unavailable with view-only access.')
  await expect(accept).toBeDisabled()
  await expect(acceptAll).toBeDisabled()
  await expect(reject).toBeDisabled()
  await expect(rejectAll).toBeDisabled()
  // No new suggestion can come, so none is announced as replacing it.
  await expect(replaced).toHaveCount(0)
  await page.locator('[data-editor-topbar-overflow]').click()
  await expect(page.getByRole('menuitem', {
    exact: true,
    name: 'This collaboration access is read-only.',
  })).toBeDisabled()
  await page.keyboard.press('Escape')
  const send = page.getByRole('button', { name: 'Send' })
  await expect(send).toBeDisabled()
  const composer = page.locator(
    '.mention-composer-prose[aria-label="Describe what should change in this document..."]',
  )
  await composer.press('Enter')
  await expect(page.getByTestId('ai-invocations')).toHaveText('0')
  await expect(page.getByTestId('anchor-invocations')).toHaveText('0')
  await expect(page.getByTestId('decision-invocations')).toHaveText('0')
  await expect(page.getByTestId('publish-invocations')).toHaveText('0')
})

test('the editor assistant answer stays with the document it was asked in', async ({ page }) => {
  // Nothing on this page may reach a server; the assistant requests are held
  // here and answered by the test.
  await page.route(
    (url) => url.pathname.startsWith('/v1/') || url.pathname.startsWith('/api/'),
    (route) => route.fulfill({ json: {}, status: 404 }),
  )
  const requests: { instruction: string; markdown: string; route: Route }[] = []
  await page.route(
    (url) => url.pathname === '/v1/editor/instruct',
    (route) => {
      const body = route.request().postDataJSON() as { document_markdown: string; instruction: string }
      requests.push({ instruction: body.instruction, markdown: body.document_markdown, route })
    },
  )
  const answer = (index: number, message: string, find: string, text: string) => (
    requests[index]!.route.fulfill({
      json: {
        assistant_message: message,
        edits: [{ find, note: 'Geändert', position: 'replace', quote_after: '', quote_before: '', text }],
        warnings: [],
      },
    })
  )
  const firstAnswer = 'Die Ersetzung in Dokument A ist vorbereitet.'
  const secondAnswer = 'Der gekürzte Satz in Dokument A ist vorbereitet.'

  await page.goto(`${fixturePath}?scenario=instruction-answer-switch`)
  const activeDocument = page.getByTestId('active-document')
  const composer = page.getByRole('textbox', {
    name: 'Beschreiben Sie, was am Dokument geändert werden soll...',
  })
  const send = page.getByRole('button', { exact: true, name: 'Senden' })
  const answered = page.getByText('Antwort · 1', { exact: true })
  const thinking = page.getByText('Denke nach', { exact: true })
  const openA = page.getByRole('button', { name: 'Dokument A öffnen' })
  const openB = page.getByRole('button', { name: 'Dokument B öffnen' })
  await expect(activeDocument).toHaveAttribute('data-document-id', 'answer-document-a')
  await expect(composer).toBeVisible()

  // An answer that arrived in A stays in A.
  await composer.click()
  await page.keyboard.type('Ersetze ca. durch etwa.')
  await send.click()
  await expect.poll(() => requests.length).toBe(1)
  expect(requests[0]!.instruction).toBe('Ersetze ca. durch etwa.')
  expect(requests[0]!.markdown).toContain('# Dokument A')
  await expect(thinking).toBeVisible()
  await answer(0, firstAnswer, 'ca. sechs Wochen', 'etwa sechs Wochen')
  await expect(answered).toBeVisible()
  await expect(page.getByText(firstAnswer)).toBeVisible()
  await expect(page.getByTestId('suggestions-a')).toHaveText('1')

  await openB.click()
  await expect(activeDocument).toHaveAttribute('data-document-id', 'answer-document-b')
  await expect(composer).toBeVisible()
  await expect(answered).toHaveCount(0)
  await expect(page.getByText(firstAnswer)).toHaveCount(0)

  await openA.click()
  await expect(activeDocument).toHaveAttribute('data-document-id', 'answer-document-a')
  await expect(answered).toBeVisible()
  await expect(page.getByText(firstAnswer)).toBeVisible()

  // A request asked in A that answers while B is open shows neither its
  // progress nor its answer in B, and its answer waits in A.
  await composer.click()
  await page.keyboard.type('Kürze den Satz.')
  await send.click()
  await expect.poll(() => requests.length).toBe(2)
  expect(requests[1]!.instruction).toBe('Kürze den Satz.')
  expect(requests[1]!.markdown).toContain('# Dokument A')
  await expect(thinking).toBeVisible()

  await openB.click()
  await expect(activeDocument).toHaveAttribute('data-document-id', 'answer-document-b')
  await expect(thinking).toHaveCount(0)
  await answer(1, secondAnswer, 'Der Bau dauert ca. sechs Wochen.', 'Der Bau dauert sechs Wochen.')
  // The run is over: its suggestion is in A, and the composer offers Send again.
  await expect(page.getByTestId('suggestions-a')).toHaveText('2')
  await expect(page.getByTestId('suggestions-b')).toHaveText('0')
  await expect(send).toBeVisible()
  await expect(thinking).toHaveCount(0)
  await expect(answered).toHaveCount(0)
  await expect(page.getByText(secondAnswer)).toHaveCount(0)

  await openA.click()
  await expect(activeDocument).toHaveAttribute('data-document-id', 'answer-document-a')
  await expect(answered).toBeVisible()
  await expect(page.getByText(secondAnswer)).toBeVisible()
})

test('an open AI suggestion is named before a new instruction replaces it, in its own document only', async ({ page }) => {
  // Nothing on this page may reach a server; each assistant request gets the
  // next answer in order.
  await page.route(
    (url) => url.pathname.startsWith('/v1/') || url.pathname.startsWith('/api/'),
    (route) => route.fulfill({ json: {}, status: 404 }),
  )
  const answers = [
    [['ca. sechs Wochen', 'etwa sechs Wochen']],
    [['Dokument A', 'Dokument Alpha'], ['ca. sechs Wochen', 'rund sechs Wochen']],
  ]
  await page.route(
    (url) => url.pathname === '/v1/editor/instruct',
    (route) => route.fulfill({
      json: {
        assistant_message: 'Die Änderung ist vorbereitet.',
        edits: answers.shift()!.map(([find, text]) => ({
          find,
          note: 'Geändert',
          position: 'replace',
          quote_after: '',
          quote_before: '',
          text,
        })),
        warnings: [],
      },
    }),
  )

  await page.goto(`${fixturePath}?scenario=instruction-answer-switch`)
  const composer = page.getByRole('textbox', {
    name: 'Beschreiben Sie, was am Dokument geändert werden soll...',
  })
  const send = page.getByRole('button', { exact: true, name: 'Senden' })
  const replacesOne = page.getByText('Der offene Vorschlag wird durch den neuen ersetzt.', { exact: true })
  const replacesMany = page.getByText('Die offenen Vorschläge werden durch die neuen ersetzt.', { exact: true })
  await expect(composer).toBeVisible()
  await expect(replacesOne).toHaveCount(0)

  await composer.click()
  await page.keyboard.type('Ersetze ca. durch etwa.')
  await send.click()
  await expect(page.getByTestId('suggestions-a')).toHaveText('1')
  await expect(replacesOne).toBeVisible()
  await expect(replacesOne).toHaveAttribute('role', 'status')

  // The line belongs to the document whose suggestion it names.
  await page.getByRole('button', { name: 'Dokument B öffnen' }).click()
  await expect(page.getByTestId('active-document')).toHaveAttribute('data-document-id', 'answer-document-b')
  await expect(replacesOne).toHaveCount(0)
  await page.getByRole('button', { name: 'Dokument A öffnen' }).click()
  await expect(replacesOne).toBeVisible()

  // The next answer does replace it; its two suggestions share one line.
  await composer.click()
  await page.keyboard.type('Benenne das Dokument um und runde.')
  await send.click()
  await expect(page.getByTestId('suggestions-a')).toHaveText('3')
  await expect(replacesMany).toBeVisible()
  await expect(replacesOne).toHaveCount(0)

  // A decided suggestion is no longer open, and the line leaves with it.
  await page.getByRole('button', { name: 'Offene Vorschläge ablehnen' }).click()
  await expect(replacesMany).toHaveCount(0)
  await expect(replacesOne).toHaveCount(0)
})

test('the editor assistant says under its input that answers come from an AI system, also while typing', async ({ page }) => {
  await page.route(
    (url) => url.pathname.startsWith('/v1/') || url.pathname.startsWith('/api/'),
    (route) => route.fulfill({ json: {}, status: 404 }),
  )
  await page.goto(`${fixturePath}?scenario=instruction-answer-switch`)
  const composer = page.getByRole('textbox', {
    name: 'Beschreiben Sie, was am Dokument geändert werden soll...',
  })
  const disclosure = page.getByText(
    'Antworten werden von einem KI-System erzeugt. Bitte überprüfen Sie die Angaben.',
    { exact: true },
  )
  await expect(disclosure).toBeVisible()
  const input = (await composer.boundingBox())!
  expect((await disclosure.boundingBox())!.y).toBeGreaterThanOrEqual(input.y + input.height)

  await composer.click()
  await page.keyboard.type('Kürze den Satz.')
  await expect(disclosure).toBeVisible()
})
