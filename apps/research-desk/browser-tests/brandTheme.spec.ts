import { expect, test, type Locator } from '@playwright/test'
import axe from 'axe-core'

const fixture = '/browser-tests/fixtures/auth-brand.html'
const presets = ['petrol', 'standard', 'slate', 'graphite', 'sage'] as const

test('lock screen stays usable in each authentication mode, language and viewport', async ({ page }, testInfo) => {
  test.setTimeout(120_000)
  for (const mode of ['local', 'sso', 'apikey']) {
    for (const locale of ['en', 'de']) {
      for (const theme of ['light', 'dark']) {
        // Each theme meets both widths across the two languages.
        const mobile = (theme === 'dark') !== (locale === 'de')
        await page.setViewportSize({ width: mobile ? 390 : 1440, height: 900 })
        await page.goto(`${fixture}?mode=${mode}&locale=${locale}&theme=${theme}&error`)
        await expect(page.getByRole('dialog')).toBeVisible()
        await expect(page.getByRole('alert')).toBeVisible()
        await expect(page.locator('html')).toHaveAttribute('data-theme-preset', 'petrol')
        const mark = page.locator(`.inqtrix-brand-mark-${theme}:visible`)
        await expect(mark).toHaveCount(1)
        // The artwork arrives after the mark is laid out, so the reading waits for it.
        await expect.poll(() => mark.evaluate((node: HTMLImageElement) => node.complete && node.naturalWidth > 0)).toBe(true)
        expect(await page.getByRole('dialog').evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true)
        const action = page.locator('.inqtrix-auth-action')
        await expect(action).toHaveCSS('height', '44px')
        await expect(action).toHaveCSS('background-color', theme === 'light' ? 'rgb(19, 37, 54)' : 'rgb(113, 200, 205)')
        // Check the real auth surface, including notices and error text.
        await page.addScriptTag({ content: axe.source })
        const violations = await page.evaluate(async () => (
          await window.axe.run(document, { runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] } })
        ).violations)
        expect(violations).toEqual([])
        if (testInfo.project.name === 'chromium') {
          await page.screenshot({ path: testInfo.outputPath(`auth-${mode}-${locale}-${theme}.png`), fullPage: true })
        }
        if (mode !== 'sso') {
          if (mode === 'local') {
            await page.locator('#inqtrix-auth-identifier').fill('test@example.invalid')
            await page.locator('#inqtrix-auth-password').fill('synthetic-test-password')
          } else {
            await page.locator('#inqtrix-auth-token').fill('synthetic-test-token')
          }
          await action.focus()
          await page.keyboard.press('Enter')
          await expect(action).toBeDisabled()
        }
      }
    }
  }
})

test('keyboard focus stays visible on brand-filled lock screen controls in every preset', async ({ browserName, page }) => {
  test.setTimeout(90_000)
  // WebKit on macOS moves Tab focus only between text fields; Alt adds buttons.
  const tab = browserName === 'webkit' && process.platform === 'darwin' ? 'Alt+Tab' : 'Tab'
  for (const preset of presets) {
    for (const theme of ['light', 'dark']) {
      await page.goto(`${fixture}?mode=local&locale=en&themePreset=${preset}&theme=${theme}`)
      const identifier = page.locator('#inqtrix-auth-identifier')
      await expect(identifier).toBeFocused()
      // The pressed language button sits right before the form and carries
      // the brand fill; the sign-in action follows the password field.
      await page.keyboard.press(`Shift+${tab}`)
      const language = page.getByRole('button', { exact: true, name: 'EN' })
      await expect(language).toHaveAttribute('aria-pressed', 'true')
      await expect(language).toBeFocused()
      await expectRingDistinctFromFill(language, `${preset} ${theme} language`)
      await page.keyboard.press(tab)
      await expect(identifier).toBeFocused()
      await page.keyboard.press(tab)
      await page.keyboard.press(tab)
      const action = page.locator('.inqtrix-auth-action')
      await expect(action).toBeFocused()
      await expectRingDistinctFromFill(action, `${preset} ${theme} sign-in`)
    }
  }
})

test('every preset keeps the fixed logo and high contrast overrides the lock screen', async ({ page }) => {
  test.setTimeout(90_000)
  for (const preset of presets) {
    for (const theme of ['light', 'dark']) {
      await page.goto(`${fixture}?themePreset=${preset}&theme=${theme}`)
      await expect(page.locator(`.inqtrix-brand-mark-${theme}:visible`)).toHaveCount(1)
      await expect(page.locator(`.inqtrix-brand-mark-${theme}:visible`)).toHaveAttribute('src', theme === 'light' ? /inqtrix-mark-lightmode-128/ : /inqtrix-mark-darkmode-128/)
      await page.goto(`${fixture}?themePreset=${preset}&theme=${theme}&contrastMode=high`)
      await expect(page.locator('.inqtrix-brand-mark img:visible')).toHaveCount(1)
      await expect(page.locator(`.inqtrix-brand-mark-mono-${theme}`)).toBeVisible()
      await expect(page.locator(`.inqtrix-brand-mark-mono-${theme}`)).toHaveAttribute('src', theme === 'light' ? /inqtrix-mark-mono-navy-128/ : /inqtrix-mark-mono-white-128/)
      await expect(page.locator('.inqtrix-auth-fade')).toHaveCSS('background-image', 'none')
      await expect(page.locator('.inqtrix-auth-action')).toHaveCSS('background-color', theme === 'light' ? 'oklch(0 0 0)' : 'oklch(1 0 0)')
      await page.addScriptTag({ content: axe.source })
      expect(await page.evaluate(async () => (await window.axe.run(document)).violations)).toEqual([])
    }
  }
})


test('owner setup and guest access use a crisp 24px mark', async ({ page }, testInfo) => {
  for (const theme of ['light', 'dark']) {
    await page.setViewportSize({ width: 390, height: 844 })
    await page.goto(`${fixture}?view=setup&theme=${theme}&locale=de`)
    await expect(page.locator('.inqtrix-brand-mark')).toHaveCSS('width', '24px')
    await expect(page.locator(`.inqtrix-brand-mark-${theme}`)).toBeVisible()
    await expect(page.locator(`.inqtrix-brand-mark-${theme}`)).toHaveAttribute('src', theme === 'light' ? /inqtrix-mark-flat-light-128/ : /inqtrix-mark-flat-dark-128/)
    expect(await page.getByRole('dialog').evaluate((node) => node.scrollWidth <= node.clientWidth)).toBe(true)
    if (testInfo.project.name === 'chromium') {
      await page.screenshot({ path: testInfo.outputPath(`owner-${theme}.png`) })
    }
    // A synthetic read-only guest session renders the actual document header.
    // All guest API traffic is intercepted; no backend or sidecar is contacted.
    await page.route('**/v1/**', (route) => {
      const path = new URL(route.request().url()).pathname
      const body = path === '/v1/editor/guest/session'
        ? { document: { id: 'brand-preview', title: 'Shared research', content_markdown: '',
            comment_revision: 0, generation: 1, persisted_sequence: 0, projection_sequence: 0 },
            guest: { id: 'preview-guest', display_name: 'Preview', link_label: 'Brand preview' },
            permission: 'view', expires_at: 2000000000 }
        : path.startsWith('/v1/editor/share-links/')
          ? { document_title: 'Shared research', label: 'Brand preview', password_required: true,
              permission: 'view', expires_at: 2000000000 }
          : null
      return route.fulfill({ status: body ? 200 : 503, contentType: 'application/json', body: JSON.stringify(body ?? {}) })
    })
    await page.goto('/s/brand-verification')
    await expect(page.locator('.inqtrix-brand-mark')).toHaveCSS('width', '24px')
    await expect(page.locator(`.inqtrix-brand-mark-${theme}`)).toBeVisible()
    await expect(page.locator(`.inqtrix-brand-mark-${theme}`)).toHaveAttribute('src', theme === 'light' ? /inqtrix-mark-flat-light-128/ : /inqtrix-mark-flat-dark-128/)
    if (testInfo.project.name === 'chromium') {
      await page.screenshot({ path: testInfo.outputPath(`guest-${theme}.png`) })
    }
  }
})

// A focus ring is one layer of the computed box-shadow: no offset, no blur and
// a positive spread. It must be drawn and must not share the control's fill,
// or the focused control only looks one pixel larger.
async function expectRingDistinctFromFill(control: Locator, label: string) {
  const { fill, focusVisible, rings } = await control.evaluate(async (node) => {
    // Read the settled ring, not a frame of the focus transition.
    await Promise.all(node.getAnimations().map((animation) => animation.finished))
    const style = getComputedStyle(node)
    const layers = style.boxShadow === 'none' ? [] : style.boxShadow.split(/,(?![^(]*\))/)
    return {
      fill: style.backgroundColor,
      focusVisible: node.matches(':focus-visible'),
      rings: layers.flatMap((layer) => {
        const lengths = [...layer.matchAll(/(-?[\d.]+)px/g)].map((match) => Number(match[1]))
        const color = layer.replace(/-?[\d.]+px/g, '').replace('inset', '').trim()
        const [x, y, blur, spread] = lengths
        const transparent = /^rgba\(.*,\s*0\)$/.test(color) || color === 'transparent'
        return x === 0 && y === 0 && blur === 0 && spread > 0 && !transparent ? [color] : []
      }),
    }
  })
  expect(focusVisible, `${label}: focus-visible`).toBe(true)
  expect(rings, `${label}: drawn focus ring`).toHaveLength(1)
  expect.soft(rings[0], `${label}: ring colour against fill ${fill}`).not.toBe(fill)
}
