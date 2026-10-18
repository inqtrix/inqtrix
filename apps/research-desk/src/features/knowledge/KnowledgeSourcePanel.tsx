import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'
import { BadgeCheck, ChevronDown, ChevronLeft, ChevronUp, ExternalLink, FileText, PanelRightClose } from '@/components/icons'
import { Button } from '@/components/ui/button'
import { ResizableHandle, ResizablePanel, ResizablePanelGroup } from '@/components/ui/resizable'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLocale } from '@/i18n/LocaleProvider'
import { cn } from '@/lib/utils'
import type { KnowledgeDocumentText } from '@/features/researchRuns/types'
import type { KnowledgeReferenceRecord, KnowledgeThreadItemRecord } from '@/features/project/types'
import { OriginalFileTab } from '@/features/files/OriginalFileTab'
import { useActiveMatchScroll } from '@/features/scroll/useActiveMatchScroll'
import { viewerErrorText } from './viewerErrorText'
import {
  findFirstMatchingTarget,
  splitByRanges,
  type HighlightRange,
} from './highlight'
import { excerptHighlightRanges, HighlightedExcerpt } from './CitationExcerpt'
import {
  activeCitationGroup,
  citationKey,
  citationViews,
  firstOpenableCitation,
  groupCitationsByDocument,
} from './citations'
import { CitationGroupList } from './CitationRow'
import type { DocumentViewerTarget, KnowledgeDataSource } from './types'
import { useFileAccessProbe } from './useFileAccessProbe'

/** "Beleg" = the cited excerpt (the retrieved chunk, highlighted); "document" =
 * the full extracted text with span highlight; "source" = the original PDF
 * opened at the cited page with a soft page highlight. */
type ReaderTab = 'excerpt' | 'document' | 'source'

type DocumentState =
  | { kind: 'idle' }
  | { kind: 'loading' }
  | { kind: 'error'; message: string }
  | { kind: 'ready'; document: KnowledgeDocumentText }

/**
 * Right-hand LAYOUT panel for the knowledge Ask view (the counterpart of the
 * Research Desk's `ReportPanel`): embedded beside the conversation, not a popup.
 * It stays focused on cited sources and document reading; Ask run steps are
 * shown inline with their Q&A entry.
 *
 * `item` is null in Find mode (only the reader); `target` is the
 * document currently open in the reader.
 */
export function KnowledgeSourcePanel({
  dataSource,
  item,
  onClose,
  onSelectReference,
  target,
}: {
  dataSource: KnowledgeDataSource
  item: KnowledgeThreadItemRecord | null
  /** Closes the panel. The shared header toggle owns this on desktop, but the
   * panel keeps its own close because on mobile the panel is a full overlay
   * (absolute inset-0 z-30) that occludes that header toggle — same occlusion
   * case as the report panel's fullscreen mode. */
  onClose: () => void
  onSelectReference: (reference: KnowledgeReferenceRecord) => void
  target: DocumentViewerTarget | null
}) {
  const { t } = useLocale()
  const references = item?.answer?.references ?? []
  const quotes = item?.answer?.quotes ?? []
  // Snippet-first rows grouped by document: the supporting passage leads, the
  // filename appears once per document (no stack of identical names).
  const citationGroups = groupCitationsByDocument(
    citationViews(references, quotes, t.knowledge.viewerSection),
  )
  const activeKey = target ? citationKey(target.documentId, target.chunkIndex) : null

  // Document-centric Belege: with a source open the list scopes to that one
  // document (its passages); "Alle Belege" returns to the full list without
  // closing the reader. The reset reacts to the active KEY (document + chunk),
  // not only documentId — switching to another passage of the SAME document
  // changes only the chunk and must re-focus too.
  const [showAllSources, setShowAllSources] = useState(false)
  useEffect(() => {
    setShowAllSources(false)
  }, [activeKey])

  const scopedGroup = target && !showAllSources
    ? activeCitationGroup(citationGroups, target.documentId)
    : null
  const visibleGroups = scopedGroup ? [scopedGroup] : citationGroups
  const canReturnToAllSources = Boolean(scopedGroup) && citationGroups.length > 1

  function focusReference(reference: KnowledgeReferenceRecord) {
    setShowAllSources(false)
    onSelectReference(reference)
  }

  // The occurrence list (back-to-all-sources affordance + grouped citations),
  // shared by the split layout and the no-reader layout.
  const listContent = (
    <div className="px-3 py-2">
      {canReturnToAllSources && (
        <button
          className="mb-1.5 flex w-full items-center gap-1 rounded-md px-1.5 py-1 text-left t-meta-sm text-muted-foreground transition-colors hover:bg-accent/60 hover:text-foreground"
          onClick={() => setShowAllSources(true)}
          type="button"
        >
          <ChevronLeft className="icon-xs shrink-0" />
          {t.knowledge.panelAllSources}
        </button>
      )}
      <CitationGroupList
        activeDocumentId={target?.documentId ?? null}
        activeKey={activeKey}
        groups={visibleGroups}
        onOpen={(view) => focusReference(view.reference)}
        onOpenDocument={(group) => {
          const view = firstOpenableCitation(group)
          if (view) focusReference(view.reference)
        }}
      />
    </div>
  )

  return (
    <aside className="flex h-full w-full min-w-0 flex-col border-l border-border bg-background">
      <div className="flex inqtrix-panel-header items-center justify-between gap-2 border-b border-border px-3">
        <h2 className="truncate t-section text-foreground">{t.knowledge.panelSources}</h2>
        <Tooltip>
          <TooltipTrigger asChild>
            <Button
              aria-label={t.knowledge.panelCollapse}
              className="size-7 shrink-0 text-muted-foreground hover:text-foreground"
              onClick={onClose}
              size="icon"
              type="button"
              variant="ghost"
            >
              <PanelRightClose className="size-4" />
            </Button>
          </TooltipTrigger>
          <TooltipContent side="left">{t.knowledge.panelCollapse}</TooltipContent>
        </Tooltip>
      </div>

      <div className="flex min-h-0 flex-1 flex-col">
        {references.length > 0 && target ? (
          // List + reader share the panel via a draggable vertical split, so the
          // reader never starves the occurrence list of height and the user can
          // choose the balance (the same primitive as the other modes' splits).
          // The list scrolls independently (type="auto" surfaces the scrollbar
          // whenever the occurrences overflow).
          <ResizablePanelGroup className="min-h-0 flex-1" orientation="vertical">
            <ResizablePanel className="flex min-h-0 flex-col overflow-hidden" defaultSize="38%" minSize="20%">
              <ScrollArea className="min-h-0 flex-1" type="auto">
                {listContent}
              </ScrollArea>
            </ResizablePanel>
            <ResizableHandle aria-label={t.knowledge.panelResizeVertical} orientation="vertical" />
            <ResizablePanel className="flex min-h-0 flex-col overflow-hidden" defaultSize="62%" minSize="30%">
              <DocumentReader collectionLabel={target.collectionLabel} dataSource={dataSource} target={target} />
            </ResizablePanel>
          </ResizablePanelGroup>
        ) : references.length > 0 ? (
          // No reader open: the occurrence list fills the panel.
          <ScrollArea className="min-h-0 flex-1" type="auto">
            {listContent}
          </ScrollArea>
        ) : target ? (
          <DocumentReader collectionLabel={target.collectionLabel} dataSource={dataSource} target={target} />
        ) : (
          <div className="flex flex-1 items-center justify-center px-6 text-center">
            <p className="t-meta text-muted-foreground">{t.knowledge.panelPickSource}</p>
          </div>
        )}
      </div>
    </aside>
  )
}

/**
 * The citation reader. Default "Beleg" tab shows the EXACT retrieved chunk with
 * the cited span highlighted (passage-in-context) plus provenance metadata — the
 * verify-the-source view. "Dokument" opens the full extracted text with the span
 * highlighted + match navigation, loaded on demand. The PDF original is not here
 * (it lives in the file-library preview); the panel shell owns the close chrome.
 *
 * Shared with the AGENT evidence canvas: a K citation there
 * opens the same reader, so "verify the source" looks and behaves
 * identically in both desks. It is chrome-free on purpose — the caller
 * supplies the container and any close affordance.
 */
export function DocumentReader({
  collectionLabel,
  dataSource,
  target,
}: {
  collectionLabel?: string
  dataSource: KnowledgeDataSource
  target: DocumentViewerTarget
}) {
  const { t } = useLocale()
  const excerpt = target.excerpt?.trim() ? target.excerpt : ''
  const hasExcerpt = excerpt.length > 0
  const [tab, setTab] = useState<ReaderTab>(hasExcerpt ? 'excerpt' : 'document')
  const [documentState, setDocumentState] = useState<DocumentState>({ kind: 'idle' })
  // Load trigger for the full document, kept SEPARATE from documentState.kind:
  // the load effect must not depend on a value it mutates, or setting 'loading'
  // re-invalidates the effect mid-flight and the load never resolves (the
  // "Lade Dokument…"-forever bug).
  const [docRequested, setDocRequested] = useState(
    !hasExcerpt || Boolean(dataSource.canLoadFileContent),
  )
  const [activeMatch, setActiveMatch] = useState(0)
  const activeMatchRef = useRef<HTMLElement | null>(null)

  // New document → drop the previously loaded one and re-arm the loader.
  // Keyed on the document alone (a changed excerpt is a new citation, handled
  // below): another citation of the SAME document keeps the loaded text.
  // Resetting it left the Dokument tab empty, because the loader's own keys
  // did not change and it never ran again.
  useEffect(() => {
    setDocumentState({ kind: 'idle' })
    setDocRequested(!hasExcerpt || Boolean(dataSource.canLoadFileContent))
  }, [dataSource.canLoadFileContent, target.documentId])

  // New citation → back to the Beleg view and re-focus the reader on its match
  // (keyed on document AND chunk, so another occurrence of the same document
  // scrolls to its own match); a no-excerpt citation's only view is the
  // document, so it asks for it.
  useEffect(() => {
    setTab(hasExcerpt ? 'excerpt' : 'document')
    setActiveMatch(0)
    if (!hasExcerpt) setDocRequested(true)
  }, [target.documentId, target.chunkIndex, hasExcerpt])

  // Load the FULL document once requested (Dokument tab opened, or no excerpt).
  // The Beleg view needs only the in-citation excerpt, so a small citation never
  // pulls a large document. Deps deliberately EXCLUDE documentState.kind.
  useEffect(() => {
    if (!docRequested) return
    let ignore = false
    setDocumentState({ kind: 'loading' })
    dataSource
      .loadDocumentText(target.documentId)
      .then((document) => {
        if (!ignore) setDocumentState({ document, kind: 'ready' })
      })
      .catch((error: unknown) => {
        if (!ignore) {
          setDocumentState({
            kind: 'error',
            message: viewerErrorText(error, t),
          })
        }
      })
    return () => {
      ignore = true
    }
  }, [docRequested, target.documentId, dataSource, t])

  const openDocument = () => {
    setTab('document')
    setDocRequested(true)
  }

  const excerptMatches = useMemo<HighlightRange[]>(
    () => (hasExcerpt ? excerptHighlightRanges(excerpt, target.highlightTargets) : []),
    [excerpt, hasExcerpt, target.highlightTargets],
  )
  const documentText = documentState.kind === 'ready' ? documentState.document.text : ''
  const matches = useMemo<HighlightRange[]>(
    () => (documentText ? findFirstMatchingTarget(documentText, target.highlightTargets) : []),
    [documentText, target.highlightTargets],
  )
  const segments = useMemo(() => splitByRanges(documentText, matches), [documentText, matches])
  useEffect(() => {
    setActiveMatch(0)
  }, [matches.length])
  useActiveMatchScroll({
    activeIndex: activeMatch,
    contentKey: `${target.documentId}:${target.chunkIndex ?? 'none'}`,
    enabled: tab === 'document' && documentState.kind === 'ready',
    targetRef: activeMatchRef,
  })

  const title = documentState.kind === 'ready' ? documentState.document.title : target.title ?? ''
  const sectionLabel = typeof target.chunkIndex === 'number'
    ? t.knowledge.viewerSection.replace('{n}', String(target.chunkIndex + 1))
    : null
  const subtitle = [collectionLabel, sectionLabel].filter(Boolean).join(' · ')

  // The original PDF (server file id lives in the loaded document's metadata).
  // The "Quelle" tab shows when the deployment serves file content; once the
  // document loads, a missing file id reads as "no original".
  const fileId = documentState.kind === 'ready'
    ? stringOrNull(documentState.document.metadata.file_id)
    : null
  const fileAccess = useFileAccessProbe(dataSource, fileId)
  const canShowSource = fileAccess === 'allowed'
  useEffect(() => {
    if (tab === 'source' && !canShowSource) {
      setTab(hasExcerpt ? 'excerpt' : 'document')
    }
  }, [canShowSource, hasExcerpt, tab])
  const loadFile = useCallback((): Promise<{ blob: Blob; contentType: string }> => {
    if (!fileId || !dataSource.loadFileContent) {
      return Promise.reject(new Error(t.knowledge.viewerError))
    }
    return dataSource.loadFileContent(fileId)
  }, [dataSource, fileId, t])
  // Opening the source PDF needs the document's metadata (for its file id), so
  // it arms the same lazy document loader the Dokument tab uses.
  const openSource = () => {
    setTab('source')
    setDocRequested(true)
  }
  // Tabs are independent of the excerpt: a citation WITHOUT an excerpt (older
  // payload, or grounding off) still reaches Dokument + Quelle. Excerpt shows
  // only when there is one; Quelle only when the deployment serves files.
  const readerTabs: ReaderTab[] = [
    ...(hasExcerpt ? (['excerpt'] as ReaderTab[]) : []),
    'document' as ReaderTab,
    ...(canShowSource ? (['source'] as ReaderTab[]) : []),
  ]

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="shrink-0 border-b border-border px-3 py-2">
        <div className="flex min-w-0 items-start gap-2">
          <FileText className="mt-0.5 icon-sm shrink-0 text-foreground/80" />
          <div className="min-w-0 flex-1">
            <div className="flex min-w-0 items-center gap-1.5">
              <h3 className="min-w-0 truncate t-list text-foreground">{title || target.title}</h3>
              {target.verified && (
                <Tooltip>
                  <TooltipTrigger asChild>
                    <span className="inline-flex shrink-0 items-center gap-0.5 rounded bg-success-subtle px-1 py-0.5 t-hint font-medium text-success">
                      <BadgeCheck className="icon-xs" />
                      {t.knowledge.viewerVerified}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent side="top">{t.knowledge.viewerVerifiedTooltip}</TooltipContent>
                </Tooltip>
              )}
            </div>
            {subtitle && <p className="truncate t-meta-sm text-muted-foreground">{subtitle}</p>}
          </div>
        </div>
        {readerTabs.length > 1 && (
          <div
            className={cn(
              'mt-2 grid h-7 min-w-36 rounded-md bg-surface p-0.5',
              readerTabs.length === 3 ? 'grid-cols-3' : 'grid-cols-2',
            )}
          >
            {readerTabs.map((tabKey) => (
              <button
                aria-pressed={tab === tabKey}
                className={cn(
                  'inline-flex items-center justify-center rounded px-2 text-xs font-medium transition-colors',
                  tab === tabKey
                    ? 'bg-background text-foreground shadow-[0_1px_2px_var(--shadow-hairline)]'
                    : 'text-muted-foreground hover:text-foreground',
                )}
                key={tabKey}
                onClick={() => {
                  if (tabKey === 'document') openDocument()
                  else if (tabKey === 'source') openSource()
                  else setTab('excerpt')
                }}
                type="button"
              >
                {tabKey === 'excerpt'
                  ? t.knowledge.viewerExcerpt
                  : tabKey === 'document'
                    ? t.knowledge.viewerDocument
                    : t.knowledge.viewerSource}
              </button>
            ))}
          </div>
        )}
        {tab === 'document' && matches.length > 0 && (
          <div className="mt-2 flex w-fit items-center gap-0.5 rounded-md border border-border bg-surface px-1 py-0.5">
            <span className="px-1 t-hint tabular-nums text-muted-foreground">
              {t.knowledge.viewerMatches} {activeMatch + 1}/{matches.length}
            </span>
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
          </div>
        )}
      </div>

      {tab === 'excerpt' ? (
        <ScrollArea className="min-h-0 flex-1">
          <div className="w-full px-5 py-5">
            <p className="mb-2 t-hint font-semibold uppercase tracking-wide text-muted-foreground">
              {t.knowledge.viewerExcerptLabel}
            </p>
            <blockquote className="border-l-2 border-brand/40 pl-3 t-meta leading-6 text-foreground/90">
              <HighlightedExcerpt ranges={excerptMatches} text={excerpt} />
            </blockquote>
            {target.highlightTargets.length > 0 && excerptMatches.length === 0 && (
              <p className="mt-3 t-hint text-muted-foreground">{t.knowledge.viewerExcerptNoSpan}</p>
            )}
            <button
              className="mt-4 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1 t-meta-sm text-foreground transition-colors hover:bg-accent/60"
              onClick={openDocument}
              type="button"
            >
              <ExternalLink className="icon-xs" />
              {t.knowledge.viewerOpenInDocument}
            </button>
          </div>
        </ScrollArea>
      ) : tab === 'source' ? (
        <div className="flex min-h-0 flex-1 flex-col">
          {documentState.kind === 'loading' && (
            <p className="px-5 py-5 t-hint text-muted-foreground">{t.knowledge.viewerLoading}</p>
          )}
          {documentState.kind === 'error' && (
            <p className="px-5 py-5 t-meta text-destructive">{documentState.message}</p>
          )}
          {documentState.kind === 'ready'
            ? fileId && canShowSource
              ? (
                <OriginalFileTab
                  fileName={title}
                  highlightPage={target.pageNumber ?? null}
                  highlightTargets={target.highlightTargets}
                  load={loadFile}
                />
              )
              : (
                <p className="px-5 py-5 t-meta text-muted-foreground">{t.knowledge.viewerSourceNone}</p>
              )
            : null}
        </div>
      ) : (
        <ScrollArea className="min-h-0 flex-1">
          <div className="w-full px-5 py-5">
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
                          'rounded-sm bg-brand/20 px-0.5 text-foreground',
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
      )}
    </div>
  )
}

/** Non-empty trimmed string, else null — for optional document metadata. */
function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' ? value : null
}
