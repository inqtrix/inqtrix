import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { ChevronDown, ChevronUp, X } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from '@/components/ui/resizable'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import { OriginalFileTab } from '@/features/files/OriginalFileTab'
import { useActiveMatchScroll } from '@/features/scroll/useActiveMatchScroll'
import type { KnowledgeDocumentText } from '@/features/researchRuns/types'
import {
  findFirstMatchingTarget,
  splitByRanges,
  type HighlightRange,
} from './highlight'
import type { DocumentViewerTarget, KnowledgeDataSource } from './types'
import { useFileAccessProbe } from './useFileAccessProbe'

type ViewerTab = 'extracted' | 'original'

type DocumentState =
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; document: KnowledgeDocumentText }

/**
 * Read part of the knowledge triad: right-hand overlay with the
 * extracted document text (highlightable, the same text retrieval and
 * grounding verified against) and — when an original server file
 * exists — a PDF preview via blob object URL.
 *
 * Architecture note: highlighting is a separate layer over the text
 * renderer (offset ranges from `highlight.ts`, rendered as `<mark>`
 * spans). A future layout parser can replace the renderer with a
 * coordinate-based view while the matching layer stays as is.
 */
export function DocumentViewer({
  dataSource,
  onClose,
  target,
}: {
  dataSource: KnowledgeDataSource
  onClose: () => void
  target: DocumentViewerTarget
}) {
  const { t } = useLocale()
  const [tab, setTab] = useState<ViewerTab>('extracted')
  const [documentState, setDocumentState] = useState<DocumentState>({ kind: 'loading' })
  const [originalOpened, setOriginalOpened] = useState(false)
  const [activeMatch, setActiveMatch] = useState(0)
  const activeMatchRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    let ignore = false
    setDocumentState({ kind: 'loading' })
    setTab('extracted')
    setActiveMatch(0)
    dataSource
      .loadDocumentText(target.documentId)
      .then((document) => {
        if (!ignore) setDocumentState({ document, kind: 'ready' })
      })
      .catch((error: unknown) => {
        if (!ignore) {
          setDocumentState({
            kind: 'error',
            message: error instanceof Error ? error.message : t.knowledge.viewerError,
          })
        }
      })
    return () => {
      ignore = true
    }
    // t is stable per locale; reloading on locale switch is acceptable.
  }, [dataSource, target.documentId, t])

  const documentText = documentState.kind === 'ready' ? documentState.document.text : ''
  const matches = useMemo<HighlightRange[]>(
    () => (documentText ? findFirstMatchingTarget(documentText, target.highlightTargets) : []),
    [documentText, target.highlightTargets],
  )
  const segments = useMemo(
    () => splitByRanges(documentText, matches),
    [documentText, matches],
  )

  useEffect(() => {
    setActiveMatch(0)
  }, [matches.length])

  useActiveMatchScroll({
    activeIndex: activeMatch,
    contentKey: `${target.documentId}:${target.chunkIndex ?? 'none'}`,
    enabled: tab === 'extracted' && documentState.kind === 'ready',
    targetRef: activeMatchRef,
  })

  const fileId = documentState.kind === 'ready'
    ? stringOrNull(documentState.document.metadata.file_id)
    : null
  const fileAccess = useFileAccessProbe(dataSource, fileId)
  const canShowOriginal = fileAccess === 'allowed'
  useEffect(() => {
    if (tab === 'original' && !canShowOriginal) setTab('extracted')
  }, [canShowOriginal, tab])

  const loadFile = useCallback((): Promise<{ blob: Blob; contentType: string }> => {
    if (!fileId || !dataSource.loadFileContent) {
      return Promise.reject(new Error(t.knowledge.viewerError))
    }
    return dataSource.loadFileContent(fileId)
  }, [dataSource, fileId, t])

  function switchTab(nextTab: ViewerTab) {
    setTab(nextTab)
    if (nextTab === 'original') setOriginalOpened(true)
  }

  const title = documentState.kind === 'ready' ? documentState.document.title : target.title ?? ''

  return (
    <div aria-modal="true" className="fixed inset-0 z-50" role="dialog">
      <ResizablePanelGroup className="h-full w-full" orientation="horizontal">
        <ResizablePanel defaultSize="58%" minSize="20%">
          <button
            aria-label={t.knowledge.viewerClose}
            className="h-full w-full cursor-default bg-background/60 backdrop-blur-sm"
            onClick={onClose}
            type="button"
          />
        </ResizablePanel>
        <ResizableHandle aria-label={t.knowledge.viewerResize} />
        <ResizablePanel
          className="min-h-0 min-w-0 overflow-hidden"
          defaultSize="42%"
          maxSize="80%"
          minSize="30%"
        >
          <aside className="flex h-full w-full flex-col border-l border-border bg-background shadow-[0_8px_28px_-12px_var(--shadow-soft)]">
            <header className="flex inqtrix-panel-header items-center gap-2 border-b border-border px-3">
          <div className="min-w-0 flex-1" title={target.collectionLabel ?? undefined}>
            <h2 className="truncate t-section text-foreground">{title}</h2>
          </div>

          {tab === 'extracted' && matches.length > 0 && (
            <div className="flex shrink-0 items-center gap-0.5 rounded-md border border-border bg-surface px-1 py-0.5">
              <span className="px-1 t-hint tabular-nums text-muted-foreground">
                {t.knowledge.viewerMatches} {activeMatch + 1}/{matches.length}
              </span>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    aria-label={t.knowledge.viewerPrevMatch}
                    className="size-6 text-muted-foreground hover:text-foreground"
                    onClick={() => setActiveMatch((current) => (current - 1 + matches.length) % matches.length)}
                    size="icon"
                    type="button"
                    variant="ghost"
                  >
                    <ChevronUp className="size-3.5" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{t.knowledge.viewerPrevMatch}</TooltipContent>
              </Tooltip>
              <Tooltip>
                <TooltipTrigger asChild>
                  <Button
                    aria-label={t.knowledge.viewerNextMatch}
                    className="size-6 text-muted-foreground hover:text-foreground"
                    onClick={() => setActiveMatch((current) => (current + 1) % matches.length)}
                    size="icon"
                    type="button"
                    variant="ghost"
                  >
                    <ChevronDown className="size-3.5" />
                  </Button>
                </TooltipTrigger>
                <TooltipContent>{t.knowledge.viewerNextMatch}</TooltipContent>
              </Tooltip>
            </div>
          )}

          {canShowOriginal ? (
            <div className="grid h-7 shrink-0 grid-cols-2 rounded-md bg-surface p-0.5">
              {(['extracted', 'original'] as const).map((tabKey) => {
                const isActive = tab === tabKey
                return (
                  <button
                    aria-pressed={isActive}
                    className={cn(
                      'inline-flex items-center justify-center rounded px-2 text-xs font-medium transition-colors',
                      isActive
                        ? 'bg-background text-foreground shadow-[0_1px_2px_var(--shadow-hairline)]'
                        : 'text-muted-foreground hover:text-foreground',
                    )}
                    key={tabKey}
                    onClick={() => switchTab(tabKey)}
                    type="button"
                  >
                    {tabKey === 'extracted' ? t.knowledge.viewerExtracted : t.knowledge.viewerOriginal}
                  </button>
                )
              })}
            </div>
          ) : null}

          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                aria-label={t.knowledge.viewerClose}
                className="size-7 shrink-0 text-muted-foreground hover:text-foreground"
                onClick={onClose}
                size="icon"
                type="button"
                variant="ghost"
              >
                <X className="size-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent>{t.knowledge.viewerClose}</TooltipContent>
          </Tooltip>
        </header>

        {tab === 'extracted' ? (
          <ScrollArea className="min-h-0 flex-1">
            <div className="w-full px-6 py-6">
              {documentState.kind === 'loading' && (
                <p className="t-hint text-muted-foreground">{t.knowledge.viewerLoading}</p>
              )}
              {documentState.kind === 'error' && (
                <p className="t-meta text-destructive">{documentState.message}</p>
              )}
              {documentState.kind === 'ready' && (
                <>
                  {target.highlightTargets.length > 0 && matches.length === 0 && (
                    <p className="mb-3 t-hint text-muted-foreground">{t.knowledge.viewerNoMatches}</p>
                  )}
                  <div className="whitespace-pre-wrap t-body text-foreground">
                    {segments.map((segment, index) => (
                      segment.rangeIndex === null ? (
                        <span key={index}>{segment.text}</span>
                      ) : (
                        <mark
                          className={cn(
                            'rounded-sm bg-brand-subtle px-0.5 text-brand',
                            segment.rangeIndex === activeMatch && 'ring-1 ring-brand/50',
                          )}
                          key={index}
                          ref={segment.rangeIndex === activeMatch
                            ? (node) => {
                              activeMatchRef.current = node
                            }
                            : undefined}
                        >
                          {segment.text}
                        </mark>
                      )
                    ))}
                  </div>
                </>
              )}
            </div>
          </ScrollArea>
        ) : null}
        {originalOpened ? (
          <div className={cn('flex min-h-0 flex-1 flex-col', tab !== 'original' && 'hidden')}>
            {canShowOriginal && fileId ? (
              <OriginalFileTab fileName={title} load={loadFile} />
            ) : null}
          </div>
        ) : null}
          </aside>
        </ResizablePanel>
      </ResizablePanelGroup>
    </div>
  )
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null
}
