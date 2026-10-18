/**
 * One visual language for every Mermaid diagram.
 *
 * Mermaid draws the picture, but the figure belongs to the page: it takes
 * the same ink, surfaces, series colours and type as the tables and
 * formulas around it, it never paints its own background, and it never
 * grows past the reading column. This module holds that contract as pure
 * functions — the theme variables, the per-diagram configuration, the
 * bounded-size constants, the directive stripping and the accessible
 * root — so it can be pinned without a browser. `MermaidFigure` applies it.
 */

/** Diagram types the product promises (the prompt block names exactly
 * these). Every other mermaid grammar still renders — it is simply not
 * promised, not styled on purpose and not covered by the render probe. */
export const MERMAID_SUPPORTED_DIAGRAMS = [
  'flowchart',
  'sequenceDiagram',
  'classDiagram',
  'stateDiagram-v2',
  'erDiagram',
  'gantt',
  'pie',
  'xychart-beta',
] as const

export type MermaidSupportedDiagram = (typeof MERMAID_SUPPORTED_DIAGRAMS)[number]

/** Series colours read from `globals.css` — ColorBrewer "Dark2", one
 * palette in every theme. */
export const MERMAID_CHART_TOKENS = [
  '--chart-1',
  '--chart-2',
  '--chart-3',
  '--chart-4',
  '--chart-5',
  '--chart-6',
  '--chart-7',
  '--chart-8',
] as const

/** Mermaid-internal SVG classes that `globals.css` styles under
 * `.inqtrix-mermaid`. They are not a public mermaid contract, so the
 * browser render probe asserts every one of them still appears in the
 * rendered sample set — a mermaid upgrade that renames one goes red there
 * instead of silently losing the ink. */
export const MERMAID_STYLED_CLASSES = [
  'flowchart-link',
  'messageLine0',
  'messageLine1',
  'loopLine',
  'actor-line',
  'transition',
  'relation',
  'node',
  'grid',
  'titleText',
] as const

/** How a figure sits in the column: alone, or sharing a row with its
 * neighbour (two adjacent data plots). Presentation only — a plot draws
 * the same picture either way. */
export type MermaidLayout = 'single' | 'half'

/** Every data plot draws on one 4:3 area of at most this size, like a
 * one-column journal figure, so a lone plot never fills the column and two
 * of them fit side by side. */
export const MERMAID_PLOT_SIZE = { height: 330, width: 440 } as const

/** Time charts need width for their axis; mermaid would otherwise size
 * them to the viewport and the figure would shrink them. */
export const MERMAID_GANTT_WIDTH = 640

/** Grammars mermaid can draw at a given width. These are redrawn to the
 * width the figure actually has instead of being scaled, so their type
 * never changes size — a label is 13 px alone, in a pair and on a phone.
 * Every other grammar has no width knob and scales as a whole. */
export const MERMAID_ADAPTIVE_DIAGRAMS: readonly string[] = ['xychart-beta', 'gantt']

/** Width buckets keep a window drag from redrawing on every pixel. */
export const MERMAID_ADAPTIVE_WIDTH_STEP = 40
/** Below this width a chart stops adapting and scrolls instead. */
export const MERMAID_ADAPTIVE_MIN_WIDTH = 200

export function isAdaptiveMermaidDiagram(type: string | null): boolean {
  return type !== null && MERMAID_ADAPTIVE_DIAGRAMS.includes(type)
}

/**
 * The width to draw an adaptive chart at inside a container of the given
 * width: never wider than its natural size, stepped so small changes do
 * not redraw, floored so the chart stays legible and scrolls instead.
 * `undefined` for grammars that are not redrawn or before a measurement.
 */
export function adaptiveMermaidWidth(type: string | null, containerWidth: number): number | undefined {
  const natural = type === 'xychart-beta'
    ? MERMAID_PLOT_SIZE.width
    : type === 'gantt' ? MERMAID_GANTT_WIDTH : undefined
  if (natural === undefined || !Number.isFinite(containerWidth) || containerWidth <= 0) return undefined
  const stepped = Math.floor(containerWidth / MERMAID_ADAPTIVE_WIDTH_STEP) * MERMAID_ADAPTIVE_WIDTH_STEP
  return Math.max(MERMAID_ADAPTIVE_MIN_WIDTH, Math.min(natural, stepped))
}

/** Below this share of its natural width a diagram stops shrinking and the
 * figure scrolls horizontally instead — labels stay legible on a phone. */
export const MERMAID_MIN_SCALE = 0.7

/** Taller diagrams (a long sequence) scroll vertically inside the figure
 * beyond this height so one picture never owns the whole column. */
export const MERMAID_MAX_FIGURE_HEIGHT = 640

/** Figure type in pixels: one point below the 14 px body text both
 * surfaces share, and never scaled — see MERMAID_ADAPTIVE_DIAGRAMS. */
export const MERMAID_FONT_SIZE = 13

const INIT_DIRECTIVE = /%%\{[\s\S]*?\}%%/g
const FRONTMATTER_FENCE = /^\s*---\s*$/
const STYLING_FRONTMATTER_KEY = /^(\s*)(config|theme|themeVariables)\s*:/

/**
 * Removes the styling escape hatches a model may write into a diagram:
 * `%%{init: …}%%` directives and the `config:`/`theme:` keys of a YAML
 * frontmatter. Both would override the app theme per diagram (a white
 * background in dark mode, a foreign font, an arbitrary palette). Other
 * frontmatter keys — `title`, `displayMode` — are content and stay.
 */
export function stripMermaidDirectives(code: string): { code: string; stripped: boolean } {
  let stripped = false
  const withoutInit = code.replace(INIT_DIRECTIVE, () => {
    stripped = true
    return ''
  })
  const lines = withoutInit.split('\n')
  let start = 0
  while (start < lines.length && lines[start].trim() === '') start += 1
  if (start < lines.length && FRONTMATTER_FENCE.test(lines[start])) {
    const end = lines.findIndex((line, index) => index > start && FRONTMATTER_FENCE.test(line))
    if (end > start) {
      const frontmatter = lines.slice(start + 1, end)
      const kept = withoutStylingKeys(frontmatter)
      if (kept.length !== frontmatter.length) {
        stripped = true
        const replacement = kept.some((line) => line.trim() !== '')
          ? ['---', ...kept, '---']
          : []
        lines.splice(start, end - start + 1, ...replacement)
      }
    }
  }
  return stripped ? { code: lines.join('\n').trim(), stripped } : { code, stripped }
}

const ACCESSIBILITY_LINE = /^acc(?:Title|Descr)\s*:/
const ACCESSIBILITY_BLOCK = /^accDescr\s*\{/

/**
 * The source mermaid is handed: styling directives removed
 * ({@link stripMermaidDirectives}) and a leading `accTitle`/`accDescr`
 * moved behind the grammar keyword. Mermaid reads the diagram type from the
 * first content line, so a diagram that opens with its accessible title is
 * "No diagram type detected"; answers stored while the prompt asked for that
 * order draw again. The move loses nothing, so `stripped` still names
 * removed styling only.
 */
export function mermaidSource(code: string): { code: string; stripped: boolean } {
  const source = stripMermaidDirectives(code)
  return { code: withKeywordFirst(source.code), stripped: source.stripped }
}

/**
 * `code` with the accessibility lines that precede its first content line
 * placed directly behind that line; every other line keeps its place.
 * Byte-identical when nothing precedes the keyword, when no content line
 * follows, or when an `accDescr {` block never closes.
 */
function withKeywordFirst(code: string): string {
  const lines = code.split('\n')
  const leading: number[] = []
  let inFrontmatter = false
  let seenContent = false
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index].trim()
    if (!line) continue
    if (!seenContent && FRONTMATTER_FENCE.test(line)) {
      inFrontmatter = !inFrontmatter
      continue
    }
    if (inFrontmatter) continue
    seenContent = true
    if (line.startsWith('%%')) continue
    if (ACCESSIBILITY_BLOCK.test(line)) {
      const end = lines.findIndex((candidate, at) => at >= index && candidate.includes('}'))
      if (end < 0) return code
      for (let at = index; at <= end; at += 1) leading.push(at)
      index = end
      continue
    }
    if (ACCESSIBILITY_LINE.test(line)) {
      leading.push(index)
      continue
    }
    if (leading.length === 0 || FRONTMATTER_FENCE.test(line)) return code
    const before = lines.slice(0, index).filter((_, at) => !leading.includes(at))
    return [...before, lines[index], ...leading.map((at) => lines[at]), ...lines.slice(index + 1)].join('\n')
  }
  return code
}

function withoutStylingKeys(frontmatter: readonly string[]): string[] {
  const kept: string[] = []
  let droppingIndent: number | null = null
  for (const line of frontmatter) {
    const indent = line.length - line.trimStart().length
    if (droppingIndent !== null) {
      if (line.trim() === '' || indent > droppingIndent) continue
      droppingIndent = null
    }
    const key = STYLING_FRONTMATTER_KEY.exec(line)
    if (key) {
      droppingIndent = key[1].length
      continue
    }
    kept.push(line)
  }
  return kept
}

/**
 * The grammar keyword a diagram opens with (`flowchart`, `pie`, …), read
 * past comments, directives, frontmatter and a leading accessible title
 * ({@link mermaidSource}). `graph` is the legacy spelling of `flowchart`.
 * `null` when the source has no keyword at all.
 */
export function detectMermaidDiagramType(code: string): string | null {
  const lines = mermaidSource(code).code.split('\n')
  let inFrontmatter = false
  let seenContent = false
  for (const raw of lines) {
    const line = raw.trim()
    if (!line) continue
    if (!seenContent && FRONTMATTER_FENCE.test(line)) {
      inFrontmatter = !inFrontmatter
      continue
    }
    if (inFrontmatter) continue
    seenContent = true
    if (line.startsWith('%%')) continue
    const keyword = line.split(/[\s;:]/, 1)[0]
    if (!keyword) return null
    return keyword === 'graph' ? 'flowchart' : keyword
  }
  return null
}

export type MermaidPalette = {
  background: string
  border: string
  brandSubtle: string
  chart: readonly string[]
  foreground: string
  muted: string
  surface: string
}

type MermaidThemeOptions = {
  dark: boolean
  fontFamily: string
  fontSize: number
}

/** Channel-wise sRGB blend of two `#rrggbb` colours (`weight` of `a`). */
export function mixHex(a: string, b: string, weight: number): string {
  const channels = (hex: string) => [1, 3, 5].map((offset) => parseInt(hex.slice(offset, offset + 2), 16))
  const [ar, ag, ab] = channels(a)
  const [br, bg, bb] = channels(b)
  const blend = (x: number, y: number) => Math.round(x * weight + y * (1 - weight))
  return `#${[blend(ar, br), blend(ag, bg), blend(ab, bb)]
    .map((channel) => channel.toString(16).padStart(2, '0'))
    .join('')}`
}

/**
 * Mermaid `base` theme variables derived from the design tokens: surfaces
 * fill nodes, the hairline border frames them, connectors are body ink at
 * 70 %, series colours come from the chart tokens (the accent first), and
 * nothing paints a background. Text on a coloured series (pie slices,
 * gantt bars) is white on the light palette and near-black on the dark one
 * — the token lightness is chosen for exactly that pairing.
 */
export function mermaidThemeVariables(
  palette: MermaidPalette,
  { dark, fontFamily, fontSize }: MermaidThemeOptions,
): Record<string, unknown> {
  const { background, border, brandSubtle, foreground, muted, surface } = palette
  // A short palette cycles so neighbouring series still differ.
  const chart = palette.chart.length > 0 ? [...palette.chart] : [foreground]
  const line = mixHex(foreground, background, 0.7)
  // Text on a coloured series: the print palette is mid-dark, so near-black
  // reads on every swatch in both themes (white fails on the yellow).
  const onSeries = '#111111'
  const series = (index: number) => chart[index % chart.length]
  const plotPalette = Array.from({ length: Math.max(chart.length, 8) }, (_, index) => series(index))
  return {
    background: 'transparent',
    darkMode: dark,
    fontFamily,
    fontSize: `${fontSize}px`,
    // Ink and surfaces shared by every diagram.
    primaryColor: surface,
    primaryBorderColor: border,
    primaryTextColor: foreground,
    secondaryColor: muted,
    secondaryBorderColor: border,
    secondaryTextColor: foreground,
    tertiaryColor: 'transparent',
    tertiaryBorderColor: border,
    tertiaryTextColor: foreground,
    lineColor: line,
    textColor: foreground,
    titleColor: foreground,
    mainBkg: surface,
    nodeBkg: surface,
    nodeBorder: border,
    nodeTextColor: foreground,
    clusterBkg: 'transparent',
    clusterBorder: border,
    defaultLinkColor: line,
    edgeLabelBackground: background,
    // Sequence diagrams.
    actorBkg: surface,
    actorBorder: border,
    actorTextColor: foreground,
    actorLineColor: line,
    signalColor: foreground,
    signalTextColor: foreground,
    labelBoxBkgColor: surface,
    labelBoxBorderColor: border,
    labelTextColor: foreground,
    loopTextColor: foreground,
    noteBkgColor: brandSubtle,
    noteBorderColor: border,
    noteTextColor: foreground,
    activationBkgColor: muted,
    activationBorderColor: border,
    sequenceNumberColor: background,
    // Class, state and entity-relationship diagrams.
    classText: foreground,
    labelColor: foreground,
    altBackground: muted,
    attributeBackgroundColorOdd: surface,
    attributeBackgroundColorEven: muted,
    // Gantt charts.
    sectionBkgColor: muted,
    altSectionBkgColor: 'transparent',
    sectionBkgColor2: muted,
    gridColor: border,
    taskBkgColor: series(0),
    taskBorderColor: series(0),
    taskTextColor: onSeries,
    taskTextLightColor: foreground,
    taskTextDarkColor: foreground,
    taskTextOutsideColor: foreground,
    taskTextClickableColor: foreground,
    activeTaskBkgColor: series(1),
    activeTaskBorderColor: series(1),
    doneTaskBkgColor: muted,
    doneTaskBorderColor: border,
    // Critical tasks take the magenta of the print palette (it has no red).
    critBkgColor: series(3),
    critBorderColor: series(3),
    todayLineColor: series(2),
    // Pie charts: mermaid's own 25 px title and 17 px labels would shout
    // next to the body text; the chart speaks at the surface's scale.
    pieTitleTextSize: `${fontSize + 1}px`,
    pieSectionTextSize: `${fontSize}px`,
    pieLegendTextSize: `${fontSize}px`,
    pie1: series(0),
    pie2: series(1),
    pie3: series(2),
    pie4: series(3),
    pie5: series(4),
    pie6: series(5),
    pie7: series(6),
    pie8: series(7),
    pieStrokeColor: background,
    pieOuterStrokeColor: background,
    pieTitleTextColor: foreground,
    pieSectionTextColor: onSeries,
    pieLegendTextColor: foreground,
    pieOpacity: 1,
    // Colour scales (timeline, mindmap, git — rendered, not promised).
    cScale0: series(0),
    cScale1: series(1),
    cScale2: series(2),
    cScale3: series(3),
    cScale4: series(4),
    cScale5: series(5),
    cScale6: series(6),
    cScale7: series(7),
    cScaleLabel0: onSeries,
    cScaleLabel1: onSeries,
    cScaleLabel2: onSeries,
    cScaleLabel3: onSeries,
    cScaleLabel4: onSeries,
    cScaleLabel5: onSeries,
    cScaleLabel6: onSeries,
    cScaleLabel7: onSeries,
    git0: series(0),
    git1: series(1),
    git2: series(2),
    git3: series(3),
    git4: series(4),
    git5: series(5),
    git6: series(6),
    git7: series(7),
    // Quadrant charts.
    quadrant1Fill: surface,
    quadrant2Fill: muted,
    quadrant3Fill: muted,
    quadrant4Fill: surface,
    quadrant1TextFill: foreground,
    quadrant2TextFill: foreground,
    quadrant3TextFill: foreground,
    quadrant4TextFill: foreground,
    quadrantPointFill: series(0),
    quadrantPointTextFill: foreground,
    quadrantXAxisTextFill: foreground,
    quadrantYAxisTextFill: foreground,
    quadrantInternalBorderStrokeFill: border,
    quadrantExternalBorderStrokeFill: border,
    quadrantTitleFill: foreground,
    // Data plots.
    xyChart: {
      backgroundColor: 'transparent',
      titleColor: foreground,
      xAxisLabelColor: foreground,
      xAxisTitleColor: foreground,
      xAxisTickColor: line,
      xAxisLineColor: line,
      yAxisLabelColor: foreground,
      yAxisTitleColor: foreground,
      yAxisTickColor: line,
      yAxisLineColor: line,
      plotColorPalette: plotPalette.join(','),
    },
  }
}

/**
 * Per-diagram layout configuration. `useMaxWidth` is stated for every
 * promised grammar so the SVG carries a viewBox and a 100 % width the
 * figure can bound. Every grammar that carries its own type sizes
 * (sequence, entity-relationship, gantt, plots) is set to the surface's
 * scale so a diagram never speaks louder than the text around it; the
 * flowchart spacing and the sequence margin are the calibrated values the
 * sample set was judged with.
 */
export function mermaidDiagramConfig({
  drawWidth,
  fontSize,
}: {
  /** Width the adaptive chart of this render is drawn at (see adaptiveMermaidWidth). */
  drawWidth?: number
  fontSize: number
}): Record<string, unknown> {
  const plotWidth = drawWidth ?? MERMAID_PLOT_SIZE.width
  // Hairline axes and ticks, labels at the text scale — the discipline of
  // a typeset figure rather than a dashboard widget.
  const axis = {
    axisLineWidth: 1,
    labelFontSize: fontSize,
    tickWidth: 1,
    titleFontSize: fontSize,
  }
  return {
    // The global size wins over a grammar's own: the sequence renderer
    // replaces its actor, message and note sizes with it (default 16 px).
    fontSize,
    flowchart: {
      curve: 'basis',
      nodeSpacing: 44,
      padding: 4,
      rankSpacing: 52,
      useMaxWidth: true,
    },
    sequence: {
      actorFontSize: fontSize,
      diagramMarginX: 24,
      height: 48,
      messageFontSize: fontSize,
      // Actors are named once, at the top, as in a printed figure.
      mirrorActors: false,
      noteFontSize: fontSize,
      useMaxWidth: true,
    },
    class: { useMaxWidth: true },
    state: { useMaxWidth: true },
    er: {
      fontSize,
      useMaxWidth: true,
    },
    gantt: {
      fontSize,
      sectionFontSize: fontSize,
      useMaxWidth: true,
      useWidth: drawWidth ?? MERMAID_GANTT_WIDTH,
    },
    pie: {
      textPosition: 0.75,
      useMaxWidth: true,
    },
    xyChart: {
      height: Math.round(plotWidth * MERMAID_PLOT_SIZE.height / MERMAID_PLOT_SIZE.width),
      titleFontSize: fontSize + 1,
      useMaxWidth: true,
      width: plotWidth,
      xAxis: axis,
      yAxis: axis,
    },
  }
}

const SVG_ROOT = /^\s*<svg\b([^>]*)>/
const A11Y_TITLE = /^\s*<svg\b[^>]*>\s*<title\b[^>]*>([^<]*)<\/title>/
const ROOT_NAME_ATTRIBUTES = /\s(?:role|aria-label)="[^"]*"/g

function escapeAttribute(value: string): string {
  return value.replace(/&(?!(?:amp|lt|gt|quot|#\d+);)/g, '&amp;').replace(/"/g, '&quot;')
}

/**
 * Gives the rendered SVG an image role and an accessible name: the
 * diagram's own `accTitle` when the author wrote one (mermaid emits it as
 * the first `<title>` child), else the caller's fallback label. Mermaid's
 * `<desc>` from `accDescr` and its `aria-roledescription` stay untouched.
 */
export function finalizeMermaidSvg(svg: string, { fallbackLabel }: { fallbackLabel: string }): string {
  const root = SVG_ROOT.exec(svg)
  if (!root) return svg
  const title = A11Y_TITLE.exec(svg)?.[1]?.trim()
  const label = title || fallbackLabel
  const attributes = root[1].replace(ROOT_NAME_ATTRIBUTES, '')
  return `<svg${attributes} role="img" aria-label="${escapeAttribute(label)}">${svg.slice(root[0].length)}`
}
