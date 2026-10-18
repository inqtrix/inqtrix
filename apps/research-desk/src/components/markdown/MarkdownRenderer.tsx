import { Check, Copy, Download, ExternalLink, FileDown, RefreshCw } from '@/components/icons'
import {
  Children,
  cloneElement,
  createContext,
  isValidElement,
  memo,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ComponentPropsWithoutRef,
  type ReactElement,
  type ReactNode,
} from 'react'
import Markdown, { type Components } from 'react-markdown'
import rehypeKatex from 'rehype-katex'
import remarkGfm from 'remark-gfm'
import remarkMath from 'remark-math'
import type { PluggableList } from 'unified'
import { Button } from '@/components/ui/button'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { ErrorBoundary } from '@/components/ErrorBoundary'
import {
  MarkdownBlockFrame,
  useMarkdownBlockAction,
  type MarkdownBlockAction,
} from '@/components/markdown/MarkdownBlockFrame'
import { MermaidFigure } from '@/components/markdown/MermaidFigure'
import {
  copyMarkdownBlockText,
  downloadMarkdownBlockPng,
  downloadMarkdownTableCsv,
  MARKDOWN_BLOCK_FILE_NAMES,
  markdownSourceFromPosition,
  type MarkdownSourcePosition,
} from '@/components/markdown/markdownBlockExport'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import { useTheme } from '@/theme/ThemeProvider'
import { useStructuralRenderBlocker } from '@/motion/StructuralLoadBoundary'
import {
  plainCodeLanguageFromClassName,
  rawCodeLanguageFromClassName,
} from './markdownLanguage'
import {
  ensureMarkdownCodeHighlight,
  markdownCodeCacheKey,
  markdownCodeTheme,
  markdownTokenCache,
  markdownTokenStyle,
  shikiCodeLanguage,
} from './codeHighlight'
import { classifyMarkdownImageSource } from './markdownImagePolicy'
import { FIGURE_ROW_ATTRIBUTE, MermaidLayoutContext, remarkFigureRows } from './figureRows'
import { normalizeLatexDelimiters } from './latexDelimiters'
import { rehypeNormalizeMathMlScripts } from './mathMl'
import { type ClaimMarkView, rehypeClaimMarks } from './claimMarks'
import { rehypeReviewMarks } from './reviewMarks'
import { useMarkdownCacheEntry } from './useMarkdownCacheEntry'
import { useProgressiveMarkdownWork } from './useProgressiveMarkdownWork'
import { copyTextToClipboard } from '@/lib/clipboard'
import { readerError } from '@/api/requestErrorText'

export type MarkdownRendererVariant = 'chat' | 'report'

/**
 * The reading-scale wrapper class belongs to the renderer, not to its
 * callers: every rule in `globals.css` that gives rendered Markdown its
 * type scale, code theme, citation chips and KaTeX fixes hangs on one of
 * these two classes, so a consumer that forgot the class rendered a
 * different product. The renderer sets it on a `display: contents` root,
 * which adds no box — callers keep their own layout wrapper. The Tiptap
 * editor surface is the one other place that may carry the class (it
 * renders its own document, not this component); a source-contract test
 * pins that allowlist.
 */
export const MARKDOWN_VARIANT_CLASS: Record<MarkdownRendererVariant, 'chat-markdown' | 'report-markdown'> = {
  chat: 'chat-markdown',
  report: 'report-markdown',
}

type MarkdownRendererProps = {
  /** Source lines (1-based) whose blocks carry the review's mark
   * (`reviewMarks.ts`); absent for every text that was never revised. */
  changedLines?: ReadonlySet<number>
  /** The statements the review marked as insufficiently supported
   * (`claimMarks.ts`); absent for every text without a review's marks. */
  claimMarks?: readonly ClaimMarkView[]
  isStreaming?: boolean
  markdown: string
  variant: MarkdownRendererVariant
}

type MarkdownImageLoadState = {
  approved: boolean
  attempt: number
  failed: boolean
}

type MarkdownImageStateContextValue = {
  approve: (source: string) => void
  fail: (source: string) => void
  states: ReadonlyMap<string, MarkdownImageLoadState>
}

const MarkdownImageStateContext = createContext<MarkdownImageStateContextValue | null>(null)
const MarkdownSourceContext = createContext<string | null>(null)

type StreamingMarkdownPendingKind = 'code' | 'math' | null

const MARKDOWN_REMARK_PLUGINS: PluggableList = [remarkGfm, remarkMath, remarkFigureRows]
const MARKDOWN_REHYPE_PLUGINS: PluggableList = [rehypeKatex, rehypeNormalizeMathMlScripts]

const MARKDOWN_COMPONENTS_BY_VARIANT: Record<MarkdownRendererVariant, Components> = {
  chat: {
    a: ({ className, ...props }) => (
      <MarkdownLink className={className} variant="chat" {...props} />
    ),
    blockquote: ({ className, ...props }) => (
      <blockquote
        className={cn('border-l-2 border-brand/35 bg-brand-subtle/55 px-3 py-2 text-sm leading-[1.45]', className)}
        {...props}
      />
    ),
    code: InlineCode,
    div: (props) => <MarkdownDiv {...props} />,
    figure: ({ className, ...props }) => (
      <figure className={cn('my-3 min-w-0', className)} {...props} />
    ),
    h1: ({ className, ...props }) => (
      <h1
        className={cn(
          'mb-3 mt-5 break-words font-semibold text-foreground [overflow-wrap:anywhere] first:mt-0',
          className,
        )}
        {...props}
      />
    ),
    h2: ({ className, ...props }) => (
      <h2
        className={cn(
          'mb-2.5 mt-5 break-words font-semibold text-foreground [overflow-wrap:anywhere] first:mt-0',
          className,
        )}
        {...props}
      />
    ),
    h3: ({ className, ...props }) => (
      <h3
        className={cn(
          'mb-2 mt-4 break-words font-semibold text-foreground [overflow-wrap:anywhere] first:mt-0',
          className,
        )}
        {...props}
      />
    ),
    h4: ({ className, ...props }) => (
      <h4
        className={cn(
          'mb-1.5 mt-3 break-words font-semibold text-foreground [overflow-wrap:anywhere] first:mt-0',
          className,
        )}
        {...props}
      />
    ),
    h5: ({ className, ...props }) => (
      <h5
        className={cn(
          'mb-1.5 mt-3 break-words font-semibold uppercase tracking-[0.02em] text-muted-foreground [overflow-wrap:anywhere] first:mt-0',
          className,
        )}
        {...props}
      />
    ),
    h6: ({ className, ...props }) => (
      <h6
        className={cn(
          'mb-1 mt-2 break-words font-semibold text-muted-foreground [overflow-wrap:anywhere] first:mt-0',
          className,
        )}
        {...props}
      />
    ),
    img: MarkdownImage,
    li: ({ className, ...props }) => (
      <li className={cn('break-words pl-1 leading-[1.45]', className)} {...props} />
    ),
    ol: ({ className, ...props }) => (
      <ol className={cn('my-3 list-decimal space-y-1 pl-5 leading-[1.45]', className)} {...props} />
    ),
    p: ({ className, ...props }) => (
      <p className={cn('my-2 break-words [overflow-wrap:anywhere] first:mt-0 last:mb-0', className)} {...props} />
    ),
    pre: (props) => <MarkdownCodePre {...props} variant="chat" />,
    strong: ({ className, ...props }) => (
      <strong className={cn('font-semibold text-foreground', className)} {...props} />
    ),
    table: (props) => <MarkdownTable {...props} variant="chat" />,
    tbody: ({ className, ...props }) => (
      <tbody className={cn('divide-y divide-border', className)} {...props} />
    ),
    td: ({ className, ...props }) => (
      <td className={cn('border-r border-border px-3 py-2 align-top last:border-r-0', className)} {...props} />
    ),
    th: ({ className, ...props }) => (
      <th className={cn('border-r border-border bg-surface px-3 py-2 align-top font-semibold text-foreground last:border-r-0', className)} {...props} />
    ),
    thead: ({ className, ...props }) => (
      <thead className={cn('border-b border-border', className)} {...props} />
    ),
    tr: ({ className, ...props }) => (
      <tr className={cn('hover:bg-surface/60', className)} {...props} />
    ),
    ul: ({ className, ...props }) => (
      <ul className={cn('my-3 list-disc space-y-1 pl-5 leading-[1.45]', className)} {...props} />
    ),
  },
  report: {
    a: ({ className, ...props }) => (
      <MarkdownLink className={className} variant="report" {...props} />
    ),
    blockquote: ({ className, ...props }) => (
      <blockquote
        className={cn(
          'border-l-2 border-brand/35 bg-brand-subtle/55 px-4 py-3 text-sm leading-7 text-foreground',
          className,
        )}
        {...props}
      />
    ),
    code: InlineCode,
    div: (props) => <MarkdownDiv {...props} />,
    figure: ({ className, ...props }) => (
      <figure className={cn('my-5 min-w-0', className)} {...props} />
    ),
    h1: ({ className, ...props }) => (
      <h1
        className={cn(
          'mb-4 mt-8 break-words text-2xl font-semibold leading-9 text-foreground [overflow-wrap:anywhere] first:mt-0',
          className,
        )}
        {...props}
      />
    ),
    h2: ({ className, ...props }) => (
      <h2
        className={cn(
          'mb-3 mt-8 break-words text-xl font-semibold leading-8 text-foreground [overflow-wrap:anywhere] first:mt-0',
          className,
        )}
        {...props}
      />
    ),
    h3: ({ className, ...props }) => (
      <h3
        className={cn(
          'mt-6 break-words text-base font-semibold leading-7 text-foreground [overflow-wrap:anywhere]',
          className,
        )}
        {...props}
      />
    ),
    h4: ({ className, ...props }) => (
      <h4 className={cn('mt-5 break-words text-sm font-semibold leading-7 text-foreground', className)} {...props} />
    ),
    h5: ({ className, ...props }) => (
      <h5 className={cn('mt-4 break-words text-xs font-semibold uppercase text-muted-foreground', className)} {...props} />
    ),
    h6: ({ className, ...props }) => (
      <h6 className={cn('mt-4 break-words text-xs font-semibold text-muted-foreground', className)} {...props} />
    ),
    img: MarkdownImage,
    hr: ({ className, ...props }) => (
      <hr className={cn('my-8 border-border', className)} {...props} />
    ),
    li: ({ className, ...props }) => (
      <li className={cn('break-words pl-1 leading-7 [overflow-wrap:anywhere]', className)} {...props} />
    ),
    ol: ({ className, ...props }) => (
      <ol className={cn('my-4 list-decimal space-y-1 pl-5 text-sm', className)} {...props} />
    ),
    p: ({ className, ...props }) => (
      <p className={cn('my-4 break-words text-sm leading-7 text-foreground [overflow-wrap:anywhere]', className)} {...props} />
    ),
    pre: (props) => <MarkdownCodePre {...props} variant="report" />,
    strong: ({ className, ...props }) => (
      <strong className={cn('font-semibold text-foreground', className)} {...props} />
    ),
    table: (props) => <MarkdownTable {...props} variant="report" />,
    tbody: ({ className, ...props }) => (
      <tbody className={cn('divide-y divide-border', className)} {...props} />
    ),
    td: ({ className, ...props }) => (
      <td className={cn('border-r border-border px-3 py-2 align-top last:border-r-0', className)} {...props} />
    ),
    th: ({ className, ...props }) => (
      <th
        className={cn(
          'border-r border-border bg-surface px-3 py-2 text-xs font-semibold uppercase text-muted-foreground last:border-r-0',
          className,
        )}
        {...props}
      />
    ),
    thead: ({ className, ...props }) => (
      <thead className={cn('border-b border-border', className)} {...props} />
    ),
    tr: ({ className, ...props }) => (
      <tr className={cn('hover:bg-surface/60', className)} {...props} />
    ),
    ul: ({ className, ...props }) => (
      <ul className={cn('my-4 list-disc space-y-1 pl-5 text-sm', className)} {...props} />
    ),
  },
}

export const MarkdownRenderer = memo(function MarkdownRenderer({
  changedLines,
  claimMarks,
  isStreaming = false,
  markdown,
  variant,
}: MarkdownRendererProps) {
  const { t } = useLocale()
  const [imageStates, setImageStates] = useState<Map<string, MarkdownImageLoadState>>(
    () => new Map(),
  )
  const approveImage = useCallback((source: string) => {
    setImageStates((current) => {
      const previous = current.get(source)
      const next = new Map(current)
      next.set(source, {
        approved: true,
        attempt: (previous?.attempt ?? 0) + 1,
        failed: false,
      })
      return next
    })
  }, [])
  const failImage = useCallback((source: string) => {
    setImageStates((current) => {
      const previous = current.get(source)
      if (previous?.failed) return current
      const next = new Map(current)
      next.set(source, {
        approved: previous?.approved ?? false,
        attempt: previous?.attempt ?? 0,
        failed: true,
      })
      return next
    })
  }, [])
  const imageStateContext = useMemo<MarkdownImageStateContextValue>(() => ({
    approve: approveImage,
    fail: failImage,
    states: imageStates,
  }), [approveImage, failImage, imageStates])
  const normalizedMarkdown = normalizeLatexDelimiters(markdown)
  const streamParts = isStreaming
    ? splitStreamingMarkdown(normalizedMarkdown)
    : { pendingKind: null, pendingText: '', stableMarkdown: normalizedMarkdown }

  return (
    <MarkdownImageStateContext.Provider value={imageStateContext}>
      <div
        className={cn('contents', MARKDOWN_VARIANT_CLASS[variant])}
        data-markdown-variant={variant}
      >
        <ErrorBoundary
          title={t.markdownError.title}
          retryLabel={t.markdownError.retry}
        >
          {streamParts.stableMarkdown && (
            <SynchronousMarkdown
              changedLines={changedLines}
              claimMarks={claimMarks}
              markdown={streamParts.stableMarkdown}
              variant={variant}
            />
          )}
          {streamParts.pendingText && (
            <StreamingMarkdownTail
              kind={streamParts.pendingKind}
              text={streamParts.pendingText}
              variant={variant}
            />
          )}
        </ErrorBoundary>
      </div>
    </MarkdownImageStateContext.Provider>
  )
})

/** The only `div` markdown can produce here is the paired-figure row
 * (raw HTML is skipped): a grid that seats two plots side by side and
 * wraps on a narrow column, with the half layout for its figures. */
function MarkdownDiv({
  className,
  node,
  ...props
}: ComponentPropsWithoutRef<'div'> & { node?: unknown }) {
  void node
  const attributes = props as Record<string, unknown>
  if (!(FIGURE_ROW_ATTRIBUTE in attributes)) {
    return <div className={className} {...props} />
  }
  return (
    <MermaidLayoutContext.Provider value="half">
      <div
        className={cn(
          'grid grid-cols-[repeat(auto-fit,minmax(300px,1fr))] items-start gap-x-4',
          className,
        )}
        {...props}
      />
    </MermaidLayoutContext.Provider>
  )
}

function MarkdownLink({
  children,
  className,
  href,
  node,
  variant,
  ...props
}: ComponentPropsWithoutRef<'a'> & {
  node?: unknown
  variant: MarkdownRendererVariant
}) {
  void node

  const childNodes = Children.toArray(children)
  if (childNodes.some(isMarkdownImageElement)) {
    return (
      <>
        {childNodes.map((child, index) => (
          isMarkdownImageElement(child)
            ? cloneElement(child, {
              key: `linked-image-${index}`,
              linkedAnchor: { ...props, className, href, variant },
            })
            : (
              <MarkdownLink
                {...props}
                className={className}
                href={href}
                key={`linked-content-${index}`}
                variant={variant}
              >
                {child}
              </MarkdownLink>
            )
        ))}
      </>
    )
  }

  const anchor = (
    <a
      {...props}
      className={cn(
        'break-words font-medium text-brand underline underline-offset-4 hover:text-foreground',
        variant === 'report' && '[overflow-wrap:anywhere]',
        className,
      )}
      href={href}
      rel={props.rel ?? 'noreferrer'}
      target={props.target ?? '_blank'}
    >
      {children}
    </a>
  )

  if (variant !== 'report' || !href) {
    return anchor
  }

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {anchor}
      </TooltipTrigger>
      <TooltipContent
        align="center"
        className="max-w-[min(34rem,calc(100vw-2rem))] border border-border/40 bg-foreground px-2.5 py-1.5 font-mono text-[11px] leading-4 text-background shadow-[0_8px_24px_var(--shadow-soft)]"
        collisionPadding={12}
        side="top"
        sideOffset={6}
      >
        <span className="block max-w-full truncate">
          {href}
        </span>
      </TooltipContent>
    </Tooltip>
  )
}

type MarkdownImageProps = ComponentPropsWithoutRef<'img'> & {
  linkedAnchor?: ComponentPropsWithoutRef<'a'> & {
    variant: MarkdownRendererVariant
  }
  node?: unknown
}

function isMarkdownImageElement(node: ReactNode): node is ReactElement<MarkdownImageProps> {
  return isValidElement<MarkdownImageProps>(node) && node.type === MarkdownImage
}

function MarkdownImage({
  alt,
  className,
  linkedAnchor,
  node,
  onError,
  onLoad,
  src,
  ...props
}: MarkdownImageProps) {
  const { t } = useLocale()
  const imageStateContext = useContext(MarkdownImageStateContext)
  void node

  if (!imageStateContext) {
    throw new Error('MarkdownImage must render inside MarkdownRenderer.')
  }

  const policy = classifyMarkdownImageSource(src, window.location.href)
  const label = alt?.trim() || t.markdown.imageFallbackAlt
  const sourceState = policy.kind === 'invalid'
    ? undefined
    : imageStateContext.states.get(policy.src)
  const failed = sourceState?.failed ?? false
  const approved = policy.kind !== 'invalid'
    && (policy.kind === 'direct' || sourceState?.approved === true)
  const imageKey = policy.kind !== 'invalid' && approved && !failed
    ? `${policy.src}\0${sourceState?.attempt ?? 0}`
    : null
  const [settledImageKey, setSettledImageKey] = useState<string | null>(null)
  const blocksStructuralReveal = useStructuralRenderBlocker(
    imageKey !== null && settledImageKey !== imageKey,
  )
  const settleImage = useCallback((key: string | null) => {
    if (key !== null) setSettledImageKey(key)
  }, [])

  if (policy.kind === 'invalid') {
    return <span className="text-muted-foreground">{label}</span>
  }

  if (!approved || failed) {
    const message = failed
      ? t.markdown.externalImageError
      : t.markdown.externalImageBlocked.replace('{host}', policy.kind === 'external' ? policy.host : '')
    return (
      <span className="my-3 flex max-w-full flex-wrap items-center gap-2 rounded-md border border-border bg-surface px-3 py-2 text-xs text-muted-foreground">
        <span className="min-w-0 flex-1 break-words">
          <span className="block font-medium text-foreground">{label}</span>
          <span className="block break-all">{message}</span>
        </span>
        <Button
          onClick={() => {
            imageStateContext.approve(policy.src)
          }}
          size="sm"
          type="button"
          variant="outline"
        >
          {failed ? <RefreshCw className="size-3.5" /> : <ExternalLink className="size-3.5" />}
          {failed ? t.markdown.externalImageRetry : t.markdown.externalImageLoad}
        </Button>
      </span>
    )
  }

  const image = (
    <img
      {...props}
      alt={alt ?? ''}
      className={cn('my-3 h-auto max-w-full rounded-md border border-border', className)}
      decoding="async"
      key={`${policy.src}-${sourceState?.attempt ?? 0}`}
      loading={blocksStructuralReveal ? 'eager' : 'lazy'}
      onError={(event) => {
        settleImage(imageKey)
        imageStateContext.fail(policy.src)
        onError?.(event)
      }}
      onLoad={(event) => {
        settleImage(imageKey)
        onLoad?.(event)
      }}
      ref={(imageElement) => {
        if (imageElement?.complete && imageElement.naturalWidth > 0) settleImage(imageKey)
      }}
      referrerPolicy="no-referrer"
      src={policy.src}
    />
  )

  return linkedAnchor ? <MarkdownLink {...linkedAnchor}>{image}</MarkdownLink> : image
}

type MarkdownTableProps = ComponentPropsWithoutRef<'table'> & {
  node?: {
    position?: MarkdownSourcePosition
  }
  variant: MarkdownRendererVariant
}

function MarkdownTable({
  className,
  node,
  variant,
  ...props
}: MarkdownTableProps) {
  const { t } = useLocale()
  const source = useContext(MarkdownSourceContext)
  const sourceMarkdown = source === null
    ? null
    : markdownSourceFromPosition(source, node?.position)
  const sourceWarningRef = useRef(false)
  const exportRef = useRef<HTMLDivElement | null>(null)
  const tableRef = useRef<HTMLTableElement | null>(null)
  const copyAction = useMarkdownBlockAction()
  const pngAction = useMarkdownBlockAction()
  const csvAction = useMarkdownBlockAction()

  useEffect(() => {
    if (sourceMarkdown !== null || sourceWarningRef.current) return
    sourceWarningRef.current = true
    console.warn('Inqtrix Markdown table source offsets are unavailable; source copy is disabled.')
  }, [sourceMarkdown])

  const actions: MarkdownBlockAction[] = [
    {
      disabled: sourceMarkdown === null,
      icon: Copy,
      id: 'copy-markdown',
      labels: {
        error: t.markdown.actionFailed,
        idle: sourceMarkdown === null
          ? t.markdown.tableSourceUnavailable
          : t.markdown.tableCopyMarkdown,
        pending: t.markdown.actionWorking,
        success: t.markdown.tableCopiedMarkdown,
      },
      onClick: () => {
        if (sourceMarkdown === null) return
        void copyAction.run(
          () => copyMarkdownBlockText(sourceMarkdown),
          'Inqtrix Markdown table source copy failed.',
        )
      },
      status: copyAction.status,
    },
    {
      icon: Download,
      id: 'save-png',
      labels: {
        error: t.markdown.actionFailed,
        idle: t.markdown.tableSavePng,
        pending: t.markdown.actionWorking,
        success: t.markdown.pngSaved,
      },
      onClick: () => {
        void pngAction.run(async () => {
          if (!exportRef.current) {
            throw new Error('Rendered Markdown table is unavailable.')
          }
          await downloadMarkdownBlockPng(exportRef.current, MARKDOWN_BLOCK_FILE_NAMES.tablePng)
        }, 'Inqtrix Markdown table PNG export failed.')
      },
      status: pngAction.status,
    },
    {
      icon: FileDown,
      id: 'save-csv',
      labels: {
        error: t.markdown.actionFailed,
        idle: t.markdown.tableSaveCsv,
        pending: t.markdown.actionWorking,
        success: t.markdown.csvSaved,
      },
      onClick: () => {
        void csvAction.run(() => {
          if (!tableRef.current) {
            throw new Error('Rendered Markdown table is unavailable.')
          }
          downloadMarkdownTableCsv(tableRef.current)
        }, 'Inqtrix Markdown table CSV export failed.')
      },
      status: csvAction.status,
    },
  ]

  return (
    <MarkdownBlockFrame actions={actions} className="inqtrix-markdown-table my-4">
      <div
        aria-label={t.markdown.scrollableTable}
        className={cn(
          'max-w-full overflow-x-auto border border-border focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring [scrollbar-width:none] [&::-webkit-scrollbar]:hidden',
          variant === 'report' ? 'rounded-lg' : 'rounded-md bg-background',
        )}
        ref={exportRef}
        role="region"
        tabIndex={0}
      >
        <table
          className={cn(
            // Keep word widths in intrinsic column sizing. The surrounding
            // prose's overflow-wrap:anywhere otherwise squeezes short labels
            // to a few letters; the region handles tables wider than the panel.
            'w-full border-collapse break-words text-left',
            variant === 'report'
              ? 'min-w-[560px] text-sm'
              : 'min-w-[32rem]',
            className,
          )}
          {...props}
          ref={tableRef}
        />
      </div>
    </MarkdownBlockFrame>
  )
}

function InlineCode({ children, className, ...props }: ComponentPropsWithoutRef<'code'>) {
  const isCodeBlock = 'data-language' in props
    || plainCodeLanguageFromClassName(className) !== null
  return (
    <code
      className={cn(
        isCodeBlock
          ? 'font-mono'
          : 'rounded bg-muted px-1 py-0.5 font-mono text-[0.85em] text-foreground',
        className,
      )}
      {...props}
    >
      {children}
    </code>
  )
}

function MarkdownCodePre({
  children,
  className,
  node,
  variant,
  ...props
}: ComponentPropsWithoutRef<'pre'> & {
  node?: {
    properties?: Record<string, unknown>
  }
  variant: MarkdownRendererVariant
}) {
  const { t } = useLocale()
  const [copied, setCopied] = useState(false)
  const preRef = useRef<HTMLPreElement | null>(null)
  const dataProps = props as Record<string, unknown>
  const language = propToString(dataProps['data-language'] ?? node?.properties?.dataLanguage ?? node?.properties?.['data-language'])
    ?? codeLanguageFromReactNode(children)
    ?? 'text'
  const codeText = textFromReactNode(children)

  // ```mermaid fences render as diagrams app-wide: this is
  // the ONE integration point every MarkdownRenderer consumer flows
  // through (chat, knowledge, reports, agent canvas, answer blocks).
  // The check reads the RAW fence token — the normalized `language`
  // above is whitelist-filtered for the highlighter and erases unknown
  // languages to 'text'.
  const rawLanguage = propToString(
    dataProps['data-language']
      ?? node?.properties?.dataLanguage
      ?? node?.properties?.['data-language'],
  )?.toLowerCase() ?? rawCodeLanguageFromReactNode(children)
  if (rawLanguage === 'mermaid') {
    return <MermaidFigure code={codeText.trim()} variant={variant} />
  }

  async function copyCode() {
    try {
      const copied = await copyTextToClipboard(
        readRenderedCodeText(preRef.current, codeText),
      )
      if (!copied) {
        throw readerError('clipboardUnavailable')
      }
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1200)
    } catch (error) {
      console.warn('Inqtrix markdown code copy failed.', error)
    }
  }

  return (
    <div
      className={cn(
        'group/code overflow-hidden border border-border bg-muted/70 text-xs shadow-[0_1px_2px_var(--shadow-hairline)]',
        variant === 'report' ? 'rounded-lg' : 'rounded-md',
      )}
    >
      <div
        className={cn(
          'flex items-center justify-between gap-2 border-b border-border bg-surface px-3',
          variant === 'report' ? 'h-9' : 'h-8',
        )}
      >
        <span className="truncate font-mono text-[11px] font-semibold text-muted-foreground">
          {language}
        </span>
        <Button
          aria-label={copied ? t.chat.copiedCode : t.chat.copyCode}
          className={cn(
            'size-6 text-muted-foreground opacity-0 transition-opacity hover:text-foreground focus-visible:opacity-100 group-hover/code:opacity-100',
            copied && 'text-success opacity-100 hover:text-success',
          )}
          onClick={() => void copyCode()}
          size="icon"
          type="button"
          variant="ghost"
        >
          {copied ? <Check className="size-3" /> : <Copy className="size-3" />}
        </Button>
      </div>
      <pre
        aria-label={t.markdown.scrollableCode.replace('{language}', language)}
        className={cn(
          'max-w-full overflow-x-auto bg-transparent font-mono text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring [scrollbar-width:none] [&::-webkit-scrollbar]:hidden',
          variant === 'report' ? 'p-4 leading-6' : 'p-3 leading-[1.45]',
          className,
        )}
        {...props}
        ref={preRef}
        role="region"
        tabIndex={0}
      >
        <HighlightedCode code={codeText} language={language} />
      </pre>
    </div>
  )
}

function HighlightedCode({
  code,
  language,
}: {
  code: string
  language: string
}) {
  const { resolvedTheme } = useTheme()
  const theme = markdownCodeTheme(resolvedTheme)
  const normalizedLanguage = shikiCodeLanguage(language)
  const cacheKey = markdownCodeCacheKey(code, normalizedLanguage, theme)
  const codeRef = useRef<HTMLElement | null>(null)
  const lines = useMarkdownCacheEntry({
    cache: markdownTokenCache,
    cacheKey,
  })

  const runHighlight = useCallback(() => {
    void ensureMarkdownCodeHighlight({
      code,
      language: normalizedLanguage,
      theme,
    })
  }, [cacheKey, code, normalizedLanguage, theme])

  useProgressiveMarkdownWork({
    isReady: lines !== undefined,
    run: runHighlight,
    targetRef: codeRef,
    workKey: cacheKey,
  })

  if (!lines) {
    return <code className={`language-${normalizedLanguage}`} ref={codeRef}>{code}</code>
  }

  return (
    <code className={`language-${normalizedLanguage}`} ref={codeRef}>
      {lines.map((line, lineIndex) => (
        <span data-line="" key={lineIndex}>
          {line.map((token, tokenIndex) => (
            <span key={`${lineIndex}-${tokenIndex}`} style={markdownTokenStyle(token)}>
              {token.content}
            </span>
          ))}
          {lineIndex < lines.length - 1 ? '\n' : null}
        </span>
      ))}
    </code>
  )
}

function SynchronousMarkdown({
  changedLines,
  claimMarks,
  markdown,
  variant,
}: {
  changedLines?: ReadonlySet<number>
  claimMarks?: readonly ClaimMarkView[]
  markdown: string
  variant: MarkdownRendererVariant
}) {
  // The marks read the parser's source positions and its text, so they
  // run before KaTeX replaces the math nodes.
  const rehypePlugins = useMemo<PluggableList>(
    () => (claimMarks?.length || changedLines?.size
      ? [
        ...(claimMarks?.length ? [[rehypeClaimMarks, { marks: claimMarks }]] : []),
        ...(changedLines?.size ? [[rehypeReviewMarks, { changedLines }]] : []),
        ...MARKDOWN_REHYPE_PLUGINS,
      ] as PluggableList
      : MARKDOWN_REHYPE_PLUGINS),
    [changedLines, claimMarks],
  )
  return (
    <MarkdownSourceContext.Provider value={markdown}>
      <Markdown
        components={MARKDOWN_COMPONENTS_BY_VARIANT[variant]}
        rehypePlugins={rehypePlugins}
        remarkPlugins={MARKDOWN_REMARK_PLUGINS}
        skipHtml
      >
        {markdown}
      </Markdown>
    </MarkdownSourceContext.Provider>
  )
}

function StreamingMarkdownTail({
  kind,
  text,
  variant,
}: {
  kind: StreamingMarkdownPendingKind
  text: string
  variant: MarkdownRendererVariant
}) {
  if (kind === 'code') {
    const pendingCode = parsePendingCodeFence(text)
    return (
      <div
        className={cn(
          'my-3 overflow-hidden border border-border bg-muted/70 text-xs shadow-[0_1px_2px_var(--shadow-hairline)]',
          variant === 'report' ? 'rounded-lg' : 'rounded-md',
        )}
      >
        <div className="flex h-8 items-center border-b border-border bg-surface px-3">
          <span className="truncate font-mono text-[11px] font-semibold text-muted-foreground">
            {pendingCode.language}
          </span>
        </div>
        <pre className="max-w-full overflow-x-auto whitespace-pre-wrap bg-transparent p-3 font-mono leading-[1.45] text-foreground [overflow-wrap:anywhere] [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {pendingCode.body}
        </pre>
      </div>
    )
  }

  return (
    <span className="whitespace-pre-wrap break-words [overflow-wrap:anywhere]">
      {text}
    </span>
  )
}

export function splitStreamingMarkdown(markdown: string): {
  pendingKind: StreamingMarkdownPendingKind
  pendingText: string
  stableMarkdown: string
} {
  const pending = findFirstUnclosedStreamingConstruct(markdown)
  if (!pending) {
    return {
      pendingKind: null,
      pendingText: '',
      stableMarkdown: markdown,
    }
  }

  return {
    pendingKind: pending.kind,
    pendingText: markdown.slice(pending.start),
    stableMarkdown: markdown.slice(0, pending.start).trimEnd(),
  }
}

function findFirstUnclosedStreamingConstruct(markdown: string): {
  kind: Exclude<StreamingMarkdownPendingKind, null>
  start: number
} | null {
  const candidates: Array<{
    kind: Exclude<StreamingMarkdownPendingKind, null>
    start: number
  }> = []
  let codeFenceStart = -1
  let blockMathStart = -1
  let inlineMathStart = -1

  for (let index = 0; index < markdown.length; index += 1) {
    const isInsideMath = blockMathStart >= 0 || inlineMathStart >= 0
    const fenceMarkerIndex = isInsideMath ? null : codeFenceMarkerIndexAtLineStart(markdown, index)
    if (fenceMarkerIndex !== null) {
      codeFenceStart = codeFenceStart >= 0 ? -1 : index
      index = fenceMarkerIndex + 2
      continue
    }

    if (codeFenceStart >= 0 || isEscaped(markdown, index)) {
      continue
    }

    if (markdown.startsWith('$$', index)) {
      blockMathStart = blockMathStart >= 0 ? -1 : index
      index += 1
      continue
    }

    if (markdown[index] === '$' && markdown[index + 1] !== '$') {
      if (inlineMathStart >= 0 && isInlineMathCloseDelimiter(markdown, index)) {
        inlineMathStart = -1
      } else if (inlineMathStart < 0 && blockMathStart < 0 && isInlineMathOpenDelimiter(markdown, index)) {
        inlineMathStart = index
      }
    }
  }

  if (codeFenceStart >= 0) {
    candidates.push({ kind: 'code', start: codeFenceStart })
  }
  if (blockMathStart >= 0) {
    candidates.push({ kind: 'math', start: blockMathStart })
  }
  if (inlineMathStart >= 0) {
    candidates.push({ kind: 'math', start: inlineMathStart })
  }

  return candidates.sort((left, right) => left.start - right.start)[0] ?? null
}

function codeFenceMarkerIndexAtLineStart(markdown: string, index: number) {
  if (index > 0 && markdown[index - 1] !== '\n') return null

  let markerIndex = index
  while (markdown[markerIndex] === ' ' && markerIndex - index < 4) {
    markerIndex += 1
  }

  return markdown.startsWith('```', markerIndex) ? markerIndex : null
}

function isEscaped(text: string, index: number) {
  let slashCount = 0
  for (let current = index - 1; current >= 0 && text[current] === '\\'; current -= 1) {
    slashCount += 1
  }
  return slashCount % 2 === 1
}

function isInlineMathOpenDelimiter(markdown: string, index: number) {
  const next = markdown[index + 1]
  const previous = markdown[index - 1]
  if (!next || /\s/.test(next)) return false
  if (previous && /[A-Za-z0-9]/.test(previous)) return false
  // A money token like "$5 Millionen" must not open "unclosed math" and
  // swallow the rest of the text as raw pending output: the delimiter
  // only opens when the SAME line plausibly closes it. An incomplete
  // final line stays open — its closing "$" may simply not have
  // streamed in yet.
  const lineEnd = markdown.indexOf('\n', index + 1)
  if (lineEnd < 0) return true
  for (let current = index + 1; current < lineEnd; current += 1) {
    if (
      markdown[current] === '$'
      && !isEscaped(markdown, current)
      && isInlineMathCloseDelimiter(markdown, current)
    ) {
      return true
    }
  }
  return false
}

function isInlineMathCloseDelimiter(markdown: string, index: number) {
  const previous = markdown[index - 1]
  if (!previous || /\s/.test(previous)) return false
  return true
}

function parsePendingCodeFence(text: string) {
  const match = /^(?: {0,3})```([^\n`]*)\n?/.exec(text)
  if (!match) {
    return {
      body: text,
      language: 'text',
    }
  }

  const language = match[1].trim().split(/\s+/)[0] || 'text'
  return {
    body: text.slice(match[0].length),
    language,
  }
}

function codeLanguageFromReactNode(node: ReactNode): string | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const language = codeLanguageFromReactNode(child)
      if (language) return language
    }
    return null
  }

  if (isValidElement<{ children?: ReactNode; className?: unknown }>(node)) {
    return plainCodeLanguageFromClassName(node.props.className)
      ?? codeLanguageFromReactNode(node.props.children)
  }

  return null
}

function rawCodeLanguageFromReactNode(node: ReactNode): string | null {
  if (Array.isArray(node)) {
    for (const child of node) {
      const language = rawCodeLanguageFromReactNode(child)
      if (language) return language
    }
    return null
  }

  if (isValidElement<{ children?: ReactNode; className?: unknown }>(node)) {
    return rawCodeLanguageFromClassName(node.props.className)
      ?? rawCodeLanguageFromReactNode(node.props.children)
  }

  return null
}

function propToString(value: unknown) {
  return typeof value === 'string' && value.trim() ? value : null
}

function textFromReactNode(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textFromReactNode).join('')
  if (isValidElement<{ children?: ReactNode }>(node)) {
    return textFromReactNode(node.props.children)
  }
  return ''
}

function readRenderedCodeText(pre: HTMLPreElement | null, fallback: ReactNode): string {
  const codeElement = pre?.querySelector('code')
  const renderedLines = codeElement
    ? Array.from(codeElement.querySelectorAll<HTMLElement>('[data-line]'))
    : []
  if (renderedLines.length > 0) {
    return renderedLines.map((line) => line.textContent ?? '').join('\n').replace(/\n$/, '')
  }

  return (codeElement?.textContent ?? textFromReactNode(fallback)).replace(/\n$/, '')
}
