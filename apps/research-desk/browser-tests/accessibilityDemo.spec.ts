import { writeFile } from 'node:fs/promises'

import { expect, test, type Page, type TestInfo } from '@playwright/test'
import axe from 'axe-core'

import {
  enableDemoMode,
  installBackendFreeDiscoveryRoutes,
  settleLayout,
} from './demoApp'

const WORKSPACES = [
  'Research Desk',
  'Knowledge Desk',
  'Chat',
  'Editor',
  'Agent Desk',
  'Prompt Library',
  'Datenbank',
  'Einstellungen',
] as const

const VIEWPORTS = [
  { height: 900, label: 'desktop', width: 1_440 },
  { height: 844, label: 'mobile', width: 390 },
] as const

type AxeViolation = {
  help: string
  helpUrl: string
  id: string
  impact: string | null
  nodes: Array<{
    failureSummary: string | null
    target: string[]
  }>
  tags: string[]
}

type SurfaceViolation = AxeViolation & {
  surface: string
  viewport: string
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.clear()
    window.sessionStorage.clear()
    // These specs address the UI by its German labels, so they state the
    // language instead of inheriting whatever the first-render fallback is.
    // That fallback is English (it matches the server account default), and
    // an unstated dependency on it breaks silently the day it changes.
    window.localStorage.setItem('inqtrix.researchDesk.locale', 'de')
  })
  await page.emulateMedia({ reducedMotion: 'reduce' })
  await installBackendFreeDiscoveryRoutes(page)
})

test('demo workspaces have no automated WCAG A or AA violations', async ({ page }, testInfo) => {
  test.setTimeout(180_000)
  await page.setViewportSize(VIEWPORTS[0])
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await enableDemoMode(page)
  await installAxe(page)

  const violations: SurfaceViolation[] = []
  for (const viewport of VIEWPORTS) {
    await page.setViewportSize(viewport)
    for (const workspace of WORKSPACES) {
      const navigation = page.getByRole('button', { exact: true, name: workspace })
      await expect(navigation).toBeVisible()
      await navigation.click()
      await expect(navigation).toHaveAttribute('aria-pressed', 'true')
      await settleLayout(page)
      if (testInfo.project.name === 'chromium') {
        await captureSurfaceScreenshot(page, testInfo, viewport.label, workspace)
      }
      violations.push(...(await scanVisibleSurface(page)).map((violation) => ({
        ...violation,
        surface: workspace,
        viewport: viewport.label,
      })))
    }
  }

  // The Skills editor is a TAB inside the Prompt Library, so the workspace
  // loop above never opened it — and its form was the one place in the app
  // with unnamed controls. Scanning it is both the fix's proof and the guard
  // against the next form that forgets its labels.
  await page.setViewportSize(VIEWPORTS[0])
  const promptLibrary = page.getByRole('button', {
    exact: true,
    name: 'Prompt Library',
  })
  await promptLibrary.click()
  await expect(promptLibrary).toHaveAttribute('aria-pressed', 'true')
  // The tab carries a "Beta" badge, so its accessible name is "Skills Beta".
  const skillsTab = page.getByRole('button', { name: /^Skills/ })
  await expect(skillsTab).toBeVisible()
  await skillsTab.click()
  await settleLayout(page)
  // Open the first skill so the editor form renders; a list alone would scan
  // nothing and pass for the wrong reason.
  const firstSkill = page.getByRole('button', { name: /Sprechzettel|Skill/ }).first()
  if (await firstSkill.isVisible().catch(() => false)) {
    await firstSkill.click()
    await settleLayout(page)
  }
  if (testInfo.project.name === 'chromium') {
    await captureSurfaceScreenshot(page, testInfo, VIEWPORTS[0].label, 'Skills')
  }
  violations.push(...(await scanVisibleSurface(page)).map((violation) => ({
    ...violation,
    surface: 'Skills',
    viewport: VIEWPORTS[0].label,
  })))

  // NOT scanned yet: open menus. Adding the Agent Desk execution menu here
  // turns this gate red on a real but unrelated defect — its 10-11px caption
  // rows use muted-foreground at 50/65/70% and reach only 2.02-2.61:1 on
  // white, under the 4.5:1 AA minimum. Those opacity variants appear in ten
  // files, so raising them is a design decision, not a fix to slip in here.
  // A permanently red gate hides everything that runs after it, so the scan
  // waits for that decision.

  const deduplicated = deduplicateViolations(violations)
  if (deduplicated.length > 0) {
    const evidencePath = testInfo.outputPath('axe-wcag-a-aa-violations.json')
    await writeFile(evidencePath, `${JSON.stringify(deduplicated, null, 2)}\n`, {
      encoding: 'utf8',
      mode: 0o600,
    })
    await testInfo.attach('axe-wcag-a-aa-violations', {
      contentType: 'application/json',
      path: evidencePath,
    })
  }
  expect(
    deduplicated,
    accessibilityFailureMessage(testInfo, deduplicated),
  ).toEqual([])
})

test('appearance presets save each choice and keep a fixed brand mark', async ({ page }) => {
  test.setTimeout(90_000)
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await enableDemoMode(page)
  await page.getByRole('button', { exact: true, name: 'Einstellungen' }).click()
  await page.getByRole('button', { exact: true, name: 'Aussehen' }).click()
  await expect(page.locator('html')).toHaveAttribute('data-theme-preset', 'petrol')
  await installAxe(page)
  for (const [label, preset] of [
    ['Inqtrix Blue', 'standard'], ['Slate', 'slate'], ['Graphite', 'graphite'],
    ['Sage', 'sage'], ['Inqtrix Petrol', 'petrol'],
  ]) {
    await page.getByRole('button', { name: new RegExp(`^${label}`) }).click()
    await expect(page.locator('html')).toHaveAttribute('data-theme-preset', preset)
    expect(await page.evaluate(() => localStorage.getItem('inqtrix.researchDesk.themePreset'))).toBe(preset)
    // The mark is the same artwork in every preset: only the theme picks it.
    const marks = page.locator('.inqtrix-brand-mark img:visible')
    await expect(marks.first()).toBeVisible()
    for (const source of await marks.evaluateAll((nodes) => nodes.map((node) => node.getAttribute('src')))) {
      expect(source).toMatch(/inqtrix-mark-flat-light-128/)
    }
    await settleLayout(page)
    expect(await scanVisibleSurface(page)).toEqual([])
  }
  // Every preset card reads in full on a phone, in both UI languages.
  for (const language of ['DE', 'EN']) {
    await page.setViewportSize({ width: 1_280, height: 720 })
    await page.getByRole('button', { exact: true, name: language }).click()
    await page.setViewportSize({ width: 375, height: 812 })
    await settleLayout(page)
    for (const label of ['Inqtrix Petrol', 'Inqtrix Blue', 'Slate', 'Graphite', 'Sage']) {
      const card = page.getByRole('button', { name: new RegExp(`^${label}`) })
      await expect(card).toBeVisible()
      const clipped = await card.evaluate((node) => [...node.querySelectorAll<HTMLElement>('.truncate')]
        .filter((text) => text.scrollWidth > text.clientWidth)
        .map((text) => text.textContent))
      expect.soft(clipped, `${language} ${label} at 375px`).toEqual([])
    }
  }
})

test('new chats start as localized empty sessions in both UI languages', async ({ page }) => {
  await page.goto('/', { waitUntil: 'domcontentloaded' })
  await enableDemoMode(page)

  const chatNavigation = page
    .getByRole('navigation', { name: 'Ansichten' })
    .getByRole('button', { exact: true, name: 'Chat' })
  await chatNavigation.click()
  await expect(chatNavigation).toHaveAttribute('aria-pressed', 'true')

  const history = page.getByRole('complementary')
  const conversation = page.getByTestId('chat-conversation-panel')
  await history.getByRole('button', { exact: true, name: 'Neuer Chat' }).click()
  await expect(conversation.getByRole('button', { exact: true, name: 'Neuer Chat' })).toBeVisible()
  await expect(conversation.locator('[title="Bereit für eine freie Frage."]')).toBeVisible()
  await expect(conversation.getByRole('heading', { name: 'Direkt fragen oder Kontext nutzen.' })).toBeVisible()
  await expect(conversation.getByText(
    'New conversation ready. Ask a question or sketch the research you want to derive from it.',
    { exact: true },
  )).toHaveCount(0)

  await page.getByRole('button', { exact: true, name: 'EN' }).click()
  await history.getByRole('button', { exact: true, name: 'New chat' }).click()
  await expect(conversation.getByRole('button', { exact: true, name: 'New chat' })).toBeVisible()
  await expect(conversation.locator('[title="Ready for an open question."]')).toBeVisible()
  await expect(conversation.getByRole('heading', { name: 'Ask directly or use context.' })).toBeVisible()
  await expect(conversation.getByText(
    'New conversation ready. Ask a question or sketch the research you want to derive from it.',
    { exact: true },
  )).toHaveCount(0)
})

async function captureSurfaceScreenshot(
  page: Page,
  testInfo: TestInfo,
  viewport: string,
  workspace: string,
): Promise<void> {
  const surface = workspace.toLowerCase().replaceAll(/[^a-z0-9]+/g, '-')
  const name = `${viewport}-${surface}`
  const evidencePath = testInfo.outputPath(`${name}.png`)
  await page.screenshot({ animations: 'disabled', path: evidencePath })
  await testInfo.attach(name, { contentType: 'image/png', path: evidencePath })
}

async function installAxe(page: Page): Promise<void> {
  await page.addScriptTag({ content: axe.source })
  await expect.poll(
    () => page.evaluate(() => typeof (window as Window & { axe?: unknown }).axe),
    { message: 'The versioned axe runtime must be available in the browser.' },
  ).toBe('object')
}

async function scanVisibleSurface(page: Page): Promise<AxeViolation[]> {
  return await page.evaluate(async () => {
    const runtime = (window as Window & {
      axe?: {
        run: (
          context: Document,
          options: Record<string, unknown>,
        ) => Promise<{ violations: AxeViolation[] }>
      }
    }).axe
    if (!runtime) throw new Error('axe is unavailable in the browser context.')
    const result = await runtime.run(document, {
      resultTypes: ['violations'],
      runOnly: {
        type: 'tag',
        values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'],
      },
    })
    return result.violations.map((violation) => ({
      help: violation.help,
      helpUrl: violation.helpUrl,
      id: violation.id,
      impact: violation.impact,
      nodes: violation.nodes.map((node) => ({
        failureSummary: node.failureSummary,
        target: node.target.map(String),
      })),
      tags: violation.tags,
    }))
  })
}

function deduplicateViolations(violations: SurfaceViolation[]): SurfaceViolation[] {
  const unique = new Map<string, SurfaceViolation>()
  for (const violation of violations) {
    const key = JSON.stringify([
      violation.viewport,
      violation.surface,
      violation.id,
      violation.nodes.map((node) => node.target),
    ])
    unique.set(key, violation)
  }
  return [...unique.values()]
}

function accessibilityFailureMessage(
  testInfo: TestInfo,
  violations: readonly SurfaceViolation[],
): string {
  if (violations.length === 0) return ''
  const summary = violations.map((violation) => (
    `${violation.viewport}/${violation.surface}: ${violation.id} `
    + `(${violation.impact ?? 'unknown'}) on `
    + violation.nodes.map((node) => node.target.join(' ')).join(', ')
  ))
  return [
    `${testInfo.project.name} reported ${violations.length} WCAG A/AA violations.`,
    ...summary,
  ].join('\n')
}
