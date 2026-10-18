import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'
import DOMPurify from 'dompurify'

import { AlertTriangle, Copy, Download, Maximize2 } from '@/components/icons'
import {
  MarkdownBlockFrame,
  useMarkdownBlockAction,
  type MarkdownBlockAction,
} from '@/components/markdown/MarkdownBlockFrame'
import {
  copyMarkdownBlockText,
  downloadMarkdownBlockPng,
  MARKDOWN_BLOCK_FILE_NAMES,
} from '@/components/markdown/markdownBlockExport'
import { Dialog } from '@/components/ui/dialog'
import { useLocale } from '@/i18n/LocaleProvider'
import { useTheme, type ContrastMode, type ThemePreset } from '@/theme/ThemeProvider'
import { cn } from '@/lib/utils'
import { useStructuralRenderBlocker } from '@/motion/StructuralLoadBoundary'
import { BoundedLruCache, MARKDOWN_RENDER_CACHE_CAPACITY } from './boundedLruCache'
import { useMermaidLayout } from './figureRows'
import type { MarkdownRendererVariant } from './MarkdownRenderer'
import { mermaidNaturalWidth, mermaidPreviewMaxWidth } from './mermaidSizing'
import {
  MERMAID_CHART_TOKENS,
  MERMAID_FONT_SIZE,
  MERMAID_MAX_FIGURE_HEIGHT,
  MERMAID_MIN_SCALE,
  adaptiveMermaidWidth,
  detectMermaidDiagramType,
  finalizeMermaidSvg,
  isAdaptiveMermaidDiagram,
  mermaidDiagramConfig,
  mermaidSource,
  mermaidThemeVariables,
} from './mermaidTheme'
import { useMarkdownCacheEntry } from './useMarkdownCacheEntry'
import { useProgressiveMarkdownWork } from './useProgressiveMarkdownWork'
import { readerError } from '@/api/requestErrorText'

/**
 * Renders a ```mermaid fence as a diagram.
 *
 * Follows the EXACT `markdownTokenCache` discipline of the code
 * highlighter (gotcha #29): chat history re-renders stay synchronous —
 * a cached diagram paints its SVG immediately; an uncached one shows a
 * stable hull ("Diagramm wird erstellt …") while the async render fills
 * the module cache once and notifies subscribers. The mermaid bundle is
 * a lazy chunk (first diagram pays it, the main bundle never does).
 *
 * Security: `securityLevel: 'strict'` stays. Labels default to the SVG
 * text-node path (`htmlLabels: false`) — no HTML at all. A fence whose
 * source carries `$$…$$` math switches that ONE render to mermaid's
 * HTML-label mode, the only mode where its KaTeX support renders; those
 * renders pass a strict DOMPurify policy twice — mermaid's own passes
 * via `dompurifyConfig`, then the app-owned pass over the final SVG
 * before it is injected. MathML/KaTeX markup is allowed; network-capable
 * tags and attributes are forbidden, so the Markdown image privacy
 * boundary cannot be bypassed through a diagram label. Without a usable
 * sanitizer the math render fails CLOSED into the visible error box.
 * Render failures are LOUD by design: a warning box with the parser
 * message plus the source, never a silently missing block.
 *
 * Look and bounds come from `mermaidTheme.ts`: design-token theme
 * variables (read once per cache key, so a theme switch re-renders with
 * the current values), one figure type size, styling directives in the
 * source stripped with a visible note, a bounded figure (never wider than
 * the column, at most 70 % shrink before it scrolls, a height cap) and an
 * SVG root that carries an image role and an accessible name. Charts that
 * mermaid can draw at a given width (plots, gantt) are redrawn at the
 * width the figure has — measured with a ResizeObserver — so their type
 * never scales.
 */

type MermaidTheme = 'light' | 'dark'

/** Mermaid detects math only via `$$…$$` (its own `katexRegex`); the same
 * pattern gates the HTML-label mode here, so every diagram WITHOUT math
 * keeps today's byte-identical SVG-text rendering. No `g` flag —
 * `.test` on a global regex is stateful. */
const MERMAID_KATEX_PATTERN = /\$\$(.*?)\$\$/

/** Sanitizer policy for HTML-label (math) renders, shared by both layers.
 * The MathML additions keep KaTeX output intact — DOMPurify's default
 * profile drops `semantics`/`annotation` while KEEPING their content,
 * which would leak raw TeX source as stray text. The forbid lists close
 * every auto-fetching vector a label could smuggle in.
 *
 * `style` differs BETWEEN the layers on purpose: mermaid's own label
 * sanitize must keep forbidding `<style>` in label CONTENT (providing a
 * `dompurifyConfig` replaces its default, which is exactly that), while
 * the app-side pass over the final SVG must NOT strip the DOCUMENT-level
 * `<style>` element — that stylesheet is generator-owned (mermaid's
 * theme CSS; without it every node paints as a black box) and it exists
 * identically in the non-math path, so keeping it is parity, not a
 * relaxation. */
const MERMAID_MATH_ADD_TAGS = [
  'foreignobject',
  'semantics',
  'annotation',
  'annotation-xml',
]
const MERMAID_MATH_FORBID_TAGS = [
  'img',
  'image',
  'video',
  'audio',
  'source',
  'track',
  'iframe',
  'object',
  'embed',
]
const MERMAID_LABEL_FORBID_TAGS = ['style', ...MERMAID_MATH_FORBID_TAGS]
const MERMAID_MATH_FORBID_ATTR = ['src', 'srcset', 'poster', 'background', 'ping']

/** App-owned guarantee at the injection boundary: whatever mermaid (or a
 * future mermaid version) emits in HTML-label mode, no network-capable
 * element or attribute reaches `dangerouslySetInnerHTML`. Fails CLOSED:
 * without a usable DOMPurify (no DOM) the diagram becomes a visible
 * render error, never an unsanitized injection. */
function sanitizeMermaidMathSvg(svg: string): string {
  if (!DOMPurify.isSupported) {
    throw readerError('mathDiagramUnavailable')
  }
  return DOMPurify.sanitize(svg, {
    ADD_TAGS: MERMAID_MATH_ADD_TAGS,
    FORBID_ATTR: MERMAID_MATH_FORBID_ATTR,
    FORBID_TAGS: MERMAID_MATH_FORBID_TAGS,
    // This DOMPurify build treats ONLY `annotation-xml` as an HTML
    // integration point, so it would strip the HTML label content inside
    // `<foreignObject>` as an invalid namespace transition (label boxes
    // measured non-empty by mermaid then render empty). Declaring
    // foreignObject as the integration point it is per SVG spec restores
    // the content — which still passes the full tag/attribute policy
    // above, so no network-capable or scripting markup survives inside.
    HTML_INTEGRATION_POINTS: { 'annotation-xml': true, foreignobject: true },
    USE_PROFILES: { html: true, mathMl: true, svg: true, svgFilters: true },
  })
}

type MermaidCacheEntry =
  | { kind: 'svg'; svg: string; directivesStripped: boolean }
  | { kind: 'error'; message: string }

const mermaidCache = new BoundedLruCache<string, MermaidCacheEntry>(
  MARKDOWN_RENDER_CACHE_CAPACITY,
)
const mermaidPending = new Set<string>()

let mermaidCounter = 0
let mermaidRenderQueue: Promise<void> = Promise.resolve()

function cacheKey(
  code: string,
  theme: MermaidTheme,
  preset: ThemePreset,
  contrastMode: ContrastMode,
  drawWidth: number | undefined,
): string {
  return `${theme}\0${preset}\0${contrastMode}\0${drawWidth ?? 'natural'}\0${code}`
}

function serializeMermaidRender<Result>(render: () => Promise<Result>): Promise<Result> {
  const result = mermaidRenderQueue.then(render)
  mermaidRenderQueue = result.then(
    () => undefined,
    () => undefined,
  )
  return result
}

/** Mermaid's color math (khroma) cannot parse modern color spaces like
 * the app's `oklch(...)` tokens — resolve every token to sRGB hex via a
 * throwaway canvas (the one conversion path every oklch-capable browser
 * guarantees). Falls back LOUDLY predictable: an unparsable value keeps
 * the per-theme fallback instead of crashing the render. */
function toHexColor(value: string, fallback: string, ground?: string): string {
  try {
    const canvas = document.createElement('canvas')
    canvas.width = 1
    canvas.height = 1
    const context = canvas.getContext('2d', { willReadFrequently: true })
    if (!context) return fallback
    if (ground) {
      // A translucent token (the dark border is white at 10 %) must resolve
      // to what the eye sees over the page, not to its pure hue.
      context.fillStyle = ground
      context.fillRect(0, 0, 1, 1)
    }
    context.fillStyle = value
    context.fillRect(0, 0, 1, 1)
    const [r, g, b] = context.getImageData(0, 0, 1, 1).data
    return `#${[r, g, b]
      .map((channel) => channel.toString(16).padStart(2, '0'))
      .join('')}`
  } catch {
    return fallback
  }
}

/** Design-token colors for the diagram (read once per render, so a
 * theme switch — new cache key — picks up the current values), mapped to
 * mermaid's `base` theme by `mermaidThemeVariables`. Every value is a
 * resolved sRGB hex because mermaid's colour math cannot read `oklch()`. */
function themeVariablesFor(theme: MermaidTheme): Record<string, unknown> {
  const styles = getComputedStyle(document.documentElement)
  const dark = theme === 'dark'
  const background = toHexColor(
    styles.getPropertyValue('--background').trim() || (dark ? '#090a0c' : '#ffffff'),
    dark ? '#090a0c' : '#ffffff',
  )
  const token = (name: string, fallback: string) => {
    const raw = styles.getPropertyValue(name).trim()
    return raw ? toHexColor(raw, fallback, background) : fallback
  }
  const foreground = token('--foreground', dark ? '#fafafa' : '#17181c')
  return mermaidThemeVariables(
    {
      background,
      border: token('--border', dark ? '#2b2e36' : '#e4e6ea'),
      brandSubtle: token('--brand-subtle', dark ? '#262b3c' : '#e8ecf8'),
      chart: MERMAID_CHART_TOKENS.map((name) => token(name, foreground)),
      foreground,
      muted: token('--muted', dark ? '#23262f' : '#eef0f3'),
      surface: token('--surface', dark ? '#1b1e27' : '#f8fafc'),
    },
    {
      dark,
      fontFamily:
        styles.getPropertyValue('--font-sans').trim()
        || 'ui-sans-serif, system-ui, sans-serif',
      fontSize: MERMAID_FONT_SIZE,
    },
  )
}

const MERMAID_MEASUREMENT_HOST_ID = 'inqtrix-mermaid-measure'

/** The out-of-flow host mermaid lays its diagram out in.
 *
 * Mermaid needs a REAL laid-out node to measure a diagram (`getBBox` has no
 * boxes under `display: none`). Given no container it appends that node to
 * `document.body` — and a body child below the fold gives the DOCUMENT
 * scroll extent. In this fixed-shell app (`main` is `h-svh overflow-hidden`,
 * the panes are the only scrollers) that painted a window scrollbar for the
 * ~175ms of a render and shifted the whole UI sideways by the scrollbar
 * width. Measured: document 900px -> 1050px, workspace 1470px -> 1455px.
 *
 * `position: fixed` is the fix: laid out, so measurement is unchanged, but
 * outside the document flow, where it can never contribute scroll extent.
 * `visibility: hidden` (not `display: none`) keeps the boxes.
 *
 * ONE shared host is safe: `serializeMermaidRender` runs renders one at a
 * time, and mermaid clears the container (`innerHTML = ''`) on entry. */
function mermaidMeasurementHost(): HTMLElement {
  const existing = document.getElementById(MERMAID_MEASUREMENT_HOST_ID)
  if (existing) return existing
  const host = document.createElement('div')
  host.id = MERMAID_MEASUREMENT_HOST_ID
  host.setAttribute('aria-hidden', 'true')
  host.style.cssText = 'position:fixed;top:0;left:0;width:100%;height:0;'
    + 'overflow:hidden;visibility:hidden;pointer-events:none;'
  document.body.appendChild(host)
  return host
}

/** Test-only visibility into a render outcome (the module cache is
 * otherwise private): lets the node suite pin the fail-closed sanitizer
 * contract without a DOM. */
export function peekMermaidRender(
  code: string,
  theme: MermaidTheme,
  preset: ThemePreset,
  contrastMode: ContrastMode,
  drawWidth?: number,
): MermaidCacheEntry | undefined {
  return mermaidCache.peek(cacheKey(code, theme, preset, contrastMode, drawWidth))
}

/** Exported for the leak-contract test only. */
export async function ensureMermaidRender(
  code: string,
  theme: MermaidTheme,
  preset: ThemePreset,
  contrastMode: ContrastMode,
  /** Width an adaptive chart is drawn at; omitted for every other grammar. */
  drawWidth?: number,
): Promise<void> {
  const key = cacheKey(code, theme, preset, contrastMode, drawWidth)
  if (mermaidCache.has(key) || mermaidPending.has(key)) return
  mermaidPending.add(key)
  mermaidCounter += 1
  const renderId = `inqtrix-mermaid-${mermaidCounter}`
  try {
    const themeVariables = themeVariablesFor(theme)
    const mermaid = (await import('mermaid')).default
    // Styling escape hatches in the source never reach mermaid: the theme
    // is the app's, per diagram and per surface. The figure says so. A
    // stored diagram that opens with its accessible title gets its keyword
    // first; that loses nothing, so the figure says nothing about it.
    const source = mermaidSource(code)
    const wantsMathLabels = MERMAID_KATEX_PATTERN.test(source.code)
    const { svg } = await serializeMermaidRender(async () => {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        htmlLabels: wantsMathLabels,
        ...(wantsMathLabels
          ? {
            dompurifyConfig: {
              ADD_TAGS: MERMAID_MATH_ADD_TAGS,
              FORBID_ATTR: MERMAID_MATH_FORBID_ATTR,
              FORBID_TAGS: MERMAID_LABEL_FORBID_TAGS,
            },
          }
          : {}),
        theme: 'base',
        themeVariables,
        ...mermaidDiagramConfig({ drawWidth, fontSize: MERMAID_FONT_SIZE }),
      })
      return mermaid.render(renderId, source.code, mermaidMeasurementHost())
    })
    // The cache stores the SANITIZED SVG, so both injection sites (inline
    // figure and expand dialog) can only ever receive the cleaned string.
    mermaidCache.set(key, {
      directivesStripped: source.stripped,
      kind: 'svg',
      svg: wantsMathLabels ? sanitizeMermaidMathSvg(svg) : svg,
    })
  } catch (error) {
    // Loud, never silent: the figure shows the parser message + source.
    const message = error instanceof Error ? error.message : String(error)
    console.warn('Inqtrix mermaid render failed.', error)
    mermaidCache.set(key, { kind: 'error', message })
  } finally {
    mermaidPending.delete(key)
    // Mermaid builds a temporary `#d<renderId>` inside the measurement host
    // and removes it on success — but LEAKS it on a parse error. The host
    // keeps a stray out of the document flow (see mermaidMeasurementHost),
    // so it cannot move the shell; it is still removed here so the
    // host stays empty between renders instead of retaining dead SVG.
    // Pinned by MermaidFigure.test.ts.
    document.getElementById(`d${renderId}`)?.remove()
  }
}

export function MermaidFigure({
  code,
  variant,
}: {
  code: string
  variant: MarkdownRendererVariant
}) {
  const { t } = useLocale()
  const { contrastMode, preset, resolvedTheme } = useTheme()
  // Presentation only (`data-layout`): a paired plot draws the same picture.
  const layout = useMermaidLayout()
  const theme: MermaidTheme = resolvedTheme === 'dark' ? 'dark' : 'light'
  const diagramType = useMemo(() => detectMermaidDiagramType(code), [code])
  const adaptive = isAdaptiveMermaidDiagram(diagramType)
  // Adaptive charts are drawn at the figure's width so their labels keep
  // their size; the width is measured before the first render and again
  // whenever the column changes (a row wrapping, a window resize).
  const [drawWidth, setDrawWidth] = useState<number | undefined>(undefined)
  const figureRef = useRef<HTMLElement | null>(null)
  useLayoutEffect(() => {
    if (!adaptive) return undefined
    const node = figureRef.current
    if (!node) return undefined
    const measure = () => {
      setDrawWidth(adaptiveMermaidWidth(diagramType, node.getBoundingClientRect().width))
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    return () => observer.disconnect()
  }, [adaptive, diagramType])
  const renderWidth = adaptive ? drawWidth : undefined
  const key = cacheKey(code, theme, preset, contrastMode, renderWidth)
  const diagramRef = useRef<HTMLDivElement | null>(null)
  const [previewOpen, setPreviewOpen] = useState(false)
  const copyAction = useMarkdownBlockAction()
  const pngAction = useMarkdownBlockAction()
  const entry = useMarkdownCacheEntry({
    cache: mermaidCache,
    cacheKey: key,
  })
  const fallbackLabel = t.markdown.mermaidDiagramLabel.replace('{type}', diagramType ?? 'mermaid')
  // The accessible root is applied at injection so a locale switch renames
  // an untitled diagram without touching the render cache.
  const svg = useMemo(
    () => (entry?.kind === 'svg' ? finalizeMermaidSvg(entry.svg, { fallbackLabel }) : null),
    [entry, fallbackLabel],
  )
  const naturalWidth = entry?.kind === 'svg'
    ? mermaidNaturalWidth(entry.svg)
    : undefined
  const previewMaxWidth = entry?.kind === 'svg'
    ? mermaidPreviewMaxWidth(entry.svg)
    : undefined
  const blocksStructuralReveal = useStructuralRenderBlocker(entry === undefined)

  const runRender = useCallback(() => {
    // The measurement normally lands before the first run (layout effect).
    // Without one — no width yet, no ResizeObserver — the chart still draws
    // at its natural size and is redrawn once a width arrives: never stuck.
    void ensureMermaidRender(code, theme, preset, contrastMode, renderWidth)
  }, [code, contrastMode, key, preset, renderWidth, theme])

  useProgressiveMarkdownWork({
    eager: blocksStructuralReveal,
    isReady: entry !== undefined,
    run: runRender,
    targetRef: figureRef,
    workKey: key,
  })

  if (entry?.kind === 'error') {
    return (
      <figure
        className={cn(
          variant === 'report' ? 'my-5' : 'my-3',
          'overflow-hidden rounded-md border border-warning/40 bg-warning-subtle/30',
        )}
        data-diagram-type={diagramType ?? undefined}
        data-testid="mermaid-figure"
        ref={figureRef}
      >
        <div className="flex items-start gap-2 border-b border-warning/30 px-3 py-2">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0 text-warning" />
          <span className="min-w-0 break-words t-meta text-foreground/90">
            {t.markdown.mermaidError.replace('{message}', entry.message)}
          </span>
        </div>
        <pre className="max-w-full overflow-x-auto p-3 font-mono text-xs text-muted-foreground">
          <code>{code}</code>
        </pre>
      </figure>
    )
  }
  // Successful diagrams are intentionally unboxed. A figure is never wider
  // than the column; it shrinks with the column down to MERMAID_MIN_SCALE of
  // its natural width and then scrolls inside itself (horizontally, and
  // vertically past MERMAID_MAX_FIGURE_HEIGHT), so labels stay legible on a
  // phone and one tall sequence never owns the whole column. Compact figures
  // keep their type scale instead of being enlarged. Error output remains
  // boxed because it is an alert.
  const actions: MarkdownBlockAction[] = entry
    ? [
      {
        icon: Maximize2,
        id: 'expand',
        labels: {
          error: t.markdown.actionFailed,
          idle: t.markdown.mermaidExpand,
          pending: t.markdown.actionWorking,
          success: t.markdown.mermaidExpand,
        },
        onClick: () => setPreviewOpen(true),
        status: 'idle',
      },
      {
        icon: Copy,
        id: 'copy',
        labels: {
          error: t.markdown.actionFailed,
          idle: t.markdown.mermaidCopy,
          pending: t.markdown.actionWorking,
          success: t.markdown.mermaidCopied,
        },
        onClick: () => {
          void copyAction.run(
            () => copyMarkdownBlockText(code),
            'Inqtrix Mermaid source copy failed.',
          )
        },
        status: copyAction.status,
      },
      {
        icon: Download,
        id: 'png',
        labels: {
          error: t.markdown.actionFailed,
          idle: t.markdown.mermaidSavePng,
          pending: t.markdown.actionWorking,
          success: t.markdown.pngSaved,
        },
        onClick: () => {
          void pngAction.run(async () => {
            if (!diagramRef.current) {
              throw new Error('Rendered Mermaid element is unavailable.')
            }
            await downloadMarkdownBlockPng(diagramRef.current, MARKDOWN_BLOCK_FILE_NAMES.diagramPng)
          }, 'Inqtrix Mermaid PNG export failed.')
        },
        status: pngAction.status,
      },
    ]
    : []

  return (
    <figure
      className={cn(
        // The same vertical rhythm as the tables and images of the surface.
        variant === 'report' ? 'my-5' : 'my-3',
        'w-full overflow-visible',
        !entry && 'min-h-24',
      )}
      data-diagram-type={diagramType ?? undefined}
      data-layout={layout}
      data-testid="mermaid-figure"
      ref={figureRef}
    >
      <MarkdownBlockFrame actions={actions}>
        {svg ? (
          <div
            // A diagram wider or taller than its frame scrolls here. Without a
            // tab stop that scrolling is mouse-only: a keyboard user cannot
            // reach the part of the diagram that is off-frame (WCAG 2.1.1).
            // The stop is named after the diagram type, so it announces what
            // it scrolls; the SVG inside keeps its own accTitle-derived name.
            aria-label={fallbackLabel}
            className="w-full overflow-auto"
            data-testid="mermaid-scroller"
            role="group"
            style={{ maxHeight: `${MERMAID_MAX_FIGURE_HEIGHT}px` }}
            tabIndex={0}
          >
            <div
              className={cn('mx-auto max-w-full', naturalWidth && 'w-full')}
              ref={diagramRef}
              style={naturalWidth
                ? {
                  maxWidth: `${naturalWidth}px`,
                  minWidth: `${Math.ceil(naturalWidth * MERMAID_MIN_SCALE)}px`,
                }
                : undefined}
            >
              <div
                className={cn(
                  'inqtrix-mermaid [&_svg]:mx-auto [&_svg]:block [&_svg]:!h-auto',
                  naturalWidth && '[&_svg]:!w-full [&_svg]:!max-w-none',
                )}
                // Mermaid's own strict-mode SVG output (sanitized labels, no
                // scripts); the sandbox contract lives in ensureMermaidRender.
                dangerouslySetInnerHTML={{ __html: svg }}
              />
            </div>
          </div>
        ) : (
          <p className="px-1 py-6 t-meta text-muted-foreground sm:px-3">
            {t.markdown.mermaidPending}
          </p>
        )}
        {entry?.kind === 'svg' && entry.directivesStripped && (
          <p
            className="px-1 pt-2 t-meta text-muted-foreground sm:px-3"
            data-testid="mermaid-directives-ignored"
          >
            {t.markdown.mermaidDirectivesIgnored}
          </p>
        )}
        {svg ? (
          <Dialog
            className="flex max-h-[calc(100svh-4rem)] max-w-[calc(100vw-2rem)] flex-col"
            closeLabel={t.common.close}
            contentClassName="min-h-0 overflow-auto p-4"
            contentProps={{
              'aria-label': t.markdown.mermaidDialogTitle,
              role: 'region',
              tabIndex: 0,
            }}
            onClose={() => setPreviewOpen(false)}
            open={previewOpen}
            title={t.markdown.mermaidDialogTitle}
          >
            <div
              className={cn('mx-auto max-w-full', previewMaxWidth && 'w-full')}
              style={previewMaxWidth ? { maxWidth: `${previewMaxWidth}px` } : undefined}
            >
              <div
                className={cn(
                  'inqtrix-mermaid [&_svg]:mx-auto [&_svg]:block [&_svg]:!h-auto',
                  previewMaxWidth && '[&_svg]:!w-full [&_svg]:!max-w-none',
                )}
                dangerouslySetInnerHTML={{ __html: svg }}
              />
            </div>
          </Dialog>
        ) : null}
      </MarkdownBlockFrame>
    </figure>
  )
}
