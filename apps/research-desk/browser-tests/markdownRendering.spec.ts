import { expect, test, type Page, type TestInfo } from '@playwright/test'

import {
  MERMAID_ADAPTIVE_DIAGRAMS,
  MERMAID_FONT_SIZE,
  MERMAID_MAX_FIGURE_HEIGHT,
  MERMAID_PLOT_SIZE,
  MERMAID_STYLED_CLASSES,
  MERMAID_SUPPORTED_DIAGRAMS,
} from '../src/components/markdown/mermaidTheme'

const FIXTURE = '/browser-tests/fixtures/markdown-rendering.html'
/** The eight promised grammars, then the two paired plots of the sample. */
const EXPECTED_TYPES = [...MERMAID_SUPPORTED_DIAGRAMS, 'xychart-beta', 'xychart-beta']
const DIAGRAMS = EXPECTED_TYPES.length
const RENDER_TIMEOUT = 60_000

type Query = {
  contrast?: 'standard' | 'high'
  extra?: 'directive'
  theme: 'light' | 'dark'
  variant: 'chat' | 'report'
}

/** Colour probes read in the page: the rendered SVG against the tokens. */
type DiagramProbe = {
  chart1: string
  flowchartLinkStroke: string
  flowchartLinkStrokeAlpha: number
  foreground: string
  pieSliceFills: string[]
  styleFontSize: string | null
  svgBackground: string
  svgInlineBackground: string
  textFill: string
  xyChartViewBox: string | null
}

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    window.localStorage.clear()
    window.sessionStorage.clear()
    // The render-error check below reads the German error copy, so the spec
    // states the language instead of inheriting the English first-render
    // fallback.
    window.localStorage.setItem('inqtrix.researchDesk.locale', 'de')
  })
  page.on('pageerror', (error) => {
    throw error
  })
})

test('rendering sample set draws every construct bounded, transparent and themed', async ({ page }, testInfo) => {
  test.setTimeout(240_000)

  await test.step('chat, light: every construct renders inside the column', async () => {
    await page.setViewportSize({ height: 900, width: 1_280 })
    await openSample(page, { theme: 'light', variant: 'chat' })

    const types = await page
      .locator('[data-testid="mermaid-figure"]')
      .evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-diagram-type')))
    expect(types).toEqual(EXPECTED_TYPES)

    const column = await page.locator('[data-fixture-body]').boundingBox()
    if (!column) throw new Error('fixture column has no box')
    for (const svg of await page.locator('[data-testid="mermaid-figure"] svg[role="img"]').all()) {
      const box = await svg.boundingBox()
      if (!box) throw new Error('rendered diagram has no box')
      expect(box.width).toBeLessThanOrEqual(column.width + 1)
    }
    // The visible figure is capped; a taller picture scrolls inside it.
    for (const scroller of await page.locator('[data-testid="mermaid-scroller"]').all()) {
      const box = await scroller.boundingBox()
      if (!box) throw new Error('figure scroller has no box')
      expect(box.height).toBeLessThanOrEqual(MERMAID_MAX_FIGURE_HEIGHT + 1)
    }
    // Every diagram is named after its accTitle.
    await expect(page.locator('figure[data-diagram-type="flowchart"] svg[role="img"]'))
      .toHaveAttribute('aria-label', 'Ablauf einer Störungsmeldung')
    await expect(page.locator('figure[data-diagram-type="xychart-beta"][data-layout="single"] svg[role="img"]'))
      .toHaveAttribute('aria-label', 'Fördermenge je Monat als Ist und Plan')

    const probe = await readDiagramProbe(page)
    expect(probe.svgBackground).toBe('rgba(0, 0, 0, 0)')
    expect(probe.svgInlineBackground).toBe('')
    expectSameColor(probe.textFill, probe.foreground)
    expect(probe.styleFontSize).toBe('13px')
    // A lone plot draws on its full 4:3 area; a paired plot on the width of
    // its cell — and neither is ever scaled: their labels stay 13 px.
    expect(probe.xyChartViewBox).toBe(`0 0 ${MERMAID_PLOT_SIZE.width} ${MERMAID_PLOT_SIZE.height}`)
    const row = page.locator('[data-figure-row]')
    await expect(row).toHaveCount(1)
    const paired = row.locator('figure[data-layout="half"] svg[role="img"]')
    await expect(paired).toHaveCount(2)
    await expectAdaptiveChartsUnscaled(page)
    const [left, right] = await Promise.all((await paired.all()).map((svg) => svg.boundingBox()))
    if (!left || !right) throw new Error('paired plots have no boxes')
    expect(Math.abs(left.y - right.y)).toBeLessThanOrEqual(2)
    expect(right.x).toBeGreaterThan(left.x + left.width - 1)
    expect(probe.pieSliceFills.length).toBeGreaterThanOrEqual(4)
    expectSameColor(probe.pieSliceFills[0], probe.chart1)
    // Connectors: the page ink at 70 % opacity, never full ink.
    expectSameColor(probe.flowchartLinkStroke, probe.foreground)
    expect(probe.flowchartLinkStrokeAlpha).toBeGreaterThan(0.6)
    expect(probe.flowchartLinkStrokeAlpha).toBeLessThan(0.8)

    // Figure type never exceeds a title's 14 px, and a sequence diagram
    // (whose renderer follows the global size) is all labels at 13 px.
    const textSizes = await page.evaluate(() => (
      [...document.querySelectorAll('[data-testid="mermaid-figure"]')].map((figure) => ({
        sizes: [...figure.querySelectorAll('svg text')].map((text) => Number.parseFloat(getComputedStyle(text).fontSize)),
        type: figure.getAttribute('data-diagram-type'),
      }))
    ))
    for (const figure of textSizes) {
      expect(Math.max(...figure.sizes), `${figure.type} largest text`).toBeLessThanOrEqual(MERMAID_FONT_SIZE + 1)
    }
    const sequence = textSizes.find((figure) => figure.type === 'sequenceDiagram')
    expect(sequence && new Set(sequence.sizes)).toEqual(new Set([MERMAID_FONT_SIZE]))

    // Every mermaid-internal class the stylesheet couples to still exists.
    for (const className of MERMAID_STYLED_CLASSES) {
      expect(await page.locator(`.inqtrix-mermaid svg .${className}`).count(), className).toBeGreaterThan(0)
    }

    // The prose constructs around the diagrams.
    await expect(page.locator('.inqtrix-markdown-table')).toHaveCount(3)
    expect(await page.locator('.katex-display').count()).toBeGreaterThanOrEqual(2)
    expect(await page.locator('td .katex').count()).toBeGreaterThanOrEqual(3)
    await expect(page.locator('pre code [data-line]').first()).toBeAttached({ timeout: RENDER_TIMEOUT })
    await expect(page.locator('input[type="checkbox"]')).toHaveCount(3)
    await expect(page.locator('a[href="#user-content-fn-1"]')).toHaveCount(1)
    await expect(page.locator('[data-markdown-variant="chat"]')).toHaveClass(/chat-markdown/)
    await captureFrame(page, testInfo, 'rendering-chat-light')
  })

  await test.step('chat, dark: ink follows the theme', async () => {
    await openSample(page, { theme: 'dark', variant: 'chat' })
    await expect(page.locator('html')).toHaveClass(/dark/)
    const probe = await readDiagramProbe(page)
    expect(probe.svgBackground).toBe('rgba(0, 0, 0, 0)')
    expectSameColor(probe.textFill, probe.foreground)
    expect(probe.textFill).not.toBe('rgb(0, 0, 0)')
    expectSameColor(probe.pieSliceFills[0], probe.chart1)
    await captureFrame(page, testInfo, 'rendering-chat-dark')
  })

  await test.step('chat, high contrast: connectors are full ink', async () => {
    await openSample(page, { contrast: 'high', theme: 'light', variant: 'chat' })
    await expect(page.locator('html')).toHaveAttribute('data-contrast-mode', 'high')
    const probe = await readDiagramProbe(page)
    expectSameColor(probe.flowchartLinkStroke, probe.foreground)
    expect(probe.flowchartLinkStrokeAlpha).toBe(1)
    await captureFrame(page, testInfo, 'rendering-chat-high-contrast')
  })

  await test.step('phone width: a diagram stops at 70 % and scrolls inside its figure', async () => {
    await page.setViewportSize({ height: 812, width: 375 })
    await openSample(page, { theme: 'light', variant: 'chat' })
    const readScroller = (selector: string) => page.locator(selector).evaluate((node) => ({
      clientWidth: node.clientWidth,
      innerMinWidth: (node.firstElementChild as HTMLElement).style.minWidth,
      scrollWidth: node.scrollWidth,
    }))
    // A wide diagram without a width knob stops at 70 % and scrolls inside
    // its figure (the flowchart is 1143 px wide) …
    const flowchart = await readScroller('figure[data-diagram-type="flowchart"] [data-testid="mermaid-scroller"]')
    expect(Number.parseInt(flowchart.innerMinWidth, 10)).toBeGreaterThan(flowchart.clientWidth)
    expect(flowchart.scrollWidth).toBeGreaterThan(flowchart.clientWidth)
    // … while plots and the gantt are redrawn to the phone column: no scroll,
    // no scaling, labels still 13 px.
    for (const selector of ['figure[data-diagram-type="xychart-beta"][data-layout="single"]', 'figure[data-diagram-type="gantt"]']) {
      const chart = await readScroller(`${selector} [data-testid="mermaid-scroller"]`)
      expect(chart.scrollWidth, selector).toBeLessThanOrEqual(chart.clientWidth)
    }
    await expectAdaptiveChartsUnscaled(page)
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
    // The paired plots wrap under each other and fit without scrolling.
    const stacked = await page.locator('[data-figure-row] figure[data-layout="half"] svg[role="img"]').all()
    const [top, bottom] = await Promise.all(stacked.map((svg) => svg.boundingBox()))
    if (!top || !bottom) throw new Error('stacked plots have no boxes')
    expect(bottom.y).toBeGreaterThan(top.y + top.height - 1)
    expect(Math.abs(top.x - bottom.x)).toBeLessThanOrEqual(2)
    await captureFrame(page, testInfo, 'rendering-chat-narrow')
  })

  await test.step('report variant: same structure at the document scale', async () => {
    await page.setViewportSize({ height: 900, width: 1_280 })
    await openSample(page, { theme: 'light', variant: 'report' })
    const probe = await readDiagramProbe(page)
    expect(probe.styleFontSize).toBe(`${MERMAID_FONT_SIZE}px`)
    await expect(page.locator('[data-markdown-variant="report"]')).toHaveClass(/report-markdown/)
    await expect(page.locator('.inqtrix-markdown-table')).toHaveCount(3)
    await captureFrame(page, testInfo, 'rendering-report-light')
  })

  await test.step('an init directive is ignored and said so', async () => {
    await openSample(page, { extra: 'directive', theme: 'dark', variant: 'chat' })
    const figure = page.locator('[data-testid="mermaid-figure"]').last()
    await expect(figure.locator('[data-testid="mermaid-directives-ignored"]')).toBeVisible()
    const background = await figure.locator('svg[role="img"]').evaluate((node) => getComputedStyle(node).backgroundColor)
    expect(background).toBe('rgba(0, 0, 0, 0)')
    expect(await figure.locator('svg rect[style*="ff0000"], svg rect[fill="#ff0000"]').count()).toBe(0)
  })
})

/** Adaptive charts are drawn at their figure's width, never scaled: the
 * rendered width equals the viewBox width, every label measures 13 px and
 * a title one point more — in every constellation. */
async function expectAdaptiveChartsUnscaled(page: Page): Promise<void> {
  const charts = await page.evaluate((types) => (
    [...document.querySelectorAll('[data-testid="mermaid-figure"]')]
      .filter((figure) => types.includes(figure.getAttribute('data-diagram-type') ?? ''))
      .map((figure) => {
        const svg = figure.querySelector('svg[role="img"]') as SVGSVGElement
        const sizes = [...svg.querySelectorAll('text')].map((text) => getComputedStyle(text).fontSize)
        return {
          rendered: svg.getBoundingClientRect().width,
          sizes,
          type: figure.getAttribute('data-diagram-type'),
          viewBoxWidth: Number(svg.getAttribute('viewBox')?.split(' ')[2]),
        }
      })
  ), MERMAID_ADAPTIVE_DIAGRAMS)
  expect(charts.length).toBeGreaterThanOrEqual(4)
  const label = `${MERMAID_FONT_SIZE}px`
  const title = `${MERMAID_FONT_SIZE + 1}px`
  for (const chart of charts) {
    expect(Math.abs(chart.rendered / chart.viewBoxWidth - 1), `${chart.type} scale`).toBeLessThan(0.02)
    expect(chart.sizes.length, `${chart.type} text`).toBeGreaterThan(3)
    expect(chart.sizes.filter((size) => size !== label && size !== title), `${chart.type} sizes`).toEqual([])
    expect(chart.sizes.filter((size) => size === label).length, `${chart.type} labels`)
      .toBeGreaterThan(chart.sizes.filter((size) => size === title).length)
  }
}

async function openSample(page: Page, query: Query): Promise<void> {
  const search = new URLSearchParams(query).toString()
  await page.goto(`${FIXTURE}?${search}`, { waitUntil: 'domcontentloaded' })
  const expected = DIAGRAMS + (query.extra === 'directive' ? 1 : 0)
  const figures = page.locator('[data-testid="mermaid-figure"]')
  await expect(figures).toHaveCount(expected)
  // Diagrams render when a reader scrolls them near the viewport (the
  // progressive-work policy); walk the page like one, then read from the top.
  for (const figure of await figures.all()) {
    await figure.scrollIntoViewIfNeeded()
  }
  await expect(page.locator('[data-testid="mermaid-figure"] svg[role="img"]'))
    .toHaveCount(expected, { timeout: RENDER_TIMEOUT })
  await page.evaluate(() => window.scrollTo(0, 0))
  await expect(page.getByText('Diagramm konnte nicht gerendert werden')).toHaveCount(0)
}

async function readDiagramProbe(page: Page): Promise<DiagramProbe> {
  return page.evaluate(() => {
    // Every colour goes through one canvas so `oklch(...)`, `color-mix(...)`
    // and baked hex compare as sRGB channels plus alpha.
    const canvas = document.createElement('canvas')
    canvas.width = 1
    canvas.height = 1
    const context = canvas.getContext('2d', { willReadFrequently: true })
    if (!context) throw new Error('no canvas context')
    const toRgba = (value: string) => {
      context.clearRect(0, 0, 1, 1)
      context.fillStyle = value
      context.fillRect(0, 0, 1, 1)
      const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data
      return { alpha: a / 255, rgb: `rgb(${r}, ${g}, ${b})` }
    }
    const resolve = (property: string, value: string) => {
      const probe = document.createElement('div')
      probe.style.setProperty(property, value)
      document.body.appendChild(probe)
      const resolved = getComputedStyle(probe).getPropertyValue(property)
      probe.remove()
      return toRgba(resolved).rgb
    }
    const flowchart = document.querySelector('figure[data-diagram-type="flowchart"] svg') as SVGSVGElement
    const text = flowchart.querySelector('text, .nodeLabel') as SVGElement | HTMLElement
    const link = flowchart.querySelector('.flowchart-link') as SVGElement
    const style = flowchart.querySelector('style')?.textContent ?? ''
    const pie = document.querySelector('figure[data-diagram-type="pie"] svg') as SVGSVGElement
    const linkStroke = toRgba(getComputedStyle(link).stroke)
    return {
      chart1: resolve('background-color', 'var(--chart-1)'),
      flowchartLinkStroke: linkStroke.rgb,
      flowchartLinkStrokeAlpha: linkStroke.alpha,
      foreground: resolve('color', 'var(--foreground)'),
      pieSliceFills: [...pie.querySelectorAll('path')]
        .map((slice) => slice.getAttribute('fill') ?? getComputedStyle(slice).fill)
        .filter((fill) => fill && fill !== 'none'),
      styleFontSize: style.match(/font-size:\s*([\d.]+px)/)?.[1] ?? null,
      svgBackground: getComputedStyle(flowchart).backgroundColor,
      svgInlineBackground: flowchart.style.background,
      textFill: toRgba(text.tagName.toLowerCase() === 'text'
        ? getComputedStyle(text).fill
        : getComputedStyle(text).color).rgb,
      xyChartViewBox: document.querySelector('figure[data-diagram-type="xychart-beta"][data-layout="single"] svg')?.getAttribute('viewBox') ?? null,
    }
  })
}

/** `#rrggbb` or `rgb(...)` against an `rgb(...)`, tolerant to the one-unit
 * rounding difference between the canvas and the CSS colour conversion. */
function expectSameColor(actual: string, expected: string): void {
  const channels = (value: string): number[] => {
    const hex = /^#([0-9a-f]{6})$/i.exec(value.trim())
    if (hex) return [0, 2, 4].map((offset) => parseInt(hex[1].slice(offset, offset + 2), 16))
    const rgb = /rgba?\(([^)]+)\)/.exec(value)
    if (!rgb) throw new Error(`unreadable colour ${value}`)
    return rgb[1].split(/[\s,/]+/).slice(0, 3).map(Number)
  }
  const a = channels(actual)
  const b = channels(expected)
  for (let index = 0; index < 3; index += 1) {
    expect(Math.abs(a[index] - b[index]), `${actual} vs ${expected}`).toBeLessThanOrEqual(2)
  }
}

async function captureFrame(page: Page, testInfo: TestInfo, name: string): Promise<void> {
  if (testInfo.project.name !== 'chromium') return
  const path = testInfo.outputPath(`${name}.png`)
  await page.screenshot({ fullPage: true, path })
  await testInfo.attach(name, { contentType: 'image/png', path })
}
