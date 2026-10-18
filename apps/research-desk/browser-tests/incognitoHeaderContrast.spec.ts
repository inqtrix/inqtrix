import { expect, test, type Page } from '@playwright/test'

import { enableDemoMode, installBackendFreeDiscoveryRoutes } from './demoApp'

// The incognito header repaints the palette for its own scope. A control in
// it that paints with a token the header does not repaint keeps the page
// colour under the header's text colour: the inactive mode segment of the
// Knowledge Desk was near-white on near-white in light mode and near-black
// on near-black in dark mode. Readable means WCAG AA for its small text,
// 4.5:1, measured on the rendered pixels of each segment.
const THEMES = ['light', 'dark'] as const

/** Contrast of a rendered segment: background = an interior pixel at its
 * start, text = the interior pixel farthest from it. The interior skips the
 * clip edge, which can carry anti-aliased pixels of the neighbouring segment. */
async function renderedContrast(page: Page, label: string) {
  const segment = page.locator('.inqtrix-chat-header--incognito button', { hasText: label }).first()
  const png = (await segment.screenshot()).toString('base64')
  return page.evaluate(async (data) => {
    const image = new Image()
    image.src = `data:image/png;base64,${data}`
    await image.decode()
    const canvas = document.createElement('canvas')
    canvas.width = image.width
    canvas.height = image.height
    const context = canvas.getContext('2d', { willReadFrequently: true })!
    context.drawImage(image, 0, 0)
    const pixels = context.getImageData(0, 0, image.width, image.height).data
    const at = (x: number, y: number) => {
      const index = (y * image.width + x) * 4
      return [pixels[index], pixels[index + 1], pixels[index + 2]]
    }
    const inset = 3
    const background = at(inset, Math.floor(image.height / 2))
    let text = background
    let distance = -1
    for (let y = inset; y < image.height - inset; y += 1) {
      for (let x = inset; x < image.width - inset; x += 1) {
        const pixel = at(x, y)
        const d = pixel.reduce((sum, value, i) => sum + Math.abs(value - background[i]), 0)
        if (d > distance) {
          distance = d
          text = pixel
        }
      }
    }
    const luminance = (rgb: number[]) => {
      const [r, g, b] = rgb.map((value) => {
        const c = value / 255
        return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4
      })
      return 0.2126 * r + 0.7152 * g + 0.0722 * b
    }
    const [lighter, darker] = [luminance(text), luminance(background)].sort((a, b) => b - a)
    return {
      background: `rgb(${background.join(', ')})`,
      contrast: (lighter + 0.05) / (darker + 0.05),
      text: `rgb(${text.join(', ')})`,
    }
  }, png)
}

for (const theme of THEMES) {
  test(`the mode switch stays readable in an incognito Knowledge Desk header (${theme})`, async ({ page }) => {
    await page.addInitScript((mode) => {
      window.localStorage.clear()
      window.sessionStorage.clear()
      // These specs address the UI by its German labels.
      window.localStorage.setItem('inqtrix.researchDesk.locale', 'de')
      window.localStorage.setItem('inqtrix.researchDesk.theme', mode)
    }, theme)
    await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' })
    await installBackendFreeDiscoveryRoutes(page)
    await page.goto('/', { waitUntil: 'domcontentloaded' })
    await enableDemoMode(page)
    const navigation = page.getByRole('button', { exact: true, name: 'Knowledge Desk' })
    await navigation.click()
    await expect(navigation).toHaveAttribute('aria-pressed', 'true')
    await page.getByRole('button', { exact: true, name: 'Inkognito fragen' }).click()
    await expect(page.locator('.inqtrix-chat-header--incognito')).toBeVisible()
    await page.mouse.move(0, 0)

    for (const label of ['Fragen', 'Finden']) {
      const reading = await renderedContrast(page, label)
      expect(
        reading.contrast,
        `${label}: ${reading.text} on ${reading.background}`,
      ).toBeGreaterThanOrEqual(4.5)
    }
  })
}
