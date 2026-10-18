import { readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import {
  MERMAID_ADAPTIVE_MIN_WIDTH,
  MERMAID_CHART_TOKENS,
  MERMAID_FONT_SIZE,
  MERMAID_STYLED_CLASSES,
  MERMAID_SUPPORTED_DIAGRAMS,
  MERMAID_GANTT_WIDTH,
  MERMAID_PLOT_SIZE,
  adaptiveMermaidWidth,
  detectMermaidDiagramType,
  finalizeMermaidSvg,
  mermaidDiagramConfig,
  mermaidSource,
  mermaidThemeVariables,
  mixHex,
  stripMermaidDirectives,
} from './mermaidTheme'

const palette = {
  background: '#ffffff',
  border: '#e4e6ea',
  brandSubtle: '#e8ecf8',
  chart: ['#1b9e77', '#d95f02', '#7570b3', '#e7298a', '#66a61e', '#e6ab02', '#a6761d', '#666666'],
  foreground: '#17181c',
  muted: '#eef0f3',
  surface: '#f8fafc',
}

describe('mermaid theme variables', () => {
  it('fills nodes with the surface, frames them with the border and inks lines at 70 %', () => {
    const variables = mermaidThemeVariables(palette, { dark: false, fontFamily: 'Inter', fontSize: 13 })
    expect(variables.primaryColor).toBe(palette.surface)
    expect(variables.primaryBorderColor).toBe(palette.border)
    expect(variables.nodeBorder).toBe(palette.border)
    expect(variables.lineColor).toBe(mixHex(palette.foreground, palette.background, 0.7))
    expect(variables.lineColor).not.toBe(palette.foreground)
    expect(variables.background).toBe('transparent')
    expect(variables.clusterBkg).toBe('transparent')
    expect(variables.fontSize).toBe('13px')
    expect(variables.fontFamily).toBe('Inter')
    expect(variables.darkMode).toBe(false)
    expect(variables.pieTitleTextSize).toBe('14px')
    expect(variables.pieSectionTextSize).toBe('13px')
    expect(variables.pieLegendTextSize).toBe('13px')
    // Near-black on the print palette in the light theme as well.
    expect(variables.pieSectionTextColor).toBe('#111111')
  })

  it('maps the eight chart tokens onto pie, colour scales, gantt and the plot palette', () => {
    const variables = mermaidThemeVariables(palette, { dark: true, fontFamily: 'Inter', fontSize: 14 })
    expect([1, 2, 3, 4, 5, 6, 7, 8].map((index) => variables[`pie${index}`])).toEqual(palette.chart)
    expect([variables.cScale0, variables.cScale7]).toEqual([palette.chart[0], palette.chart[7]])
    expect(variables.taskBkgColor).toBe(palette.chart[0])
    expect(variables.activeTaskBkgColor).toBe(palette.chart[1])
    expect(variables.critBkgColor).toBe(palette.chart[3])
    expect(variables.todayLineColor).toBe(palette.chart[2])
    const xyChart = variables.xyChart as Record<string, string>
    expect(xyChart.plotColorPalette).toBe(palette.chart.join(','))
    expect(xyChart.backgroundColor).toBe('transparent')
    expect(xyChart.xAxisLineColor).toBe(variables.lineColor)
    // Text on a coloured series flips with the theme, never the page ink.
    expect(variables.pieSectionTextColor).toBe('#111111')
    expect(variables.darkMode).toBe(true)
    expect(MERMAID_CHART_TOKENS).toHaveLength(8)
  })

  it('cycles a short palette instead of leaving a series undefined', () => {
    const variables = mermaidThemeVariables(
      { ...palette, chart: ['#111111', '#222222'] },
      { dark: false, fontFamily: 'Inter', fontSize: 13 },
    )
    expect(variables.pie6).toBe('#222222')
    expect(variables.cScale4).toBe('#111111')
    expect((variables.xyChart as Record<string, string>).plotColorPalette)
      .toBe('#111111,#222222,#111111,#222222,#111111,#222222,#111111,#222222')
    const bare = mermaidThemeVariables({ ...palette, chart: [] }, { dark: false, fontFamily: 'Inter', fontSize: 13 })
    expect(bare.pie1).toBe(palette.foreground)
  })

  it('blends hex colours channel-wise', () => {
    expect(mixHex('#000000', '#ffffff', 0.7)).toBe('#4d4d4d')
    expect(mixHex('#ff0000', '#0000ff', 1)).toBe('#ff0000')
  })
})

describe('mermaid diagram config', () => {
  it('bounds every promised grammar and fixes the plot drawing area', () => {
    const config = mermaidDiagramConfig({ fontSize: 14 }) as Record<string, Record<string, unknown>>
    for (const key of ['flowchart', 'sequence', 'class', 'state', 'er', 'gantt', 'pie', 'xyChart']) {
      expect(config[key]?.useMaxWidth, key).toBe(true)
    }
    // One 4:3 area for every plot — alone it never fills the column, two fit side by side.
    expect(config.xyChart).toMatchObject({ height: 330, width: 440 })
    expect(MERMAID_PLOT_SIZE).toEqual({ height: 330, width: 440 })
    expect(config.gantt.useWidth).toBe(MERMAID_GANTT_WIDTH)
    expect(MERMAID_GANTT_WIDTH).toBe(640)
    expect(config.sequence.diagramMarginX).toBe(24)
    expect(config.sequence.mirrorActors).toBe(false)
    expect(config.flowchart).toMatchObject({ curve: 'basis', nodeSpacing: 44, padding: 4, rankSpacing: 52 })
    // Grammars with their own type sizes speak at the surface's scale.
    expect(config.gantt.fontSize).toBe(14)
    expect(config.sequence).toMatchObject({ actorFontSize: 14, messageFontSize: 14, noteFontSize: 14 })
    expect(config.er.fontSize).toBe(14)
    expect(config.xyChart.titleFontSize).toBe(15)
    expect(config.xyChart.xAxis).toEqual({ axisLineWidth: 1, labelFontSize: 14, tickWidth: 1, titleFontSize: 14 })
    expect(config.xyChart.yAxis).toEqual(config.xyChart.xAxis)
    // One figure type for both surfaces: a point below their 14 px body text.
    expect(MERMAID_FONT_SIZE).toBe(13)
    // The global size is what the sequence renderer applies to its text.
    expect((config as Record<string, unknown>).fontSize).toBe(14)
  })

  it('draws an adaptive chart at the width the figure has, keeping 4:3 for plots', () => {
    const narrow = mermaidDiagramConfig({ drawWidth: 320, fontSize: 13 }) as Record<string, Record<string, unknown>>
    expect(narrow.xyChart).toMatchObject({ height: 240, width: 320 })
    expect(narrow.gantt.useWidth).toBe(320)
    // The label sizes do not follow the width.
    expect(narrow.xyChart.xAxis).toMatchObject({ labelFontSize: 13 })
  })
})

describe('adaptiveMermaidWidth', () => {
  it('caps at the natural size, steps down in buckets and floors before scrolling', () => {
    expect(adaptiveMermaidWidth('xychart-beta', 915)).toBe(MERMAID_PLOT_SIZE.width)
    expect(adaptiveMermaidWidth('xychart-beta', 417)).toBe(400)
    expect(adaptiveMermaidWidth('xychart-beta', 333)).toBe(320)
    expect(adaptiveMermaidWidth('xychart-beta', 150)).toBe(MERMAID_ADAPTIVE_MIN_WIDTH)
    expect(adaptiveMermaidWidth('gantt', 915)).toBe(MERMAID_GANTT_WIDTH)
    expect(adaptiveMermaidWidth('gantt', 333)).toBe(320)
  })

  it('leaves every other grammar and an unmeasured figure alone', () => {
    expect(adaptiveMermaidWidth('flowchart', 500)).toBeUndefined()
    expect(adaptiveMermaidWidth('pie', 500)).toBeUndefined()
    expect(adaptiveMermaidWidth(null, 500)).toBeUndefined()
    expect(adaptiveMermaidWidth('xychart-beta', 0)).toBeUndefined()
    expect(adaptiveMermaidWidth('xychart-beta', Number.NaN)).toBeUndefined()
  })
})

describe('mermaid directives', () => {
  it('removes init directives and reports it', () => {
    const source = '%%{init: {"theme": "forest", "themeVariables": {"primaryColor": "#fff"}}}%%\nflowchart LR\n  A --> B'
    expect(stripMermaidDirectives(source)).toEqual({ code: 'flowchart LR\n  A --> B', stripped: true })
  })

  it('drops config and theme keys from a frontmatter but keeps the title', () => {
    const source = '---\ntitle: Ablauf\nconfig:\n  theme: dark\n  look: handDrawn\n---\nflowchart TD\n  A --> B'
    expect(stripMermaidDirectives(source)).toEqual({
      code: '---\ntitle: Ablauf\n---\nflowchart TD\n  A --> B',
      stripped: true,
    })
    const onlyConfig = '---\nconfig:\n  theme: dark\n---\npie\n  "a" : 1'
    expect(stripMermaidDirectives(onlyConfig)).toEqual({ code: 'pie\n  "a" : 1', stripped: true })
  })

  it('returns clean sources byte-identical', () => {
    const source = 'sequenceDiagram\n  A->>B: hi\n'
    expect(stripMermaidDirectives(source)).toEqual({ code: source, stripped: false })
    const titled = '---\ntitle: Nur Titel\n---\nflowchart LR\n  A --> B'
    expect(stripMermaidDirectives(titled)).toEqual({ code: titled, stripped: false })
  })
})

describe('mermaid diagram type', () => {
  it('reads the grammar keyword past comments, directives and frontmatter', () => {
    expect(detectMermaidDiagramType('flowchart LR\n A-->B')).toBe('flowchart')
    expect(detectMermaidDiagramType('graph TD; A-->B')).toBe('flowchart')
    expect(detectMermaidDiagramType('%%{init: {}}%%\n---\ntitle: x\n---\n%% Kommentar\nxychart-beta\n  bar [1]')).toBe('xychart-beta')
    expect(detectMermaidDiagramType('pie showData\n "a": 1')).toBe('pie')
    expect(detectMermaidDiagramType('stateDiagram-v2\n [*] --> A')).toBe('stateDiagram-v2')
    expect(detectMermaidDiagramType('   \n%% nur Kommentar')).toBeNull()
  })

  it('reads the keyword of a stored diagram that opens with its accessible title', () => {
    expect(detectMermaidDiagramType('accTitle: Umsatz je Monat\nxychart-beta\n  bar [1]')).toBe('xychart-beta')
    expect(detectMermaidDiagramType('accDescr {\n  Zwei Balken\n}\npie\n  "a": 1')).toBe('pie')
  })

  it('promises exactly the eight grammars the sample set renders', () => {
    expect([...MERMAID_SUPPORTED_DIAGRAMS]).toEqual([
      'flowchart', 'sequenceDiagram', 'classDiagram', 'stateDiagram-v2', 'erDiagram', 'gantt', 'pie', 'xychart-beta',
    ])
  })
})

describe('mermaid source with its keyword first', () => {
  it('moves a leading accessible title and description behind the keyword, every other line in place', () => {
    expect(mermaidSource('accTitle: Umsatz\nxychart-beta\n  bar [1]').code)
      .toBe('xychart-beta\naccTitle: Umsatz\n  bar [1]')
    expect(mermaidSource('accTitle: A\naccDescr: B\nflowchart TD\n  A-->B').code)
      .toBe('flowchart TD\naccTitle: A\naccDescr: B\n  A-->B')
    expect(mermaidSource('accDescr {\n  Zwei Balken\n}\npie\n  "a": 1').code)
      .toBe('pie\naccDescr {\n  Zwei Balken\n}\n  "a": 1')
    expect(mermaidSource('---\ntitle: T\n---\n%% Notiz\naccTitle: A\nsequenceDiagram\n  A->>B: hi').code)
      .toBe('---\ntitle: T\n---\n%% Notiz\nsequenceDiagram\naccTitle: A\n  A->>B: hi')
  })

  it('leaves a source byte-identical when its keyword comes first or it has none', () => {
    const keywordFirst = 'flowchart TD\n  accTitle: A\n  A-->B'
    expect(mermaidSource(keywordFirst)).toEqual({ code: keywordFirst, stripped: false })
    const titleOnly = 'accTitle: Nur ein Titel'
    expect(mermaidSource(titleOnly).code).toBe(titleOnly)
    const openBlock = 'accDescr {\n  ohne Ende\nflowchart TD'
    expect(mermaidSource(openBlock).code).toBe(openBlock)
  })

  it('reports only removed styling as stripped, never the reorder', () => {
    expect(mermaidSource('accTitle: A\npie\n  "a": 1').stripped).toBe(false)
    expect(mermaidSource('%%{init: {}}%%\naccTitle: A\npie\n  "a": 1'))
      .toEqual({ code: 'pie\naccTitle: A\n  "a": 1', stripped: true })
  })
})

describe('finalizeMermaidSvg', () => {
  it('names the image after the accessible title mermaid emitted', () => {
    const svg = '<svg id="m1" role="graphics-document document" aria-roledescription="flowchart-v2" aria-labelledby="t1"><title id="t1">Ablauf &amp; "Plan"</title><desc id="d1">Beschreibung</desc><g/></svg>'
    const out = finalizeMermaidSvg(svg, { fallbackLabel: 'Diagramm' })
    expect(out.startsWith('<svg id="m1" aria-roledescription="flowchart-v2" aria-labelledby="t1" role="img" aria-label="Ablauf &amp; &quot;Plan&quot;">')).toBe(true)
    expect(out).toContain('<desc id="d1">Beschreibung</desc>')
  })

  it('falls back to the given label when the source carried no title', () => {
    const svg = '<svg id="m2" aria-roledescription="pie"><style/><g/></svg>'
    expect(finalizeMermaidSvg(svg, { fallbackLabel: 'Diagramm (pie)' }))
      .toBe('<svg id="m2" aria-roledescription="pie" role="img" aria-label="Diagramm (pie)"><style/><g/></svg>')
    expect(finalizeMermaidSvg('kein svg', { fallbackLabel: 'x' })).toBe('kein svg')
  })
})

describe('globals.css coupling', () => {
  it('styles no mermaid-internal class the render probe does not pin', () => {
    const css = readFileSync(path.resolve(__dirname, '../../styles/globals.css'), 'utf8')
    const rules = css.match(/\.inqtrix-mermaid svg[^{]*\{/g) ?? []
    expect(rules.length).toBeGreaterThan(0)
    const classes = new Set<string>()
    for (const rule of rules) {
      for (const match of rule.matchAll(/\.([A-Za-z][\w-]*)/g)) {
        if (match[1] !== 'inqtrix-mermaid') classes.add(match[1])
      }
    }
    expect([...classes].sort()).toEqual([...MERMAID_STYLED_CLASSES].sort())
    // One print palette for every theme: each token is defined exactly once.
    for (const token of MERMAID_CHART_TOKENS) {
      expect(css.match(new RegExp(`${token}:`, 'g'))?.length, token).toBe(1)
    }
    expect(css).toContain('--chart-1: #1b9e77;')
  })
})
